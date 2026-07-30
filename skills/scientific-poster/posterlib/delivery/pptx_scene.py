"""Canonical editable-PPTX scene contract for scientific posters."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA = "scientific-poster.pptx-scene.v2"
OBJECT_KINDS = frozenset({"equation", "image", "shape", "table", "text"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_COLOR_RE = re.compile(r"^[0-9A-F]{6}$")


class SceneError(ValueError):
    """The captured poster cannot be represented by the scene contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_scene(value: object) -> dict[str, Any]:
    """Validate and normalize a browser-captured editable poster scene."""

    raw = _mapping(value, "scene")
    if raw.get("schema") != SCHEMA:
        raise SceneError("invalid_scene_schema", f"scene schema must be {SCHEMA}")
    page = _normalize_page(raw.get("page"))
    objects_raw = raw.get("objects")
    if not isinstance(objects_raw, Sequence) or isinstance(objects_raw, (str, bytes)):
        raise SceneError("invalid_scene_objects", "scene objects must be an array")
    objects: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(objects_raw):
        normalized = _normalize_object(item, index=index)
        object_id = normalized["id"]
        if object_id in ids:
            raise SceneError(
                "duplicate_object_id",
                f"duplicate object id: {object_id}",
            )
        ids.add(object_id)
        objects.append(normalized)
    if not objects:
        raise SceneError("empty_scene", "scene must contain at least one object")
    return {"schema": SCHEMA, "page": page, "objects": objects}


def _normalize_page(value: object) -> dict[str, Any]:
    raw = _mapping(value, "page")
    width = _positive_number(raw.get("width_in"), "page.width_in")
    height = _positive_number(raw.get("height_in"), "page.height_in")
    if width > 56 or height > 56:
        raise SceneError(
            "page_too_large",
            "PowerPoint page dimensions must not exceed 56 inches",
        )
    return {
        "width_in": width,
        "height_in": height,
        "background": _color(raw.get("background", "FFFFFF"), "page.background"),
    }


def _normalize_object(value: object, *, index: int) -> dict[str, Any]:
    raw = _mapping(value, f"objects[{index}]")
    object_id = str(raw.get("id") or "").strip()
    if not _ID_RE.fullmatch(object_id):
        raise SceneError(
            "invalid_object_id",
            f"objects[{index}].id must be a stable identifier",
        )
    kind = str(raw.get("kind") or "").strip()
    if kind not in OBJECT_KINDS:
        raise SceneError(
            "invalid_object_kind",
            f"{object_id} has unsupported kind: {kind or '<empty>'}",
        )
    item: dict[str, Any] = {
        "id": object_id,
        "kind": kind,
        "role": str(raw.get("role") or kind).strip(),
        "x": _nonnegative_number(raw.get("x"), f"{object_id}.x"),
        "y": _nonnegative_number(raw.get("y"), f"{object_id}.y"),
        "w": _positive_number(raw.get("w"), f"{object_id}.w"),
        "h": _positive_number(raw.get("h"), f"{object_id}.h"),
    }
    module_id = str(raw.get("module_id") or "").strip()
    if module_id:
        item["module_id"] = module_id
    if kind == "text":
        item.update(_normalize_text(raw, object_id))
    elif kind == "equation":
        item.update(_normalize_equation(raw, object_id))
    elif kind == "image":
        src = str(raw.get("src") or "").strip()
        if not src:
            raise SceneError("missing_image_source", f"{object_id}.src is required")
        item.update({"src": src, "alt": str(raw.get("alt") or "").strip()})
    elif kind == "shape":
        item.update(
            {
                "fill": _color(raw.get("fill", "FFFFFF"), f"{object_id}.fill"),
                "fill_enabled": _boolean(
                    raw.get("fill_enabled", True), f"{object_id}.fill_enabled"
                ),
                "line": _color(raw.get("line", "FFFFFF"), f"{object_id}.line"),
                "line_width_pt": _nonnegative_number(
                    raw.get("line_width_pt", 0), f"{object_id}.line_width_pt"
                ),
                "radius": _nonnegative_number(
                    raw.get("radius", 0), f"{object_id}.radius"
                ),
            }
        )
    else:
        rows = raw.get("rows")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
            raise SceneError(
                "invalid_table_rows", f"{object_id}.rows must be a non-empty array"
            )
        normalized_rows: list[list[str]] = []
        width: int | None = None
        for row in rows:
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                raise SceneError(
                    "invalid_table_rows", f"{object_id}.rows must contain arrays"
                )
            cells = [str(cell) for cell in row]
            width = width if width is not None else len(cells)
            if not cells or len(cells) != width:
                raise SceneError(
                    "invalid_table_rows", f"{object_id}.rows must be rectangular"
                )
            normalized_rows.append(cells)
        item.update(
            {
                "rows": normalized_rows,
                "row_heights": _normalize_lengths(
                    raw.get("row_heights"),
                    count=len(normalized_rows),
                    total=item["h"],
                    label=f"{object_id}.row_heights",
                ),
                "column_widths": _normalize_lengths(
                    raw.get("column_widths"),
                    count=width,
                    total=item["w"],
                    label=f"{object_id}.column_widths",
                ),
                "font_size_pt": _positive_number(
                    raw.get("font_size_pt"), f"{object_id}.font_size_pt"
                ),
                "font_face": str(raw.get("font_face") or "Arial").strip(),
                "color": _color(raw.get("color", "111111"), f"{object_id}.color"),
                "fill": _color(raw.get("fill", "FFFFFF"), f"{object_id}.fill"),
                "line": _color(raw.get("line", "D8D8D8"), f"{object_id}.line"),
                "header_fill": _color(
                    raw.get("header_fill", raw.get("fill", "FFFFFF")),
                    f"{object_id}.header_fill",
                ),
                "header_color": _color(
                    raw.get("header_color", raw.get("color", "111111")),
                    f"{object_id}.header_color",
                ),
                "body_fill": _color(
                    raw.get("body_fill", raw.get("fill", "FFFFFF")),
                    f"{object_id}.body_fill",
                ),
                "body_color": _color(
                    raw.get("body_color", raw.get("color", "111111")),
                    f"{object_id}.body_color",
                ),
            }
        )
    return item


