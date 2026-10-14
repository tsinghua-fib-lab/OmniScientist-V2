#!/usr/bin/env python3
"""Render a canonical scientific-poster scene as native PowerPoint objects."""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from lxml import etree

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from posterlib.delivery.pptx_math import build_text_math_xml  # noqa: E402
from posterlib.delivery.pptx_scene import normalize_scene  # noqa: E402

_SVG_TARGET_PPI = 300
_SVG_MAX_LONG_EDGE_PIXELS = 8192
_SVG_MAX_PIXELS = 24_000_000


def render_scene(scene_path: Path, output_path: Path) -> None:
    """Write one editable custom-size slide from a validated scene."""

    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
        from pptx.util import Inches, Pt
    except ImportError as exc:
        raise RuntimeError("python-pptx is required for editable PPTX export") from exc

    scene = normalize_scene(json.loads(scene_path.read_text(encoding="utf-8")))
    presentation = Presentation()
    presentation.slide_width = Inches(scene["page"]["width_in"])
    presentation.slide_height = Inches(scene["page"]["height_in"])
    properties = presentation.core_properties
    properties.author = "OmniScientist scientific-poster"
    properties.subject = "Editable academic poster"
    properties.title = "Scientific poster"

    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    background = slide.background.fill
    background.solid()
    background.fore_color.rgb = RGBColor.from_string(scene["page"]["background"])
    alignments = {
        "left": PP_ALIGN.LEFT,
        "center": PP_ALIGN.CENTER,
        "right": PP_ALIGN.RIGHT,
    }

    for item in scene["objects"]:
        x, y, width, height = (item[key] for key in ("x", "y", "w", "h"))
        geometry = tuple(Inches(value) for value in (x, y, width, height))
        if item["kind"] == "shape":
            shape_type = (
                MSO_SHAPE.ROUNDED_RECTANGLE
                if item["radius"] > 0
                else MSO_SHAPE.RECTANGLE
            )
            shape = slide.shapes.add_shape(shape_type, *geometry)
            if "border_widths" in item:
                _set_rounded_border_geometry(shape, item["border_widths"], item["radius"])
            elif item["radius"] > 0:
                shape.adjustments[0] = min(
                    0.5,
                    float(item["radius"]) / min(float(width), float(height)),
                )
            if item["fill_enabled"]:
                shape.fill.solid()
                shape.fill.fore_color.rgb = RGBColor.from_string(item["fill"])
            else:
                shape.fill.background()
            if item["line_width_pt"] > 0:
                shape.line.color.rgb = RGBColor.from_string(item["line"])
                shape.line.width = Pt(item["line_width_pt"])
            else:
                shape.line.fill.background()
            _remove_theme_shape_style(shape)
        elif item["kind"] == "image":
            source = _image_source(
                item["src"],
                scene_path.parent,
                width_in=width,
                height_in=height,
            )
            shape = slide.shapes.add_picture(source, *geometry)
            if item["alt"]:
                shape._element.nvPicPr.cNvPr.set("descr", item["alt"])
        elif item["kind"] == "equation":
            _add_native_equation(
                slide,
                item,
                geometry,
                alignments,
                RGBColor,
                MSO_ANCHOR,
                Pt,
            )
            continue
        elif item["kind"] == "table":
            rows = item["rows"]
            shape = slide.shapes.add_table(len(rows), len(rows[0]), *geometry)
            shape.table._tbl.tblPr.clear()
            for row, height_in in zip(
                shape.table.rows,
                item["row_heights"],
                strict=True,
            ):
                row.height = Inches(height_in)
            for column, width_in in zip(
                shape.table.columns,
                item["column_widths"],
                strict=True,
            ):
                column.width = Inches(width_in)
            for merge in item["merges"]:
                row, column = merge["row"], merge["column"]
                shape.table.cell(row, column).merge(shape.table.cell(
                    row + merge["row_span"] - 1, column + merge["column_span"] - 1
                ))
            for row_index, row in enumerate(rows):
                for column_index, value in enumerate(row):
                    cell = shape.table.cell(row_index, column_index)
                    style = item["cell_styles"][row_index][column_index]
                    cell.text = value
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = RGBColor.from_string(style["fill"])
                    for side, padding in style["padding"].items():
                        setattr(cell, f"margin_{side}", Inches(padding))
                    cell.vertical_anchor = {
                        "top": MSO_ANCHOR.TOP,
                        "middle": MSO_ANCHOR.MIDDLE,
                        "bottom": MSO_ANCHOR.BOTTOM,
                    }[style["vertical_align"]]
                    _set_cell_borders(cell, style["borders"], Pt)
                    text_frame = cell.text_frame
                    text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
                    text_frame.word_wrap = style["word_wrap"]
                    runs = item["cell_runs"][row_index][column_index]
                    if runs:
                        text_frame.clear()
                        paragraph = text_frame.paragraphs[0]
                        for run_style in runs:
                            parts = run_style["text"].split("\n")
                            for part_index, text in enumerate(parts):
                                if part_index:
                                    paragraph = text_frame.add_paragraph()
                                if text or part_index:
                                    paragraph.alignment = alignments[style["align"]]
                                    paragraph.space_after = Pt(0)
                                    _set_maximum_line_spacing(paragraph, run_style, Pt)
                                if text:
                                    run = paragraph.add_run()
                                    run.text = text
                                    _set_run_font(run, run_style, RGBColor, Pt)
                    for paragraph in text_frame.paragraphs:
                        paragraph.alignment = alignments[style["align"]]
                        paragraph.space_after = Pt(0)
                        if not runs:
                            paragraph.font.name = style["font_face"]
                            paragraph.font.size = Pt(style["font_size_pt"])
                            paragraph.font.bold = style["bold"]
                            paragraph.font.italic = style["italic"]
                            paragraph.font.color.rgb = RGBColor.from_string(style["color"])
                            _set_paragraph_spacing(paragraph, style, Pt)
        else:
            shape = slide.shapes.add_textbox(*geometry)
            text_frame = shape.text_frame
            text_frame.clear()
            text_frame.margin_left = 0
            text_frame.margin_right = 0
            text_frame.margin_top = 0
            text_frame.margin_bottom = 0
            text_frame.vertical_anchor = MSO_ANCHOR.TOP
            text_frame.auto_size = {
                "shrink": MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE,
                "grow": MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT,
                "none": MSO_AUTO_SIZE.NONE,
            }[item["fit"]]
            text_frame.word_wrap = item["word_wrap"]
            paragraph = text_frame.paragraphs[0]
            paragraph.text = item["text"]
            paragraph.alignment = alignments[item["align"]]
            paragraph.space_after = Pt(0)
            paragraph.font.name = item["font_face"]
            paragraph.font.size = Pt(item["font_size_pt"])
            paragraph.font.bold = item["bold"]
            paragraph.font.italic = item["italic"]
            paragraph.font.color.rgb = RGBColor.from_string(item["color"])
            _set_paragraph_spacing(paragraph, item, Pt)
        shape.name = item["id"]

    # The table, its native ink and mixed-content carriers move as one object.
    # Preserve the table's layer position when its children were captured later.
    native = {shape.name: shape for shape in slide.shapes}
    for item in reversed(scene["objects"]):
        if item["kind"] != "table":
            continue
        children = [native[child["id"]] for child in scene["objects"]
                    if child.get("container_id") == item["id"]]
        if not children:
            continue
        owner = native[item["id"]]
        members = {shape._element for shape in (owner, *children)}
        following = next((node for node in owner._element.itersiblings()
                          if node not in members), None)
        ordered = [shape for shape in slide.shapes if shape._element in members]
        group = slide.shapes.add_group_shape(ordered)
        group.name = item["id"] + ".group"
        if following is not None:
            following.addprevious(group._element)
        native[item["id"]] = group

    output_path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(output_path)


