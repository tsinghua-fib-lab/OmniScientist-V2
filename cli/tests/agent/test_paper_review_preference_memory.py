"""Offline contracts for paper-review's anonymous Arena preference memory."""

from __future__ import annotations

import importlib.util
import json
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "paper-review"


def _load_memory() -> Any:
    name = "paper_review_preference_memory_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / "preference_memory.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_review_memory() -> Any:
    name = "paper_review_historical_memory_for_preference_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / "review_memory.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _TopicEmbedding:
    space_id = "emb-v1:arena-offline"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.casefold()
            if "graph" in lowered:
                vectors.append([1.0, 0.1, 0.0])
            elif "vision" in lowered:
                vectors.append([0.0, 1.0, 0.1])
            else:
                vectors.append([0.0, 0.1, 1.0])
        return vectors


def _answer(
    query_id: str,
    agent_key: str,
    answer: str,
    file_name: str,
) -> dict[str, str]:
    return {
        "query_id": query_id,
        "agent_key": agent_key,
        "answer": answer,
        "file_name": file_name,
    }


def _battle(
    query_id: str,
    a_key: str,
    b_key: str,
    result: str,
    *,
    a_name: str = "Reviewer A",
    b_name: str = "Reviewer B",
) -> dict[str, Any]:
    return {
        "battle_id": f"battle-{query_id}-{a_key}-{b_key}-{result}",
        "query_id": query_id,
        "agent_a_key": a_key,
        "agent_a_name": a_name,
        "agent_b_key": b_key,
        "agent_b_name": b_name,
        "result": result,
        "is_valid": True,
        "is_archived": False,
    }


def _write_dataset(
    root: Path,
    *,
    answers: list[dict[str, Any]],
    human: list[dict[str, Any]],
    battles: list[dict[str, Any]],
    markdown: dict[str, str],
) -> Path:
    root.mkdir()
    paper_root = root / "paper_markdown"
    paper_root.mkdir()
    for name, text in markdown.items():
        (paper_root / name).write_text(text, encoding="utf-8")
    (root / "paper_review_answers.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in answers),
        encoding="utf-8",
    )
    (root / "queries_human_responses.json").write_text(
        json.dumps(human, ensure_ascii=False), encoding="utf-8"
    )
    (root / "reviewer_results.json").write_text(
        json.dumps(battles, ensure_ascii=False), encoding="utf-8"
    )
    return root


def _packet(record: dict[str, Any]) -> list[dict[str, Any]]:
    value = json.loads(zlib.decompress(record["preferences_blob"]))
    assert isinstance(value, list)
    return value


def _parser_fixture(tmp_path: Path) -> Path:
    long_winner = "Specific revision with complete detail. " + "x" * 4000
    answers = [
        _answer("q-flip", "winner", long_winner, r"pdf\Graph Paper.pdf"),
        # Exact duplicate rows fold instead of making the battle ambiguous.
        _answer("q-flip", "winner", long_winner, r"pdf_md\Graph Paper.md"),
        _answer("q-flip", "loser", "Vague advice.", r"pdf\Graph Paper.pdf"),
        # reviewer_human here is a locator only; its answer must never be used.
        _answer("q-human", "reviewer_human", "WRONG HUMAN SOURCE", r"pdf\Vision.pdf"),
        _answer("q-human", "machine", "Machine review.", r"pdf\Vision.pdf"),
        _answer("q-ambiguous", "multi", "first", r"pdf\Ambiguous.pdf"),
        _answer("q-ambiguous", "multi", "second", r"pdf\Ambiguous.pdf"),
        _answer("q-ambiguous", "single", "one", r"pdf\Ambiguous.pdf"),
        _answer("q-tie", "left", "left", r"pdf\Tie.pdf"),
        _answer("q-tie", "right", "right", r"pdf\Tie.pdf"),
        _answer("q-bad", "good", "good", r"pdf\Bad.pdf"),
        _answer("q-bad", "other", "other", r"pdf\Bad.pdf"),
        _answer("q-title", "good", "title winner", r"pdf\Title Only.pdf"),
        _answer("q-title", "other", "title loser", r"pdf\Title Only.pdf"),
        _answer("q-path", "left", "left path", r"pdf\..\outside.pdf"),
        _answer("q-path", "right", "right path", r"pdf\..\outside.pdf"),
    ]
    battles = [
        _battle("q-flip", "winner", "loser", "A"),
        _battle("q-flip", "loser", "winner", "B"),
        _battle(
            "q-human",
            "reviewer_human",
            "machine",
            "A",
            a_name="Reviewer human",
        ),
        _battle("q-ambiguous", "multi", "single", "A"),
        _battle("q-tie", "left", "right", "Tie"),
        _battle("q-bad", "good", "other", "A"),
        _battle("q-bad", "good", "other", "BothBad"),
        _battle("q-title", "good", "other", "A"),
        _battle("q-title", "other", "good", "B"),
        _battle("q-title", "good", "other", "BothBad"),
        _battle("q-path", "left", "right", "A"),
    ]
    markdown = {
        "Graph Paper.md": (
            "# Graph Retrieval\n\nAuthors\n\n## Abstract\n\nGraph retrieval memory.\n\n"
            "## Introduction\nBody"
        ),
        "Vision.md": (
            "# Vision Models\n\nAlice and Bob Abstract\n\nVision evaluation.\n\n"
            "# Introduction\nBody"
        ),
        "Ambiguous.md": "# Ambiguous\n\nAbstract: ambiguity.\n\n## Intro\nBody",
        "Tie.md": "# Tie\n\nAbstract: tied preferences.\n\n## Intro\nBody",
        "Bad.md": "# Bad\n\nAbstract: both bad.\n\n## Intro\nBody",
        "Title Only.md": "# Language Advice\n\nAlice and Bob\n\n# Introduction\nBody",
    }
    return _write_dataset(
        tmp_path / "arena",
        answers=answers,
        human=[{"query_id": "q-human", "human_response": "TRUE HUMAN REVIEW"}],
        battles=battles,
        markdown=markdown,
    )


