"""Agent-facing scheduling tools: turn a request into a durable scheduled task.

These let the coordinating ReAct turn create / inspect / cancel scheduled jobs
from natural language ("summarise today's research every day at 6pm", in any
language) instead of dead-ending in capability matching. The scheduled unit of
work is the built-in ``agent-goal`` skill — a focused ReAct sub-agent that
pursues a free-form goal — so the schedule carries only the goal text plus a
trigger; when it comes due the scheduler materialises a normal background task
whose result lands in the inbox/session.

Architecture: ``schedule_task`` is a *thin adapter* over
:class:`omni.scheduling.service.ScheduleService` — it builds the one canonical
:class:`~omni.scheduling.contracts.ScheduleCreateRequest` and lets the service
own trigger vocabulary, time semantics, skill validation, and the create-vs-
propose consent decision (so the CLI and this tool cannot drift). The service
resolves an IM-originated request (no local approver) into a durable approval
proposal the owner later runs `omni schedule approve <id>` on — Codex's
request/response-by-id approval, made durable — instead of the generic approval
gate flatly denying it. Every terminal outcome is recorded as a single
``schedule.resolved`` event so verification requires a real scheduling result,
not model prose. Cron is read in the operator's local timezone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from omni.agent.schedule_goal import seal_schedule_work
from omni.core.action_contracts import ResolutionStatus, ResolverContext
from omni.core.react_agent import ToolSpec
from omni.core.timefmt import format_local_time, local_time_context
from omni.runtime.action_checkpoints import ActionCheckpointStore, AmbiguousCheckpointId
from omni.runtime.scheduler import Scheduler
from omni.scheduling.action import (
    SCHEDULE_CREATE_CONTRACT,
    canonical_schedule_trigger,
    prepare_schedule_create,
    temporal_clarification_payload,
)
from omni.scheduling.contracts import (
    GOAL_SKILL,
    ScheduleActor,
    ScheduleCreateRequest,
)
from omni.scheduling.service import ScheduleService
from omni.scheduling.temporal import POLICY_VERSION
from omni.skills_runtime.context import ExecContext, Tool

_SCHEDULE_TASK_SPEC = ToolSpec(
    name="schedule_task",
    # The time-grounding rules used to be restated here in full. They live on the
    # 'when' / 'at' / 'cron' parameters instead, which is where the model reads
    # them while filling the call, and which the provider sends anyway — so
    # repeating them at tool level cost every iteration and taught nothing new.
    description=(
        "Create a recurring or one-time scheduled task that later runs an autonomous agent on a "
        "natural-language goal and delivers the result to the inbox. Use this whenever the user asks "
        "to run something on a schedule (e.g. a daily research digest). Provide 'goal' plus exactly "
        "ONE trigger: 'when' for a time stated in words, or 'cron'/'every_seconds'/'at' for an exact "
        "machine value. Do not perform the goal now; only register the schedule. The tool result is "
        "the truthful outcome (created, needs approval, time in the past, or ambiguous). Do not "
        "recast that outcome as a different status."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "What the agent should do each time it runs, in the user's language.",
            },
            "when": SCHEDULE_CREATE_CONTRACT.proposal_schema["properties"]["when"],
            "cron": {
                "type": "string",
                "description": "Exact 5-field cron in LOCAL time, e.g. '0 18 * * *' = every day 18:00. Only for exact user input.",
            },
            "every_seconds": {
                "type": "integer",
                "description": "Exact fixed interval in seconds between runs (e.g. 3600 = hourly).",
            },
            "at": {
                "type": "string",
                "description": "Exact one-time ISO-8601 datetime the user gave literally, e.g. '2026-07-11T09:00'. For worded times use 'when' instead.",
            },
            "timezone": {
                "type": "string",
                "description": "Optional IANA timezone for a naive 'at' (e.g. 'Asia/Shanghai'); defaults to the operator's local zone.",
            },
            "title": {
                "type": "string",
                "description": "Optional short human-readable title for the schedule.",
            },
        },
        "required": ["goal"],
    },
)

_LIST_SCHEDULES_SPEC = ToolSpec(
    name="list_schedules",
    description="List existing scheduled tasks with their trigger, next run time (local), status, and run count.",
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Maximum schedules to return (default 30)."},
        },
        "required": [],
    },
)

_RESOLVE_CHECKPOINT_SPEC = ToolSpec(
    name="resolve_action_checkpoint",
    description=(
        "Answer a pending schedule-time clarification the system asked earlier (it returned a "
        "'draft_id' and numbered choices because a worded time was ambiguous). If the user picks "
        "one of the listed readings, pass that choice. If they give a different or new time, pass "
        "it in 'when' (worded) or 'at' (ISO) — the draft's goal is kept; do not call schedule_task "
        "with a different goal. Only the original requester can answer. The returned result is "
        "the truthful outcome; do not recast it as a different status."
    ),
    parameters={
        "type": "object",
        "properties": {
            "checkpoint_id": {"type": "string", "description": "The draft_id from the earlier clarification."},
            "choice": {
                "type": "string",
                "description": (
                    "A candidate id ('am'/'pm'), a listed choice id (e.g. 'pick:pm', "
                    "'repair_next_day:am'), or 'run_now'/'cancel'."
                ),
            },
            "when": SCHEDULE_CREATE_CONTRACT.proposal_schema["properties"]["when"],
            "at": {
                "type": "string",
                "description": (
                    "Exact one-time ISO-8601 datetime when the user gave a new time instead of "
                    "picking a listed reading. The draft goal is kept."
                ),
            },
        },
        "required": ["checkpoint_id"],
    },
)

_CANCEL_SCHEDULE_SPEC = ToolSpec(
    name="cancel_schedule",
    description="Delete a scheduled task by its id (an id prefix from list_schedules is accepted).",
    parameters={
        "type": "object",
        "properties": {
            "schedule_id": {"type": "string", "description": "Schedule id (or a unique prefix) to delete."},
        },
        "required": ["schedule_id"],
    },
)


def _needs_input(message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "needs_input", "message": message, "error": message, **extra}


def _repairable_by_model(resolution: Any) -> bool:
    """Whether the *proposal*, not the user, is what needs fixing.

    ``INVALID`` means the arguments contradict wording the user already gave —
    quoting only the hour as evidence while proposing a minute, say. The model has
    everything it needs to correct that. ``AMBIGUOUS`` and ``MISSING`` are the
    opposite: what would settle them (a bare "7" — morning or evening?) exists only
    in the user's head, and no amount of re-reading finds it there.
    """
    return getattr(resolution, "status", None) is ResolutionStatus.INVALID


def _defect_signature(resolution: Any) -> str:
    """Coarse key bounding repairs to one per unresolved field, per turn.

    Deliberately excludes ``reason``: it names the offending value, so a model
    that keeps proposing *different* wrong minutes would earn a fresh retry every
    time and never reach the user.
    """
    status = getattr(getattr(resolution, "status", None), "value", "")
    return f"{status}|{','.join(getattr(resolution, 'unresolved_fields', ()) or ())}"


def _repair_payload(
    resolution: Any, raw: str, *, user_message: str = ""
) -> dict[str, Any]:
    """A tool error the model can act on — the shape of Codex's ``RespondToModel``.

    Carries no ``outcome``/``action_required`` key on purpose, so the ReAct loop's
    :func:`_is_terminal_tool_result` keeps running and this lands as the model's
    next observation instead of ending the turn.

    Cite the *user's* wording, never the model's quote: echoing ``raw`` taught
    one retry to keep a particle the user never wrote (2367d610).
    """
    reason = getattr(resolution, "reason", "") or "the proposed time did not check out"
    written = (user_message or "").strip()
    wording = (
        f" The user wrote: '{written}'."
        if written
        else (f" The proposed quote was '{raw}'." if raw else "")
    )
    message = (
        f"The schedule was not created: {reason}.{wording} Call schedule_task again, "
        "copying the time from the user's wording into clock.evidence — hour and "
        "minute together, plus the AM/PM word if the user gave one. Do not add or "
        "drop words. Ask the user only if their wording genuinely does not say."
    )
    return {
        "status": "error",
        "error": message,
        "summary": message,
        "resolution_status": getattr(getattr(resolution, "status", None), "value", "invalid"),
        "policy": POLICY_VERSION,
    }


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _spec_label(sched: Any) -> str:
    if sched.kind == "interval":
        return f"every {sched.interval_s}s"
    if sched.kind == "cron":
        return f"cron {sched.cron_expr}"
    return "once"


def _resolver_context(ctx: ExecContext) -> ResolverContext:
    """The frozen semantic-admission context for temporal resolution.

    Prefers the turn-scoped context the orchestrator froze (carrying the user's
    raw message + a single reference time); otherwise builds a best-effort one
    from the wall clock so DB-free/older callers still get ambiguity handling
    (evidence grounding simply no-ops without a user message).
    """
    existing = getattr(ctx, "resolver_context", None)
    if isinstance(existing, ResolverContext):
        return existing
    now = local_time_context()
    return ResolverContext(
        user_message="",
        reference_time=now.now,
        timezone=now.name or "",
        timezone_source="process",
        channel=ctx.channel or "cli",
        session_id=ctx.session_id,
        principal=getattr(ctx, "principal", "local") or "local",
        project_dir=str(getattr(ctx.paths, "project_dir", "") or ""),
    )


def build_schedule_tools(runtime: Any, ctx: ExecContext) -> list[Tool]:
    """Scheduling tools for the coordinating turn.

    Gated on ``ctx.db`` (schedules are DB-backed) so DB-free callers/tests are
    unaffected. Only offered on the top-level agent surface — not to prompt-skill
    sub-agents — so a scheduled ``agent-goal`` run cannot recursively schedule.
    """
    if getattr(ctx, "db", None) is None:
        return []

    channel = ctx.channel or "cli"
    # Defects already handed back to the model this turn (see ``_defect_signature``).
    # Turn-scoped because the tool surface is rebuilt per turn.
    repaired: set[str] = set()

    def _scheduler() -> Scheduler:
        return Scheduler(ctx.db, runtime, ctx.settings)

    def _service() -> ScheduleService:
        return ScheduleService(ctx.db, runtime, ctx.settings, registry=getattr(ctx, "registry", None))

    def _checkpoints() -> ActionCheckpointStore:
        return ActionCheckpointStore(ctx.db)

    def _decider() -> str:
        return getattr(ctx, "principal", "local") or "local"

    async def _persist_clarification(
        args: dict[str, Any], resolution: Any, raw: str
    ) -> str:
        """Durably persist an ambiguous schedule-time so it survives the turn.

        Only genuine multi-candidate choices are stored (a missing/invalid time
        has nothing to pick). Best-effort: a storage hiccup must never turn a
        clarification into a crash.
        """
        if resolution is None or not getattr(resolution, "candidates", ()):  # nothing to choose
            return ""
        try:
            record = await _checkpoints().open_clarification(
                action_kind="schedule.create",
                contract_version=SCHEDULE_CREATE_CONTRACT.version,
                policy_version=POLICY_VERSION,
                channel=channel,
                session_id=ctx.session_id,
                actor_principal=_decider(),
                required_decider=_decider(),
                origin_project_dir=str(getattr(ctx.paths, "project_dir", "") or ""),
                project=str(getattr(ctx.paths, "project_name", "default") or "default"),
                payload={
                    "goal": str(args.get("goal") or ""),
                    "title": str(args.get("title") or ""),
                    "when": args.get("when") or {},
                },
                resolution={
                    "status": resolution.status.value,
                    "reason": resolution.reason,
                    "unresolved_fields": list(resolution.unresolved_fields),
                    "raw_expression": raw,
                    "candidates": [
                        {"id": c.id, "value": c.value, "label": c.label, "validity": c.validity}
                        for c in resolution.candidates
                    ],
                },
            )
            return record.id
        except Exception:  # noqa: BLE001 - durability is best-effort, never fatal
            return ""

    async def _supersede_prior_drafts(exclude_id: str = "") -> None:
        """Cancel this requester's other open schedule clarifications.

        Once a fresh ``schedule_task`` resolves — a new schedule was created, or a
        new clarification was posed — any earlier unanswered draft for the same
        requester is stale. Cancel it so ``_open_clarifications_block`` does not
        re-surface a question the user has already moved on from. Scoped to
        (principal, session); best-effort and never fatal.
        """
        try:
            store = _checkpoints()
            rows = await store.list_open(
                principal=_decider(), session_id=ctx.session_id, limit=20
            )
            for rec in rows:
                if rec.id == exclude_id:
                    continue
                await store.cancel(rec.id, decider=_decider())
        except Exception:  # noqa: BLE001 - draft hygiene must never break scheduling
            pass

    async def _create_from_trigger(record: Any, trigger_value: dict[str, Any]) -> dict[str, Any]:
        payload = record.payload or {}
        request = ScheduleCreateRequest(
            trigger=canonical_schedule_trigger(trigger_value),
            goal=str(payload.get("goal") or ""),
            title=str(payload.get("title") or ""),
            actor=ScheduleActor(
                channel=record.channel or channel,
                session_id=record.session_id or ctx.session_id,
                principal=record.actor_principal or _decider(),
            ),
            reference_time=_resolver_context(ctx).reference_time,
            already_clarified=True,
        )
        result = await _service().create(request)
        out = result.tool_result()
        await _emit_action(
            "action.admitted" if result.status == "created" else "action.rejected",
            {
                "schedule_id": getattr(result, "schedule_id", "") or "",
                "trigger": trigger_value,
                "outcome": result.status,
                "via": "checkpoint",
            },
            status=result.status,
        )
        await _record_outcome(result.status, out)
        return out

    async def _record_outcome(outcome: str, payload: dict[str, Any]) -> None:
        recorder = getattr(ctx, "task_recorder", None)
        if recorder is None or not getattr(ctx, "task_id", ""):
            return
        try:
            await recorder.append_event(
                ctx.task_id,
                event_type="schedule.resolved",
                status=outcome,
                name="schedule_task",
                tool_name="schedule_task",
                output_json=payload,
                summary=str(payload.get("summary") or "")[:400],
            )
        except Exception:  # noqa: BLE001 - the audit event must never break scheduling
            pass

    async def _emit_action(event_type: str, data: dict[str, Any], *, status: str = "info") -> None:
        """Append one structured ``action.*`` admission-trail event (best-effort).

        Records only the safe admission facts (raw expression, candidate *labels*,
        policy version, decision, checkpoint/schedule ids, canonical trigger) —
        never model reasoning, credentials, or unredacted payloads (plan §13).
        """
        recorder = getattr(ctx, "task_recorder", None)
        if recorder is None or not getattr(ctx, "task_id", ""):
            return
        try:
            await recorder.append_event(
                ctx.task_id,
                event_type=event_type,
                status=status,
                name="schedule.create",
                tool_name="schedule_task",
                output_json=data,
                summary=str(data.get("summary") or "")[:400],
            )
        except Exception:  # noqa: BLE001 - the audit trail must never break scheduling
            pass

    def _candidate_labels(resolution: Any) -> list[str]:
        cands = getattr(resolution, "candidates", ()) or ()
        return [f"{c.label} [{c.validity}]" for c in cands]

    async def _latest_open_draft() -> Any | None:
        """Newest open schedule clarification for this requester, if any."""
        try:
            rows = await _checkpoints().list_open(
                principal=_decider(), session_id=ctx.session_id, limit=1
            )
        except Exception:  # noqa: BLE001 - draft lookup must never break scheduling
            return None
        return rows[0] if rows else None

    async def _admit_and_create(
        args: dict[str, Any],
        *,
        already_clarified: bool,
        goal_source: str,
    ) -> dict[str, Any]:
        raw = ""
        when = args.get("when")
        if isinstance(when, dict):
            raw = str(when.get("raw_expression", "")).strip()
        await _emit_action(
            "action.proposed",
            {"raw_expression": raw, "has_when": isinstance(when, dict),
             "exact": [k for k in ("cron", "every_seconds", "at") if args.get(k)],
             "goal_source": goal_source},
        )

        # Semantic admission: the model's proposal (a worded ``when`` and/or an
        # exact trigger) is bound to canonical arguments before anything is
        # created. An ambiguous/invalid critical time fails closed into a user
        # clarification — never a silently completed time.
        resolver = _resolver_context(ctx)
        decision = await prepare_schedule_create(args, resolver)
        # A READY decision (exact trigger or a uniquely-resolved time) carries no
        # ResolutionResult — report it as ``resolved`` rather than dereferencing None.
        res = decision.resolution
        res_status = res.status.value if res is not None else "resolved"
        await _emit_action(
            "action.resolution",
            {
                "status": res_status,
                "unresolved_fields": list(res.unresolved_fields) if res is not None else [],
                "candidates": _candidate_labels(res) if res is not None else [],
                "policy": POLICY_VERSION,
            },
            status=res_status,
        )
        # A defect in the model's *own* arguments is not a question for the user:
        # it contradicts wording the user already gave. It goes back as an ordinary
        # tool error and the loop continues — Codex's split, where everything the
        # model can fix (245 of its tool-error sites, safety rejections included)
        # returns through ``RespondToModel`` and only a fact the human holds
        # suspends the turn. No ``schedule.resolved`` is written here: that event is
        # the durable proof a schedule exists, and a repair request is not one.
        if decision.needs_input and _repairable_by_model(res):
            signature = _defect_signature(res)
            if signature not in repaired:
                repaired.add(signature)
                await _emit_action(
                    "action.repair_requested",
                    {"raw_expression": raw, "reason": getattr(res, "reason", ""),
                     "policy": POLICY_VERSION},
                    status=res_status,
                )
                return _repair_payload(res, raw, user_message=resolver.user_message)
        if decision.needs_input:
            payload = temporal_clarification_payload(decision.resolution, raw)
            # Persist the ambiguity as a durable, resumable draft so the user can
            # answer it later (even from another process/channel) via
            # ``resolve_action_checkpoint``, not only within this live turn.
            draft_id = await _persist_clarification(args, decision.resolution, raw)
            if draft_id:
                payload["draft_id"] = draft_id
                payload["message"] += f"\n(clarification id {draft_id[:8]}; reply with your choice)"
                await _emit_action(
                    "action.checkpoint.created",
                    {"checkpoint_id": draft_id, "phase": "semantic_clarification",
                     "candidates": _candidate_labels(decision.resolution)},
                )
                # A new clarification supersedes any earlier unanswered one.
                await _supersede_prior_drafts(exclude_id=draft_id)
            await _record_outcome("needs_input", payload)
            return payload
        if not decision.ready:  # defensive: rejected
            message = decision.reason or "This request cannot be scheduled."
            result = {"status": "rejected", "outcome": "rejected", "error": message, "summary": message}
            await _emit_action("action.rejected", {"reason": message}, status="rejected")
            await _record_outcome("rejected", result)
            return result

        canonical = decision.canonical_arguments or {}
        request = ScheduleCreateRequest(
            trigger=canonical_schedule_trigger(canonical.get("trigger") or {}),
            goal=str(canonical.get("goal") or args.get("goal") or ""),
            title=str(canonical.get("title") or ""),
            actor=ScheduleActor(
                channel=channel,
                session_id=ctx.session_id,
                principal=getattr(ctx, "principal", "local") or "local",
            ),
            reference_time=_resolver_context(ctx).reference_time,
            already_clarified=already_clarified,
        )
        result = await _service().create(request)
        payload = result.tool_result()
        await _emit_action(
            "action.admitted" if result.status == "created" else "action.rejected",
            {
                "schedule_id": getattr(result, "schedule_id", "") or "",
                "trigger": canonical.get("trigger") or {},
                "outcome": result.status,
            },
            status=result.status,
        )
        if result.status in {"created", "awaiting_approval"}:
            # The user moved forward to a concrete time (or a durable proposal);
            # drop any stale draft so a later time-only follow-up cannot reopen it.
            await _supersede_prior_drafts()
        await _record_outcome(result.status, payload)
        return payload

    async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
        # Seal the work item *before* anything is stored or shown. This-turn
        # user text still beats a drifted model goal (Decision #3). An open
        # draft beats an ungrounded host goal (Active target must not replace
        # a time-only follow-up). Display, storage, and fire share this object.
        draft = await _latest_open_draft()
        draft_payload = (draft.payload if draft is not None else None) or {}
        resolver = _resolver_context(ctx)
        sealed = seal_schedule_work(
            model_goal=str(args.get("goal") or ""),
            model_title=str(args.get("title") or ""),
            host_goal=str(getattr(ctx, "deferred_goal", "") or ""),
            user_message=str(getattr(resolver, "user_message", "") or ""),
            draft_goal=str(draft_payload.get("goal") or ""),
            draft_title=str(draft_payload.get("title") or ""),
        )
        if sealed.source == "conflict" or not sealed.goal:
            result = _needs_input(
                "Which work should I schedule? This turn only supplied a time, "
                "and I have no pending draft to reuse. Name the goal, or refer "
                "to a specific figure/report ('this figure', 'this report')."
            )
            await _record_outcome("needs_input", result)
            return result
        args = {**args, "goal": sealed.goal, "title": sealed.title}
        return await _admit_and_create(
            args,
            already_clarified=sealed.source == "draft",
            goal_source=sealed.source,
        )

    async def list_schedules(args: dict[str, Any]) -> dict[str, Any]:
        limit = _positive_int(args.get("limit")) or 30
        rows = await _scheduler().list(include_disabled=True, limit=limit)
        return {
            "status": "ok",
            "count": len(rows),
            "schedules": [
                {
                    "id": s.id[:8],
                    "title": s.title or "-",
                    "goal": str((s.input_json or {}).get("input") or "")[:120],
                    "spec": _spec_label(s),
                    "next_run": format_local_time(s.next_due_at) if s.next_due_at else "-",
                    "enabled": bool(s.enabled),
                    "runs": int(s.run_count or 0),
                }
                for s in rows
            ],
        }

    def _next_day_trigger(value: dict[str, Any]) -> dict[str, Any]:
        """Same wall-clock, next day — the repair offered for an elapsed candidate."""
        at = str((value or {}).get("at", "")).strip()
        try:
            nxt = (datetime.fromisoformat(at) + timedelta(days=1)).isoformat(timespec="seconds")
        except ValueError:
            nxt = at
        return {"kind": "once", "at": nxt, "timezone": str((value or {}).get("timezone", ""))}

    # Language-neutral aliases only. The model interprets the user's own words
    # (in any language) and passes a stable id here — the tool never does NL.
    _PERIOD_ALIASES = {
        "pm": "pm", "evening": "pm", "afternoon": "pm", "night": "pm",
        "am": "am", "morning": "am",
    }

    def _map_choice_to_candidate(choice: str, record: Any) -> str:
        raw = choice.strip()
        low = raw.lower()
        if record.candidate(raw) is not None:
            return raw
        for prefix in ("pick:", "repair_next_day:"):
            if raw.startswith(prefix):
                return raw.split(":", 1)[1]
        mapped = _PERIOD_ALIASES.get(low)
        return mapped if mapped and record.candidate(mapped) is not None else ""

    async def resolve_action_checkpoint(args: dict[str, Any]) -> dict[str, Any]:
        checkpoint_id = str(args.get("checkpoint_id", "")).strip()
        choice = str(args.get("choice", "")).strip()
        if not checkpoint_id:
            return _needs_input("Provide the clarification draft id (see the earlier question).")
        store = _checkpoints()
        try:
            record = await store.get(checkpoint_id)
        except AmbiguousCheckpointId as exc:
            return _needs_input(str(exc))
        if record is None:
            return {"status": "error", "error": f"No clarification draft matches '{checkpoint_id}'."}

        # Decider identity gates *every* path (plan §9.5): only the original
        # requester may answer, cancel, or repair their clarification — enforced
        # here up front so no branch (including repair) can act for someone else.
        # ``store.resolve``/``cancel`` re-check as defence in depth.
        decider = _decider()
        if record.required_decider and decider != record.required_decider:
            return {"status": "error", "error": "Only the original requester can answer this clarification."}

        # A new time on the same draft keeps the sealed goal — do not re-enter
        # schedule_task / full planning, which is how Active target replaced RAG.
        when = args.get("when")
        at = str(args.get("at") or "").strip()
        if (isinstance(when, dict) and str(when.get("raw_expression") or "").strip()) or at:
            payload = record.payload or {}
            return await _admit_and_create(
                {
                    "goal": str(payload.get("goal") or ""),
                    "title": str(payload.get("title") or ""),
                    "when": when if isinstance(when, dict) else None,
                    "at": at,
                    "timezone": str(args.get("timezone") or ""),
                },
                already_clarified=True,
                goal_source="draft",
            )

        if not choice:
            return _needs_input(
                "Pick one of the listed readings, or give a new time in 'when'/'at'."
            )

        low = choice.lower()
        if low in {"cancel"}:
            await store.cancel(checkpoint_id, decider=decider)
            return {"status": "ok", "outcome": "cancelled", "summary": "Cancelled the pending schedule clarification."}
        if low in {"run_now", "now"}:
            await store.cancel(checkpoint_id, decider=decider)
            return _needs_input(
                "Not scheduling anything this time; tell me the goal to run now instead.",
                outcome="run_now",
            )
        if low in {"other_time", "reschedule", "different_time"}:
            # "None of these" — the user wants a time not among the offered
            # readings. Leave the draft open (they can still pick one) and ask for
            # the concrete time; the model reschedules by calling schedule_task
            # with it, which then supersedes this draft.
            return _needs_input(
                "Sure — tell me the day and time you want (for example 'tomorrow 9am' "
                "or 'Aug 5 3pm') and I'll reschedule, keeping the same goal.",
                outcome="other_time",
            )

        is_repair = choice.startswith("repair_next_day:")
        candidate_id = _map_choice_to_candidate(choice, record)
        if not candidate_id:
            # Not a listed reading and not a keyword: treat it as "none of these"
            # rather than a dead end — invite a concrete time (the model reschedules
            # via schedule_task) or a listed pick / cancel.
            return _needs_input(
                "That is not one of the offered readings. Tell me a concrete time "
                "(for example 'tomorrow 9am') and I'll reschedule, pick one of "
                f"{record.candidate_ids}, or reply cancel."
            )
        candidate = record.candidate(candidate_id)

        if is_repair:
            # The chosen reading already elapsed → same wall-clock tomorrow.
            await store.cancel(checkpoint_id, decider=decider)
            return await _create_from_trigger(record, _next_day_trigger(candidate["value"]))

        if candidate.get("validity") == "past":
            # An elapsed reading resolves nothing on its own: keep the draft open
            # and offer a future repair rather than CAS into a dead end (plan §8).
            label = candidate.get("label", candidate_id)
            return {
                "status": "needs_input",
                "resolution_status": "past",
                "message": f"The reading you picked ('{label}') has already passed.",
                "recovery_choices": [
                    {"id": f"repair_next_day:{candidate_id}", "label": "same time tomorrow"},
                    {"id": "run_now", "label": "run it now"},
                    {"id": "cancel", "label": "cancel"},
                ],
                "draft_id": record.id[:8],
            }

        outcome = await store.resolve(checkpoint_id, candidate_id=candidate_id, decider=decider)
        if outcome.status == "forbidden":
            return {"status": "error", "error": "Only the original requester can answer this clarification."}
        if outcome.status == "expired":
            await _emit_action(
                "action.checkpoint.expired", {"checkpoint_id": record.id}, status="expired"
            )
            return _needs_input("This clarification has expired; please set the schedule again.")
        if outcome.status == "conflict":
            return {"status": "error", "error": f"This clarification was already answered differently ({outcome.reason})."}
        if outcome.status == "replayed":
            # Only the CAS winner materialises a schedule; a replay converges on
            # the already-created one (or reports it is still being created),
            # so concurrent/retried answers never create a second schedule.
            fresh = await store.get(checkpoint_id)
            if fresh is not None and fresh.result_id:
                return {
                    "status": "ok",
                    "kind": "once",
                    "schedule_id": fresh.result_id,
                    "already_created": True,
                    "summary": "This schedule was already created.",
                }
            return {"status": "ok", "pending": True, "summary": "This choice is being created into a schedule."}
        if not outcome.ok:  # missing / closed / invalid_candidate
            return {"status": "error", "error": f"Could not confirm this choice ({outcome.status})."}
        # CAS winner: create exactly once, then record where it materialised.
        await _emit_action(
            "action.checkpoint.resolved",
            {"checkpoint_id": record.id, "candidate_id": candidate_id, "phase": "semantic_clarification"},
            status="resolved",
        )
        result = await _create_from_trigger(record, (outcome.candidate or candidate)["value"])
        if result.get("schedule_id"):
            await store.attach_result(checkpoint_id, result_kind="schedule", result_id=result["schedule_id"])
        return result

    async def cancel_schedule(args: dict[str, Any]) -> dict[str, Any]:
        schedule_id = str(args.get("schedule_id", "")).strip()
        if not schedule_id:
            return _needs_input("Provide the schedule id to cancel (see list_schedules).")
        removed = await _scheduler().remove(schedule_id)
        if not removed:
            return {"status": "error", "error": f"No schedule matched '{schedule_id}'."}
        return {"status": "ok", "removed": True, "schedule_id": schedule_id, "summary": f"Cancelled schedule {schedule_id}."}

    # ``schedule_task`` is intentionally *not* declared ``sensitive``: the
    # ScheduleService owns consent (a local request is created directly; an IM
    # request becomes a durable approval proposal). Routing it through the
    # generic approval gate instead would flatly deny the IM case before the
    # handler could persist a resumable proposal.
    return [
        Tool(_SCHEDULE_TASK_SPEC, schedule_task),
        Tool(_RESOLVE_CHECKPOINT_SPEC, resolve_action_checkpoint),
        Tool(_LIST_SCHEDULES_SPEC, list_schedules),
        Tool(_CANCEL_SCHEDULE_SPEC, cancel_schedule),
    ]


__all__ = ["build_schedule_tools", "GOAL_SKILL"]
