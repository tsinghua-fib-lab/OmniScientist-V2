"""Start, watch, steer, and cancel web turns.

``turn.start`` admits a background ``handle_turn`` and returns identifiers.
Observation is ``task.watch`` / ``task.events``. Closing the watch stream does
not cancel the turn.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import select
from starlette.responses import StreamingResponse

from omni.agent import OmniAgent
from omni.channels.commands import handle_channel_command
from omni.runtime.daemon import pid_alive
from omni.runtime.execution_ownership import reconcile_lost_executors
from omni.runtime.presentation import turn_presentation_from_result
from omni.storage.models import SubtaskORM
from omni.web import activity as activitymod
from omni.web.attachments import bind_web_attachments
from omni.web.protocol import RpcError, jsonable
from omni.web.runs import SERVER_STOPPING_EVENT, RunHandle
from omni.web.workspace import OpenedWorkspace, WorkspaceHub


def _sse(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps({"ok": True, **jsonable(data)}, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()


def _sse_error(code: str, message: str) -> bytes:
    payload = json.dumps({"ok": False, "error": {"code": code, "message": message}})
    return f"event: error\ndata: {payload}\n\n".encode()


async def resolve_channel(agent: OmniAgent, session_id: str | None, default: str) -> str:
    """Keep an existing session's channel; only new chats use ``web``."""
    if not session_id:
        return default
    row = await agent.get_session(session_id)
    if row is None:
        raise RpcError("not_found", f"session not found: {session_id}")
    return row.channel or default


