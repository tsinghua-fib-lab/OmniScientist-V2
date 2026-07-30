"""`omni bench` — offline retrieval/grounding benchmark for the research core.

Runs the real ``search_corpus`` pipeline over a small bundled gold set in a
throwaway store (no network, no effect on your workspace) and prints a
scorecard. Use it to sanity-check that retrieval works on this machine and to
catch regressions in chunking/ranking. Pass ``--embed`` to score with the
configured embedding model instead of the keyword fallback.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import typer

from omni.cli.render import console, data_table, info, success, warn
from omni.cli.state import AppState, run_async

app_help = "Evaluate retrieval quality offline with recall@k and MRR."


def bench_command(
    ctx: typer.Context,
    k: int = typer.Option(3, "--k", help="Retrieval depth used for recall@k and MRR."),
    embed: bool = typer.Option(
        False,
        "--embed",
        help="Use the configured embedding model instead of deterministic keyword fallback.",
    ),
) -> None:
    """Run the built-in offline retrieval benchmark and print recall@k and MRR."""
    state: AppState = ctx.obj or AppState()
    run_async(_run_bench(state, k=k, embed=embed))


async def _run_bench(state: AppState, *, k: int, embed: bool) -> None:
    agent = None
    if embed:
        from omni.cli.state import make_agent

        agent = await make_agent(state)
    try:
        await render_bench(k=k, embed=embed, agent=agent)
    finally:
        if agent is not None:
            await agent.aclose()


async def render_bench(*, k: int, embed: bool, agent=None) -> None:  # noqa: ANN001
    """Run the offline retrieval bench in a throwaway store (REPL `/bench`).

    With ``embed`` and a live ``agent``, scores via the configured embedding
    model; otherwise uses the deterministic keyword fallback.
    """
    from omni.research.bench import run_retrieval_bench
    from omni.research.store import ResearchStore
    from omni.storage.db import Database

    llm = agent.llm if (embed and agent is not None) else None
    tmp = Path(tempfile.mkdtemp(prefix="omni-bench-")) / "bench.sqlite3"
    db = Database(tmp)
    await db.init()
    try:
        result = await run_retrieval_bench(ResearchStore(db), llm, k=k)
        data_table(
            f"Retrieval benchmark (n={result.n}, k={result.k}, "
            f"{'embed' if (embed and agent is not None) else 'keyword'}）",
            ["#", "query", "hit", "rank"],
            [[str(i), q["q"][:48], "✓" if q["hit"] else "✗", str(q["rank"] or "-")]
             for i, q in enumerate(result.per_query, 1)],
        )
        line = f"recall@{result.k} = {result.recall_at_k:.0%}   MRR = {result.mrr:.3f}"
        if result.recall_at_k >= 0.8:
            success(line)
        elif result.recall_at_k >= 0.5:
            info(line)
        else:
            warn(line + " (retrieval quality is low; check embeddings and chunk settings)")
        console.print(
            "  The built-in gold set runs in a disposable store; use --embed to test real embeddings.",
            style="dim",
        )
    finally:
        await db.dispose()
