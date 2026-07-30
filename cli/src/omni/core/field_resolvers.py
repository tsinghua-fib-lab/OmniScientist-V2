"""Schema field resolvers used by pre-tool contract checks.

Skill contracts declare field formats and optional resolver names. The runtime
uses this small registry to resolve semantic candidates into canonical values
before a skill is allowed to run. Domain-specific adapters live here instead of
in plan validation or skill-name branches.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from omni.core.path_lookup import (
    QUOTE_WRAPPERS,
    closer_for_quote,
    resolve_existing_path,
    unwrap_matching_quotes,
)
from omni.core.target_resolver import (
    extract_arxiv_identifier,
    resolve_arxiv_identifier_from_fields,
)


@dataclass(frozen=True, slots=True)
class FieldResolution:
    resolved: bool = False
    value: str = ""
    label: str = ""
    reason: str = ""


FieldResolver = Callable[[dict[str, Any]], FieldResolution]
FieldSearcher = Callable[[str], Awaitable[list[tuple[str, str]]]]


@dataclass(frozen=True, slots=True)
class FieldResolverSpec:
    """One fact resolver and its optional grounded lookup adapter."""

    resolve: FieldResolver
    search: FieldSearcher | None = None


def resolve_field(
    resolver_name: str,
    fields: dict[str, Any],
) -> FieldResolution:
    """Resolve a field through a named/format resolver adapter."""
    spec = _RESOLVERS.get(_normalise(resolver_name))
    if spec is None:
        return FieldResolution(reason=f"unknown resolver: {resolver_name}")
    return spec.resolve(fields)


def has_resolver(name: str) -> bool:
    return _normalise(name) in _RESOLVERS


def has_searcher(name: str) -> bool:
    """Return whether the resolver can ground free text through lookup."""
    spec = _RESOLVERS.get(_normalise(name))
    return spec is not None and spec.search is not None


async def search_field_candidates(
    resolver_name: str,
    query: str,
) -> list[tuple[str, str]]:
    """Look up canonical candidates through the resolver's registered adapter."""
    spec = _RESOLVERS.get(_normalise(resolver_name))
    if spec is None or spec.search is None:
        return []
    return await spec.search(query)


def _resolve_arxiv_identifier(fields: dict[str, Any]) -> FieldResolution:
    resolution = resolve_arxiv_identifier_from_fields(fields)
    return FieldResolution(
        resolved=resolution.resolved,
        value=resolution.value,
        label=resolution.label,
        reason=resolution.reason,
    )


async def _search_arxiv(query: str) -> list[tuple[str, str]]:
    from omni.research import arxiv

    try:
        results = await arxiv.search(query, max_results=5)
    except Exception:  # noqa: BLE001 - lookup remains offline/transient safe.
        return []
    return [
        (str(result.get("arxiv_id") or ""), str(result.get("title") or ""))
        for result in results
        if result.get("arxiv_id")
    ]


_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)


def _resolve_doi(fields: dict[str, Any]) -> FieldResolution:
    for value in _iter_field_values(fields):
        match = _DOI_RE.search(value)
        if match:
            return FieldResolution(
                resolved=True,
                value=match.group(0),
                label=value,
                reason="explicit DOI in input fields",
            )
    return FieldResolution(reason="no DOI found")


def extract_existing_local_path(text: str) -> Path | None:
    """Return an existing path from a plain or ``@``-attached user value.

    ``@/path with spaces/paper.pdf review this`` is intentionally parsed by
    existence, not by whitespace or a shell glob: the longest existing prefix
    after ``@`` is the attachment and the remainder stays user instruction.
    Quoted attachments and ``file://`` URIs are accepted as well.
    """

    value = str(text or "").strip()
    if not value:
        return None

    direct = _path_candidate(value)
    if direct is not None:
        return direct

    for marker in (match.start() for match in re.finditer(r"@", value)):
        tail = value[marker + 1 :].lstrip()
        if not tail:
            continue
        closer = closer_for_quote(tail[0]) if tail[0] in QUOTE_WRAPPERS else ""
        if closer:
            end = tail.find(closer, 1)
            if end < 0 and closer != tail[0]:
                end = tail.find(tail[0], 1)
            if end > 1:
                quoted = _path_candidate(tail[1:end])
                if quoted is not None:
                    return quoted
            continue

        # Prefer the whole tail, then progressively remove trailing words.
        # This preserves spaces that are genuinely part of the filename.
        cut_points = [
            len(tail),
            *reversed([match.start() for match in re.finditer(r"\s+", tail)]),
        ]
        for end in cut_points:
            candidate = _path_candidate(tail[:end].rstrip())
            if candidate is not None:
                return candidate
    return None


