"""Integrity and portability contracts for paper-review's bundled FAISS indexes."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "paper-review"
INDEX_ROOT = SKILL_DIR / "resources" / "indexes"


def _load_module(filename: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _active_generation(root: Path, header: dict[str, Any]) -> Path:
    generations = root / "generations"
    active = generations / str(header["active_generation"])
    assert [path.name for path in generations.iterdir() if path.is_dir()] == [
        active.name
    ]
    return active


def test_bundled_indexes_are_complete_portable_snapshots() -> None:
    review_root = INDEX_ROOT / "iclr2026-reviews"
    preference_root = INDEX_ROOT / "review-arena-preferences"
    review_header = json.loads((review_root / "index.json").read_text(encoding="utf-8"))
    preference_header = json.loads(
        (preference_root / "index.json").read_text(encoding="utf-8")
    )

    assert not any(path.is_symlink() for path in INDEX_ROOT.rglob("*"))
    assert "manifest_path" not in review_header
    assert review_header["manifest_name"] == "manifest_body.jsonl"
    assert review_header["embedding_space_id"].startswith("emb-v2:")
    assert review_header["embedding_space_id"] == preference_header[
        "embedding_space_id"
    ]
    review_generation = _active_generation(review_root, review_header)
    preference_generation = _active_generation(preference_root, preference_header)
    assert {path.name for path in review_generation.iterdir()} == {
        "vectors.faiss",
        "papers.jsonl",
        "reviews.pack",
    }
    assert {path.name for path in preference_generation.iterdir()} == {
        "vectors.faiss",
        "papers.jsonl",
        "preferences.pack",
    }
    for binary in (
        review_generation / "vectors.faiss",
        review_generation / "reviews.pack",
        preference_generation / "vectors.faiss",
        preference_generation / "preferences.pack",
    ):
        assert binary.stat().st_size > 1_000
        with binary.open("rb") as handle:
            assert not handle.read(64).startswith(b"version https://git-lfs")

    with (review_generation / "papers.jsonl").open(encoding="utf-8") as handle:
        review_records = [json.loads(line) for line in handle]
    assert len(review_records) == 10_000
    assert all("paper_path" not in record for record in review_records)
    assert all("reviews_json_path" not in record for record in review_records)

    review_memory = _load_module(
        "review_memory.py", "paper_review_bundled_review_memory"
    )
    preference_memory = _load_module(
        "preference_memory.py", "paper_review_bundled_preference_memory"
    )
    review_inspection = review_memory.inspect_review_index(review_root)
    preference_inspection = preference_memory.inspect_preference_index(
        preference_root
    )
    assert review_inspection["status"] == "ready"
    assert review_inspection["paper_count"] == 10_000
    assert review_inspection["review_count"] == 39_018
    assert preference_inspection["status"] == "ready"
    assert preference_inspection["paper_count"] == 738
    assert preference_inspection["preference_pair_count"] == 738
