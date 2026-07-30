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

from kg_distiller.io_utils import read_json, write_jsonl
from kg_distiller.tone_extraction import (
    _exact_whitespace_variant,
    extract_tone_node,
)

SOURCE_EXEMPLARS = [
    "While these have\nbeen used, we prefer a falsifiable baseline.",
    "We first test the\r\nsimplest explanation before adding complexity.",
    "In our view, the evidence\tmust decide the boundary.",
]
MODEL_EXEMPLARS = [
    "While these have been used, we prefer a falsifiable baseline.",
    "We first test the simplest explanation before adding complexity.",
    "In our view, the evidence must decide the boundary.",
]


class _WhitespaceNormalizingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        del system
        self.calls += 1
        if "从候选原句中选出最终语气样例" in user:
            return {"tone_exemplars": MODEL_EXEMPLARS}
        if "`\\n`" not in user:
            raise AssertionError("candidate prompt must require JSON-escaped newlines")
        return {"tone_exemplars": MODEL_EXEMPLARS}


class _UnexpectedLLMCall:
    def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        raise AssertionError(f"cached resume unexpectedly called the LLM: {system} {user}")


class ToneExtractionTest(unittest.TestCase):
    def test_whitespace_equivalence_does_not_allow_text_changes(self) -> None:
        passage = "While these have\nbeen used."
        self.assertEqual(
            _exact_whitespace_variant("While these have been used.", passage),
            passage,
        )
        self.assertIsNone(
            _exact_whitespace_variant("While those have been used.", passage)
        )
        self.assertIsNone(_exact_whitespace_variant("a bc", "ab c"))

    def test_normalized_model_whitespace_maps_back_to_verbatim_source_slices(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="P04 换行(回归测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            scientist_id = "test-scientist"
            full_text = (
                "# Introduction\n"
                + " ".join(SOURCE_EXEMPLARS)
                + "\n# Methods\nMethod details."
            )
            write_jsonl(
                project
                / "scientist-corpus"
                / scientist_id
                / "source_objects.jsonl",
                [
                    {
                        "source_id": "src_0001",
                        "title": "Whitespace-Sensitive Evidence",
                        "source_type": "paper",
                        "author_role": "first",
                        "full_text": full_text,
                    }
                ],
            )
            llm = _WhitespaceNormalizingLLM()

            output = extract_tone_node(project, scientist_id, llm)
            self.assertEqual(read_json(output)["tone_exemplars"], SOURCE_EXEMPLARS)
            self.assertEqual(llm.calls, 2)

            resumed = extract_tone_node(project, scientist_id, _UnexpectedLLMCall())
            self.assertEqual(read_json(resumed)["tone_exemplars"], SOURCE_EXEMPLARS)


if __name__ == "__main__":
    unittest.main()
