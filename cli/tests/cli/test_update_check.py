"""Offline contracts for the startup update-notifier + version-aware ``omni update``.

Everything here is network-free: HTTP is faked by swapping ``httpx.Client``, and
the CLI short-circuit is exercised with the remote fetch stubbed.
"""

from __future__ import annotations

import time

import httpx
import pytest
from typer.testing import CliRunner

from omni import __version__
from omni.cli.main import app
from omni.runtime import update_check

runner = CliRunner()


# ── fake HTTP plumbing ──────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code: int, json_data=None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, handler) -> None:
        self._handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, url, headers=None):  # noqa: ARG002
        return self._handler(url)


def _install_fake_http(monkeypatch, handler) -> None:
    monkeypatch.setattr(update_check.httpx, "Client", lambda **_kw: _FakeClient(handler))


# ── version comparison (PEP 440) ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "latest", "expected"),
    [
        ("0.2.0", "0.2.1", True),
        ("0.2.0", "0.2.0", False),
        ("0.2.1", "0.2.0", False),
        ("0.2.0", "1.0.0", True),
        ("0.2.0", None, False),
        ("0.2.0", "", False),
        ("0.2.0", "not-a-version", False),
        ("1.0.0", "1.0.0a1", False),  # a prerelease is *older* than the final
    ],
)
def test_newer_available(current, latest, expected):
    assert update_check.newer_available(current, latest) is expected


# ── cache TTL + robustness ───────────────────────────────────────────────────


def test_cache_is_fresh_respects_interval():
    now = time.time()
    assert update_check.cache_is_fresh({"checked_at": now}, 24) is True
    assert update_check.cache_is_fresh({"checked_at": now - 100 * 3600}, 24) is False
    assert update_check.cache_is_fresh({}, 24) is False
    assert update_check.cache_is_fresh({"checked_at": "bogus"}, 24) is False


def test_cache_read_write_roundtrip_and_corruption(settings):
    paths = settings.paths
    assert update_check.read_cache(paths) == {}  # missing → {}
    update_check.write_cache(paths, {"latest": "0.9.0", "checked_at": 123.0})
    assert update_check.read_cache(paths)["latest"] == "0.9.0"
    # Corrupt JSON degrades to {} rather than raising.
    (paths.home / "update-check.json").write_text("{not json", encoding="utf-8")
    assert update_check.read_cache(paths) == {}


# ── enable switch (config + env) ─────────────────────────────────────────────


def test_update_check_enabled_default_and_env(monkeypatch, settings):
    assert update_check.update_check_enabled(settings) is True
    monkeypatch.setenv("OMNI_UPDATE_CHECK", "0")
    assert update_check.update_check_enabled(settings) is False


def test_update_check_disabled_by_config(settings):
    settings.update.check = False
    assert update_check.update_check_enabled(settings) is False


# ── pending notice (menu decision, pure) ─────────────────────────────────────


def test_pending_update_notice_flags_newer(settings):
    update_check.write_cache(settings.paths, {"latest": "9.9.9", "checked_at": time.time()})
    assert update_check.pending_update_notice(__version__, settings.paths, settings) == "9.9.9"


def test_pending_update_notice_none_when_current_or_older(settings):
    update_check.write_cache(settings.paths, {"latest": __version__})
    assert update_check.pending_update_notice(__version__, settings.paths, settings) is None


def test_pending_update_notice_respects_skip_version(settings):
    update_check.write_cache(settings.paths, {"latest": "9.9.9", "skip_version": "9.9.9"})
    assert update_check.pending_update_notice(__version__, settings.paths, settings) is None
    # A newer-than-skip version still notifies.
    update_check.write_cache(settings.paths, {"latest": "9.9.10", "skip_version": "9.9.9"})
    assert update_check.pending_update_notice(__version__, settings.paths, settings) == "9.9.10"


