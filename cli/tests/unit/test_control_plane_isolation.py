"""Control plane, scratch, and purge must not share one writable envelope.

A review task once ran this suite with ``TMPDIR`` under the real store. Pytest
then remapped ``HOME`` / ``OMNI_HOME`` into that tree, ``find_project_root``
treated the outer ``.omni`` as an in-place project, and ``uninstall --purge``
deleted the owner's control state. These tests rebuild that envelope under
``tmp_path`` and assert it cannot happen again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.config.paths import (
    find_project_root,
    get_paths,
    is_control_store_path,
    is_within_home,
    looks_like_control_store,
    os_user_home,
)
from omni.runtime import uninstall
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.exec_io import (
    durable_output_dir,
    exec_tmp_dir,
    kernel_write_roots,
)
from omni.skills_runtime.sandbox import SandboxUnavailableError, sandbox_prefix


def _outer_store(root: Path) -> Path:
    """A directory that looks like a real user store, not an in-place marker."""
    store = root / "Users" / "owner" / ".omni"
    store.mkdir(parents=True)
    (store / "config.toml").write_text("[model]\nprovider = 'mock'\n", encoding="utf-8")
    (store / "control.sqlite3").write_bytes(b"")
    (store / "workspaces").mkdir()
    (store / "projects").mkdir()
    return store


def _wipe_envelope(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Outer store + nested pytest-shaped home, the layout that wiped ``~/.omni``.

    ``isolated_home`` already planted ``tmp_path/.omni``. That directory *is*
    the outer store — the same way the real ``~/.omni`` contained pytest's
    ``tmp_path`` when Omni had set ``TMPDIR`` to ``<store>/tmp/exec``.
    """
    outer = tmp_path / ".omni"
    outer.mkdir(parents=True, exist_ok=True)
    (outer / "config.toml").write_text("[model]\nprovider = 'mock'\n", encoding="utf-8")
    (outer / "control.sqlite3").write_bytes(b"")
    (outer / "workspaces").mkdir(exist_ok=True)
    (outer / "projects").mkdir(exist_ok=True)
    nested = outer / "workspaces" / "work-deadbeef" / "tmp" / "exec" / "pytest" / "p0"
    test_store = nested / ".omni"
    test_home = nested / "home"
    workspace = nested / "workspace"
    test_store.mkdir(parents=True)
    test_home.mkdir()
    workspace.mkdir()
    return outer.resolve(), test_store, workspace


def test_nested_pytest_tmp_does_not_adopt_the_outer_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer, test_store, workspace = _wipe_envelope(tmp_path)
    monkeypatch.setenv("HOME", str(workspace.parent / "home"))
    monkeypatch.setenv("OMNI_HOME", str(test_store))

    assert find_project_root(workspace) is None
    paths = get_paths(cwd=workspace)
    assert paths.home == test_store.resolve()
    assert paths.project_dir.resolve() != outer.resolve()
    assert is_within_home(paths.project_dir, test_store)
    assert not is_within_home(paths.project_dir, outer) or paths.project_dir != outer


