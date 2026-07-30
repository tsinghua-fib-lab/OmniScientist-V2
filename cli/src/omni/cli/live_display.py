"""Live turn-progress display for the CLI (Claude Code / Codex-style).

``TurnDisplay`` subscribes to the orchestrator's ``on_tool_event`` stream and
renders a running transcript of the turn: planning decisions (intent, plan
shape, validation, recovery), every tool call with an argument summary and a
result preview, workflow step hierarchy with nested tool calls, and budget
notices. Between events a transient Rich status line shows a spinner, the
current stage, elapsed time, and the tool-call count.

Scope: only the CLI entry points (REPL and one-shot runs) construct a
``TurnDisplay``. IM channels call ``handle_turn`` without ``on_tool_event``,
so WeChat/Feishu/DingTalk output is unaffected by anything in this module.

Streaming coordination: token streaming and the Rich ``Status`` live region
cannot share the terminal (partial-line writes corrupt a live display), so the
first streamed token stops the status line and event lines close the open
stream line before printing. The status line resumes while tools execute.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.markup import escape
from rich.text import Text

from omni.cli.render import console, one_line, shorten
from omni.cli.repl_output import publish_transcript_event, set_output_status
from omni.cli.repl_transcript import (
    ANSWER_REPLACE_KEY,
    TOOL_CALL_MAX_LINES,
    TRACE_COMMIT_STATE,
    TRACE_DROP_STATE,
    TRACE_REPLACE_KEY,
    TranscriptEvent,
    TranscriptKind,
)
from omni.core.tool_result import (
    command_exit_summary,
    command_result_status,
    tool_event_name,
)
from omni.runtime.stage_contract import (
    InfoTier,
    Milestone,
    current_item,
    milestone_from_progress,
    tier_visible,
)
from omni.runtime.stage_map import normalize_progress
from omni.runtime.task_results import action_required_presentation

VERBOSITY_LEVELS = ("quiet", "normal", "verbose")

# A tool event that carries no name at all is a transport defect, not a tool the
# user can recognize. Name it as such rather than printing a bare glyph that
# reads like a redacted secret.
_UNNAMED_TOOL = "<unnamed tool>"

_SENSITIVE_KEY_MARKERS = ("key", "token", "secret", "password", "credential", "authorization")

# Streaming answer: coalesce token deltas so the live markdown slot re-renders
# at a readable frame rate instead of once per token.
_ANSWER_INTERVAL = 0.05
_ANSWER_SLOT = ANSWER_REPLACE_KEY

# Plan checklist glyphs: (markup glyph, skill text wrapper) per step state.
_CHECK_GLYPHS = {
    "pending": "[dim]☐[/dim]",
    "active": "[cyan]▸[/cyan]",
    "done": "[green]✔[/green]",
    "degraded": "[yellow]⚠[/yellow]",
    "failed": "[red]✗[/red]",
    "skipped": "[dim]∅[/dim]",
    "restored": "[dim]⤳[/dim]",
}
# ``update_plan`` (model-owned) states → checklist glyph states.
_PLAN_TOOL_STATE = {
    "pending": "pending",
    "in_progress": "active",
    "completed": "done",
}
_STEP_OUTCOME_STATE = {
    "done": "done",
    "degraded": "degraded",
    "failed": "failed",
    "skipped": "skipped",
    "restored": "restored",
    "retry": "active",
    "resume": "active",
}

# Value/args caps per verbosity: keep normal readable, verbose expansive.
_ARGS_LIMIT = {"normal": 90, "verbose": 220}
_VALUE_LIMIT = {"normal": 40, "verbose": 120}
_PREVIEW_LIMIT = {"normal": 110, "verbose": 240}
_COMMAND_SNIPPET_LIMIT = {"normal": 48, "verbose": 80}
_COMMAND_BODY_HEAD = 2
_COMMAND_BODY_TAIL = 2
_COMMAND_BODY_VERBOSE = 10

# The dynamic status region is capped so it never crowds out real output. Line 1
# is the stage heartbeat; line 2 the item under work; line 3 the "still moving"
# hint that only appears once a turn has run long enough to wonder if it stalled.
_STATUS_MAX_LINES = 3
_HEARTBEAT_AFTER_S = 45.0
_CURRENT_ITEM_LIMIT = 72


def resolve_verbosity(settings: Any, *, quiet: bool = False, verbose: bool = False) -> str:
    """CLI-flag precedence: --quiet > --verbose > config display.verbosity."""
    if quiet:
        return "quiet"
    if verbose:
        return "verbose"
    configured = str(getattr(getattr(settings, "display", None), "verbosity", "") or "normal").lower()
    return configured if configured in VERBOSITY_LEVELS else "normal"


def resolve_debug(settings: Any, *, debug: bool = False) -> bool:
    """Whether to reveal the L4 diagnostic layer: --debug flag or config."""
    if debug:
        return True
    return bool(getattr(getattr(settings, "display", None), "debug", False))


@dataclass
class _ActiveCell:
    """One in-flight process cell (Codex ``active_cell``).

    ``start`` is a preview that mutates into the terminal line; ``error`` stays
    open so identical retries become ``×N`` instead of a new scrollback row.
    """

    name: str
    kind: str
    fp: str
    msg: str
    line: str
    count: int = 1

    def render(self) -> str:
        if self.kind == "error" and self.count > 1:
            return f"{self.line} ×{self.count}"
        return self.line


class _StatusText:
    """Dynamic status renderable: re-rendered on every spinner refresh tick."""

    def __init__(self, display: TurnDisplay) -> None:
        self._display = display

    def __rich__(self) -> Text:
        return self._display._status_text()


class TurnDisplay:
    """Render one agent turn's live event stream to the terminal."""

    def __init__(
        self,
        *,
        verbosity: str = "normal",
        status_line: bool = True,
        on_stage: Callable[[str], None] | None = None,
        debug: bool = False,
    ) -> None:
        self.verbosity = verbosity if verbosity in VERBOSITY_LEVELS else "normal"
        self.debug = bool(debug)
        # L4 (diagnostic) content — raw args/results, protocol labels, budget and
        # transcript internals — is off by default and revealed by ``--debug`` or
        # the widest verbosity. Computed once so every call site reads one gate.
        self._diag = tier_visible(InfoTier.DIAGNOSTIC, self.verbosity, debug=self.debug)
        self._status_enabled = (
            bool(status_line) and self.verbosity != "quiet" and console.is_terminal
        )
        self._status: Any = None
        self._started = time.monotonic()
        self._on_stage = on_stage
        self._stage_value = ""
        self._stage = "planning"
        # Owning task id, surfaced the moment it is acknowledged and then kept in
        # the persistent status line for the whole turn (so a long-running or
        # timed-out turn is never "which task was that?"). Set via ``set_task``.
        self._task_id = ""
        # The concrete item under work right now (status region line 2) and the
        # last time any progress arrived (status region line 3 heartbeat), so a
        # slow turn reads as moving rather than stuck.
        self._current_item = ""
        self._last_progress_at = self._started
        self._soft_notified = False
        self._usage_warned = False
        self._usage_tokens = 0
        self._usage_cost = 0.0
        self._tools_started = 0
        self._tools_done = 0
        self._streamed: list[str] = []
        self._stream_open = False
        self._rule_printed = False
        self._streamed_answer = False
        self._answer_last = 0.0
        self._step_started: dict[tuple[str, str], float] = {}
        self._step_index: dict[tuple[str, str], str] = {}
        self._step_num: dict[tuple[str, str], int] = {}
        self._task_started: dict[str, float] = {}
        # Live plan checklist (Codex-style ☐/▸/✔): populated from a validated
        # workflow plan and updated in place as steps start/finish.
        self._plan_checklist: list[dict[str, str]] | None = None
        self._plan_name = ""
        self._plan_status = ""
        # ``verbose`` keeps the historical start/done/nested dump. Normal mode
        # holds one Codex-style active cell and commits it at a boundary.
        self._raw_trace = self.verbosity == "verbose"
        self._active: _ActiveCell | None = None
        self._shown_msgs: set[str] = set()

    @property
    def _stage(self) -> str:
        return self._stage_value

    @_stage.setter
    def _stage(self, value: str) -> None:
        """Record the current stage and let an observer follow it.

        The status line re-reads this on every spinner tick, so it has always
        shown the live stage. The managed TUI's turn state does not poll — it was
        set once at submission and again at completion, which is why a turn that
        spent minutes retrying still read "planning". Notifying on change keeps
        the two surfaces telling the same story without either polling the other.
        """
        if value == self._stage_value:
            return
        self._stage_value = value
        if self._on_stage is not None:
            with contextlib.suppress(Exception):  # a status label must never fail a turn
                self._on_stage(value)

    # ── lifecycle ──

    def begin(self, stage: str = "planning") -> None:
        self._started = time.monotonic()
        self._stage = stage
        self._resume_status()

    def set_task(self, task_id: str) -> None:
        """Bind the owning task id so it stays visible in the status line.

        Called from the ``on_task_ack`` path as soon as the turn's task exists
        (new turn or resumed/scheduled task), so the id is shown continuously —
        not just once at acknowledgement — and remains attached to a turn that
        later times out or degrades.
        """
        task_id = str(task_id or "")
        if task_id and task_id != self._task_id:
            self._task_id = task_id
            self._refresh_status()

    def end(self) -> float:
        self._commit_active()
        self._pause_status()
        if self._stream_open:
            console.print()
            self._stream_open = False
        return max(0.0, time.monotonic() - self._started)

    @property
    def streamed_text(self) -> str:
        return "".join(self._streamed)

    # ── streaming sink (wired as on_token) ──

    def token(self, piece: str) -> None:
        if not piece:
            return
        if not self._streamed:
            self._commit_active()
        self._streamed.append(piece)
        # Preferred path (managed TUI): stream the answer into one in-place
        # markdown slot that re-renders as tokens arrive, like Codex.
        if self._publish_answer(final=False):
            return
        # Classic terminal fallback: incremental raw print.
        self._pause_status()
        if not self._rule_printed:
            console.rule("[bold cyan]OmniScientist", style="cyan")
            self._rule_printed = True
        console.print(piece, end="", markup=False, highlight=False, soft_wrap=True)
        self._stream_open = True

    def finalize_answer(self, text: str) -> bool:
        """Replace the streamed partial with the authoritative final answer.

        Returns ``True`` when the answer was rendered into the live streaming
        slot (managed TUI), so the caller can skip a duplicate ``assistant_answer``.
        """
        self._commit_active()
        if not self._streamed_answer:
            return False
        self._streamed = [text]
        return self._publish_answer(final=True)

    def _publish_answer(self, *, final: bool) -> bool:
        now = time.monotonic()
        if not final and self._streamed_answer and (now - self._answer_last) < _ANSWER_INTERVAL:
            # Coalesce: token is buffered and flushed on the next frame/finalize.
            return True
        handled = publish_transcript_event(
            TranscriptEvent(
                kind=TranscriptKind.MARKDOWN,
                payload="".join(self._streamed),
                replace_key=_ANSWER_SLOT,
                final=final,
            ),
            stream=console.file,
        )
        if handled:
            self._streamed_answer = True
            self._answer_last = now
        return handled

    # ── event sink (wired as on_tool_event) ──

    def tool_event(self, phase: str, data: dict) -> None:
        if self.verbosity == "quiet":
            return
        # Any event is a sign of life for the long-turn heartbeat (status line 3).
        self._last_progress_at = time.monotonic()
        if phase == "plan":
            self._on_plan(data)
        elif phase == "start":
            self._on_tool_start(data)
        elif phase == "done":
            self._on_tool_done(data)
        elif phase == "budget":
            self._on_budget(data)
        elif phase == "notice":
            self._on_notice(data)
        elif phase == "transcript":
            if self._diag:
                repairs = data.get("repairs") or []
                self._line(f"[dim]· transcript repaired ({len(repairs)} fix(es))[/dim]")
        elif phase == "task_start":
            self._on_task_start(data)
        elif phase == "task_progress":
            self._on_task_progress(data)
        elif phase == "task_done":
            self._on_task_done(data)

    # ── phase renderers ──

    def _on_plan(self, data: dict) -> None:
        event = str(data.get("event_type") or "")
        name = escape(str(data.get("name") or ""))
        summary = escape(_squash(str(data.get("summary") or "")))
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        if event == "plan.boundary.selected":
            self._line(
                f"[bold cyan]◆[/bold cyan] intent [bold]{name}[/bold] [dim]{summary}[/dim]",
                replace_key="plan.summary",
            )
        elif event == "plan.model.proposed":
            self._line(
                f"[bold cyan]◆[/bold cyan] plan [bold]{name}[/bold] [dim]{summary}[/dim]",
                replace_key="plan.summary",
            )
        elif event == "plan.model.failed":
            if self._diag:
                self._line(f"[yellow]![/yellow] {summary}")
        elif event == "plan.model.degraded":
            self._line(f"[yellow]![/yellow] {summary}")
        elif event == "plan.approved":
            self._line(
                f"[bold cyan]◆[/bold cyan] plan approved [dim]{summary}[/dim]",
                replace_key="plan.summary",
            )
        elif event == "plan.validated":
            self._render_plan_validated(name, payload)
            self._stage = "executing"
        elif event == "plan.recovery":
            self._render_plan_recovery(payload, summary)
        elif event == "context.assembled":
            if self._diag and summary:
                self._line(f"[dim]· context: {summary}[/dim]")
        elif event == "plan.executed":
            subtask_id = str(data.get("subtask_id") or payload.get("subtask_id") or "")
            suffix = f" execution {escape(subtask_id[:8])}" if subtask_id else ""
            self._line(f"[dim]· dispatched {name}{suffix}[/dim]")
        elif event == "input.resolved":
            # "Look up before ask/error": narrate the in-lane resolution instead
            # of surfacing the skill's missing-input error.
            self._line(f"[cyan]🔎[/cyan] {summary}")
        elif self._diag and summary:
            self._line(f"[dim]· {escape(event)}: {summary}[/dim]")

    def _render_plan_validated(self, name: str, payload: dict) -> None:
        steps = [str(s) for s in payload.get("steps") or [] if str(s)]
        skills = [str(s) for s in payload.get("skills") or [] if str(s)]
        status = str(payload.get("status") or "")
        if steps:
            # A multi-step workflow: render a live checklist that fills in as the
            # steps execute (updated in place through the plan.summary slot).
            self._plan_name = name
            self._plan_status = status
            self._plan_checklist = [{"skill": step, "state": "pending"} for step in steps]
            self._render_checklist()
        else:
            parts = [f"[bold cyan]◆[/bold cyan] plan [bold]{name}[/bold]"]
            if skills:
                parts.append(f"· skills: {escape(', '.join(skills[:6]))}")
            if status and status != "validated":
                parts.append(f"[yellow]({escape(status)})[/yellow]")
            self._line(" ".join(parts), replace_key="plan.summary")
        for warning in [str(w) for w in payload.get("warnings") or [] if str(w)][:4]:
            self._line(f"  [yellow]! {escape(_squash(warning))}[/yellow]")

    def _render_checklist(self) -> None:
        """(Re)render the plan checklist into the in-place ``plan.summary`` slot."""
        if not self._plan_checklist:
            return
        header = "[bold cyan]◆[/bold cyan] plan"
        if self._plan_name:
            header += f" [bold]{escape(self._plan_name)}[/bold]"
        header += f" · {len(self._plan_checklist)} step(s)"
        if self._plan_status and self._plan_status != "validated":
            header += f" [yellow]({escape(self._plan_status)})[/yellow]"
        lines = [header]
        for item in self._plan_checklist:
            state = item.get("state", "pending")
            glyph = _CHECK_GLYPHS.get(state, _CHECK_GLYPHS["pending"])
            skill = escape(item.get("skill", ""))
            if state == "active":
                label = f"[bold]{skill}[/bold]"
            elif state == "pending":
                label = f"[dim]{skill}[/dim]"
            else:
                label = skill
            lines.append(f"  {glyph} {label}")
        self._line("\n".join(lines), replace_key="plan.summary")

    def _mark_step(self, index: object, skill: str, state: str) -> None:
        if not self._plan_checklist:
            return
        target: int | None = None
        if isinstance(index, int) and 1 <= index <= len(self._plan_checklist):
            target = index - 1
        if target is None:
            for pos, item in enumerate(self._plan_checklist):
                if item.get("skill") == skill and item.get("state") in {"pending", "active"}:
                    target = pos
                    break
        if target is not None:
            self._plan_checklist[target]["state"] = state

    def _render_plan_recovery(self, payload: dict, summary: str) -> None:
        action = str(payload.get("action") or "")
        notes = [str(n) for n in payload.get("notes") or [] if str(n)]
        finding_state = str(payload.get("finding_state") or "")
        if finding_state == "resolved" and not self._diag:
            return
        if action in {"", "execute"} and not notes:
            return  # clean pass-through: nothing worth a line
        if action == "react" and not self._diag:
            # These notes are validator text and instructions addressed to the
            # model ("plan (missing_selected_skills) cannot run deterministically:
            # …"). Printed to the owner they read as a crash report, when what
            # happened is that the turn changed route and carried on. Diagnostics
            # still carry every word, as they do for a repair that succeeded.
            self._line(
                "[yellow]↷[/yellow] the planned route could not run; "
                "[dim]continuing with tools[/dim]"
            )
            return
        rung = str(payload.get("rung") or "")
        head = f"[yellow]↷[/yellow] recovery [bold]{escape(action)}[/bold]"
        if rung:
            head += f" [dim]({escape(rung)})[/dim]"
        self._line(head)
        for note in notes[:4]:
            self._line(f"  [yellow]! {escape(_squash(note))}[/yellow]")

    def _on_tool_start(self, data: dict) -> None:
        name = _event_label(tool_event_name(data), _UNNAMED_TOOL)
        self._tools_started += 1
        self._stage = name
        if name == "update_plan":
            # The checklist itself is the output; an "invoking" line would only
            # push it off screen.
            self._refresh_status()
            return
        args = _args_summary(data.get("arguments"), self.verbosity)
        suffix = f"[dim]({args})[/dim]" if args else ""
        markup = f"  [magenta]⚙[/magenta] [bold]{escape(name)}[/bold]{suffix}"
        if self._raw_trace:
            self._line(markup)
        elif self._active and self._active.kind == "error" and self._active.name == name:
            pass  # retry of the held error cell — do not emit another start
        else:
            self._hold_start(name, markup)
        self._refresh_status()

    def _on_plan_update(self, result: Any) -> bool:
        """Render a model-published ``update_plan`` checklist; True if handled.

        The model owns the steps, so the checklist is replaced wholesale on every
        call rather than advanced by the host (Codex renders its plan tool the
        same way).
        """
        if not isinstance(result, dict) or result.get("status") != "ok":
            return False
        steps = result.get("plan")
        if not isinstance(steps, list) or not steps:
            return False
        self._plan_name = ""
        self._plan_status = ""
        self._plan_checklist = [
            {
                "skill": str(step.get("step", "")),
                "state": _PLAN_TOOL_STATE.get(str(step.get("status", "")), "pending"),
            }
            for step in steps
            if isinstance(step, dict)
        ]
        self._render_checklist()
        explanation = str(result.get("explanation") or "")
        if explanation and self._diag:
            self._line(f"  [dim]· {escape(_squash(explanation))}[/dim]")
        self._refresh_status()
        return True

    def _on_tool_done(self, data: dict) -> None:
        name = _event_label(tool_event_name(data), _UNNAMED_TOOL)
        # A budget-rejected call is not an executed tool: it never emitted a
        # ``start``, durable recorders still persist it from this event, and the
        # single ``budget`` event already reports the whole over-budget batch.
        # Rendering one ``∅`` per refused call is exactly the spam being removed
        # (incident 78071dd2), so it is collapsed away here at the display edge.
        if str(data.get("status") or "") == "rejected":
            return
        self._tools_done += 1
        self._stage = "thinking…"
        if (
            name == "update_plan"
            and not data.get("error")
            and self._on_plan_update(data.get("result"))
        ):
            return
        duration = _fmt_duration(data.get("duration_ms"))
        error = str(data.get("error") or "")
        status = str(data.get("status") or "")
        result = data.get("result")
        command_status = _command_status(result)
        transport_failed = status in {"failed", "timed_out", "cancelled", "rejected"}
        if error or transport_failed:
            glyph = "[yellow]∅[/yellow]" if status == "rejected" else "[red]✗[/red]"
            detail = f" [dim]· {duration}[/dim]" if duration else ""
            message = error or status
            markup = (
                f"  {glyph} [bold]{escape(name)}[/bold]{detail}"
                f" · [red]{escape(_detail(message, limit=200))}[/red]"
            )
            if self._raw_trace:
                self._line(markup)
            else:
                self._hold_error(name, message, markup)
        elif command_status and command_status != "succeeded":
            glyph = (
                "[red]✗[/red]"
                if command_status in {"failed", "timed_out"}
                else "[yellow]∅[/yellow]"
            )
            preview = _result_preview(result, self.verbosity)
            command = _command_snippet(data.get("arguments"), self.verbosity)
            parts = [f"  {glyph} [bold]{escape(name)}[/bold]"]
            if command:
                parts.append(f"[dim]{escape(command)}[/dim]")
            if duration:
                parts.append(f"[dim]· {duration}[/dim]")
            if preview:
                parts.append(f"[dim]· {escape(preview)}[/dim]")
            markup = " ".join(parts)
            body = _command_failure_body(result, self.verbosity)
            if body:
                markup = f"{markup}\n{body}"
            fingerprint = f"{command}|{preview or command_status}"
            if self._raw_trace:
                self._line(markup, foldable=markup.count("\n") + 1 > TOOL_CALL_MAX_LINES)
            else:
                self._hold_error(name, fingerprint, markup)
        else:
            self._drop_start(name)
            preview = _result_preview(result, self.verbosity)
            parts = [f"  [green]✓[/green] [bold]{escape(name)}[/bold]"]
            if duration:
                parts.append(f"[dim]· {duration}[/dim]")
            if preview:
                parts.append(f"[dim]· {escape(preview)}[/dim]")
            self._line(" ".join(parts))
            diff = _diff_cell(name, data.get("arguments"), self.verbosity)
            if diff is not None:
                markup, foldable, initially_collapsed = diff
                self._line(
                    markup,
                    foldable=foldable,
                    initially_collapsed=initially_collapsed,
                )
        self._refresh_status()

    def _on_notice(self, data: dict) -> None:
        kind = str(data.get("kind") or "")
        if kind == "reconnect":
            attempt = data.get("attempt") or 1
            total = data.get("max") or 5
            self._line(
                f"  [dim]Reconnecting {attempt}/{total}[/dim]",
                replace_key="llm.reconnect",
            )
            self._refresh_status()
            return
        if kind == "usage":
            self._apply_usage(data)
            return
        if kind == "usage_warn":
            if self._usage_warned:
                return
            self._usage_warned = True
            tokens = data.get("total_tokens") or self._usage_tokens
            cost = data.get("cost_usd")
            if cost is None:
                cost = self._usage_cost
            self._line(
                f"[yellow]![/yellow] usage {escape(str(tokens))} tokens · "
                f"${float(cost):.4f} — continuing; set cost.max_total_tokens "
                "or cost.max_cost_usd to hard-stop"
            )
            self._refresh_status()
            return
        if kind == "context_rollover":
            if self._diag:
                window = data.get("context_window") or {}
                self._line(
                    "  [dim]· context compacted; continuing "
                    f"(window {escape(str(window.get('rollovers') or 1))})[/dim]"
                )
            return
        # Layer-3 soft foreground threshold: reassure the operator that a long
        # turn is still working (not stuck) and keep the task id in view. Never a
        # warning — the turn has not failed and is not being stopped.
        if str(data.get("kind") or "") != "soft_timeout" or self._soft_notified:
            return
        self._soft_notified = True
        try:
            elapsed = float(data.get("elapsed_s") or 0.0)
        except (TypeError, ValueError):
            elapsed = 0.0
        owner = f" [dim]· task {escape(self._task_id[:8])}[/dim]" if self._task_id else ""
        self._line(
            f"  [cyan]◷[/cyan] still working [dim]({elapsed:.0f}s; long-running "
            f"task — continuing)[/dim]{owner}"
        )
        self._refresh_status()

    def _on_budget(self, data: dict) -> None:
        reason = str(data.get("reason") or "budget")
        messages = {
            "max_tool_calls": "configured tool-call limit reached; remaining calls were not run",
            "max_total_tokens": (
                "configured cumulative token limit reached; finalizing from gathered evidence"
            ),
            "max_cost": "configured cost limit reached; finalizing from gathered evidence",
        }
        message = messages.get(reason, f"configured execution limit reached ({reason})")
        self._line(f"[yellow]![/yellow] {escape(message)}")
        if self._diag:
            snapshot = data.get("budget")
            if isinstance(snapshot, dict) and snapshot:
                pairs = ", ".join(f"{k}={v}" for k, v in list(snapshot.items())[:6])
                self._line(f"  [dim]{escape(pairs)}[/dim]")

    def _on_task_start(self, data: dict) -> None:
        workflow_run_id = str(data.get("workflow_run_id") or "")
        task_id = str(data.get("task_id") or "")
        owner = f" task={escape(task_id[:8])}" if task_id else ""
        if workflow_run_id:
            self._task_started[workflow_run_id] = time.monotonic()
            self._stage = "workflow"
            self._line(
                "  [cyan]◷[/cyan] workflow started "
                f"[dim]workflow={escape(workflow_run_id[:8])}{owner}[/dim]"
            )
            self._refresh_status()
            return
        subtask_id = str(data.get("subtask_id") or "")
        skill = _event_label(data.get("skill"), "skill execution")
        self._task_started[subtask_id] = time.monotonic()
        self._stage = f"{skill}"
        identity = (
            f" [dim]execution={escape(subtask_id[:8])}{owner}[/dim]"
            if subtask_id
            else ""
        )
        self._line(
            f"  [cyan]◷[/cyan] execution [bold]{escape(skill)}[/bold] started"
            f"{identity}"
        )
        self._refresh_status()

    def _on_task_done(self, data: dict) -> None:
        workflow_run_id = str(data.get("workflow_run_id") or "")
        task_id = str(data.get("task_id") or "")
        owner = f" task={escape(task_id[:8])}" if task_id else ""
        if workflow_run_id:
            status = str(data.get("status") or "")
            started = self._task_started.pop(workflow_run_id, 0.0)
            elapsed = f" · {time.monotonic() - started:.1f}s" if started else ""
            if status == "succeeded":
                mark = "[green]✓[/green]"
            elif status == "degraded":
                mark = "[yellow]≈[/yellow]"
            elif status == "partial":
                mark = "[yellow]⚠[/yellow]"
            else:
                mark = "[red]✗[/red]"
            # A workflow that stops without saying why is the "it just went
            # quiet" failure. Prefer the direct error, while also preserving
            # result-level warnings/summaries emitted by skill workflows.
            terminal_detail, artifact_path = _task_terminal_detail(data)
            self._stage = "thinking…"
            shown = (not self._raw_trace) and self._message_shown(terminal_detail)
            detail = f" · {escape(terminal_detail)}" if terminal_detail and not shown else ""
            self._line(
                f"  {mark} workflow {escape(status or 'failed')}"
                f" [dim]workflow={escape(workflow_run_id[:8])}{owner}{elapsed}[/dim]"
                + detail
            )
            if terminal_detail and not shown:
                self._remember_msg(terminal_detail)
            if artifact_path:
                self._line(f"      [cyan]artifact[/cyan] {escape(artifact_path)}")
            elif status and status != "succeeded" and not shown:
                self._line("      [dim]No saved artifact was produced.[/dim]")
            self._refresh_status()
            return
        subtask_id = str(data.get("subtask_id") or "")
        skill = _event_label(data.get("skill"), "skill execution")
        status = str(data.get("status") or "")
        elapsed = ""
        started = self._task_started.pop(subtask_id, 0.0)
        if started:
            elapsed = f" · {time.monotonic() - started:.1f}s"
        self._stage = "thinking…"
        terminal_detail, artifact_path = _task_terminal_detail(data)
        shown = (not self._raw_trace) and self._message_shown(terminal_detail)
        if status == "succeeded":
            self._line(
                f"  [green]✓[/green] execution [bold]{escape(skill)}[/bold] succeeded"
                f"[dim]{elapsed} · execution={escape(subtask_id[:8])}{owner}[/dim]"
            )
        elif action_required_presentation(data.get("result")) is not None:
            # A skill that stopped for a missing setting is not a crash, and the
            # card at the end of the turn already carries its full text and the
            # command that clears it. Repeating a clipped copy here is how one
            # missing key became three paragraphs and two red crosses.
            self._line(
                f"  [yellow]⚠[/yellow] execution [bold]{escape(skill)}[/bold] needs configuration"
                f" [dim]execution={escape(subtask_id[:8])}{owner}{elapsed}[/dim]"
            )
        else:
            glyph = (
                "[yellow]⚠[/yellow]"
                if status in {"degraded", "partial"}
                else "[red]✗[/red]"
            )
            detail = f" · {escape(terminal_detail)}" if terminal_detail and not shown else ""
            self._line(
                f"  {glyph} execution [bold]{escape(skill)}[/bold] {escape(status or 'failed')}"
                f" [dim]execution={escape(subtask_id[:8])}{owner}{elapsed}[/dim]"
                + detail
            )
            if terminal_detail and not shown:
                self._remember_msg(terminal_detail)
        if artifact_path:
            self._line(f"      [cyan]artifact[/cyan] {escape(artifact_path)}")
        elif status and status != "succeeded" and not shown:
            self._line("      [dim]No saved artifact was produced.[/dim]")
        self._refresh_status()

    def _render_milestone(self, milestone: Milestone) -> None:
        """Compress a completed stage to one durable line in the persistent log.

        This is the L2->L3 move at the heart of the redesign: the high-frequency
        stage chatter refreshes in place and disappears, and only this
        milestone -- the thing worth reading, quoting, or reproducing later -- is
        appended to the transcript. Shown at every non-quiet verbosity because it
        is a result, not diagnostics.
        """
        self._current_item = ""  # the stage that produced it is finished
        rendered = escape(_squash(milestone.render()))
        if rendered:
            self._line(f"  [green]✓[/green] {rendered}")
        self._refresh_status()

    def _apply_usage(self, data: dict) -> None:
        try:
            self._usage_tokens = int(data.get("total_tokens") or 0)
        except (TypeError, ValueError):
            self._usage_tokens = 0
        try:
            self._usage_cost = float(data.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            self._usage_cost = 0.0
        self._refresh_status()

    def _on_task_progress(self, data: dict) -> None:
        if str(data.get("stage") or "") == "usage":
            self._apply_usage(data)
            return
        # Normalize an un-retrofitted skill's free-form stage into the shared
        # vocabulary (stage id + optional milestone) before anything reads it, so
        # the transcript speaks one language whether or not the skill was updated.
        data = normalize_progress(data)
        stage = str(data.get("stage") or "")
        subtask_id = str(data.get("subtask_id") or "")
        current = current_item(data)
        if current:
            self._current_item = current
        milestone = milestone_from_progress(data)
        if milestone is not None:
            self._render_milestone(milestone)
            return
        if stage == "workflow.start":
            total = data.get("total_steps")
            goal = _detail(data.get("goal"), limit=90)
            head = f"  [cyan]▶[/cyan] workflow · {total} step(s)" if total else "  [cyan]▶[/cyan] workflow"
            self._line(head + (f" [dim]{escape(goal)}[/dim]" if goal else ""))
        elif stage == "workflow.done":
            self._line("  [green]✓[/green] workflow finished")
        elif stage == "workflow.step.start":
            self._on_step_start(subtask_id, data)
        elif stage in {
            "workflow.step.done",
            "workflow.step.degraded",
            "workflow.step.failed",
            "workflow.step.skipped",
            "workflow.step.restored",
            "workflow.step.retry",
            "workflow.step.resume",
        }:
            self._on_step_terminal(subtask_id, stage, data)
        elif stage.startswith("workflow.step.tool.") or stage in {"tool.start", "tool.done"}:
            self._on_nested_tool(stage, data)
        elif stage.startswith("workflow.step.skill.") or stage.startswith("skill."):
            self._on_skill_stage(stage, data)
        else:
            label = _event_label(stage, "")
            if not label:
                return
            self._stage = _squash(label)[:90]
            # Python engines emit human-readable stage sentences. Surface
            # those in normal mode; keep unknown protocol-like labels for L4.
            if self._diag or any(char.isspace() for char in label):
                pct = _fmt_progress(data.get("pct"))
                suffix = f" [dim]· {pct}[/dim]" if pct else ""
                self._line(f"      [cyan]↳[/cyan] {escape(label)}{suffix}")
            self._refresh_status()

    def _on_step_start(self, subtask_id: str, data: dict) -> None:
        step_id = str(data.get("step_id") or "")
        skill = _event_label(data.get("skill") or step_id, "workflow step")
        index = data.get("index")
        total = data.get("total_steps")
        label = f"[{index}/{total}]" if index and total else "[step]"
        self._step_started[(subtask_id, step_id)] = time.monotonic()
        self._step_index[(subtask_id, step_id)] = label
        if isinstance(index, int):
            self._step_num[(subtask_id, step_id)] = index
        self._stage = f"{label} {skill}"
        if self._plan_checklist is not None:
            # A live checklist is active: advance it in place instead of adding a
            # separate per-step line (keeps the plan compact, Codex-style).
            self._mark_step(index, skill, "active")
            self._render_checklist()
        else:
            self._line(f"    [cyan]{escape(label)}[/cyan] [bold]{escape(skill)}[/bold] ▸ start")
        self._refresh_status()

    def _on_step_terminal(self, subtask_id: str, stage: str, data: dict) -> None:
        step_id = str(data.get("step_id") or "")
        skill = _event_label(data.get("skill") or step_id, "workflow step")
        key = (subtask_id, step_id)
        label = self._step_index.get(key, "[step]")
        started = self._step_started.pop(key, 0.0)
        elapsed = f" [dim]· {time.monotonic() - started:.1f}s[/dim]" if started else ""
        outcome = stage.rsplit(".", 1)[-1]
        glyph = {
            "done": "[green]✓[/green]",
            "degraded": "[yellow]⚠[/yellow]",
            "failed": "[red]✗[/red]",
            "skipped": "[dim]∅[/dim]",
            "restored": "[dim]⤳[/dim]",
            "retry": "[yellow]⟳[/yellow]",
            "resume": "[cyan]⟳[/cyan]",
        }.get(outcome, "[dim]·[/dim]")
        error = _detail(data.get("error") or data.get("skip_reason"), limit=160)
        echo = (
            (not self._raw_trace)
            and bool(error)
            and outcome in {"failed", "degraded", "skipped"}
            and self._message_shown(error)
        )
        suffix = (
            f" · {escape(error)}"
            if error and outcome in {"failed", "degraded", "skipped"} and not echo
            else ""
        )
        if self._plan_checklist is not None:
            index = data.get("index")
            if not isinstance(index, int):
                index = self._step_num.get(key)
            self._mark_step(index, skill, _STEP_OUTCOME_STATE.get(outcome, "done"))
            self._render_checklist()
            # Keep a detail line only when a step needs the operator's attention.
            if suffix:
                self._line(f"    {glyph} [bold]{escape(skill)}[/bold] {escape(outcome)}{suffix}")
                self._remember_msg(error)
        else:
            self._line(
                f"    {glyph} [cyan]{escape(label)}[/cyan] [bold]{escape(skill)}[/bold] "
                f"{escape(outcome)}{elapsed}{suffix}"
            )
            if suffix:
                self._remember_msg(error)
        self._refresh_status()

    def _on_nested_tool(self, stage: str, data: dict) -> None:
        tool = _event_label(tool_event_name(data), _UNNAMED_TOOL)
        if stage.endswith(".start") or stage == "tool.start":
            args = _args_summary(data.get("arguments"), self.verbosity, nested=True)
            suffix = f"[dim]({args})[/dim]" if args else ""
            markup = f"      [magenta]↳ ⚙[/magenta] {escape(tool)}{suffix}"
            if self._raw_trace:
                self._line(markup)
            elif self._active and (
                (self._active.kind == "error" and self._message_shown(self._active.msg))
                or (self._active.kind == "start" and self._active.name == tool)
            ):
                return
            else:
                self._hold_start(tool, markup)
            return
        error = str(data.get("error") or "")
        status = str(data.get("status") or "")
        duration = _fmt_duration(data.get("duration_ms"))
        result = data.get("result")
        command_status = _command_status(result)
        transport_failed = status in {"failed", "timed_out", "cancelled", "rejected"}
        if error or transport_failed:
            glyph = "[yellow]↳ ∅[/yellow]" if status == "rejected" else "[red]↳ ✗[/red]"
            message = error or status
            if not self._raw_trace and self._message_shown(message):
                return
            self._drop_start(tool)
            self._line(
                f"      {glyph} {escape(tool)} · [red]{escape(_detail(message, limit=140))}[/red]"
            )
            self._remember_msg(message)
        elif command_status and command_status != "succeeded":
            glyph = (
                "[red]↳ ✗[/red]"
                if command_status in {"failed", "timed_out"}
                else "[yellow]↳ ∅[/yellow]"
            )
            preview = _result_preview(result, self.verbosity)
            command = _command_snippet(data.get("arguments"), self.verbosity)
            detail = f" {escape(command)}" if command else ""
            if preview:
                detail = f"{detail} · {escape(preview)}" if detail else f" · {escape(preview)}"
            message = f"{command}|{preview or command_status}"
            if not self._raw_trace and self._message_shown(message):
                return
            self._drop_start(tool)
            markup = f"      {glyph} {escape(tool)}{detail}"
            body = _command_failure_body(result, self.verbosity)
            if body:
                markup = f"{markup}\n{body}"
            self._line(markup, foldable=markup.count("\n") + 1 > TOOL_CALL_MAX_LINES)
            self._remember_msg(message)
        elif self._diag:
            detail = f" [dim]· {duration}[/dim]" if duration else ""
            self._line(f"      [green]↳ ✓[/green] {escape(tool)}{detail}")
        else:
            self._drop_start(tool)

    def _on_skill_stage(self, stage: str, data: dict) -> None:
        outcome = stage.rsplit(".", 1)[-1]
        skill = _event_label(data.get("skill"), "skill")
        if outcome == "error":
            self._line(f"      [red]↳ ✗ skill {escape(skill)} errored[/red]")
        elif self._diag:
            self._line(f"      [dim]↳ skill {escape(skill)} {escape(outcome)}[/dim]")

    # ── status line ──

    def _status_text(self) -> Text:
        """The whole dynamic region as one renderable (≤3 newline-joined lines).

        Kept a single ``Text`` so ``.plain`` still yields the flat string the TUI
        path forwards and the classic Rich ``Status`` renders in place. The first
        line carries the stage/elapsed/task identity the rest of the code (and
        tests) read; lines 2-3 are the redesign's "current item" and heartbeat.
        """
        return Text("\n").join(self._status_lines())

    def _status_lines(self) -> list[Text]:
        elapsed = time.monotonic() - self._started
        head = Text()
        head.append(self._stage, style="cyan")
        head.append(f" · {elapsed:.0f}s", style="dim")
        if self._tools_started:
            head.append(f" · tools {self._tools_done}/{self._tools_started}", style="dim")
        if self._usage_tokens or self._usage_cost:
            from omni.agent.cost import format_tokens

            head.append(f" · {format_tokens(self._usage_tokens)} tok", style="dim")
            if self._usage_cost:
                head.append(f" · ${self._usage_cost:.4f}", style="dim")
        if self._task_id:
            head.append(f" · task {self._task_id[:8]}", style="dim")
        lines = [head]
        if self._current_item:
            current = Text()
            current.append("current: ", style="dim")
            current.append(one_line(self._current_item, _CURRENT_ITEM_LIMIT), style="dim")
            lines.append(current)
        heartbeat = self._heartbeat_line(elapsed)
        if heartbeat is not None and len(lines) < _STATUS_MAX_LINES:
            lines.append(heartbeat)
        return lines[:_STATUS_MAX_LINES]

    def _heartbeat_line(self, elapsed: float) -> Text | None:
        """A long-turn "still moving" hint, so slow reads apart from stalled.

        Only appears once a turn has run long enough to raise the question, and
        colours the gap amber when progress has itself gone quiet for a while.
        """
        if elapsed < _HEARTBEAT_AFTER_S:
            return None
        since = max(0.0, time.monotonic() - self._last_progress_at)
        line = Text()
        line.append("recent progress ", style="dim")
        line.append(f"{since:.0f}s ago", style="yellow" if since >= _HEARTBEAT_AFTER_S else "dim")
        return line

    def _resume_status(self) -> None:
        if not self._status_enabled or self._status is not None:
            return
        if set_output_status(self._status_text().plain):
            return
        try:
            self._status = console.status(_StatusText(self), spinner="dots")
            self._status.start()
        except Exception:  # noqa: BLE001 - a broken live display must never kill a turn
            self._status = None
            self._status_enabled = False

    def _pause_status(self) -> None:
        set_output_status("")
        if self._status is None:
            return
        try:
            self._status.stop()
        except Exception:  # noqa: BLE001
            pass
        self._status = None

    def _refresh_status(self) -> None:
        # The status text is a dynamic renderable; nothing to push. Kept as a
        # hook so a non-dynamic fallback only needs this one method changed.
        if self._status_enabled:
            set_output_status(self._status_text().plain)
        return

    # ── output helper ──

    def _message_shown(self, message: str) -> bool:
        msg = _squash(message)
        if not msg:
            return False
        if self._active is not None and self._active.msg == msg:
            return True
        return msg in self._shown_msgs

    def _remember_msg(self, message: str) -> None:
        msg = _squash(message)
        if msg:
            self._shown_msgs.add(msg)

    def _hold_start(self, name: str, markup: str) -> None:
        self._commit_active()
        self._active = _ActiveCell(name=name, kind="start", fp=f"start|{name}", msg="", line=markup)
        self._emit_cell(markup, commit=False)

    def _hold_error(self, name: str, message: str, markup: str) -> None:
        msg = _squash(message)
        fp = f"{name}|{msg}"
        if self._active and self._active.kind == "error" and self._active.fp == fp:
            self._active.count += 1
            self._emit_cell(self._active.render(), commit=False)
            return
        if msg and msg in self._shown_msgs:
            return
        if self._active and self._active.kind == "start" and self._active.name == name:
            self._active = None
        else:
            self._commit_active()
        self._active = _ActiveCell(name=name, kind="error", fp=fp, msg=msg, line=markup)
        self._emit_cell(markup, commit=False)

    def _drop_start(self, name: str) -> None:
        if self._active and self._active.kind == "start" and self._active.name == name:
            self._active = None
            self._drop_trace_slot()

    def _drop_trace_slot(self) -> None:
        publish_transcript_event(
            TranscriptEvent(
                kind=TranscriptKind.TOOL_CARD,
                payload="",
                replace_key=TRACE_REPLACE_KEY,
                state=TRACE_DROP_STATE,
            ),
            stream=console.file,
        )

    def _commit_active(self) -> None:
        cell = self._active
        if cell is None:
            return
        self._active = None
        if cell.msg:
            self._shown_msgs.add(cell.msg)
        markup = cell.render()
        foldable = markup.count("\n") + 1 > TOOL_CALL_MAX_LINES
        self._emit_cell(markup, commit=True, foldable=foldable)

    def _emit_cell(self, markup: str, *, commit: bool, foldable: bool = False) -> None:
        if self._stream_open:
            console.print()
            self._stream_open = False
        event = TranscriptEvent(
            kind=TranscriptKind.TOOL_CARD,
            payload=markup + "\n",
            replace_key=TRACE_REPLACE_KEY,
            state=TRACE_COMMIT_STATE if commit else "",
            foldable=foldable,
            initially_collapsed=foldable,
        )
        if publish_transcript_event(event, stream=console.file):
            self._resume_status()
            return
        if commit:
            console.print(markup)
        self._resume_status()

    def _line(
        self,
        markup: str,
        *,
        replace_key: str = "",
        foldable: bool = False,
        initially_collapsed: bool = False,
    ) -> None:
        if not replace_key:
            self._commit_active()
        if self._stream_open:
            console.print()  # close the partial streamed line first
            self._stream_open = False
        # Publish the Rich markup itself (not a colour-stripped ``.plain``): the
        # transcript renderer parses it into coloured prompt_toolkit fragments so
        # ``⚙``/``✓``/``◷`` keep their semantic colours in the managed TUI.
        if publish_transcript_event(
            TranscriptEvent(
                kind=TranscriptKind.STATUS,
                payload=markup + "\n",
                replace_key=replace_key,
                foldable=foldable,
                initially_collapsed=initially_collapsed,
            ),
            stream=console.file,
        ):
            self._resume_status()
            return
        console.print(markup)
        self._resume_status()


# ── formatting helpers ──

# File-edit tools whose arguments carry enough to reconstruct a diff for a
# coloured cell — derived at the render layer only (no tool-side changes).
_DIFF_TOOLS = ("edit_file", "write_file")
_DIFF_MAX_LINES = 24  # cap the body so a huge write does not flood the transcript
_DIFF_VERBOSE_MAX_LINES = 120
_WRITE_INLINE_MAX_LINES = 12
_WRITE_INLINE_MAX_CHARS = 2_000


def _diff_cell(name: str, arguments: Any, verbosity: str) -> tuple[str, bool, bool] | None:
    """Build a coloured unified-diff cell for an edit/write tool result.

    ``edit_file`` supplies ``old_string``/``new_string`` and ``write_file`` a
    new ``contents`` blob — both already ride on the tool event, so this stays
    a pure rendering concern. Returns markup plus semantic folding flags, or
    ``None`` to skip. Large newly-written files are inventory items in normal
    mode, not transcript content; verbose mode retains a bounded, collapsible
    diagnostic diff.
    """
    if verbosity == "quiet" or name not in _DIFF_TOOLS:
        return None
    if not isinstance(arguments, dict):
        return None
    path = _squash(str(arguments.get("path") or "")) or "(file)"
    if name == "edit_file":
        old = str(arguments.get("old_string", "")).splitlines()
        new = str(arguments.get("new_string", "")).splitlines()
        if old == new:
            return None
    else:  # write_file: render new content as an all-added block.
        contents = str(arguments.get("contents", ""))
        if not contents:
            return None
        old, new = [], contents.splitlines()
    body = [
        line
        for line in difflib.unified_diff(old, new, lineterm="", n=1)
        if not line.startswith(("--- ", "+++ "))
    ]
    if not body:
        return None
    added = sum(1 for line in body if line.startswith("+"))
    removed = sum(1 for line in body if line.startswith("-"))
    rows = [f"    [bold]±[/bold] {escape(path)} [green]+{added}[/green] [red]-{removed}[/red]"]
    large_write = name == "write_file" and (
        len(new) > _WRITE_INLINE_MAX_LINES
        or len(str(arguments.get("contents", ""))) > _WRITE_INLINE_MAX_CHARS
    )
    if large_write and verbosity != "verbose":
        rows.append("    [dim]large new file; content omitted from the transcript[/dim]")
        return "\n".join(rows), False, False

    max_lines = _DIFF_VERBOSE_MAX_LINES if verbosity == "verbose" else _DIFF_MAX_LINES
    for line in body[:max_lines]:
        rows.append(_diff_row(line))
    hidden = len(body) - max_lines
    if hidden > 0:
        rows.append(f"    [dim]… +{hidden} more diff line(s)[/dim]")
    foldable = verbosity == "verbose" and len(body) > _DIFF_MAX_LINES
    return "\n".join(rows), foldable, foldable


def _diff_row(line: str) -> str:
    """Colour one unified-diff line (green add / red remove / cyan hunk / dim ctx)."""
    if line.startswith("@@"):
        return f"    [cyan]{escape(line)}[/cyan]"
    if line.startswith("+"):
        return f"    [green]{escape(line)}[/green]"
    if line.startswith("-"):
        return f"    [red]{escape(line)}[/red]"
    return f"    [dim]{escape(line)}[/dim]"


def _squash(text: str) -> str:
    return " ".join(text.split())


def _detail(value: Any, *, limit: int) -> str:
    """One line of failure prose, admitting it when the line had to be cut.

    Every one of these lines used to end in a bare slice, and the tail a slice
    takes is where a message puts the command to run: a missing-key failure
    stopped at ``omni config set research.se`` with nothing to say it had been
    cut, so the reader could neither use the hint nor know one was there.
    """
    return one_line(value, limit)


def _event_label(value: Any, fallback: str) -> str:
    """Replace missing event metadata without ever printing a bare question mark."""

    label = _squash(str(value or ""))
    if label.casefold() in {"", "?", "unknown", "none", "null"}:
        return fallback
    return label


def _fmt_progress(value: Any) -> str:
    try:
        fraction = float(value)
    except (TypeError, ValueError):
        return ""
    if fraction < 0.0 or fraction > 1.0:
        return ""
    return f"{fraction:.0%}"


def _task_terminal_detail(data: dict[str, Any]) -> tuple[str, str]:
    """Extract one concise cause and one saved artifact from a terminal event."""

    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    detail = next(
        (
            _squash(str(value))
            for value in (
                data.get("error"),
                result.get("error"),
                result.get("warning"),
                result.get("summary"),
            )
            if str(value or "").strip()
        ),
        "",
    )
    if not detail and isinstance(result.get("outcome"), dict):
        detail = _squash(str(result["outcome"].get("code") or ""))
    artifact_path = ""
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_path = str(artifact.get("path") or artifact.get("uri") or "").strip()
            if artifact_path:
                break
    # Truncate the way _detail does: a bare slice ends exactly where a message
    # puts the command to run, with nothing to say the line had been cut.
    return shorten(detail, 160), artifact_path


def _fmt_duration(ms: Any) -> str:
    try:
        value = float(ms or 0.0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    if value < 1000:
        return f"{value:.0f}ms"
    return f"{value / 1000:.1f}s"


def _mask_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS):
        return "***"
    return value


def _fmt_value(value: Any, limit: int) -> str:
    if isinstance(value, str):
        text = _squash(value)
    elif isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _args_summary(arguments: Any, verbosity: str, *, nested: bool = False) -> str:
    if not isinstance(arguments, dict) or not arguments:
        return ""
    total_limit = _ARGS_LIMIT.get(verbosity, 90)
    value_limit = _VALUE_LIMIT.get(verbosity, 40)
    if nested and verbosity != "verbose":
        total_limit, value_limit = 60, 30
    parts: list[str] = []
    used = 0
    for key, raw in arguments.items():
        rendered = _fmt_value(_mask_value(str(key), raw), value_limit)
        piece = f"{key}={rendered}"
        if used + len(piece) > total_limit:
            parts.append("…")
            break
        parts.append(piece)
        used += len(piece) + 2
    return escape(", ".join(parts))


def _command_snippet(arguments: Any, verbosity: str) -> str:
    """Keep the argv on a failed bash cell so 126 is actionable."""
    if not isinstance(arguments, dict):
        return ""
    command = arguments.get("command")
    if not command:
        return ""
    return _fmt_value(command, _COMMAND_SNIPPET_LIMIT.get(verbosity, 48))


def _command_failure_body(result: Any, verbosity: str) -> str:
    """Head+tail of the process text when one preview line is not enough."""
    if not isinstance(result, dict):
        return ""
    stderr = str(result.get("stderr") or "")
    output = str(result.get("output") or "")
    output_lines = [line for line in (_squash(raw) for raw in output.splitlines()) if line]
    stderr_lines = [line for line in (_squash(raw) for raw in stderr.splitlines()) if line]
    lines = output_lines if len(output_lines) > 1 else stderr_lines
    if len(lines) <= 1:
        return ""
    if verbosity == "verbose":
        keep = _COMMAND_BODY_VERBOSE
        head, tail = keep // 2, keep - keep // 2
    else:
        head, tail = _COMMAND_BODY_HEAD, _COMMAND_BODY_TAIL
    if len(lines) <= head + tail:
        shown = lines
        omitted = 0
    else:
        shown = [*lines[:head], *lines[-tail:]]
        omitted = len(lines) - head - tail
    rows: list[str] = []
    split_at = head if omitted else len(shown)
    for index, line in enumerate(shown):
        if omitted and index == split_at:
            rows.append(f"    [dim]… +{omitted} line(s)[/dim]")
        rows.append(f"    [dim]{escape(line)}[/dim]")
    return "\n".join(rows)


def _result_preview(result: Any, verbosity: str) -> str:
    limit = _PREVIEW_LIMIT.get(verbosity, 110)
    if result is None or result == "":
        return "done"
    if isinstance(result, dict):
        if result.get("task_id") and result.get("task_status"):
            return (
                f"Task {str(result['task_id'])[:8]} status: "
                f"{result['task_status']}"
            )[:limit]
        if result.get("subtask_id") and result.get("subtask_status"):
            return (
                f"Subtask {str(result['subtask_id'])[:8]} status: "
                f"{result['subtask_status']}"
            )[:limit]
        status = command_result_status(result)
        output = str(result.get("output") or "").strip()
        stderr = str(result.get("stderr") or "")
        if status == "succeeded" and output:
            return _squash(output)[:limit]
        if status == "failed":
            exit_code = result.get("exit_code")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                preview = command_exit_summary(exit_code, output, stderr)
                if ": " in preview or not str(result.get("summary") or "").strip():
                    return _squash(preview)[:limit]
        for key in ("summary", "message", "text", "title", "note"):
            value = str(result.get(key) or "").strip()
            if value:
                return _squash(value)[:limit]
        if result.get("status") and len(result) <= 3:
            return f"status={result['status']}"
        keys = ", ".join(str(k) for k in list(result)[:6])
        return f"keys: {keys}"[:limit]
    if isinstance(result, list):
        return f"{len(result)} item(s)"
    return _squash(str(result))[:limit]


def _command_status(result: Any) -> str:
    """Return a structured process outcome without parsing legacy display text."""
    return command_result_status(result) or ""
