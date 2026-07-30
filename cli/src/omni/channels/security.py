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

# Product names written as Channel.name and ~/.omni/channels/<name>.toml.
IM_CHANNELS = {"wechat", "feishu", "dingtalk"}
# Names that still mean the same transport in logs, tests, or older configs.
_IM_CHANNEL_ALIASES = {
    "wechat": "wechat",
    "weixin": "wechat",
    "feishu": "feishu",
    "lark": "feishu",
    "dingtalk": "dingtalk",
    "dingding": "dingtalk",
}
PAIR_RE = re.compile(r"^/pair\s+([A-Za-z0-9_-]+)\s*$", re.IGNORECASE)
_INBOUND_DEDUPE_TTL_SECONDS = 24 * 60 * 60


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


def canonical_im_channel(channel: str) -> str | None:
    """Map a transport name to the product IM channel, or ``None`` if it is not IM."""
    return _IM_CHANNEL_ALIASES.get(str(channel or "").strip().lower())


def is_im_channel(channel: str) -> bool:
    """Whether this name is an IM transport (canonical product name or alias)."""
    return canonical_im_channel(channel) is not None


def completion_notify_channel(notify_channel: str, parent_channel: str = "") -> str:
    """Channel that receives the completion hop.

    Trust the stored notify. ``enqueue_notify_channel`` already decided:
    background and IM foreground that do not wait store the surface; an
    in-turn drain stores empty so the turn itself is the hop. Refilling
    from an IM parent re-pushes a card for work the turn already showed.

    ``parent_channel`` is accepted for call-site compatibility and ignored.
    """
    _ = parent_channel
    return str(notify_channel or "").strip()


def authorize_channel_message(
    settings: OmniSettings,
    channel: str,
    external_key: str,
    text: str,
) -> AuthorizationResult:
    """Authorize one inbound IM message before it reaches the agent."""
    channel = canonical_im_channel(channel) or ""
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
    channel = canonical_im_channel(channel) or ""
    if channel not in IM_CHANNELS:
        return False
    cfg = with_security_defaults(load_channel_config(settings, channel))
    return bool(cfg.get("require_sensitive_confirm", True))


def claim_inbound_message(
    settings: OmniSettings,
    channel: str,
    external_key: str,
    *,
    message_id: str = "",
    event_id: str = "",
) -> bool:
    """Return True once for each IM inbound event.

    CLI/local text input is intentionally outside this path. IM providers may
    redeliver the same event after a retry or websocket reconnect; this guard
    keeps the retry at the channel boundary so the shared agent still sees
    ordinary repeated CLI/user messages.

    Identity comes from the provider's own id and nowhere else. This used to
    fall back to hashing the message text when an id was missing, which made a
    person asking the same question twice within five minutes indistinguishable
    from the network delivering one question twice — and the second ask was
    dropped in silence. A repeat is a request, not a retransmission; without an
    id there is nothing to recognise, so the message is answered.
    """
    if not is_im_channel(channel):
        return True
    event = (message_id or event_id or "").strip()
    if not event:
        return True
    now = datetime.now(UTC)
    key = "event:" + sha256_hex(f"{channel}:{external_key}:{event}")
    ttl = _INBOUND_DEDUPE_TTL_SECONDS
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


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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
