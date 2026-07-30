from __future__ import annotations

import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from kg_distiller import main as kg_main
from kg_distiller.io_utils import read_json, write_json, write_jsonl
from kg_distiller.kg_encoder import encode_kg, validate_kg
from kg_distiller.kg_store import KGInstallError, install_kg_store, validate_kg_store

SCIENTIST_ID = "test-scientist"
L2_IDS = [f"l2_test_scientist_C0{index}" for index in range(1, 8)]
L1_ID = "l1_test_scientist_0001"


def _l2_nodes() -> list[dict]:
    return [
        {
            "node_id": node_id,
            "level": "L2",
            "category": f"C0{index}",
            "category_label": f"怎样进行研究判断 {index}",
            "description": f"在证据边界内形成可检验判断 {index}",
            "trigger_contexts": ["面对不确定结果时"],
            "contraindicated_contexts": ["证据尚未收集时"],
            "supporting_L1_count": 1 if index == 1 else 0,
        }
        for index, node_id in enumerate(L2_IDS, 1)
    ]


def _stance(question: str, summarized_from: str) -> dict:
    value_dimensions = []
    if question == "P01":
        value_dimensions = [
            {
                "name": name,
                "relative_priority": "根据问题调整优先级",
                "explanation": "以可复核证据说明取舍",
            }
            for name in ("准确性", "一致性", "范围", "简单性", "丰产性")
        ]
    identity_context = None
    if question == "P03":
        identity_context = {
            "scientist_name": "Test Scientist",
            "aliases": [],
            "occupations": ["researcher"],
            "research_fields": ["testing"],
            "education_history": [],
            "employment_history": [],
            "institutions": ["Offline Institute"],
            "sources": ["https://example.test/profile"],
        }
    return {
        "node_id": f"l3_test_scientist_{question}",
        "level": "L3",
        "question": question,
        "question_label": f"研究立场 {question[-1]}",
        "stance": "优先提出可证伪并可复核的判断",
        "explanation": "从明确观察出发，说明结论适用的证据边界。",
        "considered_L2": L2_IDS,
        "summarized_from_L2": [summarized_from],
        "exemplar_L1": [L1_ID],
        "value_dimensions": value_dimensions,
        "identity_context": identity_context,
        "human_review_required": False,
    }


def _write_inputs(project: Path) -> None:
    write_json(
        project / "scientist-corpus" / SCIENTIST_ID / "profile.json",
        {"scientist_name": "Test Scientist"},
    )
    write_jsonl(
        project / "evidence_cards" / f"{SCIENTIST_ID}.jsonl",
        [
            {
                "schema_version": "1.0.0",
                "card_id": L1_ID,
                "source_id": "src_0001",
                "source_title": "Offline Evidence",
                "source_type": "paper",
                "year": 2025,
                "excerpt": "A reproducible observation supports the scientific judgment.",
                "location": {
                    "section": "Results",
                    "start_char": 0,
                    "end_char": 58,
                },
                "observation": "The scientist checks a reproducible observation first.",
                "fact_type": "practical_habit",
                "author_role": "first",
            }
        ],
    )
    write_json(
        project / "l2" / f"{SCIENTIST_ID}_assignments.json",
        [{"card_id": L1_ID, "category": "C01"}],
    )
    write_json(project / "l2" / f"{SCIENTIST_ID}_l2.json", _l2_nodes())
    write_json(
        project / "l3" / f"{SCIENTIST_ID}_l3.json",
        [
            _stance("P01", L2_IDS[0]),
            _stance("P02", L2_IDS[1]),
            _stance("P03", L2_IDS[2]),
            {
                "node_id": "l3_test_scientist_P04",
                "level": "L3",
                "question": "P04",
                "question_label": "语气",
                "tone_exemplars": [
                    "First verify the observation.",
                    "Then isolate the decisive variable.",
                    "Finally state the evidence boundary.",
                ],
            },
        ],
    )
    write_json(
        project / "edges" / f"{SCIENTIST_ID}_edges.json",
        {"reinforces": [], "enables": [], "tension": []},
    )


