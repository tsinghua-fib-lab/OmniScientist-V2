"""Leaving the REPL is a teardown, not a last piece of work.

Ctrl+D used to spend one shared five-second budget on durable-memory
consolidation — several model round trips — and whatever it left over on the two
steps that actually matter: releasing the agent runtime and giving the terminal
back. The order and the accounting were both inverted, so the user watched a
frozen prompt print three warnings about deadlines it had already missed.
"""

from __future__ import annotations

import asyncio

import pytest


class _Recorder:
    """Collect teardown steps in the order they actually run."""

    def __init__(self) -> None:
        self.calls: list[str] = []


def _fakes(recorder: _Recorder, *, session_delay: float = 0.0, tui_delay: float = 0.0):  # noqa: ANN202
    class Agent:
        async def enqueue_session_maintenance(self, _session_id: str) -> str:
            if session_delay:
                await asyncio.sleep(session_delay)
            recorder.calls.append("session")
            return "maintenance-1"

        async def end_session(self, _session_id: str) -> list[str]:
            recorder.calls.append("consolidated")
            return []

        async def aclose(self) -> None:
            recorder.calls.append("agent")

    class Watcher:
        def stop(self) -> None:
            recorder.calls.append("watcher")

    class Tui:
        async def close(self) -> None:
            if tui_delay:
                await asyncio.sleep(tui_delay)
            recorder.calls.append("tui")

    class Guard:
        def restore(self) -> None:
            recorder.calls.append("terminal")

        def discard_pending_input(self) -> None:
            recorder.calls.append("discard")

    return Agent(), Watcher(), Tui(), Guard()


@pytest.mark.asyncio
async def test_leaving_never_waits_for_memory_consolidation() -> None:
    """Exit parks the memory work; it must not perform it."""
    from omni.cli.main import _shutdown_repl_resources

    recorder = _Recorder()
    agent, watcher, tui, guard = _fakes(recorder)

    await _shutdown_repl_resources(
        agent=agent,
        session_id="session-1",
        inbox_watcher=watcher,
        tui=tui,  # type: ignore[arg-type]
        input_guard=guard,  # type: ignore[arg-type]
    )

    assert "consolidated" not in recorder.calls, "exit ran the consolidation pass"
    assert "session" in recorder.calls, "exit forgot to park the memory work"


@pytest.mark.asyncio
async def test_the_terminal_comes_back_before_the_slow_work() -> None:
    """Whatever is slow about leaving, the prompt is not waiting behind it."""
    from omni.cli.main import _shutdown_repl_resources

    recorder = _Recorder()
    agent, watcher, tui, guard = _fakes(recorder)

    await _shutdown_repl_resources(
        agent=agent,
        session_id="session-1",
        inbox_watcher=watcher,
        tui=tui,  # type: ignore[arg-type]
        input_guard=guard,  # type: ignore[arg-type]
    )

    assert recorder.calls == ["tui", "terminal", "watcher", "session", "agent"]


@pytest.mark.asyncio
async def test_the_terminal_is_not_reset_under_a_live_terminal_ui() -> None:
    """The component that owns the terminal releases it before we reset it.

    prompt_toolkit asks the terminal where the cursor is (``ESC[6n``) after every
    print above the dock, and reads the answer itself while the tty is in raw
    mode. Resetting the tty to canonical mode first line-buffers that answer out
    of reach: the app never consumes it, the shell inherits it on exit, and the
    user's next prompt opens with ``;1R`` typed into it.
    """
    from omni.cli.main import _shutdown_repl_resources

    recorder = _Recorder()
    agent, watcher, tui, guard = _fakes(recorder)

    await _shutdown_repl_resources(
        agent=agent,
        session_id="session-1",
        inbox_watcher=watcher,
        tui=tui,  # type: ignore[arg-type]
        input_guard=guard,  # type: ignore[arg-type]
    )

    assert recorder.calls.index("tui") < recorder.calls.index("terminal")
    assert "discard" not in recorder.calls, "a clean exit must keep the user's type-ahead"


@pytest.mark.asyncio
async def test_a_wedged_terminal_ui_keeps_its_replies_off_the_shell_prompt() -> None:
    """If the UI will not let go, drop what it left in the terminal's queue.

    We reset the tty anyway — the user has to get their prompt back — but a UI
    still mid-teardown has unanswered questions queued there, and the shell we
    hand back to would read them as if the user had typed them.
    """
    from omni.cli.main import _shutdown_repl_resources

    recorder = _Recorder()
    agent, watcher, tui, guard = _fakes(recorder, tui_delay=5.0)

    await _shutdown_repl_resources(
        agent=agent,
        session_id="session-1",
        inbox_watcher=watcher,
        tui=tui,  # type: ignore[arg-type]
        input_guard=guard,  # type: ignore[arg-type]
        tui_timeout_s=0.05,
    )

    assert "tui" not in recorder.calls, "the UI was supposed to be wedged"
    assert "discard" in recorder.calls
    assert "terminal" in recorder.calls, "the user still gets their prompt back"


@pytest.mark.asyncio
async def test_a_slow_step_spends_only_its_own_budget() -> None:
    """One overrunning step must not starve the ones behind it.

    Under a single shared deadline a slow first step left 0.01s each for closing
    the agent and the terminal UI, which is what produced two "shutdown deadline
    reached" warnings for a problem the user had no part in.
    """
    from omni.cli.main import _shutdown_repl_resources

    recorder = _Recorder()
    agent, watcher, tui, guard = _fakes(recorder, session_delay=5.0, tui_delay=0.2)

    await _shutdown_repl_resources(
        agent=agent,
        session_id="session-1",
        inbox_watcher=watcher,
        tui=tui,  # type: ignore[arg-type]
        input_guard=guard,  # type: ignore[arg-type]
        session_timeout_s=0.05,
        agent_timeout_s=1.0,
        tui_timeout_s=1.0,
    )

    assert "session" not in recorder.calls, "the slow step was supposed to time out"
    assert "agent" in recorder.calls, "closing the agent was starved"
    assert "tui" in recorder.calls, "closing the terminal UI was starved"


@pytest.mark.asyncio
async def test_one_failing_step_does_not_stop_the_others() -> None:
    """Each resource is closed on its own terms."""
    from omni.cli.main import _shutdown_repl_resources

    recorder = _Recorder()

    class Agent:
        async def enqueue_session_maintenance(self, _session_id: str) -> str:
            recorder.calls.append("session")
            raise RuntimeError("could not park the memory work")

        async def aclose(self) -> None:
            recorder.calls.append("agent")

    class Watcher:
        def stop(self) -> None:
            recorder.calls.append("watcher")

    class Tui:
        async def close(self) -> None:
            recorder.calls.append("tui")

    class Guard:
        def restore(self) -> None:
            recorder.calls.append("terminal")

    await _shutdown_repl_resources(
        agent=Agent(),
        session_id="session-1",
        inbox_watcher=Watcher(),
        tui=Tui(),  # type: ignore[arg-type]
        input_guard=Guard(),  # type: ignore[arg-type]
    )

    assert recorder.calls == ["tui", "terminal", "watcher", "session", "agent"]
