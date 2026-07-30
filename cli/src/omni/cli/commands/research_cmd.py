"""Research Object Model CLI: ``omni hypo|claim|evidence|run|source``.

These give researchers a first-class, inspectable view of the research state
that omni accumulates as it works — hypotheses, the claims it makes, the
evidence binding each claim to a source, and the experiment-run ledger. They
read/write the same per-workspace store the agent uses (via :class:`ResearchStore`),
so a claim the agent recorded mid-conversation is visible here immediately.

Five small Typer apps are defined here and registered in ``cli/main.py`` as
top-level command groups (mirroring ``cite``/``tasks``).
"""

from __future__ import annotations

import json

import typer

from omni.cli.render import console, data_table, error, info, kv_table, success, warn
from omni.cli.state import AppState, make_agent, run_async
from omni.cli.timefmt import format_local_time
from omni.research.store import HYPOTHESIS_STATUSES, ResearchStore


def _store_run(state: AppState, fn):  # noqa: ANN001, ANN201
    """Open an agent, hand its store to ``fn`` (async), close, return result."""

    async def _run():
        agent = await make_agent(state)
        try:
            return await fn(ResearchStore(agent.db), agent)
        finally:
            await agent.aclose()

    return run_async(_run())


# ── omni hypo ──────────────────────────────────────────────────────────────
hypo_app = typer.Typer(help="Create, track, and assess research hypotheses.", no_args_is_help=True)
_HYPO_SUBCOMMANDS = ("list", "new", "show", "status", "help")
_CLAIM_SUBCOMMANDS = ("list", "new", "show", "help")
_EVIDENCE_SUBCOMMANDS = ("list", "add", "help")
_RUN_SUBCOMMANDS = ("list", "show", "help")
_SOURCE_SUBCOMMANDS = ("list", "show", "reindex", "help")


def render_hypo_usage_help() -> None:
    info("Use `/hypo ...` in the REPL or `omni hypo ...` in the shell.")
    info(f"Available subcommands: {', '.join(_HYPO_SUBCOMMANDS)}.")
    data_table(
        "hypo subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "List hypotheses, optionally filtered by status", "/hypo list"],
            ["new <statement>", "Create a falsifiable hypothesis with rationale, confidence, and status", '/hypo new "A reranker reduces hallucinations" -c 0.6'],
            ["show <id>", "Show hypothesis details and linked claims", "/hypo show 1a2b3c"],
            ["status <id> <status>", "Set the assessment status", "/hypo status 1a2b3c supported"],
            ["help", "Show this help", "/hypo help"],
        ],
    )
    info("Supported statuses: proposed / testing / supported / refuted / inconclusive.")


@hypo_app.command("help")
def hypo_help() -> None:
    """Show hypo subcommands and common examples."""
    render_hypo_usage_help()


@hypo_app.command("list")
def hypo_list(ctx: typer.Context, status: str = typer.Option("", help="Filter by status"),
              limit: int = typer.Option(30)) -> None:
    """List research hypotheses."""
    rows = _store_run(ctx.obj, lambda st, _a: st.list_hypotheses(limit=limit, status=status))
    if not rows:
        info("No hypotheses are recorded. The agent can call record_hypothesis, or use `omni hypo new`.")
        return
    data_table("Research hypotheses", ["id", "status", "conf", "statement"],
               [[h.id[:8], h.status, f"{h.confidence:.2f}", (h.statement or "")[:64]] for h in rows])


@hypo_app.command("new")
def hypo_new(ctx: typer.Context, statement: str,
             rationale: str = typer.Option("", "--rationale", "-r"),
             confidence: float = typer.Option(0.5, "--confidence", "-c"),
             status: str = typer.Option("proposed", "--status", "-s")) -> None:
    """Create a falsifiable research hypothesis."""
    if status not in HYPOTHESIS_STATUSES:
        raise typer.BadParameter(f"status must be one of {', '.join(HYPOTHESIS_STATUSES)}")
    h = _store_run(ctx.obj, lambda st, _a: st.add_hypothesis(
        statement, rationale=rationale, confidence=confidence, status=status))
    success(f"Recorded hypothesis {h.id[:8]} ({h.status}).")


