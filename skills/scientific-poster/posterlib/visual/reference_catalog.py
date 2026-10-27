"""Hash-bound poster reference selection and custom-image bundles."""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from posterlib.paths import SKILL_ROOT
from posterlib.runtime.identifiers import is_sha256

Orientation = Literal["landscape", "portrait"]
ReferenceSourceKind = Literal["custom", "generated", "seed"]

_SEED_ROOT = SKILL_ROOT / "seeds"
_CUSTOM_REFERENCE_ID = "custom-reference"


class ReferenceSeedError(ValueError):
    """A seed selection or reference bundle is invalid."""


@dataclass(frozen=True)
class SeedSpec:
    """Stable identity, orientation and attribution for one reference image."""

    seed_id: str
    image_path: Path
    orientation: Orientation
    source_url: str = ""


@dataclass(frozen=True)
class ReferenceBundle:
    """A hash-bound visual reference that cannot act as scientific evidence."""

    image_path: str
    image_sha256: str
    source_kind: ReferenceSourceKind
    seed_id: str
    warning: str | None = None

    def __post_init__(self) -> None:
        if not str(self.image_path).strip():
            raise ReferenceSeedError("reference image path must be non-empty")
        image = Path(self.image_path).expanduser().absolute()
        digest = str(self.image_sha256)
        if not is_sha256(digest):
            raise ReferenceSeedError("reference image hash is invalid")
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
        elif self.seed_id not in SEED_REGISTRY:
            raise ReferenceSeedError(f"unknown reference seed: {self.seed_id}")
        warning = None if self.warning is None else str(self.warning).strip()
        if self.warning is not None and not warning:
            raise ReferenceSeedError(
                "reference warning must be non-empty when present"
            )
        object.__setattr__(self, "image_path", str(image))
        object.__setattr__(self, "warning", warning)

    def verified_image_path(self) -> Path:
        """Resolve exact reference pixels when needed, including relocated seeds."""

        candidates = [Path(self.image_path)]
        if self.source_kind == "seed":
            candidates.append(SEED_REGISTRY[self.seed_id].image_path)
        for image in dict.fromkeys(candidates):
            try:
                if (
                    not image.is_symlink()
                    and image.is_file()
                    and _sha256_file(image) == self.image_sha256
                ):
                    return image.resolve(strict=True)
            except OSError:
                continue
        raise ReferenceSeedError(
            f"reference image is unavailable or changed: {self.image_path}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-safe reference identity."""

        return {
            "image_path": self.image_path,
            "image_sha256": self.image_sha256,
            "source_kind": self.source_kind,
            "seed_id": self.seed_id,
            "warning": self.warning,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReferenceBundle:
        """Restore reference identity independently of optional image availability."""

        required = {
            "image_path",
            "image_sha256",
            "source_kind",
            "seed_id",
            "warning",
        }
        fields = frozenset(value)
        if fields != frozenset(required):
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
    source_url: str = "",
) -> SeedSpec:
    return SeedSpec(
        seed_id=seed_id,
        image_path=_SEED_ROOT / f"{seed_id}.png",
        orientation=orientation,
        source_url=source_url,
    )


SEED_REGISTRY: Mapping[str, SeedSpec] = MappingProxyType(
    {
        "reading-research-landscape": _seed(
            "reading-research-landscape",
            orientation="landscape",
            source_url="https://doi.org/10.6084/m9.figshare.28903940.v2",
        ),
        "genomics-portrait": _seed(
            "genomics-portrait",
            orientation="portrait",
            source_url="https://doi.org/10.6084/m9.figshare.28989440.v5",
        ),
        # Synthetic boards retain their identity for saved draft checkpoints.
        "dense-classic-three-column": _seed(
            "dense-classic-three-column",
            orientation="landscape",
        ),
        "central-method-stage": _seed(
            "central-method-stage",
            orientation="landscape",
        ),
        "claim-led-asymmetric": _seed(
            "claim-led-asymmetric",
            orientation="landscape",
        ),
        "open-evidence-gallery": _seed(
            "open-evidence-gallery",
            orientation="landscape",
        ),
        "portrait-image-grid": _seed(
            "portrait-image-grid",
            orientation="portrait",
        ),
    }
)


def seed_by_id(seed_id: str) -> SeedSpec:
    """Return one registered seed by stable identifier."""

    try:
        return SEED_REGISTRY[str(seed_id)]
    except KeyError as exc:
        raise ReferenceSeedError(f"unknown reference seed: {seed_id}") from exc


def select_seed(*, orientation: str) -> SeedSpec:
    """Select the first attributed reference registered for the page orientation."""

    page_mode = str(orientation).strip().lower()
    if page_mode not in {"landscape", "portrait"}:
        raise ReferenceSeedError("orientation must be landscape or portrait")
    for seed in SEED_REGISTRY.values():
        if seed.orientation == page_mode and seed.source_url:
            return seed
    raise ReferenceSeedError(
        f"no attributed reference is registered for {page_mode} orientation"
    )


def load_seed_bundle(
    seed: SeedSpec,
    *,
    warning: str | None = None,
) -> ReferenceBundle:
    """Load and hash a selected registered seed."""

    registered = seed_by_id(seed.seed_id)
    if seed != registered:
        raise ReferenceSeedError(
            "reference seed must match its complete registry entry"
        )
    try:
        image = registered.image_path.resolve(strict=True)
    except OSError as exc:
        raise ReferenceSeedError(
            f"reference seed image is missing: {registered.image_path}"
        ) from exc
    if not image.is_file():
        raise ReferenceSeedError(f"reference image must be a file: {image}")
    return ReferenceBundle(
        image_path=str(image),
        image_sha256=_sha256_file(image),
        source_kind="seed",
        seed_id=registered.seed_id,
        warning=warning,
    )


def load_custom_bundle(image_path: str | Path) -> ReferenceBundle:
    """Bind an explicit user reference without changing its provenance."""

    image = Path(image_path).expanduser()
    if image.is_symlink():
        raise ReferenceSeedError("custom reference image may not be a symlink")
    try:
        resolved = image.resolve(strict=True)
    except OSError as exc:
        raise ReferenceSeedError(
            f"custom reference image is not readable: {image_path}"
        ) from exc
    if not resolved.is_file():
        raise ReferenceSeedError(f"reference image must be a file: {resolved}")
    return ReferenceBundle(
        image_path=str(resolved),
        image_sha256=_sha256_file(resolved),
        source_kind="custom",
        seed_id=_CUSTOM_REFERENCE_ID,
    )


def custom_reference_orientation(image_path: str | Path) -> Orientation:
    """Read the physical orientation of an explicit poster image reference."""

    import pymupdf

    reference = load_custom_bundle(image_path)
    try:
        with pymupdf.open(reference.image_path) as document:
            page = document[0]
            return "portrait" if page.rect.height > page.rect.width else "landscape"
    except (IndexError, OSError, RuntimeError, ValueError) as exc:
        raise ReferenceSeedError("custom reference image has no readable dimensions") from exc


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
    "seed_by_id",
    "select_seed",
]
