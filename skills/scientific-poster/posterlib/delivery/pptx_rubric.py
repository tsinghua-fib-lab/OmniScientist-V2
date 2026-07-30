"""Deterministic export checks for editable academic-poster PPTX scenes."""

from __future__ import annotations

from itertools import combinations
from typing import Any


def evaluate_scene(scene: dict[str, Any]) -> dict[str, Any]:
    """Block broken editability while reporting visual-fidelity concerns."""

    page = scene["page"]
    objects = scene["objects"]
    page_area = page["width_in"] * page["height_in"]
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    text_objects = [
        item for item in objects if item["kind"] in {"equation", "table", "text"}
    ]
    if not text_objects:
        failures.append(
            _issue(
                "poster",
                "native_text",
                "error",
                "The deck has no editable text objects.",
                "Capture headings, body copy, captions, and table cells as native text.",
            )
        )
    for item in objects:
        if (
            item["x"] + item["w"] > page["width_in"] + 0.01
            or item["y"] + item["h"] > page["height_in"] + 0.01
        ):
            warnings.append(
                _issue(
                    item["id"],
                    "slide_bounds",
                    "warning",
                    "Object extends beyond the PowerPoint slide.",
                    "Reduce or reposition this object inside the slide boundary.",
                )
            )
        if item["kind"] == "image" and item["w"] * item["h"] / page_area >= 0.8:
            failures.append(
                _issue(
                    item["id"],
                    "editable_coverage",
                    "error",
                    "A single raster image covers at least 80% of the poster.",
                    "Export regions as native shapes and text; keep raster content to figures only.",
                )
            )
        if item["kind"] in {"equation", "table", "text"}:
            size = item["font_size_pt"]
            if size < 12:
                warnings.append(
                    _issue(
                        item["id"],
                        "minimum_type_size",
                        "warning",
                        f"Text is {size:g} pt, below the advisory 12 pt reference.",
                        "Increase this object's type size or shorten its content.",
                    )
                )
            elif size < 16:
                warnings.append(
                    _issue(
                        item["id"],
                        "minimum_type_size",
                        "warning",
                        f"Text is only {size:g} pt at poster scale.",
                        "Prefer at least 16 pt for secondary poster text.",
                    )
                )
        if item["kind"] == "text" and item.get("fit") != "shrink":
            failures.append(
                _issue(
                    item["id"],
                    "text_fit_policy",
                    "error",
                    "Text box may grow beyond its captured HTML bounds after font substitution or wrapping.",
                    "Use fixed-box shrink-to-fit so neighboring editable objects cannot be displaced or overlapped.",
                )
            )
        if item["kind"] == "text" and not item.get("font_preflighted"):
            warnings.append(
                _issue(
                    item["id"],
                    "portable_font_preflight",
                    "warning",
                    "The browser could not confirm the portable PowerPoint font.",
                    "Review this text box after export because PowerPoint may substitute its font.",
                )
            )

    native_text = [
        item for item in objects if item["kind"] in {"equation", "table", "text"}
    ]
    for first, second in combinations(native_text, 2):
        if not _overlap(first, second):
            continue
        warnings.append(
            _issue(
                first["id"],
                "native_text_overlap",
                "warning",
                f"Editable object overlaps {second['id']} in the captured scene.",
                "Resize or reposition the two native objects so their slide boxes do not intersect.",
            )
        )

    return {
        "status": "error" if failures else "ok",
        "hard_failures": failures,
        "warnings": warnings,
        "summary": f"{len(failures)} hard failure(s), {len(warnings)} warning(s).",
    }


def _overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Return whether two captured native-object boxes overlap materially."""

    width = min(first["x"] + first["w"], second["x"] + second["w"]) - max(
        first["x"], second["x"]
    )
    height = min(first["y"] + first["h"], second["y"] + second["h"]) - max(
        first["y"], second["y"]
    )
    return width > 0.02 and height > 0.02


def _issue(
    object_id: str,
    criterion: str,
    severity: str,
    message: str,
    suggested_patch: str,
) -> dict[str, str]:
    return {
        "object_id": object_id,
        "criterion": criterion,
        "severity": severity,
        "message": message,
        "suggested_patch": suggested_patch,
    }
