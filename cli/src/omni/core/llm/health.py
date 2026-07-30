"""Small, non-secret health record for the active model configuration."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from omni.memory.sanitize import redact_secrets

ModelHealthStatus = Literal["unverified", "verified", "failed"]
_UNVERIFIED_MESSAGE = "Model configuration changed and has not been tested."


@dataclass(frozen=True, slots=True)
class ModelHealth:
    status: ModelHealthStatus
    message: str
    checked_at: str = ""


def load_model_health(paths: Any, model: Any) -> ModelHealth:
    """Return health only when it belongs to the exact active configuration."""
    path = _health_path(paths)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ModelHealth("unverified", _UNVERIFIED_MESSAGE)
    if payload.get("fingerprint") != _model_fingerprint(model):
        return ModelHealth("unverified", _UNVERIFIED_MESSAGE)
    status = str(payload.get("status") or "")
    if status not in {"unverified", "verified", "failed"}:
        return ModelHealth("unverified", _UNVERIFIED_MESSAGE)
    return ModelHealth(
        status=status,  # type: ignore[arg-type]
        message=redact_secrets(str(payload.get("message") or "")),
        checked_at=str(payload.get("checked_at") or ""),
    )


def record_model_health(
    paths: Any,
    model: Any,
    *,
    status: ModelHealthStatus,
    message: str,
) -> ModelHealth:
    """Atomically persist status without storing credentials or raw provider bodies."""
    record = ModelHealth(
        status=status,
        message=redact_secrets(message),
        checked_at=datetime.now(UTC).isoformat(),
    )
    path = _health_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **asdict(record),
        "fingerprint": _model_fingerprint(model),
    }
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return record


def _health_path(paths: Any) -> Path:
    return Path(paths.cache_dir) / "model-health.json"


def _model_fingerprint(model: Any) -> str:
    payload = {
        "provider": str(getattr(model, "provider", "") or ""),
        "base_url": str(getattr(model, "base_url", "") or "").rstrip("/"),
        "model": str(getattr(model, "model", "") or ""),
        "api_key": str(getattr(model, "api_key", "") or ""),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["ModelHealth", "ModelHealthStatus", "load_model_health", "record_model_health"]
