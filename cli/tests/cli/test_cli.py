"""CLI smoke tests via Typer's CliRunner (offline mock provider)."""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from omni.cli.main import app
from tests.conftest import cli_text

runner = CliRunner()


def _shown_path(text: str) -> str:
    """A path as printed, normalised so the comparison is about the path.

    Newlines go because a long path is folded to the terminal width; the
    backslash pass is for Windows, where the separator is also the escape
    character every serialiser doubles on the way out.
    """
    return text.replace("\n", "").replace("\\\\", "\\").replace("\\", "/")


def test_task_human_view_collapses_only_consecutive_duplicate_events():
    from omni.cli.commands.tasks_cmd import _collapsed_events
    from omni.storage.models import TaskEventORM

    first = TaskEventORM(
        task_id="task",
        event_type="subtask.progress",
        status="running",
        name="render",
        skill_name="scientific-figure",
        workflow_run_id="workflow",
        workflow_step_id="step",
        subtask_id="execution-a",
        step_id="figure",
        pct=0.5,
        summary="render",
    )
    duplicate = TaskEventORM(
        task_id="task",
        event_type="subtask.progress",
        status="running",
        name="render",
        skill_name="scientific-figure",
        workflow_run_id="workflow",
        workflow_step_id="step",
        subtask_id="execution-a",
        step_id="figure",
        pct=0.5,
        summary="render",
    )
    distinct_execution = TaskEventORM(
        task_id="task",
        event_type="subtask.progress",
        status="running",
        name="render",
        skill_name="scientific-figure",
        workflow_run_id="workflow",
        workflow_step_id="step",
        subtask_id="execution-b",
        step_id="figure",
        pct=0.5,
        summary="render",
    )

    collapsed = _collapsed_events([first, duplicate, distinct_execution])

    assert [(event.subtask_id, count) for event, count in collapsed] == [
        ("execution-a", 2),
        ("execution-b", 1),
    ]


def test_task_human_view_exposes_transport_and_structured_command_outcome():
    from omni.cli.commands.tasks_cmd import _event_note, _event_status
    from omni.storage.models import TaskEventORM

    event = TaskEventORM(
        task_id="task",
        event_type="react.tool.done",
        status="succeeded",
        tool_name="bash",
        output_json={
            "result_schema": "omni.command-result.v1",
            "command_status": "failed",
            "reason": "nonzero_exit",
            "exit_code": 1,
            "summary": "Command exited with code 1",
        },
        summary="Command exited with code 1",
    )

    assert _event_status(event) == "succeeded · command=failed"
    assert _event_note(event) == "exit=1 · Command exited with code 1"


def test_task_human_view_shows_the_process_error_for_a_failed_command():
    from omni.cli.commands.tasks_cmd import _event_note
    from omni.storage.models import TaskEventORM

    event = TaskEventORM(
        task_id="task",
        event_type="react.tool.done",
        status="succeeded",
        tool_name="bash",
        output_json={
            "result_schema": "omni.command-result.v1",
            "command_status": "failed",
            "reason": "nonzero_exit",
            "exit_code": 128,
            "output": "致命错误：不是 Git 仓库（或者任何父目录）：.git\n",
            "summary": "Command exited with code 128",
        },
        summary="Command exited with code 128",
    )

    assert _event_note(event) == (
        "exit=128 · 致命错误：不是 Git 仓库（或者任何父目录）：.git"
    )


def test_task_human_view_skips_progress_and_glosses_126():
    from omni.cli.commands.tasks_cmd import _event_note
    from omni.storage.models import TaskEventORM

    event = TaskEventORM(
        task_id="task",
        event_type="react.tool.done",
        status="succeeded",
        tool_name="bash",
        output_json={
            "result_schema": "omni.command-result.v1",
            "command_status": "failed",
            "reason": "nonzero_exit",
            "exit_code": 126,
            "output": "[ 36%]\n",
            "summary": "Command exited with code 126: [ 36%]",
        },
        summary="Command exited with code 126: [ 36%]",
    )

    assert _event_note(event) == "exit=126 · cannot execute"


def test_task_human_view_keeps_successful_command_output_visible():
    from omni.cli.commands.tasks_cmd import _event_note
    from omni.storage.models import TaskEventORM

    event = TaskEventORM(
        task_id="task",
        event_type="react.tool.done",
        status="succeeded",
        tool_name="bash",
        output_json={
            "result_schema": "omni.command-result.v1",
            "command_status": "succeeded",
            "reason": "ok",
            "exit_code": 0,
            "output": "first line\nsecond line",
            "summary": "Command completed successfully",
        },
        summary="Command completed successfully",
    )

    assert _event_note(event) == "exit=0 · first line second line"


def test_task_human_view_does_not_parse_legacy_shell_output():
    from omni.cli.commands.tasks_cmd import _event_note, _event_status
    from omni.storage.models import TaskEventORM

    event = TaskEventORM(
        task_id="task",
        event_type="react.tool.done",
        status="succeeded",
        tool_name="bash",
        output_json="[exit=1]\n",
        summary="[exit=1]",
    )

    assert _event_status(event) == "succeeded"
    assert _event_note(event) == "[exit=1]"


def test_task_human_view_does_not_treat_foreign_status_dict_as_command_result():
    from omni.cli.commands.tasks_cmd import _event_note, _event_status
    from omni.storage.models import TaskEventORM

    event = TaskEventORM(
        task_id="task",
        event_type="react.tool.done",
        status="succeeded",
        tool_name="external_tool",
        output_json={
            "result_schema": "external.result.v1",
            "command_status": "failed",
            "summary": "External result",
        },
        summary="External result",
    )

    assert _event_status(event) == "succeeded"
    assert _event_note(event) == "External result"


def test_root_readme_documents_shell_and_repl_management_surfaces():
    readme = (Path(__file__).resolve().parents[3] / "README.md").read_text(encoding="utf-8")

    for shell_form, repl_form in (
        ("omni skills add codex:my-skill", "/skills add codex:my-skill"),
        ("omni skills restore livefigure", "/skills restore livefigure"),
        ("omni channel login feishu", "/channel login feishu"),
    ):
        assert shell_form in readme
        assert repl_form in readme


def test_version():
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    assert "OmniScientist" in res.stdout


def test_bare_omni_runs_setup_before_repl_on_first_launch(monkeypatch):
    from omni.cli import main as main_module
    from omni.cli.commands import init_cmd

    events: list[str] = []
    monkeypatch.setattr(main_module, "_terminal_is_interactive", lambda: True)
    monkeypatch.setattr(init_cmd, "run_setup_wizard", lambda _state: events.append("setup"))
    monkeypatch.setattr(
        main_module,
        "_maybe_converge_installation",
        lambda _state: events.append("converge"),
    )
    monkeypatch.setattr(main_module, "_repl", lambda _state, **_kwargs: events.append("repl"))

    res = runner.invoke(app, [])

    assert res.exit_code == 0
    assert events == ["setup", "converge", "repl"]


def test_bare_omni_skips_setup_after_user_config_exists(monkeypatch):
    from omni.cli import main as main_module
    from omni.cli.commands import init_cmd
    from omni.config.paths import get_paths

    paths = get_paths()
    paths.ensure_dirs()
    paths.config_file.write_text('[model]\nprovider = "mock"\nmodel = "omni-mock"\n')
    events: list[str] = []
    monkeypatch.setattr(main_module, "_terminal_is_interactive", lambda: True)
    monkeypatch.setattr(init_cmd, "run_setup_wizard", lambda _state: events.append("setup"))
    monkeypatch.setattr(main_module, "_repl", lambda _state, **_kwargs: events.append("repl"))

    res = runner.invoke(app, [])

    assert res.exit_code == 0
    assert events == ["repl"]


