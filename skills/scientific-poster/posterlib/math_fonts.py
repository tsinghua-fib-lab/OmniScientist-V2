"""Shared offline math font resources for browser and native document layout."""

from __future__ import annotations

import base64
from functools import cache
from pathlib import Path

from posterlib.paths import SKILL_ROOT

FONT_FACE = "STIX Two Math"
TTF_PATH: Path = SKILL_ROOT / "assets/fonts/STIXTwoMath-Regular.ttf"
EOT_PATH: Path = SKILL_ROOT / "assets/fonts/STIXTwoMath-Regular.eot"

__all__ = ["EOT_PATH", "FONT_FACE", "TTF_PATH", "font_css"]


@cache
def font_css() -> str:
    """Bind every MathML token to the bundled face before natural layout."""

    encoded = base64.b64encode(TTF_PATH.read_bytes()).decode("ascii")
    return (
        f'@font-face{{font-family:"{FONT_FACE}";'
        f'src:url("data:font/ttf;base64,{encoded}") format("truetype");'
        'font-style:normal;font-weight:400;}'
        f'\nmath,math *{{font-family:"{FONT_FACE}" !important;}}'
    )
