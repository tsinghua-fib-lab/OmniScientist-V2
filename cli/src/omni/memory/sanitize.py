"""Redact obvious secrets before free text enters *durable* memory.

Applied on the distillation paths (session extraction, compaction summaries,
notebook flush) — not on the raw transcript, which the user already sees. The
goal is to avoid silently persisting an API key / token into ``memory_entries``
or a curated ``MEMORY.md`` where it would resurface in future prompts.
"""

from __future__ import annotations

import re

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "[REDACTED]"),
    (re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"), "bearer [REDACTED]"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|api[_-]?secret|access[_-]?token|client[_-]?secret|"
            r"secret|password|passwd|pwd|token)\b(\s*[:=]\s*)(['\"]?)([A-Za-z0-9._\-/+]{8,})\3"
        ),
        r"\1\2\3[REDACTED]\3",
    ),
]


def redact_secrets(text: str) -> str:
    """Return ``text`` with obvious credentials replaced by ``[REDACTED]``."""
    if not text:
        return text
    out = text
    for pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    return out


def contains_secret(text: str) -> bool:
    return any(pattern.search(text or "") for pattern, _ in _RULES)
