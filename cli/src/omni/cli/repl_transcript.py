"""Structured, width-aware transcript entries for the managed terminal UI."""

from __future__ import annotations

import io
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

# Rendering markup/markdown to a very wide console lets the transcript viewport
# own line wrapping (single source of truth), while Rich still emits SGR colour.
_WIDE = 4096

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

# Select-Graphic-Rendition runs only (colour/style), used to strip trailing
# full-width padding while preserving colour when committing to scrollback.
_SGR_ESCAPE = re.compile(r"\x1b\[[0-9;:]*m")

# The streaming assistant answer replaces itself in place under this key while a
# turn runs (Codex-style live "tail"), then commits once as authoritative
# markdown. Shared with ``live_display`` so both sides agree on the slot name.
ANSWER_REPLACE_KEY = "turn.answer"


class TranscriptKind(StrEnum):
    """Semantic output variants understood by the TUI transcript."""

    PLAIN_TEXT = "plain_text"
    MARKDOWN = "markdown"
    DATA_TABLE = "data_table"
    TOOL_CARD = "tool_card"
    STATUS = "status"
    RAW_COMMAND_OUTPUT = "raw_command_output"
    ERROR = "error"
    USER_MESSAGE = "user_message"


@dataclass(frozen=True)
class DataTableData:
    """Serializable table content that can choose a layout at render time."""

    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    layout: str = "auto"
    row_styles: tuple[str, ...] = ()


@dataclass(frozen=True)
class TranscriptEvent:
    """One semantic item published by an agent, command, or renderer."""

    kind: TranscriptKind
    payload: str | DataTableData
    turn_id: str = ""
    replace_key: str = ""
    state: str = ""
    # Marks the authoritative final render of a ``replace_key`` slot (the
    # streaming answer). The inline dock commits it to native scrollback and
    # stops treating it as a live tail entry.
    final: bool = False
    # Long, stable blocks can be folded by the normal-buffer TUI without losing
    # their semantic source. Producers explicitly mark blocks after deciding
    # that their complete logical-line payload exceeds the fold threshold.
    foldable: bool = False
    # Help-like blocks can start collapsed while other foldable output remains
    # expanded until the user explicitly changes the global Ctrl+T override.
    initially_collapsed: bool = False


@dataclass(frozen=True)
class TranscriptAnchor:
    """Logical location used to preserve the reader's place across reflow."""

    entry_id: int
    fraction: float = 0.0


@dataclass(frozen=True)
class EntrySpan:
    """Character range occupied by one entry in a rendered transcript."""

    entry_id: int
    start: int
    end: int
    style: str = ""


@dataclass(frozen=True)
class StyleRun:
    """A fine-grained styled run inside the rendered transcript text.

    Offsets index into :attr:`TranscriptRender.text`. ``style`` is a
    prompt_toolkit style string (e.g. ``"ansigreen"``, ``"bold"``,
    ``"#f8f8f2 bg:#272822"``) produced by parsing Rich's ANSI output. A run's
    style takes precedence over the entry's base :class:`EntrySpan` style, so a
    single row can carry many colours (Codex-style ``Line = Vec<Span>``).
    """

    start: int
    end: int
    style: str


@dataclass(frozen=True)
class TranscriptRender:
    """Rendered text together with mappings back to semantic entries."""

    text: str
    spans: tuple[EntrySpan, ...]
    truncated_chars: int = 0
    styles: tuple[StyleRun, ...] = ()

    def entry_at(self, offset: int) -> EntrySpan:
        if not self.spans:
            raise IndexError("the transcript is empty")
        position = max(0, min(len(self.text), int(offset)))
        for span in self.spans:
            if span.start <= position < span.end:
                return span
        return self.spans[-1]


@dataclass
class _StoredEntry:
    entry_id: int
    event: TranscriptEvent
    size: int
    last_rendered: str = ""
    last_runs: tuple[StyleRun, ...] = ()
    last_expanded: bool | None = None
    last_theme: str = ""


