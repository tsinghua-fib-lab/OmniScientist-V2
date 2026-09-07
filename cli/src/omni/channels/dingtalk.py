"""DingTalk channel.

Recommended path: **Stream mode** via ``dingtalk-stream`` (outbound WebSocket,
no public webhook). Handle robot callbacks, run an agent turn, and reply with
interactive cards; push async task completions proactively. This mirrors the
heavily-optimised DingTalk path in the original HelixForge deployment.

Config (``~/.omni/channels/dingtalk.toml``): ``client_id``, ``client_secret``.
Stream mode uses the optional ``dingtalk-stream`` SDK when installed; webhook
or gateway mode can send Markdown directly.
"""

import asyncio
import logging
from typing import Any

from omni.channels.base import Channel
from omni.channels.config import load_channel_config
from omni.channels.inbound import (
    InboundFragment,
    MediaRef,
    SealedJob,
    download_fail_note,
    save_inbound_bytes,
)
from omni.channels.outbound import DingTalkClient, send_presentation
from omni.channels.security import (
    UNPAIRED_MEDIA_PLACEHOLDER,
    claim_inbound_message,
    inbound_event_seen,
    inbound_media_allowed,
)
from omni.runtime.notifications import TaskNotification
from omni.runtime.presentation import TaskPresentation, TurnPresentation

logger = logging.getLogger(__name__)


