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
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from omni.agent.capabilities import CAPABILITY_TASK_INSPECT, CAPABILITY_TASK_REVIEW
from omni.core.field_contract import instruction_field
from omni.core.llm.client import LLMClient
from omni.core.tool_contracts import skill_input_contract_error
from omni.skills_runtime.admission import normalize_constraint_names
from omni.skills_runtime.registry import SkillRegistry

# One whitespace-free token ending in a letter-initial extension. The leading
# letter is what keeps a dotted *identifier* — an arXiv id (1706.03762), a DOI,
# a version string — from being read as a path.
_PATHLIKE_RE = re.compile(r"^[\w~][\w./~+-]*\.[A-Za-z][A-Za-z0-9]{0,7}$")


def missing_file_reference(value: object) -> str:
    """A filename this value names that is not on disk, or ""."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not _PATHLIKE_RE.match(candidate):
        return ""
    try:
        return "" if Path(candidate).expanduser().exists() else candidate
    except OSError:  # pragma: no cover - an unparseable path is not a usable one
        return ""


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _compose_step_input(
    entry: object | None,
    capability_input: dict[str, object] | None,
    step_input: object,
    goal: str,
) -> tuple[dict[str, object], bool]:
    """Fold per-capability inputs into a step and bind its instruction slot.

    The semantic planner emits contract-declared per-capability inputs (a
    figure's title and figure_kind, say) separately from the step scaffold,
    whose ``input`` is often empty. An explicit per-step ``input`` wins on
    conflicts, and the provider's declared *instruction* slot is bound from the
    goal when it is otherwise empty — bounded to that one slot, never a strict
    identifier/DOI/path/enum field, so executability is restored without
    stuffing the goal into arbitrary provider fields.

    Returns the composed input and whether the instruction slot was goal-bound.
    """
    merged: dict[str, object] = dict(capability_input or {})
    if isinstance(step_input, dict):
        merged.update(step_input)
    bound_from_goal = False
    instruction_slot = instruction_field(getattr(entry, "input_schema", None))
    if instruction_slot and _has_value(goal) and not _has_value(merged.get(instruction_slot)):
        merged[instruction_slot] = goal
        bound_from_goal = True
    return merged, bound_from_goal

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
# A locator asserts that a specific address holds the user's material. It is a
# different act from resolving a *named* work to its canonical identifier
# (title → ``1706.03762``), which the runtime proves by fetching it; a locator
# the request never mentioned was read off the model's own weights, and
# following it retrieves something nobody asked for. Matched at the string
# level only — language-neutral, never a keyword list — and deliberately
# narrow: identifiers stay the resolver's business.
_LOCATOR_RE = re.compile(r"(?:https?|file)://[^\s\"'<>)\]]+", re.IGNORECASE)
# Marks a gap this module created by refusing a value, as opposed to one the
# model declared. They are routed differently and must stay distinguishable:
# see ``planner._plan_from_proposal``.
GROUNDING_GAP_SOURCE = "grounding_gate"
_NATIVE_CAPABILITIES = {
    "artifact.revise",
    "draft.manuscript",
    "draft.section",
    "memory.update",
    "synthesis.final",
    CAPABILITY_TASK_INSPECT,
    CAPABILITY_TASK_REVIEW,
}
_PLANNER_PROMPT_CACHE: WeakKeyDictionary[
    SkillRegistry,
    dict[tuple[int, int], _PlannerPromptBase],
] = WeakKeyDictionary()
# Wide enough to describe the whole shipped catalogue. The cut bounds the prompt
# on a machine carrying many installed providers; it was never meant to withhold
# the providers that ship. Below the catalogue size it does exactly that, and it
# picks its victims by whoever sorts last — which, on a request the lexical pass
# cannot read, is declared priority alone. At 8 against eleven shipped providers,
# three presented no field names at all for any Chinese request. One contract
# costs roughly 400 tokens, so covering the catalogue is by far the cheaper
# error. ``test_the_contract_shortlist_covers_the_shipped_catalogue`` fails once
# the catalogue reaches this number, making the next raise a decision rather
# than a silent regression.
_PLANNER_CONTRACT_SHORTLIST_LIMIT = 16
_PLANNER_INDEX_PROSE_LIMIT = 160
# CJK, kana, and hangul: scripts written without spaces between words.
_CJK_RANGES = "\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af"
_CJK_RUN = re.compile(rf"[{_CJK_RANGES}]+")


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
    # This-turn owner bans. Admission treats them as route facts, same as a
    # missing host service. Not a keyword parser: the model names what the
    # user forbade.
    unavailable_services: list[str] = field(default_factory=list)
    unavailable_skills: list[str] = field(default_factory=list)
    # An observation, never a control. Nothing branches on it, and nothing
    # should: run dc787efa was planned at 0.95 on a reading that was wrong, so a
    # self-reported score is not evidence about the world. It is carried to the
    # ``plan.model.proposed`` event so calibration can be reviewed after the
    # fact. Anything that must stop a run belongs in a gate that checks the
    # request — the locator check below, or the model explicitly choosing to ask.
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
        # A gap earns its place by naming a field *or* by saying something the
        # user can answer. Requiring ``field`` alone discarded questions whose
        # only fault was arriving without a slot name to hang on.
        missing = [
            item for item in payload.get("missing_inputs") or []
            if isinstance(item, dict)
            and (item.get("field") or item.get("ask") or item.get("reason"))
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
            unavailable_services=sorted(normalize_constraint_names(payload.get("unavailable_services"))),
            unavailable_skills=sorted(normalize_constraint_names(payload.get("unavailable_skills"))),
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
            "unavailable_services": list(self.unavailable_services),
            "unavailable_skills": list(self.unavailable_skills),
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
        # Refuse invented locators before executability is judged, so a plan that
        # only looked runnable because of a fabricated address is measured
        # without it — and one that is genuinely runnable without it still runs.
        proposal = _refuse_invented_locators(
            proposal, user_message=user_message, context_summary=context_summary
        )
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
        # A gate gap is not advisory and cannot go stale: it exists because a
        # value was *removed*, so the very contract check above passed only in
        # its absence. Dropping it here would restore the hole this pass is
        # meant to close.
        kept = [item for item in proposal.missing_inputs if _is_gate_gap(item)]
        dropped = [item for item in proposal.missing_inputs if not _is_gate_gap(item)]
        if not dropped:
            return proposal
        audit = {
            **proposal.binding_audit,
            "dropped_missing_inputs": [dict(item) for item in dropped],
        }
        return replace(proposal, missing_inputs=kept, binding_audit=audit)


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
        "Strongly prefer making a reasonable assumption and executing over stopping to ask. "
        "Stopping is not a pause here: the task ends and waits for a person. So when a detail "
        "is unspecified but a sensible choice exists, record the gap with a \"default\" holding "
        "the value you would use, and keep planning the work — the turn will run on that value "
        "and declare it in the answer. Reserve a gap with no \"default\" for what nobody can "
        "guess and a wrong guess would waste real work: which paper, which file, which account. "
        "If the user explicitly forbids a host service or a named skill this turn "
        "(for example \"do not use VLM\" or \"don't run livefigure\"), list it in "
        "unavailable_services or unavailable_skills. Do not invent a ban. That is new "
        "authority, not a guessable default: skills that require a forbidden service "
        "cannot run. Keep outputs that other skills or write_file can still pay, and "
        "execute that work. Leave a forbidden-only output on the ledger so the host "
        "can ask; do not invent a substitute producer. "
        "Use needs_input only when the request is too vague AND nothing in the turn context "
        "or recent activity could resolve it. When the request refers to your own prior work "
        "(in any language: recently/last time/that one/again/\"the figure you generated\"), do "
        "NOT use needs_input: prefer react_fallback, whose tools (list_recent_tasks/get_task/"
        "get_subtask/open_artifact/memory_search) can look the referent up — never ask the user "
        "to re-describe something you produced and could retrieve. When you know a canonical identifier "
        "(e.g. a well-known paper's arXiv id), bind it directly into the relevant step input; list "
        "a value in missing_inputs only when you cannot bind it AND no listed capability could "
        "discover it. Never list a field you have already bound. Use direct_answer for a short answer "
        "you can give immediately; it keeps a read-only tool floor as a safety net, so do not pick it "
        "when the request clearly needs tools. "
        "General local filesystem and shell work in the working directory — listing, reading, "
        "searching, creating, editing, moving, copying, or deleting files, or running a shell "
        "command — and "
        "questions about OmniScientist itself (its architecture, storage, memory, commands, or design, "
        "answered from built-in documentation) are handled by the capable assistant turn: use "
        "react_fallback for them, not direct_answer or needs_input, whenever a target path is given or "
        "discoverable in the working directory, or the answer should come from built-in docs. "
        "Questions about recent git commits, a changelog, or what changed in this repository "
        "are local history: use react_fallback so the assistant can run a bounded git log / "
        "git show / git diff. Do not treat them as literature.search. A local "
        "file supplied as the input to a declared scientific capability is not filesystem management: "
        "select that capability directly. Treat an existing @-prefixed local file as one attachment, "
        "including spaces in its path, and bind the clean path without @ or trailing user instruction. "
        "If Active target or Recent activity identifies an earlier task and the user asks for its "
        "status, success/failure, cause, or artifact location, use react_fallback with the native "
        "capability in required_capabilities=[\"task.inspect\"]; do not launch the provider again "
        "or answer from memory. A new produce request — a survey, a code review of current "
        "changes, or an architecture comparison — is not task.inspect even when a similar "
        "earlier task is listed. "
        "If the user instead asks about MANY prior tasks — a time window (\"what did we do in the "
        "last N days\"), a cross-project retrospective, or \"which ones did you not handle well\" — "
        "use react_fallback with required_capabilities=[\"task.review\"] (not task.inspect, which is "
        "for a single task); it enumerates and reads tasks across every workspace. "
        "For answer plus figure, use qa_plus_artifact with "
        "qa.grounded and artifact.figure. For multiple ordered skills, use workflow_steps. "
        "When one role=task provider in the index already spans the request, prefer its "
        "capability over decomposing the work into role=support tools plus synthesis: a task "
        "provider runs a fuller pipeline than a raw retrieval step and a write-up can "
        "reproduce. Decompose only when no single provider covers the goal. "
        "Spanning means the provider's capability is the user's goal, not that a larger "
        "pipeline includes a needed step. literature.search retrieves existing papers "
        "(a literature search, literature review, related-work survey, or source list). "
        "research.ideation generates and pressure-tests research ideas; searching papers "
        "is only its first stage. Do not select research.ideation for a literature-only "
        "request. For literature-only retrieval (list papers or source_ids, no manuscript), "
        "use react_fallback so the native search_literature tool stays in this turn — "
        "do not compile a lone literature.search into a skill. If the user "
        "also wants a written survey, add synthesis.final so this turn keeps "
        "search_literature and write_file — the model writes the manuscript. "
        "Express a new figure, diagram, architecture, flowchart, or schematic as "
        "artifact.figure (default implementation is SVG/PNG). Express a complete "
        "multi-slide deck as slides.generate. Express a user-named single-slide "
        "editable PPTX as figure.editable.pptx. Do not name a concrete provider. "
        "Do not upgrade an ordinary figure to an editable PPTX because a VLM is "
        "configured. Put the user's figure/deck instruction in the matching "
        "capability_inputs object. "
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
        "The scheduled goal is the work THIS conversation asked to run later — the current user "
        "message, or an open schedule clarification's stored goal. Do NOT take the goal from Active "
        "target or Recent activity unless the user refers to that artifact as the work (\"revise this "
        "figure at 6pm\", \"re-run this report tomorrow\"). A message that only supplies a new time, "
        "or answers a pending time clarification, keeps the pending draft's goal; never substitute a "
        "different recent research-ideation report. "
        "In capability_inputs.schedule, put worded time in when={raw_expression,trigger_kind,"
        "constraints}; raw_expression must be copied verbatim from the request. Extract only stated "
        "constraints: a bare hour has day_period=null and no inferred 24-hour system. For an explicit "
        "AM/PM phrase outside English or Chinese, copy that phrase into clock.day_period_evidence. Use cron, "
        "every_seconds, or at instead of when only when the user supplied that exact machine value. "
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
        "Committing on the user's behalf: execution_mode \"ask\" is honoured and "
        "returns a question instead of running anything. Choose it when the "
        "request could reasonably mean more than one thing, or reads as pasted "
        "material rather than an instruction — a wrong reading spends a full "
        "delegated run. Never put a URL or file:// path in an input unless the "
        "user supplied it: resolving a named work to its canonical identifier is "
        "expected, inventing the address it lives at is not, and such a value is "
        "dropped and turned into a question. \"confidence\" is recorded for review "
        "and never gates anything, so report it honestly rather than defensively.\n\n"
        "Every entry in \"missing_inputs\" needs an \"ask\": the one question the "
        "user answers, in their language, answerable in a line. Put the internal "
        "justification in \"reason\". A gap with no \"ask\" is shown to the user as "
        "a generic sentence naming neither what you need nor why. Add a \"default\" "
        "whenever you can name a sensible value: with one the turn runs on it and "
        "declares the assumption in its answer, and only gaps without one stop the "
        "turn to ask.\n\n"
        "\"outputs\" lists ledger deliverable names (draft.section, artifact.figure), "
        "not filesystem paths for write_file.\n\n"
        "JSON shape: {"
        "\"intent_type\": string, "
        "\"confidence\": number, "
        "\"required_capabilities\": string[], "
        "\"workflow_steps\": [{\"id\": string, \"capability\": string, \"depends_on\": string[], \"input\": object, \"reason\": string}], "
        "\"outputs\": string[], "
        "\"capability_inputs\": {\"capability.name\": object}, "
        "\"missing_inputs\": [{\"field\": string, \"ask\": string, \"default\": string, \"reason\": string}], "
        "\"unavailable_services\": string[], "
        "\"unavailable_skills\": string[], "
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
    provider gets one index row, while the most relevant providers, up to
    ``_PLANNER_CONTRACT_SHORTLIST_LIMIT``, receive their complete compact input
    contract.
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
            f"mode={entry.delivery_mode.value}; role={entry.skill_role}; "
            f"capabilities={caps}; "
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


def _lexical_tokens(text: str) -> set[str]:
    """Overlap tokens for text that may have no word delimiters.

    ``\\w+`` treats an unspaced Chinese sentence as a single token, so it
    matched nothing and every provider scored zero — the shortlist then fell
    back to an intent-blind ordering for the whole language. Worse, the Latin
    words embedded in that sentence (``llm``, ``agentic``) were swallowed by the
    same run and lost with it. Split on script boundaries, and index CJK runs as
    character bigrams, the usual stand-in for a segmenter.
    """
    tokens: set[str] = set()
    # ``\w`` matches CJK too, so the Latin alternative has to exclude those
    # ranges explicitly or Latin words with no surrounding spaces re-fuse into
    # the neighbouring ideographic run.
    for run in re.findall(
        rf"[{_CJK_RANGES}]+|[^\W{_CJK_RANGES}]+", text, flags=re.UNICODE
    ):
        if _CJK_RUN.fullmatch(run):
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
        elif len(run) > 1:
            tokens.add(run)
    return tokens


def _planner_relevant_entries(
    registry: SkillRegistry,
    *,
    user_message: str,
    limit: int,
) -> list[Any]:
    """Deterministically shortlist provider contracts without classifying intent."""
    entries = list(registry.list_selectable())
    message = (user_message or "").casefold()
    message_tokens = _lexical_tokens(message)
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
        metadata_tokens = _lexical_tokens(metadata)
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
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    shortlist = [entry for *_rank, entry in scored[: max(0, limit)]]
    if len(shortlist) < limit:
        # Keep the contract list at full breadth: a decisive lexical hit should
        # lead the list, not shrink it to one provider and leave every other
        # step without an input schema. Rank the remainder by declared priority.
        # Schema width says nothing about relevance, and ranking by it put a
        # poster generator ahead of the research provider on every request the
        # lexical pass could not read — which is all of them in a language
        # written without spaces.
        picked = {id(entry) for entry in shortlist}
        filler = sorted(
            (entry for entry in entries if id(entry) not in picked),
            key=lambda entry: (
                -int(entry.priority or 0),
                -_contract_field_count(entry),
                str(entry.name or ""),
            ),
        )
        shortlist.extend(filler[: limit - len(shortlist)])
    return shortlist


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


def _is_gate_gap(item: Any) -> bool:
    """Whether this gap exists because the gate refused a value."""
    return isinstance(item, dict) and item.get("source") == GROUNDING_GAP_SOURCE


def has_refused_value(missing_inputs: Any) -> bool:
    """Whether any gap names a value the gate removed rather than never had.

    The two kinds route differently. A model-declared gap names something the
    agent may well discover on its own, so it must not veto an executable plan.
    A refused value is not discoverable by anyone — the planner made it up and
    it was taken away — so the only honest next move is to ask.
    """
    return any(_is_gate_gap(item) for item in missing_inputs or [])


def _locators(value: Any) -> list[str]:
    """Every address embedded in a planner-supplied value."""
    if not isinstance(value, str):
        return []
    return [match.group(0).rstrip(".,;") for match in _LOCATOR_RE.finditer(value)]


def _invented_path(value: Any, grounding: str) -> str:
    """A filename the request never mentioned and the disk does not have.

    A bare ``attention_is_all_you_need.pdf`` is no less invented than the
    ``https://`` the same plan carried, and in run 138c7b6e it was the one that
    survived the first cut of this gate and became the review's source. Two
    facts clear a path without asking anyone: the user named it, or it is
    actually there.
    """
    if isinstance(value, str) and value.strip().casefold() in grounding:
        return ""
    return missing_file_reference(value)


def _ground_locators(
    data: Any, grounding: str, gaps: list[dict[str, Any]], *, check_paths: bool
) -> dict[str, Any]:
    """Keep only fields whose addresses the user actually supplied.

    An ungrounded field is removed *and* recorded as a gate gap, which forces
    the plan to a question (see ``planner._plan_from_proposal``). Removing it
    alone would be worse than not checking: it turns "this address is wrong"
    into "there is no address", and nothing downstream can tell the difference.
    The fabricated value stays in ``reason`` for the event log while ``ask``
    carries the plain question the user answers.
    """
    if not isinstance(data, dict):
        return {}
    kept: dict[str, Any] = {}
    for key, value in data.items():
        kind = "link"
        invented = [loc for loc in _locators(value) if loc.casefold() not in grounding]
        if not invented and check_paths:
            invented = [path for path in [_invented_path(value, grounding)] if path]
            kind = "file"
        if not invented:
            kept[key] = value
            continue
        # Name the *kind* of thing that is missing, not the contract's field.
        # Half the providers call their source slot ``input``, and "the input to
        # use" asks the user to answer a question they were never shown.
        slot = str(key).replace("_", " ")
        gaps.append(
            {
                "field": str(key),
                "ask": f"the {kind} to use" if slot in {"input", kind} else f"the {kind} to use for {slot}",
                "reason": (
                    f"planner supplied {invented[0]} for {key}, "
                    "which appears nowhere in the request"
                ),
                "source": GROUNDING_GAP_SOURCE,
            }
        )
    return kept


def _refuse_invented_locators(
    proposal: ModelPlanProposal, *, user_message: str, context_summary: str
) -> ModelPlanProposal:
    """Turn every address the request never mentioned into a question."""
    grounding = f"{user_message}\n{context_summary}".casefold()
    gaps: list[dict[str, Any]] = []
    capability_inputs = {
        capability: _ground_locators(value, grounding, gaps, check_paths=True)
        for capability, value in proposal.capability_inputs.items()
    }
    steps = [
        {
            **step,
            "input": _ground_locators(
                step["input"],
                grounding,
                gaps,
                # A dependent step's filename is a claim about the plan, not
                # about the disk: the step that writes it has not run, so it
                # cannot exist yet. Whether the promise holds is settled when
                # the upstream step survives resolution — the workflow
                # builder's contract, not this gate's.
                check_paths=not step.get("depends_on"),
            ),
        }
        if isinstance(step.get("input"), dict)
        else step
        for step in proposal.workflow_steps
    ]
    if not gaps:
        return proposal
    seen = {
        str(item.get("field"))
        for item in proposal.missing_inputs
        if isinstance(item, dict)
    }
    added: list[dict[str, Any]] = []
    for gap in gaps:
        if gap["field"] in seen:
            continue
        seen.add(gap["field"])
        added.append(gap)
    return replace(
        proposal,
        capability_inputs=capability_inputs,
        workflow_steps=steps,
        # Gate gaps go first: the cap must never be what silently discards the
        # one gap that has to be asked.
        missing_inputs=[*added, *proposal.missing_inputs][:5],
    )


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
