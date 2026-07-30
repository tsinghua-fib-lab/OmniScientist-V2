"""Distribution contracts for the scientist distiller and SoulAgent skills."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"


def _frontmatter(skill_name: str) -> dict:
    text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])


def test_academic_persona_skills_are_top_level_prompt_only_packages() -> None:
    for skill_name in ("scientist-kg-distiller", "soulagent"):
        skill_dir = SKILLS_ROOT / skill_name
        assert skill_dir.is_dir()
        assert (skill_dir / "SKILL.md").is_file()
        assert (skill_dir / "README.md").is_file()
        assert (skill_dir / "LICENSE.txt").is_file()
        assert (skill_dir / "NOTICE.md").is_file()

        frontmatter = _frontmatter(skill_name)
        assert frontmatter["name"] == skill_name
        assert frontmatter["metadata"]["helixforge"]["kind"] == "prompt_only"


def test_academic_persona_readmes_cover_install_test_and_host_adaptation() -> None:
    required_headings = (
        "## Installation",
        "## Testing",
        "## Host adaptation",
    )
    for skill_name in ("scientist-kg-distiller", "soulagent"):
        readme = (SKILLS_ROOT / skill_name / "README.md").read_text(encoding="utf-8")
        for heading in required_headings:
            assert heading in readme
        for host in ("OmniScientist", "Codex", "Claude Code", "OpenClaw"):
            assert host in readme


def test_soulagent_ships_two_read_only_example_kgs_and_codex_guide() -> None:
    root = SKILLS_ROOT / "soulagent"
    examples = root / "examples" / "scientist-kg"

    assert {path.name for path in examples.iterdir() if path.is_dir()} == {
        "fengli-xu",
        "kaiming-he",
    }
    for scientist_id in ("fengli-xu", "kaiming-he"):
        scientist = examples / scientist_id
        assert (scientist / "manifest.json").is_file()
        assert (scientist / "identity.json").is_file()
        assert (scientist / "l2-patterns.json").is_file()
        assert (scientist / "l3-stances.json").is_file()
        assert (scientist / "edges.json").is_file()

    assert (root / "references" / "codex测试指导文档.md").is_file()


def test_soulagent_example_kgs_pass_runtime_validation() -> None:
    loader_path = SKILLS_ROOT / "soulagent" / "kg_loader.py"
    spec = importlib.util.spec_from_file_location("soulagent_kg_loader", loader_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    examples = SKILLS_ROOT / "soulagent" / "examples" / "scientist-kg"
    for scientist_id in ("fengli-xu", "kaiming-he"):
        loaded = module.load_kg(examples / scientist_id)
        assert loaded["scientist_id"] == scientist_id


def test_generated_release_bundle_is_not_part_of_the_product_layout() -> None:
    repo_root = SKILLS_ROOT.parent
    assert not (repo_root / "academic-persona-complete-release").exists()
