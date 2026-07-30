"""In-lane grounded input resolution — "look up before ask/error".

When a typed workflow step needs a strong identifier (e.g. ``arxiv-fetch`` wants
an ``arxiv_id``) but the planner only had a free-text title, the deterministic
lane should *resolve the identifier itself* before degrading, asking, or handing
off — exactly what a model-driven agent (Codex/Claude Code) does by acting and
looking up. Resolving in-lane keeps the typed step (and its provenance chain)
instead of rewriting the workflow into a lossy free-text search.

Design constraints:
* Offline/tests never hit the network: the searcher is injectable and the default
  swallows connectivity errors, returning "unresolved" so recovery falls through.
* Never bind a *wrong* identifier: a hit is only accepted when its title
  overlaps the extracted query strongly, so a vague query can't silently pin an
  unrelated paper.
* Deterministic and auditable: every bind is recorded as a resolution record the
  orchestrator turns into an ``input.resolved`` event.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.plan_validator import PlanValidationResult, PlanValidator
from omni.agent.resolver_evidence import (
    materialize_resolver_evidence,
    resolver_value_matches_user,
    seal_resolver_evidence,
)
from omni.core.field_contract import field_resolver
from omni.core.field_resolvers import (
    has_searcher,
    search_field_candidates,
)
from omni.skills_runtime.registry import SkillRegistry, resolve_step_entry

logger = logging.getLogger(__name__)

# A contiguous run of Latin words (paper titles are typically English even inside
# a CJK request), tolerating in-title punctuation.
_LATIN_RUN_RE = re.compile(r"[A-Za-z0-9]+(?:[ '’\-:&.][A-Za-z0-9]+)*")
_QUOTED_RE = re.compile(
    r"[\"'“”‘’「」『』《》]([^\"'“”‘’「」『』《》]{3,160})[\"'“”‘’「」『』《》]"
)
_WORD_RE = re.compile(r"[A-Za-z0-9]+")

# Identity grounding needs both adequate evidence and an unambiguous winner.
_MATCH_THRESHOLD = 0.72
_MATCH_MARGIN = 0.12
_MIN_INFORMATION_TOKENS = 2
# Execution-time verify-by-fetch is deliberately lenient: it only flags a *gross*
# title mismatch (a different paper), never a paraphrase, and never fails a step.
_FETCH_TITLE_MISMATCH_FLOOR = 0.5
# Id-like keys stripped before deriving the requested entity, so verify-by-fetch
# compares the fetched title against the *title the user named*, not the id itself.
_IDENTIFIER_ID_KEYS = ("identifier", "arxiv_id", "id", "doi")
_TITLE_STOPWORDS = {
    "a",
    "all",
    "an",
    "and",
    "are",
    "for",
    "in",
    "is",
    "of",
    "on",
    "the",
    "to",
    "with",
    "you",
}

# ``searcher(field_format, query) -> list[(identifier, title)]``.
Searcher = Callable[[str, str], Awaitable[list[tuple[str, str]]]]


@dataclass(slots=True)
class ResolutionRecord:
    step_id: str
    field: str
    field_format: str
    query: str
    value: str
    title: str
    via: str


def extract_entity_query(step: dict, user_message: str) -> str:
    """Return the best entity/title search query for a step (never the whole goal).

    Prefers a scalar the planner already bound, then the step's own text, then the
    user message — but always reduces each candidate to a quoted span or the
    longest Latin title run, so a multi-clause request that embeds an English
    paper title inside other-language text yields ``Attention Is All You Need``
    rather than the entire sentence.
    """
    params = step.get("input") if isinstance(step.get("input"), dict) else {}
    for key in ("identifier", "paper", "title", "paper_title", "query", "target", "input"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            span = _title_span(value)
            if span:
                return span
    # ``reason`` is intentionally excluded: the planner fills it with a rationale
    # (or, as a fallback, the capability id like ``paper.fetch.arxiv``), which is
    # never an entity title and would pin the search to the wrong string.
    for key in ("instruction", "title", "description"):
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            span = _title_span(value)
            if span:
                return span
    return _title_span(user_message or "")


def fetched_identifier_title_warning(
    step: dict,
    goal: str,
    result: dict,
) -> str | None:
    """Advisory when a fetched identifier's title clearly differs from the request.

    Verify-by-fetch — the way Codex / Claude Code / OpenClaw trust a tool argument
    and let the *result* prove it. After a ``paper.fetch`` step resolves a bound id
    at execution, compare the fetched title against the entity the user actually
    named (independent of the id). Returns a human-readable advisory on a gross
    mismatch, else ``None``.

    It is intentionally **non-blocking**: the caller attaches it as a warning and
    never fails the step, so a valid-but-wrong id is *surfaced* rather than
    silently swapped (which a plan-time search would have done, sometimes wrongly).
    """
    if not isinstance(result, dict) or str(result.get("status") or "") != "ok":
        return None
    fetched_title = str(result.get("title") or "").strip()
    if not fetched_title:
        return None
    params = step.get("input") if isinstance(step.get("input"), dict) else {}
    # The requested entity must be independent of the bound id, so strip id-like
    # keys before extracting the query; otherwise the id would "verify" itself.
    query_step = {
        **step,
        "input": {
            key: value
            for key, value in params.items()
            if key not in _IDENTIFIER_ID_KEYS
        },
    }
    entity = extract_entity_query(query_step, goal)
    if not entity or len(_information_tokens(entity)) < _MIN_INFORMATION_TOKENS:
        # No independent title to check against (e.g. the request only named an id).
        return None
    resolved_id = str(result.get("arxiv_id") or result.get("id") or "").strip()
    if resolved_id and resolved_id.casefold() in entity.casefold():
        # The "entity" is just the id-bearing phrase (the request named the id,
        # not an independent title), so there is nothing to cross-check.
        return None
    if _title_overlap(entity, fetched_title) >= _FETCH_TITLE_MISMATCH_FLOOR:
        return None
    return (
        f"fetched title '{fetched_title}' does not match the requested "
        f"'{entity}' — the bound identifier may reference a different paper"
    )


async def resolve_identifier_fields(
    plan: IntentPlan,
    validation: PlanValidationResult,
    *,
    registry: SkillRegistry,
    searcher: Searcher | None = None,
) -> tuple[IntentPlan, PlanValidationResult, list[ResolutionRecord]]:
    """Resolve resolvable identifier fields in-lane; return (plan, validation, records).

    For each ``step_input_contract`` finding whose missing field is a resolvable
    identifier (e.g. ``arxiv_id``), extract the entity title, look up a concrete
    id, and bind it into the step — keeping the typed step. The plan is
    re-validated once any binding happens. Nothing is bound for a query that does
    not confidently match a hit, so recovery can still take over.
    """
    if plan.intent_type != IntentType.WORKFLOW:
        return plan, validation, []
    findings = [
        f
        for f in validation.findings
        if f.code == "step_input_contract" and f.step_id and f.missing_field
    ]

    search = searcher or _default_searcher
    steps_by_id = {str(step.get("id") or ""): step for step in plan.workflow_steps}
    step_indexes = {
        str(step.get("id") or ""): index
        for index, step in enumerate(plan.workflow_steps)
    }
    # Only resolve genuinely *missing/invalid* identifier fields (the ones that
    # raised a ``step_input_contract`` finding). An already-bound, syntactically
    # valid id is trusted at plan time — never re-searched or silently swapped
    # here. A blocking, low-precision plan-time title search over bound ids was
    # the regression that made "get <paper> and draw <figure>" tasks fail: arXiv
    # search is slow (~tens of seconds) and imprecise, so it dead-ended good ids.
    # A valid-but-wrong id is instead caught at execution by verify-by-fetch
    # (``fetched_identifier_title_warning``), the way Codex/Claude Code let the
    # tool result prove the argument.
    targets: list[tuple[str, str, str]] = list(
        dict.fromkeys(
            (finding.step_id, finding.skill_name, finding.missing_field)
            for finding in findings
        )
    )
    if not targets:
        return plan, validation, []

    # Resolution may be called with structural validation only. Materialize
    # host fact obligations here as well so resolver evidence always seals the
    # schema-derived record, independent of a model constraint or caller order.
    materialize_resolver_evidence(plan, registry)

    records: list[ResolutionRecord] = []
    for step_id, skill_name, field_name in targets:
        step = steps_by_id.get(step_id)
        if step is None:
            continue
        entry = resolve_step_entry(registry, step) if skill_name else None
        field_format = _field_format(entry, field_name)
        if not has_searcher(field_format):
            continue
        params = step.get("input") if isinstance(step.get("input"), dict) else {}
        current = str(params.get(field_name) or "").strip()
        if current and resolver_value_matches_user(
            _field_format(entry, field_name),
            value=current,
            user_message=plan.user_message,
        ):
            # Local normalization already proved this exact user-authored fact.
            # A title search must not override an explicit identifier.
            continue
        query_step = step
        if current:
            # Verification needs evidence independent of the identifier being
            # checked. Otherwise ``extract_entity_query`` simply returns the
            # already-bound id and a syntactically valid but wrong id bypasses
            # grounding forever.
            query_step = {
                **step,
                "input": {
                    key: value
                    for key, value in params.items()
                    if key != field_name
                },
            }
        query = extract_entity_query(query_step, plan.user_message)
        if not query:
            continue
        # A bare id contains no independent semantic evidence to ground against.
        if current and query == current:
            continue
        try:
            hits = await search(field_format, query)
        except Exception:  # noqa: BLE001 — offline/transient must not break the turn
            logger.debug("identifier search failed field=%s", field_format, exc_info=True)
            hits = []
        chosen = _pick_hit(query, hits)
        if chosen is None:
            continue
        value, title = chosen
        if current != value:
            params = dict(params)
            params[field_name] = value
            step["input"] = params
            # Force recompilation so the freshly bound identifier is re-validated.
            step["input_compiled"] = False
        field_path = (
            f"/workflow_steps/{step_indexes.get(step_id, 0)}/input/"
            f"{field_name.replace('~', '~0').replace('/', '~1')}"
        )
        sealed = seal_resolver_evidence(
            plan,
            registry,
            field_path=field_path,
            value=value,
            verification_mode="grounded_search",
            source=f"{field_format}.verify" if current else f"{field_format}.search",
        )
        if not sealed:
            continue
        records.append(
            ResolutionRecord(
                step_id=step_id,
                field=field_name,
                field_format=field_format,
                query=query,
                value=value,
                title=title,
                via=f"{field_format}.verify" if current else f"{field_format}.search",
            )
        )

    if not records:
        return plan, validation, []
    plan.inputs_compiled = False
    plan.input_compilation_errors = []
    revalidated = PlanValidator(registry).validate(plan)
    return plan, revalidated, records


async def apply_identifier_resolution(
    plan: IntentPlan,
    validation: PlanValidationResult,
    *,
    registry: SkillRegistry,
    tasks: Any,
    task_id: str,
    on_tool_event: Any,
    forward: Any,
    searcher: Searcher | None = None,
    allow_network: bool = True,
) -> tuple[IntentPlan, PlanValidationResult]:
    """Resolve identifiers in-lane and narrate each bind (audit + a friendly line).

    Thin wrapper the orchestrator calls so the turn path stays a thin coordinator:
    the resolution and event shaping live here, not in the orchestrator.

    ``allow_network`` gates the *default* (arXiv-backed) searcher: offline/mock
    runtimes pass ``False`` so planning never touches the network — the recovery
    ladder's ReAct floor then looks the identifier up when tools are live. An
    explicitly injected ``searcher`` always runs (tests supply offline doubles).
    """
    if searcher is None and not allow_network:
        return plan, validation
    plan, revalidated, records = await resolve_identifier_fields(
        plan, validation, registry=registry, searcher=searcher
    )
    for record in records:
        summary = f"looked up {record.field}: '{record.query}' \u2192 {record.value}"
        if record.title:
            summary += f" ({record.title})"
        event = {
            "event_type": "input.resolved",
            "status": "succeeded",
            "name": record.via,
            "output_json": {
                "step_id": record.step_id,
                "field": record.field,
                "query": record.query,
                "value": record.value,
                "title": record.title,
            },
            "summary": summary[:220],
        }
        await tasks.append_event(task_id, **event)
        await forward(on_tool_event, event)
    return plan, revalidated


def _pick_hit(query: str, hits: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Return one well-supported, unambiguous identity match."""
    ranked = sorted(
        (
            (_title_overlap(query, title), index, identifier, title)
            for index, (identifier, title) in enumerate(hits)
            if identifier
        ),
        key=lambda item: (-item[0], item[1]),
    )
    if not ranked or ranked[0][0] < _MATCH_THRESHOLD:
        return None
    if (
        len(ranked) > 1
        and ranked[0][0] - ranked[1][0] < _MATCH_MARGIN
    ):
        return None
    _score, _index, identifier, title = ranked[0]
    return identifier, title


