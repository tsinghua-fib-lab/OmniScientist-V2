"""Model-owned visual design plans derived from exact reference pixels."""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from PIL import Image

from . import reference_catalog, vlm_client

SCHEMA = "scientific-poster.visual-design.v7"
PlannerKind = Literal["adaptive", "vlm"]

# These are observation channels, not layout tokens. Values remain free-form so
# the vision model can describe any reference without selecting from a template.
DESIGN_FIELDS = (
    "spatial_grammar",
    "hierarchy_and_reading_path",
    "typography",
    "color_and_surface",
    "boundary_and_spacing",
    "figure_treatment",
    "repeated_patterns_and_exceptions",
    "user_preferences",
)
_PREFERENCE_FIELDS = frozenset({"typography", "framing", "accent_color"})
_HASH_LENGTH = 64


class ReferenceAnalysisError(ValueError):
    """A reference or VLM response cannot produce a safe visual design plan."""


@dataclass(frozen=True)
class ReferenceAnalysis:
    """Hash-bound, model-authored design semantics shared by authoring and review."""

    reference_image_sha256: str
    planner: PlannerKind
    design: dict[str, str] = field(default_factory=dict)
    planner_model: str | None = None
    warning: str | None = None
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ReferenceAnalysisError("visual design schema is invalid")
        digest = str(self.reference_image_sha256)
        if len(digest) != _HASH_LENGTH or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ReferenceAnalysisError("visual design reference hash is invalid")
        if self.planner not in {"adaptive", "vlm"}:
            raise ReferenceAnalysisError("visual design planner is invalid")
        object.__setattr__(self, "design", _normalize_design(self.design))
        if self.planner_model is not None and not str(self.planner_model).strip():
            raise ReferenceAnalysisError("visual design planner model is invalid")
        if self.warning is not None and not str(self.warning).strip():
            raise ReferenceAnalysisError("visual design warning is invalid")

    def to_dict(self) -> dict[str, Any]:
        """Return the compact, provider-neutral checkpoint representation."""

        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReferenceAnalysis:
        """Restore one current visual-design plan."""

        if not isinstance(value, Mapping):
            raise ReferenceAnalysisError("visual design plan must be an object")
        schema = str(value.get("schema") or "")
        expected = set(cls.__dataclass_fields__)
        if schema != SCHEMA or set(value) != expected:
            raise ReferenceAnalysisError("visual design plan fields are invalid")
        design = value.get("design")
        if not isinstance(design, Mapping):
            raise ReferenceAnalysisError("visual design observations must be an object")
        return cls(
            reference_image_sha256=str(value.get("reference_image_sha256") or ""),
            planner=str(value.get("planner") or ""),  # type: ignore[arg-type]
            design=dict(design),
            planner_model=_optional_text(value.get("planner_model")),
            warning=_optional_text(value.get("warning")),
        )


async def analyze_reference(
    reference: reference_catalog.ReferenceBundle,
    *,
    client: Any | None,
    preferences: Mapping[str, Any] | None = None,
    deadline: float | None = None,
) -> ReferenceAnalysis:
    """Observe one exact reference; the author owns adaptation to the target."""

    normalized_preferences = normalize_preferences(preferences)
    image_path = Path(reference.image_path)
    image_bytes = image_path.read_bytes()
    if hashlib.sha256(image_bytes).hexdigest() != reference.image_sha256:
        raise ReferenceAnalysisError("reference image changed before visual design planning")
    if client is None:
        return _apply_preferences(
            ReferenceAnalysis(
                reference_image_sha256=reference.image_sha256,
                planner="adaptive",
                warning="Reference pixels require an image-capable model.",
            ),
            normalized_preferences,
        )

    mime_type = mimetypes.guess_type(image_path.name)[0] or ""
    if not mime_type.startswith("image/"):
        raise ReferenceAnalysisError("reference image has no recognized image type")
    with Image.open(image_path) as raster:
        width_px, height_px = raster.size
    image = vlm_client.VlmImage(
        label=(
            "VISUAL REFERENCE — observe visual language only; never copy scientific "
            "content, identity, logos, or figures."
        ),
        image_bytes=image_bytes,
        mime_type=mime_type,
    )
    try:
        raw = await _analyze_reference_image(
            client,
            _reference_analysis_prompt(
                width_px=width_px,
                height_px=height_px,
            ),
            images=(image,),
            deadline=deadline,
        )
        plan = _plan_from_response(
            _parse_response(raw),
            reference_sha256=reference.image_sha256,
            model=str(getattr(client, "model", "") or "unknown"),
        )
        return _apply_preferences(plan, normalized_preferences)
    except (
        TimeoutError,
        ReferenceAnalysisError,
        vlm_client.VlmError,
        OSError,
        ValueError,
    ) as exc:
        return _apply_preferences(
            ReferenceAnalysis(
                reference_image_sha256=reference.image_sha256,
                planner="adaptive",
                warning=(
                    "Visual reference planning was unavailable "
                    f"({type(exc).__name__}: {exc})."
                ),
            ),
            normalized_preferences,
        )