async def start_turn(
    hub: WorkspaceHub,
    rec: OpenedWorkspace,
    *,
    text: str,
    session_id: str | None,
    interaction_mode: str | None,
    file_uris: list[str] | None,
    client_run_id: str = "",
) -> dict[str, Any]:
    agent = await hub.agent_for(rec)
    channel = await resolve_channel(agent, session_id, "web")

    if text.lstrip().startswith("/"):
        sid = session_id or await agent.ensure_session(channel="web")
        presentation = await handle_channel_command(agent, text, sid)
        if presentation is not None:
            return {
                "session_id": presentation.session_id or sid,
                "task_id": presentation.task_id or "",
                "client_run_id": "",
                "channel": channel,
                "kind": "command",
                "markdown": presentation.to_markdown(),
            }

    text, file_uris = bind_web_attachments(
        text, file_uris, cwd=Path(rec.open_path)
    )

    handle = await hub.runs.admit(
        rec,
        session_id=session_id or "",
        client_run_id=client_run_id,
    )
    acked = asyncio.Event()

    def on_token(piece: str) -> None:
        if piece:
            handle.append_partial(piece)
            handle.publish("token", {"text": piece})

    def on_tool_event(phase: str, data: Any = None) -> None:
        handle.publish("tool", {"phase": phase, "event": jsonable(data)})

    def on_task_ack(info: dict[str, Any]) -> None:
        sid = str(info.get("session_id") or session_id or "")
        tid = str(info.get("task_id") or "")
        hub.runs.bind(handle, session_id=sid, task_id=tid)
        handle.publish("ack", dict(info))
        acked.set()

    async def run() -> None:
        try:
            result = await agent.handle_turn(
                text,
                session_id=session_id,
                channel=channel,
                file_uris=file_uris,
                drain_tasks=hub.drain_tasks(rec),
                on_token=on_token,
                on_tool_event=on_tool_event,
                on_task_ack=on_task_ack,
                interaction_mode=interaction_mode,
            )
            presentation = turn_presentation_from_result(result, channel=channel)
            payload = {
                "session_id": result.session_id,
                "task_id": result.task_id,
                "channel": channel,
                "kind": result.kind,
                "settlement_status": result.settlement_status,
                "submitted_workflow_ids": result.submitted_workflow_ids,
                "submitted_subtask_ids": result.submitted_subtask_ids,
                "markdown": presentation.to_markdown(),
            }
            handle.result = payload
            handle.publish("presentation", payload)
            handle.publish("done", payload)
        except asyncio.CancelledError:
            handle.error = {"code": "interrupted", "message": "web process is shutting down"}
            handle.publish("error", handle.error)
            raise
        except RpcError as exc:
            handle.error = {"code": exc.code, "message": exc.message}
            handle.publish("error", handle.error)
        except Exception as exc:  # noqa: BLE001 — surface the turn failure to the SPA
            handle.error = {"code": "turn_failed", "message": str(exc)}
            handle.publish("error", handle.error)
        finally:
            hub.runs.finish(handle)

    handle.task = asyncio.create_task(run(), name=f"web-turn-{handle.client_run_id[:8]}")
    ack_wait = asyncio.create_task(acked.wait())
    try:
        await asyncio.wait(
            {ack_wait, handle.task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not ack_wait.done():
            ack_wait.cancel()
            try:
                await ack_wait
            except asyncio.CancelledError:
                pass

    if handle.error and not handle.task_id:
        raise RpcError(handle.error["code"], handle.error["message"])
    if not handle.task_id and handle.task.done() and handle.error:
        raise RpcError(handle.error["code"], handle.error["message"])

    return {
        "session_id": handle.session_id or session_id or "",
        "task_id": handle.task_id,
        "client_run_id": handle.client_run_id,
        "channel": channel,
        "kind": "turn",
    }


async def _replay_chunks(
    agent: OmniAgent, task_id: str, after_seq: int
) -> tuple[list[bytes], int]:
    rows = await activitymod.project_events_after(agent, task_id, after_seq=after_seq)
    chunks = [_sse("activity", item) for item in rows]
    last = int(rows[-1]["seq"]) if rows else after_seq
    return chunks, last


async def _drain_replay_chunks(
    agent: OmniAgent, task_id: str, after_seq: int
) -> AsyncIterator[bytes]:
    """Yield every currently durable event, including batches past the cap."""
    last = after_seq
    while True:
        chunks, next_last = await _replay_chunks(agent, task_id, last)
        if not chunks:
            return
        last = next_last
        for chunk in chunks:
            yield chunk


def _terminal_bytes(handle: RunHandle) -> bytes:
    if handle.error:
        return _sse_error(handle.error["code"], handle.error["message"])
    if handle.result:
        return _sse("done", handle.result)
    return _sse("done", {"session_id": handle.session_id, "task_id": handle.task_id})


def _server_stopping_bytes(task_id: str) -> bytes:
    return _sse(
        SERVER_STOPPING_EVENT,
        {"state": "stopping", "task_id": task_id},
    )


async def watch_task_sse(
    hub: WorkspaceHub,
    rec: OpenedWorkspace,
    agent: OmniAgent,
    *,
    task_id: str,
    after_seq: int = 0,
) -> StreamingResponse:
    task = await agent.tasks.get_task(task_id)
    if task is None:
        raise RpcError("not_found", f"task not found: {task_id}")

    async def events() -> AsyncIterator[bytes]:
        last = max(0, int(after_seq or 0))
        chunks, last = await _replay_chunks(agent, task_id, last)
        for chunk in chunks:
            yield chunk
        handle = hub.runs.by_task(rec.key, task_id)
        if handle is None:
            # ``RunManager`` owns web turns in this process only. CLI, IM, and
            # a web turn surviving in another process have no local handle but
            # still publish durable task events into the shared workspace DB.
            # Absence from this in-memory registry is therefore not evidence of
            # a lost worker: keep following the durable stream until execution
            # leaves its active state.
            status = str(task.status or "")
            if status not in activitymod.FOLLOWABLE_TASK_STATUSES:
                async for chunk in _drain_replay_chunks(agent, task_id, last):
                    yield chunk
                yield _sse(
                    "worker",
                    {"state": status, "task_id": task_id, "status": status},
                )
                yield _sse(
                    "done",
                    {
                        "session_id": task.session_id or "",
                        "task_id": task_id,
                        "kind": task.kind,
                        "settlement_status": status,
                        "worker": status,
                    },
                )
                return
            yield _sse(
                "worker",
                {
                    "state": "external",
                    "task_id": task_id,
                    "status": status,
                },
            )
            while True:
                if hub.is_shutting_down:
                    yield _server_stopping_bytes(task_id)
                    return
                chunks, last = await _replay_chunks(agent, task_id, last)
                for chunk in chunks:
                    yield chunk
                refreshed = await agent.tasks.get_task(task_id)
                if refreshed is None:
                    yield _sse_error("not_found", f"task not found: {task_id}")
                    return
                status = str(refreshed.status or "")
                if status not in activitymod.FOLLOWABLE_TASK_STATUSES:
                    async for chunk in _drain_replay_chunks(agent, task_id, last):
                        yield chunk
                    yield _sse(
                        "worker",
                        {"state": status, "task_id": task_id, "status": status},
                    )
                    yield _sse(
                        "done",
                        {
                            "session_id": refreshed.session_id or "",
                            "task_id": task_id,
                            "kind": refreshed.kind,
                            "settlement_status": status,
                            "worker": status,
                        },
                    )
                    return
                if await hub.wait_for_shutdown(timeout=0.45):
                    yield _server_stopping_bytes(task_id)
                    return
        # Subscribe before yielding the accumulated snapshot. Token callbacks
        # can run while StreamingResponse hands that snapshot to the browser;
        # registering first closes the otherwise tiny subscribe gap.
        queue = handle.subscribe()
        try:
            if hub.is_shutting_down:
                yield _server_stopping_bytes(task_id)
                return
            if handle.partial:
                yield _sse("partial", {"text": handle.partial, "task_id": task_id})
            yield _sse(
                "worker",
                {"state": "live", "task_id": task_id, "status": task.status or ""},
            )
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.45)
                except TimeoutError:
                    chunks, last = await _replay_chunks(agent, task_id, last)
                    for chunk in chunks:
                        yield chunk
                    if handle.done:
                        async for chunk in _drain_replay_chunks(
                            agent, task_id, last
                        ):
                            yield chunk
                        yield _terminal_bytes(handle)
                        return
                    continue
                if item is None:
                    async for chunk in _drain_replay_chunks(
                        agent, task_id, last
                    ):
                        yield chunk
                    yield _terminal_bytes(handle)
                    return
                name, data = item
                if name == SERVER_STOPPING_EVENT:
                    yield _server_stopping_bytes(task_id)
                    return
                if name == "error":
                    yield _sse_error(
                        str(data.get("code") or "error"),
                        str(data.get("message") or "error"),
                    )
                    return
                if name == "tool":
                    chunks, last = await _replay_chunks(agent, task_id, last)
                    for chunk in chunks:
                        yield chunk
                    continue
                yield _sse(name, data)
        finally:
            handle.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _live_web_handle(
    hub: WorkspaceHub, rec: OpenedWorkspace, task: Any
) -> RunHandle | None:
    handle = hub.runs.by_task(rec.key, str(task.id))
    if handle is not None and not handle.done:
        return handle
    handle = hub.runs.by_session(rec.key, str(task.session_id or ""))
    if (
        handle is not None
        and not handle.done
        and (not handle.task_id or handle.task_id == task.id)
    ):
        return handle
    return None


async def _has_live_child_executor(agent: OmniAgent, task_id: str) -> bool:
    async with agent.db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(SubtaskORM).where(
                        SubtaskORM.task_id == task_id,
                        SubtaskORM.status == "running",
                    )
                )
            ).scalars().all()
        )
    return any(int(getattr(row, "owner_pid", 0) or 0) > 0 and pid_alive(int(row.owner_pid)) for row in rows)


