"""The agent's two general web capabilities: search for pages, fetch one.

``web_fetch`` reaches a known URL through a host allowlist (SSRF guard).
``web_search`` finds URLs when nothing else can ground a query — the last rung
under the scholarly connectors and the local corpus.

Both hand the model text written by someone else, so both run it through
:func:`defend_observation` first.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from omni.core.injection import defend_observation
from omni.core.react_agent import ToolSpec
from omni.skills_runtime.context import ExecContext, Tool


def _host_allowed(host: str, patterns: list[str]) -> bool:
    host = host.lower()
    return any(fnmatch.fnmatch(host, p.lower()) for p in patterns)


def _resolved_addresses(host: str) -> set[str]:
    return {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}


def _validate_url(url: str, patterns: list[str], *, allow_private: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme: {parsed.scheme}"
    if parsed.username or parsed.password:
        return "URL credentials are not allowed"
    host = parsed.hostname or ""
    if not _host_allowed(host, patterns):
        return f"host '{host}' not in allowlist"
    if allow_private:
        return ""
    try:
        addresses = _resolved_addresses(host)
    except OSError as exc:
        return f"cannot resolve host '{host}': {exc}"
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return f"invalid resolved address for '{host}'"
        if not address.is_global:
            return f"host '{host}' resolves to non-public address {address}"
    return ""


def build_web_tools(ctx: ExecContext) -> list[Tool]:
    cfg = ctx.settings.web_fetch

    async def web_fetch(args: dict) -> str:
        url = str(args.get("url", "")).strip()
        if not url:
            return "ERROR: missing url"
        try:
            current = url
            body = b""
            encoding = "utf-8"
            async with httpx.AsyncClient(timeout=cfg.timeout_s, follow_redirects=False) as client:
                for _hop in range(cfg.max_redirects + 1):
                    validation_error = _validate_url(
                        current,
                        cfg.allow_hosts,
                        allow_private=cfg.allow_private_hosts,
                    )
                    if validation_error:
                        return f"ERROR: {validation_error}"
                    async with client.stream(
                        "GET", current, headers={"User-Agent": "OmniScientist/0.2"}
                    ) as resp:
                        if resp.is_redirect:
                            location = resp.headers.get("location", "")
                            if not location:
                                return "ERROR: redirect response has no Location header"
                            current = urljoin(current, location)
                            continue
                        resp.raise_for_status()
                        encoding = resp.encoding or "utf-8"
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in resp.aiter_bytes():
                            remaining = cfg.max_body_bytes - size
                            if remaining <= 0:
                                break
                            chunks.append(chunk[:remaining])
                            size += min(len(chunk), remaining)
                        body = b"".join(chunks)
                        break
                else:
                    return f"ERROR: too many redirects (max {cfg.max_redirects})"
        except httpx.HTTPError as exc:
            return f"ERROR: fetch failed: {exc}"
        text = body.decode(encoding, errors="replace")[:200_000]
        # Fetched web content is untrusted data: scan for prompt injection.
        guarded, _hits = defend_observation(
            text, mode=getattr(ctx.settings.security, "injection_defense", "flag")
        )
        return guarded

    async def web_search(args: dict) -> Any:
        from omni.research.web_search import run_web_search

        result = await run_web_search(
            ctx.settings,
            str(args.get("query", "")),
            num_results=int(args.get("num_results", 5) or 5),
            allowed_domains=[str(d) for d in (args.get("allowed_domains") or [])],
            blocked_domains=[str(d) for d in (args.get("blocked_domains") or [])],
        )
        mode = getattr(ctx.settings.security, "injection_defense", "flag")
        for hit in result.get("results") or []:
            for field in ("title", "snippet"):
                text = str(hit.get(field) or "")
                if text:
                    hit[field], _hits = defend_observation(text, mode=mode)
        return result

    tools = [
        Tool(
            ToolSpec("web_fetch", "Fetch a webpage or API response from an allowed host, including arXiv and Semantic Scholar.", {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            }),
            web_fetch,
        )
    ]
    # Omitted rather than offered-and-refused when switched off, so the model
    # spends no turn discovering a capability this deployment does not have.
    # (Codex drops its web_search spec the same way; OpenClaw returns None.)
    if getattr(ctx.settings, "web_search", None) is None or ctx.settings.web_search.enabled:
        tools.append(
            Tool(
                ToolSpec(
                    "web_search",
                    "Search the open web for pages relevant to a query. Use when the scholarly "
                    "connectors and the local corpus cannot ground it — for a preprint or tool too "
                    "recent to be indexed, a project page, or a source whose connector is "
                    "unavailable. Returns {title, url, snippet}; read a result with web_fetch and "
                    "cite the URL.",
                    {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "num_results": {"type": "integer", "description": "1-10; default 5"},
                            "allowed_domains": {"type": "array", "items": {"type": "string"},
                                                "description": "Optional: keep only these hosts"},
                            "blocked_domains": {"type": "array", "items": {"type": "string"},
                                                "description": "Optional: drop these hosts"},
                        },
                        "required": ["query"],
                    },
                ),
                web_search,
            )
        )
    return tools
