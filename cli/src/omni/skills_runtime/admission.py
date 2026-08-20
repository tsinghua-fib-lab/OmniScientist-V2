"""Host-known skill admission: a route fact, not a turn stop.

A skill that cannot start (missing host service, binary, or Python module) is
an observation. Codex treats failed tools the same way: feed the result back
and keep looping. Conversational ``action_required`` values that ask the user
to confirm something (``action: confirm_*``) stay a suspend.

Preflight still refuses to start work that cannot run. Callers decide whether
that refusal ends the turn; this module only names the fact.
"""

from __future__ import annotations

import shutil
import sys
from typing import Any

from omni.skills_runtime.manifest import SkillEntry, SkillKind

# ``python``/``python3`` are satisfied by the interpreter already running omni,
# so a declared interpreter requirement never blocks on PATH-name quirks.
_INTERPRETER_ALIASES = frozenset({"python", "python3"})

_SERVICE_LABELS = {
    "vlm": "vision model (VLM)",
}

# Map a declared service name to the object on the exec context that reports
# readiness (``available`` / ``error_code`` / ``missing`` / ``setup_command``).
_SERVICE_PROBES: dict[str, Any] = {
    "vlm": lambda ctx: getattr(ctx, "vlm", None),
}


def is_admission_action(action: Any) -> bool:
    """True for owner-lifecycle gates, not a conversational confirm."""
    if not isinstance(action, dict):
        return False
    kind = str(action.get("kind") or "").lower()
    if kind == "install":
        return True
    if kind != "configure":
        return False
    # Soulagent distillation and similar confirms ask the user a question.
    return not str(action.get("action") or "").strip()


def is_admission_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return is_admission_action(result.get("action_required"))


def first_admission_result(value: Any) -> dict[str, Any] | None:
    """Return the first nested mapping that carries an admission action."""

    def visit(current: Any) -> dict[str, Any] | None:
        if isinstance(current, dict):
            if is_admission_result(current):
                return current
            for nested in current.values():
                found = visit(nested)
                if found is not None:
                    return found
        elif isinstance(current, (list, tuple)):
            for nested in current:
                found = visit(nested)
                if found is not None:
                    return found
        return None

    return visit(value)


def service_label(service: str) -> str:
    key = str(service or "").strip().lower()
    return _SERVICE_LABELS.get(key, key or "required service")


def host_services_from_ctx(ctx: Any) -> dict[str, Any]:
    """Probe objects already injected on an exec context."""
    services: dict[str, Any] = {}
    for name, probe in _SERVICE_PROBES.items():
        try:
            services[name] = probe(ctx)
        except Exception:  # noqa: BLE001 — a probe must never fail admission closed
            services[name] = None
    return services


def skill_admission_rejection(
    entry: SkillEntry,
    *,
    services: dict[str, Any] | None = None,
    ctx: Any | None = None,
) -> dict[str, Any] | None:
    """First host-known reason this skill cannot start, or ``None``.

    Binary gates apply to ``cli_exec`` only (same as the executor). Module and
    service gates apply to every kind.
    """
    if entry.kind == SkillKind.CLI_EXEC:
        unmet_binary = binary_admission(entry)
        if unmet_binary is not None:
            return unmet_binary
    probed = services if services is not None else host_services_from_ctx(ctx)
    unmet_service = service_admission(entry, probed)
    if unmet_service is not None:
        return unmet_service
    return module_admission(entry)


def service_admission(
    entry: SkillEntry,
    services: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Reject when a declared host service is present and not configured."""
    for service in entry.requires_services:
        gateway = (services or {}).get(service)
        if gateway is None or getattr(gateway, "available", False):
            continue
        command = str(getattr(gateway, "setup_command", "") or "")
        code = str(getattr(gateway, "error_code", "") or f"{service}_not_configured")
        missing = [str(field) for field in getattr(gateway, "missing", ()) or ()]
        label = service_label(service)
        error = f"{label} is not configured."
        if command:
            error += f" Run `{command}`."
        error += f" Do not retry {entry.name} until it is configured."
        return _admission_payload(
            entry,
            summary=f"{entry.name} cannot run: {label} is not configured.",
            error=error,
            action={
                "kind": "configure",
                "service": service,
                "command": command,
                "missing": missing,
            },
            command=command,
            code=code,
        )
    return None


def service_admission_from_ctx(entry: SkillEntry, ctx: Any) -> dict[str, Any] | None:
    return service_admission(entry, host_services_from_ctx(ctx))


def binary_admission(entry: SkillEntry) -> dict[str, Any] | None:
    """Reject a ``cli_exec`` skill whose declared binaries are not on PATH."""
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
    return _admission_payload(
        entry,
        summary=f"{entry.name} needs {joined} on PATH first.",
        error=f"Required executable(s) not found: {joined}.",
        action={"kind": "install", "bins": missing},
        command="",
        code="missing_binary",
    )


def module_admission(entry: SkillEntry) -> dict[str, Any] | None:
    """Reject when declared Python modules are not importable in omni's interpreter."""
    from omni.skills_runtime.manifest import missing_python_modules

    missing = missing_python_modules(entry)
    if not missing:
        return None
    joined = ", ".join(missing)
    command = str(entry.dependency_setup_command or "").strip()
    error = f"Required Python module(s) not importable in omni's interpreter: {joined}."
    if command:
        error += f" Install with: {command}"
    return _admission_payload(
        entry,
        summary=f"{entry.name} needs Python module(s) installed first: {joined}.",
        error=error,
        action={
            "kind": "install",
            "python_modules": missing,
            "command": command,
        },
        command=command,
        code=entry.dependency_error_code or "runtime_dependency_missing",
    )


def admission_fallthrough_lines(skill: str, result: dict[str, Any]) -> list[str]:
    """Extra fallthrough lines so ReAct does not retry a sealed unrunnable skill."""
    action = result.get("action_required") if isinstance(result.get("action_required"), dict) else {}
    command = str(
        (action or {}).get("command") or result.get("setup_command") or ""
    ).strip()
    error_info = result.get("error_info") if isinstance(result.get("error_info"), dict) else {}
    code = str((error_info or {}).get("code") or "").strip()
    parts = [f"  admission rejected (`{skill}`)"]
    if code:
        parts[0] += f": {code}"
    lines = [parts[0]]
    if command:
        lines.append(f"  setup: `{command}`")
    lines.append(f"  do not retry `{skill}` until that setup succeeds")
    return lines


def _admission_payload(
    entry: SkillEntry,
    *,
    summary: str,
    error: str,
    action: dict[str, Any],
    command: str,
    code: str,
) -> dict[str, Any]:
    return {
        "status": "error",
        "summary": summary,
        "error": error,
        "recoverable": False,
        "blocking": True,
        "do_not_retry": True,
        "action_required": action,
        "setup_command": command,
        "error_info": {
            "code": code,
            "category": "configuration",
            "retryable": False,
            "workflow_recoverable": False,
        },
    }
