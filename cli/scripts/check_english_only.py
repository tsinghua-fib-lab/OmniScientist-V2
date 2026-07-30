#!/usr/bin/env python3
"""Fail if production control-plane / public docs contain Han characters.

User language belongs to model input/output, not runtime source assets.
This is the same gate as
``test_production_control_plane_and_public_docs_are_english_only``.

Usage (from repo root or ``cli/``):

    python cli/scripts/check_english_only.py
    ./cli/scripts/release.sh --dry-run   # runs this during local preflight
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SCAN_SUFFIXES = {".py", ".md", ".toml"}

# Vendored academic-persona packages: Chinese is design provenance and
# model-facing prompt scaffolding, not OmniScientist's own control plane.
_VENDORED_PERSONA_SKILLS = (
    "skills/soulagent",
    "skills/scientist-kg-distiller",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_exempt(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root)
    except ValueError:
        return False
    parts = relative.as_posix()
    return any(parts == prefix or parts.startswith(prefix + "/") for prefix in _VENDORED_PERSONA_SKILLS)


def iter_scanned_files(root: Path) -> list[Path]:
    cli = root / "cli"
    roots = [cli / "src" / "omni", cli / "docs", root / "skills"]
    files = [root / "README.md", root / "NOTICE"]
    for scan_root in roots:
        if not scan_root.is_dir():
            continue
        files.extend(
            path
            for path in scan_root.rglob("*")
            if path.is_file() and path.suffix.lower() in _SCAN_SUFFIXES
        )
    return [path for path in files if path.is_file() and not _is_exempt(path, root)]


def find_violations(root: Path | None = None) -> list[tuple[str, int, str]]:
    """Return ``(repo-relative path, line, snippet)`` for each Han hit."""
    base = root or repo_root()
    hits: list[tuple[str, int, str]] = []
    for path in iter_scanned_files(base):
        text = path.read_text(encoding="utf-8")
        if not HAN.search(text):
            continue
        rel = str(path.resolve().relative_to(base))
        for lineno, line in enumerate(text.splitlines(), 1):
            if HAN.search(line):
                hits.append((rel, lineno, line.strip()[:120]))
        if not any(rel == item[0] for item in hits):
            hits.append((rel, 0, "(Han present but not on a single line)"))
    return hits


def violation_files(root: Path | None = None) -> list[str]:
    seen: list[str] = []
    for path, _lineno, _snippet in find_violations(root):
        if path not in seen:
            seen.append(path)
    return seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: inferred from this script)",
    )
    args = parser.parse_args(argv)
    hits = find_violations(args.root)
    if not hits:
        print("OK: control-plane source and public docs are English-only.")
        return 0
    print("Han characters in production control-plane / public docs:", file=sys.stderr)
    for path, lineno, snippet in hits:
        where = f"{path}:{lineno}" if lineno else path
        print(f"  {where}: {snippet}", file=sys.stderr)
    print(
        f"{len(violation_files(args.root))} file(s). "
        "Keep user language in model I/O; encode runtime phrases as unicode escapes.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
