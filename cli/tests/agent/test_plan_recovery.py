"""Recovery ladder: a non-safety rejection is never a dead end.

These tests pin the four recoverable rungs (grounded repair, step degradation,
needs_input, ReAct handoff) and the single safety hard stop, plus the invariants
(repair is grounded and single-shot; safety is never swallowed).
"""

from __future__ import annotations

from omni.agent.intent_plan import (
    ContextPolicy,
    IntentPlan,
    IntentType,
    SkillSelection,
    ToolPolicy,
    VerificationPlan,
)
from omni.agent.model_planner import ModelPlanProposal
from omni.agent.plan_factory import build_react_recovery_plan
from omni.agent.plan_recovery import (
    ACTION_EXECUTE,
    ACTION_HARD_STOP,
    ACTION_NEEDS_INPUT,
    ACTION_REACT,
    recover,
)
from omni.agent.plan_revision import create_execution_authority
from omni.agent.plan_validator import PlanValidator
from omni.agent.planner import IntentPlanner
from omni.agent.workflow_plan_builder import needs_input_plan
from omni.config import load_settings
from omni.skills_runtime.manifest import DeliveryMode, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry


def _builtin() -> tuple[SkillRegistry, IntentPlanner]:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return registry, IntentPlanner(registry)


def _skill(
    name: str,
    capability: str,
    *,
    role: str = "task",
    required: list[str] | None = None,
    fmt: str | None = None,
    failure_policy: str = "",
    source: str = "builtin",
) -> SkillEntry:
    required = list(required or [])
    props: dict[str, dict] = {}
    for idx, key in enumerate(required):
        props[key] = {"type": "string"}
        if fmt and idx == 0:
            props[key] = {"type": "string", "format": fmt, "aliases": ["id", "url"]}
    return SkillEntry(
        name=name,
        description=f"{name} handles {capability}",
        source=source,
        kind=SkillKind.PYTHON_ENGINE,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role=role,
        capabilities=[capability],
        priority=50,
        input_schema={"type": "object", "properties": props, "required": list(required)},
        output_schema={"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]},
        workflow={"failure_policy": failure_policy} if failure_policy else {},
    )


def _arxiv_title_plan(planner: IntentPlanner) -> IntentPlan:
    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[
            {"id": "step1", "capability": "paper.fetch.arxiv",
             "input": {"input": "Attention Is All You Need"}},
            {"id": "figure", "capability": "artifact.figure", "depends_on": ["step1"],
             "input": {"input": "query/retriever/reranker/LLM 架构图"}},
        ],
        outputs=["workflow"],
        confidence=0.85,
        rationale="fetch abstract then draw architecture",
    )
    return planner.plan_from_proposal(
        "获取 Attention Is All You Need 摘要，并生成架构图。", proposal, task_id="run-recovery"
    )


# ── Rung 0: safety is terminal ──


def test_toolpolicy_conflict_is_a_safety_hard_stop() -> None:
    registry = SkillRegistry(load_settings())
    plan = IntentPlan(
        task_id="run-safety",
        user_message="anything",
        intent_type=IntentType.REACT_FALLBACK,
        tool_policy=ToolPolicy(allowed_tools=["read_file"], blocked_tools=["read_file"]),
        verification_plan=VerificationPlan(required_outputs=["answer"]),
    )
    validation = PlanValidator(registry).validate(plan)
    assert not validation.ok
    assert validation.has_safety_finding

    outcome = recover(plan, validation, registry)
    assert outcome.action == ACTION_HARD_STOP
    assert outcome.rung == "0_safety"
    assert outcome.notes


# ── Rung 0.75: resolvable identifier → ReAct look-up (the reported bug) ──


def test_validator_degrades_support_step_missing_input_instead_of_rejecting() -> None:
    registry, planner = _builtin()
    validation = PlanValidator(registry).validate(_arxiv_title_plan(planner))

    assert validation.ok
    assert validation.status == "degraded"
    finding = next(f for f in validation.findings if f.code == "step_input_contract")
    assert finding.severity == "degraded"
    assert finding.repairable is True
    assert finding.repair_capability == "literature.search"


