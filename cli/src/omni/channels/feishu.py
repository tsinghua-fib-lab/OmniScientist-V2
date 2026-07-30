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
import logging
import threading
from typing import Any

from omni.channels.base import Channel
from omni.channels.config import load_channel_config
from omni.channels.outbound import FeishuClient, send_presentation, uploadable_roots
from omni.channels.security import claim_inbound_message
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
                for msg in await self._client.poll_messages():
                    await self.handle_feishu_message(msg)
                await asyncio.sleep(max(0.5, interval))
        app_id = str(self._cfg.get("app_id") or "")
        app_secret = str(self._cfg.get("app_secret") or "")
        if not app_id or not app_secret:
            logger.warning("Feishu channel requires app_id/app_secret.")
            return
        logger.info("Feishu WS channel starting.")
        loop = asyncio.get_running_loop()
        handle = _start_lark_channel_thread(app_id, app_secret, loop, self.handle_feishu_message)
        try:
            await _wait_lark_channel_ready(handle)
            logger.info("Feishu WS channel connected.")
            while True:
                await asyncio.sleep(3600)
        finally:
            await _stop_lark_channel_thread(handle)

    async def handle_feishu_message(self, event: Any) -> TurnPresentation | None:
        msg = _normalize_feishu_event(event)
        if not msg["text"] or not msg["chat_id"]:
            return None
        if not claim_inbound_message(
            self.settings,
            self.name,
            msg["chat_id"],
            msg["text"],
            message_id=msg.get("message_id", ""),
            event_id=msg.get("event_id", ""),
        ):
            return None
        return await self.handle_inbound_and_send(msg["text"], msg["chat_id"])

    async def send_turn(self, external_key: str, presentation: TurnPresentation | TaskPresentation) -> None:
        return await send_presentation(
            self._client,
            external_key,
            presentation,
            allowed_roots=uploadable_roots(self.settings, artifacts=getattr(self.agent, "artifacts", None)),
        )

    async def notify(self, note: TaskNotification) -> None:
        await self.send_task_notification(note)


def _normalize_feishu_event(event: Any) -> dict[str, str]:
    message_id = str(getattr(event, "message_id", "") or getattr(event, "id", "") or "")
    event_id = str(getattr(event, "event_id", "") or "")
    chat_id = str(
        getattr(event, "chat_id", "")
        or getattr(getattr(event, "conversation", None), "chat_id", "")
        or ""
    )
    text = str(
        getattr(event, "content_text", "")
        or getattr(getattr(event, "content", None), "text", "")
        or ""
    )
    if chat_id or text:
        raw = getattr(event, "raw", None)
        if isinstance(raw, dict):
            message_id = message_id or str(_dict_get(raw, "message_id", "event.message.message_id") or "")
            event_id = event_id or str(_dict_get(raw, "event_id", "header.event_id") or "")
        out = {"chat_id": chat_id, "text": text.strip()}
        if message_id:
            out["message_id"] = message_id
        if event_id:
            out["event_id"] = event_id
        return out
    if not isinstance(event, dict):
        event = getattr(event, "data", event)
        event = event if isinstance(event, dict) else getattr(event, "__dict__", {})
    body = event.get("event") if isinstance(event.get("event"), dict) else event
    message = body.get("message") if isinstance(body.get("message"), dict) else body
    chat_id = str(
        message.get("chat_id")
        or body.get("chat_id")
        or body.get("open_chat_id")
        or event.get("chat_id")
        or ""
    )
    text = str(message.get("text") or body.get("text") or "")
    content = message.get("content") or body.get("content")
    if not text and isinstance(content, str):
        try:
            import json

            parsed = json.loads(content)
            text = str(parsed.get("text") or "")
        except (ValueError, TypeError):
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
    out = {"chat_id": chat_id, "text": text.strip()}
    if message_id:
        out["message_id"] = message_id
    if event_id:
        out["event_id"] = event_id
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
