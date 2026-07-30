from __future__ import annotations

import pytest
from sqlalchemy import update

from omni.agent import OmniAgent
from omni.agent.intent_plan import IntentPlan, IntentType, VerificationPlan
from omni.config import load_settings
from omni.storage.models import SubtaskORM, WorkflowRunORM, WorkflowStepORM


@pytest.mark.asyncio
async def test_run_verification_executes_artifact_provenance_and_presentation_checks():
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="feishu", external_key="chat-1")
        run = await agent.tasks.create_task(
            session_id=session_id,
            channel="feishu",
            user_input="生成一张可审计的科研图",
        )
        plan = IntentPlan(
            task_id=run.id,
            user_message=run.user_input,
            intent_type=IntentType.QA_PLUS_ARTIFACT,
            verification_plan=VerificationPlan(
                required_outputs=["artifact"],
                artifact_checks=["artifact_emitted"],
                provenance_checks=["source_or_claim_or_evidence_recorded"],
                presentation_checks=["presentation_sent_or_degraded"],
            ),
        )
        await agent.tasks.record_plan(run.id, plan, status="created")

        assert await agent.tasks.verify_task(run.id) == "failed"

        await agent.tasks.append_event(
            run.id,
            event_type="subtask.done",
            status="succeeded",
            output_json={
                "artifact_ids": ["artifact-1"],
                "source_ids": ["source-1"],
                "claim_ids": ["claim-1"],
                "evidence_ids": ["evidence-1"],
            },
        )
        await agent.tasks.append_event(
            run.id,
            event_type="presentation.sent",
            status="succeeded",
            output_json={"channel": "feishu"},
        )

        assert await agent.tasks.verify_task(run.id) == "passed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_presentation_sent_check_is_channel_aware_for_cli():
    # CLI delivers synchronously to stdout and records no send event, so the
    # presentation_sent_or_degraded check must pass without one; the same plan
    # on an IM channel would require the event.
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="cli", external_key="cli-1")
        run = await agent.tasks.create_task(session_id=session_id, channel="cli", user_input="画个图")
        plan = IntentPlan(
            task_id=run.id,
            user_message=run.user_input,
            intent_type=IntentType.QA_PLUS_ARTIFACT,
            verification_plan=VerificationPlan(
                required_outputs=["answer"],
                presentation_checks=["presentation_sent_or_degraded"],
            ),
        )
        await agent.tasks.record_plan(run.id, plan, status="created")

        assert await agent.tasks.verify_task(run.id) == "passed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_im_presentation_check_waits_for_final_delivery_not_ack():
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="feishu", external_key="chat-1")
        run = await agent.tasks.create_task(
            session_id=session_id,
            channel="feishu",
            user_input="回答问题",
        )
        plan = IntentPlan(
            task_id=run.id,
            user_message=run.user_input,
            intent_type=IntentType.DIRECT_ANSWER,
            verification_plan=VerificationPlan(
                presentation_checks=["presentation_sent_or_degraded"],
            ),
        )
        await agent.tasks.record_plan(run.id, plan, status="created")

        assert await agent.tasks.verify_task(run.id) == "pending"
        await agent.tasks.append_event(
            run.id,
            event_type="presentation.sent",
            status="sent",
            output_json={"channel": "feishu", "kind": "ack"},
        )
        assert await agent.tasks.verify_task(run.id) == "pending"
        await agent.tasks.append_event(
            run.id,
            event_type="presentation.sent",
            status="sent",
            output_json={"channel": "feishu", "kind": "turn"},
        )
        assert await agent.tasks.verify_task(run.id) == "passed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_artifact_provenance_capsule_requires_grounded_capsule():
    # An artifact-producing run must ship a *grounded* provenance capsule
    # (recorded as a provenance.capsule event with complete=true). A hollow
    # capsule (complete=false) does not satisfy the check.
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="cli", external_key="cap-1")
        run = await agent.tasks.create_task(session_id=session_id, channel="cli", user_input="可溯源产物")
        plan = IntentPlan(
            task_id=run.id,
            user_message=run.user_input,
            intent_type=IntentType.QA_PLUS_ARTIFACT,
            verification_plan=VerificationPlan(
                required_outputs=["artifact"],
                artifact_checks=["artifact_emitted"],
                provenance_checks=["artifact_provenance_capsule"],
            ),
        )
        await agent.tasks.record_plan(run.id, plan, status="created")

        # artifact emitted, but no capsule yet → fails
        await agent.tasks.append_event(
            run.id, event_type="subtask.done", status="succeeded",
            output_json={"artifact_ids": ["artifact-1"]},
        )
        assert await agent.tasks.verify_task(run.id) == "failed"

        # a hollow capsule (complete=false) still fails
        await agent.tasks.append_event(
            run.id, event_type="provenance.capsule", status="degraded",
            output_json={"complete": False, "artifact_uri": "artifact://artifact-1"},
        )
        assert await agent.tasks.verify_task(run.id) == "failed"

        # a grounded capsule (complete=true) passes
        await agent.tasks.append_event(
            run.id, event_type="provenance.capsule", status="succeeded",
            output_json={"complete": True, "artifact_uri": "artifact://artifact-1",
                         "source_ids": ["source-1"]},
        )
        assert await agent.tasks.verify_task(run.id) == "passed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_artifact_provenance_capsule_requires_binding_and_entity_ids():
    # North-star strengthening: a capsule must be *bound to the produced
    # artifact* AND cite ≥1 real entity id — not merely carry complete=true.
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="cli", external_key="cap-2")
        run = await agent.tasks.create_task(session_id=session_id, channel="cli", user_input="强绑定溯源")
        await agent.tasks.record_plan(
            run.id,
            IntentPlan(
                task_id=run.id,
                user_message=run.user_input,
                intent_type=IntentType.QA_PLUS_ARTIFACT,
                verification_plan=VerificationPlan(
                    required_outputs=["artifact"],
                    artifact_checks=["artifact_emitted"],
                    provenance_checks=["artifact_provenance_capsule"],
                ),
            ),
            status="created",
        )
        await agent.tasks.append_event(
            run.id, event_type="subtask.done", status="succeeded",
            output_json={"artifact_ids": ["artifact-1"]},
        )

        # complete=true but NO cited entity ids → forged/empty capsule, fails.
        await agent.tasks.append_event(
            run.id, event_type="provenance.capsule", status="succeeded",
            output_json={"complete": True, "artifact_uri": "artifact://artifact-1"},
        )
        assert await agent.tasks.verify_task(run.id) == "failed"

        # complete=true + entity ids but bound to NO artifact → fails (a capsule
        # that names no artifact cannot vouch for the produced one).
        await agent.tasks.append_event(
            run.id, event_type="provenance.capsule", status="succeeded",
            output_json={"complete": True, "claim_ids": ["claim-9"]},
        )
        assert await agent.tasks.verify_task(run.id) == "failed"

        # complete=true + entity ids + bound to the produced artifact → passes.
        await agent.tasks.append_event(
            run.id, event_type="provenance.capsule", status="succeeded",
            output_json={"complete": True, "artifact_uri": "artifact://artifact-1",
                         "evidence_ids": ["evidence-7"]},
        )
        assert await agent.tasks.verify_task(run.id) == "passed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_provenance_check_is_mode_aware():
    # light_or_full_as_requested is satisfied by design in light mode, but a full
    # provenance run must record at least one source/claim/evidence.
    settings = load_settings(overrides={"model": {"provider": "mock"}})
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="cli", external_key="cli-2")

        light = await agent.tasks.create_task(session_id=session_id, channel="cli", user_input="轻量")
        await agent.tasks.record_plan(
            light.id,
            IntentPlan(
                task_id=light.id,
                user_message="轻量",
                intent_type=IntentType.WORKFLOW,
                provenance_mode="light",
                verification_plan=VerificationPlan(
                    required_outputs=["answer"],
                    provenance_checks=["light_or_full_as_requested"],
                ),
            ),
            status="created",
        )
        assert await agent.tasks.verify_task(light.id) == "passed"

        full = await agent.tasks.create_task(session_id=session_id, channel="cli", user_input="可审计证据链")
        await agent.tasks.record_plan(
            full.id,
            IntentPlan(
                task_id=full.id,
                user_message="可审计证据链",
                intent_type=IntentType.WORKFLOW,
                provenance_mode="full",
                verification_plan=VerificationPlan(
                    required_outputs=["answer"],
                    provenance_checks=["light_or_full_as_requested"],
                ),
            ),
            status="created",
        )
        assert await agent.tasks.verify_task(full.id) == "failed"

        await agent.tasks.append_event(
            full.id,
            event_type="subtask.done",
            status="succeeded",
            output_json={"source_ids": ["source-1"]},
        )
        assert await agent.tasks.verify_task(full.id) == "passed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_verifier_tracks_every_submitted_child_and_missing_links():
    agent = await OmniAgent.create(load_settings(overrides={"model": {"provider": "mock"}}))
    try:
        run = await agent.tasks.create_task(session_id="", channel="cli", user_input="make artifact")
        await agent.tasks.record_plan(
            run.id,
            IntentPlan(
                task_id=run.id,
                user_message=run.user_input,
                intent_type=IntentType.REACT_FALLBACK,
                verification_plan=VerificationPlan(artifact_checks=["artifact_emitted"]),
            ),
            status="created",
        )
        async with agent.db.session() as session:
            task = SubtaskORM(skill_name="dynamic-provider", status="pending")
            session.add(task)
            await session.commit()
            await session.refresh(task)
            subtask_id = task.id
        await agent.tasks.link_subtask(run.id, subtask_id)

        assert await agent.tasks.verify_task(run.id) == "pending"

        async with agent.db.session() as session:
            await session.execute(
                update(SubtaskORM)
                .where(SubtaskORM.id == subtask_id)
                .values(status="succeeded", result_json={"artifact_ids": ["artifact-1"]})
            )
            await session.commit()
        assert await agent.tasks.verify_task(run.id) == "passed"

        await agent.tasks.link_subtask(run.id, "missing-child-row")
        assert await agent.tasks.verify_task(run.id) == "failed"
        events = await agent.tasks.list_events(run.id)
        assert (events[-1].output_json or {})["missing_submitted_tasks"] == ["missing-child-row"]
    finally:
        await agent.aclose()


