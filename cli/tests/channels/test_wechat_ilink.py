"""Offline tests for the WeChat iLink bot connector (no network).

HTTP is stubbed with ``httpx.MockTransport`` (the iLink client builds its own
``httpx.AsyncClient`` per call, so we patch the factory to inject a transport).
Channel-runtime and login-persistence paths use a fake client.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omni.agent.orchestrator import TurnResult
from omni.channels import weixin_ilink as wi
from omni.channels.weixin_ilink import (
    WeixinIlinkClient,
    client_version_uint32,
    is_bot_message,
    media_items,
    message_text,
)

requires_crypto = pytest.mark.skipif(
    not wi.is_crypto_available(), reason="cryptography not installed"
)


# ── helpers ────────────────────────────────────────────────────────────────
def _install_mock_transport(monkeypatch, handler) -> None:  # noqa: ANN001
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


class _DummyAgent:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.turns: list[dict[str, Any]] = []

    async def ensure_session(self, **kwargs: Any) -> str:
        self.kwargs["ensure_session"] = kwargs
        return "sess-wechat"

    async def handle_turn(self, text: str, **kwargs: Any) -> TurnResult:
        self.turns.append({"text": text, **kwargs})
        return TurnResult(text="通道回答", session_id=kwargs["session_id"], submitted_subtask_ids=[])


class _FakeIlinkClient:
    """Mimics WeixinIlinkClient's runtime surface for channel tests."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.images: list[tuple[str, str, str | None]] = []
        self.files: list[tuple[str, str, str | None]] = []
        self.typing: list[tuple[str, str, int]] = []
        self.config_calls: list[tuple[str, str | None]] = []
        self.downloaded: list[dict[str, Any]] = []
        self.started = False
        self.stopped = False
        self.base_url = "https://idc.example.com"

    async def send_message(self, to_user_id: str, text: str, *, context_token: str | None = None) -> None:
        self.sent.append((to_user_id, text, context_token))

    async def send_image(self, to_user_id: str, path: str, *, context_token: str | None = None) -> None:
        self.images.append((to_user_id, path, context_token))

    async def send_file(
        self, to_user_id: str, path: str, *, file_name: str | None = None, context_token: str | None = None
    ) -> None:
        self.files.append((to_user_id, path, context_token))

    async def get_config(self, ilink_user_id: str, *, context_token: str | None = None) -> dict[str, Any]:
        self.config_calls.append((ilink_user_id, context_token))
        return {"ret": 0, "typing_ticket": "T"}

    async def send_typing(self, ilink_user_id: str, *, typing_ticket: str = "", status: int = 1) -> None:
        self.typing.append((ilink_user_id, typing_ticket, status))

    async def download_media_from_item(self, item: dict[str, Any], dest_dir: str) -> wi.InboundMedia | None:
        self.downloaded.append(item)
        itype = int(item.get("type") or 0)
        if itype == 2:
            return wi.InboundMedia(
                path=f"{dest_dir}/x.png", kind="image", mime="image/png", file_name="x.png"
            )
        return None

    async def get_updates(self, buf: str) -> dict[str, Any]:
        return {"ret": 0, "msgs": [], "get_updates_buf": buf}

    async def notify_start(self) -> None:
        self.started = True

    async def notify_stop(self) -> None:
        self.stopped = True


# ── pure helpers ───────────────────────────────────────────────────────────
def test_client_version_uint32_encodes_semver():
    assert client_version_uint32("2.4.6") == (2 << 16) | (4 << 8) | 6
    assert client_version_uint32("1") == (1 << 16)
    assert client_version_uint32("garbage") == 0


def test_message_text_extracts_text_quote_and_voice():
    assert message_text({"item_list": [{"type": 1, "text_item": {"text": "hello"}}]}) == "hello"
    quoted = message_text(
        {
            "item_list": [
                {
                    "type": 1,
                    "text_item": {"text": "reply"},
                    "ref_msg": {"title": "原文", "message_item": {"type": 1, "text_item": {"text": "q"}}},
                }
            ]
        }
    )
    assert quoted == "[Quoted: 原文 | q]\nreply"
    assert message_text({"item_list": [{"type": 3, "voice_item": {"text": "语音转写"}}]}) == "语音转写"
    assert message_text({"item_list": [{"type": 2, "image_item": {}}]}) == ""


def test_is_bot_message_flags_bot_type():
    assert is_bot_message({"message_type": 2}) is True
    assert is_bot_message({"message_type": 1}) is False
    assert is_bot_message({}) is False


