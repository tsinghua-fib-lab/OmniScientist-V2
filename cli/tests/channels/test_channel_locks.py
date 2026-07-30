"""Home-level per-channel locks: exactly one daemon binds each IM channel.

Channel credentials live under the Omni *home* (``~/.omni/channels``), so two
daemons (one per project, or a stray ghost workspace) would otherwise poll the
same WeChat/Feishu bot and fight over the session (WeChat ``errcode -14``). The
lock makes only the first daemon bind a channel; the rest degrade to task-only.
"""

from __future__ import annotations

import os

from omni.channels import locks


def test_lock_acquire_release_roundtrip(tmp_path):
    channels_dir = tmp_path / "channels"

    lock = locks.acquire(channels_dir, "wechat", project_dir="/x")
    assert lock is not None
    assert (channels_dir / "wechat.lock").is_file()
    # Our own pid is never a *foreign* owner, so the channel is bindable by us.
    assert locks.lock_owner(channels_dir, "wechat") == 0

    locks.release(lock)
    assert not (channels_dir / "wechat.lock").is_file()


def test_lock_blocks_foreign_live_owner(tmp_path, monkeypatch):
    channels_dir = tmp_path / "channels"
    channels_dir.mkdir(parents=True)
    (channels_dir / "feishu.lock").write_text('{"pid": 999999, "ts": 0}', encoding="utf-8")
    monkeypatch.setattr(locks, "pid_alive", lambda pid: pid == 999999)

    assert locks.lock_owner(channels_dir, "feishu") == 999999
    assert locks.acquire(channels_dir, "feishu") is None  # can't steal a live owner


def test_lock_reclaims_stale_dead_owner(tmp_path, monkeypatch):
    channels_dir = tmp_path / "channels"
    channels_dir.mkdir(parents=True)
    (channels_dir / "feishu.lock").write_text('{"pid": 4242, "ts": 0}', encoding="utf-8")
    monkeypatch.setattr(locks, "pid_alive", lambda _pid: False)  # previous owner died

    lock = locks.acquire(channels_dir, "feishu")
    assert lock is not None
    assert lock.pid == os.getpid()


def test_release_leaves_foreign_lock_intact(tmp_path):
    channels_dir = tmp_path / "channels"
    channels_dir.mkdir(parents=True)
    foreign = channels_dir / "wechat.lock"
    foreign.write_text('{"pid": 999999, "ts": 0}', encoding="utf-8")

    locks.release(locks.ChannelLock(path=foreign, pid=os.getpid()))
    assert foreign.is_file()  # not ours → must not be removed
