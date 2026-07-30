"""Orchestrator integration for IntentPlan-driven direct execution."""

from __future__ import annotations

import json
import sys

import pytest

import omni.agent.orchestrator as orchestrator_mod
from omni.agent import OmniAgent
from omni.agent.intent_plan import (
    ContextPolicy,
    IntentPlan,
    IntentType,
    ToolPolicy,
    VerificationPlan,
)
from omni.config import load_settings
from omni.core.approval import ApprovalDecision, ApprovalRequest
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.runtime.presentation import turn_presentation_from_result
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from omni.storage.models import TaskORM
from tests.conftest import CapturingLLM, PlanningLLM, ScriptedLLM


def _figure_async_skill() -> SkillEntry:
    script = (
        "import json,sys;"
        "d=json.load(sys.stdin);"
        "print(json.dumps({'status':'ok','summary':'figure '+d.get('title','RAG'),"
        "'artifacts':[]}, ensure_ascii=False))"
    )
    return SkillEntry(
        name="scientific-figure",
        description="Generate publication-ready scientific architecture diagrams.",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        capabilities=["artifact.figure", "figure.architecture", "artifact.svg", "artifact.png"],
        priority=100,
        default_for=["architecture diagram", "架构图", "RAG architecture"],
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
        output_schema={"type": "object", "properties": {"summary": {"type": "string"}, "artifacts": {"type": "array"}}},
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


def _schema_echo_skill(name: str, *, required: list[str]) -> SkillEntry:
    script = (
        "import json,sys;"
        "d=json.load(sys.stdin);"
        "print(json.dumps({'status':'ok','summary':'payload ok','payload':d}, ensure_ascii=False))"
    )
    properties = {field: {"type": "string"} for field in required}
    if name == "arxiv-fetch" and "identifier" in properties:
        properties["identifier"] = {
            "type": "string",
            "format": "arxiv_id",
            "aliases": ["arxiv_id", "id", "url"],
        }
    return SkillEntry(
        name=name,
        description=f"schema echo for {name}",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        capabilities=_fixture_capabilities(name),
        priority=90,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": list(required),
        },
        output_schema={"type": "object", "properties": {"status": {"type": "string"}, "summary": {"type": "string"}}},
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


def _fixture_capabilities(name: str) -> list[str]:
    return {
        "literature-search": ["literature.search"],
        "arxiv-fetch": ["paper.fetch.arxiv"],
        "corpus-index": ["corpus.index"],
        "lit-qa": ["qa.grounded"],
        "contradiction-scan": ["evidence.contradiction_scan"],
        "paper-review": ["review.paper"],
        "paper-analysis": ["analysis.paper"],
        "scientific-figure": ["artifact.figure", "figure.architecture"],
    }.get(name, [])


@pytest.mark.asyncio
async def test_rag_qa_plus_figure_uses_plan_executor_not_wide_react():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_figure_async_skill())
    agent.llm = PlanningLLM(
        {
            "intent_type": "qa_plus_artifact",
            "confidence": 0.94,
            "required_capabilities": ["qa.grounded", "artifact.figure"],
            "outputs": ["answer", "artifact"],
            "execution_mode": "background",
            "rationale": "semantic planner selected answer plus figure",
        }
    )
    prompt = "RAG 如何降低幻觉，并给我生成一份目前全球范围最流行的 RAG 构建的架构图。"
    try:
        turn = await agent.handle_turn(prompt, channel="wechat", drain_tasks=False)

        assert turn.task_id
        assert turn.submitted_subtask_ids
        assert "scientific-figure" in turn.text
        assert f"/task show {turn.task_id[:8]}" in turn.text
        assert f"/task show {turn.submitted_subtask_ids[0][:8]}" not in turn.text
        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 1
        assert agent.llm.calls == 0

        run = await agent.tasks.get_task(turn.task_id)
        assert run is not None
        assert run.status == "running"
        child = await agent.runtime.get_subtask(turn.submitted_subtask_ids[0])
        assert child is not None
        assert child.task_id == run.id
        assert child.skill_name == "scientific-figure"

        events = await agent.tasks.list_events(run.id)
        event_types = [event.event_type for event in events]
        assert "task.ack" in event_types
        assert "plan.created" in event_types
        assert "plan.validated" in event_types
        assert "plan.executed" in event_types
        assert "plan.tool.done" in event_types
        assert "subtask.submitted" in event_types
        assert not any(event.event_type.startswith("react.tool") for event in events)
        assert not any(event.tool_name == "glob" for event in events)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_open_semantic_request_uses_model_proposal_then_registry_core_skill():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_figure_async_skill())
    agent.registry.register(
        SkillEntry(
            name="user-fancy-diagram",
            description="third-party diagram generator",
            source="user_codex",
            kind=SkillKind.CLI_EXEC,
            delivery_mode=DeliveryMode.ASYNC_TASK,
            capabilities=["artifact.figure"],
            priority=999,
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
        )
    )
    agent.llm = PlanningLLM(
        {
            "intent_type": "qa_plus_artifact",
            "confidence": 0.93,
            "required_capabilities": ["qa.grounded", "artifact.figure"],
            "outputs": ["answer", "artifact"],
            "execution_mode": "background",
            "provenance_mode": "light",
            "rationale": "model mapped the open research request to grounded QA plus a figure",
        }
    )
    try:
        turn = await agent.handle_turn(
            "RAG hallucination mitigation with a production system blueprint",
            channel="feishu",
            drain_tasks=False,
        )

        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 1
        assert agent.llm.calls == 0
        assert turn.task_id
        assert turn.submitted_subtask_ids
        assert f"/task show {turn.task_id[:8]}" in turn.text
        assert f"/task show {turn.submitted_subtask_ids[0][:8]}" not in turn.text
        task = await agent.runtime.get_subtask(turn.submitted_subtask_ids[0])
        assert task is not None
        assert task.skill_name == "scientific-figure"
        assert "model mapped" in turn.plan_summary

        events = await agent.tasks.list_events(turn.task_id)
        event_types = [event.event_type for event in events]
        assert "plan.model.proposed" in event_types
        plan_event = next(event for event in events if event.event_type == "plan.created")
        selected = plan_event.output_json["selected_skills"][0]
        assert selected["skill"] == "scientific-figure"
        assert any(item["skill"] == "user-fancy-diagram" for item in selected["rejected_candidates"])
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_model_planner_invalid_json_falls_back_to_bounded_react():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = CapturingLLM([ChatWithToolsResult(content="这是一个受限兜底回答。")])
    agent.llm = llm
    try:
        turn = await agent.handle_turn("RAG 的核心思路是否合理", channel="cli", drain_tasks=False)

        assert turn.text == "这是一个受限兜底回答。"
        # Model JSON was unparseable → capable bounded assistant (not a 1-tool stub).
        for name in ("search_corpus", "read_file", "glob"):
            assert name in llm.tool_names
        # Falling back does not widen what the gate can settle: a write is
        # decided by its destination, a shell command has none to decide on.
        for name in ("bash", "run_compute"):
            assert name not in llm.tool_names
        events = await agent.tasks.list_events(turn.task_id)
        assert any(event.event_type == "plan.model.degraded" for event in events)
        validated = next(event for event in events if event.event_type == "plan.validated")
        assert validated.output_json["tool_policy"]["allowed_tools"] is None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_unresolved_planner_capability_recovers_to_bash_and_approval() -> None:
    """Regression for bceebef0: a hollow route must not erase the tool catalog."""
    agent = await OmniAgent.create(load_settings())
    agent.llm = _TranscriptLLM(
        {
            "intent_type": "react_fallback",
            "required_capabilities": ["react_fallback"],
            "outputs": ["code_review_report"],
            "confidence": 0.9,
            "rationale": "review the repository with Git",
        },
        script=[
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "git-log",
                        "bash",
                        {"command": "git log --oneline -1"},
                    )
                ]
            ),
            ChatWithToolsResult(content="review complete"),
        ],
    )

    async def unexpected_prompt(_request: ApprovalRequest) -> ApprovalDecision:
        pytest.fail("a known-safe Git read should auto-approve")

    agent.approver = unexpected_prompt
    session_id = "repeat-code-review"
    try:
        await agent._persist_message(  # noqa: SLF001 - seed the incident's history
            session_id,
            "assistant",
            "Previous review: the earlier commits looked correct.",
        )
        turn = await agent.handle_turn(
            "Review today's commits on master.",
            session_id=session_id,
            channel="cli",
            drain_tasks=False,
        )

        assert turn.text == "review complete"
        assert "bash" in agent.llm.tool_names
        assert turn.terminated_reason != "synthesized_max_tool_calls"
        seeded = "\n".join(
            str(message.get("content") or "")
            for message in agent.llm.transcripts[0]
        )
        assert "Previous review" in seeded
        assert "derive the answer in this turn" in seeded
        events = await agent.tasks.list_events(turn.task_id)
        kinds = [event.event_type for event in events]
        assert "approval.auto" in kinds
        assert "approval.requested" not in kinds
        assert not any(
            event.output_json.get("error_code") == "unknown_tool"
            for event in events
        )
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_arxiv_fetch_direct_route_extracts_identifier_for_skill_schema():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_schema_echo_skill("arxiv-fetch", required=["identifier"]))
    agent.llm = PlanningLLM(
        {
            "intent_type": "single_skill_task",
            "confidence": 0.92,
            "required_capabilities": ["paper.fetch.arxiv"],
            "outputs": ["sources", "answer"],
            "execution_mode": "background",
            "rationale": "user requested a concrete arXiv paper summary",
        }
    )
    try:
        turn = await agent.handle_turn("获取 arXiv 1706.03762 的摘要", channel="cli", drain_tasks=True)

        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 1
        assert agent.llm.calls == 0
        assert turn.submitted_subtask_ids
        task = await agent.runtime.get_subtask(turn.submitted_subtask_ids[0])
        assert task is not None
        assert task.status == "succeeded"
        assert task.input_json["identifier"] == "1706.03762"
        assert "input" not in task.input_json
        run = await agent.tasks.get_task(turn.task_id)
        assert run is not None
        assert run.status == "succeeded"
        assert turn.settlement_status == "succeeded"
        events = await agent.tasks.list_events(turn.task_id)
        event_types = [event.event_type for event in events]
        assert "task.succeeded" in event_types
        assert "task.failed" not in event_types
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_drained_plan_executor_records_plan_executed_before_the_task_settles():
    """The executed plan is on record before the run commits a terminal status.

    Settlement reads the durable record to decide what the run earned, so
    anything the run did has to be written first. If ``plan.executed`` landed
    after ``task.succeeded``, the status would have been decided against an
    incomplete record and `/task show` would show work that happened after the
    run was already closed.
    """
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_figure_async_skill())
    agent.llm = PlanningLLM(
        {
            "intent_type": "qa_plus_artifact",
            "confidence": 0.9,
            "required_capabilities": ["qa.grounded", "artifact.figure"],
            "outputs": ["answer", "artifact"],
            "execution_mode": "background",
            "rationale": "semantic planner selected answer plus figure",
        }
    )
    try:
        turn = await agent.handle_turn(
            "RAG 如何降低幻觉，并生成一张目前常见的 RAG 架构图",
            channel="cli",
            drain_tasks=True,
        )

        run = await agent.tasks.get_task(turn.task_id)
        assert run is not None
        assert run.status == "succeeded"
        events = await agent.tasks.list_events(turn.task_id)
        event_types = [event.event_type for event in events]
        assert "plan.executed" in event_types
        assert event_types.index("plan.executed") < event_types.index("task.succeeded")
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_memory_write_uses_semantic_plan_and_skips_react():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.llm = PlanningLLM(
        {
            "intent_type": "memory_update",
            "confidence": 0.95,
            "required_capabilities": ["memory.update"],
            "outputs": ["memory"],
            "capability_inputs": {
                "memory.update": {
                    "content": "This project studies how RAG rerankers affect factual consistency."
                }
            },
            "rationale": "the user asked to retain a project fact",
        }
    )
    try:
        remembered = await agent.handle_turn(
            "帮我记住：本项目研究 RAG reranker 对事实一致性的影响",
            channel="feishu",
            drain_tasks=False,
        )
        assert agent.llm.plan_calls == 1
        assert agent.llm.calls == 0
        assert not remembered.submitted_subtask_ids
        memories = await agent.memory.recall("reranker", limit=3, cross_session=True)
        assert any("RAG rerankers" in item.entry.summary for item in memories)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_recent_tasks_question_reaches_capable_assistant_with_run_tools():
    # Task/product questions no longer hit a hardcoded FAQ/executor: they go to
    # the model, which is handed the run/task recall tools and decides.
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = CapturingLLM([ChatWithToolsResult(content="最近的任务如下。")])
    agent.llm = llm
    try:
        session_id = await agent.ensure_session(channel="cli")
        failed = await agent.tasks.create_task(
            session_id=session_id,
            channel="cli",
            user_input="失败的旧任务",
        )
        await agent.tasks.finish_task(failed.id, status="failed", summary="旧任务失败", error="boom")

        turn = await agent.handle_turn(
            "列出最近任务，并告诉我哪个失败了", session_id=session_id, channel="cli", drain_tasks=False
        )

        assert turn.text == "最近的任务如下。"
        assert llm.calls == 1
        assert "list_recent_tasks" in llm.tool_names
        assert "get_subtask" in llm.tool_names
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_default_turn_uses_capable_tool_catalog():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = CapturingLLM([ChatWithToolsResult(content="这是一个概念性回答。")])
    agent.llm = llm
    try:
        turn = await agent.handle_turn("RAG 的核心思路是否合理", channel="cli", drain_tasks=False)

        assert turn.text == "这是一个概念性回答。"
        # Capable-but-safe: real read/recall/search catalog, and of the mutations
        # only those the gate can settle on its own. A write names the file it
        # will change, so an in-workspace one is auto-approved with no human; a
        # shell command names nothing to assess and stays out with no approver.
        for name in ("search_corpus", "read_file", "glob", "open_artifact", "list_recent_tasks"):
            assert name in llm.tool_names
        for name in ("write_file", "edit_file"):
            assert name in llm.tool_names
        for name in ("bash", "run_compute"):
            assert name not in llm.tool_names
        events = await agent.tasks.list_events(turn.task_id)
        plan_event = next(event for event in events if event.event_type == "plan.validated")
        policy = plan_event.output_json["tool_policy"]
        assert policy["allowed_tools"] is None
        assert "write_file" in policy["blocked_tools"]
        event_types = [event.event_type for event in events]
        assert event_types.index("execution.finished") < event_types.index("react.finished")
        assert event_types.index("react.finished") < event_types.index("assistant.message")
        finished = next(event for event in events if event.event_type == "react.finished")
        assert finished.output_json["tool_budget"]["requested"] == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_reference_to_prior_figure_reaches_planner_and_resolves_without_asking():
    """Fix 1+2: a fresh turn that references a past figure sees it in the planner
    context (recent-activity digest) and proceeds — no history-blind needs_input."""
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    captured: dict[str, str] = {}

    class _ContextAwarePlanner(CapturingLLM):
        async def chat(self, system: str, user: str, **kwargs):  # noqa: ANN003
            if "semantic intent planner" in system.lower():
                captured["planner_user"] = user
                # If the planner can see the referent it binds a capable tool
                # turn; with no context it would have to ask.
                if "架构图" in user:
                    return json.dumps(
                        {"intent_type": "react_fallback", "confidence": 0.6,
                         "rationale": "resolve the referenced figure with tools"},
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {"intent_type": "needs_input",
                     "missing_inputs": [{"field": "request", "reason": "which figure?"}],
                     "rationale": "no context"},
                    ensure_ascii=False,
                )
            return await super().chat(system, user, **kwargs)

    agent.llm = _ContextAwarePlanner(
        [ChatWithToolsResult(content="这是你之前的 RAG 系统架构图，已为你重新生成。")]
    )
    try:
        art = await agent.artifacts.put_bytes(
            b"<svg/>", kind="figure", title="RAG 系统架构图", ext="svg", session_id="past"
        )
        async with agent.db.session() as ss:
            ss.add(TaskORM(
                id="pastfig0923", kind="turn", channel="cli", status="succeeded",
                session_id="past", title="生成 RAG 系统架构图",
                artifact_ids=[art.uri.removeprefix("artifact://")],
            ))
            await ss.commit()

        turn = await agent.handle_turn(
            "你最近给我生成的架构图是讲的什么啊，给我重新生成一份吧",
            channel="cli",
            drain_tasks=False,
        )

        # Fix 1+2: the cross-session recent-activity digest reached the planner.
        assert "生成 RAG 系统架构图" in captured.get("planner_user", "")
        # The turn resolved with tools instead of asking the user to re-clarify.
        assert turn.kind != "needs_input"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_vague_context_question_goes_to_capable_react():
    # Clarification is the model's job now, not a regex "vague" needs_input gate.
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = CapturingLLM([ChatWithToolsResult(content="需要更多信息，请补充设计目标。")])
    agent.llm = llm
    try:
        turn = await agent.handle_turn("这个怎么设计？", channel="wechat", drain_tasks=False)

        assert llm.calls == 1
        assert turn.text
        events = await agent.tasks.list_events(turn.task_id)
        assert any(event.event_type == "react.finished" for event in events)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_fallback_tool_limit_keeps_audit_events_but_im_hides_trace():
    settings = load_settings()
    settings.react.max_tool_calls = 1
    agent = await OmniAgent.create(settings)
    agent.llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("search1", "search_corpus", {"query": "RAG", "k": 1})]),
        ChatWithToolsResult(tool_calls=[ToolCall("search2", "search_corpus", {"query": "RAG again", "k": 1})]),
    ])
    try:
        turn = await agent.handle_turn("RAG 的核心思路是否合理", channel="wechat", drain_tasks=False)

        assert turn.kind == "text"
        assert turn.terminated_reason == "synthesized_max_tool_calls"
        events = await agent.tasks.list_events(turn.task_id)
        assert any(event.event_type == "react.tool.done" and event.tool_name == "search_corpus" for event in events)
        finished = [event for event in events if event.event_type == "react.finished"]
        assert finished
        assert finished[-1].output_json["terminated_reason"] == "synthesized_max_tool_calls"

        presentation = turn_presentation_from_result(turn, channel="wechat")
        rendered = presentation.to_markdown()
        # The IM reader gets the bounded-run notice and a task id to follow up
        # with, but never the tool trace the CLI shows inline.
        assert "search_corpus" not in rendered
        assert "converged on the available result" in rendered
        assert turn.task_id[:8] in rendered
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_explicit_file_question_can_use_read_only_workspace_tools():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = CapturingLLM([ChatWithToolsResult(content="可以检查该文件。")])
    agent.llm = llm
    try:
        await agent.handle_turn("请读取 cli/src/omni/agent/planner.py 看看这个文件怎么设计", channel="cli", drain_tasks=False)

        assert "read_file" in llm.tool_names
        assert "glob" in llm.tool_names
        assert "open_artifact" in llm.tool_names
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "skill"),
    [
        ("检索最近关于 RAG 评估基准的文献并给我可引用结论", "literature-search"),
        ("检查这篇 RAG 草稿是否像审稿人会指出严重问题", "paper-review"),
        ("这个结论有没有反例或冲突证据？", "contradiction-scan"),
    ],
)
async def test_research_skill_scenario_prompts_submit_expected_child_skill(prompt: str, skill: str):
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_schema_echo_skill(skill, required=["input"]))
    capability = _fixture_capabilities(skill)[0]
    agent.llm = PlanningLLM(
        {
            "intent_type": "single_skill_task",
            "confidence": 0.9,
            "required_capabilities": [capability],
            "outputs": ["answer"],
            "execution_mode": "background",
            "rationale": f"semantic planner selected {capability}",
        }
    )
    try:
        turn = await agent.handle_turn(prompt, channel="cli", drain_tasks=False)

        assert isinstance(agent.llm, PlanningLLM)
        assert agent.llm.plan_calls == 1
        assert agent.llm.calls == 0
        assert turn.submitted_subtask_ids
        task = await agent.runtime.get_subtask(turn.submitted_subtask_ids[0])
        assert task is not None
        assert task.skill_name == skill
        assert task.input_json["input"] == prompt
        presentation = turn_presentation_from_result(turn)
        assert presentation.tasks
        assert presentation.tasks[0].skill == skill
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_plan_event_records_structured_skill_selection_reason():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_figure_async_skill())
    agent.llm = PlanningLLM(
        {
            "intent_type": "qa_plus_artifact",
            "confidence": 0.91,
            "required_capabilities": ["artifact.figure"],
            "outputs": ["artifact"],
            "execution_mode": "background",
            "rationale": "semantic planner selected artifact.figure",
        }
    )
    try:
        turn = await agent.handle_turn("请生成一张 RAG 系统架构图", channel="cli", drain_tasks=False)
        events = await agent.tasks.list_events(turn.task_id)
        plan_event = next(event for event in events if event.event_type == "plan.created")
        selected = plan_event.output_json["selected_skills"][0]
        assert selected["skill"] == "scientific-figure"
        assert selected["reason"]
        assert selected["contract_level"] in {"full", "partial"}
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_scientific_figure_input_preserves_user_domain_without_rag_hardcode():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_figure_async_skill())
    agent.llm = PlanningLLM(
        {
            "intent_type": "qa_plus_artifact",
            "confidence": 0.91,
            "required_capabilities": ["artifact.figure"],
            "outputs": ["artifact"],
            "execution_mode": "background",
            "rationale": "semantic planner selected artifact.figure",
        }
    )
    try:
        turn = await agent.handle_turn("帮我产出一个 Transformer 的架构图", channel="cli", drain_tasks=False)
        task = await agent.runtime.get_subtask(turn.submitted_subtask_ids[0])

        assert task is not None
        assert "Transformer" in task.input_json["input"]
        assert "RAG system architecture" not in task.input_json["input"]
        assert set(task.input_json) == {"input", "_skill_source"}
        assert task.input_json["_skill_source"] == "builtin"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_react_fallback_receives_filtered_tool_catalog(monkeypatch):
    """A bounded fallback only ever sees the tools its plan allows.

    The catalog handed to the model is the first place a tool policy has to
    hold: a tool the plan blocked must not be offered at all, and the budget the
    plan set must be the budget on record.
    """

    class _Planner:
        def __init__(self, registry) -> None:  # noqa: ANN001
            self.registry = registry

        def plan(self, user_message: str, *, task_id: str = "") -> IntentPlan:
            return IntentPlan(
                task_id=task_id,
                user_message=user_message,
                intent_type=IntentType.REACT_FALLBACK,
                confidence=0.4,
                outputs=["answer"],
                execution_mode="react",
                context_policy=ContextPolicy(include_skill_catalog=True),
                tool_policy=ToolPolicy(
                    allowed_tools=["search_corpus"],
                    blocked_tools=["glob", "list_session_artifacts"],
                    max_tool_calls=1,
                    max_iterations=1,
                ),
                verification_plan=VerificationPlan(required_outputs=["answer"], required_events=["react.finished"]),
                rationale="test bounded fallback",
            )

    monkeypatch.setattr(orchestrator_mod, "IntentPlanner", _Planner)
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = CapturingLLM([ChatWithToolsResult(content="filtered answer")])
    agent.llm = llm
    try:
        turn = await agent.handle_turn("无法确定路由的问题", channel="cli", drain_tasks=False)

        assert turn.text == "filtered answer"
        assert llm.tool_names == ["search_corpus"]
        assert "glob" not in llm.tool_names
        events = await agent.tasks.list_events(turn.task_id)
        plan_event = next(event for event in events if event.event_type == "plan.validated")
        assert plan_event.output_json["tool_policy"]["max_tool_calls"] == 1
        event_types = [event.event_type for event in events]
        assert "react.finished" in event_types
        assert turn.settlement_status == "succeeded"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_handle_turn_forwards_planning_events_to_live_callback():
    """CLI live display: planning/validation/recovery narrate through on_tool_event.

    The DB run log stays authoritative; this covers the live mirror that the
    terminal transcript (TurnDisplay) renders. Channels that pass no callback
    (WeChat/Feishu/DingTalk) are untouched by construction.
    """
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_figure_async_skill())
    agent.llm = PlanningLLM(
        {
            "intent_type": "qa_plus_artifact",
            "confidence": 0.94,
            "required_capabilities": ["qa.grounded", "artifact.figure"],
            "outputs": ["answer", "artifact"],
            "execution_mode": "background",
            "rationale": "semantic planner selected answer plus figure",
        }
    )
    events: list[tuple[str, dict]] = []

    def collect(phase: str, data: dict) -> None:
        events.append((phase, data))

    try:
        turn = await agent.handle_turn(
            "RAG 如何降低幻觉，并生成一份 RAG 架构图。",
            channel="cli",
            drain_tasks=False,
            on_tool_event=collect,
        )
        assert turn.task_id
        plan_events = [data for phase, data in events if phase == "plan"]
        types = [str(data.get("event_type")) for data in plan_events]
        assert "context.assembled" in types
        assert "plan.model.proposed" in types
        assert "plan.validated" in types
        assert "plan.recovery" in types
        validated = next(data for data in plan_events if data["event_type"] == "plan.validated")
        assert validated["name"] == "qa_plus_artifact"
        assert validated["payload"]["intent_type"] == "qa_plus_artifact"
        assert validated["payload"]["status"] in {"validated", "degraded"}
        recovery = next(data for data in plan_events if data["event_type"] == "plan.recovery")
        assert recovery["payload"]["action"] == "execute"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_blocked_tool_is_refused_when_the_model_calls_it_anyway(monkeypatch):
    """Hiding a blocked tool is not enough; a call to it has to be refused.

    A model can name a tool that was never offered to it. Admission — not a
    later review of the finished turn — is what makes the block real, so the
    call has to be turned away before the tool runs, the refusal has to be
    durable under the run, and the tool must never post a successful result the
    answer could be built on.
    """

    class _Planner:
        def __init__(self, registry) -> None:  # noqa: ANN001
            self.registry = registry

        def plan(self, user_message: str, *, task_id: str = "") -> IntentPlan:
            return IntentPlan(
                task_id=task_id,
                user_message=user_message,
                intent_type=IntentType.REACT_FALLBACK,
                confidence=0.4,
                outputs=["answer"],
                execution_mode="react",
                tool_policy=ToolPolicy(
                    allowed_tools=["search_corpus"],
                    blocked_tools=["glob"],
                    max_tool_calls=2,
                    max_iterations=2,
                ),
                verification_plan=VerificationPlan(
                    required_outputs=["answer"],
                    required_events=["react.finished"],
                ),
                rationale="test blocked tool admission",
            )

    monkeypatch.setattr(orchestrator_mod, "IntentPlanner", _Planner)
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    llm = CapturingLLM(
        [
            ChatWithToolsResult(tool_calls=[ToolCall("bad", "glob", {"pattern": "**/*"})]),
            ChatWithToolsResult(content="recovered answer"),
        ]
    )
    agent.llm = llm
    try:
        turn = await agent.handle_turn("模型尝试 forbidden tool", channel="cli", drain_tasks=False)

        assert all("glob" not in names for names in llm.tool_names_seen)
        events = await agent.tasks.list_events(turn.task_id)
        attempts = [event for event in events if event.tool_name == "glob"]
        # Catalog rejection happens before lifecycle start or approval. Recording
        # only the terminal rejection avoids claiming the blocked tool ran.
        assert [event.event_type for event in attempts] == ["react.tool.rejected"]
        assert attempts[-1].status == "rejected"
        assert "glob" in attempts[-1].error
        assert not attempts[-1].output_json
        # The answer is the model's own recovery, never a glob result.
        assert turn.text == "recovered answer"
    finally:
        await agent.aclose()


