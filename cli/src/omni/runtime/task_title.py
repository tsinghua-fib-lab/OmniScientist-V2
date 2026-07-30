"""Short, human task titles — not a clipped copy of the whole prompt.

Bundle directories and host-written drafts used to inherit
``clip(user_input, 80)`` or ``user_message[:240] + " Draft"``. A figure-plus-paper
request then produced a folder and filename that repeated the entire instruction.
This module keeps the durable title to a goal-sized phrase (theme + genre).
"""

from __future__ import annotations

import re

from omni.storage.artifacts import slugify_filename

SHORT_TITLE_MAX = 24

# User-input matchers (please/help-me/prepare-materials). Written as escapes so
# the control-plane English-only scan stays clean; behavior is unchanged.
_LEADING = re.compile(
    r"^(?:"
    r"\u8bf7(?:\u5e2e\u6211|\u5e2e\u5fd9)?|"
    r"\u5e2e\u6211|"
    r"\u9ebb\u70e6|"
    r"\u4e3a\u4e86?|"
    r"(?:Please|Write|Create|Generate|Prepare|Make)(?:\s+a(?:n)?)?"
    r")\s+",
    re.IGNORECASE,
)
_GOAL_SUFFIX = re.compile(
    r"(?:\u51c6\u5907\u6750\u6599|\u6750\u6599\u51c6\u5907|\u51c6\u5907)$"
)
_CLAUSE_SEPS = ("\uff0c\u5e76", "\u5e76\u751f\u6210", " and ", ", and ")


def short_task_title(user_input: str, *, max_len: int = SHORT_TITLE_MAX) -> str:
    """Return a short title for a turn (colon-left goal, or theme + genre)."""
    text = " ".join((user_input or "").split())
    if not text:
        return "Untitled task"
    text = _goal_clause(text)
    text = _LEADING.sub("", text).strip(" ，,。.")
    text = _first_clause(text, max_len=max_len)
    text = _GOAL_SUFFIX.sub("", text).strip(" ，,。.")
    text = _limit(text, max_len)
    return text or "Untitled task"


def manuscript_basename(title: str) -> str:
    """Bare markdown name for a host-written paper (no prompt slug, no Draft)."""
    stem = slugify_filename(title, max_len=SHORT_TITLE_MAX) or "manuscript"
    return f"{stem}.md"


def _goal_clause(text: str) -> str:
    for sep in ("：", ": "):
        if sep in text:
            head, _, _ = text.partition(sep)
            head = head.strip()
            if 2 <= len(head) <= 40:
                return head
    if ":" in text:
        head, _, _ = text.partition(":")
        head = head.strip()
        if 2 <= len(head) <= 40 and not (head[-1:].isdigit()):
            return head
    return text


def _first_clause(text: str, *, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    for sep in _CLAUSE_SEPS:
        left, _, _ = text.partition(sep)
        if 4 <= len(left) <= max_len:
            return left.strip()
    return text


def _limit(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip("-_ …。，,.")


__all__ = ["SHORT_TITLE_MAX", "manuscript_basename", "short_task_title"]
