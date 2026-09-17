"""Built-in visual seed selection and immutable reference bundles."""

from __future__ import annotations

import hashlib
import math
import mimetypes
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import poster_core

from posterlib.paths import SKILL_ROOT

Orientation = Literal["landscape", "portrait"]
ReferenceSourceKind = Literal["custom", "generated", "seed"]
SeedLeadPlacement = Literal["flow", "full-width", "center-lane"]
SeedSectionStyle = Literal["open", "rule", "band", "outlined"]
SeedModuleStyle = Literal["open", "outlined", "selective"]
SeedMastheadScale = Literal["compact", "balanced", "prominent"]
SeedMastheadAlignment = Literal["left", "center"]

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SEED_ROOT = SKILL_ROOT / "seeds"
_DENSITIES = frozenset({"open", "balanced", "dense"})
_ROLE_EMPHASES = frozenset({"balanced", "claim", "evidence", "method"})
_CUSTOM_REFERENCE_ID = "custom-reference"


class ReferenceSeedError(ValueError):
    """A seed selection or reference bundle is invalid."""


@dataclass(frozen=True)
class SeedSpec:
    """Transferable design properties for one conference-poster reference."""

    seed_id: str
    image_path: Path
    orientation: Orientation
    density: str
    organization_modes: tuple[str, ...]
    role_emphasis: str
    figure_preference: float
    supports_equations: bool = False
    supports_tables: bool = False
    column_weights: tuple[float, ...] = ()
    lead_placement: SeedLeadPlacement = "flow"
    section_style: SeedSectionStyle = "rule"
    module_style: SeedModuleStyle = "selective"
    masthead_scale: SeedMastheadScale = "balanced"
    masthead_alignment: SeedMastheadAlignment = "left"
    accent_color: str = "#315f9d"


