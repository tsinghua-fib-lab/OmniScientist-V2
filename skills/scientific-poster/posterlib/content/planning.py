"""Deterministic content-budget and physical page planning for posters."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

import poster_core

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
FOCAL_ROLES = frozenset({"source-figure", "method-flow", "result", "claim"})
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
_AUDIT_ROLE_BY_VISUAL_KIND = {
    "claim": "claim",
    "figure": "evidence",
    "method-flow": "method",
    "metrics": "evidence",
    "comparison": "evidence",
    "table": "evidence",
    "text": "context",
    "provenance": "provenance",
}
_WIDTHS_MM = (594.0, 841.0, 914.4)
_LANDSCAPE_PAGES_MM = (
    (841.0, 594.0),
    (914.4, 609.6),
    (1189.0, 841.0),
    (1219.2, 914.4),
)
_MIN_PORTRAIT_HEIGHT_RATIO = 1.10
_MAX_PORTRAIT_HEIGHT_RATIO = 1.50
_MAX_LANDSCAPE_ASPECT_RATIO = 1.50
_TARGET_OCCUPANCY = 0.90
_MAX_AUTO_OCCUPANCY = 1.0
_MIN_OCCUPANCY = 0.72
_CAPACITY_PRESSURE_LIMIT = 1.15
_COMPACT_LANDSCAPE_FLOW_RESERVE = 1.12
_COMPACT_LANDSCAPE_MAX_WIDTH_MM = 1000.0
_FIXED_VERTICAL_OVERHEAD_RATIO = 0.15
_FIGURE_ORIENTATION_EXTENT_TARGET = 0.18
_MIN_PAGE_DIMENSION_MM = 200.0
_MAX_PAGE_DIMENSION_MM = 2000.0
_MAX_EQUATION_LATEX_CHARS = 600
_FIGURE_LOCATOR_RE = re.compile(
    r"\b(?:fig(?:ure)?s?|figs?\.?)\s*(\d+)(?:\s*[-\u2013\u2014]\s*(\d+))?",
    re.IGNORECASE,
)


class PlanningError(ValueError):
    """A content budget or requested page cannot produce a truthful poster."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _derive_audit_roles(
    raw_roles: object,
    *,
    visual_kind: str,
    priority: str,
    focal_role: str,
) -> list[str]:
    """Return system-owned audit metadata from the module's structural fields."""

    roles: list[str] = []
    if isinstance(raw_roles, list):
        for value in raw_roles:
            role = str(value).strip()
            if role in poster_core.SEMANTIC_ROLES and role not in roles:
                roles.append(role)

    structural_role = _AUDIT_ROLE_BY_VISUAL_KIND.get(visual_kind)
    if (
        structural_role
        and structural_role not in roles
        and not (structural_role == "context" and (roles or priority == "focal"))
    ):
        roles.append(structural_role)
    if priority == "focal":
        focal_audit_role = {
            "method-flow": "method",
            "result": "evidence",
            "source-figure": "evidence",
        }.get(focal_role)
        if focal_audit_role and focal_audit_role not in roles:
            roles.append(focal_audit_role)
        if "claim" not in roles:
            roles.append("claim")
    if priority == "footer" and "provenance" not in roles:
        roles.append("provenance")
    return roles or ["context"]


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
    focal_role: str
    layout_capacity: dict[str, float | int]
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
    focal_role: str,
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
        density_profile=_density_profile(budget),
        focal_role=focal_role,
        layout_capacity=layout_capacity(width_mm),
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
        figure_hashes = [
            str(value) for value in raw.get("figure_sha256s") or [] if str(value)
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
        "body_target_mm": min(9.6, max(5.8, 0.0096 * width)),
        "provenance_min_mm": min(5.6, max(4.0, 0.0060 * width)),
    }


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
        return {
            **page,
            "figure_readability_reference": {
                "minimum_orientation_extent_ratio": (_FIGURE_ORIENTATION_EXTENT_TARGET),
                "policy": (
                    "Select only figures that can remain interpretable at conference "
                    "viewing scale; reduce redundant figures before shrinking them."
                ),
            },
        }
    normalized = str(orientation).strip().lower()
    if normalized == "landscape":
        bounds = _LANDSCAPE_PAGES_MM
    elif normalized == "portrait":
        bounds = tuple(
            (width, width * _MAX_PORTRAIT_HEIGHT_RATIO) for width in _WIDTHS_MM
        )
    elif normalized == "auto":
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
                    ("landscape", _LANDSCAPE_PAGES_MM),
                    (
                        "portrait",
                        tuple(
                            (width, width * _MAX_PORTRAIT_HEIGHT_RATIO)
                            for width in _WIDTHS_MM
                        ),
                    ),
                )
                for width, height in candidate_bounds
            ],
            "figure_readability_reference": {
                "minimum_orientation_extent_ratio": (_FIGURE_ORIENTATION_EXTENT_TARGET),
                "policy": (
                    "Select only figures that can remain interpretable at conference "
                    "viewing scale; reduce redundant figures before shrinking them."
                ),
            },
        }
    else:
        raise PlanningError(
            "invalid_page",
            "orientation must be auto, portrait, or landscape",
        )
    return {
        "strategy": "auto",
        "orientation": normalized,
        "common_page_bounds_mm": [
            {"width_mm": width, "max_height_mm": height} for width, height in bounds
        ],
        "figure_readability_reference": {
            "minimum_orientation_extent_ratio": _FIGURE_ORIENTATION_EXTENT_TARGET,
            "policy": (
                "Select only figures that can remain interpretable at conference "
                "viewing scale; reduce redundant figures before shrinking them."
            ),
        },
    }


def module_depth_hints(
    budget: Mapping[str, Any],
    *,
    width_mm: float,
) -> list[dict[str, float | str]]:
    """Return advisory relative layout pressure for independently movable modules."""

    modules = budget.get("content_modules")
    if not isinstance(modules, list):
        raise PlanningError("invalid_content_budget", "normalized modules are required")
    if not modules:
        return []
    metrics = _area_metrics(width_mm)
    measured: list[tuple[str, float]] = []
    for raw in modules:
        if not isinstance(raw, Mapping):
            raise PlanningError(
                "invalid_content_budget", "normalized modules are required"
            )
        measured.append(
            (
                str(raw.get("id") or ""),
                _module_content_area(raw, width_mm=width_mm, metrics=metrics),
            )
        )
    mean_area = sum(area for _module_id, area in measured) / len(measured)
    return [
        {
            "module_id": module_id,
            "relative_depth": round(area / mean_area, 2),
        }
        for module_id, area in measured
    ]


def _density_profile(plan: Mapping[str, Any]) -> str:
    """Classify grounded content pressure without forcing every poster dense."""

    raw_modules = plan.get("content_modules")
    if not isinstance(raw_modules, list):
        raw_modules = []
    modules = [item for item in raw_modules if isinstance(item, Mapping)]
    pressure = len(modules)
    pressure += sum(bool(item.get("figure_sha256s")) for item in modules)
    pressure += sum(bool(item.get("equations")) for item in modules)
    if pressure <= 3:
        return "open"
    if pressure <= 7:
        return "balanced"
    return "dense"


