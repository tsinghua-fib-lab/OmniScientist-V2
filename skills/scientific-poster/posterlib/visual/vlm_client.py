"""Provider-neutral multimodal client for Omni-managed VLM endpoints.

The endpoint contract is deliberately small: Omni supplies a complete
OpenAI-compatible ``/chat/completions`` URL, model name, and bearer credential.
Skills do not infer providers from URLs and do not carry provider-specific auth or
payload branches.
"""

from __future__ import annotations

import base64
import io
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

_TRANSIENT_HTTP_STATUSES = frozenset({408, 409, 425, 429})
_ENDPOINT_ENV = "OMNI_VLM_ENDPOINT"
_MODEL_ENV = "OMNI_VLM_MODEL"
_API_KEY_ENV = "OMNI_VLM_API_KEY"
_HOST_MODEL = "host-configured"
_LABELED_IMAGE_MAX_WIDTH = 4_096
_LABELED_IMAGE_MAX_HEIGHT = 4_096
_MONTAGE_PANEL_WIDTH = 1_920
_MONTAGE_MAX_IMAGE_HEIGHT = 2_160
_MONTAGE_GAP = 32
_MONTAGE_LABEL_HEIGHT = 88
_DEFAULT_RESPONSE_EXCERPT_CHARS = 8_000


class VlmError(RuntimeError):
    """A safe configuration, transport, or response error."""


class RetryableVlmError(VlmError):
    """A transient endpoint failure that may succeed on a bounded retry."""


def bounded_response_excerpt(
    value: Any,
    *,
    max_chars: int = _DEFAULT_RESPONSE_EXCERPT_CHARS,
) -> str:
    """Return a bounded provider response suitable for a corrective prompt."""

    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[previous response truncated]"


@dataclass(frozen=True)
class VlmConfig:
    """Connection details for one Omni-normalized VLM endpoint."""

    endpoint: str
    model: str
    api_key: str = field(repr=False)
    timeout_s: float = 60.0

    def __post_init__(self) -> None:
        endpoint = str(self.endpoint).strip()
        model = str(self.model).strip()
        api_key = str(self.api_key).strip()
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise VlmError("OMNI_VLM_ENDPOINT must be a complete HTTPS URL")
        if not model:
            raise VlmError("OMNI_VLM_MODEL is required")
        if not api_key or any(ord(char) < 32 or ord(char) == 127 for char in api_key):
            raise VlmError("OMNI_VLM_API_KEY (VLM API key) is required")
        if (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, (int, float))
            or not math.isfinite(float(self.timeout_s))
            or not 0 < float(self.timeout_s) <= 600
        ):
            raise VlmError("VLM timeout must be a positive finite number")
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "timeout_s", float(self.timeout_s))


@dataclass(frozen=True)
class VlmImage:
    """One labeled image sent to the configured VLM in deterministic order."""

    label: str
    image_bytes: bytes
    mime_type: str

    def __post_init__(self) -> None:
        if not str(self.label).strip():
            raise VlmError("VLM image label is required")
        if not self.image_bytes:
            raise VlmError(f"{self.label} is empty")
        if not str(self.mime_type).startswith("image/"):
            raise VlmError(f"{self.label} must have a recognized image type")


def config_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    timeout_s: float = 60.0,
    endpoint_override: str = "",
    model_override: str = "",
) -> VlmConfig | None:
    """Build the shared Omni VLM config, or return ``None`` when incomplete."""

    source = os.environ if environ is None else environ
    endpoint = str(endpoint_override or source.get(_ENDPOINT_ENV, "")).strip()
    model = str(model_override or source.get(_MODEL_ENV, "")).strip()
    api_key = str(source.get(_API_KEY_ENV, "")).strip()
    if not endpoint or not model or not api_key:
        return None
    return VlmConfig(
        endpoint=endpoint,
        model=model,
        api_key=api_key,
        timeout_s=timeout_s,
    )


