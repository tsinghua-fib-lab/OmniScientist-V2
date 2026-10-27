"""Decode model JSON using JSON and CommonMark parsers."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from markdown_it import MarkdownIt


class ModelBoundaryError(ValueError):
    """A model operation stopped with a machine-readable cause."""

    def __init__(self, code: str, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason or code

    def as_issue(self) -> dict[str, Any]:
        """Keep the cause independent of the wording shown to the reader."""

        return {"code": self.code, "reason": self.reason, "summary": str(self)}


@lru_cache(maxsize=1)
def _markdown_parser() -> MarkdownIt:
    return MarkdownIt("commonmark")


def parse_json_object(text: str) -> dict[str, Any]:
    """Read one complete object, optionally surrounded by Markdown presentation."""

    candidate = text.strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        tokens = _markdown_parser().parse(candidate)
        values = []
        for token in tokens:
            if token.type in {"fence", "code_block"}:
                if token.info.strip().casefold() not in {"", "json"}:
                    raise ValueError("Response code blocks must contain JSON") from exc
                values.append(json.loads(token.content))
            elif token.type == "inline":
                try:
                    values.append(json.loads(token.content))
                except json.JSONDecodeError:
                    opening = token.children[0] if token.children else None
                    if (
                        opening is not None
                        and opening.type == "text"
                        and opening.content.lstrip().startswith(("{", "["))
                    ):
                        raise
                    continue
        if len(values) != 1:
            raise ValueError("Response must contain one complete JSON object") from exc
        value = values[0]
    if not isinstance(value, dict):
        raise ValueError("Response must be a JSON object")
    return value
