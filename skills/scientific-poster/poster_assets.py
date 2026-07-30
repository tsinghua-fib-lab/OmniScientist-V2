"""Bounded image preparation and inert embedded-asset validation."""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from css_safety import offline_css_issues

MAX_EMBEDDED_ASSET_BYTES = 8 * 1024 * 1024
ACTIVE_CONTENT_TAGS = frozenset(
    {
        "animate",
        "animatemotion",
        "animatetransform",
        "audio",
        "base",
        "button",
        "details",
        "discard",
        "dialog",
        "embed",
        "foreignobject",
        "form",
        "iframe",
        "input",
        "link",
        "marquee",
        "object",
        "script",
        "select",
        "set",
        "summary",
        "textarea",
        "video",
    }
)

_REMOTE_RE = re.compile(
    r"^(?:https?:)?//|^file:|^javascript:|^vbscript:", re.IGNORECASE
)
_DATA_IMAGE_RE = re.compile(
    r"^data:image/(?:png|jpeg|gif|webp|svg\+xml);base64,([A-Za-z0-9+/=]+)$",
    re.IGNORECASE,
)


def prepare_asset_manifest(
    values: Any,
    *,
    resolve: Callable[[str], Path | None],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve supplied or bundled image assets into bounded, inert records."""

    if values is None:
        items: list[Any] = []
    elif isinstance(values, (str, Path, dict)):
        items = [values]
    else:
        try:
            items = list(values)
        except TypeError:
            items = [values]
    manifest: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, value in enumerate(items, start=1):
        source, description = _asset_source_and_description(value)
        token = f"asset://{index}"
        if not source:
            warnings.append(f"{token}: asset source is missing.")
            continue
        try:
            resolved = resolve(source)
        except (OSError, RuntimeError, ValueError) as exc:
            warnings.append(f"{token}: asset could not be resolved: {exc}")
            continue
        path = Path(resolved) if resolved is not None else None
        if path is None or not path.is_file() or path.is_symlink():
            warnings.append(f"{token}: regular asset file not found: {source}")
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            warnings.append(f"{token}: asset could not be read: {exc}")
            continue
        if len(content) > MAX_EMBEDDED_ASSET_BYTES:
            warnings.append(f"{token}: asset exceeds the embedded-image byte limit.")
            continue
        mime = image_asset_mime(content)
        if mime is None:
            warnings.append(f"{token}: unsupported image type: {path.name}")
            continue
        if mime == "image/svg+xml" and not svg_asset_is_safe(content):
            warnings.append(f"{token}: SVG contains active or external content.")
            continue
        content_sha256 = hashlib.sha256(content).hexdigest()
        source_kind = (
            str(value.get("source_kind") or "user_asset")
            if isinstance(value, dict)
            else "user_asset"
        )
        if source_kind not in {
            "pdf_figure",
            "user_asset",
            "venue_brand_asset",
        }:
            warnings.append(f"{token}: unsupported asset provenance: {source_kind}")
            continue
        claimed_sha256 = (
            str(value.get("content_sha256") or "") if isinstance(value, dict) else ""
        )
        if claimed_sha256 and claimed_sha256 != content_sha256:
            warnings.append(f"{token}: source image hash changed before embedding.")
            continue
        venue_provenance: dict[str, str] = {}
        figure_provenance: dict[str, Any] = {}
        if isinstance(value, dict) and source_kind == "venue_brand_asset":
            venue_id = str(value.get("venue_id") or "").strip()
            evidence_uri = str(value.get("evidence_uri") or "").strip()
            venue_label = " ".join(str(value.get("label") or "").split())
            if (
                not claimed_sha256
                or not venue_id
                or not evidence_uri
                or not venue_label
            ):
                warnings.append(f"{token}: venue brand provenance is incomplete.")
                continue
            venue_provenance = {
                "venue_id": venue_id,
                "label": venue_label,
                "evidence_uri": evidence_uri,
            }
        if isinstance(value, dict) and source_kind == "pdf_figure":
            extraction_mode = str(value.get("extraction_mode") or "").strip()
            if extraction_mode and extraction_mode not in {
                "embedded-raster",
                "vector-clip",
                "raster-fallback",
            }:
                warnings.append(f"{token}: unsupported PDF figure extraction mode.")
                continue
            if extraction_mode:
                figure_provenance["extraction_mode"] = extraction_mode
        manifest.append(
            {
                "token": token,
                "source": source,
                "filename": path.name,
                "mime": mime,
                "description": description,
                "bytes": len(content),
                "content_sha256": content_sha256,
                "source_kind": source_kind,
                "data_uri": f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}",
                **venue_provenance,
                **figure_provenance,
                **(
                    {
                        "figure_number": value.get("figure_number"),
                        "page": value.get("page"),
                        "crop_bbox": value.get("crop_bbox"),
                    }
                    if isinstance(value, dict) and source_kind == "pdf_figure"
                    else {}
                ),
            }
        )
    return manifest, warnings


def data_image_sha256(value: str) -> str | None:
    """Hash one bounded embedded image URI, or return ``None`` when invalid."""

    match = _DATA_IMAGE_RE.fullmatch(value.strip())
    if match is None:
        return None
    try:
        content = base64.b64decode(match.group(1), validate=True)
    except (ValueError, TypeError):
        return None
    if len(content) > MAX_EMBEDDED_ASSET_BYTES or image_asset_mime(content) is None:
        return None
    return hashlib.sha256(content).hexdigest()


def asset_aspect_ratio(asset: dict[str, Any]) -> float:
    """Return prepared crop geometry as a width/height ratio."""

    bbox = asset.get("crop_bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            width = float(bbox[2]) - float(bbox[0])
            height = float(bbox[3]) - float(bbox[1])
        except (TypeError, ValueError):
            pass
        else:
            if width > 0 and height > 0:
                return round(width / height, 4)
    return 1.0


def image_asset_mime(content: bytes) -> str | None:
    """Return the supported inert image MIME type identified from exact bytes."""

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    try:
        root = ElementTree.fromstring(content.decode("utf-8-sig"))
    except (ElementTree.ParseError, UnicodeDecodeError):
        return None
    return "image/svg+xml" if root.tag.rsplit("}", 1)[-1].lower() == "svg" else None


def svg_asset_is_safe(content: bytes) -> bool:
    """Return whether one bounded SVG is inert and fully self-contained."""

    if len(content) > MAX_EMBEDDED_ASSET_BYTES:
        return False
    try:
        text = content.decode("utf-8-sig")
        root = ElementTree.fromstring(text)
    except (ElementTree.ParseError, UnicodeDecodeError):
        return False
    if "<!doctype" in text.lower() or "<?xml-stylesheet" in text.lower():
        return False
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in ACTIVE_CONTENT_TAGS:
            return False
        for raw_name, raw_value in element.attrib.items():
            name = raw_name.rsplit("}", 1)[-1].lower()
            value = str(raw_value or "").strip()
            namespace = raw_name.split("}", 1)[0].lstrip("{")
            if (
                name == "resource"
                and namespace == "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            ):
                continue
            if name.startswith("on"):
                return False
            if name in {"href", "src"}:
                if not safe_embedded_reference(value):
                    return False
                continue
            if not safe_svg_css(value):
                return False
        if tag == "style" and not safe_svg_css(element.text or ""):
            return False
    return True


def safe_embedded_reference(value: str) -> bool:
    """Return whether a CSS or SVG reference is inert and self-contained."""

    candidate = value.strip()
    if not candidate or candidate.startswith("#"):
        return True
    if _REMOTE_RE.search(candidate):
        return False
    match = _DATA_IMAGE_RE.fullmatch(candidate)
    if match is None:
        return False
    try:
        decoded = base64.b64decode(match.group(1), validate=True)
    except ValueError:
        return False
    if len(decoded) > MAX_EMBEDDED_ASSET_BYTES:
        return False
    if candidate.lower().startswith("data:image/svg+xml"):
        return svg_asset_is_safe(decoded)
    return image_asset_mime(decoded) is not None


def _asset_source_and_description(value: Any) -> tuple[str, str]:
    if isinstance(value, dict):
        source = next(
            (
                value.get(key)
                for key in ("uri", "path", "source", "file")
                if value.get(key)
            ),
            "",
        )
        return str(source).strip(), str(
            value.get("description") or value.get("alt") or ""
        ).strip()
    return str(value or "").strip(), ""


def safe_svg_css(css: str) -> bool:
    """Return whether inline SVG presentation CSS is inert."""

    return not offline_css_issues(css, safe_reference=safe_embedded_reference)


__all__ = [
    "ACTIVE_CONTENT_TAGS",
    "MAX_EMBEDDED_ASSET_BYTES",
    "asset_aspect_ratio",
    "data_image_sha256",
    "image_asset_mime",
    "prepare_asset_manifest",
    "safe_embedded_reference",
    "safe_svg_css",
    "svg_asset_is_safe",
]
