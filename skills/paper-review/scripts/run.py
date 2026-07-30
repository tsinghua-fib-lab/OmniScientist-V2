#!/usr/bin/env python3
"""Portable paper-review preparation runner without the Omni runtime.

The copy-only host remains responsible for the final model synthesis described
in SKILL.md.  This runner validates and extracts the manuscript into a bounded,
structured handoff; the Omni engine adds managed LLM/VLM services, concurrent
evidence collection, form validation, persistence, and provenance.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

SKILL = "paper-review"
SKILL_DIR = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CORE = _load(SKILL_DIR / "core.py", "portable_paper_review_core")
EXTRACTOR = _load(
    SKILL_DIR / "scripts" / "extract_pdf_text.py",
    "portable_paper_review_extractor",
)


def run(payload: dict[str, Any]) -> dict[str, Any]:
    value = str(payload.get("input") or "").strip()
    if not value:
        return {"status": "error", "skill": SKILL, "error": "input is required"}
    path = Path(value).expanduser()
    if not path.is_file():
        return {
            "status": "error",
            "skill": SKILL,
            "error": f"input file does not exist: {path}",
        }
    structure = EXTRACTOR.extract_paper_structure(str(path.resolve()))
    venue = CORE.resolve_venue(
        str(payload.get("venue") or ""),
        SKILL_DIR / "references" / "venues",
    )
    output_dir = Path(payload.get("output_dir") or "paper-review-portable-out")
    output_dir.mkdir(parents=True, exist_ok=True)
    handoff = output_dir / "paper-review-handoff.json"
    handoff.write_text(
        json.dumps(
            {
                "paper": structure,
                "venue": {
                    "requested": venue.requested,
                    "profile": venue.profile_filename,
                    "fields": list(venue.fields),
                },
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return {
        "status": "partial",
        "skill": SKILL,
        "summary": (
            "Paper text and venue contract were prepared. Follow SKILL.md in the "
            "host agent for visual/literature evidence and final review synthesis."
        ),
        "paper": {
            "source": structure.get("source", ""),
            "title": structure.get("title", ""),
            "abstract": structure.get("abstract", ""),
        },
        "venue": venue.requested,
        "artifacts": [
            {
                "title": "Portable paper-review handoff",
                "format": "json",
                "path": str(handoff),
            }
        ],
    }


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.json_file:
        raw = Path(args.json_file).expanduser().read_text(encoding="utf-8-sig")
    elif args.json:
        raw = args.json
    elif not sys.stdin.isatty():
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
    else:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("input JSON must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the portable paper-review skill.")
    parser.add_argument("--json", help="Input JSON object.")
    parser.add_argument(
        "--json-file",
        help="UTF-8 JSON file. Prefer this on Windows/PowerShell; --json quoting is unreliable there.",
    )
    parser.add_argument("--self-test", action="store_true", help="Run an offline smoke test.")
    args = parser.parse_args(argv)
    if args.self_test:
        print(
            json.dumps(
                {"status": "ok", "skill": SKILL, "portable_runner": True},
                ensure_ascii=False,
            )
        )
        return 0
    try:
        result = run(_payload(args))
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "skill": SKILL, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
