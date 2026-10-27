"""Resumable planning, authoring, and rendering pipeline for poster drafts."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import image_assets

from posterlib.content import html_contract, planning
from posterlib.generation import authoring, model_runtime
from posterlib.runtime import browser_inspection, runtime_io
from posterlib.sources import paper_identity, source_preparation
from posterlib.visual import (
    design_direction,
    figure_analysis,
    inspection_feedback,
    reference_analysis,
    reference_catalog,
    venue_branding,
)

from . import (
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
    state: dict[str, Any] | None = None
    assets: list[dict[str, Any]] = []
    last_candidate: tuple[dict[str, Any], dict[str, Any]] | None = None

    async def publish_candidate(
        candidate_state: dict[str, Any], inspection: dict[str, Any],
    ) -> dict[str, Any]:
        """Deliver authored bytes with their actual check results, including defects."""

        if not revision_state.is_content_carrier(candidate_state["html_template"]):
            outcomes.update_workflow_issue(candidate_state, "planning", None)
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
        outcomes.merge_workflow_issues(result, candidate_state)
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
                effective, requested_feedback = request_normalization.effective_authoring_request(
                    input_data, state,
                )
                state.update(effective)
                input_data["author_model_role"] = effective["author_model_role"]
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
                effective, requested_feedback = request_normalization.effective_authoring_request(
                    input_data, state,
                )
                state.update(effective)
                input_data["author_model_role"] = effective["author_model_role"]
                if rendered_feedback is not None:
                    if stage != "author-ready":
                        raise DraftPipelineError(
                            "invalid_payload",
                            "Rendered feedback requires an authored draft checkpoint.",
                        )
                    requested_feedback = "\n\n".join(filter(None, (
                        requested_feedback,
                        json.dumps(dict(rendered_feedback), ensure_ascii=False),
                    )))
                elif persisted:
                    draft_checkpoint.save(
                        workspace,
                        stage=str(checkpoint["stage"]),
                        state=state,
                    )
            if stage == "plan-ready":
                design_reference = await _reference_stage(
                    state,
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
                    deadline=deadline,
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

            design_requirements = design_direction.context(
                reference_source_kind=design_reference.source_kind,
                user_direction=state["authoring_request"],
                visual_preferences=state["visual_preferences"],
            )
            expected_figures = set(state["source_figure_sha256s"])
            page_plan = dict(state["page_plan"])
            if input_data.get("visual_iteration") is not None:
                state["visual_iteration"] = revision_state.visual_iteration(input_data["visual_iteration"])
            budget = dict(state["content_budget"])
            paper_identity = dict(state["paper_source"])
            accepted_inspection = None
            inspected_authored: list[tuple[str, bool]] = []
            timeout_seconds, transport_retries = request_normalization.authoring_transport_options(input_data)
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
                authored = not revision_state.is_content_carrier(candidate)
                revision_state.persist_revision_sidecar(
                    artifact_path=candidate_path, html_text=embedded,
                    checkpoint_state=candidate_state,
                )
                # Static validation already passed. Keep the authored HTML even
                # if rendering consumes the remaining editing time.
                if authored and last_candidate is None:
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
                if authored and (
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
                        source_text=state["source_text"],
                        authoring_request=state["authoring_request"],
                        reference_brief=reference_brief,
                        design_requirements=design_requirements,
                        source_figure_observations=figure_analysis.normalize_observations(
                            state.get("source_figure_observations"),
                            selected_sha256s=set(state["asset_sha256s"]),
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
                    authoring_issue = {**exc.as_issue(), "phase": "editing"}
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
                                paper_identity=paper_identity,
                                reference_html=authored_candidate,
                            )
                            if report.get("status") != "ok":
                                continue
                            checked = await inspect_authored(
                                expanded, candidate_page_plan=expanded_page
                            )
                            if checked.get("status") == "ok":
                                html_template = expanded
                                accepted_inspection = checked["_rendered_inspection"]
                                authoring_issue = None
                                page_plan = expanded_page
                                break
                stage = "author-ready"
                page_plan["content_contract"] = html_contract.authored_content_contract(html_template, assets)
                state["page_plan"] = dict(page_plan)
                state["html_template"] = html_template
                outcomes.update_workflow_issue(state, "editing", authoring_issue)
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
                if requested_feedback or revision_state.is_content_carrier(html_template) or checked["status"] != "ok":
                    html_template, accepted_inspection, authoring_issue = await _author_stage(
                        progress_callback, assets=assets, expected_figures=expected_figures,
                        budget=budget, page_plan=page_plan, paper_source=paper_identity,
                        source_text=state["source_text"],
                        authoring_request=state["authoring_request"],
                        reference_brief=reference_brief, llm=author_llm(ctx),
                        design_requirements=design_requirements,
                        source_figure_observations=state.get("source_figure_observations"),
                        timeout_seconds=timeout_seconds, max_transport_retries=transport_retries,
                        source_html=html_template,
                        feedback=requested_feedback,
                        initial_inspection=checked,
                        inspect=inspect_authored, deadline=deadline,
                    )
                    outcomes.update_workflow_issue(state, "editing", authoring_issue)
                else:
                    accepted_inspection = checked["_rendered_inspection"]
                page_plan["content_contract"] = html_contract.authored_content_contract(html_template, assets)
                state = {**state, "html_template": html_template, "page_plan": page_plan}

            if resume_html_sha256 and hashlib.sha256(
                html_contract.embed_assets(state["html_template"], assets).encode("utf-8")
            ).hexdigest() == resume_html_sha256:
                return outcomes.error_result(
                    "candidate_validation_failed",
                    "Draft editing did not change the selected poster HTML.",
                )
            return await publish_candidate(state, accepted_inspection)
    except asyncio.CancelledError as cancelled:
        if persisted and stage != "author-ready" and last_candidate is not None:
            try:
                draft_checkpoint.save(workspace, stage="author-ready", state=last_candidate[0])
            except OSError as exc:
                cancelled.add_note(f"Poster checkpoint could not be saved: {exc}")
        raise
    except model_runtime.ModelBoundaryError as exc:
        result = outcomes.error_result(exc.code, str(exc), error_info=exc.as_issue())
    except (DraftPipelineError, planning.PlanningError) as exc:
        result = outcomes.error_result(exc.code, str(exc))
    except reference_analysis.ReferenceAnalysisError as exc:
        result = outcomes.error_result("invalid_payload", str(exc))
    except browser_inspection.LayoutMeasurementError as exc:
        result = dict(exc.result)
    except TimeoutError:
        result = outcomes.inspection_deadline_result()
    if last_candidate is not None:
        candidate_state, inspection = last_candidate
        issue = result.get("error_info") or {
            "code": result["outcome"]["code"],
            "summary": "Editing stopped before all checks passed; the authored draft is included.",
        }
        candidate_state = dict(candidate_state)
        outcomes.update_workflow_issue(candidate_state, "editing", issue)
        delivered = await publish_candidate(candidate_state, inspection)
        outcomes.append_warning_once(delivered, issue["summary"])
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
        *source_preparation.requested_asset_inputs(input_data),
    ]
    requested_venue = _resolve_requested_venue(input_data)
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
    planning_warnings: list[str] = []
    planning_issue: dict[str, str] | None = None
    if supplied_budget is not None:
        if isinstance(supplied_budget, Mapping):
            supplied_budget = planning.bind_prepared_figure_geometry(
                dict(supplied_budget),
                assets=assets,
            )
        budget = planning.normalize_content_budget(
            supplied_budget,
            source_figure_sha256s=expected_figures,
            source_equations=source_equations,
            source_text=source.text,
            source_assets=assets,
        )
    elif (
        input_data.get("page") is not None
        and source.summary.get("title")
        and source.summary.get("authors")
    ):
        # Prepared identity and fixed geometry permit joint evidence/composition.
        # Text sources with missing identity still use the existing planner.
        budget = _source_carrier_budget(
            source_text=source.text,
            source_kind=str(source.summary.get("kind") or ""),
            source_figure_sha256s=expected_figures,
            source_equations=source_equations,
            orientation=requested_orientation,
        )
    else:
        await runtime_io.progress(progress_callback, "poster.plan-content", 0.10)
        try:
            budget = await model_runtime.request_content_plan(
                planning_llm(ctx),
                source_text=source.text,
                assets=assets,
                source_figure_sha256s=expected_figures,
                authoring_request=source.authoring_request,
                prepared_identity=source.summary,
                max_repair_attempts=request_normalization.repair_attempts(input_data),
                source_equations=source_equations,
                page=input_data.get("page"),
                orientation=requested_orientation,
                deadline=deadline,
            )
        except model_runtime.ModelBoundaryError as exc:
            budget = _source_carrier_budget(
                source_text=source.text,
                source_kind=str(source.summary.get("kind") or ""),
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
                "summary": "Initial evidence planning did not produce a usable proposal.",
            }
    page_plan = planning.estimate_page(
        budget,
        page=input_data.get("page"),
        orientation=requested_orientation,
    ).to_dict()
    # Joint composition chooses among available figures; observation must precede
    # that choice, including when no separate content plan is needed.
    draft_figure_hashes = list(dict.fromkeys(
        str(asset["content_sha256"])
        for asset in assets
        if image_assets.is_source_figure_asset(asset)
        or asset.get("source_kind") == "native_component"
    ))
    figure_observations: dict[str, dict[str, str]] = {}
    figure_warning: str | None = None
    if (
        supplied_budget is None
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
                deadline=deadline,
            )
        )
    if figure_observations:
        page_plan = planning.estimate_page(
            budget,
            page=input_data.get("page"),
            orientation=requested_orientation,
            figure_observations=figure_observations,
        ).to_dict()
    figure_observations = figure_analysis.normalize_observations(
        figure_observations,
        selected_sha256s=set(draft_figure_hashes),
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
    identity = paper_identity.merge(source.summary, budget.get("paper_identity", {}))
    budget["paper_identity"] = identity
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
        "identity_warnings": list(source.summary.get("identity_diagnostics", {}).values()),
        "paper_source": _bind_paper_identity(
            {**source.summary, **identity},
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
    revision_state.refresh_paper_identity(state)
    if planning_issue is not None:
        outcomes.update_workflow_issue(state, "planning", planning_issue)
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


def _source_carrier_budget(
    *,
    source_text: str,
    source_kind: str,
    source_figure_sha256s: set[str],
    source_equations: list[dict[str, str]],
    orientation: str,
) -> dict[str, Any]:
    """Seed joint composition with a bounded excerpt of the exact source."""

    excerpt = _source_excerpt(source_text, source_kind=source_kind)
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
                    "source_label": " ".join(source_text.split()[:12]),
                    "figure_sha256s": [],
                    "equations": [],
                }
            ],
        },
        source_figure_sha256s=source_figure_sha256s,
        source_equations=source_equations,
        source_text=source_text,
    )


def _source_excerpt(source_text: str, *, source_kind: str) -> str:
    """Return a bounded plain-text prefix without selecting scientific evidence."""

    from posterlib.content.equations import remove_delimited_math

    if source_kind == "latex":
        from pylatexenc import latex2text

        from posterlib.sources import tex_parser

        document = next(tex_parser.environments(source_text, ("document",)), None)
        body = document.body if document is not None else source_text
        text_context = latex2text.get_default_latex_context_db()
        # Bibliographic fields already own the title band. Suppress generated
        # title matter, including maketitle's implicit current date.
        text_context.add_context_category(
            "poster-source-carrier",
            macros=[latex2text.MacroTextSpec("maketitle", discard=True)],
            prepend=True,
        )
        plain = latex2text.LatexNodes2Text(
            latex_context=text_context, math_mode="remove",
        ).latex_to_text(body)
    else:
        plain = remove_delimited_math(source_text)
    text = " ".join(" ".join(
        line.strip()
        for line in plain.splitlines()
        if line.strip() and not (
            line.strip().startswith("[Page ")
            and line.strip().endswith("]")
            and line.strip()[6:-1].isdecimal()
        )
    ).split())
    return text if len(text) <= 1600 else text[:1600].rsplit(" ", 1)[0].strip()


async def _restore_state(
    checkpoint: dict[str, Any],
    *,
    ctx: Any,
    max_source_chars: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = dict(checkpoint)
    source_text = str(state["source_text"])
    _validate_source_size(source_text, max_source_chars)
    revision_state.refresh_paper_identity(state)
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
        state["content_budget"],
        source_figure_sha256s=expected_figures,
        source_text=source_text,
        source_equations=source_equations,
        source_assets=assets,
    )
    state["content_budget"] = budget
    state["source_figure_observations"] = figure_analysis.normalize_observations(
        state.get("source_figure_observations"),
        selected_sha256s={str(asset["content_sha256"]) for asset in assets},
    )
    return state, assets


async def _reference_stage(
    state: Mapping[str, Any],
) -> reference_catalog.ReferenceBundle:
    """Resolve an explicit reference or one published academic example."""

    custom_image = str(state.get("reference_image") or "").strip()
    if custom_image:
        try:
            return reference_catalog.load_custom_bundle(
                custom_image.removeprefix("file://"),
            )
        except reference_catalog.ReferenceSeedError as exc:
            raise DraftPipelineError("invalid_payload", str(exc)) from exc
    page = state["page_plan"]
    orientation = str(page.get("orientation") or "").strip().lower()
    if orientation not in {"landscape", "portrait"}:
        orientation = "landscape" if page["width_mm"] >= page["height_mm"] else "portrait"
    seed = reference_catalog.select_seed(
        orientation=orientation,
    )
    return reference_catalog.load_seed_bundle(seed)


async def _reference_analysis_stage(
    state: dict[str, Any],
    *,
    reference: reference_catalog.ReferenceBundle,
    client: Any,
    deadline: float | None = None,
) -> reference_analysis.ReferenceAnalysis:
    """Ask the visual model for a reference brief without compiling a layout."""

    try:
        reference = replace(reference, image_path=str(reference.verified_image_path()))
    except reference_catalog.ReferenceSeedError as exc:
        plan = reference_analysis.ReferenceAnalysis(
            reference_image_sha256=reference.image_sha256,
            planner="adaptive",
            warning=f"Visual reference image is unavailable; composition uses the paper and user direction. ({exc})",
        )
    else:
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


def _append_warning_once(warnings: list[Any], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


async def _author_stage(
    progress_callback: Any,
    *,
    assets: list[dict[str, Any]],
    expected_figures: set[str],
    budget: dict[str, Any],
    page_plan: dict[str, Any],
    paper_source: dict[str, Any],
    source_text: str,
    reference_brief: reference_analysis.ReferenceAnalysis,
    llm: Any,
    source_figure_observations: Mapping[str, Mapping[str, str]] | None = None,
    authoring_request: str = "",
    design_requirements: Mapping[str, Any] | None = None,
    source_html: str | None = None,
    feedback: str = "",
    timeout_seconds: float | None = None,
    max_transport_retries: int = model_runtime.DEFAULT_AUTHORING_TRANSPORT_RETRIES,
    deadline: float | None = None,
    inspect: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    initial_inspection: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
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
    initial_composition = source_html is None or revision_state.is_content_carrier(content_carrier)

    def validate(candidate: str) -> dict[str, Any]:
        report = html_contract.validate_candidate(
            candidate,
            assets=assets,
            required_source_figure_sha256s=expected_figures,
            expected_page=expected_page,
            paper_identity=paper_source,
            source_text=source_text,
            source_equations=source_preparation.structured_source_equations(paper_source),
        )
        if (
            initial_composition
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
    system, user = authoring.layout_composition_prompt(
        feedback=feedback or "Compose the scientific argument from the complete paper.",
        content_brief=budget,
        page_plan=page_plan,
        authoring_request=authoring_request,
        reference_brief=reference_brief,
        design_requirements=design_requirements,
        source_figure_observations=source_figure_observations or {},
        initial_composition=initial_composition,
        source_text=source_text,
        assets=assets,
    )
    edit = await model_runtime.request_html_revision(
        llm,
        source_html=content_carrier,
        system=system,
        user=user,
        validate=validate,
        inspect=inspect,
        initial_inspection=initial_inspection,
        initial_composition=initial_composition,
        compact_layout=True,
        include_rendered_image=model_runtime.supports_image_input(llm),
        timeout_seconds=timeout_seconds,
        max_transport_retries=max_transport_retries,
        deadline=deadline,
    )
    authored = edit.candidate.html
    inspection = edit.candidate.inspection
    authoring_issue = edit.issue or (
        {
            "code": "authoring_unavailable",
            "phase": "editing",
            "summary": (
                "The author did not replace the grounded starter composition; "
                "its rendered version remains available for visual review."
            ),
        }
        if initial_composition and authored == content_carrier
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
) -> venue_branding.VenueBranding | None:
    """Resolve one venue identity explicitly supplied by the user."""

    conference = input_data.get("conference")
    if isinstance(conference, str) and conference.strip():
        return venue_branding.resolve_venue_branding(conference)
    return None


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
    "run_draft",
]
