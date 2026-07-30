"""Host-owned ROM writes that must not wait for the model to call a tool.

Codex records every exec in the session transcript without a separate
``log_run`` step. Omni keeps Source / Claim / Evidence model- or skill-owned —
inventing them from prose would fail ``omni verify``. Experiment runs are
facts the host already observed at the tool gateway, so generic ReAct and
skill bash share one recording boundary.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_EXEC_TOOLS = frozenset({"bash", "run_compute"})
_RECORDED_STATUSES = frozenset({"succeeded", "failed", "timed_out"})


async def record_observed_exec(
    db: Any,
    *,
    tool_name: str,
    command: str,
    status: str,
    session_id: str = "",
    subtask_id: str = "",
    output_uris: list[str] | None = None,
) -> Any:
    """Append one ``experiment_runs`` row for a command the host actually ran.

    Known-safe reporting commands (``git status``, ``pwd``) stay off the ledger.
    Failures here never raise: the command already finished.
    """
    if db is None or tool_name not in _EXEC_TOOLS:
        return None
    command = str(command or "").strip()
    if not command or status not in _RECORDED_STATUSES:
        return None
    if tool_name == "bash":
        from omni.skills_runtime.builtin_tools.shell import command_is_known_safe

        if command_is_known_safe(command):
            return None
    try:
        from omni.research.store import ResearchStore
        from omni.research.tools import capture_env_lock

        return await ResearchStore(db).add_run(
            title=f"{tool_name}: {command[:80]}",
            session_id=session_id,
            subtask_id=subtask_id,
            cmd=command,
            env_lock=capture_env_lock(),
            output_uris=list(output_uris or []),
            inputs={"origin": "host", "tool": tool_name},
            status="succeeded" if status == "succeeded" else "failed",
        )
    except Exception:  # noqa: BLE001 - inventory must not fail a finished exec
        logger.debug("host_record.exec_failed tool=%s", tool_name, exc_info=True)
        return None