class VlmClient:
    """Send text and ordered image inputs through the Omni VLM contract."""

    def __init__(
        self,
        config: VlmConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport

    @property
    def model(self) -> str:
        """Configured model identity recorded in review receipts."""

        return self._config.model

    async def generate_json_text(
        self,
        prompt: str,
        *,
        images: Sequence[VlmImage] = (),
    ) -> str:
        """Return the endpoint's textual JSON response."""

        normalized_prompt = str(prompt).strip()
        if not normalized_prompt:
            raise VlmError("VLM prompt is required")
        content: list[dict[str, Any]] = [{"type": "text", "text": normalized_prompt}]
        normalized_images = tuple(images)
        for image in normalized_images:
            if not isinstance(image, VlmImage):
                raise VlmError("VLM images must be VlmImage objects")
        if len(normalized_images) >= 2:
            content.append(
                {
                    "type": "text",
                    "text": (
                        "Each following image carries its role in a deterministic "
                        "pixel label. The embedded REFERENCE, CANDIDATE, and EVIDENCE "
                        "labels are authoritative; never swap their roles."
                    ),
                }
            )
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": _pixel_labeled_image_data_url(image)},
                }
                for image in normalized_images
            )
        else:
            for image in normalized_images:
                content.extend(
                    [
                        {"type": "text", "text": image.label},
                        {
                            "type": "image_url",
                            "image_url": {"url": _image_data_url(image)},
                        },
                    ]
                )
        payload = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
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
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            error_type = (
                RetryableVlmError
                if status in _TRANSIENT_HTTP_STATUSES or status >= 500
                else VlmError
            )
            raise error_type(f"VLM request failed (HTTP {status})") from exc
        except httpx.TransportError as exc:
            raise RetryableVlmError("VLM network request failed") from exc
        except httpx.HTTPError as exc:
            raise VlmError("VLM request could not be created") from exc
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RetryableVlmError("VLM returned an invalid JSON response") from exc
        text = _response_text(payload)
        if not text:
            raise RetryableVlmError("VLM response did not contain review text")
        return text


class HostVlmClient:
    """Adapt an injected single-image host VLM to the poster VLM interface."""

    def __init__(self, service: Any) -> None:
        if not _host_service_available(service):
            raise VlmError("Host VLM service is unavailable")
        self._service = service

    @property
    def model(self) -> str:
        """Return a credential-free identity for visual-review receipts."""

        return _HOST_MODEL

    async def generate_json_text(
        self,
        prompt: str,
        *,
        images: Sequence[VlmImage] = (),
    ) -> str:
        """Call the host port with zero, one, or one labeled evidence montage."""

        normalized_prompt = str(prompt).strip()
        if not normalized_prompt:
            raise VlmError("VLM prompt is required")
        normalized_images = tuple(images)
        if any(not isinstance(image, VlmImage) for image in normalized_images):
            raise VlmError("VLM images must be VlmImage objects")
        if len(normalized_images) > 3:
            raise VlmError("Host VLM review supports at most three ordered images")

        reference_image_uri: str | None = None
        if len(normalized_images) == 1:
            reference_image_uri = _image_data_url(normalized_images[0])
        elif len(normalized_images) >= 2:
            reference_image_uri = _labeled_montage_data_url(normalized_images)
            normalized_prompt = "\n".join(
                [
                    (
                        "The single attached image is a deterministic labeled montage. "
                        "Read each pane by its embedded label and preserve the original "
                        "left-to-right image order."
                    ),
                    normalized_prompt,
                ]
            )

        try:
            text = await self._service.generate_text(
                normalized_prompt,
                reference_image_uri=reference_image_uri,
            )
        except Exception as exc:
            message = str(getattr(exc, "safe_message", "") or "").strip()
            if not message:
                message = "Host VLM request failed"
            error_type = (
                RetryableVlmError
                if bool(getattr(exc, "retryable", False))
                else VlmError
            )
            raise error_type(message) from exc
        if not isinstance(text, str) or not text.strip():
            raise RetryableVlmError("Host VLM response did not contain review text")
        return text.strip()


def client_from_context(ctx: Any) -> HostVlmClient | None:
    """Return the usable host-injected VLM client without importing host internals."""

    service = getattr(ctx, "vlm", None) if ctx is not None else None
    if not _host_service_available(service):
        return None
    return HostVlmClient(service)


