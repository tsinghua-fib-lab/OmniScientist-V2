"""Structured plans for one user turn.

The planner is intentionally a harness boundary: the model can still help with
ambiguous cases, but runtime code gets a typed contract for route, context,
tools, provenance, and acceptance checks.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class IntentType(StrEnum):
    DIRECT_ANSWER = "direct_answer"
    QA_PLUS_ARTIFACT = "qa_plus_artifact"
    SINGLE_SKILL_TASK = "single_skill_task"
    WORKFLOW = "workflow"
    MEMORY_UPDATE = "memory_update"
    # User asked to run work on a schedule (recurring/one-time). Routed to a
    # tool-capable ReAct turn that owns only the scheduling tools, so the request
    # becomes a durable scheduled task instead of dead-ending in capability
    # matching (the "no executable contracted provider" trap).
    SCHEDULE = "schedule"
    NEEDS_INPUT = "needs_input"
    REACT_FALLBACK = "react_fallback"


@dataclass(slots=True)
class SkillCandidateRejection:
    skill: str
    reason: str


@dataclass(slots=True)
class SkillSelection:
    skill: str
    reason: str
    matched_capabilities: list[str] = field(default_factory=list)
    selection_source: str = "planner"
    confidence: float = 0.0
    candidate_score: float = 0.0
    contract_level: str = "none"
    rejected_candidates: list[SkillCandidateRejection] = field(default_factory=list)
    # Concrete discovery source when the user forced one via ``$<scope>:<name>``
    # (e.g. ``user_omni``). Empty means "the winning skill for this name".
    skill_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "rejected_candidates": [asdict(item) for item in self.rejected_candidates],
        }


@dataclass(slots=True)
class ContextPolicy:
    include_recent_activity: bool = False
    include_research_brief: bool = False
    include_skill_catalog: bool = False
    include_memory: bool = True
    include_referenced_tasks: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolPolicy:
    # None means "unrestricted except blocked_tools"; [] means "no tools".
    allowed_tools: list[str] | None = None
    blocked_tools: list[str] = field(default_factory=list)
    per_tool_limits: dict[str, int] = field(default_factory=dict)
    # None means "use runtime default"; 0 means "zero budget".
    max_tool_calls: int | None = None
    max_iterations: int | None = None
    final_reserve_enabled: bool = True
    # Require the *opening* model turn to call a tool (vs. "auto"). Used by the
    # scheduling surface so an ambiguous request is resolved through
    # ``schedule_task`` (a structured clarification the loop can suspend on)
    # instead of a prose question that produces no schedule event.
    require_opening_tool: bool = False

    def allows(self, tool_name: str) -> bool:
        if tool_name in self.blocked_tools:
            return False
        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class VerificationPlan:
    """The durable trace a turn's claims must leave.

    This used to be an eight-field acceptance contract the host re-graded the
    finished turn against. The model can see its own tool results, so grading
    them again from outside only produced a verdict that could disagree with the
    answer the user was already shown.

    What is left is the one thing the answer cannot vouch for: over IM and in
    headless runs the user sees prose, not tool calls, so a turn that says it
    created a schedule has to have left a ``schedule.resolved`` event behind.
    :mod:`omni.runtime.settlement` checks these names against the event log; a
    claim with no trace settles ``failed``.

    Named scientific ``required_outputs`` (figure, manuscript, slides) are a
    contract :mod:`omni.runtime.settlement` checks against artifacts. Prose names
    such as ``answer`` stay descriptive — the turn's text *is* the answer.
    """

    required_outputs: list[str] = field(default_factory=list)
    required_events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class IntentPlan:
    task_id: str
    user_message: str
    intent_type: IntentType
    # v2 seals exact provider identities and resolver evidence. Persisted v1
    # plans intentionally omit the new fields when rehydrated so their accepted
    # revision hashes remain stable.
    plan_schema_version: int = 2
    _plan_schema_version_present: bool = field(
        default=True,
        repr=False,
        compare=False,
    )
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Content-addressed revision metadata. ``revision_hash`` is computed from
    # the executable plan projection with these four metadata fields excluded.
    revision: int = 0
    revision_hash: str = ""
    parent_revision_hash: str = ""
    revision_source: str = "planner"
    confidence: float = 0.0
    outputs: list[str] = field(default_factory=list)
    capability_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    provider_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    inputs_compiled: bool = False
    input_compilation_errors: list[dict[str, Any]] = field(default_factory=list)
    selected_skills: list[SkillSelection] = field(default_factory=list)
    execution_mode: str = "react"
    provenance_mode: str = "light"
    context_policy: ContextPolicy = field(default_factory=ContextPolicy)
    tool_policy: ToolPolicy = field(default_factory=ToolPolicy)
    acceptance: list[str] = field(default_factory=list)
    rationale: str = ""
    fallback: str = "react_fallback"
    workflow_steps: list[dict[str, Any]] = field(default_factory=list)
    task_contract: dict[str, Any] = field(default_factory=dict)
    workflow_dag: dict[str, Any] = field(default_factory=dict)
    requested_constraints: list[dict[str, Any]] = field(default_factory=list)
    binding_records: list[dict[str, Any]] = field(default_factory=list)
    provider_bindings: list[dict[str, Any]] = field(default_factory=list)
    _provider_bindings_present: bool = field(
        default=True,
        repr=False,
        compare=False,
    )
    resolver_evidence: list[dict[str, Any]] = field(default_factory=list)
    _resolver_evidence_present: bool = field(
        default=True,
        repr=False,
        compare=False,
    )
    missing_inputs: list[dict[str, Any]] = field(default_factory=list)
    # This-turn owner constraints. Empty lists omit from the hash/payload so
    # older persisted plans stay stable. Admission reads the same names off
    # the exec context after the orchestrator copies them.
    unavailable_services: list[str] = field(default_factory=list)
    unavailable_skills: list[str] = field(default_factory=list)
    unpayable_outputs: list[dict[str, Any]] = field(default_factory=list)
    verification_plan: VerificationPlan = field(default_factory=VerificationPlan)
    validation_warnings: list[str] = field(default_factory=list)
    degraded_warnings: list[str] = field(default_factory=list)
    # Presentation-only: a footnote for the user, never a model instruction and
    # never a degraded warning. Omitted from the hash when empty.
    user_notices: list[str] = field(default_factory=list)
    # Presentation-only: the succeeded twin this turn is redoing. The
    # footnote names that earlier task; this turn's Outputs stay this task_id.
    twin_task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "plan_id": self.plan_id,
            "revision": self.revision,
            "revision_hash": self.revision_hash,
            "parent_revision_hash": self.parent_revision_hash,
            "revision_source": self.revision_source,
            "task_id": self.task_id,
            "intent_type": self.intent_type.value,
            "confidence": self.confidence,
            "user_message": self.user_message,
            "outputs": list(self.outputs),
            "capability_inputs": {
                str(capability): dict(value)
                for capability, value in self.capability_inputs.items()
                if isinstance(value, dict)
            },
            "provider_inputs": {
                str(provider): dict(value)
                for provider, value in self.provider_inputs.items()
                if isinstance(value, dict)
            },
            "inputs_compiled": self.inputs_compiled,
            "input_compilation_errors": list(self.input_compilation_errors),
            "selected_skills": [item.to_dict() for item in self.selected_skills],
            "execution_mode": self.execution_mode,
            "provenance_mode": self.provenance_mode,
            "context_policy": self.context_policy.to_dict(),
            "tool_policy": self.tool_policy.to_dict(),
            "acceptance": list(self.acceptance),
            "rationale": self.rationale,
            "fallback": self.fallback,
            "workflow_steps": list(self.workflow_steps),
            "task_contract": dict(self.task_contract),
            "workflow_dag": dict(self.workflow_dag),
            "requested_constraints": [
                dict(item) for item in self.requested_constraints
            ],
            "binding_records": [dict(item) for item in self.binding_records],
            "missing_inputs": list(self.missing_inputs),
            "verification_plan": self.verification_plan.to_dict(),
            "validation_warnings": list(self.validation_warnings),
            "degraded_warnings": list(self.degraded_warnings),
        }
        if self.unavailable_services:
            payload["unavailable_services"] = list(self.unavailable_services)
        if self.unavailable_skills:
            payload["unavailable_skills"] = list(self.unavailable_skills)
        if self.unpayable_outputs:
            payload["unpayable_outputs"] = [dict(item) for item in self.unpayable_outputs]
        if self.user_notices:
            payload["user_notices"] = list(self.user_notices)
        if self.twin_task_id:
            payload["twin_task_id"] = self.twin_task_id
        if self._plan_schema_version_present:
            payload["plan_schema_version"] = int(self.plan_schema_version)
        if self._provider_bindings_present or self.provider_bindings:
            payload["provider_bindings"] = [
                dict(item) for item in self.provider_bindings
            ]
        # Compatibility: old persisted plans did not carry this field, and their
        # revision hashes must remain stable when rehydrated. Every new plan
        # carries the field, including when no resolver evidence is required.
        if self._resolver_evidence_present or self.resolver_evidence:
            payload["resolver_evidence"] = [
                dict(item) for item in self.resolver_evidence
            ]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> IntentPlan:
        """Rehydrate a persisted plan for explicit approval/resume."""
        raw_intent = str(payload.get("intent_type") or IntentType.REACT_FALLBACK.value)
        try:
            intent = IntentType(raw_intent)
        except ValueError:
            intent = IntentType.REACT_FALLBACK

        selected: list[SkillSelection] = []
        for raw in payload.get("selected_skills") or []:
            if not isinstance(raw, dict) or not raw.get("skill"):
                continue
            rejected = [
                SkillCandidateRejection(
                    skill=str(item.get("skill") or ""),
                    reason=str(item.get("reason") or ""),
                )
                for item in raw.get("rejected_candidates") or []
                if isinstance(item, dict)
            ]
            selected.append(
                SkillSelection(
                    skill=str(raw.get("skill") or ""),
                    reason=str(raw.get("reason") or ""),
                    matched_capabilities=[str(item) for item in raw.get("matched_capabilities") or []],
                    selection_source=str(raw.get("selection_source") or "planner"),
                    confidence=float(raw.get("confidence") or 0.0),
                    candidate_score=float(raw.get("candidate_score") or 0.0),
                    contract_level=str(raw.get("contract_level") or "none"),
                    rejected_candidates=rejected,
                    skill_source=str(raw.get("skill_source") or ""),
                )
            )

        context = payload.get("context_policy") if isinstance(payload.get("context_policy"), dict) else {}
        tools = payload.get("tool_policy") if isinstance(payload.get("tool_policy"), dict) else {}
        verify = payload.get("verification_plan") if isinstance(payload.get("verification_plan"), dict) else {}
        return cls(
            plan_id=str(payload.get("plan_id") or uuid.uuid4().hex),
            revision=int(payload.get("revision") or 0),
            revision_hash=str(payload.get("revision_hash") or ""),
            parent_revision_hash=str(payload.get("parent_revision_hash") or ""),
            revision_source=str(payload.get("revision_source") or "planner"),
            task_id=str(payload.get("task_id") or ""),
            user_message=str(payload.get("user_message") or ""),
            intent_type=intent,
            plan_schema_version=int(payload.get("plan_schema_version") or 1),
            _plan_schema_version_present="plan_schema_version" in payload,
            confidence=float(payload.get("confidence") or 0.0),
            outputs=[str(item) for item in payload.get("outputs") or []],
            capability_inputs={
                str(capability): dict(value)
                for capability, value in (payload.get("capability_inputs") or {}).items()
                if isinstance(value, dict)
            },
            provider_inputs={
                str(provider): dict(value)
                for provider, value in (payload.get("provider_inputs") or {}).items()
                if isinstance(value, dict)
            },
            inputs_compiled=bool(payload.get("inputs_compiled", False)),
            input_compilation_errors=[
                item for item in payload.get("input_compilation_errors") or [] if isinstance(item, dict)
            ],
            selected_skills=selected,
            execution_mode=str(payload.get("execution_mode") or "react"),
            provenance_mode=str(payload.get("provenance_mode") or "light"),
            context_policy=ContextPolicy(**_known_fields(ContextPolicy, context)),
            tool_policy=ToolPolicy(**_known_fields(ToolPolicy, tools)),
            acceptance=[str(item) for item in payload.get("acceptance") or []],
            rationale=str(payload.get("rationale") or ""),
            fallback=str(payload.get("fallback") or "react_fallback"),
            workflow_steps=[item for item in payload.get("workflow_steps") or [] if isinstance(item, dict)],
            task_contract=dict(payload.get("task_contract") or {}),
            workflow_dag=dict(payload.get("workflow_dag") or {}),
            requested_constraints=[
                item
                for item in payload.get("requested_constraints") or []
                if isinstance(item, dict)
            ],
            binding_records=[
                item
                for item in payload.get("binding_records") or []
                if isinstance(item, dict)
            ],
            provider_bindings=[
                item
                for item in payload.get("provider_bindings") or []
                if isinstance(item, dict)
            ],
            _provider_bindings_present="provider_bindings" in payload,
            resolver_evidence=[
                item
                for item in payload.get("resolver_evidence") or []
                if isinstance(item, dict)
            ],
            _resolver_evidence_present="resolver_evidence" in payload,
            missing_inputs=[item for item in payload.get("missing_inputs") or [] if isinstance(item, dict)],
            unavailable_services=[
                str(item).strip()
                for item in payload.get("unavailable_services") or []
                if str(item).strip()
            ],
            unavailable_skills=[
                str(item).strip()
                for item in payload.get("unavailable_skills") or []
                if str(item).strip()
            ],
            unpayable_outputs=[
                dict(item)
                for item in payload.get("unpayable_outputs") or []
                if isinstance(item, dict)
            ],
            verification_plan=VerificationPlan(**_known_fields(VerificationPlan, verify)),
            validation_warnings=[str(item) for item in payload.get("validation_warnings") or []],
            degraded_warnings=[str(item) for item in payload.get("degraded_warnings") or []],
            user_notices=[str(item) for item in payload.get("user_notices") or [] if str(item).strip()],
            twin_task_id=str(payload.get("twin_task_id") or ""),
        )


def contract_level_for_schema(input_schema: dict[str, Any], output_schema: dict[str, Any], *, builtin: bool) -> str:
    """Classify a skill contract without rejecting portable third-party skills."""
    has_input = bool(input_schema and input_schema.get("type"))
    has_output = bool(output_schema and output_schema.get("type"))
    if builtin and has_input and has_output:
        return "full"
    if has_input or has_output:
        return "partial"
    return "none"


def _known_fields(cls: type[Any], payload: dict[str, Any]) -> dict[str, Any]:
    names = set(getattr(cls, "__dataclass_fields__", {}))
    return {key: value for key, value in payload.items() if key in names}