class TranscriptModel:
    """Thread-safe, bounded semantic transcript with lazy width reflow."""

    def __init__(
        self,
        *,
        max_chars: int = 500_000,
        reflow_row_budget: int = 2_000,
        cache_size: int = 1_024,
    ) -> None:
        self.max_chars = max(1, int(max_chars))
        self.reflow_row_budget = max(1, int(reflow_row_budget))
        self.cache_size = max(8, int(cache_size))
        self._entries: list[_StoredEntry] = []
        self._next_entry_id = 1
        self._source_chars = 0
        self._trimmed_chars = 0
        self._cache: OrderedDict[tuple[int, int, bool, str], str] = OrderedDict()
        self._last_render = TranscriptRender("", ())
        self._last_width = 80
        self._last_reflow_count = 0
        self._lock = threading.RLock()

    @property
    def text(self) -> str:
        with self._lock:
            return self.render(self._last_width).text

    @property
    def entries(self) -> tuple[TranscriptEvent, ...]:
        with self._lock:
            return tuple(entry.event for entry in self._entries)

    @property
    def cache_keys(self) -> frozenset[tuple[int, int, bool, str]]:
        with self._lock:
            return frozenset(self._cache)

    @property
    def last_reflow_count(self) -> int:
        with self._lock:
            return self._last_reflow_count

    @property
    def trimmed_chars(self) -> int:
        with self._lock:
            return self._trimmed_chars

    def append(self, text: str, *, raw: bool = False) -> int:
        """Append text without adding layout newlines and return trimmed source chars."""
        value = normalize_output(text)
        if not value:
            return 0
        kind = TranscriptKind.RAW_COMMAND_OUTPUT if raw else TranscriptKind.PLAIN_TEXT
        return self.publish(
            TranscriptEvent(
                kind=kind,
                payload=value,
                foldable=raw and long_output_needs_folding(value),
            )
        )

    def publish(self, event: TranscriptEvent) -> int:
        """Append a semantic event and evict complete old entries at the memory bound."""
        size = _event_size(event)
        if size <= 0:
            return 0
        with self._lock:
            if event.turn_id and event.replace_key:
                for entry in reversed(self._entries):
                    current = entry.event
                    if (
                        current.turn_id == event.turn_id
                        and current.replace_key == event.replace_key
                    ):
                        self._source_chars += size - entry.size
                        entry.event = event
                        entry.size = size
                        entry.last_rendered = ""
                        self._drop_cached_entry(entry.entry_id)
                        removed = self._trim_to_budget()
                        self._trimmed_chars += removed
                        return removed
            entry = _StoredEntry(self._next_entry_id, event, size)
            self._next_entry_id += 1
            self._entries.append(entry)
            self._source_chars += size
            removed = self._trim_to_budget()
            self._trimmed_chars += removed
            return removed

    def render(
        self,
        width: int,
        *,
        anchor_entry_id: int | None = None,
        expanded_state: bool | None = None,
        theme: str = "default",
    ) -> TranscriptRender:
        """Render entries for ``width``, reflowing only a bounded active region."""
        viewport_width = max(20, int(width))
        with self._lock:
            if not self._entries:
                self._last_width = viewport_width
                self._last_render = TranscriptRender("", ())
                self._last_reflow_count = 0
                return self._last_render

            targets = self._reflow_targets(viewport_width, anchor_entry_id)
            pieces: list[str] = []
            spans: list[EntrySpan] = []
            style_runs: list[StyleRun] = []
            offset = 0
            reflowed = 0
            prev_kind: TranscriptKind | None = None
            for entry in self._ordered_entries():
                expanded = (
                    not entry.event.initially_collapsed
                    if expanded_state is None
                    else expanded_state
                )
                key = (entry.entry_id, viewport_width, expanded, theme)
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    text, runs = cached
                elif (
                    entry.entry_id in targets
                    or not entry.last_rendered
                    or (
                        entry.event.foldable
                        and entry.last_expanded != expanded
                    )
                    or entry.last_theme != theme
                ):
                    text, runs = _render_event(
                        entry.event, viewport_width, expanded=expanded
                    )
                    self._cache[key] = (text, runs)
                    reflowed += 1
                else:
                    # Far history may retain its previous width, but never a stale
                    # fold state or theme. When it becomes the scroll anchor its
                    # width is reflowed on demand.
                    text, runs = entry.last_rendered, entry.last_runs
                entry.last_rendered = text
                entry.last_runs = runs
                entry.last_expanded = expanded
                entry.last_theme = theme
                # Codex ``UserHistoryCell`` wraps every user turn in a blank line
                # above and below (``Line::from("")``). Do the same by inserting a
                # one-row gutter at each user-turn boundary so a new prompt is never
                # flush against the previous turn's output. The gutter carries an
                # empty-style span (sentinel id ``-1``, never a real entry or the
                # notice id ``0``) so it stays the terminal background rather than
                # the user row's full-width block.
                if pieces and (
                    entry.event.kind == TranscriptKind.USER_MESSAGE
                    or prev_kind == TranscriptKind.USER_MESSAGE
                ):
                    spans.append(EntrySpan(-1, offset, offset + 1, style=""))
                    pieces.append("\n")
                    offset += 1
                pieces.append(text)
                end = offset + len(text)
                spans.append(
                    EntrySpan(
                        entry.entry_id,
                        offset,
                        end,
                        style=_event_style(entry.event),
                    )
                )
                for run in runs:
                    style_runs.append(
                        StyleRun(run.start + offset, run.end + offset, run.style)
                    )
                offset = end
                prev_kind = entry.event.kind

            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
            self._last_width = viewport_width
            self._last_reflow_count = reflowed
            self._last_render = TranscriptRender(
                "".join(pieces),
                tuple(spans),
                truncated_chars=self._trimmed_chars,
                styles=tuple(style_runs),
            )
            return self._last_render

    def anchor_for_offset(self, offset: int) -> TranscriptAnchor:
        with self._lock:
            span = self._last_render.entry_at(offset)
            length = max(1, span.end - span.start)
            fraction = max(0.0, min(1.0, (int(offset) - span.start) / length))
            return TranscriptAnchor(span.entry_id, fraction)

    def offset_for_anchor(self, anchor: TranscriptAnchor) -> int:
        with self._lock:
            for span in self._last_render.spans:
                if span.entry_id == anchor.entry_id:
                    length = max(0, span.end - span.start)
                    return span.start + min(length, max(0, int(length * anchor.fraction)))
            return len(self._last_render.text)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._source_chars = 0
            self._trimmed_chars = 0
            self._cache.clear()
            self._last_render = TranscriptRender("", ())

    def _trim_to_budget(self) -> int:
        removed = 0
        while self._source_chars > self.max_chars:
            group_keys = list(dict.fromkeys(_entry_group_key(entry) for entry in self._entries))
            if len(group_keys) <= 1:
                break
            oldest = group_keys[0]
            evicted = [entry for entry in self._entries if _entry_group_key(entry) == oldest]
            self._entries = [
                entry for entry in self._entries if _entry_group_key(entry) != oldest
            ]
            for entry in evicted:
                removed += entry.size
                self._source_chars -= entry.size
                self._drop_cached_entry(entry.entry_id)
        while len(self._entries) > 1 and self._source_chars > self.max_chars:
            index = 1 if self._entries[0].event.kind == TranscriptKind.USER_MESSAGE else 0
            entry = self._entries.pop(index)
            removed += entry.size
            self._source_chars -= entry.size
            self._drop_cached_entry(entry.entry_id)
        if self._entries and self._source_chars > self.max_chars:
            entry = self._entries[0]
            if isinstance(entry.event.payload, str):
                excess = self._source_chars - self.max_chars
                value = entry.event.payload[excess:]
                newline = value.find("\n")
                if 0 <= newline < len(value) - 1:
                    excess += newline + 1
                    value = value[newline + 1 :]
                entry.event = replace(entry.event, payload=value)
                entry.size = len(value)
                entry.last_rendered = ""
                self._source_chars = entry.size
                removed += excess
        return removed

    def _ordered_entries(self) -> list[_StoredEntry]:
        """Keep every turn contiguous even when its output arrives after queued turns."""
        groups: dict[tuple[str, str | int], list[_StoredEntry]] = {}
        order: list[tuple[str, str | int]] = []
        for entry in self._entries:
            key = _entry_group_key(entry)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(entry)
        return [entry for key in order for entry in groups[key]]

    def _drop_cached_entry(self, entry_id: int) -> None:
        stale = [key for key in self._cache if key[0] == entry_id]
        for key in stale:
            self._cache.pop(key, None)

    def _reflow_targets(self, width: int, anchor_entry_id: int | None) -> set[int]:
        # Initial population must be rendered once. Later resizes focus on the
        # newest rows plus a small neighborhood around the reader's anchor.
        if not self._last_render.spans:
            return {entry.entry_id for entry in self._entries}
        ordered_entries = self._ordered_entries()
        targets: set[int] = set()
        rows = 0
        for entry in reversed(ordered_entries):
            estimate = max(1, entry.last_rendered.count("\n") + 1)
            if rows >= self.reflow_row_budget:
                break
            targets.add(entry.entry_id)
            rows += estimate
        if anchor_entry_id is not None:
            ids = [entry.entry_id for entry in ordered_entries]
            try:
                index = ids.index(anchor_entry_id)
            except ValueError:
                pass
            else:
                targets.update(ids[max(0, index - 2) : index + 3])
        # Same-width cached entries do no work even when selected.
        return targets


