"""Bound contract files that no admitted consume path can pay this turn.

Admission is a route fact. This module is the caller that decides: when a
named file has no admitted skill and ``write_file`` cannot pay it, the debt
is unpayable. Codex stops when completion needs new authority; DeepSeek fails
a request that has no credential. Omni keeps honest remaining, then either
asks (nothing else is payable) or runs the work that still has a producer.

No deliverable or skill name is special here. Skills pay through declared
capabilities; writing debts pay through ``write_file`` unless the turn is a
retrieve window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from omni.agent.capabilities import (
    CAPABILITY_EDITABLE_PPTX_FIGURE,
    CAPABILITY_FIGURE,
    CAPABILITY_REVIEW,
    CAPABILITY_REVIEW_RESPONSE,
    CAPABILITY_SCIENTIFIC_POSTER,
    CAPABILITY_SLIDES_GENERATE,
    WRITING_DELIVERABLES,
    contract_outputs,
)
from omni.skills_runtime.admission import (
    TurnConstraints,
    skill_admission_rejection,
    turn_constraints_from,
)
from omni.skills_runtime.registry import capability_aliases

# Deliverable → the capability slot whose admitted skills can pay it.
# Writing tokens are omitted: ``write_file`` is the consume path.
_DELIVERABLE_CAPABILITIES: dict[str, str] = {
    CAPABILITY_FIGURE: CAPABILITY_FIGURE,
    "artifact.pptx": CAPABILITY_EDITABLE_PPTX_FIGURE,
    "artifact.slides": CAPABILITY_SLIDES_GENERATE,
    "artifact.poster": CAPABILITY_SCIENTIFIC_POSTER,
    "review": CAPABILITY_REVIEW,
    "response_letter": CAPABILITY_REVIEW_RESPONSE,
}


@dataclass(frozen=True, slots=True)
class UnpayableDebt:
    output: str
    reason: str
    ask: str
    code: str = ""
    command: str = ""
    action: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "output": self.output,
            "reason": self.reason,
            "ask": self.ask,
        }
        if self.code:
            payload["code"] = self.code
        if self.command:
            payload["command"] = self.command
        if self.action:
            payload["action"] = dict(self.action)
        return payload

    def as_gap(self) -> dict[str, Any]:
        gap = {
            "field": self.output,
            "ask": self.ask,
            "reason": self.reason,
        }
        if self.command:
            gap["default"] = ""
        return gap


def plan_contract_files(plan: Any) -> list[str]:
    verification = getattr(plan, "verification_plan", None)
    names = list(getattr(verification, "required_outputs", None) or [])
    names.extend(str(item) for item in (getattr(plan, "outputs", None) or []) if item)
    return list(dict.fromkeys(contract_outputs([str(name) for name in names if name])))


def entry_pays_capability(entry: Any, capability: str) -> bool:
    requested = str(capability or "").strip().lower()
    if not requested:
        return False
    aliases = {item.lower() for item in capability_aliases(requested)}
    aliases.add(requested)
    caps = {str(item).lower() for item in (getattr(entry, "capabilities", None) or [])}
    return bool(caps & aliases)


def write_file_can_pay(plan: Any, deliverable: str) -> bool:
    """Whether native write is a consume path for this bound file.

    Manuscripts are ``write_file``. Review / response-letter stay on their
    canonical skill: a leftover Markdown file does not retire those debts.
    """
    name = str(deliverable or "").strip()
    if name not in WRITING_DELIVERABLES:
        return False
    policy = getattr(plan, "tool_policy", None)
    if policy is None:
        return True
    blocked = {str(item) for item in (getattr(policy, "blocked_tools", None) or [])}
    if "run_skill" in blocked or "write_file" in blocked:
        return False
    allowed = getattr(policy, "allowed_tools", None)
    if allowed is not None and not allowed:
        return False
    return True


def admitted_producers(
    registry: Any,
    deliverable: str,
    *,
    services: dict[str, Any] | None = None,
    ctx: Any | None = None,
    constraints: TurnConstraints | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    """Admitted skill names that declare they can pay ``deliverable``.

    The last admission rejection is returned when the set is empty so the
    owner action can name the setup command.
    """
    capability = _DELIVERABLE_CAPABILITIES.get(str(deliverable or "").strip())
    if not capability or registry is None:
        return [], None
    banned = constraints if constraints is not None else turn_constraints_from(ctx)
    names: list[str] = []
    last_rejection: dict[str, Any] | None = None
    selectable = getattr(registry, "list_selectable", None)
    entries = selectable() if callable(selectable) else []
    probed = services
    if probed is None:
        probe = getattr(registry, "admission_services", None)
        if callable(probe):
            probed = probe(services=services, ctx=ctx)
    for entry in entries:
        if not entry_pays_capability(entry, capability):
            continue
        rejection = skill_admission_rejection(
            entry, services=probed, ctx=ctx, constraints=banned
        )
        if rejection is not None:
            last_rejection = rejection
            continue
        name = str(getattr(entry, "name", "") or "").strip()
        if name and name not in names:
            names.append(name)
    return names, last_rejection


def unpayable_deliverables(
    plan: Any,
    *,
    registry: Any,
    services: dict[str, Any] | None = None,
    ctx: Any | None = None,
    constraints: TurnConstraints | None = None,
) -> list[UnpayableDebt]:
    """Contract files on ``plan`` that have no consume path this turn."""
    banned = constraints if constraints is not None else turn_constraints_from(plan)
    debts: list[UnpayableDebt] = []
    for name in plan_contract_files(plan):
        if write_file_can_pay(plan, name):
            continue
        producers, rejection = admitted_producers(
            registry,
            name,
            services=services,
            ctx=ctx,
            constraints=banned,
        )
        if producers:
            continue
        debts.append(_debt_from_rejection(name, rejection))
    return debts


def apply_unpayable_contract(
    plan: Any,
    *,
    registry: Any,
    proposal: Any | None = None,
    services: dict[str, Any] | None = None,
    ctx: Any | None = None,
) -> Any:
    """Copy turn constraints onto the plan and ask only when nothing is payable.

    Mixed turns keep executing: paper and slides still run when an editable
    figure cannot. A turn whose every bound file is unpayable becomes
    ``needs_input`` — that is the owner action, not another ReAct hunt.
    """
    from omni.agent.intent_plan import IntentType
    from omni.agent.plan_factory import needs_input_plan

    if plan is None or getattr(plan, "intent_type", None) is IntentType.NEEDS_INPUT:
        return plan
    banned = turn_constraints_from(proposal) if proposal is not None else turn_constraints_from(plan)
    if proposal is not None and not banned:
        banned = turn_constraints_from(plan)
    merged = TurnConstraints(
        unavailable_services=(
            turn_constraints_from(plan).unavailable_services | banned.unavailable_services
        ),
        unavailable_skills=(
            turn_constraints_from(plan).unavailable_skills | banned.unavailable_skills
        ),
    )
    plan.unavailable_services = sorted(merged.unavailable_services)
    plan.unavailable_skills = sorted(merged.unavailable_skills)
    debts = unpayable_deliverables(
        plan, registry=registry, services=services, ctx=ctx, constraints=merged
    )
    plan.unpayable_outputs = [debt.to_dict() for debt in debts]
    if not debts:
        return plan
    payable = [
        name for name in plan_contract_files(plan) if name not in {debt.output for debt in debts}
    ]
    notice = _unpayable_notice(debts)
    notices = list(getattr(plan, "user_notices", None) or [])
    if notice and notice not in notices:
        notices.append(notice)
    plan.user_notices = notices
    if payable:
        return plan
    asked = needs_input_plan(
        getattr(plan, "user_message", "") or "",
        task_id=str(getattr(plan, "task_id", "") or ""),
        missing_inputs=[debt.as_gap() for debt in debts],
        rationale=notice or "bound files have no admitted producer this turn",
        confidence=max(float(getattr(plan, "confidence", 0) or 0), 0.7),
    )
    asked.unavailable_services = sorted(merged.unavailable_services)
    asked.unavailable_skills = sorted(merged.unavailable_skills)
    asked.unpayable_outputs = [debt.to_dict() for debt in debts]
    asked.user_notices = notices
    return asked


def unpayable_notice_text(items: list[Any] | None) -> str:
    debts: list[UnpayableDebt] = []
    for item in items or []:
        if isinstance(item, UnpayableDebt):
            debts.append(item)
        elif isinstance(item, dict) and item.get("output"):
            debts.append(
                UnpayableDebt(
                    output=str(item.get("output") or ""),
                    reason=str(item.get("reason") or ""),
                    ask=str(item.get("ask") or ""),
                    code=str(item.get("code") or ""),
                    command=str(item.get("command") or ""),
                )
            )
    return _unpayable_notice(debts)


def _unpayable_notice(debts: list[UnpayableDebt]) -> str:
    if not debts:
        return ""
    parts = []
    for debt in debts:
        chunk = f"{debt.output} cannot be produced this turn"
        if debt.ask:
            chunk += f": {debt.ask}"
        elif debt.reason:
            chunk += f" ({debt.reason})"
        parts.append(chunk)
    return " ".join(parts)


def _debt_from_rejection(output: str, rejection: dict[str, Any] | None) -> UnpayableDebt:
    action = rejection.get("action_required") if isinstance(rejection, dict) else None
    if not isinstance(action, dict):
        action = {}
    info = rejection.get("error_info") if isinstance(rejection, dict) else None
    code = str((info or {}).get("code") or action.get("code") or "unpayable_this_turn")
    command = str(
        action.get("command")
        or (rejection or {}).get("setup_command")
        or ""
    ).strip()
    reason = str(
        (rejection or {}).get("summary")
        or (rejection or {}).get("error")
        or "no admitted producer this turn"
    ).strip()
    if command:
        ask = f"Run `{command}` so this turn can produce {output}, or drop that deliverable."
    else:
        ask = (
            f"{output} has no admitted producer this turn. Configure the required "
            "service or skill, or drop that deliverable."
        )
    return UnpayableDebt(
        output=output,
        reason=reason,
        ask=ask,
        code=code,
        command=command,
        action=action or None,
    )


__all__ = [
    "UnpayableDebt",
    "admitted_producers",
    "apply_unpayable_contract",
    "entry_pays_capability",
    "plan_contract_files",
    "unpayable_deliverables",
    "unpayable_notice_text",
    "write_file_can_pay",
]
