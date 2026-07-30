"""One metadata interpretation shared by planning, resolution, and execution."""

from omni.core.field_contract import (
    contract_mapping,
    contract_text,
    field_binding_owner,
    field_resolver,
)


def test_field_contract_reads_all_compatible_extension_locations() -> None:
    schema = {
        "semantic_key": "top-level-key",
        "x-omni": {
            "binding_owner": "model",
            "expectation": {"kind": "explicit_enum"},
        },
        "x_omni": {"resolver": "arxiv_id"},
    }

    assert contract_text(schema, "semantic_key") == "top-level-key"
    assert contract_mapping(schema, "expectation") == {"kind": "explicit_enum"}
    assert field_resolver(schema) == "arxiv_id"
    assert field_binding_owner(schema) == "resolver"


def test_field_contract_uses_root_first_and_normalizes_authority() -> None:
    schema = {
        "binding_owner": " MODEL ",
        "x-omni": {"binding_owner": "compiler"},
    }

    assert field_binding_owner(schema) == "model"
