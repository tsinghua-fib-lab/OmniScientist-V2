"""Canonical editable-PPTX scene contract for scientific posters."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from posterlib.runtime.identifiers import is_hex, is_identifier

SCHEMA = "scientific-poster.pptx-scene.v3"
OBJECT_KINDS = frozenset({"component", "equation", "image", "shape", "table", "text"})


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
    for item in objects:
        container_id = item.get("container_id")
        if container_id is not None and (
            container_id == item["id"] or container_id not in ids
        ):
            raise SceneError(
                "invalid_container_id",
                f"{item['id']}.container_id must name another scene object",
            )
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
    if not is_identifier(object_id):
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
    coordinate = _number if kind == "shape" and "rotation" in raw else _nonnegative_number
    item: dict[str, Any] = {
        "id": object_id,
        "kind": kind,
        "role": str(raw.get("role") or kind).strip(),
        # A rotated shape's unrotated frame can start outside its painted bounds.
        "x": coordinate(raw.get("x"), f"{object_id}.x"),
        "y": coordinate(raw.get("y"), f"{object_id}.y"),
        "w": _positive_number(raw.get("w"), f"{object_id}.w"),
        "h": _positive_number(raw.get("h"), f"{object_id}.h"),
    }
    module_id = str(raw.get("module_id") or "").strip()
    if module_id:
        item["module_id"] = module_id
    container_id = str(raw.get("container_id") or "").strip()
    if container_id:
        if not is_identifier(container_id):
            raise SceneError(
                "invalid_container_id",
                f"{object_id}.container_id must be a stable identifier",
            )
        item["container_id"] = container_id
    if kind == "text":
        item.update(_normalize_text(raw, object_id))
    elif kind == "equation":
        item.update(_normalize_equation(raw, object_id))
    elif kind in {"image", "component"}:
        src = str(raw.get("src") or "").strip()
        if not src:
            raise SceneError("missing_image_source", f"{object_id}.src is required")
        item.update({"src": src, "alt": str(raw.get("alt") or "").strip()})
        if kind == "image" and raw.get("source_figure_sha256"):
            item["source_figure_sha256"] = str(raw["source_figure_sha256"]).strip()
    elif kind == "shape":
        if "rotation" in raw:
            item["rotation"] = _number(raw["rotation"], f"{object_id}.rotation") % 360
        if "flip_v" in raw:
            item["flip_v"] = _boolean(raw["flip_v"], f"{object_id}.flip_v")
        fill_alpha = _number(raw.get("fill_alpha", 1), f"{object_id}.fill_alpha")
        if not 0 <= fill_alpha <= 1:
            raise SceneError("invalid_shape_style", f"{object_id}.fill_alpha must be between 0 and 1")
        item.update(
            {
                "fill": _color(raw.get("fill", "FFFFFF"), f"{object_id}.fill"),
                "fill_alpha": fill_alpha,
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
        if "border_widths" in raw:
            widths = _mapping(raw["border_widths"], f"{object_id}.border_widths")
            item["border_widths"] = {
                side: _nonnegative_number(widths.get(side), f"{object_id}.border_widths.{side}")
                for side in ("top", "right", "bottom", "left")
            }
        if "border_side" in raw:
            side = raw["border_side"]
            if "border_widths" not in item or side not in ("top", "right", "bottom", "left"):
                raise SceneError(
                    "invalid_shape_style", f"{object_id}.border_side requires border widths and a valid side"
                )
            item["border_side"] = side
        if "path" in raw:
            operations = {"M": 2, "L": 2, "C": 6, "Z": 0}
            path = raw["path"]
            if not isinstance(path, list) or not path:
                raise SceneError("invalid_shape_path", f"{object_id}.path must contain drawing commands")
            item["path"] = []
            for command in path:
                if (not isinstance(command, list) or not command
                        or command[0] not in operations or len(command) != operations[command[0]] + 1):
                    raise SceneError("invalid_shape_path", f"{object_id}.path contains an invalid command")
                item["path"].append([command[0], *(_number(value, f"{object_id}.path") for value in command[1:])])
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
        try:
            cell_styles = _normalize_table_styles(raw, normalized_rows, object_id)
        except (IndexError, KeyError, TypeError, AttributeError) as exc:
            raise SceneError(
                "invalid_table_styles",
                f"{object_id}.cell_styles must contain cell style objects matching rows",
            ) from exc
        cell_runs = _normalize_table_runs(raw, normalized_rows, object_id)
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
                "cell_styles": cell_styles,
                "cell_runs": cell_runs,
                "merges": _normalize_table_merges(raw.get("merges", []), normalized_rows, object_id),
            }
        )
    return item


def _normalize_font_style(raw: Mapping[str, Any], object_id: str) -> dict[str, Any]:
    result = {
        "font_size_pt": _positive_number(
            raw.get("font_size_pt"), f"{object_id}.font_size_pt"
        ),
        "font_face": str(raw.get("font_face") or "Arial").strip(),
        "color": _color(raw.get("color", "111111"), f"{object_id}.color"),
        "bold": raw.get("bold") is True,
        "italic": raw.get("italic") is True,
    }
    if "letter_spacing_pt" in raw:
        result["letter_spacing_pt"] = _number(raw["letter_spacing_pt"], f"{object_id}.letter_spacing_pt")
    if "line_height_pt" in raw:
        result["line_height_pt"] = _nonnegative_number(raw["line_height_pt"], f"{object_id}.line_height_pt")
    return result


def _normalize_table_merges(
    value: object, rows: list[list[str]], object_id: str
) -> list[dict[str, int]]:
    """Keep native cell spans within the rectangular grid without losing text."""

    label = f"{object_id}.merges"
    if not isinstance(value, list):
        raise SceneError("invalid_table_merge", f"{label} must be an array")
    merges = []
    covered: set[tuple[int, int]] = set()
    for index, item in enumerate(value):
        raw = _mapping(item, f"{label}[{index}]")
        span = {}
        for field, minimum in (("row", 0), ("column", 0), ("row_span", 1), ("column_span", 1)):
            number = raw.get(field)
            if type(number) is not int or number < minimum:
                raise SceneError("invalid_table_merge", f"{label}[{index}].{field} is invalid")
            span[field] = number
        row, column = span["row"], span["column"]
        bottom, right = row + span["row_span"], column + span["column_span"]
        if bottom > len(rows) or right > len(rows[0]):
            raise SceneError("invalid_table_merge", f"{label}[{index}] extends outside the grid")
        for r in range(row, bottom):
            for c in range(column, right):
                if (r, c) in covered or ((r, c) != (row, column) and rows[r][c]):
                    raise SceneError("invalid_table_merge", f"{label}[{index}] overlaps cells or hides text")
                covered.add((r, c))
        merges.append(span)
    return merges


def _normalize_table_styles(
    raw: Mapping[str, Any], rows: list[list[str]], object_id: str
) -> list[list[dict[str, Any]]]:
    """Validate the captured style for every native table cell."""

    styles = raw.get("cell_styles")
    if (
        not isinstance(styles, Sequence)
        or isinstance(styles, (str, bytes))
        or len(styles) != len(rows)
    ):
        raise SceneError(
            "invalid_table_styles",
            f"{object_id}.cell_styles must match the table rows",
        )
    result: list[list[dict[str, Any]]] = []
    for row_index, row in enumerate(rows):
        row_styles = styles[row_index]
        if (
            not isinstance(row_styles, Sequence)
            or isinstance(row_styles, (str, bytes))
            or len(row_styles) != len(row)
        ):
            raise SceneError(
                "invalid_table_styles",
                f"{object_id}.cell_styles[{row_index}] must match the table columns",
            )
        output_row = []
        for column_index, _ in enumerate(row):
            label = f"{object_id}.cell_styles[{row_index}][{column_index}]"
            style = dict(_mapping(row_styles[column_index], label))
            padding = style.get("padding", {})
            borders = style.get("borders", {})
            output_row.append(
                {
                    **_normalize_font_style(style, label),
                    "fill": _color(style.get("fill", "FFFFFF"), f"{label}.fill"),
                    "fill_enabled": _boolean(
                        style.get("fill_enabled", True), f"{label}.fill_enabled"
                    ),
                    "align": style.get("align")
                    if style.get("align") in {"center", "right"}
                    else "left",
                    "word_wrap": _boolean(
                        style.get("word_wrap", True), f"{label}.word_wrap"
                    ),
                    "vertical_align": style.get("vertical_align")
                    if style.get("vertical_align") in {"top", "bottom"}
                    else "middle",
                    "padding": {
                        side: _nonnegative_number(
                            padding.get(side, 0.04), f"{label}.padding.{side}"
                        )
                        for side in ("top", "right", "bottom", "left")
                    },
                    "borders": {
                        side: {
                            "color": _color(
                                borders.get(side, {}).get(
                                    "color", raw.get("line", "D8D8D8")
                                ),
                                f"{label}.borders.{side}.color",
                            ),
                            "width_pt": _nonnegative_number(
                                borders.get(side, {}).get(
                                    "width_pt", 0.5 if styles is None else 0
                                ),
                                f"{label}.borders.{side}.width_pt",
                            ),
                        }
                        for side in ("top", "right", "bottom", "left")
                    },
                }
            )
        result.append(output_row)
    return result


def _normalize_table_runs(
    raw: Mapping[str, Any], rows: list[list[str]], object_id: str
) -> list[list[list[dict[str, Any]]]]:
    """Keep optional origin-cell text runs exact and ordered."""

    values = raw.get("cell_runs")
    if values is None:
        return [[[] for _ in row] for row in rows]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise SceneError("invalid_table_runs", f"{object_id}.cell_runs must be an array")
    if len(values) != len(rows):
        raise SceneError("invalid_table_runs", f"{object_id}.cell_runs must match rows")
    result = []
    for row_index, row in enumerate(rows):
        source_row = values[row_index]
        if not isinstance(source_row, Sequence) or isinstance(source_row, (str, bytes)):
            raise SceneError("invalid_table_runs", f"{object_id}.cell_runs[{row_index}] must be an array")
        if len(source_row) != len(row):
            raise SceneError("invalid_table_runs", f"{object_id}.cell_runs must match rows")
        output_row = []
        for column_index, text in enumerate(row):
            source_runs = source_row[column_index]
            label = f"{object_id}.cell_runs[{row_index}][{column_index}]"
            if not isinstance(source_runs, Sequence) or isinstance(source_runs, (str, bytes)):
                raise SceneError("invalid_table_runs", f"{label} must be an array")
            runs = []
            for run_index, value in enumerate(source_runs):
                run_label = f"{label}[{run_index}]"
                run = _mapping(value, run_label)
                run_text = str(run.get("text") or "")
                if not run_text:
                    raise SceneError("invalid_table_runs", f"{run_label}.text must not be empty")
                runs.append({"text": run_text, **_normalize_font_style(run, run_label)})
            if runs and "".join(run["text"] for run in runs) != text:
                raise SceneError("invalid_table_runs", f"{label} must reproduce the cell text")
            output_row.append(runs)
        result.append(output_row)
    return result


def _normalize_text(raw: Mapping[str, Any], object_id: str) -> dict[str, Any]:
    text = str(raw.get("text") or "")
    if not text.strip():
        raise SceneError("empty_text", f"{object_id}.text must not be empty")
    align = str(raw.get("align") or "left").lower()
    if align not in {"center", "left", "right"}:
        align = "left"
    fit = str(raw.get("fit") or "grow").lower()
    if fit not in {"grow", "shrink", "none"}:
        raise SceneError(
            "invalid_text_fit",
            f"{object_id}.fit must be grow, shrink or none",
        )
    result = {
        "text": text,
        **_normalize_font_style(raw, object_id),
        "align": align,
        "fit": fit,
        "word_wrap": _boolean(raw.get("word_wrap", True), f"{object_id}.word_wrap"),
        "font_preflighted": _boolean(
            raw.get("font_preflighted", False),
            f"{object_id}.font_preflighted",
        ),
    }
    return result


def _normalize_equation(raw: Mapping[str, Any], object_id: str) -> dict[str, Any]:
    mathml = str(raw.get("mathml") or "").strip()
    flow = raw.get("runs")
    if flow is None and not mathml.startswith("<math"):
        raise SceneError(
            "invalid_equation_mathml", f"{object_id}.mathml must start with <math"
        )
    align = str(raw.get("align") or "center").lower()
    if align not in {"center", "left", "right"}:
        align = "center"
    result = {
        "mathml": mathml,
        "latex": str(raw.get("latex") or "").strip(),
        "font_size_pt": _positive_number(
            raw.get("font_size_pt"), f"{object_id}.font_size_pt"
        ),
        "font_face": str(raw.get("font_face") or "Cambria Math").strip(),
        "color": _color(raw.get("color", "111111"), f"{object_id}.color"),
        "align": align,
    }
    if "normal_text_sizes_pt" in raw:
        sizes = raw["normal_text_sizes_pt"]
        if not isinstance(sizes, list):
            raise SceneError("invalid_equation_text_sizes", f"{object_id}.normal_text_sizes_pt must be an array")
        result["normal_text_sizes_pt"] = [
            _positive_number(size, f"{object_id}.normal_text_sizes_pt[{index}]")
            for index, size in enumerate(sizes)
        ]
    if flow is not None:
        if not isinstance(flow, list) or not flow:
            raise SceneError("invalid_equation_runs", f"{object_id}.runs must be a nonempty array")
        runs = []
        for index, value in enumerate(flow):
            label = f"{object_id}.runs[{index}]"
            run = _mapping(value, label)
            if "mathml" in run:
                runs.append(_normalize_equation({key: value for key, value in run.items() if key != "runs"}, label))
            else:
                text = str(run.get("text") or "")
                if not text:
                    raise SceneError("invalid_equation_runs", f"{label}.text must not be empty")
                runs.append({"text": text, **_normalize_font_style(run, label)})
        result["runs"] = runs
        result["rtl"] = _boolean(raw.get("rtl", False), f"{object_id}.rtl")
    return result


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
        _positive_number(item, f"{label}[{index}]") for index, item in enumerate(value)
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
    if not is_hex(color.lower(), 6):
        raise SceneError("invalid_color", f"{label} must be a six-digit hex color")
    return color
