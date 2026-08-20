"""Foreground drain must not write a second assistant completion line."""

from __future__ import annotations

from omni.runtime.task_results import persist_detached_skill_notice


def test_persist_detached_skill_notice_only_when_user_is_elsewhere() -> None:
    assert persist_detached_skill_notice(notify_channel="", workflow_run_id="") is False
    assert persist_detached_skill_notice(notify_channel="cli", workflow_run_id="wf-1") is False
    assert persist_detached_skill_notice(notify_channel="wechat", workflow_run_id="") is True
