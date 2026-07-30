from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScientistProfile:
    scientist_id: str
    scientist_name: str
    aliases: list[str] = field(default_factory=list)
    openalex_author_id: str | None = None
    semantic_scholar_author_id: str | None = None
    google_scholar_author_id: str | None = None
    google_scholar_url: str | None = None
    orcid: str | None = None
    portrait_url: str | None = None
    portrait_source_url: str | None = None
    official_domains: list[str] = field(default_factory=list)
    institutions: list[str] = field(default_factory=list)
    education_history: list[dict[str, Any]] = field(default_factory=list)
    employment_history: list[dict[str, Any]] = field(default_factory=list)
    occupations: list[str] = field(default_factory=list)
    biography_sources: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    known_works: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IdentityCandidate:
    candidate_id: str
    display_name: str
    aliases: list[str] = field(default_factory=list)
    provider_ids: dict[str, str] = field(default_factory=dict)
    orcid: str | None = None
    google_scholar_author_id: str | None = None
    google_scholar_url: str | None = None
    portrait_url: str | None = None
    portrait_source_url: str | None = None
    institutions: list[str] = field(default_factory=list)
    education_history: list[dict[str, Any]] = field(default_factory=list)
    employment_history: list[dict[str, Any]] = field(default_factory=list)
    occupations: list[str] = field(default_factory=list)
    biography_sources: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    known_works: list[str] = field(default_factory=list)
    official_domains: list[str] = field(default_factory=list)
    paper_count: int = 0
    citation_count: int = 0
    score: float = 0.0
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceCandidate:
    candidate_id: str
    title: str
    source_type: str
    origin: str
    year: int | None = None
    url: str | None = None
    local_path: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    authors: list[str] = field(default_factory=list)
    author_position: str | None = None
    is_corresponding: bool = False
    identity_confirmed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceObject:
    schema_version: str
    source_id: str
    scientist_id: str
    title: str
    year: int | None
    source_type: str
    full_text: str
    authors: list[str]
    author_role: str
    provenance: dict[str, Any]
    identity_binding: dict[str, Any]
    quality: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
