"""Keep a provider's native tool-call encoding out of assistant prose.

Some DeepSeek builds occasionally serialise a tool call into the assistant
*content* channel using their native sentinel vocabulary instead of the
structured ``tool_calls`` field — reliably so on a tool-free turn, where the
model still wants to call something but has no structured field to put it in.
Nothing above the provider can tell that apart from prose, so it renders
verbatim: incident ``c60c4c85`` showed a user a whole ``update_plan`` call as the
final answer.

Two rules, in this order:

* A *complete* block is a tool call the model meant to make, so recover it.
  Honouring it keeps the loop moving instead of ending the turn on
  markup-as-answer, and matches how ``codex-apply-patch``'s lenient parse
  recovers the action the model intended rather than rejecting the shape it
  arrived in.
* An *incomplete* block is never recovered — arguments cut mid-value would
  invoke a tool with the wrong input — but it is still removed. Either way no
  sentinel survives into content.

The streaming discipline is Codex's, from
``codex-rs/utils/stream-parser/src/inline_hidden_tag.rs``: hide a tag family from
visible text and hand its body back as structured data in the same pass, holding
back any tail that could still be the start of a delimiter so a sentinel split
across two deltas cannot half-leak, and treating an unclosed tag at EOF as
markup rather than flushing it as text.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# These literals use FULL-WIDTH VERTICAL LINE (U+FF5C) and, in the ``tool▁calls``
# family, LOWER ONE EIGHTH BLOCK (U+2581) as the word separator — not ASCII "|"
# and "_", which they closely resemble. They are single tokens to the model, so
# they arrive exactly like this and must be matched byte-for-byte; normalising
# them to ASCII first would also swallow prose that merely looks similar.
_DSML_TOOL_CALLS_OPEN = "<｜｜DSML｜｜tool_calls>"
_DSML_TOOL_CALLS_CLOSE = "</｜｜DSML｜｜tool_calls>"
_DSML_INVOKE_OPEN = "<｜｜DSML｜｜invoke"
_DSML_INVOKE_CLOSE = "</｜｜DSML｜｜invoke>"
_DSML_PARAMETER_OPEN = "<｜｜DSML｜｜parameter"
_DSML_PARAMETER_CLOSE = "</｜｜DSML｜｜parameter>"
_V3_CALLS_OPEN = "<｜tool▁calls▁begin｜>"
_V3_CALLS_CLOSE = "<｜tool▁calls▁end｜>"
_V3_CALL_OPEN = "<｜tool▁call▁begin｜>"
_V3_CALL_CLOSE = "<｜tool▁call▁end｜>"
_V3_SEP = "<｜tool▁sep｜>"

# Region delimiters, widest wrapper first. Only the opener's *pair* is searched
# for once a region is open, so an inner ``invoke`` inside a ``tool_calls``
# wrapper needs no nesting logic; listing the inner forms too means a model that
# omits the wrapper is still handled.
_REGIONS: tuple[tuple[str, str], ...] = (
    (_DSML_TOOL_CALLS_OPEN, _DSML_TOOL_CALLS_CLOSE),
    (_DSML_INVOKE_OPEN, _DSML_INVOKE_CLOSE),
    (_DSML_PARAMETER_OPEN, _DSML_PARAMETER_CLOSE),
    (_V3_CALLS_OPEN, _V3_CALLS_CLOSE),
    (_V3_CALL_OPEN, _V3_CALL_CLOSE),
)

# Sentinels that can survive alone when the model drops the opener that would
# have paired with them. They carry no payload, so they are simply removed.
_ORPHAN_SENTINELS: tuple[str, ...] = (
    _DSML_TOOL_CALLS_CLOSE,
    _DSML_INVOKE_CLOSE,
    _DSML_PARAMETER_CLOSE,
    _V3_CALLS_CLOSE,
    _V3_CALL_CLOSE,
    _V3_SEP,
)

_ALL_TOKENS: tuple[str, ...] = tuple(
    {*(open_ for open_, _ in _REGIONS), *_ORPHAN_SENTINELS}
)

# A one-character tail ("<") is ordinary prose often enough to be worth
# releasing; anything longer that is still only a sentinel prefix ("<｜", "<｜｜D")
# cannot be, so a response that ends mid-sentinel drops the fragment instead of
# showing it.
_MIN_TRUNCATED_SENTINEL_LEN = 2

_DSML_INVOKE_RE = re.compile(
    _DSML_INVOKE_OPEN + r'\s+name="(?P<name>[^"]*)"[^>]*>(?P<body>.*?)' + _DSML_INVOKE_CLOSE,
    re.DOTALL,
)
_DSML_PARAMETER_RE = re.compile(
    _DSML_PARAMETER_OPEN
    + r'\s+name="(?P<name>[^"]*)"(?P<attrs>[^>]*)>(?P<value>.*?)'
    + _DSML_PARAMETER_CLOSE,
    re.DOTALL,
)
_V3_CALL_RE = re.compile(
    _V3_CALL_OPEN + r"(?P<body>.*?)" + _V3_CALL_CLOSE,
    re.DOTALL,
)


@dataclass(slots=True)
class RecoveredToolCall:
    """A tool call read back out of native markup that arrived as content."""

    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class NativeMarkupSplit:
    """What a content string yields once native tool markup is separated out.

    ``stripped`` is true whenever markup was present at all — including the
    unrecoverable case — because a caller needs to distinguish "the model wrote
    nothing" from "the model wrote a call we could not honour".
    """

    content: str
    recovered: list[RecoveredToolCall] = field(default_factory=list)
    stripped: bool = False


def _held_back_len(text: str, token: str) -> int:
    """Length of the longest tail of ``text`` that is a proper prefix of ``token``."""
    for size in range(min(len(text), len(token) - 1), 0, -1):
        if text.endswith(token[:size]):
            return size
    return 0


def _first_region_or_sentinel(text: str) -> tuple[int, str, str | None] | None:
    """Earliest markup token in ``text`` as ``(offset, token, paired_close)``.

    Ties at one offset go to the longest token, so a wrapper is never mistaken
    for a shorter sentinel that starts at the same place.
    """
    best: tuple[int, str, str | None] | None = None
    candidates: list[tuple[str, str | None]] = [
        *((open_, close) for open_, close in _REGIONS),
        *((sentinel, None) for sentinel in _ORPHAN_SENTINELS),
    ]
    for token, close in candidates:
        offset = text.find(token)
        if offset < 0:
            continue
        if best is None or offset < best[0] or (offset == best[0] and len(token) > len(best[1])):
            best = (offset, token, close)
    return best


class NativeToolMarkupFilter:
    """Incremental splitter: prose out one delta at a time, markup captured.

    Feed every content delta through :meth:`push` and call :meth:`finish` once
    the response ends. Text is only released when it can no longer turn out to be
    the beginning of a sentinel, so the same instance is safe for a single
    streamed response and must not be reused across responses.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._open_close: str | None = None
        self._region = ""
        self._captured: list[str] = []

    @property
    def stripped_markup(self) -> bool:
        return bool(self._captured)

    def push(self, chunk: str) -> str:
        """Absorb one content delta and return the part that is safe to show."""
        self._pending += chunk
        visible: list[str] = []
        while True:
            if self._open_close is not None:
                close = self._open_close
                offset = self._pending.find(close)
                if offset >= 0:
                    end = offset + len(close)
                    self._captured.append(self._region + self._pending[:end])
                    self._region = ""
                    self._pending = self._pending[end:]
                    self._open_close = None
                    continue
                # Inside a region nothing is prose, so buffer everything except a
                # tail that may still complete the closing sentinel.
                keep = _held_back_len(self._pending, close)
                cut = len(self._pending) - keep
                if cut > 0:
                    self._region += self._pending[:cut]
                    self._pending = self._pending[cut:]
                break

            hit = _first_region_or_sentinel(self._pending)
            if hit is None:
                keep = max((_held_back_len(self._pending, tok) for tok in _ALL_TOKENS), default=0)
                cut = len(self._pending) - keep
                if cut > 0:
                    visible.append(self._pending[:cut])
                    self._pending = self._pending[cut:]
                break

            offset, token, close = hit
            visible.append(self._pending[:offset])
            self._pending = self._pending[offset + len(token) :]
            if close is None:
                self._captured.append(token)
                continue
            self._open_close = close
            self._region = token
        return "".join(visible)

    def finish(self) -> str:
        """End the response and return the last prose still held back."""
        if self._open_close is not None:
            # The response ended inside a region: the remainder is markup the cap
            # or the connection cut short, never something to render.
            self._captured.append(self._region + self._pending)
            self._region = ""
            self._pending = ""
            self._open_close = None
            return ""
        tail, self._pending = self._pending, ""
        partial = max((_held_back_len(tail, tok) for tok in _ALL_TOKENS), default=0)
        if partial >= _MIN_TRUNCATED_SENTINEL_LEN:
            self._captured.append(tail[len(tail) - partial :])
            return tail[: len(tail) - partial]
        return tail

    def recovered_tool_calls(self) -> list[RecoveredToolCall]:
        """Tool calls readable from everything captured so far."""
        return _parse_captured_markup("".join(self._captured))

    def split(self, visible: str) -> NativeMarkupSplit:
        """Package prose this filter already released with what it captured."""
        return NativeMarkupSplit(
            content=visible,
            recovered=self.recovered_tool_calls(),
            stripped=self.stripped_markup,
        )


