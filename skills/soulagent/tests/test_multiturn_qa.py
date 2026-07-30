from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SKILL_DIR = Path(__file__).resolve().parents[1]
EXAMPLE_KG_ROOT = SKILL_DIR / "assets" / "builtin-scientist-kg"


def _engine_module():
    path = SKILL_DIR / "engine.py"
    spec = importlib.util.spec_from_file_location("soulagent_multiturn_engine", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = []


class _ConversationLLM:
    def __init__(self) -> None:
        self.sensor_requests: list[str] = []
        self.decoder_requests: list[tuple[str, str]] = []

    async def chat_with_tools(self, messages, tools, **kwargs):
        system = str(messages[0]["content"])
        user = str(messages[1]["content"])
        if "scientific task sensor" in system:
            self.sensor_requests.append(user)
            lowered = user.casefold()
            if "readme" in lowered:
                return _Result(
                    json.dumps(
                        {
                            "is_scientific": False,
                            "phase": "general",
                            "constraints": {
                                "compute_constraint": False,
                                "time_pressure": False,
                            },
                        }
                    )
                )
            if "资源充足" in user or "不着急" in user:
                phase = "general"
                compute = False
                time_pressure = False
            elif "单卡" in user or "8小时" in user:
                phase = "general"
                compute = True
                time_pressure = True
            elif any(value in user for value in ("消融", "对照组", "实验矩阵")):
                phase = "experiment_design"
                compute = False
                time_pressure = False
            else:
                phase = "failure_diagnosis"
                compute = False
                time_pressure = False
            return _Result(
                json.dumps(
                    {
                        "is_scientific": True,
                        "phase": phase,
                        "constraints": {
                            "compute_constraint": compute,
                            "time_pressure": time_pressure,
                        },
                    }
                )
            )

        self.decoder_requests.append((system, user))
        return _Result(
            """## 当前人格：多轮 QA 科学家
### 表达语气
模型生成的占位语气，必须被 Core 替换。
### 核心原则
模型生成的占位原则，必须被 Core 替换。
### 当前任务中的思考方式
- 先建立可证伪的最小基线，再隔离变量。
### 当前取舍
当前没有触发需要消解的取舍。
### 证据来源
- 多轮 QA：使用确定性测试替身。
"""
        )


def _tone_exemplars(scientist_id: str) -> list[str]:
    nodes = json.loads(
        (EXAMPLE_KG_ROOT / scientist_id / "l3-stances.json").read_text(
            encoding="utf-8"
        )
    )
    return next(node["tone_exemplars"] for node in nodes if node["question"] == "P04")


class MultiTurnQATest(unittest.IsolatedAsyncioTestCase):
    async def test_complete_scientist_persona_conversation(self) -> None:
        module = _engine_module()
        with tempfile.TemporaryDirectory(
            prefix="多轮 QA 项目(端到端)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            shutil.copytree(EXAMPLE_KG_ROOT, project / "scientist-kg")
            original_role = "原始 OmniScientist 人格。\n"
            (project / "role.md").write_text(original_role, encoding="utf-8")
            llm = _ConversationLLM()
            engine = module.SoulAgentEngine()
            engine.ctx = SimpleNamespace(
                llm=llm,
                working_dir=project,
                paths=SimpleNamespace(workspace_root=project),
            )

            listed = await engine.execute(input="有哪些科学家？", action="list")
            self.assertEqual(listed["outcome"]["code"], "listed")
            expected_scientist_ids = {
                path.name for path in EXAMPLE_KG_ROOT.iterdir() if path.is_dir()
            }
            self.assertEqual(
                {row["scientist_id"] for row in listed["scientists"]},
                expected_scientist_ids,
            )
            self.assertEqual(len(llm.decoder_requests), 0)

            first = await engine.execute(
                input="用何恺明的方式诊断模型在 COCO 上比 baseline 低 2 AP 的原因"
            )
            self.assertEqual(first["outcome"]["code"], "refreshed")
            self.assertEqual(first["task_frame"]["phase"], "failure_diagnosis")
            self.assertTrue(
                all(value in first["persona_text"] for value in _tone_exemplars("kaiming-he"))
            )
            self.assertEqual(len(llm.decoder_requests), 1)

            same_failure = await engine.execute(
                input="继续刚才同一个 COCO 掉点问题，先检查评估配置。"
            )
            self.assertEqual(same_failure["outcome"]["code"], "unchanged_task")
            self.assertEqual(len(llm.decoder_requests), 1)

            changed_phase = await engine.execute(
                input="失败原因已经定位，接下来为同一模型设计 baseline、对照组和消融实验。"
            )
            self.assertEqual(changed_phase["outcome"]["code"], "refreshed")
            self.assertEqual(changed_phase["task_frame"]["phase"], "experiment_design")
            self.assertEqual(len(llm.decoder_requests), 2)

            same_experiment = await engine.execute(
                input="这个消融实验中对照组怎么设置比较好？"
            )
            self.assertEqual(same_experiment["outcome"]["code"], "unchanged_task")
            self.assertEqual(len(llm.decoder_requests), 2)

            constrained = await engine.execute(
                input="实验现在只能使用单卡 GPU，并且必须在 8 小时内完成，其他目标不变。"
            )
            self.assertEqual(constrained["outcome"]["code"], "refreshed")
            self.assertEqual(constrained["task_frame"]["phase"], "experiment_design")
            self.assertEqual(
                constrained["task_frame"]["constraints"],
                {"compute_constraint": True, "time_pressure": True},
            )
            self.assertEqual(len(llm.decoder_requests), 3)

            keep_constraints = await engine.execute(
                input="继续刚才的消融实验，先列出最小实验矩阵。"
            )
            self.assertEqual(keep_constraints["outcome"]["code"], "unchanged_task")
            self.assertEqual(
                keep_constraints["task_frame"]["constraints"],
                {"compute_constraint": True, "time_pressure": True},
            )
            self.assertEqual(len(llm.decoder_requests), 3)

            relaxed = await engine.execute(
                input="现在 GPU 资源充足，也不着急，仍然是同一个消融实验。"
            )
            self.assertEqual(relaxed["outcome"]["code"], "refreshed")
            self.assertEqual(relaxed["task_frame"]["phase"], "experiment_design")
            self.assertEqual(
                relaxed["task_frame"]["constraints"],
                {"compute_constraint": False, "time_pressure": False},
            )
            role_before_non_science = (project / "role.md").read_text(encoding="utf-8")
            decoder_count = len(llm.decoder_requests)

            non_science = await engine.execute(input="帮我把 README 标题改短")
            self.assertEqual(non_science["outcome"]["code"], "no_scientific_task")
            self.assertEqual(len(llm.decoder_requests), decoder_count)
            self.assertEqual(
                (project / "role.md").read_text(encoding="utf-8"),
                role_before_non_science,
            )

            active = await engine.execute(input="当前人格", action="status")
            self.assertTrue(active["active"])
            self.assertEqual(active["scientist_id"], "kaiming-he")
            self.assertEqual(len(llm.decoder_requests), decoder_count)

            switched = await engine.execute(
                input="切换为徐丰力，继续同一个消融实验。"
            )
            self.assertEqual(switched["outcome"]["code"], "refreshed")
            self.assertEqual(switched["scientist_id"], "fengli-xu")
            self.assertTrue(
                all(value in switched["persona_text"] for value in _tone_exemplars("fengli-xu"))
            )
            self.assertTrue(
                all(value not in switched["persona_text"] for value in _tone_exemplars("kaiming-he"))
            )

            for system, user in llm.decoder_requests:
                wire_text = system + "\n" + user
                for scientist_id in ("kaiming-he", "fengli-xu"):
                    self.assertTrue(
                        all(value not in wire_text for value in _tone_exemplars(scientist_id))
                    )

            unloaded = await engine.execute(input="恢复你自己")
            self.assertEqual(unloaded["outcome"]["code"], "unloaded")
            self.assertEqual((project / "role.md").read_text(encoding="utf-8"), original_role)
            self.assertFalse((project / ".soulagent" / "state.json").exists())

            again = await engine.execute(input="恢复你自己")
            self.assertEqual(again["outcome"]["code"], "already_inactive")
            self.assertEqual((project / "role.md").read_text(encoding="utf-8"), original_role)