def configuration_present(
    ctx: Any = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether a complete host or environment VLM was intentionally set."""

    service = getattr(ctx, "vlm", None) if ctx is not None else None
    if _host_service_available(service):
        return True
    try:
        host_error_code = str(getattr(service, "error_code", "") or "").strip()
    except Exception:
        return service is not None
    if host_error_code and host_error_code != "vlm_not_configured":
        return True
    source = os.environ if environ is None else environ
    return all(
        str(source.get(name, "") or "").strip()
        for name in (_ENDPOINT_ENV, _MODEL_ENV, _API_KEY_ENV)
    )


def _host_service_available(service: Any) -> bool:
    if service is None or not callable(getattr(service, "generate_text", None)):
        return False
    try:
        return bool(getattr(service, "available", False))
    except Exception:
        return False


def _image_data_url(image: VlmImage) -> str:
    mime_type = str(image.mime_type).strip().lower()
    if not mime_type.startswith("image/") or any(
        character in mime_type for character in {";", ",", "\r", "\n"}
    ):
        raise VlmError(f"{image.label} has an invalid image type")
    encoded = base64.b64encode(image.image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _labeled_montage_data_url(images: Sequence[VlmImage]) -> str:
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError as exc:
        raise VlmError(
            "Pillow is required to assemble host VLM evidence images"
        ) from exc

    decoded = []
    try:
        for image in images:
            with Image.open(io.BytesIO(image.image_bytes)) as opened:
                raster = ImageOps.exif_transpose(opened)
                raster.load()
                decoded.append(_flatten_image(raster, Image))
    except (OSError, ValueError) as exc:
        raise VlmError("VLM comparison input is not a readable raster image") from exc

    panel_width = max(
        640,
        min(_MONTAGE_PANEL_WIDTH, max(image.width for image in decoded)),
    )
    resized = [
        _fit_montage_image(
            image,
            panel_width=panel_width,
            max_height=_MONTAGE_MAX_IMAGE_HEIGHT,
            image_module=Image,
        )
        for image in decoded
    ]
    image_height = max(image.height for image in resized)
    sheet_width = len(resized) * panel_width + (len(resized) + 1) * _MONTAGE_GAP
    sheet_height = image_height + _MONTAGE_LABEL_HEIGHT + 3 * _MONTAGE_GAP
    sheet = Image.new("RGB", (sheet_width, sheet_height), "#e5e7eb")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 44)
    except OSError:
        font = ImageFont.load_default()
    for index, (source, raster) in enumerate(zip(images, resized, strict=True)):
        label = " ".join(source.label.split())[:72].upper()
        left = _MONTAGE_GAP + index * (panel_width + _MONTAGE_GAP)
        draw.rectangle(
            (
                left,
                _MONTAGE_GAP,
                left + panel_width,
                _MONTAGE_GAP + _MONTAGE_LABEL_HEIGHT,
            ),
            fill="#111827",
        )
        draw.text(
            (left + 24, _MONTAGE_GAP + 18),
            label,
            fill="#ffffff",
            font=font,
        )
        image_top = 2 * _MONTAGE_GAP + _MONTAGE_LABEL_HEIGHT
        image_left = left + (panel_width - raster.width) // 2
        sheet.paste(raster, (image_left, image_top))
        draw.rectangle(
            (
                left,
                image_top,
                left + panel_width,
                image_top + image_height,
            ),
            outline="#6b7280",
            width=2,
        )

    output = io.BytesIO()
    sheet.save(output, format="PNG", compress_level=6)
    comparison = VlmImage(
        label="labeled VLM evidence montage",
        image_bytes=output.getvalue(),
        mime_type="image/png",
    )
    return _image_data_url(comparison)


def _pixel_labeled_image_data_url(source: VlmImage) -> str:
    """Embed one authoritative role label without montage-scale detail loss."""

    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError as exc:
        raise VlmError("Pillow is required to label VLM evidence images") from exc

    try:
        with Image.open(io.BytesIO(source.image_bytes)) as opened:
            raster = ImageOps.exif_transpose(opened)
            raster.load()
            raster = _flatten_image(raster, Image)
    except (OSError, ValueError) as exc:
        raise VlmError(f"{source.label} is not a readable raster image") from exc
    raster = _fit_montage_image(
        raster,
        panel_width=_LABELED_IMAGE_MAX_WIDTH,
        max_height=_LABELED_IMAGE_MAX_HEIGHT - _MONTAGE_LABEL_HEIGHT,
        image_module=Image,
    )
    sheet = Image.new(
        "RGB",
        (raster.width, raster.height + _MONTAGE_LABEL_HEIGHT),
        "#111827",
    )
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 44)
    except OSError:
        font = ImageFont.load_default()
    draw.text(
        (24, 18),
        " ".join(source.label.split())[:72].upper(),
        fill="#ffffff",
        font=font,
    )
    sheet.paste(raster, (0, _MONTAGE_LABEL_HEIGHT))
    output = io.BytesIO()
    sheet.save(output, format="PNG", compress_level=6)
    return _image_data_url(
        VlmImage(
            label=f"pixel-labeled {source.label}",
            image_bytes=output.getvalue(),
            mime_type="image/png",
        )
    )


def _flatten_image(image: Any, image_module: Any) -> Any:
    rgba = image.convert("RGBA")
    flattened = image_module.new("RGB", rgba.size, "#ffffff")
    flattened.paste(rgba, mask=rgba.getchannel("A"))
    return flattened


def _fit_montage_image(
    image: Any,
    *,
    panel_width: int,
    max_height: int,
    image_module: Any,
) -> Any:
    scale = min(1.0, panel_width / image.width, max_height / image.height)
    if scale >= 1.0:
        return image
    size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    return image.resize(size, image_module.Resampling.LANCZOS)


def _response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") in {None, "text"}
    ).strip()


__all__ = [
    "HostVlmClient",
    "RetryableVlmError",
    "VlmClient",
    "VlmConfig",
    "VlmError",
    "VlmImage",
    "client_from_context",
    "configuration_present",
    "config_from_env",
]