def normalize_output(text: str) -> str:
    """Normalize control sequences without inserting display-width newlines."""
    value = str(text).replace("\r\n", "\n").replace("\r", "")
    return _ANSI_ESCAPE.sub("", value)


def clean_scrollback_text(text: str) -> str:
    """Trim trailing full-width padding from each line for faithful selection.

    Rich fills rendered rows with spaces (bare, or wrapped in a background-colour
    SGR span) out to the console width. A line that reaches the terminal's right
    edge trips the auto-wrap margin, so the terminal — tmux copy-mode especially —
    treats it as a *soft-wrapped* logical line: click-drag selection then snaps to
    the logical-line start and drags the padding along, which is exactly the
    "can't select a mid-line segment" bug. Codex sidesteps this by clearing to
    end-of-line instead of padding; we mirror that by emitting every committed row
    at its natural width. Colour is preserved (a reset is re-appended when a line
    left colour open); only trailing blanks are removed.
    """
    if "\n" not in text:
        return _strip_line_padding(text)
    return "\n".join(_strip_line_padding(line) for line in text.split("\n"))


def _strip_line_padding(line: str) -> str:
    if not line or ("\x1b" not in line and not line.endswith((" ", "\t"))):
        return line
    tokens: list[tuple[bool, str]] = []
    index = 0
    for match in _SGR_ESCAPE.finditer(line):
        if match.start() > index:
            tokens.append((False, line[index : match.start()]))
        tokens.append((True, match.group()))
        index = match.end()
    if index < len(line):
        tokens.append((False, line[index:]))
    had_escape = any(is_escape for is_escape, _ in tokens)
    # Drop the trailing whitespace-only text plus any now-dangling SGR that
    # followed it (a background span opened only to paint the padding).
    while tokens:
        is_escape, chunk = tokens[-1]
        if is_escape:
            tokens.pop()
            continue
        stripped = chunk.rstrip(" \t")
        if stripped:
            tokens[-1] = (False, stripped)
            break
        tokens.pop()
    rebuilt = "".join(chunk for _is_escape, chunk in tokens)
    if had_escape and rebuilt and not rebuilt.endswith("\x1b[0m"):
        rebuilt += "\x1b[0m"
    return rebuilt