def split_native_tool_markup(content: str) -> NativeMarkupSplit:
    """Separate showable prose from native tool markup in a complete content string."""
    markup_filter = NativeToolMarkupFilter()
    visible = markup_filter.push(content) + markup_filter.finish()
    return markup_filter.split(visible)


def _parse_captured_markup(markup: str) -> list[RecoveredToolCall]:
    if not markup:
        return []
    calls = [
        RecoveredToolCall(name=name, arguments=_dsml_arguments(match.group("body")))
        for match in _DSML_INVOKE_RE.finditer(markup)
        if (name := match.group("name").strip())
    ]
    for match in _V3_CALL_RE.finditer(markup):
        parsed = _parse_v3_call(match.group("body"))
        if parsed is not None:
            calls.append(parsed)
    return calls


def _dsml_arguments(body: str) -> dict[str, Any]:
    """Read one ``invoke`` body's parameters into an argument mapping.

    ``string="false"`` is DeepSeek's marker that the value is a JSON literal
    rather than text. A value that then fails to parse is kept as text: a
    wrong-typed argument the tool can reject and report beats a dropped one.
    """
    arguments: dict[str, Any] = {}
    for match in _DSML_PARAMETER_RE.finditer(body):
        # Only the surrounding newlines belong to the tag layout; interior
        # whitespace is part of the value (file contents, indented code).
        raw = match.group("value").strip("\n")
        if 'string="false"' in match.group("attrs"):
            try:
                arguments[match.group("name")] = json.loads(raw.strip())
                continue
            except json.JSONDecodeError:
                logger.debug("[llm] native markup parameter declared JSON but did not parse")
        arguments[match.group("name")] = raw
    return arguments


def _parse_v3_call(body: str) -> RecoveredToolCall | None:
    """Read a ``function<｜tool▁sep｜>name`` + fenced-JSON call, or give up."""
    if _V3_SEP not in body:
        return None
    name_and_args = body.rpartition(_V3_SEP)[2].strip()
    name, _, payload = name_and_args.partition("\n")
    name = name.strip()
    if not name:
        return None
    raw = _strip_code_fence(payload)
    if not raw:
        return RecoveredToolCall(name=name, arguments={})
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return RecoveredToolCall(name=name, arguments=parsed if isinstance(parsed, dict) else {})


def _strip_code_fence(text: str) -> str:
    """Unwrap the ```json fence DeepSeek puts around V3 tool arguments."""
    body = text.strip()
    if not body.startswith("```"):
        return body
    newline = body.find("\n")
    if newline < 0:
        return ""
    body = body[newline + 1 :]
    closing = body.rfind("```")
    return (body[:closing] if closing >= 0 else body).strip()
