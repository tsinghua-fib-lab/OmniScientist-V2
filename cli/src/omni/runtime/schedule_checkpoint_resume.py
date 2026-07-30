"""Deterministic resume of schedule Action checkpoints.

Shared by the conversational ``resolve_action_checkpoint`` tool and
``omni task resume <task-id> --input <choice>`` so both paths honour the same
CAS, decider, TTL, and materialisation rules.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from omni.core.timefmt import local_time_context
from omni.runtime.action_checkpoints import (
    ActionCheckpointStore,
    AmbiguousCheckpointId,
    CheckpointRecord,
)
from omni.scheduling.action import canonical_schedule_trigger
from omni.scheduling.contracts import ScheduleActor, ScheduleCreateRequest
from omni.scheduling.service import ScheduleService

_PERIOD_ALIASES = {
    "pm": "pm",
    "evening": "pm",
    "afternoon": "pm",
    "night": "pm",
    "am": "am",
    "morning": "am",
}


def map_choice_to_candidate(choice: str, record: CheckpointRecord) -> str:
    """Map a user/CLI choice onto a candidate id, or "" when unknown."""
    raw = (choice or "").strip()
    low = raw.lower()
    if record.candidate(raw) is not None:
        return raw
    for prefix in ("pick:", "repair_next_day:"):
        if raw.startswith(prefix):
            return raw.split(":", 1)[1]
    mapped = _PERIOD_ALIASES.get(low)
    return mapped if mapped and record.candidate(mapped) is not None else ""


def next_day_trigger(value: dict[str, Any]) -> dict[str, Any]:
    """Same wall-clock, next day — the repair offered for an elapsed candidate."""
    at = str((value or {}).get("at", "")).strip()
    try:
        nxt = (datetime.fromisoformat(at) + timedelta(days=1)).isoformat(timespec="seconds")
    except ValueError:
        nxt = at
    return {"kind": "once", "at": nxt, "timezone": str((value or {}).get("timezone", ""))}


async def find_open_checkpoint_for_task(
    store: ActionCheckpointStore,
    *,
    task_id: str,
    task_recorder: Any | None = None,
) -> CheckpointRecord | None:
    """Locate the open clarification for a suspended task.

    Prefers the ``task_id`` column. When absent (pre-migration rows), falls back
    to the newest ``action.checkpoint.created`` event on the task and binds the
    checkpoint for subsequent lookups.
    """
    if not task_id:
        return None
    direct = await store.get_open_for_task(task_id)
    if direct is not None:
        return direct
    if task_recorder is None:
        return None
    events = await task_recorder.list_events(task_id)
    checkpoint_id = ""
    for event in reversed(list(events or [])):
        if str(getattr(event, "event_type", "") or "") != "action.checkpoint.created":
            continue
        payload = getattr(event, "output_json", None) or {}
        if isinstance(payload, dict):
            checkpoint_id = str(payload.get("checkpoint_id") or "").strip()
            if checkpoint_id:
                break
    if not checkpoint_id:
        return None
    try:
        record = await store.get(checkpoint_id)
    except AmbiguousCheckpointId:
        return None
    if record is None or record.state != "open":
        return None
    # Best-effort bind so future lookups skip the event scan.
    if not record.task_id:
        async with store._db.session() as session:  # noqa: SLF001
            from omni.storage.models import ActionCheckpointORM

            row = await session.get(ActionCheckpointORM, record.id)
            if row is not None and not str(row.task_id or ""):
                row.task_id = task_id
                await session.commit()
                await session.refresh(row)
                return CheckpointRecord.of(row)
    return record


async def resolve_schedule_checkpoint(
    *,
    store: ActionCheckpointStore,
    service: ScheduleService,
    checkpoint_id: str,
    choice: str,
    decider: str,
    reference_time: datetime | None = None,
    emit_action: Any | None = None,
    record_outcome: Any | None = None,
) -> dict[str, Any]:
    """Resolve one schedule clarification and materialise the schedule.

    Returns a tool-shaped payload with ``status`` in
    ``ok | needs_input | error``.
    """
    try:
        record = await store.get(checkpoint_id)
    except AmbiguousCheckpointId as exc:
        return {"status": "needs_input", "message": str(exc), "error": str(exc)}
    if record is None:
        return {
            "status": "error",
            "error": f"No clarification draft matches '{checkpoint_id}'.",
        }
    if record.required_decider and decider != record.required_decider:
        return {
            "status": "error",
            "error": "Only the original requester can answer this clarification.",
        }

    low = (choice or "").strip().lower()
    if low in {"cancel"}:
        await store.cancel(checkpoint_id, decider=decider)
        return {
            "status": "ok",
            "outcome": "cancelled",
            "summary": "Cancelled the pending schedule clarification.",
        }
    if low in {"run_now", "now"}:
        await store.cancel(checkpoint_id, decider=decider)
        return {
            "status": "needs_input",
            "message": "Not scheduling anything this time; tell me the goal to run now instead.",
            "error": "Not scheduling anything this time; tell me the goal to run now instead.",
            "outcome": "run_now",
        }
    if low in {"other_time", "reschedule", "different_time"}:
        # "None of these" — the user wants a time not among the offered readings.
        # Ask for the concrete time; the next turn should call
        # ``resolve_action_checkpoint`` with ``when``/``at`` so the draft goal
        # is kept (not a fresh ``schedule_task`` that re-plans the work).
        message = (
            "Sure — tell me the day and time you want (for example 'tomorrow 9am' "
            "or 'Aug 5 3pm') and I'll reschedule."
        )
        return {"status": "needs_input", "message": message, "error": message, "outcome": "other_time"}

    is_repair = (choice or "").startswith("repair_next_day:")
    candidate_id = map_choice_to_candidate(choice, record)
    if not candidate_id:
        # Not a listed reading and not a keyword: treat it as "none of these"
        # rather than a dead end — invite a concrete time (the model reschedules
        # via schedule_task) or a listed pick / cancel.
        message = (
            "That is not one of the offered readings. Tell me a concrete time "
            "(for example 'tomorrow 9am') and I'll reschedule, pick one of "
            f"{record.candidate_ids}, or reply cancel."
        )
        return {"status": "needs_input", "message": message, "error": message}
    candidate = record.candidate(candidate_id)
    if candidate is None:
        return {"status": "error", "error": f"Unknown candidate '{candidate_id}'."}

    async def _create(trigger_value: dict[str, Any]) -> dict[str, Any]:
        payload = record.payload or {}
        now = reference_time or local_time_context().now
        request = ScheduleCreateRequest(
            trigger=canonical_schedule_trigger(trigger_value),
            goal=str(payload.get("goal") or ""),
            title=str(payload.get("title") or ""),
            actor=ScheduleActor(
                channel=record.channel or "cli",
                session_id=record.session_id or "",
                principal=record.actor_principal or decider,
            ),
            reference_time=now,
            already_clarified=True,
        )
        result = await service.create(request)
        out = result.tool_result()
        if emit_action is not None:
            await emit_action(
                "action.admitted" if result.status == "created" else "action.rejected",
                {
                    "schedule_id": getattr(result, "schedule_id", "") or "",
                    "trigger": trigger_value,
                    "outcome": result.status,
                    "via": "checkpoint",
                },
                status=result.status,
            )
        if record_outcome is not None:
            await record_outcome(result.status, out)
        return out

    if is_repair:
        await store.cancel(checkpoint_id, decider=decider)
        return await _create(next_day_trigger(candidate["value"]))

    if candidate.get("validity") == "past":
        label = candidate.get("label", candidate_id)
        return {
            "status": "needs_input",
            "resolution_status": "past",
            "message": f"The reading you picked ('{label}') has already passed.",
            "recovery_choices": [
                {"id": f"repair_next_day:{candidate_id}", "label": "same time tomorrow"},
                {"id": "run_now", "label": "run it now"},
                {"id": "cancel", "label": "cancel"},
            ],
            "draft_id": record.id[:8],
        }

    outcome = await store.resolve(checkpoint_id, candidate_id=candidate_id, decider=decider)
    if outcome.status == "forbidden":
        return {
            "status": "error",
            "error": "Only the original requester can answer this clarification.",
        }
    if outcome.status == "expired":
        if emit_action is not None:
            await emit_action(
                "action.checkpoint.expired",
                {"checkpoint_id": record.id},
                status="expired",
            )
        message = "This clarification has expired; please set the schedule again."
        return {"status": "needs_input", "message": message, "error": message}
    if outcome.status == "conflict":
        return {
            "status": "error",
            "error": f"This clarification was already answered differently ({outcome.reason}).",
        }
    if outcome.status == "replayed":
        fresh = await store.get(checkpoint_id)
        if fresh is not None and fresh.result_id:
            return {
                "status": "ok",
                "kind": "once",
                "schedule_id": fresh.result_id,
                "already_created": True,
                "summary": "This schedule was already created.",
            }
        return {
            "status": "ok",
            "pending": True,
            "summary": "This choice is being created into a schedule.",
        }
    if not outcome.ok:
        return {
            "status": "error",
            "error": f"Could not confirm this choice ({outcome.status}).",
        }
    if emit_action is not None:
        await emit_action(
            "action.checkpoint.resolved",
            {
                "checkpoint_id": record.id,
                "candidate_id": candidate_id,
                "phase": "semantic_clarification",
            },
            status="resolved",
        )
    result = await _create((outcome.candidate or candidate)["value"])
    if result.get("schedule_id"):
        await store.attach_result(
            checkpoint_id, result_kind="schedule", result_id=result["schedule_id"]
        )
    return result


__all__ = [
    "find_open_checkpoint_for_task",
    "map_choice_to_candidate",
    "next_day_trigger",
    "resolve_schedule_checkpoint",
]
