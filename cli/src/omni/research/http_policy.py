"""Shared HTTP retry policy and failure taxonomy for research connectors.

Every literature connector used to reimplement its own tiny retry loop with
subtly different behaviour: :mod:`omni.research.connectors` ignored
``Retry-After`` and could not tell a *burst* rate-limit apart from an
*exhausted quota*, so it would happily retry an OpenAlex ``Insufficient budget``
response three times (pure waste) and then surface a bare string. This module
lifts the mature pattern already proven in :mod:`omni.core.llm.client`
(``RetryPolicy`` + ``Retry-After`` parsing + jittered backoff) into a single
research-layer primitive and adds the piece BUG-01 needs most: a **four-way
failure taxonomy** so callers know whether to retry, open a circuit breaker, or
ask the user to fix a credential.

Design constraints (see the plan's "avoid exhaustive patching" section):

* The quota/auth signals live in a small **declarative table**, not a
  per-provider ``if/elif`` ladder. A new source reuses the table; adding a
  phrase is data, not code.
* Failures are **structured data** (:class:`ConnectorFailure`) carrying the
  kind, the ``Retry-After`` hint, and an actionable remediation — never a raw
  transport exception and never a bare string.
* Networked callers stay trivially mockable: :func:`get_json` is the single
  funnel, exactly like the old ``_get_json``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import random
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any

import httpx

_USER_AGENT = (
    "OmniScientist/0.1 "
    "(+https://github.com/tsinghua-fib-lab/OmniScientist-V2)"
)


class FailureKind(StrEnum):
    """How a connector failure should be handled by the caller.

    * ``TRANSIENT`` — burst rate-limit (429 without a quota signal), 5xx,
      timeouts, connection resets. Worth retrying with backoff; respect
      ``Retry-After`` when present.
    * ``QUOTA_EXHAUSTED`` — the provider's budget/quota is spent (a 429/402 that
      matches a quota signal, or a 429 that persists after every retry). Retrying
      inside the same window only wastes time; open a circuit breaker and cool
      down until the quota resets.
    * ``AUTH_REQUIRED`` — 401/403 or a missing/invalid credential. Never
      retryable; surface an actionable remediation instead.
    * ``TERMINAL`` — other 4xx and malformed responses. Not retryable.
    """

    TRANSIENT = "transient"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_REQUIRED = "auth_required"
    TERMINAL = "terminal"


# Declarative signal tables. Substring match against a lowercased response body.
# New providers reuse these; extend by adding a phrase, never a branch.
_QUOTA_SIGNALS: tuple[str, ...] = (
    "insufficient budget",
    "you only have $0",
    "quota",
    "daily limit",
    "monthly limit",
    "usage limit",
    "rate limit exceeded",
    "too many requests",
    "out of credits",
    "budget exceeded",
)
_AUTH_SIGNALS: tuple[str, ...] = (
    "invalid api key",
    "api key required",
    "requires an api key",
    "unauthorized",
    "authentication",
    "forbidden",
    "not authorized",
)


@dataclass(slots=True)
class RetryPolicy:
    """Transient-error retry budget shared by every research connector.

    ``max_retries`` is *extra* attempts after the first. Backoff is exponential
    with symmetric jitter and capped at ``max_delay``; a server ``Retry-After``
    hint overrides the computed delay (capped) so we honour rate-limit windows
    instead of hammering. Mirrors :class:`omni.core.llm.client.RetryPolicy`.
    """

    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.1


DEFAULT_POLICY = RetryPolicy()


class ConnectorFailure(RuntimeError):
    """A structured connector failure: kind + Retry-After + remediation.

    Subclasses :class:`RuntimeError` and is aliased as ``ConnectorError`` in
    :mod:`omni.research.connectors`, so existing ``except ConnectorError`` sites
    and bare ``ConnectorError("msg")`` constructions keep working unchanged
    (``kind`` defaults to :attr:`FailureKind.TERMINAL`).
    """

    def __init__(
        self,
        message: str,
        *,
        kind: FailureKind = FailureKind.TERMINAL,
        provider: str = "",
        retry_after: float | None = None,
        remediation: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.provider = provider
        self.retry_after = retry_after
        self.remediation = remediation
        self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "kind": self.kind.value,
            "message": str(self),
            "retry_after": self.retry_after,
            "remediation": self.remediation,
            "status_code": self.status_code,
        }


def retry_after_seconds(headers: Mapping[str, Any] | None) -> float | None:
    """Parse a ``Retry-After`` header (numeric seconds or HTTP-date) if present."""
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:  # noqa: BLE001 — never let header quirks break retry
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    now = _dt.datetime.now(when.tzinfo or _dt.UTC)
    return max(0.0, (when - now).total_seconds())


def classify(status_code: int, headers: Mapping[str, Any] | None, body: str) -> FailureKind:
    """Map an HTTP error response to a :class:`FailureKind` (declarative)."""
    text = (body or "").lower()
    if status_code in (401, 403):
        return FailureKind.AUTH_REQUIRED
    if status_code == 402:  # Payment Required → credits/quota
        return FailureKind.QUOTA_EXHAUSTED
    if status_code == 429:
        if any(sig in text for sig in _QUOTA_SIGNALS):
            return FailureKind.QUOTA_EXHAUSTED
        return FailureKind.TRANSIENT  # burst limit — retry, respecting Retry-After
    if status_code >= 500:
        return FailureKind.TRANSIENT
    if status_code >= 400:
        if any(sig in text for sig in _QUOTA_SIGNALS):
            return FailureKind.QUOTA_EXHAUSTED
        if any(sig in text for sig in _AUTH_SIGNALS):
            return FailureKind.AUTH_REQUIRED
        return FailureKind.TERMINAL
    return FailureKind.TERMINAL


def remediation_for(kind: FailureKind, provider: str, *, authenticated: bool = False) -> str:
    """A short, actionable next step for a failure (never provider-branching code)."""
    if kind is FailureKind.AUTH_REQUIRED:
        hint = _AUTH_REMEDIATION.get(provider, "")
        base = f"{provider or 'This source'} rejected the request as unauthorized."
        return f"{base} {hint}".strip()
    if kind is FailureKind.QUOTA_EXHAUSTED:
        base = (
            f"{provider or 'This source'} is out of quota for now; retry later or "
            "let another connector cover this query."
        )
        # On the public tier the quota is small enough that "retry later" only
        # buys the next 429 — the credential is the way out, and the provider's
        # own 429 body usually says so. Say it too, but never to a caller who
        # already sent a key: for them the wait really is the answer.
        hint = "" if authenticated else _AUTH_REMEDIATION.get(provider, "")
        return f"{base} {hint}".strip()
    if kind is FailureKind.TRANSIENT:
        return f"{provider or 'This source'} is temporarily unavailable; a retry may succeed."
    return f"{provider or 'This source'} could not satisfy the request."


# Provider-specific credential hints (data, not branches). Absent → generic text.
_AUTH_REMEDIATION: dict[str, str] = {
    "semanticscholar": (
        "Configure a free key with "
        "`/config set research.semantic_scholar_api_key YOUR_KEY` "
        "(request one at https://www.semanticscholar.org/product/api)."
    ),
}


def compute_delay(policy: RetryPolicy, attempt: int, retry_after: float | None) -> float:
    """Delay before the next attempt: server hint (capped), else jittered backoff."""
    if retry_after is not None:
        return min(retry_after, policy.max_delay)
    base = min(policy.base_delay * (2**attempt), policy.max_delay)
    if policy.jitter > 0 and base > 0:
        base += base * policy.jitter * random.uniform(-1.0, 1.0)
    return max(0.0, base)


async def get_json(
    url: str,
    params: dict[str, Any],
    *,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    provider: str = "",
    policy: RetryPolicy = DEFAULT_POLICY,
    authenticated: bool = False,
) -> dict[str, Any]:
    """GET JSON with taxonomy-aware retry; raise :class:`ConnectorFailure`.

    Transient failures back off (honouring ``Retry-After``) up to
    ``policy.max_retries``; quota/auth/terminal failures are raised immediately
    with their kind and remediation so the caller can open a breaker or ask the
    user to fix a credential rather than blindly retrying. ``authenticated``
    says whether this request carried a credential, which is what separates
    "your quota reset is coming" from "you are on the public tier".
    """
    hdrs = {"User-Agent": _USER_AGENT, "Accept": "application/json", **(headers or {})}
    last_error: str | None = None
    for attempt in range(policy.max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url, params=params, headers=hdrs)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < policy.max_retries:
                await asyncio.sleep(compute_delay(policy, attempt, None))
                continue
            raise ConnectorFailure(
                f"Could not connect to {url}; check the network or proxy. "
                f"This connector cannot fetch data offline. Cause: {last_error}",
                kind=FailureKind.TRANSIENT,
                provider=provider,
                remediation=remediation_for(
                    FailureKind.TRANSIENT, provider, authenticated=authenticated
                ),
            ) from exc
        if resp.status_code < 400:
            try:
                return resp.json()
            except ValueError as exc:
                raise ConnectorFailure(
                    f"{url} returned non-JSON content: {exc}",
                    kind=FailureKind.TERMINAL,
                    provider=provider,
                    status_code=resp.status_code,
                ) from exc
        kind = classify(resp.status_code, resp.headers, resp.text)
        hinted = retry_after_seconds(resp.headers)
        if kind is FailureKind.TRANSIENT and attempt < policy.max_retries:
            last_error = f"HTTP {resp.status_code}"
            await asyncio.sleep(compute_delay(policy, attempt, hinted))
            continue
        # Non-retryable, or a transient that never cleared. A 429 that survives
        # every retry is a hard window limit → treat as quota so the breaker opens.
        final_kind = kind
        if kind is FailureKind.TRANSIENT and resp.status_code == 429:
            final_kind = FailureKind.QUOTA_EXHAUSTED
        raise ConnectorFailure(
            f"{url} returned HTTP {resp.status_code}: {resp.text[:200]!r}",
            kind=final_kind,
            provider=provider,
            retry_after=hinted,
            remediation=remediation_for(final_kind, provider, authenticated=authenticated),
            status_code=resp.status_code,
        )
    raise ConnectorFailure(  # pragma: no cover — loop always returns/raises above
        f"Request to {url} failed after retries: {last_error}",
        kind=FailureKind.TRANSIENT,
        provider=provider,
    )


def failure_from_exception(exc: BaseException, provider: str = "") -> ConnectorFailure:
    """Coerce any connector error into a :class:`ConnectorFailure`.

    Already-structured failures are returned as-is (with ``provider`` filled in
    when the raiser did not know it). Bare :class:`RuntimeError` strings (e.g.
    arXiv's ``ArxivError``) and transport errors are classified heuristically so
    the breaker and diagnostics stay uniform across every source.
    """
    if isinstance(exc, ConnectorFailure):
        if provider and not exc.provider:
            exc.provider = provider
        return exc
    message = str(exc) or type(exc).__name__
    low = message.lower()
    if any(sig in low for sig in _AUTH_SIGNALS):
        kind = FailureKind.AUTH_REQUIRED
    elif any(sig in low for sig in _QUOTA_SIGNALS):
        kind = FailureKind.QUOTA_EXHAUSTED
    else:
        kind = FailureKind.TRANSIENT
    return ConnectorFailure(
        message,
        kind=kind,
        provider=provider,
        remediation=remediation_for(kind, provider),
    )


__all__ = [
    "FailureKind",
    "RetryPolicy",
    "DEFAULT_POLICY",
    "ConnectorFailure",
    "retry_after_seconds",
    "classify",
    "remediation_for",
    "compute_delay",
    "get_json",
    "failure_from_exception",
]
