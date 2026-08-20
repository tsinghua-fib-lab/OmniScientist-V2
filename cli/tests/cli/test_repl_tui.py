from __future__ import annotations

import asyncio
import io
import logging
import subprocess
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from prompt_toolkit.keys import Keys

from omni.cli.render import assistant_answer
from omni.cli.repl_output import RoutedTextIO, use_output_sink, use_output_turn
from omni.cli.repl_transcript import ANSWER_REPLACE_KEY, clean_scrollback_text, normalize_output
from omni.cli.repl_tui import (
    _OSC52_MAX_CHARS,
    _SPINNER_FRAMES,
    TERMINAL_TURN_STATES,
    ApprovalOption,
    DataTableData,
    ReplInterrupt,
    ReplSubmission,
    ReplTui,
    TranscriptEvent,
    TranscriptKind,
    TranscriptModel,
    resolve_ui_mode,
)
from omni.config.settings import load_settings


class _Stream(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _Sink:
    def __init__(self) -> None:
        self.output: list[str] = []
        self.status = ""

    def write(self, text: str) -> None:
        self.output.append(text)

    def set_status(self, text: str) -> None:
        self.status = text

    def clear(self) -> None:
        self.output.clear()


def test_routed_output_uses_active_sink_and_restores_classic_stream():
    classic = _Stream(tty=True)
    routed = RoutedTextIO(lambda: classic)
    sink = _Sink()

    routed.write("classic")
    with use_output_sink(sink):
        routed.write("tui")
        assert routed.isatty() is False
    routed.write("-again")

    assert "".join(sink.output) == "tui"
    assert classic.getvalue() == "classic-again"
    assert routed.isatty() is True


def test_managed_tui_output_quarantines_diagnostics_and_routes_stdout(
    tmp_path, monkeypatch
):
    from omni.cli.repl_output import use_managed_output_sink

    terminal_stdout = _Stream(tty=True)
    terminal_stderr = _Stream(tty=True)
    monkeypatch.setattr(sys, "stdout", terminal_stdout)
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    sink = _Sink()
    log_path = tmp_path / "tui.log"

    with use_managed_output_sink(sink, diagnostic_log_path=log_path):
        print("background result")
        sys.stderr.write("raw status=401\n")
        logging.getLogger("omni.test.tui").warning(
            "provider failed api_key=sk-1234567890abcdef"
        )

    assert "background result" in "".join(sink.output)
    assert "status=401" not in "".join(sink.output)
    assert terminal_stdout.getvalue() == ""
    assert terminal_stderr.getvalue() == ""
    diagnostics = log_path.read_text(encoding="utf-8")
    assert "raw status=401" in diagnostics
    assert "provider failed" in diagnostics
    assert "sk-1234567890abcdef" not in diagnostics
    assert "[REDACTED]" in diagnostics


@pytest.mark.asyncio
async def test_live_tui_keeps_diagnostics_out_of_transcript_and_input(tmp_path):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe_input:
        tui = ReplTui(
            commands=(),
            input=pipe_input,
            output=DummyOutput(),
            diagnostic_log_path=tmp_path / "tui.log",
        )
        await tui.start()
        try:
            logging.getLogger("omni.test.live_tui").warning(
                "[react] LLM call failed status=401"
            )
            sys.stderr.write("raw provider traceback\n")
            print("background notice")
            await asyncio.sleep(0.05)

            assert tui._input_buffer.text == ""
            assert "background notice" in tui.transcript.text
            assert "status=401" not in tui.transcript.text
            assert "provider traceback" not in tui.transcript.text
        finally:
            await tui.close()

    diagnostics = (tmp_path / "tui.log").read_text(encoding="utf-8")
    assert "status=401" in diagnostics
    assert "provider traceback" in diagnostics


def test_transcript_bounds_memory_without_losing_latest_output():
    transcript = TranscriptModel(max_chars=12)

    transcript.append("old-line\n")
    transcript.append("new-line\n")

    assert len(transcript.text) <= 12
    assert transcript.text.endswith("new-line\n")


@pytest.mark.asyncio
async def test_tui_ignores_blank_submission_and_accepts_control_while_busy():
    tui = ReplTui(commands=("/help",))

    assert tui.accept_text("   \t") is False
    assert tui.transcript.text == ""

    tui.set_busy(True)
    assert tui.accept_text("/stop") is True
    control = await tui.read_submission_async()
    assert isinstance(control, ReplSubmission)
    assert control.text == "/stop"
    assert control.turn_id

    assert tui.accept_text("hello") is True
    assert await tui.read_line_async(mode="auto", fallback=lambda: "fallback") == "hello"
    assert "› hello" in tui.transcript.text


@pytest.mark.asyncio
async def test_user_submission_is_recorded_once_as_a_semantic_header() -> None:
    tui = ReplTui(commands=())
    committed: list[str] = []
    # The dock commits stabilized entries to native scrollback; capture them.
    tui._commit_scrollback = lambda text: committed.append(text)  # type: ignore[method-assign]

    assert tui.accept_text("分析 RAG 的检索证据")
    submission = await tui.read_submission_async()

    assert submission.turn_id
    # Recorded exactly once as a USER_MESSAGE header in the semantic model...
    headers = [
        entry
        for entry in tui.transcript.entries
        if entry.kind == TranscriptKind.USER_MESSAGE and entry.turn_id == submission.turn_id
    ]
    assert len(headers) == 1
    # ...rendered with the Codex-style "›" prefix, and committed to scrollback
    # (the coloured commit keeps the message body contiguous after the prompt).
    assert "› 分析 RAG 的检索证据" in tui.transcript.text
    assert any("分析 RAG 的检索证据" in chunk for chunk in committed)


@pytest.mark.asyncio
async def test_three_rapid_user_inputs_keep_each_answer_in_its_own_turn() -> None:
    tui = ReplTui(commands=())
    for value in ("123", "456", "789"):
        assert tui.accept_text(value)
    submissions = [await tui.read_submission_async() for _ in range(3)]

    with use_output_sink(tui):
        with use_output_turn(submissions[0].turn_id):
            assistant_answer("answer for 123")
        with use_output_turn(submissions[1].turn_id):
            assistant_answer("answer for 456")

    rendered = tui.transcript.text
    assert [item.text for item in submissions] == ["123", "456", "789"]
    assert len({item.turn_id for item in submissions}) == 3
    assert rendered.index("› 123") < rendered.index("answer for 123") < rendered.index("› 456")
    assert rendered.index("› 456") < rendered.index("answer for 456") < rendered.index("› 789")


@pytest.mark.asyncio
async def test_tui_ctrl_c_clears_draft_and_interrupts_an_active_turn() -> None:
    tui = ReplTui(commands=("/stop",))
    binding = tui._app.key_bindings.get_bindings_for_keys((Keys.ControlC,))[-1]
    event = SimpleNamespace(current_buffer=tui._input_buffer)

    tui._input_buffer.insert_text("draft")
    binding.handler(event)
    assert tui._input_buffer.text == ""
    assert tui._submissions.empty()

    tui.set_busy(True)
    binding.handler(event)
    with pytest.raises(ReplInterrupt):
        await tui.read_submission_async()


@pytest.mark.asyncio
async def test_tui_ctrl_d_requests_shutdown_even_while_busy() -> None:
    tui = ReplTui(commands=("/exit",))
    tui.set_busy(True)
    binding = tui._app.key_bindings.get_bindings_for_keys((Keys.ControlD,))[-1]
    event = SimpleNamespace(current_buffer=tui._input_buffer)

    binding.handler(event)

    with pytest.raises(EOFError):
        await tui.read_submission_async()


def test_tui_ctrl_l_redraws_without_deleting_transcript(monkeypatch) -> None:
    tui = ReplTui(commands=())
    tui.append_output("kept research history\n")
    redraws: list[bool] = []
    monkeypatch.setattr(tui, "redraw", lambda: redraws.append(True))
    binding = tui._app.key_bindings.get_bindings_for_keys((Keys.ControlL,))[-1]

    binding.handler(SimpleNamespace())

    assert redraws == [True]
    assert "kept research history" in tui.transcript.text


@pytest.mark.asyncio
async def test_modify_other_keys_ctrl_c_clears_draft_instead_of_garbled_insert():
    """Ctrl+C under modifyOtherKeys must clear the draft, not insert CSI junk."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe_input:
        tui = ReplTui(
            commands=(),
            input=pipe_input,
            output=DummyOutput(),
        )
        await tui.start()
        try:
            tui._input_buffer.insert_text("draft that should clear")
            # xterm modifyOtherKeys encoding for Ctrl+C (letter code 99).
            pipe_input.send_text("\x1b[27;5;99~")
            for _ in range(40):
                await asyncio.sleep(0.02)
                if tui._input_buffer.text == "":
                    break
            assert tui._input_buffer.text == ""
            assert "[27;5;99~" not in tui.transcript.text
        finally:
            await tui.close()


@pytest.mark.asyncio
async def test_modify_other_keys_ctrl_u_discards_line_like_bash():
    """Ctrl+U under modifyOtherKeys must unix-line-discard, not insert CSI junk."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe_input:
        tui = ReplTui(
            commands=(),
            input=pipe_input,
            output=DummyOutput(),
        )
        await tui.start()
        try:
            tui._input_buffer.insert_text("aa")
            # xterm modifyOtherKeys encoding for Ctrl+U (letter code 117).
            pipe_input.send_text("\x1b[27;5;117~")
            for _ in range(40):
                await asyncio.sleep(0.02)
                if tui._input_buffer.text == "":
                    break
            assert tui._input_buffer.text == ""
            assert "[27;5;117~" not in tui._input_buffer.text
        finally:
            await tui.close()


@pytest.mark.asyncio
async def test_tui_negotiates_restores_and_suspends_keyboard_protocol(monkeypatch) -> None:
    from omni.cli import repl_tui as repl_tui_module

    events: list[str] = []

    class Protocol:
        def __init__(self, _output) -> None:  # noqa: ANN001
            pass

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    @asynccontextmanager
    async def fake_in_terminal():
        events.append("child")
        yield

    monkeypatch.setattr(repl_tui_module, "TerminalKeyboardProtocol", Protocol)
    monkeypatch.setattr(repl_tui_module, "in_terminal", fake_in_terminal)
    tui = ReplTui(commands=("/help",))
    blocker = asyncio.Event()

    async def fake_run_async(**_kwargs):  # noqa: ANN003
        await blocker.wait()

    monkeypatch.setattr(tui._app, "run_async", fake_run_async)
    await tui.start()
    async with tui.suspended():
        pass
    assert tui._app_task is not None
    tui._app_task.cancel()
    await tui.close()

    assert events == ["start", "stop", "child", "start", "stop"]


@pytest.mark.asyncio
async def test_foreground_monitor_turns_ctrl_c_into_durable_cancel() -> None:
    from omni.cli.main import _monitor_foreground_turn

    stopped = asyncio.Event()
    controls: list[tuple[str, str, str]] = []

    class Tasks:
        async def request_control(self, task_id: str, *, action: str, instruction: str = "") -> None:
            controls.append((task_id, action, instruction))
            if action == "cancel":
                stopped.set()

    async def running_turn() -> str:
        await stopped.wait()
        return "cancelled turn"

    tui = ReplTui(commands=("/stop", "/steer", "/exit"))
    tui.set_busy(True)
    tui._submissions.put_nowait(ReplInterrupt())
    outcome = await _monitor_foreground_turn(
        asyncio.create_task(running_turn()),
        tui=tui,
        agent=SimpleNamespace(tasks=Tasks()),
        task_ref={"task_id": "task-123"},
        state=SimpleNamespace(),
        session_id="session-1",
        controls=SimpleNamespace(interaction_mode="auto", display_verbosity="normal"),
    )

    assert outcome.turn == "cancelled turn"
    assert outcome.exit_requested is False
    assert controls == [("task-123", "cancel", "")]


@pytest.mark.asyncio
async def test_foreground_monitor_steers_queues_and_exits_after_cancellation() -> None:
    from omni.cli.main import _monitor_foreground_turn

    stopped = asyncio.Event()
    controls: list[tuple[str, str, str]] = []

    class Tasks:
        async def request_control(self, task_id: str, *, action: str, instruction: str = "") -> None:
            controls.append((task_id, action, instruction))
            if action == "cancel":
                stopped.set()

    async def running_turn() -> str:
        await stopped.wait()
        return "cancelled turn"

    tui = ReplTui(commands=("/stop", "/steer", "/exit"))
    tui.set_busy(True)
    assert tui.accept_text("/steer use only primary sources")
    assert tui.accept_text("summarize what completed", disposition="queue")
    assert tui.accept_text("/exit")

    outcome = await _monitor_foreground_turn(
        asyncio.create_task(running_turn()),
        tui=tui,
        agent=SimpleNamespace(tasks=Tasks()),
        task_ref={"task_id": "task-123"},
        state=SimpleNamespace(),
        session_id="session-1",
        controls=SimpleNamespace(interaction_mode="auto", display_verbosity="normal"),
    )

    assert outcome.turn == "cancelled turn"
    assert [item.text for item in outcome.queued_lines] == ["summarize what completed"]
    assert outcome.exit_requested is True
    assert controls == [
        ("task-123", "steer", "use only primary sources"),
        ("task-123", "cancel", ""),
    ]


@pytest.mark.asyncio
async def test_foreground_monitor_does_not_drop_input_when_turn_finishes_same_tick() -> None:
    from omni.cli.main import _monitor_foreground_turn

    finished = asyncio.Event()

    async def running_turn() -> str:
        await finished.wait()
        return "completed turn"

    tui = ReplTui(commands=())
    tui.set_busy(True)
    monitor = asyncio.create_task(
        _monitor_foreground_turn(
            asyncio.create_task(running_turn()),
            tui=tui,
            agent=SimpleNamespace(tasks=SimpleNamespace()),
            task_ref={"task_id": "task-123"},
            state=SimpleNamespace(),
            session_id="session-1",
            controls=SimpleNamespace(interaction_mode="auto", display_verbosity="normal"),
        )
    )
    await asyncio.sleep(0)
    assert tui.accept_text("next research question")
    finished.set()

    outcome = await monitor
    assert [item.text for item in outcome.queued_lines] == ["next research question"]


@pytest.mark.asyncio
async def test_foreground_monitor_runs_tasks_immediately_during_a_turn() -> None:
    """Codex parity: read-only /task runs at once instead of queuing behind a turn."""
    import omni.cli.main as main_mod
    from omni.cli.main import _monitor_foreground_turn, _ReplControls

    finished = asyncio.Event()
    ran = asyncio.Event()
    calls: list[str] = []

    async def fake_repl_command(agent, state, line, session_id):  # noqa: ANN001
        calls.append(line)
        ran.set()

    original = main_mod._repl_command
    main_mod._repl_command = fake_repl_command  # type: ignore[assignment]

    async def running_turn() -> str:
        await finished.wait()
        return "completed turn"

    tui = ReplTui(commands=("/task",))
    tui.set_busy(True)
    try:
        monitor = asyncio.create_task(
            _monitor_foreground_turn(
                asyncio.create_task(running_turn()),
                tui=tui,
                agent=SimpleNamespace(
                    tasks=SimpleNamespace(),
                    paths=SimpleNamespace(local_ops_dir=None),
                ),
                task_ref={"task_id": "task-123"},
                state=SimpleNamespace(),
                session_id="session-1",
                controls=_ReplControls(interaction_mode="auto", display_verbosity="normal"),
            )
        )
        await asyncio.sleep(0)
        assert tui.accept_text("/task all")
        # It must execute without waiting for the (still-running) turn to finish.
        await asyncio.wait_for(ran.wait(), timeout=1.0)
        finished.set()
        outcome = await asyncio.wait_for(monitor, timeout=1.0)
    finally:
        main_mod._repl_command = original  # type: ignore[assignment]

    assert calls == ["/task all"]
    assert outcome.queued_lines == []


@pytest.mark.asyncio
async def test_foreground_monitor_applies_mode_live_and_blocks_update() -> None:
    """`/mode` mutates loop controls live; `/update` is refused (never deferred)."""
    from omni.cli.main import _monitor_foreground_turn, _ReplControls

    finished = asyncio.Event()

    async def running_turn() -> str:
        await finished.wait()
        return "completed turn"

    tui = ReplTui(commands=("/mode", "/update"))
    tui.set_busy(True)
    controls = _ReplControls(interaction_mode="auto", display_verbosity="normal")
    monitor = asyncio.create_task(
        _monitor_foreground_turn(
            asyncio.create_task(running_turn()),
            tui=tui,
            agent=SimpleNamespace(
                tasks=SimpleNamespace(),
                paths=SimpleNamespace(local_ops_dir=None),
            ),
            task_ref={"task_id": "task-123"},
            state=SimpleNamespace(),
            session_id="session-1",
            controls=controls,
        )
    )
    await asyncio.sleep(0)
    assert tui.accept_text("/mode plan")
    assert tui.accept_text("/update")
    for _ in range(5):
        await asyncio.sleep(0)
        if controls.interaction_mode == "plan":
            break
    finished.set()
    outcome = await asyncio.wait_for(monitor, timeout=1.0)

    # Live command applied immediately; blocked command was neither run nor queued.
    assert controls.interaction_mode == "plan"
    assert outcome.queued_lines == []


@pytest.mark.asyncio
async def test_classic_foreground_ctrl_c_requests_cooperative_cancel() -> None:
    from omni.cli.main import _await_classic_foreground_turn

    stopped = asyncio.Event()
    controls: list[tuple[str, str]] = []

    class Tasks:
        async def request_control(self, task_id: str, *, action: str) -> None:
            controls.append((task_id, action))
            stopped.set()

    async def running_turn() -> str:
        await stopped.wait()
        return "cancelled turn"

    waiter = asyncio.create_task(
        _await_classic_foreground_turn(
            asyncio.create_task(running_turn()),
            agent=SimpleNamespace(tasks=Tasks()),
            task_ref={"task_id": "task-123"},
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()

    assert await waiter == "cancelled turn"
    assert controls == [("task-123", "cancel")]


@pytest.mark.asyncio
async def test_busy_footer_shows_animated_spinner_and_shimmer():
    tui = ReplTui(commands=("/stop",))
    tui.set_busy(True)
    try:
        fragments = tui.footer_fragments()
        # Leading fragment is an animated braille spinner in the accent colour.
        assert fragments[0][0] == "class:dock.spinner"
        assert fragments[0][1].strip() in _SPINNER_FRAMES
        # The status label carries a moving shimmer band.
        assert any(style == "class:dock.shimmer" for style, _text in fragments)
        assert tui._spinner_task is not None and not tui._spinner_task.done()
    finally:
        tui.set_busy(False)

    # Idle drops the spinner and hands the strip to the hint styling.
    assert tui._spinner_task is None
    idle = tui.footer_fragments()
    assert all(style != "class:dock.spinner" for style, _text in idle)
    assert "".join(text for _style, text in idle).strip() == tui.footer_text()


def test_dock_styles_resolve_to_visible_attributes():
    """Resolve the palette the way prompt_toolkit will, container style included.

    Two traps this covers. A muted role must never be a grey that can collide
    with the terminal background — that is what hid the composer frame and left
    only the cursor-lit first letter of the placeholder showing. And because a
    window's style merges into its fragments, an accent inside the muted footer
    needs ``nodim`` or it arrives dimmed.
    """
    from prompt_toolkit.styles.base import DEFAULT_ATTRS

    from omni.cli.repl_tui import _STYLE

    def attrs(*classes: str):
        return _STYLE.get_attrs_for_style_str(" ".join(f"class:{c}" for c in classes), DEFAULT_ATTRS)

    # The composer border marks where input goes; it must not be a quiet grey.
    assert attrs("frame.border").color == "ansicyan"
    # Placeholder is dimmed default foreground, never a background-adjacent colour.
    placeholder = attrs("composer.placeholder")
    assert placeholder.dim and placeholder.color == ""
    # Typed text and footer keys shed the dim they would inherit.
    assert attrs("dock.input").dim is False
    key = attrs("dock.footer", "dock.key")
    assert key.color == "ansicyan" and key.dim is False
    assert attrs("dock.footer", "dock.mode").dim is False
    # The labels beside those keys stay quiet.
    assert attrs("dock.footer").dim is True
    # Slash descriptions ride display_meta; the menu must keep them visible.
    meta = attrs("completion-menu", "completion-menu.meta.completion")
    assert meta.dim is True
    current = attrs("completion-menu.completion.current")
    assert current.color == "ansicyan"


@pytest.mark.asyncio
async def test_idle_footer_colours_the_keys_not_the_whole_strip():
    """One uniform grey gives the eye nowhere to land.

    Codex keeps footer hints structured as key-then-label; spend the accent on
    the key so the strip can be scanned for what to press.
    """
    tui = ReplTui(commands=("/stop",))
    fragments = tui.footer_fragments()
    keyed = {text for style, text in fragments if style == "class:dock.key"}

    assert {"Enter", "Ctrl+J", "Ctrl+D"} <= keyed
    # Labels and the mode indicator stay out of the accent colour.
    assert "auto mode" in [text for style, text in fragments if style == "class:dock.mode"]
    assert all(" send" != text or style != "class:dock.key" for style, text in fragments)


@pytest.mark.asyncio
async def test_idle_footer_advertises_shift_enter_when_modified_keys_are_ready():
    tui = ReplTui(commands=("/stop",), shift_enter_ready=True)
    fragments = tui.footer_fragments()
    keyed = {text for style, text in fragments if style == "class:dock.key"}

    assert "Shift+Enter" in keyed
    assert "Ctrl+J" not in keyed


def _modal_text(tui: ReplTui) -> str:
    return "".join(text for _style, text in tui._modal_fragments())


@pytest.mark.asyncio
async def test_approval_modal_navigates_and_returns_selected_value():
    tui = ReplTui(commands=())
    task = asyncio.create_task(
        tui.request_approval(
            "Run command?",
            "omni -P demo task rm deadbeef --force",
            options=(
                ApprovalOption("approve", "Approve once"),
                ApprovalOption(
                    "approve_rule",
                    "Approve `omni -P demo task rm <task-id...> --force` for this session",
                ),
                ApprovalOption("deny", "Deny"),
            ),
        )
    )
    await asyncio.sleep(0)  # let request_approval install the modal
    assert tui._modal_active() is True

    body = _modal_text(tui)
    assert "Run command?" in body
    assert "omni -P demo task rm deadbeef --force" in body
    assert "Approve once" in body and "Deny" in body
    # First option starts selected (highlighted marker on line 1).
    assert any(
        style == "class:modal.option.selected" and "Approve once" in text
        for style, text in tui._modal_fragments()
    )

    tui._modal_move(1)  # ↓ → matching task deletion rule
    assert any(
        style == "class:modal.option.selected" and "Approve `omni" in text
        for style, text in tui._modal_fragments()
    )

    tui._resolve_modal(None)  # enter → confirm current selection
    result = await asyncio.wait_for(task, timeout=1)
    assert result == "approve_rule"
    assert tui._modal_active() is False


@pytest.mark.asyncio
async def test_approval_modal_wraps_the_complete_session_rule_label():
    tui = ReplTui(commands=())
    label = (
        "Approve `omni --project omniscientist_v2-63cf08e0 task rm "
        "<task-id...> --force --yes` for this session"
    )
    task = asyncio.create_task(
        tui.request_approval(
            "Run command?",
            "omni --project omniscientist_v2-63cf08e0 task rm deadbeef "
            "--force --yes",
            options=(
                ApprovalOption("approve", "Approve once"),
                ApprovalOption("approve_rule", label),
                ApprovalOption("deny", "Deny"),
            ),
        )
    )
    await asyncio.sleep(0)
    tui._modal_move(1)

    selected = " ".join(
        text.strip()
        for style, text in tui._modal_fragments()
        if style == "class:modal.option.selected" and text.strip()
    )
    assert label in selected

    tui._resolve_modal("deny")
    assert await asyncio.wait_for(task, timeout=1) == "deny"


@pytest.mark.asyncio
async def test_approval_modal_digit_pick_and_deny_paths():
    tui = ReplTui(commands=())

    task = asyncio.create_task(tui.request_approval("Approve?", ""))
    await asyncio.sleep(0)
    tui._modal_pick(1)  # "2." → Deny in the fallback contract
    assert await asyncio.wait_for(task, timeout=1) == "deny"

    # Esc/Ctrl+C style cancel resolves to deny without a chosen option.
    task2 = asyncio.create_task(tui.request_approval("Approve?", ""))
    await asyncio.sleep(0)
    tui._resolve_modal("deny")
    assert await asyncio.wait_for(task2, timeout=1) == "deny"

    # A second concurrent request is declined while one modal is open.
    task3 = asyncio.create_task(tui.request_approval("First?", ""))
    await asyncio.sleep(0)
    assert await tui.request_approval("Second?", "") == "deny"
    tui._resolve_modal(None)
    await asyncio.wait_for(task3, timeout=1)


@pytest.mark.asyncio
async def test_cancelled_approval_clears_modal_before_the_next_request():
    tui = ReplTui(commands=())
    cancelled = asyncio.create_task(tui.request_approval("First?", "one"))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    assert tui._modal_active() is False
    next_request = asyncio.create_task(tui.request_approval("Second?", "two"))
    await asyncio.sleep(0)
    assert tui._modal_active() is True
    assert "Second?" in _modal_text(tui)
    tui._resolve_modal("approve")
    assert await next_request == "approve"


@pytest.mark.asyncio
async def test_approval_modal_default_highlights_cancel_on_confirmation():
    tui = ReplTui(commands=())
    task = asyncio.create_task(
        tui.request_approval(
            "Enable Bash trust for this task?",
            "You will not be asked again, including for git push and rm -rf.",
            options=(
                ApprovalOption("enable", "Enable for this task"),
                ApprovalOption("cancel", "Cancel"),
            ),
            default="cancel",
        )
    )
    await asyncio.sleep(0)
    assert any(
        style == "class:modal.option.selected" and "Cancel" in text
        for style, text in tui._modal_fragments()
    )
    tui._resolve_modal(None)
    assert await asyncio.wait_for(task, timeout=1) == "cancel"


def test_tui_input_is_multiline_and_grows_to_a_bounded_height():
    tui = ReplTui(commands=("/help",))

    assert tui._input_buffer.multiline() is True
    assert tui._input_window.wrap_lines() is True
    assert tui._input_window.dont_extend_height() is True
    assert tui._input_window.height.min == 1
    assert tui._input_window.height.max == 8


def test_composer_is_wrapped_in_a_border_frame():
    from prompt_toolkit.widgets import Frame

    tui = ReplTui(commands=())
    # The bordered composer wraps the same input window (props asserted above).
    assert isinstance(tui._composer_frame, Frame)
    assert tui._input_window.content is tui._input_control


@pytest.mark.asyncio
async def test_bash_mode_prompt_prefix_switches_on_bang():
    tui = ReplTui(commands=())
    assert tuple(tui._prompt_fragments()[0]) == ("class:dock.prompt", "› ")

    tui._input_buffer.insert_text("!ls -la")
    style, text = tui._prompt_fragments()[0]
    assert style == "class:dock.prompt.bash"
    assert "$" in text


def test_composer_placeholder_shows_only_on_the_empty_first_line():
    from omni.cli.repl_tui import _COMPOSER_PLACEHOLDER, _PlaceholderProcessor

    proc = _PlaceholderProcessor(_COMPOSER_PLACEHOLDER)

    def _ti(text: str, lineno: int, fragments):
        return SimpleNamespace(
            lineno=lineno, document=SimpleNamespace(text=text), fragments=fragments
        )

    empty = proc.apply_transformation(_ti("", 0, [("class:dock.prompt", "› ")]))
    assert any(
        style == "class:composer.placeholder" and _COMPOSER_PLACEHOLDER in text
        for style, text in empty.fragments
    )

    typed = proc.apply_transformation(_ti("hello", 0, [("", "hello")]))
    assert all(style != "class:composer.placeholder" for style, _text in typed.fragments)

    # Never leak the hint onto wrapped continuation lines.
    cont = proc.apply_transformation(_ti("", 1, []))
    assert list(cont.fragments) == []


@pytest.mark.asyncio
async def test_tui_newline_then_enter_submits_one_multiline_turn():
    tui = ReplTui(commands=("/help",))
    bindings = tui._app.key_bindings
    buffer = tui._input_buffer
    event = SimpleNamespace(current_buffer=buffer)
    buffer.insert_text("first line")

    newline = bindings.get_bindings_for_keys((Keys.ControlO,))[-1]
    newline.handler(event)
    buffer.insert_text("second line")

    submit = bindings.get_bindings_for_keys((Keys.ControlM,))[-1]
    submit.handler(event)

    assert await tui.read_line_async(mode="auto", fallback=lambda: "fallback") == (
        "first line\nsecond line"
    )
    assert "› first line" in tui.transcript.text
    assert "\n  second line\n" in tui.transcript.text


@pytest.mark.asyncio
async def test_tui_read_fails_fast_if_application_stops():
    tui = ReplTui(commands=())
    tui._app_task = asyncio.create_task(asyncio.sleep(0))
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="stopped unexpectedly"):
        await tui.read_line_async(mode="auto", fallback=lambda: "fallback")


def test_ui_mode_auto_uses_tui_only_for_capable_interactive_terminal():
    tty = _Stream(tty=True)
    pipe = _Stream(tty=False)

    assert resolve_ui_mode("auto", stdin=tty, stdout=tty, environ={"TERM": "xterm-256color"}) == "tui"
    assert resolve_ui_mode("auto", stdin=tty, stdout=tty, environ={"TERM": "dumb"}) == "classic"
    assert resolve_ui_mode("auto", stdin=tty, stdout=tty, environ={"TERM": "xterm", "CI": "1"}) == "classic"
    assert resolve_ui_mode("auto", stdin=tty, stdout=tty, environ={"TERM": "xterm", "CI": "0"}) == "tui"
    assert resolve_ui_mode("tui", stdin=tty, stdout=tty, environ={"TERM": "xterm", "CI": "1"}) == "tui"
    assert resolve_ui_mode("tui", stdin=pipe, stdout=tty, environ={"TERM": "xterm"}) == "classic"
    assert resolve_ui_mode("classic", stdin=tty, stdout=tty, environ={"TERM": "xterm"}) == "classic"


def test_ui_mode_can_be_configured_or_overridden_by_environment(monkeypatch):
    assert load_settings(overrides={"display": {"ui_mode": "classic"}}).display.ui_mode == "classic"

    monkeypatch.setenv("OMNI_UI", "tui")
    assert load_settings().display.ui_mode == "tui"


@pytest.mark.parametrize(
    ("turn", "expected"),
    [
        (SimpleNamespace(kind="needs_input", settlement_status=""), "needs input"),
        (SimpleNamespace(kind="error", settlement_status="failed"), "failed"),
        (SimpleNamespace(kind="partial", settlement_status="salvaged"), "degraded"),
        (SimpleNamespace(kind="text", settlement_status="passed"), ""),
        (
            SimpleNamespace(
                kind="text",
                settlement_status="succeeded",
                degraded_warnings=["All 3 seed(s) sat on the lower bound of temperature (0.2)."],
            ),
            "degraded",
        ),
    ],
)
def test_completed_turns_map_to_compact_header_states(turn, expected) -> None:  # noqa: ANN001
    from omni.cli.main import _turn_header_state

    assert _turn_header_state(turn) == expected


def test_dock_application_never_captures_mouse_or_uses_the_alternate_screen():
    """IK3MN1 guard: the interactive dock stays in the normal buffer and never
    enables mouse reporting, so drag-select/copy and scrollback stay native."""
    tui = ReplTui(commands=())

    assert tui._app.full_screen is False
    # ``mouse_support`` is a prompt_toolkit filter; it must evaluate to False.
    assert tui._app.mouse_support() is False


def test_stable_events_commit_to_scrollback_and_streaming_answer_uses_the_tail(monkeypatch):
    tui = ReplTui(commands=())
    committed: list[str] = []
    monkeypatch.setattr(tui, "_commit_scrollback", lambda text: committed.append(text))

    # A stable status line commits straight to native scrollback (not the tail).
    tui.publish_event(
        TranscriptEvent(kind=TranscriptKind.STATUS, payload="searching\n", turn_id="t1")
    )
    assert any("searching" in chunk for chunk in committed)
    assert tui._tail_visible() is False

    committed.clear()
    # Streaming tokens only redraw the live tail; nothing is committed yet.
    for piece in ("Hel", "Hello wor", "Hello world"):
        tui.publish_event(
            TranscriptEvent(
                kind=TranscriptKind.MARKDOWN,
                payload=piece,
                turn_id="t1",
                replace_key=ANSWER_REPLACE_KEY,
            )
        )
    assert committed == []
    assert tui._tail_visible() is True

    # Finalizing commits the authoritative answer and clears the tail.
    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.MARKDOWN,
            payload="Hello world",
            turn_id="t1",
            replace_key=ANSWER_REPLACE_KEY,
            final=True,
        )
    )
    assert any("Hello world" in chunk for chunk in committed)
    assert tui._tail_visible() is False


