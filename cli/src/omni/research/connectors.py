"""Curated scientific connectors: OpenAlex, Crossref, Unpaywall.

A small, *vetted* set of general academic sources (alongside the existing arXiv
client) that broadens omni's research action space without the unbounded risk of
"let the agent hit any API". Each connector normalises its provider's response
into the same paper dict shape the rest of omni uses (``title``/``authors``/
``year``/``doi``/``url``/``summary``), so results flow straight into the library
and the literature corpus.

Design mirrors :mod:`omni.research.arxiv`: stdlib + ``httpx`` only, a tiny
retry/back-off, graceful offline behaviour (one human-readable
:class:`ConnectorError` instead of a raw transport error), and all network calls
funnelled through the module-level :func:`_get_json` so they are trivially
mockable in offline tests.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
S2_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
BIORXIV_DETAILS = "https://api.biorxiv.org/details"
CLINICALTRIALS_STUDIES = "https://clinicaltrials.gov/api/v2/studies"
_USER_AGENT = (
    "OmniScientist/0.1 "
    "(+https://github.com/tsinghua-fib-lab/OmniScientist-V2)"
)
_MAX_RETRIES = 3
_TAG_RE = re.compile(r"<[^>]+>")


class ConnectorError(RuntimeError):
    """Raised when a connector cannot satisfy a request (network/HTTP/bad input)."""


async def _get_json(
    url: str, params: dict[str, Any], *, timeout: float = 15.0, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """GET JSON with retry/back-off; raise :class:`ConnectorError` on failure."""
    last_error: str | None = None
    hdrs = {"User-Agent": _USER_AGENT, "Accept": "application/json", **(headers or {})}
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url, params=params, headers=hdrs)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            raise ConnectorError(
                f"Could not connect to {url}; check the network or proxy. This connector cannot fetch data offline. Cause: {last_error}"
            ) from exc
        if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
            await asyncio.sleep(0.5 * (2**attempt))
            continue
        if resp.status_code >= 400:
            raise ConnectorError(f"{url} returned HTTP {resp.status_code}: {resp.text[:200]!r}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ConnectorError(f"{url} returned non-JSON content: {exc}") from exc
    raise ConnectorError(f"Request failed after retries: {last_error}")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def _norm_doi(doi: str) -> str:
    doi = (doi or "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    return doi.lower()


def _invert_abstract(inverted: dict[str, list[int]] | None) -> str:
    """Reconstruct OpenAlex ``abstract_inverted_index`` into plain text."""
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort(key=lambda t: t[0])
    return " ".join(w for _, w in positions)


def _openalex_wid(value: str) -> str:
    """Extract a bare OpenAlex work id (``W123…``) from an id/URL string."""
    m = re.search(r"(W\d+)", value or "")
    return m.group(1) if m else (value or "").strip()


def _normalize_openalex_work(w: dict[str, Any]) -> dict[str, Any]:
    """Normalise one raw OpenAlex work into omni's paper-dict shape."""
    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in (w.get("authorships") or [])
    ]
    venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name", "")
    return {
        "title": w.get("title") or w.get("display_name") or "",
        "authors": [a for a in authors if a],
        "year": str(w.get("publication_year") or ""),
        "doi": _norm_doi(w.get("doi") or ""),
        "url": w.get("doi") or w.get("id") or "",
        "venue": venue,
        "summary": _invert_abstract(w.get("abstract_inverted_index")),
        "origin": "openalex",
        "openalex_id": _openalex_wid(w.get("id") or ""),
        "referenced_works": [_openalex_wid(x) for x in (w.get("referenced_works") or [])],
    }


# ── OpenAlex ────────────────────────────────────────────────────────────────
async def openalex_search(
    query: str, *, rows: int = 8, email: str = ""
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"search": query, "per_page": max(1, min(int(rows), 25))}
    if email:
        params["mailto"] = email
    data = await _get_json(OPENALEX_API, params)
    return [_normalize_openalex_work(w) for w in (data.get("results") or [])]


