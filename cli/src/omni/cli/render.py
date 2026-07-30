"""Rich rendering helpers for the CLI."""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from typing import Any

from rich.console import Console, ConsoleDimensions
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from omni.cli import theme
from omni.cli.command_surface import pins_its_own_surface, spell_commands
from omni.cli.repl_output import RoutedTextIO, publish_transcript_event
from omni.cli.repl_transcript import (
    DataTableData,
    NoticeData,
    TranscriptEvent,
    TranscriptKind,
)
from omni.runtime.presentation import artifact_display_label


class RoutedConsole(Console):
    """Rich console that keeps plain output semantic until the TUI renders it."""

    @property
    def size(self) -> ConsoleDimensions:
        """Honour ``COLUMNS``/``LINES`` even in a "dumb" terminal.

        Rich hardcodes 80x25 whenever ``TERM`` is ``dumb``/``unknown`` — common
        in editor-integrated shells (Cursor sets ``TERM=dumb``) and CI — and
        never consults ``COLUMNS``. That makes width non-deterministic across
        environments and folds long diagnostic paths mid-string. Prefer an
        explicit ``COLUMNS`` so rendered output is identical everywhere;
        otherwise defer entirely to Rich's detection.
        """
        if self._width is None and self.is_dumb_terminal:
            columns = self._environ.get("COLUMNS")
            if columns and columns.isdigit():
                lines = self._environ.get("LINES")
                height = int(lines) if lines and lines.isdigit() else 25
                return ConsoleDimensions(int(columns), height)
        return super().size

    def print(self, *objects: Any, **kwargs: Any) -> None:
        event = _plain_print_event(objects, kwargs)
        if event is not None and publish_transcript_event(event, stream=self.file):
            return
        super().print(*objects, **kwargs)


console = RoutedConsole(file=RoutedTextIO(lambda: sys.stdout))
err_console = RoutedConsole(file=RoutedTextIO(lambda: sys.stderr))

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_TOKEN = re.compile(r"\s+|\S+")

#: A run of text and the Rich style carrying it; an empty style means body text.
_Span = tuple[str, str]


def banner(text: str | Text) -> None:
    """Frame the startup guide, letting its content rather than its box carry colour."""
    renderable = text if isinstance(text, Text) else Text(text)
    console.print(Panel(renderable, border_style=theme.MUTED, expand=False))


def guide(markup: str) -> None:
    """Render a multi-line startup guide as an accented callout.

    Deliberately not a ``Panel``. A box is a pre-rendered width, and only ``str``
    or ``Text`` payloads reach the TUI transcript at all — a ``Panel`` falls
    through to a stream that reports ``isatty() == False``, where Rich emits no
    styling whatsoever. That is why the startup box arrived in the dock with
    every colour stripped. A leading bar claims no width, keeps its colour
    through the transcript, and still reflows when the terminal is resized.
    """
    bar = _status_glyph(console, "▌", "|")
    body = "\n".join(
        f"[{theme.ACCENT}]{bar}[/{theme.ACCENT}] {line}" for line in markup.splitlines()
    )
    if publish_transcript_event(
        TranscriptEvent(kind=TranscriptKind.PLAIN_TEXT, payload=body + "\n"),
        stream=console.file,
    ):
        return
    console.print(body)


def info(msg: str) -> None:
    if publish_transcript_event(
        TranscriptEvent(
            kind=TranscriptKind.STATUS,
            payload=f"[{theme.ACCENT}]·[/{theme.ACCENT}] {_status_markup(msg)}\n",
        ),
        stream=console.file,
    ):
        return
    console.print(f"[{theme.ACCENT}]›[/{theme.ACCENT}] {_status_markup(msg)}")


def success(msg: str) -> None:
    marker = _status_glyph(console, "✓", "+")
    console.print(f"[{theme.SUCCESS}]{marker}[/{theme.SUCCESS}] {_status_markup(msg)}")


def warn(msg: str) -> None:
    """A degraded state, coloured whole-line so it survives a wall of output.

    Colouring only the glyph left the sentence itself in body text, which is the
    one part the reader has to notice; Codex tints the message too
    (``PrefixedWrappedHistoryCell::new(message.yellow(), "⚠ ".yellow())``).
    """
    marker = _status_glyph(console, "⚠", "!")
    console.print(f"[{theme.CAUTION}]{marker} {_status_markup(msg)}[/{theme.CAUTION}]")


def error(msg: str) -> None:
    marker = _status_glyph(err_console, "✗", "X")
    err_console.print(f"[{theme.STRONG} {theme.DANGER}]{marker} {_status_markup(msg)}[/]")


