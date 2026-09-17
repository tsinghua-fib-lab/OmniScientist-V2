"""Reference-aware visual design planning before HTML authoring."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from . import reference_seeds, vlm_client

SCHEMA = "scientific-poster.visual-design.v3"
_LEGACY_SCHEMA = "scientific-poster.visual-design.v2"

PlannerKind = Literal["adaptive", "vlm"]
LeadRole = Literal["none", "focal", "method"]
LeadPlacement = Literal["flow", "full-width", "center-lane"]
SectionStyle = Literal["open", "rule", "band", "outlined"]
ModuleStyle = Literal["open", "outlined", "selective"]
TypographyProfile = Literal["sans", "serif", "hybrid"]
SpacingProfile = Literal["compact", "balanced", "open"]
MastheadScale = Literal["compact", "balanced", "prominent"]
MastheadAlignment = Literal["left", "center"]
FigureEmphasis = Literal["copy-led", "balanced", "figure-led"]

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_PALETTE_ORDER = ("background", "surface", "ink", "muted", "accent")
_PALETTE_KEYS = frozenset(_PALETTE_ORDER)
_MIN_TEXT_CONTRAST = 4.5
_PREFERENCE_FIELDS = frozenset({"typography", "framing", "accent_color"})
_RESPONSE_FIELDS = frozenset({"typography", "palette", "layout"})
_LEGACY_PLAN_FIELDS = frozenset(
    {
        "archetype",
        "topology",
        "density",
        "section_treatment",
        "focal_strategy",
        "figure_strategy",
        "reading_path",
        "reference_observations",
        "directives",
    }
)


class VisualDesignError(ValueError):
    """A reference or VLM response cannot produce a safe visual design plan."""


@dataclass(frozen=True)
class LayoutGrammar:
    """Small executable layout vocabulary extracted from any reference image."""

    column_weights: tuple[float, ...] = ()
    lead_role: LeadRole = "focal"
    lead_placement: LeadPlacement = "flow"
    section_style: SectionStyle = "rule"
    module_style: ModuleStyle = "selective"
    spacing: SpacingProfile = "balanced"
    masthead_scale: MastheadScale = "balanced"
    masthead_alignment: MastheadAlignment = "left"
    figure_emphasis: FigureEmphasis = "balanced"

    def __post_init__(self) -> None:
        if len(self.column_weights) > 4:
            raise VisualDesignError(
                "layout column_weights must contain at most 4 lanes"
            )
        if any(
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not 0.25 <= float(weight) <= 4.0
            for weight in self.column_weights
        ):
            raise VisualDesignError("layout column_weights are invalid")
        if self.lead_role not in {"none", "focal", "method"}:
            raise VisualDesignError("layout lead_role is invalid")
        if self.lead_placement not in {"flow", "full-width", "center-lane"}:
            raise VisualDesignError("layout lead_placement is invalid")
        if self.section_style not in {"open", "rule", "band", "outlined"}:
            raise VisualDesignError("layout section_style is invalid")
        if self.module_style not in {"open", "outlined", "selective"}:
            raise VisualDesignError("layout module_style is invalid")
        if self.spacing not in {"compact", "balanced", "open"}:
            raise VisualDesignError("layout spacing is invalid")
        if self.masthead_scale not in {"compact", "balanced", "prominent"}:
            raise VisualDesignError("layout masthead_scale is invalid")
        if self.masthead_alignment not in {"left", "center"}:
            raise VisualDesignError("layout masthead_alignment is invalid")
        if self.figure_emphasis not in {"copy-led", "balanced", "figure-led"}:
            raise VisualDesignError("layout figure_emphasis is invalid")

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe executable layout contract."""

        return {
            "column_weights": [float(weight) for weight in self.column_weights],
            "lead_role": self.lead_role,
            "lead_placement": self.lead_placement,
            "section_style": self.section_style,
            "module_style": self.module_style,
            "spacing": self.spacing,
            "masthead_scale": self.masthead_scale,
            "masthead_alignment": self.masthead_alignment,
            "figure_emphasis": self.figure_emphasis,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LayoutGrammar:
        """Restore one executable layout contract without reference-specific logic."""

        common = {
            "column_weights",
            "lead_role",
            "lead_placement",
            "section_style",
            "module_style",
            "spacing",
            "masthead_scale",
            "masthead_alignment",
            "figure_emphasis",
        }
        required = common - {
            "masthead_alignment",
            "spacing",
            "masthead_scale",
            "figure_emphasis",
        }
        if not required.issubset(value) or not set(value).issubset(common):
            raise VisualDesignError("layout fields are invalid")
        weights = value.get("column_weights")
        if not isinstance(weights, list):
            raise VisualDesignError("layout column_weights must be an array")
        return cls(
            column_weights=tuple(float(weight) for weight in weights),
            lead_role=str(value["lead_role"]),  # type: ignore[arg-type]
            lead_placement=str(value["lead_placement"]),  # type: ignore[arg-type]
            section_style=str(value["section_style"]),  # type: ignore[arg-type]
            module_style=str(value["module_style"]),  # type: ignore[arg-type]
            spacing=str(value.get("spacing", "balanced")),  # type: ignore[arg-type]
            masthead_scale=str(  # type: ignore[arg-type]
                value.get("masthead_scale", "balanced")
            ),
            masthead_alignment=str(  # type: ignore[arg-type]
                value.get("masthead_alignment", "left")
            ),
            figure_emphasis=str(  # type: ignore[arg-type]
                value.get("figure_emphasis", "balanced")
            ),
        )


@dataclass(frozen=True)
class VisualDesignPlan:
    """The complete executable visual contract shared by authoring and review."""

    reference_image_sha256: str
    planner: PlannerKind
    typography: TypographyProfile
    palette: dict[str, str]
    layout: LayoutGrammar = field(default_factory=LayoutGrammar)
    planner_model: str | None = None
    warning: str | None = None
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise VisualDesignError("visual design schema is invalid")
        if self.planner not in {"adaptive", "vlm"}:
            raise VisualDesignError("visual design planner is invalid")
        _typography_profile(self.typography)
        if set(self.palette) != _PALETTE_KEYS or any(
            _HEX_COLOR_RE.fullmatch(str(value)) is None
            for value in self.palette.values()
        ):
            raise VisualDesignError("visual design palette is invalid")
        if not isinstance(self.layout, LayoutGrammar):
            raise VisualDesignError("visual design layout is invalid")

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-safe plan."""

        value = asdict(self)
        value["layout"] = self.layout.to_dict()
        return value

    def executable_dict(self) -> dict[str, Any]:
        """Return only fields the renderer and a CSS repair may execute."""

        return {
            "typography": self.typography,
            "palette": dict(self.palette),
            "layout": self.layout.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VisualDesignPlan:
        """Restore a checkpointed visual design plan with exact fields."""

        expected = set(cls.__dataclass_fields__)
        fields = frozenset(value)
        current_without_layout = expected - {"layout"}
        legacy_fields = expected | _LEGACY_PLAN_FIELDS
        legacy_without_layout = legacy_fields - {"layout"}
        if fields not in {
            frozenset(expected),
            frozenset(current_without_layout),
            frozenset(legacy_fields),
            frozenset(legacy_without_layout),
        }:
            raise VisualDesignError("visual design plan fields are invalid")
        palette = value.get("palette")
        if not isinstance(palette, Mapping):
            raise VisualDesignError("visual design plan collections are invalid")
        schema = str(value["schema"])
        if schema not in {SCHEMA, _LEGACY_SCHEMA}:
            raise VisualDesignError("visual design schema is invalid")
        planner = str(value["planner"])
        if planner == "seed" and schema == _LEGACY_SCHEMA:
            planner = "adaptive"
        return cls(
            schema=SCHEMA,
            reference_image_sha256=str(value["reference_image_sha256"]),
            planner=planner,  # type: ignore[arg-type]
            typography=str(value["typography"]),  # type: ignore[arg-type]
            palette=_functional_palette(
                {str(key): str(item) for key, item in palette.items()}
            ),
            layout=(
                LayoutGrammar.from_dict(value["layout"])
                if isinstance(value.get("layout"), Mapping)
                else LayoutGrammar()
            ),
            planner_model=(
                None if value["planner_model"] is None else str(value["planner_model"])
            ),
            warning=None if value["warning"] is None else str(value["warning"]),
        )


async def plan_visual_design(
    reference: reference_seeds.ReferenceBundle,
    *,
    content_budget: Mapping[str, Any],
    page_plan: Mapping[str, Any],
    client: Any | None = None,
    preferences: Mapping[str, Any] | None = None,
    deadline: float | None = None,
) -> VisualDesignPlan:
    """Extract reference grammar once; adapt to content only on fallback/render."""

    normalized_preferences = normalize_preferences(preferences)
    image_path = Path(reference.image_path)
    image_bytes = image_path.read_bytes()
    if hashlib.sha256(image_bytes).hexdigest() != reference.image_sha256:
        raise VisualDesignError("reference image changed before visual design planning")
    adaptive_fallback = _reference_fallback_plan(
        reference,
        content_budget=content_budget,
        page_plan=page_plan,
    )
    if reference.source_kind == "seed":
        # Built-in seeds already carry the executable grammar represented by their
        # pixels. Re-inferring those same bounded fields with a VLM adds latency and
        # can contradict the registered lane geometry; pixel interpretation remains
        # necessary for custom and generated references that have no such metadata.
        return _apply_preferences(adaptive_fallback, normalized_preferences)
    extraction_fallback = _reference_fallback_plan(reference)
    fallback_description = "content-adaptive layout grammar"
    if client is None:
        return replace(
            _apply_preferences(adaptive_fallback, normalized_preferences),
            warning=(
                "Reference pixels could not be interpreted because no VLM client was "
                f"available; using {fallback_description}."
            ),
        )

    mime_type = mimetypes.guess_type(image_path.name)[0] or ""
    if not mime_type.startswith("image/"):
        raise VisualDesignError("reference image has no recognized image type")
    image = vlm_client.VlmImage(
        label=(
            "VISUAL REFERENCE — extract layout grammar only; never copy its scientific "
            "content, identity, logos, or figures."
        ),
        image_bytes=image_bytes,
        mime_type=mime_type,
    )
    images = (image,)
    prompt = _preflight_prompt()
    try:
        raw = await _generate_preflight(
            client,
            prompt,
            images=images,
            deadline=deadline,
        )
        try:
            response = _parse_response(raw)
            plan = _plan_from_response(
                response,
                reference_sha256=reference.image_sha256,
                model=str(getattr(client, "model", "") or "unknown"),
                fallback=extraction_fallback,
            )
        except VisualDesignError as exc:
            repair_prompt = _preflight_repair_prompt(
                prompt,
                validation_error=str(exc),
                previous_response=raw,
            )
            repaired_raw = await _generate_preflight(
                client,
                repair_prompt,
                images=images,
                deadline=deadline,
            )
            response = _parse_response(repaired_raw)
            plan = _plan_from_response(
                response,
                reference_sha256=reference.image_sha256,
                model=str(getattr(client, "model", "") or "unknown"),
                fallback=extraction_fallback,
            )
        return _apply_preferences(plan, normalized_preferences)
    except (
        TimeoutError,
        VisualDesignError,
        vlm_client.VlmError,
        OSError,
        ValueError,
    ) as exc:
        last_error = exc
    fallback = _apply_preferences(adaptive_fallback, normalized_preferences)
    return replace(
        fallback,
        warning=(
            "Visual design preflight was unavailable after a bounded retry; using "
            f"{fallback_description} rather than guessing from uninspected reference pixels "
            f"({type(last_error).__name__}: {last_error})."
        ),
    )


async def _generate_preflight(
    client: Any,
    prompt: str,
    *,
    images: tuple[vlm_client.VlmImage, ...],
    deadline: float | None,
) -> Any:
    """Generate within the caller's original absolute preflight deadline."""

    if deadline is None:
        return await client.generate_json_text(prompt, images=images)
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("visual design preflight budget was exhausted")
    async with asyncio.timeout(remaining):
        return await client.generate_json_text(prompt, images=images)


def _preflight_repair_prompt(
    original_prompt: str,
    *,
    validation_error: str,
    previous_response: Any,
) -> str:
    """Request one contract repair without changing evidence or design intent."""

    return "\n".join(
        [
            original_prompt,
            "",
            "CORRECTIVE RETRY: the previous JSON failed contract validation.",
            f"Exact validation error: {validation_error}",
            (
                "Return one corrected JSON object only, using the exact field and type "
                "contract in the original request. Do not copy a prior layout value "
                "merely to satisfy the schema."
            ),
            "Preserve valid executable fields; repair only the contract-invalid fields.",
            "Previous invalid JSON:",
            vlm_client.bounded_response_excerpt(previous_response),
        ]
    )


def _preflight_prompt() -> str:
    """Ask for pixel-grounded grammar without leaking paper-specific planning."""

    return """Act as a visual-grammar analyst for a top-conference academic poster.
Inspect only the attached reference pixels.
Extract only transferable visual choices that the deterministic renderer can execute: body-lane proportions, scientific lead placement, group and module framing, group-header treatment, spacing, masthead scale and alignment, figure-to-copy emphasis, typography family relationship, and palette roles.
Never copy reference text, claims, numbers, equations, figures, logos, authors, affiliations, citations, or venue identity.
Do not infer or optimize for any unseen paper, module list, user request, or target page. A downstream deterministic resolver owns that adaptation.
Measure the reference's visible scientific-body lanes precisely. Do not infer a target paper structure or add descriptive design prose.
Treat the masthead, scientific body, and footer as separate regions. A full-width title, author row, logo strip, or footer does not imply a full-width scientific lead module. Use layout.lead_placement=full-width only when one scientific claim, method, or evidence group below the masthead visibly spans multiple body lanes.
Do not upgrade a dated or generic visual idiom into a recommendation merely because it is visible. State what it does to hierarchy and grouping.
Infer whether visible bands, headings, dividers, outlines, or surfaces distinguish coherent multi-module groups or individual items. Do not turn every module into a repeated framed card when the reference keeps body content open.
Treat section_style as the primary group-header and framing treatment. A band may label one panel or cross multiple visible panels; encode the dominant repeated content-group tier once rather than reconstructing the reference's semantic outline.
Inspect both macro and micro grammar, then encode the result only through the supported typed choices. If a visible property is not represented by the contract, do not hide it in prose.
Return one JSON object containing exactly these fields:
typography, palette, layout.
Every returned field is executable authoring input. Never encode a seed name, paper topic, fixed module id, target page, content-specific adaptation, observation list, or free-form directive.
layout.column_weights must be a JSON array of one to four positive relative widths, ordered left-to-right from measured scientific-body lane boundaries. Equal values mean visibly equal lanes; do not emit equal values as a default when the body lanes are visibly unequal.
layout lead_role is none, focal, or method; lead_placement is flow, full-width, or center-lane; section_style is open, rule, band, or outlined; module_style is open, outlined, or selective; spacing is compact, balanced, or open; masthead_scale is compact, balanced, or prominent; masthead_alignment is left or center; figure_emphasis is copy-led, balanced, or figure-led.
palette must contain exactly five six-digit hex colors: accent, background, ink, muted, and surface.
Map palette roles to visible use: background is the page field; surface is the module or panel fill behind normal ink text; accent is the dominant band, rule, or emphasis color; ink is primary text; muted is secondary text. Do not put a header-band color into surface when module interiors are visibly light.
Choose masthead_scale and figure_emphasis from relative page area and visual hierarchy in the pixels, not from a preferred conference style or a default value.
Return JSON only. Do not include Markdown, commentary, seed identity, or scientific content copied from the reference."""


def normalize_preferences(
    value: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Validate the small user-owned override surface for visual planning."""

    if value is None:
        return {}
    if not isinstance(value, Mapping) or not set(value).issubset(_PREFERENCE_FIELDS):
        raise VisualDesignError("visual preferences fields are invalid")
    normalized = {str(key): str(item).strip().lower() for key, item in value.items()}
    typography = normalized.get("typography")
    if typography is not None:
        normalized["typography"] = _typography_profile(typography)
    framing = normalized.get("framing")
    if framing is not None and framing not in {"unframed", "section-outline"}:
        raise VisualDesignError(
            "visual preference framing must be unframed or section-outline"
        )
    accent = normalized.get("accent_color")
    if accent is not None and _HEX_COLOR_RE.fullmatch(accent) is None:
        raise VisualDesignError("visual preference accent_color is invalid")
    return normalized


def _apply_preferences(
    plan: VisualDesignPlan,
    preferences: Mapping[str, str],
) -> VisualDesignPlan:
    if not preferences:
        return plan
    palette = dict(plan.palette)
    if preferences.get("accent_color"):
        palette["accent"] = preferences["accent_color"]
    framing = preferences.get("framing")
    layout = plan.layout
    if framing == "unframed":
        layout = replace(
            layout,
            section_style="open",
            module_style="open",
        )
    elif framing == "section-outline":
        layout = replace(
            layout,
            section_style="outlined",
            module_style="open",
        )
    return replace(
        plan,
        typography=preferences.get("typography", plan.typography),
        palette=_functional_palette(palette),
        layout=layout,
    )


def _parse_response(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise VisualDesignError("visual design response must be JSON text")
    text = raw.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE
    )
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisualDesignError("visual design response is not valid JSON") from exc
    if not isinstance(value, dict) or not (_RESPONSE_FIELDS & set(value)):
        raise VisualDesignError("visual design response fields are invalid")
    return {field: value[field] for field in _RESPONSE_FIELDS if field in value}


def _plan_from_response(
    value: Mapping[str, Any],
    *,
    reference_sha256: str,
    model: str,
    fallback: VisualDesignPlan,
) -> VisualDesignPlan:
    palette = value.get("palette")
    if palette is not None and not isinstance(palette, Mapping):
        raise VisualDesignError("visual design palette must be an object")
    raw_layout = value.get("layout")
    if raw_layout is not None and not isinstance(raw_layout, Mapping):
        raise VisualDesignError("visual design layout must be an object")
    merged_palette = dict(fallback.palette)
    if isinstance(palette, Mapping):
        merged_palette.update(
            {
                str(key): str(item)
                for key, item in palette.items()
                if key in _PALETTE_KEYS
                and _HEX_COLOR_RE.fullmatch(str(item)) is not None
            }
        )
    return VisualDesignPlan(
        reference_image_sha256=reference_sha256,
        planner="vlm",
        typography=_response_choice(
            value.get("typography"),
            choices=("hybrid", "sans", "serif"),
            fallback=fallback.typography,
        ),  # type: ignore[arg-type]
        palette=_functional_palette(merged_palette),
        layout=_layout_from_response(raw_layout, fallback=fallback.layout),
        planner_model=model,
    )


def _layout_from_response(
    value: Any,
    *,
    fallback: LayoutGrammar,
) -> LayoutGrammar:
    """Merge a partially returned layout object with neutral supported defaults."""

    if value is None:
        return fallback
    if not isinstance(value, Mapping):
        raise VisualDesignError("visual design layout must be an object")
    raw_weights = value.get("column_weights", fallback.column_weights)
    weights = fallback.column_weights
    if (
        isinstance(raw_weights, Sequence)
        and not isinstance(raw_weights, (str, bytes))
        and not any(isinstance(weight, bool) for weight in raw_weights)
    ):
        try:
            candidate_weights = tuple(float(weight) for weight in raw_weights)
        except (TypeError, ValueError):
            candidate_weights = ()
        if 1 <= len(candidate_weights) <= 4 and all(
            0.25 <= weight <= 4.0 for weight in candidate_weights
        ):
            weights = candidate_weights
    return LayoutGrammar(
        column_weights=weights,
        lead_role=_response_choice(
            value.get("lead_role"),
            choices=("none", "focal", "method"),
            fallback=fallback.lead_role,
        ),  # type: ignore[arg-type]
        lead_placement=_response_choice(
            value.get("lead_placement"),
            choices=("full-width", "center-lane", "flow"),
            fallback=fallback.lead_placement,
        ),  # type: ignore[arg-type]
        section_style=_response_choice(
            value.get("section_style"),
            choices=("open", "rule", "band", "outlined"),
            fallback=fallback.section_style,
        ),  # type: ignore[arg-type]
        module_style=_response_choice(
            value.get("module_style"),
            choices=("open", "outlined", "selective"),
            fallback=fallback.module_style,
        ),  # type: ignore[arg-type]
        spacing=_response_choice(
            value.get("spacing"),
            choices=("compact", "balanced", "open"),
            fallback=fallback.spacing,
        ),  # type: ignore[arg-type]
        masthead_scale=_response_choice(
            value.get("masthead_scale"),
            choices=("compact", "balanced", "prominent"),
            fallback=fallback.masthead_scale,
        ),  # type: ignore[arg-type]
        masthead_alignment=_response_choice(
            value.get("masthead_alignment"),
            choices=("left", "center"),
            fallback=fallback.masthead_alignment,
        ),  # type: ignore[arg-type]
        figure_emphasis=_response_choice(
            value.get("figure_emphasis"),
            choices=("copy-led", "balanced", "figure-led"),
            fallback=fallback.figure_emphasis,
        ),  # type: ignore[arg-type]
    )


def _response_choice(
    value: Any,
    *,
    choices: tuple[str, ...],
    fallback: str,
) -> str:
    """Read one bounded design choice without failing the rest of the pixel plan."""

    normalized = " ".join(str(value or "").strip().lower().replace("-", " ").split())
    for choice in choices:
        phrase = choice.replace("-", " ")
        if normalized == phrase or re.search(rf"\b{re.escape(phrase)}\b", normalized):
            return choice
    return fallback


def _typography_profile(value: Any) -> TypographyProfile:
    """Validate the complete font-family choice consumed by the renderer."""

    normalized = str(value).strip().lower()
    if normalized not in {"sans", "serif", "hybrid"}:
        raise VisualDesignError(
            "visual design typography must be sans, serif, or hybrid"
        )
    return normalized  # type: ignore[return-value]


def _functional_palette(palette: Mapping[str, str]) -> dict[str, str]:
    """Preserve reference hues while keeping renderer-owned text roles readable."""

    if set(palette) != _PALETTE_KEYS or any(
        _HEX_COLOR_RE.fullmatch(str(value)) is None for value in palette.values()
    ):
        raise VisualDesignError("visual design palette is invalid")
    result = {key: str(palette[key]).lower() for key in _PALETTE_ORDER}
    background = result["background"]
    ink = result["ink"]
    if _contrast_ratio(ink, background) < _MIN_TEXT_CONTRAST:
        ink = max(("#000000", "#ffffff"), key=lambda value: _contrast_ratio(value, background))
        result["ink"] = ink
    if _contrast_ratio(result["surface"], ink) < _MIN_TEXT_CONTRAST:
        result["surface"] = _blend_until_contrast(
            result["surface"],
            background,
            foreground=ink,
        )
    if _contrast_ratio(result["muted"], background) < _MIN_TEXT_CONTRAST:
        result["muted"] = _blend_until_contrast(
            result["muted"],
            ink,
            foreground=background,
        )
    if _contrast_ratio(result["accent"], "#ffffff") < _MIN_TEXT_CONTRAST:
        result["accent"] = _blend_until_contrast(
            result["accent"],
            "#000000",
            foreground="#ffffff",
        )
    return result


def _blend_until_contrast(
    color: str,
    target: str,
    *,
    foreground: str,
) -> str:
    """Move one role color minimally toward a known-readable endpoint."""

    for step in range(1, 21):
        candidate = _mix_hex(color, target, step / 20.0)
        if _contrast_ratio(candidate, foreground) >= _MIN_TEXT_CONTRAST:
            return candidate
    return target


def _mix_hex(first: str, second: str, fraction: float) -> str:
    first_rgb = _hex_rgb(first)
    second_rgb = _hex_rgb(second)
    values = [
        round(start + (end - start) * fraction)
        for start, end in zip(first_rgb, second_rgb, strict=True)
    ]
    return "#" + "".join(f"{value:02x}" for value in values)


def _contrast_ratio(first: str, second: str) -> float:
    light, dark = sorted(
        (_relative_luminance(first), _relative_luminance(second)),
        reverse=True,
    )
    return (light + 0.05) / (dark + 0.05)


def _relative_luminance(value: str) -> float:
    channels = [channel / 255.0 for channel in _hex_rgb(value)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _hex_rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]


def _reference_fallback_plan(
    reference: reference_seeds.ReferenceBundle,
    *,
    content_budget: Mapping[str, Any] | None = None,
    page_plan: Mapping[str, Any] | None = None,
) -> VisualDesignPlan:
    layout = _adaptive_layout(content_budget or {}, page_plan or {})
    palette = {
        "background": "#ffffff",
        "surface": "#f4f6f8",
        "ink": "#111827",
        "muted": "#5f6877",
        "accent": "#315f9d",
    }
    if reference.source_kind == "seed":
        layout = _seed_metadata_layout(reference, fallback=layout)
        palette["accent"] = reference_seeds.seed_by_id(
            reference.seed_id
        ).accent_color
    return VisualDesignPlan(
        reference_image_sha256=reference.image_sha256,
        planner="adaptive",
        typography="sans",
        palette=_functional_palette(palette),
        layout=layout,
    )


def _seed_metadata_layout(
    reference: reference_seeds.ReferenceBundle,
    *,
    fallback: LayoutGrammar,
) -> LayoutGrammar:
    """Apply registered visual metadata without branching on seed identity."""

    seed = reference_seeds.seed_by_id(reference.seed_id)
    lead_role: LeadRole = (
        "method"
        if seed.role_emphasis == "method"
        else "focal"
        if seed.role_emphasis in {"claim", "evidence"}
        else "none"
    )
    figure_emphasis: FigureEmphasis = (
        "figure-led"
        if seed.figure_preference >= 0.75
        else "copy-led"
        if seed.figure_preference <= 0.35
        else "balanced"
    )
    spacing_by_density: dict[str, SpacingProfile] = {
        "open": "open",
        "balanced": "balanced",
        "dense": "compact",
    }
    spacing = spacing_by_density[reference_seeds.normalize_density(seed.density)]
    return replace(
        fallback,
        column_weights=seed.column_weights,
        lead_role=lead_role,
        lead_placement=seed.lead_placement,
        section_style=seed.section_style,
        module_style=seed.module_style,
        spacing=spacing,
        masthead_scale=seed.masthead_scale,
        masthead_alignment=seed.masthead_alignment,
        figure_emphasis=figure_emphasis,
    )


def _adaptive_layout(
    content_budget: Mapping[str, Any],
    page_plan: Mapping[str, Any],
) -> LayoutGrammar:
    """Choose a neutral fallback from page and content geometry, never seed identity."""

    raw_modules = content_budget.get("content_modules")
    module_count = (
        len([item for item in raw_modules if isinstance(item, Mapping)])
        if isinstance(raw_modules, list)
        else 0
    )
    capacity = page_plan.get("layout_capacity")
    raw_maximum = (
        capacity.get("maximum_readable_column_count")
        if isinstance(capacity, Mapping)
        else None
    )
    maximum = (
        int(raw_maximum)
        if isinstance(raw_maximum, int) and not isinstance(raw_maximum, bool)
        else 4
    )
    orientation = str(page_plan.get("orientation") or _reference_orientation(page_plan))
    preferred = 2 if orientation == "portrait" else 3
    lane_count = max(1, min(maximum, preferred, module_count or preferred))
    return LayoutGrammar(
        column_weights=(1.0,) * lane_count,
        lead_role="focal",
        lead_placement="flow",
        section_style="rule",
        module_style="selective",
    )


def _reference_orientation(page_plan: Mapping[str, Any]) -> str:
    """Infer orientation only when an older page plan omitted its label."""

    width = page_plan.get("width_mm")
    height = page_plan.get("height_mm")
    if all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in (width, height)
    ):
        return "portrait" if float(height) > float(width) else "landscape"
    return "landscape"


__all__ = [
    "SCHEMA",
    "LayoutGrammar",
    "MastheadAlignment",
    "SpacingProfile",
    "TypographyProfile",
    "VisualDesignError",
    "VisualDesignPlan",
    "normalize_preferences",
    "plan_visual_design",
]
