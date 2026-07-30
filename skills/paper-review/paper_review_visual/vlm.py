"""Portable OpenAI-compatible VLM adapter for paper-review visual analysis."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class VlmError(RuntimeError):
    """A provider-neutral error that never includes credentials."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "vlm_request_failed",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class VlmConfig:
    """Generic OpenAI-compatible chat-completions configuration."""

    model: str
    endpoint: str
    api_key: str = field(repr=False)
    timeout_s: float = 180.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> VlmConfig:
        """Read the portable three-variable contract."""
        values = os.environ if environ is None else environ
        return cls(
            model=str(values.get("OMNI_VLM_MODEL") or "").strip(),
            endpoint=str(values.get("OMNI_VLM_ENDPOINT") or "").strip(),
            api_key=str(values.get("OMNI_VLM_API_KEY") or "").strip(),
        )

    def missing_env(self) -> tuple[str, ...]:
        """Return missing variable names without returning values."""
        missing: list[str] = []
        if not self.model:
            missing.append("OMNI_VLM_MODEL")
        if not self.endpoint:
            missing.append("OMNI_VLM_ENDPOINT")
        if not self.api_key:
            missing.append("OMNI_VLM_API_KEY")
        return tuple(missing)


class VlmClient:
    """Send one extracted crop to an OpenAI-compatible VLM endpoint."""

    def __init__(
        self,
        config: VlmConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport

    async def generate_text(
        self,
        prompt: str,
        *,
        reference_image_uri: str | None = None,
    ) -> str:
        """Return response text for one base64 image data URL."""
        self._validate()
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if reference_image_uri:
            if not reference_image_uri.startswith("data:image/"):
                raise VlmError(
                    "Portable visual review accepts only base64 image data URLs.",
                    code="reference_image_invalid",
                    retryable=False,
                )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": reference_image_uri},
                }
            )
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": content}],
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._config.timeout_s,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._config.endpoint,
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 400 and reference_image_uri:
                raise VlmError(
                    "VLM endpoint rejected the image request (HTTP 400); verify that "
                    "the configured model supports image input.",
                    code="vlm_image_input_rejected",
                    retryable=False,
                ) from None
            raise VlmError(
                f"VLM request failed (HTTP {status}).",
                code=(
                    "vlm_authentication_failed"
                    if status in {401, 403}
                    else "vlm_http_error"
                ),
                retryable=status in {408, 409, 425, 429} or status >= 500,
            ) from None
        except (httpx.HTTPError, ValueError):
            raise VlmError("VLM network request failed.") from None

        text = _response_text(data)
        if not text:
            raise VlmError(
                "VLM response did not contain text.",
                code="vlm_invalid_response",
            )
        return text

    def _validate(self) -> None:
        missing = self._config.missing_env()
        if missing:
            raise VlmError(
                "VLM configuration is incomplete; missing " + ", ".join(missing),
                code="vlm_not_configured",
                retryable=False,
            )
        parsed = urlparse(self._config.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise VlmError(
                "OMNI_VLM_ENDPOINT must be a complete HTTP(S) URL.",
                code="vlm_endpoint_invalid",
                retryable=False,
            )
        if parsed.username or parsed.password or parsed.fragment:
            raise VlmError(
                "OMNI_VLM_ENDPOINT must not contain embedded credentials or a fragment.",
                code="vlm_endpoint_invalid",
                retryable=False,
            )
        if (
            parsed.scheme == "http"
            and str(parsed.hostname or "").lower() not in _LOOPBACK_HOSTS
        ):
            raise VlmError(
                "OMNI_VLM_ENDPOINT must use HTTPS except for loopback.",
                code="vlm_endpoint_insecure",
                retryable=False,
            )


def _response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "").strip()
            for item in content
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        )
    return ""