def _title_overlap(a: str, b: str) -> float:
    """Bidirectional title score with an information-content floor."""
    tokens_a = _information_tokens(a)
    tokens_b = _information_tokens(b)
    if (
        len(tokens_a) < _MIN_INFORMATION_TOKENS
        or len(tokens_b) < _MIN_INFORMATION_TOKENS
    ):
        return 0.0
    common = len(tokens_a & tokens_b)
    if not common:
        return 0.0
    query_coverage = common / len(tokens_a)
    title_coverage = common / len(tokens_b)
    harmonic = (
        2.0
        * query_coverage
        * title_coverage
        / (query_coverage + title_coverage)
    )
    if max(query_coverage, title_coverage) == 1.0:
        # A title/subtitle containment match is useful, but the coverage in the
        # opposite direction still contributes so a tiny fragment cannot win.
        containment = 0.75 + 0.25 * min(
            query_coverage,
            title_coverage,
        )
        return max(harmonic, containment)
    return harmonic


def _information_tokens(value: str) -> set[str]:
    return {
        token
        for token in (item.casefold() for item in _WORD_RE.findall(value))
        if token not in _TITLE_STOPWORDS
    }


def _title_span(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    quoted = _QUOTED_RE.findall(raw)
    if quoted:
        return max((q.strip() for q in quoted), key=len)
    runs = [m.group(0).strip() for m in _LATIN_RUN_RE.finditer(raw)]
    runs = [r for r in runs if r]
    if runs:
        multi = [r for r in runs if len(_WORD_RE.findall(r)) >= 2]
        if multi:
            return max(multi, key=len)
        # No multi-word Latin run: only accept a bare scalar (avoid a lone keyword
        # plucked from a longer sentence pinning an unrelated identifier).
        if len(runs) == 1 and runs[0] == raw:
            return raw
        return ""
    return raw


async def _default_searcher(field_format: str, query: str) -> list[tuple[str, str]]:
    """Look up identifier candidates for a query; offline-safe (returns [])."""
    return await search_field_candidates(field_format, query)


def is_identifier_field(entry: object | None, field_name: str) -> bool:
    """True when ``field_name`` is a resolvable identifier (title/keywords → id)."""
    return has_searcher(_field_format(entry, field_name))


def _field_format(entry: object | None, field_name: str) -> str:
    schema = getattr(entry, "input_schema", None)
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties")
    field_schema = props.get(field_name) if isinstance(props, dict) else None
    if not isinstance(field_schema, dict):
        return ""
    return field_resolver(field_schema)


__all__ = [
    "ResolutionRecord",
    "Searcher",
    "apply_identifier_resolution",
    "extract_entity_query",
    "fetched_identifier_title_warning",
    "is_identifier_field",
    "resolve_identifier_fields",
]
