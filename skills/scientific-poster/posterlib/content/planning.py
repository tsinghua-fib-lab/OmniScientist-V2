"""Deterministic content-budget and physical page planning for posters."""

from __future__ import annotations

import copy
import math
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

import image_assets
import poster_contract

from posterlib.content.equations import (
    EquationSyntaxError,
    contains_inline_math,
    latex_to_mathml,
    validate_inline_math,
    visible_inline_math_text,
)
from posterlib.runtime.identifiers import is_module_id, is_sha256
from posterlib.sources import paper_identity, source_locators

_VISUAL_KINDS = frozenset(
    {
        "claim",
        "figure",
        "method-flow",
        "metrics",
        "comparison",
        "table",
        "text",
        "provenance",
    }
)
_WIDTHS_MM = (841.0, 914.4)
_LANDSCAPE_PAGES_MM = (
    (1189.0, 841.0),
    (1219.2, 914.4),
)
_MIN_PORTRAIT_HEIGHT_RATIO = 1.10
_MAX_PORTRAIT_HEIGHT_RATIO = 1.50
_MAX_LANDSCAPE_ASPECT_RATIO = 1.70
TARGET_OCCUPANCY = 0.88
_ADVISORY_FIT_OCCUPANCY = 1.0
_MIN_OCCUPANCY = 0.72
_FIXED_VERTICAL_OVERHEAD_RATIO = 0.15
_MIN_PAGE_DIMENSION_MM = 200.0
_MAX_PAGE_DIMENSION_MM = 2000.0
POSTER_COLUMN_GAP_MM = 14.0


class PlanningError(ValueError):
    """A content budget or requested page cannot produce a truthful poster."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class PagePlan:
    """A physical capacity recommendation with a content-integrity contract."""

    strategy: str
    width_mm: float
    height_mm: float
    min_height_mm: float
    max_height_mm: float
    orientation: str
    density_profile: str
    content_contract: dict[str, Any]
    predicted_occupancy: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation of this plan."""

        return asdict(self)


def _build_page_plan(
    *,
    strategy: str,
    width_mm: float,
    height_mm: float,
    min_height_mm: float,
    max_height_mm: float,
    orientation: str,
    budget: Mapping[str, Any],
    predicted_occupancy: float,
    reasons: tuple[str, ...],
) -> PagePlan:
    return PagePlan(
        strategy=strategy,
        width_mm=width_mm,
        height_mm=height_mm,
        min_height_mm=min_height_mm,
        max_height_mm=max_height_mm,
        orientation=orientation,
        density_profile=_density_profile(predicted_occupancy),
        content_contract=_content_contract(budget),
        predicted_occupancy=predicted_occupancy,
        reasons=reasons,
    )


def _content_contract(budget: Mapping[str, Any]) -> dict[str, Any]:
    """Bind authored modules to grounded figures and equations, without styling them."""

    raw_modules = budget.get("content_modules")
    if not isinstance(raw_modules, list) or not raw_modules:
        raise PlanningError("invalid_content_budget", "normalized modules are required")
    modules: list[dict[str, Any]] = []
    selected_figures: list[str] = []
    for raw in raw_modules:
        if not isinstance(raw, Mapping):
            raise PlanningError(
                "invalid_content_budget", "normalized modules are required"
            )
        native_components = list(raw.get("native_component_sha256s") or [])
        figure_hashes = [
            str(value) for value in raw.get("figure_sha256s") or [] if str(value)
            and value not in native_components
        ]
        equations = raw.get("equations") or []
        equation_latex = [
            str(item.get("latex") or "")
            for item in equations
            if isinstance(item, Mapping) and str(item.get("latex") or "")
        ]
        selected_figures.extend(figure_hashes)
        modules.append(
            {
                "module_id": str(raw.get("id") or ""),
                "source_figure_sha256s": figure_hashes,
                "equation_latex": equation_latex,
                **({"native_component_sha256s": native_components} if native_components else {}),
            }
        )
    return {
        "source_figure_sha256s": list(dict.fromkeys(selected_figures)),
        "modules": modules,
    }


def typography_metrics(width_mm: float) -> dict[str, float]:
    """Return minimum readable type sizes derived from physical page width."""

    width = _positive_finite(width_mm, "width_mm")
    return {
        "title_min_mm": min(26.0, max(20.0, 0.020 * width)),
        "section_heading_min_mm": min(13.0, max(10.0, 0.011 * width)),
        "body_min_mm": min(9.0, max(5.5, 0.0090 * width)),
        "body_target_mm": min(11.0, max(6.2, 0.0104 * width)),
        "provenance_min_mm": min(6.4, max(4.4, 0.0064 * width)),
    }


def poster_type_reference(width_mm: float, height_mm: float) -> dict[str, float]:
    """Suggest display sizes for both the page width and its viewing height."""

    metrics = typography_metrics(width_mm)
    height = _positive_finite(height_mm, "height_mm")
    return {
        "title_mm": round(max(metrics["title_min_mm"], 0.020 * height), 1),
        "heading_mm": round(max(metrics["section_heading_min_mm"], 0.010 * height), 1),
        "body_mm": round(max(metrics["body_target_mm"], 0.0085 * height), 1),
    }


