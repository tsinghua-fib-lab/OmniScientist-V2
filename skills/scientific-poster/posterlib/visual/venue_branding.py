"""Resolve explicit conference branding to vetted, local poster resources."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from posterlib.paths import SKILL_ROOT

VenueId = Literal["icml", "neurips", "iclr", "cvpr"]

VENUE_ASSET_ROOT = SKILL_ROOT / "assets" / "venue-branding"

_ICML_LOGO_SHA256 = "b42f6d492543f794bc1ffd7eacdb1579a8b6ea352b806cc933c0b65576520da3"
_NEURIPS_LOGO_SHA256 = (
    "b582019d2789b6706dde66b046e6ffc8c7a5d029a609bd1783eb392e8ea34fce"
)
_ICLR_LOGO_SHA256 = "4f13044841d3450c51ea4200da1e0414d3dc22a61902775263e7fc3f21cdcca5"
_CVPR_LOGO_SHA256 = "f2f873218d52d84d91fa9ba2daf8f44b9dd232157f1f95c3cd5bb97780c2eae0"
_VENUE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<name>ICML|NeurIPS|NIPS|ICLR|CVPR)(?![A-Za-z0-9])"
    r"(?:\s*[-–—:/]?\s*(?P<year>\d{4})(?!\d))?",
    re.IGNORECASE,
)


class VenueBrandingError(ValueError):
    """A bundled venue resource is missing or no longer hash-bound."""


@dataclass(frozen=True)
class _VenueSpec:
    venue_id: VenueId
    evidence_uri: str
    logo_relative_path: str | None = None
    logo_sha256: str | None = None


@dataclass(frozen=True)
class VenueBranding:
    """Display identity for one unambiguous, caller-requested conference."""

    venue_id: VenueId
    label: str
    evidence_uri: str
    distinction: str | None
    logo_path: Path | None
    logo_sha256: str | None


_SPECS = MappingProxyType(
    {
        "icml": _VenueSpec(
            venue_id="icml",
            evidence_uri="https://icml.cc/Conferences/2026/Press",
            logo_relative_path="icml/logo.svg",
            logo_sha256=_ICML_LOGO_SHA256,
        ),
        "neurips": _VenueSpec(
            venue_id="neurips",
            evidence_uri="https://neurips.cc/Conferences/2026/Press",
            logo_relative_path="neurips/logo.svg",
            logo_sha256=_NEURIPS_LOGO_SHA256,
        ),
        "iclr": _VenueSpec(
            venue_id="iclr",
            evidence_uri="https://www.iclr.cc/Conferences/2025/Press",
            logo_relative_path="iclr/logo.svg",
            logo_sha256=_ICLR_LOGO_SHA256,
        ),
        "cvpr": _VenueSpec(
            venue_id="cvpr",
            evidence_uri="https://cvpr.thecvf.com/Conferences/2026/PosterPrinting",
            logo_relative_path="cvpr/logo.svg",
            logo_sha256=_CVPR_LOGO_SHA256,
        ),
    }
)


def resolve_venue_branding(
    target: str,
    *,
    distinction: str | None = None,
) -> VenueBranding | None:
    """Resolve one explicit venue mention without guessing from a generic style.

    Ambiguous requests that mention multiple venues or multiple years return ``None``.
    Award or track distinctions are never inferred; callers must supply them explicitly.
    """

    matches = list(_VENUE_RE.finditer(str(target)))
    if not matches:
        return None

    identities = {_canonical_id(match.group("name")) for match in matches}
    if len(identities) > 1:
        dated = [match for match in matches if match.group("year")]
        if len(dated) != 1:
            return None
        matches = dated
        identities = {_canonical_id(matches[0].group("name"))}
    years = {match.group("year") for match in matches if match.group("year")}
    if len(years) > 1:
        return None

    venue_id = identities.pop()
    year = next(iter(years), None)
    first_name = matches[0].group("name")
    display_name = _display_name(venue_id, matched_name=first_name)
    label = f"{display_name} {year}" if year else display_name
    normalized_distinction = " ".join(str(distinction or "").split()) or None
    spec = _SPECS[venue_id]
    logo_path, logo_sha256 = _resolve_vetted_logo(spec)
    return VenueBranding(
        venue_id=venue_id,
        label=label,
        evidence_uri=spec.evidence_uri,
        distinction=normalized_distinction,
        logo_path=logo_path,
        logo_sha256=logo_sha256,
    )


def resolve_verified_identity(
    identity: object,
) -> VenueBranding | None:
    """Bind a structured supported venue only when its provenance matches."""

    if not isinstance(identity, Mapping) or not identity.get("venue_id"):
        return None
    venue_id = str(identity.get("venue_id") or "").strip().lower()
    if venue_id not in _SPECS:
        raise VenueBrandingError("venue_identity venue_id is not supported")
    label = " ".join(str(identity.get("label") or "").split())
    evidence_uri = str(identity.get("evidence_uri") or "").strip()
    resolved = resolve_venue_branding(
        label,
        distinction=str(identity.get("distinction") or "") or None,
    )
    spec = _SPECS[venue_id]
    if (
        resolved is None
        or resolved.venue_id != venue_id
        or evidence_uri != spec.evidence_uri
    ):
        raise VenueBrandingError(
            "venue_identity label, venue_id, and evidence_uri do not match"
        )
    return resolved


def _canonical_id(name: str) -> VenueId:
    normalized = name.casefold()
    if normalized == "icml":
        return "icml"
    if normalized in {"neurips", "nips"}:
        return "neurips"
    if normalized == "iclr":
        return "iclr"
    return "cvpr"


def _display_name(venue_id: VenueId, *, matched_name: str) -> str:
    if venue_id == "icml":
        return "ICML"
    if venue_id == "iclr":
        return "ICLR"
    if venue_id == "cvpr":
        return "CVPR"
    return "NIPS" if matched_name.casefold() == "nips" else "NeurIPS"


def _resolve_vetted_logo(spec: _VenueSpec) -> tuple[Path | None, str | None]:
    relative_path = spec.logo_relative_path
    expected_hash = spec.logo_sha256
    if relative_path is None or expected_hash is None:
        return None, None

    candidate = VENUE_ASSET_ROOT / relative_path
    if candidate.is_symlink():
        raise VenueBrandingError("venue logo must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(VENUE_ASSET_ROOT.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise VenueBrandingError(
            "vetted venue logo is missing or outside the asset root"
        ) from exc
    if not resolved.is_file():
        raise VenueBrandingError("vetted venue logo is not a file")
    actual_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise VenueBrandingError(
            "vetted venue logo hash does not match its registered bytes"
        )
    return resolved, expected_hash


__all__ = [
    "VENUE_ASSET_ROOT",
    "VenueBranding",
    "VenueBrandingError",
    "VenueId",
    "resolve_venue_branding",
    "resolve_verified_identity",
]
