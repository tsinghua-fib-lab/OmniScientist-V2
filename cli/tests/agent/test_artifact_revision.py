from __future__ import annotations

from pathlib import Path

import pytest

from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.runtime.artifact_contracts import contract_for_path
from omni.runtime.artifact_intents import artifact_intent_from_spec
from omni.runtime.artifact_revisions import revise_artifact
from omni.runtime.graphviz_revision import extract_graphviz_elements, patch_graphviz_color
from omni.runtime.session_focus import SessionFocusService, collect_artifact_refs
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM, TaskORM, WorkflowRunORM, WorkflowStepORM
from tests.conftest import PlanningLLM
from tests.conftest import install_fake_dot as _fake_dot

DOT = """
digraph G {
  subgraph cluster_phase2 {
    label="Phase II — Online Query Pipeline";
    color="#dc2626";
    bgcolor="#fef2f2";
    node [fillcolor="#fee2e2", color="#b91c1c", style="filled"];
    q [label="Query"];
  }
  subgraph cluster_legend {
    label="Legend";
    leg2 [label="Online Query", fillcolor="#fef2f2", color="#dc2626", shape=box];
  }
}
"""

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

# The model routes intent; these are the deterministic plan doubles it emits.
# ``artifact.revise`` = edit the attached figure (runtime picks an in-place patch
# or a source-preserving redraw); ``artifact.figure`` = a brand-new figure.
_REVISE_PLAN = {
    "intent_type": "single_skill_task",
    "confidence": 0.8,
    "required_capabilities": ["artifact.revise"],
    "outputs": ["artifact"],
    "rationale": "edit the attached figure",
}
_MINOR_EDIT_SPEC = {
    "target": "Phase II - Online Query Pipeline",
    "style": "blue",
    "scope": "element",
    "instruction": "Use the requested blue palette for the named pipeline phase.",
}
_MINOR_REVISE_PLAN = {
    **_REVISE_PLAN,
    "capability_inputs": {"artifact.revise": _MINOR_EDIT_SPEC},
}
_FIGURE_PLAN = {
    "intent_type": "single_skill_task",
    "confidence": 0.8,
    "required_capabilities": ["artifact.figure"],
    "outputs": ["artifact"],
    "rationale": "create a new figure",
}


# --- Deterministic grounding + edit contract (no model, no rule brain) --------


def test_patch_graphviz_color_grounds_explicit_colour():
    """A normalized target and style contract grounds and patches the DOT."""
    patched, changes = patch_graphviz_color(DOT, _MINOR_EDIT_SPEC)

    assert changes
    assert "#2563eb" in patched  # blue cluster
    assert "#eff6ff" in patched  # blue cluster background


def test_structured_element_edit_grounds_named_target_and_colour():
    elements = extract_graphviz_elements(DOT)

    intent = artifact_intent_from_spec(_MINOR_EDIT_SPEC, elements=elements)

    assert intent is not None
    assert intent.action == "minor_artifact_revision"
    assert intent.matched_element is not None
    assert "Online Query Pipeline" in intent.matched_element.label
    assert intent.style == "blue"


def test_structured_element_edit_rejects_incomplete_or_ambiguous_specs():
    """The runtime validates planner output and never infers missing semantics."""
    elements = extract_graphviz_elements(DOT)

    assert artifact_intent_from_spec({"target": "Phase II - Online Query Pipeline"}, elements=elements) is None
    assert artifact_intent_from_spec({"style": "blue", "scope": "element"}, elements=elements) is None
    assert artifact_intent_from_spec(
        {"target": "Unknown phase", "style": "blue", "scope": "element"},
        elements=elements,
    ) is None


