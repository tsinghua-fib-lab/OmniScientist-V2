"""Ground visual review in measurements from the exact rendered poster."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

from posterlib.content import planning

MAX_CROPS = 3
_COORDINATE_EPSILON_PX = 1.0


def file_sha256(path: Path) -> str:
    """Return the SHA-256 identity of an immutable visual input."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_visual_evidence(
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
    if not screenshot_path.is_file():
        raise FileNotFoundError(screenshot_path)

    text_runs = _records(report.get("text_runs"))
    visual_targets = _records(report.get("visual_targets"))
    content_rects = [
        rect for item in (*text_runs, *visual_targets) for rect in _item_rects(item)
    ]
    space_observations = [
        *_space_observations(
            report=report,
            text_runs=text_runs,
            visual_targets=visual_targets,
            page_width=width,
            page_height=height,
            content_rects=content_rects,
        ),
        *_delivery_observations(
            report,
            page_width=width,
            page_height=height,
        ),
    ]
    observations = _select_observations(
        space_observations=space_observations,
        figure_observations=_figure_observations(report, width, height),
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
    """Keep factual coverage bounded without deciding whether a layout is good."""

    page = [
        item for item in space_observations if item["kind"] == "page_trailing_space"
    ]
    modules = sorted(
        (
            item
            for item in space_observations
            if item["kind"] == "module_trailing_space"
        ),
        key=lambda item: (-float(item["salience"]), str(item["id"])),
    )[:1]
    interior_gaps = sorted(
        (item for item in space_observations if item["kind"] == "inter_module_gap"),
        key=lambda item: (-float(item["salience"]), str(item["id"])),
    )[:2]
    lane_trailing = sorted(
        (item for item in space_observations if item["kind"] == "lane_trailing_space"),
        key=lambda item: (-float(item["salience"]), str(item["id"])),
    )[:2]
    lane_depth_profiles = [
        item for item in space_observations if item["kind"] == "lane_depth_profile"
    ]
    lane_entries = sorted(
        (item for item in space_observations if item["kind"] == "lane_entry_offset"),
        key=lambda item: (-float(item["salience"]), str(item["id"])),
    )[:2]
    page_overflow = [
        item for item in space_observations if item["kind"] == "page_content_overflow"
    ]
    outside_modules = sorted(
        (item for item in space_observations if item["kind"] == "module_outside_page"),
        key=lambda item: (-float(item["salience"]), str(item["id"])),
    )[:3]
    figures = sorted(
        figure_observations,
        key=lambda item: (-float(item["salience"]), str(item["id"])),
    )[:4]
    observations = [
        {
            "id": "typography:distribution",
            "kind": "typography_distribution",
            "salience": 1.0,
            "facts": typography,
        },
        *page_overflow,
        *outside_modules,
        *page,
        *modules,
        *interior_gaps,
        *lane_entries,
        *lane_depth_profiles,
        *lane_trailing,
        *figures,
    ]
    return sorted(observations, key=lambda item: str(item["id"]))


def _space_observations(
    *,
    report: dict[str, Any],
    text_runs: list[dict[str, Any]],
    visual_targets: list[dict[str, Any]],
    page_width: float,
    page_height: float,
    content_rects: list[dict[str, float]],
) -> list[dict[str, Any]]:
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
                "trailing_space_ratio": trailing / page_height,
            },
        }
    ]
    content = (*text_runs, *visual_targets)
    measured_modules: list[tuple[str, dict[str, float]]] = []
    for module in _records(report.get("modules")):
        module_rect = _rect(module.get("rect"))
        module_id = str(module.get("module_id") or "").strip()
        if module_rect is None or not module_id:
            continue
        measured_modules.append((module_id, module_rect))
        descendants = [
            rect
            for item in content
            if str(item.get("module_id") or "").strip() == module_id
            for rect in _item_rects(item)
        ]
        local_bottom = max(
            (_bottom(rect) for rect in descendants),
            default=float(module_rect["top"]),
        )
        module_bottom = _bottom(module_rect)
        void_top = min(module_bottom, max(float(module_rect["top"]), local_bottom))
        void_height = max(0.0, module_bottom - void_top)
        observations.append(
            {
                "id": f"space:module:{_stable_id(module_id)}",
                "kind": "module_trailing_space",
                "module_ids": [module_id],
                "salience": void_height / float(module_rect["height"]),
                "rect": {
                    "left": module_rect["left"],
                    "top": void_top,
                    "width": module_rect["width"],
                    "height": void_height,
                },
                "facts": {
                    "content_bottom_px": local_bottom,
                    "module_bottom_px": module_bottom,
                    "trailing_space_px": void_height,
                    "trailing_space_ratio": void_height / float(module_rect["height"]),
                },
            }
        )
    observations.extend(
        _inter_module_gap_observations(
            measured_modules,
            page_width=page_width,
            page_height=page_height,
            content_rects=content_rects,
        )
    )
    return observations


