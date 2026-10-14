"""Read a LaTeX paper project without compiling away its source structure."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .paper_extraction import PaperSourceError

_ROOT_DECLARATION = re.compile(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{")
_INCLUDE = re.compile(r"\\(?:input|include)\b\s*(?:\{([^{}]+)\}|([\w./-]+))")
_FIGURE = re.compile(r"\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}", re.DOTALL)
_GRAPHIC = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}")
_EQUATION = re.compile(
    r"\\begin\{(equation\*?|align\*?|gather\*?|multline\*?)\}(.*?)\\end\{\1\}",
    re.DOTALL,
)


def prepare_latex_project(
    project_dir: Path,
    output_dir: Path,
    *,
    main_tex: str = "",
) -> dict[str, Any]:
    """Return source text, exact formula strings, and included figure assets."""

    project = project_dir.expanduser().resolve()
    if not project.is_dir():
        raise PaperSourceError("source_not_found", f"LaTeX project not found: {project_dir}")
    main = _main_file(project, main_tex)
    files: list[tuple[Path, str]] = []
    visited: set[Path] = set()

    def read_file(path: Path) -> str:
        resolved = path.resolve()
        if not resolved.is_relative_to(project) or not resolved.is_file():
            raise PaperSourceError("source_read_failed", f"LaTeX include not found: {path}")
        if resolved in visited:
            return ""
        visited.add(resolved)
        try:
            source = _without_comments(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise PaperSourceError("source_read_failed", f"Cannot read LaTeX source: {resolved}") from exc
        files.append((resolved, source))
        def include(match: re.Match[str]) -> str:
            name = match.group(1) or match.group(2)
            for base in (resolved.parent, project):
                included = base / name
                if not included.suffix:
                    included = included.with_suffix(".tex")
                if included.resolve().is_relative_to(project) and included.is_file():
                    return read_file(included)
            raise PaperSourceError("source_read_failed", f"LaTeX include not found: {name}")
        return (
            f"\n[LaTeX file: {resolved.relative_to(project)}]\n"
            + _INCLUDE.sub(include, source)
        )

    source_text = read_file(main)
    title = _plain_tex(_command_argument(files[0][1], "title"))
    authors = _plain_tex(_command_argument(files[0][1], "author"))
    affiliations = tuple(
        value for value in (
            _plain_tex(match.group(1))
            for _, source in files
            for match in re.finditer(r"\\affiliation\s*\{([^{}]+)\}", source)
        ) if value
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    figures: list[dict[str, Any]] = []
    equations: list[dict[str, str]] = []
    warnings: list[str] = []
    for path, source in files:
        for match in _EQUATION.finditer(source):
            latex = re.sub(r"\\label\s*\{[^{}]*\}", "", match.group(2)).strip()
            if latex:
                label = re.search(r"\\label\s*\{([^{}]+)\}", match.group(2))
                equations.append({
                    "latex": latex,
                    "source_label": (
                        f"{path.relative_to(project)}:{label.group(1)}"
                        if label else str(path.relative_to(project))
                    ),
                    "context": "LaTeX display equation",
                })
        for figure in _FIGURE.finditer(source):
            body = figure.group(1)
            caption = _plain_tex(_command_argument(body, "caption"))
            label = _command_argument(body, "label")
            for graphic in _GRAPHIC.finditer(body):
                name = graphic.group(1).strip()
                asset = _graphic_file(project, path.parent, name)
                if asset is None:
                    warnings.append(f"LaTeX figure could not be resolved: {name}")
                    continue
                try:
                    figure_path = _poster_image(asset, output_dir)
                    digest = hashlib.sha256(figure_path.read_bytes()).hexdigest()
                except (OSError, ValueError) as exc:
                    warnings.append(f"LaTeX figure could not be prepared: {name}: {exc}")
                    continue
                number = len(figures) + 1
                figures.append({
                    "path": str(figure_path),
                    "sha256": digest,
                    "source_kind": "latex_figure",
                    "figure_number": number,
                    "carrier_number": number,
                    "carrier_label": label or f"Figure {number}",
                    "caption": caption,
                    "context": str(path.relative_to(project)),
                })
    return {
        "text": source_text,
        "title": title,
        "authors": authors,
        "affiliations": affiliations,
        "equations": equations,
        "figures": figures,
        "warnings": warnings,
        "path": str(project),
    }


def _main_file(project: Path, main_tex: str) -> Path:
    if main_tex:
        path = (project / main_tex).resolve()
        if not path.is_relative_to(project) or not path.is_file() or path.suffix != ".tex":
            raise PaperSourceError("invalid_payload", "main_tex must name a .tex file inside the project")
        return path
    candidates = [
        path for path in project.rglob("*.tex")
        if path.is_file() and not path.is_symlink()
        and _ROOT_DECLARATION.search(_without_comments(path.read_text(encoding="utf-8", errors="replace")))
    ]
    if len(candidates) != 1:
        raise PaperSourceError(
            "invalid_payload", "LaTeX project needs one document root; supply main_tex when ambiguous"
        )
    return candidates[0]


def _without_comments(source: str) -> str:
    lines = []
    for line in source.splitlines():
        cut = len(line)
        for index, character in enumerate(line):
            if character != "%":
                continue
            slash_count = 0
            before = index - 1
            while before >= 0 and line[before] == "\\":
                slash_count += 1
                before -= 1
            if character == "%" and slash_count % 2 == 0:
                cut = index
                break
        lines.append(line[:cut])
    return "\n".join(lines)


def _command_argument(source: str, command: str) -> str:
    marker = re.search(rf"\\{re.escape(command)}\s*(?:\[[^\]]*\])?\s*\{{", source)
    if marker is None:
        return ""
    start = marker.end()
    depth = 1
    for index in range(start, len(source)):
        if source[index] == "{" and (index == 0 or source[index - 1] != "\\"):
            depth += 1
        elif source[index] == "}" and (index == 0 or source[index - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return source[start:index]
    return ""


def _plain_tex(value: str) -> str:
    text = value.replace(r"\and", ", ").replace(r"\\", ", ")
    text = re.sub(r"\\[A-Za-z]+\*?(?:\[[^\]]*\])?", "", text)
    text = text.replace("{", "").replace("}", "").replace("~", " ")
    return re.sub(r"\s+,", ",", " ".join(text.split())).strip(" ,")


def _graphic_file(project: Path, parent: Path, name: str) -> Path | None:
    raw = Path(name)
    paths = (parent / raw, project / raw)
    extensions = ("", ".png", ".jpg", ".jpeg", ".svg", ".pdf")
    for base in paths:
        for extension in extensions:
            candidate = Path(str(base) + extension).resolve()
            if candidate.is_relative_to(project) and candidate.is_file():
                return candidate
    return None


def _poster_image(path: Path, output_dir: Path) -> Path:
    if path.suffix.lower() != ".pdf":
        return path
    try:
        import pymupdf
    except ImportError as exc:
        raise ValueError("PDF figure conversion requires pymupdf") from exc
    with pymupdf.open(path) as document:
        if len(document) != 1:
            raise ValueError("included PDF figure must have one page")
        pixels = document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        content = pixels.tobytes("png")
    target = output_dir / f"{hashlib.sha256(path.read_bytes()).hexdigest()[:16]}.png"
    target.write_bytes(content)
    return target