@dataclass(frozen=True)
class ReferenceBundle:
    """A hash-bound visual reference that cannot act as scientific evidence."""

    image_path: str
    image_sha256: str
    source_kind: ReferenceSourceKind
    seed_id: str
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
        if self.source_kind not in {"custom", "generated", "seed"}:
            raise ReferenceSeedError(
                "reference source_kind must be custom, generated, or seed"
            )
        if self.source_kind == "custom":
            if self.seed_id != _CUSTOM_REFERENCE_ID:
                raise ReferenceSeedError(
                    "custom reference must use the stable custom reference id"
                )
            mime_type = mimetypes.guess_type(image.name)[0] or ""
            if not mime_type.startswith("image/"):
                raise ReferenceSeedError("custom reference must use an image file")
        else:
            registered = SEED_REGISTRY.get(self.seed_id)
            if registered is None:
                raise ReferenceSeedError(f"unknown reference seed: {self.seed_id}")
        if self.source_kind == "seed":
            registered = SEED_REGISTRY[self.seed_id]
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
        warning = None if self.warning is None else str(self.warning).strip()
        if self.warning is not None and not warning:
            raise ReferenceSeedError("reference warning must be non-empty when present")
        object.__setattr__(self, "image_path", str(image))
        object.__setattr__(self, "warning", warning)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-safe object kept separate from paper manifests."""

        return {
            "image_path": self.image_path,
            "image_sha256": self.image_sha256,
            "source_kind": self.source_kind,
            "seed_id": self.seed_id,
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
            "warning",
        }
        fields = frozenset(value)
        legacy_fields = {"orientation", "density", "non_authoritative_policy"}
        if fields not in {
            frozenset(required),
            frozenset(required | {"orientation", "density"}),
            frozenset(required | legacy_fields),
        }:
            raise ReferenceSeedError("reference bundle fields are invalid")
        warning = value["warning"]
        if warning is not None and not isinstance(warning, str):
            raise ReferenceSeedError("reference warning must be a string or null")
        return cls(
            image_path=str(value["image_path"]),
            image_sha256=str(value["image_sha256"]),
            source_kind=str(value["source_kind"]),  # type: ignore[arg-type]
            seed_id=str(value["seed_id"]),
            warning=warning,
        )


def _seed(
    seed_id: str,
    *,
    orientation: Orientation,
    density: str,
    organization_modes: tuple[str, ...],
    role_emphasis: str,
    figure_preference: float,
    column_weights: tuple[float, ...],
    lead_placement: SeedLeadPlacement,
    section_style: SeedSectionStyle,
    module_style: SeedModuleStyle,
    masthead_scale: SeedMastheadScale,
    masthead_alignment: SeedMastheadAlignment,
    accent_color: str,
    supports_equations: bool = False,
    supports_tables: bool = False,
) -> SeedSpec:
    filename = f"{seed_id}.png"
    return SeedSpec(
        seed_id=seed_id,
        image_path=_SEED_ROOT / filename,
        orientation=orientation,
        density=density,
        organization_modes=organization_modes,
        role_emphasis=role_emphasis,
        figure_preference=figure_preference,
        supports_equations=supports_equations,
        supports_tables=supports_tables,
        column_weights=column_weights,
        lead_placement=lead_placement,
        section_style=section_style,
        module_style=module_style,
        masthead_scale=masthead_scale,
        masthead_alignment=masthead_alignment,
        accent_color=accent_color,
    )


SEED_REGISTRY: Mapping[str, SeedSpec] = MappingProxyType(
    {
        "dense-classic-three-column": _seed(
            "dense-classic-three-column",
            orientation="landscape",
            density="dense",
            organization_modes=("scan-first", "figure-led"),
            role_emphasis="balanced",
            figure_preference=0.7,
            column_weights=(1.0, 1.0, 1.0),
            lead_placement="flow",
            section_style="band",
            module_style="outlined",
            masthead_scale="compact",
            masthead_alignment="center",
            accent_color="#315f9d",
            supports_equations=True,
            supports_tables=True,
        ),
        "central-method-stage": _seed(
            "central-method-stage",
            orientation="landscape",
            density="balanced",
            organization_modes=("method-led", "scan-first"),
            role_emphasis="method",
            figure_preference=0.4,
            column_weights=(0.85, 1.3, 0.85),
            lead_placement="center-lane",
            section_style="rule",
            module_style="selective",
            masthead_scale="compact",
            masthead_alignment="center",
            accent_color="#1d4f91",
            supports_equations=True,
        ),
        "claim-led-asymmetric": _seed(
            "claim-led-asymmetric",
            orientation="landscape",
            density="balanced",
            organization_modes=("result-led", "narrative", "scan-first"),
            role_emphasis="claim",
            figure_preference=0.3,
            column_weights=(1.12, 1.0, 0.88),
            lead_placement="full-width",
            section_style="open",
            module_style="selective",
            masthead_scale="compact",
            masthead_alignment="left",
            accent_color="#c91f1f",
        ),
        "open-evidence-gallery": _seed(
            "open-evidence-gallery",
            orientation="landscape",
            density="dense",
            organization_modes=("figure-led", "scan-first", "result-led"),
            role_emphasis="evidence",
            figure_preference=0.95,
            column_weights=(0.9, 1.2, 0.9),
            lead_placement="center-lane",
            section_style="band",
            module_style="open",
            masthead_scale="compact",
            masthead_alignment="center",
            accent_color="#315f9d",
            supports_tables=True,
        ),
        "portrait-image-grid": _seed(
            "portrait-image-grid",
            orientation="portrait",
            density="balanced",
            organization_modes=tuple(poster_core.ORGANIZATION_MODES),
            role_emphasis="balanced",
            figure_preference=0.8,
            column_weights=(1.0, 1.0),
            lead_placement="flow",
            section_style="band",
            module_style="outlined",
            masthead_scale="prominent",
            masthead_alignment="center",
            accent_color="#c81e1e",
            supports_equations=True,
            supports_tables=True,
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

    figure_pressure = min(figure_count, 6) / 6.0
    role_shares = {
        "balanced": (method_share + claim_share + evidence_share) / 3.0,
        "method": method_share,
        "claim": claim_share,
        "evidence": evidence_share,
    }
    density_index = {"open": 0, "balanced": 1, "dense": 2}
    candidates = [
        seed for seed in SEED_REGISTRY.values() if seed.orientation == page_mode
    ]
    if not candidates:
        raise ReferenceSeedError(
            f"no reference seed is registered for {page_mode} orientation"
        )

    def score(seed: SeedSpec) -> float:
        if seed.role_emphasis not in _ROLE_EMPHASES:
            raise ReferenceSeedError(
                f"reference seed {seed.seed_id!r} has invalid role metadata"
            )
        value = 3.0 if mode in seed.organization_modes else 0.0
        value += 4.0 * role_shares[seed.role_emphasis]
        value += 2.0 * (1.0 - abs(seed.figure_preference - figure_pressure))
        density_distance = abs(
            density_index[normalize_density(seed.density)] - density_index[density_mode]
        )
        value += max(0.0, 1.5 - density_distance)
        if has_equations and seed.supports_equations:
            value += 1.2
        if has_tables and seed.supports_tables:
            value += 0.8
        return value

    return max(enumerate(candidates), key=lambda item: (score(item[1]), -item[0]))[1]


def load_seed_bundle(
    seed: SeedSpec,
    *,
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
        warning=warning,
    )


def load_custom_bundle(
    image_path: str | Path,
) -> ReferenceBundle:
    """Bind an arbitrary reference, reusing registered grammar for registry assets."""

    image = Path(image_path).expanduser()
    if image.is_symlink():
        raise ReferenceSeedError("custom reference image may not be a symlink")
    try:
        resolved = image.resolve(strict=True)
    except OSError as exc:
        raise ReferenceSeedError(
            f"custom reference image is not readable: {image_path}"
        ) from exc
    for seed in SEED_REGISTRY.values():
        try:
            registered_path = seed.image_path.resolve(strict=True)
        except OSError:
            continue
        if registered_path == resolved:
            return load_seed_bundle(seed)
    return ReferenceBundle(
        image_path=str(resolved),
        image_sha256=_sha256_file(resolved),
        source_kind="custom",
        seed_id=_CUSTOM_REFERENCE_ID,
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
    "SEED_REGISTRY",
    "ReferenceBundle",
    "ReferenceSeedError",
    "SeedSpec",
    "load_custom_bundle",
    "load_seed_bundle",
    "normalize_density",
    "seed_by_id",
    "select_seed",
]