def test_bare_omni_accepts_complete_environment_model_without_setup(monkeypatch):
    from omni.cli import main as main_module
    from omni.cli.commands import init_cmd

    monkeypatch.setenv("OMNI_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OMNI_MODEL_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("OMNI_MODEL", "example-model")
    events: list[str] = []
    monkeypatch.setattr(main_module, "_terminal_is_interactive", lambda: True)
    monkeypatch.setattr(init_cmd, "run_setup_wizard", lambda _state: events.append("setup"))
    monkeypatch.setattr(main_module, "_repl", lambda _state, **_kwargs: events.append("repl"))

    res = runner.invoke(app, [])

    assert res.exit_code == 0
    assert events == ["repl"]


def test_bare_omni_first_launch_fails_fast_without_tty(monkeypatch):
    from omni.cli import main as main_module

    monkeypatch.setattr(main_module, "_terminal_is_interactive", lambda: False)
    monkeypatch.setattr(
        main_module,
        "_repl",
        lambda *_args, **_kwargs: pytest.fail("REPL must not start before first-time setup"),
    )

    res = runner.invoke(app, [])

    assert res.exit_code == 2
    output = " ".join((res.stdout + res.stderr).split())
    assert "First-time setup is required" in output
    assert "omni init --non-interactive" in output


@pytest.mark.asyncio
async def test_repl_relaunches_immediately_after_startup_update(monkeypatch, settings):
    from omni.cli import main as main_module

    events: list[str] = []
    state = SimpleNamespace(settings=lambda: settings)
    monkeypatch.setattr(main_module, "_maybe_prompt_update", lambda _settings: True)
    monkeypatch.setattr(
        main_module,
        "_relaunch_omni",
        lambda: events.append("relaunch"),
    )
    monkeypatch.setattr(
        main_module,
        "make_agent",
        lambda _state: pytest.fail("old-code agent must not be created after update"),
    )

    await main_module._repl_async(state)

    assert events == ["relaunch"]


def test_relaunch_after_interactive_update_continues_latest_session(monkeypatch):
    from omni.cli import main as main_module

    seen: list[list[str]] = []
    monkeypatch.setattr(
        main_module.sys,
        "orig_argv",
        ["/tool/bin/python", "-m", "omni.cli.main", "--profile", "work"],
    )

    def fake_execv(executable, argv):  # noqa: ANN001
        seen.append([executable, *argv])
        raise SystemExit(0)

    monkeypatch.setattr(main_module.os, "execv", fake_execv)

    with pytest.raises(SystemExit):
        main_module._relaunch_omni(continue_session=True)

    assert seen[0][0] == "/tool/bin/python"
    assert seen[0][1:] == [
        "/tool/bin/python",
        "-m",
        "omni.cli.main",
        "--profile",
        "work",
        "--continue",
    ]


def test_init_non_interactive():
    res = runner.invoke(app, ["init", "--non-interactive"])
    assert res.exit_code == 0
    assert "Done" in res.stdout
    assert "keyword recall" in res.stdout
    assert "false" in runner.invoke(
        app, ["config", "get", "memory.embeddings_enabled"]
    ).stdout.lower()


def test_init_non_interactive_configures_semantic_scholar_key_without_leaking_it():
    from omni.config.paths import get_paths

    secret = "s2-init-secret-123"
    res = runner.invoke(
        app,
        [
            "init",
            "--non-interactive",
            "--semantic-scholar-api-key",
            secret,
        ],
    )

    assert res.exit_code == 0, res.output
    assert secret not in res.stdout
    assert "semantic scholar" in res.stdout.lower()
    with get_paths().secrets_file.open("rb") as fh:
        persisted = tomllib.load(fh)
    assert persisted["research"]["semantic_scholar_api_key"] == secret


def test_init_prepares_bundled_skill_runtimes(monkeypatch):
    from omni.cli.commands import init_cmd

    prepared: list[Path] = []
    monkeypatch.setattr(
        init_cmd,
        "setup_research_pptx_runtime",
        lambda paths: prepared.append(paths.cache_dir),
        raising=False,
    )

    res = runner.invoke(app, ["init", "--non-interactive"])

    assert res.exit_code == 0, res.output
    assert prepared == [init_cmd.user_home_resolution()[0] / "cache"]


def test_init_non_interactive_can_cold_start_embeddings():
    """A new user can explicitly enable embeddings without reusing a chat-only URL."""
    res = runner.invoke(
        app,
        [
            "init", "--non-interactive", "--provider", "deepseek",
            "--embeddings", "--embedding-base-url", "https://embed.example/v1",
            "--embedding-model", "bge-m3", "--embedding-api-key", "emb-secret",
        ],
    )

    assert res.exit_code == 0
    assert "semantic recall" in res.stdout
    assert "https://embed.example/v1" in runner.invoke(
        app, ["config", "get", "memory.embedding_base_url"]
    ).stdout
    assert "bge-m3" in runner.invoke(
        app, ["config", "get", "memory.embedding_model"]
    ).stdout
    secret = runner.invoke(app, ["config", "get", "memory.embedding_api_key"])
    assert "emb-secret" not in secret.stdout
    assert "redacted" in secret.stdout


def test_init_explains_embedding_tradeoff_before_choice():
    from omni.cli.commands.init_cmd import _render_embedding_choice
    from omni.cli.render import console

    with console.capture() as cap:
        _render_embedding_choice()
    output = cap.get()
    assert "semantic recall" in output and "/embeddings" in output
    assert "Keyword recall" in output and "does not probe" in output


def test_init_non_interactive_does_not_touch_external_tools():
    # Exporting skills into ~/.claude/skills etc. and MCP registration are now
    # opt-in (default No); `omni init -y` must not silently write into other tools.
    from pathlib import Path

    res = runner.invoke(app, ["init", "--non-interactive"])
    assert res.exit_code == 0
    home = Path.home()  # sandboxed to a temp dir by the isolated_home fixture
    assert not (home / ".claude" / "skills").exists()
    assert not (home / ".codex" / "skills").exists()
    assert not (home / ".agents" / "skills").exists()
    assert not (home / ".claude.json").exists()


def test_init_enter_defaults_keep_optional_integrations_disabled():
    from pathlib import Path

    # mock provider, then Enter for embeddings, the optional Semantic Scholar
    # key, skill export, and MCP registration — optional integrations stay off.
    res = runner.invoke(app, ["init"], input="\n4\n\n\n\n\n")

    assert res.exit_code == 0
    output = res.stdout.lower()
    assert "keyword recall" in output
    assert "semantic scholar api key" in output
    assert "skills exported" in output and "skipped" in output
    assert "mcp integration" in output and "skipped" in output
    home = Path.home()
    assert not (home / ".claude" / "skills").exists()
    assert not (home / ".codex" / "skills").exists()
    assert not (home / ".agents" / "skills").exists()
    assert not (home / ".claude.json").exists()


def test_installers_recommend_init_and_explain_automatic_first_launch():
    root = Path(__file__).resolve().parents[3]
    for script in (root / "cli/scripts/install.sh", root / "cli/scripts/install.ps1"):
        text = script.read_text(encoding="utf-8")
        assert "First-time setup: omni init" in text
        assert "first bare `omni` launch" in text
        assert "_record-install" in text
        assert "omni uninstall --dry-run" in text
    for script in (root / "cli/scripts/uninstall.sh", root / "cli/scripts/uninstall.ps1"):
        text = script.read_text(encoding="utf-8")
        assert "omni uninstall" in text or '"uninstall"' in text
        assert "everything" in text.lower()


def test_init_provider_choice_and_presets():
    # The wizard offers recognizable providers and defaults to openai; each has a
    # sensible base_url/model preset so the user can just press Enter.
    from omni.cli.commands.init_cmd import _provider_preset, _resolve_provider_choice

    assert _resolve_provider_choice("1") == "openai"
    assert _resolve_provider_choice("2") == "deepseek"
    assert _resolve_provider_choice("3") == "ollama"
    assert _resolve_provider_choice("4") == "mock"
    assert _resolve_provider_choice("deepseek") == "deepseek"
    assert _resolve_provider_choice("") == "openai"  # empty answer → default
    assert _resolve_provider_choice("bogus") == "openai"  # unknown → default

    assert _provider_preset("openai")[0] == "https://api.openai.com/v1"
    assert _provider_preset("deepseek") == ("https://api.deepseek.com/v1", "deepseek-chat")
    assert _provider_preset("ollama")[0].startswith("http://localhost")


def test_init_non_interactive_with_provider_uses_preset():
    # `omni init -y --provider deepseek` (no base_url) should persist the friendly
    # provider name plus its preset endpoint/model, not collapse to openai_compatible.
    res = runner.invoke(app, ["init", "-y", "--provider", "deepseek"])
    assert res.exit_code == 0
    prov = runner.invoke(app, ["config", "get", "model.provider"])
    assert "deepseek" in prov.stdout
    base = runner.invoke(app, ["config", "get", "model.base_url"])
    assert "api.deepseek.com" in base.stdout
    mdl = runner.invoke(app, ["config", "get", "model.model"])
    assert "deepseek-chat" in mdl.stdout


def test_config_list():
    res = runner.invoke(app, ["config", "list"])
    assert res.exit_code == 0
    assert "model.provider" in res.stdout


def test_config_set_and_get():
    set_res = runner.invoke(app, ["config", "set", "react.max_iterations", "5"])
    assert set_res.exit_code == 0
    get_res = runner.invoke(app, ["config", "get", "react.max_iterations"])
    assert "5" in get_res.stdout


def test_config_key_alias_resolution():
    from omni.cli.commands.config_cmd import _resolve_key

    for key in ("provider", "api_key", "key", "base_url", "model"):
        with pytest.raises(ValueError):
            _resolve_key(key)
    # Full dotted paths and unknown keys pass through unchanged.
    assert _resolve_key("react.max_iterations") == "react.max_iterations"


def test_config_set_provider_shortcut_is_removed():
    res = runner.invoke(app, ["config", "set", "provider", "openai"])
    assert res.exit_code == 2
    assert runner.invoke(app, ["config", "set", "model.provider", "openai"]).exit_code == 0
    assert "openai" in runner.invoke(app, ["config", "get", "model.provider"]).stdout


def test_config_set_rejects_invalid_toml_without_repair():
    from omni.config.paths import get_paths

    paths = get_paths()
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(
        "[model]\n"
        "provider = openai\n"
        "base_url = https://api.deepseek.com\n",
        encoding="utf-8",
    )

    res = runner.invoke(app, ["config", "set", "model.model", "deepseek-v4-pro"])

    assert res.exit_code == 2


def test_config_api_key_is_masked_everywhere():
    secret = "sk-supersecret-zzz999"
    set_res = runner.invoke(app, ["config", "set", "model.api_key", secret])
    assert set_res.exit_code == 0
    assert secret not in set_res.stdout  # masked on write
    # masked on explicit get (never echo a secret in full)
    got = runner.invoke(app, ["config", "get", "model.api_key"])
    assert got.exit_code == 0 and secret not in got.stdout
    # but actually persisted to secrets.toml so the model can use it
    from omni.config import load_settings

    assert load_settings().model.api_key == secret


def test_config_surfaces_and_masks_semantic_scholar_key():
    secret = "s2-config-secret-456"
    set_res = runner.invoke(
        app,
        ["config", "set", "research.semantic_scholar_api_key", secret],
    )
    assert set_res.exit_code == 0
    assert secret not in set_res.stdout

    got = runner.invoke(
        app,
        ["config", "get", "research.semantic_scholar_api_key"],
    )
    assert got.exit_code == 0
    assert secret not in got.stdout
    assert "redacted" in got.stdout

    parent = runner.invoke(app, ["config", "get", "research"])
    assert parent.exit_code == 0
    assert secret not in parent.stdout
    assert "semantic_scholar_api_key" in parent.stdout
    assert "redacted" in parent.stdout

    listed = runner.invoke(app, ["config", "list"])
    assert listed.exit_code == 0
    compact_list = "".join(listed.stdout.split())
    assert "research.semantic_scholar_api_key" in compact_list
    assert secret not in listed.stdout

    help_result = runner.invoke(app, ["config", "help"])
    assert help_result.exit_code == 0
    compact_help = "".join(help_result.stdout.split())
    assert "research.semantic_scholar_api_key" in compact_help
    assert secret not in help_result.stdout

    unset_result = runner.invoke(
        app,
        ["config", "unset", "research.semantic_scholar_api_key"],
    )
    assert unset_result.exit_code == 0
    assert "omni serve restart" in unset_result.stdout
    after_unset = runner.invoke(
        app,
        ["config", "get", "research.semantic_scholar_api_key"],
    )
    assert after_unset.exit_code == 0
    assert secret not in after_unset.stdout
    assert after_unset.stdout.strip().endswith('""')


def test_parent_config_redaction_keeps_token_limits_visible():
    result = runner.invoke(app, ["config", "get", "cost"])

    assert result.exit_code == 0
    assert '"max_total_tokens": 0' in " ".join(result.stdout.split())


def test_repl_banner_keeps_its_colour_through_the_transcript():
    """Markup, not a pre-styled ``Text``.

    Only ``str``/``Text`` payloads reach the TUI transcript, and a ``Text`` gets
    there with its spans flattened, which is how the startup box ended up in the
    dock with every colour stripped.
    """
    from rich.text import Text

    from omni.cli.main import _repl_banner_text
    from omni.config import load_settings

    settings = load_settings(overrides={"model": {"provider": "openai", "model": "deepseek-v4-pro"}})
    markup = _repl_banner_text("default", settings)
    rendered = Text.from_markup(markup)

    assert isinstance(markup, str)
    assert "default" in rendered.plain
    assert "openai/deepseek-v4-pro" in rendered.plain
    # Styling survives parsing and no markup leaks into the visible characters.
    assert rendered.spans
    assert "[bold" not in rendered.plain and "[dim]" not in rendered.plain


def test_repl_banner_values_are_never_dimmer_than_their_labels():
    """The regression this palette exists to prevent.

    Dimming the workspace path and the whole guide line left the box's own
    border as the most legible thing inside it.
    """
    from rich.text import Text

    from omni.cli.main import _repl_banner_text
    from omni.config import load_settings

    settings = load_settings(overrides={"model": {"provider": "openai", "model": "deepseek-v4-pro"}})
    rendered = Text.from_markup(_repl_banner_text("default", settings))
    runs = [(rendered.plain[s.start : s.end], str(s.style)) for s in rendered.spans]
    dimmed = {text for text, style in runs if "dim" in style}
    accented = {text for text, style in runs if "cyan" in style}

    assert "workspace" in dimmed  # the label is the quiet part
    # The value carries no span at all: full-strength default foreground.
    assert str(settings.paths.project_dir) not in dimmed
    # Incomplete model: the one next step is setup, not /help + IM login.
    assert {"/init", "/model"} <= accented


def test_repl_banner_points_at_the_one_next_step():
    """Startup is one CTA: setup when the model is missing, ask + /web when ready."""
    from rich.text import Text

    from omni.cli.main import _repl_banner_text
    from omni.config import load_settings

    missing = load_settings(overrides={"model": {"provider": "openai", "model": "deepseek-v4-pro"}})
    missing_banner = " ".join(Text.from_markup(_repl_banner_text("default", missing)).plain.split())
    assert "/init" in missing_banner and "/model" in missing_banner
    assert "/channel login wechat" not in missing_banner
    assert "WeChat" not in missing_banner

    ready = load_settings(
        overrides={
            "model": {
                "provider": "openai",
                "model": "deepseek-v4-pro",
                "base_url": "https://example.invalid/v1",
                "api_key": "test",
            }
        }
    )
    ready_banner = " ".join(Text.from_markup(_repl_banner_text("default", ready)).plain.split())
    assert "/web" in ready_banner
    assert "/help" in ready_banner
    assert "/channel login wechat" not in ready_banner


def test_repl_detects_incomplete_real_model_config():
    from omni.cli.main import _missing_model_fields, _model_setup_commands
    from omni.config import load_settings

    settings = load_settings(overrides={"model": {"provider": "openai", "model": "deepseek-v4-pro"}})

    assert _missing_model_fields(settings.model) == ["model.base_url", "model.api_key"]
    rows = _model_setup_commands(settings.model)
    commands = [row[0] for row in rows]
    assert commands == [
        "/model main -u https://api.deepseek.com/v1",
        "/model main -k sk-xxx",
    ]


def test_repl_quickstart_rows_are_concise_with_examples():
    from omni.cli.main import _registered_typer_children, _repl_quickstart_rows

    quickstart = _repl_quickstart_rows()
    quickstart_text = "\n".join(" ".join(row) for row in quickstart)

    assert all(len(row) == 4 for row in quickstart)
    # Quickstart columns are (command, subcommand, purpose/details, key example): one row per command.
    # with its consolidated subcommand list (help last) and a runnable example.
    for command, example in (
        ("/model", "/model"),
        ("/config", "config model -p openai -u <BASE_URL> -m <MODEL> -k <API_KEY>"),
        ("/skills", "/skills examples"),
        ("/soul", "/soul list"),
        ("/task", "/task show <id>"),
        ("/channel", "/channel login wechat --start"),
        ("/serve", "/serve status"),
        ("/memory", "/memory search retrieval augmented generation"),
        ("/resume", "/resume --last"),
        ("/session", "/session list"),
        ("/project", "/project list"),
        ("/mcp", "/mcp install"),
        ("/profile", "/profile list"),
        ("/cite", "/cite list"),
        ("/lit", "/lit \"How does RAG reduce hallucination?\""),
        ("/hypo", "/hypo new"),
        ("/claim", "/claim list"),
        ("/evidence", "/evidence add"),
        ("/run", "/run list"),
        ("/source", "/source reindex"),
        ("/bench", "/bench --k 3"),
        ("/doctor", "/doctor"),
        ("/uninstall", "/uninstall --dry-run"),
        ("/init", "/init"),
        ("/web", "/web"),
    ):
        assert command in quickstart_text
        assert example in quickstart_text
    from omni.cli.commands import (
        channel_cmd,
        config_cmd,
        memory_cmd,
        skills_cmd,
        soul_cmd,
        tasks_cmd,
    )

    rows_by_command = {row[0]: row for row in quickstart}
    for command, command_app in {
        "/config": config_cmd.app,
        "/skills": skills_cmd.app,
        "/soul": soul_cmd.app,
        "/task": tasks_cmd.app,
        "/channel": channel_cmd.app,
        "/memory": memory_cmd.app,
    }.items():
        assert set(rows_by_command[command][1].split(" / ")) == set(
            _registered_typer_children(command_app)
        )
    # Values from the removed "important parameters" column must not appear in the quickstart.
    assert "key/value" not in quickstart_text
    assert "--start / --credential-store" not in quickstart_text
    # A fresh (mock) config leads with the concrete one-shot model command.
    assert "config model -p openai" in quickstart_text
    tasks_row = next(row for row in quickstart if row[0] == "/task")
    assert "--session" not in tasks_row[1]
    assert "--all" not in tasks_row[1]
    assert "session / all" in tasks_row[1]
    # Command groups that support a `help` subcommand list it LAST.
    subs = {r[0]: r[1] for r in quickstart}
    for cmd in ("/config", "/skills", "/soul", "/task", "/serve", "/channel"):
        assert subs[cmd].split(" / ")[-1] == "help"


def test_repl_help_is_concise_overview_with_slashes_and_hierarchy():
    # `/help` is a concise, one-row-per-command overview: no type column, no
    # per-subcommand rows, command groups shown WITH a leading slash. Details
    # move under each command's own `help` (a two-level hierarchy).
    from omni.cli.main import _show_repl_help
    from omni.cli.render import console

    with console.capture() as cap:
        _show_repl_help()
    out = cap.get()
    flat = "".join(out.split())  # collapse table wrapping for presence checks

    # Conversation and research groups both appear WITH a leading slash.
    assert "Conversation and workspace" in out
    assert "Research" in out
    for cmd in ("/config", "/skills", "/task", "/serve", "/channel", "/web"):
        assert cmd in out
    # The type column and verbose per-subcommand/parameter rows are gone.
    assert "类型" not in out
    assert "重要参数（tasks）" not in out
    assert "config get|set|model|test|path|unset" not in out
    assert "/skills list --group" not in out
    # The hierarchy hint points users to `<command> help` for details.
    assert "<command>help" in flat
    assert "/confighelp" in flat and "/skillshelp" in flat
    # The /init → adjust-command map is still echoed under /help.
    assert "/init settings and later adjustment commands" in out


def test_repl_quickstart_order_highlight_and_accurate_init():
    from omni.cli.main import _quickstart_row_style, _repl_quickstart_rows

    rows = _repl_quickstart_rows()
    # New order: just-ask first, setup, the model facade, then advanced config.
    assert [r[0] for r in rows[:4]] == ["Ask directly", "/init", "/model", "/config"]
    # The rest keep their relative order (spot-check a couple that follow /config).
    assert [r[0] for r in rows[4:7]] == ["/skills", "/soul", "/status"]
    session = [r[0] for r in _repl_quickstart_rows(group="session")]
    research = [r[0] for r in _repl_quickstart_rows(group="research")]
    assert "/web" in session and "/help /exit /quit" in session
    assert "/lit" in research and "/bench" in research
    assert "/web" not in research and "/lit" not in session

    # The three first-touch rows share one highlight (bold cyan) as key commands;
    # others use the default row colour.
    assert _quickstart_row_style("Ask directly") == "bold cyan"
    assert _quickstart_row_style("/init") == "bold cyan"
    assert _quickstart_row_style("/model") == "bold cyan"
    assert _quickstart_row_style("/config") == "bold cyan"
    assert _quickstart_row_style("/skills") is None

    # /init description must be accurate: it does NOT configure channels, so it
    # points at /channel login instead of claiming to set them up.
    init_row = next(r for r in rows if r[0] == "/init")
    assert "messaging channels" not in init_row[2].lower()
    assert "skill library" in init_row[2].lower()


def test_init_config_map_covers_all_items_with_adjust_commands():
    # The /init → adjust-command map is the single source of truth shared by
    # `/help` and the `omni init` re-run overview; assert it lists every item a
    # user might tweak and points at the right command (not "re-run /init").
    from omni.cli.commands.init_cmd import init_config_map_rows

    rows = init_config_map_rows()
    labels = [r[0] for r in rows]
    assert labels == [
        "Model",
        "Embedding recall",
        "Semantic Scholar",
        "Data directory",
        "Project workspace",
        "Skill library",
        "MCP registration",
        "Messaging channels",
    ]
    adjust = {r[0]: r[2] for r in rows}
    assert "model" in adjust["Model"]
    assert "model embedding" in adjust["Embedding recall"]
    assert (
        "config set research.semantic_scholar_api_key"
        in adjust["Semantic Scholar"]
    )
    assert "config home" in adjust["Data directory"]
    assert "skills add" in adjust["Skill library"] and "skills export" in adjust["Skill library"]
    assert "mcp install both" in adjust["MCP registration"]
    assert "channel login" in adjust["Messaging channels"]


def test_repl_help_shows_init_config_adjust_map():
    # `/help` must echo what /init configures and how to adjust each item later.
    from omni.cli.main import _show_repl_help
    from omni.cli.render import console

    with console.capture() as cap:
        _show_repl_help()
    out = cap.get()
    assert "/init settings and later adjustment commands" in out
    assert "config home" in out
    assert "channel login" in out


def test_init_rerun_shows_current_config_and_keeps_it_when_declined():
    # First init writes a (mock) config; running `omni init` again should show the
    # current-config overview and, when the user declines, leave config untouched.
    assert runner.invoke(app, ["init", "-y"]).exit_code == 0
    res = runner.invoke(app, ["init"], input="n\n")
    assert res.exit_code == 0
    assert "current configuration" in res.stdout
    assert "adjustment command" in res.stdout
    # Declining must NOT re-run the wizard.
    assert "Done" not in res.stdout
    # Config is unchanged (still the offline mock provider).
    prov = runner.invoke(app, ["config", "get", "model.provider"])
    assert "mock" in prov.stdout


def test_skills_help_includes_research_workflow_examples():
    from omni.cli.commands.skills_cmd import SKILL_WORKFLOW_EXAMPLES

    res = runner.invoke(app, ["skills", "examples"])
    normalized = " ".join(res.stdout.split())

    assert res.exit_code == 0
    assert "/skills examples" in normalized
    assert "one to seven capabilities" in normalized
    assert "2 capabilities" in normalized
    assert "paper.fetch.arxiv" in normalized
    assert "artifact.figure" in normalized
    assert [row[0] for row in SKILL_WORKFLOW_EXAMPLES] == [
        f"{count} capability" if count == 1 else f"{count} capabilities"
        for count in range(1, 8)
    ]
    assert all("corpus.index" not in row[2] for row in SKILL_WORKFLOW_EXAMPLES)


def test_skills_help_uses_command_group_contract():
    res = runner.invoke(app, ["skills", "help"])
    option_res = runner.invoke(app, ["skills", "--help"])
    normalized = " ".join(res.stdout.split())
    option_normalized = " ".join(option_res.stdout.split())

    assert res.exit_code == 0
    assert "skills subcommands" in normalized
    assert "Important skills options" in normalized
    assert "list" in normalized
    assert "examples" in normalized
    assert "workflow" in normalized
    assert "example" in normalized
    assert "/skills add codex:my-skill" in normalized
    assert "/skills trust my-skill --yes" in normalized
    assert "/skills list --disabled" in normalized
    assert "/skills restore scientific-figure" in normalized
    assert "/skills export codex" in normalized
    assert "arxiv-fetch + scientific-figure" not in normalized
    assert option_res.exit_code == 0
    assert "examples" in option_normalized
    assert "arxiv-fetch + scientific-figure" not in option_normalized


def test_skills_examples_command_includes_cli_and_repl_triggers():
    res = runner.invoke(app, ["skills", "examples"])

    assert res.exit_code == 0
    assert "omni -P skill-verify exec" in res.stdout
    assert "Shell and REPL execution" in res.stdout
    assert "REPL request" in res.stdout
    assert "/task show <id>" in res.stdout
    assert "1 capability" in res.stdout
    assert "7 capabilities" in res.stdout


def test_channel_help_includes_safe_placeholders_not_real_credentials():
    res = runner.invoke(app, ["channel", "help"])
    normalized = " ".join(res.stdout.split())

    assert res.exit_code == 0
    assert "/channel login feishu" in normalized
    assert "<FEISHU_APP_ID>" in res.stdout
    assert "<FEISHU_APP_SECRET>" in res.stdout
    # Where a secret is kept is worth stating; which flag to pass is not, since
    # every platform is handled without one.
    assert "secrets.toml" in normalized
    assert "--credential-store" not in normalized


def test_channel_help_advertises_exactly_one_way_to_connect_wechat():
    # WeChat has a single advertised path: scan the official ClawBot QR.
    # Help must not offer flags that recreate the removed :8088 / WeCom paths.
    res = runner.invoke(app, ["channel", "help"])
    normalized = " ".join(res.stdout.split())

    assert res.exit_code == 0
    assert "/channel login wechat --start" in normalized
    for alternative in ("--method", "--gateway-url", "wecom"):
        assert alternative not in normalized.lower()
    assert "clawbot" in normalized.lower()
    assert "no :8088 bridge" in normalized
    assert "/pair <code>" in normalized
    assert "--start" in normalized
    assert "list" in res.stdout
    assert "add <name>" in res.stdout
    assert "login <name>" in res.stdout
    assert "remove <name>" in res.stdout
    assert "test <name>" in res.stdout


def test_command_help_subcommands_are_available():
    cases = [
        (["task", "help"], ["Available subcommands", "show <id>", "attach <id>", "archive <id>", "rm/delete <id...>", "prune"]),
        (["memory", "help"], ["Available subcommands", "rm/delete/remove", "edit", "detail <id>", "path"]),
        (["config", "help"], ["Available subcommands", "model", "unset <key>"]),
        (["serve", "help"], ["Available subcommands", "start", "restart", "status"]),
        (["project", "help"], ["project subcommands", "new <name>", "info"]),
        (["profile", "help"], ["profile subcommands", "add <name>", "use <name>"]),
        (["cite", "help"], ["cite subcommands", "export", "bibtex"]),
        (["resume", "help"], ["resume usage", "resume --last", "replay <id>"]),
        (["lit", "help"], ["lit usage", "--verify", "openalex-search"]),
        (["hypo", "help"], ["Available subcommands", "status <id> <status>", "proposed"]),
        (["claim", "help"], ["Available subcommands", "show <id>"]),
        (["evidence", "help"], ["Available subcommands", "add <claim_id>"]),
        (["run", "help"], ["Available subcommands", "metrics"]),
        (["source", "help"], ["Available subcommands", "reindex"]),
    ]

    for args, expected in cases:
        res = runner.invoke(app, args)
        assert res.exit_code == 0, args
        for text in expected:
            assert text in res.stdout, (args, text)


def test_task_help_scopes_destructive_id_commands_to_current_workspace():
    result = runner.invoke(app, ["task", "help"])

    assert result.exit_code == 0
    output = cli_text(result.stdout)
    assert "rm/delete <id...>" in output
    assert "Delete task trees in the current workspace" in output


def test_command_consistency_aliases_and_preview_exit_codes():
    assert runner.invoke(app, ["task", "delete", "--help"]).exit_code == 0
    pin_help = runner.invoke(app, ["memory", "pin", "--help"])
    assert pin_help.exit_code == 0
    # Match the flag even when Rich wraps/ANSI-styles the help panel.
    pin_text = (pin_help.stdout or "") + (pin_help.output or "")
    assert "--on" in pin_text or "Pin the memory" in pin_text

    preview = runner.invoke(app, ["task", "clear", "--status", "failed"])
    assert preview.exit_code == 0
    assert "Add --yes to confirm" in preview.stdout


def test_repl_skills_remove_and_restore_refreshes_live_registry():
    from omni.cli.main import _repl_command
    from omni.cli.state import AppState, run_async
    from omni.config import load_settings
    from omni.skills_runtime.registry import SkillRegistry

    state = AppState()
    settings = load_settings()
    registry = SkillRegistry(settings)
    registry.build_index()
    agent = SimpleNamespace(registry=registry, settings=settings, paths=settings.paths)

    assert agent.registry.get("scientific-figure") is not None

    result = run_async(_repl_command(agent, state, "/skills remove scientific-figure", "session"))
    agent = result.agent
    assert agent.registry.get("scientific-figure") is None

    result = run_async(_repl_command(agent, state, "/skills restore scientific-figure", "session"))
    agent = result.agent
    assert agent.registry.get("scientific-figure") is not None


def test_repl_input_prepares_terminal_before_read(monkeypatch):
    import omni.cli.main as cli_main

    calls: list[str] = []

    class Guard:
        def prepare(self) -> None:
            calls.append("prepare")

    monkeypatch.setattr(cli_main.console, "input", lambda _prompt: calls.append("input") or "/exit")

    assert cli_main._read_repl_line(Guard()) == "/exit"
    assert calls == ["prepare", "input"]


def test_repl_input_passes_mode_to_input_box():
    import omni.cli.main as cli_main

    calls: list[str] = []

    class Guard:
        def prepare(self) -> None:
            calls.append("prepare")

    class InputBox:
        def read_line(self, *, mode: str, fallback) -> str:  # noqa: ANN001
            calls.append(f"box:{mode}")
            return "/exit"

    assert cli_main._read_repl_line(Guard(), input_box=InputBox(), mode="review") == "/exit"
    assert calls == ["prepare", "box:review"]


def test_terminal_input_guard_restores_canonical_backspace(monkeypatch):
    import copy

    import omni.cli.main as cli_main

    if cli_main.termios is None:
        import pytest

        pytest.skip("termios is not available")

    class FakeStream:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 99

    base_cc = [b"\x00"] * 32
    current = [0, 0, 0, 0, 0, 0, base_cc.copy()]
    original = [0, 0, 0, 0, 0, 0, base_cc.copy()]
    captured: list[list[object]] = []

    def fake_getattr(fd: int) -> list[object]:
        assert fd == 99
        return copy.deepcopy(original if not captured else current)

    def fake_setattr(fd: int, when: int, attrs: list[object]) -> None:
        assert fd == 99
        assert when == cli_main.termios.TCSANOW
        captured.append(copy.deepcopy(attrs))

    monkeypatch.setattr(cli_main.termios, "tcgetattr", fake_getattr)
    monkeypatch.setattr(cli_main.termios, "tcsetattr", fake_setattr)

    guard = cli_main._TerminalInputGuard(FakeStream())
    guard.prepare()

    fixed = captured[-1]
    assert fixed[3] & cli_main.termios.ECHO
    assert fixed[3] & cli_main.termios.ICANON
    assert fixed[3] & cli_main.termios.ISIG
    assert fixed[3] & cli_main.termios.ECHOE
    assert fixed[3] & cli_main.termios.ECHOK
    assert fixed[3] & getattr(cli_main.termios, "ECHOKE", 0)
    assert fixed[6][cli_main.termios.VERASE] == b"\x7f"
    assert fixed[6][cli_main.termios.VKILL] == b"\x15"

    guard.restore()
    assert captured[-1] == original


@pytest.mark.skipif(os.name == "nt", reason="PTY editing requires POSIX pty")
def test_repl_input_pty_handles_backspace_and_ctrl_u():
    import os
    import pty
    import select
    import signal
    import sys
    import textwrap
    import time

    if not hasattr(pty, "fork"):
        import pytest

        pytest.skip("pty.fork is not available")

    code = textwrap.dedent(
        r"""
        import asyncio
        import sys
        import termios
        import threading
        import time

        from omni.cli.main import _TerminalInputGuard, _read_repl_line_async
        from omni.cli.repl_input import ReplInputBox

        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[3] |= termios.ECHO | termios.ICANON | termios.ISIG | getattr(termios, "ECHOCTL", 0)
        attrs[3] &= ~(
            termios.ECHOE
            | termios.ECHOK
            | getattr(termios, "ECHOKE", 0)
        )

        def set_cc(index, value):
            attrs[6][index] = value if isinstance(attrs[6][index], int) else bytes([value])

        set_cc(termios.VERASE, 0x08)
        set_cc(termios.VKILL, 0x00)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

        def background_notice():
            time.sleep(0.15)
            print("BACKGROUND-NOTICE")

        threading.Thread(target=background_notice, daemon=True).start()

        line = asyncio.run(
            _read_repl_line_async(
                _TerminalInputGuard(),
                input_box=ReplInputBox(enabled=True),
                mode="review",
            )
        )
        print("RESULT:" + line)
        """
    )
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", code])

    buf = b""
    try:
        deadline = time.time() + 10
        sent = False
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            buf += chunk
            if not sent and b"BACKGROUND-NOTICE" in buf:
                # A physical Return key sends CR. LF is Ctrl+J and deliberately
                # inserts chat:newline under the Claude Code input contract.
                os.write(fd, b"ab\x7fcd\x15ok\x0c-done\r")
                sent = True
            if b"RESULT:" in buf:
                break
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)

    rendered = buf.decode("utf-8", "replace")
    assert b"^?" not in buf
    assert "BACKGROUND-NOTICE" in rendered
    # This PTY test owns line-editing behavior. The mode toolbar is tested
    # deterministically in test_repl_input_box_frame_and_toolbar_reflect_mode;
    # asserting it here races prompt_toolkit's first paint against the notice.
    assert "RESULT:ok-done" in rendered


