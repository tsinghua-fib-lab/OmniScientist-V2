"""Deterministic target resolution before runtime execution.

The model may describe a paper, artifact, or task in natural language, but the
runtime must not pass weak guesses into strong tool contracts.  This module is
the small, auditable resolver used by validators and executors before tools run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

_ARXIV_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b", re.IGNORECASE)
_ARXIV_URL_RE = re.compile(
    r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/(?P<id>[^/?#\s]+?)(?:\.pdf)?/?$",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")
_TITLE_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

@dataclass(frozen=True, slots=True)
class TargetResolution:
    kind: str
    value: str = ""
    label: str = ""
    confidence: float = 0.0
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.kind == "resolved" and bool(self.value)


def is_arxiv_identifier(value: str) -> bool:
    """Return true only for bare/versioned arXiv ids or arXiv abs/pdf URLs."""
    raw = (value or "").strip()
    if not raw:
        return False
    if _ARXIV_URL_RE.match(raw):
        return True
    return bool(_ARXIV_ID_RE.fullmatch(raw.removeprefix("arXiv:").removeprefix("arxiv:")))


def extract_arxiv_identifier(value: str) -> str:
    """Extract a canonical arXiv id from text when one is explicitly present."""
    raw = (value or "").strip()
    if not raw:
        return ""
    match = _ARXIV_URL_RE.match(raw)
    if match:
        return _strip_pdf_suffix(match.group("id"))
    match = _ARXIV_ID_RE.search(raw)
    return match.group(1) if match else ""


def resolve_paper_title(value: str) -> TargetResolution:
    """Resolve paper titles only through deterministic offline resolvers.

    The runtime deliberately ships with no baked-in title map.  A title is not a
    stable identifier; callers should use a title/search resolver or ask for an
    arXiv id/URL before running an identifier-bound skill.
    """
    title = _normalise_title(value)
    if not title:
        return TargetResolution(kind="missing", reason="empty title")
    return TargetResolution(kind="missing", label=value.strip(), reason="unknown paper title")


def resolve_arxiv_identifier_from_fields(fields: dict[str, Any]) -> TargetResolution:
    """Resolve an arXiv identifier from model-planned input fields.

    Strong identifier fields must contain real identifiers.  Title-like fields
    may resolve through a conservative title map; otherwise the caller should ask
    for an arXiv id/URL or route to a search skill.
    """
    strong_fields = ("identifier", "arxiv_id", "arxiv", "id", "url")
    for name in strong_fields:
        value = _string_field(fields.get(name))
        if not value:
            continue
        extracted = extract_arxiv_identifier(value)
        if extracted and (name != "url" or _is_arxiv_url_or_text(value)):
            return TargetResolution(
                kind="resolved",
                value=extracted,
                label=value,
                confidence=0.98,
                reason=f"explicit arXiv identifier in {name}",
            )
        if name in {"identifier", "arxiv_id", "arxiv", "id"}:
            return TargetResolution(
                kind="invalid",
                label=value,
                reason=f"{name} is not an arXiv id or arXiv URL",
            )

    for name in ("paper", "title", "paper_title"):
        value = _string_field(fields.get(name))
        if not value:
            continue
        extracted = extract_arxiv_identifier(value)
        if extracted:
            return TargetResolution(
                kind="resolved",
                value=extracted,
                label=value,
                confidence=0.96,
                reason=f"explicit arXiv identifier embedded in {name}",
            )
        resolved = resolve_paper_title(value)
        if resolved.resolved:
            return resolved
        return TargetResolution(
            kind="missing",
            label=value,
            reason="paper title requires title resolver/search before identifier-bound execution",
        )

    text = _string_field(fields.get("input") or fields.get("query") or fields.get("target"))
    if text:
        extracted = extract_arxiv_identifier(text)
        if extracted:
            return TargetResolution(
                kind="resolved",
                value=extracted,
                label=text,
                confidence=0.94,
                reason="explicit arXiv identifier embedded in text input",
            )
        title_match = resolve_paper_title(text)
        if title_match.resolved:
            return title_match

    return TargetResolution(kind="missing", reason="no arXiv id or arXiv URL found")


def _string_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _normalise_title(value: str) -> str:
    text = _TITLE_PUNCT_RE.sub(" ", (value or "").strip().lower())
    return _WS_RE.sub(" ", text).strip()


def _strip_pdf_suffix(value: str) -> str:
    return value.removesuffix(".pdf").removesuffix(".PDF")


def _is_arxiv_url_or_text(value: str) -> bool:
    raw = (value or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return True
    host = urlparse(raw).netloc.lower()
    return host in {"arxiv.org", "www.arxiv.org"}
