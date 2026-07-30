"""Regression coverage for multi-stage research requests from CLI/IM channels."""

from __future__ import annotations

import sys

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from tests.conftest import PlanningLLM


def _research_skill(name: str, *, required: list[str] | None = None) -> SkillEntry:
    required = list(required or [])
    script = (
        "import json,sys;"
        "d=json.load(sys.stdin);"
        "print(json.dumps({'status':'ok','summary':'ran " + name + "','payload':d}))"
    )
    return SkillEntry(
        name=name,
        description=f"fixture for {name}",
        source="project_omni",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        capabilities=_fixture_capabilities(name),
        priority=500 if name in {"literature-search", "arxiv-fetch", "lit-qa"} else 450,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        input_schema={
            "type": "object",
            "properties": {key: {"type": "string"} for key in required},
            "required": required,
        },
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}, "summary": {"type": "string"}},
        },
    )


def _fixture_capabilities(name: str) -> list[str]:
    return {
        "literature-search": ["literature.search"],
        "arxiv-fetch": ["paper.fetch.arxiv"],
        "corpus-index": ["corpus.index"],
        "lit-qa": ["qa.grounded"],
        "contradiction-scan": ["evidence.contradiction_scan"],
        "paper-review": ["review.paper"],
        "scientific-figure": ["artifact.figure", "figure.architecture"],
    }.get(name, [])


def _needs_topic_plan() -> dict:
    return {
        "intent_type": "needs_input",
        "confidence": 0.86,
        "outputs": ["question"],
        "missing_inputs": [{"field": "research_topic", "reason": "需要研究主题和目标输出范围"}],
        "rationale": "the requested research workflow lacks a concrete research topic",
    }


def _workflow_plan(capabilities: list[str], *, arxiv_id: str = "", topic: str = "Transformer architecture") -> dict:
    steps: list[dict] = []
    previous = ""
    for idx, capability in enumerate(capabilities, start=1):
        step_id = {
            "literature.search": "search",
            "paper.fetch.arxiv": "fetch",
            "corpus.index": "index",
            "qa.grounded": "grounded_qa",
            "review.paper": "review",
            "artifact.figure": "figure",
            "synthesis.final": "final_synthesis",
            "evidence.contradiction_scan": "contradiction",
        }.get(capability, f"step_{idx}")
        input_data: dict[str, str] = {"input": topic}
        if capability == "literature.search":
            input_data = {"query": topic}
        elif capability == "paper.fetch.arxiv":
            input_data = {"identifier": arxiv_id}
        elif capability == "synthesis.final":
            input_data = {"topic": topic, "deliverable": "draft.section"}
        step = {
            "id": step_id,
            "capability": capability,
            "input": input_data,
            "depends_on": [previous] if previous else [],
            "reason": f"test workflow capability {capability}",
        }
        steps.append(step)
        previous = step_id
    return {
        "intent_type": "workflow",
        "confidence": 0.9,
        "workflow_steps": steps,
        "outputs": ["workflow", "draft.section"],
        "execution_mode": "background",
        "provenance_mode": "light",
        "rationale": "semantic planner proposed a capability workflow",
    }


async def _agent(plans: list[dict] | None = None) -> OmniAgent:
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(plans or [])
    for entry in (
        _research_skill("literature-search", required=["query"]),
        _research_skill("arxiv-fetch", required=["identifier"]),
        _research_skill("corpus-index"),
        _research_skill("lit-qa", required=["input"]),
        _research_skill("contradiction-scan", required=["input"]),
        _research_skill("paper-review", required=["input"]),
        _research_skill("scientific-figure", required=["input"]),
    ):
        agent.registry.register(entry)
    return agent