def test_send_chunk_limit_is_conservative_for_reliable_delivery():
    # iLink rejects long text bodies well before 3500 chars; segments must be small.
    assert wi._SEND_CHUNK_LIMIT <= 2000
    chunks = wi._chunk_text("x" * 5000)
    assert chunks  # long reply is split into several FINISH bubbles (pseudo-stream)
    assert len(chunks) >= 3
    assert all(len(c) <= wi._SEND_CHUNK_LIMIT for c in chunks)


# ── real client over MockTransport ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_login_flow_returns_credentials_and_sends_expected_headers(monkeypatch):
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"method": request.method, "path": request.url.path, "headers": request.headers})
        path = request.url.path
        if path.endswith("/get_bot_qrcode"):
            assert request.url.params.get("bot_type") == "3"
            return httpx.Response(
                200,
                json={"qrcode": "QR123", "qrcode_img_content": "https://liteapp.weixin.qq.com/x?bot_type=3"},
            )
        if path.endswith("/get_qrcode_status"):
            return httpx.Response(
                200,
                json={
                    "status": "confirmed",
                    "bot_token": "tok-xyz",
                    "ilink_bot_id": "botacc@im.bot",
                    "baseurl": "https://idc.example.com",
                    "ilink_user_id": "useracc@im.wechat",
                },
            )
        return httpx.Response(404, json={})

    _install_mock_transport(monkeypatch, handler)
    client = WeixinIlinkClient()
    qr = await client.get_bot_qrcode()
    assert qr.qrcode == "QR123" and qr.qrcode_url.startswith("https://liteapp")
    result = await client.wait_for_login(qr.qrcode, timeout_s=5)

    assert result.connected is True
    assert result.bot_token == "tok-xyz"
    assert result.account_id == "botacc@im.bot"
    assert result.base_url == "https://idc.example.com"
    assert result.user_id == "useracc@im.wechat"

    qr_post = next(r for r in seen if r["path"].endswith("/get_bot_qrcode"))
    assert qr_post["headers"]["iLink-App-Id"] == "bot"
    assert qr_post["headers"]["AuthorizationType"] == "ilink_bot_token"
    assert qr_post["headers"]["X-WECHAT-UIN"]
    assert "Authorization" not in qr_post["headers"]  # no token before login


@pytest.mark.asyncio
async def test_send_message_echoes_context_token_with_base_info_and_auth(monkeypatch):
    bodies: list[dict[str, Any]] = []
    headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers)
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ret": 0})

    _install_mock_transport(monkeypatch, handler)
    client = WeixinIlinkClient(base_url="https://idc.example.com", token="tok-abc")
    await client.send_message("peer@im.wechat", "你好，世界", context_token="ctx-1")

    assert len(bodies) == 1
    msg = bodies[0]["msg"]
    assert msg["to_user_id"] == "peer@im.wechat"
    assert msg["context_token"] == "ctx-1"
    assert msg["message_type"] == 2
    assert msg["item_list"][0]["text_item"]["text"] == "你好，世界"
    assert bodies[0]["base_info"]["bot_agent"].startswith("OmniScientist/")
    assert headers[0]["Authorization"] == "Bearer tok-abc"


