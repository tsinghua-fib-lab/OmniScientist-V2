"""WeChat (Weixin) iLink bot connector.

A small, self-contained client for Tencent's iLink bot HTTP/JSON API — the same
backend the official ``@tencent-weixin/openclaw-weixin`` OpenClaw plugin speaks.
It lets OmniScientist reuse the existing WeChat ClawBot liteapp bot: the user scans
the QR in the terminal, then chats with that bot in WeChat and OmniScientist answers.

Compatibility behavior was adapted from the plugin's documented backend API protocol
and MIT-licensed transport source (see NOTICE):

* Login host ``https://ilinkai.weixin.qq.com``
  - ``POST ilink/bot/get_bot_qrcode?bot_type=3`` -> ``{qrcode, qrcode_img_content}``
    (``qrcode_img_content`` is the ``liteapp.weixin.qq.com/...`` URL to render).
  - long-poll ``GET ilink/bot/get_qrcode_status?qrcode=...`` -> a status machine;
    ``confirmed`` yields ``bot_token``, ``ilink_bot_id``, ``baseurl``, ``ilink_user_id``.
* Messaging host = the per-account ``baseurl`` returned at login.
  - ``POST ilink/bot/getupdates`` long-poll inbound (rolling ``get_updates_buf`` cursor)
  - ``POST ilink/bot/sendmessage`` outbound (echo each message's ``context_token``)
  - ``ilink/bot/msg/notifystart`` / ``notifystop`` on lifecycle.

Every request carries ``iLink-App-Id``/``iLink-App-ClientVersion`` headers; POSTs add
``AuthorizationType: ilink_bot_token`` + ``X-WECHAT-UIN`` (+ ``Authorization: Bearer
<token>`` once logged in) and a ``base_info`` body field. ``base_info.bot_agent`` is
observability-only and honestly identifies the client as OmniScientist.

Tencent publishes this bot API as the "WeChat ClawBot" plugin functionality, under
its own terms of use, which govern the paired account. OmniScientist is an
independent client of that public API rather than the official plugin, so the
backend may still rate-limit or deny by ``bot_agent``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import random
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
# Default Weixin C2C CDN host for encrypted media upload/download.
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_BOT_TYPE = "3"
# Wire-compatibility version advertised via ``iLink-App-ClientVersion`` and
# ``base_info.channel_version``. It tracks the reference plugin's published
# version so the backend accepts us; override with the channel config key
# ``ilink_client_version`` if the minimum is bumped. ``bot_agent`` below
# honestly identifies the client as OmniScientist.
DEFAULT_CLIENT_VERSION = "2.4.6"
ILINK_APP_ID = "bot"

_LONG_POLL_TIMEOUT_S = 35.0
_API_TIMEOUT_S = 15.0
_CONFIG_TIMEOUT_S = 10.0
# iLink text bodies are rejected well before 3500 chars (~2000–2400 in practice),
# so cap segments at a conservative size for reliable delivery. Long replies are
# split into several FINISH bubbles (pseudo-streaming) by ``_chunk_text``.
_SEND_CHUNK_LIMIT = 1800
_CDN_UPLOAD_MAX_RETRIES = 3
# Refuse to download/decrypt absurdly large inbound media (mirrors the plugin's cap).
MEDIA_MAX_BYTES = 100 * 1024 * 1024
SESSION_TIMEOUT_ERRCODE = -14

# WeixinMessage.message_type
_MSG_TYPE_USER = 1
_MSG_TYPE_BOT = 2
# MessageItem.type
_ITEM_TYPE_TEXT = 1
_ITEM_TYPE_IMAGE = 2
_ITEM_TYPE_VOICE = 3
_ITEM_TYPE_FILE = 4
_ITEM_TYPE_VIDEO = 5
_ITEM_TYPE_NAMES = {
    _ITEM_TYPE_TEXT: "text",
    _ITEM_TYPE_IMAGE: "image",
    _ITEM_TYPE_VOICE: "voice",
    _ITEM_TYPE_FILE: "file",
    _ITEM_TYPE_VIDEO: "video",
}

# proto: UploadMediaType
UPLOAD_MEDIA_IMAGE = 1
UPLOAD_MEDIA_VIDEO = 2
UPLOAD_MEDIA_FILE = 3
UPLOAD_MEDIA_VOICE = 4

# proto: TypingStatus
TYPING_STATUS_TYPING = 1
TYPING_STATUS_CANCEL = 2

# Minimal extension -> MIME map for naming/saving inbound CDN media.
_EXT_TO_MIME = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


class WeixinIlinkError(RuntimeError):
    """Unrecoverable iLink client/protocol error."""


# ── AES-128-ECB media crypto ────────────────────────────────────────────────
# Weixin CDN media is AES-128-ECB with PKCS7 padding. ``cryptography`` is an
# optional dependency (install ``OmniScientist-V2[channels]``); text chat never
# needs it, so the import is lazy and callers fall back to a text link when it
# is missing.
def is_crypto_available() -> bool:
    """True when the optional ``cryptography`` backend is importable."""
    try:
        import cryptography.hazmat.primitives.ciphers  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _crypto():  # noqa: ANN202
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7

        return Cipher, algorithms, modes, PKCS7
    except Exception as exc:  # noqa: BLE001
        raise WeixinIlinkError(
            "WeChat media encryption requires the 'cryptography' package. Install `OmniScientist-V2[channels]` "
            "or run `pip install cryptography`."
        ) from exc


def aes_ecb_padded_size(plaintext_size: int) -> int:
    """Ciphertext size for AES-128-ECB with PKCS7 (always pads to a full block)."""
    return ((plaintext_size // 16) + 1) * 16


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB encrypt with PKCS7 padding (key must be 16 bytes)."""
    Cipher, algorithms, modes, PKCS7 = _crypto()
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()  # noqa: S305 - protocol-mandated ECB
    return enc.update(padded) + enc.finalize()


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB decrypt with PKCS7 unpadding (key must be 16 bytes)."""
    Cipher, algorithms, modes, PKCS7 = _crypto()
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()  # noqa: S305 - protocol-mandated ECB
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def parse_aes_key(aes_key_b64: str) -> bytes:
    """Decode a ``CDNMedia.aes_key`` JSON field into a raw 16-byte key.

    Two encodings appear in the wild (mirrors the reference plugin):
    ``base64(raw 16 bytes)`` (images) and ``base64(hex string of 16 bytes)``
    (file/voice/video, i.e. 32 ASCII hex chars after base64-decoding).
    """
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", decoded):
        return bytes.fromhex(decoded.decode("ascii"))
    raise WeixinIlinkError(
        f"aes_key must decode to 16 bytes or be 32 hexadecimal characters; got {len(decoded)} bytes."
    )


@dataclass
class QrCode:
    qrcode: str
    qrcode_url: str


@dataclass
class LoginResult:
    connected: bool = False
    already_connected: bool = False
    bot_token: str = ""
    account_id: str = ""
    base_url: str = ""
    user_id: str = ""
    message: str = ""


@dataclass
class UploadedMedia:
    """Result of a CDN media upload, ready to embed in an outbound message item."""

    filekey: str
    download_param: str
    aeskey_hex: str
    file_size: int
    file_size_ciphertext: int


@dataclass
class InboundMedia:
    """A decrypted inbound media file saved to disk."""

    path: str
    kind: str  # image | file | video | voice
    mime: str = ""
    file_name: str = ""


def client_version_uint32(version: str) -> int:
    """Encode ``"major.minor.patch"`` as ``major<<16 | minor<<8 | patch`` (uint32)."""
    parts = [int(p) for p in re.findall(r"\d+", version)[:3]]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[0], parts[1], parts[2]
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def default_bot_agent() -> str:
    return f"OmniScientist/{_omni_version()}"


def _omni_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            from omni.runtime.dist_meta import DIST_NAME

            return version(DIST_NAME)
        except PackageNotFoundError:
            return "0.0.0"
    except Exception:  # noqa: BLE001
        return "0.0.0"


def _random_wechat_uin() -> str:
    return base64.b64encode(str(random.getrandbits(32)).encode()).decode()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def is_bot_message(msg: dict[str, Any]) -> bool:
    """True for the bot's own (outbound/echoed) messages — we never reply to those."""
    return int(msg.get("message_type") or 0) == _MSG_TYPE_BOT


