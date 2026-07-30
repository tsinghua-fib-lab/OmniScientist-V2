from __future__ import annotations

import pytest

from omni.agent.model_planner import ModelIntentPlanner
from omni.agent.turn_context import TurnContextAssembler
from omni.config import load_settings
from omni.core.llm.client import LLMClient
from omni.runtime.session_focus import SessionFocusService
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM


class RecordingPlannerLLM(LLMClient):
    def __init__(self) -> None:
        self.model = "recording"
        self.last_system = ""
        self.last_user = ""

    async def chat(self, system: str, user: str, **kwargs):
        self.last_system = system
        self.last_user = user
        return '{"intent_type":"single_skill_task","required_capabilities":["artifact.figure"],"confidence":0.82,"outputs":["artifact"],"execution_mode":"background","rationale":"context-aware figure revision"}'

    async def chat_with_tools(self, messages, tools, **kwargs):  # pragma: no cover - not used here
        raise AssertionError("planner test should not call chat_with_tools")

    async def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - not used here
        return [[0.0] for _ in texts]


@pytest.mark.asyncio
async def test_turn_context_assembles_active_target_and_recent_artifacts(tmp_path):
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    artifacts = ArtifactStore(settings.paths, db)
    focus = SessionFocusService(db, settings.paths)
    async with db.session() as sess:
        sess.add(SubtaskORM(id="task-1", skill_name="scientific-figure", status="succeeded"))
        await sess.commit()
    source = tmp_path / "rag.dot"
    source.write_text("digraph G { a -> b }", encoding="utf-8")
    artifact = await artifacts.put_file(
        source,
        kind="figure",
        title="RAG Architecture DOT",
        mime="text/vnd.graphviz",
        session_id="sess",
        subtask_id="task-1",
    )
    await focus.record_skill_execution_result(
        session_id="sess",
        skill_execution_id="task-1",
        skill_name="scientific-figure",
        result={"artifacts": [{"title": "RAG Architecture DOT", "format": "dot", "uri": artifact.uri}]},
        origin="task_completed",
        task_id="run-1",
    )

    context = await TurnContextAssembler(
        db=db,
        paths=settings.paths,
        focus=focus,
        artifacts=artifacts,
    ).assemble(
        session_id="sess",
        channel="wechat",
        user_message="生成的这个架构图过于简单，请结合工程实践优化",
    )

    assert context.active_target is not None
    assert context.active_target.skill_execution_id == "task-1"
    assert context.active_target.skill_name == "scientific-figure"
    assert context.active_target.source_path == str(artifact.path)
    assert context.recent_artifacts
    assert "Active target" in context.to_planner_summary()


@pytest.mark.asyncio
async def test_model_planner_prompt_includes_turn_context():
    settings = load_settings()
    registry = SkillRegistry(settings)
    registry.build_index()
    llm = RecordingPlannerLLM()

    proposal = await ModelIntentPlanner(llm, registry).propose(
        "把这个图做得更工程化",
        context_summary="Active target: RAG Architecture DOT from task 42859c02",
    )

    assert proposal is not None
    assert proposal.required_capabilities == ["artifact.figure"]
    assert "Active target: RAG Architecture DOT" in llm.last_user
