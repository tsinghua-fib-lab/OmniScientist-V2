"""OS supervisor selection and deterministic unit/plist/command generation."""

from __future__ import annotations

import json
import plistlib
import time
from pathlib import Path

import pytest

from omni.config import load_settings
from omni.runtime import service_supervisors as sup
from omni.runtime.service_state import service_instance_id
from omni.runtime.service_supervisors import (
    DetachedSupervisor,
    LaunchdSupervisor,
    SchtasksSupervisor,
    SupervisorSpec,
    SystemdUserSupervisor,
    render_launchd_plist,
    render_schtasks_create,
    render_startup_cmd,
    render_systemd_unit,
    select_supervisor_class,
    service_label,
)


@pytest.fixture(autouse=True)
def _allow_ephemeral_host_service_for_supervisor_unit_tests(monkeypatch):
    """This module installs supervisors against tmp OMNI_HOME on purpose."""
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")


@pytest.fixture
def isolated_launchd_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(
        LaunchdSupervisor,
        "_plist_path",
        lambda self: tmp_path / f"{self.label}.plist",
    )
    monkeypatch.setattr(
        LaunchdSupervisor,
        "_legacy_plist_path",
        lambda self: tmp_path / f"{self._legacy_label()}.plist",
    )


@pytest.fixture
def isolated_systemd_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(
        SystemdUserSupervisor,
        "_unit_path",
        lambda self: tmp_path / self._unit_name(),
    )
    monkeypatch.setattr(
        SystemdUserSupervisor,
        "_legacy_unit_path",
        lambda self: tmp_path / self._legacy_unit_name(),
        raising=False,
    )


def _spec() -> SupervisorSpec:
    paths = load_settings().paths
    return SupervisorSpec(
        paths=paths,
        argv=["/usr/bin/python3", "-m", "omni.cli.main", "service", "run"],
        workdir=paths.home,
        log_path=paths.logs_dir / "home-service.log",
        env={"OMNI_HOME": str(paths.home)},
    )


def _write_orphan_launchd_plist(tmp_path: Path) -> tuple[Path, Path, str]:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    label = "com.omniscientist.omni.orphan"
    plist = agents / f"{label}.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "EnvironmentVariables": {
                    "OMNI_HOME": str(tmp_path / "deleted-home"),
                },
            }
        )
    )
    return agents, plist, label


def test_service_label_is_stable_and_home_scoped(tmp_path):
    a = service_label(tmp_path / "homeA")
    b = service_label(tmp_path / "homeB")
    assert a != b
    assert service_label(tmp_path / "homeA") == a  # deterministic
    assert a.startswith("com.omniscientist.omni.")


def test_auto_selection_per_platform_prefers_native_then_detached(monkeypatch):
    # Native supervisor unavailable → auto degrades to detached.
    monkeypatch.setattr(LaunchdSupervisor, "available", classmethod(lambda cls: False))
    monkeypatch.setattr(SystemdUserSupervisor, "available", classmethod(lambda cls: False))
    monkeypatch.setattr(SchtasksSupervisor, "available", classmethod(lambda cls: False))
    assert select_supervisor_class("auto") is DetachedSupervisor

    # macOS with launchd available → launchd.
    monkeypatch.setattr(LaunchdSupervisor, "available", classmethod(lambda cls: True))
    monkeypatch.setattr(sup.sys, "platform", "darwin")
    assert select_supervisor_class("auto") is LaunchdSupervisor

    # Linux with systemd-user available → systemd.
    monkeypatch.setattr(LaunchdSupervisor, "available", classmethod(lambda cls: False))
    monkeypatch.setattr(SystemdUserSupervisor, "available", classmethod(lambda cls: True))
    monkeypatch.setattr(sup.sys, "platform", "linux")
    assert select_supervisor_class("auto") is SystemdUserSupervisor


