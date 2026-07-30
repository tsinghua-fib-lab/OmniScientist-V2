"""Shared one-shot turn runner (used by ``chat``, ``exec``, ``session resume``).

Keeping this out of ``cli/main.py`` lets subcommand modules reuse the exact same
agent lifecycle + rendering without importing the top-level Typer app (which
would be a circular import).
"""

from __future__ import annotations

from typing import Any

from omni.cli.live_display import TurnDisplay, resolve_verbosity
from omni.cli.render import (
    assistant_answer,
    console,
    error,
    error_card,
    info,
    warn,
)
from omni.cli.repl_output import set_output_status
from omni.cli.state import AppState, make_agent
from omni.core.termination import base_termination_reason
from omni.runtime.presentation import (
    TaskPresentation,
    termination_reason_label,
    turn_presentation_from_result,
)


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


def _artifact_entries(result: dict[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(value: Any, *, title: str = "", fmt: str = "") -> None:
        if isinstance(value, dict):
            uri = str(value.get("uri") or value.get("artifact_uri") or "")
            path = str(value.get("path") or value.get("file") or "")
            key = uri or path
            if key and key in seen:
                return
            if key:
                seen.add(key)
            entries.append({
                "title": str(value.get("title") or title or value.get("name") or fmt or "artifact"),
                "format": str(value.get("format") or fmt or ""),
                "uri": uri,
                "path": path,
                "mime": str(value.get("mime") or ""),
            })
        elif isinstance(value, str):
            if value in seen:
                return
            seen.add(value)
            entries.append({"title": title or fmt or "artifact", "format": fmt, "uri": value, "path": "", "mime": ""})

    for item in result.get("artifacts") or []:
        add(item)
    for key, value in result.items():
        if key == "artifacts":
            continue
        if isinstance(value, str) and (value.startswith("artifact://") or key.endswith("_uri")):
            add(value, title=key, fmt=key.removesuffix("_uri"))
        elif key in {"output_uris", "files"} and isinstance(value, list):
            for item in value:
                add(item, title=key)
    return entries


def _render_artifacts(result: dict[str, Any]) -> None:
    artifacts = _artifact_entries(result)
    if not artifacts:
        return
    console.print("  [bold cyan]Artifacts[/bold cyan]")
    for art in artifacts:
        label = art.get("title") or art.get("format") or "artifact"
        target = art.get("path") or art.get("uri") or "-"
        suffix = f" [dim]{art.get('uri')}[/dim]" if art.get("path") and art.get("uri") else ""
        console.print(f"    • {label}: {target}{suffix}")


def _render_research_provenance(result: dict[str, Any]) -> None:
    task_id = str(result.get("task_id") or "")
    research = result.get("research") if isinstance(result.get("research"), dict) else {}
    source_ids = research.get("source_ids") or []
    claim_ids = research.get("claim_ids") or []
    if task_id or source_ids or claim_ids:
        console.print("  [bold cyan]Research record[/bold cyan]")
    if task_id:
        console.print(f"    • run: {task_id[:8]}")
    if source_ids:
        console.print(f"    • sources: {', '.join(str(s)[:8] for s in source_ids[:5])}")
    if claim_ids:
        console.print(f"    • claims: {', '.join(str(c)[:8] for c in claim_ids[:5])}")


def _render_task_presentation(
    presentation: TaskPresentation,
    *,
    shell_commands: bool = False,
    session_id: str = "",
) -> None:
    if presentation.summary:
        console.print(f"  {presentation.summary[:300]}")
    if presentation.artifacts:
        console.print("  [bold cyan]Artifacts[/bold cyan]")
        for art in presentation.artifacts:
            suffix = f" [dim]{art.uri}[/dim]" if art.path and art.uri else ""
            console.print(f"    • {art.title}: {art.target}{suffix}")
        for art in presentation.artifacts:
            if not art.preview:
                continue
            heading = f"{art.title} (preview)" if art.preview_truncated else art.title
            console.print(f"  [bold cyan]{heading}[/bold cyan]")
            console.print(art.preview)
            if art.preview_truncated:
                console.print(
                    f"    [dim]Preview truncated; open with open_artifact "
                    f"{art.uri or art.target}[/dim]"
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
    if presentation.next_actions:
        console.print("  [bold cyan]Next actions[/bold cyan]")
        for action in _channel_actions(presentation.next_actions, shell_commands=shell_commands, session_id=session_id):
            console.print(f"    • {action}")


def should_suppress_assistant_text(turn) -> bool:  # noqa: ANN001
    reason = base_termination_reason(str(getattr(turn, "terminated_reason", "") or ""))
    if str(getattr(turn, "kind", "") or "") == "error" and reason == "llm_auth_error":
        return True
    if not getattr(turn, "drained_results", None):
        return False
    if not any(d.get("status") == "succeeded" for d in turn.drained_results):
        return False
    return reason in {"max_iterations", "max_tool_calls"}


def render_turn_diagnostics(turn) -> None:  # noqa: ANN001
    """Make failed turns explicit instead of showing a stale intermediate line."""
    if str(getattr(turn, "kind", "") or "") != "error":
        return
    reason = str(getattr(turn, "terminated_reason", "") or "") or "unknown"
    if base_termination_reason(reason) == "llm_auth_error":
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
        detail = f"{name}: {err[:160]}" if err else name
        if detail:
            warn(f"Last step: {detail}")


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
        status = task.status
        head = "[green]✓[/green]" if status == "succeeded" else "[red]✗[/red]"
        identity = _task_identity_suffix(task)
        console.print(
            f"\n{head} {_task_object_label(task)} [bold]{task.skill}[/bold] "
            f"({status}){identity}"
        )
        rendered = task
        if artifacts_dir is not None:
            from omni.runtime.artifact_preview import inline_text_artifacts

            rendered = inline_text_artifacts(rendered, artifacts_dir)
        _render_task_presentation(rendered, shell_commands=shell_commands, session_id=session_id)
        if task.error:
            error(f"  {task.error[:200]}")


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
    detach: bool = False,
    session_id: str | None = None,
    title: str = "",
    interaction_mode: str = "auto",
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
        )
        display.begin("planning")
        try:
            turn = await agent.handle_turn(
                text,
                session_id=session_id,
                channel="cli",
                drain_tasks=drain,
                on_tool_event=display.tool_event,
                on_task_ack=task_ack_cb(quiet),
                on_token=display.token if stream_on else None,
                interaction_mode=interaction_mode,
            )
        finally:
            display.end()
        streamed = display.streamed_text.strip()
        if streamed:
            render_turn_diagnostics(turn)
            # Re-render the authoritative answer only if it changed after streaming
            # (self-review / contract enforcement); otherwise it's already shown.
            if not should_suppress_assistant_text(turn) and turn.text and turn.text.strip() != streamed:
                assistant_answer(turn.text)
            render_tasks(turn, shell_commands=True, artifacts_dir=agent.paths.artifacts_dir)
            return turn
        console.rule("[bold cyan]OmniScientist", style="cyan")
        render_turn_diagnostics(turn)
        if not should_suppress_assistant_text(turn):
            assistant_answer(turn.text)
        render_tasks(turn, shell_commands=True, artifacts_dir=agent.paths.artifacts_dir)
        return turn
    finally:
        await agent.aclose()
