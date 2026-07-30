"""Trusted, bounded lifecycle hooks for the agent harness.

Hooks are deliberately an owner-controlled extension point. They never use a
shell, receive only redacted JSON on stdin, have a strict timeout/output cap,
and are recorded in the same run event stream as planner and tool activity.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import shlex
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from omni.config import OmniSettings
from omni.core.tool_result import (
    is_tool_rejection,
    tool_event_output,
    tool_result_failure,
)
from omni.runtime.processes import process_group_options, stop_process_tree

logger = logging.getLogger(__name__)

@dataclass(slots=True)
class ExecutionPolicyLease:
    """Revocable, one-shot authority shared by copied async contexts."""

    active: bool = True
    contract_claimed: bool = False
    delegation_claimed: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionPolicyFrame:
    """The concrete operation admitted by the current gateway frame."""

    tool_name: str
    arguments_hash: str
    sensitive: bool
    delegated_target: str = ""
    owner_task: Any = field(default=None, compare=False, repr=False)
    lease: ExecutionPolicyLease = field(
        default_factory=ExecutionPolicyLease,
        compare=False,
    )


_EXECUTION_POLICY_STACK: ContextVar[tuple[ExecutionPolicyFrame, ...]] = ContextVar(
    "omni_execution_policy_stack",
    default=(),
)


def execution_policy_active() -> bool:
    """Whether the current call stack is already inside the policy gateway."""
    stack = _EXECUTION_POLICY_STACK.get()
    return bool(stack and _frame_owned_by_current_task(stack[-1]))


def execution_policy_covers(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    sensitive: bool,
) -> bool:
    """Whether the innermost admitted frame is this exact concrete operation.

    A generic wrapper grant (for example ``run_skill``) is not authority for
    the selected skill. Sensitive coverage additionally requires that the
    concrete outer frame itself was classified sensitive.
    """
    stack = _EXECUTION_POLICY_STACK.get()
    if not stack:
        return False
    frame = stack[-1]
    return (
        _frame_owned_by_current_task(frame)
        and frame.tool_name == str(tool_name)
        and frame.arguments_hash == execution_arguments_hash(arguments)
        and (not sensitive or frame.sensitive)
    )


def claim_execution_policy_contract(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    sensitive: bool,
) -> bool:
    """Consume the exact frame's sole contract-only inner invocation."""
    if not execution_policy_covers(
        tool_name,
        arguments,
        sensitive=sensitive,
    ):
        return False
    lease = _EXECUTION_POLICY_STACK.get()[-1].lease
    if lease.contract_claimed:
        return False
    lease.contract_claimed = True
    return True


def claim_execution_policy_delegation(tool_name: str) -> bool:
    """Consume one wrapper frame's authority for its resolved concrete target."""
    stack = _EXECUTION_POLICY_STACK.get()
    if not stack:
        return False
    frame = stack[-1]
    lease = frame.lease
    if (
        not _frame_owned_by_current_task(frame)
        or frame.delegated_target != str(tool_name)
        or lease.delegation_claimed
    ):
        return False
    lease.delegation_claimed = True
    return True


def _frame_owned_by_current_task(frame: ExecutionPolicyFrame) -> bool:
    """Reject ContextVar copies in child tasks or executor threads."""
    if not frame.lease.active:
        return False
    try:
        current = asyncio.current_task()
    except RuntimeError:
        return False
    return current is not None and current is frame.owner_task


def execution_arguments_hash(arguments: dict[str, Any]) -> str:
    """Return the canonical identity used by admission and execution checks."""
    encoded = json.dumps(
        arguments or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()

_SECRET_KEYS = ("api_key", "secret", "password", "token", "credential", "authorization")


@dataclass(slots=True)
class HookDecision:
    action: str = "continue"  # continue | deny
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.action != "deny"


class HookDeniedError(PermissionError):
    """An owner-controlled pre-tool hook explicitly denied execution."""


class HookManager:
    """Dispatch configured lifecycle hooks and preserve an audit trail."""

    def __init__(self, settings: OmniSettings, tasks: Any | None = None) -> None:
        self._cfg = settings.hooks
        self._tasks = tasks

    async def emit(
        self,
        event: str,
        *,
        task_id: str = "",
        payload: dict[str, Any] | None = None,
        deny_capable: bool = False,
    ) -> HookDecision:
        if not self._cfg.enabled:
            return HookDecision()
        commands = [
            *self._cfg.commands.get("*", []),
            *self._cfg.commands.get(event, []),
        ]
        if not commands:
            return HookDecision()

        envelope = {
            "event": event,
            "task_id": task_id,
            "payload": _redact(payload or {}),
        }
        warnings: list[str] = []
        for command in commands:
            started = time.monotonic()
            status = "succeeded"
            error = ""
            output: dict[str, Any] = {}
            try:
                output = await self._run(command, envelope)
            except Exception as exc:  # noqa: BLE001 - hook policy decides fail-open/closed
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
                warnings.append(f"hook {event} failed: {error}")
                logger.warning("lifecycle hook failed event=%s command=%s: %s", event, command, error)

            duration_ms = (time.monotonic() - started) * 1000
            if self._tasks is not None and task_id:
                await self._tasks.append_event(
                    task_id,
                    event_type="hook.done" if status == "succeeded" else "hook.failed",
                    status=status,
                    name=event,
                    input_json={"event": event, "command": _command_label(command)},
                    output_json=output,
                    error=error,
                    duration_ms=duration_ms,
                    summary=f"hook {event} {status}",
                )

            requested = str(output.get("action") or "continue").lower()
            if requested == "deny" and deny_capable:
                return HookDecision(
                    action="deny",
                    reason=str(output.get("reason") or f"hook {event} denied execution"),
                    warnings=warnings,
                )
            if error and self._cfg.failure_policy == "fail" and deny_capable:
                return HookDecision(action="deny", reason=error, warnings=warnings)
        return HookDecision(warnings=warnings)

    async def _run(self, command: str, envelope: dict[str, Any]) -> dict[str, Any]:
        argv = shlex.split(command)
        if not argv:
            raise ValueError("empty hook command")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group_options(),
        )
        raw = json.dumps(envelope, ensure_ascii=False, default=str).encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(raw), timeout=max(0.1, float(self._cfg.timeout_s))
            )
        except TimeoutError:
            await stop_process_tree(proc, grace_seconds=0.1)
            raise TimeoutError(f"hook timed out after {self._cfg.timeout_s}s") from None
        except asyncio.CancelledError:
            await stop_process_tree(proc)
            raise

        limit = max(1024, int(self._cfg.max_output_bytes))
        if len(stdout) > limit or len(stderr) > limit:
            raise RuntimeError(f"hook output exceeded {limit} bytes")
        if proc.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"hook exited {proc.returncode}: {message[:500]}")
        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            return {}
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("hook output must be a JSON object")
        return _redact(parsed)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lower = str(key).lower()
            out[str(key)] = "[REDACTED]" if any(token in lower for token in _SECRET_KEYS) else _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _command_label(command: str) -> str:
    try:
        argv = shlex.split(command)
    except ValueError:
        return "invalid"
    return argv[0] if argv else ""


