"""Persistent inline REPL dock: normal buffer, native scrollback, no mouse capture.

Codex parity (IK3MN1). The interactive REPL used to run a full-screen
prompt_toolkit ``Application`` with ``mouse_support=True``. That captured the
terminal mouse (only to scroll) and used the alternate screen, so click-drag
text selection was swallowed and history vanished on exit.

This renderer stays in the terminal's *normal* buffer and never enables mouse
reporting. Stabilized transcript entries are committed straight to native
scrollback (via ``run_in_terminal``), so history is selectable, copyable, and
survives exit — the terminal owns scrolling and selection. Only a small bottom
*dock* is redrawn in place: the streaming answer tail, an optional status/meta
line, the bordered composer, an inline approval modal, and footer hints.

Scope: CLI display layer only. Agent/Workflow/Skill/MCP/research code is
untouched; ``TranscriptModel`` remains the semantic record used to render each
event to ANSI and to back the CLI's structured-output tests.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import textwrap
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TextIO

from prompt_toolkit.application import Application, get_app, in_terminal, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.filters import Condition, has_completions, has_focus
from prompt_toolkit.formatted_text import ANSI, AnyFormattedText, FormattedText
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    BufferControl,
    ConditionalContainer,
    Dimension,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import (
    BeforeInput,
    Processor,
    Transformation,
    TransformationInput,
)
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

from omni.cli import theme
from omni.cli.repl_command_policy import redact_repl_command
from omni.cli.repl_commands import CommandCatalog
from omni.cli.repl_composer import cancel_completion, install_multiline_bindings
from omni.cli.repl_input import ReplCommandHistory, build_repl_completer
from omni.cli.repl_layout import (
    COMPOSER_PLACEHOLDER,
    center_truncate_path,
    clip_display,
    compact_number,
    display_width,
    fit_hint_parts,
    newline_hint,
    placeholder_for_width,
)
from omni.cli.repl_output import (
    bind_event_to_output_turn,
    current_output_turn_id,
    use_managed_output_sink,
    use_output_sink,
)
from omni.cli.repl_transcript import (
    ANSWER_REPLACE_KEY,
    TRACE_COMMIT_STATE,
    TRACE_DROP_STATE,
    DataTableData,
    TranscriptEvent,
    TranscriptKind,
    TranscriptModel,
    clean_scrollback_text,
    long_output_needs_folding,
    normalize_output,
    render_event_ansi,
)
from omni.cli.terminal_harness import TerminalKeyboardProtocol

# Braille spinner frames (Codex ``status_indicator_widget`` uses the same idea:
# schedule a redraw every frame while busy and advance a small frame ring).
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_FPS = 10.0
_SHIMMER_FPS = 12.0

# Live-tail bounds: the streaming answer only ever shows its most recent lines
# in the dock; completed text is committed to scrollback so the dock stays small.
_TAIL_MAX_ANSWER_LINES = 10
_TAIL_MAX_ROWS = 16

# The dynamic status region is capped at three lines (redesign). Line 1 (stage +
# elapsed) rides the footer beside the spinner; the remaining lines -- the item
# under work and the long-turn heartbeat -- render just above it, here.
_STATUS_DETAIL_MAX = 2

# Rows reserved below the composer while the completion menu is open. The dock is
# a non-full-screen app pinned to the terminal bottom, so a cursor-anchored
# ``CompletionsMenu`` float has no room to grow downward and gets clipped to the
# one or two lines above the footer. ``PromptSession`` avoids this by reserving
# ``reserve_space_for_menu`` rows for its buffer window; we mirror that with a
# spacer that only claims height while completing, so every subcommand renders
# (matching ``CompletionsMenu(max_height=...)``) instead of just the first few.
_MENU_RESERVE_ROWS = 8

# Resize reflow (Codex ``reflow_transcript_now`` parity). Committed rows belong to
# the terminal and never reflow on their own, so on a settled *width* change we
# clear the screen + scrollback and re-emit the in-memory transcript at the new
# width. Polling (not SIGWINCH) keeps this framework-agnostic and fires whether or
# not a turn is running; the debounce coalesces a drag-resize into one repaint.
_RESIZE_POLL_SECONDS = 0.12
_RESIZE_DEBOUNCE_SECONDS = 0.08
# Bound how many trailing rows are re-emitted so a large transcript resizes fast
# (Codex caps replayed history similarly); older rows drop from scrollback.
_REFLOW_ROW_CAP = 1500
# Clear scrollback (3J) + visible screen (2J) then home the cursor, so the
# re-emitted transcript is the only history and stale dock frames left by the
# terminal's own resize reflow are wiped (the "duplicate input box" artifact).
_CLEAR_SCROLLBACK = "\x1b[H\x1b[2J\x1b[3J"

# Cap the payload copied via OSC 52. The escape carries base64 through the
# terminal, and many terminals bound the sequence length; keeping the source well
# under typical limits avoids a silently truncated clipboard on huge answers.
_OSC52_MAX_CHARS = 100_000

# A turn's lifecycle vocabulary: publishing one of these retires the turn, after
# which its footer can no longer be updated. Exported because anything feeding
# ``ReplTui.set_turn_state`` has to be able to tell a verdict apart from a
# cosmetic progress label, and a private copy of the list would drift.
TERMINAL_TURN_STATES: frozenset[str] = frozenset(
    {
        "",
        "cancelled",
        "control",
        "degraded",
        "failed",
        "interrupted",
        "needs input",
    }
)


# Named ANSI slots, so the dock inherits whatever palette the user's terminal
# already uses (see ``omni.cli.theme``). Grey literals here were the reason the
# footer and the composer placeholder washed out on backgrounds they were not
# chosen against.
_STYLE = Style.from_dict(
    {
        "transcript": "",
        "turn.user": "bold",
        "turn.assistant": "",
        "turn.body": "",
        "turn.status": f"italic {theme.PTK_MUTED}",
        "turn.error": f"bold {theme.PTK_DANGER}",
        "dock.meta": theme.PTK_MUTED,
        "dock.rule": theme.PTK_MUTED,
        "dock.prompt": f"bold {theme.PTK_ACCENT}",
        "dock.prompt.bash": f"bold {theme.PTK_CAUTION}",
        # What the user typed is never secondary, so it opts out of the dim it
        # would otherwise inherit from the composer frame.
        "dock.input": theme.PTK_TEXT,
        "dock.tail": theme.PTK_TEXT,
        "composer.placeholder": f"italic {theme.PTK_MUTED}",
        # The composer border is the only thing marking where input goes: it has
        # to stay findable, so it takes the accent rather than a quiet grey.
        "frame.border": theme.PTK_ACCENT,
        "frame.border.bash": theme.PTK_CAUTION,
        "dock.footer": theme.PTK_MUTED,
        "dock.key": theme.PTK_ACCENT,
        "dock.mode": theme.PTK_STRONG,
        "dock.notice": f"bold {theme.PTK_CAUTION}",
        "dock.spinner": f"bold {theme.PTK_ACCENT}",
        "dock.shimmer": theme.PTK_STRONG,
        **theme.completion_menu_styles(),
        # The modal paints its own background, so its foregrounds stay fixed:
        # here the contrast is against our colour, not the terminal's.
        "modal": "bg:#1c1c1c #e4e4e4",
        "modal.frame": "bg:#1c1c1c #00d7af",
        "modal.title": "bg:#1c1c1c bold #ffd75f",
        "modal.detail": "bg:#1c1c1c #b2b2b2",
        "modal.option": "bg:#1c1c1c #d0d0d0",
        "modal.option.selected": "bg:#00875f bold #ffffff",
        "modal.hint": "bg:#1c1c1c italic #808080",
    }
)


class TuiApplicationError(RuntimeError):
    """Raised when the managed terminal application stops unexpectedly."""


class ReplInterrupt(Exception):
    """A Ctrl+C control event scoped to the interactive REPL."""


@dataclass(frozen=True)
class ReplSubmission:
    """One user submission with a stable transcript turn identity."""

    turn_id: str
    text: str
    disposition: Literal["submit", "steer", "queue", "control"] = "submit"


@dataclass(frozen=True)
class ApprovalOption:
    """One selectable choice in the approval modal (value returned + label)."""

    value: str
    label: str


# Default approval choices when a caller does not supply its own set.
_DEFAULT_APPROVAL_OPTIONS: tuple[ApprovalOption, ...] = (
    ApprovalOption("approve", "Approve once"),
    ApprovalOption("deny", "Deny"),
)


class _ApprovalModal:
    """Transient approval state: options, cursor, and the awaiting future."""

    def __init__(
        self,
        *,
        title: str,
        detail: str,
        options: Sequence[ApprovalOption],
        future: asyncio.Future[str],
        default: str = "",
    ) -> None:
        self.title = title
        self.detail = detail
        self.options = tuple(options)
        self.future = future
        index = 0
        if default:
            for position, option in enumerate(self.options):
                if option.value == default:
                    index = position
                    break
        self.index = index

    def move(self, delta: int) -> None:
        count = len(self.options)
        if count:
            self.index = (self.index + delta) % count

    def select_value(self) -> str:
        return self.options[self.index].value if self.options else ""


# Re-export the wide composer hint so existing tests can keep importing it here.
_COMPOSER_PLACEHOLDER = COMPOSER_PLACEHOLDER


class _PlaceholderProcessor(Processor):
    """Render a dim hint on the empty first line without touching buffer text."""

    def __init__(self, text: str | Callable[[], str]) -> None:
        self._text = text

    def apply_transformation(self, ti: TransformationInput) -> Transformation:
        if ti.lineno == 0 and not ti.document.text:
            hint = self._text() if callable(self._text) else self._text
            tail = [("class:composer.placeholder", hint)]
            return Transformation(list(ti.fragments) + tail)
        return Transformation(ti.fragments)


def resolve_ui_mode(
    requested: str,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve ``auto|tui|classic`` without ever forcing the dock onto a pipe.

    ``tui`` is now the inline dock (normal buffer, native scrollback); ``classic``
    is the plain per-line ``ReplInputBox``. Non-interactive stdio, ``TERM=dumb``,
    or CI always fall back to ``classic``.
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    environ = os.environ if environ is None else environ
    mode = (requested or "auto").strip().lower()
    if mode not in {"auto", "tui", "classic"}:
        mode = "auto"
    try:
        interactive = bool(stdin.isatty() and stdout.isatty())
    except (AttributeError, OSError, ValueError):
        interactive = False
    term_limited = environ.get("TERM", "").lower() == "dumb"
    if mode == "classic" or not interactive or term_limited:
        return "classic"
    if mode == "tui":
        return "tui"
    ci_value = environ.get("CI", "").strip().lower()
    if ci_value not in {"", "0", "false", "no", "off"}:
        return "classic"
    return "tui"


class ReplTui:
    """Long-lived inline prompt_toolkit dock used by the interactive REPL.

    The dock never leaves the normal buffer and never enables mouse reporting.
    Stabilized entries are committed to native scrollback; the app only redraws
    the bottom dock (live answer tail, status, composer, footer, approval modal).
    """

    manages_terminal = True

    def __init__(
        self,
        *,
        commands: CommandCatalog | Sequence[str],
        max_transcript_chars: int = 500_000,
        input: Input | None = None,
        output: Output | None = None,
        diagnostic_log_path: str | os.PathLike[str] | None = None,
        output_base: Path | None = None,
        shift_enter_ready: bool = False,
    ) -> None:
        self.enabled = True
        self._shift_enter_ready = bool(shift_enter_ready)
        self.transcript = TranscriptModel(max_chars=max_transcript_chars)
        self._mode = "auto"
        self._model = ""
        self._focus = ""
        self._context_tokens = 0
        self._context_window = 0
        self._last_elapsed_seconds: float | None = None
        self._runtime_status = ""
        # Lines 2-3 of the dynamic status region (the item under work and the
        # long-turn heartbeat); line 1 stays in the footer beside the spinner.
        self._status_detail: list[str] = []
        self._busy = False
        self._busy_started: float | None = None
        self._spinner_task: asyncio.Task[None] | None = None
        self._modal: _ApprovalModal | None = None
        self._submissions: asyncio.Queue[ReplSubmission | Exception] = asyncio.Queue()
        self._turn_inputs: dict[str, str] = {}
        self._app_task: asyncio.Task[object] | None = None
        self._sink_context = None
        self._diagnostic_log_path = diagnostic_log_path
        # Live "tail": replace_key entries (streaming answer, plan checklist) that
        # are still changing. Committed to scrollback on finalize / turn end.
        self._live: OrderedDict[tuple[str, str], TranscriptEvent] = OrderedDict()
        # Resize reflow bookkeeping: last observed terminal width, a pending
        # debounce deadline, and the width-watcher task.
        self._known_width: int | None = None
        self._reflow_deadline: float | None = None
        self._resize_task: asyncio.Task[None] | None = None
        # True while an external command owns the terminal (see ``suspended``): the
        # width watcher must not clear/redraw over the child's output.
        self._suspended = False
        # Ctrl+T changes how semantic long-output cells are rendered, then
        # replays native scrollback from the transcript source. It never starts a
        # second Application or leaves the normal terminal buffer.
        self._foldable_override: bool | None = None
        # (turn_id, kind) of the last cell committed to scrollback, or ``None``
        # before anything commits. Codex separates distinct history cells with one
        # blank line; we reproduce that gutter (blank above/below a user prompt,
        # between consecutive prompts, and between a turn's status/answer cells)
        # by inserting a blank whenever the next commit starts a new cell group —
        # while coalescing same-turn/same-kind streamed chunks so raw output is
        # never sprinkled with blank rows.
        self._last_commit_group: tuple[str, str] | None = None

        completer: Completer = build_repl_completer(commands, output_base=output_base)
        self._input_buffer = Buffer(
            name="OMNI_INPUT",
            completer=completer,
            complete_while_typing=True,
            enable_history_search=True,
            history=ReplCommandHistory(),
            multiline=True,
            read_only=False,
        )
        self._input_control = BufferControl(
            buffer=self._input_buffer,
            input_processors=[
                BeforeInput(self._prompt_fragments),
                _PlaceholderProcessor(self._placeholder_text),
            ],
            focusable=True,
        )
        self._input_window = Window(
            content=self._input_control,
            height=Dimension(min=1, max=8),
            dont_extend_height=True,
            wrap_lines=True,
            style="class:dock.input",
        )
        # A spacer that only claims height while the completion menu is open, so
        # the cursor-anchored menu float (below) has room to render every row
        # instead of being clipped against the footer. It lives inside the frame
        # so the reserved space stays within the composer box.
        self._menu_reserve_window = ConditionalContainer(
            Window(height=Dimension.exact(_MENU_RESERVE_ROWS), style="class:dock.input"),
            filter=Condition(self._completion_menu_open),
        )
        # Codex-style bordered composer. Bash (``!``-prefixed) mode is signalled
        # by the prompt prefix colour; exec/business is unchanged — this is a hint.
        self._composer_frame = Frame(body=HSplit([self._input_window, self._menu_reserve_window]))
        # Live answer tail: only rendered while a turn is streaming; shows the most
        # recent lines in place. Stabilized text has already moved to scrollback.
        self._tail_window = ConditionalContainer(
            Window(
                content=FormattedTextControl(
                    self._tail_fragments, focusable=False, show_cursor=False
                ),
                height=Dimension(min=0, max=_TAIL_MAX_ROWS),
                dont_extend_height=True,
                wrap_lines=True,
                style="class:dock.tail",
            ),
            filter=Condition(self._tail_visible),
        )
        self._meta_window = ConditionalContainer(
            Window(
                content=self._formatted_control(self.meta_text),
                height=1,
                align=WindowAlign.RIGHT,
                style="class:dock.meta",
            ),
            filter=Condition(self._show_meta),
        )
        # Lines 2-3 of the dynamic status region, pinned just above the footer so
        # the item under work and the heartbeat sit next to the footer spinner.
        self._status_detail_window = ConditionalContainer(
            Window(
                content=self._formatted_control(self._status_detail_fragments),
                height=Dimension(min=0, max=_STATUS_DETAIL_MAX),
                dont_extend_height=True,
                wrap_lines=True,
                style="class:dock.meta",
            ),
            filter=Condition(self._status_detail_visible),
        )
        self._modal_control = FormattedTextControl(
            self._modal_fragments, focusable=True, show_cursor=False
        )
        self._modal_window = Window(
            content=self._modal_control,
            height=Dimension(min=1),
            dont_extend_height=True,
            style="class:modal",
        )
        root = HSplit(
            [
                self._tail_window,
                ConditionalContainer(self._modal_window, filter=Condition(self._modal_active)),
                self._meta_window,
                self._composer_frame,
                self._status_detail_window,
                Window(
                    content=self._formatted_control(self.footer_fragments),
                    height=1,
                    style="class:dock.footer",
                ),
            ]
        )
        container = FloatContainer(
            content=root,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    transparent=True,
                    content=CompletionsMenu(max_height=_MENU_RESERVE_ROWS, scroll_offset=1),
                ),
            ],
        )
        self._app: Application[object] = Application(
            layout=Layout(container, focused_element=self._input_control),
            key_bindings=self._key_bindings(),
            # Normal buffer (no alt-screen) so committed history stays in native
            # scrollback; no mouse capture so drag-select/copy is the terminal's.
            full_screen=False,
            mouse_support=False,
            erase_when_done=True,
            enable_page_navigation_bindings=False,
            # No idle refresh (Codex ``FrameRequester`` parity): when no turn is
            # running the dock never repaints, so the terminal keeps the user's
            # native text selection highlighted for Cmd+C. Real updates redraw on
            # demand via ``_invalidate``; the busy spinner drives its own frames.
            refresh_interval=None,
            min_redraw_interval=0.03,
            style=_STYLE,
            input=input,
            output=output,
        )
        self._keyboard_protocol = TerminalKeyboardProtocol(self._app.output)

    @staticmethod
    def _formatted_control(callback):  # noqa: ANN001, ANN205
        return FormattedTextControl(callback, focusable=False, show_cursor=False)

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        focused = has_focus(self._input_buffer)
        editable = focused
        modal_active = Condition(self._modal_active)
        busy = Condition(lambda: self._busy)

        @bindings.add("up", filter=modal_active, eager=True)
        @bindings.add("k", filter=modal_active, eager=True)
        def _modal_up(_event) -> None:  # noqa: ANN001
            self._modal_move(-1)

        @bindings.add("down", filter=modal_active, eager=True)
        @bindings.add("j", filter=modal_active, eager=True)
        def _modal_down(_event) -> None:  # noqa: ANN001
            self._modal_move(1)

        @bindings.add("enter", filter=modal_active, eager=True)
        def _modal_accept(_event) -> None:  # noqa: ANN001
            self._resolve_modal(None)

        @bindings.add("escape", filter=modal_active, eager=True)
        def _modal_cancel(_event) -> None:  # noqa: ANN001
            self._resolve_modal("deny")

        for _digit in "123456789":

            @bindings.add(_digit, filter=modal_active, eager=True)
            def _modal_digit(event, _n=int(_digit)) -> None:  # noqa: ANN001
                self._modal_pick(_n - 1)

        def submit(event, *, disposition: str | None = None) -> None:  # noqa: ANN001
            text = event.current_buffer.text
            if not text.strip():
                event.current_buffer.reset()
                return
            event.current_buffer.history.append_string(text)
            event.current_buffer.reset()
            self.accept_text(text, disposition=disposition)

        install_multiline_bindings(bindings, submit=submit, active=editable)

        # ``~has_completions``: Tab is the conventional accept/cycle key, so while
        # a menu is open it must reach prompt_toolkit's ``menu-complete`` instead
        # of queueing the draft. Without this the busy composer left a user who
        # opened a completion popup with no non-submitting key at all.
        @bindings.add("c-i", filter=focused & busy & ~modal_active & ~has_completions, eager=True)
        def queue_next(event) -> None:  # noqa: ANN001
            """Tab explicitly queues the draft for the next turn while busy."""
            submit(event, disposition="queue")

        @bindings.add("escape", filter=busy & ~modal_active)
        def stop_active(event) -> None:  # noqa: ANN001
            """Esc requests the same cooperative stop as ``/stop``.

            Checked inline rather than via ``~has_completions`` because Escape has
            no default dismiss binding to fall through to, and because ``escape``
            is a prefix key here (Alt chords) that must not become eager.
            """
            if cancel_completion(event.current_buffer):
                return
            self.request_stop()

        # Scrollback, selection, and history scrolling now belong to the terminal
        # (normal buffer). The dock only binds compose/submit/control keys.
        @bindings.add("c-l", eager=True)
        def redraw(_event) -> None:  # noqa: ANN001
            self.redraw()

        @bindings.add("c-c", eager=True)
        @bindings.add("<sigint>", eager=True)
        def cancel(event) -> None:  # noqa: ANN001
            if self._modal is not None:
                self._resolve_modal("deny")
                return
            if event.current_buffer is self._input_buffer and event.current_buffer.text:
                event.current_buffer.reset()
                self.set_status("draft cleared")
                return
            self._submissions.put_nowait(ReplInterrupt())

        @bindings.add("c-d", filter=focused, eager=True)
        def eof(event) -> None:  # noqa: ANN001
            if not event.current_buffer.text:
                self._submissions.put_nowait(EOFError())

        # Alt+Y: deterministic "copy last answer" (Codex ``Ctrl+O`` parity — that
        # combo is taken here by newline). Uses OSC 52 so it works over SSH/tmux
        # and never depends on the terminal's own text selection.
        @bindings.add("escape", "y", filter=~modal_active)
        def copy_last_answer(_event) -> None:  # noqa: ANN001
            self.copy_last_answer()

        # Ctrl+T: fold/expand semantic long-output blocks in the normal buffer.
        @bindings.add("c-t", filter=~modal_active, eager=True)
        def toggle_folding(_event) -> None:  # noqa: ANN001
            self.toggle_transcript_folding()

        return bindings

    def accept_text(self, text: str, *, disposition: str | None = None) -> bool:
        """Submit meaningful text with an explicit active-turn destination.

        Enter sends a new turn while idle and steers the active turn while busy.
        Callers use ``disposition="queue"`` (the Tab binding) to target the next
        turn. Slash commands remain control submissions so the foreground monitor
        can route them without accidentally feeding command text to the model.
        """
        value = text.strip()
        if not value:
            return False
        if value == "/queue":
            self.set_status("Usage: /queue <message>")
            return False
        if value.startswith("/queue "):
            value = value.removeprefix("/queue").strip()
            disposition = "queue"
            if not value:
                return False
        if disposition is None:
            disposition = (
                "control" if value.startswith("/") else "steer" if self._busy else "submit"
            )
        if disposition not in {"submit", "steer", "queue", "control"}:
            raise ValueError(f"unsupported REPL submission disposition: {disposition}")
        turn_id = uuid.uuid4().hex
        display_text = redact_repl_command(value)
        self._turn_inputs[turn_id] = display_text
        state = {
            "submit": "planning",
            "steer": "steering",
            "queue": "queued",
            "control": "control",
        }[disposition]
        header = TranscriptEvent(
            kind=TranscriptKind.USER_MESSAGE,
            payload=display_text,
            turn_id=turn_id,
            replace_key="turn.header",
            state=state,
        )
        # The prompt commits once to native scrollback (selectable, permanent);
        # its transient state label lives only in the semantic model.
        self.transcript.publish(header)
        self._commit_event(header)
        self._submissions.put_nowait(
            ReplSubmission(turn_id=turn_id, text=value, disposition=disposition)
        )
        self.set_busy(True)
        return True

    def request_stop(self) -> bool:
        """Submit one cooperative stop control without discarding the draft."""
        return self.accept_text("/stop", disposition="control")

    async def read_submission_async(self) -> ReplSubmission:
        """Read the next submission without changing the current busy state."""
        item = await self._submissions.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def read_turn_async(
        self,
        *,
        mode: str,
        fallback: Callable[[], str],
    ) -> ReplSubmission:
        """Read one structured submission while keeping the dock application alive."""
        del fallback
        self._mode = mode or "auto"
        if self._submissions.empty():
            self.set_busy(False)
        submission = asyncio.create_task(self._submissions.get())
        app_task = self._app_task
        if app_task is None:
            item = await submission
        else:
            done, _ = await asyncio.wait(
                {submission, app_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if submission in done:
                item = submission.result()
            else:
                submission.cancel()
                await asyncio.gather(submission, return_exceptions=True)
                await self._finish_app_task()
                raise TuiApplicationError("terminal application stopped unexpectedly")
        if isinstance(item, Exception):
            raise item
        return item

    async def read_line_async(self, *, mode: str, fallback: Callable[[], str]) -> str:
        """Compatibility API returning only submission text."""
        return (await self.read_turn_async(mode=mode, fallback=fallback)).text

    def set_turn_state(self, turn_id: str, state: str) -> None:
        """Update a submitted turn's state in the semantic model.

        The committed scrollback header is immutable (native buffer), so this only
        refreshes the model (used by tests / late-output grouping) and the footer;
        it never re-emits the prompt line.
        """
        display_text = self._turn_inputs.get(turn_id)
        if display_text is None:
            return
        self.transcript.publish(
            TranscriptEvent(
                kind=TranscriptKind.USER_MESSAGE,
                payload=display_text,
                turn_id=turn_id,
                replace_key="turn.header",
                state=state,
            )
        )
        if state in TERMINAL_TURN_STATES:
            self._turn_inputs.pop(turn_id, None)
        self._invalidate()

    def update_status(
        self,
        *,
        model: str = "",
        focus: str = "",
        context_tokens: int = 0,
        context_window: int = 0,
        clearable_tokens: int = 0,
    ) -> None:
        del clearable_tokens
        self._model = model.strip()
        self._focus = " ".join(focus.split())
        self._context_tokens = max(0, int(context_tokens))
        self._context_window = max(0, int(context_window))
        self._invalidate()

    def set_last_elapsed(self, seconds: float | None) -> None:
        self._last_elapsed_seconds = None if seconds is None else max(0.0, float(seconds))
        self._invalidate()

    # ── approval modal (Codex bottom-pane approve/deny, rendered inline) ──

    def _modal_active(self) -> bool:
        return self._modal is not None

    async def request_approval(
        self,
        title: str,
        detail: str = "",
        *,
        options: Sequence[ApprovalOption] | None = None,
        default: str = "",
    ) -> str:
        """Show a blocking approve/deny dock row and return the chosen value.

        Render-layer only: a caller (approval flow) awaits this to collect a
        human decision. Concurrent requests are declined (returns ``"deny"``).
        ``default`` selects which option starts highlighted; empty keeps the
        first option, which is the safe "once" choice on a command prompt.
        """
        if self._modal is not None:
            return "deny"
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        modal = _ApprovalModal(
            title=" ".join(str(title).split()) or "Approve action?",
            detail=str(detail),
            options=options or _DEFAULT_APPROVAL_OPTIONS,
            future=future,
            default=default,
        )
        self._modal = modal
        try:
            self._app.layout.focus(self._modal_window)
        except Exception:  # noqa: BLE001 - focus is best-effort before run().
            pass
        self._invalidate()
        try:
            return await future
        finally:
            # Cancellation belongs to this request, not to the next one. A
            # stale modal would make the next request look concurrent and the
            # legacy guard above would turn it into an unrelated denial.
            if self._modal is modal:
                self._modal = None
                if not future.done():
                    future.cancel()
                try:
                    self._app.layout.focus(self._input_control)
                except Exception:  # noqa: BLE001 - focus is best-effort.
                    pass
                self._invalidate()

    def _modal_move(self, delta: int) -> None:
        if self._modal is not None:
            self._modal.move(delta)
            self._invalidate()

    def _modal_pick(self, index: int) -> None:
        modal = self._modal
        if modal is not None and 0 <= index < len(modal.options):
            modal.index = index
            self._resolve_modal(None)

    def _resolve_modal(self, value: str | None) -> None:
        modal = self._modal
        if modal is None:
            return
        result = modal.select_value() if value is None else value
        self._modal = None
        if not modal.future.done():
            modal.future.set_result(result)
        try:
            self._app.layout.focus(self._input_control)
        except Exception:  # noqa: BLE001 - restore focus best-effort.
            pass
        self._invalidate()

    def _modal_width(self) -> int:
        modal = self._modal
        if modal is None:
            return 40
        _, columns = self.terminal_size()
        preferred = max(
            [
                display_width(modal.title),
                *(display_width(opt.label) + 4 for opt in modal.options),
                28,
            ]
        )
        available = max(10, columns - 6)
        return max(10, min(preferred + 4, available, 72))

    def _modal_fragments(self) -> FormattedText:
        modal = self._modal
        if modal is None:
            return FormattedText([])
        inner = self._modal_width()
        frags: list[tuple[str, str]] = []

        def row(style: str, text: str) -> None:
            body = clip_display(text, inner)
            body += " " * max(0, inner - display_width(body))
            frags.append(("class:modal.frame", "│ "))
            frags.append((style, body))
            frags.append(("class:modal.frame", " │\n"))

        frags.append(("class:modal.frame", "╭" + "─" * (inner + 2) + "╮\n"))
        row("class:modal.title", modal.title)
        if modal.detail.strip():
            row("class:modal.frame", "")
            for line in textwrap.wrap(" ".join(modal.detail.split()), inner) or [""]:
                row("class:modal.detail", line)
        row("class:modal.frame", "")
        for pos, opt in enumerate(modal.options):
            selected = pos == modal.index
            marker = "▸ " if selected else "  "
            style = "class:modal.option.selected" if selected else "class:modal.option"
            prefix = f"{marker}{pos + 1}. "
            continuation = " " * len(prefix)
            wrapped = textwrap.wrap(
                opt.label,
                max(1, inner - len(prefix)),
                break_on_hyphens=False,
            ) or [""]
            row(style, prefix + wrapped[0])
            for line in wrapped[1:]:
                row(style, continuation + line)
        row("class:modal.frame", "")
        hint = "↑/↓ move · 1-9 pick · enter confirm · esc deny"
        for line in textwrap.wrap(hint, max(1, inner), break_on_hyphens=False) or [""]:
            row("class:modal.hint", line)
        frags.append(("class:modal.frame", "╰" + "─" * (inner + 2) + "╯"))
        return FormattedText(frags)

    def set_busy(self, busy: bool) -> None:
        busy = bool(busy)
        if busy and not self._busy:
            self._busy_started = time.monotonic()
            self._start_spinner()
        elif not busy:
            self._busy_started = None
            self._runtime_status = ""
            self._stop_spinner()
            # A turn (or the last queued turn) finished: commit whatever is still
            # live (e.g. a plan checklist) into scrollback and empty the tail.
            self._flush_live()
        self._busy = busy
        self._invalidate()

    def _start_spinner(self) -> None:
        """Drive footer animation frames while busy (idle stays redraw-free)."""
        if self._spinner_task is not None and not self._spinner_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. unit tests constructing a TUI directly).
            self._spinner_task = None
            return
        self._spinner_task = loop.create_task(self._animate_busy())

    def _stop_spinner(self) -> None:
        task = self._spinner_task
        self._spinner_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _animate_busy(self) -> None:
        try:
            while self._busy:
                self._invalidate()
                await asyncio.sleep(1.0 / _SPINNER_FPS)
        except asyncio.CancelledError:  # pragma: no cover - cooperative stop
            pass

    async def _watch_resize(self) -> None:
        """Debounce terminal width changes and re-emit the transcript once settled.

        Polling avoids depending on prompt_toolkit's SIGWINCH internals (whose
        inline erase is what duplicates the dock) and catches resizes whether or
        not a turn is running. Only *width* changes trigger a reflow; height-only
        changes keep committed wrapping intact and never duplicate the dock.
        """
        try:
            while True:
                await asyncio.sleep(_RESIZE_POLL_SECONDS)
                if not self._app.is_running:
                    if self._app_task is not None and self._app_task.done():
                        return
                    continue
                if self._suspended:
                    # A child command owns the terminal; defer any reflow until it
                    # returns (the next poll re-compares width and reflows then).
                    continue
                width = self._commit_width()
                if self._known_width is None:
                    self._known_width = width
                    continue
                if width != self._known_width:
                    self._known_width = width
                    self._reflow_deadline = time.monotonic() + _RESIZE_DEBOUNCE_SECONDS
                    continue
                if self._reflow_deadline is not None and time.monotonic() >= self._reflow_deadline:
                    self._reflow_deadline = None
                    self._reflow_scrollback()
        except asyncio.CancelledError:  # pragma: no cover - cooperative stop
            pass

    # ── output sink (scrollback commits + live tail) ──

    def write(self, text: str) -> None:
        self.append_output(text, raw=True)

    def append_output(self, text: str, *, raw: bool = False) -> None:
        value = normalize_output(text)
        if not value:
            return
        kind = TranscriptKind.RAW_COMMAND_OUTPUT if raw else TranscriptKind.PLAIN_TEXT
        self.publish_event(
            TranscriptEvent(
                kind=kind,
                payload=value,
                turn_id=current_output_turn_id(),
                foldable=raw and long_output_needs_folding(value),
            )
        )

    def note_after_interactive(self, message: str, *, style: str = "info") -> None:
        """Record a status note after an interactive child returned, then repaint clean.

        An interactive child (QR login, ``$EDITOR``, a pager) owns the raw terminal
        and scrolls it unpredictably, so prompt_toolkit's cursor accounting is stale
        the moment it hands control back. A normal commit here lands *inside* the
        dock/input row instead of scrollback (the "note breaks the input box"
        artifact). To avoid that we publish the note into the durable transcript
        model (so it survives, and stays selectable) and then re-emit the whole
        transcript at the current width — the same clean-slate path used on resize —
        which discards the child's temporary output and the stale cursor accounting,
        rendering the note and the dock from scratch.
        """
        from rich.markup import escape

        marker = "[cyan]·[/cyan]" if style == "info" else "[yellow]![/yellow]"
        event = bind_event_to_output_turn(
            TranscriptEvent(
                kind=TranscriptKind.STATUS,
                payload=f"{marker} {escape(str(message))}\n",
                turn_id=current_output_turn_id(),
            )
        )
        loop = getattr(self._app, "loop", None)
        if loop is None or not self._app.is_running:
            # Classic mode / tests: no live dock to corrupt — just record the note.
            self.transcript.publish(event)
            return

        def _apply() -> None:
            # Model first, then a full re-emit renders it in the right place. Skip
            # the per-event commit path entirely to avoid a misplaced flash.
            self.transcript.publish(event)
            self._reflow_scrollback()

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _apply()
        else:
            loop.call_soon_threadsafe(_apply)

    def publish_event(self, event: TranscriptEvent) -> None:
        """Accept one structured event from an in-process or child renderer.

        Cross-thread callers (e.g. the inbox watcher) are marshaled onto the app
        loop so ``self._live`` and scrollback commits stay single-threaded.
        """
        event = bind_event_to_output_turn(event)
        loop = getattr(self._app, "loop", None)
        if loop is not None and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                loop.call_soon_threadsafe(self._publish_event_main, event)
                return
        self._publish_event_main(event)

    def _publish_event_main(self, event: TranscriptEvent) -> None:
        if event.replace_key and event.kind != TranscriptKind.USER_MESSAGE:
            key = (event.turn_id, event.replace_key)
            # Codex ``flush_active_cell``: commit only this in-flight slot so
            # the answer / plan live keys are not dragged into scrollback early.
            if event.state == TRACE_COMMIT_STATE:
                self._live.pop(key, None)
                stable = replace(event, replace_key="", state="", final=False)
                self.transcript.publish(stable)
                self._commit_event(stable)
                self._invalidate()
                return
            if event.state == TRACE_DROP_STATE:
                self._live.pop(key, None)
                self._invalidate()
                return
            # A live slot (streaming answer / plan checklist) is provisional dock
            # state, not durable transcript history.  Recording its first frame
            # here would pin the eventual final value to that early position;
            # resize reflow would then move the answer above tool events that
            # actually completed before it.  Stabilize it only when flushed or
            # finalized so the semantic model and native scrollback share one
            # chronology.
            self._live[key] = event
            if event.final:
                self._commit_answer_final(event)
            else:
                self._invalidate()
            return
        # Everything else is stable: commit straight to native scrollback.
        self.transcript.publish(event)
        self._commit_event(event)

    def _tail_visible(self) -> bool:
        return bool(self._live)

    def _completion_menu_open(self) -> bool:
        """True while the input buffer has an active completion menu to show.

        Drives the composer's menu-reserve spacer so the completions float has
        room to render near the terminal bottom (see ``_MENU_RESERVE_ROWS``).
        """
        return self._input_buffer.complete_state is not None

    def _tail_fragments(self) -> AnyFormattedText:
        if not self._live:
            return FormattedText([])
        width = self._commit_width()
        pieces: list[str] = []
        for event in self._live.values():
            if event.kind == TranscriptKind.MARKDOWN:
                # Streamed tokens: show the latest lines as raw text; the
                # authoritative markdown is committed to scrollback on finalize.
                text = render_event_ansi(
                    event,
                    width,
                    expanded=self._event_expanded(event),
                    as_markdown=False,
                )
                lines = text.splitlines()
                if len(lines) > _TAIL_MAX_ANSWER_LINES:
                    lines = lines[-_TAIL_MAX_ANSWER_LINES:]
                piece = "\n".join(lines)
            else:
                piece = render_event_ansi(
                    event,
                    width,
                    expanded=self._event_expanded(event),
                ).rstrip("\n")
            if piece:
                pieces.append(piece)
        joined = "\n".join(pieces)
        return ANSI(joined) if joined else FormattedText([])

    def _commit_answer_final(self, event: TranscriptEvent) -> None:
        """Commit the authoritative answer, flushing its turn's siblings first."""
        turn_id = event.turn_id
        answer_key = (event.turn_id, event.replace_key)
        for key in [k for k in self._live if k[0] == turn_id and k != answer_key]:
            sibling = self._live.pop(key)
            self.transcript.publish(sibling)
            self._commit_event(sibling)
        self._live.pop(answer_key, None)
        self.transcript.publish(event)
        self._commit_event(event)
        self._invalidate()

    def _flush_live(self) -> None:
        if not self._live:
            return
        pending = list(self._live.values())
        self._live.clear()
        for event in pending:
            self.transcript.publish(event)
            self._commit_event(event)
        self._invalidate()

    def _render_commit_ansi(self, event: TranscriptEvent, width: int) -> str:
        """Render one entry to a clean, newline-terminated scrollback chunk.

        Trailing full-width padding is stripped (see ``clean_scrollback_text``) so
        committed rows keep the terminal's native mid-line selection.
        """
        ansi = render_event_ansi(
            event,
            width,
            expanded=self._event_expanded(event),
            as_markdown=event.kind == TranscriptKind.MARKDOWN,
        )
        if not ansi:
            return ""
        ansi = clean_scrollback_text(ansi)
        if not ansi.endswith("\n"):
            ansi += "\n"
        return ansi

    @staticmethod
    def _cell_group(event: TranscriptEvent) -> tuple[str, str]:
        """Identity of the history *cell* an event belongs to (Codex parity).

        Events sharing a ``(turn_id, kind)`` are continuations of one logical cell
        (e.g. successive raw-output chunks) and stay flush; a change in either
        starts a new cell that earns a one-line gutter above it.
        """
        return (event.turn_id, str(event.kind))

    def _commit_event(self, event: TranscriptEvent) -> None:
        ansi = self._render_commit_ansi(event, self._commit_width())
        if not ansi:
            return
        group = self._cell_group(event)
        if self._last_commit_group is not None and group != self._last_commit_group:
            # New cell: prepend a plain (unstyled, terminal-background) blank row so
            # it never sits flush against the previous cell. Kept bare so it adds no
            # full-width padded line that would break mid-line selection.
            ansi = "\n" + ansi
        self._commit_scrollback(ansi)
        self._last_commit_group = group

    def _reflow_scrollback(self) -> None:
        """Re-emit committed history at the current width (Codex reflow-by-re-emit).

        Committed rows are owned by the terminal and do not reflow on resize, and
        prompt_toolkit's inline resize can leave stale dock frames in scrollback
        (the "duplicate input box" artifact). Both are fixed here: clear the
        screen + scrollback, then re-render every stabilized entry — everything
        except the entries still live in the dock tail — at the new width. Runs on
        the app loop; a no-op when the app is not running (unit tests) or while an
        external command owns the terminal.
        """
        if not self._app.is_running or self._suspended:
            return
        width = self._commit_width()
        live_keys = set(self._live)
        cells: list[tuple[tuple[str, str], str]] = []
        rows = 0
        # Re-emit newest-first so the row cap keeps the most recent history, then
        # restore chronological order before writing.
        for event in reversed(self.transcript.entries):
            if (event.turn_id, event.replace_key) in live_keys:
                continue
            ansi = self._render_commit_ansi(event, width)
            if not ansi:
                continue
            cells.append((self._cell_group(event), ansi))
            rows += ansi.count("\n")
            if rows >= _REFLOW_ROW_CAP:
                break
        cells.reverse()
        # Rebuild the same inter-cell gutters the live commit path emits so the
        # transcript's spacing survives a reflow unchanged.
        parts: list[str] = []
        prev_group: tuple[str, str] | None = None
        for group, ansi in cells:
            if prev_group is not None and group != prev_group:
                parts.append("\n")
            parts.append(ansi)
            prev_group = group
        body = "".join(parts)
        # ``run_in_terminal`` erases the dock, writes our clear + re-emitted
        # history to the normal buffer, then resets the renderer and repaints the
        # dock via ``_request_absolute_cursor_position`` — so the stale cursor
        # accounting that duplicates the dock is discarded on the way out.
        self._commit_scrollback(_CLEAR_SCROLLBACK + body)
        # Keep the gutter state consistent for the next live commit after reflow.
        self._last_commit_group = prev_group

    def _commit_scrollback(self, text: str) -> None:
        """Print stabilized text above the dock, into the terminal's scrollback.

        ``run_in_terminal`` erases the dock, writes to the normal buffer (so the
        text scrolls into native history), then repaints the dock below. When the
        app is not running (unit tests) the semantic model is the only record.

        Phase 2a note (Codex ``insert_history.rs`` parity): a DECSTBM
        scroll-region insertion — writing history above the dock *without*
        repainting it — was prototyped and validated in a real PTY (CPR answered).
        It corrupts and drops history here because prompt_toolkit's inline diff
        renderer, unlike Codex's ratatui, does not own the whole frame: it
        recomputes geometry from its own ``_cursor_pos``/``_last_screen`` after
        each render and overwrites the externally-scrolled rows. ``run_in_terminal``
        is prompt_toolkit's correct "print above the dock" primitive, so we keep it
        (the plan's sanctioned fallback). Idle quiescence (Phase 1a) already keeps
        the native selection intact, since commits only happen while a turn runs.
        """
        if not text or not self._app.is_running:
            return
        loop = getattr(self._app, "loop", None)
        if loop is None:
            return
        output = self._app.output

        def _writer() -> None:
            try:
                output.write_raw(text)
                output.flush()
            except (OSError, ValueError):  # pragma: no cover - terminal closed
                pass

        def _schedule() -> None:
            try:
                run_in_terminal(_writer)
            except Exception:  # noqa: BLE001 - a failed commit must never kill a turn
                pass

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _schedule()
        else:
            loop.call_soon_threadsafe(_schedule)

    def terminal_size(self) -> tuple[int, int]:
        """Return the live prompt_toolkit rows/columns with safe fallbacks."""
        try:
            size = self._app.output.get_size()
            return max(1, int(size.rows)), max(20, int(size.columns))
        except Exception:  # noqa: BLE001 - terminal capability fallback.
            return 24, 80

    def _commit_width(self) -> int:
        return self.terminal_size()[1]

    def set_status(self, text: str) -> None:
        # The status may arrive as up to three newline-separated lines (redesign
        # dynamic region). Line 1 goes to the footer beside the spinner; the rest
        # render in the detail strip above it. Each line collapses its own inner
        # whitespace, so a single-line status is unchanged from before.
        lines = [" ".join(line.split()) for line in str(text).split("\n")]
        lines = [line for line in lines if line]
        self._runtime_status = lines[0] if lines else ""
        self._status_detail = lines[1 : 1 + _STATUS_DETAIL_MAX]
        self._invalidate()

    def _status_detail_visible(self) -> bool:
        # Hidden when idle or when a modal owns the dock, so the "still running"
        # strip never lingers beside a confirmation prompt.
        return bool(self._status_detail) and self._modal is None

    def _status_detail_fragments(self) -> FormattedText:
        if not self._status_detail:
            return FormattedText([])
        width = max(1, self.terminal_size()[1])
        lines = [clip_display(line, width) for line in self._status_detail]
        return FormattedText([("class:dock.meta", "\n".join(lines))])

    def _last_answer_text(self) -> str:
        """Raw text of the most recent assistant answer, or '' if none yet.

        Prefers the streaming-answer slot (``ANSWER_REPLACE_KEY``) and only then
        falls back to any committed markdown cell, so ``/copy`` works the moment a
        turn produces prose — before or after finalize. The two passes are what
        make "prefers" true: the task card that follows an answer now renders its
        report deliverables as markdown too, and a single reversed scan matching
        either condition would hand the user a preview instead of the answer.
        """

        def newest(match: Callable[[TranscriptEvent], bool]) -> str:
            for event in reversed(self.transcript.entries):
                if not isinstance(event.payload, str) or not match(event):
                    continue
                text = normalize_output(event.payload).strip("\n")
                if text.strip():
                    return text
            return ""

        return newest(lambda event: event.replace_key == ANSWER_REPLACE_KEY) or newest(
            lambda event: event.kind == TranscriptKind.MARKDOWN
        )

    @staticmethod
    def _osc52_sequence(text: str) -> str:
        """Build an OSC 52 clipboard-write escape carrying ``text`` as base64."""
        payload = text[:_OSC52_MAX_CHARS].encode("utf-8", "replace")
        encoded = base64.b64encode(payload).decode("ascii")
        return f"\x1b]52;c;{encoded}\x07"

    def _emit_osc52(self, text: str) -> str | None:
        """Write an OSC 52 clipboard sequence straight to the terminal.

        OSC 52 produces no visible output and never moves the cursor, so — unlike
        a dock repaint — it does not disturb an in-progress native selection.
        Returns an error message when the write fails, otherwise ``None``.
        """
        sequence = self._osc52_sequence(text)
        output = self._app.output
        error: list[str] = []

        def _writer() -> None:
            try:
                output.write_raw(sequence)
                output.flush()
            except (OSError, ValueError) as exc:  # pragma: no cover - terminal closed
                error.append(str(exc) or exc.__class__.__name__)

        loop = getattr(self._app, "loop", None)
        if loop is None or not self._app.is_running:
            _writer()
            return error[0] if error else None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _writer()
            return error[0] if error else None
        loop.call_soon_threadsafe(_writer)
        return None

    def _report_copy_notice(self, message: str, *, ok: bool) -> None:
        """Record a durable copy result in scrollback (Codex ``/copy`` notice).

        The footer status is complementary and still invisible while idle; the
        transcript line is what the user can confirm after the fact. Composer
        focus and the draft are left untouched.
        """
        from rich.markup import escape

        self.set_status(message)
        if ok:
            kind = TranscriptKind.STATUS
            payload = f"[{theme.ACCENT}]·[/{theme.ACCENT}] {escape(message)}\n"
        else:
            kind = TranscriptKind.ERROR
            payload = f"[{theme.CAUTION}]![/{theme.CAUTION}] {escape(message)}\n"
        self.publish_event(TranscriptEvent(kind=kind, payload=payload, final=True))

    def copy_last_answer(self) -> bool:
        """Copy the latest assistant answer to the clipboard via OSC 52.

        A deterministic alternative to a mouse selection (Codex ``Ctrl+O``
        parity): the copy does not depend on a mouse selection. Success and
        failure are committed to native scrollback — the same notice channel
        ``/mode`` uses — so the user is never left guessing. Returns ``True``
        only when the write was attempted and succeeded.
        """
        text = self._last_answer_text()
        if not text:
            self._report_copy_notice(
                "no answer to copy yet · ask a question first",
                ok=False,
            )
            return False
        if len(text) > _OSC52_MAX_CHARS:
            self._report_copy_notice(
                "copy failed · answer too long for OSC 52 "
                f"({len(text):,} chars; max {_OSC52_MAX_CHARS:,}); "
                "select the answer in the terminal instead",
                ok=False,
            )
            return False
        error = self._emit_osc52(text)
        if error:
            self._report_copy_notice(
                f"copy failed · {error}; select the answer in the terminal instead",
                ok=False,
            )
            return False
        self._report_copy_notice(
            f"copied last answer ({len(text):,} characters)",
            ok=True,
        )
        return True

    def _has_foldable_output(self) -> bool:
        return any(event.foldable for event in self.transcript.entries)

    def _event_expanded(self, event: TranscriptEvent) -> bool:
        if self._foldable_override is not None:
            return self._foldable_override
        return not event.initially_collapsed

    def _has_collapsed_foldable_output(self) -> bool:
        return any(
            event.foldable and not self._event_expanded(event) for event in self.transcript.entries
        )

    def toggle_transcript_folding(self) -> bool:
        """Fold or expand long transcript cells in the existing normal buffer.

        Native terminal scrollback cannot remove rows in place, so the visible
        effect is produced by re-emitting the semantic transcript at the current
        width. The composer remains owned by this Application, preserving its
        draft, cursor, focus, and completion state.
        """
        if not self._has_foldable_output():
            self.set_status("no foldable output")
            return False
        # Mixed defaults are possible: help starts collapsed while long raw
        # output starts expanded. If anything is collapsed, the first toggle
        # expands all foldable cells; otherwise it collapses all of them.
        self._foldable_override = self._has_collapsed_foldable_output()
        action = "expanded" if self._foldable_override else "collapsed"
        self._runtime_status = f"long output {action}"
        self._reflow_scrollback()
        self._invalidate()
        return True

    def clear(self) -> None:
        self.transcript.clear()
        self._turn_inputs.clear()
        self._live.clear()
        self._status_detail = []
        self._last_commit_group = None
        self._foldable_override = None
        self._invalidate()

    def redraw(self) -> None:
        """Repaint the dock without erasing native scrollback."""
        if self._app.is_running:
            try:
                self._app.renderer.reset()
            except Exception:  # noqa: BLE001 - reset is best-effort recovery.
                pass
        self._invalidate()

    def footer_text(self, width: int | None = None) -> str:
        """Hint strip. ``width is None`` is the full logical line (tests, wide terminals)."""
        parts = self._footer_parts()
        if width is None:
            return " · ".join(parts)
        return " · ".join(fit_hint_parts(parts, width, drop_order=self._footer_drop_order()))

    def _footer_parts(self) -> list[str]:
        if self._busy:
            status = self._runtime_status or "working"
            if self._busy_started is not None:
                status += f" · {time.monotonic() - self._busy_started:.0f}s"
            parts = [status, "Enter steer", "Tab queue", "Esc stop", "Ctrl+D exit"]
        else:
            parts = [
                f"{self._mode} mode",
                "Enter send",
                newline_hint(self._shift_enter_ready),
                "select to copy",
                "Ctrl+D exit",
            ]
        if self._has_foldable_output():
            action = "expand" if self._has_collapsed_foldable_output() else "collapse"
            parts.insert(-1, f"Ctrl+T {action}")
        return parts

    def _footer_drop_order(self) -> tuple[str, ...]:
        extras: list[str] = []
        if self._has_foldable_output():
            action = "expand" if self._has_collapsed_foldable_output() else "collapse"
            extras.append(f"Ctrl+T {action}")
        if self._busy:
            return ("Tab queue", "Enter steer", *extras, "Ctrl+D exit")
        return (
            "select to copy",
            newline_hint(self._shift_enter_ready),
            *extras,
            "Ctrl+D exit",
            "Enter send",
        )

    def footer_fragments(self) -> FormattedText:
        columns = self.terminal_size()[1]
        if self._busy:
            frame = _SPINNER_FRAMES[int(time.monotonic() * _SPINNER_FPS) % len(_SPINNER_FRAMES)]
            prefix = f" {frame} "
            text = self.footer_text(width=max(0, columns - display_width(prefix)))
            return FormattedText([("class:dock.spinner", prefix), *self._shimmer(text)])
        # Idle hints are the strip the user reads to learn the keys, so the keys
        # are what carries the colour; one uniform grey gave the eye nowhere to land.
        text = self.footer_text(width=max(0, columns - 1))
        mode, separator, hints = text.partition(" · ")
        fragments: list[tuple[str, str]] = [("class:dock.footer", " "), ("class:dock.mode", mode)]
        if separator:
            fragments.append(("class:dock.footer", separator))
            fragments.extend(
                theme.hint_fragments(
                    hints, key_class="class:dock.key", label_class="class:dock.footer"
                )
            )
        return FormattedText(fragments)

    def _shimmer(self, text: str) -> list[tuple[str, str]]:
        """Sweep a bright band across the leading status label (Codex shimmer)."""
        label, sep, rest = text.partition(" · ")
        if not label:
            return [("class:dock.footer", text)]
        head = max(1, len(label))
        pos = int(time.monotonic() * _SHIMMER_FPS) % head
        fragments = [
            ("class:dock.shimmer" if abs(index - pos) <= 1 else "class:dock.footer", char)
            for index, char in enumerate(label)
        ]
        if sep:
            fragments.append(("class:dock.footer", sep + rest))
        return fragments

    def meta_text(self) -> str:
        width = self.terminal_size()[1]
        model = clip_display(self._model, 24) if self._model else ""
        focus = center_truncate_path(self._focus, 22 if width >= 112 else 16) if self._focus else ""
        parts: list[str] = []
        if model:
            parts.append(model)
        if focus:
            parts.append(focus)
        if self._context_window:
            parts.append(
                f"ctx {compact_number(self._context_tokens)}/{compact_number(self._context_window)}"
            )
        if self._last_elapsed_seconds is not None:
            parts.append(f"last {self._last_elapsed_seconds:.1f}s")
        if not parts:
            return ""
        drop_order = tuple(part for part in (focus, model) if part)
        fitted = fit_hint_parts(parts, max(0, width - 1), drop_order=drop_order)
        return (" · ".join(fitted) + " ") if fitted else ""

    def _placeholder_text(self) -> str:
        # Frame chrome plus the ``› `` prompt; keep the hint on one composer line.
        return placeholder_for_width(
            max(1, self.terminal_size()[1] - 6),
            shift_enter_ready=self._shift_enter_ready,
        )

    def _prompt_fragments(self) -> FormattedText:
        # A leading ``!`` puts the composer in shell intent: show a distinct
        # amber ``$`` prompt (Codex bash mode). The buffer text is untouched.
        if self._input_buffer.text.startswith("!"):
            return FormattedText([("class:dock.prompt.bash", "$ ")])
        return FormattedText([("class:dock.prompt", "› ")])

    @staticmethod
    def _show_meta() -> bool:
        try:
            return get_app().output.get_size().rows >= 10
        except Exception:  # noqa: BLE001 - layout fallback must stay renderable.
            return True

    async def start(self) -> None:
        if self._app_task is not None and not self._app_task.done():
            return
        self._sink_context = use_managed_output_sink(
            self,
            diagnostic_log_path=self._diagnostic_log_path,
        )
        self._sink_context.__enter__()
        try:
            self._keyboard_protocol.start()
            self._app_task = asyncio.create_task(self._app.run_async(set_exception_handler=False))
            self._resize_task = asyncio.create_task(self._watch_resize())
            await asyncio.sleep(0)
            if self._app_task.done():
                await self._finish_app_task()
                self._stop_resize_watch()
                self._keyboard_protocol.stop()
        except BaseException:
            self._stop_resize_watch()
            self._keyboard_protocol.stop()
            if self._sink_context is not None:
                self._sink_context.__exit__(*sys.exc_info())
                self._sink_context = None
            raise

    def _stop_resize_watch(self) -> None:
        task = self._resize_task
        self._resize_task = None
        if task is not None and not task.done():
            task.cancel()

    async def close(self) -> None:
        await self._stop_application()

    @asynccontextmanager
    async def suspended(self) -> AsyncIterator[None]:
        """Temporarily give an interactive child command ownership of the terminal."""
        if self._app_task is None or self._app_task.done():
            with use_output_sink(None):
                yield
            return
        self._keyboard_protocol.stop()
        self._suspended = True
        try:
            with use_output_sink(None):
                async with in_terminal():
                    yield
        finally:
            self._suspended = False
            if self._app_task is not None and not self._app_task.done():
                self._keyboard_protocol.start()
                self.redraw()

    async def _stop_application(self) -> None:
        self._stop_spinner()
        self._stop_resize_watch()
        task = self._app_task
        try:
            if task is not None and not task.done() and self._app.is_running:
                self._app.exit()
            if task is not None:
                await self._finish_app_task()
        finally:
            self._keyboard_protocol.stop()
            if self._sink_context is not None:
                self._sink_context.__exit__(None, None, None)
                self._sink_context = None

    async def _finish_app_task(self) -> None:
        task = self._app_task
        self._app_task = None
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _invalidate(self) -> None:
        if self._app.is_running:
            self._app.invalidate()


def _normalize_output(text: str) -> str:
    return normalize_output(text)


__all__ = [
    "TERMINAL_TURN_STATES",
    "ApprovalOption",
    "DataTableData",
    "ReplInterrupt",
    "ReplSubmission",
    "ReplTui",
    "TranscriptEvent",
    "TranscriptKind",
    "TranscriptModel",
    "TuiApplicationError",
    "resolve_ui_mode",
]
