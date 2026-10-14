"""Resumable planning, authoring, and rendering pipeline for poster drafts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from posterlib.content import html_contract, planning
from posterlib.generation import authoring, model_runtime
from posterlib.runtime import browser_inspection, runtime_io
from posterlib.sources import source_preparation
from posterlib.visual import (
    figure_analysis,
    inspection_feedback,
    reference_analysis,
    reference_catalog,
    reference_generation,
    venue_branding,
)

from . import (
    deadlines,
    draft_checkpoint,
    outcomes,
    request_normalization,
    revision_state,
)

PublishVersion = Callable[..., Awaitable[dict[str, Any]]]
PAGE_OVERFLOW_CODES = {
    "element_outside_poster", "module_clipped_by_ancestor",
    "source_figure_outside_poster", "source_figure_clipped",
}


class DraftPipelineError(ValueError):
    """A deterministic draft-stage contract could not be restored."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def run_draft(
    input_data: dict[str, Any],
    progress_callback: Any,
    *,
    ctx: Any,
    max_source_chars: int,
    planning_llm: Callable[[Any], Any],
    author_llm: Callable[[Any], Any],
    publish_version: PublishVersion,
    visual_client: Any | None = None,
    deadline: float | None = None,
    resume_workspace: Path | None = None,
    resume_html_sha256: str | None = None,
    rendered_feedback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one draft stage, or the whole pipeline outside a durable task."""
    workspace = resume_workspace or runtime_io.create_workspace(input_data, ctx)
    persisted = resume_workspace is not None or _has_persisted_identity(ctx)
    checkpoint = draft_checkpoint.load(workspace) if persisted else None
    if checkpoint is not None and "author_model_role" not in input_data:
        input_data["author_model_role"] = checkpoint["author_model_role"]

    state: dict[str, Any] | None = None
    assets: list[dict[str, Any]] = []
    last_candidate: tuple[dict[str, Any], dict[str, Any]] | None = None

    async def publish_candidate(
        candidate_state: dict[str, Any], inspection: dict[str, Any],
    ) -> dict[str, Any]:
        """Deliver authored bytes with their actual check results, including defects."""

        result = await publish_version(
            html_text=html_contract.embed_assets(candidate_state["html_template"], assets),
            source_text=candidate_state["source_text"],
            input_data={**input_data, "visual_iteration": candidate_state["visual_iteration"]},
            progress_callback=progress_callback, workspace=workspace,
            parent_html_sha256=None, asset_warnings=list(candidate_state["warnings"]),
            inspection=inspection,
            source_figure_sha256s=set(candidate_state["source_figure_sha256s"]),
            page_plan=candidate_state["page_plan"],
            design_reference=design_reference, reference_brief=reference_brief,
            visual_preferences=dict(candidate_state["visual_preferences"]),
            checkpoint_state=candidate_state, deadline=deadline,
        )
        result["paper_source"] = dict(candidate_state["paper_source"])
        result["author_model_role"] = str(candidate_state["author_model_role"])
        if isinstance(candidate_state.get("editing_issue"), Mapping):
            result["editing_issue"] = dict(candidate_state["editing_issue"])
        return result

    stage = (
        str(checkpoint.get("stage") or "") if checkpoint is not None else "plan-ready"
    )
    try:
        async with (
            asyncio.timeout_at(deadline),
            browser_inspection.ChromiumInspectionSession() as inspection_session,
        ):
            if checkpoint is None:
                state, assets = await _plan_stage(
                    input_data,
                    progress_callback,
                    ctx=ctx,
                    workspace=workspace,
                    max_source_chars=max_source_chars,
                    planning_llm=planning_llm,
                    visual_client=visual_client,
                    deadline=deadline,
                )
                if persisted:
                    draft_checkpoint.save(
                        workspace, stage="plan-ready", state=state
                    )
                    await runtime_io.progress(
                        progress_callback,
                        "poster.plan-ready",
                        0.16,
                        checkpoint="plan-ready",
                    )
            else:
                state, assets = await _restore_state(
                    checkpoint,
                    ctx=ctx,
                    max_source_chars=max_source_chars,
                )
                state["author_model_role"] = (
                    request_normalization.author_model_role(input_data)
                )
                if rendered_feedback is not None:
                    if stage != "author-ready":
                        raise DraftPipelineError(
                            "invalid_payload",
                            "Rendered content planning requires an authored draft checkpoint.",
                        )
                    await _replan_after_render(
                        state,
                        assets=assets,
                        feedback=rendered_feedback,
                        input_data=input_data,
                        llm=planning_llm(ctx),
                        visual_client=visual_client,
                        deadline=deadline,
                    )
                    stage = "design-ready"
                    draft_checkpoint.save(workspace, stage=stage, state=state)
                elif persisted:
                    draft_checkpoint.save(
                        workspace,
                        stage=str(checkpoint["stage"]),
                        state=state,
                    )
            reference_phase_deadline = (
                deadlines.reference_deadline(
                    deadline,
                    asyncio.get_running_loop().time(),
                )
                if deadline is not None
                else None
            )
            if stage == "plan-ready":
                design_reference = await _reference_stage(
                    state,
                    workspace=workspace,
                    deadline=reference_phase_deadline,
                    generate_pixels=visual_client is not None,
                )
                state["design_reference"] = design_reference.to_dict()
                if design_reference.source_kind == "custom":
                    state["reference_image"] = design_reference.image_path
                if design_reference.warning:
                    _append_warning_once(state["warnings"], design_reference.warning)
                if persisted:
                    draft_checkpoint.save(
                        workspace, stage="reference-ready", state=state
                    )
                    await runtime_io.progress(
                        progress_callback,
                        "poster.reference-ready",
                        0.22,
                        checkpoint="reference-ready",
                        seed_id=design_reference.seed_id,
                        source_kind=design_reference.source_kind,
                    )
                stage = "reference-ready"
            else:
                design_reference = reference_catalog.ReferenceBundle.from_dict(
                    dict(state["design_reference"])
                )
                state["design_reference"] = design_reference.to_dict()

            if stage == "reference-ready":
                reference_brief = await _reference_analysis_stage(
                    state,
                    reference=design_reference,
                    client=visual_client,
                    deadline=reference_phase_deadline,
                )
                if persisted:
                    draft_checkpoint.save(workspace, stage="design-ready", state=state)
                    await runtime_io.progress(
                        progress_callback,
                        "poster.design-ready",
                        0.28,
                        checkpoint="design-ready",
                        planner=reference_brief.planner,
                    )
                stage = "design-ready"
            else:
                reference_brief = reference_analysis.ReferenceAnalysis.from_dict(
                    dict(state["visual_design"])
                )
                state["visual_design"] = reference_brief.to_dict()

            expected_figures = set(state["source_figure_sha256s"])
            page_plan = dict(state["page_plan"])
            if input_data.get("visual_iteration") is not None:
                state["visual_iteration"] = revision_state.visual_iteration(input_data["visual_iteration"])
            budget = dict(state["content_budget"])
            paper_identity = dict(state["paper_source"])
            accepted_inspection = None
            inspected_authored: list[tuple[str, bool]] = []
            timeout_seconds, transport_retries = request_normalization.authoring_transport_options(input_data)
            requested_feedback = str(input_data.get("feedback") or "").strip()

            async def inspect_authored(
                candidate: str, *, candidate_page_plan: Mapping[str, Any] | None = None,
            ) -> dict[str, Any]:
                nonlocal last_candidate
                embedded = html_contract.embed_assets(candidate, assets)
                raw = embedded.encode("utf-8")
                digest = hashlib.sha256(raw).hexdigest()
                candidate_dir = workspace / ".candidates" / digest
                candidate_path = candidate_dir / "poster.html"
                runtime_io.replace_file_atomic(candidate_path, raw)
                candidate_page = {
                    **(candidate_page_plan if candidate_page_plan is not None else page_plan),
                    "content_contract": html_contract.authored_content_contract(candidate, assets),
                }
                # A staged candidate has its own state. It cannot replace the
                # task checkpoint or the state beside an already published poster.
                candidate_state = {
                    **state, "html_template": candidate, "page_plan": candidate_page,
                }
                revision_state.persist_revision_sidecar(
                    artifact_path=candidate_path, html_text=embedded,
                    checkpoint_state=candidate_state,
                )
                # Static validation already passed. Keep the authored HTML even
                # if rendering consumes the remaining editing time.
                if last_candidate is None:
                    last_candidate = (
                        candidate_state, outcomes.inspection_deadline_result()
                    )
                measured = await runtime_io.inspect_preview(
                    candidate_path,
                    candidate_dir / "inspection",
                    scale=runtime_io.bounded_float(
                        input_data.get("scale"), default=2.0, low=0.5, high=4.0
                    ),
                    session=inspection_session,
                )
                if candidate_page_plan is None:
                    inspected_authored.append((
                        candidate,
                        any(
                            isinstance(warning, Mapping)
                            and warning.get("code") in PAGE_OVERFLOW_CODES
                            for warning in measured.get("warnings") or []
                        ),
                    ))
                previous = last_candidate[1] if last_candidate is not None else {}
                if (
                    previous.get("status") != "ok"
                    or (
                        measured.get("status") == "ok"
                        and len(measured.get("warnings") or [])
                        <= len(previous.get("warnings") or [])
                    )
                ):
                    last_candidate = (candidate_state, measured)
                return inspection_feedback.editor_feedback(
                    measured,
                    figure_observations=state.get("source_figure_observations"),
                )

            if stage == "design-ready":
                try:
                    html_template, accepted_inspection, authoring_issue = await _author_stage(
                        progress_callback,
                        assets=assets,
                        expected_figures=expected_figures,
                        budget=budget,
                        page_plan=page_plan,
                        paper_source=dict(state["paper_source"]),
                        authoring_request=state["authoring_request"],
                        reference_brief=reference_brief,
                        source_figure_observations=figure_analysis.normalize_observations(
                            state.get("source_figure_observations"),
                            selected_sha256s=set(
                                figure_analysis.selected_figure_sha256s(budget)
                            ),
                        ),
                        llm=author_llm(ctx),
                        timeout_seconds=timeout_seconds,
                        max_transport_retries=transport_retries,
                        inspect=inspect_authored,
                        deadline=deadline,
                    )
                except model_runtime.ModelBoundaryError as exc:
                    if last_candidate is None or not inspected_authored:
                        raise
                    candidate_state, accepted_inspection = last_candidate
                    html_template = candidate_state["html_template"]
                    authoring_issue = (
                        {
                            "code": "authoring_unavailable",
                            "summary": str(exc)[:300],
                        }
                        if html_template == inspected_authored[0][0]
                        else None
                    )
                if (
                    page_plan.get("strategy") == "auto"
                    and len(inspected_authored) > 1
                    and (
                        authoring_issue is not None
                        or accepted_inspection is None
                        or accepted_inspection.get("status") != "ok"
                    )
                ):
                    current_height = float(page_plan["height_mm"])
                    maximum_height = float(page_plan["max_height_mm"])
                    if maximum_height > current_height:
                        expanded_page = {**page_plan, "height_mm": maximum_height}
                        expanded_page["predicted_occupancy"] = planning.estimate_page(
                            budget,
                            page={
                                "width_mm": float(page_plan["width_mm"]),
                                "height_mm": maximum_height,
                            },
                            figure_observations=state.get("source_figure_observations"),
                        ).predicted_occupancy
                        for authored_candidate, page_overflow in reversed(inspected_authored[1:]):
                            if not page_overflow or authored_candidate == inspected_authored[0][0]:
                                continue
                            expanded = authoring.resize_page_height(
                                authored_candidate, current_height, maximum_height
                            )
                            if expanded is None:
                                continue
                            report = html_contract.validate_candidate(
                                expanded,
                                assets=assets,
                                required_source_figure_sha256s=expected_figures,
                                expected_page={
                                    "width_mm": float(page_plan["width_mm"]),
                                    "height_mm": maximum_height,
                                },
                                content_contract=page_plan.get("content_contract"),
                                paper_identity=paper_identity,
                                reference_html=inspected_authored[0][0],
                            )
                            if report.get("status") != "ok":
                                continue
                            checked = await inspect_authored(
                                expanded, candidate_page_plan=expanded_page
                            )
                            if checked.get("status") == "ok":
                                html_template = expanded
                                accepted_inspection = checked["_accepted_inspection"]
                                authoring_issue = None
                                page_plan = expanded_page
                                break
                stage = "author-ready"
                page_plan["content_contract"] = html_contract.authored_content_contract(html_template, assets)
                state["page_plan"] = dict(page_plan)
                state["html_template"] = html_template
                _update_authoring_issue(state, authoring_issue)
                if persisted:
                    draft_checkpoint.save(workspace, stage="author-ready", state=state)
                    await runtime_io.progress(
                        progress_callback,
                        "poster.author-ready",
                        0.44,
                        checkpoint="author-ready",
                    )
            else:
                html_template = str(state["html_template"])
                _require_valid_html(
                    html_template, assets=assets, expected_figures=expected_figures,
                    page_plan=page_plan, paper_identity=paper_identity,
                )
                checked = await inspect_authored(html_template)
                if requested_feedback or checked["status"] != "ok":
                    html_template, accepted_inspection, authoring_issue = await _author_stage(
                        progress_callback, assets=assets, expected_figures=expected_figures,
                        budget=budget, page_plan=page_plan, paper_source=paper_identity,
                        authoring_request=state["authoring_request"],
                        reference_brief=reference_brief, llm=author_llm(ctx),
                        timeout_seconds=timeout_seconds, max_transport_retries=transport_retries,
                        source_html=html_template,
                        feedback="\n\n".join(filter(None, [requested_feedback, json.dumps({k:v for k,v in checked.items() if not k.startswith("_")})])),
                        inspect=inspect_authored, deadline=deadline,
                    )
                    _update_authoring_issue(state, authoring_issue)
                else:
                    accepted_inspection = checked["_accepted_inspection"]
                page_plan["content_contract"] = html_contract.authored_content_contract(html_template, assets)
                state = {**state, "html_template": html_template, "page_plan": page_plan}

            if resume_html_sha256 and hashlib.sha256(
                html_contract.embed_assets(state["html_template"], assets).encode("utf-8")
            ).hexdigest() == resume_html_sha256:
                return outcomes.error_result(
                    "candidate_validation_failed",
                    "Rendered content planning did not change the selected poster HTML.",
                )
            return await publish_candidate(state, accepted_inspection)
    except (DraftPipelineError, model_runtime.ModelBoundaryError, planning.PlanningError) as exc:
        result = outcomes.error_result(exc.code, str(exc))
    except browser_inspection.LayoutMeasurementError as exc:
        result = dict(exc.result)
    except TimeoutError:
        result = outcomes.inspection_deadline_result()
    if last_candidate is not None:
        candidate_state, inspection = last_candidate
        delivered = await publish_candidate(candidate_state, inspection)
        delivered["editing_issue"] = {
            "code": result["outcome"]["code"],
            "summary": "Editing stopped before all checks passed; the authored draft is included.",
        }
        outcomes.append_warning_once(delivered, delivered["editing_issue"]["summary"])
        return delivered
    recovered = draft_checkpoint.load(workspace) if persisted else None
    result.update(
        {
            "workspace": str(workspace),
            "checkpoint_stage": str((recovered or {}).get("stage") or ""),
            "retry_from_checkpoint": recovered is not None,
        }
    )
    if state is not None:
        result["page_plan"] = dict(state["page_plan"])
        result["paper_source"] = dict(state["paper_source"])
    if state is not None and stage != "author-ready":
        result["content_budget"] = dict(state["content_budget"])
    return result


def render_capacity_feedback(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """Identify a rendered draft whose carrier area cannot fit the page envelope."""

    inspection = result.get("inspection")
    page_plan = result.get("page_plan")
    if not isinstance(inspection, Mapping) or not isinstance(page_plan, Mapping):
        return None
    warnings = inspection.get("warnings")
    if not isinstance(warnings, list) or not any(
        isinstance(item, Mapping)
        and item.get("code") in PAGE_OVERFLOW_CODES
        for item in warnings
    ):
        return None
    report = inspection.get("report")
    report = report if isinstance(report, Mapping) else {}
    poster = report.get("poster")
    modules = report.get("modules")
    if not isinstance(poster, Mapping) or not isinstance(modules, list):
        return None
    try:
        px_per_mm = float(poster["width"]) / float(poster["width_mm"])
        page_height = float(page_plan["height_mm"])
        max_height = float(page_plan["max_height_mm"])
        page_width = float(page_plan["width_mm"])
        measured = {
            str(item["module_id"]): {
                "width_mm": round(float(item["rect"]["width"]) / px_per_mm, 1),
                "height_mm": round(float(item["rect"]["height"]) / px_per_mm, 1),
                "bottom_mm": round(
                    (float(item["rect"]["top"]) + float(item["rect"]["height"]))
                    / px_per_mm, 1,
                ),
            }
            for item in modules
            if isinstance(item, Mapping)
            and isinstance(item.get("module_id"), str)
            and isinstance(item.get("rect"), Mapping)
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if not measured:
        return None
    observed_bottom = max(item["bottom_mm"] for item in measured.values())
    observed_area = sum(
        item["width_mm"] * item["height_mm"] for item in measured.values()
    )
    if (
        observed_bottom <= max_height
        or observed_area <= planning.TARGET_OCCUPANCY * page_width * page_height
    ):
        return None
    review = result.get("visual_review")
    return {
        "page_width_mm": page_width,
        "page_height_mm": page_height,
        "maximum_page_height_mm": max_height,
        "observed_content_bottom_mm": round(observed_bottom, 1),
        "overflow_beyond_maximum_mm": round(observed_bottom - max_height, 1),
        "observed_module_area_mm2": round(observed_area),
        "module_count": len(measured),
        "rendered_modules": measured,
        "visual_review_summary": str(review.get("summary") or "")[:1200]
        if isinstance(review, Mapping) else "",
    }


async def _replan_after_render(
    state: dict[str, Any],
    *,
    assets: list[dict[str, Any]],
    feedback: Mapping[str, Any],
    input_data: Mapping[str, Any],
    llm: Any,
    visual_client: Any | None,
    deadline: float | None,
) -> None:
    """Let the main model edit the scientific carrier after measured overflow."""

    current_budget = dict(state["content_budget"])
    previous_page = dict(state["page_plan"])
    observations = state.get("source_figure_observations") or {}
    width = float(previous_page["width_mm"])
    height = float(previous_page["height_mm"])
    source_equations = source_preparation.structured_source_equations(
        state["paper_source"]
    )
    page = {
        "strategy": previous_page["strategy"],
        "width_mm": width,
        "height_mm": height,
        "max_height_mm": float(previous_page["max_height_mm"]),
        "draft_capacity_observation": {
            "predicted_occupancy": float(previous_page["predicted_occupancy"]),
            "module_area_mm2": planning.module_area_estimates(
                current_budget,
                width_mm=width,
                height_mm=height,
                figure_observations=observations,
            ),
        },
    }
    budget = await model_runtime.request_content_plan(
        llm,
        source_text=str(state["source_text"]),
        assets=assets,
        source_figure_sha256s=set(state["source_figure_sha256s"]),
        authoring_request=str(state["authoring_request"]),
        source_equations=(
            source_equations if state["paper_source"].get("kind") == "pdf" else None
        ),
        page=page,
        orientation=str(previous_page["orientation"]),
        draft_budget=current_budget,
        figure_observations=observations,
        rendered_feedback=feedback,
        max_repair_attempts=request_normalization.repair_attempts(dict(input_data)),
        deadline=deadline,
    )
    if budget == current_budget:
        raise DraftPipelineError(
            "candidate_validation_failed",
            "Rendered content planning returned the unchanged evidence budget.",
        )
    added_figures = [
        digest for digest in figure_analysis.selected_figure_sha256s(budget)
        if digest not in observations
    ]
    if added_figures and visual_client is not None:
        added_observations, warning = await figure_analysis.analyze_figures(
            added_figures,
            assets,
            client=visual_client,
            deadline=deadline,
        )
        observations = {**observations, **added_observations}
        if warning:
            _append_warning_once(state["warnings"], warning)
    new_page = planning.estimate_page(
        budget,
        page={"width_mm": width, "height_mm": height},
        orientation=str(previous_page["orientation"]),
        figure_observations=observations,
    ).to_dict()
    # Retain the measured sheet while changing its scientific load. Shrinking it
    # from the same undercounted estimate can recreate the original overflow.
    new_page["strategy"] = previous_page["strategy"]
    new_page["min_height_mm"] = previous_page["min_height_mm"]
    new_page["max_height_mm"] = previous_page["max_height_mm"]
    if previous_page["strategy"] == "auto":
        new_page["reasons"] = [
            "Retained the measured adaptive sheet while revising its content."
        ]
    new_page["requested_orientation"] = previous_page.get(
        "requested_orientation", "auto"
    )
    budget["page_orientation"] = str(new_page["orientation"])
    state["source_figure_observations"] = figure_analysis.normalize_observations(
        observations,
        selected_sha256s=set(figure_analysis.selected_figure_sha256s(budget)),
    )
    state["content_budget"] = budget
    state["page_plan"] = new_page
    state["visual_iteration"] = int(state["visual_iteration"]) + 1
    state.pop("html_template", None)
    state.pop("editing_issue", None)


async def _plan_stage(
    input_data: dict[str, Any],
    progress_callback: Any,
    *,
    ctx: Any,
    workspace: Path,
    max_source_chars: int,
    planning_llm: Callable[[Any], Any],
    visual_client: Any | None,
    deadline: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = await source_preparation.prepare_draft_source(
        input_data,
        ctx=ctx,
        workspace=workspace,
        progress_callback=progress_callback,
    )
    _validate_source_size(source.text, max_source_chars)
    asset_inputs = [
        *source.assets,
        *[
            source_preparation.normalize_user_asset(item)
            for item in source_preparation.normalize_asset_inputs(input_data.get("assets"))
        ],
    ]
    requested_venue = _resolve_requested_venue(
        input_data,
        authoring_request=source.authoring_request,
    )
    venue_asset_input = _venue_brand_asset_input(requested_venue)
    if venue_asset_input is not None:
        asset_inputs.append(venue_asset_input)
    assets, asset_warnings = await source_preparation.prepare_assets(asset_inputs, ctx)
    expected_figures = html_contract.source_figure_sha256s(assets)
    source_equations = source_preparation.structured_source_equations(source.summary)
    requested_orientation = str(input_data.get("orientation") or "auto")
    reference_image = str(input_data.get("reference_image") or "").strip()
    use_reference_orientation = bool(
        requested_orientation == "auto"
        and input_data.get("page") is None
        and reference_image
    )
    if use_reference_orientation:
        try:
            requested_orientation = reference_catalog.custom_reference_orientation(
                reference_image.removeprefix("file://")
            )
        except reference_catalog.ReferenceSeedError as exc:
            raise DraftPipelineError("invalid_payload", str(exc)) from exc
    supplied_budget = input_data.get("content_budget")
    planning_deadline: float | None = None
    planning_warnings: list[str] = []
    planner_completed = supplied_budget is not None
    planning_issue: dict[str, str] | None = None
    if supplied_budget is not None:
        supplied_budget = planning.bind_prepared_figure_geometry(
            dict(supplied_budget),
            assets=assets,
        )
        budget = planning.normalize_content_budget(
            supplied_budget,
            source_figure_sha256s=expected_figures,
            source_equations=source_equations,
        )
    else:
        await runtime_io.progress(progress_callback, "poster.plan-content", 0.10)
        planning_deadline = (
            deadlines.draft_planning_deadline(
                deadline,
                asyncio.get_running_loop().time(),
                reserve_reference_phase=visual_client is not None,
            )
            if deadline is not None
            else None
        )
        try:
            budget = await model_runtime.request_content_plan(
                planning_llm(ctx),
                source_text=source.text,
                assets=assets,
                source_figure_sha256s=expected_figures,
                authoring_request=source.authoring_request,
                max_repair_attempts=request_normalization.repair_attempts(input_data),
                source_equations=source_equations,
                page=input_data.get("page"),
                orientation=requested_orientation,
                deadline=planning_deadline,
            )
            planner_completed = True
        except model_runtime.ModelBoundaryError as exc:
            budget = _grounded_fallback_budget(
                source_text=source.text,
                source_figure_sha256s=expected_figures,
                source_equations=source_equations,
                orientation=requested_orientation,
            )
            planning_warnings.append(
                "Content planning stopped before returning a valid plan; the draft "
                f"continues from a source-grounded carrier. ({exc})"
            )
            planning_issue = {
                "code": "content_planning_unavailable",
                "summary": "Content planning did not produce an audited scientific carrier.",
            }
    page_plan = planning.estimate_page(
        budget,
        page=input_data.get("page"),
        orientation=requested_orientation,
    ).to_dict()
    draft_figure_hashes = figure_analysis.selected_figure_sha256s(budget)
    figure_observations: dict[str, dict[str, str]] = {}
    figure_warning: str | None = None
    if (
        supplied_budget is None
        and planner_completed
        and visual_client is not None
        and draft_figure_hashes
    ):
        await runtime_io.progress(
            progress_callback,
            "poster.observe-source-figures",
            0.12,
        )
        figure_observations, figure_warning = (
            await figure_analysis.analyze_figures(
                draft_figure_hashes,
                assets,
                client=visual_client,
                deadline=planning_deadline,
            )
        )
    if supplied_budget is None and planner_completed:
        page_plan = planning.estimate_page(
            budget,
            page=input_data.get("page"),
            orientation=requested_orientation,
            figure_observations=figure_observations,
        ).to_dict()
        selected_page = {
            "width_mm": float(page_plan["width_mm"]),
            "height_mm": float(page_plan["height_mm"]),
            "max_height_mm": float(page_plan["max_height_mm"]),
            "strategy": str(page_plan["strategy"]),
        }
        draft_pressure = planning.estimate_page(
            budget,
            page=selected_page,
            orientation=str(page_plan["orientation"]),
            figure_observations=figure_observations,
        ).to_dict()
        selected_page["draft_capacity_observation"] = {
            "predicted_occupancy": float(draft_pressure["predicted_occupancy"]),
            "module_area_mm2": planning.module_area_estimates(
                budget,
                width_mm=selected_page["width_mm"],
                height_mm=selected_page["height_mm"],
                figure_observations=figure_observations,
            ),
        }
        selected_page["viewing_type_reference_mm"] = planning.poster_type_reference(
            selected_page["width_mm"], selected_page["height_mm"]
        )
        observed_figures = set(draft_figure_hashes)
        previous_pressure = float(draft_pressure["predicted_occupancy"])
        audits_completed = 0
        while True:
            try:
                audited_budget = await model_runtime.request_content_plan(
                    planning_llm(ctx),
                    source_text=source.text,
                    assets=assets,
                    source_figure_sha256s=expected_figures,
                    authoring_request=source.authoring_request,
                    max_repair_attempts=request_normalization.repair_attempts(input_data),
                    source_equations=source_equations,
                    page=selected_page,
                    orientation=requested_orientation,
                    draft_budget=budget,
                    figure_observations=figure_observations,
                    deadline=planning_deadline,
                )
            except model_runtime.ModelBoundaryError as exc:
                planning_warnings.append(
                    "Page-aware content audit stopped before returning a valid revision; "
                    f"the last valid content plan was retained. ({exc})"
                )
                if not audits_completed:
                    planning_issue = {
                        "code": "content_audit_unavailable",
                        "summary": "The final main-model scientific audit did not complete.",
                    }
                break
            budget = audited_budget
            audits_completed += 1
            added_figures = [
                digest
                for digest in figure_analysis.selected_figure_sha256s(budget)
                if digest not in observed_figures
            ]
            observed_figures.update(added_figures)
            if added_figures and visual_client is not None:
                added_observations, added_warning = await figure_analysis.analyze_figures(
                    added_figures,
                    assets,
                    client=visual_client,
                    deadline=planning_deadline,
                )
                figure_observations.update(added_observations)
                if added_warning:
                    planning_warnings.append(added_warning)
            draft_pressure = planning.estimate_page(
                budget,
                page=selected_page,
                figure_observations=figure_observations,
            ).to_dict()
            current_pressure = float(draft_pressure["predicted_occupancy"])
            if (
                selected_page["height_mm"] < selected_page["max_height_mm"]
                or current_pressure <= planning.TARGET_OCCUPANCY
                or current_pressure >= previous_pressure
            ):
                break
            selected_page["draft_capacity_observation"] = {
                "predicted_occupancy": current_pressure,
                "module_area_mm2": planning.module_area_estimates(
                    budget,
                    width_mm=selected_page["width_mm"],
                    height_mm=selected_page["height_mm"],
                    figure_observations=figure_observations,
                ),
            }
            previous_pressure = current_pressure
        page_plan = planning.estimate_page(
            budget,
            page=input_data.get("page"),
            orientation=requested_orientation,
            figure_observations=figure_observations,
        ).to_dict()
    figure_observations = figure_analysis.normalize_observations(
        figure_observations,
        selected_sha256s=set(figure_analysis.selected_figure_sha256s(budget)),
    )
    budget["page_orientation"] = str(page_plan["orientation"])
    # Retain request authority separately from the estimator's chosen geometry.
    page_plan["requested_orientation"] = (
        str(input_data.get("orientation") or "auto").strip().lower()
    )
    if use_reference_orientation:
        page_plan["reasons"] = [
            *page_plan["reasons"],
            "Matched the adaptive page orientation to the supplied poster reference.",
        ]
    state = {
        "source_text": source.text,
        "authoring_request": source.authoring_request,
        "asset_inputs": asset_inputs,
        "asset_sha256s": sorted(str(item["content_sha256"]) for item in assets),
        "source_figure_sha256s": sorted(expected_figures),
        "warnings": [
            *source.warnings,
            *asset_warnings,
            *([figure_warning] if figure_warning else []),
            *planning_warnings,
        ],
        "paper_source": _bind_paper_identity(
            source.summary,
            assets,
            requested_venue=requested_venue,
        ),
        "content_budget": budget,
        "page_plan": page_plan,
        "visual_preferences": reference_analysis.normalize_preferences(
            input_data.get("visual_preferences")
        ),
        "author_model_role": request_normalization.author_model_role(input_data),
        "visual_iteration": 0,
    }
    if planning_issue is not None:
        state["editing_issue"] = planning_issue
    if figure_observations:
        state["source_figure_observations"] = figure_observations
    reference_image = str(input_data.get("reference_image") or "").strip()
    if reference_image:
        state["reference_image"] = _preserve_custom_reference(
            reference_image,
            workspace=workspace,
        )
    return state, assets


def _preserve_custom_reference(value: str, *, workspace: Path) -> str:
    """Copy one validated custom reference into the durable poster workspace."""

    try:
        reference = reference_catalog.load_custom_bundle(
            value.removeprefix("file://"),
        )
        source_path = Path(reference.image_path)
        durable_path = (
            workspace
            / "design-reference"
            / f"custom-reference{source_path.suffix.lower()}"
        )
        runtime_io.replace_file_atomic(durable_path, source_path.read_bytes())
        return str(durable_path)
    except reference_catalog.ReferenceSeedError as exc:
        raise DraftPipelineError("invalid_payload", str(exc)) from exc
    except OSError as exc:
        raise DraftPipelineError(
            "source_read_failed",
            f"Cannot preserve the custom reference image: {exc}",
        ) from exc


def _grounded_fallback_budget(
    *,
    source_text: str,
    source_figure_sha256s: set[str],
    source_equations: list[dict[str, str]],
    orientation: str,
) -> dict[str, Any]:
    """Build one delivery-only carrier from a bounded prefix of the exact source."""

    excerpt = _source_excerpt(source_text)
    if not excerpt:
        raise DraftPipelineError(
            "source_read_failed",
            "The supplied source contained no readable prose.",
        )
    return planning.normalize_content_budget(
        {
            "page_orientation": (
                orientation if orientation in {"portrait", "landscape"} else "landscape"
            ),
            "sections": [{"id": "source-evidence", "label": "Source evidence"}],
            "content_modules": [
                {
                    "id": "source-excerpt",
                    "section_id": "source-evidence",
                    "title": "Source excerpt",
                    "semantic_roles": ["context"],
                    "priority": "focal",
                    "visual_kind": "text",
                    "text": excerpt,
                    "source_label": "Source text",
                    "figure_sha256s": [],
                    "equations": [],
                }
            ],
        },
        source_figure_sha256s=source_figure_sha256s,
        source_equations=source_equations,
    )


def _source_excerpt(source_text: str) -> str:
    """Return a bounded plain-text prefix without selecting scientific evidence."""

    plain = re.sub(r"\$\$.*?\$\$|\\\[.*?\\\]", " ", source_text, flags=re.DOTALL)
    plain = re.sub(r"\$[^$]*\$|\\\([^)]*\\\)", " ", plain, flags=re.DOTALL)
    text = " ".join(
        line.strip()
        for line in plain.splitlines()
        if line.strip() and not re.fullmatch(r"\[Page \d+\]", line.strip())
    )
    return text[:1600].rsplit(" ", 1)[0].strip()


async def _restore_state(
    checkpoint: dict[str, Any],
    *,
    ctx: Any,
    max_source_chars: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = dict(checkpoint)
    source_text = str(state["source_text"])
    _validate_source_size(source_text, max_source_chars)
    assets = await source_preparation.restore_checkpoint_assets(state, ctx)
    if "html_template" in state:
        for asset in assets:
            state["html_template"] = state["html_template"].replace(
                f"asset-sha256://{asset['content_sha256']}", str(asset["token"]),
            )
    expected_figures = html_contract.source_figure_sha256s(assets)
    source_equations = source_preparation.structured_source_equations(
        state["paper_source"]
    )
    budget = planning.normalize_content_budget(
        {
            key: value
            for key, value in state["content_budget"].items()
            if key not in {"organization_mode", "focal_role"}
        },
        source_figure_sha256s=expected_figures,
        source_equations=(
            source_equations
            if state["paper_source"].get("kind") == "pdf"
            else None
        ),
    )
    state["content_budget"] = budget
    state["source_figure_observations"] = figure_analysis.normalize_observations(
        state.get("source_figure_observations"),
        selected_sha256s=set(figure_analysis.selected_figure_sha256s(budget)),
    )
    return state, assets


async def _reference_stage(
    state: Mapping[str, Any],
    *,
    workspace: Path,
    deadline: float | None,
    generate_pixels: bool = True,
) -> reference_catalog.ReferenceBundle:
    """Resolve one content-free reference after planning and before authoring."""

    custom_image = str(state.get("reference_image") or "").strip()
    if custom_image:
        try:
            return reference_catalog.load_custom_bundle(
                custom_image.removeprefix("file://"),
            )
        except reference_catalog.ReferenceSeedError as exc:
            raise DraftPipelineError("invalid_payload", str(exc)) from exc
    signals = reference_signals(
        dict(state["content_budget"]),
        dict(state["page_plan"]),
    )
    selection = signals["selection"]
    seed = reference_catalog.select_seed(
        orientation=str(selection["orientation"]),
        organization_mode=str(selection["organization_mode"]),
        figure_count=int(selection["figure_count"]),
        module_weights=dict(selection["module_weights"]),
        density=str(selection["density"]),
    )
    if not generate_pixels:
        _config, warning = reference_generation.image_config_from_env()
        return reference_catalog.load_seed_bundle(
            seed,
            warning=(
                warning
                if warning is not None
                else reference_generation.GENERATION_UNUSED_WARNING
            ),
        )
    prompt = reference_generation_prompt(signals)
    generation_budget_s: float | None = None
    if deadline is not None:
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        # Generated pixels still need interpretation before they become executable
        # grammar.  Keep half of the shared phase for that consumer instead of
        # allowing two image-generation attempts to exhaust the entire deadline.
        generation_budget_s = remaining / 2.0
    return await asyncio.to_thread(
        reference_generation.resolve_reference,
        seed,
        prompt=prompt,
        output_dir=workspace / "design-reference",
        generation_budget_s=generation_budget_s,
    )


async def _reference_analysis_stage(
    state: dict[str, Any],
    *,
    reference: reference_catalog.ReferenceBundle,
    client: Any,
    deadline: float | None = None,
) -> reference_analysis.ReferenceAnalysis:
    """Ask the visual model for a reference brief without compiling a layout."""

    plan = await reference_analysis.analyze_reference(
        reference,
        client=client,
        preferences=dict(state["visual_preferences"]),
        deadline=deadline,
    )
    if plan.warning:
        state["warnings"].append(plan.warning)
    state["visual_design"] = plan.to_dict()
    return plan


def reference_signals(
    content_budget: Mapping[str, Any],
    page_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive deterministic design-only signals without copying paper content."""

    modules = list(content_budget["content_modules"])
    figure_hashes: set[str] = set()
    role_weights: dict[str, float] = {}
    aspect_ratios: list[float] = []
    equation_profiles: list[dict[str, Any]] = []
    has_equations = False
    has_tables = False
    priority_weights = {
        "focal": 3.0,
        "primary": 2.0,
        "supporting": 1.0,
        "footer": 0.5,
    }
    for module in modules:
        module_figures = [str(value) for value in module["figure_sha256s"]]
        figure_hashes.update(module_figures)
        priority = str(module["priority"])
        weight = priority_weights[priority]
        for role in module["semantic_roles"]:
            key = str(role)
            role_weights[key] = role_weights.get(key, 0.0) + weight
        equations = module["equations"]
        has_equations |= bool(equations)
        for equation in equations:
            latex = str(equation["latex"])
            equation_profiles.append(
                {
                    "latex_characters": len(latex),
                    "priority": priority,
                }
            )
        visual_kind = str(module["visual_kind"])
        # Comparisons and metric summaries may become prose, callouts, or charts.
        # Only an explicit table should bias reference selection toward a
        # table-oriented seed.
        has_tables |= visual_kind == "table"
        raw_ratio = module.get("figure_aspect_ratio")
        if (
            module_figures
            and isinstance(raw_ratio, (int, float))
            and not isinstance(raw_ratio, bool)
            and raw_ratio > 0
        ):
            aspect_ratios.append(round(float(raw_ratio), 3))

    width = float(page_plan.get("width_mm") or 0)
    height = float(page_plan.get("height_mm") or 0)
    orientation = str(page_plan.get("orientation") or "").strip().lower()
    if orientation not in {"landscape", "portrait"}:
        orientation = "landscape" if width >= height else "portrait"
    organization_mode = str(content_budget["organization_mode"])
    density = str(page_plan.get("density_profile") or "balanced")
    selection = {
        "orientation": orientation,
        "organization_mode": organization_mode,
        "figure_count": len(figure_hashes),
        "module_weights": role_weights,
        "has_equations": has_equations,
        "has_tables": has_tables,
        "density": density,
    }
    return {
        "selection": selection,
        "module_count": len(modules),
        "section_count": len(content_budget.get("sections") or []),
        "figure_width_to_height_ratios": sorted(aspect_ratios),
        "equation_profiles": equation_profiles,
        "page_aspect_ratio": (
            round(width / height, 3) if width > 0 and height > 0 else 1.0
        ),
    }


