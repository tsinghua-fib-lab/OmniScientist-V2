"""Verification runner for IntentPlan acceptance contracts.

TaskRecorder stores runs and events.  VerificationRunner owns the harness
operation of checking whether a persisted run satisfied its VerificationPlan.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from omni.core.termination import (
    aggregate_outcome_status,
    execution_outcome_status,
    is_bounded_termination,
)
from omni.runtime.deliverable_assessment import (
    collect_deliverable_assessments,
    evaluate_deliverable_assessments,
)
from omni.storage.models import (
    SubtaskORM,
    TaskEventORM,
    TaskORM,
    WorkflowRunORM,
    WorkflowStepORM,
)

_ACTIVE_SUBTASK_STATUSES = {"scheduled", "pending", "running", "recovering"}
_TERMINAL_SUBTASK_STATUSES = {"succeeded", "degraded", "failed", "cancelled", "interrupted"}


class _VerificationStore(Protocol):
    async def get_task(self, task_id: str) -> TaskORM | None: ...

    async def list_events(self, task_id: str) -> list[TaskEventORM]: ...

    async def list_subtasks_by_ids(self, subtask_ids: list[str]) -> list[SubtaskORM]: ...

    async def list_workflows_by_ids(self, workflow_ids: list[str]) -> list[WorkflowRunORM]: ...

    async def list_subtasks_by_workflow_ids(self, workflow_ids: list[str]) -> list[SubtaskORM]: ...

    async def list_workflow_steps(self, workflow_run_id: str) -> list[WorkflowStepORM]: ...

    async def append_event(self, task_id: str, **kwargs: Any) -> TaskEventORM | None: ...


@dataclass(slots=True)
class VerificationRunner:
    store: _VerificationStore

    async def verify(self, task_id: str) -> str:
        if not task_id:
            return "not_applicable"
        run = await self.store.get_task(task_id)
        if run is None:
            return "not_applicable"
        if run.status in {"cancelled", "interrupted"}:
            await self.store.append_event(
                task_id,
                event_type="verification.skipped",
                status="skipped",
                name="verification",
                output_json={"reason": run.status},
                summary=f"verification skipped: task {run.status}",
            )
            return "skipped"
        plan = run.plan_json if isinstance(run.plan_json, dict) else {}
        verification = plan.get("verification_plan") if isinstance(plan.get("verification_plan"), dict) else {}
        if not _has_verification_checks(verification):
            return "not_applicable"
        events = await self.store.list_events(task_id)
        subtask_ids = [str(v) for v in (run.submitted_subtask_ids or []) if v]
        workflow_ids = [str(v) for v in (run.submitted_workflow_ids or []) if v]
        loaded_tasks = await self.store.list_subtasks_by_ids(subtask_ids)
        workflows = await self.store.list_workflows_by_ids(workflow_ids)
        workflow_tasks = await self.store.list_subtasks_by_workflow_ids(workflow_ids)
        workflow_steps: list[WorkflowStepORM] = []
        for workflow_id in workflow_ids:
            workflow_steps.extend(await self.store.list_workflow_steps(workflow_id))
        loaded_ids = {task.id for task in loaded_tasks}
        missing_submitted_tasks = [subtask_id for subtask_id in subtask_ids if subtask_id not in loaded_ids]
        loaded_workflow_ids = {workflow.id for workflow in workflows}
        missing_workflows = [
            workflow_id for workflow_id in workflow_ids if workflow_id not in loaded_workflow_ids
        ]
        tasks = effective_subtasks([*loaded_tasks, *workflow_tasks])
        status, checks = evaluate_verification(
            run,
            verification,
            events,
            tasks,
            missing_submitted_tasks=missing_submitted_tasks,
            workflows=workflows,
            workflow_steps=workflow_steps,
            missing_workflows=missing_workflows,
        )
        await self.store.append_event(
            task_id,
            event_type=f"verification.{status}",
            status=status,
            name="verification",
            output_json=checks,
            summary=_verification_summary(status, checks),
        )
        return status


def evaluate_verification(
    run: TaskORM,
    verification: Mapping[str, Any],
    events: list[TaskEventORM],
    tasks: list[SubtaskORM],
    *,
    missing_submitted_tasks: list[str] | None = None,
    workflows: list[WorkflowRunORM] | None = None,
    workflow_steps: list[WorkflowStepORM] | None = None,
    missing_workflows: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    workflows = list(workflows or [])
    workflow_steps = list(workflow_steps or [])
    required_events = [str(v) for v in verification.get("required_events") or [] if v]
    forbidden_tools = {str(v) for v in verification.get("forbidden_tools") or [] if v}
    required_tasks = [str(v) for v in verification.get("required_tasks") or [] if v]
    artifact_checks = [str(v) for v in verification.get("artifact_checks") or [] if v]
    provenance_checks = [str(v) for v in verification.get("provenance_checks") or [] if v]
    presentation_checks = [str(v) for v in verification.get("presentation_checks") or [] if v]
    deliverable_checks = [str(v) for v in verification.get("deliverable_checks") or [] if v]
    event_types = {event.event_type for event in events}
    missing_events = [name for name in required_events if name not in event_types]
    forbidden_hits = [
        {
            "event_type": event.event_type,
            "tool_name": event.tool_name or event.name,
            "seq": event.seq,
        }
        for event in events
        if (event.tool_name or event.name) in forbidden_tools
    ]
    missing_tasks: list[str] = []
    active_tasks: list[str] = []
    failed_tasks: list[str] = []
    degraded_required_tasks: list[str] = []
    workflow_status = {workflow.id: workflow.status for workflow in workflows}
    for required in required_tasks:
        matches = [task for task in tasks if task.skill_name == required]
        if not matches:
            missing_tasks.append(required)
            continue
        if any(task.status in _ACTIVE_SUBTASK_STATUSES for task in matches):
            active_tasks.extend(task.id for task in matches if task.status in _ACTIVE_SUBTASK_STATUSES)
        if not any(task.status in {"succeeded", "degraded"} for task in matches):
            terminal = [task for task in matches if task.status in _TERMINAL_SUBTASK_STATUSES]
            for task in terminal:
                if task.workflow_run_id and workflow_status.get(task.workflow_run_id) == "degraded":
                    degraded_required_tasks.append(task.id)
                else:
                    failed_tasks.append(task.id)
    # A run owns every task in submitted_subtask_ids, not only tasks named in the
    # optional required_tasks contract.  This prevents a parent from settling
    # while an artifact revision or another dynamically selected child is still
    # running, and prevents an unlisted failed child from being ignored.
    direct_tasks = [task for task in tasks if not task.workflow_run_id]
    submitted_active_tasks = [
        task.id for task in direct_tasks if task.status in _ACTIVE_SUBTASK_STATUSES
    ]
    submitted_failed_tasks = [
        task.id for task in direct_tasks if task.status in {"failed", "cancelled", "interrupted"}
    ]
    active_workflows = [
        workflow.id
        for workflow in workflows
        if workflow.status in _ACTIVE_SUBTASK_STATUSES
    ]
    failed_workflows = [
        workflow.id
        for workflow in workflows
        if workflow.status in {"failed", "cancelled", "interrupted"}
    ]
    degraded_workflows = [workflow.id for workflow in workflows if workflow.status == "degraded"]
    active_tasks = _unique([*active_tasks, *submitted_active_tasks])
    failed_tasks = _unique([*failed_tasks, *submitted_failed_tasks])
    artifact_failures = _artifact_check_failures(
        artifact_checks, run, tasks, events, workflow_steps
    )
    provenance_failures = _provenance_check_failures(
        provenance_checks, run, tasks, events, workflow_steps
    )
    presentation_failures, presentation_pending = _presentation_check_outcomes(
        presentation_checks,
        run,
        events,
    )
    plan = run.plan_json if isinstance(run.plan_json, Mapping) else {}
    task_contract = (
        plan.get("task_contract")
        if isinstance(plan.get("task_contract"), Mapping)
        else {}
    )
    (
        deliverable_failures,
        deliverable_degraded,
        deliverable_assessment_details,
    ) = _deliverable_check_outcomes(
        deliverable_checks,
        task_contract,
        tasks,
        workflow_steps,
    )
    execution_outcome = _execution_outcome(events)

    checks = {
        "required_events": required_events,
        "missing_events": missing_events,
        "forbidden_tools": forbidden_hits,
        "required_tasks": required_tasks,
        "missing_tasks": missing_tasks,
        "active_tasks": active_tasks,
        "failed_tasks": failed_tasks,
        "degraded_required_tasks": degraded_required_tasks,
        "missing_submitted_tasks": list(missing_submitted_tasks or []),
        "active_workflows": active_workflows,
        "failed_workflows": failed_workflows,
        "degraded_workflows": degraded_workflows,
        "missing_workflows": list(missing_workflows or []),
        "artifact_checks": artifact_checks,
        "artifact_failures": artifact_failures,
        "provenance_checks": provenance_checks,
        "provenance_failures": provenance_failures,
        "presentation_checks": presentation_checks,
        "presentation_failures": presentation_failures,
        "presentation_pending": presentation_pending,
        "deliverable_checks": deliverable_checks,
        "deliverable_failures": deliverable_failures,
        "deliverable_degraded": deliverable_degraded,
        "deliverable_assessment_details": deliverable_assessment_details,
        "execution_outcome": execution_outcome,
    }
    # Artifact, provenance, and deliverable checks describe the completed child
    # output.  They are not failures while a matching subtask can still
    # satisfy them.
    deferred_output_checks = bool(active_tasks or active_workflows) and bool(
        artifact_failures or provenance_failures or deliverable_failures
    )
    hard_artifact_failures = [] if deferred_output_checks else artifact_failures
    hard_provenance_failures = [] if deferred_output_checks else provenance_failures
    hard_deliverable_failures = [] if deferred_output_checks else deliverable_failures
    if (
        missing_events
        or forbidden_hits
        or missing_tasks
        or missing_submitted_tasks
        or missing_workflows
        or failed_tasks
        or failed_workflows
        or hard_artifact_failures
        or hard_provenance_failures
        or hard_deliverable_failures
        or presentation_failures
    ):
        status = "failed"
    elif active_tasks or active_workflows or presentation_pending or deferred_output_checks:
        status = "pending"
    elif execution_outcome in {"cancelled", "interrupted"}:
        status = "skipped"
    elif execution_outcome == "failed":
        status = "failed"
    elif (
        execution_outcome == "degraded"
        or deliverable_degraded
        or degraded_workflows
        or degraded_required_tasks
    ):
        status = "degraded"
    else:
        status = "passed"
    return status, checks


def effective_subtasks(tasks: list[SubtaskORM]) -> list[SubtaskORM]:
    superseded = {str(task.retry_of) for task in tasks if task.retry_of}
    return [task for task in tasks if task.id not in superseded]


def _verification_artifact_ids(
    run: TaskORM,
    tasks: list[SubtaskORM],
    events: list[TaskEventORM],
    workflow_steps: list[WorkflowStepORM] | None = None,
) -> list[str]:
    ids = list(run.artifact_ids or [])
    for task in tasks:
        ids.extend(_collect_artifact_ids(task.result_json or {}))
    for step in workflow_steps or []:
        ids.extend(_collect_artifact_ids(step.result_json or {}))
    for event in events:
        ids.extend(_collect_declared_artifact_ids(event.output_json or {}))
    return _unique(ids)


def _verification_research_ids(
    run: TaskORM,
    tasks: list[SubtaskORM],
    events: list[TaskEventORM],
    workflow_steps: list[WorkflowStepORM] | None = None,
) -> dict[str, list[str]]:
    found = {
        "source_ids": list(run.source_ids or []),
        "claim_ids": list(run.claim_ids or []),
        "evidence_ids": list(run.evidence_ids or []),
    }
    for payload in [
        *(task.result_json or {} for task in tasks),
        *(step.result_json or {} for step in workflow_steps or []),
        *(event.output_json or {} for event in events),
    ]:
        for key in found:
            found[key].extend(_collect_ids(payload, key))
    return {key: _unique(values) for key, values in found.items()}


def _artifact_check_failures(
    checks: list[str],
    run: TaskORM,
    tasks: list[SubtaskORM],
    events: list[TaskEventORM],
    workflow_steps: list[WorkflowStepORM] | None = None,
) -> list[str]:
    if not checks:
        return []
    artifact_ids = _verification_artifact_ids(run, tasks, events, workflow_steps)
    failures: list[str] = []
    for check in checks:
        if check == "child_task_has_artifact_contract":
            if not (artifact_ids or any(_task_declares_artifacts(task) for task in tasks)):
                failures.append(check)
            continue
        if check in {"artifact_emitted", "render_derivatives_or_report_failure"}:
            if not artifact_ids:
                failures.append(check)
            continue
        failures.append(f"unsupported:{check}")
    return failures


def _task_declares_artifacts(task: SubtaskORM) -> bool:
    result = task.result_json or {}
    return isinstance(result, Mapping) and ("artifacts" in result or "artifact_uri" in result)


def _deliverable_check_outcomes(
    checks: list[str],
    task_contract: Mapping[str, Any],
    tasks: list[SubtaskORM],
    workflow_steps: list[WorkflowStepORM] | None = None,
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Aggregate typed, provider-owned deliverable assessments.

    The verifier deliberately contains no figure-, draft-, or skill-specific
    semantic rules. Providers own those judgements; the host validates that an
    assessment exists for every requested criterion and aggregates its status.
    """
    if not checks:
        return [], [], []
    results = [task.result_json for task in tasks if isinstance(task.result_json, Mapping)]
    results.extend(
        step.result_json
        for step in workflow_steps or []
        if isinstance(step.result_json, Mapping)
    )
    outcome = evaluate_deliverable_assessments(
        checks,
        collect_deliverable_assessments(results),
        task_contract=task_contract,
    )
    return list(outcome.failures), list(outcome.degraded), list(outcome.details)