@pytest.mark.skipif(os.name == "nt", reason="PTY editing requires POSIX pty")
def test_repl_input_pty_ignores_blank_enter_before_accepting_text():
    import os
    import pty
    import select
    import signal
    import sys
    import textwrap
    import time

    if not hasattr(pty, "fork"):
        import pytest

        pytest.skip("pty.fork is not available")

    code = textwrap.dedent(
        """
        import asyncio

        from omni.cli.main import _TerminalInputGuard, _read_repl_line_async
        from omni.cli.repl_input import ReplInputBox

        line = asyncio.run(
            _read_repl_line_async(
                _TerminalInputGuard(),
                input_box=ReplInputBox(enabled=True),
                mode="auto",
            )
        )
        print("RESULT:" + line)
        """
    )
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", code])

    buf = b""
    try:
        deadline = time.time() + 10
        sent = False
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            buf += chunk
            if not sent and b"\xe2\x80\xba " in buf:
                # The prompt can be painted just before prompt_toolkit enters
                # raw mode; emulate a person rather than racing that boundary.
                time.sleep(0.1)
                os.write(fd, b"\r   \raccepted\r")
                sent = True
            if b"RESULT:" in buf:
                break
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)

    output = buf.decode("utf-8", "replace")
    assert "RESULT:accepted" in output


_ANSI_SEQUENCE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _strip_ansi(text: str) -> str:
    """Read a PTY stream as what it displays rather than as what it emits."""
    return _ANSI_SEQUENCE.sub("", text)


@pytest.mark.skipif(os.name == "nt", reason="PTY rendering requires POSIX pty")
def test_repl_tui_pty_stays_in_normal_buffer_without_mouse_capture():
    """IK3MN1 regression: the inline dock never enters the alternate screen and
    never enables mouse reporting, so drag-select/copy and native scrollback keep
    working. Committed history is streamed to the normal buffer and survives exit.
    """
    import os
    import pty
    import select
    import signal
    import sys
    import textwrap
    import time

    if not hasattr(pty, "fork"):
        import pytest

        pytest.skip("pty.fork is not available")

    code = textwrap.dedent(
        """
        import asyncio

        from omni.cli.repl_tui import ReplTui

        async def run():
            tui = ReplTui(commands=())
            await tui.start()
            try:
                tui.append_output("PERSISTENT-HISTORY-LINE\\n")
                await asyncio.sleep(0.1)
                async with tui.suspended():
                    print("EXTERNAL-CONTROL", flush=True)
                async def delayed_output():
                    await asyncio.sleep(0.5)
                    tui.append_output("NEW-OUTPUT\\n")

                notification = asyncio.create_task(delayed_output())
                line = await tui.read_line_async(mode="auto", fallback=lambda: "fallback")
                await notification
            finally:
                await tui.close()
            print("RESULT:" + line)

        asyncio.run(run())
        """
    )
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", code])

    buf = b""
    try:
        deadline = time.time() + 10
        sent = False
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            buf += chunk
            # Match the footer on its text, not its bytes: the hint strip styles
            # the key separately from its label, so "Enter" and " send" arrive
            # with a style change between them.
            if not sent and "Enter send" in _strip_ansi(buf.decode("utf-8", "replace")):
                # A terminal Return key sends CR; LF represents Ctrl+J in the
                # input parser and should not be used to emulate Enter. Blank/
                # whitespace-only Enters must be ignored before "accepted".
                os.write(fd, b"\r  \raccepted\r")
                sent = True
            if b"RESULT:" in buf:
                break
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)

    output = buf.decode("utf-8", "replace")
    plain = _strip_ansi(output)
    assert "RESULT:accepted" in plain
    assert "EXTERNAL-CONTROL" in plain
    assert "auto mode" in plain
    assert "Enter send" in plain
    # Committed history reaches the normal buffer (native, selectable scrollback)
    # and is still present in the emitted stream after the app exits.
    assert "PERSISTENT-HISTORY-LINE" in plain
    assert "NEW-OUTPUT" in plain
    # IK3MN1 root cause must not regress: no alternate-screen switch and no mouse
    # tracking enable sequences are ever emitted.
    assert "\x1b[?1049h" not in output  # alternate screen buffer
    assert "\x1b[?1047h" not in output  # legacy alternate screen
    assert "\x1b[?1000h" not in output  # X11 mouse tracking
    assert "\x1b[?1002h" not in output  # button-event mouse tracking
    assert "\x1b[?1006h" not in output  # SGR extended mouse mode


@pytest.mark.skipif(os.name == "nt", reason="PTY resize requires POSIX pty/fcntl")
def test_repl_tui_pty_reflows_and_does_not_duplicate_dock_on_width_resize():
    """Regression for the tmux resize artifacts: a *width* change clears the
    screen + scrollback (``ESC[3J``) and re-emits committed history at the new
    width in one pass (Codex reflow-by-re-emit), instead of leaving stale
    duplicate composer/meta boxes behind and freezing the old wrapping.
    """
    import fcntl
    import os
    import pty
    import select
    import signal
    import struct
    import sys
    import termios
    import textwrap
    import time

    if not hasattr(pty, "fork"):
        import pytest

        pytest.skip("pty.fork is not available")

    code = textwrap.dedent(
        """
        import asyncio

        from omni.cli.repl_tui import ReplTui

        async def run():
            tui = ReplTui(commands=())
            await tui.start()
            try:
                tui.append_output("RESIZE-HISTORY-LINE\\n")
                await asyncio.sleep(2.5)
            finally:
                await tui.close()
            print("RESULT:done", flush=True)

        asyncio.run(run())
        """
    )
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", code])

    def set_winsize(rows: int, cols: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    set_winsize(24, 80)  # baseline width the poller records before any resize

    buf = b""
    try:
        deadline = time.time() + 12
        resized = False
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                chunk = os.read(fd, 8192)
                if not chunk:
                    break
                buf += chunk
            # Once history has been committed at width 80, widen the window; the
            # width watcher must clear scrollback and re-emit at the new width.
            if not resized and b"RESIZE-HISTORY-LINE" in buf:
                time.sleep(0.3)
                set_winsize(24, 120)
                resized = True
            if b"RESULT:done" in buf:
                break
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)

    output = buf.decode("utf-8", "replace")
    # The reflow cleared scrollback + screen (Codex ``clear_scrollback...``)...
    assert "\x1b[3J" in output
    # ...and re-emitted the committed line — so it appears again after the resize,
    # not frozen at the old width nor stacked behind a duplicated dock.
    assert output.count("RESIZE-HISTORY-LINE") >= 2
    # Still never leaves the normal buffer / captures the mouse.
    assert "\x1b[?1049h" not in output
    assert "\x1b[?1000h" not in output


@pytest.mark.skipif(os.name == "nt", reason="PTY idle checks require POSIX pty/fcntl")
def test_repl_tui_pty_is_idle_quiescent_after_output_settles():
    """Phase 1a: once output settles the idle dock writes nothing (no periodic
    refresh), so the terminal keeps a native selection highlighted for Cmd+C.
    The old ``refresh_interval=0.5`` would have emitted repaints in this window."""
    import fcntl
    import os
    import pty
    import select
    import signal
    import struct
    import sys
    import termios
    import textwrap
    import time

    if not hasattr(pty, "fork"):
        import pytest

        pytest.skip("pty.fork is not available")

    rows, cols = 24, 80
    code = textwrap.dedent(
        """
        import asyncio

        from omni.cli.repl_tui import ReplTui

        async def run():
            tui = ReplTui(commands=())
            await tui.start()
            try:
                tui.append_output("IDLE-MARKER\\n")
                await asyncio.sleep(5.0)
            finally:
                await tui.close()

        asyncio.run(run())
        """
    )
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", code])

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def answer_cpr(chunk: bytes) -> None:
        # Emulate a terminal's cursor-position report so prompt_toolkit learns the
        # dock geometry (and never prints its "CPR unsupported" warning mid-window).
        if b"\x1b[6n" in chunk:
            os.write(fd, f"\x1b[{rows};1R".encode())

    buf = b""
    idle_bytes = None
    try:
        deadline = time.time() + 10
        settle_until = None
        measure_until = None
        base_len = 0
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                answer_cpr(chunk)
            now = time.time()
            if settle_until is None and b"IDLE-MARKER" in buf:
                settle_until = now + 1.0  # let the commit + repaint burst finish
            if settle_until is not None and measure_until is None and now >= settle_until:
                base_len = len(buf)
                measure_until = now + 1.3  # quiet window in which nothing should emit
            if measure_until is not None and now >= measure_until:
                idle_bytes = len(buf) - base_len
                break
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)

    assert idle_bytes is not None, "never reached the idle measurement window"
    assert idle_bytes == 0  # a quiescent dock emits nothing while idle


@pytest.mark.skipif(os.name == "nt", reason="PTY folding requires POSIX pty/fcntl")
def test_repl_tui_pty_default_fold_expands_inline_without_alternate_screen():
    """Default-collapsed help expands in the normal buffer without a nested app."""
    import fcntl
    import os
    import pty
    import select
    import signal
    import struct
    import sys
    import termios
    import textwrap
    import time

    if not hasattr(pty, "fork"):
        import pytest

        pytest.skip("pty.fork is not available")

    rows, cols = 24, 80
    code = textwrap.dedent(
        """
        import asyncio

        from omni.cli.repl_tui import (
            DataTableData,
            ReplTui,
            TranscriptEvent,
            TranscriptKind,
        )

        async def run():
            tui = ReplTui(commands=())
            await tui.start()
            try:
                tui.publish_event(
                    TranscriptEvent(
                        kind=TranscriptKind.DATA_TABLE,
                        payload=DataTableData(
                            title="FOLD-HISTORY",
                            columns=("command", "description"),
                            rows=tuple(
                                (f"/command-{i}", f"body-{i}") for i in range(40)
                            ),
                        ),
                        foldable=True,
                        initially_collapsed=True,
                    )
                )
                await asyncio.sleep(3.0)
            finally:
                await tui.close()
            print("RESULT:done", flush=True)

        asyncio.run(run())
        """
    )
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", code])

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    buf = b""
    toggled = False
    last_activity = time.time()
    try:
        deadline = time.time() + 12
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            now = time.time()
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                last_activity = now
                if b"\x1b[6n" in chunk:
                    os.write(fd, f"\x1b[{rows};1R".encode())
            # Press Ctrl+T only once the dock has gone idle-quiescent (no output
            # for 0.8s). While a commit's ``run_in_terminal`` is in flight the dock
            # briefly restores cooked mode, and on macOS the tty then treats Ctrl+T
            # (VSTATUS) as a status request and swallows the byte. A real user
            # presses Ctrl+T against a settled dock, which this reproduces; it also
            # exercises the Phase 1a idle-quiescence guarantee (a quiet window
            # exists at all).
            if (
                not toggled
                and b"Ctrl+T to expand" in buf
                and (now - last_activity) > 0.8
            ):
                os.write(fd, b"\x14")
                toggled = True
            if toggled and b"body-20" in buf and b"RESULT:done" in buf:
                break
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)

    output = buf.decode("utf-8", "replace")
    assert "Ctrl+T to expand" in output
    assert "body-20" in output
    assert "\x1b[?1049h" not in output
    assert "\x1b[?1049l" not in output
    assert "\x1b[?1000h" not in output


def test_repl_slash_external_command_args():
    from omni.cli.main import _external_repl_command_args, _external_requires_terminal
    from omni.cli.state import AppState

    assert _external_repl_command_args(AppState(project="p"), "/doctor") == [
        "--project", "p", "doctor"
    ]
    assert _external_repl_command_args(AppState(profile="dev", model="m"), "/project list") == [
        "--profile", "dev", "--model", "m", "project", "list"
    ]
    assert _external_requires_terminal(["doctor"]) is False
    assert _external_requires_terminal(["skills", "list"]) is False
    assert _external_requires_terminal(["skills", "list", "--no-pager"]) is False
    assert _external_requires_terminal(["skills", "list", "--pager"]) is True
    assert _external_requires_terminal(["config", "model"]) is False
    assert _external_requires_terminal(["channel", "login", "wechat"]) is True
    assert _external_requires_terminal(["task", "watch"]) is True
    assert _external_requires_terminal(["serve"]) is True
    assert _external_requires_terminal(["serve", "status"]) is False
    assert _external_requires_terminal(["mcp", "serve"]) is True


def test_render_tasks_shows_artifacts_and_next_actions():
    from types import SimpleNamespace

    from omni.cli.render import console
    from omni.cli.runner import render_tasks

    turn = SimpleNamespace(
        task_id="task123456789",
        session_id="sess123456789",
        submitted_subtask_ids=["task123456789"],
        drained_results=[
            {
                "subtask_id": "task123456789",
                "skill": "scientific-figure",
                "status": "succeeded",
                "result": {
                    "summary": "已生成 Transformer 架构图。",
                    "artifacts": [
                        {
                            "title": "PNG",
                            "uri": "artifact://png",
                            "path": "/tmp/transformer.png",
                            "mime": "image/png",
                        },
                        {
                            "title": "DOT source",
                            "uri": "artifact://dot",
                            "path": "/tmp/transformer.dot",
                            "mime": "text/vnd.graphviz",
                        },
                    ],
                    "run_id": "runabcdef",
                    "research": {"source_ids": ["src12345"], "claim_ids": ["claim123"]},
                },
                "trace": [{"stage": "tool.start", "tool": "bash"}],
            }
        ],
    )

    with console.capture() as capture:
        render_tasks(turn, shell_commands=True)
    out = capture.get()

    assert "Artifacts" in out
    assert "/tmp/transformer.png" in out
    assert "DOT source" not in out
    assert "/tmp/transformer.dot" not in out
    assert "artifact://dot" not in out
    assert "omni task show task1234" in out
    assert "omni task attach task1234 --session sess1234" in " ".join(out.split())
    assert "runabcde" in out


def test_canonical_outputs_do_not_remove_small_report_preview(tmp_path):
    from types import SimpleNamespace

    from omni.cli.render import console
    from omni.cli.runner import render_tasks
    from omni.runtime.presentation import ArtifactRef

    report = tmp_path / "review.md"
    report.write_text("# Review body\n\nGrounded conclusion.", encoding="utf-8")
    artifact = ArtifactRef(
        title="Review",
        format="md",
        uri="artifact://review",
        path=str(report),
        mime="text/markdown",
    )
    turn = SimpleNamespace(
        task_id="task123456789",
        session_id="sess123456789",
        submitted_workflow_ids=[],
        submitted_subtask_ids=["execution123456789"],
        artifacts=[artifact],
        drained_results=[
            {
                "subtask_id": "execution123456789",
                "task_id": "task123456789",
                "skill": "paper-review",
                "status": "succeeded",
                "result": {
                    "summary": "Review complete.",
                    "artifacts": [artifact.to_dict()],
                },
                "trace": [],
            }
        ],
    )

    with console.capture() as capture:
        render_tasks(turn, shell_commands=True, artifacts_dir=tmp_path)

    out = capture.get()
    assert "Review body" in out
    assert "Grounded conclusion" in out
    assert str(report) not in out  # the top-level Outputs inventory owns the path


