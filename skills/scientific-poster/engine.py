"""Portable host orchestration for direct HTML scientific-poster authoring."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

import poster_core as _core  # noqa: E402 - copied Skill bootstraps its own root
from posterlib.content import html_contract as _html_contract  # noqa: E402
from posterlib.content import planning as _planning  # noqa: E402
from posterlib.delivery import portable_actions as _portable_actions  # noqa: E402
from posterlib.generation import model_runtime as _model_runtime  # noqa: E402
from posterlib.runtime import runtime_io as _runtime_io  # noqa: E402
from posterlib.sources import paper_source as _paper_source  # noqa: E402
from posterlib.sources import source_runtime as _source_runtime  # noqa: E402
from posterlib.visual import candidate_control as _candidate_control  # noqa: E402
from posterlib.visual import inspection_policy as _inspection_policy  # noqa: E402
from posterlib.visual import reference_seeds as _reference_seeds  # noqa: E402
from posterlib.visual import visual_design as _visual_design  # noqa: E402
from posterlib.visual import visual_review as _visual_review  # noqa: E402
from posterlib.workflows import draft_checkpoint as _draft_checkpoint  # noqa: E402
from posterlib.workflows import draft_pipeline as _draft_pipeline  # noqa: E402
from posterlib.workflows import (  # noqa: E402
    request_normalization as _request_normalization,
)
from posterlib.workflows import revision_pipeline as _revision_pipeline  # noqa: E402
from posterlib.workflows import revision_state as _revision_state  # noqa: E402
from posterlib.workflows import runtime_budget as _runtime_budget  # noqa: E402
from posterlib.workflows import visual_loop as _visual_loop  # noqa: E402
from posterlib.workflows import workflow_outcomes as _workflow_outcomes  # noqa: E402

_MAX_SOURCE_CHARS = 1_500_000
_NOOP_REVISION_WARNING = _visual_loop.NOOP_REVISION_WARNING
_PPTX_EXPORT_WARNING = (
    "Editable PPTX export did not complete; the exact HTML candidate remains available."
)
_VISUAL_REVIEW_RECEIPT_WARNING = (
    "Visual review receipt could not be published; the exact HTML candidate remains "
    "available."
)
_PUBLIC_REVISION_REGRESSION_WARNING = (
    "The receipt-bound revision remained physically dominated by the exact source "
    "after one bounded retry; the source poster was preserved."
)
_PUBLIC_REVISION_COMPARISON_WARNING = (
    "The exact source poster could not be rendered for the receipt-bound pre-commit "
    "comparison; the source poster was preserved."
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
_DEFER_REVISION_COMMIT = object()


@dataclass(frozen=True)
class _PreparedVersion:
    """A statically valid, rendered candidate that has not replaced the active poster."""

    html_sha256: str
    candidate_path: str
    inspection: dict[str, Any]
    review_request: dict[str, Any] | None
    review_request_path: str | None
    checkpoint_state: dict[str, Any] | None
    publication: dict[str, Any]


def _prepared_inspection(candidate: Any) -> dict[str, Any] | None:
    """Expose a deferred candidate inspection without coupling posterlib to engine."""

    return candidate.inspection if isinstance(candidate, _PreparedVersion) else None


def _prepared_review(candidate: Any) -> tuple[dict[str, Any], str] | None:
    """Expose the exact pre-commit review request for one rendered candidate."""

    if (
        not isinstance(candidate, _PreparedVersion)
        or candidate.review_request is None
        or not candidate.review_request_path
    ):
        return None
    return dict(candidate.review_request), candidate.review_request_path


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


def _downgrade_unpublished_visual_review(
    result: dict[str, Any],
    *,
    summary: str,
) -> dict[str, Any]:
    """Prevent an unavailable receipt from authorizing delivery."""

    for field in (
        "visual_review",
        "visual_review_path",
        "visual_review_uri",
        "visual_review_sha256",
    ):
        result.pop(field, None)
    result["requires_approval"] = False
    result["visual_review_mode"] = "vlm"
    result["visual_quality_state"] = "awaiting-review"
    return _workflow_outcomes.visual_outcome(
        result,
        "visual_review_unavailable",
        summary,
    )


class ScientificPosterEngine:
    """Ask the host model for complete inert HTML and manage review versions."""

    def __init__(self) -> None:
        self._versions: dict[str, dict[str, Any]] = {}
        self._visual_runtime: _visual_loop.VisualLoopRuntime | None = None
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
        if action == _core.ACTION_DRAFT:
            return await self._draft(input_data, progress_callback)
        if action == _core.ACTION_ESTIMATE:
            return await self._estimate(input_data, progress_callback)
        if action == _core.ACTION_REVISE:
            loop = asyncio.get_running_loop()
            deadline = _runtime_budget.workflow_deadline(
                getattr(self, "ctx", None), loop.time()
            )
            result, continue_visual_loop = await self._run_public_revision(
                input_data,
                progress_callback,
                deadline=deadline,
            )
            if _workflow_outcomes.revision_model_timed_out(result):
                preserved = {
                    "html_uri": str(input_data.get("source_html_uri") or ""),
                    "html_sha256": str(input_data.get("source_html_sha256") or ""),
                    "warnings": [],
                }
                return _workflow_outcomes.revision_timeout_error(preserved, result)
            if continue_visual_loop:
                result = await self._complete_visual_loop(
                    result,
                    input_data=input_data,
                    progress_callback=progress_callback,
                    deadline=deadline,
                )
            return await self._attach_editable_pptx(
                result,
                progress_callback=progress_callback,
            )

        portable_input = {**input_data, "action": action}
        if action in {
            _core.ACTION_APPROVE,
            _core.ACTION_INSPECT,
            _core.ACTION_EXPORT_PPTX,
        }:
            source_uri = str(portable_input.get("source_html_uri") or "").strip()
            source_sha256 = str(portable_input.get("source_html_sha256") or "").strip()
            state = self._versions.get(source_uri) or self._versions.get(source_sha256)
            if state is not None:
                try:
                    supplied_source = (
                        await _source_runtime.resolve_explicit_source_text(
                            portable_input,
                            ctx=getattr(self, "ctx", None),
                        )
                    )
                except _paper_source.PaperSourceError as exc:
                    return _workflow_outcomes.paper_source_failure(exc)
                stored_source = str(state.get("source_text") or "")
                if supplied_source and supplied_source != stored_source:
                    return _workflow_outcomes.error_result(
                        "approval_source_mismatch",
                        "Approval grounding source differs from the authored poster version.",
                    )
                portable_input["source_text"] = str(state.get("source_text") or "")
                portable_input["source_figure_sha256s"] = list(
                    state.get("source_figure_sha256s") or ()
                )
                if action == _core.ACTION_INSPECT:
                    portable_input.setdefault(
                        "html", str(state.get("artifact_path") or "")
                    )
                    portable_input.pop("source_html_uri", None)
                    portable_input.pop("source_html_sha256", None)
                elif action == _core.ACTION_EXPORT_PPTX:
                    portable_input.setdefault(
                        "html", str(state.get("artifact_path") or "")
                    )
                    portable_input.pop("source_html_uri", None)
                    portable_input.pop("source_html_sha256", None)
                    portable_input.pop("source_figure_sha256s", None)
                    portable_input.pop("paper_path", None)
                else:
                    portable_input.pop("paper_path", None)
                    portable_input.pop("source", None)
            elif source_uri:
                source_path = await _runtime_io.resolve_path(
                    getattr(self, "ctx", None),
                    source_uri,
                    base_dir=portable_input.get("cwd"),
                )
                if source_path is None:
                    return _workflow_outcomes.error_result(
                        "source_not_found",
                        "The requested poster HTML artifact could not be resolved.",
                    )
                expected_sha256 = str(
                    portable_input.get("source_html_sha256") or ""
                ).strip()
                if expected_sha256:
                    actual_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
                    if actual_sha256 != expected_sha256:
                        return _workflow_outcomes.error_result(
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
        deadline = _runtime_budget.workflow_deadline(
            ctx, asyncio.get_running_loop().time()
        )
        workspace = _runtime_io.create_workspace(input_data, ctx)
        try:
            source = await _source_runtime.prepare_draft_source(
                input_data,
                ctx=ctx,
                workspace=workspace,
                progress_callback=progress_callback,
            )
            if len(source.text) > _MAX_SOURCE_CHARS:
                return _workflow_outcomes.error_result(
                    "source_too_large",
                    f"Research input exceeds the {_MAX_SOURCE_CHARS}-character safety limit.",
                )
            assets, asset_warnings = await _source_runtime.prepare_assets(
                [
                    *source.assets,
                    *[
                        _source_runtime.normalize_user_asset(item)
                        for item in _source_runtime.normalize_asset_inputs(
                            input_data.get("assets")
                        )
                    ],
                ],
                ctx,
            )
            expected_figures = _html_contract.source_figure_sha256s(assets)
            await _runtime_io.progress(progress_callback, "poster.plan-content", 0.12)
            budget = await _model_runtime.request_evidence_budget(
                _runtime_budget.host_llm(ctx),
                source_text=source.text,
                assets=assets,
                source_figure_sha256s=expected_figures,
                authoring_request=source.authoring_request,
                page=input_data.get("page"),
                orientation=str(input_data.get("orientation") or "auto"),
                deadline=deadline,
            )
            page_plan = _planning.estimate_page(
                budget,
                page=input_data.get("page"),
                orientation=str(input_data.get("orientation") or "auto"),
            )
        except _model_runtime.ModelBoundaryError as exc:
            return _workflow_outcomes.error_result(exc.code, str(exc))
        except _planning.PlanningError as exc:
            return _workflow_outcomes.error_result(exc.code, str(exc))
        except _paper_source.PaperSourceError as exc:
            return _workflow_outcomes.paper_source_failure(exc)
        except (OSError, UnicodeError, ValueError) as exc:
            return _workflow_outcomes.error_result(
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
        deadline = _runtime_budget.workflow_deadline(ctx, loop.time())
        visual_runtime = self._visual_runtime or _visual_loop.runtime_from_context(
            ctx,
            self._visual_environ,
        )
        if self._visual_runtime is None and visual_runtime is not None:
            self._visual_runtime = visual_runtime
        try:
            result = await _draft_pipeline.run_draft(
                input_data,
                progress_callback,
                ctx=ctx,
                max_source_chars=_MAX_SOURCE_CHARS,
                transport_options=_request_normalization.authoring_transport_options,
                host_llm=_runtime_budget.host_llm,
                publish_version=self._publish_version,
                visual_design_client=(
                    visual_runtime.client if visual_runtime is not None else None
                ),
                deadline=deadline,
            )
            completed = await self._complete_visual_loop(
                result,
                input_data=input_data,
                progress_callback=progress_callback,
                deadline=deadline,
            )
            return await self._attach_editable_pptx(
                completed,
                progress_callback=progress_callback,
            )
        except _model_runtime.ModelBoundaryError as exc:
            result = _workflow_outcomes.error_result(exc.code, str(exc))
            if exc.code == "llm_error":
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
            return _workflow_outcomes.error_result(exc.code, str(exc))
        except _planning.PlanningError as exc:
            return _workflow_outcomes.error_result(exc.code, str(exc))
        except _paper_source.PaperSourceError as exc:
            return _workflow_outcomes.paper_source_failure(exc)
        except (OSError, UnicodeError, ValueError) as exc:
            return _workflow_outcomes.error_result(
                "candidate_validation_failed",
                f"Poster authoring failed: {exc}",
            )

    async def _attach_editable_pptx(
        self,
        result: dict[str, Any],
        *,
        progress_callback: Any,
    ) -> dict[str, Any]:
        """Export the exact rendered candidate without changing its workflow status."""

        if result.get("status") == "error" or result.get("pptx_path"):
            return result
        if (
            result.get("visual_review_mode") == "vlm"
            and result.get("visual_quality_state") != "passed"
        ):
            result["pptx_export"] = {
                "status": "blocked",
                "outcome": {"code": "visual_review_required"},
                "summary": (
                    "Editable PPTX export is blocked because the configured visual "
                    "reviewer has not accepted the exact rendered candidate."
                ),
            }
            return result
        inspection = result.get("inspection")
        if not isinstance(inspection, Mapping) or inspection.get("status") != "ok":
            result["pptx_export"] = {
                "status": "blocked",
                "outcome": {"code": "inspection_blocked"},
                "summary": (
                    "Editable PPTX export is blocked because deterministic inspection "
                    "is missing or did not pass."
                ),
            }
            return result
        html_path = Path(str(result.get("html_path") or ""))
        expected_sha256 = str(result.get("html_sha256") or "")
        if not html_path.is_file() or not expected_sha256:
            return result
        workspace = Path(str(result.get("workspace") or html_path.parent))
        html_bytes = html_path.read_bytes()
        if hashlib.sha256(html_bytes).hexdigest() != expected_sha256:
            _workflow_outcomes.append_warning_once(result, _PPTX_EXPORT_WARNING)
            result["pptx_export"] = {
                "status": "error",
                "outcome": {"code": "approval_source_mismatch"},
                "summary": "HTML changed before editable PPTX export.",
            }
            return result
        export_dir = workspace / "editable-poster" / expected_sha256
        frozen_html = export_dir / "source.html"
        _runtime_io.replace_file_atomic(frozen_html, html_bytes)
        await _runtime_io.progress(progress_callback, "poster.export-pptx", 0.96)
        export = await asyncio.to_thread(
            _portable_actions.run,
            {
                "action": _core.ACTION_EXPORT_PPTX,
                "html": str(frozen_html),
                "output_dir": str(export_dir),
            },
        )
        if (
            export.get("status") == "error"
            or export.get("source_html_sha256") != expected_sha256
        ):
            _workflow_outcomes.append_warning_once(result, _PPTX_EXPORT_WARNING)
            result["pptx_export"] = {
                "status": str(export.get("status") or "error"),
                "outcome": export.get("outcome"),
                "summary": str(export.get("summary") or ""),
            }
            return result
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
        for uri_field, path, kind, title, fmt, mime in artifact_specs:
            try:
                artifact = await _runtime_io.store_artifact(
                    getattr(self, "ctx", None),
                    path,
                    kind=kind,
                    title=title,
                    fmt=fmt,
                    mime=mime,
                )
            except OSError as exc:
                _workflow_outcomes.append_warning_once(
                    result,
                    f"{_PPTX_EXPORT_WARNING} Artifact delivery failed: {exc}",
                )
                result["pptx_export"] = {
                    "status": "error",
                    "outcome": {"code": "pptx_export_failed"},
                    "summary": (
                        f"{title} was generated locally, but artifact delivery failed: "
                        f"{exc}"
                    ),
                }
                return result
            result[uri_field] = str(artifact["uri"])
            _append_artifact_once(result, artifact)
        result["pptx_export"] = {
            "status": str(export.get("status") or "ok"),
            "outcome": export.get("outcome") or {"code": "pptx_export_complete"},
            "summary": str(
                export.get("summary")
                or "Editable PPTX and its scene and rubric were published."
            ),
        }
        return result

    async def _complete_visual_loop(
        self,
        initial: dict[str, Any],
        *,
        input_data: dict[str, Any],
        progress_callback: Any,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Bind engine-owned revision operations to the portable visual loop."""

        callbacks = _visual_loop.VisualLoopCallbacks(
            revise=self._revise,
            commit=self._commit_prepared,
            prepared_inspection=_prepared_inspection,
            prepared_review=_prepared_review,
            defer_revision_commit=_DEFER_REVISION_COMMIT,
        )
        completed = await _visual_loop.complete(
            initial,
            input_data=input_data,
            progress_callback=progress_callback,
            callbacks=callbacks,
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
        """Validate and publish the receipt bound to the returned poster candidate."""

        receipt_path = str(result.get("visual_review_path") or "").strip()
        if not receipt_path:
            return result
        design_reference = result.get("design_reference")
        reference_sha256 = (
            str(design_reference.get("image_sha256") or "")
            if isinstance(design_reference, Mapping)
            else ""
        )
        try:
            receipt = _visual_review.load_receipt(
                receipt_path,
                expected_html_sha256=str(result.get("html_sha256") or ""),
                expected_reference_image_sha256=reference_sha256,
            )
        except _visual_review.VisualReviewError as exc:
            result = _downgrade_unpublished_visual_review(
                result,
                summary=(
                    "The visual review receipt could not be validated for this poster."
                ),
            )
            _workflow_outcomes.append_warning_once(
                result,
                f"{_VISUAL_REVIEW_RECEIPT_WARNING} {exc}",
            )
            return result
        try:
            artifact = await _runtime_io.store_artifact(
                getattr(self, "ctx", None),
                Path(receipt_path),
                kind="poster-review-receipt",
                title="Poster visual review receipt",
                fmt="json",
                mime="application/json",
            )
        except OSError as exc:
            result = _downgrade_unpublished_visual_review(
                result,
                summary=(
                    "The validated visual review receipt could not be published for "
                    "this poster."
                ),
            )
            _workflow_outcomes.append_warning_once(
                result,
                f"{_VISUAL_REVIEW_RECEIPT_WARNING} Artifact delivery failed: {exc}",
            )
            return result
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
    ) -> dict[str, Any] | _PreparedVersion:
        """Delegate grounded HTML revision to the portable revision pipeline."""

        return await _revision_pipeline.run_revision(
            input_data,
            progress_callback,
            ctx=getattr(self, "ctx", None),
            version_lookup=self._versions.get,
            publish_version=self._publish_version,
            defer_revision_commit=_DEFER_REVISION_COMMIT,
            max_source_chars=_MAX_SOURCE_CHARS,
            noop_revision_warning=_NOOP_REVISION_WARNING,
            deadline=deadline,
        )

    async def _run_public_revision(
        self,
        input_data: dict[str, Any],
        progress_callback: Any,
        *,
        deadline: float,
    ) -> tuple[dict[str, Any], bool]:
        """Keep a receipt-bound revision deferred until delivery evidence is safe."""

        receipt_bound = bool(str(input_data.get("visual_review_path") or "").strip())
        revision_input = dict(input_data)
        if receipt_bound:
            revision_input["_defer_revision_commit"] = _DEFER_REVISION_COMMIT
        candidate = await self._revise(
            revision_input,
            progress_callback,
            deadline=deadline,
        )
        if not isinstance(candidate, _PreparedVersion):
            return candidate, True
        if not receipt_bound:
            raise RuntimeError("public poster revision remained deferred")
        return await self._guard_receipt_bound_revision(
            input_data,
            candidate,
            progress_callback=progress_callback,
            deadline=deadline,
        )

    async def _guard_receipt_bound_revision(
        self,
        input_data: dict[str, Any],
        candidate: _PreparedVersion,
        *,
        progress_callback: Any,
        deadline: float,
    ) -> tuple[dict[str, Any], bool]:
        """Retry one physically regressed candidate before any activation."""

        source_render = await self._inspect_revision_source(
            input_data,
            candidate,
            deadline=deadline,
        )
        if source_render is None:
            return (
                self._preserved_revision_source(
                    input_data,
                    source_path=None,
                    source_sha256=str(
                        input_data.get("source_html_sha256")
                        or candidate.publication.get("parent_html_sha256")
                        or ""
                    ),
                    source_inspection=None,
                    warning=_PUBLIC_REVISION_COMPARISON_WARNING,
                ),
                False,
            )
        source_path, source_sha256, source_inspection = source_render
        if not self._revision_is_delivery_dominated(
            source_sha256,
            source_inspection,
            candidate,
        ):
            return await self._commit_prepared(candidate), True

        candidate_feedback = _inspection_policy.inspection_feedback(
            candidate.inspection
        )
        retry_input = dict(input_data)
        retry_input["feedback"] = "\n".join(
            item
            for item in (
                str(input_data.get("feedback") or "").strip(),
                (
                    "The uncommitted Chromium render was physically dominated by the "
                    "exact source poster. Repair the measured delivery regressions while "
                    "applying the same receipt-bound visual request:"
                ),
                *candidate_feedback,
            )
            if item
        )
        retry_input["_defer_revision_commit"] = _DEFER_REVISION_COMMIT
        remaining = deadline - asyncio.get_running_loop().time()
        if not _runtime_budget.bound_automatic_revision(
            retry_input,
            remaining_seconds=remaining,
        ):
            return (
                self._preserved_revision_source(
                    input_data,
                    source_path=source_path,
                    source_sha256=source_sha256,
                    source_inspection=source_inspection,
                    warning=_PUBLIC_REVISION_REGRESSION_WARNING,
                    rejected_feedback=candidate_feedback,
                ),
                False,
            )
        retried = await self._revise(
            retry_input,
            progress_callback,
            deadline=deadline,
        )
        if isinstance(retried, _PreparedVersion) and not (
            self._revision_is_delivery_dominated(
                source_sha256,
                source_inspection,
                retried,
            )
        ):
            return await self._commit_prepared(retried), True
        retry_feedback = (
            _inspection_policy.inspection_feedback(retried.inspection)
            if isinstance(retried, _PreparedVersion)
            else []
        )
        retry_failure = (
            str(retried.get("error") or retried.get("summary") or "").strip()
            if isinstance(retried, Mapping)
            else ""
        )
        return (
            self._preserved_revision_source(
                input_data,
                source_path=source_path,
                source_sha256=source_sha256,
                source_inspection=source_inspection,
                warning=_PUBLIC_REVISION_REGRESSION_WARNING,
                rejected_feedback=[
                    *retry_feedback,
                    *([retry_failure] if retry_failure else []),
                ],
            ),
            False,
        )

    async def _inspect_revision_source(
        self,
        input_data: dict[str, Any],
        candidate: _PreparedVersion,
        *,
        deadline: float | None,
    ) -> tuple[Path, str, dict[str, Any]] | None:
        """Render the exact source bytes beside a deferred revision candidate."""

        source_uri = str(input_data.get("source_html_uri") or "").strip()
        source_path = await _runtime_io.resolve_path(
            getattr(self, "ctx", None),
            source_uri,
            base_dir=input_data.get("cwd"),
        )
        if source_path is None or not source_path.is_file():
            return None
        try:
            source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except OSError:
            return None
        expected_sha256 = str(input_data.get("source_html_sha256") or "").strip()
        if expected_sha256 and expected_sha256 != source_sha256:
            return None
        expected_figures = candidate.publication.get("source_figure_sha256s")
        source_figure_sha256s = (
            set(expected_figures)
            if isinstance(expected_figures, (set, frozenset, list, tuple))
            else set()
        )
        inspection_call = _runtime_io.inspect_preview(
            source_path,
            Path(candidate.candidate_path).parent / "source-inspection",
            scale=_runtime_io.bounded_float(
                input_data.get("scale"), default=2.0, low=0.5, high=4.0
            ),
            expected_source_figure_sha256s=source_figure_sha256s,
        )
        if deadline is None:
            inspection = await inspection_call
        else:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                inspection_call.close()
                return None
            try:
                async with asyncio.timeout(remaining):
                    inspection = await inspection_call
            except TimeoutError:
                return None
        return source_path, source_sha256, dict(inspection)

    @staticmethod
    def _revision_is_delivery_dominated(
        source_sha256: str,
        source_inspection: Mapping[str, Any],
        candidate: _PreparedVersion,
    ) -> bool:
        """Compare only rendered delivery facts, never aesthetic preferences."""

        source_evidence = _candidate_control.CandidateEvidence(
            html_sha256=source_sha256,
            inspection=source_inspection,
        )
        candidate_evidence = _candidate_control.CandidateEvidence(
            html_sha256=candidate.html_sha256,
            inspection=candidate.inspection,
        )
        return (
            _candidate_control.delivery_relation(source_evidence, candidate_evidence)
            == "dominated"
        )

    def _preserved_revision_source(
        self,
        input_data: dict[str, Any],
        *,
        source_path: Path | None,
        source_sha256: str,
        source_inspection: dict[str, Any] | None,
        warning: str,
        rejected_feedback: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return a recoverable exact-source result without activating a candidate."""

        source_uri = str(input_data.get("source_html_uri") or "").strip()
        result: dict[str, Any] = {
            "html_uri": source_uri,
            "html_sha256": source_sha256,
            "visual_review_path": str(input_data.get("visual_review_path") or ""),
            "visual_review_mode": "vlm",
            "visual_quality_state": "revision-required",
            "retry_from_checkpoint": False,
            "warnings": [warning, *(rejected_feedback or [])],
        }
        if source_path is not None:
            result["html_path"] = str(source_path)
            result["preview_uri"] = source_path.resolve().as_uri()
        if source_inspection is not None:
            result["inspection"] = source_inspection
            result["inspection_feedback"] = _inspection_policy.inspection_feedback(
                source_inspection
            )
        return _workflow_outcomes.visual_outcome(
            result,
            "visual_revision_required",
            (
                "The exact source poster was preserved because the receipt-bound "
                "revision could not improve its physical delivery evidence."
            ),
            decision_reason="automatic-revision-regressed",
        )

    async def _commit_prepared(
        self,
        prepared: _PreparedVersion,
    ) -> dict[str, Any]:
        """Activate one already-inspected candidate after it beats the active version."""

        committed = await self._publish_version(
            **prepared.publication,
            inspection=prepared.inspection,
            activate=True,
            checkpoint_state=prepared.checkpoint_state,
        )
        if isinstance(committed, _PreparedVersion):
            raise RuntimeError("prepared poster activation remained deferred")
        return committed

    async def _publish_version(
        self,
        *,
        html_text: str,
        source_text: str,
        input_data: dict[str, Any],
        progress_callback: Any,
        workspace: Path,
        parent_html_sha256: str | None,
        live_html_path: Path | None,
        asset_warnings: list[str],
        inspection: dict[str, Any] | None,
        source_figure_sha256s: set[str],
        page_plan: dict[str, Any],
        content_budget: dict[str, Any] | None,
        design_reference: _reference_seeds.ReferenceBundle,
        visual_design_plan: _visual_design.VisualDesignPlan,
        visual_preferences: dict[str, str],
        activate: bool = True,
        checkpoint_state: dict[str, Any] | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any] | _PreparedVersion:
        """Persist exact HTML, inspect it, and expose live-preview metadata."""

        ctx = getattr(self, "ctx", None)
        design_reference = _reference_seeds.ReferenceBundle.from_dict(
            design_reference.to_dict()
        )
        html_bytes = html_text.encode("utf-8")
        html_sha256 = hashlib.sha256(html_bytes).hexdigest()
        candidate_dir = workspace / ".candidates" / html_sha256
        candidate_path = candidate_dir / "poster.html"
        _runtime_io.replace_file_atomic(candidate_path, html_bytes)
        static_report = _core.validate_poster_html(html_text, source_text=source_text)
        source_figure_issues = _core.source_figure_usage_issues(
            html_text,
            source_figure_sha256s,
        )
        if static_report.get("status") != "ok" or source_figure_issues:
            return _workflow_outcomes.error_result(
                "candidate_validation_failed",
                "Poster HTML changed before persistence or failed final validation.",
            )
        if inspection is None:
            await _runtime_io.progress(progress_callback, "poster.inspect", 0.68)
            inspection_call = _runtime_io.inspect_preview(
                candidate_path,
                candidate_dir / "inspection",
                scale=_runtime_io.bounded_float(
                    input_data.get("scale"), default=2.0, low=0.5, high=4.0
                ),
                expected_source_figure_sha256s=source_figure_sha256s,
            )
            if deadline is None:
                inspection = await inspection_call
            else:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    inspection_call.close()
                    return _workflow_outcomes.inspection_deadline_result()
                try:
                    async with asyncio.timeout(remaining):
                        inspection = await inspection_call
                except TimeoutError:
                    return _workflow_outcomes.inspection_deadline_result()
        inspection_feedback = _inspection_policy.inspection_feedback(inspection)
        review_request: dict[str, Any] | None = None
        review_request_path: Path | None = None
        screenshot_path = Path(str(inspection.get("screenshot_path") or ""))
        inspection_passed = inspection.get("status") == "ok"
        visual_evidence = inspection.get("visual_evidence")
        if screenshot_path.is_file() and isinstance(visual_evidence, Mapping):
            try:
                visual_iteration = _revision_state.visual_iteration(
                    input_data.get("visual_iteration")
                )
                content_brief = _revision_state.visual_content_brief(
                    content_budget,
                    page_plan,
                    displayed_html=html_text,
                )
                if visual_design_plan is not None:
                    content_brief["visual_design"] = visual_design_plan.to_dict()
                review_request = _visual_review.build_request(
                    html_path=candidate_path,
                    screenshot_path=screenshot_path,
                    reference=design_reference,
                    content_brief=content_brief,
                    visual_evidence=visual_evidence,
                    iteration=visual_iteration,
                )
                review_dir = (
                    candidate_dir / "visual-review" / f"iteration-{visual_iteration}"
                )
                review_dir.mkdir(parents=True, exist_ok=True)
                review_request_path = review_dir / "visual-review-request.json"
                _runtime_io.write_json_atomic(
                    review_request_path,
                    review_request,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
            except (OSError, _visual_review.VisualReviewError) as exc:
                review_request = None
                review_request_path = None
                asset_warnings.append(f"Cannot prepare visual review: {exc}")
        if not activate:
            if checkpoint_state is not None:
                candidate_sidecar_persisted = _revision_state.persist_revision_sidecar(
                    workspace,
                    artifact_path=candidate_path,
                    html_text=html_text,
                    checkpoint_state=checkpoint_state,
                )
                if candidate_sidecar_persisted is not True:
                    asset_warnings.append(
                        "Deferred poster candidate could not carry its grounded "
                        "revision state; it cannot be resumed after this process."
                    )
            return _PreparedVersion(
                html_sha256=html_sha256,
                candidate_path=str(candidate_path),
                inspection=dict(inspection),
                review_request=review_request,
                review_request_path=(
                    str(review_request_path)
                    if review_request_path is not None
                    else None
                ),
                checkpoint_state=(
                    dict(checkpoint_state) if checkpoint_state is not None else None
                ),
                publication={
                    "html_text": html_text,
                    "source_text": source_text,
                    "input_data": dict(input_data),
                    "progress_callback": progress_callback,
                    "workspace": workspace,
                    "parent_html_sha256": parent_html_sha256,
                    "live_html_path": live_html_path,
                    "asset_warnings": list(asset_warnings),
                    "source_figure_sha256s": set(source_figure_sha256s),
                    "page_plan": dict(page_plan),
                    "content_budget": (
                        dict(content_budget) if content_budget is not None else None
                    ),
                    "design_reference": design_reference,
                    "visual_design_plan": visual_design_plan,
                    "visual_preferences": dict(visual_preferences),
                    "deadline": deadline,
                },
            )
        if checkpoint_state is not None:
            _draft_checkpoint.save(
                workspace,
                stage="author-ready",
                state=checkpoint_state,
            )
        html_path = workspace / "poster.html"
        _runtime_io.replace_file_atomic(html_path, html_bytes)
        html_artifact = await _runtime_io.store_artifact(
            ctx,
            html_path,
            kind="poster",
            title="Scientific poster HTML",
            fmt="html",
            mime="text/html",
        )
        revision_sidecar_persisted = _revision_state.persist_revision_sidecar(
            workspace,
            artifact_path=Path(str(html_artifact["path"])),
            html_text=html_text,
        )
        inspection_path = _runtime_io.inspection_report_path(
            inspection, candidate_dir / "inspection"
        )
        inspection_artifact = await _runtime_io.store_artifact(
            ctx,
            inspection_path,
            kind="poster-report",
            title="Poster rendered inspection",
            fmt="json",
            mime="application/json",
        )
        review_artifacts: list[dict[str, Any]] = []
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
        active_live_path = live_html_path or workspace / "live" / "poster.html"
        try:
            _runtime_io.replace_file_atomic(active_live_path, html_path.read_bytes())
        except OSError as exc:
            return _workflow_outcomes.error_result(
                "live_preview_update_failed",
                f"Live preview activation failed: {exc}",
            )

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
            if inspection_passed and review_request is not None
            else inspection_outcome_code
        )
        if inspection_passed and review_request is not None:
            publication_summary = (
                "Scientific-poster HTML and screenshot are ready for "
                "image-capable review."
            )
        elif not inspection_passed:
            publication_summary = (
                inspection_summary
                or "Rendered inspection did not produce a reviewable poster."
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
        if revision_sidecar_persisted is False:
            warnings.append(
                "Published HTML could not carry its grounded revision state; "
                "revise from live_html_path for this version."
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
        checkpoint_state = _draft_checkpoint.load(workspace)
        inspection_repair_attempt = (
            _revision_state.inspection_repair_attempt(input_data)
            if "inspection_repair_attempt" in input_data
            else _revision_state.inspection_repair_attempt(checkpoint_state or {})
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
            preview_argv=preview_argv,
            selection_state_path=str(active_live_path.parent / "selection-state.json"),
            inspection=inspection,
            page_plan=page_plan,
            content_budget=content_budget,
            density_profile=page_plan.get("density_profile"),
            focal_role=page_plan.get("focal_role"),
            design_reference=design_reference.to_dict(),
            visual_design=(visual_design_plan.to_dict()),
            visual_preferences=dict(visual_preferences),
            reference_source_kind=design_reference.source_kind,
            visual_review_mode=(
                "pending"
                if review_request is not None
                else "not-run"
                if not inspection_passed
                else "deterministic-only"
            ),
            parent_html_sha256=parent_html_sha256,
            visual_quality_state=(
                "awaiting-review" if review_request is not None else "not-reviewable"
            ),
            visual_iteration=(
                review_request.get("iteration") if review_request is not None else None
            ),
            inspection_repair_attempt=inspection_repair_attempt,
            visual_review_request_path=(
                str(review_request_path) if review_request_path is not None else ""
            ),
            visual_review_request=review_request,
            visual_evidence_path=(
                str(evidence_path) if evidence_path.is_file() else ""
            ),
            visual_evidence_sha256=(
                str(visual_evidence.get("bundle_sha256") or "")
                if isinstance(visual_evidence, Mapping)
                else ""
            ),
            inspection_feedback=inspection_feedback,
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
        state = {
            "html_sha256": html_sha256,
            "artifact_path": html_artifact["path"],
            "live_html_path": str(active_live_path),
            "source_text": source_text,
            "source_figure_sha256s": tuple(sorted(source_figure_sha256s)),
            "page_plan": dict(page_plan),
            "content_budget": content_budget,
            "design_reference": design_reference.to_dict(),
            "visual_design": (visual_design_plan.to_dict()),
            "visual_preferences": dict(visual_preferences),
            "inspection": dict(inspection),
            "visual_iteration": (
                review_request.get("iteration") if review_request is not None else 0
            ),
            "inspection_repair_attempt": inspection_repair_attempt,
        }
        if checkpoint_state is not None:
            state = {**checkpoint_state, **state}
        self._versions[str(html_artifact["uri"])] = state
        self._versions[html_sha256] = state
        await _runtime_io.progress(progress_callback, "poster.ready", 1.0)
        return result