def display_width_units(value: str) -> float:
    """Estimate glyph advance using Unicode width, independent of language."""

    width = 0.0
    for character in str(value):
        if unicodedata.combining(character):
            continue
        if character == "\t":
            width += 4.0
        elif unicodedata.east_asian_width(character) in {"F", "W"}:
            width += 2.0
        else:
            width += 1.0
    return width


def layout_capacity(width_mm: float) -> dict[str, float | int]:
    """Describe physical column limits without prescribing a layout."""

    width = _positive_finite(width_mm, "width_mm")
    printable_width = _printable_width(width)
    gutter = width * 0.025
    nominal_minimum_column_width = max(
        190.0,
        typography_metrics(width)["body_target_mm"] * 20,
    )
    minimum_column_width = min(printable_width, nominal_minimum_column_width)
    maximum_column_count = max(
        (
            count
            for count in range(2, 5)
            if (printable_width - gutter * (count - 1)) / count >= minimum_column_width
        ),
        default=1,
    )
    return {
        "maximum_readable_column_count": maximum_column_count,
        "minimum_column_width_mm": round(minimum_column_width, 1),
        "gutter_mm": round(gutter, 1),
    }


def content_capacity_hint(
    page: Mapping[str, Any] | None,
    *,
    orientation: str,
) -> Mapping[str, Any]:
    """Describe the physical selection envelope before evidence is chosen."""

    if page is not None:
        hint: dict[str, Any] = {**page}
        width = page.get("width_mm")
        height = page.get("height_mm")
        if (
            isinstance(width, (int, float))
            and not isinstance(width, bool)
            and isinstance(height, (int, float))
            and not isinstance(height, bool)
            and width > 0
            and height > 0
        ):
            capacity = layout_capacity(float(width))
            preferred_columns = 2 if float(height) >= float(width) else 3
            typical_columns = min(
                preferred_columns,
                int(capacity["maximum_readable_column_count"]),
            )
            typical_width = (
                _printable_width(float(width))
                - (typical_columns - 1) * float(capacity["gutter_mm"])
            ) / typical_columns
            typography = typography_metrics(float(width))
            hint["readable_layout_reference"] = {
                "typical_column_width_mm": round(typical_width, 1),
                "minimum_column_width_mm": float(capacity["minimum_column_width_mm"]),
                "usable_body_depth_mm": round(
                    max(160.0, float(height) - _fixed_vertical_overhead(float(width))),
                    1,
                ),
                "body_target_mm": float(typography["body_target_mm"]),
                "body_minimum_mm": float(typography["body_min_mm"]),
            }
        return hint
    normalized = str(orientation).strip().lower()
    if normalized == "landscape":
        bounds = _LANDSCAPE_PAGES_MM
    elif normalized == "portrait":
        bounds = tuple(
            (width, width * _MAX_PORTRAIT_HEIGHT_RATIO) for width in _WIDTHS_MM
        )
    elif normalized == "auto":
        landscape_bounds = _LANDSCAPE_PAGES_MM
        portrait_bounds = tuple(
            (width, width * _MAX_PORTRAIT_HEIGHT_RATIO) for width in _WIDTHS_MM
        )
        typical_column_width = max(
            _maximum_typical_column_width_mm(
                landscape_bounds, preferred_column_count=3
            ),
            _maximum_typical_column_width_mm(
                portrait_bounds, preferred_column_count=2
            ),
        )
        return {
            "strategy": "auto",
            "orientation": "auto",
            "common_page_bounds_mm": [
                {
                    "orientation": candidate_orientation,
                    "width_mm": width,
                    "max_height_mm": height,
                }
                for candidate_orientation, candidate_bounds in (
                    ("landscape", landscape_bounds),
                    ("portrait", portrait_bounds),
                )
                for width, height in candidate_bounds
            ],
            "readable_layout_reference": {
                "typical_column_width_mm": typical_column_width,
            },
        }
    else:
        raise PlanningError(
            "invalid_page",
            "orientation must be auto, portrait, or landscape",
        )
    typical_column_width = _maximum_typical_column_width_mm(
        bounds,
        preferred_column_count=3 if normalized == "landscape" else 2,
    )
    return {
        "strategy": "auto",
        "orientation": normalized,
        "common_page_bounds_mm": [
            {"width_mm": width, "max_height_mm": height} for width, height in bounds
        ],
        "readable_layout_reference": {
            "typical_column_width_mm": typical_column_width,
        },
    }


def _maximum_typical_column_width_mm(
    bounds: tuple[tuple[float, float], ...],
    *,
    preferred_column_count: int,
) -> float:
    """Return the widest typical column across the candidate page bounds."""

    widths: list[float] = []
    for width, _height in bounds:
        capacity = layout_capacity(width)
        count = min(
            preferred_column_count,
            int(capacity["maximum_readable_column_count"]),
        )
        widths.append(
            (
                _printable_width(width)
                - (count - 1) * float(capacity["gutter_mm"])
            )
            / count
        )
    return round(max(widths), 1)


