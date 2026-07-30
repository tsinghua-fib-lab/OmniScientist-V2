"""Access channels: CLI plus WeChat/Feishu/DingTalk adapters.

Every channel is both an *inbound* adapter (turns external messages into
``agent.handle_turn`` calls) and an outbound :class:`Notifier` (delivers
async task completions). CLI is local and built in; IM channels are configured
through ``omni channel`` and run through ``omni serve``.
"""

from __future__ import annotations

__all__ = ["Channel", "build_channels"]


def build_channels(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
    """Lazily import channel builders to avoid package import cycles."""
    from omni.channels.base import build_channels as _build_channels

    return _build_channels(*args, **kwargs)


def __getattr__(name: str):  # noqa: ANN202
    if name == "Channel":
        from omni.channels.base import Channel

        return Channel
    raise AttributeError(name)
