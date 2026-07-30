"""A request must not lose its provider just because of the script it is typed in.

Two defects, both visible when the user wrote Chinese:

- the shortlist tokenised with ``\\w+``. Chinese is written without spaces, so a
  whole sentence became one token that matched nothing, and the Latin words
  embedded in it (``llm``, ``agentic``) were swallowed by the same run.
- with no lexical signal, the fallback ranked providers by how many input fields
  their schema happened to declare. Schema width says nothing about relevance,
  so a poster generator outranked the research provider and pushed it past the
  contract cut.

Manifests stay English (see ``test_production_control_plane_and_public_docs_are
_english_only``): cross-language understanding is the model's job, so the
deterministic pass only has to avoid actively misleading it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omni.agent.model_planner import (
    _PLANNER_CONTRACT_SHORTLIST_LIMIT,
    ModelPlanProposal,
    _lexical_tokens,
    _planner_relevant_entries,
    _planner_skill_index,
    _planner_system_prompt,
)
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.skills_runtime.manifest import DeliveryMode, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry

_SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"
_ZH_ASK = "帮我调研如何利用隐空间干预的方式提升LLM的Agentic能力"
_EN_ASK = "research how to use latent space intervention to improve LLM agentic capability"


def _skill(
    name: str,
    *,
    description: str = "",
    phrases: list[str] | None = None,
    capabilities: list[str] | None = None,
    role: str = "task",
    priority: int = 0,
    fields: int = 1,
) -> SkillEntry:
    return SkillEntry(
        name=name,
        description=description or f"{name} provider",
        source="user_omni",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role=role,
        priority=priority,
        capabilities=list(capabilities or []),
        trigger={"phrases": list(phrases or [])},
        input_schema={
            "type": "object",
            "properties": {f"f{i}": {"type": "string"} for i in range(fields)},
        },
    )


def _registry(*entries: SkillEntry) -> SkillRegistry:
    registry = SkillRegistry(load_settings(), sources=())
    for entry in entries:
        registry.register(entry)
    return registry


# ── tokenisation ──────────────────────────────────────────────────────────────
def test_an_unspaced_chinese_sentence_is_more_than_one_token():
    tokens = _lexical_tokens(_ZH_ASK.casefold())

    assert len(tokens) > 1, "a single token can never overlap with anything"


def test_latin_words_embedded_in_chinese_are_not_swallowed():
    # `\w` matches CJK too, so a naive split re-fuses the Latin run into its
    # ideographic neighbours and loses the only tokens that could ever match.
    tokens = _lexical_tokens(_ZH_ASK.casefold())

    assert "llm" in tokens
    assert "agentic" in tokens


def test_english_tokenisation_is_unchanged():
    assert _lexical_tokens("research latent space intervention") == {
        "research",
        "latent",
        "space",
        "intervention",
    }


# ── ranking when the lexical pass has nothing to go on ────────────────────────
def test_an_unreadable_request_ranks_by_declared_priority_not_schema_width():
    registry = _registry(
        _skill("research-ideation", priority=80, fields=3),
        _skill("poster-maker", priority=10, fields=9),
        _skill("slide-maker", priority=10, fields=7),
    )

    picked = [e.name for e in _planner_relevant_entries(registry, user_message=_ZH_ASK, limit=3)]

    assert picked[0] == "research-ideation"


def test_a_decisive_hit_does_not_shrink_the_contract_list():
    """Leading the list is right; being the only entry blinds every other step."""
    registry = _registry(
        _skill("research-ideation", phrases=["research"], priority=80),
        _skill("poster-maker", priority=10, fields=6),
        _skill("slide-maker", priority=10, fields=5),
    )

    picked = _planner_relevant_entries(registry, user_message=_EN_ASK, limit=3)

    assert picked[0].name == "research-ideation"
    assert len(picked) == 3, "the other providers still need their input schemas"


def test_an_english_hit_still_outranks_a_higher_priority_stranger():
    registry = _registry(
        _skill("research-ideation", phrases=["research ideation"], priority=10),
        _skill("poster-maker", priority=100, fields=9),
    )

    picked = _planner_relevant_entries(
        registry, user_message="please do some research ideation", limit=2
    )

    assert picked[0].name == "research-ideation", "priority is a fallback, not an override"


# ── what the model is told ────────────────────────────────────────────────────
def test_the_index_tells_the_model_which_providers_are_task_level():
    registry = _registry(
        _skill("research-ideation", capabilities=["research.ideation"], role="task"),
        _skill("openalex-search", capabilities=["literature.search"], role="support"),
    )

    index = _planner_skill_index(registry)

    assert "role=task" in index
    assert "role=support" in index


def test_the_planner_is_told_to_prefer_a_task_provider_over_decomposing():
    prompt = _planner_system_prompt(_registry(_skill("research-ideation")))

    assert "role=task" in prompt
    assert "role=support" in prompt


# ── the bundled manifest ──────────────────────────────────────────────────────
def _ideation_trigger() -> dict:
    frontmatter = yaml.safe_load(
        (_SKILLS_ROOT / "research-ideation" / "SKILL.md")
        .read_text(encoding="utf-8")
        .split("---")[1]
    )
    return frontmatter["metadata"]["helixforge"]["trigger"]


def test_research_ideation_does_not_claim_literature_only_survey():
    trigger = _ideation_trigger()
    when_to_use = trigger["when_to_use"].casefold()
    phrases = [str(p).casefold() for p in trigger["phrases"]]

    # Claiming survey/literature-review work made every 文献调研 request look
    # like a task the ideation pipeline already spanned, so the planner ran
    # gap analysis and idea generation instead of literature.search.
    assert "literature-only" in when_to_use or "do not use" in when_to_use
    assert all("literature review" not in p for p in phrases)
    assert all("survey the literature" not in p for p in phrases)
    assert all(p.isascii() for p in phrases), "manifests stay English by contract"


def test_the_planner_distinguishes_literature_search_from_ideation():
    prompt = _planner_system_prompt(_registry(_skill("research-ideation"))).casefold()

    assert "literature.search" in prompt
    assert "research.ideation" in prompt
    assert "literature-only" in prompt
    assert "spans" in prompt


def test_english_survey_shortlists_the_literature_search_provider():
    registry = SkillRegistry(load_settings())
    registry.build_index()

    picked = [
        e.name
        for e in _planner_relevant_entries(
            registry,
            user_message="survey the literature on retrieval augmented generation",
            limit=_PLANNER_CONTRACT_SHORTLIST_LIMIT,
        )
    ]

    assert picked[0] == "openalex-search"
    assert "research-ideation" in picked


def test_literature_search_capability_binds_openalex_not_ideation():
    registry = SkillRegistry(load_settings())
    registry.build_index()
    plan = IntentPlanner(registry).plan_from_proposal(
        "帮我做联邦学习的文献调研",
        ModelPlanProposal.from_payload(
            {
                "intent_type": "single_skill_task",
                "confidence": 0.9,
                "required_capabilities": ["literature.search"],
                "outputs": ["sources"],
                "execution_mode": "background",
                "rationale": "literature survey",
                "capability_inputs": {"literature.search": {"query": "federated learning"}},
            }
        ),
    )

    assert [selection.skill for selection in plan.selected_skills] == ["openalex-search"]


def test_the_bundled_research_provider_survives_the_contract_cut_in_chinese():
    """A research request typed in Chinese reaches the planner with the research
    provider's field names attached.

    The cut is taken at the real limit rather than a number spelled here, because
    the two drifting apart is the whole failure: a shortlist sized for a smaller
    catalogue passes this file's synthetic ordering tests and still withholds the
    contract on the machine that ships.
    """
    registry = SkillRegistry(load_settings())
    registry.build_index()

    picked = [
        e.name
        for e in _planner_relevant_entries(
            registry,
            user_message=_ZH_ASK,
            limit=_PLANNER_CONTRACT_SHORTLIST_LIMIT,
        )
    ]

    assert "research-ideation" in picked


def test_the_contract_shortlist_covers_the_shipped_catalogue():
    """No shipped provider may be cut before the planner has seen its contract.

    The cut is there to bound the prompt on a machine carrying many installed
    providers. Applied to the shipped set it does something else: it withholds
    field names from whichever providers sort last, and on a request the lexical
    pass cannot read — every request written without spaces — last is decided by
    declared priority alone. That is how adding one persona utility at priority
    95 silently removed the bundled research provider's contract from every
    Chinese request, without touching the research provider at all.

    So the sizing is the invariant, not any provider's rank: shipping the twelfth
    provider is allowed, shipping it into a shortlist too narrow to describe it
    is not.
    """
    registry = SkillRegistry(load_settings())
    registry.build_index()
    shipped = sorted(entry.name for entry in registry.list_selectable())

    assert _PLANNER_CONTRACT_SHORTLIST_LIMIT >= len(shipped), (
        f"{len(shipped)} providers ship but the planner only ever sees "
        f"{_PLANNER_CONTRACT_SHORTLIST_LIMIT} contracts, so "
        f"{len(shipped) - _PLANNER_CONTRACT_SHORTLIST_LIMIT} of them are "
        f"unusable for any request the lexical pass cannot score. Raise "
        f"_PLANNER_CONTRACT_SHORTLIST_LIMIT (~400 prompt tokens each): {shipped}"
    )