def test_recovery_hands_unresolved_arxiv_title_to_react_lookup() -> None:
    # Look-up before ask/error (Invariant-B): a resolvable identifier the in-lane
    # resolver could not bind is handed to the ReAct floor to act-and-look-up
    # (search → id → fetch), never a lossy free-text repair that drops the fetch
    # chain. The floor is told to resolve the *title*, not the whole goal.
    registry, planner = _builtin()
    plan = _arxiv_title_plan(planner)
    outcome = recover(plan, PlanValidator(registry).validate(plan), registry)

    assert outcome.action == ACTION_REACT
    assert outcome.rung == "4_react_lookup"
    assert outcome.plan.intent_type == IntentType.REACT_FALLBACK
    assert outcome.plan.outputs == plan.outputs
    assert outcome.plan.task_contract == plan.task_contract
    assert outcome.plan.workflow_steps == plan.workflow_steps
    assert outcome.plan.workflow_steps is not plan.workflow_steps
    assert outcome.plan.verification_plan.required_events == ["react.finished"]
    assert outcome.plan.verification_plan.required_tasks == []
    assert (
        outcome.plan.verification_plan.required_outputs
        == plan.verification_plan.required_outputs
    )
    assert outcome.plan.provenance_mode == plan.provenance_mode
    recovered_validation = PlanValidator(registry).validate(outcome.plan)
    assert recovered_validation.ok
    authority = create_execution_authority(outcome.plan, registry=registry)
    workflow_authorities = [
        item
        for item in authority.provider_authorities
        if item.get("consumer_kind") == "workflow_step"
    ]
    assert {item["consumer_id"] for item in workflow_authorities} == {
        "step1",
        "figure",
    }
    figure_authority = next(
        item
        for item in workflow_authorities
        if item["consumer_id"] == "figure"
    )
    assert figure_authority["assessment_identity_required"] is True
    assert (
        figure_authority["assessment_identity"]["provider_binding_id"]
        == plan.workflow_steps[1]["provider_binding_id"]
    )
    assert any("Attention Is All You Need" in note for note in outcome.notes)
    assert any("same authorised step ids/providers" in note for note in outcome.notes)
    # Never surface the skill's contract message / a lossy free-text rewrite.
    assert all("literature search" not in note.lower() for note in outcome.notes)


# ── Rung 2: degrade/prune (mirror the runtime's partial policy) ──


def test_recovery_prunes_degradable_step_without_producer_and_keeps_deliverable() -> None:
    registry = SkillRegistry(load_settings())
    registry.register(
        _skill("opt-fetch", "custom.opt", role="support", required=["identifier"], fmt="doi",
               failure_policy="continue_with_partial")
    )
    registry.register(_skill("scientific-figure", "artifact.figure", role="task", required=["input"]))
    plan = IntentPlan(
        task_id="run-prune",
        user_message="给我画个架构图，如果能拿到那篇论文就更好",
        intent_type=IntentType.WORKFLOW,
        selected_skills=[
            SkillSelection(skill="opt-fetch", reason="support", contract_level="full"),
            SkillSelection(skill="scientific-figure", reason="task", contract_level="full"),
        ],
        workflow_steps=[
            {"id": "opt", "skill_name": "opt-fetch", "capability": "custom.opt",
             "input": {"identifier": "Some Paper Title"}, "depends_on": [],
             "required": False},
            {"id": "figure", "skill_name": "scientific-figure", "capability": "artifact.figure",
             "input": {"input": "架构图"}, "depends_on": ["opt"]},
        ],
        verification_plan=VerificationPlan(required_outputs=["artifact"], required_events=["subtask.submitted"]),
    )
    validation = PlanValidator(registry).validate(plan)
    # Resolver-owned facts always fail closed. The recovery ladder may still
    # prune this explicitly partial support step before execution.
    assert not validation.ok
    outcome = recover(plan, validation, registry)

    assert outcome.action == ACTION_EXECUTE
    assert outcome.rung == "2_degrade"
    skills = [str(s["skill_name"]) for s in outcome.plan.workflow_steps]
    assert skills == ["scientific-figure"]
    # The pruned step must not leave a dangling dependency on the figure.
    assert outcome.plan.workflow_steps[0].get("depends_on") == []
    assert outcome.notes


