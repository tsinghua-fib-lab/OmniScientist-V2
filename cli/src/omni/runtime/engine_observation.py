"""Versioned envelope a skill/engine observation must expose to the coordinator.

The engine may report that *it* succeeded. It must not declare the parent task
complete. Extra fields are ignored so older engines stay compatible.
"""

from __future__ import annotations

from typing import Any

from omni.core.funnel_facts import is_empty_literature_funnel, literature_funnel_facts

SCHEMA = "omni.engine.observation/v1"

_REF_KEYS = (
    ("source", "source_ids"),
    ("claim", "claim_ids"),
    ("evidence", "evidence_ids"),
    ("artifact", "artifact_ids"),
    ("run", "run_ids"),
)
_SINGULAR = (
    ("source", "source_id"),
    ("claim", "claim_id"),
    ("evidence", "evidence_id"),
    ("artifact", "artifact_id"),
    ("run", "run_id"),
)
_PRODUCE_MARKERS = (
    "artifact",
    "figure",
    "pptx",
    "slides",
    "poster",
    "manuscript",
    "draft",
    "report",
)
_RETRIEVE_MARKERS = (
    "search",
    "literature",
    "arxiv",
    "openalex",
    "pubmed",
    "corpus",
    "fetch",
)
_VERIFY_MARKERS = ("verify", "contradiction", "review_statistics")
_COMPUTE_MARKERS = ("compute", "log_run", "experiment")


def collect_typed_refs(payload: Any) -> list[str]:
    """Collect ``kind:id`` refs the host already merges onto the task row."""
    found: list[str] = []
    seen: set[str] = set()

    def add(kind: str, value: Any) -> None:
        item = str(value or "").strip()
        if not item:
            return
        ref = f"{kind}:{item}"
        if ref not in seen:
            seen.add(ref)
            found.append(ref)

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(obj, list):
            for item in obj[:32]:
                walk(item, depth + 1)
            return
        if not isinstance(obj, dict):
            return
        for kind, key in _REF_KEYS:
            raw = obj.get(key)
            if isinstance(raw, list):
                for item in raw:
                    add(kind, item)
            elif raw:
                add(kind, raw)
        for kind, key in _SINGULAR:
            if obj.get(key):
                add(kind, obj.get(key))
        for ref_key in ("created_refs", "updated_refs"):
            raw_refs = obj.get(ref_key)
            if isinstance(raw_refs, list):
                for ref in raw_refs[:32]:
                    if isinstance(ref, str) and ":" in ref:
                        kind, _, item = ref.partition(":")
                        add(kind, item)
        for nested_key in (
            "result",
            "results",
            "payload",
            "research",
            "observation",
            "sources",
            "matches",
            "nodes",
        ):
            nested = obj.get(nested_key)
            if isinstance(nested, (dict, list)):
                walk(nested, depth + 1)

    walk(payload)
    return found


def infer_observation_role(payload: Any, *, skill_name: str = "") -> str:
    """Best-effort role from the result shape — never a dispatcher stage."""
    name = str(skill_name or "").lower()
    blob = f"{name} { _shape_blob(payload)}"
    if any(marker in blob for marker in _VERIFY_MARKERS):
        return "verify"
    if any(marker in blob for marker in _COMPUTE_MARKERS):
        return "compute"
    if "revis" in blob:
        return "revise"
    if literature_funnel_facts(payload) is not None or any(
        marker in blob for marker in _RETRIEVE_MARKERS
    ):
        return "retrieve"
    if any(marker in blob for marker in _PRODUCE_MARKERS):
        return "produce"
    return "other"


def build_engine_observation(
    result: Any,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a skill/tool payload into the v1 envelope."""
    wrapper = dict(extra or {})
    merged: dict[str, Any] = {}
    if isinstance(result, dict):
        merged.update(result)
    merged.update(wrapper)
    refs = collect_typed_refs(merged)
    facts = literature_funnel_facts(result)
    empty = is_empty_literature_funnel(result)
    status = str(wrapper.get("status") or (result.get("status") if isinstance(result, dict) else "") or "")
    if empty and status.lower() not in {
        "failed",
        "cancelled",
        "interrupted",
        "blocked",
        "rejected",
        "error",
        "needs_input",
    }:
        status = "degraded"
    elif not status:
        status = "succeeded"
    limitations: list[str] = []
    warning = ""
    if isinstance(result, dict):
        warning = str(result.get("warning") or "").strip()
    if facts and facts.get("warning"):
        warning = str(facts["warning"])
    if warning:
        limitations.append(warning)
    if empty:
        limitations.append("literature funnel kept 0 sources")
    assessment = _assessment(result)
    if assessment:
        limitations.append(assessment)
    metrics: dict[str, Any] = {}
    if facts:
        metrics = {
            "queries": facts.get("queries") or [],
            "n_retrieved": facts.get("n_retrieved"),
            "n_kept": facts.get("n_kept"),
        }
    actions: list[str] = []
    if empty:
        actions.append("retry retrieval with a different query")
    summary = str(
        wrapper.get("summary")
        or (result.get("summary") if isinstance(result, dict) else "")
        or ""
    ).strip()
    if not summary and facts:
        summary = f"literature n_kept={facts.get('n_kept', 0)}"
    if "complete" in summary.lower() and "task" in summary.lower():
        summary = summary.replace("task", "step")
    return {
        "schema": SCHEMA,
        "status": status,
        "role": infer_observation_role(result, skill_name=str(wrapper.get("skill_name") or "")),
        "summary": summary[:240],
        "created_refs": refs,
        "updated_refs": [],
        "metrics": metrics,
        "limitations": limitations[:6],
        "recommended_next_actions": actions[:2],
        "raw_event_ref": str(wrapper.get("object_id") or wrapper.get("subtask_id") or ""),
    }


def attach_engine_observation(
    payload: dict[str, Any],
    result: Any,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Put the envelope on the wrapper and keep mergeable ``*_ids`` lists."""
    observation = build_engine_observation(result, extra=extra)
    payload["observation"] = observation
    for kind in ("source", "claim", "evidence", "artifact", "run"):
        key = f"{kind}_ids"
        ids = [
            ref.split(":", 1)[1]
            for ref in observation["created_refs"]
            if ref.startswith(f"{kind}:")
        ]
        if ids and not payload.get(key):
            payload[key] = ids
    return payload


def _assessment(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    raw = result.get("deliverable_assessment")
    if not isinstance(raw, dict):
        return ""
    note = str(raw.get("summary") or raw.get("note") or raw.get("status") or "").strip()
    return note[:160]


def _shape_blob(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    keys = " ".join(str(key).lower() for key in list(payload)[:24])
    skill = str(payload.get("skill_name") or payload.get("planned_skill_name") or "")
    return f"{keys} {skill}".lower()


__all__ = [
    "SCHEMA",
    "attach_engine_observation",
    "build_engine_observation",
    "collect_typed_refs",
    "infer_observation_role",
]
