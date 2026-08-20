"""Home-scoped Web RPC for configuring and observing IM channels.

This surface only provisions the existing channel runtime.  It never handles
messages, tasks or agent execution, and it never serializes provider secrets.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from starlette.requests import Request

from omni.channels.config import load_channel_config
from omni.channels.manager import request_reload
from omni.channels.provisioning import (
    CHANNEL_FIELDS,
    clean_text,
    complete_wechat_login,
    configure_static_channel,
    enabled_channels,
    issue_pairing,
    public_config,
    safe_url,
    secret_is_set,
    set_enabled,
    validate_channel,
)
from omni.channels.weixin_ilink import (
    DEFAULT_BASE_URL,
    DEFAULT_BOT_TYPE,
    WeixinIlinkClient,
)
from omni.config import load_settings
from omni.runtime.service_control import lazy_enable
from omni.runtime.service_state import observe_service
from omni.web.home_guard import (
    RESTART_REQUIRED_CODE,
    RESTART_REQUIRED_MESSAGE,
    home_has_drifted,
    resolved_home,
)
from omni.web.protocol import RpcError

CHANNEL_METHODS = frozenset(
    {
        "channel.describe",
        "channel.configure",
        "channel.enable",
        "channel.disable",
        "channel.reconnect",
        "channel.pair",
        "channel.wechat.start",
        "channel.wechat.status",
        "channel.wechat.verify",
        "channel.wechat.cancel",
    }
)

_LOGIN_TTL_SECONDS = 5 * 60
_TERMINAL_KEEP_SECONDS = 3 * 60
_MAX_QR_REFRESHES = 3
_POLL_SLEEP_SECONDS = 1.0
_LIVE_LOGIN_STATES = frozenset({"waiting", "scanned", "verification_required"})
_TERMINAL_LOGIN_STATES = frozenset({"succeeded", "expired", "error"})
_LABELS = {"wechat": "WeChat", "feishu": "Feishu", "dingtalk": "DingTalk"}
_WECHAT_AUTH_EXPIRED_REASON = "WeChat login expired; scan the QR code again."


@dataclass
class _WechatAttempt:
    client: Any
    qrcode: str
    qr_url: str
    base_url: str
    omni_home: str
    deadline: float
    expires_at: str
    refresh_count: int = 0
    state: str = "waiting"
    message: str = ""
    verify_code: str = ""
    verify_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    service_ready: bool = False
    allowed_count: int = 0


class WechatLoginRegistry:
    """Process-local holder for one backend-owned WeChat login task."""

    def __init__(self) -> None:
        self._attempts: dict[str, _WechatAttempt] = {}

    def add(self, attempt: _WechatAttempt) -> str:
        self.cleanup()
        # One process keeps at most one live QR.  Starting again cancels the
        # previous backend task; closing a browser tab does not.
        for old in list(self._attempts.values()):
            if old.task is not None and not old.task.done():
                old.task.cancel()
        self._attempts.clear()
        login_id = secrets.token_urlsafe(18)
        self._attempts[login_id] = attempt
        return login_id

    def get(self, login_id: str) -> _WechatAttempt:
        self.cleanup()
        attempt = self._attempts.get(login_id)
        if attempt is None:
            raise RpcError("login_not_found", "WeChat login expired or was cancelled")
        return attempt

    def current(self) -> tuple[str, _WechatAttempt] | None:
        self.cleanup()
        items = list(self._attempts.items())
        if len(items) != 1:
            return None
        return items[0]

    def cancel(self, login_id: str) -> bool:
        attempt = self._attempts.pop(login_id, None)
        if attempt is None:
            return False
        if attempt.task is not None and not attempt.task.done():
            attempt.task.cancel()
        return True

    def remove(self, login_id: str) -> bool:
        return self._attempts.pop(login_id, None) is not None

    def is_current(self, login_id: str, attempt: _WechatAttempt) -> bool:
        """Identity check used after a provider poll before committing its result."""
        self.cleanup()
        return self._attempts.get(login_id) is attempt

    def cleanup(self) -> None:
        now = time.monotonic()
        for login_id, attempt in list(self._attempts.items()):
            if attempt.deadline <= now:
                if attempt.task is not None and not attempt.task.done():
                    attempt.task.cancel()
                self._attempts.pop(login_id, None)


async def handle_channel(request: Request, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one ``channel.*`` call against owner-home configuration."""
    registry = _registry(request)
    try:
        settings = load_settings(trusted=True)
        paths = settings.paths
        if paths is None:  # defensive: shipping settings always resolve owner paths
            raise RpcError("configuration_unavailable", "Omni home is unavailable")
        if method == "channel.describe":
            _allow_params(params, set())
            # Existing CLI logins may keep credentials in macOS Keychain.
            # Presence checks are metadata-only, but still subprocess I/O and
            # must not block the ASGI event loop during periodic UI polling.
            described = await asyncio.to_thread(describe_channels, settings)
            login = _current_wechat_payload(registry)
            if login is not None:
                described["wechat_login"] = login
            if home_has_drifted(request.app, settings):
                described["restart_required"] = True
                described["notice"] = RESTART_REQUIRED_MESSAGE
            return described
        if method == "channel.configure":
            _allow_params(params, {"channel", "public_id", "secret", "bot_url", "setup_url"})
            channel = validate_channel(params.get("channel"), configurable_only=True)
            configure_static_channel(
                paths,
                channel,
                public_id=params.get("public_id"),
                secret=params.get("secret"),
                bot_url=params.get("bot_url"),
                setup_url=params.get("setup_url"),
            )
            pairing = issue_pairing(paths, channel)
            await _reload_and_start(settings, channel)
            return {"channel": _one_state(load_settings(trusted=True), channel), "pairing": pairing}
        if method in {"channel.enable", "channel.disable"}:
            _allow_params(params, {"channel"})
            channel = validate_channel(params.get("channel"))
            enabled = method == "channel.enable"
            if enabled and not _is_configured(paths, channel):
                raise ValueError(f"{channel} is not configured")
            set_enabled(paths, channel, enabled)
            request_reload(paths.channels_dir)
            if enabled:
                await _start_service(settings, channel)
            return {"channel": _one_state(load_settings(trusted=True), channel)}
        if method == "channel.reconnect":
            _allow_params(params, {"channel"})
            channel = validate_channel(params.get("channel"))
            if not _is_configured(paths, channel):
                raise ValueError(f"{channel} is not configured")
            set_enabled(paths, channel, True)
            await _reload_and_start(settings, channel)
            return {"channel": _one_state(load_settings(trusted=True), channel)}
        if method == "channel.pair":
            _allow_params(params, {"channel"})
            channel = validate_channel(params.get("channel"), configurable_only=True)
            pairing = issue_pairing(paths, channel)
            request_reload(paths.channels_dir)
            return {"channel": _one_state(load_settings(trusted=True), channel), "pairing": pairing}
        if method == "channel.wechat.start":
            _allow_params(params, set())
            return await _wechat_start(settings, registry)
        if method == "channel.wechat.status":
            _allow_params(params, {"login_id"})
            return _wechat_read(registry, _login_id(params.get("login_id")))
        if method == "channel.wechat.verify":
            _allow_params(params, {"login_id", "code"})
            login_id = _login_id(params.get("login_id"))
            code = clean_text(params.get("code"), field="code", required=True, max_length=64)
            return _wechat_submit_verify(registry, login_id, code)
        if method == "channel.wechat.cancel":
            _allow_params(params, {"login_id"})
            login_id = _login_id(params.get("login_id"))
            registry.cancel(login_id)
            return {"login_id": login_id, "state": "cancelled"}
    except RpcError:
        raise
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RpcError("channel_config_error", "Channel configuration could not be saved") from exc
    except ValueError as exc:
        raise RpcError("invalid_params", str(exc)) from exc
    raise RpcError("unknown_method", f"unknown method: {method}")


