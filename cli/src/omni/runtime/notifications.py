"""Cross-channel task-completion notifications.

``Notifier`` is the abstraction every channel implements (CLI, WeChat,
Feishu, DingTalk). The default :class:`InboxNotifier` appends to a local
JSONL inbox and logs — so completions are never lost even with no daemon,
and ``omni task`` / the REPL can surface them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class TaskNotification:
    subtask_id: str
    skill_name: str
    status: str
    object_kind: str = "skill_execution"
    object_id: str = ""
    channel: str = "cli"
    session_id: str = ""
    external_key: str = ""
    title: str = ""
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    payload: dict[str, Any] = field(default_factory=dict)
    # Owning user-request Task. Kept at the end so positional construction of
    # older notification fields remains source-compatible.
    task_id: str = ""

    def __post_init__(self) -> None:
        if not self.object_id:
            self.object_id = self.subtask_id
        if not self.task_id and self.object_kind == "task" and self.object_id:
            self.task_id = self.object_id
        elif not self.task_id and self.object_kind == "scheduled_goal" and self.subtask_id:
            # Older scheduled-goal producers placed the run Task in
            # ``subtask_id``. ``object_id`` may instead be the schedule id.
            self.task_id = self.subtask_id

    @property
    def reference_id(self) -> str:
        return self.object_id or self.subtask_id

    @property
    def display_name(self) -> str:
        return self.title or self.skill_name or self.object_kind.replace("_", " ")


def delivery_key(
    *,
    channel: str,
    external_key: str,
    kind: str,
    task_id: str = "",
    subtask_id: str = "",
    object_kind: str = "",
    object_id: str = "",
    state: str = "",
) -> str:
    """Return a stable, non-PII key for one logical outbound presentation."""
    payload = json.dumps(
        [
            channel,
            external_key,
            kind,
            task_id,
            object_kind,
            object_id or subtask_id,
            state,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8", errors="backslashreplace")).hexdigest()


def task_notification_from_dict(data: dict[str, Any]) -> TaskNotification:
    """Hydrate a notification from JSONL while tolerating future fields."""
    return TaskNotification(
        subtask_id=str(data.get("subtask_id") or ""),
        skill_name=str(data.get("skill_name") or ""),
        status=str(data.get("status") or ""),
        object_kind=str(data.get("object_kind") or "skill_execution"),
        object_id=str(data.get("object_id") or data.get("subtask_id") or ""),
        channel=str(data.get("channel") or "cli"),
        session_id=str(data.get("session_id") or ""),
        external_key=str(data.get("external_key") or ""),
        title=str(data.get("title") or ""),
        summary=str(data.get("summary") or ""),
        artifacts=[str(x) for x in data.get("artifacts") or []],
        created_at=str(data.get("created_at") or datetime.now(UTC).isoformat()),
        payload=data.get("payload") if isinstance(data.get("payload"), dict) else {},
        task_id=str(data.get("task_id") or ""),
    )


class Notifier(Protocol):
    async def notify(self, note: TaskNotification) -> None: ...


class CompositeNotifier:
    """Fan a notification out to several notifiers (inbox + channels)."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self._notifiers = notifiers

    async def notify(self, note: TaskNotification) -> None:
        for n in self._notifiers:
            try:
                await n.notify(note)
            except Exception:  # noqa: BLE001
                logger.exception("notifier %s failed", type(n).__name__)


