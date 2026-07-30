from __future__ import annotations

from contextlib import nullcontext

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from omni.cli.repl_input import ReplInputBox, _display_width


def test_repl_input_box_falls_back_outside_a_tty():
    box = ReplInputBox(enabled=False)

    assert box.read_line(mode="auto", fallback=lambda: "fallback") == "fallback"


def test_repl_input_box_frame_and_toolbar_reflect_mode(monkeypatch):
    monkeypatch.setattr("omni.cli.repl_input._terminal_width", lambda: 72)
    box = ReplInputBox(enabled=False)
    box._mode = "plan"

    message = "".join(fragment for _style, fragment in box._prompt_message())
    toolbar = "".join(fragment for _style, fragment in box._bottom_toolbar())

    assert message.startswith("╭")
    assert "\n│ › " in message
    assert toolbar.startswith("╰─ plan mode")
    assert "Ctrl+C cancel" in toolbar
    assert len(toolbar) >= 72


def test_repl_input_box_uses_compact_footer_on_narrow_terminals(monkeypatch):
    monkeypatch.setattr("omni.cli.repl_input._terminal_width", lambda: 30)
    box = ReplInputBox(enabled=False)

    toolbar = "".join(fragment for _style, fragment in box._bottom_toolbar())

    assert "auto mode · Enter send" in toolbar
    assert "Ctrl+C" not in toolbar


def test_repl_input_box_uses_the_shared_multiline_composer():
    box = ReplInputBox(enabled=False)
    session = box._ensure_session()

    assert session.default_buffer.multiline() is True


def test_repl_input_box_advertises_the_portable_newline_shortcut(monkeypatch):
    monkeypatch.setattr("omni.cli.repl_input._terminal_width", lambda: 112)
    box = ReplInputBox(enabled=False)

    toolbar = "".join(fragment for _style, fragment in box._bottom_toolbar())

    assert "Enter send" in toolbar
    assert "Ctrl+J newline" in toolbar


def test_repl_input_box_never_exceeds_very_narrow_terminal(monkeypatch):
    monkeypatch.setattr("omni.cli.repl_input._terminal_width", lambda: 8)
    box = ReplInputBox(enabled=False)

    toolbar = "".join(fragment for _style, fragment in box._bottom_toolbar())

    assert _display_width(toolbar) == 8


def test_repl_input_box_status_is_responsive_and_display_width_safe(monkeypatch):
    monkeypatch.setattr("omni.cli.repl_input._terminal_width", lambda: 140)
    box = ReplInputBox(enabled=False)
    box._mode = "review"
    box.update_status(
        model="openai/deepseek-v4-pro",
        focus="RAG 工程架构图",
        context_tokens=12_400,
        context_window=32_768,
        clearable_tokens=8_200,
    )
    box.set_last_elapsed(3.25)

    toolbar = "".join(fragment for _style, fragment in box._bottom_toolbar())

    assert "review mode" in toolbar
    assert "model openai/deepseek-v4-pro" in toolbar
    assert "focus RAG 工程架构图" in toolbar
    assert "ctx 12.4k/32.8k" in toolbar
    assert "/clear saves 8.2k" in toolbar
    assert "last 3.2s" in toolbar
    assert _display_width(toolbar) == 140


def test_repl_input_box_completes_top_level_slash_commands():
    box = ReplInputBox(enabled=False, commands=("/task", "/context", "/clear"))
    event = CompleteEvent(completion_requested=True)

    completions = list(box._completer.get_completions(Document("/c"), event))
    # The completer inserts the bare name after the already-typed "/" (start
    # position replaces only the partial word), so "/c" -> "/clear"/"/context".
    assert [completion.text for completion in completions] == ["clear", "context"]
    assert all(completion.start_position == -1 for completion in completions)
    # Only the slash surface triggers completion, never an ordinary prompt.
    assert list(box._completer.get_completions(Document("ask /c"), event)) == []
    # A names-only catalog (legacy string list) exposes no subcommands, so a
    # trailing space yields nothing rather than dumping every command again.
    assert list(box._completer.get_completions(Document("/task "), event)) == []


@pytest.mark.asyncio
async def test_repl_input_box_uses_async_prompt_inside_the_repl(monkeypatch):
    calls: list[str] = []

    class Session:
        async def prompt_async(self) -> str:
            calls.append("prompt_async")
            return "hello"

    box = ReplInputBox(enabled=True)
    monkeypatch.setattr(box, "_ensure_session", lambda: Session())
    monkeypatch.setattr(box, "_print_footer", lambda: calls.append("footer"))
    monkeypatch.setattr("omni.cli.repl_input.patch_stdout", lambda **_kwargs: nullcontext())

    result = await box.read_line_async(mode="review", fallback=lambda: "fallback")

    assert result == "hello"
    assert calls == ["prompt_async", "footer"]