@pytest.mark.asyncio
async def test_multistage_research_prompt_needs_topic_before_task() -> None:
    agent = await _agent([_needs_topic_plan()])
    try:
        result = await agent.handle_turn(
            "Prepare a submission section with search, fetch, index, grounded QA, review, figure, writing.",
            channel="feishu",
            drain_tasks=False,
        )
        assert result.kind == "needs_input"
        assert not result.submitted_subtask_ids
        assert not result.submitted_workflow_ids
        assert "research topic" in result.text
        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 1
        assert agent.llm.calls == 0
        assert await agent.runtime.list_subtasks(limit=10) == []
        run = await agent.tasks.get_task(result.task_id)
        assert run is not None
        assert run.status == "needs_input"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_multistage_research_followup_creates_workflow_without_topic_arxiv_fetch() -> None:
    agent = await _agent([
        _needs_topic_plan(),
        _workflow_plan(
            ["literature.search", "corpus.index", "qa.grounded", "review.paper", "artifact.figure", "synthesis.final"],
            topic="Transformer architecture NeurIPS",
        ),
    ])
    try:
        first = await agent.handle_turn(
            "Prepare a submission section with search, fetch, index, grounded QA, review, figure, writing.",
            channel="feishu",
            drain_tasks=False,
        )
        result = await agent.handle_turn(
            "想为transformer 架构的研究主题撰写论文章节，论文的目标期刊是 NeurIPS，"
            "目前没有任何已有的草稿、笔记、大纲，请根据上面的输入构建搜索词、"
            "抓取相关文献、做接地问答、评审、绘图和写作",
            session_id=first.session_id,
            channel="feishu",
            drain_tasks=False,
        )
        assert result.kind == "workflow"
        assert len(result.submitted_workflow_ids) == 1
        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 2
        assert agent.llm.calls == 0

        workflow = await agent.runtime.get_workflow_run(result.submitted_workflow_ids[0])
        assert workflow is not None
        steps = workflow.plan_json["steps"]
        capabilities = [step["capability"] for step in steps]
        assert "literature.search" in capabilities
        assert "corpus.index" in capabilities
        assert "qa.grounded" in capabilities
        assert "review.paper" in capabilities
        assert "artifact.figure" in capabilities
        assert "synthesis.final" in capabilities
        assert "paper.fetch.arxiv" not in capabilities
        assert workflow.notify_channel == "feishu"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_multistage_research_with_explicit_arxiv_id_includes_arxiv_fetch() -> None:
    agent = await _agent([
        _workflow_plan(
            ["literature.search", "paper.fetch.arxiv", "artifact.figure", "synthesis.final"],
            arxiv_id="1706.03762",
            topic="Transformer/RAG related work",
        )
    ])
    try:
        result = await agent.handle_turn(
            "写一个 Transformer/RAG 相关研究小节：先做文献检索，再获取 arXiv 1706.03762，"
            "生成架构图，最后输出论文段落。",
            channel="cli",
            drain_tasks=False,
        )
        assert result.kind == "workflow"
        workflow = await agent.runtime.get_workflow_run(result.submitted_workflow_ids[0])
        assert workflow is not None
        steps = workflow.plan_json["steps"]
        arxiv_steps = [step for step in steps if step["capability"] == "paper.fetch.arxiv"]
        assert len(arxiv_steps) == 1
        assert arxiv_steps[0]["input"]["identifier"] == "1706.03762"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "expected_skills", "channel"),
    [
        (
            "围绕 RAG hallucination 做一轮科研 workflow：先文献检索并收录语料，"
            "再做接地问答，最后生成一张可审计架构图。",
            ["literature-search", "corpus-index", "lit-qa", "scientific-figure"],
            "wechat",
        ),
        (
            "给我一个 Transformer 研究小节 workflow：获取 arXiv 1706.03762，"
            "画方法流程图，并撰写 related work 小节。",
            ["arxiv-fetch", "scientific-figure", "synthesis.final"],
            "cli",
        ),
        (
            "围绕 RAG reranker 的事实一致性做科研审查：检索文献、基于证据回答、"
            "扫描冲突证据，并像审稿人一样指出严重问题。",
            ["literature-search", "lit-qa", "contradiction-scan", "paper-review"],
            "feishu",
        ),
    ],
)
async def test_real_user_prompts_can_submit_multi_builtin_skill_workflows(
    prompt: str,
    expected_skills: list[str],
    channel: str,
) -> None:
    capability_plan = {
        "literature-search": "literature.search",
        "arxiv-fetch": "paper.fetch.arxiv",
        "corpus-index": "corpus.index",
        "lit-qa": "qa.grounded",
        "paper-review": "review.paper",
        "contradiction-scan": "evidence.contradiction_scan",
        "scientific-figure": "artifact.figure",
        "synthesis.final": "synthesis.final",
    }
    capabilities = [capability_plan[skill] for skill in expected_skills]
    agent = await _agent([_workflow_plan(capabilities, arxiv_id="1706.03762", topic=prompt[:80])])
    try:
        result = await agent.handle_turn(prompt, channel=channel, drain_tasks=False)

        assert result.kind == "workflow"
        assert len(result.submitted_workflow_ids) == 1
        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 1
        assert agent.llm.calls == 0
        workflow = await agent.runtime.get_workflow_run(result.submitted_workflow_ids[0])
        assert workflow is not None
        assert workflow.notify_channel == channel
        step_capabilities = [step["capability"] for step in workflow.plan_json["steps"]]
        for skill in expected_skills:
            assert capability_plan[skill] in step_capabilities
    finally:
        await agent.aclose()


