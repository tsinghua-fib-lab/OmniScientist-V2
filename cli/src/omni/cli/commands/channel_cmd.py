"""`omni channel` — configure messaging channels (WeChat / Feishu / DingTalk).

The CLI channel is always available. IM channels use per-channel config under
``~/.omni/channels/<name>.toml`` and credentials in ``~/.omni/secrets.toml``.
The live polling/streaming runtime is started by ``omni serve``.
"""

from __future__ import annotations

import importlib.util
import time
import tomllib
from typing import Any

import httpx
import tomli_w
import typer

from omni.channels.config import load_channel_config
from omni.channels.credentials import (
    CredentialStoreError,
    keychain_available,
    resolve_secret_ref,
    store_channel_secret,
)
from omni.channels.manager import channel_config_state
from omni.channels.security import (
    add_allowed_external_key,
    create_pairing_code,
    security_defaults,
    with_security_defaults,
)
from omni.cli.qr import render_terminal_qr
from omni.cli.render import confirm, console, data_table, error, info, prompt_secret, success, warn
from omni.runtime.daemon import daemon_info

app = typer.Typer(help="Configure messaging channels.", no_args_is_help=True)

_KNOWN = ("cli", "wechat", "feishu", "dingtalk")

# Minimal config templates. Secrets (tokens/app secrets) belong in
# ~/.omni/secrets.toml under [channels.<name>] or the system keychain, never here.
_BASE_TEMPLATES: dict[str, dict[str, Any]] = {
    "wechat": {
        "mode": "gateway",
        "gateway_url": "http://127.0.0.1:8088",
        "inbox_path": "/messages",
        "send_path": "/send",
        "login_qr_path": "/login/qrcode",
        "login_status_path": "/login/status",
        "poll_interval": 2,
    },
    "feishu": {
        "mode": "ws",
        "app_id": "",
        "webhook_url": "",
    },
    "dingtalk": {
        "mode": "stream",
        "client_id": "",
        "webhook_url": "",
    },
}

# Personal-WeChat local gateway template (used by `--method gateway`).
_WECHAT_GATEWAY_TEMPLATE: dict[str, Any] = {
    "mode": "gateway",
    "gateway_url": "http://127.0.0.1:8088",
    "inbox_path": "/messages",
    "send_path": "/send",
    "login_qr_path": "/login/qrcode",
    "login_status_path": "/login/status",
    "poll_interval": 2,
}

_REQUIRED: dict[str, tuple[str, ...]] = {
    "feishu": ("app_id", "app_secret"),
    "dingtalk": ("client_id", "client_secret"),
}


def _required_fields(name: str, data: dict[str, Any]) -> tuple[str, ...]:
    """Required config fields for a channel, mode-aware for WeChat."""
    if name == "wechat":
        from omni.channels.wechat import resolve_wechat_mode

        mode = resolve_wechat_mode(data)
        if mode == "ilink":
            return ("bot_token",)
        if mode == "wecom":
            return ("gateway_url", "inbox_path", "send_path")
        return ("gateway_url", "inbox_path", "send_path")
    return _REQUIRED.get(name, ())

_DINGTALK_SETUP_URL = "https://open.dingtalk.com/document/direction/stream-mode-protocol-access-description"
_CHANNEL_SUBCOMMANDS = ("list", "add", "login", "remove", "test", "help")


def _enabled(paths) -> list[str]:  # noqa: ANN001
    data = tomllib.loads(paths.config_file.read_text()) if paths.config_file.is_file() else {}
    chans = data.get("channels", {})
    return list(chans.get("enabled", ["cli"]))


def _write_enabled(paths, names: list[str]) -> None:  # noqa: ANN001
    data = tomllib.loads(paths.config_file.read_text()) if paths.config_file.is_file() else {}
    data.setdefault("channels", {})["enabled"] = names
    paths.home.mkdir(parents=True, exist_ok=True)
    with paths.config_file.open("wb") as fh:
        tomli_w.dump(data, fh)


def _template(name: str) -> dict[str, Any]:
    return with_security_defaults(dict(_BASE_TEMPLATES.get(name, {})))