async def _force_cancel_unowned_turn(agent: OmniAgent, task_id: str) -> None:
    controls = await agent.tasks.consume_controls(task_id, actions={"cancel"})
    if controls:
        await agent.tasks.mark_controls_applied([str(item["id"]) for item in controls])
    await agent.tasks.finish_task(
        task_id,
        status="cancelled",
        summary="Execution was cancelled after its worker was lost.",
    )


async def _cancel_turn(
    agent: OmniAgent,
    hub: WorkspaceHub,
    rec: OpenedWorkspace,
    task: Any,
) -> dict[str, Any]:
    """Cancel a turn; force-settle when no live worker remains."""
    handle = _live_web_handle(hub, rec, task)
    control = await agent.tasks.try_request_control(task.id, action="cancel")
    if handle is not None:
        if control is None:
            refreshed = await agent.tasks.get_task(task.id)
            status = str(getattr(refreshed, "status", "") or "")
            if status not in activitymod.FOLLOWABLE_TASK_STATUSES:
                return {
                    "task_id": task.id,
                    "action": "cancel",
                    "settled": True,
                    "status": status,
                }
            raise RpcError("not_active", f"task {task.id[:8]} is no longer controllable")
        return {
            "task_id": task.id,
            "action": "cancel",
            "control_id": control.id,
            "settled": False,
        }

    stale_after_s = float(
        getattr(getattr(agent.settings, "tasks", None), "interrupt_stale_after_s", 0.0) or 0.0
    )
    await reconcile_lost_executors(
        db=agent.db,
        task_recorder=agent.tasks,
        stale_after_s=stale_after_s,
        task_id=task.id,
        explicit=True,
    )
    refreshed = await agent.tasks.get_task(task.id)
    if refreshed is None:
        raise RpcError("not_found", f"task not found: {task.id}")
    status = str(refreshed.status or "")
    if status not in activitymod.FOLLOWABLE_TASK_STATUSES:
        return {
            "task_id": task.id,
            "action": "cancel",
            "settled": True,
            "status": status,
        }
    if await _has_live_child_executor(agent, task.id):
        if control is None:
            raise RpcError("not_active", f"task {task.id[:8]} is no longer controllable")
        return {
            "task_id": task.id,
            "action": "cancel",
            "control_id": control.id,
            "settled": False,
        }
    await _force_cancel_unowned_turn(agent, task.id)
    return {
        "task_id": task.id,
        "action": "cancel",
        "settled": True,
        "status": "cancelled",
    }


