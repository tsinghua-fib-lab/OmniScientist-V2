"""Resumable planning, authoring, and rendering pipeline for poster drafts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from posterlib.content import html_contract, planning
from posterlib.generation import authoring, model_runtime
from posterlib.runtime import runtime_io
from posterlib.sources import source_runtime
from posterlib.visual import (
    reference_generation,
    reference_seeds,
    venue_branding,
    visual_design,
)

from . import draft_checkpoint, runtime_budget

PublishVersion = Callable[..., Awaitable[dict[str, Any]]]


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
    transport_options: Callable[[dict[str, Any]], tuple[float, int]],
    host_llm: Callable[[Any], Any],
    publish_version: PublishVersion,
    visual_design_client: Any | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run one draft stage, or the whole pipeline outside a durable task."""
    workspace = runtime_io.create_workspace(input_data, ctx)
    persisted = _has_persisted_identity(ctx)
    checkpoint = draft_checkpoint.load(workspace) if persisted else None

    if checkpoint is None:
        state, assets = await _plan_stage(
            input_data,
            progress_callback,
            ctx=ctx,
            workspace=workspace,
            max_source_chars=max_source_chars,
            host_llm=host_llm,
            deadline=deadline,
        )
        if persisted:
            draft_checkpoint.save(workspace, stage="plan-ready", state=state)
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

    stage = (
        str(checkpoint.get("stage") or "") if checkpoint is not None else "plan-ready"
    )
    if stage == "plan-ready":
        design_reference = await _reference_stage(
            state,
            workspace=workspace,
            deadline=deadline,
        )
        state["design_reference"] = design_reference.to_dict()
        if design_reference.warning:
            _append_warning_once(state["warnings"], design_reference.warning)
        if persisted:
            draft_checkpoint.save(workspace, stage="reference-ready", state=state)
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
        design_reference = reference_seeds.ReferenceBundle.from_dict(
            dict(state["design_reference"])
        )

    if stage == "reference-ready":
        design_plan = await _visual_design_stage(
            state,
            reference=design_reference,
            client=visual_design_client,
            deadline=deadline,
        )
        if persisted:
            draft_checkpoint.save(workspace, stage="design-ready", state=state)
            await runtime_io.progress(
                progress_callback,
                "poster.design-ready",
                0.28,
                checkpoint="design-ready",
                planner=design_plan.planner,
                topology=design_plan.topology,
            )
        stage = "design-ready"
    else:
        design_plan = visual_design.VisualDesignPlan.from_dict(
            dict(state["visual_design"])
        )

    timeout_seconds, transport_retries = transport_options(input_data)
    expected_figures = set(state["source_figure_sha256s"])
    page_plan = dict(state["page_plan"])
    source_text = str(state["source_text"])
    budget = dict(state["content_budget"])
    paper_identity = dict(state["paper_source"])
    llm = host_llm(ctx)

    if stage in {"design-ready", "author-repair-ready"}:
        authoring_deadline = (
            runtime_budget.draft_authoring_deadline(deadline)
            if deadline is not None
            else None
        )
        initial_candidate = (
            str(checkpoint.get("html_template") or "")
            if stage == "author-repair-ready" and checkpoint is not None
            else None
        )
        completed_repair_attempts = (
            int(checkpoint.get("author_repair_attempt") or 0)
            if stage == "author-repair-ready" and checkpoint is not None
            else 0
        )

        async def save_invalid_candidate(
            candidate: str,
            _issues: list[dict[str, Any]],
            attempt: int,
        ) -> None:
            state["html_template"] = candidate
            state["author_repair_attempt"] = attempt
            draft_checkpoint.save(
                workspace,
                stage="author-repair-ready",
                state=state,
            )

        html_template = await _author_stage(
            progress_callback,
            llm=llm,
            source_text=source_text,
            assets=assets,
            expected_figures=expected_figures,
            budget=budget,
            page_plan=page_plan,
            paper_source=dict(state["paper_source"]),
            authoring_request=str(state["authoring_request"]),
            design_reference=design_reference,
            visual_design_plan=design_plan,
            timeout_seconds=timeout_seconds,
            transport_retries=transport_retries,
            initial_candidate=initial_candidate,
            completed_repair_attempts=completed_repair_attempts,
            on_invalid_candidate=save_invalid_candidate if persisted else None,
            deadline=authoring_deadline,
        )
        if persisted:
            state["html_template"] = html_template
            state.pop("author_repair_attempt", None)
            draft_checkpoint.save(workspace, stage="author-ready", state=state)
            await runtime_io.progress(
                progress_callback,
                "poster.author-ready",
                0.44,
                checkpoint="author-ready",
            )
    else:
        html_template = str(checkpoint["html_template"])
        _require_valid_html(
            html_template,
            source_text=source_text,
            assets=assets,
            expected_figures=expected_figures,
            page_plan=page_plan,
            paper_identity=paper_identity,
        )

    html_text = html_contract.embed_assets(html_template, assets)
    publish_input = {**input_data, "visual_iteration": int(state["visual_iteration"])}
    result = await publish_version(
        html_text=html_text,
        source_text=source_text,
        input_data=publish_input,
        progress_callback=progress_callback,
        workspace=workspace,
        parent_html_sha256=None,
        live_html_path=None,
        asset_warnings=list(state["warnings"]),
        inspection=None,
        source_figure_sha256s=expected_figures,
        page_plan=page_plan,
        content_budget=budget,
        design_reference=design_reference,
        visual_design_plan=design_plan,
        visual_preferences=dict(state["visual_preferences"]),
        deadline=deadline,
    )
    result["paper_source"] = dict(state["paper_source"])
    return result