def _entry_group_key(entry: _StoredEntry) -> tuple[str, str | int]:
    turn_id = entry.event.turn_id
    return ("turn", turn_id) if turn_id else ("entry", entry.entry_id)


def _event_size(event: TranscriptEvent) -> int:
    if isinstance(event.payload, str):
        return len(event.payload)
    return (
        len(event.payload.title)
        + sum(map(len, event.payload.columns))
        + sum(map(len, event.payload.row_styles))
        + sum(len(value) for row in event.payload.rows for value in row)
    )


def _render_event(
    event: TranscriptEvent, width: int, *, expanded: bool = False
) -> tuple[str, tuple[StyleRun, ...]]:
    """Render one entry to visible text plus fine-grained prompt_toolkit style runs.

    Colour is produced by rendering the Rich renderable to ANSI (SGR) and parsing
    it back into prompt_toolkit fragments; wrapping stays owned by the viewport.
    """
    if isinstance(event.payload, DataTableData):
        rendered = render_data_table(event.payload, width)
        if event.foldable:
            rendered = _collapse_long_output(rendered, expanded=expanded)
        return (rendered, ())
    text = str(event.payload)
    if event.kind == TranscriptKind.USER_MESSAGE:
        return (_render_user_message(text, event.state, width), ())
    if event.kind == TranscriptKind.RAW_COMMAND_OUTPUT:
        # Subprocess output is untrusted: never interpret raw cells as Rich
        # markup. Keep the existing bounded head/tail preview behavior.
        if event.foldable:
            text = _collapse_long_output(text, expanded=expanded)
        return (text, ())
    if event.foldable:
        text = _collapse_long_output(text, expanded=expanded)
    if event.kind == TranscriptKind.MARKDOWN:
        rendered, runs = _ansi_to_text_runs(_render_ansi(Markdown(text), width))
        if not rendered.endswith("\n"):
            rendered += "\n"
        return (rendered, runs)
    # STATUS / ERROR / PLAIN_TEXT payloads are trusted Rich markup produced by
    # the CLI (dynamic content is escaped at the source). Parse for colour.
    return _ansi_to_text_runs(_markup_ansi(text))


