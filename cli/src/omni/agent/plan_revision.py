"""Immutable snapshots and canonical identities for typed intent plans."""

from __future__ import annotations

import copy
import hashlib
import importlib.machinery
import json
import marshal
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omni.agent.intent_plan import IntentPlan
from omni.skills_runtime.registry import step_skill_source

_REVISION_METADATA_FIELDS = frozenset(
    {
        "revision",
        "revision_hash",
        "parent_revision_hash",
        "revision_source",
    }
)
_LOADED_ENTRYPOINT_IDENTITIES: dict[str, dict[str, Any]] = {}
_PROVIDER_BINDING_FIELDS = frozenset(
    {
        "consumer_kind",
        "consumer_id",
        "provider_name",
        "provider_source",
    }
)
_PROVIDER_AUDIT_FIELDS = frozenset(
    {
        "authority_renewal",
        "provider_authority_root",
        "provider_authority_renewals",
    }
)


def deep_clone_plan(plan: IntentPlan) -> IntentPlan:
    """Return a fully detached plan suitable for compile/repair experiments."""
    return IntentPlan.from_dict(copy.deepcopy(plan.to_dict()))


def canonical_plan_hash(plan: IntentPlan | dict[str, Any]) -> str:
    """Hash the semantic/execution plan without self-referential revision data."""
    payload = plan.to_dict() if isinstance(plan, IntentPlan) else copy.deepcopy(plan)
    for field_name in _REVISION_METADATA_FIELDS:
        payload.pop(field_name, None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="backslashreplace")
    return hashlib.sha256(encoded).hexdigest()


def registry_snapshot_hashes(
    registry: Any,
    plan: IntentPlan | None = None,
) -> tuple[str, str]:
    """Return stable catalog and contract hashes for one planning snapshot.

    Revisions retain only the hashes: the full schemas remain in the registry
    and planner trace, while the audit log can still prove which catalog and
    contract surface governed a candidate.
    """
    catalog: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    entries = list(registry.list_selectable())
    seen = {
        (
            str(getattr(entry, "source", "")),
            str(getattr(entry, "name", "")),
        )
        for entry in entries
    }
    if plan is not None:
        references = [
            (
                selection.skill,
                getattr(selection, "skill_source", ""),
            )
            for selection in plan.selected_skills
        ]
        references.extend(
            (
                str(step.get("skill_name") or step.get("skill") or ""),
                step_skill_source(step),
            )
            for step in plan.workflow_steps
        )
        resolver = getattr(registry, "resolve_ref", None)
        getter = getattr(registry, "get", None)
        for name, source in references:
            if not name:
                continue
            entry = (
                resolver(name, source)
                if callable(resolver)
                else getter(name)
                if callable(getter)
                else None
            )
            identity = (
                str(getattr(entry, "source", "")),
                str(getattr(entry, "name", "")),
            )
            if entry is not None and identity not in seen:
                entries.append(entry)
                seen.add(identity)
    executable_file_cache: dict[str, dict[str, Any]] = {}
    for entry in sorted(
        entries,
        key=lambda item: (
            str(getattr(item, "name", "")),
            str(getattr(item, "source", "")),
        ),
    ):
        identity = {
            "name": str(getattr(entry, "name", "")),
            "source": str(getattr(entry, "source", "")),
            "version": str(getattr(entry, "version", "")),
        }
        catalog.append(
            {
                **identity,
                "contract_level": str(getattr(entry, "contract_level", "")),
                "capabilities": list(getattr(entry, "capabilities", ()) or ()),
                "deliverables": list(getattr(entry, "deliverables", ()) or ()),
                "execution_identity": _provider_execution_identity(
                    entry,
                    file_cache=executable_file_cache,
                ),
            }
        )
        contracts.append(
            {
                **identity,
                "input_schema": copy.deepcopy(
                    getattr(entry, "input_schema", None) or {}
                ),
                "output_schema": copy.deepcopy(
                    getattr(entry, "output_schema", None) or {}
                ),
                "template_signatures": copy.deepcopy(
                    getattr(entry, "template_signatures", None) or {}
                ),
            }
        )
    return _canonical_payload_hash(catalog), _canonical_payload_hash(contracts)


def provider_authority_snapshot(
    entry: Any | None,
    *,
    file_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Snapshot one provider's executable, admission, and contract identity."""
    if entry is None:
        return {}
    execution_identity = _provider_execution_identity(
        entry,
        file_cache=file_cache,
    )
    kind = getattr(entry, "kind", "")
    kind_value = str(getattr(kind, "value", kind) or "")
    if kind_value == "prompt_only":
        prompt_runtime = native_provider_authority_snapshot(
            "react_delegate",
            file_cache=file_cache,
        )
        execution_identity["prompt_runtime"] = {
            "fingerprint": str(prompt_runtime.get("fingerprint") or ""),
            "execution_identity": copy.deepcopy(
                prompt_runtime.get("execution_identity") or {}
            ),
        }
    payload = {
        "schema": "omni.provider-execution-authority.v1",
        "execution_identity": execution_identity,
        "contract": {
            "input_schema": copy.deepcopy(
                getattr(entry, "input_schema", None) or {}
            ),
            "output_schema": copy.deepcopy(
                getattr(entry, "output_schema", None) or {}
            ),
            "template_signatures": copy.deepcopy(
                getattr(entry, "template_signatures", None) or {}
            ),
        },
    }
    return {
        "fingerprint": _canonical_payload_hash(payload),
        **payload,
    }


def prompt_skill_entries(
    registry: Any,
    entry: Any,
) -> tuple[Any, ...]:
    """Return engine-backed registry tools visible to one prompt-only skill."""
    allowed = {
        str(item)
        for item in (getattr(entry, "allowed_tools", ()) or ())
        if str(item)
    }
    candidates: list[Any] = []
    for candidate in registry.list_sync_tools():
        kind = getattr(candidate, "kind", "")
        kind_value = str(getattr(kind, "value", kind) or "")
        name = str(getattr(candidate, "name", "") or "")
        if (
            kind_value in {"python_engine", "cli_exec"}
            and name != str(getattr(entry, "name", "") or "")
            and (not allowed or name in allowed)
        ):
            candidates.append(candidate)
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                str(getattr(candidate, "name", "") or ""),
                str(getattr(candidate, "source", "") or ""),
                str(getattr(candidate, "path", "") or ""),
            ),
        )
    )


