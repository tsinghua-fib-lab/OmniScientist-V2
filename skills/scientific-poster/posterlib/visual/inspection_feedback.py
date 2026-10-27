"""Pass measured browser state to the model without interpreting its layout."""

from __future__ import annotations

from itertools import pairwise
from typing import Any


def _compact_warning(value: Any) -> Any:
    """Bound repeated descendant geometry while retaining actionable owners."""

    if not isinstance(value, dict) or value.get("code") != "element_content_overflow":
        return value
    warning = dict(value)
    observed = warning.get("observed")
    if not isinstance(observed, dict):
        return warning
    observed = dict(observed)
    overflowing = observed.get("overflowing_content")
    if not isinstance(overflowing, list):
        warning["observed"] = observed
        return warning
    text_representatives: list[dict[str, Any]] = []
    visual_representatives: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[float, ...]]] = set()
    for raw in overflowing:
        if not isinstance(raw, dict):
            continue
        element = raw.get("element")
        element = element if isinstance(element, dict) else {}
        element_rect = raw.get("element_rect")
        element_rect = element_rect if isinstance(element_rect, dict) else {}
        rect_key = tuple(
            round(float(element_rect.get(field) or 0), 2)
            for field in ("left", "top", "width", "height")
        )
        key = (
            str(element.get("tag") or ""),
            str(element.get("id") or ""),
            rect_key,
        )
        if key in seen:
            continue
        seen.add(key)
        representative = {
            key: raw[key]
            for key in ("element", "element_rect", "text")
            if key in raw
        }
        text = representative.get("text")
        if isinstance(text, str) and len(text) > 240:
            representative["text"] = text[:237].rstrip() + "…"
        if isinstance(text, str) and text.strip():
            text_representatives.append(representative)
        elif str(element.get("tag") or "") in {"img", "svg", "table", "math"}:
            visual_representatives.append(representative)
    representatives = [
        *text_representatives[:3],
        *visual_representatives[: max(0, 4 - len(text_representatives[:3]))],
    ]
    observed["overflowing_content_count"] = len(overflowing)
    observed["overflowing_content"] = representatives
    warning["observed"] = observed
    return warning


