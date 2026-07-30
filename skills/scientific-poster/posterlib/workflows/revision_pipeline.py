"""Grounded HTML revision pipeline for an existing poster version."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

import poster_core

from posterlib.content import html_contract, scientific_snapshot
from posterlib.generation import authoring, model_runtime
from posterlib.runtime import runtime_io
from posterlib.visual import reference_seeds, visual_design, visual_review

from . import (
    draft_checkpoint,
    draft_pipeline,
    request_normalization,
    revision_state,
    runtime_budget,
    workflow_outcomes,
)

RevisionResult = TypeVar("RevisionResult")
VersionLookup = Callable[[str], dict[str, Any] | None]
PublishVersion = Callable[..., Awaitable[dict[str, Any] | RevisionResult]]

_REVISION_REPAIRABLE_SOURCE_ISSUES = frozenset(
    {
        "malformed_mathml_operator",
        "math_layout_override",
        "ungrounded_rights_claim",
    }
)


async def run_revision(
    input_data: dict[str, Any],
    progress_callback: Any,
    *,
    ctx: Any,
    version_lookup: VersionLookup,
    publish_version: PublishVersion[RevisionResult],
    defer_revision_commit: object,
    max_source_chars: int,
    noop_revision_warning: str,
    deadline: float | None = None,
) -> dict[str, Any] | RevisionResult:
    """Revise one exact grounded HTML version without owning engine state."""

    input_data = dict(input_data)
    defer_commit = (
        input_data.pop("_defer_revision_commit", None) is defer_revision_commit
    )
    source_uri = str(input_data.get("source_html_uri") or "").strip()
    source_path = await runtime_io.resolve_path(ctx, source_uri)
    state = version_lookup(source_uri)
    if source_path is None and state is not None:
        cached = Path(str(state.get("artifact_path") or ""))
        source_path = cached if cached.is_file() else None
    if source_path is None:
        return workflow_outcomes.error_result(
            "source_not_found",
            "The requested poster HTML artifact could not be resolved.",
        )
    try:
        source_bytes = source_path.read_bytes()
        source_html = source_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return workflow_outcomes.error_result("source_read_failed", str(exc))
    parent_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if state is None:
        state = revision_state.revision_checkpoint_state(
            source_path,
            source_html=source_html,
            html_sha256=parent_sha256,
        )
    if state is None:
        return workflow_outcomes.error_result(
            "source_read_failed",
            "Revision requires the draft checkpoint sidecar bound to this HTML.",
        )
    if parent_sha256 != state["html_sha256"]:
        return workflow_outcomes.error_result(
            "source_read_failed",
            "The source URI bytes changed after publication.",
        )
    expected_sha256 = str(input_data.get("source_html_sha256") or "").strip()
    if expected_sha256 and expected_sha256 != parent_sha256:
        return workflow_outcomes.error_result(
            "stale_selection",
            "source_html_sha256 does not match the source artifact bytes.",
        )

    workspace = runtime_io.create_workspace(input_data, ctx)
    source_text = str(state.get("source_text") or "")
    content_budget = (
        state.get("content_budget")
        if isinstance(state.get("content_budget"), dict)
        else None
    )
    raw_checkpoint_reference = state.get("design_reference")
    reference_image_sha256 = (
        str(raw_checkpoint_reference.get("image_sha256") or "").strip()
        if isinstance(raw_checkpoint_reference, Mapping)
        else ""
    )
    required_source_figure_sha256s = set(state.get("source_figure_sha256s") or ())
    if not source_text:
        return workflow_outcomes.error_result(
            "missing_input",
            "The draft checkpoint has no grounded source text.",
        )
    if len(source_text) > max_source_chars:
        return workflow_outcomes.error_result(
            "source_too_large",
            f"Research input exceeds the {max_source_chars}-character safety limit.",
        )
    source_report = poster_core.validate_poster_html(
        source_html,
        source_text=source_text,
    )
    blocking_source_issues = [
        item
        for item in source_report.get("issues", [])
        if isinstance(item, dict)
        and item.get("code") not in _REVISION_REPAIRABLE_SOURCE_ISSUES
    ]
    if source_report.get("status") != "ok" and blocking_source_issues:
        return workflow_outcomes.error_result(
            "source_html_invalid",
            "The source poster no longer satisfies the HTML contract.",
        )
    selection = input_data.get("selection_state")
    if selection is not None:
        try:
            selection = revision_state.validate_revision_selection(
                selection,
                parent_sha256=parent_sha256,
                source_html=source_html,
            )
        except revision_state.SelectionStateError as exc:
            return workflow_outcomes.error_result(exc.code, str(exc))

    caller_feedback = str(input_data.get("feedback") or "").strip()
    feedback = caller_feedback
    visual_review_path = str(input_data.get("visual_review_path") or "").strip()
    receipt_operations = frozenset[str]()
    content_replan_targets = frozenset[str]()
    if visual_review_path:
        try:
            (
                visual_feedback,
                visual_iteration,
                receipt_operations,
                content_replan_targets,
            ) = revision_state.visual_revision_feedback(
                visual_review_path,
                parent_html_sha256=parent_sha256,
                reference_image_sha256=reference_image_sha256,
                content_budget=content_budget,
            )
            feedback = "\n\n".join(
                item for item in (visual_feedback, caller_feedback) if item
            )
        except visual_review.VisualReviewError as exc:
            return workflow_outcomes.error_result(exc.code, str(exc))
        input_data["visual_iteration"] = visual_iteration
    elif not defer_commit:
        # A normal user/harness revision starts a fresh bounded visual-review
        # cycle.  Do not inherit an exhausted iteration from the source draft.
        input_data["visual_iteration"] = 0
        input_data["inspection_repair_attempt"] = 0
    preferred_asset_tokens: dict[str, str] = {}
    checkpoint_assets: list[dict[str, Any]] = []
    if all(
        name in state
        for name in ("asset_inputs", "asset_sha256s", "source_figure_sha256s")
    ):
        try:
            checkpoint_assets = await draft_pipeline.prepare_checkpoint_assets(
                state,
                ctx,
            )
        except draft_pipeline.DraftPipelineError as exc:
            return workflow_outcomes.error_result(exc.code, str(exc))
        preferred_asset_tokens = {
            str(item["content_sha256"]): str(item["token"])
            for item in checkpoint_assets
        }
    html_template, embedded_assets = html_contract.tokenize_embedded_images(
        source_html,
        preferred_tokens=preferred_asset_tokens,
    )
    checkpoint_assets_by_hash = {
        str(item.get("content_sha256") or ""): item for item in checkpoint_assets
    }
    contract_assets = [
        {
            **item,
            "description": str(
                checkpoint_assets_by_hash.get(
                    str(item.get("content_sha256") or ""), {}
                ).get("description")
                or item.get("description")
                or ""
            ),
        }
        for item in embedded_assets
    ]
    raw_design_reference = state.get("design_reference")
    try:
        if not isinstance(raw_design_reference, Mapping):
            return workflow_outcomes.error_result(
                "missing_input",
                "Revision requires the exact design_reference used for the draft.",
            )
        design_reference = reference_seeds.ReferenceBundle.from_dict(
            dict(raw_design_reference)
        )
        raw_visual_design = state.get("visual_design")
        if not isinstance(raw_visual_design, Mapping):
            return workflow_outcomes.error_result(
                "missing_input",
                "Revision requires the exact visual_design used for the draft.",
            )
        visual_design_plan = visual_design.VisualDesignPlan.from_dict(
            dict(raw_visual_design)
        )
        revision_warnings: list[str] = []
    except (TypeError, reference_seeds.ReferenceSeedError):
        return workflow_outcomes.error_result(
            "source_read_failed",
            "Revision design_reference is invalid.",
        )
    except visual_design.VisualDesignError:
        return workflow_outcomes.error_result(
            "source_read_failed",
            "Revision visual_design is invalid.",
        )
    paper_identity = _revision_paper_identity(state, embedded_assets)
    required_source_figure_sha256s.update(
        html_contract.source_figure_sha256s(embedded_assets)
    )
    page = source_report.get("page")
    stored_page_plan = state.get("page_plan")
    if not isinstance(page, dict):
        return workflow_outcomes.error_result(
            "source_html_invalid",
            "Source poster has no physical page.",
        )
    revision_page_plan, allow_adaptive_height = revision_state.revision_page_policy(
        stored_page_plan=stored_page_plan,
        page=page,
    )
    checkpoint_source = revision_state.revision_checkpoint_source(workspace, state)
    if defer_commit:
        baseline_checkpoint = revision_state.revision_checkpoint_payload(
            checkpoint_source,
            html_template=html_template,
            page_plan=dict(revision_page_plan),
            visual_iteration=revision_state.visual_iteration(
                (checkpoint_source or {}).get("visual_iteration", 0)
            ),
            inspection_repair_attempt=revision_state.inspection_repair_attempt(
                checkpoint_source or {}
            ),
            preserve_pending_visual_revision=True,
        )
        if baseline_checkpoint is not None:
            draft_checkpoint.save(
                workspace,
                stage="author-ready",
                state=baseline_checkpoint,
            )
    caller_mode = str(input_data.get("revision_mode") or "").strip()
    if not visual_review_path and caller_mode == "content-replan":
        try:
            content_replan_targets = revision_state.validate_content_replan_targets(
                input_data.get("content_replan_targets"),
                content_budget=content_budget,
            )
        except ValueError as exc:
            return workflow_outcomes.error_result("invalid_payload", str(exc))
    revision_mode = (
        revision_state.vlm_revision_mode(
            receipt_operations,
            requested_mode=caller_mode,
        )
        if visual_review_path
        else (
            caller_mode
            if caller_mode in {"style-only", "content-replan"}
            else "full-layout"
        )
    )
    style_only = revision_mode == "style-only"
    content_brief = revision_state.visual_content_brief(
        content_budget,
        revision_page_plan,
        displayed_html=html_template,
    )
    if style_only:
        system, user = authoring.stylesheet_revision_prompt(
            source_html=html_template,
            feedback=feedback,
            page_plan=revision_page_plan,
            allow_adaptive_height=allow_adaptive_height,
            visual_design_plan=visual_design_plan,
        )
    else:
        system, user = authoring.revision_prompt(
            source_html=html_template,
            feedback=feedback,
            selection=selection,
            page_plan=revision_page_plan,
            allow_adaptive_height=allow_adaptive_height,
            design_reference=design_reference,
            visual_design_plan=visual_design_plan,
            content_brief=content_brief,
            revision_mode=revision_mode,
            content_replan_targets=sorted(content_replan_targets),
        )

    def validate_for_plan(
        candidate: str,
        candidate_page_plan: dict[str, Any],
    ) -> dict[str, Any]:
        report = html_contract.validate_candidate(
            candidate,
            source_text=source_text,
            assets=embedded_assets,
            required_source_figure_sha256s=required_source_figure_sha256s,
            expected_page=candidate_page_plan,
            allow_adaptive_height=allow_adaptive_height,
            content_contract=candidate_page_plan.get("content_contract"),
            paper_identity=paper_identity,
        )
        if not style_only:
            snapshot_issues = (
                scientific_snapshot.grounded_replan_snapshot_issues(
                    html_template,
                    candidate,
                    target_module_ids=content_replan_targets,
                )
                if revision_mode == "content-replan"
                else scientific_snapshot.scientific_content_snapshot_issues(
                    html_template,
                    candidate,
                )
            )
            if snapshot_issues:
                return {
                    **report,
                    "status": "error",
                    "issues": [
                        *[
                            item
                            for item in report.get("issues", [])
                            if isinstance(item, dict)
                        ],
                        *snapshot_issues,
                    ],
                }
        return report

    def validate(candidate: str) -> dict[str, Any]:
        return validate_for_plan(candidate, revision_page_plan)

    def checkpoint_for(
        candidate_template: str,
        candidate_page_plan: dict[str, Any],
    ) -> dict[str, Any] | None:
        visual_iteration = (
            revision_state.visual_iteration(input_data.get("visual_iteration"))
            if visual_review_path or defer_commit
            else 0
        )
        return revision_state.revision_checkpoint_payload(
            checkpoint_source,
            html_template=candidate_template,
            page_plan=candidate_page_plan,
            visual_iteration=visual_iteration,
            inspection_repair_attempt=(
                revision_state.inspection_repair_attempt(input_data)
                if "inspection_repair_attempt" in input_data
                else 0
            ),
        )

    live_path_value = str(state.get("live_html_path") or "").strip()
    live_path = Path(live_path_value) if live_path_value else None

    async def publish_template(
        candidate_template: str,
        candidate_page_plan: dict[str, Any],
        *,
        activate: bool,
    ) -> dict[str, Any] | RevisionResult:
        return await publish_version(
            html_text=html_contract.embed_assets(candidate_template, embedded_assets),
            source_text=source_text,
            input_data=input_data,
            progress_callback=progress_callback,
            workspace=workspace,
            parent_html_sha256=parent_sha256,
            live_html_path=live_path,
            asset_warnings=revision_warnings,
            inspection=None,
            source_figure_sha256s=required_source_figure_sha256s,
            page_plan=candidate_page_plan,
            content_budget=state.get("content_budget")
            if isinstance(state.get("content_budget"), dict)
            else None,
            design_reference=design_reference,
            visual_design_plan=visual_design_plan,
            visual_preferences=dict(state["visual_preferences"]),
            activate=activate,
            checkpoint_state=checkpoint_for(
                candidate_template,
                candidate_page_plan,
            ),
            deadline=deadline,
        )

    try:
        await runtime_io.progress(progress_callback, "poster.revise", 0.18)
        llm = runtime_budget.host_llm(ctx)
        timeout_seconds, transport_retries = (
            request_normalization.authoring_transport_options(input_data)
        )
        if style_only:
            revised_template = await model_runtime.request_stylesheet(
                llm,
                system=system,
                user=user,
                apply_stylesheet=lambda stylesheet: (
                    html_contract.replace_single_stylesheet(
                        html_template,
                        stylesheet,
                    )
                ),
                validate=validate,
                max_repair_attempts=model_runtime.MAX_REPAIR_ATTEMPTS,
                timeout_seconds=timeout_seconds,
                max_transport_retries=transport_retries,
                deadline=deadline,
            )
        else:

            def canonicalize_full_revision(candidate: str) -> str:
                bound = html_contract.bind_authored_contract(
                    candidate,
                    content_budget=(
                        content_budget if isinstance(content_budget, dict) else None
                    ),
                    page_plan=revision_page_plan,
                    paper_identity=paper_identity,
                    assets=contract_assets,
                )
                if revision_mode == "full-layout":
                    return scientific_snapshot.restore_frozen_module_text(
                        html_template,
                        bound,
                    )
                return bound

            revised_template = await model_runtime.request_html(
                llm,
                system=system,
                user=user,
                repair_system=authoring.html_repair_system(revision_mode=revision_mode),
                repair_context=user,
                validate=validate,
                canonicalize=canonicalize_full_revision,
                max_repair_attempts=model_runtime.MAX_REPAIR_ATTEMPTS,
                initial_temperature=0.0,
                timeout_seconds=timeout_seconds,
                max_transport_retries=transport_retries,
                deadline=deadline,
            )
        if revised_template == html_template:
            return poster_core.outcome_result(
                "visual_revision_required",
                summary=(
                    "The revision model returned identical HTML; the exact source "
                    "candidate was preserved."
                ),
                html_uri=source_uri,
                html_sha256=parent_sha256,
                revision_noop=True,
                requires_approval=False,
                visual_quality_state="awaiting-review",
                warnings=[noop_revision_warning],
            )
        revision_report = validate(revised_template)
        revised_page = revision_report.get("page")
        if allow_adaptive_height and isinstance(revised_page, dict):
            revision_page_plan["height_mm"] = revised_page.get("height_mm")
    except model_runtime.ModelBoundaryError as exc:
        return workflow_outcomes.error_result(exc.code, str(exc))
    return await publish_template(
        revised_template,
        revision_page_plan,
        activate=not defer_commit,
    )


def _revision_paper_identity(
    state: dict[str, Any] | None,
    embedded_assets: list[dict[str, str]],
) -> dict[str, Any] | None:
    """Rebind a verified venue logo after revision-only asset tokenization."""

    raw_identity = (state or {}).get("paper_source")
    if not isinstance(raw_identity, dict):
        return None
    identity = dict(raw_identity)
    raw_venue = raw_identity.get("venue_identity")
    if not isinstance(raw_venue, dict):
        return identity
    venue = dict(raw_venue)
    logo_digest = str(venue.get("logo_asset_sha256") or "").strip().lower()
    if logo_digest:
        matching_asset = next(
            (
                asset
                for asset in embedded_assets
                if str(asset.get("content_sha256") or "").lower() == logo_digest
            ),
            None,
        )
        if matching_asset is not None:
            venue["logo_asset_token"] = str(matching_asset["token"])
    identity["venue_identity"] = venue
    return identity