@hypo_app.command("show")
def hypo_show(ctx: typer.Context, hyp_id: str) -> None:
    """Show a hypothesis and its linked claims."""
    async def _fn(st: ResearchStore, _a):
        h = await st.get_hypothesis(hyp_id)
        claims = await st.list_claims(hypothesis_id=h.id) if h else []
        return h, claims

    h, claims = _store_run(ctx.obj, _fn)
    if not h:
        error(f"Hypothesis {hyp_id} was not found.")
        raise typer.Exit(1)
    kv_table(f"Hypothesis {h.id[:8]}", [
        ("statement", h.statement), ("status", h.status),
        ("confidence", f"{h.confidence:.2f}"), ("rationale", h.rationale or "-"),
        ("created", format_local_time(h.created_at)),
    ])
    if claims:
        data_table("Linked claims", ["id", "conf", "text"],
                   [[c.id[:8], f"{c.confidence:.2f}", (c.text or "")[:70]] for c in claims])


@hypo_app.command("status")
def hypo_status(ctx: typer.Context, hyp_id: str, status: str,
                confidence: float = typer.Option(None, "--confidence", "-c")) -> None:
    """Set the hypothesis status."""
    if status not in HYPOTHESIS_STATUSES:
        raise typer.BadParameter(f"status must be one of {', '.join(HYPOTHESIS_STATUSES)}")
    h = _store_run(ctx.obj, lambda st, _a: st.set_hypothesis_status(hyp_id, status, confidence=confidence))
    if not h:
        error(f"Hypothesis {hyp_id} was not found.")
        raise typer.Exit(1)
    success(f"Hypothesis {h.id[:8]} -> {h.status} (confidence {h.confidence:.2f}).")


# ── omni claim ───────────────────────────────────────────────────────────
claim_app = typer.Typer(help="Manage claims with evidence and calibrated confidence.", no_args_is_help=True)


def render_claim_usage_help() -> None:
    info("Use `/claim ...` in the REPL or `omni claim ...` in the shell.")
    info(f"Available subcommands: {', '.join(_CLAIM_SUBCOMMANDS)}.")
    data_table(
        "claim subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "List claims and evidence counts; verify flags claims with none", "/claim list"],
            ["new <text>", "Record a claim manually", '/claim new "Transformers replace recurrence with self-attention" -c 0.7'],
            ["show <id>", "Show a claim and its evidence chain", "/claim show 1a2b3c"],
            ["help", "Show this help", "/claim help"],
        ],
    )


@claim_app.command("help")
def claim_help() -> None:
    """Show claim subcommands and common examples."""
    render_claim_usage_help()


@claim_app.command("list")
def claim_list(ctx: typer.Context, limit: int = typer.Option(30),
               session: str = typer.Option("", "--session", "-s"),
               hypothesis: str = typer.Option("", "--hypothesis", "-H")) -> None:
    """List claims with evidence counts."""
    async def _fn(st: ResearchStore, _a):
        claims = await st.list_claims(limit=limit, session_id=session, hypothesis_id=hypothesis)
        counts = await st.evidence_count_by_claim()
        return claims, counts

    claims, counts = _store_run(ctx.obj, _fn)
    if not claims:
        info("No claims are recorded. The agent records them with record_claim.")
        return
    data_table("Claims", ["id", "conf", "evidence", "text"],
               [[c.id[:8], f"{c.confidence:.2f}", str(counts.get(c.id, 0)),
                 (c.text or "")[:64]] for c in claims])


@claim_app.command("new")
def claim_new(ctx: typer.Context, text: str,
              confidence: float = typer.Option(0.5, "--confidence", "-c"),
              hypothesis: str = typer.Option("", "--hypothesis", "-H")) -> None:
    """Create a claim."""
    c = _store_run(ctx.obj, lambda st, _a: st.add_claim(
        text, confidence=confidence, hypothesis_id=hypothesis, made_by="user"))
    success(f"Recorded claim {c.id[:8]}. Bind a source with `omni evidence add {c.id[:8]} --source <id>`.")


