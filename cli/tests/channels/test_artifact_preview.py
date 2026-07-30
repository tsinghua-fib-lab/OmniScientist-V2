"""Inline preview for small text/report artifacts in task-completion output.

Regression guard for the UX gap where a workflow that produced a Markdown
report surfaced only ``report_uri: artifact://…`` while a memory-recalled direct
answer for the same question inlined the body. Small text/report artifacts
should now render their body alongside the link; figures/binaries stay
link-only.
"""

from __future__ import annotations

from omni.runtime.artifact_preview import MAX_PREVIEW_BYTES, inline_text_artifacts
from omni.runtime.presentation import task_presentation_from_result


def _report_dir(tmp_path, art_id: str, body: str) -> str:
    """Lay out ``artifacts/report/<id>.md`` like the ArtifactStore does."""
    report_dir = tmp_path / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{art_id}.md").write_text(body, encoding="utf-8")
    return str(tmp_path)


def test_markdown_report_uri_is_inlined_alongside_link(tmp_path):
    art_id = "431772a15b1d4c0f9bf3b2e48ee52ca5"
    body = "# Navigator roadmap\n\n| area | why |\n| --- | --- |\n| memory | recall |\n"
    artifacts_dir = _report_dir(tmp_path, art_id, body)

    presentation = task_presentation_from_result(
        subtask_id="aab13696abcd",
        skill="workflow",
        status="succeeded",
        result={"summary": "Workflow completed.", "report_uri": f"artifact://{art_id}"},
    )
    enriched = inline_text_artifacts(presentation, artifacts_dir)
    md = enriched.to_markdown()

    # Link is preserved …
    assert f"artifact://{art_id}" in md
    # … and the body is now inlined (rendered as Markdown, not fenced).
    assert "Navigator roadmap" in md
    assert "| memory | recall |" in md
    assert "```" not in md
    assert enriched.artifacts[0].preview
    assert not enriched.artifacts[0].preview_truncated


def test_semantic_filename_scheme_resolves_by_id8_suffix(tmp_path):
    """New stores name files ``<slug>-<id8>.<ext>``; URI resolution must find
    them by the 8-char id suffix while legacy ``<id>.<ext>`` files keep working."""
    art_id = "10ddda6548f84f61b85256c99eb9dd8e"
    report_dir = tmp_path / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"RAG-综述草稿-{art_id[:8]}.md").write_text("# 草稿正文", encoding="utf-8")

    presentation = task_presentation_from_result(
        subtask_id="aab13696abcd",
        skill="workflow",
        status="succeeded",
        result={"summary": "done", "report_uri": f"artifact://{art_id}"},
    )
    enriched = inline_text_artifacts(presentation, str(tmp_path))

    assert enriched.artifacts[0].preview
    assert "草稿正文" in enriched.to_markdown()


def test_binary_figure_artifact_stays_link_only(tmp_path):
    art_id = "figpng0001"
    fig_dir = tmp_path / "figure"
    fig_dir.mkdir(parents=True, exist_ok=True)
    (fig_dir / f"{art_id}.png").write_bytes(b"\x89PNG\r\n\x00\x00binary")

    presentation = task_presentation_from_result(
        subtask_id="task-fig",
        skill="scientific-figure",
        status="succeeded",
        result={
            "summary": "figure done",
            "artifacts": [
                {"title": "Figure", "format": "png", "uri": f"artifact://{art_id}", "mime": "image/png"}
            ],
        },
    )
    enriched = inline_text_artifacts(presentation, str(tmp_path))

    assert enriched.artifacts[0].preview == ""
    assert f"artifact://{art_id}" in enriched.to_markdown()


def test_graphviz_dot_sidecar_is_hidden_from_preview(tmp_path):
    """DOT source artifacts stay stored but do not enter user-facing previews."""
    art_id = "dotsrc0001"
    fig_dir = tmp_path / "figure"
    fig_dir.mkdir(parents=True, exist_ok=True)
    (fig_dir / f"{art_id}.dot").write_text("digraph G { a -> b }", encoding="utf-8")

    presentation = task_presentation_from_result(
        subtask_id="task-dot",
        skill="scientific-figure",
        status="succeeded",
        result={
            "artifacts": [
                {"title": "DOT", "format": "dot", "uri": f"artifact://{art_id}", "mime": "text/vnd.graphviz"}
            ],
        },
    )
    enriched = inline_text_artifacts(presentation, str(tmp_path))
    assert enriched.artifacts == []


def test_large_text_report_is_truncated_with_open_hint(tmp_path):
    art_id = "bigreport001"
    body = "# Big\n" + ("x" * (MAX_PREVIEW_BYTES + 5000))
    artifacts_dir = _report_dir(tmp_path, art_id, body)

    presentation = task_presentation_from_result(
        subtask_id="task-big",
        skill="workflow",
        status="succeeded",
        result={"report_uri": f"artifact://{art_id}"},
    )
    enriched = inline_text_artifacts(presentation, artifacts_dir)
    art = enriched.artifacts[0]

    assert art.preview
    assert art.preview_truncated
    assert len(art.preview) <= MAX_PREVIEW_BYTES
    md = enriched.to_markdown()
    assert "Preview truncated" in md
    assert "open_artifact" in md


def test_plain_text_report_is_fenced(tmp_path):
    art_id = "plainreport1"
    txt_dir = tmp_path / "report"
    txt_dir.mkdir(parents=True, exist_ok=True)
    (txt_dir / f"{art_id}.txt").write_text("just plain notes\nsecond line", encoding="utf-8")

    presentation = task_presentation_from_result(
        subtask_id="task-txt",
        skill="workflow",
        status="succeeded",
        result={
            "artifacts": [
                {"title": "Notes", "format": "txt", "uri": f"artifact://{art_id}", "mime": "text/plain"}
            ],
        },
    )
    enriched = inline_text_artifacts(presentation, str(tmp_path))
    md = enriched.to_markdown()

    assert "just plain notes" in md
    assert "```" in md  # plain text is fenced, unlike markdown reports


def test_missing_artifacts_dir_is_noop():
    art_id = "431772a15b1d4c0f9bf3b2e48ee52ca5"
    presentation = task_presentation_from_result(
        subtask_id="task-x",
        skill="workflow",
        status="succeeded",
        result={"report_uri": f"artifact://{art_id}"},
    )
    enriched = inline_text_artifacts(presentation, None)
    assert enriched.artifacts[0].preview == ""
    assert enriched is presentation


def test_report_via_local_path_is_inlined(tmp_path):
    """A workflow file artifact carrying a real ``path`` inlines without a store."""
    doc = tmp_path / "chapter.md"
    doc.write_text("# Chapter\n\nprose body", encoding="utf-8")

    presentation = task_presentation_from_result(
        subtask_id="task-path",
        skill="workflow",
        status="succeeded",
        result={
            "steps": [
                {
                    "id": "writing",
                    "skill_name": "synthesis.final",
                    "status": "succeeded",
                    "result": {"files": [{"title": "Chapter", "path": str(doc), "format": "md"}]},
                }
            ]
        },
    )
    enriched = inline_text_artifacts(presentation, None)
    md = enriched.to_markdown()
    assert "prose body" in md
