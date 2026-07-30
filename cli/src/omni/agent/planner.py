"""Contract-driven intent planning boundary.

This module is intentionally small. It owns deterministic boundaries
(commands, explicit skills, safety/no-tool cases, vague references) and turns
semantic model proposals into capability/deliverable plans. It does not encode
domain workflows or concrete skill names for automatic routing.
"""

from __future__ import annotations

from dataclasses import replace

from omni.agent.boundary_router import (
    BoundaryDecision,
    BoundaryRouter,
    explicit_skill_arguments,
    explicit_skill_ref,
)
from omni.agent.capabilities import (
    CAPABILITY_ARTIFACT_REVISE,
    CAPABILITY_FIGURE,
    CAPABILITY_GROUNDED_QA,
    CAPABILITY_LITERATURE_SEARCH,
    CAPABILITY_SYNTHESIS_FINAL,
    CAPABILITY_TASK_INSPECT,
    CAPABILITY_TASK_REVIEW,
    contract_outputs,
    contract_outputs_from_capabilities,
    deliverables_from_capabilities,
    is_survey_pair,
)
from omni.agent.intent_plan import (
    ContextPolicy,
    IntentPlan,
    IntentType,
    ToolPolicy,
    VerificationPlan,
)
from omni.agent.model_planner import ModelPlanProposal, has_refused_value
from omni.agent.plan_factory import (
    build_assistant_plan,
    build_schedule_plan,
    build_task_inspect_plan,
    build_task_review_plan,
    needs_input_plan,
)
from omni.agent.plan_runner_utils import gap_default, gap_question
from omni.agent.skill_arbitrator import SkillArbitrator
from omni.runtime.remaining import bind_contract_outputs, infer_figure_and_paper_outputs
from omni.skills_runtime.registry import SkillRegistry

# The gap stated when the model asks without naming what it needs. ``ask`` is
# the sentence the user reads; ``reason`` is the diagnostic the event log keeps.
_UNRESOLVED_REQUEST_GAP = {
    "field": "request",
    "ask": "what you would like me to do with this",
    "reason": "model planner could not resolve a safe executable intent",
}


