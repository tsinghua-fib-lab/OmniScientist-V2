"""High-level lifecycle for the home background service.

Framework-independent operations (``enable`` / ``disable`` / ``start`` / ``stop``
/ ``restart`` / ``ensure`` / ``status`` / ``doctor``) shared by the CLI command,
the bare-launch repair hook and the service-aware ``omni update``. Each mutating
operation is idempotent and takes the home :func:`~omni.runtime.service_state.lifecycle_lock`
so two lifecycle paths never race.

The persisted *desired state* is authoritative for "should the service be up":
``enable``/``disable`` set it; every other operation reconciles the observed
runtime toward it. This is what lets ``omni update`` restore exactly the state
the user chose — and lets a bare ``omni`` repair a crashed-but-enabled service
without ever resurrecting one the user disabled.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from types import TracebackType
from typing import Any

from omni import __version__
from omni.config import OmniSettings
from omni.runtime import service_state
from omni.runtime.dist_meta import DIST_NAME
from omni.runtime.service_state import ServiceDesiredState, lifecycle_lock
from omni.runtime.service_supervisors import (
    DefinitionStatus,
    Supervisor,
    SupervisorSpec,
    describe_platform,
    make_supervisor,
    select_supervisor_class,
)


@dataclass
class LifecycleResult:
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _LaunchOutcome:
    ok: bool
    detail: str
    installed: bool = False


def _log_path(settings: OmniSettings) -> Path:
    return settings.paths.logs_dir / "home-service.log"


def _service_env(settings: OmniSettings) -> dict[str, str]:
    """Environment pinned onto the supervised unit.

    ``OMNI_HOME`` binds the unit to this exact data directory (so a custom home
    survives login restarts), and ``PATH`` is carried so sandbox/compute helpers
    the service shells out to remain resolvable outside an interactive shell.

    ``HOME`` is snapshotted at compose time so a delayed ensure thread that
    outlives a test monkeypatch cannot rewrite LaunchAgent paths under the
    real user home while still pointing ``OMNI_HOME`` at a throwaway tree.
    """
    import os
    from pathlib import Path

    env = {"OMNI_HOME": str(settings.paths.home)}
    if path := os.environ.get("PATH"):
        env["PATH"] = path
    home = os.environ.get("HOME") or str(Path.home())
    if home:
        env["HOME"] = home
    if xdg_config_home := os.environ.get("XDG_CONFIG_HOME"):
        env["XDG_CONFIG_HOME"] = xdg_config_home
    return env


def _spec(settings: OmniSettings, *, launcher: list[str] | None = None) -> SupervisorSpec:
    argv = list(launcher) if launcher else service_state.default_launcher(settings.paths)
    return SupervisorSpec(
        paths=settings.paths,
        argv=argv,
        workdir=settings.paths.home,
        log_path=_log_path(settings),
        env=_service_env(settings),
    )


# Control-plane wait after a launch/restore. Channel HTTP (WeChat notify_start,
# Feishu WS, DingTalk Stream) is *not* part of this budget: STARTING with a live
# owner is enough for update/restart to succeed; READY is preferred, not required.
_DEFAULT_READY_WAIT_S = 20.0


def _wait_running(settings: OmniSettings, timeout_s: float) -> bool:
    deadline = time.time() + max(0.0, timeout_s)
    while time.time() < deadline:
        if service_state.service_is_ready(settings.paths):
            return True
        time.sleep(0.2)
    return service_state.service_is_ready(settings.paths)


def _log_tail(path: Path, *, lines: int = 12) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    body = text.splitlines()[-max(1, lines) :]
    return "\n".join(body)


def _observation_summary(settings: OmniSettings) -> str:
    observation = service_state.observe_service(settings.paths)
    runtime = observation.runtime or service_state.read_runtime(settings.paths) or {}
    pid = observation.pid if observation.pid is not None else runtime.get("pid") or "none"
    version = runtime.get("version") or "unknown"
    return (
        f"phase={observation.phase} pid={pid} version={version} "
        f"ready={bool(runtime.get('ready'))}"
    )


def _restore_diagnostics(settings: OmniSettings, prefix: str) -> str:
    parts = [prefix, _observation_summary(settings)]
    log_path = _log_path(settings)
    tail = _log_tail(log_path)
    if tail:
        parts.append(f"{log_path} last lines:\n{tail}")
    return "\n".join(parts)


def _wait_restore(
    settings: OmniSettings, timeout_s: float
) -> tuple[bool, bool, service_state.ServiceObservation]:
    """Poll until control-plane READY, or until the deadline.

    Returns ``(claimed, ready, last_observation)``. Claimed means the process
    published ``starting`` or ``ready`` — the gateway has taken ownership.
    A timeout still-starting is a successful claim, not a launch failure.

    ``down`` before the first claim is "launchd has not spawned yet", not a
    crash. Failing fast on that race made first launch after install report
    ``phase=down pid=none`` even when the unit was about to come up.
    """
    paths = settings.paths
    last = service_state.observe_service(paths)
    seen_claim = last.phase in {"starting", "ready"} or _service_active(settings)
    if timeout_s <= 0:
        claimed = seen_claim
        return claimed, last.phase == "ready", last
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        last = service_state.observe_service(paths)
        if last.phase == "ready":
            return True, True, last
        if last.phase in {"starting", "ready"} or _service_active(settings):
            seen_claim = True
        if last.phase in {"down", "stale"} and seen_claim and not _service_active(settings):
            return False, False, last
        time.sleep(0.2)
    last = service_state.observe_service(paths)
    claimed = last.phase in {"starting", "ready"} or _service_active(settings)
    return claimed, last.phase == "ready", last


def _still_becoming_ready_detail(phase: str, *, verb: str) -> str:
    return (
        f"{verb}; still becoming ready (phase={phase}). "
        "Channels will reconnect. See `omni serve status`."
    )


def _wait_active(settings: OmniSettings, timeout_s: float) -> bool:
    """Wait until a launched child is attributable to this home.

    The process-table marker closes the last pre-singleton window: a detached
    child can exist for a short time while Python imports the CLI, before
    :class:`HomeService` has acquired and published singleton ownership.
    """
    deadline = time.time() + max(0.0, timeout_s)
    while time.time() < deadline:
        if _service_active(settings):
            return True
        time.sleep(0.1)
    return _service_active(settings)


def _wait_down(settings: OmniSettings, timeout_s: float = 3.0) -> bool:
    deadline = time.time() + max(0.0, timeout_s)
    while time.time() < deadline:
        if not _service_active(settings):
            return True
        time.sleep(0.1)
    return not _service_active(settings)


def _supervisor_quiescent(supervisor: Supervisor) -> bool:
    try:
        check = getattr(supervisor, "is_quiescent", None)
        if callable(check):
            return bool(check())
        return supervisor.status() != "running"
    except Exception:  # noqa: BLE001 - failure to verify must fail closed.
        return False


def _supervisor_status(supervisor: Supervisor) -> str:
    try:
        return str(supervisor.status())
    except Exception:  # noqa: BLE001 - unknown means "do not assume installed".
        return "unknown"


def _definition_status(supervisor: Supervisor) -> DefinitionStatus:
    try:
        return supervisor.definition_status()
    except Exception:  # noqa: BLE001 - probe failure must not imply drift.
        return DefinitionStatus.UNKNOWN


def _wait_stably_quiescent(
    settings: OmniSettings,
    supervisor: Supervisor,
    *,
    timeout_s: float = 3.0,
) -> bool:
    """Require both manager and process ownership to remain down across polls."""
    deadline = time.time() + max(0.0, timeout_s)
    stable_polls = 0
    while time.time() < deadline:
        if _supervisor_quiescent(supervisor) and not _service_active(settings):
            stable_polls += 1
            if stable_polls >= 3:
                return True
        else:
            stable_polls = 0
        time.sleep(0.1)
    return (
        stable_polls >= 3
        and _supervisor_quiescent(supervisor)
        and not _service_active(settings)
    )


def _service_active(
    settings: OmniSettings,
    *,
    supervisor: Supervisor | None = None,
) -> bool:
    """Whether the current home owns, spawned, or supervises a service."""
    from omni.runtime.daemon import scan_running_serve_pids

    paths = settings.paths
    if service_state.service_is_active(paths):
        return True
    if scan_running_serve_pids(service_id=service_state.service_instance_id(paths)):
        return True
    if supervisor is not None:
        try:
            return supervisor.status() == "running"
        except Exception:  # noqa: BLE001 - status is a best-effort fallback.
            return False
    return False


def _service_converging(settings: OmniSettings) -> bool:
    """Whether a healthy owner exists or a marked child is claiming ownership."""
    from omni.runtime.daemon import scan_running_serve_pids

    observation = service_state.observe_service(settings.paths)
    if observation.phase in {"starting", "ready"}:
        return True
    if observation.phase in {"stopping", "unhealthy"}:
        return False
    return bool(
        scan_running_serve_pids(
            service_id=service_state.service_instance_id(settings.paths)
        )
    )


def _reap_legacy_daemons(settings: OmniSettings) -> list[int]:
    """Retire legacy per-workspace daemons so the home service supersedes them.

    Bringing the home service up (``enable``/``start``) should deterministically
    stop any historical per-workspace ``omni serve`` that would otherwise keep
    the channel lock and double-drive schedules — regardless of whether a fresh
    home-service process starts. Best-effort; never raises.
    """
    from omni.runtime.daemon import stop_legacy_daemons

    try:
        return stop_legacy_daemons(settings.paths.home)
    except Exception:  # noqa: BLE001 - reaping is best-effort hygiene.
        return []


def _reap_running_serve(settings: OmniSettings) -> list[int]:
    """Reap only processes attributable to this ``OMNI_HOME``."""
    from omni.runtime import daemon

    paths = settings.paths
    service_id = service_state.service_instance_id(paths)
    pids = set(
        daemon.scan_running_serve_pids(service_id=service_id)
    )
    holder = service_state.singleton_holder_info(paths) or {}
    if holder.get("role") != "update":
        try:
            holder_pid = int(holder.get("pid", 0) or 0)
        except (TypeError, ValueError):
            holder_pid = 0
        if holder_pid > 0:
            pids.add(holder_pid)
    return daemon.reap_serve_processes(sorted(pids)) if pids else []


def _stop_locked(
    settings: OmniSettings,
    *,
    supervisor: Supervisor | None = None,
) -> tuple[bool, str]:
    desired = service_state.read_desired(settings.paths)
    supervisor = supervisor or make_supervisor(
        _spec(settings, launcher=desired.launcher or None), desired.manager
    )
    ok, detail = supervisor.stop()
    reaped = _reap_running_serve(settings)
    quiescent = _wait_stably_quiescent(settings, supervisor)
    if not quiescent:
        return (
            False,
            f"{detail}; supervisor/current-home service did not become quiescent",
        )
    service_state.clear_runtime(settings.paths)
    if reaped:
        detail = f"{detail}; reaped pids={','.join(str(pid) for pid in reaped)}"
    if not ok:
        detail = f"{detail}; converged after the initial stop warning"
    return True, detail


def _activate_locked(
    settings: OmniSettings,
    desired: ServiceDesiredState,
    *,
    supervisor: Supervisor | None = None,
    claim_wait_s: float = 8.0,
) -> _LaunchOutcome:
    """Install/register the OS unit and perform one launch-producing action.

    Used for first-time enable, manager migration, and post-update unit refresh.
    Day-to-day repair must use :func:`_start_locked` so bare ``omni`` does not
    rewrite LaunchAgents on every launch.
    """
    _reap_legacy_daemons(settings)
    supervisor = supervisor or make_supervisor(
        _spec(settings, launcher=desired.launcher), desired.manager
    )
    ok, detail = supervisor.activate()
    if not ok:
        return _LaunchOutcome(False, detail)
    if not _wait_active(settings, claim_wait_s):
        _stop_locked(settings, supervisor=supervisor)
        return _LaunchOutcome(
            False,
            f"{detail}; service process did not claim singleton ownership",
        )
    return _LaunchOutcome(True, detail, installed=True)


def _start_locked(
    settings: OmniSettings,
    desired: ServiceDesiredState,
    *,
    supervisor: Supervisor | None = None,
    claim_wait_s: float = 8.0,
) -> _LaunchOutcome:
    """Kick an already-installed unit, installing only when objectively absent."""
    _reap_legacy_daemons(settings)
    supervisor = supervisor or make_supervisor(
        _spec(settings, launcher=desired.launcher), desired.manager
    )
    if (
        _supervisor_status(supervisor) == "not-installed"
        or _definition_status(supervisor) is DefinitionStatus.MISMATCHED
    ):
        return _activate_locked(
            settings,
            desired,
            supervisor=supervisor,
            claim_wait_s=claim_wait_s,
        )
    ok, detail = supervisor.start()
    if not ok:
        # The unit may have been removed after the first status probe. Reinstall
        # only for that concrete race; permission/config failures remain errors.
        if _supervisor_status(supervisor) == "not-installed":
            return _activate_locked(
                settings,
                desired,
                supervisor=supervisor,
                claim_wait_s=claim_wait_s,
            )
        return _LaunchOutcome(False, detail)
    if not _wait_active(settings, claim_wait_s):
        _stop_locked(settings, supervisor=supervisor)
        return _LaunchOutcome(
            False,
            f"{detail}; service process did not claim singleton ownership",
        )
    return _LaunchOutcome(True, detail)


def _refresh_desired(paths, desired: ServiceDesiredState) -> ServiceDesiredState:  # noqa: ANN001
    """Pin the next activation to the currently executing CLI installation."""
    desired.launcher = service_state.default_launcher(paths)
    try:
        desired.version = metadata.version(DIST_NAME)
    except metadata.PackageNotFoundError:
        desired.version = __version__
    desired.last_error = ""
    service_state.write_desired(paths, desired)
    return desired


def enable(
    settings: OmniSettings,
    *,
    manager: str = "auto",
    channels: bool = True,
    wait_s: float = _DEFAULT_READY_WAIT_S,
) -> LifecycleResult:
    """Persist ``enabled=True`` and bring the home service up.

    The observed supervisor state — not ``desired.configured`` — decides whether
    this is an install or a start. Missing or drifted units are installed;
    matching units are only kicked.
    """
    del channels  # Channel selection is dynamically reconciled by the home service.
    paths = settings.paths
    paths.service_dir.mkdir(parents=True, exist_ok=True)
    with lifecycle_lock(paths):
        sup_cls = select_supervisor_class(manager)
        desired = service_state.read_desired(paths)
        old_manager = desired.manager
        old_launcher = list(desired.launcher)
        active = _service_active(settings)
        converging = _service_converging(settings)
        old_manager_id = select_supervisor_class(old_manager).id
        manager_changed = desired.configured and old_manager_id != sup_cls.id
        current_launcher = service_state.default_launcher(paths)
        supervisor = make_supervisor(
            _spec(settings, launcher=current_launcher),
            sup_cls.id,
        )
        needs_install = (
            manager_changed
            or _supervisor_status(supervisor) == "not-installed"
            or _definition_status(supervisor) is DefinitionStatus.MISMATCHED
        )

        # A healthy/starting unit under the requested manager is already
        # converged. Rewriting its unit can itself launch or restart it on
        # launchd/systemd, so persist the refreshed intent without touching the
        # host supervisor.
        if converging and old_manager_id == sup_cls.id and not needs_install:
            desired.enabled = True
            desired.configured = True
            desired.manager = sup_cls.id
            desired.channel_anchor = desired.channel_anchor or "default"
            _refresh_desired(paths, desired)
            claimed, running, observation = _wait_restore(settings, wait_s)
            if _service_active(settings):
                service_state.clear_start_request(paths)
            if running:
                detail = "Home service is already running."
            elif claimed:
                detail = _still_becoming_ready_detail(
                    observation.phase, verb="Home service is already active"
                )
            else:
                detail = _restore_diagnostics(
                    settings,
                    "Home service is active but did not become ready.",
                )
            return LifecycleResult(
                claimed or running,
                detail,
                {
                    "manager": sup_cls.id,
                    "running": running,
                    "active": _service_active(settings),
                    "phase": observation.phase,
                },
            )

        if active or manager_changed:
            old_supervisor = make_supervisor(
                _spec(settings, launcher=old_launcher or None), old_manager
            )
            if active or not _supervisor_quiescent(old_supervisor):
                stopped, stop_detail = _stop_locked(
                    settings, supervisor=old_supervisor
                )
                if not stopped:
                    return LifecycleResult(
                        False,
                        f"Could not replace the active home service: {stop_detail}",
                        {"manager": old_manager, "running": False, "active": True},
                    )
            if manager_changed:
                removed, remove_detail = old_supervisor.uninstall()
                if not removed:
                    return LifecycleResult(
                        False,
                        f"Could not retire the previous service manager: {remove_detail}",
                        {
                            "manager": old_manager,
                            "running": False,
                            "active": False,
                        },
                    )

        desired.enabled = True
        desired.configured = True
        desired.manager = sup_cls.id
        desired.channel_anchor = desired.channel_anchor or "default"
        _refresh_desired(paths, desired)
        if needs_install:
            outcome = _activate_locked(
                settings, desired, supervisor=supervisor
            )
        else:
            outcome = _start_locked(
                settings, desired, supervisor=supervisor
            )
        claimed, running, observation = (
            _wait_restore(settings, wait_s)
            if outcome.ok
            else (False, False, service_state.observe_service(paths))
        )
        ok = outcome.ok and claimed
        if not ok:
            desired.last_error = (
                f"activate={outcome.detail}; {_observation_summary(settings)}"
            )
            service_state.write_desired(paths, desired)
        else:
            service_state.clear_start_request(paths)
    if ok and running:
        detail = f"Home service enabled via {supervisor.id}."
    elif ok:
        detail = _still_becoming_ready_detail(
            observation.phase,
            verb=f"Home service enabled via {supervisor.id}",
        )
    else:
        detail = _restore_diagnostics(
            settings,
            "Home service configured (enabled) but did not confirm ready: "
            f"{outcome.detail}",
        )
    return LifecycleResult(
        ok,
        detail,
        {
            "manager": supervisor.id,
            "running": running,
            "active": _service_active(settings),
            "installed": outcome.installed,
            "phase": observation.phase,
        },
    )


def lazy_enable(settings: OmniSettings, *, reason: str = "", wait_s: float = 6.0) -> LifecycleResult:
    """Guarantee the always-on home service is up (install once, else repair).

    Callers: bare ``omni`` (``reason="launch"``), channel ``--start``, schedule
    create. Contract:

    * **Ephemeral ``OMNI_HOME``** (pytest/tmp): never register a durable OS unit.
    * **First need** on a real home (``configured=False``): ``enable`` → install
      once + start (preserves "first ``omni`` brings home service up").
    * **Already configured + enabled**: ``ensure`` → kick existing unit only.
    * **Configured but disabled**: ``enable`` re-enables; same manager does not
      rewrite the host unit (``start``), matching the always-on model where a
      prior ``omni serve stop`` is only a transient pause when a feature needs
      the service again.
    """
    from omni.runtime.service_supervisors import (
        allows_ephemeral_host_service,
        is_ephemeral_omni_home,
    )

    paths = settings.paths
    # Bare-launch ensure must never mint host units for throwaway homes.
    # Feature triggers (channel/schedule) still go through enable/ensure so
    # unit tests can mock them; durable install remains refused at the
    # supervisor boundary for ephemeral OMNI_HOME.
    if (
        reason == "launch"
        and is_ephemeral_omni_home(paths.home)
        and not allows_ephemeral_host_service()
    ):
        service_state.clear_start_request(paths)
        return LifecycleResult(
            True,
            "skipped durable home-service install for ephemeral OMNI_HOME",
            {"skipped": True, "ephemeral": True, "reason": reason},
        )

    service_state.request_start(paths)
    desired = service_state.read_desired(paths)
    if not desired.configured:
        # First bare omni / first feature trigger on this home: install once.
        return enable(
            settings,
            manager=settings.service.manager,
            wait_s=wait_s,
        )
    if desired.enabled:
        return ensure(settings, wait_s=wait_s)
    # Explicit disable or never-enabled-after-configure: bring back via enable
    # (start path when the manager is unchanged).
    return enable(
        settings,
        manager=desired.manager or settings.service.manager,
        wait_s=wait_s,
    )


def disable(settings: OmniSettings) -> LifecycleResult:
    """Persist ``enabled=False`` and stop + uninstall the supervised service."""
    paths = settings.paths
    with lifecycle_lock(paths):
        desired = service_state.read_desired(paths)
        desired.enabled = False
        desired.configured = True
        service_state.write_desired(paths, desired)
        service_state.clear_start_request(paths)
        supervisor = make_supervisor(_spec(settings, launcher=desired.launcher), desired.manager)
        stopped, stop_detail = _stop_locked(settings, supervisor=supervisor)
        removed, remove_detail = supervisor.uninstall()
    ok = stopped and removed
    return LifecycleResult(
        ok,
        (
            "Home service disabled and stopped."
            if ok
            else f"Home service disable was incomplete: {stop_detail}; {remove_detail}"
        ),
        {"manager": supervisor.id},
    )


def start(settings: OmniSettings, *, wait_s: float = _DEFAULT_READY_WAIT_S) -> LifecycleResult:
    """Start the service now. Requires it to be enabled (idempotent if active)."""
    paths = settings.paths
    desired = service_state.read_desired(paths)
    if not desired.enabled:
        return LifecycleResult(
            False,
            "Home service is disabled. Run `omni serve start` first.",
            {"enabled": False},
        )
    with lifecycle_lock(paths):
        desired = service_state.read_desired(paths)
        if not desired.enabled:
            return LifecycleResult(
                False,
                "Home service is disabled. Run `omni serve start` first.",
                {"enabled": False},
            )
        if _service_converging(settings):
            claimed, running, observation = _wait_restore(settings, wait_s)
            service_state.clear_start_request(paths)
            if running:
                detail = "Home service is already running."
            elif claimed:
                detail = _still_becoming_ready_detail(
                    observation.phase, verb="Home service is already active"
                )
            else:
                detail = _restore_diagnostics(
                    settings,
                    "Home service is active but did not become ready.",
                )
            return LifecycleResult(
                claimed or running,
                detail,
                {
                    "running": running,
                    "active": True,
                    "phase": observation.phase,
                },
            )
        _refresh_desired(paths, desired)
        supervisor = make_supervisor(
            _spec(settings, launcher=desired.launcher), desired.manager
        )
        if _service_active(settings) or not _supervisor_quiescent(supervisor):
            stopped, stop_detail = _stop_locked(
                settings, supervisor=supervisor
            )
            if not stopped:
                return LifecycleResult(
                    False,
                    f"Home service could not replace an unhealthy owner: {stop_detail}",
                    {"running": False, "active": True},
                )
        outcome = _start_locked(
            settings, desired, supervisor=supervisor
        )
        claimed, running, observation = (
            _wait_restore(settings, wait_s)
            if outcome.ok
            else (False, False, service_state.observe_service(paths))
        )
        if outcome.ok:
            service_state.clear_start_request(paths)
    ok = outcome.ok and claimed
    if ok and running:
        detail = "Home service started."
    elif ok:
        detail = _still_becoming_ready_detail(
            observation.phase, verb="Home service started"
        )
    else:
        detail = _restore_diagnostics(
            settings,
            f"Home service did not confirm ready: {outcome.detail}",
        )
    return LifecycleResult(
        ok,
        detail,
        {
            "running": running,
            "active": outcome.ok,
            "installed": outcome.installed,
            "phase": observation.phase,
        },
    )


def stop(settings: OmniSettings) -> LifecycleResult:
    """Stop the running service now, leaving the desired state unchanged.

    A transient pause (unlike ``disable``): a later ``ensure`` / ``update`` will
    bring an enabled service back.
    """
    paths = settings.paths
    with lifecycle_lock(paths):
        desired = service_state.read_desired(paths)
        supervisor = make_supervisor(_spec(settings, launcher=desired.launcher), desired.manager)
        ok, detail = _stop_locked(settings, supervisor=supervisor)
    return LifecycleResult(ok, detail)


def restart(settings: OmniSettings, *, wait_s: float = _DEFAULT_READY_WAIT_S) -> LifecycleResult:
    """Atomically stop current-home owners, then perform one fresh activation."""
    paths = settings.paths
    with lifecycle_lock(paths):
        desired = service_state.read_desired(paths)
        if not desired.enabled:
            return LifecycleResult(
                False,
                "Home service is disabled. Run `omni serve start` first.",
                {"enabled": False},
            )
        stop_supervisor = make_supervisor(
            _spec(settings, launcher=desired.launcher or None), desired.manager
        )
        stopped, stop_detail = _stop_locked(
            settings, supervisor=stop_supervisor
        )
        if not stopped:
            return LifecycleResult(False, f"Home service restart failed: {stop_detail}")
        _refresh_desired(paths, desired)
        start_supervisor = make_supervisor(
            _spec(settings, launcher=desired.launcher), desired.manager
        )
        outcome = _activate_locked(
            settings, desired, supervisor=start_supervisor
        )
        claimed, running, observation = (
            _wait_restore(settings, wait_s)
            if outcome.ok
            else (False, False, service_state.observe_service(paths))
        )
        if outcome.ok:
            service_state.clear_start_request(paths)
    ok = stopped and outcome.ok and claimed
    if ok and running:
        detail = "Home service restarted on current code."
    elif ok:
        detail = _still_becoming_ready_detail(
            observation.phase, verb="Home service restarted"
        )
    else:
        detail = _restore_diagnostics(
            settings,
            f"Home service restart did not confirm ready: {outcome.detail}",
        )
    return LifecycleResult(
        ok,
        detail,
        {
            "running": running,
            "active": outcome.ok,
            "installed": outcome.installed,
            "phase": observation.phase,
        },
    )


def ensure(settings: OmniSettings, *, wait_s: float = 0.0) -> LifecycleResult:
    """Reconcile observed → desired: kick an enabled-but-down service.

    Bounded and side-effect-light so a bare ``omni`` can call it. Does nothing
    when the service is disabled or already running (the common path), so it adds
    no latency to normal launches. Uses ``start`` (not ``activate``/install) so
    repair cannot mint a new LaunchAgent. ``wait_s=0`` returns immediately after
    kicking the supervisor (fire-and-forget repair).
    """
    paths = settings.paths
    desired = service_state.read_desired(paths)
    if not desired.enabled:
        return LifecycleResult(True, "Home service disabled; nothing to ensure.", {"enabled": False})
    if _service_converging(settings):
        service_state.clear_start_request(paths)
        return LifecycleResult(
            True,
            "Home service already active.",
            {
                "running": service_state.service_is_ready(paths),
                "active": True,
            },
        )
    with lifecycle_lock(paths, timeout_s=30.0):
        # Re-check under the lock: another launcher may have just started it.
        desired = service_state.read_desired(paths)
        if not desired.enabled:
            return LifecycleResult(
                True, "Home service disabled; nothing to ensure.", {"enabled": False}
            )
        if _service_converging(settings):
            service_state.clear_start_request(paths)
            return LifecycleResult(
                True,
                "Home service already active.",
                {
                    "running": service_state.service_is_ready(paths),
                    "active": True,
                },
            )
        previous_supervisor = make_supervisor(
            _spec(settings, launcher=desired.launcher or None), desired.manager
        )
        if _service_active(settings) or not _supervisor_quiescent(
            previous_supervisor
        ):
            stopped, stop_detail = _stop_locked(
                settings, supervisor=previous_supervisor
            )
            if not stopped:
                return LifecycleResult(
                    False,
                    f"Home service repair could not stop the unhealthy owner: {stop_detail}",
                    {"running": False, "active": True},
                )
        _refresh_desired(paths, desired)
        supervisor = make_supervisor(
            _spec(settings, launcher=desired.launcher), desired.manager
        )
        outcome = _start_locked(
            settings, desired, supervisor=supervisor
        )
        running = (
            _wait_running(settings, wait_s)
            if outcome.ok and wait_s > 0
            else service_state.service_is_ready(paths)
        )
        if outcome.ok:
            service_state.clear_start_request(paths)
    return LifecycleResult(
        outcome.ok,
        (
            "Home service repair started."
            if outcome.ok
            else f"Home service repair failed: {outcome.detail}"
        ),
        {
            "running": running,
            "active": outcome.ok,
            "installed": outcome.installed,
        },
    )


class ServiceUpdateGuard:
    """Serialize an update with service quiescence and state restoration."""

    def __init__(
        self,
        settings: OmniSettings,
        *,
        restart_serve: bool,
        ready_wait_s: float = _DEFAULT_READY_WAIT_S,
    ) -> None:
        self.settings = settings
        self.restart_serve = restart_serve
        self.ready_wait_s = ready_wait_s
        self.was_active = False
        self._lock: service_state.LifecycleLock | None = None
        self._reservation_fd: int | None = None
        self._desired = ServiceDesiredState()
        self._restore_attempted = False
        self._restored = False
        self._detail = ""

    def _release_reservation(self) -> None:
        if self._reservation_fd is None:
            return
        service_state.release_singleton(self._reservation_fd)
        self._reservation_fd = None

    def _reserve_singleton(self) -> None:
        """Prevent direct/old launchers from entering during package replacement."""
        paths = self.settings.paths
        self._reservation_fd = service_state.acquire_singleton(
            paths, role="update"
        )
        if self._reservation_fd is not None:
            return
        # A direct or old fail-open launcher slipped in after quiescence. It is
        # attributable through the singleton itself, so reap it and retry once.
        _reap_running_serve(self.settings)
        if not _wait_down(self.settings, timeout_s=3.0):
            raise RuntimeError(
                "Could not reserve home-service singleton for the update."
            )
        self._reservation_fd = service_state.acquire_singleton(
            paths, role="update"
        )
        if self._reservation_fd is None:
            raise RuntimeError(
                "Could not reserve home-service singleton for the update."
            )

    def _activate_after_update(self, desired: ServiceDesiredState) -> str:
        paths = self.settings.paths
        requested = service_state.start_requested(paths)
        if requested:
            desired.enabled = True
            desired.configured = True
        if not desired.enabled:
            return "was active despite being disabled; left stopped after the update."

        desired.manager = select_supervisor_class(desired.manager).id
        _refresh_desired(paths, desired)
        self._release_reservation()
        supervisor = make_supervisor(
            _spec(self.settings, launcher=desired.launcher), desired.manager
        )
        outcome = _activate_locked(
            self.settings, desired, supervisor=supervisor
        )
        if not outcome.ok:
            raise RuntimeError(
                _restore_diagnostics(
                    self.settings,
                    "Update installed, but the home service could not restart: "
                    f"{outcome.detail}",
                )
            )
        claimed, ready, observation = _wait_restore(
            self.settings, self.ready_wait_s
        )
        if not claimed:
            raise RuntimeError(
                _restore_diagnostics(
                    self.settings,
                    "Update installed, but the restarted home service did not become ready.",
                )
            )
        service_state.clear_start_request(paths)
        if ready:
            return (
                "restarted on the updated code."
                if self.was_active
                else "started on the updated code for the pending bare `omni` launch."
            )
        verb = "restarted" if self.was_active else "started"
        return _still_becoming_ready_detail(observation.phase, verb=verb)

    def _compensate_failed_enter(
        self, supervisor: Supervisor
    ) -> BaseException | None:
        """Best-effort restoration when quiescence changed state before enter failed."""
        if (
            not self.was_active
            or _service_active(self.settings)
            or not _supervisor_quiescent(supervisor)
        ):
            return None
        self._release_reservation()
        desired = service_state.read_desired(self.settings.paths)
        if not desired.enabled:
            return None
        try:
            self._activate_after_update(desired)
        except BaseException as exc:  # noqa: BLE001 - report alongside root cause.
            return exc
        return None

    def __enter__(self) -> ServiceUpdateGuard:
        paths = self.settings.paths
        self._lock = lifecycle_lock(paths, timeout_s=30.0)
        self._lock.__enter__()
        supervisor: Supervisor | None = None
        try:
            self._desired = service_state.read_desired(paths)
            supervisor = make_supervisor(
                _spec(self.settings, launcher=self._desired.launcher or None),
                self._desired.manager,
            )
            self.was_active = _service_active(self.settings)
            if not self.was_active and self._desired.enabled:
                self.was_active = not _supervisor_quiescent(supervisor)
            # Always deactivate the manager, even when no process is currently
            # visible: a loaded launchd/systemd job could otherwise relaunch old
            # code during the install window.
            stopped, detail = _stop_locked(
                self.settings, supervisor=supervisor
            )
            if not stopped:
                raise RuntimeError(
                    f"Cannot quiesce the home service for update: {detail}"
                )
            self._reserve_singleton()
            return self
        except BaseException as exc:
            compensation_error = (
                self._compensate_failed_enter(supervisor)
                if supervisor is not None
                else None
            )
            self._release_reservation()
            self._lock.__exit__(None, None, None)
            self._lock = None
            if compensation_error is not None:
                raise RuntimeError(
                    f"{exc}; additionally failed to restore the prior service: "
                    f"{compensation_error}"
                ) from exc
            raise

    def restore(self) -> str:
        """Restore the pre-update active/down state exactly once."""
        if self._restored:
            return self._detail
        if self._restore_attempted:
            raise RuntimeError("Home-service restoration was already attempted.")
        self._restore_attempted = True

        paths = self.settings.paths
        if not self.restart_serve:
            self._detail = (
                "stopped after the update (--no-restart-serve); "
                "run `omni serve start` when ready."
                if self.was_active
                else ""
            )
            self._release_reservation()
            self._restored = True
            return self._detail
        requested = service_state.start_requested(paths)
        if not self.was_active and not requested:
            self._detail = (
                "not running; the next `omni` launch will start it on the new code."
                if service_state.read_desired(paths).enabled
                else ""
            )
            self._restored = True
            return self._detail

        self._detail = self._activate_after_update(
            service_state.read_desired(paths)
        )
        self._restored = True
        return self._detail

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        restore_error: BaseException | None = None
        if not self._restore_attempted:
            try:
                self.restore()
            except BaseException as restore_exc:  # noqa: BLE001 - preserve update error.
                restore_error = restore_exc
        elif (
            self._restored
            and self._reservation_fd is not None
            and service_state.start_requested(self.settings.paths)
        ):
            # A bare launch may record intent after an explicit restore() decided
            # to preserve DOWN but before this transaction releases its locks.
            self._restore_attempted = False
            self._restored = False
            try:
                self.restore()
            except BaseException as restore_exc:  # noqa: BLE001
                restore_error = restore_exc
        self._release_reservation()
        if self._lock is not None:
            self._lock.__exit__(exc_type, exc, traceback)
            self._lock = None
        if restore_error is not None:
            restore_detail = (
                "The update also failed to restore the prior home service: "
                f"{restore_error}"
            )
            try:
                desired = service_state.read_desired(self.settings.paths)
                desired.last_error = restore_detail
                service_state.write_desired(self.settings.paths, desired)
            except OSError:
                pass
            if exc is not None:
                exc._omni_service_restore_error = restore_detail  # type: ignore[attr-defined]
                add_note = getattr(exc, "add_note", None)
                if callable(add_note):
                    add_note(restore_detail)
        if exc_type is None and restore_error is not None:
            raise restore_error
        return False


def update_guard(
    settings: OmniSettings,
    *,
    restart_serve: bool,
    ready_wait_s: float = _DEFAULT_READY_WAIT_S,
) -> ServiceUpdateGuard:
    """Create the service-side transaction used by every update method."""
    return ServiceUpdateGuard(
        settings,
        restart_serve=restart_serve,
        ready_wait_s=ready_wait_s,
    )


def status(settings: OmniSettings) -> dict[str, Any]:
    """Structured status: desired state, live runtime, supervisor + platform."""
    from omni.runtime.daemon import scan_running_serve_pids

    paths = settings.paths
    desired = service_state.read_desired(paths)
    observation = service_state.observe_service(paths)
    runtime = observation.runtime
    supervisor = make_supervisor(_spec(settings, launcher=desired.launcher), desired.manager)
    try:
        sup_status = supervisor.status()
    except Exception:  # noqa: BLE001
        sup_status = "unknown"
    # Cross-check the process table: more than one live ``serve run`` means
    # duplicates are present (the very failure the singleton lock prevents going
    # forward, but pre-fix leftovers or another OMNI_HOME can still show here).
    serve_pids = scan_running_serve_pids(
        service_id=service_state.service_instance_id(paths)
    )
    return {
        "enabled": desired.enabled,
        "configured": desired.configured,
        "manager": desired.manager,
        "supervisor_status": sup_status,
        "running": observation.ready,
        "active": observation.active,
        "phase": observation.phase,
        "runtime": runtime,
        "channel_anchor": desired.channel_anchor or "default",
        "last_error": desired.last_error,
        "platform": describe_platform(),
        "serve_pids": serve_pids,
        "singleton_holder": service_state.singleton_holder_pid(paths),
    }


def doctor(settings: OmniSettings) -> dict[str, Any]:
    """Diagnostics: status plus drift detection and legacy-daemon findings."""
    from omni.runtime.daemon import list_running_daemons
    from omni.runtime.service_supervisors import list_orphan_launchd_agents

    snap = status(settings)
    findings: list[str] = []
    if snap["enabled"] and not snap["active"]:
        findings.append("Service is enabled but not running — run `omni serve start` or a bare `omni` to repair.")
    if not snap["enabled"] and snap["active"]:
        findings.append("Service is disabled but a runtime is live — run `omni serve stop`.")
    if snap["phase"] in {"unhealthy", "stopping"}:
        findings.append(
            f"Service owner is {snap['phase']} and not ready — run a bare `omni` "
            "or `omni serve restart` to repair it."
        )
    serve_pids = snap.get("serve_pids") or []
    if len(serve_pids) > 1:
        findings.append(
            f"{len(serve_pids)} `omni serve run` processes are live (expected 1): pids={serve_pids}. "
            "Run `omni serve stop --all` then `omni serve start` to converge on a single home service."
        )
    legacy = list_running_daemons(settings.paths.home)
    if legacy:
        findings.append(
            f"{len(legacy)} legacy per-workspace daemon(s) running; the home service will retire them on start."
        )
    orphans = list_orphan_launchd_agents()
    if orphans:
        findings.append(
            f"{len(orphans)} orphan LaunchAgent(s) point at missing/ephemeral OMNI_HOME — "
            "run `omni serve prune --orphans` to boot them out."
        )
    snap["legacy_daemons"] = [
        {"pid": d.get("pid"), "dir": d.get("project_dir")} for d in legacy
    ]
    snap["orphan_launchd_agents"] = orphans
    snap["findings"] = findings
    return snap


__all__ = [
    "LifecycleResult",
    "ServiceDesiredState",
    "ServiceUpdateGuard",
    "disable",
    "doctor",
    "enable",
    "ensure",
    "lazy_enable",
    "restart",
    "start",
    "status",
    "stop",
    "update_guard",
]
