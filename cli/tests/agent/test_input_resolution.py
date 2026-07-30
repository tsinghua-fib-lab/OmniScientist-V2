"""In-lane grounded identifier resolution — "look up before ask/error".

Unit coverage for :mod:`omni.agent.input_resolution`: entity-title extraction
(never the whole multi-clause goal), resolvable-identifier detection, and the
network-free in-lane binding that keeps a typed step (and its provenance chain)
instead of degrading to a lossy free-text search. The searcher is always injected
here so these tests never touch arXiv.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omni.agent import input_resolution
from omni.agent.input_resolution import (
    apply_identifier_resolution,
    extract_entity_query,
    is_identifier_field,
    resolve_identifier_fields,
)
from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.model_planner import ModelIntentPlanner
from omni.agent.plan_validator import PlanValidator
from omni.agent.planner import IntentPlanner
from omni.agent.resolver_evidence import (
    materialize_resolver_evidence,
    validate_resolver_evidence,
)
from omni.config import load_settings
from omni.core.llm.client import LLMClient
from omni.skills_runtime.manifest import DeliveryMode, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry


class _ScriptedLLM(LLMClient):
    """Single-pass planner double: returns one scripted proposal, no binder call."""

    def __init__(self, plan: dict) -> None:
        self.model = "scripted"
        self._plan = plan

    async def chat(self, system: str, user: str, **kwargs: Any) -> str:
        return json.dumps(self._plan, ensure_ascii=False)

    async def chat_with_tools(self, messages, tools, **kwargs: Any):  # noqa: ANN001, ANN201 # pragma: no cover
        raise AssertionError("scripted planning uses chat only")

    async def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        return [[0.0] for _ in texts]


def _registry() -> SkillRegistry:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return registry


def _doi_skill() -> SkillEntry:
    return SkillEntry(
        name="doi-fetch",
        description="doi-fetch handles doc.fetch.doi",
        source="builtin",
        kind=SkillKind.PYTHON_ENGINE,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role="support",
        capabilities=["doc.fetch.doi"],
        priority=50,
        input_schema={
            "type": "object",
            "properties": {"identifier": {"type": "string", "format": "doi"}},
            "required": ["identifier"],
        },
        output_schema={"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]},
    )


def _path_skill() -> SkillEntry:
    return SkillEntry(
        name="path-reader",
        description="read an existing local file",
        source="builtin",
        kind=SkillKind.PYTHON_ENGINE,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role="support",
        capabilities=["file.read"],
        priority=50,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "format": "file_path"}
            },
            "required": ["path"],
        },
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
    )


async def _title_only_paper_plan(registry: SkillRegistry, goal: str):
    """Build a real workflow plan whose ``paper.fetch.arxiv`` step lacks an id.

    The planner strips the ``<user_provided>`` placeholder, so validation raises a
    genuine ``step_input_contract`` gap on ``identifier`` — the exact input the
    in-lane resolver is meant to bind.
    """
    plan = {
        "intent_type": "workflow",
        "confidence": 0.85,
        "workflow_steps": [
            {"id": "paper", "capability": "paper.fetch.arxiv", "input": {"identifier": "<user_provided>"}},
            {"id": "figure", "capability": "artifact.figure", "depends_on": ["paper"], "input": {}},
        ],
        "outputs": ["artifact"],
        "missing_inputs": [{"field": "identifier", "reason": "title-only paper needs a concrete arXiv id"}],
        "rationale": "title-only paper plus figure",
    }
    proposal = await ModelIntentPlanner(_ScriptedLLM(plan), registry).propose(goal)
    assert proposal is not None
    return IntentPlanner(registry).plan_from_proposal(goal, proposal, task_id="run-resolve")


def _id_contract_findings(validation) -> list:  # noqa: ANN001
    return [
        f
        for f in validation.findings
        if f.code == "step_input_contract" and f.missing_field == "identifier"
    ]


# ── extract_entity_query: the entity/title, never the whole goal ──


def test_extract_prefers_quoted_span():
    step = {"input": {"query": 'please fetch "Deep Residual Learning for Image Recognition" now'}}
    assert extract_entity_query(step, "") == "Deep Residual Learning for Image Recognition"


def test_extract_pulls_latin_title_out_of_a_mixed_language_goal():
    goal = "获取 Attention Is All You Need 摘要，并生成 RAG 架构图。"
    assert extract_entity_query({}, goal) == "Attention Is All You Need"


def test_extract_ignores_reason_capability_id_and_falls_back_to_goal():
    # ``plan_from_proposal`` fills ``reason`` with the capability id; it is not a
    # title and must not pin the search — the goal's title wins instead.
    step = {"reason": "paper.fetch.arxiv", "input": {}}
    goal = "帮我找 Attention Is All You Need 这篇论文"
    assert extract_entity_query(step, goal) == "Attention Is All You Need"


def test_extract_prefers_a_bound_scalar_param():
    step = {"input": {"identifier": "Playing Atari with Deep Reinforcement Learning"}}
    assert extract_entity_query(step, "unrelated goal text") == (
        "Playing Atari with Deep Reinforcement Learning"
    )


# ── is_identifier_field: only resolvable identifiers route to look-up ──


def test_is_identifier_field_true_for_arxiv_id():
    registry = _registry()
    entry = registry.get("arxiv-fetch")
    assert entry is not None
    assert is_identifier_field(entry, "identifier") is True


def test_is_identifier_field_false_for_doi_and_unknown():
    assert is_identifier_field(_doi_skill(), "identifier") is False
    assert is_identifier_field(None, "identifier") is False


# ── resolve_identifier_fields: bind in-lane, network-free ──


@pytest.mark.asyncio
async def test_strong_match_binds_the_id_and_clears_the_gap():
    registry = _registry()
    goal = "获取 Attention Is All You Need 摘要"
    plan = await _title_only_paper_plan(registry, goal)
    assert _id_contract_findings(PlanValidator(registry).validate(plan))

    seen: list[tuple[str, str]] = []

    async def searcher(field_format: str, query: str) -> list[tuple[str, str]]:
        seen.append((field_format, query))
        return [("1706.03762", "Attention Is All You Need")]

    plan2, revalidated, records = await resolve_identifier_fields(
        plan, PlanValidator(registry).validate(plan), registry=registry, searcher=searcher
    )

    # The searcher was queried with the extracted title, not the whole goal.
    assert seen == [("arxiv_id", "Attention Is All You Need")]
    assert len(records) == 1
    assert records[0].value == "1706.03762"
    assert records[0].field == "identifier"
    # The id is bound into the typed step (kept, not rewritten) …
    paper = next(s for s in plan2.workflow_steps if s["id"] == "paper")
    assert paper["input"]["identifier"] == "1706.03762"
    # … and the contract gap is gone after re-validation.
    assert not _id_contract_findings(revalidated)


@pytest.mark.asyncio
async def test_weak_match_binds_nothing_so_recovery_can_take_over():
    registry = _registry()
    plan = await _title_only_paper_plan(registry, "获取 Attention Is All You Need 摘要")

    async def searcher(field_format: str, query: str) -> list[tuple[str, str]]:
        # A returned hit whose title barely overlaps must never pin an id.
        return [("2020.00000", "A Completely Different Survey On Something Else")]

    _plan2, revalidated, records = await resolve_identifier_fields(
        plan, PlanValidator(registry).validate(plan), registry=registry, searcher=searcher
    )
    assert records == []
    assert _id_contract_findings(revalidated)  # gap preserved for the ladder


def test_vague_single_token_and_ambiguous_top_hits_never_ground_identity() -> None:
    assert (
        input_resolution._pick_hit(  # noqa: SLF001
            "Attention",
            [("1706.03762", "Attention Is All You Need")],
        )
        is None
    )
    assert (
        input_resolution._pick_hit(  # noqa: SLF001
            "Graph Neural Networks for Science",
            [
                ("2401.00001", "Graph Neural Networks for Science"),
                ("2401.00002", "Graph Neural Networks in Science"),
            ],
        )
        is None
    )


@pytest.mark.asyncio
async def test_valid_but_wrong_bound_id_is_replaced_from_independent_title_evidence():
    registry = _registry()
    plan = await _title_only_paper_plan(
        registry,
        "获取 Attention Is All You Need 摘要",
    )
    paper = next(step for step in plan.workflow_steps if step["id"] == "paper")
    paper["input"] = {"identifier": "2401.00001"}
    plan.requested_constraints = [
        {
            "constraint_id": "paper-id",
            "semantic_key": "paper_id",
            "requested_value": "2401.00001",
            "source": "model",
            "evidence": "Attention Is All You Need",
            "explicit": True,
            "critical": True,
            "step_id": "paper",
            "owner": "model",
            "evidence_verified": True,
        }
    ]
    validation = PlanValidator(registry).validate(plan)
    seen: list[tuple[str, str]] = []

    async def searcher(field_format: str, query: str) -> list[tuple[str, str]]:
        seen.append((field_format, query))
        return [("1706.03762", "Attention Is All You Need")]

    corrected, revalidated, records = await resolve_identifier_fields(
        plan,
        validation,
        registry=registry,
        searcher=searcher,
    )

    assert seen == [("arxiv_id", "Attention Is All You Need")]
    assert corrected.workflow_steps[0]["input"]["identifier"] == "1706.03762"
    assert records[0].via == "arxiv_id.verify"
    assert not _id_contract_findings(revalidated)
    evidence = materialize_resolver_evidence(corrected, registry)
    assert evidence[0].value == "1706.03762"
    assert evidence[0].verification_mode == "grounded_search"
    assert evidence[0].verified is True
    assert validate_resolver_evidence(corrected, registry) == []


def test_explicit_arxiv_and_doi_literals_are_verified_without_search() -> None:
    registry = _registry()
    registry.register(_doi_skill())
    cases = [
        (
            "Fetch arXiv 1706.03762.",
            "arxiv-fetch",
            "paper.fetch.arxiv",
            "1706.03762",
        ),
        (
            "Fetch DOI 10.1000/xyz123.",
            "doi-fetch",
            "doc.fetch.doi",
            "10.1000/xyz123",
        ),
    ]
    for message, skill, capability, identifier in cases:
        plan = IntentPlan(
            task_id="resolver-exact",
            user_message=message,
            intent_type=IntentType.WORKFLOW,
            outputs=["paper"],
            workflow_steps=[
                {
                    "id": "paper",
                    "capability": capability,
                    "skill_name": skill,
                    "input": {"identifier": identifier},
                }
            ],
        )

        evidence = materialize_resolver_evidence(plan, registry)

        assert evidence[0].required_mode == "user_exact"
        assert evidence[0].verification_mode == "user_exact"
        assert evidence[0].verified is True
        assert validate_resolver_evidence(plan, registry) == []


@pytest.mark.asyncio
async def test_explicit_user_identifier_is_never_rewritten_by_title_search() -> None:
    registry = _registry()
    plan = await _title_only_paper_plan(
        registry,
        "Fetch Attention Is All You Need as arXiv 1706.03762.",
    )
    paper = next(step for step in plan.workflow_steps if step["id"] == "paper")
    paper["input"] = {"identifier": "1706.03762"}
    materialize_resolver_evidence(plan, registry)
    calls = 0

    async def searcher(
        field_format: str,
        query: str,
    ) -> list[tuple[str, str]]:
        nonlocal calls
        calls += 1
        return [("2401.00001", "Attention Is All You Need")]

    resolved, _validation, records = await resolve_identifier_fields(
        plan,
        PlanValidator(registry).validate(plan),
        registry=registry,
        searcher=searcher,
    )

    assert calls == 0
    assert records == []
    assert resolved.workflow_steps[0]["input"]["identifier"] == "1706.03762"
    exact = resolved.resolver_evidence[0]
    assert exact["value"] == "1706.03762"
    assert exact["verification_mode"] == "user_exact"


def test_title_derived_doi_requires_identity_grounding() -> None:
    registry = _registry()
    registry.register(_doi_skill())
    plan = IntentPlan(
        task_id="resolver-doi-title",
        user_message="Fetch the paper A Grounded Research Result.",
        intent_type=IntentType.WORKFLOW,
        outputs=["paper"],
        workflow_steps=[
            {
                "id": "paper",
                "capability": "doc.fetch.doi",
                "skill_name": "doi-fetch",
                "input": {"identifier": "10.1000/xyz123"},
            }
        ],
    )

    evidence = materialize_resolver_evidence(plan, registry)

    assert evidence[0].required_mode == "grounded_search"
    assert evidence[0].verified is False
    assert [
        finding.code
        for finding in validate_resolver_evidence(plan, registry)
    ] == ["grounded_binding_unverified"]


def test_existing_local_path_uses_local_exists_verification(tmp_path) -> None:  # noqa: ANN001
    registry = _registry()
    registry.register(_path_skill())
    source = tmp_path / "notes.txt"
    source.write_text("trusted local input", encoding="utf-8")
    plan = IntentPlan(
        task_id="resolver-path",
        user_message="Read the attached local notes.",
        intent_type=IntentType.WORKFLOW,
        outputs=["text"],
        workflow_steps=[
            {
                "id": "read",
                "capability": "file.read",
                "skill_name": "path-reader",
                "input": {"path": str(source)},
            }
        ],
    )

    evidence = materialize_resolver_evidence(plan, registry)

    assert evidence[0].required_mode == "local_exists"
    assert evidence[0].verification_mode == "local_exists"
    assert evidence[0].verified is True
    assert validate_resolver_evidence(plan, registry) == []


@pytest.mark.asyncio
async def test_searcher_failure_is_swallowed_and_binds_nothing():
    registry = _registry()
    plan = await _title_only_paper_plan(registry, "获取 Attention Is All You Need 摘要")

    async def searcher(field_format: str, query: str) -> list[tuple[str, str]]:
        raise ConnectionError("offline")

    _plan2, _revalidated, records = await resolve_identifier_fields(
        plan, PlanValidator(registry).validate(plan), registry=registry, searcher=searcher
    )
    assert records == []


# ── apply_identifier_resolution: bind + narrate for the orchestrator ──


class _RecordingTasks:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def append_event(self, task_id: str, **event: Any) -> None:
        self.events.append({"task_id": task_id, **event})


@pytest.mark.asyncio
async def test_apply_narrates_each_bind_as_an_input_resolved_event():
    registry = _registry()
    plan = await _title_only_paper_plan(registry, "获取 Attention Is All You Need 摘要")
    tasks = _RecordingTasks()
    forwarded: list[dict] = []

    async def forward(on_tool_event: Any, event: dict) -> None:
        forwarded.append(event)

    async def searcher(field_format: str, query: str) -> list[tuple[str, str]]:
        return [("1706.03762", "Attention Is All You Need")]

    _plan2, revalidated = await apply_identifier_resolution(
        plan,
        PlanValidator(registry).validate(plan),
        registry=registry,
        tasks=tasks,
        task_id="run-resolve",
        on_tool_event=None,
        forward=forward,
        searcher=searcher,
    )

    assert not _id_contract_findings(revalidated)
    assert len(tasks.events) == 1
    event = tasks.events[0]
    assert event["event_type"] == "input.resolved"
    assert event["status"] == "succeeded"
    assert "1706.03762" in event["summary"]
    # The narration is also forwarded to the live display.
    assert forwarded and forwarded[0]["event_type"] == "input.resolved"


@pytest.mark.asyncio
async def test_apply_is_a_noop_offline_when_no_searcher_is_injected():
    registry = _registry()
    plan = await _title_only_paper_plan(registry, "获取 Attention Is All You Need 摘要")
    tasks = _RecordingTasks()
    validation = PlanValidator(registry).validate(plan)

    async def forward(on_tool_event: Any, event: dict) -> None:  # pragma: no cover
        raise AssertionError("offline planning must not narrate a resolution")

    plan2, out = await apply_identifier_resolution(
        plan,
        validation,
        registry=registry,
        tasks=tasks,
        task_id="run-resolve",
        on_tool_event=None,
        forward=forward,
        allow_network=False,
    )
    # Untouched: the contract gap is preserved for the ReAct floor to resolve live.
    assert plan2 is plan and out is validation
    assert tasks.events == []
