"""IntentPlan routing and skill-selection explanations."""

from __future__ import annotations

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType, SkillSelection, VerificationPlan
from omni.agent.model_planner import (
    _PLANNER_INDEX_PROSE_LIMIT,
    ModelPlanProposal,
    _planner_relevant_contracts,
    _planner_skill_index,
    _planner_system_prompt,
)
from omni.agent.plan_validator import PlanValidator
from omni.agent.planner import IntentPlanner
from omni.agent.workflow_plan_builder import _compose_step_input
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
    registry = SkillRegistry(load_settings(), sources=())
    ranked_names: list[str] = []
    for index in range(9):
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
        user_message=" ".join(ranked_names[:8]),
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
    assert plan.verification_plan.required_outputs == ["answer", "artifact"]
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
    assert plan.verification_plan.forbidden_tools == []


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


def test_model_direct_answer_proposal_runs_without_tools():
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
    assert plan.tool_policy.allowed_tools == []
    assert plan.tool_policy.max_tool_calls == 0


def test_model_workflow_proposal_builds_dag_through_capability_registry():
    registry = SkillRegistry(load_settings())
    registry.register(_capability_skill("core-lit", "literature.search", source="builtin", priority=100))
    registry.register(_capability_skill("core-qa", "qa.grounded", source="builtin", priority=100))
    registry.register(_figure_skill())
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[
            {"id": "lit", "capability": "literature.search", "input": {"input": "RAG hallucination"}},
            {
                "id": "qa",
                "capability": "qa.grounded",
                "depends_on": ["lit"],
                "input": {"input": "Explain mitigation mechanisms"},
            },
            {
                "id": "fig",
                "capability": "artifact.figure",
                "depends_on": ["qa"],
                "input": {"input": "Draw the architecture"},
            },
        ],
        outputs=["workflow"],
        confidence=0.88,
        rationale="model identified a three-step research workflow",
    )
    plan = planner.plan_from_proposal("Research RAG and produce a blueprint", proposal, task_id="run-wf-model")

    assert plan.intent_type == IntentType.WORKFLOW
    assert [step["skill_name"] for step in plan.workflow_steps] == ["core-lit", "core-qa", "scientific-figure"]
    assert plan.workflow_steps[1]["depends_on"] == ["lit"]
    assert plan.workflow_steps[2]["depends_on"] == ["qa"]
    assert plan.tool_policy.max_tool_calls == 4


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


def test_multi_skill_workflow_plan_contains_explicit_dag():
    registry = SkillRegistry(load_settings())
    registry.register(_figure_skill())
    registry.register(_capability_skill("literature-search", "literature.search"))
    registry.register(_capability_skill("corpus-index", "corpus.index"))
    registry.register(_capability_skill("lit-qa", "qa.grounded"))
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[
            {"id": "literature", "capability": "literature.search", "input": {"query": "RAG"}},
            {"id": "corpus", "capability": "corpus.index", "depends_on": ["literature"], "input": {"query": "RAG"}},
            {
                "id": "grounded_qa",
                "capability": "qa.grounded",
                "depends_on": ["literature", "corpus"],
                "input": {"question": "RAG evidence-grounded answer"},
            },
            {"id": "figure", "capability": "artifact.figure", "depends_on": ["grounded_qa"], "input": {"input": "RAG architecture"}},
            {"id": "final_synthesis", "capability": "synthesis.final", "depends_on": ["figure"], "input": {"deliverable": "draft.section"}},
        ],
        outputs=["draft.section"],
        confidence=0.9,
        rationale="model proposed a multi-step research workflow",
    )
    plan = planner.plan_from_proposal(
        "围绕 RAG 做文献检索、语料索引、接地问答、绘图和论文写作 workflow",
        proposal,
        task_id="run-wf",
    )

    assert plan.intent_type == IntentType.WORKFLOW
    assert [step["id"] for step in plan.workflow_steps] == [
        "literature",
        "corpus",
        "grounded_qa",
        "figure",
        "final_synthesis",
    ]
    assert plan.workflow_steps[2]["depends_on"] == ["literature", "corpus"]
    assert plan.workflow_steps[-1]["capability"] == "synthesis.final"
    assert plan.workflow_steps[-1]["provider_type"] == "native_executor"
    assert "draft.section" in plan.outputs
    assert plan.verification_plan.required_tasks == [
        "literature-search",
        "corpus-index",
        "lit-qa",
        "scientific-figure",
    ]
    # Scientific deliverables auto-attach verification: the figure step must render
    # an artifact, provenance is checked to the requested level, and IM delivery
    # must be sent/degraded.
    assert plan.verification_plan.artifact_checks == ["artifact_emitted"]
    assert "light_or_full_as_requested" in plan.verification_plan.provenance_checks
    assert "presentation_sent_or_degraded" in plan.verification_plan.presentation_checks


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