def test_render_tasks_uses_task_id_for_workflow_completion_actions():
    from omni.cli.render import console
    from omni.cli.runner import render_tasks

    turn = SimpleNamespace(
        task_id="05571218b61b4f1aab86fd83a660c75e",
        session_id="sess123456789",
        submitted_workflow_ids=["f4902f1686924dd9a74efa920bbc6626"],
        submitted_subtask_ids=[],
        drained_results=[
            {
                "workflow_run_id": "f4902f1686924dd9a74efa920bbc6626",
                "kind": "workflow",
                "status": "succeeded",
                "result": {"summary": "Workflow complete."},
                "trace": [],
            }
        ],
        text="",
        kind="workflow",
        terminated_reason="workflow",
        plan_summary="",
        degraded_warnings=[],
        settlement_status="passed",
    )

    with console.capture() as capture:
        render_tasks(turn, shell_commands=True)
    output = " ".join(capture.get().split())

    assert "workflow=f4902f16 task=05571218" in output
    assert "omni task show 05571218" in output
    assert "omni task attach 05571218 --session sess1234" in output
    assert "omni task show f4902f16 (inspect this workflow run)" in output
    assert "omni task show  (inspect" not in output


def test_render_tasks_labels_pending_workflow_and_uses_canonical_task_actions():
    from omni.cli.render import console
    from omni.cli.runner import render_tasks

    turn = SimpleNamespace(
        task_id="05571218b61b4f1aab86fd83a660c75e",
        session_id="sess123456789",
        submitted_workflow_ids=["f4902f1686924dd9a74efa920bbc6626"],
        submitted_subtask_ids=[],
        drained_results=[],
        tool_trace=[],
        text="Workflow submitted.",
        kind="workflow",
        terminated_reason="workflow",
        plan_summary="",
        degraded_warnings=[],
        settlement_status="pending_child_task",
    )

    with console.capture() as capture:
        render_tasks(turn, shell_commands=True)
    output = " ".join(capture.get().split())

    assert "Submitted workflow runs: f4902f16" in output
    assert "Submitted background tasks" not in output
    assert "omni task show 05571218" in output
    assert "omni task attach 05571218 --session sess1234" in output
    assert "omni task show f4902f16 (inspect this workflow run)" in output


def test_shell_action_drops_task_commands_without_an_identifier():
    from omni.cli.runner import _shell_action

    assert _shell_action("/task show :", session_id="session") == ""
    assert _shell_action("/task attach :", session_id="session") == ""


def test_tasks_show_artifact_list_hides_dot_sources():
    from omni.cli.commands.tasks_cmd import _print_artifacts, _result_artifacts
    from omni.cli.render import console

    artifacts = _result_artifacts(
        {
            "artifacts": [
                {"title": "PNG", "path": "/tmp/figure.png", "uri": "artifact://png"},
                {"title": "DOT source", "path": "/tmp/figure.dot", "uri": "artifact://dot"},
            ]
        }
    )

    assert artifacts == [("PNG", "/tmp/figure.png", "artifact://png")]
    with console.capture() as capture:
        _print_artifacts(artifacts)
    out = capture.get()
    assert "PNG: /tmp/figure.png" in out
    assert "artifact://png" not in out
    assert "figure.dot" not in out


def test_render_inbox_uses_canonical_task_id_and_keeps_object_reference(tmp_path, monkeypatch):
    from omni.cli.commands import tasks_cmd
    from omni.config.paths import OmniPaths

    rendered: dict[str, object] = {}

    def capture_table(title, columns, rows, **kwargs):  # noqa: ANN001
        rendered.update(title=title, columns=columns, rows=rows, options=kwargs)

    monkeypatch.setattr(tasks_cmd, "data_table", capture_table)
    task_id = "05571218b61b4f1aab86fd83a660c75e"
    workflow_id = "f4902f1686924dd9a74efa920bbc6626"
    paths = OmniPaths(
        home=tmp_path / ".omni",
        project_name="inbox-identity",
        project_dir=tmp_path / "inbox-identity",
    )
    note = {
        "task_id": task_id,
        "object_kind": "workflow_run",
        "object_id": workflow_id,
        "subtask_id": "",
        "skill_name": "workflow",
        "status": "running",
        "summary": "done",
        "created_at": "2026-07-29T10:00:00+00:00",
    }

    tasks_cmd.render_inbox(paths, notes=[note], statuses={task_id: "succeeded"})

    assert rendered["title"] == "Notification inbox"
    assert "task" in rendered["columns"]
    assert "object" in rendered["columns"]
    row = rendered["rows"][0]
    assert task_id[:8] in row
    assert f"workflow:{workflow_id[:8]}" in row
    assert "succeeded" in row


def test_status_glyph_falls_back_for_cp1252_stream():
    import io

    from rich.console import Console

    from omni.cli.render import _status_glyph
    from omni.cli.repl_output import RoutedTextIO

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    target = Console(file=stream)
    assert _status_glyph(target, "✓", "+") == "+"
    assert _status_glyph(target, "✗", "X") == "X"
    routed = RoutedTextIO(lambda: stream)
    assert routed.write("✓ 中文") == len("✓ 中文")
    routed.flush()
    assert raw.getvalue().decode("cp1252") == "? ??"


def test_render_subtask_detail_defaults_to_workflow_view_and_json_is_explicit():
    from datetime import UTC, datetime

    from omni.cli.commands.tasks_cmd import (
        _subtask_attachment_context,
        render_subtask_detail,
        render_subtask_json,
        render_workflow_detail,
        render_workflow_json,
        render_workflow_step_detail,
        render_workflow_step_json,
    )
    from omni.cli.render import console
    from omni.storage.models import SubtaskORM, WorkflowRunORM, WorkflowStepORM

    workflow = WorkflowRunORM(
        id="workflow123456789",
        task_id="task123456789",
        status="degraded",
        goal="research workflow",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        result_json={
            "status": "degraded",
            "summary": "工作流部分完成：3/4 个 skill 成功",
        },
        trace_log=[{"stage": "workflow.step.start", "step_id": "lit"}],
    )
    lit = WorkflowStepORM(
        id="step-lit-123456789",
        workflow_run_id=workflow.id,
        task_id=workflow.task_id,
        step_key="lit",
        position=1,
        skill_name="literature-search",
        status="succeeded",
        result_json={"summary": "found papers"},
    )
    diagram = WorkflowStepORM(
        id="step-diagram-123456789",
        workflow_run_id=workflow.id,
        task_id=workflow.task_id,
        step_key="diagram",
        position=2,
        skill_name="scientific-figure",
        status="failed",
        input_json={"figure": "RAG architecture"},
        result_json={"error": "Graphviz unavailable"},
        error="Graphviz unavailable",
        recoverable=True,
        current_execution_id="execution123456789",
        execution_ids=["execution123456789"],
    )
    execution = SubtaskORM(
        id="execution123456789",
        task_id=workflow.task_id,
        workflow_run_id=workflow.id,
        workflow_step_id=diagram.id,
        skill_name="scientific-figure",
        status="failed",
        error="Graphviz unavailable",
    )

    with console.capture() as capture:
        render_workflow_detail(workflow, [lit, diagram], [execution])
    view = capture.get()
    compact_view = "".join(view.split())
    assert "workflow steps" in view
    assert "object_kind" in view
    assert "workflow_run" in view
    assert "object_id" in view
    assert workflow.id in view
    assert "task_id" in view
    assert workflow.task_id in view
    assert "literature-search" in compact_view
    assert "scientific-figure" in compact_view
    assert '"goal"' not in view
    assert '"input_json"' not in view
    assert '"result_json"' not in view
    assert "/task show workflow --json" in view
    assert f"/task show {workflow.task_id[:8]}" in view

    with console.capture() as capture:
        render_workflow_json(workflow, [lit, diagram], [execution])
    raw = json.loads(capture.get())
    assert raw["object_kind"] == "workflow_run"
    assert raw["object_id"] == workflow.id
    assert raw["task_id"] == workflow.task_id
    assert "trace_log" in raw
    assert "plan_json" in raw
    assert raw["error"] == ""

    with console.capture() as capture:
        render_workflow_step_detail(workflow, diagram, execution)
    step_view = capture.get()
    assert "Workflow step diagram" in step_view
    assert "task" in step_view
    assert "task123456789" in step_view
    assert "Graphviz unavailable" in step_view
    assert "/task retry workflow --step diagram" in step_view
    assert "resume in place: /task resume" in " ".join(step_view.split())
    assert "workflow --step diagram" in step_view
    assert f"full task: /task show {workflow.task_id[:8]}" in " ".join(
        step_view.split()
    )

    with console.capture() as capture:
        render_workflow_step_json(diagram, execution)
    step_raw = json.loads(capture.get())
    assert step_raw["object_kind"] == "workflow_step"
    assert step_raw["object_id"] == diagram.id
    assert step_raw["task_id"] == workflow.task_id
    assert step_raw["step_id"] == "diagram"

    with console.capture() as capture:
        render_subtask_detail(execution)
    execution_view = capture.get()
    assert "object_kind" in execution_view
    assert "skill_execution" in execution_view
    assert execution.id in execution_view
    assert workflow.task_id in execution_view
    assert f"Full task: /task show {workflow.task_id[:8]}" in " ".join(
        execution_view.split()
    )

    with console.capture() as capture:
        render_subtask_json(execution)
    execution_raw = json.loads(capture.get())
    assert execution_raw["object_kind"] == "skill_execution"
    assert execution_raw["object_id"] == execution.id
    assert execution_raw["task_id"] == workflow.task_id

    attachment = _subtask_attachment_context(execution)
    assert (
        f"Skill execution {execution.id} (owning Task {workflow.task_id})"
        in attachment
    )


def _seed_task_rows(project: str, rows: list[dict], *, with_children: bool = False) -> None:
    """Seed top-level TaskORM rows (user requests) for task-level command tests.

    With ``with_children`` each task also gets one subtask + one event so the
    cascade delete (task → subtasks/events) can be asserted.
    """
    from omni.cli.state import AppState, make_agent, run_async
    from omni.storage.models import SubtaskORM, TaskEventORM, TaskORM

    state = AppState(project=project)

    async def _run():
        agent = await make_agent(state)
        try:
            async with agent.db.session() as s:
                for r in rows:
                    payload = {"project": project, "channel": "cli", "kind": "turn", **r}
                    s.add(TaskORM(**payload))
                await s.flush()
                if with_children:
                    for r in rows:
                        s.add(SubtaskORM(
                            id=f"sub-{r['id']}"[:40], task_id=r["id"],
                            skill_name="x", status="succeeded",
                        ))
                        s.add(TaskEventORM(
                            id=f"evt-{r['id']}"[:40], task_id=r["id"],
                            seq=1, event_type="task.ack",
                        ))
                await s.commit()
        finally:
            await agent.aclose()

    run_async(_run())


async def _task_and_subtask_exist(project: str, task_id: str) -> tuple[bool, bool]:
    from omni.cli.state import AppState, make_agent
    from omni.storage.models import SubtaskORM, TaskORM

    agent = await make_agent(AppState(project=project))
    try:
        async with agent.db.session() as s:
            task = await s.get(TaskORM, task_id)
            sub = await s.get(SubtaskORM, f"sub-{task_id}"[:40])
            return task is not None, sub is not None
    finally:
        await agent.aclose()


async def _existing_task_ids(project: str, task_ids: list[str]) -> set[str]:
    """Return the exact task ids still present in one test workspace."""
    from omni.cli.state import AppState, make_agent
    from omni.storage.models import TaskORM

    agent = await make_agent(AppState(project=project))
    try:
        async with agent.db.session() as s:
            return {
                task_id
                for task_id in task_ids
                if await s.get(TaskORM, task_id) is not None
            }
    finally:
        await agent.aclose()


def _seed_tasks_for_project(project: str, rows: list[dict]) -> str:
    from omni.cli.state import AppState, make_agent, run_async
    from omni.storage.models import SubtaskORM, TaskORM

    state = AppState(project=project)
    task_id = f"run-{project}"[:40]

    async def _run():
        agent = await make_agent(state)
        try:
            async with agent.db.session() as s:
                s.add(TaskORM(
                    id=task_id,
                    session_id="seed-session",
                    project=project,
                    channel="cli",
                    status="succeeded",
                    title=f"{project} seed run",
                    user_input=f"{project} seed run",
                    submitted_subtask_ids=[r["id"] for r in rows],
                    current_subtask_id=rows[-1]["id"] if rows else "",
                ))
                await s.flush()
                for r in rows:
                    r = {"session_id": "seed-session", "task_id": task_id, **r}
                    s.add(SubtaskORM(**r))
                await s.commit()
        finally:
            await agent.aclose()

    run_async(_run())
    return task_id


def _seed_failed_workflow_for_project(project: str) -> tuple[str, str]:
    """Seed the workflow object graph used by recovery CLI tests."""
    from omni.agent.plan_revision import (
        queued_workflow_authority,
        runtime_provider_authority_snapshot,
    )
    from omni.cli.state import AppState, make_agent, run_async
    from omni.storage.models import SubtaskORM, TaskORM, WorkflowRunORM, WorkflowStepORM

    task_id = f"run-{project}"[:40]
    workflow_id = "workflowaa"

    async def _run() -> None:
        agent = await make_agent(AppState(project=project))
        try:
            def provider_authority(
                skill_name: str,
                step_id: str,
            ) -> dict:
                entry = agent.registry.resolve_ref(skill_name, "")
                authority = runtime_provider_authority_snapshot(
                    agent.registry,
                    entry,
                )
                authority.update(
                    consumer_kind="workflow_step",
                    consumer_id=step_id,
                    provider_name=skill_name,
                    provider_source=str(
                        getattr(entry, "source", "") or ""
                    ),
                )
                return authority

            lit_authority = provider_authority("arxiv-fetch", "lit")
            diagram_authority = provider_authority(
                "scientific-figure",
                "diagram",
            )
            plan_steps = [
                {
                    "id": "lit",
                    "skill": "arxiv-fetch",
                    "skill_name": "arxiv-fetch",
                    "input": {},
                },
                {
                    "id": "diagram",
                    "skill": "scientific-figure",
                    "skill_name": "scientific-figure",
                    "depends_on": ["lit"],
                    "input": {"input": "RAG architecture"},
                },
            ]
            async with agent.db.session() as session:
                session.add(TaskORM(
                    id=task_id,
                    session_id="seed-session",
                    project=project,
                    channel="cli",
                    status="degraded",
                    title=f"{project} seed run",
                    user_input=f"{project} seed run",
                    submitted_workflow_ids=[workflow_id],
                    current_workflow_id=workflow_id,
                ))
                await session.flush()
                session.add(WorkflowRunORM(
                    id=workflow_id,
                    task_id=task_id,
                    session_id="seed-session",
                    project=project,
                    status="degraded",
                    goal="RAG workflow",
                    plan_json={"steps": plan_steps},
                    execution_authority_json=queued_workflow_authority(
                        [lit_authority, diagram_authority]
                    ),
                    result_json={"status": "degraded", "summary": "workflow has one failed step"},
                ))
                await session.flush()
                lit = WorkflowStepORM(
                    id="workflow-step-lit",
                    workflow_run_id=workflow_id,
                    task_id=task_id,
                    step_key="lit",
                    position=1,
                    skill_name="arxiv-fetch",
                    status="succeeded",
                    provider_authority_json=lit_authority,
                    result_json={"summary": "found papers"},
                )
                diagram = WorkflowStepORM(
                    id="workflow-step-diagram",
                    workflow_run_id=workflow_id,
                    task_id=task_id,
                    step_key="diagram",
                    position=2,
                    skill_name="scientific-figure",
                    status="failed",
                    input_json={"input": "RAG architecture"},
                    provider_authority_json=diagram_authority,
                    result_json={"error": "Graphviz unavailable"},
                    error="Graphviz unavailable",
                    recoverable=True,
                    current_execution_id="execution-attempt-one",
                    execution_ids=["execution-attempt-one"],
                )
                session.add_all([lit, diagram])
                await session.flush()
                session.add(SubtaskORM(
                    id="execution-attempt-one",
                    session_id="seed-session",
                    task_id=task_id,
                    workflow_run_id=workflow_id,
                    workflow_step_id=diagram.id,
                    project=project,
                    skill_name="scientific-figure",
                    status="failed",
                    input_json={"input": "RAG architecture"},
                    provider_authority_json=diagram_authority,
                    result_json={"error": "Graphviz unavailable"},
                    error="Graphviz unavailable",
                    original_error="Graphviz unavailable",
                ))
                await session.commit()
        finally:
            await agent.aclose()

    run_async(_run())
    return task_id, workflow_id


def test_task_show_distinguishes_task_and_workflow_views_and_json():
    project = "task-workflow-object-identity"
    task_id, workflow_id = _seed_failed_workflow_for_project(project)

    task_view = runner.invoke(app, ["--project", project, "task", "show", task_id])
    workflow_view = runner.invoke(app, ["--project", project, "task", "show", workflow_id])

    assert task_view.exit_code == 0
    assert workflow_view.exit_code == 0
    assert f"Task {task_id[:8]}" in task_view.stdout
    assert "object_kind" in task_view.stdout
    assert "task" in task_view.stdout
    assert f"Workflow {workflow_id[:8]}" in workflow_view.stdout
    assert "object_kind" in workflow_view.stdout
    assert "workflow_run" in workflow_view.stdout
    assert f"Full task: /task show {task_id[:8]}" in " ".join(workflow_view.stdout.split())
    assert task_view.stdout != workflow_view.stdout

    task_json = runner.invoke(
        app, ["--project", project, "task", "show", task_id, "--json"]
    )
    workflow_json = runner.invoke(
        app, ["--project", project, "task", "show", workflow_id, "--json"]
    )

    assert task_json.exit_code == 0
    assert workflow_json.exit_code == 0
    task_payload = json.loads(task_json.stdout)
    workflow_payload = json.loads(workflow_json.stdout)
    assert task_payload["object_kind"] == "task"
    assert task_payload["object_id"] == task_id
    assert task_payload["task_id"] == task_id
    assert workflow_payload["object_kind"] == "workflow_run"
    assert workflow_payload["object_id"] == workflow_id
    assert workflow_payload["task_id"] == task_id
    assert task_payload != workflow_payload


def test_current_command_shows_active_artifact_focus():
    from omni.cli.state import AppState, make_agent, run_async
    from omni.storage.models import SubtaskORM

    project = "current-focus"

    async def _seed() -> str:
        state = AppState(project=project)
        agent = await make_agent(state)
        try:
            session_id = await agent.ensure_session(channel="cli", external_key="cli-focus")
            source = agent.paths.artifacts_dir / "figure" / "rag_focus.dot"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('digraph G { graph [label="RAG Architecture"]; q -> r }', encoding="utf-8")
            result = {
                "summary": "RAG Architecture",
                "artifacts": [{"title": "RAG DOT", "format": "dot", "path": str(source)}],
            }
            async with agent.db.session() as s:
                task = SubtaskORM(
                    skill_name="scientific-figure",
                    status="succeeded",
                    session_id=session_id,
                    result_json=result,
                )
                s.add(task)
                await s.commit()
                await s.refresh(task)
                subtask_id = task.id
            await agent.focus.record_skill_execution_result(
                session_id=session_id,
                skill_execution_id=subtask_id,
                skill_name="scientific-figure",
                result=result,
                origin="test",
            )
            return session_id
        finally:
            await agent.aclose()

    session_id = run_async(_seed())

    res = runner.invoke(app, ["--project", project, "current", "--session", session_id[:8]])

    assert res.exit_code == 0
    assert "current focus" in res.stdout
    assert "scientific-figure" in res.stdout
    assert "RAG DOT" in res.stdout
    assert "rag_focus.dot" in "".join(res.stdout.split())