def test_parser_resolves_human_and_flipped_votes_without_losing_full_text(
    tmp_path: Path,
) -> None:
    memory = _load_memory()
    dataset = _parser_fixture(tmp_path)

    records, report = memory.parse_arena_dataset(dataset)

    assert report["paper_count"] == 3
    assert report["preference_pair_count"] == 3
    assert report["battles_included"] + report["battles_skipped"] == report[
        "battles_seen"
    ]
    assert report["title_only_paper_count"] == 1
    assert report["duplicate_response_rows_folded"] == 1
    assert report["skipped_by_reason"] == {
        "ambiguous_response": 1,
        "both_bad_not_outvoted": 2,
        "invalid_paper_path": 1,
        "no_strict_winner_pair": 1,
    }

    by_name = {record["paper_relative_path"]: record for record in records}
    graph_pair = _packet(by_name["Graph Paper.md"])[0]
    assert graph_pair["preferred_votes"] == 2
    assert graph_pair["less_preferred_votes"] == 0
    assert graph_pair["preferred_review"].endswith("x" * 4000)
    assert graph_pair["less_preferred_review"] == "Vague advice."

    human_pair = _packet(by_name["Vision.md"])[0]
    assert human_pair["preferred_review"] == "TRUE HUMAN REVIEW"
    assert "WRONG HUMAN SOURCE" not in json.dumps(human_pair)

    title_pair = _packet(by_name["Title Only.md"])[0]
    assert title_pair["preferred_votes"] == 2
    assert title_pair["both_bad_votes"] == 1
    assert by_name["Title Only.md"]["abstract"] == ""


def test_markdown_path_normalization_and_escape_rejection(tmp_path: Path) -> None:
    memory = _load_memory()
    markdown_root = tmp_path / "paper_markdown"
    markdown_root.mkdir()
    (markdown_root / "Paper.md").write_text("# Paper", encoding="utf-8")

    first, relative = memory._resolve_markdown_path(
        r"pdf\Paper.pdf", markdown_root=markdown_root.resolve()
    )
    second, second_relative = memory._resolve_markdown_path(
        r"pdf_md\Paper.md", markdown_root=markdown_root.resolve()
    )

    assert first == second
    assert relative == second_relative == "Paper.md"
    with pytest.raises(ValueError, match="escapes"):
        memory._resolve_markdown_path(
            r"pdf\..\outside.pdf", markdown_root=markdown_root.resolve()
        )
    with pytest.raises(ValueError):
        memory._resolve_markdown_path(
            "/absolute.pdf", markdown_root=markdown_root.resolve()
        )


def test_numbered_abstract_heading_and_casefolded_suffix(tmp_path: Path) -> None:
    memory = _load_memory()
    markdown_root = tmp_path / "paper_markdown"
    markdown_root.mkdir()
    paper = markdown_root / "Numbered.md"
    paper.write_text(
        "# Numbered Abstract\n\nAuthors\n\n## 1 Abstract\n\n"
        "The complete abstract text.\n\n## 2 Introduction\nBody",
        encoding="utf-8",
    )

    resolved, relative = memory._resolve_markdown_path(
        r"PDF\Numbered.PDF", markdown_root=markdown_root.resolve()
    )
    title, abstract = memory._markdown_title_abstract(resolved)

    assert relative == "Numbered.md"
    assert title == "Numbered Abstract"
    assert abstract == "The complete abstract text."


def test_embedding_query_text_matches_historical_review_memory_exactly() -> None:
    preference = _load_memory()
    historical = _load_review_memory()
    title = "  Graph\nRetrieval  "
    abstract = "Dense\t scientific   search."

    assert preference.paper_embedding_text(
        title, abstract
    ) == historical.paper_embedding_text(title, abstract)


def _active_generation(index: Path) -> Path:
    header = json.loads((index / "index.json").read_text(encoding="utf-8"))
    return index / "generations" / header["active_generation"]


