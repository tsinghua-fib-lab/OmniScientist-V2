"""Built-in engines may consume only Omni's documented public research API."""

from __future__ import annotations

import ast
from pathlib import Path

from omni.skills_runtime.discovery import active_skill_names

ROOT = Path(__file__).resolve().parents[3]
ACTIVE_SKILLS = active_skill_names(ROOT / "skills")


def _is_public_research(module: str) -> bool:
    """Skill engines may import the public ``omni.research`` runtime package."""
    return module == "omni.research" or module.startswith("omni.research.")


def test_skill_engines_do_not_import_cli_internal_modules():
    violations: list[str] = []
    for name in ACTIVE_SKILLS:
        path = ROOT / "skills" / name / "engine.py"
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and str(node.module or "").startswith("omni."):
                if not _is_public_research(str(node.module)):
                    violations.append(f"{name}:{node.lineno}:{node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("omni.") and not _is_public_research(alias.name):
                        violations.append(f"{name}:{node.lineno}:{alias.name}")

    assert violations == []