class IntentPlanner:
    """Produce a typed runtime plan from boundary decisions or semantic proposals."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry
        self._boundary = BoundaryRouter(registry)
        self._arbitrator = SkillArbitrator(registry)

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
        return bind_contract_outputs(
            build_assistant_plan(
                user_message or "",
                task_id=task_id,
                rationale="semantic planner unavailable; using capable bounded assistant",
            )
        )

    def plan_from_proposal(
        self,
        user_message: str,
        proposal: ModelPlanProposal,
        *,
        task_id: str = "",
    ) -> IntentPlan:
        """Build an executable plan from a capability-level model proposal."""
        return bind_contract_outputs(
            self._plan_from_proposal(user_message, proposal, task_id=task_id),
            proposal=proposal,
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
                schedule_proposal=_schedule_action_proposal(proposal),
                provenance_mode=proposal.provenance_mode,
            )
        # Ask-last: "ask the user" is an intent the model chooses, not a veto the
        # planning boundary derives from advisory metadata. We short-circuit to a
        # question only when the model *explicitly* chose to ask — via
        # ``needs_input`` or ``execution_mode: "ask"`` — or when it reported gaps
        # and left no steps to run. With steps, the capable turn can look the
        # value up, so a discoverable gap never becomes a question.
        # ``ask`` was previously folded into ``background`` by the single-capability
        # route, so a model that hesitated was overruled into an expensive run
        # (incident dc787efa).
        #
        # A *refused* value is a different door: a gap the model reported may be
        # discoverable, but a value the grounding gate removed is discoverable by
        # no one, and continuing without it is how run 138c7b6e reviewed a paper
        # from a file that was never fetched.
        #
        # A gap that says nothing is not a door at all: a sentence naming
        # neither what is needed nor why cannot be answered, and the capable
        # turn the plan already nominates is a better use of the user's time.
        # A gap the model gave a default for is one it has already decided; the
        # turn runs on that value and says so. Stopping is reserved for gaps
        # nobody can guess, because stopping here is not a pause: the task goes
        # terminal and waits for a person, and not one of thirty such turns was
        # ever answered. A refused value is exempt — the gate removed it because
        # no value was trustworthy, so a default would re-fabricate what it just
        # deleted. Both tests below judge the gaps the model *named*, so neither
        # may veto a model that named none: hesitating without naming a gap is
        # exactly what ``ask`` is for.
        named = bool(proposal.missing_inputs)
        speakable = [gap for gap in proposal.missing_inputs if gap_question(gap)]
        refused = has_refused_value(proposal.missing_inputs)
        unguessable = [gap for gap in proposal.missing_inputs if not gap_default(gap)]
        chose_to_ask = (
            proposal.intent_type == "needs_input"
            or proposal.execution_mode == "ask"
            or refused or (named and not proposal.workflow_steps)
        )
        if chose_to_ask and (speakable or not named) and (unguessable or refused or not named):
            # Ask about what has no answer yet, not about what was already
            # decided: a question the turn could have answered itself spends the
            # user's attention and buys nothing.
            asked = [gap for gap in speakable if not gap_default(gap)] if not refused else speakable
            return needs_input_plan(
                text,
                task_id=task_id,
                missing_inputs=asked or speakable or [_UNRESOLVED_REQUEST_GAP],
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
            return _carry_inputs(plan, proposal, "memory.update")

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
            return _carry_inputs(plan, proposal, CAPABILITY_ARTIFACT_REVISE)

        # A multi-task / time-window retrospective is broader than a single-task
        # status lookup, so it is matched first: it keeps the model's narrative and
        # only appends an authoritative status footer, whereas task.inspect projects
        # one task's status deterministically.
        if CAPABILITY_TASK_REVIEW in all_caps:
            plan = build_task_review_plan(
                text,
                task_id=task_id,
                rationale=rationale,
                confidence=proposal.confidence or 0.8,
            )
            return _carry_inputs(plan, proposal, CAPABILITY_TASK_REVIEW)

        if CAPABILITY_TASK_INSPECT in all_caps:
            plan = build_task_inspect_plan(
                text,
                task_id=task_id,
                rationale=rationale,
                confidence=proposal.confidence or 0.8,
            )
            return _carry_inputs(plan, proposal, CAPABILITY_TASK_INSPECT)

        figure_and_paper = bool(infer_figure_and_paper_outputs(text))
        if is_survey_pair(all_caps, proposal.outputs) and not figure_and_paper:
            # Codex keeps the produce path on the critical path (apply_patch).
            # Omni's produce path for a written survey is host retrieval plus
            # native synthesis. Demoting this pair to ReAct made orientation
            # look like a stall and left the manuscript unwritten.
            return self._survey_plan(
                text,
                task_id=task_id,
                rationale=rationale,
                confidence=proposal.confidence or 0.86,
                proposal=proposal,
            )
        if figure_and_paper:
            # The user named both a figure and a paper. A literature+write
            # proposal must not collapse to the survey closer and drop the
            # figure; sequence the independent deliverables live.
            plan = build_assistant_plan(
                text,
                task_id=task_id,
                rationale=rationale
                or "figure and manuscript sequenced by the model against live results",
            )
            plan.confidence = proposal.confidence or plan.confidence
            plan.provenance_mode = proposal.provenance_mode
            plan.outputs = list(proposal.outputs) or plan.outputs
            return _carry_survey_retrieve(plan, proposal, text)

        host_capability = _single_host_capability(proposal, all_caps, self._registry)
        if (proposal.intent_type == "workflow" or proposal.workflow_steps) and not host_capability:
            # Multi-step work is the model's to sequence, not the host's to seal.
            # A DAG fixed here — before a single tool has run — is a guess that
            # must then be validated, repaired and recovered when the first step
            # returns something the guess did not anticipate. The capable turn
            # instead hands the model run_skill / run_workflow / spawn_subagents
            # and update_plan, so it orders the work against what it actually
            # observes and revises when reality disagrees (Codex's model).
            # A lone contracted capability labelled "workflow" is not multi-step:
            # demoting it to ReAct hid the provider contract and burned the turn
            # exploring find_skill / docs_search (BUG-11).
            plan = build_assistant_plan(
                text,
                task_id=task_id,
                rationale=rationale or "multi-step work sequenced by the model against live results",
            )
            plan.confidence = proposal.confidence or plan.confidence
            plan.provenance_mode = proposal.provenance_mode
            plan.outputs = list(proposal.outputs) or plan.outputs
            # The proposal's per-capability inputs are deliberately dropped: they
            # existed to seed steps of a plan-time DAG. The model now passes its
            # own arguments in the run_workflow call it makes, and carrying a
            # second, staler copy on the plan only invites the two to disagree.
            # literature.search is the exception: the host survey closer still
            # needs the query after this demote, or a ReAct turn that only
            # looked up memory never retrieves.
            return _carry_survey_retrieve(plan, proposal, text)
        if _live_sequence_required(proposal, all_caps, self._registry):
            # A single-skill route would drop every capability after the first,
            # or spend a figure/manuscript debt that the chosen skill cannot
            # emit. Sequence those turns live (Codex): literature failing then
            # cannot make the independent deliverables 0/1.
            plan = build_assistant_plan(
                text,
                task_id=task_id,
                rationale=rationale
                or "requested work spans more than one provider; sequence against live results",
            )
            plan.confidence = proposal.confidence or plan.confidence
            plan.provenance_mode = proposal.provenance_mode
            plan.outputs = list(proposal.outputs) or plan.outputs
            return _carry_survey_retrieve(plan, proposal, text)
        if proposal.intent_type == "qa_plus_artifact" or (wants_artifact and wants_answer):
            plan = self._qa_plus_artifact(text, task_id=task_id)
            plan.confidence = proposal.confidence or plan.confidence
            plan.rationale = rationale
            plan.provenance_mode = proposal.provenance_mode
            return _carry_inputs(plan, proposal, CAPABILITY_FIGURE)
        if host_capability or caps:
            capability = host_capability or caps[0]
            proposal = _lift_lone_step_inputs(proposal, capability)
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
            return _carry_inputs(plan, proposal, capability)
        if proposal.intent_type == "direct_answer":
            return build_assistant_plan(
                text,
                task_id=task_id,
                rationale=rationale,
                confidence=proposal.confidence or 0.6,
                intent_type=IntentType.DIRECT_ANSWER,
            )
        return build_assistant_plan(
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
        plan = build_assistant_plan(
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
        )
        return plan

    def capable_assistant_plan(self, text: str, *, task_id: str, rationale: str) -> IntentPlan:
        """Public entry to the capable, safety-bounded ReAct plan.

        The recovery ladder uses this as its floor (Rung 4): when a deterministic
        plan cannot run for a non-safety reason, the turn is handed to the same
        capable assistant every other agent uses as its default, instead of dead
        ending. Tool policy still blocks irreversible mutations.
        """
        return bind_contract_outputs(build_assistant_plan(text, task_id=task_id, rationale=rationale))

    def _plan_for_boundary(self, text: str, decision: BoundaryDecision, *, task_id: str) -> IntentPlan:
        if decision.kind == "explicit_skill":
            explicit = self._explicit_skill_plan(text, task_id=task_id)
            if explicit is not None:
                return bind_contract_outputs(explicit)
        # A gate matched but could not be materialised (e.g. explicit skill not
        # found) — hand the turn to the capable model/react path.
        return bind_contract_outputs(
            build_assistant_plan(text, task_id=task_id, rationale=decision.reason or "boundary fallthrough")
        )

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
                missing_inputs=[
                    {
                        "field": "skill",
                        "reason": f"explicitly requested skill '{skill_name}' is disabled",
                        "ask": (
                            f"the skill '{skill_name}' is disabled — enable it with "
                            f"`omni skills enable {skill_name}`, or name a different skill"
                        ),
                    }
                ],
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
        plan = IntentPlan(
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
            ),
            degraded_warnings=warnings,
            rationale=f"explicit skill invocation has priority over capability and model routing: {skill_name}",
        )
        arguments = explicit_skill_arguments(text, self._registry)
        if arguments:
            capability = (
                selection.matched_capabilities[0]
                if selection.matched_capabilities
                else f"skill:{skill_name}"
            )
            plan.capability_inputs = {capability: arguments}
        return plan

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

    def _survey_plan(
        self,
        text: str,
        *,
        task_id: str,
        rationale: str,
        confidence: float,
        proposal: ModelPlanProposal,
    ) -> IntentPlan:
        """Host-owned literature search; the closer writes the manuscript.

        ``synthesis.final`` is not a second selected skill. Native synthesis
        runs after the search drains, on this ``task_id``.
        """
        proposal = _lift_lone_step_inputs(proposal, CAPABILITY_LITERATURE_SEARCH)
        lit_input = dict(proposal.capability_inputs.get(CAPABILITY_LITERATURE_SEARCH) or {})
        if not str(lit_input.get("query") or lit_input.get("topic") or "").strip():
            lit_input["query"] = text
            proposal.capability_inputs[CAPABILITY_LITERATURE_SEARCH] = lit_input
        outputs = list(
            dict.fromkeys(
                [
                    *(proposal.outputs or []),
                    *deliverables_from_capabilities(
                        [CAPABILITY_LITERATURE_SEARCH, CAPABILITY_SYNTHESIS_FINAL]
                    ),
                ]
            )
        )
        mode = (
            proposal.execution_mode
            if proposal.execution_mode in {"background", "foreground"}
            else "background"
        )
        plan = self._single_capability(
            text,
            task_id=task_id,
            capability=CAPABILITY_LITERATURE_SEARCH,
            reason=rationale or "written survey: host retrieves then synthesizes",
            outputs=outputs,
            mode=mode,
            confidence=confidence,
        )
        return _carry_inputs(plan, proposal, CAPABILITY_LITERATURE_SEARCH)

    def _qa_plus_artifact(self, text: str, *, task_id: str) -> IntentPlan:
        selection = self._arbitrator.select_capability(
            CAPABILITY_FIGURE,
            message=text,
            reason="model requested an artifact.figure deliverable",
            confidence=0.94,
        )
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
            acceptance=["qa_answer_delivered", "artifact_figure_child_task_created"],
            verification_plan=VerificationPlan(
                required_outputs=["answer", "artifact"],
                required_events=["plan.tool.done", "subtask.submitted", "plan.executed"],
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
            ),
            rationale=reason,
        )


def _qa_figure_pair(capabilities: list[str]) -> bool:
    """The one multi-capability shape that already has a dedicated runner."""
    named = {item for item in capabilities if item}
    return named == {CAPABILITY_GROUNDED_QA, CAPABILITY_FIGURE}


def _single_host_capability(
    proposal: ModelPlanProposal,
    all_caps: list[str],
    registry: SkillRegistry,
) -> str:
    """Return the one capability a host runner can seal without dropping work.

    A model that labels a lone ``slides.generate`` step as ``workflow`` still
    has one contracted provider. Sending that turn to ReAct hides the schema
    and restarts parameter exploration. Genuine multi-provider or uncovered
    deliverable work stays on the live sequence.
    """
    if _live_sequence_required(proposal, all_caps, registry):
        return ""
    unique = [item for item in dict.fromkeys(all_caps) if item]
    if len(unique) != 1:
        return ""
    steps = [
        step
        for step in (proposal.workflow_steps or [])
        if isinstance(step, dict)
        and (step.get("capability") or step.get("skill") or step.get("skill_name"))
    ]
    if len(steps) > 1:
        return ""
    return unique[0]


def _lift_lone_step_inputs(proposal: ModelPlanProposal, capability: str) -> ModelPlanProposal:
    """Copy a lone workflow step's input onto ``capability_inputs`` when empty."""
    if proposal.capability_inputs.get(capability):
        return proposal
    for step in proposal.workflow_steps or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("capability") or "") != capability:
            continue
        raw = step.get("input") if isinstance(step.get("input"), dict) else step.get("parameters")
        if isinstance(raw, dict) and raw:
            return replace(
                proposal,
                capability_inputs={**proposal.capability_inputs, capability: dict(raw)},
            )
    return proposal