async def _openalex_fetch_work(work_ref: str, *, email: str = "") -> dict[str, Any]:
    """Fetch one raw OpenAlex work by W-id, OpenAlex URL, or DOI."""
    ref = (work_ref or "").strip()
    if not ref:
        raise ConnectorError("OpenAlex requires a work id or DOI.")
    mailto = {"mailto": email} if email else {}
    if re.search(r"W\d+", ref):
        return await _get_json(f"{OPENALEX_API}/{_openalex_wid(ref)}", dict(mailto))
    doi = _norm_doi(ref)
    data = await _get_json(OPENALEX_API, {"filter": f"doi:{doi}", "per_page": 1, **mailto})
    results = data.get("results") or []
    if not results:
        raise ConnectorError(f"OpenAlex found no work for DOI {doi}.")
    return results[0]


async def openalex_references(
    work_ref: str, *, rows: int = 25, email: str = ""
) -> list[dict[str, Any]]:
    """Return the works ``work_ref`` cites (its bibliography), normalised."""
    work = await _openalex_fetch_work(work_ref, email=email)
    refs = [_openalex_wid(x) for x in (work.get("referenced_works") or [])]
    refs = [r for r in refs if r][: max(1, min(int(rows), 50))]
    if not refs:
        return []
    mailto = {"mailto": email} if email else {}
    data = await _get_json(
        OPENALEX_API,
        {"filter": f"openalex_id:{'|'.join(refs)}", "per_page": len(refs), **mailto},
    )
    return [_normalize_openalex_work(w) for w in (data.get("results") or [])]


async def openalex_cited_by(
    work_ref: str, *, rows: int = 25, email: str = ""
) -> list[dict[str, Any]]:
    """Return the works that cite ``work_ref``, normalised."""
    work = await _openalex_fetch_work(work_ref, email=email)
    wid = _openalex_wid(work.get("id") or "")
    if not wid:
        return []
    mailto = {"mailto": email} if email else {}
    data = await _get_json(
        OPENALEX_API,
        {"filter": f"cites:{wid}", "per_page": max(1, min(int(rows), 50)), **mailto},
    )
    return [_normalize_openalex_work(w) for w in (data.get("results") or [])]


# ── Crossref ──────────────────────────────────────────────────────────────
async def crossref_search(
    query: str, *, rows: int = 8, email: str = ""
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"query": query, "rows": max(1, min(int(rows), 25))}
    if email:
        params["mailto"] = email
    data = await _get_json(CROSSREF_API, params)
    items = (data.get("message") or {}).get("items", []) or []
    out: list[dict[str, Any]] = []
    for it in items:
        title_list = it.get("title") or []
        authors = [
            " ".join(p for p in (a.get("given", ""), a.get("family", "")) if p)
            for a in (it.get("author") or [])
        ]
        issued = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
        year = str(issued[0]) if issued and issued[0] else ""
        container = it.get("container-title") or []
        out.append({
            "title": title_list[0] if title_list else "",
            "authors": [a for a in authors if a],
            "year": year,
            "doi": _norm_doi(it.get("DOI") or ""),
            "url": it.get("URL") or "",
            "venue": container[0] if container else "",
            "summary": _strip_tags(it.get("abstract") or ""),
            "origin": "crossref",
        })
    return out


# ── Unpaywall ────────────────────────────────────────────────────────────
async def unpaywall_lookup(doi: str, *, email: str = "") -> dict[str, Any]:
    doi = _norm_doi(doi)
    if not doi:
        raise ConnectorError("Unpaywall requires a valid DOI.")
    if not email:
        raise ConnectorError(
            "Unpaywall requires a contact email. Configure it with: omni config set research.contact_email you@example.com"
        )
    data = await _get_json(f"{UNPAYWALL_API}/{doi}", {"email": email})
    best = data.get("best_oa_location") or {}
    authors = [
        " ".join(p for p in (a.get("given", ""), a.get("family", "")) if p)
        for a in (data.get("z_authors") or [])
    ]
    return {
        "title": data.get("title") or "",
        "authors": [a for a in authors if a],
        "year": str(data.get("year") or ""),
        "doi": doi,
        "url": best.get("url_for_pdf") or best.get("url") or data.get("doi_url") or "",
        "venue": data.get("journal_name") or "",
        "is_oa": bool(data.get("is_oa")),
        "oa_status": data.get("oa_status") or "",
        "summary": "",
        "origin": "unpaywall",
    }