def _set_paragraph_spacing(paragraph: object, style: dict[str, Any], points: object) -> None:
    """Keep explicit CSS leading and tracking in both text boxes and table cells."""

    if "line_height_pt" in style:
        paragraph.line_spacing = points(style["line_height_pt"])
    if "letter_spacing_pt" in style:
        paragraph.font._rPr.set("spc", str(points(style["letter_spacing_pt"]).centipoints))


def _set_maximum_line_spacing(
    paragraph: object, style: dict[str, Any], points: object
) -> None:
    """Keep the largest captured leading for runs sharing one native line owner."""

    if "line_height_pt" not in style:
        return
    current = paragraph.line_spacing
    value = points(style["line_height_pt"])
    if current is None or value > current:
        paragraph.line_spacing = value


def _set_run_font(
    run: object, style: dict[str, Any], rgb_color: object, points: object
) -> None:
    """Apply the existing normalized font fields to one native text run."""

    run.font.name = style["font_face"]
    run.font.size = points(style["font_size_pt"])
    run.font.bold = style["bold"]
    run.font.italic = style["italic"]
    run.font.color.rgb = rgb_color.from_string(style["color"])
    if "letter_spacing_pt" in style:
        run.font._rPr.set("spc", str(points(style["letter_spacing_pt"]).centipoints))


