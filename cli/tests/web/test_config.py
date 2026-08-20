"""Web config RPC must write the same user files as ``omni config``."""

from __future__ import annotations

import os
import stat
import tomllib

import httpx
import pytest
from typer.testing import CliRunner

from omni.cli.main import app as cli_app
from omni.config import load_settings
from omni.config.paths import get_paths
from omni.config.user_edits import apply_config_value, setup_required

pytest.importorskip("starlette")

from omni.web.app import create_app  # noqa: E402

runner = CliRunner()


async def _rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    res = await client.post(
        "/api",
        headers={"X-Omni-Web": "1"},
        json={"method": method, "params": params or {}},
    )
    assert res.status_code == 200, res.text
    return res.json()


@pytest.fixture
def app_client():
    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    return app, transport


@pytest.mark.asyncio
async def test_describe_does_not_require_workspace(app_client) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        data = await _rpc(client, "config.describe")
    assert data["ok"] is True
    assert "setup_required" in data
    assert "rows" in data
    assert "blocks" in data
    assert data["paths"]["user_config"].endswith("config.toml")
    keys = {row["key"] for row in data["rows"]}
    assert "model.provider" in keys
    assert "vlm.endpoint" in keys
    assert "research.semantic_scholar_api_key" in keys


@pytest.mark.asyncio
async def test_set_matches_cli_config_set(app_client) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        web = await _rpc(client, "config.set", {"key": "react.max_iterations", "value": "7"})
    assert web["ok"] is True
    assert web["key"] == "react.max_iterations"
    paths = get_paths()
    raw = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
    assert raw["react"]["max_iterations"] == 7
    cli = runner.invoke(cli_app, ["config", "get", "react.max_iterations"])
    assert cli.exit_code == 0
    assert "7" in cli.stdout


@pytest.mark.asyncio
async def test_secret_goes_to_secrets_toml_and_is_redacted(app_client) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        written = await _rpc(client, "config.set", {"key": "model.api_key", "value": "sk-secret-token"})
        described = await _rpc(client, "config.describe")
        fetched = await _rpc(client, "config.get", {"key": "model.api_key"})
    assert written["ok"] is True
    assert written["secret"] is True
    paths = get_paths()
    secrets = tomllib.loads(paths.secrets_file.read_text(encoding="utf-8"))
    assert secrets["model"]["api_key"] == "sk-secret-token"
    if os.name == "posix":
        assert stat.S_IMODE(paths.secrets_file.stat().st_mode) == 0o600
    public = tomllib.loads(paths.config_file.read_text(encoding="utf-8")) if paths.config_file.is_file() else {}
    assert "api_key" not in public.get("model", {})
    model_row = next(row for row in described["rows"] if row["key"] == "model.api_key")
    assert model_row["set"] is True
    assert model_row["value"] == "***set***"
    assert "sk-secret-token" not in str(described)
    assert fetched["set"] is True
    assert "sk-secret-token" not in str(fetched["value"])


@pytest.mark.asyncio
async def test_apply_model_vlm_and_s2_write_same_keys_as_cli(app_client) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        model = await _rpc(
            client,
            "config.applyModel",
            {
                "provider": "openai",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key": "sk-web-model",
            },
        )
        vlm = await _rpc(
            client,
            "config.applyVlm",
            {
                "endpoint": "https://vision.example/v1/chat/completions",
                "model": "vision-x",
                "api_key": "sk-web-vlm",
            },
        )
        s2 = await _rpc(
            client,
            "config.applySemanticScholar",
            {"api_key": "s2-web-key"},
        )
        empty_vlm = await _rpc(client, "config.applyVlm", {})
        empty_s2 = await _rpc(client, "config.applySemanticScholar", {})
    assert model["ok"] is True
    assert vlm["ok"] is True
    assert s2["ok"] is True
    assert empty_vlm["changed"] == []
    assert empty_s2["changed"] == []
    settings = load_settings()
    assert settings.model.provider == "openai"
    assert settings.model.base_url == "https://api.deepseek.com/v1"
    assert settings.model.model == "deepseek-chat"
    assert settings.model.api_key == "sk-web-model"
    assert settings.vlm.enabled is True
    assert settings.vlm.model == "vision-x"
    assert settings.vlm.api_key == "sk-web-vlm"
    assert settings.research.semantic_scholar_api_key == "s2-web-key"