@claim_app.command("show")
def claim_show(ctx: typer.Context, claim_id: str) -> None:
    """Show a claim and its evidence chain."""
    async def _fn(st: ResearchStore, _a):
        c = await st.get_claim(claim_id)
        if not c:
            return None, []
        ev = await st.evidence_for_claim(c.id)
        srcs = {}
        for e in ev:
            if e.source_id and e.source_id not in srcs:
                srcs[e.source_id] = await st.get_source(e.source_id)
        return c, [(e, srcs.get(e.source_id)) for e in ev]

    c, ev = _store_run(ctx.obj, _fn)
    if not c:
        error(f"Claim {claim_id} was not found.")
        raise typer.Exit(1)
    kv_table(f"Claim {c.id[:8]}", [
        ("text", c.text), ("confidence", f"{c.confidence:.2f}"),
        ("polarity", c.polarity), ("made_by", c.made_by),
    ])
    if ev:
        data_table("Evidence", ["stance", "source", "quote"],
                   [[e.stance, (s.title[:30] if s else (e.source_id[:8] or "-")),
                     (e.quote or "")[:50]] for e, s in ev])
    else:
        warn("This claim has no evidence and will be reported as unsupported by verify.")


# ── omni evidence ────────────────────────────────────────────────────────
evidence_app = typer.Typer(help="Manage provenance edges between claims and sources.", no_args_is_help=True)


def render_evidence_usage_help() -> None:
    info("Use `/evidence ...` in the REPL or `omni evidence ...` in the shell.")
    info(f"Available subcommands: {', '.join(_EVIDENCE_SUBCOMMANDS)}.")
    data_table(
        "evidence subcommands",
        ["command", "purpose", "example"],
        [
            ["list <claim_id>", "List evidence for a claim", "/evidence list 1a2b3c"],
            ["add <claim_id>", "Bind a claim to a source with stance, quote, locator, and strength", "/evidence add 1a2b --source 9f8e --stance supports"],
            ["help", "Show this help", "/evidence help"],
        ],
    )


@evidence_app.command("help")
def evidence_help() -> None:
    """Show evidence subcommands and common examples."""
    render_evidence_usage_help()


@evidence_app.command("list")
def evidence_list(ctx: typer.Context, claim_id: str) -> None:
    """List evidence for a claim."""
    claim_show(ctx, claim_id)


@evidence_app.command("add")
def evidence_add(ctx: typer.Context, claim_id: str,
                 source: str = typer.Option("", "--source", help="Source ID or unique prefix"),
                 stance: str = typer.Option("supports", "--stance"),
                 quote: str = typer.Option("", "--quote"),
                 locator: str = typer.Option("", "--locator"),
                 strength: float = typer.Option(0.6, "--strength")) -> None:
    """Bind a claim to a source."""
    async def _fn(st: ResearchStore, _a):
        c = await st.get_claim(claim_id)
        if not c:
            return "no_claim", None
        sid = ""
        if source:
            src = await st.get_source(source)
            if not src:
                return "no_source", None
            sid = src.id
        ev = await st.add_evidence(c.id, source_id=sid, stance=stance,
                                   quote=quote, locator=locator, strength=strength)
        return "ok", ev

    status, ev = _store_run(ctx.obj, _fn)
    if status == "no_claim":
        error(f"Claim {claim_id} was not found.")
        raise typer.Exit(1)
    if status == "no_source":
        error(f"Source {source} was not found. Use `omni source list` or ask the agent to cite_source first.")
        raise typer.Exit(1)
    success(f"Added evidence {ev.id[:8]} ({ev.stance}) to claim {claim_id[:8]}.")


# ── omni run ─────────────────────────────────────────────────────────────
run_app = typer.Typer(help="Inspect the experiment and computation run ledger.", no_args_is_help=True)


def render_run_usage_help() -> None:
    info("Use `/run ...` in the REPL or `omni run ...` in the shell.")
    info(f"Available subcommands: {', '.join(_RUN_SUBCOMMANDS)}.")
    data_table(
        "run subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "List experiment and compute runs, optionally by session", "/run list"],
            ["show <id>", "Show command, seed, environment, metrics, and output artifacts", "/run show 1a2b3c"],
            ["help", "Show this help", "/run help"],
        ],
    )


@run_app.command("help")
def run_help() -> None:
    """Show run subcommands and common examples."""
    render_run_usage_help()


@run_app.command("list")
def run_list(ctx: typer.Context, limit: int = typer.Option(30),
             session: str = typer.Option("", "--session", "-s")) -> None:
    """List experiment runs."""
    rows = _store_run(ctx.obj, lambda st, _a: st.list_runs(limit=limit, session_id=session))
    if not rows:
        info("No experiment runs are recorded. Research skills can record completed work with log_run.")
        return
    data_table("Experiment runs", ["id", "status", "seed", "title", "metrics"],
               [[r.id[:8], r.status, str(r.seed if r.seed is not None else "-"),
                 (r.title or r.cmd or "")[:36],
                 ", ".join(f"{k}={v}" for k, v in list((r.metrics or {}).items())[:3])]
                for r in rows])


