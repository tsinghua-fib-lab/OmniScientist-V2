"""Project supported SVG paint into editable native PowerPoint paths."""

from __future__ import annotations

import math
from typing import Any

import tinycss2
from fontTools.pens.basePen import BasePen
from lxml import etree
from tinycss2.color3 import parse_color

# Chromium supplies inline computed styles. Resolve their precedence before
# MuPDF, whose SVG reader prefers presentation attributes and ignores CSS effects.
_INHERITED = {
    "color": "black", "fill": "black", "fill-opacity": "1", "fill-rule": "nonzero",
    "stroke": "none", "stroke-width": "1", "stroke-opacity": "1",
    "stroke-linecap": "butt", "stroke-linejoin": "miter", "stroke-miterlimit": "4",
    "stroke-dasharray": "none", "stroke-dashoffset": "0", "visibility": "visible",
    "marker-start": "none", "marker-mid": "none", "marker-end": "none",
    "font-size": "16",
}
_LOCAL = {
    "display": "inline", "opacity": "1", "clip-path": "none", "mask": "none",
    "mask-image": "none", "filter": "none", "mix-blend-mode": "normal",
    "isolation": "auto", "vector-effect": "none", "paint-order": "normal",
    "overflow": "hidden",
}
_GEOMETRY = {"x", "y", "width", "height", "rx", "ry", "r", "cx", "cy"}
_SHAPES = {"rect", "circle", "ellipse", "line", "polyline", "polygon", "path"}


def expand_svg_drawings(scene: dict[str, Any]) -> None:
    """Expand atomically; unsupported paint raises with the owning SVG's id."""

    if not any(item["kind"] == "svg" for item in scene["objects"]):
        return
    objects = []
    for item in scene["objects"]:
        if item["kind"] != "svg":
            objects.append(item)
            continue
        try:
            svg = _prepare_svg(item["svg"])
            paths = _drawings(svg, item)
        except ValueError as error:
            raise ValueError(f"Native SVG {item['id']}: {error}") from error
        objects.append({
            key: value for key, value in {
                **item, "kind": "shape", "role": "diagram",
                "fill_enabled": False, "line_width_pt": 0, "radius": 0,
            }.items() if key != "svg"
        })
        objects.extend(paths)
    members: dict[str, list[dict[str, Any]]] = {}
    for item in objects:
        if "_svg_order" in item:
            members.setdefault(item["container_id"], []).append(item)
    ordered = []
    for item in objects:
        if "_svg_order" in item:
            continue
        ordered.append(item)
        for child in sorted(members.get(item["id"], []), key=lambda value: value["_svg_order"]):
            ordered.append({key: value for key, value in child.items() if key != "_svg_order"})
    scene["objects"] = ordered


def _style(node: Any, parent: dict[str, str]) -> dict[str, str]:
    """Resolve presentation attributes, inline importance and inheritance."""

    authored = {key: (value, False) for key, value in node.attrib.items()}
    for declaration in tinycss2.parse_declaration_list(
        node.get("style", ""), skip_comments=True, skip_whitespace=True,
    ):
        if declaration.type != "declaration":
            raise ValueError("inline style requires valid CSS declarations")
        name = declaration.lower_name
        if name not in _INHERITED and name not in _LOCAL and name not in _GEOMETRY:
            # Text styling has no paint after Chromium extracts the labels.
            if name.startswith("font-") or name in {"text-anchor", "text-align"}:
                continue
            raise ValueError(f"CSS {name} requires browser-resolved native paint")
        if not authored.get(name, ("", False))[1] or declaration.important:
            authored[name] = (tinycss2.serialize(declaration.value).strip(), declaration.important)
    style = {}
    for name, initial in {**_INHERITED, **_LOCAL}.items():
        value = authored.get(name, (parent.get(name, initial) if name in _INHERITED else initial,))[0]
        if value == "inherit" or (value == "unset" and name in _INHERITED):
            value = parent.get(name, initial)
        elif value in {"initial", "unset"}:
            value = initial
        style[name] = value
    for name in _GEOMETRY:
        if name in authored:
            node.set(name, authored[name][0])
    node.attrib.pop("style", None)
    return style


