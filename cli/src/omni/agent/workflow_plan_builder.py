"""Build workflow IntentPlans from capability specs.

Semantic planning may propose capabilities and deliverables.  This builder is
the execution-planning seam that resolves those capability slots to providers
and materializes the workflow DAG.  It does not classify user intent.
"""

from __future__ import annotations

from omni.agent.capabilities import (
    CAPABILITY_SYNTHESIS_FINAL,
    DELIVERABLE_DRAFT_SECTION,
    WORKFLOW_CAPABILITIES,
    capabilities_from_steps,
    deliverables_from_capabilities,
    is_native_synthesis_capability,
)
from omni.agent.intent_plan import (
    ContextPolicy,
    IntentPlan,
    IntentType,
    SkillSelection,
    ToolPolicy,
    VerificationPlan,
)
from omni.agent.provider_binding import materialize_provider_bindings
from omni.agent.skill_arbitrator import SkillArbitrator
from omni.agent.task_contract import (
    bind_task_contract_providers,
    build_task_contract,
    build_workflow_dag,
    provider_quality_checks,
)
from omni.skills_runtime.registry import resolve_step_entry


class WorkflowPlanBuilder:
    """Create executable workflow plans after semantic intent is known."""

    def __init__(self, arbitrator: SkillArbitrator) -> None:
        self._arbitrator = arbitrator

    def from_specs(
        self,
        text: str,
        specs: list[dict[str, object]],
        *,
        task_id: str,
        rationale: str,
        confidence: float,
        provenance_mode: str,
        outputs: list[str] | None = None,
        capability_inputs: dict[str, dict[str, object]] | None = None,
    ) -> IntentPlan:
        workflow_steps, selections = self._resolve_steps(specs, text, capability_inputs or {})
        if not workflow_steps:
            return needs_input_plan(
                text,
                task_id=task_id,
                missing_inputs=[
                    {
                        "field": "workflow_steps",
                        "reason": "no executable contracted provider matched the proposed capabilities",
                    }
                ],
                rationale=rationale,
                confidence=0.62,
            )
        deliverables = list(outputs or [])
        if not deliverables or deliverables == ["workflow"]:
            deliverables = deliverables_from_capabilities(capabilities_from_steps(workflow_steps))
        capabilities = capabilities_from_steps(workflow_steps)
        required_skill_executions = [
            str(step.get("skill_name") or step.get("skill") or "")
            for step in workflow_steps
            if str(step.get("provider_type") or "skill") == "skill"
            and bool(step.get("required", True))
            and str(step.get("skill_name") or step.get("skill") or "")
        ]
        contract = build_task_contract(
            objective=text,
            deliverables=deliverables,
            capabilities=capabilities,
            workflow_steps=[dict(step) for step in workflow_steps],
            provenance_mode=provenance_mode,
            confidence=confidence,
        )
        dag = build_workflow_dag([dict(step) for step in workflow_steps])
        plan = IntentPlan(
            task_id=task_id,
            user_message=text,
            intent_type=IntentType.WORKFLOW,
            confidence=confidence,
            outputs=deliverables,
            selected_skills=selections,
            execution_mode="background",
            provenance_mode=provenance_mode if provenance_mode in {"light", "full"} else "light",
            context_policy=ContextPolicy(
                include_research_brief=True,
                include_skill_catalog=True,
                include_memory=True,
                include_referenced_tasks=True,
            ),
            tool_policy=ToolPolicy(final_reserve_enabled=True, max_tool_calls=4),
            workflow_steps=workflow_steps,
            task_contract=contract,
            workflow_dag=dag,
            verification_plan=VerificationPlan(
                required_outputs=deliverables,
                required_events=["workflow.submitted", "plan.executed"],
                required_tasks=required_skill_executions,
                # Scientific deliverables are verified by default: a figure/artifact
                # deliverable must render an artifact, provenance is checked to the
                # requested level, and IM delivery must be sent/degraded.
                artifact_checks=["artifact_emitted"] if _has_artifact_deliverable(deliverables + capabilities) else [],
                provenance_checks=["light_or_full_as_requested"],
                presentation_checks=["show_plan_reason", "show_next_actions", "presentation_sent_or_degraded"],
                deliverable_checks=[],
            ),
            acceptance=["workflow_task_created_or_needs_input", "plan_events_recorded"],
            rationale=rationale,
        )
        materialize_provider_bindings(plan, self._arbitrator.registry)
        plan.task_contract = bind_task_contract_providers(
            plan.task_contract,
            plan.workflow_steps,
        )
        plan.verification_plan.deliverable_checks = provider_quality_checks(
            plan.workflow_steps
        )
        return plan

    def _resolve_steps(
        self,
        specs: list[dict[str, object]],
        message: str,
        capability_inputs: dict[str, dict[str, object]],
    ) -> tuple[list[dict[str, object]], list[SkillSelection]]:
        resolved: list[dict[str, object]] = []
        selections: list[SkillSelection] = []
        resolved_ids: set[str] = set()
        for spec in specs:
            capability = str(spec.get("capability") or "")
            step_id = str(spec.get("id") or "")
            if not capability or not step_id:
                continue
            if is_native_synthesis_capability(capability):
                depends_on = [str(dep) for dep in spec.get("depends_on") or [] if str(dep) in resolved_ids]
                step = {
                    **spec,
                    "skill_name": CAPABILITY_SYNTHESIS_FINAL,
                    "skill": CAPABILITY_SYNTHESIS_FINAL,
                    "capability": CAPABILITY_SYNTHESIS_FINAL,
                    "provider_type": "native_executor",
                    "deliverable": spec.get("deliverable") or DELIVERABLE_DRAFT_SECTION,
                    "depends_on": depends_on,
                    "allow_failed_dependencies": True,
                    "failure_policy": "continue_with_partial",
                }
                resolved.append(step)
                resolved_ids.add(step_id)
                continue
            selection = self._arbitrator.select_capability(
                capability,
                message=message,
                reason=f"workflow step {step_id}: {spec.get('reason') or capability}",
                confidence=0.76,
            )
            if selection is None:
                continue
            depends_on = [str(dep) for dep in spec.get("depends_on") or [] if str(dep) in resolved_ids]
            entry = resolve_step_entry(
                self._arbitrator.registry,
                {
                    "skill_name": selection.skill,
                    "skill_source": selection.skill_source,
                },
            )
            if entry is None:
                continue
            selection.skill_source = str(getattr(entry, "source", "") or "")
            composed, bound_from_goal = _compose_step_input(
                entry, capability_inputs.get(capability), spec.get("input"), message
            )
            step = {
                **spec,
                "skill_name": selection.skill,
                "skill": selection.skill,
                "skill_source": selection.skill_source,
                "capability": capability,
                "depends_on": depends_on,
                "input": composed,
            }
            if bound_from_goal:
                # Audit trail: this step's instruction slot was bound from the
                # user goal at plan time (persisted with the workflow step).
                step["normalization_reason"] = "input_bound_from_goal"
            resolved.append(step)
            selections.append(selection)
            resolved_ids.add(step_id)
        return resolved, selections


