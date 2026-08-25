"""Unpayable remaining is a generic consume-path fact, not a figure special case."""

from __future__ import annotations

from types import SimpleNamespace

from omni.agent.intent_plan import IntentPlan, IntentType, ToolPolicy, VerificationPlan
from omni.agent.model_planner import ModelPlanProposal
from omni.runtime.unpayable import (
    apply_unpayable_contract,
    unpayable_deliverables,
)
from omni.skills_runtime.admission import TurnConstraints
from omni.skills_runtime.manifest import SkillEntry, SkillKind


def _entry(name: str, capabilities: list[str], *, services: list[str] | None = None) -> SkillEntry:
    return SkillEntry(
        name=name,
        description=name,
        kind=SkillKind.PYTHON_ENGINE,
        capabilities=capabilities,
        requires_services=list(services or []),
    )


def _registry(*entries: SkillEntry, vlm_available: bool = False) -> SimpleNamespace:
    gateway = SimpleNamespace(
        available=vlm_available,
        setup_command="omni config vlm",
        error_code="vlm_not_configured",
        missing=("model",),
    )
    return SimpleNamespace(
        list_selectable=lambda: list(entries),
        admission_services=lambda services=None, ctx=None: services or {"vlm": gateway},
    )


def _plan(*outputs: str) -> IntentPlan:
    return IntentPlan(
        task_id="t1",
        user_message="do the work",
        intent_type=IntentType.REACT_FALLBACK,
        outputs=list(outputs),
        verification_plan=VerificationPlan(required_outputs=list(outputs)),
        tool_policy=ToolPolicy(),
    )


def test_sibling_skill_keeps_a_shared_slot_payable() -> None:
    registry = _registry(
        _entry("livefigure", ["artifact.figure", "figure.editable.pptx"], services=["vlm"]),
        _entry("scientific-figure", ["artifact.figure"]),
        vlm_available=False,
    )
    debts = unpayable_deliverables(_plan("artifact.figure"), registry=registry)
    assert debts == []


def test_stricter_slot_is_unpayable_when_its_only_producer_is_rejected() -> None:
    registry = _registry(
        _entry("livefigure", ["artifact.figure", "figure.editable.pptx"], services=["vlm"]),
        _entry("scientific-figure", ["artifact.figure"]),
        vlm_available=False,
    )
    debts = unpayable_deliverables(_plan("artifact.pptx"), registry=registry)
    assert [debt.output for debt in debts] == ["artifact.pptx"]
    assert "omni config vlm" in debts[0].ask


def test_user_forbidden_service_makes_the_skill_unpayable_even_when_configured() -> None:
    registry = _registry(
        _entry("livefigure", ["figure.editable.pptx"], services=["vlm"]),
        vlm_available=True,
    )
    debts = unpayable_deliverables(
        _plan("artifact.pptx"),
        registry=registry,
        constraints=TurnConstraints(unavailable_services=frozenset({"vlm"})),
    )
    assert [debt.output for debt in debts] == ["artifact.pptx"]
    assert debts[0].code == "vlm_unavailable_this_turn"


def test_writing_debt_stays_payable_without_a_skill() -> None:
    debts = unpayable_deliverables(
        _plan("draft.manuscript"),
        registry=_registry(),
    )
    assert debts == []


def test_mixed_unpayable_keeps_executing() -> None:
    registry = _registry(
        _entry("livefigure", ["figure.editable.pptx"], services=["vlm"]),
        vlm_available=False,
    )
    plan = apply_unpayable_contract(
        _plan("artifact.pptx", "draft.manuscript"),
        registry=registry,
    )
    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert [item["output"] for item in plan.unpayable_outputs] == ["artifact.pptx"]


def test_only_unpayable_files_become_needs_input() -> None:
    registry = _registry(
        _entry("livefigure", ["figure.editable.pptx"], services=["vlm"]),
        vlm_available=False,
    )
    plan = apply_unpayable_contract(_plan("artifact.pptx"), registry=registry)
    assert plan.intent_type is IntentType.NEEDS_INPUT
    assert any(item.get("field") == "artifact.pptx" for item in plan.missing_inputs)


def test_planner_reads_unavailable_services_from_the_proposal() -> None:
    proposal = ModelPlanProposal.from_payload(
        {
            "intent_type": "react_fallback",
            "required_capabilities": ["figure.editable.pptx"],
            "outputs": ["artifact.pptx"],
            "unavailable_services": ["vlm"],
            "rationale": "user forbade the current VLM",
        }
    )
    assert proposal.unavailable_services == ["vlm"]
    registry = _registry(
        _entry("livefigure", ["figure.editable.pptx"], services=["vlm"]),
        vlm_available=True,
    )
    plan = apply_unpayable_contract(
        _plan("artifact.pptx"),
        registry=registry,
        proposal=proposal,
    )
    assert plan.intent_type is IntentType.NEEDS_INPUT
    assert plan.unavailable_services == ["vlm"]
