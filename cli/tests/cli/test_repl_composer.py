from __future__ import annotations

from types import SimpleNamespace

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.completion import Completion
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput

from omni.cli.repl_composer import (
    ChatAction,
    cancel_completion,
    install_multiline_bindings,
)


def _invoke(
    bindings: KeyBindings,
    keys: tuple[Keys, ...],
    buffer: Buffer,
) -> None:
    matches = bindings.get_bindings_for_keys(keys)
    assert matches, f"no binding registered for {keys!r}"
    matches[-1].handler(SimpleNamespace(current_buffer=buffer))


def _menu_buffer(text: str, completion: str, start_position: int) -> Buffer:
    """A buffer whose completion menu is open, with nothing highlighted yet."""
    buffer = Buffer(multiline=True)
    buffer.insert_text(text)
    buffer.complete_state = CompletionState(
        original_document=buffer.document,
        completions=[Completion(completion, start_position=start_position)],
    )
    return buffer


def test_enter_confirms_a_highlighted_completion_instead_of_submitting() -> None:
    """Regression: picking a candidate must not send the line.

    prompt_toolkit previews a highlighted completion straight into the buffer, so
    ``review @README.md`` already appeared complete while the menu still awaited
    confirmation — and the composer's eager Enter shipped that preview as a
    message. Since prompt_toolkit has no default Enter-accepts-completion binding,
    a user who picked a file could never keep typing, which is the entire point of
    a mid-sentence ``@`` mention.
    """
    submitted: list[str] = []
    bindings = KeyBindings()
    install_multiline_bindings(
        bindings, submit=lambda event: submitted.append(event.current_buffer.text)
    )
    buffer = _menu_buffer("review @READ", "README.md", -4)
    buffer.go_to_completion(0)
    assert buffer.text == "review @README.md"  # the preview the user sees

    _invoke(bindings, (Keys.ControlM,), buffer)

    assert submitted == []
    assert buffer.complete_state is None
    # A trailing space so the sentence can simply continue.
    assert buffer.text == "review @README.md "

    buffer.insert_text("and draw a diagram")
    _invoke(bindings, (Keys.ControlM,), buffer)
    assert submitted == ["review @README.md and draw a diagram"]


def test_enter_still_submits_when_nothing_is_highlighted() -> None:
    """A merely-open menu must not swallow Enter.

    With ``complete_while_typing`` the popup is visible for most of what the user
    types. If any open menu blocked submit, a fully typed path could only be sent
    after dismissing the popup first.
    """
    submitted: list[str] = []
    bindings = KeyBindings()
    install_multiline_bindings(
        bindings, submit=lambda event: submitted.append(event.current_buffer.text)
    )
    buffer = _menu_buffer("review @README.md", "README.md", -9)
    assert buffer.complete_state.current_completion is None

    _invoke(bindings, (Keys.ControlM,), buffer)

    assert submitted == ["review @README.md"]


def test_confirming_a_directory_keeps_the_path_open_for_navigation() -> None:
    """No trailing space after ``/``: the user is still descending the tree."""
    bindings = KeyBindings()
    install_multiline_bindings(bindings, submit=lambda event: None)
    buffer = _menu_buffer("@cor", "corpus/", -3)
    buffer.go_to_completion(0)

    _invoke(bindings, (Keys.ControlM,), buffer)

    assert buffer.text == "@corpus/"


def test_cancel_completion_reports_whether_it_consumed_the_key() -> None:
    """Esc must only be taken from its owner while a menu is actually open."""
    buffer = _menu_buffer("review @READ", "README.md", -4)
    assert cancel_completion(buffer) is True
    assert buffer.complete_state is None
    assert cancel_completion(buffer) is False


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


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ("\x1b[27;5;99~", Keys.ControlC),   # xterm modifyOtherKeys Ctrl+C
        ("\x1b[99;5u", Keys.ControlC),      # Kitty CSI-u Ctrl+C
        ("\x1b[27;5;100~", Keys.ControlD),  # Ctrl+D (EOF)
        ("\x1b[100;5u", Keys.ControlD),
        ("\x1b[27;5;116~", Keys.ControlT),  # Ctrl+T (fold)
        ("\x1b[116;5u", Keys.ControlT),
        ("\x1b[27;5;108~", Keys.ControlL),  # Ctrl+L (redraw)
        ("\x1b[27;5;105~", Keys.ControlI),  # Ctrl+I / Tab (queue)
        ("\x1b[27;5;117~", Keys.ControlU),  # Ctrl+U (bash unix-line-discard)
        ("\x1b[117;5u", Keys.ControlU),
        ("\x1b[27;5;97~", Keys.ControlA),   # Ctrl+A (beginning-of-line)
        ("\x1b[27;5;101~", Keys.ControlE),  # Ctrl+E (end-of-line)
        ("\x1b[27;5;107~", Keys.ControlK),  # Ctrl+K (kill-line)
        ("\x1b[27;5;119~", Keys.ControlW),  # Ctrl+W (unix-word-rubout)
    ],
)
def test_modify_other_keys_control_chords_map_to_legacy_keys(
    sequence: str, expected: Keys
) -> None:
    """After Omni enables modifyOtherKeys, Ctrl chords must not leak as literals.

    Regression: unmapped ``CSI 27 ; 5 ; 99 ~`` was parsed as Escape + the
    characters ``[27;5;99~``, which filled the composer and made subsequent
    Enter submit noise instead of the user's real input. The full Ctrl+A–Z
    alphabet is registered so bash/readline chords (Ctrl+U/A/E/K/W, …) keep
    hitting prompt_toolkit's emacs bindings instead of inserting CSI junk.
    """
    presses = []
    parser = Vt100Parser(presses.append)

    parser.feed_and_flush(sequence)

    assert [press.key for press in presses] == [expected]


def test_all_ctrl_letter_modify_other_keys_sequences_are_mapped() -> None:
    """Every Ctrl+A–Z modifyOtherKeys encoding must resolve to a single key."""
    from omni.cli.repl_composer import _control_chord_sequences, _emacs_control_chords

    for key, code in _emacs_control_chords():
        for sequence in _control_chord_sequences(code):
            presses = []
            Vt100Parser(presses.append).feed_and_flush(sequence)
            assert [press.key for press in presses] == [key], sequence


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
