"""Optional Omni image generation with deterministic seed fallback."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from . import reference_seeds

_DEFAULT_TIMEOUT_S = 180.0
_TRANSIENT_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

GENERATION_DISABLED_WARNING = (
    "Reference image generation is not explicitly configured; using a built-in seed."
)
GENERATION_INCOMPLETE_WARNING = (
    "Omni image generation configuration is incomplete; using a built-in seed."
)
GENERATION_FAILED_WARNING = (
    "Reference image generation failed after bounded attempts; using a built-in seed."
)
GENERATION_BUDGET_WARNING = (
    "Reference image generation was skipped to preserve the workflow budget; using a "
    "built-in seed."
)


class ReferenceGenerationError(RuntimeError):
    """Reference generation failed without exposing provider credentials."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Keep the bearer credential bound to the configured endpoint origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(frozen=True)
class ImageGenerationConfig:
    """Validated connection details for an Omni-normalized image endpoint."""

    endpoint: str
    model: str
    api_key: str = field(repr=False)
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def __post_init__(self) -> None:
        endpoint = str(self.endpoint).strip()
        model = str(self.model).strip()
        api_key = str(self.api_key).strip()
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ReferenceGenerationError(
                "OMNI_IMAGE_GEN_ENDPOINT must be a complete HTTPS URL"
            )
        if not model or not api_key:
            raise ReferenceGenerationError(
                "Omni image generation configuration is incomplete"
            )
        if (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, (int, float))
            or not 0 < float(self.timeout_s) <= 600
        ):
            raise ReferenceGenerationError("image generation timeout is invalid")
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "timeout_s", float(self.timeout_s))


@dataclass(frozen=True)
class HttpResponse:
    """Small transport-neutral HTTP response used by the image adapter."""

    status: int
    body: bytes


class HttpTransport(Protocol):
    """Synchronous transport boundary for focused offline tests."""

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout_s: float,
    ) -> HttpResponse: ...


class ReferenceImageGenerator(Protocol):
    """Provider-neutral image generation boundary used by resolution."""

    def generate(
        self,
        *,
        config: ImageGenerationConfig,
        prompt: str,
        orientation: str,
    ) -> bytes: ...


