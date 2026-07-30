"""Channel abstraction + factory."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from omni.agent import OmniAgent
from omni.config.settings import OmniSettings
from omni.runtime.notifications import TaskNotification, delivery_key
from omni.runtime.presentation import (
    TaskPresentation,
    TurnPresentation,
    task_presentation_from_notification,
    turn_presentation_from_result,
)

logger = logging.getLogger(__name__)


class Channel(ABC):
    name: str = "base"

    def __init__(self, settings: OmniSettings, agent: OmniAgent) -> None:
        self.settings = settings
        self.agent = agent
        self._outbound_locks: dict[str, asyncio.Lock] = {}

    @abstractmethod
    async def start(self) -> None:
        """Run the inbound loop (long-running). Return to stop."""

    async def stop(self) -> None:  # pragma: no cover - trivial
        return None

    async def notify(self, note: TaskNotification) -> None:
        logger.info(
            "[%s] %s %s %s: %s",
            self.name,
            note.object_kind,
            note.reference_id,
            note.status,
            note.summary,
        )

    async def handle_inbound(
        self,
        text: str,
        external_key: str,
        *,
        on_task_ack=None,  # noqa: ANN001
    ) -> TurnPresentation:
        """Run one inbound message through the shared agent and format it.

        Channel turns do not drain tasks inline: ``omni serve`` owns the task
        runtime and will push completions back through ``notify``.
        """
        from omni.channels.security import authorize_channel_message

        auth = authorize_channel_message(self.settings, self.name, external_key, text)
        if not auth.allowed:
            return auth.response or TurnPresentation(assistant_text="This conversation is not paired yet.")
        session_id = await self.agent.ensure_session(
            channel=self.name, external_key=external_key, reuse_latest=True
        )
        from omni.channels.commands import handle_channel_command

        request = text.strip()
        interaction_mode = None
        if request.startswith("/plan "):
            request = request.removeprefix("/plan ").strip()
            interaction_mode = "plan"
        else:
            command_presentation = await handle_channel_command(self.agent, text, session_id)
            if command_presentation is not None:
                return command_presentation
        turn = await self.agent.handle_turn(
            request,
            session_id=session_id,
            channel=self.name,
            drain_tasks=False,
            on_task_ack=on_task_ack,
            interaction_mode=interaction_mode,
        )
        return turn_presentation_from_result(turn, channel=self.name)

    async def handle_inbound_and_send(self, text: str, external_key: str) -> TurnPresentation:
        """Handle one inbound message and send its ACK before later notifications.

        Background tasks can finish while the agent is still turning the submit
        tool result into a user-visible response. Serialising outbound sends per
        external conversation keeps the durable task ACK ahead of completion
        notifications for that same chat.
        """
        async with self._outbound_lock(external_key):
            acknowledged_task_id = ""

            async def send_ack(data: dict) -> None:
                nonlocal acknowledged_task_id
                task_id = str(data.get("task_id") or "")
                if not task_id:
                    return
                acknowledged_task_id = task_id
                presentation = TurnPresentation(
                    assistant_text="",
                    task_id=task_id,
                    ack=f"Request received: `task_id={task_id[:8]}`. Planning...",
                )
                await self._send_task_presentation(
                    external_key,
                    presentation,
                    task_id=task_id,
                    kind="ack",
                )

            try:
                presentation = await self.handle_inbound(text, external_key, on_task_ack=send_ack)
            except Exception as exc:  # noqa: BLE001 - convert an acknowledged turn to a terminal result
                logger.exception(
                    "[%s] inbound turn failed after task acknowledgement task=%s",
                    self.name,
                    acknowledged_task_id[:8],
                )
                presentation = TurnPresentation(
                    assistant_text=(
                        "This request could not complete. The failure was recorded. "
                        + (
                            f"Retry the request or inspect `/task show {acknowledged_task_id[:8]}`."
                            if acknowledged_task_id
                            else "Please retry the request."
                        )
                    ),
                    task_id=acknowledged_task_id,
                )
                if acknowledged_task_id:
                    await self._record_terminal_turn_failure(
                        acknowledged_task_id,
                        presentation.assistant_text,
                        exc,
                    )
            task_id = str(getattr(presentation, "task_id", "") or "")
            if task_id:
                await self._send_task_presentation(
                    external_key,
                    presentation,
                    task_id=task_id,
                    kind="turn",
                )
            else:
                await self.send_turn(external_key, presentation)
            return presentation

    async def _record_terminal_turn_failure(
        self,
        task_id: str,
        user_message: str,
        exc: Exception,
    ) -> None:
        """Persist the failure before the idempotent terminal presentation is sent."""
        recorder = getattr(self.agent, "tasks", None)
        if recorder is None:
            return
        try:
            await recorder.append_event(
                task_id,
                event_type="assistant.message",
                status="failed",
                name="assistant",
                output_json={"text": user_message, "kind": "error", "submitted_subtask_ids": []},
                error=f"{type(exc).__name__}: {exc}",
                summary=user_message[:220],
            )
        except Exception:  # noqa: BLE001 - preserve the user-visible terminal response
            logger.exception("failed to persist terminal turn failure task=%s", task_id[:8])

    async def send_turn(self, external_key: str, presentation: TurnPresentation | TaskPresentation):  # noqa: ANN201
        logger.info("[%s] would send to %s: %s", self.name, external_key, presentation.to_plain_text()[:160])

    async def send_task_notification(self, note: TaskNotification) -> None:
        if note.channel != self.name or not note.external_key:
            return
        from omni.runtime.notifications import record_delivery_status

        async with self._outbound_lock(note.external_key):
            key = delivery_key(
                channel=self.name,
                external_key=note.external_key,
                kind="task_notification",
                object_kind=note.object_kind,
                object_id=note.reference_id,
                state=note.status,
            )
            if not await self._claim_delivery(
                key,
                task_id=note.task_id,
                object_kind=note.object_kind,
                object_id=note.reference_id,
                subtask_id=note.subtask_id if note.object_kind == "skill_execution" else "",
                external_key=note.external_key,
                kind="task_notification",
            ):
                return
            try:
                report = await self.send_turn(note.external_key, task_presentation_from_notification(note))
            except Exception as exc:
                await self._finish_delivery(key, status="failed", error=str(exc))
                record_delivery_status(
                    self.settings.paths.project_dir,
                    note,
                    status="failed",
                    message=str(exc),
                )
                await self._record_task_delivery(note, status="failed", message=str(exc))
                raise
            if report is None:
                await self._finish_delivery(key, status="sent")
                record_delivery_status(self.settings.paths.project_dir, note, status="sent")
                await self._record_task_delivery(note, status="sent")
                return
            failed = bool(getattr(report, "failed", False))
            degraded = bool(getattr(report, "degraded", False))
            status = "failed" if failed else "degraded" if degraded else "sent"
            messages: list[str] = []
            parts = getattr(report, "parts", []) or []
            for part in parts:
                msg = getattr(part, "message", "")
                if msg:
                    messages.append(str(msg))
            record_delivery_status(
                self.settings.paths.project_dir,
                note,
                status=status,
                message="; ".join(messages[:3]),
                report=report.to_dict() if hasattr(report, "to_dict") else {},
            )
            await self._finish_delivery(key, status=status, error="; ".join(messages[:3]))
            await self._record_task_delivery(note, status=status, message="; ".join(messages[:3]))

    async def _send_task_presentation(
        self,
        external_key: str,
        presentation: TurnPresentation | TaskPresentation,
        *,
        task_id: str,
        kind: str,
    ) -> None:
        key = delivery_key(
            channel=self.name,
            external_key=external_key,
            kind=kind,
            task_id=task_id,
        )
        if not await self._claim_delivery(
            key,
            task_id=task_id,
            external_key=external_key,
            kind=kind,
        ):
            return
        try:
            report = await self.send_turn(external_key, presentation)
        except Exception as exc:
            await self._finish_delivery(key, status="failed", error=str(exc))
            await self._record_presentation_event(
                task_id,
                status="failed",
                external_key=external_key,
                kind=kind,
                message=str(exc),
            )
            raise
        status, message = _delivery_outcome(report)
        await self._finish_delivery(key, status=status, error=message)
        await self._record_presentation_event(
            task_id,
            status=status,
            external_key=external_key,
            kind=kind,
            message=message,
        )

    async def _claim_delivery(
        self,
        key: str,
        *,
        external_key: str,
        kind: str,
        task_id: str = "",
        object_kind: str = "",
        object_id: str = "",
        subtask_id: str = "",
    ) -> bool:
        claim = getattr(getattr(self.agent, "tasks", None), "claim_delivery", None)
        if claim is None:
            return True
        return bool(
            await claim(
                key,
                task_id=task_id,
                object_kind=object_kind,
                object_id=object_id,
                subtask_id=subtask_id,
                channel=self.name,
                external_key=external_key,
                kind=kind,
            )
        )

    async def _finish_delivery(self, key: str, *, status: str, error: str = "") -> None:
        finish = getattr(getattr(self.agent, "tasks", None), "finish_delivery", None)
        if finish is not None:
            await finish(key, status=status, error=error)

    async def _record_turn_delivery(self, external_key: str, presentation: TurnPresentation | TaskPresentation, report) -> None:  # noqa: ANN001
        task_id = getattr(presentation, "task_id", "") or ""
        if not task_id:
            return
        failed = bool(getattr(report, "failed", False)) if report is not None else False
        degraded = bool(getattr(report, "degraded", False)) if report is not None else False
        status = "failed" if failed else "degraded" if degraded else "sent"
        await self._record_presentation_event(task_id, status=status, external_key=external_key, kind="turn")

    async def _record_task_delivery(self, note: TaskNotification, *, status: str, message: str = "") -> None:
        task_id = note.task_id
        if not task_id:
            try:
                if note.object_kind == "workflow_run":
                    workflow = await self.agent.runtime.get_workflow_run(note.reference_id)
                    task_id = str(getattr(workflow, "task_id", "") or "")
                else:
                    execution = await self.agent.runtime.get_subtask(note.reference_id)
                    task_id = str(getattr(execution, "task_id", "") or "")
            except Exception:  # noqa: BLE001
                task_id = ""
        if not task_id:
            return
        await self._record_presentation_event(
            task_id,
            status=status,
            external_key=note.external_key,
            kind="task_notification",
            subtask_id=note.subtask_id if note.object_kind == "skill_execution" else "",
            message=message,
        )

    async def _record_presentation_event(
        self,
        task_id: str,
        *,
        status: str,
        external_key: str,
        kind: str,
        subtask_id: str = "",
        message: str = "",
    ) -> None:
        event_type = "presentation.failed" if status == "failed" else "presentation.degraded" if status == "degraded" else "presentation.sent"
        try:
            await self.agent.tasks.append_event(
                task_id,
                event_type=event_type,
                status=status,
                name=self.name,
                subtask_id=subtask_id,
                output_json={
                    "channel": self.name,
                    "external_key": external_key,
                    "kind": kind,
                    "status": status,
                    "message": message,
                },
                error=message if status == "failed" else "",
                summary=f"{self.name} {kind} {status}",
            )
            if kind != "ack":
                await self._settle_after_presentation(task_id, delivery_status=status)
        except Exception:  # noqa: BLE001
            logger.debug("presentation event record failed", exc_info=True)

    async def _settle_after_presentation(
        self,
        task_id: str,
        *,
        delivery_status: str,
    ) -> None:
        """Re-run acceptance checks once a final IM delivery is durable."""
        recorder = getattr(self.agent, "tasks", None)
        if recorder is None:
            return
        task = await recorder.get_task(task_id)
        if task is None or task.status not in {"running", "recovering"}:
            return
        if delivery_status == "failed":
            await recorder.settle_task(
                task_id,
                proposed_status="failed",
                summary="final channel presentation failed",
                error="final channel presentation failed",
            )
            return
        if task.submitted_workflow_ids or task.submitted_subtask_ids:
            await recorder.refresh_from_executions(task_id)
            return
        events = await recorder.list_events(task_id)
        assistant = next(
            (event for event in reversed(events) if event.event_type == "assistant.message"),
            None,
        )
        kind = str((assistant.output_json or {}).get("kind") or "") if assistant else ""
        await recorder.settle_task(
            task_id,
            proposed_status="failed" if kind == "error" else "succeeded",
            summary=str(getattr(assistant, "summary", "") or "channel presentation delivered"),
            error=str(getattr(assistant, "error", "") or "") if kind == "error" else "",
        )

    def _outbound_lock(self, external_key: str) -> asyncio.Lock:
        key = external_key or "-"
        locks = getattr(self, "_outbound_locks", None)
        if locks is None:
            locks = {}
            self._outbound_locks = locks
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock


def build_channels(names: list[str], settings: OmniSettings, agent: OmniAgent) -> list[Channel]:
    from omni.channels.cli_channel import CLIChannel
    from omni.channels.dingtalk import DingTalkChannel
    from omni.channels.feishu import FeishuChannel
    from omni.channels.wechat import WeChatChannel

    registry = {
        "cli": CLIChannel,
        "wechat": WeChatChannel,
        "feishu": FeishuChannel,
        "dingtalk": DingTalkChannel,
    }
    channels: list[Channel] = []
    for n in names:
        cls = registry.get(n)
        if cls is None:
            logger.warning("unknown channel '%s'", n)
            continue
        channels.append(cls(settings, agent))
    return channels


def _delivery_outcome(report) -> tuple[str, str]:  # noqa: ANN001
    if report is None:
        return "sent", ""
    status = (
        "failed"
        if bool(getattr(report, "failed", False))
        else "degraded"
        if bool(getattr(report, "degraded", False))
        else "sent"
    )
    messages = [
        str(message)
        for part in (getattr(report, "parts", []) or [])
        if (message := getattr(part, "message", ""))
    ]
    return status, "; ".join(messages[:3])
