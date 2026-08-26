"""P0 — working continuity & artifact visibility.

Covers the fixes that eliminate the "amnesia" symptoms:
- task results are written back into the owning session transcript;
- produced artifacts get attributed to their session/task;
- the agent can pull memory / artifacts / tasks back explicitly;
- IM routing no longer hijacks generative requests that *reference* a task;
- workflow-step lookup resolves stable step ids without fabricating subtasks;
- curated memory files are loaded; referenced tasks are auto-attached.
"""

from __future__ import annotations

import sys

import pytest
from sqlalchemy import select

from omni.channels.commands import _normalize_command_text
from omni.config import load_settings
from omni.config.paths import OmniPaths, user_home
from omni.memory.files import load_curated_memory
from omni.memory.service import MemoryLayer, MemoryService
from omni.runtime.notifications import InboxNotifier
from omni.runtime.subtask_runtime import SubtaskRuntime, _collect_artifacts
from omni.runtime.taskref import (
    extract_task_ids,
    is_bare_task_id,
    is_task_lookup,
    is_task_reference,
)
from omni.skills_runtime.builtin_tools.recall import build_recall_tools
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import (
    ConversationMessageORM,
    SessionORM,
    SubtaskORM,
    TaskORM,
    WorkflowRunORM,
    WorkflowStepORM,
)