async def _analyze_reference_image(
    client: Any,
    prompt: str,
    *,
    images: tuple[vlm_client.VlmImage, ...],
    deadline: float | None,
) -> Any:
    """Generate the reference observation within the shared planning deadline."""

    return await vlm_client.generate_json_text_bounded(
        client,
        prompt,
        images=images,
        deadline=deadline,
        timeout_message="visual design preflight budget was exhausted",
    )


def _reference_analysis_prompt(
    *,
    width_px: int | None = None,
    height_px: int | None = None,
) -> str:
    """Ask for a structured visual reading while leaving all CSS to the author."""

    geometry = ""
    if width_px and height_px:
        orientation = (
            "landscape"
            if width_px > height_px
            else "portrait" if height_px > width_px else "square"
        )
        geometry = (
            f"Raster: {width_px}x{height_px} pixels; {orientation} orientation.\n"
        )
    fields = ", ".join(
        field for field in DESIGN_FIELDS if field != "user_preferences"
    )
    return f"""Inspect the attached VISUAL REFERENCE as it is actually drawn. There is no target paper to lay out at this stage.
{geometry}Return one JSON object {{"design":{{...}}}} using these free-form string fields:
{fields}.

Record observations of this source image, not recommendations for another poster. In spatial_grammar, describe the actual major regions, their count and arrangement, relative widths and heights, alignment, local subdivisions, dominant masses and open areas. Approximate image-relative fractions or percentages are useful when the visual evidence supports them; label estimates as approximate. These facts describe the reference, not fixed slots, a required column count, or coordinates for a future target poster. Identify regions by visible form and location rather than assigning target scientific roles or content weights.

Describe the actual hierarchy and reading path from type scale, graphic scale, contrast and grouping. In typography, distinguish display, heading, body and utility treatments through visible width, x-height, stroke contrast, weight, case, tracking, leading and relative size. When an exact face or measurement is not identifiable, state the visible qualities and uncertainty rather than inventing a font or value.

For surfaces, borders and rules, describe what they visibly cover and where they end. Distinguish a heading-only strip from a surface or outline enclosing the entire content region. Keep white background, visible enclosure and spacing separate in the description; a content group is not automatically a bordered panel. Describe color/value relationships and how figures, tables, captions, equations and callouts are scaled and framed. Keep recurring patterns distinct from local exceptions.

The author receives the target paper, content and page separately and owns all adaptation and HTML/CSS. Return no target-layout instructions, CSS, HTML, preset names or layout enums. Do not transcribe reference claims, values, equations, authors, affiliations, citations, logos or venue identity as scientific content."""


def normalize_preferences(value: Mapping[str, Any] | None) -> dict[str, str]:
    """Normalize the small user-owned visual override surface without style enums."""

    if value is None:
        return {}
    if not isinstance(value, Mapping) or not set(value).issubset(_PREFERENCE_FIELDS):
        raise ReferenceAnalysisError("visual preferences fields are invalid")
    return {
        str(key): text
        for key, item in value.items()
        if (text := " ".join(str(item or "").split()))
    }


def _parse_response(raw: Any) -> dict[str, str]:
    if not isinstance(raw, str):
        raise ReferenceAnalysisError("visual design response must be JSON text")
    try:
        value = vlm_client.parse_json_object(raw)
    except vlm_client.VlmError as exc:
        raise ReferenceAnalysisError(str(exc)) from exc
    if set(value) != {"design"} or not isinstance(value["design"], Mapping):
        raise ReferenceAnalysisError(
            "visual design response must contain exactly one design object"
        )
    design = _normalize_design(value["design"])
    if not design:
        raise ReferenceAnalysisError("visual design response contains no usable observations")
    return design


def _plan_from_response(
    value: Mapping[str, Any],
    *,
    reference_sha256: str,
    model: str,
) -> ReferenceAnalysis:
    design = _normalize_design(value)
    if not design:
        raise ReferenceAnalysisError("visual design response is empty")
    return ReferenceAnalysis(
        reference_image_sha256=reference_sha256,
        planner="vlm",
        design=design,
        planner_model=model,
    )


def _apply_preferences(
    plan: ReferenceAnalysis,
    preferences: Mapping[str, str],
) -> ReferenceAnalysis:
    """Attach explicit user direction after pixel observation, without reinterpretation."""

    if not preferences:
        return plan
    design = dict(plan.design)
    design["user_preferences"] = "; ".join(
        f"{key.replace('_', ' ')}: {value}" for key, value in preferences.items()
    )
    return replace(plan, design=design)


def _normalize_design(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ReferenceAnalysisError("visual design observations must be an object")
    return {
        field: text
        for field in DESIGN_FIELDS
        if (text := " ".join(str(value.get(field) or "").split()))
    }


def _optional_text(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


__all__ = [
    "DESIGN_FIELDS",
    "SCHEMA",
    "ReferenceAnalysis",
    "ReferenceAnalysisError",
    "analyze_reference",
    "normalize_preferences",
]