def test_pending_update_notice_silent_when_disabled(monkeypatch, settings):
    update_check.write_cache(settings.paths, {"latest": "9.9.9"})
    monkeypatch.setenv("OMNI_UPDATE_CHECK", "0")
    assert update_check.pending_update_notice(__version__, settings.paths, settings) is None


def test_mark_skip_version(settings):
    update_check.write_cache(settings.paths, {"latest": "9.9.9"})
    update_check.mark_skip_version(settings.paths, "9.9.9")
    assert update_check.read_cache(settings.paths)["skip_version"] == "9.9.9"


# ── network fetch (source routing + raw parsing) ─────────────────────────────


def test_fetch_auto_prefers_pypi(monkeypatch, settings):
    settings.update.source = "auto"

    def handler(url):
        if "pypi.org" in url:
            return _FakeResp(200, json_data={"info": {"version": "1.2.3"}})
        raise AssertionError("raw fallback should not be hit when PyPI succeeds")

    _install_fake_http(monkeypatch, handler)
    assert update_check.fetch_latest_version(settings) == "1.2.3"


def test_fetch_auto_falls_back_to_raw(monkeypatch, settings):
    settings.update.source = "auto"
    settings.update.raw_url = "https://example.test/omni/__init__.py"

    def handler(url):
        if "pypi.org" in url:
            return _FakeResp(404)
        return _FakeResp(200, text='__version__ = "2.0.0"\n')

    _install_fake_http(monkeypatch, handler)
    assert update_check.fetch_latest_version(settings) == "2.0.0"


def test_fetch_source_raw_parses_single_quotes(monkeypatch, settings):
    settings.update.source = "raw"
    settings.update.raw_url = "https://example.test/omni/__init__.py"

    def handler(url):
        assert "pypi.org" not in url  # pinned source must not touch PyPI
        return _FakeResp(200, text="__version__ = '3.1.4'  # noqa\n")

    _install_fake_http(monkeypatch, handler)
    assert update_check.fetch_latest_version(settings) == "3.1.4"


def test_fetch_source_pypi_only(monkeypatch, settings):
    settings.update.source = "pypi"

    def handler(url):
        assert "pypi.org" in url  # never falls back to raw
        return _FakeResp(404)

    _install_fake_http(monkeypatch, handler)
    assert update_check.fetch_latest_version(settings) is None


def test_pypi_is_the_default_release_authority(settings):
    assert settings.update.source == "pypi"


def test_fetch_offline_returns_none(monkeypatch, settings):
    def handler(_url):
        raise httpx.ConnectError("offline")

    _install_fake_http(monkeypatch, handler)
    assert update_check.fetch_latest_version(settings) is None


# ── background refresh ───────────────────────────────────────────────────────


def test_background_refresh_skips_when_fresh(monkeypatch, settings):
    update_check.write_cache(settings.paths, {"latest": "0.1.0", "checked_at": time.time()})
    monkeypatch.setattr(
        update_check, "fetch_latest_version", lambda *_a, **_k: pytest.fail("should not fetch")
    )
    assert update_check.maybe_refresh_in_background(settings) is None


def test_background_refresh_writes_cache_when_stale(monkeypatch, settings):
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda *_a, **_k: "5.5.5")
    thread = update_check.maybe_refresh_in_background(settings)
    assert thread is not None
    thread.join(timeout=5)
    cache = update_check.read_cache(settings.paths)
    assert cache["latest"] == "5.5.5"
    assert isinstance(cache["checked_at"], (int, float))


def test_clear_update_state_removes_cache(settings):
    update_check.write_cache(settings.paths, {"latest": "9.9.9"})
    update_check.clear_update_state(settings.paths)
    assert update_check.read_cache(settings.paths) == {}


def test_startup_update_action_uses_the_parameterless_public_command(monkeypatch):
    from types import SimpleNamespace

    from omni.cli import main as cli_main

    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli_main.subprocess,
        "run",
        lambda argv, **_kwargs: calls.append(list(argv))
        or SimpleNamespace(returncode=0),
    )

    assert cli_main._run_update_now() is True
    assert calls == [
        [
            cli_main.sys.executable,
            "-m",
            "omni.cli.main",
            "update",
        ]
    ]


