"""Open a directory the way the CLI keys a workspace: ``get_paths(cwd=D)``.

The durable store is still keyed by ``paths.project_dir`` (VCS/path identity).
Persona state and the cached :class:`OmniAgent` are keyed by the opened
folder as well, so ``/repo`` and ``/repo/subdir`` do not borrow each other's
``invocation_cwd`` even when they share ``sessions.sqlite3``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omni.agent import OmniAgent
from omni.cli.state import make_agent_from_settings
from omni.config import load_settings
from omni.config import trust as trustmod
from omni.config.paths import OmniPaths, find_project_root, get_paths, user_home
from omni.config.workspaces import (
    channel_anchor_project_dir,
    iter_catalog_workspaces,
    list_workspaces,
)
from omni.personas.roots import persona_state_root
from omni.runtime.daemon import is_daemon_running
from omni.web.host import control_store_reason
from omni.web.protocol import RpcError
from omni.web.workspace_visibility import (
    canonical_project_dir,
    hidden_project_dirs,
    set_hidden_project_dirs,
)

WRITE_METHODS = frozenset(
    {
        "session.create",
        "session.rename",
        "session.delete",
        "session.deleteMany",
        "turn.start",
        "turn.steer",
        "turn.cancel",
        "task.approve",
        "command.run",
        "attachment.upload",
        "persona.start",
    }
)


def workspace_kind(paths: OmniPaths) -> str:
    if paths.workspace_root is None:
        return "named"
    if paths.project_dir == paths.workspace_root / ".omni":
        return "in-place"
    return "path"


def workspace_label(paths: OmniPaths) -> str:
    if paths.workspace_root is not None:
        return paths.workspace_root.name or paths.project_name
    return paths.project_name


def is_path_trusted(cwd: Path, *, settings_allow: list[str] | None = None) -> bool:
    """Mirror the CLI's implicit trust rules for a directory the user opened."""
    home = user_home()
    if find_project_root(cwd) is not None:
        return True
    probe = load_settings(trusted=True)
    allow = settings_allow
    if allow is None:
        allow = list(getattr(probe.trust, "allow", None) or [])
    if not getattr(probe.trust, "enabled", True):
        return True
    return trustmod.is_trusted(cwd, home=home, allow=allow)


@dataclass
class OpenedWorkspace:
    """One opened directory and the store :func:`get_paths` assigned it."""

    paths: OmniPaths
    trusted: bool
    kind: str
    label: str
    open_path: str
    project: str | None = None

    @property
    def store_key(self) -> str:
        return str(self.paths.project_dir.resolve())

    @property
    def key(self) -> str:
        """Cache identity: store + the exact folder that owns persona state."""
        return f"{self.store_key}::{persona_state_root(self.paths)}"

    @property
    def writable(self) -> bool:
        return self.trusted

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.paths.workspace_root) if self.paths.workspace_root else None,
            "project_dir": self.store_key,
            "project_name": self.paths.project_name,
            "invocation_cwd": (
                str(self.paths.invocation_cwd) if self.paths.invocation_cwd else self.open_path
            ),
            "kind": self.kind,
            "label": self.label,
            "trusted": self.trusted,
            "writable": self.writable,
            "open_path": self.open_path,
            "artifacts_dir": str(self.paths.artifacts_dir),
            "db": str(self.paths.project_db),
        }