def _status_markup(msg: str) -> str:
    """Escape a status line, accenting the command it tells the reader to run.

    A hint is only useful if the reader can pick the command out of the sentence
    around it, and until now every one of these lines arrived in a single
    colour. Escaping still happens per span, so content like ``[mcp_servers.x]``
    is never parsed as markup.
    """
    return _spans_markup(_prose_spans(msg))


def error_card(title: str, message: str, *, actions: Sequence[str] = ()) -> None:
    """Render one semantic, actionable failure without exposing diagnostics."""
    _notice_card(
        title,
        message,
        actions,
        style=theme.DANGER,
        glyph="✗",
        fallback="X",
        kind=TranscriptKind.ERROR,
    )


def action_card(title: str, message: str, *, actions: Sequence[str] = ()) -> None:
    """Render a turn that stopped because it needs the user, not because it broke.

    A missing API key is a todo, not a crash, and painting it in the failure
    colour taught readers to distrust the failure colour. opencode draws exactly
    this line — a rejected tool gets the error gutter while a permission or
    question prompt gets warning and accent — and the state it describes is the
    one the reader can actually clear.
    """
    _notice_card(
        title,
        message,
        actions,
        style=theme.CAUTION,
        glyph="⚠",
        fallback="!",
        kind=TranscriptKind.STATUS,
    )


def _notice_card(
    title: str,
    message: str,
    actions: Sequence[str],
    *,
    style: str,
    glyph: str,
    fallback: str,
    kind: TranscriptKind,
) -> None:
    """Publish one card, leaving its layout to the surface that shows it.

    The card is handed over unwrapped. Laying it out here would freeze it at
    ``console.width``, which is 80 whenever stdout is not a terminal — the REPL
    case, where the transcript is a prompt_toolkit buffer rather than a tty — so
    a wide window got a narrow card full of pointless breaks, and resizing the
    window could not repair it. Only the plain-stream fallback, where the
    console *is* the surface, lays the card out at print time.
    """
    data = NoticeData(
        title=title,
        message=message,
        actions=tuple(str(action) for action in actions if str(action).strip()),
        style=style,
        glyph=glyph,
        fallback=fallback,
    )
    if publish_transcript_event(
        TranscriptEvent(kind=kind, payload=data),
        stream=console.file,
    ):
        return
    console.print(notice_markup(data, console.width))


def notice_markup(data: NoticeData, width: int) -> str:
    """One card at ``width``: a coloured bar down the side, prose inside it.

    The severity rides the bar and the heading rather than the sentence. Tinting
    a whole paragraph — which is what a bare ``error()`` does to a 300-character
    failure — costs the reader legibility at the moment they most need it, so
    opencode keeps its message body in ``text``/``textMuted`` and spends the
    colour on the ``┃`` gutter. The bar also claims no fixed width, which a
    ``Panel`` would; see ``guide()`` for why that matters in the transcript.

    Wrapping happens here, rather than being left to the viewport as ordinary
    markup is, because the bar is drawn per line: every break the viewport
    invented would arrive without one, leaving a card whose colour stops after
    its first line.
    """
    marker = _status_glyph(console, data.glyph, data.fallback)
    heading = f"{marker} {spell_commands(data.title)}"
    paragraphs = [[(heading, f"{theme.STRONG} {data.style}".strip())]]
    if data.message:
        paragraphs.append(_prose_spans(data.message, respell=True))
    paragraphs.extend(_action_spans(action) for action in data.actions)

    bar = _status_glyph(console, "▌", "|")
    body = max(20, width - len(bar) - 1)
    return "\n".join(
        f"[{data.style}]{bar}[/{data.style}] {_spans_markup(line)}".rstrip()
        for paragraph in paragraphs
        for line in _wrap_spans(paragraph, body)
    )


def _prose_spans(text: str, *, respell: bool = False) -> list[_Span]:
    """Split prose into body text and the `backticked` parts worth typing.

    The accent goes on the quoted span because that is the part the reader has
    to retype, and a 300-character failure gives it nowhere to stand out
    otherwise: Codex colours inline code cyan and opencode gives it its own
    foreground. The backticks stay — unlike a markdown renderer, which owns the
    whole document and can drop them, these fragments are quoted inside status
    prose where the ticks are what marks the span as literal.

    ``respell`` is off by default because most of this prose is hand-written
    help, which documents the REPL and must keep the canonical form for every
    reader; only a machine-generated card body is rewritten for the surface its
    reader is typing at. Respelling then happens per span rather than over the
    sentence, so a bare ``/run`` in a filesystem path is never taken for a
    command.
    """
    spell = spell_commands if respell and not pins_its_own_surface(text) else str
    spans: list[_Span] = []
    cursor = 0
    for match in _INLINE_CODE.finditer(text):
        spans.append((text[cursor : match.start()], ""))
        spans.append((f"`{spell(match.group(1))}`", theme.ACCENT))
        cursor = match.end()
    spans.append((text[cursor:], ""))
    return [span for span in spans if span[0]]