def _provenance_check_failures(
    checks: list[str],
    run: TaskORM,
    tasks: list[SubtaskORM],
    events: list[TaskEventORM],
    workflow_steps: list[WorkflowStepORM] | None = None,
) -> list[str]:
    if not checks:
        return []
    ids = _verification_research_ids(run, tasks, events, workflow_steps)
    has_research = any(ids.values())
    plan = run.plan_json if isinstance(run.plan_json, Mapping) else {}
    mode = str(plan.get("provenance_mode") or "light").lower()
    failures: list[str] = []
    for check in checks:
        if check == "log_revision_run":
            continue
        if check == "light_or_full_as_requested":
            # Verify provenance to the level the plan requested: a *full* run must
            # have recorded at least one source/claim/evidence; a *light* run is
            # satisfied by design (its conclusions may be labelled degraded).
            if mode == "full" and not has_research:
                failures.append("full_provenance_missing_evidence")
            continue
        if check == "source_or_claim_or_evidence_recorded":
            if not has_research:
                failures.append(check)
            continue
        if check == "artifact_provenance_capsule":
            # A produced artifact must ship a *grounded* provenance capsule.
            # Strengthened (north-star: verify entity relationships + content,
            # not "does a flag exist"): the satisfying ``provenance.capsule``
            # event must be (a) complete, (b) bound to one of the artifacts this
            # run actually produced, and (c) carry ≥1 real supporting entity id
            # (source/claim/evidence). This rejects a forged ``complete=true``
            # with no citations, and a grounded capsule for some *other*
            # artifact. Only enforced when artifacts exist.
            artifact_ids = set(_verification_artifact_ids(run, tasks, events, workflow_steps))
            if artifact_ids and not _has_grounded_capsule_for(events, artifact_ids):
                failures.append(check)
            continue
        failures.append(f"unsupported:{check}")
    return failures