def _compose_step_input(
    entry: object | None,
    capability_input: dict[str, object] | None,
    step_input: object,
    goal: str,
) -> tuple[dict[str, object], bool]:
    """Fill a workflow step's provider input at planning time (explicit, once).

    The semantic planner emits contract-declared per-capability inputs (e.g. a
    figure's title and figure_kind) separately from the step scaffold, whose
    ``input`` is often empty. Fold those inputs into the step, let an explicit
    per-step ``input`` win on conflicts, and — as a bounded, deterministic floor —
    bind the provider's declared *instruction* slot from the user goal when it is
    otherwise empty. Without this the runtime compiles the step with an empty
    ``input``, the input contract fails, and the recovery ladder prunes the step
    as "lacks required input" — silently dropping the requested deliverable
    (e.g. an architecture figure).

    Returns the composed input and whether the instruction slot was bound from
    the goal (for the step's audit trail).
    """
    merged: dict[str, object] = dict(capability_input or {})
    if isinstance(step_input, dict):
        merged.update(step_input)
    # Goal projection is bounded to the contract-declared instruction slot: never
    # a strict identifier/DOI/path/enum field, never a value the planner already
    # supplied. This restores executability without stuffing the goal into
    # arbitrary provider fields.
    bound_from_goal = False
    instruction_field = _instruction_field(entry)
    if instruction_field and _has_value(goal) and not _has_value(merged.get(instruction_field)):
        merged[instruction_field] = goal
        bound_from_goal = True
    return merged, bound_from_goal