def _delivery_observations(
    report: dict[str, Any],
    *,
    page_width: float,
    page_height: float,
) -> list[dict[str, Any]]:
    """Describe rendered modules that do not fit inside the physical page."""

    modules: list[tuple[str, dict[str, float]]] = []
    for item in _records(report.get("modules")):
        module_id = str(item.get("module_id") or "").strip()
        rect = _rect(item.get("rect"))
        if module_id and rect is not None:
            modules.append((module_id, rect))
    if not modules:
        return []

    content_right = max(_right(rect) for _, rect in modules)
    content_bottom = max(_bottom(rect) for _, rect in modules)
    overflow_right = max(0.0, content_right - page_width)
    overflow_bottom = max(0.0, content_bottom - page_height)
    observations: list[dict[str, Any]] = []
    if (
        overflow_right > _COORDINATE_EPSILON_PX
        or overflow_bottom > _COORDINATE_EPSILON_PX
    ):
        observations.append(
            {
                "id": "delivery:page-content-extent",
                "kind": "page_content_overflow",
                "salience": max(
                    overflow_right / page_width,
                    overflow_bottom / page_height,
                ),
                "facts": {
                    "page_width_px": page_width,
                    "page_height_px": page_height,
                    "content_right_px": content_right,
                    "content_bottom_px": content_bottom,
                    "overflow_right_px": overflow_right,
                    "overflow_bottom_px": overflow_bottom,
                },
            }
        )

    for module_id, rect in modules:
        overflow_right = max(0.0, _right(rect) - page_width)
        overflow_bottom = max(0.0, _bottom(rect) - page_height)
        if (
            overflow_right <= _COORDINATE_EPSILON_PX
            and overflow_bottom <= _COORDINATE_EPSILON_PX
        ):
            continue
        visible_width = max(
            0.0,
            min(_right(rect), page_width) - min(max(rect["left"], 0.0), page_width),
        )
        visible_height = max(
            0.0,
            min(_bottom(rect), page_height) - min(max(rect["top"], 0.0), page_height),
        )
        observations.append(
            {
                "id": f"delivery:outside:{_stable_id(module_id)}",
                "kind": "module_outside_page",
                "module_ids": [module_id],
                "salience": max(
                    overflow_right / page_width,
                    overflow_bottom / page_height,
                ),
                "rect": rect,
                "facts": {
                    "page_width_px": page_width,
                    "page_height_px": page_height,
                    "module_right_px": _right(rect),
                    "module_bottom_px": _bottom(rect),
                    "overflow_right_px": overflow_right,
                    "overflow_bottom_px": overflow_bottom,
                    "visible_area_fraction": (
                        visible_width
                        * visible_height
                        / (rect["width"] * rect["height"])
                    ),
                },
            }
        )
    return observations


