"""A declared skill budget must be spendable, and a quiet skill must say so.

The regression these lock down: ``execution.max_seconds`` was read and then
clamped by the very fallback it was meant to override, because one setting was
passed as both the default and the ceiling. A skill could only ever shorten its
own run, never lengthen it, and nothing said so out loud.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omni.config.settings import load_settings
from omni.skills_runtime.executor import (
    SkillBudget,
    _await_skill_call,
    _ProgressHeartbeat,
    _skill_budget,
    _SkillStalled,
)
from omni.skills_runtime.manifest import execution_budget_warnings

_SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"
_KNOB = "skills.max_python_seconds"


def _entry(**execution: float) -> SimpleNamespace:
    return SimpleNamespace(name="research-ideation", execution=dict(execution))


def _budget(entry: SimpleNamespace, ctx: object = None, **kw: float) -> SkillBudget:
    limits: dict = {"default": 600.0, "ceiling": 1800.0, "knob": _KNOB}
    limits.update(kw)
    return _skill_budget(entry, ctx or SimpleNamespace(), **limits)


def test_a_declared_budget_above_the_fallback_is_actually_granted():
    assert _budget(_entry(max_seconds=1800)).seconds == 1800


def test_a_skill_that_declares_nothing_keeps_the_conservative_fallback():
    assert _budget(_entry()).seconds == 600


def test_a_declaration_beyond_the_ceiling_is_clamped_and_said_out_loud(caplog):
    with caplog.at_level(logging.WARNING):
        budget = _budget(_entry(max_seconds=7200))

    assert budget.seconds == 1800
    assert "7200" in caplog.text, "the warning should quote what the manifest asked for"
    assert _KNOB in caplog.text, "and name the ceiling that refused it"


def test_the_timeout_names_the_ceiling_when_the_ceiling_bound_the_run():
    message = str(_budget(_entry(max_seconds=7200)).timeout_error("research-ideation"))

    assert f"`{_KNOB}`" in message
    assert "/config set" in message


def test_the_timeout_points_at_the_manifest_when_the_manifest_bound_the_run():
    message = str(_budget(_entry(max_seconds=900)).timeout_error("research-ideation"))

    assert "execution.max_seconds" in message
    assert "/config set" not in message, "the owner's ceiling was not the constraint"


def test_the_timeout_blames_the_envelope_when_the_envelope_ran_out_first():
    ctx = SimpleNamespace(execution_deadline=time.monotonic() + 5)

    budget = _budget(_entry(max_seconds=1800), ctx)

    assert budget.seconds <= 5
    assert "workflow envelope" in budget.remedy
    assert "tasks.workflow_max_seconds" in budget.remedy


def test_an_undeclared_skill_is_never_told_to_raise_a_budget_it_does_not_have():
    message = str(_budget(_entry()).timeout_error("research-ideation"))

    assert "declares no budget of its own" in message
    assert "600s" in message


@pytest.mark.asyncio
async def test_a_quiet_skill_is_reported_as_stuck_rather_than_merely_slow():
    heartbeat = _ProgressHeartbeat()
    heartbeat.wrap(lambda *_a, **_kw: None)

    async def _never_reports() -> dict:
        await asyncio.sleep(5)
        return {}

    started = time.monotonic()
    with pytest.raises(_SkillStalled):
        await _await_skill_call(
            _never_reports(), SkillBudget(5.0, "r", stall_seconds=0.05), heartbeat
        )

    assert time.monotonic() - started < 2, "the watchdog must not wait for the deadline"


@pytest.mark.asyncio
async def test_a_skill_that_keeps_reporting_progress_is_left_to_finish():
    heartbeat = _ProgressHeartbeat()
    report = heartbeat.wrap(lambda *_a, **_kw: None)

    async def _busy() -> dict:
        for _ in range(10):
            await asyncio.sleep(0.02)
            report("working", 0.5)
        return {"status": "ok"}

    result = await _await_skill_call(
        _busy(), SkillBudget(5.0, "r", stall_seconds=0.1), heartbeat
    )

    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_the_wall_clock_still_backstops_a_skill_that_reports_progress_forever():
    heartbeat = _ProgressHeartbeat()
    report = heartbeat.wrap(lambda *_a, **_kw: None)

    async def _forever() -> dict:
        while True:
            await asyncio.sleep(0.01)
            report("still going", 0.5)

    with pytest.raises(TimeoutError):
        await _await_skill_call(
            _forever(), SkillBudget(0.15, "r", stall_seconds=5.0), heartbeat
        )


@pytest.mark.asyncio
async def test_an_engine_with_no_progress_callback_keeps_the_plain_wall_clock():
    """A silent engine cannot feed a watchdog, so it must not inherit one."""
    heartbeat = _ProgressHeartbeat()

    assert heartbeat.wrap(None) is None
    assert not heartbeat.armed

    async def _slow_but_working() -> dict:
        await asyncio.sleep(0.2)
        return {"status": "ok"}

    result = await _await_skill_call(
        _slow_but_working(), SkillBudget(5.0, "r", stall_seconds=0.01), heartbeat
    )

    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_a_skill_that_raises_reports_its_own_error_not_a_timeout():
    heartbeat = _ProgressHeartbeat()
    heartbeat.wrap(lambda *_a, **_kw: None)

    async def _boom() -> dict:
        raise ValueError("engine said no")

    with pytest.raises(ValueError, match="engine said no"):
        await _await_skill_call(
            _boom(), SkillBudget(5.0, "r", stall_seconds=1.0), heartbeat
        )


def test_a_stall_window_wider_than_the_deadline_is_an_authoring_defect():
    warnings = execution_budget_warnings(
        {"max_seconds": 300, "stall_seconds": 600}, "research-ideation"
    )

    assert warnings
    assert "stall_seconds" in warnings[0]


def test_a_stall_window_inside_the_deadline_is_accepted():
    assert execution_budget_warnings({"max_seconds": 1800, "stall_seconds": 600}) == []


@pytest.mark.parametrize(
    "skill_dir",
    sorted(p for p in _SKILLS_ROOT.glob("*/SKILL.md")),
    ids=lambda p: p.parent.name,
)
def test_no_builtin_skill_declares_a_budget_its_ceiling_would_refuse(skill_dir):
    frontmatter = yaml.safe_load(skill_dir.read_text(encoding="utf-8").split("---")[1])
    helix = ((frontmatter or {}).get("metadata") or {}).get("helixforge") or {}
    declared = float((helix.get("execution") or {}).get("max_seconds") or 0)
    if not declared:
        return
    skills = load_settings().skills
    ceilings = {
        "python_engine": skills.max_python_seconds,
        "cli_exec": skills.max_cli_seconds,
    }

    ceiling = ceilings.get(str(helix.get("kind")), skills.max_prompt_seconds)

    assert declared <= ceiling, (
        f"{skill_dir.parent.name} asks for {declared:g}s but its kind is capped at "
        f"{ceiling:g}s, so the declaration would be silently trimmed"
    )