def reference_generation_prompt(signals: Mapping[str, Any]) -> str:
    """Build a content-free poster mockup prompt from layout signals only."""

    selection = dict(signals["selection"])
    safe_shape = {
        "orientation": selection["orientation"],
        "density": reference_catalog.normalize_density(str(selection["density"])),
        "organization_mode": selection["organization_mode"],
        "module_count": int(signals["module_count"]),
        "section_count": int(signals["section_count"]),
        "figure_count": int(selection["figure_count"]),
        "figure_width_to_height_ratios": list(
            signals["figure_width_to_height_ratios"]
        ),
        "equation_profiles": list(signals["equation_profiles"]),
        "page_aspect_ratio": signals["page_aspect_ratio"],
        "has_equations": bool(selection["has_equations"]),
        "has_comparison_or_table": bool(selection["has_tables"]),
    }
    return (
        "Generate a content-free visual reference for an editable top-conference academic "
        "poster. Show only abstract typographic texture, neutral diagram placeholders, and "
        "generic chart silhouettes. Use no readable claims, numbers, equations, authors, "
        "affiliations, citations, logos, venue names, or paper figures. The image supplies "
        "layout grammar only. Respect these design-only signals:\n"
        + json.dumps(safe_shape, ensure_ascii=False, sort_keys=True)
    )


