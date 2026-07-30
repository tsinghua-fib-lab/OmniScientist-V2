"""Lifecycle control, launch-ensure gating, update reconcile, and onboarding.

Uses a fake OS supervisor so the whole state machine is exercised offline
without installing a launchd/systemd/schtasks unit or spawning a process. The
fake models the one behaviour ``service_control`` depends on: ``start`` makes the
runtime "running" (a fresh runtime-state row for this pid) and ``stop`` clears
it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from omni.config import load_settings
from omni.runtime import service_control, service_state


class FakeSupervisor:
    id = "detached"

    def __init__(self, spec) -> None:  # noqa: ANN001
        self.paths = spec.paths
        self.argv = list(spec.argv)
        self.calls: list[str] = []

    def install(self):
        self.calls.append("install")
        return True, "installed"

    def uninstall(self):
        self.calls.append("uninstall")
        return True, "removed"

    def start(self):
        self.calls.append("start")
        service_state.write_runtime(self.paths, {"ready": True, "version": "test"})
        return True, "started"

    def definition_status(self):
        return service_control.DefinitionStatus.MATCHES

    def activate(self):
        self.calls.append("activate")
        service_state.write_runtime(
            self.paths, {"ready": True, "phase": "ready", "version": "test"}
        )
        return True, "activated"

    def stop(self):
        self.calls.append("stop")
        service_state.clear_runtime_if_owner(self.paths)
        return True, "stopped"

    def status(self):
        return "running" if service_state.service_is_running(self.paths) else "stopped"


@pytest.fixture
def fake_supervisor(monkeypatch):
    made: list[FakeSupervisor] = []

    def _make(spec, manager="auto"):  # noqa: ANN001
        s = FakeSupervisor(spec)
        made.append(s)
        return s

    class _Cls:
        id = "detached"

    monkeypatch.setattr(service_control, "make_supervisor", _make)
    monkeypatch.setattr(service_control, "select_supervisor_class", lambda manager="auto": _Cls)
    return made


def test_enable_persists_desired_and_starts(fake_supervisor):
    settings = load_settings()
    result = service_control.enable(settings, manager="detached")
    assert result.ok is True
    desired = service_state.read_desired(settings.paths)
    assert desired.enabled is True
    assert desired.configured is True
    assert desired.manager == "detached"
    assert service_state.service_is_running(settings.paths) is True
    assert fake_supervisor[-1].calls == ["start"]
    assert result.data.get("installed") is False


def test_enable_reenables_with_start_not_reinstall(fake_supervisor):
    settings = load_settings()
    service_control.enable(settings, manager="detached")
    service_control.disable(settings)
    made_before = len(fake_supervisor)

    result = service_control.enable(settings, manager="detached")

    assert result.ok is True
    assert result.data.get("installed") is False
    # Re-enable on the same manager kicks start; it must not activate/install again.
    assert fake_supervisor[-1].calls == ["start"]
    assert len(fake_supervisor) == made_before + 1


def test_enable_reinstalls_when_configured_unit_is_missing(
    fake_supervisor, monkeypatch
):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=False,
            configured=True,
            manager="detached",
        ),
    )
    monkeypatch.setattr(FakeSupervisor, "status", lambda _self: "not-installed")

    result = service_control.enable(settings, manager="detached")

    assert result.ok is True
    assert result.data.get("installed") is True
    assert fake_supervisor[-1].calls == ["activate"]


def test_enable_reinstalls_when_unit_definition_drifted(
    fake_supervisor, monkeypatch
):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=False,
            configured=True,
            manager="detached",
        ),
    )
    monkeypatch.setattr(
        FakeSupervisor,
        "definition_status",
        lambda _self: service_control.DefinitionStatus.MISMATCHED,
        raising=False,
    )

    result = service_control.enable(settings, manager="detached")

    assert result.ok is True
    assert result.data.get("installed") is True
    assert fake_supervisor[-1].calls == ["activate"]


def test_disable_persists_and_stops(fake_supervisor):
    settings = load_settings()
    service_control.enable(settings)
    result = service_control.disable(settings)
    assert result.ok is True
    desired = service_state.read_desired(settings.paths)
    assert desired.enabled is False
    assert desired.configured is True
    assert service_state.service_is_running(settings.paths) is False


def test_start_requires_enabled(fake_supervisor):
    settings = load_settings()
    result = service_control.start(settings)
    assert result.ok is False
    assert "disabled" in result.detail.lower()


def test_start_failure_does_not_reinstall_when_unit_is_still_present(
    monkeypatch,
):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True,
            configured=True,
            manager="detached",
        ),
    )
    calls: list[str] = []

    class _FailingSupervisor(FakeSupervisor):
        def status(self):
            return "loaded"

        def definition_status(self):
            return service_control.DefinitionStatus.UNKNOWN

        def start(self):
            calls.append("start")
            return False, "permission denied"

        def activate(self):
            calls.append("activate")
            return True, "activated"

    monkeypatch.setattr(
        service_control,
        "make_supervisor",
        lambda spec, manager="auto": _FailingSupervisor(spec),
    )

    result = service_control.start(settings)

    assert result.ok is False
    assert "permission denied" in result.detail
    assert calls == ["start"]


def test_ensure_is_noop_when_disabled(fake_supervisor):
    settings = load_settings()
    result = service_control.ensure(settings)
    assert result.data.get("enabled") is False
    assert not fake_supervisor  # never constructed a supervisor / touched the OS


def test_ensure_is_noop_when_already_running(fake_supervisor):
    settings = load_settings()
    service_control.enable(settings)
    running_supervisors = len(fake_supervisor)
    result = service_control.ensure(settings)
    assert result.data.get("running") is True
    # ensure short-circuited before constructing another supervisor.
    assert len(fake_supervisor) == running_supervisors


def test_ensure_repairs_enabled_but_down(fake_supervisor):
    settings = load_settings()
    service_control.enable(settings)
    # Simulate a crash: desired stays enabled, runtime disappears.
    service_state.clear_runtime_if_owner(settings.paths)
    assert service_state.service_is_running(settings.paths) is False
    service_control.ensure(settings, wait_s=1.0)
    assert service_state.service_is_running(settings.paths) is True


def test_ensure_starts_the_refreshed_launcher_without_reinstall(fake_supervisor):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True,
            configured=True,
            manager="detached",
            launcher=["/old/python", "-m", "omni.cli.main", "serve", "run"],
        ),
    )

    result = service_control.ensure(settings, wait_s=1.0)

    assert result.ok is True
    # Repair kicks the existing unit — it must not call activate/install.
    assert fake_supervisor[-1].calls == ["start"]
    assert fake_supervisor[-1].argv == service_state.default_launcher(
        settings.paths
    )


def test_ensure_replaces_an_unhealthy_owner(fake_supervisor):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True, configured=True, manager="detached"
        ),
    )
    service_state.write_runtime(
        settings.paths,
        {"ready": False, "phase": "unhealthy", "version": "old"},
    )

    result = service_control.ensure(settings, wait_s=1.0)

    assert result.ok is True
    assert service_state.observe_service(settings.paths).phase == "ready"
    assert [call for supervisor in fake_supervisor for call in supervisor.calls] == [
        "stop",
        "start",
    ]


def test_ensure_does_not_spawn_during_pre_singleton_child_window(
    fake_supervisor, monkeypatch
):
    """The service-id process marker is ACTIVE before HomeService owns its lock."""
    from omni.runtime import daemon as daemon_mod

    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True, configured=True, manager="detached"
        ),
    )
    own_id = service_state.service_instance_id(settings.paths)
    monkeypatch.setattr(
        daemon_mod,
        "scan_running_serve_pids",
        lambda *, service_id=None: [4321] if service_id == own_id else [],
    )

    result = service_control.ensure(settings)

    assert result.ok is True
    assert result.data["active"] is True
    assert fake_supervisor == []


def test_status_and_doctor_report_state(fake_supervisor, monkeypatch):
    from omni.runtime import daemon as daemon_mod

    # The process-table scan is host-global (not hermetic); pin it empty so this
    # "no drift" assertion doesn't depend on the developer's machine.
    monkeypatch.setattr(daemon_mod, "scan_running_serve_pids", lambda **_kwargs: [])
    settings = load_settings()
    service_control.enable(settings)
    snap = service_control.status(settings)
    assert snap["enabled"] is True
    assert snap["running"] is True
    assert "platform" in snap

    doc = service_control.doctor(settings)
    assert "findings" in doc
    # Enabled + running with no legacy daemons → no drift findings.
    assert doc["findings"] == []


def test_doctor_flags_enabled_but_down(fake_supervisor):
    settings = load_settings()
    service_control.enable(settings)
    service_state.clear_runtime_if_owner(settings.paths)
    doc = service_control.doctor(settings)
    assert any("enabled but not running" in f for f in doc["findings"])


def test_doctor_flags_an_unhealthy_owner(fake_supervisor):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(enabled=True, configured=True),
    )
    service_state.write_runtime(
        settings.paths,
        {"ready": False, "phase": "unhealthy"},
    )

    doc = service_control.doctor(settings)

    assert doc["phase"] == "unhealthy"
    assert any("unhealthy" in finding.lower() for finding in doc["findings"])


def test_enable_reaps_legacy_daemons(fake_supervisor, monkeypatch):
    """Bringing the home service up retires legacy per-workspace daemons."""
    from omni.runtime import daemon as daemon_mod

    reaped: list[str] = []
    monkeypatch.setattr(
        daemon_mod, "stop_legacy_daemons", lambda home: reaped.append(str(home)) or [4242]
    )
    settings = load_settings()
    service_control.enable(settings)
    assert reaped == [str(settings.paths.home)]


def test_enable_is_idempotent_when_already_running(fake_supervisor):
    """A second enable while active performs no host lifecycle action.

    ``install`` itself is launch-producing on launchd/systemd, so even an
    install-only second call would churn the healthy process.
    """
    settings = load_settings()
    service_control.enable(settings)
    assert fake_supervisor[-1].calls == ["start"]

    made_before = len(fake_supervisor)
    service_control.enable(settings)
    assert len(fake_supervisor) == made_before + 1
    assert fake_supervisor[-1].calls == []


def test_enable_treats_auto_as_the_resolved_running_manager(fake_supervisor):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True, configured=True, manager="auto"
        ),
    )
    service_state.write_runtime(settings.paths, {"ready": True, "phase": "ready"})

    result = service_control.enable(settings, manager="auto", wait_s=1.0)

    assert result.ok is True
    assert len(fake_supervisor) == 1
    assert fake_supervisor[-1].calls == []
    assert service_state.read_desired(settings.paths).manager == "detached"


def test_enable_uninstalls_an_armed_previous_manager(monkeypatch):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True, configured=True, manager="launchd"
        ),
    )
    calls: list[str] = []

    class _OldManager:
        id = "launchd"

        def __init__(self, _spec) -> None:  # noqa: ANN001
            self.armed = True

        def status(self):
            return "loaded" if self.armed else "not-installed"

        def is_quiescent(self):
            return not self.armed

        def stop(self):
            calls.append("old.stop")
            self.armed = False
            return True, "stopped"

        def uninstall(self):
            calls.append("old.uninstall")
            return True, "removed"

    class _NewManager(FakeSupervisor):
        id = "detached"

        def activate(self):
            calls.append("new.activate")
            return super().activate()

    old = _OldManager(service_control._spec(settings))

    def _make(spec, manager="auto"):  # noqa: ANN001
        return old if manager == "launchd" else _NewManager(spec)

    class _Launchd:
        id = "launchd"

    class _Detached:
        id = "detached"

    monkeypatch.setattr(service_control, "make_supervisor", _make)
    monkeypatch.setattr(
        service_control,
        "select_supervisor_class",
        lambda manager="auto": (
            _Launchd if manager == "launchd" else _Detached
        ),
    )

    result = service_control.enable(settings, manager="detached", wait_s=1.0)

    assert result.ok is True
    assert calls == ["old.stop", "old.uninstall", "new.activate"]
    assert service_state.read_desired(settings.paths).manager == "detached"


def test_restart_reaps_only_current_home_then_activates_once(fake_supervisor, monkeypatch):
    """Restart must not kill a legitimate service belonging to another OMNI_HOME."""
    from omni.runtime import daemon as daemon_mod

    settings = load_settings()
    service_control.enable(settings)  # one running instance
    reaped_args: list[list[int]] = []
    own_pids = {9001, 9002}

    def _scan(*, service_id: str | None = None):
        own_id = service_state.service_instance_id(settings.paths)
        return sorted(own_pids) if service_id == own_id else [7777]

    def _reap(pids, **_kwargs):  # noqa: ANN001
        reaped_args.append(list(pids))
        own_pids.difference_update(pids)
        return list(pids)

    monkeypatch.setattr(daemon_mod, "scan_running_serve_pids", _scan)
    monkeypatch.setattr(daemon_mod, "reap_serve_processes", _reap)
    result = service_control.restart(settings, wait_s=1.0)
    assert result.ok is True
    assert reaped_args == [[9001, 9002]]
    assert fake_supervisor[-1].calls == ["activate"]
    assert service_state.service_is_running(settings.paths) is True  # exactly one fresh instance


def test_update_guard_quiesces_starting_service_and_restores_after_update(
    fake_supervisor, monkeypatch
):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(enabled=True, configured=True, manager="detached"),
    )
    real_observe = service_state.observe_service
    stopped = False

    def _observe(paths):  # noqa: ANN001
        if service_state.read_runtime(paths):
            return real_observe(paths)
        if stopped:
            return service_state.ServiceObservation("down", None, None)
        return service_state.ServiceObservation("starting", 4242, None)

    def _reap(_settings):  # noqa: ANN001
        nonlocal stopped
        stopped = True
        return [4242]

    monkeypatch.setattr(
        service_state,
        "observe_service",
        _observe,
    )
    monkeypatch.setattr(service_control, "_reap_running_serve", _reap)

    with service_control.update_guard(settings, restart_serve=True) as guard:
        assert guard.was_active is True
        assert fake_supervisor[0].calls == ["stop"]
        reservation_probe = service_state.acquire_singleton(settings.paths)
        try:
            assert reservation_probe is None
        finally:
            service_state.release_singleton(reservation_probe)
        with pytest.raises(service_state.LifecycleLockTimeout):
            with service_state.lifecycle_lock(settings.paths, timeout_s=0.0):
                pass
        detail = guard.restore()

    assert "started" in detail.lower()
    assert fake_supervisor[-1].calls == ["activate"]


def test_update_guard_without_restart_still_locks_and_leaves_service_stopped(
    fake_supervisor,
):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True,
            configured=True,
            manager="detached",
        ),
    )
    service_state.write_runtime(settings.paths, {"ready": True, "pid": 4321})

    with service_control.update_guard(settings, restart_serve=False) as guard:
        assert guard.was_active is True
        assert fake_supervisor[0].calls == ["stop"]
        with pytest.raises(service_state.LifecycleLockTimeout):
            with service_state.lifecycle_lock(settings.paths, timeout_s=0.0):
                pass
        assert service_state.acquire_singleton(settings.paths) is None
        detail = guard.restore()

    assert "stopped after the update" in detail
    assert all("activate" not in supervisor.calls for supervisor in fake_supervisor)


def test_update_guard_honours_start_requested_during_install(fake_supervisor):
    """If bare omni loses the lifecycle-lock race, its start intent is not lost."""
    settings = load_settings()

    with service_control.update_guard(settings, restart_serve=True) as guard:
        assert guard.was_active is False
        service_state.request_start(settings.paths)
        detail = guard.restore()

    assert "started" in detail.lower()
    assert service_state.read_desired(settings.paths).enabled is True
    assert service_state.start_requested(settings.paths) is False
    assert fake_supervisor[-1].calls == ["activate"]


def test_update_guard_records_the_post_install_distribution_version(
    fake_supervisor, monkeypatch
):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True, configured=True, manager="detached", version="old"
        ),
    )
    service_state.write_runtime(settings.paths, {"ready": True, "version": "old"})

    with service_control.update_guard(settings, restart_serve=True) as guard:
        monkeypatch.setattr(
            service_control.metadata,
            "version",
            lambda _distribution: "9.9.9",
            raising=False,
        )
        guard.restore()

    assert service_state.read_desired(settings.paths).version == "9.9.9"


def test_bare_launch_records_intent_while_update_reserves_singleton(
    fake_supervisor, monkeypatch
):
    from omni.cli import main as cli_main

    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True,
            configured=True,
            manager="detached",
        ),
    )
    worker_entered = False

    class _State:
        def settings(self):
            return settings

    def _lazy_enable(*_args, **_kwargs):
        nonlocal worker_entered
        worker_entered = True

    monkeypatch.setattr(service_control, "lazy_enable", _lazy_enable)

    with service_control.update_guard(settings, restart_serve=True) as guard:
        cli_main._maybe_ensure_home_service(_State())
        deadline = service_control.time.time() + 1.0
        while not worker_entered and service_control.time.time() < deadline:
            service_control.time.sleep(0.01)
        assert service_state.start_requested(settings.paths) is True
        detail = guard.restore()

    assert "pending bare" in detail
    assert fake_supervisor[-1].calls == ["activate"]


def test_update_guard_consumes_start_request_arriving_after_explicit_restore(
    fake_supervisor,
):
    settings = load_settings()

    with service_control.update_guard(settings, restart_serve=True) as guard:
        assert "not running" not in guard.restore()
        service_state.request_start(settings.paths)

    assert service_state.start_requested(settings.paths) is False
    assert fake_supervisor[-1].calls == ["activate"]


def test_update_guard_restores_loaded_launchd_job_without_visible_pid(monkeypatch):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True, configured=True, manager="launchd"
        ),
    )

    class ArmedSupervisor(FakeSupervisor):
        id = "launchd"

        def __init__(self, spec) -> None:  # noqa: ANN001
            super().__init__(spec)
            self.armed = True

        def status(self):
            return "loaded" if self.armed else "not-installed"

        def is_quiescent(self):
            return not self.armed

        def stop(self):
            self.calls.append("stop")
            self.armed = False
            return True, "booted out"

        def activate(self):
            self.calls.append("activate")
            self.armed = True
            service_state.write_runtime(
                self.paths, {"ready": True, "phase": "ready"}
            )
            return True, "bootstrapped"

    supervisor = ArmedSupervisor(service_control._spec(settings))

    class _Launchd:
        id = "launchd"

    monkeypatch.setattr(
        service_control, "make_supervisor", lambda *_args, **_kwargs: supervisor
    )
    monkeypatch.setattr(
        service_control,
        "select_supervisor_class",
        lambda _manager="auto": _Launchd,
    )

    with service_control.update_guard(settings, restart_serve=True) as guard:
        assert guard.was_active is True
        guard.restore()

    assert supervisor.calls == ["stop", "activate"]


def test_update_guard_reservation_blocks_an_external_direct_owner(fake_supervisor):
    settings = load_settings()
    code = (
        "from omni.config import load_settings\n"
        "from omni.runtime.service_state import acquire_singleton, release_singleton\n"
        "fd = acquire_singleton(load_settings().paths)\n"
        "print('acquired' if fd is not None else 'blocked', flush=True)\n"
        "release_singleton(fd)\n"
    )

    with service_control.update_guard(settings, restart_serve=True) as guard:
        probe = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
        )
        assert probe.returncode == 0
        assert probe.stdout.strip() == "blocked"
        guard.restore()


def test_stop_requires_supervisor_to_be_quiescent(fake_supervisor, monkeypatch):
    settings = load_settings()
    service_control.enable(settings)

    class StickySupervisor(FakeSupervisor):
        def stop(self):
            self.calls.append("stop")
            service_state.clear_runtime_if_owner(self.paths)
            return False, "manager refused stop"

        def status(self):
            return "running"

    sticky = StickySupervisor(service_control._spec(settings))
    monkeypatch.setattr(
        service_control, "make_supervisor", lambda *_args, **_kwargs: sticky
    )

    result = service_control.stop(settings)

    assert result.ok is False
    assert "supervisor" in result.detail.lower()


def test_public_stop_does_not_reap_an_unverified_runtime_pid(monkeypatch):
    from omni.runtime import daemon as daemon_mod

    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True, configured=True, manager="detached"
        ),
    )
    monkeypatch.setattr(
        service_state,
        "service_runtime_info",
        lambda _paths: {
            "pid": 4242,
            "service_id": service_state.service_instance_id(settings.paths),
        },
    )
    monkeypatch.setattr(
        daemon_mod, "scan_running_serve_pids", lambda **_kwargs: []
    )
    reaped: list[list[int]] = []
    monkeypatch.setattr(
        daemon_mod,
        "reap_serve_processes",
        lambda pids, **_kwargs: (reaped.append(list(pids)) or list(pids)),
    )

    class _IdleSupervisor:
        id = "detached"

        def stop(self):
            return True, "stopped"

        def status(self):
            return "stopped"

        def is_quiescent(self):
            return True

    monkeypatch.setattr(
        service_control,
        "make_supervisor",
        lambda *_args, **_kwargs: _IdleSupervisor(),
    )
    monkeypatch.setattr(
        service_control, "_wait_stably_quiescent", lambda *_args, **_kwargs: True
    )

    result = service_control.stop(settings)

    assert result.ok is True
    assert reaped == []


def test_update_guard_restores_active_service_when_update_fails(fake_supervisor, monkeypatch):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(enabled=True, configured=True, manager="detached"),
    )
    real_observe = service_state.observe_service
    stopped = False

    def _observe(paths):  # noqa: ANN001
        if service_state.read_runtime(paths):
            return real_observe(paths)
        if stopped:
            return service_state.ServiceObservation("down", None, None)
        return service_state.ServiceObservation("starting", 4242, None)

    def _reap(_settings):  # noqa: ANN001
        nonlocal stopped
        stopped = True
        return [4242]

    monkeypatch.setattr(
        service_state,
        "observe_service",
        _observe,
    )
    monkeypatch.setattr(service_control, "_reap_running_serve", _reap)

    with pytest.raises(RuntimeError, match="install failed"):
        with service_control.update_guard(settings, restart_serve=True):
            raise RuntimeError("install failed")

    assert fake_supervisor[-1].calls == ["activate"]


def test_update_guard_preserves_update_error_and_records_restore_error(
    fake_supervisor, monkeypatch
):
    settings = load_settings()
    service_state.request_start(settings.paths)
    monkeypatch.setattr(
        service_control,
        "_activate_locked",
        lambda *_args, **_kwargs: service_control._LaunchOutcome(
            False,
            "activation failed",
        ),
    )

    with pytest.raises(ValueError, match="install failed") as caught:
        with service_control.update_guard(settings, restart_serve=True):
            raise ValueError("install failed")

    restore_error = getattr(
        caught.value, "_omni_service_restore_error", ""
    )
    assert "failed to restore" in restore_error
    assert "activation failed" in restore_error
    assert "failed to restore" in service_state.read_desired(
        settings.paths
    ).last_error


def test_doctor_and_status_flag_duplicate_serve_processes(fake_supervisor, monkeypatch):
    """A live count > 1 in the process table is surfaced (the 15-process bug)."""
    from omni.runtime import daemon as daemon_mod

    monkeypatch.setattr(
        daemon_mod, "scan_running_serve_pids", lambda **_kwargs: [111, 222, 333]
    )
    settings = load_settings()
    service_control.enable(settings)

    snap = service_control.status(settings)
    assert snap["serve_pids"] == [111, 222, 333]

    doc = service_control.doctor(settings)
    assert any("processes are live" in f for f in doc["findings"])


def test_update_restore_succeeds_while_control_plane_is_still_starting(
    fake_supervisor, monkeypatch
):
    """Update must restart serve, but STARTING on new code is a successful restore."""
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True, configured=True, manager="detached"
        ),
    )
    service_state.write_runtime(
        settings.paths, {"ready": True, "phase": "ready", "version": "old"}
    )

    def _activate(self):  # noqa: ANN001
        self.calls.append("activate")
        service_state.write_runtime(
            self.paths,
            {"ready": False, "phase": "starting", "version": "test"},
        )
        return True, "activated"

    monkeypatch.setattr(FakeSupervisor, "activate", _activate)

    with service_control.update_guard(
        settings, restart_serve=True, ready_wait_s=0.3
    ) as guard:
        assert guard.was_active is True
        detail = guard.restore()

    assert "still becoming ready" in detail
    assert "restarted" in detail
    assert fake_supervisor[-1].calls == ["activate"]


def test_update_restore_succeeds_when_ready_arrives_within_wait(
    fake_supervisor, monkeypatch
):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True, configured=True, manager="detached"
        ),
    )
    service_state.write_runtime(
        settings.paths, {"ready": True, "phase": "ready"}
    )

    def _activate(self):  # noqa: ANN001
        self.calls.append("activate")
        service_state.write_runtime(
            self.paths, {"ready": False, "phase": "starting", "version": "test"}
        )

        def _flip() -> None:
            service_state.write_runtime(
                self.paths,
                {"ready": True, "phase": "ready", "version": "test"},
            )

        threading.Timer(0.15, _flip).start()
        return True, "activated"

    monkeypatch.setattr(FakeSupervisor, "activate", _activate)

    with service_control.update_guard(
        settings, restart_serve=True, ready_wait_s=2.0
    ) as guard:
        detail = guard.restore()

    assert detail == "restarted on the updated code."
    assert service_state.service_is_ready(settings.paths) is True


def test_update_restore_fails_when_process_never_claims_singleton(
    fake_supervisor, monkeypatch
):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True, configured=True, manager="detached"
        ),
    )
    service_state.write_runtime(settings.paths, {"ready": True, "phase": "ready"})

    def _activate(self):  # noqa: ANN001
        self.calls.append("activate")
        return True, "activated"

    monkeypatch.setattr(FakeSupervisor, "activate", _activate)
    monkeypatch.setattr(service_control, "_wait_active", lambda *_a, **_k: True)

    with pytest.raises(RuntimeError, match="did not become ready") as caught:
        with service_control.update_guard(
            settings, restart_serve=True, ready_wait_s=0.3
        ) as guard:
            guard.restore()

    assert "phase=" in str(caught.value)


def test_update_restore_fails_when_starting_process_dies(
    fake_supervisor, monkeypatch
):
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        service_state.ServiceDesiredState(
            enabled=True, configured=True, manager="detached"
        ),
    )
    service_state.write_runtime(settings.paths, {"ready": True, "phase": "ready"})

    def _activate(self):  # noqa: ANN001
        self.calls.append("activate")
        service_state.write_runtime(
            self.paths, {"ready": False, "phase": "starting", "version": "test"}
        )

        def _die() -> None:
            service_state.clear_runtime(self.paths)

        threading.Timer(0.2, _die).start()
        return True, "activated"

    monkeypatch.setattr(FakeSupervisor, "activate", _activate)

    with pytest.raises(RuntimeError, match="did not become ready") as caught:
        with service_control.update_guard(
            settings, restart_serve=True, ready_wait_s=1.0
        ) as guard:
            guard.restore()

    text = str(caught.value)
    assert "phase=" in text
    assert "pid=" in text


def test_restart_succeeds_while_control_plane_is_still_starting(
    fake_supervisor, monkeypatch
):
    settings = load_settings()
    service_control.enable(settings)

    def _activate(self):  # noqa: ANN001
        self.calls.append("activate")
        service_state.write_runtime(
            self.paths, {"ready": False, "phase": "starting", "version": "test"}
        )
        return True, "activated"

    monkeypatch.setattr(FakeSupervisor, "activate", _activate)

    result = service_control.restart(settings, wait_s=0.3)

    assert result.ok is True
    assert result.data.get("running") is False
    assert result.data.get("phase") == "starting"
    assert "still becoming ready" in result.detail


def test_wait_restore_waits_through_initial_down(fake_supervisor, monkeypatch):
    """launchd has not spawned yet — down is a wait, not an immediate failure."""
    settings = load_settings()
    phases = iter(["down", "down", "starting", "ready"])

    def _observe(_paths):  # noqa: ANN001
        phase = next(phases, "ready")
        if phase == "down":
            return service_state.ServiceObservation("down", None, None)
        return service_state.ServiceObservation(
            phase, 4242, {"phase": phase, "ready": phase == "ready", "pid": 4242}
        )

    monkeypatch.setattr(service_state, "observe_service", _observe)
    monkeypatch.setattr(service_control, "_service_active", lambda _s: False)

    claimed, ready, last = service_control._wait_restore(settings, 1.0)
    assert claimed is True
    assert ready is True
    assert last.phase == "ready"


def test_wait_restore_fails_fast_after_claim_then_death(fake_supervisor, monkeypatch):
    settings = load_settings()
    phases = iter(["starting", "down"])

    def _observe(_paths):  # noqa: ANN001
        phase = next(phases, "down")
        if phase == "down":
            return service_state.ServiceObservation("down", None, None)
        return service_state.ServiceObservation(
            phase, 4242, {"phase": phase, "ready": False, "pid": 4242}
        )

    monkeypatch.setattr(service_state, "observe_service", _observe)
    monkeypatch.setattr(service_control, "_service_active", lambda _s: False)

    started = time.time()
    claimed, ready, last = service_control._wait_restore(settings, 2.0)
    assert claimed is False
    assert ready is False
    assert last.phase == "down"
    assert time.time() - started < 1.0