def _inter_module_gap_observations(
    modules: list[tuple[str, dict[str, float]]],
    *,
    page_width: float,
    page_height: float,
    content_rects: list[dict[str, float]],
) -> list[dict[str, Any]]:
    """Describe genuinely empty separation between modules in each visual lane."""

    observations: list[dict[str, Any]] = []
    body_top = min((rect["top"] for _, rect in modules), default=0.0)
    for upper_id, upper in modules:
        upper_bottom = _bottom(upper)
        upper_candidates = [
            other
            for other_id, other in modules
            if other_id != upper_id
            and _bottom(other) <= upper["top"]
            and _horizontal_overlap(upper, other)
            >= min(upper["width"], other["width"]) * 0.5
        ]
        entry_offset = max(0.0, upper["top"] - body_top)
        if not upper_candidates and entry_offset > _COORDINATE_EPSILON_PX:
            observations.append(
                {
                    "id": f"space:lane-entry:{_stable_id(upper_id)}",
                    "kind": "lane_entry_offset",
                    "module_ids": [upper_id],
                    "salience": entry_offset / page_height,
                    "rect": {
                        "left": upper["left"],
                        "top": body_top,
                        "width": upper["width"],
                        "height": entry_offset,
                    },
                    "facts": {
                        "body_module_start_px": body_top,
                        "lane_first_module_top_px": upper["top"],
                        "entry_offset_px": entry_offset,
                        "entry_offset_ratio": entry_offset / page_height,
                        "lane_width_ratio": upper["width"] / page_width,
                    },
                }
            )
        candidates: list[tuple[float, float, str, dict[str, float]]] = []
        for lower_id, lower in modules:
            gap = lower["top"] - upper_bottom
            if gap <= 0.0:
                continue
            overlap_left = max(upper["left"], lower["left"])
            overlap_right = min(_right(upper), _right(lower))
            overlap = max(0.0, overlap_right - overlap_left)
            if overlap < min(upper["width"], lower["width"]) * 0.5:
                continue
            candidates.append((gap, -overlap, lower_id, lower))
        if not candidates:
            trailing = max(0.0, page_height - upper_bottom)
            if trailing > 0.0:
                observations.append(
                    {
                        "id": f"space:lane-tail:{_stable_id(upper_id)}",
                        "kind": "lane_trailing_space",
                        "module_ids": [upper_id],
                        "salience": trailing / page_height,
                        "rect": {
                            "left": upper["left"],
                            "top": upper_bottom,
                            "width": upper["width"],
                            "height": trailing,
                        },
                        "facts": {
                            "module_bottom_px": upper_bottom,
                            "page_bottom_px": page_height,
                            "trailing_space_px": trailing,
                            "trailing_space_ratio": trailing / page_height,
                            "lane_width_ratio": upper["width"] / page_width,
                        },
                    }
                )
            continue
        gap, _negative_overlap, lower_id, lower = min(candidates)
        overlap_left = max(upper["left"], lower["left"])
        overlap_right = min(_right(upper), _right(lower))
        overlap = overlap_right - overlap_left
        if any(
            min(overlap_right, _right(rect)) - max(overlap_left, rect["left"]) > 1.0
            and min(lower["top"], _bottom(rect)) - max(upper_bottom, rect["top"]) > 1.0
            for rect in content_rects
        ):
            continue
        observations.append(
            {
                "id": (f"space:between:{_stable_id(upper_id)}--{_stable_id(lower_id)}"),
                "kind": "inter_module_gap",
                "module_ids": [upper_id, lower_id],
                "salience": gap / page_height,
                "rect": {
                    "left": overlap_left,
                    "top": upper_bottom,
                    "width": overlap,
                    "height": gap,
                },
                "facts": {
                    "upper_module_bottom_px": upper_bottom,
                    "lower_module_top_px": lower["top"],
                    "gap_px": gap,
                    "gap_page_ratio": gap / page_height,
                    "lane_width_ratio": overlap / page_width,
                },
            }
        )
    lane_depth_profile = _lane_depth_profile_observation(
        observations,
        page_height=page_height,
    )
    if lane_depth_profile is not None:
        observations.append(lane_depth_profile)
    return observations


def _lane_depth_profile_observation(
    observations: list[dict[str, Any]],
    *,
    page_height: float,
) -> dict[str, Any] | None:
    """Describe relative lane depth while leaving its visual value to the reviewer."""

    terminal_lanes = [
        item for item in observations if item["kind"] == "lane_trailing_space"
    ]
    if len(terminal_lanes) < 2:
        return None
    lane_facts = sorted(
        (
            {
                "module_id": str(item["module_ids"][0]),
                "left_px": float(item["rect"]["left"]),
                "right_px": _right(item["rect"]),
                "module_bottom_px": float(item["facts"]["module_bottom_px"]),
                "trailing_space_px": float(item["facts"]["trailing_space_px"]),
            }
            for item in terminal_lanes
        ),
        key=lambda item: (float(item["left_px"]), str(item["module_id"])),
    )
    if any(
        float(current["left_px"]) < float(previous["right_px"]) - _COORDINATE_EPSILON_PX
        for previous, current in zip(lane_facts, lane_facts[1:], strict=False)
    ):
        return None
    shallowest_bottom = min(float(item["module_bottom_px"]) for item in lane_facts)
    deepest_bottom = max(float(item["module_bottom_px"]) for item in lane_facts)
    depth_difference = deepest_bottom - shallowest_bottom
    if depth_difference <= _COORDINATE_EPSILON_PX:
        return None
    for item in lane_facts:
        item["unused_while_peers_continue_px"] = max(
            0.0,
            deepest_bottom - float(item["module_bottom_px"]),
        )
    left = min(float(item["left_px"]) for item in lane_facts)
    right = max(float(item["right_px"]) for item in lane_facts)
    return {
        "id": "space:lane-depth-profile",
        "kind": "lane_depth_profile",
        "module_ids": [str(item["module_id"]) for item in lane_facts],
        "salience": depth_difference / page_height,
        "rect": {
            "left": left,
            "top": shallowest_bottom,
            "width": right - left,
            "height": depth_difference,
        },
        "facts": {
            "terminal_lanes": lane_facts,
            "shallowest_lane_bottom_px": shallowest_bottom,
            "deepest_lane_bottom_px": deepest_bottom,
            "unused_while_peers_continue_px": depth_difference,
            "unused_while_peers_continue_ratio": depth_difference / page_height,
            "common_page_trailing_px": max(0.0, page_height - deepest_bottom),
        },
    }


