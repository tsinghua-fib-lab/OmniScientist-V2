"""Validation gate for IntentPlan objects.

The planner proposes; the runtime validates. This keeps ReAct from becoming the
implicit validator and makes missing contracts visible before any tool loop
starts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.provider_binding import materialize_provider_bindings
from omni.agent.resolver_evidence import validate_resolver_evidence
from omni.core.tool_contracts import (
    ProviderInputCompiler,
    input_contract_binding_owner,
    input_contract_repair_capability,
    instruction_raw_message,
    provider_schema_definition_errors,
    skill_input_contract_error,
)
from omni.skills_runtime.registry import (
    SKILL_SOURCE_PARAM,
    SkillRegistry,
    resolve_step_entry,
    step_skill_source,
)

# Severity drives the recovery ladder (see ``plan_recovery``):
#   safety      → Rung 0 hard stop; never swallowed by degradation.
#   blocking    → not directly executable, but recoverable (repair/ask/ReAct).
#   degraded    → executable with a warning (mirrors runtime partial policy).
SEVERITY_SAFETY = "safety"
SEVERITY_BLOCKING = "blocking"
SEVERITY_DEGRADED = "degraded"

# Codes whose message is an *engine self-heal* signal, not user-facing text. Their
# message (e.g. a skill ``missing_message`` such as "A concrete arXiv id or URL is
# required…") drives the recovery ladder / ReAct context, but must never surface to
# the user: the agent looks the value up before asking or erroring. See the
# "look-up before ask/error" invariant.
_SELF_HEAL_CODES = frozenset({"step_input_contract", "provider_input_contract"})

# Contract-definition failures belong to the provider and cannot be repaired by
# changing an invocation. Model repair is limited to an explicit set of
# instance-validation keywords for which replacing or adding the reported
# provider input path can produce a valid candidate.
_PROVIDER_SCHEMA_DEFINITION_KEYWORDS = frozenset(
    {"invalid_schema", "external_ref", "unresolved_ref"}
)
_MODEL_REPAIRABLE_INSTANCE_KEYWORDS = frozenset(
    {
        "anyOf",
        "const",
        "contains",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxContains",
        "maximum",
        "maxItems",
        "maxLength",
        "maxProperties",
        "minContains",
        "minimum",
        "minItems",
        "minLength",
        "minProperties",
        "multipleOf",
        "not",
        "oneOf",
        "pattern",
        "required",
        "type",
        "uniqueItems",
    }
)


@dataclass(slots=True)
class PlanFinding:
    """A single, classified validation result.

    Findings are the source of truth the recovery ladder routes on; the string
    lists (``errors``/``warnings``/``degraded_warnings``) are kept as derived,
    backward-compatible views for existing consumers and presentation.
    """

    code: str
    message: str
    severity: str = SEVERITY_BLOCKING
    scope: str = "plan"  # "plan" | "step"
    step_id: str = ""
    skill_name: str = ""
    capability: str = ""
    missing_field: str = ""
    repairable: bool = False
    repair_capability: str = ""
    # Stable, machine-readable identity and semantic binding context.  The
    # original fields above remain unchanged for backward compatibility with
    # the recovery ladder; these fields let revisions and model patches refer
    # to a finding without relying on presentation text.
    finding_id: str = ""
    constraint_id: str = ""
    field_path: str = ""
    actual: Any = None
    expected: Any = None
    owner: str = ""
    evidence: str = ""
    repair_strategy: str = ""
    deliverable_id: str = ""
    capability_instance: str = ""
    provider_binding_id: str = ""
    provider_source: str = ""
    provider_contract_hash: str = ""
    schema_keyword: str = ""
    allowed_values: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.finding_id:
            return
        identity = {
            "code": self.code,
            "scope": self.scope,
            "step_id": self.step_id,
            "skill_name": self.skill_name,
            "capability": self.capability,
            "constraint_id": self.constraint_id,
            "field_path": self.field_path or self.missing_field,
            "owner": self.owner,
            "deliverable_id": self.deliverable_id,
            "capability_instance": self.capability_instance,
            "provider_binding_id": self.provider_binding_id,
            "provider_source": self.provider_source,
            "provider_contract_hash": self.provider_contract_hash,
            "schema_keyword": self.schema_keyword,
        }
        encoded = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="backslashreplace")
        self.finding_id = "finding-" + hashlib.sha256(encoded).hexdigest()[:20]

    def to_dict(self) -> dict[str, Any]:
        """Return the complete audit-safe finding projection."""
        return asdict(self)


@dataclass(slots=True)
class PlanValidationResult:
    status: str = "validated"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    degraded_warnings: list[str] = field(default_factory=list)
    findings: list[PlanFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"validated", "degraded"}

    @property
    def has_safety_finding(self) -> bool:
        return any(f.severity == SEVERITY_SAFETY for f in self.findings)

    @property
    def recoverable_only(self) -> bool:
        """True when the plan is not ok but nothing is a safety violation."""
        return not self.ok and not self.has_safety_finding

    @property
    def _self_heal_messages(self) -> set[str]:
        """Finding messages that are engine self-heal signals, not user text."""
        return {f.message for f in self.findings if f.code in _SELF_HEAL_CODES}

    @property
    def display_warnings(self) -> list[str]:
        """Plain warnings safe to show the user (self-heal contract hints excluded)."""
        blocked = self._self_heal_messages
        return [w for w in self.warnings if w not in blocked]

    @property
    def display_degraded_warnings(self) -> list[str]:
        """Degraded warnings safe to show the user (self-heal contract hints excluded).

        The full ``degraded_warnings`` list stays intact for status derivation and
        the recovery ladder; only user-facing surfaces read this filtered view.
        """
        blocked = self._self_heal_messages
        return [w for w in self.degraded_warnings if w not in blocked]

    def add(self, finding: PlanFinding) -> None:
        if any(existing.finding_id == finding.finding_id for existing in self.findings):
            return
        self.findings.append(finding)
        if finding.severity in {SEVERITY_SAFETY, SEVERITY_BLOCKING}:
            self.errors.append(finding.message)
        elif finding.severity == SEVERITY_DEGRADED:
            self.degraded_warnings.append(finding.message)
        else:
            self.warnings.append(finding.message)

    def error(self, code: str, message: str, **kwargs: object) -> None:
        self.add(PlanFinding(code=code, message=message, severity=SEVERITY_BLOCKING, **kwargs))  # type: ignore[arg-type]

    def safety(self, code: str, message: str, **kwargs: object) -> None:
        self.add(PlanFinding(code=code, message=message, severity=SEVERITY_SAFETY, **kwargs))  # type: ignore[arg-type]

    def degrade(self, code: str, message: str, **kwargs: object) -> None:
        self.add(PlanFinding(code=code, message=message, severity=SEVERITY_DEGRADED, **kwargs))  # type: ignore[arg-type]

    def warn(self, message: str) -> None:
        self.warnings.append(message)


class PlanValidator:
    """Validate structural executability and contract completeness."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry
        self._input_compiler = ProviderInputCompiler()

    def validate(self, plan: IntentPlan) -> PlanValidationResult:
        result = PlanValidationResult()
        # Resolve and seal the exact provider source + contract before any
        # compiler or resolver-evidence decision.
        materialize_provider_bindings(plan, self._registry)
        self._compile_provider_inputs(plan)
        self._validate_compilation_errors(plan, result)
        for finding in validate_resolver_evidence(plan, self._registry):
            result.add(finding)
        if not plan.task_id:
            result.error("plan_task_id_empty", "plan.task_id is empty")
        if not plan.user_message.strip():
            result.error("plan_user_message_empty", "plan.user_message is empty")

        if plan.intent_type in {IntentType.QA_PLUS_ARTIFACT, IntentType.SINGLE_SKILL_TASK}:
            if not plan.selected_skills:
                result.error(
                    "missing_selected_skills",
                    f"{plan.intent_type.value} requires selected_skills",
                )
            for selection in plan.selected_skills:
                self._validate_skill(
                    selection.skill,
                    result,
                    explicit=selection.selection_source == "explicit",
                    source=getattr(selection, "skill_source", ""),
                )

        if plan.intent_type == IntentType.WORKFLOW:
            if not plan.workflow_steps:
                result.error("empty_workflow_steps", "workflow plan requires workflow_steps")
            self._validate_workflow_steps(plan, result)

        if plan.intent_type == IntentType.NEEDS_INPUT and not plan.missing_inputs:
            result.error("missing_missing_inputs", "needs_input plan requires missing_inputs")

        self._validate_tool_policy(plan, result)
        self._validate_verification_plan(plan, result)

        # Only user-safe warnings reach the plan (and thus the CLI / inbox):
        # contract self-heal messages stay on ``result.findings`` for recovery.
        plan.validation_warnings = [*plan.validation_warnings, *result.display_warnings]
        plan.degraded_warnings = [*plan.degraded_warnings, *result.display_degraded_warnings]
        if result.errors:
            result.status = "rejected"
        elif result.degraded_warnings:
            result.status = "degraded"
        return result

    def _compile_provider_inputs(self, plan: IntentPlan) -> None:
        """Seal provider arguments once, before structural validation."""
        if plan.inputs_compiled:
            return
        errors: list[dict[str, object]] = []
        provider_inputs: dict[str, dict] = {}
        if plan.intent_type in {
            IntentType.QA_PLUS_ARTIFACT,
            IntentType.SINGLE_SKILL_TASK,
        }:
            for selection in plan.selected_skills:
                skill_source = str(getattr(selection, "skill_source", "") or "")
                entry = resolve_step_entry(
                    self._registry,
                    {
                        "skill_name": selection.skill,
                        "skill_source": skill_source,
                    },
                )
                if entry is None:
                    continue
                semantic = _semantic_input_for_selection(plan, selection.matched_capabilities)
                compiled = self._input_compiler.compile_entry(
                    entry,
                    semantic_input=semantic,
                    raw_message=plan.user_message,
                )
                provider_inputs[selection.skill] = dict(compiled.arguments)
                errors.extend(
                    {
                        **error,
                        "scope": "plan",
                        "skill_name": selection.skill,
                        "skill_source": skill_source,
                        "capability": _first_capability(selection.matched_capabilities),
                    }
                    for error in compiled.errors
                )

        if plan.intent_type == IntentType.WORKFLOW:
            for step in plan.workflow_steps:
                if _is_native_workflow_step(step):
                    step["input_compiled"] = True
                    continue
                skill_name = str(step.get("skill_name") or step.get("skill") or "")
                skill_source = step_skill_source(step)
                if skill_source:
                    step["skill_source"] = skill_source
                entry = resolve_step_entry(self._registry, step)
                if entry is None:
                    continue
                semantic = dict(step["input"]) if isinstance(step.get("input"), dict) else {}
                semantic.pop(SKILL_SOURCE_PARAM, None)
                compiled = self._input_compiler.compile_entry(
                    entry,
                    semantic_input=semantic,
                    # Bind only an instruction the step already carried under a
                    # planner alias (goal/query). Do not copy the workflow goal
                    # into every child provider.
                    raw_message=instruction_raw_message(entry, semantic),
                )
                step["input"] = dict(compiled.arguments)
                step["input_compiled"] = True
                errors.extend(
                    {
                        **error,
                        "scope": "step",
                        "step_id": str(step.get("id") or ""),
                        "skill_name": skill_name,
                        "skill_source": skill_source,
                        "capability": str(step.get("capability") or ""),
                    }
                    for error in compiled.errors
                )

        plan.provider_inputs = provider_inputs
        plan.input_compilation_errors = errors
        plan.inputs_compiled = True

    def _validate_compilation_errors(self, plan: IntentPlan, result: PlanValidationResult) -> None:
        for error in plan.input_compilation_errors:
            scope = str(error.get("scope") or "plan")
            step_id = str(error.get("step_id") or "")
            skill_name = str(error.get("skill_name") or "")
            skill_source = str(error.get("skill_source") or "")
            capability = str(error.get("capability") or "")
            missing_field = str(error.get("field") or "")
            entry = resolve_step_entry(
                self._registry,
                {
                    "skill_name": skill_name,
                    "skill_source": skill_source,
                },
            )
            repair_capability = (
                input_contract_repair_capability(entry, missing_field) if entry else ""
            )
            owner = (
                input_contract_binding_owner(entry, missing_field)
                if entry and missing_field
                else "compiler"
            )
            keyword = str(error.get("keyword") or "")
            contract_definition_error = keyword in _PROVIDER_SCHEMA_DEFINITION_KEYWORDS
            # A slot the provider never declared has no answer anyone can give:
            # not the user, who cannot add a field to someone else's contract,
            # and not the model, whose value the compiler already left out of
            # ``arguments``. The remedy is therefore already applied by the time
            # we get here, and the only thing left to do is say so. Reporting it
            # as a blocker sent run 0db3d740 to the ask rung, which handed the
            # user the finding text as though it were a question.
            undeclared_field = str(error.get("code") or "") == "unknown_provider_field"
            objective_schema_error = bool(keyword and owner != "resolver")
            finding_owner = (
                "provider"
                if contract_definition_error
                else "resolver"
                if owner == "resolver"
                else "model"
                if objective_schema_error
                else owner
            )
            field_path = _compilation_error_pointer(
                plan,
                scope=scope,
                step_id=step_id,
                capability=capability,
                path=str(error.get("path") or missing_field),
            )
            repairable_schema_error = bool(
                objective_schema_error
                and finding_owner == "model"
                and keyword in _MODEL_REPAIRABLE_INSTANCE_KEYWORDS
                and field_path
                and entry is not None
                and str(getattr(entry, "contract_level", "") or "") == "full"
            )
            capability_repairable = bool(repair_capability and not contract_definition_error)
            message = str(
                error.get("message") or error.get("reason") or "provider input is invalid"
            )
            if undeclared_field:
                message = f"{message}; the value was dropped and the run continued without it"
            binding = _provider_binding_for_error(
                plan,
                scope=scope,
                step_id=step_id,
                skill_name=skill_name,
                skill_source=skill_source,
            )
            kwargs: dict[str, object] = {
                "scope": scope,
                "step_id": step_id,
                "skill_name": skill_name,
                "capability": capability,
                "missing_field": missing_field,
                "repairable": repairable_schema_error or capability_repairable,
                "repair_capability": repair_capability,
                "field_path": field_path,
                "actual": error.get("actual"),
                "expected": list(error.get("allowed_values") or []),
                "owner": finding_owner,
                "provider_binding_id": str(binding.get("provider_binding_id") or ""),
                "provider_source": str(binding.get("provider_source") or skill_source),
                "provider_contract_hash": str(
                    binding.get("contract_hash") or binding.get("provider_contract_hash") or ""
                ),
                "schema_keyword": keyword,
                "allowed_values": list(error.get("allowed_values") or []),
                "repair_strategy": (
                    "drop_undeclared_input"
                    if undeclared_field
                    else "resolver"
                    if finding_owner == "resolver"
                    else (
                        "schema_model_patch"
                        if repairable_schema_error
                        else (
                            "capability_repair"
                            if capability_repairable
                            else (
                                "provider_contract_fix"
                                if contract_definition_error
                                else "needs_input"
                            )
                        )
                    )
                ),
            }
            finding_code = (
                "provider_schema_invalid"
                if objective_schema_error
                else ("step_input_contract" if scope == "step" else "provider_input_contract")
            )
            if undeclared_field or (
                not contract_definition_error
                and scope == "step"
                and _step_is_degradable(
                    next(
                        (
                            step
                            for step in plan.workflow_steps
                            if str(step.get("id") or "") == step_id
                        ),
                        {},
                    ),
                    entry,
                )
            ):
                result.degrade(finding_code, message, **kwargs)  # type: ignore[arg-type]
            else:
                result.error(finding_code, message, **kwargs)  # type: ignore[arg-type]

    def _validate_skill(
        self,
        skill_name: str,
        result: PlanValidationResult,
        *,
        automatic_required_step: bool = False,
        explicit: bool = False,
        source: str = "",
    ) -> None:
        entry = resolve_step_entry(
            self._registry,
            {
                "skill_name": skill_name,
                "skill_source": source,
            },
        )
        if entry is None:
            result.error(
                "skill_not_installed",
                f"selected skill '{skill_name}' is not installed",
                skill_name=skill_name,
            )
            return
        if not entry.trusted:
            result.error(
                "skill_untrusted",
                f"skill '{skill_name}' is quarantined; review it and run `omni skills trust {skill_name} --yes`",
                skill_name=skill_name,
            )
            return
        if entry.is_disabled:
            result.error(
                "skill_disabled",
                f"selected skill '{skill_name}' is disabled",
                skill_name=skill_name,
            )
            return
        _validate_provider_schema_contracts(entry, result)
        if entry.is_deprecated and not explicit:
            result.error(
                "skill_deprecated",
                f"selected skill '{skill_name}' is deprecated",
                skill_name=skill_name,
            )
            return
        if entry.is_deprecated:
            replacement = f"; use '{entry.replaced_by}' instead" if entry.replaced_by else ""
            result.degrade(
                "skill_deprecated_explicit",
                f"selected deprecated skill '{skill_name}' explicitly{replacement}",
                skill_name=skill_name,
            )
        if automatic_required_step and entry.contract_level == "none":
            result.error(
                "skill_contract_none",
                f"automatic workflow required step skill '{skill_name}' contract is none; "
                "explicit invocation or optional step is required",
                skill_name=skill_name,
            )
            return
        if entry.contract_level != "full":
            result.degrade(
                "skill_contract_partial",
                f"skill '{skill_name}' contract is {entry.contract_level}; "
                "artifact/provenance verification may be degraded",
                skill_name=skill_name,
            )

    def _validate_workflow_steps(self, plan: IntentPlan, result: PlanValidationResult) -> None:
        seen: set[str] = set()
        compilation_error_steps = {
            str(error.get("step_id") or "")
            for error in plan.input_compilation_errors
            if str(error.get("scope") or "") == "step"
        }
        dependents = {
            str(dep) for step in plan.workflow_steps for dep in (step.get("depends_on") or [])
        }
        terminal_roles: dict[str, str] = {}
        for step in plan.workflow_steps:
            step_id = str(step.get("id") or "")
            if not step_id or step_id in dependents:
                continue
            if _is_native_workflow_step(step):
                terminal_roles[step_id] = "task"
                continue
            entry = resolve_step_entry(self._registry, step)
            terminal_roles[step_id] = entry.skill_role if entry is not None else ""
        has_terminal_task = any(role == "task" for role in terminal_roles.values())
        for idx, step in enumerate(plan.workflow_steps, start=1):
            step_id = str(step.get("id") or "")
            skill_name = str(step.get("skill_name") or step.get("skill") or "")
            if not step_id:
                result.error("workflow_step_no_id", f"workflow step {idx} has no id", scope="step")
            elif step_id in seen:
                result.error(
                    "workflow_step_duplicate_id",
                    f"workflow step id '{step_id}' is duplicated",
                    scope="step",
                    step_id=step_id,
                )
            seen.add(step_id)
            if not skill_name and _is_native_workflow_step(step):
                skill_name = "synthesis.final"
            if not skill_name:
                result.error(
                    "workflow_step_no_skill",
                    f"workflow step {step_id or idx} has no skill_name",
                    scope="step",
                    step_id=step_id,
                )
                continue
            is_required = bool(step.get("required", True)) and not bool(step.get("optional"))
            if _is_native_workflow_step(step):
                entry = None
            else:
                skill_source = step_skill_source(step)
                self._validate_skill(
                    skill_name,
                    result,
                    automatic_required_step=is_required,
                    source=skill_source,
                )
                entry = resolve_step_entry(self._registry, step)
            if (
                entry is not None
                and entry.skill_role == "support"
                and is_required
                and len(plan.workflow_steps) > 1
                and step_id in terminal_roles
                and not has_terminal_task
            ):
                result.error(
                    "workflow_support_terminal",
                    f"workflow step '{step_id}' uses support skill '{skill_name}' as a terminal deliverable; "
                    "add a downstream task skill or mark it optional",
                    scope="step",
                    step_id=step_id,
                    skill_name=skill_name,
                )
            if step_id not in compilation_error_steps:
                self._validate_step_input_contract(
                    step_id or str(idx),
                    skill_name,
                    step,
                    entry,
                    result,
                    step_index=idx - 1,
                )
            for dep in step.get("depends_on") or []:
                if str(dep) not in seen:
                    result.error(
                        "workflow_step_bad_dependency",
                        f"workflow step '{step_id}' depends on unknown or later step '{dep}'",
                        scope="step",
                        step_id=step_id,
                    )

    @staticmethod
    def _validate_step_input_contract(
        step_id: str,
        skill_name: str,
        step: dict,
        entry: object | None,
        result: PlanValidationResult,
        *,
        step_index: int,
    ) -> None:
        if entry is None:
            return
        params = step.get("input") if isinstance(step.get("input"), dict) else {}
        contract_error = skill_input_contract_error(entry, dict(params))
        if not contract_error:
            return
        capability = str(step.get("capability") or "")
        missing_field = ""
        missing = contract_error.get("missing")
        if isinstance(missing, list) and missing:
            missing_field = str(missing[0])
        repair_capability = input_contract_repair_capability(entry, missing_field)
        owner = input_contract_binding_owner(entry, missing_field)
        keyword = str(contract_error.get("keyword") or "")
        contract_definition_error = keyword in _PROVIDER_SCHEMA_DEFINITION_KEYWORDS
        objective_schema_error = bool(keyword and owner != "resolver")
        finding_owner = (
            "provider"
            if contract_definition_error
            else "resolver"
            if owner == "resolver"
            else "model"
            if objective_schema_error
            else owner
        )
        relative_path = str(contract_error.get("path") or missing_field)
        field_path = f"/workflow_steps/{step_index}/input" + "".join(
            f"/{_escape_pointer(token)}"
            for token in relative_path.replace("[", ".").replace("]", "").split(".")
            if token and token != "$"
        )
        message = (
            "workflow step "
            f"'{step_id}' for skill '{skill_name}' violates input contract"
            f" ({contract_error.get('reason') or contract_error.get('code') or 'invalid input'})"
        )
        # Degradability mirrors the *runtime's* own resilience policy: a support
        # role or a ``continue_with_partial`` skill authored itself as optional, so
        # a missing input for it must not reject the whole plan (Rung 2). Only
        # genuinely required task steps stay blocking.
        degradable = _step_is_degradable(step, entry)
        repairable_schema_error = bool(
            objective_schema_error
            and finding_owner == "model"
            and keyword in _MODEL_REPAIRABLE_INSTANCE_KEYWORDS
            and field_path
            and str(getattr(entry, "contract_level", "") or "") == "full"
        )
        capability_repairable = bool(repair_capability and not contract_definition_error)
        finding_kwargs: dict[str, object] = {
            "scope": "step",
            "step_id": step_id,
            "skill_name": skill_name,
            "capability": capability,
            "missing_field": missing_field,
            "repairable": repairable_schema_error or capability_repairable,
            "repair_capability": repair_capability,
            "field_path": field_path,
            "actual": contract_error.get("actual"),
            "expected": list(contract_error.get("allowed_values") or []),
            "owner": finding_owner,
            "provider_binding_id": str(step.get("provider_binding_id") or ""),
            "provider_source": str(step.get("provider_source") or ""),
            "provider_contract_hash": str(step.get("provider_contract_hash") or ""),
            "schema_keyword": keyword,
            "allowed_values": list(contract_error.get("allowed_values") or []),
            "repair_strategy": (
                "resolver"
                if finding_owner == "resolver"
                else (
                    "schema_model_patch"
                    if repairable_schema_error
                    else (
                        "capability_repair"
                        if capability_repairable
                        else (
                            "provider_contract_fix" if contract_definition_error else "needs_input"
                        )
                    )
                )
            ),
        }
        finding_code = (
            "provider_schema_invalid" if keyword and owner != "resolver" else "step_input_contract"
        )
        if degradable and not contract_definition_error:
            result.degrade(finding_code, message, **finding_kwargs)  # type: ignore[arg-type]
        else:
            result.error(finding_code, message, **finding_kwargs)  # type: ignore[arg-type]

    @staticmethod
    def _validate_tool_policy(plan: IntentPlan, result: PlanValidationResult) -> None:
        policy = plan.tool_policy
        conflict = set(policy.allowed_tools or []).intersection(policy.blocked_tools)
        if conflict:
            # An over-privilege policy (a tool both allowed and blocked) is a Rung 0
            # safety violation: never silently degraded or rerouted with it intact.
            result.safety(
                "tool_policy_conflict",
                "tool_policy has tools in both allowed and blocked lists: "
                + ", ".join(sorted(conflict)),
            )
        if (policy.max_tool_calls is not None and policy.max_tool_calls < 0) or (
            policy.max_iterations is not None and policy.max_iterations < 0
        ):
            result.safety("tool_policy_negative_limits", "tool_policy limits must be non-negative")
        for name, limit in policy.per_tool_limits.items():
            if int(limit) < 0:
                result.safety(
                    "tool_policy_negative_limit",
                    f"per-tool limit for '{name}' must be non-negative",
                )

    @staticmethod
    def _validate_verification_plan(plan: IntentPlan, result: PlanValidationResult) -> None:
        verification = plan.verification_plan
        if not verification.required_outputs:
            result.warn("verification_plan.required_outputs is empty")
        if (
            plan.intent_type
            in {
                IntentType.QA_PLUS_ARTIFACT,
                IntentType.SINGLE_SKILL_TASK,
                IntentType.WORKFLOW,
            }
            and not verification.required_events
        ):
            result.warn("verification_plan.required_events is empty for executable plan")


