"""PDF parsing with PyMuPDF + pymupdf4llm — fast, CPU-only, no GPU needed.

Region-based figure extraction pipeline:
  Phase 1 — Position-aware image bbox detection via get_image_info()
  Phase 2 — Spatial clustering of nearby images into logical figure regions
  Phase 3 — Region rendering via get_pixmap(clip=...) — captures both raster and vector content
  Phase 4 — Spatial caption association + in-text reference extraction
  Phase 5 — Vector figure fallback for captioned figures with no raster on their page
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Section detection patterns ───────────────────────────

SECTION_PATTERNS = {
    "abstract": re.compile(r"^\s*(Abstract|ABSTRACT)\b", re.MULTILINE),
    "introduction": re.compile(
        r"^\s*\d*\.?\s*(Introduction|INTRODUCTION)\b", re.MULTILINE,
    ),
    "methods": re.compile(
        r"^\s*\d*\.?\s*(Method|METHOD|Approach|APPROACH|Experiment)",
        re.MULTILINE | re.IGNORECASE,
    ),
    "results": re.compile(
        r"^\s*\d*\.?\s*(Result|RESULT|Evaluation|EVALUATION)",
        re.MULTILINE | re.IGNORECASE,
    ),
    "discussion": re.compile(
        r"^\s*\d*\.?\s*(Discussion|DISCUSSION)", re.MULTILINE,
    ),
    "conclusion": re.compile(
        r"^\s*\d*\.?\s*(Conclusion|CONCLUSION|Summary|SUMMARY)", re.MULTILINE,
    ),
    "references": re.compile(
        r"^\s*(References|REFERENCES|Bibliography)\b", re.MULTILINE,
    ),
}


# ── Section pre-screening ────────────────────────────────


def _prescreen_sections_sync(pdf_path: str) -> tuple[dict[str, int], int]:
    """Fast pre-screen to locate section start pages (0-indexed)."""
    import pymupdf

    try:
        doc = pymupdf.open(pdf_path)
        total_pages = len(doc)
        sections: dict[str, int] = {}

        for page_num in range(total_pages):
            page_text = doc[page_num].get_text()
            for name, pat in SECTION_PATTERNS.items():
                if name not in sections and pat.search(page_text):
                    sections[name] = page_num

        doc.close()
        return sections, total_pages
    except Exception:
        logger.debug("PyMuPDF pre-screen failed", exc_info=True)
        return {}, 0


async def prescreen_sections(pdf_path: str) -> tuple[dict[str, int], int]:
    return await asyncio.to_thread(_prescreen_sections_sync, pdf_path)


# ── Page range utilities ─────────────────────────────────


def build_page_range(
    sections: dict[str, int],
    total_pages: int,
    talk_type: str = "conference",
) -> str | None:
    if not sections or total_pages <= 6:
        return None

    needed: set[int] = set()

    if talk_type == "conference":
        needed.update(range(0, min(2, total_pages)))
        if "introduction" in sections:
            needed.add(sections["introduction"])
        if "results" in sections:
            end = sections.get(
                "discussion", sections.get("conclusion", total_pages),
            )
            needed.update(range(sections["results"], min(end, total_pages)))
        if "conclusion" in sections:
            end = sections.get("references", total_pages)
            needed.update(range(sections["conclusion"], min(end, total_pages)))
    elif talk_type in ("seminar", "group_meeting", "defense"):
        ref_start = sections.get("references", total_pages)
        needed.update(range(0, ref_start))
    else:
        return None

    if not needed:
        return None

    sorted_pages = sorted(needed)
    ranges: list[str] = []
    start = prev = sorted_pages[0]
    for p in sorted_pages[1:]:
        if p == prev + 1:
            prev = p
        else:
            ranges.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = p
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(ranges)


def _page_range_to_list(page_range: str | None) -> list[int] | None:
    if not page_range:
        return None
    pages: list[int] = []
    for part in page_range.split(","):
        part = part.strip()
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            pages.extend(range(int(start_s), int(end_s) + 1))
        else:
            pages.append(int(part))
    return sorted(set(pages))


# ── Main parse function (pymupdf4llm) ────────────────────


def _parse_pdf_sync(
    pdf_path: str,
    page_range: str | None = None,
    output_dir: str | None = None,
    prescreen: bool = False,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Parse PDF to markdown + optional metadata + optional prescreen.

    When ``prescreen=True``, the returned metadata dict additionally contains:
      - ``section_pages``: dict[section_name, page_idx]
      - ``total_pages``: total page count (pre-filter)
    """
    import pymupdf
    import pymupdf4llm

    pages = _page_range_to_list(page_range)

    kwargs: dict[str, Any] = {}
    if pages is not None:
        kwargs["pages"] = pages

    try:
        md_text = pymupdf4llm.to_markdown(pdf_path, **kwargs)
    except Exception:
        logger.warning("pymupdf4llm failed, falling back to raw pymupdf", exc_info=True)
        md_text = _pymupdf_raw_text_fallback(pdf_path, pages)

    doc = pymupdf.open(pdf_path)
    raw_meta = doc.metadata or {}
    metadata: dict[str, Any] = {
        "title": raw_meta.get("title", ""),
        "author": raw_meta.get("author", ""),
        "subject": raw_meta.get("subject", ""),
        "creator": raw_meta.get("creator", ""),
        "page_count": len(doc),
        "parsed_pages": len(pages) if pages else len(doc),
    }

    # inline prescreen within the same doc handle
    if prescreen:
        section_pages: dict[str, int] = {}
        for page_num in range(len(doc)):
            page_text = doc[page_num].get_text()
            for name, pat in SECTION_PATTERNS.items():
                if name not in section_pages and pat.search(page_text):
                    section_pages[name] = page_num
        metadata["section_pages"] = section_pages
        metadata["total_pages"] = len(doc)

    images_dict: dict[str, Any] = {}
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        images_dict = _extract_images_from_doc(doc, output_dir, pages)

    doc.close()

    logger.info(
        "[pdf-parser] Parsed %s: %d chars, %d images, pages=%s, prescreen=%s",
        Path(pdf_path).name, len(md_text), len(images_dict),
        page_range or "all", prescreen,
    )

    return md_text, metadata, images_dict


