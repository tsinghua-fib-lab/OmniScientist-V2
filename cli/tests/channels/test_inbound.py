"""Complementary inbound merge + per-peer FIFO workers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omni.channels.inbound import (
    InboundCoordinator,
    InboundFragment,
    MediaRef,
    complementary,
    is_deictic_text,
    merge_fragments,
    save_inbound_bytes,
)
from omni.runtime.presentation import TurnPresentation

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _frag(**kwargs: object) -> InboundFragment:
    base = dict(
        channel="wechat",
        conversation="alice",
        sender="alice",
        text="",
        media=(),
    )
    base.update(kwargs)
    return InboundFragment(**base)  # type: ignore[arg-type]


def test_two_plain_texts_are_never_complementary() -> None:
    left = _frag(text="问题 A", message_id="1")
    right = _frag(text="问题 B", message_id="2")
    assert complementary(left, right) is False


def test_media_and_caption_are_complementary() -> None:
    image = _frag(media=(MediaRef(path="/tmp/a.png", kind="image"),), message_id="1")
    caption = _frag(text="仔细分析这个截图", message_id="2")
    assert complementary(image, caption) is True
    assert complementary(caption, image) is True
    merged = merge_fragments(image, caption)
    assert merged.text == "仔细分析这个截图"
    assert len(merged.media) == 1


def test_same_message_id_merges_even_when_both_have_text() -> None:
    left = _frag(text="看图", message_id="same")
    right = _frag(text="", media=(MediaRef(path="/tmp/a.png"),), message_id="same")
    assert complementary(left, right) is True


def test_commands_and_other_peers_do_not_merge() -> None:
    image = _frag(media=(MediaRef(path="/tmp/a.png"),), message_id="1")
    command = _frag(text="/pair 123456", message_id="2")
    other = _frag(sender="bob", conversation="bob", text="说明", message_id="3")
    assert complementary(image, command) is False
    assert complementary(image, other) is False


def test_save_inbound_bytes_rejects_escape_and_empty(tmp_path: Path) -> None:
    dest = tmp_path / "inputs"
    dest.mkdir()
    saved = save_inbound_bytes(dest, PNG, filename="../pwned.png", require_magic=True)
    assert saved is not None
    assert saved.parent == dest
    assert not (tmp_path / "pwned.png").exists()
    assert save_inbound_bytes(dest, b"", filename="empty.png", require_magic=True) is None
    assert save_inbound_bytes(dest, b"not-an-image", filename="x.bin", require_magic=True) is None


class _RecordingChannel:
    name = "wechat"

    def __init__(self) -> None:
        self.jobs: list[object] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.slow = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = False

    async def dispatch_inbound_job(self, job):  # noqa: ANN001
        self.jobs.append(job)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.started.set()
        try:
            if self.fail:
                raise RuntimeError("boom")
            if self.slow:
                await self.release.wait()
            return TurnPresentation(assistant_text=job.text or "ok")
        finally:
            self.concurrent -= 1


@pytest.mark.asyncio
async def test_mailbox_merges_complementary_and_splits_two_texts() -> None:
    channel = _RecordingChannel()
    inbound = InboundCoordinator(channel, quiet_window_s=0.05)
    image = _frag(media=(MediaRef(path="/tmp/a.png", kind="image"),), message_id="img")
    caption = _frag(text="这张图讲什么", message_id="txt")
    assert await inbound.admit(image) is True
    assert await inbound.admit(caption) is True
    await inbound.idle()
    assert len(channel.jobs) == 1
    assert channel.jobs[0].text == "这张图讲什么"
    assert channel.jobs[0].media[0].path == "/tmp/a.png"

    other = InboundCoordinator(channel, quiet_window_s=0.05)
    assert await other.admit(_frag(text="问题 A", message_id="a"))
    assert await other.admit(_frag(text="问题 B", message_id="b"))
    await other.idle()
    assert [job.text for job in channel.jobs[1:]] == ["问题 A", "问题 B"]
    await inbound.shutdown()
    await other.shutdown()


@pytest.mark.asyncio
async def test_same_peer_is_serial_and_other_peers_are_parallel() -> None:
    channel = _RecordingChannel()
    channel.slow = True
    inbound = InboundCoordinator(channel, quiet_window_s=0)
    first = _frag(conversation="p1", sender="p1", text="one", message_id="1")
    second = _frag(conversation="p1", sender="p1", text="two", message_id="2")
    other = _frag(conversation="p2", sender="p2", text="other", message_id="3")
    await inbound.admit(first)
    await inbound.flush_peer(first.peer_key())
    await channel.started.wait()
    channel.started.clear()
    await inbound.admit(second)
    await inbound.flush_peer(second.peer_key())
    await inbound.admit(other)
    await inbound.flush_peer(other.peer_key())
    await asyncio.wait_for(channel.started.wait(), timeout=1)
    assert len(channel.jobs) == 2
    assert channel.max_concurrent == 2
    channel.release.set()
    await inbound.idle()
    assert [job.text for job in channel.jobs] == ["one", "other", "two"]
    await inbound.shutdown()


@pytest.mark.asyncio
async def test_duplicate_message_id_and_worker_exception() -> None:
    channel = _RecordingChannel()
    inbound = InboundCoordinator(channel, quiet_window_s=0)
    frag = _frag(text="repeat", message_id="dup")
    assert await inbound.admit(frag) is True
    assert await inbound.admit(_frag(text="repeat", message_id="dup")) is False
    await inbound.idle()
    assert len(channel.jobs) == 1
    channel.fail = True
    await inbound.admit(_frag(text="explode", message_id="x"))
    await inbound.idle()
    await inbound.shutdown()


@pytest.mark.asyncio
async def test_quiet_window_flushes_isolated_media() -> None:
    channel = _RecordingChannel()
    inbound = InboundCoordinator(channel, quiet_window_s=0.02)
    await inbound.admit(_frag(media=(MediaRef(path="/tmp/a.png"),), message_id="solo"))
    assert channel.jobs == []
    await inbound.idle()
    assert len(channel.jobs) == 1
    await inbound.shutdown()


def test_deictic_text_detects_caption_phrases() -> None:
    assert is_deictic_text("分析这张图讲了什么，你能做什么") is True
    assert is_deictic_text("仔细分析这个截图") is True
    assert is_deictic_text("what is in this screenshot?") is True
    assert is_deictic_text("你好") is False
    assert is_deictic_text("这个问题怎么证明") is False


@pytest.mark.asyncio
async def test_batch_holds_timer_while_later_download_is_slow() -> None:
    channel = _RecordingChannel()
    inbound = InboundCoordinator(channel, quiet_window_s=0.03)
    with inbound.batch():
        await inbound.admit(_frag(text="分析这张图讲了什么，你能做什么", message_id="txt"))
        await asyncio.sleep(0.08)
        assert channel.jobs == []
        await inbound.admit(
            _frag(media=(MediaRef(path="/tmp/a.png", kind="image"),), message_id="img")
        )
    await inbound.idle()
    assert len(channel.jobs) == 1
    assert channel.jobs[0].text == "分析这张图讲了什么，你能做什么"
    assert channel.jobs[0].media[0].path == "/tmp/a.png"
    await inbound.shutdown()


@pytest.mark.asyncio
async def test_late_complementary_resets_quiet_timer() -> None:
    channel = _RecordingChannel()
    inbound = InboundCoordinator(channel, quiet_window_s=0.08, media_hold_s=0.08)
    await inbound.admit(_frag(media=(MediaRef(path="/tmp/a.png"),), message_id="img1"))
    await asyncio.sleep(0.05)
    await inbound.admit(_frag(media=(MediaRef(path="/tmp/b.png"),), message_id="img2"))
    await asyncio.sleep(0.05)
    assert channel.jobs == []
    await inbound.admit(_frag(text="这两张一起看", message_id="cap"))
    await inbound.idle()
    assert len(channel.jobs) == 1
    assert len(channel.jobs[0].media) == 2
    assert channel.jobs[0].text == "这两张一起看"
    await inbound.shutdown()


@pytest.mark.asyncio
async def test_deictic_text_holds_longer_than_generic() -> None:
    channel = _RecordingChannel()
    deictic = InboundCoordinator(
        channel, quiet_window_s=0.04, deictic_hold_s=0.16, media_hold_s=0.04
    )
    await deictic.admit(_frag(text="分析这张图讲了什么", message_id="d"))
    await asyncio.sleep(0.08)
    assert channel.jobs == []
    await deictic.admit(_frag(media=(MediaRef(path="/tmp/a.png"),), message_id="img"))
    await deictic.idle()
    assert len(channel.jobs) == 1
    assert channel.jobs[0].text == "分析这张图讲了什么"

    generic = InboundCoordinator(
        channel, quiet_window_s=0.04, deictic_hold_s=0.16, media_hold_s=0.04
    )
    await generic.admit(_frag(text="你好", message_id="g"))
    await asyncio.sleep(0.08)
    assert [job.text for job in channel.jobs[1:]] == ["你好"]
    await deictic.shutdown()
    await generic.shutdown()


@pytest.mark.asyncio
async def test_materialize_holds_caption_while_complementary_download_is_slow() -> None:
    channel = _RecordingChannel()
    inbound = InboundCoordinator(channel, quiet_window_s=0.04, deictic_hold_s=0.04)

    await inbound.admit(_frag(text="分析这张图", message_id="txt"))
    preview = _frag(media=(MediaRef(path="", kind="image"),), message_id="img")

    async def _slow() -> InboundFragment:
        await asyncio.sleep(0.1)
        return _frag(media=(MediaRef(path="/tmp/a.png", kind="image"),), message_id="img")

    fragment = await inbound.materialize_and_admit(preview, _slow)
    assert fragment is not None
    await inbound.idle()
    assert len(channel.jobs) == 1
    assert channel.jobs[0].text == "分析这张图"
    assert channel.jobs[0].media[0].path == "/tmp/a.png"
    await inbound.shutdown()
