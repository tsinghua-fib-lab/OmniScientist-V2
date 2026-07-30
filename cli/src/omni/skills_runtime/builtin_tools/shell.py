"""Shell tool with a two-tier sandbox guard.

Commands run in the tool *working directory* (the folder the CLI was launched
from) with a timeout. ``security.bash_sandbox`` selects the tier:

* ``readonly`` — blocks both the system tier and the workspace-destructive tier
  (delete/rewrite/publish); intended for "just look" runs.
* ``workspace-write`` (default for interactive CLI) — allows destructive ops
  *inside* the working directory (``rm -rf``, ``git reset --hard``, ``git push``)
  but still routes them through the approval gate; system-tier ops stay blocked.
* ``full`` — removes the guard entirely (system tier included).

The system tier (``sudo``, ``mkfs``, ``dd if=``, fork bomb, ``>/dev/sd``,
``shutdown``/``reboot``, recursive ``chown``/``chmod`` on ``/``, ``curl | sh``)
escapes the working directory, so no approval prompt can widen scope to it — it
is hard-blocked unless the sandbox is explicitly ``full``. This is a guard, not
a jail — for a hard sandbox use the OS (containers, seatbelt).
"""

from __future__ import annotations

import asyncio
import json
import re

from omni.channels.security import channel_requires_sensitive_confirm
from omni.core.react_agent import ToolSpec
from omni.core.tool_result import COMMAND_RESULT_SCHEMA, ToolResultEnvelope
from omni.runtime.processes import process_group_options, stop_process_tree
from omni.skills_runtime.context import ExecContext, Tool
from omni.skills_runtime.sandbox import SandboxUnavailableError, sandbox_prefix

_EVENT_OUTPUT_JSON_BUDGET = 7_000
_OBSERVATION_OUTPUT_LIMIT = 100_000

# System / irreversible-beyond-workspace operations. Hard-blocked in every tier
# except ``full`` because their blast radius escapes the working directory, so
# no in-directory approval could make them safe.
_BLOCKED = [
    r"\bsudo\b", r"\bmkfs\b", r"\bdd\s+if=", r":\(\)\s*\{",
    r">\s*/dev/sd", r"\bshutdown\b", r"\breboot\b",
    r"\bchown\s+-R\s+/", r"\bchmod\s+-R\s+777\s+/",
    r"\bcurl\b[^|]*\|\s*(sudo\s+)?(ba)?sh",
]
# Destructive within the working directory (delete/rewrite/publish). Blocked in
# ``readonly``; allowed in ``workspace-write``/``full`` but still routed through
# the approval gate (classified ``destructive`` → the prompt defaults to deny).
_DESTRUCTIVE_WORKSPACE = [
    r"\brm\s+-rf?\b", r"\bgit\s+push\b", r"\bgit\s+reset\s+--hard\b",
]
_BLOCKED_RE = re.compile("|".join(_BLOCKED), re.IGNORECASE)
# Union: used only to classify approval risk (either tier is "destructive").
_DESTRUCTIVE_RE = re.compile("|".join(_BLOCKED + _DESTRUCTIVE_WORKSPACE), re.IGNORECASE)


def _fits_event_output_budget(value: dict) -> bool:
    encoded = json.dumps(value, default=str, ensure_ascii=False)
    return (
        len(encoded) <= _EVENT_OUTPUT_JSON_BUDGET
        and len(encoded.encode("utf-8")) <= _EVENT_OUTPUT_JSON_BUDGET
    )


def _bounded_event_output(
    *,
    command_status: str,
    reason: str,
    exit_code: int | None,
    output: str,
    summary: str,
) -> dict:
    """Build a command result that survives the recorder's whole-value limit.

    The recorder currently replaces a complete JSON value once its serialized
    form exceeds 8,000 Python characters. A raw character slice is insufficient:
    JSON escaping can multiply quotes, control characters, and backslashes, while
    non-ASCII code points can use several UTF-8 bytes. Budget the complete object
    with headroom and find a safe code-point boundary for the merged output.
    """
    output_length = len(output)
    candidate = output[:_EVENT_OUTPUT_JSON_BUDGET]
    result = {
        "result_schema": COMMAND_RESULT_SCHEMA,
        "command_status": command_status,
        "reason": reason,
        "exit_code": exit_code,
        "output": candidate,
        "output_truncated": output_length > len(candidate),
        "summary": summary,
    }
    if not result["output_truncated"] and _fits_event_output_budget(result):
        return result

    result["output_truncated"] = True
    low = 0
    high = len(candidate)
    while low < high:
        midpoint = (low + high + 1) // 2
        result["output"] = candidate[:midpoint]
        if _fits_event_output_budget(result):
            low = midpoint
        else:
            high = midpoint - 1
    result["output"] = candidate[:low]
    return result


