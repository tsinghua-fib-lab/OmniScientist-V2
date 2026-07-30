"""Outbound IM helpers shared by WeChat / Feishu / DingTalk channels."""

from __future__ import annotations

import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from omni.runtime.presentation import (
    MAX_PRESENTED_ARTIFACTS,
    TaskPresentation,
    TurnPresentation,
    output_inventory,
)
from omni.runtime.task_results import is_dot_artifact
from omni.storage.artifacts import slugify_filename

logger = logging.getLogger(__name__)

# Matches a bare content-addressed stem (legacy ``<uuid-hex>`` artifact files)
# so we can substitute the human-readable title as the recipient-facing name.
# New artifacts are already named ``<slug>-<id8>`` and keep their basename.
_HASH_STEM = re.compile(r"^[0-9a-f]{16,}$")


class OutboundError(RuntimeError):
    pass


# The parts that carry the reply itself, as opposed to the files beside it.
# ``_message_part_kind`` picks between the first two; the rest are accepted so a
# hand-built envelope cannot silently be classified as an attachment.
_MESSAGE_PART_KINDS = frozenset({"rich_text", "plain_text", "text", "code"})
# The parts that upload a file and, on refusal, spend a second send on a text
# fallback — the pair a refusing peer must not be offered again in one reply.
_ATTACHMENT_PART_KINDS = frozenset({"file", "image"})
# ``MAX_PRESENTED_ARTIFACTS`` bounds each group, but one reply carries a group per
# task, so a request like "send me every file from today" once queued sixty
# uploads. WeChat refused the twelfth send and then every send to that peer for
# nine minutes, including an unrelated task's completion notice. One reply
# therefore ships at most one inventory's worth of files; the text still names
# the rest, and `/task show` has all of them.
MAX_DELIVERED_ATTACHMENTS = MAX_PRESENTED_ARTIFACTS


@dataclass(frozen=True)
class DeliveryPart:
    kind: str
    text: str = ""
    title: str = ""
    path: str = ""
    uri: str = ""
    mime: str = ""
    format: str = ""


@dataclass(frozen=True)
class DeliveryEnvelope:
    parts: list[DeliveryPart] = field(default_factory=list)


@dataclass(frozen=True)
class DeliveryPartResult:
    kind: str
    status: str
    title: str = ""
    message: str = ""


@dataclass(frozen=True)
class DeliveryReport:
    target: str
    parts: list[DeliveryPartResult] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """Whether the reply itself never reached the recipient.

        An attachment that would not upload is not a failed delivery. The
        distinction decides whether the *task* is failed: WeChat refused two
        figure uploads on task 964f17aa after answering the question in full, and
        the run was recorded as failed — the researcher had their answer on
        screen while the record said the work did not finish. An artifact that
        did not send still leaves the link the fallback wrote, and the file
        itself on disk; an answer that did not send leaves nothing.
        """
        return any(
            part.status == "failed" and part.kind in _MESSAGE_PART_KINDS
            for part in self.parts
        )

    @property
    def degraded(self) -> bool:
        """Whether anything arrived as less than it should have."""
        return any(
            part.status == "degraded"
            or (part.status == "failed" and part.kind not in _MESSAGE_PART_KINDS)
            for part in self.parts
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "parts": [
                {
                    "kind": part.kind,
                    "status": part.status,
                    "title": part.title,
                    "message": part.message,
                }
                for part in self.parts
            ],
        }


class MarkdownOutbound:
    async def send_markdown(self, target: str, markdown: str) -> None:  # pragma: no cover - protocol-like
        raise NotImplementedError

    async def send_rich_text(self, target: str, markdown: str) -> None:
        await self.send_markdown(target, markdown)

    async def send_text(self, target: str, text: str) -> None:
        await self.send_markdown(target, text)

    async def poll_messages(self) -> list[dict[str, Any]]:
        return []


async def send_presentation(
    client: MarkdownOutbound,
    target: str,
    presentation: TurnPresentation | TaskPresentation,
    *,
    allowed_roots: list[str | Path] | None = None,
) -> DeliveryReport:
    return await send_delivery(client, target, delivery_envelope_from_presentation(presentation), allowed_roots=allowed_roots)


