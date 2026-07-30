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
# modified-key reporting (``CSI > 4 ; 2 m``) while the composer owns the TTY —
# after that, Ctrl/Shift/Alt chords arrive as CSI sequences instead of legacy
# C0 bytes. Ctrl+J and backslash continuation remain portable even when the
# host cannot expose modifiers.
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

def _control_chord_sequences(code: int) -> tuple[str, ...]:
    """xterm modifyOtherKeys + Kitty CSI-u encodings for one Ctrl+key."""
    return (
        f"\x1b[27;5;{code}~",
        f"\x1b[{code};5u",
        f"\x1b[{code};5:1u",
    )


def _emacs_control_chords() -> tuple[tuple[Keys, int], ...]:
    """Every Ctrl+A–Z letter code → prompt_toolkit ``Keys.Control*``.

    Omni enables modifyOtherKeys while it owns the TTY. After that, bash-style
    chords (Ctrl+U kill-to-bol, Ctrl+A bol, Ctrl+E eol, Ctrl+K kill-to-eol,
    Ctrl+W word-rubout, …) arrive as CSI sequences instead of C0 bytes. Mapping
    the full alphabet — not a hand-picked subset — keeps prompt_toolkit's
    built-in emacs/readline bindings working and prevents the Vt100 parser from
    falling apart into ``Escape`` + literal ``[27;5;117~``.
    """
    out: list[tuple[Keys, int]] = []
    for letter in "abcdefghijklmnopqrstuvwxyz":
        key = getattr(Keys, f"Control{letter.upper()}", None)
        if key is not None:
            out.append((key, ord(letter)))
    return tuple(out)


def _register_extended_key_sequences() -> None:
    for sequence in _EXTENDED_NEWLINE_SEQUENCES:
        ANSI_SEQUENCES[sequence] = Keys.ControlO
    for sequence in _EXTENDED_ENTER_SEQUENCES:
        ANSI_SEQUENCES[sequence] = Keys.ControlM
    for sequence in _EXTENDED_CONTROL_J_SEQUENCES:
        ANSI_SEQUENCES[sequence] = Keys.ControlJ
    for key, code in _emacs_control_chords():
        for sequence in _control_chord_sequences(code):
            ANSI_SEQUENCES[sequence] = key


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

    An open completion menu takes precedence over submit: Enter confirms the
    highlighted candidate and leaves the user editing.
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
        # A highlighted completion owns Enter first (see accept_highlighted_completion).
        if accept_highlighted_completion(event.current_buffer):
            return None
        return dispatch(ChatAction.SUBMIT, event)


def accept_highlighted_completion(buffer: Buffer) -> bool:
    """Confirm a highlighted completion; ``True`` when the key was consumed.

    prompt_toolkit previews a highlighted completion straight into the buffer, so
    the text already *looks* accepted while ``complete_state`` is still waiting
    for confirmation. prompt_toolkit ships no default Enter-accepts-completion
    binding, so without this the composer's Enter sends that preview as a
    message — and the user picked a candidate precisely in order to keep typing,
    which is the whole point of a mid-sentence ``@`` mention or of a slash
    command that still needs arguments.

    Only a *highlighted* completion is intercepted. A menu that is merely open
    with nothing selected must let Enter submit, otherwise a fully typed path
    could only be sent after dismissing the popup first.
    """
    state = buffer.complete_state
    completion = state.current_completion if state is not None else None
    if completion is None:
        return False
    buffer.apply_completion(completion)
    # Continue the user's sentence for them, except after a directory: a trailing
    # ``/`` means they are still descending into the tree.
    if not completion.text.endswith("/") and buffer.document.current_char != " ":
        buffer.insert_text(" ")
    return True


def cancel_completion(buffer: Buffer) -> bool:
    """Dismiss an open completion menu; ``True`` when the key was consumed."""
    if buffer.complete_state is None:
        return False
    buffer.cancel_completion()
    return True


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
