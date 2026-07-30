"""``run_compute`` — run a command on the configured compute backend (P1-E).

Local subprocess by default; SSH / Slurm / Modal when configured, degrading to
local when a backend is unavailable. Shares the shell tool's destructive-command
denylist + IM-channel confirmation guard so offloading isn't a safety bypass.
"""

from __future__ import annotations

from typing import Any

from omni.channels.security import channel_requires_sensitive_confirm
from omni.core.react_agent import ToolSpec
from omni.runtime.compute import backend_names
from omni.skills_runtime.builtin_tools.shell import command_is_destructive
from omni.skills_runtime.context import ExecContext, Tool

_VALID_BACKENDS = backend_names()


def build_compute_tools(ctx: ExecContext) -> list[Tool]:
    cfg = ctx.compute_override or getattr(ctx.settings, "compute", None)
    if cfg is None:
        return []

    async def run_compute(args: dict) -> Any:
        command = str(args.get("command", "")).strip()
        if not command:
            return {"status": "error", "error": "command is required"}
        if channel_requires_sensitive_confirm(ctx.settings, ctx.channel):
            return {"status": "error", "error": (
                "compute from IM channels requires local confirmation; run from CLI "
                f"or disable require_sensitive_confirm for channel '{ctx.channel}'."
            )}
        if ctx.settings.security.bash_sandbox != "full" and command_is_destructive(command):
            return {"status": "error", "error": (
                "command blocked by sandbox (destructive/privileged pattern); "
                "set security.bash_sandbox='full' to allow."
            )}
        backend = str(args.get("backend", "")).strip()
        if backend and backend not in _VALID_BACKENDS:
            return {"status": "error", "error": f"unknown backend '{backend}'"}

        from omni.runtime.compute import run_compute as _run
        from omni.skills_runtime.sandbox import SandboxUnavailableError, sandbox_prefix

        requested = backend or cfg.backend or "local"
        cwd = str(args.get("cwd", "") or ctx.working_dir or "")
        job_store = None
        job = None
        if getattr(ctx, "db", None) is not None:
            from omni.runtime.compute_jobs import ComputeJobStore

            job_store = ComputeJobStore(ctx.db)
            job = await job_store.create(
                command=command,
                requested_backend=requested,
                task_id=ctx.task_id,
                session_id=ctx.session_id,
                cwd=cwd,
                profile=str(getattr(ctx, "compute_profile", "") or ""),
            )
            await job_store.mark_running(job.id)
        try:
            exec_prefix = sandbox_prefix(ctx.settings.security, ctx.paths, warn_on_fallback=True)
        except SandboxUnavailableError as exc:
            if job_store is not None and job is not None:
                from omni.runtime.compute import ComputeResult

                await job_store.finish(
                    job.id,
                    ComputeResult(requested, "error", -1, "", command, str(exc)),
                )
            return {"status": "error", "error": f"OS sandbox required but unavailable: {exc}"}
        res = await _run(
            command,
            cfg=cfg,
            cwd=cwd,
            timeout=_float_or_none(args.get("timeout")), backend=backend,
            exec_prefix=exec_prefix,
            cancel_check=(
                (lambda: _cancel_requested(job_store, job.id))
                if job_store is not None and job is not None
                else None
            ),
        )
        if job_store is not None and job is not None:
            await job_store.finish(job.id, res)
        await _record_compute_event(
            ctx, res, requested=requested, job_id=job.id if job is not None else ""
        )
        return {**res.to_dict(), "compute_job_id": job.id if job is not None else ""}

    async def get_compute_job(args: dict) -> Any:
        job_id = str(args.get("job_id", "")).strip()
        if not job_id:
            return {"status": "error", "error": "job_id is required"}
        if getattr(ctx, "db", None) is None:
            return {"status": "error", "error": "compute job store unavailable"}
        from omni.runtime.compute_jobs import ComputeJobStore, compute_job_payload

        job = await ComputeJobStore(ctx.db).get(job_id)
        if job is None:
            return {"status": "error", "error": f"compute job not found: {job_id}"}
        if ctx.session_id and job.session_id and job.session_id != ctx.session_id:
            return {"status": "error", "error": "compute job does not belong to this session"}
        return {"status": "ok", "job": compute_job_payload(job)}

    async def cancel_compute(args: dict) -> Any:
        job_id = str(args.get("job_id", "")).strip()
        if not job_id:
            return {"status": "error", "error": "job_id is required"}
        if getattr(ctx, "db", None) is None:
            return {"status": "error", "error": "compute job store unavailable"}
        from omni.runtime.compute_jobs import ComputeJobStore, compute_job_payload

        jobs = ComputeJobStore(ctx.db)
        job = await jobs.get(job_id)
        if job is None:
            return {"status": "error", "error": f"compute job not found: {job_id}"}
        if ctx.session_id and job.session_id and job.session_id != ctx.session_id:
            return {"status": "error", "error": "compute job does not belong to this session"}
        if job.status not in {"queued", "running", "submitted", "cancel_requested"}:
            return {
                "status": "unchanged",
                "job": compute_job_payload(job),
                "note": "job is already terminal",
            }
        job = await jobs.request_cancel(job.id)
        return {
            "status": "cancel_requested",
            "job": compute_job_payload(job),
            "note": (
                "Cancellation is durable. Submitted backends must acknowledge it; "
                "a synchronous local command can only stop at its process boundary."
            ),
        }

    return [
        Tool(
            ToolSpec("run_compute", (
                "Run a command through a compute backend. Local execution is the default; configured "
                "Docker, SSH, Slurm, or Modal backends may handle long or compute-heavy jobs."
            ), {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to execute"},
                    "backend": {"type": "string", "enum": list(_VALID_BACKENDS),
                                "description": "Optional backend override"},
                    "cwd": {"type": "string", "description": "Optional working directory"},
                    "timeout": {"type": "number", "description": "Optional timeout in seconds"},
                },
                "required": ["command"],
            }),
            run_compute,
            sensitive=True,
        ),
        Tool(
            ToolSpec(
                "get_compute_job",
                "Inspect a managed compute job, including backend, status, result, and external job id.",
                {
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"],
                },
            ),
            get_compute_job,
        ),
        Tool(
            ToolSpec(
                "cancel_compute",
                "Request cancellation of a managed compute job. The backend lifecycle processes the persisted request.",
                {
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"],
                },
            ),
            cancel_compute,
        )
    ]


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value) != "" else None
    except (TypeError, ValueError):
        return None


