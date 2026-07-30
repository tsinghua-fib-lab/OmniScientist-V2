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
from omni.channels.outbound import DingTalkClient, send_presentation, uploadable_roots
from omni.channels.security import claim_inbound_message
from omni.runtime.notifications import TaskNotification
from omni.runtime.presentation import TaskPresentation, TurnPresentation

logger = logging.getLogger(__name__)


class DingTalkChannel(Channel):
    name = "dingtalk"

    def __init__(self, settings, agent, *, client=None) -> None:  # noqa: ANN001
        super().__init__(settings, agent)
        self._cfg = load_channel_config(settings, self.name)
        self._client = client or DingTalkClient(self._cfg)

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
                for msg in await self._client.poll_messages():
                    await self.handle_dingtalk_message(msg)
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
        stream = _build_dingtalk_stream(dingtalk_stream, client_id, client_secret, self.handle_dingtalk_message)
        result = stream.start()
        if hasattr(result, "__await__"):
            await result

    async def handle_dingtalk_message(self, event: Any) -> TurnPresentation | None:
        msg = _normalize_dingtalk_event(event)
        if not msg["text"] or not msg["target"]:
            return None
        if not claim_inbound_message(
            self.settings,
            self.name,
            msg["target"],
            msg["text"],
            message_id=msg.get("message_id", ""),
            event_id=msg.get("event_id", ""),
        ):
            return None
        return await self.handle_inbound_and_send(msg["text"], msg["target"])

    async def send_turn(self, external_key: str, presentation: TurnPresentation | TaskPresentation) -> None:
        return await send_presentation(
            self._client,
            external_key,
            presentation,
            allowed_roots=uploadable_roots(self.settings, artifacts=getattr(self.agent, "artifacts", None)),
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
    text = str(
        event.get("text", {}).get("content")
        if isinstance(event.get("text"), dict)
        else event.get("text") or event.get("content") or ""
    ).strip()
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
    out = {"text": text, "target": target}
    if message_id:
        out["message_id"] = message_id
    if event_id:
        out["event_id"] = event_id
    return out


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
                    await result
                return ok, "OK"
            except Exception as exc:  # pragma: no cover - defensive SDK boundary
                logger.exception("DingTalk chatbot callback failed.")
                return failed, str(exc)

    return OmniChatbotHandler()