def describe_channels(settings: Any) -> dict[str, Any]:
    observation = observe_service(settings.paths)
    runtime = observation.runtime if isinstance(observation.runtime, dict) else {}
    health = runtime.get("channel_health") if isinstance(runtime, dict) else {}
    health = health if isinstance(health, dict) else {}
    payload = {
        "channels": [
            _state(settings, name, observation.phase, health.get(name))
            for name in ("wechat", "feishu", "dingtalk")
        ],
        "service": {"phase": observation.phase},
    }
    return payload


def _one_state(settings: Any, channel: str) -> dict[str, Any]:
    described = describe_channels(settings)
    return next(row for row in described["channels"] if row["name"] == channel)


def _state(
    settings: Any,
    channel: str,
    service_phase: str,
    runtime: Any,
) -> dict[str, Any]:
    paths = settings.paths
    raw = public_config(paths, channel)
    enabled = channel in enabled_channels(paths)
    public_key, secret_key = CHANNEL_FIELDS.get(channel, ("account_id", "bot_token"))
    public_id = str(raw.get(public_key) or "")
    has_secret = secret_is_set(paths, channel, secret_key)
    # WeChat matches the manager: public wechat.toml plus a bot_token.
    # account_id is display-only and may be missing on older official logins.
    configured = (
        _channel_file_present(paths, channel) and has_secret
        if channel == "wechat"
        else bool(public_id and has_secret)
    )
    runtime_state, runtime_reason = _runtime_state(
        enabled=enabled,
        configured=configured,
        service_phase=service_phase,
        runtime=runtime,
    )
    allowed = raw.get("allowed_external_keys")
    bot_url = _safe_stored_url(raw.get("bot_url"))
    setup_url = _safe_stored_url(raw.get("setup_url"))
    return {
        "name": channel,
        "label": _LABELS[channel],
        "mode": (
            "ilink"
            if channel == "wechat"
            else str(raw.get("mode") or {"feishu": "ws", "dingtalk": "stream"}[channel])
        ),
        "enabled": enabled,
        "configured": configured,
        "public_id": public_id,
        "secret_set": has_secret,
        "runtime_state": runtime_state,
        "runtime_reason": runtime_reason,
        "allowed_count": len(allowed) if isinstance(allowed, list) else 0,
        "bot_url": bot_url,
        "setup_url": setup_url,
    }


