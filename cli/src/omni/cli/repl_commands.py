"""Single source of truth for the REPL's slash commands and their completion.

Both interactive surfaces (the inline dock :mod:`omni.cli.repl_tui` and the classic
:mod:`omni.cli.repl_input` prompt) build one :class:`CommandCatalog`, so the completion
menu, the ``/help`` reference, and the dispatcher can no longer drift apart. The
historical bug this prevents: ``/inbox`` was documented and dispatched yet never
suggested, because the completer's command list was a separate, hand-maintained set.

External commands are introspected straight from the Typer/Click application — names,
help, subcommands, and options — so completion always mirrors what actually runs.
REPL-only verbs (``/inbox``, ``/clear`` …) that have no Typer command are declared
explicitly here.

Codex parity: Codex feeds both its slash popup and its dispatch from one
``SlashCommand`` enum (``codex-rs/tui/src/slash_command.rs``). We follow the same
single-source model and extend it with subcommand/option tiers, because omni's
commands are hierarchical Typer groups rather than a flat enum.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import typer

# Root-level commands that are never user-facing slash commands.
_HIDDEN_ROOT = {"chat"}

# Research verbs. Everything else is session/workspace (dialogue, setup, ops).
# ``/help`` and the bare-``/`` completer both use this split so the two surfaces
# cannot drift: conversation first, then the research appendix.
RESEARCH_COMMANDS = frozenset(
    {
        "artifacts",
        "bench",
        "cite",
        "claim",
        "eval",
        "evidence",
        "hypo",
        "lit",
        "memory",
        "plan",
        "review",
        "run",
        "skills",
        "soul",
        "source",
        "task",
        "verify",
    }
)


def command_group(name: str) -> str:
    """``research`` or ``session`` for a bare or ``/``-prefixed command name."""
    return "research" if name.strip().lstrip("/").lower() in RESEARCH_COMMANDS else "session"


def _first_line(text: str | None) -> str:
    """First non-empty line of a help string with internal whitespace collapsed."""
    if not text:
        return ""
    for line in text.splitlines():
        stripped = " ".join(line.split())
        if stripped:
            return stripped
    return ""


class SlashOption:
    """A single ``--flag``/``-f`` option surfaced for completion."""

    __slots__ = ("opts", "help")

    def __init__(self, opts: Sequence[str], help: str = "") -> None:
        self.opts: tuple[str, ...] = tuple(opts)
        self.help = help

    @property
    def label(self) -> str:
        """All spellings joined for display, e.g. ``--session, -s``."""
        return ", ".join(self.opts)

    def match(self, prefix: str) -> str | None:
        """Return the concrete flag to insert for ``prefix`` (long form preferred)."""
        low = prefix.lower()
        hits = [opt for opt in self.opts if opt.lower().startswith(low)]
        if not hits:
            return None
        long = [opt for opt in hits if opt.startswith("--")]
        return (long or hits)[0]


class SlashSubcommand:
    """A subcommand of a Typer group (e.g. ``show`` under ``/task``)."""

    __slots__ = ("name", "help", "options")

    def __init__(self, name: str, help: str = "", options: Sequence[SlashOption] = ()) -> None:
        self.name = name
        self.help = help
        self.options: tuple[SlashOption, ...] = tuple(options)


class SlashCommand:
    """A top-level slash command with any subcommands and options."""

    __slots__ = ("name", "help", "kind", "group", "subcommands", "options")

    def __init__(
        self,
        name: str,
        help: str = "",
        kind: str = "external",
        *,
        group: str | None = None,
        subcommands: Sequence[SlashSubcommand] = (),
        options: Sequence[SlashOption] = (),
    ) -> None:
        self.name = name
        self.help = help
        self.kind = kind  # "external" (a Typer command) | "in_process" (REPL-only)
        self.group = group if group is not None else command_group(name)
        self.subcommands: tuple[SlashSubcommand, ...] = tuple(subcommands)
        self.options: tuple[SlashOption, ...] = tuple(options)

    @property
    def token(self) -> str:
        return f"/{self.name}"

    def subcommand(self, name: str) -> SlashSubcommand | None:
        low = name.strip().lower()
        for sub in self.subcommands:
            if sub.name.lower() == low:
                return sub
        return None


class CommandCatalog:
    """Every REPL slash command, keyed by name, for completion and name listing."""

    def __init__(self, commands: Sequence[SlashCommand]) -> None:
        self.commands: tuple[SlashCommand, ...] = tuple(commands)
        self._by_name = {command.name.lower(): command for command in self.commands}

    def get(self, name: str) -> SlashCommand | None:
        return self._by_name.get(name.strip().lstrip("/").lower())

    def names(self) -> tuple[str, ...]:
        """Bare command names, alphabetically sorted."""
        return tuple(sorted(command.name for command in self.commands))

    def slash_names(self) -> tuple[str, ...]:
        """``/``-prefixed command names, alphabetically sorted."""
        return tuple(f"/{name}" for name in self.names())

    @classmethod
    def from_names(cls, names: Iterable[str]) -> CommandCatalog:
        """Build a names-only catalog (no help/subcommands/options).

        Backwards-compatible path for callers/tests that still pass a plain list of
        command strings (with or without a leading slash) to the input surfaces.
        """
        seen: dict[str, SlashCommand] = {}
        for raw in names:
            name = raw.strip().lstrip("/").strip()
            if name and name.lower() not in seen:
                seen[name.lower()] = SlashCommand(name)
        return cls(tuple(seen.values()))


# Command objects are Click commands, but Typer bundles a *vendored* Click
# (``typer._click``), so ``isinstance(x, click.Group/Option)`` against the public
# ``click`` package is unreliable. We duck-type instead: a group exposes a
# ``.commands`` mapping, and an option is a parameter whose ``param_type_name`` is
# ``"option"`` (as opposed to ``"argument"``). This stays correct across Click
# versions and whether or not Typer vendors it.
def _command_map(command: Any) -> Mapping[str, Any]:
    commands = getattr(command, "commands", None)
    return commands if isinstance(commands, Mapping) else {}


def _is_group(command: Any) -> bool:
    return isinstance(getattr(command, "commands", None), Mapping)


def _options(command: Any) -> tuple[SlashOption, ...]:
    """Visible ``--flag``/``-f`` options of a command (positional arguments excluded)."""
    out: list[SlashOption] = []
    for param in getattr(command, "params", ()):
        if getattr(param, "param_type_name", "") != "option" or getattr(param, "hidden", False):
            continue
        opts = (*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ()))
        flags = tuple(opt for opt in opts if opt.startswith("-"))
        if flags:
            out.append(SlashOption(flags, _first_line(getattr(param, "help", None))))
    return tuple(out)


def _command_help(command: Any) -> str:
    return _first_line(getattr(command, "short_help", None) or getattr(command, "help", None))


def _subcommands(group: Any) -> tuple[SlashSubcommand, ...]:
    subs: list[SlashSubcommand] = []
    for name, command in _command_map(group).items():
        if not name or getattr(command, "hidden", False):
            continue
        subs.append(SlashSubcommand(name, _command_help(command), _options(command)))
    return tuple(subs)


def _root_group(app: typer.Typer) -> Any | None:
    """Materialise the Click command tree, or ``None`` if Typer cannot build it."""
    try:
        root = typer.main.get_command(app)
    except Exception:  # noqa: BLE001 - completion must never break the REPL.
        return None
    return root if _is_group(root) else None


def _external_from_registry(app: typer.Typer) -> tuple[SlashCommand, ...]:
    """Names-only fallback when the Click tree cannot be materialised."""
    out: list[SlashCommand] = []
    for info in app.registered_commands:
        name = info.name
        if not name or name in _HIDDEN_ROOT or name.startswith("_") or getattr(info, "hidden", False):
            continue
        out.append(SlashCommand(name, _first_line(getattr(info, "help", None))))
    for group in app.registered_groups:
        name = group.name
        if not name or name in _HIDDEN_ROOT or name.startswith("_"):
            continue
        instance = getattr(group, "typer_instance", None)
        help_text = _first_line(getattr(getattr(instance, "info", None), "help", None))
        out.append(SlashCommand(name, help_text))
    return tuple(out)


def _external_commands(app: typer.Typer) -> tuple[SlashCommand, ...]:
    """Every non-hidden top-level Typer command with its subcommands and options."""
    root = _root_group(app)
    if root is None:
        return _external_from_registry(app)
    out: list[SlashCommand] = []
    for name, command in _command_map(root).items():
        if not name or name in _HIDDEN_ROOT or name.startswith("_") or getattr(command, "hidden", False):
            continue
        subs = _subcommands(command) if _is_group(command) else ()
        out.append(
            SlashCommand(name, _command_help(command), subcommands=subs, options=_options(command))
        )
    return tuple(out)


def _in_process_commands() -> tuple[SlashCommand, ...]:
    """REPL-only verbs with no backing Typer command.

    ``lit``, ``verify``, ``memory``, ``resume``, ``update`` and friends dispatch
    in-process too, but they are also registered Typer commands, so they arrive via
    :func:`_external_commands`. Only the verbs below have no Typer command and must be
    declared here to appear in completion and stay in sync with the dispatcher.
    """
    screen = SlashOption(("--screen",), "Only redraw the screen; keep context, tasks, and memory")
    return (
        SlashCommand("help", "Show the slash-command reference", "in_process"),
        SlashCommand(
            "clear",
            "Start a clean context while keeping history, tasks, artifacts, and memory",
            "in_process",
            options=(screen,),
        ),
        SlashCommand("new", "Start a new session without clearing terminal scrollback", "in_process"),
        SlashCommand("copy", "Copy the last assistant answer to the clipboard (Alt+Y)", "in_process"),
        SlashCommand(
            "inbox",
            "Inspect completions (this workspace + IM channel anchor)",
            "in_process",
        ),
        SlashCommand("stop", "Cancel the active turn without leaving the REPL", "in_process"),
        SlashCommand("steer", "Redirect the active turn with a new instruction", "in_process"),
        SlashCommand("compact", "Compact older turns and report estimated token savings", "in_process"),
        SlashCommand("context", "Show the session context budget and injected sections", "in_process"),
        SlashCommand("mode", "Switch REPL mode: auto, plan, or review", "in_process"),
        SlashCommand("verbose", "Set live progress detail: quiet, normal, or verbose", "in_process"),
        SlashCommand("debug", "Toggle the L4 diagnostic layer: /debug [on|off]", "in_process"),
        SlashCommand("soul", "List, inspect, and create scientist personas", "in_process"),
        SlashCommand("plan", "Create and persist a plan without executing it", "in_process"),
        SlashCommand("review", "Review a request with read-only tools", "in_process"),
        SlashCommand("exit", "Leave the REPL cleanly", "in_process"),
        SlashCommand("quit", "Leave the REPL cleanly", "in_process"),
    )


def build_command_catalog(app: typer.Typer) -> CommandCatalog:
    """Build the unified slash-command catalog from ``app`` plus REPL-only verbs."""
    external = _external_commands(app)
    taken = {command.name.lower() for command in external}
    extras = tuple(command for command in _in_process_commands() if command.name.lower() not in taken)
    return CommandCatalog((*external, *extras))
