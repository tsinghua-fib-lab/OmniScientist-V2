"""The shared palette: role vocabulary and hint splitting."""

from __future__ import annotations

import pytest

from omni.cli import theme


@pytest.mark.parametrize(
    "text",
    [
        "",
        "auto mode",
        "Enter send",
        " · Enter send · Ctrl+C cancel ",
        "working · 12s · Enter steer · Tab queue · Esc stop · Ctrl+D exit",
        "auto mode · Enter send · Ctrl+J newline · select to copy · Ctrl+D exit",
    ],
)
def test_hint_fragments_reproduce_their_input_exactly(text: str) -> None:
    """Styling must not resize the strip.

    Callers width-clip the hints and draw a rule alongside them measured against
    that same width, so a swallowed trailing space shortens the frame by one
    character.
    """
    fragments = theme.hint_fragments(text, key_class="class:key", label_class="class:label")

    assert "".join(chunk for _style, chunk in fragments) == text


def test_hint_fragments_colour_keys_and_not_labels() -> None:
    fragments = theme.hint_fragments(
        "Enter send · Ctrl+J newline", key_class="class:key", label_class="class:label"
    )

    assert [chunk for style, chunk in fragments if style == "class:key"] == ["Enter", "Ctrl+J"]
    # Everything that is not a key — labels and the separator — stays muted.
    assert [chunk for style, chunk in fragments if style == "class:label"] == [
        " send",
        " · ",
        " newline",
    ]


@pytest.mark.parametrize(
    ("part", "key"),
    [
        ("Enter send", "Enter"),
        ("Ctrl+J newline", "Ctrl+J"),
        ("Shift+Enter newline", "Shift+Enter"),
        ("Esc stop", "Esc"),
        ("Tab queue", "Tab"),
        ("auto mode", ""),
        ("select to copy", ""),
        ("last 1.2s", ""),
        # A word that merely starts with a key name is prose, not a binding.
        ("Entering plan mode", ""),
    ],
)
def test_split_hint_recognises_bindings_only(part: str, key: str) -> None:
    _lead, found, _label = theme.split_hint(part)

    assert found == key


def test_muted_is_an_attribute_not_a_grey() -> None:
    """The regression that made the composer vanish.

    ``ansibrightblack`` is a bet that the user's background sits far from it. On
    a theme where it does not, muted text does not lose contrast — it disappears
    entirely, taking the input frame and the placeholder with it. ``dim`` acts on
    the default foreground, so its worst case is merely "not dimmed".
    """
    assert theme.PTK_MUTED == "dim"
    assert "ansibrightblack" not in theme.PTK_MUTED


def test_accents_opt_out_of_inherited_dim() -> None:
    """prompt_toolkit merges a container's style into its fragments.

    The footer and composer windows are themselves muted, so an accent without
    ``nodim`` renders as dimmed cyan rather than the cue it is meant to be.
    """
    for role in (
        theme.PTK_ACCENT,
        theme.PTK_SUCCESS,
        theme.PTK_DANGER,
        theme.PTK_CAUTION,
        theme.PTK_STRONG,
        theme.PTK_TEXT,
    ):
        assert role.startswith("nodim")


def test_palette_avoids_hex_literals() -> None:
    """Named ANSI slots inherit the user's terminal theme; hex cannot.

    Codex disallows ``Color::Rgb`` outright for this reason; omni's regression
    was two style dicts full of Solarized values that only suited one background.
    """
    roles = (
        theme.ACCENT,
        theme.SUCCESS,
        theme.DANGER,
        theme.CAUTION,
        theme.MUTED,
        theme.STRONG,
        theme.PTK_ACCENT,
        theme.PTK_SUCCESS,
        theme.PTK_DANGER,
        theme.PTK_CAUTION,
        theme.PTK_MUTED,
    )

    assert all("#" not in role for role in roles)
