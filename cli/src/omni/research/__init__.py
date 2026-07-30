"""Public research capabilities provided by the OmniScientist runtime.

These are *CLI-side* helpers (e.g. a resilient arXiv Atom-API client) that skill
engines may call when OmniScientist executes them. Keeping them here — rather
than inside individual skill packages — means the skill *content* under
``<repo>/skills`` stays portable (other tools read only ``SKILL.md``), while the
execution adapters reuse one tested implementation.
"""

from __future__ import annotations

from typing import Any

from omni.research.arxiv import ArxivError, fetch_by_id, normalize_arxiv_id, search
from omni.research.corpus import search_corpus
from omni.research.embedding_runtime import (
    ConfiguredEmbeddingRuntime,
    EmbeddingRuntimeError,
    configured_embedding_runtime,
    configured_embedding_space_id,
    embedding_space_id,
    specter2_embedding_space_id,
)
from omni.research.retrieval import search_literature
from omni.research.store import ResearchStore


def capture_env_lock() -> str:
    """Return the public reproducibility fingerprint used by skill engines."""
    from omni.research.tools import capture_env_lock as _capture_env_lock

    return _capture_env_lock()


def add_papers_to_library(library_path: Any, papers: list[dict[str, Any]]) -> None:
    """Add paper metadata through a stable public adapter."""
    from omni.memory.library import add_papers

    add_papers(library_path, papers)


__all__ = [
    "ArxivError",
    "ConfiguredEmbeddingRuntime",
    "EmbeddingRuntimeError",
    "ResearchStore",
    "add_papers_to_library",
    "capture_env_lock",
    "configured_embedding_runtime",
    "configured_embedding_space_id",
    "embedding_space_id",
    "fetch_by_id",
    "normalize_arxiv_id",
    "search",
    "search_corpus",
    "search_literature",
    "specter2_embedding_space_id",
]