def _read_config(path) -> dict[str, Any]:  # noqa: ANN001
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_channel_config(paths, name: str, data: dict[str, Any]) -> None:  # noqa: ANN001
    paths.channels_dir.mkdir(parents=True, exist_ok=True)
    cfg = paths.channels_dir / f"{name}.toml"
    with cfg.open("wb") as fh:
        tomli_w.dump(data, fh)


def _enable(paths, name: str) -> list[str]:  # noqa: ANN001
    enabled = _enabled(paths)
    if name not in enabled:
        enabled.append(name)
        _write_enabled(paths, enabled)
    return enabled


def _bind_allowed(paths, name: str, values: list[str] | None) -> None:  # noqa: ANN001
    cfg = paths.channels_dir / f"{name}.toml"
    for value in values or []:
        if value.strip():
            add_allowed_external_key(cfg, value.strip())


def _load_effective_channel_config(paths, name: str) -> dict[str, Any]:  # noqa: ANN001
    cfg = _read_config(paths.channels_dir / f"{name}.toml")
    secrets = _read_config(paths.secrets_file).get("channels", {})
    secret_cfg = secrets.get(name, {}) if isinstance(secrets, dict) else {}
    out = dict(cfg)
    if isinstance(secret_cfg, dict):
        out.update(secret_cfg)
    refs = out.get("credential_refs")
    if isinstance(refs, dict):
        for key, ref in refs.items():
            if isinstance(key, str) and isinstance(ref, str):
                value = resolve_secret_ref(ref)
                if value:
                    out[key] = value
    return out


def render_channel_usage_help() -> None:
    """Render channel setup help with placeholders, never real credentials."""
    info("Use `/channel ...` in the REPL or `omni channel ...` in the shell.")
    info(f"Available subcommands: {', '.join(_CHANNEL_SUBCOMMANDS)}.")
    data_table(
        "channel subcommands",
        ["command", "purpose"],
        [
            ["list", "List channel enablement and configuration status"],
            ["add <name>", "Enable a channel and write a template without pairing"],
            ["login <name>", "Configure credentials and create a pairing code or QR code"],
            ["remove <name>", "Disable a channel; --purge also deletes its config"],
            ["test <name>", "Validate configuration and dependencies without connecting"],
            ["help", "Show help and examples"],
        ],
    )
    data_table(
        "Examples (replace placeholders)",
        ["scenario", "command"],
        [
            [
                "Log in to Feishu and start listening",
                "/channel login feishu --app-id <FEISHU_APP_ID> "
                "--app-secret '<FEISHU_APP_SECRET>' --credential-store file --start",
            ],
            ["Show channel status", "/channel list"],
            ["Validate Feishu configuration", "/channel test feishu"],
            ["Write a Feishu template", "/channel add feishu"],
            ["Disable Feishu", "/channel remove feishu"],
            [
                "Log in to DingTalk Stream",
                "/channel login dingtalk --client-id <DINGTALK_CLIENT_ID> "
                "--client-secret '<DINGTALK_CLIENT_SECRET>' --credential-store file --start",
            ],
            [
                "Managed WeChat gateway for production",
                "/channel login wechat --method wecom --gateway-url http://127.0.0.1:8088 --start",
            ],
            ["Local WeChat gateway; assess platform compliance", "/channel login wechat --method gateway "
             "--gateway-url http://127.0.0.1:8088 --start"],
            ["Experimental WeChat iLink connector", "/channel login wechat --method ilink "
             "--credential-store file --start"],
        ],
    )
    warn("Do not put real app secrets in documentation or screenshots; replace every placeholder.")
    info("After login, send `/pair <code>` to the bot. One `/serve start` handles all enabled channels.")


@app.command("help")
def help_cmd() -> None:
    """Show channel configuration examples."""
    render_channel_usage_help()


@app.command("list")
def list_cmd(ctx: typer.Context) -> None:
    """List channel enablement and configuration status."""
    paths = ctx.obj.settings().paths
    enabled = set(_enabled(paths))
    d = daemon_info(paths)
    health = d.get("channel_health") if isinstance(d, dict) else {}
    health = health if isinstance(health, dict) else {}
    rows = []
    for name in _KNOWN:
        cfg = paths.channels_dir / f"{name}.toml"
        raw = _read_config(cfg)
        allowed = len(raw.get("allowed_external_keys") or []) if isinstance(raw, dict) else 0
        configured, reason = channel_config_state(ctx.obj.settings(), name)
        runtime = health.get(name) if isinstance(health.get(name), dict) else {}
        runtime_status = str(runtime.get("status") or ("serve not running" if not d else "disabled"))
        runtime_reason = str(runtime.get("reason") or "")
        status = "built-in" if name == "cli" else f"allowlist={allowed}; {runtime_status}"
        if runtime_reason:
            status += f" ({runtime_reason})"
        rows.append([
            name,
            "✓" if name in enabled else "—",
            "✓" if configured else "—",
            status,
        ])
    data_table("Channels", ["name", "enabled", "configured", "status"], rows)


