"""Secret-safe Web configuration and lifecycle control for IM channels."""

from __future__ import annotations

import asyncio
import os
import stat
import time
import tomllib
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from omni.config.paths import get_paths

pytest.importorskip("starlette")

from omni.channels.weixin_ilink import QrCode  # noqa: E402
from omni.web.app import create_app  # noqa: E402


async def _rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    response = await client.post(
        "/api",
        headers={"X-Omni-Web": "1"},
        json={"method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _wait_login(
    client: httpx.AsyncClient,
    login_id: str,
    *states: str,
    timeout: float = 2.0,
) -> dict:
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        last = await _rpc(client, "channel.wechat.status", {"login_id": login_id})
        if last.get("state") in states or last.get("ok") is False:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {states}: {last}")


@pytest.fixture(autouse=True)
def _fast_wechat_poll(monkeypatch):
    from omni.web import channels as web_channels

    monkeypatch.setattr(web_channels, "_POLL_SLEEP_SECONDS", 0.01)
    # Shipping TTL is 5 minutes. Leftover waiting pollers then parked Linux
    # 3.11 at 97% while pytest-asyncio waited for each background task.
    monkeypatch.setattr(web_channels, "_LOGIN_TTL_SECONDS", 3)


@pytest.fixture
async def app_client():
    from omni.web.workspace import close_workspace_hub

    app = create_app(cors_origins=[], trusted_hosts=["omni.test"])
    try:
        yield app, httpx.ASGITransport(app=app)
    finally:
        registry = getattr(app.state, "wechat_login_registry", None)
        leftover = []
        if registry is not None:
            for attempt in list(registry._attempts.values()):
                task = attempt.task
                if task is not None and not task.done():
                    task.cancel()
                    leftover.append(task)
        leftover.extend(
            task
            for task in asyncio.all_tasks()
            if task.get_name().startswith(("wechat-login:", "web-turn-"))
            and not task.done()
        )
        if leftover:
            await asyncio.gather(*leftover, return_exceptions=True)
        await close_workspace_hub(app.state.hub, timeout=1)


@pytest.fixture
def inert_service(monkeypatch):
    from omni.web import channels as web_channels

    class Result:
        ok = True

    monkeypatch.setattr(web_channels, "lazy_enable", lambda *_args, **_kwargs: Result())
    monkeypatch.setattr(web_channels, "request_reload", lambda _path: None)


@pytest.mark.asyncio
async def test_describe_is_home_scoped_and_never_returns_credentials(app_client) -> None:
    paths = get_paths()
    paths.channels_dir.mkdir(parents=True)
    paths.channels_dir.joinpath("feishu.toml").write_text(
        'mode = "ws"\napp_id = "cli_123"\nallowed_external_keys = ["chat-secret"]\n',
        encoding="utf-8",
    )
    paths.secrets_file.write_text(
        '[channels.feishu]\napp_secret = "do-not-return"\n', encoding="utf-8"
    )
    app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        result = await _rpc(client, "channel.describe")

    assert result["ok"] is True
    assert {row["name"] for row in result["channels"]} == {"wechat", "feishu", "dingtalk"}
    feishu = next(row for row in result["channels"] if row["name"] == "feishu")
    assert feishu["public_id"] == "cli_123"
    assert feishu["secret_set"] is True
    assert feishu["allowed_count"] == 1
    assert result["service"]["phase"] in {
        "down",
        "starting",
        "ready",
        "stopping",
        "unhealthy",
        "stale",
    }
    serialized = str(result)
    assert "do-not-return" not in serialized
    assert "chat-secret" not in serialized
    assert "pairing_code_hash" not in serialized
    assert not app.state.hub._agents


@pytest.mark.asyncio
async def test_configure_feishu_persists_public_and_private_fields_separately(
    app_client, inert_service
) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        result = await _rpc(
            client,
            "channel.configure",
            {"channel": "feishu", "public_id": "cli_web", "secret": "top-secret"},
        )

    assert result["ok"] is True
    assert result["channel"]["configured"] is True
    assert result["channel"]["secret_set"] is True
    assert result["pairing"]["command"].startswith("/pair ")
    assert result["pairing"]["code"] not in str(
        tomllib.loads(get_paths().channels_dir.joinpath("feishu.toml").read_text())
    )
    paths = get_paths()
    public = tomllib.loads(paths.channels_dir.joinpath("feishu.toml").read_text())
    private = tomllib.loads(paths.secrets_file.read_text())
    assert public["app_id"] == "cli_web"
    assert "app_secret" not in public
    assert private["channels"]["feishu"]["app_secret"] == "top-secret"
    assert "top-secret" not in str(result)
    if os.name == "posix":
        assert stat.S_IMODE(paths.secrets_file.stat().st_mode) == 0o600
    enabled = tomllib.loads(paths.config_file.read_text())["channels"]["enabled"]
    assert "feishu" in enabled


@pytest.mark.asyncio
async def test_blank_secret_keeps_existing_secret_and_stale_keychain_ref_is_removed(
    app_client, inert_service
) -> None:
    paths = get_paths()
    paths.channels_dir.mkdir(parents=True)
    paths.channels_dir.joinpath("feishu.toml").write_text(
        'mode = "ws"\napp_id = "old"\n[credential_refs]\napp_secret = "macos-keychain:feishu:app_secret"\n',
        encoding="utf-8",
    )
    paths.secrets_file.write_text(
        '[channels.feishu]\napp_secret = "preserved"\n', encoding="utf-8"
    )
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        first = await _rpc(
            client,
            "channel.configure",
            {"channel": "feishu", "public_id": "new", "secret": "replacement"},
        )
        second = await _rpc(
            client,
            "channel.configure",
            {"channel": "feishu", "public_id": "newer", "secret": ""},
        )

    assert first["ok"] is True and second["ok"] is True
    assert tomllib.loads(paths.secrets_file.read_text())["channels"]["feishu"]["app_secret"] == "replacement"
    public = tomllib.loads(paths.channels_dir.joinpath("feishu.toml").read_text())
    assert public["app_id"] == "newer"
    assert "credential_refs" not in public


def test_missing_credential_reference_is_not_reported_as_a_saved_secret(monkeypatch) -> None:
    from omni.channels import provisioning

    paths = get_paths()
    paths.channels_dir.mkdir(parents=True)
    paths.channels_dir.joinpath("feishu.toml").write_text(
        'app_id = "cli_ref"\n[credential_refs]\napp_secret = "macos-keychain:feishu:app_secret"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(provisioning, "secret_ref_exists", lambda _ref: False)
    assert provisioning.secret_is_set(paths, "feishu", "app_secret") is False


def test_keychain_presence_check_never_requests_the_secret(monkeypatch) -> None:
    from omni.channels import credentials

    seen: list[str] = []

    def fake_run(args, **kwargs):  # noqa: ANN001
        seen.extend(args)
        assert kwargs["stdout"] is credentials.subprocess.DEVNULL
        assert kwargs["stderr"] is credentials.subprocess.DEVNULL
        assert kwargs["timeout"] == 2.0
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(credentials, "_has_macos_keychain", lambda: True)
    monkeypatch.setattr(credentials.subprocess, "run", fake_run)

    assert credentials.secret_ref_exists("macos-keychain:feishu:app_secret") is True
    assert "-w" not in seen
    assert "channel:feishu:app_secret" in seen


def test_runtime_projection_matches_channel_manager_start_and_lock_states() -> None:
    from omni.web.channels import _runtime_state

    assert _runtime_state(
        enabled=True,
        configured=True,
        service_phase="ready",
        runtime={"status": "running", "reason": "starting"},
    ) == ("starting", "Channel adapter is starting.")
    state, reason = _runtime_state(
        enabled=True,
        configured=True,
        service_phase="ready",
        runtime={
            "status": "degraded",
            "reason": "another omni serve owns this channel (pid 42); this process is task-only",
        },
    )
    assert state == "degraded"
    assert reason == "Another Omni service owns this channel."


@pytest.mark.asyncio
async def test_blank_secret_migrates_legacy_inline_secret_before_removing_it(
    app_client, inert_service
) -> None:
    paths = get_paths()
    paths.channels_dir.mkdir(parents=True)
    paths.channels_dir.joinpath("dingtalk.toml").write_text(
        'mode = "stream"\nclient_id = "old"\nclient_secret = "legacy-inline"\n',
        encoding="utf-8",
    )
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        described = await _rpc(client, "channel.describe")
        saved = await _rpc(
            client,
            "channel.configure",
            {"channel": "dingtalk", "public_id": "new", "secret": ""},
        )

    before = next(row for row in described["channels"] if row["name"] == "dingtalk")
    assert before["secret_set"] is True
    assert saved["ok"] is True
    public = tomllib.loads(paths.channels_dir.joinpath("dingtalk.toml").read_text())
    private = tomllib.loads(paths.secrets_file.read_text())
    assert "client_secret" not in public
    assert private["channels"]["dingtalk"]["client_secret"] == "legacy-inline"
    assert "legacy-inline" not in str((described, saved))


@pytest.mark.asyncio
async def test_invalid_channel_inputs_fail_before_writing(app_client, inert_service) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        unknown = await _rpc(
            client,
            "channel.configure",
            {"channel": "telegram", "public_id": "x", "secret": "y"},
        )
        control = await _rpc(
            client,
            "channel.configure",
            {"channel": "feishu", "public_id": "bad\nvalue", "secret": "secret"},
        )

    assert unknown["ok"] is False
    assert control["ok"] is False
    paths = get_paths()
    assert not paths.channels_dir.joinpath("feishu.toml").exists()
    assert not paths.secrets_file.exists()


@pytest.mark.asyncio
async def test_malformed_owner_config_fails_before_secret_write(app_client, inert_service) -> None:
    paths = get_paths()
    paths.config_file.write_text("[channels\nenabled = [", encoding="utf-8")
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        result = await _rpc(
            client,
            "channel.configure",
            {"channel": "feishu", "public_id": "cli_safe", "secret": "must-not-write"},
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "channel_config_error"
    assert not paths.secrets_file.exists()
    assert not paths.channels_dir.joinpath("feishu.toml").exists()


@pytest.mark.asyncio
async def test_enable_disable_reconnect_and_pair_preserve_configuration(
    app_client, inert_service
) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(
            client,
            "channel.configure",
            {"channel": "dingtalk", "public_id": "ding-id", "secret": "ding-secret"},
        )
        disabled = await _rpc(client, "channel.disable", {"channel": "dingtalk"})
        enabled = await _rpc(client, "channel.enable", {"channel": "dingtalk"})
        reconnected = await _rpc(client, "channel.reconnect", {"channel": "dingtalk"})
        paired = await _rpc(client, "channel.pair", {"channel": "dingtalk"})

    assert disabled["channel"]["enabled"] is False
    assert enabled["channel"]["enabled"] is True
    assert reconnected["channel"]["configured"] is True
    assert paired["pairing"]["command"] == f"/pair {paired['pairing']['code']}"
    raw = get_paths().channels_dir.joinpath("dingtalk.toml").read_text()
    assert paired["pairing"]["code"] not in raw
    assert "ding-id" in raw


@pytest.mark.asyncio
async def test_enabled_channel_is_disconnected_when_home_service_is_down(
    app_client, inert_service
) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        await _rpc(
            client,
            "channel.configure",
            {"channel": "feishu", "public_id": "cli_status", "secret": "secret"},
        )
        described = await _rpc(client, "channel.describe")
    feishu = next(row for row in described["channels"] if row["name"] == "feishu")
    assert feishu["runtime_state"] == "disconnected"
    assert "service" in feishu["runtime_reason"].lower()


@pytest.mark.asyncio
async def test_wechat_login_flow_keeps_provider_tokens_server_side(
    app_client, inert_service, monkeypatch
) -> None:
    from omni.web import channels as web_channels

    class FakeWeixinClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        async def get_bot_qrcode(self, *, base=None):  # noqa: ANN001
            return QrCode("opaque-provider-token", "https://liteapp.weixin.qq.com/qr")

        async def poll_qr_status(self, _qrcode, *, verify_code="", base=None):  # noqa: ANN001
            if not verify_code:
                return {"status": "need_verifycode"}
            assert verify_code == "246810"
            return {
                "status": "confirmed",
                "bot_token": "wechat-bot-secret",
                "ilink_bot_id": "bot-account",
                "baseurl": "https://ilinkai.weixin.qq.com",
                "ilink_user_id": "wx-user-secret",
            }

    monkeypatch.setattr(web_channels, "WeixinIlinkClient", FakeWeixinClient)
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        started = await _rpc(client, "channel.wechat.start")
        waiting = await _wait_login(client, started["login_id"], "verification_required")
        submitted = await _rpc(
            client,
            "channel.wechat.verify",
            {"login_id": started["login_id"], "code": "246810"},
        )
        connected = await _wait_login(client, started["login_id"], "succeeded")
        again = await _rpc(client, "channel.wechat.status", {"login_id": started["login_id"]})
        described = await _rpc(client, "channel.describe")

    assert started["state"] == "waiting"
    assert started["qr_matrix"] and isinstance(started["qr_matrix"][0], list)
    assert waiting["state"] == "verification_required"
    assert submitted["login_id"] == started["login_id"]
    assert connected["state"] == "succeeded"
    assert connected.get("service_ready") is True
    assert connected.get("allowed_count") == 1
    assert "qr_matrix" not in connected
    assert again["state"] == "succeeded"
    assert "qr_matrix" not in again
    assert described["wechat_login"]["state"] == "succeeded"
    for payload in (started, waiting, connected, again, described):
        text = str(payload)
        assert "opaque-provider-token" not in text
        assert "wechat-bot-secret" not in text
        assert "wx-user-secret" not in text
    paths = get_paths()
    assert tomllib.loads(paths.secrets_file.read_text())["channels"]["wechat"]["bot_token"] == "wechat-bot-secret"
    public = tomllib.loads(paths.channels_dir.joinpath("wechat.toml").read_text())
    assert "wx-user-secret" in public["allowed_external_keys"]
    assert "bot_token" not in public


@pytest.mark.asyncio
async def test_wechat_expiry_refresh_cancel_and_unknown_login_are_safe(
    app_client, inert_service, monkeypatch
) -> None:
    from omni.web import channels as web_channels

    class RefreshingClient:
        count = 0

        def __init__(self) -> None:
            self.expired_once = False

        @classmethod
        def from_config(cls, _config):
            return cls()

        async def get_bot_qrcode(self, *, base=None):  # noqa: ANN001
            self.count += 1
            return QrCode(f"hidden-{self.count}", f"https://liteapp.weixin.qq.com/qr/{self.count}")

        async def poll_qr_status(self, _qrcode, *, verify_code="", base=None):  # noqa: ANN001
            if self.expired_once:
                return {"status": "wait"}
            self.expired_once = True
            return {"status": "expired"}

    monkeypatch.setattr(web_channels, "WeixinIlinkClient", RefreshingClient)
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        started = await _rpc(client, "channel.wechat.start")
        deadline = time.monotonic() + 2.0
        refreshed = started
        while time.monotonic() < deadline:
            refreshed = await _rpc(
                client, "channel.wechat.status", {"login_id": started["login_id"]}
            )
            if refreshed.get("qr_matrix") and refreshed["qr_matrix"] != started["qr_matrix"]:
                break
            await asyncio.sleep(0.02)
        cancelled = await _rpc(
            client, "channel.wechat.cancel", {"login_id": started["login_id"]}
        )
        missing = await _rpc(
            client, "channel.wechat.status", {"login_id": started["login_id"]}
        )

    assert refreshed["state"] == "waiting"
    assert refreshed["qr_matrix"] != started["qr_matrix"]
    assert cancelled["state"] == "cancelled"
    assert missing["ok"] is False
    assert missing["error"]["code"] == "login_not_found"
    assert "hidden-" not in str((started, refreshed, cancelled, missing))
    assert datetime.fromisoformat(started["expires_at"].replace("Z", "+00:00")).tzinfo == UTC


@pytest.mark.asyncio
async def test_wechat_qr_refresh_is_bounded(app_client, inert_service, monkeypatch) -> None:
    from omni.web import channels as web_channels

    class ExpiringClient:
        count = 0

        @classmethod
        def from_config(cls, _config):
            return cls()

        async def get_bot_qrcode(self, *, base=None):  # noqa: ANN001
            self.count += 1
            return QrCode(
                f"hidden-{self.count}",
                f"https://liteapp.weixin.qq.com/qr/{self.count}",
            )

        async def poll_qr_status(self, _qrcode, *, verify_code="", base=None):  # noqa: ANN001
            return {"status": "expired"}

    monkeypatch.setattr(web_channels, "WeixinIlinkClient", ExpiringClient)
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        started = await _rpc(client, "channel.wechat.start")
        login_id = started["login_id"]
        expired = await _wait_login(client, login_id, "expired")
        retained = await _rpc(client, "channel.wechat.status", {"login_id": login_id})

    assert expired["state"] == "expired"
    assert "qr_matrix" not in expired
    assert retained["state"] == "expired"


@pytest.mark.asyncio
async def test_starting_a_new_wechat_flow_invalidates_the_previous_one(
    app_client, inert_service, monkeypatch
) -> None:
    from omni.web import channels as web_channels

    class WaitingClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        async def get_bot_qrcode(self, *, base=None):  # noqa: ANN001
            return QrCode("hidden", "https://liteapp.weixin.qq.com/qr")

        async def poll_qr_status(self, _qrcode, *, verify_code="", base=None):  # noqa: ANN001
            return {"status": "wait"}

    monkeypatch.setattr(web_channels, "WeixinIlinkClient", WaitingClient)
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        first = await _rpc(client, "channel.wechat.start")
        second = await _rpc(client, "channel.wechat.start")
        stale = await _rpc(
            client, "channel.wechat.status", {"login_id": first["login_id"]}
        )
        current = await _rpc(
            client, "channel.wechat.status", {"login_id": second["login_id"]}
        )

    assert stale["ok"] is False
    assert stale["error"]["code"] == "login_not_found"
    assert current["state"] == "waiting"


@pytest.mark.asyncio
async def test_wechat_rejects_non_official_qr_endpoint(app_client, inert_service, monkeypatch) -> None:
    from omni.web import channels as web_channels

    class UntrustedClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        async def get_bot_qrcode(self, *, base=None):  # noqa: ANN001
            return QrCode("opaque", "https://attacker.example/collect")

    monkeypatch.setattr(web_channels, "WeixinIlinkClient", UntrustedClient)
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        result = await _rpc(client, "channel.wechat.start")

    assert result["ok"] is False
    assert result["error"]["code"] == "channel_unavailable"
    assert "attacker.example" not in str(result)
    assert "opaque" not in str(result)


@pytest.mark.asyncio
async def test_cancelled_wechat_poll_cannot_commit_late_credentials(
    app_client, inert_service, monkeypatch
) -> None:
    from omni.web import channels as web_channels

    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowConfirmedClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        async def get_bot_qrcode(self, *, base=None):  # noqa: ANN001
            return QrCode("hidden", "https://liteapp.weixin.qq.com/qr")

        async def poll_qr_status(self, _qrcode, *, verify_code="", base=None):  # noqa: ANN001
            entered.set()
            await release.wait()
            return {
                "status": "confirmed",
                "bot_token": "must-not-be-saved",
                "ilink_bot_id": "late-bot",
                "baseurl": "https://ilinkai.weixin.qq.com",
            }

    monkeypatch.setattr(web_channels, "WeixinIlinkClient", SlowConfirmedClient)
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        started = await _rpc(client, "channel.wechat.start")
        await entered.wait()
        cancelled = await _rpc(
            client, "channel.wechat.cancel", {"login_id": started["login_id"]}
        )
        release.set()
        await asyncio.sleep(0.05)
        late = await _rpc(client, "channel.wechat.status", {"login_id": started["login_id"]})

    assert cancelled["state"] == "cancelled"
    assert late["ok"] is False
    assert late["error"]["code"] == "login_not_found"
    assert not get_paths().secrets_file.exists()


@pytest.mark.asyncio
async def test_leftover_wechat_bridge_is_not_configured_without_bot_token(app_client) -> None:
    paths = get_paths()
    paths.channels_dir.mkdir(parents=True)
    paths.channels_dir.joinpath("wechat.toml").write_text(
        'mode = "gateway"\ngateway_url = "http://127.0.0.1:8088"\n'
        'inbox_path = "/messages"\nsend_path = "/send"\n',
        encoding="utf-8",
    )
    app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        result = await _rpc(client, "channel.describe")

    wechat = next(row for row in result["channels"] if row["name"] == "wechat")
    assert wechat["configured"] is False
    assert wechat["runtime_state"] == "not_configured"


@pytest.mark.asyncio
async def test_wechat_is_configured_from_bot_token_alone(app_client) -> None:
    paths = get_paths()
    paths.channels_dir.mkdir(parents=True)
    paths.channels_dir.joinpath("wechat.toml").write_text('mode = "ilink"\n', encoding="utf-8")
    paths.secrets_file.write_text('[channels.wechat]\nbot_token = "tok-only"\n', encoding="utf-8")
    app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        result = await _rpc(client, "channel.describe")

    wechat = next(row for row in result["channels"] if row["name"] == "wechat")
    assert wechat["configured"] is True
    assert wechat["secret_set"] is True
    assert wechat["public_id"] == ""


@pytest.mark.asyncio
async def test_orphan_wechat_token_without_config_file_is_not_configured(app_client) -> None:
    paths = get_paths()
    paths.secrets_file.write_text('[channels.wechat]\nbot_token = "orphan-token"\n', encoding="utf-8")
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        result = await _rpc(client, "channel.describe")
        enabled = await _rpc(client, "channel.enable", {"channel": "wechat"})

    wechat = next(row for row in result["channels"] if row["name"] == "wechat")
    assert wechat["configured"] is False
    assert wechat["secret_set"] is True
    assert wechat["runtime_state"] == "not_configured"
    assert enabled["ok"] is False
    assert "not configured" in enabled["error"]["message"]


@pytest.mark.asyncio
async def test_leftover_gateway_mode_with_token_is_configured_as_ilink(app_client) -> None:
    paths = get_paths()
    paths.channels_dir.mkdir(parents=True)
    paths.channels_dir.joinpath("wechat.toml").write_text(
        'mode = "gateway"\ngateway_url = "http://127.0.0.1:8088"\n',
        encoding="utf-8",
    )
    paths.secrets_file.write_text('[channels.wechat]\nbot_token = "legacy-token"\n', encoding="utf-8")
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        result = await _rpc(client, "channel.describe")

    wechat = next(row for row in result["channels"] if row["name"] == "wechat")
    assert wechat["configured"] is True
    assert wechat["secret_set"] is True
    assert wechat["mode"] == "ilink"


@pytest.mark.asyncio
async def test_channel_writes_freeze_after_home_drift(app_client, inert_service) -> None:
    app, transport = app_client
    app.state.web_home = "/not/the/current/omni/home"
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        written = await _rpc(
            client,
            "channel.configure",
            {"channel": "feishu", "public_id": "cli_drift", "secret": "must-not-save"},
        )
        described = await _rpc(client, "channel.describe")
        cancelled = await _rpc(client, "channel.wechat.cancel", {"login_id": "stale-login"})

    assert written["ok"] is False
    assert written["error"]["code"] == "restart_required"
    assert described["ok"] is True
    assert described["restart_required"] is True
    assert "Restart this omni web process" in described["notice"]
    assert cancelled["ok"] is True
    assert cancelled["state"] == "cancelled"
    paths = get_paths()
    assert not paths.secrets_file.exists() or "must-not-save" not in paths.secrets_file.read_text()


@pytest.mark.asyncio
async def test_wechat_confirm_does_not_write_after_attempt_home_drifts(
    app_client, inert_service, monkeypatch
) -> None:
    from omni.web import channels as web_channels

    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowConfirmedClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        async def get_bot_qrcode(self, *, base=None):  # noqa: ANN001
            return QrCode("hidden", "https://liteapp.weixin.qq.com/qr")

        async def poll_qr_status(self, _qrcode, *, verify_code="", base=None):  # noqa: ANN001
            entered.set()
            await release.wait()
            return {
                "status": "confirmed",
                "bot_token": "must-not-be-saved",
                "ilink_bot_id": "drifted-bot",
                "baseurl": "https://ilinkai.weixin.qq.com",
            }

    monkeypatch.setattr(web_channels, "WeixinIlinkClient", SlowConfirmedClient)
    app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        started = await _rpc(client, "channel.wechat.start")
        await entered.wait()
        attempt = app.state.wechat_login_registry.get(started["login_id"])
        attempt.omni_home = "/other/omni/home"
        release.set()
        late = await _wait_login(client, started["login_id"], "error")

    assert late["state"] == "error"
    assert "Restart this omni web process" in late["message"]
    assert not get_paths().secrets_file.exists()


@pytest.mark.asyncio
async def test_wechat_confirm_does_not_write_when_live_home_changes_during_poll(
    app_client, inert_service, monkeypatch
) -> None:
    from omni.web import channels as web_channels

    entered = asyncio.Event()
    release = asyncio.Event()
    original_home = web_channels.resolved_home

    class SlowConfirmedClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        async def get_bot_qrcode(self, *, base=None):  # noqa: ANN001
            return QrCode("hidden", "https://liteapp.weixin.qq.com/qr")

        async def poll_qr_status(self, _qrcode, *, verify_code="", base=None):  # noqa: ANN001
            entered.set()
            await release.wait()
            return {
                "status": "confirmed",
                "bot_token": "must-not-be-saved",
                "ilink_bot_id": "live-home-bot",
                "baseurl": "https://ilinkai.weixin.qq.com",
            }

    def live_elsewhere(settings=None):
        if settings is None:
            return "/other/omni/home"
        return original_home(settings)

    monkeypatch.setattr(web_channels, "WeixinIlinkClient", SlowConfirmedClient)
    app, transport = app_client
    started_reload: list[str] = []

    async def no_start(*_args, **_kwargs):  # noqa: ANN002, ANN003
        started_reload.append("started")

    monkeypatch.setattr(web_channels, "_reload_and_start", no_start)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        started = await _rpc(client, "channel.wechat.start")
        await entered.wait()
        monkeypatch.setattr(web_channels, "resolved_home", live_elsewhere)
        release.set()
        late = await _wait_login(client, started["login_id"], "error")

    assert late["state"] == "error"
    assert "Restart this omni web process" in late["message"]
    assert started_reload == []
    paths = get_paths()
    assert not paths.secrets_file.exists()
    assert not paths.channels_dir.joinpath("wechat.toml").exists()


@pytest.mark.asyncio
async def test_wechat_login_advances_waiting_scanned_confirmed(
    app_client, inert_service, monkeypatch
) -> None:
    from omni.web import channels as web_channels

    class SequenceClient:
        polls = 0

        @classmethod
        def from_config(cls, _config):
            return cls()

        async def get_bot_qrcode(self, *, base=None):  # noqa: ANN001
            return QrCode("hidden", "https://liteapp.weixin.qq.com/qr")

        async def poll_qr_status(self, _qrcode, *, verify_code="", base=None):  # noqa: ANN001
            self.polls += 1
            if self.polls == 1:
                return {"status": "wait"}
            if self.polls == 2:
                return {"status": "scaned"}
            return {
                "status": "confirmed",
                "bot_token": "wechat-bot-secret",
                "ilink_bot_id": "bot-account",
                "baseurl": "https://ilinkai.weixin.qq.com",
                "ilink_user_id": "wx-user-secret",
            }

    monkeypatch.setattr(web_channels, "WeixinIlinkClient", SequenceClient)
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        started = await _rpc(client, "channel.wechat.start")
        scanned = await _wait_login(client, started["login_id"], "scanned")
        connected = await _wait_login(client, started["login_id"], "succeeded")

    assert scanned["state"] == "scanned"
    assert scanned.get("qr_matrix")
    assert connected["state"] == "succeeded"
    assert "qr_matrix" not in connected


@pytest.mark.asyncio
async def test_wechat_login_requires_scanning_account_before_write(
    app_client, inert_service, monkeypatch
) -> None:
    from omni.web import channels as web_channels

    class IncompleteClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        async def get_bot_qrcode(self, *, base=None):  # noqa: ANN001
            return QrCode("hidden", "https://liteapp.weixin.qq.com/qr")

        async def poll_qr_status(self, _qrcode, *, verify_code="", base=None):  # noqa: ANN001
            return {
                "status": "confirmed",
                "bot_token": "wechat-bot-secret",
                "ilink_bot_id": "bot-account",
                "baseurl": "https://ilinkai.weixin.qq.com",
            }

    monkeypatch.setattr(web_channels, "WeixinIlinkClient", IncompleteClient)
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        started = await _rpc(client, "channel.wechat.start")
        failed = await _wait_login(client, started["login_id"], "error")

    assert failed["state"] == "error"
    assert "complete login" in failed["message"]
    paths = get_paths()
    assert not paths.secrets_file.exists()
    assert not paths.channels_dir.joinpath("wechat.toml").exists()


def test_wechat_auth_expiry_reason_is_preserved_for_the_ui() -> None:
    from omni.web.channels import _safe_runtime_reason

    assert _safe_runtime_reason("WeChat login expired; scan the QR code again.") == (
        "WeChat login expired; scan the QR code again."
    )
