"""WeChat channel — official ClawBot iLink only.

OmniScientist speaks Tencent's official iLink bot HTTP/JSON API directly — the
same backend the ``@tencent-weixin/openclaw-weixin`` plugin uses. ``omni channel
login wechat`` shows the liteapp QR; after the scan the user chats with the
WeChat ClawBot and OmniScientist answers. No self-hosted :8088 bridge, WeCom
adapter, public webhook, Node, or OpenClaw runtime.

Config (``~/.omni/channels/wechat.toml`` + secrets in ``secrets.toml``/Keychain):

    mode = "ilink"
    account_id = "..."          # optional, returned by the scan
    # bot_token lives in secrets.toml / Keychain
"""

import asyncio
import contextlib
import json
import logging
from typing import Any

from omni.channels.base import Channel
from omni.channels.config import load_channel_config
from omni.channels.inbound import (
    InboundFragment,
    MediaRef,
    SealedJob,
    download_fail_note,
)
from omni.channels.outbound import WeixinIlinkOutbound, send_presentation
from omni.channels.security import (
    UNPAIRED_MEDIA_PLACEHOLDER,
    claim_inbound_message,
    inbound_event_seen,
    inbound_media_allowed,
)
from omni.channels.weixin_ilink import (
    SESSION_TIMEOUT_ERRCODE,
    TYPING_STATUS_CANCEL,
    TYPING_STATUS_TYPING,
    WeixinIlinkClient,
    is_bot_message,
    media_items,
    message_text,
)
from omni.runtime.notifications import TaskNotification
from omni.runtime.presentation import TaskPresentation, TurnPresentation

logger = logging.getLogger(__name__)

WECHAT_AUTH_EXPIRED_REASON = "WeChat login expired; scan the QR code again."


class WeChatAuthExpired(RuntimeError):
    """Stored iLink token was rejected; the user must scan again."""

    health_reason = WECHAT_AUTH_EXPIRED_REASON


