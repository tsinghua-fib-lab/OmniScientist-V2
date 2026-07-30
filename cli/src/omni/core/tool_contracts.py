"""Provider input compilation and contract validation.

Semantic planning produces capability data.  After a provider is selected,
this module compiles that data exactly once against the provider's JSON schema.
It never knows skill names and never maps vocabulary such as ``query`` to
``search_query``.  Domain extraction is delegated to schema field resolvers.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from omni.core.field_contract import (
    contract_text,
    field_binding_owner,
    field_resolver,
)
from omni.core.field_resolvers import has_resolver, resolve_field


@dataclass(frozen=True, slots=True)
class ProviderInputCompilation:
    """Canonical arguments or deterministic contract errors for one provider."""

    arguments: dict[str, Any]
    errors: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class PreparedJSONSchema:
    """One private schema snapshot compiled for local-only validation."""

    validator: Any | None
    definition_errors: tuple[dict[str, Any], ...] = ()


class ProviderInputCompiler:
    """Compile planner input into one provider's declared input schema."""

    def compile_entry(
        self,
        entry: Any,
        *,
        semantic_input: dict[str, Any] | None,
        raw_message: str,
    ) -> ProviderInputCompilation:
        schema = getattr(entry, "input_schema", None)
        declared = getattr(entry, "input_schema_declared", None)
        if declared is False or (
            declared is None
            and str(getattr(entry, "contract_level", "") or "") == "none"
            and schema in ({}, {"type": "object", "properties": {}})
        ):
            schema = None
        return self.compile_schema(
            schema,
            semantic_input=semantic_input or {},
            raw_message=raw_message,
            declared=declared,
            strict_unknown=(
                str(getattr(entry, "contract_level", "") or "") == "full"
                or (isinstance(schema, dict) and schema.get("additionalProperties") is False)
            ),
        )

    def compile_schema(
        self,
        schema: Any,
        *,
        semantic_input: dict[str, Any] | None,
        raw_message: str,
        declared: bool | None = None,
        strict_unknown: bool = False,
    ) -> ProviderInputCompilation:
        semantic = dict(semantic_input or {})
        # ``None`` means that the provider declared no schema.  Every other
        # value is an explicit contract and must reach JSON-Schema definition
        # validation, including ``{}``, boolean schemas, composed roots, and
        # malformed provider-owned definitions.
        if schema is None and declared is not True:
            payload = semantic or ({"input": raw_message} if raw_message else {})
            return ProviderInputCompilation(arguments=payload)

        schema_object = schema if isinstance(schema, dict) else {}
        declared_object = _declared_object_schema(schema_object)
        properties = _properties(schema_object)
        required = _required_fields(schema_object)
        if not declared_object:
            # A composed or unconstrained root has no safe projection surface:
            # preserve the proposed instance and let the complete JSON Schema
            # decide whether it is valid.
            arguments = semantic or ({"input": raw_message} if raw_message else {})
            errors = _nested_schema_errors(
                schema,
                arguments,
                existing=[],
                declared=declared,
            )
            return ProviderInputCompilation(arguments=arguments, errors=tuple(errors))

        consumed_semantic = {name for name in properties if name in semantic}
        arguments = {name: semantic[name] for name in properties if name in semantic}

        for name, field_schema in properties.items():
            resolver_name = field_resolver(field_schema)
            if not resolver_name or not has_resolver(resolver_name):
                continue
            candidates = dict(semantic)
            if raw_message:
                candidates.setdefault("input", raw_message)
            if name in arguments:
                candidates[name] = arguments[name]
            resolution = resolve_field(resolver_name, candidates)
            if resolution.resolved:
                arguments[name] = resolution.value
                consumed_semantic.update(
                    _resolver_consumed_keys(
                        semantic,
                        declared=set(properties),
                        resolved_value=resolution.value,
                        label=resolution.label,
                    )
                )

        unresolved_text = [
            name
            for name in required
            if name not in arguments
            and _is_text_field(properties.get(name, {}))
            and not _has_registered_resolver(properties.get(name, {}))
        ]
        if len(unresolved_text) == 1:
            semantic_item = (
                _single_semantic_item(semantic, consumed=set(arguments)) if not arguments else None
            )
            candidate = semantic_item[1] if semantic_item is not None else None
            if candidate is None and raw_message:
                candidate = raw_message
            if candidate is not None:
                arguments[unresolved_text[0]] = candidate
                if semantic_item is not None:
                    consumed_semantic.add(semantic_item[0])

        # Some providers support a resume-only invocation and therefore cannot
        # mark their natural-language instruction field as JSON-schema
        # ``required``.  An explicit semantic-role declaration makes that
        # optional instruction slot safe to bind without guessing by field
        # name or copying the goal into identifiers, paths, or enum fields.
        if raw_message and not arguments:
            instruction_fields = [
                name
                for name, field_schema in properties.items()
                if contract_text(field_schema, "semantic_role", lower=True) == "instruction"
                and _is_text_field(field_schema)
                and not _has_registered_resolver(field_schema)
            ]
            if len(instruction_fields) == 1:
                arguments[instruction_fields[0]] = raw_message

        if not required and len(properties) == 1 and not arguments:
            name, field_schema = next(iter(properties.items()))
            if _is_text_field(field_schema) and not _has_registered_resolver(field_schema):
                semantic_item = _single_semantic_item(semantic, consumed=set())
                if semantic_item is not None:
                    consumed_semantic.add(semantic_item[0])
                    arguments[name] = semantic_item[1]

        if schema_object.get("additionalProperties") is True:
            arguments.update(
                {key: value for key, value in semantic.items() if key not in arguments}
            )
            consumed_semantic.update(semantic)

        errors = _validate_schema_arguments(schema_object, arguments)
        errors.extend(
            _nested_schema_errors(
                schema_object,
                arguments,
                existing=errors,
            )
        )
        enforce_unknown = schema_object.get("additionalProperties") is False or (
            strict_unknown and schema_object.get("additionalProperties") is not True
        )
        if enforce_unknown:
            errors.extend(
                _unknown_semantic_errors(
                    semantic,
                    declared=set(properties),
                    consumed=consumed_semantic,
                )
            )
        return ProviderInputCompilation(arguments=arguments, errors=tuple(errors))


