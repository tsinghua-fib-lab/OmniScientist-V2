"""Skill executor — the four forms of consumption.

- ``python_engine``: import ``module.class`` and call ``method(**input)``.
- ``cli_exec``: run a subprocess, pass input as JSON on stdin, parse stdout.
- ``prompt_only``: run a focused ReAct sub-agent seeded with the skill body.
- ``remote_mcp``: handled by the MCP layer (surfaced as direct tools), not here.

The same executor backs both the synchronous tool path (ReAct invoker) and
the asynchronous background task path (task runtime).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import importlib.util
import inspect
import json
import logging
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omni.config.settings import resolve_max_output_tokens
from omni.core.termination import (
    base_termination_reason,
    is_bounded_termination,
    termination_next_action,
    termination_reason_label,
)
from omni.core.tool_result import command_result_status, owned_result_outcome
from omni.runtime.execution_policy import skill_requires_approval
from omni.runtime.hooks import execution_policy_active, execution_policy_covers
from omni.runtime.tool_gateway import ToolGateway
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.manifest import SkillEntry, SkillKind

logger = logging.getLogger(__name__)
_ENGINE_IMPORT_LOCK = threading.RLock()


class SkillExecutionError(RuntimeError):
    """A trusted skill could not be executed."""

# Transient conditions: the same run, re-issued unchanged, can legitimately
# succeed. Resource exhaustion is deliberately *not* here — replaying a run that
# spent its whole iteration/tool budget under that same budget only spends it
# again, so it is reported as a bounded result with a widen-the-budget
# affordance rather than as something the workflow layer should silently retry.
_RECOVERABLE_PROMPT_REASONS = frozenset(
    {
        "llm_error",
        "llm_timeout",
        "llm_rate_limited",
        "llm_unavailable",
        "llm_transcript_invalid",
    }
)
_SALVAGEABLE_PROMPT_REASONS = frozenset({"timeout", "max_iterations", "max_tool_calls"})


def _provider_snapshot_names(snapshot: dict[str, Any]) -> tuple[str, str]:
    """Return the ``(name, source)`` a provider snapshot claims to govern.

    ``execution_identity`` wins over the binding fields: capability resolution
    seals ``provider_name`` as the *planned* name, which may differ from the
    entry that was actually resolved and hashed.
    """
    identity = snapshot.get("execution_identity")
    identity = identity if isinstance(identity, dict) else {}
    return (
        str(identity.get("name") or snapshot.get("provider_name") or ""),
        str(identity.get("source") or snapshot.get("provider_source") or ""),
    )


def _authority_for_entry(ctx: ExecContext, entry: SkillEntry) -> dict[str, Any]:
    """Return the direct or delegated provider slice governing ``entry``.

    An empty result means "this authority does not govern ``entry``" and callers
    fail closed on it. That distinction is the point: handing back a snapshot
    sealed for a *different* provider made the fingerprint check compare two
    unrelated skills and report a benign context reuse as provider tampering.
    """
    authority = getattr(ctx, "provider_authority", None)
    if not isinstance(authority, dict):
        return {}
    governed, source = _provider_snapshot_names(authority)
    if governed == entry.name and (not source or source == entry.source):
        return authority
    delegated = authority.get("delegated_provider_authorities")
    if isinstance(delegated, list):
        for item in delegated:
            if not isinstance(item, dict):
                continue
            name, item_source = _provider_snapshot_names(item)
            if name == entry.name and (
                not item_source or item_source == entry.source
            ):
                return item
        return {}
    if governed:
        return {}
    return authority


def _authority_scope_error(ctx: ExecContext, entry: SkillEntry) -> str:
    """Explain why the authority in scope may not stand in as ``entry``'s seal.

    Deliberately not phrased as a fingerprint mismatch: nothing about the
    provider changed, the authority in hand simply belongs to another consumer.
    """
    authority = getattr(ctx, "provider_authority", None)
    if not isinstance(authority, dict) or not authority:
        return ""
    if isinstance(authority.get("delegated_provider_authorities"), list):
        return (
            f"provider execution authority is missing for delegated skill "
            f"'{entry.name}'"
        )
    governed, _ = _provider_snapshot_names(authority)
    if governed and governed != entry.name:
        return (
            f"provider execution authority in scope governs '{governed}', not "
            f"'{entry.name}'; re-plan or re-submit before running"
        )
    return f"provider execution authority is missing for skill '{entry.name}'"


def _load_engine_module(
    entry: SkillEntry,
    module_name: str,
    *,
    expected_authority: dict[str, Any] | None = None,
):  # noqa: ANN202
    """Load one engine while serializing process-global import-cache changes."""
    with _ENGINE_IMPORT_LOCK:
        return _load_engine_module_locked(
            entry,
            module_name,
            expected_authority=expected_authority,
        )


def _load_engine_module_locked(
    entry: SkillEntry,
    module_name: str,
    *,
    expected_authority: dict[str, Any] | None = None,
):  # noqa: ANN202
    """Load a skill's engine module.

    Skills are self-contained: an ``engine.module`` like ``engine`` refers to a
    file *inside the skill package* (``<skill_dir>/engine.py``) and is loaded by
    path, so skill content needs no CLI-internal import. A dotted, installed
    module name (e.g. ``pkg.mod``) still imports normally — keeping the loader
    backward compatible and able to run engines shipped as real packages.
    """
    from omni.agent.plan_revision import (
        provider_authority_snapshot,
        provider_snapshot_authority_error,
    )

    live_authority = provider_authority_snapshot(entry)
    sealed_authority = expected_authority or live_authority
    authority_error = provider_snapshot_authority_error(
        live_authority,
        sealed_authority,
    )
    if authority_error:
        raise SkillExecutionError(authority_error)
    authority_fingerprint = str(live_authority.get("fingerprint") or "")
    path = getattr(entry, "path", None)
    if path is not None:
        base = path if path.is_dir() else path.parent
        candidate = base / f"{module_name}.py"
        if candidate.is_file():
            try:
                source_bytes = candidate.read_bytes()
            except OSError as exc:
                raise SkillExecutionError(
                    f"cannot read engine {candidate}: {exc}"
                ) from exc
            source_sha = hashlib.sha256(source_bytes).hexdigest()
            expected_engine = (
                (expected_authority or {})
                .get("execution_identity", {})
                .get("engine", {})
            )
            expected_artifact = (
                expected_engine.get("artifact")
                if isinstance(expected_engine, dict)
                else {}
            )
            expected_sha = str(
                expected_artifact.get("sha256")
                if isinstance(expected_artifact, dict)
                else ""
            )
            if expected_sha and expected_sha != source_sha:
                raise SkillExecutionError(
                    "provider execution authority changed while loading engine; "
                    "re-plan or re-submit before running"
                )
            identity = hashlib.sha256(
                (
                    f"{entry.source}\0{entry.name}\0"
                    f"{candidate.resolve(strict=False)}\0{source_sha}\0"
                    f"{authority_fingerprint}"
                ).encode("utf-8", errors="backslashreplace")
            ).hexdigest()
            mod_name = (
                f"omni_skill__{entry.name.replace('-', '_')}__"
                f"{module_name.replace('.', '_')}__{identity}"
            )
            _evict_stale_skill_modules(
                base,
                authority_fingerprint=authority_fingerprint,
            )
            _evict_conflicting_local_modules(base)
            cached = sys.modules.get(mod_name)
            if cached is not None:
                return cached
            spec = importlib.util.spec_from_loader(mod_name, loader=None)
            if spec is not None:
                module = importlib.util.module_from_spec(spec)
                module.__file__ = str(candidate)
                module.__omni_authority_sha256__ = source_sha
                module.__omni_authority_fingerprint__ = authority_fingerprint
                sys.modules[mod_name] = module
                try:
                    exec(
                        compile(source_bytes, str(candidate), "exec"),
                        module.__dict__,
                    )
                except Exception:
                    sys.modules.pop(mod_name, None)
                    raise
                try:
                    after_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
                except OSError as exc:
                    sys.modules.pop(mod_name, None)
                    raise SkillExecutionError(
                        f"cannot re-check engine {candidate}: {exc}"
                    ) from exc
                if after_sha != source_sha:
                    sys.modules.pop(mod_name, None)
                    raise SkillExecutionError(
                        "provider execution authority changed while loading engine; "
                        "re-plan or re-submit before running"
                    )
                _mark_loaded_skill_modules(
                    base,
                    authority_fingerprint=authority_fingerprint,
                )
                after_authority = provider_authority_snapshot(entry)
                if provider_snapshot_authority_error(
                    after_authority,
                    (
                        sealed_authority
                        if expected_authority
                        else after_authority
                    ),
                ):
                    sys.modules.pop(mod_name, None)
                    raise SkillExecutionError(
                        "provider execution authority changed while loading "
                        "engine; re-plan or re-submit before running"
                    )
                return module
    closure = (
        live_authority.get("execution_identity", {})
        .get("engine", {})
        .get("dependency_closure", {})
    )
    package_root = (
        Path(str(closure.get("root")))
        if isinstance(closure, dict) and closure.get("root")
        else None
    )
    if package_root is not None:
        _evict_stale_skill_modules(
            package_root,
            authority_fingerprint=authority_fingerprint,
        )
    module = importlib.import_module(module_name)
    expected_engine = (
        (expected_authority or {}).get("execution_identity", {}).get("engine", {})
    )
    expected_artifact = (
        expected_engine.get("artifact")
        if isinstance(expected_engine, dict)
        else {}
    )
    expected_sha = str(
        expected_artifact.get("sha256")
        if isinstance(expected_artifact, dict)
        else ""
    )
    origin = str(getattr(module, "__file__", "") or "")
    if expected_sha and origin.endswith((".py", ".pyw")):
        try:
            actual_sha = hashlib.sha256(
                Path(origin).read_bytes()
            ).hexdigest()
        except OSError as exc:
            raise SkillExecutionError(
                f"cannot verify loaded engine {module_name}: {exc}"
            ) from exc
        if actual_sha != expected_sha:
            raise SkillExecutionError(
                "provider execution authority changed while loading engine; "
                "re-plan or re-submit before running"
            )
        if getattr(module, "__omni_authority_sha256__", "") != actual_sha:
            importlib.invalidate_caches()
            module = importlib.reload(module)
            post_sha = hashlib.sha256(Path(origin).read_bytes()).hexdigest()
            if post_sha != expected_sha:
                raise SkillExecutionError(
                    "provider execution authority changed while loading engine; "
                    "re-plan or re-submit before running"
                )
            module.__omni_authority_sha256__ = post_sha
    if package_root is not None:
        _mark_loaded_skill_modules(
            package_root,
            authority_fingerprint=authority_fingerprint,
        )
    after_authority = provider_authority_snapshot(entry)
    if provider_snapshot_authority_error(
        after_authority,
        sealed_authority if expected_authority else after_authority,
    ):
        raise SkillExecutionError(
            "provider execution authority changed while loading engine; "
            "re-plan or re-submit before running"
        )
    return module


def _module_is_under(module: Any, root: Path) -> bool:
    module_file = str(getattr(module, "__file__", "") or "")
    if not module_file:
        return False
    try:
        return Path(module_file).resolve(strict=False).is_relative_to(
            root.resolve(strict=False)
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _evict_conflicting_local_modules(root: Path) -> None:
    """Prevent one skill from inheriting another skill's bare local imports."""
    local_names: set[str] = set()
    try:
        children = tuple(root.iterdir())
    except OSError:
        return
    import_suffixes = tuple(
        sorted(
            importlib.machinery.all_suffixes(),
            key=len,
            reverse=True,
        )
    )
    for child in children:
        name = child.name
        try:
            if child.is_dir():
                if name.isidentifier() and name != "__pycache__":
                    local_names.add(name)
                continue
            if not child.is_file():
                continue
        except OSError:
            continue
        for suffix in import_suffixes:
            if name.endswith(suffix):
                module_name = name[: -len(suffix)]
                if module_name.isidentifier():
                    local_names.add(module_name)
                break
    for name, module in tuple(sys.modules.items()):
        if name.partition(".")[0] not in local_names:
            continue
        if module is not None and _module_is_under(module, root):
            continue
        sys.modules.pop(name, None)