def _live_sequence_required(
    proposal: ModelPlanProposal,
    all_caps: list[str],
    registry: SkillRegistry,
) -> bool:
    """Whether a single-skill route would drop independent requested work."""
    unique = list(dict.fromkeys(item for item in all_caps if item))
    if len(unique) > 1:
        return not _qa_figure_pair(unique) and not is_survey_pair(unique)
    if len(unique) != 1:
        return False
    capability = unique[0]
    requested = set(contract_outputs(list(proposal.outputs or [])))
    covered = set(contract_outputs_from_capabilities([capability]))
    entry, _rejected = registry.resolve_capability(capability)
    if entry is not None:
        covered.update(contract_outputs(list(entry.deliverables or [])))
        covered.update(contract_outputs_from_capabilities(list(entry.capabilities or [])))
    if "artifact" in (proposal.outputs or []) and not covered:
        requested.add(CAPABILITY_FIGURE)
    return bool(requested - covered)


def _carry_survey_retrieve(
    plan: IntentPlan, proposal: ModelPlanProposal, text: str
) -> IntentPlan:
    """Keep literature.search inputs when a workflow is demoted to ReAct.

    The host closer resolves the skill from the registry if the plan has no
    selection. It still needs a query. Copy the proposal's, or the user text.
    """
    caps = [str(item) for item in (proposal.required_capabilities or []) if item]
    raw = proposal.capability_inputs.get(CAPABILITY_LITERATURE_SEARCH)
    if isinstance(raw, dict) and raw:
        caps.append(CAPABILITY_LITERATURE_SEARCH)
    else:
        raw = None
        for step in proposal.workflow_steps or []:
            if not isinstance(step, dict):
                continue
            if str(step.get("capability") or "") != CAPABILITY_LITERATURE_SEARCH:
                continue
            candidate = (
                step.get("input") if isinstance(step.get("input"), dict) else step.get("parameters")
            )
            if isinstance(candidate, dict) and candidate:
                raw = dict(candidate)
                caps.append(CAPABILITY_LITERATURE_SEARCH)
                break
    if CAPABILITY_LITERATURE_SEARCH not in caps:
        return plan
    inputs = dict(plan.capability_inputs or {})
    existing = inputs.get(CAPABILITY_LITERATURE_SEARCH)
    if isinstance(existing, dict) and str(
        existing.get("query") or existing.get("topic") or ""
    ).strip():
        return plan
    payload = dict(raw) if isinstance(raw, dict) and raw else {}
    if not str(payload.get("query") or payload.get("topic") or "").strip():
        payload["query"] = text
    inputs[CAPABILITY_LITERATURE_SEARCH] = payload
    plan.capability_inputs = inputs
    return plan


