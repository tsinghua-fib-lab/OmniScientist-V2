"""Keep tool policy out of the distilled user profile.

``profile.md`` is preference, not a capability denylist. A leftover
retrieve-only experiment that said "never use write_file" rewrote a later
produce turn into chat-only (afb9228d). Codex keeps apply_patch / exec
availability in sandbox config, not MEMORY.md.
"""

from __future__ import annotations

import re

# First-class tools a profile must not ban. Capability lives in settings /
# approval / the plan contract.
_TOOL_NAMES = (
    "write_file",
    "edit_file",
    "bash",
    "run_compute",
    "run_skill",
    "run_workflow",
    "spawn_subagents",
    "exec_command",
    "apply_patch",
)

_TOOL_ALT = "|".join(re.escape(name) for name in _TOOL_NAMES)
_BAN_LINE = re.compile(
    rf"(?:禁止使用|禁止调用|不要使用|勿用|never use|do not use|don't use|ban)\s*"
    rf"(?:the\s+)?(?:tools?\s+)?"
    rf"(?:{_TOOL_ALT})"
    rf"|(?:{_TOOL_ALT})\s*(?:is\s+)?(?:forbidden|banned|prohibited)",
    re.IGNORECASE,
)
# A mixed preference bullet: keep "prefer Chinese", drop only the ban clause.
_BAN_CLAUSE = re.compile(
    rf"[；;,]?\s*"
    rf"(?:禁止使用|禁止调用|不要使用|勿用|never use|do not use|don't use|ban)\s*"
    rf"(?:the\s+)?(?:tools?\s+)?"
    rf"(?:{_TOOL_ALT})[^。.\n]*[。.]?",
    re.IGNORECASE,
)


def is_tool_capability_ban(text: str) -> bool:
    """True when *text* forbids a named first-class tool."""
    return bool(_BAN_LINE.search(str(text or "")))


def strip_tool_capability_bans(text: str) -> str:
    """Drop tool-name bans; keep the rest of a mixed preference bullet."""
    raw = str(text or "")
    if not raw.strip():
        return raw
    kept: list[str] = []
    for line in raw.splitlines():
        if is_tool_capability_ban(line):
            cleaned = _BAN_CLAUSE.sub("", line).rstrip("；;，, ")
            if len(cleaned.lstrip("-•*· \t")) < 4:
                continue
            kept.append(cleaned)
            continue
        kept.append(line)
    # Collapse leftover blank runs from removed bullets.
    out: list[str] = []
    blank = False
    for line in kept:
        if not line.strip():
            if blank:
                continue
            blank = True
            out.append("")
            continue
        blank = False
        out.append(line)
    return "\n".join(out).strip()