def render_event_ansi(
    event: TranscriptEvent, width: int, *, expanded: bool = False, as_markdown: bool = True
) -> str:
    """Render one entry to a colour (SGR) string for native terminal scrollback.

    The inline dock commits stabilized entries straight to the terminal's normal
    buffer (via ``run_in_terminal``) so history stays selectable/copyable and
    survives exit. Wrapping is left to the terminal — long logical lines soft-wrap
    like Codex's raw output — so the committed text is faithful to what streamed.

    ``as_markdown=False`` keeps a MARKDOWN payload as raw text; the live tail uses
    it so streamed tokens render immediately without paying markdown cost or
    duplicating the authoritative final render.
    """
    if isinstance(event.payload, DataTableData):
        rendered = render_data_table(event.payload, width)
        if event.foldable:
            rendered = _collapse_long_output(rendered, expanded=expanded)
        return rendered
    text = str(event.payload)
    if event.kind == TranscriptKind.USER_MESSAGE:
        lines = normalize_output(text).splitlines() or [""]
        header = Text()
        header.append("› ", style="bold cyan")
        header.append(lines[0])
        for line in lines[1:]:
            header.append("\n  ")
            header.append(line)
        return _render_ansi(header, width)
    if event.kind == TranscriptKind.RAW_COMMAND_OUTPUT:
        return _collapse_long_output(text, expanded=expanded) if event.foldable else text
    if event.foldable:
        text = _collapse_long_output(text, expanded=expanded)
    if event.kind == TranscriptKind.MARKDOWN:
        if as_markdown:
            return _render_ansi(Markdown(text), width)
        return normalize_output(text)
    return _markup_ansi(text)


# Raw command output folding: how many head/tail lines to keep when collapsed.
_COLLAPSE_HEAD = 8
_COLLAPSE_TAIL = 4


def long_output_needs_folding(text: str) -> bool:
    """Return whether a stable text block has enough logical lines to fold."""
    trailing_nl = text.endswith("\n")
    body = text[:-1].split("\n") if trailing_nl else text.split("\n")
    return len(body) > _COLLAPSE_HEAD + _COLLAPSE_TAIL + 1


