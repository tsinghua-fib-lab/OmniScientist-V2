"""Invariant-C: plan-time contract self-heal messages never reach the user.

A skill's ``missing_message`` (e.g. "A concrete arXiv id or URL is required…") is
an *engine self-heal* signal: it drives the recovery ladder / ReAct context, but
must never surface as a user-facing warning. The full ``degraded_warnings`` list
stays intact for status derivation and recovery; only the ``display_*`` views the
CLI/inbox read are filtered.
"""

from __future__ import annotations

from omni.agent.plan_validator import PlanValidationResult

_CONTRACT_MSG = "A concrete arXiv id or URL is required. If only a title is available, resolve it through literature search first."


def test_display_degraded_warnings_hide_self_heal_contract_but_keep_genuine_ones():
    result = PlanValidationResult()
    # A self-heal contract degrade (the skill's missing_message) …
    result.degrade("step_input_contract", _CONTRACT_MSG, scope="step", step_id="paper", missing_field="identifier")
    # … alongside a genuine, user-relevant degraded warning.
    result.degrade("figure_signature_mismatch", "the figure looks generic for a RAG request")

    # Raw list keeps everything (recovery ladder + status derivation depend on it).
    assert _CONTRACT_MSG in result.degraded_warnings
    # The user-facing view drops only the self-heal message.
    assert _CONTRACT_MSG not in result.display_degraded_warnings
    assert "the figure looks generic for a RAG request" in result.display_degraded_warnings


def test_display_warnings_pass_through_genuine_warnings():
    result = PlanValidationResult()
    result.warn("a plain, user-safe heads-up")
    assert result.display_warnings == ["a plain, user-safe heads-up"]


def test_provider_input_contract_is_also_a_self_heal_code():
    result = PlanValidationResult()
    result.degrade("provider_input_contract", _CONTRACT_MSG)
    assert _CONTRACT_MSG in result.degraded_warnings
    assert result.display_degraded_warnings == []