def test_recovery_protects_sole_producer_of_required_deliverable_instead_of_pruning() -> None:
    # continue_with_partial means "tolerate this step's *runtime* failure so
    # independent steps proceed" — it must NOT license deleting the sole producer
    # of a required deliverable before it executes. The figure provider's only
    # required field is strict-typed, so the plan-time goal projection cannot
    # satisfy it; dropping the step would silently lose the required figure.
    registry = SkillRegistry(load_settings())
    registry.register(_skill("kw-search", "literature.search", role="task", required=["query"]))
    registry.register(
        _skill("chart-figure", "artifact.figure", role="task", required=["dataset_id"], fmt="uuid",
               failure_policy="continue_with_partial")
    )
    plan = IntentPlan(
        task_id="run-protect",
        user_message="检索资料并生成架构图",
        intent_type=IntentType.WORKFLOW,
        outputs=["sources", "artifact.figure"],
        selected_skills=[
            SkillSelection(skill="kw-search", reason="task", contract_level="full"),
            SkillSelection(skill="chart-figure", reason="task", contract_level="full"),
        ],
        workflow_steps=[
            {"id": "search", "skill_name": "kw-search", "capability": "literature.search",
             "input": {"query": "RAG"}, "depends_on": []},
            {"id": "figure", "skill_name": "chart-figure", "capability": "artifact.figure",
             "input": {}, "depends_on": ["search"]},
        ],
        verification_plan=VerificationPlan(
            required_outputs=["artifact.figure"], required_events=["subtask.submitted"]
        ),
    )
    validation = PlanValidator(registry).validate(plan)
    assert validation.ok  # figure step is degradable → degraded, not blocking
    outcome = recover(plan, validation, registry)

    assert outcome.action == ACTION_NEEDS_INPUT
    assert outcome.rung == "2_degrade"
    assert outcome.plan.intent_type == IntentType.NEEDS_INPUT
    assert any(item.get("field") == "dataset_id" for item in outcome.missing_inputs)
    # The deliverable was not silently dropped: the ask names the figure step.
    assert any("sole producer" in note for note in outcome.notes)


# ── Rung 3: ask for a single user-suppliable field ──


def test_recovery_asks_for_resolver_field_without_a_lookup_adapter() -> None:
    registry = SkillRegistry(load_settings())
    registry.register(
        _skill("task-fetch", "custom.fetch", role="task", required=["identifier"], fmt="doi")
    )
    plan = IntentPlan(
        task_id="run-needs",
        user_message="获取那篇论文的数据",
        intent_type=IntentType.WORKFLOW,
        selected_skills=[SkillSelection(skill="task-fetch", reason="task", contract_level="full")],
        workflow_steps=[
            {"id": "only", "skill_name": "task-fetch", "capability": "custom.fetch",
             "input": {"identifier": "Some Paper Title"}, "depends_on": []},
        ],
        verification_plan=VerificationPlan(required_outputs=["data"], required_events=["subtask.submitted"]),
    )
    validation = PlanValidator(registry).validate(plan)
    assert not validation.ok  # required task step, no producer → blocking

    outcome = recover(plan, validation, registry)
    assert outcome.action == ACTION_NEEDS_INPUT
    assert outcome.rung == "3_needs_input"
    assert outcome.plan.intent_type == IntentType.NEEDS_INPUT
    assert any(item.get("field") == "identifier" for item in outcome.missing_inputs)


# ── Rung 4: the floor is the capable assistant, not a dead end ──


