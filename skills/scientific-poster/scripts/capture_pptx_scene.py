#!/usr/bin/env python3
"""Capture validated scientific-poster HTML as an editable PPTX scene."""

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

import poster_contract  # noqa: E402
from posterlib.content import html_contract  # noqa: E402
from posterlib.delivery.svg_drawing import expand_svg_drawings  # noqa: E402
from posterlib.runtime import browser_scripts  # noqa: E402

CAPTURE_JAVASCRIPT = browser_scripts.load("capture_pptx_scene.js", bind_poster_selector=True)
ROOT_SIZE_JAVASCRIPT = browser_scripts.load("capture_viewport.js", bind_poster_selector=True)


async def capture(path: Path) -> dict[str, Any]:
    """Load one local poster and return its scene."""

    from playwright.async_api import async_playwright

    source = path.read_text(encoding="utf-8")
    rendered = html_contract.embed_math_font(source)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1200}, device_scale_factor=2
        )
        try:
            page = await context.new_page()
            await page.goto(path.resolve().as_uri(), wait_until="load", timeout=45_000)
            # Static validation and browser capture share the UTF-8 source text.
            await page.set_content(rendered, wait_until="load")
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
            expand_svg_drawings(scene)
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
              if (node.textContent.trim() && !node.parentElement.closest('math, style, script'))
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
    """Read painted text runs, cell insets and markers from Chromium's layout."""

    from playwright.async_api import Error

    await page.evaluate("""() => {
        globalThis.__posterListMarkers = new Map();
        globalThis.__posterCellInsets = new WeakMap();
        globalThis.__posterTextRuns = new WeakMap();
        globalThis.__posterPaintOrder = new WeakMap();
        globalThis.__posterPseudoPaints = [];
    }""")
    client = await page.context.new_cdp_session(page)
    try:
        await client.send("DOM.enable")
        await client.send("CSS.enable")
        await client.send("DOM.getDocument")
        snapshot = await browser_scripts.capture_paint_facts(page, client=client)
        strings = snapshot["strings"]
        document = snapshot["documents"][0]
        nodes, layout, boxes = (document[key] for key in ("nodes", "layout", "textBoxes"))
        pseudo = nodes.get("pseudoType", {})
        pseudo_types = {index: strings[value] for index, value in zip(
            pseudo.get("index", []), pseudo.get("value", []), strict=True
        )}
        contexts: dict[int, tuple[int, bool]] = {}
        for index in layout["stackingContexts"]["index"]:
            node = layout["nodeIndex"][index]
            if nodes["nodeType"][node] != 1:
                continue
            z_index = strings[layout["styles"][index][0]]
            contexts[node] = (layout["paintOrders"][index], z_index.startswith("-"))

        def paint_key(node: int, order: int, position: int | float) -> list[int | float]:
            """Retain browser layer order around each context's background and foreground."""

            ancestors = []
            parent = node
            while parent >= 0:
                if parent in contexts:
                    ancestors.append(contexts[parent])
                parent = nodes["parentIndex"][parent]
            key: list[int | float] = []
            for layer, negative in reversed(ancestors):
                key.extend((1 if negative else 3, layer))
            key.extend((0 if node in contexts else 2, order, position))
            return key

        subtree_end = list(range(len(nodes["parentIndex"])))
        for node in range(len(subtree_end) - 1, -1, -1):
            parent = nodes["parentIndex"][node]
            if parent >= 0:
                subtree_end[parent] = max(subtree_end[parent], subtree_end[node])
        recorded: set[int] = set()
        for index, node in enumerate(layout["nodeIndex"]):
            if nodes["nodeType"][node] not in {1, 3}:
                continue
            position: int | float = node
            paint: dict[str, Any] = {}
            owner = node
            if node in pseudo_types:
                bounds = layout["bounds"][index]
                text_index = layout["text"][index]
                if bounds[2] <= 0 or bounds[3] <= 0 or (
                    text_index >= 0 and strings[text_index]
                ):
                    continue
                while owner in pseudo_types:
                    owner = nodes["parentIndex"][owner]
                kind = pseudo_types[node]
                paint.update({
                    "pseudo": kind,
                    "bounds": bounds,
                    # Snapshot lists pseudo nodes before ordinary children.
                    # Within one paint layer, ::after follows the owner's subtree.
                })
                position = (subtree_end[owner] if kind == "after" else owner) + 0.5
            elif node in recorded:
                continue
            recorded.add(node)
            paint["key"] = paint_key(node, layout["paintOrders"][index], position)
            resolved = await client.send("DOM.resolveNode", {
                "backendNodeId": nodes["backendNodeId"][owner],
            })
            await client.send("Runtime.callFunctionOn", {
                "objectId": resolved["object"]["objectId"],
                "functionDeclaration": """function(paint) {
                    if (paint.pseudo) globalThis.__posterPseudoPaints.push({owner: this, ...paint});
                    else globalThis.__posterPaintOrder.set(this, paint);
                }""",
                "arguments": [{"value": paint}],
            })
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
        markers = {index for index, kind in pseudo_types.items() if kind == "marker"}
        runs: dict[int, list[dict[str, Any]]] = {}
        for index, layout_index in enumerate(boxes["layoutIndex"]):
            text_node = node = layout["nodeIndex"][layout_index]
            while node >= 0 and node not in pseudo_types:
                node = nodes["parentIndex"][node]
            if node in pseudo_types and node not in markers:
                owner = nodes["parentIndex"][node]
                resolved = await client.send("DOM.resolveNode", {
                    "backendNodeId": nodes["backendNodeId"][owner],
                })
                in_poster = await client.send("Runtime.callFunctionOn", {
                    "objectId": resolved["object"]["objectId"],
                    "functionDeclaration": (
                        "function(selector) { return Boolean(this.closest(selector)) "
                        "&& !this.closest('svg'); }"
                    ),
                    "arguments": [{"value": poster_contract.POSTER_ROOT_SELECTOR}],
                    "returnByValue": True,
                })
                if in_poster["result"]["value"]:
                    raise RuntimeError(
                        "Generated text has no native scene mapping: "
                        f"::{pseudo_types[node]}. Use ordinary text nodes for editable content."
                    )
                continue
            if node < 0 and nodes["nodeType"][text_node] == 3:
                node = text_node
            text_index = layout["text"][layout_index]
            if node < 0 or text_index < 0:
                continue
            start, length = boxes["start"][index], boxes["length"][index]
            fragment = {"start": start, "length": length, "bounds": boxes["bounds"][index]}
            if node in markers:
                # Generated markers have no selectable DOM text range.
                fragment["text"] = strings[text_index].encode("utf-16-le")[
                    2 * start:2 * (start + length)
                ].decode("utf-16-le")
            runs.setdefault(node, []).append(fragment)
        for node, fragments in runs.items():
            is_marker = node in markers
            owner = nodes["parentIndex"][node] if is_marker else node
            resolved = await client.send("DOM.resolveNode", {"backendNodeId": nodes["backendNodeId"][owner]})
            if not is_marker:
                await client.send("Runtime.callFunctionOn", {
                    "objectId": resolved["object"]["objectId"],
                    "functionDeclaration": "function(runs) { globalThis.__posterTextRuns.set(this, runs); }",
                    "arguments": [{"value": fragments}],
                })
                continue
            faces = []
            try:
                marker_node = await client.send("DOM.pushNodesByBackendIdsToFrontend", {
                    "backendNodeIds": [nodes["backendNodeId"][node]],
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
