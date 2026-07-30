"""Shared arXiv Atom API helpers.

Distilled from HelixForge's ``skills/research/arxiv_fetch`` engine but kept
dependency-light (stdlib XML + ``httpx``). The goals that matter for a
local-first CLI:

- **Robust identifier normalization** — bare ids, versioned ids, ``arXiv:``
  prefixes, and ``abs``/``pdf`` URLs all collapse to the same query id.
- **Resilient fetching** — a small retry/back-off loop plus arXiv's
  "1 request / 3s" courtesy gate, so transient 429/503/timeouts recover.
- **Graceful offline behaviour** — when the network is unreachable (no DNS,
  blocked egress, offline laptop) the helpers raise a single, human-readable
  :class:`ArxivError` instead of leaking a raw ``httpx.ConnectError``. Callers
  turn that into a clean ``{"status": "error", ...}`` tool result so the agent
  can tell the user *why* the lookup failed rather than crashing or looping.

Note: arXiv lookups are inherently online. The offline ``mock`` model can still
*drive* the command end-to-end, but it cannot fabricate paper metadata — a real
network path to ``export.arxiv.org`` is required for live data.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

ARXIV_API = "https://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"
_USER_AGENT = (
    "OmniScientist/0.1 "
    "(+https://github.com/tsinghua-fib-lab/OmniScientist-V2)"
)

# Identifier shapes: new (2401.01234[v2]) and old (cs/0202010) schemes.
_NEW_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
_OLD_ID_RE = re.compile(r"([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", re.IGNORECASE)
_ABS_URL_RE = re.compile(
    r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/(?P<id>[^/?#\s]+?)(?:\.pdf)?/?$",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"v\d+$")

_MIN_REQUEST_INTERVAL_S = 3.0
_MAX_RETRIES = 3


class ArxivError(RuntimeError):
    """Raised when an arXiv query cannot be satisfied (bad id, network, HTTP)."""


@dataclass
class _RateLimiter:
    """Single-process gate matching arXiv's 3-second courtesy guidance."""

    min_interval: float = _MIN_REQUEST_INTERVAL_S
    last_hit: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_hit
            if self.last_hit > 0.0 and elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self.last_hit = time.monotonic()


_RATE_LIMITER = _RateLimiter()


def normalize_arxiv_id(identifier: str) -> str:
    """Return a bare arXiv id (without version suffix) for ``identifier``.

    Accepts bare ids, versioned ids, the ``arXiv:`` prefix, and ``abs``/``pdf``
    URLs. Unparseable input is returned trimmed so the caller still gets a
    deterministic (if empty) query rather than an exception.
    """
    cleaned = (identifier or "").strip()
    if not cleaned:
        return ""
    if cleaned.lower().startswith(("http://", "https://")):
        match = _ABS_URL_RE.match(cleaned)
        if match:
            cleaned = match.group("id")
        else:  # last-ditch: take the trailing path segment
            tail = urlparse(cleaned).path.rstrip("/").rsplit("/", 1)[-1]
            cleaned = tail.removesuffix(".pdf")
    if cleaned.lower().startswith("arxiv:"):
        cleaned = cleaned[len("arxiv:"):]
    # Prefer a structured match (handles ids embedded in noisy text), else
    # fall back to stripping a trailing version suffix from the cleaned token.
    m = _NEW_ID_RE.search(cleaned) or _OLD_ID_RE.search(cleaned)
    if m:
        return m.group(1)
    return _VERSION_RE.sub("", cleaned)


async def _query(params: dict[str, Any], timeout: float = 15.0) -> str:
    """GET the arXiv API with retry/back-off; raise :class:`ArxivError`.

    Network failures (DNS, connect, timeout) and overload responses (429/503)
    are retried a few times; anything still failing surfaces as a single
    human-readable error.
    """
    last_error: str | None = None
    for attempt in range(_MAX_RETRIES):
        await _RATE_LIMITER.wait()
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as client:
                resp = await client.get(
                    ARXIV_API,
                    params=params,
                    headers={"User-Agent": _USER_AGENT, "Accept": "application/atom+xml"},
                )
        except httpx.HTTPError as exc:  # ConnectError / timeout / etc.
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            raise ArxivError(
                "Could not connect to arXiv (export.arxiv.org). Check the network or proxy and retry; "
                f"this connector cannot fetch data offline. Cause: {last_error}"
            ) from exc
        if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait_s = float(retry_after) if retry_after else 0.5 * (2 ** attempt)
            except ValueError:
                wait_s = 0.5 * (2 ** attempt)
            await asyncio.sleep(wait_s)
            continue
        if resp.status_code >= 400:
            raise ArxivError(
                f"arXiv API returned HTTP {resp.status_code}: {resp.text[:200]!r}"
            )
        return resp.text
    raise ArxivError(f"arXiv request failed after retries: {last_error}")


def _parse_entry(entry: ET.Element) -> dict[str, Any]:
    def text(tag: str) -> str:
        el = entry.find(f"{_ATOM}{tag}")
        return (el.text or "").strip() if el is not None else ""

    authors = [
        (a.find(f"{_ATOM}name").text or "").strip()
        for a in entry.findall(f"{_ATOM}author")
        if a.find(f"{_ATOM}name") is not None
    ]
    pdf_url = ""
    for link in entry.findall(f"{_ATOM}link"):
        if link.get("title") == "pdf" or link.get("type") == "application/pdf":
            pdf_url = link.get("href", "")
    raw_id = text("id")
    categories = [c.get("term", "") for c in entry.findall(f"{_ATOM}category")]
    return {
        "arxiv_id": normalize_arxiv_id(raw_id),
        "title": " ".join(text("title").split()),
        "authors": authors,
        "summary": " ".join(text("summary").split()),
        "published": text("published"),
        "updated": text("updated"),
        "pdf_url": pdf_url or raw_id.replace("/abs/", "/pdf/"),
        "abs_url": raw_id,
        "categories": [c for c in categories if c],
    }


def _parse_feed(xml: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ArxivError(f"arXiv returned unparseable content: {exc}") from exc
    return [_parse_entry(e) for e in root.findall(f"{_ATOM}entry")]


async def fetch_by_id(identifier: str) -> dict[str, Any]:
    """Resolve a single arXiv id/URL to structured metadata.

    Returns ``{"status": "error", ...}`` (never raises) so the result is a
    clean tool observation the agent can relay to the user.
    """
    arxiv_id = normalize_arxiv_id(identifier)
    if not arxiv_id:
        return {
            "status": "error",
            "arxiv_id": "",
            "error": f"Could not parse an arXiv id from {identifier!r}; use an id such as 2310.06825 or an arXiv URL.",
        }
    try:
        xml = await _query({"id_list": arxiv_id, "max_results": 1})
        entries = _parse_feed(xml)
    except ArxivError as exc:
        return {"status": "error", "arxiv_id": arxiv_id, "error": str(exc)}
    if not entries:
        return {"status": "not_found", "arxiv_id": arxiv_id}
    data = entries[0]
    data["status"] = "ok"
    return data


async def search(query: str, max_results: int = 8) -> list[dict[str, Any]]:
    """Relevance search across arXiv. Raises :class:`ArxivError` on failure."""
    xml = await _query({
        "search_query": f"all:{query}",
        "max_results": max(1, min(int(max_results), 25)),
        "sortBy": "relevance",
    })
    return _parse_feed(xml)