# ── PubMed (NCBI E-utilities) ────────────────────────────────────────────────
def _pubmed_year(pubdate: str) -> str:
    m = re.search(r"(\d{4})", pubdate or "")
    return m.group(1) if m else ""


async def pubmed_search(
    query: str, *, rows: int = 8, email: str = ""
) -> list[dict[str, Any]]:
    """Search PubMed (biomedical literature) via esearch + esummary."""
    n = max(1, min(int(rows), 25))
    es_params: dict[str, Any] = {"db": "pubmed", "term": query, "retmode": "json", "retmax": n}
    if email:
        es_params["email"] = email
        es_params["tool"] = "OmniScientist"
    data = await _get_json(PUBMED_ESEARCH, es_params)
    ids = list((data.get("esearchresult") or {}).get("idlist") or [])[:n]
    if not ids:
        return []
    sum_params: dict[str, Any] = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
    if email:
        sum_params["email"] = email
        sum_params["tool"] = "OmniScientist"
    summ = await _get_json(PUBMED_ESUMMARY, sum_params)
    result = summ.get("result") or {}
    out: list[dict[str, Any]] = []
    for uid in ids:
        rec = result.get(uid)
        if not isinstance(rec, dict):
            continue
        authors = [str(a.get("name", "")) for a in (rec.get("authors") or []) if a.get("name")]
        doi = ""
        for aid in rec.get("articleids") or []:
            if str(aid.get("idtype")).lower() == "doi":
                doi = _norm_doi(aid.get("value") or "")
                break
        out.append({
            "title": _strip_tags(rec.get("title") or ""),
            "authors": authors,
            "year": _pubmed_year(str(rec.get("pubdate") or rec.get("epubdate") or "")),
            "doi": doi,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            "venue": rec.get("fulljournalname") or rec.get("source") or "",
            "summary": "",  # abstracts need efetch (XML); metadata-only here
            "origin": "pubmed",
            "pmid": uid,
        })
    return out


# ── Semantic Scholar (Graph API) ─────────────────────────────────────────────
async def semanticscholar_search(
    query: str, *, rows: int = 8, api_key: str = ""
) -> list[dict[str, Any]]:
    """Search Semantic Scholar; includes abstracts (great for grounded RAG)."""
    fields = "title,abstract,year,authors,externalIds,venue,url"
    params: dict[str, Any] = {"query": query, "limit": max(1, min(int(rows), 25)), "fields": fields}
    headers = {"x-api-key": api_key} if api_key else None
    data = await _get_json(S2_SEARCH, params, headers=headers)
    out: list[dict[str, Any]] = []
    for p in data.get("data") or []:
        ext = p.get("externalIds") or {}
        out.append({
            "title": p.get("title") or "",
            "authors": [str(a.get("name", "")) for a in (p.get("authors") or []) if a.get("name")],
            "year": str(p.get("year") or ""),
            "doi": _norm_doi(ext.get("DOI") or ""),
            "arxiv_id": str(ext.get("ArXiv") or "").strip(),
            "url": p.get("url") or "",
            "venue": p.get("venue") or "",
            "summary": p.get("abstract") or "",
            "origin": "semanticscholar",
        })
    return out


