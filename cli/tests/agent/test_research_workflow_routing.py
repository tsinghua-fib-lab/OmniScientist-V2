"""Multi-stage research requests from CLI/IM channels reach a real workflow run.

The host no longer pre-computes a DAG at planning time. A multi-deliverable
research request lands on the capable ReAct turn and the *model* sequences the
work by calling ``run_workflow`` with its own steps. What must not regress is
the product guarantee at the other end of that call: a durable workflow run
exists, its persisted steps cover every capability the user asked for, the
notification channel is the one the request came from, and identifiers the user
supplied verbatim survive into the step inputs.
"""

from __future__ import annotations

import sys

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
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
    # The gap carries its question in ``reason`` and answers in the language the
    # request was written in, which is what a planner actually returns: the
    # contract offered no ``ask`` field for most of its life, so that is where a
    # model states what it needs.
    return {
        "intent_type": "needs_input",
        "confidence": 0.86,
        "outputs": ["question"],
        "missing_inputs": [
            {
                "field": "research_topic",
                "reason": "the research topic and the target output scope",
            }
        ],
        "rationale": "the requested research workflow lacks a concrete research topic",
    }


def _multi_step_plan() -> dict:
    """Semantic planner classifies the turn as multi-step, and stops there.

    ``workflow`` no longer compiles a DAG: it hands the turn to the capable
    ReAct plan so the model can sequence the steps against live results.
    """
    return {
        "intent_type": "workflow",
        "confidence": 0.9,
        "outputs": ["workflow", "draft.section"],
        "execution_mode": "background",
        "provenance_mode": "light",
        "rationale": "multi-deliverable research request; the model sequences the steps",
    }


_SKILL_FOR_CAPABILITY = {
    "literature.search": "literature-search",
    "paper.fetch.arxiv": "arxiv-fetch",
    "corpus.index": "corpus-index",
    "qa.grounded": "lit-qa",
    "review.paper": "paper-review",
    "artifact.figure": "scientific-figure",
    "evidence.contradiction_scan": "contradiction-scan",
}


def _workflow_steps(
    capabilities: list[str],
    *,
    arxiv_id: str = "",
    topic: str = "Transformer architecture",
) -> list[dict]:
    """The step list the model hands to ``run_workflow``.

    The model names its own provider per step (``synthesis.final`` is the
    native writing executor and carries no skill), which is what the workflow
    tool's contract asks for.
    """
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
        elif capability == "corpus.index":
            # The fixture indexer declares no input fields; a model that reads
            # the provider contract sends none.
            input_data = {}
        elif capability == "synthesis.final":
            input_data = {"topic": topic, "deliverable": "draft.section"}
        step = {
            "id": step_id,
            "capability": capability,
            "input": input_data,
            "depends_on": [previous] if previous else [],
        }
        if capability in _SKILL_FOR_CAPABILITY:
            step["skill"] = _SKILL_FOR_CAPABILITY[capability]
        else:
            step["provider_type"] = "native_executor"
        steps.append(step)
        previous = step_id
    return steps


def _run_workflow_script(steps: list[dict], *, goal: str) -> list[ChatWithToolsResult]:
    """Script one model turn that submits ``steps`` as a background workflow.

    A background submission is terminal for the ReAct loop, so the turn's final
    text is the tool's own submission message — no extra model turn is needed.
    """
    return [
        ChatWithToolsResult(
            tool_calls=[
                ToolCall(
                    id="call_workflow",
                    name="run_workflow",
                    arguments={"goal": goal, "mode": "background", "steps": steps},
                )
            ]
        )
    ]