def bind_prepared_figure_geometry(
    value: dict[str, Any],
    *,
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind selected figure groups to measured prepared-asset aspect ratios.

    The same helper is used for model-generated and caller-supplied budgets so page
    estimation never falls back to a square figure merely because of the input path.
    """

    bound_value = copy.deepcopy(value)
    ratios = {
        str(asset.get("content_sha256") or ""): image_assets.asset_aspect_ratio(asset)
        for asset in assets
        if (image_assets.is_source_figure_asset(asset) or asset.get("source_kind") == "native_component")
        and str(asset.get("content_sha256") or "")
    }
    modules = bound_value.get("content_modules")
    if not isinstance(modules, list):
        return bound_value
    for module in modules:
        if not isinstance(module, dict):
            continue
        raw_hashes = module.get("figure_sha256s")
        if not isinstance(raw_hashes, list) or not raw_hashes:
            module.pop("figure_aspect_ratio", None)
            continue
        selected = [ratios.get(str(item)) for item in raw_hashes]
        if any(ratio is None or ratio <= 0 for ratio in selected):
            module.pop("figure_aspect_ratio", None)
            continue
        valid = [float(ratio) for ratio in selected if ratio is not None]
        module["figure_aspect_ratio"] = round(
            len(valid) / sum(1.0 / ratio for ratio in valid),
            4,
        )
    return bound_value


def _density_profile(predicted_occupancy: float) -> str:
    """Classify visual pressure from the selected page's occupied area."""

    if predicted_occupancy < _MIN_OCCUPANCY:
        return "open"
    if predicted_occupancy <= TARGET_OCCUPANCY:
        return "balanced"
    return "dense"


def normalize_content_budget(
    value: object,
    *,
    source_figure_sha256s: set[str],
    source_equations: list[dict[str, str]] | None = None,
    source_text: str | None = None,
    source_assets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate source-grounded modules without prescribing visual sections."""

    if not isinstance(value, Mapping):
        raise PlanningError(
            "invalid_content_budget", "content budget must be an object"
        )
    unexpected_top_level = sorted(
        set(value)
        - {
            "paper_identity",
            "page_orientation",
            "organization_mode",
            "focal_role",
            "sections",
            "content_modules",
        }
    )
    if unexpected_top_level:
        raise PlanningError(
            "invalid_content_budget",
            "content budget has unsupported fields: " + ", ".join(unexpected_top_level),
        )
    page_orientation = str(value.get("page_orientation") or "").strip().lower()
    if page_orientation and page_orientation not in {"portrait", "landscape"}:
        raise PlanningError(
            "invalid_content_budget",
            "page_orientation must be portrait or landscape",
        )
    sections = _normalize_sections(value.get("sections"))
    _validate_visible_math(
        [section["label"] for section in sections],
        location="section labels",
    )
    section_ids = {item["id"] for item in sections}
    raw_modules = value.get("content_modules")
    if not isinstance(raw_modules, list) or not raw_modules:
        raise PlanningError(
            "invalid_content_budget", "content_modules must be a non-empty array"
        )
    allowed_figures = {str(item) for item in source_figure_sha256s}
    if any(not is_sha256(item) for item in allowed_figures):
        raise PlanningError(
            "invalid_content_budget", "source figure manifest is invalid"
        )
    native_components = {
        str(item["content_sha256"])
        for item in source_assets or []
        if item.get("source_kind") == "native_component"
    }
    allowed_figures.update(native_components)
    equation_catalog = source_equation_catalog(source_equations)

    modules: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_modules:
        if not isinstance(raw, Mapping):
            raise PlanningError(
                "invalid_content_budget", "each module must be an object"
            )
        unsupported_fields = sorted(
            set(raw)
            - {
                "id",
                "section_id",
                "title",
                "semantic_roles",
                "priority",
                "visual_kind",
                "text",
                "source_label",
                "figure_sha256s",
                "native_component_sha256s",
                "figure_aspect_ratio",
                "takeaway",
                "equations",
                "detail_points",
                "flow_steps",
                "table",
            }
        )
        if unsupported_fields:
            raise PlanningError(
                "invalid_content_budget",
                "content module has unsupported fields: "
                + ", ".join(unsupported_fields),
            )
        module_id = str(raw.get("id") or "").strip()
        section_id = str(raw.get("section_id") or "").strip()
        title = _normalize_copy(raw.get("title"))
        text = _normalize_copy(raw.get("text"))
        takeaway = _normalize_copy(raw.get("takeaway"))
        raw_detail_points = raw.get("detail_points")
        if raw_detail_points is None:
            detail_points = []
        elif not isinstance(raw_detail_points, list) or any(
            not isinstance(item, str) or not item.strip()
            for item in raw_detail_points
        ):
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id} detail_points must be an array of non-empty strings",
            )
        else:
            detail_points = [_normalize_copy(item) for item in raw_detail_points]
        flow_steps = _normalize_flow_steps(raw.get("flow_steps"), module_id=module_id)
        equations = _normalize_equations(
            raw.get("equations"),
            module_id=module_id,
            equation_catalog=equation_catalog,
        )
        table = _normalize_table(raw.get("table"), module_id=module_id)
        visible_math_values = [title, text, takeaway, *detail_points]
        visible_math_values.extend(
            value
            for step in flow_steps
            for value in (step["label"], step["detail"])
        )
        if table is not None:
            visible_math_values.extend(table["columns"])
            visible_math_values.extend(cell for row in table["rows"] for cell in row)
        _validate_visible_math(
            visible_math_values,
            location=f"module {module_id or '<unknown>'}",
        )
        raw_figures = raw.get("figure_sha256s")
        figures = (
            [str(item).strip() for item in raw_figures]
            if isinstance(raw_figures, list)
            else []
        )
        if any(
            not is_sha256(figure) or figure not in allowed_figures
            for figure in figures
        ):
            raise PlanningError(
                "invalid_content_budget", "module claims an unknown source figure or native component"
            )
        source_label = str(raw.get("source_label") or "").strip()
        priority = str(raw.get("priority") or "").strip()
        if priority not in poster_contract.MODULE_PRIORITIES:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id or '<unknown>'} priority must be one of: "
                + ", ".join(sorted(poster_contract.MODULE_PRIORITIES)),
            )
        raw_roles = raw.get("semantic_roles")
        roles = (
            list(dict.fromkeys(role.strip() for role in raw_roles))
            if isinstance(raw_roles, list) and all(isinstance(role, str) for role in raw_roles)
            else []
        )
        if not roles or any(role not in poster_contract.SEMANTIC_ROLES for role in roles):
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id or '<unknown>'} semantic_roles must contain supported roles",
            )
        visual_kind = str(raw.get("visual_kind") or "").strip()
        if visual_kind not in _VISUAL_KINDS:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id or '<unknown>'} visual_kind must be one of: "
                + ", ".join(sorted(_VISUAL_KINDS)),
            )
        if visual_kind == "figure" and not figures:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id or '<unknown>'} with visual_kind figure requires prepared figures",
            )
        if visual_kind == "table" and not table:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id or '<unknown>'} with visual_kind table requires table data",
            )
        if visual_kind == "method-flow" and not flow_steps:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id} with visual_kind method-flow requires flow_steps",
            )
        field_validity = {
            "id": is_module_id(module_id),
            "section_id": section_id in section_ids,
            "source_label": bool(source_label),
            "meaningful visible content": bool(
                title
                or text
                or takeaway
                or detail_points
                or flow_steps
                or figures
                or equations
                or table
            ),
        }
        invalid_fields = [
            field for field, is_valid in field_validity.items() if not is_valid
        ]
        if invalid_fields:
            module_label = module_id or f"#{len(modules) + 1}"
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_label} has invalid or missing fields: "
                + ", ".join(invalid_fields),
            )
        if module_id in seen_ids:
            raise PlanningError(
                "invalid_content_budget", "content module ids must be unique"
            )
        seen_ids.add(module_id)
        raw_aspect_ratio = raw.get("figure_aspect_ratio")
        aspect_ratio = (
            _positive_finite(
                1.0 if raw_aspect_ratio in (None, "") else raw_aspect_ratio,
                "figure_aspect_ratio",
                code="invalid_content_budget",
            )
            if figures
            else None
        )
        module = {
            "id": module_id,
            "section_id": section_id,
            "title": title,
            "semantic_roles": roles,
            "priority": priority,
            "visual_kind": visual_kind,
            "text": text,
            "source_label": source_label,
            "figure_sha256s": figures,
            "takeaway": takeaway,
            "equations": equations,
        }
        selected_components = [figure for figure in figures if figure in native_components]
        if selected_components:
            module["native_component_sha256s"] = selected_components
        if detail_points:
            module["detail_points"] = detail_points
        if flow_steps:
            module["flow_steps"] = flow_steps
        if table:
            module["table"] = table
        if aspect_ratio is not None:
            module["figure_aspect_ratio"] = aspect_ratio
        modules.append(module)

    normalized = {
        "sections": sections,
        "content_modules": modules,
    }
    if page_orientation:
        normalized["page_orientation"] = page_orientation
    if "paper_identity" in value:
        try:
            normalized["paper_identity"] = paper_identity.normalize(value["paper_identity"])
        except ValueError as exc:
            raise PlanningError("invalid_content_budget", str(exc)) from exc
    if source_text is not None:
        validate_content_sources(
            modules, source_text=source_text, source_equations=source_equations,
            source_assets=source_assets,
        )
    return normalized


def validate_content_sources(
    modules: list[dict[str, Any]],
    *,
    source_text: str,
    source_equations: list[dict[str, str]] | None = None,
    source_assets: list[dict[str, Any]] | None = None,
) -> None:
    """Check locator presence; the author interprets the supporting source."""

    locators = source_locators.SourceLocators(source_text)
    prepared_equations = {
        (item["source_label"], item["latex"])
        for item in (source_equation_catalog(source_equations) or {}).values()
    }
    prepared_figures = {
        str(item.get("content_sha256") or item.get("sha256")): str(item.get("carrier_label") or "")
        for item in source_assets or []
        if image_assets.is_source_figure_asset(item)
    }
    for module in modules:
        selected_labels = {
            prepared_figures[digest]
            for digest in module.get("figure_sha256s") or [] if digest in prepared_figures
        }
        selected_labels.update(
            label for label, latex in prepared_equations
            if latex in module.get("equation_latex", [])
            or any(equation.get("latex") == latex for equation in module.get("equations") or [])
        )
        for entry in (module, *(module.get("equations") or [])):
            label = str(entry.get("source_label") or "").strip()
            if entry is module and label in selected_labels:
                continue
            if (label, entry.get("latex")) in prepared_equations:
                continue
            if not locators.contains(label):
                raise PlanningError(
                    "invalid_content_budget",
                    f"module {module['id']}: source_label {label!r} has no matching "
                    "locator in the supplied source. Copy its actual heading, caption "
                    "label, printed equation identifier, or a short source excerpt. "
                    "Keep the label's original wording; inspect the full source "
                    "to confirm that this location supports the retained content.",
                )


def _normalize_copy(value: object) -> str:
    """Normalize prose while retaining source whitespace in embedded mathematics."""

    source = str(value or "").strip()
    return source if contains_inline_math(source) else " ".join(source.split())


def _normalize_flow_steps(value: object, *, module_id: str) -> list[dict[str, str]]:
    """Validate the semantic nodes of one model-authored method flow."""

    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise PlanningError(
            "invalid_content_budget",
            f"module {module_id} flow_steps must be a non-empty array",
        )
    steps: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {"label", "detail"}:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id} flow_steps require exactly label and detail",
            )
        label = _normalize_copy(raw.get("label"))
        detail = _normalize_copy(raw.get("detail"))
        if not label or not detail:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id} flow step label and detail must be non-empty",
            )
        steps.append({"label": label, "detail": detail})
    return steps


def _normalize_table(value: object, *, module_id: str) -> dict[str, Any] | None:
    """Keep shared headers and literal cells together through HTML and PPTX authoring."""

    if value is None:
        return None
    columns = value.get("columns") if isinstance(value, Mapping) else None
    rows = value.get("rows") if isinstance(value, Mapping) else None
    if (
        not isinstance(columns, list)
        or not columns
        or not isinstance(rows, list)
        or not rows
        or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)
    ):
        raise PlanningError(
            "invalid_content_budget",
            f"module {module_id} table requires columns and non-empty rectangular rows",
        )
    if any(not isinstance(cell, str) for row in [columns, *rows] for cell in row):
        raise PlanningError(
            "invalid_content_budget", f"module {module_id} table cells must be strings"
        )
    table = {
        "columns": [_normalize_copy(cell) for cell in columns],
        "rows": [[_normalize_copy(cell) for cell in row] for row in rows],
        **({"view": "comparison"} if value.get("view") == "comparison" else {}),
    }
    if table.get("view") == "comparison":
        comparison_values(table, module_id=module_id)
    return table


def comparison_values(table: Mapping[str, Any], *, module_id: str = "") -> list[list[Decimal]]:
    """Share numeric interpretation between planning and rendering."""

    headers, rows = table["columns"], table["rows"]
    message = (
        f"{f'module {module_id} ' if module_id else ''}comparison view requires finite nonnegative "
        "scalar value columns, each with a positive scale maximum. Keep source qualifications "
        "and units explicit. Omit table.view for a literal table, or select a coherent "
        "numeric comparison with its conditions preserved."
    )
    if len(headers) < 2 or not rows:
        raise PlanningError("invalid_content_budget", message)
    values: list[list[Decimal]] = []
    for row_index, row in enumerate(rows, start=1):
        numbers = []
        for column, cell in enumerate(row[1:], start=1):
            detail = f" Row {row_index}, column {headers[column]!r}: {cell!r}."
            if "," in cell:
                mantissa, exponent_separator, exponent = cell.lower().partition("e")
                integer, decimal_separator, fraction = mantissa.removeprefix("+").partition(".")
                groups = integer.split(",")
                if not (
                    1 <= len(groups[0]) <= 3
                    and all(group.isascii() and group.isdecimal() for group in groups)
                    and all(len(group) == 3 for group in groups[1:])
                    and (not decimal_separator or fraction.isascii() and fraction.isdecimal())
                    and (not exponent_separator or exponent.lstrip("+-").isascii() and exponent.lstrip("+-").isdecimal())
                ):
                    raise PlanningError("invalid_content_budget", message + detail)
            try:
                number = Decimal(cell.replace(",", ""))
            except InvalidOperation as exc:
                raise PlanningError("invalid_content_budget", message + detail) from exc
            if not number.is_finite() or number < 0:
                raise PlanningError("invalid_content_budget", message + detail)
            numbers.append(number)
        values.append(numbers)
    if any(max(column) == 0 for column in zip(*values, strict=True)):
        raise PlanningError("invalid_content_budget", message)
    return values


def _normalize_equations(
    value: object,
    *,
    module_id: str,
    equation_catalog: Mapping[str, Mapping[str, str]] | None,
) -> list[dict[str, str]]:
    """Validate optional source-located display equations for semantic MathML."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise PlanningError(
            "invalid_content_budget",
            f"module {module_id or '<unknown>'}: equations must be a list",
        )
    equations: list[dict[str, str]] = []
    for raw in value:
        if isinstance(raw, Mapping) and set(raw) == {"source_ref"}:
            reference = raw["source_ref"]
            selected = (
                (equation_catalog or {}).get(reference)
                if isinstance(reference, str) else None
            )
            if selected is None:
                raise PlanningError(
                    "invalid_content_budget",
                    f"module {module_id or '<unknown>'}: unknown prepared equation source_ref",
                )
            raw = {key: selected[key] for key in ("latex", "source_label")}
        if not isinstance(raw, Mapping) or set(raw) != {"latex", "source_label"}:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id or '<unknown>'}: each equation uses exactly one "
                'representation: {"source_ref": "catalog id"} alone, or '
                '{"latex": "...", "source_label": "..."} alone. '
                "Remove the other representation's fields; copy source_ref from the "
                "prepared catalog independently of the printed equation number.",
            )
        latex = str(raw.get("latex") or "").strip()
        source_label = str(raw.get("source_label") or "").strip()
        if not latex or not source_label:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id or '<unknown>'}: equation content is invalid",
            )
        try:
            latex_to_mathml(latex)
        except EquationSyntaxError as exc:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id or '<unknown>'}: equation LaTeX cannot be rendered",
            ) from exc
        equations.append({"latex": latex, "source_label": source_label})
    return equations