async def _run_with_workflow_result(agent, result_json: dict, *, deliverable_checks: list[str]):  # noqa: ANN001
    run = await agent.tasks.create_task(session_id="", channel="cli", user_input="research deliverables")
    await agent.tasks.record_plan(
        run.id,
        IntentPlan(
            task_id=run.id,
            user_message=run.user_input,
            intent_type=IntentType.WORKFLOW,
            verification_plan=VerificationPlan(deliverable_checks=deliverable_checks),
        ),
        status="created",
    )
    workflow_status = (
        "degraded"
        if result_json.get("status") == "completed_with_warnings"
        else str(result_json.get("status") or "succeeded")
    )
    async with agent.db.session() as session:
        workflow = WorkflowRunORM(
            task_id=run.id,
            status=workflow_status,
            goal="research deliverables",
            result_json=result_json,
        )
        session.add(workflow)
        await session.flush()
        for position, item in enumerate(result_json.get("steps") or []):
            step_key = str(item.get("id") or f"step_{position + 1}")
            session.add(WorkflowStepORM(
                workflow_run_id=workflow.id,
                task_id=run.id,
                step_key=step_key,
                position=position,
                skill_name=str(item.get("skill_name") or ""),
                provider_type="skill" if item.get("skill_name") else "native_executor",
                deliverable="draft.section" if step_key == "writing" else "",
                status=str(item.get("status") or "succeeded"),
                result_json=item.get("result") or {},
            ))
        await session.commit()
        await session.refresh(workflow)
    await agent.tasks.link_workflow(run.id, workflow.id)
    return run


