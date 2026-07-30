"""Native Gemini REST client kept local to the LiveFigure skill."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx


class GeminiError(RuntimeError):
    """A safe, user-actionable Gemini configuration or response error."""


@dataclass(frozen=True)
class GeminiConfig:
    base_url: str
    api_key: str
    auth_mode: str = "bearer"
    timeout_s: float = 180.0


class GeminiClient:
    """Call a Gemini native or legacy-compatible ``generateContent`` endpoint."""

    def __init__(self, config: GeminiConfig) -> None:
        self._config = config

    def _endpoint(self, model: str) -> str:
        base = self._config.base_url.strip().rstrip("/")
        if not base:
            raise GeminiError("livefigure.gemini.base_url is not configured")
        if base.endswith(":generateContent"):
            return base
        if not model.strip():
            raise GeminiError("LiveFigure Gemini model is not configured")
        return f"{base}/models/{model}:generateContent"

    def _headers(self) -> dict[str, str]:
        if not self._config.api_key:
            raise GeminiError("livefigure.gemini.api_key is not configured")
        headers = {"Content-Type": "application/json"}
        if self._config.auth_mode == "x-goog-api-key":
            headers["x-goog-api-key"] = self._config.api_key
        else:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    async def generate_image(self, prompt: str, *, model: str, aspect_ratio: str = "16:9") -> bytes:
        data = await self._generate(
            model,
            [{"text": prompt}],
            generation_config={"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": aspect_ratio}},
        )
        for part in _response_parts(data):
            inline = part.get("inlineData") or part.get("inline_data") or {}
            encoded = inline.get("data") if isinstance(inline, dict) else ""
            if encoded:
                try:
                    return base64.b64decode(encoded)
                except ValueError as exc:
                    raise GeminiError("Gemini returned invalid image data") from exc
        raise GeminiError("Gemini response did not contain an image")

    async def generate_text(
        self,
        prompt: str,
        *,
        model: str,
        image_bytes: bytes | None = None,
        image_mime: str = "image/png",
    ) -> str:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if image_bytes:
            parts.append({"inlineData": {"mimeType": image_mime, "data": base64.b64encode(image_bytes).decode("ascii")}})
        data = await self._generate(model, parts, generation_config={"responseModalities": ["TEXT"]})
        text = "".join(str(part.get("text") or "") for part in _response_parts(data)).strip()
        if not text:
            raise GeminiError("Gemini response did not contain any text")
        return text

    async def _generate(self, model: str, parts: list[dict[str, Any]], *, generation_config: dict[str, Any]) -> dict[str, Any]:
        payload = {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation_config}
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_s) as client:
                response = await client.post(self._endpoint(model), headers=self._headers(), json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500].strip()
            raise GeminiError(
                f"Gemini request failed (HTTP {exc.response.status_code}): {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise GeminiError(f"Gemini network request failed: {exc}") from exc
        if not isinstance(data, dict):
            raise GeminiError("Gemini returned a non-JSON object")
        return data


def _response_parts(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return []
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    return [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []
