"""Minimal OpenAI-compatible client for SoulAgent's portable runner.

OmniScientist injects its host LLM through ``engine.py``.  This module is only
the portable fallback used by ``core.py`` when SoulAgent is run directly.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class LLMClientError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.environ.get("SOULAGENT_API_KEY") and os.environ.get("SOULAGENT_MODEL"))


def complete_chat(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 8192,
    timeout_seconds: float = 120.0,
    response_format: dict[str, Any] | None = None,
) -> str:
    """Call an OpenAI-compatible chat endpoint without requiring an SDK."""
    api_key = os.environ.get("SOULAGENT_API_KEY", "").strip()
    model = os.environ.get("SOULAGENT_MODEL", "").strip()
    base_url = os.environ.get("SOULAGENT_BASE_URL", "").strip()
    missing = [
        name
        for name, value in (
            ("SOULAGENT_API_KEY", api_key),
            ("SOULAGENT_MODEL", model),
        )
        if not value
    ]
    if missing:
        raise LLMClientError("缺少解码 API 配置：" + ", ".join(missing))

    endpoint = (base_url or "https://api.openai.com/v1").rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
        decoded = json.loads(body)
        choices = decoded.get("choices") if isinstance(decoded, dict) else None
        choice = choices[0] if choices else {}
        content = choice.get("message", {}).get("content")
        if str(choice.get("finish_reason") or "") == "length":
            raise LLMClientError(
                f"LLM API 输出达到 {max_tokens} tokens 上限，拒绝使用截断结果"
            )
    except urllib.error.HTTPError as exc:
        raise LLMClientError(f"LLM API 返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise LLMClientError(f"无法连接 LLM API：{exc.reason}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LLMClientError(f"无法解析 LLM API 响应：{exc}") from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMClientError("LLM API 返回空文本")
    return content.strip()
