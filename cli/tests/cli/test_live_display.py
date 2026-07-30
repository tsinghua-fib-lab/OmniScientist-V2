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
    assert "budget" in out
    assert "max_tool_calls" in out


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
