from __future__ import annotations

from types import SimpleNamespace

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput

from omni.cli.repl_composer import ChatAction, install_multiline_bindings


def _invoke(
    bindings: KeyBindings,
    keys: tuple[Keys, ...],
    buffer: Buffer,
) -> None:
    matches = bindings.get_bindings_for_keys(keys)
    assert matches, f"no binding registered for {keys!r}"
    matches[-1].handler(SimpleNamespace(current_buffer=buffer))


@pytest.mark.parametrize(
    "keys",
    [
        (Keys.ControlJ,),
        (Keys.ControlO,),
        (Keys.Escape, Keys.ControlM),
        (Keys.Escape, Keys.ControlJ),
    ],
)
def test_multiline_shortcuts_insert_newline_without_submitting(keys: tuple[Keys, ...]):
    submitted: list[str] = []
    bindings = KeyBindings()
    install_multiline_bindings(
        bindings,
        submit=lambda event: submitted.append(event.current_buffer.text),
    )
    buffer = Buffer(multiline=True)
    buffer.insert_text("first line")

    _invoke(bindings, keys, buffer)

    assert buffer.text == "first line\n"
    assert submitted == []


def test_shortcuts_dispatch_semantic_chat_actions() -> None:
    actions: list[ChatAction] = []
    submitted: list[str] = []
    bindings = KeyBindings()
    install_multiline_bindings(
        bindings,
        submit=lambda event: submitted.append(event.current_buffer.text),
        on_action=actions.append,
    )
    buffer = Buffer(multiline=True)
    buffer.insert_text("first")

    _invoke(bindings, (Keys.ControlJ,), buffer)
    buffer.insert_text("second")
    _invoke(bindings, (Keys.ControlM,), buffer)

    assert actions == [ChatAction.NEWLINE, ChatAction.SUBMIT]
    assert submitted == ["first\nsecond"]


def test_enter_submits_multiline_text_intact():
    submitted: list[str] = []
    bindings = KeyBindings()
    install_multiline_bindings(
        bindings,
        submit=lambda event: submitted.append(event.current_buffer.text),
    )
    buffer = Buffer(multiline=True)
    buffer.insert_text("first line\nsecond line")

    _invoke(bindings, (Keys.ControlM,), buffer)

    assert submitted == ["first line\nsecond line"]


def test_backslash_enter_is_a_portable_line_continuation():
    submitted: list[str] = []
    bindings = KeyBindings()
    install_multiline_bindings(
        bindings,
        submit=lambda event: submitted.append(event.current_buffer.text),
    )
    buffer = Buffer(multiline=True)
    buffer.insert_text("first line\\")

    _invoke(bindings, (Keys.ControlM,), buffer)

    assert buffer.text == "first line\n"
    assert submitted == []


def test_escaped_trailing_backslash_is_submitted_literally():
    submitted: list[str] = []
    bindings = KeyBindings()
    install_multiline_bindings(
        bindings,
        submit=lambda event: submitted.append(event.current_buffer.text),
    )
    buffer = Buffer(multiline=True)
    buffer.insert_text("literal\\\\")

    _invoke(bindings, (Keys.ControlM,), buffer)

    assert submitted == ["literal\\\\"]


def test_external_editor_binding_edits_without_submitting(monkeypatch):
    submitted: list[str] = []
    opened: list[bool] = []
    bindings = KeyBindings()
    install_multiline_bindings(
        bindings,
        submit=lambda event: submitted.append(event.current_buffer.text),
    )
    buffer = Buffer(multiline=True)
    monkeypatch.setattr(
        buffer,
        "open_in_editor",
        lambda *, validate_and_handle=False: opened.append(validate_and_handle),
    )

    _invoke(bindings, (Keys.ControlX, Keys.ControlE), buffer)

    assert opened == [False]
    assert submitted == []


@pytest.mark.parametrize("sequence", ["\x1b[13;2u", "\x1b[27;2;13~"])
def test_common_shift_enter_protocols_map_to_newline(sequence: str):
    presses = []
    parser = Vt100Parser(presses.append)

    parser.feed_and_flush(sequence)

    assert [press.key for press in presses] == [Keys.ControlO]


@pytest.mark.asyncio
async def test_bracketed_multiline_paste_is_one_submission():
    bindings = KeyBindings()

    def submit(event) -> None:  # noqa: ANN001
        event.current_buffer.validate_and_handle()

    install_multiline_bindings(bindings, submit=submit)
    with create_pipe_input() as pipe_input:
        session: PromptSession[str] = PromptSession(
            input=pipe_input,
            output=DummyOutput(),
            multiline=True,
            key_bindings=bindings,
        )
        prompt = session.prompt_async()
        pipe_input.send_text("\x1b[200~first line\nsecond line\x1b[201~\r")

        result = await prompt

    assert result == "first line\nsecond line"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "newline_input",
    [
        "\x0a",  # Ctrl+J, Claude Code's portable newline shortcut.
        "\x0f",  # Ctrl+O.
        "\x1b\r",  # Alt/Option+Enter on CR terminals.
        "\x1b\n",  # Alt/Option+Enter on LF terminals.
        "\x1b[13;2u",  # Kitty/CSI-u Shift+Enter.
        "\x1b[27;2;13~",  # xterm modifyOtherKeys Shift+Enter.
        "\\\r",  # Portable backslash continuation on CR terminals.
        "\\\n",  # Portable backslash continuation on LF terminals.
    ],
)
async def test_terminal_shortcuts_compose_one_multiline_submission(newline_input: str):
    bindings = KeyBindings()

    def submit(event) -> None:  # noqa: ANN001
        event.current_buffer.validate_and_handle()

    install_multiline_bindings(bindings, submit=submit)
    with create_pipe_input() as pipe_input:
        session: PromptSession[str] = PromptSession(
            input=pipe_input,
            output=DummyOutput(),
            multiline=True,
            key_bindings=bindings,
        )
        prompt = session.prompt_async()
        pipe_input.send_text(f"first line{newline_input}second line\r")

        result = await prompt

    assert result == "first line\nsecond line"
