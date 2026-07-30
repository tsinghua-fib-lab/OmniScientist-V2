"""Launch-ensure hook, service-aware update, and uninstall teardown.

These wire the home-service lifecycle into the CLI entry points:

* a bare ``omni`` guarantees the single home service is up (always-on model) —
  enabling it on first need and repairing it if it drifted down;
* ``omni update`` reconciles the service toward its desired state;
* ``omni uninstall`` tears it down.

``omni init`` no longer onboards the service via a wizard prompt: it comes up on
launch, and ``omni serve stop`` is only a transient pause the next launch undoes.
"""

from __future__ import annotations

import threading
import time

from omni.cli import main as cli_main
from omni.config import load_settings
from omni.runtime import service_control, service_state
from omni.runtime.service_state import ServiceDesiredState


class _State:
    def settings(self):
        return load_settings()


# ── bare-omni ensure hook ────────────────────────────────────────────────────


def test_launch_hook_brings_up_when_down(monkeypatch):
    """Always-on: a bare launch brings the service up even from a fresh/disabled
    state (transient stop is undone on the next launch)."""
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    fired = threading.Event()

    def _fake_lazy_enable(_settings, **_kw):
        fired.set()
        return service_control.LifecycleResult(True, "brought up")

    monkeypatch.setattr(service_control, "lazy_enable", _fake_lazy_enable)
    cli_main._maybe_ensure_home_service(_State())
    assert fired.wait(2.0)


def test_launch_hook_repairs_enabled_but_down(monkeypatch):
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    settings = load_settings()
    service_state.write_desired(settings.paths, ServiceDesiredState(enabled=True, configured=True))
    fired = threading.Event()

    def _fake_lazy_enable(_settings, **_kw):
        fired.set()
        return service_control.LifecycleResult(True, "repair kicked")

    monkeypatch.setattr(service_control, "lazy_enable", _fake_lazy_enable)
    cli_main._maybe_ensure_home_service(_State())
    assert fired.wait(2.0)


def test_launch_hook_noop_when_running(monkeypatch):
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    settings = load_settings()
    service_state.write_desired(settings.paths, ServiceDesiredState(enabled=True, configured=True))
    service_state.write_runtime(settings.paths, {"ready": True})  # live pid = this process
    calls: list[int] = []
    monkeypatch.setattr(service_control, "lazy_enable", lambda *a, **k: calls.append(1))
    cli_main._maybe_ensure_home_service(_State())
    time.sleep(0.2)
    assert calls == []


def test_launch_hook_noop_when_service_is_still_starting(monkeypatch):
    """A singleton owner is already active before its READY heartbeat exists."""
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    settings = load_settings()
    service_state.write_desired(
        settings.paths, ServiceDesiredState(enabled=True, configured=True)
    )
    monkeypatch.setattr(
        service_state,
        "observe_service",
        lambda _paths: service_state.ServiceObservation("starting", 4321, None),
    )
    calls: list[int] = []
    monkeypatch.setattr(
        service_control, "lazy_enable", lambda *a, **k: calls.append(1)
    )

    cli_main._maybe_ensure_home_service(_State())
    time.sleep(0.2)

    assert calls == []


def test_launch_hook_repairs_an_unhealthy_service(monkeypatch):
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    settings = load_settings()
    service_state.write_desired(
        settings.paths, ServiceDesiredState(enabled=True, configured=True)
    )
    monkeypatch.setattr(
        service_state,
        "observe_service",
        lambda _paths: service_state.ServiceObservation(
            "unhealthy", 4321, {"phase": "unhealthy"}
        ),
    )
    fired = threading.Event()

    def _fake_lazy_enable(_settings, **_kwargs):
        fired.set()
        return service_control.LifecycleResult(True, "repair kicked")

    monkeypatch.setattr(service_control, "lazy_enable", _fake_lazy_enable)

    cli_main._maybe_ensure_home_service(_State())

    assert fired.wait(2.0)
    assert service_state.start_requested(settings.paths) is True


def test_launch_hook_runs_first_install_synchronously(monkeypatch):
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    entered = False
    worker_names: list[str] = []

    def _lazy_enable(*_args, **_kwargs):
        nonlocal entered
        entered = True
        worker_names.append(threading.current_thread().name)
        return service_control.LifecycleResult(True, "enabled")

    monkeypatch.setattr(service_control, "lazy_enable", _lazy_enable)

    cli_main._maybe_ensure_home_service(_State())
    assert entered is True
    assert worker_names == [threading.current_thread().name]


def test_launch_hook_never_raises_when_start_intent_cannot_be_written(
    monkeypatch,
):
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    settings = load_settings()
    service_state.write_desired(
        settings.paths,
        ServiceDesiredState(enabled=True, configured=True),
    )
    fired = threading.Event()
    monkeypatch.setattr(
        service_state,
        "request_start",
        lambda _paths: (_ for _ in ()).throw(OSError("read-only")),
    )
    monkeypatch.setattr(
        service_control,
        "lazy_enable",
        lambda *_args, **_kwargs: (
            fired.set()
            or service_control.LifecycleResult(True, "repair kicked")
        ),
    )

    cli_main._maybe_ensure_home_service(_State())

    assert fired.wait(2.0)


