"""REPL inbox watcher behavior."""

from __future__ import annotations

from types import SimpleNamespace


def test_repl_inbox_watcher_filters_current_cli_session(tmp_path):
    from omni.cli.main import _ReplInboxWatcher

    watcher = _ReplInboxWatcher(SimpleNamespace(project_dir=tmp_path), "sess-1")

    assert watcher._matches({"channel": "cli", "session_id": "sess-1"}) is True
    assert watcher._matches({"channel": "cli", "session_id": ""}) is True
    assert watcher._matches({"channel": "cli", "session_id": "sess-2"}) is False
    assert watcher._matches({"channel": "wechat", "session_id": "sess-1"}) is False

    watcher.set_session("sess-2")
    assert watcher._matches({"channel": "cli", "session_id": "sess-2"}) is True