def _alpha(value: str) -> float:
    tokens = tinycss2.parse_component_value_list(value, skip_comments=True)
    tokens = [token for token in tokens if token.type != "whitespace"]
    if len(tokens) != 1 or tokens[0].type not in {"number", "percentage"}:
        raise ValueError(f"opacity requires a resolved number: {value}")
    return max(0, min(1, tokens[0].value / (100 if tokens[0].type == "percentage" else 1)))


def _prepare_svg(source: str) -> Any:
    """Admit paint that survives both the SVG reader and the native renderer."""

    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True, remove_pis=True)
    root = etree.fromstring(source.encode(), parser)
    if root.xpath(".//*[local-name()='style' and normalize-space(.) != '']"):
        raise ValueError("SVG stylesheet requires complete browser-computed styles before export")

    def visit(node: Any, parent: dict[str, str], *, outer: bool = False) -> bool:
        if node.tag is etree.Entity:
            raise ValueError("SVG entity content requires expansion before export")
        tag = etree.QName(node).localname
        if tag in {"defs", "title", "desc", "metadata"}:
            return False
        style = _style(node, parent)
        if "data-poster-css-path" in node.attrib:
            tokens = [token for token in tinycss2.parse_component_value_list(
                node.attrib.pop("data-poster-css-path"), skip_comments=True,
            ) if token.type != "whitespace"]
            arguments = [token for token in tokens[0].arguments if token.type != "whitespace"] if (
                len(tokens) == 1 and tokens[0].type == "function" and tokens[0].lower_name == "path"
            ) else []
            if len(arguments) != 1 or arguments[0].type != "string":
                raise ValueError("CSS path geometry requires one resolved path string")
            node.set("d", arguments[0].value)
        if style["display"] == "none" or _alpha(style["opacity"]) == 0:
            return False
        if tag not in _SHAPES | {"svg", "g", "a"}:
            raise ValueError(f"{tag} requires resolved native SVG geometry")
        if tag == "svg" and not outer:
            raise ValueError("nested SVG viewport clipping requires native clip geometry")
        for child in list(node):
            if not visit(child, style):
                node.remove(child)
        if tag in {"g", "a", "svg"} and not len(node):
            return outer
        if tag in _SHAPES and style["visibility"] in {"hidden", "collapse"}:
            return False
        for name in ("clip-path", "mask", "mask-image", "filter", "mix-blend-mode", "isolation"):
            if style[name] != _LOCAL[name]:
                raise ValueError(f"{name}={style[name]} requires native clipping/compositing")
        if tag not in _SHAPES and _alpha(style["opacity"]) != 1:
            raise ValueError("opacity compositing requires a native isolated paint group")
        if style["paint-order"] != "normal":
            raise ValueError("paint-order requires ordered native paint events")
        if tag in _SHAPES:
            color = parse_color(style["color"])
            for paint in ("fill", "stroke"):
                value = style[paint]
                if value.lower() == "none":
                    node.set(paint, "none")
                    continue
                rgba = color if value.lower() == "currentcolor" else parse_color(value)
                if rgba is None or isinstance(rgba, str):
                    raise ValueError(f"{paint} paint requires a resolved solid sRGB color: {value}")
                opacity = rgba.alpha * _alpha(style[f"{paint}-opacity"])
                node.set(paint, "#" + "".join(f"{round(channel * 255):02x}" for channel in rgba[:3]))
                node.set(f"{paint}-opacity", str(opacity))
                if opacity == 0:
                    node.set(paint, "none")
            if tag == "line":
                node.set("fill", "none")
            if node.get("stroke") != "none":
                if style["vector-effect"] != "none":
                    raise ValueError("vector-effect requires browser-resolved stroke geometry")
                if style["stroke-linecap"] not in {"butt", "round", "square"}:
                    raise ValueError("stroke-linecap requires a supported native cap")
                if style["stroke-linejoin"] not in {"miter", "round", "bevel"}:
                    raise ValueError("stroke-linejoin requires a supported native join")
            for name in ("marker-start", "marker-mid", "marker-end"):
                if style[name] != "none":
                    raise ValueError(f"{name} requires expanded native marker geometry")
            if node.get("fill") == node.get("stroke") == "none":
                return False
            opacity = _alpha(style["opacity"])
            if opacity != 1 and node.get("fill") != "none" and node.get("stroke") != "none":
                raise ValueError("fill/stroke opacity compositing requires a native isolated paint group")
            node.set("opacity", "1")
            for paint in ("fill", "stroke"):
                node.set(f"{paint}-opacity", str(float(node.get(f"{paint}-opacity", "1")) * opacity))
            # MuPDF's polyline reader only strokes. Its path parser handles the
            # same point grammar and implicit closure for an open filled path.
            if tag == "polyline":
                points = node.get("points", "").strip()
                node.tag = "{http://www.w3.org/2000/svg}path"
                node.set("d", "M" + points if points else "")
                node.attrib.pop("points", None)
            for name in ("stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
                         "stroke-dasharray", "stroke-dashoffset", "fill-rule", "font-size"):
                node.set(name, style[name])
        if tag == "a":
            node.tag = "{http://www.w3.org/2000/svg}g"
        if outer:
            node.set("overflow", style["overflow"])
        return True

    if etree.QName(root).localname != "svg":
        raise ValueError("the native drawing root must be SVG")
    if not visit(root, {}, outer=True):
        root.clear()
        root.set("width", "1")
        root.set("height", "1")
    return root


