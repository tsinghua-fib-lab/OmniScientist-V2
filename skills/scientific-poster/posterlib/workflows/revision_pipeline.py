"""Grounded HTML revision pipeline for an existing poster version."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import poster_contract
from lxml import html as lxml_html

from posterlib.content import html_contract
from posterlib.generation import authoring, model_runtime
from posterlib.runtime import runtime_io
from posterlib.sources import source_preparation
from posterlib.visual import (
    inspection_feedback,
    reference_analysis,
    reference_catalog,
    review_contract,
)

from . import (
    outcomes,
    request_normalization,
    revision_state,
)

PublishVersion = Callable[..., Awaitable[dict[str, Any]]]

_REVISION_REPAIRABLE_SOURCE_ISSUES = frozenset(
    {
        "malformed_mathml_operator",
        "math_layout_override",
    }
)
_REVIEW_COPY_ROLES = frozenset({"text", "detail-points", "flow-detail"})
_MANUAL_COPY_ROLES = frozenset({*_REVIEW_COPY_ROLES, "heading", "takeaway"})


def _tokenize_math(html_text: str) -> tuple[str, dict[str, str]]:
    """Replace derived MathML trees with stable atomic revision tokens."""

    document = lxml_html.document_fromstring(html_text)
    frozen: dict[str, str] = {}
    for index, element in enumerate(document.xpath("//math"), start=1):
        token = f"math-{index}"
        frozen[token] = lxml_html.tostring(
            element,
            encoding="unicode",
            method="html",
            with_tail=False,
        )
        placeholder = lxml_html.Element("span")
        placeholder.set("data-frozen-math", token)
        placeholder.set("data-latex", str(element.get("data-latex") or ""))
        placeholder.tail = element.tail
        element.getparent().replace(element, placeholder)
    return (
        lxml_html.tostring(
            document,
            encoding="unicode",
            method="html",
            pretty_print=True,
            doctype="<!doctype html>",
        ),
        frozen,
    )


def _restore_math(html_text: str, frozen: Mapping[str, str]) -> str:
    """Restore every frozen formula exactly once before validation and rendering."""

    if not frozen:
        return html_text
    document = lxml_html.document_fromstring(html_text)
    placeholders = document.xpath("//*[@data-frozen-math]")
    observed = [str(element.get("data-frozen-math") or "") for element in placeholders]
    missing = sorted(set(frozen) - set(observed))
    unexpected = sorted(set(observed) - set(frozen))
    duplicates = sorted(token for token in set(observed) if observed.count(token) != 1)
    if missing or unexpected or duplicates:
        problems = []
        if missing:
            problems.append("missing=" + ", ".join(missing))
        if unexpected:
            problems.append("unexpected=" + ", ".join(unexpected))
        if duplicates:
            problems.append("duplicated=" + ", ".join(duplicates))
        raise ValueError("Frozen math tokens changed: " + "; ".join(problems))
    for placeholder in placeholders:
        token = str(placeholder.get("data-frozen-math") or "")
        equation = lxml_html.fragment_fromstring(frozen[token])
        equation.tail = placeholder.tail
        placeholder.getparent().replace(placeholder, equation)
    return lxml_html.tostring(
        document,
        encoding="unicode",
        method="html",
        pretty_print=True,
        doctype="<!doctype html>",
    )


def _with_current_inspection(user: str, inspection: Mapping[str, Any]) -> str:
    """Attach the current browser facts before the editor makes its first change."""

    public = {
        key: value
        for key, value in inspection.items()
        if not str(key).startswith("_")
    }
    return (
        f"{user}\n\nCURRENT CHECKED BROWSER STATE BEFORE THE FIRST EDIT\n"
        "These are measurements of the current source, not target sizes or a "
        "host-chosen layout. Use them to identify the governing CSS relationship "
        "before editing.\n"
        f"{json.dumps(public, ensure_ascii=False, default=str)}"
    )


def _finite_float(value: Any) -> float | None:
    """Return one finite measurement without accepting booleans."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _module_containers_fit_page(inspection: Mapping[str, Any]) -> bool:
    """Keep a shortened page from cropping a module's enclosing layout box."""

    report = inspection.get("report") or {}
    page_height = (report.get("poster") or {}).get("height")
    if page_height is None:
        return True
    for container in report.get("layout_containers") or []:
        rect = container.get("rect")
        if (
            container.get("paints_bottom")
            and rect
            and rect["top"] + rect["height"] > page_height + 1
        ):
            return False
    return True