def test_why_command_shows_route_arbitration_event():
    from omni.cli.state import AppState, make_agent, run_async

    project = "why-route"

    async def _seed() -> str:
        state = AppState(project=project)
        agent = await make_agent(state)
        try:
            session_id = await agent.ensure_session(channel="cli", external_key="cli-why")
            run = await agent.tasks.create_task(
                session_id=session_id,
                channel="cli",
                user_input="生成一个 RAG 架构图",
            )
            await agent.tasks.append_event(
                run.id,
                event_type="route.arbitration",
                status="succeeded",
                name="artifact_route",
                output_json={"decision": "create_new", "reason": "strong create verb"},
                summary="selected new artifact route",
            )
            return run.id
        finally:
            await agent.aclose()

    task_id = run_async(_seed())

    res = runner.invoke(app, ["--project", project, "why", task_id[:8]])

    assert res.exit_code == 0
    assert "why task" in res.stdout
    assert "route.arbitration" in res.stdout
    assert "create_new" in res.stdout
    assert "selected new artifact route" in res.stdout


def test_tasks_only_expose_child_tasks_linked_to_runs():
    from omni.cli.state import AppState, make_agent, run_async
    from omni.storage.models import SubtaskORM

    project = "tasks-linked-only"

    async def _run():
        agent = await make_agent(AppState(project=project))
        try:
            async with agent.db.session() as s:
                s.add(SubtaskORM(
                    id="standaloneaa",
                    session_id="standalone-session",
                    skill_name="standalone-skill",
                    status="succeeded",
                    result_json={"summary": "standalone task"},
                ))
                await s.commit()
        finally:
            await agent.aclose()

    run_async(_run())

    listed = runner.invoke(app, ["--project", project, "task", "list"])
    assert listed.exit_code == 0
    assert "standaloneaa" not in listed.stdout
    assert "standalone-skill" not in listed.stdout

    shown = runner.invoke(app, ["--project", project, "task", "show", "standaloneaa"])
    assert shown.exit_code == 1

    all_rows = runner.invoke(app, ["--project", project, "task", "list", "--all"])
    assert all_rows.exit_code == 0
    assert "standaloneaa" not in all_rows.stdout
    assert "standalone-skill" not in all_rows.stdout


def test_tasks_list_kind_filter_hides_system_tasks_by_default():
    from omni.cli.state import AppState, make_agent, run_async
    from omni.storage.models import TaskORM

    project = "tasks-kind-filter"

    async def _run():
        agent = await make_agent(AppState(project=project))
        try:
            async with agent.db.session() as s:
                s.add(TaskORM(id="turntask", project=project, channel="cli",
                              status="succeeded", kind="turn", title="user request"))
                s.add(TaskORM(id="mainttask", project=project, channel="system",
                              status="succeeded", kind="maintenance", title="memory consolidation"))
                s.add(TaskORM(id="subagenttask", project=project, channel="cli",
                              status="succeeded", kind="subagent", title="child exploration"))
                await s.commit()
        finally:
            await agent.aclose()

    run_async(_run())

    # ids render truncated to 8 chars in the table
    default = runner.invoke(app, ["--project", project, "task", "list"])
    assert default.exit_code == 0
    assert "turntask" in default.stdout
    assert "mainttas" not in default.stdout
    assert "subagent" not in default.stdout

    maint = runner.invoke(app, ["--project", project, "task", "list", "--kind", "maintenance"])
    assert maint.exit_code == 0
    assert "mainttas" in maint.stdout
    assert "turntask" not in maint.stdout

    all_kinds = runner.invoke(app, ["--project", project, "task", "list", "--kind", "all"])
    assert all_kinds.exit_code == 0
    assert "turntask" in all_kinds.stdout
    assert "mainttas" in all_kinds.stdout
    assert "subagent" in all_kinds.stdout

    bad = runner.invoke(app, ["--project", project, "task", "list", "--kind", "bogus"])
    assert bad.exit_code == 1


def test_tasks_drain_reports_when_no_executable_subtasks():
    res = runner.invoke(app, ["--project", "tasks-drain-empty", "task", "drain"])
    assert res.exit_code == 0
    assert "No executable subtasks" in res.stdout


def test_tasks_rm_cascades_to_subtasks_and_protects_provenance():
    from omni.cli.state import run_async

    project = "tasks-rm-cascade"
    _seed_task_rows(project, [
        {"id": "faildelaa", "status": "failed", "title": "failed request"},
        {"id": "okkeepbb", "status": "succeeded", "title": "kept request"},
    ], with_children=True)

    # succeeded task carries provenance → refused without --force (kept)
    guarded = runner.invoke(app, ["--project", project, "task", "rm", "okkeepbb"])
    assert guarded.exit_code == 1
    assert "--force" in guarded.stdout

    # failed task deletes cleanly and cascades to its subtask
    ok = runner.invoke(app, ["--project", project, "task", "rm", "faildelaa"])
    assert ok.exit_code == 0
    assert "and its subtasks" in ok.stdout
    task_exists, sub_exists = run_async(_task_and_subtask_exist(project, "faildelaa"))
    assert task_exists is False
    assert sub_exists is False  # FK ON DELETE CASCADE removed the subtask row

    # succeeded task deletes only under --force
    forced = runner.invoke(app, ["--project", project, "task", "rm", "okkeepbb", "--force"])
    assert forced.exit_code == 0


def test_tasks_delete_alias_keeps_single_id_immediate_compatibility():
    from omni.cli.state import run_async

    project = "tasks-delete-single"
    task_id = "singledelete111"
    _seed_task_rows(project, [{
        "id": task_id,
        "status": "failed",
        "title": "single compatible delete",
    }], with_children=True)

    result = runner.invoke(app, ["--project", project, "task", "delete", task_id])

    assert result.exit_code == 0
    assert "Deleted task" in result.stdout
    task_exists, sub_exists = run_async(_task_and_subtask_exist(project, task_id))
    assert task_exists is False
    assert sub_exists is False


def test_tasks_rm_blocks_running_even_with_force():
    from omni.cli.state import run_async

    project = "tasks-rm-running"
    _seed_task_rows(project, [{"id": "runningaa", "status": "running", "title": "in flight"}])
    guarded = runner.invoke(app, ["--project", project, "task", "rm", "runningaa"])
    assert guarded.exit_code == 1
    assert "wait for it to settle" in guarded.stdout
    forced = runner.invoke(app, ["--project", project, "task", "rm", "runningaa", "--force"])
    assert forced.exit_code == 1
    assert "wait for it to settle" in forced.stdout
    assert run_async(_existing_task_ids(project, ["runningaa"])) == {"runningaa"}


@pytest.mark.parametrize("command", ["rm", "delete"])
def test_tasks_delete_multiple_ids_previews_deduplicated_prefixes_then_confirms(command):
    from omni.cli.state import run_async

    project = f"tasks-{command}-batch"
    alpha = "batchalphaaaa1111"
    beta = "batchbetabbbb2222"
    _seed_task_rows(project, [
        {"id": alpha, "status": "failed", "title": "alpha"},
        {"id": beta, "status": "cancelled", "title": "beta"},
    ], with_children=True)
    ids = ["batchalpha", alpha, "batchbeta"]

    preview = runner.invoke(app, ["--project", project, "task", command, *ids])

    assert preview.exit_code == 0
    assert "Would delete 2 tasks" in preview.stdout
    assert "--yes" in preview.stdout
    assert run_async(_existing_task_ids(project, [alpha, beta])) == {alpha, beta}

    confirmed = runner.invoke(app, [
        "--project", project, "task", command, *ids, "--yes",
    ])

    assert confirmed.exit_code == 0
    assert "Deleted 2 tasks" in confirmed.stdout
    assert run_async(_existing_task_ids(project, [alpha, beta])) == set()
    for task_id in (alpha, beta):
        task_exists, sub_exists = run_async(_task_and_subtask_exist(project, task_id))
        assert task_exists is False
        assert sub_exists is False


def test_tasks_rm_multiple_ids_preview_lists_the_complete_descendant_closure():
    from omni.cli.state import run_async

    project = "tasks-rm-batch-preview-closure"
    root = "previewroot11111"
    child = "previewchild2222"
    independent = "previewother3333"
    _seed_task_rows(project, [
        {"id": root, "status": "failed", "title": "selected root"},
        {"id": independent, "status": "cancelled", "title": "selected independent"},
    ])
    _seed_task_rows(project, [{
        "id": child,
        "status": "interrupted",
        "title": "implicit descendant",
        "parent_task_id": root,
    }])

    preview = runner.invoke(app, [
        "--project", project, "task", "rm", root, independent,
    ])

    assert preview.exit_code == 0
    output = cli_text(preview.stdout.replace("│", " "))
    assert f"{root[:8]} failed selected root" in output
    assert f"{child[:8]} interrupted implicit descendant" in output
    assert f"{independent[:8]} cancelled selected independent" in output
    assert "Would delete 3 tasks across 2 selected Task reference(s)" in output
    assert run_async(_existing_task_ids(project, [root, child, independent])) == {
        root, child, independent,
    }


def test_tasks_rm_preview_uses_workspace_unique_task_prefixes():
    """Displayed ids remain copyable when selected and unselected rows share 8 chars."""
    project = "tasks-rm-unique-preview-prefix"
    selected = "deadbeefaaaa1111"
    unselected = "deadbeefabbb2222"
    independent = "independent3333"
    _seed_task_rows(project, [
        {"id": selected, "status": "failed", "title": "selected collision"},
        {"id": unselected, "status": "failed", "title": "hidden collision"},
        {"id": independent, "status": "cancelled", "title": "other selected"},
    ])

    preview = runner.invoke(app, [
        "--project", project, "task", "rm", selected, independent,
    ])

    assert preview.exit_code == 0
    output = cli_text(preview.stdout.replace("│", " "))
    assert "deadbeefaa failed selected collision" in output
    assert "independ cancelled other selected" in output


def test_tasks_rm_rejects_an_empty_reference():
    project = "tasks-rm-empty-reference"
    _seed_task_rows(project, [{
        "id": "emptykeep1111",
        "status": "failed",
        "title": "must remain",
    }])

    result = runner.invoke(app, [
        "--project", project, "task", "rm", "emptykeep1111", "", "--yes",
    ])

    assert result.exit_code == 1
    assert "cannot be empty" in result.output


def test_tasks_rm_active_execution_names_its_owning_task_and_object():
    from omni.cli.state import AppState, make_agent, run_async
    from omni.storage.models import SubtaskORM, TaskORM

    project = "tasks-rm-active-execution-owner"
    task_id = "executionowner111"
    execution_id = "liveexecution222"

    async def _seed():
        agent = await make_agent(AppState(project=project))
        try:
            async with agent.db.session() as session:
                session.add(TaskORM(
                    id=task_id,
                    project=project,
                    channel="cli",
                    status="failed",
                    title="terminal projection",
                ))
                await session.flush()
                session.add(SubtaskORM(
                    id=execution_id,
                    task_id=task_id,
                    skill_name="x",
                    status="running",
                ))
                await session.commit()
        finally:
            await agent.aclose()

    run_async(_seed())
    result = runner.invoke(app, [
        "--project", project, "task", "rm", task_id,
    ])

    assert result.exit_code == 1
    output = cli_text(result.stdout)
    assert task_id[:8] in output
    assert f"skill_execution {execution_id}" in output
    assert "running" in output


def test_tasks_rm_multiple_ids_missing_reference_fails_closed():
    from omni.cli.state import run_async

    project = "tasks-rm-batch-missing"
    first = "missingfirst111"
    second = "missingsecond22"
    _seed_task_rows(project, [
        {"id": first, "status": "failed", "title": "first"},
        {"id": second, "status": "failed", "title": "second"},
    ])

    result = runner.invoke(app, [
        "--project", project, "task", "rm", first, "does-not-exist", second, "--yes",
    ])

    assert result.exit_code == 1
    assert "does-not-exist" in result.output
    assert "not found" in result.output.lower()
    assert run_async(_existing_task_ids(project, [first, second])) == {first, second}


def test_tasks_rm_multiple_ids_ambiguous_prefix_fails_closed():
    from omni.cli.state import run_async

    project = "tasks-rm-batch-ambiguous"
    valid = "validbatch111111"
    ambiguous_a = "collisionaaaa111"
    ambiguous_b = "collisionbbbb222"
    all_ids = [valid, ambiguous_a, ambiguous_b]
    _seed_task_rows(project, [
        {"id": task_id, "status": "failed", "title": task_id}
        for task_id in all_ids
    ])

    result = runner.invoke(app, [
        "--project", project, "task", "rm", valid, "collision", "--yes",
    ])

    assert result.exit_code == 1
    assert "collision" in result.output
    assert "ambiguous" in result.output.lower()
    assert ambiguous_a in result.output
    assert ambiguous_b in result.output
    assert run_async(_existing_task_ids(project, all_ids)) == set(all_ids)


def test_tasks_rm_multiple_ids_blocks_active_descendant_atomically_even_with_force():
    from omni.cli.state import run_async

    project = "tasks-rm-batch-active-descendant"
    root = "activeroot111111"
    child = "activechild22222"
    independent = "activeother33333"
    _seed_task_rows(project, [
        {"id": root, "status": "failed", "title": "root"},
        {"id": independent, "status": "failed", "title": "independent"},
    ])
    _seed_task_rows(project, [{
        "id": child,
        "status": "running",
        "title": "active descendant",
        "parent_task_id": root,
    }])

    result = runner.invoke(app, [
        "--project", project, "task", "rm", root, independent, "--yes", "--force",
    ])

    assert result.exit_code == 1
    assert child[:8] in result.stdout
    assert "running" in result.stdout
    assert run_async(_existing_task_ids(project, [root, child, independent])) == {
        root, child, independent,
    }


def test_tasks_rm_multiple_ids_requires_force_for_protected_descendant_atomically():
    from omni.cli.state import run_async

    project = "tasks-rm-batch-protected-descendant"
    root = "protectedroot11"
    child = "protectedchild2"
    independent = "protectedother3"
    _seed_task_rows(project, [
        {"id": root, "status": "failed", "title": "root"},
        {"id": independent, "status": "failed", "title": "independent"},
    ])
    _seed_task_rows(project, [{
        "id": child,
        "status": "succeeded",
        "title": "protected descendant",
        "parent_task_id": root,
    }])

    guarded = runner.invoke(app, [
        "--project", project, "task", "rm", root, independent, "--yes",
    ])

    assert guarded.exit_code == 1
    assert child[:8] in guarded.stdout
    assert "--force" in guarded.stdout
    assert run_async(_existing_task_ids(project, [root, child, independent])) == {
        root, child, independent,
    }

    forced = runner.invoke(app, [
        "--project", project, "task", "rm", root, independent, "--yes", "--force",
    ])

    assert forced.exit_code == 0
    assert "Deleted 3 tasks" in forced.stdout
    assert run_async(_existing_task_ids(project, [root, child, independent])) == set()


def test_tasks_clear_preview_breakdown_and_cascade():
    from omni.cli.state import run_async

    project = "tasks-clear-preview"
    _seed_task_rows(project, [
        {"id": "f1taskaa", "status": "failed", "title": "f1"},
        {"id": "f2taskbb", "status": "failed", "title": "f2"},
        {"id": "oktaskcc", "status": "succeeded", "title": "ok"},
        {"id": "runtaskdd", "status": "running", "title": "busy"},
    ], with_children=True)

    # preview (no --yes) reports a per-status breakdown, deletes nothing
    preview = runner.invoke(app, ["--project", project, "task", "clear", "--all"])
    assert preview.exit_code == 0
    assert "Would delete 2 deletable" in preview.stdout
    assert "1 succeeded" in preview.stdout  # protected
    assert "1 running" in preview.stdout    # blocked/active

    # confirmed clear removes both failed (cascading to subtasks), keeps the rest
    done = runner.invoke(app, ["--project", project, "task", "clear", "--all", "--yes"])
    assert done.exit_code == 0
    assert "Deleted 2 tasks" in done.stdout
    for tid in ("f1taskaa", "f2taskbb"):
        task_exists, sub_exists = run_async(_task_and_subtask_exist(project, tid))
        assert task_exists is False
        assert sub_exists is False
    ok_exists, _ = run_async(_task_and_subtask_exist(project, "oktaskcc"))
    assert ok_exists is True


def test_tasks_clear_refuses_running_status_filter():
    project = "tasks-clear-running"
    _seed_task_rows(project, [{"id": "busytaskx", "status": "running", "title": "busy"}])
    res = runner.invoke(app, ["--project", project, "task", "clear", "--status", "running", "--yes"])
    assert res.exit_code == 1
    assert "cannot be deleted in bulk" in res.stdout


def test_tasks_archive_hides_and_unarchive_restores():
    project = "tasks-archive-cli"
    _seed_task_rows(project, [
        {"id": "archtask", "status": "succeeded", "title": "archived request"},
        {"id": "keeptask", "status": "failed", "title": "kept request"},
    ])

    archived = runner.invoke(app, [
        "--project", project, "task", "archive", "archtask", "--reason", "old task",
    ])
    assert archived.exit_code == 0
    assert "Archived task" in archived.stdout

    listed = runner.invoke(app, ["--project", project, "task", "list"])
    assert listed.exit_code == 0
    assert "archtask" not in listed.stdout
    assert "keeptask" in listed.stdout

    with_archived = runner.invoke(app, ["--project", project, "task", "list", "--archived"])
    assert with_archived.exit_code == 0
    assert "archtask" in with_archived.stdout
    assert "archived" in with_archived.stdout

    detail = runner.invoke(app, ["--project", project, "task", "show", "archtask"])
    assert detail.exit_code == 0
    assert "old task" in detail.stdout

    restored = runner.invoke(app, ["--project", project, "task", "unarchive", "archtask"])
    assert restored.exit_code == 0
    listed_again = runner.invoke(app, ["--project", project, "task", "list"])
    assert "archtask" in listed_again.stdout


def test_tasks_archive_refuses_running_task():
    project = "tasks-archive-running"
    _seed_task_rows(project, [{"id": "runarchaa", "status": "running", "title": "busy"}])
    res = runner.invoke(app, ["--project", project, "task", "archive", "runarchaa"])
    assert res.exit_code == 1
    assert "cannot be archived" in res.stdout


def test_doctor_flags_stale_task_and_drain_reconciles_it():
    from datetime import UTC, datetime, timedelta

    project = "tasks-hygiene"
    old = datetime.now(UTC) - timedelta(hours=3)
    _seed_task_rows(project, [{
        "id": "deadrunaa", "status": "running", "title": "orphaned turn",
        "created_at": old, "started_at": old,
    }])

    diag = runner.invoke(app, ["--project", project, "doctor"], env={"COLUMNS": "200"})
    assert diag.exit_code == 0
    assert "Task hygiene" in diag.stdout
    assert "stuck in running" in diag.stdout

    # drain housekeeping settles the orphan as interrupted…
    drained = runner.invoke(app, ["--project", project, "task", "drain"])
    assert drained.exit_code == 0
    shown = runner.invoke(app, ["--project", project, "task", "show", "deadrunaa"])
    assert "interrupted" in shown.stdout

    # …and doctor goes green.
    healthy = runner.invoke(app, ["--project", project, "doctor"], env={"COLUMNS": "200"})
    assert "no stale running tasks" in healthy.stdout