@pytest.mark.asyncio
async def test_get_updates_returns_messages_and_advances_cursor(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "base_info" in body
        return httpx.Response(
            200,
            json={
                "ret": 0,
                "msgs": [{"message_id": 1, "from_user_id": "u@im.wechat", "message_type": 1}],
                "get_updates_buf": "cursor-2",
            },
        )

    _install_mock_transport(monkeypatch, handler)
    client = WeixinIlinkClient(base_url="https://idc.example.com", token="tok")
    resp = await client.get_updates("cursor-1")
    assert resp["get_updates_buf"] == "cursor-2"
    assert resp["msgs"][0]["from_user_id"] == "u@im.wechat"


# ── phase 2: media crypto (pure) ───────────────────────────────────────────
def test_aes_ecb_padded_size_always_pads_a_full_block():
    assert wi.aes_ecb_padded_size(0) == 16
    assert wi.aes_ecb_padded_size(1) == 16
    assert wi.aes_ecb_padded_size(15) == 16
    assert wi.aes_ecb_padded_size(16) == 32  # PKCS7 adds a full block at the boundary
    assert wi.aes_ecb_padded_size(17) == 32


@requires_crypto
def test_encrypt_decrypt_aes_ecb_round_trips():
    key = bytes(range(16))
    for payload in (b"", b"hi", b"\x00" * 16, b"weixin media \xf0\x9f\x98\x80" * 50):
        ciphertext = wi.encrypt_aes_ecb(payload, key)
        assert len(ciphertext) == wi.aes_ecb_padded_size(len(payload))
        assert wi.decrypt_aes_ecb(ciphertext, key) == payload


def test_parse_aes_key_accepts_raw16_and_base64_hex():
    raw = bytes(range(16))
    assert wi.parse_aes_key(base64.b64encode(raw).decode()) == raw
    # base64(hex string of 16 bytes) -> 32 ASCII hex chars
    assert wi.parse_aes_key(base64.b64encode(raw.hex().encode()).decode()) == raw
    with pytest.raises(wi.WeixinIlinkError):
        wi.parse_aes_key(base64.b64encode(b"too-short").decode())


def test_media_items_filters_image_file_video():
    msg = {
        "item_list": [
            {"type": 1, "text_item": {"text": "hi"}},
            {"type": 2, "image_item": {}},
            {"type": 4, "file_item": {}},
            {"type": 5, "video_item": {}},
            {"type": 3, "voice_item": {}},
        ]
    }
    assert [int(i["type"]) for i in media_items(msg)] == [2, 4, 5]


# ── phase 2: media over MockTransport ───────────────────────────────────────
@requires_crypto
@pytest.mark.asyncio
async def test_upload_media_and_send_image_builds_cdn_item(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path.endswith("/getuploadurl"):
            captured["upload_req"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"upload_full_url": "https://novac2c.cdn.weixin.qq.com/c2c/upload?x=1"},
            )
        if host.startswith("novac2c") and path.endswith("/upload"):
            captured["ciphertext"] = request.content
            captured["upload_content_type"] = request.headers.get("content-type")
            return httpx.Response(200, headers={"x-encrypted-param": "DLPARAM"})
        if path.endswith("/sendmessage"):
            captured["send_req"] = json.loads(request.content)
            return httpx.Response(200, json={"ret": 0})
        return httpx.Response(404, json={})

    _install_mock_transport(monkeypatch, handler)
    original = b"\x89PNG\r\n" + b"fake image payload " * 10
    img = tmp_path / "plot.png"
    img.write_bytes(original)

    client = WeixinIlinkClient(base_url="https://idc.example.com", token="tok")
    await client.send_image("peer@im.wechat", str(img), context_token="ctx-7")

    up = captured["upload_req"]
    assert up["media_type"] == wi.UPLOAD_MEDIA_IMAGE
    assert up["to_user_id"] == "peer@im.wechat"
    assert up["rawsize"] == len(original)
    assert up["filesize"] == wi.aes_ecb_padded_size(len(original))
    assert len(up["aeskey"]) == 32 and "base_info" in up
    assert captured["upload_content_type"] == "application/octet-stream"

    # The bytes PUT to the CDN are AES-128-ECB(plaintext) under the advertised key.
    aeskey = bytes.fromhex(up["aeskey"])
    assert wi.decrypt_aes_ecb(captured["ciphertext"], aeskey) == original

    item = captured["send_req"]["msg"]["item_list"][0]
    assert item["type"] == 2
    media = item["image_item"]["media"]
    assert media["encrypt_query_param"] == "DLPARAM"
    assert media["encrypt_type"] == 1
    # aes_key on the wire is base64(hex-string)
    assert base64.b64decode(media["aes_key"]).decode("ascii") == up["aeskey"]
    assert item["image_item"]["mid_size"] == wi.aes_ecb_padded_size(len(original))
    assert captured["send_req"]["msg"]["context_token"] == "ctx-7"


@requires_crypto
@pytest.mark.asyncio
async def test_upload_uses_param_fallback_url_when_no_full_url(monkeypatch, tmp_path):
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/getuploadurl"):
            return httpx.Response(200, json={"upload_param": "UP/PARAM=="})
        if request.url.host.startswith("novac2c") and path.endswith("/upload"):
            seen_urls.append(str(request.url))
            return httpx.Response(200, headers={"x-encrypted-param": "DL"})
        if path.endswith("/sendmessage"):
            return httpx.Response(200, json={"ret": 0})
        return httpx.Response(404, json={})

    _install_mock_transport(monkeypatch, handler)
    f = tmp_path / "a.bin"
    f.write_bytes(b"data")
    client = WeixinIlinkClient(base_url="https://idc.example.com", token="tok")
    await client.send_file("peer@im.wechat", str(f), context_token="ctx")

    assert seen_urls and "encrypted_query_param=UP%2FPARAM%3D%3D" in seen_urls[0]
    assert "filekey=" in seen_urls[0]


@requires_crypto
@pytest.mark.asyncio
async def test_download_media_from_item_decrypts_and_saves(monkeypatch, tmp_path):
    plaintext = b"decrypted-image-bytes \xf0\x9f\x96\xbc" * 8
    key = bytes(range(16, 32))
    ciphertext = wi.encrypt_aes_ecb(plaintext, key)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("novac2c") and request.url.path.endswith("/download"):
            assert request.url.params.get("encrypted_query_param") == "QP1"
            return httpx.Response(200, content=ciphertext)
        return httpx.Response(404, json={})

    _install_mock_transport(monkeypatch, handler)
    client = WeixinIlinkClient(base_url="https://idc.example.com", token="tok")
    # image_item carries a raw hex aeskey (preferred over media.aes_key)
    item = {
        "type": 2,
        "image_item": {"aeskey": key.hex(), "media": {"encrypt_query_param": "QP1"}},
    }
    saved = await client.download_media_from_item(item, str(tmp_path))
    assert saved is not None
    assert saved.kind == "image"
    assert Path(saved.path).read_bytes() == plaintext


@requires_crypto
@pytest.mark.asyncio
async def test_download_file_item_uses_media_aes_key_and_filename(monkeypatch, tmp_path):
    plaintext = b"%PDF-1.4 fake doc"
    key = bytes(range(16))
    ciphertext = wi.encrypt_aes_ecb(plaintext, key)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/download"):
            return httpx.Response(200, content=ciphertext)
        return httpx.Response(404, json={})

    _install_mock_transport(monkeypatch, handler)
    client = WeixinIlinkClient(base_url="https://idc.example.com", token="tok")
    item = {
        "type": 4,
        "file_item": {
            "file_name": "report.pdf",
            "media": {
                "encrypt_query_param": "QPF",
                # base64(hex-string) encoding (32 chars after decode)
                "aes_key": base64.b64encode(key.hex().encode()).decode(),
            },
        },
    }
    saved = await client.download_media_from_item(item, str(tmp_path))
    assert saved is not None and saved.kind == "file"
    assert saved.file_name == "report.pdf" and saved.mime == "application/pdf"
    assert Path(saved.path).read_bytes() == plaintext


@pytest.mark.asyncio
async def test_get_config_and_send_typing_bodies(monkeypatch):
    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content)
        seen.append((path, body))
        if path.endswith("/getconfig"):
            return httpx.Response(200, json={"ret": 0, "typing_ticket": "TICKET"})
        if path.endswith("/sendtyping"):
            return httpx.Response(200, json={"ret": 0})
        return httpx.Response(404, json={})

    _install_mock_transport(monkeypatch, handler)
    client = WeixinIlinkClient(base_url="https://idc.example.com", token="tok")
    cfg = await client.get_config("u@im.wechat", context_token="ctx")
    assert cfg["typing_ticket"] == "TICKET"
    await client.send_typing("u@im.wechat", typing_ticket="TICKET", status=wi.TYPING_STATUS_TYPING)

    cfg_body = next(b for p, b in seen if p.endswith("/getconfig"))
    assert cfg_body["ilink_user_id"] == "u@im.wechat" and cfg_body["context_token"] == "ctx"
    typing_body = next(b for p, b in seen if p.endswith("/sendtyping"))
    assert typing_body["ilink_user_id"] == "u@im.wechat"
    assert typing_body["typing_ticket"] == "TICKET"
    assert typing_body["status"] == wi.TYPING_STATUS_TYPING
    assert "base_info" in typing_body


@requires_crypto
def test_media_crypto_methods_require_cryptography(monkeypatch):
    # When cryptography is unavailable, media crypto raises a clear, actionable error.
    monkeypatch.setattr(wi, "is_crypto_available", lambda: False)

    def _boom():
        raise wi.WeixinIlinkError("missing")

    monkeypatch.setattr(wi, "_crypto", _boom)
    with pytest.raises(wi.WeixinIlinkError):
        wi.encrypt_aes_ecb(b"x", bytes(range(16)))


# ── phase 2: outbound artifact wiring ───────────────────────────────────────
@pytest.mark.asyncio
async def test_ilink_outbound_uploads_image_artifact(tmp_path):
    from omni.channels.outbound import (
        DeliveryEnvelope,
        DeliveryPart,
        WeixinIlinkOutbound,
        send_delivery,
    )

    img = tmp_path / "fig.png"
    img.write_bytes(b"\x89PNG fake")
    fake = _FakeIlinkClient()
    out = WeixinIlinkOutbound(fake, {"peer@im.wechat": "ctx-img"})
    env = DeliveryEnvelope(parts=[DeliveryPart(kind="image", title="Figure", path=str(img))])
    await send_delivery(out, "peer@im.wechat", env)

    assert fake.images == [("peer@im.wechat", str(img), "ctx-img")]
    assert fake.sent == []