class WorkspaceHub:
    """Per-folder agent cache + catalog projection for the web process."""

    def __init__(self) -> None:
        self._agents: dict[str, OmniAgent] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._opened: dict[str, OpenedWorkspace] = {}
        self._by_path: dict[str, str] = {}
        self._by_store: dict[str, str] = {}
        self._selected: str | None = None
        from omni.web.runs import RunManager

        self.runs = RunManager()

    @property
    def selected_key(self) -> str | None:
        return self._selected

    @property
    def is_shutting_down(self) -> bool:
        """Whether the Web process has entered its request-drain phase."""
        return self.runs.is_shutting_down

    def begin_shutdown(self) -> None:
        """Detach request watchers without interrupting their underlying work."""
        self.runs.begin_shutdown()

    async def wait_for_shutdown(self, *, timeout: float | None = None) -> bool:
        """Wait until the Web process starts draining HTTP requests."""
        return await self.runs.wait_for_shutdown(timeout=timeout)

    def selected(self) -> OpenedWorkspace | None:
        if not self._selected:
            return None
        return self._opened.get(self._selected)

    def live_session_identities(self) -> set[tuple[str, str]]:
        """Project in-process Web runs onto durable workspace identities."""
        out: set[tuple[str, str]] = set()
        for workspace_key, session_id in self.runs.live_sessions():
            rec = self._opened.get(workspace_key)
            if rec is not None:
                out.add((rec.store_key, session_id))
        return out

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def catalog(self, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        hidden = set() if include_hidden else hidden_project_dirs()
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for rec in list_workspaces() + iter_catalog_workspaces():
            if not isinstance(rec, dict):
                continue
            project_dir = str(rec.get("project_dir") or "")
            if not project_dir or project_dir in seen:
                continue
            if canonical_project_dir(project_dir) in hidden:
                continue
            seen.add(project_dir)
            item = {
                "name": rec.get("name") or Path(project_dir).name,
                "root": rec.get("root"),
                "project_dir": project_dir,
                "db": rec.get("db"),
                "kind": rec.get("kind") or "path",
                "last_seen": rec.get("last_seen") or 0,
                "label": rec.get("name") or Path(str(rec.get("root") or project_dir)).name,
            }
            out.append(item)
        out.sort(key=lambda r: float(r.get("last_seen") or 0), reverse=True)
        return out

    def hide_workspaces(self, project_dirs: list[str]) -> list[str]:
        """Hide known, idle workspace rows without touching their data."""
        requested = {
            canonical_project_dir(value)
            for value in project_dirs
            if str(value).strip()
        }
        if not requested:
            raise RpcError("invalid_params", "workspace.hideMany requires project_dirs")
        known = {
            canonical_project_dir(str(item.get("project_dir") or ""))
            for item in self.catalog(include_hidden=True)
            if item.get("project_dir")
        }
        unknown = sorted(requested - known)
        if unknown:
            raise RpcError("not_found", "unknown workspace", project_dirs=unknown)
        selected = self.selected()
        if selected is not None and canonical_project_dir(selected.store_key) in requested:
            raise RpcError(
                "busy",
                "the selected workspace cannot be hidden",
                project_dir=selected.store_key,
            )
        anchor = canonical_project_dir(channel_anchor_project_dir())
        if anchor in requested:
            raise RpcError(
                "protected",
                "the Home Service channel workspace cannot be hidden",
                project_dir=anchor,
            )
        for project_dir in requested:
            opened_key = self._by_store.get(project_dir)
            if opened_key and self.runs.inflight_in_workspace(opened_key):
                raise RpcError(
                    "busy",
                    "a workspace with a running Web turn cannot be hidden",
                    project_dir=project_dir,
                )
        set_hidden_project_dirs(requested, hidden=True)
        return sorted(requested)

    def unhide_workspaces(self, project_dirs: list[str]) -> list[str]:
        """Restore explicitly named workspace rows to the Web catalog."""
        requested = sorted(
            {
                canonical_project_dir(value)
                for value in project_dirs
                if str(value).strip()
            }
        )
        if not requested:
            raise RpcError("invalid_params", "workspace.unhideMany requires project_dirs")
        set_hidden_project_dirs(requested, hidden=False)
        return requested

    async def open_path(self, path: str | Path, *, select: bool = True) -> OpenedWorkspace:
        target = Path(path).expanduser()
        try:
            target = target.resolve()
        except OSError as exc:
            raise RpcError("invalid_path", f"cannot resolve path: {exc}") from exc
        if not target.exists():
            raise RpcError("not_found", f"directory does not exist: {target}")
        if not target.is_dir():
            raise RpcError("not_a_directory", f"not a directory: {target}")
        blocked = control_store_reason(target)
        if blocked:
            raise RpcError("control_store", blocked)
        paths = get_paths(cwd=target)
        trusted = is_path_trusted(target)
        rec = OpenedWorkspace(
            paths=paths,
            trusted=trusted,
            kind=workspace_kind(paths),
            label=workspace_label(paths),
            open_path=str(target),
        )
        await self._ensure_agent(rec)
        set_hidden_project_dirs([rec.store_key], hidden=False, home=rec.paths.home)
        if select:
            self._remember(rec, selected=True)
        return rec

    async def open_named(self, name: str, *, select: bool = True) -> OpenedWorkspace:
        name = (name or "").strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise RpcError("invalid_project", f"invalid named project: {name}")
        named_dir = get_paths(project=name).project_dir
        settings = load_settings(project=name, cwd=named_dir, trusted=True)
        paths = settings.paths
        rec = OpenedWorkspace(
            paths=paths,
            trusted=True,
            kind="named",
            label=name,
            open_path=str(paths.project_dir),
            project=name,
        )
        await self._ensure_agent(rec, settings=settings)
        set_hidden_project_dirs([rec.store_key], hidden=False, home=rec.paths.home)
        if select:
            self._remember(rec, selected=True)
        return rec

    async def select(
        self,
        *,
        path: str | None = None,
        project_dir: str | None = None,
        name: str | None = None,
    ) -> OpenedWorkspace:
        if path:
            return await self.open_path(path)
        if name:
            return await self.open_named(name)
        if project_dir:
            key = str(Path(project_dir).expanduser().resolve())
            existing = self._lookup_token(key)
            if existing is not None:
                set_hidden_project_dirs(
                    [existing.store_key],
                    hidden=False,
                    home=existing.paths.home,
                )
                self._remember(existing, selected=True)
                return existing
            for rec in self.catalog(include_hidden=True):
                if str(rec.get("project_dir") or "") == key:
                    root = rec.get("root")
                    if root:
                        return await self.open_path(str(root))
                    return await self.open_named(str(rec.get("name") or Path(key).name))
            raise RpcError("not_found", f"unknown workspace: {project_dir}")
        raise RpcError("invalid_params", "workspace.select requires path, project_dir, or name")

    def _remember(self, rec: OpenedWorkspace, *, selected: bool = False) -> None:
        self._opened[rec.key] = rec
        self._by_path[rec.open_path] = rec.key
        try:
            resolved_open = str(Path(rec.open_path).expanduser().resolve())
        except OSError:
            resolved_open = rec.open_path
        self._by_path[resolved_open] = rec.key
        self._by_store[rec.store_key] = rec.key
        if selected:
            self._selected = rec.key

    def _lookup_token(self, token: str) -> OpenedWorkspace | None:
        if token in self._opened:
            return self._opened[token]
        mapped = self._by_path.get(token)
        if mapped and mapped in self._opened:
            return self._opened[mapped]
        store_mapped = self._by_store.get(token)
        if store_mapped and store_mapped in self._opened:
            return self._opened[store_mapped]
        return None

    def lookup(self, workspace: str | None) -> OpenedWorkspace | None:
        if not workspace:
            return self.selected()
        raw = str(workspace).strip()
        if not raw:
            return self.selected()
        hit = self._lookup_token(raw)
        if hit is not None:
            return hit
        try:
            resolved = str(Path(raw).expanduser().resolve())
        except OSError:
            return None
        if resolved != raw:
            return self._lookup_token(resolved)
        return None

    async def resolve(self, workspace: str | None, *, method: str) -> OpenedWorkspace:
        rec = self.lookup(workspace)
        if rec is not None:
            return rec
        if workspace:
            candidate = Path(str(workspace)).expanduser()
            if candidate.is_dir() and control_store_reason(candidate) is None:
                return await self.open_path(candidate, select=False)
        raise RpcError(
            "workspace_required",
            "select a workspace directory first",
            method=method,
        )

    async def agent_for(self, rec: OpenedWorkspace) -> OmniAgent:
        agent = self._agents.get(rec.key)
        if agent is None:
            await self._ensure_agent(rec)
            agent = self._agents[rec.key]
        return agent

    def drop_agent_cache(self) -> None:
        """Forget cached agents so the next turn reloads settings from disk.

        In-flight turns keep the agent object they already hold. Do not close
        those instances here — a settings write must not cancel a running turn.
        """
        self._agents.clear()

    def require_writable(self, rec: OpenedWorkspace, method: str) -> None:
        if method in WRITE_METHODS and not rec.writable:
            raise RpcError(
                "untrusted",
                "this directory is not trusted; the web UI is read-only until "
                "the CLI trusts it (omni trust)",
                path=rec.open_path,
            )

    def drain_tasks(self, rec: OpenedWorkspace) -> bool:
        """Match CLI/IM: drain inline unless this store already has a daemon."""
        return not is_daemon_running(rec.paths)

    async def _ensure_agent(
        self,
        rec: OpenedWorkspace,
        *,
        settings: Any = None,
    ) -> OmniAgent:
        key = rec.key
        async with self._lock_for(key):
            existing = self._agents.get(key)
            if existing is not None:
                self._remember(rec)
                return existing
            if settings is None:
                settings = load_settings(
                    project=rec.project,
                    cwd=Path(rec.open_path),
                    trusted=rec.trusted,
                )
            agent = await make_agent_from_settings(settings)
            # The CLI wires a TTY approver; the web process must not block on stdin.
            agent.approver = None
            self._agents[key] = agent
            self._remember(rec)
            return agent

    async def aclose(self) -> None:
        self.begin_shutdown()

        async def _interrupt(handle: Any) -> None:
            task_id = handle.task_id
            if not task_id:
                return
            agent = self._agents.get(handle.workspace_key)
            if agent is None:
                return
            try:
                await agent.tasks.request_control(task_id, action="cancel", instruction="")
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                pass

        await self.runs.shutdown(interrupt=_interrupt)
        agents = list(self._agents.values())
        self._agents.clear()
        for agent in agents:
            try:
                await agent.aclose()
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                pass


async def close_workspace_hub(hub: WorkspaceHub, *, timeout: float = 2.0) -> None:
    """Close opened stores without blocking Ctrl+C on a live turn."""
    try:
        await asyncio.wait_for(hub.aclose(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        return
    except Exception:  # noqa: BLE001 — process is exiting
        return
