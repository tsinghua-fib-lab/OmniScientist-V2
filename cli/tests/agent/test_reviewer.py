"""Reviewer (LLM-as-judge) gate: parsing, gating, and fail-open behaviour."""

from __future__ import annotations

from typing import Any

import pytest

from omni.agent.reviewer import (
    ReviewVerdict,
    gate,
    parse_verdict,
    review_output,
    review_research_deliverable,
    structural_review,
)


def test_parse_clean_json():
    v = parse_verdict('{"verdict": "pass", "score": 0.9, "notes": "ok"}')
    assert v.verdict == "pass"
    assert v.score == pytest.approx(0.9)
    assert v.notes == "ok"
    assert v.parsed is True


def test_parse_json_embedded_in_prose():
    raw = '好的，我的评审是：{"verdict":"revise","score":0.4,"notes":"缺少引用"} 完毕'
    v = parse_verdict(raw)
    assert v.verdict == "revise"
    assert v.score == pytest.approx(0.4)
    assert v.notes == "缺少引用"


def test_parse_unknown_verdict_normalizes_to_pass():
    v = parse_verdict('{"verdict": "maybe", "score": 0.7}')
    assert v.verdict == "pass"


def test_parse_non_json_is_fail_open():
    v = parse_verdict("looks fine to me")
    assert v.verdict == "pass"
    assert v.parsed is False


def test_parse_clamps_score():
    assert parse_verdict('{"verdict":"pass","score":5}').score == pytest.approx(1.0)
    assert parse_verdict('{"verdict":"pass","score":-3}').score == pytest.approx(0.0)
    assert parse_verdict('{"verdict":"pass","score":"x"}').score == pytest.approx(0.5)


def test_gate_mapping():
    assert gate(ReviewVerdict("reject", 0.9), min_score=0.5) == "reject"
    assert gate(ReviewVerdict("revise", 0.9), min_score=0.5) == "revise"
    assert gate(ReviewVerdict("pass", 0.3), min_score=0.5) == "revise"  # low score → revise
    assert gate(ReviewVerdict("pass", 0.8), min_score=0.5) == "accept"


class _Judge:
    def __init__(self, reply: str) -> None:
        self.model = "judge"

    async def chat(self, system: str, user: str, **kw: Any) -> str:
        return '{"verdict":"reject","score":0.1,"notes":"off-topic"}'


@pytest.mark.asyncio
async def test_review_output_uses_judge():
    v = await review_output(_Judge(""), goal="g", output="some output")
    assert v.verdict == "reject"
    assert v.score == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_review_output_fail_open_on_no_llm_or_empty():
    assert (await review_output(None, goal="g", output="x")).verdict == "pass"
    assert (await review_output(_Judge(""), goal="g", output="  ")).verdict == "pass"


@pytest.mark.asyncio
async def test_review_output_fail_open_on_judge_error():
    class _Boom:
        model = "boom"

        async def chat(self, system: str, user: str, **kw: Any) -> str:
            raise RuntimeError("judge down")

    v = await review_output(_Boom(), goal="g", output="something")
    assert v.verdict == "pass"
    assert v.parsed is False


def test_structural_review_rejects_dangling_anchor_when_grounded() -> None:
    verdict = structural_review(
        draft="A result is SOTA [S99].",
        source_ids=["source123456"],
        s_index_map={"[S1]": "source123456"},
        grounded=True,
    )
    assert verdict.verdict == "reject"
    assert verdict.blocks_success is True
    assert "[S99]" in verdict.dangling_anchors
    assert any(card.kind == "dangling_anchor" for card in verdict.findings)


def test_structural_review_arxiv_doi_are_not_dangling_against_hash_ids() -> None:
    verdict = structural_review(
        draft=(
            "The transformer 1706.03762 and RAG 10.18653/v1/2020.emnlp-main.550 "
            "are the starting points."
        ),
        source_ids=["fb3930049cda40a88ea0b75a80785df9"],
        citation_keys=["1706.03762", "10.18653/v1/2020.emnlp-main.550"],
        grounded=True,
    )
    assert verdict.dangling_anchors == []
    assert verdict.blocks_success is False
    assert verdict.verdict == "pass"


def test_structural_review_arxiv_without_recorded_keys_is_still_not_dangling() -> None:
    verdict = structural_review(
        draft="See 1706.03762 for the architecture.",
        source_ids=["fb3930049cda40a88ea0b75a80785df9"],
        grounded=True,
    )
    assert "1706.03762" not in verdict.dangling_anchors
    assert verdict.blocks_success is False


def test_structural_review_s_index_can_exceed_first_result_page() -> None:
    sources = [f"src-{index:04d}abcd" for index in range(1, 47)]
    verdict = structural_review(
        draft="Later evidence is [S17] and [S22].",
        source_ids=sources,
        s_index_map={f"[S{index}]": sources[index - 1] for index in range(1, 17)},
        grounded=True,
    )
    assert verdict.dangling_anchors == []
    assert verdict.blocks_success is False


def test_structural_review_mapped_s_anchors_are_not_dangling() -> None:
    verdict = structural_review(
        draft="The survey cites [S1] and [S2].",
        source_ids=["src-aaaa1111", "src-bbbb2222"],
        s_index_map={"[S1]": "src-aaaa1111", "[S2]": "src-bbbb2222"},
        grounded=True,
    )
    assert verdict.dangling_anchors == []
    assert verdict.blocks_success is False


def test_structural_review_flags_unanchored_absolute() -> None:
    verdict = structural_review(
        draft="This method is the first to solve RAG.",
        source_ids=["source123456"],
        grounded=True,
    )
    assert verdict.verdict == "revise"
    assert verdict.unanchored_absolutes


@pytest.mark.asyncio
async def test_research_review_llm_fail_open_keeps_structural_gate() -> None:
    class _Boom:
        model = "boom"

        async def chat(self, system: str, user: str, **kw: Any) -> str:
            raise RuntimeError("judge down")

    verdict = await review_research_deliverable(
        llm=_Boom(),
        goal="survey",
        draft="A result is SOTA [S99].",
        source_ids=["source123456"],
        s_index_map={"[S1]": "source123456"},
        grounded=True,
    )
    assert verdict.verdict == "reject"
    assert verdict.blocks_success is True


@pytest.mark.asyncio
async def test_research_review_llm_reject_does_not_block_resolved_scholarly_keys() -> None:
    verdict = await review_research_deliverable(
        llm=_Judge(""),
        goal="survey",
        draft="See 1706.03762 and 10.18653/v1/2020.emnlp-main.550.",
        source_ids=["fb3930049cda40a88ea0b75a80785df9"],
        citation_keys=["1706.03762", "10.18653/v1/2020.emnlp-main.550"],
        grounded=True,
    )
    assert verdict.verdict == "reject"
    assert verdict.blocks_success is False
    assert verdict.dangling_anchors == []