def message_text(msg: dict[str, Any]) -> str:
    """Extract a plain-text body from a WeixinMessage's ``item_list``.

    Mirrors the plugin's ``bodyFromItemList``: prefer the first TEXT item (prefixing
    a quoted-message marker when it references another message), and fall back to a
    voice transcript. Media-only messages return ``""``; their binary payloads
    are fetched separately via :func:`media_items` +
    :meth:`WeixinIlinkClient.download_media_from_item`.
    """
    items = msg.get("item_list")
    if not isinstance(items, list):
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        itype = int(item.get("type") or 0)
        if itype == _ITEM_TYPE_TEXT:
            text_item = item.get("text_item") or {}
            text = str(text_item.get("text") or "")
            ref = item.get("ref_msg")
            if not isinstance(ref, dict):
                return text
            parts: list[str] = []
            if ref.get("title"):
                parts.append(str(ref.get("title")))
            ref_item = ref.get("message_item")
            if isinstance(ref_item, dict):
                ref_body = message_text({"item_list": [ref_item]})
                if ref_body:
                    parts.append(ref_body)
            return f"[Quoted: {' | '.join(parts)}]\n{text}" if parts else text
        if itype == _ITEM_TYPE_VOICE:
            voice = item.get("voice_item") or {}
            if voice.get("text"):
                return str(voice.get("text"))
    return ""


