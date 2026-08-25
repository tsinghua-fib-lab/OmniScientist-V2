"""Read grants: ``@`` mentions, named absolute paths, and the shared envelope.

Reads follow Codex WorkspaceWrite (any path except secrets and Omni control
stores). ``@`` and a bare absolute path in the user message are the same
owner consent. Sensitivity still wins: ``@~/.ssh/id_rsa`` stays refused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omni.config import load_settings
from omni.config.paths import get_paths
from omni.skills_runtime.builtin_tools.fs import build_fs_tools
from omni.skills_runtime.context import ExecContext


def _tool(tools, name):  # noqa: ANN001, ANN202
    return next(t for t in tools if t.spec.name == name).handler


def _ctx(file_uris: list[str] | None = None) -> ExecContext:
    settings = load_settings()
    paths = get_paths(project="mentiongrant")
    paths.project_dir.mkdir(parents=True, exist_ok=True)
    return ExecContext(settings=settings, paths=paths, file_uris=file_uris or [])


@pytest.mark.asyncio
async def test_outside_file_is_readable_without_a_mention(tmp_path: Path) -> None:
    """WorkspaceWrite read envelope: a sibling tree is a list_dir, not a jail."""
    target = tmp_path / "paper.md"
    target.write_text("findings", encoding="utf-8")

    out = await _tool(build_fs_tools(_ctx()), "read_file")({"path": str(target)})

    assert out == "findings"


@pytest.mark.asyncio
async def test_mentioned_file_becomes_readable(tmp_path: Path) -> None:
    target = tmp_path / "paper.md"
    target.write_text("findings", encoding="utf-8")

    ctx = _ctx([str(target)])
    out = await _tool(build_fs_tools(ctx), "read_file")({"path": str(target)})

    assert out == "findings"


@pytest.mark.asyncio
async def test_named_message_path_is_a_read_root(tmp_path: Path) -> None:
    from omni.core.action_contracts import ResolverContext

    corpus = tmp_path / "sourcecode"
    corpus.mkdir()
    (corpus / "README.md").write_text("codex", encoding="utf-8")
    ctx = _ctx()
    ctx.resolver_context = ResolverContext(
        user_message=f"对标源码目录 {corpus} 实现",
        reference_time=datetime.now(UTC),
        timezone="UTC",
    )
    listing = await _tool(build_fs_tools(ctx), "list_dir")({"path": str(corpus)})
    assert "README.md" in listing


@pytest.mark.asyncio
async def test_mentioning_a_secret_is_still_refused(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=abc", encoding="utf-8")

    ctx = _ctx([str(secret)])
    out = await _tool(build_fs_tools(ctx), "read_file")({"path": str(secret)})

    assert out.startswith("ERROR")
    assert "sensitive" in out
    assert "TOKEN" not in out


@pytest.mark.asyncio
async def test_symlink_to_a_secret_cannot_launder_the_grant(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=abc", encoding="utf-8")
    decoy = tmp_path / "notes.md"
    decoy.symlink_to(secret)

    ctx = _ctx([str(decoy)])
    out = await _tool(build_fs_tools(ctx), "read_file")({"path": str(decoy)})

    assert out.startswith("ERROR")
    assert "TOKEN" not in out


@pytest.mark.asyncio
async def test_mentioned_directory_grants_its_subtree(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    paper = corpus / "a.md"
    paper.write_text("body", encoding="utf-8")

    ctx = _ctx([str(corpus)])
    tools = build_fs_tools(ctx)

    assert await _tool(tools, "read_file")({"path": str(paper)}) == "body"
    listing = await _tool(tools, "list_dir")({"path": str(corpus)})
    assert "a.md" in listing


@pytest.mark.asyncio
async def test_file_uri_form_and_artifact_entries(tmp_path: Path) -> None:
    target = tmp_path / "paper.md"
    target.write_text("findings", encoding="utf-8")

    ctx = _ctx([f"file://{target}", "artifact://something"])
    out = await _tool(build_fs_tools(ctx), "read_file")({"path": str(target)})

    assert out == "findings"


@pytest.mark.asyncio
async def test_model_may_pass_the_at_marker_verbatim(tmp_path: Path) -> None:
    target = tmp_path / "paper.md"
    target.write_text("findings", encoding="utf-8")

    ctx = _ctx([str(target)])
    out = await _tool(build_fs_tools(ctx), "read_file")({"path": f"@{target}"})

    assert out == "findings"
