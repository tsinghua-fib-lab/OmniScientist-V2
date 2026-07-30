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


__all__ = [
    "contract_mapping",
    "contract_text",
    "field_binding_owner",
    "field_resolver",
    "metadata_containers",
]
