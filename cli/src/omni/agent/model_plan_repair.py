"""One bounded model proposal for repairing objective provider-schema errors."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from omni.agent.intent_plan import IntentPlan
from omni.agent.plan_patch import (
    PlanPatch,
    PlanPatchOp,
    allowed_patch_paths,
    apply_plan_patch,
)
from omni.agent.plan_revision import PlanRevision
from omni.agent.plan_validator import PlanFinding
from omni.agent.provider_binding import provider_contract_hash

_PATCH_TOOL_NAME = "submit_plan_patch"
_SCHEMA_VALUE_KEYS = frozenset(
    {
        "$dynamicRef",
        "$recursiveRef",
        "$ref",
        "const",
        "deprecated",
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxContains",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minContains",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "pattern",
        "type",
        "title",
        "uniqueItems",
        "x-omni",
        "x_omni",
    }
)
_SCHEMA_CHILD_KEYS = frozenset(
    {
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SCHEMA_CHILD_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_CHILD_MAP_KEYS = frozenset(
    {"dependentSchemas", "patternProperties", "properties"}
)


@dataclass(frozen=True, slots=True)
class ModelPlanRepairResult:
    """A candidate patch; the outer lifecycle still recompiles and validates it."""

    plan: IntentPlan
    patch: PlanPatch


@dataclass(slots=True)
class ModelPlanRepairAttempt:
    """Observable metering/audit state for the one bounded model request."""

    sent: bool = False
    response: Any = None
    system_prompt: str = ""
    user_prompt: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RepairProviderContract:
    """Exact provider contract authorized for one finding's repair context."""

    contract_key: str
    provider_binding_id: str
    provider_name: str
    provider_source: str
    provider_version: str
    provider_contract_hash: str
    input_schema: dict[str, Any]


def repair_provider_contract(entry: Any, *, binding_id: str) -> RepairProviderContract:
    """Build a content-addressed repair contract from one selected provider."""
    contract_hash = provider_contract_hash(entry)
    return RepairProviderContract(
        contract_key=f"provider-contract:{contract_hash}",
        provider_binding_id=binding_id,
        provider_name=str(getattr(entry, "name", "") or ""),
        provider_source=str(getattr(entry, "source", "") or ""),
        provider_version=str(getattr(entry, "version", "") or ""),
        provider_contract_hash=contract_hash,
        input_schema=copy.deepcopy(getattr(entry, "input_schema", None) or {}),
    )


