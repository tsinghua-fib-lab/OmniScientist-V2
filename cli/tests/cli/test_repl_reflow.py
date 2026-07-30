from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from rich.cells import cell_len

from omni.cli import main as cli_main
from omni.cli.live_display import TurnDisplay
from omni.cli.render import console, data_table, info
from omni.cli.repl_output import (
    TRANSCRIPT_PROTOCOL_ENV,
    TranscriptWireDecoder,
    encode_transcript_event,
    use_output_sink,
    use_output_turn,
)
from omni.cli.repl_tui import (
    DataTableData,
    ReplTui,
    TranscriptEvent,
    TranscriptKind,
    TranscriptModel,
)
from omni.cli.runner import task_ack_cb


@dataclass
class _EventSink:
    events: list[TranscriptEvent]

    def publish_event(self, event: TranscriptEvent) -> None:
        self.events.append(event)

    def write(self, text: str) -> None:
        raise AssertionError(f"structured output fell back to static text: {text!r}")

    def set_status(self, text: str) -> None:
        del text

    def clear(self) -> None:
        self.events.clear()


def _activity_event() -> TranscriptEvent:
    return TranscriptEvent(
        kind=TranscriptKind.DATA_TABLE,
        payload=DataTableData(
            title="activity",
            columns=("#", "event", "actor", "status", "workflow", "execution", "pct", "detail"),
            rows=(
                (
                    "12",
                    "assistant.message",
                    "assistant",
                    "succeeded",
                    "research",
                    "execution.finished",
                    "100%",
                    "中文结果 🔬 /Users/demo/artifacts/figure/RAG系统架构图.png",
                ),
            ),
            layout="activity",
        ),
    )


def test_plain_paths_remain_one_logical_line_at_every_viewport_width():
    path = "/Users/antonio/.omni/workspaces/demo/artifacts/figure/RAG系统架构图：Query、Retriever、Reranker、LLM.png"
    model = TranscriptModel()
    model.publish(TranscriptEvent(kind=TranscriptKind.PLAIN_TEXT, payload=path + "\n"))

    for width in (60, 80, 120, 180):
        rendered = model.render(width)
        assert path in rendered.text
        assert "artifacts/fi\ngure" not in rendered.text


def test_activity_table_has_wide_medium_and_narrow_responsive_layouts():
    model = TranscriptModel()
    model.publish(_activity_event())

    narrow = model.render(60).text
    medium = model.render(80).text
    wide = model.render(180).text

    assert "#12 · assistant.message · succeeded" in narrow
    assert "workflow: research" in narrow
    assert "workflow" not in medium.splitlines()[1].lower()
    assert "execution.finished" in medium
    assert all(cell_len(line) <= 80 for line in medium.splitlines())
    assert all(column in wide for column in _activity_event().payload.columns)
    assert all(cell_len(line) <= 180 for line in wide.splitlines())


def test_render_cache_is_width_aware_and_resize_reflow_is_bounded():
    model = TranscriptModel(reflow_row_budget=12)
    for index in range(40):
        model.publish(
            TranscriptEvent(
                kind=TranscriptKind.MARKDOWN,
                payload=f"**entry {index}** 中文内容 🔬 " + ("word " * 20),
            )
        )

    first = model.render(80)
    anchor = model.anchor_for_offset(first.spans[15].start)
    resized = model.render(180, anchor_entry_id=anchor.entry_id)

    assert model.last_reflow_count < len(model.entries)
    assert (anchor.entry_id, 180, True, "default") in model.cache_keys
    assert resized.entry_at(model.offset_for_anchor(anchor)).entry_id == anchor.entry_id


def test_data_table_and_console_text_publish_structured_events_without_hard_wraps():
    sink = _EventSink(events=[])
    path = "/Users/demo/.omni/workspaces/project/artifacts/figure/一个很长的科研架构图.png"

    with use_output_sink(sink):
        data_table(
            "activity",
            ["#", "event", "status"],
            [["1", "execution.finished", "succeeded"]],
            layout="activity",
        )
        console.print(path)

    assert [event.kind for event in sink.events] == [
        TranscriptKind.DATA_TABLE,
        TranscriptKind.PLAIN_TEXT,
    ]
    assert sink.events[0].payload.layout == "activity"
    assert sink.events[1].payload == path + "\n"


def test_repl_startup_and_help_publish_semantic_default_collapsed_tables():
    from omni.cli.main import _show_repl_help, _show_repl_quickstart
    from omni.config.settings import ModelCfg

    sink = _EventSink(events=[])
    with use_output_sink(sink):
        _show_repl_quickstart(
            ModelCfg(
                provider="openai",
                base_url="https://example.invalid/v1",
                api_key="test",
                model="test-model",
            )
        )
        _show_repl_help()

    command_tables = [
        event
        for event in sink.events
        if isinstance(event.payload, DataTableData)
        and event.payload.title
        in {"Common commands and subcommands", "Interactive mode commands"}
    ]
    assert [event.payload.title for event in command_tables] == [
        "Common commands and subcommands",
        "Interactive mode commands",
    ]
    for event in command_tables:
        assert event.kind == TranscriptKind.DATA_TABLE
        assert event.foldable is True
        assert event.initially_collapsed is True
        assert event.payload.layout == "commands"
        assert event.payload.row_styles[:3] == (
            "bold cyan",
            "bold cyan",
            "bold cyan",
        )