def test_tasks_show_retry_and_resume_workflow_step_by_unique_id():
    project = "tasks-step-view"
    _, workflow_id = _seed_failed_workflow_for_project(project)

    shown = runner.invoke(app, ["--project", project, "task", "show", "diagram"])
    assert shown.exit_code == 0
    assert "Workflow step diagram" in shown.stdout
    assert "workflowaa" in shown.stdout
    assert "Graphviz unavailable" in shown.stdout

    explicit = runner.invoke(app, ["--project", project, "task", "step", workflow_id, "diagram", "--json"])
    assert explicit.exit_code == 0
    assert '"step_id": "diagram"' in explicit.stdout
    assert '"task_id"' in explicit.stdout

    retry = runner.invoke(app, ["--project", project, "task", "retry", "diagram"])
    assert retry.exit_code == 0
    assert "Created skill execution attempt" in retry.stdout
    assert "starting at step" in retry.stdout  # step name may wrap to the next line

    resume_project = "tasks-step-resume-view"
    _seed_failed_workflow_for_project(resume_project)
    resume = runner.invoke(
        app,
        ["--project", resume_project, "task", "resume", "diagram"],
    )
    assert resume.exit_code == 0
    assert "Returned workflow" in resume.stdout
    assert "from step diagram" in resume.stdout


def test_memory_rm_and_help_via_cli():
    from omni.cli.state import AppState, make_agent, run_async
    from omni.memory.service import MemoryLayer

    project = "memory-rm-cli"
    state = AppState(project=project)

    async def _seed() -> str:
        agent = await make_agent(state)
        try:
            return await agent.memory.record(
                layer=MemoryLayer.SEMANTIC, scope="project",
                summary="可删除的发现条目", memory_type="finding", importance=0.5,
            )
        finally:
            await agent.aclose()

    mid = run_async(_seed())

    help_res = runner.invoke(app, ["--project", project, "memory", "help"])
    assert help_res.exit_code == 0
    assert "rm/delete/remove" in help_res.stdout

    rm_res = runner.invoke(app, ["--project", project, "memory", "rm", mid[:8]])
    assert rm_res.exit_code == 0
    assert "Deleted memory" in rm_res.stdout
    # gone now → second delete fails
    again = runner.invoke(app, ["--project", project, "memory", "rm", mid[:8]])
    assert again.exit_code == 1


def test_memory_aliases_path_and_pagination_via_cli():
    from omni.cli.state import AppState, make_agent, run_async
    from omni.memory.service import MemoryLayer

    project = "memory-alias-path-page"
    state = AppState(project=project)

    async def _seed() -> tuple[str, str]:
        agent = await make_agent(state)
        try:
            first = await agent.memory.record(
                layer=MemoryLayer.SEMANTIC, scope="project",
                summary="第一页记忆", memory_type="finding", importance=0.5,
            )
            second = await agent.memory.record(
                layer=MemoryLayer.SEMANTIC, scope="project",
                summary="第二页记忆", memory_type="finding", importance=0.5,
            )
            return first, second
        finally:
            await agent.aclose()

    first, second = run_async(_seed())

    page1 = runner.invoke(app, ["--project", project, "memory", "list", "--limit", "1"])
    assert page1.exit_code == 0
    assert "page 1" in page1.stdout
    assert "Next page" in page1.stdout
    page2 = runner.invoke(app, ["--project", project, "memory", "list", "--limit", "1", "--page", "2"])
    assert page2.exit_code == 0
    assert "page 2" in page2.stdout

    path_res = runner.invoke(app, ["--project", project, "memory", "path"])
    assert path_res.exit_code == 0
    assert "memory_entries" in path_res.stdout
    assert "NOTEBOOK.md" in path_res.stdout

    remove_res = runner.invoke(app, ["--project", project, "memory", "remove", first[:8]])
    assert remove_res.exit_code == 0
    delete_res = runner.invoke(app, ["--project", project, "memory", "delete", second[:8]])
    assert delete_res.exit_code == 0


def test_memory_add_then_link_uses_the_global_graph():
    project = "memory-link-cli"
    first = runner.invoke(app, ["--project", project, "memory", "add", "prefer Chinese slides"])
    second = runner.invoke(app, ["--project", project, "memory", "add", "prefer arXiv only"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    first_id = first.stdout.split("Recorded memory ", 1)[1].split()[0].rstrip(".")
    second_id = second.stdout.split("Recorded memory ", 1)[1].split()[0].rstrip(".")

    linked = runner.invoke(
        app,
        ["--project", project, "memory", "link", first_id, second_id, "--relation", "related"],
    )
    assert linked.exit_code == 0, linked.stdout + linked.stderr
    assert "Linked" in linked.stdout
    assert "was not found" not in linked.stdout

    graph = runner.invoke(app, ["--project", project, "memory", "graph", first_id])
    assert graph.exit_code == 0, graph.stdout + graph.stderr
    assert second_id[:8] in graph.stdout

    missing = runner.invoke(
        app,
        ["--project", project, "memory", "link", first_id, "does-not-exist"],
    )
    assert missing.exit_code == 1
    assert "was not found" in missing.stdout + missing.stderr


def test_repl_memory_dispatches_subcommands_and_recalls_free_text(monkeypatch):
    # `/memory list` must run the real subcommand (not search for "list"), while
    # `/memory <free text>` stays a quick semantic recall shortcut.
    import omni.cli.main as main
    from omni.cli.state import AppState, run_async

    dispatched: list[str] = []

    async def _fake_external(state, line):  # noqa: ANN001
        dispatched.append(line)

    monkeypatch.setattr(main, "_run_repl_external_command", _fake_external)

    recalled: list[str] = []

    class _Mem:
        async def recall(self, q, **_):  # noqa: ANN001
            recalled.append(q)
            return []

    agent = SimpleNamespace(memory=_Mem())

    run_async(main._repl_memory(agent, AppState(), "list --type preference"))
    run_async(main._repl_memory(agent, AppState(), "rm 1a2b3c"))
    run_async(main._repl_memory(agent, AppState(), "delete 1a2b3c"))
    run_async(main._repl_memory(agent, AppState(), "remove 1a2b3c"))
    run_async(main._repl_memory(agent, AppState(), "path"))
    assert dispatched == [
        "/memory list --type preference",
        "/memory rm 1a2b3c",
        "/memory delete 1a2b3c",
        "/memory remove 1a2b3c",
        "/memory path",
    ]

    run_async(main._repl_memory(agent, AppState(), "检索增强生成"))
    assert recalled == ["检索增强生成"]


def test_successful_drained_task_suppresses_iteration_limit_text():
    from types import SimpleNamespace

    from omni.cli.runner import should_suppress_assistant_text

    turn = SimpleNamespace(
        text="已达到迭代上限但未收敛。",
        terminated_reason="max_iterations",
        drained_results=[{"status": "succeeded"}],
    )

    assert should_suppress_assistant_text(turn) is True


def test_degraded_iteration_limit_is_reported_with_task_hint():
    from types import SimpleNamespace

    from omni.cli.render import console
    from omni.cli.runner import render_turn_diagnostics

    turn = SimpleNamespace(
        kind="partial",
        text="",
        task_id="task-review-12345678",
        terminated_reason="max_iterations",
        tool_trace=[],
        drained_results=[{"status": "degraded"}],
    )

    with console.capture() as capture:
        render_turn_diagnostics(turn)

    output = capture.get()
    assert "iteration limit reached" in output
    assert "/task show task-rev" in output


def test_authentication_failure_renders_once_with_recovery_actions():
    from types import SimpleNamespace

    from omni.cli.render import assistant_answer, console
    from omni.cli.runner import render_turn_diagnostics, should_suppress_assistant_text

    turn = SimpleNamespace(
        kind="error",
        text=(
            "Model authentication failed. Check the configured provider API key "
            "and access permissions."
        ),
        terminated_reason="llm_auth_error",
        tool_trace=[],
        drained_results=[],
    )

    with console.capture() as capture:
        render_turn_diagnostics(turn)
        if not should_suppress_assistant_text(turn):
            assistant_answer(turn.text)

    output = capture.get()
    assert output.lower().count("model authentication failed") == 1
    assert "/config test" in output
    assert "/config model" in output
    assert "status=401" not in output


def test_tasks_watch_key_listener_can_quit_without_ctrl_c():
    from omni.cli.commands.tasks_cmd import WatchKeyListener

    class FakeListener(WatchKeyListener):
        def __init__(self) -> None:
            super().__init__(stream=None)
            self._active = True

        def should_quit(self, timeout: float = 0.0) -> bool:
            return True

    assert FakeListener().wait(5) is True


def test_serve_help_and_status_are_available():
    help_res = runner.invoke(app, ["serve", "--help"])
    assert help_res.exit_code == 0
    assert "daemon" in help_res.stdout
    assert "poller" in help_res.stdout
    assert "start" in help_res.stdout
    assert "stop" in help_res.stdout
    assert "status" in help_res.stdout

    # After the serve/service convergence, `serve status` reports the home service.
    status_res = runner.invoke(app, ["serve", "status"])
    assert status_res.exit_code == 0
    assert "home service" in status_res.stdout
    assert "Enabled" in status_res.stdout
    assert "Anchor" in status_res.stdout


def test_skills_list():
    res = runner.invoke(app, ["skills", "list", "--async"])
    assert res.exit_code == 0
    assert "research-pptx" in res.stdout


def test_skills_sources():
    res = runner.invoke(app, ["skills", "sources"])
    assert res.exit_code == 0
    assert "builtin" in res.stdout


def test_skills_export_and_unexport():
    exp = runner.invoke(app, ["skills", "export", "claude"])
    assert exp.exit_code == 0
    assert "arxiv-fetch" in exp.stdout
    # exported copy should now be discoverable as a Claude Code skill on disk
    from pathlib import Path

    exported = Path.home() / ".claude" / "skills" / "arxiv-fetch"
    assert (exported / "SKILL.md").is_file()
    assert (exported / "LICENSE.txt").is_file()
    assert (exported / "NOTICE.md").is_file()
    un = runner.invoke(app, ["skills", "unexport", "claude"])
    assert un.exit_code == 0
    assert not (Path.home() / ".claude" / "skills" / "arxiv-fetch").exists()


def test_skill_export_excludes_host_caches(tmp_path):
    from omni.config import load_settings
    from omni.skills_runtime.install import export_builtin_skills

    source = tmp_path / "source"
    skill = source / "portable-demo"
    cache = skill / "__pycache__"
    cache.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: portable-demo\ndescription: portable demo\n---\n",
        encoding="utf-8",
    )
    (cache / "engine.cpython-312.pyc").write_bytes(b"cache")

    paths = load_settings().paths
    results = export_builtin_skills(paths, ["claude"], src_dir=source)

    assert results[0].status == "installed"
    exported = Path.home() / ".claude" / "skills" / "portable-demo"
    assert (exported / "SKILL.md").is_file()
    assert not (exported / "__pycache__").exists()


def test_resync_exported_skills_refreshes_owned_copies_when_explicitly_called():
    # The helper remains explicit; `omni update` must never call it implicitly.
    from pathlib import Path

    from omni.config import load_settings
    from omni.skills_runtime.install import exported_targets, resync_exported_skills

    paths = load_settings().paths
    assert resync_exported_skills(paths) == []  # nothing exported → no-op

    exp = runner.invoke(app, ["skills", "export", "claude"])
    assert exp.exit_code == 0
    assert "claude" in exported_targets(paths)

    skill_md = Path.home() / ".claude" / "skills" / "arxiv-fetch" / "SKILL.md"
    assert skill_md.is_file()
    skill_md.write_text("STALE", encoding="utf-8")  # simulate post-upgrade drift

    results = resync_exported_skills(paths)
    assert any(r.status in ("updated", "installed") for r in results)
    assert skill_md.read_text(encoding="utf-8") != "STALE"  # refreshed to bundled content


def test_resync_exported_skills_restores_missing_legal_files():
    from pathlib import Path

    from omni.config import load_settings
    from omni.skills_runtime.install import resync_exported_skills

    paths = load_settings().paths
    exp = runner.invoke(app, ["skills", "export", "claude"])
    assert exp.exit_code == 0

    exported = Path.home() / ".claude" / "skills" / "arxiv-fetch"
    (exported / "LICENSE.txt").unlink()
    results = resync_exported_skills(paths)

    assert any(r.name == "arxiv-fetch" and r.status == "updated" for r in results)
    assert (exported / "LICENSE.txt").is_file()
    assert (exported / "NOTICE.md").is_file()


def test_skills_export_all_tools_covers_claude_codex_openclaw():
    # One-click `--all` must reach all three tools (Codex/OpenClaw also get the
    # shared ~/.agents root so discovery works regardless of tool version).
    from pathlib import Path

    res = runner.invoke(app, ["skills", "export", "--all"])
    assert res.exit_code == 0
    home = Path.home()
    for root in (".claude", ".codex", ".openclaw", ".agents"):
        assert (home / root / "skills" / "arxiv-fetch" / "SKILL.md").is_file(), root


def test_skills_export_per_tool_codex_writes_codex_and_shared_agents():
    from pathlib import Path

    res = runner.invoke(app, ["skills", "export", "codex"])
    assert res.exit_code == 0
    home = Path.home()
    assert (home / ".codex" / "skills" / "arxiv-fetch" / "SKILL.md").is_file()
    assert (home / ".agents" / "skills" / "arxiv-fetch" / "SKILL.md").is_file()
    # A tool the user didn't pick must NOT receive the export.
    assert not (home / ".claude" / "skills" / "arxiv-fetch").exists()


def test_skills_export_rejects_unknown_tool():
    res = runner.invoke(app, ["skills", "export", "bogus"])
    assert res.exit_code == 0
    assert "Unknown tools" in res.stdout


def test_expand_targets_maps_tools_to_roots():
    from omni.skills_runtime.install import expand_targets

    assert expand_targets(["claude"]) == ["claude"]
    assert expand_targets(["codex"]) == ["codex", "agents"]
    assert expand_targets(["openclaw"]) == ["openclaw", "agents"]
    # None → all three tools, shared agents root deduplicated once.
    assert expand_targets(None) == ["claude", "codex", "agents", "openclaw"]


def test_skills_remove_imported_skill_deletes_files_and_records_tombstone(tmp_path):
    import json

    from omni.config.paths import get_paths

    src = tmp_path / "demo-remove"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: demo-remove\ndescription: removable demo\n---\nbody\n",
        encoding="utf-8",
    )

    added = runner.invoke(app, ["skills", "add", str(src)])
    assert added.exit_code == 0

    paths = get_paths()
    dest = paths.user_skills_dir / "demo-remove"
    assert (dest / "SKILL.md").is_file()

    removed = runner.invoke(app, ["skills", "remove", "demo-remove"])

    assert removed.exit_code == 0
    assert "Physically deleted" in removed.stdout
    assert not dest.exists()
    deleted = json.loads((paths.home / "skills_deleted.json").read_text(encoding="utf-8"))
    row = next(item for item in deleted["deleted"] if item["name"] == "demo-remove")
    assert row["source"] == "user_omni"
    assert row["action"] == "physical_delete"
    assert row["path"] == str(dest)


def test_skills_remove_builtin_disables_without_deleting_core_skill():
    from omni.config.paths import get_paths
    from omni.data import BUILTIN_SKILLS_DIR

    removed = runner.invoke(app, ["skills", "remove", "arxiv-fetch"])

    assert removed.exit_code == 0
    assert "Disabled skill" in removed.stdout
    assert (BUILTIN_SKILLS_DIR / "arxiv-fetch" / "SKILL.md").is_file()
    raw = tomllib.loads(get_paths().config_file.read_text(encoding="utf-8"))
    assert "arxiv-fetch" in raw["skills"]["disabled"]

    listed = runner.invoke(app, ["skills", "list", "--async", "--no-pager"])
    assert listed.exit_code == 0
    assert "arxiv-fetch" not in listed.stdout


def test_skills_list_disabled_and_restore_builtin_skill():
    from omni.config.paths import get_paths

    removed = runner.invoke(app, ["skills", "remove", "scientific-figure"])
    assert removed.exit_code == 0

    disabled = runner.invoke(app, ["skills", "list", "--disabled"])
    assert disabled.exit_code == 0
    assert "scientific-figure" in disabled.stdout
    assert "config_disable" in disabled.stdout

    repeated = runner.invoke(app, ["skills", "remove", "scientific-figure"])
    assert repeated.exit_code == 0
    assert "is disabled" in repeated.stdout
    assert "skills restore scientific-figure" in " ".join(repeated.stdout.split())

    restored = runner.invoke(app, ["skills", "restore", "scientific-figure"])
    assert restored.exit_code == 0
    assert "Restored skill" in restored.stdout
    raw = tomllib.loads(get_paths().config_file.read_text(encoding="utf-8"))
    assert "scientific-figure" not in raw.get("skills", {}).get("disabled", [])

    shown = runner.invoke(app, ["skills", "info", "scientific-figure"])
    assert shown.exit_code == 0
    assert "scientific-figure" in shown.stdout


def test_skills_enable_alias_restores_disabled_skill():
    removed = runner.invoke(app, ["skills", "remove", "arxiv-fetch"])
    assert removed.exit_code == 0

    restored = runner.invoke(app, ["skills", "enable", "arxiv-fetch"])
    assert restored.exit_code == 0
    assert "Restored skill" in restored.stdout


def test_skills_remove_external_defaults_to_disable_and_preserves_files():
    from pathlib import Path

    from omni.config.paths import get_paths

    ext = Path.home() / ".claude" / "skills" / "ext-remove"
    ext.mkdir(parents=True)
    (ext / "SKILL.md").write_text(
        "---\nname: ext-remove\ndescription: external remove demo\n---\nbody\n",
        encoding="utf-8",
    )

    removed = runner.invoke(app, ["skills", "remove", "ext-remove", "--all"])

    assert removed.exit_code == 0
    assert "Disabled skill" in removed.stdout
    assert ext.exists()
    raw = tomllib.loads(get_paths().config_file.read_text(encoding="utf-8"))
    assert "ext-remove" in raw["skills"]["disabled"]

    listed = runner.invoke(app, ["skills", "list", "--all", "--no-pager"])
    assert listed.exit_code == 0
    assert "ext-remove" not in listed.stdout


def test_skills_remove_external_physical_requires_force_and_can_delete():
    from pathlib import Path

    ext = Path.home() / ".claude" / "skills" / "ext-physical"
    ext.mkdir(parents=True)
    (ext / "SKILL.md").write_text(
        "---\nname: ext-physical\ndescription: external physical demo\n---\nbody\n",
        encoding="utf-8",
    )

    refused = runner.invoke(app, ["skills", "remove", "ext-physical", "--all", "--physical"])
    assert refused.exit_code != 0
    assert ext.exists()

    removed = runner.invoke(app, ["skills", "remove", "ext-physical", "--all", "--physical", "--force"])
    assert removed.exit_code == 0
    assert "Physically deleted" in removed.stdout
    assert not ext.exists()