def _drawings(root: Any, item: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize fill topology and stroke outlines before the scene boundary."""

    try:
        import pathops
    except ImportError as error:
        raise ValueError("skia-pathops is required for native SVG winding and stroke geometry") from error

    result = []
    for element in root.iter():
        if etree.QName(element).localname not in _SHAPES:
            continue
        # Isolating one painted element keeps its dash metadata tied to its
        # device events, including elements that produce no paint.
        isolated = etree.Element(element.tag, attrib=element.attrib)
        for ancestor in element.iterancestors():
            wrapper = etree.Element(ancestor.tag, attrib=ancestor.attrib)
            wrapper.append(isolated)
            isolated = wrapper
        page_width, page_height, trace = _trace(etree.tostring(isolated))
        sx, sy = item["w"] / page_width, item["h"] / page_height
        if "viewBox" in root.attrib:
            viewbox = _numbers(root.get("viewBox"))
            viewport = math.hypot(viewbox[2], viewbox[3]) / math.sqrt(2)
        else:
            viewport = math.hypot(page_width, page_height) / math.sqrt(2)
        for paint in trace:
            if paint.tag not in {"fill_path", "stroke_path"}:
                raise ValueError(f"{paint.tag} requires native clipping/compositing or image paint")
            alpha = float(paint.get("alpha", "1"))
            if alpha == 0:
                continue
            path = pathops.Path(fillType=(
                pathops.FillType.EVEN_ODD if paint.get("winding") == "eofill"
                else pathops.FillType.WINDING
            ))
            for operation, *coordinates in _path_commands(paint):
                getattr(path, {"M": "moveTo", "L": "lineTo", "C": "cubicTo", "Z": "close"}[operation])(
                    *coordinates
                )
            if paint.tag == "stroke_path":
                width = float(paint.get("linewidth"))
                if width == 0:
                    continue
                dashes, phase = _dashes(element, viewport)
                path.stroke(width, int(paint.get("linecap").split(",")[0]),
                            int(paint.get("linejoin")), float(paint.get("miterlimit")),
                            dash_array=dashes, dash_offset=phase)
            a, b, c, d, e, f = map(float, paint.get("transform").split())
            try:
                path = path.transform(a * sx, b * sy, c * sx, d * sy, e * sx, f * sy)
                # Error is bounded in final slide inches: 0.03 pixels at 300 DPI.
                path.convertConicsToQuads(0.0001)
                path.simplify()
                if root.get("overflow", "hidden") != "visible":
                    viewport_path = pathops.Path()
                    viewport_path.moveTo(0, 0)
                    viewport_path.lineTo(item["w"], 0)
                    viewport_path.lineTo(item["w"], item["h"])
                    viewport_path.lineTo(0, item["h"])
                    viewport_path.close()
                    path = pathops.op(path, viewport_path, pathops.PathOp.INTERSECTION)
            except pathops.PathOpsError as error:
                raise ValueError(f"native vector geometry could not be normalized: {error}") from error
            if not path.verbs:
                continue
            left, top, right, bottom = path.bounds
            pen = _ScenePen()
            path.transform(translateX=-left, translateY=-top).draw(pen)
            if paint.get("colorspace") != "DeviceRGB":
                raise ValueError("native path paint requires sRGB")
            color = "".join(f"{round(float(channel) * 255):02X}" for channel in paint.get("color").split())
            result.append({
                "id": f"{item['id']}.path-{len(result)}", "kind": "shape", "role": "diagram-path",
                "module_id": item.get("module_id", ""), "container_id": item["id"],
                "x": item["x"] + left, "y": item["y"] + top,
                "w": max(right - left, 1 / 914400), "h": max(bottom - top, 1 / 914400),
                "fill": color, "fill_enabled": True, "fill_alpha": alpha,
                "line_width_pt": 0, "path": pen.commands, "radius": 0,
                **({"_svg_order": int(element.get("data-poster-paint-index"))}
                   if "data-poster-paint-index" in element.attrib else {}),
            })
    return result


def _trace(svg: bytes) -> tuple[float, float, Any]:
    """Retain MuPDF's moveto and closepath events before get_drawings merges them."""

    import pymupdf

    mupdf = pymupdf.mupdf
    with pymupdf.open(stream=svg, filetype="svg") as document:
        page = document[0]
        buffer = mupdf.FzBuffer(1024)
        output = mupdf.FzOutput(buffer)
        device = mupdf.fz_new_trace_device(output)
        try:
            mupdf.fz_run_page(page.this, device, mupdf.FzMatrix(), mupdf.FzCookie())
        finally:
            mupdf.fz_close_device(device)
            output.fz_close_output()
        trace = etree.fromstring(b"<trace>" + mupdf.fz_buffer_extract(buffer) + b"</trace>")
        return page.rect.width, page.rect.height, trace


def _path_commands(paint: Any) -> list[list[Any]]:
    fields = {
        "moveto": ("M", ("x", "y")), "lineto": ("L", ("x", "y")),
        "curveto": ("C", ("x1", "y1", "x2", "y2", "x3", "y3")), "closepath": ("Z", ()),
    }
    commands = []
    for element in paint:
        if element.tag not in fields:
            raise ValueError(f"unsupported native path event: {element.tag}")
        operation, names = fields[element.tag]
        commands.append([operation, *(float(element.get(name)) for name in names)])
    return commands


def _lengths(value: str, percentage_base: float) -> list[float]:
    # Absolute SVG lengths use the CSS pixel, independent of the element CTM.
    units = {"px": 1, "in": 96, "cm": 96 / 2.54, "mm": 96 / 25.4, "q": 96 / 101.6,
             "pt": 96 / 72, "pc": 16}
    result = []
    for token in tinycss2.parse_component_value_list(value, skip_comments=True):
        if token.type == "whitespace" or (token.type == "literal" and token.value == ","):
            continue
        if token.type == "number":
            result.append(token.value)
        elif token.type == "percentage":
            result.append(token.value * percentage_base / 100)
        elif token.type == "dimension" and token.lower_unit in units:
            result.append(token.value * units[token.lower_unit])
        else:
            raise ValueError(f"SVG dash/viewport lengths require resolved absolute units: {value}")
    return result


def _numbers(value: str) -> list[float]:
    return _lengths(value, 0)


def _dashes(element: Any, viewport: float) -> tuple[list[float] | None, float]:
    value = element.get("stroke-dasharray", "none")
    if value == "none":
        return None, 0
    lengths = _lengths(value, viewport)
    if any(value < 0 for value in lengths):
        raise ValueError("stroke-dasharray lengths must be nonnegative")
    if not lengths or sum(lengths) == 0:
        return None, 0
    if "pathLength" in element.attrib:
        raise ValueError("pathLength-calibrated dash requires resolved native path distances")
    if len(lengths) % 2:
        lengths *= 2
    phase = _lengths(element.get("stroke-dashoffset", "0"), viewport)
    if len(phase) != 1:
        raise ValueError("stroke-dashoffset requires one resolved length")
    return lengths, phase[0]


class _ScenePen(BasePen):
    """Use FontTools' exact quadratic-to-cubic conversion for DrawingML."""

    def __init__(self) -> None:
        super().__init__(None)
        self.commands: list[list[Any]] = []

    def _moveTo(self, point: tuple[float, float]) -> None:
        self.commands.append(["M", *point])

    def _lineTo(self, point: tuple[float, float]) -> None:
        self.commands.append(["L", *point])

    def _curveToOne(self, first: tuple[float, float], second: tuple[float, float],
                    end: tuple[float, float]) -> None:
        self.commands.append(["C", *first, *second, *end])

    def _closePath(self) -> None:
        self.commands.append(["Z"])
