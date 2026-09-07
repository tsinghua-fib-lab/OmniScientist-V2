"""Thin inbound fragment mailbox + per-peer FIFO workers.

One logical user input becomes one Omni task. Channels parse provider events
into :class:`InboundFragment` values; this module merges complementary pieces
and runs the agent through a sealed job. It is not a coordinator framework.

Memory mailbox is at-most-once. A crash after admission and before the worker
finishes can drop the message; that is known debt, not exactly-once.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omni.core.image_files import mime_for_bytes
from omni.core.user_inputs import bind_input_mentions, write_user_input
from omni.runtime.presentation import TurnPresentation

logger = logging.getLogger(__name__)

# iLink often delivers caption and image as two events. 1s was too short:
# a CDN download on the next event could outlast the timer and split one
# user send into two Omni tasks. 3s matches Hermes/OpenClaw Weixin debounce.
COMPLEMENTARY_QUIET_WINDOW_S = 3.0
MEDIA_HOLD_WINDOW_S = 8.0
DEICTIC_HOLD_WINDOW_S = 12.0
MAX_INBOUND_MEDIA_BYTES = 25 * 1024 * 1024
MAX_INBOUND_MEDIA_FILES = 8

_DEICTIC_MARKERS = (
    "这张图",
    "这幅图",
    "这张截图",
    "这幅截图",
    "这个截图",
    "这个图片",
    "这张图片",
    "这张照片",
    "看看这张",
    "看看这个截图",
    "看看这幅",
    "分析这张",
    "分析这个截图",
    "分析这幅",
    "上面的图",
    "附图",
)
_DEICTIC_EN = re.compile(
    r"\b(?:this|the)\s+(?:image|screenshot|picture|photo)\b",
    re.IGNORECASE,
)

_DOWNLOAD_FAIL_NOTE = "[The user sent media that could not be downloaded or decrypted.]"

_EXT_MIME = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}


@dataclass(frozen=True)
class MediaRef:
    """A file already saved under workspace ``inputs/``."""

    path: str
    kind: str = ""
    mime: str = ""
    file_name: str = ""


@dataclass
class InboundFragment:
    """One provider event after parse + download, before complementary merge."""

    channel: str
    conversation: str
    sender: str = ""
    thread: str = ""
    message_id: str = ""
    event_id: str = ""
    sequence: int | None = None
    occurred_at: float | None = None
    text: str = ""
    media: tuple[MediaRef, ...] = ()
    reply_context: dict[str, str] = field(default_factory=dict)

    def peer_key(self) -> str:
        return "\x1f".join(
            (
                self.channel,
                self.conversation,
                self.sender or self.conversation,
                self.thread,
            )
        )

    def is_command(self) -> bool:
        return self.text.lstrip().startswith("/")

    def has_text(self) -> bool:
        return bool(self.text.strip())

    def has_media(self) -> bool:
        return bool(self.media)

    def is_mixed(self) -> bool:
        return self.has_text() and self.has_media()

    def is_text_only(self) -> bool:
        return self.has_text() and not self.has_media()

    def is_media_only(self) -> bool:
        return self.has_media() and not self.has_text()

    def identity(self) -> str:
        event = (self.message_id or self.event_id or "").strip()
        return f"{self.channel}:{self.conversation}:{event}" if event else ""


@dataclass(frozen=True)
class SealedJob:
    """A logical user input ready for one ``handle_inbound_and_send``.

    ``reply_context`` is sealed here. Send must not reread a shared per-peer map
    that a later fragment could overwrite.
    """

    peer_key: str
    conversation: str
    text: str
    media: tuple[MediaRef, ...] = ()
    reply_context: dict[str, str] = field(default_factory=dict)
    message_ids: tuple[str, ...] = ()


def is_deictic_text(text: str) -> bool:
    """True when isolated text is waiting for a nearby image ('这张图', …)."""
    body = (text or "").strip()
    if not body:
        return False
    if any(marker in body for marker in _DEICTIC_MARKERS):
        return True
    return _DEICTIC_EN.search(body) is not None


def complementary(left: InboundFragment, right: InboundFragment) -> bool:
    """Whether two adjacent fragments are one logical user input."""
    if left.peer_key() != right.peer_key():
        return False
    if left.is_command() or right.is_command():
        return False
    if left.message_id and left.message_id == right.message_id:
        return True
    if left.is_mixed() or right.is_mixed():
        return False
    if left.is_media_only() and right.is_media_only():
        return True
    if left.is_media_only() and right.is_text_only():
        return True
    if left.is_text_only() and right.is_media_only():
        return True
    return False


def merge_fragments(*parts: InboundFragment) -> InboundFragment:
    """Combine same-message or complementary fragments into one."""
    first = parts[0]
    texts = [part.text.strip() for part in parts if part.text.strip()]
    media: list[MediaRef] = []
    context: dict[str, str] = {}
    ids: list[str] = []
    for part in parts:
        media.extend(part.media)
        context.update({key: value for key, value in part.reply_context.items() if value})
        if part.message_id:
            ids.append(part.message_id)
    return InboundFragment(
        channel=first.channel,
        conversation=first.conversation,
        sender=first.sender or first.conversation,
        thread=first.thread,
        message_id=ids[-1] if ids else first.message_id,
        event_id=next((part.event_id for part in reversed(parts) if part.event_id), first.event_id),
        sequence=next((part.sequence for part in reversed(parts) if part.sequence is not None), first.sequence),
        occurred_at=next(
            (part.occurred_at for part in reversed(parts) if part.occurred_at is not None),
            first.occurred_at,
        ),
        text="\n".join(texts),
        media=tuple(media),
        reply_context=context,
    )


def sniff_inbound_mime(data: bytes, filename: str = "") -> str | None:
    """Magic first, then a small extension allow-list."""
    mime = mime_for_bytes(data)
    if mime:
        return mime
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"PK"):
        return "application/zip"
    ext = Path(filename).suffix.lower()
    return _EXT_MIME.get(ext)


def save_inbound_bytes(
    dest_dir: Path,
    data: bytes,
    *,
    filename: str,
    require_magic: bool = False,
    existing_count: int = 0,
) -> Path | None:
    """Write inbound bytes through ``write_user_input`` or return ``None``.

    Failed downloads must not create a half attachment. Size, count, MIME, and
    ``inputs/`` containment are enforced here.
    """
    if not data:
        return None
    if existing_count >= MAX_INBOUND_MEDIA_FILES:
        logger.warning("inbound media count cap (%d) reached", MAX_INBOUND_MEDIA_FILES)
        return None
    if len(data) > MAX_INBOUND_MEDIA_BYTES:
        logger.warning("inbound media too large (%d bytes)", len(data))
        return None
    mime = sniff_inbound_mime(data, filename)
    if mime is None and require_magic:
        logger.warning("inbound media rejected: unrecognized type (%s)", filename)
        return None
    dest = Path(dest_dir)
    try:
        path = write_user_input(dest, data, filename=filename)
    except OSError as exc:
        logger.warning("inbound media save failed: %s", exc)
        return None
    try:
        resolved = path.resolve()
        root = dest.resolve()
    except OSError:
        path.unlink(missing_ok=True)
        return None
    if not resolved.is_relative_to(root):
        path.unlink(missing_ok=True)
        logger.warning("inbound media escaped inputs/ (%s)", path)
        return None
    return path


def download_fail_note() -> str:
    return _DOWNLOAD_FAIL_NOTE


@dataclass
class _PeerMailbox:
    pending: InboundFragment | None = None
    timer: asyncio.Task[None] | None = None
    queue: asyncio.Queue[SealedJob | None] = field(default_factory=asyncio.Queue)
    busy: bool = False
    last_presentation: TurnPresentation | None = None
    worker: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seal_gen: int = 0


class InboundCoordinator:
    """Per-peer mailbox + FIFO worker attached to one channel."""

    def __init__(
        self,
        channel: Any,
        *,
        quiet_window_s: float = COMPLEMENTARY_QUIET_WINDOW_S,
        media_hold_s: float | None = None,
        deictic_hold_s: float | None = None,
    ) -> None:
        self._channel = channel
        self.quiet_window_s = quiet_window_s
        # Tests that shrink the quiet window also shrink the longer holds so
        # idle() stays fast. Production defaults keep media/deictic longer.
        extended = quiet_window_s >= 1.0
        self.media_hold_s = (
            media_hold_s
            if media_hold_s is not None
            else (MEDIA_HOLD_WINDOW_S if extended else quiet_window_s)
        )
        self.deictic_hold_s = (
            deictic_hold_s
            if deictic_hold_s is not None
            else (DEICTIC_HOLD_WINDOW_S if extended else quiet_window_s)
        )
        self._mailboxes: dict[str, _PeerMailbox] = {}
        self._seen: set[str] = set()
        self._stopping = False
        self._batch_depth = 0

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Admit a provider poll as one unit before any quiet timer starts.

        A slow CDN download of a later message in the same ``getupdates``
        batch must not flush an earlier caption.
        """
        self.begin_batch()
        try:
            yield
        finally:
            self.end_batch()

    def begin_batch(self) -> None:
        self._batch_depth += 1

    def end_batch(self) -> None:
        if self._batch_depth <= 0:
            return
        self._batch_depth -= 1
        if self._batch_depth:
            return
        for peer_key, box in list(self._mailboxes.items()):
            if box.pending is None:
                continue
            if box.pending.is_mixed() or self.quiet_window_s <= 0:
                self._flush(box)
            else:
                self._arm_timer(peer_key, box)

    def mailbox(self, peer_key: str) -> _PeerMailbox:
        box = self._mailboxes.get(peer_key)
        if box is None:
            box = _PeerMailbox()
            self._mailboxes[peer_key] = box
        return box

    def already_seen(self, fragment: InboundFragment) -> bool:
        identity = fragment.identity()
        return bool(identity) and identity in self._seen

    def mark_seen(self, fragment: InboundFragment) -> None:
        identity = fragment.identity()
        if identity:
            self._seen.add(identity)

    async def admit(self, fragment: InboundFragment) -> bool:
        """Accept a fragment into the peer mailbox.

        Returns False when the fragment is empty or a duplicate in this process.
        Does not wait for the agent.
        """
        if self._stopping:
            return False
        if not fragment.has_text() and not fragment.has_media():
            return False
        box = self.mailbox(fragment.peer_key())
        async with box.lock:
            return self._admit_unlocked(box, fragment)

    async def materialize_and_admit(
        self,
        preview: InboundFragment,
        materialize: Callable[[], Awaitable[InboundFragment | None]],
    ) -> InboundFragment | None:
        """Download one event while holding a complementary peer open.

        Stream channels (Feishu WS, DingTalk) see text and image as two
        callbacks. The quiet timer must not seal the first piece while the
        second is still on the CDN.
        """
        if self._stopping:
            return None
        box = self.mailbox(preview.peer_key())
        async with box.lock:
            if box.pending is not None and complementary(box.pending, preview):
                self._invalidate_timer(box)
            try:
                fragment = await materialize()
            except Exception:
                self._rearm_if_pending(preview.peer_key(), box)
                raise
            if fragment is None:
                self._rearm_if_pending(preview.peer_key(), box)
                return None
            if not self._admit_unlocked(box, fragment):
                return None
            return fragment

    def _admit_unlocked(self, box: _PeerMailbox, fragment: InboundFragment) -> bool:
        if self._stopping:
            return False
        if not fragment.has_text() and not fragment.has_media():
            return False
        if self.already_seen(fragment):
            return False
        self.mark_seen(fragment)
        if fragment.is_command():
            self._flush(box)
            self._enqueue(box, fragment)
            return True
        if box.pending is not None and complementary(box.pending, fragment):
            box.pending = merge_fragments(box.pending, fragment)
            if box.pending.is_mixed():
                self._flush(box)
            else:
                self._hold_pending(fragment.peer_key(), box)
            return True
        self._flush(box)
        if fragment.is_mixed() or self.quiet_window_s <= 0:
            self._enqueue(box, fragment)
            return True
        box.pending = fragment
        self._hold_pending(fragment.peer_key(), box)
        return True

    async def flush_peer(self, peer_key: str) -> None:
        """Seal any open fragment for this peer now (tests / explicit close)."""
        box = self._mailboxes.get(peer_key)
        if box is None:
            return
        async with box.lock:
            self._flush(box)

    async def wait_peer(self, peer_key: str, *, timeout: float = 30.0) -> TurnPresentation | None:
        """Wait until this peer's queue and in-flight job are idle."""
        box = self._mailboxes.get(peer_key)
        if box is None:
            return None
        await asyncio.wait_for(box.queue.join(), timeout=timeout)
        deadline = time.monotonic() + timeout
        while box.busy:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"inbound worker still busy: {peer_key}")
            await asyncio.sleep(0)
        return box.last_presentation

    async def idle(self, *, timeout: float = 30.0) -> None:
        """Wait until every peer is quiet (timers, queue, worker)."""
        deadline = time.monotonic() + timeout
        while True:
            if self._all_idle():
                await asyncio.sleep(0)
                if self._all_idle():
                    return
            if time.monotonic() >= deadline:
                raise TimeoutError("inbound coordinator did not go idle")
            await asyncio.sleep(0.01)

    async def shutdown(self) -> None:
        """Flush pending fragments, drain workers, then cancel leftovers."""
        self._stopping = True
        for box in self._mailboxes.values():
            self._flush(box)
            box.queue.put_nowait(None)
        workers = [box.worker for box in self._mailboxes.values() if box.worker is not None]
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        for box in self._mailboxes.values():
            if box.timer is not None and not box.timer.done():
                box.timer.cancel()

    def _all_idle(self) -> bool:
        for box in self._mailboxes.values():
            if box.pending is not None:
                return False
            if box.timer is not None and not box.timer.done():
                return False
            if box.queue.qsize() > 0 or box.busy:
                return False
        return True

    def _invalidate_timer(self, box: _PeerMailbox) -> None:
        box.seal_gen += 1
        if box.timer is not None:
            box.timer.cancel()
            box.timer = None

    def _rearm_if_pending(self, peer_key: str, box: _PeerMailbox) -> None:
        if box.pending is None or self._batch_depth > 0:
            return
        if box.timer is None or box.timer.done():
            self._arm_timer(peer_key, box)

    def _flush(self, box: _PeerMailbox) -> None:
        self._invalidate_timer(box)
        pending = box.pending
        box.pending = None
        if pending is not None:
            self._enqueue(box, pending)

    def _enqueue(self, box: _PeerMailbox, fragment: InboundFragment) -> None:
        ids = tuple(
            part
            for part in (fragment.message_id, fragment.event_id)
            if part
        )
        box.queue.put_nowait(
            SealedJob(
                peer_key=fragment.peer_key(),
                conversation=fragment.conversation,
                text=fragment.text,
                media=fragment.media,
                reply_context=dict(fragment.reply_context),
                message_ids=ids,
            )
        )
        self._ensure_worker(fragment.peer_key(), box)

    def _hold_s(self, fragment: InboundFragment) -> float:
        if fragment.is_media_only():
            return self.media_hold_s
        if fragment.is_text_only() and is_deictic_text(fragment.text):
            return self.deictic_hold_s
        return self.quiet_window_s

    def _hold_pending(self, peer_key: str, box: _PeerMailbox) -> None:
        """Keep an isolated fragment open. Do not arm a timer during a batch."""
        if self._batch_depth > 0:
            if box.timer is not None:
                box.timer.cancel()
                box.timer = None
            return
        self._arm_timer(peer_key, box)

    def _arm_timer(self, peer_key: str, box: _PeerMailbox) -> None:
        if box.pending is None:
            return
        hold_s = self._hold_s(box.pending)
        if box.timer is not None:
            box.timer.cancel()
        gen = box.seal_gen
        box.timer = asyncio.create_task(
            self._quiet_flush(peer_key, hold_s, gen),
            name=f"inbound-quiet-{peer_key[:24]}",
        )

    async def _quiet_flush(self, peer_key: str, hold_s: float, gen: int) -> None:
        try:
            await asyncio.sleep(max(0.0, hold_s))
        except asyncio.CancelledError:
            return
        box = self._mailboxes.get(peer_key)
        if box is None:
            return
        async with box.lock:
            if box.seal_gen != gen:
                return
            box.timer = None
            self._flush(box)

    def _ensure_worker(self, peer_key: str, box: _PeerMailbox) -> None:
        if box.worker is not None and not box.worker.done():
            return
        box.worker = asyncio.create_task(
            self._run_worker(peer_key),
            name=f"inbound-worker-{peer_key[:24]}",
        )
        box.worker.add_done_callback(self._log_worker_done)

    def _log_worker_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception("inbound peer worker crashed", exc_info=exc)

    async def _run_worker(self, peer_key: str) -> None:
        box = self._mailboxes[peer_key]
        while True:
            job = await box.queue.get()
            if job is None:
                box.queue.task_done()
                return
            box.busy = True
            try:
                box.last_presentation = await self._channel.dispatch_inbound_job(job)
            except Exception:  # noqa: BLE001 - keep the peer worker alive
                logger.exception(
                    "[%s] inbound job failed peer=%s",
                    getattr(self._channel, "name", "?"),
                    peer_key,
                )
            finally:
                box.busy = False
                box.queue.task_done()
            if (
                box.pending is None
                and box.queue.empty()
                and (box.timer is None or box.timer.done())
            ):
                return


def bind_job_text(job: SealedJob, *, cwd: Path | None = None) -> tuple[str, list[str] | None]:
    """Canonical ``[Image #N]`` + ``@path`` binding for a sealed job."""
    paths = [ref.path for ref in job.media]
    text = bind_input_mentions(job.text, paths, cwd=cwd)
    if job.text.strip() and not paths and _DOWNLOAD_FAIL_NOTE in job.text:
        return text, None
    return text, paths or None
