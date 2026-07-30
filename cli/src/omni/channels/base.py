"""Channel abstraction + factory."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import Any

from omni.agent import OmniAgent
from omni.channels.outbound import uploadable_roots
from omni.config.settings import OmniSettings
from omni.runtime.notifications import TaskNotification, delivery_key
from omni.runtime.presentation import (
    TaskPresentation,
    TurnPresentation,
    drop_delivered_attachments,
    inventory_attachment_keys,
    output_inventory,
    task_presentation_from_notification,
    turn_covers_deliverables,
    turn_presentation_from_result,
)

logger = logging.getLogger(__name__)


def _merge_artifact_entries(parent: list[Any], skill: list[Any]) -> list[Any]:
    """Parent task inventory first, then skill-only extras, without duplicates."""
    merged: list[Any] = []
    seen: set[str] = set()
    for entry in [*parent, *skill]:
        keys = _artifact_entry_keys(entry)
        if not keys or any(key in seen for key in keys):
            continue
        seen.update(keys)
        merged.append(entry)
    return merged


def _turn_cover_payload(presentation: TurnPresentation | TaskPresentation, *, kind: str) -> dict[str, Any]:
    """Record which files this send actually uploaded."""
    if kind == "ack":
        return {}
    inventory = output_inventory(presentation)
    return {
        "attachment_count": len(inventory),
        "covers_deliverables": turn_covers_deliverables(presentation),
        "delivered_uris": [item.uri for item in inventory if item.uri],
        "delivered_paths": [item.path for item in inventory if item.path],
    }


def _artifact_entry_keys(entry: Any) -> list[str]:
    if isinstance(entry, dict):
        return [
            str(value)
            for value in (
                entry.get("path"),
                entry.get("uri"),
                entry.get("artifact_uri"),
            )
            if value
        ]
    text = str(entry or "").strip()
    return [text] if text else []


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

    def uploadable_roots(self) -> list[Path]:
        """Directories this channel may send a file from.

        Both an outbound send and the decision of *what to send* need this
        answer, and they have to agree: a reply that lists a file the transport
        then refuses to upload describes an attachment that never arrives.
        """
        return uploadable_roots(
            self.settings, artifacts=getattr(self.agent, "artifacts", None)
        )

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
        return turn_presentation_from_result(
            turn,
            channel=self.name,
            output_roots=self.uploadable_roots(),
        )

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

    async def _located_artifacts(self, note: TaskNotification) -> TaskNotification:
        """Give the notification's artifacts the files they name.

        A completion describes its deliverables the way the rest of the system
        stores them, by ``artifact://`` URI: research-ideation reported one report
        as ``{"uri": …, "kind": "report", "ext": "md"}``. Nothing in that entry is
        a file, so the transport had nothing to upload and fell back to printing
        the URI — an identifier for a store the recipient cannot reach — while the
        report itself sat registered on disk. Only the store can answer where, so
        the answer is filled in here, before anything decides what to send.
        """
        store = getattr(self.agent, "artifacts", None)
        if not hasattr(store, "resolve_path"):
            return note
        payload = note.payload if isinstance(note.payload, dict) else {}
        entries = payload.get("artifacts") or note.artifacts or []
        if not isinstance(entries, list):
            entries = []
        parent = await self._parent_task_artifact_entries(store, note.task_id)
        entries = _merge_artifact_entries(parent, list(entries))
        if not entries:
            return note
        located = [await self._locate_artifact(store, entry) for entry in entries]
        return replace(note, payload={**payload, "artifacts": located})

    async def _parent_task_artifact_entries(
        self, store: object, task_id: str
    ) -> list[dict[str, Any]]:
        """Canonical task outputs — the same list CLI Outputs would print.

        A figure skill's completion payload names the PNG/SVG it just wrote.
        The survey written on the parent turn lives on the task record. Merging
        here is what lets a *pending-child* IM notice carry every file the CLI
        table would, after the turn withheld attachments. A turn that already
        attached those files must not get this notice at all.
        """
        if not task_id or not hasattr(store, "list_by_task"):
            return []
        try:
            from omni.agent.turn_execution import artifact_output_refs

            rows = await store.list_by_task(task_id)  # type: ignore[attr-defined]
            refs = await artifact_output_refs(store, rows)
        except Exception:  # noqa: BLE001 - a store hiccup must not drop the notice
            logger.warning("[%s] could not list parent artifacts for task %s", self.name, task_id[:8])
            return []
        return [
            {
                "title": ref.title,
                "format": ref.format,
                "uri": ref.uri,
                "path": ref.path,
                "mime": ref.mime,
                "size_bytes": ref.size_bytes,
                "presentation_role": ref.presentation_role,
            }
            for ref in refs
        ]

    async def _locate_artifact(self, store: object, entry: object) -> dict:
        """One artifact entry, with its local file and size filled in."""
        record = dict(entry) if isinstance(entry, dict) else {"uri": str(entry)}
        uri = str(record.get("uri") or record.get("artifact_uri") or "")
        if not uri or record.get("path"):
            return record
        try:
            path = await store.resolve_path(uri)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a missing file must not stop the news
            logger.warning("[%s] could not locate artifact %s", self.name, uri)
            return record
        if path is None:
            return record
        found = {"path": str(path)}
        if not record.get("format"):
            found["format"] = str(record.get("ext") or path.suffix.lstrip("."))
        if not record.get("size_bytes"):
            try:
                found["size_bytes"] = path.stat().st_size
            except OSError:
                pass
        return {**record, **found}

    async def send_task_notification(self, note: TaskNotification) -> str:
        """Deliver one completion notification, returning its delivery status.

        The status is what a replay of a queued failure needs in order to stop
        retrying; ``""`` means another worker already owns this delivery.
        """
        if note.channel != self.name or not note.external_key:
            return ""
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
            located = await self._located_artifacts(note)
            presentation = task_presentation_from_notification(located, channel=self.name)
            delivered = await self._delivered_attachment_keys(note)
            if delivered:
                presentation = drop_delivered_attachments(presentation, delivered)
            if await self._skill_notice_covered_by_turn(
                note, presentation=presentation, delivered=delivered
            ):
                return await self._suppress_covered_skill_notice(note, key)
            if not await self._claim_delivery(
                key,
                task_id=note.task_id,
                object_kind=note.object_kind,
                object_id=note.reference_id,
                subtask_id=note.subtask_id if note.object_kind == "skill_execution" else "",
                external_key=note.external_key,
                kind="task_notification",
            ):
                return ""
            try:
                report = await self.send_turn(
                    note.external_key,
                    presentation,
                )
            except Exception as exc:
                await self._finish_delivery(key, status="failed", error=str(exc))
                record_delivery_status(
                    self.settings.paths.project_dir,
                    note,
                    status="failed",
                    message=str(exc),
                )
                await self._record_task_delivery(
                    note,
                    status="failed",
                    message=str(exc),
                    extra=_turn_cover_payload(presentation, kind="task_notification"),
                )
                raise
            if report is None:
                await self._finish_delivery(key, status="sent")
                record_delivery_status(self.settings.paths.project_dir, note, status="sent")
                await self._record_task_delivery(
                    note,
                    status="sent",
                    extra=_turn_cover_payload(presentation, kind="task_notification"),
                )
                return "sent"
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
            await self._record_task_delivery(
                note,
                status=status,
                message="; ".join(messages[:3]),
                extra=_turn_cover_payload(presentation, kind="task_notification"),
            )
            return status

    async def _delivered_attachment_keys(self, note: TaskNotification) -> set[str]:
        lookup = getattr(getattr(self.agent, "tasks", None), "delivered_attachment_keys", None)
        if lookup is None or not note.task_id:
            return set()
        try:
            return set(
                await lookup(
                    note.task_id,
                    channel=self.name,
                    external_key=note.external_key,
                )
            )
        except Exception:  # noqa: BLE001 - a cover lookup must not drop a real notice
            logger.debug("delivered-key lookup failed; sending skill notice", exc_info=True)
            return set()

    async def _skill_notice_covered_by_turn(
        self,
        note: TaskNotification,
        *,
        presentation: TaskPresentation,
        delivered: set[str],
    ) -> bool:
        """True when every file this notice would send was already uploaded.

        A text-only notice after a parent that already attached files is the
        second English card Codex would not show. A notice that still has a
        new file (the PPTX the parent never sent) must go out.
        """
        if note.object_kind not in {"skill_execution", "workflow_run"}:
            return False
        if not note.task_id:
            return False
        notice_keys = inventory_attachment_keys(presentation)
        if not notice_keys:
            return bool(delivered)
        return notice_keys <= delivered

    async def _suppress_covered_skill_notice(self, note: TaskNotification, key: str) -> str:
        """Settle the notice as sent without a second chat bubble or re-upload."""
        from omni.runtime.notifications import record_delivery_status

        if not await self._claim_delivery(
            key,
            task_id=note.task_id,
            object_kind=note.object_kind,
            object_id=note.reference_id,
            subtask_id=note.subtask_id if note.object_kind == "skill_execution" else "",
            external_key=note.external_key,
            kind="task_notification",
        ):
            return ""
        message = "suppressed: parent turn already delivered the files"
        await self._finish_delivery(key, status="sent", error=message)
        record_delivery_status(
            self.settings.paths.project_dir,
            note,
            status="sent",
            message=message,
        )
        await self._record_task_delivery(note, status="sent", message=message)
        return "sent"

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
                extra=_turn_cover_payload(presentation, kind=kind),
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
            extra=_turn_cover_payload(presentation, kind=kind),
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

    async def _record_task_delivery(
        self,
        note: TaskNotification,
        *,
        status: str,
        message: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
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
            extra=extra,
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
        extra: dict[str, Any] | None = None,
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
                    **(extra or {}),
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
            # The work is done and stored; what failed is the last hop to the
            # reader. Calling that a failed task says the opposite of what
            # happened — 22589d2c produced a figure in five formats and reads
            # "failed" — and status is what a reader checks to decide whether
            # anything exists to collect. ``degraded`` is this record's own word
            # for a run that finished with a piece missing, and aggregation still
            # lets a genuinely failed execution outrank it.
            await recorder.settle_task(
                task_id,
                proposed_status="degraded",
                summary="result stored; the chat channel could not deliver it",
            )
            return
        if task.submitted_workflow_ids or task.submitted_subtask_ids:
            await recorder.refresh_from_executions(task_id)
            task = await recorder.get_task(task_id)
            if task is None or task.status not in {"running", "recovering"}:
                return
            # Children are still in flight; the parent stays open until they
            # finish. Do not guess succeeded from the assistant prose.
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
