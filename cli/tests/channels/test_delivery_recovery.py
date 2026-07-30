"""What happens to a finished result when the chat channel will not take it.

WeChat answers ``ret=-2 prepare failed`` to *every* send for several minutes once
a burst crosses its limit — including the first message of an unrelated reply. On
2026-08-11 a request for "every file from today" queued sixty uploads, the twelfth
was refused, and task 22589d2c's completion notice arrived inside that window. The
figure existed in five formats on disk and the reader was told the task failed.
These tests hold the three behaviours that incident asked for: bound the burst,
stop feeding a refusal, and hand the result over once the window closes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omni.channels.outbound import (
    MAX_DELIVERED_ATTACHMENTS,
    DeliveryEnvelope,
    DeliveryPart,
    DeliveryPartResult,
    DeliveryReport,
    delivery_envelope_from_presentation,
    send_delivery,
)
from omni.runtime.notifications import (
    TaskNotification,
    pending_delivery_retries,
    record_delivery_retry,
    record_delivery_status,
)
from omni.runtime.presentation import ArtifactRef, TaskPresentation, TurnPresentation


def _note(**over) -> TaskNotification:
    fields = {
        "subtask_id": "sub-1",
        "skill_name": "scientific-figure",
        "status": "succeeded",
        "channel": "wechat",
        "external_key": "peer-1",
        "session_id": "sess-1",
        "task_id": "task-1",
    }
    fields.update(over)
    return TaskNotification(**fields)


def _queue_failure(project_dir, note: TaskNotification) -> None:
    record_delivery_status(project_dir, note, status="failed", message="ret=-2 prepare failed")


class _RefusingClient:
    """A peer that refuses every send, the way a blocked iLink account does."""

    def __init__(self) -> None:
        self.sends: list[str] = []

    async def send_rich_text(self, target: str, markdown: str) -> None:
        self.sends.append("rich_text")
        raise RuntimeError("ret=-2 prepare failed")

    async def send_text(self, target: str, text: str) -> None:
        self.sends.append("text")
        raise RuntimeError("ret=-2 prepare failed")

    async def send_file(self, target: str, path: str, file_name: str = "") -> None:
        self.sends.append("file")
        raise RuntimeError("ret=-2 prepare failed")

    async def send_image(self, target: str, path: str, file_name: str = "") -> None:
        self.sends.append("image")
        raise RuntimeError("ret=-2 prepare failed")


@pytest.mark.asyncio
async def test_a_refusing_peer_is_not_offered_every_remaining_file(tmp_path):
    """Each refused upload also spends a text fallback, lengthening the burst."""
    files = []
    for index in range(6):
        path = tmp_path / f"figure-{index}.svg"
        path.write_text("<svg/>", encoding="utf-8")
        files.append(path)
    envelope = DeliveryEnvelope(parts=[
        DeliveryPart(kind="rich_text", text="Result summary", title="OmniScientist"),
        *[DeliveryPart(kind="file", path=str(path), title=path.name) for path in files],
    ])
    client = _RefusingClient()

    report = await send_delivery(client, "peer-1", envelope, allowed_roots=[tmp_path])

    # The reply and the first upload are attempted, each with its one fallback.
    # The remaining five files are not paraded past a peer that just refused
    # both an upload and a plain line of text.
    assert client.sends.count("file") == 1
    assert len(client.sends) == 4
    assert report.failed is True
    skipped = [part for part in report.parts if "not attempted" in part.message]
    assert len(skipped) == 5


@pytest.mark.asyncio
async def test_a_reply_whose_text_will_not_render_still_hands_over_the_file(tmp_path):
    """Text failing for its own reasons says nothing about whether uploads work."""
    document = tmp_path / "report.md"
    document.write_text("# report", encoding="utf-8")
    envelope = DeliveryEnvelope(parts=[
        DeliveryPart(kind="rich_text", text="Result summary", title="OmniScientist"),
        DeliveryPart(kind="file", path=str(document), title=document.name),
    ])

    class _TextOnlyRefusal(_RefusingClient):
        async def send_file(self, target: str, path: str, file_name: str = "") -> None:
            self.sends.append("file")

    client = _TextOnlyRefusal()
    report = await send_delivery(client, "peer-1", envelope, allowed_roots=[tmp_path])

    assert client.sends.count("file") == 1
    assert report.failed is True  # the answer never arrived
    assert any(part.kind == "file" and part.status == "sent" for part in report.parts)


@pytest.mark.asyncio
async def test_one_reply_does_not_queue_a_file_upload_per_task_of_the_day(tmp_path):
    """A per-group cap times a group per task is not a cap at all."""
    groups = []
    for task in range(5):
        artifacts = []
        for index in range(10):
            path = tmp_path / f"t{task}-a{index}.md"
            path.write_text("body", encoding="utf-8")
            artifacts.append(ArtifactRef(title=path.stem, format="md", path=str(path)))
        groups.append(TaskPresentation(
            subtask_id=f"sub-{task}",
            skill="scientific-figure",
            status="succeeded",
            task_id=f"task-{task}",
            artifacts=artifacts,
        ))
    presentation = TurnPresentation(assistant_text="Everything from today", tasks=groups)

    envelope = delivery_envelope_from_presentation(presentation)

    attachments = [part for part in envelope.parts if part.kind in {"file", "image"}]
    assert len(attachments) == MAX_DELIVERED_ATTACHMENTS


def test_a_file_already_attached_is_not_also_sent_as_a_bare_uri(tmp_path):
    """Figure payloads name the SVG twice: as a deliverable and as ``svg_uri``."""
    svg = tmp_path / "figure.svg"
    svg.write_text("<svg/>", encoding="utf-8")
    presentation = TaskPresentation(
        subtask_id="sub-1",
        skill="scientific-figure",
        status="succeeded",
        task_id="task-1",
        artifacts=[
            ArtifactRef(title="Scientific Figure SVG", format="svg", path=str(svg), uri="artifact://abc"),
            ArtifactRef(title="svg_uri", format="svg", uri="artifact://abc"),
        ],
    )

    envelope = delivery_envelope_from_presentation(presentation)

    assert [part.kind for part in envelope.parts] == ["rich_text", "file"]


def test_a_failed_delivery_waits_before_the_first_replay(settings):
    """Replaying immediately spends every attempt inside the refusal window."""
    project_dir = settings.paths.project_dir
    _queue_failure(project_dir, _note())

    assert pending_delivery_retries(project_dir) == []

    later = datetime.now(UTC) + timedelta(seconds=90)
    due = pending_delivery_retries(project_dir, now=later)
    assert [row["task_id"] for row in due] == ["task-1"]


def test_a_delivery_that_keeps_failing_is_eventually_left_to_task_show(settings):
    project_dir = settings.paths.project_dir
    note = _note()
    _queue_failure(project_dir, note)

    # The three backoffs fit inside the half-hour the queue is willing to wait,
    # so this walks the attempts, not the expiry.
    moment = datetime.now(UTC)
    for wait in (61, 301, 901):
        moment += timedelta(seconds=wait)
        due = pending_delivery_retries(project_dir, now=moment)
        assert len(due) == 1
        record_delivery_retry(project_dir, due[0], status="attempted")
        _queue_failure(project_dir, note)  # the replay failed again

    assert pending_delivery_retries(project_dir, now=moment + timedelta(seconds=901)) == []


def test_a_result_nobody_is_waiting_for_anymore_is_not_replayed(settings):
    project_dir = settings.paths.project_dir
    _queue_failure(project_dir, _note())

    stale = datetime.now(UTC) + timedelta(hours=2)
    assert pending_delivery_retries(project_dir, now=stale) == []


def test_a_delivery_that_got_through_leaves_the_queue(settings):
    project_dir = settings.paths.project_dir
    _queue_failure(project_dir, _note())
    later = datetime.now(UTC) + timedelta(seconds=90)

    due = pending_delivery_retries(project_dir, now=later)
    record_delivery_retry(project_dir, due[0], status="sent")

    assert pending_delivery_retries(project_dir, now=later + timedelta(hours=1)) == []


def test_two_tasks_queued_for_the_same_reader_are_replayed_separately(settings):
    project_dir = settings.paths.project_dir
    _queue_failure(project_dir, _note(task_id="task-1", subtask_id="sub-1"))
    _queue_failure(project_dir, _note(task_id="task-2", subtask_id="sub-2"))

    later = datetime.now(UTC) + timedelta(seconds=90)
    due = pending_delivery_retries(project_dir, now=later)

    assert sorted(row["task_id"] for row in due) == ["task-1", "task-2"]


@pytest.mark.asyncio
async def test_the_daemon_hands_over_a_queued_result_once_the_peer_recovers(settings, monkeypatch):
    """The queue was written from the first day and never read; that is the bug."""
    from omni.channels.manager import ChannelManager

    project_dir = settings.paths.project_dir
    _queue_failure(project_dir, _note())

    delivered: list[TaskNotification] = []

    class _RecoveredChannel:
        name = "wechat"

        async def send_task_notification(self, note: TaskNotification) -> str:
            delivered.append(note)
            return "sent"

    manager = ChannelManager(settings, agent=None)  # type: ignore[arg-type]
    manager._channels["wechat"] = _RecoveredChannel()
    clock = _MovableClock(datetime.now(UTC) + timedelta(seconds=90))
    monkeypatch.setattr("omni.runtime.notifications.datetime", clock)

    replayed = await manager.replay_failed_deliveries()

    assert replayed == 1
    assert [note.task_id for note in delivered] == ["task-1"]

    # A result handed over once is handed over once. Waiting past every backoff
    # must not produce a second copy of the same reply.
    clock.advance(timedelta(seconds=1000))
    assert await manager.replay_failed_deliveries() == 0
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_a_channel_the_daemon_no_longer_runs_keeps_its_place_in_the_queue(settings):
    from omni.channels.manager import ChannelManager

    project_dir = settings.paths.project_dir
    _queue_failure(project_dir, _note())

    manager = ChannelManager(settings, agent=None)  # type: ignore[arg-type]

    assert await manager.replay_failed_deliveries() == 0
    later = datetime.now(UTC) + timedelta(seconds=90)
    assert len(pending_delivery_retries(project_dir, now=later)) == 1


@pytest.mark.asyncio
async def test_a_reader_who_never_got_the_reply_is_not_told_the_work_failed(settings):
    """The figure existed in five formats; only the last hop failed."""
    from omni.channels.base import Channel

    settled: list[dict] = []

    class _Recorder:
        async def get_task(self, task_id: str):
            from types import SimpleNamespace

            return SimpleNamespace(
                id=task_id,
                status="running",
                submitted_workflow_ids=[],
                submitted_subtask_ids=[],
            )

        async def settle_task(self, task_id: str, **kwargs) -> str:
            settled.append({"task_id": task_id, **kwargs})
            return str(kwargs.get("proposed_status") or "")

        async def list_events(self, task_id: str):
            return []

    class _Agent:
        def __init__(self) -> None:
            self.tasks = _Recorder()

    class _Chan(Channel):
        name = "wechat"

        async def start(self) -> None:
            return None

        async def send_turn(self, external_key, presentation):  # noqa: ANN001
            return DeliveryReport(target=external_key, parts=[])

    channel = _Chan(settings, _Agent())  # type: ignore[arg-type]

    await channel._settle_after_presentation("task-1", delivery_status="failed")

    assert settled and settled[0]["proposed_status"] == "degraded"
    assert not settled[0].get("error")


class _MovableClock:
    """A ``datetime`` stand-in so backoff arithmetic needs no real waiting."""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def advance(self, delta: timedelta) -> None:
        self._moment += delta

    def now(self, tz=None):  # noqa: ANN001
        return self._moment if tz else self._moment.replace(tzinfo=None)

    def fromisoformat(self, text: str) -> datetime:
        return datetime.fromisoformat(text)


def test_an_unknown_retry_status_is_refused(settings):
    with pytest.raises(ValueError):
        record_delivery_retry(settings.paths.project_dir, {"channel": "wechat"}, status="maybe")


def test_a_part_result_carries_the_reason_it_was_not_attempted():
    result = DeliveryPartResult(kind="file", status="failed", message="not attempted: refused")
    assert DeliveryReport(target="peer", parts=[result]).degraded is True
    assert DeliveryReport(target="peer", parts=[result]).failed is False
