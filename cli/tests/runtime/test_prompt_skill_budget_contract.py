"""Declared budgets must be spendable, and spent budgets must not be replayed."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omni.skills_runtime.executor import _prompt_skill_result
from omni.skills_runtime.manifest import SkillEntry, execution_budget_warnings

_SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"


def _loop_result(*, kind: str, reason: str, content: str = "answer") -> SimpleNamespace:
    return SimpleNamespace(
        kind=kind,
        content=content,
        terminated_reason=reason,
        total_iterations=8,
        total_tool_calls=12,
        total_usage={},
        usage_budget={},
        tool_trace=[],
        tool_names=lambda: [],
    )


def _entry() -> SkillEntry:
    return SkillEntry(name="paper-review", description="d", source="builtin")


def test_iteration_ceiling_below_the_tool_budget_is_reported_as_an_authoring_defect():
    warnings = execution_budget_warnings(
        {"max_iterations": 8, "max_tool_calls": 40}, "paper-review"
    )

    assert warnings
    assert "max_iterations" in warnings[0]
    assert "20" in warnings[0], "the warning should name the coherent floor"


def test_a_coherent_budget_is_accepted_without_complaint():
    assert execution_budget_warnings({"max_iterations": 20, "max_tool_calls": 40}) == []


@pytest.mark.parametrize(
    "skill_dir",
    sorted(p for p in _SKILLS_ROOT.glob("*/SKILL.md")),
    ids=lambda p: p.parent.name,
)
def test_no_builtin_skill_declares_an_unspendable_tool_budget(skill_dir):
    frontmatter = yaml.safe_load(skill_dir.read_text(encoding="utf-8").split("---")[1])
    execution = (
        ((frontmatter or {}).get("metadata") or {}).get("helixforge") or {}
    ).get("execution")

    assert execution_budget_warnings(execution, skill_dir.parent.name) == []


def test_hitting_the_iteration_ceiling_is_disclosed_rather_than_passed_as_success():
    """Forced synthesis returns usable text; the run was still cut short."""
    payload = _prompt_skill_result(
        _entry(), _loop_result(kind="text", reason="synthesized_max_iterations")
    )

    assert payload["status"] == "partial"
    assert "iteration limit reached" in payload["warning"]


def test_a_spent_budget_is_not_advertised_as_retryable():
    payload = _prompt_skill_result(
        _entry(), _loop_result(kind="text", reason="synthesized_max_iterations")
    )

    assert payload["error_info"]["retryable"] is False
    assert payload["error_info"]["workflow_recoverable"] is False
    assert payload["next_action"] == "re-run with a larger max_iterations budget"


def test_a_transient_model_failure_stays_retryable():
    payload = _prompt_skill_result(_entry(), _loop_result(kind="error", reason="llm_rate_limited"))

    assert payload["error_info"]["retryable"] is True
    assert payload["error_info"]["workflow_recoverable"] is True


def test_a_clean_run_is_still_reported_as_success():
    payload = _prompt_skill_result(_entry(), _loop_result(kind="text", reason="done"))

    assert payload["status"] == "ok"
    assert "next_action" not in payload


def test_empty_prompt_skill_text_is_not_reported_as_success():
    payload = _prompt_skill_result(
        _entry(), _loop_result(kind="text", reason="done", content="")
    )

    assert payload["status"] == "error"
    assert "empty result" in payload["error"]
