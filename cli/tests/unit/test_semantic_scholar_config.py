"""Owner-scoped Semantic Scholar configuration contracts."""

from __future__ import annotations

import os
import stat
import tomllib
from typing import Any

from typer.testing import CliRunner

from omni.cli.main import app
from omni.config import load_settings
from omni.config.paths import get_paths
from omni.research import connectors

runner = CliRunner()


def test_config_semantic_scholar_writes_only_private_secrets(
    monkeypatch: Any,
) -> None:
    received: dict[str, Any] = {}

    async def fake_search(
        query: str,
        *,
        rows: int,
        api_key: str,
    ) -> list[dict[str, Any]]:
        received.update(query=query, rows=rows, api_key=api_key)
        return [{"title": "Test paper"}]

    monkeypatch.setattr(connectors, "semanticscholar_search", fake_search)
    secret = "s2-owner-secret-value"

    result = runner.invoke(
        app,
        ["config", "semantic-scholar", "-k", secret, "--test"],
    )

    assert result.exit_code == 0
    assert secret not in result.stdout
    assert "credentials are working" in result.stdout
    assert received == {
        "query": "automated peer review large language model",
        "rows": 1,
        "api_key": secret,
    }
    paths = get_paths()
    assert not paths.config_file.is_file() or secret not in paths.config_file.read_text(
        encoding="utf-8"
    )
    with paths.secrets_file.open("rb") as handle:
        data = tomllib.load(handle)
    assert data["research"]["semantic_scholar_api_key"] == secret
    if os.name == "posix":
        assert stat.S_IMODE(paths.secrets_file.stat().st_mode) == 0o600
    assert load_settings().research.semantic_scholar_api_key == secret


def test_config_semantic_scholar_status_is_redacted() -> None:
    first = runner.invoke(
        app,
        ["config", "semantic-scholar", "-k", "never-print-this-key"],
    )
    assert first.exit_code == 0

    shown = runner.invoke(app, ["config", "semantic-scholar"])

    assert shown.exit_code == 0
    assert "***set***" in shown.stdout
    assert "never-print-this-key" not in shown.stdout
