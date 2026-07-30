"""Contract-driven intent planning boundary.

This module is intentionally small. It owns deterministic boundaries
(commands, explicit skills, safety/no-tool cases, vague references) and turns
semantic model proposals into capability/deliverable plans. It does not encode
domain workflows or concrete skill names for automatic routing.
"""

from __future__ import annotations

from omni.agent.boundary_router import (
    BoundaryDecision,
    BoundaryRouter,
    explicit_skill_ref,
)
from omni.agent.capabilities import (
    CAPABILITY_ARTIFACT_REVISE,
    CAPABILITY_FIGURE,
    CAPABILITY_GROUNDED_QA,
    deliverables_from_capabilities,
)
from omni.agent.intent_plan import (
    ContextPolicy,
    IntentPlan,
    IntentType,
    ToolPolicy,
    VerificationPlan,
)
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.plan_factory import build_assistant_plan, build_schedule_plan
from omni.agent.skill_arbitrator import SkillArbitrator
from omni.agent.workflow_plan_builder import (
    WorkflowPlanBuilder,
    needs_input_plan,
    steps_from_capabilities,
)
from omni.skills_runtime.registry import SkillRegistry


class IntentPlanner:
    """Produce a typed runtime plan from boundary decisions or semantic proposals."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry
        self._boundary = BoundaryRouter(registry)
        self._arbitrator = SkillArbitrator(registry)
        self._workflow_builder = WorkflowPlanBuilder(self._arbitrator)

    def boundary_plan(self, user_message: str, *, task_id: str = "") -> IntentPlan | None:
        decision = self._boundary.route(user_message or "")
        if decision is None:
            return None
        return self._plan_for_boundary(user_message or "", decision, task_id=task_id)

    def plan(self, user_message: str, *, task_id: str = "") -> IntentPlan:
        """Deterministic fallback used only when the model planner is unavailable.

        The plan pipeline calls this from ``_plan_with_model`` when there is
        no real model (offline/mock) or the model call failed/returned no JSON.
        With a real model present, intent is routed by ``plan_from_proposal``.
        The offline fallback never tries to infer domain semantics.
        """
        boundary = self.boundary_plan(user_message, task_id=task_id)
        if boundary is not None:
            return boundary
        return self._assistant_plan(
            user_message or "",
            task_id=task_id,
            rationale="semantic planner unavailable; using capable bounded assistant",
        )

    def plan_from_proposal(
        self,
        user_message: str,
        proposal: ModelPlanProposal,
        *,
        task_id: str = "",
    ) -> IntentPlan:
        """Build an executable plan from a capability-level model proposal."""
        return self._plan_from_proposal(
            user_message,
            proposal,
            task_id=task_id,
        )

    def _plan_from_proposal(
        self,
        user_message: str,
        proposal: ModelPlanProposal,
        *,
        task_id: str = "",
    ) -> IntentPlan:
        text = user_message or ""
        rationale = proposal.rationale or "model semantic planner proposal"
        # Scheduling takes precedence over every capability/workflow route (and over
        # the needs_input short-circuit): "run this on a schedule" is not a research
        # capability, so a focused scheduling turn owns it instead of the capability
        # matcher (which would reject it with "no executable contracted provider").
        # The scheduling tools themselves ask a concise question when the timing is
        # genuinely ambiguous, so we do not pre-empt with needs_input here.
        if proposal.intent_type == "schedule":
            return build_schedule_plan(
                text,
                task_id=task_id,
                rationale=rationale,
                confidence=proposal.confidence or 0.8,
                deferred_goal=_schedule_deferred_goal(proposal, text),
                provenance_mode=proposal.provenance_mode,
            )
        # Ask-last: "ask the user" is an intent the model chooses, not a veto the
        # planning boundary derives from advisory metadata. We short-circuit to a
        # question only when the model *explicitly* chose needs_input, or when it
        # reported gaps and left no workflow to execute. When there are steps, we
        # build the plan and let the validator + recovery ladder decide — genuine
        # gaps are repaired (e.g. rerouted to literature.search) before we ever
        # ask, so a value the agent can discover is never turned into a question.
        if proposal.intent_type == "needs_input" or (
            proposal.missing_inputs and not proposal.workflow_steps
        ):
            return needs_input_plan(
                text,
                task_id=task_id,
                missing_inputs=proposal.missing_inputs
                or [{"field": "request", "reason": "model planner could not resolve a safe executable intent"}],
                rationale=rationale,
                confidence=max(proposal.confidence, 0.55),
            )

        caps = list(proposal.required_capabilities)
        step_caps = [str(step.get("capability") or "") for step in proposal.workflow_steps]
        all_caps = [cap for cap in [*caps, *step_caps] if cap]
        wants_artifact = CAPABILITY_FIGURE in all_caps or "artifact" in proposal.outputs
        wants_answer = CAPABILITY_GROUNDED_QA in all_caps or "answer" in proposal.outputs

        if proposal.intent_type == "memory_update" or "memory.update" in all_caps:
            plan = self._utility_plan(
                text,
                task_id=task_id,
                intent_type=IntentType.MEMORY_UPDATE,
                outputs=["memory"],
                mode="direct",
                rationale=rationale,
            )
            plan.capability_inputs = {
                "memory.update": dict(proposal.capability_inputs.get("memory.update") or {})
            }
            return plan

        # In-place edit of an existing figure. The runtime grounds the edit
        # target against the active artifact and runs the deterministic patch;
        # if there is no active figure or the target cannot be grounded, this
        # plan degrades to the capable assistant instead of dead-ending.
        if CAPABILITY_ARTIFACT_REVISE in all_caps:
            plan = self._artifact_revise_plan(
                text,
                task_id=task_id,
                rationale=rationale,
                confidence=proposal.confidence or 0.75,
            )
            plan.capability_inputs = {
                CAPABILITY_ARTIFACT_REVISE: dict(
                    proposal.capability_inputs.get(CAPABILITY_ARTIFACT_REVISE) or {}
                )
            }
            return plan

        if proposal.intent_type == "workflow" or proposal.workflow_steps:
            specs = proposal.workflow_steps or steps_from_capabilities(caps, text)
            plan = self._workflow_builder.from_specs(
                text,
                specs,
                task_id=task_id,
                rationale=rationale,
                confidence=proposal.confidence or 0.7,
                provenance_mode=proposal.provenance_mode,
                outputs=proposal.outputs,
                capability_inputs=proposal.capability_inputs,
            )
            if plan.intent_type == IntentType.WORKFLOW:
                plan.capability_inputs = {
                    str(capability): dict(value)
                    for capability, value in proposal.capability_inputs.items()
                    if isinstance(value, dict)
                }
            return plan
        if proposal.intent_type == "qa_plus_artifact" or (wants_artifact and wants_answer):
            plan = self._qa_plus_artifact(text, task_id=task_id)
            plan.confidence = proposal.confidence or plan.confidence
            plan.rationale = rationale
            plan.provenance_mode = proposal.provenance_mode
            plan.capability_inputs = {
                CAPABILITY_FIGURE: dict(proposal.capability_inputs.get(CAPABILITY_FIGURE) or {})
            }
            return plan
        if caps:
            capability = caps[0]
            outputs = proposal.outputs or deliverables_from_capabilities([capability])
            mode = proposal.execution_mode if proposal.execution_mode in {"background", "foreground"} else "background"
            plan = self._single_capability(
                text,
                task_id=task_id,
                capability=capability,
                reason=rationale,
                outputs=outputs,
                mode=mode,
                confidence=proposal.confidence or 0.72,
            )
            plan.capability_inputs = {
                capability: dict(proposal.capability_inputs.get(capability) or {})
            }
            return plan
        if proposal.intent_type == "direct_answer":
            return self._assistant_plan(
                text,
                task_id=task_id,
                rationale=rationale,
                confidence=proposal.confidence or 0.6,
                intent_type=IntentType.DIRECT_ANSWER,
            )
        return self._assistant_plan(
            text,
            task_id=task_id,
            rationale=rationale,
            confidence=proposal.confidence or 0.4,
        )

    def _artifact_revise_plan(
        self,
        text: str,
        *,
        task_id: str,
        rationale: str,
        confidence: float,
    ) -> IntentPlan:
        """Model asked to edit the active figure in place.

        The plan carries the ``artifact.revise`` capability as a marker; the
        orchestrator dispatches it to the deterministic, contract-validated
        artifact patcher (which escalates to a full redraw when the target
        cannot be grounded). When there is no active figure to edit, the plan
        stays a capable ReAct turn instead of failing.
        """
        plan = self._assistant_plan(
            text,
            task_id=task_id,
            rationale=rationale or "model requested an in-place edit of the active figure",
            confidence=confidence,
        )
        plan.outputs = [CAPABILITY_ARTIFACT_REVISE, "answer"]
        plan.acceptance = ["artifact_revision_or_capable_react"]
        plan.verification_plan = VerificationPlan(
            required_outputs=["answer"],
            required_events=["plan.executed"],
            presentation_checks=["show_partial_when_budget_exhausted"],
        )
        return plan

    def capable_assistant_plan(self, text: str, *, task_id: str, rationale: str) -> IntentPlan:
        """Public entry to the capable, safety-bounded ReAct plan.

        The recovery ladder uses this as its floor (Rung 4): when a deterministic
        plan cannot run for a non-safety reason, the turn is handed to the same
        capable assistant every other agent uses as its default, instead of dead
        ending. Tool policy still blocks irreversible mutations.
        """
        return self._assistant_plan(text, task_id=task_id, rationale=rationale)

    @property
    def workflow_builder(self) -> WorkflowPlanBuilder:
        return self._workflow_builder

    def _plan_for_boundary(self, text: str, decision: BoundaryDecision, *, task_id: str) -> IntentPlan:
        if decision.kind == "explicit_skill":
            explicit = self._explicit_skill_plan(text, task_id=task_id)
            if explicit is not None:
                return explicit
        # A gate matched but could not be materialised (e.g. explicit skill not
        # found) — hand the turn to the capable model/react path.
        return self._assistant_plan(text, task_id=task_id, rationale=decision.reason or "boundary fallthrough")

    def _explicit_skill_plan(self, text: str, *, task_id: str) -> IntentPlan | None:
        skill_name, skill_source = explicit_skill_ref(text, self._registry)
        if not skill_name:
            return None
        entry = self._registry.resolve_ref(skill_name, skill_source)
        if entry is None:
            return None
        if entry.is_disabled:
            return needs_input_plan(
                text,
                task_id=task_id,
                missing_inputs=[{"field": "skill", "reason": f"explicitly requested skill '{skill_name}' is disabled"}],
                rationale="explicit skill request cannot run because the skill is disabled",
                confidence=0.92,
            )
        selection = self._arbitrator.select_explicit(
            skill_name,
            reason=f"user explicitly requested skill '{skill_name}'",
            confidence=0.99,
            source=skill_source,
        )
        if selection is None:
            return None
        warnings: list[str] = []
        if entry.is_deprecated:
            replacement = f"; use `{entry.replaced_by}` instead" if entry.replaced_by else ""
            warnings.append(f"Explicitly invoked deprecated skill `{skill_name}`{replacement}.")
        return IntentPlan(
            task_id=task_id,
            user_message=text,
            intent_type=IntentType.SINGLE_SKILL_TASK,
            confidence=0.99,
            outputs=["answer"],
            selected_skills=[selection],
            execution_mode="background" if entry.is_async else "foreground",
            provenance_mode="light",
            context_policy=ContextPolicy(include_memory=True, include_referenced_tasks=True),
            tool_policy=ToolPolicy(final_reserve_enabled=True, max_tool_calls=1),
            acceptance=["explicit_skill_respected", "skill_task_created_or_completed"],
            verification_plan=VerificationPlan(
                required_outputs=["answer"],
                required_events=["subtask.submitted", "plan.executed"],
                required_tasks=[skill_name],
                presentation_checks=["show_plan_reason", "show_task_id", "show_next_actions"],
            ),
            degraded_warnings=warnings,
            rationale=f"explicit skill invocation has priority over capability and model routing: {skill_name}",
        )

    def _assistant_plan(
        self,
        text: str,
        *,
        task_id: str,
        rationale: str,
        confidence: float = 0.5,
        intent_type: IntentType = IntentType.REACT_FALLBACK,
    ) -> IntentPlan:
        """Capable, safety-bounded ReAct turn (see ``plan_factory``)."""
        return build_assistant_plan(
            text,
            task_id=task_id,
            rationale=rationale,
            confidence=confidence,
            intent_type=intent_type,
        )

    def _utility_plan(
        self,
        text: str,
        *,
        task_id: str,
        intent_type: IntentType,
        outputs: list[str],
        mode: str,
        rationale: str,
    ) -> IntentPlan:
        return IntentPlan(
            task_id=task_id,
            user_message=text,
            intent_type=intent_type,
            confidence=0.86,
            outputs=outputs,
            execution_mode=mode,
            provenance_mode="light",
            context_policy=ContextPolicy(
                include_recent_activity=False,
                include_memory=intent_type != IntentType.MEMORY_UPDATE,
                include_referenced_tasks=False,
            ),
            tool_policy=ToolPolicy(allowed_tools=[], final_reserve_enabled=True, max_tool_calls=0, max_iterations=0),
            acceptance=["deterministic_no_react"],
            verification_plan=VerificationPlan(
                required_outputs=outputs,
                required_events=["plan.executed"],
            ),
            rationale=rationale,
        )

    def _qa_plus_artifact(self, text: str, *, task_id: str) -> IntentPlan:
        selection = self._arbitrator.select_capability(
            CAPABILITY_FIGURE,
            message=text,
            reason="model requested an artifact.figure deliverable",
            confidence=0.94,
        )
        required_tasks = [selection.skill] if selection else [CAPABILITY_FIGURE]
        return IntentPlan(
            task_id=task_id,
            user_message=text,
            intent_type=IntentType.QA_PLUS_ARTIFACT,
            confidence=0.94,
            outputs=["answer", "artifact"],
            selected_skills=[selection] if selection else [],
            execution_mode="background",
            provenance_mode="light",
            context_policy=ContextPolicy(include_memory=True, include_referenced_tasks=True),
            tool_policy=ToolPolicy(
                allowed_tools=["search_corpus"],
                blocked_tools=[
                    "glob",
                    "open_artifact",
                    "list_session_artifacts",
                    "find_skill",
                    "use_skill",
                    "run_skill",
                    "run_workflow",
                    "record_claim",
                    "add_evidence",
                ],
                per_tool_limits={"search_corpus": 1},
                max_tool_calls=2,
                max_iterations=2,
                final_reserve_enabled=True,
            ),
            acceptance=["no_forbidden_tools", "qa_answer_delivered", "artifact_figure_child_task_created"],
            verification_plan=VerificationPlan(
                required_outputs=["answer", "artifact"],
                required_events=["plan.tool.done", "subtask.submitted", "plan.executed"],
                forbidden_tools=["glob", "open_artifact", "list_session_artifacts", "record_claim", "add_evidence"],
                required_tasks=required_tasks,
                artifact_checks=["child_task_has_artifact_contract"],
                provenance_checks=["light_or_full_as_requested"],
                presentation_checks=[
                    "show_plan_reason", "show_task_id", "show_next_actions", "presentation_sent_or_degraded",
                ],
            ),
            rationale="model requested answer plus artifact.figure",
        )

    def _single_capability(
        self,
        text: str,
        *,
        task_id: str,
        capability: str,
        reason: str,
        outputs: list[str],
        mode: str,
        confidence: float,
    ) -> IntentPlan:
        selection = self._arbitrator.select_capability(capability, message=text, reason=reason, confidence=confidence)
        blocked = ["glob", "list_session_artifacts", "open_artifact", "find_skill"]
        required_tasks = [selection.skill] if selection else [capability]
        return IntentPlan(
            task_id=task_id,
            user_message=text,
            intent_type=IntentType.SINGLE_SKILL_TASK,
            confidence=confidence,
            outputs=outputs,
            selected_skills=[selection] if selection else [],
            execution_mode=mode,
            provenance_mode="light",
            context_policy=ContextPolicy(include_memory=True, include_referenced_tasks=True),
            tool_policy=ToolPolicy(allowed_tools=[], blocked_tools=blocked, max_tool_calls=1, final_reserve_enabled=True),
            acceptance=["skill_task_created_or_completed", "plan_events_recorded"],
            verification_plan=VerificationPlan(
                required_outputs=outputs,
                required_events=["subtask.submitted", "plan.executed"],
                forbidden_tools=blocked,
                required_tasks=required_tasks,
                artifact_checks=["child_task_has_artifact_contract"] if "artifact" in outputs else [],
                provenance_checks=["light_or_full_as_requested"],
                presentation_checks=[
                    "show_plan_reason", "show_task_id", "show_next_actions", "presentation_sent_or_degraded",
                ],
            ),
            rationale=reason,
        )


def _schedule_deferred_goal(
    proposal: ModelPlanProposal,
    fallback: str,
) -> str:
    """Return future work as intent only; provider binding happens when due."""
    for key in ("schedule", "schedule.task"):
        raw = proposal.capability_inputs.get(key)
        if not isinstance(raw, dict):
            continue
        task = raw.get("task")
        if isinstance(task, dict):
            goal = str(task.get("goal") or "").strip()
            if goal:
                return goal
        goal = str(raw.get("goal") or "").strip()
        if goal:
            return goal
    return fallback