def uploadable_roots(settings: Any, *, artifacts: Any = None, mirror_dir: Path | str | None = None) -> list[Path]:
    """Directories an IM channel may upload artifact files from.

    Single source of truth for every channel's ``send_presentation`` allow-list,
    so a future change to *where deliverables land* only needs updating here.

    Always includes the durable workspace store (``paths.artifacts_dir``). When a
    trusted launch directory is active, deliverables are written there as the
    single canonical copy (ArtifactStore single-copy / ``mirror_dir``), so that
    resolved output directory must be allowed too — otherwise ``_safe_outbound_file``
    rejects the path and native file/image uploads silently degrade to text
    links. Pass the agent's ``ArtifactStore`` (``artifacts``, whose ``mirror_dir``
    is already resolved to the launch/output dir) or an explicit ``mirror_dir``;
    both are optional and the result is de-duplicated by resolved path.
    """
    candidates: list[Path] = []
    paths = getattr(settings, "paths", None)
    art_dir = getattr(paths, "artifacts_dir", None)
    if art_dir:
        candidates.append(Path(art_dir))
    resolved_mirror = mirror_dir if mirror_dir is not None else getattr(artifacts, "mirror_dir", None)
    if resolved_mirror:
        candidates.append(Path(resolved_mirror))
    seen: set[str] = set()
    roots: list[Path] = []
    for root in candidates:
        try:
            key = str(Path(root).resolve())
        except OSError:
            key = str(root)
        if key not in seen:
            seen.add(key)
            roots.append(Path(root))
    return roots


def delivery_envelope_from_presentation(presentation: TurnPresentation | TaskPresentation) -> DeliveryEnvelope:
    # Artifact bullets still name the file (see ``_chat_artifact_line``); the
    # upload is the copy the recipient can open. ``include_local_paths=False``
    # keeps the research-ledger block and CLI follow-up menus off a chat card.
    markdown = presentation.to_markdown(include_local_paths=False)
    parts = [DeliveryPart(kind=_message_part_kind(markdown), text=markdown, title="OmniScientist")]
    seen_paths: set[str] = set()
    # A result payload often names the same artifact twice: once as a titled
    # deliverable with a path and once as a bare ``*_uri`` field. Delivering both
    # spends an extra send on a line of ``artifact://`` text for a file the reader
    # already has attached.
    seen_uris: set[str] = set()
    attached = 0
    # CLI Outputs and IM attachments share one inventory. Walking task cards
    # as a second group made WeChat receive files the terminal never listed.
    for artifact in output_inventory(presentation):
        if attached >= MAX_DELIVERED_ATTACHMENTS:
            break
        if artifact.path and artifact.path in seen_paths:
            continue
        if artifact.uri and artifact.uri in seen_uris:
            continue
        part = _artifact_part(artifact)
        if part is not None:
            parts.append(part)
            attached += 1
            if artifact.path:
                seen_paths.add(artifact.path)
            if artifact.uri:
                seen_uris.add(artifact.uri)
    return DeliveryEnvelope(parts=parts)


async def send_delivery(
    client: MarkdownOutbound,
    target: str,
    envelope: DeliveryEnvelope,
    *,
    allowed_roots: list[str | Path] | None = None,
) -> DeliveryReport:
    results: list[DeliveryPartResult] = []
    refused = False
    for part in envelope.parts:
        if refused and part.kind in _ATTACHMENT_PART_KINDS:
            # An upload whose text fallback *also* failed means the far side is
            # refusing this peer outright, not objecting to one file. Each
            # further attachment then costs two more sends and lengthens the very
            # burst being refused, so stop and let the retry queue hand these
            # over once the window closes. A message part that failed on its own
            # says nothing about uploads, and does not stop them.
            results.append(DeliveryPartResult(
                kind=part.kind,
                status="failed",
                title=part.title,
                message="not attempted: the channel refused an earlier message in this reply",
            ))
            continue
        try:
            results.append(await _send_delivery_part(client, target, part, allowed_roots=allowed_roots))
        except Exception as exc:  # noqa: BLE001
            logger.warning("delivery part send failed for %s (%s): %s", target, part.kind, exc)
            refused = refused or part.kind in _ATTACHMENT_PART_KINDS
            results.append(DeliveryPartResult(
                kind=part.kind,
                status="failed",
                title=part.title,
                message=str(exc),
            ))
    return DeliveryReport(target=target, parts=results)


