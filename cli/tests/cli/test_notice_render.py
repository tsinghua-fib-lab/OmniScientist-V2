"""How a failure and a request for input are told apart on screen.

The reported bug was one missing API key rendered as three paragraphs and two
red crosses, with the command the reader needed sliced off mid-word. These tests
pin the shape that replaced it: one card, a bar carrying the severity, and the
command intact and accented.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from omni.cli import render
from omni.cli.repl_output import (
    TranscriptWireDecoder,
    encode_transcript_event,
    use_output_sink,
)
from omni.cli.repl_transcript import NoticeData, TranscriptEvent, TranscriptModel


@pytest.fixture(autouse=True)
def _shell_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    from omni.cli import command_surface

    monkeypatch.delenv(command_surface.SURFACE_ENV, raising=False)


def _capture(
    monkeypatch: pytest.MonkeyPatch, render_call, *, width: int = 80, styles: bool = False
) -> str:
    console = Console(
        width=width,
        height=60,
        force_terminal=True,
        color_system="truecolor",
        record=True,
    )
    monkeypatch.setattr(render, "console", console)
    monkeypatch.setattr(render, "err_console", console)
    render_call()
    return console.export_text(styles=styles)


_LONG_FAILURE = (
    "Semantic Scholar API key is not configured, and without one Semantic Scholar "
    "rate-limits requests hard enough that the pipeline will almost certainly fail."
)


def test_the_bar_runs_the_whole_height_of_a_wrapped_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rich wraps at print time, one line too late to draw a gutter per line."""
    text = _capture(
        monkeypatch,
        lambda: render.action_card("research-ideation needs input", _LONG_FAILURE),
    )
    body = [line for line in text.splitlines() if line.strip()]
    assert len(body) > 2, "the fixture has to wrap for this test to mean anything"
    assert all("▌" in line for line in body)


def test_a_request_for_input_is_not_dressed_as_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _capture(
        monkeypatch,
        lambda: render.action_card("research-ideation needs input", _LONG_FAILURE),
    )
    failure = _capture(
        monkeypatch,
        lambda: render.error_card("research-ideation failed", _LONG_FAILURE),
    )
    assert "⚠" in action and "✗" not in action
    assert "✗" in failure


def test_the_command_is_accented_so_it_can_be_found_in_the_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hint = "Register it with `/config set research.contact_email a@b.c`."
    plain = _capture(monkeypatch, lambda: render.info(hint))
    # A status line shares its channel with hand-written help, which documents
    # the REPL, so it keeps the canonical form for every reader.
    assert "`/config set research.contact_email a@b.c`" in plain

    styled = _capture(monkeypatch, lambda: render.info(hint), styles=True)
    # Cyan is the accent role: the command carries it, the prose around it does not.
    command = styled.split("Register it with ")[1]
    assert command.startswith("\x1b[36m")


def test_a_card_names_the_prompt_its_reader_is_typing_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A card is generated for one reader, so it may respell what help cannot."""
    plain = _capture(
        monkeypatch,
        lambda: render.error_card(
            "Semantic Scholar is not configured",
            "Register it with `/config set research.semantic_scholar_api_key KEY`.",
        ),
    )
    assert "`omni config set research.semantic_scholar_api_key KEY`" in plain


def test_a_next_action_leads_with_its_command_and_mutes_the_gloss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _capture(
        monkeypatch,
        lambda: render.action_card(
            "research-ideation needs input",
            "",
            actions=("/task show 051fccbb: inspect details, trace, and the result",),
        ),
    )
    assert "omni task show 051fccbb" in text
    assert "inspect details" in text


def test_a_cut_message_says_that_it_was_cut() -> None:
    """The tail a slice takes is exactly where a hint puts its command."""
    assert render.shorten("abcdefghij", 20) == "abcdefghij"
    cut = render.shorten("abcdefghij", 5)
    assert cut.endswith("…")
    assert len(cut) == 5


def test_a_status_line_still_prints_content_that_looks_like_markup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Span-wise escaping has to keep the guarantee whole-string escaping gave."""
    text = _capture(monkeypatch, lambda: render.info("Section [mcp_servers.x] is stale."))
    assert "[mcp_servers.x]" in text


class _RecordingSink:
    """The transcript's stand-in: it keeps events instead of rendering them."""

    def __init__(self) -> None:
        self.events: list[TranscriptEvent] = []

    def publish_event(self, event: TranscriptEvent) -> None:
        self.events.append(event)

    def write(self, text: str) -> None:  # pragma: no cover - never reached
        raise AssertionError("a sink with publish_event is never written to")


def _published(message: str = _LONG_FAILURE, **kwargs: object) -> TranscriptEvent:
    sink = _RecordingSink()
    with use_output_sink(sink):
        render.action_card("research-ideation needs input", message, **kwargs)
    (event,) = sink.events
    return event


def _shown(event: TranscriptEvent, width: int) -> list[str]:
    model = TranscriptModel()
    model.publish(event)
    return [line for line in model.render(width).text.splitlines() if line.strip()]


def test_a_card_is_published_without_deciding_where_its_lines_break() -> None:
    """The producer cannot know the width: off a tty Rich reports 80, always.

    In the REPL the transcript is a prompt_toolkit buffer rather than a
    terminal, so a card laid out at print time was laid out for 80 columns
    however wide the window was — and no resize could repair it, because by
    then the breaks were characters in a string.
    """
    payload = _published().payload
    assert isinstance(payload, NoticeData)
    assert "\n" not in payload.message


def test_a_card_fills_the_window_it_is_shown_in() -> None:
    lines = _shown(_published(), 120)
    assert max(len(line) for line in lines) > 80


def test_a_card_reflows_when_the_window_changes() -> None:
    event = _published()
    model = TranscriptModel()
    model.publish(event)

    def widths(width: int) -> tuple[int, int]:
        lines = [line for line in model.render(width).text.splitlines() if line.strip()]
        return len(lines), max(len(line) for line in lines)

    narrow_lines, narrow_longest = widths(60)
    wide_lines, wide_longest = widths(120)
    # Returning to a width already rendered must not serve the other one's wrap.
    assert widths(60) == (narrow_lines, narrow_longest)

    assert narrow_longest <= 60 and wide_longest <= 120
    assert wide_lines < narrow_lines


def test_every_line_of_a_reflowed_card_still_carries_the_bar() -> None:
    """The gutter is why the card owns its wrap rather than the viewport."""
    for width in (40, 72, 200):
        assert all("▌" in line for line in _shown(_published(), width))


def test_a_card_crossing_the_process_boundary_keeps_its_parts() -> None:
    """A child process publishes over the wire; the parent lays the card out."""
    event = _published(actions=("/task show 051fccbb: inspect the result",))
    decoded = TranscriptWireDecoder().feed(encode_transcript_event(event))
    (restored,) = decoded
    assert restored.payload == event.payload
