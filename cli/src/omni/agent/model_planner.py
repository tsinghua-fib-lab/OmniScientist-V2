"""Model-assisted semantic planner.

The model is allowed to interpret open-ended user intent, but not to grant
itself tools or pick trusted implementations directly. It returns a small
proposal in capability terms; ``IntentPlanner`` and ``SkillRegistry`` turn that
proposal into an executable, validated runtime plan.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any
from weakref import WeakKeyDictionary

from omni.agent.workflow_plan_builder import _compose_step_input
from omni.core.llm.client import LLMClient
from omni.core.tool_contracts import skill_input_contract_error
from omni.skills_runtime.registry import SkillRegistry

_ALLOWED_INTENTS = {
    "direct_answer",
    "qa_plus_artifact",
    "single_skill_task",
    "workflow",
    "memory_update",
    "schedule",
    "needs_input",
    "react_fallback",
}
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
# A syntactic placeholder the model emits when it declines to bind a real value
# (e.g. ``<user_provided>`` / ``{{arxiv_id}}``). Matched at the string level only
# — language-neutral, never a keyword list — so a placeholder is neither counted
# as a binding nor allowed to flow into a provider's input.
_PLACEHOLDER_RE = re.compile(r"^\s*(?:<[^<>]*>|\{\{.*\}\})\s*$", re.DOTALL)
_NATIVE_CAPABILITIES = {
    "artifact.revise",
    "draft.manuscript",
    "draft.section",
    "memory.update",
    "synthesis.final",
}
_PLANNER_PROMPT_CACHE: WeakKeyDictionary[
    SkillRegistry,
    dict[tuple[int, int], _PlannerPromptBase],
] = WeakKeyDictionary()
_PLANNER_CONTRACT_SHORTLIST_LIMIT = 8
_PLANNER_INDEX_PROSE_LIMIT = 160


@dataclass(frozen=True, slots=True)
class _PlannerPromptBase:
    skill_index: str
    capabilities: str
    connectors: str
    domains: str


@dataclass(frozen=True, slots=True)
class ModelPlanProposal:
    intent_type: str = "react_fallback"
    required_capabilities: list[str] = field(default_factory=list)
    workflow_steps: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[str] = field(default_factory=lambda: ["answer"])
    capability_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing_inputs: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    execution_mode: str = "react"
    provenance_mode: str = "light"
    rationale: str = ""
    # Deterministic reconciliation audit (never from the model payload): any
    # stale ``missing_inputs`` dropped once the plan proved executable. Surfaced
    # via the ``plan.model.proposed`` run event (``to_dict``).
    binding_audit: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ModelPlanProposal:
        intent = str(payload.get("intent_type") or payload.get("intent") or "react_fallback").strip()
        if intent not in _ALLOWED_INTENTS:
            intent = "react_fallback"
        caps = _clean_capabilities(payload.get("required_capabilities") or payload.get("capabilities") or [])
        steps = _clean_workflow_steps(payload.get("workflow_steps") or [])
        outputs = [str(item) for item in payload.get("outputs") or ["answer"] if str(item)]
        missing = [
            item for item in payload.get("missing_inputs") or []
            if isinstance(item, dict) and item.get("field")
        ]
        raw_inputs = payload.get("capability_inputs")
        capability_inputs = {
            str(capability): _strip_placeholder_values(value)
            for capability, value in (raw_inputs.items() if isinstance(raw_inputs, dict) else [])
            if (
                _valid_capability(str(capability))
                or str(capability) == "schedule"
            )
            and isinstance(value, dict)
        }
        confidence = _bounded_float(payload.get("confidence"), default=0.0)
        mode = str(payload.get("execution_mode") or "react")
        if mode not in {"react", "background", "foreground", "ask", "direct"}:
            mode = "react"
        provenance = str(payload.get("provenance_mode") or "light")
        if provenance not in {"light", "full"}:
            provenance = "light"
        return cls(
            intent_type=intent,
            required_capabilities=caps,
            workflow_steps=steps,
            outputs=outputs[:6] or ["answer"],
            capability_inputs=capability_inputs,
            missing_inputs=missing[:5],
            confidence=confidence,
            execution_mode=mode,
            provenance_mode=provenance,
            rationale=str(payload.get("rationale") or "model semantic planner proposal")[:500],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_type": self.intent_type,
            "required_capabilities": list(self.required_capabilities),
            "workflow_steps": list(self.workflow_steps),
            "outputs": list(self.outputs),
            "capability_inputs": {
                capability: dict(value) for capability, value in self.capability_inputs.items()
            },
            "missing_inputs": list(self.missing_inputs),
            "confidence": self.confidence,
            "execution_mode": self.execution_mode,
            "provenance_mode": self.provenance_mode,
            "rationale": self.rationale,
            "binding_audit": dict(self.binding_audit),
        }


class ModelIntentPlanner:
    """Ask the LLM for a capability-level plan proposal."""

    def __init__(self, llm: LLMClient, registry: SkillRegistry, *, settings: Any = None) -> None:
        self._llm = llm
        self._registry = registry
        self._settings = settings
        self.last_system = ""
        self.last_user = ""
        self.last_output = ""

    async def propose(self, user_message: str, *, context_summary: str = "") -> ModelPlanProposal | None:
        self.last_system = _planner_system_prompt(
            self._registry,
            settings=self._settings,
            user_message=user_message,
        )
        self.last_user = _planner_user_prompt(user_message, context_summary=context_summary)
        raw = await self._llm.chat(
            self.last_system,
            self.last_user,
            temperature=0.0,
        )
        self.last_output = raw
        payload = parse_model_plan_payload(raw)
        if payload is None:
            return None
        proposal = ModelPlanProposal.from_payload(payload)
        # Codex-aligned: the planner binds step inputs in the single planning
        # pass (no extra per-step binding LLM calls). We only run a
        # deterministic reconciliation that drops stale ``missing_inputs`` the
        # model listed for fields it already bound, so they cannot veto an
        # executable workflow. Genuine gaps are preserved for the validator +
        # recovery ladder. Semantic input mismatches, including contracted modes,
        # are handled later by the provider-declared contract lifecycle.
        if proposal.intent_type == "workflow":
            proposal = self._reconcile_missing_inputs(user_message, proposal)
        return proposal

    def _reconcile_missing_inputs(
        self, user_message: str, proposal: ModelPlanProposal
    ) -> ModelPlanProposal:
        """Drop stale ``missing_inputs`` once the bound plan is actually executable.

        ``missing_inputs`` is advisory metadata from phase one; the model
        sometimes lists a field it then binds anyway (the arxiv-id regression:
        it emitted a placeholder ``arxiv_id`` *and* the concrete id). This check
        is schema-driven and deterministic: for every provider-backed step we
        compose the same input the plan builder will and run the same contract
        the validator will (``skill_input_contract_error``). When every step's
        required fields are satisfied (placeholders never count), the advisory
        gaps are stale and are dropped so they cannot veto an executable plan or
        mislead the recovery ladder. There is no field-name matching — that
        avoids the ``arxiv_id`` vs ``identifier`` alias trap — only schema
        satisfaction. Genuine gaps are preserved and handled downstream by the
        validator plus the recovery ladder (repair before ask).
        """
        if not proposal.missing_inputs or not proposal.workflow_steps:
            return proposal
        for step in proposal.workflow_steps:
            capability = str(step.get("capability") or "")
            if not capability or capability in _NATIVE_CAPABILITIES:
                continue
            entry, _ = self._registry.resolve_capability(capability)
            if entry is None:
                # Unresolvable provider: executability cannot be proven, so keep
                # the advisory gaps rather than dropping them.
                return proposal
            composed, _ = _compose_step_input(
                entry,
                _strip_placeholder_values(proposal.capability_inputs.get(capability)),
                _strip_placeholder_values(step.get("input")),
                user_message,
            )
            if skill_input_contract_error(entry, dict(composed)):
                return proposal
        audit = {
            **proposal.binding_audit,
            "dropped_missing_inputs": [dict(item) for item in proposal.missing_inputs],
        }
        return replace(proposal, missing_inputs=[], binding_audit=audit)


def parse_model_plan_payload(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _planner_system_prompt(
    registry: SkillRegistry,
    *,
    settings: Any = None,
    user_message: str = "",
) -> str:
    cache = _PLANNER_PROMPT_CACHE.setdefault(registry, {})
    key = (registry.generation, id(settings))
    base = cache.get(key)
    if base is None:
        base = _PlannerPromptBase(
            skill_index=_planner_skill_index(registry),
            capabilities=", ".join(_declared_capabilities(registry)),
            connectors=_planner_connector_catalog(settings),
            domains=_planner_domain_catalog(settings),
        )
        cache.clear()
        cache[key] = base
    return _build_planner_system_prompt(
        registry,
        base=base,
        user_message=user_message,
    )


def _build_planner_system_prompt(
    registry: SkillRegistry,
    *,
    base: _PlannerPromptBase,
    user_message: str,
) -> str:
    catalog = _planner_skill_catalog(
        registry,
        user_message=user_message,
        skill_index=base.skill_index,
    )
    return (
        "You are OmniScientist's semantic intent planner. "
        "Return strict JSON only. Do not call tools. Do not choose concrete skill implementations "
        "unless the user explicitly named one; express work as capabilities. "
        "Allowed intent_type values: direct_answer, qa_plus_artifact, single_skill_task, workflow, "
        "memory_update, schedule, needs_input, react_fallback. "
        "Capabilities currently executable through contracts or native runners: "
        f"{base.capabilities}. "
        "Use needs_input only when the request is too vague AND nothing in the turn context "
        "or recent activity could resolve it. When the request refers to your own prior work "
        "(in any language: recently/last time/that one/again/\"the figure you generated\"), do "
        "NOT use needs_input: prefer react_fallback, whose tools (list_recent_tasks/get_task/"
        "get_subtask/open_artifact/memory_search) can look the referent up — never ask the user "
        "to re-describe something you produced and could retrieve. When you know a canonical identifier "
        "(e.g. a well-known paper's arXiv id), bind it directly into the relevant step input; list "
        "a value in missing_inputs only when you cannot bind it AND no listed capability could "
        "discover it. Never list a field you have already bound. Use direct_answer only for a short "
        "answer that needs no tools or artifacts. "
        "Local filesystem and shell work in the working directory — listing, reading, searching, "
        "creating, editing, moving, copying, or deleting files, or running a shell command — is a "
        "real job handled by the capable assistant turn: use react_fallback for it, not direct_answer "
        "(which has no tools) or needs_input, whenever a target path is given or discoverable in the "
        "working directory. For answer plus figure, use qa_plus_artifact with "
        "qa.grounded and artifact.figure. For multiple ordered skills, use workflow_steps. "
        "Choose artifact.figure for an ordinary lightweight flowchart, architecture diagram, or "
        "system schematic whose output is DOT/SVG/PNG. Choose figure.editable.pptx only for one "
        "editable scientific figure delivered as a single-slide PPTX. Choose slides.generate for "
        "a complete multi-slide deck such as a group meeting, thesis defense, conference talk, or "
        "report. Never substitute one of these three artifact capabilities for another. Put the "
        "user's figure/deck instruction in the matching capability_inputs object. "
        "Bind provider inputs using the exact field names and enum values in the "
        "available skill contracts below; do not invent host-side aliases. "
        "Use provider enum descriptions and x-omni metadata as selection hints, "
        "but do not emit separate semantic constraints or target-field claims. "
        "When the user asks to run work on a recurring or future schedule — a repeating cadence "
        "(daily/weekly/hourly, \"every day at 6pm\", \"every Monday\", \"every hour\", a cron "
        "expression) or a one-time future time (\"tomorrow 9am\", \"next Monday\"), expressed in any "
        "language — use intent_type "
        "schedule. Do NOT perform the work now and do NOT route it to a workflow: this turn only "
        "registers the schedule. Put the recurring goal (the work to run each time, in the user's "
        "language) in capability_inputs.schedule.task.goal; scheduling itself needs no capability. "
        "When the user explicitly asks the agent to remember durable information, use memory_update "
        "with capability memory.update and put only the fact to retain in "
        "capability_inputs.memory.update.content. Preserve the fact's original language. "
        "When the turn context has an Active target that is a figure/diagram and the user wants to "
        "change THAT figure, use single_skill_task with capability artifact.revise — for any edit "
        "of the attached figure, whether a small in-place tweak (recolor, restyle, relabel, "
        "highlight a part) or a structural rework (\"too simple\", \"from an engineering angle\", "
        "\"redesign\", add modules). The runtime applies an in-place colour patch when the target and "
        "colour are explicit, otherwise a source-preserving redraw; you do not decide minor vs major. "
        "If the user asks a question about the active figure, use direct_answer or qa.grounded — do "
        "not edit. Use artifact.figure ONLY for a brand-new figure (\"generate/create a new figure\"), "
        "not to modify the attached one. "
        "For artifact.revise, put normalized execution data in capability_inputs.artifact.revise: "
        "target is the exact visible element label when one is named; style is a canonical CSS color "
        "name or hex value when one is requested; scope is element or structure. Do not copy a "
        "language-specific edit verb into these fields. Runtime validates these fields and chooses "
        "the actual provider.\n\n"
        f"Available skill contracts:\n{catalog}\n\n"
        f"{base.connectors}\n\n"
        f"{base.domains}\n\n"
        "JSON shape: {"
        "\"intent_type\": string, "
        "\"confidence\": number, "
        "\"required_capabilities\": string[], "
        "\"workflow_steps\": [{\"id\": string, \"capability\": string, \"depends_on\": string[], \"input\": object, \"reason\": string}], "
        "\"outputs\": string[], "
        "\"capability_inputs\": {\"capability.name\": object}, "
        "\"missing_inputs\": [{\"field\": string, \"reason\": string}], "
        "\"execution_mode\": \"react|background|foreground|ask|direct\", "
        "\"provenance_mode\": \"light|full\", "
        "\"rationale\": string"
        "}."
    )


def _planner_user_prompt(user_message: str, *, context_summary: str = "") -> str:
    context = (context_summary or "").strip()
    if not context:
        return f"Plan this user request in capability terms:\n{user_message}"
    return (
        "Plan this user request in capability terms.\n\n"
        "Bounded turn context (use only to resolve references like this figure/paper/result; "
        "do not invent unavailable targets):\n"
        f"{context}\n\n"
        f"User request:\n{user_message}"
    )


def _planner_skill_catalog(
    registry: SkillRegistry,
    *,
    char_budget: int | None = None,
    user_message: str = "",
    skill_index: str | None = None,
) -> str:
    """Render an all-provider index plus full contracts for a bounded shortlist.

    ``char_budget`` remains accepted for extension compatibility but raw string
    clipping is intentionally forbidden: it can silently erase a late provider
    or a late schema field. The budget is structural instead—every selectable
    provider gets one index row, while at most eight relevant providers receive
    their complete compact input contract.
    """
    del char_budget
    index = (
        skill_index
        if skill_index is not None
        else _planner_skill_index(registry)
    )
    contracts = _planner_relevant_contracts(
        registry,
        user_message=user_message,
    )
    return (
        "Provider capability index (all selectable):\n"
        f"{index}\n\n"
        "Relevant provider input contracts (exact field names):\n"
        f"{contracts}"
    )


def _planner_skill_index(registry: SkillRegistry) -> str:
    """One bounded discovery row for every selectable provider."""
    lines: list[str] = []
    for entry in registry.list_selectable():
        caps = ", ".join(entry.capabilities) if entry.capabilities else "none"
        line = (
            f"- {entry.name}: source={entry.source}; contract={entry.contract_level}; "
            f"mode={entry.delivery_mode.value}; capabilities={caps}; "
            f"description={_bounded_index_prose(entry.description)}"
        )
        when = _bounded_index_prose(entry.when_to_use)
        if when:
            line += f"; when_to_use={when}"
        lines.append(line)
    return "\n".join(lines) or "(no skills installed)"


def _bounded_index_prose(value: Any) -> str:
    """Collapse prose and cap its per-provider prompt contribution."""
    text = " ".join(str(value or "").split())
    if len(text) <= _PLANNER_INDEX_PROSE_LIMIT:
        return text
    return text[: _PLANNER_INDEX_PROSE_LIMIT - 1].rstrip() + "…"


def _planner_relevant_contracts(
    registry: SkillRegistry,
    *,
    user_message: str,
) -> str:
    entries = _planner_relevant_entries(
        registry,
        user_message=user_message,
        limit=_PLANNER_CONTRACT_SHORTLIST_LIMIT,
    )
    lines = [
        json.dumps(
            {
                "provider": {
                    "name": str(entry.name or ""),
                    "source": str(entry.source or ""),
                    "version": str(entry.version or ""),
                },
                "input_schema": (
                    entry.input_schema
                    if isinstance(entry.input_schema, dict)
                    else {}
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for entry in entries
    ]
    return "\n".join(lines) or "(no relevant provider contract)"


def _planner_relevant_entries(
    registry: SkillRegistry,
    *,
    user_message: str,
    limit: int,
) -> list[Any]:
    """Deterministically shortlist provider contracts without classifying intent."""
    entries = list(registry.list_selectable())
    message = (user_message or "").casefold()
    message_tokens = {
        token
        for token in re.findall(r"\w+", message, flags=re.UNICODE)
        if len(token) > 1
    }
    scored: list[tuple[float, int, str, Any]] = []
    for entry in entries:
        score = 0.0
        name = str(entry.name or "").casefold()
        if name and name in message:
            score += 100.0
        phrases = (
            entry.trigger.get("phrases")
            if isinstance(entry.trigger, dict)
            else []
        )
        for phrase in phrases or []:
            normalized = str(phrase).casefold().strip()
            if normalized and normalized in message:
                score += 40.0
        for phrase in entry.default_for or []:
            normalized = str(phrase).casefold().strip()
            if normalized and normalized in message:
                score += 30.0
        for capability in entry.capabilities or []:
            normalized = str(capability).casefold()
            if normalized and normalized in message:
                score += 50.0
            parts = {
                part
                for part in re.findall(r"\w+", normalized)
                if len(part) > 3
            }
            score += 4.0 * len(parts & message_tokens)
        metadata = " ".join(
            (
                str(entry.name or ""),
                str(entry.description or ""),
                str(entry.when_to_use or ""),
            )
        ).casefold()
        metadata_tokens = {
            token
            for token in re.findall(r"\w+", metadata, flags=re.UNICODE)
            if len(token) > 1
        }
        score += min(
            10.0,
            float(len(metadata_tokens & message_tokens)),
        )
        if score > 0:
            scored.append(
                (
                    score,
                    int(entry.priority or 0),
                    str(entry.name or ""),
                    entry,
                )
            )
    if not scored:
        scored = [
            (
                float(_contract_field_count(entry)),
                int(entry.priority or 0),
                str(entry.name or ""),
                entry,
            )
            for entry in entries
        ]
    scored.sort(
        key=lambda item: (-item[0], -item[1], item[2])
    )
    return [entry for *_rank, entry in scored[: max(0, limit)]]


def _contract_field_count(entry: Any) -> int:
    schema = getattr(entry, "input_schema", None)
    properties = (
        schema.get("properties")
        if isinstance(schema, dict)
        and isinstance(schema.get("properties"), dict)
        else {}
    )
    return len(properties)


def _planner_connector_catalog(settings: Any) -> str:
    """Describe the enabled external data connectors (from ConnectorRegistry)."""
    if settings is None:
        return "Data connectors: (registry unavailable; assume literature sources enabled)"
    try:
        from omni.research.registry import ConnectorRegistry

        return ConnectorRegistry(settings).catalog_prompt()
    except Exception:  # noqa: BLE001 - planner prompt must never crash on catalog
        return "Data connectors: (unavailable)"


def _planner_domain_catalog(settings: Any) -> str:
    if settings is None:
        return "Research domain packs: (registry unavailable)"
    try:
        from omni.research.domain_packs import DomainPackRegistry

        return DomainPackRegistry(settings).prompt()
    except Exception:  # noqa: BLE001 - planner prompt must never crash on pack metadata
        return "Research domain packs: (unavailable)"


def _is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER_RE.match(value))


def _strip_placeholder_values(data: Any) -> dict[str, Any]:
    """Drop placeholder-valued keys so they neither bind nor reach a provider."""
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if not _is_placeholder(value)}


def _clean_capabilities(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        cap = str(value).strip()
        if _valid_capability(cap) and cap not in out:
            out.append(cap)
    return out


def _clean_workflow_steps(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(values[:12], start=1):
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or f"step_{idx}").strip()
        capability = str(item.get("capability") or "").strip()
        if not _valid_capability(capability):
            continue
        if step_id in seen:
            step_id = f"{step_id}_{idx}"
        seen.add(step_id)
        depends = [str(dep) for dep in item.get("depends_on") or [] if str(dep) in seen]
        input_data = _strip_placeholder_values(item.get("input"))
        out.append(
            {
                "id": step_id,
                "capability": capability,
                "depends_on": depends,
                "input": input_data,
                "reason": str(item.get("reason") or capability)[:300],
            }
        )
    return out


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _valid_capability(value: str) -> bool:
    return bool(_CAPABILITY_RE.fullmatch((value or "").strip()))


def _declared_capabilities(registry: SkillRegistry) -> list[str]:
    capabilities = set(_NATIVE_CAPABILITIES)
    for entry in registry.list_selectable():
        capabilities.update(cap for cap in entry.capabilities if _valid_capability(cap))
        capabilities.update(deliverable for deliverable in entry.deliverables if _valid_capability(deliverable))
    return sorted(capabilities)
