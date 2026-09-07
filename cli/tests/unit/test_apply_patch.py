from __future__ import annotations

from pathlib import Path

import pytest

from omni.config import load_settings
from omni.config.paths import get_paths
from omni.skills_runtime.apply_patch_helper import (
    FilePatch,
    Hunk,
    PatchError,
    apply_file_patches,
    apply_to_text,
    main,
    parse_patch,
)
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


def test_empty_patch_and_file_operations(tmp_path: Path) -> None:
    with pytest.raises(PatchError, match="empty"):
        parse_patch("   ")

    files: dict[str, str] = {}

    def exists(path: str) -> bool:
        return path in files

    def read_text(path: str) -> str:
        return files[path]

    def write_text(path: str, text: str) -> None:
        files[path] = text

    def delete(path: str) -> None:
        del files[path]

    add = FilePatch(path="new.md", kind="add", hunks=[Hunk(lines=[("+", "hello")])])
    assert apply_file_patches(
        [add], read_text=read_text, write_text=write_text, exists=exists, delete=delete
    ) == ["added new.md"]
    assert files["new.md"] == "hello\n"

    with pytest.raises(PatchError, match="already exists"):
        apply_file_patches(
            [add], read_text=read_text, write_text=write_text, exists=exists, delete=delete
        )

    delete_patch = FilePatch(path="new.md", kind="delete")
    assert apply_file_patches(
        [delete_patch],
        read_text=read_text,
        write_text=write_text,
        exists=exists,
        delete=delete,
    ) == ["deleted new.md"]
    assert "new.md" not in files

    with pytest.raises(PatchError, match="file not found"):
        apply_file_patches(
            [delete_patch],
            read_text=read_text,
            write_text=write_text,
            exists=exists,
            delete=delete,
        )
    with pytest.raises(PatchError, match="file not found"):
        apply_file_patches(
            [FilePatch(path="missing.md", kind="update", hunks=[Hunk()])],
            read_text=read_text,
            write_text=write_text,
            exists=exists,
            delete=delete,
        )
    with pytest.raises(PatchError, match="missing a target path"):
        apply_file_patches(
            [FilePatch(path="", kind="add")],
            read_text=read_text,
            write_text=write_text,
            exists=exists,
            delete=delete,
        )


def test_codex_add_delete_and_unified_delete() -> None:
    added = parse_patch(
        "*** Begin Patch\n*** Add File: notes.md\n+hello\n*** End Patch\n"
    )
    assert added[0].kind == "add"
    assert apply_to_text("", added[0].hunks) == "hello\n"

    deleted = parse_patch(
        "*** Begin Patch\n*** Delete File: notes.md\n*** End Patch\n"
    )
    assert deleted[0].kind == "delete"

    with pytest.raises(PatchError, match="missing a target path"):
        parse_patch("--- /dev/null\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n")

    with pytest.raises(PatchError, match="no file hunks"):
        parse_patch("this is not a diff")
    with pytest.raises(PatchError, match="before a file header"):
        parse_patch("@@ -1 +1 @@\n-a\n+b\n")


def test_context_mismatch_and_cli_main(tmp_path: Path) -> None:
    original = "alpha\nbeta\n"

    class _EofContext(Hunk):
        @property
        def expected_old(self) -> list[str]:
            return []

    mismatch = _EofContext(old_start=3, lines=[(" ", "nope"), ("+", "BETA")])
    with pytest.raises(PatchError, match="context mismatch"):
        apply_to_text(original, [mismatch], path="notes.md")

    target = tmp_path / "notes.md"
    target.write_text("keep\n", encoding="utf-8")
    patch_file = tmp_path / "change.diff"
    patch_file.write_text(
        "--- a/notes.md\n+++ b/notes.md\n@@ -1 +1 @@\n-keep\n+changed\n",
        encoding="utf-8",
    )
    assert main([str(patch_file), "--root", str(tmp_path)]) == 0
    assert target.read_text(encoding="utf-8") == "changed\n"

    bad = tmp_path / "bad.diff"
    bad.write_text("not a patch", encoding="utf-8")
    assert main([str(bad), "--root", str(tmp_path)]) == 2

    mismatch = tmp_path / "mismatch.diff"
    mismatch.write_text(
        "--- a/notes.md\n+++ b/notes.md\n@@ -1 +1 @@\n-missing\n+other\n",
        encoding="utf-8",
    )
    assert main([str(mismatch), "--root", str(tmp_path)]) == 1
