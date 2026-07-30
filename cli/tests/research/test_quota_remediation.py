"""A rate-limited source must name the credential that ends the retry loop."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.research import connectors
from omni.research.http_policy import FailureKind, remediation_for
from omni.research.provider_health import ProviderHealth, credential_fingerprint
from omni.research.registry import ConnectorRegistry

_SET_KEY = "/config set research.semantic_scholar_api_key"


def test_a_public_tier_quota_failure_names_the_key_that_ends_the_loop():
    """Without a key the quota resets into the next 429; only a key breaks out."""
    hint = remediation_for(FailureKind.QUOTA_EXHAUSTED, "semanticscholar")

    assert _SET_KEY in hint


def test_a_caller_that_already_sent_a_key_is_told_to_wait_instead():
    """For them the wait really is the answer; asking again would be noise."""
    hint = remediation_for(
        FailureKind.QUOTA_EXHAUSTED, "semanticscholar", authenticated=True
    )

    assert _SET_KEY not in hint
    assert "out of quota" in hint


def test_a_source_with_no_credential_hint_keeps_the_plain_quota_text():
    hint = remediation_for(FailureKind.QUOTA_EXHAUSTED, "openalex")

    assert _SET_KEY not in hint
    assert "openalex" in hint


@pytest.mark.asyncio
async def test_semantic_scholar_tells_the_transport_whether_it_sent_a_key(monkeypatch):
    """The transport cannot guess; the connector is the one that knows."""
    seen: dict[str, object] = {}

    async def _fake_get_json(url, params, **kwargs):  # noqa: ANN001, ANN202
        seen.update(kwargs)
        return {"data": []}

    monkeypatch.setattr(connectors, "_get_json", _fake_get_json)

    await connectors.semanticscholar_search("latent space intervention")
    assert seen["authenticated"] is False

    await connectors.semanticscholar_search("latent space intervention", api_key="k")
    assert seen["authenticated"] is True


def _registry(*, key: str = "") -> ConnectorRegistry:
    settings = load_settings()
    settings.research.semantic_scholar_api_key = key
    return ConnectorRegistry(settings)


def _cooling_down(remediation: str) -> ProviderHealth:
    health = ProviderHealth(store_path=None, default_cooldown_s=1800.0)
    health.record_failure(
        "semanticscholar", FailureKind.QUOTA_EXHAUSTED, remediation=remediation,
    )
    return health


def test_a_cooling_down_source_still_names_the_key_it_never_had():
    """The breaker opens *because* there is no key, so the hint must survive it.

    Older breaker entries were recorded with the plain quota text, so the check
    has to be live rather than trusting whatever was persisted.
    """
    avail = _registry().connector_availability(
        "semanticscholar",
        health=_cooling_down("semanticscholar is out of quota for now; retry later."),
    )

    assert avail.state == "open"
    assert _SET_KEY in avail.remediation


def test_a_cooling_down_source_that_has_a_key_does_not_ask_for_one():
    avail = _registry(key="already-set").connector_availability(
        "semanticscholar",
        health=_cooling_down("semanticscholar is out of quota for now; retry later."),
    )

    assert avail.state == "open"
    assert _SET_KEY not in avail.remediation


def test_registering_the_key_retires_the_cooldown_it_was_told_to_fix():
    """Otherwise the remedy cannot be acted on for the rest of the half hour.

    The quota was spent on the anonymous tier; a request carrying a key is a
    different request, so the breaker recorded against "no key" says nothing
    about it and must not go on skipping the source.
    """
    health = ProviderHealth(store_path=None, default_cooldown_s=1800.0)
    health.record_failure(
        "semanticscholar", FailureKind.QUOTA_EXHAUSTED,
        remediation="retry later", credential=credential_fingerprint({}),
    )

    assert _registry().connector_availability("semanticscholar", health=health).state == "open"

    after = _registry(key="freshly-registered").connector_availability(
        "semanticscholar", health=health,
    )
    assert after.state == "available"


def test_a_cooldown_outlives_a_retry_that_changed_nothing():
    """Only a credential *change* retires it — otherwise nothing would hold."""
    health = ProviderHealth(store_path=None, default_cooldown_s=1800.0)
    health.record_failure(
        "semanticscholar", FailureKind.QUOTA_EXHAUSTED,
        remediation="retry later", credential=credential_fingerprint({}),
    )

    for _ in range(3):
        assert _registry().connector_availability("semanticscholar", health=health).state == "open"


def test_a_fingerprint_cannot_give_the_secret_back():
    """Breaker entries are persisted to disk, so the key must not be in them."""
    fingerprint = credential_fingerprint({"semantic_scholar_api_key": "sk-secret-value"})

    assert "sk-secret-value" not in fingerprint
    assert fingerprint != credential_fingerprint({"semantic_scholar_api_key": "other"})
    assert credential_fingerprint({}) == credential_fingerprint({"semantic_scholar_api_key": ""})


def test_the_credential_hint_is_not_repeated_when_the_breaker_already_carries_it():
    avail = _registry().connector_availability(
        "semanticscholar", health=_cooling_down(remediation_for(
            FailureKind.QUOTA_EXHAUSTED, "semanticscholar",
        )),
    )

    assert avail.remediation.count(_SET_KEY) == 1