@app.command("add")
def add_cmd(ctx: typer.Context, name: str) -> None:
    """Enable a channel and write its configuration template."""
    if name not in _KNOWN:
        error(f"Unknown channel '{name}'. Choose one of: {', '.join(_KNOWN)}")
        raise typer.Exit(1)
    paths = ctx.obj.settings().paths
    paths.channels_dir.mkdir(parents=True, exist_ok=True)
    if name != "cli":
        cfg = paths.channels_dir / f"{name}.toml"
        if not cfg.is_file():
            with cfg.open("wb") as fh:
                tomli_w.dump(_template(name), fh)
            success(f"Wrote configuration template to {cfg}")
        else:
            info(f"Configuration already exists: {cfg}")
    enabled = _enable(paths, name)
    success(f"Enabled channel '{name}'. Enabled channels: {', '.join(enabled)}.")
    info("A running omni serve will reconcile it automatically; otherwise run `omni serve start`.")
    if name == "wechat":
        info("The default template uses a local gateway; use a managed enterprise gateway in production.")
        warn("Experimental iLink is enabled only with `channel login wechat --method ilink`.")
    if name != "cli":
        warn(f"`channel add` only writes a template; run `omni channel login {name}` to pair.")


@app.command("login")
def login_cmd(
    ctx: typer.Context,
    name: str,
    method: str = typer.Option("auto", "--method", help="wechat: ilink|gateway|wecom；feishu/dingtalk: manual|auto"),
    gateway_url: str = typer.Option("http://127.0.0.1:8088", "--gateway-url", help="WeChat gateway URL."),
    bot_url: str = typer.Option("", "--bot-url", help="AppLink or deep link that opens the bot conversation."),
    setup_url: str = typer.Option("", "--setup-url", help="Platform setup or installation URL."),
    app_id: str = typer.Option("", "--app-id", help="Feishu app ID."),
    app_secret: str = typer.Option("", "--app-secret", help="Feishu app secret stored securely."),
    client_id: str = typer.Option("", "--client-id", help="DingTalk Stream client ID."),
    client_secret: str = typer.Option("", "--client-secret", help="DingTalk Stream client secret stored securely."),
    credential_store: str = typer.Option("auto", "--credential-store", help="auto | keychain | file"),
    allow: list[str] | None = typer.Option(None, "--allow", help="Pre-authorized chat ID, open ID, or conversation key; repeatable."),
    no_wait: bool = typer.Option(False, "--no-wait", help="Write config and pairing code without waiting for login."),
    no_qr: bool = typer.Option(False, "--no-qr", help="Print links and codes without rendering a QR code."),
    start: bool = typer.Option(False, "--start", help="Start or reuse omni serve after login."),
    timeout_s: int = typer.Option(120, "--timeout", help="Seconds to wait for scan or authorization."),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Fail on missing fields instead of prompting."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip risk confirmation."),
) -> None:
    """Authorize or pair a platform and write secure defaults."""
    if name not in _KNOWN or name == "cli":
        error("channel login supports: wechat, feishu, dingtalk")
        raise typer.Exit(1)
    paths = ctx.obj.settings().paths
    if name == "wechat":
        _login_wechat(
            paths,
            method=method,
            gateway_url=gateway_url,
            bot_url=bot_url,
            setup_url=setup_url,
            credential_store=credential_store,
            allow=allow,
            no_wait=no_wait,
            no_qr=no_qr,
            timeout_s=timeout_s,
            non_interactive=non_interactive,
            yes=yes,
        )
    elif name == "feishu":
        _login_feishu(
            paths,
            method=method,
            bot_url=bot_url,
            setup_url=setup_url,
            app_id=app_id,
            app_secret=app_secret,
            credential_store=credential_store,
            allow=allow,
            no_qr=no_qr,
            non_interactive=non_interactive,
        )
    elif name == "dingtalk":
        _login_dingtalk(
            paths,
            method=method,
            bot_url=bot_url,
            setup_url=setup_url,
            client_id=client_id,
            client_secret=client_secret,
            credential_store=credential_store,
            allow=allow,
            no_qr=no_qr,
            non_interactive=non_interactive,
        )
    if start:
        _start_channel_daemon(ctx, name)