def _collapse_long_output(text: str, *, expanded: bool) -> str:
    """Collapse a long stable block to head+tail with an actionable hint."""
    if expanded or not long_output_needs_folding(text):
        return text
    trailing_nl = text.endswith("\n")
    body = text[:-1].split("\n") if trailing_nl else text.split("\n")
    if len(body) <= _COLLAPSE_HEAD + _COLLAPSE_TAIL + 1:
        return text
    hidden = len(body) - _COLLAPSE_HEAD - _COLLAPSE_TAIL
    folded = [
        *body[:_COLLAPSE_HEAD],
        f"… +{hidden} lines · Ctrl+T to expand",
        *body[-_COLLAPSE_TAIL:],
    ]
    return "\n".join(folded) + ("\n" if trailing_nl else "")


def _render_ansi(renderable: Any, width: int, *, soft_wrap: bool = False) -> str:
    """Render a Rich renderable to an ANSI (SGR) string at ``width``.

    ``no_color`` / ``NO_COLOR`` are deliberately ignored here: we always capture
    colour into the string and let prompt_toolkit's ``ColorDepth`` downgrade it
    for the real terminal.
    """
    stream = io.StringIO()
    Console(
        file=stream,
        width=max(20, int(width)),
        color_system="truecolor",
        force_terminal=True,
        no_color=False,
        highlight=False,
        emoji=False,
    ).print(renderable, soft_wrap=soft_wrap, end="")
    return stream.getvalue()


def _markup_ansi(markup: str) -> str:
    """Render trusted Rich markup to ANSI without letting Rich wrap the lines."""
    try:
        renderable: Any = Text.from_markup(markup)
    except Exception:  # noqa: BLE001 - malformed markup must remain printable.
        renderable = Text(markup)
    return _render_ansi(renderable, _WIDE, soft_wrap=True)


def _ansi_to_text_runs(ansi: str) -> tuple[str, tuple[StyleRun, ...]]:
    """Split an ANSI string into visible text and prompt_toolkit style runs."""
    parts: list[str] = []
    runs: list[list[Any]] = []
    offset = 0
    for fragment in to_formatted_text(ANSI(ansi)):
        style = fragment[0]
        frag_text = fragment[1]
        if not frag_text or "[ZeroWidthEscape]" in style:
            continue
        parts.append(frag_text)
        end = offset + len(frag_text)
        if style:
            if runs and runs[-1][2] == style and runs[-1][1] == offset:
                runs[-1][1] = end
            else:
                runs.append([offset, end, style])
        offset = end
    return "".join(parts), tuple(StyleRun(start, stop, style) for start, stop, style in runs)


def _render_user_message(text: str, state: str, width: int) -> str:
    lines = normalize_output(text).splitlines() or [""]
    rendered = [f"› {lines[0]}"]
    rendered.extend(f"  {line}" for line in lines[1:])
    label = " ".join(state.replace("_", " ").split())
    if label:
        suffix = f"[{label}]"
        available = max(0, width - cell_len(rendered[0]) - cell_len(suffix))
        rendered[0] += (" " * max(2, available)) + suffix
    return "\n".join(rendered) + "\n"


def _event_style(event: TranscriptEvent) -> str:
    if event.kind == TranscriptKind.USER_MESSAGE:
        return "class:turn.user"
    if event.kind == TranscriptKind.STATUS:
        return "class:turn.status"
    if event.kind == TranscriptKind.ERROR:
        return "class:turn.error"
    if event.kind == TranscriptKind.MARKDOWN:
        return "class:turn.assistant"
    if event.turn_id:
        return "class:turn.body"
    return ""


def render_data_table(data: DataTableData, width: int) -> str:
    """Render a table as full columns, compact columns, or vertical cards."""
    if data.layout == "commands":
        return _render_command_table(data, width)
    if data.layout == "activity" and width < 76:
        return _render_activity_cards(data)
    if data.layout == "activity" and width < 140:
        return _render_activity_compact(data, width)
    if data.layout == "key_value" and width < 60:
        return _render_key_value_cards(data)
    if data.layout == "auto" and width < 72:
        return _render_generic_cards(data)
    return _render_full_table(data, width)


