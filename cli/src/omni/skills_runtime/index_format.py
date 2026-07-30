"""Stdlib-only parser for the product-owned active Skill index."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

SKILL_INDEX_FILENAME = "index.toml"
_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class SkillIndexError(RuntimeError):
    """The built-in Skill inventory is absent or inconsistent."""


def active_skill_names(root: Path) -> tuple[str, ...]:
    """Load and validate the ordered built-in inventory."""
    index = root / SKILL_INDEX_FILENAME
    try:
        data = tomllib.loads(index.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SkillIndexError(f"cannot load built-in skill index {index}: {exc}") from exc

    schema_version = data.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise SkillIndexError(
            f"unsupported built-in skill index schema_version: {schema_version!r}"
        )

    raw_names = data.get("active")
    if not isinstance(raw_names, list) or not raw_names:
        raise SkillIndexError("built-in skill index 'active' must be a non-empty list")

    names: list[str] = []
    for raw_name in raw_names:
        if not isinstance(raw_name, str) or not _SKILL_NAME_RE.fullmatch(raw_name):
            raise SkillIndexError(f"invalid built-in skill name in index: {raw_name!r}")
        if raw_name in names:
            raise SkillIndexError(f"duplicate built-in skill name in index: {raw_name}")
        names.append(raw_name)
    return tuple(names)


def indexed_skill_dirs(root: Path) -> list[Path]:
    """Resolve every indexed package, failing if one is incomplete."""
    out: list[Path] = []
    for name in active_skill_names(root):
        skill_dir = root / name
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            raise SkillIndexError(
                f"active built-in skill '{name}' is missing its {name}/SKILL.md package"
            )
        out.append(skill_dir)
    return out


__all__ = [
    "SKILL_INDEX_FILENAME",
    "SkillIndexError",
    "active_skill_names",
    "indexed_skill_dirs",
]
