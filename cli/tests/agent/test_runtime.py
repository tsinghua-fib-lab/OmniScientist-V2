"""Task runtime: enqueue → drain → result + notification + recovery."""

from __future__ import annotations

import asyncio
import copy
import os
import sys
from uuid import uuid4

import pytest

from omni.agent.plan_revision import (
    provider_authority_renewal_chain_is_valid,
    provider_authority_renewal_is_valid,
)
from omni.config import load_settings
from omni.runtime.notifications import InboxNotifier, TaskNotification, delivery_key
from omni.runtime.subtask_recovery import retry_subtask as retry_subtask_recovery
from omni.runtime.subtask_runtime import SubtaskRuntime
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM, TaskORM


def test_persisted_task_result_message_hides_dot_sources():
    from omni.runtime.subtask_runtime import _task_result_message

    message = _task_result_message(
        "execution-987654",
        "scientific-figure",
        "figure done",
        [
            {"label": "PNG", "path": "/tmp/figure.png", "uri": "artifact://png"},
            {"label": "DOT source", "path": "/tmp/figure.dot", "uri": "artifact://dot"},
        ],
        task_id="task-123456",
    )

    assert "task `task-123`" in message
    assert "execution `executio`" in message
    assert "/task show task-123" in message
    assert "/task attach task-123" in message
    assert "/task attach executio" not in message
    assert "/tmp/figure.png" in message
    assert "artifact://png" not in message
    assert "DOT source" not in message
    assert "/tmp/figure.dot" not in message
    assert "artifact://dot" not in message


def test_persisted_result_without_owner_does_not_emit_incomplete_task_command():
    from omni.runtime.subtask_runtime import _task_result_message

    message = _task_result_message(
        "execution-987654",
        "scientific-figure",
        "figure failed",
        [{"label": "trace", "uri": "artifact://trace"}],
    )

    assert "execution `executio`" in message
    assert "/task show " not in message
    assert "/task attach " not in message


@pytest.mark.asyncio
async def test_inbox_and_delivery_key_accept_lone_surrogates(tmp_path):
    notifier = InboxNotifier(tmp_path / "inbox.jsonl")
    note = TaskNotification(
        subtask_id="task-1",
        skill_name="echo",
        status="succeeded",
        summary="bad\udc80name 中文",
    )

    await notifier.notify(note)

    assert notifier.read_all()[0]["summary"] == note.summary
    assert delivery_key(
        channel="cli",
        external_key="bad\udc80key",
        kind="task",
        task_id="task-1",
    ) == delivery_key(
        channel="cli",
        external_key="bad\udc80key",
        kind="task",
        task_id="task-1",
    )


def _echo_skill():
    script = "import sys,json;d=json.load(sys.stdin);print(json.dumps({'status':'ok','summary':'did '+str(d.get('q'))}))"
    return SkillEntry(
        name="echo_task", description="d", kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
    )


async def _runtime():
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    reg = SkillRegistry(s)
    reg.build_index()
    reg.register(_echo_skill())
    inbox = InboxNotifier(s.paths.project_dir / "inbox.jsonl")

    def ctx_factory(session_id, channel):
        return ExecContext(settings=s, paths=s.paths, session_id=session_id, channel=channel)

    return SubtaskRuntime(db, s, reg, ctx_factory, notifier=inbox), inbox


@pytest.mark.asyncio
async def test_enqueue_and_drain():
    rt, inbox = await _runtime()
    tid = await rt.enqueue("echo_task", {"q": "X"}, "cli")
    processed = await rt.drain()
    assert tid in processed
    task = await rt.get_subtask(tid)
    assert task.status == "succeeded"
    assert "did X" in task.result_json["summary"]
    notes = inbox.read_all()
    assert any(n["subtask_id"] == tid and n["status"] == "succeeded" for n in notes)


@pytest.mark.asyncio
async def test_running_one_skill_leaves_the_callers_context_unsealed():
    """A turn's context outlives any one execution, so nothing may be sealed on it.

    ``run_skill`` hands the live ReAct context straight to the runtime. When the
    runtime assigned the executing provider's identity onto *that* object, the
    seal outlived the execution and the next skill on the same turn was checked
    against the previous skill's fingerprint — reported, wrongly, as the provider
    having been rewritten after enqueue.
    """
    rt, _ = await _runtime()
    s = load_settings()
    turn_ctx = ExecContext(settings=s, paths=s.paths, channel="cli")

    subtask_id = await rt.enqueue("echo_task", {"q": "A"}, "cli")
    await rt.process(subtask_id, ctx_override=turn_ctx)

    task = await rt.get_subtask(subtask_id)
    assert task is not None and task.status == "succeeded"
    # The execution ran under its own sealed authority...
    assert task.provider_authority_json
    # ...and left no trace of it on the caller's context.
    assert not turn_ctx.provider_authority
    assert turn_ctx.subtask_id == ""
    assert turn_ctx.workflow_run_id == ""