def _arxiv_fetch_support() -> SkillEntry:
    """arxiv-fetch faithful to the real skill: support role, arxiv_id-typed input."""
    script = "import json,sys;print(json.dumps({'status':'ok','summary':'fetched'}))"
    return SkillEntry(
        name="arxiv-fetch",
        description="fetch arXiv abstract by id/url",
        source="builtin",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role="support",
        capabilities=["paper.fetch.arxiv"],
        priority=500,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        input_schema={
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "format": "arxiv_id",
                    "aliases": ["id", "url"],
                    "x-omni": {"repair_capability": "literature.search"},
                }
            },
            "required": ["identifier"],
        },
        output_schema={"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]},
        workflow={"failure_policy": "continue_with_partial"},
    )


@pytest.mark.asyncio
async def test_arxiv_title_workflow_without_resolvable_id_hands_off_to_react_lookup() -> None:
    # The reported bug: a multilingual "fetch this title, summarize it, and create
    # a figure" request used to hard-stop with plan_validation_failed because
    # arxiv-fetch had no arXiv id — and the earlier fix rewrote it into a lossy
    # whole-sentence search. Under "look-up before ask/error": in-lane resolution
    # is offline here (mock provider), so the identifier hands off to the ReAct
    # floor to act-and-look-up (search → id → fetch), never a lossy rewrite and
    # never a plan-validation failure. The skill's contract message never leaks.
    plan = {
        "intent_type": "workflow",
        "confidence": 0.88,
        "workflow_steps": [
            {"id": "fetch", "capability": "paper.fetch.arxiv",
             "input": {"identifier": "Attention Is All You Need"},
             "depends_on": [], "reason": "user asked for the paper abstract"},
            {"id": "figure", "capability": "artifact.figure",
             "input": {"input": "query/retriever/reranker/LLM 科研架构图"},
             "depends_on": ["fetch"], "reason": "architecture diagram"},
        ],
        "outputs": ["workflow"],
        "execution_mode": "background",
        "provenance_mode": "light",
        "rationale": "fetch abstract then draw architecture",
    }
    agent = await _agent([plan])
    agent.registry.register(_arxiv_fetch_support())
    try:
        result = await agent.handle_turn(
            "为 RAG 系统综述准备材料：获取 Attention Is All You Need 摘要，"
            "并生成包含 query、retriever、reranker、LLM 的科研架构图。",
            channel="cli",
            drain_tasks=False,
        )

        assert result.terminated_reason != "plan_validation_failed"
        # The skill's contract "arXiv id or URL is required" message is a self-heal
        # signal for the engine — it must never surface as a user-facing warning.
        assert all("arXiv id or URL is required" not in w for w in result.degraded_warnings)

        events = await agent.tasks.list_events(result.task_id)
        recovery = [event for event in events if event.event_type == "plan.recovery"]
        assert recovery
        assert recovery[-1].output_json["action"] == "react"
        assert recovery[-1].output_json["rung"] == "4_react_lookup"
        # The floor is told to resolve the extracted title, not the whole goal,
        # and never to run a lossy free-text literature search rewrite.
        notes = recovery[-1].output_json.get("notes") or []
        assert any("Attention Is All You Need" in note for note in notes)
    finally:
        await agent.aclose()