def _normalize_text(raw: Mapping[str, Any], object_id: str) -> dict[str, Any]:
    text = str(raw.get("text") or "").strip()
    if not text:
        raise SceneError("empty_text", f"{object_id}.text must not be empty")
    align = str(raw.get("align") or "left").lower()
    if align not in {"center", "left", "right"}:
        align = "left"
    fit = str(raw.get("fit") or "grow").lower()
    if fit not in {"grow", "shrink"}:
        raise SceneError(
            "invalid_text_fit",
            f"{object_id}.fit must be grow or shrink",
        )
    font_scale = _positive_number(raw.get("font_scale", 1), f"{object_id}.font_scale")
    if font_scale > 1:
        raise SceneError(
            "invalid_text_scale",
            f"{object_id}.font_scale must not exceed 1",
        )
    return {
        "text": text,
        "font_size_pt": _positive_number(
            raw.get("font_size_pt"), f"{object_id}.font_size_pt"
        ),
        "font_face": str(raw.get("font_face") or "Arial").strip(),
        "color": _color(raw.get("color", "111111"), f"{object_id}.color"),
        "bold": raw.get("bold") is True,
        "italic": raw.get("italic") is True,
        "align": align,
        "fit": fit,
        "word_wrap": _boolean(raw.get("word_wrap", True), f"{object_id}.word_wrap"),
        "font_preflighted": _boolean(
            raw.get("font_preflighted", False),
            f"{object_id}.font_preflighted",
        ),
        "font_scale": font_scale,
    }


def _normalize_equation(raw: Mapping[str, Any], object_id: str) -> dict[str, Any]:
    mathml = str(raw.get("mathml") or "").strip()
    if not mathml.startswith("<math"):
        raise SceneError(
            "invalid_equation_mathml", f"{object_id}.mathml must start with <math"
        )
    fallback_src = str(raw.get("fallback_src") or "").strip()
    if not fallback_src:
        raise SceneError(
            "missing_equation_fallback",
            f"{object_id}.fallback_src is required for non-PowerPoint viewers",
        )
    align = str(raw.get("align") or "center").lower()
    if align not in {"center", "left", "right"}:
        align = "center"
    return {
        "mathml": mathml,
        "latex": str(raw.get("latex") or "").strip(),
        "fallback_src": fallback_src,
        "font_size_pt": _positive_number(
            raw.get("font_size_pt"), f"{object_id}.font_size_pt"
        ),
        "font_face": str(raw.get("font_face") or "Cambria Math").strip(),
        "color": _color(raw.get("color", "111111"), f"{object_id}.color"),
        "align": align,
    }


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SceneError("invalid_scene", f"{label} must be an object")
    return value


def _positive_number(value: object, label: str) -> float:
    number = _number(value, label)
    if number <= 0:
        raise SceneError("invalid_geometry", f"{label} must be positive")
    return number


def _nonnegative_number(value: object, label: str) -> float:
    number = _number(value, label)
    if number < 0:
        raise SceneError("invalid_geometry", f"{label} must be non-negative")
    return number


def _normalize_lengths(
    value: object,
    *,
    count: int,
    total: float,
    label: str,
) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SceneError("invalid_table_geometry", f"{label} must be an array")
    if len(value) != count:
        raise SceneError(
            "invalid_table_geometry",
            f"{label} must contain exactly {count} values",
        )
    lengths = [
        _positive_number(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    ]
    scale = total / sum(lengths)
    return [length * scale for length in lengths]


def _number(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SceneError("invalid_geometry", f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise SceneError("invalid_geometry", f"{label} must be finite")
    return number


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise SceneError("invalid_shape_style", f"{label} must be boolean")
    return value


def _color(value: object, label: str) -> str:
    color = str(value or "").strip().removeprefix("#").upper()
    if not _COLOR_RE.fullmatch(color):
        raise SceneError("invalid_color", f"{label} must be a six-digit hex color")
    return color
