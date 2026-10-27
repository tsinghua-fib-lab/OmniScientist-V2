"""Deterministic export checks for editable academic-poster PPTX scenes."""

from __future__ import annotations

import math
from itertools import combinations
from typing import Any


def check_scene(scene: dict[str, Any]) -> dict[str, Any]:
    """Block broken editability while reporting visual-fidelity concerns."""

    page = scene["page"]
    objects = scene["objects"]
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
        angle = math.radians(item.get("rotation", 0))
        width = abs(math.cos(angle)) * item["w"] + abs(math.sin(angle)) * item["h"]
        height = abs(math.sin(angle)) * item["w"] + abs(math.cos(angle)) * item["h"]
        left = item["x"] + (item["w"] - width) / 2
        top = item["y"] + (item["h"] - height) / 2
        if (
            left < -0.01 or top < -0.01
            or left + width > page["width_in"] + 0.01
            or top + height > page["height_in"] + 0.01
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
        if (
            item["kind"] == "image"
            and not item.get("source_figure_sha256")
            and left <= 0.01 and top <= 0.01
            and left + width >= page["width_in"] - 0.01
            and top + height >= page["height_in"] - 0.01
        ):
            failures.append(
                _issue(
                    item["id"],
                    "editable_coverage",
                    "error",
                    "An image without a bound paper-figure identity covers the complete poster slide.",
                    "Capture poster content as native text, tables, equations and shapes; retain bound paper figures as images.",
                )
            )
        if item["kind"] == "text" and item.get("fit") == "grow":
            failures.append(
                _issue(
                    item["id"],
                    "text_fit_policy",
                    "error",
                    "Text box may grow beyond its captured HTML bounds after font substitution or wrapping.",
                    "Use fixed-box shrink-to-fit, or disable fitting for preflighted lines, so neighboring editable objects cannot be displaced or overlapped.",
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
        if _container_pair(first, second) or not _overlap(first, second):
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


def _container_pair(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Return whether one object explicitly owns the other's scene geometry."""

    return (
        first.get("container_id") == second["id"]
        or second.get("container_id") == first["id"]
    )


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
