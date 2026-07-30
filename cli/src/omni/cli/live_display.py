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

import difflib
import json
import time
from typing import Any

from rich.markup import escape
from rich.text import Text

from omni.cli.render import console
from omni.cli.repl_output import publish_transcript_event, set_output_status
from omni.cli.repl_transcript import ANSWER_REPLACE_KEY, TranscriptEvent, TranscriptKind
from omni.core.tool_result import command_result_status

VERBOSITY_LEVELS = ("quiet", "normal", "verbose")

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


def resolve_verbosity(settings: Any, *, quiet: bool = False, verbose: bool = False) -> str:
    """CLI-flag precedence: --quiet > --verbose > config display.verbosity."""
    if quiet:
        return "quiet"
    if verbose:
        return "verbose"
    configured = str(getattr(getattr(settings, "display", None), "verbosity", "") or "normal").lower()
    return configured if configured in VERBOSITY_LEVELS else "normal"


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
    ) -> None:
        self.verbosity = verbosity if verbosity in VERBOSITY_LEVELS else "normal"
        self._status_enabled = (
            bool(status_line) and self.verbosity != "quiet" and console.is_terminal
        )
        self._status: Any = None
        self._started = time.monotonic()
        self._stage = "planning"
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

    # ── lifecycle ──

    def begin(self, stage: str = "planning") -> None:
        self._started = time.monotonic()
        self._stage = stage
        self._resume_status()

    def end(self) -> float:
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
        if phase == "plan":
            self._on_plan(data)
        elif phase == "start":
            self._on_tool_start(data)
        elif phase == "done":
            self._on_tool_done(data)
        elif phase == "budget":
            self._on_budget(data)
        elif phase == "transcript":
            if self.verbosity == "verbose":
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
            if self.verbosity == "verbose":
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
            if self.verbosity == "verbose" and summary:
                self._line(f"[dim]· context: {summary}[/dim]")
        elif event == "plan.executed":
            subtask_id = str(data.get("subtask_id") or payload.get("subtask_id") or "")
            suffix = f" execution {escape(subtask_id[:8])}" if subtask_id else ""
            self._line(f"[dim]· dispatched {name}{suffix}[/dim]")
        elif event == "input.resolved":
            # "Look up before ask/error": narrate the in-lane resolution instead
            # of surfacing the skill's missing-input error.
            self._line(f"[cyan]🔎[/cyan] {summary}")
        elif self.verbosity == "verbose" and summary:
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
        header = (
            f"[bold cyan]◆[/bold cyan] plan [bold]{escape(self._plan_name)}[/bold]"
            f" · {len(self._plan_checklist)} step(s)"
        )
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
        if finding_state == "resolved" and self.verbosity != "verbose":
            return
        if action in {"", "execute"} and not notes:
            return  # clean pass-through: nothing worth a line
        rung = str(payload.get("rung") or "")
        head = f"[yellow]↷[/yellow] recovery [bold]{escape(action)}[/bold]"
        if rung:
            head += f" [dim]({escape(rung)})[/dim]"
        self._line(head)
        for note in notes[:4]:
            self._line(f"  [yellow]! {escape(_squash(note))}[/yellow]")

    def _on_tool_start(self, data: dict) -> None:
        name = str(data.get("name") or "?")
        self._tools_started += 1
        self._stage = name
        args = _args_summary(data.get("arguments"), self.verbosity)
        suffix = f"[dim]({args})[/dim]" if args else ""
        self._line(f"  [magenta]⚙[/magenta] [bold]{escape(name)}[/bold]{suffix}")
        self._refresh_status()

    def _on_tool_done(self, data: dict) -> None:
        name = str(data.get("name") or "?")
        self._tools_done += 1
        self._stage = "thinking…"
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
            self._line(f"  {glyph} [bold]{escape(name)}[/bold]{detail} · {escape(_squash(message)[:160])}")
        elif command_status and command_status != "succeeded":
            glyph = (
                "[red]✗[/red]"
                if command_status in {"failed", "timed_out"}
                else "[yellow]∅[/yellow]"
            )
            preview = _result_preview(result, self.verbosity)
            parts = [f"  {glyph} [bold]{escape(name)}[/bold]"]
            if duration:
                parts.append(f"[dim]· {duration}[/dim]")
            if preview:
                parts.append(f"[dim]· {escape(preview)}[/dim]")
            self._line(" ".join(parts))
        else:
            preview = _result_preview(result, self.verbosity)
            parts = [f"  [green]✓[/green] [bold]{escape(name)}[/bold]"]
            if duration:
                parts.append(f"[dim]· {duration}[/dim]")
            if preview:
                parts.append(f"[dim]· {escape(preview)}[/dim]")
            self._line(" ".join(parts))
            diff = _diff_cell(name, data.get("arguments"), self.verbosity)
            if diff:
                self._line(diff)
        self._refresh_status()

    def _on_budget(self, data: dict) -> None:
        reason = str(data.get("reason") or "budget")
        self._line(f"[yellow]![/yellow] tool budget reached ({escape(reason)}); pending calls were rejected")
        if self.verbosity == "verbose":
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
        skill = str(data.get("skill") or "?")
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
            mark = "[green]✓[/green]" if status == "succeeded" else "[red]✗[/red]"
            self._stage = "thinking…"
            self._line(
                f"  {mark} workflow {escape(status or 'failed')}"
                f" [dim]workflow={escape(workflow_run_id[:8])}{owner}{elapsed}[/dim]"
            )
            self._refresh_status()
            return
        subtask_id = str(data.get("subtask_id") or "")
        skill = str(data.get("skill") or "?")
        status = str(data.get("status") or "")
        elapsed = ""
        started = self._task_started.pop(subtask_id, 0.0)
        if started:
            elapsed = f" · {time.monotonic() - started:.1f}s"
        self._stage = "thinking…"
        if status == "succeeded":
            self._line(
                f"  [green]✓[/green] execution [bold]{escape(skill)}[/bold] succeeded"
                f"[dim]{elapsed} · execution={escape(subtask_id[:8])}{owner}[/dim]"
            )
        else:
            error = _squash(str(data.get("error") or ""))[:160]
            self._line(
                f"  [red]✗[/red] execution [bold]{escape(skill)}[/bold] {escape(status or 'failed')}"
                f" [dim]execution={escape(subtask_id[:8])}{owner}{elapsed}[/dim]"
                + (f" · {escape(error)}" if error else "")
            )
        self._refresh_status()

    def _on_task_progress(self, data: dict) -> None:
        stage = str(data.get("stage") or "")
        subtask_id = str(data.get("subtask_id") or "")
        if stage == "workflow.start":
            total = data.get("total_steps")
            goal = _squash(str(data.get("goal") or ""))[:90]
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
        elif self.verbosity == "verbose":
            self._line(f"      [dim]↳ {escape(stage)}[/dim]")

    def _on_step_start(self, subtask_id: str, data: dict) -> None:
        step_id = str(data.get("step_id") or "")
        skill = str(data.get("skill") or step_id or "?")
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
        skill = str(data.get("skill") or step_id or "?")
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
        error = _squash(str(data.get("error") or data.get("skip_reason") or ""))[:140]
        suffix = f" · {escape(error)}" if error and outcome in {"failed", "degraded", "skipped"} else ""
        if self._plan_checklist is not None:
            index = data.get("index")
            if not isinstance(index, int):
                index = self._step_num.get(key)
            self._mark_step(index, skill, _STEP_OUTCOME_STATE.get(outcome, "done"))
            self._render_checklist()
            # Keep a detail line only when a step needs the operator's attention.
            if suffix:
                self._line(f"    {glyph} [bold]{escape(skill)}[/bold] {escape(outcome)}{suffix}")
        else:
            self._line(
                f"    {glyph} [cyan]{escape(label)}[/cyan] [bold]{escape(skill)}[/bold] "
                f"{escape(outcome)}{elapsed}{suffix}"
            )
        self._refresh_status()

    def _on_nested_tool(self, stage: str, data: dict) -> None:
        tool = str(data.get("tool") or "?")
        if stage.endswith(".start") or stage == "tool.start":
            args = _args_summary(data.get("arguments"), self.verbosity, nested=True)
            suffix = f"[dim]({args})[/dim]" if args else ""
            self._line(f"      [magenta]↳ ⚙[/magenta] {escape(tool)}{suffix}")
        else:
            error = str(data.get("error") or "")
            status = str(data.get("status") or "")
            duration = _fmt_duration(data.get("duration_ms"))
            result = data.get("result")
            command_status = _command_status(result)
            transport_failed = status in {"failed", "timed_out", "cancelled", "rejected"}
            if error or transport_failed:
                glyph = "[yellow]↳ ∅[/yellow]" if status == "rejected" else "[red]↳ ✗[/red]"
                message = error or status
                self._line(f"      {glyph} {escape(tool)} · {escape(_squash(message)[:120])}")
            elif command_status and command_status != "succeeded":
                glyph = (
                    "[red]↳ ✗[/red]"
                    if command_status in {"failed", "timed_out"}
                    else "[yellow]↳ ∅[/yellow]"
                )
                preview = _result_preview(result, self.verbosity)
                detail = f" · {escape(preview)}" if preview else ""
                self._line(f"      {glyph} {escape(tool)}{detail}")
            elif self.verbosity == "verbose":
                detail = f" [dim]· {duration}[/dim]" if duration else ""
                self._line(f"      [green]↳ ✓[/green] {escape(tool)}{detail}")

    def _on_skill_stage(self, stage: str, data: dict) -> None:
        outcome = stage.rsplit(".", 1)[-1]
        skill = str(data.get("skill") or "?")
        if outcome == "error":
            self._line(f"      [red]↳ ✗ skill {escape(skill)} errored[/red]")
        elif self.verbosity == "verbose":
            self._line(f"      [dim]↳ skill {escape(skill)} {escape(outcome)}[/dim]")

    # ── status line ──

    def _status_text(self) -> Text:
        elapsed = time.monotonic() - self._started
        text = Text()
        text.append(self._stage, style="cyan")
        text.append(f" · {elapsed:.0f}s", style="dim")
        if self._tools_started:
            text.append(f" · tools {self._tools_done}/{self._tools_started}", style="dim")
        return text

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

    def _line(self, markup: str, *, replace_key: str = "") -> None:
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