def _evict_stale_skill_modules(
    root: Path,
    *,
    authority_fingerprint: str,
) -> None:
    """Drop modules loaded from an older version of one skill tree."""
    for name, module in tuple(sys.modules.items()):
        if (
            module is not None
            and _module_is_under(module, root)
            and str(
                getattr(module, "__omni_authority_fingerprint__", "") or ""
            )
            != authority_fingerprint
        ):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _mark_loaded_skill_modules(
    root: Path,
    *,
    authority_fingerprint: str,
) -> None:
    """Bind newly imported sibling modules to the same provider closure."""
    for module in tuple(sys.modules.values()):
        if module is not None and _module_is_under(module, root):
            module.__omni_authority_fingerprint__ = authority_fingerprint


class SkillExecutionTimeout(SkillExecutionError):
    pass


def _make_skill_handler(skill, ctx):  # noqa: ANN001, ANN201
    async def handler(args: dict) -> Any:
        expected = _authority_for_entry(ctx, skill)
        if getattr(ctx, "provider_authority", None) and not expected:
            raise SkillExecutionError(_authority_scope_error(ctx, skill))
        if expected:
            from omni.agent.plan_revision import provider_authority_error

            error = provider_authority_error(
                skill,
                expected,
                registry=getattr(ctx, "registry", None),
            )
            if error:
                raise SkillExecutionError(error)
        return await execute_skill(skill, args, ctx)
    return handler


