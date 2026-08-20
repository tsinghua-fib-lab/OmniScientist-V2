"""IntentPlan routing and skill-selection explanations."""

from __future__ import annotations

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType, SkillSelection, VerificationPlan
from omni.agent.model_planner import (
    _PLANNER_CONTRACT_SHORTLIST_LIMIT,
    _PLANNER_INDEX_PROSE_LIMIT,
    ModelPlanProposal,
    _compose_step_input,
    _planner_relevant_contracts,
    _planner_skill_index,
    _planner_system_prompt,
)
from omni.agent.plan_validator import PlanValidator
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.skills_runtime.manifest import DeliveryMode, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry


def _figure_skill() -> SkillEntry:
    return SkillEntry(
        name="scientific-figure",
        description="Generate publication-ready scientific architecture diagrams.",
        kind=SkillKind.PYTHON_ENGINE,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role="task",
        capabilities=["artifact.figure", "figure.architecture", "artifact.svg", "artifact.png", "research.provenance"],
        priority=100,
        default_for=["architecture diagram", "架构图", "RAG architecture"],
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        output_schema={"type": "object", "properties": {"summary": {"type": "string"}, "artifacts": {"type": "array"}}},
    )


def _capability_skill(
    name: str,
    capability: str,
    *,
    source: str = "project_omni",
    priority: int = 0,
) -> SkillEntry:
    return SkillEntry(
        name=name,
        description=f"{name} handles {capability}",
        source=source,
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role="task",
        capabilities=[capability],
        priority=priority,
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
    )


def test_shortlisted_contract_preserves_exact_schema_and_x_omni_hints() -> None:
    entry = SkillEntry(
        name="slides",
        description="Generate slides",
        input_schema={
            "type": "object",
            "properties": {
                "review_mode": {
                    "type": "string",
                    "enum": ["none", "plan"],
                    "x-omni": {
                        "semantic_key": "review_mode",
                        "binding_owner": "model",
                        "expectation": {
                            "kind": "explicit_enum",
                            "explicit_only": True,
                            "signatures": {
                                "plan": [
                                    "review the outline",
                                    "approve the outline",
                                ]
                            },
                        },
                    },
                }
            },
        },
    )

    registry = SkillRegistry(load_settings(), sources=())
    registry.register(entry)
    contracts = _planner_relevant_contracts(
        registry,
        user_message="Generate slides and review the outline.",
    )

    assert '"name":"slides"' in contracts
    assert '"review_mode":{"enum":["none","plan"],"type":"string"' in contracts
    assert '"semantic_key":"review_mode"' in contracts
    assert '"binding_owner":"model"' in contracts
    assert '"kind":"explicit_enum"' in contracts
    assert '"review the outline"' in contracts


def test_planner_uses_declared_figure_kind_without_host_alias() -> None:
    entry = SkillEntry(
        name="contract-figure",
        description="Generate a contracted figure",
        capabilities=["artifact.figure"],
        input_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "figure_kind": {
                    "type": "string",
                    "enum": ["generic", "rag", "transformer"],
                    "x-omni": {
                        "semantic_key": "figure_kind",
                        "binding_owner": "model",
                    },
                },
            },
            "required": ["input"],
        },
    )
    registry = SkillRegistry(load_settings())
    registry.register(entry)

    prompt = _planner_system_prompt(registry)
    composed, _ = _compose_step_input(
        entry,
        {"template": "rag"},
        {},
        "Draw a RAG architecture.",
    )

    assert '"figure_kind":{"enum":["generic","rag","transformer"]' in prompt
    assert "capability_inputs.artifact.figure.template" not in prompt
    assert "figure_kind" not in composed


def test_planner_prompt_cache_invalidates_when_registry_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omni.agent import model_planner

    registry = SkillRegistry(load_settings())
    registry.register(_figure_skill())
    original_catalog = model_planner._planner_skill_index
    catalog_calls = 0

    def _counted_catalog(*args, **kwargs):
        nonlocal catalog_calls
        catalog_calls += 1
        return original_catalog(*args, **kwargs)

    monkeypatch.setattr(
        model_planner,
        "_planner_skill_index",
        _counted_catalog,
    )

    first = model_planner._planner_system_prompt(registry)
    second = model_planner._planner_system_prompt(registry)
    registry.register(
        _capability_skill(
            "new-contract-provider",
            "artifact.new-contract",
        )
    )
    changed = model_planner._planner_system_prompt(registry)

    assert first == second
    assert catalog_calls == 2
    assert "new-contract-provider" not in first
    assert "new-contract-provider" in changed


