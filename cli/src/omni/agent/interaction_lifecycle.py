"""Plan/review interaction modes and approval lifecycle coordination."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from omni.agent.intent_plan import IntentPlan, IntentType, ToolPolicy
from omni.agent.plan_revision import (
    ExecutionAuthority,
    PlanRevision,
    canonical_plan_hash,
)
from omni.agent.plan_runner_utils import plan_summary
from omni.agent.turn_execution import TurnResult
from omni.core.approval import ApprovalGate

_REVIEW_BLOCKED_TOOLS = {
    "bash",
    "write_file",
    "edit_file",
    "run_compute",
    "run_skill",
    "run_workflow",
    "submit_task",
}


class ApprovalConflict(ValueError):
    """The reviewed execution authority changed or was claimed elsewhere."""


def normalize_interaction_mode(value: str | None, default: str = "auto") -> str:
    """Return one supported interaction mode, falling back to automatic."""
    mode = str(value or default or "auto").strip().lower()
    return mode if mode in {"auto", "plan", "review"} else "auto"


def resolve_execution_mode(value: Any, *, wait_for_tasks: bool, is_async: bool) -> str:
    """Resolve automatic skill delivery to inline, foreground, or background."""
    mode = str(value or "auto").lower().strip()
    if mode not in {"auto", "inline", "foreground", "background"}:
        mode = "auto"
    if mode != "auto":
        return mode
    if not is_async:
        return "inline"
    return "foreground" if wait_for_tasks else "background"


def build_approval_gate(
    *,
    settings: Any,
    tasks: Any,
    approver: Any,
    session_allow: dict[str, set[str]],
    task_id: str,
    channel: str,
    session_id: str,
    additional_sensitive_tools: set[str] | None = None,
) -> ApprovalGate:
    """Build a sensitive-tool gate whose decisions are persisted as task events."""
    allow = session_allow.setdefault(session_id, set())

    async def on_event(kind: str, payload: dict[str, Any]) -> None:
        try:
            await tasks.append_event(
                task_id,
                event_type=kind,
                status="succeeded",
                name=str(payload.get("tool") or "approval"),
                tool_name=str(payload.get("tool") or ""),
                output_json=payload,
                summary=str(payload.get("summary") or kind),
            )
        except Exception:  # noqa: BLE001 - approval remains authoritative if audit fails.
            pass

    async def preauthorizer(tool_name: str, _arguments: dict[str, Any]) -> bool:
        if not task_id:
            return False
        task = await tasks.get_task(task_id)
        return (
            task is not None
            and task.status in {"running", "recovering"}
            and tool_name in set(task.approved_tools or [])
        )

    return ApprovalGate(
        settings,
        channel=channel,
        approver=approver,
        on_event=on_event,
        session_allow=allow,
        additional_sensitive_tools=additional_sensitive_tools,
        preauthorizer=preauthorizer,
    )


def apply_interaction_mode(plan: IntentPlan, mode: str) -> IntentPlan:
    """Apply read-only review constraints to a model-produced plan."""
    if mode != "review":
        return plan
    policy = ToolPolicy(
        allowed_tools=(
            [name for name in plan.tool_policy.allowed_tools if name not in _REVIEW_BLOCKED_TOOLS]
            if plan.tool_policy.allowed_tools is not None
            else None
        ),
        blocked_tools=sorted({*plan.tool_policy.blocked_tools, *_REVIEW_BLOCKED_TOOLS}),
        per_tool_limits=dict(plan.tool_policy.per_tool_limits),
        max_tool_calls=plan.tool_policy.max_tool_calls,
        max_iterations=plan.tool_policy.max_iterations,
        final_reserve_enabled=plan.tool_policy.final_reserve_enabled,
    )
    return replace(
        plan,
        intent_type=IntentType.REACT_FALLBACK,
        execution_mode="review",
        outputs=["review"],
        tool_policy=policy,
        rationale=(
            plan.rationale + "; review mode: read-only execution with mandatory self-review"
        ).strip("; "),
    )


def plan_mode_text(plan: IntentPlan) -> str:
    """Render a compact approval-oriented plan summary."""
    lines = [
        "The execution plan has been generated and validated but has not run yet.",
        f"- intent: {plan.intent_type.value}",
        f"- execution: {plan.execution_mode}",
        (
            f"- revision: {plan.revision} "
            f"({(plan.revision_hash or canonical_plan_hash(plan))[:12]})"
        ),
    ]
    if plan.selected_skills:
        lines.append("- skills:")
        lines.extend(f"  - {item.skill}: {item.reason}" for item in plan.selected_skills)
    if plan.workflow_steps:
        lines.append("- workflow:")
        for step in plan.workflow_steps:
            lines.append(
                "  - "
                + str(step.get("id") or step.get("skill_name") or "step")
                + f" ({step.get('skill_name') or step.get('capability') or 'synthesis'})"
            )
    lines.append("Approval scope: sensitive tools declared by providers selected in this plan only.")
    lines.append(f"Approve execution: /task approve {plan.task_id[:8]}")
    return "\n".join(lines)


@dataclass(frozen=True)
class TurnStart:
    """Normalized identity of a new or resumed user turn."""

    mode: str
    session_id: str
    channel: str
    user_message: str
    task_id: str
    resumed: bool


class InteractionLifecycle:
    """Own task creation/resumption and plan-mode pause/approval transitions."""

    def __init__(self, settings: Any, tasks: Any, hooks: Any, task_controller: Any) -> None:
        self._settings = settings
        self._tasks = tasks
        self._hooks = hooks
        self._run_controller = task_controller

    async def begin(
        self,
        *,
        user_message: str,
        session_id: str | None,
        channel: str,
        interaction_mode: str | None,
        existing_task_id: str,
        ensure_session: Any,
        on_task_ack: Any,
    ) -> TurnStart:
        mode = normalize_interaction_mode(
            interaction_mode, self._settings.interaction.default_mode
        )
        if existing_task_id:
            existing = await self._tasks.get_task(existing_task_id)
            if existing is None:
                raise LookupError(f"task not found: {existing_task_id}")
            session_id = session_id or existing.session_id
            channel = existing.channel or channel
            user_message = user_message or existing.user_input
            task_id = existing.id
            # Plan approval claims awaiting_approval -> running atomically with
            # the reviewed authority.  Do not emit a fictitious running ->
            # running task.resumed transition when execution begins afterward.
            # Other callers (scheduled/recovery resumes) still get a real
            # transition when their persisted task is not already active.
            if existing.status != "running":
                await self._tasks.mark_running(
                    task_id,
                    summary="approved plan execution started",
                )
            event_name = "run_resume"
        else:
            session_id = session_id or await ensure_session(channel=channel)
            task_id = await self._run_controller.create_turn_task(
                session_id=session_id,
                channel=channel,
                user_input=user_message,
                on_task_ack=on_task_ack,
            )
            event_name = "run_start"
        await self._hooks.emit(
            event_name,
            task_id=task_id,
            payload={"channel": channel, "session_id": session_id, "mode": mode},
        )
        return TurnStart(mode, session_id, channel, user_message, task_id, bool(existing_task_id))

    async def pause_for_approval(
        self,
        *,
        plan: IntentPlan,
        authority: ExecutionAuthority,
        session_id: str,
        persist_message: Any,
    ) -> str:
        if authority.plan_hash != canonical_plan_hash(plan):
            raise RuntimeError("cannot present approval for a different plan revision")
        text = plan_mode_text(plan)
        bound = await self._tasks.mark_awaiting_approval(
            plan.task_id,
            summary=plan_summary(plan),
            authority_fingerprint=authority.fingerprint,
            expected_plan_json=plan.to_dict(),
        )
        if not bound:
            raise RuntimeError(
                "plan changed before its approval request could be recorded"
            )
        await self._tasks.append_event(
            plan.task_id,
            event_type="plan.approval.requested",
            status="pending",
            name="plan",
            output_json={
                "revision": plan.revision,
                "revision_hash": plan.revision_hash or canonical_plan_hash(plan),
                **authority.to_dict(),
            },
            summary="waiting for approval of the exact execution authority",
        )
        await persist_message(
            session_id,
            "assistant",
            text,
            kind="plan",
            terminated_reason="awaiting_approval",
        )
        await self._hooks.emit(
            "pre_present", task_id=plan.task_id, payload={"kind": "plan", "text": text}
        )
        await self._hooks.emit(
            "post_present",
            task_id=plan.task_id,
            payload={"kind": "plan", "status": "awaiting_approval"},
        )
        return text

    async def gate_plan_execution(
        self,
        *,
        plan: IntentPlan,
        authority: ExecutionAuthority,
        revision: PlanRevision,
        approval_bound_hash: str,
        approved_plan: IntentPlan | None,
        approved_authority: ExecutionAuthority | None,
        mode: str,
        session_id: str,
        persist_message: Any,
        on_tool_event: Any,
        forward: Any,
    ) -> TurnResult | None:
        """Pause a plan that lacks a current, matching execution approval."""
        reason = ""
        if approved_plan is not None and (
            approved_authority is None
            or approved_authority.fingerprint != authority.fingerprint
        ):
            invalidated = {
                "event_type": "plan.approval.invalidated",
                "status": "pending",
                "name": "execution_authority_change",
                "output_json": {
                    "approved_revision_hash": approval_bound_hash,
                    "current_revision_hash": revision.content_hash,
                    "current_revision_id": revision.revision_id,
                    "approved_authority_fingerprint": (
                        approved_authority.fingerprint
                        if approved_authority is not None
                        else ""
                    ),
                    "current_authority_fingerprint": authority.fingerprint,
                },
                "summary": (
                    "plan, contract, catalog, or grants changed after approval; "
                    "renewed approval is required"
                ),
            }
            await self._tasks.append_event(plan.task_id, **invalidated)
            await forward(on_tool_event, invalidated)
            reason = "approval_invalidated"
        elif mode == "plan" and approved_plan is None:
            reason = "awaiting_approval"

        if not reason:
            return None

        text = await self.pause_for_approval(
            plan=plan,
            authority=authority,
            session_id=session_id,
            persist_message=persist_message,
        )
        return TurnResult(
            text=text,
            session_id=session_id,
            task_id=plan.task_id,
            kind="plan",
            terminated_reason=reason,
            plan_summary=plan_summary(plan),
            degraded_warnings=list(plan.degraded_warnings),
            verification_status="pending",
        )

    async def approve(
        self,
        task_id: str,
        *,
        drain_tasks: bool,
        execute: Any,
        authority_for_plan: Any,
        before_execute: Any = None,
    ) -> Any:
        """Execute a persisted plan using its existing parent task."""
        task = await self._tasks.get_task(task_id)
        if task is None:
            raise LookupError(f"task not found: {task_id}")
        if task.status != "awaiting_approval":
            raise ValueError(f"task {task.id[:8]} is not awaiting approval ({task.status})")
        if not isinstance(task.plan_json, dict) or not task.plan_json:
            raise ValueError(f"task {task.id[:8]} has no persisted plan")
        plan = IntentPlan.from_dict(task.plan_json)
        actual_hash = canonical_plan_hash(plan)
        if plan.revision_hash and plan.revision_hash != actual_hash:
            raise ValueError(
                f"task {task.id[:8]} plan revision changed; review it before approval"
            )
        authority = authority_for_plan(plan)
        if (
            not isinstance(authority, ExecutionAuthority)
            or authority.plan_hash != actual_hash
        ):
            raise ValueError(
                f"task {task.id[:8]} execution authority could not be verified"
            )
        if not await self._tasks.claim_plan_approval(
            task.id,
            authority_fingerprint=authority.fingerprint,
            expected_plan_json=task.plan_json,
            approved_tools=list(authority.approval_tools),
        ):
            raise ApprovalConflict(
                f"task {task.id[:8]} approval changed or was already claimed"
            )
        await self._tasks.append_event(
            task.id,
            event_type="plan.approval.bound",
            status="succeeded",
            name="plan",
            output_json={
                "revision": plan.revision,
                "revision_hash": actual_hash,
                **authority.to_dict(),
            },
            summary="approval bound to exact execution authority",
        )
        await self._tasks.append_event(
            task.id,
            event_type="approval.task.granted",
            status="succeeded",
            name="run_approval",
            output_json={
                "approved_tools": list(authority.approval_tools),
                "authority_fingerprint": authority.fingerprint,
                "reason": "user approved persisted plan",
            },
            summary=(
                "task approved for "
                f"{len(authority.approval_tools)} declared sensitive tool(s)"
            ),
        )
        if before_execute is not None:
            await before_execute(plan, authority)
        return await execute(
            task.user_input,
            session_id=task.session_id,
            channel=task.channel,
            drain_tasks=drain_tasks,
            interaction_mode="auto",
            approved_plan=plan,
            approved_authority=authority,
            existing_task_id=task.id,
        )


__all__ = [
    "ApprovalConflict",
    "InteractionLifecycle",
    "TurnStart",
    "apply_interaction_mode",
    "build_approval_gate",
    "normalize_interaction_mode",
    "plan_mode_text",
    "resolve_execution_mode",
]
