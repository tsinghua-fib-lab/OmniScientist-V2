"""Eval time-series dashboard: sparklines + per-dimension trend over history."""

from __future__ import annotations

from omni.eval import format_delta, sparkline, summarize_history


def _snap(ts: str, score: float, dims: dict[str, float], coverage: bool | None = None) -> dict:
    return {"ts": ts, "score": score, "dimensions": dims, "coverage_complete": coverage}


def test_sparkline_maps_extremes_to_low_and_high_ticks():
    line = sparkline([0.0, 1.0])
    assert line[0] == "▁"
    assert line[-1] == "█"
    assert len(line) == 2


def test_sparkline_empty_and_flat_series():
    assert sparkline([]) == ""
    # a flat series is legible (mid tick), never crashes on a zero span
    assert set(sparkline([0.5, 0.5, 0.5])) == {"▅"}


def test_sparkline_clamps_out_of_range_values():
    assert sparkline([-1.0, 2.0]) == "▁█"


def test_format_delta_arrows_and_new():
    assert format_delta(None) == "—"
    assert format_delta(0.0).startswith("＝")
    assert format_delta(0.25).startswith("▲")
    assert format_delta(-0.1).startswith("▼")
    assert "10.0pt" in format_delta(-0.1)


def test_summarize_empty_history_is_safe():
    summary = summarize_history([])
    assert summary.count == 0
    assert summary.dimensions == []
    assert summary.score_latest == 0.0
    assert summary.score_delta is None


def test_summarize_builds_score_and_dimension_series():
    history = [
        _snap("2026-07-01T09:00:00+00:00", 0.80, {"routing": 1.0, "safety": 0.5}, False),
        _snap("2026-07-05T09:00:00+00:00", 0.90, {"routing": 1.0, "safety": 0.75}, True),
        _snap("2026-07-10T09:00:00+00:00", 0.95, {"routing": 1.0, "safety": 1.0}, True),
    ]
    summary = summarize_history(history)
    assert summary.count == 3
    assert summary.first_ts.startswith("2026-07-01")
    assert summary.last_ts.startswith("2026-07-10")
    assert summary.score_series == [0.80, 0.90, 0.95]
    assert summary.score_latest == 0.95
    assert abs(summary.score_delta - 0.05) < 1e-9
    assert summary.coverage_latest is True

    safety = next(d for d in summary.dimensions if d.name == "safety")
    assert safety.series == [0.5, 0.75, 1.0]
    assert abs(safety.delta - 0.25) < 1e-9
    assert len(safety.spark()) == 3


def test_later_introduced_dimension_is_not_backfilled():
    """A dimension added by a new epic trends over only the runs that recorded it."""
    history = [
        _snap("2026-07-01T09:00:00+00:00", 0.80, {"routing": 1.0}),
        _snap("2026-07-05T09:00:00+00:00", 0.90, {"routing": 1.0, "streaming": 1.0}),
        _snap("2026-07-10T09:00:00+00:00", 0.95, {"routing": 1.0, "streaming": 1.0}),
    ]
    summary = summarize_history(history)
    streaming = next(d for d in summary.dimensions if d.name == "streaming")
    assert streaming.series == [1.0, 1.0]  # only the 2 runs that had it, no zero back-fill
    assert streaming.delta == 0.0


def test_tail_keeps_only_recent_snapshots():
    history = [
        _snap("2026-07-01T09:00:00+00:00", 0.10, {"routing": 0.1}),
        _snap("2026-07-05T09:00:00+00:00", 0.50, {"routing": 0.5}),
        _snap("2026-07-10T09:00:00+00:00", 0.95, {"routing": 0.95}),
    ]
    summary = summarize_history(history, tail=2)
    assert summary.count == 2
    assert summary.score_series == [0.50, 0.95]
    assert summary.first_ts.startswith("2026-07-05")


def test_to_dict_is_json_serialisable_and_rounded():
    history = [
        _snap("2026-07-01T09:00:00+00:00", 0.8123, {"routing": 0.6666}),
        _snap("2026-07-05T09:00:00+00:00", 0.9001, {"routing": 1.0}),
    ]
    payload = summarize_history(history).to_dict()
    assert payload["count"] == 2
    assert payload["score"]["latest"] == 0.9001
    assert payload["score"]["spark"]
    assert payload["dimensions"][0]["name"] == "routing"
    import json

    json.dumps(payload)  # must not raise


def _seed_history(snaps: list[dict]) -> None:
    from omni.config import load_settings
    from omni.eval import append_snapshot, default_history_path

    path = default_history_path(load_settings().paths)
    for snap in snaps:
        append_snapshot(snap, path=path)


def test_eval_trend_cli_renders_from_history() -> None:
    from typer.testing import CliRunner

    from omni.cli.main import app

    _seed_history([
        _snap("2026-07-01T09:00:00+00:00", 0.80, {"routing": 1.0, "safety": 0.5}, False),
        _snap("2026-07-10T09:00:00+00:00", 0.95, {"routing": 1.0, "safety": 1.0}, True),
    ])
    res = CliRunner().invoke(app, ["eval", "--trend", "--json"])
    assert res.exit_code == 0
    assert "routing" in res.stdout
    assert "safety" in res.stdout


def test_eval_trend_cli_without_history_warns() -> None:
    from typer.testing import CliRunner

    from omni.cli.main import app

    res = CliRunner().invoke(app, ["eval", "--trend"])
    assert res.exit_code == 0
    assert "No trend history exists" in res.stdout
