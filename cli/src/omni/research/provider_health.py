"""Provider health tracking + circuit breaking for research connectors.

BUG-01's most visible symptom was a 40-second stall: OpenAlex returned
``Insufficient budget`` (a spent daily quota), the connector retried, failed,
and then the *next* workflow step hit the very same dead provider again. A
circuit breaker fixes that — once a provider reports an exhausted quota or a
missing credential, it is skipped (with a clear reason) until its cooldown
elapses, instead of being probed on every call.

The model is deliberately three-state, not binary:

* ``available`` — usable now.
* ``degraded``  — usable but rate-limit-prone (e.g. Semantic Scholar on the
  public tier without an API key). It is still tried, just ranked lower.
* ``open``      — breaker tripped; skip until ``cooldown_until``.

``degraded`` is a *static* property computed from credentials by the connector
registry; ``open`` is the *dynamic* breaker owned here. State is process-global
(so every step in one workflow run shares it) and best-effort persisted to the
home cache so a spent daily quota is remembered across separate CLI invocations
within its cooldown window.

A breaker also remembers *which credentials were in force when it tripped*, and
a change to them retires it early. Otherwise the advice a spent quota prints —
register an API key — cannot be acted on: the user configures the key, the
breaker keeps skipping the provider for the rest of its half hour, and nothing
appears to improve, which is exactly how a correct remedy comes to look wrong.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omni.research.http_policy import FailureKind

# 30 minutes: long enough to stop hammering a spent quota within a session,
# short enough that a genuinely reset budget recovers on its own. Overridable
# via ``research.provider_cooldown_s``.
_DEFAULT_COOLDOWN_S = 1800.0
# Transient/terminal blips clear fast; cap their cooldown so a one-off timeout
# does not sideline a healthy provider for half an hour.
_SHORT_COOLDOWN_S = 60.0


@dataclass(slots=True)
class BreakerState:
    """The dynamic breaker view of one provider."""

    provider: str
    open: bool
    kind: str = ""
    remediation: str = ""
    message: str = ""
    cooldown_remaining: float = 0.0


class ProviderHealth:
    """A per-provider circuit breaker with cooldowns and best-effort persistence.

    ``now`` is injectable so tests drive the clock deterministically without
    sleeping. ``store_path`` enables cross-invocation persistence; when ``None``
    the breaker is memory-only (the default for unit tests).
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        store_path: Path | None = None,
        default_cooldown_s: float = _DEFAULT_COOLDOWN_S,
    ) -> None:
        self._now = now
        self._store_path = store_path
        self._default_cooldown_s = float(default_cooldown_s)
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    # ── recording ────────────────────────────────────────────────────────
    def record_success(self, provider: str) -> None:
        """Clear any breaker for ``provider`` after a good response."""
        name = _norm(provider)
        if name in self._entries:
            del self._entries[name]
            self._save()

    def record_failure(
        self,
        provider: str,
        kind: FailureKind,
        *,
        retry_after: float | None = None,
        remediation: str = "",
        message: str = "",
        credential: str = "",
    ) -> None:
        """Open the breaker for ``provider`` if the failure warrants a cooldown.

        ``TERMINAL`` failures (a malformed query, a 404) are *query*-specific,
        not provider-wide, so they never trip the breaker. ``credential`` is the
        fingerprint of the secrets in force at the time, which is what lets a
        later credential change retire this entry ahead of its cooldown.
        """
        name = _norm(provider)
        if not name or kind is FailureKind.TERMINAL:
            return
        cooldown = self._cooldown_for(kind, retry_after)
        now = self._now()
        self._entries[name] = {
            "kind": str(kind.value),
            "opened_at": now,
            "cooldown_until": now + cooldown,
            "remediation": remediation,
            "message": message,
            "credential": credential,
        }
        self._save()

    # ── querying ─────────────────────────────────────────────────────────
    def is_open(self, provider: str) -> bool:
        return self.state(provider).open

    def state(self, provider: str, *, credential: str | None = None) -> BreakerState:
        """The breaker view of ``provider``, retired early if its premise changed.

        Pass ``credential`` — the current fingerprint from
        :func:`credential_fingerprint` — when the caller knows it. A quota that
        was spent on the anonymous tier says nothing about the same provider
        holding a key, so the entry is dropped rather than served out.
        """
        name = _norm(provider)
        entry = self._entries.get(name)
        if entry is None:
            return BreakerState(provider=name, open=False)
        # Judged only when both sides know: an entry recorded without a
        # fingerprint (an older cache, a caller that has no registry) must serve
        # out its cooldown rather than be retired on a comparison with nothing.
        recorded = str(entry.get("credential", ""))
        stale = bool(recorded) and credential is not None and recorded != credential
        remaining = float(entry.get("cooldown_until", 0.0)) - self._now()
        if stale or remaining <= 0.0:
            # Cooldown elapsed, or the credentials behind it changed: let the
            # provider be tried again (half-open).
            del self._entries[name]
            self._save()
            return BreakerState(provider=name, open=False)
        return BreakerState(
            provider=name,
            open=True,
            kind=str(entry.get("kind", "")),
            remediation=str(entry.get("remediation", "")),
            message=str(entry.get("message", "")),
            cooldown_remaining=remaining,
        )

    def snapshot(self) -> dict[str, BreakerState]:
        return {name: self.state(name) for name in list(self._entries)}

    # ── internals ────────────────────────────────────────────────────────
    def _cooldown_for(self, kind: FailureKind, retry_after: float | None) -> float:
        if retry_after is not None and retry_after > 0:
            return float(retry_after)
        if kind in (FailureKind.QUOTA_EXHAUSTED, FailureKind.AUTH_REQUIRED):
            return self._default_cooldown_s
        return min(_SHORT_COOLDOWN_S, self._default_cooldown_s)

    def _load(self) -> None:
        if self._store_path is None:
            return
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        now = self._now()
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            try:
                if float(entry.get("cooldown_until", 0.0)) > now:
                    self._entries[_norm(name)] = entry
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        if self._store_path is None:
            return
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(
                json.dumps(self._entries, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # persistence is best-effort — never fatal


def _norm(provider: str) -> str:
    return str(provider or "").strip().lower()


def credential_fingerprint(secrets: dict[str, Any] | None) -> str:
    """Identify a credential set without being able to reconstruct it.

    Breaker entries are persisted to disk, so the secrets themselves must never
    appear there. A digest is enough for the only question asked of it — are
    these the same credentials the breaker tripped under? — and an empty set
    fingerprints as ``"none"``, which is what makes "had no key, now has one"
    the observable change it needs to be.
    """
    material = sorted(
        (str(key), str(value)) for key, value in (secrets or {}).items() if value
    )
    if not material:
        return "none"
    digest = hashlib.sha256(
        "\n".join(f"{key}={value}" for key, value in material).encode("utf-8")
    )
    return digest.hexdigest()[:16]


_SHARED: dict[str, ProviderHealth] = {}


def shared_provider_health(
    paths: Any = None, *, default_cooldown_s: float = _DEFAULT_COOLDOWN_S
) -> ProviderHealth:
    """Return the process-global breaker for this machine (cached per store path).

    Rate limits and quotas are keyed to the machine's IP/credentials rather than
    a workspace, so state persists under the home cache and is shared across
    workspaces and CLI invocations.
    """
    store_path: Path | None = None
    if paths is not None:
        try:
            store_path = Path(paths.cache_dir) / "provider_health.json"
        except Exception:  # noqa: BLE001 — fall back to memory-only
            store_path = None
    key = str(store_path or "__memory__")
    inst = _SHARED.get(key)
    if inst is None:
        inst = ProviderHealth(store_path=store_path, default_cooldown_s=default_cooldown_s)
        _SHARED[key] = inst
    return inst


__all__ = [
    "BreakerState",
    "ProviderHealth",
    "credential_fingerprint",
    "shared_provider_health",
]