def test_final_answer_is_recorded_at_commit_time_after_intervening_tool_output(monkeypatch):
    """A resize must preserve chronology, not the live placeholder's first position.

    The streamed answer is provisional dock state.  Recording it in the durable
    transcript before a later tool event made resize reflow move the final answer
    above that tool, even though the answer was physically committed afterwards.
    """
    tui = ReplTui(commands=())
    monkeypatch.setattr(tui, "_commit_scrollback", lambda _text: None)

    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.MARKDOWN,
            payload="draft reasoning",
            turn_id="t1",
            replace_key=ANSWER_REPLACE_KEY,
        )
    )
    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.STATUS,
            payload="✓ write_file · report.md\n",
            turn_id="t1",
        )
    )
    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.MARKDOWN,
            payload="Final answer",
            turn_id="t1",
            replace_key=ANSWER_REPLACE_KEY,
            final=True,
        )
    )

    entries = list(tui.transcript.entries)
    assert [entry.payload.strip() for entry in entries] == [
        "✓ write_file · report.md",
        "Final answer",
    ]
    assert "draft reasoning" not in tui.transcript.text


def test_clean_scrollback_text_trims_padding_but_keeps_colour_and_content():
    """Unit guard for the selection fix: trailing full-width padding (bare or
    wrapped in a background SGR span) is removed while colour and text survive."""
    # Bare trailing pad (plain paragraph) is removed.
    assert clean_scrollback_text("short line" + " " * 30) == "short line"
    # A background-painted padding cell collapses to an empty row.
    assert clean_scrollback_text("\x1b[48;2;39;40;34m   \x1b[0m") == ""
    # Colour that wraps real text is preserved; only the trailing pad is dropped.
    coloured = "\x1b[38;5;42mx=1\x1b[0m\x1b[48;5;236m     \x1b[0m"
    cleaned = clean_scrollback_text(coloured)
    assert "\x1b[38;5;42m" in cleaned and "x=1" in cleaned
    assert normalize_output(cleaned) == "x=1"
    # Idempotent and newline-structure-preserving (visible text per row).
    block = "a\x1b[0m   \n\x1b[48;5;236m   \x1b[0m\nb"
    cleaned_block = clean_scrollback_text(block)
    assert [normalize_output(line) for line in cleaned_block.split("\n")] == ["a", "", "b"]
    assert clean_scrollback_text(cleaned_block) == cleaned_block


