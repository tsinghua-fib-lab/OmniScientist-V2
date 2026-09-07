"""Raster-image inspection shared by clipboard paste and ``read_file``.

Pixels are not text. A PNG the user pasted or ``@``-mentioned must still be
something the model can act on: dimensions, a format label, and (when a VLM
is configured) a data URL. Magic-byte checks stay here so the CLI clipboard
module is not imported by the skill runtime.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_WEBP_MAGIC = b"WEBP"
_BMP_MAGIC = b"BM"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class EncodedImageFormat(Enum):
    PNG = "png"
    JPEG = "jpeg"
    OTHER = "other"

    def label(self) -> str:
        return {"png": "PNG", "jpeg": "JPEG", "other": "IMG"}[self.value]


@dataclass(frozen=True)
class PastedImageInfo:
    width: int
    height: int
    encoded_format: EncodedImageFormat = EncodedImageFormat.PNG


def pasted_image_format(path: Path) -> EncodedImageFormat:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return EncodedImageFormat.PNG
    if suffix in {".jpg", ".jpeg"}:
        return EncodedImageFormat.JPEG
    return EncodedImageFormat.OTHER


def looks_like_image_path(path: Path) -> bool:
    if path.suffix.lower() in _IMAGE_SUFFIXES:
        return True
    try:
        probe = path.read_bytes()[:16]
    except OSError:
        return False
    return mime_for_bytes(probe) is not None


def inspect_image(path: Path) -> PastedImageInfo | None:
    """Pixel size of a raster file, or ``None`` when it is not an image."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return inspect_image_bytes(data)


def image_data_url(path: Path, *, max_bytes: int = 4_000_000) -> str | None:
    """``data:image/...;base64,...`` for a VLM, or ``None`` when too large."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > max_bytes:
        return None
    mime = mime_for_bytes(data)
    if mime is None:
        return None
    return f"data:{mime};base64,{base64.standard_b64encode(data).decode('ascii')}"


def inspect_image_bytes(data: bytes) -> PastedImageInfo | None:
    if not data:
        return None
    if data.startswith(_PNG_MAGIC) and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return PastedImageInfo(
            width=width, height=height, encoded_format=EncodedImageFormat.PNG
        )
    converted = pymupdf_to_png(data)
    if converted is None:
        return None
    _png, width, height = converted
    fmt = EncodedImageFormat.JPEG if data.startswith(_JPEG_MAGIC) else EncodedImageFormat.OTHER
    return PastedImageInfo(width=width, height=height, encoded_format=fmt)


def encode_png(
    data: bytes, _source_format: EncodedImageFormat
) -> tuple[bytes, PastedImageInfo] | None:
    info = inspect_image_bytes(data)
    if info is None:
        return None
    if data.startswith(_PNG_MAGIC):
        return data, PastedImageInfo(
            width=info.width, height=info.height, encoded_format=EncodedImageFormat.PNG
        )
    converted = pymupdf_to_png(data)
    if converted is None:
        return None
    png, width, height = converted
    return png, PastedImageInfo(
        width=width, height=height, encoded_format=EncodedImageFormat.PNG
    )


def pymupdf_to_png(data: bytes) -> tuple[bytes, int, int] | None:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - product dependency
        return None
    kind = _pymupdf_kind(data)
    if kind is None:
        return None
    try:
        document = pymupdf.open(stream=data, filetype=kind)
    except Exception:  # noqa: BLE001 - pymupdf rejects many malformed buffers
        return None
    try:
        pixmap = document[0].get_pixmap()
        return pixmap.tobytes("png"), int(pixmap.width), int(pixmap.height)
    except Exception:  # noqa: BLE001
        return None
    finally:
        document.close()


def mime_for_bytes(data: bytes) -> str | None:
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if data.startswith(_GIF_MAGICS):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == _WEBP_MAGIC:
        return "image/webp"
    if data.startswith(_BMP_MAGIC):
        return "image/bmp"
    return None


def _pymupdf_kind(data: bytes) -> str | None:
    mime = mime_for_bytes(data)
    return {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/bmp": "bmp",
    }.get(mime or "")
