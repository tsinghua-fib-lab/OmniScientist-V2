"""Rich rendering helpers for the CLI."""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console, ConsoleDimensions
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from omni.cli.repl_output import RoutedTextIO, publish_transcript_event
from omni.cli.repl_transcript import DataTableData, TranscriptEvent, TranscriptKind


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


def banner(text: str | Text) -> None:
    renderable = text if isinstance(text, Text) else Text(text)
    console.print(Panel(renderable, border_style="cyan", expand=False))


def info(msg: str) -> None:
    # escape() so message content like "[mcp_servers.x]" isn't parsed as markup.
    if publish_transcript_event(
        TranscriptEvent(kind=TranscriptKind.STATUS, payload=f"[cyan]·[/cyan] {escape(msg)}\n"),
        stream=console.file,
    ):
        return
    console.print(f"[cyan]›[/cyan] {escape(msg)}")


def success(msg: str) -> None:
    console.print(f"[green]✓[/green] {escape(msg)}")


def warn(msg: str) -> None:
    console.print(f"[yellow]![/yellow] {escape(msg)}")


def error(msg: str) -> None:
    err_console.print(f"[red]✗[/red] {escape(msg)}")


def error_card(title: str, message: str, *, actions: tuple[str, ...] = ()) -> None:
    """Render one semantic, actionable failure without exposing diagnostics."""
    lines = [f"[bold red]✗ {escape(title)}[/bold red]"]
    if message:
        lines.append(escape(message))
    lines.extend(f"  [cyan]{escape(action)}[/cyan]" for action in actions)
    payload = "\n".join(line for line in lines if line).rstrip() + "\n"
    if publish_transcript_event(
        TranscriptEvent(kind=TranscriptKind.ERROR, payload=payload),
        stream=console.file,
    ):
        return
    console.print(f"[bold red]✗ {escape(title)}[/bold red]")
    if message:
        console.print(escape(message))
    for action in actions:
        console.print(f"  [cyan]{escape(action)}[/cyan]")


def assistant_answer(text: str) -> None:
    if not text.strip():
        console.print("[dim](empty response)[/dim]")
        return
    if publish_transcript_event(
        TranscriptEvent(kind=TranscriptKind.MARKDOWN, payload=text),
        stream=console.file,
    ):
        return
    try:
        console.print(Markdown(text))
    except Exception:  # noqa: BLE001
        console.print(text)


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
