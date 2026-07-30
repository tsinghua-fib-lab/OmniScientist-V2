"""BUG-07 / BUG-08: engine usage is metered, and huge skill dumps are not re-billed."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from omni.agent.cost import format_tokens, react_usage_limits, summarize_cost_events
from omni.agent.tool_surface import _inline_usage_progress
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult
from omni.core.observation import compact_observation
from omni.core.react_agent import ReActLoopAgent, ToolCall, ToolSpec
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.executor import execute_skill
from omni.skills_runtime.manifest import EngineSpec, SkillEntry, SkillKind
from tests.conftest import ScriptedLLM


class _RecordingTasks:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append_event(self, task_id: str, **kwargs: Any) -> None:
        self.events.append({"task_id": task_id, **kwargs})


def _ctx(**kwargs: Any) -> ExecContext:
    settings = load_settings()
    settings.paths.ensure_dirs()
    return ExecContext(settings=settings, paths=settings.paths, **kwargs)


@pytest.mark.asyncio
async def test_python_engine_records_provider_usage_as_cost_event(tmp_path) -> None:
    """BUG-07: a real engine LLM call must leave a ``cost.usage`` event."""
    mod = tmp_path / "metered_engine.py"
    mod.write_text(
        "class Echo:\n"
        "    async def execute(self, **kw):\n"
        "        result = await self.ctx.llm.chat_with_tools(\n"
        "            [{'role': 'user', 'content': 'ideate'}], [],\n"
        "        )\n"
        "        return {'status': 'ok', 'text': result.content}\n"
    )
    sys.path.insert(0, str(tmp_path))
    tasks = _RecordingTasks()
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                content="three ideas",
                usage={"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
            )
        ]
    )
    try:
        entry = SkillEntry(
            name="research-ideation",
            description="d",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="metered_engine", class_name="Echo", method="execute"),
        )
        out = await execute_skill(
            entry,
            {},
            _ctx(llm=llm, task_id="task-engine", task_recorder=tasks),
        )
    finally:
        sys.path.remove(str(tmp_path))

    assert out["status"] == "ok"
    assert out["usage"]["total_tokens"] == 160
    costs = [event for event in tasks.events if event["event_type"] == "cost.usage"]
    assert costs, "engine LLM calls must record cost.usage"
    payload = costs[-1]["output_json"]
    assert payload["component"] == "engine:research-ideation"
    assert payload["total_tokens"] == 160
    assert payload["prompt_tokens"] == 120
    assert payload["completion_tokens"] == 40
    assert payload["estimated"] is False
    assert payload["calls"] == 1


@pytest.mark.asyncio
async def test_python_engine_chat_path_still_records_usage(tmp_path) -> None:
    """research-pptx calls ``llm.chat``; the host wrapper must not drop usage."""
    mod = tmp_path / "chat_engine.py"
    mod.write_text(
        "class Echo:\n"
        "    async def execute(self, **kw):\n"
        "        text = await self.ctx.llm.chat('sys', 'draft slides')\n"
        "        return {'status': 'ok', 'text': text}\n"
    )
    sys.path.insert(0, str(tmp_path))
    tasks = _RecordingTasks()

    class _ChatLLM:
        model = "gpt-4o-mini"

        async def chat_result(self, system: str, user: str, *, temperature: float = 0.3):
            return ChatWithToolsResult(
                content="deck outline",
                usage={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
            )

        async def chat(self, system: str, user: str, *, temperature: float = 0.3) -> str:
            raise AssertionError("wrapper should prefer chat_result so usage is kept")

    try:
        entry = SkillEntry(
            name="research-pptx",
            description="d",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="chat_engine", class_name="Echo", method="execute"),
        )
        out = await execute_skill(
            entry,
            {},
            _ctx(llm=_ChatLLM(), task_id="task-pptx", task_recorder=tasks),
        )
    finally:
        sys.path.remove(str(tmp_path))

    assert out["text"] == "deck outline"
    payload = next(event["output_json"] for event in tasks.events if event["event_type"] == "cost.usage")
    assert payload["component"] == "engine:research-pptx"
    assert payload["total_tokens"] == 100


@pytest.mark.asyncio
async def test_python_engine_estimates_usage_when_provider_omits_it(tmp_path) -> None:
    """BUG-07 remainder: a tool-call round with no usage still leaves a cost event."""
    prompt = "ideate " + ("x" * 400)
    mod = tmp_path / "silent_engine.py"
    mod.write_text(
        "class Echo:\n"
        "    async def execute(self, **kw):\n"
        "        result = await self.ctx.llm.chat_with_tools(\n"
        "            [{'role': 'user', 'content': kw['prompt']}], [],\n"
        "        )\n"
        "        return {'status': 'ok', 'text': result.content}\n"
    )
    sys.path.insert(0, str(tmp_path))
    tasks = _RecordingTasks()

    class _SilentLLM:
        model = "probe"

        async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
            return ChatWithToolsResult(content="", usage={})

    try:
        entry = SkillEntry(
            name="research-ideation",
            description="d",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="silent_engine", class_name="Echo", method="execute"),
        )
        out = await execute_skill(
            entry,
            {"prompt": prompt},
            _ctx(llm=_SilentLLM(), task_id="task-silent", task_recorder=tasks),
        )
    finally:
        sys.path.remove(str(tmp_path))

    assert out["status"] == "ok"
    payload = next(event["output_json"] for event in tasks.events if event["event_type"] == "cost.usage")
    assert payload["component"] == "engine:research-ideation"
    assert payload["estimated"] is True
    assert payload["prompt_tokens"] >= len(prompt) // 4
    assert payload["total_tokens"] >= payload["prompt_tokens"]
    assert out["usage"]["total_tokens"] == payload["total_tokens"]


@pytest.mark.asyncio
async def test_python_engine_does_not_record_zero_token_cost_event(tmp_path) -> None:
    """Empty prompt + empty usage must not write a 0/0/0 cost.usage row."""
    mod = tmp_path / "empty_engine.py"
    mod.write_text(
        "class Echo:\n"
        "    async def execute(self, **kw):\n"
        "        await self.ctx.llm.chat_with_tools([{'role': 'user', 'content': ''}], [])\n"
        "        return {'status': 'ok'}\n"
    )
    sys.path.insert(0, str(tmp_path))
    tasks = _RecordingTasks()

    class _EmptyLLM:
        model = "probe"

        async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
            return ChatWithToolsResult(content="", usage={})

    try:
        entry = SkillEntry(
            name="research-ideation",
            description="d",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="empty_engine", class_name="Echo", method="execute"),
        )
        await execute_skill(
            entry,
            {},
            _ctx(llm=_EmptyLLM(), task_id="task-empty", task_recorder=tasks),
        )
    finally:
        sys.path.remove(str(tmp_path))

    assert not [event for event in tasks.events if event["event_type"] == "cost.usage"]


@pytest.mark.asyncio
async def test_python_engine_adds_estimate_on_top_of_provider_usage(tmp_path) -> None:
    prompt = "y" * 400
    mod = tmp_path / "mixed_engine.py"
    mod.write_text(
        "class Echo:\n"
        "    async def execute(self, **kw):\n"
        "        await self.ctx.llm.chat_with_tools([{'role': 'user', 'content': 'short'}], [])\n"
        "        await self.ctx.llm.chat_with_tools(\n"
        "            [{'role': 'user', 'content': kw['prompt']}], [],\n"
        "        )\n"
        "        return {'status': 'ok'}\n"
    )
    sys.path.insert(0, str(tmp_path))
    tasks = _RecordingTasks()

    class _MixedLLM:
        model = "probe"
        calls = 0

        async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                return ChatWithToolsResult(
                    content="ok",
                    usage={"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
                )
            return ChatWithToolsResult(content="", usage={})

    try:
        entry = SkillEntry(
            name="research-ideation",
            description="d",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="mixed_engine", class_name="Echo", method="execute"),
        )
        out = await execute_skill(
            entry,
            {"prompt": prompt},
            _ctx(llm=_MixedLLM(), task_id="task-mixed", task_recorder=tasks),
        )
    finally:
        sys.path.remove(str(tmp_path))

    payload = next(event["output_json"] for event in tasks.events if event["event_type"] == "cost.usage")
    assert payload["estimated"] is True
    assert payload["calls"] == 2
    assert payload["total_tokens"] >= 50 + len(prompt) // 4
    assert out["usage"]["total_tokens"] == payload["total_tokens"]


@pytest.mark.asyncio
async def test_python_engine_emits_throttled_usage_progress(tmp_path) -> None:
    """BUG-08 live path: the status channel sees engine tokens before the skill ends."""
    mod = tmp_path / "progress_engine.py"
    mod.write_text(
        "class Echo:\n"
        "    async def execute(self, **kw):\n"
        "        await self.ctx.llm.chat_with_tools([{'role': 'user', 'content': 'one'}], [])\n"
        "        await self.ctx.llm.chat_with_tools([{'role': 'user', 'content': 'two'}], [])\n"
        "        return {'status': 'ok'}\n"
    )
    sys.path.insert(0, str(tmp_path))
    seen: list[dict[str, Any]] = []

    async def progress(stage: str, pct: float = 0.0, **data: Any) -> None:
        if stage == "usage":
            seen.append(data)

    class _GrowingLLM:
        model = "probe"
        calls = 0

        async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                return ChatWithToolsResult(
                    content="a",
                    usage={"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
                )
            return ChatWithToolsResult(
                content="b",
                usage={"prompt_tokens": 10_000, "completion_tokens": 20, "total_tokens": 10_020},
            )

    try:
        entry = SkillEntry(
            name="research-ideation",
            description="d",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="progress_engine", class_name="Echo", method="execute"),
        )
        await execute_skill(
            entry,
            {},
            _ctx(llm=_GrowingLLM(), task_id="task-progress"),
            progress_callback=progress,
        )
    finally:
        sys.path.remove(str(tmp_path))

    assert len(seen) == 2
    assert seen[0]["total_tokens"] == 50
    assert seen[0]["calls"] == 1
    assert seen[1]["total_tokens"] == 10_070
    assert seen[1]["calls"] == 2
    assert "cost_usd" in seen[1]


@pytest.mark.asyncio
async def test_python_engine_throttles_small_usage_progress_updates(tmp_path) -> None:
    mod = tmp_path / "quiet_engine.py"
    mod.write_text(
        "class Echo:\n"
        "    async def execute(self, **kw):\n"
        "        await self.ctx.llm.chat_with_tools([{'role': 'user', 'content': 'one'}], [])\n"
        "        await self.ctx.llm.chat_with_tools([{'role': 'user', 'content': 'two'}], [])\n"
        "        return {'status': 'ok'}\n"
    )
    sys.path.insert(0, str(tmp_path))
    seen: list[int] = []

    async def progress(stage: str, pct: float = 0.0, **data: Any) -> None:
        if stage == "usage":
            seen.append(int(data.get("total_tokens") or 0))

    class _SmallLLM:
        model = "probe"

        async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
            return ChatWithToolsResult(
                content="ok",
                usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            )

    try:
        entry = SkillEntry(
            name="research-ideation",
            description="d",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="quiet_engine", class_name="Echo", method="execute"),
        )
        await execute_skill(
            entry,
            {},
            _ctx(llm=_SmallLLM(), task_id="task-quiet"),
            progress_callback=progress,
        )
    finally:
        sys.path.remove(str(tmp_path))

    assert seen == [25]


@pytest.mark.asyncio
async def test_inline_usage_progress_forwards_notice_to_turn() -> None:
    notices: list[tuple[str, dict[str, Any]]] = []

    def on_tool_event(phase: str, data: dict[str, Any]) -> None:
        notices.append((phase, data))

    progress = _inline_usage_progress(on_tool_event)
    await progress("skill.start", 0.0, skill="research-ideation")
    await progress("usage", 0.0, total_tokens=80, calls=1, estimated=False, cost_usd=0.02)
    assert notices == [
        ("notice", {"kind": "usage", "total_tokens": 80, "calls": 1, "estimated": False, "cost_usd": 0.02})
    ]


def test_compact_observation_keeps_skill_summary_and_drops_paper_bodies() -> None:
    """BUG-08: a research-ideation dump must not re-enter the transcript whole."""
    papers = [
        {
            "title": f"Paper {index}",
            "abstract": "A" * 4000,
            "year": 2024,
        }
        for index in range(40)
    ]
    payload = {
        "status": "succeeded",
        "skill_name": "research-ideation",
        "summary": "Three grounded ideas.",
        "queries": ["activation steering"],
        "n_kept": 3,
        "n_retrieved": 12,
        "report_uri": "artifact://research-ideation/report.md",
        "result": {"papers": papers, "ideas": [{"title": "Steer latent space"}]},
    }
    raw = __import__("json").dumps(payload)
    assert len(raw) > 50_000
    observation = compact_observation(payload, max_chars=8000)
    assert len(observation) <= 8000
    assert "Three grounded ideas." in observation
    assert "artifact://research-ideation/report.md" in observation
    assert "activation steering" in observation
    assert '"n_kept": 3' in observation
    assert observation.count("A" * 4000) == 0


@pytest.mark.asyncio
async def test_react_projects_a_huge_skill_result_before_the_next_call() -> None:
    huge = {"status": "ok", "papers": [{"abstract": "B" * 20_000} for _ in range(20)]}
    seen: list[str] = []

    async def invoker(_name: str, _args: dict[str, Any]) -> dict[str, Any]:
        return huge

    class _Probe(ScriptedLLM):
        async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001
            for message in messages:
                if message.get("role") == "tool":
                    seen.append(str(message.get("content") or ""))
            return await super().chat_with_tools(messages, tools, **kwargs)

    llm = _Probe(
        [
            ChatWithToolsResult(tool_calls=[ToolCall("c1", "run_skill", {"skill_name": "research-ideation"})]),
            ChatWithToolsResult(content="done"),
        ]
    )
    agent = ReActLoopAgent(
        llm,
        invoker,
        max_iterations=4,
        observation_max_chars=8000,
    )
    await agent.run(
        system_prompt="sys",
        user_message="ideate",
        tools=[ToolSpec("run_skill", "run a skill")],
    )

    assert seen, "the next model call must see the skill observation"
    assert all(len(item) <= 8000 for item in seen)
    assert all(("B" * 20_000) not in item for item in seen)


@pytest.mark.asyncio
async def test_react_usage_notice_includes_nested_engine_tokens() -> None:
    notices: list[dict[str, Any]] = []

    async def invoker(_name: str, _args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "skill_name": "research-ideation",
            "result": {"status": "ok", "usage": {"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100}},
        }

    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("c1", "run_skill", {"skill_name": "research-ideation"})],
                usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            ),
            ChatWithToolsResult(
                content="done",
                usage={"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            ),
        ]
    )
    agent = ReActLoopAgent(llm, invoker, max_iterations=4)
    result = await agent.run(
        system_prompt="sys",
        user_message="ideate",
        tools=[ToolSpec("run_skill", "run a skill")],
        on_tool_event=lambda phase, data: notices.append(data) if phase == "notice" else None,
    )

    assert result.total_usage["total_tokens"] == 35
    usage_notices = [item for item in notices if item.get("kind") == "usage"]
    assert usage_notices[-1]["total_tokens"] == 135


def test_hard_spend_caps_stay_opt_in_and_soft_warns_are_on() -> None:
    settings = load_settings()
    assert settings.cost.max_total_tokens == 0
    assert settings.cost.max_cost_usd == 0.0
    assert settings.cost.warn_total_tokens == 200_000
    assert settings.cost.warn_cost_usd == 0.50
    limits = react_usage_limits(settings, SimpleNamespace(model="gpt-4o"))
    assert limits["max_total_tokens"] == 0
    assert limits["warn_total_tokens"] == 200_000


def test_format_tokens_matches_status_line_compact_form() -> None:
    assert format_tokens(12) == "12"
    assert format_tokens(12_400) == "12.4k"
    assert format_tokens(2_197_080) == "2.2M"


def test_summarize_cost_events_ignores_non_usage_rows() -> None:
    events = [
        SimpleNamespace(event_type="tool.done", output_json={}, name="run_skill"),
        SimpleNamespace(
            event_type="cost.usage",
            name="engine:research-ideation",
            output_json={
                "component": "engine:research-ideation",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cost_usd": 0.01,
            },
        ),
    ]
    summary = summarize_cost_events(events)
    assert summary["calls"] == 1
    assert summary["total_tokens"] == 120
    assert "engine:research-ideation" in summary["components"]