def _runtime_state(
    *, enabled: bool, configured: bool, service_phase: str, runtime: Any
) -> tuple[str, str]:
    if not configured:
        return "not_configured", "Configuration is incomplete."
    if not enabled:
        return "disabled", "Channel is disabled."
    if service_phase == "starting":
        return "starting", "Home service is starting."
    if service_phase != "ready":
        return "disconnected", "Home service is not connected."
    item = runtime if isinstance(runtime, dict) else {}
    status = str(item.get("status") or "")
    reason = str(item.get("reason") or "")
    if status == "running":
        if reason == "starting":
            return "starting", "Channel adapter is starting."
        if reason == "ok":
            # Manager health proves that the adapter task is alive, not that
            # the provider has acknowledged a transport handshake.
            return "running", "Channel adapter is running."
    if status in {"configured", "starting", ""}:
        return "starting", "Channel adapter is starting."
    if status == "disabled":
        return "disconnected", "Channel adapter is not running."
    if status == "not_configured":
        return "not_configured", "Configuration is incomplete."
    return "degraded", _safe_runtime_reason(reason)


def _safe_runtime_reason(reason: str) -> str:
    low = reason.strip().lower()
    if low.startswith("another omni serve owns this channel"):
        return "Another Omni service owns this channel."
    if low.startswith("missing dependency "):
        package = re.sub(r"[^a-z0-9_.-]", "", low.removeprefix("missing dependency "))
        return f"Missing optional dependency {package}." if package else "Missing optional dependency."
    known = {
        "waiting before retry": "Waiting before retry.",
        "lock held by another daemon": "Another Omni service owns this channel.",
        "adapter exited": "Channel adapter exited; retry start.",
        _WECHAT_AUTH_EXPIRED_REASON.lower(): _WECHAT_AUTH_EXPIRED_REASON,
        "": "Channel runtime reported an error.",
    }
    return known.get(low, "Channel runtime reported an error.")


