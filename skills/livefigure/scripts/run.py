#!/usr/bin/env python3
"""Portable runner for the LiveFigure PPTX skill without OmniScientist."""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from livefigure.pipeline import LiveFigureError, PipelineConfig, generate_pptx  # noqa: E402
from livefigure.vlm import VlmClient, VlmConfig  # noqa: E402


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


async def _run(payload: dict[str, Any]) -> dict[str, Any]:
    requirement = str(payload.get("input") or payload.get("query") or "").strip()
    if not requirement:
        return {"status": "error", "skill": "livefigure", "error": "input is required"}
    out_dir = Path(payload.get("output_dir") or "livefigure-output")
    config, secret = _pipeline_config(payload, out_dir)
    if config is None:
        return _config_error()
    try:
        result = await generate_pptx(
            requirement,
            title=str(payload.get("title") or "LiveFigure scientific diagram"),
            output_dir=out_dir,
            config=config,
            reference_image_uri=str(payload.get("reference_image_uri") or "").strip() or None,
        )
    except LiveFigureError as exc:
        message = _redact(str(exc), secret)
        return {
            "status": "error",
            "skill": "livefigure",
            "error": message,
            "error_info": {
                "code": exc.code,
                "category": exc.category,
                "retryable": exc.retryable,
                "workflow_recoverable": exc.retryable,
            },
        }
    artifacts = [
        {"title": "Editable PPTX", "format": "pptx", "path": str(result.pptx_path)},
        {"title": "Generated source", "format": "py", "path": str(result.code_path)},
        {"title": "Input", "format": "txt", "path": str(result.input_path)},
    ]
    if result.reference_path:
        reference_format, reference_mime = _image_metadata(result.reference_path)
        artifacts.append(
            {
                "title": "Reference image",
                "format": reference_format,
                "path": str(result.reference_path),
                "mime": reference_mime,
            }
        )
    return {
        "status": "ok",
        "skill": "livefigure",
        "title": result.title,
        "artifacts": artifacts,
        "attempts": result.attempts,
    }


def _pipeline_config(
    payload: dict[str, Any], output_dir: Path
) -> tuple[PipelineConfig | None, str]:
    vlm = VlmConfig.from_env()
    if vlm.missing_env():
        return None, vlm.api_key
    roots = (Path.cwd().resolve(), output_dir.expanduser().resolve())
    vlm = replace(vlm, reference_roots=roots)
    return (
        PipelineConfig(
            vlm=VlmClient(vlm),
            max_code_retries=int(payload.get("max_code_retries", 1)),
            reference_roots=roots,
        ),
        vlm.api_key,
    )


def _config_error() -> dict[str, Any]:
    return {
        "status": "error",
        "skill": "livefigure",
        "error": (
            "LiveFigure needs VLM configuration. For direct runner mode, set "
            "OMNI_VLM_MODEL, OMNI_VLM_ENDPOINT, and OMNI_VLM_API_KEY. For the "
            "recommended Omni/MCP path, run `omni config vlm`."
        ),
        "setup_command": "omni config vlm",
        "next_actions": [
            "set OMNI_VLM_MODEL, OMNI_VLM_ENDPOINT, and OMNI_VLM_API_KEY",
            "or run omni config vlm and invoke LiveFigure through Omni MCP",
        ],
        "error_info": {
            "code": "vlm_not_configured",
            "category": "configuration",
            "retryable": False,
        },
    }


def _redact(message: str, secret: str) -> str:
    return str(message).replace(secret, "[REDACTED]") if secret else str(message)


def _image_metadata(path: Path) -> tuple[str, str]:
    image_format = path.suffix.lower().lstrip(".") or "image"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return image_format, mime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the portable LiveFigure skill.")
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
                {"status": "ok", "skill": "livefigure", "portable_runner": True}, ensure_ascii=False
            )
        )
        return 0
    try:
        result = asyncio.run(_run(_payload(args)))
    except (json.JSONDecodeError, ValueError) as exc:
        result = {"status": "error", "skill": "livefigure", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
