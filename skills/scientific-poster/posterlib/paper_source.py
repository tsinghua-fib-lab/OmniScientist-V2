"""Prepare a grounded paper source and reusable figures from one local PDF."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capability import pymupdf_supported

PDF_CAPABILITY = "pdf-reading"
_CAPTION_PATTERN = re.compile(
    r"^\s*(?:figure|fig\.?|\u56fe)\s*(\d+)\s*[.:\uff1a]?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)
_MAX_PDF_BYTES = 256 * 1024 * 1024
_MAX_PAGES = 500


class PaperSourceError(RuntimeError):
    """A PDF cannot be converted into a trustworthy poster source."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedPaper:
    """Text, metadata, and caption-grounded figure assets from a paper."""

    text: str
    title: str
    authors: str
    page_count: int
    figures: tuple[dict[str, Any], ...]


def prepare_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    max_figures: int = 8,
) -> PreparedPaper:
    """Extract page-aware text and a bounded set of captioned figure crops."""

    try:
        import pymupdf
    except ImportError as exc:
        raise PaperSourceError(
            "missing_capability",
            "PDF ingestion requires the optional pymupdf package.",
        ) from exc
    if not pymupdf_supported(pymupdf):
        raise PaperSourceError(
            "missing_capability",
            "PDF ingestion requires pymupdf>=1.24.",
        )

    source = Path(pdf_path).expanduser()
    _validate_pdf_file(source)
    destination = Path(output_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)

    try:
        document = pymupdf.open(source)
    except Exception as exc:  # PyMuPDF raises several format-specific errors
        raise PaperSourceError("source_read_failed", f"Could not open PDF: {exc}") from exc

    try:
        if document.page_count > _MAX_PAGES:
            raise PaperSourceError(
                "source_too_large",
                f"PDF has {document.page_count} pages; the limit is {_MAX_PAGES}.",
            )
        metadata = document.metadata or {}
        pages: list[str] = []
        figures: list[dict[str, Any]] = []
        for page_index, page in enumerate(document):
            page_text = page.get_text("text", sort=True).strip()
            if page_text:
                pages.append(f"[Page {page_index + 1}]\n{page_text}")
            if len(figures) < max_figures:
                figures.extend(
                    _extract_captioned_figures(
                        page,
                        destination,
                        page_index=page_index,
                        remaining=max_figures - len(figures),
                        pymupdf=pymupdf,
                    )
                )
        text = "\n\n".join(pages).strip()
        if not text:
            raise PaperSourceError(
                "source_read_failed",
                "The PDF contains no extractable text; OCR is not provided by this Skill.",
            )
        for figure in figures:
            figure["context"] = _figure_context(text, int(figure["figure_number"]))
        return PreparedPaper(
            text=text,
            title=str(metadata.get("title") or "").strip(),
            authors=str(metadata.get("author") or "").strip(),
            page_count=document.page_count,
            figures=tuple(figures),
        )
    except PaperSourceError:
        raise
    except Exception as exc:
        raise PaperSourceError(
            "source_read_failed",
            f"PDF extraction failed: {exc}",
        ) from exc
    finally:
        document.close()


def _validate_pdf_file(path: Path) -> None:
    if not path.is_file():
        raise PaperSourceError("source_not_found", f"PDF was not found: {path}")
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            signature = handle.read(5)
    except OSError as exc:
        raise PaperSourceError("source_read_failed", f"Could not read PDF: {exc}") from exc
    if size > _MAX_PDF_BYTES:
        raise PaperSourceError(
            "source_too_large",
            f"PDF is larger than {_MAX_PDF_BYTES // (1024 * 1024)} MiB.",
        )
    if signature != b"%PDF-":
        raise PaperSourceError("source_read_failed", "The supplied file is not a PDF document.")


def _extract_captioned_figures(
    page: Any,
    output_dir: Path,
    *,
    page_index: int,
    remaining: int,
    pymupdf: Any,
) -> list[dict[str, Any]]:
    captions = _caption_blocks(page, pymupdf)
    extracted: list[dict[str, Any]] = []
    for caption in captions[:remaining]:
        region = _figure_region(page, caption["rect"], pymupdf)
        if region is None:
            continue
        number = int(caption["figure_number"])
        output_path = output_dir / f"figure-{number}-page-{page_index + 1}.png"
        try:
            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(200 / 72, 200 / 72),
                clip=region,
                alpha=False,
            )
            pixmap.save(output_path)
        except Exception:
            continue
        extracted.append(
            {
                "path": str(output_path),
                "page": page_index + 1,
                "figure_number": number,
                "caption": caption["caption"],
                "width": pixmap.width,
                "height": pixmap.height,
                "source": "caption-anchored",
                "crop_bbox": [round(value, 2) for value in tuple(region)],
                "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
            }
        )
    return extracted


