"""Host I/O helpers for sandboxed compute processes.

Copied onto the process ``PYTHONPATH`` as ``omni_io``. Reads host-injected
environment variables; does not expand shell tokens in caller strings.
"""

from __future__ import annotations

import os
from pathlib import Path

_OUTPUT_ENV = "OMNI_OUTPUT_DIR"


def output_dir() -> Path:
    raw = os.environ.get(_OUTPUT_ENV, "").strip()
    if not raw:
        raise RuntimeError(f"{_OUTPUT_ENV} is not set")
    return Path(raw)


def scratch_dir() -> Path:
    raw = (
        os.environ.get("TMPDIR", "").strip()
        or os.environ.get("TMP", "").strip()
        or os.environ.get("TEMP", "").strip()
    )
    if not raw:
        raise RuntimeError("TMPDIR is not set")
    return Path(raw)


def output_path(*parts: str) -> Path:
    dest = output_dir().joinpath(*parts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest
