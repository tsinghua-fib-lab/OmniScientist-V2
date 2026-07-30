"""User-scenario benchmark for the contract-driven agent harness."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omni.agent.orchestrator import OmniAgent, TurnResult
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.runtime.presentation import turn_presentation_from_result
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from omni.storage.models import SubtaskORM, TaskORM, WorkflowRunORM, WorkflowStepORM
from tests.conftest import PlanningLLM, ScriptedLLM
from tests.conftest import install_fake_dot as _fake_dot

RAG_DOT = """
digraph RAG {
  graph [label="RAG Architecture"];
  query [label="User Query"];
  retriever [label="Retriever"];
  generator [label="LLM Generator"];
  query -> retriever;
  retriever -> generator;
}
"""

# The model routes "edit the attached figure" intent to artifact.revise; the
# runtime escalates a vague edit to a source-preserving redraw.
_REVISE_PLAN = {
    "intent_type": "single_skill_task",
    "confidence": 0.85,
    "required_capabilities": ["artifact.revise"],
    "outputs": ["artifact"],
    "rationale": "edit the attached figure",
}


async def _agent() -> OmniAgent:
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = OmniAgent(settings)
    await agent.setup()
    return agent


def _draft_workflow_plan() -> dict:
    return {
        "intent_type": "workflow",
        "confidence": 0.93,
        "workflow_steps": [
            {"id": "literature", "capability": "literature.search", "input": {"query": "Transformer/RAG"}},
            {"id": "paper", "capability": "paper.fetch.arxiv", "depends_on": ["literature"], "input": {"identifier": "1706.03762"}},
            {"id": "figure", "capability": "artifact.figure", "depends_on": ["paper"], "input": {"input": "Transformer/RAG architecture figure"}},
            {"id": "final_synthesis", "capability": "synthesis.final", "depends_on": ["figure"], "input": {"deliverable": "draft.section"}},
        ],
        "outputs": ["draft.section"],
        "execution_mode": "background",
        "provenance_mode": "light",
        "rationale": "model proposed a capability-level research workflow with final synthesis",
    }


async def _seed_figure_task(agent: OmniAgent, session_id: str, name: str = "rag.dot") -> tuple[str, Path]:
    source = agent.settings.paths.artifacts_dir / "figure" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(RAG_DOT, encoding="utf-8")
    result = {
        "summary": "RAG Architecture",
        "artifacts": [{"title": "RAG DOT", "format": "dot", "path": str(source)}],
    }
    async with agent.db.session() as s:
        task = SubtaskORM(
            skill_name="scientific-figure",
            status="succeeded",
            session_id=session_id,
            result_json=result,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        subtask_id = task.id
    await agent.focus.record_skill_execution_result(
        session_id=session_id,
        skill_execution_id=subtask_id,
        skill_name="scientific-figure",
        result=result,
        origin="task_completed",
    )
    return subtask_id, source


def _fixture_skill(name: str, capabilities: list[str], *, required: list[str] | None = None) -> SkillEntry:
    required = list(required or [])
    script = (
        "import json,sys;"
        "d=json.load(sys.stdin);"
        "print(json.dumps({'status':'ok','summary':'ran " + name + "','payload':d}))"
    )
    return SkillEntry(
        name=name,
        description=f"benchmark fixture for {name}",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        capabilities=capabilities,
        priority=90,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        input_schema={
            "type": "object",
            "properties": {key: {"type": "string"} for key in required},
            "required": required,
        },
        output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
    )


def _install_offline_workflow_fixtures(agent: OmniAgent) -> None:
    agent.registry.register(_fixture_skill("literature-search", ["literature.search"], required=["query"]))
    agent.registry.register(_fixture_skill("arxiv-fetch", ["paper.fetch.arxiv"], required=["identifier"]))
    # Keep the real built-in figure engine: an echo fixture cannot satisfy the
    # artifact-emitted verification contract and would make this journey lie
    # about having produced a scientific deliverable.


@pytest.mark.asyncio
async def test_benchmark_new_figure_attach_optimize_then_followup_without_attach(tmp_path, monkeypatch):
    _fake_dot(tmp_path, monkeypatch)
    agent = await _agent()
    agent.llm = PlanningLLM(_REVISE_PLAN)
    session_id = await agent.ensure_session(channel="wechat", external_key="wx-benchmark")
    try:
        original_task_id, _source = await _seed_figure_task(agent, session_id)
        task = await agent.runtime.get_subtask(original_task_id)
        assert task is not None
        await agent.focus.record_skill_execution_attachment(task, session_id=session_id)

        first = await agent.handle_turn(
            "生成的这个架构图有点过于简单，请结合实际大公司工程的场景做详细优化",
            session_id=session_id,
            channel="wechat",
            drain_tasks=True,
        )
        assert first.terminated_reason == "major_revision_submitted"
        assert first.submitted_subtask_ids

        second = await agent.handle_turn(
            "上面生成的这个架构图还是有点简单，请结合实际大公司工程的场景做详细优化，要求可以指导开发",
            session_id=session_id,
            channel="wechat",
            drain_tasks=False,
        )
        assert second.terminated_reason == "major_revision_submitted"
        assert second.submitted_subtask_ids
        async with agent.db.session() as s:
            first_task = await s.get(SubtaskORM, first.submitted_subtask_ids[0])
            second_task = await s.get(SubtaskORM, second.submitted_subtask_ids[0])
        assert first_task is not None
        assert second_task is not None
        assert second_task.input_json["source_task_id"] == first_task.id
        assert second_task.input_json["source_artifact_path"] != str(_source)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_benchmark_workflow_parent_attach_binds_final_child_artifact(tmp_path, monkeypatch):
    _fake_dot(tmp_path, monkeypatch)
    agent = await _agent()
    agent.llm = PlanningLLM(_REVISE_PLAN)
    session_id = await agent.ensure_session(channel="wechat", external_key="wx-workflow")
    try:
        source = agent.settings.paths.artifacts_dir / "figure" / "workflow_child.dot"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(RAG_DOT, encoding="utf-8")
        workflow_result = {
            "status": "succeeded",
            "steps": [
                {"id": "qa", "skill_name": "lit-qa", "capability": "qa.grounded", "result": {"summary": "answer"}},
                {
                    "id": "figure",
                    "skill_name": "project-figure-provider",
                    "capability": "artifact.figure",
                    "result": {"artifacts": [{"title": "RAG DOT", "format": "dot", "path": str(source)}]},
                },
            ],
        }
        async with agent.db.session() as s:
            owner = TaskORM(
                session_id=session_id,
                channel="wechat",
                status="succeeded",
                title="RAG workflow",
                user_input="Build a RAG workflow",
            )
            s.add(owner)
            await s.flush()
            workflow = WorkflowRunORM(
                task_id=owner.id,
                session_id=session_id,
                status="succeeded",
                goal="Build a RAG workflow",
                result_json=workflow_result,
            )
            s.add(workflow)
            await s.flush()
            qa_step = WorkflowStepORM(
                workflow_run_id=workflow.id,
                task_id=owner.id,
                step_key="qa",
                position=0,
                skill_name="lit-qa",
                capability="qa.grounded",
                status="succeeded",
                result_json=workflow_result["steps"][0]["result"],
            )
            figure_step = WorkflowStepORM(
                workflow_run_id=workflow.id,
                task_id=owner.id,
                step_key="figure",
                position=1,
                skill_name="project-figure-provider",
                capability="artifact.figure",
                status="succeeded",
                result_json=workflow_result["steps"][1]["result"],
            )
            s.add_all([qa_step, figure_step])
            await s.flush()
            execution = SubtaskORM(
                task_id=owner.id,
                workflow_run_id=workflow.id,
                workflow_step_id=figure_step.id,
                skill_name="project-figure-provider",
                status="succeeded",
                session_id=session_id,
                result_json=figure_step.result_json,
            )
            s.add(execution)
            await s.flush()
            figure_step.current_execution_id = execution.id
            figure_step.execution_ids = [execution.id]
            owner.submitted_workflow_ids = [workflow.id]
            owner.submitted_subtask_ids = [execution.id]
            await s.commit()
            for row in (workflow, qa_step, figure_step, execution):
                await s.refresh(row)
        focus = await agent.focus.record_workflow_attachment(
            workflow, [qa_step, figure_step], session_id=session_id
        )

        assert focus is not None
        assert focus.workflow_run_id == workflow.id
        assert focus.workflow_step_id == figure_step.id
        assert focus.subtask_id == execution.id
        assert focus.source_path == str(source)

        turn = await agent.handle_turn(
            "生成的这个架构图过于简单，内容不够，请结合实际工程实践做优化",
            session_id=session_id,
            channel="wechat",
            drain_tasks=False,
        )
        assert turn.terminated_reason == "major_revision_submitted"
        async with agent.db.session() as s:
            submitted = await s.get(SubtaskORM, turn.submitted_subtask_ids[0])
        assert submitted is not None
        assert submitted.input_json["source_task_id"] == execution.id
        assert submitted.input_json["workflow_step_id"] == figure_step.id
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_benchmark_draft_section_uses_native_synthesis():
    agent = await _agent()
    try:
        agent.llm = PlanningLLM(planner_gated=True, plans=[_draft_workflow_plan()])
        turn = await agent.handle_turn(
            "写一个 Transformer/RAG 相关研究小节：先做文献检索，再获取 arXiv 1706.03762，"
            "生成架构图，最后输出论文段落。",
            channel="cli",
            drain_tasks=False,
        )
        assert turn.kind == "workflow"
        workflow = await agent.runtime.get_workflow_run(turn.submitted_workflow_ids[0])
        assert workflow is not None
        steps = workflow.plan_json["steps"]
        assert any(step["capability"] == "synthesis.final" for step in steps)
        assert any(step.get("deliverable") == "draft.section" for step in steps)
        assert any(
            step.get("capability") == "synthesis.final"
            and step.get("provider_type") == "native_executor"
            and not step.get("skill_name")
            for step in steps
        )
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_benchmark_title_only_paper_request_does_not_run_arxiv_fetch():
    agent = await _agent()
    try:
        agent.llm = PlanningLLM(planner_gated=True, plans=[
            {
                "intent_type": "needs_input",
                "confidence": 0.88,
                "missing_inputs": [
                    {
                        "field": "paper_identifier",
                        "reason": "title-only paper request needs a resolved DOI/arXiv id/URL before paper.fetch.arxiv",
                    }
                ],
                "outputs": ["question"],
                "execution_mode": "ask",
                "rationale": "title-only paper reference must be resolved before fetching",
            }
        ])
        turn = await agent.handle_turn(
            "为 RAG 系统综述准备材料：获取 Attention Is All You Need 摘要，并生成包含 query、retriever、reranker、LLM 的科研架构图。",
            channel="cli",
            drain_tasks=False,
        )
        if turn.submitted_workflow_ids:
            workflow = await agent.runtime.get_workflow_run(turn.submitted_workflow_ids[0])
            assert workflow is not None
            steps = workflow.plan_json.get("steps") or []
            assert not any(step.get("skill_name") == "arxiv-fetch" for step in steps)
        else:
            assert turn.kind == "needs_input"
    finally:
        await agent.aclose()


def test_benchmark_im_hides_trace_for_salvaged_tool_limit():
    turn = TurnResult(
        text=(
            "部分结果：工具调用次数超出上限。\n\n"
            "已完成：\n"
            "- read_file（成功）：{\"debug\":\"internal\"}\n"
            "- glob（成功）：/tmp/file"
        ),
        session_id="sess",
        task_id="run-benchmark",
        kind="partial",
        terminated_reason="max_tool_calls",
        plan_summary="计划：react_fallback；原因：debug",
        verification_status="salvaged",
    )

    im = turn_presentation_from_result(turn, channel="wechat").to_markdown()
    cli = turn_presentation_from_result(turn, channel="cli").to_markdown()

    assert "read_file" in cli
    assert "glob" in cli
    assert "read_file" not in im
    assert "glob" not in im
    assert "tool budget reached" in im
    assert "run-benc" in im


@pytest.mark.asyncio
async def test_benchmark_tool_limit_salvages_partial_result():
    tool = ToolSpec("echo", "echo back", {"type": "object", "properties": {"x": {"type": "string"}}})

    async def invoke(name, args):  # noqa: ANN001
        return {"name": name, "args": args}

    llm = ScriptedLLM([
        ChatWithToolsResult(tool_calls=[ToolCall("c1", "echo", {"x": "one"})]),
        ChatWithToolsResult(tool_calls=[ToolCall("c2", "echo", {"x": "two"})]),
    ])
    # Exercise the salvage fallback specifically (synthesis is the default path).
    agent = ReActLoopAgent(llm, invoke, max_iterations=4, max_tool_calls=1, no_progress_synthesis=False)

    result = await agent.run(system_prompt="sys", user_message="RAG 如何降低幻觉", tools=[tool])

    assert result.kind == "partial"
    assert result.terminated_reason == "max_tool_calls"
    assert "Partial result" in result.content
    assert "echo" in result.content


@pytest.mark.asyncio
async def test_benchmark_native_synthesis_still_drains_to_draft(tmp_path, monkeypatch):
    _fake_dot(tmp_path, monkeypatch)
    agent = await _agent()
    try:
        _install_offline_workflow_fixtures(agent)
        agent.llm = PlanningLLM(planner_gated=True, plans=[_draft_workflow_plan()])
        turn = await agent.handle_turn(
            "写一个 Transformer/RAG 相关研究小节：先做文献检索，再获取 arXiv 1706.03762，"
            "生成架构图，最后输出论文段落。",
            channel="cli",
            drain_tasks=True,
        )
        assert turn.kind == "workflow"
        assert turn.drained_results
        result = turn.drained_results[0]["result"]
        synthesis = [step for step in result["steps"] if step["capability"] == "synthesis.final"]
        assert synthesis
        assert synthesis[0]["result"]["deliverable"] == "draft.section"
        assert synthesis[0]["result"]["draft_markdown"]
    finally:
        await agent.aclose()
