"""A turn must never end without saying how it ended.

Three separate surfaces used to drop the outcome on the floor: the turn
diagnostics only spoke about wall-clock stops, the workflow terminal line
ignored the error it was handed, and nested tool events rendered an anonymous
glyph whenever the name arrived under a different key. Together those produced
the "it just runs and then goes quiet" report. These tests pin each surface to
the reason codes it is required to cover.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omni.cli.live_display import TurnDisplay
from omni.cli.render import console
from omni.cli.runner import render_turn_diagnostics
from omni.core.termination import _BOUNDED_REASONS
from omni.runtime.presentation import _TERMINATION_LABELS


def _diagnostics(**turn_fields) -> str:  # noqa: ANN003
    turn = SimpleNamespace(
        kind=turn_fields.pop("kind", "text"),
        tool_trace=[],
        drained_results=[],
        task_id="abcdef1234",
        **turn_fields,
    )
    with console.capture() as capture:
        render_turn_diagnostics(turn)
    return capture.get()


@pytest.mark.parametrize("reason", sorted(_BOUNDED_REASONS))
def test_every_bounded_stop_reports_its_reason_and_task(reason):
    out = _diagnostics(terminated_reason=reason)

    assert "Best-effort result" in out
    assert "abcdef12" in out


@pytest.mark.parametrize("reason", ["max_iterations", "max_tool_calls"])
def test_budget_exhaustion_offers_the_affordance_that_lifts_it(reason):
    """Re-running under the same ceiling only exhausts it again, so say so."""
    out = _diagnostics(terminated_reason=reason)

    assert "wider budget" in out


@pytest.mark.parametrize("reason", sorted(_TERMINATION_LABELS))
def test_no_known_reason_code_ends_a_turn_silently(reason):
    out = _diagnostics(terminated_reason=reason)

    assert out.strip(), f"reason {reason!r} rendered nothing"


def test_unrecognized_reason_codes_are_reported_rather_than_dropped():
    out = _diagnostics(terminated_reason="some_future_stop_cause")

    assert "some_future_stop_cause" in out


@pytest.mark.parametrize("reason", ["", "done", "needs_input", "escalated"])
def test_intended_endings_stay_quiet(reason):
    assert _diagnostics(terminated_reason=reason).strip() == ""


def test_synthesized_prefix_does_not_hide_the_underlying_reason():
    out = _diagnostics(terminated_reason="synthesized_max_iterations")

    assert "iteration limit reached" in out


def test_failed_workflow_renders_the_error_it_was_given():
    display = TurnDisplay(verbosity="normal", status_line=False)

    with console.capture() as capture:
        display.tool_event(
            "task_done",
            {
                "workflow_run_id": "wf1234567890",
                "status": "failed",
                "error": "step draft_section produced no artifact",
            },
        )
        display.end()

    out = capture.get()
    assert "failed" in out
    assert "step draft_section produced no artifact" in out


def test_degraded_workflow_is_not_reported_as_a_plain_failure():
    display = TurnDisplay(verbosity="normal", status_line=False)

    with console.capture() as capture:
        display.tool_event(
            "task_done",
            {"workflow_run_id": "wf1234567890", "status": "degraded", "error": "1 of 3 steps skipped"},
        )
        display.end()

    out = capture.get()
    assert "degraded" in out
    assert "1 of 3 steps skipped" in out


@pytest.mark.parametrize("key", ["tool", "name"])
def test_nested_tool_name_renders_under_either_event_key(key):
    """Progress relays flatten the name as ``tool``; the loop emits ``name``."""
    display = TurnDisplay(verbosity="normal", status_line=False)

    with console.capture() as capture:
        display.tool_event(
            "task_progress",
            {"stage": "workflow.step.tool.start", key: "arxiv_search", "arguments": {}},
        )
        display.end()

    assert "arxiv_search" in capture.get()


def test_nameless_tool_event_is_labelled_instead_of_shown_as_a_glyph():
    display = TurnDisplay(verbosity="normal", status_line=False)

    with console.capture() as capture:
        display.tool_event("task_progress", {"stage": "workflow.step.tool.start", "tool": ""})
        display.end()

    out = capture.get()
    assert "unnamed" in out
    assert "⚙ ?" not in out
