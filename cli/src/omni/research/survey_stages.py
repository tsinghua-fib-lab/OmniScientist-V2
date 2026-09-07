"""STORM-like survey stage events: retrieve → evidence → outline → write.

Stages are informational. A missing outline must not flip settlement or paint
Partial success.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

SURVEY_STAGES_EVENT = "research.stages"
SURVEY_STAGES = ("retrieve", "evidence", "outline", "write")

_RETRIEVE_TOOLS = frozenset(
    {
        "search_literature",
        "search_corpus",
        "literature.search",
        "openalex-search",
        "arxiv-fetch",
        "paper.fetch.arxiv",
    }
)
_EVIDENCE_TOOLS = frozenset({"cite_source", "add_evidence", "cite"})
_OUTLINE_TOOLS = frozenset({"update_plan"})
_WRITE_TOOLS = frozenset({"write_file", "apply_patch"})


def _tool_name(record: Any) -> str:
    if isinstance(record, dict):
        return str(
            record.get("name")
            or record.get("tool")
            or record.get("event_type")
            or ""
        ).strip()
    return str(getattr(record, "name", "") or getattr(record, "tool", "") or "").strip()


def detect_survey_stages(
    *,
    tool_trace: Sequence[Any] = (),
    drained: Sequence[Any] = (),
    draft: str = "",
) -> dict[str, Any]:
    names = {_tool_name(item) for item in (*tool_trace, *drained) if _tool_name(item)}
    body = str(draft or "")
    present: list[str] = []
    if names & _RETRIEVE_TOOLS:
        present.append("retrieve")
    if names & _EVIDENCE_TOOLS or "[S" in body:
        present.append("evidence")
    if names & _OUTLINE_TOOLS or _has_outline_headings(body):
        present.append("outline")
    if names & _WRITE_TOOLS or _looks_written(body):
        present.append("write")
    missing = [stage for stage in SURVEY_STAGES if stage not in present]
    return {
        "present": present,
        "missing": missing,
        "complete": not missing,
    }


def format_survey_stages(payload: dict[str, Any] | None) -> list[str]:
    data = payload if isinstance(payload, dict) else {}
    present = [str(item) for item in (data.get("present") or []) if str(item).strip()]
    if not present and not data.get("missing"):
        return []
    complete = bool(data.get("complete"))
    marks = " → ".join(
        ("✓ " if stage in present else "· ") + stage for stage in SURVEY_STAGES
    )
    head = "Survey stages complete: " if complete else "Survey stages still open: "
    return [head + marks]


def _has_outline_headings(draft: str) -> bool:
    return any(line.startswith("#") for line in str(draft or "").splitlines())


def _looks_written(draft: str) -> bool:
    body = str(draft or "").strip()
    return len(body) >= 80 and ("\n" in body or "[S" in body)


__all__ = [
    "SURVEY_STAGES",
    "SURVEY_STAGES_EVENT",
    "detect_survey_stages",
    "format_survey_stages",
]
