"""Explicit subagent execution isolation (none, git worktree, container)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from omni.runtime.processes import process_group_options, stop_process_tree
from omni.skills_runtime.context import ExecContext


class IsolationError(RuntimeError):
    """The requested isolation boundary could not be established."""


async def prepare_subagent_context(
    ctx: ExecContext,
    *,
    mode: str,
    compute_profile: str = "",
) -> ExecContext:
    """Return a context with an explicit filesystem/compute boundary.

    Worktrees are intentionally retained after the specialist completes so its
    files stay inspectable and recoverable. A later lifecycle/prune command can
    remove them deliberately; silent cleanup would discard research state.
    """
    selected = (mode or "none").strip().lower()
    if selected not in {"none", "worktree", "container"}:
        raise IsolationError(f"unknown isolation mode: {selected}")
    working_dir = ctx.working_dir or ctx.paths.workspace_root or Path.cwd()
    compute = _compute_profile(ctx, compute_profile)

    if selected == "worktree":
        working_dir = await _create_worktree(ctx)
    elif selected == "container":
        compute = (compute or ctx.settings.compute).model_copy(deep=True)
        compute.backend = "docker"
        compute.fallback_local = False
        if not compute.docker_image:
            raise IsolationError(
                "container isolation requires compute profile docker_image"
            )

    child = replace(ctx, working_dir=working_dir, compute_override=compute)
    await _record_isolation(child, selected, compute_profile)
    return child


def _compute_profile(ctx: ExecContext, name: str) -> Any:
    if not name:
        return ctx.compute_override
    profile = ctx.settings.compute_profiles.get(name)
    if profile is None:
        raise IsolationError(f"unknown compute profile: {name}")
    return profile.model_copy(deep=True)


async def _create_worktree(ctx: ExecContext) -> Path:
    root = ctx.paths.workspace_root
    if root is None or not (root / ".git").exists():
        raise IsolationError("worktree isolation requires a git workspace")
    base = ctx.paths.project_dir / "worktrees"
    base.mkdir(parents=True, exist_ok=True)
    dest = base / f"subagent-{uuid.uuid4().hex[:10]}"
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(root),
        "worktree",
        "add",
        "--detach",
        str(dest),
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **process_group_options(),
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except TimeoutError:
        await stop_process_tree(proc, grace_seconds=0.1)
        raise IsolationError("git worktree creation timed out") from None
    except asyncio.CancelledError:
        await stop_process_tree(proc)
        raise
    if proc.returncode != 0:
        detail = output.decode("utf-8", errors="replace").strip()
        raise IsolationError(f"git worktree creation failed: {detail[:500]}")
    return dest


async def _record_isolation(ctx: ExecContext, mode: str, profile: str) -> None:
    if ctx.db is None or not ctx.task_id:
        return
    try:
        from omni.runtime.task_recorder import TaskRecorder

        await TaskRecorder(ctx.db, project=ctx.project).append_event(
            ctx.task_id.split("::sub-", 1)[0],
            event_type="subagent.isolation",
            status="succeeded",
            name=mode,
            output_json={
                "subagent_task_id": ctx.task_id,
                "mode": mode,
                "working_dir": str(ctx.working_dir or ""),
                "compute_profile": profile,
                "compute_backend": str(getattr(ctx.compute_override, "backend", "") or ""),
            },
            summary=f"subagent isolation={mode}",
        )
    except Exception:  # noqa: BLE001 - isolation itself is established; audit is best-effort
        return


__all__ = ["IsolationError", "prepare_subagent_context"]