def _render_command_table(data: DataTableData, width: int) -> str:
    """Preserve the borderless CLI help layout in semantic TUI output."""
    table = Table(
        title=data.title,
        title_justify="left",
        title_style="cyan",
        header_style="cyan",
        box=None,
        padding=(0, 2),
    )
    for index, column in enumerate(data.columns):
        table.add_column(column, overflow="fold", no_wrap=index == 0)
    for index, row in enumerate(data.rows):
        style = data.row_styles[index] if index < len(data.row_styles) else None
        table.add_row(*row, style=style or None)
    return _render_rich(table, width)


def _render_full_table(data: DataTableData, width: int) -> str:
    table = Table(
        title=data.title,
        title_justify="left",
        title_style="bold",
        header_style="bold cyan",
        expand=False,
    )
    for column in data.columns:
        table.add_column(column, overflow="fold")
    for index, row in enumerate(data.rows):
        style = data.row_styles[index] if index < len(data.row_styles) else None
        table.add_row(*row, style=style)
    return _render_rich(table, width)


def _render_activity_compact(data: DataTableData, width: int) -> str:
    indexes = {name.lower(): index for index, name in enumerate(data.columns)}
    table = Table(
        title=data.title,
        title_justify="left",
        title_style="bold",
        header_style="bold cyan",
        expand=False,
    )
    for name in ("#", "event", "status", "detail"):
        table.add_column(name, overflow="fold")
    for row in data.rows:
        def value(*names: str, _row: tuple[str, ...] = row) -> str:
            for name in names:
                index = indexes.get(name)
                if index is not None and index < len(_row) and _row[index]:
                    return _row[index]
            return ""

        context = " · ".join(
            part
            for part in (
                value("execution"),
                f"wf={value('workflow')}" if value("workflow") else "",
                value("pct"),
                value("detail", "note"),
            )
            if part
        )
        table.add_row(value("#"), value("event", "type"), value("status"), context)
    return _render_rich(table, width)


def _render_activity_cards(data: DataTableData) -> str:
    indexes = {name.lower(): index for index, name in enumerate(data.columns)}
    lines = [data.title]
    for row in data.rows:
        def value(*names: str, _row: tuple[str, ...] = row) -> str:
            for name in names:
                index = indexes.get(name)
                if index is not None and index < len(_row) and _row[index]:
                    return _row[index]
            return ""

        lines.append(f"#{value('#')} · {value('event', 'type')} · {value('status')}")
        fields = (
            ("actor", value("actor", "skill/step")),
            ("workflow", value("workflow")),
            ("execution", value("execution")),
            ("pct", value("pct")),
            ("detail", value("detail", "note")),
        )
        for name, item in fields:
            if item:
                lines.append(f"  {name}: {item}")
    return "\n".join(lines) + "\n"


def _render_key_value_cards(data: DataTableData) -> str:
    lines = [data.title]
    for row in data.rows:
        if len(row) >= 2:
            lines.append(f"{row[0]}: {row[1]}")
    return "\n".join(lines) + "\n"


def _render_generic_cards(data: DataTableData) -> str:
    lines = [data.title]
    for number, row in enumerate(data.rows, start=1):
        lines.append(f"#{number}")
        lines.extend(
            f"  {column}: {value}"
            for column, value in zip(data.columns, row, strict=False)
            if value
        )
    return "\n".join(lines) + "\n"


def _render_rich(renderable: Any, width: int) -> str:
    stream = io.StringIO()
    renderer = Console(
        file=stream,
        width=max(20, int(width)),
        color_system=None,
        force_terminal=False,
        no_color=True,
    )
    renderer.print(renderable)
    return stream.getvalue()


__all__ = [
    "DataTableData",
    "EntrySpan",
    "StyleRun",
    "TranscriptAnchor",
    "TranscriptEvent",
    "TranscriptKind",
    "TranscriptModel",
    "TranscriptRender",
    "clean_scrollback_text",
    "long_output_needs_folding",
    "normalize_output",
    "render_data_table",
    "render_event_ansi",
]
