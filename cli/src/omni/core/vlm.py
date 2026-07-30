"""Owner-controlled VLM endpoint validation and offline-testable health probe."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from omni.config.settings import VlmCfg

_SUPPORTED_PROTOCOL = "openai_compatible_chat"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_PROBE_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class VlmServiceError(RuntimeError):
    """Safe host-service failure; its message never includes provider bodies."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        category: str,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.safe_message = message
        self.code = code
        self.category = category
        self.retryable = retryable


class VlmGateway:
    """Owner-controlled VLM host service injected as a narrow generation port."""

    setup_command = "omni config vlm"

    def __init__(
        self,
        config: VlmCfg,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport

    @property
    def available(self) -> bool:
        """Return whether the complete supported configuration is enabled."""
        return not self.missing and not self.configuration_error

    @property
    def error_code(self) -> str:
        """Distinguish absent fields from a complete but unsafe configuration."""
        return (
            "vlm_invalid_configuration"
            if self.configuration_error
            else "vlm_not_configured"
        )

    @property
    def missing(self) -> tuple[str, ...]:
        """Return field names only; credential values never leave the service."""
        if not self._config.enabled:
            return ("model", "endpoint", "api_key")
        return tuple(
            name
            for name, value in (
                ("model", self._config.model),
                ("endpoint", self._config.endpoint),
                ("api_key", self._config.api_key),
            )
            if not str(value or "").strip()
        )

    @property
    def configuration_error(self) -> str:
        """Return one safe validation error for a complete but invalid config."""
        if self.missing:
            return ""
        try:
            validate_vlm_protocol(self._config.protocol)
            validate_vlm_endpoint(self._config.endpoint)
        except ValueError as exc:
            return str(exc)
        return ""

    async def generate_text(
        self,
        prompt: str,
        *,
        reference_image_uri: str | None = None,
    ) -> str:
        """Call the configured provider without exposing endpoint credentials."""
        if not self.available:
            detail = self.configuration_error
            if not detail:
                detail = "VLM configuration is incomplete; missing: " + ", ".join(self.missing) + "."
            raise VlmServiceError(
                detail,
                code=self.error_code,
                category="configuration",
                retryable=False,
            )

        content: list[dict[str, Any]] = [{"type": "text", "text": str(prompt)}]
        if reference_image_uri:
            reference = str(reference_image_uri).strip()
            if not reference.lower().startswith("data:image/") or ";base64," not in reference[:128].lower():
                raise VlmServiceError(
                    "VLM reference must be a validated image data URL.",
                    code="reference_image_invalid",
                    category="input",
                    retryable=False,
                )
            content.append(
                {"type": "image_url", "image_url": {"url": reference}}
            )
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
                    headers={
                        "Authorization": f"Bearer {self._config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                raise VlmServiceError(
                    f"VLM authentication failed (HTTP {status}); check the API key.",
                    code="vlm_authentication_failed",
                    category="configuration",
                    retryable=False,
                ) from None
            raise VlmServiceError(
                f"VLM endpoint returned HTTP {status}.",
                code="vlm_http_error",
                category="network",
                retryable=status in {408, 409, 425, 429} or status >= 500,
            ) from None
        except httpx.HTTPError:
            raise VlmServiceError(
                "VLM network request failed; check the endpoint and network access.",
                code="vlm_request_failed",
                category="network",
                retryable=True,
            ) from None
        try:
            data = response.json()
        except ValueError:
            raise VlmServiceError(
                "VLM endpoint returned invalid JSON.",
                code="vlm_invalid_response",
                category="generation",
                retryable=True,
            ) from None
        text = _response_text(data)
        if not text:
            raise VlmServiceError(
                "VLM endpoint responded, but no text choice was returned.",
                code="vlm_invalid_response",
                category="generation",
                retryable=True,
            )
        return text


def validate_vlm_endpoint(endpoint: str) -> None:
    """Require a complete HTTPS URL, allowing plain HTTP only on loopback."""
    value = str(endpoint or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("VLM endpoint must be a complete HTTP(S) URL.")
    if parsed.username or parsed.password:
        raise ValueError("VLM endpoint must not contain embedded credentials.")
    if parsed.fragment:
        raise ValueError("VLM endpoint must not contain a URL fragment.")
    if parsed.scheme == "http" and parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ValueError("VLM endpoint must use HTTPS; plain HTTP is allowed only for loopback.")


def validate_vlm_protocol(protocol: str) -> None:
    """Accept the single generic protocol exposed by the new VLM namespace."""
    if str(protocol or "").strip() != _SUPPORTED_PROTOCOL:
        raise ValueError("Unsupported VLM protocol; use openai_compatible_chat.")


async def check_vlm_connectivity(
    config: VlmCfg,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, str]:
    """Probe the configured multimodal chat endpoint without exposing secrets."""
    if not config.enabled:
        return False, "VLM is disabled; run `omni config vlm --enable`."
    missing = [
        name
        for name, value in (
            ("model", config.model),
            ("endpoint", config.endpoint),
            ("API key", config.api_key),
        )
        if not str(value or "").strip()
    ]
    if missing:
        return False, "VLM configuration is incomplete; missing: " + ", ".join(missing) + "."
    try:
        validate_vlm_protocol(config.protocol)
        validate_vlm_endpoint(config.endpoint)
    except ValueError as exc:
        return False, str(exc)

    try:
        await VlmGateway(config, transport=transport).generate_text(
            "Reply with one word describing this image.",
            reference_image_uri=_PROBE_IMAGE,
        )
    except VlmServiceError as exc:
        return False, exc.safe_message
    return True, "VLM multimodal configuration verified."


def _response_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        ).strip()
    return ""


__all__ = [
    "VlmGateway",
    "VlmServiceError",
    "check_vlm_connectivity",
    "validate_vlm_endpoint",
    "validate_vlm_protocol",
]
