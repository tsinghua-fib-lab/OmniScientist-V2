"""Ground structured artifact-edit contracts against artifact elements.

Natural-language interpretation belongs to ``SemanticPlanner``. This module
accepts only normalized contract fields (target, style, scope) and resolves an
exact target in the source artifact. It deliberately contains no language
lexicon or user-intent classifier.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ArtifactElement:
    id: str
    label: str
    kind: str
    start: int
    end: int

    @property
    def search_text(self) -> str:
        return f"{self.id} {self.label}"


@dataclass(frozen=True, slots=True)
class ArtifactIntent:
    action: str = "question"
    target: str = ""
    change: str = ""
    style: str = ""
    confidence: float = 0.0
    needs_confirmation: bool = False
    matched_element: ArtifactElement | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


_CANONICAL_STYLES = {
    "blue",
    "cyan",
    "teal",
    "green",
    "purple",
    "pink",
    "orange",
    "neutral",
}
_HEX_COLOR_RE = re.compile(r"#[0-9a-f]{6}\Z", re.IGNORECASE)


def artifact_intent_from_spec(
    spec: dict[str, Any], *, elements: list[ArtifactElement] | None = None
) -> ArtifactIntent | None:
    """Validate and ground a planner-produced artifact edit specification."""
    target = str(spec.get("target") or "").strip()
    style = str(spec.get("style") or "").strip().lower()
    scope = str(spec.get("scope") or "element").strip().lower()
    if not target or not _valid_style(style) or scope != "element":
        return None
    match = resolve_named_element(target, elements or [])
    if match is None:
        return None
    return ArtifactIntent(
        action="minor_artifact_revision",
        target=match.label or match.id,
        change=str(spec.get("instruction") or ""),
        style=style,
        confidence=1.0,
        matched_element=match,
        reasons=("structured-contract", f"style:{style}"),
    )


def resolve_named_element(target: str, elements: list[ArtifactElement]) -> ArtifactElement | None:
    """Resolve an exact normalized id or label, rejecting ambiguous matches."""
    normalized = normalize_text(target)
    if not normalized:
        return None
    matches = [
        element
        for element in elements
        if normalized in {normalize_text(element.id), normalize_text(element.label)}
    ]
    return matches[0] if len(matches) == 1 else None


def normalize_text(value: str) -> str:
    """Normalize identifiers without assumptions about the writing system."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _valid_style(style: str) -> bool:
    return style in _CANONICAL_STYLES or bool(_HEX_COLOR_RE.fullmatch(style))
