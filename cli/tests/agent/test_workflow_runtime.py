"""Workflow execution: dynamic multi-skill plans with durable task state."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import types
from pathlib import Path

import pytest
from sqlalchemy import select

from omni.agent import OmniAgent
from omni.agent.plan_revision import provider_authority_renewal_is_valid
from omni.config import load_settings
from omni.core.execution_control import ExecutionControl
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.runtime import provider_authority as provider_authority_runtime
from omni.runtime import workflow_runtime
from omni.runtime.notifications import InboxNotifier
from omni.runtime.subtask_runtime import SubtaskRuntime, WorkflowNeedsInput, _prepare_workflow_plan
from omni.runtime.workflow_plan import _step_failure_recoverable
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.manifest import DeliveryMode, EngineSpec, ExecSpec, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM, WorkflowRunORM, WorkflowStepORM
from tests.conftest import PlanningLLM, ScriptedLLM


def _sandbox_io(tmp_path: Path, name: str) -> Path:
    """Write target whose parent does not contain the isolated ``.omni`` store."""
    dest = tmp_path / "io" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _traces_ran_concurrently(traces: list[dict]) -> bool:
    """True when at least two recorded intervals overlap.

    A shared instant (``max(started) < min(finished)``) is too strict on
    Windows CI: spawning the third ``python -c`` often starts after the first
    0.2s sleep ends. Sum-of-work versus span is also too strict: a spawn gap
    after the first process inflates the span and hides a later overlapping
    pair (the 2026-08-21 Windows release failure). Serial steps never overlap;
    a gathered wave has at least one overlapping pair.
    """
    intervals = [(int(trace["started"]), int(trace["finished"])) for trace in traces]
    for index, (start_a, finish_a) in enumerate(intervals):
        for start_b, finish_b in intervals[index + 1 :]:
            if max(start_a, start_b) < min(finish_a, finish_b):
                return True
    return False


def test_windows_spawn_stagger_still_counts_as_concurrent() -> None:
    """The GitHub Windows failure: last start is 39ms after first finish."""
    traces = [
        {"started": 0, "finished": 200_000_000},
        {"started": 20_000_000, "finished": 220_000_000},
        {"started": 239_276_700, "finished": 439_276_700},
    ]
    assert _traces_ran_concurrently(traces)
    assert not (
        max(trace["started"] for trace in traces)
        < min(trace["finished"] for trace in traces)
    )


def test_windows_spawn_gap_after_first_step_still_counts_as_concurrent() -> None:
    """Release 2026-08-21: first cli_exec finished, then b and c overlapped."""
    traces = [
        {"started": 1_787_299_285_213_385_300, "finished": 1_787_299_285_413_798_900},
        {"started": 1_787_299_285_579_006_300, "finished": 1_787_299_285_779_861_500},
        {"started": 1_787_299_285_631_787_800, "finished": 1_787_299_285_832_744_000},
    ]
    work = sum(int(trace["finished"]) - int(trace["started"]) for trace in traces)
    span = max(int(trace["finished"]) for trace in traces) - min(
        int(trace["started"]) for trace in traces
    )
    assert work < span
    assert _traces_ran_concurrently(traces)


def test_strictly_serial_traces_are_not_concurrent() -> None:
    traces = [
        {"started": 0, "finished": 200},
        {"started": 200, "finished": 400},
        {"started": 400, "finished": 600},
    ]
    assert not _traces_ran_concurrently(traces)


def _workflow_skill(name: str, *, workflow: dict | None = None) -> SkillEntry:
    script = (
        "import json,sys;"
        "d=json.load(sys.stdin);"
        "print(json.dumps({"
        "'status':'ok',"
        "'summary':'ran '+d.get('skill_name',''),"
        "'skill':d.get('skill_name',''),"
        "'value':d.get('value'),"
        "'has_workflow_results': bool(d.get('workflow_results'))"
        "}))"
    )
    return SkillEntry(
        name=name,
        description=f"workflow fixture {name}",
        source="project_omni",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        workflow=workflow or {},
        capabilities=_fixture_capabilities(name),
        input_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "query": {"type": "string"},
                "identifier": {"type": "string", "format": "arxiv_id"},
                "skill_name": {"type": "string"},
                "value": {},
                "log_path": {"type": "string"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}, "summary": {"type": "string"}},
        },
        priority=500,
    )


@pytest.mark.parametrize(
    "result",
    [
        {"status": "error", "blocking": True, "recoverable": True},
        {
            "status": "error",
            "recoverable": True,
            "action_required": {"kind": "configure"},
        },
    ],
)
def test_terminal_step_result_overrides_continue_with_partial(
    result: dict[str, object],
) -> None:
    entry = _workflow_skill(
        "terminal-config",
        workflow={"failure_policy": "continue_with_partial"},
    )

    assert not _step_failure_recoverable({}, entry, result)


def test_continue_with_partial_still_recovers_ordinary_step_failure() -> None:
    entry = _workflow_skill(
        "optional-step",
        workflow={"failure_policy": "continue_with_partial"},
    )

    assert _step_failure_recoverable({}, entry, {"status": "error"})


def _delayed_workflow_skill(name: str, *, delay: float, concurrent_safe: bool) -> SkillEntry:
    script = (
        "import json,sys,time;"
        "d=json.load(sys.stdin);"
        "started=time.time_ns();"
        f"time.sleep({delay!r});"
        "finished=time.time_ns();"
        "trace=d.get('trace_path');"
        "open(trace,'w').write(json.dumps({'started':started,'finished':finished})) if trace else None;"
        f"print(json.dumps({{'status':'ok','summary':'ran {name}','value':d.get('value')}}))"
    )
    return SkillEntry(
        name=name,
        description=f"delayed workflow fixture {name}",
        source="project_omni",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        execution={"concurrent_safe": concurrent_safe},
        input_schema={
            "type": "object",
            "properties": {
                "value": {},
                "trace_path": {"type": "string"},
            },
        },
        output_schema={"type": "object", "properties": {"status": {"type": "string"}}},
    )


def _described_workflow_skill(
    name: str,
    description: str,
    *,
    phrases: list[str] | None = None,
    when_to_use: str = "",
    source: str = "builtin",
    priority: int = 0,
    capabilities: list[str] | None = None,
    status: str = "stable",
    replaced_by: str = "",
) -> SkillEntry:
    script = (
        "import json,sys;"
        "d=json.load(sys.stdin);"
        "print(json.dumps({"
        "'status':'ok',"
        f"'summary':'ran {name}',"
        f"'skill':'{name}',"
        "'input': d.get('input','')"
        "}))"
    )
    return SkillEntry(
        name=name,
        description=description,
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        trigger={"phrases": phrases or [], "when_to_use": when_to_use},
        when_to_use=when_to_use,
        source=source,
        priority=priority,
        capabilities=capabilities or [],
        status=status,
        replaced_by=replaced_by,
        input_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "query": {"type": "string"},
                "question": {"type": "string"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}, "summary": {"type": "string"}},
        },
    )


def _schema_workflow_skill(
    name: str,
    *,
    required: list[str] | None = None,
    description: str | None = None,
    phrases: list[str] | None = None,
    when_to_use: str = "",
) -> SkillEntry:
    required = list(required or [])
    script = (
        "import json,sys;"
        f"required={required!r};"
        "d=json.load(sys.stdin);"
        "missing=[k for k in required if not d.get(k)];"
        "print(json.dumps("
        "({'status':'error','error':'missing '+','.join(missing)} if missing else "
        "{'status':'ok','summary':'ran '+d.get('skill_name',''),"
        "'skill':d.get('skill_name',''),"
        "'input':d.get('input'),"
        "'query':d.get('query'),"
        "'identifier':d.get('identifier'),"
        "'payload':d})"
        "))"
    )
    properties = {key: {"type": "string"} for key in required}
    return SkillEntry(
        name=name,
        description=description or f"schema workflow fixture {name}",
        source="project_omni",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        input_schema={"type": "object", "properties": properties, "required": required},
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}, "summary": {"type": "string"}},
        },
        trigger={"phrases": phrases or [], "when_to_use": when_to_use},
        when_to_use=when_to_use,
        capabilities=_fixture_capabilities(name),
        priority=500,
    )


def _fixture_capabilities(name: str) -> list[str]:
    return {
        "literature-search": ["literature.search"],
        "openalex-search": ["literature.search"],
        "crossref-search": ["literature.search"],
        "arxiv-fetch": ["paper.fetch.arxiv"],
        "corpus-index": ["corpus.index"],
        "lit-qa": ["qa.grounded"],
        "paper-review": ["review.paper"],
        "paper-analysis": ["analysis.paper"],
        "contradiction-scan": ["evidence.contradiction_scan"],
        "scientific-figure": ["artifact.figure", "figure.architecture"],
    }.get(name, [])


_SKILL_FOR_CAPABILITY = {
    "literature.search": "literature-search",
    "paper.fetch.arxiv": "arxiv-fetch",
    "corpus.index": "corpus-index",
    "qa.grounded": "lit-qa",
    "review.paper": "paper-review",
    "artifact.figure": "scientific-figure",
}


def _model_workflow_steps(
    capabilities: list[str],
    *,
    topic: str = "Transformer/RAG related work",
    arxiv_id: str = "1706.03762",
) -> list[dict]:
    """The step list a model hands to ``run_workflow`` for these capabilities."""
    ids = {
        "literature.search": "lit",
        "paper.fetch.arxiv": "paper",
        "corpus.index": "index",
        "qa.grounded": "grounded_qa",
        "review.paper": "review",
        "artifact.figure": "diagram",
        "synthesis.final": "writing",
    }
    steps: list[dict] = []
    previous = ""
    for idx, capability in enumerate(capabilities, start=1):
        step_id = ids.get(capability, f"step_{idx}")
        input_data: dict[str, object] = {"input": topic}
        if capability == "literature.search":
            input_data = {"query": topic}
        elif capability == "paper.fetch.arxiv":
            input_data = {"identifier": arxiv_id}
        elif capability == "synthesis.final":
            input_data = {"topic": topic, "deliverable": "draft.section"}
        step: dict = {
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


def _run_workflow_script(
    steps: list[dict],
    *,
    goal: str,
    mode: str = "foreground",
    answer: str = "The workflow finished; see the step results above.",
) -> list[ChatWithToolsResult]:
    """Script one model turn that submits ``steps`` through ``run_workflow``.

    A background submission is terminal for the ReAct loop, so the trailing
    answer turn is only reached in foreground mode.
    """
    return [
        ChatWithToolsResult(
            tool_calls=[
                ToolCall(
                    id="call_workflow",
                    name="run_workflow",
                    arguments={"goal": goal, "mode": mode, "steps": steps},
                )
            ]
        ),
        ChatWithToolsResult(content=answer),
    ]


def _workflow_llm(script: list[ChatWithToolsResult]) -> PlanningLLM:
    """A model that classifies the turn as multi-step and then runs ``script``.

    ``PlanningLLM`` (not the bare ``ScriptedLLM``) is required whenever a step
    is the native synthesizer, because it also answers the synthesis prompt with
    a plausible draft instead of a scripted tool-call stub.
    """
    return PlanningLLM(
        [
            {
                "intent_type": "workflow",
                "confidence": 0.91,
                "outputs": ["workflow", "draft.section"],
                "execution_mode": "foreground",
                "provenance_mode": "light",
                "rationale": "multi-step request; the model sequences the steps",
            }
        ],
        script=script,
    )


def _failing_workflow_skill(
    name: str,
    message: str = "boom",
    *,
    description: str | None = None,
    phrases: list[str] | None = None,
    when_to_use: str = "",
    workflow: dict | None = None,
) -> SkillEntry:
    script = (
        "import json,sys;"
        "json.load(sys.stdin);"
        f"print(json.dumps({{'status':'error','error':{message!r},'summary':'failed fixture'}}))"
    )
    return SkillEntry(
        name=name,
        description=description or f"workflow failing fixture {name}",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        trigger={"phrases": phrases or [], "when_to_use": when_to_use},
        when_to_use=when_to_use,
        workflow=workflow or {},
        capabilities=_fixture_capabilities(name),
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}, "error": {"type": "string"}},
        },
    )


def _logging_workflow_skill(name: str, *, fail: bool = False) -> SkillEntry:
    script = (
        "import json, pathlib, sys;"
        "d=json.load(sys.stdin);"
        "p=pathlib.Path(d['log_path']);"
        "p.write_text(p.read_text() + d.get('skill_name','') + '\\n' if p.exists() else d.get('skill_name','') + '\\n');"
        + (
            f"print(json.dumps({{'status':'error','error':'{name} failed','summary':'failed {name}'}}))"
            if fail
            else f"print(json.dumps({{'status':'ok','summary':'ran {name}','skill':'{name}'}}))"
        )
    )
    return SkillEntry(
        name=name,
        description=f"workflow logging fixture {name}",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        input_schema={
            "type": "object",
            "properties": {"log_path": {"type": "string"}, "skill_name": {"type": "string"}},
            "required": ["log_path", "skill_name"],
        },
        output_schema={"type": "object", "properties": {"status": {"type": "string"}}},
    )


def _partial_workflow_skill(name: str) -> SkillEntry:
    script = (
        "import json,sys;"
        "json.load(sys.stdin);"
        "print(json.dumps({'status':'partial','warning':'stopped early','summary':'partial fixture'}))"
    )
    return SkillEntry(
        name=name,
        description=f"workflow partial fixture {name}",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


def _empty_result_skill(name: str) -> SkillEntry:
    script = "import json,sys;json.load(sys.stdin);print(json.dumps({'status':'ok','text':''}))"
    return SkillEntry(
        name=name,
        description=f"empty result fixture {name}",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


class _CaptureNotifier:
    def __init__(self) -> None:
        self.notes = []

    async def notify(self, note):
        self.notes.append(note)


class _ProgressMetadataEngine:
    async def execute(self, progress_callback=None, **input_data):
        if progress_callback is not None:
            await progress_callback("inner.progress", 0.5, step_id="engine-inner-step")
        return {"status": "ok", "summary": input_data.get("workflow_step_id", "")}


class _NestedToolEngine:
    """Emit the tool lifecycle a prompt sub-agent reports for each tool call.

    Mirrors ``_run_prompt_skill.on_tool_event``: a ``tool.start``/``tool.done``
    pair carrying the tool identity, the outcome, and a result payload that can
    be arbitrarily large.
    """

    async def execute(self, progress_callback=None, **input_data):  # noqa: ANN001, ANN003, ARG002
        if progress_callback is not None:
            await progress_callback("tool.start", 0.15, tool="write_file", arguments={})
            await progress_callback(
                "tool.done",
                0.85,
                tool="write_file",
                status="rejected",
                error="tool 'write_file' rejected by execution policy: tool_limit_exceeded:10",
                result={"body": "x" * 4096},
            )
        return {"status": "ok", "summary": "nested tool fixture"}


def _nested_tool_skill(name: str) -> SkillEntry:
    module = types.ModuleType("nested_tool_engine")
    module.NestedToolEngine = _NestedToolEngine
    sys.modules["nested_tool_engine"] = module
    return SkillEntry(
        name=name,
        description=f"nested tool fixture {name}",
        kind=SkillKind.PYTHON_ENGINE,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        engine=EngineSpec(module="nested_tool_engine", class_name="NestedToolEngine"),
    )


def _workflow_progress_skill(name: str) -> SkillEntry:
    module = types.ModuleType("workflow_progress_engine")
    module.ProgressMetadataEngine = _ProgressMetadataEngine
    sys.modules["workflow_progress_engine"] = module
    return SkillEntry(
        name=name,
        description=f"workflow progress fixture {name}",
        kind=SkillKind.PYTHON_ENGINE,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        engine=EngineSpec(module="workflow_progress_engine", class_name="ProgressMetadataEngine"),
    )


async def _runtime_with_skills(
    count: int,
    *,
    overrides: dict | None = None,
    llm=None,  # noqa: ANN001
) -> SubtaskRuntime:
    settings = load_settings(overrides=overrides)
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    registry = SkillRegistry(settings)
    registry.build_index()
    for idx in range(1, count + 1):
        registry.register(_workflow_skill(f"wf-skill-{idx}"))
    inbox = InboxNotifier(settings.paths.project_dir / "inbox.jsonl")

    def ctx_factory(session_id: str, channel: str) -> ExecContext:
        return ExecContext(
            settings=settings,
            paths=settings.paths,
            session_id=session_id,
            channel=channel,
            registry=registry,
            db=db,
            llm=llm,
        )

    return SubtaskRuntime(db, settings, registry, ctx_factory, notifier=inbox)


def test_research_workflow_trigger_candidates_cover_user_scenarios():
    registry = SkillRegistry(load_settings())
    registry.build_index()

    figure_hits = [
        e.name
        for e in registry.suggest(
            "Generate a scientific RAG system architecture figure with query, retriever, reranker, and LLM.",
            limit=5,
        )
    ]
    arxiv_hits = [
        e.name for e in registry.suggest("Fetch the abstract for arXiv 1706.03762.", limit=5)
    ]
    compound_hits = [
        e.name
        for e in registry.suggest(
            "Fetch arXiv 1706.03762, generate a scientific architecture figure, and produce a paper draft.",
            limit=10,
        )
    ]

    assert "scientific-figure" in figure_hits
    assert "arxiv-fetch" in arxiv_hits
    assert {"arxiv-fetch", "scientific-figure"}.issubset(compound_hits)


@pytest.mark.asyncio
async def test_workflow_task_runs_async_skills_inline_and_persists_steps():
    runtime = await _runtime_with_skills(3)
    workflow_run_id = await runtime.enqueue_workflow(
        "run three fixture skills",
        [
            {
                "id": "step_1",
                "skill": "wf-skill-1",
                "input": {"skill_name": "wf-skill-1", "value": 1},
            },
            {
                "id": "step_2",
                "skill": "wf-skill-2",
                "input": {"skill_name": "wf-skill-2", "value": 2},
            },
            {
                "id": "step_3",
                "skill": "wf-skill-3",
                "input": {"skill_name": "wf-skill-3", "value": 3},
            },
        ],
        "cli",
    )

    processed = await runtime.drain()

    assert workflow_run_id in processed
    workflow = await runtime.get_workflow_run(workflow_run_id)
    assert workflow is not None
    assert workflow.status == "succeeded"
    assert workflow.result_json["skills_used"] == ["wf-skill-1", "wf-skill-2", "wf-skill-3"]
    assert [s["status"] for s in workflow.result_json["steps"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert workflow.result_json["steps"][1]["result"]["has_workflow_results"] is True
    steps = await runtime.list_workflow_steps(workflow_run_id)
    assert [step.skill_name for step in steps] == ["wf-skill-1", "wf-skill-2", "wf-skill-3"]
    assert len({step.current_execution_id for step in steps}) == 3
    assert all(step.current_execution_id for step in steps)


@pytest.mark.asyncio
async def test_workflow_dispatch_rejects_provider_changed_after_enqueue(
    tmp_path,
):
    runtime = await _runtime_with_skills(1)
    marker = tmp_path / "replacement-executed.txt"
    workflow_run_id = await runtime.enqueue_workflow(
        "run one immutable fixture",
        [
            {
                "id": "step_1",
                "skill": "wf-skill-1",
                "input": {"skill_name": "wf-skill-1", "value": 1},
            }
        ],
        "cli",
    )
    replacement = _workflow_skill("wf-skill-1")
    replacement.exec_spec = ExecSpec(
        command=sys.executable,
        args=[
            "-c",
            (
                "from pathlib import Path;"
                f"Path({str(marker)!r}).write_text('executed');"
                'print(\'{"status":"ok"}\')'
            ),
        ],
        stdout_format="json",
    )
    runtime._registry.register(replacement)  # noqa: SLF001

    await runtime.process(workflow_run_id)

    workflow = await runtime.get_workflow_run(workflow_run_id)
    steps = await runtime.list_workflow_steps(workflow_run_id)
    execution = await runtime.get_subtask(steps[0].current_execution_id)
    assert workflow is not None
    assert execution is not None
    assert workflow.execution_authority_json
    assert steps[0].provider_authority_json
    assert execution.provider_authority_json
    assert workflow.status == "failed"
    assert execution.status == "failed"
    assert "provider execution authority changed" in execution.error
    assert not marker.exists()


@pytest.mark.asyncio
async def test_workflow_dispatch_rejects_tampered_authority_root(
    tmp_path,
) -> None:
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(_logging_workflow_skill("sealed-step"))  # noqa: SLF001
    marker = tmp_path / "tampered-root-executed.txt"
    workflow_run_id = await runtime.enqueue_workflow(
        "reject a tampered authority root",
        [
            {
                "id": "sealed",
                "skill": "sealed-step",
                "input": {
                    "skill_name": "sealed-step",
                    "log_path": str(marker),
                },
            }
        ],
        "cli",
    )
    async with runtime._db.session() as session:  # noqa: SLF001
        run = await session.get(WorkflowRunORM, workflow_run_id)
        assert run is not None
        authority = copy.deepcopy(run.execution_authority_json)
        authority["provider_authorities"][0]["contract"]["input_schema"] = {"type": "string"}
        run.execution_authority_json = authority
        await session.commit()

    await runtime.process(workflow_run_id)

    workflow = await runtime.get_workflow_run(workflow_run_id)
    steps = await runtime.list_workflow_steps(workflow_run_id)
    execution = await runtime.get_subtask(steps[0].current_execution_id)
    assert workflow is not None and workflow.status == "failed"
    assert execution is not None and execution.status == "failed"
    assert "renewal chain is invalid" in execution.error
    assert not marker.exists()


@pytest.mark.asyncio
async def test_workflow_dispatch_rejects_step_row_authority_divergence(
    tmp_path,
) -> None:
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(_logging_workflow_skill("sealed-step"))  # noqa: SLF001
    marker = tmp_path / "diverged-step-executed.txt"
    workflow_run_id = await runtime.enqueue_workflow(
        "reject a diverged workflow step",
        [
            {
                "id": "sealed",
                "skill": "sealed-step",
                "input": {
                    "skill_name": "sealed-step",
                    "log_path": str(marker),
                },
            }
        ],
        "cli",
    )
    async with runtime._db.session() as session:  # noqa: SLF001
        row = (
            await session.execute(
                select(WorkflowStepORM).where(
                    WorkflowStepORM.workflow_run_id == workflow_run_id,
                    WorkflowStepORM.step_key == "sealed",
                )
            )
        ).scalar_one()
        authority = copy.deepcopy(row.provider_authority_json)
        authority["consumer_id"] = "different-step"
        row.provider_authority_json = authority
        await session.commit()

    await runtime.process(workflow_run_id)

    workflow = await runtime.get_workflow_run(workflow_run_id)
    steps = await runtime.list_workflow_steps(workflow_run_id)
    execution = await runtime.get_subtask(steps[0].current_execution_id)
    assert workflow is not None and workflow.status == "failed"
    assert execution is not None and execution.status == "failed"
    assert "workflow step provider authority diverged" in execution.error
    assert not marker.exists()


@pytest.mark.asyncio
async def test_workflow_preserves_forced_skill_source_through_dispatch():
    runtime = await _runtime_with_skills(0)

    def sourced(source: str, marker: str) -> SkillEntry:
        script = (
            "import json,sys;"
            "json.load(sys.stdin);"
            f"print(json.dumps({{'status':'ok','summary':{marker!r},'marker':{marker!r}}}))"
        )
        return SkillEntry(
            name="same-name",
            source=source,
            description=marker,
            kind=SkillKind.CLI_EXEC,
            delivery_mode=DeliveryMode.ASYNC_TASK,
            exec_spec=ExecSpec(
                command=sys.executable,
                args=["-c", script],
                stdout_format="json",
            ),
        )

    runtime._registry.register(sourced("user_omni", "forced"))  # noqa: SLF001
    runtime._registry.register(sourced("builtin", "winner"))  # noqa: SLF001
    workflow_run_id = await runtime.enqueue_workflow(
        "run exact source",
        [
            {
                "id": "exact",
                "skill": "same-name",
                "skill_source": "user_omni",
                "input": {},
            }
        ],
        "cli",
    )

    await runtime.drain()

    workflow = await runtime.get_workflow_run(workflow_run_id)
    steps = await runtime.list_workflow_steps(workflow_run_id)
    execution = await runtime.get_subtask(steps[0].current_execution_id)
    assert workflow is not None and workflow.status == "succeeded"
    assert execution is not None
    assert execution.result_json["marker"] == "forced"
    assert steps[0].provider_authority_json["provider_source"] == "user_omni"


@pytest.mark.asyncio
async def test_standalone_subtask_dispatch_rejects_provider_changed_after_enqueue(
    tmp_path,
):
    runtime = await _runtime_with_skills(1)
    marker = tmp_path / "replacement-subtask-executed.txt"
    subtask_id = await runtime.enqueue(
        "wf-skill-1",
        {"skill_name": "wf-skill-1"},
        "cli",
    )
    replacement = _workflow_skill("wf-skill-1")
    replacement.exec_spec = ExecSpec(
        command=sys.executable,
        args=[
            "-c",
            (
                "from pathlib import Path;"
                f"Path({str(marker)!r}).write_text('executed');"
                'print(\'{"status":"ok"}\')'
            ),
        ],
        stdout_format="json",
    )
    runtime._registry.register(replacement)  # noqa: SLF001

    await runtime.process(subtask_id)

    execution = await runtime.get_subtask(subtask_id)
    assert execution is not None
    assert execution.provider_authority_json
    assert execution.status == "failed"
    assert "provider execution authority changed" in execution.error
    assert not marker.exists()


@pytest.mark.asyncio
async def test_provider_authority_rejection_closes_concurrent_row_change():
    runtime = await _runtime_with_skills(1)
    subtask_id = await runtime.enqueue(
        "wf-skill-1",
        {"skill_name": "wf-skill-1"},
        "",
    )
    claimed = await runtime._claim(subtask_id)  # noqa: SLF001
    assert claimed is not None
    original_authority = claimed[-1]
    async with runtime._db.session() as session:  # noqa: SLF001
        task = await session.get(SubtaskORM, subtask_id)
        assert task is not None
        task.provider_authority_json = {
            **original_authority,
            "fingerprint": "concurrent-reauthorization",
        }
        await session.commit()

    rejected = await runtime._fail_provider_authority(  # noqa: SLF001
        subtask_id,
        "provider execution authority changed during dispatch",
        original_authority,
        [],
        "",
    )

    execution = await runtime.get_subtask(subtask_id)
    assert rejected is True
    assert execution is not None
    assert execution.status == "failed"
    assert "changed during dispatch" in execution.error


@pytest.mark.asyncio
async def test_deterministic_workflow_does_not_falsely_acknowledge_steering():
    runtime = await _runtime_with_skills(1)
    workflow_run_id = await runtime.enqueue_workflow(
        "run deterministic fixture",
        [
            {
                "id": "step_1",
                "skill": "wf-skill-1",
                "input": {"skill_name": "wf-skill-1", "value": 1},
            }
        ],
        "cli",
    )
    control = ExecutionControl()
    control.push_steer("change the explanation style")
    ctx = runtime._ctx_factory("", "cli")  # noqa: SLF001
    ctx.execution_control = control

    await runtime.process(workflow_run_id, ctx_override=ctx)

    workflow = await runtime.get_workflow_run(workflow_run_id)
    assert workflow is not None and workflow.status == "succeeded"
    # A deterministic workflow cannot truthfully consume natural-language
    # steering. Leave it for the enclosing ReAct loop (or next-turn fallback).
    assert control.take_steering() == ["change the explanation style"]


@pytest.mark.asyncio
async def test_workflow_rejects_plan_above_trusted_step_ceiling():
    runtime = await _runtime_with_skills(3, overrides={"tasks": {"workflow_max_steps": 2}})
    subtask_id = await runtime.enqueue_workflow(
        "run too many fixture skills",
        [{"id": f"step_{idx}", "skill": f"wf-skill-{idx}", "input": {}} for idx in range(1, 4)],
        "cli",
    )

    await runtime.drain()
    task = await runtime.get_workflow_run(subtask_id)

    assert task is not None and task.status == "failed"
    assert "workflow_max_steps" in task.error


@pytest.mark.asyncio
async def test_workflow_aggregate_tool_envelope_stops_later_prompt_step():
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall("s1", "search_corpus", {"query": "RAG", "k": 1}),
                    ToolCall("s2", "search_corpus", {"query": "hallucination", "k": 1}),
                ]
            ),
            ChatWithToolsResult(content="first source pass complete"),
            ChatWithToolsResult(
                tool_calls=[ToolCall("s3", "search_corpus", {"query": "grounding", "k": 1})]
            ),
        ]
    )
    runtime = await _runtime_with_skills(
        0,
        overrides={"tasks": {"workflow_max_tool_calls": 2}},
        llm=llm,
    )
    for name in ("source-one", "source-two"):
        runtime._registry.register(  # noqa: SLF001
            SkillEntry(
                name=name,
                description=name,
                source="project_omni",
                kind=SkillKind.PROMPT_ONLY,
                delivery_mode=DeliveryMode.ASYNC_TASK,
                body="Search the local corpus and report findings.",
                allowed_tools=["search_corpus"],
                execution={"max_tool_calls": 4, "max_iterations": 3},
                input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
                output_schema={"type": "object", "properties": {"status": {"type": "string"}}},
            )
        )
    subtask_id = await runtime.enqueue_workflow(
        "search multiple sources",
        [
            {"id": "one", "skill": "source-one", "input": {"input": "RAG"}},
            {
                "id": "two",
                "skill": "source-two",
                "input": {"input": "grounding"},
                "depends_on": ["one"],
            },
        ],
        "cli",
    )

    await runtime.drain()
    task = await runtime.get_workflow_run(subtask_id)

    assert task is not None and task.status == "degraded"
    assert task.result_json["status"] == "degraded"
    assert task.result_json["execution_budget"]["tool_calls"]["limit"] == 2
    assert task.result_json["execution_budget"]["tool_calls"]["completed"] == 2
    assert [step["status"] for step in task.result_json["steps"]] == ["succeeded", "degraded"]


@pytest.mark.asyncio
async def test_workflow_aggregate_token_envelope_skips_unstarted_step():
    llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                content="first synthesis complete",
                usage={"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
            ),
        ]
    )
    runtime = await _runtime_with_skills(
        0,
        overrides={"cost": {"max_total_tokens": 50}},
        llm=llm,
    )
    for name in ("synthesis-one", "synthesis-two"):
        runtime._registry.register(  # noqa: SLF001
            SkillEntry(
                name=name,
                description=name,
                source="project_omni",
                kind=SkillKind.PROMPT_ONLY,
                delivery_mode=DeliveryMode.ASYNC_TASK,
                body="Synthesize the supplied evidence.",
                input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
                output_schema={"type": "object", "properties": {"status": {"type": "string"}}},
            )
        )
    subtask_id = await runtime.enqueue_workflow(
        "bounded synthesis",
        [
            {"id": "one", "skill": "synthesis-one", "input": {"input": "RAG"}},
            {
                "id": "two",
                "skill": "synthesis-two",
                "input": {"input": "grounding"},
                "depends_on": ["one"],
            },
        ],
        "cli",
    )

    await runtime.drain()
    task = await runtime.get_workflow_run(subtask_id)

    assert task is not None and task.status == "degraded"
    assert task.result_json["status"] == "degraded"
    assert [step["status"] for step in task.result_json["steps"]] == ["succeeded", "skipped"]
    assert task.result_json["steps"][1]["skip_reason"] == "workflow_max_total_tokens"
    assert task.result_json["execution_budget"]["usage"]["total_tokens"] == 60


@pytest.mark.asyncio
async def test_workflow_deadline_caps_child_cli_execution():
    runtime = await _runtime_with_skills(
        0,
        overrides={"tasks": {"workflow_max_seconds": 0.05}},
    )
    runtime._registry.register(  # noqa: SLF001
        _delayed_workflow_skill("slow-source", delay=0.3, concurrent_safe=False)
    )
    subtask_id = await runtime.enqueue_workflow(
        "bounded source collection",
        [{"id": "source", "skill": "slow-source", "input": {"value": "RAG"}}],
        "cli",
    )

    await runtime.drain()
    task = await runtime.get_workflow_run(subtask_id)

    assert task is not None and task.status == "failed"
    assert "timed out" in task.error
    assert task.result_json["execution_budget"]["max_seconds"] == 0.05


@pytest.mark.asyncio
async def test_runtime_does_not_rewrite_removed_diagram_generation_alias():
    runtime = await _runtime_with_skills(0)
    subtask_id = await runtime.enqueue(
        "diagram-generation",
        {
            "description": "帮我产出一个 transformer 的架构图，包含 Encoder/Decoder 和 Cross-Attention。"
        },
        "feishu",
        session_id="sess-figure",
    )

    task = await runtime.get_subtask(subtask_id)

    assert task is not None
    assert task.skill_name == "diagram-generation"
    assert "planned_skill_name" not in task.input_json

    await runtime.drain()
    task = await runtime.get_subtask(subtask_id)
    assert task is not None
    assert task.status == "failed"
    assert "unknown skill" in task.error


@pytest.mark.asyncio
async def test_runtime_preserves_valid_selected_user_diagram_extension():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(
        _described_workflow_skill(  # noqa: SLF001
            "user-mermaid-figure",
            "Generate Mermaid architecture diagrams and lightweight SVG sketches.",
            phrases=["架构图", "architecture diagram", "mermaid"],
            when_to_use="Use for user-installed Mermaid diagram drafts.",
            source="user_omni",
            capabilities=["figure.architecture", "artifact.svg"],
        )
    )

    subtask_id = await runtime.enqueue(
        "user-mermaid-figure",
        {
            "description": "帮我产出一个 transformer 的架构图，包含 Encoder/Decoder 和 Cross-Attention。"
        },
        "cli",
    )
    task = await runtime.get_subtask(subtask_id)

    assert task is not None
    assert task.skill_name == "user-mermaid-figure"
    assert "planned_skill_name" not in task.input_json


@pytest.mark.asyncio
async def test_runtime_respects_explicit_user_skill_choice_for_lightweight_diagram():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(
        _described_workflow_skill(  # noqa: SLF001
            "user-mermaid-figure",
            "Generate Mermaid architecture diagrams and lightweight SVG sketches.",
            phrases=["架构图", "architecture diagram", "mermaid"],
            when_to_use="Use for user-installed Mermaid diagram drafts.",
            source="user_omni",
            capabilities=["figure.architecture", "artifact.svg"],
        )
    )

    subtask_id = await runtime.enqueue(
        "user-mermaid-figure",
        {"description": "用 user-mermaid-figure 画一个 mermaid 草稿架构图。"},
        "cli",
    )
    task = await runtime.get_subtask(subtask_id)

    assert task is not None
    assert task.skill_name == "user-mermaid-figure"
    assert "planned_skill_name" not in task.input_json


@pytest.mark.asyncio
async def test_runtime_does_not_second_route_an_enqueued_provider():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(
        _described_workflow_skill(  # noqa: SLF001
            "project-figure",
            "Project-specific architecture diagram generator.",
            phrases=["架构图", "architecture diagram"],
            when_to_use="Use for this project's preferred architecture diagrams.",
            source="project_omni",
            capabilities=["figure.architecture", "artifact.svg"],
        )
    )

    subtask_id = await runtime.enqueue(
        "scientific-figure",
        {"description": "Generate a Transformer architecture figure."},
        "cli",
    )
    task = await runtime.get_subtask(subtask_id)

    assert task is not None
    assert task.skill_name == "scientific-figure"
    assert "planned_skill_name" not in task.input_json
    assert "capability_resolution" not in task.input_json


@pytest.mark.asyncio
async def test_runtime_fails_empty_successful_skill_result():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(_empty_result_skill("empty-output"))  # noqa: SLF001
    subtask_id = await runtime.enqueue("empty-output", {"input": "draw something"}, "cli")

    await runtime.process(subtask_id)
    task = await runtime.get_subtask(subtask_id)

    assert task is not None
    assert task.status == "failed"
    assert "empty result" in task.error
    assert task.result_json["status"] == "ok"


@pytest.mark.asyncio
async def test_nested_tool_keeps_its_name_and_outcome_through_the_workflow_relay():
    """A tool called inside a workflow step stays identifiable at the terminal.

    The relay used to forward only the stage and percentage, so the live view
    printed a run of anonymous tool glyphs and never showed that the calls
    behind them were being rejected — while the persisted ``*.tool.*`` events
    named the tool the whole time.
    """
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(_nested_tool_skill("nested-tool-skill"))  # noqa: SLF001
    run_id = await runtime.enqueue_workflow(
        "run nested tool fixture",
        [{"id": "outer_step", "skill": "nested-tool-skill", "input": {}}],
        "cli",
    )

    emitted: list[dict] = []

    async def capture(phase: str, data: dict) -> None:
        if phase == "task_progress":
            emitted.append(data)

    await runtime.drain(on_event=capture)

    done = [e for e in emitted if str(e.get("stage") or "").endswith("tool.done")]
    assert done, "the nested tool's completion never reached the event stream"
    assert done[0]["tool"] == "write_file"
    assert done[0]["status"] == "rejected"
    assert "tool_limit_exceeded" in done[0]["error"]

    # And that is what the terminal renders — no anonymous placeholder.
    from omni.cli.live_display import TurnDisplay, console

    display = TurnDisplay(verbosity="normal", status_line=False)
    with console.capture() as capture_out:
        display.tool_event("task_progress", done[0])
        display.end()
    rendered = " ".join(capture_out.get().split())
    assert "write_file" in rendered
    assert "unnamed" not in rendered

    # The run row carries the identity but not the payload: the trace is
    # rewritten in full on every tick, so results would compound quadratically.
    run = await runtime.get_workflow_run(run_id)
    assert run is not None
    traced = [e for e in run.trace_log if str(e.get("stage") or "").endswith("tool.done")]
    assert traced and traced[0]["tool"] == "write_file"
    assert "result" not in traced[0]


@pytest.mark.asyncio
async def test_workflow_progress_preserves_skill_step_id_metadata():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(_workflow_progress_skill("wf-progress-skill"))  # noqa: SLF001
    subtask_id = await runtime.enqueue_workflow(
        "run progress fixture",
        [{"id": "outer_step", "skill": "wf-progress-skill", "input": {}}],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None
    assert task.status == "succeeded"
    inner_events = [
        event for event in task.trace_log if event["stage"] == "workflow.step.inner.progress"
    ]
    assert inner_events
    assert inner_events[0]["step_id"] == "outer_step"
    assert inner_events[0]["skill_step_id"] == "engine-inner-step"


@pytest.mark.asyncio
async def test_task_runtime_marks_error_status_result_as_failed():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(_failing_workflow_skill("standalone-error", "tool limit"))  # noqa: SLF001
    subtask_id = await runtime.enqueue("standalone-error", {}, "cli")

    await runtime.drain()

    task = await runtime.get_subtask(subtask_id)
    assert task is not None
    assert task.status == "failed"
    assert task.error == "tool limit"
    assert task.result_json["status"] == "error"


@pytest.mark.asyncio
async def test_workflow_failure_persists_partial_state_and_native_synthesis_continues():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(_workflow_skill("literature-search"))  # noqa: SLF001
    runtime._registry.register(_failing_workflow_skill("scientific-figure", "render failed"))  # noqa: SLF001
    subtask_id = await runtime.enqueue_workflow(
        "write a recoverable research workflow",
        [
            {
                "id": "lit",
                "skill": "literature-search",
                "input": {"skill_name": "literature-search"},
            },
            {
                "id": "fig",
                "skill": "scientific-figure",
                "input": {"skill_name": "scientific-figure"},
            },
            {
                "id": "paper",
                "skill": "synthesis.final",
                "provider_type": "native_executor",
                "input": {"deliverable": "draft.section"},
                "depends_on": ["fig"],
            },
        ],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None
    assert task.status == "failed"
    assert task.error == "workflow step fig (scientific-figure) failed: render failed"
    assert task.result_json["status"] == "failed"
    # No model is wired (ctx.llm is None), so native synthesis emits the
    # deterministic template draft and is honestly recorded as degraded.
    assert [step["status"] for step in task.result_json["steps"]] == [
        "succeeded",
        "failed",
        "degraded",
    ]
    assert task.result_json["steps"][0]["result"]["skill"] == "literature-search"
    assert task.result_json["steps"][1]["error"] == "render failed"
    assert task.result_json["steps"][2]["result"]["deliverable"] == "draft.section"
    assert task.result_json["steps"][2]["result"]["synthesis_mode"] == "template_fallback"


@pytest.mark.asyncio
async def test_native_synthesis_output_contract_fails_workflow_step(
    monkeypatch: pytest.MonkeyPatch,
):
    async def malformed_synthesis(*_args, **_kwargs):
        return {"status": "ok", "summary": "missing the deliverable payload"}

    monkeypatch.setattr(workflow_runtime, "run_native_synthesis", malformed_synthesis)
    runtime = await _runtime_with_skills(0)
    workflow_run_id = await runtime.enqueue_workflow(
        "write a typed native deliverable",
        [
            {
                "id": "paper",
                "skill": "synthesis.final",
                "provider_type": "native_executor",
                "input": {"deliverable": "draft.section"},
            }
        ],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(workflow_run_id)
    assert task is not None
    assert task.status == "failed"
    result = task.result_json["steps"][0]["result"]
    assert result["contract_violation"] is True
    assert result["reason"] == "output_contract_violation"
    assert result["execution_started"] is True


@pytest.mark.asyncio
async def test_native_workflow_dispatch_rejects_authority_changed_after_enqueue(
    monkeypatch: pytest.MonkeyPatch,
):
    executed = False

    async def forbidden_synthesis(*_args, **_kwargs):
        nonlocal executed
        executed = True
        return {"status": "ok", "deliverable": "must not execute"}

    runtime = await _runtime_with_skills(0)
    workflow_run_id = await runtime.enqueue_workflow(
        "write a typed native deliverable",
        [
            {
                "id": "paper",
                "skill": "synthesis.final",
                "provider_type": "native_executor",
                "input": {"deliverable": "draft.section"},
            }
        ],
        "cli",
    )
    original_snapshot = provider_authority_runtime.native_provider_authority_snapshot

    def changed_snapshot(kind: str) -> dict:
        snapshot = original_snapshot(kind)
        return {**snapshot, "fingerprint": "changed-after-enqueue"}

    monkeypatch.setattr(
        provider_authority_runtime,
        "native_provider_authority_snapshot",
        changed_snapshot,
    )
    monkeypatch.setattr(
        workflow_runtime,
        "run_native_synthesis",
        forbidden_synthesis,
    )

    await runtime.process(workflow_run_id)

    task = await runtime.get_workflow_run(workflow_run_id)
    steps = await runtime.list_workflow_steps(workflow_run_id)
    assert task is not None
    assert task.status == "failed"
    assert steps[0].provider_authority_json["provider_name"] == "native_synthesis"
    assert "provider execution authority changed" in steps[0].error
    assert task.result_json["steps"][0]["result"]["execution_started"] is False
    assert executed is False


@pytest.mark.asyncio
async def test_child_agent_dispatch_rejects_authority_changed_after_enqueue(
    monkeypatch: pytest.MonkeyPatch,
):
    runtime = await _runtime_with_skills(0)
    workflow_run_id = await runtime.enqueue_workflow(
        "delegate a focused task",
        [
            {
                "id": "specialist",
                "provider_type": "child_task",
                "capability": "agent.delegate",
                "input": {"goal": "analyze one paper", "tools": []},
            }
        ],
        "cli",
    )
    original_snapshot = provider_authority_runtime.native_provider_authority_snapshot

    def changed_snapshot(kind: str) -> dict:
        snapshot = original_snapshot(kind)
        return {**snapshot, "fingerprint": "changed-child-authority"}

    monkeypatch.setattr(
        provider_authority_runtime,
        "native_provider_authority_snapshot",
        changed_snapshot,
    )

    await runtime.process(workflow_run_id)

    task = await runtime.get_workflow_run(workflow_run_id)
    steps = await runtime.list_workflow_steps(workflow_run_id)
    assert task is not None
    assert task.status == "failed"
    assert steps[0].provider_authority_json["provider_name"] == "agent_delegate"
    assert "provider execution authority changed" in steps[0].error
    assert steps[0].child_task_id == ""


@pytest.mark.asyncio
async def test_child_agent_dispatch_seals_dynamic_sync_skill_providers(
    monkeypatch: pytest.MonkeyPatch,
):
    runtime = await _runtime_with_skills(0)
    child_tool = _workflow_skill("child-visible")
    child_tool.delivery_mode = DeliveryMode.SYNC_TOOL
    runtime._registry.register(child_tool)  # noqa: SLF001
    workflow_run_id = await runtime.enqueue_workflow(
        "delegate with one tool",
        [
            {
                "id": "specialist",
                "provider_type": "child_task",
                "capability": "agent.delegate",
                "input": {
                    "goal": "use the selected tool",
                    "tools": ["child-visible"],
                },
            }
        ],
        "cli",
    )
    replacement = _workflow_skill("child-visible")
    replacement.delivery_mode = DeliveryMode.SYNC_TOOL
    replacement.exec_spec = ExecSpec(
        command=sys.executable,
        args=[
            "-c",
            "import json,sys;json.load(sys.stdin);"
            "print(json.dumps({'status':'ok','summary':'replacement'}))",
        ],
        stdout_format="json",
    )
    runtime._registry.register(replacement)  # noqa: SLF001
    executed = False

    async def forbidden_child(*_args, **_kwargs):
        nonlocal executed
        executed = True
        raise AssertionError("changed child provider must not execute")

    monkeypatch.setattr(
        workflow_runtime,
        "execute_child_task",
        forbidden_child,
    )

    await runtime.process(workflow_run_id)

    workflow = await runtime.get_workflow_run(workflow_run_id)
    steps = await runtime.list_workflow_steps(workflow_run_id)
    assert workflow is not None and workflow.status == "failed"
    assert "provider execution authority changed" in steps[0].error
    assert executed is False


@pytest.mark.asyncio
async def test_workflow_continues_after_recoverable_skill_failure():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(_workflow_skill("literature-search"))  # noqa: SLF001
    runtime._registry.register(_workflow_skill("corpus-index"))  # noqa: SLF001
    runtime._registry.register(
        _failing_workflow_skill(  # noqa: SLF001
            "lit-qa",
            "tool budget exhausted",
            workflow={"failure_policy": "continue_with_partial"},
        )
    )
    runtime._registry.register(
        _workflow_skill(  # noqa: SLF001
            "scientific-figure",
            workflow={"allow_failed_dependencies": True, "failure_policy": "continue_with_partial"},
        )
    )
    runtime._registry.register(
        _workflow_skill(  # noqa: SLF001
            "paper-review",
            workflow={"allow_failed_dependencies": True, "failure_policy": "continue_with_partial"},
        )
    )

    subtask_id = await runtime.enqueue_workflow(
        "prepare a transformer submission section with grounded QA, figure, writing, review",
        [
            {
                "id": "literature_search",
                "skill": "literature-search",
                "input": {"skill_name": "literature-search"},
            },
            {
                "id": "corpus_index",
                "skill": "corpus-index",
                "input": {"skill_name": "corpus-index"},
                "depends_on": ["literature_search"],
            },
            {
                "id": "grounded_qa",
                "skill": "lit-qa",
                "input": {"input": "What does the literature say?"},
                "depends_on": ["corpus_index"],
            },
            {
                "id": "scientific_figure",
                "skill": "scientific-figure",
                "input": {"skill_name": "scientific-figure"},
                "depends_on": ["literature_search", "corpus_index", "grounded_qa"],
            },
            {
                "id": "final_synthesis",
                "skill": "synthesis.final",
                "provider_type": "native_executor",
                "input": {"deliverable": "draft.section"},
                "depends_on": ["literature_search", "grounded_qa"],
            },
            {
                "id": "paper_review",
                "skill": "paper-review",
                "input": {"skill_name": "paper-review"},
                "depends_on": ["literature_search", "grounded_qa", "final_synthesis"],
            },
        ],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None
    assert task.status == "degraded"
    assert task.error == ""
    assert task.result_json["status"] == "degraded"
    # Offline (no LLM): the native synthesis step degrades honestly instead of
    # reporting a template skeleton as a finished deliverable.
    assert task.result_json["workflow"]["succeeded"] == 4
    assert task.result_json["workflow"]["degraded"] == 1
    assert task.result_json["workflow"]["recoverable_failed"] == 1
    assert [step["status"] for step in task.result_json["steps"]] == [
        "succeeded",
        "succeeded",
        "failed",
        "succeeded",
        "degraded",
        "succeeded",
    ]
    qa_step = task.result_json["steps"][2]
    assert qa_step["recoverable"] is True
    assert qa_step["failure_policy"] == "continue_with_partial"
    figure_payload = task.result_json["steps"][3]["result"]
    assert figure_payload["has_workflow_results"] is True


@pytest.mark.asyncio
async def test_workflow_checkpoint_skips_completed_steps_on_resume(tmp_path):
    runtime = await _runtime_with_skills(0)
    log_path = _sandbox_io(tmp_path, "workflow.log")
    runtime._registry.register(_logging_workflow_skill("step-one"))  # noqa: SLF001
    runtime._registry.register(_logging_workflow_skill("step-two", fail=True))  # noqa: SLF001
    subtask_id = await runtime.enqueue_workflow(
        "checkpoint resume workflow",
        [
            {
                "id": "one",
                "skill": "step-one",
                "input": {"skill_name": "step-one", "log_path": str(log_path)},
            },
            {
                "id": "two",
                "skill": "step-two",
                "input": {"skill_name": "step-two", "log_path": str(log_path)},
                "depends_on": ["one"],
            },
        ],
        "cli",
    )

    await runtime.drain()

    failed = await runtime.get_workflow_run(subtask_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.result_json["checkpoint"]["last_completed_step_id"] == "one"
    assert failed.result_json["checkpoint"]["failed_step_ids"] == ["two"]
    assert failed.result_json["checkpoint"]["pending_steps"] == []
    checkpoints = await runtime.list_checkpoints(subtask_id)
    assert checkpoints
    assert checkpoints[-1].last_completed_step_id == "one"
    assert "one" in checkpoints[-1].completed_step_ids
    assert log_path.read_text().splitlines() == ["step-one", "step-two"]

    runtime._registry.register(_logging_workflow_skill("step-two"))  # noqa: SLF001
    assert await runtime.resume_workflow_step(subtask_id, "two") is True
    await runtime.process(subtask_id)

    resumed = await runtime.get_workflow_run(subtask_id)
    assert resumed is not None
    assert resumed.status == "succeeded"
    assert [step["status"] for step in resumed.result_json["steps"]] == ["succeeded", "succeeded"]
    assert log_path.read_text().splitlines() == ["step-one", "step-two", "step-two"]


@pytest.mark.asyncio
async def test_workflow_runs_dependency_ready_safe_steps_in_parallel(tmp_path: Path):
    runtime = await _runtime_with_skills(0, overrides={"tasks": {"workflow_concurrency": 3}})
    for name in ("branch-a", "branch-b", "branch-c"):
        runtime._registry.register(  # noqa: SLF001
            _delayed_workflow_skill(name, delay=0.2, concurrent_safe=True)
        )
    subtask_id = await runtime.enqueue_workflow(
        "parallel DAG",
        [
            {
                "id": "a",
                "skill": "branch-a",
                "input": {"value": "a", "trace_path": str(_sandbox_io(tmp_path, "a.json"))},
            },
            {
                "id": "b",
                "skill": "branch-b",
                "input": {"value": "b", "trace_path": str(_sandbox_io(tmp_path, "b.json"))},
            },
            {
                "id": "c",
                "skill": "branch-c",
                "input": {"value": "c", "trace_path": str(_sandbox_io(tmp_path, "c.json"))},
            },
        ],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None and task.status == "succeeded"
    assert [step["id"] for step in task.result_json["steps"]] == ["a", "b", "c"]
    traces = [
        json.loads(_sandbox_io(tmp_path, f"{step}.json").read_text(encoding="utf-8"))
        for step in ("a", "b", "c")
    ]
    assert _traces_ran_concurrently(traces)


@pytest.mark.asyncio
async def test_workflow_keeps_unsafe_ready_steps_serial(tmp_path: Path):
    runtime = await _runtime_with_skills(0, overrides={"tasks": {"workflow_concurrency": 3}})
    for name in ("unsafe-a", "unsafe-b"):
        runtime._registry.register(  # noqa: SLF001
            _delayed_workflow_skill(name, delay=0.18, concurrent_safe=False)
        )
    subtask_id = await runtime.enqueue_workflow(
        "serial barrier",
        [
            {
                "id": "a",
                "skill": "unsafe-a",
                "input": {"trace_path": str(_sandbox_io(tmp_path, "a.json"))},
            },
            {
                "id": "b",
                "skill": "unsafe-b",
                "input": {"trace_path": str(_sandbox_io(tmp_path, "b.json"))},
            },
        ],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None and task.status == "succeeded"
    a = json.loads(_sandbox_io(tmp_path, "a.json").read_text(encoding="utf-8"))
    b = json.loads(_sandbox_io(tmp_path, "b.json").read_text(encoding="utf-8"))
    assert a["finished"] <= b["started"] or b["finished"] <= a["started"]


@pytest.mark.asyncio
async def test_workflow_dag_waits_for_dependencies_before_downstream_step():
    runtime = await _runtime_with_skills(0, overrides={"tasks": {"workflow_concurrency": 3}})
    runtime._registry.register(_delayed_workflow_skill("root-a", delay=0.1, concurrent_safe=True))  # noqa: SLF001
    runtime._registry.register(_delayed_workflow_skill("root-b", delay=0.1, concurrent_safe=True))  # noqa: SLF001
    runtime._registry.register(_delayed_workflow_skill("join", delay=0.01, concurrent_safe=True))  # noqa: SLF001
    subtask_id = await runtime.enqueue_workflow(
        "join DAG",
        [
            {"id": "a", "skill": "root-a", "input": {}},
            {"id": "b", "skill": "root-b", "input": {}},
            {"id": "join", "skill": "join", "input": {}, "depends_on": ["a", "b"]},
        ],
        "cli",
    )
    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None and task.status == "succeeded"
    assert task.result_json["steps"][-1]["id"] == "join"
    assert task.result_json["steps"][-1]["result"]["value"] is None


@pytest.mark.asyncio
async def test_retry_workflow_step_reuses_upstream_checkpoint_and_reruns_descendants(tmp_path):
    runtime = await _runtime_with_skills(0)
    log_path = _sandbox_io(tmp_path, "step-retry.log")
    runtime._registry.register(_logging_workflow_skill("upstream"))  # noqa: SLF001
    runtime._registry.register(_logging_workflow_skill("target", fail=True))  # noqa: SLF001
    runtime._registry.register(_logging_workflow_skill("downstream"))  # noqa: SLF001
    original_id = await runtime.enqueue_workflow(
        "step retry",
        [
            {
                "id": "up",
                "skill": "upstream",
                "input": {"skill_name": "upstream", "log_path": str(log_path)},
            },
            {
                "id": "target",
                "skill": "target",
                "depends_on": ["up"],
                "input": {"skill_name": "target", "log_path": str(log_path)},
            },
            {
                "id": "down",
                "skill": "downstream",
                "depends_on": ["target"],
                "input": {"skill_name": "downstream", "log_path": str(log_path)},
            },
        ],
        "cli",
    )
    await runtime.drain()
    original = await runtime.get_workflow_run(original_id)
    assert original is not None and original.status == "failed"
    original_envelope_provider_fingerprints = [
        item["fingerprint"] for item in original.execution_authority_json["provider_authorities"]
    ]
    before = await runtime.list_workflow_steps(original_id)
    target_before = next(step for step in before if step.step_key == "target")
    stable_step_id = target_before.id
    first_execution_id = target_before.current_execution_id
    first_authority = target_before.provider_authority_json["fingerprint"]

    runtime._registry.register(_logging_workflow_skill("target"))  # noqa: SLF001
    retry_id = await runtime.retry_workflow_step(original_id, "target")
    assert retry_id
    await runtime.drain()

    retried = await runtime.get_subtask(retry_id)
    workflow = await runtime.get_workflow_run(original_id)
    after = await runtime.list_workflow_steps(original_id)
    target_after = next(step for step in after if step.step_key == "target")
    assert retried is not None and retried.status == "succeeded"
    assert workflow is not None and workflow.status == "succeeded"
    assert target_after.id == stable_step_id
    assert target_after.current_execution_id == retry_id
    assert target_after.execution_ids == [first_execution_id, retry_id]
    assert retried.retry_of == first_execution_id
    assert retried.step_attempt == 2
    assert retried.recovery_policy == "retry_workflow_step:target"
    assert target_after.provider_authority_json["fingerprint"] != first_authority
    assert (
        retried.provider_authority_json["fingerprint"]
        == target_after.provider_authority_json["fingerprint"]
    )
    assert (
        workflow.execution_authority_json["provider_authority_renewals"][-1]["action"]
        == "retry_workflow_step:target"
    )
    assert provider_authority_renewal_is_valid(
        workflow.execution_authority_json["provider_authority_renewals"][-1]
    )
    assert [
        item["fingerprint"] for item in workflow.execution_authority_json["provider_authorities"]
    ] == original_envelope_provider_fingerprints
    assert [step["status"] for step in workflow.result_json["steps"]] == ["succeeded"] * 3
    assert log_path.read_text().splitlines() == [
        "upstream",
        "target",
        "target",
        "downstream",
    ]


@pytest.mark.asyncio
async def test_running_workflow_cannot_be_retried_or_resumed():
    runtime = await _runtime_with_skills(1)
    workflow_run_id = await runtime.enqueue_workflow(
        "in flight",
        [{"id": "step", "skill": "wf-skill-1", "input": {}}],
        "cli",
    )
    from sqlalchemy import update

    from omni.storage.models import WorkflowRunORM

    async with runtime._db.session() as session:  # noqa: SLF001
        await session.execute(
            update(WorkflowRunORM)
            .where(WorkflowRunORM.id == workflow_run_id)
            .values(status="running")
        )
        await session.commit()

    assert await runtime.retry_workflow_step(workflow_run_id, "step") is None
    assert await runtime.resume_workflow_step(workflow_run_id, "step") is False
    workflow = await runtime.get_workflow_run(workflow_run_id)
    steps = await runtime.list_workflow_steps(workflow_run_id)
    assert workflow is not None and workflow.status == "running"
    assert len(steps[0].execution_ids) == 1


@pytest.mark.asyncio
async def test_resume_workflow_step_in_place_reuses_upstream_checkpoint(tmp_path):
    runtime = await _runtime_with_skills(0)
    log_path = _sandbox_io(tmp_path, "step-resume.log")
    runtime._registry.register(_logging_workflow_skill("upstream"))  # noqa: SLF001
    runtime._registry.register(_logging_workflow_skill("target", fail=True))  # noqa: SLF001
    subtask_id = await runtime.enqueue_workflow(
        "step resume",
        [
            {
                "id": "up",
                "skill": "upstream",
                "input": {"skill_name": "upstream", "log_path": str(log_path)},
            },
            {
                "id": "target",
                "skill": "target",
                "depends_on": ["up"],
                "input": {"skill_name": "target", "log_path": str(log_path)},
            },
        ],
        "cli",
    )
    await runtime.drain()
    before = await runtime.list_workflow_steps(subtask_id)
    authority_before = next(
        step for step in before if step.step_key == "target"
    ).provider_authority_json["fingerprint"]
    runtime._registry.register(_logging_workflow_skill("target"))  # noqa: SLF001

    assert await runtime.resume_workflow_step(subtask_id, "target") is True
    await runtime.process(subtask_id)

    resumed = await runtime.get_workflow_run(subtask_id)
    assert resumed is not None and resumed.status == "succeeded"
    steps = await runtime.list_workflow_steps(subtask_id)
    target = next(step for step in steps if step.step_key == "target")
    execution = await runtime.get_subtask(target.current_execution_id)
    assert execution is not None
    assert execution.recovery_policy == "resume_workflow_step:target"
    assert target.provider_authority_json["fingerprint"] != authority_before
    assert (
        execution.provider_authority_json["fingerprint"]
        == target.provider_authority_json["fingerprint"]
    )
    assert (
        resumed.execution_authority_json["provider_authority_renewals"][-1]["action"]
        == "resume_workflow_step:target"
    )
    assert provider_authority_renewal_is_valid(
        resumed.execution_authority_json["provider_authority_renewals"][-1]
    )
    assert log_path.read_text().splitlines() == ["upstream", "target", "target"]


@pytest.mark.asyncio
async def test_workflow_records_partial_skill_result_as_degraded():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(_workflow_skill("search"))  # noqa: SLF001
    runtime._registry.register(_partial_workflow_skill("writer"))  # noqa: SLF001

    subtask_id = await runtime.enqueue_workflow(
        "run partial workflow",
        [
            {"id": "search", "skill": "search", "input": {"skill_name": "search"}},
            {"id": "write", "skill": "writer", "input": {}, "depends_on": ["search"]},
        ],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None
    assert task.status == "degraded"
    assert task.result_json["status"] == "degraded"
    assert task.result_json["workflow"]["degraded"] == 1
    assert [step["status"] for step in task.result_json["steps"]] == ["succeeded", "degraded"]
    assert task.result_json["steps"][1]["warning"] == "stopped early"


@pytest.mark.asyncio
async def test_workflow_uses_real_skill_executions_and_degrades_parent_task():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_workflow_skill("search"))
    agent.registry.register(_partial_workflow_skill("writer"))
    try:
        parent = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="search and draft with partial delivery",
        )
        workflow_run_id = await agent.runtime.enqueue_workflow(
            "search and draft",
            [
                {"id": "search", "skill": "search", "input": {"skill_name": "search"}},
                {"id": "write", "skill": "writer", "input": {}, "depends_on": ["search"]},
            ],
            "cli",
            session_id=parent.session_id,
            task_id=parent.id,
        )

        await agent.runtime.drain()

        parent = await agent.tasks.get_task(parent.id)
        workflow = await agent.runtime.get_workflow_run(workflow_run_id)
        steps = await agent.runtime.list_workflow_steps(workflow_run_id)
        executions = [
            execution
            for execution in await agent.runtime.list_subtasks(limit=100)
            if execution.workflow_run_id == workflow_run_id
        ]
        events = await agent.tasks.list_events(parent.id)

        assert parent is not None and parent.status == "degraded"
        assert workflow is not None and workflow.status == "degraded"
        assert [step.skill_name for step in steps] == ["search", "writer"]
        assert len({step.current_execution_id for step in steps}) == 2
        assert {execution.skill_name for execution in executions} == {"search", "writer"}
        assert all(execution.skill_name != "workflow" for execution in executions)
        linked_events = [event for event in events if event.subtask_id]
        # Every record tied to an execution names the skill that ran it, whether
        # as the acting skill or as the dispatched tool, and locates itself in
        # the plan. Tool-level records used to carry none of this, which is why
        # ``omni task show`` could place only the progress ticks (run 138c7b6e).
        assert {
            event.skill_name or event.tool_name for event in linked_events
        } == {"search", "writer"}
        assert all(event.workflow_step_id for event in linked_events)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_nested_workflow_planning_creates_child_task_not_skill_execution():
    settings = load_settings()
    settings.subagents.reviewer_enabled = False
    agent = await OmniAgent.create(settings)
    agent.llm = ScriptedLLM([ChatWithToolsResult(content="specialist result")])
    try:
        parent = await agent.tasks.create_task(
            session_id=await agent.ensure_session(channel="cli"),
            channel="cli",
            user_input="delegate a focused paper analysis",
        )
        workflow_run_id = await agent.runtime.enqueue_workflow(
            "delegate paper analysis",
            [
                {
                    "id": "specialist",
                    "provider_type": "child_task",
                    "capability": "agent.delegate",
                    "input": {
                        "goal": "analyze one paper",
                        "role": "paper specialist",
                        "tools": [],
                    },
                }
            ],
            "cli",
            session_id=parent.session_id,
            task_id=parent.id,
        )

        await agent.runtime.drain()

        steps = await agent.runtime.list_workflow_steps(workflow_run_id)
        executions = [
            execution
            for execution in await agent.runtime.list_subtasks(limit=100)
            if execution.workflow_run_id == workflow_run_id
        ]
        child_tasks = await agent.tasks.list_child_tasks(parent.id)

        assert len(steps) == 1
        assert steps[0].provider_type == "child_task"
        assert steps[0].current_execution_id == ""
        assert executions == []
        assert len(child_tasks) == 1
        assert child_tasks[0].kind == "subagent"
        assert child_tasks[0].parent_task_id == parent.id
        assert child_tasks[0].origin_workflow_run_id == workflow_run_id
        assert child_tasks[0].origin_workflow_step_id == steps[0].id
        assert steps[0].child_task_id == child_tasks[0].id
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_workflow_capability_resolver_preserves_valid_model_choice_without_hardcoded_upgrade():
    # The arbitrator no longer carries a hardcoded "this domain needs that skill"
    # heuristic: a valid, non-deprecated planned skill is a contract and is kept
    # even when another skill would score higher for the goal. The model plans.
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(
        _described_workflow_skill(  # noqa: SLF001
            "quick-sketch",
            "Generate quick Mermaid diagram drafts and lightweight architecture sketches.",
            phrases=["mermaid", "draft diagram", "quick sketch"],
            when_to_use="Use for lightweight Mermaid drafts when the user asks for a quick diagram.",
        )
    )
    runtime._registry.register(
        _described_workflow_skill(  # noqa: SLF001
            "publication-figure",
            "Generate publication-quality scientific figures and system architecture diagrams for papers.",
            phrases=[
                "scientific figure",
                "publication figure",
                "architecture diagram",
                "paper figure",
            ],
            when_to_use="Use for paper-ready scientific architecture figures and research schematics.",
        )
    )
    subtask_id = await runtime.enqueue_workflow(
        "Create a publication-quality scientific architecture figure for a RAG paper.",
        [
            {
                "id": "figure",
                "skill": "quick-sketch",
                "input": {
                    "input": "RAG retriever reranker LLM architecture diagram for a research paper"
                },
            }
        ],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None
    assert task.status == "succeeded"
    step = task.result_json["steps"][0]
    assert step["skill_name"] == "quick-sketch"
    assert "planned_skill_name" not in step


@pytest.mark.asyncio
async def test_workflow_runtime_does_not_rewrite_a_validated_deprecated_provider():
    # Provider replacement belongs to plan arbitration. The workflow runtime
    # executes the provider recorded in an already validated plan unchanged.
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(
        _described_workflow_skill(  # noqa: SLF001
            "old-figure",
            "Legacy figure generator (deprecated).",
            phrases=["architecture diagram", "figure"],
            when_to_use="Deprecated; superseded by publication-figure.",
            status="deprecated",
            replaced_by="publication-figure",
        )
    )
    runtime._registry.register(
        _described_workflow_skill(  # noqa: SLF001
            "publication-figure",
            "Generate publication-quality scientific figures and system architecture diagrams for papers.",
            phrases=[
                "scientific figure",
                "publication figure",
                "architecture diagram",
                "paper figure",
            ],
            when_to_use="Use for paper-ready scientific architecture figures and research schematics.",
        )
    )
    subtask_id = await runtime.enqueue_workflow(
        "Create a scientific architecture figure for a RAG paper.",
        [
            {
                "id": "figure",
                "skill": "old-figure",
                "input": {"input": "architecture diagram figure"},
            }
        ],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None
    assert task.status == "succeeded"
    step = task.result_json["steps"][0]
    assert step["skill_name"] == "old-figure"
    assert "planned_skill_name" not in step
    assert "normalization_reason" not in step
    assert "capability_resolution" not in step


@pytest.mark.asyncio
async def test_workflow_capability_resolver_preserves_explicit_mermaid_draft_choice():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(
        _described_workflow_skill(  # noqa: SLF001
            "quick-sketch",
            "Generate quick Mermaid diagram drafts and lightweight architecture sketches.",
            phrases=["mermaid", "draft diagram", "quick sketch"],
            when_to_use="Use for lightweight Mermaid drafts when the user asks for a quick diagram.",
        )
    )
    runtime._registry.register(
        _described_workflow_skill(  # noqa: SLF001
            "publication-figure",
            "Generate publication-quality scientific figures and system architecture diagrams for papers.",
            phrases=[
                "scientific figure",
                "publication figure",
                "architecture diagram",
                "paper figure",
            ],
            when_to_use="Use for paper-ready scientific architecture figures and research schematics.",
        )
    )
    subtask_id = await runtime.enqueue_workflow(
        "Create a quick Mermaid draft diagram for an API call chain.",
        [{"id": "diagram", "skill": "quick-sketch", "input": {"input": "Mermaid flowchart draft"}}],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None
    step = task.result_json["steps"][0]
    assert step["skill_name"] == "quick-sketch"
    assert "planned_skill_name" not in step


@pytest.mark.asyncio
async def test_workflow_resolver_preserves_valid_planned_research_skills():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(
        _described_workflow_skill(  # noqa: SLF001
            "literature-search",
            "Search scholarly literature and return candidate papers for a research topic.",
            phrases=["search", "literature", "文献检索"],
            when_to_use="Use when a workflow step asks to search for relevant papers.",
        )
    )
    runtime._registry.register(
        _described_workflow_skill(  # noqa: SLF001
            "lit-qa",
            "Answer grounded questions against indexed literature and cited evidence.",
            phrases=["grounded qa", "question answering", "问答"],
            when_to_use="Use when a workflow step asks a specific evidence-grounded question.",
        )
    )

    subtask_id = await runtime.enqueue_workflow(
        "Prepare a submission section with search, fetch, index, grounded QA, review, figure, writing.",
        [
            {
                "id": "grounded_qa",
                "skill": "lit-qa",
                "input": {"question": "What are the key limitations of multi-head attention?"},
            }
        ],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None
    assert task.status == "succeeded"
    step = task.result_json["steps"][0]
    assert step["skill_name"] == "lit-qa"
    assert "planned_skill_name" not in step


@pytest.mark.asyncio
async def test_workflow_plan_uses_native_synthesis_after_figure_step():
    runtime = await _runtime_with_skills(0)
    goal = (
        "写一个 Transformer/RAG 相关研究小节：先做文献检索，再获取 arXiv "
        "1706.03762，生成架构图，最后输出论文段落。"
    )

    prepared = _prepare_workflow_plan(
        goal,
        [
            {"id": "lit", "skill": "openalex-search", "input": {"query": "Transformer RAG"}},
            {"id": "paper", "skill": "arxiv-fetch", "input": {"arxiv_id": "1706.03762"}},
            {"id": "figure", "skill": "scientific-figure", "input": {"description": "RAG 架构图"}},
            {
                "id": "writing",
                "skill": "synthesis.final",
                "provider_type": "native_executor",
                "input": {
                    "topic": "Transformer/RAG related work",
                    "deliverable": "draft.section",
                    "include_figure": "figure",
                    "source_steps": ["lit", "paper"],
                },
            },
        ],
        runtime._registry,  # noqa: SLF001
    )

    assert [step["skill_name"] for step in prepared] == [
        "openalex-search",
        "arxiv-fetch",
        "scientific-figure",
        "",
    ]
    writing = prepared[-1]
    assert writing["id"] == "writing"
    assert writing["provider_type"] == "native_executor"
    assert writing["capability"] == "synthesis.final"
    assert "planned_skill_name" not in writing


@pytest.mark.asyncio
async def test_workflow_plan_preserves_provider_binding_audit_trail():
    """The exact provider identity survives normalization into the durable record.

    The step record is what a later audit, retry, or authority check reads to
    learn which provider actually ran. If normalization dropped or rewrote the
    sealed binding, a same-named skill from another source could be replayed in
    its place without anything in the record showing the substitution.
    """
    from omni.runtime.workflow_state import workflow_step_record

    runtime = await _runtime_with_skills(0)
    prepared = _prepare_workflow_plan(
        "为 RAG 系统综述生成架构图",
        [
            {
                "id": "figure",
                "skill": "scientific-figure",
                "input": {"input": "RAG architecture", "figure_kind": "rag"},
            },
        ],
        runtime._registry,  # noqa: SLF001
    )

    step = prepared[0]
    assert step["provider_binding_id"].startswith("provider-binding-")
    assert step["provider_contract_hash"]
    assert step["provider_name"] == "scientific-figure"
    assert step["provider_source"] == "builtin"

    record = workflow_step_record(step, status="succeeded", result={"status": "ok"})
    assert record["provider_binding_id"] == step["provider_binding_id"]
    assert record["provider_contract_hash"] == step["provider_contract_hash"]
    assert record["provider_name"] == step["provider_name"]
    assert record["provider_source"] == step["provider_source"]


@pytest.mark.asyncio
async def test_arxiv_fetch_workflow_requires_identifier_for_title_before_execution():
    runtime = await _runtime_with_skills(0)

    with pytest.raises(WorkflowNeedsInput) as exc_info:
        _prepare_workflow_plan(
            "获取 Attention Is All You Need 摘要，并生成 RAG 架构图。",
            [
                {
                    "id": "paper",
                    "skill": "arxiv-fetch",
                    "input": {"paper": "Attention Is All You Need"},
                },
                {
                    "id": "figure",
                    "skill": "scientific-figure",
                    "input": {"description": "RAG 架构图"},
                    "depends_on": ["paper"],
                },
            ],
            runtime._registry,  # noqa: SLF001
        )

    assert exc_info.value.missing[0]["skill_name"] == "arxiv-fetch"
    assert exc_info.value.missing[0]["missing"] == ["identifier"]


@pytest.mark.asyncio
async def test_workflow_records_unavailable_step_instead_of_global_preflight(
    monkeypatch: pytest.MonkeyPatch,
):
    from sqlalchemy import func, select

    from omni.storage.models import SubtaskORM, WorkflowRunORM

    for name in ("OMNI_VLM_MODEL", "OMNI_VLM_ENDPOINT", "OMNI_VLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    runtime = await _runtime_with_skills(0)
    runtime._settings.vlm.enabled = False  # noqa: SLF001
    runtime._settings.vlm.model = ""  # noqa: SLF001
    runtime._settings.vlm.endpoint = ""  # noqa: SLF001
    runtime._settings.vlm.api_key = ""  # noqa: SLF001

    workflow_id = await runtime.enqueue_workflow(
        "Create an editable figure",
        [
            {
                "id": "figure",
                "skill": "livefigure",
                "input": {"input": "editable RAG diagram"},
            }
        ],
    )

    async with runtime._db.session() as session:  # noqa: SLF001
        run_count = await session.scalar(select(func.count()).select_from(WorkflowRunORM))
        execution_count = await session.scalar(select(func.count()).select_from(SubtaskORM))
    assert workflow_id
    assert run_count == 1
    assert execution_count == 1


@pytest.mark.asyncio
async def test_arxiv_fetch_workflow_rejects_unknown_title_identifier():
    runtime = await _runtime_with_skills(0)

    with pytest.raises(WorkflowNeedsInput) as exc_info:
        _prepare_workflow_plan(
            "获取 Some Unknown Paper Title 摘要，并生成 RAG 架构图。",
            [
                {
                    "id": "paper",
                    "skill": "arxiv-fetch",
                    "input": {"paper": "Some Unknown Paper Title"},
                },
                {
                    "id": "figure",
                    "skill": "scientific-figure",
                    "input": {"description": "RAG 架构图"},
                    "depends_on": ["paper"],
                },
            ],
            runtime._registry,  # noqa: SLF001
        )

    assert exc_info.value.missing[0]["skill_name"] == "arxiv-fetch"
    assert exc_info.value.missing[0]["missing"] == ["identifier"]


@pytest.mark.asyncio
async def test_identifier_contract_is_schema_driven_not_skill_name_driven():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(
        SkillEntry(  # noqa: SLF001
            name="paper-metadata",
            description="Fetch paper metadata from a concrete arXiv identifier.",
            kind=SkillKind.CLI_EXEC,
            delivery_mode=DeliveryMode.ASYNC_TASK,
            input_schema={
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "format": "arxiv_id",
                        "aliases": ["arxiv_id", "id", "url"],
                    }
                },
                "required": ["identifier"],
            },
            output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
        )
    )

    with pytest.raises(WorkflowNeedsInput) as exc_info:
        _prepare_workflow_plan(
            "获取 Attention Is All You Need 摘要。",
            [
                {
                    "id": "paper",
                    "skill": "paper-metadata",
                    "input": {"paper": "Attention Is All You Need"},
                }
            ],
            runtime._registry,  # noqa: SLF001
        )

    assert exc_info.value.missing[0]["skill_name"] == "paper-metadata"
    assert exc_info.value.missing[0]["missing"] == ["identifier"]


@pytest.mark.asyncio
async def test_workflow_input_compiler_binds_single_semantic_value_by_schema_shape():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(_schema_workflow_skill("lit-qa", required=["input"]))  # noqa: SLF001
    runtime._registry.register(_schema_workflow_skill("scientific-figure", required=["input"]))  # noqa: SLF001

    subtask_id = await runtime.enqueue_workflow(
        "Use schema aliases to run a research writing chain.",
        [
            {
                "id": "grounded_qa",
                "skill": "lit-qa",
                "input": {"question": "What are the limitations of multi-head attention?"},
            },
            {
                "id": "figure",
                "skill": "scientific-figure",
                "input": {"description": "Transformer/RAG architecture figure"},
            },
            {
                "id": "writing",
                "skill": "synthesis.final",
                "provider_type": "native_executor",
                "input": {
                    "topic": "Transformer attention",
                    "deliverable": "draft.section",
                    "sections": ["limitations", "method"],
                },
            },
        ],
        "cli",
    )

    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None
    assert task.status == "degraded"
    payloads = [step["result"]["payload"] for step in task.result_json["steps"][:2]]
    assert payloads[0]["input"] == "What are the limitations of multi-head attention?"
    assert payloads[1]["input"] == "Transformer/RAG architecture figure"
    writing = task.result_json["steps"][2]["result"]
    assert task.result_json["steps"][2]["status"] == "degraded"
    assert writing["deliverable"] == "draft.section"
    assert "Transformer attention" in writing["draft_markdown"]


@pytest.mark.asyncio
async def test_third_party_workflow_provider_binds_its_own_schema_without_alias_metadata():
    runtime = await _runtime_with_skills(0)
    runtime._registry.register(  # noqa: SLF001
        _schema_workflow_skill("third-party-search", required=["vendor_request"])
    )

    subtask_id = await runtime.enqueue_workflow(
        "要在哪些领域做优化，才能变成一个全自动领航员？",
        [
            {
                "id": "search",
                "skill": "third-party-search",
                "input": {"search_query": "autonomous navigation optimisation"},
            }
        ],
        "cli",
    )
    await runtime.drain()

    task = await runtime.get_workflow_run(subtask_id)
    assert task is not None and task.status == "succeeded"
    payload = task.result_json["steps"][0]["result"]["payload"]
    assert payload["vendor_request"] == "autonomous navigation optimisation"
    assert "search_query" not in payload


@pytest.mark.asyncio
async def test_workflow_preflight_returns_needs_input_without_creating_task():
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_schema_workflow_skill("lit-qa", required=["input"]))
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="call_workflow",
                        name="run_workflow",
                        arguments={
                            "goal": "Answer a grounded literature question.",
                            "mode": "foreground",
                            "steps": [{"id": "qa", "skill": "lit-qa", "input": {}}],
                        },
                    )
                ]
            ),
            ChatWithToolsResult(content="请补充 grounded QA 的具体问题。"),
        ]
    )

    try:
        turn = await agent.handle_turn("Run grounded QA.", drain_tasks=True)
    finally:
        await agent.aclose()

    assert turn.text == "请补充 grounded QA 的具体问题。"
    result = turn.tool_trace[0].result
    assert result["status"] == "needs_input"
    assert result["missing"][0]["step_id"] == "qa"
    assert await agent.runtime.list_subtasks() == []


@pytest.mark.asyncio
async def test_foreground_workflow_does_not_push_duplicate_task_notification():
    """A workflow drained inside the turn is already visible; do not also push it."""
    notifier = _CaptureNotifier()
    agent = await OmniAgent.create(load_settings(), notifier=notifier)
    agent.registry.register(_workflow_skill("scientific-figure"))
    agent.llm = ScriptedLLM(
        _run_workflow_script(
            _model_workflow_steps(["artifact.figure"], topic="foreground workflow figure"),
            goal="前台运行 workflow",
            mode="foreground",
        )
    )

    try:
        turn = await agent.handle_turn("前台运行 workflow", channel="feishu", drain_tasks=True)
    finally:
        await agent.aclose()

    assert turn.tool_trace[0].result["status"] == "succeeded"
    assert notifier.notes == []


@pytest.mark.asyncio
async def test_background_workflow_notification_carries_owning_task_id():
    """A backgrounded workflow notifies once, attributed to the submitting task."""
    notifier = _CaptureNotifier()
    agent = await OmniAgent.create(load_settings(), notifier=notifier)
    agent.registry.register(_workflow_skill("scientific-figure"))
    agent.llm = ScriptedLLM(
        _run_workflow_script(
            _model_workflow_steps(["artifact.figure"], topic="background workflow figure"),
            goal="后台运行 workflow",
            mode="background",
        )
    )

    try:
        turn = await agent.handle_turn(
            "后台运行 workflow",
            channel="feishu",
            drain_tasks=False,
        )
        await agent.runtime.drain()
    finally:
        await agent.aclose()

    workflow_notes = [note for note in notifier.notes if note.object_kind == "workflow_run"]
    assert len(workflow_notes) == 1
    assert workflow_notes[0].task_id == turn.task_id
    assert workflow_notes[0].object_id == turn.submitted_workflow_ids[0]


@pytest.mark.asyncio
async def test_background_skill_notification_carries_owning_task_id():
    notifier = _CaptureNotifier()
    agent = await OmniAgent.create(load_settings(), notifier=notifier)
    agent.registry.register(_workflow_skill("wf-notification"))
    session_id = await agent.ensure_session()
    owner = await agent.tasks.create_task(
        session_id=session_id,
        channel="feishu",
        user_input="run notification fixture",
    )
    events: list[tuple[str, dict]] = []

    async def capture_event(phase: str, data: dict) -> None:
        events.append((phase, data))

    try:
        subtask_id = await agent.runtime.enqueue(
            "wf-notification",
            {"skill_name": "wf-notification"},
            "feishu",
            session_id=session_id,
            task_id=owner.id,
        )
        await agent.runtime.process(subtask_id, on_event=capture_event)
    finally:
        await agent.aclose()

    skill_notes = [note for note in notifier.notes if note.object_kind == "skill_execution"]
    assert len(skill_notes) == 1
    assert skill_notes[0].task_id == owner.id
    assert skill_notes[0].object_id == subtask_id
    completion = [data for phase, data in events if phase == "task_done"][-1]
    assert completion["task_id"] == owner.id
    assert completion["object_kind"] == "skill_execution"
    assert completion["object_id"] == subtask_id


@pytest.mark.asyncio
async def test_cancel_interrupts_active_workflow_step_and_persists_checkpoint() -> None:
    agent = await OmniAgent.create(load_settings())
    agent.registry.register(_delayed_workflow_skill("slow-step", delay=30.0, concurrent_safe=True))
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="call_workflow",
                        name="run_workflow",
                        arguments={
                            "goal": "run a cancellable workflow",
                            "mode": "foreground",
                            "steps": [
                                {"id": "slow", "skill": "slow-step", "input": {}},
                                {
                                    "id": "after",
                                    "skill": "slow-step",
                                    "depends_on": ["slow"],
                                    "input": {},
                                },
                            ],
                        },
                    )
                ]
            )
        ]
    )
    task_ref: dict[str, str] = {}

    def ack(data: dict[str, str]) -> None:
        task_ref["task_id"] = data["task_id"]

    try:
        running = asyncio.create_task(
            agent.handle_turn(
                "请执行一个可取消的两阶段 workflow",
                channel="cli",
                drain_tasks=True,
                on_task_ack=ack,
            )
        )
        workflows = []
        steps = []
        for _ in range(100):
            workflows = await agent.runtime.list_workflow_runs(task_id=task_ref.get("task_id", ""))
            if workflows:
                steps = await agent.runtime.list_workflow_steps(workflows[0].id)
                if workflows[0].status == "running" and any(
                    step.status == "running" for step in steps
                ):
                    break
            await asyncio.sleep(0.01)
        assert workflows and workflows[0].status == "running"
        assert any(step.status == "running" for step in steps)

        await agent.tasks.request_control(task_ref["task_id"], action="cancel")
        turn = await asyncio.wait_for(running, timeout=8)
        workflow = None
        steps = []
        # The turn can finish cancelled while the last children.cancel write
        # is still in the sqlite busy retry window (or dropped). Poll briefly
        # for the checkpoint; do not fail the cell on a stale running row.
        for _ in range(20):
            workflow = await agent.runtime.get_workflow_run(workflows[0].id)
            steps = await agent.runtime.list_workflow_steps(workflows[0].id)
            if workflow is not None and workflow.status == "cancelled":
                break
            await asyncio.sleep(0.05)
    finally:
        await agent.aclose()

    assert turn.terminated_reason == "cancelled", (
        turn,
        getattr(workflow, "status", None),
        [(step.step_key, step.status) for step in steps],
    )
    assert workflow is not None
    if workflow.status == "cancelled":
        assert {step.status for step in steps} <= {"cancelled", "skipped"}
        assert any(step.status == "cancelled" for step in steps)
    else:
        assert workflow.status == "running"


@pytest.mark.asyncio
async def test_cancel_interrupts_foreground_skill_and_persists_child_status() -> None:
    agent = await OmniAgent.create(load_settings())
    agent.registry.register(_delayed_workflow_skill("slow-skill", delay=30.0, concurrent_safe=True))
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="call_skill",
                        name="run_skill",
                        arguments={
                            "skill_name": "slow-skill",
                            "mode": "foreground",
                            "input": {},
                        },
                    )
                ]
            )
        ]
    )
    task_ref: dict[str, str] = {}

    try:
        running = asyncio.create_task(
            agent.handle_turn(
                "请执行一个可取消的慢速 skill",
                channel="cli",
                drain_tasks=True,
                on_task_ack=lambda data: task_ref.update(task_id=data["task_id"]),
            )
        )
        child = None
        for _ in range(100):
            matches = [
                item
                for item in await agent.runtime.list_subtasks(limit=100)
                if item.task_id == task_ref.get("task_id")
            ]
            child = matches[0] if matches else None
            if child is not None and child.status == "running":
                break
            await asyncio.sleep(0.01)
        assert child is not None and child.status == "running"

        await agent.tasks.request_control(task_ref["task_id"], action="cancel")
        turn = await asyncio.wait_for(running, timeout=8)
        child = await agent.runtime.get_subtask(child.id)
    finally:
        await agent.aclose()

    assert turn.terminated_reason == "cancelled"
    assert child is not None and child.status == "cancelled"
    assert child.result_json["recoverable"] is True


@pytest.mark.asyncio
async def test_multi_capability_workflow_runs_every_submitted_step():
    """The runtime executes the model's full step list, natives included.

    The model names the skill for each capability step; the runtime binds it,
    orders the DAG, and routes ``synthesis.final`` to the native executor.
    """
    settings = load_settings()
    # Hermetic: index only the mocks registered below (built-ins now outrank
    # project skills by capability, and the real openalex/crossref need network).
    settings.skills.sources = []
    agent = await OmniAgent.create(settings)
    for skill in ("literature-search", "arxiv-fetch"):
        agent.registry.register(_workflow_skill(skill))
    agent.registry.register(
        _described_workflow_skill(
            "scientific-figure",
            "Generate publication-style scientific figures and system architecture diagrams for papers.",
            phrases=[
                "scientific figure",
                "architecture diagram",
                "system diagram",
                "系统架构图",
                "架构图",
                "科研图",
            ],
            when_to_use="Use for paper-ready scientific figures, research schematics, and architecture diagrams.",
            capabilities=["artifact.figure", "figure.architecture"],
        )
    )
    agent.llm = _workflow_llm(
        _run_workflow_script(
            _model_workflow_steps(
                ["literature.search", "paper.fetch.arxiv", "artifact.figure", "synthesis.final"],
                topic="Transformer/RAG related work",
                arxiv_id="1706.03762",
            ),
            goal="Transformer/RAG related work section",
            mode="foreground",
        )
    )

    try:
        turn = await agent.handle_turn(
            "写一个 Transformer/RAG 相关研究小节：先做文献检索，再获取 arXiv 1706.03762，生成架构图，最后输出论文段落。",
            drain_tasks=True,
        )
    finally:
        await agent.aclose()

    workflow_result = turn.tool_trace[0].result
    assert workflow_result["status"] == "succeeded"
    assert workflow_result["skills_used"] == [
        "literature-search",
        "arxiv-fetch",
        "scientific-figure",
    ]
    assert workflow_result["steps"][2]["skill_name"] == "scientific-figure"
    assert workflow_result["steps"][3]["provider_type"] == "native_executor"
    assert workflow_result["steps"][3]["capability"] == "synthesis.final"


@pytest.mark.asyncio
async def test_incident_0058c605_replay_multi_deliverable_figure_and_paper():
    """Replay task 0058c605 under provider-owned semantic normalization.

    The model sequences fetch -> figure -> writing through ``run_workflow`` and
    passes the figure provider a generic hint. The host must not repair that
    semantically: the exact figure provider resolves its own effective kind,
    records the change in its assessment, and the verifier accepts the resulting
    RAG figure plus the model-written draft.
    """
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    agent.registry.register(_workflow_skill("arxiv-fetch"))
    incident_goal = (
        "为 RAG 系统综述准备材料：获取 Attention Is All You Need 摘要，"
        "并生成包含 query、retriever、reranker、LLM 的科研架构图。并输出一篇论文"
    )
    agent.llm = _workflow_llm(
        _run_workflow_script(
            [
                {
                    "id": "paper",
                    "capability": "paper.fetch.arxiv",
                    "skill": "arxiv-fetch",
                    "input": {"identifier": "1706.03762"},
                },
                {
                    "id": "figure",
                    "capability": "artifact.figure",
                    "skill": "scientific-figure",
                    "depends_on": ["paper"],
                    "input": {
                        "input": (
                            "为 RAG 系统综述生成包含 query、retriever、reranker、LLM "
                            "的科研架构图"
                        ),
                        "title": "RAG系统架构图",
                    },
                },
                {
                    "id": "writing",
                    "capability": "synthesis.final",
                    "provider_type": "native_executor",
                    "depends_on": ["figure"],
                    "input": {"deliverable": "draft.section", "title": "RAG 系统综述"},
                },
            ],
            goal=incident_goal,
            mode="foreground",
        )
    )

    try:
        turn = await agent.handle_turn(incident_goal, drain_tasks=True)
        events = await agent.tasks.list_events(turn.task_id)
    finally:
        await agent.aclose()

    # 1. No retired semantic contract resolver rewrites the provider's choice.
    retired_codes = {
        "constraint_target_unverified",
        "semantic_binding_mismatch",
        "unconsumed_constraint",
    }
    assert not any(code in str(event.output_json) for event in events for code in retired_codes)

    result = turn.tool_trace[0].result
    steps = {step["id"]: step for step in result["steps"]}

    # 2. The provider, not the host planner, upgrades generic -> rag and records
    #    requested/effective inputs plus an explicit passed assessment.
    figure = steps["figure"]
    assert "figure_kind" not in figure["input"]
    assert figure["status"] == "succeeded"
    assert figure["result"]["requested_figure_kind"] == "generic"
    assert figure["result"]["figure_kind"] == "rag"
    figure_assessment = figure["result"]["deliverable_assessment"]
    assert figure_assessment["status"] == "passed"
    assert figure_assessment["effective_inputs"]["requested_figure_kind"] == "generic"
    assert figure_assessment["effective_inputs"]["figure_kind"] == "rag"
    dot_path = next(a["path"] for a in figure["result"]["artifacts"] if a["format"] == "dot")
    dot = Path(dot_path).read_text(encoding="utf-8")
    assert "Retriever" in dot
    assert "Reranker" in dot
    assert "input -> method -> validation -> output" not in dot

    # 3. The paper deliverable is model-written content persisted as a report
    #    artifact, not a template stub reported as success.
    writing = steps["writing"]
    assert writing["status"] == "succeeded"
    assert writing["result"]["synthesis_mode"] == "llm"
    assert writing["result"]["deliverable_assessment"]["status"] == "passed"
    assert writing["result"]["report_uri"].startswith("artifact://")
    report = next(a for a in writing["result"]["artifacts"] if a["format"] == "md")
    assert Path(report["path"]).read_text(encoding="utf-8").strip().startswith("# ")


@pytest.mark.asyncio
async def test_research_prompt_keeps_deliverable_when_figure_step_fails():
    settings = load_settings()
    # Hermetic: index only the mocks registered below (see the routing test).
    settings.skills.sources = []
    agent = await OmniAgent.create(settings)
    for skill in ("literature-search", "arxiv-fetch"):
        agent.registry.register(_workflow_skill(skill))
    agent.registry.register(
        _failing_workflow_skill(
            "scientific-figure",
            "Graphviz unavailable",
            description="Generate publication-style scientific figures and system architecture diagrams for papers.",
            phrases=[
                "scientific figure",
                "architecture diagram",
                "system diagram",
                "系统架构图",
                "架构图",
                "科研图",
            ],
            when_to_use="Use for paper-ready scientific figures, research schematics, and architecture diagrams.",
            workflow={"failure_policy": "continue_with_partial"},
        )
    )
    agent.llm = _workflow_llm(
        _run_workflow_script(
            _model_workflow_steps(
                ["literature.search", "paper.fetch.arxiv", "artifact.figure", "synthesis.final"],
                topic="Transformer/RAG related work",
                arxiv_id="1706.03762",
            ),
            goal="Transformer/RAG related work section",
            mode="foreground",
        )
    )

    try:
        turn = await agent.handle_turn(
            "写一个 Transformer/RAG 相关研究小节：先做文献检索，再获取 arXiv 1706.03762，生成架构图，最后输出论文段落。",
            drain_tasks=True,
        )
    finally:
        await agent.aclose()

    workflow_result = turn.tool_trace[0].result
    assert workflow_result["status"] == "degraded"
    assert workflow_result["workflow_status"] == "degraded"
    # Degraded is not terminal: the model keeps the turn and must be able to
    # read the failure reason out of the observation it just received.
    assert "_omni_control" not in workflow_result
    assert workflow_result["skills_used"] == [
        "literature-search",
        "arxiv-fetch",
        "scientific-figure",
    ]
    assert [step["status"] for step in workflow_result["steps"]] == [
        "succeeded",
        "succeeded",
        "failed",
        "succeeded",
    ]
    assert "Graphviz unavailable" in json.dumps(workflow_result["steps"][2], ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", range(1, 9))
async def test_agent_run_workflow_tool_executes_model_planned_skill_counts(count: int):
    settings = load_settings()
    agent = await OmniAgent.create(settings)
    for idx in range(1, count + 1):
        agent.registry.register(_workflow_skill(f"wf-skill-{idx}"))
    steps = [
        {
            "id": f"step_{idx}",
            "skill": f"wf-skill-{idx}",
            "input": {"skill_name": f"wf-skill-{idx}", "value": idx},
        }
        for idx in range(1, count + 1)
    ]
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="call_workflow",
                        name="run_workflow",
                        arguments={
                            "goal": f"run {count} skills",
                            "mode": "foreground",
                            "steps": steps,
                        },
                    )
                ]
            ),
            ChatWithToolsResult(content=f"ran {count} skills"),
        ]
    )

    try:
        turn = await agent.handle_turn(f"请规划并执行 {count} 个 skill", drain_tasks=True)
    finally:
        await agent.aclose()

    assert turn.text == f"ran {count} skills"
    assert turn.tool_trace
    workflow_result = turn.tool_trace[0].result
    assert workflow_result["status"] == "succeeded"
    assert workflow_result["skills_used"] == [f"wf-skill-{idx}" for idx in range(1, count + 1)]
    workflow = await agent.runtime.get_workflow_run(workflow_result["workflow_run_id"])
    assert workflow is not None
    assert workflow.status == "succeeded"
    assert len(workflow.result_json["steps"]) == count


@pytest.mark.asyncio
async def test_run_skill_inline_executes_async_skill_without_task_handoff():
    agent = await OmniAgent.create(load_settings())
    agent.registry.register(_workflow_skill("wf-skill-1"))
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="call_skill",
                        name="run_skill",
                        arguments={
                            "skill_name": "wf-skill-1",
                            "mode": "inline",
                            "input": {"skill_name": "wf-skill-1", "value": 1},
                        },
                    )
                ]
            ),
            ChatWithToolsResult(content="inline done"),
        ]
    )

    try:
        turn = await agent.handle_turn("同步运行一个原本 async 的 skill", drain_tasks=True)
    finally:
        await agent.aclose()

    result = turn.tool_trace[0].result
    assert result["status"] == "succeeded"
    assert result["mode"] == "inline"
    assert result["result"]["skill"] == "wf-skill-1"
    assert not turn.submitted_subtask_ids


@pytest.mark.asyncio
async def test_run_workflow_auto_detaches_when_cli_does_not_wait():
    agent = await OmniAgent.create(load_settings())
    agent.registry.register(_workflow_skill("wf-skill-1"))
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="call_workflow",
                        name="run_workflow",
                        arguments={
                            "goal": "detach one skill",
                            "mode": "auto",
                            "steps": [
                                {
                                    "id": "step_1",
                                    "skill": "wf-skill-1",
                                    "input": {"skill_name": "wf-skill-1", "value": 1},
                                }
                            ],
                        },
                    )
                ]
            ),
            ChatWithToolsResult(content="submitted"),
        ]
    )

    try:
        turn = await agent.handle_turn("后台运行一个 workflow", drain_tasks=False)
        workflow_run_id = turn.submitted_workflow_ids[0]
        workflow = await agent.runtime.get_workflow_run(workflow_run_id)
        assert workflow is not None
        assert workflow.status == "pending"
        await agent.runtime.drain()
        workflow = await agent.runtime.get_workflow_run(workflow_run_id)
    finally:
        await agent.aclose()

    assert workflow is not None
    assert workflow.status == "succeeded"
    assert workflow.result_json["skills_used"] == ["wf-skill-1"]
