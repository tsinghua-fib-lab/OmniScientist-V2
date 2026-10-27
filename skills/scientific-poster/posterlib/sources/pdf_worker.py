"""Keep PDF preparation independent of a host reader's process-global settings."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import redirect_stdout, suppress
from dataclasses import asdict, replace
from pathlib import Path

from . import PaperSourceError
from .paper_extraction import (
    PreparedPaper,
    _extract_layout_visual_carriers,
    _parse_carrier_label,
    enrich_structured_equations,
)

_SKILL_ROOT = Path(__file__).resolve().parents[2]


def _command(pdf_path: str | Path, output_dir: str | Path) -> list[str]:
    return [
        sys.executable, "-m", "posterlib.sources.pdf_worker",
        str(Path(pdf_path).expanduser().resolve()),
        str(Path(output_dir).expanduser().resolve()),
    ]


def _read_result(stdout: bytes, stderr: bytes, returncode: int) -> PreparedPaper:
    if returncode:
        raise PaperSourceError(
            "source_read_failed",
            f"PDF preparation process exited {returncode}: "
            + stderr.decode("utf-8", errors="replace").strip(),
        )
    try:
        result = json.loads(stdout)
    except ValueError as exc:
        raise PaperSourceError("source_read_failed", "PDF preparation returned invalid data.") from exc
    if "error" in result:
        raise PaperSourceError(result["error"]["code"], result["error"]["message"])
    paper = result["paper"]
    paper["figures"] = tuple(paper["figures"])
    paper["equations"] = tuple(paper.get("equations", ()))
    paper["warnings"] = tuple(paper.get("warnings", ()))
    return PreparedPaper(**paper)


def prepare_pdf(pdf_path: str | Path, output_dir: str | Path) -> PreparedPaper:
    """Use the same clean extraction environment for synchronous approval reads."""

    try:
        result = subprocess.run(
            _command(pdf_path, output_dir), cwd=_SKILL_ROOT,
            stdin=subprocess.DEVNULL, capture_output=True, check=False,
        )
    except OSError as exc:
        raise PaperSourceError("source_read_failed", str(exc)) from exc
    return _read_result(result.stdout, result.stderr, result.returncode)


async def prepare_pdf_async(pdf_path: str | Path, output_dir: str | Path) -> PreparedPaper:
    """Prepare one source without inheriting host PDF-library globals."""

    try:
        process = await asyncio.create_subprocess_exec(
            *_command(pdf_path, output_dir), cwd=_SKILL_ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise PaperSourceError("source_read_failed", str(exc)) from exc
    try:
        stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        # wait() alone can stall on a paused, unread result pipe after exit.
        await process.communicate()
        raise
    return _read_result(stdout, stderr, process.returncode)


def _complete_pdf_visuals(
    paper: PreparedPaper,
    pdf_path: str | Path,
    output_dir: str | Path,
) -> tuple[PreparedPaper, tuple[str, ...]]:
    """Complete captions and add typed layout carriers after native extraction."""

    layout_pages = _layout_pages(paper)
    try:
        # Importing this reader changes process-global PDF text settings. Keep it
        # at the end of the existing isolated worker, after every native crop.
        import pymupdf4llm

        pymupdf4llm.use_layout(True)
        layout = json.loads(
            pymupdf4llm.to_json(
                str(pdf_path),
                pages=layout_pages,
                use_ocr=False,
                force_ocr=False,
                force_text=True,
                write_images=False,
                embed_images=False,
                show_progress=False,
            )
        )
        pages = {page["page_number"]: page for page in layout["pages"]}
        captions = [figure["caption"] for figure in paper.figures]
        for index, figure in enumerate(paper.figures):
            anchor = figure["caption_bbox"]
            center_x = (anchor[0] + anchor[2]) / 2
            center_y = (anchor[1] + anchor[3]) / 2
            candidates: list[tuple[float, str]] = []
            for box in pages.get(figure["page"], {}).get("boxes", []):
                if box.get("boxclass") != "caption":
                    continue
                # Quad corrections can slightly change glyph bounds between the
                # readers; the native anchor center still identifies its caption.
                if not (
                    box["x0"] <= center_x <= box["x1"]
                    and box["y0"] <= center_y <= box["y1"]
                ):
                    continue
                text = " ".join(
                    str(span.get("text") or "")
                    for line in box.get("textlines") or []
                    for span in line.get("spans") or []
                )
                match = _parse_carrier_label(" ".join(text.split()), "Figure")
                if match is None or match[0] != figure["figure_number"]:
                    continue
                caption = match[1]
                if caption:
                    area = (box["x1"] - box["x0"]) * (box["y1"] - box["y0"])
                    candidates.append((area, caption))
            if candidates:
                captions[index] = min(candidates, key=lambda item: item[0])[1]
    except ImportError as exc:
        return paper, (
            (
                f"PDF figure caption and visual-carrier enrichment is unavailable: {exc}. "
                "Native captions, source text, and figure assets were retained; check "
                "the pdf-reading capability."
            ),
        )
    except Exception as exc:  # noqa: BLE001 -- auxiliary captions must not block native PDF use
        return paper, (
            (
                f"PDF figure caption and visual-carrier enrichment failed: {exc}. "
                "Native captions, source text, and figure assets were retained."
            ),
        )
    for figure, caption in zip(paper.figures, captions, strict=True):
        figure["caption"] = caption
    carriers = _extract_layout_visual_carriers(
        pdf_path,
        output_dir,
        layout=layout,
        source_text=paper.text,
    )
    return replace(paper, figures=(*paper.figures, *carriers)), ()


def _layout_pages(paper: PreparedPaper) -> list[int]:
    """Return only pages needed by existing visual anchors."""

    pages = {int(figure["page"]) - 1 for figure in paper.figures}
    current_page = 0
    for line in paper.text.splitlines():
        if line.startswith("[Page ") and line.endswith("]"):
            try:
                current_page = int(line[6:-1]) - 1
            except ValueError:
                continue
        elif _parse_carrier_label(line, "Table") or _parse_carrier_label(line, "Algorithm"):
            pages.add(current_page)
    return sorted(page for page in pages if 0 <= page < paper.page_count)


def _main() -> None:
    from .paper_extraction import prepare_pdf as prepare_in_process

    # Library diagnostics belong on stderr; stdout carries this one result.
    with redirect_stdout(sys.stderr):
        try:
            paper = prepare_in_process(sys.argv[1], sys.argv[2])
            paper, warnings = _complete_pdf_visuals(paper, sys.argv[1], sys.argv[2])
            paper, equation_warnings = enrich_structured_equations(paper)
            paper = replace(
                paper,
                warnings=(*paper.warnings, *warnings, *equation_warnings),
            )
            result = {"paper": asdict(paper)}
        except PaperSourceError as exc:
            result = {"error": {"code": exc.code, "message": str(exc)}}
    print(json.dumps(result))


if __name__ == "__main__":
    _main()