def _figure_observations(
    report: dict[str, Any],
    page_width: float,
    page_height: float,
) -> list[dict[str, Any]]:
    source = report.get("source_figures")
    details = _records(source.get("details")) if isinstance(source, dict) else []
    observations = []
    for index, item in enumerate(details):
        rect = _rect(item.get("painted_rect") or item.get("rect"))
        if rect is None:
            continue
        digest = str(item.get("sha256") or "")
        identity = digest[:12] if re.fullmatch(r"[0-9a-f]{64}", digest) else str(index)
        area_ratio = rect["width"] * rect["height"] / (page_width * page_height)
        observations.append(
            {
                "id": f"figure:{identity}",
                "kind": "source_figure_extent",
                "salience": area_ratio,
                "rect": rect,
                "facts": {
                    "sha256": digest,
                    "usable": item.get("usable") is True,
                    "readable": item.get("readable") is True,
                    "inside_poster": item.get("inside_poster") is True,
                    "clipped": item.get("clipped") is True,
                    "viewable_area_ratio": area_ratio,
                    "width_ratio": rect["width"] / page_width,
                    "height_ratio": rect["height"] / page_height,
                },
            }
        )
    return observations


def _typography_summary(report: dict[str, Any]) -> dict[str, Any]:
    values = [
        (
            size,
            max(1, int(item.get("char_count") or 0)),
            str(item.get("role") or "body"),
        )
        for item in _records(report.get("typography"))
        if (size := _number(item.get("font_size_mm"))) is not None
    ]
    poster = report.get("poster")
    poster = poster if isinstance(poster, dict) else {}
    width_mm = _number(poster.get("width_mm"))
    reading_targets = planning.typography_metrics(width_mm) if width_mm else {}
    by_role: dict[str, dict[str, float | int]] = {}
    for role in sorted({item[2] for item in values}):
        role_values = [
            (size, weight) for size, weight, item_role in values if item_role == role
        ]
        median = _weighted_quantile(role_values, 0.5)
        minimum = _typography_minimum(role, reading_targets)
        by_role[role] = {
            "character_count": sum(weight for _, weight in role_values),
            "weighted_median_mm": median,
        }
        if minimum is not None:
            by_role[role].update(
                {
                    "minimum_mm": minimum,
                    "difference_from_minimum_mm": median - minimum,
                }
            )
    weighted = [(size, weight) for size, weight, _ in values]
    return {
        "character_count": sum(weight for _, weight in weighted),
        "weighted_p10_mm": _weighted_quantile(weighted, 0.1),
        "weighted_median_mm": _weighted_quantile(weighted, 0.5),
        "weighted_p90_mm": _weighted_quantile(weighted, 0.9),
        "by_role": by_role,
        "reading_targets_mm": reading_targets,
    }


def _typography_minimum(
    role: str,
    targets: dict[str, float],
) -> float | None:
    """Map one rendered text role to its physical viewing-distance reference."""

    if not targets:
        return None
    if role == "title":
        return targets["title_min_mm"]
    if role in {"caption", "identity", "provenance"}:
        return targets["provenance_min_mm"]
    return targets["body_min_mm"]


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
            if _rect(item.get("rect")) is not None
            and _number(item.get("salience"), allow_zero=True) is not None
        ),
        key=lambda item: (-float(item["salience"]), str(item["id"])),
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


def _right(rect: dict[str, float]) -> float:
    return rect["left"] + rect["width"]


def _bottom(rect: dict[str, float]) -> float:
    return rect["top"] + rect["height"]


def _horizontal_overlap(
    first: dict[str, float],
    second: dict[str, float],
) -> float:
    return max(
        0.0, min(_right(first), _right(second)) - max(first["left"], second["left"])
    )


def _stable_id(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-") or "unknown"


def _document_sha256(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
