from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_VERSION = "1.0.0"
KG_SCHEMA_VERSION = "1.1.0"


@cache
def _validator(filename: str) -> Draft202012Validator:
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / filename
    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_schema(value: Any, filename: str, *, label: str) -> None:
    errors = sorted(
        _validator(filename).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    raise ValueError(f"{label} schema error at {location}: {error.message}")


def validate_source_object(value: dict[str, Any]) -> None:
    validate_schema(value, "source-object.schema.json", label="SourceObject")


def validate_evidence_card(value: dict[str, Any]) -> None:
    validate_schema(value, "evidence-card.schema.json", label="EvidenceCard")


def validate_kg_schema(value: dict[str, Any]) -> None:
    validate_schema(value, "kg.schema.json", label="KG")
