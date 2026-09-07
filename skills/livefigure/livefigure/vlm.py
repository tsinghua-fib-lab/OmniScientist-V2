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
from urllib.parse import quote, urlparse, urlsplit, urlunsplit
from urllib.request import url2pathname

import httpx

_MAX_REFERENCE_BYTES = 20 * 1024 * 1024
_PROTOCOL = "openai_compatible_chat"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"
_IMAGES_GENERATIONS_SUFFIX = "/images/generations"
_IMAGE_MODEL_REJECT_HINTS = (
    "only imagen models",
    "not supported model for image generation",
    "not an image generation model",
    "unsupported model for image generation",
)
_PREFERRED_IMAGE_MODELS = (
    "gpt-image-2",
    "gpt-image-1",
    "dall-e-3",
    "dall-e-2",
)


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
    image_model: str = ""
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
            image_model=str(values.get("OMNI_VLM_IMAGE_MODEL") or "").strip(),
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

    async def generate_image(
        self,
        prompt: str,
        *,
        size: str = "1536x1024",
        aspect_ratio: str = "16:9",
        image_size: str = "",
    ) -> bytes:
        """Generate the composition reference on the public contract that fits the model."""
        self._validate_config()
        if _should_try_gemini_generate_content(self._config.model, self._config.image_model):
            try:
                return await _generate_gemini_native_image(
                    self._config,
                    prompt,
                    aspect_ratio=aspect_ratio,
                    image_size=image_size,
                    transport=self._transport,
                )
            except VlmError as exc:
                if exc.code == "vlm_authentication_failed":
                    raise
        endpoint = _images_generation_endpoint(self._config.endpoint)
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        models = [_openai_images_start_model(self._config.model, self._config.image_model)]
        tried: set[str] = set()
        last_error: VlmError | None = None
        try:
            async with httpx.AsyncClient(
                timeout=self._config.timeout_s,
                transport=self._transport,
            ) as client:
                while models:
                    model = models.pop(0)
                    if not model or model in tried:
                        continue
                    tried.add(model)
                    payload = {
                        "model": model,
                        "prompt": str(prompt),
                        "n": 1,
                        "size": size,
                    }
                    if not str(model).lower().startswith("gpt-image"):
                        payload["response_format"] = "b64_json"
                    try:
                        response = await client.post(endpoint, headers=headers, json=payload)
                    except httpx.HTTPError:
                        raise VlmError("VLM image network request failed") from None
                    if response.status_code in {401, 403}:
                        raise VlmError(
                            f"VLM image request failed (HTTP {response.status_code})",
                            code="vlm_authentication_failed",
                            category="configuration",
                            retryable=False,
                        )
                    if response.status_code >= 400:
                        rejected = _images_model_rejected(response)
                        fallback = await _discover_images_model(
                            client,
                            images_url=endpoint,
                            headers=headers,
                            exclude=tried,
                        )
                        if fallback:
                            models.append(fallback)
                            continue
                        if rejected:
                            raise VlmError(
                                "OpenAI Images rejected this VLM chat model; it is not an image generator",
                                code="vlm_http_error",
                                category="configuration",
                                retryable=False,
                            )
                        status = response.status_code
                        raise VlmError(
                            f"VLM image request failed (HTTP {status})",
                            code="vlm_http_error",
                            category="configuration" if status in {401, 403} else "network",
                            retryable=status in {408, 409, 425, 429} or status >= 500,
                        )
                    try:
                        data = response.json()
                    except ValueError:
                        data = None
                    image = _response_image(data)
                    if image is not None:
                        return image
                    last_error = VlmError(
                        "VLM image response did not contain a base64 image",
                        code="vlm_invalid_response",
                        category="generation",
                        retryable=True,
                    )
        except VlmError:
            raise
        if last_error is not None:
            raise last_error
        raise VlmError(
            "VLM image response did not contain a base64 image",
            code="vlm_invalid_response",
            category="generation",
            retryable=True,
        )

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


def _resolve_chat_url(endpoint: str) -> str:
    """Expand a site origin the same way Omni's chat client does."""
    parsed = urlsplit(str(endpoint or "").strip())
    path = (parsed.path or "").rstrip("/")
    if path.lower().endswith(_CHAT_COMPLETIONS_SUFFIX):
        new_path = path
    elif not path:
        new_path = "/v1/chat/completions"
    else:
        new_path = f"{path}/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, new_path, parsed.query, ""))


def _images_generation_endpoint(chat_endpoint: str) -> str:
    """Map chat or a site origin to the OpenAI Images sibling URL."""
    raw = str(chat_endpoint or "").strip()
    parsed = urlsplit(raw)
    path = (parsed.path or "").rstrip("/")
    if path.lower().endswith(_IMAGES_GENERATIONS_SUFFIX):
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    parsed = urlsplit(_resolve_chat_url(raw))
    path = (parsed.path or "").rstrip("/")
    if path.lower().endswith(_CHAT_COMPLETIONS_SUFFIX):
        path = path[: -len(_CHAT_COMPLETIONS_SUFFIX)] + _IMAGES_GENERATIONS_SUFFIX
    else:
        path = f"{path}{_IMAGES_GENERATIONS_SUFFIX}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _looks_images_model(name: str) -> bool:
    lowered = str(name or "").lower()
    return (
        lowered.startswith("gpt-image")
        or lowered.startswith("dall-e")
        or "imagen" in lowered
    )


def _looks_gemini_image_model(name: str) -> bool:
    lowered = str(name or "").lower()
    return "gemini" in lowered and "image" in lowered


def _should_try_gemini_generate_content(model: str, image_model: str) -> bool:
    pinned = str(image_model or "").strip()
    if pinned and _looks_images_model(pinned):
        return False
    if pinned and _looks_gemini_image_model(pinned):
        return True
    return _looks_gemini_image_model(model)