async def parse_pdf(
    pdf_path: str,
    page_range: str | None = None,
    output_dir: str | None = None,
    prescreen: bool = False,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return await asyncio.to_thread(
        _parse_pdf_sync, pdf_path, page_range, output_dir, prescreen,
    )


def _pymupdf_raw_text_fallback(pdf_path: str, pages: list[int] | None) -> str:
    import pymupdf

    doc = pymupdf.open(pdf_path)
    parts: list[str] = []

    target_pages = pages if pages is not None else range(len(doc))
    for page_num in target_pages:
        if page_num >= len(doc):
            continue
        page = doc[page_num]
        blocks = page.get_text("dict", flags=11)["blocks"]
        for block in blocks:
            if block["type"] != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s["text"] for s in spans).strip()
                if not text:
                    continue
                max_size = max(s["size"] for s in spans)
                if max_size >= 14:
                    parts.append(f"\n## {text}\n")
                else:
                    parts.append(text)

    doc.close()
    return "\n".join(parts)


def _extract_images_from_doc(
    doc: Any,
    output_dir: str,
    pages: list[int] | None = None,
) -> dict[str, str]:
    MIN_WIDTH = 200
    MIN_HEIGHT = 150
    MIN_BYTES = 5_000

    seen_xrefs: set[int] = set()
    saved: dict[str, str] = {}
    img_counter = 0

    target_pages = pages if pages is not None else range(len(doc))

    for page_num in target_pages:
        if page_num >= len(doc):
            continue
        page = doc[page_num]

        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)

            try:
                base_image = doc.extract_image(xref)
                if not base_image:
                    continue

                width = base_image.get("width", 0)
                height = base_image.get("height", 0)
                image_bytes = base_image.get("image", b"")
                ext = base_image.get("ext", "png")

                if width < MIN_WIDTH or height < MIN_HEIGHT:
                    continue
                if len(image_bytes) < MIN_BYTES:
                    continue

                filename = f"img_{img_counter:03d}_p{page_num}.{ext}"
                filepath = os.path.join(output_dir, filename)
                with open(filepath, "wb") as f:
                    f.write(image_bytes)

                saved[filename] = filepath
                img_counter += 1
            except Exception:
                logger.debug("Failed to extract image xref=%d", xref, exc_info=True)

    return saved


# ── Legacy figure extraction (kept for non-PDF sources) ──


def _extract_figures_sync(pdf_path: str, output_dir: str) -> list[str]:
    import pymupdf

    os.makedirs(output_dir, exist_ok=True)
    try:
        doc = pymupdf.open(pdf_path)
        saved = _extract_images_from_doc(doc, output_dir)
        doc.close()
        return list(saved.values())
    except Exception:
        logger.debug("pymupdf image extraction failed", exc_info=True)
        return []


async def extract_figures_parallel(pdf_path: str, output_dir: str) -> list[str]:
    return await asyncio.to_thread(_extract_figures_sync, pdf_path, output_dir)


_MIN_FIGURE_WIDTH = 200
_MIN_FIGURE_HEIGHT = 150
_MIN_FIGURE_BYTES = 5_000


def filter_figures(
    paths: list[str],
    min_width: int = _MIN_FIGURE_WIDTH,
    min_height: int = _MIN_FIGURE_HEIGHT,
    min_bytes: int = _MIN_FIGURE_BYTES,
) -> list[str]:
    try:
        from PIL import Image
    except ImportError:
        return paths

    kept: list[str] = []
    for p in paths:
        try:
            if os.path.getsize(p) < min_bytes:
                continue
            with Image.open(p) as img:
                w, h = img.size
                if w < min_width or h < min_height:
                    continue
            kept.append(p)
        except Exception:
            pass
    return kept


# ── Content structuring ──────────────────────────────────


def split_markdown_sections(markdown: str) -> dict[str, str]:
    parts = re.split(r"^(#{1,3}\s+.+)$", markdown, flags=re.MULTILINE)
    current = "preamble"
    sections: dict[str, str] = {}
    for part in parts:
        part = part.strip()
        if re.match(r"^#{1,3}\s+", part):
            current = part.lstrip("#").strip()
        else:
            if part:
                sections[current] = sections.get(current, "") + "\n" + part
    return sections


def _extract_table_captions_from_markdown(
    markdown: str,
) -> list[dict[str, Any]]:
    """Parse table captions from markdown text (multi-line aware)."""
    results: list[dict[str, Any]] = []
    seen: set[int] = set()

    matches = list(_TABLE_CAPTION_HEAD_RE.finditer(markdown))
    for i, m in enumerate(matches):
        try:
            tab_num = int(m.group(1))
        except ValueError:
            continue
        if tab_num in seen:
            continue

        body_start = m.end()
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        search_end = min(body_start + 800, next_start, len(markdown))
        window = markdown[body_start:search_end]

        end_markers = [
            re.search(r"\n\s*\n", window),
            re.search(r"\n\s*#{1,6}\s+", window),
            re.search(r"\n\s*(?:Figure|Fig|\u56fe)\s*\d+\s*[.:\uFF1A]", window, re.IGNORECASE),
        ]
        end_positions = [mm.start() for mm in end_markers if mm]
        end_pos = min(end_positions) if end_positions else len(window)

        caption_body = re.sub(r"\s+", " ", window[:end_pos].strip())
        if len(caption_body) > 400:
            caption_body = caption_body[:397] + "..."
        if not caption_body:
            continue

        seen.add(tab_num)
        results.append({"table_num": tab_num, "caption": caption_body})

    results.sort(key=lambda x: x["table_num"])
    return results


