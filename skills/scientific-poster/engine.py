"""Portable host orchestration for direct HTML scientific-poster authoring."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

import poster_core as _core  # noqa: E402 - copied Skill bootstraps its own root
from posterlib import paper_source as _paper_source  # noqa: E402

_LLM_TIMEOUT_SECONDS = 120
_MAX_REPAIR_ATTEMPTS = 2
_MAX_LAYOUT_REPAIR_ATTEMPTS = 2
_MAX_SOURCE_CHARS = 1_500_000
_REVISION_REPAIRABLE_SOURCE_ISSUES = frozenset(
    {"ungrounded_number", "ungrounded_rights_claim"}
)
_PDF_PATH_PATTERN = re.compile(
    r"(?P<path>(?:file://)?(?:~?/|\.{1,2}/)?[A-Za-z0-9_./~-]+\.pdf)",
    re.IGNORECASE,
)
_EMBEDDED_IMAGE_PATTERN = re.compile(
    r"data:image/(?:png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)
_IMAGE_ASSET_PATTERN = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*([\"'])(asset://[0-9]+)\1",
    re.IGNORECASE,
)
_IMAGE_TAG_PATTERN = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SOURCE_FIGURE_ATTR_PATTERN = re.compile(
    r"\bdata-source-figure-sha256\s*=\s*([\"'])([0-9a-f]{64})\1",
    re.IGNORECASE,
)


class _ModelBoundaryError(ValueError):
    """A host model response could not satisfy the HTML contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _SelectionStateError(ValueError):
    """A live-preview selection does not identify the source HTML."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _DraftSource:
    """Grounding text and figures prepared before poster authoring begins."""

    text: str
    authoring_request: str
    assets: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    summary: dict[str, Any]


def _validate_action_boundary(
    data: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Normalize an action and enforce required inputs at every host entry point."""

    try:
        action = _core.normalize_action(data.get("action"))
    except ValueError as exc:
        return None, _error_result("invalid_action", str(exc))
    if action == _core.ACTION_DRAFT and not _has_draft_source(data):
        return None, _error_result(
            "missing_input",
            "A local PDF, complete paper text, or grounded poster brief is required.",
        )
    if action == _core.ACTION_REVISE and not (
        str(data.get("source_html_uri") or "").strip()
        and str(data.get("feedback") or "").strip()
    ):
        return None, _error_result(
            "missing_input",
            "source_html_uri and feedback are required.",
        )
    return action, None


