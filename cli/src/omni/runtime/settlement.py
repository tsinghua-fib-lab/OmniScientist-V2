"""What the durable record says a finished turn actually achieved.

This replaces a host-side *grader*. The old verifier read an eight-field
acceptance contract off the plan and re-judged the finished turn against it:
were the declared artifacts emitted, was provenance recorded to the requested
depth, did each provider's self-assessment clear its criteria. That is the model's
job — it can see the tool results — and grading them a second time from the
outside only produced a verdict that could disagree with the answer the user had
already been shown.

What survives is bookkeeping, not judgement. Three questions have to be answered
from the record because no single execution can answer them:

* **Are the children done?** A parent turn that submitted background work is not
  finished until that work is. Settling early would publish a status the run has
  not earned yet.
* **What did the children come back with?** The parent's terminal status is the
  strongest of its own outcome and its *live* children's. A failed or partial
  sibling that a later produce already superseded — Codex: a recovered tool is
  an observation — is leftover, not a verdict, once the named outputs are on
  this task.
* **Did the side effect the turn claims actually happen?** On CLI the user sees
  the tool call; over IM they see only the prose. ``required_events`` names the
  durable trace a claim must leave — a schedule the model says it created has to
  exist as a ``schedule.resolved`` event — so an unfounded claim settles ``failed``
  instead of ``succeeded``. This question is only meaningful once the turn has
  ended: while it is still running, a required event it has not written yet has
  simply not come due.

Everything here is a fact lookup against rows that already exist. Nothing here
reads the answer text or forms an opinion about quality.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from omni.agent.capabilities import contract_outputs
from omni.channels.security import is_im_channel
from omni.core.funnel_facts import (
    RETRIEVE_TOOL_NAMES,
    has_literature_hits,
    is_empty_literature_funnel,
)
from omni.core.termination import OutcomeStatus, aggregate_outcome_status, base_termination_reason
from omni.runtime.remaining import remaining_deliverables
from omni.storage.models import SubtaskORM, TaskEventORM, TaskORM, WorkflowRunORM

_ACTIVE_STATUSES = frozenset({"scheduled", "pending", "running", "recovering"})
_LOST_STATUSES = frozenset({"failed", "cancelled", "interrupted"})

# Returned instead of a terminal status when submitted work is still in flight.
# The caller leaves the task running rather than committing a status.
PENDING = "pending"

# What a turn writes when its own loop ends: ``execution.finished`` for the loop
# itself, ``react.finished`` once post-review and artifact processing are done.
# Their absence is the difference between "the turn has not got there yet" and
# "the turn never did it", which is the whole basis of a verification verdict.
TURN_END_EVENTS = frozenset({"execution.finished", "react.finished"})


def turn_reached_its_end(events: list[TaskEventORM]) -> bool:
    """Whether the turn recorded the end of its own loop.

    Codex settles a turn only from the turn's own completion (``TaskComplete`` /
    ``TurnAborted``); a tool or child finishing is progress, never a verdict.
    This predicate is how the same rule is enforced here.
    """
    return any(event.event_type in TURN_END_EVENTS for event in events)


@dataclass(slots=True)
class Settlement:
    """The terminal status a run has earned, plus why."""

    status: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pending(self) -> bool:
        return self.status == PENDING


class SettlementStore(Protocol):
    async def get_task(self, task_id: str) -> TaskORM | None: ...

    async def list_events(self, task_id: str) -> list[TaskEventORM]: ...

    async def list_subtasks_by_ids(self, subtask_ids: list[str]) -> list[SubtaskORM]: ...

    async def list_workflows_by_ids(self, workflow_ids: list[str]) -> list[WorkflowRunORM]: ...

    async def list_subtasks_by_workflow_ids(
        self, workflow_ids: list[str]
    ) -> list[SubtaskORM]: ...

    async def list_artifacts_by_task(self, task_id: str) -> list[Any]: ...


async def settlement_for(
    store: SettlementStore,
    task_id: str,
    *,
    turn_in_flight: bool = False,
) -> Settlement:
    """Read the record and report the status ``task_id`` has earned.

    ``turn_in_flight`` is the caller saying "I am not the turn". Whether a
    required event is *missing* or merely *not written yet* cannot be read off
    the record — both look like an absent row — so the one party that knows has
    to say. A turn settling itself has finished producing evidence and leaves
    this False; a child completion or a daemon drain arriving while the loop is
    still running passes True, and gets ``PENDING`` rather than a verdict on a
    record that is still being written.
    """
    if not task_id:
        return Settlement("succeeded")
    run = await store.get_task(task_id)
    if run is None:
        return Settlement("succeeded")
    # ``needs_input`` sits with the other two because all three are decisions the
    # record has already made, not outcomes to be re-derived from the children. A
    # run pauses for input precisely *because* a step could not proceed, so that
    # step is sitting there failed; ranking it would convert every pause into the
    # failure that caused it and lose the one thing the user can act on.
    if run.status in {"cancelled", "interrupted", "needs_input"}:
        return Settlement(run.status)

    subtask_ids = [str(v) for v in (run.submitted_subtask_ids or []) if v]
    workflow_ids = [str(v) for v in (run.submitted_workflow_ids or []) if v]
    events = await store.list_events(task_id)
    direct = await store.list_subtasks_by_ids(subtask_ids)
    workflows = await store.list_workflows_by_ids(workflow_ids)
    workflow_children = await store.list_subtasks_by_workflow_ids(workflow_ids)
    children = effective_subtasks([*direct, *workflow_children])

    # A submitted id with no row is not "nothing to check": the run recorded
    # work it cannot account for, which is a lost child rather than a clean run.
    loaded = {task.id for task in direct}
    missing = [task_id for task_id in subtask_ids if task_id not in loaded]
    loaded_workflows = {workflow.id for workflow in workflows}
    missing.extend(wid for wid in workflow_ids if wid not in loaded_workflows)

    active = [task.id for task in children if task.status in _ACTIVE_STATUSES]
    active.extend(w.id for w in workflows if w.status in _ACTIVE_STATUSES)
    if not missing and active:
        # A running child may still produce the event or artifact the turn is
        # waiting on, so there is nothing to conclude yet.
        return Settlement(PENDING, {"active": active})

    unfounded = _unfounded_claims(run, events)
    if unfounded and turn_in_flight and not turn_reached_its_end(events):
        # An outsider is asking about a turn that has not reached its own end,
        # so an event the turn has not written yet is "not yet", not "claimed
        # but absent". Judging it now would read a mid-turn child completion as
        # an unfounded claim and fail a run that is still producing the very
        # record being checked.
        return Settlement(PENDING, {"in_flight": ["turn"]})
    if not missing and not unfounded and _undelivered_presentation(run, events):
        # Delivery is worth waiting for only when the turn is otherwise sound.
        # Sending the message cannot make a missing event appear, so an unfounded
        # claim is reported now rather than parked behind the transport.
        return Settlement(PENDING, {"awaiting_presentation": ["presentation"]})

    lost = [task.id for task in children if task.status in _LOST_STATUSES]
    lost.extend(w.id for w in workflows if w.status in _LOST_STATUSES)
    undelivered: list[str] = []
    if turn_reached_its_end(events):
        undelivered = remaining_deliverables(
            _required_outputs(run),
            await _artifacts_for(store, task_id),
        )

    required_contract = contract_outputs(_required_outputs(run))
    leftover_delivered: set[str] = set()
    if turn_reached_its_end(events):
        leftover_delivered = _superseded_when_delivered(
            children,
            workflows,
            missing=missing,
            unfounded=unfounded,
            required_contract=required_contract,
            undelivered=undelivered,
        )
    leftover_lost = [item for item in lost if item in leftover_delivered]
    leftover = leftover_delivered | _superseded_empty_funnel(
        children,
        events,
        lost=[item for item in lost if item not in leftover_delivered],
        missing=missing,
        unfounded=unfounded,
        required_contract=required_contract,
        undelivered=undelivered,
    )
    lost = [item for item in lost if item not in leftover]
    degraded = [
        task.id
        for task in children
        if task.status == "degraded" and task.id not in leftover
    ]
    degraded.extend(
        w.id for w in workflows if w.status == "degraded" and w.id not in leftover
    )

    execution = _execution_outcome(events)
    # Codex does not settle a turn degraded because the model retried a tool
    # after the work existed. When every named scientific output is on the
    # record, a ``no_progress`` stop is leftover churn, not a missing paper.
    # An empty-funnel ideation sibling is the same leftover once a later
    # retrieve kept papers (or the named outputs are already on this task).
    if (
        required_contract
        and not undelivered
        and not missing
        and not lost
        and not unfounded
        and not degraded
        and _execution_is_progress_stall(events)
    ):
        execution = "succeeded"

    status = aggregate_outcome_status(
        execution,
        "failed" if (missing or lost or unfounded) else "succeeded",
        "degraded" if (degraded or undelivered or _failed_presentation(run, events)) else "succeeded",
    )
    detail: dict[str, Any] = {}
    for key, values in (
        ("missing", missing),
        ("lost", lost),
        ("degraded", degraded),
        ("unfounded_claims", unfounded),
        ("undelivered_outputs", undelivered),
        ("superseded_failures", leftover_lost),
    ):
        if values:
            detail[key] = values
    return Settlement(status, detail)


def effective_subtasks(tasks: list[SubtaskORM]) -> list[SubtaskORM]:
    """Drop attempts a later retry superseded, so only the live one counts."""
    superseded = {str(task.retry_of) for task in tasks if task.retry_of}
    return [task for task in tasks if task.id not in superseded]


def _superseded_when_delivered(
    children: list[SubtaskORM],
    workflows: list[WorkflowRunORM],
    *,
    missing: list[str],
    unfounded: list[str],
    required_contract: list[str],
    undelivered: list[str],
) -> set[str]:
    """Children whose failure or partial is leftover once this task has the files.

    Codex treats a recovered tool as an observation. When every named scientific
    output is already on this ``task_id``, a failed livefigure or a topology
    ``partial`` figure does not get to fail or yellow the parent.
    """
    if missing or unfounded or not required_contract or undelivered:
        return set()
    leftover = {
        task.id
        for task in children
        if task.status in _LOST_STATUSES or task.status == "degraded"
    }
    leftover.update(
        workflow.id
        for workflow in workflows
        if workflow.status in _LOST_STATUSES or workflow.status == "degraded"
    )
    return leftover


def _superseded_empty_funnel(
    children: list[SubtaskORM],
    events: list[TaskEventORM],
    *,
    lost: list[str],
    missing: list[str],
    unfounded: list[str],
    required_contract: list[str],
    undelivered: list[str],
) -> set[str]:
    """Empty-funnel degraded children that later produce already superseded.

    Failed / lost / unfounded children still win. A figure ``partial`` is not
    an empty funnel and stays on the parent. Answer-only recovery counts when
    ``react.finished`` named a retrieve tool or a sibling kept papers.
    """
    if lost or missing or unfounded:
        return set()
    empty = {
        task.id
        for task in children
        if task.status == "degraded" and is_empty_literature_funnel(task.result_json)
    }
    if not empty:
        return set()
    later_hits = any(
        task.id not in empty
        and task.status in {"succeeded", "degraded"}
        and has_literature_hits(task.result_json)
        for task in children
    )
    retrieved = bool(RETRIEVE_TOOL_NAMES.intersection(_react_tool_names(events)))
    outputs_delivered = bool(required_contract) and not undelivered
    if later_hits or retrieved or outputs_delivered:
        return empty
    return set()


def _react_tool_names(events: list[TaskEventORM]) -> set[str]:
    names: set[str] = set()
    for event in events:
        if event.event_type != "react.finished":
            continue
        payload = event.output_json or {}
        raw = payload.get("tool_names")
        if isinstance(raw, list):
            names.update(str(item) for item in raw if item)
    return names


def _required_outputs(run: TaskORM) -> list[str]:
    plan = run.plan_json if isinstance(run.plan_json, Mapping) else {}
    verification = plan.get("verification_plan")
    required = (
        [str(v) for v in (verification.get("required_outputs") or []) if v]
        if isinstance(verification, Mapping)
        else []
    )
    if required:
        return required
    outputs = plan.get("outputs")
    if isinstance(outputs, list):
        return [str(v) for v in outputs if v]
    return []


async def _artifacts_for(store: SettlementStore, task_id: str) -> list[Any]:
    list_fn = getattr(store, "list_artifacts_by_task", None)
    if list_fn is None:
        return []
    return list(await list_fn(task_id))


def _unfounded_claims(run: TaskORM, events: list[TaskEventORM]) -> list[str]:
    """Required events the plan declared that the record never recorded."""
    plan = run.plan_json if isinstance(run.plan_json, Mapping) else {}
    verification = plan.get("verification_plan")
    required = (
        [str(v) for v in (verification.get("required_events") or []) if v]
        if isinstance(verification, Mapping)
        else []
    )
    if not required:
        return []
    seen = {event.event_type for event in events}
    return [name for name in required if name not in seen]


def _undelivered_presentation(run: TaskORM, events: list[TaskEventORM]) -> bool:
    """Only a run that hands off to a remote transport can be undelivered.

    CLI and REPL write to stdout inside the turn and record no send. Internal
    runs (``maintenance`` and friends) address no one at all — waiting on a
    delivery they will never make would leave them ``running`` forever in
    ``omni task list``, with their model spend attributed to nothing. So this
    asks the narrow question — is this an IM channel that still owes a send —
    rather than treating every unfamiliar channel as outbound.

    A failed send is *not* pending: it is terminal, and falls through to the
    normal aggregation as a lost delivery.
    """
    channel = str(getattr(run, "channel", "") or "cli").lower()
    if not is_im_channel(channel):
        return False
    final = [
        event
        for event in events
        if event.event_type in {"presentation.sent", "presentation.degraded", "presentation.failed"}
        and str((event.output_json or {}).get("kind") or "turn") != "ack"
    ]
    return not final


def _failed_presentation(run: TaskORM, events: list[TaskEventORM]) -> bool:
    """A finished IM send that failed is a degraded hop, not a pending one."""
    channel = str(getattr(run, "channel", "") or "cli").lower()
    if not is_im_channel(channel):
        return False
    return any(
        event.event_type == "presentation.failed"
        and str((event.output_json or {}).get("kind") or "turn") != "ack"
        for event in events
    )


def _execution_is_progress_stall(events: list[TaskEventORM]) -> bool:
    """Whether the loop's only non-success is a ``no_progress`` bounded stop."""
    saw_stall = False
    for event_type in ("execution.finished", "react.finished"):
        event = next(
            (item for item in reversed(events) if item.event_type == event_type), None
        )
        if event is None:
            continue
        payload = event.output_json or {}
        reason = base_termination_reason(str(payload.get("terminated_reason") or ""))
        kind = str(payload.get("kind") or "").lower()
        if event.status == "failed" or kind == "error":
            return False
        if event.status == "degraded" or kind == "partial" or reason == "no_progress":
            if reason != "no_progress":
                return False
            saw_stall = True
    return saw_stall


def _execution_outcome(events: list[TaskEventORM]) -> OutcomeStatus:
    """Combine the loop's own stop with post-review, letting neither erase the other.

    ``execution.finished`` records how the ReAct loop ended; ``react.finished``
    records what post-review and artifact processing made of it. Reading only the
    latest of each type lets a recovery cycle supersede an earlier attempt without
    the run staying poisoned by it.
    """
    outcomes: list[str] = []
    for event_type in ("execution.finished", "react.finished"):
        event = next(
            (item for item in reversed(events) if item.event_type == event_type), None
        )
        if event is None:
            continue
        if event.status in {"succeeded", "degraded", "failed"}:
            outcomes.append(event.status)
        payload = event.output_json or {}
        if payload.get("kind") or payload.get("terminated_reason"):
            from omni.core.termination import execution_outcome_status

            outcomes.append(
                execution_outcome_status(
                    str(payload.get("kind") or ""),
                    str(payload.get("terminated_reason") or ""),
                )
            )
    return aggregate_outcome_status(*outcomes) if outcomes else "succeeded"


__all__ = [
    "PENDING",
    "TURN_END_EVENTS",
    "Settlement",
    "SettlementStore",
    "effective_subtasks",
    "settlement_for",
    "turn_reached_its_end",
]
