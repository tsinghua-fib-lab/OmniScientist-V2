"""Feishu / Lark channel.

Recommended path: **WebSocket long connection** via the official ``lark-oapi``
SDK (no public callback URL needed). Subscribe to ``im.message.receive_v1``,
run an agent turn, and reply with the message-send API. Async task
completions are pushed proactively to the originating chat.

Config (``~/.omni/channels/feishu.toml``): ``app_id``, ``app_secret``.

WS mode uses the optional official SDK when installed; gateway mode uses the
same poll/send contract as tests and local adapters. Outbound messages use
Feishu REST/webhook APIs.
"""

import asyncio
import inspect
import json
import logging
import re
import threading
from typing import Any

from omni.channels.base import Channel
from omni.channels.config import load_channel_config
from omni.channels.inbound import InboundFragment, MediaRef, download_fail_note, save_inbound_bytes
from omni.channels.outbound import FeishuClient, send_presentation
from omni.channels.security import (
    UNPAIRED_MEDIA_PLACEHOLDER,
    claim_inbound_message,
    inbound_event_seen,
    inbound_media_allowed,
)
from omni.runtime.notifications import TaskNotification
from omni.runtime.presentation import TaskPresentation, TurnPresentation

logger = logging.getLogger(__name__)


class FeishuChannel(Channel):
    name = "feishu"

    def __init__(self, settings, agent, *, client=None) -> None:  # noqa: ANN001
        super().__init__(settings, agent)
        self._cfg = load_channel_config(settings, self.name)
        self._client = client or FeishuClient(self._cfg)

    def _config_path(self):
        return self.settings.paths.channels_dir / "feishu.toml"

    async def start(self) -> None:
        cfg = self._config_path()
        if not cfg.is_file():
            logger.warning("Feishu channel not configured. Create %s (app_id/app_secret).", cfg)
            return
        if str(self._cfg.get("mode") or "ws") == "gateway":
            interval = float(self._cfg.get("poll_interval_s") or 2)
            while True:
                await self._admit_feishu_batch(await self._client.poll_messages())
                await asyncio.sleep(max(0.5, interval))
        app_id = str(self._cfg.get("app_id") or "")
        app_secret = str(self._cfg.get("app_secret") or "")
        if not app_id or not app_secret:
            logger.warning("Feishu channel requires app_id/app_secret.")
            return
        logger.info("Feishu WS channel starting.")
        loop = asyncio.get_running_loop()
        handle = _start_lark_channel_thread(app_id, app_secret, loop, self.admit_feishu_event)
        try:
            await _wait_lark_channel_ready(handle)
            logger.info("Feishu WS channel connected.")
            while True:
                await asyncio.sleep(3600)
        finally:
            await _stop_lark_channel_thread(handle)

    async def handle_feishu_message(self, event: Any) -> TurnPresentation | None:
        fragment = await self.admit_feishu_event(event)
        if fragment is None:
            return None
        await self.inbound.flush_peer(fragment.peer_key())
        return await self.inbound.wait_peer(fragment.peer_key())

    async def admit_feishu_event(self, event: Any) -> InboundFragment | None:
        msg = _normalize_feishu_event(event)
        preview = self._feishu_preview(msg)
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
            return await self._admit_parsed_feishu(self._feishu_unpaired(preview))
        fragment = await self.inbound.materialize_and_admit(
            preview, lambda: self._materialize_feishu(msg)
        )
        return await self._claim_feishu(fragment)

    async def _admit_feishu_batch(self, events: Any) -> None:
        msgs = [event for event in (events or [])]
        if not msgs:
            return
        with self.inbound.batch():
            parsed = await asyncio.gather(
                *(self._parse_feishu_event(event) for event in msgs),
                return_exceptions=True,
            )
            for fragment in parsed:
                if isinstance(fragment, BaseException):
                    logger.error("Feishu inbound parse failed", exc_info=fragment)
                    continue
                await self._admit_parsed_feishu(fragment)

    async def _admit_parsed_feishu(self, fragment: InboundFragment | None) -> InboundFragment | None:
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
        return await self._claim_feishu(fragment)

    async def _claim_feishu(self, fragment: InboundFragment | None) -> InboundFragment | None:
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

    def _feishu_preview(self, msg: dict[str, str]) -> InboundFragment | None:
        chat_id = msg.get("chat_id") or ""
        if not chat_id:
            return None
        keys = _feishu_media_keys(msg)
        text = msg.get("text") or ""
        if not text and not keys:
            return None
        return InboundFragment(
            channel=self.name,
            conversation=chat_id,
            sender=msg.get("sender") or chat_id,
            thread=msg.get("thread") or "",
            message_id=msg.get("message_id") or "",
            event_id=msg.get("event_id") or "",
            text=text,
            media=tuple(MediaRef(path="", kind=kind) for kind, _key, _name in keys),
        )

    def _feishu_unpaired(self, preview: InboundFragment) -> InboundFragment:
        return InboundFragment(
            channel=preview.channel,
            conversation=preview.conversation,
            sender=preview.sender,
            thread=preview.thread,
            message_id=preview.message_id,
            event_id=preview.event_id,
            text=preview.text or UNPAIRED_MEDIA_PLACEHOLDER,
            media=(),
        )

    async def _parse_feishu_event(self, event: Any) -> InboundFragment | None:
        msg = _normalize_feishu_event(event)
        preview = self._feishu_preview(msg)
        if preview is None:
            return None
        if not inbound_media_allowed(self.settings, self.name, preview.conversation):
            return self._feishu_unpaired(preview)
        return await self._materialize_feishu(msg)

    async def _materialize_feishu(self, msg: dict[str, str]) -> InboundFragment | None:
        preview = self._feishu_preview(msg)
        if preview is None:
            return None
        refs, notes = await self._download_feishu_media(msg)
        text = "\n".join(filter(None, [preview.text, *notes]))
        if not text and not refs:
            return None
        return InboundFragment(
            channel=preview.channel,
            conversation=preview.conversation,
            sender=preview.sender,
            thread=preview.thread,
            message_id=preview.message_id,
            event_id=preview.event_id,
            text=text,
            media=tuple(refs),
        )

    async def _download_feishu_media(self, msg: dict[str, str]) -> tuple[list[MediaRef], list[str]]:
        keys = _feishu_media_keys(msg)
        if not keys:
            return [], []
        from omni.core.user_inputs import ensure_inputs_dir

        dest = ensure_inputs_dir(self.settings.paths)
        refs: list[MediaRef] = []
        notes: list[str] = []
        message_id = msg.get("message_id") or ""
        for kind, key, filename in keys:
            data = await self._fetch_feishu_bytes(kind, key, message_id=message_id)
            if data is None:
                notes.append(download_fail_note())
                continue
            name = filename or f"{kind}-{key[-8:]}.{'png' if kind == 'image' else 'bin'}"
            path = save_inbound_bytes(
                dest,
                data,
                filename=name,
                require_magic=kind == "image",
                existing_count=len(refs),
            )
            if path is None:
                notes.append(download_fail_note())
                continue
            refs.append(MediaRef(path=str(path), kind=kind, file_name=path.name))
        return refs, notes

    async def _fetch_feishu_bytes(self, kind: str, key: str, *, message_id: str = "") -> bytes | None:
        resource = getattr(self._client, "download_message_resource", None)
        fetch = getattr(
            self._client, "download_image" if kind == "image" else "download_file", None
        )
        try:
            if message_id and callable(resource):
                data = await resource(message_id, key, resource_type=kind)
            elif callable(fetch):
                try:
                    data = await fetch(key, message_id=message_id)
                except TypeError:
                    data = await fetch(key)
            else:
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Feishu %s download failed: %s", kind, exc)
            return None
        return data if isinstance(data, (bytes, bytearray)) else None

    async def send_turn(self, external_key: str, presentation: TurnPresentation | TaskPresentation) -> None:
        return await send_presentation(
            self._client,
            external_key,
            presentation,
            allowed_roots=self.uploadable_roots(),
        )

    async def notify(self, note: TaskNotification) -> None:
        await self.send_task_notification(note)


