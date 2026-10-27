"""Provider-neutral multimodal client for Omni-managed VLM endpoints.

The endpoint contract is deliberately small: Omni supplies an OpenAI-compatible
base URL or ``/chat/completions`` URL, model name, and bearer credential. Skills do
not infer providers from URLs and do not carry provider-specific auth or payload
branches.
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from posterlib.runtime import model_response

_TRANSIENT_HTTP_STATUSES = frozenset({408, 409, 425, 429})
_ENDPOINT_ENV = "OMNI_VLM_ENDPOINT"
_MODEL_ENV = "OMNI_VLM_MODEL"
_API_KEY_ENV = "OMNI_VLM_API_KEY"
_HOST_MODEL = "host-configured"
_LABELED_IMAGE_MAX_WIDTH = 4_096
_SINGLE_IMAGE_MAX_BYTES = 2 * 1024 * 1024
_SINGLE_IMAGE_MIN_DIMENSION = 640
_MONTAGE_PANEL_WIDTH = 1_920
_MONTAGE_MAX_IMAGE_HEIGHT = 2_160
_MONTAGE_GAP = 32
_MONTAGE_LABEL_HEIGHT = 88
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TRANSIENT_ATTEMPTS = 2
_RETRY_DELAY_S = 0.25


class VlmError(RuntimeError):
    """A safe configuration, transport, or response error."""


class RetryableVlmError(VlmError):
    """A transient endpoint failure that may succeed on a bounded retry."""


async def generate_json_text_bounded(
    client: Any,
    prompt: str,
    *,
    images: Sequence[VlmImage],
    deadline: float | None,
    timeout_message: str,
) -> str:
    """Generate within one shared deadline and retry one transient failure."""

    for attempt in range(_TRANSIENT_ATTEMPTS):
        remaining = (
            None
            if deadline is None
            else deadline - asyncio.get_running_loop().time()
        )
        if remaining is not None and remaining <= 0:
            raise TimeoutError(timeout_message)
        try:
            if remaining is None:
                return await client.generate_json_text(prompt, images=images)
            async with asyncio.timeout(remaining):
                return await client.generate_json_text(prompt, images=images)
        except RetryableVlmError:
            if attempt == _TRANSIENT_ATTEMPTS - 1:
                raise
            await asyncio.sleep(_RETRY_DELAY_S)
    raise AssertionError("unreachable VLM transient retry state")


def parse_json_object(text: str) -> dict[str, Any]:
    """Decode one complete VLM object without synthesizing missing review data."""

    try:
        return model_response.parse_json_object(text)
    except ValueError as exc:
        raise VlmError(f"VLM response is not valid JSON: {exc}") from exc


def _chat_completions_url(endpoint: str) -> str:
    """Normalize a site origin, API base, or complete chat endpoint."""

    parsed = urlparse(endpoint)
    path = (parsed.path or "").rstrip("/")
    if path.lower().endswith("/chat/completions"):
        request_path = path
    elif path:
        request_path = f"{path}/chat/completions"
    else:
        request_path = "/v1/chat/completions"
    return parsed._replace(path=request_path, fragment="").geturl()


@dataclass(frozen=True)
class VlmConfig:
    """Connection details for one Omni-normalized VLM endpoint."""

    endpoint: str
    model: str
    api_key: str = field(repr=False)
    timeout_s: float | None = None

    def __post_init__(self) -> None:
        endpoint = str(self.endpoint).strip()
        model = str(self.model).strip()
        api_key = str(self.api_key).strip()
        parsed = urlparse(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
        ):
            raise VlmError("OMNI_VLM_ENDPOINT must be a complete HTTP(S) URL")
        if parsed.username or parsed.password:
            raise VlmError("OMNI_VLM_ENDPOINT must not contain embedded credentials")
        if parsed.fragment:
            raise VlmError("OMNI_VLM_ENDPOINT must not contain a URL fragment")
        if (
            parsed.scheme == "http"
            and parsed.hostname.lower() not in _LOOPBACK_HOSTS
        ):
            raise VlmError(
                "OMNI_VLM_ENDPOINT must use HTTPS; plain HTTP is allowed only for "
                "loopback"
            )
        if not model:
            raise VlmError("OMNI_VLM_MODEL is required")
        if not api_key or any(ord(char) < 32 or ord(char) == 127 for char in api_key):
            raise VlmError("OMNI_VLM_API_KEY (VLM API key) is required")
        if self.timeout_s is not None and (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, (int, float))
            or not math.isfinite(float(self.timeout_s))
            or float(self.timeout_s) <= 0
        ):
            raise VlmError("VLM timeout must be a positive finite number")
        object.__setattr__(self, "endpoint", _chat_completions_url(endpoint))
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "api_key", api_key)
        if self.timeout_s is not None:
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
    timeout_s: float | None = None,
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
            montage = _image_data_url(labeled_montage(normalized_images))
            content.append(
                {
                    "type": "text",
                    "text": (
                        "The single attached image is a deterministic labeled montage. "
                        "Read each pane by its embedded label and preserve the original "
                        "left-to-right image order."
                    ),
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": montage},
                }
            )
        else:
            for image in normalized_images:
                content.extend(
                    [
                        {"type": "text", "text": image.label},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": bounded_image_data_url(image)
                            },
                        },
                    ]
                )
        payload = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            endpoint_host = (urlparse(self._config.endpoint).hostname or "").lower()
            async with httpx.AsyncClient(
                timeout=self._config.timeout_s,
                transport=self._transport,
                # A loopback VLM is the local service itself. System proxy
                # discovery can otherwise route 127.0.0.1 through a desktop proxy
                # and leave a healthy local reviewer waiting on the wrong socket.
                trust_env=endpoint_host not in _LOOPBACK_HOSTS,
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
            reference_image_uri = bounded_image_data_url(
                normalized_images[0]
            )
        elif len(normalized_images) >= 2:
            reference_image_uri = _image_data_url(labeled_montage(normalized_images))
            normalized_prompt = (
                "The single attached image is a deterministic labeled montage. "
                "Read each pane by its embedded label and preserve the original "
                f"left-to-right image order.\n{normalized_prompt}"
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
    except Exception:  # noqa: BLE001 - an injected host property may be arbitrary
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
    except Exception:  # noqa: BLE001 - an injected host property may be arbitrary
        return False


def _image_data_url(image: VlmImage) -> str:
    mime_type = str(image.mime_type).strip().lower()
    if not mime_type.startswith("image/") or any(
        character in mime_type for character in (";", ",", "\r", "\n")
    ):
        raise VlmError(f"{image.label} has an invalid image type")
    encoded = base64.b64encode(image.image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def bounded_image_data_url(
    source: VlmImage,
    *,
    max_edge_px: int = _LABELED_IMAGE_MAX_WIDTH,
) -> str:
    """Bound raster dimensions and transport bytes without changing source identity."""

    if source.mime_type == "image/svg+xml":
        from PIL import Image

        raster = _flatten_image(_rasterize_svg(source.image_bytes), Image)
        return _bounded_raster_data_url(
            raster,
            label=source.label,
            image_module=Image,
        )
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return _image_data_url(source)
    try:
        with Image.open(io.BytesIO(source.image_bytes)) as opened:
            raster = ImageOps.exif_transpose(opened)
            raster.load()
            if (
                raster.width <= max_edge_px
                and raster.height <= max_edge_px
                and len(source.image_bytes) <= _SINGLE_IMAGE_MAX_BYTES
            ):
                return _image_data_url(source)
            raster = _flatten_image(raster, Image)
    except (OSError, ValueError):
        return _image_data_url(source)
    raster = _fit_montage_image(
        raster,
        panel_width=max_edge_px,
        max_height=max_edge_px,
        image_module=Image,
    )
    return _bounded_raster_data_url(
        raster,
        label=source.label,
        image_module=Image,
    )


def _bounded_raster_data_url(
    raster: Any,
    *,
    label: str,
    image_module: Any,
) -> str:
    """Encode one visual reference under the endpoint transport budget."""

    lossless = io.BytesIO()
    raster.save(lossless, format="PNG", compress_level=9, optimize=True)
    if len(lossless.getvalue()) <= _SINGLE_IMAGE_MAX_BYTES:
        return _image_data_url(
            VlmImage(
                label=label,
                image_bytes=lossless.getvalue(),
                mime_type="image/png",
            )
        )

    encoded = b""
    while True:
        output = io.BytesIO()
        raster.save(output, format="JPEG", quality=90, optimize=True)
        encoded = output.getvalue()
        if (
            len(encoded) <= _SINGLE_IMAGE_MAX_BYTES
            or min(raster.size) <= _SINGLE_IMAGE_MIN_DIMENSION
        ):
            break
        scale = max(
            0.5,
            min(0.9, math.sqrt(_SINGLE_IMAGE_MAX_BYTES / len(encoded)) * 0.95),
        )
        raster = raster.resize(
            (
                max(_SINGLE_IMAGE_MIN_DIMENSION, round(raster.width * scale)),
                max(_SINGLE_IMAGE_MIN_DIMENSION, round(raster.height * scale)),
            ),
            image_module.Resampling.LANCZOS,
        )
    return _image_data_url(
        VlmImage(label=label, image_bytes=encoded, mime_type="image/jpeg")
    )


def labeled_montage(
    images: Sequence[VlmImage],
    *,
    label: str = "labeled VLM evidence montage",
    columns: int | None = None,
) -> VlmImage:
    """Return the maintained labeled montage as one reusable image input."""

    images = tuple(images)
    if not images:
        raise VlmError("A labeled montage requires at least one image")
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError as exc:
        raise VlmError(
            "Pillow is required to assemble host VLM evidence images"
        ) from exc

    decoded = []
    try:
        for image in images:
            if image.mime_type == "image/svg+xml":
                opened = _rasterize_svg(image.image_bytes)
            else:
                opened = Image.open(io.BytesIO(image.image_bytes))
            with opened:
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
    column_count = min(columns, len(resized)) if columns is not None else len(resized)
    row_heights = [
        max(image.height for image in resized[start:start + column_count])
        for start in range(0, len(resized), column_count)
    ]
    sheet_width = column_count * panel_width + (column_count + 1) * _MONTAGE_GAP
    sheet_height = (
        sum(row_heights)
        + len(row_heights) * (_MONTAGE_LABEL_HEIGHT + 2 * _MONTAGE_GAP)
        + _MONTAGE_GAP
    )
    sheet = Image.new("RGB", (sheet_width, sheet_height), "#e5e7eb")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 44)
    except OSError:
        font = ImageFont.load_default()
    top = _MONTAGE_GAP
    for index, (source, raster) in enumerate(zip(images, resized, strict=True)):
        row, column = divmod(index, column_count)
        pane_label = " ".join(source.label.split())[:72].upper()
        left = _MONTAGE_GAP + column * (panel_width + _MONTAGE_GAP)
        draw.rectangle(
            (
                left,
                top,
                left + panel_width,
                top + _MONTAGE_LABEL_HEIGHT,
            ),
            fill="#111827",
        )
        draw.text(
            (left + 24, top + 18),
            pane_label,
            fill="#ffffff",
            font=font,
        )
        image_top = top + _MONTAGE_GAP + _MONTAGE_LABEL_HEIGHT
        image_left = left + (panel_width - raster.width) // 2
        sheet.paste(raster, (image_left, image_top))
        draw.rectangle(
            (
                left,
                image_top,
                left + panel_width,
                image_top + row_heights[row],
            ),
            outline="#6b7280",
            width=2,
        )
        if column == column_count - 1:
            top = image_top + row_heights[row] + _MONTAGE_GAP

    output = io.BytesIO()
    sheet.save(output, format="PNG", compress_level=6)
    return VlmImage(
        label=label,
        image_bytes=output.getvalue(),
        mime_type="image/png",
    )


def _rasterize_svg(content: bytes) -> Any:
    """Rasterize one prepared SVG within the VLM image bounds."""

    try:
        import pymupdf
        from PIL import Image
    except ImportError as exc:
        raise VlmError("PyMuPDF and Pillow are required for SVG VLM inputs") from exc
    document = None
    try:
        document = pymupdf.open(stream=content, filetype="svg")
        page = document[0]
        width = float(page.rect.width)
        height = float(page.rect.height)
        if width <= 0 or height <= 0:
            raise ValueError("SVG dimensions must be positive")
        scale = min(
            _MONTAGE_PANEL_WIDTH / width,
            _MONTAGE_MAX_IMAGE_HEIGHT / height,
        )
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
        raster = Image.open(io.BytesIO(pixmap.tobytes("png")))
        raster.load()
        return raster
    except (IndexError, OSError, RuntimeError, ValueError) as exc:
        raise VlmError("VLM input contains an unreadable SVG image") from exc
    finally:
        if document is not None:
            document.close()


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
    "bounded_image_data_url",
    "client_from_context",
    "config_from_env",
    "configuration_present",
    "generate_json_text_bounded",
    "labeled_montage",
]