def test_planner_catalog_keeps_late_provider_and_field_thirteen() -> None:
    registry = SkillRegistry(load_settings(), sources=())
    names: list[str] = []
    for index in range(40):
        name = f"catalog-provider-{index:02d}"
        names.append(name)
        registry.register(
            _capability_skill(
                name,
                f"catalog.capability_{index:02d}",
            )
        )
    properties = {
        f"field_{index:02d}": {"type": "string"}
        for index in range(1, 13)
    }
    properties["review_mode"] = {
        "type": "string",
        "enum": ["none", "plan", "interactive"],
        "x-omni": {
            "semantic_key": "review_mode",
            "binding_owner": "model",
            "expectation": {
                "kind": "explicit_enum",
                "signatures": {
                    "plan": [
                        "review the outline",
                        "approve the outline",
                        (
                            "approve the complete scientific outline "
                            "before rendering"
                        ),
                    ],
                },
            },
        },
    }
    late = SkillEntry(
        name="zeta-review-deck",
        description="Build a deck with optional outline review.",
        capabilities=["slides.generate"],
        trigger={"phrases": ["review the outline"]},
        input_schema={
            "type": "object",
            "properties": properties,
        },
    )
    names.append(late.name)
    registry.register(late)

    prompt = _planner_system_prompt(
        registry,
        user_message=(
            "Use zeta-review-deck to build slides and review the outline."
        ),
    )

    assert all(f"- {name}:" in prompt for name in names)
    assert '"review_mode":{"enum":["none","plan","interactive"]' in prompt
    assert '"plan":["review the outline","approve the outline",' in prompt
    assert (
        "approve the complete scientific outline before rendering"
        in prompt
    )
    assert "... (truncated)" not in prompt


def test_planner_index_caps_provider_prose_without_clipping_identity() -> None:
    registry = SkillRegistry(load_settings(), sources=())
    count = 24
    for index in range(count):
        registry.register(
            SkillEntry(
                name=f"long-prose-provider-{index:02d}",
                description="d" * 10_000,
                when_to_use="w" * 10_000,
                capabilities=[f"long.capability_{index:02d}"],
            )
        )

    skill_index = _planner_skill_index(registry)
    lines = skill_index.splitlines()

    assert len(lines) == count
    assert len(skill_index) < count * (
        2 * _PLANNER_INDEX_PROSE_LIMIT + 300
    )
    for index, line in enumerate(lines):
        assert f"- long-prose-provider-{index:02d}:" in line
        assert f"capabilities=long.capability_{index:02d}" in line
        parts = {
            key: value
            for key, value in (
                part.split("=", 1)
                for part in line.split("; ")
                if "=" in part
            )
        }
        assert len(parts["description"]) <= _PLANNER_INDEX_PROSE_LIMIT
        assert len(parts["when_to_use"]) <= _PLANNER_INDEX_PROSE_LIMIT