def _capsule_covered_artifacts(payload: Mapping[str, Any]) -> set[str]:
    """Artifact ids a provenance capsule payload claims to describe.

    Reads the capsule's ``artifact_uri`` (``artifact://<id>`` or a bare id) plus
    any ``artifact_ids`` / ``artifact_id`` fields, so binding works regardless of
    which form the writer used.
    """
    covered: set[str] = set()
    uri = str(payload.get("artifact_uri") or "")
    if uri.startswith("artifact://"):
        covered.add(uri[len("artifact://"):])
    elif uri:
        covered.add(uri)
    raw_ids = payload.get("artifact_ids")
    if isinstance(raw_ids, list):
        covered.update(str(v) for v in raw_ids if v)
    if payload.get("artifact_id"):
        covered.add(str(payload["artifact_id"]))
    return {c for c in covered if c}


def _capsule_has_entity_ids(payload: Mapping[str, Any]) -> bool:
    """Whether the capsule payload cites ≥1 source/claim/evidence id (content)."""
    for key in ("source_ids", "claim_ids", "evidence_ids"):
        values = payload.get(key)
        if isinstance(values, list) and any(str(v).strip() for v in values):
            return True
    return False


def _has_grounded_capsule_for(events: list[TaskEventORM], artifact_ids: set[str]) -> bool:
    """A run has a grounded capsule when some ``provenance.capsule`` event is
    complete, is bound to one of the produced ``artifact_ids``, and cites at
    least one real supporting entity id.  Binding + content are checked here so a
    bare ``complete=true`` flag (or a capsule for an unrelated artifact) does not
    silently pass verification.
    """
    for event in events:
        if event.event_type != "provenance.capsule":
            continue
        payload = event.output_json if isinstance(event.output_json, Mapping) else {}
        if not payload.get("complete"):
            continue
        if not (_capsule_covered_artifacts(payload) & artifact_ids):
            continue
        if _capsule_has_entity_ids(payload):
            return True
    return False


