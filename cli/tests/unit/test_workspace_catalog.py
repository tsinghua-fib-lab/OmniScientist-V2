"""Workspace catalog: registry ∪ channel anchor ∪ named project DBs."""

from __future__ import annotations

import json

from omni.cli.commands.init_cmd import first_run_setup_message
from omni.config.paths import get_paths
from omni.config.workspaces import (
    iter_catalog_workspaces,
    list_workspaces,
    prior_user_data_summary,
    register_workspace,
    registry_path,
)


def test_catalog_includes_unregistered_channel_anchor(omni_home, tmp_path):
    """IM anchor ``default`` is catalogued even when workspaces.json omits it."""

    # Seed the anchor store without registering it.
    anchor = get_paths(project="default")
    anchor.ensure_dirs()
    (anchor.project_dir / "sessions.sqlite3").write_bytes(b"")  # existence probe only

    # Registry stays empty — the bug that made /task all miss WeChat work.
    assert list_workspaces(omni_home) == []
    assert not registry_path(omni_home).exists()

    names = {r["name"] for r in iter_catalog_workspaces(omni_home)}
    assert "default" in names
    rec = next(r for r in iter_catalog_workspaces(omni_home) if r["name"] == "default")
    assert rec["kind"] == "named"
    assert rec["db"].endswith("sessions.sqlite3")


def test_catalog_respects_service_channel_anchor_override(omni_home, tmp_path):

    for name in ("default", "im-home"):
        p = get_paths(project=name)
        p.ensure_dirs()
        (p.project_dir / "sessions.sqlite3").write_bytes(b"")

    service = omni_home / "service"
    service.mkdir()
    (service / "settings.json").write_text(
        json.dumps({"channel_anchor": "im-home", "enabled": True}),
        encoding="utf-8",
    )

    names = {r["name"] for r in iter_catalog_workspaces(omni_home)}
    assert "im-home" in names
    assert "default" in names  # still discovered via projects/* scan


def test_catalog_merges_registry_path_workspaces(omni_home, tmp_path):

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    paths = get_paths(cwd=repo)
    paths.ensure_dirs()
    (paths.project_db).write_bytes(b"")
    register_workspace(paths)

    # Anchor with no DB yet must not appear; path workspace must.
    names = {r["name"] for r in iter_catalog_workspaces(omni_home)}
    assert paths.project_name in names
    assert "default" not in names


def test_catalog_includes_unregistered_path_workspace(omni_home, tmp_path):
    """A path-keyed store on disk stays visible after workspaces.json is gone."""

    repo = tmp_path / "paper"
    (repo / ".git").mkdir(parents=True)
    paths = get_paths(cwd=repo)
    paths.ensure_dirs()
    paths.project_db.write_bytes(b"")
    register_workspace(paths)
    registry_path(omni_home).unlink()
    assert list_workspaces(omni_home) == []

    names = {r["name"] for r in iter_catalog_workspaces(omni_home)}
    assert paths.project_name in names
    rec = next(r for r in iter_catalog_workspaces(omni_home) if r["name"] == paths.project_name)
    assert rec["kind"] == "path"
    assert rec["db"] == str(paths.project_db)


def test_register_rebuilds_registry_from_disk(omni_home, tmp_path):
    """Writing a new registry entry must not hide sibling stores on disk."""

    first = tmp_path / "alpha"
    (first / ".git").mkdir(parents=True)
    a = get_paths(cwd=first)
    a.ensure_dirs()
    a.project_db.write_bytes(b"")
    register_workspace(a)
    registry_path(omni_home).write_text("{not-json", encoding="utf-8")

    second = tmp_path / "beta"
    (second / ".git").mkdir(parents=True)
    b = get_paths(cwd=second)
    b.ensure_dirs()
    b.project_db.write_bytes(b"")
    register_workspace(b)

    names = {r["name"] for r in list_workspaces(omni_home)}
    assert a.project_name in names
    assert b.project_name in names


def test_prior_user_data_summary_blank_home(omni_home):
    assert prior_user_data_summary(omni_home) is None
    assert "No user configuration" in first_run_setup_message(omni_home)


def test_prior_user_data_summary_finds_path_workspace(omni_home):
    store = omni_home / "workspaces" / "paper-abcd1234"
    store.mkdir(parents=True)
    (store / "sessions.sqlite3").write_bytes(b"")
    (omni_home / "secrets.toml").write_text("x = 1\n", encoding="utf-8")

    summary = prior_user_data_summary(omni_home)
    assert summary is not None
    assert "paper-abcd1234" in summary
    message = first_run_setup_message(omni_home)
    assert "without deleting" in message
    assert "paper-abcd1234" in message
