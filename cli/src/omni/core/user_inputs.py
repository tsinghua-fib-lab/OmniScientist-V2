"""Durable user-provided files for every surface (CLI, web, WeChat).

Codex writes clipboard images to an OS tempfile (``codex-clipboard-*.png``)
and never revisits them. Omni already has a user-facing ``outputs/`` for what
the agent produces; inbound files use the sibling ``inputs/`` on the same
workspace store so a paste, a paperclip upload, and a WeChat image land in
one folder and attach through the same ``@path`` + ``file_uris`` contract.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omni.core.file_mentions import format_mention, parse_mentions
from omni.core.image_files import looks_like_image_path

USER_INPUT_DIRNAME = "inputs"

_IMAGE_PLACEHOLDER = re.compile(r"\[Image #(\d+)\]")


def inputs_dir_for(paths: object) -> Path:
    """``<project_dir>/inputs`` — the Omni workspace store, not the git tree."""
    project = getattr(paths, "project_dir", None)
    if project is None:
        raise TypeError("paths.project_dir is required")
    return Path(project) / USER_INPUT_DIRNAME


def ensure_inputs_dir(paths: object) -> Path:
    """Create ``inputs/`` if needed and return it."""
    dest = inputs_dir_for(paths)
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def next_image_placeholder(text: str) -> str:
    """``[Image #N]`` after the highest number already in ``text``."""
    numbers = [int(match) for match in _IMAGE_PLACEHOLDER.findall(text)]
    return f"[Image #{max(numbers, default=0) + 1}]"


def clipboard_input_filename() -> str:
    """Findable name: ``clipboard-YYYYMMDD-HHMMSS-<rand>.png``."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"clipboard-{stamp}-{secrets.token_hex(3)}.png"


def safe_input_filename(name: str, *, fallback: str = "upload.bin") -> str:
    """Keep the basename; reject empty or traversal tokens."""
    raw = Path(str(name or "")).name.strip() or fallback
    if raw in {".", ".."}:
        return fallback
    return raw


def unique_input_path(dest_dir: Path, filename: str) -> Path:
    """Collision-safe path under ``dest_dir`` (``photo.png``, ``photo-2.png``, …)."""
    dest_dir = Path(dest_dir)
    safe = safe_input_filename(filename)
    candidate = dest_dir / safe
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 10_000):
        alt = dest_dir / f"{stem}-{index}{suffix}"
        if not alt.exists():
            return alt
    raise OSError(f"too many files named {safe} in {dest_dir}")


def write_user_input(dest_dir: Path, data: bytes, *, filename: str) -> Path:
    """Write ``data`` into ``dest_dir`` under a unique, safe filename."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = unique_input_path(dest, filename)
    if not path.resolve().is_relative_to(dest.resolve()):
        raise OSError(f"refusing to write outside {dest}: {path}")
    path.write_bytes(data)
    return path


def bind_input_mentions(
    text: str,
    paths: Sequence[Path | str],
    *,
    cwd: Path | None = None,
    image_placeholders: bool = True,
) -> str:
    """Append ``[Image #N]`` (rasters) and ``@path`` for each still-missing file.

    Already-mentioned paths are left alone so a retry does not duplicate them.
    """
    mentioned = {str(mention.path) for mention in parse_mentions(text, cwd=cwd) if mention.exists}
    extras: list[str] = []
    working = text
    for raw in paths:
        path = Path(raw)
        uri = str(path)
        if uri in mentioned or format_mention(path) in text:
            continue
        if image_placeholders and looks_like_image_path(path):
            placeholder = next_image_placeholder(f"{working} {' '.join(extras)}")
            extras.append(placeholder)
        extras.append(format_mention(path))
        mentioned.add(uri)
    if not extras:
        return text
    body = text.rstrip()
    block = "\n".join(extras)
    return f"{body}\n{block}" if body else block