def _openai_images_start_model(model: str, image_model: str) -> str:
    pinned = str(image_model or "").strip()
    if pinned:
        return pinned
    chat = str(model or "").strip()
    if _looks_gemini_image_model(chat):
        return _PREFERRED_IMAGE_MODELS[0]
    return chat


def _service_origin(endpoint: str) -> str:
    parsed = urlsplit(str(endpoint or "").strip())
    path = (parsed.path or "").rstrip("/")
    lower = path.lower()
    for suffix in (
        "/v1/chat/completions",
        "/v1/images/generations",
        "/chat/completions",
        "/images/generations",
        "/v1",
    ):
        if lower.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _gemini_generate_content_url(endpoint: str, model: str) -> str:
    origin = _service_origin(endpoint)
    safe_model = quote(str(model or "").strip(), safe="-._~")
    return f"{origin}/v1beta/models/{safe_model}:generateContent"


async def _generate_gemini_native_image(
    config: VlmConfig,
    prompt: str,
    *,
    aspect_ratio: str,
    image_size: str,
    transport: httpx.AsyncBaseTransport | None,
) -> bytes:
    model = str(config.image_model or "").strip() or config.model
    if not _looks_gemini_image_model(model):
        model = config.model
    url = _gemini_generate_content_url(config.endpoint, model)
    image_config = {"aspectRatio": str(aspect_ratio or "16:9").strip() or "16:9"}
    raw = str(image_size or "").strip().upper()
    image_config["imageSize"] = raw if raw in {"1K", "2K", "4K"} else "1K"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": str(prompt)}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": image_config,
            "image": image_config,
        },
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "x-goog-api-key": str(config.api_key),
    }
    try:
        async with httpx.AsyncClient(timeout=config.timeout_s, transport=transport) as client:
            response = await client.post(
                url,
                headers=headers,
                params={"key": config.api_key},
                json=payload,
            )
    except httpx.HTTPError:
        raise VlmError("VLM Gemini image network request failed") from None
    if response.status_code in {401, 403}:
        raise VlmError(
            f"VLM Gemini image request failed (HTTP {response.status_code})",
            code="vlm_authentication_failed",
            category="configuration",
            retryable=False,
        )
    if response.status_code >= 400:
        status = response.status_code
        raise VlmError(
            f"VLM Gemini image request failed (HTTP {status})",
            code="vlm_http_error",
            category="network",
            retryable=status in {408, 409, 425, 429} or status >= 500,
        )
    try:
        data = response.json()
    except ValueError:
        data = None
    image = _gemini_response_image(data)
    if image is None:
        raise VlmError(
            "VLM Gemini image response did not contain an image",
            code="vlm_invalid_response",
            category="generation",
            retryable=True,
        )
    return image


def _gemini_response_image(data: Any) -> bytes | None:
    if not isinstance(data, dict):
        return None
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return None
    content = candidates[0].get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return None
    for part in parts:
        inline = (part.get("inlineData") or part.get("inline_data")) if isinstance(part, dict) else None
        encoded = inline.get("data") if isinstance(inline, dict) else ""
        if not encoded:
            continue
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError:
            continue
        if image:
            return image
    return None


def _images_model_rejected(response: httpx.Response) -> bool:
    try:
        data = response.json()
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    err = data.get("error")
    if isinstance(err, dict):
        text = str(err.get("message") or "")
    elif err:
        text = str(err)
    else:
        text = str(data.get("message") or "")
    lowered = text.lower()
    return any(hint in lowered for hint in _IMAGE_MODEL_REJECT_HINTS)


async def _discover_images_model(
    client: httpx.AsyncClient,
    *,
    images_url: str,
    headers: dict[str, str],
    exclude: set[str],
) -> str:
    parsed = urlsplit(images_url)
    path = (parsed.path or "").rstrip("/")
    if path.lower().endswith(_IMAGES_GENERATIONS_SUFFIX):
        models_path = path[: -len(_IMAGES_GENERATIONS_SUFFIX)] + "/models"
    else:
        models_path = "/v1/models"
    models_url = urlunsplit((parsed.scheme, parsed.netloc, models_path, "", ""))
    try:
        response = await client.get(models_url, headers=headers)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return next((name for name in _PREFERRED_IMAGE_MODELS if name not in exclude), "")
    rows = data.get("data") if isinstance(data, dict) else None
    names = [
        str(item.get("id") or "").strip()
        for item in (rows or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    available = {name for name in names if name not in exclude}
    for preferred in _PREFERRED_IMAGE_MODELS:
        if preferred in available:
            return preferred
    catalog_images = [name for name in names if name not in exclude and _looks_images_model(name)]
    if catalog_images:
        return catalog_images[0]
    if any(_looks_images_model(name) for name in names):
        return ""
    return next((name for name in _PREFERRED_IMAGE_MODELS if name not in exclude), "")


def _response_image(data: Any) -> bytes | None:
    """Decode a standard OpenAI Images ``b64_json`` response."""
    if not isinstance(data, dict):
        return None
    items = data.get("data")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    encoded = str(items[0].get("b64_json") or "")
    if not encoded:
        return None
    try:
        value = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None
    return value or None


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
    if _is_windows_drive_path(value):
        # ``urlparse`` reads a native Windows drive letter as a URI scheme.
        path = Path(value).expanduser()
    elif parsed.scheme == "file":
        path = Path(url2pathname(parsed.path))
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


def _is_windows_drive_path(value: str) -> bool:
    """Whether ``value`` begins with a native Windows drive prefix."""
    return len(value) >= 3 and value[0].isalpha() and value[1:3] in {":\\", ":/"}


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