async def steer_or_cancel(
    agent: OmniAgent,
    hub: WorkspaceHub,
    rec: OpenedWorkspace,
    *,
    session_id: str,
    action: str,
    instruction: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    tid = (task_id or "").strip()
    if not tid:
        live = hub.runs.by_session(rec.key, session_id)
        if live is not None and live.task_id:
            tid = live.task_id
    task = await agent.tasks.get_task(tid) if tid else None
    if task is None:
        task = await agent.tasks.active_task_for_session(session_id)
    if task is None:
        raise RpcError("not_found", "no active task in this session")
    if action == "cancel":
        return await _cancel_turn(agent, hub, rec, task)
    control = await agent.tasks.request_control(
        task.id, action=action, instruction=instruction
    )
    if control is None:
        raise RpcError("not_active", f"task {task.id[:8]} is no longer controllable")
    return {
        "task_id": task.id,
        "action": action,
        "control_id": control.id,
        "settled": False,
    }


async def approve_task(agent: OmniAgent, rec: OpenedWorkspace, hub: WorkspaceHub, task_id: str) -> dict[str, Any]:
    result = await agent.approve_task(task_id, drain_tasks=hub.drain_tasks(rec))
    presentation = turn_presentation_from_result(result)
    return {
        "session_id": result.session_id,
        "task_id": result.task_id,
        "kind": result.kind,
        "markdown": presentation.to_markdown(),
        "text": result.text,
    }