@pytest.mark.asyncio
async def test_ilink_outbound_falls_back_to_text_when_file_unsafe(tmp_path):
    from omni.channels.outbound import (
        DeliveryEnvelope,
        DeliveryPart,
        WeixinIlinkOutbound,
        send_delivery,
    )

    fake = _FakeIlinkClient()
    out = WeixinIlinkOutbound(fake, {"peer@im.wechat": "ctx-img"})
    missing = DeliveryPart(kind="image", title="Fig", path=str(tmp_path / "missing.png"))
    await send_delivery(out, "peer@im.wechat", DeliveryEnvelope(parts=[missing]))

    assert fake.images == []
    assert fake.sent and fake.sent[0][0] == "peer@im.wechat"
    assert fake.sent[0][2] == "ctx-img"


# ── channel runtime ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_channel_inbound_routes_to_agent_and_echoes_context_token(settings):
    from omni.channels.security import add_allowed_external_key
    from omni.channels.wechat import WeChatChannel

    cfg = settings.paths.channels_dir / "wechat.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('mode = "ilink"\nbase_url = "https://idc.example.com"\n', encoding="utf-8")
    add_allowed_external_key(cfg, "user@im.wechat")

    agent = _DummyAgent()
    fake = _FakeIlinkClient()
    channel = WeChatChannel(settings, agent, client=fake)  # type: ignore[arg-type]
    assert channel._ilink_outbound is not None  # fake speaks send_message

    msg = {
        "message_type": 1,
        "from_user_id": "user@im.wechat",
        "context_token": "ctx-1",
        "message_id": 42,
        "item_list": [{"type": 1, "text_item": {"text": "hi there"}}],
    }
    await channel.handle_ilink_message(msg)

    assert agent.turns and agent.turns[0]["text"] == "hi there"
    assert len(fake.sent) == 1
    to, text, ctx = fake.sent[0]
    assert to == "user@im.wechat"
    assert ctx == "ctx-1"
    assert "通道回答" in text


@pytest.mark.asyncio
async def test_session_timeout_stops_the_adapter(settings):
    from omni.channels.wechat import WeChatAuthExpired, WeChatChannel

    cfg = settings.paths.channels_dir / "wechat.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('mode = "ilink"\n', encoding="utf-8")

    class ExpiredClient(_FakeIlinkClient):
        async def get_updates(self, buf: str) -> dict[str, Any]:
            return {"ret": 0, "errcode": -14, "errmsg": "session timeout"}

    channel = WeChatChannel(settings, _DummyAgent(), client=ExpiredClient())  # type: ignore[arg-type]
    channel._cfg["bot_token"] = "expired-token"
    with pytest.raises(WeChatAuthExpired, match="login expired"):
        await channel.start()


@pytest.mark.asyncio
async def test_channel_dedupes_message_id_and_ignores_bot_messages(settings):
    from omni.channels.security import add_allowed_external_key
    from omni.channels.wechat import WeChatChannel

    cfg = settings.paths.channels_dir / "wechat.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('mode = "ilink"\n', encoding="utf-8")
    add_allowed_external_key(cfg, "user@im.wechat")

    agent = _DummyAgent()
    fake = _FakeIlinkClient()
    channel = WeChatChannel(settings, agent, client=fake)  # type: ignore[arg-type]

    msg = {
        "message_type": 1,
        "from_user_id": "user@im.wechat",
        "context_token": "ctx-1",
        "message_id": 7,
        "item_list": [{"type": 1, "text_item": {"text": "repeat"}}],
    }
    await channel.handle_ilink_message(msg)
    await channel.handle_ilink_message(dict(msg))  # same message_id -> deduped
    # bot's own echoed message must never trigger a turn
    await channel.handle_ilink_message(
        {"message_type": 2, "from_user_id": "", "item_list": [{"type": 1, "text_item": {"text": "echo"}}]}
    )

    assert len(agent.turns) == 1
    assert len(fake.sent) == 1


@pytest.mark.asyncio
async def test_channel_shows_typing_indicator_around_turn(settings):
    from omni.channels.security import add_allowed_external_key
    from omni.channels.wechat import WeChatChannel

    cfg = settings.paths.channels_dir / "wechat.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('mode = "ilink"\n', encoding="utf-8")
    add_allowed_external_key(cfg, "user@im.wechat")

    agent = _DummyAgent()
    fake = _FakeIlinkClient()
    channel = WeChatChannel(settings, agent, client=fake)  # type: ignore[arg-type]

    await channel.handle_ilink_message(
        {
            "message_type": 1,
            "from_user_id": "user@im.wechat",
            "context_token": "ctx-1",
            "message_id": 11,
            "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
        }
    )

    # getconfig once (cached), then TYPING (1) before and CANCEL (2) after the turn.
    assert fake.config_calls == [("user@im.wechat", "ctx-1")]
    assert [t[2] for t in fake.typing] == [wi.TYPING_STATUS_TYPING, wi.TYPING_STATUS_CANCEL]
    assert all(t[0] == "user@im.wechat" and t[1] == "T" for t in fake.typing)