def _presentation_check_outcomes(
    checks: list[str],
    run: TaskORM,
    events: list[TaskEventORM],
) -> tuple[list[str], list[str]]:
    if not checks:
        return [], []
    event_types = {event.event_type for event in events}
    assistant_messages = [event for event in events if event.event_type == "assistant.message"]
    react_finished = [event for event in events if event.event_type == "react.finished"]
    failures: list[str] = []
    pending: list[str] = []
    for check in checks:
        if check == "presentation_sent_or_degraded":
            # CLI/REPL delivers synchronously to stdout and records no channel
            # send event; only outbound IM channels must prove sent/degraded.
            channel = str(getattr(run, "channel", "") or "cli").lower()
            if channel in {"", "cli"}:
                continue
            final_deliveries = [
                event
                for event in events
                if event.event_type in {"presentation.sent", "presentation.degraded"}
                and str((event.output_json or {}).get("kind") or "turn") != "ack"
            ]
            failed_deliveries = [
                event
                for event in events
                if event.event_type == "presentation.failed"
                and str((event.output_json or {}).get("kind") or "turn") != "ack"
            ]
            if failed_deliveries:
                failures.append(check)
            elif not final_deliveries:
                pending.append(check)
            continue
        if check == "show_plan_reason":
            if not (run.plan_json or "plan.created" in event_types or "plan.validated" in event_types):
                failures.append(check)
            continue
        if check == "show_task_id":
            if not (
                run.submitted_workflow_ids
                or run.submitted_subtask_ids
                or any(event.workflow_run_id or event.subtask_id for event in events)
            ):
                failures.append(check)
            continue
        if check == "show_next_actions":
            if not (assistant_messages or "subtask.submitted" in event_types or "plan.executed" in event_types):
                failures.append(check)
            continue
        if check == "channel_appropriate_next_actions":
            if not assistant_messages:
                failures.append(check)
            continue
        if check == "show_partial_when_budget_exhausted":
            exhausted = any(
                is_bounded_termination(
                    str((event.output_json or {}).get("terminated_reason") or "")
                )
                for event in react_finished
            )
            if exhausted and not any(
                str((event.output_json or {}).get("kind") or "") in {"partial", "text"}
                for event in assistant_messages + react_finished
            ):
                failures.append(check)
            continue
        failures.append(f"unsupported:{check}")
    return failures, pending


