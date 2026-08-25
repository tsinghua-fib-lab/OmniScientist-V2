"""The home-level background service runtime (one supervised process per ``OMNI_HOME``).

This replaces the legacy "one detached ``omni serve`` per workspace" model with a
single control service that:

* dispatches **schedules for every registered workspace** — it starts one task
  runtime (workers + DB poller) per eligible workspace, and each poller ticks
  that workspace's :class:`~omni.runtime.scheduler.Scheduler`, so a schedule
  created in project A fires even while the user is working in project B; and
* owns **messaging channels once**, on a single stable *anchor* workspace (the
  ``default`` named project), so WeChat / Feishu / DingTalk connections are
  bound by exactly one process instead of being fought over by several
  per-workspace daemons.

It is deliberately conservative: constructing a per-workspace agent is isolated
(its own SQLite store, memory, registry), a bad workspace is skipped rather than
crashing the service, and teardown drains cooperatively so ``omni update`` can
hand off cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omni import __version__
from omni.config import OmniSettings, load_settings
from omni.config.paths import is_within_home
from omni.config.workspaces import register_workspace
from omni.runtime import service_state
from omni.runtime.daemon import stop_legacy_daemons

logger = logging.getLogger(__name__)

# Safety cap: never host more than this many workspace runtimes in one service.
# A machine with hundreds of stale registry entries should not fork hundreds of
# SQLite connections; the most-recently-seen workspaces win.
_MAX_WORKSPACES = 64

# The stable channel anchor: inbound IM always lands in one consistent project
# regardless of which repos are open, matching how a single ``omni serve`` from
# a neutral directory resolved to the ``default`` project today.
_DEFAULT_ANCHOR = "default"


def _has_active_schedules(db_path: str) -> bool:
    """True if the workspace DB has at least one enabled schedule.

    A cheap, read-only probe (no agent construction) so the service can host
    *only* workspaces that actually need a scheduler tick — the anchor plus any
    workspace with real scheduled work — instead of forking an idle runtime for
    every repo the user has ever opened. Missing DB / missing table ⇒ ``False``.
    """
    if not db_path:
        return False
    path = Path(db_path)
    if not path.is_file():
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return False
    try:
        row = con.execute("SELECT 1 FROM schedules WHERE enabled=1 LIMIT 1").fetchone()
        return row is not None
    except sqlite3.Error:
        return False  # table absent on a fresh workspace
    finally:
        con.close()


@dataclass
class _WorkspaceRuntime:
    """One hosted workspace: its agent, whether it anchors channels, and health."""

    key: str  # project_dir string (stable identity)
    name: str
    agent: Any  # OmniAgent
    is_anchor: bool = False
    channel_manager: Any = None
    channel_task: asyncio.Task | None = None


class HomeService:
    """Supervises per-workspace runtimes and the single channel anchor."""

    def __init__(
        self,
        settings: OmniSettings,
        *,
        workers: int = 1,
        enable_channels: bool = True,
        channels_filter: str = "",
        reconcile_interval_s: float | None = None,
    ) -> None:
        self.settings = settings
        self.paths = settings.paths
        self.workers = max(1, int(workers))
        self.enable_channels = enable_channels
        self.channels_filter = channels_filter
        self.reconcile_interval_s = float(
            reconcile_interval_s
            if reconcile_interval_s is not None
            else settings.service.reconcile_interval_s
        )
        self._ws: dict[str, _WorkspaceRuntime] = {}
        self._anchor_key: str = ""
        self._stop = asyncio.Event()
        self._generation = 0
        self._singleton_fd: int | None = None
        self._instance_id = uuid.uuid4().hex

    # ── eligibility & construction ───────────────────────────────────────────

    def _anchor_project(self) -> str:
        desired = service_state.read_desired(self.paths)
        return (desired.channel_anchor or _DEFAULT_ANCHOR).strip() or _DEFAULT_ANCHOR

    def _eligible_records(self) -> list[dict]:
        """Registered workspaces this service should host, most-recent first.

        A workspace is hosted only when it has real scheduled work (≥1 enabled
        schedule): the home service exists to keep channels *and* schedules
        alive, so forking an idle task runtime for every repo the user has ever
        opened is pure waste and clutters ``omni serve status``. The anchor is
        always hosted (for channels) by :meth:`start`, independent of this list.

        Ghost workspaces keyed under ``~/.omni`` are skipped (they would poll the
        same schedules/bots as the real project), as are path-keyed workspaces
        whose root no longer exists on disk.
        """
        out: list[dict] = []
        seen: set[str] = set()
        from omni.config.workspaces import iter_catalog_workspaces

        for record in iter_catalog_workspaces(self.paths.home):
            project_dir = str(record.get("project_dir") or "")
            if not project_dir or project_dir in seen:
                continue
            root = record.get("root")
            if root and is_within_home(Path(root), self.paths.home):
                continue  # ghost/data-dir workspace
            if root and not Path(root).is_dir():
                continue  # moved/deleted repo
            if not _has_active_schedules(str(record.get("db") or "")):
                continue  # nothing to dispatch here — don't fork an idle runtime
            seen.add(project_dir)
            out.append(record)
            if len(out) >= _MAX_WORKSPACES:
                break
        return out

    def _settings_for_record(self, record: dict) -> OmniSettings:
        kind = str(record.get("kind") or "")
        root = record.get("root")
        if kind == "named" or not root:
            return load_settings(project=str(record.get("name") or _DEFAULT_ANCHOR), trusted=None)
        # Honour the persisted folder-trust decision the same way the interactive
        # CLI does (``omni.cli.state.resolve_workspace_trust``): a scheduled or
        # background task in a directory the user already trusted must still write
        # deliverables INTO that directory's ``outputs/``, not silently divert
        # them to the durable ``~/.omni`` store. The daemon never prompts — only
        # an already trusted root mirrors — and ``output_dir`` is pinned to that
        # workspace's ``outputs/`` folder because the service CWD is not the
        # workspace.
        from omni.storage.artifacts import USER_OUTPUT_DIRNAME

        root_path = Path(root)
        if self._workspace_trusted(root_path):
            return load_settings(
                cwd=root_path,
                trusted=True,
                overrides={
                    "artifacts": {
                        "output_dir": str((root_path / USER_OUTPUT_DIRNAME).resolve())
                    }
                },
            )
        return load_settings(cwd=root_path, trusted=None)

    def _workspace_trusted(self, root: Path) -> bool:
        """Whether the daemon may mirror deliverables into ``root``.

        Non-interactive parity with the CLI trust gate: global trust disabled ⇒
        trusted; ``root`` itself already adopted as an in-place ``.omni``
        project ⇒ trusted; otherwise only the persisted ledger / config allowlist
        decides (never a prompt).

        Adoption is consent for the directory that carries the marker. This used
        to accept any adopted *ancestor*, which is a containment question
        standing in for an identity one: it answered "is this below something
        adopted?" while meaning "was this adopted?". Every directory under an
        adopted tree inherited a decision the owner made about one folder, and
        the consequence is not cosmetic — it decides whether the daemon writes a
        turn's figures and reports into that directory.
        """
        from omni.config import trust as trustmod
        from omni.config.paths import find_project_root

        tcfg = self.settings.trust
        if not tcfg.enabled:
            return True
        if find_project_root(root) == root.resolve():
            return True
        return trustmod.is_trusted(root, home=self.paths.home, allow=tcfg.allow)

    async def _build_agent(self, settings: OmniSettings) -> Any:
        from omni.agent import OmniAgent

        return await OmniAgent.create(settings)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Foreground entrypoint used by ``omni serve run`` under a supervisor.

        Guarded by the home-service singleton lock: if another live service
        already holds it (a duplicate supervised unit, a stray detached spawn, a
        racing launch-ensure), this instance exits immediately instead of adding
        a second process that fights over the runtime pidfile and channel locks.
        """
        logging.basicConfig(
            level=getattr(logging, self.settings.observability.log_level, logging.INFO)
        )
        self._singleton_fd = service_state.acquire_singleton(self.paths)
        if self._singleton_fd is None:
            holder = service_state.singleton_holder_pid(self.paths)
            logger.warning(
                "home service: another instance already owns %s (pid=%s); exiting to keep "
                "exactly one service per OMNI_HOME.",
                self.paths.home,
                holder if holder else "unknown",
            )
            return
        # Publish process identity immediately. Agent/skill/channel initialization
        # can take seconds; lifecycle callers must see STARTING rather than mistake
        # this legitimate singleton owner for a down or stray process.
        self._write_runtime(ready=False, phase="starting")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        # Migration: retire legacy per-workspace daemons so schedules/channels are
        # not double-driven while this home service owns them.
        self._reap_legacy_daemons()
        try:
            await self.start()
            hb = asyncio.create_task(self._heartbeat_loop())
            reconcile = asyncio.create_task(self._reconcile_loop())
            await self._stop.wait()
            hb.cancel()
            reconcile.cancel()
            await asyncio.gather(hb, reconcile, return_exceptions=True)
        finally:
            await self.stop()
            service_state.release_singleton(self._singleton_fd)
            self._singleton_fd = None

    async def start(self) -> None:
        anchor_project = self._anchor_project()
        anchor_settings = load_settings(project=anchor_project, trusted=None)
        anchor_key = str(anchor_settings.paths.project_dir)
        self._anchor_key = anchor_key
        await self._ensure_workspace(anchor_settings, anchor=True)
        for record in self._eligible_records():
            key = str(record.get("project_dir") or "")
            if key == anchor_key or key in self._ws:
                continue
            try:
                ws_settings = self._settings_for_record(record)
                await self._ensure_workspace(ws_settings, anchor=False)
            except Exception:  # noqa: BLE001 - one bad workspace must not sink the service
                logger.exception("home service: failed to host workspace %s", key)
        # Control-plane READY is independent of IM HTTP. WeChat/Feishu/DingTalk
        # connect in the background and report through ``channel_health``;
        # ``omni update`` / ``omni serve restart`` must not wait for them.
        self._write_runtime(ready=True)
        service_state.clear_start_request(self.paths)
        if self.enable_channels:
            anchor = self._ws.get(anchor_key)
            if anchor is not None:
                try:
                    await self._install_channels(anchor, anchor_settings, anchor.agent)
                except Exception:  # noqa: BLE001 - channels must not sink the gateway
                    logger.exception(
                        "home service: channel install failed; control plane is still ready"
                    )
                self._write_runtime(ready=True)
        logger.info(
            "home service up: %d workspace runtime(s), anchor=%s, channels=%s",
            len(self._ws), Path(anchor_key).name, "on" if self.enable_channels else "off",
        )

    async def _ensure_workspace(self, settings: OmniSettings, *, anchor: bool) -> None:
        key = str(settings.paths.project_dir)
        if key in self._ws:
            return
        agent = await self._build_agent(settings)
        # Write-through into workspaces.json so CLI surfaces that still call
        # ``list_workspaces`` (and humans inspecting the registry) see the same
        # stores the catalog already enumerates — especially the IM channel
        # anchor, which previously hosted forever without ever being registered.
        try:
            register_workspace(settings.paths)
        except Exception:  # noqa: BLE001 — registry is advisory, never block hosting
            logger.debug("home service: register_workspace failed for %s", key, exc_info=True)
        from omni.runtime.notifications import InboxNotifier

        # Inbox first so the task runtime can accept work before IM adapters
        # finish their HTTP handshake. Channel install wraps this notifier later.
        agent.runtime.set_notifier(InboxNotifier(agent.paths.project_dir / "inbox.jsonl"))
        ws = _WorkspaceRuntime(key=key, name=settings.paths.project_name, agent=agent, is_anchor=anchor)
        await agent.runtime.start(workers=self.workers)
        self._ws[key] = ws

    async def _install_channels(self, ws: _WorkspaceRuntime, settings: OmniSettings, agent: Any) -> None:
        from omni.channels.manager import ChannelManager
        from omni.runtime.notifications import CompositeNotifier, InboxNotifier

        explicit = [c.strip() for c in self.channels_filter.split(",") if c.strip()]
        manager = ChannelManager(settings, agent, explicit_channels=explicit or None)
        agent.runtime.set_notifier(
            CompositeNotifier([InboxNotifier(agent.paths.project_dir / "inbox.jsonl"), manager])
        )
        ws.channel_manager = manager
        await manager.reconcile_once()
        ws.channel_task = asyncio.create_task(manager.start(), name="channel-manager")

    async def reconcile(self) -> None:
        """Pick up newly opened workspaces without a restart."""
        for record in self._eligible_records():
            key = str(record.get("project_dir") or "")
            if not key or key in self._ws:
                continue
            try:
                await self._ensure_workspace(self._settings_for_record(record), anchor=False)
                logger.info("home service: adopted new workspace %s", Path(key).name)
            except Exception:  # noqa: BLE001
                logger.exception("home service: reconcile failed to host %s", key)

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.reconcile_interval_s)
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            self._generation += 1
            # Retire any legacy per-workspace daemon that appeared since startup
            # before adopting new work, so schedules are never double-driven.
            self._reap_legacy_daemons()
            try:
                await self.reconcile()
            except Exception:  # noqa: BLE001
                logger.exception("home service reconcile tick failed")

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            self._write_runtime(ready=True)

    def _channel_health(self) -> dict[str, Any]:
        anchor = self._ws.get(self._anchor_key)
        if anchor is None or anchor.channel_manager is None:
            return {}
        try:
            return anchor.channel_manager.snapshot()
        except Exception:  # noqa: BLE001
            return {}

    def _channel_names(self) -> list[str]:
        anchor = self._ws.get(self._anchor_key)
        if anchor is None or anchor.channel_manager is None:
            return []
        try:
            return anchor.channel_manager.desired_names()
        except Exception:  # noqa: BLE001
            return []

    def _write_runtime(self, *, ready: bool, phase: str | None = None) -> None:
        service_state.write_runtime(
            self.paths,
            {
                "version": __version__,
                "executable": sys.executable,
                "manager": service_state.read_desired(self.paths).manager,
                "service_id": service_state.service_instance_id(self.paths),
                "instance_id": self._instance_id,
                "ready": ready,
                "phase": phase or ("ready" if ready else "starting"),
                "workers": self.workers,
                "anchor": Path(self._anchor_key).name if self._anchor_key else "",
                "anchor_dir": self._anchor_key,
                "workspaces": [
                    {"name": ws.name, "dir": ws.key, "anchor": ws.is_anchor}
                    for ws in self._ws.values()
                ],
                "channels": self._channel_names(),
                "channel_health": self._channel_health(),
                "reconcile_generation": self._generation,
            },
        )

    def _reap_legacy_daemons(self) -> None:
        """Stop pre-existing per-workspace ``omni serve`` daemons.

        They would otherwise keep their own channel locks (starving this service)
        and double-tick the same schedules. Runs at startup *and* on every
        reconcile tick, so a legacy daemon that appears later is retired too.
        Best-effort; a daemon we cannot signal is left alone.
        """
        try:
            reaped = stop_legacy_daemons(self.paths.home)
        except Exception:  # noqa: BLE001
            return
        for pid in reaped:
            logger.info("home service: retired legacy per-workspace daemon pid=%s", pid)

    async def stop(self) -> None:
        if self._singleton_fd is not None:
            self._write_runtime(ready=False, phase="stopping")
        drain_grace = float(self.settings.service.drain_grace_s)
        # Stop channel managers first so no new inbound work arrives mid-drain.
        for ws in self._ws.values():
            if ws.channel_task is not None:
                ws.channel_task.cancel()
        for ws in self._ws.values():
            if ws.channel_task is not None:
                await asyncio.gather(ws.channel_task, return_exceptions=True)
            if ws.channel_manager is not None:
                try:
                    await ws.channel_manager.stop()
                except Exception:  # noqa: BLE001
                    logger.debug("channel manager stop failed", exc_info=True)
        # Bounded drain: give in-flight tasks a moment before tearing down runtimes.
        deadline = time.monotonic() + max(0.0, drain_grace)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            break  # single settle tick; runtimes cancel their own workers below
        for ws in self._ws.values():
            try:
                await ws.agent.runtime.stop()
            except Exception:  # noqa: BLE001
                logger.debug("runtime stop failed for %s", ws.key, exc_info=True)
            try:
                await ws.agent.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("agent aclose failed for %s", ws.key, exc_info=True)
        self._ws.clear()
        service_state.clear_runtime_if_owner(self.paths)


async def run_home_service(
    settings: OmniSettings,
    *,
    workers: int = 1,
    enable_channels: bool = True,
    channels_filter: str = "",
) -> None:
    service = HomeService(
        settings,
        workers=workers,
        enable_channels=enable_channels,
        channels_filter=channels_filter,
    )
    await service.run()


__all__ = ["HomeService", "run_home_service"]
