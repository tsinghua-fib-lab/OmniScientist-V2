"""Builtin tool hardening: fs actionable observations + docs self-knowledge.

Covers Layer B (list_dir, dir-aware read_file, sensitive denylist) and Layer C′
(docs_search / docs_read scoped to omni's own docs, env/source invisible).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omni.config import load_settings
from omni.config.paths import OmniPaths, get_paths
from omni.core.observation import compact_observation, observation_spill_path
from omni.core.tool_result import (
    attach_tool_outcome,
    fs_result_outcome,
    tool_call_outcome,
    tool_result_failure,
)
from omni.skills_runtime.builtin_tools.docs import build_docs_tools
from omni.skills_runtime.builtin_tools.fs import build_fs_tools
from omni.skills_runtime.context import ExecContext


def _tool(tools, name):
    return next(t for t in tools if t.spec.name == name).handler


def _ctx():
    settings = load_settings()
    paths = get_paths(project="fstest")
    paths.project_dir.mkdir(parents=True, exist_ok=True)
    return ExecContext(settings=settings, paths=paths)


# ── Layer B: filesystem tools ──


@pytest.mark.asyncio
async def test_list_dir_lists_and_hides_sensitive_files():
    ctx = _ctx()
    proj = ctx.paths.project_dir
    (proj / "sub").mkdir(exist_ok=True)
    (proj / "notes.md").write_text("content", encoding="utf-8")
    (proj / ".env").write_text("SECRET=1", encoding="utf-8")
    (proj / "secrets.toml").write_text("[k]\napi='x'", encoding="utf-8")

    tools = build_fs_tools(ctx)
    out = await _tool(tools, "list_dir")({"path": str(proj)})

    assert "sub/" in out
    assert "notes.md" in out
    assert ".env" not in out
    assert "secrets.toml" not in out


@pytest.mark.asyncio
async def test_read_file_on_directory_returns_listing_not_error():
    ctx = _ctx()
    proj = ctx.paths.project_dir
    (proj / "pkg").mkdir(exist_ok=True)
    (proj / "pkg" / "a.txt").write_text("hello", encoding="utf-8")

    tools = build_fs_tools(ctx)
    out = await _tool(tools, "read_file")({"path": str(proj / "pkg")})

    assert "directory" in out
    assert "a.txt" in out
    assert not out.startswith("ERROR")


@pytest.mark.asyncio
async def test_read_file_accepts_ascii_quotes_for_curly_named_file():
    ctx = _ctx()
    proj = ctx.paths.project_dir
    real = proj / "报告“初稿”.md"
    real.write_text("body", encoding="utf-8")
    asked = proj / '报告"初稿".md'

    out = await _tool(build_fs_tools(ctx), "read_file")({"path": str(asked)})

    assert out == "body"


@pytest.mark.asyncio
async def test_read_file_missing_path_forbids_quote_retries():
    ctx = _ctx()
    missing = ctx.paths.project_dir / "nope.md"

    out = await _tool(build_fs_tools(ctx), "read_file")({"path": str(missing)})

    assert out.startswith("ERROR: path does not exist")
    assert "do not rewrite quotation marks" in out
    assert "Do not retry" in out


@pytest.mark.asyncio
async def test_read_file_resolves_artifact_uri():
    ctx = _ctx()
    target = ctx.paths.project_dir / "stored.md"
    target.write_text("from-store", encoding="utf-8")

    class _Store:
        async def resolve_path(self, uri: str):
            return target if uri == "artifact://abc123" else None

    ctx.artifacts = _Store()
    out = await _tool(build_fs_tools(ctx), "read_file")({"path": "artifact://abc123"})
    assert out == "from-store"

    missing = await _tool(build_fs_tools(ctx), "read_file")({"path": "artifact://missing"})
    assert missing.startswith("ERROR: artifact not found")


@pytest.mark.asyncio
async def test_read_file_denies_sensitive_and_actionable_root_error():
    ctx = _ctx()
    proj = ctx.paths.project_dir
    (proj / ".env").write_text("SECRET=1", encoding="utf-8")

    read = _tool(build_fs_tools(ctx), "read_file")
    hidden = await read({"path": str(proj / ".env")})
    assert "sensitive file hidden" in hidden
    assert "SECRET" not in hidden

    outside = await read({"path": "/etc/hosts"})
    assert outside.startswith("ERROR")
    assert "accessible roots" in outside


@pytest.mark.asyncio
async def test_grep_and_glob_skip_sensitive_files():
    ctx = _ctx()
    proj = ctx.paths.project_dir
    (proj / ".env").write_text("SECRET=topsecret", encoding="utf-8")
    (proj / "doc.md").write_text("visible content", encoding="utf-8")

    tools = build_fs_tools(ctx)
    grep_out = await _tool(tools, "grep")({"pattern": "topsecret", "path": str(proj)})
    # The secret's line must not surface (no hit referencing .env); grep reports
    # no matches because the only file containing it is skipped.
    assert ".env:" not in grep_out
    assert "SECRET=topsecret" not in grep_out
    assert "no matches" in grep_out

    # The sensitive file is filtered from glob results; ordinary files are not.
    glob_env = await _tool(tools, "glob")({"pattern": "*.env"})
    assert str(proj / ".env") not in glob_env
    glob_md = await _tool(tools, "glob")({"pattern": "*.md"})
    assert "doc.md" in glob_md


@pytest.mark.asyncio
async def test_fs_denies_symlink_to_sensitive_target_under_root(tmp_path):  # noqa: ANN001
    # TOCTOU hardening: a benign-named symlink whose *resolved* target is
    # sensitive must be denied on read/write/edit — checking only the link name
    # (``notes.txt``) would smuggle the secret past the name-glob guard.
    #
    # The link lives in an ordinary working directory rather than under the omni
    # store: inside the store the protected-directory check refuses first, and
    # the refusal this test is about would never be reached.
    settings = load_settings()
    paths = get_paths(project="fstest")
    paths.project_dir.mkdir(parents=True, exist_ok=True)
    proj = tmp_path / "repo"
    proj.mkdir(parents=True, exist_ok=True)
    ctx = ExecContext(settings=settings, paths=paths, working_dir=proj)
    secret = proj / ".env"
    secret.write_text("SECRET=1", encoding="utf-8")
    link = proj / "notes.txt"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(secret)

    tools = build_fs_tools(ctx)
    read = await _tool(tools, "read_file")({"path": str(link)})
    assert "sensitive" in read and "SECRET" not in read

    written = await _tool(tools, "write_file")({"path": str(link), "contents": "pwned"})
    assert written.startswith("ERROR") and "sensitive" in written
    assert secret.read_text(encoding="utf-8") == "SECRET=1"  # target untouched


@pytest.mark.asyncio
async def test_open_artifact_readmits_raw_path_fallback(tmp_path):
    # open_artifact's raw-path fallback must apply the read-root + sensitivity
    # gate so it cannot be used to read arbitrary/sensitive files off disk.
    from omni.skills_runtime.builtin_tools.recall import build_recall_tools
    from omni.storage.artifacts import ArtifactStore
    from omni.storage.db import get_database, reset_databases

    settings = load_settings()
    paths = get_paths(project="fstest-openart")
    paths.ensure_dirs()
    db = get_database(paths.project_db)
    await db.init()
    try:
        ctx = ExecContext(
            settings=settings, paths=paths, project=paths.project_name,
            session_id="sess-open", channel="cli", db=db,
            artifacts=ArtifactStore(paths, db), llm=None,
        )
        proj = paths.project_dir
        (proj / ".env").write_text("SECRET=1", encoding="utf-8")
        (proj / "ok.txt").write_text("plain artifact", encoding="utf-8")

        open_artifact = _tool(build_recall_tools(ctx), "open_artifact")

        # A real file outside the roots → refused on every host OS.
        outside_path = tmp_path / "outside.txt"
        outside_path.write_text("not accessible", encoding="utf-8")
        outside = await open_artifact({"uri": str(outside_path)})
        assert "outside the accessible roots" in outside.get("error", "")

        # Sensitive file under a root → refused.
        sensitive = await open_artifact({"uri": str(proj / ".env")})
        assert "sensitive" in sensitive.get("error", "")

        # An ordinary file under a root still opens.
        ok = await open_artifact({"uri": str(proj / "ok.txt")})
        assert ok.get("content") == "plain artifact"
    finally:
        await reset_databases()


# ── Layer C′: self-knowledge docs tools ──


@pytest.mark.asyncio
async def test_docs_search_finds_storage_architecture():
    tools = build_docs_tools(_ctx())
    res = await _tool(tools, "docs_search")({"query": "local storage architecture SQLite filesystem"})
    assert res["status"] == "ok"
    docs = {m["doc"] for m in res["matches"]}
    assert any("memory.md" in d for d in docs)


@pytest.mark.asyncio
async def test_docs_read_serves_bundled_doc():
    tools = build_docs_tools(_ctx())
    content = await _tool(tools, "docs_read")({"doc": "memory.md"})
    assert len(content) > 100
    assert not content.startswith("ERROR")


@pytest.mark.asyncio
async def test_docs_read_blocks_traversal_and_non_docs():
    read = _tool(build_docs_tools(_ctx()), "docs_read")

    for bad in ("../../secrets.toml", "../src/omni/config/settings.py", "/etc/hosts"):
        out = await read({"doc": bad})
        assert out.startswith("ERROR")
        assert "api" not in out.lower() or "Available documents" in out


@pytest.mark.asyncio
async def test_docs_read_without_name_lists_available_docs():
    out = await _tool(build_docs_tools(_ctx()), "docs_read")({})
    assert "memory.md" in out


@pytest.mark.asyncio
async def test_observation_spill_is_readable_and_jail_denial_is_blocked(tmp_path: Path) -> None:
    """Reproduce ef3b6546: spill pointer must be readable; jail denial is not success."""
    home = tmp_path / "home"
    project = home / "projects" / "walkthrough"
    project.mkdir(parents=True)
    ctx = ExecContext(
        settings=load_settings(),
        paths=OmniPaths(home=home, project_name="walkthrough", project_dir=project),
    )
    leftover = home / "cache" / "spillover" / "source_ids-deadbeef.txt"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("abc\n", encoding="utf-8")

    read = _tool(build_fs_tools(ctx), "read_file")
    assert await read({"path": str(leftover)}) == "abc\n"

    source_ids = [f"source-{index:04d}-{'x' * 80}" for index in range(80)]
    observation = compact_observation(
        {"status": "ok", "source_ids": source_ids, "count": 80},
        max_chars=1500,
        spill_dir=observation_spill_path(ctx.paths),
    )
    spilled = Path(json.loads(observation)["source_ids_spill"])
    assert spilled.is_relative_to(project)
    assert (await read({"path": str(spilled)})).splitlines() == source_ids

    denied = await read({"path": "/etc/hosts"})
    assert "outside the accessible roots" in denied
    wrapped = attach_tool_outcome(denied, fs_result_outcome(denied))
    assert tool_call_outcome(wrapped).lifecycle == "blocked"
    assert tool_call_outcome(wrapped).result_success is not True
    assert tool_result_failure(wrapped)[0] == "rejected"
