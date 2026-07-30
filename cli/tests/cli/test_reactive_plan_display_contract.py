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
