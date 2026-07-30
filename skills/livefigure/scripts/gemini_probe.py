#!/usr/bin/env python3
"""Probe LiveFigure's Gemini image and vision calls without exposing secrets."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from livefigure.gemini import GeminiClient, GeminiConfig, GeminiError  # noqa: E402


def _from_omni() -> dict[str, str]:
    """Load current user config only when OmniScientist is installed."""
    try:
        from omni.config import load_settings

        cfg = load_settings().livefigure.gemini
        return {
            "base_url": cfg.base_url,
            "api_key": cfg.api_key,
            "auth_mode": cfg.auth_mode,
            "image_model": cfg.image_model,
            "vision_model": cfg.vision_model,
        }
    except ImportError as exc:
        raise GeminiError(
            "OmniScientist is not installed; use environment variables or CLI arguments instead."
        ) from exc


async def probe(args: argparse.Namespace) -> int:
    stored = _from_omni() if args.use_omni_config else {}
    base_url = args.base_url or os.getenv("OMNI_LIVEFIGURE_GEMINI_BASE_URL") or stored.get("base_url", "")
    api_key = args.api_key or os.getenv("OMNI_LIVEFIGURE_GEMINI_API_KEY") or stored.get("api_key", "")
    auth_mode = args.auth_mode or os.getenv("OMNI_LIVEFIGURE_GEMINI_AUTH_MODE") or stored.get("auth_mode", "bearer")
    image_model = args.image_model or os.getenv("OMNI_LIVEFIGURE_GEMINI_IMAGE_MODEL") or stored.get("image_model", "")
    vision_model = args.vision_model or os.getenv("OMNI_LIVEFIGURE_GEMINI_VISION_MODEL") or stored.get("vision_model", "") or image_model
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    client = GeminiClient(GeminiConfig(base_url, api_key, auth_mode, args.timeout))

    try:
        print("[1/2] Requesting Gemini image …", flush=True)
        image = await client.generate_image(
            "A minimal scientific vector illustration of a neural network node, white background, no text.",
            model=image_model,
        )
        image_path = output_dir / "gemini-probe.png"
        image_path.write_bytes(image)
        print(f"[1/2] Image OK: {image_path} ({len(image)} bytes)", flush=True)

        print("[2/2] Sending the generated image to Gemini vision …", flush=True)
        description = await client.generate_text(
            "Describe this image in one concise sentence. Start the answer with VISION_OK:.",
            model=vision_model,
            image_bytes=image,
        )
        print(f"[2/2] Vision OK: {description[:500]}", flush=True)
    except GeminiError as exc:
        print(f"Probe failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test LiveFigure Gemini image + vision access.")
    parser.add_argument("--use-omni-config", action="store_true", help="Read current ~/.omni LiveFigure configuration.")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="", help="Avoid this when possible; prefer a secret config or env var.")
    parser.add_argument("--auth-mode", choices=("bearer", "x-goog-api-key"), default="")
    parser.add_argument("--image-model", default="")
    parser.add_argument("--vision-model", default="")
    parser.add_argument("--output-dir", default="gemini-probe-output")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    return asyncio.run(probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
