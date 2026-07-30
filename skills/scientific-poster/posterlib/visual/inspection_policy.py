"""Repair feedback derived from rendered poster measurements."""

from __future__ import annotations

import json
from typing import Any


def inspection_feedback(inspection: dict[str, Any]) -> list[str]:
    """Turn rendered geometry failures into bounded whole-page repair guidance."""

    report = inspection.get("report")
    if not isinstance(report, dict):
        return []
    poster = report.get("poster")
    modules = report.get("modules")
    if not isinstance(poster, dict) or not isinstance(modules, list):
        return []
    width = _positive_number(poster.get("width"))
    height = _positive_number(poster.get("height"))
    width_mm = _positive_number(poster.get("width_mm"))
    height_mm = _positive_number(poster.get("height_mm"))
    if None in {width, height, width_mm, height_mm}:
        return []

    feedback: list[str] = []
    valid_modules = [item for item in modules if isinstance(item, dict)]
    geometry: list[dict[str, float | str]] = []
    for module in valid_modules:
        rect = module.get("rect")
        if not isinstance(rect, dict):
            continue
        left = _positive_number(rect.get("left"), allow_zero=True)
        top = _positive_number(rect.get("top"), allow_zero=True)
        module_width = _positive_number(rect.get("width"))
        module_height = _positive_number(rect.get("height"))
        if top is not None and module_height is not None:
            geometry.append(
                {
                    "module_id": str(module.get("module_id") or "unknown"),
                    "left": left if left is not None else 0.0,
                    "top": top,
                    "width": module_width if module_width is not None else 0.0,
                    "bottom": top + module_height,
                }
            )
    bottoms = [float(item["bottom"]) for item in geometry]
    if bottoms and max(bottoms) > height + 1.0:
        overflow_mm = (max(bottoms) - height) * height_mm / height
        feedback.append(
            f"Rendered modules extend about {overflow_mm:.0f} mm below the physical page; "
            "repack the whole composition instead of clipping or shrinking type."
        )
        overflowing = [
            item for item in geometry if float(item["bottom"]) > height + 1.0
        ]
        placements = ", ".join(
            f"{item['module_id']} ends at "
            f"{float(item['bottom']) * height_mm / height:.0f} mm "
            f"({(float(item['bottom']) - height) * height_mm / height:.0f} mm past page)"
            for item in overflowing[:8]
        )
        if placements:
            feedback.append(
                "Overflowing module placements: "
                + placements
                + ". Relocate intact modules into shorter visual stacks before changing "
                "scientific copy or type size."
            )
        spanning = [
            item
            for item in overflowing
            if float(item["width"]) >= width * 0.8 and float(item["top"]) >= height
        ]
        if spanning:
            names = ", ".join(str(item["module_id"]) for item in spanning)
            feedback.append(
                f"Spanning module {names} begins below the physical page. Do not append a "
                "full-width row after already full-height stacks; reserve that row first or "
                "place the module in available body space."
            )
        title_band = report.get("title_band")
        title_rect = title_band.get("rect") if isinstance(title_band, dict) else None
        title_height = (
            _positive_number(title_rect.get("height"))
            if isinstance(title_rect, dict)
            else None
        )
        if title_height is not None and title_height / height >= 0.20:
            feedback.append(
                f"The title band occupies {title_height / height:.0%} of page height "
                f"({title_height * height_mm / height:.0f} mm) while body content "
                "overflows. Check selector cascade and restore a compact horizontal "
                "masthead before compressing scientific content."
            )

    raw_warnings = [
        item
        for source in (report.get("warnings"), inspection.get("warnings"))
        if isinstance(source, list)
        for item in source
        if isinstance(item, dict)
    ]
    warnings: list[dict[str, Any]] = []
    seen_warnings: set[str] = set()
    for item in raw_warnings:
        identity = json.dumps(
            {
                "code": item.get("code"),
                "poster_id": item.get("poster_id"),
                "role": item.get("role"),
                "observed": item.get("observed"),
                "target": item.get("target"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if identity not in seen_warnings:
            seen_warnings.add(identity)
            warnings.append(item)
    for item in warnings:
        if item.get("code") == "poster_physical_size_mismatch":
            rendered_width = _positive_number(item.get("rendered_width_mm"))
            rendered_height = _positive_number(item.get("rendered_height_mm"))
            expected_width = _positive_number(item.get("expected_width_mm"))
            expected_height = _positive_number(item.get("expected_height_mm"))
            if None not in {
                rendered_width,
                rendered_height,
                expected_width,
                expected_height,
            }:
                feedback.append(
                    f"The body-level poster root renders at {rendered_width:g} × "
                    f"{rendered_height:g} mm, but @page is {expected_width:g} × "
                    f"{expected_height:g} mm. Give @page, html, body, and that root the "
                    "same explicit physical width and height; the root must enclose the "
                    "title band and evidence body, never use auto, min-height, or "
                    "max-height."
                )
            continue
        if item.get("code") != "element_content_overflow":
            continue
        observed = item.get("observed")
        observed = observed if isinstance(observed, dict) else {}
        overflow_x = _positive_number(observed.get("overflow_x_px"), allow_zero=True)
        overflow_y = _positive_number(observed.get("overflow_y_px"), allow_zero=True)
        module_id = str(item.get("poster_id") or "unknown")
        if overflow_x is not None and overflow_x > 1:
            amount_mm = overflow_x * width_mm / width
            feedback.append(
                f"Module {module_id} exceeds its own box horizontally by about "
                f"{amount_mm:.0f} mm; remove fixed/minimum child widths and non-wrapping "
                "flex rows, then use minmax(0, 1fr), wrapping, or a compact inline plot."
            )
        if overflow_y is not None and overflow_y > 1:
            amount_mm = overflow_y * height_mm / height
            feedback.append(
                f"Module {module_id} exceeds its own box vertically by about "
                f"{amount_mm:.0f} mm; remove constrained child heights and let its content "
                "flow intrinsically."
            )
    stack_bottoms = _stack_bottoms(geometry, poster_width=width)
    if len(stack_bottoms) >= 2:
        shallow = min(stack_bottoms, key=lambda item: item[1])
        deep = max(stack_bottoms, key=lambda item: item[1])
        if deep[1] - shallow[1] > 1.0:
            positions = ", ".join(
                f"x≈{left * width_mm / width:.0f} mm ends at "
                f"{bottom * height_mm / height:.0f} mm"
                for left, bottom in stack_bottoms
            )
            feedback.append(
                "Observed visual-stack bottoms: "
                + positions
                + ". This is measured evidence for visual review, not a requirement "
                "that independent stacks have equal height."
            )
    for item in warnings:
        if item.get("code") != "module_overlap":
            continue
        observed = item.get("observed")
        observed = observed if isinstance(observed, dict) else {}
        first = str(observed.get("module_a") or "unknown")
        second = str(observed.get("module_b") or "unknown")
        overlap_width = _positive_number(observed.get("overlap_width_px"))
        overlap_height = _positive_number(observed.get("overlap_height_px"))
        if overlap_width is None or overlap_height is None:
            continue
        feedback.append(
            f"Modules {first} and {second} overlap by about "
            f"{overlap_width * width_mm / width:.0f} × "
            f"{overlap_height * height_mm / height:.0f} mm. Separate their rendered "
            "boxes by changing whole-page placement or intrinsic flow; do not cover "
            "one module with another."
        )
    if any(item.get("code") == "missing_visible_modules" for item in warnings):
        feedback.append(
            "Scientific audit wrappers do not render measurable boxes. Keep every "
            "[data-poster-module] element visible and boxed; never apply display:none or "
            "display:contents to the audit wrapper itself. Apply display:contents only to "
            "an optional grouping parent when CSS-level placement requires it."
        )
    below_target = [
        item for item in warnings if item.get("code") == "type_below_reading_target"
    ]
    if below_target:
        comparable = [
            (observed, target, str(item.get("role") or "body"))
            for item in below_target
            if (
                (observed := _observed_font_size_mm(item)) is not None
                and (
                    target := _positive_number(
                        (item.get("target") or {}).get("minimum_mm")
                    )
                )
                is not None
            )
        ]
        if comparable:
            measurements = "; ".join(
                f"{role}: {observed:g} mm measured / {target:g} mm advisory"
                for observed, target, role in sorted(
                    comparable,
                    key=lambda values: values[0] / values[1],
                )
            )
            feedback.append(
                "Rendered type below its role-aware conference-viewing target: "
                f"{measurements}. Correct the hierarchy together instead of shrinking "
                "one role to compensate for another."
            )

    below_reference = [
        item for item in warnings if item.get("code") == "type_below_advisory_reference"
    ]
    if below_reference:
        observed = min(
            (_observed_font_size_mm(item) for item in below_reference),
            default=None,
            key=lambda value: float("inf") if value is None else value,
        )
        if observed is not None:
            feedback.append(
                f"Smallest type is {observed:g} mm, below the advisory 12 pt "
                "readability reference."
            )

    source_figures = report.get("source_figures")
    if isinstance(source_figures, dict):
        count = source_figures.get("count")
        usable_count = source_figures.get("usable_count")
        if (
            isinstance(count, int)
            and not isinstance(count, bool)
            and isinstance(usable_count, int)
            and not isinstance(usable_count, bool)
            and count > usable_count
        ):
            feedback.append(
                f"Only {usable_count} of {count} selected source figures render with "
                "valid identity, nonzero size, visibility, no clipping, and full poster "
                "containment."
            )
        readable_count = source_figures.get("readable_count")
        if (
            isinstance(usable_count, int)
            and not isinstance(usable_count, bool)
            and isinstance(readable_count, int)
            and not isinstance(readable_count, bool)
            and usable_count > readable_count
        ):
            feedback.append(
                f"{readable_count} of {usable_count} usable source figures meet the "
                "advisory figure readability measurements; compare the screenshot with "
                "the visual reference and resize or regroup only when labels are visibly "
                "hard to read."
            )
    return feedback


def _observed_font_size_mm(warning: dict[str, Any]) -> float | None:
    """Read either element-level or role-aggregated typography measurements."""

    observed = warning.get("observed")
    if not isinstance(observed, dict):
        return None
    return _positive_number(
        observed.get("minimum_font_size_mm", observed.get("font_size_mm"))
    )


def _positive_number(value: Any, *, allow_zero: bool = False) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    minimum = 0.0 if allow_zero else 1e-12
    return number if number >= minimum else None


def _stack_bottoms(
    geometry: list[dict[str, float | str]],
    *,
    poster_width: float,
) -> list[tuple[float, float]]:
    """Return deepest bottom for each narrow visual stack, ordered left to right."""

    stacks: list[list[float]] = []
    tolerance = poster_width * 0.02
    for item in sorted(geometry, key=lambda value: float(value["left"])):
        width = float(item["width"])
        if width <= 0 or width >= poster_width * 0.8:
            continue
        left = float(item["left"])
        bottom = float(item["bottom"])
        target = next(
            (stack for stack in stacks if abs(stack[0] - left) <= tolerance),
            None,
        )
        if target is None:
            stacks.append([left, bottom])
        else:
            target[1] = max(target[1], bottom)
    return [(left, bottom) for left, bottom in stacks]
