"""Execution-boundary JSON Schema security contracts."""

from __future__ import annotations

from typing import Any

import pytest

from omni.core.tool_contracts import (
    validate_json_schema,
    validate_json_schema_definition,
)


def test_internal_schema_references_remain_supported() -> None:
    schema = {
        "$defs": {
            "nonempty": {
                "type": "string",
                "minLength": 1,
            }
        },
        "type": "object",
        "properties": {
            "name": {"$ref": "#/$defs/nonempty"},
        },
        "required": ["name"],
    }

    assert validate_json_schema({"name": "omni"}, schema) == ()
    assert validate_json_schema({"name": ""}, schema)


def test_empty_uri_reference_remains_a_local_recursive_reference() -> None:
    schema = {
        "type": "object",
        "properties": {
            "child": {
                "anyOf": [
                    {"type": "null"},
                    {"$ref": ""},
                ]
            }
        },
    }

    assert validate_json_schema({"child": {"child": None}}, schema) == ()


def test_external_schema_reference_is_rejected_without_network(
    monkeypatch: Any,
) -> None:
    def unexpected_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("schema validation attempted network access")

    monkeypatch.setattr(
        "urllib.request.urlopen",
        unexpected_network,
    )

    errors = validate_json_schema(
        {},
        {"$ref": "https://schema.example.invalid/provider.json"},
    )

    assert errors == (
        {
            "path": "$",
            "keyword": "external_ref",
            "message": "external schema references are not allowed",
        },
    )


def test_relative_external_schema_reference_is_rejected() -> None:
    errors = validate_json_schema(
        {},
        {
            "$defs": {
                "nested": {
                    "$dynamicRef": "../shared/provider.json",
                }
            }
        },
    )

    assert errors[0]["keyword"] == "external_ref"


@pytest.mark.parametrize(
    "reference",
    [
        "https://schema.example.invalid/provider.json",
        "file:///etc/passwd",
        "omni+schema://provider/contract",
    ],
)
def test_pointer_trampoline_cannot_hide_external_reference(
    monkeypatch: Any,
    reference: str,
) -> None:
    retrievals: list[str] = []

    def unexpected_retrieval(request: Any, *_args: Any, **_kwargs: Any) -> Any:
        retrievals.append(str(getattr(request, "full_url", request)))
        raise AssertionError("schema validation attempted external retrieval")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_retrieval)
    schema = {
        "x-provider-extension": {
            "$ref": reference,
        },
        "$ref": "#/x-provider-extension",
    }

    errors = validate_json_schema_definition(schema)

    assert errors[0]["keyword"] == "external_ref"
    assert retrievals == []


def test_unreferenced_annotation_data_is_not_mistaken_for_a_schema() -> None:
    schema = {
        "type": "object",
        "examples": [
            {
                "$ref": "https://example.invalid/ordinary-data",
            }
        ],
    }

    assert validate_json_schema_definition(schema) == ()
    assert validate_json_schema({}, schema) == ()


def test_local_pointer_target_under_annotation_is_validated_as_schema() -> None:
    schema = {
        "examples": [{"type": "string"}],
        "$ref": "#/examples/0",
    }

    assert validate_json_schema_definition(schema) == ()
    assert validate_json_schema("omni", schema) == ()
    assert validate_json_schema(7, schema)[0]["keyword"] == "type"


def test_anchor_in_embedded_resource_is_not_visible_from_root_resource() -> None:
    errors = validate_json_schema_definition(
        {
            "$id": "https://schema.example.invalid/root",
            "$defs": {
                "embedded": {
                    "$id": "embedded",
                    "$anchor": "private",
                    "type": "string",
                }
            },
            "$ref": "#private",
        }
    )

    assert errors[0]["keyword"] == "unresolved_ref"


def test_dynamic_anchor_in_sibling_resource_is_not_visible() -> None:
    errors = validate_json_schema_definition(
        {
            "$id": "https://schema.example.invalid/root",
            "$defs": {
                "owner": {
                    "$id": "owner",
                    "$dynamicAnchor": "item",
                    "type": "string",
                },
                "consumer": {
                    "$id": "consumer",
                    "$dynamicRef": "#item",
                },
            },
            "$ref": "#/$defs/consumer",
        }
    )

    assert errors[0]["keyword"] == "unresolved_ref"


def test_embedded_resource_can_resolve_its_own_anchor() -> None:
    schema = {
        "$id": "https://schema.example.invalid/root",
        "$defs": {
            "embedded": {
                "$id": "embedded",
                "$defs": {
                    "value": {
                        "$anchor": "local",
                        "type": "string",
                    }
                },
                "$ref": "#local",
            }
        },
        "$ref": "#/$defs/embedded",
    }

    assert validate_json_schema_definition(schema) == ()
    assert validate_json_schema("omni", schema) == ()
    assert validate_json_schema(7, schema)[0]["keyword"] == "type"


def test_common_dynamic_recursive_schema_remains_supported() -> None:
    schema = {
        "$id": "https://schema.example.invalid/tree",
        "$dynamicAnchor": "node",
        "type": "object",
        "properties": {
            "children": {
                "type": "array",
                "items": {"$dynamicRef": "#node"},
            }
        },
        "required": ["children"],
    }

    assert validate_json_schema_definition(schema) == ()
    assert validate_json_schema(
        {"children": [{"children": []}]},
        schema,
    ) == ()


def test_relative_reference_to_embedded_local_resource_remains_supported() -> None:
    schema = {
        "$id": "https://schema.example.invalid/root",
        "$defs": {
            "embedded": {
                "$id": "embedded",
                "type": "string",
            }
        },
        "$ref": "embedded",
    }

    assert validate_json_schema_definition(schema) == ()
    assert validate_json_schema("omni", schema) == ()
    assert validate_json_schema(7, schema)[0]["keyword"] == "type"


def test_missing_internal_reference_fails_eager_schema_preflight() -> None:
    errors = validate_json_schema_definition(
        {
            "oneOf": [
                {"type": "object"},
                {"$ref": "#/$defs/missing"},
            ],
            "$defs": {
                "present": {"type": "string"},
            },
        }
    )

    assert errors == (
        {
            "path": "$",
            "keyword": "unresolved_ref",
            "message": "execution contract contains an unresolved local reference",
        },
    )


def test_internal_reference_must_resolve_to_a_schema() -> None:
    errors = validate_json_schema_definition(
        {
            "default": {"not_a_schema": 7},
            "$ref": "#/default/not_a_schema",
        }
    )

    assert errors[0]["keyword"] == "unresolved_ref"
