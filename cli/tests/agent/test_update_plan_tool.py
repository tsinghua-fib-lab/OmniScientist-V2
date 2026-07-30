"""``update_plan``: the model owns the checklist, the host only renders it.

Codex parity. The plan is a tool the model calls mid-turn, not a contract the
host computes before the first tool call — so these tests pin two things: the
handler stays inert and forgiving, and the CLI renders a call as the plan
checklist rather than as one more tool line.
"""

from __future__ import annotations

import pytest

from omni.cli.live_display import TurnDisplay
from omni.cli.render import console
from omni.config import load_settings
from omni.skills_runtime.builtin_tools import build_builtin_tools
from omni.skills_runtime.builtin_tools.plan import build_plan_tools, normalize_plan
from omni.skills_runtime.context import ExecContext


def _ctx() -> ExecContext:
    settings = load_settings()
    return ExecContext(settings=settings, paths=settings.paths)


def _tool():  # noqa: ANN202 - test helper
    return build_plan_tools(_ctx())[0]


def _capture(display: TurnDisplay, events: list[tuple[str, dict]]) -> str:
    with console.capture() as cap:
        for phase, data in events:
            display.tool_event(phase, data)
        display.end()
    return cap.get()


def test_update_plan_is_offered_on_every_surface_including_db_free():
    # No store, no registry: the checklist must still be available (a headless
    # or IM turn plans too).
    names = {t.spec.name for t in build_builtin_tools(_ctx())}
    assert "update_plan" in names


@pytest.mark.asyncio
async def test_handler_returns_the_normalized_checklist_and_a_bare_ack():
    out = await _tool().handler(
        {
            "explanation": "surveying first",
            "plan": [
                {"step": "read the funnel", "status": "completed"},
                {"step": "add the fallback", "status": "in_progress"},
                {"step": "write tests", "status": "pending"},
            ],
        }
    )
    assert out["status"] == "ok"
    assert [s["status"] for s in out["plan"]] == ["completed", "in_progress", "pending"]
    # The checklist is already in the model's own message; echoing it back would
    # only spend context, so the acknowledgement stays bare (Codex behaviour).
    assert out["note"] == "Plan updated."


@pytest.mark.asyncio
async def test_an_unknown_status_degrades_to_pending_rather_than_failing_the_call():
    out = await _tool().handler({"plan": [{"step": "do it", "status": "wip"}]})
    assert out["status"] == "ok"
    assert out["plan"] == [{"step": "do it", "status": "pending"}]


@pytest.mark.asyncio
async def test_an_empty_plan_is_the_one_rejected_case():
    out = await _tool().handler({"plan": []})
    assert out["status"] == "error"


def test_normalize_accepts_bare_strings_and_drops_blanks():
    assert normalize_plan(["  survey  ", "", {"step": "  ", "status": "pending"}]) == [
        {"step": "survey", "status": "pending"}
    ]


def test_a_plan_call_renders_the_checklist_not_another_tool_line():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            ("start", {"name": "update_plan", "arguments": {"plan": []}}),
            (
                "done",
                {
                    "name": "update_plan",
                    "result": {
                        "status": "ok",
                        "plan": [
                            {"step": "read the funnel", "status": "completed"},
                            {"step": "add the fallback", "status": "in_progress"},
                            {"step": "write tests", "status": "pending"},
                        ],
                    },
                },
            ),
        ],
    )
    assert "read the funnel" in out
    assert "add the fallback" in out
    assert "3 step(s)" in out
    # Glyphs carry the state; a generic "✓ update_plan" line would be noise.
    assert "✔" in out and "▸" in out and "☐" in out
    assert "✓ update_plan" not in out


def test_a_later_call_replaces_the_checklist_wholesale():
    display = TurnDisplay(verbosity="normal", status_line=False)
    _capture(
        display,
        [("done", {"name": "update_plan",
                   "result": {"status": "ok", "plan": [{"step": "old step", "status": "pending"}]}})],
    )
    out = _capture(
        display,
        [("done", {"name": "update_plan",
                   "result": {"status": "ok", "plan": [{"step": "new step", "status": "completed"}]}})],
    )
    # The model rewrote the plan; the previous list is not merged into it.
    assert "new step" in out
    assert "1 step(s)" in out


def test_a_failed_plan_call_still_reports_as_an_ordinary_tool_error():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [("done", {"name": "update_plan", "error": "plan must contain at least one step"})],
    )
    assert "✗" in out
    assert "update_plan" in out
