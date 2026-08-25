"""OmniAgent — single entry point used by the CLI and every channel.

Responsibilities (the request flow distilled from HelixForge's runs route, now task-based):
  1. ensure session + persist the user turn
  2. recall memory + assemble the system prompt
  3. build the tool surface (builtin + sync skills + workflow + MCP)
  4. run the bounded ReAct loop
  5. persist the assistant turn, record memory
  6. drain inline background tasks (one-shot) or leave them for the daemon
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from omni.agent.artifact_revision_router import ArtifactRevisionRouter
from omni.agent.artifact_targets import ArtifactTargetResolver
from omni.agent.capabilities import CAPABILITY_ARTIFACT_REVISE, CAPABILITY_TASK_INSPECT
from omni.agent.conversation_store import ConversationStore, SessionDeleteOutcome
from omni.agent.cost import react_usage_limits, record_cost_event
from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.interaction_lifecycle import (
    InteractionLifecycle,
    build_approval_gate,
    react_tool_policy,
    unblock_produce_tools,
)
from omni.agent.persona_stoma import load_base_role, load_turn_persona_overlay
from omni.agent.plan_executor import PlanExecutor
from omni.agent.plan_fallthrough import (
    history_with_failed_attempt,
    loop_result_with_failed_attempt,
    policy_after_failed_route,
)
from omni.agent.plan_pipeline import PlanPipeline
from omni.agent.plan_recovery import (
    ACTION_NEEDS_INPUT,
    react_context_block,
)
from omni.agent.plan_revision import (
    ExecutionAuthority,
    create_execution_authority,
)
from omni.agent.plan_runner_utils import (
    approval_tools_for_plan,
    assumption_block,
    emit_tool_event,
    loop_result_event,
    needs_input_text,
    plan_capabilities,
    plan_summary,
)
from omni.agent.planner import IntentPlanner
from omni.agent.recent_activity import recent_activity_digest
from omni.agent.reviewer import review_and_correct
from omni.agent.schedule_goal import goal_grounded_in_message
from omni.agent.scheduled_goal_runner import ScheduledGoalRunner
from omni.agent.session_compactor import _COMPACT_THRESHOLD, SessionCompactor
from omni.agent.task_controller import TaskController
from omni.agent.tool_surface import ToolSurfaceBuilder
from omni.agent.turn_context import ContextSnapshot, TurnContextAssembler
from omni.agent.turn_execution import TurnCompletion, TurnExecution, TurnResult
from omni.agent.turn_memory import TurnMemory
from omni.channels.security import is_im_channel
from omni.config.settings import (
    OmniSettings,
    microcompact_token_budget,
    resolve_max_input_tokens,
    resolve_max_output_tokens,
    session_compact_token_budget,
)
from omni.core.action_contracts import ResolverContext
from omni.core.approval import (
    SENSITIVE_TOOLS,
    ApprovalGate,
    Approver,
    sandbox_is_write_capable,
)
from omni.core.approval_rules import SessionApprovalStore
from omni.core.execution_budget import ToolExecutionBudget
from omni.core.execution_control import ExecutionControl
from omni.core.llm import LLMClient, create_llm_client
from omni.core.observation import observation_spill_path
from omni.core.react_agent import AgentLoopResult, ReActLoopAgent
from omni.core.system_prompt import build_system_prompt
from omni.core.timefmt import local_time_context
from omni.core.tool_policy import (
    filter_tools_for_policy,
    policy_max_iterations,
    policy_max_tool_calls,
)
from omni.core.vlm import VlmGateway
from omni.memory.compiler import MemoryCompiler
from omni.memory.files import load_curated_memory
from omni.memory.notebook import read_recent
from omni.memory.service import MemoryService, open_global_store
from omni.runtime.artifact_revisions import (
    ArtifactRevisionResult,
)
from omni.runtime.execution_policy import ToolResourceLockPool
from omni.runtime.hooks import HookManager
from omni.runtime.memory_maintenance import maintenance_tick
from omni.runtime.notifications import InboxNotifier, Notifier
from omni.runtime.remaining import plan_owes_scientific_outputs
from omni.runtime.research_state import LiveTaskResearchFeed
from omni.runtime.scheduler import Scheduler
from omni.runtime.session_focus import SessionFocusService
from omni.runtime.subtask_runtime import SubtaskRuntime
from omni.runtime.task_continue import is_continue_request, resolve_continue_task
from omni.runtime.task_index import TaskIndex
from omni.runtime.task_recorder import TaskRecorder
from omni.runtime.tool_gateway import ToolGateway
from omni.skills_runtime.builtin_tools.fs import output_roots_for, write_roots_for
from omni.skills_runtime.context import ExecContext, Tool
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import Database, get_database
from omni.storage.models import ConversationMessageORM, SessionORM

logger = logging.getLogger(__name__)


def _artifact_mirror_dir(settings: OmniSettings) -> Path | None:
    """Resolve the single user-facing output folder.

    Trusted launches write into ``artifacts.output_dir`` (default ``outputs/``
    next to cwd). Untrusted / named home projects still get one findable folder
    at ``<project_dir>/outputs`` rather than scattering into ``artifacts/<kind>``.
    ``artifact://`` is not a directory — it is the SQLite id used to resolve
    these files after they move.
    """
    from omni.storage.artifacts import USER_OUTPUT_DIRNAME

    cfg = getattr(settings, "artifacts", None)
    if cfg is not None and cfg.mirror_outputs:
        base = Path((cfg.output_dir or USER_OUTPUT_DIRNAME).strip()).expanduser()
        if not base.is_absolute():
            base = Path.cwd() / base
        return base.resolve()
    paths = getattr(settings, "paths", None)
    if paths is None:
        return None
    return (paths.project_dir / USER_OUTPUT_DIRNAME).resolve()


def _react_on_token(plan: IntentPlan, on_token: Any) -> Any:
    """Buffer model prose when the host must project authoritative task facts."""
    if CAPABILITY_TASK_INSPECT in plan.capability_inputs:
        return None
    return on_token


# Owner / CLI identity for memory isolation (see MemoryService.PRINCIPAL_OWNER).
_PRINCIPAL_OWNER = "local"


class OmniAgent:
    def __init__(self, settings: OmniSettings, *, notifier: Notifier | None = None) -> None:
        self.settings = settings
        self.paths = settings.paths
        self.db: Database = get_database(self.paths.project_db)
        # Machine-global memory store (cross-workspace / cross-CLI / cross-channel).
        # ``None`` when memory.global_store is off → identical to legacy behaviour.
        self._global_db: Database | None = open_global_store(settings)
        # Session + transcript persistence (sessions, history, principal cache).
        self.conversations = ConversationStore(
            self.db,
            project_name=self.paths.project_name,
            channel_identity=settings.memory.channel_identity,
        )
        self.llm: LLMClient = create_llm_client(settings)
        self.vlm = VlmGateway(settings.vlm)
        self.registry = SkillRegistry(settings)
        self.memory = MemoryService(self.db, settings, llm=self.llm, global_db=self._global_db)
        self.artifacts = ArtifactStore(
            self.paths,
            self.db,
            mirror_dir=_artifact_mirror_dir(settings),
            mirror_formats=settings.artifacts.mirror_formats,
        )
        self.focus = SessionFocusService(self.db, self.paths)
        self.notifier = notifier or InboxNotifier(self.paths.project_dir / "inbox.jsonl")
        # Cross-workspace task index (control.sqlite3): mirrors task lifecycle for CLI list/route.
        self._task_index = TaskIndex.for_workspace(self.paths)
        self.tasks = TaskRecorder(
            self.db,
            project=self.paths.project_name,
            index=self._task_index,
            classify_conversational=settings.tasks.classify_conversational,
        )
        self.compactor = SessionCompactor(
            store=self.conversations,
            memory=self.memory,
            llm=self.llm,
            settings=self.settings,
            tasks=self.tasks,
        )
        self.turn_memory = TurnMemory(
            store=self.conversations,
            memory=self.memory,
            llm=self.llm,
            settings=self.settings,
            tasks=self.tasks,
            paths=self.paths,
        )
        self.hooks = HookManager(settings, self.tasks)
        self._tool_resource_locks = ToolResourceLockPool(
            lock_dir=self.paths.project_dir / ".resource-locks"
        )
        self.task_controller = TaskController(self.tasks)
        self.interaction = InteractionLifecycle(settings, self.tasks, self.hooks, self.task_controller)
        self.plan_pipeline = PlanPipeline(
            settings=settings,
            registry=self.registry,
            tasks=self.tasks,
            hooks=self.hooks,
        )
        self.runtime = SubtaskRuntime(
            self.db, settings, self.registry, self._make_ctx,
            notifier=self.notifier, memory=self.memory, task_recorder=self.tasks,
        )
        self.turn_completion = TurnCompletion(
            tasks=self.tasks,
            task_controller=self.task_controller,
            hooks=self.hooks,
            runtime=self.runtime,
            artifacts=self.artifacts,
            llm=self.llm,
            registry=self.registry,
        )
        self.artifact_targets = ArtifactTargetResolver(
            db=self.db,
            paths=self.paths,
            runtime=self.runtime,
            focus=self.focus,
            artifacts=self.artifacts,
        )
        self.artifact_revision = ArtifactRevisionRouter(
            artifact_targets=self.artifact_targets,
            tasks=self.tasks,
            conversations=self.conversations,
            turn_memory=self.turn_memory,
            focus=self.focus,
            runtime=self.runtime,
            registry=self.registry,
            paths=self.paths,
            db=self.db,
            artifacts=self.artifacts,
        )
        self.tool_surface = ToolSurfaceBuilder(self.runtime, self.tasks, self.registry, self._load_mcp_tools)
        # Goal schedules use the full headless planner/workflow path;
        # explicit-skill schedules retain the direct-enqueue path.
        self._scheduled_goals = ScheduledGoalRunner(self)
        self.scheduler = Scheduler(
            self.db, self.runtime, settings, goal_runner=self.run_scheduled_goal
        )
        self.runtime.add_tick_hook(self.scheduler.run_due)
        # Inert in an interactive window, which never starts the runtime; a
        # long-lived service is where parked memory work would otherwise pile up.
        self.runtime.add_tick_hook(maintenance_tick(self))
        self._role = load_base_role(role=self.settings.role or "", role_file=self.paths.role_file)
        self._ready = False
        # Interactive CLI approvals use per-session stores; daemon/IM/headless
        # calls fail closed without an approver.
        self.approver: Approver | None = None
        self._session_tool_allow: dict[str, SessionApprovalStore] = {}
        self._approved_task_tools: dict[str, set[str]] = {}
        self._workspace_auto_tasks: set[str] = set()
        # Runtime hosts may provide task-scoped tools (for example AstaBench's
        # date-pinned search and sandbox tools). They remain per-agent and still
        # traverse ToolGateway, approval, hooks, and resource serialization.
        self._external_tools: list[Tool] = []
        self._external_tools_authoritative = False

    @classmethod
    async def create(cls, settings: OmniSettings, *, notifier: Notifier | None = None) -> OmniAgent:
        agent = cls(settings, notifier=notifier)
        await agent.setup()
        return agent

    async def setup(self) -> None:
        if self._ready:
            return
        self.paths.ensure_dirs()
        await self.db.init()
        # One-shot, marker-guarded import of pre-index tasks (new tasks dual-write at creation).
        try:
            await self._task_index.backfill_workspace(
                self.db, marker=self.paths.project_dir / ".task_index_backfilled"
            )
        except Exception:  # noqa: BLE001 - index backfill is best-effort.
            logger.debug("task index backfill skipped", exc_info=True)
        # Open the machine-global memory store and backfill legacy identity rows
        # once per workspace (marker-guarded, dedup-safe) so enabling global_store
        # on an existing install carries the owner's memory across projects.
        if self._global_db is not None:
            await self._global_db.init()
            try:
                await self.memory.migrate_identity_to_global(
                    marker=self.paths.project_dir / ".global_memory_migrated"
                )
            except Exception:  # noqa: BLE001 — backfill is best-effort.
                logger.debug("identity→global migration skipped", exc_info=True)
        self.registry.build_index()
        # Bidirectional distillation: pull human-flagged lines from MEMORY.md /
        # NOTEBOOK.md into the store (pinned) so file edits become durable memory.
        try:
            from omni.memory.files import import_curated_memory
            await import_curated_memory(self.paths, self.memory)
        except Exception:  # noqa: BLE001
            logger.debug("curated memory import skipped", exc_info=True)
        self._ready = True

    def set_external_tools(self, tools: list[Tool], *, authoritative: bool = False) -> None:
        """Install task-scoped tools without mutating the durable skill registry.

        An authoritative catalog replaces local ReAct tools so a benchmark's
        constrained tool cannot be shadowed by a same-named local implementation.
        """
        self._external_tools = list(tools)
        self._external_tools_authoritative = authoritative

    # ── sessions ──
    async def ensure_session(
        self, *, channel: str = "cli", external_key: str = "", reuse_latest: bool = False,
        title: str = "",
    ) -> str:
        return await self.conversations.ensure_session(
            channel=channel, external_key=external_key, reuse_latest=reuse_latest, title=title,
        )

    def _principal_of(self, channel: str, external_key: str) -> str:
        return self.conversations.principal_of(channel, external_key)

    async def _principal_for_session(self, session_id: str) -> str:
        return await self.conversations.principal_for_session(session_id)

    async def _recent_rows(self, session_id: str) -> list[ConversationMessageORM]:
        return await self.conversations.recent_rows(session_id)

    @staticmethod
    def _normal_rows(rows: list[ConversationMessageORM]) -> list[ConversationMessageORM]:
        return ConversationStore.normal_rows(rows)

    async def _history(self, session_id: str, limit: int = 12) -> list[dict[str, Any]]:
        return await self.conversations.history(session_id, limit)

    async def _extraction_history(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        return await self.conversations.extraction_history(session_id, limit)

    async def _visible_normal_messages(self, session_id: str) -> list[ConversationMessageORM]:
        return await self.conversations.visible_normal_messages(session_id)

    async def _maybe_compact(self, session_id: str, *, task_id: str = "") -> None:
        await self.compactor.maybe_compact(session_id, task_id=task_id)

    async def compact_session(
        self,
        session_id: str,
        *,
        keep_last: int = 8,
        task_id: str = "",
    ) -> dict[str, int]:
        """Flush durable facts, then fold older turns into one compaction summary."""
        return await self.compactor.compact(session_id, keep_last=keep_last, task_id=task_id)

    async def _persist_message(self, session_id: str, role: str, content: str, **meta: Any) -> None:
        await self.conversations.persist_message(session_id, role, content, **meta)

    async def list_sessions(self, *, limit: int = 30) -> list[tuple[SessionORM, int]]:
        """Sessions in this workspace, newest first, with message counts."""
        return await self.conversations.list_sessions(limit=limit)

    async def get_session(self, session_id: str) -> SessionORM | None:
        """Resolve a session by exact id or unique prefix (within this workspace)."""
        return await self.conversations.get_session(session_id)

    async def session_messages(self, session_id: str) -> list[ConversationMessageORM]:
        """All messages for a session in chronological order."""
        return await self.conversations.session_messages(session_id)

    async def touch_session(self, session_id: str) -> bool:
        """Bump ``updated_at`` (and re-activate) so ``--continue`` picks it."""
        return await self.conversations.touch_session(session_id)

    async def fork_session(
        self, session_id: str, *, up_to_message: str = "", title: str = "",
    ) -> str | None:
        """Branch a session into a new one, copying its transcript (P2)."""
        return await self.conversations.fork_session(
            session_id, up_to_message=up_to_message, title=title,
        )

    async def rename_session(self, session_id: str, title: str) -> SessionORM | None:
        """Set the owner-authored session title without touching ``updated_at``."""
        return await self.conversations.set_session_title(session_id, title)

    async def delete_session(self, session_id: str) -> SessionDeleteOutcome:
        """Delete a conversation and the tasks that belong to it.

        A session is the transcript. Each turn is a Task keyed by
        ``tasks.session_id`` (a weak string, not an FK). Throwing the thread
        away also removes those turns via :meth:`TaskRecorder.delete_tasks`
        with ``force=True`` so completed / needs-input history does not linger
        as orphans. Running or recovering work still blocks. Artifact *files*
        survive, the same as ``/task rm``.
        """
        row = await self.conversations.get_session(session_id)
        if row is None:
            return SessionDeleteOutcome(
                session_id=session_id,
                deleted=False,
                code="not_found",
                message=f"session not found: {session_id}",
            )
        sid = row.id
        tasks = await self.tasks.list_tasks_for_session(sid)
        deleted_ids: tuple[str, ...] = ()
        if tasks:
            outcome = await self.tasks.delete_tasks(
                [task.id for task in tasks], force=True
            )
            if outcome.concurrent_write:
                return SessionDeleteOutcome(
                    session_id=sid,
                    deleted=False,
                    code="concurrent_write",
                    message="session deletion could not reserve the workspace; retry after it settles",
                )
            if outcome.blocked_tasks or outcome.blocked_executions:
                return SessionDeleteOutcome(
                    session_id=sid,
                    deleted=False,
                    code="busy",
                    message="session has active work; cancel or wait before deleting",
                )
            if outcome.missing_ids or outcome.protected_tasks:
                return SessionDeleteOutcome(
                    session_id=sid,
                    deleted=False,
                    code="busy",
                    message="session tasks could not be deleted; nothing was removed",
                )
            deleted_ids = tuple(outcome.deleted_ids)
        if not await self.conversations.delete_session(sid):
            return SessionDeleteOutcome(
                session_id=sid,
                deleted=False,
                deleted_task_ids=deleted_ids,
                code="not_found",
                message=f"session not found: {sid}",
            )
        return SessionDeleteOutcome(
            session_id=sid, deleted=True, deleted_task_ids=deleted_ids
        )

    # ── exec context + tools ──
    def _make_ctx(
        self,
        session_id: str,
        channel: str,
        file_uris: list[str] | None = None,
        *,
        task_id: str = "",
        principal: str = _PRINCIPAL_OWNER,
        execution_control: ExecutionControl | None = None,
        origin: str = "interactive",
        user_message: str = "",
        deferred_goal: str = "",
        receipt_time: Any = None,
    ) -> ExecContext:
        # Headless schedules have no live approver and cannot create schedules.
        autonomous = origin == "schedule"
        approver = None if autonomous else self.approver
        ctx = ExecContext(
            settings=self.settings, paths=self.paths, project=self.paths.project_name,
            session_id=session_id, channel=channel, principal=principal,
            task_id=task_id, file_uris=file_uris or [],
            db=self.db, artifacts=self.artifacts, llm=self.llm, vlm=self.vlm,
            registry=self.registry,
            hooks=self.hooks,
            approval_gate_factory=lambda task, source_channel, source_session, sensitive: build_approval_gate(
                settings=self.settings,
                tasks=self.tasks,
                approver=approver,
                session_allow=self._session_tool_allow,
                task_id=task,
                channel=source_channel,
                session_id=source_session,
                additional_sensitive_tools=sensitive,
                writable_roots=write_roots_for(
                    self.paths.project_dir,
                    self._tool_working_dir(channel),
                    self.settings.security.fs_write_allow,
                    self.artifacts.managed_output_roots,
                ),
                output_roots=output_roots_for(self.paths, self.artifacts),
                working_dir=self._tool_working_dir(channel),
                workspace=self.paths.project_dir,
                workspace_auto=task in self._workspace_auto_tasks,
            ),
            resource_locks=self._tool_resource_locks,
            working_dir=self._tool_working_dir(channel),
            task_recorder=self.tasks,
            execution_control=execution_control,
            origin=origin,
            autonomous=autonomous,
            allow_scheduling=not autonomous,
            deferred_goal=deferred_goal,
        )
        # Freeze this turn's semantic-admission facts once: the user's raw
        # message + a single reference time in the operator's IANA zone. Resolvers
        # (temporal, …) read "now" only from here, so the model's notion of the
        # current time and theirs never disagree. Only the attended coordinator
        # surface gets one (a headless scheduled run neither offers scheduling
        # nor needs a resolver).
        if not autonomous:
            now = local_time_context()
            ctx.resolver_context = ResolverContext(
                user_message=user_message or "",
                reference_time=receipt_time if receipt_time is not None else now.now,
                timezone=now.name or "",
                timezone_source="host",
                channel=channel,
                session_id=session_id,
                principal=principal,
                project_dir=str(self.paths.workspace_root or ""),
            )
        return ctx

    @staticmethod
    def _plan_deferred_goal(plan: Any) -> str:
        """Host-owned deferred goal for a SCHEDULE plan, when the planner extracted
        a *distinct* one.

        Returns the planner's ``task_contract.deferred_goal.objective`` only when
        it differs from the raw user message *and* is grounded in that message —
        a genuine clean goal, not the full-message fallback and not an Active-
        target inference the user did not state this turn. Empty otherwise, so
        ``schedule_task`` keeps trusting the model's goal or an open draft.
        """
        if getattr(plan, "intent_type", None) != IntentType.SCHEDULE:
            return ""
        contract = getattr(plan, "task_contract", None)
        deferred = contract.get("deferred_goal") if isinstance(contract, dict) else None
        objective = str(deferred.get("objective") or "").strip() if isinstance(deferred, dict) else ""
        raw = str(getattr(plan, "user_message", "") or "").strip()
        if objective and objective != raw and goal_grounded_in_message(objective, raw):
            return objective
        return ""

    async def _open_clarifications_block(self, ctx: ExecContext) -> str:
        """One-line-per-draft notice of this requester's open schedule clarifications.

        Surfaces durable ambiguous-time drafts at turn start so the model can
        resume one via ``resolve_action_checkpoint`` even after the conversation
        history that first asked the question has been compacted away. Scoped to
        the requester (principal+session); best-effort and never fatal.
        """
        if not getattr(ctx, "allow_scheduling", False) or ctx.db is None:
            return ""
        try:
            from omni.runtime.action_checkpoints import ActionCheckpointStore

            rows = await ActionCheckpointStore(ctx.db).list_open(
                principal=ctx.principal, session_id=ctx.session_id, limit=3
            )
        except Exception:  # noqa: BLE001 - a surfacing hint must never break a turn
            return ""
        if not rows:
            return ""
        lines = ["Open schedule clarifications you (this requester) can still answer:"]
        for rec in rows:
            resolution = rec.resolution or {}
            asked = str(resolution.get("raw_expression", "")).strip() or "a scheduled time"
            labels = [str(c.get("label", "")) for c in resolution.get("candidates", [])]
            options = ", ".join(label for label in labels if label) or "the offered options"
            goal = str((rec.payload or {}).get("goal") or "").strip()
            goal_bit = f" — goal: {goal}" if goal else ""
            lines.append(f"- id {rec.id[:8]}: '{asked}' — options: {options}{goal_bit}")
        lines.append(
            "If the user's message picks one of these listed readings, call resolve_action_checkpoint "
            "with that id and their choice (a candidate id like am/pm, or run_now/cancel). If they "
            "instead give a different or new time, call resolve_action_checkpoint with that id and "
            "the new time in 'when' or 'at' — the draft's goal is kept. Do not call schedule_task "
            "with a different goal. Otherwise ignore this."
        )
        return "\n".join(lines)

    def _tool_working_dir(self, channel: str) -> Path | None:
        """Directory local file/shell tools operate in for this turn.

        Local (CLI/terminal) turns operate on the folder the user launched from
        (``invocation_cwd``), so ``bash``/``write_file``/``edit_file`` act on the
        user's real directory like Claude Code — including it as a write/exec
        root. IM/daemon turns keep the path-keyed ``workspace_root`` so a remote
        channel never widens its scope to an arbitrary launch directory (and
        sensitive tools remain blocked there without a local approver anyway).
        """
        if is_im_channel(channel):
            return self.paths.workspace_root
        return self.paths.local_ops_dir

    async def _research_brief(self, task_id: str = "") -> str:
        from omni.runtime.research_state import opening_research_brief

        return await opening_research_brief(self.tasks, self.artifacts, self.db, task_id)

    def _domain_pack_brief(self) -> str:
        """Expose configured domain methods and specialist templates to ReAct."""
        try:
            from omni.research.domain_packs import DomainPackRegistry

            return DomainPackRegistry(self.settings).prompt()
        except Exception:  # noqa: BLE001 - domain guidance is additive
            return ""

    async def _recent_activity_block(
        self, *, principal: str = _PRINCIPAL_OWNER, limit: int = 6
    ) -> str:
        """Principal-scoped, cross-session digest of the caller's recent products.

        Gives the planner and ReAct turn the recent deliverables the caller can
        reopen (via get_task/get_subtask/open_artifact) so references to prior
        work resolve without re-asking; ``principal_of`` isolates per_peer callers.
        """
        try:
            return await recent_activity_digest(
                self.db,
                self.artifacts,
                principal=principal,
                principal_of=self._principal_of,
                limit=limit,
            )
        except Exception:  # noqa: BLE001 - continuity context is additive
            return ""

    async def _referenced_task_context(self, user_message: str, *, limit: int = 2) -> str:
        """Attach results for task identifiers explicitly present in this turn."""
        from omni.runtime.taskref import extract_task_ids, is_task_reference

        if not is_task_reference(user_message):
            return ""
        ids = extract_task_ids(user_message)
        if not ids:
            return ""
        # Lazy import avoids the agent → cli.state → agent import cycle.
        from omni.cli.commands.tasks_cmd import _subtask_attachment_context, resolve_subtask

        blocks: list[str] = []
        for tid in ids[:limit]:
            try:
                task = await resolve_subtask(self.runtime, tid)
            except Exception:  # noqa: BLE001
                task = None
            if task is not None:
                blocks.append(_subtask_attachment_context(task))
        if not blocks:
            return ""
        return (
            "[Referenced task context]\n"
            "Use these task outputs as context. Open an artifact only when its concrete content is needed; "
            "do not repeat completed work.\n\n"
            + "\n\n".join(blocks)
        )

    async def _apply_artifact_revision(
        self,
        user_message: str,
        *,
        session_id: str,
        channel: str,
        task_id: str = "",
        drain_tasks: bool = False,
        on_tool_event: Any = None,
        force_major: bool = False,
        edit_spec: dict[str, Any] | None = None,
    ) -> TurnResult | None:
        return await self.artifact_revision.apply(
            user_message,
            session_id=session_id,
            channel=channel,
            task_id=task_id,
            drain_tasks=drain_tasks,
            on_tool_event=on_tool_event,
            force_major=force_major,
            edit_spec=edit_spec,
        )

    async def _enforce_artifact_contracts(
        self,
        result: AgentLoopResult,
        *,
        session_id: str,
    ) -> ArtifactRevisionResult | None:
        return await self.artifact_revision.enforce_contracts(result, session_id=session_id)

    async def _build_tools(
        self,
        ctx: ExecContext,
        *,
        wait_for_tasks: bool = True,
        on_tool_event: Any = None,
    ) -> list[Tool]:
        return await self.tool_surface.build(
            ctx,
            wait_for_tasks=wait_for_tasks,
            on_tool_event=on_tool_event,
            external_tools=self._external_tools,
            external_authoritative=self._external_tools_authoritative,
        )

    async def _bind_turn_approvals(
        self, task_id: str, channel: str, *, workspace_auto: bool
    ) -> None:
        """Seed catalog + durable grants for this turn's approval envelope."""
        if workspace_auto and not is_im_channel(channel):
            self._workspace_auto_tasks.add(task_id)
            # Never + read-only must not pre-grant writes. Codex exec in an
            # untrusted folder still cannot edit; only a write-capable sandbox
            # inherits the durable workspace-auto tool set.
            if not sandbox_is_write_capable(self.settings):
                return
            granted = await self.tasks.grant_tools(
                task_id, sorted(SENSITIVE_TOOLS), reason="workspace-auto"
            )
            self._approved_task_tools[task_id] = (
                set(granted or SENSITIVE_TOOLS) & set(SENSITIVE_TOOLS)
            )
            return
        task = await self.tasks.get_task(task_id)
        existing = set(getattr(task, "approved_tools", None) or []) if task is not None else set()
        if existing:
            self._approved_task_tools[task_id] = existing & set(SENSITIVE_TOOLS)

    def _react_tool_policy(  # noqa: ANN001 - ToolPolicy
        self, policy, task_id: str = "", channel: str = "", execution_mode: str = ""
    ):
        """Effective ReAct policy: offer sensitive builtins the gate can clear."""
        return react_tool_policy(
            policy,
            settings=self.settings,
            approver=self.approver,
            approved=self._approved_task_tools.get(task_id, set()),
            channel=channel,
            read_only=execution_mode == "review",
            workspace_auto=task_id in getattr(self, "_workspace_auto_tasks", set()),
        )

    def _approval_gate(
        self,
        task_id: str,
        channel: str,
        session_id: str,
        *,
        additional_sensitive_tools: set[str] | None = None,
    ) -> ApprovalGate:
        """Build the approval boundary for sensitive ReAct tools."""
        return build_approval_gate(
            settings=self.settings,
            tasks=self.tasks,
            approver=self.approver,
            session_allow=self._session_tool_allow,
            task_id=task_id,
            channel=channel,
            session_id=session_id,
            additional_sensitive_tools=additional_sensitive_tools,
            writable_roots=write_roots_for(
                self.paths.project_dir,
                self._tool_working_dir(channel),
                self.settings.security.fs_write_allow,
                self.artifacts.managed_output_roots,
            ),
            output_roots=output_roots_for(self.paths, self.artifacts),
            working_dir=self._tool_working_dir(channel),
            workspace=self.paths.project_dir,
            workspace_auto=task_id in getattr(self, "_workspace_auto_tasks", set()),
        )
    async def _record_cost(
        self,
        task_id: str,
        result: Any,
        *,
        system: str,
        user_message: str,
        component: str = "coordinator",
    ) -> None:
        """Preserve the historic test seam while delegating cost accounting."""
        await record_cost_event(
            self.tasks, self.settings, self.llm, task_id, result,
            system=system,
            user_message=user_message,
            component=component,
        )

    async def _apply_settlement(self, task_id: str, result: Any) -> Any:
        return await self.task_controller.apply_settlement(task_id, result)

    async def _self_review_correct(
        self, *, react, result, system: str, user_message: str,  # noqa: ANN001
        tool_specs, history, task_id: str, force: bool = False,  # noqa: ANN001
    ):  # noqa: ANN201 - AgentLoopResult (avoid import churn)
        """Preserve the coordinator seam while delegating bounded self-review."""
        return await review_and_correct(
            llm=self.llm,
            cfg=self.settings.react,
            tasks=self.tasks,
            react=react,
            result=result,
            system=system,
            user_message=user_message,
            tool_specs=tool_specs,
            history=history,
            task_id=task_id,
            force=force,
            settings=self.settings,
        )

    async def _load_mcp_tools(self, ctx: ExecContext) -> list[Tool]:
        if not self.settings.mcp_servers:
            return []
        try:
            from omni.compat.mcp_client import load_mcp_tools
        except Exception as exc:  # noqa: BLE001
            logger.debug("MCP client unavailable: %s", exc)
            return []
        try:
            return await load_mcp_tools(self.settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to load MCP tools: %s", exc)
            return []

    async def _execute_intent_plan(
        self,
        plan: IntentPlan,
        user_message: str,
        *,
        session_id: str,
        channel: str,
        file_uris: list[str] | None,
        drain_tasks: bool,
        on_tool_event: Any = None,
        turn_context: Any = None,
        execution_control: ExecutionControl | None = None,
        execution_authority: ExecutionAuthority | None = None,
        origin: str = "interactive",
        receipt_time: Any = None,
    ):
        """Execute deterministic plan routes before falling back to wide ReAct."""
        if plan.intent_type not in {
            IntentType.QA_PLUS_ARTIFACT,
            IntentType.SINGLE_SKILL_TASK,
            IntentType.MEMORY_UPDATE,
            IntentType.SCHEDULE,
        }:
            return None
        if plan.intent_type in {IntentType.QA_PLUS_ARTIFACT, IntentType.SINGLE_SKILL_TASK} and not plan.selected_skills:
            return None
        principal = await self._principal_for_session(session_id)
        ctx = self._make_ctx(
            session_id,
            channel,
            file_uris,
            task_id=plan.task_id,
            principal=principal,
            execution_control=execution_control,
            origin=origin,
            user_message=user_message,
            deferred_goal=self._plan_deferred_goal(plan),
            receipt_time=receipt_time,
        )
        ctx.execution_authority = execution_authority
        if plan.intent_type == IntentType.MEMORY_UPDATE:
            policy_tools = []
        else:
            tools = await self._build_tools(ctx, wait_for_tasks=drain_tasks, on_tool_event=on_tool_event)
            policy_tools = filter_tools_for_policy(tools, plan.tool_policy)

        gateway = ToolGateway(
            task_id=plan.task_id,
            tools=policy_tools,
            tasks=self.tasks,
            event_family="plan",
            upstream=on_tool_event,
            hooks=self.hooks,
            approval_gate=ctx.approval_gate(),
            resource_locks=ctx.resource_locks,
            resource_scope=ctx.resource_scope,
            policy=plan.tool_policy,
        )
        ctx.tool_gateway = gateway
        result = await PlanExecutor(self.runtime, self.tasks, self.registry, self.memory).execute(
            plan,
            ctx=ctx,
            tools=policy_tools,
            drain_tasks=drain_tasks,
            on_tool_event=gateway.emit,
            active_target=getattr(turn_context, "active_target", None),
        )
        # An unhandled result is not always an empty one: a runner that tried and
        # failed returns the attempt so the turn can carry it into ReAct instead
        # of starting over blind. Callers decide on ``handled``.
        return result if (result.handled or result.drained_results) else None

    # ── main turn ──
    async def handle_turn(
        self, user_message: str, *, session_id: str | None = None, channel: str = "cli",
        file_uris: list[str] | None = None, drain_tasks: bool = True,
        on_tool_event: Any = None,
        on_task_ack: Any = None, on_token: Any = None,
        interaction_mode: str | None = None,
        approved_plan: IntentPlan | None = None,
        approved_authority: ExecutionAuthority | None = None,
        existing_task_id: str = "",
        origin: str = "interactive",
        workspace_auto: bool = False,
    ) -> TurnResult:
        """Run one turn under the shared durable cancellation boundary."""
        from omni.runtime.cancel_persist import persist_scope

        async with persist_scope(self.db):
            return await TurnExecution(
                self.tasks, self.task_controller, self._persist_message
            ).run(
                execute=self._handle_turn_impl,
                user_message=user_message,
                session_id=session_id,
                existing_task_id=existing_task_id,
                on_task_ack=on_task_ack,
                execute_kwargs={
                    "session_id": session_id,
                    "channel": channel,
                    "file_uris": file_uris,
                    "drain_tasks": drain_tasks,
                    "on_tool_event": on_tool_event,
                    "on_token": on_token,
                    "interaction_mode": interaction_mode,
                    "approved_plan": approved_plan,
                    "approved_authority": approved_authority,
                    "existing_task_id": existing_task_id,
                    "origin": origin,
                    "workspace_auto": workspace_auto,
                },
            )

    async def _handle_turn_impl(
        self, user_message: str, *, session_id: str | None = None, channel: str = "cli",
        file_uris: list[str] | None = None, drain_tasks: bool = True,
        on_tool_event: Any = None,
        on_task_ack: Any = None, on_token: Any = None,
        interaction_mode: str | None = None,
        approved_plan: IntentPlan | None = None,
        approved_authority: ExecutionAuthority | None = None,
        existing_task_id: str = "",
        execution_control: ExecutionControl | None = None,
        origin: str = "interactive",
        workspace_auto: bool = False,
    ) -> TurnResult:
        receipt_time = local_time_context().now
        await self.setup()
        if not existing_task_id:
            session_hint = session_id or await self.ensure_session(channel=channel)
            existing_task_id = await resolve_continue_task(
                self.tasks, user_message=user_message, session_id=session_hint
            )
            if existing_task_id and not session_id:
                session_id = session_hint
        start = await self.interaction.begin(
            user_message=user_message,
            session_id=session_id,
            channel=channel,
            interaction_mode=interaction_mode,
            existing_task_id=existing_task_id,
            ensure_session=self.ensure_session,
            on_task_ack=on_task_ack,
            file_uris=file_uris,
        )
        mode, session_id, channel = start.mode, start.session_id, start.channel
        user_message, task_id = start.user_message, start.task_id
        await self._bind_turn_approvals(
            task_id, channel, workspace_auto=workspace_auto
        )
        principal = await self._principal_for_session(session_id)
        react_events = ToolGateway(
            task_id=task_id,
            tools=[],
            tasks=self.tasks,
            event_family="react",
            upstream=on_tool_event,
            hooks=self.hooks,
        )
        emit_tool_event = react_events.emit

        if not existing_task_id or is_continue_request(user_message):
            await self._persist_message(session_id, "user", user_message)
            if not existing_task_id:
                await self._maybe_compact(session_id, task_id=task_id)
        turn_context = await TurnContextAssembler(
            db=self.db,
            paths=self.paths,
            focus=self.focus,
            artifacts=self.artifacts,
        ).assemble(
            session_id=session_id,
            channel=channel,
            user_message=user_message,
        )
        planning_memory = await MemoryCompiler(self.memory).compile_for_planning(
            query=user_message,
            session_id=session_id,
            token_budget=320,
            principal=principal,
        )
        # Cross-session, principal-scoped continuity, computed once and reused by
        # the planner and the ReAct turn so references to prior work resolve.
        recent_activity = await self._recent_activity_block(principal=principal)
        context_summary = "\n\n".join(
            part for part in (turn_context.to_planner_summary(), planning_memory.text) if part
        )
        await self.tasks.append_event(
            task_id,
            event_type="context.assembled",
            status="succeeded",
            name="turn_context",
            output_json={
                **turn_context.to_event_payload(),
                "compiled_memory": {
                    "ids": planning_memory.selected_memory_ids,
                    "layers": planning_memory.layers,
                    "omitted_count": planning_memory.omitted_count,
                    "budget": planning_memory.budget,
                },
            },
            summary=(context_summary or "no active turn context")[:220],
        )
        await _forward_plan_event(
            on_tool_event,
            {
                "event_type": "context.assembled",
                "name": "turn_context",
                "summary": (context_summary or "")[:220],
            },
        )
        pre_plan = await self.hooks.emit(
            "pre_plan",
            task_id=task_id,
            payload={"user_message": user_message, "mode": mode},
            deny_capable=True,
        )
        if not pre_plan.allowed:
            text = f"Planning was blocked by a lifecycle policy: {pre_plan.reason}"
            await self._persist_message(session_id, "assistant", text, kind="error")
            await self.task_controller.finish_turn(task_id, kind="error", text=text, error=pre_plan.reason)
            return TurnResult(
                text=text,
                session_id=session_id,
                task_id=task_id,
                kind="error",
                terminated_reason="hook_denied",
                settlement_status="failed",
            )

        pipeline = await self.plan_pipeline.run(
            llm=self.llm,
            user_message=user_message,
            task_id=task_id,
            mode=mode,
            approved_plan=approved_plan,
            carry_contract=bool(existing_task_id) and approved_plan is None,
            turn_context=turn_context,
            context_summary=context_summary,
            recent_activity=recent_activity,
            on_tool_event=on_tool_event,
            forward=_forward_plan_event,
            planner_factory=IntentPlanner,
        )
        plan = pipeline.plan
        validation = pipeline.validation
        current_revision = pipeline.revision
        recovery = pipeline.recovery
        approval_bound_hash = pipeline.approval_bound_hash
        recovery_react_notes = pipeline.recovery_react_notes
        if pipeline.hard_stop_reasons:
            reasons = pipeline.hard_stop_reasons
            text = (
                "Execution stopped because the plan failed a safety policy.\n"
                + "\n".join(f"- {item}" for item in reasons)
            )
            await self._persist_message(
                session_id,
                "assistant",
                text,
                kind="error",
                terminated_reason="plan_validation_failed",
            )
            await self.task_controller.finish_turn(
                task_id,
                kind="error",
                text=text,
                error="; ".join(reasons),
            )
            return TurnResult(
                text=text,
                session_id=session_id,
                task_id=task_id,
                kind="error",
                terminated_reason="plan_validation_failed",
                plan_summary=plan_summary(plan),
                degraded_warnings=[
                    *validation.warnings,
                    *validation.degraded_warnings,
                ],
                settlement_status="failed",
            )

        if recovery.action == ACTION_NEEDS_INPUT or plan.intent_type == IntentType.NEEDS_INPUT:
            missing = plan.missing_inputs or []
            text = needs_input_text(missing)
            await self._persist_message(
                session_id,
                "assistant",
                text,
                kind="needs_input",
                terminated_reason="needs_input",
            )
            await self.task_controller.finish_turn(
                task_id,
                kind="needs_input",
                text=text,
                task_status="needs_input",
                missing_inputs=missing,
            )
            return TurnResult(
                text=text,
                session_id=session_id,
                task_id=task_id,
                kind="needs_input",
                terminated_reason="needs_input",
                plan_summary=plan_summary(plan),
                degraded_warnings=list(plan.degraded_warnings),
                user_notices=list(plan.user_notices),
                settlement_status="needs_input",
            )

        current_authority = pipeline.execution_authority
        if current_authority is None:
            raise RuntimeError(
                "accepted plan pipeline result is missing execution authority"
            )

        approval_result = await self.interaction.gate_plan_execution(
            plan=plan,
            authority=current_authority,
            revision=current_revision,
            approval_bound_hash=approval_bound_hash,
            approved_plan=approved_plan,
            approved_authority=approved_authority,
            mode=mode,
            session_id=session_id,
            persist_message=self._persist_message,
            on_tool_event=on_tool_event,
            forward=_forward_plan_event,
        )
        if approval_result is not None:
            return approval_result

        bound_execution_authority = await self.plan_pipeline.bind_execution_plan(
            plan,
            current_revision,
            on_tool_event=on_tool_event,
            forward=_forward_plan_event,
        )

        # Model chose an in-place figure edit (capability indirection): run the
        # contract-validated patch, auto-escalating to a redraw when the target
        # cannot be grounded; degrades to normal planning when no active figure.
        if CAPABILITY_ARTIFACT_REVISE in plan_capabilities(plan):
            # Persist dispatch before a foreground child can finish and trigger
            # parent verification.  The verifier must never observe a completed
            # child before the plan event that caused it exists.
            await self.tasks.append_event(
                task_id,
                event_type="plan.executed",
                status="running",
                name=CAPABILITY_ARTIFACT_REVISE,
                output_json={"intent_type": plan.intent_type.value, "action": "dispatch"},
                summary="dispatching artifact revision",
            )
            await _forward_plan_event(
                on_tool_event,
                {
                    "event_type": "plan.executed",
                    "name": CAPABILITY_ARTIFACT_REVISE,
                    "summary": "dispatching artifact revision",
                },
            )
            edit_result = await self._apply_artifact_revision(
                user_message,
                session_id=session_id,
                channel=channel,
                task_id=task_id,
                drain_tasks=drain_tasks,
                on_tool_event=on_tool_event,
                force_major=True,
                edit_spec=plan.capability_inputs.get(CAPABILITY_ARTIFACT_REVISE),
            )
            if edit_result is not None:
                edit_result.task_id = task_id
                await self.task_controller.finish_turn(
                    task_id,
                    kind=edit_result.kind,
                    text=edit_result.text,
                    submitted_workflow_ids=edit_result.submitted_workflow_ids,
                    submitted_subtask_ids=edit_result.submitted_subtask_ids,
                    drain_tasks=drain_tasks,
                )
                return await self._apply_settlement(task_id, edit_result)

        direct_result = await self._execute_intent_plan(
            plan,
            user_message,
            session_id=session_id,
            channel=channel,
            file_uris=file_uris,
            drain_tasks=drain_tasks,
            on_tool_event=on_tool_event,
            turn_context=turn_context,
            execution_control=execution_control,
            execution_authority=bound_execution_authority,
            origin=origin,
            receipt_time=receipt_time,
        )
        if direct_result is not None and direct_result.handled:
            return await self.turn_completion.complete_plan(
                plan=plan,
                result=direct_result,
                session_id=session_id,
                user_message=user_message,
                drain_tasks=drain_tasks,
                persist_message=self._persist_message,
                record_turn_memory=self._record_turn_memory,
                apply_settlement=self._apply_settlement,
                channel=channel,
            )

        ctx = self._make_ctx(
            session_id,
            channel,
            file_uris,
            task_id=task_id,
            principal=principal,
            execution_control=execution_control,
            origin=origin,
            user_message=user_message,
            deferred_goal=self._plan_deferred_goal(plan),
            receipt_time=receipt_time,
        )
        # Turn-scoped async multi-agent control plane (Codex ``AgentControl``
        # analog). Present only when async delegation is enabled; the async
        # spawn/wait/list/interrupt tools read it from ``ctx`` and it is joined or
        # cancelled in the ``finally`` around the ReAct loop below.
        if self.settings.subagents.async_enabled:
            from omni.agent.subagent_control import SubagentControl

            ctx.subagent_control = SubagentControl(ctx, cfg=self.settings.subagents, depth=0)
        tools = await self._build_tools(
            ctx,
            wait_for_tasks=drain_tasks,
            on_tool_event=emit_tool_event,
        )
        # Sensitive builtins (bash/write/edit/compute) are declared blocked by the
        # planner but governed at execution by the approval gate (Claude Code /
        # Codex parity). With no owner to ask, a write can still be settled from
        # its destination while a shell command cannot. See ``react_tool_policy``.
        routed = policy_after_failed_route(plan.tool_policy, direct_result)
        react_policy = unblock_produce_tools(
            self._react_tool_policy(routed, task_id, channel, plan.execution_mode),
            plan,
        )
        policy_tools = filter_tools_for_policy(tools, react_policy)
        react_tool_limit = policy_max_tool_calls(
            react_policy, self.settings.react.max_tool_calls
        )
        react_gateway = ToolGateway(
            task_id=task_id,
            tools=policy_tools,
            tasks=self.tasks,
            event_family="react",
            upstream=on_tool_event,
            hooks=self.hooks,
            approval_gate=ctx.approval_gate(),
            resource_locks=ctx.resource_locks,
            resource_scope=ctx.resource_scope,
            policy=replace(react_policy, max_tool_calls=None),
        )
        tool_specs = react_gateway.tool_specs
        invoker = react_gateway.react_invoker()

        compiled_memory = await MemoryCompiler(self.memory).compile_for_turn(
            plan,
            query=user_message,
            session_id=session_id,
            token_budget=700,
            principal=principal,
        )
        memory_block = compiled_memory.text
        skill_catalog = (
            self.registry.react_skill_catalog(context_window_tokens=resolve_max_input_tokens(self.settings))
            if plan.context_policy.include_skill_catalog and self.registry.list_all()
            else ""
        )
        fact_feed = LiveTaskResearchFeed(
            tasks=self.tasks,
            artifacts=self.artifacts,
            db=self.db,
            task_id=task_id,
            plan=plan,
            resumed=bool(existing_task_id),
        )
        research_brief = await fact_feed.opening_snapshot()
        domain_pack_brief = self._domain_pack_brief() if plan.context_policy.include_research_brief else ""
        referenced = await self._referenced_task_context(user_message) if plan.context_policy.include_referenced_tasks else ""
        turn_context_block = context_summary if plan.context_policy.include_referenced_tasks else ""
        recent_activity_block = recent_activity if plan.context_policy.include_recent_activity else ""
        recovery_block = react_context_block(recovery_react_notes)
        assumptions = assumption_block(plan.missing_inputs)
        clarification_block = await self._open_clarifications_block(ctx)
        persona_overlay = load_turn_persona_overlay(self.paths, channel=ctx.channel)
        system = build_system_prompt(
            role=self._role, tools=tool_specs, persona_overlay=persona_overlay,
            memory_block="\n\n".join(
                x for x in (
                    clarification_block, recovery_block, assumptions, turn_context_block, referenced,
                    research_brief, domain_pack_brief, memory_block, skill_catalog,
                ) if x
            ),
            project_memory=load_curated_memory(self.paths),
            recent_activity=recent_activity_block,
            project_name=self.paths.project_name,
            notebook_summary=read_recent(self.paths.notebook, max_chars=800),
            working_dir=ctx.working_dir,
        )
        history = history_with_failed_attempt(await self._history(session_id), direct_result)

        turn_tool_budget = ToolExecutionBudget(react_tool_limit)
        react = ReActLoopAgent(
            self.llm, invoker,
            max_iterations=policy_max_iterations(plan.tool_policy, self.settings.react.max_iterations),
            max_tool_calls=react_tool_limit,
            max_seconds=self._react_max_seconds(getattr(ctx, "origin", "interactive")),
            stall_timeout_s=self.settings.react.stall_timeout_s,
            soft_timeout_s=self.settings.react.foreground_soft_seconds,
            finalization_timeout_s=self.settings.react.finalization_timeout_s,
            finalization_attempts=self.settings.react.finalization_attempts,
            temperature=self.settings.model.temperature,
            max_tokens=resolve_max_output_tokens(self.settings.model),
            soft_token_limit=microcompact_token_budget(self.settings),
            context_rollover_token_limit=session_compact_token_budget(self.settings),
            microcompact_keep_tool_results=int(
                getattr(self.settings.memory, "microcompact_keep_tool_results", 0) or 0
            ),
            observation_max_chars=int(
                getattr(self.settings.memory, "tool_observation_max_chars", 8000) or 0
            ),
            observation_spill_dir=str(observation_spill_path(self.paths)),
            no_progress_threshold=self.settings.react.no_progress_threshold,
            shared_tool_budget=turn_tool_budget,
            require_opening_tool=plan.tool_policy.require_opening_tool,
            owes_scientific_outputs=plan_owes_scientific_outputs(plan),
            fact_feed=fact_feed,
            **react_usage_limits(self.settings, self.llm),
        )
        try:
            result: AgentLoopResult = await react.run(
                system_prompt=system, user_message=user_message, tools=tool_specs,
                history=history,
                allow_escalation=plan.tool_policy.allows("escalate_run"),
                on_tool_event=react_gateway.emit,
                on_token=_react_on_token(plan, on_token),
                execution_control=execution_control,
            )
            execution_status, execution_payload = loop_result_event(result)
            await self.tasks.append_event(
                task_id,
                event_type="execution.finished",
                status=execution_status,
                name="react",
                output_json=execution_payload,
                summary=f"execution {result.kind}: {result.terminated_reason}",
            )

            await self._record_cost(task_id, result, system=system, user_message=user_message)

            if result.terminated_reason != "cancelled" and result.kind != "needs_input":
                result = await self._self_review_correct(
                    react=react, result=result, system=system, user_message=user_message,
                    tool_specs=tool_specs, history=history, task_id=task_id, force=mode == "review",
                )

                contract_result = await self._enforce_artifact_contracts(result, session_id=session_id)
                if contract_result is not None:
                    if result.content:
                        result.content = f"{result.content.rstrip()}\n\n{contract_result.message}"
                    else:
                        result.content = contract_result.message
                    if not contract_result.ok:
                        result.kind = "error"
                        result.terminated_reason = "artifact_contract_failed"
        finally:
            if ctx.subagent_control is not None:
                await ctx.subagent_control.aclose(grace_s=2.0)

        return await self.turn_completion.complete_react(
            plan=plan,
            result=loop_result_with_failed_attempt(result, direct_result),
            session_id=session_id,
            user_message=user_message,
            channel=channel,
            drain_tasks=drain_tasks,
            emit_tool_event=emit_tool_event,
            maybe_escalate=self._maybe_escalate,
            persist_message=self._persist_message,
            record_turn_memory=self._record_turn_memory,
            apply_settlement=self._apply_settlement,
        )

    async def run_scheduled_goal(self, **kwargs: Any) -> TurnResult | None:
        """Delegate one due goal to the headless scheduled-goal runner."""
        return await self._scheduled_goals.run(**kwargs)

    async def approve_task(self, task_id: str, *, drain_tasks: bool = True) -> TurnResult:
        """Execute a persisted plan-mode task without creating a second task."""
        await self.setup()
        task = await self.tasks.get_task(task_id)
        if task is None:
            raise LookupError(f"task not found: {task_id}")
        if task.status != "awaiting_approval":
            raise ValueError(f"task {task.id[:8]} is not awaiting approval ({task.status})")
        if not isinstance(task.plan_json, dict) or not task.plan_json:
            raise ValueError(f"task {task.id[:8]} has no persisted plan")
        def authority_for_plan(plan: IntentPlan) -> ExecutionAuthority:
            return create_execution_authority(
                plan,
                registry=self.registry,
                approval_tools=approval_tools_for_plan(plan, self.registry),
            )

        async def bind_plan_tools(
            plan: IntentPlan,
            authority: ExecutionAuthority,
        ) -> None:
            direct_tools = set(plan.tool_policy.allowed_tools or []) - set(
                plan.tool_policy.blocked_tools or []
            )
            self._approved_task_tools[task.id] = (
                set(authority.approval_tools) & direct_tools
            )

        return await self.interaction.approve(
            task_id,
            drain_tasks=drain_tasks,
            execute=self.handle_turn,
            authority_for_plan=authority_for_plan,
            before_execute=bind_plan_tools,
        )

    def _react_max_seconds(self, origin: str) -> float:
        """Overall wall-clock ceiling (layer 2) for a turn's ReAct loop.

        Headless scheduled / long-running research turns (``origin="schedule"``)
        get the larger ceiling so an autonomous multi-stage job is not clipped at
        the interactive bound; the progress watchdog (layer 1) and the soft
        foreground notice (layer 3) are shared. A time layer never fails the turn
        — it forces a final synthesis and settles ``degraded`` with the best
        answer gathered so far.
        """
        if origin == "schedule":
            return self.settings.react.scheduled_max_seconds
        return self.settings.react.max_seconds

    async def _maybe_escalate(self, goal: str, session_id: str, channel: str, *, task_id: str = "") -> str | None:
        from omni.agent.turn_escalate import maybe_escalate_run

        return await maybe_escalate_run(self, goal, session_id, channel, task_id=task_id)

    async def _record_turn_memory(
        self,
        session_id: str,
        user_message: str,
        result: AgentLoopResult,
        *,
        task_id: str = "",
    ) -> None:
        await self.turn_memory.record(session_id, user_message, result, task_id=task_id)

    async def _consolidate(self, session_id: str, *, task_id: str = "") -> list[str]:
        """Run session extraction under a per-session single-flight lock."""
        return await self.turn_memory.consolidate(session_id, task_id=task_id)

    async def end_session(self, session_id: str) -> list[str]:
        """Consolidate + maintain durable memory now (nobody is waiting)."""
        return await self.turn_memory.end_session(session_id)

    async def enqueue_session_maintenance(self, session_id: str) -> str:
        """Record that a session owes durable-memory maintenance, and return.

        What interactive surfaces call when a session ends: parking costs one
        database write, where performing the pass costs several model round
        trips. See :meth:`drain_pending_maintenance`.
        """
        return await self.turn_memory.enqueue_session_maintenance(session_id)

    async def drain_pending_maintenance(
        self, *, limit: int = 5, stale_after_s: float = 1800.0
    ) -> int:
        """Run the maintenance passes earlier sessions parked. Best-effort."""
        return await self.turn_memory.drain_pending_maintenance(
            limit=limit, stale_after_s=stale_after_s
        )

    async def context_snapshot(
        self,
        session_id: str,
        *,
        include_injected: bool = True,
    ) -> ContextSnapshot:
        """Estimate the bounded context carried into the next model turn."""
        from omni.config.settings import resolve_max_input_tokens

        rows = await self.session_messages(session_id)
        active = self._normal_rows(rows)
        history = await self._history(session_id)
        blocks: dict[str, str] = {}
        if include_injected:
            principal = await self._principal_for_session(session_id)
            recall = await self.memory.recall(
                "", session_id=session_id, limit=8, principal=principal
            )
            blocks = {
                "project_memory": load_curated_memory(self.paths),
                "recalled_memory": self.memory.build_recall_block(recall),
                "recent_activity": await self._recent_activity_block(principal=principal),
                "research_brief": await self._research_brief(),
                "notebook": read_recent(self.paths.notebook, max_chars=800),
            }
        target = await self.focus.latest(session_id)
        focus = target.focus if target is not None else None
        return ContextSnapshot.create(
            session_id=session_id,
            model="/".join(filter(None, (self.settings.model.provider, self.settings.model.model))),
            stored_messages=len(rows),
            active_messages=len(active),
            prompt_history=history,
            compacted_messages=sum(bool((row.meta or {}).get("compacted")) for row in rows),
            injected_text=blocks,
            context_window_tokens=resolve_max_input_tokens(self.settings),
            focus=focus,
        )

    async def context_report(self, session_id: str) -> str:
        """Return token-based diagnostics for the context of the next turn."""
        snapshot = await self.context_snapshot(session_id)
        return snapshot.render(
            near_compaction_threshold=snapshot.active_messages >= _COMPACT_THRESHOLD
        )

    async def aclose(self) -> None:
        # Stop detached scheduled goals before their database closes.
        try:
            await self.scheduler.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.runtime.stop()
        except Exception:  # noqa: BLE001
            pass


async def _forward_plan_event(on_tool_event: Any, event: dict[str, Any]) -> None:
    """Mirror a durable planning event onto the live turn-event stream.

    The DB record (``tasks.append_event``) stays authoritative; this only lets
    interactive surfaces (the CLI) narrate planning/validation/recovery as it
    happens. Channels that pass no callback are unaffected.
    """
    await emit_tool_event(
        on_tool_event,
        "plan",
        {
            "event_type": str(event.get("event_type") or ""),
            "name": str(event.get("name") or ""),
            "summary": str(event.get("summary") or ""),
            "payload": event.get("output_json") if isinstance(event.get("output_json"), dict) else {},
            **({"subtask_id": str(event["subtask_id"])} if event.get("subtask_id") else {}),
        },
    )