def test_remapped_home_does_not_adopt_os_account_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cwd under the real account home is not an in-place project of ``~/.omni``.

    ``Path.home()`` follows a remapped ``HOME``. The walk must still stop at the
    host-fixed account home, or every directory under ``/Users/<name>`` becomes
    an adoption of the real store.
    """
    account = tmp_path / "Users" / "owner"
    store = account / ".omni"
    store.mkdir(parents=True)
    (store / "config.toml").write_text("[model]\nprovider = 'mock'\n", encoding="utf-8")
    (store / "workspaces").mkdir()
    work = account / "work"
    work.mkdir()
    fake_home = tmp_path / "pytest-home"
    fake_store = tmp_path / "pytest-omni"
    fake_home.mkdir()
    fake_store.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("OMNI_HOME", str(fake_store))
    monkeypatch.setattr("omni.config.paths.os_user_home", lambda: account.resolve())

    assert find_project_root(work) is None
    paths = get_paths(cwd=work)
    assert paths.project_dir.resolve() != store.resolve()
    assert is_within_home(paths.project_dir, fake_store)


def test_a_real_in_place_marker_is_still_found(tmp_path: Path) -> None:
    repo = tmp_path / "code" / "myrepo"
    (repo / ".omni").mkdir(parents=True)
    assert find_project_root(repo / "src") == repo.resolve()


def test_looks_like_control_store_requires_store_fingerprints(tmp_path: Path) -> None:
    marker = tmp_path / "repo" / ".omni"
    marker.mkdir(parents=True)
    (marker / "sessions.sqlite3").write_bytes(b"")
    assert not looks_like_control_store(marker)

    store = _outer_store(tmp_path / "machine")
    assert looks_like_control_store(store)
    assert is_control_store_path(store, store)


def test_exec_io_lives_outside_the_control_store(tmp_path: Path) -> None:
    from omni.config import load_settings
    from omni.config.paths import OmniPaths

    home = tmp_path / ".omni"
    project = home / "workspaces" / "demo-abcd1234"
    work = tmp_path / "src"
    work.mkdir(parents=True)
    paths = OmniPaths(
        home=home,
        project_name="demo",
        project_dir=project,
        workspace_root=work,
        invocation_cwd=work,
    )
    paths.ensure_dirs()
    settings = load_settings()
    settings.paths = paths
    ctx = ExecContext(settings=settings, paths=paths, working_dir=work, task_id="a" * 32)

    scratch = exec_tmp_dir(ctx)
    outbox = durable_output_dir(ctx)
    assert not is_within_home(scratch, home)
    assert not is_within_home(outbox, home)
    roots = {Path(root).resolve() for root in kernel_write_roots(ctx)}
    assert scratch in roots
    assert outbox in roots
    assert home.resolve() not in roots
    assert project.resolve() not in roots
    assert work.resolve() in roots


def test_kernel_write_roots_do_not_open_an_ancestor_of_the_store(tmp_path: Path) -> None:
    from omni.config import load_settings
    from omni.config.paths import OmniPaths

    # Linux CI parks pytest under /tmp; a /tmp write root would reopen the store.
    home = tmp_path / ".omni"
    paths = OmniPaths(home=home, project_name="demo", project_dir=home / "projects" / "demo")
    paths.ensure_dirs()
    settings = load_settings()
    settings.paths = paths
    ctx = ExecContext(settings=settings, paths=paths, channel="cli")
    store = home.resolve()
    for raw in kernel_write_roots(ctx):
        root = Path(raw).resolve()
        if root == store or store in root.parents or root in store.parents:
            pytest.fail(f"write root {root} opens control store {store}")


def test_safe_remove_in_place_refuses_a_user_store(tmp_path: Path) -> None:
    outer = _outer_store(tmp_path)
    with pytest.raises(ValueError, match="control-state"):
        uninstall._safe_remove_in_place(outer)
    assert outer.is_dir()
    assert (outer / "config.toml").is_file()


def test_registered_in_place_skips_the_active_or_outer_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer, test_store, workspace = _wipe_envelope(tmp_path)
    monkeypatch.setenv("HOME", str(workspace.parent / "home"))
    monkeypatch.setenv("OMNI_HOME", str(test_store))
    paths = get_paths(cwd=workspace)
    # Even if resolution leaked and pointed project_dir at the outer store,
    # the registered-in-place list must not offer that store for rmtree.
    leaked = paths.__class__(
        home=paths.home,
        project_name=outer.name,
        project_dir=outer,
        workspace_root=outer.parent,
        invocation_cwd=workspace,
    )
    assert outer not in uninstall._registered_in_place_projects(leaked)
    assert outer not in uninstall._registered_in_place_projects(paths)


def test_purge_plan_for_nested_resolution_does_not_target_outer_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer, test_store, workspace = _wipe_envelope(tmp_path)
    monkeypatch.setenv("HOME", str(workspace.parent / "home"))
    monkeypatch.setenv("OMNI_HOME", str(test_store))
    paths = get_paths(cwd=workspace)
    plan = uninstall.build_uninstall_plan(
        paths,
        purge=True,
        all_project_data=True,
        all_installations=False,
        remove_program=False,
        remove_untracked_exports=False,
    )
    targeted = {Path(action.target).resolve() for action in plan.actions if action.category in {"user-data", "project-data"}}
    assert outer.resolve() not in targeted
    assert test_store.resolve() in targeted or any(
        action.category == "user-data" and Path(action.target).resolve() == test_store.resolve()
        for action in plan.actions
    )


def test_auto_sandbox_runs_unconfined_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from omni.config import load_settings
    from omni.skills_runtime import sandbox

    monkeypatch.setattr(sandbox, "resolve_sandbox", lambda _s: "")
    settings = load_settings()
    settings.security.os_sandbox = "auto"
    assert sandbox_prefix(settings.security, settings.paths) == []


def test_explicit_backend_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from omni.skills_runtime import sandbox

    sandbox._sandbox_works.cache_clear()
    monkeypatch.setattr(sandbox, "_sandbox_works", lambda _name: False)
    with pytest.raises(SandboxUnavailableError, match="unavailable"):
        sandbox.resolve_sandbox("bwrap")


def test_explicit_off_still_runs_unconfined(monkeypatch: pytest.MonkeyPatch) -> None:
    from omni.config import load_settings
    from omni.skills_runtime import sandbox

    monkeypatch.setattr(sandbox, "resolve_sandbox", lambda _s: "")
    settings = load_settings()
    settings.security.os_sandbox = "off"
    assert sandbox_prefix(settings.security, settings.paths) == []


def test_default_sandbox_write_roots_omit_home_and_project_dir() -> None:
    from omni.config import load_settings
    from omni.skills_runtime import sandbox

    settings = load_settings()
    settings.paths.ensure_dirs()
    roots = {Path(raw).resolve() for raw in sandbox._write_roots(settings.paths)}
    assert settings.paths.home.resolve() not in roots
    assert settings.paths.project_dir.resolve() not in roots


def test_os_user_home_ignores_remapped_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "remapped"))
    if os_name_is_posix():
        assert os_user_home() != (tmp_path / "remapped").resolve()


def os_name_is_posix() -> bool:
    import os

    return os.name == "posix"


def test_confined_exec_prefix_is_the_shared_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    from omni.config import load_settings
    from omni.skills_runtime.exec_io import confined_exec_prefix

    seen: dict[str, object] = {}

    def fake_prefix(security, paths, **kwargs):  # noqa: ANN001
        seen["roots"] = kwargs.get("writable_roots")
        seen["tmp"] = kwargs.get("persist_tmp")
        return ["sandbox-exec", "-p", "(version 1)"]

    monkeypatch.setattr("omni.skills_runtime.sandbox.sandbox_prefix", fake_prefix)
    settings = load_settings()
    settings.paths.ensure_dirs()
    ctx = ExecContext(settings=settings, paths=settings.paths, working_dir=settings.paths.invocation_cwd)
    assert confined_exec_prefix(ctx)[0] == "sandbox-exec"
    roots = {Path(raw).resolve() for raw in seen["roots"]}  # type: ignore[arg-type]
    assert settings.paths.home.resolve() not in roots
    assert Path(seen["tmp"]).resolve() == exec_tmp_dir(ctx)  # type: ignore[arg-type]


def test_os_sandbox_prefix_does_not_swallow_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from omni.config import load_settings

    settings = load_settings()
    settings.security.os_sandbox = "auto"
    monkeypatch.setattr(
        "omni.skills_runtime.sandbox.sandbox_prefix",
        lambda *a, **k: (_ for _ in ()).throw(SandboxUnavailableError("no backend")),
    )
    ctx = ExecContext(settings=settings, paths=settings.paths)
    with pytest.raises(SandboxUnavailableError):
        ctx.os_sandbox_prefix()


def test_input_write_roots_skip_a_parent_that_contains_the_store(tmp_path: Path) -> None:
    from omni.config import load_settings
    from omni.config.paths import user_home
    from omni.skills_runtime.exec_io import input_write_roots, kernel_write_roots

    settings = load_settings()
    settings.paths.ensure_dirs()
    ctx = ExecContext(settings=settings, paths=settings.paths, working_dir=settings.paths.invocation_cwd)
    beside_store = user_home().parent / "next-to-store.txt"
    safe = tmp_path / "io" / "count.txt"
    safe.parent.mkdir(parents=True)
    extra = input_write_roots({"counter": str(safe), "also": str(beside_store)})
    roots = {Path(raw).resolve() for raw in kernel_write_roots(ctx, extra)}
    assert safe.parent.resolve() in roots
    assert user_home().parent.resolve() not in roots
    assert user_home().resolve() not in roots


def test_scratch_is_per_task_and_private(tmp_path: Path) -> None:
    import stat

    from omni.config import load_settings
    from omni.config.paths import OmniPaths

    home = tmp_path / ".omni"
    work = tmp_path / "src"
    work.mkdir()
    paths = OmniPaths(
        home=home, project_name="demo", project_dir=home / "projects" / "demo",
        workspace_root=work, invocation_cwd=work,
    )
    paths.ensure_dirs()
    settings = load_settings()
    settings.paths = paths
    ctx_a = ExecContext(settings=settings, paths=paths, working_dir=work, task_id="a" * 32)
    ctx_b = ExecContext(settings=settings, paths=paths, working_dir=work, task_id="b" * 32)
    scratch_a = exec_tmp_dir(ctx_a)
    scratch_b = exec_tmp_dir(ctx_b)
    assert scratch_a != scratch_b
    assert scratch_a.is_dir() and scratch_b.is_dir()
    if os_name_is_posix():
        assert stat.S_IMODE(scratch_a.stat().st_mode) == 0o700


def test_kernel_write_roots_omit_slash_tmp(tmp_path: Path) -> None:
    from omni.config import load_settings
    from omni.config.paths import OmniPaths

    home = tmp_path / ".omni"
    work = tmp_path / "src"
    work.mkdir()
    paths = OmniPaths(
        home=home, project_name="demo", project_dir=home / "projects" / "demo",
        workspace_root=work, invocation_cwd=work,
    )
    paths.ensure_dirs()
    settings = load_settings()
    settings.paths = paths
    ctx = ExecContext(settings=settings, paths=paths, working_dir=work, task_id="c" * 32)
    roots = {Path(raw).resolve() for raw in kernel_write_roots(ctx)}
    assert Path("/tmp").resolve() not in roots
    assert Path("/private/tmp").resolve() not in roots


def test_git_write_adds_dot_git_but_not_the_store(tmp_path: Path) -> None:
    from omni.config import load_settings
    from omni.config.paths import OmniPaths
    from omni.skills_runtime.builtin_tools.shell import (
        command_writes_git_metadata,
        git_metadata_write_roots,
    )

    assert command_writes_git_metadata("git add .")
    assert not command_writes_git_metadata("git status")
    home = tmp_path / ".omni"
    work = tmp_path / "repo"
    work.mkdir()
    paths = OmniPaths(
        home=home, project_name="demo", project_dir=home / "projects" / "demo",
        workspace_root=work, invocation_cwd=work,
    )
    paths.ensure_dirs()
    settings = load_settings()
    settings.paths = paths
    ctx = ExecContext(settings=settings, paths=paths, working_dir=work, task_id="d" * 32)
    extra = git_metadata_write_roots("git commit -m x", work)
    roots = {Path(raw).resolve() for raw in kernel_write_roots(ctx, extra)}
    assert (work / ".git").resolve() in roots
    assert home.resolve() not in roots


def test_persist_tmp_symlink_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omni.config import load_settings
    from omni.skills_runtime import sandbox

    monkeypatch.setattr(sandbox, "resolve_sandbox", lambda _s: "sandbox-exec")
    settings = load_settings()
    settings.paths.ensure_dirs()
    link = tmp_path / "exec"
    link.symlink_to(settings.paths.home)
    with pytest.raises(SandboxUnavailableError, match="symlink"):
        sandbox_prefix(settings.security, settings.paths, persist_tmp=link)


def test_frozen_control_stores_include_active_and_pointer() -> None:
    import os

    from omni.config.paths import frozen_control_stores, home_selection_file, user_home

    stores = {path.resolve() for path in frozen_control_stores()}
    assert user_home().resolve() in stores
    assert Path(os.environ["OMNI_HOME"]).resolve() in stores
    assert home_selection_file().resolve() in stores


def test_quarantine_rename_failure_does_not_rmtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "research-project" / ".omni"
    target.mkdir(parents=True)
    marker = target / "sessions.sqlite3"
    marker.write_text("keep", encoding="utf-8")

    def boom(_self: Path, _dest: Path) -> None:
        raise OSError("cross-device")

    monkeypatch.setattr(Path, "rename", boom)
    with pytest.raises(ValueError, match="quarantine"):
        uninstall._quarantine_remove(target)
    assert marker.is_file()


def test_purge_aborts_when_service_stop_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = get_paths(cwd=workspace)
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text("[model]\nprovider = 'mock'\n", encoding="utf-8")
    monkeypatch.setattr(uninstall, "_teardown_home_service", lambda *_a, **_k: False)
    monkeypatch.setattr(uninstall, "scan_running_serve_pids", lambda **_k: [])
    plan = uninstall.build_uninstall_plan(
        paths,
        purge=True,
        all_project_data=False,
        all_installations=False,
        remove_program=False,
        remove_untracked_exports=False,
    )
    report = uninstall.execute_uninstall_plan(paths, plan)
    assert paths.config_file.is_file()
    assert any("purge aborted" in error for error in report.errors)


def test_purge_refuses_when_home_identity_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = get_paths(cwd=workspace)
    paths.home.mkdir(parents=True, exist_ok=True)
    (paths.home / "config.toml").write_text("[model]\nprovider = 'mock'\n", encoding="utf-8")
    plan = uninstall.build_uninstall_plan(
        paths,
        purge=True,
        all_project_data=False,
        all_installations=False,
        remove_program=False,
        remove_untracked_exports=False,
    )
    original = tmp_path / "original-home"
    paths.home.rename(original)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "config.toml").write_text("stolen", encoding="utf-8")
    replacement.rename(paths.home)
    with pytest.raises(ValueError, match="identity changed"):
        uninstall._safe_remove_home(plan.home, expected_identity=plan.home_identity)
    assert (paths.home / "config.toml").read_text(encoding="utf-8") == "stolen"


def test_git_root_inside_store_is_dropped(tmp_path: Path) -> None:
    from omni.config import load_settings
    from omni.config.paths import OmniPaths
    from omni.skills_runtime.builtin_tools.shell import git_metadata_write_roots

    home = tmp_path / ".omni"
    work = home / "projects" / "demo"
    work.mkdir(parents=True)
    paths = OmniPaths(
        home=home, project_name="demo", project_dir=work,
        workspace_root=work, invocation_cwd=work,
    )
    paths.ensure_dirs()
    settings = load_settings()
    settings.paths = paths
    ctx = ExecContext(settings=settings, paths=paths, working_dir=work, task_id="e" * 32)
    extra = git_metadata_write_roots("git add .", work)
    roots = {Path(raw).resolve() for raw in kernel_write_roots(ctx, extra)}
    assert (work / ".git").resolve() not in roots
    assert home.resolve() not in roots


def test_ensure_private_dir_refuses_store_and_symlink_leaf(tmp_path: Path) -> None:
    from omni.config.paths import user_home
    from omni.skills_runtime.exec_io import ensure_private_dir, host_scratch_base

    with pytest.raises(RuntimeError, match="control state"):
        ensure_private_dir(user_home() / "tmp" / "exec")
    link = tmp_path / "exec"
    link.symlink_to(tmp_path / "missing-target")
    with pytest.raises(RuntimeError, match="symlink"):
        ensure_private_dir(link)
    base = host_scratch_base(user_home())
    assert base.resolve() not in {Path("/tmp").resolve(), Path("/private/tmp").resolve()}
    assert "omni-exec" in base.parts


def test_bwrap_overlays_tmp_when_scratch_is_outside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omni.config import load_settings
    from omni.skills_runtime import sandbox

    monkeypatch.setattr(sandbox, "resolve_sandbox", lambda _s: "bwrap")
    settings = load_settings()
    persist = tmp_path / "cache" / "omni-exec" / "task" / "exec"
    persist.mkdir(parents=True)
    work = tmp_path / "cache" / "work"
    work.mkdir()
    prefix = sandbox.sandbox_prefix(
        settings.security,
        settings.paths,
        writable_roots=[str(work)],
        persist_tmp=persist,
    )
    bind_tmp = None
    for index, token in enumerate(prefix):
        if token == "--bind" and index + 2 < len(prefix) and prefix[index + 2] == "/tmp":
            bind_tmp = prefix[index + 1]
            break
    if sandbox._under_tmp_mount(work) or sandbox._under_tmp_mount(persist):
        assert bind_tmp is None
    else:
        assert bind_tmp == str(persist.resolve())


def test_seatbelt_git_grant_is_not_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from omni.config import load_settings
    from omni.skills_runtime import sandbox

    monkeypatch.setattr(sandbox, "resolve_sandbox", lambda _s: "sandbox-exec")
    settings = load_settings()
    work = tmp_path / "repo"
    work.mkdir()
    git_dir = work / ".git"
    git_dir.mkdir()
    prefix = sandbox.sandbox_prefix(
        settings.security,
        settings.paths,
        writable_roots=[str(work), str(git_dir)],
    )
    profile = prefix[2]
    granted = sandbox._seatbelt_literal(str(git_dir.resolve()))
    assert f'(subpath "{granted}")' in profile
    # The grant must not be cancelled by a later deny of the same path.
    assert f'(deny file-write* (subpath "{granted}"))' not in profile