def _set_rounded_border_geometry(
    shape: object, widths: dict[str, float], radius: float
) -> None:
    """Use opposite-winding native contours for a solid rounded border ring."""

    from pptx.util import Inches

    namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}"

    width, height = int(shape.width), int(shape.height)
    radius = min(Inches(radius), width / 2, height / 2)
    top, right, bottom, left = (Inches(widths[side]) for side in ("top", "right", "bottom", "left"))
    geometry = etree.Element(tag("custGeom"))
    for child in ("avLst", "gdLst", "ahLst", "cxnLst"):
        etree.SubElement(geometry, tag(child))
    etree.SubElement(geometry, tag("rect"), l="l", t="t", r="r", b="b")
    path = etree.SubElement(
        etree.SubElement(geometry, tag("pathLst")), tag("path"),
        w=str(width), h=str(height), stroke="false", fill="norm",
    )

    def point(operation: str, x: float, y: float) -> None:
        etree.SubElement(etree.SubElement(path, tag(operation)), tag("pt"),
                         x=str(round(x)), y=str(round(y)))

    def arc(radii: tuple[float, float], angle: int, sweep: int) -> None:
        rx, ry = radii
        if rx and ry:
            etree.SubElement(path, tag("arcTo"), wR=str(round(rx)), hR=str(round(ry)),
                             stAng=str(angle * 60000), swAng=str(sweep * 60000))

    def corners(x0: float, y0: float, x1: float, y1: float,
                radii: list[tuple[float, float]]) -> list[tuple]:
        # A zero radius on either axis makes that inner corner square.
        tl, tr, br, bl = [(rx, ry) if rx and ry else (0, 0) for rx, ry in radii]
        return [
            (tl, (x0, y0 + tl[1]), (x0 + tl[0], y0), 180),
            (tr, (x1 - tr[0], y0), (x1, y0 + tr[1]), 270),
            (br, (x1, y1 - br[1]), (x1 - br[0], y1), 0),
            (bl, (x0 + bl[0], y1), (x0, y1 - bl[1]), 90),
        ]

    def contour(boundary: list[tuple], *, reverse: bool = False) -> None:
        indices = (0, 3, 2, 1) if reverse else (0, 1, 2, 3)
        endpoint = 2 if reverse else 1
        point("moveTo", *boundary[0][endpoint])
        for offset, index in enumerate(indices):
            radii, _entry, _exit, angle = boundary[index]
            arc(radii, angle + (90 if reverse else 0), -90 if reverse else 90)
            point("lnTo", *boundary[indices[(offset + 1) % 4]][endpoint])
        etree.SubElement(path, tag("close"))

    outer = corners(0, 0, width, height, [(radius, radius)] * 4)
    inner = []
    if width > left + right and height > top + bottom:
        inner_radii = [(max(0, radius - horizontal), max(0, radius - vertical))
                       for horizontal, vertical in ((left, top), (right, top),
                                                    (right, bottom), (left, bottom))]
        inner = corners(left, top, width - right, height - bottom, inner_radii)
    edge_widths = (top, right, bottom, left)
    if not inner or all(edge_widths):
        contour(outer)
        if inner:
            contour(inner, reverse=True)
    else:
        # Close only painted runs. Coincident outer/inner paths on zero-width
        # edges otherwise leave antialias hairlines in native Office viewers.
        for start in range(4):
            if not edge_widths[start] or edge_widths[(start - 1) % 4]:
                continue
            indices = [start]
            while edge_widths[indices[-1]]:
                indices.append((indices[-1] + 1) % 4)
            point("moveTo", *outer[start][1])
            for index in indices:
                radii, entry, _exit, angle = outer[index]
                point("lnTo", *entry)
                arc(radii, angle, 90)
            for index in reversed(indices):
                radii, _entry, exit_point, angle = inner[index]
                point("lnTo", *exit_point)
                arc(radii, angle + 90, -90)
            etree.SubElement(path, tag("close"))
    properties = shape._element.spPr
    properties.replace(properties.prstGeom, geometry)


def _set_cell_borders(cell: object, borders: dict[str, object], points: object) -> None:
    """Override theme gridlines with the captured edges, including absent lines."""

    namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
    properties = cell._tc.get_or_add_tcPr()
    for side, suffix in reversed(
        (("left", "L"), ("right", "R"), ("top", "T"), ("bottom", "B"))
    ):
        border = borders[side]
        line = etree.Element(
            f"{{{namespace}}}ln{suffix}", w=str(points(border["width_pt"]))
        )
        fill = etree.SubElement(
            line, f"{{{namespace}}}{'solidFill' if border['width_pt'] else 'noFill'}"
        )
        if border["width_pt"]:
            etree.SubElement(fill, f"{{{namespace}}}srgbClr", val=border["color"])
        properties.insert(0, line)