class ImageGenerationClient:
    """Generate one PNG through an Omni-normalized image endpoint."""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 2,
    ) -> None:
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 3
        ):
            raise ValueError("max_attempts must be an integer between 1 and 3")
        self._transport = transport or _urlopen_transport
        self._sleep = sleep
        self._max_attempts = max_attempts

    def generate(
        self,
        *,
        config: ImageGenerationConfig,
        prompt: str,
        orientation: str,
    ) -> bytes:
        """Return decoded PNG bytes after bounded transient retries."""

        normalized_prompt = str(prompt).strip()
        if not normalized_prompt:
            raise ReferenceGenerationError("reference generation prompt is empty")
        if orientation not in {"landscape", "portrait"}:
            raise ReferenceGenerationError("reference orientation is invalid")
        payload = json.dumps(
            {
                "model": config.model,
                "n": 1,
                "output_format": "png",
                "prompt": normalized_prompt,
                "size": "1536x1024" if orientation == "landscape" else "1024x1536",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        for attempt in range(self._max_attempts):
            try:
                response = self._transport(
                    config.endpoint,
                    headers,
                    payload,
                    config.timeout_s,
                )
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                if attempt + 1 < self._max_attempts:
                    self._sleep(0.25 * (attempt + 1))
                    continue
                raise ReferenceGenerationError(
                    "image generation transport failed after bounded attempts"
                ) from exc
            if 200 <= response.status < 300:
                return _decode_b64_image(response.body)
            if (
                response.status in _TRANSIENT_STATUS_CODES
                and attempt + 1 < self._max_attempts
            ):
                self._sleep(0.25 * (attempt + 1))
                continue
            raise ReferenceGenerationError(
                f"image generation request failed with HTTP {response.status}"
            )
        raise AssertionError("image generation retry loop exited without a result")


def image_config_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> tuple[ImageGenerationConfig | None, str | None]:
    """Load complete opt-in configuration or return a safe seed warning."""

    source = os.environ if environ is None else environ
    endpoint = str(source.get("OMNI_IMAGE_GEN_ENDPOINT", "")).strip()
    model = str(source.get("OMNI_IMAGE_GEN_MODEL", "")).strip()
    api_key = str(source.get("OMNI_IMAGE_GEN_API_KEY", "")).strip()
    if not endpoint and not model and not api_key:
        return None, GENERATION_DISABLED_WARNING
    if not endpoint or not model or not api_key:
        return None, GENERATION_INCOMPLETE_WARNING
    try:
        config = ImageGenerationConfig(
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            timeout_s=timeout_s,
        )
    except ReferenceGenerationError:
        return None, GENERATION_INCOMPLETE_WARNING
    return config, None


def resolve_reference(
    seed: reference_seeds.SeedSpec,
    *,
    prompt: str,
    output_dir: str | Path,
    density: str,
    environ: Mapping[str, str] | None = None,
    generator: ReferenceImageGenerator | None = None,
    generation_budget_s: float | None = None,
) -> reference_seeds.ReferenceBundle:
    """Generate a hash-bound reference or deterministically return the selected seed."""

    registered_seed = reference_seeds.seed_by_id(seed.seed_id)
    if seed != registered_seed:
        raise reference_seeds.ReferenceSeedError(
            "reference seed must come from registry"
        )
    normalized_density = reference_seeds.normalize_density(density)
    if generation_budget_s is not None and generation_budget_s < 10.0:
        return reference_seeds.load_seed_bundle(
            seed,
            density=normalized_density,
            warning=GENERATION_BUDGET_WARNING,
        )
    attempt_timeout = (
        _DEFAULT_TIMEOUT_S
        if generation_budget_s is None
        else max(5.0, min(_DEFAULT_TIMEOUT_S, generation_budget_s / 2.0))
    )
    config, configuration_warning = image_config_from_env(
        environ,
        timeout_s=attempt_timeout,
    )
    if config is None:
        return reference_seeds.load_seed_bundle(
            seed,
            density=normalized_density,
            warning=configuration_warning,
        )
    client = generator or ImageGenerationClient()
    try:
        image_bytes = _generate_image(
            client,
            config=config,
            prompt=prompt,
            orientation=seed.orientation,
        )
    except ReferenceGenerationError:
        return reference_seeds.load_seed_bundle(
            seed,
            density=normalized_density,
            warning=GENERATION_FAILED_WARNING,
        )

    destination = Path(output_dir).expanduser() / (
        f"generated-reference-{seed.seed_id}.png"
    )
    persisted = _persist_generated_reference(destination, image_bytes)
    return reference_seeds.ReferenceBundle(
        image_path=str(persisted),
        image_sha256=_sha256_bytes(image_bytes),
        source_kind="generated",
        seed_id=seed.seed_id,
        orientation=seed.orientation,
        density=normalized_density,
        design_brief=seed.design_brief,
    )


def _urlopen_transport(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout_s: float,
) -> HttpResponse:
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=timeout_s) as response:
            return HttpResponse(status=int(response.status), body=response.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(status=int(exc.code), body=exc.read())


def _decode_b64_image(raw: bytes) -> bytes:
    try:
        payload: Any = json.loads(raw)
        data = payload.get("data") if isinstance(payload, dict) else None
        first = data[0] if isinstance(data, list) and data else None
        encoded = first.get("b64_json") if isinstance(first, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("missing b64_json")
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReferenceGenerationError(
            "image generation endpoint returned an invalid b64_json response"
        ) from exc
    if not image.startswith(_PNG_SIGNATURE):
        raise ReferenceGenerationError(
            "image generation endpoint returned a non-PNG image"
        )
    return image


def _generate_image(
    generator: ReferenceImageGenerator,
    *,
    config: ImageGenerationConfig,
    prompt: str,
    orientation: str,
) -> bytes:
    try:
        image = generator.generate(
            config=config,
            prompt=prompt,
            orientation=orientation,
        )
    except ReferenceGenerationError:
        raise
    except Exception:
        raise ReferenceGenerationError("reference image provider failed") from None
    if not isinstance(image, bytes) or not image.startswith(_PNG_SIGNATURE):
        raise ReferenceGenerationError("generated reference is not a PNG image")
    return image


def _persist_generated_reference(destination: Path, content: bytes) -> Path:
    parent = destination.parent
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        raise OSError(f"reference output directory is unsafe: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise OSError(f"reference output directory is unsafe: {parent}")
    if destination.is_symlink():
        raise OSError(
            f"generated reference destination may not be a symlink: {destination}"
        )
    if destination.exists() and not destination.is_file():
        raise OSError(f"generated reference destination must be a file: {destination}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if destination.is_symlink():
            raise OSError(
                f"generated reference destination may not be a symlink: {destination}"
            )
        os.replace(temporary, destination)
        if destination.is_symlink() or not destination.is_file():
            raise OSError(
                f"generated reference destination is not a regular file: {destination}"
            )
        return destination.resolve(strict=True)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "GENERATION_DISABLED_WARNING",
    "GENERATION_FAILED_WARNING",
    "GENERATION_INCOMPLETE_WARNING",
    "HttpResponse",
    "ImageGenerationClient",
    "ImageGenerationConfig",
    "ReferenceGenerationError",
    "image_config_from_env",
    "resolve_reference",
]
