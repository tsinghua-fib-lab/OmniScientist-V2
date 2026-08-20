"""Contracts for SoulAgent's verbatim P04 injection."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"


def _decoder():
    path = SKILLS_ROOT / "soulagent" / "kg_decoder.py"
    spec = importlib.util.spec_from_file_location("soulagent_kg_decoder_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _subgraph() -> dict:
    return {
        "identity": {"scientist_name": "Test Scientist"},
        "philosophy_kernel": {
            "stances": [
                {"question_label": "原则一", "stance": "先验证再扩展。"},
                {"question_label": "原则二", "stance": "保持设计简单。"},
                {"question_label": "原则三", "stance": "承认证据边界。"},
            ],
            "tone_exemplars": [
                "However, we show that\na surprisingly simple system works.",
                "Finally, we note that the exact form is not crucial.",
                "This strong evidence shows that the principle is generic.",
            ],
        },
        "active_l2": [
            {"category_label": "选择方法", "description": "优先检验简单基线。"}
        ],
        "l1_evidence": [
            {"source_title": "A Paper", "observation": "简单基线已经有效。"}
        ],
        "tension_resolved": [],
    }


def _task_frame() -> dict:
    return {
        "objective": "为新方法设计一个基线实验",
        "constraints": {"compute_constraint": False, "time_pressure": False},
    }


def _assert_decoder_error(decoder, expected: str, callback) -> None:
    try:
        callback()
    except decoder.DecoderError as exc:
        assert expected in str(exc)
    else:
        raise AssertionError("DecoderError was not raised")


def test_decoder_input_does_not_send_p04_to_the_llm() -> None:
    decoder = _decoder()
    subgraph = _subgraph()

    prompt = decoder.build_decoder_input(subgraph, _task_frame())

    for exemplar in subgraph["philosophy_kernel"]["tone_exemplars"]:
        assert exemplar not in prompt


def test_decoder_rejects_persona_that_ignores_tone() -> None:
    decoder = _decoder()

    def completion(_system_prompt: str, _user_prompt: str) -> str:
        return """## 当前人格：Test Scientist
### 核心原则
稍后由程序替换。
### 当前任务中的思考方式
- 先做基线。
### 当前取舍
当前没有触发需要消解的取舍。
### 证据来源
- A Paper：简单基线已经有效。
"""

    _assert_decoder_error(
        decoder,
        "表达语气",
        lambda: decoder.decode_subgraph(_subgraph(), _task_frame(), completion),
    )


def test_decoder_injects_p01_to_p04_verbatim_after_llm_completion() -> None:
    decoder = _decoder()

    def completion(system_prompt: str, user_prompt: str) -> str:
        for exemplar in _subgraph()["philosophy_kernel"]["tone_exemplars"]:
            assert exemplar not in system_prompt
            assert exemplar not in user_prompt
        return """## 当前人格：Test Scientist
### 表达语气
这段由模型生成的语气归纳应被替换。
### 核心原则
这段内容应被替换。
### 当前任务中的思考方式
- 先做最简单的基线；如果它已成立，再增加组件。
### 当前取舍
当前没有触发需要消解的取舍。
### 证据来源
- A Paper：简单基线已经有效。
"""

    persona = decoder.decode_subgraph(_subgraph(), _task_frame(), completion)

    assert "### 表达语气" in persona
    for exemplar in _subgraph()["philosophy_kernel"]["tone_exemplars"]:
        assert exemplar in persona
    assert "模型生成的语气归纳" not in persona
    assert "原则一：先验证再扩展。" in persona
    assert "这段内容应被替换" not in persona


def test_decoder_retries_compact_after_a_truncated_first_draft() -> None:
    decoder = _decoder()
    calls: list[str] = []

    def completion(_system_prompt: str, user_prompt: str) -> str:
        calls.append(user_prompt)
        if decoder.COMPACT_RETRY_HINT in user_prompt:
            return """## 当前人格：Test Scientist
### 表达语气
placeholder
### 核心原则
placeholder
### 当前任务中的思考方式
- 先做最简单的基线。
### 当前取舍
当前没有触发需要消解的取舍。
### 证据来源
- A Paper：简单基线已经有效。
"""
        return "## 当前人格：Test Scientist\n### 表达语气\n被截断"

    persona = decoder.decode_subgraph(_subgraph(), _task_frame(), completion)

    assert len(calls) == 2
    assert decoder.COMPACT_RETRY_HINT in calls[1]
    assert "先做最简单的基线。" in persona
    assert "原则一：先验证再扩展。" in persona


def test_decoder_reports_both_failures_after_compact_retry() -> None:
    decoder = _decoder()

    def completion(_system_prompt: str, _user_prompt: str) -> str:
        return "## 当前人格：Test Scientist\n### 表达语气\n仍不完整"

    try:
        decoder.decode_subgraph(_subgraph(), _task_frame(), completion)
    except decoder.DecoderError as exc:
        assert "两次均未得到完整结果" in str(exc)
        assert "缺少章节" in str(exc)
    else:
        raise AssertionError("DecoderError was not raised")


def test_validator_rejects_non_verbatim_tone_content() -> None:
    decoder = _decoder()
    persona = """## 当前人格：Test Scientist
### 表达语气
简洁、专业、严谨。
### 核心原则
原则。
### 当前任务中的思考方式
- 先做基线。
### 当前取舍
当前没有触发需要消解的取舍。
### 证据来源
- A Paper。
"""

    _assert_decoder_error(
        decoder,
        "逐字保留全部语气原句",
        lambda: decoder.validate_persona(persona, _subgraph()),
    )
