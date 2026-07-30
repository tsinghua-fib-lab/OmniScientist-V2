"""Per-run token & cost accounting (P2 experience).

Every turn that reaches the ReAct loop burns LLM tokens; this module turns that
into an auditable number. It reads the provider's reported ``usage`` when present
(exact) and otherwise falls back to a deterministic char-based estimate — so
accounting still works offline and with the mock/scenario provider — then prices
it from a small built-in table (overridable per-deployment via ``settings.cost``).

The orchestrator mirrors the result into a durable ``cost.usage`` run event so a
run's spend is visible after the fact (``omni task``) and the eval harness can
assert accounting happened. Pure + stdlib; no LLM, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from omni.core.termination import base_termination_reason

# USD per 1M tokens as ``(input, output)``. Coarse public list prices — good
# enough for a running estimate, not billing — and overridable per deployment via
# ``settings.cost.{input_per_mtok,output_per_mtok}``. Matched by longest
# substring so dated/aliased names (``gpt-4o-2024-08-06``) resolve to their base.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.0, 8.0),
    "o4-mini": (1.10, 4.40),
    "o3-mini": (1.10, 4.40),
    "o3": (2.0, 8.0),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "claude-3-5-haiku": (0.80, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-7-sonnet": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-opus-4": (15.0, 75.0),
    "qwen": (0.20, 0.60),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.0),
    "omni-mock": (0.0, 0.0),  # offline mock is free — accounting still records tokens
}
# Unknown model → conservative middle-ground so an unpriced model isn't free.
_DEFAULT_RATE = (0.50, 1.50)
_CHARS_PER_TOKEN = 4  # rough English/code heuristic for offline estimation


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate for text (used when no provider ``usage``)."""
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def rate_for(model: str, cost_cfg: Any = None) -> tuple[float, float]:
    """Return ``(input, output)`` USD-per-1M-tokens for ``model``.

    An explicit non-zero ``settings.cost`` override wins (per-deployment pricing);
    otherwise longest-substring match against :data:`PRICING`, then the default.
    """
    if cost_cfg is not None:
        i = float(getattr(cost_cfg, "input_per_mtok", 0) or 0)
        o = float(getattr(cost_cfg, "output_per_mtok", 0) or 0)
        if i > 0 or o > 0:
            return i, o
    m = (model or "").lower()
    best = ""
    for key in PRICING:
        if key in m and len(key) > len(best):
            best = key
    return PRICING[best] if best else _DEFAULT_RATE