async def _cancel_requested(job_store: Any, job_id: str) -> bool:
    job = await job_store.get(job_id)
    return bool(job is not None and job.status == "cancel_requested")


async def _record_compute_event(
    ctx: ExecContext,
    res: Any,
    *,
    requested: str,
    job_id: str = "",
) -> None:
    """Record a durable ``compute.run`` event (best-effort, auditable).

    Compute runs are sensitive and easy to lose track of once offloaded, so we
    mirror each into the run ledger — which backend was *requested* vs which
    actually ran (a transparent fallback to local is recorded as such, never a
    silent remote escape). This is also what the eval harness inspects for the
    ``compute`` dimension.
    """
    db = getattr(ctx, "db", None)
    task_id = getattr(ctx, "task_id", "") or ""
    if db is None or not task_id:
        return
    try:
        from omni.runtime.task_recorder import TaskRecorder

        fell_back = res.backend != requested and str(res.detail or "").startswith("fell back")
        recorder = TaskRecorder(db, project=getattr(ctx, "project", "default") or "default")
        await recorder.append_event(
            task_id,
            event_type="compute.run",
            status="succeeded" if res.status in ("ok", "submitted") else "degraded",
            name="compute",
            output_json={
                "backend": res.backend,
                "requested_backend": requested,
                "fell_back": fell_back,
                "status": res.status,
                "returncode": res.returncode,
                "detail": (res.detail or "")[:300],
                "compute_job_id": job_id,
            },
            summary=f"compute {res.backend} status={res.status}"
                    + (f" (fell back from {requested})" if fell_back else ""),
        )
    except Exception:  # noqa: BLE001 — the compute event is best-effort, never fatal.
        pass


__all__ = ["build_compute_tools"]
