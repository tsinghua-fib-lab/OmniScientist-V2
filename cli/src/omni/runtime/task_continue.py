"""Bind a continue/resume utterance to the task that already holds the ledger.

Natural-language continue is not a new planner request. It reopens the last
cancelled/failed research task (or an explicit id) so ReAct sees that row's
ROM and debts. Retry forks a new attempt and copies the same ledger onto it.
"""

from __future__ import annotations

import re
from typing import Any

from omni.agent.capabilities import contract_outputs
from omni.agent.intent_plan import IntentPlan, VerificationPlan
from omni.agent.plan_factory import build_assistant_plan
from omni.runtime.taskref import extract_task_ids

CONTINUABLE_STATUSES = frozenset({"cancelled", "interrupted", "failed", "degraded"})

# CJK spellings as unicode escapes so the control-plane source stays English
# (same pattern as ``runtime.remaining``). Runtime values are the ideographs
# users type.
_STRONG = (
    "\u7ee7\u7eed\u4e0a\u6b21",
    "\u63a5\u7740\u4e0a\u6b21",
    "\u63a5\u7740\u5199",
    "\u7ee7\u7eed\u5199",
    "\u8865\u4e0a\u56fe",
    "\u8865\u56fe",
    "\u8865\u4e0a\u7a3f",
    "\u7ee7\u7eed\u8fd9\u4e2a\u4efb\u52a1",
    "\u4ece\u4e0a\u6b21",
    "\u6309\u5ba1\u7a3f",
    "continue last",
    "resume last",
    "pick up where",
    "finish the figure",
    "add the figure",
    "continue the survey",
    "continue the paper",
    "continue this task",
    "keep going",
)
_BARE = re.compile(
    r"^(?:"
    + "\u7ee7\u7eed|\u63a5\u7740\u505a"
    + r"|continue|resume|keep going)[.!?。？]*$",
    re.IGNORECASE,
)


def is_continue_request(text: str) -> bool:
    """True for control-plane continue, not a new scientific request."""
    raw = str(text or "").strip()
    if not raw:
        return False
    folded = raw.casefold()
    if any(hint in raw or hint in folded for hint in _STRONG):
        return True
    return bool(_BARE.match(raw))


def task_has_research_work(task: Any) -> bool:
    """Whether this row already has a research contract or ledger to resume."""
    if any(
        getattr(task, key, None)
        for key in ("source_ids", "claim_ids", "evidence_ids", "artifact_ids")
    ):
        return True
    payload = getattr(task, "plan_json", None)
    if not isinstance(payload, dict):
        return False
    verification = payload.get("verification_plan") if isinstance(payload.get("verification_plan"), dict) else {}
    names = list(verification.get("required_outputs") or payload.get("outputs") or [])
    return bool(contract_outputs(names))


def continue_from_persisted_plan(
    user_message: str, task_id: str, persisted: Any
) -> IntentPlan | None:
    """React floor that keeps the accepted contract and does not re-route skills."""
    payload = getattr(persisted, "plan_json", None)
    if not isinstance(payload, dict) or not payload:
        return None
    plan = build_assistant_plan(
        user_message,
        task_id=task_id,
        rationale="continue this task from its existing contract and ledger",
        confidence=0.9,
    )
    verification = (
        payload.get("verification_plan")
        if isinstance(payload.get("verification_plan"), dict)
        else {}
    )
    required = [str(item) for item in (verification.get("required_outputs") or payload.get("outputs") or []) if item]
    events = [str(item) for item in (verification.get("required_events") or []) if item]
    contract = contract_outputs(required)
    if contract:
        plan.outputs = list(dict.fromkeys([*plan.outputs, *contract]))
        plan.verification_plan = VerificationPlan(
            required_outputs=list(
                dict.fromkeys([*plan.verification_plan.required_outputs, *contract])
            ),
            required_events=events or list(plan.verification_plan.required_events),
        )
    mode = str(getattr(persisted, "provenance_mode", "") or payload.get("provenance_mode") or "")
    if mode in {"light", "full"}:
        plan.provenance_mode = mode
    plan.tool_policy.require_opening_tool = False
    return plan


async def resolve_continue_task(
    tasks: Any,
    *,
    user_message: str,
    session_id: str = "",
) -> str:
    """Return the task_id a continue utterance should reopen, or empty."""
    if not is_continue_request(user_message) or tasks is None:
        return ""
    for raw in extract_task_ids(user_message):
        try:
            row = await tasks.get_task(raw)
        except Exception:  # noqa: BLE001
            row = None
        if row is not None and _can_reopen(row):
            return str(row.id)
    if not session_id:
        return ""
    try:
        rows = await tasks.list_tasks_for_session(session_id)
    except Exception:  # noqa: BLE001
        return ""
    turns = [
        row
        for row in rows
        if str(getattr(row, "kind", "") or "turn") == "turn"
        and getattr(row, "archived_at", None) is None
    ]
    turns.sort(key=lambda row: getattr(row, "created_at", None) or 0, reverse=True)
    for row in turns:
        if _can_reopen(row):
            return str(row.id)
    return ""


def _can_reopen(task: Any) -> bool:
    status = str(getattr(task, "status", "") or "")
    return status in CONTINUABLE_STATUSES and task_has_research_work(task)


def copy_id_list(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


__all__ = [
    "CONTINUABLE_STATUSES",
    "continue_from_persisted_plan",
    "copy_id_list",
    "is_continue_request",
    "resolve_continue_task",
    "task_has_research_work",
]