@pytest.mark.asyncio
async def test_background_worker_processes_enqueued_task():
    rt, _ = await _runtime()
    await rt.start(workers=1, poll_interval=0)
    try:
        tid = await rt.enqueue("echo_task", {"q": "worker"}, "cli")
        await asyncio.wait_for(rt._queue.join(), timeout=2)  # noqa: SLF001

        task = await rt.get_subtask(tid)
        assert task is not None and task.status == "succeeded"
        assert task.result_json["summary"] == "did worker"
    finally:
        await rt.stop()


@pytest.mark.asyncio
async def test_task_notification_carries_channel_external_key():
    rt, inbox = await _runtime()
    from omni.storage.models import SessionORM

    async with rt._db.session() as s:
        s.add(SessionORM(id="sess-im", channel="feishu", external_key="chat-42"))
        await s.commit()

    tid = await rt.enqueue("echo_task", {"q": "IM"}, "feishu", session_id="sess-im")
    await rt.drain()

    note = next(n for n in inbox.read_all() if n["subtask_id"] == tid)
    assert note["channel"] == "feishu"
    assert note["session_id"] == "sess-im"
    assert note["external_key"] == "chat-42"


@pytest.mark.asyncio
async def test_failed_task_records_error():
    rt, inbox = await _runtime()
    tid = await rt.enqueue("missing_skill", {}, "cli")
    await rt.drain()
    task = await rt.get_subtask(tid)
    assert task.status == "failed"
    assert "unknown skill" in task.error


