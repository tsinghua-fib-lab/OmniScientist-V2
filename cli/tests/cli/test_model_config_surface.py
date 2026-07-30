"""The model surface is a typed, explainable facade over existing Home config.

These tests deliberately pin scope as well as values: ``/model`` must make the
three existing model roles easier to configure without turning persistent Home
configuration into session state or changing the runtime execution contract.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomli_w
from typer.testing import CliRunner

from omni.cli.main import app

runner = CliRunner()


def _write_toml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        tomli_w.dump(data, stream)


def _read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_model_source_resolution_preserves_the_existing_layer_order(tmp_path: Path) -> None:
    from omni.config import resolve_settings
    from omni.config.paths import get_paths

    paths = get_paths(cwd=tmp_path)
    _write_toml(
        paths.config_file,
        {
            "model": {
                "provider": "home-provider",
                "base_url": "https://home.example/v1",
                "model": "home-model",
            }
        },
    )
    _write_toml(
        paths.home / "dev.config.toml",
        {"model": {"provider": "profile-provider", "model": "profile-model"}},
    )
    _write_toml(
        paths.project_config,
        {
            "model": {
                "provider": "project-provider",
                "model": "project-model",
                "base_url": "https://project-must-not-override.example/v1",
            },
            "vlm": {"model": "project-must-not-override-vlm"},
        },
    )
    _write_toml(paths.secrets_file, {"model": {"api_key": "owner-secret"}})

    os.environ["OMNI_MODEL"] = "environment-model"
    os.environ["OMNI_VLM_MODEL"] = "environment-vlm"
    try:
        resolution = resolve_settings(
            profile="dev",
            cwd=tmp_path,
            trusted=True,
            overrides={"model": {"model": "explicit-model"}},
        )
    finally:
        os.environ.pop("OMNI_MODEL", None)
        os.environ.pop("OMNI_VLM_MODEL", None)

    settings = resolution.settings
    assert settings.model.model == "explicit-model"
    assert resolution.source_for("model.model").kind == "override"
    assert settings.model.provider == "project-provider"
    assert resolution.source_for("model.provider").kind == "project"
    assert settings.model.base_url == "https://home.example/v1"
    assert resolution.source_for("model.base_url").kind == "user"
    assert settings.model.api_key == "owner-secret"
    assert resolution.source_for("model.api_key").kind == "secrets"
    assert settings.vlm.model == "environment-vlm"
    assert resolution.source_for("vlm.model").kind == "environment"

    project_skipped_when_untrusted = resolve_settings(
        profile="dev", cwd=tmp_path, trusted=False
    )
    assert project_skipped_when_untrusted.settings.model.provider == "profile-provider"
    assert project_skipped_when_untrusted.source_for("model.provider").kind == "profile"

    project_wins_without_an_explicit_override = resolve_settings(
        profile="dev", cwd=tmp_path, trusted=True
    )
    assert project_wins_without_an_explicit_override.settings.model.model == "project-model"
    assert project_wins_without_an_explicit_override.source_for("model.model").kind == "project"


def test_typed_model_stack_maps_the_existing_three_roles() -> None:
    from omni.config import resolve_settings
    from omni.config.model_stack import ModelRole, providers_for, resolve_model_stack

    stack = resolve_model_stack(
        resolve_settings(
            overrides={
                "model": {
                    "provider": "openai",
                    "model": "gpt-main",
                    "api_key": "typed-stack-must-redact-main",
                },
                "vlm": {
                    "enabled": True,
                    "model": "gpt-vision",
                    "endpoint": "https://vision.example/v1/chat/completions",
                    "api_key": "typed-stack-must-redact-vision",
                },
                "memory": {
                    "embeddings_enabled": True,
                    "embedding_provider": "openai_compatible",
                    "embedding_model": "text-embedding-test",
                    "embedding_base_url": "https://embed.example/v1",
                    "embedding_api_key": "typed-stack-must-redact-embedding",
                },
            }
        )
    )

    assert tuple(item.role for item in stack.roles) == (
        ModelRole.MAIN,
        ModelRole.VISION,
        ModelRole.EMBEDDING,
    )
    assert stack.for_role("main").model == "gpt-main"
    assert stack.for_role("vlm").model == "gpt-vision"
    assert stack.for_role("embeddings").model == "text-embedding-test"
    assert "typed-stack-must-redact" not in repr(stack)
    assert {item.key for item in providers_for("main")} >= {
        "mock",
        "openai",
        "deepseek",
        "ollama",
    }
    assert {item.key for item in providers_for("vision")} == {"openai"}
    assert {item.key for item in providers_for("embedding")} >= {
        "openai",
        "ollama",
        "specter2",
    }
    vision = providers_for("vision")[0]
    assert vision.protocol_for("vision") == "openai_compatible_chat"

    vision_fields = {field.path for field in stack.for_role("vision").fields}
    embedding_fields = {field.path for field in stack.for_role("embedding").fields}
    assert "vlm.timeout_s" in vision_fields
    assert embedding_fields >= {
        "memory.embedding_dim",
        "memory.embedding_specter2_python",
        "memory.embedding_specter2_base_model",
        "memory.embedding_specter2_adapter",
        "memory.embedding_specter2_device",
    }


def test_init_and_model_facade_share_the_main_provider_catalog() -> None:
    from omni.cli.commands.init_cmd import _PROVIDER_PRESETS
    from omni.config.model_stack import MODEL_PROVIDER_CATALOG, ModelRole

    expected = [
        (item.key, item.label, item.default_endpoint, item.default_model)
        for item in MODEL_PROVIDER_CATALOG
        if ModelRole.MAIN in item.roles
    ]
    assert _PROVIDER_PRESETS == expected
    assert [item[0] for item in _PROVIDER_PRESETS] == [
        "openai",
        "deepseek",
        "ollama",
        "mock",
    ]


def test_model_main_keeps_the_existing_home_scope_when_a_profile_is_active() -> None:
    home = Path(os.environ["OMNI_HOME"])
    profile = home / "dev.config.toml"
    _write_toml(
        profile,
        {"model": {"provider": "openai", "model": "profile-model"}},
    )

    result = runner.invoke(
        app,
        ["--profile", "dev", "model", "main", "--model", "saved-home-model"],
    )

    assert result.exit_code == 0, result.output
    assert _read_toml(home / "config.toml")["model"]["model"] == "saved-home-model"
    assert _read_toml(profile)["model"]["model"] == "profile-model"
    assert "Home" in result.output
    assert "profile" in result.output
    assert "still overrides" in result.output


def test_model_save_reports_a_profile_that_only_shadows_the_changed_provider() -> None:
    home = Path(os.environ["OMNI_HOME"])
    _write_toml(
        home / "dev.config.toml",
        {"model": {"provider": "profile-provider"}},
    )

    result = runner.invoke(
        app,
        ["--profile", "dev", "model", "main", "--provider", "openai"],
    )

    assert result.exit_code == 0, result.output
    assert "still overrides" in result.output
    assert "model.provider" in result.output
    assert "profile-provider" not in result.output


def test_model_role_commands_reuse_existing_config_and_secret_files() -> None:
    home = Path(os.environ["OMNI_HOME"])

    main = runner.invoke(
        app,
        [
            "model",
            "main",
            "--provider",
            "openai",
            "--base-url",
            "https://main.example/v1",
            "--model",
            "main-model",
            "--api-key",
            "main-secret",
        ],
    )
    vision = runner.invoke(
        app,
        [
            "model",
            "vision",
            "--endpoint",
            "https://vision.example/v1/chat/completions",
            "--model",
            "vision-model",
            "--api-key",
            "vision-secret",
        ],
    )
    embedding = runner.invoke(
        app,
        [
            "model",
            "embedding",
            "--enable",
            "--base-url",
            "https://embed.example/v1",
            "--model",
            "embedding-model",
            "--api-key",
            "embedding-secret",
        ],
    )

    assert main.exit_code == vision.exit_code == embedding.exit_code == 0
    public = _read_toml(home / "config.toml")
    secrets = _read_toml(home / "secrets.toml")
    assert public["model"]["model"] == "main-model"
    assert public["vlm"]["model"] == "vision-model"
    assert public["memory"]["embedding_model"] == "embedding-model"
    assert secrets["model"]["api_key"] == "main-secret"
    assert secrets["vlm"]["api_key"] == "vision-secret"
    assert secrets["memory"]["embedding_api_key"] == "embedding-secret"


def test_model_configuration_is_isolated_by_the_existing_omni_home(tmp_path: Path) -> None:
    first_home = tmp_path / "first" / ".omni"
    second_home = tmp_path / "second" / ".omni"
    first_home.mkdir(parents=True)
    second_home.mkdir(parents=True)

    first = runner.invoke(
        app,
        ["model", "main", "--model", "first-home-model"],
        env={"OMNI_HOME": str(first_home)},
    )
    assert first.exit_code == 0, first.output
    assert _read_toml(first_home / "config.toml")["model"]["model"] == "first-home-model"
    assert not (second_home / "config.toml").exists()

    second = runner.invoke(
        app,
        ["model", "main", "--model", "second-home-model"],
        env={"OMNI_HOME": str(second_home)},
    )
    assert second.exit_code == 0, second.output
    assert _read_toml(second_home / "config.toml")["model"]["model"] == "second-home-model"
    assert _read_toml(first_home / "config.toml")["model"]["model"] == "first-home-model"


def test_model_name_shortcut_persists_a_known_preset() -> None:
    home = Path(os.environ["OMNI_HOME"])

    result = runner.invoke(app, ["model", "deepseek-chat"])

    assert result.exit_code == 0, result.output
    saved = _read_toml(home / "config.toml")["model"]
    assert saved["provider"] == "deepseek"
    assert saved["model"] == "deepseek-chat"
    assert "deepseek.com" in saved["base_url"]


def test_model_use_keeps_the_configured_endpoint_for_the_same_vendor() -> None:
    home = Path(os.environ["OMNI_HOME"])
    _write_toml(
        home / "config.toml",
        {
            "model": {
                "provider": "openai",
                "base_url": "https://already-configured.example/v1",
                "model": "gpt-4o-mini",
            }
        },
    )

    result = runner.invoke(app, ["model", "use", "gpt-4.1"])

    assert result.exit_code == 0, result.output
    saved = _read_toml(home / "config.toml")["model"]
    assert saved["model"] == "gpt-4.1"
    assert saved["base_url"] == "https://already-configured.example/v1"
    assert saved["provider"] == "openai"


def test_unknown_model_name_on_mock_is_rejected() -> None:
    result = runner.invoke(app, ["model", "totally-custom-finetune"])

    assert result.exit_code == 2
    assert "Unknown model" in result.output
    assert not (Path(os.environ["OMNI_HOME"]) / "config.toml").exists()


def test_model_status_warns_when_environment_is_not_persisted(monkeypatch) -> None:
    monkeypatch.setenv("OMNI_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OMNI_MODEL_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OMNI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "env-secret")

    result = runner.invoke(app, ["model", "status"])

    assert result.exit_code == 0, result.output
    assert "gpt-4o-mini" in result.output
    assert "not persisted" in result.output
    assert not (Path(os.environ["OMNI_HOME"]) / "config.toml").exists()


def test_init_y_persists_a_complete_environment_model_instead_of_mock(
    monkeypatch,
) -> None:
    home = Path(os.environ["OMNI_HOME"])
    monkeypatch.setenv("OMNI_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OMNI_MODEL_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OMNI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "env-secret")

    result = runner.invoke(app, ["init", "-y"])

    assert result.exit_code == 0, result.output
    public = _read_toml(home / "config.toml")["model"]
    secrets = _read_toml(home / "secrets.toml")["model"]
    assert public["provider"] == "openai"
    assert public["model"] == "gpt-4o-mini"
    assert public["base_url"] == "https://api.openai.com/v1"
    assert secrets["api_key"] == "env-secret"
    assert "mock" not in result.output.lower() or "openai" in result.output.lower()


def test_picker_lists_presets_and_selects_the_first(monkeypatch) -> None:
    from omni.cli.commands import model_cmd
    from omni.cli.state import AppState
    from omni.config.model_stack import ModelRole

    monkeypatch.setattr(model_cmd, "prompt_text", lambda *_args, **_kwargs: "1")

    edit = model_cmd.prompt_model_edit(AppState())

    assert edit is not None
    assert edit.role is ModelRole.MAIN
    assert edit.values["provider"] == "openai"
    assert edit.values["model"] == "gpt-4o-mini"


def test_startup_model_override_remains_non_persistent() -> None:
    home = Path(os.environ["OMNI_HOME"])

    result = runner.invoke(app, ["--model", "temporary-model", "model", "status"])

    assert result.exit_code == 0, result.output
    assert "temporary-model" in result.output
    assert "override" in result.output
    assert not (home / "config.toml").exists()
    assert not (home / "secrets.toml").exists()


def test_model_status_and_explain_redact_credentials_and_show_sources() -> None:
    secret = "must-never-appear-in-model-output"
    configured = runner.invoke(
        app,
        [
            "model",
            "main",
            "--provider",
            "openai",
            "--base-url",
            "https://main.example/v1",
            "--model",
            "main-model",
            "--api-key",
            secret,
        ],
    )
    assert configured.exit_code == 0

    status = runner.invoke(app, ["model", "status"])
    explain = runner.invoke(app, ["model", "explain", "main"])

    assert status.exit_code == explain.exit_code == 0
    for role in ("main", "vision", "embedding"):
        assert role in status.output.lower()
    assert "configured" in status.output.lower()
    assert "model.api_key" in explain.output
    assert "secrets" in explain.output
    assert secret not in status.output
    assert secret not in explain.output


def test_model_status_and_explain_redact_endpoint_credentials() -> None:
    endpoint = (
        "https://endpoint-user:endpoint-password@main.example/v1"
        "?api_key=endpoint-query-secret#endpoint-fragment-secret"
    )
    configured = runner.invoke(
        app,
        ["model", "main", "--base-url", endpoint, "--model", "main-model"],
    )
    assert configured.exit_code == 0, configured.output

    status = runner.invoke(app, ["model", "status"])
    explain = runner.invoke(app, ["model", "explain", "main"])

    assert status.exit_code == explain.exit_code == 0
    combined = configured.output + status.output + explain.output
    assert "main.example" in combined
    assert "REDACTED" in combined
    for secret_part in (
        "endpoint-user",
        "endpoint-password",
        "endpoint-query-secret",
        "endpoint-fragment-secret",
    ):
        assert secret_part not in combined

    from omni.config import resolve_settings
    from omni.config.model_stack import resolve_model_stack

    stack = resolve_model_stack(resolve_settings())
    assert "endpoint-password" not in repr(stack)
    assert "endpoint-query-secret" not in repr(stack)


def test_read_only_role_commands_never_echo_raw_endpoint_suffixes() -> None:
    home = Path(os.environ["OMNI_HOME"])
    _write_toml(
        home / "config.toml",
        {
            "vlm": {
                "endpoint": "https://vision.example/v1?token=vision-secret",
                "model": "vision-model",
            },
            "memory": {
                "embedding_base_url": "https://embed.example/v1?token=embed-secret",
                "embedding_model": "embed-model",
            },
        },
    )

    vision = runner.invoke(app, ["model", "vision"])
    embedding = runner.invoke(app, ["model", "embedding"])

    assert vision.exit_code == embedding.exit_code == 0
    assert "vision-secret" not in vision.output
    assert "embed-secret" not in embedding.output
    assert "REDACTED" in vision.output


def test_local_embedding_picker_preserves_the_configured_device(monkeypatch) -> None:
    from omni.cli.commands import model_cmd
    from omni.config import resolve_settings
    from omni.config.model_stack import ModelRole, resolve_model_stack

    stack = resolve_model_stack(
        resolve_settings(
            overrides={
                "memory": {
                    "embedding_provider": "specter2",
                    "embedding_model": "specter-model",
                    "embedding_specter2_python": "/runtime/python",
                    "embedding_specter2_base_model": "/models/base",
                    "embedding_specter2_adapter": "/models/adapter",
                    "embedding_specter2_device": "cuda:2",
                }
            }
        )
    )
    values = {
        "Embedding mode (remote/local/disable)": "local",
        "SPECTER2 Python executable": "/runtime/python",
        "SPECTER2 base-model directory": "/models/base",
        "SPECTER2 adapter directory": "/models/adapter",
        "Embedding model": "specter-model",
    }

    def fake_prompt(label: str, default: str = "") -> str:
        if label == "Device":
            assert default == "cuda:2"
            return default
        return values[label]

    monkeypatch.setattr(model_cmd, "prompt_text", fake_prompt)

    edit = model_cmd._prompt_embedding(stack)

    assert edit is not None
    assert edit.role is ModelRole.EMBEDDING
    assert edit.values["device"] == "cuda:2"


@pytest.mark.asyncio
async def test_repl_model_mutation_reloads_only_after_a_successful_save(monkeypatch) -> None:
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    calls: list[str] = []

    class OldAgent:
        async def aclose(self) -> None:
            calls.append("closed")

    new_agent = SimpleNamespace()

    async def fake_external(_state, command):  # noqa: ANN001
        calls.append(command)
        return 0

    async def fake_make_agent(_state):  # noqa: ANN001
        calls.append("reloaded")
        return new_agent

    monkeypatch.setattr(cli_main, "_run_repl_external_command", fake_external)
    monkeypatch.setattr(cli_main, "make_agent", fake_make_agent)

    result = await cli_main._repl_command(
        OldAgent(), AppState(), "/model main --model next-model", "session-1"
    )

    assert result.agent is new_agent
    assert calls == ["/model main --model next-model", "closed", "reloaded"]


@pytest.mark.asyncio
async def test_repl_model_name_shortcut_reloads_after_save(monkeypatch) -> None:
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    calls: list[str] = []

    class OldAgent:
        async def aclose(self) -> None:
            calls.append("closed")

    new_agent = SimpleNamespace()

    async def fake_external(_state, command):  # noqa: ANN001
        calls.append(command)
        return 0

    async def fake_make_agent(_state):  # noqa: ANN001
        calls.append("reloaded")
        return new_agent

    monkeypatch.setattr(cli_main, "_run_repl_external_command", fake_external)
    monkeypatch.setattr(cli_main, "make_agent", fake_make_agent)

    result = await cli_main._repl_command(
        OldAgent(), AppState(), "/model deepseek-chat", "session-1"
    )

    assert result.agent is new_agent
    assert calls == ["/model deepseek-chat", "closed", "reloaded"]


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["status", "explain main", "main --test", "main --help"])
async def test_repl_model_read_only_actions_do_not_reload_agent(monkeypatch, argument) -> None:
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    calls: list[str] = []

    class Agent:
        async def aclose(self) -> None:
            calls.append("closed")

    agent = Agent()

    async def fake_external(_state, command):  # noqa: ANN001
        calls.append(command)
        return 0

    monkeypatch.setattr(cli_main, "_run_repl_external_command", fake_external)
    monkeypatch.setattr(
        cli_main,
        "make_agent",
        lambda _state: (_ for _ in ()).throw(AssertionError("must not reload")),
    )

    result = await cli_main._repl_command(
        agent,
        AppState(),
        f"/model {argument}",
        "session-1",
    )

    assert result.agent is agent
    assert calls == [f"/model {argument}"]


@pytest.mark.asyncio
async def test_bare_repl_model_cancel_has_no_configuration_or_agent_side_effect(
    monkeypatch,
) -> None:
    from omni.cli import main as cli_main
    from omni.cli.commands import model_cmd
    from omni.cli.state import AppState

    calls: list[str] = []

    class Agent:
        async def aclose(self) -> None:
            calls.append("closed")

    agent = Agent()
    monkeypatch.setattr(model_cmd, "prompt_model_edit", lambda _state: None)
    monkeypatch.setattr(
        cli_main,
        "make_agent",
        lambda _state: (_ for _ in ()).throw(AssertionError("must not reload")),
    )

    result = await cli_main._repl_command(agent, AppState(), "/model", "session-1")

    assert result.agent is agent
    assert calls == []


@pytest.mark.asyncio
async def test_bare_repl_model_applies_one_home_edit_then_reloads(monkeypatch) -> None:
    from omni.cli import main as cli_main
    from omni.cli.commands import model_cmd
    from omni.cli.state import AppState
    from omni.config.model_stack import ModelRole

    calls: list[str] = []

    class OldAgent:
        async def aclose(self) -> None:
            calls.append("closed")

    new_agent = SimpleNamespace()
    edit = model_cmd.ModelEdit(ModelRole.MAIN, {"model": "picked-model"})
    monkeypatch.setattr(model_cmd, "prompt_model_edit", lambda _state: edit)
    monkeypatch.setattr(
        model_cmd,
        "apply_model_edit",
        lambda _state, selected: calls.append(f"applied:{selected.values['model']}"),
    )

    async def fake_make_agent(_state):  # noqa: ANN001
        calls.append("reloaded")
        return new_agent

    monkeypatch.setattr(cli_main, "make_agent", fake_make_agent)

    result = await cli_main._repl_command(
        OldAgent(), AppState(), "/model", "session-1"
    )

    assert result.agent is new_agent
    assert calls == ["applied:picked-model", "closed", "reloaded"]


@pytest.mark.asyncio
async def test_repl_model_reload_preserves_the_active_tui_approver(monkeypatch) -> None:
    from omni.cli import approval_prompt
    from omni.cli import main as cli_main
    from omni.cli.state import AppState

    tui = object()
    approver = object()
    new_agent = SimpleNamespace(approver=None)

    class OldAgent:
        async def aclose(self) -> None:
            return None

    async def fake_make_agent(_state):  # noqa: ANN001
        return new_agent

    monkeypatch.setattr(cli_main, "_active_repl_tui", lambda: tui)
    monkeypatch.setattr(cli_main, "make_agent", fake_make_agent)
    monkeypatch.setattr(
        approval_prompt,
        "build_tui_approver",
        lambda selected: approver if selected is tui else None,
    )

    result = await cli_main._reload_repl_agent(OldAgent(), AppState())

    assert result is new_agent
    assert result.approver is approver


def test_model_is_queued_instead_of_running_inside_an_active_turn() -> None:
    from omni.cli.main import _REPL_BLOCKED_DURING_TURN, _REPL_LIVE_DURING_TURN

    assert "model" not in _REPL_LIVE_DURING_TURN
    assert "model" not in _REPL_BLOCKED_DURING_TURN


@pytest.mark.asyncio
async def test_model_submission_waits_for_the_active_turn_to_finish() -> None:
    import asyncio

    from omni.cli.main import _monitor_foreground_turn, _ReplControls
    from omni.cli.repl_tui import ReplSubmission, ReplTui

    async def active_turn() -> object:
        await asyncio.sleep(0.02)
        return object()

    tui = ReplTui(commands=("/model",))
    tui.set_busy(True)
    tui._submissions.put_nowait(
        ReplSubmission(
            turn_id="model-config-after-turn",
            text="/model main --model next-model",
            disposition="submit",
        )
    )

    outcome = await _monitor_foreground_turn(
        asyncio.create_task(active_turn()),
        tui=tui,
        agent=SimpleNamespace(tasks=SimpleNamespace()),
        task_ref={"task_id": ""},
        state=SimpleNamespace(),
        session_id="session-1",
        controls=_ReplControls(
            interaction_mode="auto",
            display_verbosity="normal",
        ),
    )

    assert [item.text for item in outcome.queued_lines] == [
        "/model main --model next-model"
    ]