# Requires the separator, so a genuine one-letter scheme ("x:foo") stays a URL.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _path_candidate(value: str) -> Path | None:
    candidate_text = unwrap_matching_quotes(str(value or ""))
    # A drive letter is not a URL scheme. ``urlparse`` reads "C:\\work\\p.pdf" as
    # scheme "c", so the URL rejection below threw out every absolute Windows
    # path: an attached file resolved to nothing, and a path the user had just
    # named could not even be proved to exist.
    if not _WINDOWS_DRIVE.match(candidate_text):
        parsed = urlparse(candidate_text)
        if parsed.scheme == "file":
            candidate_text = unquote(parsed.path)
            # "file:///C:/work/p.pdf" leaves the drive behind a leading slash.
            if _WINDOWS_DRIVE.match(candidate_text[1:]):
                candidate_text = candidate_text[1:]
        elif parsed.scheme or parsed.netloc:
            return None
    try:
        candidate = resolve_existing_path(candidate_text)
        if candidate is not None:
            return candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def _resolve_file_path(fields: dict[str, Any]) -> FieldResolution:
    for value in _iter_field_values(fields):
        candidate = extract_existing_local_path(value)
        if candidate is not None:
            return FieldResolution(
                resolved=True,
                value=str(candidate),
                label=value,
                reason="existing local path in input fields",
            )
    return FieldResolution(reason="no existing local path found")


def _resolve_local_file_or_text(fields: dict[str, Any]) -> FieldResolution:
    """Resolve a paper-like input as an attachment first, otherwise as text."""

    path = _resolve_file_path(fields)
    if path.resolved:
        return path
    for value in _iter_field_values(fields):
        return FieldResolution(
            resolved=True,
            value=value,
            label="inline text or review target",
            reason="non-empty text in input fields",
        )
    return FieldResolution(reason="no existing local path or text found")


def _resolve_identifier(fields: dict[str, Any]) -> FieldResolution:
    """Resolve broad paper identifiers without binding to a concrete skill."""
    arxiv = _resolve_arxiv_identifier(fields)
    if arxiv.resolved:
        return arxiv
    doi = _resolve_doi(fields)
    if doi.resolved:
        return doi
    for value in _iter_field_values(fields):
        extracted = extract_arxiv_identifier(value)
        if extracted:
            return FieldResolution(
                resolved=True,
                value=extracted,
                label=value,
                reason="explicit arXiv identifier embedded in text",
            )
    return FieldResolution(reason="no supported paper identifier found")


def _iter_field_values(fields: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for raw in fields.values():
        if raw is None:
            continue
        if isinstance(raw, str):
            text = raw.strip()
        else:
            text = str(raw).strip()
        if text:
            values.append(text)
    return values


def _normalise(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


_ARXIV = FieldResolverSpec(_resolve_arxiv_identifier, _search_arxiv)
_RESOLVERS: dict[str, FieldResolverSpec] = {
    "arxiv_id": _ARXIV,
    "arxiv_identifier": _ARXIV,
    "doi": FieldResolverSpec(_resolve_doi),
    "file_path": FieldResolverSpec(_resolve_file_path),
    "path": FieldResolverSpec(_resolve_file_path),
    "local_file_or_text": FieldResolverSpec(_resolve_local_file_or_text),
    "paper_identifier": FieldResolverSpec(_resolve_identifier),
    "identifier": FieldResolverSpec(_resolve_identifier),
}


__all__ = [
    "FieldResolution",
    "FieldResolverSpec",
    "extract_existing_local_path",
    "has_resolver",
    "has_searcher",
    "resolve_field",
    "search_field_candidates",
]