async def _plan_stage(
    input_data: dict[str, Any],
    progress_callback: Any,
    *,
    ctx: Any,
    workspace: Path,
    max_source_chars: int,
    host_llm: Callable[[Any], Any],
    deadline: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = await source_runtime.prepare_draft_source(
        input_data,
        ctx=ctx,
        workspace=workspace,
        progress_callback=progress_callback,
    )
    _validate_source_size(source.text, max_source_chars)
    asset_inputs = [
        *source.assets,
        *[
            source_runtime.normalize_user_asset(item)
            for item in source_runtime.normalize_asset_inputs(input_data.get("assets"))
        ],
    ]
    try:
        automatic_venue = venue_branding.resolve_verified_identity(
            input_data.get("venue_identity")
        ) or _resolve_automatic_venue(
            input_data,
            authoring_request=source.authoring_request,
        )
    except venue_branding.VenueBrandingError as exc:
        raise DraftPipelineError("invalid_payload", str(exc)) from exc
    venue_asset_input = _venue_brand_asset_input(automatic_venue)
    if venue_asset_input is not None:
        asset_inputs.append(venue_asset_input)
    assets, asset_warnings = await source_runtime.prepare_assets(asset_inputs, ctx)
    expected_figures = html_contract.source_figure_sha256s(assets)
    supplied_budget = input_data.get("content_budget")
    if supplied_budget is not None:
        budget = planning.normalize_content_budget(
            supplied_budget,
            source_text=source.text,
            source_figure_sha256s=expected_figures,
            source_figure_numbers=planning.source_figure_number_bindings(assets),
        )
    else:
        await runtime_io.progress(progress_callback, "poster.plan-content", 0.10)
        budget = await model_runtime.request_evidence_budget(
            host_llm(ctx),
            source_text=source.text,
            assets=assets,
            source_figure_sha256s=expected_figures,
            authoring_request=source.authoring_request,
            page=input_data.get("page"),
            capacity_hint=planning.content_capacity_hint(
                input_data.get("page"),
                orientation=str(input_data.get("orientation") or "auto"),
            ),
            orientation=str(input_data.get("orientation") or "auto"),
            deadline=deadline,
        )
    page_plan = planning.estimate_page(
        budget,
        page=input_data.get("page"),
        orientation=str(input_data.get("orientation") or "auto"),
    ).to_dict()
    state = {
        "source_text": source.text,
        "authoring_request": source.authoring_request,
        "asset_inputs": asset_inputs,
        "asset_sha256s": sorted(str(item["content_sha256"]) for item in assets),
        "source_figure_sha256s": sorted(expected_figures),
        "warnings": [*source.warnings, *asset_warnings],
        "paper_source": _bind_paper_identity(
            source.summary,
            input_data,
            assets,
            automatic_venue=automatic_venue,
        ),
        "content_budget": budget,
        "page_plan": page_plan,
        "visual_preferences": visual_design.normalize_preferences(
            input_data.get("visual_preferences")
        ),
        "visual_iteration": 0,
    }
    return state, assets


async def _restore_state(
    checkpoint: dict[str, Any],
    *,
    ctx: Any,
    max_source_chars: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = dict(checkpoint)
    source_text = str(state["source_text"])
    _validate_source_size(source_text, max_source_chars)
    assets = await prepare_checkpoint_assets(state, ctx)
    expected_figures = html_contract.source_figure_sha256s(assets)
    budget = planning.normalize_content_budget(
        state["content_budget"],
        source_text=source_text,
        source_figure_sha256s=expected_figures,
        source_figure_numbers=planning.source_figure_number_bindings(assets),
    )
    state["content_budget"] = budget
    return state, assets


async def prepare_checkpoint_assets(
    state: Mapping[str, Any], ctx: Any
) -> list[dict[str, Any]]:
    """Rebuild the original token-to-byte manifest and reject changed assets."""

    assets, _warnings = await source_runtime.prepare_assets(state["asset_inputs"], ctx)
    asset_sha256s = sorted(str(item["content_sha256"]) for item in assets)
    expected_figures = html_contract.source_figure_sha256s(assets)
    planned_asset_sha256s = [str(item) for item in state["asset_sha256s"]]
    planned_source_figures = [str(item) for item in state["source_figure_sha256s"]]
    if (
        asset_sha256s != planned_asset_sha256s
        or sorted(expected_figures) != planned_source_figures
    ):
        missing_assets = sorted(set(planned_asset_sha256s) - set(asset_sha256s))
        unexpected_assets = sorted(set(asset_sha256s) - set(planned_asset_sha256s))
        missing_figures = sorted(set(planned_source_figures) - expected_figures)
        unexpected_figures = sorted(expected_figures - set(planned_source_figures))
        raise DraftPipelineError(
            "source_read_failed",
            "Prepared poster assets changed after the plan checkpoint: "
            + json.dumps(
                {
                    "missing_assets": missing_assets,
                    "unexpected_assets": unexpected_assets,
                    "missing_source_figures": missing_figures,
                    "unexpected_source_figures": unexpected_figures,
                },
                sort_keys=True,
            ),
        )
    return assets


async def _reference_stage(
    state: Mapping[str, Any],
    *,
    workspace: Path,
    deadline: float | None,
) -> reference_seeds.ReferenceBundle:
    """Resolve one content-free reference after planning and before authoring."""

    signals = reference_signals(
        dict(state["content_budget"]),
        dict(state["page_plan"]),
    )
    seed = reference_seeds.select_seed(**signals["selection"])
    prompt = reference_generation_prompt(signals)
    generation_budget_s: float | None = None
    if deadline is not None:
        remaining = deadline - asyncio.get_running_loop().time()
        generation_budget_s = max(
            0.0,
            min(
                90.0,
                remaining - runtime_budget.POST_REFERENCE_RESERVE_SECONDS,
            ),
        )
    return await asyncio.to_thread(
        reference_generation.resolve_reference,
        seed,
        prompt=prompt,
        output_dir=workspace / "design-reference",
        density=str(signals["selection"]["density"]),
        generation_budget_s=generation_budget_s,
    )


async def _visual_design_stage(
    state: dict[str, Any],
    *,
    reference: reference_seeds.ReferenceBundle,
    client: Any | None,
    deadline: float | None = None,
) -> visual_design.VisualDesignPlan:
    """Plan visual grammar from exact reference pixels before authoring."""

    preflight_deadline: float | None = None
    if deadline is not None and client is not None:
        now = asyncio.get_running_loop().time()
        preflight_deadline = runtime_budget.reference_preflight_deadline(deadline, now)
    plan = await visual_design.plan_visual_design(
        reference,
        content_budget=dict(state["content_budget"]),
        page_plan=dict(state["page_plan"]),
        client=client,
        preferences=dict(state["visual_preferences"]),
        authoring_request=str(state["authoring_request"]),
        deadline=preflight_deadline,
    )
    state["visual_design"] = plan.to_dict()
    if plan.warning:
        _append_warning_once(state["warnings"], plan.warning)
    return plan


def reference_signals(
    content_budget: Mapping[str, Any],
    page_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive deterministic design-only signals without copying paper content."""

    raw_modules = content_budget.get("content_modules")
    modules = (
        [item for item in raw_modules if isinstance(item, Mapping)]
        if isinstance(raw_modules, list)
        else []
    )
    figure_hashes: set[str] = set()
    role_weights: dict[str, float] = {}
    aspect_ratios: list[float] = []
    has_equations = False
    has_tables = False
    priority_weights = {
        "focal": 3.0,
        "primary": 2.0,
        "supporting": 1.0,
        "footer": 0.5,
    }
    for module in modules:
        raw_figures = module.get("figure_sha256s")
        module_figures = (
            [str(value) for value in raw_figures if str(value)]
            if isinstance(raw_figures, list)
            else []
        )
        figure_hashes.update(module_figures)
        weight = priority_weights.get(str(module.get("priority") or "").lower(), 1.0)
        raw_roles = module.get("semantic_roles")
        if isinstance(raw_roles, list):
            for role in raw_roles:
                key = str(role).strip().lower()
                if key:
                    role_weights[key] = role_weights.get(key, 0.0) + weight
        equations = module.get("equations")
        has_equations |= isinstance(equations, list) and bool(equations)
        visual_kind = str(module.get("visual_kind") or "").strip().lower()
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
    organization_mode = (
        str(content_budget.get("organization_mode") or "scan-first").strip().lower()
    )
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
        "figure_aspect_ratios": sorted(aspect_ratios),
        "page_aspect_ratio": (
            round(width / height, 3) if width > 0 and height > 0 else 1.0
        ),
    }


def reference_generation_prompt(signals: Mapping[str, Any]) -> str:
    """Build a content-free poster mockup prompt from layout signals only."""

    selection = dict(signals["selection"])
    safe_shape = {
        "orientation": selection["orientation"],
        "density": reference_seeds.normalize_density(str(selection["density"])),
        "organization_mode": selection["organization_mode"],
        "module_count": int(signals["module_count"]),
        "section_count": int(signals["section_count"]),
        "figure_count": int(selection["figure_count"]),
        "figure_aspect_ratios": list(signals["figure_aspect_ratios"]),
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


async def _author_stage(
    progress_callback: Any,
    *,
    llm: Any,
    source_text: str,
    assets: list[dict[str, Any]],
    expected_figures: set[str],
    budget: dict[str, Any],
    page_plan: dict[str, Any],
    paper_source: dict[str, Any],
    authoring_request: str,
    design_reference: reference_seeds.ReferenceBundle,
    visual_design_plan: visual_design.VisualDesignPlan,
    timeout_seconds: float,
    transport_retries: int,
    initial_candidate: str | None = None,
    completed_repair_attempts: int = 0,
    on_invalid_candidate: Callable[[str, list[dict[str, Any]], int], Awaitable[None]]
    | None = None,
    deadline: float | None = None,
) -> str:
    expected_page = {
        "width_mm": float(page_plan["width_mm"]),
        "height_mm": float(page_plan["height_mm"]),
    }
    system, user = authoring.draft_prompt(
        assets=assets,
        content_budget=budget,
        page_plan=page_plan,
        authoring_request=authoring_request,
        paper_source=paper_source,
        design_reference=design_reference,
        visual_design_plan=visual_design_plan,
    )
    await runtime_io.progress(progress_callback, "poster.author", 0.18)

    async def report_repair(attempt: int, maximum: int) -> None:
        await runtime_io.progress(
            progress_callback,
            "poster.author-repair",
            0.18 + (0.20 * attempt / max(1, maximum)),
            attempt=attempt,
            maximum=maximum,
        )

    html_template = await model_runtime.request_html(
        llm,
        system=system,
        user=user,
        repair_system=authoring.html_repair_system(),
        repair_context=authoring.repair_manifest(
            assets=assets,
            content_budget=budget,
            page_plan=page_plan,
            paper_source=paper_source,
        ),
        validate=lambda candidate: html_contract.validate_candidate(
            candidate,
            source_text=source_text,
            assets=assets,
            required_source_figure_sha256s=expected_figures,
            expected_page=expected_page,
            content_contract=page_plan.get("content_contract"),
            paper_identity=paper_source,
        ),
        canonicalize=lambda candidate: html_contract.bind_authored_contract(
            candidate,
            content_budget=budget,
            page_plan=page_plan,
            paper_identity=paper_source,
            assets=assets,
        ),
        initial_candidate=initial_candidate,
        completed_repair_attempts=completed_repair_attempts,
        initial_temperature=0.0,
        timeout_seconds=timeout_seconds,
        max_transport_retries=transport_retries,
        on_repair_attempt=report_repair,
        on_invalid_candidate=on_invalid_candidate,
        deadline=deadline,
        minimum_repair_budget_seconds=(
            runtime_budget.MIN_FULL_HTML_REPAIR_BUDGET_SECONDS
            if on_invalid_candidate is not None
            else 0.0
        ),
    )
    return html_template


def _require_valid_html(
    html_template: str,
    *,
    source_text: str,
    assets: list[dict[str, Any]],
    expected_figures: set[str],
    page_plan: dict[str, Any],
    paper_identity: dict[str, Any],
) -> None:
    report = html_contract.validate_candidate(
        html_template,
        source_text=source_text,
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
    input_data: Mapping[str, Any],
    assets: list[dict[str, Any]],
    *,
    automatic_venue: venue_branding.VenueBranding | None = None,
) -> dict[str, Any]:
    """Bind explicit or safely resolved venue identity to its local logo asset."""

    identity = dict(source_summary)
    raw_venue = input_data.get("venue_identity")
    if raw_venue is None:
        if automatic_venue is None:
            return identity
        venue = {
            "label": automatic_venue.label,
            "evidence_uri": automatic_venue.evidence_uri,
        }
        if automatic_venue.distinction:
            venue["distinction"] = automatic_venue.distinction
        logo_digest = automatic_venue.logo_sha256
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
    if not isinstance(raw_venue, Mapping):
        raise DraftPipelineError(
            "invalid_payload",
            "venue_identity must be an object supplied by a verifying harness",
        )
    label = " ".join(str(raw_venue.get("label") or "").split())
    evidence_uri = str(raw_venue.get("evidence_uri") or "").strip()
    if not label or not evidence_uri:
        raise DraftPipelineError(
            "invalid_payload",
            "venue_identity requires label and evidence_uri",
        )
    venue = {"label": label, "evidence_uri": evidence_uri}
    venue_id = str(raw_venue.get("venue_id") or "").strip().lower()
    if venue_id:
        venue["venue_id"] = venue_id
    distinction = " ".join(str(raw_venue.get("distinction") or "").split())
    if distinction:
        venue["distinction"] = distinction
    logo_digest = str(raw_venue.get("logo_asset_sha256") or "").strip().lower()
    if logo_digest:
        logo_asset = next(
            (
                item
                for item in assets
                if item.get("source_kind") == "user_asset"
                and str(item.get("content_sha256") or "").lower() == logo_digest
            ),
            None,
        )
        if logo_asset is None:
            raise DraftPipelineError(
                "invalid_payload",
                "venue_identity logo_asset_sha256 must match a prepared user asset",
            )
        venue["logo_asset_sha256"] = logo_digest
        venue["logo_asset_token"] = str(logo_asset["token"])
    elif automatic_venue is not None and automatic_venue.logo_sha256:
        logo_asset = next(
            (
                item
                for item in assets
                if item.get("source_kind") == "venue_brand_asset"
                and str(item.get("content_sha256") or "").lower()
                == automatic_venue.logo_sha256
            ),
            None,
        )
        if logo_asset is None:
            raise DraftPipelineError(
                "source_read_failed",
                "bundled venue logo is missing from the prepared asset manifest",
            )
        venue["logo_asset_sha256"] = automatic_venue.logo_sha256
        venue["logo_asset_token"] = str(logo_asset["token"])
    identity["venue_identity"] = venue
    return identity


def _resolve_automatic_venue(
    input_data: Mapping[str, Any],
    *,
    authoring_request: str,
) -> venue_branding.VenueBranding | None:
    """Resolve one explicit venue without treating design direction as evidence."""

    if input_data.get("venue_identity") is not None:
        return None
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
    return venue_branding.resolve_venue_branding("\n".join(targets))


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
