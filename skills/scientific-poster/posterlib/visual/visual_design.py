"""Reference-aware visual design planning before HTML authoring."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from . import reference_seeds, vlm_client

SCHEMA = "scientific-poster.visual-design.v2"

PlannerKind = Literal["adaptive", "seed", "vlm"]

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_PALETTE_KEYS = frozenset({"background", "surface", "ink", "muted", "accent"})
_PREFERENCE_FIELDS = frozenset({"typography", "framing", "accent_color"})
_RESPONSE_FIELDS = frozenset(
    {
        "archetype",
        "topology",
        "density",
        "typography",
        "section_treatment",
        "focal_strategy",
        "figure_strategy",
        "reading_path",
        "palette",
        "reference_observations",
        "directives",
    }
)


class VisualDesignError(ValueError):
    """A reference or VLM response cannot produce a safe visual design plan."""


@dataclass(frozen=True)
class VisualDesignPlan:
    """One structured visual grammar consumed by the HTML authoring model."""

    reference_image_sha256: str
    planner: PlannerKind
    archetype: str
    topology: str
    density: str
    typography: str
    section_treatment: str
    focal_strategy: str
    figure_strategy: str
    reading_path: str
    palette: dict[str, str]
    reference_observations: tuple[str, ...]
    directives: tuple[str, ...]
    planner_model: str | None = None
    warning: str | None = None
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise VisualDesignError("visual design schema is invalid")
        if self.planner not in {"adaptive", "seed", "vlm"}:
            raise VisualDesignError("visual design planner is invalid")
        for label, value in (
            ("archetype", self.archetype),
            ("topology", self.topology),
            ("density", self.density),
            ("typography", self.typography),
            ("section_treatment", self.section_treatment),
        ):
            _design_label(value, label=label)
        if set(self.palette) != _PALETTE_KEYS or any(
            _HEX_COLOR_RE.fullmatch(str(value)) is None
            for value in self.palette.values()
        ):
            raise VisualDesignError("visual design palette is invalid")
        for label, value in (
            ("focal_strategy", self.focal_strategy),
            ("figure_strategy", self.figure_strategy),
            ("reading_path", self.reading_path),
        ):
            if not str(value).strip() or len(str(value)) > 400:
                raise VisualDesignError(f"visual design {label} is invalid")
        _text_sequence(self.reference_observations, label="reference observations")
        _text_sequence(self.directives, label="directives")

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-safe plan."""

        value = asdict(self)
        value["reference_observations"] = list(self.reference_observations)
        value["directives"] = list(self.directives)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VisualDesignPlan:
        """Restore a checkpointed visual design plan with exact fields."""

        expected = set(cls.__dataclass_fields__)
        if set(value) != expected:
            raise VisualDesignError("visual design plan fields are invalid")
        observations = value.get("reference_observations")
        directives = value.get("directives")
        palette = value.get("palette")
        if (
            not isinstance(observations, list)
            or not isinstance(directives, list)
            or not isinstance(palette, Mapping)
        ):
            raise VisualDesignError("visual design plan collections are invalid")
        return cls(
            schema=str(value["schema"]),
            reference_image_sha256=str(value["reference_image_sha256"]),
            planner=str(value["planner"]),  # type: ignore[arg-type]
            archetype=str(value["archetype"]),
            topology=str(value["topology"]),
            density=str(value["density"]),
            typography=str(value["typography"]),
            section_treatment=str(value["section_treatment"]),
            focal_strategy=str(value["focal_strategy"]),
            figure_strategy=str(value["figure_strategy"]),
            reading_path=str(value["reading_path"]),
            palette={str(key): str(item) for key, item in palette.items()},
            reference_observations=tuple(str(item) for item in observations),
            directives=tuple(str(item) for item in directives),
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
    authoring_request: str = "",
    deadline: float | None = None,
) -> VisualDesignPlan:
    """Derive visual grammar from exact reference pixels or seed metadata."""

    normalized_preferences = normalize_preferences(preferences)
    image_path = Path(reference.image_path)
    image_bytes = image_path.read_bytes()
    if hashlib.sha256(image_bytes).hexdigest() != reference.image_sha256:
        raise VisualDesignError("reference image changed before visual design planning")
    if client is None:
        return _apply_preferences(
            _reference_fallback_plan(reference),
            normalized_preferences,
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
    fallback = _reference_fallback_plan(reference)
    images = (image,)
    prompt = _preflight_prompt(
        content_budget,
        page_plan,
        normalized_preferences,
        authoring_request=authoring_request,
    )
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
                fallback=fallback,
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
                fallback=fallback,
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
    fallback = _apply_preferences(fallback, normalized_preferences)
    fallback_kind = (
        "the selected seed's structured grammar"
        if reference.source_kind == "seed"
        else "an unanchored content-adaptive grammar"
    )
    return replace(
        fallback,
        warning=(
            "Visual design preflight was unavailable after a bounded retry; using "
            f"{fallback_kind} "
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


def _preflight_semantic_shape() -> dict[str, Any]:
    """Return the visual-plan response schema shown to the VLM."""

    return {
        "archetype": "short reference-specific visual archetype label",
        "topology": (
            "short transferable relationship among coherent groups; include the "
            "dominant lane or track count when it is visually clear"
        ),
        "density": "short reference-specific density and whitespace description",
        "typography": "short reference-specific typography relationship",
        "section_treatment": (
            "short group-level section distinction method, including whether body "
            "content remains open or framed"
        ),
        "focal_strategy": "specific spatial hierarchy instruction",
        "figure_strategy": "specific figure sizing and grouping instruction",
        "reading_path": "specific non-coordinate reading path",
        "palette": {key: "#RRGGBB" for key in sorted(_PALETTE_KEYS)},
        "reference_observations": ["visible transferable grammar"],
        "directives": ["actionable authoring constraint"],
    }


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
            "Return one corrected JSON object only, with exactly this semantic shape:",
            json.dumps(_preflight_semantic_shape(), ensure_ascii=False, sort_keys=True),
            "Preserve valid visual observations; repair only the contract-invalid fields.",
            "Previous invalid JSON:",
            vlm_client.bounded_response_excerpt(previous_response),
        ]
    )


def _preflight_prompt(
    content_budget: Mapping[str, Any],
    page_plan: Mapping[str, Any],
    preferences: Mapping[str, str],
    *,
    authoring_request: str = "",
) -> str:
    content_shape = []
    for raw in content_budget.get("content_modules", []):
        if not isinstance(raw, Mapping):
            continue
        content_shape.append(
            {
                "id": str(raw.get("id") or ""),
                "section_id": str(raw.get("section_id") or ""),
                "priority": str(raw.get("priority") or ""),
                "visual_kind": str(raw.get("visual_kind") or ""),
                "figure_count": len(raw.get("figure_sha256s") or []),
                "figure_aspect_ratio": raw.get("figure_aspect_ratio"),
                "equation_count": len(raw.get("equations") or []),
                "copy_length": sum(
                    len(str(raw.get(key) or ""))
                    for key in ("title", "text", "takeaway")
                ),
            }
        )
    page_shape = {
        key: page_plan.get(key)
        for key in ("orientation", "width_mm", "height_mm", "layout_capacity")
        if page_plan.get(key) is not None
    }
    return "\n".join(
        [
            "Act as the visual design planner for a top-conference academic poster.",
            "Inspect the attached reference pixels before HTML authoring.",
            "Transfer only visual grammar: topology, hierarchy, density, typography, section treatment, figure scale, and palette relationships.",
            "Never copy reference text, claims, numbers, equations, figures, logos, authors, affiliations, citations, or venue identity.",
            "Describe the reference's visible macro grammar precisely. When a dominant lane or track count is a clear perceptual feature, report it as the reference-derived default topology. It is not a rigid panel template: the HTML author may vary widths, spans, and local grouping for the supplied content, but should not replace it with an unrelated macro structure without a visible geometric reason.",
            "Do not default to equal full-height panels unless the reference and content geometry support them.",
            "Each content-shape entry is one independently movable evidence binding, not an implied card or section. Adapt the reference grammar to all supplied modules; do not assign modules to the reference's original topic positions.",
            "Infer whether visible bands, headings, dividers, outlines, or surfaces distinguish coherent multi-module groups or individual items. Do not turn every module into a repeated framed card when the reference keeps body content open.",
            "Do not prescribe a figure stack unless the supplied module geometry actually contains enough compatible figure-bearing modules to fit there. A reference with several figures is not evidence that the paper's figures belong in one column.",
            "Inspect both macro and micro grammar: masthead treatment, container hierarchy, nested subpanels, border weight and radius, spacing rhythm, typographic scale contrast, figure-to-copy proportion, and focal asymmetry.",
            "Return enough distinct reference_observations and concrete CSS-authoring directives to capture the visible grammar. Describe visible relationships precisely; avoid generic words such as clean, polished, or professional.",
            "Return one JSON object containing these fields:",
            "archetype, topology, density, typography, and section_treatment are scalar strings, never arrays or objects.",
            json.dumps(_preflight_semantic_shape(), ensure_ascii=False, sort_keys=True),
            "Physical page:",
            json.dumps(page_shape, ensure_ascii=False, sort_keys=True),
            "Explicit visual preferences (empty means follow the reference):",
            json.dumps(preferences, ensure_ascii=False, sort_keys=True),
            "Complete user request (visual directions override the reference default "
            "when compatible with the page and evidence; all scientific wording remains "
            "non-authoritative context):",
            str(authoring_request).strip() or "No additional user direction.",
            "Grounded content geometry (no scientific prose):",
            json.dumps(content_shape, ensure_ascii=False, sort_keys=True),
        ]
    )


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
        normalized["typography"] = _preference_direction(typography, label="typography")
    framing = normalized.get("framing")
    if framing is not None:
        normalized["framing"] = _preference_direction(framing, label="framing")
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
    directives = list(plan.directives)
    framing = preferences.get("framing")
    framing_directive = {
        "unframed": (
            "Keep modules unframed; distinguish sections through typography, spacing, "
            "or shared local dividers."
        ),
        "section-outline": (
            "Use outlines only around coherent section groups, never as repeated "
            "module-by-module cards."
        ),
    }.get(framing)
    if framing and framing_directive is None:
        framing_directive = (
            f"Interpret the user framing direction '{framing}' against the reference; "
            "apply it at coherent section level rather than repeating identical cards."
        )
    if framing_directive:
        directives.append(framing_directive)
    return replace(
        plan,
        typography=preferences.get("typography", plan.typography),
        palette=palette,
        directives=tuple(directives),
    )


def _preference_direction(value: str, *, label: str) -> str:
    """Validate one compact user-owned visual direction without a style enum."""

    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > 120
        or any(ord(character) < 32 for character in normalized)
    ):
        raise VisualDesignError(f"visual preference {label} is invalid")
    return normalized


