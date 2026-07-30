"""A turn that waits should not describe work it could have simply done.

Task aac5b285 asked for an abstract, a diagram and a paper. The model dispatched
the diagram with mode="background", polled three times, saw "pending" every time,
and finished by reporting the diagram as still generating — while the drain that
runs after the loop produced the finished figure whose artifacts are listed
directly beneath that sentence.

Nothing was slow. Background work does not run alongside the turn; it is deferred
to the end-of-turn drain, so on a turn that drains, the status the model waits on
cannot change until the model has stopped asking.
"""

from __future__ import annotations

import pytest

from omni.agent.interaction_lifecycle import enqueue_notify_channel, resolve_execution_mode

# ── on a turn that waits, a deferral the model cannot observe is not honoured ──


def test_asking_to_defer_on_a_waiting_turn_runs_the_skill_instead() -> None:
    assert (
        resolve_execution_mode("background", wait_for_tasks=True, is_async=True)
        == "foreground"
    )


def test_the_same_holds_for_a_skill_that_is_not_async() -> None:
    assert (
        resolve_execution_mode("background", wait_for_tasks=False, is_async=False)
        == "background"
    ), "with nobody waiting the submission genuinely outlives the turn"


def test_a_detached_turn_still_gets_a_real_background_submission() -> None:
    """Daemon and IM turns do not drain, so deferring is what was asked for."""
    assert (
        resolve_execution_mode("background", wait_for_tasks=False, is_async=True)
        == "background"
    )


# ── every other mode is untouched ──


@pytest.mark.parametrize("wait", [True, False])
def test_inline_is_unchanged(wait: bool) -> None:
    assert resolve_execution_mode("inline", wait_for_tasks=wait, is_async=True) == "inline"


def test_foreground_still_waits_on_a_waiting_turn() -> None:
    assert (
        resolve_execution_mode("foreground", wait_for_tasks=True, is_async=True)
        == "foreground"
    )


def test_asking_to_wait_on_a_detached_turn_detaches_instead() -> None:
    """IM / daemon: the work outlives the turn. Waiting holds the send lock.

    Task 4e86a6da asked for scientific-figure in foreground after livefigure
    failed. process() then waited for hop 2, which waited for the same WeChat
    outbound lock the inbound turn still held.
    """
    assert (
        resolve_execution_mode("foreground", wait_for_tasks=False, is_async=True)
        == "background"
    )
    assert (
        resolve_execution_mode("foreground", wait_for_tasks=False, is_async=False)
        == "background"
    )


def test_auto_still_waits_on_a_waiting_turn() -> None:
    assert resolve_execution_mode("auto", wait_for_tasks=True, is_async=True) == "foreground"


def test_auto_still_detaches_when_nothing_waits() -> None:
    assert resolve_execution_mode("auto", wait_for_tasks=False, is_async=True) == "background"


def test_auto_runs_a_quick_skill_in_the_turn() -> None:
    assert resolve_execution_mode("auto", wait_for_tasks=True, is_async=False) == "inline"


def test_an_unrecognised_mode_still_falls_back_to_auto() -> None:
    assert resolve_execution_mode("sideways", wait_for_tasks=True, is_async=True) == "foreground"


def test_im_foreground_that_does_not_wait_still_notifies() -> None:
    assert (
        enqueue_notify_channel("wechat", mode="foreground", wait_for_tasks=False) == "wechat"
    )


def test_background_always_notifies() -> None:
    assert enqueue_notify_channel("wechat", mode="background", wait_for_tasks=False) == "wechat"
    assert enqueue_notify_channel("cli", mode="background", wait_for_tasks=True) == "cli"


def test_cli_foreground_that_waits_does_not_enqueue_a_second_hop() -> None:
    assert enqueue_notify_channel("cli", mode="foreground", wait_for_tasks=True) == ""
    assert enqueue_notify_channel("wechat", mode="foreground", wait_for_tasks=True) == ""