async def _send_delivery_part(
    client: MarkdownOutbound,
    target: str,
    part: DeliveryPart,
    *,
    allowed_roots: list[str | Path] | None = None,
) -> DeliveryPartResult:
    if part.kind == "rich_text":
        try:
            await _client_send_rich_text(client, target, part.text)
            return DeliveryPartResult(kind=part.kind, status="sent", title=part.title)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rich text send failed for %s; falling back to plain text: %s", target, exc)
            await _client_send_text(client, target, part.text)
            return DeliveryPartResult(
                kind=part.kind,
                status="degraded",
                title=part.title,
                message=f"rich_text fallback to plain_text: {exc}",
            )
    if part.kind in {"plain_text", "code"}:
        await _client_send_text(client, target, part.text)
        return DeliveryPartResult(kind=part.kind, status="sent", title=part.title)
    if part.kind == "image":
        return await _send_file_part(client, target, part, method="send_image", allowed_roots=allowed_roots)
    if part.kind == "file":
        return await _send_file_part(client, target, part, method="send_file", allowed_roots=allowed_roots)
    if part.kind == "link":
        await _client_send_text(client, target, f"{part.title or 'Artifact'}: {part.uri}")
        return DeliveryPartResult(kind=part.kind, status="sent", title=part.title)
    return DeliveryPartResult(kind=part.kind, status="failed", title=part.title, message="unknown delivery part")


async def _send_file_part(
    client: MarkdownOutbound,
    target: str,
    part: DeliveryPart,
    *,
    method: str,
    allowed_roots: list[str | Path] | None = None,
) -> DeliveryPartResult:
    safe_file = bool(part.path and _safe_outbound_file(part.path, allowed_roots=allowed_roots))
    if not safe_file or not hasattr(client, method):
        await _client_send_text(client, target, _file_fallback_text(part, include_local_path=allowed_roots is None and safe_file))
        return DeliveryPartResult(
            kind=part.kind,
            status="degraded",
            title=part.title,
            message="file unavailable, outside allowed artifact roots, or channel lacks file API; sent text fallback",
        )
    try:
        send = getattr(client, method)
        display_name = _display_filename(part)
        if display_name and _accepts_file_name(send):
            await send(target, part.path, file_name=display_name)
        else:
            await send(target, part.path)
        return DeliveryPartResult(kind=part.kind, status="sent", title=part.title)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s failed for %s; falling back to text: %s", method, part.path, exc)
        await _client_send_text(client, target, _file_fallback_text(part, include_local_path=allowed_roots is None))
        return DeliveryPartResult(
            kind=part.kind,
            status="degraded",
            title=part.title,
            message=f"{method} fallback to text: {exc}",
        )


async def _send_artifact(client: MarkdownOutbound, target: str, artifact: Any) -> None:
    part = _artifact_part(artifact)
    if part is not None:
        await _send_delivery_part(client, target, part)


async def _client_send_rich_text(client: MarkdownOutbound, target: str, markdown: str) -> None:
    method = getattr(client, "send_rich_text", None)
    if callable(method):
        await method(target, markdown)
        return
    await client.send_markdown(target, markdown)


async def _client_send_text(client: MarkdownOutbound, target: str, text: str) -> None:
    method = getattr(client, "send_text", None)
    if callable(method):
        await method(target, text)
        return
    await client.send_markdown(target, text)


