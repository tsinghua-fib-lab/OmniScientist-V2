"""Resolve local papers and preserve available arXiv TeX source assets."""

from __future__ import annotations

import tarfile
import tempfile
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pyparsing as pp

from . import PaperSourceError

_ARXIV_ID = pp.Combine(
    (
        pp.Word(pp.nums, exact=4) + "." + pp.Word(pp.nums, min=4, max=5)
        | pp.Word(pp.alphas + "-")
        + pp.Opt("." + pp.Word(pp.alphas, exact=2))
        + "/" + pp.Word(pp.nums, exact=7)
    )
    + pp.Opt(pp.CaselessLiteral("v") + pp.Word(pp.nums))
)
_ARXIV_ID_END = ~pp.Char(
    pp.pyparsing_unicode.alphanums + "_/-",
).set_name("arXiv identifier continuation").leave_whitespace()
_MAX_PDF_BYTES = 50 * 1024 * 1024


def select_paper_source(
    *,
    latex_project_dir: str = "",
    paper_path: str = "",
    arxiv_id: str = "",
    main_tex: str = "",
) -> tuple[str, str]:
    """Prefer local source structure, then the local paper, then remote retrieval."""

    if main_tex and not latex_project_dir.strip():
        raise PaperSourceError("invalid_payload", "main_tex requires latex_project_dir")
    for kind, value in (("latex", latex_project_dir), ("pdf", paper_path), ("arxiv", arxiv_id)):
        if value.strip():
            return kind, value.strip()
    return "", ""


def materialize_paper_pdf(
    *,
    paper_path: str = "",
    arxiv_id: str = "",
    output_dir: Path,
) -> Path:
    """Select a PDF or arXiv paper for the PDF preparation path."""

    kind, value = select_paper_source(paper_path=paper_path, arxiv_id=arxiv_id)
    if not kind:
        raise PaperSourceError(
            "invalid_payload", "Supply one paper_path or arxiv_id"
        )
    if kind == "arxiv":
        return download_arxiv_pdf(value, output_dir / "arxiv-paper")
    source = Path(value).expanduser()
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
    try:
        _ARXIV_ID.parse_string(raw, parse_all=True)
    except pp.ParseBaseException as exc:
        raise PaperSourceError("invalid_payload", "arxiv_id is not a valid arXiv id") from exc
    return raw


def arxiv_stamp_identifier(text: str) -> str:
    """Read an explicit arXiv stamp with the same identifier grammar as inputs."""

    stamp = (
        pp.CaselessKeyword("arxiv").suppress() + pp.Suppress(":")
        + _ARXIV_ID("identifier") + _ARXIV_ID_END
    )
    return next((match.identifier for match, _, _ in stamp.scan_string(text)), "")


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
    except (OSError, HTTPException) as exc:
        raise PaperSourceError("source_read_failed", f"Could not fetch arXiv PDF: {exc}") from exc
    if len(content) > _MAX_PDF_BYTES or not content.startswith(b"%PDF-"):
        raise PaperSourceError("source_read_failed", "arXiv did not return a usable PDF")
    target.write_bytes(content)
    return target


def prepare_arxiv_latex(
    identifier: str, output_dir: Path,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Prefer the submitted source, reporting when PDF preparation is needed."""

    from . import latex_source, source_bundle

    arxiv_id = arxiv_identifier(identifier)
    output_dir.mkdir(parents=True, exist_ok=True)
    request = Request(
        f"https://arxiv.org/src/{arxiv_id}",
        headers={"User-Agent": "OmniScientist-scientific-poster/1.0"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            content = response.read(source_bundle.MAX_SOURCE_BYTES + 1)
        if len(content) > source_bundle.MAX_SOURCE_BYTES:
            raise ValueError("arXiv source exceeds the download size limit")
        directory = Path(tempfile.mkdtemp(prefix="arxiv-source-", dir=output_dir))
        archive = directory / "submission.source"
        archive.write_bytes(content)
        project = source_bundle.unpack_source_bundle(archive, directory / "project")
        prepared = latex_source.prepare_latex_project(project, directory / "figures")
    except (PaperSourceError, OSError, ValueError, EOFError, HTTPException, tarfile.TarError) as exc:
        return None, [f"arXiv TeX source could not be prepared; using the paper PDF: {exc}"]
    prepared["source_archive"] = str(archive)
    return prepared, []