async def _agent(
    plans: list[dict] | None = None,
    *,
    script: list[ChatWithToolsResult] | None = None,
) -> OmniAgent:
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(plans or [], script=script)
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
async def test_a_step_may_name_the_capability_and_let_the_registry_pick_the_provider() -> None:
    """Deleting the planner must not force the model to know every skill name.

    Capability→provider arbitration used to happen while the planner compiled a
    DAG. It now happens where the model's steps arrive, against the live
    registry, so a step that says what it needs still resolves to who provides
    it — and a capability nobody offers still fails loudly rather than silently
    dropping the deliverable.
    """
    steps = [
        {"id": "search", "capability": "literature.search", "input": {"query": "RAG reranking"}},
        {
            "id": "figure",
            "capability": "artifact.figure",
            "input": {"input": "architecture of the reranking pipeline"},
            "depends_on": ["search"],
        },
    ]
    agent = await _agent(
        [_multi_step_plan()],
        script=_run_workflow_script(steps, goal="survey RAG reranking and draw the pipeline"),
    )
    try:
        result = await agent.handle_turn(
            "检索 RAG reranking 文献，并画出流水线架构图。", channel="cli", drain_tasks=False
        )
        assert result.submitted_workflow_ids
        workflow = await agent.runtime.get_workflow_run(result.submitted_workflow_ids[0])
        assert workflow is not None
        resolved = {step["capability"]: step["skill_name"] for step in workflow.plan_json["steps"]}
        # Whichever provider wins, it has to be one that actually declares the
        # capability — the registry arbitrates, the test does not pin the winner.
        for capability, skill in resolved.items():
            assert skill, f"{capability} resolved to no provider"
            entry = agent.registry.resolve_ref(skill, "")
            assert entry is not None and capability in (entry.capabilities or [])
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_a_capability_no_provider_offers_is_refused_not_silently_dropped() -> None:
    steps = [{"id": "impossible", "capability": "quantum.teleport", "input": {"input": "x"}}]
    agent = await _agent(
        [_multi_step_plan()],
        script=_run_workflow_script(steps, goal="do the impossible"),
    )
    try:
        result = await agent.handle_turn("做点不可能的事。", channel="cli", drain_tasks=False)
        assert not result.submitted_workflow_ids
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_multistage_research_followup_creates_workflow_without_topic_arxiv_fetch() -> None:
    steps = _workflow_steps(
        ["literature.search", "corpus.index", "qa.grounded", "review.paper", "artifact.figure", "synthesis.final"],
        topic="Transformer architecture NeurIPS",
    )
    agent = await _agent(
        [_needs_topic_plan(), _multi_step_plan()],
        script=_run_workflow_script(steps, goal="Transformer architecture NeurIPS section"),
    )
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
        assert len(result.submitted_workflow_ids) == 1
        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 2

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
    steps = _workflow_steps(
        ["literature.search", "paper.fetch.arxiv", "artifact.figure", "synthesis.final"],
        arxiv_id="1706.03762",
        topic="Transformer/RAG related work",
    )
    agent = await _agent(
        [_multi_step_plan()],
        script=_run_workflow_script(steps, goal="Transformer/RAG related work section"),
    )
    try:
        result = await agent.handle_turn(
            "写一个 Transformer/RAG 相关研究小节：先做文献检索，再获取 arXiv 1706.03762，"
            "生成架构图，最后输出论文段落。",
            channel="cli",
            drain_tasks=False,
        )
        assert result.submitted_workflow_ids
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
    steps = _workflow_steps(capabilities, arxiv_id="1706.03762", topic=prompt[:80])
    agent = await _agent(
        [_multi_step_plan()],
        script=_run_workflow_script(steps, goal=prompt),
    )
    try:
        result = await agent.handle_turn(prompt, channel=channel, drain_tasks=False)

        assert len(result.submitted_workflow_ids) == 1
        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 1
        workflow = await agent.runtime.get_workflow_run(result.submitted_workflow_ids[0])
        assert workflow is not None
        assert workflow.notify_channel == channel
        step_capabilities = [step["capability"] for step in workflow.plan_json["steps"]]
        for skill in expected_skills:
            assert capability_plan[skill] in step_capabilities
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_builtin_resolver_keeps_final_synthesis_as_native_deliverable() -> None:
    """``synthesis.final`` stays a native deliverable, never a matched skill.

    The capability→provider resolver runs when the model's ``run_workflow``
    steps are enqueued, so the writing step must still bind to the native
    executor instead of being routed to some superficially similar skill.
    """
    steps = _workflow_steps(
        ["literature.search", "corpus.index", "qa.grounded", "review.paper", "artifact.figure", "synthesis.final"],
        topic="Transformer architecture NeurIPS",
    )
    agent = await _agent(
        [_multi_step_plan()],
        script=_run_workflow_script(steps, goal="Transformer architecture NeurIPS section"),
    )
    try:
        result = await agent.handle_turn(
            "想为transformer 架构的研究主题撰写论文章节，论文的目标期刊是 NeurIPS，"
            "目前没有任何已有的草稿、笔记、大纲，请构建搜索词、抓取相关文献、"
            "做接地问答、评审、绘图和写作",
            channel="feishu",
            drain_tasks=False,
        )
        assert result.submitted_workflow_ids
        workflow = await agent.runtime.get_workflow_run(result.submitted_workflow_ids[0])
        assert workflow is not None
        writing = next(step for step in workflow.plan_json["steps"] if step["id"] == "final_synthesis")
        assert writing["skill_name"] == ""
        assert writing["provider_type"] == "native_executor"
        assert writing["capability"] == "synthesis.final"
    finally:
        await agent.aclose()