# ── bioRxiv / medRxiv ──────────────────────────────────────────────────────
async def biorxiv_search(
    query: str, *, rows: int = 8, server: str = "biorxiv", interval: str = "30d"
) -> list[dict[str, Any]]:
    """Search recent bioRxiv/medRxiv metadata and filter it locally by query.

    The official API exposes recent/date/category windows rather than arbitrary
    full-text query.  We request one bounded recent page and deterministically
    filter title, abstract, category, and authors client-side.
    """
    source = str(server or "biorxiv").strip().lower()
    if source not in {"biorxiv", "medrxiv"}:
        raise ConnectorError("bioRxiv connector server must be biorxiv or medrxiv")
    n = max(1, min(int(rows), 25))
    data = await _get_json(f"{BIORXIV_DETAILS}/{source}/{interval}/0/json", {})
    terms = [term for term in re.findall(r"[\w-]+", query.lower()) if len(term) > 1]
    out: list[dict[str, Any]] = []
    for item in data.get("collection") or []:
        haystack = " ".join(str(item.get(key) or "") for key in (
            "title", "abstract", "category", "authors",
        )).lower()
        if terms and not all(term in haystack for term in terms):
            continue
        doi = _norm_doi(str(item.get("doi") or ""))
        date = str(item.get("date") or "")
        authors = [part.strip() for part in re.split(r"[;,]", str(item.get("authors") or ""))]
        out.append({
            "title": item.get("title") or "",
            "authors": [author for author in authors if author],
            "year": date[:4] if len(date) >= 4 else "",
            "doi": doi,
            "url": f"https://doi.org/{doi}" if doi else "",
            "venue": source,
            "summary": item.get("abstract") or "",
            "origin": source,
            "category": item.get("category") or "",
            "version": str(item.get("version") or ""),
        })
        if len(out) >= n:
            break
    return out


# ── ClinicalTrials.gov API v2 ──────────────────────────────────────────────
async def clinicaltrials_search(query: str, *, rows: int = 8) -> list[dict[str, Any]]:
    """Search registered clinical studies through the official v2 API."""
    params = {
        "query.term": query,
        "pageSize": max(1, min(int(rows), 25)),
        "format": "json",
    }
    data = await _get_json(CLINICALTRIALS_STUDIES, params)
    out: list[dict[str, Any]] = []
    for study in data.get("studies") or []:
        protocol = study.get("protocolSection") or {}
        ident = protocol.get("identificationModule") or {}
        status = protocol.get("statusModule") or {}
        design = protocol.get("designModule") or {}
        conditions = protocol.get("conditionsModule") or {}
        description = protocol.get("descriptionModule") or {}
        arms = protocol.get("armsInterventionsModule") or {}
        outcomes = protocol.get("outcomesModule") or {}
        nct_id = str(ident.get("nctId") or "")
        sponsor = ((ident.get("organization") or {}).get("fullName") or "")
        interventions = [
            str(item.get("name") or "") for item in (arms.get("interventions") or [])
            if item.get("name")
        ]
        primary = [
            str(item.get("measure") or "") for item in (outcomes.get("primaryOutcomes") or [])
            if item.get("measure")
        ]
        start_date = str((status.get("startDateStruct") or {}).get("date") or "")
        out.append({
            "title": ident.get("briefTitle") or ident.get("officialTitle") or nct_id,
            "authors": [sponsor] if sponsor else [],
            "year": start_date[:4] if len(start_date) >= 4 else "",
            "doi": "",
            "url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "",
            "venue": "ClinicalTrials.gov",
            "summary": description.get("briefSummary") or "",
            "origin": "clinicaltrials",
            "kind": "trial",
            "nct_id": nct_id,
            "status": status.get("overallStatus") or "",
            "phases": list(design.get("phases") or []),
            "conditions": list(conditions.get("conditions") or []),
            "interventions": interventions,
            "primary_outcomes": primary,
            "has_results": bool(study.get("hasResults")),
        })
    return out


__all__ = [
    "ConnectorError",
    "openalex_search",
    "openalex_references",
    "openalex_cited_by",
    "crossref_search",
    "unpaywall_lookup",
    "pubmed_search",
    "semanticscholar_search",
    "biorxiv_search",
    "clinicaltrials_search",
]
