"""Interactive confirmation detection + presentation for skill checkpoints.

These pin the three checkpoints the redesign routes to the confirmation region
(research-pptx outline, scientific-poster approval, soulagent distillation) and
the conservative rule that an ordinary turn is left untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omni.cli.confirmation import (
    ConfirmationRequest,
    detect_confirmation,
    present_confirmation,
)


def _turn(*, tool_results=(), drained=()) -> SimpleNamespace:
    trace = [SimpleNamespace(name="run_skill", status="succeeded", result=r) for r in tool_results]
    return SimpleNamespace(tool_trace=trace, drained_results=list(drained))


def test_pptx_awaiting_review_becomes_a_confirmation_with_a_resume_submit() -> None:
    turn = _turn(tool_results=[{
        "status": "partial",
        "outcome": {"code": "awaiting_review"},
        "resume_token": "tok-123",
        "summary": "Outline ready for review.",
    }])
    request = detect_confirmation(turn)
    assert request is not None
    assert request.source == "research-pptx"
    approve = request.option("approve")
    assert approve is not None
    assert 'resume_token="tok-123"' in approve.submit


def test_poster_operator_confirmation_is_submitted_verbatim() -> None:
    phrase = "APPROVE SCIENTIFIC-POSTER 9f2a"
    turn = _turn(tool_results=[{
        "status": "partial",
        "approval": {"operator_confirmation": phrase},
        "summary": "Poster draft ready.",
    }])
    request = detect_confirmation(turn)
    assert request is not None
    assert request.source == "scientific-poster"
    # The skill matches the phrase exactly, so it must be forwarded unchanged.
    assert request.option("approve").submit == phrase


def test_soulagent_distillation_confirmation_is_detected() -> None:
    turn = _turn(drained=[{
        "result": {
            "status": "needs_input",
            "action_required": {"kind": "configure", "action": "confirm_scientist_distillation"},
            "message": "Distill Dr. X into a soul KG?",
        }
    }])
    request = detect_confirmation(turn)
    assert request is not None
    assert request.source == "soulagent"
    assert "distill" in request.option("approve").submit.lower()


def test_an_ordinary_turn_detects_nothing() -> None:
    turn = _turn(tool_results=[{"status": "ok", "summary": "done", "artifacts": []}])
    assert detect_confirmation(turn) is None


def test_awaiting_review_without_a_token_is_not_actionable() -> None:
    turn = _turn(tool_results=[{"outcome": {"code": "awaiting_review"}}])
    assert detect_confirmation(turn) is None


def test_the_most_recent_tool_checkpoint_wins() -> None:
    turn = _turn(tool_results=[
        {"approval": {"operator_confirmation": "APPROVE SCIENTIFIC-POSTER old"}},
        {"outcome": {"code": "awaiting_review"}, "resume_token": "newest"},
    ])
    request = detect_confirmation(turn)
    # tool_trace is scanned newest-first, so the pptx checkpoint is chosen.
    assert request is not None
    assert request.source == "research-pptx"


@pytest.mark.asyncio
async def test_present_confirmation_uses_the_tui_modal_when_available() -> None:
    seen: dict[str, object] = {}

    class FakeTui:
        async def request_approval(self, title, detail="", *, options=None):
            seen["title"] = title
            seen["options"] = tuple(opt.value for opt in options or ())
            return "approve"

    request = ConfirmationRequest(
        source="scientific-poster",
        title="Approve the poster draft?",
        detail="ready",
        options=detect_confirmation(_turn(tool_results=[{
            "approval": {"operator_confirmation": "APPROVE SCIENTIFIC-POSTER z"}
        }])).options,
    )
    choice = await present_confirmation(request, tui=FakeTui())
    assert choice == "approve"
    assert seen["title"] == "Approve the poster draft?"
    assert seen["options"] == ("approve", "deny")