def _validate_visible_math(values: list[str], *, location: str) -> None:
    """Validate expressions explicitly marked as mathematics by the author."""
    for item in values:
        try:
            validate_inline_math(item)
        except EquationSyntaxError as exc:
            preview = item if len(item) <= 240 else item[:237] + "..."
            raise PlanningError(
                "invalid_content_budget",
                f"{location} contains inline LaTeX that cannot be rendered: {exc}; "
                f"offending value: {preview!r}",
            ) from exc


def source_equation_catalog(
    value: list[dict[str, str]] | None,
) -> dict[str, dict[str, str]] | None:
    """Give each optional source expression a stable transcription shortcut."""

    if value is None:
        return None
    catalog: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping):
            raise PlanningError(
                "invalid_content_budget", "prepared source equations are invalid"
            )
        latex = str(raw.get("latex") or "").strip()
        source_label = str(raw.get("source_label") or "").strip()
        if not latex or not source_label:
            raise PlanningError(
                "invalid_content_budget", "prepared source equations are invalid"
            )
        catalog[f"equation://{index}"] = {
            "latex": latex, "source_label": source_label,
            "context": str(raw.get("context") or ""),
        }
    return catalog


def _normalize_sections(value: object) -> list[dict[str, str]]:
    """Validate ordered poster scan sections without imposing a narrative arc."""

    if not isinstance(value, list) or not value:
        raise PlanningError(
            "invalid_content_budget",
            "sections must contain at least one ordered scan group",
        )
    sections: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise PlanningError(
                "invalid_content_budget", "each section must be an object"
            )
        if set(raw) != {"id", "label"}:
            raise PlanningError(
                "invalid_content_budget",
                "each section must contain exactly id and label",
            )
        section_id = str(raw.get("id") or "").strip()
        label = _normalize_copy(raw.get("label"))
        if (
            not is_module_id(section_id)
            or section_id in seen_ids
            or not label
        ):
            raise PlanningError(
                "invalid_content_budget",
                "each section needs a unique kebab-case id and short label",
            )
        seen_ids.add(section_id)
        sections.append({"id": section_id, "label": label})
    return sections