@pytest.mark.asyncio
async def test_revise_artifact_renders_and_records_graphviz_artifacts(tmp_path, monkeypatch):
    _fake_dot(tmp_path, monkeypatch)
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    store = ArtifactStore(settings.paths, db)
    async with db.session() as sess:
        sess.add(SubtaskORM(id="task", skill_name="scientific-figure", status="succeeded"))
        await sess.commit()
    source = settings.paths.artifacts_dir / "figure" / "rag_engineering_v2.dot"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(DOT, encoding="utf-8")

    result = await revise_artifact(
        source_path=source,
        instruction="Phase II - Online Query Pipeline 改成蓝色",
        paths=settings.paths,
        db=db,
        artifacts=store,
        session_id="sess",
        subtask_id="task",
        edit_spec=_MINOR_EDIT_SPEC,
    )

    assert result.ok
    assert result.task_id
    assert {a["format"] for a in result.artifacts} == {"dot", "svg", "png"}
    assert all(Path(a["path"]).is_file() for a in result.artifacts)
    assert "re-rendered and registered" in result.message
    for item in result.artifacts:
        row = await store.get(item["uri"])
        assert row is not None
        assert row.meta["revision_of"] == str(source)
        assert row.meta["contract"] == "graphviz-dot"
        assert row.meta["instruction"].startswith("Phase II")


@pytest.mark.asyncio
async def test_revise_artifact_returns_observation_when_ungrounded(tmp_path, monkeypatch):
    """A vague instruction cannot be grounded to a target + concrete colour, so
    the tool returns an observation (not a guess, not a silent success)."""
    _fake_dot(tmp_path, monkeypatch)
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    store = ArtifactStore(settings.paths, db)
    source = settings.paths.artifacts_dir / "figure" / "rag_vague.dot"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(DOT, encoding="utf-8")

    result = await revise_artifact(
        source_path=source,
        instruction="Improve the figure aesthetics.",
        paths=settings.paths,
        db=db,
        artifacts=store,
        session_id="sess",
        subtask_id="task",
    )

    assert not result.ok
    assert "structured edit did not resolve" in result.message


@pytest.mark.asyncio
async def test_session_focus_resolves_uri_only_dot_artifact(tmp_path):
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    store = ArtifactStore(settings.paths, db)
    async with db.session() as sess:
        sess.add(SubtaskORM(id="task", skill_name="scientific-figure", status="succeeded"))
        await sess.commit()
    source = tmp_path / "figure.dot"
    source.write_text(DOT, encoding="utf-8")
    artifact = await store.put_file(
        source,
        kind="figure",
        title="RAG DOT",
        mime="text/vnd.graphviz",
        session_id="sess",
        subtask_id="task",
    )

    focus = SessionFocusService(db, settings.paths)
    await focus.record_skill_execution_result(
        session_id="sess",
        skill_execution_id="task",
        skill_name="scientific-figure",
        result={"artifacts": [{"title": "RAG DOT", "format": "dot", "uri": artifact.uri}]},
    )
    target = await focus.latest("sess")

    assert target is not None
    assert target.source_path == artifact.path


def test_session_focus_prefers_produced_artifact_over_assessment_and_revision_refs():
    current_uri = "artifact://current-dot"
    current_path = "/workspace/current.dot"
    source_path = "/workspace/source.dot"

    refs = collect_artifact_refs(
        {
            "deliverable_assessment": {
                "evidence_refs": [current_uri],
            },
            "artifacts": [
                {
                    "title": "Current DOT",
                    "format": "dot",
                    "uri": current_uri,
                    "path": current_path,
                }
            ],
            "revision": {
                "source_artifact_path": source_path,
            },
        }
    )

    assert [(ref.uri, ref.path, ref.fmt) for ref in refs] == [
        (current_uri, current_path, "dot")
    ]


def test_graphviz_contract_registry_and_skill_revision_metadata():
    settings = load_settings()
    registry = SkillRegistry(settings)
    registry.build_index()
    entry = registry.get("scientific-figure")

    assert contract_for_path(Path("figure.dot")) is not None
    assert entry is not None
    assert entry.artifact_revision["contract"] == "graphviz-dot"
    assert "major_revision" in entry.artifact_revision["supports"]


