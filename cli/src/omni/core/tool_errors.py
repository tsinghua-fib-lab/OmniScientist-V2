"""Recoverable tool-error classes the model can act on.

The ReAct loop already returns every failure. These labels tell the model
whether to retry the same tool, wait, keep going with leftover artifacts, or
stop one debt. They are host-owned facts, not a second judge.
"""

from __future__ import annotations

import re
from typing import Any

INVALID_ARGS = "invalid_args"
RETRYABLE_IO = "retryable_io"
SKILL_FAILED_PARTIAL = "skill_failed_partial"
UNPAYABLE = "unpayable"
FATAL_TURN = "fatal_turn"

ERROR_CLASSES = (
    INVALID_ARGS,
    RETRYABLE_IO,
    SKILL_FAILED_PARTIAL,
    UNPAYABLE,
    FATAL_TURN,
)

_INVALID_CODES = frozenset(
    {
        "unknown_tool",
        "tool_arguments_invalid",
        "tool_arguments_truncated",
        "tool_contract_violation",
        "tool_policy_rejected",
        "tool_approval_required",
    }
)
_RETRYABLE_CODES = frozenset(
    {
        "tool_circuit_open",
        "tool_timeout",
        "timed_out",
        "timeout",
        "rate_limited",
        "429",
    }
)
_UNPAYABLE_CODES = frozenset(
    {
        "unpayable",
        "runtime_dependency_missing",
        "vlm_unavailable",
        "vlm_not_configured",
        "node_unavailable",
        "pptx_unavailable",
        "graphviz_unavailable",
    }
)
_FATAL_CODES = frozenset(
    {
        "sandbox_escape",
        "storage_corrupt",
        "storage_corrupted",
    }
)
_TRACEBACK_MARKERS = (
    "Traceback (most recent call last)",
    'File "',
    "  File ",
)
_SKILL_TOOLS = frozenset(
    {
        "run_skill",
        "livefigure",
        "research-pptx",
        "scientific-figure",
        "paper-review",
        "research-poster",
    }
)

_VLM_RE = re.compile(r"\bvlm\b|vision model|visual language", re.IGNORECASE)
_PPTX_RE = re.compile(r"\bpptx\b|\bnode(?:js)?\b|npm ci|research-pptx", re.IGNORECASE)
_DOT_RE = re.compile(r"\bdot\b|graphviz|not an already-written", re.IGNORECASE)


def classify_tool_error(
    *,
    name: str = "",
    error_code: str = "",
    status: str = "",
    result: Any = None,
    error: str = "",
    retryable: bool = False,
) -> str:
    """Return one of the five host error classes, or ``""`` on success."""
    if isinstance(result, dict) and str(result.get("error_class") or "").strip() in ERROR_CLASSES:
        return str(result["error_class"]).strip()
    code = str(error_code or "").strip()
    if isinstance(result, dict):
        code = code or str(result.get("error_code") or result.get("code") or "").strip()
        if not code and result.get("contract_violation") is True:
            code = "tool_contract_violation"
    lowered = code.lower()
    if lowered in _FATAL_CODES or _looks_fatal(error, result):
        return FATAL_TURN
    if _is_inspect_record(result) and str(status or "").lower() in {"", "succeeded", "completed"}:
        # get_task / get_subtask carry the inspected object's status. That is
        # not a tool failure — same rule as recall_result_outcome.
        return ""
    if lowered in _INVALID_CODES:
        return INVALID_ARGS
    # Transient provider HTTP (503/429) wins over a generic "vlm" substring.
    # Last week's Images path made livefigure hit /v1/images/generations; a
    # gateway 503 is retryable, not "VLM is not configured".
    if lowered in _RETRYABLE_CODES or retryable or _looks_retryable(error, result):
        if lowered not in _UNPAYABLE_CODES and not _looks_config_unpayable(error, result):
            return RETRYABLE_IO
    if lowered in _UNPAYABLE_CODES or _looks_unpayable(error, result):
        return UNPAYABLE
    if _is_failure(status, error, result):
        if _has_partial_outputs(result) or _is_skill_name(name, result):
            return SKILL_FAILED_PARTIAL
        if _looks_invalid_args(error, result):
            return INVALID_ARGS
        if _is_skill_name(name, result):
            return SKILL_FAILED_PARTIAL
    return ""


def short_skill_observation(
    name: str,
    *,
    result: Any = None,
    error: str = "",
    error_class: str = "",
    status: str = "failed",
) -> dict[str, Any] | None:
    """Return a short, actionable skill failure payload, or ``None`` if not a skill."""
    if _is_inspect_record(result):
        return None
    skill = _skill_name(name, result)
    if not skill and not _is_skill_name(name, result):
        return None
    if not _is_failure(status, error, result) and not (
        isinstance(result, dict) and str(result.get("status") or "").lower()
        in {"failed", "error", "timed_out", "degraded"}
    ):
        if not error:
            return None
    raw_error = str(error or "").strip()
    if not raw_error and isinstance(result, dict):
        raw_error = str(result.get("error") or result.get("message") or "").strip()
    short = strip_traceback(raw_error) or "skill failed"
    hint = _remediation_hint(skill, short, result)
    payload: dict[str, Any] = {
        "status": status or "failed",
        "error_class": error_class or SKILL_FAILED_PARTIAL,
        "error": short,
        "skill_name": skill or name,
    }
    if hint:
        payload["hint"] = hint
        payload["next_action"] = hint
    leftover = _partial_outputs(result)
    if leftover:
        payload["partial_outputs"] = leftover
        payload["error_class"] = SKILL_FAILED_PARTIAL
    return payload


