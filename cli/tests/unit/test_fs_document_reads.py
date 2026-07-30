"""``read_file`` as a research-grade reader: PDFs, binaries, and truncation.

A research agent whose reader hands back replacement-character noise for a PDF
only *appears* to support ``@paper.pdf``, and a silent truncation reads to the
model as "the file ends here". Both are correctness issues, not niceties.
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


def _ctx(file_uris: list[str] | None = None) -> ExecContext:
    settings = load_settings()
    paths = get_paths(project="docreads")
    paths.project_dir.mkdir(parents=True, exist_ok=True)
    return ExecContext(settings=settings, paths=paths, file_uris=file_uris or [])


@pytest.mark.asyncio
async def test_pdf_is_extracted_as_text_not_noise(tmp_path: Path) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    pytest.importorskip("pymupdf4llm")
    pdf = tmp_path / "paper.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Attention Is All You Need")
    document.save(str(pdf))
    document.close()

    out = await _tool(build_fs_tools(_ctx([str(pdf)])), "read_file")({"path": str(pdf)})

    assert "Attention Is All You Need" in out
    assert "\ufffd" not in out  # no replacement-character garbage


@pytest.mark.asyncio
async def test_binary_file_is_described_not_decoded(tmp_path: Path) -> None:
    blob = tmp_path / "model.bin"
    blob.write_bytes(b"\x00\x01\x02\x03" * 512)

    out = await _tool(build_fs_tools(_ctx([str(blob)])), "read_file")({"path": str(blob)})

    assert "binary file" in out
    assert "\ufffd" not in out


@pytest.mark.asyncio
async def test_long_file_truncation_is_announced_with_a_next_step(tmp_path: Path) -> None:
    big = tmp_path / "huge.md"
    big.write_text("x" * 250_000, encoding="utf-8")

    out = await _tool(build_fs_tools(_ctx([str(big)])), "read_file")({"path": str(big)})

    assert "truncated" in out
    assert "offset" in out  # tells the model how to continue


@pytest.mark.asyncio
async def test_plain_text_read_is_unchanged(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text("# Title\n\nbody\n", encoding="utf-8")

    out = await _tool(build_fs_tools(_ctx([str(note)])), "read_file")({"path": str(note)})

    assert out == "# Title\n\nbody\n"


@pytest.mark.asyncio
async def test_offset_and_limit_still_window_lines(tmp_path: Path) -> None:
    note = tmp_path / "lines.md"
    note.write_text("\n".join(f"line{i}" for i in range(10)), encoding="utf-8")

    out = await _tool(build_fs_tools(_ctx([str(note)])), "read_file")(
        {"path": str(note), "offset": 2, "limit": 3}
    )

    assert out == "line2\nline3\nline4"