def _append_warning_once(warnings: list[Any], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


def _update_authoring_issue(
    state: dict[str, Any],
    issue: dict[str, str] | None,
) -> None:
    """Replace or clear only the recoverable initial-author fallback marker."""

    if issue is not None:
        state["editing_issue"] = issue
    elif isinstance(state.get("editing_issue"), Mapping) and state[
        "editing_issue"
    ].get("code") == "authoring_unavailable":
        state.pop("editing_issue", None)


async def _author_stage(
    progress_callback: Any,
    *,
    assets: list[dict[str, Any]],
    expected_figures: set[str],
    budget: dict[str, Any],
    page_plan: dict[str, Any],
    paper_source: dict[str, Any],
    reference_brief: reference_analysis.ReferenceAnalysis,
    llm: Any,
    source_figure_observations: Mapping[str, Mapping[str, str]] | None = None,
    authoring_request: str = "",
    source_html: str | None = None,
    feedback: str = "",
    timeout_seconds: float = model_runtime.DEFAULT_AUTHORING_TIMEOUT_SECONDS,
    max_transport_retries: int = model_runtime.DEFAULT_AUTHORING_TRANSPORT_RETRIES,
    deadline: float | None = None,
    inspect: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, str] | None]:
    """Let the model compose and inspect a draft before the pipeline's visual review."""

    expected_page = {
        "width_mm": float(page_plan["width_mm"]),
        "height_mm": float(page_plan["height_mm"]),
    }
    await runtime_io.progress(progress_callback, "poster.author", 0.18)
    content_carrier = source_html if source_html is not None else authoring.render_initial_html(
        assets=assets,
        content_budget=budget,
        page_plan=page_plan,
        paper_source=paper_source,
    )

    def validate(candidate: str) -> dict[str, Any]:
        report = html_contract.validate_candidate(
            candidate,
            assets=assets,
            required_source_figure_sha256s=expected_figures,
            expected_page=expected_page,
            content_contract=page_plan.get("content_contract"),
            paper_identity=paper_source,
            reference_html=content_carrier,
        )
        if (
            source_html is None
            and candidate == content_carrier
            and report.get("status") == "ok"
        ):
            report = dict(report)
            report["warnings"] = [
                *list(report.get("warnings") or []),
                {
                    "code": "authoring_noop",
                    "severity": "warning",
                    "message": (
                        "The neutral scientific carrier is the delivery fallback; "
                        "apply a visual composition when model time remains."
                    ),
                },
            ]
        return report

    if source_html is None:
        report = html_contract.validate_candidate(
            content_carrier,
            assets=assets,
            required_source_figure_sha256s=expected_figures,
            expected_page=expected_page,
            content_contract=page_plan.get("content_contract"),
            paper_identity=paper_source,
        )
        if report.get("status") != "ok":
            raise model_runtime.ModelBoundaryError(
                "candidate_validation_failed",
                "Grounded content carrier violated the poster contract: "
                + json.dumps(report.get("issues") or [], ensure_ascii=False),
            )
    if source_html is None:
        system, user = authoring.layout_composition_prompt(
            feedback="Create a complete conference poster from the grounded carrier.",
            content_brief=budget,
            page_plan=page_plan,
            authoring_request=authoring_request,
            reference_brief=reference_brief,
            source_figure_observations=source_figure_observations or {},
            initial_composition=True,
        )
    else:
        system, user = authoring.revision_prompt(
            source_html=source_html,
            feedback=feedback,
            selection=None,
            page_plan=page_plan,
            content_brief=revision_state.visual_content_brief(
                page_plan, displayed_html=source_html,
                user_direction=authoring_request,
            ),
            reference_brief=reference_brief,
        )
    authored, _report, inspection = await model_runtime.request_html_revision(
        llm,
        source_html=content_carrier,
        system=system,
        user=user,
        validate=validate,
        inspect=inspect,
        initial_composition=source_html is None,
        compact_layout=source_html is None,
        include_rendered_image=bool(
            getattr(llm, "supports_image_input", False)
        ),
        timeout_seconds=timeout_seconds,
        max_transport_retries=max_transport_retries,
        deadline=deadline,
    )
    authoring_issue = (
        {
            "code": "authoring_unavailable",
            "summary": (
                "The author did not replace the grounded starter composition; "
                "its rendered version remains available for visual review."
            ),
        }
        if source_html is None and authored == content_carrier
        else None
    )
    return authored, inspection, authoring_issue