def _remove_theme_shape_style(shape: object) -> None:
    """Keep scene shapes free from implicit theme effects such as outer shadows."""

    element = shape._element
    for child in list(element):
        if etree.QName(child).localname == "style":
            element.remove(child)


def _add_native_equation(
    slide: object,
    item: dict[str, object],
    geometry: tuple[object, object, object, object],
    alignments: dict[str, object],
    rgb_color: object,
    vertical_anchor: object,
    points: object,
) -> None:
    """Add one editable Office Math object."""

    native_shape = slide.shapes.add_textbox(*geometry)
    native_shape.name = str(item["id"])
    text_frame = native_shape.text_frame
    text_frame.clear()
    text_frame.margin_left = 0
    text_frame.margin_right = 0
    text_frame.margin_top = 0
    text_frame.margin_bottom = 0
    text_frame.vertical_anchor = vertical_anchor.MIDDLE
    text_frame.word_wrap = False
    paragraph = text_frame.paragraphs[0]
    paragraph.alignment = alignments[str(item["align"])]
    paragraph.space_after = points(0)
    paragraph.font.name = str(item["font_face"])
    paragraph.font.size = points(float(item["font_size_pt"]))
    paragraph.font.color.rgb = rgb_color.from_string(str(item["color"]))
    paragraph_element = paragraph._p
    for child in list(paragraph_element):
        if etree.QName(child).localname != "pPr":
            paragraph_element.remove(child)
    equation_element = etree.fromstring(
        build_text_math_xml(str(item["mathml"]), align=str(item["align"])).encode()
    )
    paragraph_element.append(equation_element)


def _image_source(
    value: str,
    base_dir: Path,
    *,
    width_in: float,
    height_in: float,
) -> Path | BytesIO:
    """Resolve one scene image without network access."""

    mime = ""
    if value.startswith("data:"):
        header, separator, payload = value.partition(",")
        if not separator or ";base64" not in header:
            raise ValueError("PPTX image data URI must be base64 encoded")
        mime = header[5:].split(";", 1)[0].lower()
        raw = base64.b64decode(payload, validate=True)
        return (
            _render_svg(raw, width_in=width_in, height_in=height_in)
            if mime == "image/svg+xml"
            else BytesIO(raw)
        )
    if value.startswith("file://"):
        path = Path(unquote(urlparse(value).path)).resolve(strict=True)
    else:
        path = (base_dir / value).resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"PPTX image is not a regular file: {path}")
    return (
        _render_svg(path.read_bytes(), width_in=width_in, height_in=height_in)
        if path.suffix.lower() == ".svg"
        else path
    )


def _svg_target_pixels(width_in: float, height_in: float) -> tuple[int, int]:
    """Return a 300-PPI placement raster within one bounded memory budget."""

    if (
        not math.isfinite(width_in)
        or not math.isfinite(height_in)
        or width_in <= 0
        or height_in <= 0
    ):
        raise ValueError("SVG placement dimensions must be positive finite inches")
    width = max(1, math.ceil(width_in * _SVG_TARGET_PPI))
    height = max(1, math.ceil(height_in * _SVG_TARGET_PPI))
    limit_scale = min(
        1.0,
        _SVG_MAX_LONG_EDGE_PIXELS / width,
        _SVG_MAX_LONG_EDGE_PIXELS / height,
        math.sqrt(_SVG_MAX_PIXELS / (width * height)),
    )
    if limit_scale < 1:
        width = max(1, math.floor(width * limit_scale))
        height = max(1, math.floor(height * limit_scale))
    return width, height


def _render_svg(raw: bytes, *, width_in: float, height_in: float) -> BytesIO:
    """Rasterize an SVG at its final PowerPoint placement size."""

    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required to export SVG figures") from exc
    document = pymupdf.open(stream=raw, filetype="svg")
    try:
        page = document[0]
        if page.rect.width <= 0 or page.rect.height <= 0:
            raise ValueError("SVG dimensions must be positive")
        target_width, target_height = _svg_target_pixels(width_in, height_in)
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(
                target_width / page.rect.width,
                target_height / page.rect.height,
            ),
            alpha=True,
        )
        return BytesIO(pixmap.tobytes("png"))
    finally:
        document.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path)
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        render_scene(args.scene.resolve(strict=True), args.output.resolve())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
