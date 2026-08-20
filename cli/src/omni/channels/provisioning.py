"""Secret-safe provisioning primitives for the Web IM-channel surface.

The helpers form a small domain service that the CLI can reuse later.  For now
they keep Web-specific persistence out of the RPC adapter: public channel TOML,
credentials, enablement and one-time pairing.  They deliberately do not start
adapters or touch the agent/task runtime.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from omni.channels.credentials import secret_ref_exists, store_channel_secret
from omni.channels.security import (
    add_allowed_external_key,
    create_pairing_code,
    with_security_defaults,
)
from omni.config.secure_files import write_private_toml

CONFIGURABLE_CHANNELS = frozenset({"feishu", "dingtalk"})
IM_CHANNELS = frozenset({"wechat", *CONFIGURABLE_CHANNELS})
PAIRING_CHANNELS = CONFIGURABLE_CHANNELS

CHANNEL_FIELDS: dict[str, tuple[str, str]] = {
    "wechat": ("account_id", "bot_token"),
    "feishu": ("app_id", "app_secret"),
    "dingtalk": ("client_id", "client_secret"),
}

BASE_CONFIG: dict[str, dict[str, Any]] = {
    "wechat": {"mode": "ilink"},
    "feishu": {"mode": "ws", "app_id": "", "webhook_url": ""},
    "dingtalk": {"mode": "stream", "client_id": "", "webhook_url": ""},
}

DINGTALK_SETUP_URL = (
    "https://open.dingtalk.com/document/direction/stream-mode-protocol-access-description"
)

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def read_toml(path: Path) -> dict[str, Any]:
    """Read one owner config file, treating absence as an empty table."""
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def validate_channel(value: Any, *, configurable_only: bool = False) -> str:
    channel = str(value or "").strip().lower()
    allowed = CONFIGURABLE_CHANNELS if configurable_only else IM_CHANNELS
    if channel not in allowed:
        raise ValueError(f"unsupported channel: {channel or '(missing)'}")
    return channel


def clean_text(value: Any, *, field: str, required: bool, max_length: int) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_length:
        raise ValueError(f"{field} is too long")
    if _CONTROL_RE.search(text):
        raise ValueError(f"{field} contains control characters")
    return text


def safe_url(value: Any, *, field: str) -> str:
    text = clean_text(value, field=field, required=False, max_length=2048)
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field} must be an http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{field} must not contain credentials")
    return text


def public_config(paths: Any, channel: str) -> dict[str, Any]:
    return read_toml(paths.channels_dir / f"{channel}.toml")


def write_channel_config(paths: Any, channel: str, payload: dict[str, Any]) -> Path:
    """Atomically persist non-secret channel configuration owner-only."""
    channel = validate_channel(channel)
    path = paths.channels_dir / f"{channel}.toml"
    write_private_toml(path, payload)
    return path


def enabled_channels(paths: Any) -> list[str]:
    raw = read_toml(paths.config_file)
    channels = raw.get("channels") if isinstance(raw, dict) else None
    value = channels.get("enabled", ["cli"]) if isinstance(channels, dict) else ["cli"]
    if isinstance(value, str):
        candidates = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        candidates = [str(part).strip() for part in value]
    else:
        candidates = ["cli"]
    out: list[str] = []
    for name in candidates:
        if name and name not in out:
            out.append(name)
    if "cli" not in out:
        out.insert(0, "cli")
    return out


def set_enabled(paths: Any, channel: str, enabled: bool) -> list[str]:
    """Atomically update one channel without dropping unrelated config."""
    channel = validate_channel(channel)
    raw = read_toml(paths.config_file)
    names = enabled_channels(paths)
    if enabled and channel not in names:
        names.append(channel)
    if not enabled:
        names = [name for name in names if name != channel]
    raw.setdefault("channels", {})["enabled"] = names
    write_private_toml(paths.config_file, raw)
    return names


def secret_is_set(paths: Any, channel: str, key: str) -> bool:
    """Check credential presence without resolving or returning its value."""
    public = public_config(paths, channel)
    # Read-only compatibility for old channel files that predate split secret
    # storage.  The next configure call migrates this value before removing it.
    if bool(str(public.get(key) or "").strip()):
        return True
    raw_secret = read_toml(paths.secrets_file).get("channels", {})
    channel_secrets = raw_secret.get(channel, {}) if isinstance(raw_secret, dict) else {}
    if isinstance(channel_secrets, dict) and bool(str(channel_secrets.get(key) or "").strip()):
        return True
    refs = public.get("credential_refs")
    ref = str(refs.get(key) or "").strip() if isinstance(refs, dict) else ""
    return bool(ref and secret_ref_exists(ref))


def configure_static_channel(
    paths: Any,
    channel: str,
    *,
    public_id: Any,
    secret: Any = "",
    bot_url: Any = "",
    setup_url: Any = "",
) -> dict[str, Any]:
    """Persist Feishu/DingTalk config; blank secret preserves the old value."""
    channel = validate_channel(channel, configurable_only=True)
    public_key, secret_key = CHANNEL_FIELDS[channel]
    identifier = clean_text(public_id, field=public_key, required=True, max_length=256)
    secret_value = clean_text(secret, field=secret_key, required=False, max_length=4096)
    # Parse every file that this transaction will update before the first
    # write, so a malformed owner config cannot leave a half-configured login.
    enabled_channels(paths)
    existing = public_config(paths, channel)
    config = with_security_defaults({**BASE_CONFIG[channel], **existing})
    config[public_key] = identifier
    chat_link = safe_url(bot_url, field="bot_url")
    setup_link = safe_url(setup_url, field="setup_url")
    if channel == "feishu":
        config["bot_url"] = chat_link or str(
            existing.get("bot_url") or f"https://applink.feishu.cn/client/bot/open?appId={identifier}"
        )
        if setup_link:
            config["setup_url"] = setup_link
    else:
        if chat_link:
            config["bot_url"] = chat_link
        config["setup_url"] = setup_link or str(
            existing.get("setup_url") or DINGTALK_SETUP_URL
        )

    legacy_secret = clean_text(
        existing.get(secret_key),
        field=secret_key,
        required=False,
        max_length=4096,
    )
    if secret_value:
        _store_secret(paths, channel, secret_key, secret_value, config)
    elif legacy_secret:
        _store_secret(paths, channel, secret_key, legacy_secret, config)
    elif not secret_is_set(paths, channel, secret_key):
        raise ValueError(f"{secret_key} is required")

    # Never copy a legacy inline secret back into public TOML.
    config.pop(secret_key, None)
    write_channel_config(paths, channel, config)
    set_enabled(paths, channel, True)
    return config


def complete_wechat_login(
    paths: Any,
    *,
    bot_token: Any,
    account_id: Any,
    base_url: Any,
    user_id: Any = "",
    bot_type: Any = "3",
) -> dict[str, Any]:
    """Persist one confirmed iLink login without exposing its credential."""
    token = clean_text(bot_token, field="bot_token", required=True, max_length=4096)
    account = clean_text(account_id, field="account_id", required=True, max_length=256)
    base = safe_url(base_url, field="base_url")
    external_user = clean_text(user_id, field="user_id", required=False, max_length=512)
    kind = clean_text(bot_type, field="bot_type", required=True, max_length=32)

    enabled_channels(paths)
    existing = public_config(paths, "wechat")
    config = with_security_defaults({**BASE_CONFIG["wechat"], **existing})
    config.update({"mode": "ilink", "base_url": base, "bot_type": kind, "account_id": account})
    _store_secret(paths, "wechat", "bot_token", token, config)
    config.pop("bot_token", None)
    path = write_channel_config(paths, "wechat", config)
    set_enabled(paths, "wechat", True)
    if external_user:
        add_allowed_external_key(path, external_user)
        config = public_config(paths, "wechat")
    return config


def issue_pairing(paths: Any, channel: str, *, ttl_seconds: int = 600) -> dict[str, Any]:
    channel = validate_channel(channel, configurable_only=True)
    path = paths.channels_dir / f"{channel}.toml"
    if not path.is_file():
        raise ValueError(f"{channel} is not configured")
    code = create_pairing_code(path, ttl_seconds=ttl_seconds)
    expires_at = str(public_config(paths, channel).get("pairing_expires_at") or "")
    return {
        "code": code,
        "command": f"/pair {code}",
        "expires_at": expires_at,
        "expires_in_seconds": ttl_seconds,
    }


def _store_secret(
    paths: Any,
    channel: str,
    key: str,
    value: str,
    config: dict[str, Any],
) -> None:
    """Use Web's explicit, cross-platform secrets.toml backend.

    Unlike an interactive CLI login, a browser request must not trigger a
    hidden OS Keychain prompt.  This mirrors the existing Web model settings:
    local owner-only storage, atomically written with mode 0600.
    """
    store_channel_secret(paths, channel, key, value, backend="file")
    refs = config.get("credential_refs")
    if not isinstance(refs, dict):
        refs = {}
    refs.pop(key, None)
    if refs:
        config["credential_refs"] = refs
    else:
        config.pop("credential_refs", None)


__all__ = [
    "CHANNEL_FIELDS",
    "CONFIGURABLE_CHANNELS",
    "IM_CHANNELS",
    "PAIRING_CHANNELS",
    "clean_text",
    "complete_wechat_login",
    "configure_static_channel",
    "enabled_channels",
    "issue_pairing",
    "public_config",
    "read_toml",
    "safe_url",
    "secret_is_set",
    "set_enabled",
    "validate_channel",
    "write_channel_config",
]
