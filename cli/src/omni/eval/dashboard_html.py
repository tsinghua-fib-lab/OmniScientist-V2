"""Static, self-contained HTML rendering of the eval trend dashboard (P3).

The terminal dashboard (``omni eval --trend``) draws unicode sparklines; this
module renders the *same* :class:`~omni.eval.trend.TrendSummary` as a single,
dependency-free HTML file: inline SVG line charts, inline CSS, no JavaScript, no
external assets. So a run's trend can be opened in a browser or committed as an
artifact and viewed anywhere, while staying local-first and offline (``omni
serve`` has no HTTP server and the repo has no web framework dep).

Everything user/data-derived is HTML-escaped; charts are pure geometry over the
recorded pass-rate series (0..1), so the page is safe to write and share.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from omni.eval.trend import format_delta

if TYPE_CHECKING:
    from omni.eval.trend import TrendSummary

_ACCENT = "#2563eb"
_ACCENT_FILL = "#2563eb1f"  # ~12% alpha


# ── inline-SVG chart helpers ────────────────────────────────────────────────


def _chart_points(
    series: list[float], *, width: float, height: float, pad: float,
    lo: float = 0.0, hi: float = 1.0,
) -> list[tuple[float, float]]:
    """Map a value series to SVG (x, y) points (one point per value).

    x is spread evenly across the inner width (a single value sits centred); y is
    the value scaled into ``[lo, hi]`` and inverted (SVG y grows downward). An
    empty series yields no points (callers render a bare baseline).
    """
    n = len(series)
    if n == 0:
        return []
    inner_w = max(1.0, width - 2 * pad)
    inner_h = max(1.0, height - 2 * pad)
    span = (hi - lo) or 1.0
    points: list[tuple[float, float]] = []
    for i, value in enumerate(series):
        x = pad + inner_w * (i / (n - 1)) if n > 1 else pad + inner_w / 2
        frac = (float(value) - lo) / span
        frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
        y = pad + inner_h * (1.0 - frac)
        points.append((round(x, 1), round(y, 1)))
    return points


def _points_attr(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x},{y}" for x, y in points)


def _svg_line_chart(
    series: list[float], *, width: float = 560.0, height: float = 140.0,
    stroke: str = _ACCENT, fill: str = _ACCENT_FILL,
) -> str:
    """A filled line chart for a pass-rate series (0..1), as inline ``<svg>``.

    Draws top/mid/bottom guide lines, an area fill under the curve, the line
    itself, and a marker on the latest point. Empty/one-point series stay safe
    (a bare baseline / a single dot).
    """
    pad = 10.0
    points = _chart_points(series, width=width, height=height, pad=pad)
    baseline = height - pad
    guides = "".join(
        f'<line x1="{pad}" y1="{round(y, 1)}" x2="{width - pad}" y2="{round(y, 1)}" '
        f'stroke="#e5e7eb" stroke-width="1" />'
        for y in (pad, height / 2, baseline)
    )
    body = ""
    if len(points) >= 2:
        area = f"{pad},{baseline} {_points_attr(points)} {width - pad},{baseline}"
        body = (
            f'<polygon points="{area}" fill="{fill}" stroke="none" />'
            f'<polyline points="{_points_attr(points)}" fill="none" '
            f'stroke="{stroke}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />'
        )
    if points:
        cx, cy = points[-1]
        body += f'<circle cx="{cx}" cy="{cy}" r="3.5" fill="{stroke}" />'
    return (
        f'<svg class="chart" viewBox="0 0 {width:g} {height:g}" '
        f'preserveAspectRatio="none" role="img" xmlns="http://www.w3.org/2000/svg">'
        f"{guides}{body}</svg>"
    )


def _svg_sparkline(
    series: list[float], *, width: float = 180.0, height: float = 40.0, stroke: str = _ACCENT,
) -> str:
    """A minimal axis-less line (per-dimension card), as inline ``<svg>``."""
    pad = 4.0
    points = _chart_points(series, width=width, height=height, pad=pad)
    body = ""
    if len(points) >= 2:
        body = (
            f'<polyline points="{_points_attr(points)}" fill="none" '
            f'stroke="{stroke}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />'
        )
    if points:
        cx, cy = points[-1]
        body += f'<circle cx="{cx}" cy="{cy}" r="2.5" fill="{stroke}" />'
    return (
        f'<svg class="spark" viewBox="0 0 {width:g} {height:g}" '
        f'preserveAspectRatio="none" role="img" xmlns="http://www.w3.org/2000/svg">{body}</svg>'
    )


# ── page assembly ───────────────────────────────────────────────────────────


def _delta_html(delta: float | None) -> str:
    """A coloured delta badge (green up / red down / grey flat / new)."""
    text = html.escape(format_delta(delta))
    if delta is None or abs(delta) < 5e-4:
        cls = "flat"
    else:
        cls = "up" if delta > 0 else "down"
    return f'<span class="delta {cls}">{text}</span>'


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _coverage_badge(coverage_latest: bool | None) -> str:
    if coverage_latest is True:
        return '<span class="badge ok">Coverage complete</span>'
    if coverage_latest is False:
        return '<span class="badge warn">Coverage incomplete</span>'
    return '<span class="badge muted">Coverage unknown</span>'


_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem; font: 15px/1.5 -apple-system, BlinkMacSystemFont,
  "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #111827; background: #f9fafb; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
.sub { color: #6b7280; margin: 0 0 1.5rem; font-size: .9rem; }
.overview { background: #fff; border: 1px solid #e5e7eb; border-radius: 14px;
  padding: 1.25rem 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
.score-row { display: flex; align-items: baseline; gap: .75rem; margin-bottom: .75rem; }
.score-big { font-size: 2.4rem; font-weight: 700; letter-spacing: -.02em; }
.chart { width: 100%; height: 140px; display: block; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 1rem; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 12px;
  padding: 1rem 1.1rem; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
.card .name { font-weight: 600; margin-bottom: .5rem; word-break: break-word; }
.spark { width: 100%; height: 40px; display: block; margin: .25rem 0 .6rem; }
.metrics { display: flex; align-items: baseline; justify-content: space-between; }
.latest { font-size: 1.5rem; font-weight: 700; }
.samples { color: #9ca3af; font-size: .8rem; }
.delta { font-size: .85rem; font-weight: 600; padding: .1rem .4rem; border-radius: 6px; }
.delta.up { color: #047857; background: #ecfdf5; }
.delta.down { color: #b91c1c; background: #fef2f2; }
.delta.flat { color: #6b7280; background: #f3f4f6; }
.badge { display: inline-block; font-size: .8rem; font-weight: 600;
  padding: .2rem .6rem; border-radius: 999px; }
.badge.ok { color: #047857; background: #ecfdf5; }
.badge.warn { color: #b45309; background: #fffbeb; }
.badge.muted { color: #6b7280; background: #f3f4f6; }
footer { margin-top: 1.75rem; color: #9ca3af; font-size: .8rem; }
@media (prefers-color-scheme: dark) {
  body { color: #e5e7eb; background: #0b0f19; }
  .overview, .card { background: #111827; border-color: #1f2937; }
  .sub { color: #9ca3af; }
}
""".strip()


