"""Spell a command the way the reader would type it on their own surface.

omni is driven from two places: a shell, where the user types ``omni config set
…``, and the interactive REPL, where the same command is ``/config set …``.
Producers of a hint — a skill engine, a doctor check, a failure card — cannot
know which one is reading, and every one that guessed wrote the shell form. The
REPL therefore told users to type ``omni config set …`` at a prompt that answers
to ``/``.

So the *slash form is canonical*: it is what ``runtime.presentation`` already
emits for task next-actions, and the render layer respells it for a shell reader
on the way out. Producers write one form and stay surface-agnostic, which also
keeps portable skills free of any CLI import.

The rewrite is deliberately confined to machine-generated command fields — card
bodies, action lists — and never runs over hand-written help prose, because a
sentence like "use ``/channel`` in the REPL or ``omni channel`` in the shell"
names both surfaces on purpose and respelling it would say the same thing twice.
"""

from __future__ import annotations

import os
import re

REPL = "repl"
SHELL = "shell"

#: Set for every child the REPL spawns, so a subprocess spells hints for the
#: surface its output lands on rather than for the shell it technically runs in.
SURFACE_ENV = "OMNI_SURFACE"

#: Public top-level commands. A canonical hint may only name one of these, so a
#: bare path or URL fragment is never mistaken for a command. Kept in step with
#: the Typer app by ``test_command_surface``.
COMMANDS = frozenset({
    "artifacts", "autosota", "bench", "channel", "chat", "cite", "claim", "config",
    "current", "doctor", "eval", "evidence", "exec", "hypo", "init", "lit", "mcp",
    "memory", "model", "profile", "project", "replay", "resume", "run", "schedule", "serve",
    "session", "skills", "soul", "source", "status", "task", "terminal", "terminal-setup",
    "trust", "uninstall", "update", "upgrade", "verify", "web", "why",
})

# A canonical command: a slash that opens a word, naming a command, and stopping
# at punctuation that ends a sentence rather than continuing a filename. The
# lookbehind rejects ``https://host/task`` and ``~/.omni/config``; the negative
# lookahead rejects ``/config.toml``.
_CANONICAL = re.compile(r"(?<![\w/])/([a-z][a-z-]*)(?!\.\w)(?=[\s'\"`,);:.\]]|$)")
# Prose that names a surface is documenting *that* surface, whoever is reading.
_NAMES_A_SURFACE = re.compile(r"\b(?:in|at|from) the (?:repl|shell)\b", re.IGNORECASE)


def active_surface() -> str:
    """Return the surface whose reader will see the next message.

    An in-process agent turn is on the REPL exactly when the TUI owns output; a
    child command is told through the environment, because it has its own stdout
    and cannot see the parent's sink.
    """
    from omni.cli.repl_output import get_output_sink

    if get_output_sink() is not None:
        return REPL
    return REPL if os.environ.get(SURFACE_ENV) == REPL else SHELL


def spell_commands(text: str, *, surface: str | None = None) -> str:
    """Respell every canonical command inside ``text`` for ``surface``.

    A REPL reader already sees the canonical form, so that path is a no-op and
    costs nothing on the common surface.
    """
    if not text or pins_its_own_surface(text):
        return text
    if (surface or active_surface()) == REPL:
        return text
    return _CANONICAL.sub(_to_shell, text)


def pins_its_own_surface(text: str) -> bool:
    """Whether ``text`` already decides which surface its commands belong to.

    A line does that two ways. It may spell the shell form itself — "use
    ``/project …`` in the REPL or ``omni project …`` in the shell" — where
    respelling the first half would make the sentence say one thing twice. Or it
    may name the surface in prose — "Import and enable in the REPL: ``/skills
    add …``" — where the commands are quoted as documentation *of the REPL*, and
    respelling them for a shell reader makes the sentence contradict itself.

    Callers must test the whole sentence, not an individual quoted span, or the
    halves are judged apart and the guard never sees the pair it protects.
    """
    return "omni " in text or _NAMES_A_SURFACE.search(text) is not None


def _to_shell(match: re.Match[str]) -> str:
    name = match.group(1)
    return f"omni {name}" if name in COMMANDS else match.group(0)
