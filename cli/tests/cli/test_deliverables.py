"""The closing deliverable block: what a turn produced and how long it took."""

from __future__ import annotations

from types import SimpleNamespace

from omni.cli.render import console
from omni.cli.runner import (
    _answer_changed_after_stream,
    render_deliverables,
    render_tasks,
    render_turn_outcome,
)
from omni.runtime.presentation import ArtifactRef, turn_presentation_from_result


def _capture(turn, **kwargs) -> str:
    with console.capture() as cap:
        render_deliverables(turn, **kwargs)
    return cap.get()


def test_a_sync_skill_turn_gets_a_deliverables_table_and_completion_line() -> None:
    turn = SimpleNamespace(
        drained_results=[],
        tool_trace=[
            SimpleNamespace(
                name="run_skill",
                status="succeeded",
                result={
                    "status": "ok",
                    "artifacts": [
                        {"title": "Transformer SVG", "path": "/w/fig.svg", "uri": "artifact://svg"},
                        {"title": "Transformer DOT", "path": "/w/fig.dot", "uri": "artifact://dot"},
                    ],
                },
            )
        ],
    )
    out = _capture(turn, elapsed_s=12.3, verbosity="normal")
    assert "Outputs" in out
    assert "Transformer SVG" in out
    assert "SVG" in out
    # DOT sources are indexed but never listed as a user deliverable.
    assert "Transformer DOT" not in out
    # The completion line reports the artifact count and the elapsed time.
    assert "1 artifact" in out
    assert "12.3s" in out


def test_empty_canonical_still_harvests_this_turn_write_file() -> None:
    turn = SimpleNamespace(
        artifacts=[],
        drained_results=[],
        tool_trace=[
            SimpleNamespace(
                name="write_file",
                status="succeeded",
                result={
                    "status": "ok",
                    "artifacts": [
                        {"title": "RAG survey", "path": "/tmp/rag_survey.md"},
                    ],
                },
            )
        ],
        task_id="newwrite1",
    )
    out = _capture(turn, elapsed_s=12.0, verbosity="normal")
    assert "Outputs" in out
    assert "/tmp/rag_survey.md" in out


def test_harvest_skips_internal_artifact_uris() -> None:
    turn = SimpleNamespace(
        artifacts=[],
        drained_results=[],
        tool_trace=[
            SimpleNamespace(
                name="write_file",
                status="succeeded",
                result={
                    "status": "ok",
                    "artifacts": [
                        {
                            "title": "latent-steering-related-work",
                            "uri": "artifact://35c6de7a6a7c4f8d93dd2dbeeffbc1e5",
                        }
                    ],
                },
            )
        ],
        task_id="newwrite2",
    )
    out = _capture(turn, elapsed_s=12.0, verbosity="normal")
    assert "artifact://" not in out
    assert "35c6de7a6a7c4f8d93dd2dbeeffbc1e5" not in out


def test_empty_canonical_artifacts_do_not_harvest_sibling_paths() -> None:
    turn = SimpleNamespace(
        artifacts=[],
        drained_results=[],
        tool_trace=[
            SimpleNamespace(
                name="search_corpus",
                status="succeeded",
                result={
                    "status": "ok",
                    "artifacts": [
                        {
                            "title": "old RAG paper",
                            "path": "/Users/antonio/work/outputs/RAG-系统综述_2f141af4/RAG_survey_paper.md",
                        }
                    ],
                },
            )
        ],
        settlement_status="degraded",
        task_id="72590550",
    )
    out = _capture(turn, elapsed_s=12.0, verbosity="normal")
    assert "Outputs" not in out
    assert "RAG_survey_paper.md" not in out
    assert "3 artifact" not in out
    assert "degraded" in out


def test_canonical_turn_outputs_use_real_paths_and_hide_support_records() -> None:
    turn = SimpleNamespace(
        artifacts=[
            ArtifactRef(title="RAG survey", format="md", path="/papers/rag.md", uri="artifact://paper"),
            ArtifactRef(title="Figure PNG", format="png", path="/figures/rag.png", uri="artifact://png"),
            ArtifactRef(title="Figure SVG", format="svg", path="/figures/rag.svg", uri="artifact://svg"),
            ArtifactRef(title="Figure DOT", format="dot", path="/figures/rag.dot", uri="artifact://dot"),
            ArtifactRef(
                title="Figure provenance",
                format="json",
                path="/figures/rag.provenance.json",
                uri="artifact://provenance",
                presentation_role="support",
            ),
        ],
        drained_results=[],
        tool_trace=[SimpleNamespace(name="write_file", status="succeeded", result="ok")],
    )

    out = _capture(turn, elapsed_s=3.0, verbosity="normal")

    assert out.count("Outputs") == 1
    assert "/papers/rag.md" in out
    assert "/figures/rag.png" in out
    assert "/figures/rag.svg" in out
    assert "/figures/rag.dot" not in out
    assert "provenance" not in out
    assert "artifact://" not in out
    assert "3 artifacts" in out