def test_committed_rows_are_not_padded_to_full_width_for_selection(monkeypatch):
    """Selection fix (Codex clear-to-EOL): committed rows keep their natural width
    so terminals/tmux allow mid-line drag-select instead of snapping to the start
    of a padded, auto-wrapped logical line."""
    tui = ReplTui(commands=())
    monkeypatch.setattr(tui, "terminal_size", lambda: (24, 60))
    committed: list[str] = []
    monkeypatch.setattr(tui, "_commit_scrollback", lambda text: committed.append(text))

    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.MARKDOWN,
            payload="short line",
            turn_id="t1",
            replace_key=ANSWER_REPLACE_KEY,
            final=True,
        )
    )

    assert committed
    visible_lines = normalize_output("".join(committed)).split("\n")
    assert any("short line" in line for line in visible_lines)
    for line in visible_lines:
        assert line == line.rstrip(" ")  # no trailing padding
        assert len(line) < 60  # never filled out to the terminal width


def test_width_change_reemits_committed_history_once_at_new_width(monkeypatch):
    """Resize reflow (Codex parity): a settled width change clears scrollback and
    re-emits committed history at the new width in a single write — fixing frozen
    wrapping and the stale duplicate dock that inline resize otherwise leaves."""
    from omni.cli.repl_tui import _CLEAR_SCROLLBACK

    tui = ReplTui(commands=())
    monkeypatch.setattr(tui, "terminal_size", lambda: (24, 100))

    tui.publish_event(
        TranscriptEvent(kind=TranscriptKind.USER_MESSAGE, payload="hello", turn_id="t1")
    )
    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.MARKDOWN,
            payload="an answer with several words to wrap",
            turn_id="t1",
            replace_key=ANSWER_REPLACE_KEY,
            final=True,
        )
    )
    # A live plan entry stays in the dock tail and must never be re-emitted.
    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.STATUS,
            payload="live plan step\n",
            turn_id="t1",
            replace_key="plan.summary",
        )
    )
    assert tui._tail_visible() is True

    committed: list[str] = []
    monkeypatch.setattr(tui, "_commit_scrollback", lambda text: committed.append(text))
    tui._app._is_running = True  # let the reflow run without a live application
    monkeypatch.setattr(tui, "terminal_size", lambda: (24, 40))  # the window shrank

    tui._reflow_scrollback()

    assert len(committed) == 1  # one clean repaint, never stacked duplicate docks
    payload = committed[0]
    assert payload.startswith(_CLEAR_SCROLLBACK)  # scrollback + screen cleared first
    visible = normalize_output(payload)
    assert "hello" in visible
    assert "an answer with several words to wrap" in visible
    assert "live plan step" not in visible  # the live tail is not committed twice


