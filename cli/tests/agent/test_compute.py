"""Compute backends (P1-E): planning, availability, local exec, ssh/slurm/modal,
fallback, and the run_compute tool guards."""

from __future__ import annotations

from typing import Any

import pytest

from omni.config import load_settings
from omni.runtime import compute
from omni.runtime.compute import (
    BACKENDS,
    ComputeBackend,
    backend_available,
    backend_names,
    get_backend,
    plan_docker,
    plan_slurm,
    plan_ssh,
    run_compute,
)
from omni.skills_runtime.builtin_tools.compute import build_compute_tools
from omni.skills_runtime.context import ExecContext


def _cfg(**overrides: Any):
    cfg = load_settings().compute
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ── pure planning ────────────────────────────────────────────────────────────
def test_plan_ssh_argv():
    cfg = _cfg(ssh_host="me@host", ssh_port=2222, ssh_key="/k", workdir="/work")
    argv = plan_ssh("echo hi", cfg)
    assert argv[0] == "ssh"
    assert "me@host" in argv
    assert "-p" in argv and "2222" in argv
    assert "-i" in argv and "/k" in argv
    # workdir is prepended to the remote command
    assert argv[-1] == "cd /work && echo hi"


def test_plan_ssh_default_port_omitted():
    argv = plan_ssh("ls", _cfg(ssh_host="h"))
    assert "-p" not in argv


def test_plan_slurm_local_and_via_ssh():
    local = plan_slurm("python x.py", _cfg(slurm_partition="gpu", slurm_cpus=4, slurm_mem="8G"))
    assert local[0] == "sbatch"
    assert "--parsable" in local
    assert "--partition" in local and "gpu" in local
    assert local[-1] == "--wrap=python x.py"

    via = plan_slurm("python x.py", _cfg(ssh_host="h", slurm_via_ssh=True))
    assert via[0] == "ssh"  # tunnelled through ssh


def test_plan_docker_argv():
    cfg = _cfg(
        docker_image="python:3.12-slim", docker_gpus="all",
        docker_mounts=["/data:/data:ro"], workdir="/work",
    )
    argv = plan_docker("python x.py && echo ok", cfg)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--gpus" in argv and "all" in argv
    assert "-v" in argv and "/data:/data:ro" in argv
    assert "-w" in argv and "/work" in argv
    # command runs through /bin/sh -c inside the image so shell syntax works
    assert argv[-4:] == ["python:3.12-slim", "/bin/sh", "-c", "python x.py && echo ok"]


def test_plan_docker_minimal_omits_optional_flags():
    argv = plan_docker("echo hi", _cfg(docker_image="img"))
    assert "--gpus" not in argv
    assert "-v" not in argv
    assert argv[-4:] == ["img", "/bin/sh", "-c", "echo hi"]


# ── availability ─────────────────────────────────────────────────────────────
def test_backend_available_matrix(monkeypatch):
    assert backend_available("local", _cfg())[0] is True
    assert backend_available("ssh", _cfg(ssh_host=""))[0] is False
    monkeypatch.setattr(compute.shutil, "which", lambda _n: "/usr/bin/ssh")
    assert backend_available("ssh", _cfg(ssh_host="h"))[0] is True
    assert backend_available("modal", _cfg(modal_app=""))[0] is False
    assert backend_available("weird", _cfg())[0] is False


def test_docker_backend_available(monkeypatch):
    assert backend_available("docker", _cfg(docker_image=""))[0] is False
    monkeypatch.setattr(compute.shutil, "which", lambda _n: None)
    ok, reason = backend_available("docker", _cfg(docker_image="img"))
    assert ok is False and "docker binary" in reason
    monkeypatch.setattr(compute.shutil, "which", lambda _n: "/usr/bin/docker")
    assert backend_available("docker", _cfg(docker_image="img"))[0] is True


# ── backend abstraction / registry ───────────────────────────────────────────
def test_backend_registry_lists_all_backends():
    assert set(backend_names()) == {"local", "docker", "ssh", "slurm", "modal"}
    assert get_backend("weird") is None
    for name in backend_names():
        impl = get_backend(name)
        assert impl is not None and impl.name == name
        # every backend satisfies the ComputeBackend protocol (runtime-checkable)
        assert isinstance(impl, ComputeBackend)


def test_backends_map_keys_match_backend_names():
    assert set(BACKENDS) == set(backend_names())


# ── local execution (real, offline) ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_local_backend_runs():
    res = await run_compute("echo hello123", cfg=_cfg(backend="local"))
    assert res.backend == "local"
    assert res.status == "ok"
    assert res.returncode == 0
    assert "hello123" in res.stdout


@pytest.mark.asyncio
async def test_local_backend_nonzero():
    res = await run_compute("exit 3", cfg=_cfg(backend="local"))
    assert res.status == "error"
    assert res.returncode == 3


@pytest.mark.asyncio
async def test_empty_command_errors():
    res = await run_compute("   ", cfg=_cfg())
    assert res.status == "error"


