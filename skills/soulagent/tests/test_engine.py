from __future__ import annotations

import asyncio
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[1]
REQUIRED_CONTRACT_FIELDS = {
    "loaded",
    "missing_kg",
    "invalid_kg",
    "needs_input",
    "active_scientist_id",
}


def _assert_engine_contract(result: dict) -> None:
    assert REQUIRED_CONTRACT_FIELDS <= result.keys()
    if not result["loaded"]:
        assert result["status"] not in {"ok", "succeeded", "refreshed"}


def _engine_module():
    path = SKILL_DIR / "engine.py"
    spec = importlib.util.spec_from_file_location("soulagent_engine_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = []


class _FakeLLM:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def chat_with_tools(self, messages, tools, **kwargs):
        self.requests.append({"messages": messages, "tools": tools, **kwargs})
        system = str(messages[0]["content"])
        if "scientific task sensor" in system:
            return _Result(
                '{"is_scientific":true,"phase":"failure_diagnosis",'
                '"constraints":{"compute_constraint":false,"time_pressure":false}}'
            )
        return _Result(
            """## 当前人格：Fengli Xu
### 表达语气
- 面对结果异常时，先指出最反常的事实，再给出可检验的解释。
- 在给出结论之前，先区分观测与推断，再说明证据边界。
### 核心原则
由程序替换。
### 当前任务中的思考方式
- 先复现 AP 下降，再逐项隔离数据、评估和训练变量。
### 当前取舍
当前没有触发需要消解的取舍。
### 证据来源
- 测试证据：采用可证伪的诊断顺序。
"""
        )


class _BlockingLLM:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def chat_with_tools(self, messages, tools, **kwargs):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class SoulAgentEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_host_completion_uses_stage_specific_token_limits(self) -> None:
        module = _engine_module()
        llm = _FakeLLM()
        loop = asyncio.get_running_loop()
        sensor = module._OmniCompletion(
            llm,
            loop,
            stage_label="科研任务识别",
            max_tokens=module.TASK_SENSOR_MAX_TOKENS,
            timeout_seconds=1,
        )
        decoder = module._OmniCompletion(
            llm,
            loop,
            stage_label="任务化人格生成",
            max_tokens=module.PERSONA_DECODER_MAX_TOKENS,
            timeout_seconds=1,
        )

        await asyncio.to_thread(sensor, "scientific task sensor", "task")
        await asyncio.to_thread(decoder, "KG解码器", "subgraph")

        self.assertEqual(
            [request["max_tokens"] for request in llm.requests],
            [512, 8192],
        )

    async def test_host_completion_timeout_cancels_the_model_request(self) -> None:
        module = _engine_module()
        llm = _BlockingLLM()
        completion = module._OmniCompletion(
            llm,
            asyncio.get_running_loop(),
            stage_label="科研任务识别",
            max_tokens=module.TASK_SENSOR_MAX_TOKENS,
            timeout_seconds=0.05,
        )

        with self.assertRaisesRegex(RuntimeError, "科研任务识别超时"):
            await asyncio.to_thread(completion, "sensor", "task")
        await asyncio.wait_for(llm.cancelled.wait(), timeout=1)

    async def test_host_completion_keeps_nonempty_truncated_text(self) -> None:
        module = _engine_module()

        class _TruncatedLLM:
            async def chat_with_tools(self, messages, tools, **kwargs):
                result = _Result("partial persona")
                result.finish_reason = "length"
                return result

        completion = module._OmniCompletion(
            _TruncatedLLM(),
            asyncio.get_running_loop(),
            stage_label="任务化人格生成",
            max_tokens=module.PERSONA_DECODER_MAX_TOKENS,
            timeout_seconds=1,
        )
        text = await asyncio.to_thread(completion, "decoder", "subgraph")
        self.assertEqual(text, "partial persona")

    async def test_host_completion_rejects_empty_truncated_results(self) -> None:
        module = _engine_module()

        class _EmptyTruncatedLLM:
            async def chat_with_tools(self, messages, tools, **kwargs):
                result = _Result("")
                result.finish_reason = "length"
                return result

        completion = module._OmniCompletion(
            _EmptyTruncatedLLM(),
            asyncio.get_running_loop(),
            stage_label="任务化人格生成",
            max_tokens=module.PERSONA_DECODER_MAX_TOKENS,
            timeout_seconds=1,
        )
        with self.assertRaisesRegex(RuntimeError, "未得到可用文本"):
            await asyncio.to_thread(completion, "decoder", "subgraph")

    def test_who_am_i_questions_are_status_and_restore_is_unload(self) -> None:
        module = _engine_module()
        cases = {
            "人格状态": "status",
            "当前人格": "status",
            "你现在是谁的人格？": "status",
            "你当前是谁的人格?": "status",
            "whose persona is this": "status",
            "restore yourself": "unload",
            "think like Fengli Xu": "activate",
        }
        for request, expected in cases.items():
            with self.subTest(request=request):
                self.assertEqual(module._infer_action(request, None), expected)

    def test_explicit_scientist_name_requires_a_named_persona(self) -> None:
        module = _engine_module()
        cases = {
            "用 Yann LeCun 的方式": "Yann LeCun",
            "换 LeCun": "LeCun",
            "think like Richard Feynman": "Richard Feynman",
            "换一个人格": None,
            "实验现在只能使用单卡 GPU，并且必须在 8 小时内完成": None,
            "use a single GPU, then finish the experiment": None,
        }

        for request, expected in cases.items():
            with self.subTest(request=request):
                self.assertEqual(
                    module._explicit_scientist_name(request, None),
                    expected,
                )

    async def test_named_user_request_downloads_into_the_active_scanner_root(self) -> None:
        module = _engine_module()
        with tempfile.TemporaryDirectory(
            prefix="用户触发下载 中文(测试)-", dir=Path(__file__).parent
        ) as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            omni_home = root / ".omni"
            expected_kg_root = omni_home / "scientist-kg"
            engine = module.SoulAgentEngine()
            engine.ctx = SimpleNamespace(
                llm=_FakeLLM(),
                working_dir=project,
                paths=SimpleNamespace(workspace_root=project, home=omni_home),
            )

            def install(kg_root, request, *, registry_url):
                self.assertEqual(Path(kg_root), expected_kg_root)
                self.assertEqual(request, "Kaiming He")
                self.assertEqual(registry_url, module._core.DEFAULT_REGISTRY_URL)
                target = Path(kg_root) / "kaiming-he"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(
                    SKILL_DIR / "assets" / "builtin-scientist-kg" / "kaiming-he",
                    target,
                )
                kg = module._core.load_kg(target)
                return {
                    "downloaded": True,
                    "scientist_id": "kaiming-he",
                    "scientist_name": "Kaiming He",
                    "kg_path": str(target),
                    "registry_url": registry_url,
                    "manifest_sha256": kg["manifest_sha256"],
                }

            with mock.patch.object(
                module._core,
                "resolve_and_install_remote_scientist",
                side_effect=install,
            ) as remote_install:
                result = await engine.execute(
                    input="用 Kaiming He 的方式设计一个消融实验"
                )

            remote_install.assert_called_once()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["outcome"]["code"], "refreshed")
            self.assertEqual(result["scientist_id"], "kaiming-he")
            self.assertTrue(result["loaded"])
            self.assertTrue(result["remote_install"]["downloaded"])
            self.assertTrue((expected_kg_root / "kaiming-he").is_dir())
            self.assertTrue((project / "role.md").is_file())
            _assert_engine_contract(result)

    async def test_lifecycle_actions_validation_and_recoverable_errors(self) -> None:
        module = _engine_module()
        self.assertEqual(
            module.SoulAgentEngine.validate_params(arguments={}),
            {"error": "input is required"},
        )
        self.assertEqual(
            module.SoulAgentEngine.validate_params(
                arguments={"input": "x", "action": "unknown"}
            ),
            {"error": "unsupported action: unknown"},
        )

        with tempfile.TemporaryDirectory(
            prefix="生命周期 中文(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            shutil.copytree(
                SKILL_DIR / "assets" / "builtin-scientist-kg",
                project / "scientist-kg",
            )
            engine = module.SoulAgentEngine()
            engine.ctx = SimpleNamespace(
                llm=_FakeLLM(),
                working_dir=project,
                paths=SimpleNamespace(workspace_root=project),
            )

            listed = await engine.execute(input="有哪些科学家？", action="list")
            self.assertEqual(listed["outcome"]["code"], "listed")
            self.assertEqual(len(listed["scientists"]), 8)

            inactive = await engine.execute(input="当前人格", action="status")
            self.assertFalse(inactive["active"])
            self.assertEqual(inactive["outcome"]["code"], "inactive")
            already = await engine.execute(input="不用科学家了", action="unload")
            self.assertEqual(already["outcome"]["code"], "already_inactive")

            missing = await engine.execute(input="帮我设计消融实验")
            self.assertEqual(missing["status"], "needs_input")
            self.assertEqual(missing["outcome"]["code"], "scientist_required")
            self.assertFalse(missing["loaded"])
            self.assertFalse(missing["missing_kg"])
            self.assertFalse(missing["invalid_kg"])
            self.assertTrue(missing["needs_input"])
            self.assertIsNone(missing["active_scientist_id"])
            with mock.patch.object(
                module._core,
                "resolve_and_install_remote_scientist",
                side_effect=module._core.RemoteScientistNotFound(
                    "远端人格仓库中没有找到：not-a-scientist"
                ),
            ):
                unknown = await engine.execute(
                    input="装载不存在的人格", scientist_id="not-a-scientist"
                )
            self.assertEqual(
                unknown["outcome"]["code"],
                "distillation_confirmation_required",
            )
            self.assertEqual(
                unknown["action_required"]["skill"],
                "scientist-kg-distiller",
            )
            self.assertEqual(
                unknown["action_required"]["distiller_input"]["scientist"],
                "not-a-scientist",
            )
            self.assertEqual(
                Path(unknown["action_required"]["distiller_input"]["install_root"]),
                project / "scientist-kg",
            )
            self.assertTrue(unknown["host_must_not_fabricate"])

            request = "用徐丰力的方式诊断模型在 COCO 上掉点的原因"
            progress: list[tuple[str, float]] = []

            async def report(message: str, fraction: float) -> None:
                progress.append((message, fraction))

            activated = await engine.execute(input=request, progress_callback=report)
            self.assertEqual(activated["outcome"]["code"], "refreshed")
            self.assertEqual(
                progress,
                [("SoulAgent 正在识别科研任务并生成任务化人格", 0.2)],
            )
            self.assertEqual(
                [request["max_tokens"] for request in engine.ctx.llm.requests[:2]],
                [512, 8192],
            )

            unchanged = await engine.execute(input=request)
            self.assertEqual(unchanged["outcome"]["code"], "unchanged_task")
            forced = await engine.execute(input=request, force=True)
            self.assertEqual(forced["outcome"]["code"], "refreshed")

            switched = await engine.execute(
                input="用何恺明的方式诊断模型在 COCO 上掉点的原因"
            )
            self.assertEqual(switched["scientist_id"], "kaiming-he")
            active = await engine.execute(input="当前人格", action="status")
            self.assertTrue(active["active"])
            self.assertEqual(active["scientist_id"], "kaiming-he")

            unloaded = await engine.execute(input="恢复你自己")
            self.assertEqual(unloaded["outcome"]["code"], "unloaded")
            for result in (
                listed,
                inactive,
                already,
                missing,
                unknown,
                activated,
                unchanged,
                forced,
                switched,
                active,
                unloaded,
            ):
                _assert_engine_contract(result)

        with tempfile.TemporaryDirectory(
            prefix="缺少KG 中文(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            engine = module.SoulAgentEngine()
            engine.ctx = SimpleNamespace(
                llm=_FakeLLM(),
                working_dir=project,
                paths=SimpleNamespace(workspace_root=project),
            )
            with mock.patch.object(
                module._core,
                "resolve_and_install_remote_scientist",
                side_effect=module._core.RemoteScientistNotFound(
                    "远端人格仓库中没有找到：何恺明"
                ),
            ):
                missing_kg = await engine.execute(
                    input="用何恺明的方式设计实验",
                    kg_root=project / "scientist-kg",
                )
            self.assertEqual(
                missing_kg["outcome"]["code"],
                "distillation_confirmation_required",
            )
            self.assertEqual(
                missing_kg["action_required"]["action"],
                "confirm_scientist_distillation",
            )
            _assert_engine_contract(missing_kg)

    async def test_chinese_alias_loads_and_unloads_in_unicode_project(self) -> None:
        module = _engine_module()
        with tempfile.TemporaryDirectory(
            prefix="中文 项目(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            shutil.copytree(
                SKILL_DIR / "assets" / "builtin-scientist-kg",
                project / "scientist-kg",
            )
            engine = module.SoulAgentEngine()
            engine.ctx = SimpleNamespace(
                llm=_FakeLLM(),
                working_dir=project,
                paths=SimpleNamespace(workspace_root=project),
            )

            result = await engine.execute(
                input="用徐丰力的方式，帮我诊断模型在 COCO 上掉了 2 个 AP 的原因"
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["scientist_id"], "fengli-xu")
            self.assertEqual(result["outcome"]["code"], "refreshed")
            self.assertTrue((project / "role.md").is_file())
            self.assertTrue((project / ".soulagent" / "state.json").is_file())
            self.assertIn("当前人格：Fengli Xu", result["persona_text"])
            _assert_engine_contract(result)

            # Rollback must remain available even if the external KG is removed.
            shutil.rmtree(project / "scientist-kg")
            unloaded = await engine.execute(input="不用科学家了")
            self.assertEqual(unloaded["status"], "unloaded")
            self.assertEqual(unloaded["outcome"]["code"], "unloaded")
            self.assertFalse(unloaded["loaded"])
            self.assertFalse(unloaded["missing_kg"])
            self.assertFalse(unloaded["invalid_kg"])
            self.assertFalse(unloaded["needs_input"])
            self.assertIsNone(unloaded["active_scientist_id"])
            _assert_engine_contract(unloaded)
            self.assertFalse((project / "role.md").exists())
            self.assertFalse((project / ".soulagent" / "state.json").exists())

    def test_state_root_stays_on_working_dir_when_parent_has_scientist_kg(self) -> None:
        module = _engine_module()
        with tempfile.TemporaryDirectory(prefix="persona-root-") as temporary:
            repo = Path(temporary)
            (repo / "scientist-kg").mkdir()
            subdir = repo / "subdir"
            subdir.mkdir()
            ctx = SimpleNamespace(
                working_dir=subdir,
                paths=SimpleNamespace(workspace_root=repo, home=repo / ".omni"),
            )
            self.assertEqual(module._resolve_project_root(ctx, None), subdir.resolve())
            self.assertEqual(
                module._resolve_project_root(ctx, str(subdir)),
                subdir.resolve(),
            )
            self.assertEqual(
                module._resolve_kg_root(ctx, subdir.resolve(), None),
                (repo / ".omni" / "scientist-kg").resolve(),
            )

    async def test_status_and_unload_do_not_follow_parent_persona_state(self) -> None:
        module = _engine_module()
        with tempfile.TemporaryDirectory(prefix="persona-isolate-") as temporary:
            repo = Path(temporary)
            (repo / "scientist-kg").mkdir()
            lock = repo / ".soulagent" / "lock"
            lock.mkdir(parents=True)
            (lock / "ready").touch()
            (repo / ".soulagent" / "state.json").write_text(
                '{"host":"omniscientist","scientist_id":"fengli-xu","scientist_name":"Fengli Xu"}',
                encoding="utf-8",
            )
            (repo / "role.md").write_text("parent persona", encoding="utf-8")
            subdir = repo / "subdir"
            subdir.mkdir()
            engine = module.SoulAgentEngine()
            engine.ctx = SimpleNamespace(
                llm=_FakeLLM(),
                working_dir=subdir,
                paths=SimpleNamespace(workspace_root=repo, home=repo / ".omni"),
            )

            status = await engine.execute(input="当前人格", action="status")
            self.assertEqual(Path(status["project_root"]), subdir.resolve())
            self.assertFalse(status["active"])
            self.assertEqual(status["outcome"]["code"], "inactive")

            unloaded = await engine.execute(input="恢复你自己", action="unload")
            self.assertEqual(Path(unloaded["project_root"]), subdir.resolve())
            self.assertEqual(unloaded["outcome"]["code"], "already_inactive")
            self.assertTrue((repo / ".soulagent" / "state.json").is_file())
            self.assertEqual(
                (repo / "role.md").read_text(encoding="utf-8"),
                "parent persona",
            )
            _assert_engine_contract(status)
            _assert_engine_contract(unloaded)


if __name__ == "__main__":
    unittest.main()