def validate_json_schema(
    value: Any,
    schema: Any,
    *,
    declared: bool | None = None,
) -> tuple[dict[str, Any], ...]:
    """Return deterministic JSON-Schema violations without mutating inputs.

    Unlike the planning compiler below, this function performs no coercion,
    resolver lookup, aliasing, or default insertion. Draft 2020-12 validation
    keeps nested/composed schemas strict while normalized errors avoid echoing
    potentially sensitive argument values into model-visible observations.
    """
    return validate_prepared_json_schema(
        value,
        prepare_json_schema(schema, declared=declared),
    )


def prepare_json_schema(
    schema: Any,
    *,
    declared: bool | None = None,
) -> PreparedJSONSchema:
    """Snapshot and compile a schema with an offline, fail-closed registry.

    The returned validator owns a deep-copied schema. Provider code therefore
    cannot change the contract between execution-time preflight and output
    validation. Every reachable reference is resolved against an in-memory
    registry whose retrieval callback always denies network, file, and custom
    URI access.
    """
    if declared is False:
        return PreparedJSONSchema(validator=None)
    if schema is None:
        return (
            PreparedJSONSchema(
                validator=None,
                definition_errors=(_invalid_schema_definition_error(),),
            )
            if declared is True
            else PreparedJSONSchema(validator=None)
        )
    try:
        snapshot = deepcopy(schema)
    except Exception:  # noqa: BLE001 - non-copyable contracts are not executable.
        return PreparedJSONSchema(
            validator=None,
            definition_errors=(_invalid_schema_definition_error(),),
        )
    if not isinstance(snapshot, (dict, bool)):
        return PreparedJSONSchema(
            validator=None,
            definition_errors=(_invalid_schema_definition_error(),),
        )

    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    try:
        Draft202012Validator.check_schema(snapshot)
    except SchemaError:
        return PreparedJSONSchema(
            validator=None,
            definition_errors=(_invalid_schema_definition_error(),),
        )
    try:
        resource = Resource.from_contents(
            snapshot,
            default_specification=DRAFT202012,
        )
        base_uri = resource.id() or ""
        registry = (
            Registry(retrieve=_deny_external_schema_retrieval)
            .with_resource(base_uri, resource)
            .crawl()
        )
        resolver = registry.resolver(base_uri)
        reference_error = _reachable_reference_error(
            resource.contents,
            resolver=resolver,
            visited=set(),
        )
        if reference_error is not None:
            return PreparedJSONSchema(
                validator=None,
                definition_errors=(reference_error,),
            )
        validator = Draft202012Validator(
            snapshot,
            registry=registry,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
    except Exception:  # noqa: BLE001 - malformed registries/contracts fail closed.
        return PreparedJSONSchema(
            validator=None,
            definition_errors=(_invalid_schema_resolution_error(),),
        )
    return PreparedJSONSchema(validator=validator)


def validate_prepared_json_schema(
    value: Any,
    prepared: PreparedJSONSchema,
) -> tuple[dict[str, Any], ...]:
    """Validate an instance against a previously snapshotted contract."""
    if prepared.definition_errors:
        return prepared.definition_errors
    if prepared.validator is None:
        return ()
    try:
        errors = sorted(
            prepared.validator.iter_errors(value),
            key=lambda error: (
                tuple(str(part) for part in error.absolute_path),
                str(error.validator or ""),
            ),
        )
    except Exception:  # noqa: BLE001 - malformed/unresolvable contracts fail closed.
        return (
            {
                "path": "$",
                "keyword": "invalid_schema",
                "message": "execution contract could not be resolved locally",
            },
        )
    return tuple(record for error in errors for record in _validation_error_records(error))


def validate_json_schema_definition(
    schema: Any,
    *,
    declared: bool | None = None,
) -> tuple[dict[str, Any], ...]:
    """Validate a contract itself without evaluating an instance.

    Provider contracts are local data. External references are rejected before
    jsonschema can resolve them, and every internal reference is checked
    eagerly so an invalid output contract cannot be discovered only after a
    side-effecting provider has run.
    """
    return prepare_json_schema(schema, declared=declared).definition_errors


def provider_schema_definition_errors(entry: Any) -> tuple[dict[str, Any], ...]:
    """Return invalid explicitly declared provider input/output contracts.

    Manifest presence is authority: an absent schema remains unconstrained,
    while an explicitly declared YAML ``null`` is an invalid JSON Schema and
    must fail before planning or execution.
    """
    errors: list[dict[str, Any]] = []
    for schema_field in ("input_schema", "output_schema"):
        schema = getattr(entry, schema_field, None)
        declared = getattr(entry, f"{schema_field}_declared", None)
        errors.extend(
            {
                **error,
                "schema_field": schema_field,
            }
            for error in validate_json_schema_definition(
                schema,
                declared=declared,
            )
        )
    return tuple(errors)


def _deny_external_schema_retrieval(uri: str) -> Any:
    """Registry callback that makes every non-bundled resource unavailable."""
    from referencing.exceptions import NoSuchResource

    raise NoSuchResource(ref=uri)


def _reachable_reference_error(
    contents: Any,
    *,
    resolver: Any,
    visited: set[tuple[int, str]],
) -> dict[str, Any] | None:
    """Resolve the active schema graph with JSON Schema resource scoping."""
    if isinstance(contents, bool):
        return None
    if not isinstance(contents, dict):
        return _unresolved_schema_reference_error()
    key = (id(contents), str(getattr(resolver, "_base_uri", "")))
    if key in visited:
        return None
    visited.add(key)

    from referencing import Resource
    from referencing.exceptions import Unresolvable
    from referencing.jsonschema import DRAFT202012

    for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
        reference = contents.get(keyword)
        if not isinstance(reference, str):
            continue
        try:
            resolved = resolver.lookup(reference)
        except Unresolvable:
            return (
                _external_schema_reference_error()
                if reference and not reference.startswith("#")
                else _unresolved_schema_reference_error()
            )
        if not isinstance(resolved.contents, (dict, bool)):
            return _unresolved_schema_reference_error()
        error = _reachable_reference_error(
            resolved.contents,
            resolver=resolved.resolver,
            visited=visited,
        )
        if error is not None:
            return error

    resource = Resource.from_contents(
        contents,
        default_specification=DRAFT202012,
    )
    for subresource in resource.subresources():
        error = _reachable_reference_error(
            subresource.contents,
            resolver=resolver.in_subresource(subresource),
            visited=visited,
        )
        if error is not None:
            return error
    return None


def _invalid_schema_definition_error() -> dict[str, Any]:
    return {
        "path": "$",
        "keyword": "invalid_schema",
        "message": "execution contract is not a valid JSON schema",
    }


def _invalid_schema_resolution_error() -> dict[str, Any]:
    return {
        "path": "$",
        "keyword": "invalid_schema",
        "message": "execution contract could not be resolved locally",
    }


def _external_schema_reference_error() -> dict[str, Any]:
    return {
        "path": "$",
        "keyword": "external_ref",
        "message": "external schema references are not allowed",
    }


def _unresolved_schema_reference_error() -> dict[str, Any]:
    return {
        "path": "$",
        "keyword": "unresolved_ref",
        "message": "execution contract contains an unresolved local reference",
    }


def _validation_error_records(error: Any) -> tuple[dict[str, Any], ...]:
    parts = list(error.absolute_path)
    keyword = str(error.validator or "schema")
    if keyword == "required" and isinstance(error.instance, dict):
        required = error.validator_value if isinstance(error.validator_value, list) else []
        missing = [name for name in required if name not in error.instance]
        return tuple(
            _validation_error([*parts, name], keyword, "required property is missing")
            for name in missing
        )
    if keyword == "additionalProperties" and isinstance(error.instance, dict):
        properties = error.schema.get("properties")
        declared = properties if isinstance(properties, dict) else {}
        extras = sorted(str(name) for name in error.instance if name not in declared)
        return tuple(
            _validation_error([*parts, name], keyword, "additional property is not allowed")
            for name in extras
        )
    messages = {
        "type": "value has the wrong JSON type",
        "enum": "value is not one of the allowed values",
        "const": "value does not match the required constant",
        "pattern": "string does not match the required pattern",
        "format": "string does not match the required format",
        "oneOf": "value must match exactly one allowed schema",
        "anyOf": "value does not match an allowed schema",
    }
    return (
        _validation_error(
            parts, keyword, messages.get(keyword, "value violates the schema constraint")
        ),
    )


def _validation_error(
    parts: list[Any],
    keyword: str,
    message: str,
) -> dict[str, Any]:
    path = ""
    for part in parts:
        if isinstance(part, int):
            path = f"{path}[{part}]"
        else:
            path = f"{path}.{part}" if path else str(part)
    return {"path": path or "$", "keyword": keyword, "message": message}


def skill_input_contract_error(skill_or_entry: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Return the first contract error for already-compiled provider arguments.

    This function is deliberately pure.  Compilation and deterministic
    normalization happen before plan validation; validation never rewrites a
    plan or mutates an invocation at execution time.
    """
    schema = getattr(skill_or_entry, "input_schema", None)
    declared = getattr(skill_or_entry, "input_schema_declared", None)
    if declared is False or (declared is None and schema is None):
        return {}
    schema_object = schema if isinstance(schema, dict) else {}
    errors = _validate_schema_arguments(schema_object, params)
    errors.extend(
        _nested_schema_errors(
            schema,
            params,
            existing=errors,
            declared=declared,
        )
    )
    if (
        str(getattr(skill_or_entry, "contract_level", "") or "") == "full"
        and isinstance(schema, dict)
        and _declared_object_schema(schema)
        and schema.get("additionalProperties") is not True
    ):
        errors.extend(
            _unknown_semantic_errors(
                params,
                declared=set(_properties(schema)),
                consumed=set(),
            )
        )
    return errors[0] if errors else {}


def input_contract_repair_capability(skill_or_entry: Any, field_name: str) -> str:
    """Return the contract-declared producer capability for a missing field."""
    schema = getattr(skill_or_entry, "input_schema", None)
    if not isinstance(schema, dict):
        return ""
    field_schema = _properties(schema).get(field_name)
    if not isinstance(field_schema, dict):
        return ""
    return contract_text(
        field_schema,
        "repair_capability",
        "fallback_capability",
    )


def input_contract_binding_owner(skill_or_entry: Any, field_name: str) -> str:
    """Return the host-authoritative owner for one provider input field."""
    schema = getattr(skill_or_entry, "input_schema", None)
    if not isinstance(schema, dict):
        return "compiler"
    field_schema = _properties(schema).get(field_name)
    if not isinstance(field_schema, dict):
        return "compiler"
    return field_binding_owner(field_schema)


def _validate_schema_arguments(
    schema: dict[str, Any], arguments: dict[str, Any]
) -> list[dict[str, Any]]:
    properties = _properties(schema)
    errors: list[dict[str, Any]] = []
    for name in _required_fields(schema):
        if name not in arguments:
            field_schema = properties.get(name, {})
            errors.append(
                _contract_error(
                    name,
                    field_schema,
                    code=f"missing_{name}",
                    reason=f"required field '{name}' was not resolved",
                    keyword="required",
                )
            )
    for name, value in arguments.items():
        field_schema = properties.get(name)
        if not isinstance(field_schema, dict):
            continue
        expected = str(field_schema.get("type") or "")
        if expected and not _matches_type(value, expected):
            errors.append(
                _contract_error(
                    name,
                    field_schema,
                    code=f"invalid_{name}_type",
                    reason=f"field '{name}' must be {expected}",
                    keyword="type",
                    actual=value,
                )
            )
            continue
        resolver_name = field_resolver(field_schema)
        if resolver_name and has_resolver(resolver_name) and _has_value(value):
            resolution = resolve_field(resolver_name, {name: value, "input": value})
            if not resolution.resolved:
                errors.append(
                    _contract_error(
                        name,
                        field_schema,
                        code=f"invalid_{name}",
                        reason=resolution.reason or f"field '{name}' is invalid",
                        label=resolution.label,
                        keyword="format",
                        actual=value,
                    )
                )
        enum = field_schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            errors.append(
                _contract_error(
                    name,
                    field_schema,
                    code=f"invalid_{name}_value",
                    reason=f"field '{name}' is not one of the allowed values",
                    keyword="enum",
                    actual=value,
                    allowed_values=enum,
                )
            )
    return errors


def _contract_error(
    field_name: str,
    field_schema: dict[str, Any],
    *,
    code: str,
    reason: str,
    label: str = "",
    keyword: str = "",
    path: str = "",
    actual: Any = None,
    allowed_values: list[Any] | None = None,
) -> dict[str, Any]:
    error = {
        "code": code,
        "field": field_name,
        "path": path or field_name,
        "keyword": keyword,
        "missing": [field_name],
        "label": label,
        "reason": reason,
        "message": (
            contract_text(field_schema, "missing_message", "message")
            or (reason if not field_name else "")
            or (
                f"A value for `{field_name}` is required before this action can continue. {reason}"
            ).strip()
        ),
        "allowed_values": list(allowed_values or []),
    }
    if actual is not None:
        error["actual"] = actual
    return error


def _nested_schema_errors(
    schema: Any,
    arguments: dict[str, Any],
    *,
    existing: list[dict[str, Any]],
    declared: bool | None = None,
) -> list[dict[str, Any]]:
    """Convert complete JSON-Schema findings into compiler-shaped records."""
    known = {
        (str(item.get("path") or item.get("field") or ""), str(item.get("keyword") or ""))
        for item in existing
    }
    output: list[dict[str, Any]] = []
    for violation in validate_json_schema(
        arguments,
        schema,
        declared=declared,
    ):
        path = str(violation.get("path") or "$")
        keyword = str(violation.get("keyword") or "schema")
        if (path, keyword) in known:
            continue
        field_name = _top_level_path(path)
        allowed_values = _allowed_values_at_path(schema, path, keyword=keyword)
        output.append(
            _contract_error(
                field_name,
                _properties(schema).get(field_name, {}) if isinstance(schema, dict) else {},
                code="provider_schema_invalid",
                reason=str(
                    violation.get("message") or "provider input violates the selected schema"
                ),
                keyword=keyword,
                path=path,
                actual=(
                    None
                    if _is_schema_definition_keyword(keyword)
                    else _value_at_path(arguments, path)
                ),
                allowed_values=(
                    allowed_values if not _is_schema_definition_keyword(keyword) else []
                ),
            )
        )
        known.add((path, keyword))
    return output


def _unknown_semantic_errors(
    semantic: dict[str, Any],
    *,
    declared: set[str],
    consumed: set[str],
) -> list[dict[str, Any]]:
    return [
        {
            "code": "unknown_provider_field",
            "field": name,
            "path": name,
            "keyword": "additionalProperties",
            "missing": [],
            "label": "",
            "reason": f"field '{name}' is not declared by the selected provider",
            "message": f"field '{name}' is not declared by the selected provider",
            "allowed_values": [],
        }
        for name in sorted(semantic)
        if name not in declared and name not in consumed
    ]


def _single_semantic_item(
    semantic: dict[str, Any],
    *,
    consumed: set[str],
) -> tuple[str, Any] | None:
    values = [
        (key, value) for key, value in semantic.items() if key not in consumed and _is_scalar(value)
    ]
    return values[0] if len(values) == 1 else None


def _resolver_consumed_keys(
    semantic: dict[str, Any],
    *,
    declared: set[str],
    resolved_value: str,
    label: str,
) -> set[str]:
    candidates = [
        (name, value)
        for name, value in semantic.items()
        if name not in declared and _has_value(value)
    ]
    if len(candidates) == 1:
        return {candidates[0][0]}
    needles = {str(resolved_value or "").strip(), str(label or "").strip()}
    return {
        name
        for name, value in candidates
        if any(needle and needle in str(value) for needle in needles)
    }


def _top_level_path(path: str) -> str:
    if not path or path == "$":
        return ""
    return path.split(".", 1)[0].split("[", 1)[0]


def _value_at_path(value: Any, path: str) -> Any:
    if not path or path == "$":
        return value
    current = value
    for token in path.replace("[", ".").replace("]", "").split("."):
        if not token:
            continue
        try:
            current = current[int(token)] if isinstance(current, list) else current[token]
        except (IndexError, KeyError, TypeError, ValueError):
            return None
    return current


def _allowed_values_at_path(
    schema: dict[str, Any],
    path: str,
    *,
    keyword: str,
) -> list[Any]:
    if keyword != "enum":
        return []
    current: Any = schema
    for token in path.replace("[", ".").replace("]", "").split("."):
        if not token:
            continue
        if token.isdigit():
            current = current.get("items", {}) if isinstance(current, dict) else {}
            continue
        properties = current.get("properties") if isinstance(current, dict) else None
        current = properties.get(token, {}) if isinstance(properties, dict) else {}
    enum = current.get("enum") if isinstance(current, dict) else None
    return list(enum) if isinstance(enum, list) else []


def _declared_object_schema(schema: dict[str, Any]) -> bool:
    if not isinstance(schema, dict):
        return False
    # Projection is safe only for a direct object-field contract. Composed
    # schemas may declare their properties inside branches, so filtering by the
    # root would destroy a JSON-Schema-valid instance before validation.
    if any(key in schema for key in ("allOf", "anyOf", "oneOf", "if", "then", "else")):
        return False
    return any(key in schema for key in ("properties", "required", "additionalProperties"))


def _is_schema_definition_keyword(keyword: str) -> bool:
    return keyword in {"invalid_schema", "external_ref", "unresolved_ref"}


def _properties(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = schema.get("properties")
    if not isinstance(raw, dict):
        return {}
    return {str(name): value if isinstance(value, dict) else {} for name, value in raw.items()}


def _required_fields(schema: dict[str, Any]) -> list[str]:
    raw = schema.get("required")
    return [str(name) for name in raw] if isinstance(raw, list) else []


def _is_text_field(field_schema: dict[str, Any]) -> bool:
    return str(field_schema.get("type") or "string") == "string"


def _has_registered_resolver(field_schema: dict[str, Any]) -> bool:
    return has_resolver(field_resolver(field_schema))


def _matches_type(value: Any, expected: str) -> bool:
    expected_types: dict[str, type[Any] | tuple[type[Any], ...]] = {
        "string": str,
        "object": dict,
        "array": list,
        "boolean": bool,
        "integer": int,
        "number": (int, float),
    }
    target = expected_types.get(expected)
    if target is None:
        return True
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, target)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, str | int | float | bool)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple | set | dict):
        return bool(value)
    return True


__all__ = [
    "PreparedJSONSchema",
    "ProviderInputCompilation",
    "ProviderInputCompiler",
    "input_contract_binding_owner",
    "input_contract_repair_capability",
    "prepare_json_schema",
    "provider_schema_definition_errors",
    "skill_input_contract_error",
    "validate_json_schema",
    "validate_prepared_json_schema",
]
