"""Plan/review interaction modes and approval lifecycle coordination."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from omni.agent.intent_plan import IntentPlan, IntentType, ToolPolicy
from omni.agent.plan_revision import (
    ExecutionAuthority,
    PlanRevision,
    canonical_plan_hash,
)
from omni.agent.plan_runner_utils import plan_summary
from omni.agent.turn_execution import TurnResult
from omni.channels.security import is_im_channel
from omni.core.approval import (
    SENSITIVE_TOOLS,
    ApprovalGate,
    reconcile_sensitive_visibility,
    resolve_policy,
)
from omni.core.approval_rules import SessionApprovalStore

_REVIEW_BLOCKED_TOOLS = {
    "bash",
    "write_file",
    "edit_file",
    "run_compute",
    "run_skill",
    "run_workflow",
    "submit_task",
}

# Plan-only: same ReAct turn, host denies mutating / retrieval side effects.
# Codex ModeKind::Plan and Kimi PlanModeGuardDeny are this shape — not a
# second semantic planner LLM.
_PLAN_BLOCKED_TOOLS = {
    *_REVIEW_BLOCKED_TOOLS,
    "spawn_subagents",
    "search_literature",
    "search_corpus",
    "cite_source",
    "record_claim",
    "add_evidence",
    "record_hypothesis",
    "remember",
    "log_run",
    "record_run",
    "build_research_artifact",
}


class ApprovalConflict(ValueError):
    """The reviewed execution authority changed or was claimed elsewhere."""


def normalize_interaction_mode(value: str | None, default: str = "auto") -> str:
    """Return one supported interaction mode, falling back to automatic."""
    mode = str(value or default or "auto").strip().lower()
    return mode if mode in {"auto", "plan", "review"} else "auto"


def resolve_execution_mode(value: Any, *, wait_for_tasks: bool, is_async: bool) -> str:
    """Resolve automatic skill delivery to inline, foreground, or background.

    ``background`` defers a skill to the end-of-turn drain rather than running it
    alongside the turn, so on a turn that drains, asking for it buys nothing and
    costs the model its result: the work cannot start until the loop is over, so
    every poll reports ``pending`` and the answer gets written as "still
    generating" about something that finishes moments later. Task aac5b285 ends
    exactly that way — a figure reported as in progress by prose sitting directly
    above the finished figure's artifacts.

    So the distinction is honoured where it is real. When nobody waits (daemon
    and IM turns, ``wait_for_tasks`` false) a submission genuinely outlives the
    turn — even if the model asked for ``foreground``. Waiting there holds the
    WeChat outbound lock across ``process()``, and hop 2 then waits for the
    same lock. When the turn does wait, the same work happens either way and
    running it now is what lets the model describe what actually happened —
    the shape Codex keeps by having a tool call return a real result unless
    the process truly outlives it.
    """
    mode = str(value or "auto").lower().strip()
    if mode not in {"auto", "inline", "foreground", "background"}:
        mode = "auto"
    if mode == "background" and wait_for_tasks:
        return "foreground"
    if mode == "foreground" and not wait_for_tasks:
        return "background"
    if mode != "auto":
        return mode
    if not is_async:
        return "inline"
    return "foreground" if wait_for_tasks else "background"


def enqueue_notify_channel(channel: str, *, mode: str, wait_for_tasks: bool) -> str:
    """Channel that must receive the completion notice after this enqueue.

    Background always notifies. A detached turn coerces ``foreground`` to
    ``background`` first, so hop 2 is the file card after the inbound send
    lock drops. The leftover ``foreground and not wait`` branch is only a
    guard if something bypasses that coerce. CLI foreground waits in-turn,
    so the hop is the turn itself.
    """
    if mode == "background" or (mode == "foreground" and not wait_for_tasks):
        return str(channel or "")
    return ""


def build_approval_gate(
    *,
    settings: Any,
    tasks: Any,
    approver: Any,
    session_allow: dict[str, SessionApprovalStore],
    task_id: str,
    channel: str,
    session_id: str,
    additional_sensitive_tools: set[str] | None = None,
    writable_roots: Sequence[Path] | None = None,
    output_roots: Sequence[Path] | None = None,
    working_dir: Path | None = None,
    workspace: Path | None = None,
    workspace_auto: bool = False,
) -> ApprovalGate:
    """Build a sensitive-tool gate whose decisions are persisted as task events."""
    allow = session_allow.setdefault(session_id, SessionApprovalStore())

    async def on_event(kind: str, payload: dict[str, Any]) -> None:
        # Let audit failures reach ApprovalGate._emit. Exact grants still
        # publish; a wide workspace grant does not, because an unaudited
        # "approve all" is worse than asking again.
        await tasks.append_event(
            task_id,
            event_type=kind,
            status="succeeded",
            name=str(payload.get("tool") or "approval"),
            tool_name=str(payload.get("tool") or ""),
            output_json=payload,
            summary=str(payload.get("summary") or kind),
        )
        if (
            kind == "approval.granted"
            and payload.get("approval_scope") == "task-bash-grant"
        ):
            grant = getattr(tasks, "grant_tools", None)
            if grant is None:
                return
            try:
                await grant(
                    task_id,
                    sorted(SENSITIVE_TOOLS),
                    reason="task-workspace",
                )
            except Exception:  # noqa: BLE001 — live grant still publishes.
                return

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
        writable_roots=writable_roots,
        output_roots=output_roots,
        working_dir=working_dir,
        workspace=workspace,
        task_id=task_id,
        workspace_auto=workspace_auto and not is_im_channel(channel),
    )


def react_tool_policy(
    policy: ToolPolicy,
    *,
    settings: Any,
    approver: Any,
    approved: set[str] | frozenset[str] = frozenset(),
    channel: str = "",
    read_only: bool = False,
    workspace_auto: bool = False,
) -> ToolPolicy:
    """Narrow a plan's deny-list to the sensitive tools the gate cannot clear.

    The planner keeps declaring bash/write_file/edit_file/run_compute blocked
    (the plan record stays deny-by-default); the approval gate governs them at
    execution, so the catalog should carry exactly those the gate could actually
    settle. This gathers the security facts and lets
    ``reconcile_sensitive_visibility`` state the rule, so the decision lives in
    one place instead of being kept in step in two.

    A write is settleable without a human because it names its destination and
    one inside the workspace auto-approves. That does not hold under ``always``,
    which asks about everything.

    An IM turn is not the exception it used to be. Withholding the write tools
    from the catalog entirely, on the grounds that they refuse in their own body
    anyway, left a chat request that needs to produce a document with nowhere to
    put it: the model asked for ``write_file``, was told no such tool exists, and
    delivered a whole paper as chat text. Codex settles a write by destination
    (``assess_patch_safety``); generating a file inside the turn workspace is a
    basic agent capability on CLI and IM alike. Escaping that workspace is still
    refused. The catalog has to offer the tool for that distinction to be reached.

    ``read_only`` short-circuits all of it. A deny-list mixes two reasons for the
    same entry — "sensitive, pending consent" and "this turn does not get to
    mutate anything" — and only the first is the gate's to reconsider. Review
    mode means the second, so no amount of available consent may hand it a write.
    """
    blocked = list(getattr(policy, "blocked_tools", None) or [])
    if read_only or not any(t in blocked for t in SENSITIVE_TOOLS):
        return policy
    effective = resolve_policy(settings)
    remaining = reconcile_sensitive_visibility(
        blocked,
        gate_can_clear=(
            effective == "never"
            or approver is not None
            or (workspace_auto and not is_im_channel(channel))
        ),
        approved=approved,
        path_assessed_can_clear=effective != "always"
        or (workspace_auto and not is_im_channel(channel)),
    )
    if remaining == blocked:
        return policy
    return replace(policy, blocked_tools=remaining)


def unblock_produce_tools(policy: ToolPolicy, plan: IntentPlan) -> ToolPolicy:
    """Offer write_file/edit_file when this plan still owes a file deliverable.

    The planner deny-list is conservative. A manuscript/figure/slides contract
    is not answer-only: the model must be able to produce the file. bash and
    run_compute stay gated.
    """
    from omni.runtime.remaining import plan_owes_scientific_outputs

    if not plan_owes_scientific_outputs(plan):
        return policy
    remaining = [
        name
        for name in (policy.blocked_tools or [])
        if name not in {"write_file", "edit_file"}
    ]
    if remaining == list(policy.blocked_tools or []):
        return policy
    return replace(policy, blocked_tools=remaining)


def apply_interaction_mode(plan: IntentPlan, mode: str) -> IntentPlan:
    """Apply host tool policy for review (read-only) or plan (no execution)."""
    if mode == "review":
        return _apply_mode_blocks(
            plan,
            blocked=_REVIEW_BLOCKED_TOOLS,
            execution_mode="review",
            outputs=["review"],
            note="review mode: read-only execution with mandatory self-review",
        )
    if mode == "plan":
        return _apply_mode_blocks(
            plan,
            blocked=_PLAN_BLOCKED_TOOLS,
            execution_mode="plan",
            outputs=list(plan.outputs) or ["answer"],
            note="plan mode: same turn, host denies mutating and retrieval tools",
        )
    return plan


def _apply_mode_blocks(
    plan: IntentPlan,
    *,
    blocked: set[str],
    execution_mode: str,
    outputs: list[str],
    note: str,
) -> IntentPlan:
    policy = ToolPolicy(
        allowed_tools=(
            [name for name in plan.tool_policy.allowed_tools if name not in blocked]
            if plan.tool_policy.allowed_tools is not None
            else None
        ),
        blocked_tools=sorted({*plan.tool_policy.blocked_tools, *blocked}),
        per_tool_limits=dict(plan.tool_policy.per_tool_limits),
        max_tool_calls=plan.tool_policy.max_tool_calls,
        max_iterations=plan.tool_policy.max_iterations,
        final_reserve_enabled=plan.tool_policy.final_reserve_enabled,
    )
    return replace(
        plan,
        intent_type=IntentType.REACT_FALLBACK,
        execution_mode=execution_mode,
        outputs=outputs,
        tool_policy=policy,
        rationale=(plan.rationale + f"; {note}").strip("; "),
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
        file_uris: list[str] | None = None,
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
            # Resumes (plan approval, /task attach, recovery) still surface the
            # owning task id to the caller so the live display keeps it visible —
            # a resumed turn must never look "id-less". Headless callers pass a
            # ``None`` ack and are unaffected.
            if on_task_ack is not None:
                ack_result = on_task_ack(
                    {"task_id": task_id, "session_id": session_id, "status": "resuming"}
                )
                if inspect.isawaitable(ack_result):
                    await ack_result
            event_name = "run_resume"
        else:
            session_id = session_id or await ensure_session(channel=channel)
            task_id = await self._run_controller.create_turn_task(
                session_id=session_id,
                channel=channel,
                user_input=user_message,
                file_uris=file_uris,
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
            settlement_status="pending",
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
    "react_tool_policy",
    "enqueue_notify_channel",
    "resolve_execution_mode",
    "unblock_produce_tools",
]
