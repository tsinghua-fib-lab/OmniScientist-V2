"""How a finished turn should be shown: success, partial, or failure.

Settlement already classifies a run as ``succeeded`` / ``degraded`` / ``failed``.
That status used to stop at the task row. The CLI then printed a green completion
line and ``omni exec -o`` said "Answer written" whenever *any* text existed —
including a 429 diagnostic stuffed into the requested blueprint path. Codex
writes ``--output-last-message`` for scripting and exits 1 on a failed turn; it
never claims the file is an answer. Omni keeps a first-class ``degraded``
(Codex has no such status) and labels the file and the terminal the same way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from omni.core.termination import (
    base_termination_reason,
    execution_outcome_status,
    termination_reason_label,
)

DisplayOutcome = Literal[
    "succeeded", "degraded", "failed", "needs_input", "cancelled", "interrupted"
]
ExecFileKind = Literal["answer", "partial", "error", "message"]

_FAILED_SETTLEMENTS = frozenset({"failed", "error", "verification_failed"})
_DEGRADED_SETTLEMENTS = frozenset({"degraded", "partial", "salvaged"})
_PENDING_SETTLEMENTS = frozenset({"pending", "pending_child_task"})
_SUCCESS_SETTLEMENTS = frozenset({"succeeded", "passed", "ok"})
_BOUND_EPS = 1e-9


def classify_turn_outcome(turn: Any) -> DisplayOutcome:
    """Map kind, settlement, stop reason, and warnings onto one display status.

    ``degraded_warnings`` and bound-saturated parameters are display signals:
    they do not rewrite the durable task row, but they stop a turn that only
    *looks* finished from being rendered as a full success.

    A user abort outranks an unfinished settlement: ``pending`` means the
    bookkeeping has not closed, not that the turn succeeded. Codex never paints
    ``TurnStatus::Interrupted`` as a completed turn; Omni keeps the same rule
    while still treating a non-aborted parent that submitted children as
    succeeded (the daemon / IM hand-off).
    """
    kind = str(getattr(turn, "kind", "") or "").lower()
    settled = str(getattr(turn, "settlement_status", "") or "").lower()
    reason = str(getattr(turn, "terminated_reason", "") or "")
    warnings = display_warnings(turn)

    if kind == "needs_input" or settled == "needs_input":
        return "needs_input"

    base = base_termination_reason(reason)
    head = reason.split(":", 1)[0].lower()
    if base == "cancelled" or head == "cancelled":
        return "cancelled"
    if base == "interrupted" or head == "interrupted":
        return "interrupted"

    if settled in _PENDING_SETTLEMENTS:
        return "succeeded"
    if kind == "error" or settled in _FAILED_SETTLEMENTS or settled.endswith("_failed"):
        return "failed"

    if kind == "partial" or settled in _DEGRADED_SETTLEMENTS or warnings:
        return "degraded"
    if settled in _SUCCESS_SETTLEMENTS:
        if hasattr(turn, "text") and not _has_visible_output(turn):
            return "failed"
        return "succeeded"
    if kind or reason:
        mapped = execution_outcome_status(kind or "text", reason)
        return mapped
    return _outcome_from_drained(turn)


def header_state(turn: Any) -> str:
    """Compact TUI footer label; a full success stays blank (the default)."""
    outcome = classify_turn_outcome(turn)
    if outcome == "succeeded":
        return ""
    if outcome == "needs_input":
        return "needs input"
    return outcome


def has_valid_answer(turn: Any) -> bool:
    """Whether ``turn.text`` is a deliverable rather than a failure log."""
    outcome = classify_turn_outcome(turn)
    if outcome in {"failed", "cancelled", "interrupted", "needs_input"}:
        return False
    return bool(str(getattr(turn, "text", "") or "").strip())


def _is_informational_host_fill(text: str) -> bool:
    """Successful host-fill notes are audit, not Partial success.

    ``Host filled remaining draft.manuscript via native synthesis.`` means the
    file is on this task. ``Host did not fill`` / ``could not fill`` / a fill
    that ``ended`` failed stay as display warnings.
    """
    if not text.startswith("Host filled remaining ") or " via " not in text:
        return False
    lowered = text.lower()
    return "could not fill" not in lowered and "ended " not in lowered


def informational_host_fill_notes(turn: Any) -> list[str]:
    """Successful host-fill lines kept on the audit list for a quiet info print."""
    seen: list[str] = []
    for item in getattr(turn, "degraded_warnings", None) or []:
        text = str(item or "").strip()
        if text and _is_informational_host_fill(text) and text not in seen:
            seen.append(text)
    return seen


def display_warnings(turn: Any) -> list[str]:
    """User-facing warnings: host degradation plus bound-saturated parameters.

    Successful host-fill notes stay on ``degraded_warnings`` for recovery and
    tests; they do not paint Partial success once the named files exist.
    """
    seen: list[str] = []
    for item in getattr(turn, "degraded_warnings", None) or []:
        text = str(item or "").strip()
        if text and text not in seen and not _is_informational_host_fill(text):
            seen.append(text)
    for payload in _result_payloads(turn):
        for text in bound_saturation_warnings(payload):
            if text not in seen:
                seen.append(text)
    return seen


def bound_saturation_warnings(payload: Any) -> list[str]:
    """Warn when every trial of a numeric parameter sits on a declared bound.

    A three-seed sweep that all land on ``temperature=0.2`` with
    ``bounds.temperature = [0.2, 1.0]`` is an optimisation signal, not a
    finished search. Skills may also emit ``constraint_hits`` directly.
    """
    if not isinstance(payload, dict):
        return []
    warnings: list[str] = []
    for hit in payload.get("constraint_hits") or []:
        if not isinstance(hit, dict):
            continue
        name = str(hit.get("name") or hit.get("parameter") or "").strip()
        if not name:
            continue
        value = hit.get("value")
        side = str(hit.get("bound") or hit.get("side") or "constraint").strip() or "constraint"
        limit = hit.get("limit", hit.get("bound_value"))
        detail = f"{name}={value}" if value is not None else name
        limit_note = f" ({limit})" if limit is not None else ""
        warnings.append(
            f"Parameter {detail} sat on the {side} bound{limit_note}; "
            "the optimum may lie outside the allowed range."
        )

    trials = _trial_rows(payload)
    bounds = payload.get("bounds") or payload.get("parameter_bounds")
    if not trials or not isinstance(bounds, dict):
        return warnings
    n = len(trials)
    for name, raw_bound in bounds.items():
        key = str(name or "").strip()
        if not key:
            continue
        lo, hi = _as_bound(raw_bound)
        values = [_numeric(row.get(key)) for row in trials]
        if any(value is None for value in values):
            continue
        if lo is not None and all(_near(value, lo) for value in values):
            warnings.append(
                f"All {n} seed(s) sat on the lower bound of {key} ({lo}); "
                "the search may be constrained rather than converged."
            )
        elif hi is not None and all(_near(value, hi) for value in values):
            warnings.append(
                f"All {n} seed(s) sat on the upper bound of {key} ({hi}); "
                "the search may be constrained rather than converged."
            )
    return warnings


def format_exec_file(turn: Any) -> tuple[ExecFileKind, str]:
    """Body and honesty label for ``omni exec -o``.

    Codex always writes the last agent message. Omni does too, so scripts still
    get a file, but a failed or empty turn is wrapped as an error report and a
    degraded turn is stamped so the file cannot be read as a finished blueprint.
    """
    outcome = classify_turn_outcome(turn)
    text = str(getattr(turn, "text", "") or "")
    if outcome == "needs_input":
        return "message", text
    if outcome in {"failed", "cancelled", "interrupted"} or not has_valid_answer(turn):
        return "error", _error_report(turn, text, outcome)
    if outcome == "degraded":
        return "partial", _partial_report(turn, text)
    return "answer", text


def persist_exec_output(path: Path, turn: Any) -> tuple[ExecFileKind, int]:
    """Write ``path`` and return the file kind plus the process exit code."""
    kind, body = format_exec_file(turn)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return kind, 1 if kind == "error" else 0


def exec_exit_code(turn: Any | None) -> int:
    """Codex exits 1 on a failed/interrupted turn; Omni matches that."""
    if turn is None:
        return 1
    return 1 if classify_turn_outcome(turn) in {"failed", "cancelled", "interrupted"} else 0


def _has_visible_output(turn: Any) -> bool:
    if str(getattr(turn, "text", "") or "").strip():
        return True
    if getattr(turn, "artifacts", None):
        return True
    for item in getattr(turn, "drained_results", None) or []:
        if isinstance(item, dict) and str(item.get("status") or "") == "succeeded":
            return True
    return False


def _outcome_from_drained(turn: Any) -> DisplayOutcome:
    statuses = [
        str(item.get("status") or "").lower()
        for item in (getattr(turn, "drained_results", None) or [])
        if isinstance(item, dict)
    ]
    if not statuses:
        return "succeeded"
    if any(status == "failed" for status in statuses) and not any(
        status == "succeeded" for status in statuses
    ):
        return "failed"
    if any(status in {"degraded", "failed", "partial"} for status in statuses):
        return "degraded"
    return "succeeded"


def _result_payloads(turn: Any) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for item in getattr(turn, "drained_results", None) or []:
        if not isinstance(item, dict):
            continue
        payloads.append(item)
        nested = item.get("result")
        if isinstance(nested, dict):
            payloads.append(nested)
    for record in getattr(turn, "tool_trace", None) or []:
        result = getattr(record, "result", None)
        if isinstance(result, dict):
            payloads.append(result)
    return payloads


def _trial_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("seeds", "trials", "runs"):
        rows = payload.get(key)
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
            return list(rows)
    return []


def _as_bound(value: Any) -> tuple[float | None, float | None]:
    if isinstance(value, dict):
        return _numeric(value.get("min")), _numeric(value.get("max"))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _numeric(value[0]), _numeric(value[1])
    return None, None


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _near(value: float | None, bound: float) -> bool:
    return value is not None and abs(value - bound) <= _BOUND_EPS


def _meta_lines(turn: Any, outcome: DisplayOutcome) -> list[str]:
    lines = [f"- status: `{outcome}`"]
    settled = str(getattr(turn, "settlement_status", "") or "").strip()
    if settled:
        lines.append(f"- verification: `{settled}`")
    reason = str(getattr(turn, "terminated_reason", "") or "").strip()
    if reason:
        lines.append(f"- reason: {termination_reason_label(reason)}")
    task_id = str(getattr(turn, "task_id", "") or "").strip()
    if task_id:
        lines.append(f"- task: `{task_id[:8]}`")
    for warning in display_warnings(turn):
        lines.append(f"- warning: {warning}")
    return lines


def _error_report(turn: Any, text: str, outcome: DisplayOutcome) -> str:
    meta = "\n".join(_meta_lines(turn, outcome))
    body = text.strip()
    parts = [
        "# Error report",
        "",
        "This file is not a completed answer. The turn failed or produced no deliverable.",
        "",
        meta,
    ]
    if body:
        parts.extend(["", "## What the run reported", "", body])
    return "\n".join(parts).rstrip() + "\n"


def _partial_report(turn: Any, text: str) -> str:
    meta = "\n".join(_meta_lines(turn, "degraded"))
    banner = (
        "> **Partial result (degraded)** — this is not a full success.\n"
        + "\n".join(f"> {line}" for line in meta.splitlines())
        + "\n"
    )
    body = text.rstrip()
    return f"{banner}\n{body}\n" if body else banner + "\n"