def _caption_blocks(page: Any, pymupdf: Any) -> list[dict[str, Any]]:
    captions: list[dict[str, Any]] = []
    try:
        blocks = page.get_text("blocks", sort=True)
    except Exception:
        return captions
    for block in blocks:
        if len(block) < 7 or block[6] != 0:
            continue
        text = re.sub(r"\s+", " ", str(block[4])).strip()
        match = _CAPTION_PATTERN.match(text)
        if match is None:
            continue
        caption = match.group(2).strip()
        captions.append(
            {
                "figure_number": int(match.group(1)),
                "caption": caption[:600],
                "rect": pymupdf.Rect(block[:4]),
            }
        )
    return captions


def _figure_region(page: Any, caption: Any, pymupdf: Any) -> Any | None:
    page_rect = page.rect
    column = _column_bounds(page_rect, caption, pymupdf)
    search = pymupdf.Rect(
        column.x0,
        max(page_rect.y0, caption.y0 - 500),
        column.x1,
        caption.y0 - 3,
    )
    if search.is_empty or search.height < 60:
        return None

    graphic_candidates: list[Any] = []
    label_candidates: list[Any] = []
    try:
        for image in page.get_image_info(xrefs=True):
            rect = pymupdf.Rect(image["bbox"])
            if rect.width >= 12 and rect.height >= 12 and rect.intersects(search):
                graphic_candidates.append(rect & search)
    except Exception:
        pass
    try:
        for drawing in page.get_drawings():
            rect = pymupdf.Rect(drawing["rect"])
            if rect.width < 3 or rect.height < 3:
                continue
            if rect.get_area() > page_rect.get_area() * 0.6:
                continue
            if rect.intersects(search):
                graphic_candidates.append(rect & search)
    except Exception:
        pass
    try:
        for block in page.get_text("blocks", sort=True):
            if len(block) < 7 or block[6] != 0:
                continue
            text = re.sub(r"\s+", " ", str(block[4])).strip()
            rect = pymupdf.Rect(block[:4])
            if not text or len(text) > 80 or _CAPTION_PATTERN.match(text):
                continue
            if rect.intersects(search):
                label_candidates.append(rect & search)
    except Exception:
        pass

    clusters = _cluster_rectangles(
        [*graphic_candidates, *label_candidates],
        pymupdf,
        gap=22,
    )
    valid = [
        rect
        for rect in clusters
        if rect.width >= 60
        and rect.height >= 60
        and rect.y1 <= caption.y0 + 2
        and rect.get_area() <= page_rect.get_area() * 0.6
        and any(rect.intersects(graphic) for graphic in graphic_candidates)
    ]
    if not valid:
        return None
    best = min(valid, key=lambda rect: caption.y0 - rect.y1)
    padded = pymupdf.Rect(best.x0 - 6, best.y0 - 6, best.x1 + 6, best.y1 + 6)
    return padded & page_rect


def _column_bounds(page_rect: Any, caption: Any, pymupdf: Any) -> Any:
    midpoint = page_rect.x0 + page_rect.width / 2
    if caption.width >= page_rect.width * 0.68:
        return pymupdf.Rect(page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1)
    if caption.x1 <= midpoint + 12:
        return pymupdf.Rect(page_rect.x0, page_rect.y0, midpoint, page_rect.y1)
    if caption.x0 >= midpoint - 12:
        return pymupdf.Rect(midpoint, page_rect.y0, page_rect.x1, page_rect.y1)
    return pymupdf.Rect(page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1)


def _cluster_rectangles(rectangles: list[Any], pymupdf: Any, *, gap: float) -> list[Any]:
    pending = [pymupdf.Rect(rect) for rect in rectangles if not rect.is_empty]
    clusters: list[Any] = []
    while pending:
        cluster = pending.pop()
        changed = True
        while changed:
            changed = False
            envelope = pymupdf.Rect(
                cluster.x0 - gap,
                cluster.y0 - gap,
                cluster.x1 + gap,
                cluster.y1 + gap,
            )
            keep: list[Any] = []
            for rect in pending:
                if envelope.intersects(rect):
                    cluster |= rect
                    changed = True
                else:
                    keep.append(rect)
            pending = keep
        clusters.append(cluster)
    return clusters


def _figure_context(text: str, figure_number: int) -> str:
    reference = re.compile(
        rf"(?:figure|fig\.?)\s*{figure_number}(?!\d)",
        re.IGNORECASE,
    )
    for paragraph in re.split(r"\n\s*\n", text):
        compact = re.sub(r"\s+", " ", paragraph).strip()
        if not compact or _CAPTION_PATTERN.match(compact):
            continue
        match = reference.search(compact)
        if match is None:
            continue
        start = max(0, match.start() - 220)
        end = min(len(compact), match.end() + 320)
        return compact[start:end]
    return ""


__all__ = [
    "PDF_CAPABILITY",
    "PaperSourceError",
    "PreparedPaper",
    "prepare_pdf",
]
