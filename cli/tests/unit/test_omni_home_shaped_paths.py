"""The write guard, exercised against paths shaped like the ones that ship.

Two commits ago the workspace artifacts directory became the default destination
for a bare filename, and the same commit made every path under `.omni` read-only.
In production the workspace *is* `~/.omni/workspaces/<slug>/`, so the default
destination was refused by the guard that shipped beside it: the model asked to
write a paper, was denied, and fell back to the repository root. No paper ever
reached the artifacts directory.

Every test agreed the feature worked, because `conftest` pointed `OMNI_HOME` at a
directory with no leading dot, so the guard answered False under pytest and True
in production. The only configuration that ships was the one never tested.
`conftest` has since moved to the shipping shape, taking the name from
`paths._PROJECT_MARKER` so the fixture cannot drift from the rule again; these
tests keep their own literal paths anyway, because `is_write_protected_path` is a
pure function of a path and a hard-coded production path is the clearest possible
statement of what it must answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.config import load_settings
from omni.config.paths import get_paths
from omni.core.sensitive_paths import is_write_protected_path
from omni.skills_runtime.builtin_tools.fs import build_fs_tools
from omni.skills_runtime.context import ExecContext

_HOME = Path("/Users/researcher/.omni")
_WORKSPACE = _HOME / "workspaces" / "omniscientist_v2-63cf08e0"
_ARTIFACTS = _WORKSPACE / "artifacts"


def _tool(tools, name):  # noqa: ANN001, ANN202
    return next(t for t in tools if t.spec.name == name).handler


# ── the output area omni is supposed to write into ──


def test_the_workspace_artifacts_directory_is_where_output_goes() -> None:
    assert not is_write_protected_path(_ARTIFACTS / "RAG_review.md", [_ARTIFACTS])


def test_a_subdirectory_of_the_output_area_is_output_too() -> None:
    """Skills file their bundles under `artifacts/bundle/`."""
    assert not is_write_protected_path(_ARTIFACTS / "bundle" / "figure.json", [_ARTIFACTS])


def test_without_a_declared_output_area_the_store_is_closed() -> None:
    """The exemption is something a caller opts into by naming a directory, not
    a hole that opens by default."""
    assert is_write_protected_path(_ARTIFACTS / "RAG_review.md")


# ── what stays protected: omni's own state ──


@pytest.mark.parametrize(
    "target",
    [
        _WORKSPACE / "sessions.sqlite3",
        _WORKSPACE / "sessions.sqlite3-wal",
        _WORKSPACE / "library.jsonl",
        _WORKSPACE / "inbox.jsonl",
        _WORKSPACE / ".resource-locks" / "skill.lock",
        _WORKSPACE / "skills" / "evil" / "SKILL.md",
        _HOME / "config.toml",
        _HOME / "secrets.toml",
        _HOME / "trust.json",
        _HOME / "control.sqlite3",
        _HOME / "memory.sqlite3",
        _HOME / "workspaces.json",
        _HOME / "skills" / "evil" / "engine.py",
    ],
)
def test_omni_state_is_not_reachable_even_with_an_output_area_declared(target: Path) -> None:
    """Declaring where output goes must not unlock the store beside it: these
    files decide what runs and what later turns trust."""
    assert is_write_protected_path(target, [_ARTIFACTS])


def test_the_store_of_another_workspace_is_not_this_turns_output() -> None:
    other = _HOME / "workspaces" / "someone-else-11111111" / "artifacts" / "paper.md"

    assert is_write_protected_path(other, [_ARTIFACTS])


# ── version control metadata is never negotiable ──


def test_git_metadata_stays_refused_however_the_output_area_is_declared() -> None:
    """A hook runs on the owner's next commit, so writing one turns file access
    into code execution. No declaration may exempt it."""
    hook = Path("/repo/.git/hooks/pre-commit")

    assert is_write_protected_path(hook, [Path("/repo/.git")])
    assert is_write_protected_path(hook, [Path("/repo")])


def test_agent_metadata_dirs_are_write_protected() -> None:
    """``.agents`` / ``.codex`` are host-owned metadata, same as ``.omni``."""
    assert is_write_protected_path(Path("/repo/.agents/skills/evil/SKILL.md"))
    assert is_write_protected_path(Path("/repo/.codex/skills/evil/SKILL.md"))


# ── and the whole path, end to end, on a home shaped like the real one ──


@pytest.fixture
def production_shaped_home(tmp_path: Path, omni_home: Path) -> ExecContext:
    """A workspace under the store `conftest` already provides.

    The store is shaped the way an installed omni lays one out, so this no longer
    has to opt back into the real shape — it only has to name a project inside it
    and a working directory outside it, which is the arrangement the guard sees in
    the field.
    """
    paths = get_paths(project="shapedhome")
    paths.ensure_dirs()
    working = tmp_path / "repo"
    working.mkdir(parents=True, exist_ok=True)
    return ExecContext(settings=load_settings(), paths=paths, working_dir=working)


@pytest.mark.asyncio
async def test_the_paper_reaches_the_artifacts_directory_on_a_real_shaped_home(
    production_shaped_home: ExecContext,
) -> None:
    ctx = production_shaped_home
    assert ".omni" in ctx.paths.artifacts_dir.parts, "fixture must reproduce the shipped shape"

    result = await _tool(build_fs_tools(ctx), "write_file")(
        {"path": "RAG_review.md", "contents": "# survey"}
    )

    landed = ctx.paths.artifacts_dir / "RAG_review.md"
    assert not result.startswith("ERROR"), result
    assert landed.read_text(encoding="utf-8") == "# survey"


@pytest.mark.asyncio
async def test_the_session_store_is_still_refused_on_that_same_home(
    production_shaped_home: ExecContext,
) -> None:
    ctx = production_shaped_home
    target = ctx.paths.project_dir / "sessions.sqlite3"

    result = await _tool(build_fs_tools(ctx), "write_file")(
        {"path": str(target), "contents": "corrupt"}
    )

    assert result.startswith("ERROR")
    assert "protected" in result