def _instruction_field(entry: object) -> str:
    """The provider's declared free-text *instruction* slot, or "" if none.

    Prefer any field that explicitly declares
    ``x-omni.semantic_role == "instruction"`` (required fields first); otherwise
    fall back to a required field literally named ``input`` (the de-facto
    instruction slot across built-in skills) *only* when it is genuinely free
    text. Strict-typed fields (identifier/DOI/arXiv id/path via ``format``, or
    ``enum`` choices) are never treated as instruction slots, so the goal is
    never bound into them.
    """
    schema = getattr(entry, "input_schema", None)
    if not isinstance(schema, dict):
        return ""
    required_raw = schema.get("required")
    required = {str(field) for field in required_raw} if isinstance(required_raw, list) else set()
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for name in required:
        field_schema = properties.get(name) if isinstance(properties.get(name), dict) else {}
        if _semantic_role(field_schema) == "instruction" and _is_free_text_field(field_schema):
            return name
    if "input" in required:
        field_schema = properties.get("input") if isinstance(properties.get("input"), dict) else {}
        if _is_free_text_field(field_schema):
            return "input"
    for name, raw_field_schema in properties.items():
        field_schema = raw_field_schema if isinstance(raw_field_schema, dict) else {}
        if _semantic_role(field_schema) == "instruction" and _is_free_text_field(field_schema):
            return str(name)
    return ""


def _semantic_role(field_schema: dict[str, object]) -> str:
    for container in (field_schema, field_schema.get("x-omni"), field_schema.get("x_omni")):
        if isinstance(container, dict):
            role = container.get("semantic_role")
            if isinstance(role, str) and role.strip():
                return role.strip()
    return ""


def _is_free_text_field(field_schema: dict[str, object]) -> bool:
    if str(field_schema.get("type") or "string") != "string":
        return False
    # A strictly-typed field (arxiv_id/doi/uuid/path via ``format``, or a bounded
    # ``enum``) is not a free-text instruction slot and must never carry the goal.
    return not field_schema.get("format") and not field_schema.get("enum")


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def steps_from_capabilities(capabilities: list[str], text: str) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    previous_ids: list[str] = []
    for idx, capability in enumerate(capabilities, start=1):
        if capability not in WORKFLOW_CAPABILITIES:
            continue
        step_id = capability.split(".")[-1].replace("-", "_")
        if any(step["id"] == step_id for step in steps):
            step_id = f"{step_id}_{idx}"
        step = {
            "id": step_id,
            "capability": capability,
            "input": {"input": text},
            "depends_on": list(previous_ids[-1:]),
            "reason": f"model requested capability {capability}",
        }
        if is_native_synthesis_capability(capability):
            step["deliverable"] = DELIVERABLE_DRAFT_SECTION
        steps.append(step)
        previous_ids.append(step_id)
    return steps


def needs_input_plan(
    text: str,
    *,
    task_id: str,
    missing_inputs: list[dict[str, object]],
    rationale: str,
    confidence: float,
) -> IntentPlan:
    return IntentPlan(
        task_id=task_id,
        user_message=text,
        intent_type=IntentType.NEEDS_INPUT,
        confidence=confidence,
        outputs=["question"],
        execution_mode="ask",
        provenance_mode="light",
        context_policy=ContextPolicy(include_memory=False),
        tool_policy=ToolPolicy(allowed_tools=[], final_reserve_enabled=True, max_tool_calls=0, max_iterations=0),
        missing_inputs=[dict(item) for item in missing_inputs],
        verification_plan=VerificationPlan(required_outputs=["clarifying_question"]),
        acceptance=["missing_inputs_reported"],
        rationale=rationale,
    )


def _has_artifact_deliverable(deliverables: list[str]) -> bool:
    """Whether a workflow produces a renderable artifact (figure/diagram/etc.)."""
    return any("figure" in d or "artifact" in d for d in (deliverables or []))
