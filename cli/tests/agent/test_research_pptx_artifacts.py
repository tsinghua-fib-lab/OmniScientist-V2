"""research-pptx artifact contract and presentation integration."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omni.channels.outbound import delivery_envelope_from_presentation
from omni.config import load_settings
from omni.runtime.presentation import task_presentation_from_result
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "research-pptx"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _load_engine():
    path = SKILL_DIR / "engine.py"
    spec = importlib.util.spec_from_file_location("test_research_pptx_engine", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_portable_runner():
    path = SKILL_DIR / "scripts" / "run.py"
    spec = importlib.util.spec_from_file_location("test_research_pptx_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_research_pptx_store_returns_complete_artifact_descriptor(tmp_path: Path) -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    store = ArtifactStore(settings.paths, db)
    engine = _load_engine().ResearchPptxEngine()
    engine.ctx = SimpleNamespace(
        artifacts=store,
        session_id="",
        subtask_id="",
        workflow_run_id="",
    )
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx-bytes")

    artifact = await engine._store_pptx(str(source), "RAG Group Meeting")  # noqa: SLF001

    assert artifact["title"] == "RAG Group Meeting"
    assert artifact["format"] == "pptx"
    assert artifact["uri"].startswith("artifact://")
    assert Path(artifact["path"]).is_file()
    assert artifact["mime"] == PPTX_MIME
    assert artifact["size_bytes"] == len(b"pptx-bytes")


@pytest.mark.asyncio
async def test_research_pptx_store_passes_execution_ownership(tmp_path: Path) -> None:
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx-bytes")
    captured: dict = {}

    class CapturingArtifacts:
        async def put_bytes(self, data: bytes, **kwargs):  # noqa: ANN003, ANN202
            captured.update(kwargs)
            return SimpleNamespace(
                uri="artifact://deck123",
                path=source,
                mime=PPTX_MIME,
                size_bytes=len(data),
            )

    engine = _load_engine().ResearchPptxEngine()
    engine.ctx = SimpleNamespace(
        artifacts=CapturingArtifacts(),
        session_id="session-pptx",
        subtask_id="subtask-pptx",
        workflow_run_id="workflow-pptx",
    )

    await engine._store_pptx(str(source), "RAG Group Meeting")  # noqa: SLF001

    assert captured["session_id"] == "session-pptx"
    assert captured["subtask_id"] == "subtask-pptx"
    assert captured["workflow_run_id"] == "workflow-pptx"


@pytest.mark.asyncio
async def test_portable_artifact_store_matches_engine_contract(tmp_path: Path) -> None:
    runner = _load_portable_runner()
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"pptx-bytes")
    engine = _load_engine().ResearchPptxEngine()
    engine.ctx = SimpleNamespace(
        artifacts=runner._MockArtifacts(tmp_path / "portable-out"),  # noqa: SLF001
        session_id="portable-session",
        subtask_id="portable-subtask",
        workflow_run_id="portable-workflow",
    )

    artifact = await engine._store_pptx(str(source), "Portable Deck")  # noqa: SLF001

    assert artifact["uri"].startswith("file://")
    assert Path(artifact["path"]).read_bytes() == b"pptx-bytes"
    assert artifact["mime"] == PPTX_MIME
    assert artifact["size_bytes"] == len(b"pptx-bytes")


def test_research_pptx_manifest_declares_structured_artifacts() -> None:
    frontmatter = yaml.safe_load((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[1])
    artifacts = frontmatter["metadata"]["helixforge"]["output_schema"]["properties"]["artifacts"]

    assert artifacts["type"] == "array"
    assert artifacts["items"]["required"] == ["title", "format", "uri", "path", "mime", "size_bytes"]


def test_research_pptx_result_model_preserves_artifacts() -> None:
    result = _load_engine().PresentationResult(
        title="RAG Group Meeting",
        pptx_uri="artifact://deck123",
        artifacts=[
            {
                "title": "RAG Group Meeting",
                "format": "pptx",
                "uri": "artifact://deck123",
                "path": "/workspace/artifacts/presentation/deck.pptx",
                "mime": PPTX_MIME,
                "size_bytes": 42,
            }
        ],
    ).model_dump()

    assert result["artifacts"][0]["uri"] == "artifact://deck123"


def test_research_pptx_python_dependency_error_returns_setup_action() -> None:
    module = _load_engine()
    python_result = module._python_dependency_error(  # noqa: SLF001
        ModuleNotFoundError("No module named 'fitz'", name="fitz")
    )
    assert python_result["error_info"]["code"] == "runtime_dependency_missing"
    assert python_result["action_required"]["kind"] == "install"
    assert python_result["setup_command"] == "omni update --force"
    assert python_result["action_required"]["missing"] == ["fitz"]


def test_pptx_presentation_shows_cli_path_but_not_im_path(tmp_path: Path) -> None:
    pptx_path = tmp_path / "rag-group-meeting.pptx"
    pptx_path.write_bytes(b"pptx")
    result = {
        "status": "ok",
        "summary": "Generated the deck.",
        "pptx_uri": "artifact://deck123",
        "artifacts": [
            {
                "title": "RAG Group Meeting",
                "format": "pptx",
                "uri": "artifact://deck123",
                "path": str(pptx_path),
                "mime": PPTX_MIME,
                "size_bytes": pptx_path.stat().st_size,
            }
        ],
    }
    presentation = task_presentation_from_result(
        subtask_id="task-pptx",
        skill="research-pptx",
        status="succeeded",
        result=result,
    )

    cli = presentation.to_markdown()
    envelope = delivery_envelope_from_presentation(presentation)

    assert str(pptx_path) in cli
    assert "artifact://deck123" in cli
    assert str(pptx_path) not in envelope.parts[0].text
    assert [(part.kind, part.path) for part in envelope.parts[1:]] == [("file", str(pptx_path))]