def estimate_page(
    budget: Mapping[str, Any],
    page: Mapping[str, Any] | None = None,
    *,
    orientation: str | None = None,
    figure_observations: Mapping[str, Mapping[str, str]] | None = None,
) -> PagePlan:
    """Choose a common academic page from grounded content pressure alone."""

    requested_orientation = str(orientation or "auto").strip().lower()
    if requested_orientation not in {"auto", "portrait", "landscape"}:
        raise PlanningError(
            "invalid_page",
            "orientation must be auto, portrait, or landscape",
        )
    if page is not None:
        if not isinstance(page, Mapping):
            raise PlanningError("invalid_page", "page must be an object")
        width = _positive_finite(page.get("width_mm"), "width_mm")
        height = _positive_finite(page.get("height_mm"), "height_mm")
        if not (
            _MIN_PAGE_DIMENSION_MM <= width <= _MAX_PAGE_DIMENSION_MM
            and _MIN_PAGE_DIMENSION_MM <= height <= _MAX_PAGE_DIMENSION_MM
        ):
            raise PlanningError(
                "invalid_page",
                "page dimensions must be between 200 mm and 2000 mm",
            )
        orientation = "portrait" if height >= width else "landscape"
        reserved_occupancy = _raw_occupancy_for(
            budget,
            width,
            height,
            figure_observations=figure_observations,
        )
        predicted_occupancy = reserved_occupancy
        reasons = ["Explicit physical page preserved exactly."]
        if reserved_occupancy < _MIN_OCCUPANCY:
            reasons.append(
                f"Estimated occupancy is {reserved_occupancy:.0%}, below the advisory "
                "72% planning target; review rendered space use."
            )
        elif reserved_occupancy > _ADVISORY_FIT_OCCUPANCY:
            reasons.append(
                "Estimated occupancy exceeds the advisory fit target; review rendered "
                "overflow and density."
            )
        return _build_page_plan(
            strategy="fixed",
            width_mm=width,
            height_mm=height,
            min_height_mm=height,
            max_height_mm=height,
            orientation=orientation,
            budget=budget,
            predicted_occupancy=predicted_occupancy,
            reasons=tuple(reasons),
        )

    if requested_orientation == "auto":
        requested_orientation = str(budget.get("page_orientation") or "").strip()
        if requested_orientation not in {"portrait", "landscape"}:
            raise PlanningError(
                "invalid_content_budget",
                "page_orientation is required when orientation is auto",
            )

    if requested_orientation == "landscape":
        return _estimate_adaptive_page(
            budget,
            orientation="landscape",
            page_bounds=_LANDSCAPE_PAGES_MM,
            minimum_height_ratio=1.0 / _MAX_LANDSCAPE_ASPECT_RATIO,
            occupancy_probe_height_mm=_LANDSCAPE_PAGES_MM[0][1],
            figure_observations=figure_observations,
            sizing_reason=(
                "Uses the selected landscape orientation with a common academic "
                "width and a 5 mm adaptive-height grid."
            ),
        )
    return _estimate_adaptive_page(
        budget,
        orientation="portrait",
        page_bounds=tuple(
            (width, width * _MAX_PORTRAIT_HEIGHT_RATIO) for width in _WIDTHS_MM
        ),
        minimum_height_ratio=_MIN_PORTRAIT_HEIGHT_RATIO,
        occupancy_probe_height_mm=_WIDTHS_MM[0] * _MIN_PORTRAIT_HEIGHT_RATIO,
        figure_observations=figure_observations,
        sizing_reason=(
            "Uses the selected portrait orientation with a common academic "
            "width and a 5 mm adaptive-height grid."
        ),
    )


