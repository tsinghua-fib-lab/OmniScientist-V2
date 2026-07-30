"""`omni serve` home-service wiring (offline, fake supervisor — no real units).

After the serve/service convergence there is a single command surface: ``omni
serve`` manages the home-level background service (``omni service`` is gone).
"""

from __future__ import annotations

import sys

import pytest
from click import unstyle
from typer.testing import CliRunner

from omni.cli.main import app
from omni.runtime import service_control, service_state

runner = CliRunner()


class _FakeSupervisor:
    id = "detached"

    def __init__(self, spec) -> None:  # noqa: ANN001
        self.paths = spec.paths

    def install(self):
        return True, "installed"

    def uninstall(self):
        return True, "removed"

    def start(self):
        service_state.write_runtime(self.paths, {"ready": True, "version": "test"})
        return True, "started"

    def activate(self):
        return self.start()

    def stop(self):
        service_state.clear_runtime_if_owner(self.paths)
        return True, "stopped"

    def status(self):
        return "running" if service_state.service_is_running(self.paths) else "stopped"


@pytest.fixture
def fake_supervisor(monkeypatch):
    class _Cls:
        id = "detached"

    monkeypatch.setattr(service_control, "make_supervisor", lambda spec, manager="auto": _FakeSupervisor(spec))
    monkeypatch.setattr(service_control, "select_supervisor_class", lambda manager="auto": _Cls)


def _settings():
    from omni.config import load_settings

    return load_settings()


def test_no_service_command_group():
    """The legacy `omni service` group is fully removed; only `omni serve` remains."""
    res = runner.invoke(app, ["service", "status"])
    assert res.exit_code != 0


def test_serve_status_on_fresh_home_is_disabled():
    res = runner.invoke(app, ["serve", "status"])
    assert res.exit_code == 0
    assert "Enabled" in res.stdout
    assert "False" in res.stdout


def test_serve_doctor_reports_platform():
    res = runner.invoke(app, ["serve", "doctor"])
    assert res.exit_code == 0
    assert "Supervisor availability" in res.stdout
    assert "auto selects" in res.stdout


def test_serve_run_rejects_launcher_identity_for_another_home(monkeypatch):
    """A Python-level launcher marker is validated before service startup."""
    import omni.runtime.home_service as home_service

    monkeypatch.setitem(sys._xoptions, "omni_service_id", "another-home")

    def _must_not_start(*_args, **_kwargs):
        raise AssertionError("mismatched launcher identity reached service startup")

    monkeypatch.setattr(home_service, "run_home_service", _must_not_start)
    result = runner.invoke(app, ["serve", "run"])

    assert result.exit_code == 2
    assert "service id does not match" in result.output
    assert "active OMNI_HOME" in result.output


def test_serve_run_accepts_matching_python_launcher_identity(monkeypatch):
    """The Python-level marker is the one supported supervisor protocol."""
    import omni.runtime.home_service as home_service

    expected_id = service_state.service_instance_id(_settings().paths)
    monkeypatch.setitem(sys._xoptions, "omni_service_id", expected_id)
    started: list[bool] = []

    async def _run(*_args, **_kwargs):
        started.append(True)

    monkeypatch.setattr(home_service, "run_home_service", _run)
    result = runner.invoke(app, ["serve", "run"])

    assert result.exit_code == 0, result.output
    assert started == [True]


@pytest.mark.parametrize("malformed_id", [True, ""])
def test_serve_run_rejects_malformed_launcher_identity(monkeypatch, malformed_id):
    """An identity marker without a value must not bypass scoped ownership."""
    import omni.runtime.home_service as home_service

    monkeypatch.setitem(sys._xoptions, "omni_service_id", malformed_id)

    def _must_not_start(*_args, **_kwargs):
        raise AssertionError("malformed launcher identity reached service startup")

    monkeypatch.setattr(home_service, "run_home_service", _must_not_start)
    result = runner.invoke(app, ["serve", "run"])

    assert result.exit_code == 2
    assert "service launcher identity must" in result.output
    assert "be a non-empty value" in result.output


