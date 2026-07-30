#!/usr/bin/env python3
"""Portable JSON runner for SoulAgent.

This entry point deliberately depends only on the Python standard library and
the files copied with this skill.  It therefore works in Claude Code, Codex,
and OpenClaw layouts without importing OmniScientist.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SKILL = "soulagent"
SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR))

import core
from kg_loader import KGValidationError
from stoma_writer import StomaError


def _configure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.json_file:
        raw = Path(args.json_file).expanduser().read_text(encoding="utf-8-sig")
    elif args.json is not None:
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
        # Keep all user-input validation under the runner's ValueError boundary.
        raise ValueError("input JSON must be an object")  # noqa: TRY004
    return value


def _project_and_kg(payload: dict[str, Any]) -> tuple[Path, Path]:
    project = Path(str(payload.get("project_root") or ".")).expanduser().resolve()
    kg_value = payload.get("kg_root")
    project_kg = project / "scientist-kg"
    kg_root = (
        Path(str(kg_value)).expanduser().resolve()
        if kg_value
        else project_kg
        if project_kg.is_dir()
        else Path.home() / ".omni" / "scientist-kg"
    )
    return project, kg_root


def _execute(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip().casefold()
    action = {"refresh": "activate", "switch": "activate"}.get(action, action)
    if action not in {"list", "activate", "status", "unload"}:
        raise ValueError("action must be one of: list, activate, refresh, switch, status, unload")

    project, kg_root = _project_and_kg(payload)
    if action == "list":
        return core.list_scientists(kg_root)
    if action == "status":
        return core._read_state(project) or {"status": "inactive"}
    if action == "unload":
        return core.unload(project)

    conversation = payload.get("conversation", payload.get("input"))
    if not isinstance(conversation, (str, list)) or not conversation:
        raise ValueError("conversation (or input) is required for activation")
    host = str(payload.get("host") or "").strip()
    if not host:
        raise ValueError("host is required for activation")
    scientist_id = str(payload.get("scientist_id") or "").strip() or None
    return core.run_pipeline(
        project_root=project,
        kg_root=kg_root,
        conversation=conversation,
        scientist_id=scientist_id,
        host=host,
        force=bool(payload.get("force", False)),
        registry_url=str(payload.get("registry_url") or core.DEFAULT_REGISTRY_URL),
    )


def main(argv: list[str] | None = None) -> int:
    _configure_utf8()
    parser = argparse.ArgumentParser(description="Run the portable SoulAgent skill.")
    parser.add_argument("--json", help="Input JSON object; stdin is also accepted.")
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
        result = _execute(_load_payload(args))
    except (
        ValueError,
        json.JSONDecodeError,
        core.SoulAgentError,
        KGValidationError,
        StomaError,
        RuntimeError,
        OSError,
    ) as exc:
        result = {"status": "error", "skill": SKILL, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
