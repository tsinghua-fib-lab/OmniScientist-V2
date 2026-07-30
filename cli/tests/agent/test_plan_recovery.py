"""Recovery ladder: a non-safety rejection is never a dead end.

The ladder's middle rungs (identifier look-up handoff, grounded repair,
degradable-step pruning) existed to patch a DAG the host had sealed before any
tool ran. The model now sequences multi-step work itself, so those rungs are
gone and these tests pin what is left: the single safety hard stop, the ask for
one user-suppliable field, the capable ReAct floor, and the invariant that a
reference to the agent's own prior work becomes a look-it-up turn rather than a
history-blind question.
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
from omni.agent.plan_factory import build_react_recovery_plan, needs_input_plan
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


def test_a_field_the_provider_never_declared_is_not_a_question_for_the_user() -> None:
    """A slot the planner invented is the planner's mistake, not a missing input.

    Rung 3 asks when one *user-suppliable* field is the only blocker. A field the
    provider does not declare is not suppliable by anyone: there is nowhere to
    put the answer. Run 0db3d740 asked anyway, and because the finding text was
    copied into ``ask`` the user was invited to answer "field 'output_language'
    is not declared by the selected provider".

    Dropping the value is safe because the compiler never put it in
    ``arguments`` to begin with, and it cannot hide a real gap: a field the
    provider actually requires raises its own ``missing_<name>`` blocker.
    """
    registry = SkillRegistry(load_settings())
    registry.register(
        SkillEntry(
            name="figure-maker",
            description="figure-maker handles artifact.figure",
            source="builtin",
            kind=SkillKind.PYTHON_ENGINE,
            delivery_mode=DeliveryMode.ASYNC_TASK,
            role="task",
            capabilities=["artifact.figure"],
            priority=50,
            input_schema={
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        )
    )
    plan = IntentPlan(
        task_id="run-undeclared",
        user_message="画一张 RAG 架构图",
        intent_type=IntentType.SINGLE_SKILL_TASK,
        selected_skills=[
            SkillSelection(
                skill="figure-maker",
                reason="task",
                contract_level="full",
                matched_capabilities=["artifact.figure"],
            )
        ],
        capability_inputs={
            "artifact.figure": {"input": "RAG architecture", "output_language": "zh"}
        },
        verification_plan=VerificationPlan(required_outputs=["figure"]),
    )
    validation = PlanValidator(registry).validate(plan)

    outcome = recover(plan, validation, registry)
    assert outcome.action == ACTION_EXECUTE
    assert not any("output_language" in str(item) for item in outcome.missing_inputs)
    assert any("output_language" in note for note in outcome.notes)


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
    # The workflow executor is gone, so its ``workflow.submitted`` promise goes
    # with it: the floor may only be held to a trace it can actually leave.
    assert outcome.plan.verification_plan.required_events == ["react.finished"]
    assert (
        outcome.plan.verification_plan.required_outputs
        == verification.required_outputs
    )
    assert outcome.plan.verification_plan is not plan.verification_plan
    assert outcome.plan.provenance_mode == "full"
    assert outcome.plan.acceptance == ["draft_delivered"]
    assert outcome.notes


def test_react_recovery_preserves_the_goal_without_broadening_policy() -> None:
    """The floor inherits the plan's ceiling; it does not get a wider one.

    Recovery only swaps *how* the turn runs. A request the planner limited to
    two tool calls, one corpus search, and no ``open_artifact`` must come back
    with those same limits — the only change permitted is adding the safety
    blocks the ReAct floor always applies.
    """
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
    # The goal survives the executor swap; the abandoned executor's promise does not.
    assert recovered.verification_plan.required_outputs == ["artifact"]
    assert recovered.verification_plan.required_events == ["react.finished"]


def test_react_recovery_keeps_the_exact_selected_provider_authority() -> None:
    """Recovery may change how a turn runs, never which provider runs it.

    The floor is handed ``run_skill`` so it can re-submit the abandoned work.
    That only stays safe while the sealed authority keeps naming the exact
    provider the plan selected — otherwise ReAct could satisfy the turn with a
    same-named look-alike from another source that was never authorised.
    """
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
        ),
    )
    assert PlanValidator(registry).validate(plan).ok
    sealed = plan.provider_bindings[0]

    recovered = build_react_recovery_plan(
        plan,
        rationale="repair objective inputs before exact provider execution",
    )
    assert [item.skill for item in recovered.selected_skills] == [
        "scientific-figure"
    ]
    assert recovered.tool_policy.allowed_tools == ["run_skill"]
    assert recovered.verification_plan.required_events == ["react.finished"]
    assert PlanValidator(registry).validate(recovered).ok
    assert recovered.provider_bindings == [sealed]

    authority = create_execution_authority(recovered, registry=registry)
    selected = [
        item
        for item in authority.provider_authorities
        if item.get("consumer_kind") == "selected_skill"
    ]
    assert len(selected) == 1
    assert selected[0]["provider_name"] == sealed["provider_name"]
    assert selected[0]["provider_source"] == sealed["provider_source"]
    identity = selected[0]["execution_identity"]
    assert identity["name"] == "scientific-figure"
    assert identity["source"] == "builtin"
    assert identity["version"] == sealed["provider_version"]


def test_identifier_lookup_recovery_widens_the_tool_budget() -> None:
    """A workflow that hit the floor over one unbound id gets room to act.

    The downgrade happened *because* an identifier could not be resolved in
    lane. Inheriting the workflow's tight ceiling would leave no calls to look
    the id up and re-run the authorised DAG, so the turn would dead-end for the
    same reason twice. Every other recovery keeps the planner's ceiling.
    """
    verification = VerificationPlan(
        required_outputs=["artifact"],
        required_events=["workflow.submitted", "plan.executed"],
    )
    plan = IntentPlan(
        task_id="run-id-lookup",
        user_message="获取 Attention Is All You Need 摘要并生成架构图",
        intent_type=IntentType.WORKFLOW,
        outputs=["artifact"],
        workflow_steps=[
            {"id": "paper", "capability": "paper.fetch.arxiv", "input": {"input": "x"}},
            {"id": "figure", "capability": "artifact.figure", "depends_on": ["paper"], "input": {}},
        ],
        tool_policy=ToolPolicy(max_tool_calls=4, max_iterations=4),
        verification_plan=verification,
    )

    lookup = build_react_recovery_plan(
        plan, rationale="id lookup", identifier_lookup=True
    )
    assert lookup.tool_policy.max_tool_calls >= 8
    assert lookup.tool_policy.max_iterations >= 8
    # Widening the budget is not licence to change what the turn owes.
    assert lookup.verification_plan.required_outputs == ["artifact"]

    general = build_react_recovery_plan(plan, rationale="other")
    assert general.tool_policy.max_tool_calls == 4
    assert general.tool_policy.max_iterations == 4


def test_the_floor_inherits_the_obligation_to_produce_the_answer() -> None:
    """A downgraded turn still owes this turn's work, not last turn's answer.

    Asked twice to review the day's commits, the planner proposed a skill task
    and named no skill; the ladder handed the request to the floor, which found
    the previous review sitting in context and returned it. Nothing in that turn
    read a commit, and no approval was ever requested because no command was
    ever run. The floor has to be told that the route which would have produced
    the output did not run — the lookup rung says the opposite on purpose, which
    is why the instruction belongs to this branch alone.
    """
    registry, _ = _builtin()
    plan = IntentPlan(
        task_id="run-review",
        user_message="仔细 review 今天 push 到 master 上的代码",
        intent_type=IntentType.SINGLE_SKILL_TASK,
        outputs=["review"],
        selected_skills=[],
        tool_policy=ToolPolicy(
            allowed_tools=[],
            blocked_tools=["bash", "write_file"],
            per_tool_limits={"search_corpus": 1},
            max_tool_calls=1,
            max_iterations=1,
        ),
    )
    validation = PlanValidator(registry).validate(plan)
    assert not validation.ok
    assert "missing_selected_skills" in {f.code for f in validation.findings}

    outcome = recover(plan, validation, registry)

    assert outcome.action == ACTION_REACT
    assert outcome.rung == "4_react"
    joined = " ".join(outcome.notes).lower()
    assert "derive the answer in this turn" in joined
    assert "earlier turn does not satisfy" in joined
    assert outcome.plan.tool_policy.allowed_tools is None
    assert outcome.plan.tool_policy.max_tool_calls is None
    assert outcome.plan.tool_policy.max_iterations is None
    assert {"bash", "write_file"} <= set(outcome.plan.tool_policy.blocked_tools)
    assert outcome.plan.tool_policy.per_tool_limits == {"search_corpus": 1}


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