def test_non_shortlisted_provider_stays_discoverable_without_schema_bloat() -> None:
    """A provider past the cut keeps its index row and costs no schema.

    The registry is sized from the cut rather than from a number written here:
    with a fixed nine providers the test stopped exercising a cut at all the
    moment the shortlist was widened to cover the shipped catalogue, and went on
    passing while proving nothing.
    """
    registry = SkillRegistry(load_settings(), sources=())
    ranked_names: list[str] = []
    for index in range(_PLANNER_CONTRACT_SHORTLIST_LIMIT + 1):
        name = f"ranked-provider-{index:02d}"
        ranked_names.append(name)
        registry.register(
            _capability_skill(name, f"ranked.capability_{index:02d}")
        )
    semantic_properties = {
        f"late_semantic_{index:02d}": {
            "type": "string",
            "enum": ["default", f"value_{index:02d}"],
            "x-omni": {
                "semantic_key": f"late_semantic_{index:02d}",
                "binding_owner": "model",
                "expectation": {
                    "kind": "explicit_enum",
                    "signatures": {
                        f"value_{index:02d}": [
                            f"late-signature-{index:02d}-never-shortlisted"
                        ]
                    },
                },
            },
        }
        for index in range(13)
    }
    registry.register(
        SkillEntry(
            name="semantic-late-provider",
            description="Provider with a declarative semantic contract.",
            capabilities=["semantic.late"],
            input_schema={
                "type": "object",
                "properties": semantic_properties,
            },
        )
    )

    prompt = _planner_system_prompt(
        registry,
        # Name enough providers to claim every slot, so the late one is cut for
        # being irrelevant rather than for losing a tie-break.
        user_message=" ".join(ranked_names[:_PLANNER_CONTRACT_SHORTLIST_LIMIT]),
    )
    full_contracts = prompt.split(
        "Relevant provider input contracts (exact field names):\n",
        1,
    )[1]

    assert "- semantic-late-provider:" in prompt
    assert '"name":"semantic-late-provider"' not in full_contracts
    assert "late_semantic_12" not in full_contracts
    assert "late-signature-12-never-shortlisted" not in prompt


def test_planner_prompt_uses_provider_schema_without_constraint_protocol() -> None:
    prompt = _planner_system_prompt(
        SkillRegistry(load_settings(), sources=()),
    )

    assert "Bind provider inputs using the exact field names and enum values" in prompt
    assert "requested_constraints" not in prompt
    assert "evidence must be a verbatim substring" not in prompt


def test_model_proposal_ignores_retired_constraint_payload() -> None:
    constraints = [
        {
            "semantic_key": f"preference_{index:02d}",
            "requested_value": f"value_{index:02d}",
            "evidence": f"evidence-{index:02d}",
            "explicit": True,
            "step_id": "consume",
            "capability": "artifact.consume",
        }
        for index in range(13)
    ]

    proposal = ModelPlanProposal.from_payload(
        {"requested_constraints": constraints}
    )
    assert "requested_constraints" not in proposal.to_dict()


def test_model_proposal_cannot_choose_concrete_runtime_tools() -> None:
    proposal = ModelPlanProposal.from_payload(
        {
            "intent_type": "react_fallback",
            "required_capabilities": ["task.inspect"],
            "required_tools": ["get_task", "bash"],
        }
    )

    assert proposal.required_capabilities == ["task.inspect"]
    assert "required_tools" not in proposal.to_dict()


def test_real_research_pptx_late_provider_field_reaches_exact_schema_prompt() -> None:
    settings = load_settings()
    registry = SkillRegistry(settings)
    registry.build_index()

    prompt = _planner_system_prompt(
        registry,
        settings=settings,
        user_message=(
            "Create a research slide deck and let me review the outline first."
        ),
    )

    assert "research-pptx" in prompt
    assert '"review_mode":{"description":' in prompt
    assert '"enum":["none","plan","interactive"]' in prompt
    assert '"semantic_key":"review_mode"' in prompt


def test_rag_qa_plus_artifact_plan_explains_scientific_figure_selection():
    registry = SkillRegistry(load_settings())
    registry.register(_figure_skill())
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="qa_plus_artifact",
        required_capabilities=["qa.grounded", "artifact.figure"],
        outputs=["answer", "artifact"],
        confidence=0.91,
        rationale="model identified grounded QA plus a figure deliverable",
    )
    plan = planner.plan_from_proposal(
        "RAG 如何降低幻觉，并给我生成一份目前全球范围最流行的 RAG 构建的架构图。",
        proposal,
        task_id="run123",
    )

    assert plan.intent_type == IntentType.QA_PLUS_ARTIFACT
    assert plan.provenance_mode == "light"
    assert [s.skill for s in plan.selected_skills] == ["scientific-figure"]
    selection = plan.selected_skills[0]
    assert "architecture" in " ".join(selection.matched_capabilities)
    assert selection.reason
    assert selection.selection_source
    assert plan.tool_policy.per_tool_limits["search_corpus"] == 1
    assert "glob" in plan.tool_policy.blocked_tools
    assert "record_claim" in plan.tool_policy.blocked_tools
    assert plan.verification_plan.required_outputs == [
        "answer",
        "artifact",
        "artifact.figure",
    ]
    assert "subtask.submitted" in plan.verification_plan.required_events