def _carry_inputs(plan: IntentPlan, proposal: ModelPlanProposal, capability: str) -> IntentPlan:
    """Forward only the proposal inputs belonging to the capability being planned.

    Copied rather than aliased, and narrowed to the one capability, so a plan
    cannot execute with arguments the model proposed for a different step.
    """
    plan.capability_inputs = {capability: dict(proposal.capability_inputs.get(capability) or {})}
    return plan


def _schedule_deferred_goal(
    proposal: ModelPlanProposal,
    fallback: str,
) -> str:
    """Return future work as intent only; provider binding happens when due."""
    normalized = _schedule_action_proposal(proposal)
    return str(normalized.get("goal") or "").strip() or fallback


def _schedule_action_proposal(proposal: ModelPlanProposal) -> dict[str, object]:
    """Freeze only declared schedule-create fields from the planner proposal."""
    resolved: dict[str, object] = {}
    for key in ("schedule", "schedule.task"):
        raw = proposal.capability_inputs.get(key)
        if not isinstance(raw, dict):
            continue
        task = raw.get("task")
        root_goal = str(raw.get("goal") or "").strip()
        task_goal = (
            str(task.get("goal") or "").strip() if isinstance(task, dict) else ""
        )
        if root_goal and task_goal and root_goal != task_goal:
            return {}
        goal = task_goal or root_goal
        if not goal:
            return {}
        out: dict[str, object] = {"goal": goal}
        for field in ("title", "when", "cron", "every_seconds", "at", "timezone"):
            root_value = raw.get(field)
            task_value = task.get(field) if isinstance(task, dict) else None
            if root_value is not None and task_value is not None and root_value != task_value:
                return {}
            if root_value is not None:
                out[field] = root_value
            elif task_value is not None:
                out[field] = task_value
        if resolved and out != resolved:
            return {}
        resolved = out
    return resolved