def _dead_skill(name: str) -> SkillEntry:
    """A skill that resolves and runs, then fails the way a keyless provider does."""
    script = (
        "import sys;"
        "sys.stderr.write('Semantic Scholar API key is not configured');"
        "sys.exit(3)"
    )
    return SkillEntry(
        name=name,
        description=f"always-failing {name}",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        capabilities=_fixture_capabilities(name),
        priority=90,
        input_schema={
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
        output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


class _TranscriptLLM(PlanningLLM):
    """PlanningLLM that also keeps the messages each ReAct call was given."""

    def __init__(self, plan: dict, *, script: list[ChatWithToolsResult]) -> None:
        super().__init__(plan, script=script)
        self.transcripts: list[list[dict]] = []

    async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.transcripts.append([dict(m) for m in messages])
        return await super().chat_with_tools(messages, tools, **kwargs)


@pytest.mark.asyncio
async def test_a_dead_skill_ends_a_route_not_the_turn():
    """One failed provider is a detour, not the answer to a research question."""
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_dead_skill("literature-search"))
    llm = _TranscriptLLM(
        {
            "intent_type": "single_skill_task",
            "confidence": 0.9,
            "required_capabilities": ["literature.search"],
            "outputs": ["answer"],
            "execution_mode": "foreground",
            "rationale": "semantic planner selected literature.search",
        },
        script=[ChatWithToolsResult(content="Found the benchmarks via another source.")],
    )
    agent.llm = llm
    try:
        turn = await agent.handle_turn(
            "检索最近关于 RAG 评估基准的文献", channel="cli", drain_tasks=True
        )

        # The turn continued past the failure and the model, not the runner,
        # wrote the answer.
        assert llm.calls >= 1, "the turn ended at the failed skill instead of routing around it"
        assert turn.text == "Found the benchmarks via another source."

        # The model was told which route was already spent, so its retry is a
        # detour rather than the same call again.
        seeded = "\n".join(
            str(m.get("content") or "") for m in llm.transcripts[0]
        )
        assert "literature-search" in seeded
        assert "Semantic Scholar API key is not configured" in seeded

        # And it was handed a catalog it can actually route with, not the
        # single-skill plan's empty one.
        assert len(llm.tool_names) > 1
        assert {"search_literature", "web_search"} & set(llm.tool_names)
    finally:
        await agent.aclose()
