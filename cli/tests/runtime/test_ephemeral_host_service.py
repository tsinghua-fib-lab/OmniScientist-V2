"""Guards that stop throwaway OMNI_HOME from installing durable host units."""

from __future__ import annotations

from pathlib import Path

from omni.config import load_settings
from omni.runtime import service_control
from omni.runtime import service_supervisors as sup
from omni.runtime.service_supervisors import (
    DetachedSupervisor,
    LaunchdSupervisor,
    is_ephemeral_omni_home,
)

# Capture before conftest's autouse inert mock replaces ``start``.
_REAL_DETACHED_START = DetachedSupervisor.__dict__["start"]


def test_is_ephemeral_omni_home_detects_pytest_and_temp(tmp_path: Path) -> None:
    assert is_ephemeral_omni_home(tmp_path / "omni") is True
    assert is_ephemeral_omni_home(Path("/tmp/omni-throwaway")) is True
    # A durable project-local home must never be treated as ephemeral.
    # Do not use Path.home(): the suite's isolated_home fixture points HOME at
    # a pytest temp tree, which is correctly classified as ephemeral.
    assert is_ephemeral_omni_home(Path.cwd() / ".omni") is False


def test_launchd_install_refuses_ephemeral_omni_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", raising=False)
    settings = load_settings()
    assert is_ephemeral_omni_home(settings.paths.home)

    calls: list[list[str]] = []
    monkeypatch.setattr(
        sup,
        "_run",
        lambda cmd, **_kw: (calls.append(list(cmd)) or (0, "ok")),
    )
    real_agents = Path.home() / "Library" / "LaunchAgents"
    before = set(real_agents.glob("com.omniscientist.omni.*.plist")) if real_agents.is_dir() else set()

    ok, detail = LaunchdSupervisor(service_control._spec(settings)).install()

    assert ok is False
    assert "ephemeral OMNI_HOME" in detail
    assert calls == []
    after = set(real_agents.glob("com.omniscientist.omni.*.plist")) if real_agents.is_dir() else set()
    assert after == before


def test_detached_start_refuses_ephemeral_omni_home(monkeypatch) -> None:
    monkeypatch.delenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", raising=False)
    settings = load_settings()
    spawned: list[object] = []
    monkeypatch.setattr(
        sup.subprocess,
        "Popen",
        lambda *a, **k: spawned.append((a, k)),
    )
    # conftest neuters DetachedSupervisor.start; exercise the real method here.
    monkeypatch.setattr(DetachedSupervisor, "start", _REAL_DETACHED_START, raising=True)

    ok, detail = DetachedSupervisor(service_control._spec(settings)).start()

    assert ok is False
    assert "ephemeral OMNI_HOME" in detail
    assert spawned == []


def test_allow_env_re_enables_ephemeral_host_install(monkeypatch) -> None:
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    settings = load_settings()
    calls: list[list[str]] = []

    def _run(cmd, **_kw):  # noqa: ANN001
        calls.append(list(cmd))
        if "print" in cmd or "bootout" in cmd:
            return 1, "not loaded"
        return 0, "ok"

    monkeypatch.setattr(sup, "_run", _run)
    monkeypatch.setattr(
        LaunchdSupervisor,
        "_plist_path",
        lambda self: settings.paths.home / f"{self.label}.plist",
    )
    monkeypatch.setattr(
        LaunchdSupervisor,
        "_legacy_plist_path",
        lambda self: settings.paths.home / f"{self._legacy_label()}.plist",
    )
    monkeypatch.setattr(
        LaunchdSupervisor, "_wait_job_absent", lambda *_a, **_k: True
    )

    ok, _ = LaunchdSupervisor(service_control._spec(settings)).activate()
    assert ok is True
    assert any("bootstrap" in cmd or "load" in cmd for cmd in calls)