def test_update_child_reuses_the_exact_installed_launcher(monkeypatch, tmp_path):
    from omni.cli import main as cli_main

    launcher = tmp_path / "custom-bin" / "omni"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(cli_main.sys, "argv", [str(launcher)])

    assert cli_main._fresh_cli_command(["update"]) == [
        str(launcher),
        "update",
    ]


def test_startup_update_prompt_restarts_only_after_a_successful_update(
    settings, monkeypatch
):
    from omni.cli import main as cli_main

    monkeypatch.setattr(
        update_check,
        "pending_update_notice",
        lambda *_a, **_k: "9.9.9",
    )
    monkeypatch.setattr(cli_main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_main.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli_main.console, "input", lambda _prompt: "")

    monkeypatch.setattr(cli_main, "_run_update_now", lambda: False)
    assert cli_main._maybe_prompt_update(settings) is False

    monkeypatch.setattr(cli_main, "_run_update_now", lambda: True)
    assert cli_main._maybe_prompt_update(settings) is True


# ── omni update version-aware short-circuit ──────────────────────────────────


def _pin_published_install(monkeypatch, uc) -> None:
    """Pin the *published* update path for the version-comparison short-circuit.

    The real test interpreter is an editable git checkout, which now takes the
    git two-phase path (git state decides freshness). These version-string tests
    want the published branch, so we force a non-checkout plan and treat the
    current version as a comparable release.
    """
    monkeypatch.setattr(
        uc,
        "_plan",
        lambda ref="", **_kwargs: (
            "uv",
            ["uv", "pip", "install", "-U", uc.DIST],
            "uv fake",
        ),
    )
    monkeypatch.setattr(update_check, "is_source_build_version", lambda _v: False)


def test_update_skips_package_download_but_converges_when_already_latest(monkeypatch):
    import omni.cli.commands.update_cmd as uc

    _pin_published_install(monkeypatch, uc)
    fetches = {"n": 0}
    prepared: list[object] = []

    def _fetch(*_a, **_k):
        fetches["n"] += 1
        return __version__

    monkeypatch.setattr(update_check, "fetch_latest_version", _fetch)
    monkeypatch.setattr(
        uc,
        "_prepare_bundled_skill_runtimes_with_updated_cli",
        lambda paths: prepared.append(paths),
    )

    def _boom(*_a, **_k):  # pragma: no cover - only on regression
        raise AssertionError("must not run a package command when already latest")

    monkeypatch.setattr(uc.subprocess, "run", _boom)
    res = runner.invoke(app, ["update"])
    assert res.exit_code == 0
    assert "already up to date" in res.output
    assert "No Python package download is required" in res.output
    assert fetches["n"] == 1
    assert len(prepared) == 1


def test_update_repairs_bundled_skill_runtime_even_when_already_latest(monkeypatch):
    import omni.cli.commands.update_cmd as uc

    _pin_published_install(monkeypatch, uc)
    prepared: list[object] = []
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda *_a, **_k: __version__)
    monkeypatch.setattr(
        uc,
        "_prepare_bundled_skill_runtimes_with_updated_cli",
        lambda paths: prepared.append(paths),
    )

    res = runner.invoke(app, ["update"])

    assert res.exit_code == 0, res.output
    assert len(prepared) == 1


