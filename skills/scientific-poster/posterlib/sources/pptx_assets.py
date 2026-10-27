"""Pair a producer's prepared preview with the original native drawing."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from posterlib.delivery.pptx_components import MIME, load_component


def prepare_pptx_asset(content: bytes, *, preview: dict[str, Any]) -> dict[str, Any]:
    """Return an asset payload whose PPTX objects, rather than its preview, export."""

    presentation = load_component(content)
    return {
        "mime": preview["mime"], "source_kind": "native_component",
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "data_uri": preview["data_uri"],
        "native_pptx": f"data:{MIME};base64," + base64.b64encode(content).decode("ascii"),
        "natural_width_px": presentation.slide_width / 914400 * 96,
        "natural_height_px": presentation.slide_height / 914400 * 96,
        "labels": list(presentation.slides[0].element.xpath(".//a:t/text()")),
        "bytes": len(content),
    }