@dataclass(slots=True)
class CostEstimate:
    """Token counts + estimated cost for one turn's LLM usage."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    estimated: bool  # True when token counts were char-estimated (no provider usage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "estimated": self.estimated,
        }


def estimate_cost(
    model: str,
    usage: dict[str, int] | None,
    *,
    cost_cfg: Any = None,
    fallback_text_in: str = "",
    fallback_text_out: str = "",
) -> CostEstimate:
    """Price a turn's usage, estimating token counts from text when unreported.

    ``usage`` is the provider's ``{prompt_tokens, completion_tokens, total_tokens}``
    (exact when present). When the provider reports nothing (offline / mock), token
    counts are estimated from ``fallback_text_in`` / ``fallback_text_out`` so the
    accounting is never silently zero and stays deterministic in tests.
    """
    prompt = int((usage or {}).get("prompt_tokens") or 0)
    completion = int((usage or {}).get("completion_tokens") or 0)
    total = int((usage or {}).get("total_tokens") or 0)
    estimated = False
    if prompt <= 0 and completion <= 0 and total <= 0:
        prompt = estimate_tokens(fallback_text_in)
        completion = estimate_tokens(fallback_text_out)
        total = prompt + completion
        estimated = True
    elif total <= 0:
        total = prompt + completion

    rin, rout = rate_for(model, cost_cfg)
    cost = (prompt / 1_000_000.0) * rin + (completion / 1_000_000.0) * rout
    return CostEstimate(model or "", prompt, completion, total, round(cost, 6), estimated)


async def record_cost_event(
    tasks: Any,
    settings: Any,
    llm: Any,
    task_id: str,
    result: Any,
    *,
    system: str,
    user_message: str,
    component: str = "coordinator",
) -> None:
    """Persist best-effort token and cost accounting for one LLM component."""
    cfg = getattr(settings, "cost", None)
    if (cfg is not None and not getattr(cfg, "enabled", True)) or not task_id:
        return
    model = getattr(llm, "model", "") or getattr(settings.model, "model", "")
    estimate = estimate_cost(
        model,
        getattr(result, "total_usage", None) or getattr(result, "usage", None),
        cost_cfg=cfg,
        fallback_text_in=f"{system}\n{user_message}",
        fallback_text_out=getattr(result, "content", "") or "",
    )
    currency = getattr(cfg, "currency", "USD") if cfg is not None else "USD"
    try:
        await tasks.append_event(
            task_id,
            event_type="cost.usage",
            status="succeeded",
            name=component,
            output_json={**estimate.to_dict(), "currency": currency, "component": component},
            summary=(
                f"{component}: cost ~{estimate.cost_usd:.4f} {currency} · tokens {estimate.total_tokens}"
                + (" (est)" if estimate.estimated else "")
            ),
        )
    except Exception:  # noqa: BLE001 - metering must never block a turn.
        pass


async def record_text_cost_event(
    tasks: Any,
    settings: Any,
    llm: Any,
    task_id: str,
    *,
    system: str,
    user_message: str,
    output: str,
    component: str,
) -> None:
    """Record an estimated component cost when a text-only LLM API hides usage."""

    class _TextResult:
        total_usage: dict[str, int] = {}
        content = output

    await record_cost_event(
        tasks,
        settings,
        llm,
        task_id,
        _TextResult(),
        system=system,
        user_message=user_message,
        component=component,
    )


def react_usage_limits(settings: Any, llm: Any) -> dict[str, int | float]:
    """Translate optional accounting config into ReAct enforcement arguments."""
    cfg = getattr(settings, "cost", None)
    if cfg is None or not getattr(cfg, "enabled", True):
        return {
            "max_total_tokens": 0,
            "max_cost_usd": 0.0,
            "input_cost_per_mtok": 0.0,
            "output_cost_per_mtok": 0.0,
        }
    model = getattr(llm, "model", "") or getattr(settings.model, "model", "")
    input_rate, output_rate = rate_for(model, cfg)
    return {
        "max_total_tokens": max(0, int(getattr(cfg, "max_total_tokens", 0) or 0)),
        "max_cost_usd": max(0.0, float(getattr(cfg, "max_cost_usd", 0.0) or 0.0)),
        "input_cost_per_mtok": input_rate,
        "output_cost_per_mtok": output_rate,
    }


def usage_budget_exhausted(result: Any) -> bool:
    """Whether an instrumented loop has consumed an enabled usage boundary."""
    if base_termination_reason(str(getattr(result, "terminated_reason", "") or "")) in {
        "max_total_tokens",
        "max_cost",
    }:
        return True
    budget = getattr(result, "usage_budget", None)
    if not isinstance(budget, dict) or not budget.get("enforced"):
        return False
    max_tokens = max(0, int(budget.get("max_total_tokens") or 0))
    max_cost = max(0.0, float(budget.get("max_cost_usd") or 0.0))
    return bool(
        (max_tokens and int(budget.get("total_tokens") or 0) >= max_tokens)
        or (max_cost and float(budget.get("cost_usd") or 0.0) >= max_cost)
    )


__all__ = [
    "PRICING",
    "CostEstimate",
    "estimate_cost",
    "estimate_tokens",
    "rate_for",
    "react_usage_limits",
    "record_cost_event",
    "record_text_cost_event",
    "usage_budget_exhausted",
]
