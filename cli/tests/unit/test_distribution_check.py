from __future__ import annotations

import importlib.util
import tarfile
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_dist.py"
SPEC = importlib.util.spec_from_file_location("check_dist", SCRIPT)
assert SPEC and SPEC.loader
check_dist = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_dist)


def test_distribution_validator_requires_licenses_and_bundled_skills(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "omniscientist-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "omniscientist-1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: omniscientist\nVersion: 1.0\n",
        )
        archive.writestr("omniscientist-1.0.dist-info/licenses/LICENSE", "license")
        archive.writestr("omniscientist-1.0.dist-info/licenses/NOTICE", "notice")
        archive.writestr("omni/data/skills/scientific-figure/SKILL.md", "skill")
        archive.writestr("omni/data/skills/scientific-figure/LICENSE.txt", "license")
        archive.writestr("omni/data/skills/scientific-figure/NOTICE.md", "notice")
    root = tmp_path / "omniscientist-1.0"
    (root / "skills/scientific-figure").mkdir(parents=True)
    for name in ("LICENSE", "NOTICE"):
        (root / name).write_text(name, encoding="utf-8")
    (root / "PKG-INFO").write_text(
        "Metadata-Version: 2.4\nName: omniscientist\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (root / "skills/scientific-figure/SKILL.md").write_text("skill", encoding="utf-8")
    (root / "skills/scientific-figure/LICENSE.txt").write_text("license", encoding="utf-8")
    (root / "skills/scientific-figure/NOTICE.md").write_text("notice", encoding="utf-8")
    sdist = dist / "omniscientist-1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(root, arcname=root.name)

    assert check_dist.validate(dist) == []

    (root / "PKG-INFO").write_text(
        "Metadata-Version: 2.5\nName: omniscientist\nVersion: 1.0\n",
        encoding="utf-8",
    )
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(root, arcname=root.name)
    errors = check_dist.validate(dist)
    assert any("core metadata 2.5" in error and "expected 2.4" in error for error in errors)

    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("omni/data/skills/pdf/SKILL.md", "bad")
    errors = check_dist.validate(dist)
    assert any("missing LICENSE" in error for error in errors)
    assert any("bundled skill" in error and "missing LICENSE.txt" in error for error in errors)
    assert any("non-redistributable pdf skill" in error for error in errors)
