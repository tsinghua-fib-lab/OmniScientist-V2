"""Per-run token & cost accounting (P2)."""

from __future__ import annotations

import pytest

from omni.agent import OmniAgent
from omni.agent.cost import estimate_cost, estimate_tokens, rate_for
from omni.config import load_settings


def test_estimate_tokens_is_char_based_and_positive():
    assert estimate_tokens("") == 0
    assert estimate_tokens("x") == 1  # never zero for non-empty text
    assert estimate_tokens("a" * 400) == 100  # ~chars/4


def test_rate_for_matches_by_longest_substring():
    # dated/aliased model names resolve to their base entry
    assert rate_for("gpt-4o-2024-08-06") == (2.50, 10.0)
    # a more specific key wins over a shorter prefix
    assert rate_for("gpt-4o-mini") == (0.15, 0.60)
    # unknown model → conservative default (not free)
    rin, rout = rate_for("some-unknown-model")
    assert rin > 0 and rout > 0


def test_rate_for_honours_explicit_override():
    class _Cfg:
        input_per_mtok = 1.0
        output_per_mtok = 3.0

    assert rate_for("gpt-4o", _Cfg()) == (1.0, 3.0)


def test_estimate_cost_uses_provider_usage_when_present():
    est = estimate_cost(
        "gpt-4o",
        {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "total_tokens": 2_000_000},
    )
    assert est.estimated is False
    assert est.total_tokens == 2_000_000
    # 1M in @2.50 + 1M out @10.0 = 12.50
    assert est.cost_usd == pytest.approx(12.5)


def test_estimate_cost_falls_back_to_text_estimate_offline():
    est = estimate_cost(
        "omni-mock", {}, fallback_text_in="a" * 400, fallback_text_out="b" * 400
    )
    assert est.estimated is True
    assert est.prompt_tokens == 100
    assert est.completion_tokens == 100
    assert est.total_tokens == 200
    assert est.cost_usd == 0.0  # mock is priced free, but tokens are still counted


def test_estimate_cost_derives_total_when_missing():
    est = estimate_cost("gpt-4o", {"prompt_tokens": 10, "completion_tokens": 5})
    assert est.total_tokens == 15
    assert est.estimated is False


@pytest.mark.asyncio
async def test_handle_turn_records_cost_usage_event():
    """A real turn (mock provider) records a durable ``cost.usage`` event with
    positive token counts — spend is accountable even offline."""
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    agent = await OmniAgent.create(settings)
    try:
        res = await agent.handle_turn("帮我简单解释一下什么是向量数据库。", channel="cli")
        events = await agent.tasks.list_events(res.task_id)
        costs = [e for e in events if e.event_type == "cost.usage"]
        assert costs, "expected a cost.usage event"
        payload = costs[-1].output_json or {}
        assert int(payload.get("total_tokens") or 0) > 0
        assert float(payload.get("cost_usd", -1.0)) >= 0.0
        assert payload.get("currency") == "USD"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_cost_accounting_can_be_disabled():
    settings = load_settings(overrides={"model": {"provider": "mock"}, "cost": {"enabled": False}})
    agent = await OmniAgent.create(settings)
    try:
        res = await agent.handle_turn("你好，介绍一下你自己。", channel="cli")
        events = await agent.tasks.list_events(res.task_id)
        assert not [e for e in events if e.event_type == "cost.usage"]
    finally:
        await agent.aclose()
