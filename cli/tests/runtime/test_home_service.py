"""The home service runtime: cross-workspace scheduling and exactly-once fire.

Proves the two load-bearing behaviours of the home service, fully offline:

* a schedule created in *workspace B* fires while the service runs from the
  neutral *anchor* (``default``) context — the whole point of one machine-level
  service dispatching every registered workspace's schedules; and
* overlapping ticks fire a due occurrence exactly once (claim-then-fire), so a
  not-yet-retired legacy daemon or a raced poll never double-runs a job.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.config.workspaces import register_workspace
from omni.runtime import service_state
from omni.runtime.home_service import HomeService, _has_active_schedules


async def _tasks_for(runtime, skill: str) -> int:  # noqa: ANN001
    tasks = await runtime.list_subtasks(limit=100)
    return sum(1 for t in tasks if t.skill_name == skill)


@pytest.mark.asyncio
async def test_schedule_in_other_workspace_fires_from_anchor(tmp_path):
    ws_b = tmp_path / "workspace_b"
    ws_b.mkdir()
    ws_b_settings = load_settings(cwd=ws_b)
    register_workspace(ws_b_settings.paths)
    ws_b_key = str(ws_b_settings.paths.project_dir)

    # Seed a past-due schedule directly in workspace B's store, then close it so
    # the home service opens its own connection to the same DB.
    seed = await OmniAgent.create(ws_b_settings)
    now = datetime.now(UTC)
    await seed.scheduler.add(
        "cron-fixture", {"input": "go"}, kind="interval", interval_s=3600,
        first_due=now - timedelta(seconds=1),
    )
    await seed.aclose()

    service = HomeService(
        load_settings(project="default"),
        workers=1,
        enable_channels=False,
        reconcile_interval_s=999.0,
    )
    await service.start()
    try:
        assert ws_b_key in service._ws  # workspace B is hosted
        assert service._anchor_key and service._anchor_key != ws_b_key
        assert service._ws[service._anchor_key].channel_manager is None  # channels off

        hosted = service._ws[ws_b_key]
        fired = False
        for _ in range(40):  # ~10s budget; the first poll tick fires immediately
            if await _tasks_for(hosted.agent.runtime, "cron-fixture") >= 1:
                fired = True
                break
            await asyncio.sleep(0.25)
        assert fired, "schedule in workspace B did not fire under the home service"

        # Runtime state advertises the hosted workspace and readiness.
        info = service_state.service_runtime_info(service.paths)
        assert info is not None and info["ready"] is True
        assert any(w["dir"] == ws_b_key for w in info.get("workspaces", []))
    finally:
        await service.stop()

    assert service_state.read_runtime(service.paths) is None  # cleared on stop


def test_has_active_schedules_probe_is_safe_on_missing_db(tmp_path):
    assert _has_active_schedules("") is False
    assert _has_active_schedules(str(tmp_path / "nope.sqlite3")) is False


def test_settings_for_record_mirrors_into_trusted_root(tmp_path):
    """A scheduled task in an already-trusted directory mirrors deliverables back
    into that directory.

    Regression: the home service forced *every* hosted workspace untrusted
    (``trusted=None``), so scheduled/background figures and reports silently
    diverted to the durable ``~/.omni`` store instead of the folder the user ran
    omni in — unlike interactive runs, which honour the persisted trust ledger.
    """
    from omni.agent.orchestrator import _artifact_mirror_dir
    from omni.config import trust as trustmod

    service = HomeService(
        load_settings(project="default"),
        workers=1,
        enable_channels=False,
        reconcile_interval_s=999.0,
    )
    root = tmp_path / "repo"
    root.mkdir()
    record = {"kind": "path", "root": str(root), "name": "repo"}

    # Not yet vouched for → outputs stay in the durable store (no mirror).
    untrusted = service._settings_for_record(record)
    assert untrusted.artifacts.mirror_outputs is False
    assert _artifact_mirror_dir(untrusted) is None

    # After the user trusts the directory the daemon mirrors into it, with
    # output_dir pinned to the absolute root (not the service's own CWD, which is
    # never the workspace for a background daemon).
    trustmod.set_trusted(root, home=service.paths.home)
    trusted = service._settings_for_record(record)
    assert trusted.artifacts.mirror_outputs is True
    assert trusted.artifacts.output_dir == str(root.resolve())
    assert _artifact_mirror_dir(trusted) == root.resolve()


def test_settings_for_record_respects_disabled_trust_gate(tmp_path, monkeypatch):
    """When workspace trust is globally disabled, every hosted root mirrors —
    matching the interactive CLI, which treats ``trust.enabled=false`` as "trust
    everything"."""
    service = HomeService(
        load_settings(project="default"),
        workers=1,
        enable_channels=False,
        reconcile_interval_s=999.0,
    )
    monkeypatch.setattr(service.settings.trust, "enabled", False)
    root = tmp_path / "untracked_repo"
    root.mkdir()

    settings = service._settings_for_record({"kind": "path", "root": str(root)})
    assert settings.artifacts.mirror_outputs is True
    assert settings.artifacts.output_dir == str(root.resolve())


@pytest.mark.asyncio
async def test_workspace_without_schedules_is_not_hosted(tmp_path):
    """The service hosts only the anchor + workspaces with real scheduled work.

    A registered repo with no enabled schedule must not get an idle task runtime
    (that was the source of spurious "worker" rows and wasted resources).
    """
    empty = tmp_path / "empty_ws"
    empty.mkdir()
    empty_settings = load_settings(cwd=empty)
    register_workspace(empty_settings.paths)
    # Materialise the workspace DB (all tables, but zero schedules).
    seed = await OmniAgent.create(empty_settings)
    await seed.aclose()

    service = HomeService(
        load_settings(project="default"),
        workers=1,
        enable_channels=False,
        reconcile_interval_s=999.0,
    )
    await service.start()
    try:
        assert str(empty_settings.paths.project_dir) not in service._ws
        # The anchor itself is always hosted (it owns channels), independent of
        # whether it has schedules.
        assert service._anchor_key in service._ws
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_run_due_is_exactly_once_across_overlapping_ticks():
    agent = await OmniAgent.create(load_settings())
    try:
        now = datetime.now(UTC)
        await agent.scheduler.add(
            "cron-fixture", {"input": "go"}, kind="interval", interval_s=3600,
            first_due=now - timedelta(seconds=1),
        )
        # First tick claims + fires; a second tick at the *same* moment re-reads the
        # advanced next_due_at and fires nothing.
        first = await agent.scheduler.run_due(now=now)
        second = await agent.scheduler.run_due(now=now)
        assert len(first) == 1
        assert second == []
        assert await _tasks_for(agent.runtime, "cron-fixture") == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_run_publishes_starting_identity_before_heavy_initialization():
    """The owner PID must be observable before agent/channel startup can block."""
    seen: dict = {}

    class ProbeService(HomeService):
        async def start(self) -> None:
            seen.update(service_state.read_runtime(self.paths) or {})
            self._stop.set()

        async def stop(self) -> None:
            service_state.clear_runtime_if_owner(self.paths)

    service = ProbeService(
        load_settings(project="default"),
        workers=1,
        enable_channels=False,
        reconcile_interval_s=999.0,
    )
    await service.run()

    assert seen["pid"] == os.getpid()
    assert seen["ready"] is False
    assert seen["phase"] == "starting"
    assert seen["service_id"] == service_state.service_instance_id(service.paths)
    assert seen["instance_id"]