def test_artifact_uri_projection_does_not_repeat_a_streamed_answer() -> None:
    turn = SimpleNamespace(
        text="Saved artifact://paper",
        task_id="task-1",
        session_id="session-1",
        kind="text",
        terminated_reason="done",
        plan_summary="",
        degraded_warnings=[],
        settlement_status="",
        submitted_workflow_ids=[],
        submitted_subtask_ids=[],
        drained_results=[],
        artifacts=[
            ArtifactRef(
                title="Paper",
                format="md",
                uri="artifact://paper",
                path="/workspace/reports/paper.md",
            )
        ],
    )
    presentation = turn_presentation_from_result(turn)

    assert _answer_changed_after_stream("Saved artifact://paper", presentation) is False


def test_an_async_task_turn_is_summarised_without_repeating_the_card_table() -> None:
    turn = SimpleNamespace(
        drained_results=[{"result": {"artifacts": [{"title": "Report", "path": "/w/r.md"}]}}],
        tool_trace=[],
    )
    out = _capture(turn, elapsed_s=90.0, verbosity="normal")
    # No second artifact table for async tasks (the card already listed them)...
    assert "Outputs" not in out
    # ...but the closing stats line still lands, with a minutes-formatted elapsed.
    assert "1 task" in out
    assert "1m30s" in out


def test_the_closing_line_names_the_task_it_just_ran() -> None:
    """The id has to survive the turn that produced it.

    It was shown only in the live status line, which is erased on completion,
    so a finished run left no id anywhere the user could read. When run
    2db31f83 was also filed as ``chat`` and dropped out of ``/task list``, there
    was no way left to name it — not in the scrollback, not in the ledger.
    """
    turn = SimpleNamespace(
        drained_results=[], tool_trace=[], task_id="2db31f83c4e5460fa1b2c3d4e5f60718"
    )

    out = _capture(turn, elapsed_s=8.0, verbosity="normal")

    assert "2db31f83" in out


def test_the_closing_line_shows_task_tokens_and_cost() -> None:
    turn = SimpleNamespace(
        drained_results=[],
        tool_trace=[SimpleNamespace(name="run_skill", status="succeeded", result={"status": "ok"})],
        task_id="0abee92a00000000",
        cost={"total_tokens": 12400, "cost_usd": 0.0123},
        usage={"total_tokens": 100},
    )
    out = _capture(turn, elapsed_s=8.0, verbosity="normal")
    assert "12.4k tokens" in out
    assert "$0.0123" in out


def test_a_trivial_quick_turn_prints_nothing() -> None:
    turn = SimpleNamespace(drained_results=[], tool_trace=[], task_id="t1")
    assert _capture(turn, elapsed_s=0.4, verbosity="normal") == ""


def test_a_slow_toolless_turn_still_confirms_completion() -> None:
    turn = SimpleNamespace(drained_results=[], tool_trace=[])
    out = _capture(turn, elapsed_s=8.0, verbosity="normal")
    assert "done" in out
    assert "8.0s" in out


def test_a_degraded_turn_closes_as_degraded_not_done() -> None:
    turn = SimpleNamespace(
        kind="text",
        settlement_status="degraded",
        terminated_reason="done",
        degraded_warnings=["Retrieval failed; the report is ungrounded."],
        drained_results=[{"status": "degraded"}],
        tool_trace=[],
        task_id="litfail01",
    )
    out = _capture(turn, elapsed_s=12.0, verbosity="normal")
    assert "degraded" in out
    assert "done" not in out


def test_degraded_warnings_are_printed_as_a_partial_success_banner() -> None:
    turn = SimpleNamespace(
        kind="text",
        settlement_status="degraded",
        terminated_reason="done",
        degraded_warnings=["OpenAlex and Semantic Scholar both returned 429."],
        drained_results=[],
        tool_trace=[],
    )
    with console.capture() as cap:
        render_turn_outcome(turn)
    out = cap.get()
    assert "Partial success" in out
    assert "429" in out


def test_successful_host_fill_is_an_info_line_not_a_partial_banner() -> None:
    turn = SimpleNamespace(
        kind="text",
        settlement_status="succeeded",
        terminated_reason="done",
        degraded_warnings=["Host filled remaining draft.manuscript via native synthesis."],
        drained_results=[],
        tool_trace=[],
    )
    with console.capture() as cap:
        render_turn_outcome(turn)
    out = cap.get()
    assert "Partial success" not in out
    assert "Host filled remaining draft.manuscript" in out