def test_skills_list_excludes_external_libraries_by_default():
    # plant a fake Claude Code user skill in the isolated HOME
    from pathlib import Path

    ext = Path.home() / ".claude" / "skills" / "ext-demo-skill"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "SKILL.md").write_text(
        "---\nname: ext-demo-skill\ndescription: external demo\n---\nbody\n", encoding="utf-8"
    )
    default = runner.invoke(app, ["skills", "list"])
    assert default.exit_code == 0
    assert "ext-demo-skill" not in default.stdout  # external libs hidden by default
    full = runner.invoke(app, ["skills", "list", "--all"])
    assert full.exit_code == 0
    assert "ext-demo-skill" in full.stdout  # opt-in shows them


def test_skills_list_pagination_and_grouping():
    from pathlib import Path

    root = Path.home() / ".claude" / "skills"
    for n in ("ext-a", "ext-b", "ext-c", "ext-d", "ext-e"):
        d = root / n
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {n}\ndescription: demo {n}\n---\nbody\n", encoding="utf-8"
        )

    # Pagination: filter to one source so page contents are deterministic (5 → 3 pages of 2).
    base = ["skills", "list", "--all", "--source", "user_claude", "--page-size", "2"]
    p1 = runner.invoke(app, base)
    assert p1.exit_code == 0
    assert "Page 1/3" in p1.stdout
    assert "ext-a" in p1.stdout and "ext-b" in p1.stdout
    assert "ext-c" not in p1.stdout  # only 2 per page
    p2 = runner.invoke(app, [*base, "--page", "2"])
    assert p2.exit_code == 0
    assert "ext-c" in p2.stdout and "ext-d" in p2.stdout
    assert "ext-a" not in p2.stdout

    # --page-size 0 disables paging (no page footer, all rows shown).
    allp = runner.invoke(app, ["skills", "list", "--all", "--source", "user_claude", "--page-size", "0"])
    assert allp.exit_code == 0
    assert "Page" not in allp.stdout and "ext-e" in allp.stdout

    # Grouping: per-source breakdown line + a section header carrying the source key.
    g = runner.invoke(app, ["skills", "list", "--all", "--group"])
    assert g.exit_code == 0
    assert "By source" in g.stdout
    assert "user_claude" in g.stdout and "builtin" in g.stdout


def test_skills_list_pager_renders_full_list(monkeypatch):
    """`--pager` routes the whole list through less (we stub it to capture)."""
    import omni.cli.commands.skills_cmd as sc

    captured: dict[str, str] = {}
    monkeypatch.setattr(sc._LessPager, "show", lambda self, content: captured.__setitem__("c", content))

    res = runner.invoke(app, ["skills", "list", "--all", "--pager"], env={"COLUMNS": "200"})
    assert res.exit_code == 0
    buf = captured.get("c", "")
    assert "q quit" in buf  # interactive navigation hint is shown at the top of the pager
    assert "livefigure" in buf  # the *full* list is rendered into the pager buffer


def test_skills_add_imports_local_skill_into_omni(tmp_path):
    from omni.config.paths import get_paths

    src = tmp_path / "my-skill"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: imported demo\n---\nhello\n", encoding="utf-8"
    )
    res = runner.invoke(app, ["skills", "add", str(src)])
    assert res.exit_code == 0
    assert (get_paths().user_skills_dir / "my-skill" / "SKILL.md").is_file()
    # imported skills show by default (user_omni), no --all needed
    listed = runner.invoke(app, ["skills", "list"])
    assert "my-skill" in listed.stdout


def test_skills_add_from_tool_root_by_name():
    from pathlib import Path

    from omni.config.paths import get_paths

    ext = Path.home() / ".claude" / "skills" / "cc-demo"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "SKILL.md").write_text(
        "---\nname: cc-demo\ndescription: cc demo\n---\nx\n", encoding="utf-8"
    )
    res = runner.invoke(app, ["skills", "add", "claude:cc-demo"])
    assert res.exit_code == 0
    assert (get_paths().user_skills_dir / "cc-demo" / "SKILL.md").is_file()


def _git_commit_repo(repo):
    import subprocess

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        check=True,
    )


def test_looks_like_git_url():
    from omni.skills_runtime.install import looks_like_git_url

    assert looks_like_git_url("https://github.com/o/r.git")
    assert looks_like_git_url("git@github.com:o/r.git")
    assert looks_like_git_url("github.com/o/r")
    assert not looks_like_git_url("claude:pdf")
    assert not looks_like_git_url("summarize")


def test_skills_add_from_git_repo_is_skill(tmp_path):
    import shutil

    if shutil.which("git") is None:
        import pytest

        pytest.skip("git not available")
    from omni.config.paths import get_paths
    from omni.skills_runtime.install import import_skill_from_git

    repo = tmp_path / "repo-skill"
    repo.mkdir()
    (repo / "SKILL.md").write_text(
        "---\nname: git-one\ndescription: from git\n---\nhi\n", encoding="utf-8"
    )
    _git_commit_repo(repo)
    results = import_skill_from_git(f"file://{repo}", get_paths())
    assert [r.status for r in results] == ["installed"]
    # named from SKILL.md (`git-one`), not the generic clone dir
    assert results[0].name == "git-one"
    assert (get_paths().user_skills_dir / "git-one" / "SKILL.md").is_file()


def test_skills_add_from_git_collection(tmp_path):
    import shutil

    if shutil.which("git") is None:
        import pytest

        pytest.skip("git not available")
    from omni.config.paths import get_paths
    from omni.skills_runtime.install import import_skill_from_git

    repo = tmp_path / "coll"
    for n in ("a-skill", "b-skill"):
        d = repo / "skills" / n
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {n}\ndescription: {n}\n---\nx\n", encoding="utf-8")
    _git_commit_repo(repo)
    results = import_skill_from_git(f"file://{repo}", get_paths())
    assert sorted(r.name for r in results) == ["a-skill", "b-skill"]
    assert all(r.status == "installed" for r in results)


def test_registry_suggest_matches_trigger_phrases():
    """Intent recognition uses authors' trigger.phrases / when_to_use."""
    from omni.config import load_settings
    from omni.skills_runtime.registry import SkillRegistry

    reg = SkillRegistry(load_settings())
    reg.build_index()
    assert "scientific-figure" in [
        e.name for e in reg.suggest("Generate a scientific RAG system architecture figure", limit=3)
    ]
    assert "livefigure" in [e.name for e in reg.suggest("editable PPTX scientific figure", limit=3)]
    assert reg.suggest("", limit=3) == []


def test_config_model_sets_fields():
    res = runner.invoke(
        app,
        ["config", "model", "-p", "openai_compatible", "-u",
         "https://example.test/v1", "-m", "demo-model", "-k", "sk-secret-123"],
    )
    assert res.exit_code == 0
    # api key must be masked in output, never printed in full
    assert "sk-secret-123" not in res.stdout
    got = runner.invoke(app, ["config", "get", "model.model"])
    assert "demo-model" in got.stdout


def test_config_model_auto_defaults_provider_when_endpoint_given():
    # Pointing at a real endpoint without -p must flip provider off the offline
    # mock, so the endpoint is actually used (no silent stay-on-mock footgun).
    res = runner.invoke(
        app,
        ["config", "model", "-u", "https://api.deepseek.com/v1", "-m", "deepseek-chat", "-k", "sk-abc"],
    )
    assert res.exit_code == 0
    assert "openai_compatible" in res.stdout  # auto-selected
    got = runner.invoke(app, ["config", "get", "model.provider"])
    assert "openai_compatible" in got.stdout


def test_config_model_keeps_explicit_provider():
    # An explicit provider must never be overwritten by the auto-default.
    runner.invoke(app, ["config", "set", "model.provider", "openai"])
    res = runner.invoke(app, ["config", "model", "-u", "https://api.deepseek.com/v1"])
    assert res.exit_code == 0
    got = runner.invoke(app, ["config", "get", "model.provider"])
    assert "openai" in got.stdout
    assert "openai_compatible" not in got.stdout


def test_embeddings_default_off_and_master_switch_wins():
    """Keyword recall is the safe default and the master switch wins over stale config."""
    from omni.config import load_settings
    from omni.core.llm.client import create_llm_client

    base = {
        "model": {
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-x",
            "model": "deepseek-chat",
        }
    }
    default_client = create_llm_client(load_settings(overrides=base))
    assert default_client._emb_model == ""

    # Enablement without a dedicated endpoint must not probe a chat-only host.
    misconfigured = create_llm_client(load_settings(overrides={
        **base, "memory": {"embeddings_enabled": True},
    }))
    assert misconfigured._emb_model == ""

    # A configured embedding endpoint is wired only after explicit enablement.
    on = create_llm_client(load_settings(overrides={
        **base,
        "memory": {
            "embeddings_enabled": True,
            "embedding_base_url": "https://api.openai.com/v1",
        },
    }))
    assert on._emb_model == "text-embedding-3-small"
    assert on._emb_base == "https://api.openai.com/v1"

    # Explicit opt-out → no embedding model wired → embed() never probes.
    off = create_llm_client(
        load_settings(overrides={**base, "memory": {"embeddings_enabled": False}})
    )
    assert off._emb_model == ""

    # Stale endpoint/provider values must not override the explicit master switch.
    stale = create_llm_client(
        load_settings(overrides={
            **base,
            "memory": {
                "embeddings_enabled": False,
                "embedding_provider": "openai_compatible",
                "embedding_base_url": "https://api.openai.com/v1",
            },
        })
    )
    assert stale._emb_model == ""


def test_config_embeddings_enable_and_disable_workflow():
    """Existing users get one command for endpoint/model/key and a true off switch."""
    enabled = runner.invoke(
        app,
        [
            "config", "embeddings", "--enable",
            "--base-url", "https://embed.example/v1",
            "--model", "bge-m3", "--api-key", "emb-secret",
        ],
    )
    assert enabled.exit_code == 0
    assert "semantic recall" in enabled.stdout
    assert "true" in runner.invoke(
        app, ["config", "get", "memory.embeddings_enabled"]
    ).stdout.lower()
    assert "https://embed.example/v1" in runner.invoke(
        app, ["config", "get", "memory.embedding_base_url"]
    ).stdout

    disabled = runner.invoke(app, ["config", "embeddings", "--disable"])
    assert disabled.exit_code == 0
    assert "Keyword recall" in disabled.stdout
    assert "false" in runner.invoke(
        app, ["config", "get", "memory.embeddings_enabled"]
    ).stdout.lower()


def test_config_embeddings_enable_requires_explicit_endpoint():
    result = runner.invoke(app, ["config", "embeddings", "--enable"])
    assert result.exit_code == 2
    assert "/embeddings" in result.stderr