@pytest.mark.parametrize(
    ("returncode", "output"),
    [
        (1, "offline"),
        (1, "Failed to connect to bus: No such file or directory"),
    ],
)
def test_systemd_availability_rejects_unusable_user_manager(
    monkeypatch, returncode, output
):
    monkeypatch.setattr(sup.sys, "platform", "linux")
    monkeypatch.setattr(sup.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(sup, "_run", lambda *_args, **_kwargs: (returncode, output))

    assert SystemdUserSupervisor.available() is False


@pytest.mark.parametrize(("returncode", "output"), [(0, "running"), (1, "degraded")])
def test_systemd_availability_accepts_usable_user_manager(
    monkeypatch, returncode, output
):
    monkeypatch.setattr(sup.sys, "platform", "linux")
    monkeypatch.setattr(sup.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(sup, "_run", lambda *_args, **_kwargs: (returncode, output))

    assert SystemdUserSupervisor.available() is True


def test_explicit_manager_is_honoured_even_when_unavailable():
    assert select_supervisor_class("launchd") is LaunchdSupervisor
    assert select_supervisor_class("systemd") is SystemdUserSupervisor
    assert select_supervisor_class("schtasks") is SchtasksSupervisor
    assert select_supervisor_class("detached") is DetachedSupervisor


def test_launchd_plist_is_valid_and_restart_on_crash_only():
    data = plistlib.loads(render_launchd_plist("com.test.omni", _spec()))
    assert data["Label"] == "com.test.omni"
    assert data["RunAtLoad"] is True
    assert data["ProgramArguments"][0].endswith("python3")
    # KeepAlive restarts on failure but not after a clean stop.
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["EnvironmentVariables"]["OMNI_HOME"]


def test_systemd_unit_has_execstart_restart_and_install_section():
    unit = render_systemd_unit("omni-home-service-abcd1234", _spec())
    assert "[Service]" in unit and "[Install]" in unit
    assert "ExecStart=" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert "Environment=OMNI_HOME=" in unit


def test_schtasks_command_and_startup_wrapper():
    wrapper = Path("/tmp/home-service.cmd")
    cmd = render_schtasks_create("omni-home-service-abcd1234", wrapper)
    assert cmd[:2] == ["schtasks", "/Create"]
    assert "/SC" in cmd and "ONLOGON" in cmd
    assert "omni-home-service-abcd1234" in cmd

    body = render_startup_cmd(_spec())
    assert "@echo off" in body
    assert "service" in body and "run" in body


def test_detached_supervisor_is_always_available():
    assert DetachedSupervisor.available() is True
    spec = _spec()
    supervisor = DetachedSupervisor(spec)
    ok, _ = supervisor.install()
    assert ok is True  # no-op install


def test_default_stop_never_kills_a_pid_from_stale_runtime(
    monkeypatch,
):
    supervisor = DetachedSupervisor(_spec())
    runtime_path = supervisor.spec.paths.service_dir / "service.pid"
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(
        json.dumps(
            {
                "pid": 4242,
                "heartbeat": time.time(),
                "service_id": service_instance_id(supervisor.spec.paths),
                "ready": True,
            }
        ),
        encoding="utf-8",
    )
    killed: list[int] = []
    monkeypatch.setattr(sup, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        sup,
        "_terminate_and_wait",
        lambda pid, **_kwargs: (killed.append(pid) or True),
    )

    ok, detail = supervisor.stop()

    assert ok is True
    assert detail == "service not running"
    assert killed == []


def test_launchd_activate_bootstraps_once_without_kickstart(
    isolated_launchd_paths, monkeypatch
):
    commands: list[list[str]] = []
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kw: (
            commands.append(list(cmd))
            or ((1, "not loaded") if "print" in cmd else (0, "ok"))
        ),
    )

    ok, _ = LaunchdSupervisor(_spec()).activate()

    assert ok is True
    assert sum("bootstrap" in cmd for cmd in commands) == 1
    assert not any("kickstart" in cmd for cmd in commands)


def test_launchd_start_does_not_install_on_arbitrary_kickstart_failure(
    isolated_launchd_paths, monkeypatch
):
    commands: list[list[str]] = []

    def _run(cmd, **_kwargs):  # noqa: ANN001
        commands.append(list(cmd))
        if "kickstart" in cmd:
            return 1, "permission denied"
        if "print" in cmd:
            return 0, "state = waiting"
        return 0, "ok"

    monkeypatch.setattr(sup, "_run", _run)

    ok, detail = LaunchdSupervisor(_spec()).start()

    assert ok is False
    assert "permission denied" in detail
    assert not any("bootstrap" in cmd or "load" in cmd for cmd in commands)


def test_launchd_status_distinguishes_loaded_from_running(monkeypatch):
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kw: (
            (0, "state = waiting")
            if "print" in cmd
            else (0, "ok")
        ),
    )

    supervisor = LaunchdSupervisor(_spec())

    assert supervisor.status() == "loaded"
    assert supervisor.is_quiescent() is False


