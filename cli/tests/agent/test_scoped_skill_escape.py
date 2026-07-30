"""``$<scope>:<name>`` explicit-source escape, end to end.

Built-ins hard-override a same-named user/external skill. The only way to reach
a shadowed skill is the escape ``$user:<name>`` / ``$<source>:<name>``. These
tests pin every hop of that ripple: parse (boundary) → select (planner) →
resolve (registry) → execute (subtask runtime).
"""

from __future__ import annotations

import sys

import pytest

from omni.agent.boundary_router import explicit_skill_ref
from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.plan_validator import PlanValidator
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.runtime.subtask_runtime import SubtaskRuntime
from omni.skills_runtime.context import SKILL_SOURCE_PARAM, ExecContext
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry, resolve_step_entry
from omni.storage.db import get_database


def _dup(name: str, *, source: str, which: str) -> SkillEntry:
    """A runnable same-named cli_exec skill that reports which source ran."""
    script = (
        "import json,sys;json.load(sys.stdin);"
        f"print(json.dumps({{'status':'ok','summary':'ran '+{which!r},'which':{which!r}}}))"
    )
    return SkillEntry(
        name=name,
        description=f"escape fixture ({which})",
        source=source,
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}, "which": {"type": "string"}},
        },
    )


def _shadowed_registry() -> SkillRegistry:
    """A registry where ``dup`` exists as both a built-in (winner) and a user
    skill (shadowed). Registering the user copy first, then the built-in, makes
    the built-in the automatic winner while both stay reachable by source."""
    reg = SkillRegistry(load_settings(), sources=())
    reg.register(_dup("dup", source="user_omni", which="user"))
    reg.register(_dup("dup", source="builtin", which="builtin"))
    return reg


# ── registry resolution ─────────────────────────────────────────────────────
def test_resolve_explicit_bare_vs_scoped():
    reg = _shadowed_registry()

    assert reg.get("dup").source == "builtin"  # winner
    assert reg.resolve_explicit("dup").source == "builtin"  # bare → winner
    assert reg.resolve_explicit("user:dup").source == "user_omni"  # escape → shadowed
    assert reg.resolve_explicit("builtin:dup").source == "builtin"
    assert reg.resolve_explicit("user_omni:dup").source == "user_omni"  # exact source name
    # A colon that is not a recognised scope is treated as part of a bare name.
    assert reg.resolve_explicit("bogus:dup") is None


def test_resolve_ref_prefers_source_then_winner():
    reg = _shadowed_registry()
    assert reg.resolve_ref("dup", "user_omni").source == "user_omni"
    assert reg.resolve_ref("dup", "").source == "builtin"
    # A persisted explicit source is authority, not a hint to fall back.
    assert reg.resolve_ref("dup", "user_claude") is None


def test_workflow_step_source_resolution_never_falls_back_to_winner():
    reg = _shadowed_registry()

    assert (
        resolve_step_entry(
            reg,
            {"skill_name": "dup", "skill_source": "user_omni"},
        ).source
        == "user_omni"
    )
    assert (
        resolve_step_entry(
            reg,
            {
                "skill_name": "dup",
                "input": {SKILL_SOURCE_PARAM: "user_omni"},
            },
        ).source
        == "user_omni"
    )
    assert (
        resolve_step_entry(
            reg,
            {"skill_name": "dup", "skill_source": "user_claude"},
        )
        is None
    )


def test_plan_validation_promotes_legacy_input_source_to_step_authority():
    reg = _shadowed_registry()
    plan = IntentPlan(
        task_id="legacy-source",
        user_message="run the forced provider",
        intent_type=IntentType.WORKFLOW,
        outputs=["answer"],
        workflow_steps=[
            {
                "id": "forced",
                "skill_name": "dup",
                "capability": "fixture.run",
                "input": {
                    "input": "x",
                    SKILL_SOURCE_PARAM: "user_omni",
                },
            }
        ],
    )

    PlanValidator(reg).validate(plan)

    assert plan.workflow_steps[0]["skill_source"] == "user_omni"
    assert SKILL_SOURCE_PARAM not in plan.workflow_steps[0]["input"]


# ── boundary parsing ────────────────────────────────────────────────────────
def test_explicit_skill_ref_reports_forced_source():
    reg = _shadowed_registry()
    assert explicit_skill_ref("please $user:dup now", reg) == ("dup", "user_omni")
    assert explicit_skill_ref("run $builtin:dup", reg) == ("dup", "builtin")
    # Bare $name resolves to the winner with no forced source.
    assert explicit_skill_ref("just $dup please", reg) == ("dup", "")
    assert explicit_skill_ref("nothing explicit here", reg) == ("", "")


# ── planner ripple ──────────────────────────────────────────────────────────
def test_boundary_plan_threads_skill_source():
    reg = _shadowed_registry()
    planner = IntentPlanner(reg)

    forced = planner.boundary_plan("$user:dup do the thing", task_id="run-forced")
    assert forced is not None
    assert forced.intent_type == IntentType.SINGLE_SKILL_TASK
    assert forced.selected_skills[0].skill == "dup"
    assert forced.selected_skills[0].skill_source == "user_omni"

    bare = planner.boundary_plan("$dup do the thing", task_id="run-bare")
    assert bare is not None
    assert bare.selected_skills[0].skill == "dup"
    assert bare.selected_skills[0].skill_source == ""


# ── execution ripple (worker resolves the forced source, strips control key) ──
async def _runtime() -> SubtaskRuntime:
    settings = load_settings()
    settings.skills.sources = []  # hermetic: only the dup fixtures below
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    reg = SkillRegistry(settings, sources=())
    reg.register(_dup("dup", source="user_omni", which="user"))
    reg.register(_dup("dup", source="builtin", which="builtin"))

    def ctx_factory(session_id: str, channel: str) -> ExecContext:
        return ExecContext(settings=settings, paths=settings.paths, session_id=session_id, channel=channel)

    return SubtaskRuntime(db, settings, reg, ctx_factory)


@pytest.mark.asyncio
async def test_worker_executes_forced_source_and_strips_control_key():
    runtime = await _runtime()

    forced = await runtime.enqueue(
        "dup", {"input": "x", SKILL_SOURCE_PARAM: "user_omni"}, "", session_id="s1"
    )
    await runtime.process(forced)
    task = await runtime.get_subtask(forced)
    assert task.status == "succeeded"
    assert task.result_json["which"] == "user"  # shadowed skill actually ran

    bare = await runtime.enqueue("dup", {"input": "x"}, "", session_id="s1")
    await runtime.process(bare)
    task_bare = await runtime.get_subtask(bare)
    assert task_bare.status == "succeeded"
    assert task_bare.result_json["which"] == "builtin"  # winner ran


@pytest.mark.asyncio
async def test_worker_does_not_fallback_when_forced_source_disappeared():
    runtime = await _runtime()

    forced = await runtime.enqueue(
        "dup",
        {"input": "x", SKILL_SOURCE_PARAM: "user_claude"},
        "",
        session_id="s1",
    )
    await runtime.process(forced)
    task = await runtime.get_subtask(forced)

    assert task.status == "failed"
    assert "unknown skill" in task.error.lower()
    assert not task.result_json or "which" not in task.result_json