class WeChatGatewayClient(MarkdownOutbound):
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.base_url = str(cfg.get("base_url") or cfg.get("gateway_url") or "http://127.0.0.1:8088").rstrip("/")
        self.inbox_path = str(cfg.get("inbox_path") or "/messages")
        self.send_path = str(cfg.get("send_path") or "/send")
        self.timeout = float(cfg.get("timeout_s") or 10.0)

    async def poll_messages(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            res = await client.get(self.base_url + self.inbox_path)
            res.raise_for_status()
            data = res.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return list(data.get("messages") or data.get("items") or [])
        return []

    async def send_markdown(self, target: str, markdown: str) -> None:
        if _prefer_plain_text(markdown):
            await self.send_text(target, markdown)
            return
        await self.send_rich_text(target, markdown)

    async def send_rich_text(self, target: str, markdown: str) -> None:
        await self._post_send({"to": target, "text": markdown, "markdown": markdown, "type": "markdown"})

    async def send_text(self, target: str, text: str) -> None:
        await self._post_send({"to": target, "text": _plain_text_markdown(text) or "-", "type": "text"})

    async def send_file(self, target: str, path: str, *, file_name: str | None = None) -> None:
        await self._post_send(_file_payload(target, path, kind="file", file_name=file_name))

    async def send_image(self, target: str, path: str, *, file_name: str | None = None) -> None:
        await self._post_send(_file_payload(target, path, kind="image", file_name=file_name))

    async def _post_send(self, payload: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            res = await client.post(self.base_url + self.send_path, json=payload)
            res.raise_for_status()


class WeixinIlinkOutbound(MarkdownOutbound):
    """Adapt :class:`~omni.channels.weixin_ilink.WeixinIlinkClient` to the outbound
    protocol so ``send_presentation`` works for the WeChat iLink channel.

    The iLink ``sendmessage`` API must echo the per-peer ``context_token`` last
    seen on inbound, so the channel passes its live ``{external_key: token}`` map
    here (by reference). Image/file artifacts are encrypted and uploaded to the
    Weixin CDN via the client (``send_image``/``send_file``); the shared delivery
    helpers fall back to a text link if upload fails or ``cryptography`` is absent.
    """

    def __init__(self, client: Any, context_tokens: dict[str, str]) -> None:
        self._client = client
        self._context_tokens = context_tokens

    async def send_markdown(self, target: str, markdown: str) -> None:
        await self._client.send_message(
            target, markdown, context_token=self._context_tokens.get(target)
        )

    async def send_image(self, target: str, path: str, *, file_name: str | None = None) -> None:
        await self._client.send_image(
            target, path, context_token=self._context_tokens.get(target)
        )

    async def send_file(self, target: str, path: str, *, file_name: str | None = None) -> None:
        await self._client.send_file(
            target,
            path,
            file_name=file_name or Path(path).name,
            context_token=self._context_tokens.get(target),
        )


class FeishuClient(MarkdownOutbound):
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.base_url = str(cfg.get("base_url") or "https://open.feishu.cn").rstrip("/")
        self.timeout = float(cfg.get("timeout_s") or 15.0)
        self._tenant_token = ""

    async def send_markdown(self, target: str, markdown: str) -> None:
        if _prefer_plain_text(markdown):
            await self.send_text(target, markdown)
            return
        await self.send_rich_text(target, markdown)

    async def send_rich_text(self, target: str, markdown: str) -> None:
        webhook = str(self.cfg.get("webhook_url") or "")
        if webhook:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.post(webhook, json={"msg_type": "post", "content": _feishu_webhook_post_content(markdown)})
                try:
                    _raise_for_status(res, "Feishu webhook post send")
                    return
                except OutboundError as exc:
                    logger.warning("Feishu webhook post send failed; falling back to text: %s", exc)
                res = await client.post(webhook, json={"msg_type": "text", "content": _feishu_text_content(markdown)})
                _raise_for_status(res, "Feishu webhook text send")
            return
        try:
            await self._send_message(target, "post", _feishu_post_content(markdown))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Feishu post send failed; falling back to text: %s", exc)
            await self._send_message(target, "text", _feishu_text_content(markdown))

    async def send_text(self, target: str, text: str) -> None:
        webhook = str(self.cfg.get("webhook_url") or "")
        if webhook:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.post(webhook, json={"msg_type": "text", "content": _feishu_text_content(text)})
                _raise_for_status(res, "Feishu webhook text send")
            return
        await self._send_message(target, "text", _feishu_text_content(text))

    async def send_file(self, target: str, path: str, *, file_name: str | None = None) -> None:
        file_key = await self._upload_file(path, file_name=file_name)
        await self._send_message(target, "file", {"file_key": file_key})

    async def send_image(self, target: str, path: str, *, file_name: str | None = None) -> None:
        image_key = await self._upload_image(path)
        await self._send_message(target, "image", {"image_key": image_key})

    async def _token(self) -> str:
        if self._tenant_token:
            return self._tenant_token
        app_id = str(self.cfg.get("app_id") or "")
        app_secret = str(self.cfg.get("app_secret") or "")
        if not app_id or not app_secret:
            raise OutboundError("Feishu requires webhook_url or app_id/app_secret")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            res = await client.post(
                self.base_url + "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            _raise_for_status(res, "Feishu tenant token request")
            data = res.json()
        token = str(data.get("tenant_access_token") or "")
        if not token:
            raise OutboundError(f"Feishu token response missing tenant_access_token: {data}")
        self._tenant_token = token
        return token

    async def _send_message(self, target: str, msg_type: str, content: dict[str, Any]) -> None:
        token = await self._token()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            res = await client.post(
                self.base_url + "/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": target, "msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)},
            )
            _raise_for_status(res, f"Feishu {msg_type} message send")

    async def _upload_file(self, path: str, *, file_name: str | None = None) -> str:
        token = await self._token()
        p = Path(path)
        display = file_name or p.name
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            with p.open("rb") as fh:
                res = await client.post(
                    self.base_url + "/open-apis/im/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file_type": "stream", "file_name": display},
                    files={"file": (display, fh, "application/octet-stream")},
                )
            _raise_for_status(res, "Feishu file upload")
            data = res.json().get("data") or {}
        key = str(data.get("file_key") or "")
        if not key:
            raise OutboundError("Feishu file upload did not return file_key")
        return key

    async def _upload_image(self, path: str) -> str:
        token = await self._token()
        p = Path(path)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            with p.open("rb") as fh:
                res = await client.post(
                    self.base_url + "/open-apis/im/v1/images",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"image_type": "message"},
                    files={"image": (p.name, fh, "application/octet-stream")},
                )
            _raise_for_status(res, "Feishu image upload")
            data = res.json().get("data") or {}
        key = str(data.get("image_key") or "")
        if not key:
            raise OutboundError("Feishu image upload did not return image_key")
        return key


# A DingTalk robot webhook carries text, markdown, link and cards — there is no
# media call on it, so a file needs the configured gateway. Printing the server
# path instead used to look like a delivery and be recorded as one: the recipient
# got a directory on somebody else's machine, and the report said "sent".
_DINGTALK_NO_UPLOAD = (
    "DingTalk cannot upload a file over a robot webhook; set base_url (or "
    "gateway_url) in dingtalk.toml to deliver artifacts as attachments"
)


class DingTalkClient(MarkdownOutbound):
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.timeout = float(cfg.get("timeout_s") or 15.0)

    async def send_markdown(self, target: str, markdown: str) -> None:
        if _prefer_plain_text(markdown):
            await self.send_text(target, markdown)
            return
        await self.send_rich_text(target, markdown)

    async def send_rich_text(self, target: str, markdown: str) -> None:
        if self._gateway_enabled:
            await self._post_gateway({"to": target, "text": markdown, "markdown": markdown, "type": "markdown"})
            return
        webhook = str(self.cfg.get("webhook_url") or target)
        if not webhook.startswith("http"):
            raise OutboundError("DingTalk proactive send requires webhook_url in config or callback target")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            res = await client.post(
                webhook,
                json={
                    "msgtype": "markdown",
                    "markdown": {"title": "OmniScientist", "text": markdown},
                },
            )
            res.raise_for_status()

    async def send_text(self, target: str, text: str) -> None:
        plain = _plain_text_markdown(text) or "-"
        if self._gateway_enabled:
            await self._post_gateway({"to": target, "text": plain, "type": "text"})
            return
        webhook = str(self.cfg.get("webhook_url") or target)
        if not webhook.startswith("http"):
            raise OutboundError("DingTalk proactive send requires webhook_url in config or callback target")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            res = await client.post(
                webhook,
                json={
                    "msgtype": "text",
                    "text": {"content": plain},
                },
            )
            res.raise_for_status()

    async def send_file(self, target: str, path: str, *, file_name: str | None = None) -> None:
        if not self._gateway_enabled:
            raise OutboundError(_DINGTALK_NO_UPLOAD)
        await self._post_gateway(_file_payload(target, path, kind="file", file_name=file_name))

    async def send_image(self, target: str, path: str, *, file_name: str | None = None) -> None:
        if not self._gateway_enabled:
            raise OutboundError(_DINGTALK_NO_UPLOAD)
        await self._post_gateway(_file_payload(target, path, kind="image", file_name=file_name))

    @property
    def _gateway_enabled(self) -> bool:
        return bool(self.cfg.get("base_url") or self.cfg.get("gateway_url"))

    async def _post_gateway(self, payload: dict[str, Any]) -> None:
        base_url = str(self.cfg.get("base_url") or self.cfg.get("gateway_url") or "").rstrip("/")
        send_path = str(self.cfg.get("send_path") or "/send")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            res = await client.post(base_url + send_path, json=payload)
            res.raise_for_status()


_ALLOWED_OUTBOUND_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".dot",
    ".gif",
    ".bib",
    ".mmd",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".svg",
    ".tex",
    ".txt",
    ".webp",
    ".xls",
    ".xlsx",
}
_MAX_OUTBOUND_FILE_BYTES = 50 * 1024 * 1024
_RASTER_IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
_RASTER_IMAGE_FORMATS = {ext.removeprefix(".") for ext in _RASTER_IMAGE_EXTENSIONS}
_RASTER_IMAGE_MIMES = {"image/gif", "image/jpeg", "image/png", "image/webp"}


