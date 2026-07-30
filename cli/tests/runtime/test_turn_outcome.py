"""UX-09: a finished turn must not be shown as a successful answer when it is not."""

from __future__ import annotations

from types import SimpleNamespace

from omni.agent.turn_execution import TurnResult
from omni.runtime.turn_outcome import (
    bound_saturation_warnings,
    classify_turn_outcome,
    display_warnings,
    exec_exit_code,
    format_exec_file,
    has_valid_answer,
    header_state,
    informational_host_fill_notes,
    persist_exec_output,
)


def _turn(**fields: object) -> TurnResult:
    payload = {
        "text": "Request received: task_id=abc12345; planning failed: HTTP 429.",
        "session_id": "sess",
        "task_id": "abc12345deadbeef",
        "kind": "error",
        "terminated_reason": "llm_rate_limited",
        "settlement_status": "failed",
    }
    payload.update(fields)
    return TurnResult(**payload)  # type: ignore[arg-type]


def test_rate_limit_turn_is_failed_and_not_a_valid_answer() -> None:
    turn = _turn()

    assert classify_turn_outcome(turn) == "failed"
    assert has_valid_answer(turn) is False
    kind, body = format_exec_file(turn)
    assert kind == "error"
    assert "not** a completed answer" in body or "not a completed answer" in body
    assert "429" in body
    assert "Answer written" not in body


def test_degraded_literature_turn_is_stamped_not_sold_as_an_answer() -> None:
    turn = _turn(
        text="# Literature review\n\nRetrieval failed; this draft is ungrounded.",
        kind="text",
        terminated_reason="done",
        settlement_status="degraded",
        degraded_warnings=["OpenAlex and Semantic Scholar both returned 429."],
    )

    assert classify_turn_outcome(turn) == "degraded"
    assert has_valid_answer(turn) is True
    kind, body = format_exec_file(turn)
    assert kind == "partial"
    assert "Partial result (degraded)" in body
    assert "Literature review" in body
    assert "429" in body


def test_verification_failed_settlement_is_not_a_full_success() -> None:
    turn = _turn(
        text="# Survey\n\nLooks complete.",
        kind="text",
        terminated_reason="done",
        settlement_status="verification_failed",
        degraded_warnings=[],
    )

    assert classify_turn_outcome(turn) == "failed"
    assert format_exec_file(turn)[0] == "error"


def test_bound_saturated_seeds_are_a_display_warning() -> None:
    warnings = bound_saturation_warnings(
        {
            "seeds": [
                {"temperature": 0.2, "seed": 1},
                {"temperature": 0.2, "seed": 2},
                {"temperature": 0.2, "seed": 3},
            ],
            "bounds": {"temperature": [0.2, 1.0]},
        }
    )

    assert len(warnings) == 1
    assert "lower bound of temperature" in warnings[0]
    assert "0.2" in warnings[0]


def test_constraint_hits_are_surfaced_even_without_seed_rows() -> None:
    warnings = bound_saturation_warnings(
        {
            "constraint_hits": [
                {"name": "temperature", "value": 0.2, "bound": "min", "limit": 0.2}
            ]
        }
    )

    assert warnings
    assert "temperature=0.2" in warnings[0]


def test_bound_hits_on_a_succeeded_turn_render_as_degraded() -> None:
    turn = _turn(
        text="Best temperature is 0.2 for every seed.",
        kind="text",
        terminated_reason="done",
        settlement_status="succeeded",
        degraded_warnings=[],
        drained_results=[
            {
                "status": "succeeded",
                "result": {
                    "seeds": [{"temperature": 0.2}, {"temperature": 0.2}, {"temperature": 0.2}],
                    "bounds": {"temperature": {"min": 0.2, "max": 1.0}},
                },
            }
        ],
    )

    assert classify_turn_outcome(turn) == "degraded"
    assert header_state(turn) == "degraded"
    assert format_exec_file(turn)[0] == "partial"


def test_empty_success_is_an_error_report(tmp_path) -> None:  # noqa: ANN001
    turn = _turn(
        text="",
        kind="text",
        terminated_reason="done",
        settlement_status="succeeded",
        degraded_warnings=[],
    )
    path = tmp_path / "research_blueprint_v1.md"

    kind, code = persist_exec_output(path, turn)

    assert kind == "error"
    assert code == 1
    assert "not a completed answer" in path.read_text(encoding="utf-8")