def _extract_pipe_tables(markdown: str) -> list[dict[str, Any]]:
    """Extract markdown pipe tables with structured rows/headers."""
    results: list[dict[str, Any]] = []
    table_pat = re.compile(r"((?:^\|.+\|\s*$\n?)+)", re.MULTILINE)

    for m in table_pat.finditer(markdown):
        block = m.group(1).strip()
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue

        # Parse header
        header_line = lines[0]
        headers = [
            c.strip() for c in header_line.strip("|").split("|")
        ]
        # Skip separator line (|---|---|)
        data_start = 1
        if len(lines) > 1 and re.match(r"^\|[\s\-:|]+\|?\s*$", lines[1]):
            data_start = 2

        rows: list[list[str]] = []
        for ln in lines[data_start:]:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) == len(headers) and any(c for c in cells):
                rows.append(cells)

        if not headers or not rows:
            continue

        # Locate caption: search 200 chars before this table's start
        ctx_start = max(0, m.start() - 250)
        ctx = markdown[ctx_start:m.start()]
        caption = ""
        cap_match = list(_TABLE_CAPTION_HEAD_RE.finditer(ctx))
        if cap_match:
            cap_pos = cap_match[-1].end()
            cap_body = ctx[cap_pos:].strip()
            cap_body = re.sub(r"\s+", " ", cap_body)
            if cap_body:
                caption = cap_body[:300]

        results.append({
            "headers": headers,
            "rows": rows,
            "caption": caption,
            "raw_markdown": block,
            "source": "markdown_pipe",
        })

    return results


def _extract_tables_from_pdf_layout(
    pdf_path: str,
    pages: list[int] | None = None,
    max_tables: int = 20,
) -> list[dict[str, Any]]:
    """Extract tables using PyMuPDF's find_tables() (layout-based detection).

    This catches tables that pymupdf4llm doesn't render as pipe tables —
    which is the majority of scientific PDF tables.
    """
    import pymupdf

    results: list[dict[str, Any]] = []

    try:
        doc = pymupdf.open(pdf_path)
        target_pages = pages if pages is not None else list(range(len(doc)))

        for page_num in target_pages:
            if page_num >= len(doc):
                continue
            if len(results) >= max_tables:
                break

            page = doc[page_num]
            try:
                # find_tables added in PyMuPDF 1.23.0
                tables_finder = page.find_tables()
                tables_list = (
                    tables_finder.tables
                    if hasattr(tables_finder, "tables")
                    else list(tables_finder)
                )
            except (AttributeError, Exception):
                continue

            for tbl in tables_list:
                try:
                    extracted = tbl.extract()  # list of list of str|None
                except Exception:
                    continue
                if not extracted or len(extracted) < 2:
                    continue

                # Clean cells
                cleaned: list[list[str]] = []
                for row in extracted:
                    cleaned_row = [
                        str(c).strip() if c is not None else ""
                        for c in row
                    ]
                    if any(cleaned_row):  # skip fully empty rows
                        cleaned.append(cleaned_row)

                if len(cleaned) < 2:
                    continue

                # Heuristic: first row is header
                headers = cleaned[0]
                rows = cleaned[1:]

                # Filter: need ≥2 cols, ≥1 data row, ≥1 non-empty header
                if len(headers) < 2 or not rows:
                    continue
                if not any(h.strip() for h in headers):
                    continue

                # Search for nearby caption on the page
                caption = ""
                try:
                    tbl_bbox = pymupdf.Rect(tbl.bbox)
                    text_dict = page.get_text("dict", flags=11)
                    for block in text_dict.get("blocks", []):
                        if block.get("type") != 0:
                            continue
                        bx0, by0, bx1, by1 = block["bbox"]
                        # Caption usually above the table
                        if by1 < tbl_bbox.y0 and by1 > tbl_bbox.y0 - 50:
                            block_text = " ".join(
                                span.get("text", "")
                                for line in block.get("lines", [])
                                for span in line.get("spans", [])
                            ).strip()
                            block_text = re.sub(r"\s+", " ", block_text)
                            if _TABLE_CAPTION_HEAD_RE.search(" " + block_text):
                                caption = block_text[:300]
                                break
                except Exception:
                    pass

                results.append({
                    "headers": headers,
                    "rows": rows,
                    "caption": caption,
                    "page_num": page_num,
                    "source": "pdf_layout",
                })

                if len(results) >= max_tables:
                    break

        doc.close()
    except Exception:
        logger.debug("PDF layout table extraction failed", exc_info=True)

    return results


