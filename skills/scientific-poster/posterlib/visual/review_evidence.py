"""Ground visual review in measurements from the exact rendered poster."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from posterlib.runtime.identifiers import is_sha256

MAX_CROPS = 3
OVERVIEW_MAX_EDGE_PX = 1_920
_DETAIL_PRIORITY_ORDER = ("focal", "primary", "supporting", "footer")
_ATLAS_GAP_PX = 24
_ATLAS_LABEL_HEIGHT_PX = 54
# Fit all detail tiles into one existing 1920 x 2160 montage pane.
_ATLAS_TILE_SIZE = (
    OVERVIEW_MAX_EDGE_PX - 2 * _ATLAS_GAP_PX,
    (2_160 - _ATLAS_GAP_PX) // MAX_CROPS - _ATLAS_LABEL_HEIGHT_PX - _ATLAS_GAP_PX,
)


def file_sha256(path: Path) -> str:
    """Return the SHA-256 identity of an immutable visual input."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_evidence(
    *,
    report: dict[str, Any],
    screenshot_path: Path,
    out_dir: Path,
) -> tuple[dict[str, Any], Path]:
    """Persist a compact, factual evidence bundle for downstream visual judgment."""

    screenshot_path = screenshot_path.resolve()
    out_dir = out_dir.resolve()
    poster = report.get("poster")
    poster = poster if isinstance(poster, dict) else {}
    width = _number(poster.get("width"))
    height = _number(poster.get("height"))
    if width is None or height is None:
        raise ValueError("visual evidence requires positive poster dimensions")
    width_mm = _number(poster.get("width_mm")) or width * 25.4 / 96
    height_mm = _number(poster.get("height_mm")) or height * 25.4 / 96
    if not screenshot_path.is_file():
        raise FileNotFoundError(screenshot_path)

    text_runs = _records(report.get("text_runs"))
    visual_targets = _records(report.get("visual_targets"))
    occupancy_targets = [
        item
        for item in visual_targets
        if str(item.get("kind") or "")
        in {"img", "svg", "canvas", "table", "math", "pre", "figure"}
    ]
    content_rects: list[dict[str, float]] = []
    content_rects_by_module: dict[str, list[dict[str, float]]] = {}
    for item in (*text_runs, *occupancy_targets):
        item_rects = _item_rects(item)
        module_id = str(item.get("module_id") or "").strip()
        if module_id:
            content_rects_by_module.setdefault(module_id, []).extend(item_rects)
        else:
            content_rects.extend(item_rects)
    for module in _records(report.get("modules")):
        internal = module.get("internal_space")
        if isinstance(internal, dict):
            bounds = _rect(internal.get("content_bounds_px"))
            content_rects_by_module[str(module.get("module_id") or "")] = (
                [bounds] if bounds is not None else []
            )
    content_rects.extend(rect for values in content_rects_by_module.values() for rect in values)
    space_observations = _space_observations(
        report=report,
        page_width=width,
        page_height=height,
        page_width_mm=width_mm,
        page_height_mm=height_mm,
        content_rects=content_rects,
        content_rects_by_module=content_rects_by_module,
    )
    figures = _figure_observations(report, width, height, width_mm, height_mm)
    observations = _select_observations(
        space_observations=space_observations,
        detail_observations=[
            *figures,
            *_native_observations(report, space_observations, figures, width, height, width_mm, height_mm),
        ],
        typography=_typography_observations(report),
    )
    observations.extend(_rendered_copy_observations(text_runs))

    overview = {
        "path": str(screenshot_path),
        "sha256": file_sha256(screenshot_path),
        "mime_type": "image/png",
    }
    crops = _write_crops(
        screenshot_path=screenshot_path,
        out_dir=out_dir,
        observations=observations,
        page_width=width,
        page_height=height,
    )
    atlas = _write_atlas(crops=crops, out_dir=out_dir)
    unsigned = {
        "schema": "scientific-poster.visual-evidence.v1",
        "overview": overview,
        "observations": observations,
        "crops": crops,
        "atlas": atlas,
    }
    bundle = {**unsigned, "bundle_sha256": _document_sha256(unsigned)}
    path = out_dir / "visual-evidence.json"
    path.write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return bundle, path


