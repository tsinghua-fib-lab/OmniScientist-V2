"""Static HTML trend dashboard: self-contained page, safe SVG helpers, CLI wiring."""

from __future__ import annotations

from pathlib import Path

from omni.eval import DimensionTrend, TrendSummary, render_trend_html, summarize_history
from omni.eval.dashboard_html import _chart_points, _svg_line_chart, _svg_sparkline


def _snap(ts: str, score: float, dims: dict[str, float], coverage: bool | None = None) -> dict:
    return {"ts": ts, "score": score, "dimensions": dims, "coverage_complete": coverage}


def _summary() -> TrendSummary:
    history = [
        _snap("2026-07-01T09:00:00+00:00", 0.80, {"routing": 1.0, "safety": 0.5}, False),
        _snap("2026-07-10T09:00:00+00:00", 0.95, {"routing": 1.0, "safety": 1.0}, True),
    ]
    return summarize_history(history)


# ── page assembly ───────────────────────────────────────────────────────────


def test_render_trend_html_is_self_contained() -> None:
    out = render_trend_html(_summary())
    assert out.startswith("<!doctype html>")
    assert "<svg" in out  # inline charts, not <img>
    assert "routing" in out and "safety" in out  # dimension names present
    assert "95%" in out  # latest overall pass-rate
    assert "Coverage complete" in out  # coverage badge from latest snapshot


def test_render_trend_html_has_no_external_assets_or_scripts() -> None:
    """No JS and no networked assets — safe to open/commit anywhere, offline."""
    out = render_trend_html(_summary())
    assert "<script" not in out  # no JavaScript at all
    assert 'src="http' not in out  # no external script/image
    assert 'href="http' not in out  # no external stylesheet/link
    assert "@import" not in out  # no CSS @import of a remote sheet
    # the only permitted absolute URL is the inert SVG namespace
    assert out.count("http") == out.count("http://www.w3.org/2000/svg")


def test_render_trend_html_escapes_injected_dimension_name() -> None:
    """A hostile dimension name is HTML-escaped, never emitted as live markup."""
    summary = TrendSummary(
        count=2, first_ts="2026-07-01", last_ts="2026-07-10",
        score_series=[0.5, 0.6],
        dimensions=[DimensionTrend(name="<b>pwn</b>", series=[0.5, 0.6])],
        coverage_latest=True,
    )
    out = render_trend_html(summary)
    assert "<b>pwn</b>" not in out
    assert "&lt;b&gt;pwn&lt;/b&gt;" in out


def test_render_trend_html_empty_summary_is_safe() -> None:
    out = render_trend_html(summarize_history([]))
    assert out.startswith("<!doctype html>")
    assert "No trend history" in out
    assert "<script" not in out


# ── inline-SVG helpers ──────────────────────────────────────────────────────


def test_chart_points_count_matches_series() -> None:
    pts = _chart_points([0.1, 0.5, 0.9], width=100, height=40, pad=5)
    assert len(pts) == 3
    # x is monotonically increasing across the inner width; y inverted (0.9 highest → smallest y)
    assert pts[0][0] < pts[1][0] < pts[2][0]
    assert pts[2][1] < pts[0][1]


def test_chart_points_single_value_is_centred() -> None:
    (pt,) = _chart_points([0.5], width=100, height=40, pad=10)
    assert pt[0] == 50.0  # pad + inner_w/2


def test_chart_points_empty_series_is_empty() -> None:
    assert _chart_points([], width=100, height=40, pad=5) == []


def test_svg_helpers_tolerate_empty_and_single_series() -> None:
    for empty in (_svg_line_chart([]), _svg_sparkline([])):
        assert empty.startswith("<svg") and empty.rstrip().endswith("</svg>")
        assert "polyline" not in empty  # nothing to connect
    # a single point renders a marker but no connecting line
    one = _svg_sparkline([0.7])
    assert "<circle" in one and "polyline" not in one


def test_svg_line_chart_draws_line_and_marker_for_series() -> None:
    svg = _svg_line_chart([0.2, 0.6, 0.9])
    assert "<polyline" in svg  # the trend line
    assert "<polygon" in svg  # the area fill under it
    assert "<circle" in svg  # latest-point marker


# ── CLI wiring ──────────────────────────────────────────────────────────────


def _seed_history(snaps: list[dict]) -> None:
    from omni.config import load_settings
    from omni.eval import append_snapshot, default_history_path

    path = default_history_path(load_settings().paths)
    for snap in snaps:
        append_snapshot(snap, path=path)


def test_eval_html_path_cli_writes_file(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from omni.cli.main import app

    _seed_history([
        _snap("2026-07-01T09:00:00+00:00", 0.80, {"routing": 1.0, "safety": 0.5}, False),
        _snap("2026-07-10T09:00:00+00:00", 0.95, {"routing": 1.0, "safety": 1.0}, True),
    ])
    out = tmp_path / "trend.html"
    res = CliRunner().invoke(app, ["eval", "--html-path", str(out)])
    assert res.exit_code == 0
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>")
    assert "<svg" in text
    assert "routing" in text and "safety" in text


def test_eval_html_flag_uses_default_path() -> None:
    from typer.testing import CliRunner

    from omni.cli.main import app
    from omni.config import load_settings

    _seed_history([
        _snap("2026-07-01T09:00:00+00:00", 0.80, {"routing": 1.0}),
        _snap("2026-07-10T09:00:00+00:00", 0.95, {"routing": 1.0}),
    ])
    res = CliRunner().invoke(app, ["eval", "--trend", "--html"])
    assert res.exit_code == 0
    out = Path(load_settings().paths.home) / "eval_trend.html"
    assert out.exists()
    assert "<!doctype html>" in out.read_text(encoding="utf-8")


def test_eval_html_without_history_warns_and_writes_nothing(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from omni.cli.main import app

    out = tmp_path / "trend.html"
    res = CliRunner().invoke(app, ["eval", "--html-path", str(out)])
    assert res.exit_code == 0
    assert "No trend history exists" in res.stdout
    assert not out.exists()
