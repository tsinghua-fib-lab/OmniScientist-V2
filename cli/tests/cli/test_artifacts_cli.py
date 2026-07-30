from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from omni.cli.main import app
from omni.config import load_settings
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM

runner = CliRunner()


def test_artifacts_preview_diff_and_versions(tmp_path):
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)

    async def setup() -> tuple[str, str]:
        await db.init()
        store = ArtifactStore(settings.paths, db)
        old = tmp_path / "old.dot"
        new = tmp_path / "new.dot"
        old.write_text('digraph G { a [label="A"]; }\n', encoding="utf-8")
        new.write_text('digraph G { a [label="A"]; b [label="B"]; a -> b; }\n', encoding="utf-8")
        old_art = await store.put_file(old, kind="figure", title="Old DOT", mime="text/vnd.graphviz")
        async with db.session() as s:
            s.add(
                SubtaskORM(
                    id="task-review-1",
                    skill_name="scientific-figure",
                    status="succeeded",
                    result_json={
                        "source_ids": ["source123456"],
                        "claim_ids": ["claim123456"],
                        "evidence_ids": ["evidence123456"],
                    },
                )
            )
            await s.commit()
        new_art = await store.put_file(
            new,
            kind="figure",
            title="New DOT",
            mime="text/vnd.graphviz",
            subtask_id="task-review-1",
            meta={"revision_of": str(old_art.path)},
        )
        return old_art.id, new_art.id

    old_id, new_id = asyncio.run(setup())

    preview = runner.invoke(app, ["artifacts", "preview", old_id[:8]])
    assert preview.exit_code == 0
    assert "Old DOT" in preview.stdout
    assert "digraph G" in preview.stdout

    diff = runner.invoke(app, ["artifacts", "diff", old_id[:8], new_id[:8]])
    assert diff.exit_code == 0
    assert "+digraph G" in diff.stdout or "b [label=\"B\"]" in diff.stdout

    versions = runner.invoke(app, ["artifacts", "versions", old_id[:8]])
    assert versions.exit_code == 0
    assert old_id[:8] in versions.stdout
    assert new_id[:8] in versions.stdout

    review = runner.invoke(app, ["artifacts", "review", new_id[:8]])
    assert review.exit_code == 0
    assert "artifact review" in review.stdout
    assert "file.exists" in review.stdout
    assert "contract.elements" in review.stdout
    assert "revision.link" in review.stdout
    assert "research.provenance" in review.stdout
    assert "source=source12" in review.stdout