def test_foldable_table_collapses_without_losing_its_semantic_source():
    rows = tuple((f"/command-{index}", "details " * 8) for index in range(30))
    event = TranscriptEvent(
        kind=TranscriptKind.DATA_TABLE,
        payload=DataTableData(
            title="Long help",
            columns=("command", "description"),
            rows=rows,
        ),
        foldable=True,
        initially_collapsed=True,
    )
    model = TranscriptModel()
    model.publish(event)

    default = model.render(80).text
    expanded = model.render(80, expanded_state=True).text
    collapsed = model.render(80, expanded_state=False).text

    assert "Ctrl+T to expand" in default
    assert "/command-29" in expanded
    assert "Ctrl+T to expand" in collapsed
    assert len(collapsed.splitlines()) < len(expanded.splitlines())
    assert model.entries == (event,)


def test_expand_override_reflows_foldable_history_beyond_the_row_budget():
    model = TranscriptModel(reflow_row_budget=1)
    for block in range(3):
        model.publish(
            TranscriptEvent(
                kind=TranscriptKind.PLAIN_TEXT,
                payload="\n".join(
                    f"block-{block}-line-{line}" for line in range(20)
                )
                + "\n",
                foldable=True,
                initially_collapsed=True,
            )
        )

    assert model.render(80).text.count("Ctrl+T to expand") == 3
    expanded = model.render(80, expanded_state=True).text
    assert "Ctrl+T to expand" not in expanded
    assert all(f"block-{block}-line-10" in expanded for block in range(3))


def test_long_raw_output_is_foldable_but_expanded_by_default():
    model = TranscriptModel()
    model.append("\n".join(f"raw-{index}" for index in range(30)) + "\n", raw=True)

    event = model.entries[0]
    assert event.foldable is True
    assert event.initially_collapsed is False
    assert "raw-20" in model.render(80).text
    assert "Ctrl+T to expand" not in model.render(80).text


def test_transcript_wire_decoder_preserves_structured_events_and_raw_output():
    base = _activity_event()
    event = TranscriptEvent(
        kind=base.kind,
        payload=DataTableData(
            title=base.payload.title,
            columns=base.payload.columns,
            rows=base.payload.rows,
            layout=base.payload.layout,
            row_styles=("bold cyan",),
        ),
        turn_id="turn-42",
        replace_key="plan.summary",
        state="planning",
        foldable=True,
        initially_collapsed=True,
    )
    wire = encode_transcript_event(event)
    decoder = TranscriptWireDecoder()

    first = decoder.feed(wire[:17])
    second = decoder.feed(wire[17:] + b"third-party output\n")
    tail = decoder.finish()

    assert first == []
    assert second[0] == event
    assert second[1].kind == TranscriptKind.RAW_COMMAND_OUTPUT
    assert second[1].payload == "third-party output\n"
    assert tail == []


def test_late_output_is_rendered_inside_its_original_user_turn() -> None:
    model = TranscriptModel()
    model.publish(
        TranscriptEvent(
            TranscriptKind.USER_MESSAGE,
            "first research question",
            turn_id="turn-1",
        )
    )
    model.publish(
        TranscriptEvent(
            TranscriptKind.USER_MESSAGE,
            "queued follow-up",
            turn_id="turn-2",
        )
    )
    model.publish(
        TranscriptEvent(TranscriptKind.MARKDOWN, "first answer", turn_id="turn-1")
    )

    rendered = model.render(80).text
    assert rendered.index("first research question") < rendered.index("first answer")
    assert rendered.index("first answer") < rendered.index("queued follow-up")


def test_user_turn_is_separated_from_surrounding_output_by_a_blank_gutter() -> None:
    """Each user prompt gets a plain blank line above and below (Codex
    ``UserHistoryCell`` parity) so it never sits flush against the previous
    turn's output, and the gutter stays terminal-background — never the user
    row's full-width grey block."""
    model = TranscriptModel()
    model.publish(
        TranscriptEvent(TranscriptKind.USER_MESSAGE, "first question", turn_id="t1")
    )
    model.publish(TranscriptEvent(TranscriptKind.MARKDOWN, "first answer\n", turn_id="t1"))
    model.publish(
        TranscriptEvent(TranscriptKind.USER_MESSAGE, "second question", turn_id="t2")
    )

    rendered = model.render(80)
    text = rendered.text
    lines = text.splitlines()

    # The first prompt has no leading blank (nothing precedes it)...
    assert text.startswith("› first question")
    # ...but a blank line separates the previous answer from the next prompt.
    prompt_line = next(i for i, ln in enumerate(lines) if ln.startswith("› second question"))
    assert lines[prompt_line - 1].strip() == ""
    assert lines[prompt_line - 2].strip() == "first answer"
    # The separator row is an empty-style span, so the viewport paints it as the
    # terminal background instead of the user turn's grey block.
    gutter = text.index("› second question") - 1
    assert text[gutter] == "\n"
    assert rendered.entry_at(gutter).style == ""