@pytest.mark.asyncio
async def test_first_run_mock_persists_config_and_clears_setup(app_client) -> None:
    paths = get_paths()
    if paths.config_file.is_file():
        paths.config_file.unlink()
    assert setup_required(load_settings()) is True
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        before = await _rpc(client, "config.describe")
        assert before["setup_required"] is True
        saved = await _rpc(
            client,
            "config.applyModel",
            {"provider": "mock", "model": "omni-mock"},
        )
        after = await _rpc(client, "config.describe")
    assert saved["ok"] is True
    assert paths.config_file.is_file()
    assert after["setup_required"] is False


@pytest.mark.asyncio
async def test_unset_and_advanced_json_value(app_client) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(client, "config.set", {"key": "channels.enabled", "value": ["cli", "web"]})
        got = await _rpc(client, "config.get", {"key": "channels.enabled"})
        removed = await _rpc(client, "config.unset", {"key": "channels.enabled"})
        missing = await _rpc(client, "config.unset", {"key": "channels.enabled"})
    assert got["value"] == ["cli", "web"]
    assert removed["ok"] is True
    assert missing["ok"] is False
    assert missing["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_apply_embeddings_disable_keeps_endpoint(app_client) -> None:
    paths = get_paths()
    apply_config_value(paths, "memory.embedding_base_url", "https://embed.example/v1")
    apply_config_value(paths, "memory.embedding_model", "bge-m3")
    apply_config_value(paths, "memory.embeddings_enabled", "true")
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        data = await _rpc(client, "config.applyEmbeddings", {"enabled": False})
    assert data["ok"] is True
    settings = load_settings()
    assert settings.memory.embeddings_enabled is False
    assert settings.memory.embedding_base_url == "https://embed.example/v1"
    assert settings.memory.embedding_model == "bge-m3"


@pytest.mark.asyncio
async def test_write_drops_cached_agent(app_client, tmp_path) -> None:
    from omni.config import trust as trustmod

    work = tmp_path / "cfg-ws"
    work.mkdir()
    trustmod.set_trusted(work)
    app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        opened = await _rpc(client, "workspace.open", {"path": str(work)})
        assert opened["ok"] is True
        key = opened["workspace"]["project_dir"]
        assert any(
            cached == key or cached.startswith(f"{key}::")
            for cached in app.state.hub._agents
        )
        await _rpc(client, "config.set", {"key": "display.verbosity", "value": "verbose"})
        assert not app.state.hub._agents


@pytest.mark.asyncio
async def test_data_dir_is_readonly_via_set(app_client) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        data = await _rpc(client, "config.set", {"key": "data_dir", "value": "/tmp/nope"})
    assert data["ok"] is False
    assert "home" in data["error"]["message"]


@pytest.mark.asyncio
async def test_config_writes_freeze_after_home_drift(app_client) -> None:
    app, transport = app_client
    app.state.web_home = "/not/the/current/omni/home"
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        written = await _rpc(client, "config.set", {"key": "react.max_iterations", "value": "3"})
        described = await _rpc(client, "config.describe")
        fetched = await _rpc(client, "config.get", {"key": "react.max_iterations"})

    assert written["ok"] is False
    assert written["error"]["code"] == "restart_required"
    assert described["ok"] is True
    assert described["restart_required"] is True
    assert "Restart this omni web process" in described["notice"]
    assert fetched["ok"] is True
    assert fetched["restart_required"] is True
    paths = get_paths()
    raw = tomllib.loads(paths.config_file.read_text(encoding="utf-8")) if paths.config_file.is_file() else {}
    assert raw.get("react", {}).get("max_iterations") != 3