def _poster_assessment(
    ctx: Any,
    input_data: dict[str, Any],
    *,
    inspection: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe final static and browser checks under the common envelope."""

    evidence_refs = [
        str(item.get("uri") or "")
        for item in artifacts
        if isinstance(item, dict) and item.get("uri")
    ]
    inspection_outcome = inspection.get("outcome")
    inspection_code = (
        str(inspection_outcome.get("code") or "")
        if isinstance(inspection_outcome, dict)
        else ""
    )
    inspection_status = str(inspection.get("status") or "")
    if inspection_status == "ok":
        render_status = "passed"
        render_summary = "Chromium inspection completed without blocking layout issues."
    elif inspection_code in {"inspection_unavailable", "missing_capability"}:
        render_status = "unknown"
        render_summary = (
            "Browser inspection was unavailable; static HTML validation passed, "
            "but rendered layout quality remains unknown."
        )
    elif inspection_status == "error":
        render_status = "failed"
        render_summary = "Rendered inspection reported a blocking layout failure."
    else:
        render_status = "unknown"
        render_summary = "Rendered inspection did not provide a conclusive quality result."

    status = render_status
    summary = (
        "Poster HTML passed the provider's static grounding and safety contract. "
        + render_summary
    )
    deliverable_id = str(
        input_data.get("deliverable_id")
        or input_data.get("deliverable")
        or "artifact.poster"
    )
    authority = getattr(ctx, "provider_authority", None)
    authority_fingerprint = (
        str(authority.get("fingerprint") or "")
        if isinstance(authority, dict)
        else ""
    )
    contract_hash = authority_fingerprint or hashlib.sha256(
        b"scientific-poster:quality-contract:v1"
    ).hexdigest()
    step_id = str(
        getattr(ctx, "workflow_step_key", "")
        or getattr(ctx, "workflow_step_id", "")
        or input_data.get("workflow_step_id")
        or deliverable_id
    )
    return {
        "schema": "omni.deliverable-assessment/v1",
        "deliverable_id": deliverable_id,
        "provider_binding_id": str(
            input_data.get("provider_binding_id")
            or f"skill:scientific-poster:{deliverable_id}"
        ),
        "provider": "scientific-poster",
        "provider_authority_fingerprint": authority_fingerprint,
        "contract_hash": contract_hash,
        "step_id": step_id,
        "feedback": summary,
        "status": status,
        # Draft/revise already perform bounded repair before publication and
        # artifact writes make an automatic replay unsafe.
        "retryable": False,
        "effective_inputs": {
            "action": str(input_data.get("action") or _core.ACTION_DRAFT),
            "page_mode": str(input_data.get("page_mode") or ""),
            "source_html_uri": str(input_data.get("source_html_uri") or ""),
            "has_pdf_source": bool(_pdf_uri(input_data)),
        },
        "criteria": [
            {
                "criterion_id": "poster_html_valid",
                "status": "passed",
                "summary": (
                    "Final HTML passed static structure, safety, grounding, "
                    "and source-figure validation."
                ),
                "evidence_refs": evidence_refs,
            },
            {
                "criterion_id": "poster_render_inspected",
                "status": render_status,
                "summary": render_summary,
                "evidence_refs": evidence_refs,
            },
        ],
        "evidence_refs": evidence_refs,
        "summary": summary,
    }


class ScientificPosterEngine:
    """Ask the host model for complete inert HTML and manage review versions."""

    def __init__(self) -> None:
        self._versions: dict[str, dict[str, Any]] = {}

    @staticmethod
    def validate_params(
        *,
        arguments: dict | None = None,
        input_data: dict | None = None,
    ) -> dict[str, Any] | None:
        """Validate the engine boundary before a host invokes it."""

        data = arguments if arguments is not None else input_data or {}
        _, error = _validate_action_boundary(data)
        return error

    async def execute(
        self,
        progress_callback: Any = None,
        **input_data: Any,
    ) -> dict[str, Any]:
        """Dispatch model-backed authoring or a portable deterministic action."""

        action, error = _validate_action_boundary(input_data)
        if error is not None:
            return error
        assert action is not None
        if action == _core.ACTION_DRAFT:
            return await self._draft(input_data, progress_callback)
        if action == _core.ACTION_REVISE:
            return await self._revise(input_data, progress_callback)

        from scripts import run as portable
        portable_input = {**input_data, "action": action}
        if action == _core.ACTION_APPROVE:
            source_uri = str(portable_input.get("source_html_uri") or "").strip()
            source_sha256 = str(portable_input.get("source_html_sha256") or "").strip()
            state = self._versions.get(source_uri) or self._versions.get(source_sha256)
            if state is not None:
                supplied_source = _explicit_source_text(portable_input)
                stored_source = str(state.get("source_text") or "")
                if supplied_source and supplied_source != stored_source:
                    return _error_result(
                        "approval_source_mismatch",
                        "Approval grounding source differs from the authored poster version.",
                    )
                portable_input["source_text"] = str(state.get("source_text") or "")
                portable_input["source_figure_sha256s"] = list(
                    state.get("source_figure_sha256s") or ()
                )
                portable_input.pop("pdf_uri", None)
                portable_input.pop("source", None)
        return portable.run(portable_input)

    async def _draft(
        self,
        input_data: dict[str, Any],
        progress_callback: Any,
    ) -> dict[str, Any]:
        """Author one self-contained HTML poster from supplied research."""

        ctx = getattr(self, "ctx", None)
        workspace = _create_workspace(input_data, ctx)
        try:
            source = await _prepare_draft_source(
                input_data,
                ctx=ctx,
                workspace=workspace,
                progress_callback=progress_callback,
            )
            source_text = source.text
            if len(source_text) > _MAX_SOURCE_CHARS:
                return _error_result(
                    "source_too_large",
                    f"Research input exceeds the {_MAX_SOURCE_CHARS}-character safety limit.",
                )
            assets, asset_warnings = await _prepare_assets(
                [
                    *source.assets,
                    *[
                        _as_user_asset(item)
                        for item in _as_list(input_data.get("assets"))
                    ],
                ],
                ctx,
            )
            system, user = _draft_prompt(
                source_text=source_text,
                assets=assets,
                page=input_data.get("page"),
                resource_guidance=_resource_guidance(input_data),
                authoring_request=source.authoring_request,
            )
            await _progress(progress_callback, "poster.author", 0.18)
            html_template = await _request_html(
                _host_llm(ctx),
                system=system,
                user=user,
                validate=lambda candidate: _validate_candidate(
                    candidate,
                    source_text=source_text,
                    assets=assets,
                    required_source_figure_sha256s=_source_figure_sha256s(assets),
                ),
                initial_temperature=0.0,
            )
            await _progress(progress_callback, "poster.render-check", 0.46)
            scale = _bounded_float(input_data.get("scale"), default=2.0, low=0.5, high=4.0)
            draft_page = _validate_candidate(
                html_template,
                source_text=source_text,
                assets=assets,
            ).get("page")

            def validate_layout(candidate: str) -> dict[str, Any]:
                report = _validate_candidate(
                    candidate,
                    source_text=source_text,
                    assets=assets,
                    required_source_figure_sha256s=_source_figure_sha256s(assets),
                )
                if report.get("status") == "ok" and report.get("page") != draft_page:
                    return {
                        "status": "error",
                        "issues": [
                            _issue(
                                "page_changed",
                                "Layout repair changed the declared physical page dimensions.",
                            )
                        ],
                    }
                return report

            html_template, inspection = await _repair_rendered_layout(
                html_template,
                assets=assets,
                llm=_host_llm(ctx),
                workspace=workspace,
                validate=validate_layout,
                scale=scale,
            )
            html_text = _embed_assets(html_template, assets)
        except _ModelBoundaryError as exc:
            return _error_result(exc.code, str(exc))
        except _paper_source.PaperSourceError as exc:
            return _paper_source_failure(exc)
        except (OSError, UnicodeError, ValueError) as exc:
            return _error_result(
                "candidate_validation_failed",
                f"Poster authoring failed: {exc}",
            )
        result = await self._publish_version(
            html_text=html_text,
            source_text=source_text,
            input_data=input_data,
            progress_callback=progress_callback,
            workspace=workspace,
            parent_html_sha256=None,
            live_html_path=None,
            asset_warnings=[*source.warnings, *asset_warnings],
            inspection=inspection,
            source_figure_sha256s=_source_figure_sha256s(assets),
        )
        result["paper_source"] = source.summary
        return result

    async def _revise(
        self,
        input_data: dict[str, Any],
        progress_callback: Any,
    ) -> dict[str, Any]:
        """Revise the exact source HTML, optionally focused by a DOM selection."""

        ctx = getattr(self, "ctx", None)
        source_uri = str(input_data.get("source_html_uri") or "").strip()
        source_path = await _resolve_path(ctx, source_uri)
        state = self._versions.get(source_uri)
        if source_path is None and state is not None:
            cached = Path(str(state.get("artifact_path") or ""))
            source_path = cached if cached.is_file() else None
        if source_path is None:
            return _error_result(
                "source_not_found",
                "The requested poster HTML artifact could not be resolved.",
            )
        try:
            source_bytes = source_path.read_bytes()
            source_html = source_bytes.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            return _error_result("source_read_failed", str(exc))
        parent_sha256 = hashlib.sha256(source_bytes).hexdigest()
        if state is not None and parent_sha256 != state["html_sha256"]:
            return _error_result(
                "source_read_failed",
                "The source URI bytes changed after publication.",
            )
        expected_sha256 = str(input_data.get("source_html_sha256") or "").strip()
        if expected_sha256 and expected_sha256 != parent_sha256:
            return _error_result(
                "stale_selection",
                "source_html_sha256 does not match the source artifact bytes.",
            )

        workspace = _create_workspace(input_data, ctx)
        source_text = _explicit_source_text(input_data) or str(
            (state or {}).get("source_text") or ""
        )
        required_source_figure_sha256s = set(
            (state or {}).get("source_figure_sha256s") or ()
        )
        if not source_text and _pdf_uri(input_data):
            try:
                prepared_source = await _prepare_draft_source(
                    input_data,
                    ctx=ctx,
                    workspace=workspace,
                    progress_callback=progress_callback,
                )
                source_text = prepared_source.text
                required_source_figure_sha256s.update(
                    str(item.get("content_sha256") or "")
                    for item in prepared_source.assets
                )
            except _paper_source.PaperSourceError as exc:
                return _paper_source_failure(exc)
        if not source_text and not _pdf_uri(input_data):
            source_text = _source_text(input_data)
        if not source_text:
            return _error_result(
                "missing_input",
                "Revision requires the original grounded source text.",
            )
        if len(source_text) > _MAX_SOURCE_CHARS:
            return _error_result(
                "source_too_large",
                f"Research input exceeds the {_MAX_SOURCE_CHARS}-character safety limit.",
            )
        source_report = _core.validate_poster_html(source_html, source_text=source_text)
        blocking_source_issues = [
            item
            for item in source_report.get("issues", [])
            if isinstance(item, dict)
            and item.get("code") not in _REVISION_REPAIRABLE_SOURCE_ISSUES
        ]
        if source_report.get("status") != "ok" and blocking_source_issues:
            return _error_result(
                "source_html_invalid",
                "The source poster no longer satisfies the HTML contract.",
            )
        selection = input_data.get("selection_state")
        if selection is not None:
            try:
                selection = _validate_revision_selection(
                    selection,
                    parent_sha256=parent_sha256,
                    source_html=source_html,
                )
            except _SelectionStateError as exc:
                return _error_result(exc.code, str(exc))

        feedback = str(input_data.get("feedback") or "").strip()
        html_template, embedded_assets = _tokenize_embedded_images(source_html)
        required_source_figure_sha256s.update(
            _source_figure_sha256s(embedded_assets)
        )
        system, user = _revision_prompt(
            source_html=html_template,
            source_text=source_text,
            feedback=feedback,
            selection=selection,
        )
        page = source_report.get("page")

        def validate(candidate: str) -> dict[str, Any]:
            report = _validate_candidate(
                candidate,
                source_text=source_text,
                assets=embedded_assets,
                required_source_figure_sha256s=required_source_figure_sha256s,
            )
            if report.get("status") == "ok" and report.get("page") != page:
                return {
                    "status": "error",
                    "issues": [
                        {
                            "code": "page_changed",
                            "message": "Revision changed the approved physical page dimensions.",
                            "severity": "error",
                        }
                    ],
                }
            return report

        try:
            await _progress(progress_callback, "poster.revise", 0.18)
            llm = _host_llm(ctx)
            revised_template = await _request_html(
                llm,
                system=system,
                user=user,
                validate=validate,
                initial_temperature=0.0,
            )
            await _progress(progress_callback, "poster.render-check", 0.46)
            scale = _bounded_float(input_data.get("scale"), default=2.0, low=0.5, high=4.0)
            revised_template, inspection = await _repair_rendered_layout(
                revised_template,
                assets=embedded_assets,
                llm=llm,
                workspace=workspace,
                validate=validate,
                scale=scale,
            )
            html_text = _embed_assets(revised_template, embedded_assets)
        except _ModelBoundaryError as exc:
            return _error_result(exc.code, str(exc))

        live_path_value = str((state or {}).get("live_html_path") or "").strip()
        live_path = Path(live_path_value) if live_path_value else None
        return await self._publish_version(
            html_text=html_text,
            source_text=source_text,
            input_data=input_data,
            progress_callback=progress_callback,
            workspace=workspace,
            parent_html_sha256=parent_sha256,
            live_html_path=live_path,
            asset_warnings=[],
            inspection=inspection,
            source_figure_sha256s=required_source_figure_sha256s,
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
        live_html_path: Path | None,
        asset_warnings: list[str],
        inspection: dict[str, Any] | None,
        source_figure_sha256s: set[str],
    ) -> dict[str, Any]:
        """Persist exact HTML, inspect it, and expose live-preview metadata."""

        ctx = getattr(self, "ctx", None)
        html_path = workspace / "poster.html"
        html_path.write_bytes(html_text.encode("utf-8"))
        static_report = _core.validate_poster_html(html_text, source_text=source_text)
        source_figure_issues = _core.source_figure_usage_issues(
            html_text,
            source_figure_sha256s,
        )
        if static_report.get("status") != "ok" or source_figure_issues:
            return _error_result(
                "candidate_validation_failed",
                "Poster HTML changed before persistence or failed final validation.",
            )
        html_sha256 = hashlib.sha256(html_path.read_bytes()).hexdigest()
        html_artifact = await _store_artifact(
            ctx,
            html_path,
            kind="poster",
            title="Scientific poster HTML",
            fmt="html",
            mime="text/html",
        )
        await _progress(progress_callback, "poster.inspect", 0.68)
        if inspection is None:
            inspection = await _inspect_preview(
                html_path,
                workspace / "inspection",
                scale=_bounded_float(input_data.get("scale"), default=2.0, low=0.5, high=4.0),
            )
        inspection_path = _inspection_report_path(inspection, workspace / "inspection")
        inspection_artifact = await _store_artifact(
            ctx,
            inspection_path,
            kind="poster-report",
            title="Poster rendered inspection",
            fmt="json",
            mime="application/json",
        )
        inspection_passed = inspection.get("status") == "ok"
        active_live_path = live_html_path or workspace / "live" / "poster.html"
        try:
            _replace_file_atomic(active_live_path, html_path.read_bytes())
        except OSError as exc:
            return _error_result(
                "live_preview_update_failed",
                f"Live preview activation failed: {exc}",
            )

        preview_argv = _core.build_preview_argv(
            active_live_path,
            skill_dir=SKILL_DIR,
            python_executable=sys.executable,
        )
        outcome_code = "preview_ready" if inspection_passed else str(
            (inspection.get("outcome") or {}).get("code") or "inspection_unavailable"
        )
        warnings = [
            *asset_warnings,
            *[
                str(item.get("message") or item)
                for item in inspection.get("warnings", [])
                if isinstance(item, dict)
            ],
        ]
        grounding_source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
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
            summary=(
                "Scientific-poster HTML is ready for review."
                if inspection_passed
                else "HTML is ready, but browser inspection must pass before approval."
            ),
            requires_approval=inspection_passed,
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
            parent_html_sha256=parent_html_sha256,
            warnings=warnings,
            artifacts=[html_artifact, inspection_artifact],
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
        result["deliverable_assessment"] = _poster_assessment(
            ctx,
            input_data,
            inspection=inspection,
            artifacts=[html_artifact, inspection_artifact],
        )
        state = {
            "html_sha256": html_sha256,
            "artifact_path": html_artifact["path"],
            "live_html_path": str(active_live_path),
            "source_text": source_text,
            "source_figure_sha256s": tuple(sorted(source_figure_sha256s)),
        }
        self._versions[str(html_artifact["uri"])] = state
        self._versions[html_sha256] = state
        await _progress(progress_callback, "poster.ready", 1.0)
        return result


async def _prepare_draft_source(
    input_data: dict[str, Any],
    *,
    ctx: Any,
    workspace: Path,
    progress_callback: Any,
) -> _DraftSource:
    """Prepare a complete source before any HTML authoring model call."""

    pdf_uri = _pdf_uri(input_data)
    if not pdf_uri:
        text = _source_text(input_data)
        return _DraftSource(
            text=text,
            authoring_request=str(input_data.get("instructions") or "").strip(),
            assets=(),
            warnings=(),
            summary={"kind": "text", "character_count": len(text)},
        )

    await _progress(progress_callback, "poster.prepare-source", 0.04)
    pdf_path = await _resolve_path(
        ctx,
        pdf_uri,
        base_dir=input_data.get("cwd"),
    )
    if pdf_path is None:
        raise _paper_source.PaperSourceError(
            "source_not_found",
            f"The requested PDF could not be resolved: {pdf_uri}",
        )
    prepared = await asyncio.to_thread(
        _paper_source.prepare_pdf,
        pdf_path,
        workspace / "paper-figures",
    )
    assets = tuple(
        {
            "path": figure["path"],
            "description": _figure_description(figure),
            "source_kind": "pdf_figure",
            "content_sha256": figure["sha256"],
            "figure_number": figure["figure_number"],
            "page": figure["page"],
            "crop_bbox": figure["crop_bbox"],
        }
        for figure in prepared.figures
    )
    warnings = (
        ()
        if assets
        else (
            "The PDF text was extracted, but no caption-anchored figures were found; "
            "the poster must use grounded HTML/CSS diagrams instead.",
        )
    )
    request = input_data.get("input")
    return _DraftSource(
        text=prepared.text,
        authoring_request=request.strip() if isinstance(request, str) else "",
        assets=assets,
        warnings=warnings,
        summary={
            "kind": "pdf",
            "path": str(pdf_path),
            "title": prepared.title,
            "authors": prepared.authors,
            "page_count": prepared.page_count,
            "figure_count": len(prepared.figures),
            "figures": [
                {
                    "figure_number": figure["figure_number"],
                    "page": figure["page"],
                    "crop_bbox": figure["crop_bbox"],
                    "sha256": figure["sha256"],
                }
                for figure in prepared.figures
            ],
        },
    )


def _figure_description(figure: dict[str, Any]) -> str:
    parts = [
        f"Figure {figure['figure_number']} from source PDF, page {figure['page']}"
    ]
    caption = str(figure.get("caption") or "").strip()
    context = str(figure.get("context") or "").strip()
    if caption:
        parts.append(f"caption: {caption}")
    if context:
        parts.append(f"paper discussion: {context}")
    return ". ".join(parts)


def _pdf_uri(input_data: dict[str, Any]) -> str:
    explicit = input_data.get("pdf_uri")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    research = input_data.get("research")
    if isinstance(research, dict):
        nested = research.get("pdf_uri")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    source = input_data.get("source")
    if isinstance(source, str) and source.lower().split("?", 1)[0].endswith(".pdf"):
        return source.strip()
    request = input_data.get("input")
    if not isinstance(request, str):
        return ""
    match = _PDF_PATH_PATTERN.search(request)
    return match.group("path").rstrip(".,;:)]}") if match is not None else ""


def _has_draft_source(input_data: dict[str, Any]) -> bool:
    return bool(_pdf_uri(input_data) or _source_text(input_data))


async def _repair_rendered_layout(
    html_template: str,
    *,
    assets: list[dict[str, Any]],
    llm: Any,
    workspace: Path,
    validate: Callable[[str], dict[str, Any]],
    scale: float,
    inspect: Callable[..., Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Use bounded Chromium feedback to repair layout without changing the paper story."""

    inspector = inspect or _inspect_preview
    candidate_dir = workspace / "render-check"
    candidate_path = candidate_dir / "poster.html"
    current = html_template
    content_fingerprint = _core.poster_content_fingerprint(current)
    inspection: dict[str, Any] = {}
    previous_repair_was_noop = False
    for attempt in range(_MAX_LAYOUT_REPAIR_ATTEMPTS + 1):
        _replace_file_atomic(candidate_path, _embed_assets(current, assets).encode("utf-8"))
        inspection = await inspector(
            candidate_path,
            candidate_dir / f"pass-{attempt}",
            scale=scale,
        )
        if previous_repair_was_noop and inspection.get("status") != "ok":
            inspection = {
                **inspection,
                "warnings": [
                    *[
                        item
                        for item in inspection.get("warnings", [])
                        if isinstance(item, dict)
                    ],
                    {
                        "code": "layout_repair_noop",
                        "message": "The previous layout repair returned unchanged HTML.",
                    },
                ],
            }
        if inspection.get("status") == "ok" or attempt == _MAX_LAYOUT_REPAIR_ATTEMPTS:
            return current, inspection
        code = str((inspection.get("outcome") or {}).get("code") or "")
        if code in {"inspection_unavailable", "missing_capability"}:
            return current, inspection
        warnings = [
            item
            for item in inspection.get("warnings", [])
            if isinstance(item, dict)
        ]
        system, user = _layout_repair_prompt(current, warnings)
        revised = await _request_html(
            llm,
            system=system,
            user=user,
            validate=validate,
            max_repair_attempts=0,
            initial_temperature=0.0,
        )
        previous_repair_was_noop = revised == current
        if _core.poster_content_fingerprint(revised) != content_fingerprint:
            inspection = {
                **inspection,
                "warnings": [
                    *[
                        item
                        for item in inspection.get("warnings", [])
                        if isinstance(item, dict)
                    ],
                    {
                        "code": "layout_content_changed",
                        "message": (
                            "Layout repair changed scientific copy or protected semantic identity."
                        ),
                    },
                ],
            }
            return current, inspection
        current = revised
    return current, inspection


def _layout_repair_prompt(
    html_template: str,
    warnings: list[dict[str, Any]],
) -> tuple[str, str]:
    system = """Repair the rendered layout of a complete top-conference scientific poster.
Return the entire HTML document beginning with <!doctype html>, without Markdown or commentary.
Change layout and CSS only. Preserve every scientific claim, number, citation, source label, data-poster-id, data-poster-region, asset:// token, and physical page dimension exactly. Do not add or remove scientific content.
Use readable conference-scale type, purposeful density, and occupied page area. Do not solve overflow or empty space by shrinking text."""
    diagnostics = json.dumps(warnings, ensure_ascii=False, sort_keys=True)
    targeted_guidance: list[str] = []
    if any(item.get("code") == "poster_bottom_underfill" for item in warnings):
        targeted_guidance.append(
            "The bottom-underfill diagnostic requires the final semantic region to end within "
            "the last 8% of the poster height. Enlarge or reflow existing scientific content, "
            "figures, type, and row distribution so the page is genuinely occupied; do not fake "
            "the metric with an empty stretched wrapper or oversized blank gap."
        )
    dense_hero = next(
        (item for item in warnings if item.get("code") == "dense_hero_text_block"),
        None,
    )
    if dense_hero is not None:
        targeted_guidance.append(
            "The hero-density diagnostic found display-scale text wrapping into four or more "
            "lines while using less than 70% of its parent content width. Remove restrictive "
            "max-inline-size/max-width rules, widen or rebalance the hero columns, and preserve "
            "the exact copy. Do not solve this only by shrinking type."
        )
    physical_issue = next(
        (
            item
            for item in warnings
            if item.get("code") == "poster_physical_size_mismatch"
        ),
        None,
    )
    if physical_issue is not None:
        expected_width = physical_issue.get("expected_width_mm")
        expected_height = physical_issue.get("expected_height_mm")
        if isinstance(expected_width, (int, float)) and isinstance(
            expected_height, (int, float)
        ):
            targeted_guidance.append(
                "The poster root itself—not only @page, html, or body—must render at exactly "
                f"{expected_width:g}mm × {expected_height:g}mm. Set @page margin to 0, give the "
                "single body-level [data-poster-id] root those exact width and height values, and "
                "move printable margins into root padding while keeping all content inside it."
            )
    user = (
        "Correct every Chromium diagnostic below. Prefer reflowing grids, removing fixed or "
        "stretched row heights, enlarging type, and resizing existing figures. Do not return "
        "unchanged HTML while any diagnostic remains.\n"
        f"{' '.join(targeted_guidance)}\n"
        f"Chromium diagnostics:\n{diagnostics}\n\n"
        f"Current complete HTML:\n{html_template}"
    )
    return system, user


async def _request_html(
    llm: Any,
    *,
    system: str,
    user: str,
    validate: Callable[[str], dict[str, Any]],
    max_repair_attempts: int = _MAX_REPAIR_ATTEMPTS,
    initial_temperature: float = 0.2,
) -> str:
    """Request exact complete HTML with at most two validator-guided repairs."""

    request = user
    last_issues: list[dict[str, Any]] = []
    for attempt in range(max_repair_attempts + 1):
        try:
            response = await asyncio.wait_for(
                llm.chat(
                    system,
                    request,
                    temperature=initial_temperature if attempt == 0 else 0.0,
                ),
                timeout=_LLM_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise _ModelBoundaryError("llm_error", "HTML model call timed out") from exc
        except Exception as exc:  # noqa: BLE001 - host LLM is an external boundary
            raise _ModelBoundaryError("llm_error", f"HTML model call failed: {exc}") from exc
        if not isinstance(response, str):
            raw = str(response or "")
            last_issues = [_issue("non_text_response", "Model response must be text HTML.")]
        else:
            raw = _normalize_model_html(response)
            if not raw.lower().startswith("<!doctype html>"):
                last_issues = [
                    _issue(
                        "complete_document_required",
                        "The first bytes must be <!doctype html>; no preamble is allowed.",
                    )
                ]
            else:
                report = validate(raw)
                if report.get("status") == "ok":
                    return raw
                last_issues = [
                    item
                    for item in report.get("issues", [])
                    if isinstance(item, dict) and item.get("severity") != "warning"
                ]
                if not last_issues:
                    last_issues = [_issue("invalid_html", "HTML validation failed.")]
        if attempt < max_repair_attempts:
            if any(
                item.get("code") in {"non_text_response", "complete_document_required"}
                for item in last_issues
            ):
                request = (
                    "Regenerate the complete HTML document from the original request. "
                    "The previous response contained no usable complete document. Begin with "
                    "<!doctype html>, end with </html>, and return no Markdown or commentary.\n\n"
                    f"Original authoring request:\n{user}"
                )
                continue
            grounding_guidance = _repair_guidance(last_issues)
            request = (
                "Repair the complete HTML document using every validator issue below. "
                "Return the full corrected document beginning with <!doctype html>. "
                "No Markdown fences or commentary. Preserve valid scientific content and "
                "make only the changes required by the issues.\n\n"
                f"Validator issues:\n{json.dumps(last_issues, ensure_ascii=False)}\n\n"
                f"{grounding_guidance}"
                f"Invalid HTML:\n{raw}"
            )
    message = "; ".join(str(item.get("message") or item) for item in last_issues)
    raise _ModelBoundaryError(
        "candidate_validation_failed",
        f"HTML remained invalid after {max_repair_attempts} repair attempt(s): {message}",
    )


def _normalize_model_html(value: str) -> str:
    """Extract exactly one complete HTML document from harmless model prose."""

    text = value.strip()
    documents = re.findall(r"<!doctype html>.*?</html>", text, re.I | re.S)
    if len(documents) == 1:
        return documents[0].strip()
    html_documents = re.findall(r"<html(?:\s[^>]*)?>.*?</html>", text, re.I | re.S)
    if not documents and len(html_documents) == 1:
        return "<!doctype html>" + html_documents[0].strip()
    return text


def _repair_guidance(issues: list[dict[str, Any]]) -> str:
    guidance: list[str] = []
    if any(item.get("code") == "ungrounded_number" for item in issues):
        guidance.append(
            "Scientific grounding rule: replace every rejected numeric surface form with the "
            "paper's exact wording or remove that claim. Do not retain a rejected form, compute "
            "a delta, round a value, or add a new identifier or quantity."
        )
    if any(item.get("code") == "ungrounded_rights_claim" for item in issues):
        guidance.append(
            "Remove every unsupported copyright, rights, reproduction, or permission statement; "
            "do not replace it with another legal claim."
        )
    return "\n".join(guidance) + ("\n\n" if guidance else "")


def _draft_prompt(
    *,
    source_text: str,
    assets: list[dict[str, Any]],
    page: object,
    resource_guidance: str,
    authoring_request: str,
) -> tuple[str, str]:
    system = """You are authoring a complete HTML/CSS scientific poster for expert discussion at a top conference.
Return one complete HTML document beginning with <!doctype html>. Return no Markdown or commentary.
Use inline CSS and no JavaScript, forms, event handlers, animation, remote resources, or external fonts.
The poster must be physically sized in millimetres through @page and HTML/body CSS.
Use a restrained conference visual language, a strong reading path, evidence-led hierarchy, and dense but readable use of space. It is not a dashboard, advertisement, or decorative infographic.
Give a long central hero claim enough inline measure: do not compress display-scale text into four or more lines inside a narrow fraction of an otherwise wide panel. Rebalance hero columns or widen the text block before reducing type.
For an A0 poster, keep ordinary body copy at least 28 CSS px and provenance at least 18 CSS px. The rendered poster root must retain the declared physical page dimensions. Semantic content must reach the last 8% of the page height; never create tall grid rows whose content occupies only the top edge.
Use one body-level poster root (prefer main or article) with a stable data-poster-id. Add stable data-poster-id and data-source-label attributes to meaningful elements. Mark exactly one visible hero, method, evidence, limitations, and provenance region with data-poster-region.
Use the five data-poster-region values only on five unique wrapper elements under the root: one hero wrapper, one method wrapper, one evidence wrapper, one limitations wrapper, and one provenance wrapper. Never repeat a region value on a descendant or split one semantic region across several wrappers.
The semantic shape is root > [data-poster-region="hero"], [data-poster-region="method"], [data-poster-region="evidence"], [data-poster-region="limitations"], [data-poster-region="provenance"]. Layout containers may appear around or inside them but must not carry data-poster-region.
Ground every claim, number, author, affiliation, limitation, caption, and citation only in the supplied source. Every semantic region's data-source-label must contain a verifiable locator such as p.3, §3, Figure 1, Table 2, Abstract, Title page, or References. Select the paper's decisive contribution, method novelty, and strongest results; do not merely restate the abstract.
When extracted source figures are available, use at least one relevant asset:// token as the quoted src of an img element. Prefer the paper's real architecture, result, or qualitative figure over redrawing it. Give every used figure an accurate alt, caption, takeaway, and source locator.
Never invent copyright, permission, reproduction, venue, contact, or repository statements. Include them only when the supplied source states them.
Reusable HTML/CSS primitives are optional guidance, never mandatory slots. Compose the page to fit its actual scientific content."""
    page_text = _page_request(page)
    asset_text = _asset_prompt(assets)
    user = (
        "Author the complete HTML/CSS scientific poster.\n\n"
        f"User instructions (preferences only, never scientific evidence):\n"
        f"{authoring_request or 'No additional preferences.'}\n\n"
        f"Page constraints:\n{page_text}\n\n"
        f"Optional reusable resource guidance:\n{resource_guidance or 'No reusable primitive is required.'}\n\n"
        f"Available embedded figures:\n{asset_text}\n\n"
        "SUPPLIED PAPER OR GROUNDED BRIEF (the only scientific authority):\n"
        "<source>\n"
        f"{source_text}\n"
        "</source>"
    )
    return system, user


def _revision_prompt(
    *,
    source_html: str,
    source_text: str,
    feedback: str,
    selection: dict[str, Any] | None,
) -> tuple[str, str]:
    system = """Revise a complete inert HTML/CSS scientific poster.
Return the entire corrected HTML document beginning with <!doctype html>, with no Markdown or commentary.
Keep physical page dimensions, scientific grounding, stable data-poster-id values, and unrelated regions unchanged unless the feedback explicitly requires a broader change. Preserve the five semantic regions and keep their content reaching the last 8% of the page height. Do not add scripts, active content, remote resources, or invented claims."""
    selection_text = (
        json.dumps(selection, ensure_ascii=False, sort_keys=True)
        if selection is not None
        else "No element selection; interpret the feedback at poster level."
    )
    user = (
        f"Feedback:\n{feedback}\n\nSelected DOM context:\n{selection_text}\n\n"
        f"Grounding source:\n<source>\n{source_text}\n</source>\n\n"
        f"Current complete HTML:\n{source_html}"
    )
    return system, user


def _validate_candidate(
    html_text: str,
    *,
    source_text: str,
    assets: list[dict[str, Any]],
    required_source_figure_sha256s: set[str] | None = None,
) -> dict[str, Any]:
    asset_source_figures = {
        str(item["content_sha256"])
        for item in assets
        if item.get("source_kind") == "pdf_figure"
    }
    expected_source_figures = (
        {value for value in required_source_figure_sha256s if value}
        if required_source_figure_sha256s is not None
        else asset_source_figures
    )
    try:
        resolved = _embed_assets(html_text, assets)
    except ValueError as exc:
        return {"status": "error", "issues": [_issue("asset_token", str(exc))]}
    report = _core.validate_poster_html(resolved, source_text=source_text)
    source_figure_issues = _core.source_figure_usage_issues(
        resolved,
        expected_source_figures,
    )
    if source_figure_issues:
        report = {
            **report,
            "status": "error",
            "issues": [
                *[
                    item
                    for item in report.get("issues", [])
                    if isinstance(item, dict)
                ],
                *source_figure_issues,
            ],
        }
    return report


def _source_figure_sha256s(assets: list[dict[str, Any]]) -> set[str]:
    """Return verified PDF-figure image identities from a prepared asset manifest."""

    return {
        str(item.get("content_sha256") or "")
        for item in assets
        if item.get("source_kind") == "pdf_figure"
        and re.fullmatch(r"[0-9a-f]{64}", str(item.get("content_sha256") or ""))
    }


def _embed_assets(html_text: str, assets: list[dict[str, Any]]) -> str:
    html_text = _annotate_source_figure_tokens(html_text, assets)
    mapping = {str(item["token"]): str(item["data_uri"]) for item in assets}
    used = set(re.findall(r"asset://\d+", html_text))
    unknown = sorted(used - set(mapping))
    if unknown:
        raise ValueError("Unknown embedded figure token(s): " + ", ".join(unknown))
    for token in sorted(used, key=len, reverse=True):
        html_text = html_text.replace(token, mapping[token])
    return html_text


def _annotate_source_figure_tokens(
    html_text: str,
    assets: list[dict[str, Any]],
) -> str:
    """Bind PDF-figure hashes to the exact img elements that consume their tokens."""

    source_figures = {
        str(item["token"]): str(item["content_sha256"])
        for item in assets
        if item.get("source_kind") == "pdf_figure"
    }
    if not source_figures:
        return html_text

    def annotate(match: re.Match[str]) -> str:
        tag = match.group(0)
        token_match = _IMAGE_ASSET_PATTERN.search(tag)
        if token_match is None:
            return tag
        digest = source_figures.get(token_match.group(2))
        if digest is None:
            return tag
        tag = _SOURCE_FIGURE_ATTR_PATTERN.sub("", tag)
        insertion = f' data-source-figure-sha256="{digest}"'
        return tag[:-2] + insertion + "/>" if tag.endswith("/>") else tag[:-1] + insertion + ">"

    return _IMAGE_TAG_PATTERN.sub(annotate, html_text)


def _tokenize_embedded_images(html_text: str) -> tuple[str, list[dict[str, str]]]:
    """Replace repeated embedded image bytes with compact revision-only tokens."""

    assets: list[dict[str, str]] = []
    tokens_by_uri: dict[str, str] = {}
    source_figure_sha256s = {
        match.group(2) for match in _SOURCE_FIGURE_ATTR_PATTERN.finditer(html_text)
    }

    def replace(match: re.Match[str]) -> str:
        data_uri = match.group(0)
        token = tokens_by_uri.get(data_uri)
        if token is None:
            token = f"asset://{len(tokens_by_uri) + 1}"
            tokens_by_uri[data_uri] = token
            digest = _core.data_image_sha256(data_uri)
            assets.append(
                {
                    "token": token,
                    "data_uri": data_uri,
                    "content_sha256": digest or "",
                    "source_kind": (
                        "pdf_figure"
                        if digest in source_figure_sha256s
                        else "user_asset"
                    ),
                }
            )
        return token

    return _EMBEDDED_IMAGE_PATTERN.sub(replace, html_text), assets


def _resource_guidance(input_data: dict[str, Any]) -> str:
    explicit = str(input_data.get("resource_guidance") or "").strip()
    if explicit:
        return explicit[:40_000]
    try:
        from posterlib.registry import (
            load_resource_package,
            query_resources,
            resolve_registry_roots,
        )

        roots = resolve_registry_roots(
            cwd=input_data.get("cwd") or Path.cwd(),
            skill_dir=SKILL_DIR,
            project_library=input_data.get("project_library"),
            user_library=input_data.get("user_library"),
        )
        allow_candidates = input_data.get("allow_candidates") is True
        components = query_resources(
            roots,
            kind="component",
            allow_candidates=allow_candidates,
        )
        policies = query_resources(
            roots,
            kind="layout-policy",
            allow_candidates=allow_candidates,
        )
        sections: list[str] = [
            "These packages are optional examples. Adapt, combine, or omit them; do not turn them into fixed slots."
        ]
        for record in [*components, *policies]:
            package = load_resource_package(record)
            header = (
                f"## {record.kind} {record.resource_id}@{record.version} "
                f"[{record.layer}; sha256:{record.content_sha256}]"
            )
            if record.kind == "component":
                sections.append(
                    f"{header}\nPurpose: {package.manifest['purpose']}\n"
                    f"HTML fragment:\n{package.fragment_html}\nCSS:\n{package.style_css}"
                )
            else:
                sections.append(
                    f"{header}\nPurpose: {package.manifest['purpose']}\n"
                    f"Guidance:\n{package.guidance_markdown}\nCSS tokens/helpers:\n{package.style_css}"
                )
        return "\n\n".join(sections)[:80_000]
    except (OSError, ValueError):
        return "Reusable packages are unavailable; author a complete adaptive poster directly."


def _page_request(value: object) -> str:
    if isinstance(value, dict):
        width = value.get("width_mm")
        height = value.get("height_mm")
        if isinstance(width, (int, float)) and isinstance(height, (int, float)):
            return f"Use exactly {width:g} mm × {height:g} mm."
    return "Choose A0 portrait (841 mm × 1189 mm) unless the supplied venue says otherwise."


def _asset_prompt(assets: list[dict[str, Any]]) -> str:
    if not assets:
        return "No supplied figure assets. Create diagrams only with inert HTML/CSS/SVG grounded in the source."
    return "Use at least one relevant token as <img src=\"asset://…\">.\n" + "\n".join(
        f"- {item['token']}: {item['description'] or item['filename']} ({item['mime']})"
        for item in assets
    )


def _validate_revision_selection(
    value: object,
    *,
    parent_sha256: str,
    source_html: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _SelectionStateError("candidate_validation_failed", "selection_state must be an object")
    selection = dict(value)
    if selection.get("source_html_sha256") != parent_sha256:
        raise _SelectionStateError("stale_selection", "Selection belongs to different HTML bytes.")
    poster_id = str(selection.get("poster_id") or "").strip()
    identities = _core.poster_identity_map(source_html)
    if not poster_id or poster_id not in identities:
        raise _SelectionStateError(
            "invalid_selection",
            "Selection does not identify a stable data-poster-id in the source HTML.",
        )
    expected = identities[poster_id]
    if str(selection.get("semantic_region") or "") != expected.get("semantic_region", ""):
        raise _SelectionStateError(
            "invalid_selection",
            "Selection semantic_region does not match the source HTML.",
        )
    for name in ("component_id", "component_version"):
        if str(selection.get(name) or "") != expected.get(name, ""):
            raise _SelectionStateError(
                "invalid_selection",
                "Selection component identity does not match the source HTML.",
            )
    return selection


def _host_llm(ctx: Any) -> Any:
    llm = getattr(ctx, "llm", None) if ctx is not None else None
    if llm is None or not callable(getattr(llm, "chat", None)):
        raise _ModelBoundaryError(
            "llm_unavailable",
            "This host action requires an LLM supplied by the host runtime.",
        )
    return llm


def _source_text(input_data: dict[str, Any]) -> str:
    value: Any = ""
    for key in ("source_text", "research", "source", "input"):
        candidate = input_data.get(key)
        if candidate not in (None, "", {}, []):
            value = candidate
            break
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return str(value).strip()


def _explicit_source_text(input_data: dict[str, Any]) -> str:
    for key in ("source_text", "research", "source"):
        value = input_data.get(key)
        if value in (None, "", {}, []):
            continue
        if key == "source" and isinstance(value, str) and value.lower().endswith(".pdf"):
            continue
        if isinstance(value, str):
            return value.strip()
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            return str(value).strip()
    return ""


def _create_workspace(input_data: dict[str, Any], ctx: Any) -> Path:
    output_dir = str(input_data.get("output_dir") or "").strip()
    paths = getattr(ctx, "paths", None) if ctx is not None else None
    artifacts_dir = getattr(paths, "artifacts_dir", None) if paths is not None else None
    if artifacts_dir is not None:
        artifacts_root = Path(artifacts_dir).expanduser().resolve()
        requested = Path(output_dir).expanduser().resolve() if output_dir else None
        base = (
            requested
            if requested is not None and requested.is_relative_to(artifacts_root)
            else artifacts_root / "poster-workspaces"
        )
    elif output_dir:
        base = Path(output_dir).expanduser()
    else:
        base = Path.cwd() / "scientific-poster-output"
    session = _slug(str(getattr(ctx, "session_id", "") or "local"))
    task = _slug(str(input_data.get("task_id") or getattr(ctx, "task_id", "") or ""))
    prefix = "-".join(part for part in ("scientific-poster", session, task) if part)
    for _ in range(5):
        candidate = base / f"{prefix}-{uuid.uuid4().hex[:10]}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("could not allocate a unique scientific poster workspace")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_").lower()[:48]


async def _prepare_assets(values: Any, ctx: Any) -> tuple[list[dict[str, Any]], list[str]]:
    raw_values = _as_list(values)
    resolved: dict[str, Path | None] = {}
    for value in raw_values:
        source = _asset_source(value)
        if source and source not in resolved:
            resolved[source] = await _resolve_path(ctx, source)
    return _core.prepare_asset_manifest(raw_values, resolve=lambda item: resolved.get(item))


def _as_list(values: Any) -> list[Any]:
    if values is None:
        return []
    if isinstance(values, (str, Path, dict)):
        return [values]
    try:
        return list(values)
    except TypeError:
        return [values]


def _as_user_asset(value: Any) -> Any:
    """Prevent caller-supplied images from impersonating extracted PDF figures."""

    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    normalized["source_kind"] = "user_asset"
    normalized.pop("content_sha256", None)
    normalized.pop("figure_number", None)
    normalized.pop("page", None)
    normalized.pop("crop_bbox", None)
    return normalized


def _asset_source(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("uri", "path", "source", "file"):
            if value.get(key):
                return str(value[key]).strip()
        return ""
    return str(value or "").strip()


async def _resolve_path(
    ctx: Any,
    value: str,
    *,
    base_dir: object = None,
) -> Path | None:
    if not value:
        return None
    store = getattr(ctx, "artifacts", None) if ctx is not None else None
    if store is not None and callable(getattr(store, "resolve_path", None)):
        try:
            resolved = await store.resolve_path(value)
        except Exception:  # noqa: BLE001 - host resolver is an external boundary
            resolved = None
        if resolved is not None:
            return Path(resolved)
        if value.startswith("artifact://"):
            return None
    path = Path(value.removeprefix("file://")).expanduser()
    if not path.is_absolute() and base_dir:
        path = Path(str(base_dir)).expanduser() / path
    return path if path.is_file() else None


async def _store_artifact(
    ctx: Any,
    path: Path,
    *,
    kind: str,
    title: str,
    fmt: str,
    mime: str,
) -> dict[str, Any]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    stored = None
    store = getattr(ctx, "artifacts", None) if ctx is not None else None
    if store is not None and callable(getattr(store, "put_file", None)):
        try:
            stored = await store.put_file(
                path,
                kind=kind,
                title=title,
                mime=mime,
                session_id=str(getattr(ctx, "session_id", "") or ""),
                task_id=str(getattr(ctx, "task_id", "") or ""),
                copy=True,
                meta={"skill": "scientific-poster", "format": fmt, "sha256": digest},
            )
        except Exception:  # noqa: BLE001 - local snapshot is the safe fallback
            stored = None
    uri = str(getattr(stored, "uri", "") or "")
    stored_value = getattr(stored, "path", None)
    stored_path = Path(stored_value) if stored_value else None
    valid_uri = re.match(r"^(?:artifact://|snapshot:|bundle:|project-report:)", uri)
    valid_copy = False
    if valid_uri and stored_path is not None and stored_path.is_file():
        try:
            valid_copy = (
                hashlib.sha256(stored_path.read_bytes()).hexdigest() == digest
                and stored_path.resolve() != path.resolve()
            )
        except OSError:
            valid_copy = False
    if not valid_copy:
        uri = f"snapshot:{digest}"
        stored_path = _local_snapshot(path, raw=raw, digest=digest)
    return {
        "title": title,
        "format": fmt,
        "uri": uri,
        "path": str(stored_path),
        "mime": mime,
        "size_bytes": len(raw),
        "sha256": digest,
    }


def _local_snapshot(path: Path, *, raw: bytes, digest: str) -> Path:
    target = path.parent / ".snapshots" / digest / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise OSError(f"snapshot target must not be a symlink: {target}")
    try:
        with target.open("xb") as handle:
            handle.write(raw)
    except FileExistsError:
        pass
    if target.read_bytes() != raw:
        raise OSError(f"snapshot content collision: {target}")
    target.chmod(0o444)
    return target


async def _inspect_preview(
    html_path: Path,
    out_dir: Path,
    *,
    scale: float,
) -> dict[str, Any]:
    try:
        from scripts import inspect_poster as inspector

        result = await inspector.inspect_document(html_path, out_dir, scale=scale)
    except Exception as exc:  # noqa: BLE001 - browser tooling is optional
        return {
            **_core.outcome_result(
                "inspection_unavailable",
                summary="Rendered inspection is unavailable; review HTML manually.",
            ),
            "warnings": [{"code": "inspection_unavailable", "message": str(exc)}],
        }
    normalized = _core.normalize_outcome_result(
        result,
        fallback_code="inspection_unavailable",
        fallback_summary="Rendered inspection returned an invalid result.",
    )
    if not isinstance(result, dict):
        normalized["warnings"] = [
            {"code": "invalid_inspection", "message": "Expected an object."}
        ]
    return normalized


def _inspection_report_path(inspection: dict[str, Any], directory: Path) -> Path:
    raw = str(inspection.get("report_path") or "").strip()
    path = Path(raw) if raw else directory / "dom-report.json"
    if not path.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "dom-report.json"
        path.write_text(
            json.dumps(inspection, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return path


def _replace_file_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message, "severity": "error"}


def _error_result(code: str, message: str) -> dict[str, Any]:
    result = _core.outcome_result(
        code,
        summary=f"scientific-poster did not complete: {message}",
    )
    if result["status"] == "error":
        result["error"] = message
    return result


def _paper_source_failure(exc: _paper_source.PaperSourceError) -> dict[str, Any]:
    if exc.code == "missing_capability":
        from posterlib.capability import missing_result

        return missing_result(
            "pdf-reading",
            dependency="pymupdf",
            stage="poster.prepare-source",
            error=exc,
        )
    return _error_result(exc.code, str(exc))


def _bounded_float(value: Any, *, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


async def _progress(callback: Any, stage: str, pct: float, **data: Any) -> None:
    if callback is None:
        return
    try:
        result = callback(stage=stage, progress=pct, **data)
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001 - progress must never break authoring
        return