def test_height_only_change_does_not_trigger_a_reflow(monkeypatch):
    """A height-only resize keeps committed wrapping intact and must not clear
    scrollback: only *width* changes drive a re-emit."""
    tui = ReplTui(commands=())
    widths = iter([(24, 80), (24, 80), (30, 80)])  # rows change, columns stay 80
    monkeypatch.setattr(tui, "terminal_size", lambda: next(widths))
    reflows: list[bool] = []
    monkeypatch.setattr(tui, "_reflow_scrollback", lambda: reflows.append(True))

    # Prime the known width, then feed a height-only change; width is unchanged so
    # no debounce deadline is armed and no reflow fires.
    assert tui._commit_width() == 80
    tui._known_width = 80
    assert tui._commit_width() == 80  # deadline stays unset
    assert tui._reflow_deadline is None
    assert reflows == []


def test_pending_live_plan_is_flushed_to_scrollback_when_the_turn_ends(monkeypatch):
    tui = ReplTui(commands=())
    committed: list[str] = []
    monkeypatch.setattr(tui, "_commit_scrollback", lambda text: committed.append(text))

    # A live plan checklist stays in the tail while the turn runs...
    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.STATUS,
            payload="plan · 2 steps\n",
            turn_id="t1",
            replace_key="plan.summary",
        )
    )
    assert committed == []
    assert tui._tail_visible() is True

    # ...and is committed to scrollback once the turn (or last queued turn) ends.
    tui.set_busy(False)
    assert any("plan · 2 steps" in chunk for chunk in committed)
    assert tui._tail_visible() is False