def _corrupt_one_byte(path: Path) -> None:
    value = path.read_bytes()
    assert value
    middle = len(value) // 2
    path.write_bytes(value[:middle] + bytes([value[middle] ^ 1]) + value[middle + 1 :])


@pytest.mark.asyncio
async def test_faiss_build_retrieve_public_anonymity_and_integrity(tmp_path: Path) -> None:
    memory = _load_memory()
    dataset = _parser_fixture(tmp_path)
    index = tmp_path / "preference-faiss"
    embedder = _TopicEmbedding()

    built = await memory.build_preference_index(
        dataset,
        index,
        embedder=embedder,
        embedding_model="offline-topic",
        batch_size=2,
    )

    assert built["status"] == "ok"
    assert built["paper_count"] == 3
    generation = _active_generation(index)
    assert {path.name for path in generation.iterdir()} == {
        "vectors.faiss",
        "papers.jsonl",
        "preferences.pack",
    }
    assert json.loads((index / "index.json").read_text(encoding="utf-8"))[
        "index_owner"
    ] == memory.INDEX_OWNER
    assert not any(
        path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}
        for path in index.rglob("*")
    )

    result = await memory.retrieve_preference_memory(
        index,
        embedder=_TopicEmbedding(),
        structure={"title": "New Graph Work", "abstract": "Graph retrieval methods."},
        top_k=99,
        embedding_model="offline-topic",
        embedding_space_id=_TopicEmbedding.space_id,
    )

    assert result["status"] == "partial"
    assert result["requested_top_k"] == 5
    assert result["matched_paper_count"] == 3
    assert result["matched_pair_count"] == 3
    assert len(result["_preference_pairs"]) == 3
    private = json.dumps(result["_preference_pairs"], ensure_ascii=False)
    assert "Specific revision with complete detail" in private
    assert not any(
        forbidden in private
        for forbidden in (
            "query_id",
            "battle_id",
            "agent_key",
            "paper_id",
            "Graph Retrieval",
            "Vision Models",
        )
    )
    public = memory.public_preference_memory(result)
    public_text = json.dumps(public, ensure_ascii=False)
    assert "_preference_pairs" not in public
    assert "Specific revision with complete detail" not in public_text
    inspected = memory.inspect_preference_index(index)
    assert inspected["status"] == "ready"
    assert inspected["paper_count"] == 3
    assert inspected["preference_pair_count"] == 3

    old_generation = inspected["active_generation"]
    rebuilt = await memory.build_preference_index(
        dataset,
        index,
        embedder=_TopicEmbedding(),
        embedding_model="offline-topic",
        rebuild=True,
    )
    assert rebuilt["active_generation"] != old_generation
    assert (index / "generations" / old_generation).is_dir()

    _corrupt_one_byte(_active_generation(index) / "preferences.pack")
    assert memory.inspect_preference_index(index)["status"] == "invalid"
    unavailable = await memory.retrieve_preference_memory(
        index,
        embedder=_TopicEmbedding(),
        structure={"title": "Graph", "abstract": "retrieval"},
        embedding_model="offline-topic",
        embedding_space_id=_TopicEmbedding.space_id,
    )
    assert unavailable["status"] == "unavailable"
    assert unavailable["outcome"]["code"] == "preference_memory_index_invalid"
    assert unavailable["_preference_pairs"] == []


@pytest.mark.asyncio
async def test_missing_or_mismatched_index_is_fail_soft(tmp_path: Path) -> None:
    memory = _load_memory()
    missing = tmp_path / "missing"

    result = await memory.retrieve_preference_memory(
        missing,
        embedder=_TopicEmbedding(),
        structure={"title": "Paper", "abstract": "Abstract"},
    )

    assert result["status"] == "unavailable"
    assert result["outcome"]["code"] == "preference_memory_index_missing"
    assert "build_preference_index.py" in result["setup_command"]
    assert result["next_actions"]
    assert memory.inspect_preference_index(missing) == {
        "status": "missing",
        "index_path": str(missing.resolve()),
    }


@pytest.mark.asyncio
async def test_embedding_provider_details_are_not_exposed(tmp_path: Path) -> None:
    memory = _load_memory()
    dataset = _parser_fixture(tmp_path)
    index = tmp_path / "preference-faiss"
    await memory.build_preference_index(
        dataset,
        index,
        embedder=_TopicEmbedding(),
        embedding_model="offline-topic",
    )

    async def unsafe_embedder(_texts: list[str]) -> list[list[float]]:
        raise ValueError("https://provider.invalid/?api_key=super-secret")

    result = await memory.retrieve_preference_memory(
        index,
        embedder=unsafe_embedder,
        structure={"title": "Graph", "abstract": "retrieval"},
        embedding_model="offline-topic",
        embedding_space_id=_TopicEmbedding.space_id,
    )

    rendered = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "unavailable"
    assert result["outcome"]["code"] == "preference_memory_embedding_unavailable"
    assert "super-secret" not in rendered
    assert "provider.invalid" not in rendered