@pytest.mark.asyncio
async def test_retry_and_resume_record_recovery_schema():
    rt, _ = await _runtime()
    tid = await rt.enqueue("missing_skill", {}, "cli")
    await rt.drain()
    failed = await rt.get_subtask(tid)
    assert failed.status == "failed"

    retry_id = await rt.retry_subtask(tid)
    retry = await rt.get_subtask(retry_id)
    assert retry.retry_of == tid
    assert "unknown skill" in retry.original_error
    assert retry.recovery_attempt == 1
    assert retry.recovery_policy == "retry_fresh_execution"
    assert provider_authority_renewal_is_valid(
        retry.provider_authority_json["authority_renewal"]
    )

    assert await rt.resume_subtask(tid) is True
    resumed = await rt.get_subtask(tid)
    assert resumed.status == "recovering"
    assert resumed.resume_of == tid
    assert "unknown skill" in resumed.original_error
    assert resumed.recovery_attempt == 1
    assert resumed.recovery_policy == "requeue_in_place"
    assert provider_authority_renewal_is_valid(
        resumed.provider_authority_json["authority_renewal"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["retry", "resume"])
async def test_standalone_recovery_dispatch_rejects_tampered_renewal_chain(
    recovery: str,
) -> None:
    rt, _ = await _runtime()
    subtask_id = await rt.enqueue("echo_task", {"q": recovery}, "cli")
    async with rt._db.session() as session:  # noqa: SLF001
        task = await session.get(SubtaskORM, subtask_id)
        assert task is not None
        task.status = "failed"
        task.error = "fixture failure"
        await session.commit()

    if recovery == "retry":
        execution_id = await rt.retry_subtask(subtask_id)
    else:
        assert await rt.resume_subtask(subtask_id) is True
        execution_id = subtask_id

    async with rt._db.session() as session:  # noqa: SLF001
        task = await session.get(SubtaskORM, execution_id)
        assert task is not None
        authority = copy.deepcopy(task.provider_authority_json)
        authority["provider_authority_renewals"][-1][
            "previous_fingerprint"
        ] = "forged-root"
        task.provider_authority_json = authority
        await session.commit()

    await rt.process(execution_id)

    execution = await rt.get_subtask(execution_id)
    assert execution is not None and execution.status == "failed"
    assert "renewal chain is invalid" in execution.error


@pytest.mark.asyncio
async def test_repeated_standalone_resume_preserves_root_and_contiguous_chain():
    rt, _ = await _runtime()
    tid = await rt.enqueue("echo_task", {"q": "recover twice"}, "cli")
    from sqlalchemy import update

    async with rt._db.session() as session:  # noqa: SLF001
        await session.execute(
            update(SubtaskORM)
            .where(SubtaskORM.id == tid)
            .values(status="failed", error="first failure")
        )
        await session.commit()
    original = await rt.get_subtask(tid)
    original_authority = dict(original.provider_authority_json)

    assert await rt.resume_subtask(tid) is True
    first = await rt.get_subtask(tid)
    first_link = first.provider_authority_json["authority_renewal"]

    async with rt._db.session() as session:  # noqa: SLF001
        await session.execute(
            update(SubtaskORM)
            .where(SubtaskORM.id == tid)
            .values(status="failed", error="second failure")
        )
        await session.commit()

    assert await rt.resume_subtask(tid) is True
    second = await rt.get_subtask(tid)
    authority = second.provider_authority_json
    renewals = authority["provider_authority_renewals"]

    assert authority["provider_authority_root"] == original_authority
    assert [item["action"] for item in renewals] == [
        f"requeue_subtask:{tid}",
        f"requeue_subtask:{tid}",
    ]
    assert renewals[0] == first_link
    assert renewals[1]["previous_fingerprint"] == renewals[0]["fingerprint"]
    assert provider_authority_renewal_chain_is_valid(
        {
            **authority["provider_authority_root"],
            "provider_authority_renewals": renewals,
        }
    )


class _BlockingRecoveryRecorder:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.reopens = 0

    async def reopen_task_for_recovery(self, *_args, **_kwargs) -> None:
        self.reopens += 1
        if self.reopens == 1:
            self.entered.set()
            await self.release.wait()

    async def append_event(self, *_args, **_kwargs) -> None:
        return None


class _BrokenRecoveryRecorder:
    async def reopen_task_for_recovery(self, *_args, **_kwargs) -> None:
        raise RuntimeError("audit sink unavailable")

    async def append_event(self, *_args, **_kwargs) -> None:
        return None


class _BrokenSubmissionAndRecoveryRecorder:
    def __init__(self) -> None:
        self.submissions = 0
        self.reopens = 0

    async def record_subtask_submitted(self, *_args, **_kwargs) -> None:
        self.submissions += 1
        raise RuntimeError("submission audit unavailable")

    async def reopen_task_for_recovery(self, *_args, **_kwargs) -> None:
        self.reopens += 1
        raise RuntimeError("recovery audit unavailable")

    async def append_event(self, *_args, **_kwargs) -> None:
        return None


async def _failed_retry_fixture(rt: SubtaskRuntime) -> SubtaskORM:
    tid = await rt.enqueue("echo_task", {"q": "retry once"}, "cli")
    from sqlalchemy import update

    async with rt._db.session() as session:  # noqa: SLF001
        await session.execute(
            update(SubtaskORM)
            .where(SubtaskORM.id == tid)
            .values(status="failed", error="retry fixture")
        )
        await session.commit()
    original = await rt.get_subtask(tid)
    original.task_id = "detached-owner-for-recorder"
    return original


def _retry_enqueue_fixture(rt: SubtaskRuntime, calls: list[str]):
    async def enqueue(
        skill_name: str,
        input_data: dict,
        notify_channel: str,
        **kwargs,
    ) -> str:
        calls.append(skill_name)
        subtask_id = str(kwargs.get("subtask_id") or uuid4().hex)
        async with rt._db.session() as session:  # noqa: SLF001
            session.add(
                SubtaskORM(
                    id=subtask_id,
                    skill_name=skill_name,
                    status="scheduled",
                    input_json=dict(input_data),
                    provider_authority_json={},
                    notify_channel=notify_channel,
                    retry_of=str(kwargs.get("retry_of") or ""),
                )
            )
            await session.commit()
        return subtask_id

    return enqueue


@pytest.mark.asyncio
async def test_retry_claim_remains_idempotent_until_recorder_finishes():
    rt, _ = await _runtime()
    original = await _failed_retry_fixture(rt)
    calls: list[str] = []
    enqueue = _retry_enqueue_fixture(rt, calls)
    recorder = _BlockingRecoveryRecorder()

    first = asyncio.create_task(
        retry_subtask_recovery(
            db=rt._db,  # noqa: SLF001
            original=original,
            enqueue=enqueue,
            task_recorder=recorder,
            notify_channel=None,
        )
    )
    await recorder.entered.wait()
    second = await retry_subtask_recovery(
        db=rt._db,  # noqa: SLF001
        original=original,
        enqueue=enqueue,
        task_recorder=recorder,
        notify_channel=None,
    )
    recorder.release.set()
    first_id = await first

    assert first_id == second
    assert calls == ["echo_task"]


@pytest.mark.asyncio
async def test_retry_recorder_failure_returns_persisted_execution_without_duplicate():
    rt, _ = await _runtime()
    original = await _failed_retry_fixture(rt)
    calls: list[str] = []
    enqueue = _retry_enqueue_fixture(rt, calls)

    first_id = await retry_subtask_recovery(
        db=rt._db,  # noqa: SLF001
        original=original,
        enqueue=enqueue,
        task_recorder=_BrokenRecoveryRecorder(),
        notify_channel=None,
    )
    second_id = await retry_subtask_recovery(
        db=rt._db,  # noqa: SLF001
        original=original,
        enqueue=enqueue,
        task_recorder=None,
        notify_channel=None,
    )

    assert first_id == second_id
    assert calls == ["echo_task"]


@pytest.mark.asyncio
async def test_retry_is_durable_when_submission_and_recovery_recorders_fail():
    rt, _ = await _runtime()
    owner_id = uuid4().hex
    async with rt._db.session() as session:  # noqa: SLF001
        session.add(TaskORM(id=owner_id, status="failed", kind="turn"))
        await session.commit()
    original_id = await rt.enqueue(
        "echo_task",
        {"q": "durable retry"},
        "cli",
        task_id=owner_id,
    )
    from sqlalchemy import select, update

    async with rt._db.session() as session:  # noqa: SLF001
        await session.execute(
            update(SubtaskORM)
            .where(SubtaskORM.id == original_id)
            .values(status="failed", error="retry fixture")
        )
        await session.commit()
    recorder = _BrokenSubmissionAndRecoveryRecorder()
    rt.set_task_recorder(recorder)

    first_id = await rt.retry_subtask(original_id)
    second_id = await rt.retry_subtask(original_id)
    async with rt._db.session() as session:  # noqa: SLF001
        retries = list(
            (
                await session.execute(
                    select(SubtaskORM).where(
                        SubtaskORM.retry_of == original_id
                    )
                )
            ).scalars()
        )

    assert first_id == second_id
    assert [row.id for row in retries] == [first_id]
    assert recorder.submissions == 1
    assert recorder.reopens == 1


@pytest.mark.asyncio
async def test_retry_persists_renewed_authority_before_worker_can_observe_it():
    rt, _ = await _runtime()
    original = await _failed_retry_fixture(rt)
    original.task_id = ""
    original_authority = dict(original.provider_authority_json)
    observed: list[dict] = []

    async def observe_enqueued(subtask_id: str, *, kind: str) -> None:
        assert kind == "subtask"
        row = await rt.get_subtask(subtask_id)
        assert row is not None
        observed.append(dict(row.provider_authority_json))

    rt._enqueue_local = observe_enqueued  # type: ignore[method-assign]  # noqa: SLF001
    rt._running = True  # noqa: SLF001
    try:
        retry_id = await retry_subtask_recovery(
            db=rt._db,  # noqa: SLF001
            original=original,
            enqueue=rt.enqueue,
            task_recorder=None,
            notify_channel=None,
        )
    finally:
        rt._running = False  # noqa: SLF001

    assert retry_id
    assert len(observed) == 1
    authority = observed[0]
    assert authority["provider_authority_root"] == original_authority
    assert provider_authority_renewal_chain_is_valid(
        {
            **authority["provider_authority_root"],
            "provider_authority_renewals": authority[
                "provider_authority_renewals"
            ],
        }
    )


@pytest.mark.asyncio
async def test_running_subtask_cannot_be_retried_or_resumed():
    rt, _ = await _runtime()
    tid = await rt.enqueue("echo_task", {"q": "in-flight"}, "cli")
    from sqlalchemy import update

    from omni.storage.models import SubtaskORM

    async with rt._db.session() as session:  # noqa: SLF001
        await session.execute(
            update(SubtaskORM)
            .where(SubtaskORM.id == tid)
            .values(status="running", owner_pid=os.getpid())
        )
        await session.commit()

    with pytest.raises(ValueError, match="only terminal"):
        await rt.retry_subtask(tid)
    assert await rt.resume_subtask(tid) is False
    current = await rt.get_subtask(tid)
    assert current is not None and current.status == "running"


@pytest.mark.asyncio
async def test_drain_claims_recovering_tasks():
    rt, _ = await _runtime()
    tid = await rt.enqueue("echo_task", {"q": "recover"}, "cli")

    from sqlalchemy import update

    from omni.storage.models import SubtaskORM
    async with rt._db.session() as s:
        await s.execute(update(SubtaskORM).where(SubtaskORM.id == tid).values(status="recovering"))
        await s.commit()

    processed = await rt.drain()
    task = await rt.get_subtask(tid)

    assert tid in processed
    assert task.status == "succeeded"
    assert "did recover" in task.result_json["summary"]


@pytest.mark.asyncio
async def test_recover_resets_running():
    rt, _ = await _runtime()
    tid = await rt.enqueue("echo_task", {"q": "Y"}, "cli")
    # simulate crash: mark running
    from sqlalchemy import update

    from omni.storage.models import SubtaskORM
    async with rt._db.session() as s:
        await s.execute(
            update(SubtaskORM)
            .where(SubtaskORM.id == tid)
            .values(status="running", owner_pid=2_147_483_647)
        )
        await s.commit()
    n = await rt.recover()
    assert n >= 1
    task = await rt.get_subtask(tid)
    # Standalone orphans settle to a terminal state; they are not silently
    # re-queued (non-replay-safe skills must not rerun without the user).
    assert task.status == "interrupted"
