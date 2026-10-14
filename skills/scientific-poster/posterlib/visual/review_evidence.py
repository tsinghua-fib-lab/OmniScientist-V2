"""Ground visual review in measurements from the exact rendered poster."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_CROPS = 3
_FIGURE_PRIORITY_ORDER = ("focal", "primary", "supporting", "footer")


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
        content_rects.extend(item_rects)
        module_id = str(item.get("module_id") or "").strip()
        if module_id and item_rects:
            content_rects_by_module.setdefault(module_id, []).extend(item_rects)
    space_observations = _space_observations(
        report=report,
        page_width=width,
        page_height=height,
        page_width_mm=width_mm,
        page_height_mm=height_mm,
        content_rects=content_rects,
        content_rects_by_module=content_rects_by_module,
    )
    observations = _select_observations(
        space_observations=space_observations,
        figure_observations=_figure_observations(
            report,
            width,
            height,
            width_mm,
            height_mm,
        ),
        typography=_typography_summary(report),
    )

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
    figure_observations: list[dict[str, Any]],
    typography: dict[str, Any],
) -> list[dict[str, Any]]:
    """Keep all module measurements and bounded source-figure detail."""

    return [
        {
            "id": "typography:distribution",
            "kind": "typography_distribution",
            "salience": 1.0,
            "facts": typography,
        },
        *space_observations,
        *sorted(figure_observations, key=_figure_order)[:4],
    ]


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
        digest[:12] if re.fullmatch(r"[0-9a-f]{64}", digest) else str(index)
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


def _figure_order(item: dict[str, Any]) -> tuple[int, float, str]:
    """Keep the existing scientific priority ladder ahead of crop area."""

    priority = str(item.get("module_priority") or "").strip().lower()
    try:
        priority_rank = _FIGURE_PRIORITY_ORDER.index(priority)
    except ValueError:
        priority_rank = len(_FIGURE_PRIORITY_ORDER)
    return priority_rank, -float(item["salience"]), str(item["id"])


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
            "weighted_median_mm": median,
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
            if item.get("kind") == "source_figure_extent"
            if _rect(item.get("rect")) is not None
            and _number(item.get("salience"), allow_zero=True) is not None
        ),
        key=_figure_order,
    )[:MAX_CROPS]
    crops = []
    with Image.open(screenshot_path) as image:
        scale_x = image.width / page_width
        scale_y = image.height / page_height
        for index, observation in enumerate(selected, start=1):
            rect = _rect(observation.get("rect"))
            assert rect is not None
            padding_x = page_width * 0.015
            padding_y = page_height * 0.015
            box = (
                max(0, math.floor((rect["left"] - padding_x) * scale_x)),
                max(0, math.floor((rect["top"] - padding_y) * scale_y)),
                min(
                    image.width,
                    math.ceil((_right(rect) + padding_x) * scale_x),
                ),
                min(
                    image.height,
                    math.ceil((_bottom(rect) + padding_y) * scale_y),
                ),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
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
                raster.thumbnail((1200, 760), Image.Resampling.LANCZOS)
                tiles.append((str(crop["observation_id"]), raster.copy()))
    except (OSError, ValueError, KeyError):
        return None
    gap = 24
    label_height = 54
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