def _execution_outcome(events: list[TaskEventORM]) -> str:
    """Aggregate the latest execution and post-review boundaries.

    ``execution.finished`` captures the original loop result, while
    ``react.finished`` captures post-review and artifact-contract processing.
    Neither stage may erase a stronger outcome from the other. Taking only the
    latest event of each type also lets a future recovery cycle supersede older
    attempts without permanently poisoning the run.
    """
    boundaries: list[TaskEventORM] = []
    for event_type in ("execution.finished", "react.finished"):
        event = next(
            (item for item in reversed(events) if item.event_type == event_type),
            None,
        )
        if event is not None:
            boundaries.append(event)
    if not boundaries:
        return "succeeded"
    outcomes: list[str] = []
    for event in boundaries:
        boundary_outcomes: list[str] = []
        if event.status in {"succeeded", "degraded", "failed"}:
            boundary_outcomes.append(event.status)
        payload = event.output_json or {}
        if payload.get("kind") or payload.get("terminated_reason"):
            boundary_outcomes.append(
                execution_outcome_status(
                    str(payload.get("kind") or ""),
                    str(payload.get("terminated_reason") or ""),
                )
            )
        outcomes.append(aggregate_outcome_status(*boundary_outcomes))
    return aggregate_outcome_status(*outcomes)