def _step_is_degradable(step: dict, entry: object | None) -> bool:
    """Whether a step may be pruned/degraded instead of failing the plan.

    A step is degradable when the plan marked it optional/non-required, or when
    the resolved skill authored itself as tolerant of partial failure (``support``
    role or ``continue_with_partial``). This keeps plan-time validation no
    stricter than the workflow runtime's own partial-completion policy.
    """
    if bool(step.get("optional")):
        return True
    if not bool(step.get("required", True)):
        return True
    if str(step.get("failure_policy") or "") == "continue_with_partial":
        return True
    if entry is not None:
        if getattr(entry, "skill_role", "") == "support":
            return True
        workflow = getattr(entry, "workflow", {}) or {}
        if (
            isinstance(workflow, dict)
            and str(workflow.get("failure_policy") or "") == "continue_with_partial"
        ):
            return True
    return False




def _validate_provider_schema_contracts(
    entry: object,
    result: PlanValidationResult,
) -> None:
    """Fail closed on provider-owned schema definitions, including YAML null."""
    skill_name = str(getattr(entry, "name", "") or "")
    provider_source = str(getattr(entry, "source", "") or "")
    for error in provider_schema_definition_errors(entry):
        schema_field = str(error.get("schema_field") or "schema")
        keyword = str(error.get("keyword") or "invalid_schema")
        if schema_field == "input_schema" and any(
            finding.code == "provider_schema_invalid"
            and finding.skill_name == skill_name
            and finding.owner == "provider"
            and finding.schema_keyword == keyword
            for finding in result.findings
        ):
            continue
        message = str(error.get("message") or "provider schema is invalid")
        result.error(
            "provider_schema_invalid",
            f"skill '{skill_name}' has an invalid {schema_field}: {message}",
            skill_name=skill_name,
            owner="provider",
            provider_source=provider_source,
            field_path=f"/{schema_field}",
            schema_keyword=keyword,
            repairable=False,
            repair_strategy="provider_contract_fix",
        )


def _is_native_workflow_step(step: dict) -> bool:
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


def _semantic_input_for_selection(plan: IntentPlan, capabilities: list[str]) -> dict:
    for capability in capabilities:
        value = plan.capability_inputs.get(capability)
        if isinstance(value, dict):
            return dict(value)
    if len(plan.capability_inputs) == 1:
        value = next(iter(plan.capability_inputs.values()))
        return dict(value) if isinstance(value, dict) else {}
    return {}


def _first_capability(capabilities: list[str]) -> str:
    return str(capabilities[0]) if capabilities else ""


def _compilation_error_pointer(
    plan: IntentPlan,
    *,
    scope: str,
    step_id: str,
    capability: str,
    path: str,
) -> str:
    """Map a provider-relative schema path back to its authoritative plan input."""
    suffix = "".join(
        f"/{_escape_pointer(token)}"
        for token in path.replace("[", ".").replace("]", "").split(".")
        if token and token != "$"
    )
    if scope == "step":
        for index, step in enumerate(plan.workflow_steps):
            if str(step.get("id") or "") == step_id:
                return f"/workflow_steps/{index}/input{suffix}"
        return ""
    if capability:
        return f"/capability_inputs/{_escape_pointer(capability)}{suffix}"
    return ""


def _provider_binding_for_error(
    plan: IntentPlan,
    *,
    scope: str,
    step_id: str,
    skill_name: str,
    skill_source: str,
) -> dict[str, Any]:
    """Return the exact sealed binding associated with a compiler finding."""
    if scope == "step":
        step = next(
            (item for item in plan.workflow_steps if str(item.get("id") or "") == step_id),
            {},
        )
        return {
            "provider_binding_id": step.get("provider_binding_id"),
            "provider_source": step.get("provider_source"),
            "provider_contract_hash": step.get("provider_contract_hash"),
        }
    return next(
        (
            item
            for item in plan.provider_bindings
            if str(item.get("provider_name") or "") == skill_name
            and (not skill_source or str(item.get("provider_source") or "") == skill_source)
        ),
        {},
    )


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")