def test_editable_update_pulls_then_syncs_before_fresh_runtime_setup(monkeypatch):
    from types import SimpleNamespace

    import omni.cli.commands.update_cmd as uc

    repo = uc.Path("/repo")
    # Editable source checkout that is behind upstream on a clean tree → the
    # two-phase source path: fast-forward pull, editable dependency re-sync, then
    # a fresh-process runtime setup.
    monkeypatch.setattr(uc, "_source_checkout", lambda *_a, **_k: (repo, repo / "cli", True))
    monkeypatch.setattr(uc, "_git_behind_count", lambda *_a, **_k: 1)
    monkeypatch.setattr(uc, "_git_tree_is_dirty", lambda *_a, **_k: False)
    monkeypatch.setattr(
        uc,
        "_editable_dependency_sync_plan",
        lambda: ["uv", "pip", "install", "--editable", str(repo / "cli")],
        raising=False,
    )
    monkeypatch.setattr(
        uc,
        "setup_research_pptx_runtime",
        lambda _paths: pytest.fail("updated runtime setup must not use the old imported implementation"),
        raising=False,
    )
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(uc.subprocess, "run", fake_run)

    res = runner.invoke(
        app,
        ["update", "--yes", "--no-restart-serve"],
    )

    assert res.exit_code == 0, res.output
    assert calls == [
        ["git", "-C", str(repo), "pull", "--ff-only"],
        ["uv", "pip", "install", "--editable", str(repo / "cli")],
        [
            uc.sys.executable,
            "-m",
            "omni.cli.main",
            "skills",
            "setup",
                "all",
        ],
    ]


def test_non_editable_update_uses_fresh_cli_for_runtime_setup(monkeypatch):
    from types import SimpleNamespace

    import omni.cli.commands.update_cmd as uc

    monkeypatch.setattr(update_check, "fetch_latest_version", lambda *_a, **_k: "9.9.9")
    monkeypatch.setattr(
        uc,
        "_plan",
        lambda ref="", **_kwargs: ("pip", ["python", "-m", "pip"], "pip fake"),
    )
    monkeypatch.setattr(
        uc,
        "setup_research_pptx_runtime",
        lambda _paths: pytest.fail("updated runtime setup must not use the old imported implementation"),
        raising=False,
    )
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(uc.subprocess, "run", fake_run)

    res = runner.invoke(app, ["update", "--yes", "--no-restart-serve"])

    assert res.exit_code == 0, res.output
    assert calls == [
        ["python", "-m", "pip"],
        [
            uc.sys.executable,
            "-m",
            "omni.cli.main",
            "skills",
            "setup",
                "all",
        ],
    ]


def test_update_force_reinstalls_when_latest(monkeypatch):
    import omni.cli.commands.update_cmd as uc

    _pin_published_install(monkeypatch, uc)
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda *_a, **_k: __version__)
    monkeypatch.setattr(
        uc,
        "_plan",
        lambda ref="", **_kwargs: ("pip", ["python", "-m", "pip"], "pip fake"),
    )

    def _boom(*_a, **_k):  # pragma: no cover
        raise AssertionError("--check must not run a subprocess")

    monkeypatch.setattr(uc.subprocess, "run", _boom)
    # --force + --check: proceeds past the "already latest" short-circuit, then
    # stops at --check without running anything.
    res = runner.invoke(app, ["update", "--force", "--check"])
    assert res.exit_code == 0
    assert "--force" in res.output


def test_update_force_builds_a_real_published_reinstall(monkeypatch):
    """Compatibility ``--force`` must change the package-manager command too."""
    import omni.cli.commands.update_cmd as uc

    monkeypatch.setattr(uc, "_source_checkout", lambda _dist=None: None)
    monkeypatch.setattr(uc, "_installed_source_spec", lambda _dist: uc.DIST)
    monkeypatch.setattr(uc.shutil, "which", lambda _n: "/usr/bin/uv")
    monkeypatch.setattr(uc, "installation_method_for_prefix", lambda _prefix: "env")

    _kind, argv, _label = uc._plan(force_reinstall=True)

    assert "--reinstall-package" in argv
    assert argv[argv.index("--reinstall-package") + 1] == uc.DIST


def test_update_check_is_read_only_when_published_package_is_current(monkeypatch):
    """The preview path must not repair runtimes or write convergence state."""
    import omni.cli.commands.update_cmd as uc

    _pin_published_install(monkeypatch, uc)
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda *_a, **_k: __version__)
    monkeypatch.setattr(
        uc,
        "_prepare_bundled_skill_runtimes",
        lambda _paths: pytest.fail("--check must not prepare runtimes"),
    )

    res = runner.invoke(app, ["update", "--check"])

    assert res.exit_code == 0, res.output
    assert "no changes were made" in res.output