def _command_result(
    observation: str,
    *,
    command_status: str,
    reason: str,
    exit_code: int | None,
    output: str,
    summary: str,
) -> ToolResultEnvelope:
    return ToolResultEnvelope(
        observation=observation,
        event_output=_bounded_event_output(
            command_status=command_status,
            reason=reason,
            exit_code=exit_code,
            output=output,
            summary=summary,
        ),
    )


def _controlled_result(
    observation: str,
    *,
    command_status: str,
    reason: str,
    summary: str,
) -> ToolResultEnvelope:
    return _command_result(
        observation,
        command_status=command_status,
        reason=reason,
        exit_code=None,
        output=observation,
        summary=summary,
    )


def command_is_system_blocked(command: str) -> bool:
    """True for system/irreversible ops that escape the working directory.

    Hard-blocked in every sandbox tier except ``full`` — no in-directory
    approval can widen scope this far.
    """
    return bool(_BLOCKED_RE.search(command or ""))


def command_is_destructive(command: str) -> bool:
    """True if ``command`` matches any destructive/privileged pattern.

    Union of the system-block tier and the workspace-destructive tier so the
    approval gate classifies either as ``risk='destructive'`` (the prompt then
    defaults to *deny*), independent of the active sandbox tier.
    """
    return bool(_DESTRUCTIVE_RE.search(command or ""))


def build_shell_tools(ctx: ExecContext) -> list[Tool]:
    working_dir = ctx.working_dir or ctx.paths.project_dir

    async def bash(args: dict) -> ToolResultEnvelope:
        command = str(args.get("command", "")).strip()
        if not command:
            observation = "ERROR: empty command"
            return _controlled_result(
                observation,
                command_status="invalid",
                reason="empty_command",
                summary="Empty command",
            )
        if channel_requires_sensitive_confirm(ctx.settings, ctx.channel):
            observation = (
                "ERROR: shell commands from IM channels require local confirmation. "
                "Run the request from the CLI, or explicitly disable "
                f"require_sensitive_confirm for channel '{ctx.channel}'."
            )
            return _controlled_result(
                observation,
                command_status="blocked",
                reason="channel_confirmation_required",
                summary="Shell command requires local confirmation",
            )
        mode = ctx.settings.security.bash_sandbox
        if mode != "full":
            if command_is_system_blocked(command):
                observation = (
                    "ERROR: command blocked by sandbox (system/privileged pattern that "
                    "escapes the working directory). "
                    f"Current bash_sandbox='{mode}'. Set security.bash_sandbox='full' to allow."
                )
                return _controlled_result(
                    observation,
                    command_status="blocked",
                    reason="sandbox_blocked",
                    summary="Command blocked by sandbox",
                )
            if mode == "readonly" and command_is_destructive(command):
                observation = (
                    "ERROR: command blocked by sandbox (destructive pattern) in read-only "
                    f"mode. Current bash_sandbox='{mode}'. Set security.bash_sandbox="
                    "'workspace-write' to allow destructive commands inside the working "
                    "directory (still subject to approval)."
                )
                return _controlled_result(
                    observation,
                    command_status="blocked",
                    reason="sandbox_blocked",
                    summary="Command blocked by sandbox",
                )
        timeout = float(args.get("timeout", 60) or 60)
        try:
            prefix = sandbox_prefix(ctx.settings.security, ctx.paths, warn_on_fallback=True)
        except SandboxUnavailableError as exc:
            observation = f"ERROR: OS sandbox required but unavailable: {exc}"
            return _controlled_result(
                observation,
                command_status="blocked",
                reason="sandbox_unavailable",
                summary="OS sandbox required but unavailable",
            )
        if prefix:
            proc = await asyncio.create_subprocess_exec(
                *prefix, "/bin/sh", "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(working_dir),
                **process_group_options(),
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(working_dir),
                **process_group_options(),
            )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            await stop_process_tree(proc, grace_seconds=0.1)
            observation = f"ERROR: command timed out after {timeout}s"
            return _controlled_result(
                observation,
                command_status="timed_out",
                reason="timeout",
                summary=f"Command timed out after {timeout}s",
            )
        except asyncio.CancelledError:
            await stop_process_tree(proc)
            raise
        text = (out or b"").decode("utf-8", errors="replace")
        exit_code = int(proc.returncode or 0)
        observation = f"[exit={proc.returncode}]\n{text[:_OBSERVATION_OUTPUT_LIMIT]}"
        succeeded = exit_code == 0
        return _command_result(
            observation,
            command_status="succeeded" if succeeded else "failed",
            reason="ok" if succeeded else "nonzero_exit",
            exit_code=exit_code,
            output=text,
            summary=(
                "Command completed successfully"
                if succeeded
                else f"Command exited with code {exit_code}"
            ),
        )

    return [
        Tool(
            ToolSpec("bash", "Run a shell command in the working directory, subject to sandbox policy.", {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number", "description": "Seconds; default 60"},
                },
                "required": ["command"],
            }),
            bash,
        )
    ]