@run_app.command("show")
def run_show(ctx: typer.Context, run_id: str) -> None:
    """Show complete provenance for a run."""
    r = _store_run(ctx.obj, lambda st, _a: st.get_run(run_id))
    if not r:
        error(f"Run {run_id} was not found.")
        raise typer.Exit(1)
    kv_table(f"run {r.id[:8]}", [
        ("title", r.title or "-"), ("status", r.status), ("cmd", r.cmd or "-"),
        ("seed", r.seed), ("code_uri", r.code_uri or "-"),
        ("started", format_local_time(r.started_at)),
        ("finished", format_local_time(r.finished_at)),
    ])
    if r.metrics:
        console.print("\n[bold]metrics[/bold]")
        console.print_json(json.dumps(r.metrics, ensure_ascii=False, default=str))
    if r.output_uris:
        console.print("[bold]outputs[/bold]: " + ", ".join(r.output_uris))
    if r.env_lock:
        console.print(f"[dim]env: {r.env_lock.splitlines()[0]}[/dim]")


# ── omni source ──────────────────────────────────────────────────────────
source_app = typer.Typer(help="Manage research sources such as papers, web pages, and datasets.", no_args_is_help=True)


def render_source_usage_help() -> None:
    info("Use `/source ...` in the REPL or `omni source ...` in the shell.")
    info(f"Available subcommands: {', '.join(_SOURCE_SUBCOMMANDS)}.")
    data_table(
        "source subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "List indexed sources", "/source list"],
            ["show <id>", "Show source details and chunk count", "/source show 1a2b3c"],
            ["reindex", "Idempotently import library.jsonl into the structured source store", "/source reindex"],
            ["help", "Show this help", "/source help"],
        ],
    )


@source_app.command("help")
def source_help() -> None:
    """Show source subcommands and common examples."""
    render_source_usage_help()


@source_app.command("list")
def source_list(ctx: typer.Context, limit: int = typer.Option(30)) -> None:
    """List indexed sources."""
    rows = _store_run(ctx.obj, lambda st, _a: st.list_sources(limit=limit))
    if not rows:
        info("The source store is empty. Search for papers or use `omni source reindex` to import library.jsonl.")
        return
    data_table("Sources", ["id", "kind", "year", "ref", "title"],
               [[s.id[:8], s.kind, s.year or "-",
                 (s.arxiv_id or s.doi or s.url or "")[:24], (s.title or "")[:48]] for s in rows])


@source_app.command("show")
def source_show(ctx: typer.Context, source_id: str) -> None:
    """Show source details and chunk count."""
    async def _fn(st: ResearchStore, _a):
        s = await st.get_source(source_id)
        chunks = await st.chunks_for_source(s.id) if s else []
        return s, len(chunks)

    s, n_chunks = _store_run(ctx.obj, _fn)
    if not s:
        error(f"Source {source_id} was not found.")
        raise typer.Exit(1)
    kv_table(f"Source {s.id[:8]}", [
        ("title", s.title), ("kind", s.kind), ("authors", ", ".join(s.authors or [])[:80]),
        ("year", s.year or "-"), ("arxiv_id", s.arxiv_id or "-"), ("doi", s.doi or "-"),
        ("url", s.url or "-"), ("origin", s.origin), ("chunks", n_chunks),
        ("retrieved", format_local_time(s.retrieved_at)),
    ])


@source_app.command("reindex")
def source_reindex(ctx: typer.Context) -> None:
    """Idempotently import papers from library.jsonl into the structured source store."""
    from omni.memory.library import load_library

    async def _fn(st: ResearchStore, agent):
        entries = load_library(agent.paths.library)
        added = 0
        for e in entries:
            before = await st.find_source(e)
            await st.add_source(e, origin=str(e.get("source") or "arxiv"))
            if before is None:
                added += 1
        return len(entries), added

    total, added = _store_run(ctx.obj, _fn)
    if total == 0:
        warn("library.jsonl is empty; there is nothing to import.")
        return
    success(f"Imported {added} new sources from library.jsonl ({total} total after deduplication).")