async def _wechat_start(settings: Any, registry: WechatLoginRegistry) -> dict[str, Any]:
    client = WeixinIlinkClient.from_config(load_channel_config(settings, "wechat"))
    try:
        qr = await client.get_bot_qrcode()
        url = _official_wechat_url(qr.qrcode_url, field="qr_url")
        opaque_qr = clean_text(
            qr.qrcode,
            field="qrcode",
            required=True,
            max_length=4096,
        )
        _qr_matrix(url)
    except Exception as exc:
        raise RpcError("channel_unavailable", "Could not start WeChat login; retry shortly") from exc
    expires = datetime.now(UTC) + timedelta(seconds=_LOGIN_TTL_SECONDS)
    attempt = _WechatAttempt(
        client=client,
        qrcode=opaque_qr,
        qr_url=url,
        base_url=DEFAULT_BASE_URL,
        omni_home=resolved_home(settings),
        deadline=time.monotonic() + _LOGIN_TTL_SECONDS,
        expires_at=expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    login_id = registry.add(attempt)
    attempt.task = asyncio.create_task(
        _wechat_run(registry, login_id, attempt),
        name=f"wechat-login:{login_id[:8]}",
    )
    return _wechat_payload(login_id, attempt)


def _wechat_read(registry: WechatLoginRegistry, login_id: str) -> dict[str, Any]:
    return _wechat_payload(login_id, registry.get(login_id))


def _wechat_submit_verify(
    registry: WechatLoginRegistry, login_id: str, code: str
) -> dict[str, Any]:
    attempt = registry.get(login_id)
    attempt.verify_code = code
    attempt.verify_event.set()
    return _wechat_payload(login_id, attempt)


def _current_wechat_payload(registry: WechatLoginRegistry) -> dict[str, Any] | None:
    current = registry.current()
    if current is None:
        return None
    login_id, attempt = current
    return _wechat_payload(login_id, attempt)


async def _wechat_run(
    registry: WechatLoginRegistry, login_id: str, attempt: _WechatAttempt
) -> None:
    try:
        await _wechat_drive(registry, login_id, attempt)
    except asyncio.CancelledError:
        raise
    except Exception:
        if registry.is_current(login_id, attempt):
            _mark_terminal(attempt, "error", "WeChat login status is temporarily unavailable")


async def _wechat_drive(
    registry: WechatLoginRegistry, login_id: str, attempt: _WechatAttempt
) -> None:
    pending_code = ""
    while registry.is_current(login_id, attempt):
        data = await _poll_provider(attempt, pending_code)
        if not registry.is_current(login_id, attempt):
            return
        pending_code, done = await _advance_login(
            registry, login_id, attempt, data, pending_code
        )
        if done:
            return


async def _poll_provider(attempt: _WechatAttempt, pending_code: str) -> dict[str, Any]:
    try:
        data = await attempt.client.poll_qr_status(
            attempt.qrcode,
            verify_code=pending_code,
            base=attempt.base_url,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        await asyncio.sleep(_POLL_SLEEP_SECONDS)
        return {"status": "wait"}
    return data if isinstance(data, dict) else {"status": "wait"}


async def _advance_login(
    registry: WechatLoginRegistry,
    login_id: str,
    attempt: _WechatAttempt,
    data: dict[str, Any],
    pending_code: str,
) -> tuple[str, bool]:
    status = str(data.get("status") or "wait")
    if status in {"wait", "waiting"}:
        attempt.state = "waiting"
        if attempt.message.startswith("Enter the verification"):
            attempt.message = ""
        await asyncio.sleep(_POLL_SLEEP_SECONDS)
        return "", False
    if status in {"scaned", "scanned"}:
        attempt.state = "scanned"
        attempt.message = ""
        await asyncio.sleep(_POLL_SLEEP_SECONDS)
        return pending_code, False
    if status == "need_verifycode":
        attempt.state = "verification_required"
        attempt.message = "Enter the verification code shown in WeChat."
        await attempt.verify_event.wait()
        attempt.verify_event.clear()
        code = attempt.verify_code
        attempt.verify_code = ""
        return code, False
    if status == "scaned_but_redirect":
        host = str(data.get("redirect_host") or "").strip()
        if host:
            try:
                attempt.base_url = _official_wechat_url(f"https://{host}", field="redirect_host")
            except ValueError:
                _mark_terminal(attempt, "error", "WeChat returned an invalid redirect")
                return "", True
        attempt.state = "scanned"
        return pending_code, False
    if status in {"expired", "verify_code_blocked"}:
        await _refresh_or_expire(attempt)
        return "", attempt.state in _TERMINAL_LOGIN_STATES
    if status == "binded_redirect":
        _mark_terminal(
            attempt,
            "error",
            "This WeChat bot is already connected on another session.",
        )
        return "", True
    if status == "confirmed":
        await _finish_confirmed(registry, login_id, attempt, data)
        return "", True
    await asyncio.sleep(_POLL_SLEEP_SECONDS)
    return pending_code, False


async def _refresh_or_expire(attempt: _WechatAttempt) -> None:
    attempt.refresh_count += 1
    if attempt.refresh_count > _MAX_QR_REFRESHES:
        _mark_terminal(
            attempt,
            "expired",
            "The WeChat QR code expired repeatedly. Start a new login.",
        )
        return
    try:
        qr = await attempt.client.get_bot_qrcode(base=attempt.base_url)
        attempt.qrcode = clean_text(
            qr.qrcode,
            field="qrcode",
            required=True,
            max_length=4096,
        )
        attempt.qr_url = _official_wechat_url(qr.qrcode_url, field="qr_url")
    except Exception:
        _mark_terminal(attempt, "error", "Could not refresh the WeChat QR code")
        return
    attempt.deadline = time.monotonic() + _LOGIN_TTL_SECONDS
    expires = datetime.now(UTC) + timedelta(seconds=_LOGIN_TTL_SECONDS)
    attempt.expires_at = expires.isoformat(timespec="seconds").replace("+00:00", "Z")
    attempt.state = "waiting"
    attempt.message = "The QR code was refreshed."


async def _finish_confirmed(
    registry: WechatLoginRegistry,
    login_id: str,
    attempt: _WechatAttempt,
    data: dict[str, Any],
) -> None:
    settings = load_settings(trusted=True)
    try:
        _require_attempt_home(settings, attempt)
    except RpcError as exc:
        _mark_terminal(attempt, "error", exc.message)
        return
    token = str(data.get("bot_token") or "")
    account = str(data.get("ilink_bot_id") or "")
    user_id = str(data.get("ilink_user_id") or "").strip()
    if not token or not account or not user_id:
        _mark_terminal(attempt, "error", "WeChat did not return a complete login")
        return
    if not registry.is_current(login_id, attempt):
        return
    try:
        complete_wechat_login(
            settings.paths,
            bot_token=token,
            account_id=account,
            base_url=_official_wechat_url(
                str(data.get("baseurl") or attempt.base_url or DEFAULT_BASE_URL),
                field="base_url",
            ),
            user_id=user_id,
            bot_type=DEFAULT_BOT_TYPE,
        )
    except (ValueError, OSError):
        _mark_terminal(attempt, "error", "WeChat login could not be saved")
        return
    if not registry.is_current(login_id, attempt):
        return
    fresh = load_settings(trusted=True)
    service_ready = False
    if resolved_home() == attempt.omni_home and resolved_home(fresh) == attempt.omni_home:
        service_ready = await _reload_and_start(fresh, "wechat")
    allowed = public_config(fresh.paths, "wechat").get("allowed_external_keys")
    attempt.allowed_count = len(allowed) if isinstance(allowed, list) else 0
    attempt.service_ready = service_ready
    _mark_terminal(
        attempt,
        "succeeded",
        "WeChat connected." if service_ready else "WeChat authorized. Starting the message service…",
    )


def _mark_terminal(attempt: _WechatAttempt, state: str, message: str = "") -> None:
    attempt.state = state
    attempt.message = message
    attempt.qrcode = ""
    attempt.qr_url = ""
    attempt.deadline = time.monotonic() + _TERMINAL_KEEP_SECONDS


def _require_attempt_home(settings: Any, attempt: _WechatAttempt) -> None:
    live = resolved_home()
    captured = resolved_home(settings)
    if live == attempt.omni_home and captured == attempt.omni_home:
        return
    raise RpcError(RESTART_REQUIRED_CODE, RESTART_REQUIRED_MESSAGE)


def _channel_file_present(paths: Any, channel: str) -> bool:
    return (paths.channels_dir / f"{channel}.toml").is_file()


def _wechat_payload(login_id: str, attempt: _WechatAttempt) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "login_id": login_id,
        "state": attempt.state,
    }
    if attempt.state in _LIVE_LOGIN_STATES:
        payload["expires_at"] = attempt.expires_at
        if attempt.qr_url:
            payload["qr_matrix"] = _qr_matrix(attempt.qr_url)
    if attempt.message:
        payload["message"] = attempt.message
    if attempt.state == "succeeded":
        payload["service_ready"] = attempt.service_ready
        payload["allowed_count"] = attempt.allowed_count
    return payload


def _qr_matrix(value: str) -> list[list[bool]]:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=1, border=4)
    qr.add_data(value)
    qr.make(fit=True)
    return [[bool(cell) for cell in row] for row in qr.get_matrix()]