def test_launchd_status_does_not_treat_probe_failure_as_absent(monkeypatch):
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kwargs: (1, "operation not permitted"),
    )

    assert LaunchdSupervisor(_spec()).status() == "unknown"


def test_launchd_definition_matches_current_spec(isolated_launchd_paths):
    supervisor = LaunchdSupervisor(_spec())
    path = supervisor._plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_launchd_plist(supervisor.label, supervisor.spec))

    assert supervisor.definition_matches() is True

    payload = plistlib.loads(path.read_bytes())
    payload["ProgramArguments"] = ["/old/python", *payload["ProgramArguments"][1:]]
    path.write_bytes(plistlib.dumps(payload))
    assert supervisor.definition_matches() is False


def test_launchd_quiescence_includes_the_legacy_job(monkeypatch):
    supervisor = LaunchdSupervisor(_spec())
    legacy_label = supervisor._legacy_label()

    def _run(cmd, **_kwargs):  # noqa: ANN001
        if "print" not in cmd:
            return 0, "ok"
        return (
            (0, "state = waiting")
            if legacy_label in " ".join(cmd)
            else (1, "not loaded")
        )

    monkeypatch.setattr(sup, "_run", _run)

    assert supervisor.status() == "not-installed"
    assert supervisor.is_quiescent() is False


def test_launchd_stop_fails_closed_while_job_remains_loaded(
    isolated_launchd_paths, monkeypatch
):
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kw: (
            (0, "state = running")
            if "print" in cmd
            else (1, "bootout failed")
        ),
    )
    monkeypatch.setattr(
        LaunchdSupervisor, "_wait_job_absent", lambda *_args, **_kwargs: False
    )

    ok, _ = LaunchdSupervisor(_spec()).stop()

    assert ok is False


def test_launchd_activate_retires_same_home_legacy_agent(
    isolated_launchd_paths, monkeypatch
):
    supervisor = LaunchdSupervisor(_spec())
    legacy_path = supervisor._legacy_plist_path()
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(
        plistlib.dumps(
            {
                "Label": supervisor._legacy_label(),
                "EnvironmentVariables": {"OMNI_HOME": str(supervisor.spec.home)},
            }
        )
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kw: (
            commands.append(list(cmd))
            or ((1, "not loaded") if "print" in cmd else (0, "ok"))
        ),
    )

    ok, _ = supervisor.activate()

    assert ok is True
    assert legacy_path.exists() is False
    assert any(supervisor._legacy_label() in " ".join(cmd) for cmd in commands)


def test_launchd_does_not_remove_legacy_agent_claiming_another_home(
    isolated_launchd_paths, monkeypatch
):
    supervisor = LaunchdSupervisor(_spec())
    legacy_path = supervisor._legacy_plist_path()
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(
        plistlib.dumps(
            {
                "Label": supervisor._legacy_label(),
                "EnvironmentVariables": {"OMNI_HOME": "/different/home"},
            }
        )
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kw: (commands.append(list(cmd)) or (0, "ok")),
    )

    ok, detail = supervisor.activate()

    assert ok is False
    assert "home mismatch" in detail
    assert legacy_path.exists() is True
    assert commands == []


