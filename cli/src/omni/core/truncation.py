"""Codex-style middle truncation with an original-length marker.

Codex keeps a prefix and a suffix of oversized tool output and names what was
removed (``…N chars truncated…``), then wraps the snippet with the original
token count and line count. Hook spill additionally reserves the recovery-path
footer *before* shrinking the preview, so the path cannot push the result over
the budget. Omni uses the same shape on a character budget so
``compact_observation`` can still promise ``len(result) <= max_chars``.
"""

from __future__ import annotations

APPROX_BYTES_PER_TOKEN = 4
# Marker plus a few head/tail characters. The Codex warning header is only
# added when this much body still fits; otherwise the snippet would be just
# the header.
_MIN_BODY_CHARS = 32


def approx_token_count(text: str) -> int:
    """Codex ``approx_token_count``: ceil(UTF-8 bytes / 4)."""
    n = len(text.encode("utf-8"))
    return (n + APPROX_BYTES_PER_TOKEN - 1) // APPROX_BYTES_PER_TOKEN


def approx_tokens_from_byte_count(byte_count: int) -> int:
    return (max(0, byte_count) + APPROX_BYTES_PER_TOKEN - 1) // APPROX_BYTES_PER_TOKEN


def truncate_middle_chars(text: str, max_chars: int) -> str:
    """Keep head and tail; mark how many characters were removed.

    Codex ``truncate_middle_chars`` adds the marker on top of the budget. This
    port reserves the marker first so the result fits ``max_chars``.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    reserved = len(_char_marker(len(text)))
    body = max_chars - reserved
    if body <= 0:
        marker = _char_marker(len(text))
        return marker if len(marker) <= max_chars else marker[:max_chars]
    left, right = body // 2, body - body // 2
    removed, prefix, suffix = _split_string(text, left, right)
    result = f"{prefix}{_char_marker(removed)}{suffix}"
    return result if len(result) <= max_chars else result[:max_chars]


def formatted_truncate_text(text: str, max_chars: int, *, footer: str = "") -> str:
    """Head/tail truncate and stamp the original length, optionally plus a footer.

    Codex ``formatted_truncate_text`` writes ``original token count`` and
    ``Total output lines``. The footer is reserved first, matching hook spill
    in ``output_spill.rs``.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars and not footer:
        return text
    if footer and len(text) + len(footer) <= max_chars:
        return f"{text}{footer}"
    reserved_footer = footer if len(footer) < max_chars else ""
    budget = max_chars - len(reserved_footer)
    tokens = approx_token_count(text)
    lines = len(text.splitlines())
    header = (
        f"Warning: truncated output (original token count: {tokens})\n"
        f"Total output lines: {lines}\n\n"
    )
    short_header = (
        f"Warning: truncated output (original token count: {tokens}; lines: {lines})\n"
    )
    if budget - len(header) >= _MIN_BODY_CHARS:
        return f"{header}{truncate_middle_chars(text, budget - len(header))}{reserved_footer}"
    if budget - len(short_header) >= _MIN_BODY_CHARS:
        return (
            f"{short_header}"
            f"{truncate_middle_chars(text, budget - len(short_header))}"
            f"{reserved_footer}"
        )
    return f"{truncate_middle_chars(text, budget)}{reserved_footer}"


def _char_marker(removed: int) -> str:
    return f"…{removed} chars truncated…"


def _split_string(text: str, beginning: int, end: int) -> tuple[int, str, str]:
    n = len(text)
    if n == 0:
        return 0, "", ""
    prefix_end = min(max(0, beginning), n)
    suffix_start = max(n - max(0, end), 0)
    if suffix_start < prefix_end:
        suffix_start = prefix_end
    prefix = text[:prefix_end]
    suffix = text[suffix_start:]
    return n - len(prefix) - len(suffix), prefix, suffix


__all__ = [
    "APPROX_BYTES_PER_TOKEN",
    "approx_token_count",
    "approx_tokens_from_byte_count",
    "formatted_truncate_text",
    "truncate_middle_chars",
]
