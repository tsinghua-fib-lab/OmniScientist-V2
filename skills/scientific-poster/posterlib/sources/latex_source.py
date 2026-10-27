"""Read a LaTeX paper project without compiling away its source structure."""

from __future__ import annotations

import base64
import hashlib
import math
from bisect import bisect_right
from pathlib import Path
from typing import Any

import image_assets
from lxml import etree

from . import PaperSourceError, latex_identity, source_bundle, tex_parser


def prepare_latex_project(
    project_dir: Path,
    output_dir: Path,
    *,
    main_tex: str = "",
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Read a project from the invocation base, preserving its source and figures."""

    invocation_dir = (base_dir or Path.cwd()).expanduser().resolve()
    project = (invocation_dir / project_dir.expanduser()).resolve()
    if not project.is_dir():
        raise PaperSourceError("source_not_found", f"LaTeX project not found: {project_dir}")
    main = _main_file(project, main_tex, base_dir=invocation_dir)
    remaining_source_bytes = source_bundle.MAX_SOURCE_BYTES
    read_paths: set[Path] = set()
    bibliography_resources: list[tuple[Path, str]] = []

    def resolve_source(name: str, parent: Path, suffix: str) -> Path | None:
        for base in (parent, project):
            candidate = base / name
            if not candidate.suffix:
                candidate = candidate.with_suffix(suffix)
            resolved = candidate.resolve()
            if resolved.is_relative_to(project) and resolved.is_file():
                return resolved
        return None

    def read_source(path: Path) -> tuple[str, str]:
        """Share project confinement and the byte budget across source files."""

        nonlocal remaining_source_bytes
        resolved = path.resolve()
        if not resolved.is_relative_to(project) or not resolved.is_file():
            raise PaperSourceError("source_read_failed", f"LaTeX source not found: {path}")
        marker = f"\n[LaTeX file: {resolved.relative_to(project)}]\n"
        try:
            with resolved.open("rb") as stream:
                raw = stream.read(remaining_source_bytes + 1)
            # Reused files count on every inclusion, including their source markers.
            # This bounds expansion work even for a small, highly connected project.
            cost = len(raw) + len(marker.encode("utf-8"))
            if cost > remaining_source_bytes:
                raise PaperSourceError(
                    "source_too_large",
                    "LaTeX source expansion exceeds the "
                    f"{source_bundle.MAX_SOURCE_BYTES // (1024 * 1024)} MiB read budget",
                )
            remaining_source_bytes -= cost
            source = raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise PaperSourceError("source_read_failed", f"Cannot read LaTeX source: {resolved}") from exc
        read_paths.add(resolved)
        return marker, source

    def read_file(
        path: Path, ancestors: tuple[Path, ...] = (),
    ) -> tuple[str, list[tuple[Path, str]]]:
        resolved = path.resolve()
        if resolved in ancestors:
            raise PaperSourceError(
                "source_read_failed", f"Cyclic LaTeX include: {resolved.relative_to(project)}",
            )
        marker, source = read_source(resolved)
        source = _without_comments(source)
        parts = [marker]
        chunks: list[tuple[Path, str]] = []
        previous = 0
        for command in tex_parser.commands(
            source, ("input", "include", "bibliography", "addbibresource"), bare=True,
        ):
            if command.name in {"bibliography", "addbibresource"}:
                names = command.argument.split(",") if command.name == "bibliography" else [command.argument]
                bibliography_resources.extend(
                    (resolved.parent, name.strip()) for name in names if name.strip()
                )
                continue
            included = resolve_source(command.argument.strip(), resolved.parent, ".tex")
            if included is None:
                raise PaperSourceError("source_read_failed", f"LaTeX include not found: {command.argument}")
            annotated, included_chunks = read_file(included, (*ancestors, resolved))
            parts.extend((source[previous:command.start], annotated))
            chunks.append((resolved, source[previous:command.start]))
            chunks.extend(included_chunks)
            previous = command.end
        parts.append(source[previous:])
        chunks.append((resolved, source[previous:]))
        return "".join(parts), chunks

    source_text, chunks = read_file(main)
    semantic_source = "".join(text for _path, text in chunks)
    source_offsets: list[int] = []
    source_paths: list[Path] = []
    offset = 0
    for path, text in chunks:
        if text:
            source_offsets.append(offset)
            source_paths.append(path)
            offset += len(text)

    def source_path(position: int) -> Path:
        return source_paths[bisect_right(source_offsets, position) - 1]

    identity, identity_diagnostics = latex_identity.extract_identity(semantic_source)
    warnings = list(identity_diagnostics.values())

    def append_bibliography(path: Path) -> bool:
        nonlocal source_text
        if path in read_paths:
            return True
        try:
            marker, bibliography = read_source(path)
        except PaperSourceError as exc:
            if exc.code == "source_too_large":
                raise
            warnings.append(str(exc))
            return False
        if not bibliography.strip():
            warnings.append(f"Bibliography file is empty: {path.relative_to(project)}")
            return False
        # Keep bibliography bytes as authoring material. Titles and preambles
        # may contain TeX commands that belong to cited works, not this paper.
        source_text += marker + bibliography + "\n"
        return True

    if bibliography_resources:
        compiled = resolve_source(main.with_suffix(".bbl").name, main.parent, ".bbl")
        if compiled is None or not append_bibliography(compiled):
            for parent, name in dict.fromkeys(bibliography_resources):
                resource = resolve_source(name, parent, ".bib")
                if resource is None:
                    warnings.append(f"Bibliography resource could not be resolved: {name}")
                else:
                    append_bibliography(resource)
    output_dir.mkdir(parents=True, exist_ok=True)
    figures: list[dict[str, Any]] = []
    equations: list[dict[str, str]] = []
    equation_names = (
        "equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*",
    )
    for environment in tex_parser.environments(
        semantic_source, ("tikzpicture", "figure", "figure*", *equation_names),
    ):
        path = source_path(environment.start)
        if environment.name == "tikzpicture":
            warning = f"TikZ in {path.relative_to(project)} requires TeX compilation to preview."
            if warning not in warnings:
                warnings.append(warning)
            continue
        if environment.name in equation_names:
            latex = tex_parser.remove_commands(environment.body, ("label",)).strip()
            if latex:
                label = _command_argument(environment.body, "label")
                equations.append({
                    "latex": latex,
                    "source_label": (
                        f"{path.relative_to(project)}:{label}"
                        if label else str(path.relative_to(project))
                    ),
                    "context": "LaTeX display equation",
                })
            continue
        body = environment.body
        raw_caption = _command_argument(body, "caption")
        try:
            caption = _plain_tex(raw_caption)
        except ValueError as exc:
            caption = raw_caption
            warnings.append(f"LaTeX caption retains its source markup: {exc}")
        label = _command_argument(body, "label")
        figure_source = semantic_source[environment.start:environment.end]
        for graphic in tex_parser.commands(figure_source, ("includegraphics",)):
            name = graphic.argument.strip()
            owner = source_path(environment.start + graphic.start)
            asset = _graphic_file(project, owner.parent, name)
            if asset is None:
                warnings.append(f"LaTeX figure could not be resolved: {name}")
                continue
            try:
                figure_path = _poster_image(asset, output_dir, options=graphic.options)
                digest = hashlib.sha256(figure_path.read_bytes()).hexdigest()
            except (OSError, ValueError) as exc:
                warnings.append(f"LaTeX figure could not be prepared: {name}: {exc}")
                continue
            number = len(figures) + 1
            figures.append({
                "path": str(figure_path),
                "source_path": str(asset),
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
        **identity,
        "affiliations": tuple(identity["affiliations"]),
        "identity_diagnostics": identity_diagnostics,
        "equations": equations,
        "figures": figures,
        "warnings": warnings,
        "path": str(project),
    }


def _main_file(project: Path, main_tex: str, *, base_dir: Path | None = None) -> Path:
    """Select a unique in-project root from the project and invocation bases."""

    if main_tex:
        raw = Path(main_tex).expanduser()
        paths = {(base / raw).resolve() for base in (project, base_dir or Path.cwd())}
        matches = {
            path for path in paths
            if path.is_relative_to(project) and path.is_file() and path.suffix == ".tex"
        }
        if not matches:
            raise PaperSourceError("invalid_payload", "main_tex must name a .tex file inside the project")
        if len(matches) > 1:
            raise PaperSourceError(
                "invalid_payload",
                "main_tex is ambiguous between project and invocation cwd; supply an absolute path",
            )
        return next(iter(matches))
    candidates = [
        path for path in project.rglob("*.tex")
        if path.is_file() and not path.is_symlink()
        and next(tex_parser.commands(
            _without_comments(path.read_text(encoding="utf-8", errors="replace")), ("documentclass",),
        ), None) is not None
    ]
    if len(candidates) != 1:
        raise PaperSourceError(
            "invalid_payload", "LaTeX project needs one document root; supply main_tex when ambiguous"
        )
    return candidates[0]


def _without_comments(source: str) -> str:
    return tex_parser.without_comments(source)


def _command_argument(source: str, command: str) -> str:
    found = next(tex_parser.commands(source, (command,)), None)
    return found.argument if found is not None else ""


def _plain_tex(value: str) -> str:
    return tex_parser.plain_text(value)


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


def _poster_image(path: Path, output_dir: Path, *, options: str = "") -> Path:
    angle = _graphic_rotation(options)
    if path.suffix.lower() != ".pdf" and not angle:
        return path
    try:
        import pymupdf
    except ImportError as exc:
        raise ValueError("PDF figure conversion requires pymupdf") from exc
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    if path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as document:
            if len(document) != 1:
                raise ValueError("included PDF figure must have one page")
            # Glyph outlines preserve appearance with the original PDF fonts.
            root = etree.fromstring(
                document[0].get_svg_image(text_as_path=True).encode("utf-8"), parser=parser,
            )
            # MuPDF's data-* annotations are metadata; the shared validator
            # interprets other attributes as SVG/CSS values.
            for node in root.iter():
                for attribute in tuple(node.attrib):
                    if attribute.startswith("data-"):
                        del node.attrib[attribute]
            content = etree.tostring(root, encoding="utf-8")
    else:
        content = path.read_bytes()
    mime = image_assets.image_asset_mime(content)
    if mime is None or (mime == "image/svg+xml" and not image_assets.svg_asset_is_safe(content)):
        raise ValueError("includegraphics rotation requires a supported inert image")
    if not angle:
        return _write_svg_preview(content, output_dir)
    size = image_assets._image_natural_size(content, mime)
    if size is None:
        raise ValueError("includegraphics rotation requires resolved intrinsic image dimensions")
    width, height = size
    matrix = pymupdf.Matrix(-angle)
    bounds = pymupdf.Rect(0, 0, width, height) * matrix
    matrix.e, matrix.f = -bounds.x0, -bounds.y0
    svg_namespace = "http://www.w3.org/2000/svg"
    svg = etree.Element(f"{{{svg_namespace}}}svg", {
        "width": str(bounds.width), "height": str(bounds.height),
        "viewBox": f"0 0 {bounds.width} {bounds.height}",
    }, nsmap={None: svg_namespace, "xlink": "http://www.w3.org/1999/xlink"})
    transform = "matrix(" + " ".join(str(value) for value in matrix) + ")"
    if mime == "image/svg+xml":
        group = etree.SubElement(svg, f"{{{svg_namespace}}}g", {"transform": transform})
        nested = etree.fromstring(content, parser=parser)
        # Preserve the source viewport, clipping and internal references.
        nested.set("width", str(width))
        nested.set("height", str(height))
        group.append(nested)
    else:
        etree.SubElement(svg, f"{{{svg_namespace}}}image", {
            "width": str(width), "height": str(height),
            "transform": transform,
            "{http://www.w3.org/1999/xlink}href": (
                f"data:{mime};base64," + base64.b64encode(content).decode("ascii")
            ),
        })
    rotated = etree.tostring(svg, encoding="utf-8")
    if not image_assets.svg_asset_is_safe(rotated):
        raise ValueError("includegraphics rotation exceeds the supported embedded image contract")
    return _write_svg_preview(rotated, output_dir)


def _write_svg_preview(content: bytes, output_dir: Path) -> Path:
    """Write a content-addressed SVG preview."""

    target = output_dir / f"{hashlib.sha256(content).hexdigest()[:16]}.svg"
    target.write_bytes(content)
    return target


def _graphic_rotation(options: str) -> float:
    """Separate placement and descriptive options from visible image transformations."""

    if not options.strip():
        return 0.0
    import pyparsing as pp

    _, _, _, group, _ = tex_parser.source_tokens()
    entry = pp.original_text_for(pp.OneOrMore(group | pp.CharsNotIn(",{}")))
    try:
        entries = pp.DelimitedList(entry).parse_string(options, parse_all=True)
    except pp.ParseBaseException as exc:
        raise ValueError("includegraphics options require TeX layout interpretation") from exc
    size_keys = {"width", "height", "totalheight", "scale"}
    signs = pp.ZeroOrMore(pp.Char("+-"))
    margin = pp.Group(pp.pyparsing_common.number + pp.Opt(pp.Word(pp.alphas)))
    angle = 0.0
    rotation_seen = False
    for entry in entries:
        name, _, value = entry.partition("=")
        name, value = name.strip(), value.strip()
        if value.startswith("{"):
            try:
                value = group.parse_string(value, parse_all=True)[0][1:-1].strip()
            except pp.ParseBaseException as exc:
                raise ValueError(f"includegraphics option has an unresolved value: {name}") from exc
        if name in size_keys:
            # Poster placement uses intrinsic proportions; a negative source size
            # changes handedness and requires the rendered source transformation.
            if sum(sign == "-" for sign in signs.parse_string(value)) % 2:
                raise ValueError(f"includegraphics {name} expresses a reflection requiring a prepared source figure")
        elif name in {"origin", "keepaspectratio", "alt", "actualtext"}:
            # The rotation preview is translated to its own bounds, so changing
            # the paper's rotation anchor changes placement only. Descriptive
            # text and fitting constraints also leave the image content intact.
            continue
        elif name == "angle":
            if rotation_seen:
                raise ValueError("Repeated includegraphics rotations require a rendered source figure")
            rotation_seen = True
            try:
                angle = float(value)
            except ValueError as exc:
                raise ValueError("includegraphics angle requires a literal numeric value") from exc
            if not math.isfinite(angle):
                raise ValueError("includegraphics angle requires a finite value")
        elif name == "trim":
            try:
                margins = (margin * 4).parse_string(value, parse_all=True)
            except pp.ParseBaseException as exc:
                raise ValueError("includegraphics trim requires a rendered source figure") from exc
            if any(item[0] != 0 for item in margins):
                raise ValueError("includegraphics trim changes the source viewport; supply a rendered source figure")
        elif name in {"clip", "draft"}:
            if value not in {"", "true", "false"}:
                raise ValueError(f"includegraphics {name} requires a literal boolean value")
            if name == "draft" and value != "false":
                raise ValueError("includegraphics draft replaces the image; supply a rendered source figure")
            # Clipping to the unchanged intrinsic bounds has no visible effect.
            # Nonzero trim and explicit viewport/bounding-box changes require rendering.
        else:
            raise ValueError(f"includegraphics option requires a rendered source figure: {name}")
    return angle % 360
