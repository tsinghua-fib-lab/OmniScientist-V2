"""Native final synthesis executor for research workflows.

Writing is a deliverable, not a mandatory skill implementation.  When no
explicit writing provider is selected the runtime still owes the user real
content, so :func:`run_native_synthesis` follows the universal executor ladder:

    dedicated writing skill  →  base model (LLM) draft  →  deterministic template

The template rung is an *offline fallback only*: it keeps the workflow contract
stable without a model, but its output is honestly reported as a degraded draft
(``status="partial"`` → workflow step ``degraded``), never as a finished
deliverable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import resources
from typing import Any

from omni.runtime.deliverable_assessment import make_provider_binding_id


class EvidencePolicy(StrEnum):
    """Document-level evidence posture for the whole synthesis draft."""

    GROUNDED = "grounded"
    CONTEXTUAL = "contextual"
    DEGRADED = "degraded"


class ConclusionSupport(StrEnum):
    """Per-conclusion evidence label."""

    SOURCED = "sourced"
    INFERRED = "inferred"
    INSUFFICIENT = "insufficient"


_SUPPORT_LABELS: dict[ConclusionSupport, str] = {
    ConclusionSupport.SOURCED: "sourced",
    ConclusionSupport.INFERRED: "inferred",
    ConclusionSupport.INSUFFICIENT: "insufficient evidence",
}


@dataclass(frozen=True, slots=True)
class SynthesisContract:
    topic: str
    deliverable: str = "draft.section"
    audience: str = "researcher"
    style: str = "academic"
    language: str = "auto"
    evidence_policy: EvidencePolicy = EvidencePolicy.DEGRADED
    summaries: list[str] = field(default_factory=list)
    provenance: dict[str, list[str]] = field(default_factory=dict)
    conclusions: list[dict[str, Any]] = field(default_factory=list)


class SynthesisTemplateRegistry:
    """Small template registry for native synthesis fallbacks.

    Domain-specific prose should live in templates/domain packs or external
    writing providers. The built-in template only expresses evidence hygiene.
    """

    def __init__(self) -> None:
        self._templates: dict[str, str] = {
            "draft.section": _load_template("draft.section.md") or _FALLBACK_SECTION_TEMPLATE
        }

    def render(self, contract: SynthesisContract) -> str:
        template = self._templates.get(contract.deliverable) or self._templates["draft.section"]
        evidence_note = (
            "; ".join(contract.summaries[:4])
            if contract.summaries
            else "No upstream evidence is available; this is a degraded draft."
        )
        return template.format_map(
            _SafeTemplateContext(
                topic=contract.topic,
                deliverable=contract.deliverable,
                audience=contract.audience,
                style=contract.style,
                language=contract.language,
                evidence_note=evidence_note,
                evidence_level=contract.evidence_policy.value,
                provenance_note=_provenance_note(contract.provenance),
                conclusions_block=_conclusions_block(contract.conclusions),
            )
        )


def execute_final_synthesis(goal: str, step: dict[str, Any], results_by_id: dict[str, Any]) -> dict[str, Any]:
    """Create a lightweight research draft from completed upstream steps."""
    step_input = step.get("input") if isinstance(step.get("input"), dict) else {}
    deliverable = str(step.get("deliverable") or step_input.get("deliverable") or "draft.section")
    # A planner-provided title is a much better heading than a truncated goal
    # sentence (the goal often contains instructions, not a topic).
    topic = str(
        step_input.get("topic")
        or step_input.get("title")
        or _topic_from_goal(goal)
        or "Current research topic"
    )
    summaries = _upstream_summaries(step, results_by_id)
    provenance = _collect_provenance(results_by_id)
    conclusions = _label_conclusions(step, results_by_id)
    has_research_objects = bool(provenance["source_ids"] or provenance["claim_ids"] or provenance["evidence_ids"])
    status = "ok" if summaries else "partial"
    evidence_level = (
        EvidencePolicy.GROUNDED
        if has_research_objects
        else EvidencePolicy.CONTEXTUAL
        if summaries
        else EvidencePolicy.DEGRADED
    )
    contract = SynthesisContract(
        topic=topic,
        deliverable=deliverable,
        audience=str(step_input.get("audience") or "researcher"),
        style=str(step_input.get("style") or "academic"),
        language=str(step_input.get("language") or "auto"),
        evidence_policy=evidence_level,
        summaries=summaries,
        provenance=provenance,
        conclusions=conclusions,
    )
    text = SynthesisTemplateRegistry().render(contract)
    return {
        "status": status,
        "summary": f"Generated research deliverable {deliverable}",
        "deliverable": deliverable,
        "topic": topic,
        "text": text,
        "draft_markdown": text,
        "evidence_level": evidence_level.value,
        "provenance": provenance,
        "provenance_labels": conclusions,
        "source_steps": [str(step_id) for step_id in results_by_id],
        "upstream_summaries": summaries,
    }


# --- LLM-first native synthesis (universal executor ladder) -----------------

# Below this size a "draft" is a stub (scripted test doubles, refusals, error
# strings); fall back to the labelled template instead of shipping it.
_MIN_LLM_DRAFT_CHARS = 120
_LLM_DRAFT_TIMEOUT_S = 240.0
_MATERIAL_PER_STEP_CHARS = 4000
_MATERIAL_TOTAL_CHARS = 16000
_MATERIAL_KEYS = (
    "title",
    "authors",
    "summary",
    "abstract",
    "answer",
    "text",
    "draft_markdown",
    "caption",
    "abs_url",
    "url",
    "published",
    "outcome",
)

# Public: test doubles and the eval harness identify native-synthesis calls by
# this exact system prompt instead of string-matching prompt prose.
SYNTHESIS_SYSTEM_PROMPT = (
    "You are OmniScientist's research writing executor. Write the requested "
    "research deliverable as clean Markdown, starting with a single `#` title "
    "line. Ground every substantive statement in the provided upstream "
    "materials; cite sources inline (arXiv id / DOI / URL) when they appear in "
    "the material. Statements without upstream support must be explicitly "
    "marked as unverified. Never invent citations, numbers, or experimental "
    "results. Write in the same language as the user goal unless the request "
    "names another language."
)

# Native executors are providers too.  Keep their wire contract next to the
# implementation so workflow dispatch cannot silently bypass the same typed
# boundary used for catalog skills and tools.
NATIVE_SYNTHESIS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal", "step", "upstream_results"],
    "properties": {
        "goal": {"type": "string"},
        "step": {"type": "object"},
        "upstream_results": {"type": "object"},
    },
    "additionalProperties": False,
}

NATIVE_SYNTHESIS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "status",
        "summary",
        "deliverable",
        "topic",
        "text",
        "draft_markdown",
        "evidence_level",
        "provenance",
        "source_steps",
        "upstream_summaries",
        "synthesis_mode",
        "deliverable_assessment",
    ],
    "properties": {
        "status": {"enum": ["ok", "partial"]},
        "summary": {"type": "string", "minLength": 1},
        "deliverable": {"type": "string", "minLength": 1},
        "topic": {"type": "string", "minLength": 1},
        "text": {"type": "string", "minLength": 1},
        "draft_markdown": {"type": "string", "minLength": 1},
        "evidence_level": {"enum": ["grounded", "contextual", "degraded"]},
        "provenance": {
            "type": "object",
            "required": ["source_ids", "claim_ids", "evidence_ids", "artifact_ids"],
            "properties": {
                "source_ids": {"type": "array", "items": {"type": "string"}},
                "claim_ids": {"type": "array", "items": {"type": "string"}},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "artifact_ids": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": True,
        },
        "provenance_labels": {"type": "array", "items": {"type": "object"}},
        "source_steps": {"type": "array", "items": {"type": "string"}},
        "upstream_summaries": {"type": "array", "items": {"type": "string"}},
        "synthesis_mode": {"enum": ["llm", "template_fallback"]},
        "deliverable_assessment": {
            "type": "object",
            "required": [
                "schema",
                "deliverable_id",
                "provider_binding_id",
                "provider",
                "contract_hash",
                "step_id",
                "feedback",
                "status",
                "retryable",
                "effective_inputs",
                "criteria",
            ],
            "properties": {
                "schema": {
                    "const": "omni.deliverable-assessment/v1",
                },
                "deliverable_id": {"type": "string", "minLength": 1},
                "provider_binding_id": {"type": "string", "minLength": 1},
                "provider": {"const": "synthesis.final"},
                "contract_hash": {"type": "string", "minLength": 1},
                "step_id": {"type": "string", "minLength": 1},
                "feedback": {"type": "string", "minLength": 1},
                "status": {
                    "enum": ["passed", "degraded", "failed", "unknown"],
                },
                "retryable": {"type": "boolean"},
                "effective_inputs": {"type": "object"},
                "criteria": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["criterion_id", "status"],
                        "properties": {
                            "criterion_id": {"type": "string", "minLength": 1},
                            "status": {
                                "enum": [
                                    "passed",
                                    "degraded",
                                    "failed",
                                    "unknown",
                                ]
                            },
                            "summary": {"type": "string"},
                            "evidence_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "summary": {"type": "string"},
            },
        },
        "synthesis_error": {"type": "string"},
        "warning": {"type": "string"},
        "artifacts": {"type": "array", "items": {"type": "object"}},
        "report_uri": {"type": "string"},
    },
    "additionalProperties": True,
}

NATIVE_SYNTHESIS_QUALITY_CONTRACT: dict[str, Any] = {
    "checks": ["draft_content_present"],
    "assessment_required": True,
    "assessment_schema": "omni.deliverable-assessment/v1",
    "retry": {
        "max_attempts": 1,
        "provider_replay_safe_required": True,
        "side_effect_policy": "idempotency_key_required",
    },
}


async def run_native_synthesis(
    goal: str,
    step: dict[str, Any],
    results_by_id: dict[str, Any],
    *,
    llm: Any = None,
    artifacts: Any = None,
    session_id: str = "",
    task_id: str = "",
    subtask_id: str = "",
    workflow_run_id: str = "",
) -> dict[str, Any]:
    """Produce the writing deliverable via the universal executor ladder.

    The base model writes the draft from the *full* upstream results; the
    deterministic template is the offline rung and is honestly downgraded to
    ``status="partial"`` (→ workflow step ``degraded``) so a skeleton never
    masquerades as a finished deliverable. When an artifact store is wired the
    draft is persisted under ``artifacts/report/`` so the user receives a file,
    not just inline text.
    """
    result = execute_final_synthesis(goal, step, results_by_id)
    draft, llm_error = await _llm_draft(goal, step, results_by_id, result, llm)
    if draft:
        result["text"] = draft
        result["draft_markdown"] = draft
        result["synthesis_mode"] = "llm"
        result["summary"] = f"Drafted research deliverable {result['deliverable']} from upstream results."
    else:
        result["synthesis_mode"] = "template_fallback"
        if llm_error:
            # Audit why the model rung failed (timeout, provider error, stub
            # output) so a degraded draft is diagnosable, not just labelled.
            result["synthesis_error"] = llm_error
        if result.get("status") == "ok":
            result["status"] = "partial"
        result["warning"] = (
            "No model draft was produced for final synthesis; a deterministic "
            "template draft was emitted instead (degraded quality)."
        )
    artifact = await _store_draft_artifact(
        result,
        artifacts,
        session_id=session_id,
        task_id=task_id,
        subtask_id=subtask_id,
        workflow_run_id=workflow_run_id,
    )
    if artifact:
        result["artifacts"] = [artifact]
        result["report_uri"] = artifact["uri"]
    result["deliverable_assessment"] = _draft_deliverable_assessment(
        result,
        step,
    )
    return result


def _draft_deliverable_assessment(
    result: dict[str, Any],
    step: dict[str, Any],
) -> dict[str, Any]:
    """Assess the effective native draft without trusting mere key presence."""

    text = str(result.get("draft_markdown") or "").strip()
    synthesis_mode = str(result.get("synthesis_mode") or "")
    deliverable_id = str(
        step.get("deliverable_id")
        or step.get("deliverable")
        or result.get("deliverable")
        or step.get("id")
        or "draft"
    )
    if len(text) < _MIN_LLM_DRAFT_CHARS:
        status = "failed"
        summary = (
            f"Draft is too short to be usable "
            f"({len(text)} chars < {_MIN_LLM_DRAFT_CHARS})."
        )
    elif synthesis_mode == "template_fallback":
        status = "degraded"
        summary = "A usable deterministic template was emitted instead of model-written content."
    else:
        status = "passed"
        summary = f"Model-written draft contains {len(text)} non-whitespace characters."

    evidence_refs = [
        str(result.get("report_uri") or ""),
        *[
            str(item)
            for values in (result.get("provenance") or {}).values()
            if isinstance(values, list)
            for item in values
        ],
    ]
    evidence_refs = [item for item in evidence_refs if item]
    step_input = step.get("input") if isinstance(step.get("input"), dict) else {}
    step_id = str(step.get("id") or deliverable_id)
    contract_hash = str(step.get("provider_contract_hash") or "")
    if not contract_hash:
        contract_hash = hashlib.sha256(
            json.dumps(
                {
                    "name": "synthesis.final",
                    "source": "native",
                    "version": "1",
                    "input_schema": NATIVE_SYNTHESIS_INPUT_SCHEMA,
                    "output_schema": NATIVE_SYNTHESIS_OUTPUT_SCHEMA,
                    "quality_contract": NATIVE_SYNTHESIS_QUALITY_CONTRACT,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    effective_inputs = {
        "deliverable": str(result.get("deliverable") or ""),
        "topic": str(result.get("topic") or ""),
        "audience": str(step_input.get("audience") or "researcher"),
        "style": str(step_input.get("style") or "academic"),
        "language": str(step_input.get("language") or "auto"),
        "synthesis_mode": synthesis_mode,
    }
    return {
        "schema": "omni.deliverable-assessment/v1",
        "deliverable_id": deliverable_id,
        "provider_binding_id": str(
            step.get("provider_binding_id")
            or make_provider_binding_id(
                provider_type="native_executor",
                provider_name="synthesis.final",
                deliverable_id=deliverable_id,
            )
        ),
        "provider": "synthesis.final",
        "contract_hash": contract_hash,
        "step_id": step_id,
        "feedback": summary,
        "status": status,
        # A model error/stub can improve on a subsequent stochastic attempt.
        # Offline template fallback without a failed model rung cannot.
        "retryable": bool(result.get("synthesis_error")),
        "effective_inputs": effective_inputs,
        "criteria": [
            {
                "criterion_id": "draft_content_present",
                "status": status,
                "summary": summary,
                "evidence_refs": evidence_refs,
            }
        ],
        "evidence_refs": evidence_refs,
        "summary": summary,
    }


async def _llm_draft(
    goal: str,
    step: dict[str, Any],
    results_by_id: dict[str, Any],
    base_result: dict[str, Any],
    llm: Any,
) -> tuple[str, str]:
    """Return ``(draft, error_note)``.

    ``error_note`` is non-empty only when a model was present but its rung
    failed (exception, timeout, or stub-length output); running without a
    model is the expected offline path, not an error.
    """
    if llm is None:
        return "", ""
    step_input = step.get("input") if isinstance(step.get("input"), dict) else {}
    material = _upstream_material(step, results_by_id)
    conclusions = _conclusions_block(base_result.get("provenance_labels") or [])
    provenance_note = _provenance_note(base_result.get("provenance") or {
        "source_ids": [], "claim_ids": [], "evidence_ids": [], "artifact_ids": [],
    })
    user = (
        f"User goal:\n{goal}\n\n"
        f"Deliverable: {base_result.get('deliverable')}\n"
        f"Topic: {base_result.get('topic')}\n"
        f"Audience: {step_input.get('audience') or 'researcher'}; "
        f"style: {step_input.get('style') or 'academic'}; "
        f"language: {step_input.get('language') or 'auto (match the goal language)'}\n\n"
        f"Upstream materials (completed workflow steps):\n{material or '(none)'}\n\n"
        f"Evidence support classification:\n{conclusions}\n\n"
        f"Provenance: {provenance_note}\n\n"
        "Write the full deliverable now."
    )
    try:
        text = await asyncio.wait_for(
            llm.chat(SYNTHESIS_SYSTEM_PROMPT, user, temperature=0.3),
            timeout=_LLM_DRAFT_TIMEOUT_S,
        )
    except TimeoutError:
        return "", f"model draft timed out after {_LLM_DRAFT_TIMEOUT_S:.0f}s"
    except Exception as exc:  # noqa: BLE001 - any model failure falls to the template rung
        return "", f"{type(exc).__name__}: {exc}"[:300]
    text = (text or "").strip()
    if len(text) < _MIN_LLM_DRAFT_CHARS:
        return "", (
            f"model draft too short ({len(text)} chars < {_MIN_LLM_DRAFT_CHARS}); "
            "treated as a stub"
        )
    return text, ""


def _upstream_material(step: dict[str, Any], results_by_id: dict[str, Any]) -> str:
    """Full (bounded) upstream results for the writing prompt.

    Unlike :func:`_upstream_summaries` (240-char UI teasers) the writer needs
    the actual content — e.g. a fetched abstract in full — so each step
    contributes up to ``_MATERIAL_PER_STEP_CHARS`` characters.
    """
    depends = [str(item) for item in step.get("depends_on") or []]
    keys = depends or list(results_by_id)
    blocks: list[str] = []
    used = 0
    for key in keys:
        body = _material_from_result(results_by_id.get(key), limit=_MATERIAL_PER_STEP_CHARS)
        if not body:
            continue
        block = f"### Step `{key}`\n{body}"
        if used + len(block) > _MATERIAL_TOTAL_CHARS:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def _material_from_result(result: Any, *, limit: int) -> str:
    if isinstance(result, str):
        return result.strip()[:limit]
    if not isinstance(result, dict):
        return ""
    picked: dict[str, Any] = {}
    for key in _MATERIAL_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            picked[key] = value.strip()
        elif isinstance(value, (list, dict)) and value:
            picked[key] = value
    artifact_titles = [
        str(item.get("title") or "")
        for item in (result.get("artifacts") or [])
        if isinstance(item, dict) and item.get("title")
    ]
    if artifact_titles:
        picked["artifacts"] = artifact_titles
    if not picked:
        try:
            return json.dumps(result, ensure_ascii=False)[:limit]
        except (TypeError, ValueError):
            return str(result)[:limit]
    try:
        return json.dumps(picked, ensure_ascii=False, indent=1)[:limit]
    except (TypeError, ValueError):
        return str(picked)[:limit]


async def _store_draft_artifact(
    result: dict[str, Any],
    artifacts: Any,
    *,
    session_id: str,
    task_id: str = "",
    subtask_id: str,
    workflow_run_id: str,
) -> dict[str, str] | None:
    if artifacts is None:
        return None
    text = str(result.get("draft_markdown") or result.get("text") or "")
    if not text.strip():
        return None
    topic = str(result.get("topic") or "Research draft")
    title = f"{topic} Draft"
    try:
        stored = await artifacts.put_bytes(
            text.encode("utf-8"),
            kind="report",
            title=title,
            ext="md",
            mime="text/markdown",
            session_id=session_id,
            task_id=task_id,
            subtask_id=subtask_id,
            workflow_run_id=workflow_run_id,
            meta={
                "deliverable": str(result.get("deliverable") or ""),
                "synthesis_mode": str(result.get("synthesis_mode") or ""),
            },
        )
    except Exception:  # noqa: BLE001 - a storage hiccup must not fail the draft
        return None
    return {
        "title": title,
        "format": "md",
        "uri": stored.uri,
        "path": str(stored.path),
        "mime": stored.mime,
        "size_bytes": str(stored.size_bytes),
    }


def _upstream_summaries(step: dict[str, Any], results_by_id: dict[str, Any]) -> list[str]:
    depends = [str(item) for item in step.get("depends_on") or []]
    keys = depends or list(results_by_id)
    summaries: list[str] = []
    for key in keys:
        result = results_by_id.get(key)
        summary = _summary_from_result(result)
        if summary:
            summaries.append(f"{key}: {summary}")
    return summaries


def _summary_from_result(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("summary", "text", "answer"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:240]
        outcome = result.get("outcome")
        if isinstance(outcome, dict):
            for key in ("summary", "abstract", "title"):
                value = outcome.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:240]
    if isinstance(result, str):
        return result.strip()[:240]
    return ""


def _label_conclusions(step: dict[str, Any], results_by_id: dict[str, Any]) -> list[dict[str, Any]]:
    """Classify each upstream conclusion as sourced / inferred / insufficient.

    A conclusion is sourced when its step recorded structured evidence and
    inferred when it only carries an upstream summary. An explicit insufficient
    marker prevents an empty evidence base from appearing as settled fact.
    """
    depends = [str(item) for item in step.get("depends_on") or []]
    keys = depends or list(results_by_id)
    labels: list[dict[str, Any]] = []
    for key in keys:
        result = results_by_id.get(key)
        summary = _summary_from_result(result)
        if not summary:
            continue
        refs = _collect_provenance({key: result})
        evidence_refs = {
            "source_ids": refs["source_ids"],
            "claim_ids": refs["claim_ids"],
            "evidence_ids": refs["evidence_ids"],
        }
        has_evidence = any(evidence_refs.values())
        support = ConclusionSupport.SOURCED if has_evidence else ConclusionSupport.INFERRED
        labels.append(
            {
                "id": key,
                "support": support.value,
                "label": _SUPPORT_LABELS[support],
                "summary": summary,
                "refs": evidence_refs,
            }
        )
    if not labels:
        labels.append(
            {
                "id": "",
                "support": ConclusionSupport.INSUFFICIENT.value,
                "label": _SUPPORT_LABELS[ConclusionSupport.INSUFFICIENT],
                "summary": "No usable upstream conclusion was produced; treat this point as unverified.",
                "refs": {"source_ids": [], "claim_ids": [], "evidence_ids": []},
            }
        )
    return labels


def _conclusions_block(conclusions: list[dict[str, Any]]) -> str:
    if not conclusions:
        return "Conclusion support: no conclusions are available to classify."
    lines = ["Conclusion support (sourced / inferred / insufficient evidence):"]
    for item in conclusions:
        label = str(item.get("label") or "inferred")
        summary = str(item.get("summary") or "").strip()
        key = str(item.get("id") or "").strip()
        prefix = f"{key}: " if key else ""
        refs = item.get("refs") if isinstance(item.get("refs"), dict) else {}
        ref_note = _conclusion_ref_note(refs)
        lines.append(f"- [{label}] {prefix}{summary}{ref_note}")
    return "\n".join(lines)


def _conclusion_ref_note(refs: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("source_ids", "claim_ids", "evidence_ids"):
        values = refs.get(key) or []
        if values:
            label = key.removesuffix("_ids")
            parts.append(f"{label}={', '.join(str(v)[:8] for v in values[:3])}")
    if not parts:
        return " (no structured source/claim/evidence; inferred)"
    return f" ({'; '.join(parts)})"


def _collect_provenance(results_by_id: dict[str, Any]) -> dict[str, list[str]]:
    out = {
        "source_ids": [],
        "claim_ids": [],
        "evidence_ids": [],
        "artifact_ids": [],
        "artifact_uris": [],
    }

    def add(key: str, value: Any) -> None:
        if not value:
            return
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = str(item)
            if text and text not in out[key]:
                out[key].append(text)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("source_ids", "claim_ids", "evidence_ids", "artifact_ids"):
                add(key, value.get(key))
            for key, raw in value.items():
                if isinstance(raw, str) and raw.startswith("artifact://"):
                    add("artifact_uris", raw)
                    add("artifact_ids", raw.removeprefix("artifact://"))
                elif key.endswith("_uri") and isinstance(raw, str) and raw.startswith("artifact://"):
                    add("artifact_uris", raw)
                elif isinstance(raw, (dict, list)):
                    walk(raw)
            research = value.get("research")
            if isinstance(research, dict):
                walk(research)
            artifacts = value.get("artifacts")
            if isinstance(artifacts, list):
                for artifact in artifacts:
                    if isinstance(artifact, dict):
                        add("artifact_uris", artifact.get("uri"))
                        uri = artifact.get("uri")
                        if isinstance(uri, str) and uri.startswith("artifact://"):
                            add("artifact_ids", uri.removeprefix("artifact://"))
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(results_by_id)
    return out


def _provenance_note(provenance: dict[str, list[str]]) -> str:
    parts: list[str] = []
    if provenance["source_ids"]:
        parts.append(f"source={', '.join(item[:8] for item in provenance['source_ids'][:5])}")
    if provenance["claim_ids"]:
        parts.append(f"claim={', '.join(item[:8] for item in provenance['claim_ids'][:5])}")
    if provenance["evidence_ids"]:
        parts.append(f"evidence={', '.join(item[:8] for item in provenance['evidence_ids'][:5])}")
    if provenance["artifact_ids"]:
        parts.append(f"artifact={', '.join(item[:8] for item in provenance['artifact_ids'][:5])}")
    return "; ".join(parts) if parts else (
        "No structured source/claim/evidence was detected; add citations or run verification."
    )


def _topic_from_goal(goal: str) -> str:
    text = " ".join((goal or "").split())
    return text[:80]


class _SafeTemplateContext(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _load_template(name: str) -> str:
    try:
        path = resources.files("omni.data").joinpath("synthesis_templates", name)
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return ""


_FALLBACK_SECTION_TEMPLATE = """## {topic}: research section draft

This draft combines the upstream workflow results into an editable research section. Available support: {evidence_note}

{conclusions_block}

The final section should state the problem, method, evidence, limitations, and validation plan. Prefer recorded source/claim/evidence objects and label unsupported statements as unverified.

Evidence status: {evidence_level}. {provenance_note}
"""