def test_recovery_hands_off_to_react_for_no_contract_required_skill() -> None:
    registry = SkillRegistry(load_settings())
    registry.register(
        SkillEntry(
            name="third-party-search",
            description="third party skill without schemas",
            source="user_claude",
            kind=SkillKind.CLI_EXEC,
            delivery_mode=DeliveryMode.ASYNC_TASK,
            input_schema={},
            output_schema={},
        )
    )
    task_contract = {
        "schema_version": 2,
        "objective": "围绕 RAG 做科研 workflow",
        "deliverables": [
            {
                "id": "report",
                "kind": "draft.manuscript",
                "required": True,
                "acceptance": ["draft_content_present"],
            }
        ],
    }
    verification = VerificationPlan(
        required_outputs=["draft.manuscript"],
        required_events=["workflow.submitted", "plan.executed"],
        provenance_checks=["full_as_requested"],
        deliverable_checks=["draft_content_present"],
    )
    plan = IntentPlan(
        task_id="run-react",
        user_message="围绕 RAG 做科研 workflow",
        intent_type=IntentType.WORKFLOW,
        outputs=["draft.manuscript"],
        provenance_mode="full",
        acceptance=["draft_delivered"],
        task_contract=task_contract,
        selected_skills=[SkillSelection(skill="third-party-search", reason="auto", contract_level="none")],
        workflow_steps=[
            {"id": "third", "skill_name": "third-party-search", "input": {"input": "RAG"}, "depends_on": []}
        ],
        verification_plan=verification,
    )
    validation = PlanValidator(registry).validate(plan)
    assert not validation.ok
    assert not validation.has_safety_finding

    outcome = recover(plan, validation, registry)
    assert outcome.action == ACTION_REACT
    assert outcome.rung == "4_react"
    assert outcome.plan.intent_type == IntentType.REACT_FALLBACK
    assert outcome.plan.execution_mode == "react"
    assert outcome.plan.outputs == plan.outputs
    assert outcome.plan.task_contract == task_contract
    assert outcome.plan.task_contract is not plan.task_contract
    assert outcome.plan.workflow_steps == plan.workflow_steps
    assert outcome.plan.workflow_steps is not plan.workflow_steps
    assert outcome.plan.verification_plan.required_events == ["react.finished"]
    assert outcome.plan.verification_plan.required_tasks == []
    assert (
        outcome.plan.verification_plan.required_outputs
        == verification.required_outputs
    )
    assert (
        outcome.plan.verification_plan.provenance_checks
        == verification.provenance_checks
    )
    assert (
        outcome.plan.verification_plan.deliverable_checks
        == verification.deliverable_checks
    )
    assert outcome.plan.verification_plan is not plan.verification_plan
    assert outcome.plan.provenance_mode == "full"
    assert outcome.plan.acceptance == ["draft_delivered"]
    assert outcome.notes


def test_react_recovery_preserves_goal_checks_without_broadening_policy() -> None:
    registry = SkillRegistry(load_settings())
    registry.register(
        SkillEntry(
            name="third-party-search",
            description="third party skill without schemas",
            source="user_claude",
            kind=SkillKind.CLI_EXEC,
            delivery_mode=DeliveryMode.ASYNC_TASK,
            input_schema={},
            output_schema={},
        )
    )
    plan = IntentPlan(
        task_id="run-policy",
        user_message="produce the requested artifact",
        intent_type=IntentType.WORKFLOW,
        outputs=["artifact"],
        selected_skills=[
            SkillSelection(
                skill="third-party-search",
                reason="auto",
                contract_level="none",
            )
        ],
        workflow_steps=[
            {
                "id": "third",
                "skill_name": "third-party-search",
                "input": {"input": "RAG"},
                "depends_on": [],
            }
        ],
        context_policy=ContextPolicy(
            include_recent_activity=False,
            include_research_brief=True,
            include_skill_catalog=False,
            include_memory=False,
            include_referenced_tasks=False,
        ),
        tool_policy=ToolPolicy(
            allowed_tools=["search_corpus", "run_skill"],
            blocked_tools=["open_artifact"],
            per_tool_limits={"search_corpus": 1},
            max_tool_calls=2,
            max_iterations=3,
            final_reserve_enabled=False,
        ),
        verification_plan=VerificationPlan(
            required_outputs=["artifact"],
            required_events=["workflow.submitted", "plan.executed"],
            forbidden_tools=["open_artifact"],
            required_tasks=["third-party-search"],
            artifact_checks=["artifact_emitted"],
            provenance_checks=["light_or_full_as_requested"],
            presentation_checks=["presentation_sent_or_degraded"],
        ),
    )

    recovered = build_react_recovery_plan(plan, rationale="test recovery")

    assert recovered.context_policy == plan.context_policy
    assert recovered.tool_policy.allowed_tools == [
        "search_corpus",
        "run_skill",
    ]
    assert set(recovered.tool_policy.blocked_tools) >= {
        "open_artifact",
        "write_file",
        "edit_file",
        "bash",
        "run_compute",
    }
    assert recovered.tool_policy.per_tool_limits == {"search_corpus": 1}
    assert recovered.tool_policy.max_tool_calls == 2
    assert recovered.tool_policy.max_iterations == 3
    assert recovered.tool_policy.final_reserve_enabled is True
    assert recovered.verification_plan.required_events == ["react.finished"]
    assert recovered.verification_plan.required_tasks == []
    assert recovered.verification_plan.artifact_checks == ["artifact_emitted"]
    assert recovered.verification_plan.provenance_checks == [
        "light_or_full_as_requested"
    ]
    assert recovered.verification_plan.presentation_checks == [
        "presentation_sent_or_degraded",
        "show_partial_when_budget_exhausted",
    ]


def test_react_recovery_keeps_exact_selected_provider_quality_authority() -> None:
    registry, _planner = _builtin()
    plan = IntentPlan(
        task_id="run-selected-recovery",
        user_message="Generate a RAG architecture figure.",
        intent_type=IntentType.SINGLE_SKILL_TASK,
        outputs=["artifact"],
        selected_skills=[
            SkillSelection(
                skill="scientific-figure",
                skill_source="builtin",
                reason="artifact.figure provider",
                matched_capabilities=["artifact.figure"],
                contract_level="full",
            )
        ],
        capability_inputs={
            "artifact.figure": {"input": "RAG architecture"}
        },
        tool_policy=ToolPolicy(
            allowed_tools=[],
            max_tool_calls=1,
        ),
        verification_plan=VerificationPlan(
            required_outputs=["artifact"],
            required_events=["subtask.submitted", "plan.executed"],
            required_tasks=["scientific-figure"],
            artifact_checks=["child_task_has_artifact_contract"],
        ),
    )
    assert PlanValidator(registry).validate(plan).ok

    recovered = build_react_recovery_plan(
        plan,
        rationale="repair objective inputs before exact provider execution",
    )
    assert [item.skill for item in recovered.selected_skills] == [
        "scientific-figure"
    ]
    assert recovered.tool_policy.allowed_tools == ["run_skill"]
    assert recovered.verification_plan.required_events == ["react.finished"]
    assert recovered.verification_plan.required_tasks == []
    assert PlanValidator(registry).validate(recovered).ok

    authority = create_execution_authority(recovered, registry=registry)
    selected = [
        item
        for item in authority.provider_authorities
        if item.get("consumer_kind") == "selected_skill"
    ]
    assert len(selected) == 1
    assert selected[0]["provider_name"] == "scientific-figure"
    assert selected[0]["assessment_identity_required"] is True
    assert (
        selected[0]["assessment_identity"]["provider_binding_id"]
        == plan.provider_bindings[0]["provider_binding_id"]
    )


# ── Reference-aware downgrade: look before asking (codex parity) ──


