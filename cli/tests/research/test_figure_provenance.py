from __future__ import annotations

from types import SimpleNamespace

from omni.research.figure_provenance import (
    figure_provenance,
    figures_missing_script,
    sibling_source_script,
    slide_artifact_paths,
)


def test_sibling_dot_is_provenance(tmp_path) -> None:  # noqa: ANN001
    png = tmp_path / "rag.png"
    dot = tmp_path / "rag.dot"
    png.write_bytes(b"png")
    dot.write_text("digraph { a -> b }", encoding="utf-8")
    assert sibling_source_script(png) == str(dot)
    artifact = SimpleNamespace(kind="figure", path=str(png), uri="artifact://fig1", meta={})
    report = figure_provenance(artifact)
    assert report["has_provenance"] is True
    assert report["source_script"] == str(dot)
    assert report["package"] is True
    assert "Graphviz" in report["method"]
    assert figures_missing_script([artifact]) == []


def test_figure_without_script_is_recorded(tmp_path) -> None:  # noqa: ANN001
    png = tmp_path / "lonely.png"
    png.write_bytes(b"png")
    artifact = SimpleNamespace(kind="figure", path=str(png), uri="artifact://fig2", meta={})
    assert figures_missing_script([artifact]) == [str(png)]


def test_livefigure_pptx_is_not_a_figure_script_gap(tmp_path) -> None:  # noqa: ANN001
    deck = tmp_path / "LiveFigure-scientific-diagram.pptx"
    deck.write_bytes(b"pptx")
    artifact = SimpleNamespace(kind="figure", path=str(deck), uri="artifact://fig3", meta={})
    assert figures_missing_script([artifact]) == []


def test_slide_artifact_paths_collect_pptx(tmp_path) -> None:  # noqa: ANN001
    deck = tmp_path / "talk.pptx"
    deck.write_bytes(b"pptx")
    artifact = SimpleNamespace(kind="slides", path=str(deck), uri="artifact://deck", meta={})
    assert slide_artifact_paths([artifact]) == [str(deck)]
