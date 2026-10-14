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

import poster_contract  # noqa: E402
from posterlib.runtime import browser_scripts  # noqa: E402

CAPTURE_JAVASCRIPT = browser_scripts.load("capture_pptx_scene.js", bind_poster_selector=True)
ROOT_SIZE_JAVASCRIPT = browser_scripts.load("capture_viewport.js", bind_poster_selector=True)


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
            await _capture_platform_fonts(page)
            await _capture_layout_metadata(page)
            scene = await page.evaluate(CAPTURE_JAVASCRIPT)
            await _rasterize_svg_images(page, scene)
            return scene
        finally:
            await context.close()
            await browser.close()


async def _capture_platform_fonts(page: Any) -> None:
    """Record Chromium's rendered faces instead of choosing a second font system."""

    from playwright.async_api import Error

    await page.evaluate("globalThis.__posterPlatformFonts = new WeakMap()")
    client = await page.context.new_cdp_session(page)
    try:
        await client.send("DOM.enable")
        await client.send("CSS.enable")
        await client.send("DOM.getDocument")
        nodes = await client.send("Runtime.evaluate", {"expression": f"""
          (() => {{
            const root = document.querySelector({json.dumps(poster_contract.POSTER_ROOT_SELECTOR)});
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            const nodes = [];
            for (let node = walker.nextNode(); node; node = walker.nextNode()) {{
              if (node.textContent.trim() && !node.parentElement.closest('svg, math, style, script'))
                nodes.push(node);
            }}
            return nodes;
          }})()
        """})
        properties = await client.send("Runtime.getProperties", {
            "objectId": nodes["result"]["objectId"], "ownProperties": True,
        })
        for item in properties["result"]:
            value = item.get("value", {})
            if value.get("subtype") != "node":
                continue
            node = await client.send("DOM.requestNode", {"objectId": value["objectId"]})
            used = await client.send("CSS.getPlatformFontsForNode", node)
            await client.send("Runtime.callFunctionOn", {
                "objectId": value["objectId"],
                "functionDeclaration": (
                    "function(faces) { globalThis.__posterPlatformFonts.set(this, faces); }"
                ),
                "arguments": [{"value": [font["familyName"] for font in used["fonts"]]}],
            })
    except Error:
        # Unconfirmed objects keep their declared face and the existing
        # font-preflight warning; unavailable metadata is not a new export gate.
        pass
    finally:
        await client.detach()


async def _capture_layout_metadata(page: Any) -> None:
    """Read used cell insets and native markers without recreating browser layout."""

    from playwright.async_api import Error

    await page.evaluate("globalThis.__posterListMarkers = new Map(); globalThis.__posterCellInsets = new WeakMap()")
    client = await page.context.new_cdp_session(page)
    try:
        await client.send("DOM.enable")
        await client.send("CSS.enable")
        await client.send("DOM.getDocument")
        snapshot = await client.send("DOMSnapshot.captureSnapshot", {
            "computedStyles": [],
        })
        strings = snapshot["strings"]
        document = snapshot["documents"][0]
        nodes, layout, boxes = (document[key] for key in ("nodes", "layout", "textBoxes"))
        for node in layout["nodeIndex"]:
            if strings[nodes["nodeName"][node]] not in {"TD", "TH"}:
                continue
            target = {"backendNodeId": nodes["backendNodeId"][node]}
            model = (await client.send("DOM.getBoxModel", target))["model"]
            content, border = model["content"], model["border"]
            # The used collapsed border can differ from computed CSS and includes
            # half pixels lost by clientLeft/clientTop. Office margins start at it.
            insets = [content[1] - border[1], border[2] - content[2],
                      border[5] - content[5], content[0] - border[0]]
            resolved = await client.send("DOM.resolveNode", target)
            await client.send("Runtime.callFunctionOn", {
                "objectId": resolved["object"]["objectId"],
                "functionDeclaration": "function(insets) { globalThis.__posterCellInsets.set(this, insets); }",
                "arguments": [{"value": insets}],
            })
        pseudo = nodes.get("pseudoType", {})
        markers = {index for index, value in zip(pseudo.get("index", []), pseudo.get("value", []), strict=True)
                   if strings[value] == "marker"}
        runs: dict[int, list[dict[str, Any]]] = {}
        for index, layout_index in enumerate(boxes["layoutIndex"]):
            node = layout["nodeIndex"][layout_index]
            while node >= 0 and node not in markers:
                node = nodes["parentIndex"][node]
            text_index = layout["text"][layout_index]
            if node < 0 or text_index < 0:
                continue
            # CDP offsets count UTF-16 code units, not Python Unicode code points.
            start, length = boxes["start"][index], boxes["length"][index]
            text = strings[text_index].encode("utf-16-le")[2 * start:2 * (start + length)].decode("utf-16-le")
            runs.setdefault(node, []).append({"text": text, "bounds": boxes["bounds"][index]})
        for marker, fragments in runs.items():
            owner = nodes["parentIndex"][marker]
            resolved = await client.send("DOM.resolveNode", {"backendNodeId": nodes["backendNodeId"][owner]})
            faces = []
            try:
                marker_node = await client.send("DOM.pushNodesByBackendIdsToFrontend", {
                    "backendNodeIds": [nodes["backendNodeId"][marker]],
                })
                used = await client.send("CSS.getPlatformFontsForNode", {"nodeId": marker_node["nodeIds"][0]})
                faces = [font["familyName"] for font in used["fonts"]]
            except Error:
                pass  # Same declared-face fallback and font warning as ordinary text.
            await client.send("Runtime.callFunctionOn", {
                "objectId": resolved["object"]["objectId"],
                "functionDeclaration": "function(runs, faces) { globalThis.__posterListMarkers.set(this, {runs, faces}); }",
                "arguments": [{"value": fragments}, {"value": faces}],
            })
    finally:
        await client.detach()


async def _rasterize_svg_images(page: Any, scene: dict[str, Any]) -> None:
    """Use Chromium's SVG rendering when Office rasterizers are not equivalent."""

    objects = scene.get("objects")
    if not isinstance(objects, list):
        raise RuntimeError("captured PPTX scene has no object array")
    image_elements = page.locator(poster_contract.POSTER_ROOT_SELECTOR).locator("img, svg")
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
