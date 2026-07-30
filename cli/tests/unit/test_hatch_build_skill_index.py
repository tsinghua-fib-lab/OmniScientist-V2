"""Wheel/sdist build contracts for the product-owned skill index."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_build_module(monkeypatch):
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
    return module


def _load_build_hook(monkeypatch):
    return _load_build_module(monkeypatch).CustomBuildHook


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


@pytest.mark.parametrize(
    ("target_name", "destination"),
    [
        ("wheel", "omni/data/skills/paper-review"),
        ("sdist", "skills/paper-review"),
    ],
)
def test_build_hook_excludes_paper_review_data_from_pip_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    destination: str,
) -> None:
    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    paper_review = _write_skill(skills, "paper-review")
    engine = paper_review / "engine.py"
    engine.write_text("VALUE = 1\n", encoding="utf-8")
    manifest = (
        paper_review
        / "resources"
        / "indexes"
        / "iclr2026-reviews"
        / "index.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}\n", encoding="utf-8")
    generation = manifest.parent / "generations" / "gen-test"
    generation.mkdir(parents=True)
    review_text = generation / "reviews.pack"
    review_text.write_bytes(b"private-review-text-placeholder")
    paper_map = generation / "papers.jsonl"
    paper_map.write_text('{"paper_id":"test"}\n', encoding="utf-8")
    vectors = generation / "vectors.faiss"
    vectors.write_bytes(b"derived-vector-placeholder")
    bundle_manifest = paper_review / "resources" / "indexes" / "manifest.json"
    bundle_manifest.write_text('{"contains":"data metadata"}\n', encoding="utf-8")
    (skills / "index.toml").write_text(
        'schema_version = 1\nactive = ["paper-review"]\n',
        encoding="utf-8",
    )

    hook_type = _load_build_hook(monkeypatch)
    hook = hook_type()
    hook.root = str(cli_root)
    hook.target_name = target_name
    build_data: dict = {}

    hook.initialize("test", build_data)

    included = build_data["force_include"]
    assert included[str(engine.resolve())] == f"{destination}/engine.py"
    assert included[str(manifest.resolve())] == (
        f"{destination}/resources/indexes/iclr2026-reviews/index.json"
    )
    for data_file in (review_text, paper_map, vectors, bundle_manifest):
        assert str(data_file.resolve()) not in included
    assert str(paper_review.resolve()) not in included


def test_build_hook_rejects_damaged_bundled_scientist_persona(
    tmp_path, monkeypatch
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    soulagent = skills / "soulagent"
    (soulagent / "assets").mkdir(parents=True)
    (soulagent / "SKILL.md").write_text("---\nname: soulagent\n---\n", encoding="utf-8")
    source = repo_root / "skills" / "soulagent" / "assets" / "builtin-scientist-kg"
    shutil.copytree(source, soulagent / "assets" / "builtin-scientist-kg")
    (skills / "index.toml").write_text(
        'schema_version = 1\nactive = ["soulagent"]\n',
        encoding="utf-8",
    )
    identity = soulagent / "assets" / "builtin-scientist-kg" / "kaiming-he" / "identity.json"
    identity.write_text("{}\n", encoding="utf-8")

    module = _load_build_module(monkeypatch)
    # Point the directly-loaded build hook at this checkout's validator while
    # keeping its source Skill collection in the temporary test repository.
    module.__file__ = str(repo_root / "cli" / "hatch_build.py")
    hook = module.CustomBuildHook()
    hook.root = str(cli_root)
    hook.target_name = "wheel"

    with pytest.raises(RuntimeError, match="invalid bundled scientist personas"):
        hook.initialize("test", {})