def _assert_capable_assistant(plan: IntentPlan) -> None:
    """The single default path: capable, safety-bounded ReAct.

    ``allowed_tools=None`` means "unrestricted except blocked"; only irreversible
    filesystem/shell mutations are blocked, so the model gets a real agent
    catalog (read, search, recall, research-capture, skill/workflow invocation).
    """
    assert plan.execution_mode == "react"
    assert plan.tool_policy.final_reserve_enabled is True
    assert plan.tool_policy.allowed_tools is None
    assert plan.tool_policy.max_tool_calls is None
    for blocked in ("write_file", "edit_file", "bash"):
        assert plan.tool_policy.allows(blocked) is False
    for allowed in ("search_corpus", "read_file", "glob", "open_artifact", "run_skill", "run_workflow"):
        assert plan.tool_policy.allows(allowed) is True
    assert plan.context_policy.include_skill_catalog is True


def test_plain_prompt_without_clear_route_uses_capable_assistant():
    planner = IntentPlanner(SkillRegistry(load_settings()))

    plan = planner.plan("RAG 的核心思路是否合理", task_id="run456")

    assert plan.intent_type == IntentType.REACT_FALLBACK
    _assert_capable_assistant(plan)


def test_vague_contextual_prompt_goes_to_capable_assistant():
    # Ambiguity is the model's job now, not a regex "vague" gate: the capable
    # assistant can ask for clarification itself instead of a hardcoded needs_input.
    planner = IntentPlanner(SkillRegistry(load_settings()))

    plan = planner.plan("这个怎么设计？", task_id="run-needs-input")

    assert plan.intent_type == IntentType.REACT_FALLBACK
    _assert_capable_assistant(plan)


def test_product_question_goes_to_capable_assistant_not_hardcoded_faq():
    # Product/design questions previously hit a hardcoded FAQ; they now go to the
    # model with a real read/recall catalog so it can inspect the workspace.
    planner = IntentPlanner(SkillRegistry(load_settings()))

    plan = planner.plan("的存储是怎么架构设计的", task_id="run-product")

    assert plan.intent_type == IntentType.REACT_FALLBACK
    _assert_capable_assistant(plan)


def test_workspace_lookup_gets_read_tools_via_capable_assistant():
    planner = IntentPlanner(SkillRegistry(load_settings()))

    plan = planner.plan("请读取 cli/src/omni/agent/planner.py 看看这个文件怎么设计", task_id="run-file")

    assert plan.intent_type == IntentType.REACT_FALLBACK
    assert plan.tool_policy.allows("read_file") is True
    assert plan.tool_policy.allows("glob") is True
    assert plan.tool_policy.allows("open_artifact") is True
    assert plan.tool_policy.allows("write_file") is False


def test_execution_prompt_gets_execution_and_read_tools():
    planner = IntentPlanner(SkillRegistry(load_settings()))

    plan = planner.plan("简单说说你的看法", task_id="run-exec")

    assert plan.intent_type == IntentType.REACT_FALLBACK
    assert plan.tool_policy.allows("run_workflow") is True
    assert plan.tool_policy.allows("run_skill") is True
    # A capable agent can also read the workspace; only mutations are blocked.
    assert plan.tool_policy.allows("read_file") is True
    assert plan.tool_policy.allows("bash") is False


def test_model_proposal_is_resolved_by_registry_contracts_not_skill_name_guessing():
    registry = SkillRegistry(load_settings())
    registry.register(_figure_skill())
    registry.register(_capability_skill("user-fancy-diagram", "artifact.figure", source="user_codex", priority=999))
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="qa_plus_artifact",
        required_capabilities=["qa.grounded", "artifact.figure"],
        outputs=["answer", "artifact"],
        confidence=0.91,
        rationale="model identified a research QA answer plus a figure deliverable",
    )
    plan = planner.plan_from_proposal(
        "RAG hallucination mitigation with a system blueprint",
        proposal,
        task_id="run-model-plan",
    )

    assert plan.intent_type == IntentType.QA_PLUS_ARTIFACT
    assert [selection.skill for selection in plan.selected_skills] == ["scientific-figure"]
    assert plan.selected_skills[0].selection_source == "capability"
    assert any(item.skill == "user-fancy-diagram" for item in plan.selected_skills[0].rejected_candidates)
    assert plan.rationale == proposal.rationale
    assert "glob" in plan.tool_policy.blocked_tools