def runtime_provider_authority_snapshot(
    registry: Any,
    entry: Any | None,
    *,
    file_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Seal one provider plus any registry providers it may invoke inline."""
    base = provider_authority_snapshot(entry, file_cache=file_cache)
    if not base or entry is None:
        return base
    kind = getattr(entry, "kind", "")
    if str(getattr(kind, "value", kind) or "") != "prompt_only":
        return base
    delegated: list[dict[str, Any]] = []
    for candidate in prompt_skill_entries(registry, entry):
        snapshot = provider_authority_snapshot(
            candidate,
            file_cache=file_cache,
        )
        snapshot.update(
            provider_name=str(getattr(candidate, "name", "") or ""),
            provider_source=str(getattr(candidate, "source", "") or ""),
        )
        delegated.append(snapshot)
    payload = {
        key: copy.deepcopy(value)
        for key, value in base.items()
        if key != "fingerprint"
    }
    payload["base_provider_fingerprint"] = str(
        base.get("fingerprint") or ""
    )
    payload["delegated_provider_authorities"] = delegated
    return {"fingerprint": _canonical_payload_hash(payload), **payload}


def provider_authorities_for_plan(
    registry: Any,
    plan: IntentPlan,
) -> tuple[dict[str, Any], ...]:
    """Bind each executable plan consumer to its concrete provider snapshot."""
    authorities: list[dict[str, Any]] = []
    native_file_cache: dict[str, dict[str, Any]] = {}
    native_snapshots: dict[str, dict[str, Any]] = {}
    resolver = getattr(registry, "resolve_ref", None)
    getter = getattr(registry, "get", None)

    def _resolve(name: str, source: str) -> Any:
        if callable(resolver):
            return resolver(name, source)
        return getter(name) if callable(getter) else None

    def _native_snapshot(kind: str) -> dict[str, Any]:
        if kind not in native_snapshots:
            native_snapshots[kind] = native_provider_authority_snapshot(
                kind,
                file_cache=native_file_cache,
            )
        return copy.deepcopy(native_snapshots[kind])

    for index, selection in enumerate(plan.selected_skills):
        name = str(selection.skill or "")
        source = str(getattr(selection, "skill_source", "") or "")
        entry = _resolve(name, source)
        snapshot = runtime_provider_authority_snapshot(
            registry,
            entry,
            file_cache=native_file_cache,
        )
        if snapshot:
            authorities.append(
                {
                    "consumer_kind": "selected_skill",
                    "consumer_id": str(index),
                    "provider_name": name,
                    "provider_source": str(getattr(entry, "source", "") or source),
                    **snapshot,
                }
            )
    for step in plan.workflow_steps:
        name = str(step.get("skill_name") or step.get("skill") or "")
        native_kind = workflow_native_authority_kind(step)
        if native_kind:
            snapshot = (
                child_agent_provider_authority_snapshot(
                    registry,
                    step,
                    file_cache=native_file_cache,
                )
                if native_kind == "agent_delegate"
                else _native_snapshot(native_kind)
            )
            authorities.append(
                {
                    "consumer_kind": "workflow_step",
                    "consumer_id": str(step.get("id") or ""),
                    "provider_name": native_kind,
                    "provider_source": "omni_runtime",
                    **snapshot,
                }
            )
            continue
        if not name:
            continue
        source = step_skill_source(step)
        entry = _resolve(name, source)
        snapshot = runtime_provider_authority_snapshot(
            registry,
            entry,
            file_cache=native_file_cache,
        )
        if snapshot:
            authorities.append(
                {
                    "consumer_kind": "workflow_step",
                    "consumer_id": str(step.get("id") or ""),
                    "provider_name": name,
                    "provider_source": str(getattr(entry, "source", "") or source),
                    **snapshot,
                }
            )
    if str(getattr(plan.intent_type, "value", plan.intent_type)) == "react_fallback":
        snapshot = _native_snapshot("react_delegate")
        authorities.append(
            {
                "consumer_kind": "react_turn",
                "consumer_id": "react",
                "provider_name": "react_delegate",
                "provider_source": "omni_runtime",
                **snapshot,
            }
        )
    return tuple(authorities)


def workflow_native_authority_kind(step: dict[str, Any]) -> str:
    """Return the host-native provider identity for a workflow step."""
    provider = str(
        step.get("provider_type") or step.get("provider") or ""
    ).strip().lower()
    capability = str(step.get("capability") or "").strip().lower()
    skill_name = str(
        step.get("skill_name") or step.get("skill") or ""
    ).strip().lower()
    if (
        provider == "native_executor"
        or capability in {
            "synthesis.final",
            "draft.section",
            "draft.manuscript",
        }
    ):
        return "native_synthesis"
    if (
        provider in {"child_task", "subagent", "agent"}
        or skill_name == "workflow"
    ):
        return "agent_delegate"
    return ""


def native_provider_authority_snapshot(
    kind: str,
    *,
    file_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Seal native workflow/ReAct providers to versioned code and I/O contracts."""
    normalized = str(kind or "").strip().lower()
    omni_root = Path(__file__).resolve().parents[1]
    if normalized == "native_synthesis":
        from omni.runtime.final_synthesis import (
            NATIVE_SYNTHESIS_INPUT_SCHEMA,
            NATIVE_SYNTHESIS_OUTPUT_SCHEMA,
        )

        input_schema = copy.deepcopy(NATIVE_SYNTHESIS_INPUT_SCHEMA)
        output_schema = copy.deepcopy(NATIVE_SYNTHESIS_OUTPUT_SCHEMA)
        files = [
            omni_root / "runtime" / "workflow_runtime.py",
            omni_root / "runtime" / "final_synthesis.py",
            omni_root / "data" / "synthesis_templates" / "draft.section.md",
        ]
    elif normalized == "agent_delegate":
        input_schema = {
            "type": "object",
            "required": ["goal"],
            "properties": {
                "goal": {"type": "string"},
                "role": {"type": "string"},
                "context": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "string"}},
                "model": {"type": "string"},
                "compute_profile": {"type": "string"},
                "isolation": {"type": "string"},
            },
        }
        output_schema = {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": {"type": "string"},
                "content": {"type": "string"},
                "error": {"type": "string"},
            },
        }
        files = [
            omni_root / "agent" / "subagents.py",
            omni_root / "core" / "react_agent.py",
            omni_root / "runtime" / "tool_gateway.py",
            omni_root / "runtime" / "workflow_runtime.py",
            omni_root / "runtime" / "workflow_step_outcomes.py",
        ]
    elif normalized == "react_delegate":
        input_schema = {
            "type": "object",
            "required": ["user_message"],
            "properties": {
                "user_message": {"type": "string"},
                "tool_policy": {"type": "object"},
            },
        }
        output_schema = {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "terminated_reason": {"type": "string"},
            },
        }
        files = [
            omni_root / "agent" / "orchestrator.py",
            omni_root / "core" / "react_agent.py",
            omni_root / "agent" / "tool_surface.py",
            omni_root / "runtime" / "tool_gateway.py",
        ]
    else:
        return {}
    from omni import __version__

    payload = {
        "schema": "omni.native-provider-authority.v1",
        "execution_identity": {
            "name": normalized,
            "source": "omni_runtime",
            "version": __version__,
            "implementation": {
                "runtime_tree": _executable_tree_identity(
                    omni_root,
                    cache=file_cache,
                ),
                # Disk identity alone is insufficient in a long-lived daemon:
                # Python may still be executing modules loaded before an
                # in-place update. Seal the actual in-process callable code too.
                "loaded_entrypoints": _stable_loaded_files_code_identity(
                    normalized,
                    files,
                ),
                "entrypoints": _file_set_identity(
                    files,
                    cache=file_cache,
                ),
            },
        },
        "contract": {
            "input_schema": input_schema,
            "output_schema": output_schema,
        },
    }
    return {
        "fingerprint": _canonical_payload_hash(payload),
        **payload,
    }


