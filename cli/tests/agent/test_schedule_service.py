"""ScheduleService: one contract, one time-policy, durable approval by id.

This is the structural fix for the ``omni schedule add --at`` incident. It pins
the invariants that make that class of bug impossible to reship:

* **CLI parity** — the deterministic ``to_cli_argv`` serialiser round-trips
  through the *real* Typer parser, so a surfaced fallback command always parses
  (the incident was a model hand-composing a flag that did not exist).
* **One time-policy** — a naive ``at`` is the operator's local wall-clock on both
  the tool and CLI paths (never UTC on one and local on the other), and a
  one-time trigger already in the past returns structured ``needs_input`` instead
  of being silently created.
* **Durable approval** — an IM-originated request with no local approver becomes
  a persisted proposal that a later local ``approve <id>`` executes from the
  *stored, digest-checked* payload, idempotently.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from omni.agent import OmniAgent
from omni.cli.main import app
from omni.config import load_settings
from omni.scheduling.contracts import (
    STATUS_AWAITING_APPROVAL,
    STATUS_CREATED,
    STATUS_NEEDS_INPUT,
    STATUS_REJECTED,
    ScheduleActor,
    ScheduleCreateRequest,
    cron_trigger,
    interval_trigger,
    once_trigger,
    resolve_once_instant,
    to_cli_argv,
)
from omni.scheduling.service import ScheduleService, cli_fallback_command

runner = CliRunner()


def _future_local_iso(hours: int = 3) -> str:
    """A naive local ISO timestamp `hours` in the future (no tz suffix)."""
    return (datetime.now().astimezone().replace(tzinfo=None) + timedelta(hours=hours)).isoformat(
        timespec="minutes"
    )


async def _service(agent: OmniAgent) -> ScheduleService:
    return ScheduleService(agent.db, agent.runtime, agent.settings, registry=agent.registry)


# ── L1/L2: one contract, deterministic CLI parity ──


def test_to_cli_argv_round_trips_through_the_real_cli_parser(omni_home):
    """Every serialised request parses under the real Typer command.

    Guards the incident directly: a generated ``omni schedule add`` command can
    never contain an option the parser does not accept.
    """
    project = "argv-parity"
    requests = [
        ScheduleCreateRequest(trigger=cron_trigger("0 18 * * *"), goal="daily digest", title="d"),
        ScheduleCreateRequest(trigger=interval_trigger(3600), goal="hourly pulse"),
        ScheduleCreateRequest(trigger=once_trigger(_future_local_iso()), goal="one-off summary"),
    ]
    for req in requests:
        argv = to_cli_argv(req)
        assert argv[:2] == ["schedule", "add"]
        res = runner.invoke(app, ["--project", project, *argv])
        assert res.exit_code == 0, f"{argv} -> {res.output}"
    # The human-facing fallback string is shell-safe and starts with `omni`.
    assert cli_fallback_command(requests[0]).startswith("omni schedule add ")


def test_canonical_payload_digest_is_stable_and_replayable():
    req = ScheduleCreateRequest(
        trigger=once_trigger("2099-01-01T09:00"), goal="x", title="t",
        actor=ScheduleActor(channel="wechat", session_id="s1", principal="wechat:abc"),
    )
    # Digest is order-independent and reconstructs an identical request.
    assert req.digest() == ScheduleCreateRequest.from_payload(req.canonical_payload()).digest()


# ── L4: one time-policy (naive == local; past == needs_input) ──


def test_naive_at_is_interpreted_as_operator_local_not_utc():
    now = datetime(2026, 7, 28, 0, 0, tzinfo=UTC)
    # Reference: what the shared policy yields for the same naive string.
    expected_utc, _label, err = resolve_once_instant("2026-07-28T10:00", "", now=now)
    assert err == ""
    # A bare datetime is local wall-clock (matches astimezone), never UTC-at-face.
    same_but_utc, _l2, _e2 = resolve_once_instant("2026-07-28T10:00+00:00", "", now=now)
    if datetime.now().astimezone().utcoffset() not in (None, timedelta(0)):
        assert expected_utc != same_but_utc


def test_explicit_timezone_is_honoured():
    due, label, err = resolve_once_instant("2099-01-01T09:00", "Asia/Shanghai")
    assert err == "" and label == "Asia/Shanghai"
    # 09:00 Shanghai == 01:00 UTC.
    assert due == datetime(2099, 1, 1, 1, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_past_one_time_trigger_returns_needs_input_with_recovery():
    agent = await OmniAgent.create(load_settings())
    try:
        service = await _service(agent)
        past = (datetime.now().astimezone().replace(tzinfo=None) - timedelta(days=1)).isoformat()
        result = await service.create(
            ScheduleCreateRequest(trigger=once_trigger(past), goal="late digest")
        )
        assert result.status == STATUS_NEEDS_INPUT
        assert "past" in (result.error or "").lower()
        assert {c["id"] for c in result.recovery_choices} >= {"future_time", "run_now"}
        # Nothing was created.
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


# ── L1: create-vs-propose consent ──


@pytest.mark.asyncio
async def test_local_cli_request_is_created_directly():
    agent = await OmniAgent.create(load_settings())
    try:
        service = await _service(agent)
        result = await service.create(
            ScheduleCreateRequest(
                trigger=cron_trigger("0 9 * * *"), goal="morning digest",
                actor=ScheduleActor(channel="cli", principal="local"),
            )
        )
        assert result.status == STATUS_CREATED
        assert result.schedule_id and result.spec == "cron 0 9 * * *"
        assert len(await agent.scheduler.list()) == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_unknown_skill_is_rejected_before_scheduling():
    agent = await OmniAgent.create(load_settings())
    try:
        service = await _service(agent)
        result = await service.create(
            ScheduleCreateRequest(trigger=interval_trigger(60), skill_name="no-such-skill")
        )
        assert result.status == STATUS_REJECTED
        assert "no-such-skill" in (result.error or "")
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


# ── L3: durable approval proposal + resume-by-id ──


@pytest.mark.asyncio
async def test_im_request_becomes_a_durable_proposal_and_approve_creates_it():
    agent = await OmniAgent.create(load_settings())
    try:
        service = await _service(agent)
        req = ScheduleCreateRequest(
            trigger=cron_trigger("0 18 * * *"), goal="daily research digest",
            actor=ScheduleActor(channel="wechat", session_id="s1", principal="wechat:peer"),
        )
        proposed = await service.create(req)
        assert proposed.status == STATUS_AWAITING_APPROVAL
        assert proposed.proposal_id and proposed.approve_command.startswith("omni schedule approve ")
        # No schedule exists yet — it only awaits the owner.
        assert await agent.scheduler.list() == []

        # Idempotent proposal: an identical re-request converges, not duplicates.
        again = await service.create(req)
        assert again.proposal_id == proposed.proposal_id
        assert len(await service.list_proposals()) == 1

        approved = await service.approve(proposed.proposal_id, decided_by="local")
        assert approved.status == STATUS_CREATED and approved.schedule_id
        rows = await agent.scheduler.list()
        assert len(rows) == 1 and rows[0].cron_expr == "0 18 * * *"

        # Replayed approval converges on the same schedule (no second row).
        replay = await service.approve(proposed.proposal_id)
        assert replay.schedule_id == approved.schedule_id
        assert len(await agent.scheduler.list()) == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_denied_proposal_creates_nothing():
    agent = await OmniAgent.create(load_settings())
    try:
        service = await _service(agent)
        proposed = await service.create(
            ScheduleCreateRequest(
                trigger=interval_trigger(3600), goal="hourly digest",
                actor=ScheduleActor(channel="feishu", principal="feishu:peer"),
            )
        )
        assert proposed.status == STATUS_AWAITING_APPROVAL
        denied = await service.deny(proposed.proposal_id)
        assert denied.status == STATUS_REJECTED
        assert await agent.scheduler.list() == []
        # A denied proposal cannot then be approved.
        after = await service.approve(proposed.proposal_id)
        assert after.status == STATUS_REJECTED
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_tampered_proposal_payload_is_rejected_on_approval():
    agent = await OmniAgent.create(load_settings())
    try:
        service = await _service(agent)
        proposed = await service.create(
            ScheduleCreateRequest(
                trigger=cron_trigger("0 6 * * *"), goal="dawn digest",
                actor=ScheduleActor(channel="dingtalk", principal="dingtalk:peer"),
            )
        )
        # Tamper with the stored payload so its digest no longer matches. The
        # proposal lives in the machine-global control store, not the workspace DB.
        from omni.storage.db import get_database
        from omni.storage.models import ScheduleActionProposalORM

        store = get_database(agent.paths.control_db)
        await store.init()
        async with store.session() as s:
            row = await s.get(ScheduleActionProposalORM, proposed.proposal_id)
            payload = dict(row.payload_json)
            payload["input"] = {"input": "exfiltrate secrets"}
            row.payload_json = payload
            await s.commit()

        result = await service.approve(proposed.proposal_id)
        assert result.status == STATUS_REJECTED
        assert "integrity" in (result.error or "").lower()
        assert await agent.scheduler.list() == []
    finally:
        await agent.aclose()


# ── the incident: a proposal is machine-owner state, not workspace state ──


@pytest.mark.asyncio
async def test_proposal_is_approvable_from_a_different_workspace(omni_home, tmp_path):
    """The regression this fixes: WeChat is served on the ``default`` anchor, but
    the owner approves from their repo. Creation and approval resolve to different
    workspace DBs, so a per-workspace proposal store made ``approve`` miss it
    ("No pending schedule proposal matches"). Proposals now live in the machine-
    global control store, and the approved schedule materialises back into the
    *originating* workspace (so an IM result still returns to that channel).

    The approving side is keyed off a repository directory rather than named with
    ``--project``, because that is what the owner's side of the incident was and
    the two resolve by different rules: a named project is a directory the store
    owns, a repo is a workspace hashed from a path outside it. Two named projects
    exercised one rule twice and could not have caught a control store that
    resolved per workspace *kind*. Nor could this test be written at all until
    the home moved to the shipping shape: pointed at a bare temp directory, a
    repo underneath it is inside the store, and resolution sends it to the
    ``default`` named project instead of hashing its path.
    """
    checkout = tmp_path / "repo"
    (checkout / ".git").mkdir(parents=True)

    anchor = await OmniAgent.create(load_settings(project="anchor"))
    repo = await OmniAgent.create(load_settings(cwd=checkout))
    try:
        assert anchor.settings.paths.project_dir.parent.name == "projects"
        assert repo.settings.paths.project_dir.parent.name == "workspaces"
        # Created by the IM anchor workspace (as the daemon would).
        svc_anchor = await _service(anchor)
        proposed = await svc_anchor.create(
            ScheduleCreateRequest(
                trigger=cron_trigger("0 18 * * *"), goal="daily research digest",
                actor=ScheduleActor(channel="wechat", session_id="s1", principal="wechat:peer"),
            )
        )
        assert proposed.status == STATUS_AWAITING_APPROVAL and proposed.proposal_id

        # Approved from an unrelated workspace (the owner's repo) — previously the
        # dead-end. It must both *see* and *approve* the proposal.
        svc_repo = await _service(repo)
        assert any(p.id == proposed.proposal_id for p in await svc_repo.list_proposals())
        approved = await svc_repo.approve(proposed.proposal_id, decided_by="local")
        assert approved.status == STATUS_CREATED and approved.schedule_id

        # The schedule lands in the ORIGIN (anchor) workspace, where its runtime
        # fires it and its channel delivers the result — not the approving repo.
        assert len(await anchor.scheduler.list()) == 1
        assert await repo.scheduler.list() == []
    finally:
        await repo.aclose()
        await anchor.aclose()


@pytest.mark.asyncio
async def test_legacy_workspace_proposal_is_migrated_into_the_control_store(omni_home):
    """A pending proposal written by a pre-upgrade build (per-workspace store) is
    swept into the machine-global store on first touch, so it is not orphaned."""
    agent = await OmniAgent.create(load_settings(project="legacy"))
    try:
        from omni.storage.models import ScheduleActionProposalORM, _utcnow

        req = ScheduleCreateRequest(
            trigger=cron_trigger("0 6 * * *"), goal="dawn digest",
            actor=ScheduleActor(channel="wechat", session_id="s1", principal="wechat:peer"),
        )
        # Simulate the old location: a pending proposal in the workspace DB.
        async with agent.db.session() as s:
            s.add(
                ScheduleActionProposalORM(
                    id="legacy01deadbeef", project="legacy", channel="wechat",
                    actor_principal="wechat:peer", kind="schedule_create", title="dawn",
                    payload_json=req.canonical_payload(), payload_digest=req.digest(),
                    state="pending", expires_at=_utcnow() + timedelta(hours=24),
                )
            )
            await s.commit()

        service = await _service(agent)
        # First touch migrates it; approve then works from the control store.
        assert any(p.id == "legacy01deadbeef" for p in await service.list_proposals())
        approved = await service.approve("legacy01", decided_by="local")
        assert approved.status == STATUS_CREATED
    finally:
        await agent.aclose()


# ── L6: truthful readiness in the summary ──


@pytest.mark.asyncio
async def test_created_summary_is_honest_about_runner_and_autonomy():
    agent = await OmniAgent.create(load_settings())
    try:
        service = await _service(agent)
        result = await service.create(
            ScheduleCreateRequest(
                trigger=cron_trigger("0 9 * * *"), goal="digest",
                actor=ScheduleActor(channel="cli", principal="local"),
            )
        )
        assert result.status == STATUS_CREATED
        # The deterministic summary never over-promises: with no runner up it
        # tells the owner how to actually make it fire, rather than "it will run".
        assert result.summary
        assert result.registered is True
        assert result.runner_ready in (False, None)
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_origin_scheduler_does_not_create_sqlite_in_the_checkout(omni_home, tmp_path):
    """A legacy origin that stored workspace_root must not grow <repo>/sessions.sqlite3."""
    checkout = tmp_path / "repo"
    (checkout / ".git").mkdir(parents=True)
    leaked = checkout / "sessions.sqlite3"

    agent = await OmniAgent.create(load_settings(cwd=checkout))
    try:
        service = await _service(agent)
        await service._origin_scheduler(str(checkout))
        assert not leaked.exists()
        assert agent.paths.project_dir != checkout
        assert (agent.paths.project_dir / "sessions.sqlite3").is_file()
    finally:
        await agent.aclose()