async def invoke_tool_with_hooks(
    hooks: HookManager | None,
    *,
    task_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    family: str,
    invoke: Callable[[], Awaitable[Any]],
    approval_gate: Any = None,
    resource_locks: Any = None,
    resource_scope: str = "",
    sensitive: bool = False,
    delegated_target: str = "",
    result_finalizer: Callable[[Any, bool], Any] | None = None,
    pre_invoke_validator: Callable[[], Any] | None = None,
) -> Any:
    """Invoke one tool under the shared hook, approval, and resource policy.

    This helper is used by the coordinator, prompt-only skills, workflow steps,
    subtasks, and delegated specialists so no execution path can silently
    bypass an owner's lifecycle policy.
    """
    policy_arguments = copy.deepcopy(arguments)
    if hooks is not None:
        decision = await hooks.emit(
            "pre_tool",
            task_id=task_id,
            payload={
                "tool_name": tool_name,
                "arguments": copy.deepcopy(policy_arguments),
                "family": family,
            },
            deny_capable=True,
        )
        if not decision.allowed:
            raise HookDeniedError(decision.reason)
    execution_started = False

    async def invoke_provider() -> Any:
        nonlocal execution_started
        if pre_invoke_validator is not None:
            rejected = pre_invoke_validator()
            if inspect.isawaitable(rejected):
                rejected = await rejected
            if rejected is not None:
                return rejected
        execution_started = True
        return await invoke()

    async def invoke_locked() -> Any:
        frame = ExecutionPolicyFrame(
            tool_name=str(tool_name),
            arguments_hash=execution_arguments_hash(policy_arguments),
            sensitive=bool(sensitive),
            delegated_target=str(delegated_target or ""),
            owner_task=asyncio.current_task(),
        )
        token = _EXECUTION_POLICY_STACK.set((*_EXECUTION_POLICY_STACK.get(), frame))
        try:
            if resource_locks is None:
                return await invoke_provider()
            from omni.runtime.execution_policy import resources_for_tool

            resources = resources_for_tool(
                tool_name,
                copy.deepcopy(policy_arguments),
                scope=resource_scope,
                sensitive=sensitive,
            )
            return await resource_locks.run(resources, invoke_provider)
        finally:
            # ContextVars are copied into asyncio children. Revoking the shared
            # lease prevents a detached child from retaining authority after
            # the parent invocation has returned and reset its local stack.
            frame.lease.active = False
            _EXECUTION_POLICY_STACK.reset(token)

    try:
        if approval_gate is None:
            result = await invoke_locked()
        else:
            result = await approval_gate.invoke(
                tool_name,
                copy.deepcopy(policy_arguments),
                invoke_locked,
                sensitive=sensitive,
            )
        if result_finalizer is not None:
            result = result_finalizer(result, execution_started)
    except Exception as exc:
        if hooks is not None:
            await hooks.emit(
                "post_tool",
                task_id=task_id,
                payload={
                    "tool_name": tool_name,
                    "arguments": copy.deepcopy(policy_arguments),
                    "family": family,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        raise
    if hooks is not None:
        event_result = tool_event_output(result)
        denied = is_tool_rejection(event_result)
        failure = tool_result_failure(event_result)
        await hooks.emit(
            "post_tool",
            task_id=task_id,
            payload={
                "tool_name": tool_name,
                "arguments": copy.deepcopy(policy_arguments),
                "family": family,
                "status": (
                    "denied"
                    if denied
                    else (failure[0] if failure is not None else "succeeded")
                ),
                "result": _result_brief(event_result),
            },
        )
    return result


def _result_brief(value: Any, *, limit: int = 1000) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _result_brief(item, limit=max(100, limit // 2))
            for key, item in list(value.items())[:20]
        }
    if isinstance(value, list):
        return [_result_brief(item, limit=max(100, limit // 2)) for item in value[:10]]
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


__all__ = [
    "ExecutionPolicyFrame",
    "ExecutionPolicyLease",
    "HookDecision",
    "HookManager",
    "claim_execution_policy_contract",
    "claim_execution_policy_delegation",
    "execution_policy_active",
    "execution_policy_covers",
    "execution_arguments_hash",
    "invoke_tool_with_hooks",
]