@pytest.mark.asyncio
async def test_external_noninteractive_command_is_captured_in_transcript(monkeypatch):
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    tui = ReplTui(commands=())

    class FakeStream:
        def __init__(self) -> None:
            self.chunks = iter((b"ordinary output\n", b""))

        async def read(self, _size: int) -> bytes:
            return next(self.chunks)

    class FakeProcess:
        stdout = FakeStream()

        async def wait(self) -> int:
            return 0

    async def fake_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        assert args[:3] == (cli_main.sys.executable, "-m", "omni.cli.main")
        assert kwargs["stdin"] is asyncio.subprocess.DEVNULL
        assert kwargs["stdout"] is asyncio.subprocess.PIPE
        assert kwargs["stderr"] is asyncio.subprocess.STDOUT
        return FakeProcess()

    monkeypatch.setattr(cli_main.asyncio, "create_subprocess_exec", fake_exec)
    with use_output_sink(tui):
        returncode = await cli_main._run_repl_external_command(AppState(), "/doctor")

    assert returncode == 0
    assert "ordinary output" in tui.transcript.text


@pytest.mark.asyncio
async def test_skills_list_is_kept_in_tui_transcript(monkeypatch):
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    tui = ReplTui(commands=())

    class FakeStream:
        def __init__(self) -> None:
            self.chunks = iter((b"Skills (20)\nscientific-figure\n", b""))

        async def read(self, _size: int) -> bytes:
            return next(self.chunks)

    class FakeProcess:
        stdout = FakeStream()

        async def wait(self) -> int:
            return 0

    async def fake_exec(*args, **_kwargs):  # noqa: ANN002
        assert args[-2:] == ("skills", "list")
        return FakeProcess()

    def unexpected_terminal_command(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("default skills list must not leave the managed TUI")

    monkeypatch.setattr(cli_main.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(cli_main.subprocess, "run", unexpected_terminal_command)
    assert tui.accept_text("/skills list")
    skills_turn = await tui.read_submission_async()
    assert tui.accept_text("总结刚才的 skill 列表")
    next_turn = await tui.read_submission_async()
    with use_output_sink(tui):
        with use_output_turn(skills_turn.turn_id):
            returncode = await cli_main._run_repl_external_command(AppState(), "/skills list")

    assert returncode == 0
    rendered = tui.transcript.text
    assert "Skills (20)" in rendered
    assert "scientific-figure" in rendered
    assert rendered.index("› /skills list") < rendered.index("Skills (20)")
    assert rendered.index("scientific-figure") < rendered.index("› 总结刚才的 skill 列表")
    assert next_turn.turn_id != skills_turn.turn_id


@pytest.mark.asyncio
async def test_external_interactive_command_runs_with_tui_suspended(monkeypatch):
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    tui = ReplTui(commands=())
    suspended = False

    @asynccontextmanager
    async def fake_suspended():
        nonlocal suspended
        suspended = True
        yield

    def fake_run(*args, **kwargs):  # noqa: ANN002, ANN003
        assert suspended is True
        assert "capture_output" not in kwargs
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(tui, "suspended", fake_suspended)
    monkeypatch.setattr(cli_main.subprocess, "run", fake_run)
    with use_output_sink(tui):
        returncode = await cli_main._run_repl_external_command(
            AppState(), "/channel login feishu"
        )

    assert returncode == 0


def test_dock_is_idle_quiescent_with_no_periodic_refresh():
    """Phase 1a: no idle refresh (Codex FrameRequester parity). Without a periodic
    repaint, the terminal keeps a mouse selection highlighted while the dock idles,
    so Cmd+C copies. Real updates still redraw on demand via ``_invalidate``."""
    tui = ReplTui(commands=())
    assert tui._app.refresh_interval is None


def test_commit_path_inserts_inter_cell_gutter_but_not_before_the_first_cell(monkeypatch):
    """Phase 1b: distinct history cells are separated by one plain blank line
    (Codex inter-cell rule), while consecutive same-turn/same-kind output chunks
    coalesce so streamed raw output is never sprinkled with blank rows."""
    tui = ReplTui(commands=())
    monkeypatch.setattr(tui, "terminal_size", lambda: (24, 80))
    committed: list[str] = []
    monkeypatch.setattr(tui, "_commit_scrollback", lambda text: committed.append(text))

    # First committed cell (the user prompt): no leading blank — nothing precedes.
    tui.publish_event(
        TranscriptEvent(kind=TranscriptKind.USER_MESSAGE, payload="hello", turn_id="t1")
    )
    assert committed and not committed[0].startswith("\n")

    # The answer is a different cell (kind change) → exactly one leading blank.
    committed.clear()
    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.MARKDOWN,
            payload="an answer",
            turn_id="t1",
            replace_key=ANSWER_REPLACE_KEY,
            final=True,
        )
    )
    assert committed
    assert committed[0].startswith("\n") and not committed[0].startswith("\n\n")

    # Two consecutive raw chunks of the *same* cell (same turn + kind) stay flush:
    # the first earns a gutter (new cell vs. the answer), the second does not.
    committed.clear()
    tui.publish_event(
        TranscriptEvent(kind=TranscriptKind.RAW_COMMAND_OUTPUT, payload="chunk-1\n", turn_id="t2")
    )
    tui.publish_event(
        TranscriptEvent(kind=TranscriptKind.RAW_COMMAND_OUTPUT, payload="chunk-2\n", turn_id="t2")
    )
    assert committed[0].startswith("\n")  # new cell after the answer
    assert not committed[1].startswith("\n")  # continuation of the same cell


