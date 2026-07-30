"""Workspace catalog: registry ∪ channel anchor ∪ named project DBs."""

from __future__ import annotations

import json

from omni.config.paths import get_paths
from omni.config.workspaces import (
    iter_catalog_workspaces,
    list_workspaces,
    register_workspace,
    registry_path,
)


def test_catalog_includes_unregistered_channel_anchor(tmp_path, monkeypatch):
    """IM anchor ``default`` is catalogued even when workspaces.json omits it."""
    home = tmp_path / "omni-home"
    home.mkdir()
    monkeypatch.setenv("OMNI_HOME", str(home))

    # Seed the anchor store without registering it.
    anchor = get_paths(project="default")
    anchor.ensure_dirs()
    (anchor.project_dir / "sessions.sqlite3").write_bytes(b"")  # existence probe only

    # Registry stays empty — the bug that made /task all miss WeChat work.
    assert list_workspaces(home) == []
    assert not registry_path(home).exists()

    names = {r["name"] for r in iter_catalog_workspaces(home)}
    assert "default" in names
    rec = next(r for r in iter_catalog_workspaces(home) if r["name"] == "default")
    assert rec["kind"] == "named"
    assert rec["db"].endswith("sessions.sqlite3")


def test_catalog_respects_service_channel_anchor_override(tmp_path, monkeypatch):
    home = tmp_path / "omni-home"
    home.mkdir()
    monkeypatch.setenv("OMNI_HOME", str(home))

    for name in ("default", "im-home"):
        p = get_paths(project=name)
        p.ensure_dirs()
        (p.project_dir / "sessions.sqlite3").write_bytes(b"")

    service = home / "service"
    service.mkdir()
    (service / "settings.json").write_text(
        json.dumps({"channel_anchor": "im-home", "enabled": True}),
        encoding="utf-8",
    )

    names = {r["name"] for r in iter_catalog_workspaces(home)}
    assert "im-home" in names
    assert "default" in names  # still discovered via projects/* scan


def test_catalog_merges_registry_path_workspaces(tmp_path, monkeypatch):
    home = tmp_path / "omni-home"
    home.mkdir()
    monkeypatch.setenv("OMNI_HOME", str(home))

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    paths = get_paths(cwd=repo)
    paths.ensure_dirs()
    (paths.project_db).write_bytes(b"")
    register_workspace(paths)

    # Anchor with no DB yet must not appear; path workspace must.
    names = {r["name"] for r in iter_catalog_workspaces(home)}
    assert paths.project_name in names
    assert "default" not in names
