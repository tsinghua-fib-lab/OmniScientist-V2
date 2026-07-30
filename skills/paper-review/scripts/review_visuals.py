#!/usr/bin/env python3
"""Portable MinerU/VLM visual-analysis helper for the paper-review skill."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from paper_review_visual.core import (
    MineruError,
    MineruMissingError,
    VisualItem,
    build_visual_prompt,
    parse_visual_response,
    run_review,
)
from paper_review_visual.vlm import VlmClient, VlmConfig


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
    source = str(payload.get("input") or "").strip()
    if not source:
        return {
            "status": "error",
            "component": "paper-review.visual-analysis",
            "error": "input is required",
        }
    output_dir = Path(
        payload.get("output_dir") or "paper-review-visual-output"
    ).expanduser()
    extract_only = bool(payload.get("extract_only", False))
    config = VlmConfig.from_env()
    vlm = None if extract_only or config.missing_env() else VlmClient(config)
    try:
        result = await run_review(
            source,
            output_dir,
            vlm=vlm,
            max_visuals=_bounded_int(payload.get("max_visuals"), 12, 1, 30),
            visual_types=_types(payload.get("visual_types")),
            analysis_language=str(payload.get("analysis_language") or "").strip(),
            focus=str(payload.get("focus") or "").strip(),
            extract_only=extract_only,
            mineru_backend=str(payload.get("mineru_backend") or "pipeline"),
            mineru_command=str(payload.get("mineru_command") or "mineru"),
            mineru_timeout_s=_bounded_float(
                payload.get("mineru_timeout_s"),
                600.0,
                1.0,
                600.0,
            ),
            mineru_device=str(payload.get("mineru_device") or "auto").strip(),
        )
    except MineruMissingError as exc:
        return {
            "status": "error",
            "component": "paper-review.visual-analysis",
            "error": str(exc),
            "setup_command": 'uv pip install -U "mineru[core]"',
            "error_info": {
                "code": exc.code,
                "category": "dependency",
                "retryable": False,
            },
        }
    except MineruError as exc:
        process_run = exc.runs[-1] if exc.runs else None
        return {
            "status": "error",
            "component": "paper-review.visual-analysis",
            "error": str(exc),
            "mineru_run": process_run.as_dict() if process_run else {},
            "mineru_runtime": (
                process_run.runtime.as_dict() if process_run else {}
            ),
            "artifacts": [
                {
                    "title": title,
                    "format": fmt,
                    "uri": "",
                    "path": str(path),
                }
                for path, title, fmt in _run_specs(process_run)
                if path.is_file()
            ],
            "diagnostic_notice": (
                "MinerU stdout, stderr, run metadata, and the resolved device "
                "decision were saved. Inspect the stderr artifact first."
            ),
            "error_info": {
                "code": exc.code,
                "category": "extraction",
                "retryable": exc.retryable,
                "run_started": process_run is not None,
            },
        }
    payload_out = result.as_dict()
    if not extract_only and config.missing_env():
        payload_out.setdefault("warnings", []).append(
            "Portable VLM analysis needs OMNI_VLM_MODEL, OMNI_VLM_ENDPOINT, "
            "and OMNI_VLM_API_KEY."
        )
    return payload_out


def _self_test() -> dict[str, Any]:
    visual = VisualItem(
        visual_id="image-001",
        visual_type="image",
        page_index=0,
        bbox=(10.0, 20.0, 900.0, 700.0),
        image_path=Path("fixture.png"),
        caption="Figure 1. Offline fixture.",
    )
    prompt = build_visual_prompt(visual, analysis_language="English")
    parsed = parse_visual_response(
        json.dumps(
            {
                "summary": "fixture",
                "readability": "good",
                "caption_alignment": "aligned",
                "scientific_interpretability": "good",
                "positive_evidence": [],
                "issues": [],
                "needs_text_verification": [],
            }
        ),
        visual,
    )
    ok = (
        "untrusted" in prompt
        and parsed["visual_id"] == "image-001"
        and parsed["readability"] == "good"
    )
    return {
        "status": "ok" if ok else "error",
        "component": "paper-review.visual-analysis",
        "portable_runner": True,
    }


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _bounded_float(
    value: Any,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Coerce one numeric option into a bounded runtime-safe range."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _types(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ("image", "chart", "table")
    return tuple(str(item) for item in value)


def _run_specs(
    process_run: Any | None,
) -> tuple[tuple[Path, str, str], ...]:
    """Return portable local diagnostics for the single MinerU run."""

    if process_run is None:
        return ()
    return (
        (process_run.stdout_path, "MinerU standard output", "log"),
        (process_run.stderr_path, "MinerU error output", "log"),
        (process_run.metadata_path, "MinerU run metadata", "json"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run paper-review's portable MinerU/VLM visual-analysis helper."
    )
    parser.add_argument("--json", help="Input JSON object.")
    parser.add_argument(
        "--json-file",
        help="UTF-8 JSON file. Prefer this on Windows/PowerShell; --json quoting is unreliable there.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run an offline smoke test.",
    )
    args = parser.parse_args(argv)
    if args.self_test:
        result = _self_test()
    else:
        try:
            result = asyncio.run(_run(_payload(args)))
        except (json.JSONDecodeError, ValueError) as exc:
            result = {
                "status": "error",
                "component": "paper-review.visual-analysis",
                "error": str(exc),
                "error_info": {
                    "code": "invalid_json",
                    "category": "input",
                    "retryable": False,
                },
            }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