def _estimate_adaptive_page(
    budget: Mapping[str, Any],
    *,
    orientation: str,
    page_bounds: tuple[tuple[float, float], ...],
    minimum_height_ratio: float,
    occupancy_probe_height_mm: float,
    figure_observations: Mapping[str, Mapping[str, str]] | None,
    sizing_reason: str,
) -> PagePlan:
    """Choose a bounded common page using occupancy only as a sizing heuristic."""

    smallest_width = page_bounds[0][0]
    sparse_at_smallest = (
        _raw_occupancy_for(
            budget,
            smallest_width,
            occupancy_probe_height_mm,
            figure_observations=figure_observations,
        )
        < _MIN_OCCUPANCY
    )
    candidates: list[PagePlan] = []
    required_height = 0.0
    for width, maximum_height in page_bounds:
        max_height = maximum_height
        min_height = min(
            _round_up(width * minimum_height_ratio, 5.0),
            max_height,
        )
        target_height = _required_height_for(
            budget,
            width,
            figure_observations=figure_observations,
        )
        required_height = target_height
        height = min(max(_round_up(target_height, 5.0), min_height), max_height)
        occupancy = _raw_occupancy_for(
            budget,
            width,
            height,
            figure_observations=figure_observations,
        )
        reasons = [
            (
                "Estimated from "
                f"{len(budget.get('content_modules') or [])} grounded modules."
            ),
            "Reserves intrinsic height for contiguous column groups.",
            sizing_reason,
        ]
        if sparse_at_smallest and not candidates:
            reasons.append(
                "At the standard maximum height, estimated occupancy is below the "
                "72% minimum fill guide; reduce to the smallest bounded page before "
                "rendered review."
            )
        candidate = _build_page_plan(
            strategy="auto",
            width_mm=width,
            height_mm=height,
            min_height_mm=min_height,
            max_height_mm=max_height,
            orientation=orientation,
            budget=budget,
            predicted_occupancy=occupancy,
            reasons=tuple(reasons),
        )
        candidates.append(candidate)
        if sparse_at_smallest:
            return candidate
        target_pressure = occupancy / TARGET_OCCUPANCY
        if target_pressure <= 1.0:
            return candidate
    largest = candidates[-1]
    required = _round_up(required_height, 5.0)
    return replace(
        largest,
        reasons=(
            *largest.reasons,
            (
                "Estimated occupancy exceeds the advisory fit target: the heuristic would "
                f"use about {required:g} mm of height, so the recommendation is bounded to "
                f"the largest common {orientation} page for rendered geometry review."
            ),
        ),
    )