def test_serve_run_rejects_removed_legacy_service_id_option():
    """The unreleased Typer-level identity protocol is not part of the CLI."""
    result = runner.invoke(
        app,
        ["serve", "run", "--service-id", "another-home"],
    )

    assert result.exit_code == 2
    # Rich may inject ANSI styles and wrap inside the option name on narrow CI
    # terminals. Compare the semantic error after removing both.
    compact = "".join(unstyle(result.output).split()).lower()
    assert "nosuchoption:--service-id" in compact


def test_serve_start_status_stop_roundtrip(fake_supervisor):
    start = runner.invoke(app, ["serve", "start", "--manager", "detached"])
    assert start.exit_code == 0

    status = runner.invoke(app, ["serve", "status"])
    assert "True" in status.stdout  # enabled + running

    stop = runner.invoke(app, ["serve", "stop"])
    assert stop.exit_code == 0
    # Transient pause: the service is stopped now, but the desired state stays
    # enabled so the next `omni` launch brings it back automatically.
    assert service_state.read_desired(_settings().paths).enabled is True
    assert "next time you run" in stop.stdout


def test_serve_stop_has_no_second_reap_after_lifecycle_lock(
    fake_supervisor, monkeypatch
):
    from omni.runtime import daemon as daemon_mod

    start = runner.invoke(app, ["serve", "start", "--manager", "detached"])
    assert start.exit_code == 0

    # service_control.stop performs its scoped reap while holding lifecycle.lock.
    # A second CLI-level scan after it returns could kill a concurrently repaired
    # legitimate service.
    settings = _settings()

    def _scan(**_kwargs):
        try:
            with service_state.lifecycle_lock(settings.paths, timeout_s=0.0):
                pass
        except service_state.LifecycleLockTimeout:
            return []
        raise AssertionError("lock-free second reap")

    monkeypatch.setattr(
        daemon_mod,
        "scan_running_serve_pids",
        _scan,
    )

    stopped = runner.invoke(app, ["serve", "stop"])

    assert stopped.exit_code == 0, stopped.stdout


def test_serve_status_relabels_roles_and_warns_on_duplicates(monkeypatch):
    """Roles read as capabilities (not 'worker'), and duplicates are surfaced."""
    from omni.runtime import daemon as daemon_mod

    monkeypatch.setenv("COLUMNS", "200")  # avoid rich fold-wrapping in assertions
    paths = _settings().paths
    # A live runtime advertising the anchor + a schedules-only workspace.
    service_state.write_runtime(
        paths,
        {
            "ready": True,
            "version": "test",
            "anchor": "default",
            "workspaces": [
                {"name": "default", "dir": "/a", "anchor": True},
                {"name": "repo", "dir": "/b", "anchor": False},
            ],
        },
    )
    # Two live serve processes → the duplicate warning must fire.
    monkeypatch.setattr(
        daemon_mod, "scan_running_serve_pids", lambda **_kwargs: [4001, 4002]
    )

    # Default view collapses to one service: a compact summary, no per-workspace
    # role table, but the duplicate-process warning still fires.
    res = runner.invoke(app, ["serve", "status"])
    assert res.exit_code == 0
    assert "Dispatching schedules for 2 workspace(s)" in res.stdout
    assert "channels+schedules" not in res.stdout  # detail is behind --verbose
    assert "stop --all" in res.stdout  # duplicate-process remediation hint

    # --verbose expands the per-workspace breakdown with capability roles.
    verbose = runner.invoke(app, ["serve", "status", "--verbose"])
    assert verbose.exit_code == 0
    assert "channels+schedules" in verbose.stdout  # anchor role
    assert "worker" not in verbose.stdout  # the misleading label is gone