@app.command("remove")
def remove_cmd(
    ctx: typer.Context,
    name: str,
    purge: bool = typer.Option(False, "--purge", help="Also delete the configuration file."),
) -> None:
    """Disable a channel and optionally delete its configuration."""
    paths = ctx.obj.settings().paths
    enabled = [n for n in _enabled(paths) if n != name]
    _write_enabled(paths, enabled)
    if purge:
        cfg = paths.channels_dir / f"{name}.toml"
        if cfg.is_file():
            cfg.unlink()
            info(f"Deleted {cfg}")
    success(f"Disabled channel '{name}'.")


@app.command("test")
def test_cmd(ctx: typer.Context, name: str) -> None:
    """Validate a channel configuration without connecting."""
    if name not in _KNOWN:
        error(f"Unknown channel '{name}'. Choose one of: {', '.join(_KNOWN)}")
        raise typer.Exit(1)
    if name == "cli":
        success("The CLI channel is always available.")
        return
    paths = ctx.obj.settings().paths
    cfg = paths.channels_dir / f"{name}.toml"
    if not cfg.is_file():
        error(f"Configuration not found: {cfg}. Run `omni channel login {name}` or `channel add`.")
        raise typer.Exit(1)
    data = load_channel_config(ctx.obj.settings(), name)
    console.print(f"[bold]{cfg}[/bold]")
    missing = [k for k in _required_fields(name, data) if not str(data.get(k, "")).strip()]
    if missing:
        warn(f"Required fields are empty: {', '.join(missing)}")
    else:
        success("Configuration fields are complete.")
    raw = _read_config(cfg)
    security = {**security_defaults(), **raw}
    if security.get("allowlist_enabled", True):
        info(f"Allowlist enabled with {len(security.get('allowed_external_keys') or [])} bound conversations.")
    else:
        warn("Allowlist disabled: any conversation reaching this channel may trigger the local agent.")
    if security.get("require_sensitive_confirm", True):
        info("Sensitive file and shell tools require local approval for IM channels by default.")
    _warn_missing_runtime_dependency(name, data)
    info("Live connections are established by omni serve.")


def _login_wechat(
    paths,  # noqa: ANN001
    *,
    method: str,
    gateway_url: str,
    bot_url: str,
    setup_url: str,
    credential_store: str,
    allow: list[str] | None,
    no_wait: bool,
    no_qr: bool,
    timeout_s: int,
    non_interactive: bool,
    yes: bool,
) -> None:
    method = "gateway" if method in {"auto", ""} else method
    if method == "ilink":
        _login_wechat_ilink(
            paths,
            credential_store=credential_store,
            allow=allow,
            no_wait=no_wait,
            no_qr=no_qr,
            timeout_s=timeout_s,
            non_interactive=non_interactive,
            yes=yes,
        )
        return
    if method not in {"gateway", "wecom"}:
        error("WeChat login --method supports ilink, gateway, or wecom.")
        raise typer.Exit(2)
    if method == "gateway" and not yes:
        msg = "A personal WeChat gateway may violate platform terms and risk account restrictions. Continue?"
        if non_interactive or not confirm(msg, default=False):
            error("Personal WeChat gateway login cancelled.")
            raise typer.Exit(1)
    cfg = with_security_defaults(dict(_WECHAT_GATEWAY_TEMPLATE))
    cfg["mode"] = method
    cfg["gateway_url"] = gateway_url.rstrip("/")
    if bot_url:
        cfg["bot_url"] = bot_url
    if setup_url:
        cfg["setup_url"] = setup_url
    _write_channel_config(paths, "wechat", cfg)
    _enable(paths, "wechat")
    _bind_allowed(paths, "wechat", allow)
    success(f"Wrote WeChat channel configuration to {paths.channels_dir / 'wechat.toml'}")

    bound_key = None if no_wait or method != "gateway" else _try_wechat_gateway_login(cfg, timeout_s)
    if bound_key:
        add_allowed_external_key(paths.channels_dir / "wechat.toml", bound_key)
        success(f"Bound WeChat conversation: {bound_key}")
        return
    code = create_pairing_code(paths.channels_dir / "wechat.toml")
    warn("The gateway did not return a WeChat conversation key for automatic binding.")
    _show_pairing_qr(
        "wechat",
        code,
        bot_url=bot_url or setup_url,
        no_qr=no_qr,
        fallback_hint="Send this to the agent in WeChat",
    )


