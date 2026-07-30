"""arxiv-fetch engine — fetch arXiv paper metadata.

This file is the OmniScientist *execution adapter* for the portable
``arxiv-fetch`` skill. The skill instructions (``SKILL.md``) are tool-agnostic;
this engine runs only when OmniScientist executes the skill, and reuses omni's
public research client (``omni.research.arxiv``). Other tools (Claude Code /
Codex / OpenClaw) read ``SKILL.md`` and never import this module.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any


def _load_core():  # noqa: ANN202
    """Load sibling core.py even when engine.py is imported by absolute path."""
    candidate = Path(__file__).with_name("core.py")
    spec = importlib.util.spec_from_file_location("arxiv_fetch_core", candidate)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {candidate}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fetch, module.normalize_arxiv_id


_fetch, _normalize_arxiv_id = _load_core()


async def _emit_milestone(progress_callback: Any) -> None:
    """Report the one completion milestone (a lookup has no intermediate stages)."""
    if progress_callback is None:
        return
    try:
        emitted = progress_callback(
            "fetched",
            1.0,
            stage_id="arxiv.done",
            milestone="Paper metadata fetched",
        )
    except TypeError:
        emitted = progress_callback("fetched", 1.0)
    if hasattr(emitted, "__await__"):
        await emitted


def _invalid_identifier_error() -> dict[str, Any]:
    return {
        "status": "error",
        "outcome": {"code": "invalid_identifier"},
        "error": (
            "identifier must contain a valid arXiv id or arXiv URL; "
            "for topic search use a provider with the literature.search capability."
        ),
        "recoverable": False,
        "blocking": True,
        "error_info": {
            "code": "invalid_identifier",
            "message": "identifier must contain a valid arXiv id or arXiv URL",
            "retryable": False,
            "workflow_recoverable": False,
        },
        "retryable": False,
        "next_capabilities": ["literature.search"],
    }


class ArxivFetchEngine:
    @staticmethod
    def validate_params(*, arguments: dict | None = None, input_data: dict | None = None) -> dict | None:
        data = arguments or input_data or {}
        identifier = data.get("identifier") or data.get("arxiv_id") or data.get("id") or ""
        if not identifier:
            return _invalid_identifier_error()
        if not _normalize_arxiv_id(str(identifier)):
            return _invalid_identifier_error()
        return None

    async def execute(self, progress_callback: Any = None, **input_data: Any) -> dict[str, Any]:
        identifier = (
            input_data.get("identifier")
            or input_data.get("arxiv_id")
            or input_data.get("id")
            or ""
        )
        if not _normalize_arxiv_id(str(identifier)):
            return _invalid_identifier_error()
        result = await asyncio.to_thread(_fetch, str(identifier))
        if result.get("status") == "ok":
            self._save_to_library(result)
            await _emit_milestone(progress_callback)
        return result

    def _save_to_library(self, paper: dict[str, Any]) -> None:
        paths = getattr(getattr(self, "ctx", None), "paths", None)
        if paths is None:
            return
        try:
            from omni.research import add_papers_to_library

            add_papers_to_library(paths.library, [paper])
        except Exception:  # noqa: BLE001
            pass
