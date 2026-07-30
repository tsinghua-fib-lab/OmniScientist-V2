"""End-to-end invariants for the authoritative typed-plan pipeline."""

from __future__ import annotations

import sys

import pytest

from omni.agent import OmniAgent
from omni.agent.intent_plan import IntentPlan, IntentType, VerificationPlan
from omni.agent.plan_revision import (
    canonical_plan_hash,
    create_execution_authority,
    create_revision,
    registry_snapshot_hashes,
)
from omni.agent.plan_runner_utils import approval_tools_for_plan
from omni.config import load_settings
from omni.runtime.workflow_plan import prepare_workflow_plan
from omni.skills_runtime.context import SKILL_SOURCE_PARAM
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from tests.conftest import PlanningLLM


async def _ignore_forward(*_args: object) -> None:
    return None


def _search_skill() -> SkillEntry:
    return SkillEntry(
        name="pipeline-search",
        description="offline workflow fixture",
        source="project_omni",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(
            command=sys.executable,
            args=["-c", "print('{}')"],
            stdout_format="json",
        ),
        capabilities=["literature.search"],
        workflow={
            "failure_policy": "continue_with_partial",
            "allow_failed_dependencies": True,
        },
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
        },
        priority=500,
    )


def _shadowed_semantic_skill(
    source: str,
    *,
    detect_language: bool,
) -> SkillEntry:
    language: dict[str, object] = {
        "type": "string",
        "enum": ["en", "zh"],
    }
    if detect_language:
        language["x-omni"] = {
            "semantic_key": "language",
            "binding_owner": "model",
            "expectation": {
                "kind": "explicit_enum",
                "signatures": {"en": ["English"]},
            },
        }
    return SkillEntry(
        name="shadowed-report",
        description=f"{source} report provider",
        source=source,
        capabilities=["report.write"],
        input_schema={
            "type": "object",
            "properties": {"language": language},
            "required": ["language"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
    )


@pytest.mark.asyncio
async def test_only_one_final_revision_is_accepted_validated_and_execution_bound() -> None:
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(
        {
            "intent_type": "direct_answer",
            "confidence": 0.9,
            "outputs": ["answer"],
            "rationale": "answer directly",
        }
    )
    try:
        turn = await agent.handle_turn("Explain typed plans briefly.", drain_tasks=False)
        task = await agent.tasks.get_task(turn.task_id)
        events = await agent.tasks.list_events(turn.task_id)
    finally:
        await agent.aclose()

    assert task is not None
    event_types = [event.event_type for event in events]
    assert event_types.index("plan.model.proposed") < event_types.index(
        "plan.revision.proposed"
    )
    accepted = [
        event for event in events if event.event_type == "plan.revision.accepted"
    ]
    validated = [event for event in events if event.event_type == "plan.validated"]
    bound = [event for event in events if event.event_type == "plan.execution.bound"]
    assert len(accepted) == len(validated) == len(bound) == 1
    assert accepted[0].name == accepted[0].output_json["source"]
    assert accepted[0].output_json["stage"] == "accepted"
    assert accepted[0].output_json["catalog_hash"]
    assert accepted[0].output_json["contract_hash"]

    persisted = IntentPlan.from_dict(task.plan_json)
    expected = canonical_plan_hash(persisted)
    assert accepted[0].output_json["content_hash"] == expected
    assert validated[0].output_json["revision_hash"] == expected
    assert bound[0].output_json["revision_hash"] == expected
    assert task.current_authority_fingerprint


@pytest.mark.asyncio
async def test_final_bind_rejects_provider_identity_changed_after_acceptance() -> None:
    agent = await OmniAgent.create(load_settings())
    agent.registry.register(_search_skill())
    agent.llm = PlanningLLM(
        planner_gated=True,
        plans=[
            {
                "intent_type": "workflow",
                "confidence": 0.95,
                "workflow_steps": [
                    {
                        "id": "search",
                        "capability": "literature.search",
                        "input": {"query": "authority closure"},
                    }
                ],
                "outputs": ["sources"],
                "execution_mode": "background",
                "rationale": "run one contracted search step",
            }
        ],
    )
    original_bind = agent.plan_pipeline.bind_execution_plan

    async def _replace_then_bind(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        replacement = _search_skill()
        assert replacement.exec_spec is not None
        replacement.exec_spec = ExecSpec(
            command=replacement.exec_spec.command,
            args=[
                "-c",
                "print('{\"status\":\"ok\",\"summary\":\"replacement\"}')",
            ],
            stdout_format="json",
        )
        agent.registry.register(replacement)
        return await original_bind(*args, **kwargs)

    agent.plan_pipeline.bind_execution_plan = _replace_then_bind  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="authority|snapshot"):
            await agent.handle_turn(
                "Search for authority closure.",
                drain_tasks=False,
            )
        assert await agent.runtime.list_workflow_runs() == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_accepted_pipeline_authority_is_reused_by_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accepted snapshot owns authority; orchestration must not rescan it."""
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(
        {
            "intent_type": "direct_answer",
            "confidence": 0.9,
            "outputs": ["answer"],
            "rationale": "answer directly",
        }
    )

    def _unexpected_resnapshot(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("orchestrator recomputed accepted execution authority")

    monkeypatch.setattr(
        "omni.agent.orchestrator.create_execution_authority",
        _unexpected_resnapshot,
    )
    try:
        turn = await agent.handle_turn(
            "Plan a short answer about typed plans.",
            interaction_mode="plan",
            drain_tasks=False,
        )
        task = await agent.tasks.get_task(turn.task_id)
    finally:
        await agent.aclose()

    assert turn.kind == "plan"
    assert task is not None
    assert task.current_authority_fingerprint


@pytest.mark.asyncio
async def test_healthy_accepted_candidate_is_not_revalidated_during_recovery() -> None:
    """Recovery may route a plan, but an unchanged healthy snapshot stays sealed."""
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(
        {
            "intent_type": "direct_answer",
            "confidence": 0.9,
            "outputs": ["answer"],
            "rationale": "answer directly",
        }
    )
    original_validate = agent.plan_pipeline._validate  # noqa: SLF001
    validate_calls = 0

    def _counted_validate(plan: IntentPlan):
        nonlocal validate_calls
        validate_calls += 1
        return original_validate(plan)

    agent.plan_pipeline._validate = _counted_validate  # type: ignore[method-assign]  # noqa: SLF001
    try:
        turn = await agent.handle_turn(
            "Plan a short answer about typed plans.",
            interaction_mode="plan",
            drain_tasks=False,
        )
    finally:
        await agent.aclose()

    assert turn.kind == "plan"
    assert validate_calls == 1


@pytest.mark.asyncio
async def test_plan_pipeline_batches_each_persisted_transition_atomically() -> None:
    """Proposal and acceptance each persist projection plus ordered audit once."""
    agent = await OmniAgent.create(load_settings())
    agent.llm = PlanningLLM(
        {
            "intent_type": "direct_answer",
            "confidence": 0.9,
            "outputs": ["answer"],
            "rationale": "answer directly",
        }
    )
    original_transition = agent.tasks.record_plan_transition
    transitions: list[tuple[str, list[str]]] = []

    async def _record_transition(
        task_id: str,
        plan: object,
        *,
        status: str,
        events: list[dict],
        current_authority_fingerprint: str = "",
    ) -> None:
        transitions.append(
            (
                status,
                [str(event.get("event_type") or "") for event in events],
            )
        )
        await original_transition(
            task_id,
            plan,
            status=status,
            events=events,
            current_authority_fingerprint=current_authority_fingerprint,
        )

    agent.tasks.record_plan_transition = _record_transition  # type: ignore[method-assign]
    try:
        turn = await agent.handle_turn(
            "Plan a short answer about typed plans.",
            interaction_mode="plan",
            drain_tasks=False,
        )
    finally:
        await agent.aclose()

    assert turn.kind == "plan"
    assert transitions == [
        (
            "created",
            ["plan.created", "plan.revision.proposed"],
        ),
        (
            "validated",
            [
                "plan.revision.accepted",
                "plan.recovery",
                "plan.validated",
            ],
        ),
    ]


async def _persist_forged_accepted_plan(
    agent: OmniAgent,
    plan: IntentPlan,
):
    catalog_hash, contract_hash = registry_snapshot_hashes(
        agent.registry,
        plan,
    )
    revision = create_revision(
        plan,
        revision=1,
        source="forged_test",
        stage="accepted",
        catalog_hash=catalog_hash,
        contract_hash=contract_hash,
    )
    accepted = revision.plan
    authority = create_execution_authority(
        accepted,
        registry=agent.registry,
        approval_tools=approval_tools_for_plan(accepted, agent.registry),
    )
    await agent.tasks.record_plan(
        plan.task_id,
        accepted,
        status="validated",
        current_authority_fingerprint=authority.fingerprint,
        emit_event=False,
    )
    return accepted, revision


@pytest.mark.asyncio
async def test_accepted_v1_workflow_binds_without_hash_migration() -> None:
    """Accepted v1 content stays immutable while runtime seals its derivative."""
    agent = await OmniAgent.create(load_settings())
    agent.registry.register(_search_skill())
    task = await agent.tasks.create_task(
        session_id="legacy-session",
        channel="cli",
        user_input="Search for accepted v1 workflow compatibility.",
    )
    steps = prepare_workflow_plan(
        task.user_input,
        [
            {
                "id": "search",
                "skill_name": "pipeline-search",
                "skill_source": "project_omni",
                "capability": "literature.search",
                "input": {"query": "accepted v1 workflow compatibility"},
            }
        ],
        agent.registry,
        seal_provider_bindings=False,
    )
    source = IntentPlan(
        task_id=task.id,
        user_message=task.user_input,
        intent_type=IntentType.WORKFLOW,
        outputs=["sources"],
        workflow_steps=steps,
        inputs_compiled=True,
        execution_mode="background",
        verification_plan=VerificationPlan(
            required_outputs=["sources"],
            required_events=["workflow.submitted"],
        ),
    )
    legacy_payload = source.to_dict()
    legacy_payload.pop("plan_schema_version")
    legacy_payload.pop("provider_bindings")
    legacy_payload.pop("resolver_evidence")
    legacy = IntentPlan.from_dict(legacy_payload)
    accepted, revision = await _persist_forged_accepted_plan(agent, legacy)
    accepted_payload = accepted.to_dict()
    accepted_hash = canonical_plan_hash(accepted)
    execution_candidate = IntentPlan.from_dict(accepted.to_dict())
    execution_validation = agent.plan_pipeline._validate(  # noqa: SLF001
        execution_candidate
    )
    execution_candidate, _ = agent.plan_pipeline._materialize_workflow(  # noqa: SLF001
        execution_candidate,
        execution_validation,
    )
    assert execution_candidate.to_dict() == accepted_payload

    try:
        authority = await agent.plan_pipeline.bind_execution_plan(
            accepted,
            revision,
            on_tool_event=None,
            forward=_ignore_forward,
        )
        workflow_id = await agent.runtime.enqueue_workflow(
            accepted.user_message,
            accepted.workflow_steps,
            task_id=accepted.task_id,
            session_id="legacy-session",
            execution_authority=authority.to_dict(),
        )
        run = await agent.runtime.get_workflow_run(workflow_id)
    finally:
        await agent.aclose()

    assert accepted.to_dict() == accepted_payload
    assert canonical_plan_hash(accepted) == accepted_hash
    assert "plan_schema_version" not in accepted_payload
    assert "provider_bindings" not in accepted_payload
    assert "provider_binding_id" not in accepted_payload["workflow_steps"][0]
    assert run is not None
    assert run.plan_json["steps"][0]["provider_binding_id"].startswith(
        "provider-binding-"
    )


@pytest.mark.asyncio
async def test_execution_bind_rejects_hash_consistent_unknown_provider() -> None:
    """Matching hashes cannot bypass final structural contract validation."""
    agent = await OmniAgent.create(load_settings())
    task = await agent.tasks.create_task(
        session_id="",
        channel="cli",
        user_input="run a forged provider",
    )
    plan = IntentPlan(
        task_id=task.id,
        user_message=task.user_input,
        intent_type=IntentType.WORKFLOW,
        outputs=["artifact"],
        workflow_steps=[
            {
                "id": "forged",
                "capability": "artifact.forged",
                "skill_name": "unknown-forged-skill",
                "input": {"input": "payload"},
            }
        ],
    )
    accepted, revision = await _persist_forged_accepted_plan(
        agent,
        plan,
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="final execution validation",
        ):
            await agent.plan_pipeline.bind_execution_plan(
                accepted,
                revision,
                on_tool_event=None,
                forward=_ignore_forward,
            )
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_execution_bind_rejects_model_omitted_fake_resolver_fact() -> None:
    """A resolver fact is gated even when the model omitted its constraint."""
    agent = await OmniAgent.create(load_settings())
    task = await agent.tasks.create_task(
        session_id="",
        channel="cli",
        user_input="Fetch Attention Is All You Need.",
    )
    plan = IntentPlan(
        task_id=task.id,
        user_message=task.user_input,
        intent_type=IntentType.WORKFLOW,
        outputs=["paper"],
        workflow_steps=[
            {
                "id": "paper",
                "capability": "paper.fetch.arxiv",
                "skill_name": "arxiv-fetch",
                "input": {"identifier": "2401.99999"},
            }
        ],
    )
    plan.workflow_steps = prepare_workflow_plan(
        plan.user_message,
        plan.workflow_steps,
        agent.registry,
    )
    accepted, revision = await _persist_forged_accepted_plan(
        agent,
        plan,
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="final execution validation",
        ):
            await agent.plan_pipeline.bind_execution_plan(
                accepted,
                revision,
                on_tool_event=None,
                forward=_ignore_forward,
            )
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_input_source", [False, True])
async def test_execution_bind_validates_forced_step_source_semantics(
    legacy_input_source: bool,
) -> None:
    """The final gate must validate the provider source it will dispatch."""
    agent = await OmniAgent.create(load_settings())
    agent.registry.register(
        _shadowed_semantic_skill(
            "user_omni",
            detect_language=True,
        )
    )
    agent.registry.register(
        _shadowed_semantic_skill(
            "builtin",
            detect_language=False,
        )
    )
    task = await agent.tasks.create_task(
        session_id="",
        channel="cli",
        user_input="Write the report in English.",
    )
    step = {
        "id": "write",
        "capability": "report.write",
        "skill_name": "shadowed-report",
        "input": {
            "language": "zh",
            **(
                {SKILL_SOURCE_PARAM: "user_omni"}
                if legacy_input_source
                else {}
            ),
        },
        **(
            {}
            if legacy_input_source
            else {"skill_source": "user_omni"}
        ),
    }
    if legacy_input_source:
        step["input_compiled"] = True
    plan = IntentPlan(
        task_id=task.id,
        user_message=task.user_input,
        intent_type=IntentType.WORKFLOW,
        outputs=["report"],
        workflow_steps=[step],
    )
    plan.workflow_steps = prepare_workflow_plan(
        plan.user_message,
        plan.workflow_steps,
        agent.registry,
    )
    accepted, revision = await _persist_forged_accepted_plan(
        agent,
        plan,
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="final execution validation",
        ):
            await agent.plan_pipeline.bind_execution_plan(
                accepted,
                revision,
                on_tool_event=None,
                forward=_ignore_forward,
            )
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_execution_bind_accepts_canonical_identifier_without_a_network_seal() -> None:
    """The final gate admits a canonical bound id as a locally-provable fact.

    A syntactically valid arXiv id is self-verifying: planning admits it with a
    ``syntactic`` verification mode — no ``grounded_binding_unverified`` finding and
    no network seal — and the execution-bind gate accepts it end to end. This is the
    intended fast path (Codex / Claude Code / OpenClaw likewise trust a well-formed
    id argument and let the tool validate it at call time). The seal path for
    genuinely non-canonical values is covered at the resolver-evidence layer in
    ``test_resolver_evidence``.
    """
    agent = await OmniAgent.create(load_settings())
    task = await agent.tasks.create_task(
        session_id="",
        channel="cli",
        user_input="Fetch the attention paper.",
    )
    plan = IntentPlan(
        task_id=task.id,
        user_message=task.user_input,
        intent_type=IntentType.WORKFLOW,
        outputs=["paper"],
        workflow_steps=[
            {
                "id": "paper",
                "capability": "paper.fetch.arxiv",
                "skill_name": "arxiv-fetch",
                "input": {"identifier": "1706.03762"},
            }
        ],
    )
    validation = agent.plan_pipeline._validate(plan)  # noqa: SLF001
    # Canonical id → admitted syntactically, so the network-grounding gate never fires.
    assert "grounded_binding_unverified" not in {
        finding.code for finding in validation.findings
    }
    plan, validation = agent.plan_pipeline._materialize_workflow(  # noqa: SLF001
        plan,
        validation,
    )
    assert validation.ok
    accepted, revision = await _persist_forged_accepted_plan(
        agent,
        plan,
    )
    try:
        await agent.plan_pipeline.bind_execution_plan(
            accepted,
            revision,
            on_tool_event=None,
            forward=_ignore_forward,
        )
    finally:
        await agent.aclose()