def test_prune_orphan_launchd_agent_verifies_unload_before_delete(
    tmp_path, monkeypatch
):
    agents, plist, label = _write_orphan_launchd_plist(tmp_path)
    monkeypatch.setattr(sup.sys, "platform", "darwin")
    monkeypatch.setattr(sup, "_launchd_agents_dir", lambda: agents)
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kwargs: (
            (1, "not loaded") if "print" in cmd else (0, "ok")
        ),
    )

    pruned, failures = sup.prune_orphan_launchd_agents(timeout_s=0.0)

    assert [row["label"] for row in pruned] == [label]
    assert failures == []
    assert plist.exists() is False


@pytest.mark.parametrize(
    ("print_result", "status"),
    [
        ((0, "state = running"), "running"),
        ((1, "operation not permitted"), "unknown"),
    ],
)
def test_prune_orphan_launchd_agent_preserves_plist_without_verified_absence(
    tmp_path, monkeypatch, print_result, status
):
    agents, plist, label = _write_orphan_launchd_plist(tmp_path)
    monkeypatch.setattr(sup.sys, "platform", "darwin")
    monkeypatch.setattr(sup, "_launchd_agents_dir", lambda: agents)
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kwargs: (
            print_result if "print" in cmd else (1, "failed")
        ),
    )

    pruned, failures = sup.prune_orphan_launchd_agents(timeout_s=0.0)

    assert pruned == []
    assert failures == [
        f"{label}: could not verify launchd job was unloaded (status={status})"
    ]
    assert plist.exists() is True


def test_systemd_activate_enable_now_once_without_restart(
    isolated_systemd_paths, monkeypatch
):
    commands: list[list[str]] = []
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kw: (
            commands.append(list(cmd))
            or ((3, "inactive") if "is-active" in cmd else (0, "ok"))
        ),
    )

    ok, _ = SystemdUserSupervisor(_spec()).activate()

    assert ok is True
    assert sum("enable" in cmd and "--now" in cmd for cmd in commands) == 1
    assert not any("restart" in cmd for cmd in commands)


def test_systemd_definition_matches_current_spec(isolated_systemd_paths):
    supervisor = SystemdUserSupervisor(_spec())
    path = supervisor._unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = render_systemd_unit(supervisor.label, supervisor.spec)
    path.write_text(expected, encoding="utf-8")

    assert supervisor.definition_matches() is True

    path.write_text(expected.replace("Restart=on-failure", "Restart=no"), encoding="utf-8")
    assert supervisor.definition_matches() is False


@pytest.mark.parametrize("state", ["activating", "deactivating", "reloading"])
def test_systemd_transitional_state_is_not_quiescent(
    isolated_systemd_paths, monkeypatch, state
):
    supervisor = SystemdUserSupervisor(_spec())

    def _run(cmd, **_kwargs):  # noqa: ANN001
        if "is-active" in cmd:
            unit = cmd[-1]
            if unit == supervisor._unit_name():
                return 3, state
            return 3, "inactive"
        return 0, "ok"

    monkeypatch.setattr(sup, "_run", _run)

    assert supervisor.status() == state
    assert supervisor.is_quiescent() is False