def normalize_content_budget(
    value: object,
    *,
    source_text: str,
    source_figure_sha256s: set[str],
    source_figure_numbers: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Validate source-grounded modules without prescribing visual sections."""

    if not isinstance(value, Mapping):
        raise PlanningError(
            "invalid_content_budget", "content budget must be an object"
        )
    sections = _normalize_sections(value.get("sections"))
    section_ids = {item["id"] for item in sections}
    raw_modules = value.get("content_modules")
    if not isinstance(raw_modules, list) or not raw_modules:
        raise PlanningError(
            "invalid_content_budget", "content_modules must be a non-empty array"
        )
    organization_mode, focal_role = _presentation_hints(value, raw_modules)

    allowed_figures = {str(item) for item in source_figure_sha256s}
    if any(_HASH_RE.fullmatch(item) is None for item in allowed_figures):
        raise PlanningError(
            "invalid_content_budget", "source figure manifest is invalid"
        )

    modules: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_modules:
        if not isinstance(raw, Mapping):
            raise PlanningError(
                "invalid_content_budget", "each module must be an object"
            )
        module_id = str(raw.get("id") or "").strip()
        section_id = str(raw.get("section_id") or "").strip()
        title = " ".join(str(raw.get("title") or "").split())
        text = " ".join(str(raw.get("text") or "").split())
        takeaway = " ".join(str(raw.get("takeaway") or "").split())
        raw_detail_points = raw.get("detail_points")
        detail_points = (
            [
                normalized
                for item in raw_detail_points
                if (normalized := " ".join(str(item).split()))
            ]
            if isinstance(raw_detail_points, list)
            else []
        )
        equations = _normalize_equations(raw.get("equations"), module_id=module_id)
        raw_figures = raw.get("figure_sha256s")
        figures = (
            [str(item).strip() for item in raw_figures]
            if isinstance(raw_figures, list)
            else []
        )
        if len(figures) != len(set(figures)) or any(
            _HASH_RE.fullmatch(figure) is None or figure not in allowed_figures
            for figure in figures
        ):
            raise PlanningError(
                "invalid_content_budget", "module claims an unknown source figure"
            )
        source_label = str(raw.get("source_label") or "").strip()
        raw_priority = str(raw.get("priority") or "").strip()
        priority = (
            raw_priority
            if raw_priority in poster_core.MODULE_PRIORITIES
            else "supporting"
        )
        visual_kind = _derive_visual_kind(
            raw.get("visual_kind"),
            raw_roles=raw.get("semantic_roles"),
            figures=figures,
            equations=equations,
        )
        roles = _derive_audit_roles(
            raw.get("semantic_roles"),
            visual_kind=visual_kind,
            priority=priority,
            focal_role=focal_role,
        )
        field_validity = {
            "id": _MODULE_ID_RE.fullmatch(module_id) is not None,
            "section_id": section_id in section_ids,
            "source_label": bool(source_label),
            "meaningful visible content": bool(
                title or text or takeaway or detail_points or figures or equations
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
        _validate_figure_locator_bindings(
            module_id=module_id,
            source_label=source_label,
            figure_sha256s=figures,
            source_figure_numbers=source_figure_numbers,
        )
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
        syntax_issue = _visible_copy_syntax_issue(
            title=title,
            text=text,
            takeaway=takeaway,
            detail_points=detail_points,
        )
        if syntax_issue is not None:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id}: {syntax_issue}",
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
            "detail_points": detail_points,
            "equations": equations,
        }
        if aspect_ratio is not None:
            module["figure_aspect_ratio"] = aspect_ratio
        modules.append(module)

    _ensure_focal_module(modules, focal_role=focal_role)

    grounding_fragments = [
        {
            "text": f"{module['title']} {module['text']} {module['takeaway']}",
            "detail_points": module["detail_points"],
            "source_label": module["source_label"],
        }
        for module in modules
    ]
    grounding_fragments.extend(
        {
            "text": equation["latex"],
            "detail_points": [],
            "source_label": equation["source_label"],
        }
        for module in modules
        for equation in module["equations"]
    )
    grounding = poster_core.validate_grounded_fragments(
        grounding_fragments,
        source_text=source_text,
    )
    grounding_issues = (
        grounding.get("issues") if isinstance(grounding, dict) else grounding
    )
    if grounding_issues:
        messages = "; ".join(
            str(item.get("message") or item) if isinstance(item, Mapping) else str(item)
            for item in grounding_issues
        )
        raise PlanningError("invalid_content_budget", messages)
    return {
        "organization_mode": organization_mode,
        "focal_role": focal_role,
        "sections": sections,
        "content_modules": modules,
    }


def _ensure_focal_module(
    modules: list[dict[str, Any]],
    *,
    focal_role: str,
) -> None:
    """Promote one evidence-led focal module when the plan omitted all hierarchy."""

    if not modules or any(module["priority"] == "focal" for module in modules):
        return

    def score(module: Mapping[str, Any]) -> float:
        visual_kind = str(module.get("visual_kind") or "")
        roles = {str(role) for role in module.get("semantic_roles") or []}
        figures = len(module.get("figure_sha256s") or [])
        equations = len(module.get("equations") or [])
        value = {"primary": 2.0, "supporting": 1.0, "footer": -10.0}.get(
            str(module.get("priority") or ""),
            0.0,
        )
        if focal_role == "source-figure":
            value += figures * 2.0 + (3.0 if visual_kind == "figure" else 0.0)
        elif focal_role == "method-flow":
            value += equations * 2.0 + (
                3.0 if visual_kind == "method-flow" or "method" in roles else 0.0
            )
        elif focal_role == "result":
            value += figures + (
                3.0
                if visual_kind in {"comparison", "metrics", "table"}
                or "evidence" in roles
                else 0.0
            )
        else:
            value += 3.0 if visual_kind == "claim" or "claim" in roles else 0.0
        value += 0.5 if module.get("takeaway") else 0.0
        return value

    focal = max(modules, key=score)
    focal["priority"] = "focal"
    focal["semantic_roles"] = _derive_audit_roles(
        focal.get("semantic_roles"),
        visual_kind=str(focal["visual_kind"]),
        priority="focal",
        focal_role=focal_role,
    )


def _presentation_hints(
    value: Mapping[str, Any],
    raw_modules: list[object],
) -> tuple[str, str]:
    """Return canonical optional hints, inferring them from focal evidence as needed."""

    candidates = [item for item in raw_modules if isinstance(item, Mapping)]
    focal_candidates = [
        item
        for item in candidates
        if str(item.get("priority") or "").strip().lower() == "focal"
    ] or candidates
    roles = {
        str(role).strip().lower()
        for item in focal_candidates
        for role in (
            item.get("semantic_roles")
            if isinstance(item.get("semantic_roles"), list)
            else []
        )
        if str(role).strip()
    }
    visual_kinds = {
        str(item.get("visual_kind") or "").strip().lower() for item in focal_candidates
    }
    has_figure = any(
        isinstance(item.get("figure_sha256s"), list)
        and bool(item.get("figure_sha256s"))
        for item in focal_candidates
    )
    has_equation = any(
        isinstance(item.get("equations"), list) and bool(item.get("equations"))
        for item in focal_candidates
    )
    if has_figure or "figure" in visual_kinds:
        inferred_focal_role = "source-figure"
    elif has_equation or "method-flow" in visual_kinds or "method" in roles:
        inferred_focal_role = "method-flow"
    elif visual_kinds.intersection({"comparison", "metrics"}) or "evidence" in roles:
        inferred_focal_role = "result"
    else:
        inferred_focal_role = "claim"

    requested_focal_role = str(value.get("focal_role") or "").strip().lower()
    focal_role = (
        requested_focal_role
        if requested_focal_role in FOCAL_ROLES
        else inferred_focal_role
    )
    requested_organization_mode = (
        str(value.get("organization_mode") or "").strip().lower()
    )
    organization_mode = (
        requested_organization_mode
        if requested_organization_mode in poster_core.ORGANIZATION_MODES
        else {
            "source-figure": "figure-led",
            "method-flow": "method-led",
            "result": "result-led",
        }.get(focal_role, "scan-first")
    )
    return organization_mode, focal_role


def source_figure_number_bindings(
    assets: list[dict[str, Any]],
) -> dict[str, int]:
    """Map prepared PDF-figure hashes to their explicit paper figure numbers."""

    bindings: dict[str, int] = {}
    for asset in assets:
        if asset.get("source_kind") != "pdf_figure":
            continue
        digest = str(asset.get("content_sha256") or "").strip()
        raw_number = asset.get("figure_number")
        number_text = str(raw_number).strip() if raw_number is not None else ""
        if _HASH_RE.fullmatch(digest) and number_text.isdigit():
            bindings[digest] = int(number_text)
    return bindings


def source_figure_numbers_in_label(source_label: str) -> set[int]:
    """Return explicit ``Figure N`` references from a source locator."""

    numbers: set[int] = set()
    for match in _FIGURE_LOCATOR_RE.finditer(source_label):
        start = int(match.group(1))
        end_text = match.group(2)
        if end_text is None:
            numbers.add(start)
            continue
        end = int(end_text)
        numbers.update(range(min(start, end), max(start, end) + 1))
    return numbers


def _validate_figure_locator_bindings(
    *,
    module_id: str,
    source_label: str,
    figure_sha256s: list[str],
    source_figure_numbers: Mapping[str, int] | None,
) -> None:
    """Require provenance locators for displayed prepared figures."""

    if not figure_sha256s or not source_figure_numbers:
        return
    bound_numbers = {
        int(source_figure_numbers[digest])
        for digest in figure_sha256s
        if digest in source_figure_numbers
    }
    if not bound_numbers:
        return
    cited_numbers = source_figure_numbers_in_label(source_label)
    missing = sorted(bound_numbers - cited_numbers)
    if missing:
        shown = ", ".join(f"Figure {number}" for number in missing)
        raise PlanningError(
            "invalid_content_budget",
            f"module {module_id}: displayed prepared figure(s) are missing from "
            f"source_label provenance: {shown}",
        )


def _normalize_equations(value: object, *, module_id: str) -> list[dict[str, str]]:
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
        if not isinstance(raw, Mapping) or set(raw) != {"latex", "source_label"}:
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id or '<unknown>'}: each equation needs latex and "
                "source_label",
            )
        latex = " ".join(str(raw.get("latex") or "").split())
        source_label = str(raw.get("source_label") or "").strip()
        if (
            not latex
            or len(latex) > _MAX_EQUATION_LATEX_CHARS
            or "</" in latex
            or not source_label
        ):
            raise PlanningError(
                "invalid_content_budget",
                f"module {module_id or '<unknown>'}: equation content is invalid",
            )
        equations.append({"latex": latex, "source_label": source_label})
    return equations


def _visible_copy_syntax_issue(
    *,
    title: str,
    text: str,
    takeaway: str,
    detail_points: list[str],
) -> str | None:
    """Catch truncated visible copy before it reaches HTML authoring."""

    fields = {
        "title": title,
        "text": text,
        "takeaway": takeaway,
        **{
            f"detail_points[{index}]": point
            for index, point in enumerate(detail_points)
        },
    }
    for field, value in fields.items():
        if value.count("(") != value.count(")"):
            return f"field {field} has unbalanced parentheses"
        if value.count("[") != value.count("]"):
            return f"field {field} has unbalanced brackets"
    return None


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
        section_id = str(raw.get("id") or "").strip()
        label = " ".join(str(raw.get("label") or "").split())
        if (
            _MODULE_ID_RE.fullmatch(section_id) is None
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


def _derive_visual_kind(
    value: object,
    *,
    raw_roles: object,
    figures: list[str],
    equations: list[dict[str, str]],
) -> str:
    """Return an optional presentation hint inferred from scientific content."""

    explicit = str(value or "").strip()
    if explicit in _VISUAL_KINDS:
        return explicit
    if figures:
        return "figure"
    if equations:
        return "method-flow"
    roles = (
        {str(role).strip() for role in raw_roles if str(role).strip()}
        if isinstance(raw_roles, list)
        else set()
    )
    if "provenance" in roles:
        return "provenance"
    if "method" in roles:
        return "method-flow"
    if "evidence" in roles:
        return "comparison"
    if "claim" in roles:
        return "claim"
    return "text"


def estimate_page(
    budget: Mapping[str, Any],
    page: Mapping[str, Any] | None = None,
    *,
    orientation: str | None = None,
) -> PagePlan:
    """Choose a common academic page from grounded content pressure alone."""

    focal_role = str(budget.get("focal_role") or "claim")
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
        reserved_occupancy = _reserved_occupancy_for(budget, width, height)
        predicted_occupancy = min(1.0, reserved_occupancy)
        reasons = ["Explicit physical page preserved exactly."]
        if reserved_occupancy < _MIN_OCCUPANCY:
            reasons.append(
                f"Estimated occupancy is {reserved_occupancy:.0%}, below the advisory "
                "72% planning target; review rendered space use."
            )
        elif reserved_occupancy > _MAX_AUTO_OCCUPANCY:
            reasons.append(
                "Estimated occupancy exceeds the advisory fit target; review rendered "
                "overflow and density."
            )
        plan = _build_page_plan(
            strategy="fixed",
            width_mm=width,
            height_mm=height,
            min_height_mm=height,
            max_height_mm=height,
            orientation=orientation,
            focal_role=focal_role,
            budget=budget,
            predicted_occupancy=predicted_occupancy,
            reasons=tuple(reasons),
        )
        validate_content_capacity(
            budget,
            width_mm=plan.width_mm,
            height_mm=plan.height_mm,
        )
        return plan

    landscape = _prefer_standard_landscape_height(
        budget,
        _estimate_adaptive_page(
            budget,
            orientation="landscape",
            page_bounds=_LANDSCAPE_PAGES_MM,
            minimum_height_ratio=1.0 / _MAX_LANDSCAPE_ASPECT_RATIO,
            occupancy_probe_height_mm=_LANDSCAPE_PAGES_MM[0][1],
            focal_role=focal_role,
            sizing_reason=(
                "Uses a common landscape academic width with a 5 mm adaptive-height grid."
            ),
        ),
    )
    if requested_orientation == "landscape":
        return landscape

    portrait = _estimate_adaptive_page(
        budget,
        orientation="portrait",
        page_bounds=tuple(
            (width, width * _MAX_PORTRAIT_HEIGHT_RATIO) for width in _WIDTHS_MM
        ),
        minimum_height_ratio=_MIN_PORTRAIT_HEIGHT_RATIO,
        occupancy_probe_height_mm=_WIDTHS_MM[0] * _MIN_PORTRAIT_HEIGHT_RATIO,
        focal_role=focal_role,
        sizing_reason="Uses a common academic width with a 5 mm adaptive-height grid.",
    )
    if requested_orientation == "portrait":
        return portrait

    orientation_candidates = (landscape, portrait)
    target_capacity_candidates = tuple(
        candidate
        for candidate in orientation_candidates
        if _capacity_pressure_for(
            budget,
            candidate.width_mm,
            candidate.height_mm,
        )
        <= 1.0
    )
    chosen = min(
        target_capacity_candidates or orientation_candidates,
        key=lambda candidate: (
            _auto_orientation_score(budget, candidate),
            0 if candidate.orientation == "landscape" else 1,
        ),
    )
    plan = replace(
        chosen,
        reasons=(
            *chosen.reasons,
            "Compared common landscape and portrait bounds using predicted overflow, "
            "occupancy, page area, and source-figure aspect ratios.",
        ),
    )
    return plan


def validate_content_capacity(
    budget: Mapping[str, Any],
    *,
    width_mm: float,
    height_mm: float,
) -> float:
    """Reject only evidence budgets that materially exceed a physical page.

    Text-flow and browser geometry are estimates, so pressure up to fifteen percent
    beyond the nominal printable area remains an authoring concern. Larger excess is
    a semantic planning failure: layout cannot fix it without sacrificing readable
    type, figure scale, or grounded content.
    """

    width = _positive_finite(width_mm, "width_mm")
    height = _positive_finite(height_mm, "height_mm")
    pressure = _capacity_pressure_for(budget, width, height)
    if pressure <= _CAPACITY_PRESSURE_LIMIT:
        return pressure

    raise _content_capacity_exceeded_error(
        budget,
        width_mm=width,
        height_mm=height,
        pressure=pressure,
        capacity_context=(
            "The estimator already allows 15% headroom for approximate text flow "
            "and browser geometry."
        ),
    )


def _content_capacity_exceeded_error(
    budget: Mapping[str, Any],
    *,
    width_mm: float,
    height_mm: float,
    pressure: float,
    capacity_context: str,
) -> PlanningError:
    """Build the shared semantic-repair error for an overfull evidence budget."""

    required_height = _round_up(
        _required_height_for(
            budget,
            width_mm,
            orientation="landscape" if width_mm > height_mm else "portrait",
        ),
        5.0,
    )
    return PlanningError(
        "content_capacity_exceeded",
        "evidence budget materially exceeds the selected one-page capacity: "
        f"estimated pressure is {pressure:.1%} of the readable planning target and "
        f"would need about {required_height:g} mm height at readable planning density, "
        f"versus {height_mm:g} mm available on the {width_mm:g} × {height_mm:g} mm "
        f"page. {capacity_context} Revise the evidence semantics before authoring: "
        "preserve the main claim, essential method, and decisive evidence while "
        "removing repetition and secondary material; do not mechanically truncate or "
        "invent content.",
    )


def _prefer_standard_landscape_height(
    budget: Mapping[str, Any],
    plan: PagePlan,
) -> PagePlan:
    """Start on the full common format while retaining bounded later adaptation."""

    standard_height = next(
        (
            height
            for width, height in _LANDSCAPE_PAGES_MM
            if math.isclose(width, plan.width_mm, abs_tol=0.01)
        ),
        plan.height_mm,
    )
    if math.isclose(standard_height, plan.height_mm, abs_tol=0.01):
        return plan
    return replace(
        plan,
        height_mm=standard_height,
        predicted_occupancy=min(
            1.0,
            _reserved_occupancy_for(
                budget,
                plan.width_mm,
                standard_height,
            ),
        ),
        reasons=(
            *plan.reasons,
            "Starts on the full common landscape format; visual review may shorten "
            "only within the existing common-proportion bounds.",
        ),
    )


def _auto_orientation_score(
    budget: Mapping[str, Any],
    plan: PagePlan,
) -> float:
    """Rank an adaptive orientation without prescribing a fixed poster scaffold."""

    occupancy = _reserved_occupancy_for(
        budget,
        plan.width_mm,
        plan.height_mm,
    )
    overflow_penalty = max(0.0, occupancy - 1.0) * 8.0
    underfill_penalty = max(0.0, _MIN_OCCUPANCY - occupancy) * 2.0
    area_penalty = plan.width_mm * plan.height_mm / 10_000_000.0
    aspect_ratios = [
        float(raw.get("figure_aspect_ratio"))
        for raw in budget.get("content_modules", [])
        if isinstance(raw, Mapping)
        and isinstance(raw.get("figure_aspect_ratio"), (int, float))
        and not isinstance(raw.get("figure_aspect_ratio"), bool)
        and float(raw.get("figure_aspect_ratio")) > 0
        and raw.get("figure_sha256s")
    ]
    aspect_penalty = 0.0
    if aspect_ratios:
        mean_log_aspect = sum(math.log(value) for value in aspect_ratios) / len(
            aspect_ratios
        )
        if plan.orientation == "landscape":
            aspect_penalty = max(0.0, -mean_log_aspect) * 0.5
        else:
            aspect_penalty = max(0.0, mean_log_aspect) * 0.5
    return overflow_penalty + underfill_penalty + area_penalty + aspect_penalty


def _estimate_adaptive_page(
    budget: Mapping[str, Any],
    *,
    orientation: str,
    page_bounds: tuple[tuple[float, float], ...],
    minimum_height_ratio: float,
    occupancy_probe_height_mm: float,
    focal_role: str,
    sizing_reason: str,
) -> PagePlan:
    """Choose a bounded common page using occupancy only as a sizing heuristic."""

    smallest_width = page_bounds[0][0]
    sparse_at_smallest = (
        _occupancy_for(budget, smallest_width, occupancy_probe_height_mm)
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
        flow_reserve = _flow_reserve_for(width, orientation=orientation)
        target_height = _required_height_for(
            budget,
            width,
            orientation=orientation,
        )
        required_height = target_height
        height = min(max(_round_up(target_height, 5.0), min_height), max_height)
        raw_occupancy = _raw_occupancy_for(budget, width, height)
        flow_occupancy = raw_occupancy * flow_reserve
        occupancy = min(1.0, flow_occupancy)
        reasons = [
            "Estimated from "
            f"{len(budget.get('content_modules') or [])} grounded modules.",
            "Reserves intrinsic height for contiguous column groups.",
            sizing_reason,
        ]
        raw_modules = budget.get("content_modules")
        if (
            isinstance(raw_modules, list)
            and _grouped_figure_flow_reserve(raw_modules) > 1.0
        ):
            reasons.append(
                "Reserves flow depth for grouped figures that cannot pack as one "
                "continuous area."
            )
        if sparse_at_smallest and not candidates:
            reasons.append(
                "Estimated occupancy is below the advisory 72% planning target; the "
                "smallest bounded page is recommended for rendered review."
            )
        candidates.append(
            _build_page_plan(
                strategy="auto",
                width_mm=width,
                height_mm=height,
                min_height_mm=min_height,
                max_height_mm=max_height,
                orientation=orientation,
                focal_role=focal_role,
                budget=budget,
                predicted_occupancy=occupancy,
                reasons=tuple(reasons),
            )
        )
        if sparse_at_smallest:
            return candidates[0]
        target_pressure = flow_occupancy / _TARGET_OCCUPANCY
        if target_pressure <= 1.0:
            return candidates[-1]
    largest = candidates[-1]
    required = _round_up(required_height, 5.0)
    return replace(
        largest,
        reasons=(
            *largest.reasons,
            "Estimated occupancy exceeds the advisory fit target: the heuristic would "
            f"use about {required:g} mm of height, so the recommendation is bounded to "
            f"the largest common {orientation} page for rendered geometry review.",
        ),
    )


def _meaningful_content_area(budget: Mapping[str, Any], width_mm: float) -> float:
    """Estimate rendered text, figure, hierarchy, and spacing area in square mm."""

    modules = budget.get("content_modules")
    if not isinstance(modules, list):
        raise PlanningError("invalid_content_budget", "normalized modules are required")
    printable_width = _printable_width(width_mm)
    metrics = _area_metrics(width_mm)
    area = 0.0
    for raw in modules:
        if not isinstance(raw, Mapping):
            raise PlanningError(
                "invalid_content_budget", "normalized modules are required"
            )
        area += _module_content_area(raw, width_mm=width_mm, metrics=metrics)
    if not any(
        isinstance(module, Mapping) and module.get("figure_sha256s")
        for module in modules
    ):
        focal_area_ratio = {
            "method-flow": 0.32,
            "result": 0.34,
            "claim": 0.24,
        }.get(str(budget.get("focal_role") or ""), 0.0)
        area += printable_width * width_mm * focal_area_ratio
    return area * _grouped_figure_flow_reserve(modules)


def _area_metrics(width_mm: float) -> dict[str, float]:
    typography = typography_metrics(width_mm)
    body = typography["body_min_mm"]
    minimum_readable_width = float(layout_capacity(width_mm)["minimum_column_width_mm"])
    return {
        **typography,
        "body": body,
        "line_height": body * 1.35,
        "char_width": body * 0.52,
        "minimum_readable_width": minimum_readable_width,
        "minimum_readable_figure_width": max(
            minimum_readable_width,
            width_mm * _FIGURE_ORIENTATION_EXTENT_TARGET,
        ),
    }


def _module_content_area(
    raw: Mapping[str, Any],
    *,
    width_mm: float,
    metrics: Mapping[str, float],
) -> float:
    title = str(raw.get("title") or "")
    text = " ".join(str(raw.get(name) or "") for name in ("text", "takeaway"))
    detail_points = raw.get("detail_points")
    if isinstance(detail_points, list):
        text = " ".join([text, *(str(item) for item in detail_points)])
    area = len(text) * metrics["char_width"] * metrics["line_height"]
    area += len(title) * metrics["section_heading_min_mm"] ** 2 * 0.55
    area += metrics["minimum_readable_width"] * metrics["body"] * 1.7
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
    figure_width = min(
        _printable_width(width_mm),
        metrics["minimum_readable_figure_width"]
        * (1.45 if str(raw.get("priority") or "") == "focal" else 1.0),
    )
    figure_height = min(figure_width / aspect_ratio, width_mm * 0.54)
    caption_height = metrics["provenance_min_mm"] * 2.5
    return area + len(figure_hashes) * figure_width * (figure_height + caption_height)


def _grouped_figure_flow_reserve(modules: list[Any]) -> float:
    """Account for atomic-module packing loss and grouped figure depth."""

    additional_figures = 0
    module_count = 0
    for module in modules:
        if not isinstance(module, Mapping):
            continue
        module_count += 1
        raw_hashes = module.get("figure_sha256s")
        if isinstance(raw_hashes, list):
            additional_figures += max(0, len(raw_hashes) - 1)
    atomic_packing = 0.10 if module_count > 1 else 0.0
    return 1.0 + min(0.30, atomic_packing + additional_figures * 0.06)


def _occupancy_for(
    budget: Mapping[str, Any], width_mm: float, height_mm: float
) -> float:
    return min(1.0, _reserved_occupancy_for(budget, width_mm, height_mm))


def _raw_occupancy_for(
    budget: Mapping[str, Any], width_mm: float, height_mm: float
) -> float:
    """Return uncapped content pressure for fit decisions."""

    printable_width = _printable_width(width_mm)
    printable_height = max(1.0, height_mm - _fixed_vertical_overhead(width_mm))
    return _meaningful_content_area(budget, width_mm) / (
        printable_width * printable_height
    )


def _reserved_occupancy_for(
    budget: Mapping[str, Any],
    width_mm: float,
    height_mm: float,
) -> float:
    """Return content occupancy including compact-landscape packing loss."""

    orientation = "landscape" if width_mm > height_mm else "portrait"
    return _raw_occupancy_for(budget, width_mm, height_mm) * _flow_reserve_for(
        width_mm,
        orientation=orientation,
    )


def _capacity_pressure_for(
    budget: Mapping[str, Any],
    width_mm: float,
    height_mm: float,
) -> float:
    """Return reserve-aware pressure relative to the readable density target."""

    return _reserved_occupancy_for(budget, width_mm, height_mm) / _TARGET_OCCUPANCY


def _required_height_for(
    budget: Mapping[str, Any],
    width_mm: float,
    *,
    orientation: str,
) -> float:
    """Return height needed at the target density after packing reserve."""

    content_area = _meaningful_content_area(budget, width_mm)
    flow_reserve = _flow_reserve_for(width_mm, orientation=orientation)
    return content_area * flow_reserve / (
        _printable_width(width_mm) * _TARGET_OCCUPANCY
    ) + _fixed_vertical_overhead(width_mm)


def _flow_reserve_for(width_mm: float, *, orientation: str) -> float:
    """Return extra packing depth for compact landscape module groups."""

    if orientation == "landscape" and width_mm < _COMPACT_LANDSCAPE_MAX_WIDTH_MM:
        return _COMPACT_LANDSCAPE_FLOW_RESERVE
    return 1.0


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