def _login_wechat_ilink(
    paths,  # noqa: ANN001
    *,
    credential_store: str,
    allow: list[str] | None,
    no_wait: bool,
    no_qr: bool,
    timeout_s: int,
    non_interactive: bool,
    yes: bool,
) -> None:
    """Log in to WeChat via Tencent's iLink bot connector."""
    import asyncio

    from omni.channels.weixin_ilink import (
        DEFAULT_BASE_URL,
        DEFAULT_BOT_TYPE,
        LoginResult,
        WeixinIlinkClient,
        WeixinIlinkError,
    )

    cfg_path = paths.channels_dir / "wechat.toml"
    effective = _load_effective_channel_config(paths, "wechat")

    if no_wait:
        cfg = with_security_defaults(dict(_BASE_TEMPLATES["wechat"]))
        cfg.update({"mode": "ilink", "base_url": DEFAULT_BASE_URL, "bot_type": DEFAULT_BOT_TYPE})
        _write_channel_config(paths, "wechat", cfg)
        _enable(paths, "wechat")
        _bind_allowed(paths, "wechat", allow)
        success("Wrote the WeChat iLink template without scanning.")
        info("Run `omni channel login wechat --method ilink` to scan and connect.")
        return

    if not yes:
        msg = (
            "This experimental client connects local OmniScientist through Tencent's iLink bot backend. "
            "It is not endorsed or supported by Tencent or OpenClaw and may be affected by platform terms, "
            "rate limits, or account restrictions. Use a managed enterprise path in production. Continue?"
        )
        if non_interactive or not confirm(msg, default=True):
            error("WeChat iLink login cancelled.")
            raise typer.Exit(1)

    client = WeixinIlinkClient.from_config(effective)

    def _show_qr(url: str) -> None:
        info("Scan this QR code with WeChat, then send a message in the opened bot conversation:")
        if not no_qr:
            render_terminal_qr(url)
        info(f"QR URL: {url}")

    async def _verify_code() -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: typer.prompt("Enter the verification code shown in WeChat")
        )

    verify_provider = None if non_interactive else _verify_code

    async def _run() -> LoginResult:
        qr = await client.get_bot_qrcode()
        _show_qr(qr.qrcode_url)
        return await client.wait_for_login(
            qr.qrcode,
            timeout_s=float(timeout_s),
            verify_code_provider=verify_provider,
            on_refresh=_show_qr,
        )

    try:
        result = asyncio.run(_run())
    except WeixinIlinkError as exc:
        error(f"WeChat iLink login failed: {exc}")
        raise typer.Exit(1) from exc
    except httpx.HTTPError as exc:
        error(f"Could not connect to the WeChat iLink service ({DEFAULT_BASE_URL}): {exc}")
        raise typer.Exit(1) from exc

    if not (result.connected or result.already_connected):
        error(result.message or "WeChat iLink login did not complete.")
        raise typer.Exit(1)

    cfg = with_security_defaults(_read_config(cfg_path))
    cfg["mode"] = "ilink"
    cfg["base_url"] = result.base_url or str(effective.get("base_url") or DEFAULT_BASE_URL)
    cfg["bot_type"] = str(effective.get("bot_type") or DEFAULT_BOT_TYPE)
    if result.account_id:
        cfg["account_id"] = result.account_id
    if result.connected and result.bot_token:
        _store_secret(paths, "wechat", "bot_token", result.bot_token, credential_store, cfg)
    _write_channel_config(paths, "wechat", cfg)
    _enable(paths, "wechat")
    _bind_allowed(paths, "wechat", allow)
    if result.user_id:
        add_allowed_external_key(cfg_path, result.user_id)

    if result.already_connected:
        success("WeChat is already connected to this machine.")
        warn("No new bot token was returned; unbind the old device and retry if messaging still fails.")
    else:
        success("Connected local OmniScientist to WeChat.")
        if result.user_id:
            info(f"Automatically allowed WeChat account: {result.user_id}")
    info("Run `omni serve start` to keep the WeChat conversation online.")


