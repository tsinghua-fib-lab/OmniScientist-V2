"""Desired vs. observed home-service state, and the lifecycle lock."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from omni.config import load_settings
from omni.runtime import service_state
from omni.runtime.service_state import ServiceDesiredState


def _paths():
    return load_settings().paths


def test_desired_defaults_to_disabled_and_unconfigured():
    desired = service_state.read_desired(_paths())
    assert desired.enabled is False
    assert desired.configured is False
    assert desired.manager == "auto"


def test_desired_roundtrip_persists_choice():
    paths = _paths()
    service_state.write_desired(
        paths,
        ServiceDesiredState(enabled=True, configured=True, manager="detached", channel_anchor="default"),
    )
    reloaded = service_state.read_desired(paths)
    assert reloaded.enabled is True
    assert reloaded.configured is True
    assert reloaded.manager == "detached"
    assert reloaded.channel_anchor == "default"
    assert reloaded.updated_at > 0


def test_desired_ignores_unknown_keys_and_corruption():
    paths = _paths()
    path = service_state.desired_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"enabled": true, "bogus": 1}', encoding="utf-8")
    desired = service_state.read_desired(paths)
    assert desired.enabled is True
    # Corrupt JSON falls back to safe defaults rather than raising.
    path.write_text("{not json", encoding="utf-8")
    assert service_state.read_desired(paths).enabled is False


def test_runtime_state_liveness_uses_pid_and_heartbeat():
    paths = _paths()
    # A runtime row for *this* live process with a fresh heartbeat is "running".
    service_state.write_runtime(paths, {"version": "x", "ready": True})
    assert service_state.service_is_running(paths) is True
    info = service_state.service_runtime_info(paths)
    assert info is not None and info["pid"] == os.getpid()

    # A dead pid is never considered running.
    service_state.write_runtime(paths, {"pid": 2, "version": "x"})
    # write_runtime overwrites pid with our own; force a dead pid on disk.
    path = service_state.runtime_path(paths)
    import json

    data = json.loads(path.read_text())
    data["pid"] = 999_999_999
    path.write_text(json.dumps(data), encoding="utf-8")
    assert service_state.service_is_running(paths) is False


def test_stale_heartbeat_is_not_running():
    paths = _paths()
    service_state.write_runtime(paths, {"ready": True})
    path = service_state.runtime_path(paths)
    import json

    data = json.loads(path.read_text())
    data["heartbeat"] = 0.0  # ancient
    path.write_text(json.dumps(data), encoding="utf-8")
    assert service_state.service_is_running(paths) is False


def test_clear_runtime_only_by_owner():
    paths = _paths()
    service_state.write_runtime(paths, {"ready": True})
    assert service_state.clear_runtime_if_owner(paths) is True
    assert service_state.read_runtime(paths) is None


def test_lifecycle_lock_serializes_and_leaves_a_stable_lock_inode():
    paths = _paths()
    paths.service_dir.mkdir(parents=True, exist_ok=True)
    with service_state.lifecycle_lock(paths):
        lock = paths.service_dir / "lifecycle.lock"
        assert lock.exists()
        with pytest.raises(service_state.LifecycleLockTimeout):
            with service_state.lifecycle_lock(paths, timeout_s=0.0):
                pass

    # The inode remains stable so an unlock/unlink race cannot split contenders
    # across two different files. The kernel lock itself is now available.
    assert lock.exists()
    with service_state.lifecycle_lock(paths, timeout_s=1.0):
        pass


def test_lifecycle_lock_is_kernel_released_when_holder_process_exits():
    paths = _paths()
    code = (
        "import sys\n"
        "from omni.config import load_settings\n"
        "from omni.runtime.service_state import lifecycle_lock\n"
        "with lifecycle_lock(load_settings().paths):\n"
        "    print('LOCKED', flush=True)\n"
        "    sys.stdin.read(1)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(service_state.LifecycleLockTimeout):
            with service_state.lifecycle_lock(paths, timeout_s=0.0):
                pass
        assert child.stdin is not None
        child.stdin.write("x")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0
        with service_state.lifecycle_lock(paths, timeout_s=1.0):
            pass
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_default_launcher_binds_to_current_interpreter():
    paths = _paths()
    argv = service_state.default_launcher(paths)
    service_id = service_state.service_instance_id(paths)
    assert argv[1:3] == ["-X", f"omni_service_id={service_id}"]
    assert argv[3:] == ["-m", "omni.cli.main", "serve", "run"]
    assert "--service-id" not in argv
    assert argv[0]  # sys.executable


def test_launcher_identity_is_consumed_by_python_before_cli_parsing():
    """The identity marker must remain compatible with older Omni CLIs.

    ``-X`` belongs to the Python interpreter, so it stays visible in ``ps`` but
    is not forwarded to Typer as an unknown ``serve run`` option.
    """
    paths = _paths()
    argv = service_state.default_launcher(paths)
    expected_id = service_state.service_instance_id(paths)
    probe = subprocess.run(
        [
            argv[0],
            *argv[1:3],
            "-c",
            (
                "import json,sys;"
                "print(json.dumps([sys._xoptions.get('omni_service_id'),sys.argv]))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(probe.stdout) == [expected_id, ["-c"]]


# ── home-service singleton lock ──────────────────────────────────────────────


def test_singleton_lock_is_exclusive_and_records_holder():
    paths = _paths()
    fd = service_state.acquire_singleton(paths)
    assert fd is not None
    try:
        # A second acquirer (a duplicate spawn) is denied while the lock is held —
        # this is what makes any redundant ``omni serve run`` exit instead of
        # coexisting.
        assert service_state.acquire_singleton(paths) is None
        # The holder pid is recorded and reported for status/doctor.
        assert service_state.singleton_holder_pid(paths) == os.getpid()
    finally:
        service_state.release_singleton(fd)


def test_singleton_lock_is_reacquirable_after_release():
    paths = _paths()
    fd = service_state.acquire_singleton(paths)
    assert fd is not None
    service_state.release_singleton(fd)
    # Once released (clean shutdown; the kernel does the same on crash), the next
    # service can take ownership.
    fd2 = service_state.acquire_singleton(paths)
    assert fd2 is not None
    service_state.release_singleton(fd2)


def test_singleton_holder_is_none_when_unlocked():
    paths = _paths()
    assert service_state.singleton_holder_pid(paths) is None


def test_update_singleton_reservation_is_not_a_running_service():
    paths = _paths()
    fd = service_state.acquire_singleton(paths, role="update")
    assert fd is not None
    try:
        info = service_state.singleton_holder_info(paths)
        assert info is not None
        assert info["pid"] == os.getpid()
        assert info["role"] == "update"
        assert service_state.singleton_holder_pid(paths) == os.getpid()
        assert service_state.observe_service(paths).phase == "down"
        assert service_state.service_is_active(paths) is False
    finally:
        service_state.release_singleton(fd)


def test_observation_distinguishes_starting_owner_from_ready_runtime():
    """A live singleton owner is STARTING before it publishes a ready heartbeat."""
    paths = _paths()
    fd = service_state.acquire_singleton(paths)
    assert fd is not None
    try:
        starting = service_state.observe_service(paths)
        assert starting.phase == "starting"
        assert starting.pid == os.getpid()
        assert service_state.service_is_active(paths) is True
        assert service_state.service_is_ready(paths) is False

        service_state.write_runtime(paths, {"ready": True, "phase": "ready"})
        ready = service_state.observe_service(paths)
        assert ready.phase == "ready"
        assert ready.pid == os.getpid()
        assert service_state.service_is_ready(paths) is True
    finally:
        service_state.clear_runtime_if_owner(paths)
        service_state.release_singleton(fd)


def test_observation_reports_full_runtime_phase_progression():
    paths = _paths()
    fd = service_state.acquire_singleton(paths)
    assert fd is not None
    try:
        service_state.write_runtime(paths, {"ready": False, "phase": "starting"})
        assert service_state.observe_service(paths).phase == "starting"

        service_state.write_runtime(paths, {"ready": True, "phase": "ready"})
        assert service_state.observe_service(paths).phase == "ready"

        service_state.write_runtime(paths, {"ready": False, "phase": "stopping"})
        assert service_state.observe_service(paths).phase == "stopping"

        import json

        runtime_path = service_state.runtime_path(paths)
        payload = json.loads(runtime_path.read_text(encoding="utf-8"))
        payload["heartbeat"] = 0.0
        runtime_path.write_text(json.dumps(payload), encoding="utf-8")
        unhealthy = service_state.observe_service(paths)
        assert unhealthy.phase == "unhealthy"
        assert unhealthy.active is True
    finally:
        service_state.release_singleton(fd)

    stale = service_state.observe_service(paths)
    assert stale.phase == "stale"
    assert stale.active is False
    service_state.clear_runtime(paths)
    assert service_state.observe_service(paths).phase == "down"