# ── ssh / slurm via injected executor ────────────────────────────────────────
@pytest.mark.asyncio
async def test_ssh_backend_builds_and_runs(monkeypatch):
    monkeypatch.setattr(compute.shutil, "which", lambda _n: "/usr/bin/ssh")
    captured: dict[str, Any] = {}

    async def fake_exec(argv, *, shell, cwd, timeout):
        captured["argv"] = argv
        captured["shell"] = shell
        return 0, "REMOTE-OK"

    monkeypatch.setattr(compute, "_exec", fake_exec)
    res = await run_compute("nvidia-smi", cfg=_cfg(backend="ssh", ssh_host="me@gpu"))
    assert res.status == "ok"
    assert res.detail == "me@gpu"
    assert res.stdout == "REMOTE-OK"
    assert captured["argv"][0] == "ssh" and captured["shell"] is False


@pytest.mark.asyncio
async def test_slurm_backend_parses_job_id(monkeypatch):
    monkeypatch.setattr(compute.shutil, "which", lambda _n: "/usr/bin/sbatch")

    async def fake_exec(argv, *, shell, cwd, timeout):
        return 0, "12345\n"

    monkeypatch.setattr(compute, "_exec", fake_exec)
    res = await run_compute("python train.py", cfg=_cfg(backend="slurm", slurm_via_ssh=False))
    assert res.status == "submitted"
    assert res.detail == "job 12345"


# ── fallback semantics ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_unconfigured_ssh_falls_back_to_local():
    res = await run_compute("echo fb", cfg=_cfg(backend="ssh", ssh_host="", fallback_local=True))
    assert res.backend == "local"
    assert res.status == "ok"
    assert "fell back to local" in res.detail


@pytest.mark.asyncio
async def test_unconfigured_ssh_errors_without_fallback():
    res = await run_compute("echo fb", cfg=_cfg(backend="ssh", ssh_host="", fallback_local=False))
    assert res.backend == "ssh"
    assert res.status == "error"


@pytest.mark.asyncio
async def test_modal_unavailable_falls_back(monkeypatch):
    monkeypatch.setattr(compute, "modal_available", lambda: False)
    res = await run_compute("echo m", cfg=_cfg(backend="modal", modal_app="app", fallback_local=True))
    assert res.backend == "local"
    assert res.status == "ok"


@pytest.mark.asyncio
async def test_docker_backend_builds_and_runs(monkeypatch):
    monkeypatch.setattr(compute.shutil, "which", lambda _n: "/usr/bin/docker")
    captured: dict[str, Any] = {}

    async def fake_exec(argv, *, shell, cwd, timeout):
        captured["argv"] = argv
        return 0, "CONTAINER-OK"

    monkeypatch.setattr(compute, "_exec", fake_exec)
    res = await run_compute("python train.py", cfg=_cfg(backend="docker", docker_image="img:1"))
    assert res.backend == "docker"
    assert res.status == "ok"
    assert res.detail == "img:1"
    assert res.stdout == "CONTAINER-OK"
    assert captured["argv"][0] == "docker" and "img:1" in captured["argv"]


@pytest.mark.asyncio
async def test_docker_unconfigured_falls_back_to_local():
    res = await run_compute("echo d", cfg=_cfg(backend="docker", docker_image="", fallback_local=True))
    assert res.backend == "local"
    assert res.status == "ok"
    assert "fell back to local" in res.detail


# ── tool surface ─────────────────────────────────────────────────────────────
def _ctx(**kw):
    s = load_settings()
    s.paths.ensure_dirs()
    return ExecContext(settings=s, paths=s.paths, **kw)


@pytest.mark.asyncio
async def test_run_compute_tool_runs_local():
    tools = {t.spec.name: t for t in build_compute_tools(_ctx(channel="cli"))}
    assert "run_compute" in tools
    out = await tools["run_compute"].handler({"command": "echo tool-ok"})
    assert out["status"] == "ok"
    assert "tool-ok" in out["stdout"]


@pytest.mark.asyncio
async def test_run_compute_tool_blocks_destructive():
    tools = {t.spec.name: t for t in build_compute_tools(_ctx(channel="cli"))}
    out = await tools["run_compute"].handler({"command": "sudo rm -rf /"})
    assert out["status"] == "error"
    assert "sandbox" in out["error"]


@pytest.mark.asyncio
async def test_run_compute_tool_blocks_on_im_channel():
    tools = {t.spec.name: t for t in build_compute_tools(_ctx(channel="wechat"))}
    out = await tools["run_compute"].handler({"command": "echo hi"})
    assert out["status"] == "error"
    assert "confirmation" in out["error"]


@pytest.mark.asyncio
async def test_run_compute_tool_accepts_docker_backend_and_falls_back():
    # ``docker`` is now a first-class backend; unconfigured → transparent local
    # fallback (not "unknown backend").
    tools = {t.spec.name: t for t in build_compute_tools(_ctx(channel="cli"))}
    out = await tools["run_compute"].handler({"command": "echo dk", "backend": "docker"})
    assert out["status"] == "ok"
    assert out["backend"] == "local"
    assert "fell back to local" in out["detail"]


@pytest.mark.asyncio
async def test_run_compute_tool_rejects_unknown_backend():
    tools = {t.spec.name: t for t in build_compute_tools(_ctx(channel="cli"))}
    out = await tools["run_compute"].handler({"command": "echo x", "backend": "quantum"})
    assert out["status"] == "error"
    assert "unknown backend" in out["error"]