def test_systemd_activate_retires_same_home_legacy_unit(
    isolated_systemd_paths, monkeypatch
):
    supervisor = SystemdUserSupervisor(_spec())
    legacy_path = supervisor._legacy_unit_path()
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        (
            "[Service]\n"
            f'Environment="OMNI_HOME={supervisor.spec.home}"\n'
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def _run(cmd, **_kwargs):  # noqa: ANN001
        commands.append(list(cmd))
        if "is-active" in cmd:
            return 3, "inactive"
        return 0, "ok"

    monkeypatch.setattr(sup, "_run", _run)

    ok, _ = supervisor.activate()

    assert ok is True
    assert legacy_path.exists() is False
    assert any(
        supervisor._legacy_unit_name() in cmd for cmd in commands
    )


def test_systemd_does_not_remove_legacy_unit_claiming_another_home(
    isolated_systemd_paths, monkeypatch
):
    supervisor = SystemdUserSupervisor(_spec())
    legacy_path = supervisor._legacy_unit_path()
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        '[Service]\nEnvironment="OMNI_HOME=/different/home"\n',
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kwargs: (commands.append(list(cmd)) or (0, "ok")),
    )

    ok, detail = supervisor.activate()

    assert ok is False
    assert "home mismatch" in detail
    assert legacy_path.exists() is True
    assert commands == []


def test_schtasks_activate_retires_same_home_legacy_task(monkeypatch):
    supervisor = SchtasksSupervisor(_spec())
    legacy_path = supervisor._legacy_wrapper_path()
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(render_startup_cmd(supervisor.spec), encoding="utf-8")
    commands: list[list[str]] = []

    def _run(cmd, **_kwargs):  # noqa: ANN001
        commands.append(list(cmd))
        if "/Query" in cmd and supervisor._legacy_label() in cmd:
            return 1, "not found"
        return 0, "ok"

    monkeypatch.setattr(sup, "_run", _run)

    ok, _ = supervisor.activate()

    assert ok is True
    assert legacy_path.exists() is False
    assert any(
        "/Delete" in cmd and supervisor._legacy_label() in cmd
        for cmd in commands
    )


def test_schtasks_status_does_not_treat_probe_failure_as_absent(monkeypatch):
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kwargs: (1, "access is denied"),
    )

    assert SchtasksSupervisor(_spec()).status() == "unknown"


def test_schtasks_definition_matches_wrapper_and_registered_action(
    monkeypatch,
):
    supervisor = SchtasksSupervisor(_spec())
    wrapper = supervisor._wrapper_path()
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_bytes(render_startup_cmd(supervisor.spec).encode("utf-8"))

    def _task_xml(command: str) -> str:
        return (
            '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            f"<Actions><Exec><Command>{command}</Command></Exec></Actions>"
            "</Task>"
        )

    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kwargs: (
            (0, _task_xml(str(wrapper)))
            if "/XML" in cmd
            else (0, "ok")
        ),
    )
    assert supervisor.definition_matches() is True

    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kwargs: (
            (0, _task_xml(str(wrapper.with_name("other.cmd"))))
            if "/XML" in cmd
            else (0, "ok")
        ),
    )
    assert supervisor.definition_matches() is False

    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kwargs: (
            (1, "access is denied") if "/XML" in cmd else (0, "ok")
        ),
    )
    assert supervisor.definition_status() is sup.DefinitionStatus.UNKNOWN

    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kwargs: (
            (0, "<truncated") if "/XML" in cmd else (0, "ok")
        ),
    )
    assert supervisor.definition_status() is sup.DefinitionStatus.UNKNOWN


def test_schtasks_does_not_remove_legacy_task_claiming_another_home(
    monkeypatch,
):
    supervisor = SchtasksSupervisor(_spec())
    legacy_path = supervisor._legacy_wrapper_path()
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        '@echo off\r\nset "OMNI_HOME=C:\\different\\home"\r\n',
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kwargs: (commands.append(list(cmd)) or (0, "ok")),
    )

    ok, detail = supervisor.activate()

    assert ok is False
    assert "home mismatch" in detail
    assert legacy_path.exists() is True
    assert commands == []


def test_describe_platform_reports_capabilities():
    snap = sup.describe_platform()
    assert set(snap) >= {"platform", "launchd", "systemd", "schtasks", "auto"}
