from __future__ import annotations

from omni.research.literature_modes import (
    precedent_verdict,
    utterance_asks_precedent,
)


def test_utterance_asks_precedent_en_and_zh() -> None:
    assert utterance_asks_precedent("Has anyone used activation steering for tool-use agents?")
    assert utterance_asks_precedent("有没有人做过用隐空间干预提升 agent 工具调用的工作？")
    assert not utterance_asks_precedent("Write a related-work section on RAG evaluation.")


def test_precedent_verdict_reads_first_line() -> None:
    assert precedent_verdict("Yes. See 1706.03762.") == "yes"
    assert precedent_verdict("No prior work on this exact setup.") == "no"
    assert precedent_verdict("A long survey without a verdict.") == ""
