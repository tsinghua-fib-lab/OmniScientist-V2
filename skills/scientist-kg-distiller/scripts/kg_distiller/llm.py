from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path
from typing import Any, Protocol


class JsonLLM(Protocol):
    def complete_json(self, *, system: str, user: str) -> Any: ...


class OpenAIJsonLLM:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 600.0,
        reasoning_effort: str | None = None,
    ):
        from openai import OpenAI

        resolved_base, resolved_key = resolve_connection(base_url, api_key)
        self.model = model or os.environ.get("KG_DISTILLER_MODEL", "gpt-5.4-1m")
        self.reasoning_effort = reasoning_effort or os.environ.get(
            "KG_DISTILLER_REASONING_EFFORT", "low"
        )
        configured_timeout = float(
            os.environ.get("KG_DISTILLER_LLM_TIMEOUT", str(timeout))
        )
        configured_retries = int(
            os.environ.get("KG_DISTILLER_LLM_MAX_RETRIES", "2")
        )
        self.client = OpenAI(
            api_key=resolved_key,
            base_url=resolved_base,
            timeout=configured_timeout,
            max_retries=configured_retries,
        )

    def complete_json(self, *, system: str, user: str) -> Any:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            reasoning_effort=self.reasoning_effort,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("LLM returned an empty response")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM returned invalid JSON: {exc}") from exc


def resolve_connection(
    base_url: str | None = None, api_key: str | None = None
) -> tuple[str | None, str]:
    if api_key or os.environ.get("OPENAI_API_KEY"):
        return base_url or os.environ.get("OPENAI_BASE_URL"), api_key or os.environ["OPENAI_API_KEY"]

    proxy_config = Path.home() / "Desktop" / "codex反代" / "config.yaml"
    if proxy_config.exists() and _port_is_available("127.0.0.1", 8317):
        text = proxy_config.read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^api-keys:\s*\n(?:\s*#.*\n)*\s*-\s*[\"']?([^\"'\s]+)",
            text,
        )
        if match:
            return "http://127.0.0.1:8317/v1", match.group(1)

    raise RuntimeError(
        "No LLM connection found. Set OPENAI_API_KEY (and optionally OPENAI_BASE_URL), "
        "or start the configured CLIProxyAPI on 127.0.0.1:8317."
    )


def _port_is_available(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False