def _login_feishu(
    paths,  # noqa: ANN001
    *,
    method: str,
    bot_url: str,
    setup_url: str,
    app_id: str,
    app_secret: str,
    credential_store: str,
    allow: list[str] | None,
    no_qr: bool,
    non_interactive: bool,
) -> None:
    if method == "auto":
        info("Feishu scan flow: open the bot chat with AppLink and send the one-time pairing code.")
    elif method != "manual":
        error("Feishu login supports only --method manual or auto.")
        raise typer.Exit(2)
    cfg_path = paths.channels_dir / "feishu.toml"
    raw = _read_config(cfg_path)
    effective = _load_effective_channel_config(paths, "feishu")
    app_id = app_id.strip() or str(raw.get("app_id") or effective.get("app_id") or "")
    if not app_id and not non_interactive:
        app_id = typer.prompt("Feishu App ID", default="")
    if not app_secret and not non_interactive:
        app_secret = prompt_secret("Feishu App Secret")
    has_secret = bool(app_secret or effective.get("app_secret"))
    if not app_id or not has_secret:
        error("Feishu login requires --app-id and --app-secret.")
        raise typer.Exit(2)
    cfg = {**_template("feishu"), **raw}
    cfg["app_id"] = app_id
    bot_url = bot_url.strip() or str(raw.get("bot_url") or "")
    setup_url = setup_url.strip() or str(raw.get("setup_url") or "")
    cfg["bot_url"] = bot_url or _feishu_bot_url(app_id)
    if setup_url:
        cfg["setup_url"] = setup_url
    if app_secret:
        _store_secret(paths, "feishu", "app_secret", app_secret, credential_store, cfg)
    _write_channel_config(paths, "feishu", cfg)
    _enable(paths, "feishu")
    _bind_allowed(paths, "feishu", allow)
    code = create_pairing_code(paths.channels_dir / "feishu.toml")
    success(f"Wrote Feishu channel configuration to {paths.channels_dir / 'feishu.toml'}")
    _show_pairing_qr("feishu", code, bot_url=str(cfg.get("bot_url") or ""), no_qr=no_qr)
    warn(
        "Ensure that the Feishu app subscribes to im.message.receive_v1. "
        "The channels extra includes lark-oapi."
    )


def _login_dingtalk(
    paths,  # noqa: ANN001
    *,
    method: str,
    bot_url: str,
    setup_url: str,
    client_id: str,
    client_secret: str,
    credential_store: str,
    allow: list[str] | None,
    no_qr: bool,
    non_interactive: bool,
) -> None:
    if method == "auto":
        info("DingTalk scan flow: open the bot or setup page and send the one-time pairing code.")
    elif method != "manual":
        error("DingTalk login supports only --method manual or auto.")
        raise typer.Exit(2)
    cfg_path = paths.channels_dir / "dingtalk.toml"
    raw = _read_config(cfg_path)
    effective = _load_effective_channel_config(paths, "dingtalk")
    client_id = client_id.strip() or str(raw.get("client_id") or effective.get("client_id") or "")
    if not client_id and not non_interactive:
        client_id = typer.prompt("DingTalk Client ID", default="")
    if not client_secret and not non_interactive:
        client_secret = prompt_secret("DingTalk Client Secret")
    has_secret = bool(client_secret or effective.get("client_secret"))
    if not client_id or not has_secret:
        error("DingTalk login requires --client-id and --client-secret.")
        raise typer.Exit(2)
    cfg = {**_template("dingtalk"), **raw}
    cfg["client_id"] = client_id
    bot_url = bot_url.strip() or str(raw.get("bot_url") or "")
    setup_url = setup_url.strip() or str(raw.get("setup_url") or "")
    if bot_url:
        cfg["bot_url"] = bot_url
    cfg["setup_url"] = setup_url or str(raw.get("setup_url") or _DINGTALK_SETUP_URL)
    if client_secret:
        _store_secret(paths, "dingtalk", "client_secret", client_secret, credential_store, cfg)
    _write_channel_config(paths, "dingtalk", cfg)
    _enable(paths, "dingtalk")
    _bind_allowed(paths, "dingtalk", allow)
    code = create_pairing_code(paths.channels_dir / "dingtalk.toml")
    success(f"Wrote DingTalk channel configuration to {paths.channels_dir / 'dingtalk.toml'}")
    _show_pairing_qr(
        "dingtalk",
        code,
        bot_url=str(cfg.get("bot_url") or ""),
        setup_url=str(cfg.get("setup_url") or ""),
        no_qr=no_qr,
    )
    warn(
        "Ensure that the DingTalk enterprise bot has Stream mode enabled. "
        "The channels extra includes dingtalk-stream."
    )


def _store_secret(
    paths,  # noqa: ANN001
    channel: str,
    key: str,
    value: str,
    credential_store: str,
    cfg: dict[str, Any],
) -> None:
    backend = (credential_store or "auto").strip().lower()
    try:
        ref = store_channel_secret(paths, channel, key, value, backend=backend)
    except CredentialStoreError as exc:
        # A freshly-scanned credential is expensive to obtain (the user just did
        # a QR flow). When an encrypted store *exists* but refuses the write at
        # runtime — e.g. a locked keychain / non-interactive (SSH) session:
        # "SecKeychainItemCreateFromContent: User interaction is not allowed." —
        # don't discard it: fall back to secrets.toml (0600) with a loud warning.
        # When no encrypted store exists at all, keep the deliberate opt-in.
        wants_keychain = backend in {"auto", "keychain", "macos-keychain"}
        if wants_keychain and keychain_available():
            warn(f"Could not write to the system keychain: {exc}")
            try:
                store_channel_secret(paths, channel, key, value, backend="file")
            except CredentialStoreError as exc2:
                error(str(exc2))
                raise typer.Exit(2) from exc2
            _drop_credential_ref(cfg, key)
            warn(
                f"Fell back to storing {channel}.{key} in {paths.secrets_file} "
                "with mode 0600 instead of the system keychain."
            )
            info(
                "To use the keychain, first run "
                "`security unlock-keychain ~/Library/Keychains/login.keychain-db`, "
                f"then rerun `omni channel login {channel}`."
            )
            return
        if backend == "auto" and not keychain_available():
            # Windows / Linux have no built-in OS keychain here. Don't dump a bare
            # English exception — tell the user the exact, secure way to proceed.
            error(f"No supported encrypted credential store is available; {channel}.{key} was not saved.")
            info(
                f"Log in again with --credential-store file to write the credential to {paths.secrets_file}. "
                "The file uses mode 0600 on Linux and macOS; on Windows, restrict it to the current user."
            )
            info(f"Example: omni channel login {channel} --credential-store file --start")
            raise typer.Exit(2) from exc
        error(str(exc))
        raise typer.Exit(2) from exc
    if ref:
        cfg.setdefault("credential_refs", {})[key] = ref
        success(f"Stored {channel}.{key} in the encrypted system credential store.")
    else:
        warn(f"Stored {channel}.{key} in secrets.toml with mode 0600 as explicitly requested.")


def _drop_credential_ref(cfg: dict[str, Any], key: str) -> None:
    """Remove a stale keychain ref for ``key`` so the written config stays clean."""
    refs = cfg.get("credential_refs")
    if isinstance(refs, dict):
        refs.pop(key, None)
        if not refs:
            cfg.pop("credential_refs", None)


def _feishu_bot_url(app_id: str) -> str:
    return f"https://applink.feishu.cn/client/bot/open?appId={app_id}"


def _show_pairing_qr(
    channel: str,
    code: str,
    *,
    bot_url: str = "",
    setup_url: str = "",
    no_qr: bool,
    fallback_hint: str = "Send after opening the bot chat",
) -> None:
    command = f"/pair {code}"
    target = (bot_url or setup_url).strip()
    if target:
        if bot_url:
            info(f"Scan to open the {channel} bot chat: {target}")
        else:
            info(f"Scan to open the {channel} setup page: {target}")
        if not no_qr:
            render_terminal_qr(target)
        info(f"{fallback_hint}: {command}")
        return
    warn(f"No bot_url is configured for {channel}; showing only the pairing code.")
    if not no_qr:
        render_terminal_qr(command)
    info(f"{fallback_hint}: {command}")


def _start_channel_daemon(ctx: typer.Context, channel: str) -> None:
    from omni.channels.manager import request_reload
    from omni.runtime import service_control

    settings = ctx.obj.settings()
    paths = settings.paths
    data = _load_effective_channel_config(paths, channel)
    _warn_missing_runtime_dependency(channel, data)
    # The running service caches each channel's client/token at build time, so a
    # fresh login (new bot_token) must be pushed in. Nudge a home-level hot-reload
    # rather than restarting the whole service: only this channel is rebuilt, so
    # other already logged-in channels keep their live sessions and in-flight
    # turns. The sentinel is home-level, so it also reaches the channel's real
    # owner even if it runs from a different workspace.
    request_reload(paths.channels_dir)

    # `login --start` asks to keep this channel online, which depends on the
    # always-on home service — the one process per OMNI_HOME that owns messaging
    # channels for every workspace. In the always-on model it is normally already
    # up (a bare `omni` brings it up, and a transient `omni serve stop` is undone
    # on the next launch); this ensures it is running now so `--start` is never a
    # no-op even from a script with no prior interactive launch.
    result = service_control.lazy_enable(settings, reason=f"channel:{channel}")
    (success if result.ok else warn)(result.detail)
    if result.ok:
        info(
            f"The {channel} channel joins the always-on home service, which manages channels "
            "dynamically from configuration; new logins apply within seconds."
        )
    else:
        warn(
            "Run `omni serve start` to bring the home service online so this channel stays "
            "connected in the background."
        )


def _try_wechat_gateway_login(cfg: dict[str, Any], timeout_s: int) -> str | None:
    base_url = str(cfg.get("gateway_url") or cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        return None
    try:
        qr_data = _get_json(base_url + str(cfg.get("login_qr_path") or "/login/qrcode"))
    except httpx.HTTPError as exc:
        warn(f"Could not reach the WeChat gateway QR endpoint: {exc}")
        return None
    payload = _extract_first(qr_data, ("qr_url", "qrcode_url", "url", "qr", "qrcode", "data"))
    if payload:
        info("Scan the QR code in WeChat to complete login:")
        render_terminal_qr(payload)
    else:
        warn("The WeChat gateway did not return a renderable QR payload.")
    status_path = str(cfg.get("login_status_path") or "/login/status")
    deadline = time.time() + max(1, timeout_s)
    while time.time() < deadline:
        try:
            status_data = _get_json(base_url + status_path)
        except httpx.HTTPError:
            time.sleep(2)
            continue
        external_key = _extract_first(
            status_data,
            ("external_key", "openid", "wxid", "user_id", "account_id", "chat_id"),
        )
        status = _extract_first(status_data, ("status", "state", "login_status")).lower()
        if external_key and status in {"", "ok", "success", "confirmed", "logged_in", "online"}:
            return external_key
        if status in {"failed", "expired", "cancelled", "canceled"}:
            warn(f"WeChat QR login status: {status}")
            return None
        time.sleep(2)
    warn("Timed out while waiting for WeChat QR login.")
    return None


def _get_json(url: str) -> dict[str, Any]:
    res = httpx.get(url, timeout=8.0)
    res.raise_for_status()
    data = res.json()
    return data if isinstance(data, dict) else {"data": data}


def _extract_first(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = data.get("data")
    if isinstance(nested, dict):
        return _extract_first(nested, keys)
    return ""


def _warn_missing_runtime_dependency(name: str, data: dict[str, Any]) -> None:
    mode = str(data.get("mode") or "")
    if name == "feishu" and mode != "gateway" and importlib.util.find_spec("lark_oapi") is None:
        warn("Missing runtime dependency: Feishu long connections require lark-oapi. Reinstall `omniscientist[channels]`.")
    if name == "dingtalk" and mode != "gateway" and importlib.util.find_spec("dingtalk_stream") is None:
        warn("Missing runtime dependency: DingTalk Stream mode requires dingtalk-stream. Reinstall `omniscientist[channels]`.")
