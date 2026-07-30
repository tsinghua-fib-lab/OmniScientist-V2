"""Normal output hides successful internal plan-repair machinery."""

from __future__ import annotations

from omni.cli.live_display import TurnDisplay
from omni.cli.render import console


def _render(verbosity: str) -> str:
    display = TurnDisplay(verbosity=verbosity, status_line=False)
    event = {
        "event_type": "plan.recovery",
        "name": "execute",
        "summary": "Bounded repair corrected provider_schema_invalid",
        "payload": {
            "action": "execute",
            "rung": "objective_schema_repair",
            "notes": ["provider_schema_invalid: cn -> zh"],
            "finding_state": "resolved",
        },
    }
    with console.capture() as captured:
        display.tool_event("plan", event)
    return captured.get()


def test_normal_display_hides_resolved_finding_and_recovery_internals() -> None:
    output = _render("normal")

    assert "!" not in output
    assert "recovery" not in output.lower()
    assert "rung" not in output.lower()
    assert "provider_schema_invalid" not in output


def test_verbose_display_keeps_resolved_finding_auditable() -> None:
    output = _render("verbose")

    assert "objective_schema_repair" in output
    assert "provider_schema_invalid" in output


def _render_downgrade(verbosity: str) -> str:
    display = TurnDisplay(verbosity=verbosity, status_line=False)
    event = {
        "event_type": "plan.recovery",
        "name": "react",
        "summary": "recovery react (4_react)",
        "payload": {
            "action": "react",
            "rung": "4_react",
            "notes": [
                "plan (missing_selected_skills) cannot run deterministically: "
                "single_skill_task requires selected_skills"
            ],
        },
    }
    with console.capture() as captured:
        display.tool_event("plan", event)
    return captured.get()


def test_normal_display_reports_the_change_of_route_not_the_validator() -> None:
    """The owner is told what happened, in words about their request.

    A downgrade printed its validator text verbatim, so a turn that had merely
    changed route read as a crash: an internal code and the phrase "cannot run
    deterministically", addressed to nobody in the room.
    """
    output = _render_downgrade("normal")

    assert "could not run" in output
    assert "continuing with tools" in output
    assert "missing_selected_skills" not in output
    assert "deterministically" not in output


def test_verbose_display_keeps_the_downgrade_findings_auditable() -> None:
    output = _render_downgrade("verbose")

    assert "4_react" in output
    assert "missing_selected_skills" in output
