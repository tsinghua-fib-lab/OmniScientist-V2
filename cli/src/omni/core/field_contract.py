"""Canonical accessors for provider field-contract metadata.

Skills may keep Omni-specific schema metadata at the field root or under the
``x-omni``/``x_omni`` extension containers. Every planner, resolver,
validator, and execution gate must resolve those declarations identically.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def metadata_containers(field_schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield contract containers in compatibility precedence order."""
    yield field_schema
    for key in ("x-omni", "x_omni"):
        value = field_schema.get(key)
        if isinstance(value, dict):
            yield value


def contract_text(
    field_schema: dict[str, Any],
    *keys: str,
    lower: bool = False,
) -> str:
    """Return the first non-empty text value for ``keys``."""
    for container in metadata_containers(field_schema):
        for key in keys:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                return text.lower() if lower else text
    return ""


def contract_mapping(
    field_schema: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    """Return the first mapping-valued extension, or an empty mapping."""
    for container in metadata_containers(field_schema):
        value = container.get(key)
        if isinstance(value, dict):
            return value
    return {}


def field_resolver(field_schema: dict[str, Any]) -> str:
    """Return the canonical resolver/format name for a field."""
    return contract_text(field_schema, "resolver", "format", lower=True)


def field_binding_owner(field_schema: dict[str, Any]) -> str:
    """Return the authority that is allowed to establish this binding."""
    if field_resolver(field_schema):
        return "resolver"
    return contract_text(field_schema, "binding_owner", lower=True) or "compiler"


def instruction_field(input_schema: object) -> str:
    """Return the contract-declared free-text instruction field, if any.

    Explicit semantic metadata wins, with required fields preferred. The
    legacy required ``input`` convention remains supported, but identifiers,
    paths, enums, and other constrained strings are never used as a goal slot.
    """
    if not isinstance(input_schema, dict):
        return ""
    required_raw = input_schema.get("required")
    required = [str(name) for name in required_raw] if isinstance(required_raw, list) else []
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return ""
    for name in required:
        field_schema = properties.get(name)
        if _is_instruction(field_schema):
            return name
    input_field = properties.get("input")
    if "input" in required and _is_free_text_field(input_field):
        return "input"
    for name, field_schema in properties.items():
        if _is_instruction(field_schema):
            return str(name)
    return ""


def _is_instruction(field_schema: object) -> bool:
    return (
        isinstance(field_schema, dict)
        and contract_text(field_schema, "semantic_role") == "instruction"
        and _is_free_text_field(field_schema)
    )


def _is_free_text_field(field_schema: object) -> bool:
    return (
        isinstance(field_schema, dict)
        and str(field_schema.get("type") or "string") == "string"
        and not field_schema.get("format")
        and not field_schema.get("enum")
    )


__all__ = [
    "contract_mapping",
    "contract_text",
    "field_binding_owner",
    "field_resolver",
    "instruction_field",
    "metadata_containers",
]