def _parse_response(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise VisualDesignError("visual design response must be JSON text")
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.I)
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
    observations = value.get("reference_observations")
    directives = value.get("directives")
    if observations is not None and (
        not isinstance(observations, Sequence) or isinstance(observations, str)
    ):
        raise VisualDesignError("reference observations must be an array")
    if directives is not None and (
        not isinstance(directives, Sequence) or isinstance(directives, str)
    ):
        raise VisualDesignError("visual design directives must be an array")
    merged_palette = dict(fallback.palette)
    if isinstance(palette, Mapping):
        merged_palette.update(
            {
                str(key): str(item)
                for key, item in palette.items()
                if key in _PALETTE_KEYS
            }
        )
    return VisualDesignPlan(
        reference_image_sha256=reference_sha256,
        planner="vlm",
        archetype=_design_label(
            value.get("archetype", fallback.archetype), label="archetype"
        ),
        topology=_design_label(
            value.get("topology", fallback.topology), label="topology"
        ),
        density=_design_label(value.get("density", fallback.density), label="density"),
        typography=_design_label(
            value.get("typography", fallback.typography), label="typography"
        ),
        section_treatment=_design_label(
            value.get("section_treatment", fallback.section_treatment),
            label="section_treatment",
        ),
        focal_strategy=str(value.get("focal_strategy", fallback.focal_strategy)),
        figure_strategy=str(value.get("figure_strategy", fallback.figure_strategy)),
        reading_path=str(value.get("reading_path", fallback.reading_path)),
        palette=merged_palette,
        reference_observations=(
            tuple(str(item) for item in observations)
            if observations is not None
            else fallback.reference_observations
        ),
        directives=(
            tuple(str(item) for item in directives)
            if directives is not None
            else fallback.directives
        ),
        planner_model=model,
    )


