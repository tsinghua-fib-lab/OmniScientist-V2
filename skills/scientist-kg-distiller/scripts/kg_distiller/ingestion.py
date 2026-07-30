from __future__ import annotations

import hashlib
import html
import io
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .http_client import HttpClient
from .io_utils import read_json, read_jsonl, write_json, write_jsonl
from .models import ScientistProfile, SourceCandidate, SourceObject
from .schemas import SCHEMA_VERSION, validate_source_object

SUPPORTED_LOCAL_SUFFIXES = {".pdf", ".txt", ".md", ".html", ".htm"}
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


class MaterialIngestor:
    def __init__(
        self,
        project_root: Path,
        *,
        http: HttpClient | None = None,
        max_sources: int = 200,
    ):
        self.root = project_root
        self.http = http or HttpClient()
        self.max_sources = max_sources
        self.attempts: list[dict[str, Any]] = []

    def run(self, scientist_id: str) -> Path:
        self.collect(scientist_id)
        return self.ingest(scientist_id)

    def collect(self, scientist_id: str) -> Path:
        corpus = self.root / "scientist-corpus" / scientist_id
        profile = self._load_profile(corpus, scientist_id)
        entries = self._local_and_manifest_entries(profile, corpus)
        if entries:
            self.attempts.append(
                {"channel": "local_manifest", "status": "ok", "candidate_count": len(entries)}
            )
        channels: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = []
        if profile.openalex_author_id:
            channels.append(
                (
                    "openalex",
                    lambda: self._discover_openalex(profile.openalex_author_id or ""),
                )
            )
        if profile.semantic_scholar_author_id:
            channels.append(
                (
                    "semantic_scholar",
                    lambda: self._discover_semantic_scholar(
                        profile.semantic_scholar_author_id or ""
                    ),
                )
            )
        channels.append(("arxiv", lambda: self._discover_arxiv(profile)))
        if profile.official_domains:
            channels.append(
                ("official_pages", lambda: self._discover_official_pages(profile))
            )
        for channel, operation in channels:
            try:
                found = operation()
                entries.extend(found)
                self.attempts.append(
                    {"channel": channel, "status": "ok", "candidate_count": len(found)}
                )
            except Exception as exc:  # noqa: BLE001 - isolate independent discovery channels
                self.attempts.append(
                    {
                        "channel": channel,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        write_jsonl(corpus / "collection_attempts.jsonl", self.attempts)
        candidates = self._deduplicate_and_rank(entries, corpus, profile)
        self._enrich_crossref(candidates)
        write_jsonl(corpus / "collection_attempts.jsonl", self.attempts)
        if not candidates:
            raise RuntimeError(
                f"No materials found for {scientist_id}; inspect "
                f"{corpus / 'collection_attempts.jsonl'}."
            )
        return write_jsonl(
            corpus / "source_candidates.jsonl",
            (candidate.to_dict() for candidate in candidates),
        )

    def ingest(self, scientist_id: str) -> Path:
        corpus = self.root / "scientist-corpus" / scientist_id
        profile = self._load_profile(corpus, scientist_id)
        candidate_path = corpus / "source_candidates.jsonl"
        if not candidate_path.exists():
            self.collect(scientist_id)
        candidates = [
            self._candidate_from_dict(row) for row in read_jsonl(candidate_path)
        ]
        accepted: list[tuple[SourceCandidate, dict[str, Any]]] = []
        rejected: list[dict[str, Any]] = []
        for candidate in candidates:
            binding = self.bind_identity(profile, candidate)
            record = {**candidate.to_dict(), "identity_binding": binding}
            if binding["accepted"]:
                accepted.append((candidate, binding))
            else:
                rejected.append(record)
        write_jsonl(corpus / "verified_candidates.jsonl", (
            {**candidate.to_dict(), "identity_binding": binding}
            for candidate, binding in accepted
        ))
        write_jsonl(corpus / "rejected_candidates.jsonl", rejected)
        if not accepted:
            raise RuntimeError(
                f"All {len(candidates)} materials failed identity binding; inspect "
                f"{corpus / 'rejected_candidates.jsonl'}."
            )

        partial_sources_path = corpus / "source_objects.partial.jsonl"
        partial_failures_path = corpus / "fetch_failures.partial.jsonl"
        source_rows = (
            read_jsonl(partial_sources_path) if partial_sources_path.exists() else []
        )
        failure_by_candidate = {
            str(row["candidate_id"]): row
            for row in (
                read_jsonl(partial_failures_path)
                if partial_failures_path.exists()
                else []
            )
        }
        completed_candidate_ids = {
            str(row.get("provenance", {}).get("candidate_id"))
            for row in source_rows
            if row.get("provenance", {}).get("candidate_id")
        }
        for source_index, (candidate, binding) in enumerate(accepted, 1):
            if candidate.candidate_id in completed_candidate_ids:
                continue
            try:
                raw_text = self.fetch_text(candidate)
                if (
                    candidate.source_type == "paper"
                    and not candidate.local_path
                    and not _content_matches_title(candidate.title, raw_text)
                ):
                    raise ValueError(
                        "fetched content does not match the candidate title"
                    )
                full_text = self.normalize(candidate, raw_text)
                if len(full_text) < 200:
                    raise ValueError("extracted text is shorter than 200 characters")
                source = SourceObject(
                    schema_version=SCHEMA_VERSION,
                    source_id=f"src_{source_index:04d}",
                    scientist_id=scientist_id,
                    title=candidate.title,
                    year=candidate.year,
                    source_type=candidate.source_type,
                    full_text=full_text,
                    authors=candidate.authors,
                    author_role=self._author_role(candidate, raw_text, profile),
                    provenance={
                        "origin": candidate.origin,
                        "url": candidate.url,
                        "local_path": candidate.local_path,
                        "doi": candidate.doi,
                        "arxiv_id": candidate.arxiv_id,
                        "candidate_id": candidate.candidate_id,
                        "provider_ids": candidate.metadata.get("provider_ids", {}),
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "sha256": hashlib.sha256(
                            full_text.encode("utf-8")
                        ).hexdigest(),
                    },
                    identity_binding=binding,
                    quality={
                        "status": "usable",
                        "character_count": len(full_text),
                        "has_section_markers": bool(
                            re.search(r"(?m)^#{1,3}\s", full_text)
                        ),
                    },
                )
                value = source.to_dict()
                validate_source_object(value)
                source_rows.append(value)
                write_jsonl(partial_sources_path, source_rows)
                failure_by_candidate.pop(candidate.candidate_id, None)
                write_jsonl(partial_failures_path, failure_by_candidate.values())
            except Exception as exc:  # noqa: BLE001 - preserve per-source ingestion failure
                failure_by_candidate[candidate.candidate_id] = {
                    "candidate_id": candidate.candidate_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "status": "metadata_only",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                write_jsonl(partial_failures_path, failure_by_candidate.values())
        failures = list(failure_by_candidate.values())
        if not source_rows:
            raise RuntimeError(
                f"No material produced usable text; inspect "
                f"{partial_failures_path}."
            )
        output = write_jsonl(
            corpus / "source_objects.jsonl",
            source_rows,
        )
        write_jsonl(corpus / "fetch_failures.jsonl", failures)
        partial_sources_path.unlink(missing_ok=True)
        partial_failures_path.unlink(missing_ok=True)
        write_json(
            corpus / "material_audit.json",
            {
                "schema_version": SCHEMA_VERSION,
                "scientist_id": scientist_id,
                "discovered": len(candidates),
                "identity_accepted": len(accepted),
                "identity_rejected": len(rejected),
                "usable_source_objects": len(source_rows),
                "metadata_only": len(failures),
                "source_types": _counts(row["source_type"] for row in source_rows),
                "providers": _counts(
                    row["provenance"]["origin"] for row in source_rows
                ),
                "years": _counts(
                    str(row["year"]) for row in source_rows if row.get("year")
                ),
                "total_characters": sum(
                    len(row["full_text"]) for row in source_rows
                ),
            },
        )
        return output

    def _load_profile(self, corpus: Path, scientist_id: str) -> ScientistProfile:
        path = corpus / "profile.json"
        if not path.exists():
            raise FileNotFoundError(
                f"Missing identity profile: {path}. Run the identity step or provide "
                "a profile with scientist_name."
            )
        value = read_json(path)
        return ScientistProfile(
            scientist_id=scientist_id,
            scientist_name=str(value["scientist_name"]),
            aliases=[str(item) for item in value.get("aliases", [])],
            openalex_author_id=value.get("openalex_author_id"),
            semantic_scholar_author_id=value.get("semantic_scholar_author_id"),
            google_scholar_author_id=value.get("google_scholar_author_id"),
            google_scholar_url=value.get("google_scholar_url"),
            orcid=value.get("orcid"),
            portrait_url=value.get("portrait_url"),
            portrait_source_url=value.get("portrait_source_url"),
            official_domains=[str(item) for item in value.get("official_domains", [])],
            institutions=[str(item) for item in value.get("institutions", [])],
            fields=[str(item) for item in value.get("fields", [])],
            known_works=[str(item) for item in value.get("known_works", [])],
            sources=list(value.get("sources", [])),
        )

    def _local_and_manifest_entries(
        self, profile: ScientistProfile, corpus: Path
    ) -> list[dict[str, Any]]:
        entries = list(profile.sources)
        manifest = corpus / "sources.json"
        if manifest.exists():
            value = read_json(manifest)
            entries.extend(value if isinstance(value, list) else value["sources"])
        raw_dir = corpus / "raw"
        if raw_dir.exists():
            configured = {
                str(_resolve_local_path(str(item["path"]), corpus))
                for item in entries
                if item.get("path")
            }
            for path in sorted(raw_dir.rglob("*")):
                if (
                    path.is_file()
                    and path.suffix.lower() in SUPPORTED_LOCAL_SUFFIXES
                    and str(path.resolve()) not in configured
                ):
                    entries.append({"path": str(path), "title": path.stem})
        return entries

    def _discover_openalex(self, author_id: str) -> list[dict[str, Any]]:
        identifier = author_id.rsplit("/", 1)[-1]
        entries: list[dict[str, Any]] = []
        cursor = "*"
        target = self._discovery_target()
        while cursor and len(entries) < target:
            page_size = min(200, target - len(entries))
            payload = self.http.get_json(
                "https://api.openalex.org/works",
                {
                    "filter": f"authorships.author.id:{identifier}",
                    "per-page": page_size,
                    "sort": "cited_by_count:desc",
                    "cursor": cursor,
                },
            )
            works = payload.get("results", [])
            if not works:
                break
            for work in works:
                entry = self._openalex_entry(work, identifier)
                if entry:
                    entries.append(entry)
            cursor = str((payload.get("meta") or {}).get("next_cursor") or "")
            if len(works) < page_size:
                break
        return entries

    def _openalex_entry(
        self, work: dict[str, Any], identifier: str
    ) -> dict[str, Any] | None:
            authorships = work.get("authorships") or []
            target = next(
                (
                    item
                    for item in authorships
                    if str((item.get("author") or {}).get("id", "")).endswith(
                        identifier
                    )
                ),
                {},
            )
            primary = work.get("primary_location") or {}
            best = work.get("best_oa_location") or {}
            locations = work.get("locations") or []
            direct_pdf = next(
                (
                    location.get("pdf_url")
                    for location in locations
                    if location.get("pdf_url")
                ),
                None,
            )
            arxiv_landing = next(
                (
                    location.get("landing_page_url")
                    for location in locations
                    if "arxiv.org/abs/" in str(location.get("landing_page_url") or "")
                ),
                None,
            )
            arxiv_pdf = (
                str(arxiv_landing).replace("/abs/", "/pdf/")
                if arxiv_landing
                else None
            )
            alternate_urls = _unique_strings(
                [
                    str(value)
                    for value in [
                        direct_pdf,
                        arxiv_pdf,
                        best.get("pdf_url"),
                        best.get("landing_page_url"),
                        primary.get("pdf_url"),
                        primary.get("landing_page_url"),
                    ]
                    if value
                ]
            )
            url = alternate_urls[0] if alternate_urls else None
            if not url:
                return None
            ids = work.get("ids") or {}
            doi = str(ids.get("doi") or "").removeprefix("https://doi.org/") or None
            arxiv_id = _arxiv_id(
                str(ids.get("arxiv") or work.get("doi") or url)
            )
            return {
                    "title": work.get("title") or "Untitled",
                    "year": work.get("publication_year"),
                    "source_type": "paper",
                    "url": url,
                    "origin": "openalex",
                    "authors": [
                        (item.get("author") or {}).get("display_name", "")
                        for item in authorships
                    ],
                    "author_position": target.get("author_position"),
                    "is_corresponding": target.get("is_corresponding", False),
                    "identity_confirmed": True,
                    "doi": doi,
                    "arxiv_id": arxiv_id,
                    "metadata": {
                        "citation_count": work.get("cited_by_count") or 0,
                        "fields": _unique_strings(
                            [
                                str(
                                    (work.get("primary_topic") or {}).get(
                                        "display_name"
                                    )
                                    or ""
                                ),
                                *[
                                    str((topic.get("topic") or {}).get("display_name") or "")
                                    for topic in work.get("topics") or []
                                ],
                            ]
                        ),
                        "provider_ids": {"openalex": work.get("id")},
                        "alternate_urls": alternate_urls,
                    },
                }

    def _discover_semantic_scholar(self, author_id: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        offset = 0
        target = self._discovery_target()
        while len(entries) < target:
            page_size = min(1000, target - len(entries))
            payload = self.http.get_json(
                f"https://api.semanticscholar.org/graph/v1/author/"
                f"{urllib.parse.quote(author_id)}/papers",
                {
                    "limit": page_size,
                    "offset": offset,
                    "fields": (
                        "title,year,authors,openAccessPdf,url,externalIds,"
                        "publicationTypes,citationCount,fieldsOfStudy"
                    ),
                },
            )
            works = payload.get("data", [])
            if not works:
                break
            for work in works:
                entry = self._semantic_scholar_entry(work, author_id)
                if entry:
                    entries.append(entry)
            offset += len(works)
            if len(works) < page_size:
                break
        return entries

    def _semantic_scholar_entry(
        self, work: dict[str, Any], author_id: str
    ) -> dict[str, Any] | None:
            open_access = work.get("openAccessPdf") or {}
            external = work.get("externalIds") or {}
            arxiv_id = external.get("ArXiv")
            alternate_urls = _unique_strings(
                [
                    str(value)
                    for value in [
                        open_access.get("url"),
                        (f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None),
                        work.get("url"),
                    ]
                    if value
                ]
            )
            url = alternate_urls[0] if alternate_urls else None
            if not url:
                return None
            raw_authors = work.get("authors") or []
            authors = [
                str(item.get("name", ""))
                for item in raw_authors
                if item.get("name")
            ]
            target_index = next(
                (
                    index
                    for index, item in enumerate(raw_authors)
                    if str(item.get("authorId", "")) == str(author_id)
                ),
                None,
            )
            position = (
                "first"
                if target_index == 0
                else "last"
                if target_index is not None and target_index == len(authors) - 1
                else "middle"
            )
            return {
                    "title": work.get("title") or "Untitled",
                    "year": work.get("year"),
                    "source_type": "paper",
                    "url": url,
                    "origin": "semantic_scholar",
                    "authors": authors,
                    "author_position": position,
                    "identity_confirmed": True,
                    "doi": external.get("DOI"),
                    "arxiv_id": arxiv_id,
                    "metadata": {
                        "citation_count": work.get("citationCount") or 0,
                        "fields": [
                            str(value) for value in work.get("fieldsOfStudy") or []
                        ],
                        "provider_ids": {
                            "semantic_scholar": work.get("paperId")
                        },
                        "alternate_urls": alternate_urls,
                    },
                }

    def _discover_arxiv(self, profile: ScientistProfile) -> list[dict[str, Any]]:
        names = [profile.scientist_name, *profile.aliases][:3]
        entries: list[dict[str, Any]] = []
        for name in names:
            entries.extend(
                self._arxiv_query(
                    f'au:"{name}"', profile, sort_by="submittedDate"
                )
            )
        # OpenAlex and Semantic Scholar already provide broad discovery. Bound
        # title-specific arXiv lookups so prolific scientists do not turn this
        # supplementary channel into hundreds of serial network requests.
        known_work_query_limit = min(12, max(3, self.max_sources // 20))
        for title in profile.known_works[:known_work_query_limit]:
            try:
                text = self.http.get_text(
                    "https://export.arxiv.org/api/query",
                    {
                        "search_query": f'ti:"{title}"',
                        "start": 0,
                        "max_results": 3,
                    },
                )
                entries.extend(_arxiv_feed_entries(text, profile))
            except Exception as exc:  # noqa: BLE001 - optional arXiv supplement must degrade
                self.attempts.append(
                    {
                        "channel": "arxiv_known_work",
                        "title": title,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        return entries

    def _arxiv_query(
        self, query: str, profile: ScientistProfile, *, sort_by: str | None = None
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        page_size = 100
        target = self._discovery_target()
        for start in range(0, target, page_size):
            params: dict[str, Any] = {
                "search_query": query,
                "start": start,
                "max_results": min(page_size, target - start),
            }
            if sort_by:
                params["sortBy"] = sort_by
                params["sortOrder"] = "descending"
            text = self.http.get_text("https://export.arxiv.org/api/query", params)
            page = _arxiv_feed_entries(text, profile)
            entries.extend(page)
            if len(page) < params["max_results"]:
                break
        return entries

    def _discover_official_pages(
        self, profile: ScientistProfile
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        for domain in profile.official_domains:
            base = domain if re.match(r"^https?://", domain) else f"https://{domain}"
            parsed = urllib.parse.urlparse(base)
            root = f"{parsed.scheme}://{parsed.netloc}/"
            pages = [
                base,
                urllib.parse.urljoin(root, "publications"),
                urllib.parse.urljoin(root, "publications.html"),
                urllib.parse.urljoin(root, "papers"),
            ]
            page_index = 0
            while page_index < len(pages) and page_index < 12:
                page = pages[page_index]
                page_index += 1
                if page in seen_pages:
                    continue
                seen_pages.add(page)
                try:
                    page_html = self.http.get_text(page)
                except Exception as exc:  # noqa: BLE001 - continue crawling other pages
                    self.attempts.append(
                        {
                            "channel": "official_page_url",
                            "url": page,
                            "status": "failed",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                    continue
                for href, label in _links(page_html, page):
                    source_type = _linked_source_type(href, label)
                    if not source_type:
                        if _looks_like_collection_page(href, label, parsed.netloc):
                            pages.append(href)
                        continue
                    entries.append(
                        {
                            "title": label or href.rsplit("/", 1)[-1],
                            "source_type": source_type,
                            "url": href,
                            "origin": "official_page",
                            "identity_confirmed": True,
                            "arxiv_id": _arxiv_id(href),
                        }
                    )
        return entries

    def _deduplicate_and_rank(
        self,
        entries: list[dict[str, Any]],
        corpus: Path,
        profile: ScientistProfile,
    ) -> list[SourceCandidate]:
        selected: list[SourceCandidate] = []
        key_to_candidate: dict[str, SourceCandidate] = {}
        for entry in entries:
            candidate = self._candidate_from_entry(entry, corpus)
            keys = _dedup_keys(candidate)
            current = next(
                (key_to_candidate[key] for key in keys if key in key_to_candidate),
                None,
            )
            if current is None:
                selected.append(candidate)
                current = candidate
            elif _candidate_rank(candidate) > _candidate_rank(current):
                previous = current
                _merge_candidate(candidate, previous)
                selected[selected.index(current)] = candidate
                for key, value in list(key_to_candidate.items()):
                    if value is previous:
                        key_to_candidate[key] = candidate
                current = candidate
            else:
                _merge_candidate(current, candidate)
            for key in keys:
                key_to_candidate[key] = current
        return _balanced_selection(selected, profile, self.max_sources)

    def _enrich_crossref(self, candidates: list[SourceCandidate]) -> None:
        attempted = 0
        enriched = 0
        failures: list[str] = []
        needs_enrichment = [
            candidate
            for candidate in candidates
            if candidate.doi
            and (
                not candidate.authors
                or not candidate.metadata.get("alternate_urls")
            )
        ]
        enrichment_limit = min(40, max(10, self.max_sources // 5))
        for candidate in needs_enrichment[:enrichment_limit]:
            attempted += 1
            try:
                payload = self.http.get_json(
                    "https://api.crossref.org/works/"
                    + urllib.parse.quote(candidate.doi, safe="")
                )
                message = payload.get("message") or {}
                raw_authors = message.get("author") or []
                authors = [
                    " ".join(
                        part
                        for part in (
                            str(author.get("given") or "").strip(),
                            str(author.get("family") or "").strip(),
                        )
                        if part
                    )
                    for author in raw_authors
                ]
                if authors and not candidate.authors:
                    candidate.authors = authors
                candidate.metadata.setdefault("provider_ids", {})["crossref"] = (
                    message.get("DOI") or candidate.doi
                )
                candidate.metadata["alternate_urls"] = _unique_strings(
                    [
                        *[
                            str(value)
                            for value in candidate.metadata.get(
                                "alternate_urls", []
                            )
                        ],
                        *[
                            str(link.get("URL"))
                            for link in message.get("link") or []
                            if link.get("URL")
                        ],
                    ]
                )
                enriched += 1
            except Exception as exc:  # noqa: BLE001 - isolate per-DOI enrichment failure
                failures.append(f"{candidate.doi}: {type(exc).__name__}: {exc}")
        if attempted:
            self.attempts.append(
                {
                    "channel": "crossref",
                    "status": "ok" if enriched else "failed",
                    "attempted": attempted,
                    "enriched": enriched,
                    "failure_count": len(failures),
                    "sample_errors": failures[:3],
                }
            )

    def _candidate_from_entry(
        self, entry: dict[str, Any], corpus: Path
    ) -> SourceCandidate:
        raw_path = entry.get("path")
        local_path = (
            str(_resolve_local_path(str(raw_path), corpus)) if raw_path else None
        )
        authors = [
            str(author.get("name", "")) if isinstance(author, dict) else str(author)
            for author in entry.get("authors", [])
        ]
        source_type = entry.get("source_type") or _infer_type(
            entry.get("url") or local_path or ""
        )
        title = str(entry.get("title") or Path(local_path or "untitled").stem)
        key = "|".join(
            [
                str(entry.get("doi") or ""),
                str(entry.get("arxiv_id") or ""),
                str(entry.get("url") or local_path or ""),
                title,
            ]
        )
        return SourceCandidate(
            candidate_id=f"cand_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}",
            title=title,
            source_type=source_type,
            origin=str(
                entry.get("origin") or ("local" if local_path else "manifest")
            ),
            year=_year(entry.get("year")),
            url=entry.get("url"),
            local_path=local_path,
            doi=entry.get("doi"),
            arxiv_id=entry.get("arxiv_id"),
            authors=[name for name in authors if name],
            author_position=entry.get("author_position"),
            is_corresponding=bool(entry.get("is_corresponding")),
            identity_confirmed=bool(entry.get("identity_confirmed")),
            metadata=dict(entry.get("metadata", {})),
        )

    def _candidate_from_dict(self, value: dict[str, Any]) -> SourceCandidate:
        allowed = {
            "candidate_id",
            "title",
            "source_type",
            "origin",
            "year",
            "url",
            "local_path",
            "doi",
            "arxiv_id",
            "authors",
            "author_position",
            "is_corresponding",
            "identity_confirmed",
            "metadata",
        }
        return SourceCandidate(**{key: value[key] for key in allowed if key in value})

    def bind_identity(
        self, profile: ScientistProfile, candidate: SourceCandidate
    ) -> dict[str, Any]:
        if candidate.identity_confirmed:
            if candidate.origin in {"official_page", "local", "manifest"}:
                return {
                    "accepted": True,
                    "score": 1.0,
                    "evidence": [
                        "material explicitly confirmed or linked from official domain"
                    ],
                }
            context_score, context_evidence = _profile_context_score(
                profile, candidate
            )
            if profile.known_works or profile.fields:
                if context_score <= 0:
                    return {
                        "accepted": False,
                        "score": 0.35,
                        "evidence": [
                            f"confirmed provider author ID: {candidate.origin}"
                        ],
                        "reason": (
                            "author ID is present but paper context conflicts with or "
                            "does not support the confirmed authority profile"
                        ),
                    }
                return {
                    "accepted": True,
                    "score": min(1.0, 0.8 + context_score * 0.05),
                    "evidence": [
                        f"confirmed provider author ID: {candidate.origin}",
                        *context_evidence,
                    ],
                }
            return {
                "accepted": True,
                "score": 1.0,
                "evidence": [
                    f"candidate comes from confirmed identity channel: {candidate.origin}"
                ],
            }
        aliases = {
            _normalize(profile.scientist_name),
            *(_normalize(alias) for alias in profile.aliases),
        }
        matched = sorted(
            name for name in candidate.authors if _normalize(name) in aliases
        )
        return {
            "accepted": bool(matched),
            "score": 0.9 if matched else 0.0,
            "evidence": [
                f"exact normalized author match: {name}" for name in matched
            ],
            "reason": None if matched else "no confirmed alias in author metadata",
        }

    def fetch_text(self, candidate: SourceCandidate) -> str:
        if candidate.local_path:
            path = Path(candidate.local_path)
            if not path.exists():
                raise FileNotFoundError(path)
            return self._extract(path.read_bytes(), path.suffix.lower(), None)
        urls = _unique_strings(
            [
                str(value)
                for value in [
                    candidate.url,
                    *(candidate.metadata.get("alternate_urls") or []),
                ]
                if value
            ]
        )
        if not urls:
            raise ValueError("candidate has neither local_path nor url")
        errors: list[str] = []
        for url in urls:
            try:
                if _is_youtube(url):
                    text = self._fetch_youtube_transcript(url)
                elif _is_github_repo(url):
                    text = self._fetch_github_readme(url)
                else:
                    data, headers = self.http.get_bytes(url)
                    content_type = headers.get_content_type()
                    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
                    text = self._extract(data, suffix, content_type)
                if (
                    candidate.source_type == "paper"
                    and not _content_matches_title(candidate.title, text)
                ):
                    raise ValueError("fetched content does not match the candidate title")
                candidate.url = url
                return text
            except Exception as exc:  # noqa: BLE001 - try the next full-text URL
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
        raise ValueError("all full-text URLs failed: " + " | ".join(errors[:3]))

    def _discovery_target(self) -> int:
        return max(200, self.max_sources * 3)

    def _fetch_youtube_transcript(self, url: str) -> str:
        watch_html = self.http.get_text(
            url, headers={"User-Agent": "Mozilla/5.0 scientist-kg-distiller/1.0"}
        )
        match = re.search(
            r'"captionTracks":\s*\[\s*\{.*?"baseUrl":"(.*?)"', watch_html
        )
        if not match:
            raise ValueError("YouTube video has no accessible caption track")
        caption_url = json.loads(f'"{match.group(1)}"').replace(r"\u0026", "&")
        caption_data, _ = self.http.get_bytes(caption_url)
        try:
            root = ET.fromstring(caption_data)
        except ET.ParseError as exc:
            raise ValueError("YouTube caption response is not valid XML") from exc
        lines = [
            html.unescape("".join(node.itertext())).strip()
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1] in {"text", "p"}
        ]
        transcript = "\n".join(line for line in lines if line)
        if not transcript:
            raise ValueError("YouTube caption track is empty")
        return transcript

    def _fetch_github_readme(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise ValueError("GitHub URL is not a repository")
        payload = self.http.get_json(
            f"https://api.github.com/repos/{parts[0]}/{parts[1]}/readme",
            headers={"Accept": "application/vnd.github+json"},
        )
        download_url = payload.get("download_url")
        if not download_url:
            raise ValueError("GitHub repository has no downloadable README")
        return self.http.get_text(str(download_url))

    def _extract(
        self, data: bytes, suffix: str, content_type: str | None
    ) -> str:
        if (
            suffix == ".pdf"
            or content_type == "application/pdf"
            or data.startswith(b"%PDF")
        ):
            reader = PdfReader(io.BytesIO(data))
            return "\n\n".join(
                f"## Page {index}\n{page.extract_text() or ''}"
                for index, page in enumerate(reader.pages, 1)
            )
        text = data.decode("utf-8-sig", errors="replace")
        if suffix in {".html", ".htm"} or content_type == "text/html":
            parser = _VisibleText()
            parser.feed(text)
            return parser.text()
        return text

    def normalize(self, candidate: SourceCandidate, raw_text: str) -> str:
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        header = [
            f"# {candidate.title}",
            f"Year: {candidate.year or 'unknown'}",
            f"Source-Type: {candidate.source_type}",
            f"Authors: {', '.join(candidate.authors) or 'unknown'}",
            "",
            "## Full Text",
        ]
        return "\n".join(header) + "\n" + text

    def _author_role(
        self,
        candidate: SourceCandidate,
        raw_text: str,
        profile: ScientistProfile,
    ) -> str:
        if candidate.source_type in {"talk", "interview"}:
            return "speaker"
        if candidate.is_corresponding or _corresponding_note(raw_text, profile):
            return "corresponding"
        if candidate.author_position == "first":
            return "first"
        if candidate.author_position == "last":
            return "last"
        if len(candidate.authors) <= 4 and candidate.author_position in {
            "first",
            "middle",
            "last",
        }:
            return "small_team_core"
        return candidate.author_position or "author"


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag in {
            "p",
            "div",
            "section",
            "article",
            "h1",
            "h2",
            "h3",
            "li",
            "br",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(html.unescape(data))

    def text(self) -> str:
        return "".join(self.parts)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", value.casefold())


def _year(value: Any) -> int | None:
    if value in (None, ""):
        return None
    year = int(value)
    if not 1800 <= year <= 2200:
        raise ValueError(f"invalid year: {year}")
    return year


def _resolve_local_path(value: str, corpus: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = corpus / path
    return path.resolve()


def _infer_type(value: str) -> str:
    lowered = value.casefold()
    if _is_youtube(value):
        return "talk"
    if "github.com" in lowered:
        return "code"
    return "paper"


def _is_youtube(value: str) -> bool:
    host = urllib.parse.urlparse(value).netloc.casefold().split(":")[0]
    return host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }


def _is_github_repo(value: str) -> bool:
    host = urllib.parse.urlparse(value).netloc.casefold().split(":")[0]
    return host in {"github.com", "www.github.com"}


def _arxiv_id(value: str) -> str | None:
    match = re.search(
        r"(?:arxiv[.:/ ]+|abs/|pdf/)([a-z-]+/[0-9]{7}|[0-9]{4}\.[0-9]{4,5})",
        value,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _dedup_keys(candidate: SourceCandidate) -> list[str]:
    keys: list[str] = []
    if candidate.doi:
        keys.append(
            f"doi:{candidate.doi.casefold().removeprefix('https://doi.org/')}"
        )
    if candidate.arxiv_id:
        keys.append(f"arxiv:{candidate.arxiv_id.casefold()}")
    provider_ids = candidate.metadata.get("provider_ids") or {}
    keys.extend(
        f"{provider}:{identifier}"
        for provider, identifier in sorted(provider_ids.items())
        if identifier
    )
    title = re.sub(r"[^a-z0-9]", "", candidate.title.casefold())
    if title:
        keys.append(f"title:{title}:{candidate.year or ''}")
        if len(title) >= 20:
            keys.append(f"title_exact:{title}")
    if not keys:
        keys.append(f"candidate:{candidate.candidate_id}")
    return keys


def _candidate_rank(candidate: SourceCandidate) -> tuple[int, int, int, int]:
    fulltext = int(
        bool(
            candidate.local_path
            or candidate.url
            and (
                candidate.url.casefold().endswith(".pdf")
                or "arxiv.org/pdf/" in candidate.url.casefold()
                or _is_youtube(candidate.url)
                or _is_github_repo(candidate.url)
            )
        )
    )
    authority = {
        "local": 5,
        "official_page": 5,
        "arxiv": 4,
        "openalex": 3,
        "semantic_scholar": 3,
        "manifest": 2,
    }.get(candidate.origin, 1)
    citations = int(candidate.metadata.get("citation_count") or 0)
    return fulltext, authority, citations, candidate.year or 0


def _merge_candidate(
    primary: SourceCandidate, secondary: SourceCandidate
) -> None:
    primary.metadata["provider_ids"] = {
        **secondary.metadata.get("provider_ids", {}),
        **primary.metadata.get("provider_ids", {}),
    }
    primary.metadata["citation_count"] = max(
        int(primary.metadata.get("citation_count") or 0),
        int(secondary.metadata.get("citation_count") or 0),
    )
    primary.metadata["fields"] = _unique_strings(
        [
            *[str(value) for value in primary.metadata.get("fields", [])],
            *[str(value) for value in secondary.metadata.get("fields", [])],
        ]
    )
    primary.doi = primary.doi or secondary.doi
    primary.arxiv_id = primary.arxiv_id or secondary.arxiv_id
    primary.is_corresponding = (
        primary.is_corresponding or secondary.is_corresponding
    )
    if not primary.authors and secondary.authors:
        primary.authors = secondary.authors


def _balanced_selection(
    candidates: list[SourceCandidate],
    profile: ScientistProfile,
    limit: int,
) -> list[SourceCandidate]:
    result: list[SourceCandidate] = []
    seen: set[str] = set()

    def add(candidate: SourceCandidate) -> None:
        key = candidate.candidate_id
        if key not in seen and len(result) < limit:
            seen.add(key)
            result.append(candidate)

    known_titles = {
        re.sub(r"[^a-z0-9]", "", title.casefold())
        for title in profile.known_works
    }
    representative = [
        candidate
        for candidate in candidates
        if re.sub(r"[^a-z0-9]", "", candidate.title.casefold()) in known_titles
    ]
    representative.sort(
        key=lambda item: (
            int(item.metadata.get("citation_count") or 0),
            _candidate_rank(item),
        ),
        reverse=True,
    )
    representative_quota = min(
        len(representative),
        max(1, limit // 2),
    )
    for candidate in representative[:representative_quota]:
        add(candidate)

    citation_quota = max(1, limit * 3 // 10)
    by_citations = sorted(
        candidates,
        key=lambda item: (
            int(item.metadata.get("citation_count") or 0),
            _candidate_rank(item),
        ),
        reverse=True,
    )
    added = 0
    for candidate in by_citations:
        if candidate.candidate_id not in seen:
            add(candidate)
            added += 1
        if added >= citation_quota or len(result) >= limit:
            break

    recent_quota = max(1, limit * 3 // 10)
    by_recency = sorted(
        candidates,
        key=lambda item: (item.year or 0, _candidate_rank(item)),
        reverse=True,
    )
    added = 0
    for candidate in by_recency:
        if candidate.candidate_id not in seen:
            add(candidate)
            added += 1
        if added >= recent_quota or len(result) >= limit:
            break

    origins = sorted({candidate.origin for candidate in candidates})
    for origin in origins:
        candidate = next(
            (
                item
                for item in sorted(
                    candidates, key=_candidate_rank, reverse=True
                )
                if item.origin == origin and item.candidate_id not in seen
            ),
            None,
        )
        if candidate:
            add(candidate)

    for candidate in sorted(candidates, key=_candidate_rank, reverse=True):
        add(candidate)
    return result


def _links(page_html: str, page_url: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for match in re.finditer(
        r"(?is)<a\b[^>]*\bhref=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
        page_html,
    ):
        href = urllib.parse.urljoin(page_url, html.unescape(match.group(1)))
        label = re.sub(r"(?is)<[^>]+>", " ", match.group(2))
        label = " ".join(html.unescape(label).split())
        if href.startswith(("http://", "https://")):
            result.append((href, label))
    return result


def _linked_source_type(href: str, label: str) -> str | None:
    value = f"{href} {label}".casefold()
    if "github.com" in value:
        return "code"
    if "youtube.com" in value or "youtu.be" in value:
        return "interview" if "interview" in value else "talk"
    if (
        href.casefold().endswith(".pdf")
        or "arxiv.org/" in value
        or "doi.org/" in value
        or any(word in value for word in ("paper", "publication", "proceedings"))
    ):
        return "paper"
    return None


def _looks_like_collection_page(href: str, label: str, official_host: str) -> bool:
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc != official_host or Path(parsed.path).suffix.lower() in {
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
    }:
        return False
    value = f"{parsed.path} {label}".casefold()
    return any(
        token in value
        for token in (
            "publication",
            "paper",
            "research",
            "project",
            "talk",
            "video",
            "code",
            "software",
            "cv",
        )
    )


def _corresponding_note(text: str, profile: ScientistProfile) -> bool:
    aliases = [profile.scientist_name, *profile.aliases]
    patterns = [
        r"corresponding author[s]?\s*[:\-]?\s*([^\n]{0,160})",
        r"correspondence\s+(?:to|should be addressed to)\s*[:\-]?\s*([^\n]{0,160})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            context = match.group(0)
            if any(
                _normalize(alias) and _normalize(alias) in _normalize(context)
                for alias in aliases
            ):
                return True
    return False


def _counts(values: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[str(value)] = result.get(str(value), 0) + 1
    return result


def _arxiv_feed_entries(
    text: str, profile: ScientistProfile
) -> list[dict[str, Any]]:
    root = ET.fromstring(text)
    aliases = {
        _normalize(profile.scientist_name),
        *(_normalize(alias) for alias in profile.aliases),
    }
    entries: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        title = " ".join(
            (entry.findtext("atom:title", "", ARXIV_NS) or "").split()
        )
        identifier_url = entry.findtext("atom:id", "", ARXIV_NS)
        arxiv_id = identifier_url.rsplit("/", 1)[-1].split("v", 1)[0]
        authors = [
            node.findtext("atom:name", "", ARXIV_NS)
            for node in entry.findall("atom:author", ARXIV_NS)
        ]
        if not any(_normalize(author) in aliases for author in authors):
            continue
        published = entry.findtext("atom:published", "", ARXIV_NS)
        categories = [
            str(node.attrib.get("term") or "")
            for node in entry.findall("atom:category", ARXIV_NS)
        ]
        entries.append(
            {
                "title": title or f"arXiv {arxiv_id}",
                "year": int(published[:4]) if published[:4].isdigit() else None,
                "source_type": "paper",
                "url": f"https://arxiv.org/pdf/{arxiv_id}",
                "origin": "arxiv",
                "authors": authors,
                "author_position": (
                    "first"
                    if authors and _normalize(authors[0]) in aliases
                    else "middle"
                ),
                "identity_confirmed": True,
                "arxiv_id": arxiv_id,
                "metadata": {
                    "fields": categories,
                    "provider_ids": {"arxiv": arxiv_id},
                },
            }
        )
    return entries


def _content_matches_title(title: str, text: str) -> bool:
    stop = {
        "about",
        "after",
        "based",
        "from",
        "into",
        "through",
        "towards",
        "using",
        "with",
    }
    title_tokens = {
        token
        for token in re.findall(r"[a-z0-9]{3,}", title.casefold())
        if token not in stop
    }
    if not title_tokens:
        return True
    header = text[:20000].casefold()
    hits = sum(1 for token in title_tokens if token in header)
    required = max(2, (len(title_tokens) * 3 + 4) // 5)
    return hits >= min(required, len(title_tokens))


def _profile_context_score(
    profile: ScientistProfile, candidate: SourceCandidate
) -> tuple[int, list[str]]:
    normalized_title = re.sub(r"[^a-z0-9]", "", candidate.title.casefold())
    for known_work in profile.known_works:
        if normalized_title and normalized_title == re.sub(
            r"[^a-z0-9]", "", known_work.casefold()
        ):
            return 4, [f"exact known-work title: {known_work}"]
    context = " ".join(
        [
            candidate.title,
            *[str(value) for value in candidate.metadata.get("fields", [])],
        ]
    ).casefold()
    phrase_hits = [
        field
        for field in profile.fields
        if len(field) >= 5 and field.casefold() in context
    ]
    stop = {
        "about",
        "after",
        "based",
        "deep",
        "from",
        "into",
        "learning",
        "method",
        "model",
        "network",
        "study",
        "towards",
        "using",
        "with",
    }
    vocabulary = {
        token
        for value in [*profile.fields, *profile.known_works]
        for token in re.findall(r"[a-z]{4,}", value.casefold())
        if token not in stop
    }
    token_hits = sorted(token for token in vocabulary if token in context)
    score = len(phrase_hits) * 2 + min(len(token_hits), 3)
    evidence = [f"field phrase match: {value}" for value in phrase_hits]
    if token_hits:
        evidence.append("domain token matches: " + ", ".join(token_hits[:5]))
    return score, evidence


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result