def test_update_status_is_read_only(monkeypatch):
    import omni.cli.commands.update_cmd as uc
    from omni.runtime import service_control, update_state

    fingerprint = update_state.InstallationFingerprint(
        token="installed",
        version="2.0.0",
        owner="uv",
        source="pypi",
        python="/tool/python",
    )
    monkeypatch.setattr(update_state, "current_fingerprint", lambda: fingerprint)
    monkeypatch.setattr(update_state, "read_state", lambda _paths: {})
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda _settings: "2.1.0")
    monkeypatch.setattr(
        uc,
        "_plan",
        lambda **_kwargs: ("uv", ["uv", "tool", "upgrade", uc.DIST], "uv tool owner"),
    )
    monkeypatch.setattr(
        service_control,
        "update_guard",
        lambda *_a, **_k: pytest.fail("status must not take the lifecycle lock"),
    )

    res = runner.invoke(app, ["update", "status"])

    assert res.exit_code == 0, res.output
    assert "2.0.0" in res.output
    assert "2.1.0" in res.output
    assert "Needs convergence" in res.output


def test_current_package_still_runs_convergence(monkeypatch):
    """External pipx/uv upgrades need convergence even with no package download."""
    import omni.cli.commands.update_cmd as uc
    from omni.runtime import service_control

    _pin_published_install(monkeypatch, uc)
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda *_a, **_k: __version__)
    monkeypatch.setattr(uc, "list_running_daemons", lambda _home: [])

    events: list[str] = []

    class _Guard:
        def __enter__(self):
            events.append("enter")
            return self

        def restore(self):
            events.append("restore")
            return ""

        def __exit__(self, *_args):
            events.append("exit")
            return False

    monkeypatch.setattr(service_control, "update_guard", lambda *_a, **_k: _Guard())
    monkeypatch.setattr(
        uc,
        "_prepare_bundled_skill_runtimes_with_updated_cli",
        lambda _paths: events.append("runtimes"),
    )

    def _no_package_install(*_a, **_k):
        raise AssertionError("an already-current package must not be downloaded again")

    monkeypatch.setattr(uc.subprocess, "run", _no_package_install)

    res = runner.invoke(app, ["update", "--yes"])

    assert res.exit_code == 0, res.output
    assert events == ["enter", "runtimes", "restore", "exit"]


def test_update_success_does_not_resync_exported_skills(monkeypatch):
    """Updating Omni itself must not mutate Claude/Codex/OpenClaw skill roots."""
    from pathlib import Path
    from types import SimpleNamespace

    import omni.cli.commands.update_cmd as uc
    import omni.skills_runtime.install as skill_install

    monkeypatch.setattr(update_check, "fetch_latest_version", lambda *_a, **_k: __version__)
    monkeypatch.setattr(
        uc,
        "_plan",
        lambda ref="", **_kwargs: ("pip", ["python", "-m", "pip"], "pip fake"),
    )
    monkeypatch.setattr(
        uc.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        skill_install,
        "resync_exported_skills",
        lambda *_a, **_k: calls.append("resync") or [],
    )

    res = runner.invoke(
        app,
        ["update", "--force", "--yes", "--no-restart-serve"],
    )

    assert res.exit_code == 0
    assert calls == []
    assert "exported skills" not in res.output.lower()
    home = Path.home()
    for root in (".claude", ".codex", ".agents", ".openclaw"):
        assert not (home / root / "skills").exists()


# ── source-checkout (git two-phase) update ───────────────────────────────────