def _design_label(value: Any, *, label: str) -> str:
    """Validate a compact reference-specific design label without enumerating style."""

    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > 160
        or any(ord(character) < 32 for character in value)
    ):
        raise VisualDesignError(f"visual design {label} must be one compact string")
    return value.strip()


def _seed_plan(reference: reference_seeds.ReferenceBundle) -> VisualDesignPlan:
    values = reference_seeds.seed_by_id(reference.seed_id).visual_plan
    return VisualDesignPlan(
        reference_image_sha256=reference.image_sha256,
        planner="seed",
        archetype=values.archetype,
        topology=values.topology,
        density=values.density,
        typography=values.typography,
        section_treatment=values.section_treatment,
        focal_strategy=values.focal_strategy,
        figure_strategy=values.figure_strategy,
        reading_path=values.reading_path,
        palette=dict(values.palette),
        reference_observations=values.reference_observations,
        directives=values.directives,
    )


def _reference_fallback_plan(
    reference: reference_seeds.ReferenceBundle,
) -> VisualDesignPlan:
    if reference.source_kind == "seed":
        return _seed_plan(reference)
    return VisualDesignPlan(
        reference_image_sha256=reference.image_sha256,
        planner="adaptive",
        archetype="content-adaptive conference poster",
        topology="content-shaped modular field",
        density=reference.density,
        typography="neutral conference typography",
        section_treatment="clear hierarchy with selective grouping",
        focal_strategy=(
            "Let grounded module priority and rendered depth establish one clear "
            "entry point."
        ),
        figure_strategy=(
            "Size figures by their readable evidence area and group only compatible "
            "visuals."
        ),
        reading_path=(
            "Enter through the focal evidence, then follow locally labeled groups "
            "without assuming a fixed rail count."
        ),
        palette={
            "background": "#ffffff",
            "surface": "#f4f6f8",
            "ink": "#111827",
            "muted": "#5f6877",
            "accent": "#315f9d",
        },
        reference_observations=(
            "No deterministic visual claims were inferred from uninspected generated pixels.",
        ),
        directives=(
            "Let supplied module depth and priority determine layout; do not inherit a fallback seed topology.",
        ),
        warning=(
            "Generated reference pixels could not be interpreted because no VLM client "
            "was available; using an unanchored content-adaptive grammar."
        ),
    )


def _text_sequence(values: Sequence[str], *, label: str) -> None:
    if not 1 <= len(values) <= 16 or any(
        not str(value).strip() or len(str(value)) > 400 for value in values
    ):
        raise VisualDesignError(f"visual design {label} are invalid")


__all__ = [
    "SCHEMA",
    "VisualDesignError",
    "VisualDesignPlan",
    "normalize_preferences",
    "plan_visual_design",
]