def _require_valid_html(
    html_template: str,
    *,
    assets: list[dict[str, Any]],
    expected_figures: set[str],
    page_plan: dict[str, Any],
    paper_identity: dict[str, Any],
) -> None:
    report = html_contract.validate_candidate(
        html_template,
        assets=assets,
        required_source_figure_sha256s=expected_figures,
        expected_page={
            "width_mm": float(page_plan["width_mm"]),
            "height_mm": float(page_plan["height_mm"]),
        },
        content_contract=page_plan.get("content_contract"),
        paper_identity=paper_identity,
    )
    if report.get("status") != "ok":
        raise model_runtime.ModelBoundaryError(
            "candidate_validation_failed",
            "Draft HTML checkpoint failed validation: "
            + json.dumps(report.get("issues") or [], ensure_ascii=False),
        )


def _bind_paper_identity(
    source_summary: Mapping[str, Any],
    assets: list[dict[str, Any]],
    *,
    requested_venue: venue_branding.VenueBranding | None = None,
) -> dict[str, Any]:
    """Bind one safely resolved venue identity to its vetted local logo asset."""

    identity = dict(source_summary)
    if requested_venue is None:
        return identity
    venue = {
        "label": requested_venue.label,
        "evidence_uri": requested_venue.evidence_uri,
    }
    if requested_venue.distinction:
        venue["distinction"] = requested_venue.distinction
    logo_digest = requested_venue.logo_sha256
    if logo_digest:
        logo_asset = next(
            (
                item
                for item in assets
                if item.get("source_kind") == "venue_brand_asset"
                and str(item.get("content_sha256") or "").lower() == logo_digest
            ),
            None,
        )
        if logo_asset is None:
            raise DraftPipelineError(
                "source_read_failed",
                "bundled venue logo is missing from the prepared asset manifest",
            )
        venue["logo_asset_sha256"] = logo_digest
        venue["logo_asset_token"] = str(logo_asset["token"])
    identity["venue_identity"] = venue
    return identity