def test_a_clean_success_writes_the_answer_unchanged() -> None:
    turn = _turn(
        text="# Blueprint\n\nA real research plan.",
        kind="text",
        terminated_reason="done",
        settlement_status="succeeded",
        degraded_warnings=[],
    )

    assert classify_turn_outcome(turn) == "succeeded"
    assert format_exec_file(turn) == ("answer", "# Blueprint\n\nA real research plan.")


def test_header_state_keeps_a_passed_schedule_quiet() -> None:
    turn = SimpleNamespace(kind="text", settlement_status="passed", terminated_reason="done")

    assert header_state(turn) == ""


def test_interrupted_turn_is_not_shown_as_user_cancel() -> None:
    turn = _turn(
        text="Execution was interrupted; the owning process exited.",
        kind="partial",
        terminated_reason="interrupted",
        settlement_status="skipped",
    )

    assert classify_turn_outcome(turn) == "interrupted"
    assert header_state(turn) == "interrupted"
    assert has_valid_answer(turn) is False
    kind, body = format_exec_file(turn)
    assert kind == "error"
    assert "interrupted" in body.lower()
    assert "cancelled by the user" not in body.lower()


def test_cancelled_outranks_an_unfinished_settlement() -> None:
    """UX-03: pending bookkeeping must not paint a user abort as success."""
    turn = _turn(
        text="The user cancelled execution; completed results were preserved.",
        kind="partial",
        terminated_reason="cancelled",
        settlement_status="pending",
    )

    assert classify_turn_outcome(turn) == "cancelled"
    assert header_state(turn) == "cancelled"
    assert has_valid_answer(turn) is False
    assert exec_exit_code(turn) == 1
    assert format_exec_file(turn)[0] == "error"


def test_cancelled_outranks_a_pending_child_task() -> None:
    turn = _turn(
        text="Created execution",
        kind="partial",
        terminated_reason="cancelled",
        settlement_status="pending_child_task",
    )

    assert classify_turn_outcome(turn) == "cancelled"
    assert exec_exit_code(turn) == 1


def test_synthesized_cancel_outranks_pending() -> None:
    turn = _turn(
        text="Partial answer before the user stopped the run.",
        kind="text",
        terminated_reason="synthesized_cancelled",
        settlement_status="pending",
    )

    assert classify_turn_outcome(turn) == "cancelled"


def test_interrupted_outranks_pending() -> None:
    turn = _turn(
        text="Execution was interrupted; the owning process exited.",
        kind="partial",
        terminated_reason="interrupted",
        settlement_status="pending_child_task",
    )

    assert classify_turn_outcome(turn) == "interrupted"
    assert header_state(turn) == "interrupted"
    assert exec_exit_code(turn) == 1


def test_a_pending_child_without_abort_is_still_a_successful_handoff() -> None:
    """Daemon / IM submit: the parent closed after handing work off."""
    turn = _turn(
        text="Created `literature-search` execution.",
        kind="text",
        terminated_reason="single_skill",
        settlement_status="pending_child_task",
        degraded_warnings=[],
    )

    assert classify_turn_outcome(turn) == "succeeded"
    assert header_state(turn) == ""
    assert exec_exit_code(turn) == 0


def test_successful_host_fill_notes_do_not_paint_partial_success() -> None:
    turn = _turn(
        text="Wrote the manuscript as a file.",
        kind="text",
        terminated_reason="done",
        settlement_status="succeeded",
        degraded_warnings=[
            "Host filled remaining draft.manuscript via native synthesis.",
            "Host filled remaining artifact.slides via research-pptx.",
        ],
    )

    assert display_warnings(turn) == []
    assert informational_host_fill_notes(turn) == [
        "Host filled remaining draft.manuscript via native synthesis.",
        "Host filled remaining artifact.slides via research-pptx.",
    ]
    assert classify_turn_outcome(turn) == "succeeded"
    assert header_state(turn) == ""
    assert format_exec_file(turn)[0] == "answer"


def test_failed_host_fill_notes_still_paint_partial_success() -> None:
    turn = _turn(
        text="Found last week's report.",
        kind="text",
        terminated_reason="done",
        settlement_status="succeeded",
        degraded_warnings=[
            "Host did not fill remaining draft.section: no this-turn research evidence.",
        ],
    )

    assert display_warnings(turn)
    assert classify_turn_outcome(turn) == "degraded"