def _action_spans(action: str) -> list[_Span]:
    """Style one next-action so the command leads and its gloss follows.

    Next-actions arrive in three shapes — a bare command, a command with a
    ``": "`` explanation, and a sentence that quotes one — and colouring the
    whole line flattens the difference. openclaw's help formatter makes the same
    split, accenting the command and muting the description beside it.
    """
    action = str(action).strip()
    head, separator, tail = action.partition(": ")
    if separator and _is_command(head):
        return [(spell_commands(head), theme.ACCENT), (separator + tail, theme.MUTED)]
    if _is_command(action):
        return [(spell_commands(action), theme.ACCENT)]
    return _prose_spans(action, respell=True)


def _is_command(text: str) -> bool:
    return text.startswith(("/", "omni "))


def _wrap_spans(spans: Sequence[_Span], width: int) -> list[list[_Span]]:
    """Word-wrap styled text, so a caller can own what starts each line.

    Rich wraps at print time, which is one line too late for a gutter: the bar
    is drawn per line, and every continuation Rich invents arrives without one,
    leaving a block whose colour stops after its first line. Wrapping here keeps
    the bar unbroken and the styling attached to the words that carry it.
    """
    lines: list[list[_Span]] = []
    current: list[_Span] = []
    used = 0
    for text, style in spans:
        for token in _TOKEN.findall(text):
            if token.isspace():
                if current:  # a wrapped line never opens with padding
                    current.append((token, style))
                    used += len(token)
                continue
            if current and used + len(token) > width:
                lines.append(_rstrip_spans(current))
                current, used = [], 0
            current.append((token, style))
            used += len(token)
    if current:
        lines.append(_rstrip_spans(current))
    return lines or [[]]


def _rstrip_spans(line: list[_Span]) -> list[_Span]:
    while line and not line[-1][0].strip():
        line.pop()
    return line


def _spans_markup(line: Sequence[_Span]) -> str:
    """Emit one tag pair per run of styling, not one per word.

    Wrapping works on tokens, so a styled phrase arrives here in pieces. Tagging
    each piece renders identically but writes several times the markup, and the
    transcript keeps its scrollback under a character budget that this would
    spend on escape codes.
    """
    return "".join(
        f"[{style}]{escape(text)}[/{style}]" if style else escape(text)
        for text, style in _coalesce(line)
    )


def _coalesce(line: Sequence[_Span]) -> list[_Span]:
    merged: list[_Span] = []
    for text, style in line:
        if merged and merged[-1][1] == style:
            merged[-1] = (merged[-1][0] + text, style)
        else:
            merged.append((text, style))
    return merged


