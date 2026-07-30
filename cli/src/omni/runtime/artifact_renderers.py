"""Renderer adapters for artifact contracts."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from omni.runtime.processes import process_group_options, stop_process_tree


@dataclass(frozen=True, slots=True)
class RenderedFile:
    path: Path
    format: str
    mime: str


@dataclass(frozen=True, slots=True)
class RenderResult:
    ok: bool
    files: tuple[RenderedFile, ...] = ()
    error: str = ""
    command: str = ""


class GraphvizRenderer:
    """Graphviz ``dot`` adapter for DOT -> SVG/PNG."""

    formats = ("svg", "png")
    mime = {
        "dot": "text/vnd.graphviz",
        "svg": "image/svg+xml",
        "png": "image/png",
    }

    def __init__(self, dot_bin: str | None = None) -> None:
        # Resolve ``dot`` lazily so test doubles that prepend a fake binary to
        # PATH after import (and CI hosts without a system Graphviz) still work.
        # A concrete override always wins.
        self._dot_bin_override = dot_bin

    @property
    def dot_bin(self) -> str:
        if self._dot_bin_override:
            return self._dot_bin_override
        return shutil.which("dot") or ""

    @property
    def available(self) -> bool:
        return bool(self.dot_bin)

    async def render(self, source: Path, *, output_stem: Path | None = None) -> RenderResult:
        dot_bin = self.dot_bin
        if not dot_bin:
            return RenderResult(False, error="graphviz dot not found")
        source = Path(source)
        output_stem = output_stem or source.with_suffix("")
        files: list[RenderedFile] = []
        command = f"{dot_bin} -Tsvg/-Tpng {source}"
        for fmt in self.formats:
            out = output_stem.with_suffix(f".{fmt}")
            try:
                proc = await asyncio.create_subprocess_exec(
                    dot_bin,
                    f"-T{fmt}",
                    str(source),
                    "-o",
                    str(out),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    **process_group_options(),
                )
            except OSError as exc:
                return RenderResult(False, error=str(exc), command=command)
            try:
                stdout, _ = await proc.communicate()
            except asyncio.CancelledError:
                await stop_process_tree(proc)
                raise
            if proc.returncode != 0:
                error = (stdout or b"").decode("utf-8", errors="replace") or f"dot exited {proc.returncode}"
                return RenderResult(False, error=error, command=command)
            if not out.is_file() or out.stat().st_size == 0:
                return RenderResult(False, error=f"renderer did not produce {out.name}", command=command)
            files.append(RenderedFile(path=out, format=fmt, mime=self.mime[fmt]))
        stale = [f.path.name for f in files if f.path.stat().st_mtime < source.stat().st_mtime]
        if stale:
            return RenderResult(False, error="rendered outputs are older than source: " + ", ".join(stale), command=command)
        return RenderResult(True, files=tuple(files), command=command)
