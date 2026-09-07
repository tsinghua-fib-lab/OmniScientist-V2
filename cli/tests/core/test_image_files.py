"""Raster helpers shared by clipboard paste and read_file."""

from __future__ import annotations

import base64
from pathlib import Path

from omni.core.image_files import (
    EncodedImageFormat,
    encode_png,
    image_data_url,
    inspect_image,
    inspect_image_bytes,
    looks_like_image_path,
    mime_for_bytes,
    pasted_image_format,
)

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_format_label_and_suffix() -> None:
    assert EncodedImageFormat.PNG.label() == "PNG"
    assert EncodedImageFormat.JPEG.label() == "JPEG"
    assert EncodedImageFormat.OTHER.label() == "IMG"
    assert pasted_image_format(Path("a.JPEG")) is EncodedImageFormat.JPEG
    assert pasted_image_format(Path("a.bmp")) is EncodedImageFormat.OTHER


def test_mime_magic_and_inspect(tmp_path: Path) -> None:
    assert mime_for_bytes(_TINY_PNG) == "image/png"
    assert mime_for_bytes(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert mime_for_bytes(b"GIF89a....") == "image/gif"
    assert mime_for_bytes(b"RIFF....WEBP....") == "image/webp"
    assert mime_for_bytes(b"BM....") == "image/bmp"
    assert mime_for_bytes(b"not-an-image") is None
    assert inspect_image_bytes(b"") is None
    assert inspect_image_bytes(_TINY_PNG) is not None

    png = tmp_path / "dot.png"
    png.write_bytes(_TINY_PNG)
    assert looks_like_image_path(png) is True
    assert inspect_image(png) is not None
    url = image_data_url(png)
    assert url is not None and url.startswith("data:image/png;base64,")
    assert image_data_url(png, max_bytes=4) is None
    assert inspect_image(tmp_path / "missing.png") is None
    assert looks_like_image_path(tmp_path / "missing.bin") is False


def test_encode_png_rejects_unknown_and_keeps_png() -> None:
    encoded = encode_png(_TINY_PNG, EncodedImageFormat.PNG)
    assert encoded is not None
    data, info = encoded
    assert data.startswith(b"\x89PNG")
    assert info.width == 1
    assert encode_png(b"not-an-image", EncodedImageFormat.OTHER) is None
