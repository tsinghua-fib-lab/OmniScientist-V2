"""SQLAlchemy ORM models (single SQLite store per project).

Distilled from HelixForge ``common/models/orm.py`` — tenant/org/policy/eval
tables dropped. The one multi-identity concept kept is ``principal`` on
``memory_entries``: the machine owner (CLI / local) is ``"local"``; an IM peer
is ``"<channel>:<external_key>"`` (e.g. ``feishu:oc_abc``). This isolates
auto-learned memory per conversational identity so one ``omni serve`` daemon can
safely serve many IM users without cross-contaminating their recall.
JSON columns map to TEXT on SQLite and are perfectly adequate for a
single-machine workload.

Execution vocabulary (schema generation 3, ``user_version`` 1):

* **Task** (``tasks``) — one user request, from any channel. What users see
  in ``/task`` and act on (approve / cancel / steer / rm / archive).
* **Workflow run / step** (``workflow_runs`` / ``workflow_steps``) — one
  durable DAG execution and its stable logical nodes.
* **Subtask** (``subtasks``) — one durable skill-execution attempt. A workflow
  step may own multiple attempts, but a workflow is never a subtask.
* **Task events / controls** (``task_events`` / ``task_controls``) — the
  append-only activity stream and steer/cancel messages of a task.

The lifecycle triangle (subtasks / task_events / task_controls → tasks) is
enforced with real foreign keys and ``ON DELETE CASCADE`` so deleting a task
removes its execution records in one statement. ``artifacts.subtask_id`` uses
``ON DELETE SET NULL``: produced files are user deliverables and must survive
task deletion. Peripheral tables (focus stack, delivery ledger, compute jobs,
research objects) keep weak string references by design.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    literal_column,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SessionORM(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    project: Mapped[str] = mapped_column(String(128), default="default", index=True)
    channel: Mapped[str] = mapped_column(String(32), default="cli", index=True)
    external_key: Mapped[str] = mapped_column(String(256), default="")  # e.g. wechat openid
    title: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(24), default="active")
    # Session forking (P2): id of the session this one was branched from ("" =
    # a root session). The transcript is copied at fork time; the two sessions
    # then evolve independently.
    forked_from: Mapped[str] = mapped_column(String(40), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ConversationMessageORM(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user|assistant|tool|system
    content: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[str] = mapped_column(String(24), default="text")
    name: Mapped[str] = mapped_column(String(128), default="")  # tool name
    tool_call_id: Mapped[str] = mapped_column(String(128), default="")
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


# SQLite can stamp two inserts with the same ``created_at`` (common on Windows).
# ``rowid`` is insertion order and is the stable tie-break for a transcript.
_MESSAGE_ROWID = literal_column("rowid")
MESSAGE_ORDER_ASC = (ConversationMessageORM.created_at.asc(), _MESSAGE_ROWID.asc())
MESSAGE_ORDER_DESC = (ConversationMessageORM.created_at.desc(), _MESSAGE_ROWID.desc())


class TaskORM(Base):
    """One user request, from any channel — the record ``/task`` shows.

    A task starts when a user turn enters Omni. Tool calls, subtask
    submissions, subtask progress, artifacts, and the final assistant message
    are appended as task events so ``/task show`` can replay the whole chain,
    not just the async subtask created near the end of a turn.
    """

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    parent_task_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, default=None, index=True
    )
    origin_workflow_run_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    origin_workflow_step_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    # Origin schedule when this task is the owning task of a headless scheduled
    # run (``ScheduleORM.id``); "" for interactive/channel turns. Lets
    # ``/schedule show`` list a schedule's run history in headless-turn mode,
    # where the work is a full planner→workflow turn (not one direct subtask).
    schedule_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    # Retry lineage. ``task retry`` creates a NEW task that carries
    # ``retry_of_task_id`` (its immediate parent attempt), ``root_task_id`` (the
    # first attempt of the chain), and a monotonically increasing ``attempt``.
    # ``input_snapshot_json`` is the immutable turn input (user text + file uris +
    # interaction mode + origin) so a later attempt reproduces the original
    # request without mutating the task it retried.
    retry_of_task_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    root_task_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    input_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    kind: Mapped[str] = mapped_column(String(24), default="turn", index=True)
    # turn | subagent | maintenance
    depth: Mapped[int] = mapped_column(Integer, default=0)
    project: Mapped[str] = mapped_column(String(128), default="default", index=True)
    channel: Mapped[str] = mapped_column(String(32), default="cli", index=True)
    external_key: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    # running | awaiting_approval | needs_input | recovering | succeeded |
    # degraded | failed | cancelled | interrupted
    title: Mapped[str] = mapped_column(String(512), default="")
    user_input: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    current_stage: Mapped[str] = mapped_column(String(128), default="")
    # closed | open | sealed. This execution-epoch gate is independent of
    # current_stage, which later audit/cost events legitimately overwrite.
    steering_status: Mapped[str] = mapped_column(String(16), default="closed")
    current_tool: Mapped[str] = mapped_column(String(128), default="")
    current_workflow_id: Mapped[str] = mapped_column(String(40), default="")
    current_subtask_id: Mapped[str] = mapped_column(String(40), default="")
    plan_json: Mapped[dict] = mapped_column(JSON, default=dict)
    plan_status: Mapped[str] = mapped_column(String(32), default="")
    intent_type: Mapped[str] = mapped_column(String(64), default="")
    provenance_mode: Mapped[str] = mapped_column(String(32), default="")
    tool_policy_json: Mapped[dict] = mapped_column(JSON, default=dict)
    approved_tools: Mapped[list] = mapped_column(JSON, default=list)
    # Content-addressed execution authority for the latest accepted plan and
    # the exact snapshot presented for approval.  A claim succeeds only when
    # both still match, binding plan + catalog + contracts + prospective grants.
    current_authority_fingerprint: Mapped[str] = mapped_column(
        String(64), default=""
    )
    approval_authority_fingerprint: Mapped[str] = mapped_column(
        String(64), default=""
    )
    submitted_workflow_ids: Mapped[list] = mapped_column(JSON, default=list)
    submitted_subtask_ids: Mapped[list] = mapped_column(JSON, default=list)
    artifact_ids: Mapped[list] = mapped_column(JSON, default=list)
    source_ids: Mapped[list] = mapped_column(JSON, default=list)
    claim_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_reason: Mapped[str] = mapped_column(Text, default="")


class WorkflowRunORM(Base):
    """One durable execution attempt of a validated workflow DAG."""

    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    project: Mapped[str] = mapped_column(String(128), default="default", index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    # pending | running | recovering | succeeded | degraded | failed |
    # cancelled | interrupted
    goal: Mapped[str] = mapped_column(Text, default="")
    plan_json: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_authority_json: Mapped[dict] = mapped_column(JSON, default=dict)
    task_contract_json: Mapped[dict] = mapped_column(JSON, default=dict)
    workflow_dag_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    current_step_id: Mapped[str] = mapped_column(String(128), default="")
    notify_channel: Mapped[str] = mapped_column(String(32), default="cli")
    trace_log: Mapped[list] = mapped_column(JSON, default=list)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    retry_of: Mapped[str] = mapped_column(String(40), default="", index=True)
    resume_of: Mapped[str] = mapped_column(String(40), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowStepORM(Base):
    """A stable logical node in a workflow run.

    ``step_key`` is the planner-facing id and remains stable across retries.
    Skill executions are separate :class:`SubtaskORM` attempts referenced by
    ``execution_ids`` and ``current_execution_id``.
    """

    __tablename__ = "workflow_steps"
    __table_args__ = (UniqueConstraint("workflow_run_id", "step_key"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    workflow_run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    step_key: Mapped[str] = mapped_column(String(128), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    skill_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    capability: Mapped[str] = mapped_column(String(128), default="")
    provider_type: Mapped[str] = mapped_column(String(32), default="skill")
    deliverable: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    # pending | running | succeeded | degraded | failed | skipped | cancelled
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    optional_depends_on: Mapped[list] = mapped_column(JSON, default=list)
    allow_failed_dependencies: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_policy: Mapped[str] = mapped_column(String(32), default="")
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    provider_authority_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    warning: Mapped[str] = mapped_column(Text, default="")
    recoverable: Mapped[bool] = mapped_column(Boolean, default=False)
    current_execution_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    execution_ids: Mapped[list] = mapped_column(JSON, default=list)
    child_task_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    child_task_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SubtaskORM(Base):
    """One durable skill execution submitted by a task.

    ``task_id`` is NULL only for standalone submissions that have no owning
    user request (e.g. schedule-fired jobs); deleting a task cascades to its
    subtasks at the database level.
    """

    __tablename__ = "subtasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    task_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, default=None, index=True
    )
    workflow_run_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=True, default=None, index=True
    )
    workflow_step_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("workflow_steps.id", ondelete="CASCADE"), nullable=True, default=None, index=True
    )
    parent_event_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    # Origin schedule when this execution was materialised by the scheduler
    # (``ScheduleORM.id``); "" for interactive/workflow submissions. Lets
    # ``/schedule show`` list a schedule's full run history and trace a run back
    # to its schedule.
    schedule_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    project: Mapped[str] = mapped_column(String(128), default="default")
    skill_name: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    # scheduled | pending | running | succeeded | degraded | failed |
    # recovering | skipped | cancelled | interrupted
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    provider_authority_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    notify_channel: Mapped[str] = mapped_column(String(32), default="cli")
    notify_config: Mapped[dict] = mapped_column(JSON, default=dict)
    trace_log: Mapped[list] = mapped_column(JSON, default=list)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    step_attempt: Mapped[int] = mapped_column(Integer, default=1)
    retry_of: Mapped[str] = mapped_column(String(40), default="", index=True)
    resume_of: Mapped[str] = mapped_column(String(40), default="", index=True)
    original_error: Mapped[str] = mapped_column(Text, default="")
    recovery_attempt: Mapped[int] = mapped_column(Integer, default=0)
    recovery_policy: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_reason: Mapped[str] = mapped_column(Text, default="")
    # Local-first execution owner. A replacement process can immediately
    # settle a claim whose PID is dead; live owners are never stolen.
    # 0 means "legacy / unclaimed" and follows the time lease.
    owner_pid: Mapped[int] = mapped_column(Integer, default=0)


class WorkflowCheckpointORM(Base):
    """Durable progress snapshot for a workflow run."""

    __tablename__ = "workflow_checkpoints"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    workflow_run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0, index=True)
    status: Mapped[str] = mapped_column(String(32), default="", index=True)
    current_step_id: Mapped[str] = mapped_column(String(128), default="")
    last_completed_step_id: Mapped[str] = mapped_column(String(128), default="")
    completed_step_ids: Mapped[list] = mapped_column(JSON, default=list)
    failed_step_ids: Mapped[list] = mapped_column(JSON, default=list)
    pending_steps: Mapped[list] = mapped_column(JSON, default=list)
    emitted_artifacts: Mapped[list] = mapped_column(JSON, default=list)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class TaskEventORM(Base):
    """Append-only event stream for a :class:`TaskORM`."""

    __tablename__ = "task_events"
    # Deployed databases carry this index under an older name, but nothing in the
    # code declared it any more, so a freshly created store had no constraint at
    # all: concurrent appends silently wrote two events under one sequence number
    # and the stream stopped being orderable. Declaring it keeps old and new
    # stores telling the same story; `TaskRecorder.append_event` retries on the
    # collision rather than surfacing it.
    __table_args__ = (UniqueConstraint("task_id", "seq", name="ux_task_events_task_seq"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0, index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="")
    # Canonical invocation semantics. ``status`` remains the legacy projection
    # consumed by existing CLI/API clients during the compatibility window.
    lifecycle_status: Mapped[str] = mapped_column(String(32), default="")
    result_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    tool_name: Mapped[str] = mapped_column(String(128), default="")
    skill_name: Mapped[str] = mapped_column(String(128), default="")
    workflow_run_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    workflow_step_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    subtask_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    step_id: Mapped[str] = mapped_column(String(128), default="")
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class TaskControlORM(Base):
    """A durable control message consumed at an execution boundary.

    ``steer`` appends an operator instruction to the active loop; ``cancel``
    asks the loop/workflow to stop after preserving its partial state. Controls
    are append-only audit records whose status moves pending -> consumed
    (claimed by the control poller) -> applied (drained at a safe boundary), or
    pending/consumed -> requeued when a finishing turn hands an unapplied steer
    to the next user turn.
    """

    __tablename__ = "task_controls"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(24), index=True)  # steer | cancel
    instruction: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Local-first execution owner. A replacement process can immediately
    # recover a claim whose PID is dead; live owners retain the time lease.
    consumer_pid: Mapped[int] = mapped_column(Integer, default=0)


class OutboundDeliveryORM(Base):
    """Durable application-level idempotency record for channel sends."""

    __tablename__ = "outbound_deliveries"

    delivery_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    object_kind: Mapped[str] = mapped_column(String(32), default="skill_execution", index=True)
    object_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    subtask_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    channel: Mapped[str] = mapped_column(String(32), default="", index=True)
    external_key: Mapped[str] = mapped_column(String(256), default="")
    kind: Mapped[str] = mapped_column(String(32), default="", index=True)
    status: Mapped[str] = mapped_column(String(24), default="sending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str] = mapped_column(Text, default="")
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, index=True
    )


class ComputeJobORM(Base):
    """Managed lifecycle record for local or remote scientific compute."""

    __tablename__ = "compute_jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    requested_backend: Mapped[str] = mapped_column(String(32), default="local")
    backend: Mapped[str] = mapped_column(String(32), default="", index=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    # queued | running | submitted | succeeded | failed | timeout |
    # cancel_requested | cancelled
    command: Mapped[str] = mapped_column(Text, default="")
    cwd: Mapped[str] = mapped_column(String(1024), default="")
    profile: Mapped[str] = mapped_column(String(128), default="")
    external_job_id: Mapped[str] = mapped_column(String(256), default="")
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, index=True
    )


class MemoryEntryORM(Base):
    __tablename__ = "memory_entries"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    # Conversational identity that owns this memory: "local" (CLI/machine owner)
    # or "<channel>:<external_key>" for an IM peer. Recall filters by principal so
    # one daemon serving many IM users never leaks A's memory into B's context.
    principal: Mapped[str] = mapped_column(String(96), default="local", index=True)
    layer: Mapped[str] = mapped_column(String(8), index=True)  # M1..M5
    scope: Mapped[str] = mapped_column(String(16), index=True)  # session|task|user|project
    scope_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    memory_type: Mapped[str] = mapped_column(String(32), default="note")
    summary: Mapped[str] = mapped_column(Text, default="")
    payload_ref: Mapped[str] = mapped_column(String(256), default="")  # artifact/file ref
    embedding_id: Mapped[str] = mapped_column(String(64), default="")
    embedding: Mapped[list] = mapped_column(JSON, default=list)  # inline vector (naive backend)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    pinned: Mapped[int] = mapped_column(Integer, default=0)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recall_count: Mapped[int] = mapped_column(Integer, default=0)
    consolidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MemoryEdgeORM(Base):
    """A directed, weighted edge between two memories (the memory graph, P3).

    Where :class:`MemoryEntryORM` rows are the nodes, these are the links that
    let recall spread across *sessions*: a fresh memory is auto-linked to the
    semantically nearest existing memories (``relation="related"``) and to
    memories sharing a tag (``relation="same_topic"``), so retrieving one hit
    can surface its neighbours even when they never co-occurred in a session.

    ``principal`` mirrors the endpoints' owner so graph traversal is isolated
    per identity exactly like recall — one daemon serving many IM peers never
    walks an edge from A's memory into B's. Endpoints are referenced by id
    without SQL foreign keys, matching the rest of this local-first store.
    """

    __tablename__ = "memory_edges"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    src_id: Mapped[str] = mapped_column(String(40), index=True)
    dst_id: Mapped[str] = mapped_column(String(40), index=True)
    principal: Mapped[str] = mapped_column(String(96), default="local", index=True)
    relation: Mapped[str] = mapped_column(String(24), default="related", index=True)
    # related | same_topic | derived_from | contradicts
    weight: Mapped[float] = mapped_column(Float, default=0.5)
    origin: Mapped[str] = mapped_column(String(16), default="auto")  # auto|manual
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ArtifactORM(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    # Producing user-request task. References from later turns do not change
    # this owner. SET NULL preserves user deliverables when task history is
    # removed.
    task_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, default=None, index=True
    )
    # Producing subtask. SET NULL on delete: artifacts are user deliverables
    # and must survive task/subtask deletion.
    subtask_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("subtasks.id", ondelete="SET NULL"), nullable=True, default=None, index=True
    )
    workflow_run_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True, default=None, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="file")  # figure|paper|report|data|file
    title: Mapped[str] = mapped_column(String(512), default="")
    uri: Mapped[str] = mapped_column(String(1024), default="")  # artifact://<id> or file path
    rel_path: Mapped[str] = mapped_column(String(1024), default="")
    mime: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SessionFocusORM(Base):
    """Append-only active target stack for a conversation session.

    This is short-horizon working context, not long-term memory. It lets turns
    that refer to "this figure" bind to the most recently attached or delivered
    artifact before the model planner decides whether the request is underspecified.
    """

    __tablename__ = "session_focus"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    target_kind: Mapped[str] = mapped_column(String(32), default="skill_execution", index=True)
    # task | workflow_run | workflow_step | skill_execution | child_task
    workflow_run_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    workflow_step_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    child_task_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    subtask_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    task_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    skill_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    origin: Mapped[str] = mapped_column(String(32), default="", index=True)
    # task_completed | task_attached | artifact_revision | artifact_sent
    artifact_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    artifact_uri: Mapped[str] = mapped_column(String(1024), default="")
    artifact_path: Mapped[str] = mapped_column(String(1024), default="")
    artifact_kind: Mapped[str] = mapped_column(String(32), default="")
    artifact_title: Mapped[str] = mapped_column(String(512), default="")
    source_uri: Mapped[str] = mapped_column(String(1024), default="")
    source_path: Mapped[str] = mapped_column(String(1024), default="")
    source_kind: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


# ── Research Object Model (ROM) ────────────────────────────────────────────
# Structured carriers for the research state: a source/chunk corpus, the
# hypothesis → claim → evidence graph, and an experiment run ledger. They
# reference sessions/subtasks/artifacts by id without SQL foreign keys, matching
# ``ConversationMessageORM.session_id`` and avoiding cross-table delete-cascade
# surprises in a local-first SQLite store.


class SourceORM(Base):
    """A citable source (paper / web page / dataset / run output)."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(24), default="paper", index=True)
    # paper | web | dataset | run | book | other
    arxiv_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    doi: Mapped[str] = mapped_column(String(128), default="", index=True)
    url: Mapped[str] = mapped_column(String(1024), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    authors: Mapped[list] = mapped_column(JSON, default=list)
    year: Mapped[str] = mapped_column(String(8), default="")
    venue: Mapped[str] = mapped_column(String(256), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(32), default="manual")  # arxiv|openalex|crossref|web|manual
    dedup_key: Mapped[str] = mapped_column(String(256), default="", index=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    # ``retrieved_at`` is when omni fetched it; ``date_pin`` is the project
    # "as-of" date used for reproducible, date-restricted retrieval (M3).
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    date_pin: Mapped[str] = mapped_column(String(32), default="")
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ChunkORM(Base):
    """A retrievable passage of a source (grounded RAG unit)."""

    __tablename__ = "source_chunks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(String(40), index=True)
    ord: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str] = mapped_column(String(256), default="")
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CitationORM(Base):
    """A directed bibliographic edge: ``citing`` source → ``cited`` work.

    The cited endpoint is stored by its stable dedup key (``doi:…`` / ``arxiv:…``
    / ``title:…``) so an edge survives even before the cited work is itself
    ingested as a :class:`SourceORM`; ``cited_source_id`` is filled in once (if)
    that work joins the corpus. This lets the citation graph be traversed in both
    directions (references / cited-by) without SQL foreign keys.
    """

    __tablename__ = "citations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    citing_source_id: Mapped[str] = mapped_column(String(40), index=True)
    citing_key: Mapped[str] = mapped_column(String(256), default="", index=True)
    cited_source_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    cited_key: Mapped[str] = mapped_column(String(256), default="", index=True)
    cited_title: Mapped[str] = mapped_column(Text, default="")
    cited_doi: Mapped[str] = mapped_column(String(128), default="")
    cited_year: Mapped[str] = mapped_column(String(8), default="")
    origin: Mapped[str] = mapped_column(String(32), default="manual")  # openalex|crossref|manual
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class HypothesisORM(Base):
    __tablename__ = "hypotheses"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    statement: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="proposed", index=True)
    # proposed | testing | supported | refuted | inconclusive
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ClaimORM(Base):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    hypothesis_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    polarity: Mapped[str] = mapped_column(String(16), default="assert")  # assert|negate|open
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    made_by: Mapped[str] = mapped_column(String(16), default="agent")  # agent|user
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EvidenceORM(Base):
    """An edge binding a claim to a source passage (provenance)."""

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    claim_id: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    chunk_id: Mapped[str] = mapped_column(String(40), default="")
    stance: Mapped[str] = mapped_column(String(16), default="supports")  # supports|contradicts|mentions
    quote: Mapped[str] = mapped_column(Text, default="")
    locator: Mapped[str] = mapped_column(String(256), default="")  # page/section
    strength: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RunORM(Base):
    """An experiment / computation run (the ledger every number cites)."""

    __tablename__ = "experiment_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    hypothesis_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    subtask_id: Mapped[str] = mapped_column(String(40), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    cmd: Mapped[str] = mapped_column(Text, default="")
    code_uri: Mapped[str] = mapped_column(String(1024), default="")
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    env_lock: Mapped[str] = mapped_column(Text, default="")
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    output_uris: Mapped[list] = mapped_column(JSON, default=list)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="recorded")  # recorded|succeeded|failed
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ScheduleORM(Base):
    """A recurring / one-shot scheduled job (P2, cron/scheduled jobs).

    When ``next_due_at`` passes, the scheduler materialises this schedule into a
    normal :class:`SubtaskORM` (via the subtask runtime) — so a schedule is just
    a *recurring source of subtasks*, reusing all of the runtime's durability,
    retry and notification machinery. ``kind`` is ``interval`` (every
    ``interval_s`` seconds), ``cron`` (a 5-field cron expression), or ``once``
    (fire then disable).
    """

    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    project: Mapped[str] = mapped_column(String(128), default="default", index=True)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    channel: Mapped[str] = mapped_column(String(32), default="cli")
    title: Mapped[str] = mapped_column(String(512), default="")
    skill_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    kind: Mapped[str] = mapped_column(String(16), default="interval")  # interval|cron|once
    interval_s: Mapped[int] = mapped_column(Integer, default=0)
    cron_expr: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    next_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_subtask_id: Mapped[str] = mapped_column(String(40), default="")
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    # Sensitive tool names this schedule may run unattended (no interactive
    # approver in ``omni serve``). Seeded at creation from ``schedules.autonomy``;
    # each fire grants exactly these to the run's owning task so the approval
    # gate can clear them via its preauthorizer. Empty ⇒ fail-closed (today's
    # behaviour): sensitive tools stay blocked in the daemon.
    approved_tools: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ScheduleActionProposalORM(Base):
    """A durable, immutable proposal to create a schedule, awaiting local approval.

    Codex resolves a sensitive action with an in-memory request/response keyed by
    ``call_id`` (``pending_approvals: HashMap<String, oneshot::Sender>``): the
    action is *held* server-side and resumed by id when the user decides. That
    channel only survives the blocking turn in one process. An IM-originated
    schedule request is harder — the approver is the machine owner on a *different*
    process (their local ``omni`` CLI), the IM turn is request/response and cannot
    block, and the daemon may restart before approval. So we persist the held
    action here instead of an in-memory channel: ``payload_json`` is the exact
    :class:`~omni.scheduling.contracts.ScheduleCreateRequest` snapshot, and
    ``payload_digest`` (sha256 of the canonical payload) lets approval verify the
    stored action was not tampered with. Approval executes the *stored* payload —
    never a command re-composed from prose — exactly like Codex resuming the held
    action by id. ``idempotency_key`` + ``result_schedule_id`` make a replayed or
    concurrent approval converge on a single created schedule.
    """

    __tablename__ = "schedule_action_proposals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    project: Mapped[str] = mapped_column(String(128), default="default", index=True)
    # Conversational origin of the request (who asked): the IM peer's channel and
    # session, plus the memory ``principal`` ("<channel>:<external_key>"). Approval
    # authority is the local owner (the CLI approve command runs locally); this is
    # recorded for delivery + audit, not to widen who may approve.
    channel: Mapped[str] = mapped_column(String(32), default="cli", index=True)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    actor_principal: Mapped[str] = mapped_column(String(96), default="local", index=True)
    # Absolute ``project_dir`` of the workspace that originated the request — the
    # workspace whose runtime/channel will fire the schedule and deliver its
    # result (an IM turn's anchor). Proposals live in the machine-global control
    # store, but approval must materialise the schedule *back* into this
    # workspace so a WeChat/Feishu result returns to the right inbox. Empty for
    # legacy rows migrated from a per-workspace store (approval falls back to the
    # approving CLI's own workspace).
    origin_project_dir: Mapped[str] = mapped_column(String(512), default="")
    kind: Mapped[str] = mapped_column(String(24), default="schedule_create")
    title: Mapped[str] = mapped_column(String(512), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_digest: Mapped[str] = mapped_column(String(64), default="", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending | approved | denied | expired
    result_schedule_id: Mapped[str] = mapped_column(String(40), default="")
    decided_by: Mapped[str] = mapped_column(String(96), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ActionCheckpointORM(Base):
    """A durable, resumable checkpoint in an Action's semantic-admission pipeline.

    Generalises the schedule-proposal pattern into the two phases the admission
    design keeps **separate** (never one row mutated in place):

    * ``semantic_clarification`` — "what did the user mean?" A critical field
      (e.g. an ambiguous schedule time) could not be uniquely resolved, so the
      grounded candidates are persisted and the *original requester* (same
      ``channel``/``session``/``principal``) picks one. Resolving it yields a
      complete canonical Action.
    * ``authorization`` — "may this run?" Decided by the machine owner. Modelled
      today by :class:`ScheduleActionProposalORM`; this table is the forward
      home a later adapter can converge onto without a risky migration.

    Concurrency is guarded by an optimistic ``version`` (compare-and-set on every
    state transition) plus an ``idempotency_key``/``result_id`` so a replayed or
    concurrent selection converges on a single result. ``payload_fingerprint`` is
    a SHA-256 for *drift detection only* — authority comes from the DB boundary,
    ``required_decider``, and the CAS transitions, never from the hash.
    """

    __tablename__ = "action_checkpoints"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_uuid)
    phase: Mapped[str] = mapped_column(String(24), default="semantic_clarification", index=True)
    action_kind: Mapped[str] = mapped_column(String(48), default="", index=True)
    contract_version: Mapped[str] = mapped_column(String(16), default="")
    policy_version: Mapped[str] = mapped_column(String(32), default="")
    # Conversational origin + the identity that may resolve this checkpoint.
    project: Mapped[str] = mapped_column(String(128), default="default", index=True)
    origin_project_dir: Mapped[str] = mapped_column(String(512), default="")
    channel: Mapped[str] = mapped_column(String(32), default="cli", index=True)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    # Owning Task (the needs_input turn that persisted this draft). Lets
    # ``task resume <task-id>`` find the checkpoint directly; pre-migration rows
    # that predate this column backfill lazily from ``action.checkpoint.created``
    # events during resume.
    task_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    actor_principal: Mapped[str] = mapped_column(String(96), default="local", index=True)
    required_decider: Mapped[str] = mapped_column(String(96), default="", index=True)
    # The sealed proposal-so-far, its drift fingerprint, and the resolver verdict
    # (candidates / unresolved fields / reason) shown to the decider.
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), default="", index=True)
    resolution_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # State machine (phase-specific vocabulary) + optimistic-concurrency counter.
    state: Mapped[str] = mapped_column(String(20), default="open", index=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    parent_checkpoint_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    # The recorded decision + where it materialised (e.g. a created schedule).
    decision_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_kind: Mapped[str] = mapped_column(String(24), default="")
    result_id: Mapped[str] = mapped_column(String(40), default="")
    # A crashed claimant's hold lapses so recovery can retry.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class TaskIndexORM(Base):
    """Machine-global, cross-workspace index of tasks (lives in ``control.sqlite3``).

    The heavy per-task data (events, subtasks, workflow steps, artifacts, ROM)
    stays sharded in each workspace's ``sessions.sqlite3``. This one global table
    is the *control-plane index*: the small row that lets any CLI — launched from
    any directory — list tasks across every workspace and, crucially, **route a
    task id back to the workspace that owns it**, so ``omni task show <id>`` finds
    a task that ``--all`` listed even when the CLI resolved to a different
    workspace (the "global list, local lookup" bug otherwise).

    This mirrors how Codex/opencode split storage: one global index keyed by the
    workspace (Codex's ``threads.cwd``) plus per-conversation heavy stores.
    ``project_dir`` is the stable workspace key *and* the pointer used to open the
    owning ``sessions.sqlite3``; ``workspace_root`` / ``workspace_kind`` capture the
    registry recipe to rebuild that workspace's settings (named ``-P`` vs in-place
    ``.omni`` vs path-keyed). Every other column is a denormalised copy of the
    owning :class:`TaskORM` for cheap list/route rendering *without* opening the
    workspace — best-effort freshness, never the source of truth for a task.
    """

    __tablename__ = "task_index"

    task_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # Owning workspace: stable key + reopen recipe (mirrors the workspace registry).
    project_dir: Mapped[str] = mapped_column(String(1024), default="", index=True)
    workspace_root: Mapped[str] = mapped_column(String(1024), default="")
    workspace_kind: Mapped[str] = mapped_column(String(16), default="")  # named|in-place|path
    workspace_label: Mapped[str] = mapped_column(String(128), default="", index=True)
    # Denormalised task columns (discriminators + display); refreshed best-effort.
    project: Mapped[str] = mapped_column(String(128), default="default")
    channel: Mapped[str] = mapped_column(String(32), default="cli", index=True)
    external_key: Mapped[str] = mapped_column(String(256), default="", index=True)
    session_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    parent_task_id: Mapped[str] = mapped_column(String(40), default="")
    # Denormalised retry lineage so ``task --all`` can render/route attempts
    # without opening the owning workspace (mirrors :class:`TaskORM`).
    retry_of_task_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    root_task_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    schedule_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    kind: Mapped[str] = mapped_column(String(24), default="turn", index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


__all__ = [
    "Base",
    "SessionORM",
    "ConversationMessageORM",
    "TaskORM",
    "TaskIndexORM",
    "WorkflowRunORM",
    "WorkflowStepORM",
    "SubtaskORM",
    "TaskEventORM",
    "TaskControlORM",
    "WorkflowCheckpointORM",
    "OutboundDeliveryORM",
    "ComputeJobORM",
    "ScheduleORM",
    "ScheduleActionProposalORM",
    "ActionCheckpointORM",
    "MemoryEntryORM",
    "MemoryEdgeORM",
    "ArtifactORM",
    "SessionFocusORM",
    "SourceORM",
    "ChunkORM",
    "CitationORM",
    "HypothesisORM",
    "ClaimORM",
    "EvidenceORM",
    "RunORM",
    "_utcnow",
    "_uuid",
]