def shorten(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, admitting it when anything was lost.

    A silent slice is worse than a short message: the reader cannot tell a
    sentence that ended from one that was cut, and the piece most often lost is
    the command at the end. Every terminal that truncates says so — Codex and
    opencode both append ``…``.
    """
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def one_line(value: Any, limit: int) -> str:
    """Fold ``value`` onto a single line of at most ``limit`` characters.

    Table cells and status lines get one line each, so a summary carrying a
    newline would otherwise break the row it sits in. Collapsing first and
    cutting second also keeps the limit honest: cutting raw text spends the
    budget on whitespace the reader never sees.
    """
    return shorten(" ".join(str(value or "").split()), limit)


def _status_glyph(target: Console, preferred: str, fallback: str) -> str:
    """Return a marker encodable by the active terminal stream."""
    encoding = str(getattr(target.file, "encoding", None) or "utf-8")
    try:
        preferred.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return fallback
    return preferred


def markdown_body(text: str) -> None:
    """Render a markdown *document* — assistant prose, or a report deliverable.

    Codex draws the line between document and output rather than between
    assistant and tool: ``markdown.rs`` renders agent replies and plan bodies as
    markdown while exec stdout and MCP results stay literal and dim. A generated
    paper sits on the document side of that line, so printing its ``##`` and
    ``**`` markers verbatim made the very thing the turn was asked to produce
    read like a log dump.
    """
    if publish_transcript_event(
        TranscriptEvent(kind=TranscriptKind.MARKDOWN, payload=text),
        stream=console.file,
    ):
        return
    try:
        console.print(Markdown(text))
    except Exception:  # noqa: BLE001
        console.print(text)


def assistant_answer(text: str) -> None:
    if not text.strip():
        console.print(f"[{theme.MUTED}](empty response)[/]")
        return
    markdown_body(text)


def artifact_line(label: str, target: str = "", uri: str = "", *, indent: str = "  ") -> None:
    """One produced file: bold what it is, plain where it is, dim how to reopen it.

    The emphasis belongs on the label because that is what the reader scans a
    result block for — which outputs exist — while the path is what they copy
    once they have found the right line. Codex makes the same split (bold
    ``Added``/``Edited`` verb, plainly-styled path) and so does OpenClaw (bold
    tool title over a dim argument line).
    """
    name = escape(artifact_display_label(label))
    body = f"{indent}[{theme.ACCENT}]•[/] [{theme.STRONG}]{name}[/]"
    if target:
        body += f": {escape(target)}"
    if uri and uri != target:
        body += f" [{theme.MUTED}]{escape(uri)}[/]"
    console.print(body)


def artifact_preview(label: str, body: str, *, markdown: bool, hint: str = "") -> None:
    """Inline a deliverable's body under its own heading, bounded by the caller.

    ``hint`` is the continuation affordance for a body the caller had to cut, and
    its presence is what makes the heading say "(preview)" — an untruncated body
    is the whole artifact and should not apologise for itself.
    """
    heading = artifact_display_label(label)
    if hint:
        heading += " (preview)"
    console.print(f"\n[{theme.STRONG} {theme.ACCENT}]{escape(heading)}[/]")
    if markdown:
        markdown_body(body)
    else:
        console.print(escape(body))
    if hint:
        console.print(f"[{theme.MUTED}]{escape(hint)}[/]")


def kv_table(title: str, rows: list[tuple[str, Any]]) -> None:
    event = TranscriptEvent(
        kind=TranscriptKind.DATA_TABLE,
        payload=DataTableData(
            title=title,
            columns=("key", "value"),
            rows=tuple((str(key), str(value)) for key, value in rows),
            layout="key_value",
        ),
    )
    if publish_transcript_event(event, stream=console.file):
        return
    table = Table(title=title, show_header=False, box=None, title_justify="left", title_style="bold")
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(overflow="fold")
    for k, v in rows:
        table.add_row(str(k), str(v))
    console.print(table)


def data_table(
    title: str,
    columns: list[str],
    rows: list[list[Any]],
    *,
    layout: str = "auto",
) -> None:
    event = TranscriptEvent(
        kind=TranscriptKind.DATA_TABLE,
        payload=DataTableData(
            title=title,
            columns=tuple(str(column) for column in columns),
            rows=tuple(tuple(str(value) for value in row) for row in rows),
            layout=layout,
        ),
    )
    if publish_transcript_event(event, stream=console.file):
        return
    table = Table(title=title, title_justify="left", title_style="bold", header_style="bold cyan")
    for c in columns:
        table.add_column(c, overflow="fold")
    for r in rows:
        table.add_row(*[str(x) for x in r])
    console.print(table)


def _plain_print_event(
    objects: tuple[Any, ...],
    options: dict[str, Any],
) -> TranscriptEvent | None:
    """Convert ordinary Rich print calls without prematurely wrapping text."""
    if any(not isinstance(obj, (str, Text)) for obj in objects):
        return None
    separator = str(options.get("sep", " "))
    end = str(options.get("end", "\n"))
    markup = options.get("markup") is not False
    parts: list[str] = []
    for obj in objects:
        if isinstance(obj, Text):
            # Already-rendered Text carries no markup; escape so the transcript
            # renderer prints it literally rather than re-parsing brackets.
            parts.append(escape(obj.plain))
        elif markup:
            # Preserve the Rich markup so the transcript keeps its colours.
            parts.append(obj)
        else:
            parts.append(escape(obj))
    return TranscriptEvent(
        kind=TranscriptKind.PLAIN_TEXT,
        payload=separator.join(parts) + end,
    )


def prompt_text(label: str, default: str = "") -> str:
    from rich.prompt import Prompt

    return Prompt.ask(Text(label, style="cyan"), default=default)


def prompt_secret(label: str) -> str:
    from rich.prompt import Prompt

    return Prompt.ask(Text(label, style="cyan"), password=True, default="")


def confirm(label: str, default: bool = True) -> bool:
    from rich.prompt import Confirm

    return Confirm.ask(Text(label, style="cyan"), default=default)
