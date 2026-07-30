"""Writing a document longer than one response can carry.

Before ``append``, every write replaced the whole file — ``edit_file`` with an
empty ``old_string`` did too — so a long document had exactly one way in: a
single call carrying all of it. Incident 599a725b is what that costs when the
document is bigger than the response limit: the call is cut off mid-argument,
arrives unparseable, and no smaller retry is available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.config import load_settings
from omni.config.paths import get_paths
from omni.skills_runtime.builtin_tools.fs import build_fs_tools
from omni.skills_runtime.context import ExecContext


def _tool(tools, name):  # noqa: ANN001, ANN202
    return next(t for t in tools if t.spec.name == name).handler


@pytest.fixture
def write(tmp_path: Path):  # noqa: ANN201
    settings = load_settings()
    paths = get_paths(project="appendwrite")
    paths.project_dir.mkdir(parents=True, exist_ok=True)
    ctx = ExecContext(settings=settings, paths=paths, working_dir=tmp_path)
    return _tool(build_fs_tools(ctx), "write_file")


@pytest.mark.asyncio
async def test_a_long_document_can_arrive_in_successive_calls(write, tmp_path: Path) -> None:  # noqa: ANN001
    target = tmp_path / "survey.md"

    await write({"path": str(target), "contents": "# RAG survey\n\n## Retrieval\n"})
    await write({"path": str(target), "contents": "## Reranking\n", "append": True})
    await write({"path": str(target), "contents": "## Generation\n", "append": True})

    assert target.read_text(encoding="utf-8") == (
        "# RAG survey\n\n## Retrieval\n## Reranking\n## Generation\n"
    )


@pytest.mark.asyncio
async def test_writing_without_append_still_replaces_the_file(write, tmp_path: Path) -> None:  # noqa: ANN001
    """The default must not change: existing callers pass no ``append`` and rely
    on a write being a replacement."""
    target = tmp_path / "notes.md"
    target.write_text("stale draft", encoding="utf-8")

    await write({"path": str(target), "contents": "final draft"})

    assert target.read_text(encoding="utf-8") == "final draft"


@pytest.mark.asyncio
async def test_appending_to_a_file_that_does_not_exist_creates_it(write, tmp_path: Path) -> None:  # noqa: ANN001
    """A first chunk sent with append=true is a plausible model mistake, and
    failing it would strand the write with nothing yet on disk."""
    target = tmp_path / "new" / "draft.md"

    out = await write({"path": str(target), "contents": "opening section"})
    assert not out.startswith("ERROR")

    out = await write({"path": str(target), "contents": " and more", "append": True})

    assert not out.startswith("ERROR")
    assert target.read_text(encoding="utf-8") == "opening section and more"


@pytest.mark.asyncio
async def test_append_reports_the_running_total_so_progress_is_visible(write, tmp_path: Path) -> None:  # noqa: ANN001
    """Chunked writing is a loop, and a model driving it needs to see the file
    growing rather than only how much its latest call carried."""
    target = tmp_path / "paper.md"

    await write({"path": str(target), "contents": "x" * 100})
    out = await write({"path": str(target), "contents": "y" * 50, "append": True})

    assert "appended 50 chars" in out
    assert "150" in out


@pytest.mark.asyncio
async def test_append_is_refused_outside_the_write_roots(write, tmp_path: Path) -> None:  # noqa: ANN001
    """Appending is a write, so it answers to the same boundary."""
    out = await write({"path": "/etc/hosts", "contents": "evil", "append": True})

    assert out.startswith("ERROR")


def test_the_tool_tells_the_model_how_to_write_something_long(tmp_path: Path) -> None:
    """The parameter is only useful if the model knows it exists and when to
    reach for it; the schema is the only place it finds out."""
    settings = load_settings()
    paths = get_paths(project="appendwrite")
    paths.project_dir.mkdir(parents=True, exist_ok=True)
    ctx = ExecContext(settings=settings, paths=paths, working_dir=tmp_path)
    spec = next(t.spec for t in build_fs_tools(ctx) if t.spec.name == "write_file")

    assert "append" in spec.parameters["properties"]
    assert "append" not in spec.parameters.get("required", [])
    assert "append" in spec.description
