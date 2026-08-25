"""Absolute paths the user typed in the current message.

An ``@`` mention is an explicit attachment. A bare absolute directory in
the same sentence is the same consent for *this turn*: ``对标 … 源码目录
/Users/…/sourcecode`` must be listable without falling back to bash.
Writes stay on the write jail + approval gate.
"""

from __future__ import annotations

import re
from pathlib import Path

from omni.core.path_lookup import resolve_existing_path

# Unix absolute path, or Windows drive path. Stop before whitespace / quotes /
# CJK grouping punctuation that often wraps a "source directory" hint.
_ABS_PATH = re.compile(
    r"(?P<path>"
    r"/(?:[^\s\"'`，,;:）)】>\u3002]+)"
    r"|[A-Za-z]:\\(?:[^\s\"'`，,;:）)】>\u3002]+)"
    r"|~/(?:[^\s\"'`，,;:）)】>\u3002]+)"
    r")"
)
_TRAILING = "\"'`，,;:.)）】>"


def iter_named_absolute_paths(text: str) -> list[Path]:
    """Existing absolute (or ``~/``) paths mentioned in *text*, de-duplicated."""
    raw = str(text or "")
    if not raw:
        return []
    out: list[Path] = []
    seen: set[Path] = set()
    for match in _ABS_PATH.finditer(raw):
        token = match.group("path").rstrip(_TRAILING)
        if len(token) < 2:
            continue
        resolved = resolve_existing_path(token)
        if resolved is None:
            try:
                candidate = Path(token).expanduser()
            except (OSError, RuntimeError):
                continue
            if not candidate.exists():
                continue
            try:
                resolved = candidate.resolve()
            except (OSError, RuntimeError):
                continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out