def test_model_direct_answer_proposal_keeps_capable_read_only_floor():
    # direct_answer is a capability-preserving turn with an eager-answer bias, not
    # a zero-tool turn: stripping the catalog while the system prompt still asks
    # for docs_search is exactly what made a self-knowledge question dead-end.
    planner = IntentPlanner(SkillRegistry(load_settings()))

    proposal = ModelPlanProposal(
        intent_type="direct_answer",
        outputs=["answer"],
        confidence=0.86,
        rationale="model identified a plain product answer",
    )
    plan = planner.plan_from_proposal("这个工具的存储架构是什么？", proposal, task_id="run-direct")

    assert plan.intent_type == IntentType.DIRECT_ANSWER
    assert plan.execution_mode == "direct"
    assert plan.tool_policy.allowed_tools is None
    assert plan.tool_policy.final_reserve_enabled is True
    for allowed in ("docs_search", "read_file", "search_corpus", "run_skill"):
        assert plan.tool_policy.allows(allowed) is True
    for blocked in ("write_file", "edit_file", "bash", "run_compute"):
        assert plan.tool_policy.allows(blocked) is False


def test_model_direct_answer_proposal_for_toolful_request_still_has_read_tools():
    # Even when the model misroutes a read/query request to direct_answer, the
    # turn keeps the read-only tool floor, so the whole class of "no tool for a
    # tool-needing request" dead-ends is gone — not just the docs_search symptom.
    planner = IntentPlanner(SkillRegistry(load_settings()))

    proposal = ModelPlanProposal(
        intent_type="direct_answer",
        outputs=["answer"],
        confidence=0.8,
        rationale="model misjudged a file read as a short answer",
    )
    plan = planner.plan_from_proposal(
        "读一下 cli/src/omni/agent/planner.py 看看怎么设计的",
        proposal,
        task_id="run-direct-read",
    )

    assert plan.intent_type == IntentType.DIRECT_ANSWER
    assert plan.tool_policy.allows("read_file") is True
    assert plan.tool_policy.allows("glob") is True
    assert plan.tool_policy.allows("write_file") is False


def test_task_inspect_capability_requires_authoritative_get_task_lookup() -> None:
    registry = SkillRegistry(load_settings())
    planner = IntentPlanner(registry)
    proposal = ModelPlanProposal.from_payload(
        {
            "intent_type": "react_fallback",
            "required_capabilities": ["task.inspect"],
            "outputs": ["answer"],
            "confidence": 0.95,
            "rationale": "read the active task status before answering",
        }
    )

    plan = planner.plan_from_proposal(
        "刚才的审稿成功了吗？结果在哪里？",
        proposal,
        task_id="run-status-followup",
    )

    # The model names a native information capability; only the host maps it to
    # the concrete, read-only tool surface.
    assert proposal.required_capabilities == ["task.inspect"]
    assert plan.tool_policy.allowed_tools == ["get_task"]
    assert plan.tool_policy.require_opening_tool is True
    assert plan.tool_policy.max_tool_calls is None
    assert plan.tool_policy.max_iterations is None
    assert plan.tool_policy.allows("get_task") is True
    assert plan.tool_policy.allows("open_artifact") is False
    assert plan.context_policy.include_recent_activity is True
    assert "react.tool.done" in plan.verification_plan.required_events
    assert PlanValidator(registry).validate(plan).status == "validated"


