"""Bind policy: loopback only, refuse 0.0.0.0 before the server starts."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from omni.cli.main import app
from omni.web.bind import ready_url, validate_bind_host


def test_validate_bind_host_accepts_loopback() -> None:
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("localhost") == "localhost"
    assert validate_bind_host("::1") == "::1"
    assert validate_bind_host("127.0.0.2") == "127.0.0.2"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "*", "8.8.8.8", "192.168.1.10"])
def test_validate_bind_host_rejects_non_loopback(host: str) -> None:
    with pytest.raises(ValueError, match="loopback|0.0.0.0"):
        validate_bind_host(host)


def test_ready_url_prints_canonical_loopback() -> None:
    assert ready_url("127.0.0.1", 1088) == "http://127.0.0.1:1088"


def test_cli_web_help_is_registered() -> None:
    result = CliRunner().invoke(app, ["web", "--help"])
    assert result.exit_code == 0
    assert "1088" in result.stdout
    assert "start" in result.stdout
    assert "stop" in result.stdout


def test_cli_web_rejects_wildcard_bind() -> None:
    result = CliRunner().invoke(app, ["web", "--host", "0.0.0.0"])
    assert result.exit_code != 0
    shown = f"{result.stdout}\n{result.stderr}"
    assert "0.0.0.0" in shown or "loopback" in shown.lower()
