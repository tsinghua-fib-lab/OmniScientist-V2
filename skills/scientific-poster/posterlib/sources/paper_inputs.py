"""Resolve local PDF and arXiv inputs for the PDF preparation path."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .paper_extraction import PaperSourceError

_ARXIV_ID = re.compile(
    r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?",
    re.IGNORECASE,
)
_MAX_PDF_BYTES = 50 * 1024 * 1024


def materialize_paper_pdf(
    *,
    paper_path: str = "",
    arxiv_id: str = "",
    output_dir: Path,
) -> Path:
    """Select a PDF or arXiv paper for the PDF preparation path."""

    if sum(bool(value) for value in (paper_path, arxiv_id)) != 1:
        raise PaperSourceError(
            "invalid_payload", "Supply one paper_path or arxiv_id"
        )
    if arxiv_id:
        return download_arxiv_pdf(arxiv_id, output_dir / "arxiv-paper")
    source = Path(paper_path).expanduser()
    if not source.is_file():
        raise PaperSourceError("source_not_found", f"Paper PDF was not found: {source}")
    return source


def arxiv_identifier(value: str) -> str:
    """Accept an exact arXiv id or an arxiv.org abstract/PDF URL."""

    raw = str(value or "").strip()
    if raw.lower().startswith("arxiv:"):
        raw = raw[6:].strip()
    if raw.startswith(("https://", "http://")):
        parsed = urlparse(raw)
        if parsed.scheme != "https" or parsed.hostname not in {
            "arxiv.org", "www.arxiv.org"
        } or parsed.query or parsed.fragment:
            raise PaperSourceError("invalid_payload", "arxiv_id must use arxiv.org HTTPS")
        path = parsed.path.strip("/").split("/", 1)
        if len(path) != 2 or path[0] not in {"abs", "pdf"}:
            raise PaperSourceError("invalid_payload", "arxiv_id must name an arXiv paper")
        raw = path[1].removesuffix(".pdf")
    if _ARXIV_ID.fullmatch(raw) is None:
        raise PaperSourceError("invalid_payload", "arxiv_id is not a valid arXiv id")
    return raw


def download_arxiv_pdf(identifier: str, output_dir: Path) -> Path:
    """Download one bounded arXiv PDF for the normal PDF preparation path."""

    arxiv_id = arxiv_identifier(identifier)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "paper.pdf"
    request = Request(
        f"https://arxiv.org/pdf/{arxiv_id}",
        headers={"User-Agent": "OmniScientist-scientific-poster/1.0"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            content = response.read(_MAX_PDF_BYTES + 1)
    except OSError as exc:
        raise PaperSourceError("source_read_failed", f"Could not fetch arXiv PDF: {exc}") from exc
    if len(content) > _MAX_PDF_BYTES or not content.startswith(b"%PDF-"):
        raise PaperSourceError("source_read_failed", "arXiv did not return a usable PDF")
    target.write_bytes(content)
    return target
