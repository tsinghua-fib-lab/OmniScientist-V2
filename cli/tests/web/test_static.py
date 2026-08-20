"""SPA resolution: env override, packaged wheel path, checkout fallback."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest
from typer.testing import CliRunner

from omni.cli.main import app
from omni.web.static import (
    WebUiMissing,
    ensure_web_ui,
    is_spa,
    spa_build_commands,
    spa_version,
    web_dist_dir,
)


def _write_spa(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<!doctype html><title>omni</title>", encoding="utf-8")
    return root


def test_is_spa_requires_index_html(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert is_spa(empty) is False
    assert is_spa(_write_spa(tmp_path / "ready")) is True


def test_web_dist_dir_env_override_exclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ready = _write_spa(tmp_path / "override")
    monkeypatch.setenv("OMNI_WEB_DIST", str(ready))
    assert web_dist_dir() == ready

    monkeypatch.setenv("OMNI_WEB_DIST", str(tmp_path / "missing"))
    assert web_dist_dir() is None


def test_web_dist_dir_prefers_packaged_over_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = _write_spa(tmp_path / "packaged")
    monkeypatch.delenv("OMNI_WEB_DIST", raising=False)
    monkeypatch.setattr("omni.web.static.packaged_web_dir", lambda: packaged)
    assert web_dist_dir() == packaged


def test_ensure_web_ui_raises_without_dist_or_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNI_WEB_DIST", str(tmp_path / "missing"))
    monkeypatch.setattr("omni.web.static.prepare_web_ui", lambda: None)
    with pytest.raises(WebUiMissing, match="not available"):
        ensure_web_ui()


def test_spa_build_commands_prefer_local_vite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web_root = tmp_path / "web"
    vite_js = web_root / "node_modules" / "vite" / "bin" / "vite.js"
    vite_js.parent.mkdir(parents=True)
    vite_js.write_text("/* vite */", encoding="utf-8")
    monkeypatch.setattr("omni.web.static.shutil.which", lambda name: "/usr/bin/node" if name == "node" else None)
    assert spa_build_commands(web_root) == [["/usr/bin/node", str(vite_js), "build"]]


def test_prepare_web_ui_stamps_package_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_root = tmp_path / "web"
    web_root.mkdir()

    def _build(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        _write_spa(web_root / "dist")
        return CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr("omni.web.static.web_dist_dir", lambda: None)
    monkeypatch.setattr("omni.web.static.checkout_web_root", lambda: web_root)
    monkeypatch.setattr("omni.web.static.spa_build_commands", lambda root: [["vite", "build"]])
    monkeypatch.setattr("omni.web.static.subprocess.run", _build)
    monkeypatch.setattr("omni.web.static.package_version", lambda: "2.0.0rc4")

    built = ensure_web_ui()

    assert built == web_root / "dist"
    assert spa_version(built) == "2.0.0rc4"


def test_cli_web_refuses_without_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing() -> Path:
        raise WebUiMissing("omni web UI is not available.")

    monkeypatch.setattr("omni.cli.commands.web_cmd.ensure_web_ui", _missing)
    result = CliRunner().invoke(app, ["web"])
    assert result.exit_code == 2
    shown = f"{result.stdout}\n{result.stderr}"
    assert "not available" in shown


@pytest.mark.asyncio
async def test_create_app_serves_env_spa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("starlette")
    import httpx

    from omni.web.app import create_app

    ready = _write_spa(tmp_path / "dist")
    (ready / "assets").mkdir()
    (ready / "assets" / "app.js").write_text("window.omni=1", encoding="utf-8")
    expected_version = "9.8.7-test"
    (ready / "version.json").write_text(
        f'{{"version": "{expected_version}"}}\n', encoding="utf-8"
    )
    monkeypatch.setattr("omni.web.app.package_version", lambda: expected_version)
    monkeypatch.setenv("OMNI_WEB_DIST", str(ready))
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "omni" in page.text
        assert "no-store" in page.headers.get("cache-control", "")
        asset = await client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "immutable" in asset.headers.get("cache-control", "")
        health = await client.get("/health")
        assert health.status_code == 200
        payload = health.json()
        assert payload["surface"] == "web"
        assert payload["version"] == payload["ui_version"] == expected_version


@pytest.mark.asyncio
async def test_create_app_returns_503_without_spa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("starlette")
    import httpx

    from omni.web.app import create_app

    monkeypatch.setenv("OMNI_WEB_DIST", str(tmp_path / "missing"))
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        page = await client.get("/")
        assert page.status_code == 503
        assert "not available" in page.text
        health = await client.get("/health")
        assert health.status_code == 200


def test_spa_version_reads_stamped_json(tmp_path: Path) -> None:
    root = _write_spa(tmp_path / "dist")
    (root / "version.json").write_text('{"version": "2.0.0rc4"}\n', encoding="utf-8")
    assert spa_version(root) == "2.0.0rc4"
    assert spa_version(tmp_path / "missing") == ""