class DingTalkChannel(Channel):
    name = "dingtalk"

    def __init__(self, settings, agent, *, client=None) -> None:  # noqa: ANN001
        super().__init__(settings, agent)
        self._cfg = load_channel_config(settings, self.name)
        self._client = client or DingTalkClient(self._cfg)
        self._session_webhooks: dict[str, str] = {}
        self._sealed_webhooks: dict[str, str] = {}

    def _config_path(self):
        return self.settings.paths.channels_dir / "dingtalk.toml"

    async def start(self) -> None:
        cfg = self._config_path()
        if not cfg.is_file():
            logger.warning("DingTalk channel not configured. Create %s (client_id/client_secret).", cfg)
            return
        if str(self._cfg.get("mode") or "stream") == "gateway":
            interval = float(self._cfg.get("poll_interval_s") or 2)
            while True:
                await self._admit_dingtalk_batch(await self._client.poll_messages())
                await asyncio.sleep(max(0.5, interval))
        try:
            import dingtalk_stream  # type: ignore
        except ImportError:
            logger.warning("DingTalk stream mode needs 'dingtalk-stream'. Install it or use mode='gateway'.")
            return
        client_id = str(self._cfg.get("client_id") or "")
        client_secret = str(self._cfg.get("client_secret") or "")
        if not client_id or not client_secret:
            logger.warning("DingTalk channel requires client_id/client_secret.")
            return
        logger.info("DingTalk stream channel starting.")
        stream = _build_dingtalk_stream(dingtalk_stream, client_id, client_secret, self.admit_dingtalk_event)
        result = stream.start()
        if hasattr(result, "__await__"):
            await result

    async def handle_dingtalk_message(self, event: Any) -> TurnPresentation | None:
        fragment = await self.admit_dingtalk_event(event)
        if fragment is None:
            return None
        await self.inbound.flush_peer(fragment.peer_key())
        return await self.inbound.wait_peer(fragment.peer_key())

    async def admit_dingtalk_event(self, event: Any) -> InboundFragment | None:
        msg = _normalize_dingtalk_event(event)
        preview = self._dingtalk_preview(msg)
        if preview is None:
            return None
        if inbound_event_seen(
            self.settings,
            self.name,
            preview.conversation,
            message_id=preview.message_id,
            event_id=preview.event_id,
        ):
            return None
        if not inbound_media_allowed(self.settings, self.name, preview.conversation):
            return await self._admit_parsed_dingtalk(self._dingtalk_unpaired(preview))
        fragment = await self.inbound.materialize_and_admit(
            preview, lambda: self._materialize_dingtalk(msg)
        )
        return await self._claim_dingtalk(fragment)

    async def _admit_dingtalk_batch(self, events: Any) -> None:
        msgs = [event for event in (events or [])]
        if not msgs:
            return
        with self.inbound.batch():
            parsed = await asyncio.gather(
                *(self._parse_dingtalk_event(event) for event in msgs),
                return_exceptions=True,
            )
            for fragment in parsed:
                if isinstance(fragment, BaseException):
                    logger.error("DingTalk inbound parse failed", exc_info=fragment)
                    continue
                await self._admit_parsed_dingtalk(fragment)

    async def _admit_parsed_dingtalk(self, fragment: InboundFragment | None) -> InboundFragment | None:
        if fragment is None:
            return None
        if inbound_event_seen(
            self.settings,
            self.name,
            fragment.conversation,
            message_id=fragment.message_id,
            event_id=fragment.event_id,
        ):
            return None
        if not await self.inbound.admit(fragment):
            return None
        return await self._claim_dingtalk(fragment)

    async def _claim_dingtalk(self, fragment: InboundFragment | None) -> InboundFragment | None:
        if fragment is None:
            return None
        claim_inbound_message(
            self.settings,
            self.name,
            fragment.conversation,
            message_id=fragment.message_id,
            event_id=fragment.event_id,
        )
        return fragment

    def _dingtalk_preview(self, msg: dict[str, str]) -> InboundFragment | None:
        target = msg.get("target") or ""
        if not target:
            return None
        codes = [part for part in (msg.get("downloadCodes") or "").split("\n") if part]
        text = msg.get("text") or ""
        if not text and not codes:
            return None
        context: dict[str, str] = {}
        hook = msg.get("sessionWebhook") or ""
        if hook:
            context["sessionWebhook"] = hook
        return InboundFragment(
            channel=self.name,
            conversation=target,
            sender=msg.get("sender") or target,
            message_id=msg.get("message_id") or "",
            event_id=msg.get("event_id") or "",
            text=text,
            media=tuple(MediaRef(path="", kind="image") for _ in codes),
            reply_context=context,
        )

    def _dingtalk_unpaired(self, preview: InboundFragment) -> InboundFragment:
        return InboundFragment(
            channel=preview.channel,
            conversation=preview.conversation,
            sender=preview.sender,
            message_id=preview.message_id,
            event_id=preview.event_id,
            text=preview.text or UNPAIRED_MEDIA_PLACEHOLDER,
            media=(),
            reply_context=dict(preview.reply_context),
        )

    async def _parse_dingtalk_event(self, event: Any) -> InboundFragment | None:
        msg = _normalize_dingtalk_event(event)
        preview = self._dingtalk_preview(msg)
        if preview is None:
            return None
        if not inbound_media_allowed(self.settings, self.name, preview.conversation):
            return self._dingtalk_unpaired(preview)
        return await self._materialize_dingtalk(msg)

    async def _materialize_dingtalk(self, msg: dict[str, str]) -> InboundFragment | None:
        preview = self._dingtalk_preview(msg)
        if preview is None:
            return None
        refs, notes = await self._download_dingtalk_media(msg)
        text = "\n".join(filter(None, [preview.text, *notes]))
        if not text and not refs:
            return None
        return InboundFragment(
            channel=preview.channel,
            conversation=preview.conversation,
            sender=preview.sender,
            message_id=preview.message_id,
            event_id=preview.event_id,
            text=text,
            media=tuple(refs),
            reply_context=dict(preview.reply_context),
        )

    async def _download_dingtalk_media(self, msg: dict[str, str]) -> tuple[list[MediaRef], list[str]]:
        codes = [part for part in (msg.get("downloadCodes") or "").split("\n") if part]
        if not codes:
            return [], []
        from omni.core.user_inputs import ensure_inputs_dir

        dest = ensure_inputs_dir(self.settings.paths)
        fetch = getattr(self._client, "download_robot_media", None)
        refs: list[MediaRef] = []
        notes: list[str] = []
        for index, code in enumerate(codes):
            data = None
            if callable(fetch):
                try:
                    data = await fetch(code)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("DingTalk media download failed: %s", exc)
            if not isinstance(data, (bytes, bytearray)):
                notes.append(download_fail_note())
                continue
            path = save_inbound_bytes(
                dest,
                bytes(data),
                filename=f"dingtalk-{index + 1}.png",
                require_magic=True,
                existing_count=len(refs),
            )
            if path is None:
                notes.append(download_fail_note())
                continue
            refs.append(MediaRef(path=str(path), kind="image", file_name=path.name))
        return refs, notes

    def apply_sealed_reply(self, job: SealedJob) -> None:
        hook = str(job.reply_context.get("sessionWebhook") or "")
        if not hook:
            return
        self._session_webhooks[job.conversation] = hook
        self._sealed_webhooks[job.conversation] = hook

    def clear_sealed_reply(self, job: SealedJob) -> None:
        self._sealed_webhooks.pop(job.conversation, None)

    async def send_turn(self, external_key: str, presentation: TurnPresentation | TaskPresentation) -> None:
        target = (
            self._sealed_webhooks.get(external_key)
            or self._session_webhooks.get(external_key)
            or external_key
        )
        return await send_presentation(
            self._client,
            target,
            presentation,
            allowed_roots=self.uploadable_roots(),
        )

    async def notify(self, note: TaskNotification) -> None:
        await self.send_task_notification(note)


