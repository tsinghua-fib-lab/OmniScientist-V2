"""Host-owned projection of tool results into the model-facing transcript.

A python-engine skill (research-ideation, research-pptx, …) returns a structured
result that is persisted in full on the task event. Re-sending that blob on every
later ReAct iteration is what turned a small research turn into a multi-million
token bill: the coordinator's prompt_tokens include the previous observation
each time. Codex keeps tool output in the transcript but shrinks it; Omni does
the same at ingest so the latest observation is already bounded, not only the
older ones that microcompact later trims.
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_OBSERVATION_MAX_CHARS = 8000
_LIST_KEEP = 8
_STR_KEEP = 480
_DICT_KEEP = 28
_PLACEHOLDER = "… [observation truncated]"

# Keys a coordinator actually needs to continue after a skill returns. Everything
# else (full paper lists, slide bodies, raw abstracts) stays on the event.
_PREFERRED_KEYS = (
    "status",
    "skill_name",
    "mode",
    "summary",
    "message",
    "error",
    "warning",
    "queries",
    "query",
    "n_retrieved",
    "n_kept",
    "paper_count",
    "count",
    "report_uri",
    "artifact_uri",
    "artifacts",
    "outcome",
    "usage",
    "partial_outputs",
    "result",
    "planned_skill_name",
    "capability_resolution",
    "subtask_id",
    "task_id",
    "object_kind",
    "object_id",
)


def compact_observation(value: Any, *, max_chars: int = DEFAULT_OBSERVATION_MAX_CHARS) -> str:
    """Return a model-facing string no larger than ``max_chars``.

    ``max_chars <= 0`` disables the cap and dumps the value as JSON (or as-is
    for strings), matching the historic unbounded observation.
    """
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(_project(value), ensure_ascii=False, default=str)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    room = max(1, max_chars - len(_PLACEHOLDER) - 1)
    return text[:room].rstrip() + "\n" + _PLACEHOLDER


def _project(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) <= _STR_KEEP:
            return value
        return value[:_STR_KEEP].rstrip() + "…"
    if isinstance(value, list):
        items = [_project(item, depth=depth + 1) for item in value[:_LIST_KEEP]]
        omitted = len(value) - _LIST_KEEP
        if omitted > 0:
            items.append(f"… {omitted} more")
        return items
    if isinstance(value, dict):
        keys = _ordered_keys(value)
        return {key: _project(value[key], depth=depth + 1) for key in keys}
    return value


def _ordered_keys(payload: dict[str, Any]) -> list[str]:
    preferred = [key for key in _PREFERRED_KEYS if key in payload]
    extras = [key for key in payload if key not in preferred]
    ordered = preferred + extras
    if len(ordered) <= _DICT_KEEP:
        return ordered
    return ordered[:_DICT_KEEP]


__all__ = ["DEFAULT_OBSERVATION_MAX_CHARS", "compact_observation"]