class ModelPlanRepairer:
    """Ask an online model for at most one host-allowlisted typed-plan patch."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm
        self.last_attempt = ModelPlanRepairAttempt()

    async def repair(
        self,
        plan: IntentPlan,
        findings: list[PlanFinding],
        *,
        revision: PlanRevision,
        provider_contracts: dict[str, RepairProviderContract] | None = None,
    ) -> ModelPlanRepairResult | None:
        self.last_attempt = ModelPlanRepairAttempt()
        if _is_offline_llm(self._llm):
            self.last_attempt.reason = "offline provider"
            return None

        patchable = [
            finding
            for finding in findings
            if allowed_patch_paths(plan, [finding])
        ]
        if not patchable:
            self.last_attempt.reason = "no patchable findings"
            return None
        schema_projection, patchable, contract_keys = _repair_schema_projection(
            provider_contracts or {},
            patchable,
        )
        if not patchable:
            self.last_attempt.reason = "no exact provider contract for patchable findings"
            return None
        paths = allowed_patch_paths(plan, patchable)
        if not paths:
            self.last_attempt.reason = "no host-allowed patch paths"
            return None
        system_prompt = (
            "Repair only objective provider-schema violations in the typed-plan "
            "fields allowed by the host. Use the selected provider's exact "
            "field names, nested structure, types, formats, and enum values. "
            "Submit exactly one patch tool call. Never change policy, "
            "identity, budgets, grounded facts, or DAG structure."
        )
        user_prompt = json.dumps(
            {
                "provider_contracts": schema_projection,
                "base_revision": revision.content_hash,
                "allowed_paths": sorted(paths),
                # The repair model needs the user's semantics and the exact
                # patchable values, not the full executable plan. In
                # particular, derived provider inputs, policies, approvals,
                # task identity, and unrelated fields stay on the host.
                "user_request": plan.user_message,
                "current_values": _current_values(plan, paths),
                "findings": [
                    _repair_finding_projection(
                        finding,
                        contract_key=contract_keys[finding.finding_id],
                    )
                    for finding in patchable
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        self.last_attempt = ModelPlanRepairAttempt(
            sent=True,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            reason="request sent",
        )
        try:
            response = await self._llm.chat_with_tools(
                [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                [_patch_tool(paths)],
                tool_choice="required",
                temperature=0.0,
                max_tokens=1024,
            )
        except Exception:  # noqa: BLE001 - repair is optional; the floor remains available.
            self.last_attempt.reason = "provider request failed"
            return None
        self.last_attempt.response = response
        tool_calls = list(getattr(response, "tool_calls", None) or [])
        if len(tool_calls) != 1 or tool_calls[0].name != _PATCH_TOOL_NAME:
            self.last_attempt.reason = "response did not contain one patch tool call"
            return None
        if getattr(tool_calls[0], "arguments_error", None):
            self.last_attempt.reason = "patch tool arguments were malformed"
            return None
        operations = _parse_operations(tool_calls[0].arguments)
        if not operations:
            self.last_attempt.reason = "patch contained no valid operations"
            return None
        patch = PlanPatch(
            base_revision=revision.content_hash,
            finding_ids=[finding.finding_id for finding in patchable],
            operations=operations,
        )
        try:
            repaired = apply_plan_patch(
                plan,
                patch,
                current_revision=revision,
                findings=findings,
            )
        except (TypeError, ValueError):
            self.last_attempt.reason = "host rejected the proposed patch"
            return None
        self.last_attempt.reason = "candidate produced"
        return ModelPlanRepairResult(
            plan=repaired,
            patch=patch,
        )


def _patch_tool(paths: frozenset[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _PATCH_TOOL_NAME,
            "description": "Submit one bounded patch for the detected plan findings.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "operations": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": len(paths),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "op": {"type": "string", "enum": ["add", "replace"]},
                                "path": {
                                    "type": "string",
                                    "enum": sorted(paths),
                                },
                                "value": {},
                            },
                            "required": ["op", "path", "value"],
                        },
                    }
                },
                "required": ["operations"],
            },
        },
    }


def _parse_operations(arguments: Any) -> list[PlanPatchOp]:
    if not isinstance(arguments, dict):
        return []
    raw_operations = arguments.get("operations")
    if not isinstance(raw_operations, list):
        return []
    operations: list[PlanPatchOp] = []
    for raw in raw_operations:
        if not isinstance(raw, dict) or "value" not in raw:
            return []
        op = str(raw.get("op") or "")
        path = str(raw.get("path") or "")
        if op not in {"add", "replace"} or not path:
            return []
        operations.append(PlanPatchOp(op=op, path=path, value=raw["value"]))
    return operations


def _current_values(
    plan: IntentPlan,
    paths: frozenset[str],
) -> dict[str, Any]:
    """Project only host-allowlisted fields into the repair prompt."""
    payload = plan.to_dict()
    values: dict[str, Any] = {}
    for path in sorted(paths):
        current: Any = payload
        try:
            for raw_part in path.split("/")[1:]:
                part = raw_part.replace("~1", "/").replace("~0", "~")
                current = (
                    current[int(part)]
                    if isinstance(current, list)
                    else current[part]
                )
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        values[path] = current
    return values


def project_repair_schema(
    schema: dict[str, Any],
    field_names: set[str] | frozenset[str],
) -> dict[str, Any]:
    """Project one provider schema to exact repair fields and reachable definitions."""
    if _has_invalid_reference(schema):
        return {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    selected = {
        name: _sanitize_schema(properties[name])
        for name in sorted(field_names)
        if name in properties
    }
    if not selected:
        return {}

    projected = _sanitize_schema(schema, selected_properties=frozenset(selected))
    projected["properties"] = selected
    required = schema.get("required")
    if isinstance(required, list):
        selected_required = [
            name
            for name in required
            if isinstance(name, str) and name in selected
        ]
        if selected_required:
            projected["required"] = selected_required

    definitions = _reachable_definitions(schema, projected)
    if definitions:
        projected["$defs"] = definitions
    legacy_definitions = _reachable_definitions(
        schema,
        projected,
        definition_key="definitions",
    )
    if legacy_definitions:
        projected["definitions"] = legacy_definitions
    if _has_dangling_internal_reference(projected):
        return {}
    return projected


def repair_target_field(finding: PlanFinding) -> str:
    """Return the provider root field addressed by one exact plan pointer."""
    tokens = _pointer_tokens(str(finding.field_path or ""))
    if len(tokens) >= 4 and tokens[:1] == ["workflow_steps"] and tokens[2] == "input":
        return tokens[3]
    if len(tokens) >= 3 and tokens[:1] == ["capability_inputs"]:
        return tokens[2]
    return ""


def _pointer_tokens(path: str) -> list[str]:
    if not path.startswith("/"):
        return []
    try:
        return [
            token.replace("~1", "/").replace("~0", "~")
            for token in path.split("/")[1:]
        ]
    except (AttributeError, TypeError):
        return []


def _repair_schema_projection(
    provider_contracts: dict[str, RepairProviderContract],
    findings: list[PlanFinding],
) -> tuple[
    dict[str, dict[str, Any]],
    list[PlanFinding],
    dict[str, str],
]:
    if not findings:
        return {}, [], {}

    scoped: list[tuple[PlanFinding, RepairProviderContract, str]] = []
    fields_by_contract: dict[str, set[str]] = {}
    contracts_by_key: dict[str, RepairProviderContract] = {}
    invalid_keys: set[str] = set()
    for finding in findings:
        contract = provider_contracts.get(finding.finding_id)
        field_name = repair_target_field(finding)
        if (
            contract is None
            or not field_name
            or not _finding_matches_contract(finding, contract)
        ):
            continue
        existing = contracts_by_key.setdefault(contract.contract_key, contract)
        if (
            existing.provider_name != contract.provider_name
            or existing.provider_source != contract.provider_source
            or existing.provider_version != contract.provider_version
            or existing.provider_contract_hash != contract.provider_contract_hash
            or existing.input_schema != contract.input_schema
        ):
            invalid_keys.add(contract.contract_key)
            continue
        fields_by_contract.setdefault(contract.contract_key, set()).add(field_name)
        scoped.append((finding, contract, field_name))

    projected: dict[str, dict[str, Any]] = {}
    for contract_key, field_names in sorted(fields_by_contract.items()):
        if contract_key in invalid_keys:
            continue
        contract = contracts_by_key[contract_key]
        input_schema = project_repair_schema(contract.input_schema, field_names)
        if not input_schema:
            continue
        projected[contract_key] = {
            "provider_name": contract.provider_name,
            "provider_source": contract.provider_source,
            "provider_version": contract.provider_version,
            "provider_contract_hash": contract.provider_contract_hash,
            "input_schema": input_schema,
        }

    eligible = [
        finding
        for finding, contract, _ in scoped
        if contract.contract_key in projected
        and contract.contract_key not in invalid_keys
    ]
    contract_keys = {
        finding.finding_id: contract.contract_key
        for finding, contract, _ in scoped
        if finding in eligible
    }
    return projected, eligible, contract_keys


def _finding_matches_contract(
    finding: PlanFinding,
    contract: RepairProviderContract,
) -> bool:
    return bool(
        finding.provider_binding_id
        and finding.provider_source
        and finding.provider_contract_hash
        and contract.contract_key
        == f"provider-contract:{contract.provider_contract_hash}"
        and finding.provider_binding_id == contract.provider_binding_id
        and finding.provider_source == contract.provider_source
        and finding.provider_contract_hash == contract.provider_contract_hash
    )


def _sanitize_schema(
    schema: Any,
    *,
    selected_properties: frozenset[str] | None = None,
) -> Any:
    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, dict):
        return {}
    clean: dict[str, Any] = {}
    for key in sorted(_SCHEMA_VALUE_KEYS):
        if key in schema:
            value = schema[key]
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                not isinstance(value, str) or not value.startswith("#")
            ):
                continue
            clean[key] = (
                _sanitize_annotation(value)
                if key in {"x-omni", "x_omni"}
                else copy.deepcopy(value)
            )
    for key in sorted(_SCHEMA_CHILD_KEYS):
        child = schema.get(key)
        if isinstance(child, (dict, bool)):
            clean[key] = _sanitize_schema(
                child,
                selected_properties=(
                    selected_properties
                    if key in {"else", "if", "not", "then"}
                    else None
                ),
            )
    for key in sorted(_SCHEMA_CHILD_LIST_KEYS):
        children = schema.get(key)
        if isinstance(children, list):
            clean[key] = [
                _sanitize_schema(
                    child,
                    selected_properties=selected_properties,
                )
                for child in children
                if isinstance(child, (dict, bool))
            ]
    for key in sorted(_SCHEMA_CHILD_MAP_KEYS):
        children = schema.get(key)
        if isinstance(children, dict):
            clean[key] = {
                str(name): _sanitize_schema(child)
                for name, child in sorted(children.items())
                if (
                    key != "properties"
                    or selected_properties is None
                    or str(name) in selected_properties
                )
                if (
                    key != "dependentSchemas"
                    or selected_properties is None
                    or str(name) in selected_properties
                )
                if (
                    key != "patternProperties"
                    or selected_properties is None
                    or _pattern_matches_any(str(name), selected_properties)
                )
                if isinstance(child, (dict, bool))
            }
    required = schema.get("required")
    if isinstance(required, list):
        clean["required"] = [
            item
            for item in required
            if isinstance(item, str)
            and (
                selected_properties is None
                or item in selected_properties
            )
        ]
    dependent_required = schema.get("dependentRequired")
    if isinstance(dependent_required, dict):
        clean["dependentRequired"] = {
            str(name): [
                item
                for item in values
                if isinstance(item, str)
                and (
                    selected_properties is None
                    or item in selected_properties
                )
            ]
            for name, values in sorted(dependent_required.items())
            if isinstance(values, list)
            and (
                selected_properties is None
                or str(name) in selected_properties
            )
        }
    return clean


def _pattern_matches_any(
    pattern: str,
    property_names: frozenset[str],
) -> bool:
    try:
        return any(re.search(pattern, name) is not None for name in property_names)
    except re.error:
        return False


def _sanitize_annotation(value: Any) -> Any:
    """Keep provider hints while stripping secret-prone example material."""
    if isinstance(value, dict):
        return {
            str(key): _sanitize_annotation(item)
            for key, item in sorted(value.items())
            if str(key) not in {"default", "examples"}
        }
    if isinstance(value, list):
        return [_sanitize_annotation(item) for item in value]
    return copy.deepcopy(value)


def _reachable_definitions(
    schema: dict[str, Any],
    projected_value: dict[str, Any],
    *,
    definition_key: str = "$defs",
) -> dict[str, Any]:
    raw_definitions = schema.get(definition_key)
    if not isinstance(raw_definitions, dict):
        return {}
    pending = list(
        _definition_references(
            projected_value,
            definition_key=definition_key,
        )
    )
    projected: dict[str, Any] = {}
    while pending:
        name = pending.pop()
        if name in projected:
            continue
        raw = raw_definitions.get(name)
        if not isinstance(raw, (dict, bool)):
            continue
        clean = _sanitize_schema(raw)
        projected[name] = clean
        pending.extend(
            reference
            for reference in _definition_references(
                clean,
                definition_key=definition_key,
            )
            if reference not in projected
        )
    return {
        name: projected[name]
        for name in sorted(projected)
    }


def _definition_references(
    value: Any,
    *,
    definition_key: str,
) -> set[str]:
    references: set[str] = set()
    if not isinstance(value, dict):
        return references
    prefix = f"#/{definition_key}/"
    for key in ("$ref", "$dynamicRef", "$recursiveRef"):
        reference = value.get(key)
        if isinstance(reference, str) and reference.startswith(prefix):
            raw_name = reference[len(prefix) :].split("/", 1)[0]
            references.add(
                raw_name.replace("~1", "/").replace("~0", "~")
            )
    for child in value.values():
        if isinstance(child, dict):
            references.update(
                _definition_references(child, definition_key=definition_key)
            )
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, dict):
                    references.update(
                        _definition_references(
                            item,
                            definition_key=definition_key,
                        )
                    )
    return references


def _has_invalid_reference(value: Any) -> bool:
    if isinstance(value, list):
        return any(_has_invalid_reference(item) for item in value)
    if not isinstance(value, dict):
        return False
    for key in ("$ref", "$dynamicRef", "$recursiveRef"):
        if key not in value:
            continue
        reference = value[key]
        if not isinstance(reference, str) or (
            reference != "#" and not reference.startswith("#/")
        ):
            return True
    return any(
        _has_invalid_reference(child)
        for child in value.values()
    )


def _has_dangling_internal_reference(schema: dict[str, Any]) -> bool:
    return any(
        not _resolves_json_pointer(schema, reference)
        for reference in _internal_references(schema)
    )


def _internal_references(value: Any) -> set[str]:
    if isinstance(value, list):
        references: set[str] = set()
        for item in value:
            references.update(_internal_references(item))
        return references
    if not isinstance(value, dict):
        return set()
    references = {
        reference
        for key in ("$ref", "$dynamicRef", "$recursiveRef")
        if isinstance((reference := value.get(key)), str)
        and (reference == "#" or reference.startswith("#/"))
    }
    for child in value.values():
        references.update(_internal_references(child))
    return references


def _resolves_json_pointer(schema: dict[str, Any], reference: str) -> bool:
    if reference == "#":
        return True
    current: Any = schema
    for raw_token in reference[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (IndexError, TypeError, ValueError):
                return False
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            return False
    return True


def _repair_finding_projection(
    finding: PlanFinding,
    *,
    contract_key: str,
) -> dict[str, Any]:
    consumer = {
        key: value
        for key, value in (
            ("step_id", finding.step_id),
            ("deliverable_id", finding.deliverable_id),
            (
                "capability",
                finding.capability_instance or finding.capability,
            ),
        )
        if value
    }
    return {
        "code": finding.code,
        **({"consumer": consumer} if consumer else {}),
        **({"evidence": finding.evidence} if finding.evidence else {}),
        "expected": copy.deepcopy(finding.expected),
        "path": finding.field_path,
        "provider_contract": {
            "contract_key": contract_key,
            "provider_binding_id": finding.provider_binding_id,
            "provider_source": finding.provider_source,
            "provider_contract_hash": finding.provider_contract_hash,
        },
    }


def _is_offline_llm(llm: Any) -> bool:
    names = {
        str(getattr(llm, "model", "") or "").strip().lower(),
        str(getattr(llm, "provider", "") or "").strip().lower(),
    }
    return any(
        name in {"mock", "omni-mock", "offline", "scripted"}
        or name.startswith("mock-")
        or name.endswith("-mock")
        for name in names
        if name
    )