def _adjacent_vertical_gaps(modules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Report measured space between vertically adjacent, overlapping modules."""

    placed = [
        (module, module["rect"])
        for module in modules
        if isinstance(module.get("rect"), dict)
    ]
    edges = sorted({
        edge
        for _, rect in placed
        for edge in (rect["left"], rect["left"] + rect["width"])
    })
    gaps: list[dict[str, Any]] = []
    regions_by_signature: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for left, right in pairwise(edges):
        middle = (left + right) / 2
        column = sorted(
            (
                (module, rect) for module, rect in placed
                if rect["left"] < middle < rect["left"] + rect["width"]
            ),
            key=lambda item: item[1]["top"],
        )
        for (upper, upper_rect), (lower, lower_rect) in pairwise(column):
            top = upper_rect["top"] + upper_rect["height"]
            height = lower_rect["top"] - top
            if height <= 0:
                continue
            above_id = upper.get("module_id")
            below_id = lower.get("module_id")
            signature = (above_id, below_id, round(top, 2), round(height, 2))
            regions = regions_by_signature.setdefault(signature, [])
            previous = regions[-1] if regions else None
            if (
                previous is not None
                and previous["rect"]["left"] + previous["rect"]["width"]
                == round(left, 2)
            ):
                previous["rect"]["width"] = round(right - previous["rect"]["left"], 2)
            else:
                region = {
                    "above_module_id": above_id,
                    "below_module_id": below_id,
                    "rect": {
                        "left": round(left, 2),
                        "top": round(top, 2),
                        "width": round(right - left, 2),
                        "height": round(height, 2),
                    },
                }
                regions.append(region)
                gaps.append(region)
    return sorted(
        gaps,
        key=lambda item: item["rect"]["width"] * item["rect"]["height"],
        reverse=True,
    )


def editor_feedback(
    inspection: dict[str, Any],
    *,
    figure_observations: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Preserve page, module, and rendered-content geometry for reflow."""

    report = inspection.get("report")
    report = report if isinstance(report, dict) else {}
    evidence = inspection.get("visual_evidence")
    observations = evidence.get("observations", []) if isinstance(evidence, dict) else []
    content_bounds_by_module: dict[str, Any] = {}
    for observation in observations:
        if (
            not isinstance(observation, dict)
            or observation.get("kind") != "module_content_extent"
        ):
            continue
        facts = observation.get("facts")
        module_id = observation.get("module_id")
        if (
            isinstance(module_id, str)
            and isinstance(facts, dict)
            and "content_bounds_px" in facts
        ):
            content_bounds_by_module[module_id] = facts["content_bounds_px"]
    result = {
        "status": inspection.get("status"),
        "outcome": inspection.get("outcome"),
        "summary": inspection.get("summary", ""),
        "report": {
            "warnings": [
                _compact_warning(item)
                for item in inspection.get("warnings", report.get("warnings", [])) or []
            ],
            **{
                key: report[key]
                for key in (
                    "poster", "title_band", "modules", "source_figures",
                    "unmatched_style_selectors",
                )
                if key in report
            },
        },
    }
    # Sibling modules share the same layout ancestors. Show each container once
    # so its governing dimensions are not buried in repeated per-module JSON.
    containers: list[dict[str, Any]] = []

    def container_index(value: dict[str, Any]) -> int:
        if value not in containers:
            containers.append(value)
        return containers.index(value)

    modules = []
    for module in result["report"].get("modules", []):
        compact = dict(module)
        module_id = module.get("module_id")
        internal = module.get("internal_space")
        if isinstance(internal, dict):
            # Keep the largest local bands; all measurements remain in the raw
            # inspection. This is a context bound, independent of visual quality.
            compact["internal_space"] = {
                **internal,
                "vertical_gaps": internal.get("vertical_gaps", [])[:3],
            }
            bounds = internal.get("content_bounds_px")
            compact["content_bounds_px"] = bounds
            compact["content_bottom_inset_px"] = internal["bottom_inset_px"]
        elif module_id in content_bounds_by_module:
            bounds = content_bounds_by_module[module_id]
            compact["content_bounds_px"] = bounds
            frame = module.get("rect")
            if isinstance(frame, dict) and isinstance(bounds, dict):
                compact["content_bottom_inset_px"] = round(max(
                    0, frame["top"] + frame["height"]
                    - bounds["top"] - bounds["height"],
                ), 2)
        if isinstance(module.get("layout"), dict):
            layout = dict(module["layout"])
            parent = {key.removeprefix("parent_"): layout.pop(key)
                      for key in list(layout) if key.startswith("parent_")}
            if parent:
                layout["parent_container"] = container_index(parent)
            chain = layout.pop("ancestor_chain", None)
            if isinstance(chain, list):
                layout["ancestor_containers"] = [container_index(item) for item in chain]
            compact["layout"] = layout
        modules.append(compact)
    result["report"]["modules"] = modules
    gaps = _adjacent_vertical_gaps(modules)
    if gaps:
        result["report"]["adjacent_vertical_gaps"] = gaps
    if containers:
        result["report"]["layout_containers"] = containers
        relations = []
        for index, container in enumerate(containers):
            children = [
                module
                for module in modules
                if module.get("layout", {}).get("parent_container") == index
            ]
            if not children:
                continue
            columns = str(container.get("grid_template_columns") or "")
            relation = {
                "container": index,
                "display": str(container.get("display") or ""),
                "child_module_ids": [module.get("module_id") for module in children],
                "auto_placed_module_ids": [
                    module.get("module_id")
                    for module in children
                    if module.get("layout", {}).get("grid_column_start") == "auto"
                    and module.get("layout", {}).get("grid_column_end") == "auto"
                ],
            }
            if relation["display"] == "grid" and columns:
                from tinycss2 import parse_component_value_list

                # Computed grid styles contain resolved tracks and optional named
                # lines. Functions stay atomic; line-name blocks are not tracks.
                relation["computed_column_track_count"] = sum(
                    token.type not in {"whitespace", "comment", "[] block"}
                    and not (token.type == "ident" and token.lower_value == "none")
                    for token in parse_component_value_list(columns)
                )
            relations.append(relation)
        if relations:
            result["report"]["layout_relations"] = relations
    figures = result["report"].get("source_figures")
    if isinstance(figures, dict):
        for figure in figures.get("details", []):
            observation = (figure_observations or {}).get(str(figure.get("sha256") or ""), {})
            poster_width = result["report"].get("poster", {}).get("width_mm")
            module_width = figure.get("module_width_mm")
            painted_width = figure.get("painted_width_mm")
            if (
                observation.get("width_need") == "dominant"
                and figure.get("visible")
                and isinstance(poster_width, (int, float))
                and isinstance(painted_width, (int, float))
                and 3 * painted_width < 2 * poster_width
            ):
                result["report"].setdefault("warnings", []).append({
                    "code": "dominant_figure_underallocated",
                    "severity": "warning",
                    "module_id": figure.get("module_id"),
                    "sha256": figure.get("sha256"),
                    "observed": {
                        "module_width_mm": module_width,
                        "painted_width_mm": figure.get("painted_width_mm"),
                        "page_width_mm": poster_width,
                    },
                    "message": (
                        "The page estimate treats this figure as full-width, but its "
                        "painted image uses less than two-thirds of the page width. Enlarge the "
                        "actual image and recompose surrounding modules to use the "
                        "reserved area."
                    ),
                })
            rect = figure.get("rect")
            painted = figure.get("painted_rect")
            if isinstance(rect, dict) and isinstance(painted, dict):
                for axis in ("width", "height"):
                    box_extent = rect.get(axis)
                    painted_extent = painted.get(axis)
                    if (
                        isinstance(box_extent, (int, float))
                        and not isinstance(box_extent, bool)
                        and box_extent > 0
                        and isinstance(painted_extent, (int, float))
                        and not isinstance(painted_extent, bool)
                    ):
                        figure[f"painted_{axis}_fraction_of_box"] = round(
                            float(painted_extent) / float(box_extent),
                            4,
                        )
            if figure.get("visible") and not figure.get("module_id"):
                result["report"].setdefault("warnings", []).append({
                    "code": "source_figure_without_evidence_role", "severity": "warning",
                    "sha256": figure.get("sha256"),
                    "painted_width_mm": figure.get("painted_width_mm"),
                    "message": (
                        "This source figure has no scientific module owner. Give it an "
                        "explicit evidence role, or remove it if it only repeats another "
                        "figure as decoration. Rendered dimensions do not certify legibility."
                    ),
                })
        result["report"]["source_figures"] = {
            key: value for key, value in figures.items() if not key.endswith("_sha256s")
        }
    type_observations = [
        item for item in observations
        if isinstance(item, dict) and item.get("kind") == "typography_distribution"
    ]
    if type_observations:
        result["diagnostics"] = {"observations": type_observations}
    result["_rendered_inspection"] = inspection
    return result
