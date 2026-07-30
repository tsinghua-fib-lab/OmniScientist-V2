#!/usr/bin/env python3
"""Capture validated scientific-poster HTML as an editable PPTX scene."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

import poster_core  # noqa: E402
from posterlib.runtime import browser_scripts  # noqa: E402

CAPTURE_JAVASCRIPT = browser_scripts.load("capture_scene.js", bind_poster_selector=True)
ROOT_SIZE_JAVASCRIPT = browser_scripts.load("root_size.js", bind_poster_selector=True)


async def capture(path: Path) -> dict[str, Any]:
    """Load one local poster and return its scene."""

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1200}, device_scale_factor=2
        )
        try:
            page = await context.new_page()
            await page.goto(path.resolve().as_uri(), wait_until="load", timeout=45_000)
            await page.evaluate(
                "document.fonts ? document.fonts.ready : Promise.resolve()"
            )
            size = await page.evaluate(ROOT_SIZE_JAVASCRIPT)
            if not isinstance(size, dict):
                raise RuntimeError("poster root was not found")
            width = min(8000, max(800, int(size["width"])))
            height = min(8000, max(600, int(size["height"])))
            await page.set_viewport_size({"width": width, "height": height})
            await page.wait_for_timeout(100)
            scene = await page.evaluate(CAPTURE_JAVASCRIPT)
            await _rasterize_svg_images(page, scene)
            await _attach_equation_fallbacks(page, scene)
            return scene
        finally:
            await context.close()
            await browser.close()


async def _attach_equation_fallbacks(page: Any, scene: dict[str, Any]) -> None:
    """Attach high-resolution browser previews for non-PowerPoint viewers."""

    objects = scene.get("objects")
    if not isinstance(objects, list):
        raise RuntimeError("captured PPTX scene has no object array")
    math_elements = page.locator("math")
    math_count = await math_elements.count()
    for item in objects:
        if not isinstance(item, dict) or item.get("kind") != "equation":
            continue
        index = item.pop("math_index", None)
        if not isinstance(index, int) or not 0 <= index < math_count:
            raise RuntimeError("captured equation has no matching MathML element")
        png = await math_elements.nth(index).screenshot(
            type="png", omit_background=True
        )
        item["fallback_src"] = "data:image/png;base64," + base64.b64encode(png).decode(
            "ascii"
        )


async def _rasterize_svg_images(page: Any, scene: dict[str, Any]) -> None:
    """Use Chromium's SVG rendering when Office rasterizers are not equivalent."""

    objects = scene.get("objects")
    if not isinstance(objects, list):
        raise RuntimeError("captured PPTX scene has no object array")
    image_elements = page.locator(poster_core.POSTER_ROOT_SELECTOR).locator("img, svg")
    image_count = await image_elements.count()
    for item in objects:
        if not isinstance(item, dict) or item.get("kind") != "image":
            continue
        index = item.pop("image_index", None)
        source = str(item.get("src") or "")
        if not source.startswith("data:image/svg+xml"):
            continue
        if not isinstance(index, int) or not 0 <= index < image_count:
            raise RuntimeError("captured SVG has no matching browser element")
        png = await image_elements.nth(index).screenshot(
            type="png", omit_background=True
        )
        item["src"] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    path = Path(args.html).expanduser().resolve()
    try:
        scene = asyncio.run(capture(path))
    except Exception as exc:  # noqa: BLE001 - child boundary always emits JSON
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "ok", "scene": scene}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
