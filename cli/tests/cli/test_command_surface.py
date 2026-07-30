"""A command hint has to name the prompt its reader will type at.

omni answers to ``omni config set …`` in a shell and to ``/config set …`` in the
REPL. Producers write the slash form and the render layer respells it, so these
tests pin both the respelling and the cases where respelling would be wrong.
"""

from __future__ import annotations

import pytest

from omni.cli import command_surface
from omni.cli.command_surface import REPL, SHELL, active_surface, spell_commands


def test_the_repl_reader_sees_the_form_they_can_type() -> None:
    hint = "/config set research.semantic_scholar_api_key YOUR_KEY"
    assert spell_commands(hint, surface=REPL) == hint


def test_the_shell_reader_sees_the_binary_they_invoke() -> None:
    assert (
        spell_commands("/config set research.contact_email a@b.c", surface=SHELL)
        == "omni config set research.contact_email a@b.c"
    )


@pytest.mark.parametrize(
    "text",
    [
        "see https://example.com/task for the schedule",
        "open ~/.omni/config.toml to inspect it",
        "the socket lives at /run/omni.sock",
        "the file /config.toml is stale",
        "/unknowncommand is not ours",
    ],
)
def test_a_path_or_url_is_never_mistaken_for_a_command(text: str) -> None:
    assert spell_commands(text, surface=SHELL) == text


def test_prose_that_teaches_both_surfaces_is_left_alone() -> None:
    """Respelling the slash half would make the sentence say one thing twice."""
    line = "Use `/project ...` in the REPL or `omni project ...` in the shell."
    assert spell_commands(line, surface=SHELL) == line


def test_prose_that_names_one_surface_documents_that_surface() -> None:
    """`omni skills help` read in a shell still teaches the REPL its own form."""
    line = "Import and enable in the REPL: `/skills add codex:my-skill`."
    assert spell_commands(line, surface=SHELL) == line


def test_every_named_command_is_a_real_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whitelist is what keeps a bare path from being rewritten, so it may
    only contain commands the app actually registers."""
    from omni.cli.main import app

    registered = {command.name for command in app.registered_commands if command.name}
    registered |= {
        group.name or group.typer_instance.info.name for group in app.registered_groups
    }
    public = {name for name in registered if name and not name.startswith("_")}
    assert command_surface.COMMANDS == public


def test_a_child_of_the_repl_writes_hints_for_the_repl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subcommand runs in its own process but its output lands in the dock."""
    monkeypatch.setenv(command_surface.SURFACE_ENV, REPL)
    assert active_surface() == REPL


def test_a_plain_shell_invocation_defaults_to_the_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(command_surface.SURFACE_ENV, raising=False)
    assert active_surface() == SHELL