def specialist_skill_entries(
    registry: Any,
    step: dict[str, Any],
) -> tuple[Any, ...]:
    """Return the registry skills a child-agent step is allowed to expose."""
    input_data = step.get("input")
    if not isinstance(input_data, dict):
        input_data = {}
    isolation = str(input_data.get("isolation") or "").strip().lower()
    if isolation == "container":
        return ()
    allowed = {
        str(item)
        for item in (input_data.get("tools") or [])
        if str(item)
    }
    blocked = {"write_file", "edit_file", "bash", "run_compute"}
    entries: list[Any] = []
    for entry in registry.list_sync_tools():
        kind = getattr(entry, "kind", "")
        kind_value = str(getattr(kind, "value", kind) or "")
        if kind_value not in {"python_engine", "cli_exec"}:
            continue
        name = str(getattr(entry, "name", "") or "")
        if allowed and name not in allowed:
            continue
        if not allowed and name in blocked:
            continue
        entries.append(entry)
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                str(getattr(entry, "name", "") or ""),
                str(getattr(entry, "source", "") or ""),
                str(getattr(entry, "path", "") or ""),
            ),
        )
    )


def child_agent_provider_authority_snapshot(
    registry: Any,
    step: dict[str, Any],
    *,
    file_cache: dict[str, dict[str, Any]] | None = None,
    native_snapshot_factory: Any = native_provider_authority_snapshot,
) -> dict[str, Any]:
    """Seal the child runtime and every dynamic registry provider it may call."""
    base = (
        native_snapshot_factory(
            "agent_delegate",
            file_cache=file_cache,
        )
        if file_cache is not None
        else native_snapshot_factory("agent_delegate")
    )
    if not base:
        return {}
    delegated: list[dict[str, Any]] = []
    for entry in specialist_skill_entries(registry, step):
        snapshot = provider_authority_snapshot(
            entry,
            file_cache=file_cache,
        )
        snapshot.update(
            provider_name=str(getattr(entry, "name", "") or ""),
            provider_source=str(getattr(entry, "source", "") or ""),
        )
        delegated.append(snapshot)
    payload = {
        key: copy.deepcopy(value)
        for key, value in base.items()
        if key != "fingerprint"
    }
    payload["native_provider_fingerprint"] = str(
        base.get("fingerprint") or ""
    )
    payload["delegated_provider_authorities"] = delegated
    return {"fingerprint": _canonical_payload_hash(payload), **payload}


def provider_authority_for_consumer(
    authority: ExecutionAuthority | dict[str, Any] | None,
    *,
    consumer_kind: str,
    consumer_id: str,
) -> dict[str, Any]:
    """Return the exact persisted provider authority for one plan consumer."""
    if isinstance(authority, ExecutionAuthority):
        providers = authority.provider_authorities
    elif isinstance(authority, dict):
        providers = authority.get("provider_authorities") or []
    else:
        providers = []
    for item in providers:
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("consumer_kind") or "") == consumer_kind
            and str(item.get("consumer_id") or "") == consumer_id
        ):
            return copy.deepcopy(item)
    return {}


def provider_authority_error(
    entry: Any | None,
    expected: dict[str, Any] | None,
    *,
    registry: Any = None,
    authority_envelope: dict[str, Any] | None = None,
    consumer_kind: str = "",
    consumer_id: str = "",
) -> str:
    """Explain why a queued provider may not execute under its sealed authority."""
    active, audit_error = _validated_active_provider_authority(
        expected,
        authority_envelope=authority_envelope,
        consumer_kind=consumer_kind,
        consumer_id=consumer_id,
    )
    if audit_error:
        return audit_error
    expected = active
    if (
        entry is None
        and (
            not isinstance(expected, dict)
            or not expected.get("fingerprint")
        )
    ):
        return ""
    if not isinstance(expected, dict) or not expected.get("fingerprint"):
        return (
            "provider execution authority is missing; re-submit the task with "
            "the current Omni version"
        )
    if entry is None:
        return "provider execution authority changed: provider is no longer installed"
    live = (
        runtime_provider_authority_snapshot(registry, entry)
        if registry is not None
        and isinstance(expected, dict)
        and isinstance(
            expected.get("delegated_provider_authorities"),
            list,
        )
        else provider_authority_snapshot(entry)
    )
    return provider_snapshot_authority_error(live, expected)


