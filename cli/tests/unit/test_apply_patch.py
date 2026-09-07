from __future__ import annotations

from pathlib import Path

import pytest

from omni.config import load_settings
from omni.config.paths import get_paths
from omni.skills_runtime.apply_patch_helper import PatchError, apply_to_text, parse_patch
from omni.skills_runtime.builtin_tools.fs import build_fs_tools
from omni.skills_runtime.context import ExecContext


def test_unified_diff_multi_hunk() -> None:
    original = "alpha\nbeta\ngamma\ndelta\n"
    patch = """--- a/notes.md
+++ b/notes.md
@@ -1,2 +1,2 @@
 alpha
-beta
+BETA
@@ -3,2 +3,2 @@
 gamma
-delta
+DELTA
"""
    files = parse_patch(patch)
    assert files[0].path == "notes.md"
    assert len(files[0].hunks) == 2
    assert apply_to_text(original, files[0].hunks, path="notes.md") == "alpha\nBETA\ngamma\nDELTA\n"


def test_hunk_mismatch_names_the_hunk() -> None:
    original = "alpha\nbeta\n"
    patch = """--- a/notes.md
+++ b/notes.md
@@ -1,2 +1,2 @@
 alpha
-NOPE
+BETA
"""
    files = parse_patch(patch)
    with pytest.raises(PatchError, match="hunk 1"):
        apply_to_text(original, files[0].hunks, path="notes.md")


def test_codex_update_file() -> None:
    original = "hello\nworld\n"
    patch = """*** Begin Patch
*** Update File: greet.md
@@
 hello
-world
+WORLD
*** End Patch
"""
    files = parse_patch(patch)
    assert files[0].kind == "update"
    assert apply_to_text(original, files[0].hunks) == "hello\nWORLD\n"


@pytest.mark.asyncio
async def test_apply_patch_tool_updates_existing_file(tmp_path: Path) -> None:
    settings = load_settings()
    paths = get_paths(project="applypatch")
    paths.project_dir.mkdir(parents=True, exist_ok=True)
    ctx = ExecContext(settings=settings, paths=paths, working_dir=tmp_path)
    tools = build_fs_tools(ctx)
    apply = next(tool.handler for tool in tools if tool.spec.name == "apply_patch")
    target = tmp_path / "survey.md"
    target.write_text("# Title\n\nold paragraph\n", encoding="utf-8")
    out = await apply(
        {
            "patch": (
                f"--- a/{target}\n+++ b/{target}\n"
                "@@ -1,3 +1,3 @@\n"
                " # Title\n"
                " \n"
                "-old paragraph\n"
                "+new paragraph\n"
            )
        }
    )
    assert out.startswith("OK:")
    assert "new paragraph" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_apply_patch_tool_reports_failed_hunk(tmp_path: Path) -> None:
    settings = load_settings()
    paths = get_paths(project="applypatch2")
    paths.project_dir.mkdir(parents=True, exist_ok=True)
    ctx = ExecContext(settings=settings, paths=paths, working_dir=tmp_path)
    apply = next(tool.handler for tool in build_fs_tools(ctx) if tool.spec.name == "apply_patch")
    target = tmp_path / "notes.md"
    target.write_text("keep\n", encoding="utf-8")
    out = await apply(
        {
            "patch": (
                f"--- a/{target}\n+++ b/{target}\n"
                "@@ -1,1 +1,1 @@\n"
                "-missing\n"
                "+other\n"
            )
        }
    )
    assert out.startswith("ERROR:")
    assert "hunk" in out
