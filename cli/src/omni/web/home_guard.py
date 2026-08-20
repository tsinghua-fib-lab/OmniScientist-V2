"""Keep Web configuration writes inside the Home this process started with."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omni.config.paths import get_paths
from omni.web.protocol import RpcError

RESTART_REQUIRED_CODE = "restart_required"
RESTART_REQUIRED_MESSAGE = (
    "The Omni data directory changed. Restart this omni web process "
    "before changing configuration or connecting channels."
)

# Reads (and login cancel) stay available so the UI can explain the freeze.
HOME_DRIFT_ALLOWED = frozenset(
    {
        "channel.describe",
        "channel.wechat.cancel",
        "channel.wechat.status",
        "config.describe",
        "config.get",
        "skill.list",
        "skill.info",
    }
)


def resolved_home(settings: Any | None = None) -> str:
    """Return the normalized owner Home, without reading user config.toml."""
    paths = getattr(settings, "paths", None) if settings is not None else None
    home = getattr(paths, "home", None) if paths is not None else None
    if home is None:
        home = get_paths().home
    return str(Path(home).expanduser().resolve())


def bind_process_home(app: Any, settings: Any | None = None) -> str:
    """Remember the first Home this Web process served, then keep returning it."""
    current = resolved_home(settings)
    stored = getattr(app.state, "web_home", None)
    if not stored and current:
        app.state.web_home = current
        return current
    return str(stored or current)


def home_has_drifted(app: Any, settings: Any | None = None) -> bool:
    """True when the owner Home now differs from the Home this process bound."""
    current = resolved_home(settings)
    stored = bind_process_home(app, settings)
    return bool(stored and current and stored != current)


def refuse_if_home_drifted(app: Any, method: str, settings: Any | None = None) -> None:
    """Fail closed on configuration writes after the process Home changed."""
    if method in HOME_DRIFT_ALLOWED:
        return
    if home_has_drifted(app, settings):
        raise RpcError(RESTART_REQUIRED_CODE, RESTART_REQUIRED_MESSAGE)