def _message_part_kind(markdown: str) -> str:
    return "plain_text" if _prefer_plain_text(markdown) else "rich_text"


def _prefer_plain_text(markdown: str) -> bool:
    text = markdown.strip()
    if not text:
        return False
    lower = text.lower()
    if "```" in text or "```mermaid" in lower:
        return True
    if len(text) > 3000:
        return True
    lines = text.splitlines()
    if len(lines) > 60:
        return True
    if any(len(line) > 600 for line in lines):
        return True
    return False


def _artifact_part(artifact: Any) -> DeliveryPart | None:
    if is_dot_artifact(artifact):
        return None
    path = str(getattr(artifact, "path", "") or "")
    uri = str(getattr(artifact, "uri", "") or "")
    title = str(getattr(artifact, "title", "") or "artifact")
    fmt = str(getattr(artifact, "format", "") or "")
    mime = str(getattr(artifact, "mime", "") or "")
    if path:
        kind = "image" if _prefer_image_upload(artifact, path) else "file"
        return DeliveryPart(kind=kind, title=title, path=path, uri=uri, mime=mime, format=fmt)
    if uri:
        return DeliveryPart(kind="link", title=title, uri=uri, mime=mime, format=fmt)
    return None


def _accepts_file_name(method: Any) -> bool:
    """True when an outbound ``send_file``/``send_image`` accepts ``file_name``.

    Lets us pass a recipient-facing name to Omni's own clients while staying
    backward-compatible with the 2-arg ``(target, path)`` outbound protocol that
    third-party channel plugins may implement.
    """
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    return "file_name" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _display_filename(part: DeliveryPart) -> str:
    """Recipient-facing filename for an artifact upload.

    New artifacts already carry a semantic on-disk basename (``<slug>-<id8>``)
    and keep it. Legacy content-addressed files (``<uuid-hex>.<ext>``) would
    otherwise reach the user as an opaque hash, so we substitute a slug derived
    from the artifact title, preserving the real extension.
    """
    source = part.path or part.uri
    if not source:
        return ""
    p = Path(source)
    stem, suffix = p.stem, p.suffix
    if suffix and not _HASH_STEM.match(stem):
        return p.name  # already human-readable on disk
    ext = suffix.lstrip(".") or (part.format or "").lstrip(".")
    slug = slugify_filename(part.title) or "artifact"
    if ext and slug.lower().endswith(f"-{ext.lower()}"):
        slug = slug[: -(len(ext) + 1)] or "artifact"
    return f"{slug}.{ext}" if ext else slug


