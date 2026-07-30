"""Built-in visual seed selection and immutable reference bundles."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import poster_core

from posterlib.paths import SKILL_ROOT

Orientation = Literal["landscape", "portrait"]
ReferenceSourceKind = Literal["generated", "seed"]

NON_AUTHORITATIVE_REFERENCE_POLICY = (
    "The reference image is non-authoritative for scientific content. Never copy its "
    "claims, numbers, authors, affiliations, equations, figures, citations, logos, or "
    "venue identity."
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SEED_ROOT = SKILL_ROOT / "seeds"
_DENSITIES = frozenset({"open", "balanced", "dense"})
_LANDSCAPE_SEED_ORDER = (
    "central-method-stage",
    "claim-led-asymmetric",
    "open-evidence-gallery",
    "dense-classic-three-column",
)


class ReferenceSeedError(ValueError):
    """A seed selection or reference bundle is invalid."""


@dataclass(frozen=True)
class SeedVisualPlan:
    """Structured fallback grammar paired with one immutable seed image."""

    archetype: str
    topology: str
    density: str
    typography: str
    section_treatment: str
    focal_strategy: str
    figure_strategy: str
    reading_path: str
    palette: Mapping[str, str]
    reference_observations: tuple[str, ...]
    directives: tuple[str, ...]


@dataclass(frozen=True)
class SeedSpec:
    """Transferable design properties for one conference-poster reference."""

    seed_id: str
    image_path: Path
    orientation: Orientation
    density: str
    design_brief: tuple[str, ...]
    visual_plan: SeedVisualPlan


@dataclass(frozen=True)
class ReferenceBundle:
    """A hash-bound visual reference that cannot act as scientific evidence."""

    image_path: str
    image_sha256: str
    source_kind: ReferenceSourceKind
    seed_id: str
    orientation: Orientation
    density: str
    design_brief: tuple[str, ...]
    non_authoritative_policy: str = NON_AUTHORITATIVE_REFERENCE_POLICY
    warning: str | None = None

    def __post_init__(self) -> None:
        raw_image = Path(self.image_path).expanduser()
        if raw_image.is_symlink():
            raise ReferenceSeedError("reference image may not be a symlink")
        try:
            image = raw_image.resolve(strict=True)
        except OSError as exc:
            raise ReferenceSeedError(
                f"reference image is not readable: {self.image_path}"
            ) from exc
        if not image.is_file():
            raise ReferenceSeedError(f"reference image must be a file: {image}")
        digest = str(self.image_sha256)
        if _HASH_RE.fullmatch(digest) is None or _sha256_file(image) != digest:
            raise ReferenceSeedError("reference image hash does not match its bytes")
        if self.source_kind not in {"generated", "seed"}:
            raise ReferenceSeedError("reference source_kind must be generated or seed")
        registered = SEED_REGISTRY.get(self.seed_id)
        if registered is None:
            raise ReferenceSeedError(f"unknown reference seed: {self.seed_id}")
        if self.orientation not in {"landscape", "portrait"}:
            raise ReferenceSeedError(
                "reference orientation must be landscape or portrait"
            )
        density = normalize_density(self.density)
        brief = tuple(str(item).strip() for item in self.design_brief)
        if not brief or any(not item for item in brief):
            raise ReferenceSeedError("reference design_brief must contain guidance")
        if (
            self.orientation != registered.orientation
            or brief != registered.design_brief
        ):
            raise ReferenceSeedError(
                "reference orientation or design metadata does not match its seed"
            )
        if self.source_kind == "seed":
            try:
                registered_path = registered.image_path.resolve(strict=True)
            except OSError as exc:
                raise ReferenceSeedError(
                    f"registered seed image is missing: {registered.image_path}"
                ) from exc
            if image != registered_path:
                raise ReferenceSeedError(
                    "seed reference image does not use its registered path"
                )
        if self.non_authoritative_policy != NON_AUTHORITATIVE_REFERENCE_POLICY:
            raise ReferenceSeedError(
                "reference non-authoritative policy cannot be changed"
            )
        warning = None if self.warning is None else str(self.warning).strip()
        if self.warning is not None and not warning:
            raise ReferenceSeedError("reference warning must be non-empty when present")
        object.__setattr__(self, "image_path", str(image))
        object.__setattr__(self, "density", density)
        object.__setattr__(self, "design_brief", brief)
        object.__setattr__(self, "warning", warning)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-safe object kept separate from paper manifests."""

        return {
            "image_path": self.image_path,
            "image_sha256": self.image_sha256,
            "source_kind": self.source_kind,
            "seed_id": self.seed_id,
            "orientation": self.orientation,
            "density": self.density,
            "design_brief": list(self.design_brief),
            "non_authoritative_policy": self.non_authoritative_policy,
            "warning": self.warning,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReferenceBundle:
        """Validate and restore one serialized reference bundle."""

        required = {
            "image_path",
            "image_sha256",
            "source_kind",
            "seed_id",
            "orientation",
            "density",
            "design_brief",
            "non_authoritative_policy",
            "warning",
        }
        if set(value) != required:
            raise ReferenceSeedError("reference bundle fields are invalid")
        brief = value["design_brief"]
        if not isinstance(brief, list) or not all(
            isinstance(item, str) for item in brief
        ):
            raise ReferenceSeedError(
                "reference design_brief must be an array of strings"
            )
        warning = value["warning"]
        if warning is not None and not isinstance(warning, str):
            raise ReferenceSeedError("reference warning must be a string or null")
        return cls(
            image_path=str(value["image_path"]),
            image_sha256=str(value["image_sha256"]),
            source_kind=str(value["source_kind"]),  # type: ignore[arg-type]
            seed_id=str(value["seed_id"]),
            orientation=str(value["orientation"]),  # type: ignore[arg-type]
            density=str(value["density"]),
            design_brief=tuple(brief),
            non_authoritative_policy=str(value["non_authoritative_policy"]),
            warning=warning,
        )


def _seed(
    seed_id: str,
    *,
    orientation: Orientation,
    density: str,
    design_brief: tuple[str, ...],
    visual_plan: SeedVisualPlan,
) -> SeedSpec:
    filename = f"{seed_id}.png"
    return SeedSpec(
        seed_id=seed_id,
        image_path=_SEED_ROOT / filename,
        orientation=orientation,
        density=density,
        design_brief=design_brief,
        visual_plan=visual_plan,
    )


def _visual_plan(
    *,
    archetype: str,
    topology: str,
    density: str,
    typography: str,
    section_treatment: str,
    focal_strategy: str,
    figure_strategy: str,
    reading_path: str,
    palette: tuple[str, str, str, str, str],
    reference_observations: tuple[str, ...],
    directives: tuple[str, ...],
) -> SeedVisualPlan:
    """Return immutable fallback grammar stored beside its reference seed."""

    background, surface, ink, muted, accent = palette
    return SeedVisualPlan(
        archetype=archetype,
        topology=topology,
        density=density,
        typography=typography,
        section_treatment=section_treatment,
        focal_strategy=focal_strategy,
        figure_strategy=figure_strategy,
        reading_path=reading_path,
        palette=MappingProxyType(
            {
                "background": background,
                "surface": surface,
                "ink": ink,
                "muted": muted,
                "accent": accent,
            }
        ),
        reference_observations=reference_observations,
        directives=directives,
    )


SEED_REGISTRY: Mapping[str, SeedSpec] = MappingProxyType(
    {
        "dense-classic-three-column": _seed(
            "dense-classic-three-column",
            orientation="landscape",
            density="dense",
            design_brief=(
                "Use a compact masthead above three readable conference-poster rails.",
                "Use strong section bands to organize dense equations, figures, and results.",
                "Vary module depth while keeping one decisive result visually dominant.",
            ),
            visual_plan=_visual_plan(
                archetype="classic-rails",
                topology="modular-rails",
                density="dense",
                typography="neutral-sans",
                section_treatment="strong full-width section bands",
                focal_strategy="Break the rail rhythm with one visibly dominant evidence module.",
                figure_strategy="Make decisive plots readable and group supporting evidence compactly.",
                reading_path="Scan labeled evidence groups while entering through the dominant result.",
                palette=("#ffffff", "#f2f5f8", "#111827", "#566170", "#345f91"),
                reference_observations=(
                    "Dense evidence is grouped by strong local section hierarchy and full-width bands.",
                ),
                directives=(
                    "Balance unequal module depth inside the reference-derived macro rails.",
                ),
            ),
        ),
        "central-method-stage": _seed(
            "central-method-stage",
            orientation="landscape",
            density="balanced",
            design_brief=(
                "Give the central method or architecture the largest visual stage.",
                "Use narrower side rails for motivation, theory, and decisive evidence.",
                "Keep equations and explanations adjacent to the mechanism they support.",
            ),
            visual_plan=_visual_plan(
                archetype="central-stage",
                topology="central-stage-with-side-stacks",
                density="balanced",
                typography="neutral-sans",
                section_treatment="labeled groups",
                focal_strategy="Give the method or architecture the largest contiguous visual stage.",
                figure_strategy="Keep mechanism labels readable and place validation next to the stage it supports.",
                reading_path="Enter at the method stage, then scan adjacent motivation and results.",
                palette=("#ffffff", "#f3f6f4", "#13211b", "#56645d", "#18705c"),
                reference_observations=(
                    "A central visual stage controls the page hierarchy.",
                ),
                directives=("Do not divide the page into equal full-height panels.",),
            ),
        ),
        "claim-led-asymmetric": _seed(
            "claim-led-asymmetric",
            orientation="landscape",
            density="balanced",
            design_brief=(
                "Use one concise question-and-answer claim as the primary reading entry.",
                "Let equations, mechanisms, and evidence form unequal but aligned zones.",
                "Use restrained outlines or soft highlights instead of repeated heavy cards.",
            ),
            visual_plan=_visual_plan(
                archetype="asymmetric-claim",
                topology="asymmetric-zones",
                density="balanced",
                typography="hybrid",
                section_treatment="local dividers and selective framing",
                focal_strategy="Use the central claim as a concise oversized entry point.",
                figure_strategy="Arrange mechanisms and evidence in unequal zones around the claim.",
                reading_path="Enter at the claim, then branch to method and validation.",
                palette=("#fffefd", "#f7f2eb", "#201b18", "#6a5d55", "#9b4d32"),
                reference_observations=(
                    "Unequal zones create hierarchy without repeated card chrome.",
                ),
                directives=(
                    "Preserve asymmetry and keep the claim shorter than its evidence.",
                ),
            ),
        ),
        "open-evidence-gallery": _seed(
            "open-evidence-gallery",
            orientation="landscape",
            density="dense",
            design_brief=(
                "Use a compact academic masthead above three primary vertical evidence lanes.",
                "Keep the three-lane scan grammar while adapting lane widths, local spans, and group depth to the supplied content.",
                "Keep body content mostly open and figures legible beside concise explanation; use strong section cues at group level rather than framing every module.",
            ),
            visual_plan=_visual_plan(
                archetype="compact open evidence gallery",
                topology="three primary vertical evidence lanes with content-shaped local groups",
                density="compact evidence-rich",
                typography="neutral-sans",
                section_treatment="strong group-level section cues above open body content",
                focal_strategy="Use a compact masthead, then let evidence priority establish local emphasis across coherent groups.",
                figure_strategy="Give decisive figures substantial local width; compose related figures as readable small multiples rather than thumbnail rows.",
                reading_path="Read the compact masthead, then scan top-to-bottom within three primary lanes while following local evidence groups.",
                palette=("#ffffff", "#f4f6fa", "#111827", "#5f6877", "#315f9d"),
                reference_observations=(
                    "A compact masthead sits above three dominant vertical lanes; strong headings identify coherent groups while local depths vary.",
                ),
                directives=(
                    "Apply each strong section cue to a coherent evidence group; module bindings inside that group remain open unless their content needs local framing.",
                    "Preserve the reference's three-lane macro scan path as the default; vary widths, spans, and local group depth when content geometry or figure readability benefits.",
                    "Do not replace the lane structure with a stack of full-width horizontal bands unless the supplied geometry makes the reference topology genuinely infeasible.",
                    "Preserve open body surfaces and avoid repeated shadows, rounded containers, or module-by-module card chrome absent from the reference.",
                    "Balance independently movable modules by rendered depth without forcing equal-height groups.",
                ),
            ),
        ),
        "portrait-image-grid": _seed(
            "portrait-image-grid",
            orientation="portrait",
            density="balanced",
            design_brief=(
                "Use a portrait reading path with a compact masthead.",
                "Build varied stacked evidence groups around one dominant visual result.",
                "Keep text clusters short, dense enough, and aligned to their evidence.",
            ),
            visual_plan=_visual_plan(
                archetype="portrait-flow",
                topology="portrait-stacks",
                density="balanced",
                typography="neutral-sans",
                section_treatment="labeled groups",
                focal_strategy="Place one dominant visual early in the vertical reading path.",
                figure_strategy="Use varied stacked groups and preserve figure label size.",
                reading_path="Read downward through compact, clearly separated evidence groups.",
                palette=("#ffffff", "#f4f6f8", "#15171a", "#626973", "#2e648c"),
                reference_observations=(
                    "A compact vertical sequence avoids manuscript-like pagination.",
                ),
                directives=(
                    "Vary group depth instead of repeating equal horizontal bands.",
                ),
            ),
        ),
    }
)


def seed_by_id(seed_id: str) -> SeedSpec:
    """Return one registered seed by stable identifier."""

    try:
        return SEED_REGISTRY[str(seed_id)]
    except KeyError as exc:
        raise ReferenceSeedError(f"unknown reference seed: {seed_id}") from exc


def select_seed(
    *,
    orientation: str,
    organization_mode: str,
    figure_count: int,
    module_weights: Mapping[str, float],
    has_equations: bool,
    has_tables: bool,
    density: str,
) -> SeedSpec:
    """Select one seed deterministically from grounded composition signals."""

    page_mode = str(orientation).strip().lower()
    if page_mode not in {"landscape", "portrait"}:
        raise ReferenceSeedError("orientation must be landscape or portrait")
    mode = str(organization_mode).strip().lower()
    if mode not in poster_core.ORGANIZATION_MODES:
        raise ReferenceSeedError("unsupported organization_mode")
    if isinstance(figure_count, bool) or not isinstance(figure_count, int):
        raise ReferenceSeedError("figure_count must be a non-negative integer")
    if figure_count < 0:
        raise ReferenceSeedError("figure_count must be a non-negative integer")
    if not isinstance(has_equations, bool) or not isinstance(has_tables, bool):
        raise ReferenceSeedError("equation and table signals must be booleans")
    weights = _normalize_weights(module_weights)
    density_mode = normalize_density(density)

    if page_mode == "portrait":
        return SEED_REGISTRY["portrait-image-grid"]
    method_weight = _role_weight(weights, "method", "method-flow", "architecture")
    claim_weight = _role_weight(weights, "claim", "result", "context")
    evidence_weight = _role_weight(
        weights, "evidence", "figure", "figures", "comparison", "metrics"
    )
    role_total = method_weight + claim_weight + evidence_weight
    if role_total > 0:
        method_share = method_weight / role_total
        claim_share = claim_weight / role_total
        evidence_share = evidence_weight / role_total
    else:
        method_share = claim_share = evidence_share = 0.0

    scores = {seed_id: 0.0 for seed_id in _LANDSCAPE_SEED_ORDER}
    mode_affinity = {
        "scan-first": {
            "dense-classic-three-column": 0.7,
            "central-method-stage": 0.4,
            "claim-led-asymmetric": 0.6,
            "open-evidence-gallery": 0.8,
        },
        "figure-led": {
            "dense-classic-three-column": 0.5,
            "open-evidence-gallery": 4.0,
        },
        "method-led": {
            "central-method-stage": 4.0,
            "claim-led-asymmetric": 0.3,
        },
        "result-led": {
            "claim-led-asymmetric": 4.0,
            "open-evidence-gallery": 1.0,
        },
        "narrative": {
            "central-method-stage": 0.5,
            "claim-led-asymmetric": 3.5,
        },
    }
    for seed_id, affinity in mode_affinity[mode].items():
        scores[seed_id] += affinity

    scores["central-method-stage"] += 4.0 * method_share
    scores["claim-led-asymmetric"] += 4.0 * claim_share
    scores["open-evidence-gallery"] += 4.0 * evidence_share
    scores["dense-classic-three-column"] += 1.2 * evidence_share

    figure_pressure = min(figure_count, 6) / 6.0
    scores["open-evidence-gallery"] += 2.2 * figure_pressure
    scores["dense-classic-three-column"] += 0.8 * figure_pressure
    scores["central-method-stage"] += 0.3 * figure_pressure
    scores["claim-led-asymmetric"] += 0.8 * (1.0 - figure_pressure)

    density_affinity = {
        "open": {
            "central-method-stage": 0.5,
            "claim-led-asymmetric": 0.8,
            "open-evidence-gallery": 1.8,
        },
        "balanced": {
            "dense-classic-three-column": 0.3,
            "central-method-stage": 1.0,
            "claim-led-asymmetric": 1.0,
            "open-evidence-gallery": 0.7,
        },
        "dense": {
            "dense-classic-three-column": 3.0,
            "central-method-stage": 0.4,
            "open-evidence-gallery": 1.0,
        },
    }
    for seed_id, affinity in density_affinity[density_mode].items():
        scores[seed_id] += affinity

    if has_equations:
        scores["central-method-stage"] += 1.5
        scores["dense-classic-three-column"] += 0.8
    if has_tables:
        scores["dense-classic-three-column"] += 4.0
    if has_equations and has_tables:
        scores["dense-classic-three-column"] += 1.0

    selected = max(
        enumerate(_LANDSCAPE_SEED_ORDER),
        key=lambda item: (scores[item[1]], -item[0]),
    )[1]
    return SEED_REGISTRY[selected]


def load_seed_bundle(
    seed: SeedSpec,
    *,
    density: str | None = None,
    warning: str | None = None,
) -> ReferenceBundle:
    """Load and hash a selected seed without treating it as a paper asset."""

    registered = seed_by_id(seed.seed_id)
    if seed != registered:
        raise ReferenceSeedError(
            "reference seed must match its complete registry entry"
        )
    image = registered.image_path
    try:
        resolved = image.resolve(strict=True)
    except OSError as exc:
        raise ReferenceSeedError(f"reference seed image is missing: {image}") from exc
    return ReferenceBundle(
        image_path=str(resolved),
        image_sha256=_sha256_file(resolved),
        source_kind="seed",
        seed_id=registered.seed_id,
        orientation=registered.orientation,
        density=normalize_density(density or registered.density),
        design_brief=registered.design_brief,
        warning=warning,
    )


def normalize_density(value: str) -> str:
    """Validate one explicit seed-density value."""

    key = str(value).strip().lower()
    if key not in _DENSITIES:
        raise ReferenceSeedError("density must be one of: open, balanced, dense")
    return key


def _normalize_weights(value: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ReferenceSeedError("module_weights must be an object")
    normalized: dict[str, float] = {}
    for raw_key, raw_weight in value.items():
        key = str(raw_key).strip().lower()
        if not key or isinstance(raw_weight, bool):
            raise ReferenceSeedError("module_weights are invalid")
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError) as exc:
            raise ReferenceSeedError("module_weights are invalid") from exc
        if not math.isfinite(weight) or weight < 0:
            raise ReferenceSeedError("module_weights must be finite and non-negative")
        normalized[key] = weight
    return normalized


def _role_weight(weights: Mapping[str, float], *keys: str) -> float:
    return sum(weights.get(key, 0.0) for key in keys)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "NON_AUTHORITATIVE_REFERENCE_POLICY",
    "ReferenceBundle",
    "ReferenceSeedError",
    "SEED_REGISTRY",
    "SeedSpec",
    "SeedVisualPlan",
    "load_seed_bundle",
    "normalize_density",
    "seed_by_id",
    "select_seed",
]