def render_trend_html(summary: TrendSummary, *, title: str = "OmniScientist evaluation trend") -> str:
    """Render a :class:`TrendSummary` as a full, self-contained HTML page.

    The page needs no network and no JavaScript: inline CSS + inline SVG only.
    All text (title, dimension names, timestamps) is HTML-escaped.
    """
    safe_title = html.escape(title)
    if summary.count == 0:
        return _empty_page(safe_title)

    span = (
        html.escape(summary.first_ts)
        if summary.count == 1
        else f"{html.escape(summary.first_ts)} → {html.escape(summary.last_ts)}"
    )
    cards = "\n".join(
        f'<div class="card"><div class="name">{html.escape(d.name)}</div>'
        f"{_svg_sparkline(d.series)}"
        f'<div class="metrics"><span class="latest">{_pct(d.latest)}</span>'
        f"{_delta_html(d.delta)}</div>"
        f'<div class="samples">{len(d.series)} records</div></div>'
        for d in summary.dimensions
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{safe_title}</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>{safe_title}</h1>
<p class="sub">{summary.count} records · {span} · {_coverage_badge(summary.coverage_latest)}</p>
<section class="overview">
  <div class="score-row">
    <span class="score-big">{_pct(summary.score_latest)}</span>
    {_delta_html(summary.score_delta)}
    <span class="samples">Overall score (change)</span>
  </div>
  {_svg_line_chart(summary.score_series)}
</section>
<h2 style="font-size:1.05rem;margin:0 0 .75rem;">Pass rate by dimension</h2>
<div class="grid">
{cards}
</div>
<footer>Read from eval_history.jsonl, appended by <code>omni eval --record</code>. "pt" means percentage points. This static offline page does not rerun evaluations.</footer>
</body>
</html>
"""


def _empty_page(safe_title: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>{safe_title}</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>{safe_title}</h1>
<p class="sub">No trend history. Record evaluation scores with <code>omni eval --record</code>.</p>
</body>
</html>
"""


__all__ = ["render_trend_html"]
