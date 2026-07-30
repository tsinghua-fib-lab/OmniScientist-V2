"""Contracts keeping ``omni command`` and REPL ``/command`` in sync."""

from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.main import get_command

from omni.cli.main import app
from omni.cli.state import AppState


def _command_paths() -> list[tuple[str, ...]]:
    root = get_command(app)
    paths: list[tuple[str, ...]] = []

    def walk(command, prefix: tuple[str, ...] = ()) -> None:  # noqa: ANN001
        for name, child in getattr(command, "commands", {}).items():
            path = (*prefix, name)
            paths.append(path)
            walk(child, path)

    walk(root)
    return paths


def test_repl_route_contract_covers_every_cli_command_path():
    import omni.cli.main as main

    roots = {path[0] for path in _command_paths()}
    in_process = set(getattr(main, "_REPL_IN_PROCESS_COMMANDS", ()))
    external = set(main._REPL_EXTERNAL_COMMANDS)

    assert in_process == {"lit", "verify", "memory", "resume", "update", "upgrade"}
    assert external == roots - in_process
    assert external.isdisjoint(in_process)
    # These formerly hand-parsed commands must inherit every Typer subcommand
    # and option automatically instead of maintaining a second parser.
    assert {"config", "skills", "task", "eval", "bench", "current", "why", "chat"} <= external
    in_process_children = {
        "memory": set(main._MEMORY_SUBCOMMANDS),
        "resume": {"help"},
        "lit": set(),
        "verify": set(),
        "update": set(main._UPDATE_SUBCOMMANDS),
        "upgrade": set(),
    }
    for path in _command_paths():
        if len(path) == 1 or path[0] in external:
            continue
        assert path[1] in in_process_children[path[0]], path


def test_repl_session_aware_external_translation_uses_cli_syntax():
    import omni.cli.main as main

    translate = getattr(main, "_session_aware_external_line", None)
    assert translate is not None
    sid = "session-123"

    assert shlex.split(translate("/task", sid)) == ["task", "list"]
    assert shlex.split(translate("/skills", sid)) == ["skills", "help"]
    assert shlex.split(translate("/config", sid)) == ["config", "help"]
    assert shlex.split(translate("/channel", sid)) == ["channel", "help"]
    # These groups gained a real `help` subcommand, so a bare group opens its
    # reference table instead of Typer's `--help` usage/error exit.
    assert shlex.split(translate("/mcp", sid)) == ["mcp", "help"]
    assert shlex.split(translate("/session", sid)) == ["session", "help"]
    assert shlex.split(translate("/artifacts", sid)) == ["artifacts", "help"]
    assert shlex.split(translate("/schedule", sid)) == ["schedule", "list"]
    assert shlex.split(translate("/task session", sid)) == ["task", "session", sid]
    assert shlex.split(translate("/task attach abc", sid)) == [
        "task", "attach", "abc", "--session", sid,
    ]
    assert shlex.split(translate("/current", sid)) == ["current", "--session", sid]
    assert shlex.split(translate("/why run-1", sid)) == [
        "why", "run-1", "--session", sid,
    ]
    assert shlex.split(translate("/skills arxiv", sid)) == ["skills", "search", "arxiv"]
    assert shlex.split(translate("/skills export codex", sid)) == [
        "skills", "export", "codex",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        "/skills",
        "/skills export codex",
        "/skills unexport codex",
        "/task step workflow-1 step-1 --json",
        "/task delete task-1 --force",
        "/task drain",
        "/task inbox",
        "/eval --record --tag routing",
        "/bench --help",
        "/chat explain RAG",
    ],
)
async def test_repl_external_commands_share_the_cli_dispatcher(monkeypatch, line):
    import omni.cli.main as main

    dispatched: list[str] = []

    async def fake_external(_state, command):  # noqa: ANN001
        dispatched.append(command)
        return 0

    monkeypatch.setattr(main, "_run_repl_external_command", fake_external)
    monkeypatch.setattr("omni.cli.commands.eval_cmd.render_eval", lambda **_kw: None)
    monkeypatch.setattr("omni.cli.commands.bench_cmd.render_bench", lambda **_kw: None)

    class Registry:
        def refresh_settings(self, _settings) -> None:  # noqa: ANN001
            pass

    agent = SimpleNamespace(registry=Registry())
    result = await main._repl_command(agent, AppState(), line, "session-123")

    assert dispatched == [main._session_aware_external_line(line, "session-123")]
    assert result.agent is agent
    assert result.session_id == "session-123"
    assert result.restart is False