class InboxNotifier:
    """Default notifier: append to ``<project>/inbox.jsonl`` + log."""

    def __init__(self, inbox_path: Path, *, live_hook: Any = None) -> None:
        self._path = inbox_path
        self._live_hook = live_hook  # optional callable(note) for REPL/live display

    async def notify(self, note: TaskNotification) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8", errors="backslashreplace") as fh:
            fh.write(json.dumps(asdict(note), ensure_ascii=False) + "\n")
        logger.info(
            "[notify] kind=%s object=%s provider=%s status=%s",
            note.object_kind,
            note.reference_id,
            note.skill_name,
            note.status,
        )
        if self._live_hook:
            try:
                self._live_hook(note)
            except Exception:  # noqa: BLE001
                pass

    def read_all_dicts(self) -> list[dict[str, Any]]:
        return self.read_all()

    def read_all(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out


def collect_inbox_notes(
    paths: Any,
    *,
    include_channel_anchor: bool = True,
) -> list[dict[str, Any]]:
    """Read the local inbox, optionally merging the IM channel-anchor inbox.

    WeChat / home-service completions land on ``projects/<channel_anchor>/inbox.jsonl``
    while the interactive CLI often runs in a path-keyed workspace. Merging those
    two sources (tagged with ``workspace`` + ``_project_dir``) keeps ``/inbox``
    aligned with ``/task all`` without rewriting IM work into the repo store.
    """
    from omni.config.workspaces import channel_anchor_project_dir

    project_dir = Path(paths.project_dir)
    sources: list[tuple[str, Path]] = [
        (str(getattr(paths, "project_name", None) or project_dir.name or "local"), project_dir)
    ]
    if include_channel_anchor:
        home = getattr(paths, "home", None)
        anchor_dir = channel_anchor_project_dir(home)
        try:
            if anchor_dir.resolve() != project_dir.resolve():
                sources.append((anchor_dir.name, anchor_dir))
        except OSError:
            sources.append((anchor_dir.name, anchor_dir))

    notes: list[dict[str, Any]] = []
    for label, src_dir in sources:
        for note in InboxNotifier(src_dir / "inbox.jsonl").read_all():
            if not isinstance(note, dict):
                continue
            tagged = dict(note)
            tagged["workspace"] = label
            tagged["_project_dir"] = str(src_dir)
            notes.append(tagged)
    notes.sort(key=lambda n: str(n.get("created_at") or ""))
    return notes


def record_delivery_status(
    project_dir: Path,
    note: TaskNotification,
    *,
    status: str,
    message: str = "",
    report: dict[str, Any] | None = None,
) -> None:
    """Persist channel delivery outcome and queue hard failures for retry.

    Task results are already durable in the DB/inbox; this sidecar records
    whether the *outbound channel* actually delivered the notification, degraded
    to text, or failed. Failed records are duplicated into a retry queue so a
    future resend command/daemon can replay them without scraping logs.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "created_at": datetime.now(UTC).isoformat(),
        "task_id": note.task_id,
        "subtask_id": note.subtask_id,
        "object_kind": note.object_kind,
        "object_id": note.reference_id,
        "skill_name": note.skill_name,
        "task_status": note.status,
        "channel": note.channel,
        "session_id": note.session_id,
        "external_key": note.external_key,
        "delivery_status": status,
        "message": message,
        "report": report or {},
    }
    _append_jsonl(project_dir / "delivery_status.jsonl", entry)
    if status == "failed":
        _append_jsonl(
            project_dir / "delivery_retry.jsonl",
            {
                **entry,
                "notification": asdict(note),
                "retry_status": "pending",
            },
        )


# A failed send is usually the far side refusing a burst for a few minutes, so
# replays are spaced to outlast the window instead of spending every attempt
# inside it: the first after a minute, then five, then fifteen. Past
# ``_RETRY_MAX_AGE_SECONDS`` the queue gives up — a reader who asked half an hour
# ago has moved on, and arriving late is its own kind of wrong answer.
_RETRY_BACKOFF_SECONDS = (60.0, 300.0, 900.0)
_RETRY_MAX_AGE_SECONDS = 1800.0


def _retry_key(entry: Mapping[str, Any]) -> str:
    """Return the delivery identity a queued row and its replays share."""
    return delivery_key(
        channel=str(entry.get("channel") or ""),
        external_key=str(entry.get("external_key") or ""),
        kind="task_notification",
        task_id=str(entry.get("task_id") or ""),
        object_kind=str(entry.get("object_kind") or ""),
        object_id=str(entry.get("object_id") or entry.get("subtask_id") or ""),
        state=str(entry.get("task_status") or ""),
    )


def _entry_moment(entry: Mapping[str, Any]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(entry.get("created_at") or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def pending_delivery_retries(
    project_dir: Path,
    *,
    channel: str = "",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return queued deliveries whose next replay is due, oldest first.

    The result is already durable, so a hard delivery failure means only that
    the reader has not been handed it yet. Attempts are counted across replays
    and never reset, so a delivery that keeps failing is abandoned to
    ``/task show`` rather than retried forever.
    """
    moment = now or datetime.now(UTC)
    queued: dict[str, dict[str, Any]] = {}
    attempts: dict[str, int] = {}
    resolved: set[str] = set()
    last_seen: dict[str, datetime] = {}
    for row in _read_jsonl(project_dir / "delivery_retry.jsonl"):
        key = _retry_key(row)
        status = str(row.get("retry_status") or "")
        stamp = _entry_moment(row)
        if stamp is not None:
            last_seen[key] = stamp
        if status == "pending":
            queued.setdefault(key, row)
        elif status == "attempted":
            attempts[key] = attempts.get(key, 0) + 1
        else:
            resolved.add(key)
    due: list[dict[str, Any]] = []
    for key, row in queued.items():
        tries = attempts.get(key, 0)
        if key in resolved or tries >= len(_RETRY_BACKOFF_SECONDS):
            continue
        if channel and str(row.get("channel") or "") != channel:
            continue
        queued_at = _entry_moment(row)
        if queued_at is None or (moment - queued_at).total_seconds() > _RETRY_MAX_AGE_SECONDS:
            continue
        waited = (moment - last_seen.get(key, queued_at)).total_seconds()
        if waited < _RETRY_BACKOFF_SECONDS[tries]:
            continue
        due.append(row)
    due.sort(key=lambda row: str(row.get("created_at") or ""))
    return due


def record_delivery_retry(project_dir: Path, entry: Mapping[str, Any], *, status: str) -> None:
    """Log one replay of a queued delivery so the queue drains instead of looping."""
    if status not in {"attempted", "sent", "abandoned"}:
        raise ValueError(f"unsupported retry status: {status}")
    payload = {
        key: value for key, value in entry.items() if key not in {"notification", "report"}
    }
    payload["created_at"] = datetime.now(UTC).isoformat()
    payload["retry_status"] = status
    _append_jsonl(project_dir / "delivery_retry.jsonl", payload)


def read_delivery_statuses(project_dir: Path, subtask_id: str | None = None) -> list[dict[str, Any]]:
    rows = _read_jsonl(project_dir / "delivery_status.jsonl")
    if subtask_id:
        rows = [
            row
            for row in rows
            if str(row.get("object_id") or row.get("subtask_id") or "").startswith(subtask_id)
        ]
    return rows


def latest_delivery_status(project_dir: Path, subtask_id: str) -> dict[str, Any] | None:
    rows = read_delivery_statuses(project_dir, subtask_id)
    return rows[-1] if rows else None


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="backslashreplace") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out