def _meaningful_content_area(
    budget: Mapping[str, Any],
    width_mm: float,
    *,
    height_mm: float | None = None,
    figure_observations: Mapping[str, Mapping[str, str]] | None = None,
) -> float:
    """Estimate rendered text, figure, hierarchy, and spacing area in square mm."""

    modules = budget.get("content_modules")
    if not isinstance(modules, list):
        raise PlanningError("invalid_content_budget", "normalized modules are required")
    metrics = _area_metrics(width_mm, height_mm=height_mm)
    area = 0.0
    for raw in modules:
        if not isinstance(raw, Mapping):
            raise PlanningError(
                "invalid_content_budget", "normalized modules are required"
            )
        area += _module_content_area(
            raw,
            width_mm=width_mm,
            metrics=metrics,
            figure_observations=figure_observations,
        )
    return area


def _area_metrics(
    width_mm: float, *, height_mm: float | None = None
) -> dict[str, float]:
    typography = typography_metrics(width_mm)
    body = (
        poster_type_reference(width_mm, height_mm)["body_mm"]
        if height_mm is not None
        else typography["body_target_mm"]
    )
    capacity = layout_capacity(width_mm)
    minimum_readable_width = float(capacity["minimum_column_width_mm"])
    preferred_columns = 3 if height_mm is not None and width_mm > height_mm else 2
    columns = min(preferred_columns, int(capacity["maximum_readable_column_count"]))
    typical_module_width = (
        _printable_width(width_mm) - (columns - 1) * POSTER_COLUMN_GAP_MM
    ) / columns
    return {
        **typography,
        "body": body,
        "line_height": body * 1.35,
        "char_width": body * 0.52,
        "minimum_readable_width": minimum_readable_width,
        "typical_module_width": typical_module_width,
    }


