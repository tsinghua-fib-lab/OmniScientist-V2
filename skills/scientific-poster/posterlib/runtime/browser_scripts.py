"""Load browser scripts and share Chromium paint facts across poster workflows."""

from __future__ import annotations

import io
import json
import math
from typing import Any

import poster_contract

from posterlib.paths import SKILL_ROOT

_SCRIPT_DIR = SKILL_ROOT / "scripts" / "browser"
_POSTER_SELECTOR_MARKER = "__POSTER_ROOT_SELECTOR__"


def load(filename: str, *, bind_poster_selector: bool = False) -> str:
    """Return one shipped browser script with an optional poster-root binding."""

    script = (_SCRIPT_DIR / filename).read_text(encoding="utf-8")
    if "__PAINTED_IMAGE_SIZE__" in script:
        script = script.replace(
            "__PAINTED_IMAGE_SIZE__",
            (_SCRIPT_DIR / "painted_image_size.js").read_text(encoding="utf-8"),
        )
    if "__PAINT_VISIBILITY__" in script:
        script = script.replace(
            "__PAINT_VISIBILITY__",
            (_SCRIPT_DIR / "paint_visibility.js").read_text(encoding="utf-8"),
        )
    if not bind_poster_selector:
        return script
    if script.count(_POSTER_SELECTOR_MARKER) != 1:
        raise RuntimeError(
            "browser script must contain exactly one poster selector marker"
        )
    return script.replace(
        _POSTER_SELECTOR_MARKER,
        json.dumps(poster_contract.POSTER_ROOT_SELECTOR, ensure_ascii=False),
    )


async def capture_paint_facts(page: Any, *, client: Any = None) -> dict[str, Any]:
    """Share Chromium's generated boxes and exposed canvas backgrounds."""

    owned_client = client is None
    if owned_client:
        client = await page.context.new_cdp_session(page)
    try:
        await client.send("DOM.enable")
        await client.send("DOM.getDocument")
        snapshot = await client.send("DOMSnapshot.captureSnapshot", {
            "computedStyles": [
                "z-index", "background-color", "background-image", "box-shadow",
                "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
            ], "includePaintOrder": True,
        })
        await page.evaluate("""() => {
            globalThis.__posterPaintFacts = {
                pseudoBoxes: new WeakMap(), ancestorBackgrounds: [], textOcclusions: new WeakMap()
            };
        }""")
        document = snapshot["documents"][0]
        nodes, layout = document["nodes"], document["layout"]
        pseudo = nodes.get("pseudoType", {})
        kinds = {node: snapshot["strings"][kind] for node, kind in zip(
            pseudo.get("index", []), pseudo.get("value", []), strict=True,
        )}
        for index, node in enumerate(layout["nodeIndex"]):
            if node not in kinds:
                continue
            owner = nodes["parentIndex"][node]
            while owner in kinds:
                owner = nodes["parentIndex"][owner]
            resolved = await client.send("DOM.resolveNode", {
                "backendNodeId": nodes["backendNodeId"][owner],
            })
            await client.send("Runtime.callFunctionOn", {
                "objectId": resolved["object"]["objectId"],
                "functionDeclaration": """function(kind, bounds) {
                    const boxes = globalThis.__posterPaintFacts.pseudoBoxes;
                    if (!boxes.has(this)) boxes.set(this, {});
                    (boxes.get(this)[kind] ||= []).push(bounds);
                }""",
                "arguments": [{"value": kinds[node]}, {"value": layout["bounds"][index]}],
            })
        await _capture_ancestor_backgrounds(page)
        await _capture_text_occlusions(page, client, snapshot)
        return snapshot
    finally:
        if owned_client:
            await client.detach()


