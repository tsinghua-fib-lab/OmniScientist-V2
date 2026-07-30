"""Offline contracts for channel-aware and developer ``omni update`` paths.

Everything here is network-free: git-backed installs are faked via PEP 610
``direct_url.json`` payloads, ``uv`` presence is stubbed, and any subprocess is
either asserted-against or forbidden. Also covers the installer's channel
argument validation (which exits before any network probe) and the recorded
``channel`` metadata.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import omni.cli.commands.update_cmd as uc
from omni.cli.main import app
from tests.conftest import has_usable_bash

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "cli" / "scripts" / "install.sh"


def test_update_help_keeps_advanced_compatibility_options_hidden():
    result = runner.invoke(app, ["update", "--help"])

    assert result.exit_code == 0, result.output
    for option in (
        "--force",
        "--to",
        "--ref",
        "--local",
        "--dev",
        "--editable",
        "--yes",
        "--restart-serve",
        "--no-restart-serve",
    ):
        assert option not in result.output


class _FakeDist:
    """Minimal ``importlib.metadata.Distribution`` stub exposing direct_url.json."""

    def __init__(self, direct_url: dict | None) -> None:
        self._payload = json.dumps(direct_url) if direct_url is not None else None

    def read_text(self, name: str):  # noqa: ANN201 - mirrors the metadata API
        return self._payload if name == "direct_url.json" else None


# ── ref / spec classification ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ref", "moving"),
    [
        ("master", True),
        ("main", True),
        ("develop", True),
        ("v2.0.0", False),
        ("2.0.0", False),
        ("2.0.0-rc1", False),  # release tag; a prerelease suffix needs a separator
        ("a" * 40, False),  # full commit hash
        ("", False),
    ],
)
def test_ref_is_moving(ref, moving):
    assert uc._ref_is_moving(ref) is moving


@pytest.mark.parametrize(
    ("spec", "moving"),
    [
        ("OmniScientist-V2[x] @ git+https://gitee.com/o/r.git@master#subdirectory=cli", True),
        ("OmniScientist-V2 @ git+https://gitee.com/o/r.git@v2.0.0#subdirectory=cli", False),
        ("OmniScientist-V2 @ git+https://gitee.com/o/r.git@" + "a" * 40 + "#subdirectory=cli", False),
        ("git+ssh://git@host/o/r.git", False),  # userinfo '@', no ref → not moving
        ("OmniScientist-V2==2.0.0", False),
        ("/local/path/cli", False),
    ],
)
def test_spec_is_moving_git(spec, moving):
    assert uc._spec_is_moving_git(spec) is moving


# ── _installed_source_spec: branch tip vs pinned commit ──────────────────────


def test_installed_source_spec_prefers_branch_over_commit():
    dist = _FakeDist(
        {
            "url": "https://gitee.com/o/r.git",
            "vcs_info": {"vcs": "git", "commit_id": "d" * 40, "requested_revision": "master"},
            "subdirectory": "cli",
        }
    )
    assert uc._installed_source_spec(dist) == "OmniScientist-V2 @ git+https://gitee.com/o/r.git@master#subdirectory=cli"


def test_installed_source_spec_pins_commit_without_requested_revision():
    dist = _FakeDist(
        {
            "url": "https://gitee.com/o/r.git",
            "vcs_info": {"vcs": "git", "commit_id": "d" * 40},
            "subdirectory": "cli",
        }
    )
    assert uc._installed_source_spec(dist).endswith("@" + "d" * 40 + "#subdirectory=cli")


# ── _exact_install_plan: refresh only for uv, only when requested ────────────


def test_exact_install_plan_refresh_adds_refresh_package_for_uv(monkeypatch):
    monkeypatch.setattr(uc.shutil, "which", lambda _n: "/usr/bin/uv")
    _kind, argv, _label = uc._exact_install_plan("git+x@master", reinstall=True, refresh=True)
    assert "--refresh-package" in argv
    assert argv[argv.index("--refresh-package") + 1] == uc.DIST
    _k2, argv2, _l2 = uc._exact_install_plan("x", reinstall=True, refresh=False)
    assert "--refresh-package" not in argv2


def test_exact_install_plan_pip_ignores_refresh(monkeypatch):
    monkeypatch.setattr(uc.shutil, "which", lambda _n: None)
    _kind, argv, _label = uc._exact_install_plan("git+x@master", reinstall=True, refresh=True)
    assert "--refresh-package" not in argv  # pip has no such flag; it re-resolves anyway
    assert argv[:4] == [uc.sys.executable, "-m", "pip", "install"]


def test_exact_install_plan_uv_compiles_bytecode_pip_defaults(monkeypatch):
    # uv skips .pyc by default -> compile at install so the first post-update
    # process doesn't pay a cold bytecode-compile pause. pip compiles by default.
    monkeypatch.setattr(uc.shutil, "which", lambda _n: "/usr/bin/uv")
    _kind, uv_argv, _label = uc._exact_install_plan("x", reinstall=False, refresh=False)
    assert "--compile-bytecode" in uv_argv

    monkeypatch.setattr(uc.shutil, "which", lambda _n: None)
    _kind, pip_argv, _label = uc._exact_install_plan("x", reinstall=False, refresh=False)
    assert "--compile-bytecode" not in pip_argv


def test_published_uv_tool_update_uses_the_owning_package_manager(monkeypatch):
    monkeypatch.setattr(uc, "installation_method_for_prefix", lambda _prefix: "uv")
    monkeypatch.setattr(
        uc.shutil,
        "which",
        lambda name: "/usr/bin/uv" if name == "uv" else None,
    )

    kind, argv, _label = uc._exact_install_plan(
        uc.DIST, reinstall=False, refresh=False
    )

    assert kind == "uv"
    assert argv == [
        "/usr/bin/uv",
        "tool",
        "upgrade",
        "OmniScientist-V2",
        "--compile-bytecode",
    ]


def test_uv_owned_update_never_falls_back_to_mutating_the_tool_with_pip(
    monkeypatch,
):
    monkeypatch.setattr(uc, "installation_method_for_prefix", lambda _prefix: "uv")
    monkeypatch.setattr(uc.shutil, "which", lambda _name: None)

    kind, argv, _label = uc._exact_install_plan(
        uc.DIST, reinstall=False, refresh=False
    )

    assert kind == "uv"
    assert argv[:3] == ["uv", "tool", "upgrade"]
    assert "-m" not in argv
    assert "pip" not in argv


def test_uv_owned_source_reinstall_uses_tool_install_and_refreshes_receipt(
    monkeypatch,
):
    monkeypatch.setattr(uc, "installation_method_for_prefix", lambda _prefix: "uv")
    monkeypatch.setattr(uc.shutil, "which", lambda _name: "/usr/bin/uv")

    kind, argv, _label = uc._exact_install_plan(
        "/checkout/cli", reinstall=True, editable=True, refresh=True
    )

    assert kind == "uv"
    assert argv[:4] == ["/usr/bin/uv", "tool", "install", "--force"]
    assert "--editable" in argv
    assert "--refresh-package" in argv
    assert "--python" not in argv


def test_uv_owned_source_update_preserves_the_existing_receipt(monkeypatch):
    monkeypatch.setattr(uc, "installation_method_for_prefix", lambda _prefix: "uv")
    monkeypatch.setattr(uc.shutil, "which", lambda _name: "/usr/bin/uv")

    kind, argv, _label = uc._exact_install_plan(
        "/checkout/cli",
        reinstall=True,
        editable=False,
        refresh=True,
        preserve_owner=True,
    )

    assert kind == "uv"
    assert argv[:4] == ["/usr/bin/uv", "tool", "upgrade", "OmniScientist-V2"]
    assert "--refresh-package" in argv
    assert "--reinstall-package" in argv
    assert "/checkout/cli" not in argv


def test_published_pipx_update_uses_the_owning_package_manager(monkeypatch):
    monkeypatch.setattr(uc, "installation_method_for_prefix", lambda _prefix: "pipx")
    monkeypatch.setattr(
        uc.shutil,
        "which",
        lambda name: "/usr/bin/pipx" if name == "pipx" else None,
    )

    kind, argv, _label = uc._exact_install_plan(
        uc.DIST, reinstall=False, refresh=False
    )

    assert kind == "pipx"
    assert argv == ["/usr/bin/pipx", "upgrade", "OmniScientist-V2"]


def test_pipx_owned_source_reinstall_keeps_pipx_metadata_current(monkeypatch):
    monkeypatch.setattr(uc, "installation_method_for_prefix", lambda _prefix: "pipx")
    monkeypatch.setattr(
        uc.shutil,
        "which",
        lambda name: "/usr/bin/pipx" if name == "pipx" else None,
    )

    kind, argv, _label = uc._exact_install_plan(
        "/checkout/cli", reinstall=True, editable=False, refresh=False
    )

    assert kind == "pipx"
    assert argv == ["/usr/bin/pipx", "install", "--force", "/checkout/cli"]


def test_pipx_owned_source_update_preserves_existing_metadata(monkeypatch):
    monkeypatch.setattr(uc, "installation_method_for_prefix", lambda _prefix: "pipx")
    monkeypatch.setattr(
        uc.shutil,
        "which",
        lambda name: "/usr/bin/pipx" if name == "pipx" else None,
    )

    kind, argv, _label = uc._exact_install_plan(
        "/checkout/cli",
        reinstall=True,
        refresh=False,
        preserve_owner=True,
    )

    assert kind == "pipx"
    assert argv == ["/usr/bin/pipx", "upgrade", "OmniScientist-V2", "--force"]
    assert "/checkout/cli" not in argv


def test_manager_commands_are_bound_to_the_current_custom_registry(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(uc.sys, "argv", ["python", "-m", "omni.cli.main"])
    monkeypatch.setattr(
        uc.shutil,
        "which",
        lambda name: "/another/install/bin/omni" if name == "omni" else None,
    )

    uv_prefix = tmp_path / "uv-tools" / "omniscientist-v2"
    monkeypatch.setattr(uc.sys, "prefix", str(uv_prefix))
    uv_env = uc._manager_environment("uv")
    assert uv_env is not None
    assert uv_env["UV_TOOL_DIR"] == str(uv_prefix.parent.resolve())
    assert "UV_TOOL_BIN_DIR" not in uv_env

    pipx_prefix = tmp_path / "pipx-home" / "venvs" / "omniscientist-v2"
    monkeypatch.setattr(uc.sys, "prefix", str(pipx_prefix))
    pipx_env = uc._manager_environment("pipx")
    assert pipx_env is not None
    assert pipx_env["PIPX_HOME"] == str(pipx_prefix.parent.parent.resolve())
    assert pipx_env["PIPX_MAN_DIR"] == str(
        pipx_prefix.parent.parent.resolve() / "man"
    )
    assert "PIPX_BIN_DIR" not in pipx_env


def test_package_command_passes_the_bound_manager_environment(
    monkeypatch, tmp_path: Path
):
    uv_prefix = tmp_path / "uv-tools" / "omniscientist-v2"
    monkeypatch.setattr(uc.sys, "prefix", str(uv_prefix))
    monkeypatch.setattr(uc.sys, "argv", ["python", "-m", "omni.cli.main"])
    monkeypatch.setattr(uc.shutil, "which", lambda _name: None)
    seen: dict[str, str] = {}

    def fake_run(argv, *, check, env):  # noqa: ANN001
        assert argv == ["uv", "tool", "upgrade", "OmniScientist-V2"]
        assert check is False
        seen.update(env)
        return uc.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(uc.subprocess, "run", fake_run)

    uc._run_package_command(
        ["uv", "tool", "upgrade", "OmniScientist-V2"],
        "uv",
    )

    assert seen["UV_TOOL_DIR"] == str(uv_prefix.parent.resolve())


def test_update_command_display_redacts_url_credentials_and_queries():
    rendered = uc._display_update_command(
        [
            "uv",
            "pip",
            "install",
            "OmniScientist-V2 @ git+https://user:secret@example.test/repo.git"
            "?access_token=also-secret#subdirectory=cli",
        ]
    )

    assert "secret" not in rendered
    assert "also-secret" not in rendered
    assert "https://***@example.test/repo.git?<redacted>#subdirectory=cli" in rendered


# ── post-update runtime prep always uses the newly installed CLI ──────────────


def test_prepare_runtimes_spawns_updated_cli(monkeypatch):
    paths = object()
    calls: list[list[str]] = []

    def _record_run(argv, **_k):  # noqa: ANN001
        calls.append(list(argv))
        return uc.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(uc.subprocess, "run", _record_run)
    uc._prepare_bundled_skill_runtimes_with_updated_cli(paths)
    assert calls and calls[0][1:] == ["-m", "omni.cli.main", "skills", "setup", "all"]


def test_source_pull_reports_missing_native_owner_without_traceback(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(uc, "_git_tree_is_dirty", lambda _root: False)
    monkeypatch.setattr(
        uc.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0),
    )
    monkeypatch.setattr(
        uc, "installation_method_for_prefix", lambda _prefix: "uv"
    )
    monkeypatch.setattr(
        uc,
        "_run_package_command",
        lambda _argv, _kind: (_ for _ in ()).throw(FileNotFoundError("uv")),
    )

    with pytest.raises(typer.Exit) as raised:
        uc._execute_git_update(
            repo_root=tmp_path,
            src=tmp_path / "cli",
            editable=False,
            ref="",
        )

    assert raised.value.exit_code == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "fast-forwarded" in output
    assert "Restore uv on PATH" in output


# ── _plan: branch channel reinstalls the tip; commit pin stays reproducible ──


def test_plan_branch_channel_reinstalls_tip_with_refresh(monkeypatch):
    dist = _FakeDist(
        {
            "url": "https://gitee.com/o/r.git",
            "vcs_info": {"vcs": "git", "commit_id": "d" * 40, "requested_revision": "master"},
            "subdirectory": "cli",
        }
    )
    monkeypatch.setattr(uc, "_distribution", lambda: dist)
    monkeypatch.setattr(uc, "_source_checkout", lambda dist=None: None)
    monkeypatch.setattr(uc.shutil, "which", lambda _n: "/usr/bin/uv")

    kind, argv, label = uc._plan()
    joined = " ".join(argv)
    assert "@master#subdirectory=cli" in joined
    assert "--refresh-package" in argv
    assert "channel tip" in label


def test_plan_commit_pin_is_reproducible_no_refresh(monkeypatch):
    dist = _FakeDist(
        {
            "url": "https://gitee.com/o/r.git",
            "vcs_info": {"vcs": "git", "commit_id": "d" * 40, "requested_revision": "d" * 40},
            "subdirectory": "cli",
        }
    )
    monkeypatch.setattr(uc, "_distribution", lambda: dist)
    monkeypatch.setattr(uc, "_source_checkout", lambda dist=None: None)
    monkeypatch.setattr(uc.shutil, "which", lambda _n: "/usr/bin/uv")

    _kind, argv, label = uc._plan()
    assert "--refresh-package" not in argv
    assert "channel tip" not in label
    assert ("@" + "d" * 40) in " ".join(argv)


# ── _resolve_local_checkout ──────────────────────────────────────────────────


def _make_checkout(base: Path) -> Path:
    cli = base / "cli"
    (cli / "src" / "omni").mkdir(parents=True)
    (cli / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (cli / "src" / "omni" / "__init__.py").write_text("__version__='0'\n", encoding="utf-8")
    return cli


def test_resolve_local_checkout_prefers_recorded_source(monkeypatch, tmp_path):
    cli = _make_checkout(tmp_path)
    monkeypatch.setattr(uc, "_editable_source", lambda _d: cli)
    monkeypatch.setattr(uc, "_local_source", lambda _d: None)
    monkeypatch.setattr(uc, "_distribution", lambda: None)
    assert uc._resolve_local_checkout() == cli


def test_resolve_local_checkout_none_when_unresolvable(monkeypatch, tmp_path):
    monkeypatch.setattr(uc, "_editable_source", lambda _d: None)
    monkeypatch.setattr(uc, "_local_source", lambda _d: None)
    monkeypatch.setattr(uc, "_distribution", lambda: None)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert uc._resolve_local_checkout() is None


# ── omni update --local / --dev / --editable planning ────────────────────────


def test_update_local_check_shows_snapshot_plan(monkeypatch, tmp_path):
    cli = tmp_path / "cli"
    monkeypatch.setattr(uc, "_resolve_local_checkout", lambda dist=None: cli)
    monkeypatch.setattr(uc.shutil, "which", lambda _n: "/usr/bin/uv")

    def _boom(*_a, **_k):  # pragma: no cover - only on regression
        raise AssertionError("--check must not run a subprocess")

    monkeypatch.setattr(uc.subprocess, "run", _boom)
    res = runner.invoke(app, ["update", "--local", "--check", "--no-restart-serve"])
    assert res.exit_code == 0, res.output
    # The label text is short enough to stay on one line (the long path wraps).
    assert "developer snapshot of current tree" in res.output
    assert "editable (live edits)" not in res.output


def test_update_editable_check_shows_editable_plan(monkeypatch, tmp_path):
    cli = tmp_path / "cli"
    monkeypatch.setattr(uc, "_resolve_local_checkout", lambda dist=None: cli)
    monkeypatch.setattr(uc.shutil, "which", lambda _n: "/usr/bin/uv")
    res = runner.invoke(app, ["update", "--editable", "--check", "--no-restart-serve"])
    assert res.exit_code == 0, res.output
    assert "developer editable (live edits)" in res.output


def test_update_dev_is_alias_of_local(monkeypatch, tmp_path):
    cli = tmp_path / "cli"
    monkeypatch.setattr(uc, "_resolve_local_checkout", lambda dist=None: cli)
    monkeypatch.setattr(uc.shutil, "which", lambda _n: "/usr/bin/uv")
    res = runner.invoke(app, ["update", "--dev", "--check", "--no-restart-serve"])
    assert res.exit_code == 0, res.output
    assert "developer snapshot of current tree" in res.output


def test_update_local_without_checkout_errors(monkeypatch):
    monkeypatch.setattr(uc, "_resolve_local_checkout", lambda dist=None: None)
    res = runner.invoke(app, ["update", "--local", "--no-restart-serve"])
    assert res.exit_code == 1
    assert "No local source checkout" in res.output


def test_update_local_conflicts_with_to():
    res = runner.invoke(app, ["update", "--local", "--to", "OmniScientist-V2==2.0.0", "--no-restart-serve"])
    assert res.exit_code == 2
    assert "cannot be combined" in res.output


def test_update_local_refuses_manager_owned_mode_or_checkout_transition(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(uc, "_resolve_local_checkout", lambda dist=None: tmp_path / "cli")
    monkeypatch.setattr(uc, "_source_checkout", lambda dist=None: None)
    monkeypatch.setattr(
        uc, "installation_method_for_prefix", lambda _prefix: "pipx"
    )

    res = runner.invoke(app, ["update", "--editable", "--check"])

    assert res.exit_code == 2
    assert "discard its recorded extras and package index" in res.output
    assert "./cli/scripts/install.sh --local" in res.output


def test_update_local_serializes_a_concurrent_bare_launch(
    monkeypatch, tmp_path, settings
):
    """Command wiring keeps install exclusive and restores exactly one READY owner."""
    import threading
    from types import SimpleNamespace

    from omni.cli import main as cli_main
    from omni.runtime import service_control, service_state

    # The suite intentionally points OMNI_HOME at pytest's temp tree. Product
    # code correctly refuses to launch a durable service there unless a
    # supervisor-focused test explicitly opts in.
    monkeypatch.setenv("OMNI_ALLOW_EPHEMERAL_HOST_SERVICE", "1")
    activation_calls: list[str] = []
    reservation_seen = False
    worker_done = threading.Event()

    class _Supervisor:
        id = "detached"

        def __init__(self, spec) -> None:  # noqa: ANN001
            self.paths = spec.paths

        def stop(self):
            service_state.clear_runtime(self.paths)
            return True, "stopped"

        def activate(self):
            activation_calls.append("activate")
            service_state.write_runtime(
                self.paths,
                {
                    "ready": True,
                    "phase": "ready",
                    "service_id": service_state.service_instance_id(self.paths),
                },
            )
            return True, "activated"

        def status(self):
            return (
                "running"
                if service_state.service_is_ready(self.paths)
                else "stopped"
            )

        def is_quiescent(self):
            return self.status() == "stopped"

    class _SupervisorClass:
        id = "detached"

    class _State:
        def settings(self):
            return settings

    monkeypatch.setattr(
        service_control,
        "make_supervisor",
        lambda spec, _manager="auto": _Supervisor(spec),
    )
    monkeypatch.setattr(
        service_control,
        "select_supervisor_class",
        lambda _manager="auto": _SupervisorClass,
    )
    real_lazy_enable = service_control.lazy_enable

    def _lazy_enable(*args, **kwargs):  # noqa: ANN002, ANN003
        try:
            return real_lazy_enable(*args, **kwargs)
        finally:
            worker_done.set()

    monkeypatch.setattr(service_control, "lazy_enable", _lazy_enable)
    monkeypatch.setattr(uc, "_resolve_local_checkout", lambda _dist=None: tmp_path / "cli")
    monkeypatch.setattr(
        uc,
        "_exact_install_plan",
        lambda *_args, **_kwargs: ("pip", ["fake-installer"], "fake local"),
    )
    monkeypatch.setattr(
        uc, "_prepare_bundled_skill_runtimes_with_updated_cli", lambda _paths: None
    )
    monkeypatch.setattr(uc, "list_running_daemons", lambda _home: [])

    def _install(_cmd, **_kwargs):  # noqa: ANN001
        nonlocal reservation_seen
        probe = service_state.acquire_singleton(settings.paths)
        reservation_seen = probe is None
        service_state.release_singleton(probe)
        cli_main._maybe_ensure_home_service(_State())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(uc.subprocess, "run", _install)

    result = runner.invoke(app, ["update", "--local", "--yes"])

    assert result.exit_code == 0, result.output
    assert worker_done.wait(2.0)
    assert reservation_seen is True
    assert activation_calls == ["activate"]
    assert service_state.observe_service(settings.paths).phase == "ready"
    assert service_state.start_requested(settings.paths) is False
    assert "Update completed" in result.output
    assert "stray omni serve" not in result.output


# ── installer channel argument validation (bash, offline, no install) ────────

_HAS_BASH = has_usable_bash()


@pytest.mark.skipif(not _HAS_BASH, reason="bash is required to exercise install.sh")
def test_installer_rejects_unknown_channel():
    proc = subprocess.run(
        ["bash", str(INSTALL_SH), "--channel", "bogus"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "Unknown --channel" in proc.stderr


@pytest.mark.skipif(not _HAS_BASH, reason="bash is required to exercise install.sh")
def test_installer_rejects_branch_ref_without_channel():
    # A moving branch is only allowed via the explicit --channel master; a bare
    # --remote --ref <branch> must still demand an immutable tag/commit.
    proc = subprocess.run(
        ["bash", str(INSTALL_SH), "--remote", "--ref", "master"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "not an immutable" in proc.stderr


# ── recorded channel metadata ────────────────────────────────────────────────


def test_record_installation_persists_channel(settings):
    from omni.runtime import uninstall

    path = uninstall.record_installation(
        settings.paths,
        method="uv",
        source="OmniScientist-V2 @ git+https://gitee.com/o/r.git@master#subdirectory=cli",
        channel="master",
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["installations"][-1]["channel"] == "master"
