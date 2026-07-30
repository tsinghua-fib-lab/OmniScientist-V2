"""Time-series trend gate for the capability benchmark (regression baseline).

``omni eval`` scores the agent *now*; this module remembers past scores so CI (or
a pre-commit hook) can fail when a change **regresses** a capability dimension
relative to the last recorded run — the "trend gate" that backs every epic's
regression net. Storage is a boring append-only JSONL so it diffs cleanly and
needs no schema.

Each entry is a compact snapshot: overall pass-rate, per-dimension rates, and
whether the coverage net is complete. ``detect_regressions`` compares a fresh
snapshot to a baseline and returns human-readable regressions (empty ⇒ green).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omni.eval.report import BenchmarkReport


def default_history_path(paths: Any = None) -> Path:
    """Where trend snapshots are appended under the active Omni data directory."""
    if paths is not None and getattr(paths, "home", None):
        return Path(paths.home) / "eval_history.jsonl"
    from omni.config.paths import user_home

    return user_home() / "eval_history.jsonl"


def report_snapshot(
    report: BenchmarkReport,
    *,
    coverage_complete: bool | None = None,
    label: str = "",
) -> dict[str, Any]:
    """Build a comparable, serialisable snapshot from a benchmark report."""
    return {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "label": label,
        "score": round(report.score, 4),
        "scenarios_passed": report.scenarios_passed,
        "scenarios_total": len(report.results),
        "checks_passed": report.passed_checks,
        "checks_total": report.total_checks,
        "dimensions": {d.name: round(d.rate, 4) for d in report.dimensions()},
        "coverage_complete": coverage_complete,
    }


def append_snapshot(snapshot: dict[str, Any], *, path: Path) -> None:
    """Append one snapshot as a JSONL line (creating the file/dir if needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def load_history(path: Path) -> list[dict[str, Any]]:
    """Read all recorded snapshots (skips malformed lines)."""
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def last_snapshot(path: Path) -> dict[str, Any] | None:
    """Most recent recorded snapshot, or ``None`` when there is no history."""
    history = load_history(path)
    return history[-1] if history else None


def detect_regressions(
    current: dict[str, Any],
    baseline: dict[str, Any] | None,
    *,
    score_tolerance: float = 0.0,
    dim_tolerance: float = 0.0,
) -> list[str]:
    """Return human-readable regressions of ``current`` vs ``baseline``.

    A regression is: the overall score dropped by more than ``score_tolerance``,
    any dimension present in the baseline dropped by more than ``dim_tolerance``,
    or a previously-complete coverage net became incomplete. No baseline ⇒ no
    regressions (the first run establishes the baseline).
    """
    if not baseline:
        return []
    out: list[str] = []
    base_score = float(baseline.get("score", 0.0) or 0.0)
    cur_score = float(current.get("score", 0.0) or 0.0)
    if cur_score < base_score - score_tolerance:
        out.append(f"overall score {cur_score:.0%} < baseline {base_score:.0%}")

    base_dims = baseline.get("dimensions") or {}
    cur_dims = current.get("dimensions") or {}
    for name, base_rate in sorted(base_dims.items()):
        cur_rate = float(cur_dims.get(name, 0.0) or 0.0)
        if cur_rate < float(base_rate or 0.0) - dim_tolerance:
            out.append(f"dimension '{name}' {cur_rate:.0%} < baseline {float(base_rate):.0%}")

    if baseline.get("coverage_complete") is True and current.get("coverage_complete") is False:
        out.append("coverage net regressed from complete → incomplete")
    return out


__all__ = [
    "default_history_path",
    "report_snapshot",
    "append_snapshot",
    "load_history",
    "last_snapshot",
    "detect_regressions",
]