@pytest.mark.asyncio
async def test_im_run_skill_foreground_does_not_await_process(tmp_path) -> None:
    """WeChat inbound holds the outbound lock for the whole ReAct turn.

    Awaiting process() on that turn makes hop 2 wait for the same lock.
    Detach, keep notify=wechat, and let the worker send after hop 1.
    """
    from types import SimpleNamespace
    from typing import Any

    from omni.agent.tool_surface import ToolSurfaceBuilder
    from omni.config import load_settings
    from omni.skills_runtime.context import ExecContext
    from omni.skills_runtime.manifest import DeliveryMode, SkillEntry, SkillKind

    class Runtime:
        def __init__(self) -> None:
            self.enqueued: list[dict[str, Any]] = []
            self.process_calls = 0

        async def enqueue(self, skill_name: str, params: dict[str, Any], notify_channel: str, **kwargs: Any) -> str:
            self.enqueued.append(
                {"skill_name": skill_name, "params": params, "notify_channel": notify_channel, **kwargs}
            )
            return "9c44a913"

        async def process(self, _subtask_id: str, **_kwargs: Any) -> None:
            self.process_calls += 1

        async def get_subtask(self, _subtask_id: str) -> Any:
            raise AssertionError("detached IM skill must not wait for the child")

    entry = SkillEntry(
        name="scientific-figure",
        description="figure",
        kind=SkillKind.PYTHON_ENGINE,
        delivery_mode=DeliveryMode.ASYNC_TASK,
    )
    settings = load_settings(cwd=tmp_path)
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        task_id="4e86a6da",
        session_id="sess",
        channel="wechat",
    )
    runtime = Runtime()
    tool = ToolSurfaceBuilder(
        runtime=runtime,
        tasks=SimpleNamespace(),
        registry=SimpleNamespace(
            list_sync_tools=lambda: [],
            list_selectable=lambda: [entry],
            get=lambda name: entry if name == entry.name else None,
        ),
        mcp_loader=lambda _ctx: [],
    )._run_skill(ctx, wait_for_tasks=False, on_tool_event=None)

    result = await tool.handler(
        {"skill_name": "scientific-figure", "mode": "foreground", "input": "RAG architecture"}
    )

    assert result["status"] == "submitted"
    assert result["mode"] == "background"
    assert result["subtask_id"] == "9c44a913"
    assert runtime.process_calls == 0
    assert runtime.enqueued[0]["notify_channel"] == "wechat"


@pytest.mark.asyncio
async def test_im_run_workflow_foreground_does_not_await_process(tmp_path) -> None:
    from types import SimpleNamespace
    from typing import Any

    from omni.agent.tool_surface import ToolSurfaceBuilder
    from omni.config import load_settings
    from omni.skills_runtime.context import ExecContext

    class Runtime:
        def __init__(self) -> None:
            self.process_calls = 0
            self.notify_channel = ""

        async def enqueue_workflow(self, _goal: str, _steps: list, notify_channel: str, **_kwargs: Any) -> str:
            self.notify_channel = notify_channel
            return "wf-1"

        async def process(self, _workflow_run_id: str, **_kwargs: Any) -> None:
            self.process_calls += 1

    settings = load_settings(cwd=tmp_path)
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        task_id="4e86a6da",
        session_id="sess",
        channel="wechat",
    )
    runtime = Runtime()
    tool = ToolSurfaceBuilder(
        runtime=runtime,
        tasks=SimpleNamespace(),
        registry=SimpleNamespace(),
        mcp_loader=lambda _ctx: [],
    )._run_workflow(ctx, wait_for_tasks=False, on_tool_event=None)

    result = await tool.handler(
        {
            "goal": "figure then slides",
            "mode": "foreground",
            "steps": [{"id": "figure", "skill": "scientific-figure", "input": {}}],
        }
    )

    assert result["status"] == "submitted"
    assert result["mode"] == "background"
    assert runtime.process_calls == 0
    assert runtime.notify_channel == "wechat"