def strip_traceback(text: str) -> str:
    """Keep the first actionable line; drop a 120s traceback wall."""
    body = str(text or "").strip()
    if not body:
        return ""
    for marker in _TRACEBACK_MARKERS:
        if marker in body:
            body = body.split(marker, 1)[0].rstrip()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return body[:400]
    first = lines[0]
    if first.lower().startswith("error:"):
        first = first[6:].strip()
    return first[:400]


def _is_inspect_record(result: Any) -> bool:
    """True for a successful get_task / get_subtask payload.

    Those records reuse ``status`` for the inspected object's lifecycle. Last
    week's error classes treated that as the tool failing.
    """
    if not isinstance(result, dict):
        return False
    if result.get("task_id") and result.get("task_status"):
        return True
    if result.get("subtask_id") and result.get("subtask_status"):
        return True
    return False


def _is_failure(status: str, error: str, result: Any) -> bool:
    token = str(status or "").strip().lower()
    if token in {"failed", "error", "rejected", "timed_out", "cancelled"}:
        return True
    if error:
        return True
    if _is_inspect_record(result):
        return False
    if isinstance(result, dict):
        inner = str(result.get("status") or "").strip().lower()
        if inner in {"failed", "error", "timed_out"}:
            return True
        if result.get("error"):
            return True
    return False


def _is_skill_name(name: str, result: Any) -> bool:
    if str(name or "") in _SKILL_TOOLS:
        return True
    if isinstance(result, dict):
        skill = str(result.get("skill_name") or result.get("skill") or "").strip()
        if skill:
            return True
    return False


def _skill_name(name: str, result: Any) -> str:
    if isinstance(result, dict):
        skill = str(result.get("skill_name") or result.get("skill") or "").strip()
        if skill:
            return skill
    if str(name or "") in _SKILL_TOOLS and name != "run_skill":
        return str(name)
    return ""


def _has_partial_outputs(result: Any) -> bool:
    return bool(_partial_outputs(result))


def _partial_outputs(result: Any) -> list[Any]:
    if not isinstance(result, dict):
        return []
    artifacts = result.get("artifacts") or result.get("partial_outputs")
    if isinstance(artifacts, list) and artifacts:
        return artifacts[:8]
    return []


def _looks_retryable(error: str, result: Any) -> bool:
    blob = f"{error} {result if isinstance(result, str) else ''}".lower()
    if isinstance(result, dict):
        blob = f"{blob} {result.get('error') or ''} {result.get('error_kind') or ''}".lower()
    return any(
        token in blob
        for token in ("429", "rate limit", "timeout", "timed out", "temporarily unavailable", "503")
    )


def _looks_config_unpayable(error: str, result: Any) -> bool:
    """True only for missing authority/config, not a transient VLM HTTP error."""
    blob = str(error or "").lower()
    if isinstance(result, dict):
        blob = f"{blob} {result.get('error') or ''} {result.get('code') or ''} {result.get('reason') or ''}".lower()
        if result.get("unpayable") is True:
            return True
    return any(
        token in blob
        for token in (
            "vlm_not_configured",
            "vlm_unavailable",
            "vlm is not enabled",
            "vlm configuration is incomplete",
            "not configured",
            "node_unavailable",
            "runtime_dependency_missing",
        )
    )


def _looks_unpayable(error: str, result: Any) -> bool:
    blob = str(error or "").lower()
    if isinstance(result, dict):
        blob = f"{blob} {result.get('error') or ''} {result.get('code') or ''} {result.get('reason') or ''}".lower()
        if result.get("unpayable") is True:
            return True
    if _looks_config_unpayable(error, result):
        return True
    return "unpayable" in blob


def _looks_fatal(error: str, result: Any) -> bool:
    blob = str(error or "").lower()
    if isinstance(result, dict):
        blob = f"{blob} {result.get('error') or ''} {result.get('error_code') or ''}".lower()
    return any(token in blob for token in ("sandbox escape", "storage corrupt", "database is disk"))


def _looks_invalid_args(error: str, result: Any) -> bool:
    blob = str(error or "").lower()
    if isinstance(result, dict) and result.get("field_errors"):
        return True
    return any(
        token in blob
        for token in ("required", "invalid argument", "unknown tool", "must be", "json")
    )


def _remediation_hint(skill: str, error: str, result: Any) -> str:
    blob = f"{skill} {error}"
    if isinstance(result, dict):
        blob = f"{blob} {result.get('error') or ''} {result.get('reason') or ''}"
    if _VLM_RE.search(blob):
        if _looks_retryable(error, result) or "http 503" in blob or "http 5" in blob:
            return (
                "VLM image generation hit a transient provider error; retry livefigure. "
                "Do not treat this as missing VLM configuration."
            )
        return "VLM is not configured; skip the visual step or configure a vision model."
    if _PPTX_RE.search(blob) or skill in {"research-pptx", "livefigure"}:
        return (
            "PPTX/livefigure renderer is unavailable; run `omni skills setup research-pptx` "
            "or continue the manuscript without slides."
        )
    if _DOT_RE.search(blob) or skill == "scientific-figure":
        return (
            "Write the DOT (or source script) with apply_patch/write_file first, "
            "then render that already-authored file."
        )
    if skill:
        return f"Skill {skill} failed; change arguments or use find_skill for a different producer."
    return "Change the arguments or switch tools; do not retry the identical call."


__all__ = [
    "ERROR_CLASSES",
    "FATAL_TURN",
    "INVALID_ARGS",
    "RETRYABLE_IO",
    "SKILL_FAILED_PARTIAL",
    "UNPAYABLE",
    "classify_tool_error",
    "short_skill_observation",
    "strip_traceback",
]
