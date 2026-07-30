"""Portable OpenAI-compatible multimodal client for LiveFigure.

The public environment contract is intentionally small and provider-neutral:
``OMNI_VLM_MODEL``, ``OMNI_VLM_ENDPOINT``, and ``OMNI_VLM_API_KEY``.  The
endpoint is a complete chat-completions URL and authentication uses a bearer
token.  Native provider protocols belong in explicit legacy adapters.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

_MAX_REFERENCE_BYTES = 20 * 1024 * 1024
_PROTOCOL = "openai_compatible_chat"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class VlmError(RuntimeError):
    """A redacted VLM error with harness-friendly classification."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "vlm_request_failed",
        category: str = "network",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.retryable = retryable


@dataclass(frozen=True)
class VlmConfig:
    """One run's fixed OpenAI-compatible multimodal configuration."""

    model: str
    endpoint: str
    api_key: str = field(repr=False)
    timeout_s: float = 180.0
    protocol: str = _PROTOCOL
    # Local references are opt-in. Omni supplies artifact/attachment paths;
    # the portable runner supplies its current/output directories.
    reference_roots: tuple[Path, ...] = field(default_factory=tuple, repr=False)
    reference_files: tuple[Path, ...] = field(default_factory=tuple, repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> VlmConfig:
        """Read the portable three-variable contract without mutating it."""
        values = os.environ if environ is None else environ
        return cls(
            model=str(values.get("OMNI_VLM_MODEL") or "").strip(),
            endpoint=str(values.get("OMNI_VLM_ENDPOINT") or "").strip(),
            api_key=str(values.get("OMNI_VLM_API_KEY") or "").strip(),
        )

    def missing_env(self) -> tuple[str, ...]:
        """Return names, never values, for incomplete configuration."""
        missing: list[str] = []
        if not self.model:
            missing.append("OMNI_VLM_MODEL")
        if not self.endpoint:
            missing.append("OMNI_VLM_ENDPOINT")
        if not self.api_key:
            missing.append("OMNI_VLM_API_KEY")
        return tuple(missing)


class VlmClient:
    """Call one complete OpenAI-compatible multimodal chat endpoint."""

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
        """Generate text, optionally attaching a data URL or local image."""
        self._validate_config()
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if reference_image_uri:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": reference_as_data_url(
                            reference_image_uri,
                            allowed_roots=self._config.reference_roots,
                            allowed_files=self._config.reference_files,
                        )
                    },
                }
            )
        payload = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
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
            retryable = status in {408, 409, 425, 429} or status >= 500
            category = "configuration" if status in {401, 403} else "network"
            code = "vlm_authentication_failed" if status in {401, 403} else "vlm_http_error"
            # Provider response bodies are intentionally omitted: they may echo
            # request headers or credentials.
            raise VlmError(
                f"VLM request failed (HTTP {status})",
                code=code,
                category=category,
                retryable=retryable,
            ) from None
        except (httpx.HTTPError, ValueError):
            # Avoid embedding exception strings because URLs may contain secret
            # query parameters in misconfigured third-party gateways.
            raise VlmError("VLM network request failed") from None

        text = _response_text(data)
        if not text:
            raise VlmError(
                "VLM response did not contain text",
                code="vlm_invalid_response",
                category="generation",
                retryable=True,
            )
        return text

    def _validate_config(self) -> None:
        missing = self._config.missing_env()
        if missing:
            raise VlmError(
                "VLM configuration is incomplete; configure " + ", ".join(missing),
                code="vlm_not_configured",
                category="configuration",
                retryable=False,
            )
        if self._config.protocol != _PROTOCOL:
            raise VlmError(
                f"Unsupported VLM protocol: {self._config.protocol}",
                code="vlm_protocol_unsupported",
                category="configuration",
                retryable=False,
            )
        parsed = urlparse(self._config.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise VlmError(
                "OMNI_VLM_ENDPOINT must be a complete HTTP(S) chat-completions URL",
                code="vlm_endpoint_invalid",
                category="configuration",
                retryable=False,
            )
        if parsed.username or parsed.password or parsed.fragment:
            raise VlmError(
                "OMNI_VLM_ENDPOINT must not contain embedded credentials or a URL fragment",
                code="vlm_endpoint_invalid",
                category="configuration",
                retryable=False,
            )
        if parsed.scheme == "http" and str(parsed.hostname or "").lower() not in _LOOPBACK_HOSTS:
            raise VlmError(
                "OMNI_VLM_ENDPOINT must use HTTPS; plain HTTP is allowed only for loopback",
                code="vlm_endpoint_insecure",
                category="configuration",
                retryable=False,
            )


def reference_as_data_url(
    uri: str,
    *,
    allowed_roots: tuple[Path, ...] = (),
    allowed_files: tuple[Path, ...] = (),
) -> str:
    """Return an image data URL without performing a network fetch."""
    value = str(uri or "").strip()
    if not value:
        raise VlmError(
            "Reference image URI is empty",
            code="reference_image_invalid",
            category="input",
            retryable=False,
        )
    if value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        if (
            not separator
            or not header.lower().startswith("data:image/")
            or ";base64" not in header.lower()
        ):
            raise VlmError(
                "Reference must be a base64 image data URL",
                code="reference_image_invalid",
                category="input",
                retryable=False,
            )
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except ValueError:
            decoded = b""
        mime = header[5:].split(";", 1)[0].lower()
        if (
            not decoded
            or len(decoded) > _MAX_REFERENCE_BYTES
            or not _valid_image_bytes(decoded, mime)
        ):
            raise VlmError(
                "Reference image data is invalid or exceeds 20 MiB",
                code="reference_image_invalid",
                category="input",
                retryable=False,
            )
        return value

    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        raise VlmError(
            "Remote reference images are not fetched; use a data URL or local file",
            code="reference_image_remote_forbidden",
            category="input",
            retryable=False,
        )
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
    elif parsed.scheme:
        raise VlmError(
            "Unsupported reference image URI; use a data URL or local file",
            code="reference_image_invalid",
            category="input",
            retryable=False,
        )
    else:
        path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=True)
        resolved_roots = tuple(root.expanduser().resolve() for root in allowed_roots)
        resolved_files = tuple(item.expanduser().resolve() for item in allowed_files)
        allowed = resolved in resolved_files or any(
            resolved == root or root in resolved.parents for root in resolved_roots
        )
        if not allowed:
            raise VlmError(
                "Local reference image is outside the allowed workspace/attachment paths",
                code="reference_image_forbidden",
                category="input",
                retryable=False,
            )
        size = resolved.stat().st_size
        if not resolved.is_file() or size <= 0 or size > _MAX_REFERENCE_BYTES:
            raise OSError
        raw = resolved.read_bytes()
    except VlmError:
        raise
    except OSError:
        raise VlmError(
            "Reference image is unavailable or exceeds 20 MiB",
            code="reference_image_unavailable",
            category="input",
            retryable=False,
        ) from None
    mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    if not mime.startswith("image/"):
        raise VlmError(
            "Reference file must use a recognized image extension",
            code="reference_image_invalid",
            category="input",
            retryable=False,
        )
    if not _valid_image_bytes(raw, mime):
        raise VlmError(
            "Reference file content is not a supported PNG, JPEG, GIF, or WebP image",
            code="reference_image_invalid",
            category="input",
            retryable=False,
        )
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _valid_image_bytes(raw: bytes, mime: str) -> bool:
    value = mime.lower()
    if value == "image/png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if value in {"image/jpeg", "image/jpg"}:
        return raw.startswith(b"\xff\xd8\xff")
    if value == "image/gif":
        return raw.startswith((b"GIF87a", b"GIF89a"))
    if value == "image/webp":
        return len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
    return False


def reference_bytes(
    uri: str,
    *,
    allowed_roots: tuple[Path, ...] = (),
    allowed_files: tuple[Path, ...] = (),
) -> tuple[bytes, str]:
    """Decode a supported reference without fetching remote content."""
    data_url = reference_as_data_url(
        uri,
        allowed_roots=allowed_roots,
        allowed_files=allowed_files,
    )
    header, _, encoded = data_url.partition(",")
    mime = header[5:].split(";", 1)[0]
    return base64.b64decode(encoded), mime


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
            if isinstance(part, dict) and part.get("type") in {None, "text"}
        ).strip()
    return ""


__all__ = [
    "VlmClient",
    "VlmConfig",
    "VlmError",
    "reference_as_data_url",
    "reference_bytes",
]