def provider_snapshot_authority_error(
    live: dict[str, Any],
    expected: dict[str, Any] | None,
    *,
    authority_envelope: dict[str, Any] | None = None,
    consumer_kind: str = "",
    consumer_id: str = "",
) -> str:
    """Compare an already-materialized native or skill provider snapshot."""
    active, audit_error = _validated_active_provider_authority(
        expected,
        authority_envelope=authority_envelope,
        consumer_kind=consumer_kind,
        consumer_id=consumer_id,
    )
    if audit_error:
        return audit_error
    expected = active
    if not isinstance(expected, dict) or not expected.get("fingerprint"):
        return (
            "provider execution authority is missing; re-submit the task with "
            "the current Omni version"
        )
    if not provider_snapshot_is_valid(expected):
        return (
            "persisted provider execution authority is invalid; "
            "re-plan or re-submit before running"
        )
    unsafe = _provider_snapshot_runtime_error(expected)
    if unsafe:
        return unsafe
    if str(live.get("fingerprint") or "") != str(
        expected.get("fingerprint") or ""
    ):
        return (
            "provider execution authority changed after enqueue; "
            "re-plan or re-submit before running"
        )
    if not provider_snapshot_is_valid(live):
        return (
            "live provider execution authority is invalid; "
            "restart Omni, then re-plan or re-submit"
        )
    unsafe = _provider_snapshot_runtime_error(live)
    if unsafe:
        return unsafe
    return ""


def provider_snapshot_is_valid(snapshot: dict[str, Any]) -> bool:
    """Verify one provider snapshot and every delegated provider recursively."""
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema")
        not in {
            "omni.provider-execution-authority.v1",
            "omni.native-provider-authority.v1",
        }
        or not snapshot.get("fingerprint")
    ):
        return False
    payload = _provider_snapshot_payload(snapshot)
    if str(snapshot.get("fingerprint") or "") != _canonical_payload_hash(payload):
        return False
    delegated = payload.get("delegated_provider_authorities")
    if delegated is not None and (
        not isinstance(delegated, list)
        or not all(
            isinstance(item, dict) and provider_snapshot_is_valid(item)
            for item in delegated
        )
    ):
        return False
    base_fingerprint = str(payload.get("base_provider_fingerprint") or "")
    native_fingerprint = str(payload.get("native_provider_fingerprint") or "")
    if base_fingerprint or native_fingerprint:
        base_payload = {
            key: copy.deepcopy(value)
            for key, value in payload.items()
            if key
            not in {
                "base_provider_fingerprint",
                "native_provider_fingerprint",
                "delegated_provider_authorities",
            }
        }
        if (base_fingerprint or native_fingerprint) != _canonical_payload_hash(
            base_payload
        ):
            return False
    return True


def _provider_snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the exact payload covered by a provider's own fingerprint."""
    ignored = {
        "fingerprint",
        *_PROVIDER_BINDING_FIELDS,
        *_PROVIDER_AUDIT_FIELDS,
    }
    return {
        key: copy.deepcopy(value)
        for key, value in snapshot.items()
        if key not in ignored
    }


def _provider_snapshot_runtime_error(snapshot: dict[str, Any]) -> str:
    """Reject provider closures that cannot be sealed deterministically."""
    identity = snapshot.get("execution_identity")
    if isinstance(identity, dict):
        closures: list[dict[str, Any]] = []
        for key in ("engine", "exec_spec"):
            binding = identity.get(key)
            if isinstance(binding, dict):
                closure = binding.get("dependency_closure")
                if isinstance(closure, dict) and closure:
                    closures.append(closure)
        for key in ("implementation",):
            binding = identity.get(key)
            if isinstance(binding, dict):
                closure = binding.get("runtime_tree")
                if isinstance(closure, dict) and closure:
                    closures.append(closure)
        for closure in closures:
            state = str(closure.get("state") or "")
            if state == "unsafe_symbolic_link":
                path = str(closure.get("symlink") or "")
                return (
                    "provider execution authority cannot seal a symbolic link"
                    + (f" ({path})" if path else "")
                    + "; replace it with regular files, then re-submit"
                )
            if state not in {"", "loaded_callable", "present", "unresolved"}:
                return (
                    "provider execution authority dependency closure is "
                    f"{state}; repair the provider, then re-submit"
                )
    delegated = snapshot.get("delegated_provider_authorities")
    if isinstance(delegated, list):
        for item in delegated:
            if isinstance(item, dict):
                error = _provider_snapshot_runtime_error(item)
                if error:
                    return error
    return ""


def create_provider_authority_renewal(
    *,
    previous_fingerprint: str,
    action: str,
    renewed_at: str,
    provider_authorities: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create one content-addressed link in an explicit re-authorization chain."""
    payload = {
        "schema": "omni.provider-authority-renewal.v1",
        "previous_fingerprint": str(previous_fingerprint or ""),
        "action": str(action or ""),
        "renewed_at": str(renewed_at or ""),
        "provider_authorities": copy.deepcopy(provider_authorities),
    }
    return {
        "fingerprint": _canonical_payload_hash(payload),
        **payload,
    }


def provider_authority_renewal_is_valid(payload: dict[str, Any]) -> bool:
    """Verify a persisted re-authorization chain link without trusting its hash."""
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "omni.provider-authority-renewal.v1"
        or not payload.get("fingerprint")
    ):
        return False
    body = {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key != "fingerprint"
    }
    if str(payload["fingerprint"]) != _canonical_payload_hash(body):
        return False
    providers = payload.get("provider_authorities")
    return isinstance(providers, list) and all(
        isinstance(item, dict) and provider_snapshot_is_valid(item)
        for item in providers
    )


def queued_workflow_authority(
    provider_authorities: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a content-addressed authority envelope for ad-hoc workflows."""
    payload = {
        "schema": "omni.queued-workflow-authority.v1",
        "provider_authorities": copy.deepcopy(provider_authorities),
    }
    return {"fingerprint": _canonical_payload_hash(payload), **payload}


def provider_authority_renewal_chain_is_valid(
    envelope: dict[str, Any],
) -> bool:
    """Validate every renewal hash and its link to the preceding authority."""
    if not isinstance(envelope, dict) or not _authority_root_is_valid(envelope):
        return False
    previous = str(envelope.get("fingerprint") or "")
    for renewal in envelope.get("provider_authority_renewals") or []:
        if (
            not isinstance(renewal, dict)
            or not provider_authority_renewal_is_valid(renewal)
            or str(renewal.get("previous_fingerprint") or "") != previous
        ):
            return False
        previous = str(renewal.get("fingerprint") or "")
    return True


def _validated_active_provider_authority(
    expected: dict[str, Any] | None,
    *,
    authority_envelope: dict[str, Any] | None,
    consumer_kind: str,
    consumer_id: str,
) -> tuple[dict[str, Any], str]:
    """Bind one active provider row to its immutable root and renewal head."""
    active = _provider_authority_without_audit_fields(expected)
    envelope = authority_envelope
    if envelope is None and isinstance(expected, dict) and any(
        key in expected for key in _PROVIDER_AUDIT_FIELDS
    ):
        root = expected.get("provider_authority_root")
        renewals = expected.get("provider_authority_renewals")
        if not isinstance(root, dict) or not isinstance(renewals, list):
            return active, (
                "persisted provider authority renewal chain is incomplete; "
                "re-plan or re-submit before running"
            )
        envelope = {
            **copy.deepcopy(root),
            "provider_authority_renewals": copy.deepcopy(renewals),
        }
    if envelope is None:
        return active, ""
    if not provider_authority_renewal_chain_is_valid(envelope):
        return active, (
            "persisted provider authority renewal chain is invalid; "
            "re-plan or re-submit before running"
        )

    kind = str(consumer_kind or active.get("consumer_kind") or "")
    consumer = str(consumer_id or active.get("consumer_id") or "")
    authorized = _provider_authority_at_renewal_head(
        envelope,
        consumer_kind=kind,
        consumer_id=consumer,
        provider_name=str(active.get("provider_name") or ""),
        provider_source=str(active.get("provider_source") or ""),
    )
    if not authorized:
        return active, (
            "provider authority renewal head does not authorize this consumer; "
            "re-plan or re-submit before running"
        )
    if active != authorized:
        return active, (
            "active provider authority does not match the latest renewal; "
            "re-plan or re-submit before running"
        )
    return active, ""


def _provider_authority_at_renewal_head(
    envelope: dict[str, Any],
    *,
    consumer_kind: str,
    consumer_id: str,
    provider_name: str,
    provider_source: str,
) -> dict[str, Any]:
    """Resolve the latest explicitly authorized snapshot for one consumer."""
    if str(envelope.get("schema") or "") in {
        "omni.provider-execution-authority.v1",
        "omni.native-provider-authority.v1",
    }:
        providers = [_provider_authority_without_audit_fields(envelope)]
    else:
        providers = [
            copy.deepcopy(item)
            for item in (envelope.get("provider_authorities") or [])
            if isinstance(item, dict)
        ]
    active = _unique_matching_provider(
        providers,
        consumer_kind=consumer_kind,
        consumer_id=consumer_id,
        provider_name=provider_name,
        provider_source=provider_source,
    )
    for renewal in envelope.get("provider_authority_renewals") or []:
        matches = [
            copy.deepcopy(item)
            for item in (renewal.get("provider_authorities") or [])
            if isinstance(item, dict)
            and _provider_matches_consumer(
                item,
                consumer_kind=consumer_kind,
                consumer_id=consumer_id,
                provider_name=provider_name,
                provider_source=provider_source,
            )
        ]
        if len(matches) > 1:
            return {}
        if matches:
            active = matches[0]
    return active


def _unique_matching_provider(
    providers: list[dict[str, Any]],
    *,
    consumer_kind: str,
    consumer_id: str,
    provider_name: str,
    provider_source: str,
) -> dict[str, Any]:
    matches = [
        item
        for item in providers
        if _provider_matches_consumer(
            item,
            consumer_kind=consumer_kind,
            consumer_id=consumer_id,
            provider_name=provider_name,
            provider_source=provider_source,
        )
    ]
    return matches[0] if len(matches) == 1 else {}


def _provider_matches_consumer(
    snapshot: dict[str, Any],
    *,
    consumer_kind: str,
    consumer_id: str,
    provider_name: str,
    provider_source: str,
) -> bool:
    if consumer_kind or consumer_id:
        return (
            str(snapshot.get("consumer_kind") or "") == consumer_kind
            and str(snapshot.get("consumer_id") or "") == consumer_id
        )
    if provider_name:
        return (
            str(snapshot.get("provider_name") or "") == provider_name
            and (
                not provider_source
                or str(snapshot.get("provider_source") or "") == provider_source
            )
        )
    return True


def _provider_authority_without_audit_fields(
    authority: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in dict(authority or {}).items()
        if key not in _PROVIDER_AUDIT_FIELDS
    }


def _authority_root_is_valid(envelope: dict[str, Any]) -> bool:
    """Validate a queued workflow, accepted plan, or standalone provider root."""
    schema = str(envelope.get("schema") or "")
    providers = envelope.get("provider_authorities")
    if schema == "omni.queued-workflow-authority.v1":
        if not isinstance(providers, list):
            return False
        payload = {
            "schema": schema,
            "provider_authorities": copy.deepcopy(providers),
        }
        return (
            str(envelope.get("fingerprint") or "")
            == _canonical_payload_hash(payload)
            and all(
                isinstance(item, dict) and provider_snapshot_is_valid(item)
                for item in providers
            )
        )
    if schema in {
        "omni.provider-execution-authority.v1",
        "omni.native-provider-authority.v1",
    }:
        return provider_snapshot_is_valid(envelope)
    if all(
        key in envelope
        for key in (
            "fingerprint",
            "plan_hash",
            "catalog_hash",
            "contract_hash",
            "approval_tools",
            "provider_authorities",
        )
    ):
        if not isinstance(providers, list):
            return False
        payload = {
            "plan_hash": str(envelope.get("plan_hash") or ""),
            "catalog_hash": str(envelope.get("catalog_hash") or ""),
            "contract_hash": str(envelope.get("contract_hash") or ""),
            "approval_tools": sorted(
                {
                    str(item)
                    for item in (envelope.get("approval_tools") or [])
                    if str(item)
                }
            ),
            "provider_authorities": copy.deepcopy(providers),
        }
        return (
            str(envelope.get("fingerprint") or "")
            == _canonical_payload_hash(payload)
            and all(
                isinstance(item, dict) and provider_snapshot_is_valid(item)
                for item in providers
            )
        )
    return False


def execution_authority_hash(
    plan: IntentPlan,
    *,
    catalog_hash: str,
    contract_hash: str,
    approval_tools: list[str] | tuple[str, ...] | set[str] = (),
    provider_authorities: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> str:
    """Fingerprint the complete authority a human is asked to approve."""
    return _canonical_payload_hash(
        {
            "plan_hash": canonical_plan_hash(plan),
            "catalog_hash": catalog_hash,
            "contract_hash": contract_hash,
            "approval_tools": sorted({str(item) for item in approval_tools if str(item)}),
            "provider_authorities": list(provider_authorities),
        }
    )


@dataclass(frozen=True, slots=True)
class ExecutionAuthority:
    """The exact plan, contract snapshot, and grants covered by approval."""

    fingerprint: str
    plan_hash: str
    catalog_hash: str
    contract_hash: str
    approval_tools: tuple[str, ...] = ()
    provider_authorities: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "plan_hash": self.plan_hash,
            "catalog_hash": self.catalog_hash,
            "contract_hash": self.contract_hash,
            "approval_tools": list(self.approval_tools),
            "provider_authorities": copy.deepcopy(
                list(self.provider_authorities)
            ),
        }


def create_execution_authority(
    plan: IntentPlan,
    *,
    registry: Any,
    approval_tools: list[str] | tuple[str, ...] | set[str] = (),
) -> ExecutionAuthority:
    """Snapshot all authority-bearing inputs for one execution decision."""
    catalog_hash, contract_hash = registry_snapshot_hashes(registry, plan)
    provider_authorities = provider_authorities_for_plan(registry, plan)
    return execution_authority_from_snapshot(
        plan,
        catalog_hash=catalog_hash,
        contract_hash=contract_hash,
        approval_tools=approval_tools,
        provider_authorities=provider_authorities,
    )


def execution_authority_from_snapshot(
    plan: IntentPlan,
    *,
    catalog_hash: str,
    contract_hash: str,
    approval_tools: list[str] | tuple[str, ...] | set[str] = (),
    provider_authorities: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> ExecutionAuthority:
    """Build authority from the exact catalog/contract snapshot already sealed."""
    tools = tuple(sorted({str(item) for item in approval_tools if str(item)}))
    providers = tuple(copy.deepcopy(list(provider_authorities)))
    return ExecutionAuthority(
        fingerprint=execution_authority_hash(
            plan,
            catalog_hash=catalog_hash,
            contract_hash=contract_hash,
            approval_tools=tools,
            provider_authorities=providers,
        ),
        plan_hash=canonical_plan_hash(plan),
        catalog_hash=catalog_hash,
        contract_hash=contract_hash,
        approval_tools=tools,
        provider_authorities=providers,
    )


def _provider_execution_identity(
    entry: Any,
    *,
    file_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    engine = getattr(entry, "engine", None)
    exec_spec = getattr(entry, "exec_spec", None)
    path = getattr(entry, "path", None)
    root = (
        path
        if isinstance(path, Path) and path.is_dir()
        else path.parent
        if isinstance(path, Path)
        else None
    )
    skill_file = (
        path / "SKILL.md"
        if isinstance(path, Path) and path.is_dir()
        else path
        if isinstance(path, Path)
        else None
    )
    engine_module = str(getattr(engine, "module", "") or "")
    engine_file: Path | None = None
    engine_tree: Path | None = root if engine_module else None
    if root is not None and engine_module:
        candidate = root / f"{engine_module}.py"
        if candidate.is_file():
            engine_file = candidate
    if engine_file is None and engine_module:
        spec, package_root = _find_module_spec_without_import(engine_module)
        origin = str(getattr(spec, "origin", "") or "")
        if origin and origin not in {"built-in", "frozen"}:
            engine_file = Path(origin)
        engine_tree = package_root
    command = str(getattr(exec_spec, "command", "") or "")
    command_file: Path | None = None
    if command:
        direct = Path(command).expanduser()
        root_candidate = (
            root / direct
            if root is not None and not direct.is_absolute()
            else direct
        )
        resolved = (
            str(direct)
            if direct.is_file()
            else str(root_candidate)
            if root_candidate.is_file()
            else shutil.which(command)
        )
        if resolved:
            command_file = Path(resolved)
    exec_args = list(getattr(exec_spec, "args", ()) or ())
    exec_cwd = str(getattr(exec_spec, "cwd", "") or "")
    return {
        "name": str(getattr(entry, "name", "") or ""),
        "source": str(getattr(entry, "source", "") or ""),
        "version": str(getattr(entry, "version", "") or ""),
        "path": str(path.resolve(strict=False)) if isinstance(path, Path) else "",
        "sha256": str(getattr(entry, "sha256", "") or ""),
        "skill_file": _file_identity(skill_file, cache=file_cache),
        "kind": str(getattr(entry, "kind", "") or ""),
        "delivery_mode": str(getattr(entry, "delivery_mode", "") or ""),
        "engine": (
            {
                "module": engine_module,
                "class_name": str(getattr(engine, "class_name", "") or ""),
                "method": str(getattr(engine, "method", "") or ""),
                "artifact": _file_identity(engine_file, cache=file_cache),
                "dependency_closure": (
                    _executable_tree_identity(
                        engine_tree,
                        cache=file_cache,
                    )
                    if engine_tree is not None
                    else _loaded_engine_identity(
                        engine_module,
                        str(getattr(engine, "class_name", "") or ""),
                        str(getattr(engine, "method", "") or ""),
                    )
                ),
            }
            if engine is not None
            else {}
        ),
        "exec_spec": (
            {
                "command": command,
                "args": exec_args,
                "stdout_format": str(
                    getattr(exec_spec, "stdout_format", "") or ""
                ),
                "timeout_seconds": int(
                    getattr(exec_spec, "timeout_seconds", 0) or 0
                ),
                "cwd": exec_cwd,
                "env_allowlist": list(
                    getattr(exec_spec, "env_allowlist", ()) or ()
                ),
                "artifact": _file_identity(command_file, cache=file_cache),
                "argument_artifacts": _local_exec_argument_identities(
                    exec_args,
                    root=root,
                    cwd=exec_cwd,
                    cache=file_cache,
                ),
                "dependency_closure": (
                    _executable_tree_identity(root, cache=file_cache)
                    if root is not None
                    else {}
                ),
            }
            if exec_spec is not None
            else {}
        ),
        "execution": copy.deepcopy(getattr(entry, "execution", None) or {}),
        "workflow": copy.deepcopy(getattr(entry, "workflow", None) or {}),
        "replay_safe": bool(getattr(entry, "replay_safe", False)),
        "trusted": bool(getattr(entry, "trusted", False)),
        "origin": str(getattr(entry, "origin", "") or ""),
        "status": str(getattr(entry, "status", "") or ""),
        "allow_implicit": bool(getattr(entry, "allow_implicit", False)),
        "contract_level": str(getattr(entry, "contract_level", "") or ""),
        "role": str(getattr(entry, "role", "") or ""),
        "capabilities": list(getattr(entry, "capabilities", ()) or ()),
        "deliverables": list(getattr(entry, "deliverables", ()) or ()),
        "allowed_tools": list(getattr(entry, "allowed_tools", ()) or ()),
        "dependencies": list(getattr(entry, "dependencies", ()) or ()),
        "requires_bins": list(getattr(entry, "requires_bins", ()) or ()),
        "requires_env": list(getattr(entry, "requires_env", ()) or ()),
        "requires_services": list(
            getattr(entry, "requires_services", ()) or ()
        ),
        "artifact_revision": copy.deepcopy(
            getattr(entry, "artifact_revision", None) or {}
        ),
    }


def _file_identity(
    path: Path | None,
    *,
    cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if path is None:
        return {}
    resolved = str(path.expanduser().resolve(strict=False))
    if cache is not None and resolved in cache:
        return copy.deepcopy(cache[resolved])
    identity: dict[str, Any] = {"path": resolved, "sha256": "", "state": "missing"}
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        identity.update(sha256=digest.hexdigest(), state="present")
    except OSError as exc:
        identity["state"] = f"unreadable:{type(exc).__name__}"
    if cache is not None:
        cache[resolved] = copy.deepcopy(identity)
    return identity


def _file_set_identity(
    files: list[Path],
    *,
    cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Hash a fixed set of native runtime files without importing their modules."""
    digest = hashlib.sha256()
    records: list[dict[str, Any]] = []
    for path in sorted(files, key=lambda item: str(item.resolve(strict=False))):
        identity = _file_identity(path, cache=cache)
        records.append(identity)
        digest.update(str(identity.get("path") or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(identity.get("state") or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(identity.get("sha256") or "").encode("ascii"))
        digest.update(b"\0")
    return {
        "state": (
            "present"
            if records
            and all(item.get("state") == "present" for item in records)
            else "incomplete"
        ),
        "sha256": digest.hexdigest(),
        "file_count": len(records),
        "files": records,
    }


def _local_exec_argument_identities(
    args: list[str],
    *,
    root: Path | None,
    cwd: str,
    cache: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Seal static local files named by a CLI binding's argv."""
    roots: list[Path] = []
    if root is not None:
        roots.append(root)
    if cwd:
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_absolute() and root is not None:
            cwd_path = root / cwd_path
        roots.append(cwd_path)
    artifacts: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for position, raw_value in enumerate(args):
        value = str(raw_value or "")
        if (
            not value
            or value.startswith("-")
            or "\x00" in value
            or "\n" in value
            or "{" in value
            or "}" in value
            or len(value) > 4096
        ):
            continue
        candidate = Path(value).expanduser()
        candidates = (
            [candidate]
            if candidate.is_absolute()
            else [base / candidate for base in roots]
        )
        for path in candidates:
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            resolved = str(path.resolve(strict=False))
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            artifacts.append(
                {
                    "position": position,
                    "argument": value,
                    "artifact": _file_identity(path, cache=cache),
                }
            )
    return artifacts


def _find_module_spec_without_import(
    module_name: str,
) -> tuple[Any | None, Path | None]:
    """Resolve a dotted module through filesystem finders without importing it."""
    search_path: Any = None
    current = ""
    package_root: Path | None = None
    spec: Any | None = None
    for part in (item for item in module_name.split(".") if item):
        current = f"{current}.{part}" if current else part
        try:
            spec = importlib.machinery.PathFinder.find_spec(
                current,
                search_path,
            )
        except (ImportError, OSError, ValueError):
            return None, package_root
        if spec is None:
            return None, package_root
        locations = list(getattr(spec, "submodule_search_locations", ()) or ())
        if package_root is None and locations:
            package_root = Path(str(locations[0]))
        search_path = locations or None
    return spec, package_root


def _loaded_engine_identity(
    module_name: str,
    class_name: str,
    method_name: str,
) -> dict[str, Any]:
    """Seal pathless test/plugin engines from already-loaded callable bytecode."""
    module = sys.modules.get(module_name)
    target = getattr(module, class_name, None) if module is not None else None
    callable_value = getattr(target, method_name or "execute", None)
    code = getattr(callable_value, "__code__", None)
    if code is None:
        return {
            "state": "unresolved",
            "module": module_name,
            "policy": "dispatch_fails_if_identity_changes",
        }
    return {
        "state": "loaded_callable",
        "module": module_name,
        "sha256": hashlib.sha256(marshal.dumps(code)).hexdigest(),
    }


def _loaded_files_code_identity(files: list[Path]) -> dict[str, Any]:
    """Hash callable bytecode actually loaded for fixed native entrypoints."""
    digest = hashlib.sha256()
    modules = 0
    callables = 0
    expected_paths = {
        str(path.expanduser().resolve(strict=False))
        for path in files
        if path.suffix.lower() in {".py", ".pyw"}
    }
    for module_name, module in sorted(sys.modules.items()):
        if module is None:
            continue
        module_path = str(
            Path(str(getattr(module, "__file__", "") or "")).resolve(
                strict=False
            )
        )
        if module_path not in expected_paths:
            continue
        modules += 1
        namespace = vars(module)
        for symbol, value in sorted(namespace.items()):
            code = getattr(value, "__code__", None)
            if code is not None:
                digest.update(f"{module_name}:{symbol}".encode())
                digest.update(b"\0")
                digest.update(marshal.dumps(code))
                digest.update(b"\0")
                callables += 1
                continue
            if not isinstance(value, type) or value.__module__ != module_name:
                continue
            for member_name, member in sorted(vars(value).items()):
                if isinstance(member, (staticmethod, classmethod)):
                    member = member.__func__
                member_code = getattr(member, "__code__", None)
                if member_code is None:
                    continue
                digest.update(
                    f"{module_name}:{symbol}.{member_name}".encode()
                )
                digest.update(b"\0")
                digest.update(marshal.dumps(member_code))
                digest.update(b"\0")
                callables += 1
    return {
        "state": "loaded",
        "sha256": digest.hexdigest(),
        "module_count": modules,
        "callable_count": callables,
    }


def _stable_loaded_files_code_identity(
    key: str,
    files: list[Path],
) -> dict[str, Any]:
    """Capture process-loaded entrypoint code once, avoiding runtime quickening noise."""
    if key not in _LOADED_ENTRYPOINT_IDENTITIES:
        _LOADED_ENTRYPOINT_IDENTITIES[key] = _loaded_files_code_identity(
            files
        )
    return copy.deepcopy(_LOADED_ENTRYPOINT_IDENTITIES[key])


def _executable_tree_identity(
    root: Path,
    *,
    cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Hash every runtime-visible file in a provider package tree."""
    resolved = str(root.expanduser().resolve(strict=False))
    cache_key = f"tree:{resolved}"
    if cache is not None and cache_key in cache:
        return copy.deepcopy(cache[cache_key])
    excluded_dirs = {
        "__pycache__",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
    }
    files: list[Path] = []
    if root.is_symlink():
        identity = {
            "root": resolved,
            "state": "unsafe_symbolic_link",
            "sha256": "",
            "file_count": 0,
            "symlink": ".",
        }
        if cache is not None:
            cache[cache_key] = copy.deepcopy(identity)
        return identity
    try:
        for candidate in root.rglob("*"):
            relative = candidate.relative_to(root)
            if any(part in excluded_dirs for part in relative.parts):
                continue
            if candidate.is_symlink():
                identity = {
                    "root": resolved,
                    "state": "unsafe_symbolic_link",
                    "sha256": "",
                    "file_count": 0,
                    "symlink": relative.as_posix(),
                }
                if cache is not None:
                    cache[cache_key] = copy.deepcopy(identity)
                return identity
            if candidate.is_file():
                files.append(candidate)
    except (OSError, RuntimeError) as exc:
        return {
            "root": resolved,
            "state": f"unreadable:{type(exc).__name__}",
            "sha256": "",
            "file_count": 0,
        }
    digest = hashlib.sha256()
    included = 0
    complete = True
    for candidate in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = candidate.relative_to(root).as_posix()
        file_identity = _file_identity(candidate, cache=cache)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(file_identity.get("state") or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(file_identity.get("sha256") or "").encode("ascii"))
        digest.update(b"\0")
        included += 1
        complete = complete and file_identity.get("state") == "present"
    identity = {
        "root": resolved,
        "state": (
            "present"
            if root.is_dir() and complete
            else "incomplete"
            if root.is_dir()
            else "missing"
        ),
        "sha256": digest.hexdigest() if root.is_dir() else "",
        "file_count": included,
    }
    if cache is not None:
        cache[cache_key] = copy.deepcopy(identity)
    return identity


def _canonical_payload_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="backslashreplace")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PlanRevision:
    """A content-addressed, append-only plan snapshot."""

    revision: int
    revision_id: str
    content_hash: str
    parent_hash: str | None
    source: str
    _plan: IntentPlan = field(repr=False)
    stage: str = ""
    finding_ids: tuple[str, ...] = ()
    diff: tuple[dict[str, Any], ...] = ()
    catalog_hash: str = ""
    contract_hash: str = ""

    @property
    def plan(self) -> IntentPlan:
        """Expose a clone so callers cannot mutate the recorded snapshot."""
        return deep_clone_plan(self._plan)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
            "parent_hash": self.parent_hash,
            "source": self.source,
            "stage": self.stage,
            "finding_ids": list(self.finding_ids),
            "diff": copy.deepcopy(list(self.diff)),
            "catalog_hash": self.catalog_hash,
            "contract_hash": self.contract_hash,
            "plan": copy.deepcopy(self._plan.to_dict()),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PlanRevision:
        """Rehydrate an audited revision and reject a corrupted snapshot."""
        raw_plan = payload.get("plan")
        if not isinstance(raw_plan, dict):
            raise ValueError("plan revision is missing its materialized plan")
        snapshot = IntentPlan.from_dict(copy.deepcopy(raw_plan))
        expected_hash = str(payload.get("content_hash") or "")
        actual_hash = canonical_plan_hash(snapshot)
        if not expected_hash or expected_hash != actual_hash:
            raise ValueError("plan revision content hash mismatch")
        revision = int(payload.get("revision") or 0)
        revision_id = str(payload.get("revision_id") or "")
        parent_hash = (
            str(payload["parent_hash"])
            if payload.get("parent_hash") is not None
            else None
        )
        source = str(payload.get("source") or "planner")
        expected_revision_id = (
            f"{snapshot.plan_id}:r{revision}:{expected_hash[:12]}"
        )
        if (
            snapshot.revision != revision
            or snapshot.revision_hash != expected_hash
            or (snapshot.parent_revision_hash or None) != parent_hash
            or snapshot.revision_source != source
            or revision_id != expected_revision_id
        ):
            raise ValueError(
                "plan revision envelope disagrees with its materialized revision"
            )
        return cls(
            revision=revision,
            revision_id=revision_id,
            content_hash=expected_hash,
            parent_hash=parent_hash,
            source=source,
            _plan=snapshot,
            stage=str(payload.get("stage") or ""),
            finding_ids=tuple(str(item) for item in payload.get("finding_ids") or []),
            diff=tuple(
                copy.deepcopy(item)
                for item in payload.get("diff") or []
                if isinstance(item, dict)
            ),
            catalog_hash=str(payload.get("catalog_hash") or ""),
            contract_hash=str(payload.get("contract_hash") or ""),
        )


def create_revision(
    plan: IntentPlan,
    *,
    revision: int,
    parent_hash: str | None = None,
    source: str = "planner",
    stage: str = "",
    finding_ids: list[str] | tuple[str, ...] = (),
    diff: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    catalog_hash: str = "",
    contract_hash: str = "",
) -> PlanRevision:
    """Seal ``plan`` into a detached revision with a stable content identity."""
    if revision < 0:
        raise ValueError("revision must be non-negative")
    snapshot = deep_clone_plan(plan)
    snapshot.revision = revision
    snapshot.parent_revision_hash = parent_hash or ""
    snapshot.revision_source = source
    content_hash = canonical_plan_hash(snapshot)
    snapshot.revision_hash = content_hash
    revision_id = f"{snapshot.plan_id}:r{revision}:{content_hash[:12]}"
    return PlanRevision(
        revision=revision,
        revision_id=revision_id,
        content_hash=content_hash,
        parent_hash=parent_hash,
        source=source,
        _plan=snapshot,
        stage=stage or source,
        finding_ids=tuple(str(item) for item in finding_ids),
        diff=tuple(copy.deepcopy(list(diff))),
        catalog_hash=catalog_hash,
        contract_hash=contract_hash,
    )