def test_replace_keys_update_turn_headers_and_plan_state_without_duplicates() -> None:
    model = TranscriptModel()
    model.publish(
        TranscriptEvent(
            TranscriptKind.USER_MESSAGE,
            "review this paper",
            turn_id="turn-1",
            replace_key="turn.header",
            state="planning",
        )
    )
    model.publish(
        TranscriptEvent(
            TranscriptKind.USER_MESSAGE,
            "review this paper",
            turn_id="turn-1",
            replace_key="turn.header",
            state="needs input",
        )
    )
    model.publish(
        TranscriptEvent(
            TranscriptKind.STATUS,
            "proposed plan\n",
            turn_id="turn-1",
            replace_key="plan.summary",
        )
    )
    model.publish(
        TranscriptEvent(
            TranscriptKind.STATUS,
            "validated plan\n",
            turn_id="turn-1",
            replace_key="plan.summary",
        )
    )

    rendered = model.render(80).text
    assert rendered.count("review this paper") == 1
    assert "[needs input]" in rendered
    assert "[planning]" not in rendered
    assert "proposed plan" not in rendered
    assert rendered.count("validated plan") == 1


def test_transcript_budget_evicts_complete_old_turns() -> None:
    model = TranscriptModel(max_chars=52)
    for turn_id, question, answer in (
        ("old", "old question", "old answer with evidence\n"),
        ("new", "new question", "new answer with evidence\n"),
    ):
        model.publish(
            TranscriptEvent(TranscriptKind.USER_MESSAGE, question, turn_id=turn_id)
        )
        model.publish(TranscriptEvent(TranscriptKind.MARKDOWN, answer, turn_id=turn_id))

    rendered = model.render(80).text
    assert "old question" not in rendered
    assert "old answer" not in rendered
    assert "new question" in rendered
    assert "new answer" in rendered


def test_info_is_semantic_status_and_task_ack_remains_transient() -> None:
    class Sink(_EventSink):
        status = ""

        def set_status(self, text: str) -> None:
            self.status = text

    sink = Sink(events=[])
    with use_output_sink(sink):
        info("searching the literature corpus")
        task_ack_cb(False)({"task_id": "1234567890"})

    assert [event.kind for event in sink.events] == [TranscriptKind.STATUS]
    # Payload now carries Rich markup so the transcript can colour it; the
    # message text (with markup escaped) is preserved verbatim.
    assert sink.events[0].payload == "[cyan]·[/cyan] searching the literature corpus\n"
    assert sink.status == "planning · task 12345678"


@pytest.mark.asyncio
async def test_live_plan_updates_collapse_to_one_summary_in_the_user_turn() -> None:
    tui = ReplTui(commands=())
    assert tui.accept_text("检索 RAG 论文并生成架构图")
    submission = await tui.read_submission_async()
    display = TurnDisplay(verbosity="normal", status_line=False)

    with use_output_sink(tui), use_output_turn(submission.turn_id):
        display.tool_event(
            "plan",
            {
                "event_type": "plan.boundary.selected",
                "name": "workflow",
                "summary": "multi-stage request",
            },
        )
        display.tool_event(
            "plan",
            {
                "event_type": "plan.model.proposed",
                "name": "research workflow",
                "summary": "search then figure",
            },
        )
        display.tool_event(
            "plan",
            {
                "event_type": "plan.validated",
                "name": "research workflow",
                "payload": {"steps": ["literature.search", "artifact.figure"]},
            },
        )

    summaries = [
        event
        for event in tui.transcript.entries
        if event.turn_id == submission.turn_id and event.replace_key == "plan.summary"
    ]
    assert len(summaries) == 1
    assert "literature.search" in str(summaries[0].payload)
    assert "multi-stage request" not in tui.transcript.text


@pytest.mark.asyncio
async def test_child_commands_receive_live_terminal_size_and_structured_protocol(monkeypatch):
    tui = ReplTui(commands=())
    monkeypatch.setattr(tui, "terminal_size", lambda: (52, 177))
    event = TranscriptEvent(kind=TranscriptKind.PLAIN_TEXT, payload="new-width output\n")

    class _Stream:
        def __init__(self) -> None:
            self._chunks = iter((encode_transcript_event(event), b""))

        async def read(self, _size: int) -> bytes:
            return next(self._chunks)

    class _Process:
        stdout = _Stream()

        async def wait(self) -> int:
            return 0

    async def fake_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        assert args[:3] == (cli_main.sys.executable, "-m", "omni.cli.main")
        assert kwargs["env"]["COLUMNS"] == "177"
        assert kwargs["env"]["LINES"] == "52"
        assert kwargs["env"][TRANSCRIPT_PROTOCOL_ENV] == "1"
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await cli_main._stream_repl_external_command(tui, ["task", "show", "abc"]) == 0
    assert tui.transcript.entries[-1] == event