def _file_fallback_text(part: DeliveryPart, *, include_local_path: bool = True) -> str:
    """Name a file that could not be attached, without quoting where it lives.

    Reached for two unrelated reasons — the channel has no way to upload, or the
    upload was refused — and neither is the recipient's to act on. What they can
    act on is the filename: it is what the artifact is called everywhere else, so
    it is what makes the file findable from the task it belongs to.
    """
    target = part.uri or (part.path if include_local_path else "")
    label = "Image" if part.kind == "image" else "File"
    name = Path(part.path).name if part.path else ""
    title = part.title or name or "artifact"
    if not target:
        detail = f"{name}\n" if name and name != title else ""
        return (
            f"{label} artifact: {title}\n{detail}"
            "It could not be attached on this channel; it is stored with its task."
        )
    return f"{label} artifact: {title}\n{target}"


def _safe_outbound_file(path: str, *, allowed_roots: list[str | Path] | None = None) -> bool:
    if not path:
        return False
    p = Path(path)
    try:
        if not p.is_file():
            return False
        if p.suffix.lower() not in _ALLOWED_OUTBOUND_EXTENSIONS:
            return False
        if allowed_roots is not None:
            resolved = p.resolve()
            roots = [Path(root).resolve() for root in allowed_roots if root]
            if not any(_is_relative_to(resolved, root) for root in roots):
                return False
        return p.stat().st_size <= _MAX_OUTBOUND_FILE_BYTES
    except OSError:
        return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _prefer_image_upload(artifact: Any, path: str) -> bool:
    fmt = str(getattr(artifact, "format", "") or "").lower().lstrip(".")
    mime = str(getattr(artifact, "mime", "") or "").lower()
    suffix = Path(path).suffix.lower()
    return (
        suffix in _RASTER_IMAGE_EXTENSIONS
        or fmt in _RASTER_IMAGE_FORMATS
        or mime in _RASTER_IMAGE_MIMES
    )