@pytest.mark.asyncio
async def test_channel_typing_can_be_disabled(settings):
    from omni.channels.security import add_allowed_external_key
    from omni.channels.wechat import WeChatChannel

    cfg = settings.paths.channels_dir / "wechat.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('mode = "ilink"\ntyping_indicator = false\n', encoding="utf-8")
    add_allowed_external_key(cfg, "user@im.wechat")

    agent = _DummyAgent()
    fake = _FakeIlinkClient()
    channel = WeChatChannel(settings, agent, client=fake)  # type: ignore[arg-type]
    await channel.handle_ilink_message(
        {
            "message_type": 1,
            "from_user_id": "user@im.wechat",
            "message_id": 12,
            "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
        }
    )
    assert fake.typing == []
    assert len(agent.turns) == 1


@pytest.mark.asyncio
async def test_typing_keepalive_refreshes_during_long_turn(settings):
    """A long turn keeps the typing indicator alive (it otherwise expires)."""
    from omni.channels.wechat import WeChatChannel

    cfg = settings.paths.channels_dir / "wechat.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('mode = "ilink"\n', encoding="utf-8")

    agent = _DummyAgent()
    fake = _FakeIlinkClient()
    channel = WeChatChannel(settings, agent, client=fake)  # type: ignore[arg-type]
    channel._typing_refresh_s = 0.02  # fast refresh for the test

    async def _slow_turn() -> None:
        await asyncio.sleep(0.12)  # spans several refresh intervals

    await channel._run_with_typing("user@im.wechat", "ctx-1", _slow_turn())

    statuses = [t[2] for t in fake.typing]
    assert statuses[0] == wi.TYPING_STATUS_TYPING       # initial
    assert statuses[-1] == wi.TYPING_STATUS_CANCEL      # cleared at the end
    # At least one keepalive refresh fired in between (≥2 TYPING total).
    assert statuses.count(wi.TYPING_STATUS_TYPING) >= 2


@pytest.mark.asyncio
async def test_channel_inbound_media_downloads_and_notes_path(settings):
    from omni.channels.security import add_allowed_external_key
    from omni.channels.wechat import WeChatChannel

    cfg = settings.paths.channels_dir / "wechat.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('mode = "ilink"\n', encoding="utf-8")
    add_allowed_external_key(cfg, "user@im.wechat")

    agent = _DummyAgent()
    fake = _FakeIlinkClient()
    channel = WeChatChannel(settings, agent, client=fake)  # type: ignore[arg-type]

    await channel.handle_ilink_message(
        {
            "message_type": 1,
            "from_user_id": "user@im.wechat",
            "context_token": "ctx-9",
            "message_id": 77,
            "item_list": [
                {"type": 1, "text_item": {"text": "看看这张图"}},
                {"type": 2, "image_item": {"media": {"encrypt_query_param": "QP"}}},
            ],
        }
    )

    assert len(fake.downloaded) == 1
    assert agent.turns, "media-bearing message should reach the agent"
    turn_text = agent.turns[0]["text"]
    assert "看看这张图" in turn_text
    assert "image" in turn_text and "saved locally" in turn_text and "x.png" in turn_text
    # reply still echoes the inbound context_token
    assert fake.sent and fake.sent[0][2] == "ctx-9"


@pytest.mark.asyncio
async def test_channel_media_only_message_still_handled(settings):
    from omni.channels.security import add_allowed_external_key
    from omni.channels.wechat import WeChatChannel

    cfg = settings.paths.channels_dir / "wechat.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('mode = "ilink"\n', encoding="utf-8")
    add_allowed_external_key(cfg, "user@im.wechat")

    agent = _DummyAgent()
    fake = _FakeIlinkClient()
    channel = WeChatChannel(settings, agent, client=fake)  # type: ignore[arg-type]
    await channel.handle_ilink_message(
        {
            "message_type": 1,
            "from_user_id": "user@im.wechat",
            "message_id": 78,
            "item_list": [{"type": 2, "image_item": {"media": {"encrypt_query_param": "QP"}}}],
        }
    )
    assert len(agent.turns) == 1
    assert "image" in agent.turns[0]["text"]


