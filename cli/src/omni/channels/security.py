"""Inbound authorization and pairing for IM channels."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import tomli_w

from omni.channels.config import load_channel_config
from omni.config.settings import OmniSettings, read_toml_file
from omni.runtime.presentation import TurnPresentation

IM_CHANNELS = {"wechat", "feishu", "dingtalk"}
PAIR_RE = re.compile(r"^/pair\s+([A-Za-z0-9_-]+)\s*$", re.IGNORECASE)
_INBOUND_DEDUPE_TTL_SECONDS = 24 * 60 * 60
_INBOUND_FALLBACK_WINDOW_SECONDS = 5 * 60


@dataclass(frozen=True)
class AuthorizationResult:
    allowed: bool
    response: TurnPresentation | None = None


def security_defaults() -> dict[str, Any]:
    """Return secure-by-default channel policy fields."""
    return {
        "allowlist_enabled": True,
        "allowed_external_keys": [],
        "pairing_enabled": True,
        "pairing_code_hash": "",
        "pairing_code_salt": "",
        "pairing_expires_at": "",
        "require_sensitive_confirm": True,
    }


def with_security_defaults(config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config)
    for key, value in security_defaults().items():
        out.setdefault(key, value)
    return out


def authorize_channel_message(
    settings: OmniSettings,
    channel: str,
    external_key: str,
    text: str,
) -> AuthorizationResult:
    """Authorize one inbound IM message before it reaches the agent."""
    if channel not in IM_CHANNELS:
        return AuthorizationResult(True)
    cfg = with_security_defaults(load_channel_config(settings, channel))
    if not bool(cfg.get("allowlist_enabled", True)):
        return AuthorizationResult(True)
    allowed = {str(v) for v in cfg.get("allowed_external_keys") or []}
    if external_key in allowed:
        return AuthorizationResult(True)
    pair_match = PAIR_RE.match(text.strip())
    if pair_match and bool(cfg.get("pairing_enabled", True)):
        if _pairing_code_valid(cfg, pair_match.group(1)):
            add_allowed_external_key(settings.paths.channels_dir / f"{channel}.toml", external_key)
            return AuthorizationResult(
                False,
                TurnPresentation(
                    assistant_text="Pairing complete. This conversation can now use OmniScientist."
                ),
            )
        return AuthorizationResult(
            False,
            TurnPresentation(assistant_text="The pairing code is invalid or expired. Run `omni channel login` locally again."),
        )
    return AuthorizationResult(
        False,
        TurnPresentation(
            assistant_text=(
                "This IM conversation is not paired with the local OmniScientist instance.\n\n"
                f"Run `omni channel login {channel}` locally to obtain a one-time code, "
                "then send `/pair <code>` here."
            )
        ),
    )


def create_pairing_code(config_path: Path, *, ttl_seconds: int = 600) -> str:
    """Create and persist a short-lived pairing code for one channel config."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(16)
    data = _read_config(config_path)
    data.update(with_security_defaults(data))
    data["pairing_enabled"] = True
    data["pairing_code_salt"] = salt
    data["pairing_code_hash"] = _hash_pairing_code(salt, code)
    data["pairing_expires_at"] = (
        datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    ).isoformat(timespec="seconds")
    _write_config(config_path, data)
    return code


def add_allowed_external_key(config_path: Path, external_key: str) -> None:
    """Persist an allowed chat/user key for a channel."""
    data = _read_config(config_path)
    data.update(with_security_defaults(data))
    allowed = [str(v) for v in data.get("allowed_external_keys") or []]
    if external_key not in allowed:
        allowed.append(external_key)
    data["allowed_external_keys"] = allowed
    data["pairing_code_hash"] = ""
    data["pairing_code_salt"] = ""
    data["pairing_expires_at"] = ""
    _write_config(config_path, data)


def channel_requires_sensitive_confirm(settings: OmniSettings, channel: str) -> bool:
    """Whether IM-originated tool calls should require local confirmation."""
    if channel not in IM_CHANNELS:
        return False
    cfg = with_security_defaults(load_channel_config(settings, channel))
    return bool(cfg.get("require_sensitive_confirm", True))


def claim_inbound_message(
    settings: OmniSettings,
    channel: str,
    external_key: str,
    text: str,
    *,
    message_id: str = "",
    event_id: str = "",
    fallback_window_seconds: int = _INBOUND_FALLBACK_WINDOW_SECONDS,
) -> bool:
    """Return True once for each IM inbound event.

    CLI/local text input is intentionally outside this path. IM providers may
    redeliver the same event after a retry or websocket reconnect; this guard
    keeps the retry at the channel boundary so the shared agent still sees
    ordinary repeated CLI/user messages.
    """
    if channel not in IM_CHANNELS:
        return True
    now = datetime.now(UTC)
    key, ttl = _inbound_dedupe_key(
        channel,
        external_key,
        text,
        message_id=message_id,
        event_id=event_id,
        fallback_window_seconds=fallback_window_seconds,
    )
    path = settings.paths.project_dir / "channel_inbound_seen.json"
    store = _read_seen_store(path, now)
    if key in store:
        return False
    store[key] = {
        "channel": channel,
        "external_key_hash": hashlib.sha256(external_key.encode()).hexdigest()[:16],
        "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
    }
    _write_seen_store(path, store)
    return True


def _pairing_code_valid(cfg: dict[str, Any], code: str) -> bool:
    expected = str(cfg.get("pairing_code_hash") or "")
    salt = str(cfg.get("pairing_code_salt") or "")
    expires_at = str(cfg.get("pairing_expires_at") or "")
    if not expected or not salt or not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if datetime.now(UTC) > expires:
        return False
    return secrets.compare_digest(expected, _hash_pairing_code(salt, code))


def _inbound_dedupe_key(
    channel: str,
    external_key: str,
    text: str,
    *,
    message_id: str,
    event_id: str,
    fallback_window_seconds: int,
) -> tuple[str, int]:
    event_key = (message_id or event_id or "").strip()
    if event_key:
        return (
            "event:" + hashlib.sha256(f"{channel}:{external_key}:{event_key}".encode()).hexdigest(),
            _INBOUND_DEDUPE_TTL_SECONDS,
        )
    normalized_text = " ".join(text.split())
    digest = hashlib.sha256(
        f"{channel}:{external_key}:{normalized_text}".encode()
    ).hexdigest()
    return f"fallback:{digest}", max(1, fallback_window_seconds)


def _read_seen_store(path: Path, now: datetime) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        expires_at = str(value.get("expires_at") or "")
        try:
            expires = datetime.fromisoformat(expires_at)
        except ValueError:
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires > now:
            out[str(key)] = value
    return out


def _write_seen_store(path: Path, data: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _hash_pairing_code(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def _read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = read_toml_file(path)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(data, fh)
