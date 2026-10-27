"""Retain an arXiv TeX submission without executing its contents."""

from __future__ import annotations

import gzip
import tarfile
import tempfile
import zlib
from io import BytesIO
from pathlib import Path

from . import tex_parser

MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_FILES = 4096


def unpack_source_bundle(archive_path: Path, output_dir: Path) -> Path:
    """Extract bounded regular files into a new directory with tarfile's data filter."""

    if output_dir.exists():
        raise FileExistsError(output_dir)
    with archive_path.open("rb") as stream:
        content = stream.read(MAX_SOURCE_BYTES + 1)
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("Source package exceeds the input size limit")
    if content.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=BytesIO(content)) as compressed:
                content = compressed.read(MAX_SOURCE_BYTES + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise ValueError(f"Could not decompress source submission: {exc}") from exc
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("Expanded source package exceeds the size limit")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="source-unpack-", dir=output_dir.parent) as directory:
        stage = Path(directory) / "project"
        stage.mkdir()
        try:
            archive = tarfile.open(fileobj=BytesIO(content), mode="r:")
        except tarfile.ReadError:
            text = content.decode("utf-8")
            if next(tex_parser.commands(text, ("documentclass", "documentstyle")), None) is None:
                raise ValueError("Source download contains neither a TeX project nor a TeX document") from None
            (stage / "main.tex").write_bytes(content)
        else:
            with archive:
                if not hasattr(tarfile, "data_filter"):
                    raise ValueError("Safe source extraction requires a Python tarfile data filter")
                members = []
                size = 0
                for member in archive:
                    if not member.isfile() and not member.isdir():
                        raise ValueError("Source package requires regular files and directories")
                    size += member.size
                    members.append(member)
                    if size > MAX_SOURCE_BYTES or len(members) > MAX_SOURCE_FILES:
                        raise ValueError("Expanded source package exceeds its file or size limit")
                archive.extractall(stage, members=members, filter="data")
        stage.rename(output_dir)
    return output_dir
