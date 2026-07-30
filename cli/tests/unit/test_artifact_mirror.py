"""ArtifactStore single-copy output into the (trusted) launch directory.

When the launch directory is trusted, user-facing deliverables are written
DIRECTLY into it as the single canonical copy (Codex / Claude-Code parity) —
there is no duplicate under ``~/.omni``. Sidecars and untrusted runs keep the
durable workspace store. These tests pin that behaviour and the ``artifact://``
round-trip through ``resolve_path``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from omni.config import load_settings
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import TaskORM


async def _store(mirror_dir=None, formats=None) -> ArtifactStore:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    return ArtifactStore(s.paths, db, mirror_dir=mirror_dir, mirror_formats=formats)


async def _task(store: ArtifactStore, task_id: str, title: str) -> None:
    async with store._db.session() as session:
        if await session.get(TaskORM, task_id) is None:
            session.add(
                TaskORM(
                    id=task_id,
                    session_id=f"session-{task_id}",
                    project=store._paths.project_name,
                    title=title,
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_untrusted_run_keeps_single_copy_in_store():
    store = await _store(mirror_dir=None)
    art = await store.put_bytes(b"<svg/>", kind="figure", title="Fig", ext="svg")
    assert art.mirror_path is None
    assert art.path.is_file()  # canonical copy lives under the durable store
    assert store._paths.artifacts_dir in art.path.parents


@pytest.mark.asyncio
async def test_deliverable_written_once_into_launch_dir(tmp_path):
    out = tmp_path / "out"
    store = await _store(mirror_dir=out, formats=["svg", "png"])
    art = await store.put_bytes(
        b"<svg/>", kind="figure", title="RAG Arch", ext="svg", task_id="deadbeef01"
    )
    # Single canonical copy sits in a clean per-kind subfolder of the launch dir…
    assert art.path.parent == (out / "figures").resolve()
    # …named ``<slug>-<task8>-<art8>.<ext>`` so the owning task is greppable.
    assert art.path.name == f"RAG-Arch-deadbeef-{art.id[:8]}.svg"
    assert art.path.is_file()
    assert art.path.read_bytes() == b"<svg/>"
    # No separate mirror copy, and no duplicate under the durable store.
    assert art.mirror_path is None
    assert not (store._paths.artifacts_dir / "figure" / art.path.name).exists()


@pytest.mark.asyncio
async def test_reports_go_to_reports_subfolder(tmp_path):
    out = tmp_path / "out"
    store = await _store(mirror_dir=out, formats=["md"])
    art = await store.put_bytes(
        b"# report",
        kind="report",
        title="RAG Review",
        ext="md",
        mime="text/markdown",
        task_id="aabbccdd01",
    )
    assert art.path.parent == (out / "reports").resolve()
    assert art.path.name == f"RAG-Review-aabbccdd-{art.id[:8]}.md"


@pytest.mark.asyncio
async def test_one_task_keeps_mixed_outputs_in_one_named_bundle(tmp_path):
    out = tmp_path / "out"
    store = await _store(mirror_dir=out, formats=["md", "svg", "json"])
    task_id = "mixedtask01234567"
    await _task(store, task_id, "RAG architecture review")

    report = await store.put_bytes(
        b"# report", kind="report", title="Review", ext="md", task_id=task_id
    )
    figure = await store.put_bytes(
        b"<svg/>", kind="figure", title="Architecture", ext="svg", task_id=task_id
    )
    data = await store.put_bytes(
        b"{}", kind="data", title="Evidence", ext="json", task_id=task_id
    )

    assert report.path.parent == figure.path.parent == data.path.parent
    assert report.path.parent.parent == (out / "reports").resolve()
    assert report.path.parent.name == "RAG-architecture-review_mixedtas"
    manifest = json.loads((report.path.parent / "_omni-manifest.json").read_text())
    assert manifest["task_id"] == task_id
    assert {item["uri"] for item in manifest["artifacts"]} == {
        report.uri,
        figure.uri,
        data.uri,
    }


@pytest.mark.asyncio
async def test_historical_uri_uses_its_persisted_output_root(tmp_path):
    first_root = tmp_path / "first"
    store = await _store(mirror_dir=first_root, formats=["md"])
    task_id = "history0012345678"
    await _task(store, task_id, "Historical report")
    art = await store.put_bytes(
        b"# report", kind="report", title="Report", ext="md", task_id=task_id
    )

    moved_session = ArtifactStore(
        store._paths,
        store._db,
        mirror_dir=tmp_path / "second",
        mirror_formats=["md"],
    )

    assert await moved_session.resolve_path(art.uri) == art.path.resolve()
    assert await moved_session.task_output_path(
        "continued.md", task_id=task_id, kind="document"
    ) == art.path.parent / "continued.md"
    assert first_root.resolve() in moved_session.managed_output_roots


@pytest.mark.asyncio
async def test_concurrent_first_outputs_cannot_split_the_task_bundle(tmp_path):
    out = tmp_path / "out"
    store = await _store(mirror_dir=out, formats=["md", "svg"])
    task_id = "concurrent01234567"
    await _task(store, task_id, "Concurrent output")

    report, figure = await asyncio.gather(
        store.put_bytes(
            b"# report", kind="report", title="Report", ext="md", task_id=task_id
        ),
        store.put_bytes(
            b"<svg/>", kind="figure", title="Figure", ext="svg", task_id=task_id
        ),
    )

    assert report.path.parent == figure.path.parent
    manifest = json.loads((report.path.parent / "_omni-manifest.json").read_text())
    assert {item["uri"] for item in manifest["artifacts"]} == {
        report.uri,
        figure.uri,
    }


@pytest.mark.asyncio
async def test_same_title_files_stay_distinct_via_art_id(tmp_path):
    out = tmp_path / "out"
    store = await _store(mirror_dir=out, formats=["svg"])
    first = await store.put_bytes(
        b"<svg>1</svg>", kind="figure", title="Arch", ext="svg", task_id="task0001"
    )
    second = await store.put_bytes(
        b"<svg>2</svg>", kind="figure", title="Arch", ext="svg", task_id="task0001"
    )
    assert first.path.name == f"Arch-task0001-{first.id[:8]}.svg"
    assert second.path.name == f"Arch-task0001-{second.id[:8]}.svg"
    assert first.path.name != second.path.name
    assert first.path.read_bytes() == b"<svg>1</svg>"
    assert second.path.read_bytes() == b"<svg>2</svg>"


@pytest.mark.asyncio
async def test_figure_bundle_co_locates_source_and_provenance(tmp_path):
    """A ``figure`` is a self-contained bundle: the rendered image, its ``.dot``
    source, and ``*.provenance.json`` all sit together in ``figures/`` — even
    though ``dot``/``json`` are not user-facing formats."""
    out = tmp_path / "out"
    store = await _store(mirror_dir=out, formats=["svg", "png"])
    figures = (out / "figures").resolve()
    tid = "bundle01"

    svg = await store.put_bytes(
        b"<svg/>", kind="figure", title="RAG Arch SVG", ext="svg", task_id=tid
    )
    dot = await store.put_bytes(
        b"digraph{}", kind="figure", title="RAG Arch DOT", ext="dot", task_id=tid
    )
    prov = await store.put_bytes(
        b"{}",
        kind="figure",
        title="RAG Arch",
        ext="provenance.json",
        mime="application/json",
        task_id=tid,
    )

    # All three land in figures/ as a single copy each, with task+art suffixes.
    assert svg.path.parent == figures
    assert dot.path.parent == figures
    assert prov.path.parent == figures
    assert dot.path.name == f"RAG-Arch-bundle01-{dot.id[:8]}.dot"
    assert prov.path.name == f"RAG-Arch-bundle01-{prov.id[:8]}.provenance.json"
    # Nothing is duplicated back under the durable store.
    for art in (svg, dot, prov):
        assert art.mirror_path is None
        assert store._paths.artifacts_dir not in art.path.parents


@pytest.mark.asyncio
async def test_non_bundle_sidecar_stays_in_store(tmp_path):
    """A loose sidecar of a *non-bundle* kind (e.g. a ``data`` json that is not a
    user-facing deliverable) still stays hidden in the durable store."""
    out = tmp_path / "out"
    store = await _store(mirror_dir=out, formats=["svg", "png"])
    side = await store.put_bytes(b"{}", kind="data", title="Notes", ext="json")
    assert side.mirror_path is None
    assert store._paths.artifacts_dir in side.path.parents
    assert not out.exists() or not list(out.rglob("*.json"))


@pytest.mark.asyncio
async def test_empty_formats_treats_all_as_deliverables(tmp_path):
    out = tmp_path / "out"
    store = await _store(mirror_dir=out, formats=None)
    art = await store.put_bytes(b"x", kind="data", title="d", ext="json")
    assert art.path.parent == (out / "data").resolve()
    assert art.mirror_path is None


@pytest.mark.asyncio
async def test_resolve_path_maps_uri_to_launch_dir_file(tmp_path):
    out = tmp_path / "out"
    store = await _store(mirror_dir=out, formats=["svg"])
    art = await store.put_bytes(b"<svg/>", kind="figure", title="Fig", ext="svg")
    # The artifact:// URI still resolves to the single launch-directory copy,
    # even though it lives outside the durable store (guarded to the output dir).
    resolved = await store.resolve_path(art.uri)
    assert resolved == art.path.resolve()
    assert resolved.parent == (out / "figures").resolve()


# ── shared result-record helper (StoredArtifact → skill-result dict) ────────
@pytest.mark.asyncio
async def test_result_record_uses_launch_dir_path(tmp_path):
    out = tmp_path / "out"
    store = await _store(mirror_dir=out, formats=["svg"])
    art = await store.put_bytes(
        b"<svg/>", kind="figure", title="RAG Arch", ext="svg", mime="image/svg+xml"
    )
    rec = art.result_record(format="svg")
    # The displayed path is the launch-directory file (next to the user), not a
    # ~/.omni store path — this is the fix for the misleading path.
    assert rec["path"] == str(art.path)
    assert art.path.parent == (out / "figures").resolve()
    # The durable artifact:// URI is always retained for resolution.
    assert rec["uri"] == art.uri
    assert rec["format"] == "svg"
    assert rec["mime"] == "image/svg+xml"
    assert rec["title"] == "RAG Arch"
    assert rec["size_bytes"] == str(art.size_bytes)


@pytest.mark.asyncio
async def test_result_record_falls_back_to_store_path(tmp_path):
    store = await _store(mirror_dir=None)
    art = await store.put_bytes(
        b"# report", kind="report", title="Rep", ext="md", mime="text/markdown"
    )
    rec = art.result_record()
    # Untrusted → the canonical store path is what we surface.
    assert art.mirror_path is None
    assert rec["path"] == str(art.path)
    assert rec["format"] == "md"  # derived from the file extension
    assert rec["uri"] == art.uri


# ── orchestrator wiring: launch-directory semantics ────────────────────────
def test_output_dir_resolves_to_launch_cwd_when_trusted(tmp_path, monkeypatch):
    from omni.agent.orchestrator import _artifact_mirror_dir

    monkeypatch.chdir(tmp_path)
    trusted = load_settings(cwd=tmp_path, trusted=True)
    # "." → the directory omni was launched in
    assert _artifact_mirror_dir(trusted) == tmp_path.resolve()
    # restricted / untrusted → outputs stay in the durable store only
    assert _artifact_mirror_dir(load_settings(cwd=tmp_path, trusted=False)) is None
    assert _artifact_mirror_dir(load_settings(cwd=tmp_path, trusted=None)) is None


@pytest.mark.asyncio
async def test_agent_writes_generated_file_into_launch_dir(tmp_path, monkeypatch):
    from omni.agent import OmniAgent

    monkeypatch.chdir(tmp_path)
    settings = load_settings(cwd=tmp_path, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        art = await agent.artifacts.put_bytes(
            b"<svg/>", kind="figure", title="RAG Arch", ext="svg"
        )
        # The single deliverable copy is written into the launch dir's figures/.
        assert art.path.parent == (tmp_path / "figures").resolve()
        assert art.path.is_file()
        assert art.mirror_path is None
        # It is NOT duplicated under the durable workspace store.
        assert settings.paths.artifacts_dir not in art.path.parents
        # …and the artifact:// URI still round-trips to that one file.
        assert await agent.artifacts.resolve_path(art.uri) == art.path.resolve()
    finally:
        await agent.aclose()