def test_task_review_capability_maps_to_enumerative_recall_surface() -> None:
    registry = SkillRegistry(load_settings())
    planner = IntentPlanner(registry)
    proposal = ModelPlanProposal.from_payload(
        {
            "intent_type": "react_fallback",
            "required_capabilities": ["task.review"],
            "outputs": ["answer"],
            "confidence": 0.9,
            "rationale": "review the last few days of tasks across projects",
        }
    )

    plan = planner.plan_from_proposal(
        "过去四天我们都做了些什么？哪些没做好？",
        proposal,
        task_id="run-review",
    )

    # A retrospective is enumerative: the host grants the cross-workspace recall
    # tools, not the single-task get_task surface.
    assert "task.review" in plan.capability_inputs
    assert plan.tool_policy.allows("list_recent_tasks") is True
    assert plan.tool_policy.allows("search_tasks") is True
    assert plan.tool_policy.max_tool_calls is None
    assert plan.tool_policy.max_iterations is None
    assert plan.tool_policy.allows("get_task") is True
    assert plan.tool_policy.allows("write_file") is False
    # No hard per-turn call ceiling that would refuse legitimate enumeration
    # (the count cap is exactly what broke the original incident).
    assert plan.tool_policy.per_tool_limits.get("get_task") is None
    assert plan.tool_policy.require_opening_tool is True
    assert plan.context_policy.include_recent_activity is True
    assert PlanValidator(registry).validate(plan).status == "validated"


def _builtin_planner() -> IntentPlanner:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return IntentPlanner(registry)


def test_model_memory_update_proposal_becomes_no_tool_plan() -> None:
    planner = _builtin_planner()
    proposal = ModelPlanProposal(
        intent_type="memory_update",
        required_capabilities=["memory.update"],
        outputs=["memory"],
        capability_inputs={"memory.update": {"content": "Project studies reranker factual consistency."}},
        confidence=0.95,
        rationale="the user explicitly requested durable memory",
    )
    plan = planner.plan_from_proposal("Remember this project fact.", proposal, task_id="run-mem")

    assert plan.intent_type == IntentType.MEMORY_UPDATE
    assert plan.execution_mode == "direct"
    assert plan.tool_policy.max_tool_calls == 0
    assert plan.tool_policy.max_iterations == 0
    assert plan.capability_inputs["memory.update"]["content"].startswith("Project studies")


@pytest.mark.parametrize(
    "prompt",
    [
        # Product / storage / command / channel / task-status questions that used
        # to hit a hardcoded regex→DIRECT_ANSWER/TASK_QUERY FAQ. (Prompts with
        # figure/qa signal words still map to a declarative workflow recipe, which
        # is a separate, intentional preset — not the removed FAQ vocabulary.)
        "你的存储架构是如何实现的",
        "的存储是怎么架构设计的",
        "你支持哪些命令？科研工作流怎么用？",
        "我想把 Omni 接到微信和飞书，应该怎么配置？",
        "列出最近任务，并告诉我哪个失败了",
    ],
)
def test_non_gate_prompts_go_to_capable_assistant(prompt: str) -> None:
    # Everything that is not a hard gate (explicit skill / memory write / — via
    # the orchestrator — artifact edit) and not a workflow recipe now goes to the
    # model with a real tool catalog instead of a parallel regex vocabulary.
    planner = _builtin_planner()

    plan = planner.plan(prompt, task_id="run-scenario")

    assert plan.intent_type == IntentType.REACT_FALLBACK
    _assert_capable_assistant(plan)


@pytest.mark.parametrize(
    ("prompt", "expected_skill"),
    [
        ("$research-ideation Generate three research ideas about RAG factuality.", "research-ideation"),
        ("$research-pptx Create a group-meeting deck about arXiv 1706.03762.", "research-pptx"),
    ],
)
def test_explicit_skill_selection_has_highest_priority(prompt: str, expected_skill: str) -> None:
    planner = _builtin_planner()

    plan = planner.plan(prompt, task_id="run-explicit")

    assert plan.intent_type == IntentType.SINGLE_SKILL_TASK
    assert [selection.skill for selection in plan.selected_skills] == [expected_skill]
    assert plan.selected_skills[0].selection_source == "explicit"
    assert "explicit skill invocation" in plan.rationale