@pytest.mark.parametrize(
    ("version", "is_source"),
    [
        ("2.0.0.dev0", True),
        ("2.0.0+local", True),
        ("0.0.0", True),
        ("not-a-version", True),
        ("", True),
        ("2.0.0", False),
        ("1.2.3", False),
        ("2.0.0rc1", False),  # a real pre-release is still a published, comparable version
    ],
)
def test_is_source_build_version_classifies_dev_and_release(version, is_source):
    assert update_check.is_source_build_version(version) is is_source


def test_plan_snapshot_source_checkout_is_git_non_editable(monkeypatch):
    """A non-editable ``file://`` install inside a repo is still a source checkout."""
    import omni.cli.commands.update_cmd as uc

    repo = uc.Path("/tmp/omni-snap")
    monkeypatch.setattr(uc, "_editable_source", lambda _dist: None)
    monkeypatch.setattr(uc, "_local_source", lambda _dist: repo / "cli")
    monkeypatch.setattr(uc, "_git_root", lambda _source: repo)

    kind, argv, label = uc._plan()
    assert kind == "git"
    assert argv == ["git", "-C", str(repo), "pull", "--ff-only"]
    assert "snapshot" in label
    # The seam both the planner and executor consult agrees it is non-editable.
    assert uc._source_checkout() == (repo, repo / "cli", False)


def test_plan_ref_targets_origin_branch(monkeypatch):
    import omni.cli.commands.update_cmd as uc

    repo = uc.Path("/tmp/omni-ref")
    monkeypatch.setattr(uc, "_editable_source", lambda _dist: repo / "cli")
    monkeypatch.setattr(uc, "_git_root", lambda _source: repo)

    kind, argv, label = uc._plan(ref="master")
    assert kind == "git"
    assert argv == ["git", "-C", str(repo), "pull", "--ff-only", "origin", "master"]
    assert "origin/master" in label


def test_plan_manual_fallback_for_unmanaged_source_build(monkeypatch):
    """A dev build that is neither a git checkout nor a published package → manual."""
    import omni.cli.commands.update_cmd as uc

    monkeypatch.setattr(uc, "_editable_source", lambda _dist: None)
    monkeypatch.setattr(uc, "_local_source", lambda _dist: None)
    monkeypatch.setattr(uc, "_installed_source_spec", lambda _dist: uc.DIST)
    monkeypatch.setattr(update_check, "is_source_build_version", lambda _v: True)

    kind, argv, _label = uc._plan()
    assert kind == "manual"
    assert argv == []

    def _boom(*_a, **_k):  # pragma: no cover - only fires on regression
        raise AssertionError("manual fallback must not run any install/subprocess")

    monkeypatch.setattr(uc.subprocess, "run", _boom)
    res = runner.invoke(app, ["update", "--yes", "--no-restart-serve"])
    assert res.exit_code == 1
    assert "can't upgrade it automatically" in res.output


def test_source_checkout_short_circuits_when_current(monkeypatch):
    import omni.cli.commands.update_cmd as uc

    repo = uc.Path("/repo")
    monkeypatch.setattr(uc, "_source_checkout", lambda *_a, **_k: (repo, repo / "cli", True))
    monkeypatch.setattr(uc, "_git_behind_count", lambda *_a, **_k: 0)  # not behind
    monkeypatch.setattr(uc, "_git_tree_is_dirty", lambda *_a, **_k: False)  # clean
    monkeypatch.setattr(uc, "setup_research_pptx_runtime", lambda _paths: None, raising=False)
    monkeypatch.setattr(
        uc,
        "_prepare_bundled_skill_runtimes_with_updated_cli",
        lambda _paths: None,
    )

    def _boom(*_a, **_k):  # pragma: no cover - only fires on regression
        raise AssertionError("a current, clean checkout must not pull or reinstall")

    monkeypatch.setattr(uc.subprocess, "run", _boom)
    res = runner.invoke(app, ["update", "--no-restart-serve"])
    assert res.exit_code == 0, res.output
    assert "Source checkout is current" in res.output
    assert "No Python package download is required" in res.output


