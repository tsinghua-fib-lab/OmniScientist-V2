"""Shared one-shot turn runner (used by ``chat``, ``exec``, ``session resume``).

Keeping this out of ``cli/main.py`` lets subcommand modules reuse the exact same
agent lifecycle + rendering without importing the top-level Typer app (which
would be a circular import).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omni.cli import theme
from omni.cli.live_display import TurnDisplay, resolve_debug, resolve_verbosity
from omni.cli.render import (
    action_card,
    artifact_line,
    artifact_preview,
    assistant_answer,
    console,
    data_table,
    error,
    error_card,
    info,
    warn,
)
from omni.cli.repl_output import set_output_status
from omni.cli.state import AppState, make_agent
from omni.core.file_mentions import resolve_turn_attachments
from omni.core.termination import (
    BUDGET_EXHAUSTED_REASONS,
    base_termination_reason,
    is_bounded_termination,
    termination_reason_label,
)
from omni.runtime.presentation import (
    TaskPresentation,
    TurnPresentation,
    presentable_artifacts,
    project_artifact_locations,
    turn_presentation_from_result,
)
from omni.runtime.turn_outcome import (
    classify_turn_outcome,
    display_warnings,
    informational_host_fill_notes,
)

# Reasons that describe an intended end of turn: the model answered, asked, or
# handed the work off. Anything outside this set is reported, so a new reason
# code can never be introduced into a silent path by omission.
_CLEAN_TERMINATIONS = frozenset({
    "",
    "done",
    "needs_input",
    "escalated",
    "terminal_tool_result",
    "cancelled",
    "interrupted",
})


def task_ack_cb(quiet: bool):  # noqa: ANN201
    def cb(data: dict) -> None:
        if quiet:
            return
        task_id = str(data.get("task_id") or "")
        if task_id:
            if set_output_status(f"planning · task {task_id[:8]}"):
                return
            info(f"Request received: task_id={task_id[:8]}; planning...")

    return cb


def _render_task_presentation(
    presentation: TaskPresentation,
    *,
    shell_commands: bool = False,
    session_id: str = "",
    show_artifact_inventory: bool = True,
) -> None:
    if presentation.summary:
        console.print(f"  {presentation.summary[:300]}")
    _render_task_evidence(
        presentation,
        show_artifact_inventory=show_artifact_inventory,
    )
    if presentation.next_actions:
        console.print("  [bold cyan]Next actions[/bold cyan]")
        for action in _channel_actions(presentation.next_actions, shell_commands=shell_commands, session_id=session_id):
            console.print(f"    • {action}")


def _render_task_evidence(
    presentation: TaskPresentation,
    *,
    show_artifact_inventory: bool = True,
) -> None:
    """The files and records a task produced, independent of how it ended.

    Split out because a task that stopped for input is announced by a card that
    already carries its summary and next actions, yet may still have written
    something worth keeping.
    """
    visible_artifacts = presentable_artifacts(presentation.artifacts)
    if visible_artifacts:
        if show_artifact_inventory:
            console.print(f"  [{theme.STRONG} {theme.ACCENT}]Artifacts[/]")
            for art in visible_artifacts:
                artifact_line(art.title, art.target, indent="    ")
        for art in visible_artifacts:
            if not art.preview:
                continue
            artifact_preview(
                art.title,
                art.preview,
                markdown=art.is_markdown,
                hint=(
                    f"Preview truncated; open with open_artifact {art.uri or art.target}"
                    if art.preview_truncated
                    else ""
                ),
            )
    if presentation.research.has_any:
        console.print("  [bold cyan]Research record[/bold cyan]")
        if presentation.research.run_id:
            console.print(f"    • run: {presentation.research.run_id[:8]}")
        if presentation.research.source_ids:
            console.print(f"    • sources: {', '.join(str(s)[:8] for s in presentation.research.source_ids[:5])}")
        if presentation.research.claim_ids:
            console.print(f"    • claims: {', '.join(str(c)[:8] for c in presentation.research.claim_ids[:5])}")
        if presentation.research.evidence_ids:
            console.print(f"    • evidence: {', '.join(str(e)[:8] for e in presentation.research.evidence_ids[:5])}")


def should_suppress_assistant_text(turn) -> bool:  # noqa: ANN001
    reason = base_termination_reason(str(getattr(turn, "terminated_reason", "") or ""))
    if str(getattr(turn, "kind", "") or "") == "error" and reason == "llm_auth_error":
        return True
    if not getattr(turn, "drained_results", None):
        return False
    if _assistant_text_repeats_a_result(turn):
        return True
    if not any(d.get("status") == "succeeded" for d in turn.drained_results):
        return False
    return reason in {"max_iterations", "max_tool_calls"}


def _assistant_text_repeats_a_result(turn) -> bool:  # noqa: ANN001
    """Whether the result card is about to print the sentence the answer just did.

    A skill that stops for input has one message, and three layers each treat it
    as theirs: the runtime projects it into the turn text, and the presentation
    layer carries it as the task summary. Printing both put the same paragraph
    on screen twice, so the card — which also carries the command that clears
    the state — is the copy worth keeping.
    """
    text = str(getattr(turn, "text", "") or "").strip()
    if not text:
        return False
    return any(
        task.summary.strip() == text
        for task in turn_presentation_from_result(turn).tasks
    )


def _render_task_hint(task_id: str) -> None:
    """Surface the owning task id + an inspect affordance (never leaves it hidden)."""
    tid = str(task_id or "")[:8]
    if tid:
        info(f"Task {tid} · inspect the full trace with /task show {tid}")


def render_turn_diagnostics(turn) -> None:  # noqa: ANN001
    """Make bounded/failed turns explicit, and always surface the owning task id.

    A wall-clock stop is no longer a failure: the loop forces a best-effort
    synthesis and settles ``degraded`` (reason ``timeout``/``stalled``). So we
    do not print "Turn not completed" for those — we note the best-effort reason
    and keep the task id visible so the full trace stays one command away.

    Coverage is closed rather than enumerated: only the reasons that describe an
    intended end of turn stay silent, so adding a new stop cause cannot make a
    turn end without saying anything.
    """
    kind = str(getattr(turn, "kind", "") or "")
    raw_reason = str(getattr(turn, "terminated_reason", "") or "")
    reason = raw_reason or "unknown"
    task_id = str(getattr(turn, "task_id", "") or "")
    base = base_termination_reason(raw_reason)
    if kind == "error":
        if base == "llm_auth_error":
            error_card(
                "Model authentication failed",
                "The configured provider rejected the active credential, so this turn stopped.",
                actions=(
                    "Check it with `/config test` in the REPL or `omni config test` in the shell.",
                    "Update it with `/config model ...` or `omni config model ...`.",
                ),
            )
            return
        warn(f"Turn not completed: {termination_reason_label(reason)}.")
        trace = list(getattr(turn, "tool_trace", []) or [])
        if trace:
            last = trace[-1]
            name = str(getattr(last, "name", "") or "")
            err = str(getattr(last, "error", "") or "")
            status = str(getattr(last, "status", "") or "").lower()
            # A successful trailing update_plan is not the cause. Only name the
            # last tool when that record itself failed (Codex: checklist ≠ verdict).
            if err or status in {"failed", "error"}:
                detail = f"{name}: {err[:160]}" if err else name
                if detail:
                    warn(f"Last step: {detail}")
        _render_task_hint(task_id)
        return
    # Every non-clean stop gets a line. A bounded stop already delivered a
    # best-effort answer above, but saying nothing about *why* it stopped is
    # what makes a turn read as "it just went quiet". Budget exhaustion also
    # carries the affordance that unblocks it, because re-running the same
    # request under the same ceiling would only exhaust it again.
    if is_bounded_termination(raw_reason):
        info(f"Best-effort result: {termination_reason_label(reason)}.")
        if base in BUDGET_EXHAUSTED_REASONS:
            info("Continue with a wider budget: `/task retry <id> --max-iterations N`.")
        _render_task_hint(task_id)
        return
    if base in {"cancelled", "interrupted"}:
        info(f"Turn ended: {termination_reason_label(reason)}.")
        _render_task_hint(task_id)
        return
    if base not in _CLEAN_TERMINATIONS:
        info(f"Turn ended: {termination_reason_label(reason)}.")
        _render_task_hint(task_id)
        return
    if base in {"max_iterations", "max_tool_calls", "max_total_tokens", "max_cost"}:
        drained = list(getattr(turn, "drained_results", []) or [])
        if not any(item.get("status") == "succeeded" for item in drained):
            warn(f"Turn stopped before completion: {termination_reason_label(reason)}.")
            _render_task_hint(task_id)


def render_turn_outcome(turn) -> None:  # noqa: ANN001
    """Say plainly when a finished turn is not a full success.

    Diagnostics already cover hard errors and bounded stops. This banner is for
    the case those miss: a structurally complete report that settled
    ``degraded``, a verification miss, every seed sitting on a constraint
    bound, or a turn that stopped because the user cancelled or the process
    exited. Without it the green completion line is what the reader believes.
    """
    outcome = classify_turn_outcome(turn)
    warnings = display_warnings(turn)
    if outcome == "succeeded":
        for item in informational_host_fill_notes(turn):
            info(item)
        return
    if outcome == "cancelled":
        warn("Turn cancelled — completed results were preserved.")
        return
    if outcome == "interrupted":
        warn("Turn interrupted — the owning process exited.")
        return
    if outcome == "degraded":
        warn("Partial success — this is not a full success.")
        settled = str(getattr(turn, "settlement_status", "") or "").strip()
        if settled and settled not in {"degraded", "partial"}:
            warn(f"Verification: {settled}")
        for item in warnings:
            warn(item)
        return
    if outcome == "failed" and str(getattr(turn, "kind", "") or "") != "error":
        error("Turn failed — no complete answer was produced.")
        for item in warnings:
            warn(item)


def render_tasks(turn, *, shell_commands: bool = False, artifacts_dir=None) -> None:  # noqa: ANN001
    session_id = str(getattr(turn, "session_id", "") or "")
    presentation = turn_presentation_from_result(turn)
    pending_workflows = list(getattr(turn, "submitted_workflow_ids", []) or [])
    pending_executions = list(getattr(turn, "submitted_subtask_ids", []) or [])
    if (pending_workflows or pending_executions) and not turn.drained_results:
        for task in presentation.tasks:
            identity = _task_identity_suffix(task)
            console.print(
                f"\n[cyan]◷[/cyan] {_task_object_label(task)} [bold]{task.skill}[/bold] "
                f"(submitted){identity}"
            )
            _render_task_presentation(task, shell_commands=shell_commands, session_id=session_id)
        if not presentation.tasks:
            if pending_workflows:
                info(
                    "Submitted workflow runs: "
                    + ", ".join(value[:8] for value in pending_workflows)
                )
            if pending_executions:
                info(
                    "Submitted skill executions: "
                    + ", ".join(value[:8] for value in pending_executions)
                )
            if presentation.next_actions:
                console.print("  [bold cyan]Next actions[/bold cyan]")
                for action in _channel_actions(
                    presentation.next_actions,
                    shell_commands=shell_commands,
                    session_id=session_id,
                ):
                    console.print(f"    • {action}")
    completed_tasks = presentation.tasks if turn.drained_results else []
    for task in completed_tasks:
        # Turn-level canonical outputs own the one user-facing inventory. Child
        # cards still report status/research evidence and small text previews,
        # but do not enumerate the same file paths a second time.
        rendered = task
        if artifacts_dir is not None:
            from omni.runtime.artifact_preview import inline_text_artifacts

            rendered = inline_text_artifacts(rendered, artifacts_dir)
        if task.status == "needs_input":
            # Waiting on the user is not the same event as breaking, and marking
            # both with a red cross taught readers to discount the red cross.
            # The card states what is missing and the command that supplies it.
            action_card(
                f"{_task_object_label(task)} {task.skill} needs input{_task_identity_suffix(task)}",
                rendered.summary,
                actions=_channel_actions(
                    rendered.next_actions,
                    shell_commands=shell_commands,
                    session_id=session_id,
                ),
            )
            _render_task_evidence(
                rendered,
                show_artifact_inventory=not bool(presentation.artifacts),
            )
            continue
        head = _task_status_glyph(task.status)
        console.print(
            f"\n{head} {_task_object_label(task)} [bold]{task.skill}[/bold] "
            f"({task.status}){_task_identity_suffix(task)}"
        )
        _render_task_presentation(
            rendered,
            shell_commands=shell_commands,
            session_id=session_id,
            show_artifact_inventory=not bool(presentation.artifacts),
        )
        if task.error:
            error(f"  {task.error[:200]}")


_DELIVERABLE_MIN_SECONDS = 2.0
_DELIVERABLE_MAX_ROWS = 12


def _fmt_elapsed(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rem = divmod(int(seconds), 60)
    return f"{minutes}m{rem:02d}s"


def _artifact_format(target: str) -> str:
    suffix = Path(target.split("?", 1)[0].split("#", 1)[0]).suffix.lstrip(".")
    return suffix.upper() if suffix else ""


_PRODUCE_TRACE_TOOLS = frozenset({"write_file", "edit_file", "run_skill", "run_workflow"})


def _format_turn_artifact(artifact: Any) -> dict[str, str]:
    return {
        "label": str(getattr(artifact, "title", "") or "artifact"),
        "format": str(getattr(artifact, "display_format", "") or "").upper(),
        "target": str(getattr(artifact, "path", "") or "saved (path unavailable)"),
    }


def _harvest_trace_artifacts(roots: list[dict[str, Any]]) -> list[dict[str, str]]:
    from omni.runtime.task_results import _collect_artifacts, is_dot_artifact

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for root in roots:
        for art in _collect_artifacts(root):
            target = str(art.get("path") or "").strip()
            if not target or target.startswith("artifact://") or target in seen:
                continue
            if is_dot_artifact({"path": art.get("path", ""), "uri": art.get("uri", "")}):
                continue
            seen.add(target)
            out.append(
                {
                    "label": str(art.get("label") or "artifact"),
                    "format": _artifact_format(target),
                    "target": target,
                }
            )
    return out


def _turn_deliverables(turn: Any) -> list[dict[str, str]]:
    """Every distinct user-facing output, preferring the canonical task inventory.

    An empty ``artifacts`` list means this ``task_id`` paid nothing. Harvest
    then only from this-turn produce tools (``write_file`` / ``run_skill``),
    never from lookup dumps that can name a sibling RAG run. Callers that
    never set ``artifacts`` still harvest the whole tool trace.
    """
    from omni.runtime.task_results import is_dot_artifact

    if hasattr(turn, "artifacts"):
        canonical = [
            _format_turn_artifact(artifact)
            for artifact in (getattr(turn, "artifacts", []) or [])
            if getattr(artifact, "is_primary", True)
            and not is_dot_artifact(artifact)
        ]
        if canonical:
            return canonical
        roots: list[dict[str, Any]] = []
        for record in getattr(turn, "tool_trace", []) or []:
            if getattr(record, "name", "") not in _PRODUCE_TRACE_TOOLS:
                continue
            result = getattr(record, "result", None)
            if isinstance(result, dict):
                roots.append(result)
        roots.extend(
            d for d in (getattr(turn, "drained_results", []) or []) if isinstance(d, dict)
        )
        return _harvest_trace_artifacts(roots)

    roots = []
    for record in getattr(turn, "tool_trace", []) or []:
        result = getattr(record, "result", None)
        if isinstance(result, dict):
            roots.append(result)
    roots.extend(d for d in (getattr(turn, "drained_results", []) or []) if isinstance(d, dict))
    return _harvest_trace_artifacts(roots)


def _answer_changed_after_stream(
    streamed: str,
    presentation: TurnPresentation,
) -> bool:
    """Compare both answers after applying the same artifact projection."""
    projected_stream = project_artifact_locations(
        streamed,
        presentation.artifacts,
        include_local_paths=True,
    )
    return bool(
        presentation.assistant_text
        and presentation.assistant_text.strip() != projected_stream.strip()
    )


def render_deliverables(turn: Any, *, elapsed_s: float, verbosity: str = "normal") -> None:
    """Close a turn with what it produced and how long it took.

    The completion line always lands once a turn did real work, so a run reads as
    finished instead of trailing off. A deliverables table is added only when no
    task card already enumerated the artifacts (the synchronous ``run_skill``
    path), so async task turns are summarised here rather than repeated.
    """
    artifacts = _turn_deliverables(turn)
    drained = bool(getattr(turn, "drained_results", []) or [])
    worked = bool(artifacts) or drained or bool(getattr(turn, "tool_trace", []) or [])
    if not worked and elapsed_s < _DELIVERABLE_MIN_SECONDS:
        return

    canonical = bool(getattr(turn, "artifacts", []) or [])
    if artifacts and (canonical or not drained) and verbosity != "quiet":
        data_table(
            "Outputs",
            ["artifact", "format", "location"],
            [
                [art["label"], art["format"], art["target"]]
                for art in artifacts[:_DELIVERABLE_MAX_ROWS]
            ],
        )

    outcome = classify_turn_outcome(turn)
    word, color, glyph = _completion_mark(outcome)
    parts = [word]
    if artifacts:
        parts.append(f"{len(artifacts)} artifact{'s' if len(artifacts) != 1 else ''}")
    task_count = len(getattr(turn, "drained_results", []) or [])
    if task_count:
        parts.append(f"{task_count} task{'s' if task_count != 1 else ''}")
    parts.append(_fmt_elapsed(elapsed_s))
    cost = getattr(turn, "cost", None)
    cost = cost if isinstance(cost, dict) else {}
    tokens = int(cost.get("total_tokens") or 0)
    if not tokens:
        usage = getattr(turn, "usage", None)
        usage = usage if isinstance(usage, dict) else {}
        tokens = int(usage.get("total_tokens") or 0)
    if tokens:
        from omni.agent.cost import format_tokens

        parts.append(f"{format_tokens(tokens)} tokens")
    spent = float(cost.get("cost_usd") or 0.0)
    if spent:
        parts.append(f"${spent:.4f}")
    # The turn's own id, written where it stays readable. It was shown only in
    # the live status line, which the turn erases as it ends, so a finished run
    # named itself nowhere — and a run that is also absent from `/task list`
    # cannot be looked up at all.
    owner = str(getattr(turn, "task_id", "") or "")[:8]
    suffix = f" [dim]· task {owner}[/dim]" if owner else ""
    console.print(f"[{color}]{glyph}[/{color}] {' · '.join(parts)}{suffix}")


def _completion_mark(outcome: str) -> tuple[str, str, str]:
    if outcome == "failed":
        return "failed", theme.DANGER, "✗"
    if outcome == "cancelled":
        return "cancelled", theme.CAUTION, "⚠"
    if outcome == "interrupted":
        return "interrupted", theme.CAUTION, "⚠"
    if outcome == "degraded":
        return "degraded", theme.CAUTION, "⚠"
    if outcome == "needs_input":
        return "needs input", theme.CAUTION, "⚠"
    return "done", theme.SUCCESS, "✓"


def _task_status_glyph(status: str) -> str:
    if status == "succeeded":
        return f"[{theme.SUCCESS}]✓[/{theme.SUCCESS}]"
    if status == "degraded":
        return f"[{theme.CAUTION}]⚠[/{theme.CAUTION}]"
    return f"[{theme.DANGER}]✗[/{theme.DANGER}]"


def _task_identity_suffix(presentation: TaskPresentation) -> str:
    tokens = presentation.identity_tokens
    return f" {' '.join(tokens)}" if tokens else ""


def _task_object_label(presentation: TaskPresentation) -> str:
    return {
        "workflow_run": "Workflow",
        "workflow_step": "Workflow step",
        "skill_execution": "Skill execution",
        "scheduled_goal": "Scheduled task",
        "task": "Task",
    }.get(presentation.object_kind, "Task result")


def _channel_actions(actions: list[str], *, shell_commands: bool, session_id: str) -> list[str]:
    if not shell_commands:
        return actions
    converted = [_shell_action(action, session_id=session_id) for action in actions]
    return [action for action in converted if action]


def _shell_action(action: str, *, session_id: str) -> str:
    value = action.strip()
    if value.startswith("/task show "):
        task, note = _command_tail(value.removeprefix("/task show "))
        if not task:
            return ""
        return f"omni task show {task}{note}"
    if value.startswith("/task attach "):
        tail = value.removeprefix("/task attach ")
        task, note = _command_tail(tail)
        if not task:
            return ""
        session = f" --session {session_id[:8]}" if session_id else ""
        return f"omni task attach {task}{session}{note}"
    if value.startswith("/task watch"):
        note = _command_note(value.removeprefix("/task watch"))
        return f"omni task watch{note}"
    if value.startswith("/inbox"):
        note = _command_note(value.removeprefix("/inbox"))
        return f"omni task inbox{note}"
    if value.startswith("/verify --session"):
        note = _command_note(value.removeprefix("/verify --session"))
        session = f" --session {session_id[:8]}" if session_id else ""
        return f"omni verify{session}{note}"
    return value


def _command_tail(tail: str) -> tuple[str, str]:
    parts = tail.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0].rstrip(":"), _command_note(parts[1] if len(parts) > 1 else "")


def _command_note(text: str) -> str:
    note = text.strip().lstrip(":").strip()
    return f" ({note})" if note else ""


def render_verify(report) -> None:  # noqa: ANN001
    """Render a :class:`omni.research.verify.VerifyReport` for ``--verify``."""
    console.rule("[bold magenta]Verification (--verify)", style="magenta")
    if report.total_claims == 0 and not getattr(report, "memory_total", 0):
        info("This turn recorded no claims to verify.")
        run_count = int(getattr(report, "run_count", 0) or 0)
        source_count = int(getattr(report, "source_count", 0) or 0)
        if run_count or source_count:
            info(
                f"ROM inventory: {source_count} sources · 0 claims · {run_count} runs. "
                "Inspect runs with `omni run list`."
            )
        return
    if report.total_claims:
        info(
            f"Claims {report.total_claims} · grounding rate {report.grounding_rate:.0%} "
            f"· issues {report.issues}"
        )
    if report.unsupported:
        warn(f"Unsupported claims: {len(report.unsupported)}")
        for c in report.unsupported[:8]:
            console.print(f"   [yellow]·[/yellow] {c.text[:90]} [dim](claim {c.id[:8]})[/dim]")
    if report.contradicted:
        warn(f"Claims with contradicting evidence: {len(report.contradicted)}")
        for c, n in report.contradicted[:8]:
            console.print(f"   [red]·[/red] {c.text[:80]} [dim](claim {c.id[:8]}, contradictions {n})[/dim]")
    if report.overconfident:
        warn(f"Overconfident claims without evidence: {len(report.overconfident)}")
        for c in report.overconfident[:8]:
            console.print(
                f"   [yellow]·[/yellow] {c.text[:80]} "
                f"[dim](claim {c.id[:8]}, confidence {c.confidence:.0%})[/dim]"
            )
    mem_total = getattr(report, "memory_total", 0)
    if mem_total:
        info(
            f"Memory claims {mem_total} · grounded {report.memory_grounded} "
            f"· without sources {len(report.memory_unsupported)}"
        )
        if report.memory_unsupported:
            warn(f"Memory claims without sources: {len(report.memory_unsupported)}")
            for m in report.memory_unsupported[:8]:
                console.print(f"   [yellow]·[/yellow] {m.summary[:90]} [dim](mem {m.id[:8]})[/dim]")
    if report.issues == 0:
        from omni.cli.render import success

        success("All claims have supporting evidence or sources and no contradictions.")
    else:
        info(
            "Add evidence with `omni evidence add <claim> --source <id>`, attach a "
            "source to memory, lower confidence, or withdraw the claim."
        )


async def run_one_shot(
    state: AppState,
    text: str,
    *,
    cont: bool = False,
    quiet: bool = False,
    verbose: bool = False,
    debug: bool = False,
    detach: bool = False,
    session_id: str | None = None,
    title: str = "",
    interaction_mode: str = "auto",
    workspace_auto: bool = False,
) -> Any:
    """Run a single turn end-to-end and render the answer + any tasks."""
    from omni.runtime.daemon import is_daemon_running

    agent = await make_agent(state)
    try:
        if session_id is None:
            session_id = await agent.ensure_session(
                channel="cli", reuse_latest=cont, title=title
            )
        # Let a running daemon own background tasks; only drain inline when this
        # is a foreground-only run (no daemon) and the caller didn't --detach.
        drain = not detach and not is_daemon_running(agent.paths)
        # Streaming output (P2): progressively render the answer as it arrives on
        # an interactive terminal. The display prints the header lazily on the
        # first token so a task-only turn (no streamed text) still renders normally.
        stream_on = bool(getattr(agent.settings.react, "stream", True)) and not quiet and console.is_terminal
        display = TurnDisplay(
            verbosity=resolve_verbosity(agent.settings, quiet=quiet, verbose=verbose),
            status_line=bool(getattr(agent.settings.display, "status_line", True)),
            debug=resolve_debug(agent.settings, debug=debug),
        )
        display.begin("planning")
        base_ack = task_ack_cb(quiet)

        def on_ack(data: dict) -> None:
            # Bind the task id to the live display first so it stays in the
            # status line for the whole turn (including a later timeout/degrade),
            # then run the normal acknowledgement print.
            display.set_task(str(data.get("task_id") or ""))
            base_ack(data)

        # ``@path`` mentions work the same from a one-shot invocation as from the
        # REPL: the marker is in the text, so nothing composer-specific is needed.
        attachments = resolve_turn_attachments(text)
        if attachments.missing and not quiet:
            warn(f"No such file, not attached: {', '.join(attachments.missing)}")
        try:
            turn = await agent.handle_turn(
                text,
                session_id=session_id,
                channel="cli",
                file_uris=attachments.file_uris,
                drain_tasks=drain,
                on_tool_event=display.tool_event,
                on_task_ack=on_ack,
                on_token=display.token if stream_on else None,
                interaction_mode=interaction_mode,
                workspace_auto=workspace_auto,
            )
        finally:
            elapsed_s = display.end()
        verbosity = resolve_verbosity(agent.settings, quiet=quiet, verbose=verbose)
        streamed = display.streamed_text.strip()
        presentation = turn_presentation_from_result(turn)
        answer = presentation.assistant_text
        if streamed:
            render_turn_diagnostics(turn)
            render_turn_outcome(turn)
            # Re-render the authoritative answer only if it changed after streaming
            # (self-review / contract enforcement); otherwise it's already shown.
            if (
                not should_suppress_assistant_text(turn)
                and _answer_changed_after_stream(streamed, presentation)
            ):
                assistant_answer(answer)
            render_tasks(turn, shell_commands=True, artifacts_dir=agent.paths.artifacts_dir)
            render_deliverables(turn, elapsed_s=elapsed_s, verbosity=verbosity)
            return turn
        console.rule("[bold cyan]OmniScientist", style="cyan")
        render_turn_diagnostics(turn)
        render_turn_outcome(turn)
        if not should_suppress_assistant_text(turn):
            assistant_answer(answer)
        render_tasks(turn, shell_commands=True, artifacts_dir=agent.paths.artifacts_dir)
        render_deliverables(turn, elapsed_s=elapsed_s, verbosity=verbosity)
        return turn
    finally:
        await agent.aclose()