def _deliverable_assessment(
    deliverable_id: str,
    check: str,
    status: str,
    *,
    provider: str,
) -> dict[str, object]:
    provider_type = "native_executor" if provider == "synthesis.final" else "skill"
    return {
        "schema": "omni.deliverable-assessment/v1",
        "deliverable_id": deliverable_id,
        "provider_binding_id": f"{provider_type}:{provider}:{deliverable_id}",
        "provider": provider,
        "contract_hash": f"contract:{provider}:v1",
        "step_id": deliverable_id,
        "feedback": f"{check} is {status}",
        "status": status,
        "retryable": False,
        "effective_inputs": {},
        "criteria": [
            {
                "criterion_id": check,
                "status": status,
                "summary": f"{check} is {status}",
            }
        ],
        "evidence_refs": [],
    }


@pytest.mark.asyncio
async def test_deliverable_checks_degrade_placeholder_figure_and_template_draft():
    # Semantic acceptance: a run that shipped a generic placeholder figure
    # (despite domain terms in its instruction) and a template-fallback draft
    # verifies as degraded — the deliverables exist but are honestly not done.
    agent = await OmniAgent.create(load_settings(overrides={"model": {"provider": "mock"}}))
    try:
        run = await _run_with_workflow_result(
            agent,
            {
                "status": "completed_with_warnings",
                "steps": [
                    {
                        "id": "figure",
                        "status": "degraded",
                        "result": {
                            "outcome": {"code": "generic_despite_domain_terms"},
                            "deliverable_assessment": _deliverable_assessment(
                                "figure",
                                "figure_matches_instruction",
                                "degraded",
                                provider="scientific-figure",
                            ),
                        },
                    },
                    {
                        "id": "writing",
                        "status": "degraded",
                        "result": {
                            "draft_markdown": "## Draft skeleton",
                            "synthesis_mode": "template_fallback",
                            "deliverable_assessment": _deliverable_assessment(
                                "writing",
                                "draft_content_present",
                                "degraded",
                                provider="synthesis.final",
                            ),
                        },
                    },
                ],
            },
            deliverable_checks=["figure_matches_instruction", "draft_content_present"],
        )
        assert await agent.tasks.verify_task(run.id) == "degraded"
        events = await agent.tasks.list_events(run.id)
        payload = events[-1].output_json or {}
        assert set(payload["deliverable_degraded"]) == {
            "figure_matches_instruction",
            "draft_content_present",
        }
        assert payload["deliverable_failures"] == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_deliverable_checks_pass_model_written_draft_and_matching_figure():
    agent = await OmniAgent.create(load_settings(overrides={"model": {"provider": "mock"}}))
    try:
        run = await _run_with_workflow_result(
            agent,
            {
                "status": "succeeded",
                "steps": [
                    {
                        "id": "figure",
                        "status": "succeeded",
                        "result": {
                            "caption": "RAG architecture",
                            "deliverable_assessment": _deliverable_assessment(
                                "figure",
                                "figure_matches_instruction",
                                "passed",
                                provider="scientific-figure",
                            ),
                        },
                    },
                    {
                        "id": "writing",
                        "status": "succeeded",
                        "result": {
                            "draft_markdown": "# Review\n\nGrounded prose.",
                            "synthesis_mode": "llm",
                            "deliverable_assessment": _deliverable_assessment(
                                "writing",
                                "draft_content_present",
                                "passed",
                                provider="synthesis.final",
                            ),
                        },
                    },
                ],
            },
            deliverable_checks=["figure_matches_instruction", "draft_content_present"],
        )
        assert await agent.tasks.verify_task(run.id) == "passed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_deliverable_check_fails_when_provider_assessment_is_missing():
    agent = await OmniAgent.create(load_settings(overrides={"model": {"provider": "mock"}}))
    try:
        run = await _run_with_workflow_result(
            agent,
            {
                "status": "succeeded",
                "steps": [
                    {
                        "id": "figure",
                        "status": "succeeded",
                        # A plausible caption is not provider-owned evidence that
                        # the effective output matched the instruction.
                        "result": {"caption": "RAG architecture"},
                    }
                ],
            },
            deliverable_checks=["figure_matches_instruction"],
        )

        assert await agent.tasks.verify_task(run.id) == "failed"
        events = await agent.tasks.list_events(run.id)
        payload = events[-1].output_json or {}
        assert payload["deliverable_failures"] == ["figure_matches_instruction"]
        assert payload["deliverable_assessment_details"][0]["reason"] == "missing_assessment"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_draft_content_check_rejects_nonempty_stub_assessment():
    agent = await OmniAgent.create(load_settings(overrides={"model": {"provider": "mock"}}))
    try:
        run = await _run_with_workflow_result(
            agent,
            {
                "status": "succeeded",
                "steps": [
                    {
                        "id": "writing",
                        "status": "succeeded",
                        "result": {
                            "draft_markdown": "x",
                            "deliverable_assessment": {
                                "schema": "omni.deliverable-assessment/v1",
                                "deliverable_id": "writing",
                                "provider_binding_id": "native_executor:synthesis.final:writing",
                                "provider": "synthesis.final",
                                "contract_hash": "contract:synthesis.final:v1",
                                "step_id": "writing",
                                "feedback": "draft is too short to be usable",
                                "status": "failed",
                                "retryable": False,
                                "effective_inputs": {"deliverable": "draft.section"},
                                "criteria": [
                                    {
                                        "criterion_id": "draft_content_present",
                                        "status": "failed",
                                        "summary": "draft is too short to be usable",
                                    }
                                ],
                                "evidence_refs": [],
                            },
                        },
                    }
                ],
            },
            deliverable_checks=["draft_content_present"],
        )

        assert await agent.tasks.verify_task(run.id) == "failed"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_deliverable_checks_fail_when_draft_deliverable_is_missing_entirely():
    agent = await OmniAgent.create(load_settings(overrides={"model": {"provider": "mock"}}))
    try:
        run = await _run_with_workflow_result(
            agent,
            {"status": "succeeded", "steps": [{"id": "lit", "status": "succeeded", "result": {}}]},
            deliverable_checks=["draft_content_present"],
        )
        assert await agent.tasks.verify_task(run.id) == "failed"
        events = await agent.tasks.list_events(run.id)
        assert (events[-1].output_json or {})["deliverable_failures"] == ["draft_content_present"]
    finally:
        await agent.aclose()