def test_reflow_preserves_inter_cell_gutter_spacing(monkeypatch):
    """Phase 1b: the resize reflow reproduces the same inter-cell blank gutters as
    the live commit path, so spacing survives a width change unchanged."""
    tui = ReplTui(commands=())
    monkeypatch.setattr(tui, "terminal_size", lambda: (24, 80))
    tui.publish_event(
        TranscriptEvent(kind=TranscriptKind.USER_MESSAGE, payload="q1", turn_id="t1")
    )
    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.MARKDOWN,
            payload="a1",
            turn_id="t1",
            replace_key=ANSWER_REPLACE_KEY,
            final=True,
        )
    )

    committed: list[str] = []
    monkeypatch.setattr(tui, "_commit_scrollback", lambda text: committed.append(text))
    tui._app._is_running = True
    tui._reflow_scrollback()

    assert len(committed) == 1
    visible = normalize_output(committed[0])
    lines = visible.split("\n")
    answer_row = next(i for i, ln in enumerate(lines) if "a1" in ln)
    assert "q1" in visible
    assert lines[answer_row - 1].strip() == ""  # blank gutter between the cells


def _last_copy_notice(tui: ReplTui) -> tuple[TranscriptKind | None, str]:
    for event in reversed(tui.transcript.entries):
        if event.kind in {TranscriptKind.STATUS, TranscriptKind.ERROR}:
            return event.kind, normalize_output(str(event.payload))
    return None, ""


