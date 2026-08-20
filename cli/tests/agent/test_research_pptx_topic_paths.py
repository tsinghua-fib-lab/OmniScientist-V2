"""Bare filenames in topic are deliverable names, not cwd-required paths."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "research-pptx"


def _load_engine():
    path = SKILL_DIR / "engine.py"
    spec = importlib.util.spec_from_file_location("test_research_pptx_topic_paths", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_topic_bare_filename_is_a_mention_not_a_required_path() -> None:
    module = _load_engine()
    arguments = {
        "topic": "综述论文已写于 RAG-系统综述.md，请据此生成介绍型演示文稿",
        "reference_text": "Query → Retriever → Reranker → LLM",
        "language": "zh",
        "talk_type": "seminar",
    }

    error = module.ResearchPptxEngine.validate_params(arguments=arguments)

    assert error is None
    assert "markdown_uri" not in arguments


def test_topic_bare_filename_binds_when_file_is_in_task_reports(tmp_path: Path) -> None:
    module = _load_engine()
    manuscript = tmp_path / "reports" / "RAG-系统综述_demo" / "RAG-系统综述.md"
    manuscript.parent.mkdir(parents=True)
    manuscript.write_text("# RAG\n\nQuery, Retriever, Reranker, LLM.\n", encoding="utf-8")
    engine = module.ResearchPptxEngine()
    engine.ctx = SimpleNamespace(
        working_dir=tmp_path,
        paths=SimpleNamespace(workspace_root=tmp_path, artifacts_dir=tmp_path / "artifacts"),
    )
    arguments = {
        "topic": "综述论文已写于 RAG-系统综述.md，请据此生成介绍型演示文稿",
        "language": "zh",
    }

    error = engine.validate_params(arguments=arguments)

    assert error is None
    assert arguments["markdown_uri"] == str(manuscript)


def test_explicit_missing_markdown_uri_still_fails(tmp_path: Path) -> None:
    module = _load_engine()
    arguments = {
        "topic": "RAG 系统综述",
        "markdown_uri": str(tmp_path / "missing-outline.md"),
    }

    error = module.ResearchPptxEngine.validate_params(arguments=arguments)

    assert error is not None
    assert error["error_info"]["code"] == "markdown_not_found"


def test_explicit_bare_markdown_uri_resolves_from_reports(tmp_path: Path) -> None:
    module = _load_engine()
    manuscript = tmp_path / "reports" / "bundle" / "notes.md"
    manuscript.parent.mkdir(parents=True)
    manuscript.write_text("# Notes\n", encoding="utf-8")
    engine = module.ResearchPptxEngine()
    engine.ctx = SimpleNamespace(
        working_dir=tmp_path,
        paths=SimpleNamespace(workspace_root=tmp_path, artifacts_dir=tmp_path / "artifacts"),
    )
    arguments = {"topic": "Turn the notes into slides", "markdown_uri": "notes.md"}

    error = engine.validate_params(arguments=arguments)

    assert error is None
    assert arguments["markdown_uri"] == str(manuscript)


def test_reports_deliverable_wins_over_cwd_stray(tmp_path: Path) -> None:
    module = _load_engine()
    stray = tmp_path / "RAG-系统综述.md"
    stray.write_text("# stray cwd copy\n", encoding="utf-8")
    manuscript = tmp_path / "reports" / "RAG-系统综述_demo" / "RAG-系统综述.md"
    manuscript.parent.mkdir(parents=True)
    manuscript.write_text("# managed deliverable\n", encoding="utf-8")
    engine = module.ResearchPptxEngine()
    engine.ctx = SimpleNamespace(
        working_dir=tmp_path,
        paths=SimpleNamespace(workspace_root=tmp_path, artifacts_dir=tmp_path / "artifacts"),
    )
    arguments = {"topic": "综述论文已写于 RAG-系统综述.md，请生成 PPT"}

    error = engine.validate_params(arguments=arguments)

    assert error is None
    assert arguments["markdown_uri"] == str(manuscript)


def test_existing_absolute_topic_path_still_promotes(tmp_path: Path) -> None:
    module = _load_engine()
    markdown = tmp_path / "研究提纲.md"
    markdown.write_text("# Outline\n", encoding="utf-8")
    arguments = {"topic": f'请根据“{markdown}”生成汇报幻灯片'}

    error = module.ResearchPptxEngine.validate_params(arguments=arguments)

    assert error is None
    assert arguments["markdown_uri"] == str(markdown)