def _figure_skill_full() -> SkillEntry:
    """scientific-figure faithful to the real skill: input/title/figure_kind schema."""
    script = "import json,sys;print(json.dumps({'status':'ok','summary':'figure'}))"
    return SkillEntry(
        name="scientific-figure",
        description="generate a publication-quality scientific figure",
        source="builtin",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role="task",
        capabilities=["artifact.figure", "figure.architecture"],
        priority=500,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        input_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "title": {"type": "string"},
                "figure_kind": {"type": "string", "enum": ["generic", "rag", "transformer"]},
            },
            "required": ["input"],
        },
        output_schema={"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]},
        workflow={"failure_policy": "continue_with_partial"},
    )


@pytest.mark.asyncio
async def test_figure_workflow_step_seeds_input_from_capability_inputs_and_goal() -> None:
    # The reported regression: the real semantic planner emits the figure's
    # contract fields in ``capability_inputs`` (title/figure_kind)
    # and leaves the workflow step's ``input`` empty. Once workflow-step inputs
    # were compiled strictly against the provider schema with no goal fallback,
    # that empty step failed its ``required: [input]`` contract, was classified
    # as degradable, and got pruned ("lacks required input") — so no figure was
    # produced. The planner must now fill the step input explicitly at plan
    # time: fold in contract-declared capability_inputs and seed the
    # required free-text ``input`` from the user goal.
    goal = (
        "为 RAG 系统综述准备材料：获取 Attention Is All You Need 摘要，"
        "并生成包含 query、retriever、reranker、LLM 的科研架构图。"
    )
    plan = {
        "intent_type": "workflow",
        "confidence": 0.9,
        "workflow_steps": [
            {"id": "fetch", "capability": "paper.fetch.arxiv",
             "input": {"identifier": "1706.03762"},
             "depends_on": [], "reason": "user asked for the paper abstract"},
            {"id": "generate_rag_architecture", "capability": "artifact.figure",
             "input": {}, "depends_on": ["fetch"], "reason": "RAG architecture diagram"},
        ],
        "capability_inputs": {
            "artifact.figure": {
                "title": "RAG Architecture",
                "figure_kind": "rag",
            }
        },
        "outputs": ["workflow"],
        "execution_mode": "background",
        "provenance_mode": "light",
        "rationale": "fetch abstract then draw the RAG architecture",
    }
    agent = await _agent([plan])
    agent.registry.register(_figure_skill_full())
    try:
        result = await agent.handle_turn(goal, channel="cli", drain_tasks=False)

        assert result.kind == "workflow"
        assert result.submitted_workflow_ids
        # The figure step must survive (not pruned for "lacks required input").
        assert not any("lacks required input" in warning for warning in result.degraded_warnings)

        workflow = await agent.runtime.get_workflow_run(result.submitted_workflow_ids[0])
        assert workflow is not None
        steps = workflow.plan_json["steps"]
        assert [step["capability"] for step in steps].count("artifact.figure") == 1
        figure = next(step for step in steps if step["capability"] == "artifact.figure")
        # Explicit planner-time population: required free-text input seeded from
        # the goal, while the model-bound contract field stays unchanged.
        assert goal in str(figure["input"].get("input") or "")
        assert figure["input"].get("figure_kind") == "rag"
        assert figure["input"].get("title") == "RAG Architecture"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_builtin_resolver_keeps_final_synthesis_as_native_deliverable() -> None:
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM([
        _workflow_plan(
            ["literature.search", "corpus.index", "qa.grounded", "review.paper", "artifact.figure", "synthesis.final"],
            topic="Transformer architecture NeurIPS",
        )
    ])
    try:
        result = await agent.handle_turn(
            "想为transformer 架构的研究主题撰写论文章节，论文的目标期刊是 NeurIPS，"
            "目前没有任何已有的草稿、笔记、大纲，请构建搜索词、抓取相关文献、"
            "做接地问答、评审、绘图和写作",
            channel="feishu",
            drain_tasks=False,
        )
        assert result.kind == "workflow"
        workflow = await agent.runtime.get_workflow_run(result.submitted_workflow_ids[0])
        assert workflow is not None
        writing = next(step for step in workflow.plan_json["steps"] if step["id"] == "final_synthesis")
        assert writing["skill_name"] == ""
        assert writing["provider_type"] == "native_executor"
        assert writing["capability"] == "synthesis.final"
    finally:
        await agent.aclose()
