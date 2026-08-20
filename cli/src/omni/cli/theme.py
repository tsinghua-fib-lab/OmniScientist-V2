"""One semantic palette shared by every terminal surface.

Colours are named ANSI slots, not hex literals. Codex states the reason plainly
in ``codex-rs/tui/styles.md`` — "Avoid custom colors because there's no
guarantee that they'll contrast well or look good in various terminal color
themes" — and enforces it by disallowing ``Color::Rgb`` in ``clippy.toml``. omni
had drifted the other way: two prompt_toolkit style dicts carried Solarized
literals (``#839496``, ``#657b83``, ``#6c6c6c``) picked against one background,
so on any other background the hints dissolved into it.

Call sites should name a *role*, never a colour:

``ACCENT``   what the user can act on — commands, keys, the selected row.
``SUCCESS``  a healthy or completed outcome.
``DANGER``   a failure the user has to read.
``CAUTION``  a degraded state the turn survived.
``MUTED``    supporting prose that must not compete with the value beside it.

The rule that decides which of two adjacent spans gets colour: colour marks what
the reader can *do* or must *notice*, dim marks what merely labels it. A value is
never dimmer than its own label, which is how the workspace path and the guide
line had become unreadable.

The one sanctioned exception is a surface painting its own background (the
approval modal): fixed foregrounds are required there because the contrast is
against our colour rather than the terminal's.
"""

from __future__ import annotations

import re

# Rich markup names. Rich maps these onto the terminal's own ANSI slots.
ACCENT = "cyan"
SUCCESS = "green"
DANGER = "red"
CAUTION = "yellow"
MUTED = "dim"
STRONG = "bold"

# Muted is the ``dim`` *attribute*, never a grey colour. This matters more than
# it looks: a grey is a bet that the user's background is far enough from it, and
# ``ansibrightblack`` loses that bet on any theme whose background sits near it —
# the text does not merely lose contrast, it disappears. ``dim`` modifies the
# default foreground, so its worst case is "not actually dimmed", which is still
# readable. Codex reaches the same rule from the other direction: "Secondary
# text: use `dim`".
PTK_MUTED = "dim"

# Accents carry ``nodim`` because prompt_toolkit merges a container's style into
# its fragments: the footer and composer windows are themselves muted, so a bare
# ``ansicyan`` inside them would render as dimmed cyan.
PTK_ACCENT = "nodim ansicyan"
PTK_SUCCESS = "nodim ansigreen"
PTK_DANGER = "nodim ansired"
PTK_CAUTION = "nodim ansiyellow"
PTK_STRONG = "nodim bold"
PTK_TEXT = "nodim"  # the terminal's default foreground, at full strength


def completion_menu_styles() -> dict[str, str]:
    """prompt_toolkit classes for the slash popup's name + description columns.

    Catalog help is already attached as ``display_meta``; these roles keep the
    description readable without betting on a grey that can vanish into the
    terminal background.
    """
    return {
        "completion-menu": PTK_TEXT,
        "completion-menu.completion": PTK_TEXT,
        "completion-menu.completion.current": f"reverse {PTK_ACCENT}",
        "completion-menu.meta.completion": PTK_MUTED,
        "completion-menu.meta.completion.current": "reverse",
    }


# ``Ctrl+J``, ``Enter``, ``Esc`` … the part of a hint the user actually presses.
_KEY = re.compile(r"^(\s*)(Ctrl\+\S+|Alt\+\S+|Shift\+\S+|Enter|Tab|Esc|Space)(\s.*)?$", re.DOTALL)


def split_hint(part: str) -> tuple[str, str, str]:
    """Split ``"Ctrl+J newline"`` into leading space, the key, and its label.

    The three pieces always concatenate back to ``part``. That is a requirement,
    not a nicety: callers hand this text *after* width-clipping it, and the rule
    drawn beside the hints is measured against the same width, so swallowing
    even a trailing space would shorten the strip by a character.

    An empty key means the part is a status rather than a binding
    (``"auto mode"``, ``"select to copy"``), which callers style as prose.
    """
    match = _KEY.match(part)
    if match is None:
        return "", "", part
    lead, key, label = match.groups()
    return lead, key, label or ""


def hint_fragments(
    text: str,
    *,
    key_class: str,
    label_class: str,
    separator: str = " · ",
) -> list[tuple[str, str]]:
    """Style a ``" · "``-joined hint strip so the keys carry the colour.

    Codex renders its footer hints uniformly dim, which works against its own
    palette but leaves one flat grey strip with no scanning cue. Keep Codex's
    structure — key, then label — and spend the accent on the key the way its
    modal tips bold theirs, so the eye lands on what to press.
    """
    fragments: list[tuple[str, str]] = []
    for index, part in enumerate(text.split(separator)):
        if index:
            fragments.append((label_class, separator))
        lead, key, label = split_hint(part)
        for style, chunk in ((label_class, lead), (key_class, key), (label_class, label)):
            if chunk:
                fragments.append((style, chunk))
    return fragments