async def _reload_and_start(settings: Any, channel: str) -> bool:
    request_reload(settings.paths.channels_dir)
    return await _start_service(settings, channel)


async def _start_service(settings: Any, channel: str) -> bool:
    try:
        result = await asyncio.to_thread(
            lazy_enable,
            settings,
            reason=f"channel:{channel}",
        )
    except Exception:  # lifecycle details stay in local service diagnostics
        return False
    return bool(getattr(result, "ok", False))


def _is_configured(paths: Any, channel: str) -> bool:
    raw = public_config(paths, channel)
    public_key, secret_key = CHANNEL_FIELDS.get(channel, ("account_id", "bot_token"))
    if channel == "wechat":
        return _channel_file_present(paths, "wechat") and secret_is_set(
            paths, "wechat", "bot_token"
        )
    return bool(str(raw.get(public_key) or "").strip()) and secret_is_set(paths, channel, secret_key)


def _registry(request: Request) -> WechatLoginRegistry:
    registry = getattr(request.app.state, "wechat_login_registry", None)
    if registry is None:
        registry = WechatLoginRegistry()
        request.app.state.wechat_login_registry = registry
    return registry


def _login_id(value: Any) -> str:
    return clean_text(value, field="login_id", required=True, max_length=128)


def _allow_params(params: dict[str, Any], allowed: set[str]) -> None:
    extra = sorted(set(params) - allowed)
    if extra:
        raise ValueError(f"unexpected parameter(s): {', '.join(extra)}")


def _safe_stored_url(value: Any) -> str:
    try:
        return safe_url(value, field="url")
    except ValueError:
        return ""


def _official_wechat_url(value: Any, *, field: str) -> str:
    """Accept only TLS endpoints owned by the official WeChat iLink service."""
    text = clean_text(value, field=field, required=True, max_length=2048)
    parsed = urlsplit(text)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or (host != "weixin.qq.com" and not host.endswith(".weixin.qq.com"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError(f"{field} is not an official WeChat HTTPS URL")
    return text


__all__ = [
    "CHANNEL_METHODS",
    "WechatLoginRegistry",
    "describe_channels",
    "handle_channel",
]