def _resolve_requested_venue(
    input_data: Mapping[str, Any],
    *,
    authoring_request: str,
) -> venue_branding.VenueBranding | None:
    """Resolve one venue identity explicitly supplied by the user."""

    conference = input_data.get("conference")
    if isinstance(conference, str) and conference.strip():
        return venue_branding.resolve_venue_branding(conference)
    targets: list[str] = []
    for value in (
        authoring_request,
        input_data.get("input"),
        input_data.get("instructions"),
    ):
        text = " ".join(str(value or "").split())
        if text and text not in targets:
            targets.append(text)
    requested = venue_branding.resolve_venue_branding("\n".join(targets))
    return requested


def _venue_brand_asset_input(
    venue: venue_branding.VenueBranding | None,
) -> dict[str, Any] | None:
    """Return the hash-bound bundled logo as a distinct internal asset provenance."""

    if venue is None or venue.logo_path is None or venue.logo_sha256 is None:
        return None
    return {
        "path": str(venue.logo_path),
        "description": f"Official {venue.label} venue mark",
        "source_kind": "venue_brand_asset",
        "content_sha256": venue.logo_sha256,
        "venue_id": venue.venue_id,
        "label": venue.label,
        "evidence_uri": venue.evidence_uri,
    }


def _validate_source_size(source_text: str, max_source_chars: int) -> None:
    if len(source_text) > max_source_chars:
        raise DraftPipelineError(
            "source_too_large",
            f"Research input exceeds the {max_source_chars}-character safety limit.",
        )


def _has_persisted_identity(ctx: Any) -> bool:
    return bool(
        str(getattr(ctx, "task_id", "") or "").strip()
        or str(getattr(ctx, "workflow_task_id", "") or "").strip()
    )


__all__ = [
    "DraftPipelineError",
    "reference_generation_prompt",
    "reference_signals",
    "run_draft",
]