# ── login persistence (CLI) ────────────────────────────────────────────────
def test_login_persists_credentials_and_binds_user(settings, monkeypatch):
    from omni.channels.config import load_channel_config
    from omni.channels.manager import channel_config_state
    from omni.cli.commands import channel_cmd

    class _FakeLoginClient:
        @classmethod
        def from_config(cls, cfg: dict[str, Any]) -> _FakeLoginClient:
            return cls()

        async def get_bot_qrcode(self, *, base: str | None = None) -> wi.QrCode:
            return wi.QrCode(qrcode="QR", qrcode_url="https://liteapp.weixin.qq.com/x")

        async def wait_for_login(self, qrcode: str, **kwargs: Any) -> wi.LoginResult:
            return wi.LoginResult(
                connected=True,
                bot_token="tok-xyz",
                account_id="botacc@im.bot",
                base_url="https://idc.example.com",
                user_id="user@im.wechat",
            )

    monkeypatch.setattr(wi, "WeixinIlinkClient", _FakeLoginClient)

    channel_cmd._login_wechat_ilink(
        settings.paths,
        credential_store="file",
        allow=None,
        no_wait=False,
        no_qr=True,
        timeout_s=5,
        non_interactive=True,
    )

    data = load_channel_config(settings, "wechat")
    assert data.get("mode") == "ilink"
    assert data.get("bot_token") == "tok-xyz"
    assert data.get("base_url") == "https://idc.example.com"
    assert data.get("account_id") == "botacc@im.bot"
    assert "user@im.wechat" in (data.get("allowed_external_keys") or [])

    configured, reason = channel_config_state(settings, "wechat")
    assert configured is True, reason


def test_login_falls_back_to_secrets_file_when_keychain_refuses(settings, monkeypatch):
    """A locked/non-interactive keychain must not discard a freshly-scanned token.

    Reproduces ``SecKeychainItemCreateFromContent: User interaction is not
    allowed.``: the keychain exists but refuses the write, so login must fall
    back to ``secrets.toml`` (not exit) and stay fully configured.
    """
    import tomllib

    import omni.channels.credentials as creds
    from omni.channels.config import load_channel_config
    from omni.channels.manager import channel_config_state
    from omni.cli.commands import channel_cmd

    class _FakeLoginClient:
        @classmethod
        def from_config(cls, cfg: dict[str, Any]) -> _FakeLoginClient:
            return cls()

        async def get_bot_qrcode(self, *, base: str | None = None) -> wi.QrCode:
            return wi.QrCode(qrcode="QR", qrcode_url="https://liteapp.weixin.qq.com/x")

        async def wait_for_login(self, qrcode: str, **kwargs: Any) -> wi.LoginResult:
            return wi.LoginResult(
                connected=True,
                bot_token="tok-xyz",
                account_id="botacc@im.bot",
                base_url="https://idc.example.com",
                user_id="user@im.wechat",
            )

    monkeypatch.setattr(wi, "WeixinIlinkClient", _FakeLoginClient)
    # Keychain is present, but every write refuses with the macOS interaction error.
    monkeypatch.setattr(creds, "_has_macos_keychain", lambda: True)

    def _refuse(channel: str, key: str, value: str) -> None:
        raise creds.CredentialStoreError(
            "SecKeychainItemCreateFromContent (<default>): User interaction is not allowed."
        )

    monkeypatch.setattr(creds, "_store_macos_keychain", _refuse)

    # auto backend (the default) must not raise — it falls back to secrets.toml.
    channel_cmd._login_wechat_ilink(
        settings.paths,
        credential_store="auto",
        allow=None,
        no_wait=False,
        no_qr=True,
        timeout_s=5,
        non_interactive=True,
    )

    data = load_channel_config(settings, "wechat")
    assert data.get("bot_token") == "tok-xyz"
    assert data.get("mode") == "ilink"
    assert data.get("base_url") == "https://idc.example.com"
    assert data.get("account_id") == "botacc@im.bot"
    assert "user@im.wechat" in (data.get("allowed_external_keys") or [])

    # token landed in secrets.toml, and wechat.toml carries no dangling keychain ref
    secrets = tomllib.loads(settings.paths.secrets_file.read_text(encoding="utf-8"))
    assert secrets["channels"]["wechat"]["bot_token"] == "tok-xyz"
    raw = tomllib.loads((settings.paths.channels_dir / "wechat.toml").read_text(encoding="utf-8"))
    assert "bot_token" not in raw.get("credential_refs", {})

    configured, reason = channel_config_state(settings, "wechat")
    assert configured is True, reason


