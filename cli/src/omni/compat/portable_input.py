"""UTF-8 JSON input for portable skill runners.

PowerShell mangles ``--json '{"..."}'`` (quotes disappear) and a non-UTF-8
console code page turns Chinese ``output_dir`` values into ``????``. Codex-style
hosts take a file or stdin as UTF-8 bytes instead of trusting the console.

Portable ``scripts/run.py`` files must not import this module (they stay
Omni-free). They implement the same contract: ``--json-file`` (UTF-8),
``--json``, then stdin decoded as UTF-8.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO


class PortableJsonError(ValueError):
    """Structured portable-runner input failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_json_object(
    *,
    json_text: str | None = None,
    json_file: str | None = None,
    stdin: TextIO | None = None,
) -> dict[str, Any]:
    """Load a JSON object from a UTF-8 file, a ``--json`` string, or stdin.

    ``--json-file`` wins when both file and text are supplied. Stdin is read as
    UTF-8 bytes (``utf-8-sig``) so a Windows console code page cannot rewrite
    Chinese paths. An empty payload is ``{}``.
    """
    if json_file:
        path = Path(json_file).expanduser()
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise PortableJsonError(
                "json_file_unreadable", f"Cannot read --json-file {path}: {exc}"
            ) from exc
    elif json_text is not None:
        raw = json_text
    else:
        stream = stdin if stdin is not None else sys.stdin
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            data = buffer.read()
            raw = data.decode("utf-8-sig") if data else ""
        else:
            raw = stream.read()
    raw = raw.strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PortableJsonError(
            "invalid_json", f"Command input is not valid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PortableJsonError(
            "invalid_payload", "Command input must be a JSON object."
        )
    return value
