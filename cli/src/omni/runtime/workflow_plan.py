"""Workflow plan normalization and input-contract policy."""

from __future__ import annotations

from typing import Any

from omni.agent.provider_binding import materialize_step_provider_binding
from omni.core.tool_contracts import (
    ProviderInputCompiler,
    provider_schema_definition_errors,
    skill_input_contract_error,
)
from omni.skills_runtime.registry import (
    SKILL_SOURCE_PARAM,
    SkillRegistry,
    resolve_step_entry,
    step_skill_source,
)

WORKFLOW_SKILL_NAME = "workflow"
CHILD_TASK_PROVIDER = "child_task"


class WorkflowNeedsInput(ValueError):
    """Workflow is under-specified and should not be persisted yet."""

    def __init__(self, missing: list[dict[str, Any]]) -> None:
        self.missing = missing
        super().__init__(_needs_input_message(missing))


def prepare_workflow_plan(
    goal: str,
    raw_steps: Any,
    registry: SkillRegistry,
    *,
    seal_provider_bindings: bool = True,
) -> list[dict[str, Any]]:
    """Materialize the exact idempotent workflow shape persisted at runtime.

    Planning calls this before sealing its authoritative revision; runtime calls
    the same implementation again as a defensive check.  The second call must
    therefore be value-identical rather than silently rewriting an accepted
    plan after its hash was approved. ``seal_provider_bindings=False`` is only
    for read-only validation of an already accepted v1 revision; direct runtime
    ingress keeps the safe default and seals the derived execution steps.
    """
    return _prepare_workflow_plan(
        goal,
        raw_steps,
        registry,
        seal_provider_bindings=seal_provider_bindings,
    )


def _normalise_workflow_plan(
    goal: str, steps: list[dict[str, Any]], registry: SkillRegistry
) -> list[dict[str, Any]]:
    """Preserve the providers selected by the validated intent plan.

    Capability arbitration happens before the workflow is persisted. Runtime
    preparation may validate inputs, but it must never reinterpret user text or
    replace a contracted provider.
    """
    del goal, registry
    return [dict(step) for step in steps]


def _is_native_workflow_step(step: dict[str, Any]) -> bool:
    provider = str(step.get("provider_type") or step.get("provider") or "").strip().lower()
    capability = (
        str(step.get("capability") or step.get("skill_name") or step.get("skill") or "")
        .strip()
        .lower()
    )
    return provider == "native_executor" or capability in {
        "synthesis.final",
        "draft.section",
        "draft.manuscript",
    }


def _is_child_task_step(step: dict[str, Any]) -> bool:
    """Return whether this node delegates a new agent task instead of a skill."""
    provider = str(step.get("provider_type") or step.get("provider") or "").strip().lower()
    skill_name = str(step.get("skill_name") or step.get("skill") or "").strip().lower()
    return (
        provider in {CHILD_TASK_PROVIDER, "subagent", "agent"} or skill_name == WORKFLOW_SKILL_NAME
    )


def _prepare_workflow_plan(
    goal: str,
    raw_steps: Any,
    registry: SkillRegistry,
    *,
    seal_provider_bindings: bool = True,
) -> list[dict[str, Any]]:
    normalised = _normalise_workflow_plan(
        goal, _normalise_workflow_steps(raw_steps, registry), registry
    )
    terminal_support_step_ids = _terminal_support_step_ids(normalised, registry)
    prepared: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    compiler = ProviderInputCompiler()
    for step in normalised:
        next_step = dict(step)
        entry = resolve_step_entry(registry, next_step)
        if _is_child_task_step(next_step):
            next_step["skill_name"] = ""
            next_step["provider_type"] = CHILD_TASK_PROVIDER
            next_step.setdefault("capability", "agent.delegate")
            next_step.setdefault("failure_policy", "continue_with_partial")
            prepared.append(next_step)
            continue
        if _is_native_workflow_step(next_step):
            next_step["skill_name"] = ""
            next_step["provider_type"] = "native_executor"
            next_step.setdefault("capability", "synthesis.final")
            next_step.setdefault("failure_policy", "continue_with_partial")
            next_step.setdefault("allow_failed_dependencies", True)
            if seal_provider_bindings:
                materialize_step_provider_binding(next_step, None)
            prepared.append(next_step)
            continue
        if str(next_step.get("id") or "") in terminal_support_step_ids:
            missing.append(
                {
                    "step_id": next_step.get("id", ""),
                    "skill_name": next_step.get("skill_name", ""),
                    "missing": ["downstream_task_skill"],
                    "provided": sorted(str(key) for key in next_step),
                    "reason": "support skill cannot be the terminal deliverable in a multi-step workflow",
                }
            )
            prepared.append(next_step)
            continue
        if entry is None:
            missing.append(
                {
                    "step_id": next_step.get("id", ""),
                    "skill_name": next_step.get("skill_name", ""),
                    "missing": ["provider"],
                    "provided": [],
                    "reason": "the validated workflow provider is not installed",
                }
            )
            prepared.append(next_step)
            continue
        schema_errors = provider_schema_definition_errors(entry)
        if schema_errors:
            missing.extend(
                {
                    "step_id": next_step.get("id", ""),
                    "skill_name": next_step.get("skill_name", ""),
                    "missing": ["provider_schema"],
                    "provided": [],
                    "reason": str(error.get("message") or "provider schema is invalid"),
                    "label": str(error.get("schema_field") or "provider_schema"),
                }
                for error in schema_errors
            )
            prepared.append(next_step)
            continue
        _apply_entry_workflow_policy(next_step, entry)
        if seal_provider_bindings:
            materialize_step_provider_binding(next_step, entry)
        if not next_step.get("input_compiled"):
            compiled = compiler.compile_entry(
                entry,
                semantic_input=dict(next_step.get("input") or {}),
                raw_message="",
            )
            next_step["input"] = dict(compiled.arguments)
            next_step["input_compiled"] = True
            if compiled.errors:
                missing.extend(
                    {
                        "step_id": next_step.get("id", ""),
                        "skill_name": next_step.get("skill_name", ""),
                        "missing": list(error.get("missing") or []),
                        "provided": sorted(str(key) for key in compiled.arguments),
                        "reason": str(error.get("reason") or ""),
                        "label": str(error.get("label") or ""),
                    }
                    for error in compiled.errors
                )
                prepared.append(next_step)
                continue
        contract_error = skill_input_contract_error(entry, dict(next_step.get("input") or {}))
        if contract_error:
            missing.append(
                {
                    "step_id": next_step.get("id", ""),
                    "skill_name": next_step.get("skill_name", ""),
                    "missing": list(contract_error.get("missing") or []),
                    "provided": sorted(str(key) for key in next_step.get("input") or {}),
                    "reason": str(contract_error.get("reason") or ""),
                    "label": str(contract_error.get("label") or ""),
                }
            )
            prepared.append(next_step)
            continue
        prepared.append(next_step)
    if missing:
        raise WorkflowNeedsInput(missing)
    return prepared


def _terminal_support_step_ids(steps: list[dict[str, Any]], registry: SkillRegistry) -> set[str]:
    if len(steps) <= 1:
        return set()
    dependents = {str(dep) for step in steps for dep in (step.get("depends_on") or [])}
    terminal: list[tuple[str, str]] = []
    for step in steps:
        step_id = str(step.get("id") or "")
        if not step_id or step_id in dependents:
            continue
        if _is_native_workflow_step(step) or _is_child_task_step(step):
            terminal.append((step_id, "task"))
            continue
        entry = resolve_step_entry(registry, step)
        terminal.append((step_id, entry.skill_role if entry is not None else ""))
    if any(role == "task" for _, role in terminal):
        return set()
    return {step_id for step_id, role in terminal if role == "support"}


def _apply_entry_workflow_policy(step: dict[str, Any], entry: Any) -> None:
    workflow = getattr(entry, "workflow", None)
    if not isinstance(workflow, dict):
        return
    if not step.get("failure_policy"):
        policy = _normalise_failure_policy(
            workflow.get("failure_policy") or workflow.get("on_failure")
        )
        if policy:
            step["failure_policy"] = policy
    if (
        workflow.get("allow_failed_dependencies") is True
        or workflow.get("continue_on_dependency_failure") is True
    ):
        step["allow_failed_dependencies"] = True


def _normalise_workflow_steps(
    raw_steps: Any, registry: SkillRegistry | None = None
) -> list[dict[str, Any]]:
    if not isinstance(raw_steps, list):
        raise ValueError("workflow steps must be a list")
    steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"workflow step {idx} must be an object")
        capability = str(raw.get("capability") or "").strip()
        provider_type = str(raw.get("provider_type") or raw.get("provider") or "").strip()
        skill_name = str(raw.get("skill_name") or raw.get("skill") or "").strip()
        child_task = (
            provider_type.lower() in {CHILD_TASK_PROVIDER, "subagent", "agent"}
            or skill_name == WORKFLOW_SKILL_NAME
        )
        if child_task:
            provider_type = CHILD_TASK_PROVIDER
            skill_name = ""
            capability = capability or "agent.delegate"
        native_step = provider_type == "native_executor" or capability in {
            "synthesis.final",
            "draft.section",
            "draft.manuscript",
        }
        if native_step:
            provider_type = "native_executor"
            skill_name = ""
            capability = capability or "synthesis.final"
        if not skill_name and capability and registry is not None:
            # A step may name what it needs rather than who provides it. Picking
            # the provider here — at the tool boundary, against the live registry
            # — is what the planner used to do before it was deleted; doing it
            # any earlier would just be guessing before the work starts.
            entry, _ = registry.resolve_capability(capability)
            if entry is not None:
                skill_name = entry.name
        if not skill_name and not child_task and not native_step:
            raise ValueError(
                f"workflow step {idx} is missing skill"
                + (f" and no provider offers capability '{capability}'" if capability else "")
            )
        step_id = str(raw.get("id") or f"step_{idx}").strip()
        if step_id in seen:
            raise ValueError(f"duplicate workflow step id '{step_id}'")
        seen.add(step_id)
        params = raw.get("input", raw.get("parameters", {}))
        if isinstance(params, str):
            params = {"input": params}
        if not isinstance(params, dict):
            raise ValueError(f"workflow step {step_id} input must be an object or string")
        params = dict(params)
        skill_source = step_skill_source({**raw, "input": params})
        params.pop(SKILL_SOURCE_PARAM, None)
        depends_on = raw.get("depends_on") or raw.get("depends") or []
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        if not isinstance(depends_on, list):
            raise ValueError(f"workflow step {step_id} depends_on must be a list")
        optional_depends_on = raw.get("optional_depends_on") or raw.get("optional_depends") or []
        if isinstance(optional_depends_on, str):
            optional_depends_on = [optional_depends_on]
        if not isinstance(optional_depends_on, list):
            raise ValueError(f"workflow step {step_id} optional_depends_on must be a list")
        normalised = {
            "id": step_id,
            "skill_name": skill_name,
            "input": params,
            "depends_on": [str(dep) for dep in depends_on],
            "optional_depends_on": [str(dep) for dep in optional_depends_on],
        }
        if skill_source:
            normalised["skill_source"] = skill_source
        if capability:
            normalised["capability"] = capability
        if provider_type:
            normalised["provider_type"] = provider_type
        if raw.get("deliverable"):
            normalised["deliverable"] = str(raw.get("deliverable"))
        normalised["required"] = raw.get("required", True) is not False
        policy = _normalise_failure_policy(raw.get("failure_policy") or raw.get("on_failure"))
        if policy:
            normalised["failure_policy"] = policy
        if raw.get("continue_on_error") is True:
            normalised["failure_policy"] = "continue_with_partial"
        if raw.get("continue_on_dependency_failure") is True:
            normalised["allow_failed_dependencies"] = True
        if raw.get("planned_skill_name"):
            normalised["planned_skill_name"] = str(raw.get("planned_skill_name"))
        if raw.get("normalization_reason"):
            normalised["normalization_reason"] = str(raw.get("normalization_reason"))
        if isinstance(raw.get("capability_resolution"), dict):
            normalised["capability_resolution"] = dict(raw["capability_resolution"])
        # Exact provider/quality identity is sealed before workflow
        # materialization and must survive the runtime's idempotent normalizer.
        for field_name in (
            "provider_binding_id",
            "provider_contract_hash",
            "provider_name",
            "provider_source",
            "provider_version",
            "deliverable_id",
        ):
            if raw.get(field_name):
                normalised[field_name] = str(raw[field_name])
        if raw.get("input_compiled") is True:
            normalised["input_compiled"] = True
        if raw.get("fallback_skill"):
            normalised["fallback_skill"] = str(raw.get("fallback_skill"))
        steps.append(normalised)
    return steps


def _normalise_failure_policy(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "continue": "continue_with_partial",
        "warn": "continue_with_partial",
        "warning": "continue_with_partial",
        "continue_on_error": "continue_with_partial",
        "continue_with_warning": "continue_with_partial",
        "partial": "continue_with_partial",
        "partial_success": "continue_with_partial",
        "fail": "fail_fast",
        "fail_fast": "fail_fast",
        "block": "fail_fast",
        "required": "fail_fast",
    }
    return aliases.get(raw, raw if raw in {"continue_with_partial", "fail_fast"} else "")


def _step_failure_policy(step: dict[str, Any], entry: Any) -> str:
    policy = _normalise_failure_policy(step.get("failure_policy"))
    if policy:
        return policy
    workflow = getattr(entry, "workflow", None)
    if isinstance(workflow, dict):
        return _normalise_failure_policy(
            workflow.get("failure_policy") or workflow.get("on_failure")
        )
    return ""


def _step_failure_recoverable(step: dict[str, Any], entry: Any, result: dict[str, Any]) -> bool:
    if result.get("blocking") is True or isinstance(result.get("action_required"), dict):
        return False
    if _step_failure_policy(step, entry) == "continue_with_partial":
        return True
    info = result.get("error_info")
    if isinstance(info, dict) and info.get("workflow_recoverable") is True:
        return True
    return result.get("recoverable") is True and result.get("blocking") is not True


def _step_allows_failed_dependencies(step: dict[str, Any], entry: Any) -> bool:
    if step.get("allow_failed_dependencies") is True:
        return True
    workflow = getattr(entry, "workflow", None)
    if isinstance(workflow, dict):
        return (
            workflow.get("allow_failed_dependencies") is True
            or workflow.get("continue_on_dependency_failure") is True
        )
    return False


def _workflow_step_input(
    step: dict[str, Any],
    goal: str,
    results_by_id: dict[str, Any],
) -> dict[str, Any]:
    depends_on = list(step.get("depends_on") or [])
    optional_depends_on = list(step.get("optional_depends_on") or [])
    selected_ids = [*depends_on, *optional_depends_on]
    selected = {sid: results_by_id[sid] for sid in selected_ids if sid in results_by_id}
    dependency_failures = {
        sid: value
        for sid, value in selected.items()
        if isinstance(value, dict)
        and str(value.get("status", "")).lower()
        in {"error", "failed", "skipped", "partial", "degraded", "warning"}
    }
    return {
        **dict(step.get("input") or {}),
        "workflow_goal": goal,
        "workflow_step_id": step.get("id", ""),
        "workflow_results": dict(results_by_id),
        "depends_on_results": selected,
        "dependency_failures": dependency_failures,
    }


def _needs_input_message(missing: list[dict[str, Any]]) -> str:
    parts = []
    for item in missing:
        fields = ", ".join(str(field) for field in item.get("missing") or [])
        parts.append(f"{item.get('step_id')}({item.get('skill_name')}): {fields}")
    return "workflow needs more input: " + "; ".join(parts)