def _echo_skill() -> SkillEntry:
    script = (
        "import sys,json;d=json.load(sys.stdin);"
        "print(json.dumps({'status':'ok','summary':'did '+str(d.get('q'))}))"
    )
    return SkillEntry(
        name="echo_task", description="d", kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


async def _runtime(*, with_memory: bool = True):
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    reg = SkillRegistry(s)
    reg.build_index()
    reg.register(_echo_skill())
    inbox = InboxNotifier(s.paths.project_dir / "inbox.jsonl")
    from tests.conftest import ScriptedLLM

    llm = ScriptedLLM()
    store = ArtifactStore(s.paths, db)
    mem = MemoryService(db, s, llm=llm) if with_memory else None

    def ctx_factory(session_id, channel):  # noqa: ANN001
        return ExecContext(
            settings=s, paths=s.paths, session_id=session_id, channel=channel,
            db=db, artifacts=store, llm=llm,
        )

    rt = SubtaskRuntime(db, s, reg, ctx_factory, notifier=inbox, memory=mem)
    return rt, db, s, store, mem


# ── taskref + IM routing ──────────────────────────────────────────────────


def test_taskref_lookup_vs_reference():
    lookup = "/task show c98e4330"
    assert is_task_lookup(lookup)
    assert not is_task_reference_only(lookup)
    assert _normalize_command_text(lookup) == "/task show c98e4330"

    natural_lookup = "我想查任务id为c98e4330的执行过程"
    assert not is_task_lookup(natural_lookup)
    assert is_task_reference(natural_lookup)
    assert _normalize_command_text(natural_lookup) is None

    generative = "基于 task 14121f34 的产出，生成一篇符合 NeurIPS 规范的论文吧"
    assert extract_task_ids(generative) == ["14121f34"]
    assert not is_task_lookup(generative)
    assert is_task_reference(generative)
    assert not is_bare_task_id(generative)
    # A generative request must NOT be hijacked into a status view.
    assert _normalize_command_text(generative) is None

    assert is_bare_task_id("c98e4330")
    assert is_bare_task_id("6978342b")
    assert not is_bare_task_id("/task show c98e4330")


def is_task_reference_only(text: str) -> bool:
    return is_task_reference(text) and not is_task_lookup(text)


def test_normalize_ignores_plain_words():
    assert _normalize_command_text("workflow 该怎么跑") is None
    assert _normalize_command_text("你好") is None


# ── stable workflow-step lookup ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_task_step_id_fallback():
    from omni.cli.commands.tasks_cmd import resolve_workflow_step

    rt, db, _s, _store, _mem = await _runtime()
    async with db.session() as ss:
        ss.add(TaskORM(id="run0000aa11"))
        await ss.flush()
        ss.add(WorkflowRunORM(
            id="wf0000aa11", task_id="run0000aa11", status="succeeded"
        ))
        await ss.flush()
        ss.add(WorkflowStepORM(
            id="step-row-aa11",
            workflow_run_id="wf0000aa11",
            task_id="run0000aa11",
            step_key="stepbb22",
            position=1,
            skill_name="x",
            status="succeeded",
        ))
        await ss.commit()

    workflow, step, status = await resolve_workflow_step(rt, "stepbb22")
    assert status == "ok"
    assert workflow is not None and workflow.id == "wf0000aa11"
    assert step is not None and step.step_key == "stepbb22"
    assert (await resolve_workflow_step(rt, "nope404x"))[2] == "not_found"


# ── write-back ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_success_writes_back_to_session():
    rt, db, _s, _store, _mem = await _runtime()
    from omni.storage.models import SessionORM

    async with db.session() as ss:
        ss.add(SessionORM(id="sess-w", channel="cli"))
        await ss.commit()

    tid = await rt.enqueue("echo_task", {"q": "X"}, "cli", session_id="sess-w")
    await rt.drain()

    async with db.session() as ss:
        rows = (await ss.execute(
            select(ConversationMessageORM).where(ConversationMessageORM.session_id == "sess-w")
        )).scalars().all()
    written = [r for r in rows if r.content_type == "task_result"]
    assert written, "task result should be written back to the session transcript"
    assert "did X" in written[0].content
    assert written[0].meta.get("subtask_id") == tid


@pytest.mark.asyncio
async def test_write_back_attributes_artifacts_and_indexes_m5():
    rt, _db, _s, store, mem = await _runtime()
    async with _db.session() as ss:
        ss.add(SessionORM(id="sess-a", channel="cli"))
        ss.add(SubtaskORM(id="task-a", skill_name="synthesis.final", status="succeeded"))
        await ss.commit()
    art = await store.put_bytes(b"# report\nbody", kind="report", title="rep", ext="md")
    assert (await store.get(art.uri)).session_id == ""

    await rt._write_back_result(
        "sess-a", "task-a", "synthesis.final", "wrote a section",
        {"report_uri": art.uri, "dot": {"path": "/tmp/x.dot", "uri": "artifact://figdot"}},
    )

    row = await store.get(art.uri)
    assert row.session_id == "sess-a" and row.subtask_id == "task-a"

    # M5 artifact memory recorded (cross-session recallable)
    res = await mem.recall("产物", cross_session=True)
    assert any(sm.entry.layer == MemoryLayer.ARTIFACT.value for sm in res)


def test_collect_artifacts_walks_nested_results():
    result = {
        "report_uri": "artifact://r1",
        "figure": {"format": "svg", "path": "/a/b.svg", "uri": "artifact://r2"},
        "steps": [{"result": {"png_uri": "artifact://r3"}}],
        "noise": "not an artifact",
    }
    uris = {a["uri"] for a in _collect_artifacts(result)}
    assert {"artifact://r1", "artifact://r2", "artifact://r3"} <= uris


# ── recall tools ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_tools_round_trip():
    rt, db, s, store, _mem = await _runtime()
    ctx = ExecContext(
        settings=s, paths=s.paths, session_id="S1",
        db=db, artifacts=store, llm=None,
    )
    tools = {t.spec.name: t for t in build_recall_tools(ctx)}

    art = await store.put_bytes(b"# Title\nhello body", kind="report",
                                title="rep", ext="md", session_id="S1")
    listed = await tools["list_session_artifacts"].handler({})
    assert listed["scope"] == "session"
    assert any(a["uri"] == art.uri for a in listed["artifacts"])

    opened = await tools["open_artifact"].handler({"uri": art.uri})
    assert "hello body" in opened["content"]

    mem = MemoryService(db, s, llm=None)
    mid = await mem.record(
        layer=MemoryLayer.SEMANTIC, scope="project",
        summary="user prefers NeurIPS submission format", memory_type="preference",
        importance=0.8,
    )
    found = await tools["memory_search"].handler({"query": "NeurIPS format"})
    assert any(m["id"] == mid for m in found["matches"])
    got = await tools["memory_get"].handler({"id": mid})
    assert "NeurIPS" in got["summary"]

    async with db.session() as ss:
        ss.add(SubtaskORM(id="tk12345a", skill_name="synthesis.final",
                            status="succeeded",
                            result_json={"summary": "wrote", "report_uri": art.uri}))
        await ss.commit()
    gt = await tools["get_subtask"].handler({"subtask_id": "tk12345a"})
    assert gt["status"] == "succeeded"
    assert any(a["uri"] == art.uri for a in gt["artifacts"])


# ── curated memory files ──────────────────────────────────────────────────


def test_curated_memory_files_loaded(tmp_path):
    home = user_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "MEMORY.md").write_text("个人偏好：输出中文、NeurIPS 风格。", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("项目规则：引用必须可溯源。", encoding="utf-8")
    paths = OmniPaths(home=home, project_name="repo", project_dir=root / ".omni",
                      workspace_root=root)

    block = load_curated_memory(paths)
    assert "项目规则：引用必须可溯源" in block
    assert "个人偏好" in block
    assert "Project memory and rules" in block

    # No curated files → empty (lean prompt for fresh projects).
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    empty_home = tmp_path / "emptyhome"
    empty_home.mkdir()
    empty_paths = OmniPaths(home=empty_home, project_name="empty",
                            project_dir=empty_root / ".omni", workspace_root=empty_root)
    assert load_curated_memory(empty_paths) == ""


# ── agent-level: referenced task context + recent activity ────────────────


@pytest.mark.asyncio
async def test_agent_referenced_task_context_and_recent_activity():
    from omni.agent import OmniAgent

    agent = await OmniAgent.create(load_settings())
    try:
        art = await agent.artifacts.put_bytes(b"<svg/>", kind="figure", title="RAG 架构图",
                                              ext="svg", session_id="old")
        async with agent.db.session() as ss:
            ss.add(TaskORM(id="run14121f34"))
            await ss.flush()
            ss.add(SubtaskORM(id="14121f34", skill_name="synthesis.final",
                                status="succeeded", session_id="old", task_id="run14121f34",
                                result_json={"summary": "wrote intro", "report_uri": "artifact://abc"}))
            # A succeeded turn task is the principal-scoped anchor the digest lists.
            ss.add(TaskORM(id="turn0923f641", kind="turn", channel="cli", status="succeeded",
                            session_id="old", title="生成 RAG 系统架构图",
                            artifact_ids=[art.uri.removeprefix("artifact://")]))
            await ss.commit()

        block = await agent._referenced_task_context(
            "基于 task 14121f34 的产出，生成一篇符合 NeurIPS 规范的论文"
        )
        assert "Referenced task context" in block
        assert "synthesis.final" in block

        assert await agent._referenced_task_context("你好，今天天气如何") == ""

        # Cross-session, principal-scoped recent-activity digest: the CLI owner
        # (principal=local) sees the succeeded turn and its named output.
        recent = await agent._recent_activity_block(principal="local")
        assert "Recent activity" in recent
        assert "turn0923"[:8] in recent
        assert "生成 RAG 系统架构图" in recent
        assert "RAG 架构图" in recent  # artifact title attached as an output
    finally:
        await agent.aclose()