def test_launch_hook_respects_ensure_on_launch_escape_hatch(monkeypatch):
    """`service.ensure_on_launch = false` opts out of the always-on bring-up."""
    settings = load_settings()
    settings.service.ensure_on_launch = False

    class _Off:
        def settings(self):
            return settings

    calls: list[int] = []
    monkeypatch.setattr(service_control, "lazy_enable", lambda *a, **k: calls.append(1))
    cli_main._maybe_ensure_home_service(_Off())
    time.sleep(0.2)
    assert calls == []


def test_launch_hook_skips_ephemeral_home_before_writing_start_intent(monkeypatch):
    monkeypatch.delenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", raising=False)
    requested: list[str] = []
    calls: list[str] = []
    monkeypatch.setattr(
        service_state,
        "request_start",
        lambda _paths: requested.append("request"),
    )
    monkeypatch.setattr(
        service_control,
        "lazy_enable",
        lambda *_args, **_kwargs: calls.append("enable"),
    )

    cli_main._maybe_ensure_home_service(_State())
    time.sleep(0.1)

    assert requested == []
    assert calls == []


# ── lazy enablement policy ───────────────────────────────────────────────────


def test_lazy_enable_enables_when_never_configured(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        service_control,
        "enable",
        lambda s, **k: calls.append("enable") or service_control.LifecycleResult(True, "enabled"),
    )
    result = service_control.lazy_enable(load_settings(), reason="channel:feishu")
    assert result.ok and calls == ["enable"]


def test_lazy_enable_installs_once_on_first_bare_launch(monkeypatch):
    """First bare omni on an unconfigured real home goes through enable (install)."""
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    calls: list[str] = []
    monkeypatch.setattr(
        service_control,
        "enable",
        lambda s, **k: calls.append("enable") or service_control.LifecycleResult(True, "enabled"),
    )
    monkeypatch.setattr(
        service_control,
        "ensure",
        lambda s, **k: calls.append("ensure") or service_control.LifecycleResult(True, "ensured"),
    )
    result = service_control.lazy_enable(load_settings(), reason="launch")
    assert result.ok and calls == ["enable"]


def test_lazy_enable_honours_configured_manager_on_first_launch(monkeypatch):
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    settings = load_settings()
    settings.service.manager = "detached"
    managers: list[str] = []
    monkeypatch.setattr(
        service_control,
        "enable",
        lambda _settings, **kwargs: (
            managers.append(str(kwargs.get("manager")))
            or service_control.LifecycleResult(True, "enabled")
        ),
    )

    result = service_control.lazy_enable(settings, reason="launch")

    assert result.ok is True
    assert managers == ["detached"]


def test_lazy_enable_skips_durable_install_on_ephemeral_bare_launch(monkeypatch):
    monkeypatch.delenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", raising=False)
    settings = load_settings()
    service_state.request_start(settings.paths)
    calls: list[str] = []
    monkeypatch.setattr(
        service_control,
        "enable",
        lambda s, **k: calls.append("enable") or service_control.LifecycleResult(True, "enabled"),
    )
    result = service_control.lazy_enable(settings, reason="launch")
    assert result.ok is True
    assert result.data.get("ephemeral") is True
    assert calls == []
    assert service_state.start_requested(settings.paths) is False


def test_lazy_enable_reenables_after_explicit_disable(monkeypatch):
    """An explicit feature trigger (channel --start / schedule add) actively starts
    the home service even if a prior `omni serve stop` disabled it: the user just
    opted into a feature that depends on it, so we override the stale disable."""
    settings = load_settings()
    service_state.write_desired(settings.paths, ServiceDesiredState(enabled=False, configured=True))
    calls: list[str] = []
    monkeypatch.setattr(
        service_control,
        "enable",
        lambda s, **k: calls.append("enable") or service_control.LifecycleResult(True, "enabled"),
    )
    result = service_control.lazy_enable(settings, reason="schedule")
    assert result.ok and calls == ["enable"]


def test_lazy_enable_repairs_when_already_enabled(monkeypatch):
    settings = load_settings()
    service_state.write_desired(settings.paths, ServiceDesiredState(enabled=True, configured=True))
    calls: list[str] = []
    monkeypatch.setattr(
        service_control,
        "ensure",
        lambda s, **k: calls.append("ensure") or service_control.LifecycleResult(True, "ensured"),
    )
    monkeypatch.setattr(
        service_control,
        "enable",
        lambda s, **k: calls.append("enable") or service_control.LifecycleResult(True, "enabled"),
    )
    result = service_control.lazy_enable(settings, reason="channel:wechat")
    assert result.ok and calls == ["ensure"]


# ── uninstall teardown ───────────────────────────────────────────────────────


def test_uninstall_tears_down_home_service(monkeypatch):
    from omni.runtime import uninstall

    settings = load_settings()
    service_state.write_desired(settings.paths, ServiceDesiredState(enabled=True, configured=True))
    service_state.write_runtime(settings.paths, {"ready": True})

    stopped: list[str] = []

    class _Sup:
        id = "detached"

        def __init__(self, spec):  # noqa: ANN001
            self.paths = spec.paths

        def stop(self):
            stopped.append("stop")
            return True, "stopped"

        def uninstall(self):
            stopped.append("uninstall")
            return True, "removed"

    from omni.runtime import service_supervisors

    monkeypatch.setattr(service_supervisors, "make_supervisor", lambda spec, manager="auto": _Sup(spec))

    report = uninstall.UninstallReport()
    uninstall._teardown_home_service(settings.paths, report)
    assert "stop" in stopped and "uninstall" in stopped
    desired = service_state.read_desired(settings.paths)
    assert desired.enabled is False
    assert any("home service" in line for line in report.completed)
