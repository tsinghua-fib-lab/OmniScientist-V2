"""Read-only recall tools — explicit ``search → get`` retrieval.

These let the agent pull *precise* prior context back into a turn instead of
relying on a fuzzy auto-injected blob: search long-term memory, open a produced
artifact by URI, or look up a past task by id. This is the same pattern Claude
Code / Codex / OpenClaw converge on (the model decides what to retrieve).

All tools are gated on ``ctx.db`` so DB-free callers (some unit tests) skip them.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from omni.core.react_agent import ToolSpec
from omni.core.timefmt import format_local_iso
from omni.core.tool_result import recall_result_outcome
from omni.memory.sanitize import redact_secrets
from omni.memory.service import (
    MemoryLayer,
    MemoryService,
    _principal_visible,
    open_global_store,
)
from omni.skills_runtime.context import ExecContext, Tool
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database
from omni.storage.models import ArtifactORM, SubtaskORM, TaskORM

_GROUNDING_KEYS = (("source_id", "source"), ("claim_id", "claim"), ("task_id", "run"))

_TEXT_EXT = {
    ".md", ".txt", ".json", ".csv", ".tsv", ".dot", ".svg", ".tex", ".bib",
    ".yaml", ".yml", ".py", ".html", ".xml", ".log", ".rst", ".toml", ".ini",
}
_MAX_OPEN_BYTES = 100_000


def _search_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _bigrams(value: str) -> set[str]:
    return {value[index:index + 2] for index in range(max(0, len(value) - 1))}


def _task_score(query: str, run: TaskORM) -> float:
    query_key = _search_key(query)
    if not query_key:
        return 0.0
    query_grams = _bigrams(query_key)
    best = 0.0
    for value, weight in (
        (run.title, 1.0),
        (run.user_input, 0.9),
        (run.summary, 0.7),
    ):
        candidate = _search_key(value)
        if not candidate:
            continue
        if candidate in query_key or query_key in candidate:
            best = max(best, weight)
            continue
        grams = _bigrams(candidate)
        if grams:
            best = max(best, weight * len(query_grams & grams) / len(grams))
    return best


def _typed_many(namespace: str, values: Any) -> list[str]:
    return [f"{namespace}:{value}" for value in values or [] if value]


def _task_payload(
    run: TaskORM, *, score: float | None = None, workspace: str | None = None
) -> dict[str, Any]:
    # A finished task's summary and its failure reason are different facts: a
    # failed task has no real summary, and folding ``run.error`` into ``summary``
    # is what let a prior attempt's error text be re-consumed downstream as if it
    # were a valid result. Keep them separate — ``summary`` is the real output
    # digest (empty for a failure), ``failure_reason`` carries the error.
    payload: dict[str, Any] = {
        "ref": f"task:{run.id}",
        "task_id": run.id,
        "task_status": run.status,
        # Compatibility alias for existing clients.  Invocation lifecycle is
        # carried out-of-band by ToolCallOutcome, never inferred from this key.
        "status": run.status,
        "title": (run.title or run.user_input or "")[:512],
        "user_input": (run.user_input or "")[:1200],
        "summary": (run.summary or "")[:1600],
        "failure_reason": (run.error or "")[:1600],
        "subtask_refs": _typed_many("subtask", run.submitted_subtask_ids),
        "artifact_refs": _typed_many("artifact", run.artifact_ids),
        "source_refs": _typed_many("source", run.source_ids),
        "claim_refs": _typed_many("claim", run.claim_ids),
        "evidence_refs": _typed_many("evidence", run.evidence_ids),
        # Local ISO with offset (e.g. 2026-07-24T15:13:24+08:00) so the model
        # renders the operator's wall-clock time, not a bare UTC value 8h behind.
        "created_at": format_local_iso(run.created_at),
    }
    if score is not None:
        payload["match_score"] = round(score, 4)
    if workspace:
        payload["workspace"] = workspace
    return payload


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a possibly-naive timestamp to an aware UTC one for comparison."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _since_from_days(days: Any) -> datetime | None:
    """Translate a ``days`` argument into an aware UTC lower bound, or ``None``."""
    if days is None:
        return None
    try:
        span = max(0, int(days))
    except (TypeError, ValueError):
        return None
    return datetime.now(UTC) - timedelta(days=span) if span > 0 else None


def _agg_payload(row: Any) -> dict[str, Any]:
    """Shallow payload for a cross-workspace list/search hit (index-backed row).

    The global index/aggregate row is deliberately thin (no user_input/summary),
    so this carries identity, status, title, and the owning ``workspace`` label.
    The model deepens any single hit with ``get_task``, which now routes across
    workspaces on its own.
    """
    return {
        "ref": f"task:{row.id}",
        "task_id": row.id,
        "task_status": row.status,
        "status": row.status,
        "title": (row.title or "")[:120],
        "channel": getattr(row, "channel", "") or "",
        "session_id": getattr(row, "session_id", "") or "",
        "created_at": format_local_iso(row.created_at),
        "workspace": row.workspace,
    }


def _agg_score(query: str, row: Any) -> float:
    """Title-only fuzzy score for a cross-workspace row (agg rows lack body text)."""
    query_key = _search_key(query)
    if not query_key:
        return 0.0
    title_key = _search_key(getattr(row, "title", "") or "")
    if not title_key:
        return 0.0
    if title_key in query_key or query_key in title_key:
        return 1.0
    grams = _bigrams(title_key)
    return len(_bigrams(query_key) & grams) / len(grams) if grams else 0.0


async def _resolve_foreign_task(
    ctx: ExecContext, ident: str
) -> tuple[TaskORM, Any] | None:
    """Route a task id to the workspace that owns it (index-first, self-healing).

    Mirrors ``omni task show <id>``: ``resolve_task_workspace`` finds the owning
    workspace via the global task index (with reconcile + scan fallback), then we
    read the ``TaskORM`` straight from that workspace's own store. Returns
    ``(task, owner_settings)`` or ``None`` when the id is unknown everywhere.
    """
    from omni.runtime.task_index import resolve_task_workspace

    owner = await resolve_task_workspace(ctx.settings, ident)
    if owner is None:
        return None
    try:
        db = get_database(owner.paths.project_db)
        await db.init()
        async with db.session() as session:
            task = await session.get(TaskORM, ident)
            if task is None:
                rows = list(
                    (
                        await session.execute(
                            select(TaskORM)
                            .where(TaskORM.id.startswith(ident, autoescape=True))
                            .limit(2)
                        )
                    ).scalars().all()
                )
                task = rows[0] if len(rows) == 1 else None
    except Exception:  # noqa: BLE001 - a bad foreign store must fail closed, not crash
        return None
    return (task, owner) if task is not None else None


async def _all_workspace_tasks(
    ctx: ExecContext, *, status: str | None, since: datetime | None, limit: int
) -> list[Any]:
    """Recent tasks across every registered workspace (newest-first, index-synced).

    Reuses the same catalog aggregate that powers ``omni task --all`` / ``/task
    all``, so the agent sees exactly what that command sees — and the read also
    keeps the global index in sync as a side effect.
    """
    from omni.runtime.aggregate import list_tasks_all_workspaces

    rows = await list_tasks_all_workspaces(
        home=ctx.paths.home, status=status, limit_per=max(limit, 50)
    )
    if since is not None:
        rows = [
            row
            for row in rows
            if (created := _as_utc(row.created_at)) is not None and created >= since
        ]
    return rows[:limit]


def _looks_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    sample = data[:4096]
    if not sample:
        return False
    nontext = sum(1 for b in sample if b < 9 or (13 < b < 32))
    return nontext / len(sample) > 0.30


def _binary_artifact_result(ctx: ExecContext, ref: str, path: Path, size: int) -> dict[str, Any]:
    """Content-type-aware result for a binary artifact — never invites shell/pip.

    Dynamically resolves a *declared* reader capability for this content type
    (registry ``find_reader`` over SKILL.md ``runtime_requirements.reads``):

    * reader declared but its Python deps are missing → surface the one-shot
      ``dependency_setup_command`` (detect → prompt the existing install flow);
    * reader declared and ready → name the capability to use;
    * no reader declared → report honestly and tell the model NOT to shell out.

    This is the generic, non-enumerated answer to "read an artifact I can't
    natively open": a new readable format is added by *declaring* it on a skill,
    never by editing this host code (which is why ``df16e466`` — a deck read that
    fell into ``bash``+``pip`` across the wrong interpreter — cannot recur).
    """
    import mimetypes

    from omni.skills_runtime.manifest import missing_python_modules

    suffix = path.suffix.lower()
    mime = (mimetypes.guess_type(path.name)[0] or "").lower()
    result: dict[str, Any] = {
        "uri": ref,
        "path": str(path),
        "size": size,
        "binary": True,
        "content_type": mime or (suffix.lstrip(".") or "unknown"),
    }
    registry = getattr(ctx, "registry", None)
    reader = registry.find_reader(suffix, mime) if registry is not None else None
    if reader is None:
        result["note"] = (
            f"Binary {suffix or 'artifact'}: no reader capability is registered for this "
            "type. Reference it by path/uri and use its producer's structured output; do "
            "NOT parse it with shell or install packages ad hoc."
        )
        return result
    missing = missing_python_modules(reader)
    command = str(reader.dependency_setup_command or "").strip()
    if missing:
        joined = ", ".join(missing)
        result["reader"] = reader.name
        result["action_required"] = {
            "kind": "install",
            "python_modules": missing,
            "command": command,
            "reader": reader.name,
        }
        result["setup_command"] = command
        result["error_info"] = {
            "code": reader.dependency_error_code or "runtime_dependency_missing",
            "category": "configuration",
            "retryable": False,
        }
        result["note"] = (
            f"The '{reader.name}' capability reads {suffix} but needs Python module(s) "
            f"first: {joined}." + (f" Run: {command}." if command else "")
            + " Do NOT install packages ad hoc inside the loop."
        )
        return result
    result["reader"] = reader.name
    result["note"] = (
        f"This {suffix} artifact is read by the '{reader.name}' capability — route to it "
        "(e.g. via the skill / run_skill path), NOT shell or pip."
    )
    return result


def build_recall_tools(ctx: ExecContext) -> list[Tool]:
    # Search/get span the workspace store *and* the machine-global store so the
    # model can pull back a preference the owner set in another project/channel.
    memory = MemoryService(
        ctx.db, ctx.settings, llm=ctx.llm, global_db=open_global_store(ctx.settings)
    )
    store = ctx.artifacts or ArtifactStore(ctx.paths, ctx.db)

    async def memory_search(args: dict) -> Any:
        query = str(args.get("query", "")).strip()
        limit = max(1, min(20, int(args.get("limit", 6) or 6)))
        # Bounded candidate set (recall_scoped) rather than a full-table scan:
        # keeps the tool fast and hard-caps how many rows are read per call.
        res = await memory.recall_scoped(
            query, session_id=ctx.session_id, cross_session=True, limit=limit,
            candidate_limit=max(100, limit * 20), principal=ctx.principal,
        )
        return {
            "matches": [
                {
                    "id": sm.entry.id,
                    "layer": sm.entry.layer,
                    "type": sm.entry.memory_type,
                    "summary": sm.entry.summary[:300],
                    "ref": sm.entry.payload_ref,
                    "score": round(sm.score, 3),
                }
                for sm in res
            ]
        }

    async def memory_get(args: dict) -> Any:
        mid = str(args.get("id", "")).strip()
        if not mid:
            return {"error": "id required"}
        # Cross-store id/prefix resolution (workspace + global).
        row = await memory.get(mid)
        if row is None or not _principal_visible(row.principal, ctx.principal):
            return {"error": f"memory {mid} not found"}
        return {
            "id": row.id, "layer": row.layer, "type": row.memory_type,
            "summary": row.summary, "payload_ref": row.payload_ref, "tags": row.tags,
            "importance": row.importance, "pinned": bool(row.pinned),
        }

    async def list_session_artifacts(args: dict) -> Any:
        limit = max(1, min(50, int(args.get("limit", 20) or 20)))
        rows = await store.list_by_session(ctx.session_id, limit=limit)
        scope = "session"
        if not rows:
            rows = await store.list_recent(limit=limit)
            scope = "workspace"
        return {
            "scope": scope,
            "artifacts": [
                {
                    "uri": r.uri,
                    "title": r.title or r.kind,
                    "kind": r.kind,
                    "subtask_id": r.subtask_id,
                    "path": str(ctx.paths.project_dir / r.rel_path) if r.rel_path else "",
                }
                for r in rows
            ],
        }

    async def get_subtask(args: dict) -> Any:
        from omni.runtime.subtask_runtime import _collect_artifacts, _result_summary

        subtask_id = str(args.get("subtask_id", "") or args.get("id", "")).strip()
        if not subtask_id:
            return {"error": "subtask_id required"}
        async with ctx.db.session() as s:
            task = (
                await s.execute(select(SubtaskORM).where(SubtaskORM.id == subtask_id))
            ).scalar_one_or_none()
            if task is None:
                rows = (
                    await s.execute(
                        select(SubtaskORM).order_by(SubtaskORM.created_at.desc()).limit(500)
                    )
                ).scalars().all()
                cand = [t for t in rows if t.id.startswith(subtask_id)]
                task = cand[0] if len(cand) == 1 else None
        if task is None:
            return {"error": f"task {subtask_id} not found in this workspace",
                    "hint": "try /task all, or run omni in the project where it was created"}
        artifacts = _collect_artifacts(task.result_json)
        return {
            "subtask_id": task.id,
            "skill": task.skill_name,
            "subtask_status": task.status,
            # Compatibility alias; invocation lifecycle is carried by the
            # host-owned ToolCallOutcome attached to Recall tools.
            "status": task.status,
            "session_id": task.session_id,
            "summary": _result_summary(task.result_json) if task.result_json else (task.error or ""),
            "failure_reason": task.error or "",
            "artifacts": [{"label": a["label"], "uri": a["uri"], "path": a["path"]} for a in artifacts],
        }

    async def remember(args: dict) -> Any:
        text = redact_secrets(str(args.get("text", "")).strip())
        if len(text) < 4:
            return {"error": "text required"}
        mtype = str(args.get("type", "finding")).strip() or "finding"
        pin = bool(args.get("pin", False))
        ref = ""
        for key, scheme in _GROUNDING_KEYS:
            val = str(args.get(key, "")).strip()
            if val:
                ref = f"{scheme}://{val}"
                break
        mid = await memory.record(
            layer=MemoryLayer.SEMANTIC, scope="project",
            summary=text, memory_type=mtype,
            importance=0.9 if pin else 0.7, pinned=pin, payload_ref=ref,
            principal=ctx.principal,
        )
        return {"id": mid, "grounded": bool(ref), "ref": ref,
                "note": "Memory is ungrounded without source_id, claim_id, or task_id; omni verify will flag it." if not ref else ""}

    async def list_recent_tasks(args: dict) -> Any:
        limit = max(1, min(30, int(args.get("limit", 8) or 8)))
        status = str(args.get("status", "") or "").strip() or None
        scope = str(args.get("scope", "") or "workspace").strip().lower()
        since = _since_from_days(args.get("days"))
        if scope == "all":
            rows = await _all_workspace_tasks(
                ctx, status=status, since=since, limit=limit
            )
            return {
                "scope": "all",
                "tasks": [_agg_payload(row) for row in rows if row.id != ctx.task_id],
            }
        async with ctx.db.session() as s:
            q = (
                select(TaskORM)
                .where(
                    TaskORM.project == ctx.project,
                    TaskORM.archived_at.is_(None),
                )
                .order_by(TaskORM.created_at.desc())
                # A time window may skip many recent rows, so widen the scan when
                # ``days`` is set and slice back to ``limit`` after filtering.
                .limit(limit if since is None else 500)
            )
            if status:
                q = q.where(TaskORM.status == status)
            if since is not None:
                q = q.where(TaskORM.created_at >= since)
            rows = list((await s.execute(q)).scalars().all())
        rows = [row for row in rows if row.id != ctx.task_id][:limit]
        return {
            "scope": "workspace",
            "tasks": [
                {
                    **_task_payload(row, workspace=ctx.project),
                    "title": (row.title or row.user_input or "")[:80],
                    "user_input": (row.user_input or "")[:160],
                    "summary": (row.summary or "")[:160],
                    "failure_reason": (row.error or "")[:160],
                }
                for row in rows
            ],
        }

    async def search_tasks(args: dict) -> Any:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "query required"}
        limit = max(1, min(20, int(args.get("limit", 8) or 8)))
        status = str(args.get("status") or "").strip()
        scope = str(args.get("scope", "") or "workspace").strip().lower()
        if scope == "all":
            rows = await _all_workspace_tasks(
                ctx, status=status or None, since=None, limit=500
            )
            ranked_all = sorted(
                (
                    (_agg_score(query, row), row)
                    for row in rows
                    if row.id != ctx.task_id
                ),
                key=lambda pair: (
                    pair[0],
                    pair[1].created_at.isoformat() if pair[1].created_at else "",
                ),
                reverse=True,
            )
            return {
                "scope": "all",
                "matches": [
                    {**_agg_payload(row), "match_score": round(score, 4)}
                    for score, row in ranked_all[:limit]
                    if score >= 0.2
                ],
            }
        async with ctx.db.session() as session:
            stmt = (
                select(TaskORM)
                .where(
                    TaskORM.project == ctx.project,
                    TaskORM.archived_at.is_(None),
                )
                .order_by(TaskORM.created_at.desc())
                .limit(500)
            )
            if status:
                stmt = stmt.where(TaskORM.status == status)
            rows = list((await session.execute(stmt)).scalars().all())
        ranked = sorted(
            (
                (_task_score(query, row), row)
                for row in rows
                if row.id != ctx.task_id
            ),
            key=lambda pair: (pair[0], pair[1].created_at),
            reverse=True,
        )
        return {
            "scope": "workspace",
            "matches": [
                _task_payload(row, score=score, workspace=ctx.project)
                for score, row in ranked[:limit]
                if score >= 0.2
            ],
        }

    async def _detail(
        run: TaskORM,
        db: Any,
        task_store: ArtifactStore,
        *,
        workspace: str = "",
    ) -> dict[str, Any]:
        payload = _task_payload(run, workspace=workspace or None)
        artifact_ids = [str(value) for value in run.artifact_ids or [] if value]
        if not artifact_ids:
            payload["artifacts"] = []
            return payload
        async with db.session() as artifact_session:
            rows = list(
                (
                    await artifact_session.execute(
                        select(ArtifactORM).where(ArtifactORM.id.in_(artifact_ids))
                    )
                ).scalars().all()
            )
        by_id = {row.id: row for row in rows}
        artifacts: list[dict[str, Any]] = []
        for artifact_id in artifact_ids:
            row = by_id.get(artifact_id)
            if row is None:
                continue
            path = await task_store.resolve_path(row.uri or f"artifact://{row.id}")
            artifacts.append(
                {
                    "title": row.title or row.kind or "artifact",
                    "kind": row.kind or "file",
                    "uri": row.uri or f"artifact://{row.id}",
                    "path": str(path) if path is not None else "",
                }
            )
        payload["artifacts"] = artifacts
        return payload

    async def get_task(args: dict) -> Any:
        raw = str(args.get("task_id") or args.get("id") or args.get("ref") or "").strip()
        task_id = raw.removeprefix("task:")
        if not task_id:
            return {"error": "task id required"}
        async with ctx.db.session() as session:
            exact = await session.get(TaskORM, task_id)
            if exact is not None and exact.project == ctx.project:
                return await _detail(exact, ctx.db, store, workspace=ctx.project)
            matches = list(
                (
                    await session.execute(
                        select(TaskORM)
                        .where(
                            TaskORM.project == ctx.project,
                            TaskORM.id.startswith(task_id, autoescape=True),
                        )
                        .limit(2)
                    )
                ).scalars().all()
            )
        if len(matches) == 1:
            return await _detail(matches[0], ctx.db, store, workspace=ctx.project)
        if len(matches) > 1:
            return {"error": "task id prefix is ambiguous", "task_id": task_id}
        # Cross-workspace fallback: an id absent from this workspace is routed to
        # its owning workspace via the global task index (the same path
        # ``omni task show`` uses), so an agent in workspace A can still read a
        # task created in workspace B instead of dead-ending on "not found".
        foreign = await _resolve_foreign_task(ctx, task_id)
        if foreign is not None:
            run, owner = foreign
            foreign_db = get_database(owner.paths.project_db)
            await foreign_db.init()
            foreign_store = ArtifactStore(owner.paths, foreign_db)
            return await _detail(
                run, foreign_db, foreign_store, workspace=owner.paths.project_name
            )
        return {
            "error": f"task {task_id} not found in this workspace or any registered workspace"
        }

    async def get_run(args: dict) -> Any:
        from omni.research.store import ResearchStore

        rid = str(args.get("run_id") or args.get("id") or "").strip()
        if not rid:
            return {"error": "run_id required"}
        if rid.startswith("task:"):
            return {
                "error": "wrong_id_type",
                "expected": "experiment_run:<id>",
                "received": rid,
                "hint": "use get_task for a user-request task",
            }
        rid = rid.removeprefix("experiment_run:")
        async with ctx.db.session() as session:
            matches = list(
                (
                    await session.execute(
                        select(TaskORM)
                        .where(
                            TaskORM.project == ctx.project,
                            TaskORM.id.startswith(rid, autoescape=True),
                        )
                        .limit(2)
                    )
                ).scalars().all()
            )
        if matches:
            return {
                "error": "wrong_id_type",
                "expected": "experiment_run:<id>",
                "received": f"task:{rid}",
                "hint": "use get_task for a user-request task",
            }
        rstore = ResearchStore(ctx.db)
        run = await rstore.get_run(rid)
        if run is None:
            return {"error": f"run {rid} not found"}
        claims = await rstore.list_claims(limit=200)
        siblings = [
            c for c in claims if run.hypothesis_id and c.hypothesis_id == run.hypothesis_id
        ]
        return {
            "ref": f"experiment_run:{run.id}",
            "task_id": run.id, "title": run.title, "cmd": run.cmd, "seed": run.seed,
            "env_lock": (run.env_lock or "")[:400], "metrics": run.metrics,
            "output_uris": run.output_uris, "subtask_id": run.subtask_id,
            "hypothesis_id": run.hypothesis_id,
            "claims": [{"id": c.id, "text": c.text[:160]} for c in siblings[:20]],
        }

    async def open_artifact(args: dict) -> Any:
        ref = str(args.get("uri") or args.get("ref") or args.get("path") or "").strip()
        if not ref:
            return {"error": "uri/path required"}
        if ref.startswith("artifact://"):
            # Trusted store artifact: resolved under the managed artifacts dir.
            path = await store.resolve_path(ref)
            if path is None:
                return {"error": f"artifact not found: {ref}"}
        else:
            # Raw filesystem path (``file://`` or plain/``~``). Re-admit it against
            # the read roots + resolved-sensitivity gate so a crafted ``uri`` /
            # ``path`` cannot turn open_artifact into a read-any-file primitive
            # (e.g. ``~/.ssh/id_rsa`` or a benign-named symlink pointing at it).
            from omni.skills_runtime.builtin_tools import fs

            raw = ref[len("file://"):] if ref.startswith("file://") else ref
            from omni.core.path_lookup import resolve_existing_path

            p = resolve_existing_path(raw) or Path(raw).expanduser()
            if not p.is_file():
                return {"error": f"artifact not found: {ref}"}
            if not fs.within_roots(p, fs.read_roots(ctx)):
                return {"error": f"path is outside the accessible roots: {ref}"}
            if fs.is_sensitive_target(p):
                return {"error": f"sensitive file blocked by security policy: {p.name}"}
            path = p
        size = path.stat().st_size
        data = path.read_bytes()[: _MAX_OPEN_BYTES + 1]
        if path.suffix.lower() not in _TEXT_EXT and _looks_binary(data):
            return _binary_artifact_result(ctx, ref, path, size)
        text = data[:_MAX_OPEN_BYTES].decode("utf-8", "replace")
        from omni.core.injection import defend_observation

        text, _hits = defend_observation(
            text, mode=getattr(ctx.settings.security, "injection_defense", "flag")
        )
        return {"uri": ref, "path": str(path), "size": size,
                "truncated": size > _MAX_OPEN_BYTES, "content": text}

    tools = [
        Tool(
            ToolSpec(
                "memory_search",
                "Search long-term and current-session memory. Returns candidate ids; use memory_get for details.",
                {"type": "object", "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                }, "required": ["query"]},
            ),
            memory_search,
        ),
        Tool(
            ToolSpec(
                "memory_get",
                "Retrieve a memory entry and its provenance by id or unique id prefix.",
                {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            ),
            memory_get,
        ),
        Tool(
            ToolSpec(
                "list_session_artifacts",
                "List artifacts from the current session, falling back to recent workspace artifacts.",
                {"type": "object", "properties": {"limit": {"type": "integer"}}},
            ),
            list_session_artifacts,
        ),
        Tool(
            ToolSpec(
                "get_subtask",
                "Inspect a background subtask (skill execution) by id or unique prefix, including status, summary, and artifacts.",
                {"type": "object", "properties": {"subtask_id": {"type": "string"}}, "required": ["subtask_id"]},
            ),
            get_subtask,
        ),
        Tool(
            ToolSpec(
                "list_recent_tasks",
                "List recent user-request tasks, optionally filtered by status. Default "
                "scope is the current workspace; pass scope='all' for cross-project "
                "questions ('everything I worked on', 'the last N days') and days=N to "
                "bound the time window. Use get_subtask for child subtasks.",
                {"type": "object", "properties": {
                    "limit": {"type": "integer"},
                    "status": {"type": "string"},
                    "scope": {"type": "string", "enum": ["workspace", "all"]},
                    "days": {"type": "integer"},
                }},
            ),
            list_recent_tasks,
        ),
        Tool(
            ToolSpec(
                "search_tasks",
                "Search historical user requests by title or description and return typed "
                "run, task, artifact, and source references. Default scope is the current "
                "workspace; pass scope='all' to search across every workspace.",
                {"type": "object", "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                    "status": {"type": "string"},
                    "scope": {"type": "string", "enum": ["workspace", "all"]},
                }, "required": ["query"]},
            ),
            search_tasks,
        ),
        Tool(
            ToolSpec(
                "get_task",
                "Read a user request by task:<id> or unique prefix, including status, "
                "summary, failure reason, and typed object references. Resolves the id "
                "across all workspaces, so a task created in another project is still "
                "found.",
                {"type": "object", "properties": {
                    "task_id": {"type": "string"},
                }, "required": ["task_id"]},
            ),
            get_task,
        ),
        Tool(
            ToolSpec(
                "open_artifact",
                "Open an artifact by artifact:// URI or local path. Text is returned "
                "inline; a binary artifact is routed to a declared reader capability "
                "(or reports how to enable one) — never read binaries with shell/pip.",
                {"type": "object", "properties": {
                    "uri": {"type": "string"},
                    "path": {"type": "string"},
                }},
            ),
            open_artifact,
        ),
        Tool(
            ToolSpec(
                "remember",
                "Store a durable finding or preference. Ground it with source_id, claim_id, or task_id when possible; pin=true forces recall.",
                {"type": "object", "properties": {
                    "text": {"type": "string"},
                    "type": {"type": "string",
                             "enum": ["finding", "preference", "decision", "dead_end",
                                      "negative_result", "idea_evolution"]},
                    "source_id": {"type": "string"},
                    "claim_id": {"type": "string"},
                    "task_id": {"type": "string"},
                    "pin": {"type": "boolean"},
                }, "required": ["text"]},
            ),
            remember,
        ),
        Tool(
            ToolSpec(
                "get_run",
                "Inspect reproducibility data for experiment_run:<id>, including command, seed, environment lock, metrics, artifacts, and related claims.",
                {"type": "object", "properties": {"run_id": {"type": "string"}},
                 "required": ["run_id"]},
            ),
            get_run,
        ),
    ]
    for tool in tools:
        tool.outcome_resolver = recall_result_outcome
    return tools