class WeChatChannel(Channel):
    name = "wechat"

    def __init__(self, settings, agent, *, client=None) -> None:  # noqa: ANN001
        super().__init__(settings, agent)
        self._cfg = load_channel_config(settings, self.name)
        # Per-peer context_token map (echoed verbatim on every iLink reply).
        self._ctx_tokens: dict[str, str] = {}
        # Per-peer typing_ticket cache (fetched lazily via getconfig). ``None``
        # marks a user we already tried and should not refetch this session.
        self._typing_tickets: dict[str, str] = {}
        self._typing_enabled = bool(self._cfg.get("typing_indicator", True))
        # iLink typing indicators expire after a few seconds, so refresh them while
        # a turn is in flight, keeping the native typing indicator active.
        self._typing_refresh_s = max(1.0, float(self._cfg.get("typing_refresh_s") or 4.0))
        self._client = client if client is not None else WeixinIlinkClient.from_config(self._cfg)
        # The iLink outbound wrapper threads the per-peer context_token onto every
        # reply. Test fakes that only speak ``send_markdown`` skip the wrapper.
        self._ilink_outbound = (
            WeixinIlinkOutbound(self._client, self._ctx_tokens)
            if hasattr(self._client, "send_message")
            else None
        )

    def _config_path(self):
        return self.settings.paths.channels_dir / "wechat.toml"

    async def start(self) -> None:
        cfg = self._config_path()
        if not cfg.is_file():
            logger.warning(
                "WeChat channel not configured. Run `omni channel login wechat`. (%s)", cfg
            )
            return
        await self._start_ilink()

    async def _start_ilink(self) -> None:
        token = str(self._cfg.get("bot_token") or "")
        if not token:
            logger.warning(
                "WeChat iLink channel has no bot_token; run `omni channel login wechat`."
            )
            return
        client = self._client
        logger.info(
            "WeChat iLink channel running (account=%s, base=%s)",
            self._cfg.get("account_id") or "?",
            getattr(client, "base_url", "?"),
        )
        await client.notify_start()
        buf = self._load_sync_buf()
        failures = 0
        while True:
            try:
                resp = await client.get_updates(buf)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failures += 1
                logger.warning("WeChat getupdates error (%d): %s", failures, exc)
                await asyncio.sleep(30 if failures >= 3 else 2)
                if failures >= 3:
                    failures = 0
                continue
            ret = int(resp.get("ret") or 0)
            errcode = int(resp.get("errcode") or 0)
            if ret or errcode:
                if SESSION_TIMEOUT_ERRCODE in (ret, errcode):
                    logger.error(
                        "WeChat iLink token expired; re-run `omni channel login wechat`."
                    )
                    raise WeChatAuthExpired(WECHAT_AUTH_EXPIRED_REASON)
                failures += 1
                logger.warning(
                    "WeChat getupdates ret=%s errcode=%s errmsg=%s",
                    ret,
                    errcode,
                    resp.get("errmsg"),
                )
                await asyncio.sleep(30 if failures >= 3 else 2)
                if failures >= 3:
                    failures = 0
                continue
            failures = 0
            msgs = [msg for msg in (resp.get("msgs") or []) if isinstance(msg, dict)]
            await self._admit_ilink_batch(msgs)
            # Cursor moves only after the batch is in the mailbox. The mailbox
            # is at-most-once: a crash after this save and before the worker
            # finishes can drop the turn. That is known debt, not exactly-once.
            new_buf = resp.get("get_updates_buf")
            if new_buf:
                buf = str(new_buf)
                self._save_sync_buf(buf)

    async def handle_ilink_message(self, msg: dict[str, Any]) -> TurnPresentation | None:
        """Admit one event and wait for its peer worker (tests / single-shot)."""
        fragment = await self.admit_ilink_message(msg)
        if fragment is None:
            return None
        await self.inbound.flush_peer(fragment.peer_key())
        return await self.inbound.wait_peer(fragment.peer_key())

    async def admit_ilink_message(self, msg: dict[str, Any]) -> InboundFragment | None:
        """Parse, download, and enqueue. Does not wait for the agent."""
        preview = self._ilink_preview(msg)
        if preview is None:
            return None
        if inbound_event_seen(
            self.settings,
            self.name,
            preview.conversation,
            message_id=preview.message_id,
        ):
            return None
        if preview.has_media() and any(not ref.path for ref in preview.media):
            fragment = await self.inbound.materialize_and_admit(
                preview, lambda: self._materialize_ilink(msg, preview)
            )
            if fragment is None:
                return None
            claim_inbound_message(
                self.settings,
                self.name,
                fragment.conversation,
                message_id=fragment.message_id,
            )
            return fragment
        return await self._admit_parsed_ilink(preview)

    async def _admit_ilink_batch(self, msgs: list[dict[str, Any]]) -> None:
        """Admit one getupdates batch, then arm timers.

        Each event goes through ``materialize_and_admit`` so a slow image in a
        later poll can hold a pending caption. Same-poll items stay one unit
        because ``batch()`` does not start quiet timers until the end.
        """
        if not msgs:
            return
        with self.inbound.batch():
            for msg in msgs:
                try:
                    await self.admit_ilink_message(msg)
                except Exception:
                    logger.exception("WeChat inbound parse failed")

    async def _admit_parsed_ilink(self, fragment: InboundFragment | None) -> InboundFragment | None:
        if fragment is None:
            return None
        if inbound_event_seen(
            self.settings,
            self.name,
            fragment.conversation,
            message_id=fragment.message_id,
        ):
            return None
        if not await self.inbound.admit(fragment):
            return None
        claim_inbound_message(
            self.settings,
            self.name,
            fragment.conversation,
            message_id=fragment.message_id,
        )
        return fragment

    def _ilink_preview(self, msg: dict[str, Any]) -> InboundFragment | None:
        if is_bot_message(msg):
            return None
        external_key = str(msg.get("from_user_id") or "")
        if not external_key:
            return None
        text = message_text(msg)
        media = media_items(msg)
        if not text and not media:
            return None
        message_id = str(msg.get("message_id") or msg.get("seq") or "")
        seq = msg.get("seq")
        sequence = int(seq) if isinstance(seq, int) else None
        token = str(msg.get("context_token") or "")
        context = {"context_token": token} if token else {}
        if media and not inbound_media_allowed(self.settings, self.name, external_key):
            return InboundFragment(
                channel=self.name,
                conversation=external_key,
                sender=external_key,
                message_id=message_id,
                sequence=sequence,
                text=text or UNPAIRED_MEDIA_PLACEHOLDER,
                media=(),
                reply_context=context,
            )
        placeholders = tuple(MediaRef(path="", kind="image") for _ in media)
        return InboundFragment(
            channel=self.name,
            conversation=external_key,
            sender=external_key,
            message_id=message_id,
            sequence=sequence,
            text=text,
            media=placeholders,
            reply_context=context,
        )

    async def _materialize_ilink(
        self, msg: dict[str, Any], preview: InboundFragment
    ) -> InboundFragment | None:
        try:
            refs, notes = await self._download_ilink_media(media_items(msg))
        except Exception as exc:  # noqa: BLE001 - keep caption if CDN/decrypt blows up
            logger.warning("WeChat inbound media download failed: %s", exc)
            refs, notes = [], [download_fail_note()]
        body = "\n".join(filter(None, [preview.text, *notes]))
        if not body and not refs:
            return None
        return InboundFragment(
            channel=preview.channel,
            conversation=preview.conversation,
            sender=preview.sender,
            message_id=preview.message_id,
            sequence=preview.sequence,
            text=body,
            media=tuple(refs),
            reply_context=dict(preview.reply_context),
        )

    async def _download_ilink_media(
        self, media: list[dict[str, Any]]
    ) -> tuple[list[MediaRef], list[str]]:
        if not media:
            return [], []
        from omni.core.user_inputs import ensure_inputs_dir

        notes: list[str] = []
        refs: list[MediaRef] = []
        dest = str(ensure_inputs_dir(self.settings.paths))
        downloader = getattr(self._client, "download_media_from_item", None)
        for item in media:
            saved = None
            try:
                if callable(downloader):
                    saved = await downloader(item, dest)
            except Exception as exc:  # noqa: BLE001 - one failed file must not drop the caption
                logger.warning("WeChat inbound media item failed: %s", exc)
                saved = None
            if saved is not None:
                refs.append(
                    MediaRef(
                        path=saved.path,
                        kind=str(getattr(saved, "kind", "") or ""),
                        mime=str(getattr(saved, "mime", "") or ""),
                        file_name=str(getattr(saved, "file_name", "") or ""),
                    )
                )
            else:
                notes.append(download_fail_note())
        return refs, notes

    def apply_sealed_reply(self, job: SealedJob) -> None:
        token = str(job.reply_context.get("context_token") or "")
        if not token:
            return
        self._ctx_tokens[job.conversation] = token
        if self._ilink_outbound is not None:
            self._ilink_outbound.seal(job.conversation, token)

    def clear_sealed_reply(self, job: SealedJob) -> None:
        if self._ilink_outbound is not None:
            self._ilink_outbound.unseal(job.conversation)

    async def dispatch_inbound_job(self, job: SealedJob) -> TurnPresentation:
        token = str(job.reply_context.get("context_token") or "")
        return await self._run_with_typing(
            job.conversation,
            token,
            super().dispatch_inbound_job(job),
        )

    # ── typing indicator ──────────────────────────────────────────────────────
    async def _run_with_typing(self, external_key: str, context_token: str, coro):  # noqa: ANN001
        """Show a *continuous* typing indicator around one agent turn.

        WeChat can't render true per-character streaming in a normal bot bubble,
        so the closest "still working" feedback is keeping the typing indicator
        alive for the whole turn (it otherwise expires after a few seconds), then
        delivering the reply as one or more FINISH bubbles. Best-effort: typing is
        cosmetic and must never break or delay the turn.
        """
        if not self._typing_enabled or not hasattr(self._client, "send_typing"):
            return await coro
        await self._send_typing(external_key, context_token, TYPING_STATUS_TYPING)
        keepalive = asyncio.create_task(self._typing_keepalive(external_key, context_token))
        try:
            return await coro
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
            await self._send_typing(external_key, context_token, TYPING_STATUS_CANCEL)

    async def _typing_keepalive(self, external_key: str, context_token: str) -> None:
        """Re-assert the typing indicator every ``typing_refresh_s`` until cancelled."""
        while True:
            await asyncio.sleep(self._typing_refresh_s)
            await self._send_typing(external_key, context_token, TYPING_STATUS_TYPING)

    async def _send_typing(self, external_key: str, context_token: str, status: int) -> None:
        send_typing = getattr(self._client, "send_typing", None)
        if not callable(send_typing):
            return
        try:
            ticket = await self._typing_ticket(external_key, context_token)
            await send_typing(external_key, typing_ticket=ticket, status=status)
        except Exception:  # noqa: BLE001 - typing is cosmetic; never break a turn
            logger.debug("wechat send_typing failed (ignored)", exc_info=True)

    async def _typing_ticket(self, external_key: str, context_token: str) -> str:
        if external_key in self._typing_tickets:
            return self._typing_tickets[external_key]
        ticket = ""
        get_config = getattr(self._client, "get_config", None)
        if callable(get_config):
            try:
                cfg = await get_config(external_key, context_token=context_token or None)
                ticket = str((cfg or {}).get("typing_ticket") or "")
            except Exception:  # noqa: BLE001
                logger.debug("wechat get_config failed (ignored)", exc_info=True)
        self._typing_tickets[external_key] = ticket
        return ticket

    def _sync_buf_path(self):
        return self.settings.paths.cache_dir / "wechat_ilink_sync.json"

    def _load_sync_buf(self) -> str:
        path = self._sync_buf_path()
        if not path.is_file():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str(data.get("get_updates_buf") or "") if isinstance(data, dict) else ""

    def _save_sync_buf(self, buf: str) -> None:
        path = self._sync_buf_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"get_updates_buf": buf}, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            logger.debug("failed to persist wechat sync buf: %s", exc)

    async def send_turn(
        self, external_key: str, presentation: TurnPresentation | TaskPresentation
    ) -> None:
        roots = self.uploadable_roots()
        client = self._ilink_outbound if self._ilink_outbound is not None else self._client
        return await send_presentation(client, external_key, presentation, allowed_roots=roots)

    async def notify(self, note: TaskNotification) -> None:
        await self.send_task_notification(note)

    async def stop(self) -> None:
        await super().stop()
        notify_stop = getattr(self._client, "notify_stop", None)
        if callable(notify_stop):
            try:
                await notify_stop()
            except Exception:  # noqa: BLE001
                logger.debug("wechat notify_stop failed", exc_info=True)