def test_last_step_names_only_a_failed_trailing_tool() -> None:
    from omni.cli.runner import render_turn_diagnostics

    failed = SimpleNamespace(
        kind="error",
        terminated_reason="tool_error",
        task_id="deadbeef" + "0" * 24,
        tool_trace=[
            SimpleNamespace(name="run_skill", status="failed", error="vlm timeout"),
            SimpleNamespace(name="update_plan", status="succeeded", error=""),
        ],
    )
    with console.capture() as cap:
        render_turn_diagnostics(failed)
    assert "Last step" not in cap.get()

    last_failed = SimpleNamespace(
        kind="error",
        terminated_reason="tool_error",
        task_id="deadbeef" + "0" * 24,
        tool_trace=[
            SimpleNamespace(name="run_skill", status="failed", error="vlm timeout"),
        ],
    )
    with console.capture() as cap:
        render_turn_diagnostics(last_failed)
    out = cap.get()
    assert "Last step: run_skill: vlm timeout" in out


def test_a_degraded_task_card_uses_the_caution_mark_not_a_red_cross() -> None:
    turn = SimpleNamespace(
        task_id="task123456789",
        session_id="sess",
        submitted_workflow_ids=[],
        submitted_subtask_ids=["exec123456789"],
        artifacts=[],
        drained_results=[
            {
                "subtask_id": "exec123456789",
                "task_id": "task123456789",
                "skill": "literature-search",
                "status": "degraded",
                "result": {"summary": "Fell back after 429; report is ungrounded."},
                "trace": [],
            }
        ],
    )
    with console.capture() as cap:
        render_tasks(turn)
    out = cap.get()
    assert "(degraded)" in out
    assert "literature-search" in out


def test_a_cancelled_turn_closes_as_cancelled_not_done() -> None:
    turn = SimpleNamespace(
        kind="partial",
        settlement_status="skipped",
        terminated_reason="cancelled",
        degraded_warnings=[],
        drained_results=[],
        tool_trace=[SimpleNamespace(name="search", status="cancelled")],
        task_id="cancel001",
    )
    out = _capture(turn, elapsed_s=8.0, verbosity="normal")
    assert "cancelled" in out
    assert "done" not in out


def test_a_cancelled_pending_turn_closes_as_cancelled_not_done() -> None:
    """The closer must follow the abort, not the unfinished settlement."""
    turn = SimpleNamespace(
        kind="partial",
        settlement_status="pending",
        terminated_reason="cancelled",
        degraded_warnings=[],
        drained_results=[],
        tool_trace=[SimpleNamespace(name="search", status="cancelled")],
        task_id="cancel002",
    )
    out = _capture(turn, elapsed_s=8.0, verbosity="normal")
    assert "cancelled" in out
    assert "done" not in out


def test_a_cancelled_turn_prints_an_abort_banner() -> None:
    turn = SimpleNamespace(
        kind="partial",
        settlement_status="skipped",
        terminated_reason="cancelled",
        degraded_warnings=[],
        drained_results=[],
        tool_trace=[],
    )
    with console.capture() as cap:
        render_turn_outcome(turn)
    out = cap.get()
    assert "cancelled" in out.lower()
    assert "preserved" in out.lower()


def test_an_interrupted_turn_prints_an_abort_banner() -> None:
    turn = SimpleNamespace(
        kind="partial",
        settlement_status="skipped",
        terminated_reason="interrupted",
        degraded_warnings=[],
        drained_results=[],
        tool_trace=[],
    )
    with console.capture() as cap:
        render_turn_outcome(turn)
    out = cap.get()
    assert "interrupted" in out.lower()
    assert "cancelled" not in out.lower()


def test_a_pending_child_handoff_still_closes_as_done() -> None:
    turn = SimpleNamespace(
        kind="text",
        settlement_status="pending_child_task",
        terminated_reason="single_skill",
        degraded_warnings=[],
        drained_results=[],
        tool_trace=[SimpleNamespace(name="run_skill", status="submitted")],
        task_id="handoff01",
    )
    out = _capture(turn, elapsed_s=3.0, verbosity="normal")
    assert "done" in out
    assert "cancelled" not in out


def test_a_failed_turn_closes_as_failed() -> None:
    turn = SimpleNamespace(
        kind="error",
        settlement_status="failed",
        terminated_reason="llm_rate_limited",
        degraded_warnings=[],
        drained_results=[],
        tool_trace=[SimpleNamespace(name="search", status="failed")],
        task_id="rate42901",
    )
    out = _capture(turn, elapsed_s=3.0, verbosity="normal")
    assert "failed" in out
    assert "done" not in out
