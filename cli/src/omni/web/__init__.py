"""Loopback web surface: the same cwd-keyed store the CLI uses, in a browser.

``omni web`` is another projection of :meth:`OmniAgent.handle_turn`, not a
second agent. Opening directory ``D`` calls :func:`omni.config.paths.get_paths`
with ``cwd=D`` so sessions, artifacts, and tool roots match ``omni`` launched
in that folder.
"""

from __future__ import annotations

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1088

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "create_app", "validate_bind_host"]


def validate_bind_host(host: str) -> str:
    from omni.web.bind import validate_bind_host as _validate

    return _validate(host)


def create_app(**kwargs):  # noqa: ANN003
    from omni.web.app import create_app as _create_app

    return _create_app(**kwargs)
