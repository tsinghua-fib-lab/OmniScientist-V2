"""Width-aware chrome for the REPL dock and classic input box.

Codex keeps instructional chrome on one row: drop whole hint tokens first,
then ellipsize, and never silently slice a key in half. Paths keep a head and
a basename around a single ``…``. This module is pure rendering — it does not
decide which footer mode is active or mutate agent/skill state.
"""

from __future__ import annotations

from collections.abc import Sequence

from prompt_toolkit.utils import get_cwidth

_HINT_SEP = " · "

COMPOSER_PLACEHOLDER = "Send a message  ·  / commands  ·  ! shell  ·  Ctrl+J newline"
COMPOSER_PLACEHOLDER_SHIFT = "Send a message  ·  / commands  ·  ! shell  ·  Shift+Enter newline"
COMPOSER_PLACEHOLDER_MEDIUM = "Send a message  ·  /  ·  !"
COMPOSER_PLACEHOLDER_NARROW = "Send a message"


def newline_hint(shift_enter_ready: bool) -> str:
    """Footer token for inserting a newline.

    Advertise Shift+Enter only when the host can distinguish it from Enter.
    Ctrl+J remains the portable fallback and still works in every mode.
    """
    return "Shift+Enter newline" if shift_enter_ready else "Ctrl+J newline"


def composer_placeholder(*, shift_enter_ready: bool = False) -> str:
    """Wide composer hint, capability-aware for the newline key."""
    return COMPOSER_PLACEHOLDER_SHIFT if shift_enter_ready else COMPOSER_PLACEHOLDER


def display_width(text: str) -> int:
    """Terminal columns occupied by ``text`` (East-Asian width, not ``len``)."""
    return sum(get_cwidth(char) for char in text)


def clip_display(text: str, width: int) -> str:
    """Cut ``text`` to ``width`` columns, admitting the loss with ``…``.

    A silent slice is worse than a short message: the reader cannot tell a
    sentence that ended from one that was cut. Codex and opencode both append
    an ellipsis; Classic's toolbar already did the same.
    """
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    return _take_prefix(text, width - 1) + "…"


def fit_hint_parts(
    parts: Sequence[str],
    width: int,
    *,
    drop_order: Sequence[str] = (),
) -> list[str]:
    """Fit a `` · ``-joined hint strip into ``width`` columns.

    Whole tokens listed in ``drop_order`` are removed first (leftmost listed
    goes first). If the strip still overflows, the first remaining part — the
    mode or status label — is clipped so operational keys stay intact. A key
    is never split into ``Ctrl+``.
    """
    kept = [part for part in parts if part]
    if width <= 0:
        return []
    if _joined_width(kept) <= width:
        return kept

    for name in drop_order:
        if name in kept and len(kept) > 1:
            kept.remove(name)
            if _joined_width(kept) <= width:
                return kept

    if kept:
        rest = kept[1:]
        reserved = _joined_width(rest)
        sep = display_width(_HINT_SEP) if rest else 0
        budget = width - reserved - sep
        if budget >= 1:
            kept[0] = clip_display(kept[0], budget)
            if _joined_width(kept) <= width:
                return kept
        elif rest:
            return fit_hint_parts(rest, width, drop_order=drop_order)

    while len(kept) > 1 and _joined_width(kept) > width:
        kept.pop()
    if kept and _joined_width(kept) > width:
        return [clip_display(kept[0], width)]
    return kept


def center_truncate_path(path: str, max_width: int) -> str:
    """Keep a path's head and basename, inserting one ``…`` when it cannot fit.

    Mirrors Codex ``center_truncate_path`` in spirit: the leaf stays readable
    so a long workspace path still names the project. Non-paths (no separator)
    fall through to ordinary display-width clipping.
    """
    if max_width <= 0:
        return ""
    if display_width(path) <= max_width:
        return path
    if max_width == 1:
        return "…"
    sep = "/" if "/" in path else ("\\" if "\\" in path else "")
    if not sep:
        return clip_display(path, max_width)
    tail = path.rsplit(sep, 1)[-1]
    ellipsis = "…"
    need = display_width(ellipsis) + display_width(tail)
    if need >= max_width:
        return ellipsis + _take_suffix(tail, max_width - 1)
    return _take_prefix(path, max_width - need) + ellipsis + tail


def placeholder_for_width(width: int, *, shift_enter_ready: bool = False) -> str:
    """Composer hint that stays on one line at ``width`` columns."""
    full = composer_placeholder(shift_enter_ready=shift_enter_ready)
    if width >= display_width(full):
        return full
    if width >= display_width(COMPOSER_PLACEHOLDER_MEDIUM):
        return COMPOSER_PLACEHOLDER_MEDIUM
    return COMPOSER_PLACEHOLDER_NARROW


def compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _joined_width(parts: Sequence[str]) -> int:
    return display_width(_HINT_SEP.join(parts)) if parts else 0


def _take_prefix(text: str, width: int) -> str:
    if width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for char in text:
        char_width = get_cwidth(char)
        if used + char_width > width:
            break
        out.append(char)
        used += char_width
    return "".join(out)


def _take_suffix(text: str, width: int) -> str:
    if width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for char in reversed(text):
        char_width = get_cwidth(char)
        if used + char_width > width:
            break
        out.append(char)
        used += char_width
    return "".join(reversed(out))
