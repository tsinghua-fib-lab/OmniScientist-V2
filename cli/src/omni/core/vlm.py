"""Owner-controlled VLM endpoint validation and offline-testable health probe."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from omni.config.settings import LiveFigureGeminiCfg, VlmCfg

_SUPPORTED_PROTOCOL = "openai_compatible_chat"
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
        livefigure_gemini: LiveFigureGeminiCfg | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._livefigure_gemini = livefigure_gemini or LiveFigureGeminiCfg()
        self._transport = transport
        self.last_images_model = ""

    @property
    def available(self) -> bool:
        """Return whether the complete supported configuration is enabled."""
        return bool(self._config.enabled) and not self.missing and not self.configuration_error

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
        """Return field names only; credential values never leave the service.

        ``enabled=false`` is not reported as missing ``model`` / ``endpoint`` /
        ``api_key``. Those fields may already be on disk; admission should say
        the VLM is not enabled rather than pretend the three fields are absent.
        """
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

    def image_generation_environment(self) -> dict[str, str]:
        """Return one-run credentials for a trusted LiveFigure icon helper.

        LiveFigure executes owner-authorized Python that can request isolated
        visual assets.  Preserve the legacy native Gemini wire contract when
        the owner has retained that section. Gemini image chat models also
        expose ``generateContent`` so the helper does not POST them to
        OpenAI Images.
        """
        if self._legacy_image_available:
            return {
                "LIVEFIGURE_GEMINI_IMAGE_URL": self._livefigure_gemini.base_url,
                "LIVEFIGURE_GEMINI_API_KEY": self._livefigure_gemini.api_key,
            }
        if not self.available:
            return {}
        env = {
            "LIVEFIGURE_IMAGE_ENDPOINT": images_generation_url(self._config.endpoint),
            "LIVEFIGURE_IMAGE_MODEL": self._openai_images_model(),
            "LIVEFIGURE_IMAGE_API_KEY": str(self._config.api_key),
            "LIVEFIGURE_IMAGE_TIMEOUT_S": str(self._config.timeout_s),
        }
        if self._should_try_gemini_generate_content():
            env["LIVEFIGURE_GEMINI_IMAGE_URL"] = gemini_generate_content_url(
                self._config.endpoint,
                self._gemini_image_model(),
            )
            env["LIVEFIGURE_GEMINI_API_KEY"] = str(self._config.api_key)
        return env

    async def generate_text(
        self,
        prompt: str,
        *,
        reference_image_uri: str | None = None,
    ) -> str:
        """Call the configured provider without exposing endpoint credentials."""
        if not self.available:
            if not self._config.enabled:
                detail = "VLM is not enabled."
            else:
                detail = self.configuration_error
                if not detail:
                    detail = (
                        "VLM configuration is incomplete; missing: "
                        + ", ".join(self.missing)
                        + "."
                    )
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
                    resolve_vlm_request_url(self._config.endpoint),
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
            if status == 400 and reference_image_uri:
                raise VlmServiceError(
                    "VLM endpoint rejected the image request (HTTP 400); verify that "
                    "the configured model supports image input.",
                    code="vlm_image_input_rejected",
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
                "VLM endpoint returned invalid JSON, not a chat-completions body.",
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

    async def generate_image(
        self,
        prompt: str,
        *,
        size: str = "1536x1024",
        aspect_ratio: str = "16:9",
        image_size: str = "",
    ) -> bytes:
        """Generate a reference image on the public contract that fits the model.

        Gemini image models use ``generateContent`` (Google + 汇云 native).
        GPT Image / DALL·E use OpenAI ``/v1/images/generations``. A Gemini
        native miss falls through to Images so a pinned or catalog
        ``gpt-image-2`` can still pay livefigure.
        """
        if self._legacy_image_available:
            return await self._generate_legacy_livefigure_image(
                prompt,
                aspect_ratio=aspect_ratio,
                image_size=image_size,
            )
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
        if self._should_try_gemini_generate_content():
            try:
                return await self._generate_gemini_native_image(
                    prompt,
                    aspect_ratio=aspect_ratio,
                    image_size=image_size,
                )
            except VlmServiceError as exc:
                if exc.code == "vlm_authentication_failed":
                    raise
        return await self._generate_openai_images(prompt, size=size)

    def _images_model(self) -> str:
        """Images API model. Chat VLMs (Gemini preview, etc.) are often not valid here."""
        return self._openai_images_model()

    def _openai_images_model(self) -> str:
        pinned = str(getattr(self._config, "image_model", "") or "").strip()
        if pinned:
            return pinned
        chat = str(self._config.model or "").strip()
        if _looks_gemini_image_model(chat):
            return _PREFERRED_IMAGE_MODELS[0]
        return chat

    def _gemini_image_model(self) -> str:
        pinned = str(getattr(self._config, "image_model", "") or "").strip()
        if pinned and _looks_gemini_image_model(pinned):
            return pinned
        return str(self._config.model or "").strip()

    def _should_try_gemini_generate_content(self) -> bool:
        pinned = str(getattr(self._config, "image_model", "") or "").strip()
        if pinned and _looks_openai_images_model(pinned):
            return False
        if pinned and _looks_gemini_image_model(pinned):
            return True
        return _looks_gemini_image_model(self._config.model)

    async def _generate_gemini_native_image(
        self,
        prompt: str,
        *,
        aspect_ratio: str,
        image_size: str,
    ) -> bytes:
        """POST Google/汇云 ``generateContent`` without exposing the API key."""
        model = self._gemini_image_model()
        url = gemini_generate_content_url(self._config.endpoint, model)
        image_config = _gemini_image_config(aspect_ratio, image_size)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": str(prompt)}]}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": image_config,
                "image": image_config,
            },
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
            "x-goog-api-key": str(self._config.api_key),
        }
        display = _safe_url(url)
        try:
            async with httpx.AsyncClient(
                timeout=self._config.timeout_s,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    params={"key": self._config.api_key},
                    json=payload,
                )
        except httpx.HTTPError:
            raise VlmServiceError(
                f"VLM Gemini image path {display} network request failed.",
                code="vlm_request_failed",
                category="network",
                retryable=True,
            ) from None
        if response.status_code in {401, 403}:
            raise VlmServiceError(
                f"VLM Gemini image authentication failed (HTTP {response.status_code}); check the API key.",
                code="vlm_authentication_failed",
                category="configuration",
                retryable=False,
            )
        if response.status_code >= 400:
            status = response.status_code
            raise VlmServiceError(
                f"VLM Gemini image path {display} returned HTTP {status}.",
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
            raise VlmServiceError(
                f"VLM Gemini image path {display} responded, but no inline image was returned.",
                code="vlm_invalid_response",
                category="generation",
                retryable=True,
            )
        self.last_images_model = model
        return image

    async def _generate_openai_images(self, prompt: str, *, size: str) -> bytes:
        url = images_generation_url(self._config.endpoint)
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        models = [self._openai_images_model()]
        tried: set[str] = set()
        last_error: VlmServiceError | None = None
        async with httpx.AsyncClient(
            timeout=self._config.timeout_s,
            transport=self._transport,
        ) as client:
            while models:
                model = models.pop(0)
                if not model or model in tried:
                    continue
                tried.add(model)
                payload: dict[str, Any] = {
                    "model": model,
                    "prompt": str(prompt),
                    "n": 1,
                    "size": size,
                }
                if not str(model).lower().startswith("gpt-image"):
                    payload["response_format"] = "b64_json"
                try:
                    response = await client.post(url, headers=headers, json=payload)
                except httpx.HTTPError:
                    raise VlmServiceError(
                        "VLM image network request failed; check the endpoint and network access.",
                        code="vlm_request_failed",
                        category="network",
                        retryable=True,
                    ) from None
                if response.status_code in {401, 403}:
                    raise VlmServiceError(
                        f"VLM image authentication failed (HTTP {response.status_code}); check the API key.",
                        code="vlm_authentication_failed",
                        category="configuration",
                        retryable=False,
                    )
                if response.status_code >= 400:
                    rejected = _images_model_rejected(response)
                    fallback = await _discover_images_model(
                        client,
                        images_url=url,
                        headers=headers,
                        exclude=tried,
                    )
                    if fallback:
                        models.append(fallback)
                        continue
                    if rejected:
                        raise VlmServiceError(
                            "OpenAI Images rejected this VLM chat model; it is not an "
                            "image generator. Set `omni config vlm --image-model "
                            "gpt-image-2` (or another model that implements "
                            "/v1/images/generations).",
                            code="vlm_http_error",
                            category="configuration",
                            retryable=False,
                        )
                    status = response.status_code
                    tried_note = ", ".join(tried) if tried else "the configured model"
                    raise VlmServiceError(
                        f"VLM image endpoint returned HTTP {status} after trying {tried_note}.",
                        code="vlm_http_error",
                        category="network",
                        retryable=status in {408, 409, 425, 429} or status >= 500,
                    )
                try:
                    data = response.json()
                except ValueError:
                    data = None
                image = _response_image(data)
                if image is not None:
                    self.last_images_model = model
                    return image
                last_error = VlmServiceError(
                    "VLM image endpoint responded, but no base64 image was returned.",
                    code="vlm_invalid_response",
                    category="generation",
                    retryable=True,
                )
        if last_error is not None:
            raise last_error
        raise VlmServiceError(
            "VLM image endpoint responded, but no base64 image was returned.",
            code="vlm_invalid_response",
            category="generation",
            retryable=True,
        )

    @property
    def _legacy_image_available(self) -> bool:
        cfg = self._livefigure_gemini
        return bool(cfg.enabled and cfg.base_url.strip() and cfg.api_key.strip())

    async def _generate_legacy_livefigure_image(
        self,
        prompt: str,
        *,
        aspect_ratio: str = "16:9",
        image_size: str = "",
    ) -> bytes:
        """Reuse the old Gemini ``generateContent`` reference-image call."""
        cfg = self._livefigure_gemini
        headers = {"Content-Type": "application/json"}
        if cfg.auth_mode == "x-goog-api-key":
            headers["x-goog-api-key"] = cfg.api_key
        else:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        image_config: dict[str, str] = {"aspectRatio": aspect_ratio}
        if image_size:
            image_config["imageSize"] = image_size
        payload = {
            "contents": [{"role": "user", "parts": [{"text": str(prompt)}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": image_config,
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=cfg.timeout_s,
                transport=self._transport,
            ) as client:
                response = await client.post(cfg.base_url, headers=headers, json=payload)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise VlmServiceError(
                f"LiveFigure Gemini image endpoint returned HTTP {status}.",
                code="vlm_authentication_failed" if status in {401, 403} else "vlm_http_error",
                category="configuration" if status in {401, 403} else "network",
                retryable=status in {408, 409, 425, 429} or status >= 500,
            ) from None
        except httpx.HTTPError:
            raise VlmServiceError(
                "LiveFigure Gemini image network request failed.",
                code="vlm_request_failed",
                category="network",
                retryable=True,
            ) from None
        try:
            data = response.json()
        except ValueError:
            data = None
        image = _gemini_response_image(data)
        if image is None:
            raise VlmServiceError(
                "LiveFigure Gemini response did not contain an image.",
                code="vlm_invalid_response",
                category="generation",
                retryable=True,
            )
        self.last_images_model = "legacy_gemini"
        return image


def images_generation_url(endpoint: str) -> str:
    """Derive the OpenAI Images URL from a chat URL or site origin.

    Chat is ``{api_base}/v1/chat/completions``. Images is the sibling
    ``{api_base}/v1/images/generations``. A stored origin such as
    ``https://gateway.example`` is expanded the same way as chat, so we never
    POST the site homepage at ``/images/generations``.
    """
    raw = str(endpoint or "").strip()
    parsed = urlsplit(raw)
    path = (parsed.path or "").rstrip("/")
    if path.lower().endswith(_IMAGES_GENERATIONS_SUFFIX):
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    parsed = urlsplit(resolve_vlm_request_url(raw))
    path = (parsed.path or "").rstrip("/")
    if path.lower().endswith(_CHAT_COMPLETIONS_SUFFIX):
        path = path[: -len(_CHAT_COMPLETIONS_SUFFIX)] + _IMAGES_GENERATIONS_SUFFIX
    else:
        path = f"{path}{_IMAGES_GENERATIONS_SUFFIX}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def gemini_generate_content_url(endpoint: str, model: str) -> str:
    """Derive Google/汇云 ``generateContent`` from a chat URL or site origin."""
    origin = _service_origin(endpoint)
    safe_model = quote(str(model or "").strip(), safe="-._~")
    return f"{origin}/v1beta/models/{safe_model}:generateContent"


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


def _safe_url(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _images_generation_endpoint(chat_endpoint: str) -> str:
    """Backward-compatible alias for :func:`images_generation_url`."""
    return images_generation_url(chat_endpoint)


def _looks_openai_images_model(name: str) -> bool:
    return _looks_images_model(name)


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


def _gemini_image_config(aspect_ratio: str, image_size: str) -> dict[str, str]:
    config = {"aspectRatio": str(aspect_ratio or "16:9").strip() or "16:9"}
    raw = str(image_size or "").strip().upper()
    if raw in {"1K", "2K", "4K"}:
        config["imageSize"] = raw
    else:
        config["imageSize"] = "1K"
    return config


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
    """Pick an Images-capable model from the same OpenAI-compatible /v1/models list."""
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
    # Catalog advertised only chat VLMs. Still try known OpenAI Images ids.
    return next((name for name in _PREFERRED_IMAGE_MODELS if name not in exclude), "")


def resolve_vlm_request_url(endpoint: str) -> str:
    """Expand a Claude-Code-style base URL into a chat-completions request URL.

    A site origin (``https://host``) becomes ``https://host/v1/chat/completions``.
    A versioned base (``https://host/v1``) appends ``/chat/completions``, matching
    the main-model client. An already-complete ``.../chat/completions`` path is
    left unchanged so existing saved URLs keep working.
    """
    parsed = urlsplit(str(endpoint or "").strip())
    path = (parsed.path or "").rstrip("/")
    lower = path.lower()
    if lower.endswith("/chat/completions"):
        new_path = path
    elif not path:
        new_path = "/v1/chat/completions"
    else:
        new_path = f"{path}/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, new_path, parsed.query, ""))


def validate_vlm_endpoint(endpoint: str) -> None:
    """Require an HTTPS (or loopback HTTP) base URL or chat-completions URL."""
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
    return True, (
        "VLM multimodal chat verified: the model accepted an image-input probe. "
        "This does not test /v1/images/generations (livefigure)."
    )


async def check_vlm_images_connectivity(
    config: VlmCfg,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, str]:
    """Probe OpenAI Images generation used by livefigure, without exposing secrets."""
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

    gateway = VlmGateway(config, transport=transport)
    if gateway._should_try_gemini_generate_content():
        display = _safe_url(
            gemini_generate_content_url(config.endpoint, gateway._gemini_image_model())
        )
    else:
        display = _safe_url(images_generation_url(config.endpoint))
    try:
        image = await gateway.generate_image(
            "A 1x1 gray square used only as an Images connectivity probe."
        )
    except VlmServiceError as exc:
        return False, f"VLM image path {display} failed: {exc.safe_message}"
    if not image:
        return False, f"VLM image path {display} returned no image bytes."
    model = gateway.last_images_model or gateway._images_model()
    return True, (
        f"VLM image generation verified at {display} using {model} "
        f"({len(image)} bytes)."
    )


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


def _response_image(data: Any) -> bytes | None:
    """Decode the standard OpenAI Images response without fetching URLs."""
    if not isinstance(data, dict):
        return None
    items = data.get("data")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    encoded = str(items[0].get("b64_json") or "")
    if not encoded:
        return None
    try:
        image = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None
    return image or None


def _gemini_response_image(data: Any) -> bytes | None:
    """Decode an inline image part returned by Gemini ``generateContent``."""
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
        inline = None
        if isinstance(part, dict):
            inline = part.get("inlineData") or part.get("inline_data")
        encoded = inline.get("data") if isinstance(inline, dict) else ""
        if encoded:
            try:
                image = base64.b64decode(encoded, validate=True)
            except ValueError:
                continue
            if image:
                return image
    return None


__all__ = [
    "VlmGateway",
    "VlmServiceError",
    "check_vlm_connectivity",
    "check_vlm_images_connectivity",
    "gemini_generate_content_url",
    "images_generation_url",
    "resolve_vlm_request_url",
    "validate_vlm_endpoint",
    "validate_vlm_protocol",
]
