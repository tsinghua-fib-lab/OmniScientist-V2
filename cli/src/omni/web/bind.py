"""Loopback-only bind policy for ``omni web``.

The first web surface is unauthenticated. Binding ``0.0.0.0`` (or any
non-loopback address) would expose the local agent on the LAN. Reject that
before uvicorn starts.
"""

from __future__ import annotations

import ipaddress

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1088

_EXPLICIT_REFUSALS = frozenset({"0.0.0.0", "::", "[::]", "*", "0"})
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def validate_bind_host(host: str) -> str:
    """Return ``host`` if it is loopback; raise ``ValueError`` otherwise."""
    raw = (host or "").strip()
    if not raw:
        raise ValueError("omni web host must not be empty")
    lowered = raw.lower()
    if lowered in _EXPLICIT_REFUSALS:
        raise ValueError("omni web refuses to bind 0.0.0.0 (loopback only)")
    if lowered in _LOOPBACK_NAMES:
        return raw
    try:
        addr = ipaddress.ip_address(raw.strip("[]"))
    except ValueError as exc:
        raise ValueError(f"omni web invalid host: {host}") from exc
    if not addr.is_loopback:
        raise ValueError("omni web binds loopback only")
    return raw


def ready_url(host: str, port: int) -> str:
    """Canonical URL printed after the ASGI server is listening."""
    display = "127.0.0.1" if host in {"::1", "[::1]"} else host
    if ":" in display and not display.startswith("["):
        display = f"[{display}]"
    return f"http://{display}:{int(port)}"