def _diff_cell(name: str, arguments: Any, verbosity: str) -> str:
    """Build a coloured unified-diff cell for an edit/write tool result.

    ``edit_file`` supplies ``old_string``/``new_string`` and ``write_file`` a
    new ``contents`` blob — both already ride on the tool event, so this stays
    a pure rendering concern. Returns Rich markup (multi-line) or "" to skip.
    """
    if verbosity == "quiet" or name not in _DIFF_TOOLS:
        return ""
    if not isinstance(arguments, dict):
        return ""
    path = _squash(str(arguments.get("path") or "")) or "(file)"
    if name == "edit_file":
        old = str(arguments.get("old_string", "")).splitlines()
        new = str(arguments.get("new_string", "")).splitlines()
        if old == new:
            return ""
    else:  # write_file: render new content as an all-added block.
        contents = str(arguments.get("contents", ""))
        if not contents:
            return ""
        old, new = [], contents.splitlines()
    body = [
        line
        for line in difflib.unified_diff(old, new, lineterm="", n=1)
        if not line.startswith(("--- ", "+++ "))
    ]
    if not body:
        return ""
    added = sum(1 for line in body if line.startswith("+"))
    removed = sum(1 for line in body if line.startswith("-"))
    rows = [f"    [bold]±[/bold] {escape(path)} [green]+{added}[/green] [red]-{removed}[/red]"]
    for line in body[:_DIFF_MAX_LINES]:
        rows.append(_diff_row(line))
    hidden = len(body) - _DIFF_MAX_LINES
    if hidden > 0:
        rows.append(f"    [dim]… +{hidden} more diff line(s)[/dim]")
    return "\n".join(rows)


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


def _result_preview(result: Any, verbosity: str) -> str:
    limit = _PREVIEW_LIMIT.get(verbosity, 110)
    if result is None or result == "":
        return "done"
    if isinstance(result, dict):
        if command_result_status(result) == "succeeded":
            output = str(result.get("output") or "").strip()
            if output:
                return _squash(output)[:limit]
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