def media_items(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the image/file/video MessageItems of an inbound message."""
    items = msg.get("item_list")
    if not isinstance(items, list):
        return []
    media_types = {_ITEM_TYPE_IMAGE, _ITEM_TYPE_FILE, _ITEM_TYPE_VIDEO}
    return [
        item
        for item in items
        if isinstance(item, dict) and int(item.get("type") or 0) in media_types
    ]


def _chunk_text(text: str, limit: int = _SEND_CHUNK_LIMIT) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text] if text.strip() else []
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return [c.strip("\n") for c in chunks if c.strip()]


class WeixinIlinkClient:
    """Async client for the Weixin iLink bot API (login + messaging)."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token: str = "",
        bot_type: str = DEFAULT_BOT_TYPE,
        client_version: str = DEFAULT_CLIENT_VERSION,
        bot_agent: str = "",
        cdn_base_url: str = DEFAULT_CDN_BASE_URL,
        timeout_s: float = _API_TIMEOUT_S,
        long_poll_timeout_s: float = _LONG_POLL_TIMEOUT_S,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = (token or "").strip()
        self.bot_type = str(bot_type or DEFAULT_BOT_TYPE)
        self.client_version = client_version or DEFAULT_CLIENT_VERSION
        self.bot_agent = bot_agent or default_bot_agent()
        self.cdn_base_url = (cdn_base_url or DEFAULT_CDN_BASE_URL).rstrip("/")
        self.timeout_s = float(timeout_s)
        self.long_poll_timeout_s = float(long_poll_timeout_s)
        # Position within the current reply, for diagnosing refusals. Only the
        # reply being sent matters, so one token is remembered rather than a map.
        self._reply_token = ""
        self._reply_length = 0

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> WeixinIlinkClient:
        return cls(
            base_url=str(cfg.get("base_url") or DEFAULT_BASE_URL),
            token=str(cfg.get("bot_token") or ""),
            bot_type=str(cfg.get("bot_type") or DEFAULT_BOT_TYPE),
            client_version=str(cfg.get("ilink_client_version") or DEFAULT_CLIENT_VERSION),
            bot_agent=str(cfg.get("bot_agent") or default_bot_agent()),
            cdn_base_url=str(cfg.get("cdn_base_url") or DEFAULT_CDN_BASE_URL),
        )

    # ── headers / base_info ────────────────────────────────────────────────
    def _common_headers(self) -> dict[str, str]:
        return {
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(client_version_uint32(self.client_version)),
        }

    def _post_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _random_wechat_uin(),
            **self._common_headers(),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _base_info(self) -> dict[str, str]:
        return {"channel_version": self.client_version, "bot_agent": self.bot_agent}

    def _url(self, endpoint: str, base: str | None = None) -> str:
        root = (base or self.base_url).rstrip("/")
        return f"{root}/{endpoint.lstrip('/')}"

    async def _post(
        self,
        endpoint: str,
        body: dict[str, Any],
        *,
        params: dict[str, str] | None = None,
        base: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout or self.timeout_s) as client:
            res = await client.post(
                self._url(endpoint, base), headers=self._post_headers(), params=params, json=body
            )
            res.raise_for_status()
            return _as_dict(res.json())

    async def _get(
        self,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        base: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout or self.timeout_s) as client:
            res = await client.get(
                self._url(endpoint, base), headers=self._common_headers(), params=params
            )
            res.raise_for_status()
            return _as_dict(res.json())

    # ── login ──────────────────────────────────────────────────────────────
    async def get_bot_qrcode(self, *, base: str | None = None) -> QrCode:
        data = await self._post(
            "ilink/bot/get_bot_qrcode",
            {"local_token_list": []},
            params={"bot_type": self.bot_type},
            base=base or DEFAULT_BASE_URL,
        )
        qrcode = str(data.get("qrcode") or "")
        url = str(data.get("qrcode_img_content") or "")
        if not qrcode or not url:
            raise WeixinIlinkError(f"Failed to obtain QR code: {data}")
        return QrCode(qrcode=qrcode, qrcode_url=url)

    async def poll_qr_status(
        self, qrcode: str, *, verify_code: str = "", base: str | None = None
    ) -> dict[str, Any]:
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        try:
            return await self._get(
                "ilink/bot/get_qrcode_status",
                params=params,
                base=base or DEFAULT_BASE_URL,
                timeout=self.long_poll_timeout_s + 5,
            )
        except httpx.TimeoutException:
            return {"status": "wait"}
        except httpx.HTTPError as exc:  # transient gateway error -> keep waiting
            logger.debug("poll_qr_status transient error: %s", exc)
            return {"status": "wait"}

    async def wait_for_login(
        self,
        qrcode: str,
        *,
        timeout_s: float = 480.0,
        base: str | None = None,
        verify_code_provider: Callable[[], Awaitable[str]] | None = None,
        on_refresh: Callable[[str], None] | None = None,
        max_qr_refresh: int = 3,
    ) -> LoginResult:
        """Drive the QR status state machine until login resolves or times out."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(1.0, timeout_s)
        current_base = base or DEFAULT_BASE_URL
        pending_code = ""
        refreshed = 0
        while loop.time() < deadline:
            status_data = await self.poll_qr_status(
                qrcode, verify_code=pending_code, base=current_base
            )
            status = str(status_data.get("status") or "wait")
            if status == "confirmed":
                if not status_data.get("ilink_bot_id"):
                    return LoginResult(message="Login failed: the service returned no ilink_bot_id.")
                return LoginResult(
                    connected=True,
                    bot_token=str(status_data.get("bot_token") or ""),
                    account_id=str(status_data.get("ilink_bot_id") or ""),
                    base_url=str(status_data.get("baseurl") or current_base),
                    user_id=str(status_data.get("ilink_user_id") or ""),
                    message="Connected the local OmniScientist instance to WeChat.",
                )
            if status == "binded_redirect":
                return LoginResult(
                    already_connected=True, message="This bot is already connected to the local instance."
                )
            if status == "scaned_but_redirect":
                redirect_host = str(status_data.get("redirect_host") or "")
                if redirect_host:
                    current_base = f"https://{redirect_host}"
                continue
            if status == "need_verifycode":
                if verify_code_provider is None:
                    return LoginResult(message="A verification code must be entered in WeChat. Retry from an interactive terminal.")
                pending_code = (await verify_code_provider()).strip()
                continue
            if status in {"expired", "verify_code_blocked"}:
                pending_code = ""
                refreshed += 1
                if refreshed > max_qr_refresh:
                    return LoginResult(message="The QR code expired repeatedly. Connection stopped; retry later.")
                qr = await self.get_bot_qrcode(base=current_base)
                qrcode = qr.qrcode
                if on_refresh is not None:
                    on_refresh(qr.qrcode_url)
                continue
            # wait / scaned -> keep polling
            await asyncio.sleep(1.0)
        return LoginResult(message="Login timed out. Retry.")

    # ── messaging ──────────────────────────────────────────────────────────
    async def get_updates(self, get_updates_buf: str) -> dict[str, Any]:
        body = {"get_updates_buf": get_updates_buf or "", "base_info": self._base_info()}
        try:
            return await self._post(
                "ilink/bot/getupdates", body, timeout=self.long_poll_timeout_s + 5
            )
        except httpx.TimeoutException:
            # Long-poll client timeout is normal; retry with the same cursor.
            return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}

    async def send_message(
        self, to_user_id: str, text: str, *, context_token: str | None = None
    ) -> None:
        for chunk in _chunk_text(text) or [""]:
            await self._send_item(
                to_user_id,
                {"type": _ITEM_TYPE_TEXT, "text_item": {"text": chunk}},
                context_token=context_token,
            )

    async def _send_item(
        self, to_user_id: str, item: dict[str, Any], *, context_token: str | None = None
    ) -> None:
        """Send exactly one MessageItem (text or media) downstream."""
        msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": uuid.uuid4().hex,
            "message_type": _MSG_TYPE_BOT,
            "message_state": 2,  # FINISH
            "item_list": [item],
        }
        if context_token:
            msg["context_token"] = context_token
        position = self._position_in_reply(context_token)
        data = await self._post(
            "ilink/bot/sendmessage", {"msg": msg, "base_info": self._base_info()}
        )
        ret = data.get("ret")
        if ret not in (None, 0):
            raise WeixinIlinkError(
                f"sendmessage failed: ret={ret} errmsg={data.get('errmsg')} "
                f"(item={_ITEM_TYPE_NAMES.get(int(item.get('type') or 0), 'unknown')}, "
                f"message {position} of this reply)"
            )

    def _position_in_reply(self, context_token: str | None) -> int:
        """Which message of the current reply this is, counting from one.

        Upstream answered ``ret=-2 prepare failed`` to every send from the
        eleventh onward on task 964f17aa, having accepted the ten before it — the
        reply was a whole paper split into bubbles, and the figures queued after
        it never went out. Whether the rule is a per-reply quota is not something
        this side can see, so the count travels with the error instead of being
        guessed at: the next report says outright which message was refused.

        ``context_token`` identifies the inbound message being answered, which is
        exactly the scope of "this reply".
        """
        token = context_token or ""
        if token != self._reply_token:
            self._reply_token, self._reply_length = token, 0
        self._reply_length += 1
        return self._reply_length

    # ── typing indicator ─────────────────────────────────────────────────────
    async def get_config(
        self, ilink_user_id: str, *, context_token: str | None = None
    ) -> dict[str, Any]:
        """Fetch per-user bot config (notably ``typing_ticket``)."""
        body: dict[str, Any] = {"ilink_user_id": ilink_user_id, "base_info": self._base_info()}
        if context_token:
            body["context_token"] = context_token
        return await self._post("ilink/bot/getconfig", body, timeout=_CONFIG_TIMEOUT_S)

    async def send_typing(
        self,
        ilink_user_id: str,
        *,
        typing_ticket: str = "",
        status: int = TYPING_STATUS_TYPING,
    ) -> None:
        """Send a typing (1) / cancel-typing (2) indicator to a user."""
        body = {
            "ilink_user_id": ilink_user_id,
            "typing_ticket": typing_ticket,
            "status": int(status),
            "base_info": self._base_info(),
        }
        await self._post("ilink/bot/sendtyping", body, timeout=_CONFIG_TIMEOUT_S)

    # ── media: outbound (upload + send) ──────────────────────────────────────
    async def get_upload_url(
        self,
        *,
        filekey: str,
        media_type: int,
        to_user_id: str,
        rawsize: int,
        rawfilemd5: str,
        filesize: int,
        aeskey_hex: str,
        no_need_thumb: bool = True,
    ) -> dict[str, Any]:
        body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": no_need_thumb,
            "aeskey": aeskey_hex,
            "base_info": self._base_info(),
        }
        return await self._post("ilink/bot/getuploadurl", body)

    async def upload_media(
        self, file_path: str, to_user_id: str, *, media_type: int
    ) -> UploadedMedia:
        """Encrypt + upload a local file to the Weixin CDN, returning a CDN ref."""
        path = Path(file_path)
        plaintext = path.read_bytes()
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()  # noqa: S324 - protocol-mandated MD5 checksum
        filesize = aes_ecb_padded_size(rawsize)
        filekey = os.urandom(16).hex()
        aeskey = os.urandom(16)

        resp = await self.get_upload_url(
            filekey=filekey,
            media_type=media_type,
            to_user_id=to_user_id,
            rawsize=rawsize,
            rawfilemd5=rawfilemd5,
            filesize=filesize,
            aeskey_hex=aeskey.hex(),
        )
        upload_full_url = str(resp.get("upload_full_url") or "").strip()
        upload_param = str(resp.get("upload_param") or "").strip()
        if not upload_full_url and not upload_param:
            raise WeixinIlinkError(f"getuploadurl returned no upload URL: {resp}")

        download_param = await self._cdn_upload(
            plaintext,
            aeskey,
            upload_full_url=upload_full_url,
            upload_param=upload_param,
            filekey=filekey,
        )
        return UploadedMedia(
            filekey=filekey,
            download_param=download_param,
            aeskey_hex=aeskey.hex(),
            file_size=rawsize,
            file_size_ciphertext=filesize,
        )

    async def _cdn_upload(
        self,
        plaintext: bytes,
        aeskey: bytes,
        *,
        upload_full_url: str,
        upload_param: str,
        filekey: str,
    ) -> str:
        ciphertext = encrypt_aes_ecb(plaintext, aeskey)
        if upload_full_url:
            url = upload_full_url
        else:
            url = (
                f"{self.cdn_base_url}/upload?encrypted_query_param={quote(upload_param, safe='')}"
                f"&filekey={quote(filekey, safe='')}"
            )
        last_error: str = ""
        for attempt in range(_CDN_UPLOAD_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    res = await client.post(
                        url,
                        headers={"Content-Type": "application/octet-stream"},
                        content=ciphertext,
                    )
                if 400 <= res.status_code < 500:
                    raise WeixinIlinkError(
                        f"CDN upload client error {res.status_code}: "
                        f"{res.headers.get('x-error-message') or res.text[:200]}"
                    )
                if res.status_code != 200:
                    last_error = res.headers.get("x-error-message") or f"status {res.status_code}"
                    raise httpx.HTTPError(last_error)
                download_param = res.headers.get("x-encrypted-param") or ""
                if not download_param:
                    raise WeixinIlinkError("CDN upload response is missing the x-encrypted-param header.")
                return download_param
            except WeixinIlinkError:
                raise
            except httpx.HTTPError as exc:
                last_error = str(exc)
                if attempt < _CDN_UPLOAD_MAX_RETRIES - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
        raise WeixinIlinkError(f"CDN upload failed after {_CDN_UPLOAD_MAX_RETRIES} attempts: {last_error}")

    @staticmethod
    def _media_item(uploaded: UploadedMedia, *, item_type: int, **extra: Any) -> dict[str, Any]:
        media = {
            "encrypt_query_param": uploaded.download_param,
            # aes_key is base64(hex-string) — see parse_aes_key's second encoding.
            "aes_key": base64.b64encode(uploaded.aeskey_hex.encode("ascii")).decode("ascii"),
            "encrypt_type": 1,
        }
        sub_key = {
            _ITEM_TYPE_IMAGE: "image_item",
            _ITEM_TYPE_FILE: "file_item",
            _ITEM_TYPE_VIDEO: "video_item",
        }[item_type]
        return {"type": item_type, sub_key: {"media": media, **extra}}

    async def send_image(
        self, to_user_id: str, file_path: str, *, context_token: str | None = None
    ) -> None:
        uploaded = await self.upload_media(file_path, to_user_id, media_type=UPLOAD_MEDIA_IMAGE)
        item = self._media_item(
            uploaded, item_type=_ITEM_TYPE_IMAGE, mid_size=uploaded.file_size_ciphertext
        )
        await self._send_item(to_user_id, item, context_token=context_token)

    async def send_file(
        self,
        to_user_id: str,
        file_path: str,
        *,
        file_name: str | None = None,
        context_token: str | None = None,
    ) -> None:
        uploaded = await self.upload_media(file_path, to_user_id, media_type=UPLOAD_MEDIA_FILE)
        item = self._media_item(
            uploaded,
            item_type=_ITEM_TYPE_FILE,
            file_name=file_name or Path(file_path).name,
            len=str(uploaded.file_size),
        )
        await self._send_item(to_user_id, item, context_token=context_token)

    async def send_video(
        self, to_user_id: str, file_path: str, *, context_token: str | None = None
    ) -> None:
        uploaded = await self.upload_media(file_path, to_user_id, media_type=UPLOAD_MEDIA_VIDEO)
        item = self._media_item(
            uploaded, item_type=_ITEM_TYPE_VIDEO, video_size=uploaded.file_size_ciphertext
        )
        await self._send_item(to_user_id, item, context_token=context_token)

    # ── media: inbound (download + decrypt) ──────────────────────────────────
    async def _cdn_download(
        self, encrypt_query_param: str, aes_key_b64: str, *, full_url: str = ""
    ) -> bytes:
        key = parse_aes_key(aes_key_b64)
        url = full_url or (
            f"{self.cdn_base_url}/download?encrypted_query_param={quote(encrypt_query_param, safe='')}"
        )
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            res = await client.get(url)
            res.raise_for_status()
            ciphertext = res.content
        if len(ciphertext) > MEDIA_MAX_BYTES:
            raise WeixinIlinkError(f"CDN media is too large ({len(ciphertext)} bytes); download rejected.")
        return decrypt_aes_ecb(ciphertext, key)

    async def download_media_from_item(
        self, item: dict[str, Any], dest_dir: str
    ) -> InboundMedia | None:
        """Download + AES-128-ECB decrypt one inbound media MessageItem to disk.

        Returns ``None`` for non-media / unsupported items or on failure. Voice
        is already surfaced as a transcript via :func:`message_text`, so only
        image/file/video binary payloads are fetched here.
        """
        itype = int(item.get("type") or 0)
        spec = {
            _ITEM_TYPE_IMAGE: ("image_item", "image", "media image", ".jpg"),
            _ITEM_TYPE_FILE: ("file_item", "file", "attachment", ".bin"),
            _ITEM_TYPE_VIDEO: ("video_item", "video", "video", ".mp4"),
        }.get(itype)
        if spec is None:
            return None
        sub_key, kind, default_name, default_ext = spec
        sub = item.get(sub_key)
        if not isinstance(sub, dict):
            return None
        media = sub.get("media")
        if not isinstance(media, dict):
            return None
        encrypt_query_param = str(media.get("encrypt_query_param") or "")
        full_url = str(media.get("full_url") or "")
        if not encrypt_query_param and not full_url:
            return None
        # Images may carry a raw hex aeskey on the item; others use media.aes_key.
        item_aeskey = str(sub.get("aeskey") or "")
        if item_aeskey:
            aes_key_b64 = base64.b64encode(bytes.fromhex(item_aeskey)).decode("ascii")
        else:
            aes_key_b64 = str(media.get("aes_key") or "")
        if not aes_key_b64:
            return None
        try:
            data = await self._cdn_download(encrypt_query_param, aes_key_b64, full_url=full_url)
        except (httpx.HTTPError, WeixinIlinkError, ValueError) as exc:
            logger.warning("WeChat inbound %s download/decrypt failed: %s", kind, exc)
            return None

        file_name = str(sub.get("file_name") or "")
        if file_name:
            ext = Path(file_name).suffix.lower() or default_ext
        else:
            ext = default_ext
        mime = _EXT_TO_MIME.get(ext, "application/octet-stream")
        out_name = file_name or f"{default_name}-{uuid.uuid4().hex[:8]}{ext}"
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / out_name
        try:
            out_path.write_bytes(data)
        except OSError as exc:
            logger.warning("WeChat inbound %s save failed: %s", kind, exc)
            return None
        return InboundMedia(path=str(out_path), kind=kind, mime=mime, file_name=out_name)

    async def notify_start(self) -> None:
        try:
            await self._post("ilink/bot/msg/notifystart", {"base_info": self._base_info()})
        except httpx.HTTPError as exc:  # best-effort
            logger.debug("notifystart failed (ignored): %s", exc)

    async def notify_stop(self) -> None:
        try:
            await self._post(
                "ilink/bot/msg/notifystop",
                {"base_info": self._base_info()},
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as exc:  # best-effort
            logger.debug("notifystop failed (ignored): %s", exc)