def test_copy_last_answer_emits_osc52_and_reports_copied(monkeypatch):
    """Phase 1c: /copy (Alt+Y) copies the last answer via OSC 52 and commits a
    scrollback notice so the user can confirm it after the fact (Codex parity)."""
    tui = ReplTui(commands=())
    emitted: list[str] = []
    monkeypatch.setattr(tui, "_emit_osc52", lambda text: emitted.append(text))

    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.MARKDOWN,
            payload="the answer body",
            turn_id="t1",
            replace_key=ANSWER_REPLACE_KEY,
            final=True,
        )
    )
    assert tui.copy_last_answer() is True
    assert emitted == ["the answer body"]
    kind, notice = _last_copy_notice(tui)
    assert kind == TranscriptKind.STATUS
    assert "copied last answer" in notice
    assert "15 characters" in notice
    assert tui._last_answer_text() == "the answer body"


def test_copy_last_answer_without_an_answer_reports_status(monkeypatch):
    tui = ReplTui(commands=())
    emitted: list[str] = []
    monkeypatch.setattr(tui, "_emit_osc52", lambda text: emitted.append(text))

    assert tui.copy_last_answer() is False
    assert emitted == []
    kind, notice = _last_copy_notice(tui)
    assert kind == TranscriptKind.ERROR
    assert "no answer to copy yet" in notice
    assert "ask a question first" in notice


def test_copy_last_answer_reports_write_failure(monkeypatch):
    tui = ReplTui(commands=())
    monkeypatch.setattr(tui, "_emit_osc52", lambda text: "failed to write OSC 52")
    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.MARKDOWN,
            payload="the answer body",
            turn_id="t1",
            replace_key=ANSWER_REPLACE_KEY,
            final=True,
        )
    )

    assert tui.copy_last_answer() is False
    kind, notice = _last_copy_notice(tui)
    assert kind == TranscriptKind.ERROR
    assert "copy failed" in notice
    assert "failed to write OSC 52" in notice
    assert "select the answer" in notice


def test_copy_last_answer_rejects_oversized_payload(monkeypatch):
    tui = ReplTui(commands=())
    emitted: list[str] = []
    monkeypatch.setattr(tui, "_emit_osc52", lambda text: emitted.append(text))
    tui.publish_event(
        TranscriptEvent(
            kind=TranscriptKind.MARKDOWN,
            payload="x" * (_OSC52_MAX_CHARS + 1),
            turn_id="t1",
            replace_key=ANSWER_REPLACE_KEY,
            final=True,
        )
    )

    assert tui.copy_last_answer() is False
    assert emitted == []
    kind, notice = _last_copy_notice(tui)
    assert kind == TranscriptKind.ERROR
    assert "too long for OSC 52" in notice
    assert "select the answer" in notice


def test_osc52_sequence_is_well_formed_and_roundtrips_utf8():
    import base64

    seq = ReplTui._osc52_sequence("héllo · 世界")
    assert seq.startswith("\x1b]52;c;") and seq.endswith("\x07")
    encoded = seq[len("\x1b]52;c;") : -1]
    assert base64.b64decode(encoded).decode("utf-8") == "héllo · 世界"


def test_copy_keybinding_is_registered_for_alt_y():
    tui = ReplTui(commands=())
    binding = tui._app.key_bindings.get_bindings_for_keys((Keys.Escape, "y"))
    assert binding  # Alt+Y is delivered as Escape then 'y'


def test_ctrl_t_toggles_inline_transcript_folding(monkeypatch):
    tui = ReplTui(commands=())
    toggled: list[bool] = []
    monkeypatch.setattr(tui, "toggle_transcript_folding", lambda: toggled.append(True))
    binding = tui._app.key_bindings.get_bindings_for_keys((Keys.ControlT,))[-1]

    binding.handler(SimpleNamespace())

    assert toggled == [True]


def test_toggle_transcript_folding_reflows_and_preserves_the_draft(monkeypatch):
    tui = ReplTui(commands=())
    event = TranscriptEvent(
        kind=TranscriptKind.DATA_TABLE,
        payload=DataTableData(
            title="Long help",
            columns=("command", "description"),
            rows=tuple((f"/command-{index}", "details") for index in range(30)),
        ),
        foldable=True,
        initially_collapsed=True,
    )
    tui.transcript.publish(event)
    tui._input_buffer.text = "draft remains here"
    reflows: list[bool] = []
    monkeypatch.setattr(tui, "_reflow_scrollback", lambda: reflows.append(True))

    assert "Ctrl+T to expand" in normalize_output(tui._render_commit_ansi(event, 80))
    assert "Ctrl+T expand" in tui.footer_text()
    assert tui.toggle_transcript_folding() is True
    assert tui._foldable_override is True
    assert "/command-29" in normalize_output(tui._render_commit_ansi(event, 80))
    assert "Ctrl+T collapse" in tui.footer_text()
    assert tui._input_buffer.text == "draft remains here"

    assert tui.toggle_transcript_folding() is True
    assert tui._foldable_override is False
    assert "Ctrl+T to expand" in normalize_output(tui._render_commit_ansi(event, 80))
    assert reflows == [True, True]


def test_long_raw_output_remains_expanded_until_the_user_collapses_it():
    tui = ReplTui(commands=())
    tui.append_output("\n".join(f"raw-{index}" for index in range(30)) + "\n", raw=True)
    event = tui.transcript.entries[0]

    assert event.foldable is True
    assert event.initially_collapsed is False
    assert "raw-20" in normalize_output(tui._render_commit_ansi(event, 80))
    assert "Ctrl+T collapse" in tui.footer_text()


