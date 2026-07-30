"""Error-path coverage for AutoSOTA release fetch / install helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from omni.autosota import integration
from omni.autosota.integration import AutosotaError


def test_fetch_json_and_latest_page_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_request, timeout=30):  # noqa: ANN001, ARG001
        raise URLError("offline")

    monkeypatch.setattr(integration, "urlopen", boom)
    with pytest.raises(AutosotaError, match="Could not query"):
        integration._fetch_json("https://example.test/release")

    with pytest.raises(AutosotaError, match="Could not resolve the latest"):
        integration._latest_release_tag_from_page()


def test_fetch_json_rejects_non_object(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def read(self) -> bytes:
            return b"[1,2,3]"

    monkeypatch.setattr(integration, "urlopen", lambda *_a, **_k: _Resp())
    with pytest.raises(AutosotaError, match="malformed"):
        integration._fetch_json("https://example.test/release")


def test_latest_page_requires_tag_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def geturl(self) -> str:
            return "https://github.com/example/repo"

    monkeypatch.setattr(integration, "urlopen", lambda *_a, **_k: _Resp())
    with pytest.raises(AutosotaError, match="did not resolve"):
        integration._latest_release_tag_from_page()


def test_active_install_rejects_incomplete_and_escaped_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = SimpleNamespace(cache_dir=tmp_path / "cache", secrets_file=tmp_path / "secrets.toml")
    meta = integration.metadata_path(paths)
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"version": "v0"}), encoding="utf-8")
    with pytest.raises(AutosotaError, match="incomplete"):
        integration.active_install(paths)

    root = integration.autosota_root(paths)
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside" / "autosota"
    outside.parent.mkdir(parents=True)
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    meta.write_text(
        json.dumps(
            {
                "version": "v0",
                "runtime_dir": str(tmp_path / "outside"),
                "executable": str(outside),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AutosotaError, match="outside"):
        integration.active_install(paths)

    runtime_dir = root / "versions" / "v0"
    executable = runtime_dir / "node_modules" / ".bin" / "autosota"
    executable.parent.mkdir(parents=True)
    # Missing executable file → None (not installed).
    meta.write_text(
        json.dumps(
            {
                "version": "v0",
                "runtime_dir": str(runtime_dir),
                "executable": str(executable),
            }
        ),
        encoding="utf-8",
    )
    assert integration.active_install(paths) is None

    with pytest.raises(AutosotaError, match="not installed"):
        integration.require_active_install(paths)


def test_official_release_asset_falls_back_when_api_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        integration,
        "_fetch_json",
        lambda _url: (_ for _ in ()).throw(AutosotaError("rate limited")),
    )
    monkeypatch.setattr(integration, "_latest_release_tag_from_page", lambda: "v0.3.0")
    asset = integration.official_release_asset("latest")
    assert asset.version == "v0.3.0"
    assert asset.url.endswith("autosota-0.3.0.tgz")