def test_explicit_skill_key_value_arguments_compile_against_contract(
    tmp_path,
) -> None:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    planner = IntentPlanner(registry)
    pdf = tmp_path / "paper draft.pdf"
    pdf.write_bytes(b"%PDF-test")
    prompt = (
        f'$paper-review input="{pdf}" '
        'venue="ACL 2025 Main Conference — Long Papers" '
        "mode=strict max_visuals=8 skip_visual=false "
        'mineru_command="/opt/mineru-venv/bin/mineru" '
        "mineru_backend=pipeline mineru_timeout_s=600 mineru_device=cuda:3 "
        "review_rag=on preference_rag=on "
        'preference_rag_index="indexes/review-arena-faiss" '
        'preference_rag_data="data/review_arena_clean" preference_rag_top_k=3'
    )

    plan = planner.plan(prompt, task_id="run-explicit-paper-review")
    validation = PlanValidator(registry).validate(plan)

    assert validation.status == "validated"
    assert plan.provider_inputs["paper-review"] == {
        "input": str(pdf),
        "venue": "ACL 2025 Main Conference — Long Papers",
        "mode": "strict",
        "max_visuals": 8,
        "skip_visual": False,
        "mineru_command": "/opt/mineru-venv/bin/mineru",
        "mineru_backend": "pipeline",
        "mineru_timeout_s": 600,
        "mineru_device": "cuda:3",
        "review_rag": "on",
        "preference_rag": "on",
        "preference_rag_index": "indexes/review-arena-faiss",
        "preference_rag_data": "data/review_arena_clean",
        "preference_rag_top_k": 3,
    }


def test_review_capability_cleans_at_attachment_with_spaces(tmp_path) -> None:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    planner = IntentPlanner(registry)
    pdf = tmp_path / "Worldlines in the Mean Field Real Town.pdf"
    pdf.write_bytes(b"%PDF-test")
    prompt = f"@{pdf} 请审稿"
    proposal = ModelPlanProposal(
        intent_type="single_skill_task",
        required_capabilities=["review.paper"],
        capability_inputs={"review.paper": {}},
        confidence=0.95,
        execution_mode="foreground",
        rationale="the user supplied a paper and requested peer review",
    )

    plan = planner.plan_from_proposal(prompt, proposal, task_id="run-at-paper-review")
    validation = PlanValidator(registry).validate(plan)

    assert validation.status == "validated"
    assert plan.intent_type == IntentType.SINGLE_SKILL_TASK
    assert [selection.skill for selection in plan.selected_skills] == ["paper-review"]
    assert plan.provider_inputs["paper-review"]["input"] == str(pdf.resolve())


def test_planner_prompt_distinguishes_scientific_file_input_from_file_management() -> None:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    prompt = _planner_system_prompt(registry)

    assert "scientific capability is not filesystem management" in prompt
    assert "@-prefixed local file as one attachment" in prompt
    assert "status, success/failure, cause, or artifact location" in prompt
    assert 'required_capabilities=["task.inspect"]' in prompt


def test_arxiv_id_does_not_steal_explicit_research_pptx_route() -> None:
    planner = _builtin_planner()

    plan = planner.boundary_plan("$research-pptx Create slides about arXiv 1706.03762", task_id="run-paper")

    assert plan is not None
    assert plan.intent_type == IntentType.SINGLE_SKILL_TASK
    assert [selection.skill for selection in plan.selected_skills] == ["research-pptx"]
    assert plan.selected_skills[0].selection_source == "explicit"


def test_automatic_capability_skips_none_contract_third_party_candidate():
    registry = SkillRegistry(load_settings())
    registry.register(SkillEntry(
        name="unsafe-third-party-lit",
        description="third party search with no contract",
        source="user_claude",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        capabilities=["review.paper"],
        priority=999,
        input_schema={},
        output_schema={},
    ))
    registry.register(_capability_skill("safe-paper-review", "review.paper", source="builtin", priority=10))
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="single_skill_task",
        required_capabilities=["review.paper"],
        outputs=["review"],
        confidence=0.84,
        rationale="model proposed paper review capability",
    )
    plan = planner.plan_from_proposal("Review this paper for NeurIPS.", proposal, task_id="run-contract")

    selected = next(selection for selection in plan.selected_skills if selection.skill == "safe-paper-review")
    assert any(item.skill == "unsafe-third-party-lit" and "contract is none" in item.reason for item in selected.rejected_candidates)


