"""Stage normalization, milestone compression, and the 3-line status region.

These pin the CLI-readability behavior: an un-retrofitted skill's free-form
progress is spoken in one shared vocabulary (via ``stage_map``), a completed
stage collapses to a single durable milestone line, and the dynamic status
region carries the item under work plus a long-turn heartbeat.
"""

from __future__ import annotations

from omni.cli.live_display import TurnDisplay
from omni.cli.render import console


def _capture(display: TurnDisplay, events: list[tuple[str, dict]]) -> str:
    with console.capture() as cap:
        for phase, data in events:
            display.tool_event(phase, data)
        display.end()
    return cap.get()


def test_an_unretrofitted_skill_speaks_the_shared_vocabulary() -> None:
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            ("task_progress", {"subtask_id": "s", "skill": "openalex-search",
                               "stage": "querying OpenAlex", "pct": 0.3}),
            ("task_progress", {"subtask_id": "s", "skill": "openalex-search",
                               "stage": "done", "pct": 1.0}),
        ],
    )
    # The raw stage is normalized to a clean live label...
    assert "searching OpenAlex" in out
    assert "querying OpenAlex" not in out
    # ...and the terminal stage compresses to one durable milestone line.
    assert "✓" in out
    assert "Literature search complete" in out


def test_a_native_milestone_renders_its_stats_inline() -> None:
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            ("task_progress", {
                "subtask_id": "s",
                "skill": "openalex-search",
                "stage_id": "literature.done",
                "milestone": "Literature search complete",
                "stats": {"found": 126, "kept": 20},
                "pct": 1.0,
            }),
        ],
    )
    assert "Literature search complete · found 126 · kept 20" in out


def test_the_status_region_shows_the_item_under_work() -> None:
    display = TurnDisplay(verbosity="normal", status_line=False)
    display.begin("planning")
    display.tool_event(
        "task_progress",
        {"subtask_id": "s", "skill": "x", "stage": "analyzing",
         "current": "Zero-shot prediction of cellular responses", "pct": 0.5},
    )
    plain = display._status_text().plain
    assert "current: Zero-shot prediction of cellular responses" in plain
    # Still a single "task " occurrence contract is irrelevant here (no task id),
    # but the region must stay within its three-line cap.
    assert len(plain.splitlines()) <= 3


def test_a_milestone_clears_the_current_item() -> None:
    display = TurnDisplay(verbosity="normal", status_line=False)
    display.begin("planning")
    display.tool_event(
        "task_progress",
        {"subtask_id": "s", "skill": "scientific-figure", "stage": "render graphviz",
         "current": "component 3", "pct": 0.35},
    )
    assert "current: component 3" in display._status_text().plain
    display.tool_event(
        "task_progress",
        {"subtask_id": "s", "skill": "scientific-figure", "stage": "done", "pct": 1.0},
    )
    assert "current:" not in display._status_text().plain


def test_the_heartbeat_appears_only_after_a_turn_has_run_long() -> None:
    display = TurnDisplay(status_line=False)
    display.begin("planning")
    assert "recent progress" not in display._status_text().plain

    # Pretend the turn started a minute ago and has since gone quiet.
    display._started -= 60.0
    display._last_progress_at = display._started
    plain = display._status_text().plain
    assert "recent progress" in plain
    assert len(plain.splitlines()) <= 3


def test_the_diagnostic_layer_is_hidden_by_default_and_shown_with_debug() -> None:
    budget = ("budget", {"reason": "max_tool_calls", "budget": {"tool_calls": "8/8"}})

    normal = _capture(TurnDisplay(verbosity="normal", status_line=False), [budget])
    # L1/L3: the operator always learns the budget was reached...
    assert "configured tool-call limit reached" in normal
    # ...but the raw budget snapshot (L4) stays out of the normal view.
    assert "tool_calls=8/8" not in normal

    debugged = _capture(
        TurnDisplay(verbosity="normal", status_line=False, debug=True), [budget]
    )
    assert "configured tool-call limit reached" in debugged
    assert "tool_calls=8/8" in debugged


def test_verbose_still_implies_the_diagnostic_layer() -> None:
    budget = ("budget", {"reason": "max_tool_calls", "budget": {"tool_calls": "8/8"}})
    out = _capture(TurnDisplay(verbosity="verbose", status_line=False), [budget])
    # --debug is a separate switch, but the widest verbosity still reveals L4.
    assert "tool_calls=8/8" in out


def test_internal_workflow_stages_are_left_untouched() -> None:
    """The normalizer must not rewrite the display's own workflow vocabulary."""
    display = TurnDisplay(verbosity="normal", status_line=False)
    out = _capture(
        display,
        [
            ("task_start", {"subtask_id": "t", "skill": "workflow"}),
            ("task_progress", {"subtask_id": "t", "stage": "workflow.start",
                               "total_steps": 2, "goal": "survey RAG"}),
        ],
    )
    assert "workflow" in out
    assert "2 step(s)" in out