def test_login_without_any_keychain_still_stores_the_token(settings, monkeypatch):
    """Linux and Windows have no OS keychain, and the default must still succeed.

    The bot token only exists because the user just scanned a QR, so failing
    here would discard the expensive step and force every non-macOS user to
    rerun login with an explicit ``--credential-store file``.
    """
    import tomllib

    import omni.channels.credentials as creds
    from omni.channels.config import load_channel_config
    from omni.channels.manager import channel_config_state
    from omni.cli.commands import channel_cmd

    class _FakeLoginClient:
        @classmethod
        def from_config(cls, cfg: dict[str, Any]) -> _FakeLoginClient:
            return cls()

        async def get_bot_qrcode(self, *, base: str | None = None) -> wi.QrCode:
            return wi.QrCode(qrcode="QR", qrcode_url="https://liteapp.weixin.qq.com/x")

        async def wait_for_login(self, qrcode: str, **kwargs: Any) -> wi.LoginResult:
            return wi.LoginResult(
                connected=True,
                bot_token="tok-nokeychain",
                account_id="botacc@im.bot",
                base_url="https://idc.example.com",
                user_id="user@im.wechat",
            )

    monkeypatch.setattr(wi, "WeixinIlinkClient", _FakeLoginClient)
    monkeypatch.setattr(creds, "_has_macos_keychain", lambda: False)

    channel_cmd._login_wechat_ilink(
        settings.paths,
        credential_store="auto",
        allow=None,
        no_wait=False,
        no_qr=True,
        timeout_s=5,
        non_interactive=True,
    )

    secrets = tomllib.loads(settings.paths.secrets_file.read_text(encoding="utf-8"))
    assert secrets["channels"]["wechat"]["bot_token"] == "tok-nokeychain"
    data = load_channel_config(settings, "wechat")
    assert data.get("mode") == "ilink"
    assert "user@im.wechat" in (data.get("allowed_external_keys") or [])
    configured, reason = channel_config_state(settings, "wechat")
    assert configured is True, reason


def test_login_scan_is_not_gated_behind_a_confirmation_prompt(settings, monkeypatch):
    """`channel login wechat` must go straight to the QR.

    The ClawBot API is Tencent's own, so the old "not endorsed by Tencent"
    confirmation is both wrong and the reason a non-interactive run used to
    abort before ever showing a code.
    """
    from omni.cli import render
    from omni.cli.commands import channel_cmd

    shown: list[str] = []

    class _FakeLoginClient:
        @classmethod
        def from_config(cls, cfg: dict[str, Any]) -> _FakeLoginClient:
            return cls()

        async def get_bot_qrcode(self, *, base: str | None = None) -> wi.QrCode:
            return wi.QrCode(qrcode="QR", qrcode_url="https://liteapp.weixin.qq.com/x")

        async def wait_for_login(self, qrcode: str, **kwargs: Any) -> wi.LoginResult:
            return wi.LoginResult(connected=True, bot_token="tok", user_id="user@im.wechat")

    monkeypatch.setattr(wi, "WeixinIlinkClient", _FakeLoginClient)
    monkeypatch.setattr(channel_cmd, "render_terminal_qr", shown.append)

    def _no_prompts(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("login must not ask for confirmation before showing the QR")

    monkeypatch.setattr(render, "confirm", _no_prompts)

    channel_cmd._login_wechat_ilink(
        settings.paths,
        credential_store="file",
        allow=None,
        no_wait=False,
        no_qr=False,
        timeout_s=5,
        non_interactive=True,
    )

    assert shown == ["https://liteapp.weixin.qq.com/x"]


def test_login_no_wait_writes_template_without_token(settings):
    from omni.channels.manager import channel_config_state
    from omni.cli.commands import channel_cmd

    channel_cmd._login_wechat_ilink(
        settings.paths,
        credential_store="file",
        allow=None,
        no_wait=True,
        no_qr=True,
        timeout_s=5,
        non_interactive=True,
    )

    configured, reason = channel_config_state(settings, "wechat")
    assert configured is False
    assert "bot_token" in reason


def test_leftover_self_hosted_wechat_bridge_is_not_configured(settings):
    """Abandoned :8088 / WeCom files are incomplete; users must scan the iLink QR."""
    from omni.channels.manager import channel_config_state

    cfg = settings.paths.channels_dir / "wechat.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        'mode = "gateway"\n'
        'gateway_url = "http://127.0.0.1:8088"\n'
        'inbox_path = "/messages"\n'
        'send_path = "/send"\n',
        encoding="utf-8",
    )
    configured, reason = channel_config_state(settings, "wechat")
    assert configured is False
    assert "bot_token" in reason


def test_ilink_bot_token_alone_is_configured(settings):
    from omni.channels.manager import channel_config_state

    settings.paths.channels_dir.mkdir(parents=True, exist_ok=True)
    (settings.paths.channels_dir / "wechat.toml").write_text('mode = "ilink"\n', encoding="utf-8")
    settings.paths.secrets_file.write_text(
        '[channels.wechat]\nbot_token = "tok-only"\n', encoding="utf-8"
    )
    configured, reason = channel_config_state(settings, "wechat")
    assert configured is True, reason