def _normalize_dingtalk_event(event: Any) -> dict[str, str]:
    if not isinstance(event, dict):
        if hasattr(event, "to_dict"):
            event = event.to_dict()
        else:
            event = getattr(event, "data", event)
            event = event if isinstance(event, dict) else getattr(event, "__dict__", {})
    text = _dingtalk_plain_text(event)
    target = str(
        event.get("conversationId")
        or event.get("conversation_id")
        or event.get("senderStaffId")
        or event.get("sender_id")
        or event.get("webhook_url")
        or ""
    )
    message_id = str(
        event.get("msgId")
        or event.get("messageId")
        or event.get("message_id")
        or event.get("msg_id")
        or ""
    )
    event_id = str(event.get("eventId") or event.get("event_id") or "")
    sender = str(event.get("senderStaffId") or event.get("sender_id") or "")
    hook = str(event.get("sessionWebhook") or event.get("session_webhook") or "")
    msgtype = str(event.get("msgtype") or event.get("msg_type") or "")
    extra_text, codes = _dingtalk_media_and_rich_text(event, msgtype)
    if extra_text and not text:
        text = extra_text
    elif extra_text:
        text = f"{text}\n{extra_text}".strip()
    out = {"text": text, "target": target}
    if message_id:
        out["message_id"] = message_id
    if event_id:
        out["event_id"] = event_id
    if sender:
        out["sender"] = sender
    if hook:
        out["sessionWebhook"] = hook
    if codes:
        out["downloadCodes"] = "\n".join(codes)
    return out


def _dingtalk_plain_text(event: dict[str, Any]) -> str:
    """User-visible text only. A content dict is media metadata, not a caption."""
    raw_text = event.get("text")
    if isinstance(raw_text, dict):
        return str(raw_text.get("content") or "").strip()
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()
    content = event.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def _dingtalk_media_and_rich_text(event: dict[str, Any], msgtype: str) -> tuple[str, list[str]]:
    content = event.get("content")
    codes: list[str] = []
    texts: list[str] = []
    if isinstance(content, dict):
        for key in ("downloadCode", "pictureDownloadCode"):
            value = str(content.get(key) or "").strip()
            if value:
                codes.append(value)
        rich = content.get("richText")
        if isinstance(rich, list):
            for part in rich:
                if not isinstance(part, dict):
                    continue
                piece = str(part.get("text") or "").strip()
                if piece:
                    texts.append(piece)
                for key in ("downloadCode", "pictureDownloadCode"):
                    value = str(part.get(key) or "").strip()
                    if value:
                        codes.append(value)
    if msgtype in {"picture", "image"} and not codes:
        picture = event.get("picture") if isinstance(event.get("picture"), dict) else {}
        for key in ("downloadCode", "pictureDownloadCode"):
            value = str(picture.get(key) or event.get(key) or "").strip()
            if value:
                codes.append(value)
    return "\n".join(texts), codes


def _build_dingtalk_stream(sdk: Any, client_id: str, client_secret: str, handler: Any) -> Any:
    # The Python SDK has changed names across releases; keep this adapter small
    # and duck-typed so current and older packages can both work.
    if hasattr(sdk, "DingTalkStreamClient"):
        credential = sdk.Credential(client_id, client_secret) if hasattr(sdk, "Credential") else (client_id, client_secret)
        client = sdk.DingTalkStreamClient(credential)
    elif hasattr(sdk, "DingTalkStream"):
        credential = sdk.Credential(client_id, client_secret) if hasattr(sdk, "Credential") else (client_id, client_secret)
        client = sdk.DingTalkStream(credential)
    else:
        raise RuntimeError("unsupported dingtalk_stream SDK version")
    if hasattr(client, "register_callback_handler"):
        topic = getattr(getattr(sdk, "ChatbotMessage", object), "TOPIC", "/v1.0/im/bot/messages/get")
        client.register_callback_handler(topic, _build_dingtalk_chatbot_handler(sdk, handler))
    elif hasattr(client, "on_message"):
        client.on_message(handler)
    return client


def _build_dingtalk_chatbot_handler(sdk: Any, handler: Any) -> Any:
    base = getattr(sdk, "ChatbotHandler", getattr(sdk, "CallbackHandler", object))
    ack = getattr(sdk, "AckMessage", None)
    ok = getattr(ack, "STATUS_OK", 200)
    failed = getattr(ack, "STATUS_SYSTEM_EXCEPTION", 500)
    message_cls = getattr(sdk, "ChatbotMessage", None)

    class OmniChatbotHandler(base):  # type: ignore[misc, valid-type]
        async def process(self, message: Any) -> tuple[int, str]:
            try:
                payload = getattr(message, "data", message)
                if message_cls is not None and isinstance(payload, dict):
                    payload = message_cls.from_dict(payload)
                result = handler(payload)
                if hasattr(result, "__await__"):
                    task = asyncio.create_task(result, name="dingtalk-inbound-admit")
                    task.add_done_callback(_log_dingtalk_admit)
                return ok, "OK"
            except Exception as exc:  # pragma: no cover - defensive SDK boundary
                logger.exception("DingTalk chatbot callback failed.")
                return failed, str(exc)

    return OmniChatbotHandler()


def _log_dingtalk_admit(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception("DingTalk inbound admit failed", exc_info=exc)
