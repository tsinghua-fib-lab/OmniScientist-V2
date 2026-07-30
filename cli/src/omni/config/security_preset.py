"""Codex-aligned security presets applied after layered settings resolve.

Sandbox and approval stay separate. This module only picks the default
combination from workspace trust; an explicit ``security.approval_policy``
still wins. Untrusted directories always become read-only — trust is the
write gate, the same way Codex stays read-only until the folder is vouched.

    trusted interactive / trusted load → workspace-write + on-request (Auto)
    untrusted                         → read-only + on-request
    ``omni exec``                     → Never inside a write-capable sandbox
                                        (``workspace_auto``, not this module)
    library / tests (trusted is None) → factory defaults (untrusted +
                                        workspace-write)
"""

from __future__ import annotations

from typing import Any

from omni.config.settings import ConfigSource


def field_is_explicit(sources: dict[str, ConfigSource], dotted: str) -> bool:
    """Whether a dotted field was set by a config layer, not the factory default."""
    source = sources.get(dotted)
    return source is not None and source.kind != "default"


def apply_codex_security_preset(
    settings: Any,
    sources: dict[str, ConfigSource],
    trusted: bool | None,
) -> None:
    """Mutate ``settings.security`` to the Codex preset for this trust decision."""
    if trusted is None:
        return
    security = getattr(settings, "security", None)
    if security is None:
        return
    if trusted:
        if not field_is_explicit(sources, "security.approval_policy"):
            security.approval_policy = "on-request"
        if not field_is_explicit(sources, "security.bash_sandbox"):
            security.bash_sandbox = "workspace-write"
        return
    security.bash_sandbox = "readonly"
    if not field_is_explicit(sources, "security.approval_policy"):
        security.approval_policy = "on-request"


def security_preset_label(settings: Any, *, trusted: bool | None) -> str:
    """One status-line description of the effective Codex combination."""
    security = getattr(settings, "security", None)
    policy = str(getattr(security, "approval_policy", "untrusted") or "untrusted")
    sandbox = str(getattr(security, "bash_sandbox", "workspace-write") or "workspace-write")
    if trusted is False:
        return f"{policy} + {sandbox} (untrusted read-only)"
    if trusted is True:
        return f"{policy} + {sandbox} (trusted Auto)"
    return f"{policy} + {sandbox}"


__all__ = [
    "apply_codex_security_preset",
    "field_is_explicit",
    "security_preset_label",
]
