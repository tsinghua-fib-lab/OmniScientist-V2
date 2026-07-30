"""BUG-09: generic ReAct must hit the same ROM recording boundary as skills.

A skill engine calls ``add_run`` / ``cite_source`` itself. Generic ReAct only
had deferred ``log_run`` / ``record_claim`` tools and a prompt that says to use
light provenance, so ``omni run list`` / ``omni verify`` stayed empty after a
real write-and-exec turn. Source/Claim stay model-owned; the host records the
exec it already ran (Codex records execs the same way).
"""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent
from omni.research.store import ResearchStore
from omni.research.verify import verify_session
from omni.runtime.task_recorder import TaskRecorder
from omni.runtime.tool_gateway import ToolGateway
from omni.skills_runtime.builtin_tools.fs import build_fs_tools
from omni.skills_runtime.builtin_tools.shell import build_shell_tools
from omni.skills_runtime.context import ExecContext
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from tests.conftest import ScriptedLLM, python_shell_command


async def _runtime(tmp_path):
    settings = load_settings(cwd=tmp_path)
    settings.security.bash_sandbox = "workspace-write"
    settings.security.os_sandbox = "off"
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    recorder = TaskRecorder(db, project=settings.paths.project_name)
    task = await recorder.create_task(
        session_id="sess-bug09",
        channel="cli",
        user_input="Write a script, run it, and report the metric.",
    )
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        channel="cli",
        db=db,
        session_id="sess-bug09",
        task_id=task.id,
        task_recorder=recorder,
        working_dir=tmp_path,
        artifacts=ArtifactStore(settings.paths, db),
    )
    return recorder, task, ctx, db


def _gateway(ctx, recorder, task_id, tools):
    return ToolGateway(
        task_id=task_id,
        tools=tools,
        tasks=recorder,
        event_family="react",
    )


@pytest.mark.asyncio
async def test_generic_react_write_and_exec_records_artifact_and_run(tmp_path):
    recorder, task, ctx, db = await _runtime(tmp_path)
    script = tmp_path / "experiment.py"
    write_file = next(t for t in build_fs_tools(ctx) if t.spec.name == "write_file")
    bash = build_shell_tools(ctx)[0]
    gateway = _gateway(ctx, recorder, task.id, [write_file, bash])
    run_cmd = python_shell_command(
        "import json, os, pathlib;"
        "out = pathlib.Path(os.environ['OMNI_OUTPUT_DIR']) / 'results.json';"
        "out.write_text(json.dumps({'acc': 0.91}));"
        "print(out)"
    )
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "w1",
                        "write_file",
                        {
                            "path": str(script),
                            "contents": "print('acc=0.91')\n",
                        },
                    )
                ]
            ),
            ChatWithToolsResult(
                tool_calls=[ToolCall("b1", "bash", {"command": run_cmd})]
            ),
            ChatWithToolsResult(content="The script reported acc=0.91."),
        ]
    )
    react = ReActLoopAgent(llm, gateway.react_invoker(), max_iterations=4)
    await react.run(
        system_prompt="Write the script and run it.",
        user_message="Measure accuracy.",
        tools=gateway.tool_specs,
        on_tool_event=gateway.emit,
    )

    store = ResearchStore(db)
    counts = await store.counts()
    artifacts = await ctx.artifacts.list_by_task(task.id)
    names = {item.title for item in artifacts}

    assert "experiment" in names
    assert "results" in names
    assert counts["runs"] >= 1
    assert counts["claims"] == 0
    assert counts["sources"] == 0
    runs = await store.list_runs(session_id="sess-bug09")
    assert any(row.inputs.get("origin") == "host" for row in runs)

    report = await verify_session(store, session_id="sess-bug09")
    assert report.total_claims == 0
    assert report.run_count >= 1


@pytest.mark.asyncio
async def test_known_safe_bash_does_not_create_a_run(tmp_path):
    recorder, task, ctx, db = await _runtime(tmp_path)
    bash = build_shell_tools(ctx)[0]
    gateway = _gateway(ctx, recorder, task.id, [bash])
    await gateway.react_invoker()("bash", {"command": "git status --short"})
    store = ResearchStore(db)
    assert (await store.counts())["runs"] == 0
