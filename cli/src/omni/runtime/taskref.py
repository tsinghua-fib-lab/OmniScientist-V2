"""Parse explicit task identifiers without classifying natural language.

Commands are parsed by the command layer. A task id found in ordinary prose is
only a context reference; the semantic planner still decides what the user
wants to do with that task. Keeping this module language-neutral prevents an IM
message from being silently rewritten because it happened to contain a word
from one language-specific verb list.
"""

from __future__ import annotations

import re

_TASK_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(?=[A-Za-z0-9_-]{6,40}(?![A-Za-z0-9_-]))"
    r"(?=[A-Za-z0-9_-]*\d)"
    r"([A-Za-z0-9][A-Za-z0-9_-]{5,39})"
    r"(?![A-Za-z0-9_-])"
)
_TASK_LOOKUP_COMMAND_RE = re.compile(
    r"^/?tasks?\s+(?:show|status)\s+(?P<id>[A-Za-z0-9][A-Za-z0-9_-]{5,39})\s*$",
    re.IGNORECASE,
)


def extract_task_ids(text: str) -> list[str]:
    """Return candidate task ids (alnum-ish, 6–40 chars, containing a digit)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _TASK_ID_RE.finditer(text or ""):
        value = m.group(1)
        if value.lower() not in seen:
            seen.add(value.lower())
            out.append(value)
    return out


def is_task_lookup(text: str) -> bool:
    """Return whether ``text`` is an explicit task lookup command."""
    return bool(_TASK_LOOKUP_COMMAND_RE.fullmatch((text or "").strip()))


def is_task_reference(text: str) -> bool:
    """Return whether ordinary input contains an explicit task identifier."""
    return bool(extract_task_ids(text or "")) and not is_task_lookup(text)


def is_bare_task_id(text: str) -> bool:
    """Return whether ``text`` is only a task/execution identifier, nothing else.

    A generative request that *mentions* an id must keep going through the
    planner. A message that is the id itself is a status lookup: WeChat users
    paste what the ACK named, the way ``/task show`` works on the CLI.
    """
    value = (text or "").strip()
    if not value or value.startswith("/"):
        return False
    ids = extract_task_ids(value)
    return len(ids) == 1 and ids[0] == value