def test_scientific_draft_request_routes_to_native_synthesis() -> None:
    planner = _builtin_planner()

    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[
            {
                "id": "final_synthesis",
                "capability": "synthesis.final",
                "input": {"input": "围绕 RAG 去幻觉撰写论文摘要、方法和实验章节", "deliverable": "draft.section"},
            }
        ],
        outputs=["draft.section"],
        confidence=0.88,
        rationale="model mapped a writing goal to synthesis.final",
    )
    plan = planner.plan_from_proposal(
        "围绕 RAG 去幻觉撰写论文摘要、方法和实验章节。",
        proposal,
        task_id="run-writing",
    )

    assert plan.intent_type == IntentType.WORKFLOW
    assert plan.workflow_steps[-1]["skill_name"] == "synthesis.final"
    assert plan.workflow_steps[-1]["provider_type"] == "native_executor"
    assert "draft.section" in plan.outputs


def test_arxiv_id_does_not_steal_explicit_research_pptx_route() -> None:
    planner = _builtin_planner()

    plan = planner.boundary_plan("$research-pptx Create slides about arXiv 1706.03762", task_id="run-paper")

    assert plan is not None
    assert plan.intent_type == IntentType.SINGLE_SKILL_TASK
    assert [selection.skill for selection in plan.selected_skills] == ["research-pptx"]
    assert plan.selected_skills[0].selection_source == "explicit"


def test_scientific_draft_prompt_is_not_routed_to_review_because_of_draft_word() -> None:
    planner = _builtin_planner()

    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[
            {
                "id": "final_synthesis",
                "capability": "synthesis.final",
                "input": {"input": "撰写论文摘要、方法和实验章节草稿", "deliverable": "draft.section"},
            }
        ],
        outputs=["draft.section"],
        confidence=0.86,
        rationale="model mapped writing to final synthesis deliverable",
    )
    plan = planner.plan_from_proposal("撰写论文摘要、方法和实验章节草稿", proposal, task_id="run-writing")

    assert plan.intent_type == IntentType.WORKFLOW
    assert len(plan.workflow_steps) == 1
    step = plan.workflow_steps[0]
    assert step["capability"] == "synthesis.final"
    assert step["provider_type"] == "native_executor"
    assert step["deliverable"] == "draft.section"
    assert step["skill_name"] == "synthesis.final"


def test_workflow_resolves_steps_from_capabilities_not_hardcoded_skill_names():
    registry = SkillRegistry(load_settings())
    registry.register(_capability_skill("project-literature-engine", "literature.search", priority=200))
    registry.register(_capability_skill("project-grounded-answer", "qa.grounded", priority=200))
    registry.register(_capability_skill("project-figure-engine", "artifact.figure", priority=200))
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[
            {"id": "literature", "capability": "literature.search", "input": {"query": "RAG"}},
            {"id": "answer", "capability": "qa.grounded", "depends_on": ["literature"], "input": {"question": "RAG"}},
            {"id": "figure", "capability": "artifact.figure", "depends_on": ["answer"], "input": {"input": "RAG method flow"}},
        ],
        outputs=["answer", "artifact.figure"],
        confidence=0.9,
        rationale="model proposed capability workflow",
    )
    plan = planner.plan_from_proposal(
        "围绕 RAG 做科研 workflow：先文献检索，再基于证据回答，最后生成方法流程图。",
        proposal,
        task_id="run-capability",
    )

    assert plan.intent_type == IntentType.WORKFLOW
    assert [step["skill_name"] for step in plan.workflow_steps] == [
        "project-literature-engine",
        "project-grounded-answer",
        "project-figure-engine",
    ]
    assert [step["capability"] for step in plan.workflow_steps] == [
        "literature.search",
        "qa.grounded",
        "artifact.figure",
    ]
    assert [selection.selection_source for selection in plan.selected_skills] == ["capability", "capability", "capability"]


def test_automatic_workflow_skips_none_contract_third_party_capability_candidate():
    registry = SkillRegistry(load_settings())
    registry.register(SkillEntry(
        name="unsafe-third-party-lit",
        description="third party search with no contract",
        source="user_claude",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        capabilities=["literature.search"],
        priority=999,
        input_schema={},
        output_schema={},
    ))
    registry.register(_capability_skill("safe-literature-search", "literature.search", source="builtin", priority=10))
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[{"id": "literature", "capability": "literature.search", "input": {"query": "RAG"}}],
        outputs=["sources"],
        confidence=0.84,
        rationale="model proposed literature search capability",
    )
    plan = planner.plan_from_proposal("围绕 RAG 做科研 workflow：先文献检索，再输出综述报告。", proposal, task_id="run-contract")

    assert plan.intent_type == IntentType.WORKFLOW
    literature = next(step for step in plan.workflow_steps if step["capability"] == "literature.search")
    assert literature["skill_name"] == "safe-literature-search"
    selected = next(selection for selection in plan.selected_skills if selection.skill == "safe-literature-search")
    assert any(item.skill == "unsafe-third-party-lit" and "contract is none" in item.reason for item in selected.rejected_candidates)


