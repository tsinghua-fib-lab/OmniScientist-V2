"""A document the model wrote is part of what the turn produced.

In task aac5b285 the paper the request asked for was written, and then absent
from every result surface: `artifact_ids` held only the figure skill's outputs,
so `/task show` listed the diagram and not the paper beside it. Registration ran
on the skill path and nowhere else. Codex closes the same gap from the tool
side — `apply_patch` feeds the turn diff tracker, and the UI reports from that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.config import load_settings
from omni.config.paths import get_paths
from omni.runtime.remaining import remaining_deliverables
from omni.skills_runtime.builtin_tools.fs import build_fs_tools
from omni.skills_runtime.context import ExecContext
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import TaskORM


def _tool(tools, name):  # noqa: ANN001, ANN202
    return next(t for t in tools if t.spec.name == name).handler


@pytest.fixture
async def turn(tmp_path: Path):  # noqa: ANN201
    """A context with a real store and a real task row to attribute writes to."""
    settings = load_settings()
    paths = get_paths(project="writtenartifacts")
    paths.ensure_dirs()
    db = get_database(paths.project_db)
    await db.init()
    store = ArtifactStore(paths, db)
    task_id = "task-written-artifacts"
    async with db.session() as s:
        if await s.get(TaskORM, task_id) is None:
            s.add(
                TaskORM(
                    id=task_id,
                    session_id="s1",
                    project=paths.project_name,
                    title="Written artifact review",
                )
            )
            await s.commit()
    ctx = ExecContext(
        settings=settings,
        paths=paths,
        working_dir=tmp_path,
        artifacts=store,
        task_id=task_id,
        session_id="s1",
        db=db,
    )
    yield ctx, db, task_id
    await db.dispose()


async def _artifact_ids(db, task_id: str) -> list[str]:  # noqa: ANN001
    async with db.session() as s:
        task = await s.get(TaskORM, task_id)
        return list(task.artifact_ids or [])


@pytest.mark.asyncio
async def test_the_paper_the_turn_wrote_is_listed_among_its_results(turn) -> None:  # noqa: ANN001
    ctx, db, task_id = turn
    write = _tool(build_fs_tools(ctx), "write_file")

    await write({"path": "RAG_review.md", "contents": "# RAG survey\n"})

    assert await _artifact_ids(db, task_id), "the written document is part of the turn's output"


@pytest.mark.asyncio
async def test_skill_artifact_calls_inherit_live_context_ownership(turn) -> None:  # noqa: ANN001
    ctx, db, task_id = turn
    late_bound = ExecContext(
        settings=ctx.settings,
        paths=ctx.paths,
        working_dir=ctx.working_dir,
        artifacts=ctx.artifacts._store,  # noqa: SLF001 - exercise late host binding
        db=db,
    )
    # SubtaskRuntime assigns these after constructing its context.
    late_bound.task_id = task_id
    late_bound.session_id = "s1"

    stored = await late_bound.artifacts.put_bytes(
        b"# result", kind="report", title="Result", ext="md"
    )
    row = await late_bound.artifacts.get(stored.uri)

    assert row is not None
    assert row.task_id == task_id


@pytest.mark.asyncio
async def test_bare_outputs_publish_into_the_trusted_task_bundle(tmp_path: Path) -> None:
    settings = load_settings()
    paths = get_paths(project="writtenartifacts-visible")
    paths.ensure_dirs()
    db = get_database(paths.project_db)
    await db.init()
    task_id = "visible-task-123456"
    async with db.session() as session:
        session.add(
            TaskORM(
                id=task_id,
                session_id="visible-session",
                project=paths.project_name,
                title="Visible research report",
            )
        )
        await session.commit()
    ctx = ExecContext(
        settings=settings,
        paths=paths,
        working_dir=tmp_path,
        artifacts=ArtifactStore(paths, db, mirror_dir=tmp_path, mirror_formats=["md"]),
        task_id=task_id,
        session_id="visible-session",
        db=db,
    )

    try:
        result = await _tool(build_fs_tools(ctx), "write_file")(
            {"path": "report.md", "contents": "# visible\n"}
        )
        expected = (
            tmp_path
            / "reports"
            / "Visible-research-report_visible-"
            / "report.md"
        )
        assert expected.read_text(encoding="utf-8") == "# visible\n"
        assert str(expected) in result
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_a_ledger_token_lands_in_the_task_report_bundle(tmp_path: Path) -> None:
    settings = load_settings()
    paths = get_paths(project="writtenartifacts-contract")
    paths.ensure_dirs()
    db = get_database(paths.project_db)
    await db.init()
    task_id = "visible-task-abcdef"
    async with db.session() as session:
        session.add(
            TaskORM(
                id=task_id,
                session_id="visible-session",
                project=paths.project_name,
                title="Latent space survey",
            )
        )
        await session.commit()
    ctx = ExecContext(
        settings=settings,
        paths=paths,
        working_dir=tmp_path,
        artifacts=ArtifactStore(paths, db, mirror_dir=tmp_path, mirror_formats=["md"]),
        task_id=task_id,
        session_id="visible-session",
        db=db,
    )
    stray = tmp_path / "draft.section"
    stray.write_text("old cwd token\n", encoding="utf-8")

    try:
        result = await _tool(build_fs_tools(ctx), "write_file")(
            {"path": "draft.section", "contents": "# rewritten survey\n"}
        )
        expected = (
            tmp_path
            / "reports"
            / "Latent-space-survey_visible-"
            / "Latent-space-survey.md"
        )
        assert expected.read_text(encoding="utf-8") == "# rewritten survey\n"
        assert str(expected) in result
        assert stray.read_text(encoding="utf-8") == "old cwd token\n"
        rows = await ctx.artifacts.list_by_task(task_id)
        assert remaining_deliverables(["draft.section"], rows) == []
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_a_document_built_from_several_appends_is_one_entry(turn) -> None:  # noqa: ANN001
    """Chunked writing is how a long paper arrives; it is still one paper."""
    ctx, db, task_id = turn
    write = _tool(build_fs_tools(ctx), "write_file")

    await write({"path": "survey.md", "contents": "# Survey\n"})
    await write({"path": "survey.md", "contents": "## Retrieval\n", "append": True})
    await write({"path": "survey.md", "contents": "## Reranking\n", "append": True})

    assert len(await _artifact_ids(db, task_id)) == 1


@pytest.mark.asyncio
async def test_a_same_named_repo_file_appearing_midway_does_not_split_the_record(turn) -> None:  # noqa: ANN001
    """Registration is keyed on the resolved path, so a deliverable that changed
    destination between chunks would be recorded twice, each row holding half a
    document. Resolving to artifacts first is what keeps the destination stable
    once the first chunk has created the file."""
    ctx, db, task_id = turn
    write = _tool(build_fs_tools(ctx), "write_file")

    await write({"path": "midway.md", "contents": "# Survey\n"})
    (ctx.working_dir / "midway.md").write_text("unrelated\n", encoding="utf-8")
    await write({"path": "midway.md", "contents": "## Retrieval\n", "append": True})

    ids = await _artifact_ids(db, task_id)
    assert len(ids) == 1, f"one document should be one record, got {len(ids)}"
    stored = await ctx.artifacts.get(ids[0])
    assert stored is not None
    assert stored.size_bytes == len("# Survey\n## Retrieval\n")


@pytest.mark.asyncio
async def test_a_written_document_keeps_the_bytes_the_model_wrote(turn) -> None:  # noqa: ANN001
    """Line endings are content, and the tool must not rewrite them.

    Text mode on Windows expands every "\\n" to "\\r\\n", so the same deliverable
    lands two bytes longer per line than the length its own tool just reported,
    the recorded size stops matching the file, and a document hashed on one
    platform no longer matches itself on another. Reading back in binary is the
    only way to see the translation; on POSIX this passes either way, so Windows
    CI is what this assertion is really for.
    """
    ctx, _db, _task_id = turn
    write = _tool(build_fs_tools(ctx), "write_file")
    contents = "# Survey\nfirst\nsecond\n"

    # A relative write resolves into the artifacts directory, so take the
    # destination from the tool rather than guessing where it landed.
    said = await write({"path": "endings.md", "contents": contents})
    written = Path(said.rsplit(" to ", 1)[1])

    on_disk = written.read_bytes()
    assert on_disk == contents.encode("utf-8")
    assert b"\r\n" not in on_disk


@pytest.mark.asyncio
async def test_the_recorded_size_follows_the_finished_file(turn) -> None:  # noqa: ANN001
    ctx, db, task_id = turn
    write = _tool(build_fs_tools(ctx), "write_file")

    await write({"path": "notes.md", "contents": "a"})
    await write({"path": "notes.md", "contents": "bcdef", "append": True})

    stored = await ctx.artifacts.get((await _artifact_ids(db, task_id))[0])
    assert stored is not None
    assert stored.size_bytes == 6, "an entry that stops at the first chunk understates the work"


@pytest.mark.asyncio
async def test_registering_leaves_the_file_where_the_user_was_told_it_is(turn) -> None:  # noqa: ANN001
    """Copying it into the store would leave two files and one wrong path."""
    ctx, db, task_id = turn
    write = _tool(build_fs_tools(ctx), "write_file")

    result = await write({"path": "RAG_review.md", "contents": "# survey"})

    landed = ctx.paths.artifacts_dir / "RAG_review.md"
    assert landed.is_file()
    assert str(landed) in result
    copies = list(ctx.paths.project_dir.rglob("RAG_review*.md"))
    assert copies == [landed], f"the document should exist once, found {copies}"


@pytest.mark.asyncio
async def test_editing_a_file_records_it_too(turn) -> None:  # noqa: ANN001
    ctx, db, task_id = turn
    tools = build_fs_tools(ctx)
    target = ctx.working_dir / "draft.md"
    target.write_text("old text", encoding="utf-8")

    await _tool(tools, "edit_file")(
        {"path": str(target), "old_string": "old", "new_string": "new"}
    )

    assert await _artifact_ids(db, task_id)


@pytest.mark.asyncio
async def test_a_write_outside_any_task_still_succeeds(tmp_path: Path) -> None:
    """Skills and headless helpers write without a task; recording is optional."""
    settings = load_settings()
    paths = get_paths(project="writtenartifacts")
    paths.ensure_dirs()
    ctx = ExecContext(settings=settings, paths=paths, working_dir=tmp_path)

    result = await _tool(build_fs_tools(ctx), "write_file")(
        {"path": str(tmp_path / "scratch.md"), "contents": "x"}
    )

    assert result.startswith("OK")