def _file_payload(target: str, path: str, *, kind: str, file_name: str | None = None) -> dict[str, Any]:
    name = file_name or Path(path).name
    return {
        "to": target,
        "type": kind,
        "path": path,
        "file_path": path,
        "name": name,
        "file_name": name,
        "mime": _guess_mime(path, kind=kind),
    }


def _guess_mime(path: str, *, kind: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".svg":
        return "image/svg+xml"
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".gif":
        return "image/gif"
    if suffix == ".webp":
        return "image/webp"
    if suffix in {".txt", ".md", ".mmd", ".dot", ".tex", ".bib"}:
        return "text/plain"
    return "application/octet-stream" if kind == "file" else "image/*"


def _raise_for_status(res: httpx.Response, action: str) -> None:
    try:
        res.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = _response_body_snippet(res)
        raise OutboundError(
            f"{action} failed: HTTP {res.status_code} for {res.request.url}; body={body}"
        ) from exc


def _response_body_snippet(res: httpx.Response, *, limit: int = 1000) -> str:
    text = res.text
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _feishu_text_content(markdown: str) -> dict[str, Any]:
    text = _plain_text_markdown(markdown) or "-"
    return {"text": text}


def _feishu_post_content(markdown: str) -> dict[str, Any]:
    """Rich-text (``post``) body for the ``im/v1/messages`` send-message API.

    Returns the ``{"zh_cn": {...}}`` structure the *send-message* API expects as
    its ``content``. This must **not** be wrapped in an extra ``{"post": ...}``
    envelope — that wrapper is only for the custom-bot **webhook** payload
    (see :func:`_feishu_webhook_post_content`). Sending the wrapped form to
    ``im/v1/messages`` is rejected by Feishu with ``400 / code 230001
    "invalid message content"`` and the reply silently degrades to plain text.
    """
    lines = _plain_text_markdown(markdown).splitlines() or [""]
    content = [
        [{"tag": "text", "text": line if line.strip() else " "}] for line in lines
    ]
    return {"zh_cn": {"title": "OmniScientist", "content": content}}


def _feishu_webhook_post_content(markdown: str) -> dict[str, Any]:
    """Rich-text body for the custom-bot **webhook**, wrapped under ``post``."""
    return {"post": _feishu_post_content(markdown)}


def _plain_text_markdown(markdown: str) -> str:
    lines = []
    in_fence = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = line.replace("**", "").replace("__", "")
        line = line.replace("`", "")
        if in_fence and line:
            line = "  " + line
        lines.append(line)
    return "\n".join(lines).strip()
