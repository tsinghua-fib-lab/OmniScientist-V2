"""Headless scheduled-goal execution — "one brain, headless door".

A due *goal* schedule runs the same planner→workflow→verification pipeline an
interactive turn uses, unattended. This collaborator owns that flow so the
orchestrator stays a thin coordinator:

* run the full pipeline under the schedule's pre-created owning task (which
  carries the unattended tool grant), with ``origin="schedule"`` — no
  interactive approver, and no self-scheduling (recursion guard);
* a run that finishes short (degraded/failed) triggers a bounded
  auto-continuation that finishes only the missing deliverables; and
* the verified outcome — or an honest error — is always delivered to the origin
  channel's inbox, and the schedule's observability "last run" is bound to the
  turn's newest subtask.

The deployed regression this fixes: a due goal used to run as a single bounded
``agent-goal`` ReAct loop that hit an iteration cliff after one deliverable and
returned degraded — a different code path from the interactive turn. Here it is
the *same* pipeline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from omni.agent.turn_execution import TurnResult
from omni.runtime.notifications import TaskNotification
from omni.runtime.presentation import ArtifactRef
from omni.runtime.task_status import await_settled_status, is_terminal
from omni.scheduling.contracts import GOAL_SKILL

if TYPE_CHECKING:  # avoid an import cycle: the orchestrator constructs us
    from omni.agent.orchestrator import OmniAgent

logger = logging.getLogger(__name__)

# The verifier's terminal vocabulary a short run maps to (a bounded finish-the-
# job continuation is worth trying); ``passed``/``skipped`` are done and
# ``needs_input`` cannot be answered unattended.
_CONTINUABLE = {"degraded", "failed"}

# Verifier status → user-facing scheduled-run status. Only a *fallback* now: the
# settled task row (see ``await_settled_status``) is authoritative. ``pending`` is
# deliberately absent — an unsettled run must never be reported as ``degraded``;
# it falls through to the kind-based default below.
_STATUS_MAP = {
    "passed": "succeeded",
    "degraded": "degraded",
    "failed": "failed",
    "needs_input": "needs_input",
    "skipped": "degraded",
}


class ScheduledGoalRunner:
    """Executes one due scheduled goal as a full headless orchestrator turn."""

    def __init__(self, agent: OmniAgent) -> None:
        self._agent = agent

    async def run(
        self,
        *,
        goal: str,
        task_id: str,
        channel: str = "cli",
        session_id: str = "",
        schedule_id: str = "",
        approved_tools: list[str] | None = None,
    ) -> TurnResult | None:
        """Run one due scheduled goal end to end. Never raises.

        An unattended run is always accounted for: on success/degraded it delivers
        the verified outcome, on a crash it delivers an honest error note. Returns
        the final :class:`TurnResult`, or ``None`` if the run crashed.
        """
        agent = self._agent
        await agent.setup()
        grant = list(approved_tools or [])
        if not grant and task_id:
            existing = await agent.tasks.get_task(task_id)
            grant = list(getattr(existing, "approved_tools", None) or [])
        try:
            result = await self._run_turn(
                goal=goal, task_id=task_id, channel=channel, session_id=session_id, grant=grant
            )
        except Exception as exc:  # noqa: BLE001 — an unattended run must never vanish silently
            logger.exception("scheduled goal turn failed task=%s", (task_id or "-")[:8])
            await self._deliver(
                None, goal=goal, channel=channel, session_id=session_id,
                schedule_id=schedule_id, task_id=task_id, error=str(exc),
            )
            return None
        result = await self._maybe_continue(
            result, goal=goal, channel=channel, session_id=session_id,
            schedule_id=schedule_id, grant=grant,
        )
        # Bind the schedule's observability "last run" to the turn's newest
        # subtask so ``schedule show``/``list`` render real execution facts.
        if schedule_id:
            try:
                await agent.scheduler.bind_last_run(schedule_id, result.task_id or task_id)
            except Exception:  # noqa: BLE001 - observability binding is best-effort
                logger.debug("bind_last_run failed schedule=%s", schedule_id, exc_info=True)
        await self._deliver(
            result, goal=goal, channel=channel, session_id=session_id,
            schedule_id=schedule_id, task_id=result.task_id or task_id,
        )
        return result

    async def _run_turn(
        self, *, goal: str, task_id: str, channel: str, session_id: str, grant: list[str]
    ) -> TurnResult:
        """Run one headless turn under a pre-created owning task carrying ``grant``."""
        agent = self._agent
        # Expose the task's granted sensitive tools to the ReAct policy for this
        # turn so a scheduled run can actually use write_file/edit_file/…; the
        # approval gate still clears them only via the task-grant preauthorizer
        # (autonomous ctx ⇒ no interactive prompt), and ungranted tools stay blocked.
        if grant and task_id:
            from omni.core.approval import SENSITIVE_TOOLS

            agent._approved_task_tools[task_id] = set(grant) & set(SENSITIVE_TOOLS)
        return await agent.handle_turn(
            goal,
            session_id=session_id,
            channel=channel or "cli",
            drain_tasks=True,
            existing_task_id=task_id,
            origin="schedule",
        )

    @staticmethod
    def _incomplete(result: TurnResult | None) -> bool:
        return result is not None and result.settlement_status in _CONTINUABLE

    @staticmethod
    def _missing(result: TurnResult | None) -> list[str]:
        if result is None:
            return []
        return [w for w in (result.degraded_warnings or []) if w][:6]

    @staticmethod
    def _continuation_goal(goal: str, missing: list[str]) -> str:
        note = (
            "\n\n[Unattended continuation] A prior automated run of this goal finished "
            "incomplete. Produce ONLY the parts that are not yet complete; do not redo "
            "work that was already delivered. Finish the full goal."
        )
        if missing:
            note += "\nOutstanding: " + "; ".join(missing)
        return goal + note

    async def _maybe_continue(
        self,
        result: TurnResult,
        *,
        goal: str,
        channel: str,
        session_id: str,
        schedule_id: str,
        grant: list[str],
    ) -> TurnResult:
        """Enqueue up to ``schedules.max_continuations`` bounded finish-the-job turns."""
        agent = self._agent
        cfg = getattr(agent.settings, "schedules", None)
        if not bool(getattr(cfg, "auto_continue", True)):
            return result
        max_cont = max(0, int(getattr(cfg, "max_continuations", 1) or 0))
        attempt = 0
        accumulated_artifacts = list(result.artifacts)
        while attempt < max_cont and self._incomplete(result):
            attempt += 1
            missing = self._missing(result)
            cont_goal = self._continuation_goal(goal, missing)
            cont_task = await agent.tasks.create_task(
                session_id=session_id,
                channel=channel or "cli",
                user_input=cont_goal,
                title=("continue: " + goal)[:80],
                kind="turn",
                schedule_id=schedule_id,
            )
            if grant:
                await agent.tasks.grant_tools(
                    cont_task.id, grant, reason=f"scheduled continuation {attempt}"
                )
            await agent.tasks.append_event(
                cont_task.id,
                event_type="schedule.continuation",
                status="running",
                name="continuation",
                output_json={"attempt": attempt, "schedule_id": schedule_id, "missing": missing},
                summary=f"auto-continuation {attempt} for missing deliverables",
            )
            result = await self._run_turn(
                goal=cont_goal, task_id=cont_task.id, channel=channel, session_id=session_id, grant=grant
            )
            accumulated_artifacts = self._merge_artifacts(
                accumulated_artifacts,
                result.artifacts,
            )
            result.artifacts = accumulated_artifacts
        return result

    @staticmethod
    def _merge_artifacts(
        existing: list[ArtifactRef],
        added: list[ArtifactRef],
    ) -> list[ArtifactRef]:
        merged: list[ArtifactRef] = []
        seen: set[str] = set()
        for artifact in [*existing, *added]:
            key = artifact.uri or artifact.path
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            merged.append(artifact)
        return merged

    async def _deliver(
        self,
        result: TurnResult | None,
        *,
        goal: str,
        channel: str,
        session_id: str,
        schedule_id: str,
        task_id: str = "",
        error: str = "",
    ) -> None:
        """Deliver a verification-honest scheduled-run report to the origin inbox."""
        if result is None:
            status = "failed"
            summary = (error or "The scheduled run failed before producing a result.")[:2000]
            verification = "failed"
            warnings: list[str] = []
        else:
            # R1: the turn result can carry a transient (``pending``) verification
            # captured before the verifier settled. Re-read the durable task row so
            # a passing run is never mislabeled ``degraded`` in the inbox.
            settled_status, _settled = await await_settled_status(
                self._agent.tasks, result.task_id or task_id
            )
            if is_terminal(settled_status):
                status = settled_status
            else:
                status = _STATUS_MAP.get(
                    result.settlement_status,
                    "succeeded" if result.kind == "text" else "degraded",
                )
            summary = (result.text or "")[:2000]
            verification = result.settlement_status
            warnings = list(result.degraded_warnings or [])
        artifacts = list(result.artifacts) if result is not None else []
        note = TaskNotification(
            subtask_id=task_id or "",
            task_id=task_id or "",
            skill_name=GOAL_SKILL,
            status=status,
            object_kind="task" if task_id else "scheduled_goal",
            object_id=task_id or schedule_id or "",
            channel=channel or "cli",
            session_id=session_id,
            title=(goal or "Scheduled goal")[:80],
            summary=summary,
            artifacts=[
                artifact.uri or artifact.path
                for artifact in artifacts
                if artifact.uri or artifact.path
            ],
            payload={
                "schedule_id": schedule_id,
                "settlement_status": verification,
                "degraded_warnings": warnings,
                "artifacts": [artifact.to_dict() for artifact in artifacts],
            },
        )
        try:
            await self._agent.notifier.notify(note)
        except Exception:  # noqa: BLE001 — durability of the run does not depend on delivery
            logger.exception("scheduled goal delivery failed schedule=%s", schedule_id)


__all__ = ["ScheduledGoalRunner"]