_FEISHU_MEDIA_PLACEHOLDER = re.compile(
    r"^!\[(?:image|file|audio|video|sticker)\]\([^)]*\)$"
)
_FEISHU_MEDIA_TOKEN = frozenset({"[image]", "[file]", "[audio]", "[video]", "[sticker]"})


def _normalize_feishu_event(event: Any) -> dict[str, str]:
    """Flatten SDK objects and webhook dicts without dropping resources.

    lark-oapi ``InboundMessage`` carries ``resources`` as objects, sender as
    ``Identity``, and ``content_text`` like ``![image](key)``. Those are not
    user captions and must not mark a pure image as mixed text+media.
    """
    payload = _feishu_event_payload(event)
    out = _normalize_feishu_payload(payload)
    chat_id = str(
        getattr(event, "chat_id", "")
        or getattr(getattr(event, "conversation", None), "chat_id", "")
        or ""
    )
    text = _feishu_visible_text(
        str(
            getattr(event, "content_text", "")
            or getattr(getattr(event, "content", None), "text", "")
            or ""
        )
    )
    message_id = str(getattr(event, "message_id", "") or getattr(event, "id", "") or "")
    event_id = str(getattr(event, "event_id", "") or "")
    if chat_id and not out.get("chat_id"):
        out["chat_id"] = chat_id
    if text and not out.get("text"):
        out["text"] = text
    if message_id and not out.get("message_id"):
        out["message_id"] = message_id
    if event_id and not out.get("event_id"):
        out["event_id"] = event_id
    if not out.get("sender"):
        sender = _feishu_identity_id(getattr(event, "sender", None)) or str(
            getattr(event, "sender_id", "") or ""
        )
        if sender:
            out["sender"] = sender
    if not out.get("thread"):
        conversation = getattr(event, "conversation", None)
        reply = getattr(event, "reply", None)
        thread = str(
            getattr(event, "thread_id", "")
            or getattr(conversation, "thread_id", None)
            or getattr(event, "parent_id", "")
            or getattr(event, "reply_to_message_id", "")
            or getattr(reply, "message_id", "")
            or ""
        )
        if thread:
            out["thread"] = thread
    extra: list[list[str]] = []
    resources = getattr(event, "resources", None)
    if resources:
        extra.extend(_collect_feishu_media_keys({"resources": resources}, {}, None))
    extra.extend(_feishu_content_media_keys(getattr(event, "content", None)))
    _merge_feishu_media(out, extra)
    if out.get("text"):
        out["text"] = _feishu_visible_text(out["text"])
    return out


