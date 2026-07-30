"""Wheel/sdist build contracts for the product-owned skill index."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_build_hook(monkeypatch):
    interface = ModuleType("hatchling.builders.hooks.plugin.interface")

    class BuildHookInterface:
        pass

    interface.BuildHookInterface = BuildHookInterface
    for package in (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
    ):
        monkeypatch.setitem(sys.modules, package, ModuleType(package))
    monkeypatch.setitem(sys.modules, interface.__name__, interface)

    path = Path(__file__).resolve().parents[2] / "hatch_build.py"
    spec = importlib.util.spec_from_file_location("test_hatch_build", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CustomBuildHook


def _write_skill(root: Path, name: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return skill


def test_build_hook_packages_only_skills_listed_in_active_index(tmp_path, monkeypatch) -> None:
    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    active = _write_skill(skills, "active-skill")
    stale = _write_skill(skills, "stale-skill")
    index = skills / "index.toml"
    index.write_text(
        'schema_version = 1\nactive = ["active-skill"]\n',
        encoding="utf-8",
    )

    hook_type = _load_build_hook(monkeypatch)
    hook = hook_type()
    hook.root = str(cli_root)
    hook.target_name = "wheel"
    build_data: dict = {}

    hook.initialize("test", build_data)

    included = build_data["force_include"]
    assert included[str(index.resolve())] == "omni/data/skills/index.toml"
    assert included[str(active.resolve())] == "omni/data/skills/active-skill"
    assert str(stale.resolve()) not in included


def test_build_hook_refuses_host_node_modules_in_portable_skills(tmp_path, monkeypatch) -> None:
    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    active = _write_skill(skills, "active-skill")
    (active / "scripts" / "node_modules" / "sharp").mkdir(parents=True)
    (skills / "index.toml").write_text(
        'schema_version = 1\nactive = ["active-skill"]\n',
        encoding="utf-8",
    )

    hook_type = _load_build_hook(monkeypatch)
    hook = hook_type()
    hook.root = str(cli_root)
    hook.target_name = "wheel"

    with pytest.raises(RuntimeError, match="node_modules"):
        hook.initialize("test", {})
