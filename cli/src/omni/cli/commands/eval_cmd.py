"""`omni eval` — offline capability + coverage regression for the agent.

Where ``omni bench`` scores retrieval (recall@k), ``omni eval`` scores the agent:
it runs a corpus of real research/dialogue scenarios through the harness offline
(the model's plan for each turn is scripted, so it is deterministic and needs no
network) and prints a per-capability scoreboard. Scenarios are role-played by a
``persona`` (``scientist`` / ``general`` / ``red_team``) so regressions are
triggered through realistic user behavior.

``--coverage`` runs the orthogonal audit: which capabilities / dimensions /
personas have *no* scenario at all — the holes in the regression net.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import typer

from omni.cli.render import console, data_table, error, info, success, warn

app_help = "Run offline agent regressions for routing, skills, guardrails, retrieval, and coverage."


def eval_command(
    ctx: typer.Context,
    scenarios: Path = typer.Option(
        None, "--scenarios", "-s", help="Scenario file or directory; defaults to the bundled corpus"
    ),
    tag: str = typer.Option("", "--tag", "-t", help="Run only scenarios with this tag"),
    persona: str = typer.Option(
        "", "--persona", "-p", help="Run one persona: scientist / general / red_team"
    ),
    coverage: bool = typer.Option(False, "--coverage", help="Report capabilities, dimensions, and personas with no scenario coverage"),
    research_quality: bool = typer.Option(
        False, "--research-quality", help="Evaluate citation fidelity, statistical correctness, and reproducibility"
    ),
    black_box: bool = typer.Option(
        False, "--black-box", help="Run isolated natural-language agent trials without injecting planner results"
    ),
    memory: bool = typer.Option(
        False, "--memory", help="Run the persistent-memory benchmark (injection hit / citation hit / zero leakage)"
    ),
    repeats: int = typer.Option(
        1, "--repeats", min=1, help="Black-box repeats for reliability measurement"
    ),
    concurrency: int = typer.Option(
        1, "--concurrency", min=1, help="Concurrent black-box attempts for concurrency and soak validation"
    ),
    live: bool = typer.Option(
        False, "--live", help="Allow network scenarios using the current model/API configuration; may incur cost"
    ),
    quality_input: Path = typer.Option(
        None, "--quality-input", help="Research-quality JSON; defaults to the bundled offline benchmark"
    ),
    record: bool = typer.Option(False, "--record", help="Append scores to eval_history.jsonl"),
    gate: bool = typer.Option(False, "--gate", help="Exit nonzero on regression against the previous record"),
    trend: bool = typer.Option(False, "--trend", help="Render dimension trends from stored snapshots without rerunning evaluation"),
    tail: int = typer.Option(0, "--tail", help="Show only the latest N snapshots; 0 means all"),
    html: bool = typer.Option(
        False,
        "--html",
        help="Export a static trend dashboard under the active Omni data directory",
    ),
    html_path: str = typer.Option("", "--html-path", help="Custom HTML output path; implies --html"),
    open_browser: bool = typer.Option(False, "--open", help="Open generated HTML in a browser"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON for CI and trend recording"),
) -> None:
    """Run capability regressions, coverage checks, gates, and trend reports."""
    from omni.cli.state import run_async

    run_async(
        render_eval(
            source=scenarios,
            tag=tag or None,
            persona=persona or None,
            coverage=coverage,
            research_quality=research_quality,
            black_box=black_box,
            memory=memory,
            repeats=repeats,
            concurrency=concurrency,
            live=live,
            quality_input=quality_input,
            record=record,
            gate=gate,
            trend=trend,
            tail=tail,
            html=html,
            html_path=html_path,
            open_browser=open_browser,
            as_json=as_json,
        )
    )


async def render_eval(
    *,
    source: Path | None = None,
    tag: str | None = None,
    persona: str | None = None,
    coverage: bool = False,
    research_quality: bool = False,
    black_box: bool = False,
    memory: bool = False,
    repeats: int = 1,
    concurrency: int = 1,
    live: bool = False,
    quality_input: Path | None = None,
    record: bool = False,
    gate: bool = False,
    trend: bool = False,
    tail: int = 0,
    html: bool = False,
    html_path: str = "",
    open_browser: bool = False,
    as_json: bool = False,
) -> None:
    """Run the capability benchmark / coverage audit and render it (REPL `/eval`)."""
    want_html = html or bool(html_path)
    if trend or want_html:
        _render_trend(
            tail=tail, as_json=as_json,
            html=want_html, html_path=html_path, open_browser=open_browser,
        )
        return
    if coverage:
        await _render_coverage(source=source, tag=tag, persona=persona, as_json=as_json)
        return
    if research_quality or quality_input is not None:
        _render_research_quality(source=quality_input, as_json=as_json)
        return
    if memory:
        await _render_memory_benchmark(as_json=as_json, gate=gate)
        return
    if black_box:
        await _render_blackbox(
            source=source,
            repeats=repeats,
            concurrency=concurrency,
            allow_network=live,
            as_json=as_json,
        )
        return

    from omni.eval import run_benchmark

    report = await run_benchmark(source=source, tag=tag, persona=persona)

    if record or gate:
        _apply_trend_gate(report, record=record, gate=gate)

    if as_json:
        console.print_json(_json.dumps(report.to_dict(), ensure_ascii=False))
        return

    data_table(
        "Capability dimension scores",
        ["dimension", "passed/total", "pass rate"],
        [[d.name, f"{d.passed}/{d.total}", f"{d.rate:.0%}"] for d in report.dimensions()],
    )
    data_table(
        "Scenario results",
        ["scenario", "tags", "passed", "checks"],
        [
            [
                r.scenario_id,
                ", ".join(r.tags),
                "✓" if r.passed else "✗",
                f"{r.n_passed}/{len(r.checks)}" + (f"  ⚠ {r.error}" if r.error else ""),
            ]
            for r in report.results
        ],
    )
    line = (
        f"Score = {report.score:.0%}   Scenarios {report.scenarios_passed}/{len(report.results)}   "
        f"Checks {report.passed_checks}/{report.total_checks}"
    )
    if report.score >= 0.99:
        success(line)
    elif report.score >= 0.8:
        info(line)
    else:
        warn(line + " (regression detected; inspect failed scenario checks)")
    console.print(
        "  Deterministic offline evaluation uses scripted model plans; use --json for CI trends.",
        style="dim",
    )


async def _render_blackbox(
    *,
    source: Path | None,
    repeats: int,
    concurrency: int,
    allow_network: bool,
    as_json: bool,
) -> None:
    """Run the no-plan-injection product-quality suite."""
    from omni.config import load_settings
    from omni.eval import load_blackbox_scenarios, run_blackbox_benchmark

    scenarios = load_blackbox_scenarios(source)
    report = await run_blackbox_benchmark(
        scenarios,
        settings=load_settings(),
        repeats=repeats,
        concurrency=concurrency,
        allow_model=allow_network,
        allow_network=allow_network,
    )
    if as_json:
        console.print_json(_json.dumps(report.to_dict(), ensure_ascii=False))
        return
    data_table(
        "Natural-language black-box evaluation",
        ["scenario", "repeat", "passed", "status", "duration", "tokens", "rework"],
        [
            [
                attempt.scenario_id,
                str(attempt.repeat),
                "✓" if attempt.passed else "✗",
                "/".join(attempt.statuses) or "error",
                f"{attempt.duration_ms / 1000:.2f}s",
                str(attempt.cost.get("total_tokens") or 0),
                str(attempt.manual_rework),
            ]
            for attempt in report.attempts
        ],
    )
    line = (
        f"Success {report.success_rate:.0%} · Provenance accuracy {report.provenance_accuracy:.0%} · "
        f"Mean rework {report.mean_manual_rework:.2f} · Mean duration {report.mean_duration_ms / 1000:.2f}s · "
        f"Total tokens {report.total_tokens} · Estimated cost ${report.total_cost_usd:.4f}"
    )
    if report.success_rate >= 0.99:
        success(line)
    else:
        warn(line)
    if report.skips:
        info(f"Skipped {report.skipped} attempts because model or network prerequisites were unavailable; --live enables network scenarios.")


async def _render_memory_benchmark(*, as_json: bool, gate: bool) -> None:
    """Run the persistent-memory benchmark (P3) and render its scoreboard.

    Metrics are injection hit / citation hit / zero leakage across the
    cross-session, cross-workspace, cross-channel, isolation, concurrency and
    offline dimensions. Offline and deterministic; ``--gate`` exits non-zero on
    any regression so it can guard CI.
    """
    from omni.eval import run_memory_benchmark

    report = await run_memory_benchmark()
    if as_json:
        console.print_json(_json.dumps(report.to_dict(), ensure_ascii=False))
    else:
        data_table(
            "Persistent-memory metrics",
            ["metric", "passed/total", "pass rate"],
            [[d.name, f"{d.passed}/{d.total}", f"{d.rate:.0%}"] for d in report.dimensions()],
        )
        data_table(
            "Memory dimensions",
            ["dimension", "passed", "checks"],
            [[r.scenario_id, "✓" if r.passed else "✗", f"{r.n_passed}/{len(r.checks)}"] for r in report.results],
        )
        line = f"Memory score = {report.score:.0%}   Dimensions {report.scenarios_passed}/{len(report.results)}"
        (success if report.score >= 0.99 else warn)(line)
    if gate and report.score < 1.0:
        raise typer.Exit(1)


def _render_research_quality(*, source: Path | None, as_json: bool) -> None:
    """Run deterministic citation/statistics/reproducibility quality checks."""
    from omni.eval import evaluate_research_quality, load_quality_payload

    report = evaluate_research_quality(load_quality_payload(source))
    if as_json:
        console.print_json(_json.dumps(report.to_dict(), ensure_ascii=False))
        return
    data_table(
        "Research-quality evaluation",
        ["dimension", "passed", "score", "checks"],
        [
            [
                dimension.name,
                "✓" if dimension.passed else "✗",
                f"{dimension.score:.0%}",
                f"{sum(item.passed for item in dimension.checks)}/{len(dimension.checks)}",
            ]
            for dimension in report.dimensions
        ],
    )
    line = f"Research-quality score = {report.score:.0%}"
    if report.passed:
        success(line)
    else:
        warn(line + " (use --json to inspect failed assertions)")


def _apply_trend_gate(report, *, record: bool, gate: bool) -> None:  # noqa: ANN001
    """Compare this run to the last recorded snapshot; record and/or gate on it.

    ``--gate`` exits non-zero (CI) when a dimension or the overall score
    regressed vs the baseline; ``--record`` appends this run so it becomes the
    next baseline. When both are set, gating happens *before* recording so the
    comparison is against the prior run, not this one.
    """
    from omni.config import load_settings
    from omni.eval import (
        append_snapshot,
        default_history_path,
        detect_regressions,
        last_snapshot,
        report_snapshot,
    )

    path = default_history_path(load_settings().paths)
    snapshot = report_snapshot(report, label="omni eval")

    if gate:
        regressions = detect_regressions(snapshot, last_snapshot(path))
        if regressions:
            for item in regressions:
                error(f"Regression: {item}")
            if record:
                append_snapshot(snapshot, path=path)
            raise typer.Exit(1)
        success("Trend gate passed: no regression against the previous record.")

    if record:
        append_snapshot(snapshot, path=path)
        info(f"Recorded scores in trend history: {path}")


def _render_trend(
    *, tail: int = 0, as_json: bool = False,
    html: bool = False, html_path: str = "", open_browser: bool = False,
) -> None:
    """Render the eval time-series dashboard from ``eval_history.jsonl``.

    Reads the append-only trend log (populated by ``--record``) and prints
    per-dimension sparklines + the latest pass-rate and its delta vs the prior
    run. With ``html`` set, writes a static, self-contained HTML page instead
    (``open_browser`` opens it best-effort). This never re-runs the benchmark —
    it visualises history only.
    """
    from omni.config import load_settings
    from omni.eval import default_history_path, format_delta, load_history, summarize_history

    paths = load_settings().paths
    path = default_history_path(paths)
    history = load_history(path)
    if not history:
        warn(f"No trend history exists. Record scores with `omni eval --record` first ({path}).")
        return

    summary = summarize_history(history, tail=tail or None)
    if html:
        _write_trend_html(summary, paths, html_path=html_path, open_browser=open_browser)
        return
    if as_json:
        console.print_json(_json.dumps(summary.to_dict(), ensure_ascii=False))
        return

    span = summary.first_ts if summary.count == 1 else f"{summary.first_ts} → {summary.last_ts}"
    info(f"Trend dashboard: {summary.count} records ({span})")
    data_table(
        "Overall score trend",
        ["trend", "latest", "change"],
        [[summary.score_spark(), f"{summary.score_latest:.0%}", format_delta(summary.score_delta)]],
    )
    data_table(
        "Dimension pass-rate trends",
        ["dimension", "trend", "latest", "change", "samples"],
        [
            [d.name, d.spark(), f"{d.latest:.0%}", format_delta(d.delta), str(len(d.series))]
            for d in summary.dimensions
        ],
    )
    if summary.coverage_latest is True:
        success("The latest snapshot has complete coverage.")
    elif summary.coverage_latest is False:
        warn("The latest snapshot has incomplete coverage; run `omni eval --coverage`.")
    console.print(
        "  Trends come from eval_history.jsonl; --record appends snapshots and pt means percentage points.",
        style="dim",
    )


def _write_trend_html(summary, paths, *, html_path: str, open_browser: bool) -> None:  # noqa: ANN001
    """Write the trend summary to a static HTML file (``--html`` / ``--html-path``).

    An empty ``html_path`` uses the default ``~/.omni/eval_trend.html``; a
    non-empty value is taken as the output path.
    """
    from omni.eval import render_trend_html

    out = Path(html_path).expanduser() if html_path else Path(paths.home) / "eval_trend.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_trend_html(summary), encoding="utf-8")
    success(f"Generated static HTML trend dashboard: {out}")
    if open_browser:
        import webbrowser

        try:
            webbrowser.open(out.as_uri())
        except Exception as exc:  # noqa: BLE001 — opening a browser is best-effort
            warn(f"Could not open the browser automatically ({exc}); open {out} manually.")


async def _render_coverage(
    *,
    source: Path | None = None,
    tag: str | None = None,
    persona: str | None = None,
    as_json: bool = False,
) -> None:
    from omni.eval import audit_coverage
    from omni.eval.coverage import TARGET_PERSONAS, target_capabilities, target_dimensions
    from omni.eval.scenarios import load_scenarios

    scenarios = load_scenarios(source, tag=tag, persona=persona)
    cov = audit_coverage(scenarios)

    if as_json:
        console.print_json(_json.dumps(cov.to_dict(), ensure_ascii=False))
        return

    caps, dims = target_capabilities(), target_dimensions()
    data_table(
        "Coverage audit",
        ["target set", "covered/total", "coverage", "gaps"],
        [
            [
                "capability",
                f"{len(cov.covered_capabilities & caps)}/{len(caps)}",
                f"{cov._rate(cov.covered_capabilities, caps):.0%}",
                ", ".join(sorted(cov.missing_capabilities)) or "—",
            ],
            [
                "dimension",
                f"{len(cov.covered_dimensions & dims)}/{len(dims)}",
                f"{cov._rate(cov.covered_dimensions, dims):.0%}",
                ", ".join(sorted(cov.missing_dimensions)) or "—",
            ],
            [
                "persona",
                f"{len(cov.covered_personas & set(TARGET_PERSONAS))}/{len(TARGET_PERSONAS)}",
                f"{cov._rate(cov.covered_personas, set(TARGET_PERSONAS)):.0%}",
                ", ".join(sorted(cov.missing_personas)) or "—",
            ],
        ],
    )
    if cov.complete:
        success(f"Coverage is complete: {cov.scenarios_total} scenarios cover all target capabilities, dimensions, and personas.")
    else:
        error("Coverage is incomplete; the table lists capabilities, dimensions, or personas with no scenarios.")


__all__ = ["eval_command", "render_eval", "app_help"]
