#!/usr/bin/env python3
"""JSON CLI adapter for the scientific-poster deterministic action service."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

import poster_core  # noqa: E402 - copied Skill bootstraps its own root
from posterlib.delivery.portable_actions import run  # noqa: E402


class InputError(ValueError):
    """Portable CLI input is malformed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        print(json.dumps({"status": "error", "error": message}, ensure_ascii=False))
        raise SystemExit(2)


def _error(code: str, message: str) -> dict[str, Any]:
    result = poster_core.outcome_result(code, summary=message, error=message)
    result["skill"] = "scientific-poster"
    return result


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _JsonArgumentParser(description=__doc__)
    parser.add_argument("--json", help="Input JSON object; default reads stdin")
    parser.add_argument(
        "--json-file",
        help="UTF-8 JSON file. Prefer this on Windows/PowerShell; --json quoting is unreliable there.",
    )
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.self_test:
        return {"self_test": True}
    if args.json_file:
        try:
            raw = Path(args.json_file).expanduser().read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise InputError(
                "json_file_unreadable", f"Cannot read --json-file {args.json_file}: {exc}"
            ) from exc
    elif args.json is not None:
        raw = args.json
    else:
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
    raw = raw.strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(
            "invalid_json", f"Command input is not valid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise InputError("invalid_payload", "Command input must be a JSON object.")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        payload = _load_payload(args)
        result = run(payload)
    except InputError as exc:
        result = _error(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001 - CLI boundary always returns structured JSON
        result = _error("runner_failed", str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("status") in {"ok", "partial"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
