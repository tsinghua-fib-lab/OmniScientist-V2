"""Portable host orchestration for direct HTML scientific-poster authoring."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

import poster_contract as _core  # noqa: E402
from posterlib.content import html_contract as _html_contract  # noqa: E402
from posterlib.content import planning as _planning  # noqa: E402
from posterlib.delivery import portable_actions as _portable_actions  # noqa: E402
from posterlib.generation import model_runtime as _model_runtime  # noqa: E402
from posterlib.runtime import browser_inspection as _browser_inspection  # noqa: E402
from posterlib.runtime import runtime_io as _runtime_io  # noqa: E402
from posterlib.sources import paper_extraction as _paper_extraction  # noqa: E402
from posterlib.sources import source_preparation as _source_preparation  # noqa: E402
from posterlib.visual import reference_analysis as _reference_analysis  # noqa: E402
from posterlib.visual import reference_catalog as _reference_catalog  # noqa: E402
from posterlib.visual import review_contract as _review_contract  # noqa: E402
from posterlib.workflows import deadlines as _deadlines  # noqa: E402
from posterlib.workflows import draft_checkpoint as _draft_checkpoint  # noqa: E402
from posterlib.workflows import draft_pipeline as _draft_pipeline  # noqa: E402
from posterlib.workflows import outcomes as _outcomes  # noqa: E402
from posterlib.workflows import (  # noqa: E402
    request_normalization as _request_normalization,
)
from posterlib.workflows import review_pipeline as _review_pipeline  # noqa: E402
from posterlib.workflows import revision_pipeline as _revision_pipeline  # noqa: E402
from posterlib.workflows import revision_state as _revision_state  # noqa: E402

_MAX_SOURCE_CHARS = 1_500_000
_PPTX_EXPORT_WARNING = (
    "Editable PPTX export did not complete; the exact HTML candidate remains available."
)
_INVALID_VISUAL_REVIEW_WARNING = (
    "Visual review receipt is unavailable for this exact HTML candidate; the poster "
    "remains unapproved."
)
_PORTABLE_HOST_METADATA_FIELDS = frozenset(
    {
        "channel",
        "dependency_failures",
        "depends_on_results",
        "file_uri",
        "input",
        "project",
        "run_id",
        "runtime_steer",
        "task_id",
        "tenant_id",
        "user_id",
        "workflow_goal",
        "workflow_results",
        "workflow_step_id",
    }
)


def _visual_reflow_targets(review: Any) -> frozenset[str]:
    """Return module targets that the validated review asks to reflow."""

    if not isinstance(review, Mapping):
        return frozenset()
    issues = review.get("critical_issues")
    if not isinstance(issues, list):
        return frozenset()
    return frozenset(
        str(target).strip()
        for issue in issues
        if isinstance(issue, Mapping) and issue.get("operation") == "reflow"
        for target in issue.get("targets", [])
        if str(target).strip()
    )


def _activate_candidate_files(
    result: Mapping[str, Any], *, selected_state: bytes | None = None
) -> None:
    """Restore the selected candidate after comparing a later proposal."""

    workspace = Path(str(result.get("workspace") or ""))
    digest = str(result.get("html_sha256") or "")
    source = workspace / ".candidates" / digest / "poster.html"
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("The selected candidate does not match its recorded HTML hash.")
    raw_html_path = str(result.get("html_path") or "").strip()
    source_sidecar = _revision_state.revision_sidecar_path(source)
    if selected_state is not None:
        _runtime_io.replace_file_atomic(source_sidecar, selected_state)
    if raw_html_path and _draft_checkpoint.load_path(source_sidecar) is None:
        raise ValueError("The selected candidate has no valid draft checkpoint.")
    for field in ("html_path", "live_html_path"):
        raw_path = str(result.get(field) or "").strip()
        if raw_path:
            _runtime_io.replace_file_atomic(Path(raw_path), content)
    if raw_html_path:
        html_path = Path(raw_html_path)
        _runtime_io.replace_file_atomic(
            _revision_state.revision_sidecar_path(html_path),
            source_sidecar.read_bytes(),
        )
        if not _draft_checkpoint.restore(workspace, source_sidecar):
            raise ValueError("The selected candidate has no valid draft checkpoint.")
    if selected_state is not None:
        for artifact in result.get("artifacts") or []:
            if not isinstance(artifact, Mapping) or not (
                artifact.get("kind") == "poster"
                and artifact.get("format") == "html"
                and artifact.get("sha256") == digest
            ):
                continue
            artifact_path = Path(str(artifact.get("path") or ""))
            if artifact_path.is_file() and hashlib.sha256(
                artifact_path.read_bytes()
            ).hexdigest() == digest:
                _runtime_io.replace_file_atomic(
                    _revision_state.revision_sidecar_path(artifact_path),
                    selected_state,
                )


def _append_artifact_once(
    result: dict[str, Any],
    artifact: Mapping[str, Any],
) -> None:
    """Append one immutable artifact descriptor without duplicating it."""

    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
        result["artifacts"] = artifacts
    uri = str(artifact.get("uri") or "")
    digest = str(artifact.get("sha256") or "")
    fmt = str(artifact.get("format") or "")
    for existing in artifacts:
        if not isinstance(existing, Mapping):
            continue
        if uri and str(existing.get("uri") or "") == uri:
            return
        if (
            digest
            and str(existing.get("sha256") or "") == digest
            and str(existing.get("format") or "") == fmt
            and str(existing.get("title") or "") == str(artifact.get("title") or "")
        ):
            return
    artifacts.append(dict(artifact))


async def _store_delivery_artifact(
    ctx: Any,
    path: Path,
    *,
    kind: str,
    title: str,
    fmt: str,
    mime: str,
    warnings: list[str],
) -> dict[str, Any]:
    """Keep a primary delivery available when host artifact registration fails."""

    try:
        return await _runtime_io.store_artifact(
            ctx,
            path,
            kind=kind,
            title=title,
            fmt=fmt,
            mime=mime,
        )
    except OSError as exc:
        warnings.append(
            f"{title} could not be registered with the host; using its immutable "
            f"local snapshot instead: {exc}"
        )
        return _runtime_io.snapshot_artifact(
            path,
            kind=kind,
            title=title,
            fmt=fmt,
            mime=mime,
        )


def _downgrade_invalid_visual_review(
    result: dict[str, Any],
    *,
    summary: str,
) -> dict[str, Any]:
    """Discard a receipt whose candidate identity is invalid."""

    result.pop("visual_review_uri", None)
    result["visual_review_mode"] = "vlm"
    return _outcomes.visual_review_unavailable(result, summary)


def _pptx_export_failure(
    result: dict[str, Any],
    *,
    code: str,
    summary: str,
    outcome: Any = None,
) -> dict[str, Any]:
    """Keep the HTML deliverable while marking the poster package incomplete."""

    _outcomes.append_warning_once(result, _PPTX_EXPORT_WARNING)
    if result.get("status") != "error":
        result["status"] = "partial"
    result["pptx_export"] = {
        "status": "error",
        "outcome": outcome if isinstance(outcome, Mapping) else {"code": code},
        "summary": summary,
    }
    return result


class ScientificPosterEngine:
    """Ask the host model for complete inert HTML and manage review versions."""

    def __init__(self) -> None:
        self._visual_runtime: _review_pipeline.ReviewRuntime | None = None
        self._visual_reviewer: Any = None
        self._visual_environ: dict[str, str] | None = None

    @staticmethod
    def validate_params(
        *,
        arguments: dict | None = None,
        input_data: dict | None = None,
    ) -> dict[str, Any] | None:
        """Validate the engine boundary before a host invokes it."""

        raw = arguments if arguments is not None else input_data or {}
        data = _request_normalization.normalize_poster_input(raw)
        _, error = _request_normalization.validate_action_boundary(data)
        return error

    async def execute(
        self,
        progress_callback: Any = None,
        **input_data: Any,
    ) -> dict[str, Any]:
        """Dispatch model-backed authoring or a portable deterministic action."""

        input_data = _request_normalization.normalize_poster_input(input_data)
        action, error = _request_normalization.validate_action_boundary(input_data)
        if error is not None:
            return error
        assert action is not None
        if (
            action in {_core.ACTION_DRAFT, _core.ACTION_REVISE}
            and self._visual_runtime is None
        ):
            self._visual_runtime = _review_pipeline.runtime_from_context(
                getattr(self, "ctx", None), self._visual_environ,
            )
        if action == _core.ACTION_DRAFT:
            return await self._draft(input_data, progress_callback)
        if action == _core.ACTION_ESTIMATE:
            return await self._estimate(input_data, progress_callback)
        if action == _core.ACTION_REVISE:
            loop = asyncio.get_running_loop()
            deadline = _deadlines.workflow_deadline(
                getattr(self, "ctx", None), loop.time(), input_data
            )
            edit_deadline = _deadlines.editing_deadline(deadline, loop.time())
            result = await self._revise(
                input_data,
                progress_callback,
                deadline=edit_deadline,
            )
            if _outcomes.revision_model_timed_out(result):
                preserved = {
                    "html_uri": str(input_data.get("source_html_uri") or ""),
                    "html_sha256": str(input_data.get("source_html_sha256") or ""),
                    "warnings": [],
                }
                return _outcomes.revision_timeout_error(preserved, result)
            if result.get("status") != "error":
                receipt_path = str(input_data.get("visual_review_path") or "")
                if (
                    receipt_path
                    and result.get("html_sha256")
                    == input_data.get("source_html_sha256")
                ):
                    receipt = _review_contract.load_receipt(
                        receipt_path,
                        expected_html_sha256=str(result["html_sha256"]),
                    )
                    result["visual_review"] = receipt
                    result["visual_review_path"] = receipt_path
                    result["visual_review_mode"] = "vlm"
                    result["visual_quality_state"] = receipt["quality_state"]
                    result = await self._attach_visual_review_receipt(result)
                    result = _outcomes.visual_outcome(
                        result,
                        (
                            "visual_review_passed"
                            if receipt["quality_state"] == "passed"
                            else "visual_revision_required"
                        ),
                        str(receipt["summary"]),
                    )
                else:
                    result = await self._review_and_revise(
                        result,
                        input_data=input_data,
                        progress_callback=progress_callback,
                        deadline=edit_deadline,
                    )
            return await self._attach_editable_pptx(
                result,
                progress_callback=progress_callback,
                deadline=deadline,
            )

        portable_input = {**input_data, "action": action}
        if action in {
            _core.ACTION_APPROVE,
            _core.ACTION_INSPECT,
            _core.ACTION_EXPORT_PPTX,
        }:
            source_uri = str(portable_input.get("source_html_uri") or "").strip()
            if source_uri:
                source_path = await _runtime_io.resolve_path(
                    getattr(self, "ctx", None),
                    source_uri,
                    base_dir=portable_input.get("cwd"),
                )
                if source_path is None:
                    return _outcomes.error_result(
                        "source_not_found",
                        "The requested poster HTML artifact could not be resolved.",
                    )
                expected_sha256 = str(
                    portable_input.get("source_html_sha256") or ""
                ).strip()
                if expected_sha256:
                    actual_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
                    if actual_sha256 != expected_sha256:
                        return _outcomes.error_result(
                            "stale_selection",
                            "source_html_sha256 does not match the source artifact bytes.",
                        )
                if action == _core.ACTION_APPROVE:
                    portable_input.setdefault("source_html_path", str(source_path))
                else:
                    portable_input.setdefault("html", str(source_path))
                    portable_input.pop("source_html_uri", None)
                    portable_input.pop("source_html_sha256", None)
        for field in _PORTABLE_HOST_METADATA_FIELDS:
            portable_input.pop(field, None)
        if action != _core.ACTION_APPROVE:
            portable_input.pop("session_id", None)
        if action != _core.ACTION_APPROVE:
            portable_input.pop("host_event_id", None)
        return await asyncio.to_thread(_portable_actions.run, portable_input)

    async def _estimate(
        self,
        input_data: dict[str, Any],
        progress_callback: Any,
    ) -> dict[str, Any]:
        """Build a grounded evidence budget and recommend a physical page."""

        ctx = getattr(self, "ctx", None)
        deadline = _deadlines.workflow_deadline(
            ctx, asyncio.get_running_loop().time(), input_data
        )
        workspace = _runtime_io.create_workspace(input_data, ctx)
        try:
            source = await _source_preparation.prepare_draft_source(
                input_data,
                ctx=ctx,
                workspace=workspace,
                progress_callback=progress_callback,
            )
            if len(source.text) > _MAX_SOURCE_CHARS:
                return _outcomes.error_result(
                    "source_too_large",
                    f"Research input exceeds the {_MAX_SOURCE_CHARS}-character safety limit.",
                )
            assets, asset_warnings = await _source_preparation.prepare_assets(
                [
                    *source.assets,
                    *[
                        _source_preparation.normalize_user_asset(item)
                        for item in _source_preparation.normalize_asset_inputs(
                            input_data.get("assets")
                        )
                    ],
                ],
                ctx,
            )
            expected_figures = _html_contract.source_figure_sha256s(assets)
            source_equations = _source_preparation.structured_source_equations(
                source.summary
            )
            await _runtime_io.progress(progress_callback, "poster.plan-content", 0.12)
            budget = await _model_runtime.request_content_plan(
                _model_runtime.require_host_llm(ctx),
                source_text=source.text,
                assets=assets,
                source_figure_sha256s=expected_figures,
                authoring_request=source.authoring_request,
                max_repair_attempts=_request_normalization.repair_attempts(input_data),
                source_equations=source_equations,
                page=input_data.get("page"),
                orientation=str(input_data.get("orientation") or "auto"),
                deadline=deadline,
            )
            page_plan = _planning.estimate_page(
                budget,
                page=input_data.get("page"),
                orientation=str(input_data.get("orientation") or "auto"),
            )
            budget = await _model_runtime.request_content_plan(
                _model_runtime.require_host_llm(ctx),
                source_text=source.text,
                assets=assets,
                source_figure_sha256s=expected_figures,
                authoring_request=source.authoring_request,
                max_repair_attempts=_request_normalization.repair_attempts(
                    input_data
                ),
                source_equations=source_equations,
                page={
                    "width_mm": page_plan.width_mm,
                    "height_mm": page_plan.height_mm,
                },
                orientation=page_plan.orientation,
                draft_budget=budget,
                deadline=deadline,
            )
            page_plan = _planning.estimate_page(
                budget,
                page=input_data.get("page"),
                orientation=page_plan.orientation,
            )
        except _model_runtime.ModelBoundaryError as exc:
            return _outcomes.error_result(exc.code, str(exc))
        except _planning.PlanningError as exc:
            return _outcomes.error_result(exc.code, str(exc))
        except _paper_extraction.PaperSourceError as exc:
            return _outcomes.paper_source_failure(exc)
        except (OSError, UnicodeError, ValueError) as exc:
            return _outcomes.error_result(
                "candidate_validation_failed",
                f"Poster estimation failed: {exc}",
            )
        await _runtime_io.progress(progress_callback, "poster.plan-ready", 1.0)
        return _core.outcome_result(
            "estimate_complete",
            summary="Grounded content budget and physical page recommendation are ready.",
            content_budget=budget,
            page_plan=page_plan.to_dict(),
            paper_source=source.summary,
            expected_source_figure_sha256s=sorted(expected_figures),
            warnings=[*source.warnings, *asset_warnings],
        )

    async def _draft(
        self,
        input_data: dict[str, Any],
        progress_callback: Any,
    ) -> dict[str, Any]:
        """Author one self-contained HTML poster from supplied research."""

        ctx = getattr(self, "ctx", None)
        loop = asyncio.get_running_loop()
        deadline = _deadlines.workflow_deadline(ctx, loop.time(), input_data)
        edit_deadline = _deadlines.editing_deadline(deadline, loop.time())
        visual_runtime = self._visual_runtime
        try:
            result = await _draft_pipeline.run_draft(
                input_data,
                progress_callback,
                ctx=ctx,
                max_source_chars=_MAX_SOURCE_CHARS,
                planning_llm=_model_runtime.require_host_llm,
                author_llm=lambda current_ctx: _model_runtime.require_author_llm(
                    current_ctx,
                    _request_normalization.author_model_role(input_data),
                ),
                publish_version=self._publish_version,
                visual_client=(
                    visual_runtime.client if visual_runtime is not None else None
                ),
                deadline=edit_deadline,
            )
            if not result.get("html_path"):
                return result
            completed = await self._review_and_revise(
                result,
                input_data=input_data,
                progress_callback=progress_callback,
                deadline=edit_deadline,
                allow_content_replan=True,
            )
            return await self._attach_editable_pptx(
                completed,
                progress_callback=progress_callback,
                deadline=deadline,
            )
        except _model_runtime.ModelBoundaryError as exc:
            result = _outcomes.error_result(exc.code, str(exc))
            workspace = _runtime_io.create_workspace(input_data, ctx)
            checkpoint = _draft_checkpoint.load(workspace)
            checkpoint_stage = str((checkpoint or {}).get("stage") or "")
            result.update(
                {
                    "workspace": str(workspace),
                    "checkpoint_stage": checkpoint_stage,
                    "retry_from_checkpoint": bool(checkpoint_stage),
                }
            )
            return result
        except _draft_pipeline.DraftPipelineError as exc:
            return _outcomes.error_result(exc.code, str(exc))
        except _planning.PlanningError as exc:
            return _outcomes.error_result(exc.code, str(exc))
        except _paper_extraction.PaperSourceError as exc:
            return _outcomes.paper_source_failure(exc)
        except (OSError, UnicodeError, ValueError) as exc:
            return _outcomes.error_result(
                "candidate_validation_failed",
                f"Poster authoring failed: {exc}",
            )

    async def _review_and_revise(
        self,
        result: dict[str, Any],
        *,
        input_data: dict[str, Any],
        progress_callback: Any,
        deadline: float,
        allow_content_replan: bool = False,
    ) -> dict[str, Any]:
        """Keep the best rendered candidate until review passes or progress stops."""

        completed = await self._review_candidate(
            result,
            progress_callback=progress_callback,
            deadline=deadline,
        )
        previous_reflow_targets = frozenset[str]()
        retry_feedback = ""
        replan_stalled = False
        layout_failure = ""

        def escalate_layout_failure(incumbent: Mapping[str, Any], reason: str) -> bool:
            nonlocal layout_failure, replan_stalled
            if (
                not allow_content_replan
                or layout_failure
                or _draft_pipeline.render_capacity_feedback(incumbent) is None
            ):
                return False
            layout_failure = reason or "Layout revision did not improve the candidate."
            replan_stalled = False
            return True

        while True:
            if not (
                completed.get("visual_quality_state") == "revision-required"
                and str(completed.get("visual_review_path") or "").strip()
                and str(completed.get("html_uri") or "").strip()
            ):
                break
            now = asyncio.get_running_loop().time()
            if deadline <= now:
                _outcomes.append_warning_once(
                    completed,
                    _deadlines.VISUAL_LOOP_TIMEOUT_WARNING,
                )
                break
            incumbent = completed
            selected_sidecar = _revision_state.revision_sidecar_path(
                Path(str(incumbent.get("html_path") or ""))
            )
            selected_state = (
                selected_sidecar.read_bytes() if selected_sidecar.is_file() else None
            )
            incumbent_review = incumbent.get("visual_review")
            baseline_sha256 = (
                str(incumbent_review.get("screenshot_sha256") or "")
                if isinstance(incumbent_review, Mapping)
                else ""
            )
            revision_input = {
                **input_data,
                "action": _core.ACTION_REVISE,
                "source_html_uri": str(completed["html_uri"]),
                "source_html_sha256": str(completed.get("html_sha256") or ""),
                "visual_review_path": str(completed["visual_review_path"]),
                "_comparison_baseline": {
                    "path": str(completed.get("screenshot_path") or ""),
                    "sha256": baseline_sha256,
                },
                "_comparison_baseline_page": {
                    key: completed.get("page_plan", {}).get(key)
                    for key in ("width_mm", "height_mm")
                },
            }
            reflow_targets = _visual_reflow_targets(completed.get("visual_review"))
            persistent_targets = reflow_targets & previous_reflow_targets
            feedback_parts = [retry_feedback] if retry_feedback else []
            if persistent_targets:
                target_list = ", ".join(sorted(persistent_targets))
                feedback_parts.append(
                    "Rendered review still flags the same targets for reflow after "
                    f"the preceding revision: {target_list}. Compare their measured "
                    "bounds and the prior visual review before choosing a different "
                    "composition or sizing strategy."
                )
            if feedback_parts:
                revision_input["feedback"] = "\n\n".join(feedback_parts)
            previous_reflow_targets = reflow_targets
            # The user selection belongs to the original manual edit. The
            # reviewer supplies targets bound to this new candidate instead.
            revision_input.pop("selection_state", None)
            rendered_feedback = (
                _draft_pipeline.render_capacity_feedback(incumbent)
                if allow_content_replan and not replan_stalled else None
            )
            if rendered_feedback is not None and layout_failure:
                rendered_feedback["layout_attempt_failure"] = layout_failure
            trying_replan = rendered_feedback is not None
            try:
                if trying_replan:
                    ctx = getattr(self, "ctx", None)
                    revised = await _draft_pipeline.run_draft(
                        {
                            **input_data,
                            "visual_iteration": int(incumbent.get("visual_iteration") or 0) + 1,
                            "_comparison_baseline": revision_input["_comparison_baseline"],
                            "_comparison_baseline_page": revision_input[
                                "_comparison_baseline_page"
                            ],
                        },
                        progress_callback,
                        ctx=ctx,
                        max_source_chars=_MAX_SOURCE_CHARS,
                        planning_llm=_model_runtime.require_host_llm,
                        author_llm=lambda current_ctx: _model_runtime.require_author_llm(
                            current_ctx,
                            _request_normalization.author_model_role(input_data),
                        ),
                        publish_version=self._publish_version,
                        visual_client=(
                            self._visual_runtime.client
                            if self._visual_runtime is not None else None
                        ),
                        deadline=deadline,
                        resume_workspace=Path(str(incumbent["workspace"])),
                        resume_html_sha256=str(incumbent["html_sha256"]),
                        rendered_feedback=rendered_feedback,
                    )
                else:
                    revised = await self._revise(
                        revision_input,
                        progress_callback,
                        deadline=deadline,
                    )
            except _model_runtime.ModelBoundaryError as exc:
                _outcomes.append_warning_once(
                    completed,
                    f"Bounded visual revision was unavailable: {exc}",
                )
                if trying_replan:
                    _activate_candidate_files(incumbent, selected_state=selected_state)
                    if layout_failure:
                        break
                    replan_stalled = True
                    continue
                if escalate_layout_failure(incumbent, str(exc)):
                    continue
                break
            if (
                revised.get("status") == "error"
                or _outcomes.revision_model_timed_out(revised)
            ):
                failure_summary = str(revised.get("summary") or "").strip()
                if failure_summary:
                    _outcomes.append_warning_once(completed, failure_summary)
                diagnostic = revised.get("diagnostic_candidate")
                if isinstance(diagnostic, Mapping):
                    completed["diagnostic_candidate"] = dict(diagnostic)
                    completed["retry_from_checkpoint"] = bool(
                        diagnostic.get("revision_state_persisted") is True
                    )
                warnings = revised.get("warnings")
                if isinstance(warnings, list):
                    for warning in warnings:
                        if str(warning).strip():
                            _outcomes.append_warning_once(
                                completed,
                                str(warning),
                            )
                if trying_replan:
                    _activate_candidate_files(incumbent, selected_state=selected_state)
                    if layout_failure:
                        break
                    replan_stalled = True
                    continue
                if escalate_layout_failure(incumbent, failure_summary):
                    continue
                break
            if (
                str(revised.get("html_sha256") or "")
                == str(incumbent.get("html_sha256") or "")
            ):
                for warning in revised.get("warnings") or []:
                    if str(warning).strip():
                        _outcomes.append_warning_once(incumbent, str(warning))
                _outcomes.append_warning_once(
                    incumbent,
                    "Visual revision did not change the selected poster; "
                    "review was not repeated.",
                )
                _activate_candidate_files(incumbent, selected_state=selected_state)
                completed = incumbent
                if trying_replan:
                    if layout_failure:
                        break
                    replan_stalled = True
                    continue
                if escalate_layout_failure(incumbent, "The layout revision changed no HTML."):
                    continue
                break
            revised_screenshot = Path(str(revised.get("screenshot_path") or ""))
            if (
                baseline_sha256
                and revised_screenshot.is_file()
                and hashlib.sha256(revised_screenshot.read_bytes()).hexdigest()
                == baseline_sha256
            ):
                _activate_candidate_files(incumbent, selected_state=selected_state)
                completed = incumbent
                if trying_replan:
                    if layout_failure:
                        break
                    replan_stalled = True
                    continue
                if retry_feedback:
                    _outcomes.append_warning_once(
                        incumbent,
                        "The revised layout still rendered the same pixels; the current "
                        "poster remains selected.",
                    )
                    if escalate_layout_failure(
                        incumbent, "Layout revisions rendered the same pixels."
                    ):
                        continue
                    break
                retry_feedback = (
                    "The proposal rendered exactly the same pixels as the current "
                    "poster. Change the CSS rule that actually sizes the painted "
                    "figures or choose a different wrapper composition."
                )
                continue
            proposal = await self._review_candidate(
                revised,
                progress_callback=progress_callback,
                deadline=deadline,
            )
            proposal_review = proposal.get("visual_review")
            comparison = (
                str(proposal_review.get("comparison") or "")
                if isinstance(proposal_review, Mapping)
                else ""
            )
            if (
                proposal.get("visual_quality_state") == "passed"
                and comparison != "regressed"
            ):
                completed = proposal
                break
            proposal_inspection = proposal.get("inspection")
            incumbent_inspection = incumbent.get("inspection")
            if (
                comparison not in {"improved", "regressed"}
                and proposal.get("visual_quality_state") == "awaiting-review"
                and isinstance(proposal_inspection, Mapping)
                and proposal_inspection.get("status") == "ok"
                and isinstance(incumbent_inspection, Mapping)
                and incumbent_inspection.get("status") != "ok"
            ):
                completed = proposal
                break
            if comparison != "improved":
                for warning in proposal.get("warnings") or []:
                    if str(warning).strip():
                        _outcomes.append_warning_once(incumbent, str(warning))
                _outcomes.append_warning_once(
                    incumbent,
                    (
                        "Pairwise review did not establish an overall improvement, so "
                        "the current best poster remains selected."
                    ),
                )
                _activate_candidate_files(incumbent, selected_state=selected_state)
                completed = incumbent
                if trying_replan:
                    if layout_failure:
                        break
                    replan_stalled = True
                    continue
                if escalate_layout_failure(
                    incumbent,
                    str(proposal_review.get("summary") or "")
                    if isinstance(proposal_review, Mapping)
                    else "Layout review did not improve the candidate.",
                ):
                    continue
                if comparison not in {"unchanged", "regressed"}:
                    break
                review_summary = (
                    str(proposal_review.get("summary") or "").strip()
                    if isinstance(proposal_review, Mapping)
                    else ""
                )
                retry_feedback = (
                    f"Pairwise review judged the previous proposal {comparison}. "
                    "Start again from the retained incumbent and choose a materially "
                    "different composition or governing layout strategy. Preserve its "
                    "existing strengths."
                    + (f" Previous proposal review: {review_summary}" if review_summary else "")
                )
                continue
            completed = proposal
            replan_stalled = False
            layout_failure = ""
            retry_feedback = ""
        return completed

    async def _attach_editable_pptx(
        self,
        result: dict[str, Any],
        *,
        progress_callback: Any,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Export the exact rendered candidate and report an incomplete package."""

        if result.get("status") == "error" or result.get("pptx_path"):
            return result
        html_path = Path(str(result.get("html_path") or ""))
        expected_sha256 = str(result.get("html_sha256") or "")
        if not html_path.is_file() or not expected_sha256:
            return result
        workspace = Path(str(result.get("workspace") or html_path.parent))
        html_bytes = html_path.read_bytes()
        if hashlib.sha256(html_bytes).hexdigest() != expected_sha256:
            return _pptx_export_failure(
                result,
                code="approval_source_mismatch",
                summary="HTML changed before editable PPTX export.",
            )
        export_dir = workspace / "editable-poster" / expected_sha256
        frozen_html = export_dir / "source.html"
        _runtime_io.replace_file_atomic(frozen_html, html_bytes)
        await _runtime_io.progress(progress_callback, "poster.export-pptx", 0.96)
        export_payload: dict[str, Any] = {
            "action": _core.ACTION_EXPORT_PPTX,
            "html": str(frozen_html),
            "output_dir": str(export_dir),
        }
        remaining: float | None = None
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 1.0:
                return _pptx_export_failure(
                    result,
                    code="pptx_export_failed",
                    summary=(
                        "Editable PPTX export was skipped because the workflow runtime "
                        "budget was exhausted; the rendered HTML remains available."
                    ),
                )
            export_payload["runtime_timeout_seconds"] = min(remaining, 180.0)
        export_call = asyncio.to_thread(
            _portable_actions.run,
            export_payload,
        )
        try:
            if remaining is None:
                export = await export_call
            else:
                async with asyncio.timeout(remaining):
                    export = await export_call
        except TimeoutError:
            return _pptx_export_failure(
                result,
                code="pptx_export_failed",
                summary=(
                    "Editable PPTX export reached the workflow runtime budget; the "
                    "rendered HTML remains available."
                ),
            )
        if (
            export.get("status") == "error"
            or export.get("source_html_sha256") != expected_sha256
        ):
            return _pptx_export_failure(
                result,
                code="pptx_export_failed",
                outcome=export.get("outcome"),
                summary=str(export.get("summary") or "Editable PPTX export failed."),
            )
        for field in (
            "pptx_path",
            "scene_path",
            "rubric_path",
            "rubric",
            "openxml",
            "editable_object_count",
        ):
            if field in export:
                result[field] = export[field]
        artifact_specs = (
            (
                "pptx_uri",
                Path(str(export["pptx_path"])),
                "poster-pptx",
                "Editable scientific poster",
                "pptx",
                (
                    "application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation"
                ),
            ),
            (
                "scene_uri",
                Path(str(export["scene_path"])),
                "poster-pptx-scene",
                "Editable scientific poster scene",
                "json",
                "application/json",
            ),
            (
                "rubric_uri",
                Path(str(export["rubric_path"])),
                "poster-pptx-rubric",
                "Editable scientific poster rubric",
                "json",
                "application/json",
            ),
        )
        warnings = list(result.get("warnings") or [])
        for uri_field, path, kind, title, fmt, mime in artifact_specs:
            artifact = await _store_delivery_artifact(
                getattr(self, "ctx", None),
                path,
                kind=kind,
                title=title,
                fmt=fmt,
                mime=mime,
                warnings=warnings,
            )
            result[uri_field] = str(artifact["uri"])
            _append_artifact_once(result, artifact)
        result["warnings"] = warnings
        result["pptx_export"] = {
            "status": str(export.get("status") or "ok"),
            "outcome": export.get("outcome") or {"code": "pptx_export_complete"},
            "summary": str(
                export.get("summary")
                or "Editable PPTX and its scene and rubric were published."
            ),
        }
        return result

    async def _review_candidate(
        self,
        initial: dict[str, Any],
        *,
        progress_callback: Any,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Run one exact-byte visual review without hidden authoring retries."""

        completed = await _review_pipeline.run_review(
            initial,
            progress_callback=progress_callback,
            ctx=getattr(self, "ctx", None),
            deadline=deadline,
            environ=self._visual_environ,
            runtime=self._visual_runtime,
            reviewer=self._visual_reviewer,
        )
        return await self._attach_visual_review_receipt(completed)

    async def _attach_visual_review_receipt(
        self,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish the receipt already validated by the visual-review owner."""

        receipt_path = str(result.get("visual_review_path") or "").strip()
        if not receipt_path:
            return result
        receipt = result.get("visual_review")
        if not isinstance(receipt, dict) or receipt.get(
            "candidate_html_sha256"
        ) != result.get("html_sha256"):
            result = _downgrade_invalid_visual_review(
                result,
                summary=(
                    "The validated visual review receipt is not bound to this poster."
                ),
            )
            _outcomes.append_warning_once(
                result,
                _INVALID_VISUAL_REVIEW_WARNING,
            )
            return result
        warnings = list(result.get("warnings") or [])
        artifact = await _store_delivery_artifact(
            getattr(self, "ctx", None),
            Path(receipt_path),
            kind="poster-review-receipt",
            title="Poster visual review receipt",
            fmt="json",
            mime="application/json",
            warnings=warnings,
        )
        result["warnings"] = warnings
        result["visual_review"] = receipt
        result["visual_review_uri"] = str(artifact["uri"])
        result["visual_review_sha256"] = str(artifact["sha256"])
        _append_artifact_once(result, artifact)
        return result

    async def _revise(
        self,
        input_data: dict[str, Any],
        progress_callback: Any,
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Delegate grounded HTML revision to the portable revision pipeline."""

        return await _revision_pipeline.run_revision(
            input_data,
            progress_callback,
            ctx=getattr(self, "ctx", None),
            publish_version=self._publish_version,
            max_source_chars=_MAX_SOURCE_CHARS,
            deadline=deadline,
        )

    async def _publish_version(
        self,
        *,
        html_text: str,
        source_text: str,
        input_data: dict[str, Any],
        progress_callback: Any,
        workspace: Path,
        parent_html_sha256: str | None,
        asset_warnings: list[str],
        inspection: dict[str, Any] | None,
        source_figure_sha256s: set[str],
        page_plan: dict[str, Any],
        design_reference: _reference_catalog.ReferenceBundle,
        reference_brief: _reference_analysis.ReferenceAnalysis,
        visual_preferences: dict[str, str],
        checkpoint_state: dict[str, Any] | None = None,
        deadline: float | None = None,
        inspection_session: _browser_inspection.ChromiumInspectionSession | None = None,
    ) -> dict[str, Any]:
        """Persist exact HTML, inspect it, and expose live-preview metadata."""

        ctx = getattr(self, "ctx", None)
        html_bytes = html_text.encode("utf-8")
        html_sha256 = hashlib.sha256(html_bytes).hexdigest()
        candidate_dir = workspace / ".candidates" / html_sha256
        candidate_path = candidate_dir / "poster.html"
        _runtime_io.replace_file_atomic(candidate_path, html_bytes)
        if inspection is None:
            await _runtime_io.progress(progress_callback, "poster.inspect", 0.68)
            inspection_call = _runtime_io.inspect_preview(
                candidate_path,
                candidate_dir / "inspection",
                scale=_runtime_io.bounded_float(
                    input_data.get("scale"), default=2.0, low=0.5, high=4.0
                ),
                session=inspection_session,
            )
            if deadline is None:
                inspection = await inspection_call
            else:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    inspection_call.close()
                    return _outcomes.inspection_deadline_result()
                try:
                    async with asyncio.timeout(remaining):
                        inspection = await inspection_call
                except TimeoutError:
                    return _outcomes.inspection_deadline_result()
        review_request: dict[str, Any] | None = None
        review_request_path: Path | None = None
        screenshot_path = Path(str(inspection.get("screenshot_path") or ""))
        inspection_passed = inspection.get("status") == "ok"
        visual_evidence = inspection.get("visual_evidence")
        review_inputs_ready = (
            screenshot_path.is_file()
            and isinstance(visual_evidence, Mapping)
        )
        if review_inputs_ready:
            try:
                visual_iteration = _revision_state.visual_iteration(
                    input_data.get("visual_iteration")
                )
                review_request, review_request_path = _review_pipeline.prepare_candidate_review(
                    candidate_path, inspection, page_plan=page_plan,
                    iteration=visual_iteration,
                    quality_reference=(
                        design_reference.to_dict() if design_reference.source_kind == "custom" else None
                    ),
                    comparison_baseline=(
                        input_data.get("_comparison_baseline")
                        if isinstance(input_data.get("_comparison_baseline"), Mapping)
                        else None
                    ),
                    comparison_baseline_page=(
                        input_data.get("_comparison_baseline_page")
                        if isinstance(input_data.get("_comparison_baseline_page"), Mapping)
                        else None
                    ),
                )
            except (OSError, _review_contract.ReviewContractError) as exc:
                review_request = None
                review_request_path = None
                asset_warnings.append(f"Cannot prepare visual review: {exc}")
        review_preparation_failed = review_inputs_ready and review_request is None
        if checkpoint_state is not None:
            _draft_checkpoint.save(
                workspace,
                stage="author-ready",
                state=checkpoint_state,
            )
        html_path = workspace / "poster.html"
        _runtime_io.replace_file_atomic(html_path, html_bytes)
        html_artifact = await _store_delivery_artifact(
            ctx,
            html_path,
            kind="poster",
            title="Scientific poster HTML",
            fmt="html",
            mime="text/html",
            warnings=asset_warnings,
        )
        published_paths = dict.fromkeys(
            (html_path, Path(str(html_artifact["path"])))
        )
        sidecar_results = (
            [
                _revision_state.persist_revision_sidecar(
                    artifact_path=published_path,
                    html_text=html_text,
                    checkpoint_state=checkpoint_state,
                )
                for published_path in published_paths
            ]
            if checkpoint_state is not None
            else []
        )
        revision_sidecar_persisted = bool(sidecar_results) and all(sidecar_results)
        inspection_path = _runtime_io.inspection_report_path(
            inspection, candidate_dir / "inspection"
        )
        inspection_artifact = await _store_delivery_artifact(
            ctx,
            inspection_path,
            kind="poster-report",
            title="Poster rendered inspection",
            fmt="json",
            mime="application/json",
            warnings=asset_warnings,
        )
        review_artifacts: list[dict[str, Any]] = []
        screenshot_artifact: dict[str, Any] | None = None
        evidence_path = Path(str(inspection.get("visual_evidence_path") or ""))
        if evidence_path.is_file():
            try:
                evidence_artifact = await _runtime_io.store_artifact(
                    ctx,
                    evidence_path,
                    kind="poster-visual-evidence",
                    title="Poster visual evidence bundle",
                    fmt="json",
                    mime="application/json",
                )
                review_artifacts.append(evidence_artifact)
            except OSError as exc:
                asset_warnings.append(f"Cannot store visual evidence bundle: {exc}")
        atlas_value = (
            visual_evidence.get("atlas")
            if isinstance(visual_evidence, Mapping)
            else None
        )
        atlas_path = Path(
            str(atlas_value.get("path") or "")
            if isinstance(atlas_value, Mapping)
            else ""
        )
        if atlas_path.is_file():
            try:
                atlas_artifact = await _runtime_io.store_artifact(
                    ctx,
                    atlas_path,
                    kind="poster-preview",
                    title="Poster visual evidence atlas",
                    fmt="png",
                    mime="image/png",
                )
                review_artifacts.append(atlas_artifact)
            except OSError as exc:
                asset_warnings.append(f"Cannot store visual evidence atlas: {exc}")
        if screenshot_path.is_file():
            try:
                screenshot_artifact = await _runtime_io.store_artifact(
                    ctx,
                    screenshot_path,
                    kind="poster-preview",
                    title="Scientific poster rendered candidate",
                    fmt="png",
                    mime="image/png",
                )
                review_artifacts.append(screenshot_artifact)
            except OSError as exc:
                asset_warnings.append(f"Cannot store rendered screenshot: {exc}")
        if review_request_path is not None:
            try:
                request_artifact = await _runtime_io.store_artifact(
                    ctx,
                    review_request_path,
                    kind="poster-review-request",
                    title="Poster visual review request",
                    fmt="json",
                    mime="application/json",
                )
                review_artifacts.append(request_artifact)
            except OSError as exc:
                asset_warnings.append(f"Cannot store visual-review request: {exc}")
        active_live_path = workspace / "live" / "poster.html"
        try:
            _runtime_io.replace_file_atomic(active_live_path, html_path.read_bytes())
        except OSError as exc:
            asset_warnings.append(
                f"Live preview activation failed; use the published poster HTML instead: {exc}"
            )
            active_live_path = html_path

        preview_argv = _core.build_preview_argv(
            active_live_path,
            skill_dir=SKILL_DIR,
            python_executable=sys.executable,
        )
        inspection_outcome_code = str(
            (inspection.get("outcome") or {}).get("code") or "inspection_unavailable"
        )
        inspection_summary = str(inspection.get("summary") or "").strip()
        outcome_code = (
            "visual_review_unavailable"
            if inspection_passed
            and (review_request is not None or review_preparation_failed)
            else "visual_revision_required"
            if not inspection_passed
            else inspection_outcome_code
        )
        if inspection_passed and review_request is not None:
            publication_summary = (
                "Scientific-poster HTML and screenshot are ready for "
                "image-capable review."
            )
        elif review_preparation_failed:
            publication_summary = (
                "HTML and screenshot are ready, but the visual review request "
                "could not be prepared."
            )
        elif not inspection_passed:
            publication_summary = (
                "An editable draft is available and still needs revision. "
                + (inspection_summary or "Rendered inspection has not passed.")
            )
        else:
            publication_summary = (
                "HTML is ready, but no reviewable screenshot is available."
            )
        warnings = [
            *asset_warnings,
            *[
                str(item.get("message") or item)
                for item in inspection.get("warnings", [])
                if isinstance(item, dict)
            ],
        ]
        if revision_sidecar_persisted is not True:
            warnings.append(
                "Published HTML could not carry its grounded revision state; "
                "automatic revision of this version is unavailable."
            )
        grounding_source_sha256 = hashlib.sha256(
            source_text.encode("utf-8")
        ).hexdigest()
        source_figure_manifest_sha256 = _core.source_figure_manifest_sha256(
            source_figure_sha256s
        )
        operator_confirmation = _core.poster_approval_phrase(
            html_sha256,
            grounding_source_sha256,
            source_figure_manifest_sha256,
        )
        result = _core.outcome_result(
            outcome_code,
            summary=publication_summary,
            requires_approval=False,
            workspace=str(workspace),
            html_path=str(html_path),
            html_uri=str(html_artifact["uri"]),
            html_sha256=html_sha256,
            grounding_source_sha256=grounding_source_sha256,
            source_figure_manifest_sha256=source_figure_manifest_sha256,
            live_html_path=str(active_live_path),
            preview_uri=active_live_path.resolve().as_uri(),
            screenshot_path=str(screenshot_path) if screenshot_path.is_file() else None,
            screenshot_uri=str(screenshot_artifact["uri"]) if screenshot_artifact else None,
            preview_argv=preview_argv,
            selection_state_path=str(active_live_path.parent / "selection-state.json"),
            inspection=inspection,
            page_plan=page_plan,
            density_profile=page_plan.get("density_profile"),
            focal_role=page_plan.get("focal_role"),
            design_reference=design_reference.to_dict(),
            visual_design=(reference_brief.to_dict()),
            visual_preferences=dict(visual_preferences),
            reference_source_kind=design_reference.source_kind,
            visual_review_mode=(
                "vlm"
                if review_preparation_failed or review_request is not None
                else "not-run"
                if not inspection_passed
                else "deterministic-only"
            ),
            parent_html_sha256=parent_html_sha256,
            visual_quality_state=(
                "revision-required" if not inspection_passed else
                "awaiting-review"
                if review_request is not None
                else "not-reviewable"
            ),
            visual_iteration=(
                review_request.get("iteration") if review_request is not None else None
            ),
            visual_review_request_path=(
                str(review_request_path) if review_request_path is not None else ""
            ),
            visual_evidence_path=(
                str(evidence_path) if evidence_path.is_file() else ""
            ),
            visual_evidence_sha256=(
                str(visual_evidence.get("bundle_sha256") or "")
                if isinstance(visual_evidence, Mapping)
                else ""
            ),
            warnings=warnings,
            artifacts=[html_artifact, inspection_artifact, *review_artifacts],
            approval={
                "source_html_path": str(html_path),
                "source_html_uri": str(html_artifact["uri"]),
                "source_html_sha256": html_sha256,
                "grounding_source_sha256": grounding_source_sha256,
                "source_figure_manifest_sha256": source_figure_manifest_sha256,
                "source_figure_sha256s": sorted(source_figure_sha256s),
                "operator_confirmation": operator_confirmation,
            },
        )
        result = {key: value for key, value in result.items() if value is not None}
        if review_request is not None:
            result["visual_review_request"] = review_request
        await _runtime_io.progress(progress_callback, "poster.ready", 1.0)
        return result