def test_needs_input_referencing_prior_work_downgrades_to_lookup_react() -> None:
    # The model chose to ask, but the request points at the agent's own prior
    # work — it must be turned into a capable, tool-enabled look-it-up turn
    # instead of a history-blind clarifying question.
    registry, _ = _builtin()
    plan = needs_input_plan(
        "你最近给我生成的架构图是讲的什么啊，给我重新生成一份吧",
        task_id="run-ref",
        missing_inputs=[{"field": "request", "reason": "which figure?"}],
        rationale="model asked",
        confidence=0.6,
    )
    outcome = recover(plan, PlanValidator(registry).validate(plan), registry)

    assert outcome.action == ACTION_REACT
    assert outcome.rung == "4_react_lookup"
    assert outcome.plan.intent_type == IntentType.REACT_FALLBACK
    # The recent-activity digest is surfaced so the referent resolves deterministically.
    assert outcome.plan.context_policy.include_recent_activity is True
    assert outcome.notes


def test_needs_input_without_referent_still_asks() -> None:
    # No referential marker → genuinely under-specified → still ask (no regression).
    registry, _ = _builtin()
    plan = needs_input_plan(
        "帮我弄个东西吧",
        task_id="run-vague",
        missing_inputs=[{"field": "request", "reason": "too vague"}],
        rationale="model asked",
        confidence=0.6,
    )
    outcome = recover(plan, PlanValidator(registry).validate(plan), registry)

    assert outcome.action != ACTION_REACT
    assert outcome.plan.intent_type == IntentType.NEEDS_INPUT


# ── Invariants ──


def test_clean_plan_executes_unchanged() -> None:
    registry, planner = _builtin()
    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[
            {"id": "fetch", "capability": "paper.fetch.arxiv", "input": {"identifier": "1706.03762"}},
            {"id": "figure", "capability": "artifact.figure", "depends_on": ["fetch"], "input": {"input": "图"}},
        ],
        outputs=["workflow"],
        confidence=0.9,
        rationale="valid arxiv id workflow",
    )
    plan = planner.plan_from_proposal("获取 arXiv 1706.03762 摘要并画图", proposal, task_id="run-clean")
    validation = PlanValidator(registry).validate(plan)
    assert validation.ok

    outcome = recover(plan, validation, registry)
    assert outcome.action == ACTION_EXECUTE
    assert outcome.plan is plan  # unchanged fast path
    assert outcome.rung == "ok"


def test_unresolved_arxiv_identifier_hands_off_to_react_even_without_a_producer() -> None:
    # Invariant-B: an identifier binding the in-lane resolver could not satisfy is
    # handed to the ReAct floor regardless of whether a search *producer* is
    # registered — the floor searches, takes the id, and fetches. It is never a
    # lossy free-text rewrite, and never silently drops the fetch by pruning.
    registry = SkillRegistry(load_settings())
    registry.register(
        _skill("arxiv-fetch", "paper.fetch.arxiv", role="support", required=["identifier"], fmt="arxiv_id",
               failure_policy="continue_with_partial")
    )
    registry.register(_skill("scientific-figure", "artifact.figure", role="task", required=["input"]))
    plan = IntentPlan(
        task_id="run-noproducer",
        user_message="获取 Attention Is All You Need 摘要并画架构图",
        intent_type=IntentType.WORKFLOW,
        selected_skills=[
            SkillSelection(skill="arxiv-fetch", reason="support", contract_level="full"),
            SkillSelection(skill="scientific-figure", reason="task", contract_level="full"),
        ],
        workflow_steps=[
            {"id": "step1", "skill_name": "arxiv-fetch", "capability": "paper.fetch.arxiv",
             "input": {"identifier": "Attention Is All You Need"}, "depends_on": []},
            {"id": "figure", "skill_name": "scientific-figure", "capability": "artifact.figure",
             "input": {"input": "架构图"}, "depends_on": ["step1"]},
        ],
        verification_plan=VerificationPlan(required_outputs=["artifact"], required_events=["subtask.submitted"]),
    )
    validation = PlanValidator(registry).validate(plan)
    outcome = recover(plan, validation, registry)

    assert outcome.action == ACTION_REACT
    assert outcome.rung == "4_react_lookup"
    assert any("Attention Is All You Need" in note for note in outcome.notes)
