"""Product-owned inventory contracts for bundled skills."""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.skills_runtime.discovery import (
    SkillIndexError,
    active_skill_names,
    indexed_skill_dirs,
    iter_skill_paths,
)
from omni.skills_runtime.registry import SkillRegistry

SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"
ACTIVE_SKILLS = (
    "arxiv-fetch",
    "openalex-search",
    "paper-review",
    "review-response",
    "scientist-kg-distiller",
    "scientific-figure",
    "scientific-poster",
    "livefigure",
    "research-ideation",
    "research-pptx",
    "soulagent",
)


def _write_skill(root: Path, name: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\nInstructions.\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_index(root: Path, *names: str) -> None:
    quoted = ", ".join(f'"{name}"' for name in names)
    (root / "index.toml").write_text(
        f"schema_version = 1\nactive = [{quoted}]\n",
        encoding="utf-8",
    )


def test_product_skill_index_is_the_exact_eleven_item_inventory() -> None:
    assert active_skill_names(SKILLS_ROOT) == ACTIVE_SKILLS
    assert tuple(path.name for path in indexed_skill_dirs(SKILLS_ROOT)) == ACTIVE_SKILLS


def test_indexed_builtin_discovery_ignores_unlisted_skill_directory(tmp_path: Path) -> None:
    root = tmp_path / "builtin"
    root.mkdir()
    active = _write_skill(root, "active-skill")
    _write_skill(root, "stale-skill")
    _write_index(root, "active-skill")

    assert indexed_skill_dirs(root) == [active]


def test_indexed_builtin_discovery_fails_closed_when_an_active_skill_is_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "builtin"
    root.mkdir()
    _write_index(root, "accidentally-deleted")

    with pytest.raises(SkillIndexError, match="accidentally-deleted"):
        indexed_skill_dirs(root)


def test_external_skill_roots_remain_dynamic_without_a_product_index(tmp_path: Path) -> None:
    root = tmp_path / "external"
    direct = _write_skill(root, "direct-skill")
    nested = _write_skill(root / "plugin", "nested-skill")

    assert iter_skill_paths(root) == [direct, nested]


def test_registry_uses_index_only_for_builtin_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    import omni.skills_runtime.registry as registry_module

    builtin = tmp_path / "builtin"
    builtin.mkdir()
    _write_skill(builtin, "active-skill")
    _write_skill(builtin, "stale-skill")
    _write_index(builtin, "active-skill")
    monkeypatch.setattr(registry_module, "BUILTIN_SKILLS_DIR", builtin)

    registry = SkillRegistry(settings, sources=["builtin"])
    registry.build_index()

    assert [entry.name for entry in registry.list_all()] == ["active-skill"]

    external = settings.paths.codex_user_skills
    _write_skill(external, "external-one")
    _write_skill(external / "plugin", "external-two")
    registry = SkillRegistry(settings, sources=["user_codex"])
    registry.build_index()

    assert [entry.name for entry in registry.list_all()] == ["external-one", "external-two"]


def test_builtin_export_uses_the_same_product_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    import omni.skills_runtime.install as install_module

    builtin = tmp_path / "builtin"
    builtin.mkdir()
    _write_skill(builtin, "active-skill")
    _write_skill(builtin, "stale-skill")
    _write_index(builtin, "active-skill")
    monkeypatch.setattr(install_module, "BUILTIN_SKILLS_DIR", builtin)

    results = install_module.export_builtin_skills(settings.paths, ["claude"])

    assert [(result.name, result.status) for result in results] == [
        ("active-skill", "installed")
    ]
    assert (settings.paths.claude_user_skills / "active-skill" / "SKILL.md").is_file()
    assert not (settings.paths.claude_user_skills / "stale-skill").exists()
