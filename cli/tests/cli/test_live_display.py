"""CLI live-progress display (Claude Code / Codex-style turn transcript).

TurnDisplay turns the orchestrator's ``on_tool_event`` stream into a running
terminal transcript: plan decisions, tool calls with argument/result previews,
workflow step hierarchy, and budget notices. These tests drive the event sink
directly and assert on captured console output (no terminal → no status line).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omni.cli.live_display import TurnDisplay, resolve_verbosity
from omni.cli.render import console


def _capture(display: TurnDisplay, events: list[tuple[str, dict]]) -> str:
    with console.capture() as cap:
        for phase, data in events:
            display.tool_event(phase, data)
        display.end()
    return cap.get()


def test_tool_start_shows_name_and_argument_summary():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [("start", {"name": "memory_search", "arguments": {"query": "RAG optimization", "limit": 5}})],
    )
    assert "memory_search" in out
    assert "query=RAG optimization" in out
    assert "limit=5" in out


def test_sensitive_argument_values_are_masked():
    display = TurnDisplay(verbosity="verbose", status_line=False)
    out = _capture(
        display,
        [("start", {"name": "http_fetch", "arguments": {"url": "https://x.test", "api_key": "sk-123456"}})],
    )
    assert "sk-123456" not in out
    assert "api_key=***" in out


def test_tool_done_shows_duration_and_result_preview():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [("done", {"name": "arxiv_search", "duration_ms": 1234.5, "result": {"summary": "5 papers found"}})],
    )
    assert "✓" in out
    assert "arxiv_search" in out
    assert "1.2s" in out
    assert "5 papers found" in out


def test_successful_historical_task_read_is_not_rendered_as_current_failure():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "done",
                {
                    "name": "get_task",
                    "status": "succeeded",
                    "result": {
                        "task_id": "old-task",
                        "task_status": "cancelled",
                        "status": "cancelled",
                        "summary": "Partial result: The user cancelled execution.",
                    },
                },
            )
        ],
    )

    assert "✓" in out
    assert "✗" not in out
    assert "get_task" in out
    assert "Task old-task status: cancelled" in out
    assert "Partial result" not in out


def test_successful_historical_subtask_read_is_not_rendered_as_current_failure():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "done",
                {
                    "name": "get_subtask",
                    "status": "succeeded",
                    "result": {
                        "subtask_id": "old-child",
                        "subtask_status": "failed",
                        "status": "failed",
                        "error": "historical provider failure",
                    },
                },
            )
        ],
    )

    assert "✓" in out
    assert "✗" not in out
    assert "Subtask old-chil status: failed" in out
    assert "historical provider failure" not in out


def test_budget_rejected_calls_collapse_to_one_line_not_per_call_spam():
    # An over-budget batch of 3 rejected calls plus the single ``budget`` event:
    # the display must render exactly one budget notice, not one ``∅`` per refused
    # call (the spam symptom in incident 78071dd2).
    display = TurnDisplay(verbosity="normal", status_line=False)
    events: list[tuple[str, dict]] = [
        (
            "done",
            {
                "name": "get_task",
                "status": "rejected",
                "error": "The tool call was not executed because the turn reached its hard tool budget.",
            },
        )
        for _ in range(3)
    ]
    events.append(("budget", {"reason": "max_tool_calls", "budget": {"rejected": 3}}))
    out = _capture(display, events)

    assert "∅" not in out
    assert out.count("configured tool-call limit reached") == 1


def test_tool_failure_renders_error_line():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [("done", {"name": "http_fetch", "error": "timeout after 30s", "duration_ms": 30000.0})],
    )
    assert "✗" in out
    assert "http_fetch" in out
    assert "timeout after 30s" in out


@pytest.mark.parametrize(
    ("command_status", "expected_glyph"),
    [
        ("failed", "✗"),
        ("timed_out", "✗"),
        ("blocked", "∅"),
        ("invalid", "∅"),
    ],
)
def test_command_outcome_failure_never_renders_as_green_success(
    command_status: str,
    expected_glyph: str,
):
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "done",
                {
                    "name": "bash",
                    "status": "succeeded",
                    "result": {
                        "result_schema": "omni.command-result.v1",
                        "command_status": command_status,
                        "exit_code": 1 if command_status == "failed" else None,
                        "summary": f"Command outcome: {command_status}",
                    },
                },
            )
        ],
    )

    assert expected_glyph in out
    assert "✓" not in out
    assert f"Command outcome: {command_status}" in out


def test_successful_command_outcome_previews_process_output():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "done",
                {
                    "name": "bash",
                    "status": "succeeded",
                    "result": {
                        "result_schema": "omni.command-result.v1",
                        "command_status": "succeeded",
                        "exit_code": 0,
                        "output": "first line\nsecond line",
                        "summary": "Command completed successfully",
                    },
                },
            )
        ],
    )

    assert "✓" in out
    assert "first line second line" in out
    assert "Command completed successfully" not in out


def test_foreign_status_dict_is_not_rendered_as_command_failure():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "done",
                {
                    "name": "external_tool",
                    "status": "succeeded",
                    "result": {
                        "result_schema": "external.result.v1",
                        "command_status": "failed",
                        "summary": "External result",
                    },
                },
            )
        ],
    )

    assert "✓" in out
    assert "✗" not in out
    assert "External result" in out


def test_nested_prompt_command_failure_is_visible_in_normal_mode():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "task_progress",
                {
                    "subtask_id": "task-1",
                    "stage": "tool.done",
                    "tool": "bash",
                    "status": "succeeded",
                    "result": {
                        "result_schema": "omni.command-result.v1",
                        "command_status": "failed",
                        "reason": "nonzero_exit",
                        "exit_code": 1,
                        "summary": "Command exited with code 1",
                    },
                },
            )
        ],
    )

    assert "✗" in out
    assert "✓" not in out
    assert "bash" in out
    assert "Command exited with code 1" in out


def test_command_failure_preview_shows_the_process_error_not_just_the_exit_code():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "done",
                {
                    "name": "bash",
                    "status": "succeeded",
                    "duration_ms": 103,
                    "result": {
                        "result_schema": "omni.command-result.v1",
                        "command_status": "failed",
                        "reason": "nonzero_exit",
                        "exit_code": 128,
                        "output": "致命错误：不是 Git 仓库（或者任何父目录）：.git\n",
                        "summary": "Command exited with code 128",
                    },
                },
            )
        ],
    )

    flat = " ".join(out.split())
    assert "✗" in out
    assert "bash" in out
    assert "不是 Git 仓库" in flat
    assert "Command exited with code 128" in flat


def test_command_failure_keeps_the_command_and_prefers_stderr():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "start",
                {"name": "bash", "arguments": {"command": "tail log; ./nope"}},
            ),
            (
                "done",
                {
                    "name": "bash",
                    "status": "succeeded",
                    "duration_ms": 68,
                    "arguments": {"command": "tail log; ./nope"},
                    "result": {
                        "result_schema": "omni.command-result.v1",
                        "command_status": "failed",
                        "reason": "nonzero_exit",
                        "exit_code": 126,
                        "output": "[ 36%]\n./nope: Permission denied\n",
                        "stderr": "./nope: Permission denied\n",
                        "summary": "Command exited with code 126: ./nope: Permission denied",
                    },
                },
            ),
        ],
    )

    flat = " ".join(out.split())
    assert "✗" in out
    assert "tail log; ./nope" in flat
    assert "Permission denied" in flat
    assert "126: [ 36%]" not in flat


def test_command_failure_glosses_126_when_output_is_only_progress():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "done",
                {
                    "name": "bash",
                    "status": "succeeded",
                    "duration_ms": 68,
                    "arguments": {"command": "./pytest_full.log"},
                    "result": {
                        "result_schema": "omni.command-result.v1",
                        "command_status": "failed",
                        "reason": "nonzero_exit",
                        "exit_code": 126,
                        "output": "[ 36%]\n",
                        "summary": "Command exited with code 126: [ 36%]",
                    },
                },
            )
        ],
    )

    flat = " ".join(out.split())
    assert "./pytest_full.log" in flat
    assert "cannot execute" in flat
    assert "126: [ 36%]" not in flat


def test_command_failure_body_keeps_head_and_tail():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "done",
                {
                    "name": "bash",
                    "status": "succeeded",
                    "result": {
                        "result_schema": "omni.command-result.v1",
                        "command_status": "failed",
                        "reason": "nonzero_exit",
                        "exit_code": 1,
                        "output": "head-a\nhead-b\nmiddle-1\nmiddle-2\nmiddle-3\ntail-a\ntail-b\n",
                        "summary": "Command exited with code 1: tail-b",
                    },
                },
            )
        ],
    )

    assert "head-a" in out
    assert "tail-b" in out
    assert "+3 line(s)" in out
    assert "middle-2" not in out


def test_nested_prompt_transport_failure_without_error_is_not_hidden():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "task_progress",
                {
                    "subtask_id": "task-1",
                    "stage": "tool.done",
                    "tool": "bash",
                    "status": "failed",
                },
            )
        ],
    )

    assert "✗" in out
    assert "✓" not in out
    assert "bash" in out
    assert "failed" in out


def test_human_progress_is_visible_and_placeholder_question_mark_is_suppressed() -> None:
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "task_progress",
                {"subtask_id": "review-1", "stage": "?", "pct": 0.1},
            ),
            (
                "task_progress",
                {
                    "subtask_id": "review-1",
                    "stage": "Paper text ready; starting full-manuscript understanding",
                    "pct": 0.2,
                },
            ),
        ],
    )

    assert "?" not in out
    assert "Paper text ready" in out
    assert "20%" in out


def test_needs_input_execution_uses_yellow_warning() -> None:
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "task_done",
                {
                    "subtask_id": "review-2",
                    "task_id": "owner-task-2",
                    "skill": "paper-review",
                    "status": "needs_input",
                    "result": {
                        "status": "needs_input",
                        "outcome": "needs_input",
                        "error": "Paper review needs a local PDF. Missing: draft.pdf.",
                    },
                },
            )
        ],
    )
    assert "⚠" in out
    assert "needs_input" in out
    assert "✗" not in out


def test_degraded_execution_shows_cause_and_missing_artifact() -> None:
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "task_done",
                {
                    "subtask_id": "review-1",
                    "task_id": "owner-task-1",
                    "skill": "paper-review",
                    "status": "degraded",
                    "result": {
                        "summary": "Paper review stopped during evidence preparation.",
                        "artifacts": [],
                    },
                },
            )
        ],
    )

    assert "⚠" in out
    assert "evidence preparation" in out
    assert "No saved artifact was produced" in out


def test_live_task_events_label_execution_and_workflow_ids_by_kind():
    display = TurnDisplay(verbosity="normal", status_line=False)

    execution = _capture(
        display,
        [
            (
                "task_start",
                {
                    "subtask_id": "execution12345678",
                    "task_id": "task123456789",
                    "skill": "literature-search",
                },
            ),
            (
                "task_done",
                {
                    "subtask_id": "execution12345678",
                    "task_id": "task123456789",
                    "skill": "literature-search",
                    "status": "succeeded",
                },
            ),
        ],
    )
    workflow = _capture(
        display,
        [
            (
                "task_start",
                {
                    "workflow_run_id": "workflow12345678",
                    "task_id": "task123456789",
                    "kind": "workflow",
                },
            ),
            (
                "task_done",
                {
                    "workflow_run_id": "workflow12345678",
                    "task_id": "task123456789",
                    "kind": "workflow",
                    "status": "succeeded",
                },
            ),
        ],
    )

    assert "execution=executio" in execution
    assert "task=task1234" in execution
    assert "task literature-search" not in execution
    assert "workflow=workflow" in workflow
    assert "task=task1234" in workflow
    assert "task ?" not in workflow


def test_failed_execution_keeps_execution_and_owning_task_identity():
    display = TurnDisplay(verbosity="normal", status_line=False)

    out = _capture(
        display,
        [
            (
                "task_done",
                {
                    "subtask_id": "execution12345678",
                    "task_id": "task123456789",
                    "skill": "literature-search",
                    "status": "failed",
                    "error": "provider unavailable",
                },
            ),
        ],
    )

    assert "execution=executio" in out
    assert "task=task1234" in out
    assert "provider unavailable" in out


def test_edit_file_success_renders_colored_diff_cell():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "done",
                {
                    "name": "edit_file",
                    "status": "ok",
                    "result": "OK: edited notes.md",
                    "arguments": {
                        "path": "notes.md",
                        "old_string": "alpha\nbeta\ngamma",
                        "new_string": "alpha\nBETA\ngamma",
                    },
                },
            )
        ],
    )
    assert "± notes.md" in out
    assert "+1" in out and "-1" in out  # one line changed → one add + one remove
    assert "+BETA" in out and "-beta" in out


def test_write_file_success_renders_added_lines_diff():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "done",
                {
                    "name": "write_file",
                    "status": "ok",
                    "result": "OK: wrote 24 chars to hello.py",
                    "arguments": {"path": "hello.py", "contents": "print('a')\nprint('b')"},
                },
            )
        ],
    )
    assert "± hello.py" in out
    assert "+2" in out and "-0" in out
    assert "print('a')" in out


def test_large_write_file_shows_only_a_compact_summary_in_normal_mode():
    contents = "\n".join(f"paper line {index}: detailed research prose" for index in range(1, 242))
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "done",
                {
                    "name": "write_file",
                    "status": "ok",
                    "result": "OK: wrote 11350 chars to /papers/rag_system_survey.md",
                    "arguments": {
                        "path": "/papers/rag_system_survey.md",
                        "contents": contents,
                    },
                },
            )
        ],
    )

    assert "± /papers/rag_system_survey.md" in out
    assert "+241" in out and "-0" in out
    assert "content omitted" in out
    assert "paper line 1:" not in out
    assert "more diff line" not in out


def test_no_op_edit_and_quiet_mode_skip_the_diff_cell():
    noop = _capture(
        TurnDisplay(verbosity="normal", status_line=False),
        [(
            "done",
            {"name": "edit_file", "status": "ok",
             "arguments": {"path": "x", "old_string": "same", "new_string": "same"}},
        )],
    )
    assert "±" not in noop  # identical old/new → nothing to show

    quiet = _capture(
        TurnDisplay(verbosity="quiet", status_line=False),
        [(
            "done",
            {"name": "write_file", "status": "ok",
             "arguments": {"path": "x", "contents": "line1\nline2"}},
        )],
    )
    assert "±" not in quiet  # quiet verbosity suppresses all event output


def test_workflow_step_hierarchy_with_nested_tools_and_elapsed():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            ("task_start", {"subtask_id": "task-1234abcd", "skill": "workflow"}),
            ("task_progress", {"subtask_id": "task-1234abcd", "stage": "workflow.start", "total_steps": 3, "goal": "survey RAG"}),
            (
                "task_progress",
                {
                    "subtask_id": "task-1234abcd",
                    "stage": "workflow.step.start",
                    "step_id": "s1",
                    "skill": "arxiv-search",
                    "index": 1,
                    "total_steps": 3,
                },
            ),
            (
                "task_progress",
                {
                    "subtask_id": "task-1234abcd",
                    "stage": "workflow.step.tool.start",
                    "step_id": "s1",
                    "tool": "http_fetch",
                    "arguments": {"url": "https://arxiv.org"},
                },
            ),
            (
                "task_progress",
                {"subtask_id": "task-1234abcd", "stage": "workflow.step.done", "step_id": "s1", "skill": "arxiv-search"},
            ),
            ("task_done", {"subtask_id": "task-1234abcd", "skill": "workflow", "status": "succeeded"}),
        ],
    )
    assert "task" in out and "workflow" in out
    assert "3 step(s)" in out
    assert "[1/3]" in out and "arxiv-search" in out
    assert "http_fetch" in out
    assert "done" in out
    assert "succeeded" in out


def test_step_failure_carries_error_and_index_from_start_event():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            (
                "task_progress",
                {"subtask_id": "t", "stage": "workflow.step.start", "step_id": "fig", "skill": "scientific-figure", "index": 2, "total_steps": 2},
            ),
            (
                "task_progress",
                {"subtask_id": "t", "stage": "workflow.step.failed", "step_id": "fig", "skill": "scientific-figure", "error": "renderer crashed"},
            ),
        ],
    )
    assert out.count("[2/2]") == 2  # start and terminal line share the index label
    assert "renderer crashed" in out


def test_plan_events_render_intent_steps_warnings_and_recovery():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            ("plan", {"event_type": "plan.model.proposed", "name": "workflow", "summary": "multi-step research request"}),
            (
                "plan",
                {
                    "event_type": "plan.validated",
                    "name": "workflow",
                    "summary": "Plan: workflow",
                    "payload": {
                        "intent_type": "workflow",
                        "status": "degraded",
                        "warnings": ["step generate_figure input was seeded from the goal"],
                        "steps": ["arxiv-search", "scientific-figure"],
                    },
                },
            ),
            (
                "plan",
                {
                    "event_type": "plan.recovery",
                    "name": "needs_input",
                    "summary": "recovery needs_input (rung2)",
                    "payload": {"action": "needs_input", "rung": "rung2", "notes": ["missing identifier for arxiv-fetch"]},
                },
            ),
        ],
    )
    assert "plan" in out and "workflow" in out
    assert "multi-step research request" in out
    # A validated multi-step plan renders as a checklist of the step skills.
    assert "arxiv-search" in out and "scientific-figure" in out
    assert "degraded" in out
    assert "seeded from the goal" in out
    assert "recovery" in out and "needs_input" in out
    assert "missing identifier" in out


def test_plan_checklist_updates_checkboxes_in_place():
    """A validated workflow plan renders one checklist slot that fills in live."""
    from omni.cli.repl_output import use_output_sink

    class Sink:
        def __init__(self) -> None:
            self.events: list = []

        def write(self, text: str) -> None:
            pass

        def publish_event(self, event) -> None:  # noqa: ANN001
            self.events.append(event)

        def set_status(self, text: str) -> None:
            pass

        def clear(self) -> None:
            pass

    sink = Sink()
    display = TurnDisplay(verbosity="normal", status_line=False)
    with use_output_sink(sink):
        display.tool_event(
            "plan",
            {
                "event_type": "plan.validated",
                "name": "workflow",
                "payload": {"status": "validated", "steps": ["arxiv-search", "scientific-figure"]},
            },
        )
        display.tool_event(
            "task_progress",
            {"subtask_id": "t", "stage": "workflow.step.start", "step_id": "s1",
             "skill": "arxiv-search", "index": 1, "total_steps": 2},
        )
        display.tool_event(
            "task_progress",
            {"subtask_id": "t", "stage": "workflow.step.done", "step_id": "s1",
             "skill": "arxiv-search", "index": 1},
        )
        display.tool_event(
            "task_progress",
            {"subtask_id": "t", "stage": "workflow.step.start", "step_id": "s2",
             "skill": "scientific-figure", "index": 2, "total_steps": 2},
        )

    slots = [event for event in sink.events if event.replace_key == "plan.summary"]
    assert slots, "expected a live checklist slot"
    final = slots[-1].payload
    # Step 1 finished (✔), step 2 now active (▸); both live in the one slot.
    assert "✔" in final and "arxiv-search" in final
    assert "▸" in final and "scientific-figure" in final


def test_clean_recovery_pass_through_is_silent():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [("plan", {"event_type": "plan.recovery", "name": "execute", "summary": "recovery execute (none)", "payload": {"action": "execute", "notes": []}})],
    )
    assert out.strip() == ""


def test_internal_model_planner_failure_is_only_visible_in_verbose_mode():
    events = [
        (
            "plan",
            {
                "event_type": "plan.model.failed",
                "name": "model_planner",
                "summary": "model planner failed; falling back to deterministic planner",
            },
        )
    ]

    normal_out = _capture(TurnDisplay(verbosity="normal", status_line=False), events)
    verbose_out = _capture(TurnDisplay(verbosity="verbose", status_line=False), events)

    assert normal_out.strip() == ""
    assert "model planner failed" in verbose_out


def test_quiet_verbosity_suppresses_all_event_output():
    display = TurnDisplay(verbosity="quiet", status_line=False)
    out = _capture(
        display,
        [
            ("plan", {"event_type": "plan.validated", "name": "workflow", "payload": {}}),
            ("start", {"name": "memory_search", "arguments": {"query": "x"}}),
            ("done", {"name": "memory_search", "result": "ok"}),
            ("task_start", {"subtask_id": "t", "skill": "workflow"}),
        ],
    )
    assert out.strip() == ""


def test_skill_stages_and_context_shown_only_in_verbose():
    events = [
        ("task_progress", {"subtask_id": "t", "stage": "skill.start", "skill": "deep-research"}),
        ("plan", {"event_type": "context.assembled", "name": "turn_context", "summary": "focus: artifact 1a2b"}),
    ]
    normal_out = _capture(TurnDisplay(verbosity="normal", status_line=False), events)
    verbose_out = _capture(TurnDisplay(verbosity="verbose", status_line=False), events)
    assert "deep-research" not in normal_out
    assert "focus: artifact" not in normal_out
    assert "deep-research" in verbose_out
    assert "focus: artifact" in verbose_out


def test_budget_notice_renders_reason():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(display, [("budget", {"reason": "max_tool_calls", "budget": {"completed": 12}})])
    assert "configured tool-call limit" in out
    assert "pending calls were rejected" not in out


def test_token_limit_notice_is_not_mislabeled_as_a_tool_budget():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(display, [("budget", {"reason": "max_total_tokens"})])

    assert "configured cumulative token limit" in out
    assert "tool budget" not in out


def test_context_rollover_is_quiet_normally_and_visible_in_debug():
    event = (
        "notice",
        {"kind": "context_rollover", "context_window": {"rollovers": 2}},
    )

    normal = _capture(TurnDisplay(verbosity="normal", status_line=False), [event])
    debug = _capture(
        TurnDisplay(verbosity="normal", status_line=False, debug=True), [event]
    )

    assert normal.strip() == ""
    assert "context compacted; continuing" in debug
    assert "window 2" in debug


def test_streamed_tokens_are_closed_before_event_lines():
    display = TurnDisplay(verbosity="normal", status_line=False)
    with console.capture() as cap:
        display.token("Hello, wor")
        display.token("ld")
        display.tool_event("start", {"name": "memory_search", "arguments": {}})
        display.end()
    out = cap.get()
    assert "Hello, world" in out
    # The tool line must start on a fresh line, not be glued to the stream.
    stream_pos = out.index("Hello, world")
    tool_pos = out.index("memory_search")
    assert "\n" in out[stream_pos:tool_pos]
    assert display.streamed_text == "Hello, world"


def test_markup_in_dynamic_text_is_escaped():
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [("start", {"name": "shell", "arguments": {"command": "[red]rm -rf[/red]"}})],
    )
    # Rendered literally instead of being interpreted as Rich markup.
    assert "rm -rf" in out


def test_streaming_answer_updates_one_managed_slot_then_finalizes():
    """In a managed TUI the answer streams into a single in-place markdown slot."""
    from omni.cli.repl_output import use_output_sink
    from omni.cli.repl_transcript import TranscriptKind

    class Sink:
        def __init__(self) -> None:
            self.events: list = []

        def write(self, text: str) -> None:
            self.events.append(("write", text))

        def publish_event(self, event) -> None:  # noqa: ANN001
            self.events.append(event)

        def set_status(self, text: str) -> None:
            pass

        def clear(self) -> None:
            pass

    sink = Sink()
    display = TurnDisplay(verbosity="normal", status_line=False)
    with use_output_sink(sink):
        display.token("Hello ")
        display.token("**world**")
        assert display.finalize_answer("Hello **world**!") is True

    markdown = [e for e in sink.events if getattr(e, "kind", None) == TranscriptKind.MARKDOWN]
    assert markdown, "expected the answer to stream into the transcript"
    # All updates target one replaceable slot, and the final text is authoritative.
    assert all(event.replace_key == "turn.answer" for event in markdown)
    assert markdown[-1].payload == "Hello **world**!"
    # No raw writes: streaming stays inside the semantic slot, not the byte sink.
    assert not any(item[0] == "write" for item in sink.events if isinstance(item, tuple))


def test_streaming_without_managed_sink_prints_raw_and_declines_finalize():
    display = TurnDisplay(verbosity="normal", status_line=False)
    with console.capture() as cap:
        display.token("Hello, ")
        display.token("world")
    out = cap.get()

    assert "Hello, world" in out
    # Nothing streamed into a managed slot, so the caller must render the answer.
    assert display.finalize_answer("Hello, world") is False
    assert display.streamed_text == "Hello, world"


def test_resolve_verbosity_precedence_quiet_over_verbose_over_config():
    settings = SimpleNamespace(display=SimpleNamespace(verbosity="verbose"))
    assert resolve_verbosity(settings, quiet=True, verbose=True) == "quiet"
    assert resolve_verbosity(settings, verbose=True) == "verbose"
    assert resolve_verbosity(settings) == "verbose"
    assert resolve_verbosity(SimpleNamespace(display=SimpleNamespace(verbosity="bogus"))) == "normal"
    assert resolve_verbosity(SimpleNamespace()) == "normal"


def test_the_turn_state_follows_the_work_instead_of_staying_at_planning():
    """A turn's footer state must track what the turn is doing.

    In incident 599a725b the header read "planning" for the whole run, including
    minutes of retries, because the state was written once at submission and not
    again until the turn finished. The status line already knew the real stage;
    only the TUI's copy of it was frozen.
    """
    seen: list[str] = []
    display = TurnDisplay(verbosity="normal", status_line=False, on_stage=seen.append)

    display.begin("planning")
    _capture(display, [
        ("start", {"name": "web_search", "arguments": {"query": "attention is all you need"}}),
        ("done", {"name": "web_search", "output": {"hits": 3}}),
    ])

    assert seen[0] == "planning"
    assert seen[-1] != "planning"
    assert "web_search" in " ".join(seen)


def test_an_unchanged_stage_is_not_republished():
    """The status line re-reads the stage on every spinner tick; the observer
    must fire on change only, or the TUI would be rewritten several times a
    second for a stage that never moved."""
    seen: list[str] = []
    display = TurnDisplay(verbosity="normal", status_line=False, on_stage=seen.append)

    # The constructor already published "planning", so repeating it proves
    # nothing on its own — a moved stage has to be observed first.
    display.begin("retrieving")
    display.begin("retrieving")
    display.begin("retrieving")

    assert seen == ["planning", "retrieving"]


def test_a_failing_status_observer_never_breaks_the_turn():
    """The observer writes to a UI surface that can be torn down mid-turn; a
    cosmetic label must not take the run with it."""
    def explode(_stage: str) -> None:
        raise RuntimeError("tui closed")

    display = TurnDisplay(verbosity="normal", status_line=False, on_stage=explode)
    display.begin("planning")

    out = _capture(display, [("start", {"name": "read_file", "arguments": {"path": "a.md"}})])

    assert "read_file" in out


def test_repeated_identical_errors_coalesce_with_a_count() -> None:
    """Codex holds one active cell; identical retries become ×N, not N rows."""
    timeout = "HTTPSConnectionPool(host='export.arxiv.org'): Read timed out."
    events = [
        ("done", {"name": "http_fetch", "error": timeout, "status": "failed", "duration_ms": 30000})
        for _ in range(5)
    ]
    out = _capture(TurnDisplay(verbosity="normal", status_line=False), events)
    assert out.count("http_fetch") == 1
    assert "×5" in out
    assert out.count("timed out") == 1


def test_nested_relay_does_not_reprint_the_same_error() -> None:
    timeout = "HTTPSConnectionPool(host='export.arxiv.org'): Read timed out."
    out = _capture(
        TurnDisplay(verbosity="normal", status_line=False),
        [
            ("start", {"name": "http_fetch", "arguments": {"url": "https://export.arxiv.org"}}),
            ("done", {"name": "http_fetch", "error": timeout, "status": "failed"}),
            (
                "task_progress",
                {
                    "subtask_id": "exec1",
                    "stage": "workflow.step.tool.done",
                    "name": "http_fetch",
                    "error": timeout,
                    "status": "failed",
                },
            ),
        ],
    )
    assert "http_fetch" in out
    assert out.count("timed out") == 1
    assert "↳" not in out


def test_later_execution_line_keeps_ids_without_repeating_the_error() -> None:
    err = "provider unavailable"
    out = _capture(
        TurnDisplay(verbosity="normal", status_line=False),
        [
            ("done", {"name": "http_fetch", "error": err, "status": "failed"}),
            (
                "task_done",
                {
                    "subtask_id": "execution12345678",
                    "task_id": "task123456789",
                    "skill": "literature-search",
                    "status": "failed",
                    "error": err,
                },
            ),
        ],
    )
    assert out.count(err) == 1
    assert "execution=executio" in out
    assert "task=task1234" in out
    assert "No saved artifact" not in out


def test_verbose_keeps_every_retry_line() -> None:
    timeout = "Read timed out."
    events = [
        ("start", {"name": "http_fetch", "arguments": {"url": "https://x.test"}}),
        ("done", {"name": "http_fetch", "error": timeout, "status": "failed"}),
    ] * 3
    out = _capture(TurnDisplay(verbosity="verbose", status_line=False), events)
    assert "×" not in out
    assert out.count("http_fetch") >= 6
    assert out.count("timed out") == 3


def test_notice_usage_updates_status_token_count() -> None:
    display = TurnDisplay(verbosity="normal", status_line=False)
    display.tool_event("notice", {"kind": "usage", "total_tokens": 12_400, "cost_usd": 0.12})
    assert display._usage_tokens == 12_400
    assert display._usage_cost == 0.12
    plain = display._status_text().plain
    assert "12.4k tok" in plain
    assert "$0.12" in plain


def test_task_progress_usage_updates_status_token_count() -> None:
    display = TurnDisplay(verbosity="normal", status_line=False)
    display.tool_event(
        "task_progress",
        {"stage": "usage", "skill": "research-ideation", "total_tokens": 50, "cost_usd": 0.01},
    )
    assert display._usage_tokens == 50
    plain = display._status_text().plain
    assert "50 tok" in plain
    assert "$0.01" in plain


def test_active_cell_is_a_tool_card_committed_at_end() -> None:
    from omni.cli.repl_output import use_output_sink
    from omni.cli.repl_transcript import TRACE_COMMIT_STATE, TRACE_REPLACE_KEY, TranscriptKind

    class Sink:
        def __init__(self) -> None:
            self.events: list = []

        def write(self, text: str) -> None:
            pass

        def publish_event(self, event) -> None:  # noqa: ANN001
            self.events.append(event)

        def set_status(self, text: str) -> None:
            pass

        def clear(self) -> None:
            pass

    sink = Sink()
    display = TurnDisplay(verbosity="normal", status_line=False)
    with use_output_sink(sink):
        display.tool_event("done", {"name": "http_fetch", "error": "timeout after 30s"})
        live = [e for e in sink.events if getattr(e, "replace_key", "") == TRACE_REPLACE_KEY]
        assert live
        assert live[-1].state != TRACE_COMMIT_STATE
        display.end()
    committed = [
        e
        for e in sink.events
        if getattr(e, "kind", None) == TranscriptKind.TOOL_CARD
        and e.state == TRACE_COMMIT_STATE
    ]
    assert committed
    assert "http_fetch" in str(committed[-1].payload)
    assert "timeout after 30s" in str(committed[-1].payload)