def test_automatic_workflow_prefers_core_full_over_high_priority_partial_third_party():
    registry = SkillRegistry(load_settings())
    registry.register(_capability_skill("core-literature-engine", "literature.search", source="builtin", priority=1))
    registry.register(_capability_skill("codex-lit-supersearch", "literature.search", source="user_codex", priority=999))
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[{"id": "literature", "capability": "literature.search", "input": {"query": "RAG"}}],
        outputs=["sources"],
        confidence=0.84,
        rationale="model proposed literature search capability",
    )
    plan = planner.plan_from_proposal("围绕 RAG 做科研 workflow：先文献检索，再输出综述报告。", proposal, task_id="run-core-first")

    assert plan.intent_type == IntentType.WORKFLOW
    literature = next(step for step in plan.workflow_steps if step["capability"] == "literature.search")
    assert literature["skill_name"] == "core-literature-engine"
    selected = next(selection for selection in plan.selected_skills if selection.skill == "core-literature-engine")
    assert any(
        item.skill == "codex-lit-supersearch" and "core full-contract" in item.reason
        for item in selected.rejected_candidates
    )


def test_automatic_workflow_can_fallback_to_partial_third_party_when_no_core_skill_exists():
    registry = SkillRegistry(load_settings())
    registry.register(_capability_skill("codex-lit-supersearch", "literature.search", source="user_codex", priority=999))
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[{"id": "literature", "capability": "literature.search", "input": {"query": "RAG"}}],
        outputs=["sources"],
        confidence=0.84,
        rationale="model proposed literature search capability",
    )
    plan = planner.plan_from_proposal("围绕 RAG 做科研 workflow：先文献检索，再输出综述报告。", proposal, task_id="run-third-party-fallback")
    validation = PlanValidator(registry).validate(plan)

    assert plan.intent_type == IntentType.WORKFLOW
    literature = next(step for step in plan.workflow_steps if step["capability"] == "literature.search")
    assert literature["skill_name"] == "codex-lit-supersearch"
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
    registry.register(_capability_skill("literature-search", "literature.search", source="builtin", priority=1))
    registry.register(_capability_skill("project-literature-engine", "literature.search", source="project_omni", priority=500))
    registry.register(_capability_skill("user-literature-engine", "literature.search", source="user_omni", priority=999))
    planner = IntentPlanner(registry)

    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[{"id": "literature", "capability": "literature.search", "input": {"query": "RAG"}}],
        outputs=["sources"],
        confidence=0.84,
        rationale="model proposed literature search capability",
    )
    plan = planner.plan_from_proposal("围绕 RAG 做科研 workflow：先文献检索，再输出综述报告。", proposal, task_id="run-builtin-first")

    assert plan.intent_type == IntentType.WORKFLOW
    literature = next(step for step in plan.workflow_steps if step["capability"] == "literature.search")
    assert literature["skill_name"] == "literature-search"


@pytest.mark.parametrize(
    ("prompt", "workflow_steps", "expected_skills"),
    [
        (
            "围绕 RAG hallucination 做一轮科研 workflow：先检索并收录文献，最后生成架构图。",
            [
                {"id": "literature", "capability": "literature.search", "input": {"query": "RAG hallucination"}},
                {"id": "figure", "capability": "artifact.figure", "depends_on": ["literature"], "input": {"input": "auditable architecture figure"}},
            ],
            ["openalex-search", "scientific-figure"],
        ),
        (
            "给我一个 Transformer 研究小节 workflow：获取 arXiv 1706.03762，"
            "画方法流程图，并撰写 related work 小节。",
            [
                {"id": "paper", "capability": "paper.fetch.arxiv", "input": {"identifier": "1706.03762"}},
                {"id": "figure", "capability": "artifact.figure", "depends_on": ["paper"], "input": {"input": "method flow figure"}},
                {"id": "final_synthesis", "capability": "synthesis.final", "depends_on": ["figure"], "input": {"deliverable": "draft.section"}},
            ],
            ["arxiv-fetch", "scientific-figure", "synthesis.final"],
        ),
        (
            "围绕 RAG reranker 的事实一致性提出研究方向，并制作完整组会幻灯片。",
            [
                {"id": "idea", "capability": "research.ideation", "input": {"input": "RAG reranker factual consistency"}},
                {"id": "slides", "capability": "slides.generate", "depends_on": ["idea"], "input": {"topic": "RAG reranker group meeting"}},
            ],
            ["research-ideation", "research-pptx"],
        ),
    ],
)
def test_real_user_multi_builtin_skill_workflow_scenarios(
    prompt: str,
    workflow_steps: list[dict[str, object]],
    expected_skills: list[str],
) -> None:
    planner = _builtin_planner()

    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=workflow_steps,
        outputs=["workflow"],
        confidence=0.9,
        rationale="model proposed a real research workflow in capability terms",
    )
    plan = planner.plan_from_proposal(prompt, proposal, task_id="run-real-workflow")

    assert plan.intent_type == IntentType.WORKFLOW
    skill_names = [str(step["skill_name"]) for step in plan.workflow_steps]
    for skill in expected_skills:
        assert skill in skill_names
    assert plan.intent_type != IntentType.REACT_FALLBACK
    assert plan.tool_policy.max_tool_calls <= 4


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