def _feishu_event_payload(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    raw = getattr(event, "raw", None)
    if isinstance(raw, dict) and (
        raw.get("event") or raw.get("message") or raw.get("chat_id") or raw.get("content")
    ):
        return raw
    data = getattr(event, "data", None)
    if isinstance(data, dict) and data:
        return data
    dumped = getattr(event, "__dict__", None)
    return dumped if isinstance(dumped, dict) else {}


def _normalize_feishu_payload(event: dict[str, Any]) -> dict[str, str]:
    if not event:
        return {"chat_id": "", "text": ""}
    body = event.get("event") if isinstance(event.get("event"), dict) else event
    message = body.get("message") if isinstance(body.get("message"), dict) else body
    chat_id = str(
        message.get("chat_id")
        or body.get("chat_id")
        or body.get("open_chat_id")
        or event.get("chat_id")
        or ""
    )
    text = str(message.get("text") or body.get("text") or event.get("text") or "")
    content = message.get("content") or body.get("content")
    parsed_content: dict[str, Any] | None = None
    if isinstance(content, dict):
        parsed_content = content
    elif isinstance(content, str):
        try:
            loaded = json.loads(content)
            parsed_content = loaded if isinstance(loaded, dict) else None
            if not text:
                text = str((parsed_content or {}).get("text") or "")
                if not text and parsed_content is None:
                    text = content
        except (ValueError, TypeError):
            if not text:
                text = content
    message_id = str(
        message.get("message_id")
        or message.get("messageId")
        or body.get("message_id")
        or body.get("messageId")
        or event.get("message_id")
        or event.get("messageId")
        or ""
    )
    event_id = str(
        event.get("event_id")
        or event.get("eventId")
        or _dict_get(event, "header.event_id")
        or body.get("event_id")
        or body.get("eventId")
        or ""
    )
    sender = _feishu_sender(body, event)
    thread = str(
        message.get("parent_id")
        or message.get("thread_id")
        or body.get("parent_id")
        or ""
    )
    msg_type = str(message.get("message_type") or message.get("msg_type") or body.get("message_type") or "")
    if parsed_content and not text:
        text = _feishu_post_text(parsed_content)
    out = {"chat_id": chat_id, "text": text.strip()}
    if message_id:
        out["message_id"] = message_id
    if event_id:
        out["event_id"] = event_id
    if sender:
        out["sender"] = sender
    if thread:
        out["thread"] = thread
    if msg_type:
        out["message_type"] = msg_type
    keys = _collect_feishu_media_keys(message, body, parsed_content)
    if keys:
        out["_media"] = json.dumps(keys)
    return out


def _feishu_sender(body: dict[str, Any], event: dict[str, Any]) -> str:
    sender = body.get("sender") if isinstance(body.get("sender"), dict) else None
    if sender is None and isinstance(event.get("sender"), dict):
        sender = event["sender"]
    if sender is None:
        sender = _feishu_identity_id(body.get("sender")) or _feishu_identity_id(event.get("sender"))
        if sender:
            return sender
        return str(body.get("open_id") or event.get("open_id") or event.get("sender_id") or "")
    sender_id = sender.get("sender_id")
    if isinstance(sender_id, dict):
        return str(sender_id.get("open_id") or sender_id.get("user_id") or "")
    return str(sender.get("sender_id") or sender.get("open_id") or "")


def _feishu_identity_id(value: Any) -> str:
    if value is None or isinstance(value, (str, bytes, int)):
        return str(value or "").strip() if isinstance(value, str) else ""
    if isinstance(value, dict):
        nested = value.get("sender_id")
        if isinstance(nested, dict):
            return str(nested.get("open_id") or nested.get("user_id") or "")
        return str(value.get("open_id") or value.get("sender_id") or value.get("user_id") or "")
    return str(
        getattr(value, "open_id", "")
        or getattr(value, "user_id", "")
        or getattr(value, "sender_id", "")
        or ""
    )


def _feishu_visible_text(text: str) -> str:
    """Drop SDK media placeholders so a pure image stays media-only."""
    lines = [
        line
        for line in (text or "").splitlines()
        if not _feishu_media_placeholder(line.strip())
    ]
    return "\n".join(lines).strip()


def _feishu_media_placeholder(line: str) -> bool:
    if not line:
        return False
    if line in _FEISHU_MEDIA_TOKEN:
        return True
    return _FEISHU_MEDIA_PLACEHOLDER.fullmatch(line) is not None


def _feishu_post_text(content: dict[str, Any]) -> str:
    if "zh_cn" in content and isinstance(content["zh_cn"], dict):
        return _feishu_post_text(content["zh_cn"])
    parts: list[str] = []
    title = str(content.get("title") or "").strip()
    if title:
        parts.append(title)
    body = content.get("content")
    if not isinstance(body, list):
        return "\n".join(parts)
    for paragraph in body:
        if not isinstance(paragraph, list):
            continue
        line: list[str] = []
        for run in paragraph:
            if not isinstance(run, dict):
                continue
            tag = str(run.get("tag") or "")
            if tag in {"text", "a", "at"}:
                piece = str(run.get("text") or run.get("user_name") or "")
                if piece:
                    line.append(piece)
        if line:
            parts.append("".join(line))
    return "\n".join(parts)


def _collect_feishu_media_keys(
    message: dict[str, Any],
    body: dict[str, Any],
    content: dict[str, Any] | None,
) -> list[list[str]]:
    keys: list[list[str]] = []
    seen: set[str] = set()

    def add(kind: str, key: str, filename: str = "") -> None:
        token = key.strip()
        if not token or token in seen:
            return
        seen.add(token)
        keys.append([kind, token, filename])

    if content:
        if content.get("image_key"):
            add("image", str(content.get("image_key")))
        if content.get("file_key"):
            add("file", str(content.get("file_key")), str(content.get("file_name") or ""))
        _walk_feishu_post_media(content, add)
    for resource in _feishu_resource_list(message, body):
        kind = str(resource.get("type") or resource.get("resource_type") or "")
        key = str(
            resource.get("file_key")
            or resource.get("image_key")
            or resource.get("resource_key")
            or ""
        )
        filename = str(resource.get("file_name") or resource.get("name") or "")
        add("image" if "image" in kind or kind == "img" else "file", key, filename)
    return keys


def _feishu_resource_as_dict(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        return item
    key = str(
        getattr(item, "file_key", "")
        or getattr(item, "image_key", "")
        or getattr(item, "resource_key", "")
        or ""
    )
    if not key:
        return None
    return {
        "type": str(getattr(item, "type", "") or getattr(item, "resource_type", "") or ""),
        "file_key": key,
        "file_name": str(getattr(item, "file_name", "") or getattr(item, "name", "") or ""),
    }


def _feishu_resource_list(message: dict[str, Any], body: dict[str, Any]) -> list[dict[str, Any]]:
    raw = message.get("resources") or body.get("resources") or message.get("resource")
    if isinstance(raw, dict):
        return [raw]
    if not isinstance(raw, list):
        return []
    mapped: list[dict[str, Any]] = []
    for item in raw:
        resource = _feishu_resource_as_dict(item)
        if resource is not None:
            mapped.append(resource)
    return mapped


def _feishu_content_media_keys(content: Any) -> list[list[str]]:
    if content is None or isinstance(content, (str, bytes)):
        return []
    if isinstance(content, dict):
        return _collect_feishu_media_keys({}, {}, content)
    keys: list[list[str]] = []
    image_key = str(getattr(content, "image_key", "") or "")
    file_key = str(getattr(content, "file_key", "") or "")
    file_name = str(getattr(content, "file_name", "") or "")
    if image_key:
        keys.append(["image", image_key, ""])
    if file_key:
        keys.append(["file", file_key, file_name])
    post = getattr(content, "post", None)
    if isinstance(post, dict):
        keys.extend(_collect_feishu_media_keys({}, {}, post))
    raw = getattr(content, "raw", None)
    if isinstance(raw, dict):
        keys.extend(_collect_feishu_media_keys({}, {}, raw))
    return keys


def _merge_feishu_media(out: dict[str, str], extra: list[list[str]]) -> None:
    if not extra:
        return
    existing = _feishu_media_keys(out)
    seen = {key for _kind, key, _name in existing}
    merged = [[kind, key, name] for kind, key, name in existing]
    for item in extra:
        if len(item) < 2:
            continue
        key = str(item[1] or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append([str(item[0]), key, str(item[2] if len(item) > 2 else "")])
    if merged:
        out["_media"] = json.dumps(merged)


def _walk_feishu_post_media(content: dict[str, Any], add) -> None:  # noqa: ANN001
    if "zh_cn" in content and isinstance(content["zh_cn"], dict):
        _walk_feishu_post_media(content["zh_cn"], add)
        return
    body = content.get("content")
    if not isinstance(body, list):
        return
    for paragraph in body:
        if not isinstance(paragraph, list):
            continue
        for run in paragraph:
            if not isinstance(run, dict):
                continue
            tag = str(run.get("tag") or "")
            if tag in {"img", "image"}:
                add("image", str(run.get("image_key") or run.get("file_key") or ""))
            elif tag in {"media", "file"}:
                add(
                    "file",
                    str(run.get("file_key") or ""),
                    str(run.get("file_name") or ""),
                )


def _feishu_media_keys(msg: dict[str, str]) -> list[tuple[str, str, str]]:
    raw = msg.get("_media") or ""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out: list[tuple[str, str, str]] = []
    for item in items:
        if isinstance(item, list) and len(item) >= 2:
            out.append((str(item[0]), str(item[1]), str(item[2] if len(item) > 2 else "")))
    return out


def _dict_get(data: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        cursor: Any = data
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                cursor = None
                break
            cursor = cursor[part]
        if cursor:
            return cursor
    return None


class _LarkChannelThread:
    def __init__(self) -> None:
        self.channel: Any | None = None
        self.error: BaseException | None = None
        self.thread: threading.Thread | None = None
        self.stopped = threading.Event()


def _start_lark_channel_thread(
    app_id: str,
    app_secret: str,
    loop: asyncio.AbstractEventLoop,
    handler: Any,
) -> _LarkChannelThread:
    """Run the blocking lark-oapi WebSocket client on an isolated event loop.

    lark-oapi's WebSocket module keeps a module-level asyncio loop. If that
    module is imported while Omni's daemon loop is running, the SDK later tries
    to ``run_until_complete`` the already-running loop. Importing and starting
    the SDK from this dedicated thread gives the SDK a loop it owns.
    """

    handle = _LarkChannelThread()

    def _run() -> None:
        sdk_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(sdk_loop)
        ws_module = None
        previous_ws_loop = None
        try:
            try:
                import lark_oapi.ws.client as ws_module  # type: ignore

                previous_ws_loop = getattr(ws_module, "loop", None)
                ws_module.loop = sdk_loop
            except ImportError:
                ws_module = None
            from lark_oapi.channel import FeishuChannel as LarkFeishuChannel  # type: ignore

            channel = LarkFeishuChannel(app_id=app_id, app_secret=app_secret)
            handle.channel = channel
            _register_lark_message_handler(channel, loop, handler)
            start = getattr(channel, "start", None)
            if not callable(start):
                raise RuntimeError("installed lark_oapi channel object has no start() method")
            start()
        except BaseException as exc:  # noqa: BLE001
            handle.error = exc
            logger.exception("Feishu WS channel failed.")
        finally:
            if ws_module is not None and getattr(ws_module, "loop", None) is sdk_loop:
                ws_module.loop = previous_ws_loop
            handle.stopped.set()
            _drain_and_close_loop(sdk_loop)

    handle.thread = threading.Thread(target=_run, name="omni-feishu-ws", daemon=True)
    handle.thread.start()
    return handle


def _drain_and_close_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel leftover SDK tasks before closing the loop so teardown stays quiet.

    lark-oapi leaves its ``_receive_message_loop`` / ``_ping_loop`` and an
    ``ExpiringCache`` cron task pending on this loop. Closing the loop while they
    are still pending logs noisy ``Task was destroyed but it is pending!`` errors
    and later trips ``ExpiringCache.__del__`` (``Event loop is closed``). This
    surfaces whenever the daemon is stopped/restarted — e.g. by ``omni update``.
    Cancelling and awaiting the tasks first keeps shutdown clean.
    """
    if loop.is_closed():
        return

    async def _cancel_pending() -> None:
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    try:
        loop.run_until_complete(_cancel_pending())
        loop.run_until_complete(loop.shutdown_asyncgens())
    except RuntimeError:
        logger.debug("Feishu SDK loop busy during shutdown drain", exc_info=True)
    except Exception:  # noqa: BLE001 - shutdown best-effort; never raise from teardown.
        logger.debug("Feishu SDK loop drain failed", exc_info=True)
    finally:
        if not loop.is_closed():
            try:
                loop.close()
            except RuntimeError:
                logger.debug("Feishu SDK loop was still running during shutdown", exc_info=True)


def _register_lark_message_handler(channel: Any, loop: asyncio.AbstractEventLoop, handler: Any) -> None:
    def _on_message(event: Any) -> None:
        _schedule_on_loop(loop, handler(event), "Feishu message handler")

    if hasattr(channel, "on"):
        channel.on("message", _on_message)
    elif hasattr(channel, "on_message"):
        channel.on_message(_on_message)
    else:
        logger.warning("Installed lark_oapi channel object has no message subscription API.")


async def _wait_lark_channel_ready(handle: _LarkChannelThread, *, timeout: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if handle.error is not None:
            raise RuntimeError(f"Feishu WS channel failed: {handle.error}") from handle.error
        channel = handle.channel
        if channel is not None:
            if bool(getattr(channel, "_ready_flag", False)):
                return
            ws = getattr(channel, "_ws_client", None)
            if ws is not None and getattr(ws, "_conn", None) is not None:
                mark_ready = getattr(channel, "_mark_ready", None)
                if callable(mark_ready):
                    mark_ready()
                return
        if handle.stopped.is_set():
            raise RuntimeError("Feishu WS channel exited before becoming ready")
        if loop.time() >= deadline:
            await _stop_lark_channel_thread(handle)
            raise RuntimeError("Timed out waiting for Feishu WS channel readiness")
        await asyncio.sleep(0.05)


def _schedule_on_loop(loop: asyncio.AbstractEventLoop, coro: Any, label: str) -> None:
    """Run an SDK callback coroutine on Omni's main service loop."""
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception:  # noqa: BLE001
        logger.exception("%s could not be scheduled", label)
        if inspect.iscoroutine(coro):
            coro.close()
        return

    def _done(done_future) -> None:  # noqa: ANN001
        try:
            done_future.result()
        except Exception:  # noqa: BLE001
            logger.exception("%s failed", label)

    future.add_done_callback(_done)


async def _stop_lark_channel_thread(handle: _LarkChannelThread) -> None:
    channel = handle.channel
    if channel is not None:
        await _stop_lark_channel(channel)
    thread = handle.thread
    if thread is not None and thread.is_alive():
        await asyncio.to_thread(thread.join, 5.0)


async def _stop_lark_channel(channel: Any) -> None:
    """Best-effort shutdown for lark-oapi channel objects across versions."""
    for name in ("stop", "stop_background"):
        method = getattr(channel, name, None)
        if not callable(method):
            continue
        try:
            result = method()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001
            logger.debug("Feishu channel %s failed during shutdown", name, exc_info=True)
        return