def extract_tables_enriched(
    pdf_path: str | None,
    markdown: str,
    pages: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Combine pipe-table extraction (from markdown) with PyMuPDF layout
    detection (from PDF). Deduplicates by content similarity.

    Returns enriched list:
      [{headers, rows, caption, source, page_num?}, ...]
    """
    pipe_tables = _extract_pipe_tables(markdown)

    layout_tables: list[dict[str, Any]] = []
    if pdf_path:
        layout_tables = _extract_tables_from_pdf_layout(pdf_path, pages)

    # If no captions found in tables, try to associate from markdown captions
    md_captions = _extract_table_captions_from_markdown(markdown)

    all_tables = pipe_tables + layout_tables

    # Cap dimensions for slide-friendliness
    for t in all_tables:
        # Limit columns to 5 (more won't fit)
        if len(t["headers"]) > 5:
            t["headers"] = t["headers"][:5]
            t["rows"] = [r[:5] for r in t["rows"]]
        # Limit rows to 8
        if len(t["rows"]) > 8:
            t["rows"] = t["rows"][:8]
            t["truncated"] = True

    # Deduplicate by header signature
    seen_sigs: set[str] = set()
    unique: list[dict[str, Any]] = []
    for t in all_tables:
        sig = "|".join(h.lower().strip() for h in t["headers"])[:200]
        if sig in seen_sigs or len(sig) < 3:
            continue
        seen_sigs.add(sig)
        unique.append(t)

    # Backfill captions from markdown if missing
    for i, t in enumerate(unique):
        if not t.get("caption") and i < len(md_captions):
            t["caption"] = md_captions[i]["caption"]

    logger.info(
        "[pdf-parser] Tables extracted: %d pipe + %d layout = %d unique",
        len(pipe_tables), len(layout_tables), len(unique),
    )

    return unique



def extract_equations_from_markdown(markdown: str) -> list[str]:
    return re.findall(r"\$\$(.+?)\$\$", markdown, re.DOTALL)


# ══════════════════════════════════════════════════════════
# Region-Based Figure Extraction Pipeline
# ══════════════════════════════════════════════════════════

# Caption header: matches only the "Figure N." / "Fig. N:" prefix.
# Actual caption body is extracted separately to allow multi-line text.
_FIG_CAPTION_HEAD_RE = re.compile(
    r"(?:^|\n)\s*(?:Figure|Fig\.?|\u56fe)\s*(\d+)\s*[.:\uFF1A]\s*",
    re.IGNORECASE,
)

# Legacy — kept for _find_caption_near_region to test "does this block
# start with a figure caption header?"
_FIG_CAPTION_RE = re.compile(
    r"(?:Figure|Fig\.?|\u56fe)\s*(\d+)\s*[.:\uFF1A]\s*(.+?)(?:\n\s*\n|$)",
    re.IGNORECASE | re.DOTALL,
)

_FIG_REF_RE = re.compile(
    r"(?:Figure|Fig\.?|\u56fe)\s*\.?\s*(\d+)",
    re.IGNORECASE,
)

_TABLE_CAPTION_HEAD_RE = re.compile(
    r"(?:^|\n)\s*(?:Table|Tab\.?|\u8868)\s*(\d+)\s*[.:\uFF1A]\s*",
    re.IGNORECASE,
)


# ── Phase 1: Position-aware image bbox detection ─────────


def _get_image_bboxes_on_page(
    page: Any,
    min_display_pt: float = 50.0,
    min_pixel: int = 80,
) -> list[dict[str, Any]]:
    """Get positioned bounding boxes of images on a page.

    Uses ``get_image_info(xrefs=True)`` which returns the display bbox
    (after transformation) for each image instance on the page.

    Returns list of ``{rect, xref, width_px, height_px}``.
    Relaxed thresholds allow sub-panels of composite figures to be captured;
    filtering is done AFTER clustering.
    """
    import pymupdf

    results: list[dict[str, Any]] = []
    try:
        infos = page.get_image_info(xrefs=True)
    except (AttributeError, Exception):
        # PyMuPDF < 1.18.11 — fall back handled by caller
        return results

    for info in infos:
        bbox = info.get("bbox")
        if not bbox:
            continue
        rect = pymupdf.Rect(bbox)
        # Skip images that are extremely tiny on the page (icons, bullets)
        if rect.width < min_display_pt or rect.height < min_display_pt:
            continue
        w_px = info.get("width", 0)
        h_px = info.get("height", 0)
        # Skip images with very low pixel resolution
        if w_px < min_pixel or h_px < min_pixel:
            continue
        results.append({
            "rect": rect,
            "xref": info.get("xref", 0),
            "width_px": w_px,
            "height_px": h_px,
        })
    return results


# ── Phase 2: Spatial clustering ──────────────────────────


def _cluster_nearby_rects(
    rects: list[Any],
    distance_pt: float = 25.0,
) -> list[dict[str, Any]]:
    """Cluster nearby pymupdf.Rect objects using union-find.

    *distance_pt* controls how close two rects must be (in points,
    1 inch = 72 pt) to be merged into the same cluster.  25 pt ≈ 0.35 inch
    is enough to catch composite-figure sub-panels while separating
    independent figures on the same page.

    Returns list of ``{bbox, element_count, member_rects}``.
    """
    import pymupdf

    if not rects:
        return []

    n = len(rects)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Merge rects whose expanded envelopes intersect
    for i in range(n):
        ri = rects[i]
        expanded_i = pymupdf.Rect(
            ri.x0 - distance_pt, ri.y0 - distance_pt,
            ri.x1 + distance_pt, ri.y1 + distance_pt,
        )
        for j in range(i + 1, n):
            if expanded_i.intersects(rects[j]):
                union(i, j)

    # Group by root
    groups: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)

    # Compute bounding box for each group
    result: list[dict[str, Any]] = []
    for indices in groups.values():
        bbox = pymupdf.Rect(rects[indices[0]])
        members = [pymupdf.Rect(rects[indices[0]])]
        for idx in indices[1:]:
            bbox |= rects[idx]
            members.append(pymupdf.Rect(rects[idx]))
        result.append({
            "bbox": bbox,
            "element_count": len(indices),
            "member_rects": members,
        })

    return result


def _is_valid_figure_region(
    cluster: dict[str, Any],
    page_rect: Any,
    page: Any = None,
    min_region_pt: float = 100.0,
    max_page_ratio: float = 0.75,
    max_text_overlap_ratio: float = 0.35,
) -> bool:
    """Filter out clusters that are too small, cover the entire page,
    or overlap heavily with body text (indicating mis-identified text blocks)."""
    bbox = cluster["bbox"]
    if bbox.width < min_region_pt or bbox.height < min_region_pt:
        return False
    page_area = page_rect.width * page_rect.height
    if page_area <= 0:
        return False
    region_area = bbox.width * bbox.height
    if region_area / page_area > max_page_ratio:
        return False

    # reject if the region is dominated by body text
    if page is not None:
        text_overlap = _compute_text_overlap_ratio(page, bbox)
        if text_overlap > max_text_overlap_ratio:
            return False

    # reject absurdly thin regions (likely underlines or separators)
    aspect = bbox.width / max(bbox.height, 1.0)
    if aspect > 15 or aspect < 0.07:
        return False

    return True

def _compute_text_overlap_ratio(page: Any, region_rect: Any) -> float:
    """Compute what fraction of ``region_rect`` is occupied by body text blocks.

    Used to distinguish genuine figure regions from paragraphs that happen
    to contain inline images (icons, small diagrams).
    """
    import pymupdf

    try:
        text_dict = page.get_text("dict", flags=11)
    except Exception:
        return 0.0

    region_area = region_rect.width * region_rect.height
    if region_area <= 0:
        return 0.0

    text_area_inside = 0.0
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:  # only text blocks
            continue
        bx0, by0, bx1, by1 = block["bbox"]
        block_rect = pymupdf.Rect(bx0, by0, bx1, by1)

        # Heuristic: skip short blocks (likely captions/labels, not body text)
        block_text = " ".join(
            span.get("text", "")
            for line in block.get("lines", [])
            for span in line.get("spans", [])
        ).strip()
        if len(block_text) < 40:  # short text = likely label, not body
            continue
        # Skip caption-like blocks
        if _FIG_CAPTION_RE.match(block_text):
            continue

        intersection = block_rect & region_rect
        if not intersection.is_empty:
            text_area_inside += intersection.width * intersection.height

    return text_area_inside / region_area


# ── Phase 3: Region rendering ────────────────────────────


def _render_page_region(
    page: Any,
    region_rect: Any,
    output_path: str,
    dpi: int = 200,
    padding_pt: float = 8.0,
) -> dict[str, Any] | None:
    """Render a specific rectangular region of a page to PNG.

    Captures both raster images AND vector drawings within the region.
    This is the key advantage over ``extract_image(xref)`` which only
    retrieves individual embedded raster objects.
    """
    import pymupdf

    try:
        # Add minimal padding without extending into text areas
        padded = pymupdf.Rect(
            region_rect.x0 - padding_pt,
            region_rect.y0 - padding_pt,
            region_rect.x1 + padding_pt,
            region_rect.y1 + padding_pt,
        )
        # Clip to page bounds
        padded &= page.rect

        mat = pymupdf.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, clip=padded)
        pix.save(output_path)

        return {
            "path": output_path,
            "width": pix.width,
            "height": pix.height,
            "region_rect": (padded.x0, padded.y0, padded.x1, padded.y1),
        }
    except Exception:
        logger.debug("Failed to render page region", exc_info=True)
        return None


# ── Phase 4: Caption association ─────────────────────────


def _find_caption_near_region(
    page: Any,
    region_rect: Any,
    search_distance_pt: float = 45.0,
) -> str:
    """Find a figure caption near a region by spatial proximity.

    Merges multi-line caption blocks (common in 2-column papers) and returns
    a cleaned single-line caption string.
    """
    try:
        text_dict = page.get_text("dict", flags=11)
    except Exception:
        return ""

    candidates: list[tuple[float, str]] = []

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        bx0, by0, bx1, by1 = block["bbox"]
        # Join all lines into a single normalized string
        block_text = " ".join(
            span.get("text", "")
            for line in block.get("lines", [])
            for span in line.get("spans", [])
        ).strip()
        block_text = re.sub(r"\s+", " ", block_text)

        if not block_text or not _FIG_CAPTION_HEAD_RE.search(" " + block_text):
            continue

        # Below the figure
        if by0 >= region_rect.y1 - 5 and by0 <= region_rect.y1 + search_distance_pt:
            h_overlap = min(bx1, region_rect.x1) - max(bx0, region_rect.x0)
            if h_overlap > region_rect.width * 0.3:
                dist = by0 - region_rect.y1
                candidates.append((dist, block_text))

        # Above the figure
        if by1 <= region_rect.y0 + 5 and by1 >= region_rect.y0 - search_distance_pt:
            h_overlap = min(bx1, region_rect.x1) - max(bx0, region_rect.x0)
            if h_overlap > region_rect.width * 0.3:
                dist = region_rect.y0 - by1
                candidates.append((dist, block_text))

    if not candidates:
        return ""

    candidates.sort(key=lambda c: c[0])
    caption = candidates[0][1]

    # Truncate to reasonable length (full caption is later stored in markdown-
    # parsed caption dict; this one is just the spatial-match signature)
    if len(caption) > 400:
        caption = caption[:397] + "..."
    return caption


def _extract_captions_from_markdown(
    markdown: str,
) -> list[dict[str, Any]]:
    """Parse figure captions from markdown, supporting multi-line captions.

    A caption starts with ``Figure N.`` / ``Fig N:`` (or the Chinese equivalent) and extends
    until one of:
      - a blank line (double newline)
      - a new section header (``#``, ``##``)
      - another figure/table caption
      - 500 characters (hard cap to avoid runaway)
    """
    results: list[dict[str, Any]] = []
    seen: set[int] = set()

    matches = list(_FIG_CAPTION_HEAD_RE.finditer(markdown))
    for i, m in enumerate(matches):
        try:
            fig_num = int(m.group(1))
        except ValueError:
            continue
        if fig_num in seen:
            continue

        body_start = m.end()
        # Find the end of this caption
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        # Limit search window
        search_end = min(body_start + 800, next_start, len(markdown))
        window = markdown[body_start:search_end]

        # Stop at blank line, new heading, or table caption
        end_markers = [
            re.search(r"\n\s*\n", window),
            re.search(r"\n\s*#{1,6}\s+", window),
            re.search(r"\n\s*(?:Table|\u8868)\s*\d+\s*[.:\uFF1A]", window, re.IGNORECASE),
        ]
        end_positions = [mm.start() for mm in end_markers if mm]
        end_pos = min(end_positions) if end_positions else len(window)

        caption_body = window[:end_pos].strip()
        # Collapse internal whitespace (including intra-line breaks)
        caption_body = re.sub(r"\s+", " ", caption_body)
        if len(caption_body) > 500:
            caption_body = caption_body[:497] + "..."
        if not caption_body:
            continue

        seen.add(fig_num)
        results.append({"fig_num": fig_num, "caption": caption_body})

    results.sort(key=lambda x: x["fig_num"])
    return results


# ── Phase 4b: In-text reference extraction ───────────────


def _extract_text_references_for_figure(
    markdown: str,
    fig_num: int,
    context_chars: int = 400,
    max_passages: int = 3,
) -> str:
    """Find paragraphs in the markdown that reference a figure number.

    Strategy:
      - Match whole-word references only (avoids matching ``Figure 10`` when
        looking for ``Figure 1``)
      - Use paragraph boundaries (double newline) for cleaner passages
      - Prefer passages that contain quantitative signals (%, numbers, etc.)
      - Deduplicate overlapping hits
    """
    # Whole-word regex to avoid matching "Figure 10" for fig_num=1
    ref_pattern = re.compile(
        rf"(?:Figure|Fig\.?|\u56fe)\s*\.?\s*{fig_num}(?![\d])",
        re.IGNORECASE,
    )

    matches = list(ref_pattern.finditer(markdown))
    if not matches:
        return ""

    # Split markdown into paragraphs with position offsets
    paragraphs: list[tuple[int, int, str]] = []  # (start, end, text)
    cursor = 0
    for para in re.split(r"\n\s*\n", markdown):
        start = markdown.find(para, cursor)
        if start < 0:
            continue
        end = start + len(para)
        cursor = end
        paragraphs.append((start, end, para.strip()))

    # Collect paragraphs containing references
    hit_para_indices: list[int] = []
    for m in matches:
        pos = m.start()
        for i, (ps, pe, _) in enumerate(paragraphs):
            if ps <= pos < pe:
                if i not in hit_para_indices:
                    hit_para_indices.append(i)
                break

    if not hit_para_indices:
        return ""

    # Score paragraphs: prefer ones with numbers (more informative)
    def _score(idx: int) -> float:
        text = paragraphs[idx][2]
        num_density = len(re.findall(r"\d", text)) / max(len(text), 1)
        has_percent = "%" in text
        has_pvalue = bool(re.search(r"p\s*[<>=]\s*0?\.\d", text, re.IGNORECASE))
        # Caption itself is less useful than discussion
        is_caption = bool(_FIG_CAPTION_HEAD_RE.match(" " + text[:80]))
        score = num_density * 10
        if has_percent:
            score += 2
        if has_pvalue:
            score += 3
        if is_caption:
            score -= 5
        return score

    hit_para_indices.sort(key=_score, reverse=True)
    selected = hit_para_indices[:max_passages]
    # Preserve original document order for readability
    selected.sort()

    passages: list[str] = []
    for idx in selected:
        text = paragraphs[idx][2]
        # Truncate very long paragraphs
        if len(text) > context_chars:
            # Find the reference position within the paragraph
            rel_pos = -1
            for m in ref_pattern.finditer(text):
                rel_pos = m.start()
                break
            if rel_pos >= 0:
                # Center window around reference
                half = context_chars // 2
                s = max(0, rel_pos - half)
                e = min(len(text), rel_pos + half)
                text = ("..." if s > 0 else "") + text[s:e] + ("..." if e < len(text) else "")
            else:
                text = text[:context_chars] + "..."
        passages.append(re.sub(r"\s+", " ", text).strip())

    return " [...] ".join(passages)


# ── Phase 5: Vector figure fallback ──────────────────────


def _match_caption_to_page(
    caption_fig_num: int,
    markdown: str,
    total_pages: int,
) -> int | None:
    """Estimate which page a figure caption appears on."""
    patterns = [
        f"Figure {caption_fig_num}",
        f"Fig. {caption_fig_num}",
        f"Fig {caption_fig_num}",
        f"\u56fe {caption_fig_num}",
        f"\u56fe{caption_fig_num}",
    ]

    pos = -1
    for pat in patterns:
        idx = markdown.find(pat)
        if idx >= 0:
            pos = idx
            break

    if pos < 0:
        return None

    text_len = len(markdown)
    if text_len == 0:
        return 0
    ratio = pos / text_len
    return min(int(ratio * total_pages), total_pages - 1)


def _render_page_as_figure(
    doc: Any,
    page_num: int,
    output_dir: str,
    dpi: int = 150,
) -> dict[str, Any] | None:
    """Render full page to PNG — last-resort fallback for vector figures."""
    import pymupdf

    if page_num >= len(doc):
        return None

    try:
        page = doc[page_num]
        mat = pymupdf.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        filename = f"page_render_p{page_num:03d}.png"
        filepath = os.path.join(output_dir, filename)
        pix.save(filepath)

        return {
            "path": filepath,
            "page_num": page_num,
            "width": pix.width,
            "height": pix.height,
            "source": "page_render",
            "caption": "",
            "related_text": "",
            "element_count": 0,
        }
    except Exception:
        logger.debug("Failed to render page %d", page_num, exc_info=True)
        return None


# ── Orchestrator: enriched extraction ────────────────────
def _find_caption_blocks_on_page(page: Any) -> list[dict[str, Any]]:
    """Locate figure-caption text blocks on a page using layout info.

    Returns list of {"fig_num": int, "caption": str, "bbox": pymupdf.Rect,
                     "page_num": int}.
    A caption block is a text block whose first non-whitespace tokens
    match English and Chinese variants of 'Figure N.' / 'Fig. N:' etc.
    """
    import pymupdf

    results: list[dict[str, Any]] = []
    try:
        td = page.get_text("dict", flags=11)
    except Exception:
        return results

    for block in td.get("blocks", []):
        if block.get("type") != 0:
            continue
        # Reconstruct block text (preserving line structure)
        lines_text = []
        for line in block.get("lines", []):
            line_str = "".join(
                span.get("text", "") for span in line.get("spans", [])
            ).strip()
            if line_str:
                lines_text.append(line_str)
        if not lines_text:
            continue

        first = lines_text[0]
        m = re.match(
            r"^\s*(?:Figure|Fig\.?|\u56fe)\s*(\d+)\s*[.:\uFF1A]\s*(.*)",
            first, re.IGNORECASE,
        )
        if not m:
            continue
        try:
            fig_num = int(m.group(1))
        except ValueError:
            continue

        caption_text = " ".join([m.group(2)] + lines_text[1:]).strip()
        caption_text = re.sub(r"\s+", " ", caption_text)[:500]

        bx0, by0, bx1, by1 = block["bbox"]
        results.append({
            "fig_num": fig_num,
            "caption": caption_text,
            "bbox": pymupdf.Rect(bx0, by0, bx1, by1),
            "page_num": page.number,
        })

    return results

def _find_figure_region_above_caption(
    page: Any,
    caption_bbox: Any,
    min_gap_pt: float = 4.0,
    max_search_pt: float = 500.0,
    side_tolerance_pt: float = 30.0,
) -> Any | None:
    """Walk upward from a caption bbox and merge all non-caption content
    blocks (images, drawings, sub-labels) into one figure region.

    Returns the merged pymupdf.Rect or None if nothing is found.
    """
    import pymupdf

    page_rect = page.rect

    # Caption's horizontal extent defines the column the figure lives in
    col_x0 = max(page_rect.x0, caption_bbox.x0 - side_tolerance_pt)
    col_x1 = min(page_rect.x1, caption_bbox.x1 + side_tolerance_pt)
    search_top = max(page_rect.y0, caption_bbox.y0 - max_search_pt)
    search_rect = pymupdf.Rect(
        col_x0, search_top, col_x1, caption_bbox.y0 - min_gap_pt,
    )
    if search_rect.is_empty or search_rect.height < 20:
        return None

    candidates: list[Any] = []

    # (a) Raster images within the search band
    try:
        for info in page.get_image_info(xrefs=True):
            r = pymupdf.Rect(info["bbox"])
            if not (r & search_rect).is_empty and r.height > 8:
                candidates.append(r)
    except Exception:
        pass

    # (b) Vector drawings within the search band — group small paths
    try:
        for d in page.get_drawings():
            r = pymupdf.Rect(d["rect"])
            if r.width < 3 or r.height < 3:
                continue
            # Drop full-page background rects
            if r.width * r.height > page_rect.width * page_rect.height * 0.6:
                continue
            if not (r & search_rect).is_empty:
                candidates.append(r)
    except Exception:
        pass

    # (c) Text blocks that look like axis labels / sub-panel letters
    #     (short, non-caption text inside the search band)
    try:
        td = page.get_text("dict", flags=11)
        for block in td.get("blocks", []):
            if block.get("type") != 0:
                continue
            bx0, by0, bx1, by1 = block["bbox"]
            br = pymupdf.Rect(bx0, by0, bx1, by1)
            if (br & search_rect).is_empty:
                continue
            txt = " ".join(
                s.get("text", "")
                for line in block.get("lines", [])
                for s in line.get("spans", [])
            ).strip()
            # Skip body paragraphs; keep short labels (axis ticks, "(a)", etc.)
            if len(txt) > 80:
                continue
            if _FIG_CAPTION_HEAD_RE.search(" " + txt):
                continue  # another caption — never absorb
            candidates.append(br)
    except Exception:
        pass

    if not candidates:
        return None

    # Cluster nearby candidates so we don't pick up unrelated stuff
    clusters = _cluster_nearby_rects(candidates, distance_pt=20.0)
    if not clusters:
        return None

    # Choose the cluster closest to (just above) the caption
    def _dist_to_caption(c: dict[str, Any]) -> float:
        bbox = c["bbox"]
        # Negative means cluster is above caption — that's good; we want the
        # lowest gap to the caption
        return caption_bbox.y0 - bbox.y1

    valid = [c for c in clusters if _dist_to_caption(c) >= 0]
    if not valid:
        return None
    valid.sort(key=_dist_to_caption)
    best = valid[0]

    # Sanity check: cluster shouldn't be tiny or absurdly thin
    bb = best["bbox"]
    if bb.width < 60 or bb.height < 60:
        return None
    return bb


def _extract_figures_enriched_sync(
    pdf_path: str,
    markdown_text: str,
    output_dir: str,
    pages: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Caption-first figure extraction.

    For every figure caption found on a page, walk upward and render the
    region containing all images / drawings / small labels above it. This
    handles raster, vector, and mixed figures uniformly.
    """
    import pymupdf

    os.makedirs(output_dir, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    total_pages = len(doc)
    target_pages = pages if pages is not None else list(range(total_pages))

    all_figures: list[dict[str, Any]] = []
    seen_fig_nums: set[int] = set()
    fig_counter = 0

    # ── Pass 1: caption-anchored extraction ──
    for page_num in target_pages:
        if page_num >= total_pages:
            continue
        page = doc[page_num]

        captions = _find_caption_blocks_on_page(page)
        for cap in captions:
            if cap["fig_num"] in seen_fig_nums:
                continue
            region = _find_figure_region_above_caption(page, cap["bbox"])
            if region is None:
                continue

            filename = f"fig_cap_{fig_counter:03d}_p{page_num}_f{cap['fig_num']}.png"
            filepath = os.path.join(output_dir, filename)
            render_info = _render_page_region(
                page, region, filepath, dpi=200, padding_pt=6.0,
            )
            if not render_info:
                continue

            related = _extract_text_references_for_figure(
                markdown_text, cap["fig_num"],
            )

            all_figures.append({
                "path": filepath,
                "page_num": page_num,
                "width": render_info["width"],
                "height": render_info["height"],
                "source": "caption_anchored",
                "caption": cap["caption"],
                "related_text": related,
                "element_count": 1,
                "fig_num": cap["fig_num"],
            })
            seen_fig_nums.add(cap["fig_num"])
            fig_counter += 1

    # ── Pass 2: legacy region-clustering as a SUPPLEMENT ──
    # Catches figures whose captions are missing/misformatted.
    xref_page_count: dict[int, int] = {}
    for page_num in target_pages:
        if page_num >= total_pages:
            continue
        try:
            for info in doc[page_num].get_image_info(xrefs=True):
                xref = info.get("xref", 0)
                if xref > 0:
                    xref_page_count[xref] = xref_page_count.get(xref, 0) + 1
        except Exception:
            pass
    repeating_xrefs = {x for x, c in xref_page_count.items() if c >= 3}

    pages_already_covered = {f["page_num"] for f in all_figures}

    for page_num in target_pages:
        if page_num >= total_pages or page_num in pages_already_covered:
            continue
        page = doc[page_num]
        img_bboxes = _get_image_bboxes_on_page(page)
        img_bboxes = [
            ib for ib in img_bboxes if ib["xref"] not in repeating_xrefs
        ]
        if not img_bboxes:
            continue
        rects = [ib["rect"] for ib in img_bboxes]
        clusters = _cluster_nearby_rects(rects, distance_pt=15.0)
        for cluster in clusters:
            if not _is_valid_figure_region(cluster, page.rect, page=page):
                continue
            filename = f"fig_supp_{fig_counter:03d}_p{page_num}.png"
            filepath = os.path.join(output_dir, filename)
            render_info = _render_page_region(
                page, cluster["bbox"], filepath, dpi=200, padding_pt=6.0,
            )
            if not render_info:
                continue
            caption = _find_caption_near_region(
                page, cluster["bbox"], search_distance_pt=45.0,
            )
            all_figures.append({
                "path": filepath,
                "page_num": page_num,
                "width": render_info["width"],
                "height": render_info["height"],
                "source": "region_render",
                "caption": caption,
                "related_text": "",
                "element_count": cluster["element_count"],
            })
            fig_counter += 1

    # ── Pass 3: vector-only pages — drawings-cluster fallback ──
    # If a page has no captions AND no raster images BUT has substantial
    # vector drawings, render the drawing region.
    covered = {f["page_num"] for f in all_figures}
    for page_num in target_pages:
        if page_num >= total_pages or page_num in covered:
            continue
        page = doc[page_num]
        # only consider pages that look like they have a figure (lots of drawings)
        try:
            n_draw = sum(
                1 for d in page.get_drawings()
                if pymupdf.Rect(d["rect"]).width > 10
                and pymupdf.Rect(d["rect"]).height > 10
            )
        except Exception:
            n_draw = 0
        if n_draw < 20:
            continue
        region = _detect_drawing_region(page)
        if region is None:
            continue
        filename = f"fig_vec_{fig_counter:03d}_p{page_num}.png"
        filepath = os.path.join(output_dir, filename)
        render_info = _render_page_region(
            page, region, filepath, dpi=200, padding_pt=10.0,
        )
        if not render_info:
            continue
        all_figures.append({
            "path": filepath,
            "page_num": page_num,
            "width": render_info["width"],
            "height": render_info["height"],
            "source": "vector_fallback",
            "caption": "",
            "related_text": "",
            "element_count": 0,
        })
        fig_counter += 1

    # ── Scoring & dedup (same as before, lightly adjusted) ──
    def _score(f: dict[str, Any]) -> float:
        s = 0.0
        if f.get("caption"):
            s += 5.0 + min(len(f["caption"]) / 100, 2.0)
        if f.get("related_text"):
            s += 3.0
        src = f.get("source")
        if src == "caption_anchored":
            s += 3.0   # highest confidence
        elif src == "region_render":
            s += 1.5
        elif src == "vector_fallback":
            s += 0.5
        w, h = f.get("width", 0), f.get("height", 0)
        if isinstance(w, int) and isinstance(h, int):
            area = w * h
            if area > 500_000:
                s += 1.0
            elif area < 30_000:
                s -= 2.0
        return s

    for f in all_figures:
        f["_score"] = _score(f)
    all_figures = [f for f in all_figures if f["_score"] > -1.0 or f.get("caption")]
    all_figures.sort(key=lambda f: (f["page_num"], -f["_score"]))
    for f in all_figures:
        f.pop("_score", None)
        f.pop("fig_num", None)  # internal only

    doc.close()

    n_cap = sum(1 for f in all_figures if f["source"] == "caption_anchored")
    n_reg = sum(1 for f in all_figures if f["source"] == "region_render")
    n_vec = sum(1 for f in all_figures if f["source"] == "vector_fallback")
    logger.info(
        "[pdf-parser] Figures: %d caption-anchored + %d region + %d vector "
        "= %d total (from %d pages)",
        n_cap, n_reg, n_vec, len(all_figures), len(target_pages),
    )

    return all_figures


def _detect_drawing_region(
    page: Any,
    min_size_pt: float = 120.0,
    max_page_ratio: float = 0.75,
) -> Any | None:
    """Detect the bounding box of significant vector drawings on a page.

    Excludes text-decoration drawings (underlines, separators, table borders)
    by cross-referencing against text block positions.
    """
    import pymupdf

    try:
        drawings = page.get_drawings()
    except Exception:
        return None
    if not drawings:
        return None

    page_area = page.rect.width * page.rect.height

    # Pre-compute text block rects for exclusion
    text_rects: list[Any] = []
    try:
        text_dict = page.get_text("dict", flags=11)
        for block in text_dict.get("blocks", []):
            if block.get("type") == 0:
                bx0, by0, bx1, by1 = block["bbox"]
                # Expand by a few pt to catch underlines/decorations
                text_rects.append(pymupdf.Rect(bx0 - 2, by0 - 2, bx1 + 2, by1 + 2))
    except Exception:
        pass

    def _inside_text_block(r: Any) -> bool:
        """True if most of r lies inside a text block (likely text decoration)."""
        r_area = r.width * r.height
        if r_area <= 0:
            return True
        for tr in text_rects:
            inter = r & tr
            if not inter.is_empty:
                if (inter.width * inter.height) / r_area > 0.6:
                    return True
        return False

    significant: list[Any] = []
    for d in drawings:
        r = pymupdf.Rect(d["rect"])
        # Skip page-wide fills
        if r.width * r.height > page_area * max_page_ratio:
            continue
        # Skip tiny decorations
        if r.width < 20 and r.height < 20:
            continue
        # skip absurdly thin rects (lines, underlines)
        if r.height < 3 or r.width < 3:
            continue
        # skip extreme aspect ratios (separator lines)
        aspect = r.width / max(r.height, 1.0)
        if aspect > 20 or aspect < 0.05:
            continue
        # skip drawings buried inside text blocks
        if _inside_text_block(r):
            continue
        significant.append(r)

    if len(significant) < 4:
        return None

    clusters = _cluster_nearby_rects(significant, distance_pt=15.0)

    best: Any | None = None
    best_area = 0.0
    for cluster in clusters:
        bbox = cluster["bbox"]
        area = bbox.width * bbox.height
        if bbox.width < min_size_pt or bbox.height < min_size_pt:
            continue
        if area / page_area > max_page_ratio:
            continue
        if cluster["element_count"] < 4:
            continue
        if area > best_area:
            best_area = area
            best = bbox

    return best


async def extract_figures_enriched(
    pdf_path: str,
    markdown_text: str,
    output_dir: str,
    pages: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Async wrapper for enriched figure extraction."""
    return await asyncio.to_thread(
        _extract_figures_enriched_sync,
        pdf_path, markdown_text, output_dir, pages,
    )
