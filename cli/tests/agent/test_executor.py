"""Skill executor: cli_exec, python_engine, prompt_only."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omni.agent.plan_revision import provider_authority_snapshot
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.termination import base_termination_reason
from omni.runtime.tool_gateway import ToolGateway
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.executor import (
    SkillExecutionError,
    _bounded_int_policy,
    _prompt_partial_outputs,
    execute_skill,
)
from omni.skills_runtime.manifest import (
    DeliveryMode,
    EngineSpec,
    ExecSpec,
    SkillEntry,
    SkillKind,
)
from tests.conftest import CapturingLLM, ScriptedLLM, python_shell_command


def _ctx(**kw):
    s = load_settings()
    s.paths.ensure_dirs()
    s.security.os_sandbox = "off"
    return ExecContext(settings=s, paths=s.paths, **kw)


def test_prompt_budget_default_is_distinct_from_its_ceiling():
    assert _bounded_int_policy(None, 20, 32) == 20
    assert _bounded_int_policy(28, 20, 32) == 28
    assert _bounded_int_policy(999, 20, 32) == 32


def test_prompt_partial_outputs_does_not_interpret_foreign_command_status():
    record = SimpleNamespace(
        name="bash",
        arguments={"command": "external"},
        error=None,
        status="succeeded",
        result={
            "result_schema": "external.command-result.v1",
            "command_status": "failed",
            "reason": "foreign_reason",
            "exit_code": 99,
        },
        to_observation=lambda: "foreign result",
    )

    assert _prompt_partial_outputs([record]) == [
        {
            "tool": "bash",
            "command": "external",
            "status": "ok",
            "transport_status": "succeeded",
            "observation": "foreign result",
        }
    ]


@pytest.mark.asyncio
async def test_cli_exec_uses_the_shared_sandbox_prefix(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_exec(*argv, **kwargs):  # noqa: ANN001
        captured["argv"] = argv

        class _Proc:
            returncode = 0

            async def communicate(self, _payload=None):
                return b'{"status":"ok"}', b""

        return _Proc()

    monkeypatch.setattr(
        "omni.skills_runtime.exec_io.confined_exec_prefix",
        lambda _ctx, extra_writable=(): ["sandbox-exec", "-p", "(version 1)"],
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    entry = SkillEntry(
        name="ex", description="d", kind=SkillKind.CLI_EXEC,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", "print(1)"], stdout_format="json"),
    )
    out = await execute_skill(entry, {}, _ctx())
    assert out["status"] == "ok"
    assert captured["argv"][:3] == ("sandbox-exec", "-p", "(version 1)")
    assert captured["argv"][3] == sys.executable


@pytest.mark.asyncio
async def test_cli_exec_skill_json_roundtrip():
    script = "import sys,json;d=json.load(sys.stdin);print(json.dumps({'got':d.get('q'),'status':'ok'}))"
    entry = SkillEntry(
        name="ex", description="d", kind=SkillKind.CLI_EXEC,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )
    out = await execute_skill(entry, {"q": "hello"}, _ctx())
    assert out["status"] == "ok"
    assert out["got"] == "hello"


@pytest.mark.asyncio
async def test_cli_exec_skill_delivers_omni_output_dir():
    from omni.skills_runtime.exec_io import OMNI_OUTPUT_ENV, durable_output_dir
    from omni.storage.artifacts import ArtifactStore
    from omni.storage.db import get_database
    from omni.storage.models import TaskORM

    script = (
        "import json,os,pathlib,sys;"
        f"p=pathlib.Path(os.environ['{OMNI_OUTPUT_ENV}'])/'plot.svg';"
        "p.write_text('<svg/>');"
        "print(json.dumps({'status':'ok','path':str(p)}))"
    )
    settings = load_settings()
    settings.paths.ensure_dirs()
    settings.security.os_sandbox = "off"
    db = get_database(settings.paths.project_db)
    await db.init()
    task_id = "f" * 32
    async with db.session() as session:
        session.add(TaskORM(id=task_id, status="running", kind="turn", title="cli-out"))
        await session.commit()
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        task_id=task_id,
        session_id="sess-cli",
        artifacts=ArtifactStore(settings.paths, db),
    )
    entry = SkillEntry(
        name="ex-out",
        description="d",
        kind=SkillKind.CLI_EXEC,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )
    out = await execute_skill(entry, {}, ctx)
    assert out["status"] == "ok"
    assert (durable_output_dir(ctx) / "plot.svg").is_file()
    rows = await ctx.artifacts.list_by_task(task_id)
    assert any(
        str(row.title or "") == "plot"
        and Path(str(row.rel_path or "")).suffix.lower() == ".svg"
        for row in rows
    )


@pytest.mark.asyncio
async def test_cli_exec_skill_sanitizes_lone_surrogates_before_stdin(monkeypatch):
    class SurrogateText:
        def __str__(self) -> str:
            return "custom\udc82value"

    script = (
        "import sys,json;d=json.load(sys.stdin);"
        "print(json.dumps({'got':d,'status':'ok'}))"
    )
    entry = SkillEntry(
        name="surrogate-safe",
        description="d",
        kind=SkillKind.CLI_EXEC,
        exec_spec=ExecSpec(
            command=sys.executable,
            args=["-c", script],
            stdout_format="json",
        ),
    )
    # Reproduce Windows redirected-pipe defaults on every platform. The CLI
    # skill transport must override this legacy code page with UTF-8.
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")

    out = await execute_skill(
        entry,
        {
            "q": "bad\udc81value",
            "nested": {"key\udc8f": ["ok", "\ud800"]},
            "custom": SurrogateText(),
        },
        _ctx(),
    )

    assert out["status"] == "ok"
    assert out["got"]["q"] == "bad\ufffdvalue"
    assert out["got"]["nested"] == {"key\ufffd": ["ok", "\ufffd"]}
    assert out["got"]["custom"] == "custom\ufffdvalue"


@pytest.mark.asyncio
async def test_python_engine_skill(tmp_path):
    mod = tmp_path / "fake_engine_mod.py"
    mod.write_text(
        "class Echo:\n"
        "    async def execute(self, **kw):\n"
        "        return {'status':'ok','echo':kw.get('value')}\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        entry = SkillEntry(
            name="eng", description="d", kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="fake_engine_mod", class_name="Echo", method="execute"),
        )
        out = await execute_skill(entry, {"value": 42}, _ctx())
        assert out == {"status": "ok", "echo": 42}
    finally:
        sys.path.remove(str(tmp_path))


@pytest.mark.asyncio
async def test_local_engine_cache_is_scoped_by_source_path_and_content(tmp_path):
    """Same-named shadowed skills must never share an in-process engine module."""
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    for root, marker in ((first_dir, "first"), (second_dir, "second")):
        (root / "engine.py").write_text(
            "class Engine:\n"
            "    async def execute(self, **kw):\n"
            f"        return {{'status': 'ok', 'marker': {marker!r}}}\n"
        )
    first = SkillEntry(
        name="shadowed-engine",
        source="project_omni",
        path=first_dir,
        description="first",
        kind=SkillKind.PYTHON_ENGINE,
        engine=EngineSpec(module="engine", class_name="Engine"),
    )
    second = SkillEntry(
        name="shadowed-engine",
        source="user_omni",
        path=second_dir,
        description="second",
        kind=SkillKind.PYTHON_ENGINE,
        engine=EngineSpec(module="engine", class_name="Engine"),
    )

    assert (await execute_skill(first, {}, _ctx()))["marker"] == "first"
    assert (await execute_skill(second, {}, _ctx()))["marker"] == "second"


@pytest.mark.asyncio
async def test_local_engine_cache_does_not_run_stale_code_after_file_change(tmp_path):
    skill_dir = tmp_path / "mutable"
    skill_dir.mkdir()
    engine = skill_dir / "engine.py"
    engine.write_text(
        "class Engine:\n"
        "    async def execute(self, **kw):\n"
        "        return {'status': 'ok', 'marker': 'before'}\n"
    )
    entry = SkillEntry(
        name="mutable-engine",
        source="project_omni",
        path=skill_dir,
        description="mutable",
        kind=SkillKind.PYTHON_ENGINE,
        engine=EngineSpec(module="engine", class_name="Engine"),
    )

    assert (await execute_skill(entry, {}, _ctx()))["marker"] == "before"
    engine.write_text(
        "class Engine:\n"
        "    async def execute(self, **kw):\n"
        "        return {'status': 'ok', 'marker': 'after'}\n"
    )

    assert (await execute_skill(entry, {}, _ctx()))["marker"] == "after"


def _engine_skill(tmp_path, name: str, marker: str) -> SkillEntry:
    skill_dir = tmp_path / name
    skill_dir.mkdir()
    (skill_dir / "engine.py").write_text(
        "class Engine:\n"
        "    async def execute(self, **kw):\n"
        f"        return {{'status': 'ok', 'marker': {marker!r}}}\n"
    )
    return SkillEntry(
        name=name,
        source="project_omni",
        path=skill_dir,
        description=name,
        kind=SkillKind.PYTHON_ENGINE,
        engine=EngineSpec(module="engine", class_name="Engine"),
    )


@pytest.mark.asyncio
async def test_a_sealed_execution_does_not_seal_the_next_skill_on_the_same_turn(
    tmp_path,
):
    """One execution's seal must not become the next skill's expected authority."""
    fetcher = _engine_skill(tmp_path, "abstract-fetcher", "fetched")
    plotter = _engine_skill(tmp_path, "figure-plotter", "plotted")
    turn_ctx = _ctx()

    execution_ctx = turn_ctx.for_execution(
        subtask_id="exec-1",
        provider_authority=provider_authority_snapshot(plotter),
    )
    assert (await execute_skill(plotter, {}, execution_ctx))["marker"] == "plotted"

    assert not turn_ctx.provider_authority
    # Same turn, a different skill: admitted exactly as on a fresh turn.
    assert (await execute_skill(fetcher, {}, turn_ctx))["marker"] == "fetched"


@pytest.mark.asyncio
async def test_authority_sealed_for_another_provider_is_never_reused_as_a_seal(
    tmp_path,
):
    """A foreign seal is refused as out of scope, not misreported as tampering."""
    fetcher = _engine_skill(tmp_path, "abstract-fetcher", "fetched")
    plotter = _engine_skill(tmp_path, "figure-plotter", "plotted")
    ctx = _ctx()
    ctx.provider_authority = provider_authority_snapshot(plotter)

    with pytest.raises(SkillExecutionError) as caught:
        await execute_skill(fetcher, {}, ctx)

    message = str(caught.value)
    assert "figure-plotter" in message and "abstract-fetcher" in message
    # Reserved for a provider actually rewritten under a queued run.
    assert "changed after enqueue" not in message


def test_derived_execution_context_keeps_delegation_depth_and_owner(tmp_path):
    """Derivation must carry non-field state and re-own lazily-read collaborators."""
    from omni.storage.artifacts import ContextArtifactStore

    ctx = _ctx(task_id="turn-1")
    ctx.artifacts = ContextArtifactStore(object(), ctx)
    ctx.subagent_depth = 2  # non-field: gates delegation nesting

    derived = ctx.for_execution(subtask_id="exec-1", task_id="task-1")

    assert derived.subagent_depth == 2
    # Artifacts recorded during the execution are owned by the execution.
    assert derived.artifacts._owned({}) == {  # noqa: SLF001
        "task_id": "task-1",
        "subtask_id": "exec-1",
    }
    assert ctx.artifacts._owned({}) == {"task_id": "turn-1"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_local_engine_cache_tracks_loaded_dependency_closure(tmp_path):
    skill_dir = tmp_path / "dependency-mutable"
    skill_dir.mkdir()
    helper_name = f"dependency_helper_{tmp_path.name.replace('-', '_')}"
    helper = skill_dir / f"{helper_name}.py"
    helper.write_text("MARKER = 'before'\n")
    (skill_dir / "engine.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        f"import {helper_name} as helper\n"
        "class Engine:\n"
        "    async def execute(self, **kw):\n"
        "        return {'status': 'ok', 'marker': helper.MARKER}\n"
    )
    entry = SkillEntry(
        name="dependency-mutable-engine",
        source="project_omni",
        path=skill_dir,
        description="mutable dependency",
        kind=SkillKind.PYTHON_ENGINE,
        engine=EngineSpec(module="engine", class_name="Engine"),
    )
    ctx = _ctx()
    try:
        ctx.provider_authority = provider_authority_snapshot(entry)
        assert (await execute_skill(entry, {}, ctx))["marker"] == "before"

        helper.write_text("MARKER = 'after'\n")
        ctx.provider_authority = provider_authority_snapshot(entry)

        assert (await execute_skill(entry, {}, ctx))["marker"] == "after"
    finally:
        sys.modules.pop(helper_name, None)


@pytest.mark.asyncio
async def test_local_engines_do_not_share_same_named_sibling_modules(tmp_path):
    helper_name = f"shared_helper_{tmp_path.name.replace('-', '_')}"
    entries = []
    roots = []
    for source, marker in (
        ("project_omni", "project"),
        ("user_omni", "user"),
    ):
        root = tmp_path / marker
        root.mkdir()
        roots.append(root)
        (root / f"{helper_name}.py").write_text(f"MARKER = {marker!r}\n")
        (root / "engine.py").write_text(
            "import asyncio\n"
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).parent))\n"
            f"import {helper_name} as helper\n"
            "class Engine:\n"
            "    async def execute(self, **kw):\n"
            "        await asyncio.sleep(0)\n"
            "        return {'status': 'ok', 'marker': helper.MARKER}\n"
        )
        entries.append(
            SkillEntry(
                name="same-import-engine",
                source=source,
                path=root,
                description=marker,
                kind=SkillKind.PYTHON_ENGINE,
                engine=EngineSpec(module="engine", class_name="Engine"),
            )
        )
    try:
        first, second = await asyncio.gather(
            execute_skill(entries[0], {}, _ctx()),
            execute_skill(entries[1], {}, _ctx()),
        )
        assert first["marker"] == "project"
        assert second["marker"] == "user"
    finally:
        sys.modules.pop(helper_name, None)
        for root in roots:
            while str(root) in sys.path:
                sys.path.remove(str(root))


@pytest.mark.asyncio
async def test_execute_skill_enforces_concrete_contract_before_and_after_engine(tmp_path):
    marker = tmp_path / "engine-called"
    mod = tmp_path / "bad_contract_engine.py"
    mod.write_text(
        "from pathlib import Path\n"
        "class BadContract:\n"
        "    async def execute(self, **kw):\n"
        f"        Path({str(marker)!r}).write_text('called')\n"
        "        return {'status': 'ok', 'count': 'not-an-integer'}\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        entry = SkillEntry(
            name="contracted-engine",
            description="d",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="bad_contract_engine", class_name="BadContract"),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"const": "ok"},
                    "count": {"type": "integer"},
                },
                "required": ["status", "count"],
                "additionalProperties": False,
            },
        )

        invalid_input = await execute_skill(entry, {}, _ctx())
        assert invalid_input["reason"] == "input_contract_violation"
        assert invalid_input["execution_started"] is False
        assert not marker.exists()

        invalid_output = await execute_skill(entry, {"query": "attention"}, _ctx())
        assert invalid_output["reason"] == "output_contract_violation"
        assert invalid_output["execution_started"] is True
        assert invalid_output["side_effect_maybe_committed"] is True
        assert marker.read_text() == "called"
    finally:
        sys.path.remove(str(tmp_path))


@pytest.mark.parametrize(
    ("null_field", "reason"),
    [
        ("input_schema", "input_contract_violation"),
        ("output_schema", "output_contract_violation"),
    ],
)
@pytest.mark.asyncio
async def test_execute_skill_rejects_explicit_null_schema_before_engine(
    tmp_path,
    null_field: str,
    reason: str,
) -> None:
    marker = tmp_path / f"{null_field}-engine-called"
    mod = tmp_path / f"{null_field}_engine.py"
    mod.write_text(
        "from pathlib import Path\n"
        "class Engine:\n"
        "    async def execute(self, **kw):\n"
        f"        Path({str(marker)!r}).write_text('called')\n"
        "        return {'status': 'ok'}\n"
    )
    schemas: dict[str, object] = {
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"status": {"const": "ok"}},
            "required": ["status"],
        },
    }
    schemas[null_field] = None
    sys.path.insert(0, str(tmp_path))
    try:
        entry = SkillEntry(
            name=f"explicit-null-{null_field}",
            description="invalid explicit null provider contract",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module=f"{null_field}_engine", class_name="Engine"),
            input_schema=schemas["input_schema"],
            input_schema_declared=True,
            output_schema=schemas["output_schema"],
            output_schema_declared=True,
        )

        result = await execute_skill(entry, {"query": "attention"}, _ctx())

        assert result["reason"] == reason
        assert result["contract_violation"] is True
        assert result["errors"][0]["keyword"] == "invalid_schema"
        assert result["execution_started"] is False
        assert not marker.exists()
    finally:
        sys.path.remove(str(tmp_path))


@pytest.mark.asyncio
async def test_nested_skill_policy_still_enforces_concrete_output_contract(tmp_path):
    mod = tmp_path / "nested_bad_contract_engine.py"
    mod.write_text(
        "class BadContract:\n"
        "    async def execute(self, **kw):\n"
        "        return {'status': 'ok', 'count': 'not-an-integer'}\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        entry = SkillEntry(
            name="nested-contracted-engine",
            description="d",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="nested_bad_contract_engine", class_name="BadContract"),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"status": {"const": "ok"}, "count": {"type": "integer"}},
                "required": ["status", "count"],
                "additionalProperties": False,
            },
        )
        ctx = _ctx()
        outer = ToolGateway(task_id="", tools=[], event_family="react")

        result = await outer.invoke_operation(
            "run_skill",
            {"skill_name": entry.name},
            invoke=lambda: execute_skill(entry, {"query": "attention"}, ctx),
            delegated_target=entry.name,
        )

        assert result["reason"] == "output_contract_violation"
        assert result["execution_started"] is True
    finally:
        sys.path.remove(str(tmp_path))


@pytest.mark.asyncio
async def test_python_engine_timeout_is_capped_by_trusted_settings(tmp_path):
    mod = tmp_path / "slow_engine_mod.py"
    mod.write_text(
        "import asyncio\n"
        "class Slow:\n"
        "    async def execute(self, **kw):\n"
        "        await asyncio.sleep(0.2)\n"
        "        return {'status':'ok'}\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        entry = SkillEntry(
            name="slow-engine",
            description="d",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="slow_engine_mod", class_name="Slow"),
            execution={"max_seconds": 30},
        )
        ctx = _ctx()
        ctx.settings.skills.max_python_seconds = 0.02

        with pytest.raises(Exception, match="timed out"):
            await execute_skill(entry, {}, ctx)
    finally:
        sys.path.remove(str(tmp_path))


@pytest.mark.asyncio
async def test_cli_timeout_is_capped_by_trusted_settings():
    script = "import time;time.sleep(0.2);print('{}')"
    entry = SkillEntry(
        name="slow-cli",
        description="d",
        kind=SkillKind.CLI_EXEC,
        exec_spec=ExecSpec(
            command=sys.executable,
            args=["-c", script],
            stdout_format="json",
            timeout_seconds=30,
        ),
    )
    ctx = _ctx()
    ctx.settings.skills.max_cli_seconds = 0.02

    with pytest.raises(Exception, match="timed out"):
        await execute_skill(entry, {}, ctx)


@pytest.mark.asyncio
async def test_skill_dir_engine_loads_from_package():
    """A built-in skill's ``engine.py`` is loaded from its package dir.

    Exercises the decoupling path: ``SKILL.md`` references the skill-local
    ``engine`` module (not a CLI-internal dotted name), and the executor loads
    it by file path. Uses an invalid id so it returns a graceful offline error
    (no network).
    """
    from omni.skills_runtime.registry import SkillRegistry

    reg = SkillRegistry(load_settings())
    reg.build_index()
    entry = reg.get("arxiv-fetch")
    assert entry is not None and entry.kind == SkillKind.PYTHON_ENGINE
    assert entry.engine is not None and entry.engine.module == "engine"
    out = await execute_skill(entry, {"identifier": "not-an-arxiv-id!!"}, _ctx())
    assert out["status"] == "error"
    assert out["retryable"] is False
    assert out["next_capabilities"] == ["literature.search"]
    assert "next_tools" not in out


@pytest.mark.asyncio
async def test_prompt_only_skill_runs_subagent():
    entry = SkillEntry(
        name="p", description="d", kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK, body="Do the thing.",
    )
    ctx = _ctx()
    ctx.llm = ScriptedLLM()  # returns final "done"
    out = await execute_skill(entry, {"input": "go"}, ctx)
    assert out["status"] == "ok"
    assert "done" in out["text"]


@pytest.mark.asyncio
async def test_prompt_only_skill_emits_nested_tool_progress():
    entry = SkillEntry(
        name="p", description="d", kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK, body="Write a file.",
    )
    ctx = _ctx()
    ctx.llm = ScriptedLLM([
        ChatWithToolsResult(
            tool_calls=[ToolCall("c1", "write_file", {"path": str(ctx.paths.project_dir / "out.txt"), "contents": "ok"})]
        ),
        ChatWithToolsResult(content="done"),
    ])
    events: list[dict] = []

    async def progress(stage: str, pct: float = 0.0, **data) -> None:
        events.append({"stage": stage, "pct": pct, **data})

    out = await execute_skill(entry, {"input": "go"}, ctx, progress_callback=progress)

    assert out["status"] == "ok"
    assert any(e["stage"] == "tool.start" and e.get("tool") == "write_file" for e in events)
    assert any(e["stage"] == "tool.done" and e.get("tool") == "write_file" for e in events)
    assert "write_file" in out["tools_used"]


@pytest.mark.asyncio
async def test_prompt_only_skill_persists_each_nested_tool_lifecycle_once(tmp_path):
    class RecordingTasks:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def append_event(self, task_id: str, **event) -> None:
            self.events.append({"task_id": task_id, **event})

    entry = SkillEntry(
        name="p",
        description="d",
        kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        body="Run a Bash command.",
        allowed_tools=["bash"],
    )
    tasks = RecordingTasks()
    ctx = _ctx(task_id="task-1", task_recorder=tasks, working_dir=tmp_path)
    ctx.settings.security.bash_sandbox = "workspace-write"
    ctx.settings.security.os_sandbox = "off"
    failing_command = python_shell_command("raise SystemExit(1)")
    ctx.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall("c1", "bash", {"command": failing_command})
                ]
            ),
            ChatWithToolsResult(content="The command failed with exit code 1."),
        ]
    )

    out = await execute_skill(entry, {"input": "go"}, ctx)

    assert out["status"] == "ok"
    nested = [
        event
        for event in tasks.events
        if event.get("tool_name") == "bash"
        and str(event["event_type"]).startswith("prompt_skill.tool.")
    ]
    assert [event["event_type"] for event in nested] == [
        "prompt_skill.tool.start",
        "prompt_skill.tool.done",
    ]
    assert nested[-1]["status"] == "succeeded"
    assert nested[-1]["output_json"]["command_status"] == "failed"
    assert nested[-1]["output_json"]["exit_code"] == 1
    assert out["partial_outputs"] == [
        {
            "tool": "bash",
            "command": failing_command,
            "status": "ok",
            "transport_status": "succeeded",
            "command_status": "failed",
            "reason": "nonzero_exit",
            "exit_code": 1,
            "observation": "[exit=1]\n",
        }
    ]


@pytest.mark.asyncio
async def test_prompt_only_skill_enforces_allowed_tools_contract():
    entry = SkillEntry(
        name="p", description="d", kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK, body="Write only.",
        allowed_tools=["write_file"],
    )
    ctx = _ctx()
    ctx.llm = CapturingLLM()

    out = await execute_skill(entry, {"input": "go"}, ctx)

    assert out["status"] == "ok"
    assert ctx.llm.tool_names_seen
    assert ctx.llm.tool_names_seen[0] == ["write_file"]


@pytest.mark.asyncio
async def test_prompt_only_skill_receives_installed_root_and_structured_inputs(tmp_path):
    skill_root = tmp_path / "paper-review"
    skill_root.mkdir()

    class RecordingLLM(ScriptedLLM):
        def __init__(self) -> None:
            super().__init__()
            self.messages = []

        async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001, ANN202
            self.messages = list(messages)
            return await super().chat_with_tools(messages, tools, **kwargs)

    entry = SkillEntry(
        name="paper-review",
        description="d",
        kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        body="Use scripts/extract_pdf_text.py and references/venues/iclr.md.",
        path=skill_root,
    )
    ctx = _ctx()
    ctx.llm = RecordingLLM()

    out = await execute_skill(
        entry,
        {"input": "Review paper.pdf", "venue": "ICLR", "mode": "strict"},
        ctx,
    )

    assert out["status"] == "ok"
    assert str(skill_root.resolve()) in ctx.llm.messages[0]["content"]
    assert '"venue": "ICLR"' in ctx.llm.messages[-1]["content"]
    assert '"mode": "strict"' in ctx.llm.messages[-1]["content"]


@pytest.mark.asyncio
async def test_prompt_only_skill_applies_per_tool_budget():
    entry = SkillEntry(
        name="p", description="d", kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK, body="Write twice.",
        allowed_tools=["write_file"],
        execution={"tool_limits": {"write_file": 1}, "max_tool_calls": 4},
    )
    ctx = _ctx()
    ctx.llm = ScriptedLLM([
        ChatWithToolsResult(
            tool_calls=[
                ToolCall("c1", "write_file", {"path": str(ctx.paths.project_dir / "one.txt"), "contents": "1"}),
                ToolCall("c2", "write_file", {"path": str(ctx.paths.project_dir / "two.txt"), "contents": "2"}),
            ]
        ),
        ChatWithToolsResult(content="done"),
    ])

    out = await execute_skill(entry, {"input": "go"}, ctx)

    assert out["status"] == "ok"
    assert out["total_tool_calls"] == 2
    assert any(
        "tool_limit_exceeded:1" in str(item.get("observation", ""))
        for item in out["partial_outputs"]
    )


@pytest.mark.asyncio
async def test_prompt_only_skill_returns_partial_contract_on_tool_limit():
    entry = SkillEntry(
        name="p", description="d", kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK, body="Write too many files.",
    )
    ctx = _ctx()
    ctx.settings.skills.default_prompt_tool_calls = 1
    ctx.llm = ScriptedLLM([
        ChatWithToolsResult(
            tool_calls=[
                ToolCall("c1", "write_file", {"path": str(ctx.paths.project_dir / "one.txt"), "contents": "1"}),
                ToolCall("c2", "write_file", {"path": str(ctx.paths.project_dir / "two.txt"), "contents": "2"}),
            ]
        )
    ])

    out = await execute_skill(entry, {"input": "go"}, ctx)

    assert out["status"] == "partial"
    assert out["warning"]
    assert out["recoverable"] is True
    # The loop forces a tool-free final pass at the bound, so the reason carries
    # a ``synthesized_`` prefix describing *how* the answer was produced. The
    # cause underneath it is unchanged.
    assert base_termination_reason(out["terminated_reason"]) == "max_tool_calls"
    assert base_termination_reason(out["error_info"]["code"]) == "max_tool_calls"
    # Replaying the same run under the same ceiling would only exhaust it again.
    assert out["error_info"]["retryable"] is False
    assert out["next_action"] == "re-run with a larger max_tool_calls budget"
    assert out["total_tool_calls"] == 1
    assert out["partial_outputs"]


@pytest.mark.asyncio
async def test_prompt_skill_manifest_budget_cannot_exceed_trusted_ceiling(tmp_path):
    entry = SkillEntry(
        name="over-budget-prompt",
        description="d",
        kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        body="Write three files.",
        allowed_tools=["write_file"],
        execution={"max_tool_calls": 999, "max_iterations": 999, "max_seconds": 999},
    )
    ctx = _ctx()
    ctx.settings.skills.max_prompt_tool_calls = 2
    ctx.settings.skills.max_prompt_iterations = 3
    ctx.settings.skills.max_prompt_seconds = 10
    ctx.llm = ScriptedLLM([
        ChatWithToolsResult(
            tool_calls=[
                ToolCall(f"c{i}", "write_file", {"path": str(tmp_path / f"{i}.txt"), "contents": str(i)})
                for i in range(3)
            ]
        )
    ])

    out = await execute_skill(entry, {"input": "go"}, ctx)

    assert out["status"] == "partial"
    assert base_termination_reason(out["terminated_reason"]) == "max_tool_calls"
    assert out["total_tool_calls"] == 2
    assert sum(path.exists() for path in tmp_path.iterdir()) == 2
