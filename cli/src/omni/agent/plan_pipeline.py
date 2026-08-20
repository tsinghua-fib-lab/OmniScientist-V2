"""Authoritative typed-plan lifecycle from proposal through execution binding.

The orchestrator owns the user turn.  This collaborator owns the plan truth:
produce candidates, validate and repair detached snapshots, select exactly one
authoritative revision, and fail closed if execution diverges from that truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from omni.agent.cost import record_text_cost_event
from omni.agent.input_resolution import apply_identifier_resolution
from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.interaction_lifecycle import apply_interaction_mode
from omni.agent.model_planner import ModelIntentPlanner
from omni.agent.plan_factory import build_assistant_plan
from omni.agent.plan_lifecycle import plan_diff
from omni.agent.plan_recovery import (
    ACTION_HARD_STOP,
    ACTION_REACT,
    RecoveryOutcome,
    hard_stop_reasons,
    recover,
    recovery_event,
)
from omni.agent.plan_revision import (
    ExecutionAuthority,
    PlanRevision,
    canonical_plan_hash,
    create_execution_authority,
    create_revision,
    deep_clone_plan,
    registry_snapshot_hashes,
)
from omni.agent.plan_runner_utils import approval_tools_for_plan, gap_default, plan_summary
from omni.agent.plan_validator import (
    SEVERITY_BLOCKING,
    PlanFinding,
    PlanValidationResult,
    PlanValidator,
)
from omni.agent.planner import IntentPlanner
from omni.agent.provider_binding import preserves_legacy_accepted_revision
from omni.runtime.workflow_plan import WorkflowNeedsInput, prepare_workflow_plan


@dataclass(slots=True)
class PlanPipelineResult:
    """The one plan state the outer turn may inspect or execute."""

    plan: IntentPlan
    validation: PlanValidationResult
    revision: PlanRevision
    recovery: RecoveryOutcome
    execution_authority: ExecutionAuthority | None = None
    approval_bound_hash: str = ""
    hard_stop_reasons: list[str] = field(default_factory=list)
    recovery_react_notes: list[str] = field(default_factory=list)


class PlanPipeline:
    """Build and seal one authoritative typed plan for a turn."""

    def __init__(self, *, settings: Any, registry: Any, tasks: Any, hooks: Any) -> None:
        self.settings = settings
        self.registry = registry
        self.tasks = tasks
        self.hooks = hooks

    async def run(
        self,
        *,
        llm: Any,
        user_message: str,
        task_id: str,
        mode: str,
        approved_plan: IntentPlan | None,
        turn_context: Any,
        context_summary: str,
        recent_activity: str,
        on_tool_event: Any,
        forward: Any,
        planner_factory: Any = IntentPlanner,
        carry_contract: bool = False,
    ) -> PlanPipelineResult:
        """Produce, repair, recover, and persist the final authoritative plan."""
        planner = planner_factory(self.registry)
        plan, planning_events, approval_bound_hash = await self._produce_plan(
            planner=planner,
            llm=llm,
            user_message=user_message,
            task_id=task_id,
            mode=mode,
            approved_plan=approved_plan,
            carry_contract=carry_contract,
            turn_context=turn_context,
            context_summary=context_summary,
            recent_activity=recent_activity,
        )
        catalog_hash, contract_hash = registry_snapshot_hashes(
            self.registry,
            plan,
        )
        # The model/boundary decision causally precedes the typed revision it
        # produced, so record that proposal before sealing its snapshot.
        for event in planning_events:
            await self.tasks.append_event(task_id, **event)
            await forward(on_tool_event, event)
        proposed_revision = create_revision(
            plan,
            revision=plan.revision if approved_plan is not None else 0,
            parent_hash=plan.parent_revision_hash or None,
            source="approved" if approved_plan is not None else "planner",
            stage="approved" if approved_plan is not None else "proposed",
            catalog_hash=catalog_hash,
            contract_hash=contract_hash,
        )
        plan = await self._record_revision(
            proposed_revision,
            status="created",
            event_type="plan.revision.proposed",
            on_tool_event=on_tool_event,
            forward=forward,
            # A resumed approval already has an accepted durable projection.
            # Rewriting it as "proposed" would invalidate the just-claimed
            # authority before the accepted snapshot is re-sealed.
            persist=approved_plan is None,
        )

        plan, validation, current_revision = await self._compile_candidate(
            plan=plan,
            proposed_revision=proposed_revision,
            task_id=task_id,
            catalog_hash=catalog_hash,
            contract_hash=contract_hash,
            on_tool_event=on_tool_event,
            forward=forward,
        )
        return await self._recover_and_accept(
            plan=plan,
            validation=validation,
            current_revision=current_revision,
            approval_bound_hash=approval_bound_hash,
            mode=mode,
            catalog_hash=catalog_hash,
            contract_hash=contract_hash,
            on_tool_event=on_tool_event,
            forward=forward,
        )

    async def bind_execution_plan(
        self,
        plan: IntentPlan,
        revision: PlanRevision,
        *,
        on_tool_event: Any,
        forward: Any,
    ) -> ExecutionAuthority:
        """Fail closed unless persisted, accepted, and executing plans agree."""
        expected = canonical_plan_hash(plan)
        persisted = await self.tasks.get_task(plan.task_id)
        persisted_plan = (
            IntentPlan.from_dict(persisted.plan_json)
            if persisted is not None and isinstance(persisted.plan_json, dict)
            else None
        )
        persisted_hash = (
            canonical_plan_hash(persisted_plan) if persisted_plan is not None else ""
        )
        if (
            persisted_plan is None
            or persisted_hash != expected
            or revision.stage != "accepted"
            or revision.content_hash != expected
            or not plan.revision_hash
            or plan.revision_hash != expected
        ):
            raise RuntimeError(
                "authoritative plan mismatch: persisted and executing revisions differ"
            )
        live_authority = create_execution_authority(
            plan,
            registry=self.registry,
            approval_tools=approval_tools_for_plan(plan, self.registry),
        )
        if (
            revision.catalog_hash != live_authority.catalog_hash
            or revision.contract_hash != live_authority.contract_hash
        ):
            raise RuntimeError(
                "authoritative contract snapshot changed before execution"
            )
        current_fingerprint = str(
            getattr(persisted, "current_authority_fingerprint", "") or ""
        )
        approval_fingerprint = str(
            getattr(persisted, "approval_authority_fingerprint", "") or ""
        )
        if (
            not current_fingerprint
            or current_fingerprint != live_authority.fingerprint
            or (
                approval_fingerprint
                and approval_fingerprint != live_authority.fingerprint
            )
        ):
            raise RuntimeError(
                "authoritative execution fingerprint changed before execution"
            )
        execution_candidate = deep_clone_plan(plan)
        execution_validation = self._validate(execution_candidate)
        execution_candidate, execution_validation = (
            self._materialize_workflow(
                execution_candidate,
                execution_validation,
            )
        )
        if not execution_validation.ok:
            finding_codes = ", ".join(
                sorted(
                    {
                        finding.code
                        for finding in execution_validation.findings
                    }
                )
            )
            raise RuntimeError(
                "final execution validation rejected accepted plan"
                + (f": {finding_codes}" if finding_codes else "")
            )
        if canonical_plan_hash(execution_candidate) != expected:
            raise RuntimeError(
                "final execution validation changed accepted plan"
            )
        event = {
            "event_type": "plan.execution.bound",
            "status": "succeeded",
            "name": plan.intent_type.value,
            "output_json": {
                "revision": plan.revision,
                "revision_hash": expected,
            },
            "summary": f"execution bound to plan revision {plan.revision}",
        }
        await self.tasks.append_event(plan.task_id, **event)
        await forward(on_tool_event, event)
        return live_authority

    async def _produce_plan(
        self,
        *,
        planner: IntentPlanner,
        llm: Any,
        user_message: str,
        task_id: str,
        mode: str,
        approved_plan: IntentPlan | None,
        carry_contract: bool = False,
        turn_context: Any,
        context_summary: str,
        recent_activity: str,
    ) -> tuple[IntentPlan, list[dict[str, Any]], str]:
        approval_bound_hash = ""
        carried = None
        if approved_plan is not None:
            plan = deep_clone_plan(approved_plan)
            if plan.task_id != task_id or plan.user_message != user_message:
                raise ValueError("approved plan identity does not match the resumed task")
            approval_bound_hash = plan.revision_hash or canonical_plan_hash(plan)
            events = [
                {
                    "event_type": "plan.approved",
                    "status": "succeeded",
                    "name": plan.intent_type.value,
                    "output_json": {
                        "plan_id": plan.plan_id,
                        "intent_type": plan.intent_type.value,
                        "revision": plan.revision,
                        "revision_hash": approval_bound_hash,
                    },
                    "summary": f"approved plan {plan.plan_id[:8]}",
                }
            ]
        else:
            if carry_contract and self.tasks is not None:
                from omni.runtime.task_continue import continue_from_persisted_plan

                persisted = await self.tasks.get_task(task_id)
                carried = continue_from_persisted_plan(user_message, task_id, persisted)
            if carried is not None:
                plan = carried
                events = [
                    {
                        "event_type": "plan.contract.carried",
                        "status": "succeeded",
                        "name": plan.intent_type.value,
                        "output_json": {
                            "intent_type": plan.intent_type.value,
                            "outputs": list(plan.outputs),
                            "provenance_mode": plan.provenance_mode,
                        },
                        "summary": "continued from the accepted task contract",
                    }
                ]
            else:
                boundary = (
                    planner.boundary_plan(user_message, task_id=task_id)
                    if hasattr(planner, "boundary_plan")
                    else None
                )
                if boundary is not None:
                    plan = boundary
                    events = [
                        {
                            "event_type": "plan.boundary.selected",
                            "status": "succeeded",
                            "name": boundary.intent_type.value,
                            "output_json": {
                                "intent_type": boundary.intent_type.value,
                                "rationale": boundary.rationale,
                            },
                            "summary": boundary.rationale[:220],
                        }
                    ]
                elif mode == "plan":
                    plan = build_assistant_plan(
                        user_message,
                        task_id=task_id,
                        rationale=(
                            "plan mode skipped the semantic planner; "
                            "host presents a read-only plan for approval"
                        ),
                        confidence=0.7,
                    )
                    events = [
                        {
                            "event_type": "plan.mode.bounded",
                            "status": "succeeded",
                            "name": "plan",
                            "summary": (
                                "plan mode did not run ModelIntentPlanner.propose"
                            ),
                        }
                    ]
                else:
                    planner_context = "\n\n".join(
                        part for part in (context_summary, recent_activity) if part
                    )
                    plan, events = await self._plan_with_model(
                        planner,
                        llm=llm,
                        user_message=user_message,
                        task_id=task_id,
                        turn_context=turn_context,
                        context_summary=planner_context,
                    )
        if approved_plan is None and carried is None:
            plan, repeat_event = await self._rerun_rather_than_ask(
                plan, user_message=user_message, task_id=task_id
            )
            if repeat_event is not None:
                events.append(repeat_event)
        return deep_clone_plan(apply_interaction_mode(plan, mode)), events, approval_bound_hash

    async def _rerun_rather_than_ask(
        self, plan: IntentPlan, *, user_message: str, task_id: str
    ) -> tuple[IntentPlan, dict[str, Any] | None]:
        """Never ask reuse-or-rerun; always run again; hint if a twin is fresh.

        "You asked for this before — shall I reuse it or run it again?" is not a
        missing input. Codex and Claude Code do not detect duplicates: a repeat
        is a new turn. Asking costs a round trip and the question has already
        been dropped on the way to the user (cff3eeda).

        Only an exact repeat of a *succeeded* turn qualifies. A near-miss is a
        different request. Age no longer chooses retrieve vs rerun — the turn
        always runs. A twin inside the window is mentioned to the user as a
        footnote so they can open the earlier task; it is not a model instruction
        and not a degraded warning.
        """
        finished, fresh = await self._finished_twin(user_message, exclude_id=task_id)
        event: dict[str, Any] | None = None
        if plan.intent_type is IntentType.NEEDS_INPUT and finished:
            plan = build_assistant_plan(
                plan.user_message,
                task_id=task_id,
                rationale=(
                    "this request succeeded before; running it again instead of asking "
                    "whether to reuse or re-run it"
                ),
                confidence=max(plan.confidence, 0.6),
            )
            event = {
                "event_type": "plan.rerun.preferred",
                "status": "succeeded",
                "name": "react_fallback",
                "output_json": {
                    "twin_task_id": finished,
                    "fresh": fresh,
                    "replaced": "needs_input",
                },
                "summary": (
                    f"an identical request already succeeded as {finished[:8]}; "
                    "running it again rather than asking the user to choose"
                ),
            }
        if fresh and finished:
            notice = identical_twin_notice(finished, user_message)
            plan.user_notices = list(dict.fromkeys([*plan.user_notices, notice]))
            plan.twin_task_id = finished
        return plan, event

    async def _finished_twin(
        self, user_message: str, *, exclude_id: str = ""
    ) -> tuple[str, bool]:
        """Id of a succeeded turn answering this exact request, and its freshness."""
        wanted = " ".join((user_message or "").split()).casefold()
        if not wanted or self.tasks is None:
            return "", False
        try:
            rows = await self.tasks.list_tasks(limit=50, status="succeeded", kind="turn")
        except Exception:  # noqa: BLE001 - a history read must never block planning.
            return "", False
        skip = (exclude_id or "").strip()
        for row in rows:
            said = " ".join(str(getattr(row, "user_input", "") or "").split()).casefold()
            if said != wanted:
                continue
            found = str(getattr(row, "id", "") or "")
            if not found or found == skip:
                continue
            return found, self._within_retrieval_window(row)
        return "", False

    def _within_retrieval_window(self, row: Any) -> bool:
        minutes = int(getattr(getattr(self.settings, "planner", None), "retrieval_window_minutes", 30))
        if minutes <= 0:
            return False
        finished = getattr(row, "finished_at", None) or getattr(row, "created_at", None)
        if not isinstance(finished, datetime):
            return False
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=UTC)
        return datetime.now(UTC) - finished <= timedelta(minutes=minutes)

    async def _plan_with_model(
        self,
        planner: IntentPlanner,
        *,
        llm: Any,
        user_message: str,
        task_id: str,
        turn_context: Any,
        context_summary: str,
    ) -> tuple[IntentPlan, list[dict[str, Any]]]:
        if not hasattr(planner, "boundary_plan") or not hasattr(
            planner, "plan_from_proposal"
        ):
            return planner.plan(user_message, task_id=task_id), []
        if not context_summary and turn_context is not None:
            context_summary = turn_context.to_planner_summary()
        try:
            model_planner = ModelIntentPlanner(
                llm,
                self.registry,
                settings=self.settings,
            )
            proposal = await model_planner.propose(
                user_message,
                context_summary=context_summary,
            )
            await record_text_cost_event(
                self.tasks,
                self.settings,
                llm,
                task_id,
                system=model_planner.last_system,
                user_message=model_planner.last_user,
                output=model_planner.last_output,
                component="planner",
            )
        except Exception as exc:  # noqa: BLE001 - deterministic planner is the floor.
            return planner.plan(user_message, task_id=task_id), [
                {
                    "event_type": "plan.model.failed",
                    "status": "failed",
                    "name": "model_planner",
                    "error": f"{type(exc).__name__}: {exc}",
                    "summary": (
                        "model planner failed; falling back to deterministic planner"
                    ),
                }
            ]
        if proposal is None:
            return planner.plan(user_message, task_id=task_id), [
                {
                    "event_type": "plan.model.degraded",
                    "status": "degraded",
                    "name": "model_planner",
                    "summary": (
                        "model planner returned no valid JSON; falling back to "
                        "deterministic planner"
                    ),
                }
            ]
        plan = planner.plan_from_proposal(
            user_message,
            proposal,
            task_id=task_id,
        )
        # A gap the planner defaulted its way past is not asked, but it is not
        # forgotten either: it rides along so the turn can run on the value and
        # name it in the answer. Only a plan that is *not* asking needs this —
        # on a needs_input plan the gaps are the question itself.
        if plan.intent_type is not IntentType.NEEDS_INPUT and not plan.missing_inputs:
            plan.missing_inputs = [
                dict(gap) for gap in proposal.missing_inputs if gap_default(gap)
            ]
        return plan, [
            {
                "event_type": "plan.model.proposed",
                "status": "succeeded",
                "name": proposal.intent_type,
                "output_json": proposal.to_dict(),
                "summary": proposal.rationale[:220],
            }
        ]

    def _validate(self, plan: IntentPlan) -> PlanValidationResult:
        return self._validate_structural(plan)

    def _validate_structural(
        self,
        plan: IntentPlan,
    ) -> PlanValidationResult:
        plan.inputs_compiled = False
        plan.provider_inputs = {}
        plan.input_compilation_errors = []
        plan.validation_warnings = []
        plan.degraded_warnings = []
        return PlanValidator(self.registry).validate(plan)

    async def _compile_candidate(
        self,
        *,
        plan: IntentPlan,
        proposed_revision: PlanRevision,
        task_id: str,
        catalog_hash: str,
        contract_hash: str,
        on_tool_event: Any,
        forward: Any,
    ) -> tuple[IntentPlan, PlanValidationResult, PlanRevision]:
        candidate = deep_clone_plan(plan)
        validation = self._validate(candidate)
        candidate, validation = await apply_identifier_resolution(
            candidate,
            validation,
            registry=self.registry,
            tasks=self.tasks,
            task_id=task_id,
            on_tool_event=on_tool_event,
            forward=forward,
            allow_network=self.settings.model.provider != "mock",
        )
        candidate, validation = self._materialize_workflow(candidate, validation)
        revision = create_revision(
            candidate,
            revision=proposed_revision.revision + 1,
            parent_hash=proposed_revision.content_hash,
            source="compiler",
            stage="candidate",
            finding_ids=[
                finding.finding_id for finding in validation.findings
            ],
            diff=plan_diff(proposed_revision.plan, candidate),
            catalog_hash=catalog_hash,
            contract_hash=contract_hash,
        )
        candidate = await self._record_revision(
            revision,
            status=validation.status,
            event_type="plan.revision.candidate",
            on_tool_event=on_tool_event,
            forward=forward,
            reason=(
                "provider inputs compiled and contract snapshot sealed"
            ),
        )
        return candidate, validation, revision

    async def _recover_and_accept(
        self,
        *,
        plan: IntentPlan,
        validation: PlanValidationResult,
        current_revision: PlanRevision,
        approval_bound_hash: str,
        mode: str,
        catalog_hash: str,
        contract_hash: str,
        on_tool_event: Any,
        forward: Any,
    ) -> PlanPipelineResult:
        unresolved = validation
        recovery_validation = unresolved
        recovery = recover(
            deep_clone_plan(plan),
            recovery_validation,
            self.registry,
        )
        if recovery.action == ACTION_HARD_STOP:
            event = recovery_event(recovery, recovery_validation)
            event["output_json"]["finding_state"] = "open"
            await self.tasks.append_event(plan.task_id, **event)
            await forward(on_tool_event, event)
            return PlanPipelineResult(
                plan=plan,
                validation=unresolved,
                revision=current_revision,
                recovery=recovery,
                approval_bound_hash=approval_bound_hash,
                hard_stop_reasons=hard_stop_reasons(
                    recovery,
                    recovery_validation,
                ),
            )

        recovery_preserved_snapshot = (
            recovery.action == "execute"
            and validation.ok
            and canonical_plan_hash(recovery.plan) == current_revision.content_hash
        )
        final_candidate = deep_clone_plan(recovery.plan)
        if recovery_preserved_snapshot:
            # The compiler already materialized and validated this exact
            # content-addressed snapshot. Recovery only selected it, so running
            # both passes again cannot add evidence and only increases latency.
            final_validation = validation
        else:
            final_validation = self._validate(final_candidate)
            final_candidate, final_validation = self._materialize_workflow(
                final_candidate,
                final_validation,
            )
        if not final_validation.ok:
            recovery_validation = final_validation
            recovery = recover(
                final_candidate,
                recovery_validation,
                self.registry,
            )
            if recovery.action == ACTION_HARD_STOP:
                event = recovery_event(recovery, recovery_validation)
                event["output_json"]["finding_state"] = "open"
                await self.tasks.append_event(plan.task_id, **event)
                await forward(on_tool_event, event)
                return PlanPipelineResult(
                    plan=final_candidate,
                    validation=final_validation,
                    revision=current_revision,
                    recovery=recovery,
                    approval_bound_hash=approval_bound_hash,
                    hard_stop_reasons=hard_stop_reasons(
                        recovery,
                        recovery_validation,
                    ),
                )
            final_candidate = deep_clone_plan(recovery.plan)
            final_validation = self._validate(final_candidate)
            final_candidate, final_validation = self._materialize_workflow(
                final_candidate,
                final_validation,
            )
            if not final_validation.ok:
                raise RuntimeError(
                    "recovery produced a plan that still fails final validation"
                )
        if recovery.notes and recovery.action != ACTION_REACT:
            final_candidate.degraded_warnings = list(
                dict.fromkeys(
                    [
                        *final_candidate.degraded_warnings,
                        *recovery.notes,
                    ]
                )
            )
        if canonical_plan_hash(final_candidate) != current_revision.content_hash:
            current_revision = create_revision(
                final_candidate,
                revision=current_revision.revision + 1,
                parent_hash=current_revision.content_hash,
                source="recovery",
                stage="candidate",
                finding_ids=[
                    finding.finding_id
                    for finding in recovery_validation.findings
                ],
                diff=plan_diff(plan, final_candidate),
                catalog_hash=catalog_hash,
                contract_hash=contract_hash,
            )
            final_candidate = await self._record_revision(
                current_revision,
                status=final_validation.status,
                event_type="plan.revision.candidate",
                on_tool_event=on_tool_event,
                forward=forward,
                reason=f"recovery selected {recovery.action}",
            )

        accepted_revision = create_revision(
            final_candidate,
            revision=current_revision.revision,
            parent_hash=current_revision.parent_hash,
            source=current_revision.source,
            stage="accepted",
            finding_ids=current_revision.finding_ids,
            diff=current_revision.diff,
            catalog_hash=catalog_hash,
            contract_hash=contract_hash,
        )
        accepted_authority = create_execution_authority(
            final_candidate,
            registry=self.registry,
            approval_tools=approval_tools_for_plan(
                final_candidate,
                self.registry,
            ),
        )
        if (
            accepted_authority.catalog_hash != catalog_hash
            or accepted_authority.contract_hash != contract_hash
        ):
            raise RuntimeError(
                "registry execution authority changed while sealing the plan"
            )
        accepted_plan = accepted_revision.plan
        recovery_decision_event = recovery_event(
            recovery,
            recovery_validation,
        )
        recovery_decision_event["output_json"]["finding_state"] = (
            "resolved" if recovery.action == "execute" else "open"
        )
        recovery_decision_event["output_json"]["revision_id"] = (
            accepted_revision.revision_id
        )
        validated_event = self._validated_event(
            plan=accepted_plan,
            validation=final_validation,
            revision=accepted_revision,
        )
        plan = await self._record_revision(
            accepted_revision,
            status=final_validation.status,
            event_type="plan.revision.accepted",
            on_tool_event=on_tool_event,
            forward=forward,
            reason=f"authoritative candidate selected after {recovery.action}",
            persist=True,
            authority_fingerprint=accepted_authority.fingerprint,
            additional_events=[
                recovery_decision_event,
                validated_event,
            ],
        )
        await self._emit_post_plan_hook(
            plan=plan,
            validation=final_validation,
            revision=accepted_revision,
            mode=mode,
        )
        return PlanPipelineResult(
            plan=plan,
            validation=final_validation,
            revision=accepted_revision,
            recovery=recovery,
            execution_authority=accepted_authority,
            approval_bound_hash=approval_bound_hash,
            recovery_react_notes=(
                list(recovery.notes) if recovery.action == ACTION_REACT else []
            ),
        )

    def _validated_event(
        self,
        *,
        plan: IntentPlan,
        validation: PlanValidationResult,
        revision: PlanRevision,
    ) -> dict[str, Any]:
        return {
            "event_type": "plan.validated",
            "status": "succeeded",
            "name": plan.intent_type.value,
            "summary": plan_summary(plan),
            "output_json": {
                **plan.to_dict(),
                "intent_type": plan.intent_type.value,
                "status": validation.status,
                "revision": revision.revision,
                "revision_id": revision.revision_id,
                "revision_hash": revision.content_hash,
                "warnings": [
                    *validation.display_warnings,
                    *validation.display_degraded_warnings,
                ],
                "steps": [
                    str(
                        step.get("skill_name")
                        or step.get("capability")
                        or step.get("id")
                        or ""
                    )
                    for step in plan.workflow_steps
                ],
                "skills": [
                    selection.skill for selection in plan.selected_skills
                ],
            },
        }

    async def _emit_post_plan_hook(
        self,
        *,
        plan: IntentPlan,
        validation: PlanValidationResult,
        revision: PlanRevision,
        mode: str,
    ) -> None:
        await self.hooks.emit(
            "post_plan",
            task_id=plan.task_id,
            payload={
                "mode": mode,
                "validation": validation.status,
                "revision_id": revision.revision_id,
                "plan": plan.to_dict(),
            },
        )

    async def _record_revision(
        self,
        revision: PlanRevision,
        *,
        status: str,
        event_type: str,
        on_tool_event: Any,
        forward: Any,
        reason: str = "",
        persist: bool = False,
        authority_fingerprint: str = "",
        additional_events: list[dict[str, Any]] | None = None,
    ) -> IntentPlan:
        plan = revision.plan
        events: list[tuple[dict[str, Any], bool]] = []
        if event_type == "plan.revision.proposed":
            events.append(
                (
                    {
                        "event_type": "plan.created",
                        "status": "succeeded",
                        "name": "plan",
                        "output_json": plan.to_dict(),
                        "summary": (
                            f"plan created: {plan.intent_type.value}"
                        ),
                    },
                    False,
                )
            )
        event = {
            "event_type": event_type,
            "status": "succeeded",
            "name": revision.source,
            "output_json": {
                **revision.to_dict(),
                "validation_status": status,
            },
            "summary": (
                reason or f"plan revision {revision.revision} {revision.source}"
            )[:220],
        }
        events.append((event, True))
        events.extend(
            (additional, True)
            for additional in (additional_events or [])
        )
        if persist and callable(
            transition := getattr(
                self.tasks,
                "record_plan_transition",
                None,
            )
        ):
            await transition(
                plan.task_id,
                plan,
                status=status,
                events=[item for item, _ in events],
                current_authority_fingerprint=authority_fingerprint,
            )
        else:
            if persist:
                await self.tasks.record_plan(
                    plan.task_id,
                    plan,
                    status=status,
                    emit_event=False,
                    current_authority_fingerprint=authority_fingerprint,
                )
            for item, _ in events:
                await self.tasks.append_event(plan.task_id, **item)
        for item, should_forward in events:
            if should_forward:
                await forward(on_tool_event, item)
        return plan

    def _materialize_workflow(
        self,
        plan: IntentPlan,
        validation: PlanValidationResult,
    ) -> tuple[IntentPlan, PlanValidationResult]:
        """Seal the exact workflow shape runtime will persist and execute."""
        if plan.intent_type != IntentType.WORKFLOW:
            return plan, validation
        candidate = deep_clone_plan(plan)
        try:
            candidate.workflow_steps = prepare_workflow_plan(
                candidate.user_message,
                candidate.workflow_steps,
                self.registry,
                seal_provider_bindings=not preserves_legacy_accepted_revision(
                    candidate
                ),
            )
        except WorkflowNeedsInput as exc:
            blocked = self._validate(candidate)
            for item in exc.missing:
                missing = [str(value) for value in item.get("missing") or []]
                reason = str(item.get("reason") or str(exc))
                for field_name in missing or ["workflow_input"]:
                    finding = PlanFinding(
                        code="step_input_contract",
                        message=reason,
                        severity=SEVERITY_BLOCKING,
                        scope="step",
                        step_id=str(item.get("step_id") or ""),
                        skill_name=str(item.get("skill_name") or ""),
                        missing_field=field_name,
                        repairable=False,
                    )
                    blocked.findings.append(finding)
                    blocked.errors.append(reason)
            blocked.status = "rejected"
            return candidate, blocked
        return candidate, self._validate_structural(candidate)


def identical_twin_notice(task_id: str, user_message: str) -> str:
    """User-facing footnote for a fresh identical succeeded turn.

    Not a model instruction and not a degraded warning. The executing turn
    still produces new artifacts; this only names the earlier task.
    """
    short = (task_id or "")[:8]
    _ = user_message
    return (
        f"An identical request succeeded recently as `{short}`. "
        f"This turn is producing a new result; `/task show {short}` opens the earlier one."
    )
