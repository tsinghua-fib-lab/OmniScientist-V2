"""Offline contracts for product-bundled scientist persona installation."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omni.personas.bundle_format import (
    BundledPersonaValidationError,
    validate_builtin_persona_collection,
)
from omni.personas.installer import BuiltinPersonaInstallError, install_builtin_personas

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "skills" / "soulagent"
BUNDLED_ROOT = SKILL_DIR / "assets" / "builtin-scientist-kg"
SCIENTIST_IDS = (
    "alan-turing",
    "claude-shannon",
    "fengli-xu",
    "herbert-a-simon",
    "john-von-neumann",
    "kaiming-he",
    "norbert-wiener",
    "richard-feynman",
)


def _paths(home: Path) -> SimpleNamespace:
    return SimpleNamespace(home=home, scientist_kg_dir=home / "scientist-kg")


def _subset_source(root: Path, *scientist_ids: str) -> Path:
    source = root / "bundled-source"
    source.mkdir()
    catalog = json.loads((BUNDLED_ROOT / "index.json").read_text(encoding="utf-8"))
    catalog["scientists"] = [
        entry
        for entry in catalog["scientists"]
        if entry["scientist_id"] in scientist_ids
    ]
    (source / "index.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for scientist_id in scientist_ids:
        shutil.copytree(BUNDLED_ROOT / scientist_id, source / scientist_id)
    return source


def _load_soulagent_core():  # noqa: ANN202
    module_name = "test_builtin_persona_soulagent_core"
    spec = importlib.util.spec_from_file_location(module_name, SKILL_DIR / "core.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(SKILL_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_bundled_catalog_validates_all_eight_personas() -> None:
    assert validate_builtin_persona_collection(BUNDLED_ROOT) == SCIENTIST_IDS


def test_first_install_into_unicode_home_is_immediately_scannable(tmp_path: Path) -> None:
    home = tmp_path / "用户 数据 (OmniScientist)"
    result = install_builtin_personas(_paths(home), source_root=BUNDLED_ROOT)

    assert result.installed == SCIENTIST_IDS
    assert result.skipped_existing == ()
    core = _load_soulagent_core()
    inventory = core.list_scientists(home / "scientist-kg")
    assert tuple(row["scientist_id"] for row in inventory["scientists"]) == SCIENTIST_IDS
    assert inventory["invalid"] == []
    assert core.load_kg(home / "scientist-kg" / "kaiming-he")["scientist_id"] == "kaiming-he"


def test_repeated_install_is_idempotent(tmp_path: Path) -> None:
    paths = _paths(tmp_path / "omni-home")
    first = install_builtin_personas(paths, source_root=BUNDLED_ROOT)
    second = install_builtin_personas(paths, source_root=BUNDLED_ROOT)

    assert first.installed == SCIENTIST_IDS
    assert second.installed == ()
    assert second.skipped_existing == SCIENTIST_IDS
    assert not list(paths.home.glob(".builtin-*.tmp"))


def test_existing_user_directory_is_never_overwritten_even_if_invalid(tmp_path: Path) -> None:
    paths = _paths(tmp_path / "omni-home")
    existing = paths.scientist_kg_dir / "kaiming-he"
    existing.mkdir(parents=True)
    marker = existing / "user-owned.txt"
    marker.write_text("keep my local version\n", encoding="utf-8")

    result = install_builtin_personas(paths, source_root=BUNDLED_ROOT)

    assert "kaiming-he" in result.skipped_existing
    assert "kaiming-he" not in result.installed
    assert marker.read_text(encoding="utf-8") == "keep my local version\n"
    assert not (existing / "manifest.json").exists()


def test_upgrade_adds_new_builtin_personas_without_replacing_old_ones(tmp_path: Path) -> None:
    paths = _paths(tmp_path / "omni-home")
    first_source = _subset_source(tmp_path, "kaiming-he")
    first = install_builtin_personas(paths, source_root=first_source)
    local_identity = paths.scientist_kg_dir / "kaiming-he" / "identity.json"
    original = local_identity.read_bytes()

    upgraded = install_builtin_personas(paths, source_root=BUNDLED_ROOT)

    assert first.installed == ("kaiming-he",)
    assert upgraded.skipped_existing == ("kaiming-he",)
    assert set(upgraded.installed) == set(SCIENTIST_IDS) - {"kaiming-he"}
    assert local_identity.read_bytes() == original


def test_damaged_bundled_persona_fails_before_scanner_is_created(tmp_path: Path) -> None:
    source = _subset_source(tmp_path, "kaiming-he")
    identity = source / "kaiming-he" / "identity.json"
    identity.write_text("{}\n", encoding="utf-8")
    paths = _paths(tmp_path / "omni-home")

    with pytest.raises(BuiltinPersonaInstallError, match="bundled scientist personas are invalid"):
        install_builtin_personas(paths, source_root=source)

    assert not paths.scientist_kg_dir.exists()


def test_catalog_rejects_an_undeclared_persona_directory(tmp_path: Path) -> None:
    source = _subset_source(tmp_path, "kaiming-he")
    shutil.copytree(BUNDLED_ROOT / "fengli-xu", source / "not-in-catalog")

    with pytest.raises(
        BundledPersonaValidationError,
        match="directories do not exactly match",
    ):
        validate_builtin_persona_collection(source)
