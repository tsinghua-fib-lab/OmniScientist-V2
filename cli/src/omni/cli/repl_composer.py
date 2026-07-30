"""Shared multiline input behavior for Omni's interactive terminal surfaces."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import FilterOrBool
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys

SubmitHandler = Callable[[KeyPressEvent], object | None]
ActionObserver = Callable[["ChatAction"], object | None]


class ChatAction(StrEnum):
    """Stable composer actions; physical key bindings are replaceable adapters."""

    SUBMIT = "chat:submit"
    NEWLINE = "chat:newline"

# Modern terminals can distinguish Shift+Enter using either the Kitty keyboard
# protocol or xterm's modifyOtherKeys protocol. The terminal harness requests
# modified-key reporting while the composer owns the TTY. Ctrl+J and backslash
# continuation remain portable even when the host cannot expose modifiers.
_EXTENDED_NEWLINE_SEQUENCES = (
    "\x1b[13;2u",
    "\x1b[13;2:1u",
    "\x1b[13;3u",
    "\x1b[13;3:1u",
    "\x1b[27;2;13~",
    "\x1b[27;3;13~",
)

_EXTENDED_ENTER_SEQUENCES = ("\x1b[13u", "\x1b[13;1u")
_EXTENDED_CONTROL_J_SEQUENCES = (
    "\x1b[106;5u",
    "\x1b[106;5:1u",
    "\x1b[27;5;106~",
    "\x1b[27;5;10~",
)


def _register_extended_key_sequences() -> None:
    for sequence in _EXTENDED_NEWLINE_SEQUENCES:
        ANSI_SEQUENCES[sequence] = Keys.ControlO
    for sequence in _EXTENDED_ENTER_SEQUENCES:
        ANSI_SEQUENCES[sequence] = Keys.ControlM
    for sequence in _EXTENDED_CONTROL_J_SEQUENCES:
        ANSI_SEQUENCES[sequence] = Keys.ControlJ


_register_extended_key_sequences()


def install_multiline_bindings(
    bindings: KeyBindings,
    *,
    submit: SubmitHandler,
    active: FilterOrBool = True,
    on_action: ActionObserver | None = None,
) -> None:
    """Install one consistent compose/send contract on ``bindings``.

    Enter dispatches ``chat:submit``. Ctrl+J, Ctrl+O, Shift+Enter and
    Alt/Option+Enter dispatch ``chat:newline``. A trailing backslash changes
    Enter into the newline action. Ctrl+X Ctrl+E opens ``$VISUAL``/``$EDITOR``.
    """

    def dispatch(action: ChatAction, event: KeyPressEvent) -> object | None:
        effective = action
        continued = _consume_line_continuation(event.current_buffer)
        if action is ChatAction.SUBMIT and continued:
            effective = ChatAction.NEWLINE
        if on_action is not None:
            on_action(effective)
        if effective is ChatAction.NEWLINE:
            event.current_buffer.newline(copy_margin=False)
            return None
        return submit(event)

    @bindings.add("c-j", filter=active, eager=True)
    @bindings.add("c-o", filter=active, eager=True)
    @bindings.add("escape", "enter", filter=active, eager=True)
    @bindings.add("escape", "c-j", filter=active, eager=True)
    def insert_newline(event: KeyPressEvent) -> None:
        dispatch(ChatAction.NEWLINE, event)

    @bindings.add("c-x", "c-e", filter=active, eager=True)
    def open_in_editor(event: KeyPressEvent) -> None:
        event.current_buffer.open_in_editor(validate_and_handle=False)

    @bindings.add("enter", filter=active, eager=True)
    def submit_or_continue(event: KeyPressEvent) -> object | None:
        return dispatch(ChatAction.SUBMIT, event)


def _consume_line_continuation(buffer: Buffer) -> bool:
    """Remove one unescaped trailing backslash when Enter means continuation."""
    if buffer.cursor_position != len(buffer.text):
        return False
    before_cursor = buffer.document.text_before_cursor
    backslash_count = len(before_cursor) - len(before_cursor.rstrip("\\"))
    if backslash_count % 2 == 0:
        return False
    buffer.delete_before_cursor(count=1)
    return True