def _source_figure_extent_baseline(
    evidence: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Index physical source-figure extents from one inspected candidate."""

    observations = evidence.get("observations") if isinstance(evidence, Mapping) else None
    baseline: dict[str, dict[str, Any]] = {}
    for item in observations if isinstance(observations, list) else []:
        if not isinstance(item, Mapping) or item.get("kind") != "source_figure_extent":
            continue
        observation_id = str(item.get("id") or "").strip()
        facts = item.get("facts")
        if not observation_id or not isinstance(facts, Mapping):
            continue
        width = _finite_float(facts.get("painted_width_mm"))
        height = _finite_float(facts.get("painted_height_mm"))
        if width is None and height is None:
            continue
        baseline[observation_id] = {
            "module_id": str(item.get("module_id") or "").strip(),
            "painted_width_mm": width,
            "painted_height_mm": height,
            "figure_wrapper_width_mm": _finite_float(
                facts.get("figure_wrapper_width_mm")
            ),
            "module_width_mm": _finite_float(facts.get("module_width_mm")),
            "image_box_width_mm": _finite_float(facts.get("image_box_width_mm")),
        }
    return baseline


def _source_figure_extent_changes(
    evidence: Mapping[str, Any] | None,
    baseline: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Describe current physical extents relative to an inspected baseline."""

    current = _source_figure_extent_baseline(evidence)
    changes: list[dict[str, Any]] = []
    for observation_id in sorted(set(baseline) & set(current)):
        prior = baseline[observation_id]
        now = current[observation_id]
        item: dict[str, Any] = {
            "observation_id": observation_id,
            "module_id": str(now.get("module_id") or prior.get("module_id") or ""),
        }
        for axis in ("width", "height"):
            key = f"painted_{axis}_mm"
            prior_value = _finite_float(prior.get(key))
            current_value = _finite_float(now.get(key))
            if prior_value is None or current_value is None:
                continue
            item[f"baseline_{key}"] = prior_value
            item[f"current_{key}"] = current_value
            change = round(current_value - prior_value, 6)
            item[f"{axis}_change_mm"] = change
            item[f"{axis}_direction"] = (
                "larger" if change > 0 else "smaller" if change < 0 else "unchanged"
            )
        for key in (
            "figure_wrapper_width_mm",
            "module_width_mm",
            "image_box_width_mm",
        ):
            current_value = _finite_float(now.get(key))
            if current_value is not None:
                item[f"current_{key}"] = current_value
        if len(item) > 2:
            changes.append(item)
    return changes


def _persist_diagnostic_candidate(
    *,
    candidate_path: Path,
    html_text: str,
    html_sha256: str,
    checkpoint_state: dict[str, Any],
    screenshot_path: Path,
) -> dict[str, Any]:
    """Keep one statically valid WIP resumable without publishing it."""

    persisted = revision_state.persist_revision_sidecar(
        artifact_path=candidate_path,
        html_text=html_text,
        checkpoint_state=checkpoint_state,
    )
    return {
        "html_path": str(candidate_path),
        "html_sha256": html_sha256,
        "screenshot_path": str(screenshot_path),
        "sidecar_path": (
            str(revision_state.revision_sidecar_path(candidate_path))
            if persisted
            else ""
        ),
        "revision_state_persisted": bool(persisted),
    }


async def run_revision(
    input_data: dict[str, Any],
    progress_callback: Any,
    *,
    ctx: Any,
    publish_version: PublishVersion,
    max_source_chars: int,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Revise one exact grounded HTML version without owning engine state."""

    input_data = dict(input_data)
    source_uri = str(input_data.get("source_html_uri") or "").strip()
    source_path = await runtime_io.resolve_path(ctx, source_uri)
    if source_path is None:
        return outcomes.error_result(
            "source_not_found",
            "The requested poster HTML artifact could not be resolved.",
        )
    try:
        source_bytes = source_path.read_bytes()
        source_html = source_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return outcomes.error_result("source_read_failed", str(exc))
    parent_sha256 = hashlib.sha256(source_bytes).hexdigest()
    state = revision_state.revision_checkpoint_state(
        source_path,
        source_html=source_html,
        html_sha256=parent_sha256,
    )
    if state is None:
        return outcomes.error_result(
            "source_read_failed",
            "Revision requires the draft checkpoint sidecar bound to this HTML.",
        )
    expected_sha256 = str(input_data.get("source_html_sha256") or "").strip()
    if expected_sha256 and expected_sha256 != parent_sha256:
        return outcomes.error_result(
            "stale_selection",
            "source_html_sha256 does not match the source artifact bytes.",
        )

    workspace = runtime_io.create_workspace(input_data, ctx)
    source_text = state["source_text"]
    content_budget = revision_state.visual_content_brief(
        {}, displayed_html=source_html,
    )["grounded_authority"]
    if not source_text:
        return outcomes.error_result(
            "missing_input",
            "The draft checkpoint has no grounded source text.",
        )
    if len(source_text) > max_source_chars:
        return outcomes.error_result(
            "source_too_large",
            f"Research input exceeds the {max_source_chars}-character safety limit.",
        )
    source_report = poster_contract.validate_poster_html(
        source_html,
    )
    blocking_source_issues = [
        item
        for item in source_report.get("issues", [])
        if isinstance(item, dict)
        and item.get("code") not in _REVISION_REPAIRABLE_SOURCE_ISSUES
    ]
    if source_report.get("status") != "ok" and blocking_source_issues:
        return outcomes.error_result(
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
            return outcomes.error_result(exc.code, str(exc))

    original_direction = state["authoring_request"]
    caller_direction = source_preparation.authoring_request(input_data)
    caller_feedback = "\n\n".join(
        dict.fromkeys(
            item for item in (
                caller_direction if caller_direction != original_direction else "",
                str(input_data.get("feedback") or "").strip(),
            ) if item
        )
    )
    feedback = caller_feedback
    visual_review_path = str(input_data.get("visual_review_path") or "").strip()
    visual_receipt: dict[str, Any] | None = None
    receipt_operations = frozenset[str]()
    content_replan_targets = frozenset[str]()
    if visual_review_path:
        try:
            (
                visual_feedback,
                visual_iteration,
                receipt_operations,
                content_replan_targets,
                visual_receipt,
            ) = revision_state.visual_revision_feedback(
                visual_review_path,
                parent_html_sha256=parent_sha256,
                content_budget=content_budget,
            )
            feedback = f"Image-review diagnostics:\n{visual_feedback}"
            if caller_feedback:
                feedback += f"\n\nCurrent user revision:\n{caller_feedback}"
        except review_contract.ReviewContractError as exc:
            return outcomes.error_result(exc.code, str(exc))
        input_data["visual_iteration"] = visual_iteration
    else:
        # A normal user/harness revision starts a fresh bounded visual-review
        # cycle.  Do not inherit an exhausted iteration from the source draft.
        input_data["visual_iteration"] = 0
    html_template, embedded_assets = html_contract.tokenize_embedded_images(source_html)
    editor_html, frozen_math = _tokenize_math(html_template)
    html_template = _restore_math(editor_html, frozen_math)
    required_source_figure_sha256s = html_contract.source_figure_sha256s(
        embedded_assets
    )
    raw_design_reference = state.get("design_reference")
    try:
        if not isinstance(raw_design_reference, Mapping):
            return outcomes.error_result(
                "missing_input",
                "Revision requires the exact design_reference used for the draft.",
            )
        design_reference = reference_catalog.ReferenceBundle.from_dict(
            dict(raw_design_reference)
        )
        raw_visual_design = state.get("visual_design")
        if not isinstance(raw_visual_design, Mapping):
            return outcomes.error_result(
                "missing_input",
                "Revision requires the exact visual_design used for the draft.",
            )
        reference_brief = reference_analysis.ReferenceAnalysis.from_dict(
            dict(raw_visual_design)
        )
        revision_warnings: list[str] = []
    except (TypeError, reference_catalog.ReferenceSeedError):
        return outcomes.error_result(
            "source_read_failed",
            "Revision design_reference is invalid.",
        )
    except reference_analysis.ReferenceAnalysisError:
        return outcomes.error_result(
            "source_read_failed",
            "Revision visual_design is invalid.",
        )
    paper_identity = _revision_paper_identity(state, embedded_assets)
    page = source_report.get("page")
    stored_page_plan = state.get("page_plan")
    if not isinstance(page, dict):
        return outcomes.error_result(
            "source_html_invalid",
            "Source poster has no physical page.",
        )
    revision_page_plan = revision_state.revision_page_policy(
        stored_page_plan=stored_page_plan,
        page=page,
    )
    checkpoint_source = state
    caller_mode = str(input_data.get("revision_mode") or "").strip()
    if not visual_review_path and caller_mode == "content-replan":
        try:
            content_replan_targets = revision_state.validate_content_replan_targets(
                input_data.get("content_replan_targets"),
                content_budget=content_budget,
            )
        except ValueError as exc:
            return outcomes.error_result("invalid_payload", str(exc))
    try:
        revision_mode = (
            revision_state.vlm_revision_mode(
                receipt_operations,
            )
            if visual_review_path
            else revision_state.manual_revision_mode(caller_mode)
        )
    except ValueError as exc:
        return outcomes.error_result("invalid_payload", str(exc))
    required_visual_operation = (
        "reflow"
        if "reflow" in receipt_operations
        else "restyle"
        if "restyle" in receipt_operations
        else ""
    )
    if visual_review_path and revision_mode == "content-replan":
        copy_inventory = authoring.copy_channel_inventory(
            editor_html,
            sorted(content_replan_targets),
        )
        editable_targets = frozenset(
            module_id
            for module_id, channels in copy_inventory.items()
            if channels
        )
        unavailable_targets = sorted(content_replan_targets - editable_targets)
        if unavailable_targets:
            required_visual_operation = "reflow"
            feedback += (
                "\n\nThese review targets have no editable explanatory-copy channel: "
                + ", ".join(unavailable_targets)
                + ". Keep their scientific content unchanged and solve their visible "
                "defect through composition and layout."
            )
        content_replan_targets = editable_targets
        if not content_replan_targets:
            revision_mode = "full-layout"
    content_brief = revision_state.visual_content_brief(
        revision_page_plan,
        displayed_html=html_template,
        user_direction=original_direction,
    )
    raw_figure_observations = state.get("source_figure_observations")
    retained_figure_observations = {
        digest: dict(observation)
        for digest, observation in (
            raw_figure_observations.items()
            if isinstance(raw_figure_observations, Mapping)
            else ()
        )
        if digest in required_source_figure_sha256s
        and isinstance(observation, Mapping)
    }
    compact_layout = bool(
        visual_review_path
        and revision_mode == "full-layout"
        and required_visual_operation == "reflow"
    )
    if compact_layout:
        assert visual_receipt is not None
        compact_feedback = json.dumps({
            "summary": visual_receipt["summary"],
            "scores": visual_receipt["scores"],
            "issues": [
                {key: issue[key] for key in ("criterion", "targets", "operation")}
                for issue in visual_receipt["critical_issues"]
            ],
            "current_user_revision": caller_feedback,
        }, ensure_ascii=False)
        system, user = authoring.layout_composition_prompt(
            feedback=compact_feedback,
            page_plan=revision_page_plan,
            content_brief=content_brief,
            authoring_request=original_direction,
            source_figure_observations=retained_figure_observations,
        )
    else:
        system, user = authoring.revision_prompt(
            source_html=editor_html,
            feedback=feedback,
            selection=selection,
            page_plan=revision_page_plan,
            content_brief=content_brief,
            revision_mode=revision_mode,
            content_replan_targets=sorted(content_replan_targets),
            source_text=source_text,
            source_figure_observations=retained_figure_observations,
            allow_source_corrections=not bool(visual_review_path),
            required_visual_operation=required_visual_operation,
            reference_brief=reference_brief,
        )

    def restore_math(candidate: str) -> str:
        try:
            return _restore_math(candidate, frozen_math)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Preserve each data-frozen-math placeholder exactly once; the host "
                "restores its verified MathML before rendering. " + str(exc)
            ) from exc

    def validate(candidate: str, *, require_change: bool = True) -> dict[str, Any]:
        try:
            restored = restore_math(candidate)
        except ValueError as exc:
            return {
                "status": "error",
                "issues": [
                    {
                        "code": "frozen_math_token_changed",
                        "severity": "error",
                        "message": str(exc),
                    }
                ],
            }
        report = html_contract.validate_candidate(
            restored,
            assets=embedded_assets,
            required_source_figure_sha256s=required_source_figure_sha256s,
            expected_page=revision_page_plan,
            paper_identity=paper_identity,
            reference_html=html_template,
            content_replan_targets=(
                content_replan_targets
                if revision_mode == "content-replan"
                else None
            ),
            editable_text_roles=(
                _REVIEW_COPY_ROLES
                if visual_review_path
                else _MANUAL_COPY_ROLES
            ),
        )
        if require_change and report.get("status") == "ok" and restored == html_template:
            return {
                **report,
                "status": "error",
                "issues": [
                    {
                        "code": "revision_noop",
                        "severity": "error",
                        "message": (
                            "The requested revision requires a changed candidate, but "
                            "the candidate is byte-identical to the current HTML. Apply "
                            "the requested revision before returning the document."
                        ),
                    }
                ],
            }
        return report

    diagnostic_candidate: dict[str, Any] | None = None
    source_figure_baseline: dict[str, dict[str, Any]] = {}
    source_figure_baseline_kind = "visual_review" if visual_review_path else "revision_source"

    async def inspect_candidate(candidate: str) -> dict[str, Any]:
        nonlocal diagnostic_candidate, source_figure_baseline
        restored = restore_math(candidate)
        embedded = html_contract.embed_assets(restored, embedded_assets)
        digest = hashlib.sha256(embedded.encode("utf-8")).hexdigest()
        edit_dir = workspace / ".candidates" / digest
        edit_path = edit_dir / "poster.html"
        runtime_io.replace_file_atomic(edit_path, embedded.encode("utf-8"))
        inspection_call = runtime_io.inspect_preview(
            edit_path,
            edit_dir / "inspection",
            scale=runtime_io.bounded_float(
                input_data.get("scale"), default=2.0, low=0.5, high=4.0
            ),
        )
        if deadline is None:
            inspection = await inspection_call
        else:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                inspection_call.close()
                return {"status": "error", "layout_feedback": ["deadline exhausted"]}
            try:
                async with asyncio.timeout(remaining):
                    inspection = await inspection_call
            except TimeoutError:
                return {"status": "error", "layout_feedback": ["inspection timed out"]}
        evidence = inspection.get("visual_evidence")
        is_revision_source = candidate == editor_html
        if is_revision_source and isinstance(evidence, Mapping):
            source_figure_baseline = _source_figure_extent_baseline(evidence)
        result = inspection_feedback.editor_feedback(
            inspection,
            figure_observations=retained_figure_observations,
        )
        extent_changes = (
            []
            if is_revision_source
            else _source_figure_extent_changes(
                evidence if isinstance(evidence, Mapping) else None,
                source_figure_baseline,
            )
        )
        if extent_changes:
            result["source_figure_changes"] = {
                "baseline": source_figure_baseline_kind,
                "figures": extent_changes,
            }
        screenshot_path = Path(str(inspection.get("screenshot_path") or ""))
        diagnostic_candidate = _persist_diagnostic_candidate(
            candidate_path=edit_path,
            html_text=embedded,
            html_sha256=digest,
            checkpoint_state=checkpoint_for(candidate, revision_page_plan),
            screenshot_path=screenshot_path,
        )
        diagnostic_candidate["inspection_status"] = str(
            inspection.get("status") or "error"
        )
        return result

    def checkpoint_for(
        candidate_template: str,
        candidate_page_plan: dict[str, Any],
    ) -> dict[str, Any]:
        restored = restore_math(candidate_template)
        visual_iteration = (
            revision_state.visual_iteration(input_data.get("visual_iteration"))
            if visual_review_path
            else 0
        )
        return revision_state.revision_checkpoint_payload(
            checkpoint_source,
            html_template=revision_state.bind_asset_tokens(restored, embedded_assets),
            page_plan=candidate_page_plan,
            visual_iteration=visual_iteration,
        )

    async def publish_template(
        candidate_template: str,
        candidate_page_plan: dict[str, Any],
        *,
        inspection: dict[str, Any] | None,
    ) -> dict[str, Any]:
        restored = restore_math(candidate_template)
        result = await publish_version(
            html_text=html_contract.embed_assets(restored, embedded_assets),
            source_text=source_text,
            input_data=input_data,
            progress_callback=progress_callback,
            workspace=workspace,
            parent_html_sha256=parent_sha256,
            asset_warnings=revision_warnings,
            inspection=inspection,
            source_figure_sha256s=required_source_figure_sha256s,
            page_plan=candidate_page_plan,
            design_reference=design_reference,
            reference_brief=reference_brief,
            visual_preferences=dict(state["visual_preferences"]),
            checkpoint_state=checkpoint_for(
                candidate_template,
                candidate_page_plan,
            ),
            deadline=deadline,
        )
        editing_issue = state.get("editing_issue")
        if (
            isinstance(editing_issue, Mapping)
            and editing_issue.get("code") != "authoring_unavailable"
        ):
            result["editing_issue"] = dict(editing_issue)
        return result

    source_diagnostics = validate(editor_html, require_change=False)
    initial_inspection = (
        await inspect_candidate(editor_html)
        if source_diagnostics.get("status") == "ok"
        else {"status": "error", "diagnostics": source_diagnostics}
    )
    initial_feedback = _with_current_inspection("", initial_inspection)

    space_only = bool(
        compact_layout
        and visual_receipt
        and visual_receipt["critical_issues"]
        and all(
            issue["criterion"] == "space_use" and issue["operation"] == "reflow"
            for issue in visual_receipt["critical_issues"]
        )
    )
    original_strategy = (
        stored_page_plan.get("source_page_strategy", stored_page_plan.get("strategy"))
        if isinstance(stored_page_plan, Mapping) else None
    )
    initial_report = initial_inspection.get("report")
    page_overflow = isinstance(initial_report, Mapping) and any(
        isinstance(warning, Mapping)
        and warning.get("code") in {"element_outside_poster", "module_clipped_by_ancestor"}
        for warning in initial_report.get("warnings") or []
    )
    if original_strategy == "auto" and page_overflow:
        current_height = float(revision_page_plan["height_mm"])
        maximum_height = float(
            stored_page_plan.get(
                "source_max_height_mm",
                stored_page_plan.get("max_height_mm", current_height),
            )
        )
        if maximum_height > current_height:
            expanded = authoring.resize_page_height(
                editor_html, current_height, maximum_height
            )
            if expanded is not None:
                previous_plan = dict(revision_page_plan)
                revision_page_plan.update({
                    "height_mm": maximum_height,
                    "min_height_mm": maximum_height,
                    "max_height_mm": maximum_height,
                })
                if validate(expanded).get("status") == "ok":
                    measured = await inspect_candidate(expanded)
                    if measured.get("status") == "ok":
                        return await publish_template(
                            expanded, revision_page_plan,
                            inspection=measured.get("_accepted_inspection"),
                        )
                revision_page_plan.clear()
                revision_page_plan.update(previous_plan)
    if space_only and original_strategy == "auto":
        report = initial_inspection.get("report")
        measured_modules = report.get("modules") if isinstance(report, Mapping) else None
        heights = {
            str(module["module_id"]): float(module["rect"]["height"])
            for module in measured_modules or []
            if isinstance(module, Mapping) and isinstance(module.get("rect"), Mapping)
        }
        balanced = authoring.balance_two_column_modules(editor_html, heights)
        current_height = float(revision_page_plan["height_mm"])
        minimum_height = float(
            stored_page_plan.get(
                "source_min_height_mm", stored_page_plan.get("min_height_mm", current_height)
            )
        )
        if minimum_height < current_height:
            previous_plan = dict(revision_page_plan)
            source_layout = balanced or editor_html

            async def try_height(height_mm: float) -> tuple[str, dict[str, Any]] | None:
                resized = authoring.resize_page_height(
                    source_layout, current_height, height_mm
                )
                if resized is None:
                    return None
                resized = authoring.finish_poster_shell(resized)
                revision_page_plan.update({
                    "height_mm": height_mm,
                    "min_height_mm": height_mm,
                    "max_height_mm": height_mm,
                })
                if validate(resized).get("status") != "ok":
                    return None
                await runtime_io.progress(progress_callback, "poster.revise", 0.18)
                measured = await inspect_candidate(resized)
                if measured.get("status") == "ok" and _module_containers_fit_page(measured):
                    return resized, measured
                return None

            lower, upper = minimum_height, current_height
            trial_height = minimum_height
            best: tuple[str, dict[str, Any]] | None = None
            best_height = current_height
            while trial_height < upper:
                trial = await try_height(trial_height)
                if trial is None:
                    lower = trial_height
                else:
                    best, best_height, upper = trial, trial_height, trial_height
                if upper - lower <= 5.0:
                    break
                next_height = round((lower + upper) / 10.0) * 5.0
                if not lower < next_height < upper:
                    break
                trial_height = next_height
            if best is not None:
                revision_page_plan.update({
                    "height_mm": best_height,
                    "min_height_mm": best_height,
                    "max_height_mm": best_height,
                })
                return await publish_template(
                    best[0], revision_page_plan,
                    inspection=best[1].get("_accepted_inspection"),
                )
            revision_page_plan.clear()
            revision_page_plan.update(previous_plan)

    try:
        await runtime_io.progress(progress_callback, "poster.revise", 0.18)
        selected_author_role = request_normalization.author_model_role(
            {
                "author_model_role": input_data.get(
                    "author_model_role",
                    state["author_model_role"],
                )
            }
        )
        author_role = (
            "main" if revision_mode == "content-replan" else selected_author_role
        )
        llm = model_runtime.require_author_llm(ctx, author_role)
        timeout_seconds, transport_retries = (
            request_normalization.authoring_transport_options(input_data)
        )
        (
            revised_template,
            _revision_report,
            accepted_inspection,
        ) = await model_runtime.request_html_revision(
            llm,
            source_html=editor_html,
            system=system,
            user=user,
            initial_feedback=initial_feedback,
            validate=validate,
            inspect=inspect_candidate,
            initial_validation=source_diagnostics,
            initial_inspection=initial_inspection,
            required_copy_targets=(
                content_replan_targets
                if visual_review_path and revision_mode == "content-replan"
                else None
            ),
            required_visual_operation=required_visual_operation,
            compact_layout=compact_layout,
            include_rendered_image=bool(
                getattr(llm, "supports_image_input", False)
            ),
            timeout_seconds=timeout_seconds,
            max_transport_retries=transport_retries,
            deadline=deadline,
        )
    except model_runtime.ModelBoundaryError as exc:
        accepted_source = initial_inspection.get("_accepted_inspection")
        failure = await publish_template(
            editor_html,
            revision_page_plan,
            inspection=(
                accepted_source if isinstance(accepted_source, dict) else initial_inspection
            ),
        )
        failure["author_model_role"] = author_role
        failure["editing_issue"] = {
            "code": exc.code,
            "summary": (
                "Editing stopped before a changed candidate was ready; the current "
                "poster version remains selected."
            ),
        }
        outcomes.append_warning_once(
            failure,
            f"{failure['editing_issue']['summary']} ({exc})",
        )
        if diagnostic_candidate is not None:
            failure["diagnostic_candidate"] = dict(diagnostic_candidate)
            failure["retry_from_checkpoint"] = bool(
                diagnostic_candidate.get("revision_state_persisted")
            )
        return failure
    result = await publish_template(
        revised_template,
        revision_page_plan,
        inspection=accepted_inspection,
    )
    result["author_model_role"] = author_role
    return result


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
