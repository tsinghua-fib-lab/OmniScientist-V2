"""BUG-02: compute sandbox delivers files to the artifact system.

A named-project (``-P``) turn used to have three disconnected envelopes:

* ``write_file`` could write the launch directory and register the file;
* the OS sandbox's write roots omitted that directory, so ``bash`` saw EROFS /
  ``Operation not permitted``;
* ``/tmp`` was a fresh bwrap tmpfs (Linux) and invisible to ``read_file``, so
  nothing reached the artifact inventory.

Codex ``WorkspaceWrite`` is cwd + configured roots + a host temp. Omni keeps
the ArtifactStore: the same permission model, minus whole ``/tmp``, plus a
host-owned ``$OMNI_OUTPUT_DIR`` that the host promotes with ``put_file``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.config import load_settings
from omni.config.paths import OmniPaths
from omni.core.tool_result import tool_observation
from omni.runtime.remaining import remaining_deliverables
from omni.skills_runtime import sandbox
from omni.skills_runtime.builtin_tools.fs import build_fs_tools
from omni.skills_runtime.builtin_tools.shell import build_shell_tools
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.exec_io import (
    OMNI_OUTPUT_ENV,
    durable_output_dir,
    exec_tmp_dir,
    kernel_write_roots,
)
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import TaskORM


def _obs(value: object) -> str:
    return str(tool_observation(value))


def _promoted_as(rows: list[object], stem: str, suffix: str) -> bool:
    """True when the store has this outbox file (title + suffix; slug may differ)."""
    want = suffix if suffix.startswith(".") else f".{suffix}"
    return any(
        str(getattr(row, "title", "") or "") == stem
        and Path(str(getattr(row, "rel_path", "") or "")).suffix.lower() == want
        for row in rows
    )


async def _named_project_ctx(
    tmp_path: Path,
    *,
    task_id: str,
    os_sandbox: str = "off",
) -> ExecContext:
    home = tmp_path / "omni-home"
    project_dir = home / "projects" / "demo"
    work = tmp_path / "experiment"
    work.mkdir(parents=True)
    settings = load_settings()
    settings.security.bash_sandbox = "workspace-write"
    settings.security.os_sandbox = os_sandbox
    settings.security.require_approval = False
    settings.security.approval_policy = "never"
    paths = OmniPaths(
        home=home,
        project_name="demo",
        project_dir=project_dir,
        workspace_root=None,
        invocation_cwd=work,
    )
    paths.ensure_dirs()
    settings.paths = paths
    db = get_database(paths.project_db)
    await db.init()
    async with db.session() as session:
        session.add(TaskORM(id=task_id, status="running", kind="turn", title="handoff"))
        await session.commit()
    return ExecContext(
        settings=settings,
        paths=paths,
        project="demo",
        session_id="sess-handoff",
        channel="cli",
        task_id=task_id,
        working_dir=work,
        db=db,
        artifacts=ArtifactStore(paths, db),
    )


def test_kernel_write_roots_include_named_project_working_dir(tmp_path: Path) -> None:
    settings = load_settings()
    work = tmp_path / "launch"
    work.mkdir()
    paths = OmniPaths(
        home=tmp_path / "home",
        project_name="demo",
        project_dir=tmp_path / "home" / "projects" / "demo",
        workspace_root=None,
        invocation_cwd=work,
    )
    paths.ensure_dirs()
    settings.paths = paths
    ctx = ExecContext(settings=settings, paths=paths, working_dir=work, channel="cli")
    # The launch dir must be a write root; the store and whole /tmp must not.
    roots = {Path(root).resolve() for root in kernel_write_roots(ctx)}
    assert work.resolve() in roots
    assert durable_output_dir(ctx) in roots
    assert exec_tmp_dir(ctx) in roots
    assert paths.home.resolve() not in roots
    assert paths.project_dir.resolve() not in roots
    fallback = {Path(p).resolve() for p in sandbox._write_roots(paths)}
    assert work.resolve() in fallback
    assert paths.home.resolve() not in fallback
    assert paths.project_dir.resolve() not in fallback


def test_bwrap_binds_persistent_tmp_not_tmpfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "resolve_sandbox", lambda _s: "bwrap")
    settings = load_settings()
    settings.paths.ensure_dirs()
    persist = tmp_path / "exec-tmp"
    persist.mkdir()
    prefix = sandbox.sandbox_prefix(
        settings.security,
        settings.paths,
        writable_roots=[str(tmp_path), "/tmp"],
        persist_tmp=persist,
    )
    assert prefix[0] == "bwrap"
    assert "--tmpfs" not in prefix
    assert str(persist.resolve()) in prefix
    bind_tmp = None
    for index, token in enumerate(prefix):
        if token == "--bind" and index + 2 < len(prefix) and prefix[index + 2] == "/tmp":
            bind_tmp = prefix[index + 1]
            break
    under_tmp = sandbox._under_tmp_mount(tmp_path)
    if under_tmp:
        # Overlaying guest /tmp would hide other write roots under the same mount.
        assert bind_tmp is None
    else:
        assert bind_tmp == str(persist.resolve())


@pytest.mark.asyncio
async def test_bash_output_dir_persists_registers_and_is_readable(tmp_path: Path) -> None:
    ctx = await _named_project_ctx(tmp_path, task_id="a" * 32, os_sandbox="off")
    bash = build_shell_tools(ctx)[0].handler
    tools = {tool.spec.name: tool for tool in build_fs_tools(ctx)}
    output = durable_output_dir(ctx)
    scratch = exec_tmp_dir(ctx)

    spec = build_shell_tools(ctx)[0].spec.description
    assert OMNI_OUTPUT_ENV in spec
    assert str(output) in spec

    from omni.skills_runtime.builtin_tools.shell import posix_shell_executable

    if posix_shell_executable() is None:
        pytest.skip("bash tool needs a POSIX shell for $OMNI_OUTPUT_DIR expansion")
    first = await bash({
        "command": (
            'printf "x,1\\n" > "$OMNI_OUTPUT_DIR/results.csv" && '
            'printf "<svg/>" > "$OMNI_OUTPUT_DIR/plot.svg" && '
            'printf "scratch\\n" > "$TMPDIR/note.txt" && '
            'echo WROTE'
        )
    })
    assert "WROTE" in _obs(first)
    assert (output / "results.csv").read_text() == "x,1\n"
    assert (scratch / "note.txt").read_text() == "scratch\n"

    second = await bash({
        "command": (
            'if [ -f "$OMNI_OUTPUT_DIR/results.csv" ] && [ -f "$TMPDIR/note.txt" ]; '
            'then echo SURVIVED; else echo GONE; fi'
        )
    })
    assert "SURVIVED" in _obs(second)

    read_csv = await tools["read_file"].handler({"path": str(output / "results.csv")})
    assert "x,1" in _obs(read_csv)
    read_scratch = await tools["read_file"].handler({"path": str(scratch / "note.txt")})
    assert "scratch" in _obs(read_scratch)
    read_host_tmp = await tools["read_file"].handler({"path": "/tmp/omni-not-a-deliverable.csv"})
    assert "denied" in _obs(read_host_tmp).lower()
    assert OMNI_OUTPUT_ENV in _obs(read_host_tmp)

    rows = await ctx.artifacts.list_by_task(ctx.task_id)
    names = {Path(row.rel_path).name for row in rows if row.rel_path}
    assert "results.csv" in names
    assert "plot.svg" in names
    assert remaining_deliverables(["artifact.figure"], rows) == []
    promoted = [row for row in rows if Path(row.rel_path).name in {"results.csv", "plot.svg"}]
    for row in promoted:
        resolved = await ctx.artifacts.resolve_path(row.uri)
        assert resolved is not None and resolved.is_file()
    import shutil

    shutil.rmtree(output)
    for row in promoted:
        resolved = await ctx.artifacts.resolve_path(row.uri)
        assert resolved is not None and resolved.is_file()
        assert output.resolve() not in resolved.parents


@pytest.mark.asyncio
async def test_bash_output_dir_does_not_harvest_a_venv(tmp_path: Path) -> None:
    ctx = await _named_project_ctx(tmp_path, task_id="d" * 32, os_sandbox="off")
    bash = build_shell_tools(ctx)[0].handler
    from omni.skills_runtime.builtin_tools.shell import posix_shell_executable

    if posix_shell_executable() is None:
        pytest.skip("bash tool needs a POSIX shell for $OMNI_OUTPUT_DIR expansion")
    result = await bash(
        {
            "command": (
                'mkdir -p "$OMNI_OUTPUT_DIR/.venv/lib/site-packages/pkg" && '
                'printf "MIT\\n" > "$OMNI_OUTPUT_DIR/LICENSE" && '
                'printf "x=1\\n" > "$OMNI_OUTPUT_DIR/.venv/lib/site-packages/pkg/mod.py" && '
                'printf "a,1\\n" > "$OMNI_OUTPUT_DIR/results.csv" && '
                'echo WROTE'
            )
        }
    )
    assert "WROTE" in _obs(result)
    rows = await ctx.artifacts.list_by_task(ctx.task_id)
    names = {Path(row.rel_path).name for row in rows if row.rel_path}
    assert names == {"results.csv"}


@pytest.mark.asyncio
async def test_im_bash_stays_blocked(tmp_path: Path) -> None:
    ctx = await _named_project_ctx(tmp_path, task_id="b" * 32, os_sandbox="off")
    ctx.channel = "wechat"
    bash = build_shell_tools(ctx)[0].handler
    result = await bash({"command": 'echo pwned > "$OMNI_OUTPUT_DIR/x.csv"'})
    assert "require local confirmation" in _obs(result)
    assert not (durable_output_dir(ctx) / "x.csv").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(sandbox.detect_sandbox() == "", reason="no OS sandbox on this host")
async def test_os_sandbox_allows_working_dir_and_output_dir(tmp_path: Path) -> None:
    ctx = await _named_project_ctx(tmp_path, task_id="c" * 32, os_sandbox="auto")
    bash = build_shell_tools(ctx)[0].handler
    work = ctx.working_dir
    assert work is not None

    cwd_write = await bash({"command": "echo csv,1 > results.csv && echo WROTE"})
    assert "WROTE" in _obs(cwd_write)
    assert (work / "results.csv").read_text() == "csv,1\n"

    handed = await bash({
        "command": (
            'printf "<svg id=\\"ok\\"/>" > "$OMNI_OUTPUT_DIR/from-bash.svg" && echo HANDED'
        )
    })
    assert "HANDED" in _obs(handed)
    rows = await ctx.artifacts.list_by_task(ctx.task_id)
    assert any(str(row.rel_path).endswith("from-bash.svg") for row in rows)

    outside = tmp_path / "escape-outside-roots.txt"
    denied = await bash({"command": f'echo pwned > "{outside}" && echo ESCAPED'})
    assert "ESCAPED" not in _obs(denied)
    assert not outside.exists()
