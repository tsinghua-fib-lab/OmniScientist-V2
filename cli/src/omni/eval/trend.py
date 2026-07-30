"""Time-series dashboard over recorded eval snapshots (regression trend view).

``omni eval --record`` appends one snapshot per run to ``eval_history.jsonl`` and
``omni eval --gate`` fails CI on regressions. This module turns that same
append-only log into a compact **trend dashboard**: per-dimension sparklines, the
latest pass-rate, and the delta vs the previous run, so improvement through use
becomes visible *over time*, not just at a single point.

Pure and offline: it reads the JSONL history and renders unicode sparklines, so
it needs no plotting deps and runs anywhere the CLI runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_SPARK_TICKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], *, lo: float = 0.0, hi: float = 1.0) -> str:
    """Render ``values`` (0..1 rates by default) as a unicode block sparkline.

    Values are clamped to ``[lo, hi]``; an empty series renders as empty string.
    A flat/degenerate range collapses to the lowest tick so the row still reads.
    """
    if not values:
        return ""
    span = hi - lo
    ticks = _SPARK_TICKS
    out: list[str] = []
    for value in values:
        if span <= 0:
            out.append(ticks[0])
            continue
        frac = (float(value) - lo) / span
        frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
        out.append(ticks[round(frac * (len(ticks) - 1))])
    return "".join(out)


@dataclass
class DimensionTrend:
    """Per-dimension pass-rate series across the recorded snapshots."""

    name: str
    series: list[float]

    @property
    def latest(self) -> float:
        return self.series[-1] if self.series else 0.0

    @property
    def previous(self) -> float | None:
        return self.series[-2] if len(self.series) >= 2 else None

    @property
    def delta(self) -> float | None:
        prev = self.previous
        return None if prev is None else self.latest - prev

    def spark(self) -> str:
        return sparkline(self.series)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "series": [round(v, 4) for v in self.series],
            "latest": round(self.latest, 4),
            "delta": None if self.delta is None else round(self.delta, 4),
            "spark": self.spark(),
        }


@dataclass
class TrendSummary:
    """A comparable, renderable summary of the whole eval history."""

    count: int
    first_ts: str
    last_ts: str
    score_series: list[float]
    dimensions: list[DimensionTrend]
    coverage_latest: bool | None

    @property
    def score_latest(self) -> float:
        return self.score_series[-1] if self.score_series else 0.0

    @property
    def score_previous(self) -> float | None:
        return self.score_series[-2] if len(self.score_series) >= 2 else None

    @property
    def score_delta(self) -> float | None:
        prev = self.score_previous
        return None if prev is None else self.score_latest - prev

    def score_spark(self) -> str:
        return sparkline(self.score_series)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "score": {
                "series": [round(v, 4) for v in self.score_series],
                "latest": round(self.score_latest, 4),
                "delta": None if self.score_delta is None else round(self.score_delta, 4),
                "spark": self.score_spark(),
            },
            "dimensions": [d.to_dict() for d in self.dimensions],
            "coverage_latest": self.coverage_latest,
        }


def summarize_history(
    history: list[dict[str, Any]],
    *,
    tail: int | None = None,
) -> TrendSummary:
    """Build a :class:`TrendSummary` from recorded snapshots (oldest→newest).

    ``tail`` keeps only the most recent N snapshots (``None``/0 ⇒ all). Each
    dimension's series is taken from the snapshots that actually recorded it, so
    a dimension introduced later (a new epic) still trends cleanly instead of
    being back-filled with zeros.
    """
    snaps = [s for s in history if isinstance(s, dict)]
    if tail and tail > 0:
        snaps = snaps[-tail:]
    if not snaps:
        return TrendSummary(0, "", "", [], [], None)

    score_series = [float(s.get("score", 0.0) or 0.0) for s in snaps]

    names: list[str] = []
    seen: set[str] = set()
    for snap in snaps:
        for name in (snap.get("dimensions") or {}):
            if name not in seen:
                seen.add(name)
                names.append(name)

    dimensions: list[DimensionTrend] = []
    for name in sorted(names):
        series = [
            float(dims[name])
            for snap in snaps
            if isinstance((dims := snap.get("dimensions") or {}), dict) and name in dims
        ]
        if series:
            dimensions.append(DimensionTrend(name=name, series=series))

    return TrendSummary(
        count=len(snaps),
        first_ts=str(snaps[0].get("ts", "")),
        last_ts=str(snaps[-1].get("ts", "")),
        score_series=score_series,
        dimensions=dimensions,
        coverage_latest=snaps[-1].get("coverage_complete"),
    )


def format_delta(delta: float | None) -> str:
    """Human-readable delta as an arrow + percentage-point change (``—`` if new)."""
    if delta is None:
        return "—"
    if abs(delta) < 5e-4:
        return "＝ 0.0pt"
    arrow = "▲" if delta > 0 else "▼"
    return f"{arrow} {abs(delta) * 100:.1f}pt"


__all__ = [
    "sparkline",
    "DimensionTrend",
    "TrendSummary",
    "summarize_history",
    "format_delta",
]
