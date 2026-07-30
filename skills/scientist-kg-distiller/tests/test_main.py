from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from kg_distiller import main as kg_main
from kg_distiller.io_utils import read_jsonl, write_jsonl
from kg_distiller.schemas import validate_source_object


class _MockJsonLLM:
    """Deterministic offline replacement for the JSON LLM provider."""

    model = "mock-json-llm"

    def __init__(self, excerpt: str) -> None:
        self.excerpt = excerpt
        self.requests: list[tuple[str, str]] = []

    def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        self.requests.append((system, user))
        return {
            "cards": [
                {
                    "source_id": "src_0001",
                    "excerpt": self.excerpt,
                    "location": {"section": "Introduction"},
                    "observation": "The scientist starts from a falsifiable baseline.",
                    "fact_type": "practical_habit",
                }
            ]
        }


class DistillCommandTest(unittest.TestCase):
    def test_distill_argument_parsing_preserves_all_entrypoint_options(self) -> None:
        project = Path("科研 项目(离线测试)")
        result = Path("蒸馏 结果(离线测试)")
        install = Path("人格 扫描目录(离线测试)")
        args = kg_main.parse_args(
            [
                "distill",
                "--scientist",
                "Ada Lovelace",
                "--field",
                "mathematics",
                "--institution",
                "University of London",
                "--identity-candidate",
                "openalex:A123",
                "--google-scholar-url",
                "https://scholar.example/ada",
                "--step",
                "evidence",
                "--resume",
                "--model",
                "mock-model",
                "--max-sources",
                "17",
                "--project-root",
                str(project),
                "--result-root",
                str(result),
                "--install-root",
                str(install),
            ]
        )

        self.assertEqual(args.command, "distill")
        self.assertEqual(args.scientist, "Ada Lovelace")
        self.assertEqual(args.field, "mathematics")
        self.assertEqual(args.institution, "University of London")
        self.assertEqual(args.identity_candidate, "openalex:A123")
        self.assertEqual(args.step, "evidence")
        self.assertTrue(args.resume)
        self.assertEqual(args.model, "mock-model")
        self.assertEqual(args.max_sources, 17)
        self.assertEqual(args.project_root, project)
        self.assertEqual(args.result_root, result)
        self.assertEqual(args.install_root, install)

    def test_distill_requires_identity_and_positive_source_budget(self) -> None:
        with self.assertRaises(SystemExit):
            kg_main.parse_args(["distill"])
        with self.assertRaises(SystemExit):
            kg_main.parse_args(
                ["distill", "--scientist-id", "ada-lovelace", "--max-sources", "0"]
            )

    def test_evidence_step_uses_injected_mock_llm_without_network(self) -> None:
        excerpt = "We test a falsifiable baseline before adding complexity."
        full_text = excerpt + " " + ("Offline scientific source material. " * 8)
        source = {
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
                "sha256": "0" * 64,
            },
            "identity_binding": {
                "accepted": True,
                "score": 1.0,
                "evidence": ["offline fixture"],
            },
            "quality": {
                "status": "usable",
                "character_count": len(full_text),
            },
        }
        validate_source_object(source)

        with tempfile.TemporaryDirectory(
            prefix="蒸馏器 中文(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            write_jsonl(
                project
                / "scientist-corpus"
                / "ada-lovelace"
                / "source_objects.jsonl",
                [source],
            )
            llm = _MockJsonLLM(excerpt)

            outputs = kg_main.distill(
                project,
                "ada-lovelace",
                selected_step="evidence",
                result_root=project / "result",
                llm=llm,
            )

            self.assertEqual(len(outputs), 1)
            cards = read_jsonl(outputs[0])
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["excerpt"], excerpt)
            self.assertEqual(len(llm.requests), 1)


if __name__ == "__main__":
    unittest.main()
