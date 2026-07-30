from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from kg_distiller.schemas import (
    validate_evidence_card,
    validate_source_object,
)


def _source_object() -> dict:
    full_text = "A source-grounded scientific passage. " * 8
    return {
        "schema_version": "1.0.0",
        "source_id": "src_0001",
        "scientist_id": "ada-lovelace",
        "title": "Notes on the Analytical Engine",
        "year": 1843,
        "source_type": "paper",
        "full_text": full_text,
        "authors": ["Ada Lovelace"],
        "author_role": "first",
        "provenance": {
            "origin": "offline-test-fixture",
            "sha256": "a" * 64,
        },
        "identity_binding": {
            "accepted": True,
            "score": 1.0,
            "evidence": ["verified fixture identity"],
        },
        "quality": {
            "status": "usable",
            "character_count": len(full_text),
        },
    }


def _evidence_card() -> dict:
    return {
        "schema_version": "1.0.0",
        "card_id": "l1_ada_lovelace_0001",
        "source_id": "src_0001",
        "source_title": "Notes on the Analytical Engine",
        "source_type": "paper",
        "year": 1843,
        "excerpt": "The engine might compose elaborate and scientific pieces of music.",
        "location": {
            "section": "Note A",
            "start_char": 0,
            "end_char": 68,
        },
        "observation": "The scientist reasons from general symbolic operations.",
        "fact_type": "explicit_judgment",
        "author_role": "first",
    }


class SchemaValidationTest(unittest.TestCase):
    def test_valid_source_object_and_evidence_card(self) -> None:
        validate_source_object(_source_object())
        validate_evidence_card(_evidence_card())

    def test_source_schema_reports_the_first_invalid_field(self) -> None:
        source = _source_object()
        source["full_text"] = "too short"
        with self.assertRaisesRegex(ValueError, "SourceObject schema error at full_text"):
            validate_source_object(source)

    def test_evidence_schema_rejects_invalid_card_identifier(self) -> None:
        card = _evidence_card()
        card["card_id"] = "invalid"
        with self.assertRaisesRegex(ValueError, "EvidenceCard schema error at card_id"):
            validate_evidence_card(card)


if __name__ == "__main__":
    unittest.main()