def _collect_ids(value: Any, key: str) -> list[str]:
    found: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, Mapping):
            raw = obj.get(key)
            if isinstance(raw, list):
                found.extend(str(v) for v in raw if v)
            elif raw:
                found.append(str(raw))
            research = obj.get("research")
            if isinstance(research, Mapping):
                walk(research)
            for nested_key in ("result", "results", "payload"):
                nested = obj.get(nested_key)
                if isinstance(nested, (Mapping, list)):
                    walk(nested)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(value)
    return _unique(found)


def _collect_artifact_ids(value: Any) -> list[str]:
    ids: list[str] = []

    def add_uri(uri: str) -> None:
        if uri.startswith("artifact://"):
            ids.append(uri[len("artifact://"):])

    def walk(obj: Any) -> None:
        if isinstance(obj, Mapping):
            for key, raw in obj.items():
                if key == "artifact_ids" and isinstance(raw, list):
                    ids.extend(str(item) for item in raw if item)
                elif key == "artifact_id" and raw:
                    ids.append(str(raw))
                elif isinstance(raw, str) and (raw.startswith("artifact://") or key.endswith("_uri")):
                    add_uri(raw)
                elif isinstance(raw, (Mapping, list)):
                    walk(raw)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(value)
    return _unique(ids)


def _collect_declared_artifact_ids(value: Any) -> list[str]:
    """Collect explicit producer declarations, not incidental artifact URIs."""
    ids: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, Mapping):
            raw_ids = obj.get("artifact_ids")
            if isinstance(raw_ids, list):
                ids.extend(str(item) for item in raw_ids if item)
            raw_id = obj.get("artifact_id")
            if raw_id:
                ids.append(str(raw_id))
            for nested_key in ("result", "results", "payload"):
                nested = obj.get(nested_key)
                if isinstance(nested, (Mapping, list)):
                    walk(nested)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(value)
    return _unique(ids)


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "")
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _has_verification_checks(verification: Any) -> bool:
    if not isinstance(verification, Mapping):
        return False
    return any(
        verification.get(key)
        for key in (
            "required_events",
            "forbidden_tools",
            "required_tasks",
            "artifact_checks",
            "provenance_checks",
            "presentation_checks",
            "deliverable_checks",
        )
    )


def _verification_summary(status: str, checks: Mapping[str, Any]) -> str:
    if status == "passed":
        return "verification passed"
    if status == "degraded":
        degraded_deliverables = checks.get("deliverable_degraded")
        if degraded_deliverables:
            return "verification degraded: " + ", ".join(str(v) for v in degraded_deliverables)
        return "verification degraded: execution reached a bounded terminal condition"
    problems = []
    for key in (
        "missing_events",
        "forbidden_tools",
        "missing_tasks",
        "active_tasks",
        "failed_tasks",
        "artifact_failures",
        "provenance_failures",
        "presentation_failures",
        "deliverable_failures",
        "deliverable_degraded",
    ):
        values = checks.get(key)
        if values:
            problems.append(f"{key}={len(values)}")
    detail = ", ".join(problems) or "no blocking detail"
    return f"verification {status}: {detail}"
