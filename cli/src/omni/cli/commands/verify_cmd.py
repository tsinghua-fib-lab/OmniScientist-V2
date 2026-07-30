"""`omni verify` — audit recorded claims against their evidence (honesty pass).

A deliberately manual check the user runs when they want to trust a result. It
does *not* call the model again; it inspects the claim/evidence graph this
workspace has accumulated and flags the trust-eroding failure modes: claims with
no supporting evidence, claims with contradicting evidence, and over-confident
yet unsupported claims. Use after a research session, or pass ``--session`` to
scope it to one conversation.
"""

from __future__ import annotations

import typer

from omni.cli.render import console, error, info
from omni.cli.state import AppState, run_async

app_help = "Audit recorded claims for missing, contradicting, or overconfident evidence."


def verify_command(
    ctx: typer.Context,
    session: str = typer.Option(
        "",
        "--session",
        "-s",
        help="Audit one session by full ID or unique prefix; defaults to the workspace.",
    ),
) -> None:
    """Audit whether recorded claims have supporting evidence.

    This check does not call the model. It inspects claims and evidence persisted
    by ``record_claim`` and ``add_evidence`` and reports grounding coverage,
    contradictions, overconfidence, and suggested remediation.
    """
    state: AppState = ctx.obj or AppState()
    run_async(_run_verify(state, session=session))


async def _run_verify(state: AppState, *, session: str) -> None:
    from omni.cli.state import make_agent

    agent = await make_agent(state)
    try:
        verdict = await render_verify_report(agent, session=session)
    finally:
        await agent.aclose()
    # In ``strict`` acceptance mode a thin/contradicted research graph is a
    # non-zero exit so scripts and CI can gate on it; ``warn``/``off`` never fail.
    if verdict is not None and not verdict.accepted:
        raise typer.Exit(2)


async def render_verify_report(agent, *, session: str = ""):  # noqa: ANN001, ANN201
    """Audit the claim/evidence graph on an *existing* agent (REPL `/verify`).

    Returns the :class:`AcceptanceReport` so callers (the CLI command) can gate an
    exit code on it; the REPL path ignores the return value.
    """
    from omni.cli.runner import render_verify
    from omni.research.acceptance import AcceptanceEngine
    from omni.research.store import ResearchStore
    from omni.research.verify import verify_session

    session = await _resolve_session_id(agent, session)
    report = await verify_session(ResearchStore(agent.db), session_id=session)
    scope = f"session {session[:8]}" if session else "entire workspace"
    info(f"Verification scope: {scope}")
    render_verify(report)
    # Research-fact acceptance layer (configurable warn/strict). ``warn`` (default)
    # annotates without changing terminal status; ``strict`` marks not-accepted so
    # the CLI command can exit non-zero. It never rewrites stored task status.
    verdict = AcceptanceEngine.from_settings(agent.settings).evaluate(report)
    if verdict.has_findings:
        console.print(verdict.annotation(), style="yellow" if verdict.accepted else "red")
    if report.total_claims == 0:
        console.print(
            "  Tip: use `omni lit \"...\"` or research-ideation to create evidence-backed "
            "claims, then run `omni verify`.",
            style="dim",
        )
    return verdict


async def _resolve_session_id(agent, session: str) -> str:  # noqa: ANN001
    """Resolve a shell/REPL session filter by exact id or unique prefix."""
    session = (session or "").strip()
    if not session:
        return ""
    rows = await agent.list_sessions(limit=500)
    exact = [s.id for s, _count in rows if s.id == session]
    if exact:
        return exact[0]
    matches = [s.id for s, _count in rows if s.id.startswith(session)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        error(f"Session {session} was not found. Use `omni session list` to inspect the workspace.")
        raise typer.Exit(1)
    shown = ", ".join(m[:8] for m in matches[:8])
    error(f"Session prefix {session} is ambiguous; matches: {shown}. Provide a longer prefix.")
    raise typer.Exit(1)