def _module_content_area(
    raw: Mapping[str, Any],
    *,
    width_mm: float,
    metrics: Mapping[str, float],
    figure_observations: Mapping[str, Mapping[str, str]] | None = None,
) -> float:
    title = str(raw.get("title") or "")
    text = " ".join(str(raw.get(name) or "") for name in ("text", "takeaway"))
    detail_points = raw.get("detail_points")
    if isinstance(detail_points, list):
        text = " ".join([text, *(str(item) for item in detail_points)])
    flow_steps = raw.get("flow_steps")
    if isinstance(flow_steps, list):
        text = " ".join(
            [
                text,
                *(
                    " ".join(
                        str(step.get(field) or "") for field in ("label", "detail")
                    )
                    for step in flow_steps
                    if isinstance(step, Mapping)
                ),
            ]
        )
    table = raw.get("table")
    table_rows = [table["columns"], *table["rows"]] if table else []
    if table_rows:
        text = " ".join([text, *(cell for row in table_rows for cell in row)])
    area = (
        display_width_units(visible_inline_math_text(text))
        * metrics["char_width"]
        * metrics["line_height"]
    )
    area += (
        display_width_units(visible_inline_math_text(title))
        * metrics["section_heading_min_mm"] ** 2
        * 0.55
    )
    area += metrics["minimum_readable_width"] * metrics["body"] * 1.7
    if isinstance(flow_steps, list):
        # Each native method node owns a distinct labeled row. The text estimate
        # covers wrapping; this adds the row boundary that concatenated prose lacks.
        area += (
            len(flow_steps)
            * metrics["minimum_readable_width"]
            * metrics["line_height"]
        )
    if table_rows:
        # Text area above accounts for cell wrapping. A table row also owns one
        # physical reading line plus the padding and rhythm emitted by the renderer;
        # without that structural area, several full tables appear artificially cheap.
        table_row_depth = metrics["body"] * 1.16 + 5.0 + 1.2
        area += (
            len(table_rows)
            * metrics["typical_module_width"]
            * table_row_depth
        )
    equations = raw.get("equations")
    if isinstance(equations, list):
        area += (
            len(equations) * metrics["minimum_readable_width"] * metrics["body"] * 4.0
        )
    figure_hashes = raw.get("figure_sha256s")
    if not isinstance(figure_hashes, list) or not figure_hashes:
        return area
    raw_aspect_ratio = raw.get("figure_aspect_ratio")
    aspect_ratio = _positive_finite(
        1.0 if raw_aspect_ratio in (None, "") else raw_aspect_ratio,
        "figure_aspect_ratio",
        code="invalid_content_budget",
    )
    base_group_width = min(
        _printable_width(width_mm),
        metrics["typical_module_width"]
        * (1.15 if str(raw.get("priority") or "") == "focal" else 1.0),
    )
    width_need_rank = {"column": 0, "wide": 1, "dominant": 2}
    width_need = max(
        (
            str((figure_observations or {}).get(str(digest), {}).get("width_need") or "column")
            for digest in figure_hashes
        ),
        key=lambda value: width_need_rank.get(value, 0),
    )
    printable_width = _printable_width(width_mm)
    typical_width = metrics["typical_module_width"]
    if width_need == "dominant":
        grid_columns = 1
        group_width = printable_width
    elif len(figure_hashes) == 1:
        grid_columns = 1
        group_width = min(
            printable_width,
            base_group_width * (1.1 if width_need == "wide" else 1.0),
        )
    else:
        grid_columns = min(2, len(figure_hashes))
        group_width = min(
            printable_width,
            max(
                base_group_width,
                grid_columns * typical_width
                + (grid_columns - 1) * POSTER_COLUMN_GAP_MM,
            ),
        )
    figure_width = group_width / grid_columns
    figure_height = min(figure_width / aspect_ratio, width_mm * 0.54)
    caption_height = metrics["provenance_min_mm"] * 2.5
    return area + len(figure_hashes) * figure_width * (figure_height + caption_height)


def _raw_occupancy_for(
    budget: Mapping[str, Any],
    width_mm: float,
    height_mm: float,
    *,
    figure_observations: Mapping[str, Mapping[str, str]] | None = None,
) -> float:
    """Return uncapped content pressure for fit decisions."""

    printable_width = _printable_width(width_mm)
    printable_height = max(1.0, height_mm - _fixed_vertical_overhead(width_mm))
    return _meaningful_content_area(
        budget,
        width_mm,
        height_mm=height_mm,
        figure_observations=figure_observations,
    ) / (
        printable_width * printable_height
    )


def _required_height_for(
    budget: Mapping[str, Any],
    width_mm: float,
    *,
    figure_observations: Mapping[str, Mapping[str, str]] | None = None,
) -> float:
    """Return a coarse pre-layout height from the grounded content area."""

    content_area = _meaningful_content_area(
        budget,
        width_mm,
        figure_observations=figure_observations,
    )
    return content_area / (
        _printable_width(width_mm) * TARGET_OCCUPANCY
    ) + _fixed_vertical_overhead(width_mm)


def _printable_width(width_mm: float) -> float:
    return width_mm * 0.90


def _fixed_vertical_overhead(width_mm: float) -> float:
    """Reserve the title band, page padding, and inter-module rhythm once."""

    return width_mm * _FIXED_VERTICAL_OVERHEAD_RATIO


def _positive_finite(
    value: object,
    name: str,
    *,
    code: str = "invalid_page",
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanningError(code, f"{name} must be a positive finite number") from exc
    if not math.isfinite(number) or number <= 0:
        raise PlanningError(code, f"{name} must be a positive finite number")
    return number


def _round_up(value: float, quantum: float) -> float:
    return math.ceil(value / quantum) * quantum
