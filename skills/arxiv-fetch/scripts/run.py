#!/usr/bin/env python3
"""Portable runner for arxiv-fetch.

This script is intentionally self-contained so Claude Code, Codex, and
OpenClaw can run the skill without installing Omni.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import fetch  # noqa: E402

SKILL = "arxiv-fetch"


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
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
    parser = argparse.ArgumentParser(description="Run the portable arxiv-fetch skill.")
    parser.add_argument("--json", help="Input JSON object.")
    parser.add_argument(
        "--json-file",
        help="UTF-8 JSON file. Prefer this on Windows/PowerShell; --json quoting is unreliable there.",
    )
    parser.add_argument("--self-test", action="store_true", help="Run an offline smoke test.")
    args = parser.parse_args(argv)

    if args.self_test:
        print(json.dumps({"status": "ok", "skill": SKILL, "portable_runner": True}, ensure_ascii=False))
        return 0

    try:
        payload = _load_payload(args)
        result = fetch(str(payload.get("identifier") or payload.get("arxiv_id") or payload.get("id") or ""))
    except Exception as exc:  # noqa: BLE001 - keep stdout machine-readable
        result = {"status": "error", "skill": SKILL, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
