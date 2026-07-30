"""P1-D′a: token counting uses a real tokenizer when available, heuristic else.

Offline (no ``tiktoken`` installed, or ``OMNI_DISABLE_TIKTOKEN=1``) it must fall
back to the deterministic heuristic and never crash — including on text that
contains tokenizer "special" markers.
"""

from __future__ import annotations

import importlib.util

import pytest

from omni.memory.compaction import _heuristic_tokens, estimate_tokens


def test_empty_is_zero():
    assert estimate_tokens("") == 0


def test_heuristic_counts_cjk_heavier_than_ascii():
    # 4 CJK chars ≈ 4 tokens; 4 ascii chars ≈ 1 token. CJK must cost more.
    assert _heuristic_tokens("研究智能体") > _heuristic_tokens("abcd")


def test_disable_env_forces_heuristic(monkeypatch):
    import omni.memory.compaction as comp

    monkeypatch.setenv("OMNI_DISABLE_TIKTOKEN", "1")
    monkeypatch.setattr(comp, "_TIKTOKEN_TRIED", False, raising=False)
    monkeypatch.setattr(comp, "_TIKTOKEN_ENC", None, raising=False)
    assert comp._tiktoken_encoder() is None
    # With the encoder disabled the public estimate equals the heuristic.
    assert estimate_tokens("hello world 你好") == _heuristic_tokens("hello world 你好")


def test_never_crashes_on_special_tokens():
    # A naïve tiktoken call would raise on <|endoftext|>; ours must not.
    assert estimate_tokens("<|endoftext|> hi <|im_start|>") > 0


@pytest.mark.skipif(
    importlib.util.find_spec("tiktoken") is None, reason="tiktoken not installed"
)
def test_real_tokenizer_is_used_when_available(monkeypatch):
    import omni.memory.compaction as comp

    monkeypatch.delenv("OMNI_DISABLE_TIKTOKEN", raising=False)
    monkeypatch.setattr(comp, "_TIKTOKEN_TRIED", False, raising=False)
    monkeypatch.setattr(comp, "_TIKTOKEN_ENC", None, raising=False)
    assert comp._tiktoken_encoder() is not None
    assert estimate_tokens("hello world") > 0
