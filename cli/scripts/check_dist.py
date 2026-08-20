#!/usr/bin/env python3
"""Validate release archives before publishing."""

from __future__ import annotations

import json
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path


def _members(path: Path) -> set[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    with tarfile.open(path, "r:gz") as archive:
        return set(archive.getnames())


def _has_suffix(names: set[str], suffix: str) -> bool:
    return any(name == suffix or name.endswith("/" + suffix) for name in names)


def _read_member(path: Path, member: str) -> bytes:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.read(member)
    with tarfile.open(path, "r:gz") as archive:
        extracted = archive.extractfile(member)
        if extracted is None:
            raise ValueError(f"could not read {member}")
        return extracted.read()


def _bundled_skill_roots(names: set[str], *, wheel: bool) -> set[str]:
    marker = "/omni/data/skills/" if wheel else "/skills/"
    return {
        name.rsplit("/", 1)[0]
        for name in names
        if marker in "/" + name and name.endswith("/SKILL.md")
    }


def validate(dist: Path) -> list[str]:
    errors: list[str] = []
    archives = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
    if len([path for path in archives if path.suffix == ".whl"]) != 1:
        errors.append("expected exactly one wheel")
    if len([path for path in archives if path.name.endswith(".tar.gz")]) != 1:
        errors.append("expected exactly one sdist")
    for archive in archives:
        names = _members(archive)
        for required in ("LICENSE", "NOTICE"):
            if not _has_suffix(names, required):
                errors.append(f"{archive.name}: missing {required}")
        wheel = archive.suffix == ".whl"
        metadata_suffix = ".dist-info/METADATA" if wheel else "PKG-INFO"
        metadata_members = sorted(
            name
            for name in names
            if name.endswith(metadata_suffix)
            and (wheel or name.count("/") == 1)
        )
        if len(metadata_members) != 1:
            errors.append(
                f"{archive.name}: expected exactly one top-level {metadata_suffix}"
            )
        else:
            message = BytesParser(policy=compat32).parsebytes(
                _read_member(archive, metadata_members[0])
            )
            metadata_version = str(message.get("Metadata-Version") or "missing")
            if metadata_version != "2.4":
                errors.append(
                    f"{archive.name}: core metadata {metadata_version}; expected 2.4 "
                    "for PyPI publisher compatibility"
                )
        skill = (
            "omni/data/skills/scientific-figure/SKILL.md"
            if wheel
            else "skills/scientific-figure/SKILL.md"
        )
        if not _has_suffix(names, skill):
            errors.append(f"{archive.name}: missing bundled skill {skill}")
        for skill_root in sorted(_bundled_skill_roots(names, wheel=wheel)):
            for legal_file in ("LICENSE.txt", "NOTICE.md"):
                member = f"{skill_root}/{legal_file}"
                if member not in names:
                    errors.append(
                        f"{archive.name}: bundled skill {skill_root} missing {legal_file}"
                    )
        if any("/skills/pdf/" in "/" + name for name in names):
            errors.append(f"{archive.name}: contains removed non-redistributable pdf skill")
        web_ui = "omni/data/web/index.html" if wheel else "web/dist/index.html"
        if not _has_suffix(names, web_ui):
            errors.append(f"{archive.name}: missing bundled web UI {web_ui}")
        stamp_name = "omni/data/web/version.json" if wheel else "web/dist/version.json"
        stamp_member = next(
            (name for name in names if name == stamp_name or name.endswith("/" + stamp_name)),
            None,
        )
        if stamp_member is None:
            errors.append(f"{archive.name}: missing bundled web UI version stamp {stamp_name}")
        elif metadata_members:
            metadata_pkg_version = str(
                BytesParser(policy=compat32)
                .parsebytes(_read_member(archive, metadata_members[0]))
                .get("Version")
                or ""
            )
            try:
                stamped = json.loads(_read_member(archive, stamp_member))
                ui_version = str((stamped or {}).get("version") or "")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                ui_version = ""
            if not ui_version:
                errors.append(f"{archive.name}: web UI version stamp is empty")
            elif metadata_pkg_version and ui_version != metadata_pkg_version:
                errors.append(
                    f"{archive.name}: web UI {ui_version} does not match package {metadata_pkg_version}"
                )
    return errors


def main() -> int:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    errors = validate(dist)
    if errors:
        print("distribution validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("distribution validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
