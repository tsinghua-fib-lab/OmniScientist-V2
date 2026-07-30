"""Load and bind JavaScript resources shared by poster browser workflows."""

from __future__ import annotations

import json

import poster_core

from posterlib.paths import SKILL_ROOT

_SCRIPT_DIR = SKILL_ROOT / "scripts" / "browser"
_POSTER_SELECTOR_MARKER = "__POSTER_ROOT_SELECTOR__"


def load(filename: str, *, bind_poster_selector: bool = False) -> str:
    """Return one shipped browser script with an optional poster-root binding."""

    script = (_SCRIPT_DIR / filename).read_text(encoding="utf-8")
    if not bind_poster_selector:
        return script
    if script.count(_POSTER_SELECTOR_MARKER) != 1:
        raise RuntimeError(
            "browser script must contain exactly one poster selector marker"
        )
    return script.replace(
        _POSTER_SELECTOR_MARKER,
        json.dumps(poster_core.POSTER_ROOT_SELECTOR, ensure_ascii=False),
    )
