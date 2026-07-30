"""Reviewer (LLM-as-judge) gate: parsing, gating, and fail-open behaviour."""

from __future__ import annotations

from typing import Any

import pytest

from omni.agent.reviewer import ReviewVerdict, gate, parse_verdict, review_output


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
