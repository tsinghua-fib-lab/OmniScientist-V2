"""Canonical short-id display and git-style leading-prefix resolution."""

from __future__ import annotations

SHORT_ID_MIN_LENGTH = 8


def short_id(value: str | None, minimum: int = SHORT_ID_MIN_LENGTH) -> str:
    """Return the leading display prefix for an identifier."""
    return str(value or "")[: max(1, minimum)]


def shortest_unique_prefixes(
    values: list[str],
    minimum: int = SHORT_ID_MIN_LENGTH,
) -> dict[str, str]:
    """Return git-style unique leading abbreviations of at least ``minimum`` chars."""
    candidates = list(dict.fromkeys(str(value) for value in values if value))
    out: dict[str, str] = {}
    for value in candidates:
        length = min(len(value), max(1, minimum))
        while length < len(value):
            prefix = value[:length]
            if sum(candidate.startswith(prefix) for candidate in candidates) == 1:
                break
            length += 1
        out[value] = value[:length]
    return out
