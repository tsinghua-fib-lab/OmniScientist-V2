"""Owner model/VLM facts that long-running processes re-resolve.

``omni serve`` used to construct :class:`~omni.core.vlm.VlmGateway` once at
process boot. A later Settings / ``omni config vlm`` write reached disk, but
WeChat kept the frozen gateway (often ``vlm.enabled=false``), so the next
inbound turn admitted as ``vlm_not_configured``.

Codex reloads after an explicit write. DeepSeek resolves connection facts
from one settings snapshot per operation and keeps last-good on a bad file.
This module is that seam: persist still goes through ``user_edits``; callers
bump a home-level generation and the next turn loads ``config.toml`` plus
``secrets.toml`` together so an endpoint is never paired with a newer key.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from omni.config.paths import OmniPaths
from omni.config.settings import OmniSettings, load_settings

logger = logging.getLogger(__name__)

_RELOAD_SENTINEL = "settings.reload"

OWNER_CONNECTION_RELOAD_NOTICE = (
    "The next turn in this web process, an open REPL, and a running "
    "`omni serve` (WeChat included) will use the new connection. "
    "A turn that already started keeps the connection it began with."
)


def is_owner_connection_key(key: str) -> bool:
    """True for dotted keys that change the owner model or VLM client."""
    head = str(key or "").split(".", 1)[0].strip()
    return head in {"model", "vlm"}


def settings_reload_path(home: Path) -> Path:
    """Home-level sentinel ``omni serve`` polls (``~/.omni/service/settings.reload``)."""
    return Path(home) / "service" / _RELOAD_SENTINEL


def request_settings_reload(home: Path) -> None:
    """Ask a running home service to re-read owner model/VLM facts.

    Best-effort and cross-platform (no signals). Failures are swallowed so a
    config write never fails because the sentinel directory is missing.
    """
    try:
        path = settings_reload_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{time.time()}\n", encoding="utf-8")
    except OSError:
        pass


def _path_stamp(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (0, 0)


def owner_source_stamp(paths: OmniPaths) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    """Cheap identity of the owner files a long-running process last observed."""
    return (
        _path_stamp(paths.config_file),
        _path_stamp(paths.secrets_file),
        _path_stamp(settings_reload_path(paths.home)),
    )


def connection_token(settings: OmniSettings) -> tuple[str, ...]:
    """Comparable owner-connection identity, including secret material.

    Never log the return value: it contains API keys so a URL rewrite and a
    key rewrite in the same save stay one generation.
    """
    model = settings.model
    vlm = settings.vlm
    return (
        str(model.provider or ""),
        str(model.base_url or ""),
        str(model.api_key or ""),
        str(model.model or ""),
        str(model.max_tokens),
        str(model.temperature),
        str(model.request_timeout_s),
        str(model.fallback_provider or ""),
        str(model.fallback_base_url or ""),
        str(model.fallback_api_key or ""),
        str(model.fallback_model or ""),
        str(bool(vlm.enabled)),
        str(vlm.model or ""),
        str(getattr(vlm, "image_model", "") or ""),
        str(vlm.endpoint or ""),
        str(vlm.api_key or ""),
        str(vlm.protocol or ""),
        str(vlm.timeout_s),
    )


def public_connection_identity(settings: OmniSettings) -> dict[str, Any]:
    """Non-secret model/VLM identity for ``serve status`` / doctor."""
    return {
        "model_provider": settings.model.provider,
        "model_name": settings.model.model,
        "model_base_url": settings.model.base_url,
        "vlm_enabled": bool(settings.vlm.enabled),
        "vlm_model": settings.vlm.model,
        "vlm_image_model": settings.vlm.image_model,
        "vlm_endpoint": settings.vlm.endpoint,
    }


def format_vlm_identity(enabled: Any, model: Any, endpoint: Any) -> str:
    """One-line VLM identity for status tables (never includes a key)."""
    if not enabled:
        return "disabled"
    name = str(model or "").strip() or "(unset)"
    url = str(endpoint or "").strip() or "(unset)"
    return f"{name} @ {url}"


def format_model_identity(provider: Any, model: Any) -> str:
    """One-line main-model identity for status tables."""
    return f"{provider or '-'} / {model or '-'}"


def try_load_owner_settings(paths: OmniPaths) -> OmniSettings | None:
    """Load current owner settings, or ``None`` when the files cannot be parsed.

    A corrupt ``config.toml`` must not collapse to defaults (that would disable
    a working VLM). Callers keep the last-good connection.
    """
    try:
        if paths.workspace_root is not None:
            return load_settings(cwd=paths.workspace_root)
        if paths.project_name:
            return load_settings(project=paths.project_name)
        return load_settings()
    except Exception:  # noqa: BLE001 — last-good is the contract
        logger.warning(
            "owner settings reload failed; keeping the last-good model/VLM connection",
            exc_info=True,
        )
        return None


def adopt_owner_connection(current: OmniSettings, fresh: OmniSettings) -> None:
    """Copy only model/VLM facts onto ``current`` (do not rebuild the agent)."""
    current.model = fresh.model
    current.vlm = fresh.vlm