# --- Semantic planner contract: language-neutral execution ---------------------


@pytest.mark.parametrize(
    "user_message",
    [
        "Change Phase II - Online Query Pipeline to blue.",
        "把 Phase II - Online Query Pipeline 改成蓝色。",
        "Cambia Phase II - Online Query Pipeline a azul.",
    ],
)
@pytest.mark.asyncio
async def test_planner_structured_colour_edit_uses_artifact_transaction(
    tmp_path,
    monkeypatch,
    user_message: str,
):
    """Equivalent multilingual requests execute through one normalized contract."""
    _fake_dot(tmp_path, monkeypatch)
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = OmniAgent(settings)
    await agent.setup()
    agent.llm = PlanningLLM(_MINOR_REVISE_PLAN)
    session_id = await agent.ensure_session(channel="cli")
    source = settings.paths.artifacts_dir / "figure" / "rag_engineering_v2.dot"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(DOT, encoding="utf-8")
    async with agent.db.session() as s:
        task = SubtaskORM(
            skill_name="scientific-figure",
            status="succeeded",
            session_id=session_id,
            result_json={"artifacts": [{"title": "DOT", "format": "dot", "path": str(source)}]},
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
    await agent.focus.record_skill_execution_attachment(task, session_id=session_id)
    await agent._persist_message(  # noqa: SLF001
        session_id,
        "assistant",
        f"Artifact: DOT {source}",
        kind="text",
    )

    turn = await agent.handle_turn(
        user_message,
        session_id=session_id,
        channel="cli",
    )

    assert turn.kind == "text"
    assert turn.terminated_reason == "artifact_revision_done"
    assert "artifact transaction" in turn.text
    assert "re-rendered and registered" in turn.text
    await agent.aclose()


@pytest.mark.asyncio
async def test_offline_vague_edit_degrades_to_placeholder_not_guess(tmp_path, monkeypatch):
    """Offline, a vague edit is no longer keyword-guessed into a revision; it
    degrades to an honest placeholder (no in-place edit, no dead-end error)."""
    _fake_dot(tmp_path, monkeypatch)
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = OmniAgent(settings)
    await agent.setup()
    session_id = await agent.ensure_session(channel="wechat", external_key="wx-offline-vague-b")
    source = settings.paths.artifacts_dir / "figure" / "rag_architecture.dot"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(DOT, encoding="utf-8")
    result = {"summary": "RAG Architecture", "artifacts": [{"title": "RAG DOT", "format": "dot", "path": str(source)}]}
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
    await agent.runtime._write_back_result(  # noqa: SLF001
        session_id, subtask_id, "scientific-figure", "RAG Architecture", result
    )

    turn = await agent.handle_turn(
        "这张图过于简单，请从工程角度上做优化",
        session_id=session_id,
        channel="wechat",
        drain_tasks=False,
    )

    assert turn.terminated_reason != "artifact_revision_failed"
    assert turn.terminated_reason != "artifact_revision_done"
    assert turn.terminated_reason != "major_revision_submitted"
    assert turn.kind != "error"
    await agent.aclose()


@pytest.mark.asyncio
async def test_offline_figure_question_degrades_to_placeholder(tmp_path, monkeypatch):
    """Offline, a question about the active figure is not answered by a templated
    rule brain; it degrades to the normal (placeholder) path."""
    _fake_dot(tmp_path, monkeypatch)
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = OmniAgent(settings)
    await agent.setup()
    session_id = await agent.ensure_session(channel="wechat", external_key="wx-offline-qa-b")
    source = settings.paths.artifacts_dir / "figure" / "rag_architecture.dot"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(DOT, encoding="utf-8")
    result = {"summary": "RAG Architecture", "artifacts": [{"title": "RAG DOT", "format": "dot", "path": str(source)}]}
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
    await agent.runtime._write_back_result(  # noqa: SLF001
        session_id, subtask_id, "scientific-figure", "RAG Architecture", result
    )

    turn = await agent.handle_turn(
        "上面生成的这个图主要讲了什么？",
        session_id=session_id,
        channel="wechat",
    )

    assert turn.terminated_reason != "artifact_qa_done"
    events = await agent.tasks.list_events(turn.task_id)
    assert not any(event.event_type == "artifact.qa" for event in events)
    await agent.aclose()


# --- Model-routed revision (replay of the real path) --------------------------


@pytest.mark.asyncio
async def test_model_major_revision_submits_new_skill_task(tmp_path, monkeypatch):
    _fake_dot(tmp_path, monkeypatch)
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = OmniAgent(settings)
    await agent.setup()
    agent.llm = PlanningLLM(_REVISE_PLAN)
    session_id = await agent.ensure_session(channel="cli")
    source = settings.paths.artifacts_dir / "figure" / "rag_engineering_v2.dot"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(DOT, encoding="utf-8")
    async with agent.db.session() as s:
        task = SubtaskORM(
            skill_name="scientific-figure",
            status="succeeded",
            session_id=session_id,
            result_json={"artifacts": [{"title": "DOT", "format": "dot", "path": str(source)}]},
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        subtask_id = task.id
    await agent.focus.record_skill_execution_attachment(task, session_id=session_id)

    turn = await agent.handle_turn(
        "这张图过于简单，请从工程角度上做优化",
        session_id=session_id,
        channel="cli",
        drain_tasks=False,
    )

    assert turn.kind == "text"
    assert turn.terminated_reason == "major_revision_submitted"
    assert turn.task_id
    assert turn.submitted_subtask_ids
    assert "revision task" in turn.text
    assert f"/task show {turn.task_id[:8]}" in turn.text
    assert f"/task show {turn.submitted_subtask_ids[0][:8]}" not in turn.text
    async with agent.db.session() as s:
        submitted = await s.get(SubtaskORM, turn.submitted_subtask_ids[0])
        assert submitted is not None
        assert submitted.input_json["source_task_id"] == subtask_id
        assert submitted.input_json["source_artifact_path"] == str(source)
        assert submitted.input_json["source_artifact_dot"].strip().startswith("digraph")
        assert submitted.input_json["source_nodes"]
        assert submitted.input_json["revision_constraints"]["preserve_source_structure"] is True
        assert submitted.input_json["revision_constraints"]["min_nodes"] >= 1
        assert submitted.input_json["revision_constraints"]["allow_simplification"] is False
    await agent.aclose()


@pytest.mark.asyncio
async def test_model_major_revision_from_recent_focus_without_attach(tmp_path, monkeypatch):
    _fake_dot(tmp_path, monkeypatch)
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = OmniAgent(settings)
    await agent.setup()
    agent.llm = PlanningLLM(_REVISE_PLAN)
    session_id = await agent.ensure_session(channel="wechat", external_key="wx-user")
    source = settings.paths.artifacts_dir / "figure" / "rag_architecture.dot"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(DOT, encoding="utf-8")
    result = {"summary": "RAG Architecture", "artifacts": [{"title": "RAG DOT", "format": "dot", "path": str(source)}]}
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
    await agent.runtime._write_back_result(  # noqa: SLF001
        session_id, subtask_id, "scientific-figure", "RAG Architecture", result
    )

    turn = await agent.handle_turn(
        "生成的这个架构图过于简单，内容不够，请结合实际工程实践做优化",
        session_id=session_id,
        channel="wechat",
        drain_tasks=False,
    )

    assert turn.kind == "text"
    assert turn.terminated_reason == "major_revision_submitted"
    assert turn.submitted_subtask_ids
    async with agent.db.session() as s:
        submitted = await s.get(SubtaskORM, turn.submitted_subtask_ids[0])
        assert submitted is not None
        assert submitted.skill_name == "scientific-figure"
        assert submitted.input_json["source_task_id"] == subtask_id
        assert submitted.input_json["source_artifact_path"] == str(source)
        assert "source_artifact_dot" in submitted.input_json
        assert submitted.input_json["revision_constraints"]["reject_generic_template"] is True
    await agent.aclose()


@pytest.mark.asyncio
async def test_completed_major_revision_promotes_new_artifact_focus_for_followup(tmp_path, monkeypatch):
    _fake_dot(tmp_path, monkeypatch)
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = OmniAgent(settings)
    await agent.setup()
    agent.llm = PlanningLLM(_REVISE_PLAN)
    session_id = await agent.ensure_session(channel="wechat", external_key="wx-promote")
    source = settings.paths.artifacts_dir / "figure" / "rag_architecture.dot"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(RAG_DOT, encoding="utf-8")
    first_result = {"summary": "RAG Architecture", "artifacts": [{"title": "RAG DOT", "format": "dot", "path": str(source)}]}
    async with agent.db.session() as s:
        task = SubtaskORM(
            skill_name="scientific-figure",
            status="succeeded",
            session_id=session_id,
            result_json=first_result,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        first_task_id = task.id
    await agent.runtime._write_back_result(  # noqa: SLF001
        session_id, first_task_id, "scientific-figure", "RAG Architecture", first_result
    )

    first_turn = await agent.handle_turn(
        "生成的这个架构图有点过于简单，请结合实际大公司工程的场景做详细优化",
        session_id=session_id,
        channel="wechat",
        drain_tasks=True,
    )

    assert first_turn.submitted_subtask_ids
    await agent.runtime.process(first_turn.submitted_subtask_ids[0])
    async with agent.db.session() as s:
        first_revision_task = await s.get(SubtaskORM, first_turn.submitted_subtask_ids[0])
    assert first_revision_task is not None
    first_revision_result = first_revision_task.result_json
    assert first_revision_result["revision"]["mode"] == "major"
    first_revision_dot = next(
        artifact["path"]
        for artifact in first_revision_result["artifacts"]
        if artifact["format"] == "dot"
    )

    second_turn = await agent.handle_turn(
        "上面生成的这个架构图还是有点简单，请结合实际大公司工程的场景做详细优化，要求可以指导开发",
        session_id=session_id,
        channel="wechat",
        drain_tasks=False,
    )

    assert second_turn.terminated_reason == "major_revision_submitted"
    assert second_turn.submitted_subtask_ids
    async with agent.db.session() as s:
        second_task = await s.get(SubtaskORM, second_turn.submitted_subtask_ids[0])
    assert second_task is not None
    assert second_task.input_json["source_task_id"] == first_revision_task.id
    assert second_task.input_json["source_artifact_path"] == first_revision_dot
    revised_source = second_task.input_json["source_artifact_dot"]
    assert "User Query" in revised_source
    assert "Retriever" in revised_source
    assert "cluster_revision" in revised_source or "cluster_engineering" in revised_source
    await agent.aclose()


@pytest.mark.asyncio
async def test_workflow_parent_attach_promotes_final_child_artifact_for_revision(tmp_path, monkeypatch):
    _fake_dot(tmp_path, monkeypatch)
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = OmniAgent(settings)
    await agent.setup()
    agent.llm = PlanningLLM(_REVISE_PLAN)
    session_id = await agent.ensure_session(channel="wechat", external_key="wx-workflow-parent")
    source = settings.paths.artifacts_dir / "figure" / "rag_child.dot"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(RAG_DOT, encoding="utf-8")
    workflow_result = {
        "status": "succeeded",
        "summary": "工作流完成",
        "steps": [
            {"id": "qa", "skill_name": "lit-qa", "status": "succeeded", "result": {"summary": "RAG answer"}},
            {
                "id": "figure",
                "skill_name": "scientific-figure",
                "status": "succeeded",
                "result": {
                    "title": "RAG Architecture",
                    "summary": "Figure ready",
                    "artifacts": [{"title": "RAG DOT", "format": "dot", "path": str(source)}],
                },
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
            result_json={"summary": "RAG answer"},
        )
        figure_step = WorkflowStepORM(
            workflow_run_id=workflow.id,
            task_id=owner.id,
            step_key="figure",
            position=1,
            skill_name="scientific-figure",
            capability="artifact.figure",
            status="succeeded",
            result_json=workflow_result["steps"][1]["result"],
        )
        s.add_all([qa_step, figure_step])
        await s.flush()
        figure_execution = SubtaskORM(
            task_id=owner.id,
            workflow_run_id=workflow.id,
            workflow_step_id=figure_step.id,
            skill_name="scientific-figure",
            status="succeeded",
            session_id=session_id,
            result_json=figure_step.result_json,
        )
        s.add(figure_execution)
        await s.flush()
        figure_step.current_execution_id = figure_execution.id
        figure_step.execution_ids = [figure_execution.id]
        owner.submitted_workflow_ids = [workflow.id]
        owner.submitted_subtask_ids = [figure_execution.id]
        await s.commit()
        for row in (workflow, qa_step, figure_step, figure_execution):
            await s.refresh(row)
    focus = await agent.focus.record_workflow_attachment(
        workflow, [qa_step, figure_step], session_id=session_id
    )

    assert focus is not None
    assert focus.skill_name == "scientific-figure"
    assert focus.workflow_run_id == workflow.id
    assert focus.workflow_step_id == figure_step.id
    assert focus.subtask_id == figure_execution.id
    assert focus.source_path == str(source)

    turn = await agent.handle_turn(
        "生成的这个架构图过于简单，内容不够，请结合实际工程实践做优化",
        session_id=session_id,
        channel="wechat",
        drain_tasks=False,
    )

    assert turn.terminated_reason == "major_revision_submitted"
    assert turn.submitted_subtask_ids
    async with agent.db.session() as s:
        submitted = await s.get(SubtaskORM, turn.submitted_subtask_ids[0])
    assert submitted is not None
    assert submitted.input_json["source_task_id"] == figure_execution.id
    assert submitted.input_json["source_artifact_path"] == str(source)
    assert submitted.input_json["workflow_step_id"] == figure_step.id
    await agent.aclose()


@pytest.mark.asyncio
async def test_new_rag_figure_request_is_not_stolen_by_active_artifact_focus(tmp_path, monkeypatch):
    """A genuine new-figure request routes to artifact.figure and is drawn fresh
    — the active figure is not pulled in as a source (no word-brain hijack)."""
    _fake_dot(tmp_path, monkeypatch)
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    agent = OmniAgent(settings)
    await agent.setup()
    agent.llm = PlanningLLM(_FIGURE_PLAN)
    session_id = await agent.ensure_session(channel="wechat", external_key="wx-new-rag")
    source = settings.paths.artifacts_dir / "figure" / "rag_architecture.dot"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(RAG_DOT, encoding="utf-8")
    result = {"summary": "RAG Architecture", "artifacts": [{"title": "RAG DOT", "format": "dot", "path": str(source)}]}
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
    await agent.runtime._write_back_result(  # noqa: SLF001
        session_id, subtask_id, "scientific-figure", "RAG Architecture", result
    )

    turn = await agent.handle_turn(
        "RAG 如何降低幻觉，并给我生成一份目前全球范围最流行的 RAG 构建的架构图",
        session_id=session_id,
        channel="wechat",
        drain_tasks=False,
    )

    assert turn.submitted_subtask_ids
    assert turn.terminated_reason != "artifact_revision_failed"
    assert turn.terminated_reason != "artifact_revision_done"
    async with agent.db.session() as s:
        submitted = await s.get(SubtaskORM, turn.submitted_subtask_ids[0])
    assert submitted is not None
    # A new figure must NOT inherit the active figure as a revision source.
    assert "source_artifact_path" not in submitted.input_json
    assert "source_task_id" not in submitted.input_json
    events = await agent.tasks.list_events(turn.task_id)
    assert not any(event.event_type == "artifact.revision" for event in events)
    await agent.aclose()
