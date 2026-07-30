from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .http_client import HttpClient
from .ids import scientist_slug
from .io_utils import write_json, write_jsonl
from .models import IdentityCandidate, ScientistProfile


class IdentityAmbiguityError(RuntimeError):
    pass


class IdentityResolver:
    def __init__(self, http: HttpClient | None = None):
        self.http = http or HttpClient()
        self.attempts: list[dict[str, Any]] = []

    def resolve(
        self,
        name: str,
        *,
        field: str | None = None,
        institution: str | None = None,
    ) -> list[IdentityCandidate]:
        candidates: list[IdentityCandidate] = []
        channels = [
            ("openalex", lambda: self._search_openalex(name)),
            ("semantic_scholar", lambda: self._search_semantic_scholar(name)),
        ]
        successes = 0
        for channel, operation in channels:
            try:
                found = operation()
                candidates.extend(found)
                successes += 1
                self.attempts.append(
                    {"channel": channel, "status": "ok", "candidate_count": len(found)}
                )
            except Exception as exc:  # noqa: BLE001 - isolate independent identity providers
                self.attempts.append(
                    {
                        "channel": channel,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        if not successes:
            raise RuntimeError("All identity providers failed; inspect identity_attempts.jsonl")
        merged = self._merge_candidates(candidates)
        wikidata = self._wikidata_enrichment(name)
        if wikidata:
            self.attempts.append({"channel": "wikidata", "status": "ok", "candidate_count": 1})
            self._apply_wikidata(merged, wikidata)
        for index, candidate in enumerate(merged):
            merged[index] = self._score(candidate, name, field, institution)
        merged.sort(
            key=lambda item: (item.score, item.citation_count, item.paper_count),
            reverse=True,
        )
        return merged

    def choose(
        self,
        candidates: list[IdentityCandidate],
        *,
        selected_candidate_id: str | None = None,
    ) -> IdentityCandidate:
        if not candidates:
            raise IdentityAmbiguityError("No academic identity candidate was found")
        if selected_candidate_id:
            selected = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.candidate_id == selected_candidate_id
                    or selected_candidate_id in candidate.provider_ids.values()
                ),
                None,
            )
            if not selected:
                raise IdentityAmbiguityError(
                    f"Selected identity candidate does not exist: {selected_candidate_id}"
                )
            selected.evidence.append("explicitly selected by user")
            return replace(selected, score=1.0)
        top = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None
        margin = top.score - (runner_up.score if runner_up else 0.0)
        cross_provider = len(top.provider_ids) >= 2
        if top.score >= 0.78 and (margin >= 0.12 or cross_provider and margin >= 0.05):
            return top
        summary = "; ".join(
            f"{item.candidate_id}={item.display_name} score={item.score:.2f}"
            for item in candidates[:5]
        )
        raise IdentityAmbiguityError(
            "Identity is ambiguous. Select a candidate with --identity-candidate. "
            f"Top candidates: {summary}"
        )

    def enrich_selected_with_orcid(self, candidate: IdentityCandidate) -> None:
        if not candidate.orcid:
            return
        try:
            record = self.http.get_json(
                f"https://pub.orcid.org/v3.0/{urllib.parse.quote(candidate.orcid)}/record",
                headers={"Accept": "application/json"},
            )
            person = record.get("person") or {}
            activities = record.get("activities-summary") or {}
            urls = [
                str((item.get("url") or {}).get("value"))
                for item in ((person.get("researcher-urls") or {}).get("researcher-url") or [])
                if (item.get("url") or {}).get("value")
            ]
            keywords = [
                str(item.get("content"))
                for item in ((person.get("keywords") or {}).get("keyword") or [])
                if item.get("content")
            ]
            candidate.official_domains = _unique(
                candidate.official_domains
                + [urlparse(url).netloc for url in urls if urlparse(url).netloc]
            )
            candidate.fields = _unique(candidate.fields + keywords)
            candidate.education_history = _merge_timeline(
                candidate.education_history,
                _orcid_affiliations(activities.get("educations") or {}, "institution"),
            )
            candidate.employment_history = _merge_timeline(
                candidate.employment_history,
                _orcid_affiliations(activities.get("employments") or {}, "organization"),
            )
            candidate.biography_sources = _unique(
                candidate.biography_sources
                + [f"https://orcid.org/{candidate.orcid}", *urls]
            )
            candidate.evidence.append("ORCID public record enriched biography")
            self.attempts.append({"channel": "orcid", "status": "ok"})
        except Exception as exc:  # noqa: BLE001 - optional ORCID enrichment must degrade
            self.attempts.append(
                {"channel": "orcid", "status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
            )

    def enrich_selected_with_google_scholar(
        self, candidate: IdentityCandidate, profile_url: str | None = None
    ) -> None:
        """Add Scholar metadata only after the primary identity is selected.

        Google Scholar is useful for an author-curated publication list and avatar,
        but its name search is ambiguous. A result must agree on a work, institution,
        or verified academic email before it can alter the selected identity.
        """
        try:
            urls = [profile_url] if profile_url else self._google_scholar_search(candidate.display_name)
            profiles = [self._google_scholar_profile(url) for url in urls]
            profiles = [profile for profile in profiles if profile]
            ranked = sorted(
                ((self._google_scholar_match_score(candidate, profile), profile) for profile in profiles),
                key=lambda item: item[0], reverse=True,
            )
            if not ranked or ranked[0][0] < 0.45:
                self.attempts.append({"channel": "google_scholar", "status": "unmatched", "candidate_count": len(profiles)})
                return
            _, scholar = ranked[0]
            candidate.google_scholar_url = scholar["url"]
            candidate.google_scholar_author_id = scholar.get("author_id")
            candidate.known_works = _unique(candidate.known_works + scholar["works"])
            candidate.fields = _unique(candidate.fields + scholar["interests"])
            candidate.official_domains = _unique(candidate.official_domains + scholar["official_domains"])
            candidate.biography_sources = _unique(candidate.biography_sources + [scholar["url"]])
            if scholar.get("portrait_url"):
                candidate.portrait_url = scholar["portrait_url"]
                candidate.portrait_source_url = scholar["url"]
            candidate.evidence.append("Google Scholar profile matched by independent identity context")
            self.attempts.append({"channel": "google_scholar", "status": "ok", "profile_url": scholar["url"], "work_count": len(scholar["works"])})
        except Exception as exc:  # noqa: BLE001 - optional Scholar enrichment must degrade
            self.attempts.append({"channel": "google_scholar", "status": "failed", "error_type": type(exc).__name__, "error": str(exc)})

    def _google_scholar_search(self, name: str) -> list[str]:
        page = self.http.get_text(
            "https://scholar.google.com/citations",
            {"view_op": "search_authors", "mauthors": name, "hl": "en"},
        )
        _assert_google_scholar_page(page)
        return _unique([
            urllib.parse.urljoin("https://scholar.google.com", href.replace("&amp;", "&"))
            for href in re.findall(r'href=["\']([^"\']*citations\?[^"\']*\\buser=[^"\']+)["\']', page)
        ])[:8]

    def _google_scholar_profile(self, url: str) -> dict[str, Any] | None:
        page = self.http.get_text(url, headers={"User-Agent": "Mozilla/5.0"})
        _assert_google_scholar_page(page)
        name = _html_text_by_id(page, "gsc_prf_in")
        if not name:
            return None
        affiliation = _html_text_by_class(page, "gsc_prf_il")
        email = _html_text_by_id(page, "gsc_prf_ivh")
        interests = _html_texts_by_class(page, "gsc_prf_inta")
        works = _html_texts_by_class(page, "gsc_a_at")
        image = _html_attr_by_class(page, "gsc_prf_pup-img", "src")
        homepage = _html_attr_by_id(page, "gsc_prf_ila", "href")
        source_url = urllib.parse.urljoin("https://scholar.google.com", url)
        return {
            "url": source_url,
            "author_id": (urllib.parse.parse_qs(urllib.parse.urlparse(source_url).query).get("user") or [None])[0],
            "name": name,
            "affiliation": affiliation,
            "email": email,
            "interests": interests,
            "works": works,
            "portrait_url": urllib.parse.urljoin(source_url, image) if image else None,
            "official_domains": [urlparse(homepage).netloc] if homepage and urlparse(homepage).netloc else [],
        }

    def _google_scholar_match_score(self, candidate: IdentityCandidate, profile: dict[str, Any]) -> float:
        score = 0.25 if _same_name(candidate.display_name, str(profile["name"])) else 0.0
        work_agreement = _work_overlap(candidate.known_works, profile["works"])
        if work_agreement:
            score += 0.45
        affiliation = str(profile.get("affiliation") or "")
        if _contains_any(candidate.institutions, affiliation) or _contains_any([affiliation], "Tsinghua University") and _contains_any(candidate.institutions, "Tsinghua University"):
            score += 0.35
        email = str(profile.get("email") or "")
        if any(domain and domain.casefold() in email.casefold() for domain in candidate.official_domains):
            score += 0.35
        return score

    def _search_openalex(self, name: str) -> list[IdentityCandidate]:
        payload = self.http.get_json(
            "https://api.openalex.org/authors",
            {"search": name, "per-page": 10},
        )
        result: list[IdentityCandidate] = []
        for raw in payload.get("results", []):
            display_name = str(raw.get("display_name") or "").strip()
            identifier = str(raw.get("id") or "").rsplit("/", 1)[-1]
            if not display_name or not identifier:
                continue
            institutions = [
                str(item.get("display_name"))
                for item in raw.get("last_known_institutions") or []
                if item.get("display_name")
            ]
            fields = [
                str((item.get("topic") or {}).get("display_name"))
                for item in raw.get("topics") or []
                if (item.get("topic") or {}).get("display_name")
            ]
            known_works = self._openalex_sample_works(identifier)
            result.append(
                IdentityCandidate(
                    candidate_id=f"openalex:{identifier}",
                    display_name=display_name,
                    aliases=[
                        str(value)
                        for value in raw.get("display_name_alternatives") or []
                    ],
                    provider_ids={"openalex": identifier},
                    orcid=(str(raw.get("orcid")).rsplit("/", 1)[-1] if raw.get("orcid") else None),
                    institutions=institutions,
                    fields=fields,
                    known_works=known_works,
                    paper_count=int(raw.get("works_count") or 0),
                    citation_count=int(raw.get("cited_by_count") or 0),
                    evidence=["OpenAlex author search"],
                )
            )
        return result

    def _openalex_sample_works(self, author_id: str) -> list[str]:
        try:
            payload = self.http.get_json(
                "https://api.openalex.org/works",
                {
                    "filter": f"authorships.author.id:{author_id}",
                    "per-page": 8,
                    "sort": "cited_by_count:desc",
                    "select": "title",
                },
            )
        except Exception:  # noqa: BLE001 - sample works are optional enrichment
            return []
        return [
            str(work.get("title"))
            for work in payload.get("results", [])
            if work.get("title")
        ]

    def _search_semantic_scholar(self, name: str) -> list[IdentityCandidate]:
        payload = self.http.get_json(
            "https://api.semanticscholar.org/graph/v1/author/search",
            {
                "query": name,
                "limit": 10,
                "fields": (
                    "name,paperCount,citationCount,hIndex,"
                    "papers.title,papers.year,papers.fieldsOfStudy"
                ),
            },
        )
        result: list[IdentityCandidate] = []
        for raw in payload.get("data", []):
            display_name = str(raw.get("name") or "").strip()
            identifier = str(raw.get("authorId") or "")
            if not display_name or not identifier:
                continue
            result.append(
                IdentityCandidate(
                    candidate_id=f"semantic_scholar:{identifier}",
                    display_name=display_name,
                    aliases=[str(value) for value in raw.get("aliases") or []],
                    provider_ids={"semantic_scholar": identifier},
                    known_works=[
                        str(work.get("title"))
                        for work in raw.get("papers") or []
                        if work.get("title")
                    ],
                    fields=_unique(
                        [
                            str(field)
                            for work in raw.get("papers") or []
                            for field in work.get("fieldsOfStudy") or []
                        ]
                    ),
                    paper_count=int(raw.get("paperCount") or 0),
                    citation_count=int(raw.get("citationCount") or 0),
                    evidence=["Semantic Scholar author search"],
                )
            )
        return result

    def _merge_candidates(
        self, candidates: list[IdentityCandidate]
    ) -> list[IdentityCandidate]:
        merged: list[IdentityCandidate] = []
        for candidate in candidates:
            target = next(
                (
                    item
                    for item in merged
                    if _same_name(item.display_name, candidate.display_name)
                    and _work_overlap(item.known_works, candidate.known_works)
                ),
                None,
            )
            if target is None:
                merged.append(candidate)
                continue
            target.provider_ids.update(candidate.provider_ids)
            target.aliases = _unique(target.aliases + candidate.aliases)
            target.institutions = _unique(target.institutions + candidate.institutions)
            target.fields = _unique(target.fields + candidate.fields)
            target.known_works = _unique(target.known_works + candidate.known_works)
            target.paper_count = max(target.paper_count, candidate.paper_count)
            target.citation_count = max(target.citation_count, candidate.citation_count)
            target.evidence = _unique(
                target.evidence
                + candidate.evidence
                + ["cross-provider work-title agreement"]
            )
        return merged

    def _score(
        self,
        candidate: IdentityCandidate,
        name: str,
        field: str | None,
        institution: str | None,
    ) -> IdentityCandidate:
        score = 0.0
        names = [candidate.display_name, *candidate.aliases]
        if _same_name(candidate.display_name, name):
            score += 0.35
            candidate.evidence.append("exact normalized display-name match")
        elif any(_same_name(alias, name) for alias in names):
            score += 0.25
            candidate.evidence.append("exact normalized alias match")
        if len(candidate.provider_ids) >= 2:
            score += 0.3
        if candidate.known_works:
            score += 0.08
        if candidate.paper_count >= 5:
            score += 0.04
        if candidate.citation_count >= 100:
            score += 0.03
        if candidate.orcid:
            score += 0.05
        if any(evidence.startswith("Wikidata identity matched") for evidence in candidate.evidence):
            score += 0.1
        if institution:
            if _contains_any(candidate.institutions, institution):
                score += 0.15
                candidate.evidence.append(f"institution match: {institution}")
            else:
                score -= 0.1
        if field:
            context = candidate.fields + candidate.known_works
            if _contains_any(context, field):
                score += 0.1
                candidate.evidence.append(f"field match: {field}")
        candidate.score = round(max(0.0, min(score, 1.0)), 4)
        return candidate

    def _wikidata_enrichment(self, name: str) -> dict[str, Any] | None:
        try:
            search = self.http.get_json(
                "https://www.wikidata.org/w/api.php",
                {
                    "action": "wbsearchentities",
                    "search": name,
                    "language": "en",
                    "uselang": "en",
                    "type": "item",
                    "limit": 5,
                    "format": "json",
                },
            )
            match = next(
                (
                    item
                    for item in search.get("search", [])
                    if _same_name(str(item.get("label", "")), name)
                    and _looks_academic(str(item.get("description", "")))
                ),
                None,
            )
            if not match:
                return None
            entity_id = str(match["id"])
            entity_data = self.http.get_json(
                f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json"
            )
            entity = entity_data["entities"][entity_id]
            aliases = [
                value["value"]
                for language in ("en", "zh", "zh-hans")
                for value in (entity.get("aliases", {}).get(language) or [])
            ]
            claims = entity.get("claims") or {}
            urls = _claim_values(claims.get("P856") or [])
            orcids = _claim_values(claims.get("P496") or [])
            semantic_ids = _claim_values(claims.get("P4012") or [])
            field_ids = _entity_claim_ids(claims.get("P101") or [])
            education_claims = claims.get("P69") or []
            employment_claims = claims.get("P108") or []
            occupation_ids = _entity_claim_ids(claims.get("P106") or [])
            education_ids = _entity_claim_ids(education_claims)
            employment_ids = _entity_claim_ids(employment_claims)
            degree_ids = _qualifier_entity_ids(education_claims, "P512")
            labels = self._wikidata_labels(
                field_ids
                + education_ids
                + employment_ids
                + occupation_ids
                + degree_ids
            )
            semantic_profile = (
                self._semantic_scholar_profile(semantic_ids[0])
                if semantic_ids
                else {}
            )
            return {
                "display_name": str(match.get("label") or name),
                "description": str(match.get("description") or ""),
                "aliases": aliases,
                "official_domains": [
                    urlparse(url).netloc for url in urls if urlparse(url).netloc
                ],
                "orcid": orcids[0] if orcids else None,
                "semantic_scholar_author_id": semantic_ids[0] if semantic_ids else None,
                "fields": [labels[value] for value in field_ids if value in labels],
                "institutions": [
                    labels[value]
                    for value in _unique(education_ids + employment_ids)
                    if value in labels
                ],
                "education_history": _timeline_entries(
                    education_claims,
                    labels,
                    organization_key="institution",
                ),
                "employment_history": _timeline_entries(
                    employment_claims,
                    labels,
                    organization_key="organization",
                ),
                "occupations": [
                    labels[value] for value in occupation_ids if value in labels
                ],
                "biography_sources": [
                    f"https://www.wikidata.org/wiki/{entity_id}"
                ],
                "known_works": semantic_profile.get("known_works", []),
                "semantic_aliases": semantic_profile.get("aliases", []),
            }
        except Exception as exc:  # noqa: BLE001 - optional Wikidata enrichment must degrade
            self.attempts.append(
                {
                    "channel": "wikidata",
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            return None

    def _semantic_scholar_profile(self, author_id: str) -> dict[str, Any]:
        try:
            raw = self.http.get_json(
                "https://api.semanticscholar.org/graph/v1/author/"
                + urllib.parse.quote(author_id),
                {
                    "fields": (
                        "name,paperCount,citationCount,hIndex,"
                        "papers.title,papers.year,papers.fieldsOfStudy"
                    )
                },
            )
        except Exception:  # noqa: BLE001 - semantic profile is optional enrichment
            return {}
        return {
            "aliases": [str(value) for value in raw.get("aliases") or []],
            "known_works": [
                str(work.get("title"))
                for work in raw.get("papers") or []
                if work.get("title")
            ],
        }

    def _wikidata_labels(self, entity_ids: list[str]) -> dict[str, str]:
        if not entity_ids:
            return {}
        payload = self.http.get_json(
            "https://www.wikidata.org/w/api.php",
            {
                "action": "wbgetentities",
                "ids": "|".join(_unique(entity_ids)),
                "props": "labels",
                "languages": "en",
                "format": "json",
            },
        )
        return {
            entity_id: str(
                ((entity.get("labels") or {}).get("en") or {}).get("value") or ""
            )
            for entity_id, entity in (payload.get("entities") or {}).items()
            if ((entity.get("labels") or {}).get("en") or {}).get("value")
        }

    def _apply_wikidata(
        self,
        candidates: list[IdentityCandidate],
        wikidata: dict[str, Any],
    ) -> None:
        eligible = [
            candidate
            for candidate in candidates
            if _same_name(candidate.display_name, wikidata["display_name"])
            and (
                not wikidata.get("orcid")
                or not candidate.orcid
                or candidate.orcid == wikidata["orcid"]
            )
        ]
        if not eligible:
            return
        scored = sorted(
            (
                (_wikidata_context_score(candidate, wikidata), candidate)
                for candidate in eligible
            ),
            key=lambda item: (item[0], item[1].citation_count),
            reverse=True,
        )
        best_score, best = scored[0]
        runner_score = scored[1][0] if len(scored) > 1 else -1
        best_orcid_match = bool(
            wikidata.get("orcid") and best.orcid == wikidata["orcid"]
        )
        runner_orcid_match = bool(
            len(scored) > 1
            and wikidata.get("orcid")
            and scored[1][1].orcid == wikidata["orcid"]
        )
        if best_score <= 0 or (
            best_score == runner_score
            and not (best_orcid_match and not runner_orcid_match)
        ):
            return
        best.aliases = _unique(
            [best.display_name]
            + wikidata["aliases"]
            + wikidata.get("semantic_aliases", [])
        )
        best.official_domains = _unique(wikidata["official_domains"])
        best.fields = _unique(wikidata.get("fields", []))
        best.institutions = _unique(wikidata.get("institutions", []))
        best.education_history = list(wikidata.get("education_history", []))
        best.employment_history = list(wikidata.get("employment_history", []))
        best.occupations = _unique(wikidata.get("occupations", []))
        best.biography_sources = _unique(wikidata.get("biography_sources", []))
        if wikidata.get("known_works"):
            trusted_titles = {
                _normalize_title(value): value
                for value in wikidata["known_works"]
            }
            intersection = [
                value
                for value in best.known_works
                if _normalize_title(value) in trusted_titles
            ]
            if intersection:
                best.known_works = _unique(intersection)
        if wikidata.get("semantic_scholar_author_id"):
            existing = best.provider_ids.get("semantic_scholar")
            if existing and existing != str(wikidata["semantic_scholar_author_id"]):
                best.provider_ids["semantic_scholar_search"] = existing
            best.provider_ids["semantic_scholar"] = str(
                wikidata["semantic_scholar_author_id"]
            )
        best.evidence.append(
            "Wikidata identity matched by ORCID and field/institution context"
        )


def resolve_identity(
    project_root: Path,
    scientist_name: str,
    *,
    scientist_id: str | None = None,
    field: str | None = None,
    institution: str | None = None,
    selected_candidate_id: str | None = None,
    google_scholar_url: str | None = None,
    http: HttpClient | None = None,
) -> tuple[str, Path]:
    resolved_id = scientist_id or scientist_slug(scientist_name)
    corpus = project_root / "scientist-corpus" / resolved_id
    corpus.mkdir(parents=True, exist_ok=True)
    resolver = IdentityResolver(http)
    try:
        candidates = resolver.resolve(
            scientist_name, field=field, institution=institution
        )
    finally:
        write_jsonl(corpus / "identity_attempts.jsonl", resolver.attempts)
    write_json(
        corpus / "identity_candidates.json",
        [candidate.to_dict() for candidate in candidates],
    )
    selected = resolver.choose(
        candidates, selected_candidate_id=selected_candidate_id
    )
    resolver.enrich_selected_with_orcid(selected)
    resolver.enrich_selected_with_google_scholar(selected, google_scholar_url)
    profile = ScientistProfile(
        scientist_id=resolved_id,
        scientist_name=selected.display_name,
        aliases=_unique([scientist_name, *selected.aliases]),
        openalex_author_id=selected.provider_ids.get("openalex"),
        semantic_scholar_author_id=selected.provider_ids.get("semantic_scholar"),
        google_scholar_author_id=selected.google_scholar_author_id,
        google_scholar_url=selected.google_scholar_url,
        orcid=selected.orcid,
        portrait_url=selected.portrait_url,
        portrait_source_url=selected.portrait_source_url,
        official_domains=selected.official_domains,
        institutions=selected.institutions,
        education_history=selected.education_history,
        employment_history=selected.employment_history,
        occupations=selected.occupations,
        biography_sources=selected.biography_sources,
        fields=selected.fields,
        known_works=selected.known_works,
    )
    profile_path = write_json(corpus / "profile.json", profile.to_dict())
    write_json(
        corpus / "identity_confirmation.json",
        {
            "scientist_id": resolved_id,
            "candidate_id": selected.candidate_id,
            "confidence": selected.score,
            "evidence": selected.evidence,
            "human_selected": bool(selected_candidate_id),
        },
    )
    return resolved_id, profile_path


def _orcid_affiliations(payload: dict[str, Any], organization_key: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for group in payload.get("affiliation-group") or []:
        for item in group.get("summaries") or []:
            summary = item.get("education-summary") or item.get("employment-summary") or {}
            organization = (summary.get("organization") or {}).get("name")
            if not organization:
                continue
            entry: dict[str, Any] = {organization_key: str(organization)}
            if summary.get("department-name"):
                entry["department"] = str(summary["department-name"])
            if summary.get("role-title"):
                entry["degree" if organization_key == "institution" else "role"] = str(summary["role-title"])
            start = summary.get("start-date") or {}
            end = summary.get("end-date") or {}
            if (start.get("year") or {}).get("value"):
                entry["start_year"] = int(start["year"]["value"])
            if (end.get("year") or {}).get("value"):
                entry["end_year"] = int(end["year"]["value"])
            entries.append(entry)
    return entries


def _merge_timeline(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = list(existing)
    seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in result}
    for item in incoming:
        fingerprint = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if fingerprint not in seen:
            result.append(item)
            seen.add(fingerprint)
    return result


def _assert_google_scholar_page(page: str) -> None:
    if "accounts.google.com" in page or "Sign in" in page[:5000]:
        raise RuntimeError("Google Scholar returned an authentication page, not a public profile")


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", value)).strip()


def _html_text_by_id(page: str, element_id: str) -> str:
    match = re.search(
        rf'<[^>]*\bid=["\']{re.escape(element_id)}["\'][^>]*>(.*?)</[^>]+>',
        page,
        re.IGNORECASE | re.DOTALL,
    )
    return _strip_html(match.group(1)) if match else ""


def _html_text_by_class(page: str, class_name: str) -> str:
    values = _html_texts_by_class(page, class_name)
    return values[0] if values else ""


def _html_texts_by_class(page: str, class_name: str) -> list[str]:
    matches = re.findall(
        rf'<[^>]*\bclass=["\'][^"\']*\b{re.escape(class_name)}\b[^"\']*["\'][^>]*>(.*?)</[^>]+>',
        page,
        re.IGNORECASE | re.DOTALL,
    )
    return _unique([_strip_html(value) for value in matches if _strip_html(value)])


def _html_attr_by_id(page: str, element_id: str, attribute: str) -> str | None:
    match = re.search(
        rf'<[^>]*\bid=["\']{re.escape(element_id)}["\'][^>]*\b{re.escape(attribute)}=["\']([^"\']+)["\']',
        page,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _html_attr_by_class(page: str, class_name: str, attribute: str) -> str | None:
    match = re.search(
        rf'<[^>]*\bclass=["\'][^"\']*\b{re.escape(class_name)}\b[^"\']*["\'][^>]*\b{re.escape(attribute)}=["\']([^"\']+)["\']',
        page,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", value.casefold())


def _same_name(left: str, right: str) -> bool:
    return bool(left and right and _normalize(left) == _normalize(right))


def _normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _work_overlap(left: list[str], right: list[str]) -> bool:
    if not left or not right:
        return False
    a = {_normalize_title(value) for value in left if len(value) >= 8}
    b = {_normalize_title(value) for value in right if len(value) >= 8}
    return bool(a & b)


def _contains_any(values: list[str], needle: str) -> bool:
    normalized = _normalize(needle)
    return any(normalized in _normalize(value) for value in values)


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = str(value).strip()
        key = _normalize(value)
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _looks_academic(description: str) -> bool:
    return bool(
        re.search(
            r"\b(scientist|researcher|professor|computer scientist|physicist|"
            r"chemist|biologist|mathematician|academic|engineer)\b",
            description,
            re.IGNORECASE,
        )
    )


def _claim_values(claims: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for claim in claims:
        value = (
            (claim.get("mainsnak") or {})
            .get("datavalue", {})
            .get("value")
        )
        if isinstance(value, str):
            values.append(value)
    return values


def _entity_claim_ids(claims: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for claim in claims:
        value = (
            (claim.get("mainsnak") or {})
            .get("datavalue", {})
            .get("value")
        )
        if isinstance(value, dict) and value.get("id"):
            result.append(str(value["id"]))
    return result


def _qualifier_entity_ids(
    claims: list[dict[str, Any]], property_id: str
) -> list[str]:
    result: list[str] = []
    for claim in claims:
        for qualifier in (claim.get("qualifiers") or {}).get(property_id, []):
            value = (qualifier.get("datavalue") or {}).get("value")
            if isinstance(value, dict) and value.get("id"):
                result.append(str(value["id"]))
    return result


def _timeline_entries(
    claims: list[dict[str, Any]],
    labels: dict[str, str],
    *,
    organization_key: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for claim in claims:
        organization_id = (
            (claim.get("mainsnak") or {})
            .get("datavalue", {})
            .get("value", {})
            .get("id")
        )
        if not organization_id or organization_id not in labels:
            continue
        qualifiers = claim.get("qualifiers") or {}
        degree_ids = [
            str((item.get("datavalue") or {}).get("value", {}).get("id"))
            for item in qualifiers.get("P512", [])
            if (item.get("datavalue") or {}).get("value", {}).get("id")
        ]
        entry: dict[str, Any] = {
            organization_key: labels[organization_id],
            "start_year": _qualifier_year(qualifiers, "P580"),
            "end_year": _qualifier_year(qualifiers, "P582"),
        }
        degrees = [labels[value] for value in degree_ids if value in labels]
        if degrees:
            entry["degree"] = degrees[0]
        entries.append(entry)
    return entries


def _qualifier_year(
    qualifiers: dict[str, list[dict[str, Any]]], property_id: str
) -> int | None:
    values = qualifiers.get(property_id) or []
    if not values:
        return None
    time_value = (values[0].get("datavalue") or {}).get("value", {}).get("time")
    match = re.match(r"^[+-](\d{4,})", str(time_value or ""))
    return int(match.group(1)) if match else None


def _wikidata_context_score(
    candidate: IdentityCandidate, wikidata: dict[str, Any]
) -> int:
    candidate_context = " ".join(
        [
            *candidate.fields,
            *candidate.institutions,
            *candidate.known_works,
        ]
    ).casefold()
    reference_values = [
        str(wikidata.get("description") or ""),
        *[str(value) for value in wikidata.get("fields", [])],
        *[str(value) for value in wikidata.get("institutions", [])],
    ]
    tokens = {
        token
        for value in reference_values
        for token in re.findall(r"[a-z]{4,}", value.casefold())
        if token not in {"researcher", "scientist", "chinese", "american"}
    }
    score = sum(1 for token in tokens if token in candidate_context)
    if wikidata.get("orcid") and candidate.orcid == wikidata["orcid"]:
        score += 1
    return score