def test_automatic_capability_prefers_core_full_over_high_priority_partial_third_party():
    registry = SkillRegistry(load_settings())
    registry.register(_capability_skill("core-paper-review", "review.paper", source="builtin", priority=1))
    registry.register(_capability_skill("codex-review-supersearch", "review.paper", source="user_codex", priority=999))
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="single_skill_task",
        required_capabilities=["review.paper"],
        outputs=["review"],
        confidence=0.84,
        rationale="model proposed paper review capability",
    )
    plan = planner.plan_from_proposal("Review this paper for NeurIPS.", proposal, task_id="run-core-first")

    assert [selection.skill for selection in plan.selected_skills] == ["core-paper-review"]
    selected = plan.selected_skills[0]
    assert any(
        item.skill == "codex-review-supersearch" and "core full-contract" in item.reason
        for item in selected.rejected_candidates
    )


def test_automatic_capability_can_fallback_to_partial_third_party_when_no_core_skill_exists():
    registry = SkillRegistry(load_settings())
    registry.register(_capability_skill("codex-review-supersearch", "review.paper", source="user_codex", priority=999))
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="single_skill_task",
        required_capabilities=["review.paper"],
        outputs=["review"],
        confidence=0.84,
        rationale="model proposed paper review capability",
    )
    plan = planner.plan_from_proposal("Review this paper for NeurIPS.", proposal, task_id="run-third-party-fallback")
    validation = PlanValidator(registry).validate(plan)

    assert [selection.skill for selection in plan.selected_skills] == ["codex-review-supersearch"]
    assert validation.ok
    assert validation.status == "degraded"
    assert any("contract is partial" in warning for warning in validation.degraded_warnings)


def test_builtin_capability_skill_hard_overrides_same_named_user_and_project():
    """Built-ins win capability resolution even against a higher-priority
    user/project skill declaring the same capability — the ``$user:``/``$<source>:``
    escape is the only way to reach a shadowed skill (see boundary router tests)."""
    registry = SkillRegistry(load_settings())
    # Built-in deliberately has the *lowest* priority to prove source rank
    # dominates the automatic capability tie-break.
    registry.register(_capability_skill("paper-review", "review.paper", source="builtin", priority=1))
    registry.register(_capability_skill("project-paper-review", "review.paper", source="project_omni", priority=500))
    registry.register(_capability_skill("user-paper-review", "review.paper", source="user_omni", priority=999))
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="single_skill_task",
        required_capabilities=["review.paper"],
        outputs=["review"],
        confidence=0.84,
        rationale="model proposed paper review capability",
    )
    plan = planner.plan_from_proposal("Review this paper for NeurIPS.", proposal, task_id="run-builtin-first")

    assert [selection.skill for selection in plan.selected_skills] == ["paper-review"]


def test_automatic_workflow_rejects_required_no_contract_third_party_skill():
    registry = SkillRegistry(load_settings())
    registry.register(SkillEntry(
        name="third-party-search",
        description="third party skill without schemas",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        source="user_claude",
        input_schema={},
        output_schema={},
    ))
    plan = IntentPlan(
        task_id="run-wf",
        user_message="围绕 RAG 做科研 workflow",
        intent_type=IntentType.WORKFLOW,
        selected_skills=[
            SkillSelection(skill="third-party-search", reason="automatic workflow", contract_level="none")
        ],
        workflow_steps=[
            {"id": "third", "skill_name": "third-party-search", "input": {"input": "RAG"}, "depends_on": []}
        ],
        verification_plan=VerificationPlan(required_outputs=["workflow"], required_events=["subtask.submitted"]),
    )

    validation = PlanValidator(registry).validate(plan)

    assert not validation.ok
    assert any("contract is none" in error for error in validation.errors)


def test_explicit_third_party_skill_with_no_contract_is_allowed_but_degraded():
    registry = SkillRegistry(load_settings())
    registry.register(SkillEntry(
        name="third-party-explicit",
        description="explicit third party skill without schemas",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        source="user_claude",
        input_schema={},
        output_schema={},
    ))
    plan = IntentPlan(
        task_id="run-explicit",
        user_message="使用 third-party-explicit",
        intent_type=IntentType.SINGLE_SKILL_TASK,
        selected_skills=[
            SkillSelection(skill="third-party-explicit", reason="user explicitly requested", contract_level="none")
        ],
        verification_plan=VerificationPlan(required_outputs=["artifact"], required_events=["subtask.submitted"]),
    )

    validation = PlanValidator(registry).validate(plan)

    assert validation.ok
    assert validation.status == "degraded"
    assert any("contract is none" in warning for warning in validation.degraded_warnings)
