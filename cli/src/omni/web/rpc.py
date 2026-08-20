"""JSON-RPC-ish dispatcher for ``POST /api``."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from omni.web import host as hostmod
from omni.web import projectors, turns
from omni.web.channels import CHANNEL_METHODS, handle_channel
from omni.web.config import CONFIG_METHODS, handle_config, writes_config
from omni.web.home_guard import (
    RESTART_REQUIRED_MESSAGE,
    home_has_drifted,
    refuse_if_home_drifted,
)
from omni.web.personas import (
    describe_persona,
    persona_admission,
    persona_operation_status,
    persona_turn_request,
)
from omni.web.protocol import RpcError, biz_error, ok, params_of, read_json
from omni.web.skills import SKILL_METHODS, handle_skill, writes_skills
from omni.web.workspace import WorkspaceHub

OPEN_METHODS = frozenset(
    {
        "host.listDirectory",
        "workspace.list",
        "workspace.open",
        "workspace.select",
        *CHANNEL_METHODS,
        *CONFIG_METHODS,
        *SKILL_METHODS,
    }
)


async def dispatch(request: Request, method: str = "") -> Response:
    hub: WorkspaceHub = request.app.state.hub
    try:
        body = await read_json(request)
        method = method or str(body.get("method") or "")
        if not method:
            raise RpcError("invalid_params", "missing method")
        params = params_of(body)
        workspace = (
            params.get("workspace")
            or body.get("workspace")
            or request.headers.get("X-Omni-Workspace")
        )
        if method in OPEN_METHODS:
            return await _open_method(request, hub, method, params)
        rec = await hub.resolve(workspace if isinstance(workspace, str) else None, method=method)
        hub.require_writable(rec, method)
        agent = await hub.agent_for(rec)
        return await _store_method(hub, rec, agent, method, params)
    except RpcError as exc:
        return biz_error(exc.code, exc.message, **exc.extra)
    except LookupError as exc:
        return biz_error("not_found", str(exc))
    except ValueError as exc:
        return biz_error("invalid_params", str(exc))


async def _open_method(
    request: Request,
    hub: WorkspaceHub,
    method: str,
    params: dict[str, Any],
) -> JSONResponse:
    if method == "host.listDirectory":
        path = params.get("path")
        show_hidden = bool(params.get("show_hidden") or params.get("showHidden"))
        return ok(**hostmod.list_directory(str(path) if path else None, show_hidden=show_hidden))
    if method == "workspace.list":
        selected = hub.selected()
        return ok(workspaces=hub.catalog(), selected=selected.to_dict() if selected else None)
    if method == "workspace.open":
        path = params.get("path")
        if not path:
            raise RpcError("invalid_params", "workspace.open requires path")
        rec = await hub.open_path(str(path))
        return ok(workspace=rec.to_dict())
    if method == "workspace.select":
        rec = await hub.select(
            path=str(params["path"]) if params.get("path") else None,
            project_dir=str(params["project_dir"]) if params.get("project_dir") else None,
            name=str(params["name"]) if params.get("name") else None,
        )
        return ok(workspace=rec.to_dict())
    if method in CHANNEL_METHODS or method in CONFIG_METHODS or method in SKILL_METHODS:
        refuse_if_home_drifted(request.app, method)
    if method in CHANNEL_METHODS:
        result = await handle_channel(request, method, params)
        return ok(**result)
    if method in CONFIG_METHODS:
        result = await handle_config(method, params)
        if writes_config(method):
            hub.drop_agent_cache()
        if method in {"config.describe", "config.get"} and home_has_drifted(request.app):
            result["restart_required"] = True
            result["notice"] = RESTART_REQUIRED_MESSAGE
        return ok(**result)
    if method in SKILL_METHODS:
        result = await handle_skill(method, params)
        if writes_skills(method):
            hub.drop_agent_cache()
        if method in {"skill.list", "skill.info"} and home_has_drifted(request.app):
            result["restart_required"] = True
            result["notice"] = RESTART_REQUIRED_MESSAGE
        return ok(**result)
    raise RpcError("unknown_method", f"unknown method: {method}")


async def _store_method(  # noqa: C901 — thin method switch
    hub: WorkspaceHub,
    rec: Any,
    agent: Any,
    method: str,
    params: dict[str, Any],
) -> Response:
    if method == "session.list":
        return ok(
            sessions=await projectors.list_sessions(
                agent,
                limit=int(params.get("limit") or 50),
                channel=str(params.get("channel") or ""),
                hub=hub,
                rec=rec,
            ),
            workspace=rec.to_dict(),
        )
    if method == "persona.describe":
        return ok(persona=await describe_persona(agent, rec))
    if method == "persona.status":
        return ok(
            **await persona_operation_status(
                agent,
                str(params.get("task_id") or ""),
            )
        )
    if method == "persona.start":
        request = await persona_turn_request(agent, rec, params)
        async with persona_admission(agent, rec):
            return ok(**await turns.start_turn(hub, rec, **request))
    if method == "session.get":
        session_id = str(params.get("session_id") or params.get("id") or "")
        if not session_id:
            raise RpcError("invalid_params", "session.get requires session_id")
        return ok(session=await projectors.get_session(agent, session_id, hub=hub, rec=rec))
    if method == "workspace.inbox":
        return ok(
            **await projectors.workspace_inbox(
                agent,
                limit=int(params.get("limit") or 50),
                channel=str(params.get("channel") or ""),
                session_id=str(params.get("session_id") or ""),
                hub=hub,
                rec=rec,
            ),
            workspace=rec.to_dict(),
        )
    if method == "session.messages":
        session_id = str(params.get("session_id") or params.get("id") or "")
        if not session_id:
            raise RpcError("invalid_params", "session.messages requires session_id")
        return ok(messages=await projectors.session_messages(agent, session_id))
    if method == "session.timeline":
        session_id = str(params.get("session_id") or params.get("id") or "")
        if not session_id:
            raise RpcError("invalid_params", "session.timeline requires session_id")
        return ok(
            **await projectors.session_timeline(
                agent,
                session_id,
                hub=hub,
                rec=rec,
                limit=int(params.get("limit") or 200),
            )
        )
    if method == "session.create":
        return ok(session=await projectors.create_session(agent, title=str(params.get("title") or "")))
    if method == "session.rename":
        session_id = str(params.get("session_id") or params.get("id") or "")
        if not session_id:
            raise RpcError("invalid_params", "session.rename requires session_id")
        title = params.get("title")
        if title is None:
            raise RpcError("invalid_params", "session.rename requires title")
        return ok(session=await projectors.rename_session(agent, session_id, str(title)))
    if method == "session.delete":
        session_id = str(params.get("session_id") or params.get("id") or "")
        if not session_id:
            raise RpcError("invalid_params", "session.delete requires session_id")
        return ok(
            **await projectors.delete_session(agent, session_id, hub=hub, rec=rec)
        )
    if method == "task.list":
        return ok(
            tasks=await projectors.list_tasks(
                agent,
                limit=int(params.get("limit") or 40),
                session_id=str(params.get("session_id") or ""),
            )
        )
    if method == "task.get":
        task_id = str(params.get("task_id") or params.get("id") or "")
        if not task_id:
            raise RpcError("invalid_params", "task.get requires task_id")
        return ok(**await projectors.get_task(agent, task_id))
    if method == "task.events":
        task_id = str(params.get("task_id") or params.get("id") or "")
        if not task_id:
            raise RpcError("invalid_params", "task.events requires task_id")
        return ok(
            **await projectors.list_task_events(
                agent,
                task_id,
                after_seq=int(params.get("after_seq") or 0),
                limit=params.get("limit"),
            )
        )
    if method == "task.watch":
        task_id = str(params.get("task_id") or params.get("id") or "")
        if not task_id:
            raise RpcError("invalid_params", "task.watch requires task_id")
        return await turns.watch_task_sse(
            hub,
            rec,
            agent,
            task_id=task_id,
            after_seq=int(params.get("after_seq") or 0),
        )
    if method == "artifact.list":
        return ok(
            artifacts=await projectors.list_artifacts(
                agent,
                session_id=str(params.get("session_id") or ""),
                task_id=str(params.get("task_id") or ""),
                limit=int(params.get("limit") or 40),
            )
        )
    if method == "artifact.get":
        ident = str(params.get("id") or params.get("uri") or "")
        if not ident:
            raise RpcError("invalid_params", "artifact.get requires id or uri")
        return ok(artifact=await projectors.get_artifact(agent, ident))
    if method == "rom.get":
        return ok(rom=await projectors.rom_snapshot(agent))
    if method == "notebook.get":
        return ok(notebook=projectors.notebook_text(agent.paths), path=str(agent.paths.notebook))
    if method == "cost.get":
        return ok(
            cost=await projectors.cost_snapshot(
                agent,
                session_id=str(params.get("session_id") or ""),
                task_id=str(params.get("task_id") or ""),
            )
        )
    if method == "turn.start":
        text = str(params.get("text") or params.get("message") or "").strip()
        if not text:
            raise RpcError("invalid_params", "turn.start requires text")
        mode = params.get("interaction_mode") or params.get("mode")
        file_uris = params.get("file_uris") or []
        if not isinstance(file_uris, list):
            raise RpcError("invalid_params", "file_uris must be a list")
        session_id = str(params.get("session_id") or "") or None
        return ok(
            **await turns.start_turn(
                hub,
                rec,
                text=text,
                session_id=session_id,
                interaction_mode=str(mode) if mode else None,
                file_uris=[str(u) for u in file_uris],
                client_run_id=str(params.get("client_run_id") or ""),
            )
        )
    if method == "turn.steer":
        session_id = str(params.get("session_id") or "")
        instruction = str(params.get("instruction") or params.get("text") or "")
        if not session_id:
            raise RpcError("invalid_params", "turn.steer requires session_id")
        return ok(
            **await turns.steer_or_cancel(
                agent,
                hub,
                rec,
                session_id=session_id,
                action="steer",
                instruction=instruction,
                task_id=str(params.get("task_id") or ""),
            )
        )
    if method == "turn.cancel":
        session_id = str(params.get("session_id") or "")
        if not session_id:
            raise RpcError("invalid_params", "turn.cancel requires session_id")
        return ok(
            **await turns.steer_or_cancel(
                agent,
                hub,
                rec,
                session_id=session_id,
                action="cancel",
                task_id=str(params.get("task_id") or ""),
            )
        )
    if method == "task.approve":
        task_id = str(params.get("task_id") or params.get("id") or "")
        if not task_id:
            raise RpcError("invalid_params", "task.approve requires task_id")
        return ok(**await turns.approve_task(agent, rec, hub, task_id))
    if method == "command.run":
        text = str(params.get("text") or "").strip()
        session_id = str(params.get("session_id") or "")
        if not text or not session_id:
            raise RpcError("invalid_params", "command.run requires text and session_id")
        from omni.channels.commands import handle_channel_command

        presentation = await handle_channel_command(agent, text, session_id)
        if presentation is None:
            raise RpcError("not_a_command", "not a channel command")
        return ok(
            markdown=presentation.to_markdown(),
            session_id=presentation.session_id or session_id,
            task_id=presentation.task_id,
        )
    if method == "attachment.upload":
        raise RpcError("use_multipart", "POST /api/attachment.upload as multipart")
    raise RpcError("unknown_method", f"unknown method: {method}")
