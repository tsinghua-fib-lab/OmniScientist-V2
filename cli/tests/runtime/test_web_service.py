"""Workspace ``web.pid`` liveness and stale-record cleanup."""

from __future__ import annotations

import os

from omni.config import load_settings
from omni.runtime.web_service import (
    clear_pidfile,
    port_listening,
    read_pidfile,
    web_info,
    write_pidfile,
)


def test_web_info_clears_a_dead_pidfile(tmp_path) -> None:
    settings = load_settings(cwd=tmp_path, trusted=True)
    write_pidfile(settings.paths, pid=2**30, host="127.0.0.1", port=1088)

    assert read_pidfile(settings.paths) is not None
    assert web_info(settings.paths) is None
    assert read_pidfile(settings.paths) is None


def test_web_info_keeps_a_live_pid(tmp_path) -> None:
    settings = load_settings(cwd=tmp_path, trusted=True)
    write_pidfile(settings.paths, pid=os.getpid(), host="127.0.0.1", port=1290)

    info = web_info(settings.paths)
    assert info is not None
    assert info["pid"] == os.getpid()
    assert info["url"] == "http://127.0.0.1:1290"
    clear_pidfile(settings.paths)


def test_port_listening_false_on_unused_port() -> None:
    assert port_listening("127.0.0.1", 1) is False
