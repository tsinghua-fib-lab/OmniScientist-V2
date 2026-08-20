"""The REPL slash-command catalog and its context-aware completion.

These lock in the fix for the historical drift bug (``/inbox`` was dispatched and
documented yet never suggested) and the three completion tiers: command names,
subcommands after a space, and options after ``-``/``--``.
"""

from __future__ import annotations

from prompt_toolkit.completion import CompleteEvent, Completion
from prompt_toolkit.document import Document
from typer.main import get_command

from omni.cli.main import _repl_quickstart_rows, _repl_slash_commands, app
from omni.cli.repl_commands import RESEARCH_COMMANDS, CommandCatalog, build_command_catalog
from omni.cli.repl_input import SlashCommandCompleter

# REPL-only verbs that have no backing Typer command but are dispatched in the loop.
_IN_PROCESS = {
    "help", "clear", "new", "inbox", "stop", "steer", "compact",
    "context", "mode", "verbose", "model", "plan", "review", "exit", "quit",
}


def _completer() -> SlashCommandCompleter:
    return SlashCommandCompleter(build_command_catalog(app))


def _rows(text: str) -> list[Completion]:
    doc = Document(text, len(text))
    return list(_completer().get_completions(doc, CompleteEvent(completion_requested=True)))


def _texts(text: str) -> list[str]:
    return [completion.text for completion in _rows(text)]


def _visible_root_commands() -> set[str]:
    root = get_command(app)
    return {
        name
        for name, command in root.commands.items()
        if name and name != "chat" and not name.startswith("_") and not getattr(command, "hidden", False)
    }


def _help_table_tokens() -> set[str]:
    return {
        token.lstrip("/")
        for row in _repl_quickstart_rows()
        for token in str(row[0]).split()
        if token.startswith("/")
    }


def test_catalog_covers_inbox_every_dispatched_and_documented_command():
    """The catalog is the single source of truth, so it must cover every command
    the dispatcher handles and ``/help`` documents — including ``/inbox``, the
    verb that used to be dispatched and documented but never suggested."""
    names = set(build_command_catalog(app).names())

    assert "inbox" in names
    assert _IN_PROCESS <= names
    assert _visible_root_commands() <= names
    assert _help_table_tokens() <= names


def test_repl_slash_commands_are_derived_from_the_catalog():
    slash = _repl_slash_commands()

    assert slash == build_command_catalog(app).slash_names()
    assert "/inbox" in slash
    # Sorted, unique, and every entry carries the leading slash.
    assert list(slash) == sorted(set(slash))
    assert all(name.startswith("/") for name in slash)


def test_bare_slash_lists_all_commands_with_descriptions():
    rows = _rows("/")
    catalog = build_command_catalog(app)

    assert {completion.text for completion in rows} == set(catalog.names())
    # Displayed as "/name" while inserting the bare name after the typed slash.
    inbox = next(completion for completion in rows if completion.text == "inbox")
    assert inbox.display_text == "/inbox"
    assert inbox.display_meta_text == (
        "Inspect completions (this workspace + IM channel anchor)"
    )
    assert inbox.start_position == 0
    web = next(completion for completion in rows if completion.text == "web")
    assert web.display_meta_text
    assert "browser" in web.display_meta_text.lower() or "web" in web.display_meta_text.lower()


def test_catalog_groups_session_and_research():
    catalog = build_command_catalog(app)
    assert {command.group for command in catalog.commands} <= {"session", "research"}
    assert catalog.get("web").group == "session"
    assert catalog.get("help").group == "session"
    assert catalog.get("lit").group == "research"
    assert RESEARCH_COMMANDS <= set(catalog.names())


def test_bare_slash_lists_session_commands_before_research():
    names = _texts("/")
    catalog = build_command_catalog(app)
    session = sorted(command.name for command in catalog.commands if command.group == "session")
    research = sorted(command.name for command in catalog.commands if command.group == "research")

    assert names[: len(session)] == session
    assert names[len(session) :] == research
    assert names.index("web") < names.index("lit")


def test_prefix_completes_both_inbox_and_init():
    """The regression: typing ``/in`` must offer both ``/init`` and ``/inbox``."""
    texts = _texts("/in")

    assert "inbox" in texts
    assert "init" in texts
    # Alphabetical within the prefix bucket.
    assert texts == sorted(texts)


def test_space_completes_subcommands_of_a_group():
    after_space = _texts("/task ")
    assert "show" in after_space
    assert "list" in after_space

    typing = _rows("/task sh")
    assert [completion.text for completion in typing] == ["show"]
    assert typing[0].start_position == -2  # replaces only "sh", keeping "/task "


def test_model_completion_exposes_the_three_typed_roles_and_source_views():
    assert {"main", "vision", "embedding", "status", "explain"} <= set(
        _texts("/model ")
    )


def test_group_help_is_completed_for_every_multi_subcommand_group():
    """`schedule`/`mcp`/`session`/`artifacts` gained a `help` subcommand, so
    `/x help` now works and is suggested (they previously lacked it)."""
    for group in ("schedule", "mcp", "session", "artifacts", "task", "web"):
        assert "help" in _texts(f"/{group} ")


def test_web_offers_its_manage_subcommand_surface():
    assert {
        "start", "stop", "status", "restart", "port", "help",
    } <= set(_texts("/web "))


def test_schedule_offers_its_full_subcommand_surface():
    """The truncated-hint report: `/schedule ` must offer every subcommand (the
    completer already yields them; the dock now reserves room to render them)."""
    assert {
        "add", "list", "all", "show", "remove", "enable",
        "disable", "proposals", "approve", "deny", "run", "help",
    } <= set(_texts("/schedule "))


def test_double_dash_completes_options_for_command_and_subcommand():
    # Subcommand option (Typer group -> child -> option).
    assert "--json" in _texts("/task show --")
    # Leaf-command option.
    assert "--verify" in _texts("/lit --")
    # REPL-only command option declared in the catalog.
    assert "--screen" in _texts("/clear --")


def test_option_completion_shows_all_spellings_and_prefers_long_form():
    rows = _rows("/lit --q")
    quiet = next(completion for completion in rows if completion.text == "--quiet")

    assert quiet.display_text == "--quiet, -q"
    assert quiet.start_position == -3  # replaces "--q"


def test_no_completions_off_the_slash_surface_or_for_unknown_commands():
    assert _texts("summarise this paper") == []
    assert _texts("ask /task") == []
    assert _texts("/definitely-not-a-command ") == []
    # A leaf command has no subcommands, so a trailing space offers nothing.
    assert _texts("/inbox ") == []


def test_from_names_builds_a_names_only_catalog():
    catalog = CommandCatalog.from_names(["/alpha", "beta", "/alpha"])

    assert catalog.names() == ("alpha", "beta")
    assert catalog.slash_names() == ("/alpha", "/beta")
    assert catalog.get("alpha") is not None
    assert catalog.get("/beta") is not None
    assert catalog.get("alpha").subcommands == ()
