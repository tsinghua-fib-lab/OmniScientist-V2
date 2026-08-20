"""Folder-exact Persona Root: state, KG, overlay, and host binding."""

from __future__ import annotations

from pathlib import Path

from omni.config.paths import get_paths
from omni.personas.catalog import resolve_persona_paths
from omni.personas.roots import (
    bind_soulagent_project_root,
    persona_kg_root,
    persona_overlay_root,
    persona_state_root,
)


def _vcs_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


def test_path_workspace_persona_root_is_the_opened_folder(tmp_path: Path) -> None:
    repo = _vcs_repo(tmp_path / "repo")
    (repo / "scientist-kg").mkdir()
    subdir = repo / "subdir"
    subdir.mkdir()
    paths = get_paths(cwd=subdir)
    resolved = resolve_persona_paths(paths)

    assert paths.workspace_root == repo.resolve()
    assert persona_state_root(paths) == subdir.resolve()
    assert resolved.project_root == subdir.resolve()
    assert resolved.project_root != paths.workspace_root
    assert persona_kg_root(paths) == Path(paths.scientist_kg_dir).resolve()
    assert persona_overlay_root(paths, channel="cli") == subdir.resolve()
    assert persona_overlay_root(paths, channel="wechat") == subdir.resolve()


def test_named_project_persona_root_is_the_store_folder() -> None:
    paths = get_paths(project="lab")
    resolved = resolve_persona_paths(paths)
    assert paths.workspace_root is None
    assert resolved.project_root == paths.project_dir.resolve()
    assert persona_kg_root(paths) == Path(paths.scientist_kg_dir).resolve()


def test_wechat_named_default_uses_the_same_store_root() -> None:
    paths = get_paths(project="default")
    store = paths.project_dir.resolve()
    assert paths.workspace_root is None
    assert persona_state_root(paths) == store
    assert persona_overlay_root(paths, channel="wechat") == store
    bound = bind_soulagent_project_root({}, paths, channel="wechat")
    assert bound["project_root"] == str(store)


def test_folder_scientist_kg_is_used_without_moving_the_state_root(tmp_path: Path) -> None:
    folder = tmp_path / "lab"
    folder.mkdir()
    kg = folder / "scientist-kg"
    kg.mkdir()
    paths = get_paths(cwd=folder)
    assert persona_state_root(paths) == folder.resolve()
    assert persona_kg_root(paths) == kg.resolve()


def test_bind_soulagent_project_root_pins_every_channel(tmp_path: Path) -> None:
    folder = tmp_path / "lab"
    folder.mkdir()
    paths = get_paths(cwd=folder)
    bound = bind_soulagent_project_root({}, paths, channel="cli")
    assert bound["project_root"] == str(folder.resolve())
    wechat = bind_soulagent_project_root({}, paths, channel="wechat")
    assert wechat["project_root"] == str(folder.resolve())
    explicit = bind_soulagent_project_root(
        {"project_root": "/tmp/chosen"}, paths, channel="cli"
    )
    assert explicit["project_root"] == "/tmp/chosen"