def test_source_checkout_aborts_on_dirty_tree(monkeypatch):
    from types import SimpleNamespace

    import omni.cli.commands.update_cmd as uc

    repo = uc.Path("/repo")
    monkeypatch.setattr(uc, "_source_checkout", lambda *_a, **_k: (repo, repo / "cli", True))
    monkeypatch.setattr(uc, "_git_behind_count", lambda *_a, **_k: 1)  # behind, so it would update
    monkeypatch.setattr(uc, "_git_tree_is_dirty", lambda *_a, **_k: True)  # …but the tree is dirty
    calls: list[list[str]] = []
    monkeypatch.setattr(
        uc.subprocess, "run", lambda argv, **_k: calls.append(list(argv)) or SimpleNamespace(returncode=0)
    )

    res = runner.invoke(app, ["update", "--yes", "--no-restart-serve"])
    assert res.exit_code == 1
    assert "uncommitted changes" in res.output
    assert calls == []  # never touched git or the environment


def test_source_checkout_aborts_on_non_fast_forward(monkeypatch):
    from types import SimpleNamespace

    import omni.cli.commands.update_cmd as uc

    repo = uc.Path("/repo")
    monkeypatch.setattr(uc, "_source_checkout", lambda *_a, **_k: (repo, repo / "cli", True))
    monkeypatch.setattr(uc, "_git_behind_count", lambda *_a, **_k: 1)
    monkeypatch.setattr(uc, "_git_tree_is_dirty", lambda *_a, **_k: False)
    calls: list[list[str]] = []

    def fake_run(argv, **_k):  # noqa: ANN001, ANN202
        calls.append(list(argv))
        return SimpleNamespace(returncode=1)  # the pull cannot fast-forward

    monkeypatch.setattr(uc.subprocess, "run", fake_run)
    res = runner.invoke(app, ["update", "--yes", "--no-restart-serve"])
    assert res.exit_code != 0
    assert "diverged" in res.output
    assert calls == [["git", "-C", str(repo), "pull", "--ff-only"]]  # stopped after the failed pull


def test_git_pull_uses_repo_dir_so_update_is_cwd_independent():
    """`git -C <repo>` pins the recorded checkout, so cwd never matters."""
    import omni.cli.commands.update_cmd as uc

    repo = uc.Path("/somewhere/else/repo")
    argv = uc._git_pull_argv(repo)
    assert argv[:3] == ["git", "-C", str(repo)]


# ── git branch channel freshness (commit-based notifier) ─────────────────────


def _fake_direct_url(monkeypatch, payload: dict) -> None:
    """Pretend the running dist has this PEP 610 ``direct_url.json`` payload."""
    monkeypatch.setattr(update_check, "_installed_direct_url", lambda: payload)


def test_installed_branch_channel_only_for_moving_branch(monkeypatch):
    _fake_direct_url(monkeypatch, {"vcs_info": {"requested_revision": "master", "commit_id": "a" * 40}})
    assert update_check.installed_branch_channel() == "master"
    assert update_check.installed_commit_id() == "a" * 40
    # A commit/tag pin is reproducible — never a moving channel.
    _fake_direct_url(monkeypatch, {"vcs_info": {"requested_revision": "v2.0.0"}})
    assert update_check.installed_branch_channel() == ""
    _fake_direct_url(monkeypatch, {"vcs_info": {"requested_revision": "b" * 40}})
    assert update_check.installed_branch_channel() == ""
    # A non-VCS install (editable/PyPI) carries no vcs_info at all.
    _fake_direct_url(monkeypatch, {})
    assert update_check.installed_branch_channel() == ""
    assert update_check.installed_commit_id() == ""


