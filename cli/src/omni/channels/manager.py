"""Dynamic channel adapter manager for ``omni serve``.

One daemon owns the task runtime and reconciles channel adapters from local
configuration. A broken IM adapter must not take down the daemon; it becomes a
degraded channel with a retry delay while other channels keep working.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import time
import tomllib
from pathlib import Path
from typing import Any

from omni.agent import OmniAgent
from omni.channels import build_channels
from omni.channels import locks as channel_locks
from omni.channels.config import load_channel_config
from omni.config.settings import OmniSettings
from omni.runtime.notifications import TaskNotification

logger = logging.getLogger(__name__)

KNOWN_CHANNELS = ("cli", "wechat", "feishu", "dingtalk")
# IM channels whose credentials live home-level (``~/.omni/channels``): exactly
# one daemon machine-wide may bind each of these, guarded by a home-level lock.
# ``cli`` has no external poll loop, so it never needs the lock.
_LOCKED_CHANNELS = ("wechat", "feishu", "dingtalk")
_REQUIRED: dict[str, tuple[str, ...]] = {
    "feishu": ("app_id", "app_secret"),
    "dingtalk": ("client_id", "client_secret"),
}
# Home-level sentinel a freshly-logged-in channel touches (see ``request_reload``)
# to ask the running daemon to reconcile *now* instead of after the full interval.
_RELOAD_SENTINEL = ".reload"
# How often the reconcile loop wakes to notice the sentinel while idling.
_RELOAD_POLL_INTERVAL = 0.5


def _required_fields(name: str, data: dict[str, Any]) -> tuple[str, ...]:
    """Required config fields for static completeness, mode-aware for WeChat."""
    if name == "wechat":
        from omni.channels.wechat import resolve_wechat_mode

        mode = resolve_wechat_mode(data)
        if mode == "ilink":
            return ("bot_token",)
        if mode == "wecom":
            return ("gateway_url", "inbox_path", "send_path")
        return ("gateway_url", "inbox_path", "send_path")
    return _REQUIRED.get(name, ())


def configured_channel_names(settings: OmniSettings) -> list[str]:
    """Read the latest configured/enabled channel names from disk."""
    paths = settings.paths
    enabled = _enabled_from_config(paths.config_file)
    out: list[str] = []
    for name in enabled:
        if name not in KNOWN_CHANNELS or name in out:
            continue
        out.append(name)
    return out or ["cli"]


def channel_config_state(settings: OmniSettings, name: str) -> tuple[bool, str]:
    """Return ``(configured, reason)`` for static config completeness."""
    if name == "cli":
        return True, "configured"
    if name not in KNOWN_CHANNELS:
        return False, "unknown channel"
    cfg_path = settings.paths.channels_dir / f"{name}.toml"
    if not cfg_path.is_file():
        return False, "not configured"
    data = load_channel_config(settings, name)
    missing = [field for field in _required_fields(name, data) if not str(data.get(field) or "").strip()]
    if missing:
        return False, "missing " + ",".join(missing)
    return True, "configured"


def channel_runtime_state(settings: OmniSettings, name: str) -> tuple[str, str]:
    """Return ``(status, reason)`` before starting a channel adapter."""
    configured, reason = channel_config_state(settings, name)
    if not configured:
        return "not_configured", reason
    if name == "cli":
        return "configured", "configured"
    data = load_channel_config(settings, name)
    dep = _missing_dependency(name, data)
    if dep:
        return "degraded", dep
    return "configured", "configured"


class ChannelManager:
    """Own dynamic channel tasks and fan task notifications to active adapters."""

    def __init__(
        self,
        settings: OmniSettings,
        agent: OmniAgent,
        *,
        explicit_channels: list[str] | None = None,
        reconcile_interval: float = 5.0,
        retry_interval: float = 30.0,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.explicit_channels = [c for c in (explicit_channels or []) if c]
        self.reconcile_interval = reconcile_interval
        self.retry_interval = retry_interval
        self._tasks: dict[str, asyncio.Task] = {}
        self._channels: dict[str, Any] = {}
        self._health: dict[str, dict[str, Any]] = {}
        self._retry_after: dict[str, float] = {}
        self._locks: dict[str, channel_locks.ChannelLock] = {}
        # Per-channel credential/config fingerprint captured when the adapter was
        # built, so a re-login (new token) hot-reloads *only* that channel.
        self._fingerprints: dict[str, str] = {}
        self._reload_seen: float = self._reload_mtime()
        self._stopping = False

    @property
    def dynamic(self) -> bool:
        return not self.explicit_channels

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """JSON-serialisable health snapshot for pidfile/status commands."""
        out: dict[str, dict[str, Any]] = {}
        for name in KNOWN_CHANNELS:
            out[name] = dict(self._health.get(name) or {"status": "disabled", "reason": ""})
        return out

    def desired_names(self) -> list[str]:
        names = self._desired_names()
        return names if names else ["cli"]

    async def start(self) -> None:
        """Run the reconcile loop until cancelled."""
        try:
            while not self._stopping:
                await self.reconcile_once()
                await self.replay_failed_deliveries()
                await self._wait_next_cycle()
        except asyncio.CancelledError:
            raise
        finally:
            await self.stop()

    async def _wait_next_cycle(self) -> None:
        """Sleep one reconcile interval, waking early when a reload is requested.

        Polls the home-level reload sentinel in small steps so a fresh
        ``omni channel login`` is picked up within ``_RELOAD_POLL_INTERVAL``
        rather than the full ``reconcile_interval`` — without any OS signals
        (``SIGHUP`` is unavailable on Windows).
        """
        deadline = time.monotonic() + self.reconcile_interval
        while not self._stopping:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(_RELOAD_POLL_INTERVAL, remaining))
            if self._reload_requested():
                return

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._channels.clear()
        for name in list(self._locks):
            self._release_lock(name)

    async def notify(self, note: TaskNotification) -> None:
        channel = self._channels.get(note.channel)
        if channel is None:
            if note.channel == "cli":
                return
            from omni.runtime.notifications import record_delivery_status

            record_delivery_status(
                self.settings.paths.project_dir,
                note,
                status="failed",
                message=f"channel '{note.channel}' is not active in this daemon",
            )
            return
        await channel.notify(note)

    async def replay_failed_deliveries(self) -> int:
        """Hand over notifications a channel failed to deliver, once it can.

        A hard failure is rarely about the message: WeChat refuses a burst by
        answering ``ret=-2`` to *every* send for a few minutes, including the
        first message of an unrelated reply that follows. The task result is
        already durable, so leaving the queue undrained is the difference
        between a reader who gets their files a minute late and one who never
        gets them at all.
        """
        from omni.runtime.notifications import (
            pending_delivery_retries,
            record_delivery_retry,
            task_notification_from_dict,
        )

        project_dir = self.settings.paths.project_dir
        replayed = 0
        for entry in pending_delivery_retries(project_dir):
            channel = self._channels.get(str(entry.get("channel") or ""))
            if channel is None:
                continue
            note = task_notification_from_dict(dict(entry.get("notification") or {}))
            if not note.external_key:
                continue
            record_delivery_retry(project_dir, entry, status="attempted")
            try:
                status = await channel.send_task_notification(note)
            except Exception:  # noqa: BLE001
                logger.debug("delivery replay raised for %s", note.channel, exc_info=True)
                continue
            if status in {"sent", "degraded"}:
                record_delivery_retry(project_dir, entry, status="sent")
                replayed += 1
                logger.info(
                    "replayed %s delivery for task %s (%s)",
                    note.channel,
                    note.task_id[:8],
                    status,
                )
        return replayed

    async def reconcile_once(self) -> None:
        desired = set(self._desired_names())
        now = time.time()

        for name in KNOWN_CHANNELS:
            if name not in desired:
                await self._stop_channel(name, reason="disabled")
                self._health[name] = {"status": "disabled", "reason": ""}

        for name in desired:
            status, reason = channel_runtime_state(self.settings, name)
            if status == "not_configured":
                await self._stop_channel(name, reason=reason)
                self._health[name] = {"status": "not_configured", "reason": reason}
                continue
            if status == "degraded":
                await self._stop_channel(name, reason=reason)
                self._health[name] = {"status": "degraded", "reason": reason}
                continue

            task = self._tasks.get(name)
            if task is not None and not task.done():
                if not self._config_changed(name):
                    self._health[name] = {"status": "running", "reason": "ok"}
                    continue
                # Credentials/config changed under a live adapter (e.g. a fresh
                # `omni channel login`): rebuild *this* channel only so its new
                # token takes effect. Other running channels are untouched.
                logger.info("channel %s credentials/config changed; hot-reloading", name)
                await self._stop_channel(name, reason="config changed")
                self._retry_after.pop(name, None)
                task = None

            if task is not None and task.done():
                self._tasks.pop(name, None)
                self._channels.pop(name, None)

            if now < self._retry_after.get(name, 0.0):
                self._health.setdefault(name, {"status": "degraded", "reason": "waiting before retry"})
                continue

            self._start_channel(name)

    def _desired_names(self) -> list[str]:
        raw = self.explicit_channels or configured_channel_names(self.settings)
        out: list[str] = []
        for name in raw:
            if name in KNOWN_CHANNELS and name not in out:
                out.append(name)
        return out or ["cli"]

    def _start_channel(self, name: str) -> None:
        if not self._acquire_lock(name):
            return  # another daemon owns this IM channel → stay task-only
        channel_list = build_channels([name], self.settings, self.agent)
        if not channel_list:
            self._release_lock(name)
            self._health[name] = {"status": "not_configured", "reason": "unknown channel"}
            return
        channel = channel_list[0]
        self._channels[name] = channel
        self._health[name] = {"status": "running", "reason": "starting"}
        self._fingerprints[name] = self._fingerprint(name)
        self._tasks[name] = asyncio.create_task(self._run_channel(name, channel), name=f"channel:{name}")
        logger.info("channel %s adapter starting", name)

    def _acquire_lock(self, name: str) -> bool:
        """Hold the home-level lock before binding an IM channel.

        Returns ``True`` when this daemon owns (or already held) the channel and
        may bind it; ``False`` when a live foreign daemon owns it, in which case
        the channel is marked degraded (task-only) and retried later so it can
        take over if the owner exits.
        """
        if name not in _LOCKED_CHANNELS or name in self._locks:
            return True
        channels_dir = self.settings.paths.channels_dir
        lock = channel_locks.acquire(
            channels_dir, name, project_dir=str(self.settings.paths.project_dir)
        )
        if lock is None:
            owner = channel_locks.lock_owner(channels_dir, name)
            reason = (
                f"another omni serve owns this channel (pid {owner}); this process is task-only"
                if owner
                else "another omni serve owns this channel; this process is task-only"
            )
            self._health[name] = {"status": "degraded", "reason": reason}
            self._retry_after[name] = time.time() + self.retry_interval
            # Expected when another daemon machine-wide already owns this IM
            # channel: this process stays task-only and retries later. Kept at
            # DEBUG so it doesn't clutter the log file / confuse users; the
            # degraded state is still visible via the health snapshot / status.
            logger.debug("channel %s not bound: %s", name, reason)
            return False
        self._locks[name] = lock
        logger.info("channel %s home-lock acquired (pid %s)", name, lock.pid)
        return True

    def _release_lock(self, name: str) -> None:
        lock = self._locks.pop(name, None)
        if lock is not None:
            channel_locks.release(lock)
            logger.info("channel %s home-lock released", name)

    def _config_changed(self, name: str) -> bool:
        """True when ``name``'s stored credentials/config differ from start time."""
        return self._fingerprints.get(name) != self._fingerprint(name)

    def _fingerprint(self, name: str) -> str:
        """Cheap, per-channel credential/config fingerprint (no keychain I/O).

        Built from this channel's own ``<name>.toml`` (content + mtime) and its
        ``[channels.<name>]`` secrets section. ``login`` always rewrites
        ``<name>.toml`` (mtime bump), so this flips on re-login even for
        keychain-backed secrets whose ref string is unchanged. Crucially it never
        reads the *global* secrets mtime, so logging into a different channel
        cannot perturb this one's fingerprint — only the changed channel reloads.
        """
        paths = self.settings.paths
        cfg_path = paths.channels_dir / f"{name}.toml"
        cfg = _read_toml(cfg_path)
        secrets = _read_toml(paths.secrets_file).get("channels", {})
        sec = secrets.get(name, {}) if isinstance(secrets, dict) else {}
        payload = {"cfg": cfg, "sec": sec, "cfg_mtime": _safe_mtime(cfg_path)}
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _reload_mtime(self) -> float:
        return _safe_mtime(self.settings.paths.channels_dir / _RELOAD_SENTINEL)

    def _reload_requested(self) -> bool:
        """True once after the reload sentinel is touched; consumes the signal."""
        mtime = self._reload_mtime()
        if mtime > self._reload_seen:
            self._reload_seen = mtime
            return True
        return False

    async def _run_channel(self, name: str, channel: Any) -> None:
        try:
            await channel.start()
            if not self._stopping:
                self._health[name] = {"status": "degraded", "reason": "adapter exited"}
                self._retry_after[name] = time.time() + self.retry_interval
                logger.warning("channel %s adapter exited; will retry later", name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._stopping:
                reason = _short_error(exc)
                self._health[name] = {"status": "degraded", "reason": reason}
                self._retry_after[name] = time.time() + self.retry_interval
                logger.warning("channel %s adapter degraded: %s", name, reason)
                logger.debug("channel %s adapter error", name, exc_info=True)

    async def _stop_channel(self, name: str, *, reason: str) -> None:
        task = self._tasks.pop(name, None)
        channel = self._channels.pop(name, None)
        self._fingerprints.pop(name, None)
        had_runtime = task is not None or channel is not None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if channel is not None:
            try:
                await channel.stop()
            except Exception:  # noqa: BLE001
                logger.debug("channel %s stop failed", name, exc_info=True)
        self._release_lock(name)
        if had_runtime and reason:
            logger.info("channel %s stopped: %s", name, reason)


def request_reload(channels_dir: Path) -> None:
    """Ask any running ``omni serve`` to reconcile channels promptly.

    Writes a home-level sentinel (``~/.omni/channels/.reload``) that every
    daemon's :class:`ChannelManager` polls. It only requests an *immediate*
    reconcile; which channel actually rebuilds is still decided per-channel by
    the config fingerprint, so other live channels keep running. Cross-platform
    (no signals) and best-effort — failures are swallowed.
    """
    try:
        channels_dir.mkdir(parents=True, exist_ok=True)
        (channels_dir / _RELOAD_SENTINEL).write_text(f"{time.time()}", encoding="utf-8")
    except OSError:
        pass


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _enabled_from_config(path: Path) -> list[str]:
    if not path.is_file():
        return ["cli"]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ["cli"]
    channels = data.get("channels", {}) if isinstance(data, dict) else {}
    enabled = channels.get("enabled", ["cli"]) if isinstance(channels, dict) else ["cli"]
    if isinstance(enabled, str):
        return [part.strip() for part in enabled.split(",") if part.strip()]
    if isinstance(enabled, list):
        return [str(part).strip() for part in enabled if str(part).strip()]
    return ["cli"]


def _missing_dependency(name: str, data: dict[str, Any]) -> str:
    mode = str(data.get("mode") or "")
    if name == "feishu" and mode != "gateway" and importlib.util.find_spec("lark_oapi") is None:
        return "missing dependency lark-oapi"
    if name == "dingtalk" and mode != "gateway" and importlib.util.find_spec("dingtalk_stream") is None:
        return "missing dependency dingtalk-stream"
    return ""


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    if len(text) > 220:
        text = text[:217].rstrip() + "..."
    return text


__all__ = [
    "ChannelManager",
    "KNOWN_CHANNELS",
    "channel_config_state",
    "channel_runtime_state",
    "configured_channel_names",
    "request_reload",
]