def test_config_embeddings_can_enable_local_specter2(tmp_path: Path):
    base = tmp_path / "specter2-base"
    adapter = tmp_path / "specter2-adapter"
    python_launcher = tmp_path / "specter2-python"
    base.mkdir()
    adapter.mkdir()
    python_launcher.symlink_to(sys.executable)

    result = runner.invoke(
        app,
        [
            "config",
            "embeddings",
            "--enable",
            "--provider",
            "specter2",
            "--python",
            str(python_launcher),
            "--base-model",
            str(base),
            "--adapter",
            str(adapter),
            "--device",
            "cuda:0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "local SPECTER2" in result.stdout
    assert "768 dimensions" in " ".join(result.stdout.split())
    assert "specter2" in runner.invoke(
        app, ["config", "get", "memory.embedding_provider"]
    ).stdout
    assert "allenai/specter2-proximity" in runner.invoke(
        app, ["config", "get", "memory.embedding_model"]
    ).stdout
    assert "768" in runner.invoke(
        app, ["config", "get", "memory.embedding_dim"]
    ).stdout
    python_result = runner.invoke(
        app, ["config", "get", "memory.embedding_specter2_python"]
    )
    assert python_launcher.absolute().as_posix() in _shown_path(python_result.stdout)
    base_result = runner.invoke(
        app, ["config", "get", "memory.embedding_specter2_base_model"]
    )
    assert base.resolve().as_posix() in _shown_path(base_result.stdout)


def test_config_specter2_rejects_missing_path_without_echoing_it(tmp_path: Path):
    private_missing = tmp_path / "secret-model-location"
    result = runner.invoke(
        app,
        [
            "config",
            "embeddings",
            "--enable",
            "--provider",
            "specter2",
            "--python",
            sys.executable,
            "--base-model",
            str(private_missing),
            "--adapter",
            str(private_missing),
        ],
    )

    assert result.exit_code == 2
    assert "local path does not exist" in result.stderr
    assert str(private_missing) not in result.output


def test_config_help_documents_all_embedding_configuration_paths():
    result = runner.invoke(app, ["config", "help"])
    assert result.exit_code == 0
    assert "config embeddings --enable" in result.stdout
    assert "config embeddings --disable" in result.stdout
    assert "edit [memory] embedding_*" in result.stdout
    assert "config path" in result.stdout
    assert "secrets.toml" in result.stdout


def test_doctor():
    res = runner.invoke(app, ["doctor"])
    assert res.exit_code == 0
    assert "Doctor" in res.stdout


def test_doctor_separates_node_from_npm(monkeypatch):
    """A Node-only PATH must not look like a missing Node.js install."""
    import omni.cli.commands.doctor_cmd as dc
    import omni.skills_runtime.runtime_setup as runtime_setup

    def fake_which(name: str) -> str | None:
        if name in {"node", "node.exe"}:
            return "/usr/bin/node"
        return None

    monkeypatch.setattr(dc.shutil, "which", fake_which)
    monkeypatch.setattr(runtime_setup, "research_pptx_runtime_ready", lambda _paths: False)

    result = runner.invoke(app, ["doctor"], env={"COLUMNS": "240"})
    assert result.exit_code == 0, result.output
    assert "bin: node" in result.stdout
    assert "/usr/bin/node" in result.stdout
    assert "bin: npm" in result.stdout
    assert "Node is installed, but npm is not on PATH" in result.stdout
    assert "research-pptx runtime" in result.stdout
    assert "omni skills setup research-pptx" in result.stdout


def test_doctor_reports_active_owner_path_order_and_conflicting_copies(
    tmp_path, monkeypatch
):
    import omni.cli.commands.doctor_cmd as dc
    from omni.runtime.uninstall import InstallationRecord

    active_path = tmp_path / "uv-bin" / "omni"
    other_path = tmp_path / "conda" / "bin" / "omni"
    active_path.parent.mkdir(parents=True)
    other_path.parent.mkdir(parents=True)
    active_path.write_text("launcher", encoding="utf-8")
    other_path.write_text("launcher", encoding="utf-8")
    active = InstallationRecord(
        method="uv",
        executable=str(active_path),
        python=str(tmp_path / "uv-tools" / "bin" / "python"),
        current=True,
    )
    other = InstallationRecord(
        method="env",
        executable=str(other_path),
        python=str(tmp_path / "conda" / "bin" / "python"),
    )
    monkeypatch.setattr(dc, "current_installation", lambda _paths: active)
    monkeypatch.setattr(dc, "omni_entrypoints_on_path", lambda: [other_path, active_path])
    monkeypatch.setattr(
        dc,
        "detect_installations",
        lambda _paths, *, all_installations: [other, active],
    )
    monkeypatch.setattr(dc, "_launcher_version", lambda path: f"version-for-{path.parent.name}")

    result = runner.invoke(app, ["doctor"], env={"COLUMNS": "240"})

    # Collapse whitespace so the assertions test *content*, not terminal width:
    # a narrow/dumb terminal may fold a long path across lines, which is a
    # rendering detail, not a diagnostic-correctness failure.
    flat = "".join(result.stdout.split())
    assert result.exit_code == 0, result.output
    assert "Active executable" in result.stdout
    assert "Install owner" in result.stdout
    assert "method=uv" in result.stdout
    assert "Omni PATH order" in result.stdout
    assert "1." in result.stdout and "2." in result.stdout
    assert "Conflicting installs" in result.stdout
    assert "".join(str(other_path).split()) in flat


# ── update ───────────────────────────────────────────────────────────────


def test_update_check_compares_versions_without_running(monkeypatch):
    # --check now performs a *real* version comparison, but must never run a
    # subprocess. We patch the remote fetch (no network) + trip subprocess.run.
    import omni.cli.commands.update_cmd as uc
    import omni.runtime.update_check as update_check

    # Pin the *published* path: the real test env is an editable git checkout, so
    # force a non-checkout plan and treat the version as a real release, otherwise
    # the source-checkout branch would ignore version comparison entirely.
    monkeypatch.setattr(
        uc,
        "_plan",
        lambda ref="", **_kwargs: (
            "uv",
            ["uv", "pip", "install", "-U", uc.DIST],
            "uv fake",
        ),
    )
    monkeypatch.setattr(update_check, "is_source_build_version", lambda _v: False)
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda *_a, **_k: "9.9.9")

    def _boom(*_a, **_k):  # pragma: no cover - only fires on regression
        raise AssertionError("--check must not run a subprocess")

    monkeypatch.setattr(uc.subprocess, "run", _boom)
    res = runner.invoke(app, ["update", "--check"])
    assert res.exit_code == 0
    assert "OmniScientist" in res.stdout
    assert "9.9.9" in res.output  # the discovered latest is shown
    assert "--check" in res.output


def test_update_plan_prefers_git_for_editable(monkeypatch):
    # Simulate an editable checkout → plan should be a `git pull` of the source.
    import omni.cli.commands.update_cmd as uc

    fake_src = uc.Path("/tmp/omni-fake-root/cli")
    fake_root = uc.Path("/tmp/omni-fake-root")
    monkeypatch.setattr(uc, "_editable_source", lambda _dist: fake_src)
    monkeypatch.setattr(uc, "_git_root", lambda _source: fake_root)
    kind, argv, _label = uc._plan()
    assert kind == "git"
    assert argv[:3] == ["git", "-C", str(fake_root)] and argv[-1] == "--ff-only"


def test_update_plan_uses_uv_tool_owner_for_a_published_uv_install(monkeypatch):
    import omni.cli.commands.update_cmd as uc
    import omni.runtime.update_check as update_check

    # Simulate a *published* install: neither an editable nor a snapshot source
    # checkout, and a real (comparable) version so the manual fallback is skipped.
    monkeypatch.setattr(uc, "_editable_source", lambda _dist: None)
    monkeypatch.setattr(uc, "_local_source", lambda _dist: None)
    monkeypatch.setattr(update_check, "is_source_build_version", lambda _v: False)
    monkeypatch.setattr(uc, "_installed_source_spec", lambda _dist: uc.DIST)
    monkeypatch.setattr(uc.sys, "executable", "/isolated/omni/bin/python")
    monkeypatch.setattr(uc.sys, "prefix", "/isolated/uv/tools/omniscientist-v2")
    monkeypatch.setattr(uc.shutil, "which", lambda name: "/usr/local/bin/uv" if name == "uv" else None)

    kind, argv, label = uc._plan()

    assert kind == "uv"
    assert argv == [
        "/usr/local/bin/uv",
        "tool",
        "upgrade",
        "OmniScientist-V2",
        "--compile-bytecode",
    ]
    assert "current interpreter" in label


def test_update_plan_uses_current_python_pip_for_an_env_without_uv(monkeypatch):
    import omni.cli.commands.update_cmd as uc
    import omni.runtime.update_check as update_check

    monkeypatch.setattr(uc, "_editable_source", lambda _dist: None)
    monkeypatch.setattr(uc, "_local_source", lambda _dist: None)
    monkeypatch.setattr(update_check, "is_source_build_version", lambda _v: False)
    monkeypatch.setattr(uc, "_installed_source_spec", lambda _dist: uc.DIST)
    monkeypatch.setattr(uc.sys, "executable", "/work/.venv/bin/python")
    monkeypatch.setattr(uc.sys, "prefix", "/work/.venv")
    monkeypatch.setattr(uc.shutil, "which", lambda _name: None)

    kind, argv, _label = uc._plan()

    assert kind == "env"
    assert argv == [
        "/work/.venv/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "OmniScientist-V2",
    ]


def test_update_to_uses_current_python_when_uv_is_unavailable(monkeypatch):
    import omni.cli.commands.update_cmd as uc

    monkeypatch.setattr(uc.shutil, "which", lambda _bin: None)
    monkeypatch.setattr(uc.sys, "executable", "/owned/omni/bin/python")
    res = runner.invoke(
        app,
        ["update", "--to", "OmniScientist-V2==2.0.0", "--check"],
    )
    assert res.exit_code == 0, res.output
    assert "/owned/omni/bin/python -m pip install" in res.output


def test_chat_one_shot_mock():
    res = runner.invoke(app, ["chat", "你好"])
    assert res.exit_code == 0
    assert "mock" in res.stdout.lower()


def test_chat_without_prompt_honors_continue_for_repl_restart(monkeypatch):
    from omni.cli import main as main_module

    seen: list[str | None] = []
    monkeypatch.setattr(
        main_module.resume_cmd,
        "resolve_last",
        lambda _state: "session-to-resume",
    )
    monkeypatch.setattr(
        main_module,
        "_repl",
        lambda _state, *, resume_session_id=None: seen.append(resume_session_id),
    )

    result = runner.invoke(app, ["chat", "--continue"])

    assert result.exit_code == 0
    assert seen == ["session-to-resume"]


def test_free_form_prompt_falls_back_to_chat():
    # bare unknown token must route to chat, not error
    res = runner.invoke(app, ["介绍一下你自己"])
    assert res.exit_code == 0
    assert "mock" in res.stdout.lower()


def test_help_token_shows_cli_help_and_never_routes_to_chat():
    """`omni help` must mirror `omni --help`, not run a chat/agent turn.

    Routing it to ``chat`` (as an unknown command) would build an agent, trip the
    workspace-trust prompt, and try to *answer* "help" as a research question.
    """
    res = runner.invoke(app, ["help"])
    assert res.exit_code == 0
    assert "Usage:" in res.stdout
    assert "Commands" in res.stdout
    # It is the help screen, not a mock chat answer.
    assert "mock" not in res.stdout.lower()


def test_routed_textio_unwraps_rich_file_proxy_without_recursion():
    """Regression for the `omni help` / one-shot hang.

    Rich's ``Live``/``Status`` (``console.status(...)``) redirect the global
    ``sys.stdout`` to a ``FileProxy`` that writes back into our console. Since
    ``RoutedTextIO`` reads its target from ``sys.stdout`` lazily, following that
    proxy recurses forever. It must unwrap to the real stream instead.
    """
    import io
    import sys

    from rich.console import Console
    from rich.file_proxy import FileProxy

    from omni.cli.repl_output import RoutedTextIO

    real = io.StringIO()
    routed = RoutedTextIO(lambda: sys.stdout)
    console = Console(file=routed)
    proxy = FileProxy(console, real)  # emulates the Live/Status redirect_stdout
    saved = sys.stdout
    sys.stdout = proxy
    try:
        written = routed.write("ping\n")  # must not recurse
    finally:
        sys.stdout = saved
    assert written == len("ping\n")
    assert "ping" in real.getvalue()


def test_project_new_and_list():
    res = runner.invoke(app, ["project", "new", "demoproj"])
    assert res.exit_code == 0
    listed = runner.invoke(app, ["project", "list"])
    assert "demoproj" in listed.stdout


# ── unknown-command masking (the heuristic) ──────────────────────────────


def test_botched_command_helper():
    from omni.cli.main import _botched_command

    known = ["config", "exec", "profile", "session", "skills", "cite"]
    # near-miss of a real command → suggest it
    assert "profile" in (_botched_command("profil", ["list"], known) or "")
    assert "exec" in (_botched_command("exce", ["-f", "x"], known) or "")
    # bare ascii word carrying a flag → botched (prompts don't take bare flags)
    assert _botched_command("summarize", ["-v"], known) is not None
    # genuine prompts → routed to chat (None)
    assert _botched_command("hello", [], known) is None
    assert _botched_command("summarize", ["this", "paper"], known) is None
    assert _botched_command("介绍一下", [], known) is None  # non-ascii
    assert _botched_command("2310.06825", [], known) is None  # digit-leading


def test_typo_command_errors_with_suggestion():
    res = runner.invoke(app, ["profil", "list"])
    assert res.exit_code != 0
    assert "profile" in res.output


# ── exec ─────────────────────────────────────────────────────────────────


def test_exec_from_file(tmp_path):
    task = tmp_path / "task.md"
    task.write_text("用一句话解释扩散模型", encoding="utf-8")
    res = runner.invoke(app, ["exec", "-f", str(task), "-q"])
    assert res.exit_code == 0
    assert "mock" in res.stdout.lower()


def test_exec_writes_output(tmp_path):
    task = tmp_path / "task.md"
    task.write_text("hello", encoding="utf-8")
    out = tmp_path / "answer.md"
    res = runner.invoke(app, ["exec", "-f", str(task), "-q", "-o", str(out)])
    assert res.exit_code == 0
    assert out.is_file() and out.read_text(encoding="utf-8").strip()
    assert "Answer written" in res.output


def test_exec_rate_limit_writes_error_report_not_answer(tmp_path, monkeypatch):
    """UX-09 / 于恒彬: a 429 must not be sold as the requested blueprint."""
    from omni.agent.turn_execution import TurnResult

    async def fake_run(*_args, **_kwargs):
        return TurnResult(
            text="Request received: task_id=abc12345; planning...\nHTTP 429 rate limited.",
            session_id="s",
            task_id="abc12345deadbeef",
            kind="error",
            terminated_reason="llm_rate_limited",
            settlement_status="failed",
        )

    monkeypatch.setattr("omni.cli.commands.exec_cmd.run_one_shot", fake_run)
    out = tmp_path / "research_blueprint_v1.md"
    res = runner.invoke(app, ["exec", "write a research blueprint", "-q", "-o", str(out)])

    assert res.exit_code == 1
    assert "Answer written" not in res.output
    assert "Error report written" in res.output
    body = out.read_text(encoding="utf-8")
    assert "not a completed answer" in body
    assert "429" in body


def test_exec_requires_input():
    res = runner.invoke(app, ["exec", "-f", "/nonexistent/path/task.md"])
    assert res.exit_code != 0


# ── profile ──────────────────────────────────────────────────────────────


def test_profile_add_list_use():
    add = runner.invoke(app, ["profile", "add", "local", "-p", "ollama"])
    assert add.exit_code == 0
    listed = runner.invoke(app, ["profile", "list"])
    assert "local" in listed.stdout
    use = runner.invoke(app, ["profile", "use", "local"])
    assert use.exit_code == 0
    # use of a non-existent profile fails
    assert runner.invoke(app, ["profile", "use", "ghost"]).exit_code != 0


# ── session + replay ─────────────────────────────────────────────────────


def test_session_list_and_replay_flow():
    # a one-shot chat creates a session
    assert runner.invoke(app, ["chat", "你好"]).exit_code == 0
    listed = runner.invoke(app, ["session", "list"])
    assert listed.exit_code == 0
    # unknown session id replays as a clean error
    assert runner.invoke(app, ["replay", "deadbeef"]).exit_code != 0


# ── channel ──────────────────────────────────────────────────────────────


def test_channel_add_list():
    add = runner.invoke(app, ["channel", "add", "feishu"])
    assert add.exit_code == 0
    listed = runner.invoke(app, ["channel", "list"])
    assert "feishu" in listed.stdout
    # unknown channel rejected
    assert runner.invoke(app, ["channel", "add", "telegram"]).exit_code != 0


def test_channel_login_feishu_manual_stores_secret_and_pairing():
    from omni.config.paths import get_paths

    secret = "fs-secret-123"
    res = runner.invoke(app, [
        "channel", "login", "feishu",
        "--method", "manual",
        "--app-id", "cli_test_app",
        "--app-secret", secret,
        "--credential-store", "file",
        "--non-interactive",
    ])

    assert res.exit_code == 0
    assert secret not in res.stdout
    assert "/pair " in res.stdout
    assert "applink.feishu.cn/client/bot/open" in res.stdout
    paths = get_paths()
    cfg = tomllib.loads((paths.channels_dir / "feishu.toml").read_text(encoding="utf-8"))
    assert cfg["mode"] == "ws"
    assert cfg["app_id"] == "cli_test_app"
    assert cfg["bot_url"] == "https://applink.feishu.cn/client/bot/open?appId=cli_test_app"
    assert cfg["allowlist_enabled"] is True
    assert cfg["pairing_enabled"] is True
    assert cfg["require_sensitive_confirm"] is True
    secrets = tomllib.loads(paths.secrets_file.read_text(encoding="utf-8"))
    assert secrets["channels"]["feishu"]["app_secret"] == secret
    test = runner.invoke(app, ["channel", "test", "feishu"])
    assert test.exit_code == 0
    assert "Configuration fields are complete" in test.stdout
    assert "Allowlist enabled" in test.stdout


def test_channel_add_wechat_points_at_the_single_login_command():
    res = runner.invoke(app, ["channel", "add", "wechat"])

    assert res.exit_code == 0
    assert "omni channel login wechat --start" in " ".join(res.stdout.split())


def test_channel_login_fails_closed_when_the_named_keychain_is_unavailable(monkeypatch):
    # `--credential-store keychain` names a store this machine cannot provide,
    # so it must fail rather than quietly downgrade to a plaintext file. Only
    # the `auto` default is allowed to fall back.
    import omni.channels.credentials as creds

    monkeypatch.setattr(creds, "_has_macos_keychain", lambda: False)
    res = runner.invoke(app, [
        "channel", "login", "feishu",
        "--method", "manual",
        "--app-id", "cli_test_app",
        "--app-secret", "fs-secret-123",
        "--credential-store", "keychain",
        "--no-qr",
    ])

    assert res.exit_code == 2


def test_default_login_never_asks_the_user_to_choose_a_credential_store(monkeypatch):
    """On Linux and Windows, secrets.toml is where a credential belongs.

    Reporting the platform's normal outcome as a fault, in a message naming
    `--credential-store file`, is what taught users to paste that flag into
    every login. The default has to be a plain statement of what happened.
    """
    import omni.channels.credentials as creds

    monkeypatch.setattr(creds, "_has_macos_keychain", lambda: False)
    res = runner.invoke(app, [
        "channel", "login", "feishu",
        "--app-id", "cli_test_app",
        "--app-secret", "fs-secret-123",
        "--no-qr",
    ], env={"COLUMNS": "200"})  # the store path is one long word; 80 folds it mid-name

    assert res.exit_code == 0
    out = cli_text(res.stdout, res.stderr)
    assert "secrets.toml with mode 0600" in out
    assert "credential-store" not in out
    assert "Fell back" not in out
    assert "no encrypted credential store" not in out


def test_a_keychain_that_refuses_a_write_still_warns(monkeypatch):
    """A locked Keychain is a real anomaly: the secret lands in a weaker store
    than this machine can offer, so it must not read like the normal path."""
    import omni.channels.credentials as creds

    monkeypatch.setattr(creds, "_has_macos_keychain", lambda: True)

    def _refuse(channel: str, key: str, value: str) -> None:
        raise creds.CredentialStoreError("User interaction is not allowed.")

    monkeypatch.setattr(creds, "_store_macos_keychain", _refuse)
    res = runner.invoke(app, [
        "channel", "login", "feishu",
        "--app-id", "cli_test_app",
        "--app-secret", "fs-secret-123",
        "--no-qr",
    ])

    assert res.exit_code == 0
    out = " ".join((res.stdout + res.stderr).split())
    assert "Could not write to the system keychain" in out
    assert "unlock-keychain" in out


def test_channel_login_start_lazy_enables_home_service(monkeypatch):
    from omni.runtime import service_control

    calls: list[str] = []

    def fake_lazy_enable(_settings, *, reason: str = "", wait_s: float = 6.0):
        calls.append(reason)
        return service_control.LifecycleResult(True, "Home service enabled via detached.")

    monkeypatch.setattr(service_control, "lazy_enable", fake_lazy_enable)
    res = runner.invoke(app, [
        "channel", "login", "feishu",
        "--method", "manual",
        "--app-id", "cli_test_app",
        "--app-secret", "fs-secret-123",
        "--credential-store", "file",
        "--non-interactive",
        "--no-qr",
        "--start",
    ])

    assert res.exit_code == 0
    assert calls == ["channel:feishu"]
    assert "always-on home service" in res.stdout


def test_channel_login_dingtalk_manual_renders_bot_url_and_stores_secret():
    from omni.config.paths import get_paths

    secret = "dt-secret-123"
    bot_url = "dingtalk://dingtalkclient/page/link?url=https%3A%2F%2Fexample.test%2Fbot"
    res = runner.invoke(app, [
        "channel", "login", "dingtalk",
        "--method", "manual",
        "--client-id", "ding_client",
        "--client-secret", secret,
        "--credential-store", "file",
        "--bot-url", bot_url,
        "--non-interactive",
        "--no-qr",
    ])

    assert res.exit_code == 0
    assert secret not in res.stdout
    assert bot_url in res.stdout.replace("\n", "")
    assert "/pair " in res.stdout
    paths = get_paths()
    cfg = tomllib.loads((paths.channels_dir / "dingtalk.toml").read_text(encoding="utf-8"))
    assert cfg["mode"] == "stream"
    assert cfg["client_id"] == "ding_client"
    assert cfg["bot_url"] == bot_url
    assert cfg["allowlist_enabled"] is True
    secrets = tomllib.loads(paths.secrets_file.read_text(encoding="utf-8"))
    assert secrets["channels"]["dingtalk"]["client_secret"] == secret


def test_channel_login_wechat_ilink_no_wait_writes_template():
    from omni.config.paths import get_paths

    res = runner.invoke(
        app, ["channel", "login", "wechat", "--method", "ilink", "--no-wait"]
    )

    assert res.exit_code == 0
    paths = get_paths()
    cfg = tomllib.loads((paths.channels_dir / "wechat.toml").read_text(encoding="utf-8"))
    assert cfg["mode"] == "ilink"
    assert cfg["base_url"].startswith("https://")
    assert cfg["allowlist_enabled"] is True
    assert not cfg.get("bot_token")


def test_channel_login_wechat_defaults_to_official_ilink_without_a_gateway():
    # `channel login wechat` with no --method must land on Tencent's official
    # ClawBot API, so the one advertised command needs no self-hosted gateway
    # and behaves identically on Linux, macOS, and Windows.
    from omni.config.paths import get_paths

    res = runner.invoke(app, ["channel", "login", "wechat", "--no-wait"])

    assert res.exit_code == 0
    cfg = tomllib.loads(
        (get_paths().channels_dir / "wechat.toml").read_text(encoding="utf-8")
    )
    assert cfg["mode"] == "ilink"
    assert cfg["base_url"].startswith("https://")
    assert "gateway_url" not in cfg


def test_channel_login_wechat_rejects_removed_gateway_and_wecom_methods():
    for method in ("gateway", "wecom"):
        res = runner.invoke(app, ["channel", "login", "wechat", "--method", method, "--no-wait"])
        assert res.exit_code == 2
        text = res.output
        assert "official ClawBot iLink QR" in text
        assert ":8088" in text


def test_channel_login_feishu_prints_the_pairing_code_and_applink():
    res = runner.invoke(
        app,
        ["channel", "login", "feishu", "--app-id", "cli_demo", "--app-secret", "s3cret",
         "--credential-store", "file", "--no-qr"],
    )

    assert res.exit_code == 0
    out = " ".join(res.stdout.split())
    assert "/pair " in out
    assert "applink.feishu.cn" in out
    assert "s3cret" not in out


def test_channel_login_dingtalk_does_not_offer_a_qr_of_the_developer_guide():
    """DingTalk has no deep link to a bot chat, so the only URL is documentation.

    Rendering that as a QR wastes twenty lines and invites the user to scan
    their way into a developer page instead of the bot.
    """
    res = runner.invoke(
        app,
        ["channel", "login", "dingtalk", "--client-id", "ding_demo",
         "--client-secret", "s3cret", "--credential-store", "file"],
    )

    assert res.exit_code == 0
    out = " ".join(res.stdout.split())
    assert "/pair " in out
    assert "open.dingtalk.com" in out
    assert "█" not in out and "▀" not in out


def test_channel_pair_reissues_a_code_after_the_first_one_expires():
    """A pairing code is single-use and lives 10 minutes, so it must be re-issuable."""
    from omni.config.paths import get_paths

    runner.invoke(
        app,
        ["channel", "login", "feishu", "--app-id", "cli_demo", "--app-secret", "s3cret",
         "--credential-store", "file", "--no-qr"],
    )
    cfg_path = get_paths().channels_dir / "feishu.toml"
    first = tomllib.loads(cfg_path.read_text(encoding="utf-8"))["pairing_code_hash"]

    res = runner.invoke(app, ["channel", "pair", "feishu", "--no-qr"])

    assert res.exit_code == 0
    assert "/pair " in res.stdout
    reissued = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert reissued["pairing_code_hash"] != first
    assert reissued["app_id"] == "cli_demo"


def test_channel_pair_before_login_says_what_to_run_first():
    res = runner.invoke(app, ["channel", "pair", "dingtalk"])

    assert res.exit_code == 2
    assert "omni channel login dingtalk" in " ".join((res.stdout + res.stderr).split())


def test_channel_pair_rejects_wechat_which_binds_the_scanning_account():
    res = runner.invoke(app, ["channel", "pair", "wechat"])

    assert res.exit_code == 2
    assert "feishu" in res.stdout + res.stderr


def test_channel_remove_rejects_unknown_names_and_path_escape():
    from omni.config.paths import get_paths

    paths = get_paths()
    paths.home.mkdir(parents=True, exist_ok=True)
    marker = "must-not-delete = true\n"
    paths.config_file.write_text(marker, encoding="utf-8")
    paths.channels_dir.mkdir(parents=True, exist_ok=True)
    paths.channels_dir.joinpath("wechat.toml").write_text('mode = "ilink"\n', encoding="utf-8")

    unknown = runner.invoke(app, ["channel", "remove", "../config", "--purge"])
    absolute = runner.invoke(app, ["channel", "remove", str(paths.home / "config"), "--purge"])
    cli_channel = runner.invoke(app, ["channel", "remove", "cli", "--purge"])

    assert unknown.exit_code != 0
    assert absolute.exit_code != 0
    assert cli_channel.exit_code != 0
    assert paths.config_file.read_text(encoding="utf-8") == marker

    removed = runner.invoke(app, ["channel", "remove", "wechat", "--purge"])
    assert removed.exit_code == 0
    assert not paths.channels_dir.joinpath("wechat.toml").is_file()
    assert paths.config_file.is_file()


# ── cite ─────────────────────────────────────────────────────────────────


def test_cite_export_empty_library():
    res = runner.invoke(app, ["cite", "export"])
    assert res.exit_code == 0
    assert "library is empty" in res.stdout