def test_github_and_gitee_branch_api_url_derivation():
    raw = "https://gitee.com/example-org/example-repo/raw/master/cli/src/omni/__init__.py"
    assert (
        update_check._branch_api_url(raw, "master")
        == "https://gitee.com/api/v5/repos/example-org/example-repo/branches/master"
    )
    github_raw = (
        "https://raw.githubusercontent.com/omni-org/omniscientist/master/"
        "cli/src/omni/__init__.py"
    )
    assert update_check._branch_api_url(github_raw, "feature/release") == (
        "https://api.github.com/repos/omni-org/omniscientist/"
        "branches/feature%2Frelease"
    )
    assert update_check._branch_api_url("https://example.com/other", "master") is None
    assert update_check._branch_api_url("", "master") is None


def test_fetch_remote_commit_parses_sha(monkeypatch, settings):
    settings.update.raw_url = (
        "https://gitee.com/example-org/example-repo/raw/master/"
        "cli/src/omni/__init__.py"
    )

    def handler(url):
        assert "api/v5/repos/example-org/example-repo/branches/master" in url
        return _FakeResp(200, json_data={"name": "master", "commit": {"sha": "abc123"}})

    _install_fake_http(monkeypatch, handler)
    assert update_check.fetch_remote_commit(settings, "master") == "abc123"


def test_fetch_remote_commit_offline_returns_none(monkeypatch, settings):
    settings.update.raw_url = (
        "https://gitee.com/example-org/example-repo/raw/master/"
        "cli/src/omni/__init__.py"
    )

    def handler(_url):
        raise httpx.ConnectError("offline")

    _install_fake_http(monkeypatch, handler)
    assert update_check.fetch_remote_commit(settings, "master") is None


def test_pending_channel_notice_flags_new_commits(monkeypatch, settings):
    _fake_direct_url(monkeypatch, {"vcs_info": {"requested_revision": "master", "commit_id": "old"}})
    update_check.write_cache(settings.paths, {"remote_commit": "new"})
    assert update_check.pending_channel_notice(settings.paths, settings) == "master"
    # Same tip → nothing to do.
    update_check.write_cache(settings.paths, {"remote_commit": "old"})
    assert update_check.pending_channel_notice(settings.paths, settings) is None
    # skip_commit suppresses the hint until the tip moves again.
    update_check.write_cache(settings.paths, {"remote_commit": "new", "skip_commit": "new"})
    assert update_check.pending_channel_notice(settings.paths, settings) is None


def test_pending_channel_notice_none_for_non_channel(monkeypatch, settings):
    _fake_direct_url(monkeypatch, {})  # editable/PyPI/dev — not a branch channel
    update_check.write_cache(settings.paths, {"remote_commit": "new"})
    assert update_check.pending_channel_notice(settings.paths, settings) is None


def test_pending_channel_notice_silent_when_disabled(monkeypatch, settings):
    _fake_direct_url(monkeypatch, {"vcs_info": {"requested_revision": "master", "commit_id": "old"}})
    update_check.write_cache(settings.paths, {"remote_commit": "new"})
    monkeypatch.setenv("OMNI_UPDATE_CHECK", "0")
    assert update_check.pending_channel_notice(settings.paths, settings) is None


def test_refresh_cache_records_remote_commit_for_branch_channel(monkeypatch, settings):
    _fake_direct_url(monkeypatch, {"vcs_info": {"requested_revision": "master", "commit_id": "old"}})
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda *_a, **_k: "2.0.0.dev0")
    monkeypatch.setattr(update_check, "fetch_remote_commit", lambda *_a, **_k: "newsha")
    update_check.refresh_cache(settings)
    cache = update_check.read_cache(settings.paths)
    assert cache["remote_commit"] == "newsha"
    assert cache["branch"] == "master"


def test_refresh_cache_skips_remote_commit_off_channel(monkeypatch, settings):
    _fake_direct_url(monkeypatch, {})  # not a branch channel → no extra network
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda *_a, **_k: "9.9.9")
    monkeypatch.setattr(
        update_check,
        "fetch_remote_commit",
        lambda *_a, **_k: pytest.fail("must not fetch a remote commit off-channel"),
    )
    update_check.refresh_cache(settings)
    assert update_check.read_cache(settings.paths)["latest"] == "9.9.9"