class KGEncoderTest(unittest.TestCase):
    def test_encode_builds_and_validates_a_complete_graph(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="KG 编码(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            _write_inputs(project)

            output = encode_kg(project, SCIENTIST_ID)
            graph = read_json(output)

            self.assertEqual(output, project / "scientist-kg" / f"{SCIENTIST_ID}.kg.json")
            self.assertEqual(graph["meta"]["scientist_name"], "Test Scientist")
            self.assertEqual(graph["meta"]["total_L1"], 1)
            self.assertEqual(len(graph["L2_patterns"]), 7)
            self.assertEqual(len(graph["L3_stances"]), 4)
            self.assertEqual(
                graph["edges"]["supports"],
                [{"from": L2_IDS[0], "to": L1_ID}],
            )
            validate_kg(graph)

    def test_validation_rejects_a_dangling_edge(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="KG 校验(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            _write_inputs(project)
            graph = read_json(encode_kg(project, SCIENTIST_ID))
            invalid = copy.deepcopy(graph)
            invalid["edges"]["reinforces"].append(
                {"from": L2_IDS[0], "to": "l2_unknown_C02", "reason": "invalid"}
            )

            with self.assertRaisesRegex(ValueError, "unknown node"):
                validate_kg(invalid)

    def test_distill_kg_atomically_installs_into_soulagent_scanner_root(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="人格 安装(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            _write_inputs(project)
            scanner_root = project / "SoulAgent 扫描目录(测试)"

            outputs = kg_main.distill(
                project,
                SCIENTIST_ID,
                selected_step="kg",
                result_root=project / "result",
                install_root=scanner_root,
            )

            installed = scanner_root / SCIENTIST_ID
            self.assertEqual(outputs[-1], installed / "manifest.json")
            self.assertTrue((installed / "identity.json").is_file())
            self.assertFalse((installed / "kg").exists())
            self.assertFalse((installed / "kg.json").exists())
            validate_kg_store(installed, require_bundle=False)
            loader_path = SKILL_DIR.parent / "soulagent" / "kg_loader.py"
            spec = importlib.util.spec_from_file_location(
                "soulagent_kg_loader_handoff_test", loader_path
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader if spec else None)
            loader = importlib.util.module_from_spec(spec)
            assert spec is not None and spec.loader is not None
            spec.loader.exec_module(loader)
            self.assertEqual(loader.load_kg(installed)["scientist_id"], SCIENTIST_ID)
            self.assertFalse((scanner_root / f".install-{SCIENTIST_ID}.lock").exists())
            self.assertEqual(
                list(scanner_root.parent.glob(f".scientist-kg-distill-{SCIENTIST_ID}-*")),
                [],
            )

            with self.assertRaisesRegex(KGInstallError, "refusing overwrite"):
                install_kg_store(
                    project / "result" / SCIENTIST_ID / "kg",
                    scanner_root,
                    SCIENTIST_ID,
                )

            source_store = project / "result" / SCIENTIST_ID / "kg"
            (source_store / "undeclared.tmp").write_text(
                "must not be published", encoding="utf-8"
            )
            second_root = project / "second scanner"
            install_kg_store(source_store, second_root, SCIENTIST_ID)
            self.assertFalse(
                (second_root / SCIENTIST_ID / "undeclared.tmp").exists()
            )

    def test_install_rejects_tampering_without_exposing_partial_persona(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="人格 篡改(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            _write_inputs(project)
            kg_main.distill(
                project,
                SCIENTIST_ID,
                selected_step="kg",
                result_root=project / "result",
            )
            store = project / "result" / SCIENTIST_ID / "kg"
            (store / "identity.json").write_text("{}", encoding="utf-8")
            scanner_root = project / "scanner"

            with self.assertRaisesRegex(KGInstallError, "validation failed"):
                install_kg_store(store, scanner_root, SCIENTIST_ID)

            self.assertFalse((scanner_root / SCIENTIST_ID).exists())


if __name__ == "__main__":
    unittest.main()
