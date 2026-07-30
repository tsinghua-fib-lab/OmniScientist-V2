"""Interactive REPL input surface with terminal-safe redraws."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    ThreadedCompleter,
    merge_completers,
)
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style

from omni.cli import theme
from omni.cli.file_search import (
    DEFAULT_LIMIT,
    FileCandidate,
    FileSearcher,
    deliverable_roots,
)
from omni.cli.repl_command_policy import command_contains_sensitive_data
from omni.cli.repl_commands import CommandCatalog, SlashCommand
from omni.cli.repl_composer import install_multiline_bindings
from omni.cli.repl_layout import clip_display as _clip_display
from omni.cli.repl_layout import compact_number as _compact_number
from omni.cli.repl_layout import display_width as _display_width
from omni.cli.terminal_harness import TerminalKeyboardProtocol
from omni.core.file_mentions import active_mention_token

_STYLE = Style.from_dict(
    {
        "omni.frame": theme.PTK_ACCENT,
        "omni.prompt": f"bold {theme.PTK_ACCENT}",
        "omni.mode": theme.PTK_STRONG,
        "omni.hint": theme.PTK_MUTED,
        "omni.key": theme.PTK_ACCENT,
    }
)


def _ranked(items: Sequence[tuple[str, str, str]], current: str) -> list[tuple[str, str, str]]:
    """Order ``(insert, display, meta)`` rows exact-first then prefix (case-insensitive).

    Mirrors Codex's ``command_popup`` filtering: an exact name match sorts ahead of
    prefix matches, and an empty ``current`` keeps every candidate (bare ``/`` lists
    all commands). Relative order within each bucket is preserved.
    """
    low = current.lower()
    exact: list[tuple[str, str, str]] = []
    prefix: list[tuple[str, str, str]] = []
    for insert, display, meta in items:
        lowered = insert.lower()
        if lowered == low:
            exact.append((insert, display, meta))
        elif lowered.startswith(low):
            prefix.append((insert, display, meta))
    return exact + prefix


class SlashCommandCompleter(Completer):
    """Context-aware completion for the REPL's ``/`` command surface.

    Three tiers, driven by one :class:`~omni.cli.repl_commands.CommandCatalog`:

    * ``/`` or ``/ver`` -> command names, each with its description as completion meta.
    * ``/task`` + space, or ``/task sh`` -> that command's subcommands.
    * ``/task show --`` (any word starting with ``-``) -> the resolved (sub)command's
      options.

    Codex completes only the command name (its args are free text); omni's commands
    are Typer groups, so we can offer subcommand and option tiers as well. Completion
    only ever triggers on the slash surface — ordinary prompts are untouched.
    """

    def __init__(self, catalog: CommandCatalog | Sequence[str]) -> None:
        self._catalog = (
            catalog if isinstance(catalog, CommandCatalog) else CommandCatalog.from_names(catalog)
        )

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> Iterator[Completion]:
        del complete_event
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        body = text[1:]
        ends_with_space = bool(body) and body[-1].isspace()
        parts = body.split()
        current = "" if ends_with_space else (parts[-1] if parts else "")
        completed = parts if ends_with_space else parts[:-1]

        if not completed:
            yield from self._complete_names(current)
            return
        command = self._catalog.get(completed[0])
        if command is None:
            return
        if current.startswith("-"):
            yield from self._complete_options(command, completed, current)
        elif len(completed) == 1 and command.subcommands:
            yield from self._complete_subcommands(command, current)

    def _complete_names(self, current: str) -> Iterator[Completion]:
        # Alphabetical within the exact/prefix buckets: the top-level list is long
        # (~50 commands), so a predictable order beats registration order here.
        commands = sorted(self._catalog.commands, key=lambda command: command.name)
        rows = [(command.name, command.token, command.help) for command in commands]
        for insert, display, meta in _ranked(rows, current):
            yield Completion(
                insert, start_position=-len(current), display=display, display_meta=meta
            )

    def _complete_subcommands(self, command: SlashCommand, current: str) -> Iterator[Completion]:
        rows = [(sub.name, sub.name, sub.help) for sub in command.subcommands]
        for insert, display, meta in _ranked(rows, current):
            yield Completion(
                insert, start_position=-len(current), display=display, display_meta=meta
            )

    def _complete_options(
        self, command: SlashCommand, completed: Sequence[str], current: str
    ) -> Iterator[Completion]:
        scope = command.options
        if len(completed) >= 2:
            sub = command.subcommand(completed[1])
            if sub is not None:
                scope = sub.options
        low = current.lower()
        exact: list[Completion] = []
        prefix: list[Completion] = []
        for option in scope:
            insert = option.match(current)
            if insert is None:
                continue
            completion = Completion(
                insert, start_position=-len(current), display=option.label, display_meta=option.help
            )
            (exact if insert.lower() == low else prefix).append(completion)
        yield from exact
        yield from prefix


class FileMentionCompleter(Completer):
    """Completion for the REPL's ``@`` file-mention surface.

    The sibling of :class:`SlashCommandCompleter`: that one owns text starting
    with ``/``, this one owns the ``@`` token under the cursor anywhere in the
    buffer, because a mention is normally written mid-sentence ("review
    @README.md and plot it"). Candidates are gitignore-aware and never include
    sensitive files.

    Accepting a candidate keeps the ``@`` and replaces only the token, so the
    marker survives into the submitted text where it acts as the explicit
    attachment grant. Directories are completed with a trailing ``/`` and an open
    quote so navigation can continue.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        searcher: FileSearcher | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        self._searcher = searcher or FileSearcher(root)
        self._limit = max(1, limit)

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> Iterator[Completion]:
        del complete_event
        active = active_mention_token(document.text_before_cursor)
        if active is None:
            return
        for candidate in self._searcher.search(active.token, limit=self._limit):
            label = candidate.relative + ("/" if candidate.is_dir else "")
            yield Completion(
                _mention_completion_text(candidate, quoted=active.quoted),
                start_position=-len(active.token),
                display=label,
                display_meta="dir" if candidate.is_dir else "file",
            )


def _mention_completion_text(candidate: FileCandidate, *, quoted: bool) -> str:
    """Text that replaces the typed token, quoting only when required."""
    path = candidate.relative + ("/" if candidate.is_dir else "")
    needs_quotes = any(char.isspace() for char in path)
    if candidate.is_dir:
        # Leave any quote open: the user is still descending into the tree.
        if quoted:
            return path
        return f'"{path}' if needs_quotes else path
    if quoted:
        return f'{path}"'
    return f'"{path}"' if needs_quotes else path


def build_repl_completer(
    commands: CommandCatalog | Sequence[str] = (),
    *,
    root: Path | None = None,
    output_base: Path | None = None,
) -> Completer:
    """The one completer both interactive surfaces use.

    ``ReplInputBox`` and ``ReplTui`` must not drift: a mention that completes in
    one and not the other is worse than no completion at all. Threading the
    merged completer keeps typing responsive because building the file index can
    shell out to ``git ls-files`` or walk a large tree.

    ``output_base`` is where omni writes deliverables (``artifacts.output_dir``);
    its per-kind subfolders stay mentionable even when gitignored.
    """
    base = (root or Path.cwd()).resolve()
    # ``artifacts.output_dir`` is conventionally relative (default ``"."``);
    # anchor it to the picker root so indexed paths stay relative to that root.
    configured = Path(output_base) if output_base is not None else base
    anchored = configured if configured.is_absolute() else base / configured
    searcher = FileSearcher(base, always_visible=deliverable_roots(anchored))
    return ThreadedCompleter(
        merge_completers(
            [SlashCommandCompleter(commands), FileMentionCompleter(searcher=searcher)],
        )
    )


class ReplCommandHistory(InMemoryHistory):
    """In-memory history that never recalls commands containing credentials."""

    def append_string(self, string: str) -> None:
        if command_contains_sensitive_data(string):
            return
        super().append_string(string)


class ReplInputBox:
    """Render a framed, editable prompt without taking over terminal scrollback.

    ``prompt_toolkit`` owns terminal editing only while input is active. Its
    stdout proxy redraws the prompt after daemon notifications or other
    background output, avoiding the corrupted input lines produced by a plain
    ``input()`` call.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        commands: CommandCatalog | Sequence[str] = (),
        output_base: Path | None = None,
    ) -> None:
        self._enabled = _interactive_terminal() if enabled is None else enabled
        self._mode = "auto"
        self._model = ""
        self._focus = ""
        self._context_tokens = 0
        self._context_window = 0
        self._clearable_tokens = 0
        self._last_elapsed_seconds: float | None = None
        self._completer = build_repl_completer(commands, output_base=output_base)
        self._key_bindings = self._create_key_bindings()
        self._session: PromptSession[str] | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def update_status(
        self,
        *,
        model: str = "",
        focus: str = "",
        context_tokens: int = 0,
        context_window: int = 0,
        clearable_tokens: int = 0,
    ) -> None:
        """Update the status rendered below the next input prompt."""
        self._model = model.strip()
        self._focus = " ".join(focus.split())
        self._context_tokens = max(0, int(context_tokens))
        self._context_window = max(0, int(context_window))
        self._clearable_tokens = max(0, int(clearable_tokens))

    def set_last_elapsed(self, seconds: float | None) -> None:
        self._last_elapsed_seconds = None if seconds is None else max(0.0, float(seconds))

    def read_line(self, *, mode: str, fallback: Callable[[], str]) -> str:
        """Read one line, falling back to the legacy prompt outside a TTY."""
        if not self._enabled:
            return fallback()

        self._mode = mode or "auto"
        session = self._ensure_session()
        output = getattr(getattr(session, "app", None), "output", None)
        try:
            with TerminalKeyboardProtocol(output):
                with patch_stdout(raw=True):
                    value = session.prompt()
        finally:
            self._print_footer()
        return value

    async def read_line_async(self, *, mode: str, fallback: Callable[[], str]) -> str:
        """Read one line without nesting a synchronous event loop."""
        if not self._enabled:
            return fallback()

        self._mode = mode or "auto"
        session = self._ensure_session()
        output = getattr(getattr(session, "app", None), "output", None)
        try:
            with TerminalKeyboardProtocol(output):
                with patch_stdout(raw=True):
                    value = await session.prompt_async()
        finally:
            self._print_footer()
        return value

    def _ensure_session(self) -> PromptSession[str]:
        if self._session is None:
            self._session = PromptSession(
                message=self._prompt_message,
                bottom_toolbar=self._bottom_toolbar,
                history=ReplCommandHistory(),
                enable_history_search=True,
                multiline=True,
                mouse_support=False,
                completer=self._completer,
                complete_while_typing=True,
                key_bindings=self._key_bindings,
                style=_STYLE,
            )
        return self._session

    def _create_key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        def submit(event) -> None:  # noqa: ANN001
            if not event.current_buffer.text.strip():
                event.current_buffer.reset()
                return
            event.current_buffer.validate_and_handle()

        install_multiline_bindings(bindings, submit=submit)

        return bindings

    def _prompt_message(self) -> FormattedText:
        return FormattedText(
            [
                ("class:omni.frame", f"╭{_fill('─')}\n│ "),
                ("class:omni.prompt", "› "),
            ]
        )

    def _bottom_toolbar(self) -> FormattedText:
        prefix = "╰─ "
        mode = f"{self._mode} mode"
        width = _terminal_width()
        details: list[str] = []
        if width >= 112:
            if self._model:
                details.append(f"model {_clip_display(self._model, 24)}")
            if self._focus:
                details.append(f"focus {_clip_display(self._focus, 22)}")
        if width >= 72 and self._context_window:
            details.append(
                f"ctx {_compact_number(self._context_tokens)}/{_compact_number(self._context_window)}"
            )
        if width >= 92 and self._clearable_tokens:
            details.append(f"/clear saves {_compact_number(self._clearable_tokens)}")
        if width >= 58 and self._last_elapsed_seconds is not None:
            details.append(f"last {_format_elapsed(self._last_elapsed_seconds)}")
        if width >= 112:
            details.append("Enter send · Ctrl+J newline · Ctrl+C cancel · Ctrl+L redraw")
        elif width >= 88:
            details.append("Enter send · Ctrl+J newline · Ctrl+C cancel")
        elif width >= 60:
            details.append("Enter send · Ctrl+C cancel")
        elif width >= 28:
            details.append("Enter send")

        available = max(0, width - _display_width(prefix) - 1)
        mode = _clip_display(mode, available)
        detail = f" · {' · '.join(details)} " if details else ""
        detail = _clip_display(detail, max(0, available - _display_width(mode)))
        used = _display_width(prefix) + _display_width(mode) + _display_width(detail)
        return FormattedText(
            [
                ("class:omni.frame", prefix),
                ("class:omni.mode", mode),
                *theme.hint_fragments(
                    detail, key_class="class:omni.key", label_class="class:omni.hint"
                ),
                ("class:omni.frame", "─" * max(1, width - used)),
            ]
        )

    def _print_footer(self) -> None:
        print_formatted_text(self._bottom_toolbar(), style=_STYLE)


def _interactive_terminal() -> bool:
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, OSError):
        return False


def _terminal_width() -> int:
    return max(8, shutil.get_terminal_size(fallback=(80, 24)).columns)


def _fill(char: str) -> str:
    return char * max(1, _terminal_width() - 1)


def _format_elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {seconds % 60:.0f}s"