def test_mixed_fold_defaults_override_new_events_and_clear(monkeypatch):
    tui = ReplTui(commands=())
    reflows: list[bool] = []
    monkeypatch.setattr(tui, "_reflow_scrollback", lambda: reflows.append(True))
    help_event = TranscriptEvent(
        kind=TranscriptKind.PLAIN_TEXT,
        payload="\n".join(f"help-{index}" for index in range(20)) + "\n",
        foldable=True,
        initially_collapsed=True,
    )
    tui.publish_event(help_event)
    tui.append_output("\n".join(f"raw-{index}" for index in range(20)) + "\n", raw=True)
    raw_event = tui.transcript.entries[-1]

    # Per-event defaults: help is folded, ordinary long raw output is expanded.
    assert "Ctrl+T to expand" in normalize_output(tui._render_commit_ansi(help_event, 80))
    assert "raw-10" in normalize_output(tui._render_commit_ansi(raw_event, 80))
    assert "Ctrl+T expand" in tui.footer_text()

    # First toggle expands everything because one default-collapsed block exists.
    assert tui.toggle_transcript_folding() is True
    assert tui._foldable_override is True
    assert "help-10" in normalize_output(tui._render_commit_ansi(help_event, 80))

    # New foldable events inherit the user's explicit override.
    later_help = TranscriptEvent(
        kind=TranscriptKind.PLAIN_TEXT,
        payload="\n".join(f"later-help-{index}" for index in range(20)) + "\n",
        foldable=True,
        initially_collapsed=True,
    )
    tui.publish_event(later_help)
    assert "later-help-10" in normalize_output(
        tui._render_commit_ansi(later_help, 80)
    )

    assert tui.toggle_transcript_folding() is True
    assert tui._foldable_override is False
    tui.append_output("\n".join(f"later-raw-{index}" for index in range(20)) + "\n", raw=True)
    later_raw = tui.transcript.entries[-1]
    assert "Ctrl+T to expand" in normalize_output(
        tui._render_commit_ansi(later_raw, 80)
    )

    # Clearing the transcript also clears the user's global override.
    tui.clear()
    assert tui._foldable_override is None
    tui.publish_event(help_event)
    tui.append_output("\n".join(f"fresh-raw-{index}" for index in range(20)) + "\n", raw=True)
    fresh_raw = tui.transcript.entries[-1]
    assert "Ctrl+T to expand" in normalize_output(tui._render_commit_ansi(help_event, 80))
    assert "fresh-raw-10" in normalize_output(tui._render_commit_ansi(fresh_raw, 80))
    assert reflows == [True, True]


def test_toggle_transcript_folding_is_a_noop_without_foldable_output(monkeypatch):
    tui = ReplTui(commands=())
    tui.append_output("short raw output\n", raw=True)
    reflows: list[bool] = []
    monkeypatch.setattr(tui, "_reflow_scrollback", lambda: reflows.append(True))

    assert tui.toggle_transcript_folding() is False
    assert tui._foldable_override is None
    assert reflows == []
    assert tui._runtime_status == "no foldable output"
    assert "Ctrl+T" not in tui.footer_text()


def _turn_header_states(tui: ReplTui, turn_id: str) -> list[str]:
    """The footer state currently shown for ``turn_id``, newest last."""
    return [
        entry.state
        for entry in tui.transcript.entries
        if entry.kind == TranscriptKind.USER_MESSAGE and entry.turn_id == turn_id
    ]


async def _submitted_turn(tui: ReplTui, text: str = "summarise the paper") -> str:
    assert tui.accept_text(text)
    return (await tui.read_submission_async()).turn_id


@pytest.mark.asyncio
async def test_a_stage_label_moves_the_turn_footer_off_planning() -> None:
    """Submission writes "planning" once and never again, so without the
    reporter the footer of a turn that spent minutes retrieving still read
    "planning" (incident 599a725b)."""
    from omni.cli.main import _turn_stage_reporter

    tui = ReplTui(commands=())
    turn_id = await _submitted_turn(tui)
    report = _turn_stage_reporter(tui, turn_id)
    assert report is not None

    assert _turn_header_states(tui, turn_id)[-1] == "planning"
    report("web_search")

    assert _turn_header_states(tui, turn_id)[-1] == "web_search"


@pytest.mark.asyncio
async def test_a_long_stage_label_is_trimmed_to_what_a_footer_can_show() -> None:
    """Stage strings are written for a status line and carry whole sentences
    from python-engine skills; the footer shares its row with the composer."""
    from omni.cli.main import _turn_stage_reporter

    tui = ReplTui(commands=())
    turn_id = await _submitted_turn(tui)
    report = _turn_stage_reporter(tui, turn_id)
    assert report is not None

    report("  Paper text ready;\n  starting full-manuscript understanding  ")

    state = _turn_header_states(tui, turn_id)[-1]
    assert len(state) == 32
    assert state == "Paper text ready; starting full-"


@pytest.mark.asyncio
async def test_a_blank_stage_label_leaves_the_footer_as_it_was() -> None:
    """An empty state is itself terminal, so forwarding one would retire the
    turn on nothing more than a skill emitting a whitespace label."""
    from omni.cli.main import _turn_stage_reporter

    tui = ReplTui(commands=())
    turn_id = await _submitted_turn(tui)
    report = _turn_stage_reporter(tui, turn_id)
    assert report is not None

    report("retrieving")
    report("   ")

    assert _turn_header_states(tui, turn_id)[-1] == "retrieving"


@pytest.mark.asyncio
async def test_a_progress_label_cannot_impersonate_the_turns_own_verdict() -> None:
    """A python-engine skill picks its own progress wording, and the TUI retires
    a turn the moment it is told one of its lifecycle states. A skill reporting
    a step it calls "failed" would therefore end the turn's footer mid-run and
    swallow the real outcome — the frozen-footer symptom the stage reporter was
    added to cure, arriving through the reporter itself.
    """
    from omni.cli.main import _turn_stage_reporter

    tui = ReplTui(commands=())
    turn_id = await _submitted_turn(tui)
    report = _turn_stage_reporter(tui, turn_id)
    assert report is not None

    report("failed")
    # Still shown, so the skill's progress is not lost, but marked as a step.
    assert _turn_header_states(tui, turn_id)[-1] == "stage: failed"

    # ...and the turn is still live, so the work after it keeps rendering.
    report("writing section 3")
    assert _turn_header_states(tui, turn_id)[-1] == "writing section 3"
    tui.set_turn_state(turn_id, "done")
    assert _turn_header_states(tui, turn_id)[-1] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", sorted(state for state in TERMINAL_TURN_STATES if state))
async def test_no_reserved_lifecycle_word_reaches_the_turn_state_as_a_stage(
    reserved: str,
) -> None:
    """The guard is checked against the TUI's own vocabulary rather than a copy,
    so adding a lifecycle state cannot quietly reopen the hole."""
    from omni.cli.main import _turn_stage_reporter

    tui = ReplTui(commands=())
    turn_id = await _submitted_turn(tui)
    report = _turn_stage_reporter(tui, turn_id)
    assert report is not None

    report(reserved)

    assert _turn_header_states(tui, turn_id)[-1] != reserved
    assert reserved in _turn_header_states(tui, turn_id)[-1]
    assert tui._turn_inputs.get(turn_id) is not None


def test_dock_reserves_space_for_completion_menu():
    """The dock is pinned to the terminal bottom, so a cursor-anchored completion
    float has no room to grow and gets clipped to a row or two. A spacer reserves
    rows only while a menu is open; this pins that predicate and the full surface
    the completer offers (the truncated ``/schedule`` hint the user reported)."""
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from omni.cli.main import app as cli_app
    from omni.cli.repl_commands import build_command_catalog
    from omni.cli.repl_tui import _MENU_RESERVE_ROWS

    tui = ReplTui(commands=build_command_catalog(cli_app))

    # Closed menu → no reservation; a non-empty completion state flips it on.
    assert tui._completion_menu_open() is False
    tui._input_buffer.complete_state = object()
    assert tui._completion_menu_open() is True
    assert _MENU_RESERVE_ROWS >= 8  # room for a typical group's subcommands

    # The composer's completer offers the full schedule surface (rendered now that
    # the dock reserves room for the menu).
    completer = tui._input_buffer.completer
    subs = {
        completion.text
        for completion in completer.get_completions(
            Document("/schedule "), CompleteEvent(completion_requested=True)
        )
    }
    assert {"add", "list", "all", "show", "help", "run", "proposals"} <= subs