@pytest.mark.asyncio
async def test_repl_skills_export_and_unexport_codex_end_to_end():
    import omni.cli.main as main

    class Registry:
        def refresh_settings(self, _settings) -> None:  # noqa: ANN001
            pass

    agent = SimpleNamespace(registry=Registry())
    state = AppState()
    result = await main._repl_command(agent, state, "/skills export codex", "session-123")

    home = Path.home()
    assert result.agent is agent
    assert (home / ".codex" / "skills" / "arxiv-fetch" / "SKILL.md").is_file()
    assert (home / ".agents" / "skills" / "arxiv-fetch" / "SKILL.md").is_file()
    assert not (home / ".claude" / "skills" / "arxiv-fetch").exists()

    await main._repl_command(agent, state, "/skills unexport codex", "session-123")
    assert not (home / ".codex" / "skills" / "arxiv-fetch").exists()
    assert not (home / ".agents" / "skills" / "arxiv-fetch").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        "/config model -p openai -m gpt-test",
        "/config embeddings --disable",
    ],
)
async def test_repl_config_mutation_reloads_the_live_agent(monkeypatch, line):
    import omni.cli.main as main

    calls: list[str] = []

    class OldAgent:
        async def aclose(self) -> None:
            calls.append("closed")

    old_agent = OldAgent()
    new_agent = SimpleNamespace()

    async def fake_external(_state, command):  # noqa: ANN001
        calls.append(command)
        return 0

    async def fake_make_agent(_state):  # noqa: ANN001
        calls.append("reloaded")
        return new_agent

    monkeypatch.setattr(main, "_run_repl_external_command", fake_external)
    monkeypatch.setattr(main, "make_agent", fake_make_agent)

    result = await main._repl_command(
        old_agent,
        AppState(),
        line,
        "session-123",
    )

    assert calls == [line.lstrip("/"), "closed", "reloaded"]
    assert result.agent is new_agent


def test_repl_help_covers_all_visible_cli_commands():
    from omni.cli.main import _repl_quickstart_rows

    root = get_command(app)
    visible = {
        name
        for name, command in root.commands.items()
        if not getattr(command, "hidden", False) and name != "chat"
    }
    shown = {
        token.lstrip("/")
        for row in _repl_quickstart_rows()
        for token in row[0].split()
        if token.startswith("/")
    }

    assert visible <= shown


@pytest.mark.asyncio
async def test_repl_clear_starts_clean_context_without_deleting_history(monkeypatch):
    import omni.cli.main as main
    from omni.agent import OmniAgent
    from omni.config import load_settings

    agent = await OmniAgent.create(load_settings())
    old_session = await agent.ensure_session(channel="cli", reuse_latest=False)
    await agent._persist_message(old_session, "user", "Keep this research question")
    await agent._persist_message(old_session, "assistant", "Keep this answer")
    cleared: list[bool] = []
    monkeypatch.setattr(main.console, "clear", lambda: cleared.append(True))
    try:
        result = await main._repl_command(agent, AppState(), "/clear", old_session)

        assert result.session_id != old_session
        assert cleared == [True]
        assert [row["content"] for row in await agent._history(old_session)] == [
            "Keep this research question",
            "Keep this answer",
        ]
        assert await agent._history(result.session_id) == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_repl_clear_screen_does_not_change_session(monkeypatch):
    import omni.cli.main as main

    redrawn: list[bool] = []
    monkeypatch.setattr(
        main,
        "redraw_active_output",
        lambda: redrawn.append(True) or True,
        raising=False,
    )
    monkeypatch.setattr(
        main,
        "clear_active_output",
        lambda: pytest.fail("screen redraw must not delete the transcript"),
    )
    agent = SimpleNamespace()

    result = await main._repl_command(agent, AppState(), "/clear --screen", "session-123")

    assert result.session_id == "session-123"
    assert redrawn == [True]