async def _capture_text_occlusions(page: Any, client: Any, snapshot: dict[str, Any]) -> None:
    """Confirm candidate paint overlaps by measuring text contribution in Chromium."""

    from PIL import Image, ImageChops

    document, strings = snapshot["documents"][0], snapshot["strings"]
    nodes, layout = document["nodes"], document["layout"]
    parents = nodes["parentIndex"]
    ancestors: list[set[int]] = []
    for parent in parents:
        ancestors.append(ancestors[parent] | {parent} if parent >= 0 else set())
    pseudo = nodes.get("pseudoType", {})
    kinds = {node: strings[kind] for node, kind in zip(
        pseudo.get("index", []), pseudo.get("value", []), strict=True,
    )}
    svg_nodes = {node for node, name in enumerate(nodes["nodeName"]) if strings[name] == "svg"}
    paints = []
    for index, node in enumerate(layout["nodeIndex"]):
        if nodes["nodeType"][node] != 1:
            continue
        style = [strings[value] if value >= 0 else "" for value in layout["styles"][index]]
        if (style[1] not in {"", "transparent", "rgba(0, 0, 0, 0)"}
                or style[2] not in {"", "none"} or style[3] not in {"", "none"}
                or any(value not in {"", "0px"} for value in style[4:])
                or strings[nodes["nodeName"][node]].lower() in {"img", "svg", "canvas"}
                or ancestors[node] & svg_nodes):
            paints.append(index)

    groups = []
    for index, node in enumerate(layout["nodeIndex"]):
        if nodes["nodeType"][node] != 3 or parents[node] in kinds:
            continue
        text = layout["text"][index]
        if text < 0 or not strings[text].strip():
            continue
        x, y, width, height = layout["bounds"][index]
        candidates = []
        for other in paints:
            paint_node = layout["nodeIndex"][other]
            if paint_node in ancestors[node] or layout["paintOrders"][other] < layout["paintOrders"][index]:
                continue
            px, py, pw, ph = layout["bounds"][other]
            if min(x + width, px + pw) - max(x, px) > 1 and min(y + height, py + ph) - max(y, py) > 1:
                candidates.append(other)
        candidates = [other for other in candidates if not any(
            layout["nodeIndex"][parent] in ancestors[layout["nodeIndex"][other]]
            for parent in candidates
        )]
        if candidates:
            groups.append((node, candidates))
    if not groups:
        return

    await page.evaluate("globalThis.__posterPaintNodes = new Map()")
    needed = {node for node, _ in groups}
    for _, candidates in groups:
        for index in candidates:
            node = layout["nodeIndex"][index]
            needed.add(parents[node] if node in kinds else node)
    try:
        for node in needed:
            resolved = await client.send("DOM.resolveNode", {
                "backendNodeId": nodes["backendNodeId"][node], "objectGroup": "poster-text-visibility",
            })
            await client.send("Runtime.callFunctionOn", {
                "objectId": resolved["object"]["objectId"],
                "functionDeclaration": "function(index) { globalThis.__posterPaintNodes.set(index, this); }",
                "arguments": [{"value": node}],
            })
        visibility = load("paint_visibility.js")
        for node, candidates in groups:
            probe = await page.evaluate_handle("""({node, paints, selector}) => {
                const root = document.querySelector(selector), text = globalThis.__posterPaintNodes.get(node);
                if (!root?.contains(text)) return null;
                return visibility(root).textProbe(text, paints.map(paint => ({
                    ...paint, element: globalThis.__posterPaintNodes.get(paint.node)
                })));
            }""".replace("visibility(root)", f"({visibility})(root)"), {
                "node": node, "selector": poster_contract.POSTER_ROOT_SELECTOR,
                "paints": [{
                    "node": parents[layout["nodeIndex"][index]] if layout["nodeIndex"][index] in kinds else layout["nodeIndex"][index],
                    "pseudo": kinds.get(layout["nodeIndex"][index], ""),
                    "paint_order": layout["paintOrders"][index],
                } for index in candidates],
            })
            try:
                rect = await probe.evaluate("probe => probe?.rect")
                if rect is None:
                    continue
                clip = {"x": max(0, math.floor(rect["x"])), "y": max(0, math.floor(rect["y"]))}
                clip.update(width=math.ceil(rect["x"] + rect["width"]) - clip["x"],
                            height=math.ceil(rect["y"] + rect["height"]) - clip["y"])
                if clip["width"] <= 0 or clip["height"] <= 0:
                    continue
                frames = []
                for text_visible, paint_visible in ((True, True), (False, True), (False, False), (True, False)):
                    await probe.evaluate("(probe, state) => probe.show(...state)", [text_visible, paint_visible])
                    frames.append(Image.open(io.BytesIO(await page.screenshot(
                        clip=clip, scale="css", animations="disabled",
                    ))).convert("RGB"))
                visible = ImageChops.difference(frames[0], frames[1])
                exposed = ImageChops.difference(frames[3], frames[2])
                # Strong glyph pixels absent from the original composition are lost ink.
                # The small channel tolerance excludes antialiasing changes at clear edges.
                mask = Image.new("L", visible.size)
                mask.putdata([255 if max(actual) <= 2 and max(expected) > 16 else 0
                              for actual, expected in zip(visible.getdata(), exposed.getdata(), strict=True)])
                bounds = mask.getbbox()
                area = mask.histogram()[255]
                if bounds is not None and area > 1:
                    left, top, right, bottom = bounds
                    await probe.evaluate("(probe, evidence) => probe.record(evidence)", {
                        "rect": {"x": clip["x"] + left, "y": clip["y"] + top,
                                 "width": right - left, "height": bottom - top},
                        "area": area,
                    })
            finally:
                await probe.evaluate("probe => probe?.restore()")
                await probe.dispose()
    finally:
        await page.evaluate("delete globalThis.__posterPaintNodes")
        await client.send("Runtime.releaseObjectGroup", {"objectGroup": "poster-text-visibility"})


async def _capture_ancestor_backgrounds(page: Any) -> None:
    """Measure which ancestor image layers contribute to the poster pixels."""

    ancestors = await page.evaluate_handle("""selector => {
        const root = document.querySelector(selector), layers = [];
        for (let element = root?.parentElement; element; element = element.parentElement) {
            if (getComputedStyle(element).backgroundImage !== 'none') layers.push(element);
        }
        return layers;
    }""", poster_contract.POSTER_ROOT_SELECTOR)
    try:
        layers = list((await ancestors.get_properties()).values())
        if not layers:
            return
        root = page.locator(poster_contract.POSTER_ROOT_SELECTOR)
        original = await root.screenshot(animations="disabled")
        for layer in layers:
            style = await layer.evaluate("element => element.getAttribute('style')")
            try:
                await layer.evaluate("""element => {
                    element.style.setProperty('background-image', 'none', 'important');
                }""")
                changed = await root.screenshot(animations="disabled")
            finally:
                await layer.evaluate("""(element, style) => {
                    element.setAttribute('style', style ?? '');
                    if (style === null) {
                        element.removeAttribute('style');
                    }
                }""", style)
            if changed != original:
                await layer.evaluate("element => globalThis.__posterPaintFacts.ancestorBackgrounds.push(element)")
    finally:
        await ancestors.dispose()