def _jsonify(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except TypeError:
        return str(value)


def _sanitize_json_surrogates(value: Any) -> Any:
    """Replace invalid Unicode surrogate code points at process boundaries."""
    if isinstance(value, str):
        return "".join(
            "\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char
            for char in value
        )
    if isinstance(value, dict):
        return {
            _sanitize_json_surrogates(key): _sanitize_json_surrogates(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json_surrogates(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_json_surrogates(item) for item in value)
    return value


def _json_stdin_bytes(value: Any) -> bytes:
    """Serialize skill stdin as strict, valid UTF-8 JSON."""
    safe_value = _sanitize_json_surrogates(value)
    return json.dumps(
        safe_value,
        ensure_ascii=False,
        default=lambda item: _sanitize_json_surrogates(str(item)),
    ).encode("utf-8")


def _prompt_partial_outputs(tool_trace: list[Any]) -> list[dict[str, Any]]:
    """Extract likely useful partial artifacts from a prompt-only sub-agent trace."""
    outputs: list[dict[str, Any]] = []
    for record in tool_trace:
        name = str(getattr(record, "name", ""))
        args = getattr(record, "arguments", {}) if isinstance(getattr(record, "arguments", {}), dict) else {}
        item: dict[str, Any] | None = None
        if name in {"write_file", "edit_file"} and args.get("path"):
            item = {"tool": name, "path": str(args.get("path"))}
        elif name == "bash" and args.get("command"):
            item = {"tool": name, "command": str(args.get("command"))[:500]}
        if item is not None:
            error = getattr(record, "error", None)
            # Keep the existing prompt-skill ``ok`` / ``error`` field stable:
            # it describes whether Omni invoked the tool successfully. Command
            # outcomes are a separate domain layer (a process can exit non-zero
            # after the Bash tool itself completed normally).
            item["status"] = "error" if error else "ok"
            item["transport_status"] = str(
                getattr(record, "status", "") or ("failed" if error else "succeeded")
            )
            result = getattr(record, "result", None)
            if isinstance(result, dict) and command_result_status(result) is not None:
                for key in ("command_status", "reason", "exit_code"):
                    if key in result:
                        item[key] = result[key]
            try:
                observation = record.to_observation()
            except Exception:  # noqa: BLE001 - partial recovery must stay best-effort
                observation = (
                    result
                    if isinstance(result, str)
                    else json.dumps(result, ensure_ascii=False, default=str)
                    if result not in (None, "")
                    else ""
                )
            if observation not in (None, ""):
                item["observation"] = str(observation)[:500]
            outputs.append(item)
    return outputs


def _bounded_stop_message(entry: SkillEntry, reason: str) -> str:
    """Say which budget stopped the run, in the user's terms."""
    label = termination_reason_label(reason)
    return f"{entry.name} stopped at its budget ({label})" if label else ""


def _prompt_skill_result(
    entry: SkillEntry,
    result: Any,
    *,
    salvage_text: str = "",
) -> dict[str, Any]:
    """Return a stable contract for prompt-only skill sub-agent outcomes."""
    reason = base_termination_reason(str(result.terminated_reason or ""))
    bounded = is_bounded_termination(str(result.terminated_reason or ""))
    status = "ok" if result.kind == "text" else result.kind
    original_error = ""
    if status == "error":
        original_error = result.content or result.terminated_reason or f"{entry.name} failed"
    text = str(salvage_text or result.content or "").strip()
    salvaged_empty = bool(salvage_text.strip()) and not str(result.content or "").strip()
    if status == "error" and salvage_text:
        status = "partial"
    elif status == "ok" and salvaged_empty:
        # The model announced done with no answer. Salvage recovered prose, but
        # that is a wrap-up, not a completed skill — same honesty as a budget stop.
        status = "partial"
        original_error = f"{entry.name} stopped without a final answer"
    if (
        status == "ok"
        and not text
        and not _prompt_has_file_deliverable(result)
    ):
        status = "error"
        original_error = f"{entry.name} returned empty result"
    # The loop now forces a real final answer when it hits a budget, so the
    # result arrives as usable text. That answer is still *bounded*: reporting
    # it as a plain success would hide that exploration was cut short, which is
    # how a truncated review used to pass as a finished one.
    if status == "ok" and bounded:
        status = "partial"
    payload: dict[str, Any] = {
        "status": status,
        "text": text,
        "tools_used": result.tool_names(),
        "terminated_reason": result.terminated_reason,
        "total_iterations": result.total_iterations,
        "total_tool_calls": result.total_tool_calls,
        "usage": dict(result.total_usage),
        "usage_budget": dict(result.usage_budget),
        "partial_outputs": _prompt_partial_outputs(result.tool_trace),
    }
    next_action = termination_next_action(reason)
    if next_action:
        payload["next_action"] = next_action
    if status == "partial":
        message = original_error or _bounded_stop_message(entry, reason) or f"{entry.name} stopped early"
        # A run stopped by its own budget is not retryable *as issued*: the
        # replay would exhaust the same ceiling. It is recoverable only by
        # widening the budget, which is an operator decision, so the workflow
        # layer must not loop on it.
        retryable = not bounded
        payload["warning"] = message
        payload["summary"] = f"{entry.name} partially completed: {message}"
        payload["recoverable"] = True
        payload["blocking"] = False
        payload["error_info"] = {
            "code": result.terminated_reason or "partial",
            "message": message,
            "retryable": retryable,
            "workflow_recoverable": retryable,
            "tool_calls": result.total_tool_calls,
            "iterations": result.total_iterations,
            **({"next_action": next_action} if next_action else {}),
        }
    if status == "error":
        message = original_error
        payload["error"] = message
        payload["summary"] = f"{entry.name} did not complete: {message}"
        retryable = reason in _RECOVERABLE_PROMPT_REASONS
        payload["recoverable"] = retryable
        payload["blocking"] = not retryable
        payload["error_info"] = {
            "code": result.terminated_reason or "error",
            "message": message,
            "retryable": retryable,
            "workflow_recoverable": retryable,
            "terminated_reason": result.terminated_reason,
            "tool_calls": result.total_tool_calls,
            "iterations": result.total_iterations,
            **({"next_action": next_action} if next_action else {}),
        }
    return payload


def _int_policy(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _float_policy(value: Any, default: float) -> float:
    try:
        return max(0.1, float(value))
    except (TypeError, ValueError):
        return default


def _bounded_int_policy(value: Any, default: int, ceiling: int) -> int:
    return min(_int_policy(value, max(1, int(default))), max(1, int(ceiling)))


@dataclass(frozen=True)
class SkillBudget:
    """A skill's wall clock plus the advice that names whatever bound it.

    Resolution is ``skill-specific > skill-global > ceiling > live envelope``:
    a manifest's ``execution.max_seconds`` wins over the global fallback, may
    raise it up to the owner-trusted ceiling, and is then bounded for real by
    the workflow envelope clock. Keeping ``default`` and ``ceiling`` distinct is
    the point — passing one value for both silently clamps every declaration
    back to the fallback.
    """

    seconds: float
    remedy: str
    stall_seconds: float = 0.0

    def timeout_error(self, name: str) -> SkillExecutionTimeout:
        return SkillExecutionTimeout(
            f"skill '{name}' timed out after {self.seconds:g}s — {self.remedy}"
        )

    def stall_error(self, name: str) -> SkillExecutionTimeout:
        return SkillExecutionTimeout(
            f"skill '{name}' reported no progress for {self.stall_seconds:g}s — the run "
            f"is stuck rather than slow. Raise `execution.stall_seconds` in its SKILL.md "
            f"if the skill is simply quiet for that long."
        )


def _skill_budget(
    entry: Any, ctx: Any, *, default: float, ceiling: float, knob: str
) -> SkillBudget:
    declared = _float_policy((entry.execution or {}).get("max_seconds"), 0.0)
    requested = declared if declared > 0 else float(default)
    capped = min(requested, max(0.01, float(ceiling)))
    if declared > capped:
        # Loud, not silent: the manifest asked for a budget it can never get, so
        # the author learns here instead of at the truncated deadline.
        logger.warning(
            "[skills] skill '%s' declares execution.max_seconds=%g but %s caps it at %g",
            entry.name, declared, knob, capped,
        )
    seconds = _remaining_timeout(ctx, capped)
    if seconds < capped:
        remedy = (
            "the surrounding workflow envelope ran out first, so the skill never had "
            "its full budget. Give the workflow more room with "
            "`/config set tasks.workflow_max_seconds <seconds>`."
        )
    elif capped < requested:
        remedy = (
            f"its SKILL.md asked for {requested:g}s but `{knob}` caps skills at "
            f"{capped:g}s. Raise the ceiling with `/config set {knob} <seconds>`."
        )
    elif declared > 0:
        remedy = (
            "that is the budget its own SKILL.md declares; raise "
            "`execution.max_seconds` there if the work legitimately takes longer."
        )
    else:
        remedy = (
            f"it declares no budget of its own, so it fell back to {default:g}s. "
            "Declare `execution.max_seconds` in its SKILL.md if the work legitimately "
            "takes longer."
        )
    stall = _float_policy((entry.execution or {}).get("stall_seconds"), 0.0)
    return SkillBudget(seconds, remedy, min(stall, seconds) if stall > 0 else 0.0)


class _SkillStalled(Exception):
    """A watched skill went quiet for longer than its declared stall window."""


class _ProgressHeartbeat:
    """Records when a skill last reported progress.

    Mirrors the coordinator loop, where ``react.stall_timeout_s`` is the primary
    "something is stuck" guard and the wall clock is only a runaway backstop. A
    skill is watched only when it declares ``execution.stall_seconds`` *and*
    accepts a progress callback — a silent engine cannot feed a watchdog, so it
    keeps the plain wall-clock behaviour.
    """

    def __init__(self) -> None:
        self.last = time.monotonic()
        self.armed = False

    def wrap(self, callback: Any) -> Any:
        if callback is None:
            return None
        self.armed = True

        def _forward(*args: Any, **kwargs: Any) -> Any:
            self.last = time.monotonic()
            return callback(*args, **kwargs)

        return _forward


async def _await_skill_call(call: Any, budget: SkillBudget, heartbeat: _ProgressHeartbeat) -> Any:
    if budget.stall_seconds <= 0 or not heartbeat.armed:
        return await asyncio.wait_for(call, timeout=budget.seconds)
    task = asyncio.ensure_future(call)
    deadline = time.monotonic() + budget.seconds
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError
            if now >= heartbeat.last + budget.stall_seconds:
                raise _SkillStalled
            slice_s = min(deadline, heartbeat.last + budget.stall_seconds) - now
            done, _ = await asyncio.wait({task}, timeout=slice_s)
            if done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


def _remaining_timeout(ctx: Any, requested: float) -> float:
    # Prefer the live pause-aware envelope clock when a workflow supplied one:
    # its ``remaining()`` already credits any approval waits, so a skill started
    # after a long human decision isn't handed a negative budget.
    clock = getattr(ctx, "execution_clock", None)
    if clock is not None:
        remaining = clock.remaining()
        if remaining <= 0:
            raise SkillExecutionTimeout("workflow execution envelope timed out")
        return min(requested, remaining)
    deadline = float(getattr(ctx, "execution_deadline", 0.0) or 0.0)
    if deadline <= 0:
        return requested
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SkillExecutionTimeout("workflow execution envelope timed out")
    return min(requested, remaining)


def _tool_limits(entry: SkillEntry) -> dict[str, int]:
    raw = (entry.execution or {}).get("tool_limits")
    if not isinstance(raw, dict):
        return {}
    limits: dict[str, int] = {}
    for name, value in raw.items():
        try:
            limit = int(value)
        except (TypeError, ValueError):
            continue
        if limit > 0:
            limits[str(name)] = limit
    return limits


def _installed_skill_root(entry: SkillEntry) -> Path | None:
    if entry.path is None:
        return None
    root = entry.path if entry.path.is_dir() else entry.path.parent
    return root.resolve()


def _prompt_has_file_deliverable(result: Any) -> bool:
    for record in getattr(result, "tool_trace", None) or []:
        name = str(getattr(record, "name", "") or "")
        args = getattr(record, "arguments", {})
        if not isinstance(args, dict):
            continue
        if name in {"write_file", "edit_file"} and str(args.get("path") or "").strip():
            return True
    return False


def _should_salvage_prompt_result(result: Any) -> bool:
    """Salvage budget stops *and* a voluntary empty ``done`` (Codex-style wrap-up)."""
    reason = base_termination_reason(str(result.terminated_reason or ""))
    if result.kind == "error" and reason in _SALVAGEABLE_PROMPT_REASONS:
        return True
    if str(getattr(result, "content", "") or "").strip():
        return False
    if _prompt_has_file_deliverable(result):
        return False
    return reason in {"done", "no_progress", *_SALVAGEABLE_PROMPT_REASONS}


def _recoverable_prompt_boundary(result: Any) -> bool:
    return _should_salvage_prompt_result(result)


async def _salvage_prompt_result(entry: SkillEntry, merged: dict[str, Any], result: Any, ctx: Any) -> str:
    """One no-tool finalization pass after a prompt skill hits a runtime boundary."""
    if not _should_salvage_prompt_result(result) or getattr(ctx, "llm", None) is None:
        return ""
    observations: list[str] = []
    for idx, record in enumerate(result.tool_trace[-12:], start=1):
        try:
            obs = record.to_observation()
        except Exception:  # noqa: BLE001
            obs = str(getattr(record, "result", "") or getattr(record, "error", ""))
        observations.append(
            f"{idx}. {record.name} args={json.dumps(record.arguments, ensure_ascii=False, default=str)[:500]}\n"
            f"   observation={obs[:1000]}"
        )
    system = (
        "You are finalizing a skill run that stopped without a usable answer. "
        "Do not call tools. Produce the best partial result from the available observations, "
        "clearly list missing work, and do not invent evidence or artifacts."
    )
    user = (
        f"Skill: {entry.name}\n"
        f"Original input: {json.dumps(merged, ensure_ascii=False, default=str)[:2000]}\n"
        f"Stop reason: {result.terminated_reason}\n\n"
        "Recent observations:\n"
        + "\n\n".join(observations)
        + "\n\nReturn a concise partial answer with a 'Warnings / Remaining work' note."
    )
    try:
        output = str(
            await asyncio.wait_for(ctx.llm.chat(system, user, temperature=0.2), timeout=30)
        ).strip()
        db = getattr(ctx, "db", None)
        task_id = str(getattr(ctx, "task_id", "") or "")
        if db is not None and task_id:
            from omni.agent.cost import record_text_cost_event
            from omni.runtime.task_recorder import TaskRecorder

            await record_text_cost_event(
                TaskRecorder(db, project=getattr(ctx, "project", "default") or "default"),
                ctx.settings,
                ctx.llm,
                task_id,
                system=system,
                user_message=user,
                output=output,
                component=f"prompt_skill:{entry.name}:salvage",
            )
        return output
    except Exception:  # noqa: BLE001
        return ""


def _filter_prompt_tools(entry: SkillEntry, tools: list[Any]) -> list[Any]:
    """Apply the Claude-compatible ``allowed-tools`` contract when present."""
    if not entry.allowed_tools:
        return tools
    allowed = {str(name) for name in entry.allowed_tools}
    return [tool for tool in tools if tool.spec.name in allowed]


def _make_local_tool_handler(spec: Any, instance: Any):  # noqa: ANN202
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        validate = getattr(instance, "validate_params", None)
        if callable(validate):
            try:
                error = validate(arguments=args, input_data=args)
            except TypeError:
                error = None
            if error:
                return (
                    {"status": "error", **error}
                    if isinstance(error, dict)
                    else {
                        "status": "error",
                        "error": str(error),
                    }
                )
        method = getattr(instance, spec.method or "execute")
        result = method(**dict(args or {}))
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {"status": "ok", "result": _jsonify(result)}

    return handler


def _build_local_prompt_tools(entry: SkillEntry, ctx: Any) -> list[Any]:
    """Load trusted Skill-private tools without adding them to the global catalog."""
    if not entry.trusted or not entry.local_tools:
        return []
    from omni.core.react_agent import ToolSpec
    from omni.skills_runtime.context import Tool

    tools: list[Tool] = []
    for spec in entry.local_tools:
        try:
            module = _load_engine_module(entry, spec.module)
            cls = getattr(module, spec.class_name)
            instance = cls()
            instance.ctx = ctx
        except (ImportError, AttributeError, OSError, SyntaxError) as exc:
            logger.warning(
                "cannot load local tool %s for skill %s: %s",
                spec.name,
                entry.name,
                exc,
            )
            continue
        tools.append(
            Tool(
                ToolSpec(spec.name, spec.description, spec.input_schema),
                _make_local_tool_handler(spec, instance),
                outcome_resolver=owned_result_outcome,
            )
        )
    return tools


def _prompt_budget_note(entry: SkillEntry, limits: dict[str, int | float]) -> str:
    parts: list[str] = []
    if limits.get("max_tool_calls"):
        parts.append(f"at most {limits['max_tool_calls']} tool calls")
    if limits.get("max_iterations"):
        parts.append(f"at most {limits['max_iterations']} reasoning iterations")
    limits = _tool_limits(entry)
    if limits:
        parts.append(
            "per-tool limits: " + ", ".join(f"{name}<={limit}" for name, limit in sorted(limits.items()))
        )
    if not parts:
        return ""
    return "\n\nExecution limits: " + "; ".join(parts) + ". At a limit, return a partial result from existing evidence and identify the gaps."


def _prompt_skill_runtime_context(entry: SkillEntry) -> str:
    """Expose the installed Skill root so bundled relative resources resolve."""
    if entry.path is None:
        return ""
    root = entry.path if entry.path.is_dir() else entry.path.parent
    return (
        "\n\nOmni runtime context:\n"
        f"- Installed Skill root: {root.resolve()}\n"
        "- Bundled scripts and references are also in $OMNI_SKILL_DIR "
        "(same path). Call them as $OMNI_SKILL_DIR/scripts/... — do not resolve "
        "them against the process working directory.\n"
        "- Keep requested deliverables under the current Omni workspace/artifacts roots."
    )


def _prompt_skill_user_message(merged: dict[str, Any]) -> str:
    """Preserve the primary request and every structured contract argument."""
    primary_key = "input" if merged.get("input") not in (None, "") else "query"
    primary = merged.get(primary_key)
    extras = {key: value for key, value in merged.items() if key != primary_key}
    if isinstance(primary, str) and not extras:
        return primary
    if isinstance(primary, str):
        return (
            primary
            + "\n\nStructured Omni inputs:\n"
            + json.dumps(extras, ensure_ascii=False, sort_keys=True, default=str)
        )
    return json.dumps(merged, ensure_ascii=False, sort_keys=True, default=str)


async def _emit_progress(progress_callback: Any, stage: str, pct: float = 0.0, **data: Any) -> None:
    if progress_callback is None:
        return
    try:
        result = progress_callback(stage, pct, **data)
    except TypeError:
        result = progress_callback(stage, pct)
    if inspect.isawaitable(result):
        await result


# Host-service availability probes: map a declared service name to the object
# on the exec context that reports its own readiness. Each probe returns
# something exposing ``available`` / ``error_code`` / ``missing`` /
# ``setup_command`` (the VlmGateway contract) or ``None`` when the host did not
# inject the service (then the engine's own invariant check reports it).
_SERVICE_PROBES: dict[str, Any] = {
    "vlm": lambda ctx: getattr(ctx, "vlm", None),
}


def _service_configuration_action(entry: SkillEntry, ctx: ExecContext) -> dict[str, Any] | None:
    """Preflight a skill's declared service requirements before running it.

    A skill declares the host services it needs (SKILL.md
    ``runtime_requirements.services``). When a required service is injected but
    not configured, return a terminal ``action_required: configure`` result so
    the turn asks the owner to set it up — instead of dispatching the engine
    only to fail mid-run. Mirrors Codex's capability preflight: never start work
    that is structurally guaranteed to fail for a missing prerequisite. The
    ``action_required`` marker makes the runtime treat this as an admission
    rejection (not an execution attempt).
    """
    for service in entry.requires_services:
        probe = _SERVICE_PROBES.get(service)
        if probe is None:
            continue
        gateway = probe(ctx)
        if gateway is None or getattr(gateway, "available", False):
            continue
        command = str(getattr(gateway, "setup_command", "") or "")
        code = str(getattr(gateway, "error_code", "") or f"{service}_not_configured")
        missing = [str(field) for field in getattr(gateway, "missing", ()) or ()]
        return {
            "status": "error",
            "summary": f"{entry.name} needs the {service} service configured first.",
            "error": f"The {service} service is not configured.",
            "recoverable": False,
            "blocking": True,
            "action_required": {
                "kind": "configure",
                "service": service,
                "command": command,
                "missing": missing,
            },
            "setup_command": command,
            "error_info": {
                "code": code,
                "category": "configuration",
                "retryable": False,
                "workflow_recoverable": False,
            },
        }
    return None


# ``python``/``python3`` are satisfied by the interpreter already running omni,
# so a declared interpreter requirement never blocks on PATH-name quirks.
_INTERPRETER_ALIASES = frozenset({"python", "python3"})


def _missing_binary_action(entry: SkillEntry) -> dict[str, Any] | None:
    """Preflight a skill's declared executable requirements (SKILL.md
    ``requires.bins``).

    A missing binary is structurally fatal, so — like the service gate — reject
    admission with a terminal ``action_required: install`` result instead of
    dispatching an engine that cannot run. This is fully generic: a skill declares
    its bins and the *same* gate enforces them, so no per-skill host code is
    needed. Env vars are intentionally not gated (mirrors Codex, which dropped
    env-var prompting; secrets are the gateway's/engine's concern).
    """
    missing: list[str] = []
    for binary in entry.requires_bins:
        name = str(binary).strip()
        if not name or (name in _INTERPRETER_ALIASES and sys.executable):
            continue
        if shutil.which(name) is None:
            missing.append(name)
    if not missing:
        return None
    joined = ", ".join(missing)
    return {
        "status": "error",
        "summary": f"{entry.name} needs {joined} on PATH first.",
        "error": f"Required executable(s) not found: {joined}.",
        "recoverable": False,
        "blocking": True,
        "action_required": {"kind": "install", "bins": missing},
        "setup_command": "",
        "error_info": {
            "code": "missing_binary",
            "category": "configuration",
            "retryable": False,
            "workflow_recoverable": False,
        },
    }


def _missing_python_module_action(entry: SkillEntry) -> dict[str, Any] | None:
    """Preflight a skill's declared Python module requirements (SKILL.md
    ``runtime_requirements.python_modules``).

    Uses ``importlib.util.find_spec`` against omni's *own* interpreter — the same
    environment the engine imports from — so a module omni already has never
    blocks, and a genuinely missing one is reported once as a terminal
    ``action_required: install`` carrying the skill's declared
    ``dependency_setup_command``. This is the generic answer to "process an
    artifact whose parser isn't installed": a capability declares its modules and
    this single host gate enforces them, instead of the model hand-rolling
    ``pip install`` across the wrong venv/interpreter (PEP 668, missing
    ``.venv/bin/pip``, ``--break-system-packages``…). Adding a new format never
    adds host code — only a skill declaration.
    """
    from omni.skills_runtime.manifest import missing_python_modules

    missing = missing_python_modules(entry)
    if not missing:
        return None
    joined = ", ".join(missing)
    command = str(entry.dependency_setup_command or "").strip()
    error = f"Required Python module(s) not importable in omni's interpreter: {joined}."
    if command:
        error += f" Install with: {command}"
    return {
        "status": "error",
        "summary": f"{entry.name} needs Python module(s) installed first: {joined}.",
        "error": error,
        "recoverable": False,
        "blocking": True,
        "action_required": {
            "kind": "install",
            "python_modules": missing,
            "command": command,
        },
        "setup_command": command,
        "error_info": {
            "code": entry.dependency_error_code or "runtime_dependency_missing",
            "category": "configuration",
            "retryable": False,
            "workflow_recoverable": False,
        },
    }


async def execute_skill(
    entry: SkillEntry,
    input_data: dict[str, Any],
    ctx: ExecContext,
    *,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Execute a trusted skill under the shared tool-gateway policy.

    Trust is fail-closed: quarantined skills never reach an engine. When an
    outer execution policy is already active (for example ``run_skill``), only
    duplicate hooks/approval/locking/limits are skipped; the concrete skill's
    input and output contracts remain mandatory.
    """
    if not entry.trusted:
        raise SkillExecutionError(
            f"skill '{entry.name}' is quarantined; review it and run "
            f"`omni skills trust {entry.name} --yes` before execution"
        )
    expected = _authority_for_entry(ctx, entry)
    if getattr(ctx, "provider_authority", None) and not expected:
        raise SkillExecutionError(_authority_scope_error(ctx, entry))
    skill_root = _installed_skill_root(entry)
    if skill_root is not None and getattr(ctx, "skill_root", None) is None:
        ctx = ctx.for_execution(skill_root=skill_root)
    if expected:
        from omni.agent.plan_revision import provider_authority_error

        authority_error = provider_authority_error(
            entry,
            expected,
            registry=getattr(ctx, "registry", None),
        )
        if authority_error:
            raise SkillExecutionError(authority_error)
    # cli_exec skills are opaque subprocesses with no in-process fallback, so a
    # declared-but-missing binary is a structural admission failure. python_engine
    # skills run in-process and own their degradation (e.g. research-pptx returns a
    # ``node_unavailable`` domain outcome), so we never preempt them here.
    if entry.kind == SkillKind.CLI_EXEC:
        unmet_binary = _missing_binary_action(entry)
        if unmet_binary is not None:
            return unmet_binary
    unmet_service = _service_configuration_action(entry, ctx)
    if unmet_service is not None:
        return unmet_service
    # Declared Python module deps are gated for every kind: a skill that opts in
    # via ``runtime_requirements.python_modules`` gets one actionable install
    # prompt instead of a mid-engine ImportError (skills that declare none are
    # unaffected).
    unmet_module = _missing_python_module_action(entry)
    if unmet_module is not None:
        return unmet_module
    tool_gateway = ToolGateway.from_context(ctx, event_family="skill")

    async def invoke() -> dict[str, Any]:
        return await _execute_skill_unchecked(
            entry,
            input_data,
            ctx,
            progress_callback=progress_callback,
        )

    sensitive = skill_requires_approval(entry)
    if execution_policy_covers(
        entry.name,
        input_data,
        sensitive=sensitive,
    ):
        return await tool_gateway.invoke_contract_operation(
            entry.name,
            input_data,
            invoke=invoke,
            sensitive=sensitive,
            contract=entry,
        )
    if execution_policy_active():
        return await tool_gateway.invoke_nested_operation(
            entry.name,
            input_data,
            invoke=invoke,
            sensitive=sensitive,
            contract=entry,
        )
    return await tool_gateway.invoke_operation(
        entry.name,
        input_data,
        invoke=invoke,
        sensitive=sensitive,
        contract=entry,
    )


async def _execute_skill_unchecked(
    entry: SkillEntry,
    input_data: dict[str, Any],
    ctx: ExecContext,
    *,
    progress_callback: Any = None,
) -> dict[str, Any]:
    if not entry.trusted:
        raise SkillExecutionError(
            f"skill '{entry.name}' is quarantined; review it and run "
            f"`omni skills trust {entry.name} --yes` before execution"
        )
    merged = {**ctx.base_input(), **(input_data or {})}
    await _emit_progress(progress_callback, "skill.start", 0.0, skill=entry.name, kind=entry.kind.value)
    try:
        if entry.kind == SkillKind.PYTHON_ENGINE:
            result = await _run_python_engine(entry, merged, ctx, progress_callback)
        elif entry.kind == SkillKind.CLI_EXEC:
            result = await _run_cli_exec(entry, merged, ctx)
        elif entry.kind == SkillKind.PROMPT_ONLY:
            result = await _run_prompt_skill(entry, merged, ctx, progress_callback)
        else:
            raise SkillExecutionError(f"skill '{entry.name}' kind '{entry.kind}' is not directly executable")
    except Exception:
        await _emit_progress(progress_callback, "skill.error", 1.0, skill=entry.name)
        raise
    await _emit_progress(progress_callback, "skill.done", 1.0, skill=entry.name)
    return result


async def _run_python_engine(entry, merged, ctx, progress_callback) -> dict[str, Any]:
    spec = entry.engine
    if not spec or not spec.module or not spec.class_name:
        raise SkillExecutionError(f"skill '{entry.name}' has no engine binding")
    try:
        module = _load_engine_module(
            entry,
            spec.module,
            expected_authority=_authority_for_entry(ctx, entry),
        )
        cls = getattr(module, spec.class_name)
    except (ImportError, AttributeError, OSError, SyntaxError) as exc:
        raise SkillExecutionError(f"cannot load engine {spec.module}.{spec.class_name}: {exc}") from exc

    instance = cls()
    instance.ctx = ctx
    validate = getattr(instance, "validate_params", None)
    if callable(validate):
        try:
            err = validate(arguments=merged, input_data=merged)
        except TypeError:
            err = None
        if err:
            if isinstance(err, dict):
                return {"status": "error", **err}
            return {"status": "error", "error": err}

    method = getattr(instance, spec.method or "execute")
    sig = inspect.signature(method)
    kwargs = dict(merged)
    budget = _skill_budget(
        entry, ctx,
        default=ctx.settings.skills.default_seconds,
        ceiling=ctx.settings.skills.max_python_seconds,
        knob="skills.max_python_seconds",
    )
    heartbeat = _ProgressHeartbeat()
    if "progress_callback" in sig.parameters:
        kwargs["progress_callback"] = heartbeat.wrap(progress_callback)
    # Drop kwargs the method does not accept unless it takes **kwargs.
    accepts_var_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
    if not accepts_var_kw:
        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

    meter = None
    original_llm = getattr(ctx, "llm", None)
    if original_llm is not None:
        from omni.core.llm.usage import UsageMeter, UsageTrackingLLM

        meter = UsageMeter()
        ctx.llm = UsageTrackingLLM(
            original_llm,
            meter,
            on_usage=_engine_usage_publisher(ctx, progress_callback),
        )
    call = method(**kwargs) if inspect.iscoroutinefunction(method) else asyncio.to_thread(method, **kwargs)
    try:
        result = await _await_skill_call(call, budget, heartbeat)
    except TimeoutError:
        raise budget.timeout_error(entry.name) from None
    except _SkillStalled:
        raise budget.stall_error(entry.name) from None
    finally:
        if original_llm is not None:
            ctx.llm = original_llm
        if meter is not None:
            await _record_engine_cost(entry, ctx, meter)
    if not isinstance(result, dict):
        result = {"result": _jsonify(result)}
    if meter is not None:
        usage = meter.as_usage()
        if meter.calls and usage.get("total_tokens") and "usage" not in result:
            result["usage"] = usage
    return result


async def _run_cli_exec(entry, merged, ctx) -> dict[str, Any]:
    from omni.runtime.processes import process_group_options, stop_process_tree

    spec = entry.exec_spec
    if not spec or not spec.command:
        raise SkillExecutionError(f"skill '{entry.name}' has no exec binding")
    safe_merged = _sanitize_json_surrogates(merged)
    fmt_args = []
    for a in spec.args:
        try:
            fmt_args.append(a.format(**safe_merged))
        except (KeyError, IndexError):
            fmt_args.append(a)
    import os

    if spec.env_allowlist:
        env = {k: os.environ[k] for k in spec.env_allowlist if k in os.environ}
    else:
        env = os.environ.copy()
    from omni.skills_runtime.exec_io import (
        compute_io_vars,
        confined_exec_prefix,
        input_write_roots,
        register_output_dir,
    )
    from omni.skills_runtime.sandbox import SandboxUnavailableError

    # Host-owned delivery path: same $OMNI_OUTPUT_DIR / $TMPDIR as bash/run_compute,
    # even when the skill listed a narrow env_allowlist.
    env.update(compute_io_vars(ctx))
    # JSON stdin/stdout is an explicit UTF-8 transport contract. Redirected
    # Python pipes otherwise default to a legacy code page on some Windows
    # runners, turning U+FFFD bytes (EF BF BD) into the three characters ï¿½.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    payload = _json_stdin_bytes(safe_merged)
    budget = _skill_budget(
        entry, ctx,
        default=spec.timeout_seconds,
        ceiling=ctx.settings.skills.max_cli_seconds,
        knob="skills.max_cli_seconds",
    )
    try:
        prefix = confined_exec_prefix(ctx, extra_writable=input_write_roots(safe_merged))
    except SandboxUnavailableError as exc:
        raise SkillExecutionError(
            f"skill '{entry.name}': OS sandbox required but unavailable: {exc}"
        ) from exc
    proc = await asyncio.create_subprocess_exec(
        *prefix, spec.command, *fmt_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=spec.cwd or str(ctx.working_dir or ctx.paths.invocation_cwd or ctx.paths.project_dir),
        env=env,
        **process_group_options(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload), timeout=budget.seconds)
    except TimeoutError:
        await stop_process_tree(proc, grace_seconds=0.1)
        raise budget.timeout_error(entry.name) from None
    except asyncio.CancelledError:
        await stop_process_tree(proc)
        raise
    await register_output_dir(ctx)
    if proc.returncode != 0:
        raise SkillExecutionError(
            f"skill '{entry.name}' exited {proc.returncode}: {err.decode('utf-8', 'replace')[:500]}"
        )
    text = out.decode("utf-8", "replace").strip()
    if spec.stdout_format == "json":
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {"status": "ok", "stdout": text}
    return {"status": "ok", "stdout": text}


async def _run_prompt_skill(entry, merged, ctx, progress_callback=None) -> dict[str, Any]:  # noqa: ANN001
    """Run a prompt-only skill as a focused ReAct sub-agent."""
    if ctx.llm is None:
        return {"status": "ok", "instructions": entry.load_body(), "note": "no LLM configured; returned skill body"}

    from omni.config.settings import session_compact_token_budget
    from omni.core.react_agent import ReActLoopAgent, ToolSpec
    from omni.skills_runtime.builtin_tools import build_builtin_tools
    from omni.skills_runtime.context import Tool

    tools = build_builtin_tools(ctx)
    tools.extend(_build_local_prompt_tools(entry, ctx))
    # Give the prompt sub-agent access to engine-backed sync skills too
    # (so e.g. research-ideation can call arxiv-fetch).
    if getattr(ctx, "registry", None) is not None:
        for sk in ctx.registry.list_sync_tools():
            if sk.kind in (SkillKind.PYTHON_ENGINE, SkillKind.CLI_EXEC) and sk.name != entry.name:
                tools.append(Tool(
                    ToolSpec(
                        sk.name,
                        sk.short_desc(200),
                        sk.input_schema,
                        replay_safe=sk.replay_safe,
                    ),
                    _make_skill_handler(sk, ctx),
                    sensitive=skill_requires_approval(sk),
                    input_schema=sk.input_schema,
                    output_schema=sk.output_schema,
                    replay_safe=sk.replay_safe,
                    outcome_resolver=owned_result_outcome,
                ))
    tools = _filter_prompt_tools(entry, tools)
    per_tool_limits = _tool_limits(entry)

    async def on_tool_event(phase: str, data: dict) -> None:
        tool_name = str(data.get("name", ""))
        pct = 0.15 if phase == "start" else 0.85
        # ``gateway.react_invoker()`` intentionally invokes with ``emit_events=False``:
        # ReAct owns the final call id, duration, and projected event result.
        # Forward that completed event exactly once here so prompt-only nested
        # tools are durable and auditable, not just temporary progress text.
        await gateway.emit(phase, data)
        await _emit_progress(
            progress_callback,
            f"tool.{phase}",
            pct,
            tool=tool_name,
            arguments=data.get("arguments") or {},
            error=data.get("error"),
            result=data.get("result"),
            status=data.get("status"),
            duration_ms=data.get("duration_ms"),
        )

    gateway = ToolGateway.from_context(
        ctx,
        event_family="prompt_skill",
        tools=tools,
        per_tool_limits=per_tool_limits,
        skill_name=entry.name,
    )
    invoker = gateway.react_invoker()

    skill_cfg = ctx.settings.skills
    limits: dict[str, int | float] = {
        "max_tool_calls": _bounded_int_policy(
            (entry.execution or {}).get("max_tool_calls"),
            skill_cfg.default_prompt_tool_calls,
            skill_cfg.max_prompt_tool_calls,
        ),
        "max_iterations": _bounded_int_policy(
            (entry.execution or {}).get("max_iterations"),
            skill_cfg.default_prompt_iterations,
            skill_cfg.max_prompt_iterations,
        ),
        # A delegated sub-agent that declares nothing gets the same conservative
        # fallback as any other skill; declaring ``execution.max_seconds`` is how
        # it asks for the headroom its ceiling already permits.
        "max_seconds": _skill_budget(
            entry, ctx,
            default=skill_cfg.default_seconds,
            ceiling=skill_cfg.max_prompt_seconds,
            knob="skills.max_prompt_seconds",
        ).seconds,
    }
    max_tool_calls = int(limits["max_tool_calls"])
    soft_limit, keep_results = _prompt_compaction_limits(ctx)
    react = ReActLoopAgent(
        ctx.llm, invoker,
        max_iterations=int(limits["max_iterations"]),
        max_tool_calls=max_tool_calls,
        max_seconds=float(limits["max_seconds"]),
        shared_tool_budget=getattr(ctx, "tool_budget", None),
        finalization_timeout_s=ctx.settings.react.finalization_timeout_s,
        finalization_attempts=ctx.settings.react.finalization_attempts,
        max_tokens=resolve_max_output_tokens(ctx.settings.model),
        # A high-budget prompt sub-agent (e.g. a long agent-goal / systematic
        # review) can accumulate many large tool observations. Shrink the oldest
        # ones once the transcript passes the model-window budget so a long run
        # keeps making progress instead of starving its own context — the same
        # cheap first-tier microcompaction the coordinator loop uses.
        soft_token_limit=soft_limit,
        context_rollover_token_limit=session_compact_token_budget(ctx.settings),
        microcompact_keep_tool_results=keep_results,
        observation_max_chars=int(
            getattr(getattr(ctx.settings, "memory", None), "tool_observation_max_chars", 8000) or 0
        ),
        # A prompt sub-agent keeps the *structured* clarification contract the
        # workflow layer depends on: a ``needs_input`` terminal stays the tool's
        # typed payload rather than being recomposed as prose.
        compose_needs_input=False,
        stall_timeout_s=float(
            getattr(getattr(ctx.settings, "react", None), "stall_timeout_s", 0.0) or 0.0
        ),
        **_prompt_usage_limits(ctx),
    )
    system = (
        (ctx.settings.role or "You are a rigorous research assistant. Answer in the language of the current user turn.")
        + _prompt_skill_runtime_context(entry)
        + "\n\nSkill instructions:\n"
        + entry.load_body()
        + _prompt_budget_note(entry, limits)
    )
    user = _prompt_skill_user_message(merged)
    result = await react.run(
        system_prompt=system, user_message=user, tools=[t.spec for t in tools],
        on_tool_event=on_tool_event,
    )
    await _record_prompt_skill_cost(entry, ctx, result, system=system, user=user)
    salvage_text = await _salvage_prompt_result(entry, merged, result, ctx)
    return _prompt_skill_result(entry, result, salvage_text=salvage_text)


def _prompt_usage_limits(ctx: Any) -> dict[str, int | float]:
    from omni.agent.cost import react_usage_limits

    return react_usage_limits(ctx.settings, ctx.llm)


def _prompt_compaction_limits(ctx: Any) -> tuple[int, int]:
    """Model-window soft budget + tool-results-to-keep for prompt-skill microcompaction.

    Mirrors the coordinator loop: the *cheap* tier's threshold
    (``window × microcompact_pct``) plus ``memory.microcompact_keep_tool_results``.
    Trimming an observation is what this budget buys, so it must not be read off
    the expensive tier's percentage. Returns ``(0, 0)`` when either is
    disabled/unresolvable so the loop leaves the transcript untouched.
    """
    try:
        from omni.config.settings import microcompact_token_budget

        soft = max(0, microcompact_token_budget(ctx.settings))
        keep = int(getattr(ctx.settings.memory, "microcompact_keep_tool_results", 0) or 0)
    except Exception:  # noqa: BLE001 - compaction is an optimisation, never fatal
        return 0, 0
    return soft, keep


_USAGE_PROGRESS_INTERVAL_S = 2.0
_USAGE_PROGRESS_TOKEN_STEP = 10_000


def _engine_usage_publisher(ctx: Any, progress_callback: Any) -> Any:
    """Throttle engine usage snapshots onto the skill progress channel."""
    last = {"t": 0.0, "tokens": 0}

    async def _publish(snapshot: dict[str, Any]) -> None:
        tokens = int(snapshot.get("total_tokens") or 0)
        if tokens <= 0:
            return
        now = time.monotonic()
        first = last["t"] == 0.0
        jumped = tokens - int(last["tokens"]) >= _USAGE_PROGRESS_TOKEN_STEP
        due = (now - last["t"]) >= _USAGE_PROGRESS_INTERVAL_S
        if not (first or due or jumped):
            return
        last["t"] = now
        last["tokens"] = tokens
        payload = dict(snapshot)
        try:
            from omni.agent.cost import estimate_cost

            model = getattr(ctx.llm, "model", "") or getattr(ctx.settings.model, "model", "")
            payload["cost_usd"] = estimate_cost(
                model, snapshot, cost_cfg=getattr(ctx.settings, "cost", None)
            ).cost_usd
        except Exception:  # noqa: BLE001 - a missing price must not hide the token count
            pass
        await _emit_progress(progress_callback, "usage", 0.0, **payload)

    return _publish


async def _record_engine_cost(entry, ctx, meter) -> None:  # noqa: ANN001
    """Write one ``cost.usage`` event for every real model call the engine made."""
    task_id = str(getattr(ctx, "task_id", "") or "")
    if not task_id or meter.calls <= 0:
        return
    tasks = getattr(ctx, "task_recorder", None)
    if tasks is None:
        db = getattr(ctx, "db", None)
        if db is None:
            return
        from omni.runtime.task_recorder import TaskRecorder

        tasks = TaskRecorder(db, project=getattr(ctx, "project", "default") or "default")
    from omni.agent.cost import record_usage_cost_event

    await record_usage_cost_event(
        tasks,
        ctx.settings,
        getattr(ctx, "llm", None),
        task_id,
        meter.as_usage(),
        component=f"engine:{entry.name}",
        estimated=meter.estimated,
        calls=meter.calls,
    )


async def _record_prompt_skill_cost(entry, ctx, result, *, system: str, user: str) -> None:  # noqa: ANN001
    db = getattr(ctx, "db", None)
    task_id = str(getattr(ctx, "task_id", "") or "")
    if db is None or not task_id:
        return
    from omni.agent.cost import record_cost_event
    from omni.runtime.task_recorder import TaskRecorder

    await record_cost_event(
        TaskRecorder(db, project=getattr(ctx, "project", "default") or "default"),
        ctx.settings,
        ctx.llm,
        task_id,
        result,
        system=system,
        user_message=user,
        component=f"prompt_skill:{entry.name}",
    )