def _select_observations(
    *,
    space_observations: list[dict[str, Any]],
    detail_observations: list[dict[str, Any]],
    typography: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep measured carriers available to the shared detail selection."""

    return [
        *typography,
        *space_observations,
        *sorted(detail_observations, key=_detail_order),
    ]


def _rendered_copy_observations(text_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group browser-visible text fragments with their painted locations and owners."""

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for run in text_runs:
        text = run.get("text")
        rects = [
            dict(rect) for rect in _records(run.get("rects"))
            if _number(rect.get("width")) is not None
            and _number(rect.get("height")) is not None
        ]
        if not isinstance(text, str) or not text.strip() or not rects:
            continue
        module_id = str(run.get("module_id") or "").strip()
        poster_id = str(run.get("poster_id") or "").strip()
        owner = (module_id, "" if module_id else poster_id)
        if owner not in groups:
            identity = {"module_id": owner[0], "poster_id": owner[1]}
            groups[owner] = {
                "id": f"copy:{len(groups) + 1}",
                "kind": "rendered_copy",
                **identity,
                "facts": {"runs": []},
            }
        groups[owner]["facts"]["runs"].append({
            "text": text, "poster_id": poster_id, "rects": rects,
        })
    return list(groups.values())


def _space_observations(
    *,
    report: dict[str, Any],
    page_width: float,
    page_height: float,
    page_width_mm: float,
    page_height_mm: float,
    content_rects: list[dict[str, float]],
    content_rects_by_module: dict[str, list[dict[str, float]]],
) -> list[dict[str, Any]]:
    """Expose measured extents without inferring lanes or classifying empty space."""

    content_bottom = max((_bottom(rect) for rect in content_rects), default=0.0)
    trailing = max(0.0, page_height - content_bottom)
    observations = [
        {
            "id": "space:page-trailing",
            "kind": "page_trailing_space",
            "salience": trailing / page_height,
            "rect": {
                "left": 0.0,
                "top": min(page_height, content_bottom),
                "width": page_width,
                "height": trailing,
            },
            "facts": {
                "content_bottom_px": content_bottom,
                "trailing_space_px": trailing,
                "trailing_space_mm": trailing * page_height_mm / page_height,
                "trailing_space_ratio": trailing / page_height,
            },
        }
    ]
    for module in _records(report.get("modules")):
        frame = _rect(module.get("rect"))
        module_id = str(module.get("module_id") or "").strip()
        if frame is None or not module_id:
            continue
        rectangles = content_rects_by_module.get(module_id, [])
        content_bounds = None
        if rectangles:
            left = min(rect["left"] for rect in rectangles)
            top = min(rect["top"] for rect in rectangles)
            content_bounds = {
                "left": left,
                "top": top,
                "width": max(_right(rect) for rect in rectangles) - left,
                "height": max(_bottom(rect) for rect in rectangles) - top,
            }
        observations.append({
            "id": "module:" + _document_sha256({"module_id": module_id}),
            "kind": "module_content_extent",
            "module_id": module_id,
            "salience": frame["width"] * frame["height"] / (page_width * page_height),
            "rect": frame,
            "facts": {
                "content_bounds_px": content_bounds,
                "module_width_mm": frame["width"] * page_width_mm / page_width,
                "module_height_mm": frame["height"] * page_height_mm / page_height,
                **({"internal_space": module["internal_space"]}
                   if isinstance(module.get("internal_space"), dict) else {}),
            },
        })
    return observations


def _figure_observations(
    report: dict[str, Any],
    page_width: float,
    page_height: float,
    page_width_mm: float,
    page_height_mm: float,
) -> list[dict[str, Any]]:
    source = report.get("source_figures")
    details = _records(source.get("details")) if isinstance(source, dict) else []
    identities = [
        digest[:12] if is_sha256(digest) else str(index)
        for index, item in enumerate(details)
        for digest in [str(item.get("sha256") or "")]
    ]
    identity_counts = Counter(identities)
    observations = []
    for index, item in enumerate(details):
        painted_rect = _rect(item.get("painted_rect") or item.get("rect"))
        if painted_rect is None:
            continue
        image_box_rect = _rect(item.get("rect")) or painted_rect
        digest = str(item.get("sha256") or "")
        identity = identities[index]
        placement = f":{index + 1}" if identity_counts[identity] > 1 else ""
        area_ratio = (
            painted_rect["width"]
            * painted_rect["height"]
            / (page_width * page_height)
        )
        facts = {
            "sha256": digest,
            "usable": item.get("usable") is True,
            "inside_poster": item.get("inside_poster") is True,
            "clipped": item.get("clipped") is True,
            "painted_width_mm": _number(item.get("painted_width_mm")),
            "painted_height_mm": painted_rect["height"] * page_height_mm / page_height,
            "image_box_width_mm": (
                image_box_rect["width"] * page_width_mm / page_width
            ),
            "image_box_height_mm": (
                image_box_rect["height"] * page_height_mm / page_height
            ),
            "painted_width_fraction_of_box": (
                painted_rect["width"] / image_box_rect["width"]
            ),
            "painted_height_fraction_of_box": (
                painted_rect["height"] / image_box_rect["height"]
            ),
            "figure_wrapper_width_mm": _number(
                item.get("figure_wrapper_width_mm")
            ),
            "figure_wrapper_rect": _rect(item.get("figure_wrapper_rect")),
            "module_width_mm": _number(item.get("module_width_mm")),
            "group_source_figure_count": int(
                item.get("group_source_figure_count") or 0
            ),
            "group_display": str(item.get("group_display") or ""),
            "group_flex_direction": str(
                item.get("group_flex_direction") or ""
            ),
            "width_ratio": painted_rect["width"] / page_width,
            "height_ratio": painted_rect["height"] / page_height,
        }
        raw_image_style = item.get("image_computed_style")
        if isinstance(item.get("surface_context"), list):
            facts["surface_context"] = _records(item["surface_context"])
        if isinstance(raw_image_style, dict):
            facts["image_computed_style"] = {
                key: str(raw_image_style.get(key) or "")
                for key in (
                    "width",
                    "height",
                    "max_width",
                    "max_height",
                    "object_fit",
                )
            }
        observations.append(
            {
                "id": f"figure:{identity}{placement}",
                "kind": "source_figure_extent",
                "salience": area_ratio,
                "module_id": str(item.get("module_id") or "").strip(),
                "module_priority": str(item.get("module_priority") or "").strip(),
                "rect": painted_rect,
                "facts": facts,
            }
        )
    return observations


def _native_observations(
    report: dict[str, Any],
    space_observations: list[dict[str, Any]],
    figures: list[dict[str, Any]],
    page_width: float,
    page_height: float,
    page_width_mm: float,
    page_height_mm: float,
) -> list[dict[str, Any]]:
    """Reuse browser targets and content bounds for editable scientific carriers."""

    priorities = {
        str(item.get("module_id") or ""): str(item.get("module_priority") or "")
        for item in _records(report.get("typography"))
    }
    targets = []
    for target in _records(report.get("visual_targets")):
        module_id = str(target.get("module_id") or "").strip()
        rect = _rect(target.get("rect"))
        if not module_id or rect is None:
            continue
        if target.get("kind") not in {"img", "svg", "canvas", "table", "math", "pre", "figure"}:
            continue
        if any(
            figure["module_id"] == module_id and (
                _contains_rect(rect, figure["rect"])
                or _contains_rect(figure["rect"], rect)
            )
            for figure in figures
        ):
            continue
        targets.append({**target, "module_id": module_id, "rect": rect})
    for module in space_observations:
        if module["kind"] != "module_content_extent":
            continue
        rectangles = [
            _rect(module["facts"].get("content_bounds_px")) or module["rect"],
            *[
                rect for target in _records(report.get("visual_targets"))
                if target.get("module_id") == module["module_id"]
                and (rect := _rect(target.get("rect"))) is not None
            ],
        ]
        left = min(rect["left"] for rect in rectangles)
        top = min(rect["top"] for rect in rectangles)
        targets.append({
            "target_id": module["id"], "kind": "module", "module_id": module["module_id"],
            "rect": {
                "left": left, "top": top,
                "width": max(_right(rect) for rect in rectangles) - left,
                "height": max(_bottom(rect) for rect in rectangles) - top,
            },
        })
    return [
        {
            "id": "carrier:" + _document_sha256({
                "target_id": target["target_id"], "module_id": target["module_id"], "rect": target["rect"],
            }),
            "kind": "native_carrier_extent",
            "module_id": target["module_id"],
            "module_priority": priorities.get(target["module_id"], ""),
            "rect": target["rect"],
            "salience": target["rect"]["width"] * target["rect"]["height"] / (page_width * page_height),
            "facts": {
                "target_id": target["target_id"], "carrier_kind": target["kind"],
                "width_mm": target["rect"]["width"] * page_width_mm / page_width,
                "height_mm": target["rect"]["height"] * page_height_mm / page_height,
            },
        }
        for target in targets
    ]


def _contains_rect(outer: dict[str, float], inner: dict[str, float]) -> bool:
    return (
        outer["left"] <= inner["left"] and outer["top"] <= inner["top"]
        and _right(outer) >= _right(inner) and _bottom(outer) >= _bottom(inner)
    )


def _detail_order(item: dict[str, Any]) -> tuple[int, float, str]:
    """Keep the existing scientific priority ladder ahead of crop area."""

    priority = str(item.get("module_priority") or "").strip().lower()
    try:
        priority_rank = _DETAIL_PRIORITY_ORDER.index(priority)
    except ValueError:
        priority_rank = len(_DETAIL_PRIORITY_ORDER)
    return priority_rank, -float(item["salience"]), str(item["id"])


def _typography_observations(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep page context and each module's measured reading roles addressable."""

    observations = [{
        "id": "typography:distribution",
        "kind": "typography_distribution",
        "salience": 1.0,
        "facts": _typography_summary(report),
    }]
    by_module: dict[str, list[dict[str, Any]]] = {}
    for item in _records(report.get("typography")):
        module_id = str(item.get("module_id") or "").strip()
        if module_id:
            by_module.setdefault(module_id, []).append(item)
    for module_id, records in by_module.items():
        observations.append({
            "id": f"typography:module:{module_id}",
            "kind": "typography_distribution",
            "salience": 1.0,
            "module_id": module_id,
            "module_priority": _weighted_mode(records, "module_priority"),
            "facts": _typography_summary({**report, "typography": records}),
        })
    return observations


def _typography_summary(report: dict[str, Any]) -> dict[str, Any]:
    records = _records(report.get("typography"))
    values = [
        (
            size,
            max(1, int(item.get("char_count") or 0)),
            str(item.get("role") or "body"),
        )
        for item in records
        if (size := _number(item.get("font_size_mm"))) is not None
    ]
    poster = report.get("poster")
    poster = poster if isinstance(poster, dict) else {}
    width_mm = _number(poster.get("width_mm"))
    height_mm = _number(poster.get("height_mm"))
    by_role: dict[str, dict[str, float | int | str]] = {}
    for role in sorted({item[2] for item in values}):
        role_records = [
            item for item in records if str(item.get("role") or "body") == role
        ]
        role_values = [
            (size, weight) for size, weight, item_role in values if item_role == role
        ]
        median = _weighted_quantile(role_values, 0.5)
        by_role[role] = {
            "character_count": sum(weight for _, weight in role_values),
            "weighted_p10_mm": _weighted_quantile(role_values, 0.1),
            "weighted_median_mm": median,
            "weighted_p90_mm": _weighted_quantile(role_values, 0.9),
        }
        if height_mm and median is not None:
            by_role[role]["median_percent_of_page_height"] = round(
                100.0 * median / height_mm, 3
            )
        font_family = _weighted_mode(role_records, "font_family")
        font_weight = _weighted_mode(role_records, "font_weight")
        if font_family:
            by_role[role]["font_family"] = font_family
        if font_weight:
            by_role[role]["font_weight"] = font_weight
        line_heights: list[tuple[float, int]] = []
        letter_spacings: list[tuple[float, int]] = []
        for item in role_records:
            font_px = _number(item.get("font_size_px"))
            weight = max(1, int(item.get("char_count") or 0))
            if font_px is None:
                continue
            line_height_px = _number(item.get("line_height_px"))
            if line_height_px is not None:
                line_heights.append((line_height_px / font_px, weight))
            letter_spacing_px = _signed_number(item.get("letter_spacing_px"))
            if letter_spacing_px is not None:
                letter_spacings.append((letter_spacing_px / font_px, weight))
        if line_heights:
            by_role[role]["line_height_ratio"] = _weighted_quantile(
                line_heights, 0.5
            )
        if letter_spacings:
            by_role[role]["letter_spacing_em"] = _weighted_quantile(
                letter_spacings, 0.5
            )
    weighted = [(size, weight) for size, weight, _ in values]
    summary = {
        "character_count": sum(weight for _, weight in weighted),
        "weighted_p10_mm": _weighted_quantile(weighted, 0.1),
        "weighted_median_mm": _weighted_quantile(weighted, 0.5),
        "weighted_p90_mm": _weighted_quantile(weighted, 0.9),
        "by_role": by_role,
    }
    title_records = [item for item in records if item.get("role") == "title"]
    title_band = report.get("title_band")
    title_band_rect = (
        _rect(title_band.get("rect")) if isinstance(title_band, dict) else None
    )
    if title_records and title_band_rect is not None:
        title = max(title_records, key=lambda item: int(item.get("char_count") or 0))
        title_box = _rect(title.get("element_rect"))
        line_rects = _item_rects(title)
        scale = width_mm / float(poster["width"]) if width_mm else None
        if title_box is not None and line_rects and scale is not None:
            band_width_mm = title_band_rect["width"] * scale
            title_texture: dict[str, float | str] = {}
            font_px = _number(title.get("font_size_px"))
            font_weight = str(title.get("font_weight") or "").strip()
            if font_weight:
                title_texture["font_weight"] = font_weight
            if font_px is not None:
                line_height_px = _number(title.get("line_height_px"))
                letter_spacing_px = _signed_number(title.get("letter_spacing_px"))
                if line_height_px is not None:
                    title_texture["line_height_ratio"] = line_height_px / font_px
                if letter_spacing_px is not None:
                    title_texture["letter_spacing_em"] = (
                        letter_spacing_px / font_px
                    )
            summary["title_composition"] = {
                "char_count": int(title.get("char_count") or 0),
                "font_family": str(title.get("font_family") or ""),
                **title_texture,
                "font_size_mm": _number(title.get("font_size_mm")),
                "line_count": int(title.get("line_count") or 0),
                "line_texts": [
                    " ".join(str(value).split())
                    for value in (
                        title.get("line_texts")
                        if isinstance(title.get("line_texts"), list)
                        else []
                    )
                    if str(value).strip()
                ],
                "line_widths_mm": [rect["width"] * scale for rect in line_rects],
                "line_width_ratios_of_title_band": [
                    rect["width"] / title_band_rect["width"] for rect in line_rects
                ],
                "text_align": str(title.get("text_align") or ""),
                "title_band_width_mm": band_width_mm,
                "title_box_width_mm": title_box["width"] * scale,
                "title_box_width_ratio_of_title_band": (
                    title_box["width"] / title_band_rect["width"]
                ),
            }
    focal_colors: dict[tuple[str, str, str, tuple[str, ...]], int] = {}
    for item in records:
        if str(item.get("module_priority") or "") != "focal":
            continue
        foreground = str(item.get("foreground_color") or "").strip()
        raw_layers = item.get("background_layers")
        layers = tuple(
            str(value).strip()
            for value in (raw_layers if isinstance(raw_layers, list) else [])
            if str(value).strip()
        )
        if not foreground or not layers:
            continue
        key = (
            str(item.get("module_id") or ""),
            str(item.get("role") or "body"),
            foreground,
            layers,
        )
        focal_colors[key] = focal_colors.get(key, 0) + max(
            1, int(item.get("char_count") or 0)
        )
    if focal_colors:
        summary["focal_color_relationships"] = [
            {
                "background_layers": list(layers),
                "char_count": char_count,
                "foreground_color": foreground,
                "module_id": module_id,
                "role": role,
            }
            for (module_id, role, foreground, layers), char_count in sorted(
                focal_colors.items()
            )
        ]
    return summary


def _weighted_mode(records: list[dict[str, Any]], key: str) -> str:
    """Return the text value carried by the most visible characters."""

    counts: dict[str, int] = {}
    for item in records:
        value = str(item.get(key) or "").strip()
        if not value:
            continue
        counts[value] = counts.get(value, 0) + max(
            1, int(item.get("char_count") or 0)
        )
    if not counts:
        return ""
    return min(counts, key=lambda value: (-counts[value], value))


def _write_crops(
    *,
    screenshot_path: Path,
    out_dir: Path,
    observations: list[dict[str, Any]],
    page_width: float,
    page_height: float,
) -> list[dict[str, Any]]:
    try:
        from PIL import Image
    except ImportError:
        return []
    selected = sorted(
        (
            item
            for item in observations
            if item.get("kind") in {"source_figure_extent", "native_carrier_extent"}
            if _rect(item.get("rect")) is not None
            and _number(item.get("salience"), allow_zero=True) is not None
        ),
        key=_detail_order,
    )
    crops = []
    selected_regions: list[dict[str, float]] = []
    with Image.open(screenshot_path) as image:
        scale_x = image.width / page_width
        scale_y = image.height / page_height
        overview_scale = min(1.0, OVERVIEW_MAX_EDGE_PX / max(image.size))
        for observation in selected:
            rect = _rect(observation.get("rect"))
            assert rect is not None
            extents = [rect]
            wrapper = _rect(observation["facts"].get("figure_wrapper_rect"))
            if wrapper is not None:
                left = min(rect["left"], wrapper["left"])
                top = min(rect["top"], wrapper["top"])
                extents.insert(0, {
                    "left": left,
                    "top": top,
                    "width": max(_right(rect), _right(wrapper)) - left,
                    "height": max(_bottom(rect), _bottom(wrapper)) - top,
                })
            padding_x = page_width * 0.015
            padding_y = page_height * 0.015
            for rect in extents:
                box = (
                    max(0, math.floor((rect["left"] - padding_x) * scale_x)),
                    max(0, math.floor((rect["top"] - padding_y) * scale_y)),
                    min(image.width, math.ceil((_right(rect) + padding_x) * scale_x)),
                    min(image.height, math.ceil((_bottom(rect) + padding_y) * scale_y)),
                )
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                if box == (0, 0, image.width, image.height):
                    continue
                detail_scale = min(
                    1.0,
                    _ATLAS_TILE_SIZE[0] / (box[2] - box[0]),
                    _ATLAS_TILE_SIZE[1] / (box[3] - box[1]),
                )
                if overview_scale < 1.0 and detail_scale <= overview_scale:
                    continue
                break
            else:
                continue
            # Carrier extents distinguish nested evidence from shared crop padding.
            if any(
                _contains_rect(selected_rect, rect) or _contains_rect(rect, selected_rect)
                for selected_rect in selected_regions
            ):
                continue
            index = len(crops) + 1
            path = out_dir / f"visual-evidence-{index:02d}.png"
            image.crop(box).save(path)
            crops.append(
                {
                    "observation_id": str(observation["id"]),
                    "path": str(path),
                    "sha256": file_sha256(path),
                    "mime_type": "image/png",
                }
            )
            selected_regions.append(rect)
            if len(crops) == MAX_CROPS:
                break
    return crops


def _write_atlas(
    *,
    crops: list[dict[str, Any]],
    out_dir: Path,
) -> dict[str, Any] | None:
    if not crops:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError:
        return None
    tiles: list[tuple[str, Any]] = []
    try:
        for crop in crops:
            with Image.open(str(crop["path"])) as opened:
                raster = ImageOps.exif_transpose(opened).convert("RGB")
                raster.load()
                raster.thumbnail(_ATLAS_TILE_SIZE, Image.Resampling.LANCZOS)
                tiles.append((str(crop["observation_id"]), raster.copy()))
    except (OSError, ValueError, KeyError):
        return None
    gap = _ATLAS_GAP_PX
    label_height = _ATLAS_LABEL_HEIGHT_PX
    width = max(tile.width for _, tile in tiles)
    height = sum(tile.height + label_height + gap for _, tile in tiles) + gap
    atlas = Image.new("RGB", (width + 2 * gap, height), "#e5e7eb")
    draw = ImageDraw.Draw(atlas)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    top = gap
    for label, tile in tiles:
        draw.rectangle((gap, top, gap + width, top + label_height), fill="#111827")
        draw.text((gap + 16, top + 12), label[:100], fill="#ffffff", font=font)
        top += label_height
        left = gap + (width - tile.width) // 2
        atlas.paste(tile, (left, top))
        top += tile.height + gap
    path = out_dir / "visual-evidence-atlas.png"
    atlas.save(path, format="PNG")
    return {
        "path": str(path),
        "sha256": file_sha256(path),
        "mime_type": "image/png",
    }


def _weighted_quantile(
    values: Iterable[tuple[float, int]],
    quantile: float,
) -> float | None:
    ordered = sorted(values)
    total = sum(weight for _, weight in ordered)
    if total <= 0:
        return None
    target = total * quantile
    cumulative = 0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= target:
            return value
    return ordered[-1][0]


def _item_rects(item: dict[str, Any]) -> list[dict[str, float]]:
    rects = [_rect(value) for value in item.get("rects", [])]
    direct = _rect(item.get("rect"))
    return [item for item in ([direct] if direct else rects) if item is not None]


def _records(value: object) -> list[dict[str, Any]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _rect(value: object) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    left = _number(value.get("left"), allow_zero=True)
    top = _number(value.get("top"), allow_zero=True)
    width = _number(value.get("width"))
    height = _number(value.get("height"))
    if None in {left, top, width, height}:
        return None
    return {"left": left, "top": top, "width": width, "height": height}


def _number(value: object, *, allow_zero: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    minimum = 0.0 if allow_zero else 1e-12
    return number if math.isfinite(number) and number >= minimum else None


def _signed_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _right(rect: dict[str, float]) -> float:
    return rect["left"] + rect["width"]


def _bottom(rect: dict[str, float]) -> float:
    return rect["top"] + rect["height"]


def _document_sha256(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
