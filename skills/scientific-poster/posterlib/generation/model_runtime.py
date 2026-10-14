"""Model calls and stateful HTML editing for scientific-poster generation."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from lxml import etree
from lxml import html as lxml_html
from lxml.html import defs as html_defs

from posterlib.content import planning
from posterlib.visual import vlm_client

from . import authoring

MAX_AUTHORING_TIMEOUT_SECONDS = 900.0
DEFAULT_AUTHORING_TIMEOUT_SECONDS = MAX_AUTHORING_TIMEOUT_SECONDS
DEFAULT_AUTHORING_TRANSPORT_RETRIES = 0
MAX_AUTHORING_TRANSPORT_RETRIES = 2
_STREAM_TEXT_MAX_TOKENS = 65536
_LAYOUT_REFLOW_TOOLS = frozenset(
    {
        "replace_poster_layout",
        "replace_poster_stylesheet",
        "move_poster_elements",
    }
)
_CONTENT_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_content_plan",
        "description": (
            "Submit the one complete grounded ContentBudget for the current "
            "scientific planning stage."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "object",
                    "properties": {
                        "page_orientation": {
                            "type": "string",
                            "enum": ["portrait", "landscape"],
                        },
                        "sections": {"type": "array", "items": {"type": "object"}},
                        "content_modules": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                    },
                    "required": ["page_orientation", "sections", "content_modules"],
                }
            },
            "required": ["plan"],
            "additionalProperties": False,
        },
    },
}
_REVISION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "apply_poster_edits",
            "description": (
                "Apply one or more exact, unique replacements to the current poster "
                "HTML. Use this "
                "repeatedly for local or multi-region edits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "old": {"type": "string"},
                                "new": {"type": "string"},
                            },
                            "required": ["old", "new"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["edits"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_poster_module_copy",
            "description": (
                "Update an existing module's authorized explanatory copy without matching "
                "HTML. Use only when the current content-replan instructions authorize that "
                "module. text replaces its existing body channel; detail_points replaces its "
                "existing ordinary list; flow_details replaces only the ordered method-step "
                "details by position while preserving their label association, order, equations, "
                "and bindings. Reuse every inline formula in that channel with its exact current "
                "LaTeX delimiters; the host rebinds it to the frozen MathML node."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "module_id": {"type": "string"},
                    "text": {"type": "string"},
                    "detail_points": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "flow_details": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                },
                "required": ["module_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_poster_elements",
            "description": (
                "Move existing HTML elements between existing parents without copying or "
                "retyping their scientific markup. Select each element and destination "
                "with a unique XPath. Optionally place it before an existing direct child "
                "of that parent; otherwise append. Moves apply in order. You choose all "
                "placements; this tool does not choose layout or change CSS."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "moves": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "element_xpath": {"type": "string"},
                                "parent_xpath": {"type": "string"},
                                "before_xpath": {"type": "string"},
                            },
                            "required": ["element_xpath", "parent_xpath"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["moves"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_poster_stylesheet",
            "description": (
                "Replace the complete poster stylesheet for a coherent visual reflow "
                "without retyping the body. Supply all CSS, including @page. Existing "
                "style elements are replaced; DOM content, images, equations, and "
                "inline style attributes are preserved. You choose every layout rule."
            ),
            "parameters": {
                "type": "object",
                "properties": {"css": {"type": "string"}},
                "required": ["css"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_poster_layout",
            "description": (
                "Atomically replace the inner layout of the existing poster root and the "
                "complete stylesheet. layout_html must not include html, head, body, style, "
                "or another data-poster-root. The host reinserts the exact current title band "
                "and scientific "
                "module nodes. Use data-poster-ref=\"title-band\" and "
                "data-poster-ref=\"module:<module-id>\" placeholders exactly once for "
                "every retained node. Place layout classes and attributes on a retained "
                "parent wrapper: each placeholder must be empty and have no other attributes "
                "because the host replaces that element. Example: <div class=\"grid\"><div "
                "class=\"masthead\"><span data-poster-ref=\"title-band\"></span></div>"
                "<section class=\"panel\"><span data-poster-ref=\"module:claim\"></span>"
                "</section></div>."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "layout_html": {"type": "string"},
                    "css": {"type": "string"},
                },
                "required": ["layout_html", "css"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restore_poster_checkpoint",
            "description": (
                "Restore the current working HTML to the original source or the most "
                "recent Chromium-valid candidate after an edit creates new failures."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "checkpoint": {
                        "type": "string",
                        "enum": ["source", "last_candidate"],
                    }
                },
                "required": ["checkpoint"],
                "additionalProperties": False,
            },
        },
    },
]


def _copy_update_tool(source_html: str, module_id: str) -> dict[str, Any]:
    """Expose only the structured copy fields present on one pending module."""

    base = next(
        tool
        for tool in _REVISION_TOOLS
        if tool["function"]["name"] == "update_poster_module_copy"
    )
    tool = copy.deepcopy(base)
    channels = authoring.copy_channel_inventory(source_html, [module_id]).get(
        module_id, {}
    )
    all_properties = base["function"]["parameters"]["properties"]
    properties: dict[str, Any] = {
        "module_id": {"type": "string", "enum": [module_id]},
    }
    for field in channels:
        properties[field] = copy.deepcopy(all_properties[field])
    flow = channels.get("flow_details")
    if isinstance(flow, Mapping):
        item_count = int(flow.get("required_item_count") or 0)
        if item_count > 0:
            properties["flow_details"]["minItems"] = item_count
            properties["flow_details"]["maxItems"] = item_count
    parameters = tool["function"]["parameters"]
    parameters["properties"] = properties
    parameters["required"] = ["module_id"]
    if len(channels) == 1:
        parameters["required"].append(next(iter(channels)))
    tool["function"]["description"] += (
        f" Current target: {module_id}. Available fields: "
        + ", ".join(channels)
        + "."
    )
    return tool

_TRANSIENT_MODEL_ERROR_MARKERS = (
    "connecterror",
    "connection aborted",
    "connection error",
    "connection reset",
    "end of file",
    "incomplete chunked read",
    "name resolution",
    "network is unreachable",
    "nodename nor servname",
    "peer closed connection",
    "remote protocol error",
    "server disconnected",
    "timeout",
    "unexpected eof",
)
_UNAVAILABLE_INSPECTION_OUTCOMES = frozenset(
    {
        "chromium_inspection_failed",
        "dom_evaluation_failed",
        "html_not_found",
        "inspection_failed",
        "inspection_output_failed",
        "inspection_unavailable",
        "invalid_inspection_options",
        "missing_capability",
        "screenshot_failed",
    }
)


class ModelBoundaryError(ValueError):
    """A host model response could not satisfy its output contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def require_host_llm(ctx: Any) -> Any:
    """Return the callable host LLM or raise the stable boundary error."""

    llm = getattr(ctx, "llm", None) if ctx is not None else None
    if llm is None or not callable(getattr(llm, "chat", None)):
        raise ModelBoundaryError(
            "llm_unavailable",
            "This host action requires an LLM supplied by the host runtime.",
        )
    return llm


def require_author_llm(ctx: Any, role: str) -> Any:
    """Return the configured authoring model for one canonical runtime role."""

    if role == "main":
        return require_host_llm(ctx)
    if role != "vision":
        raise ModelBoundaryError(
            "invalid_payload", "author_model_role must be main or vision"
        )
    vlm = getattr(ctx, "vlm", None) if ctx is not None else None
    if (
        vlm is None
        or not getattr(vlm, "available", False)
        or not callable(getattr(vlm, "chat", None))
    ):
        raise ModelBoundaryError(
            "vision_author_unavailable",
            "The configured VLM is not available for authoring.",
        )
    return vlm


async def request_content_plan(
    llm: Any,
    *,
    source_text: str,
    assets: list[dict[str, Any]],
    source_figure_sha256s: set[str],
    authoring_request: str,
    source_equations: list[dict[str, str]] | None = None,
    page: Any = None,
    orientation: str = "auto",
    draft_budget: dict[str, Any] | None = None,
    figure_observations: Mapping[str, Mapping[str, str]] | None = None,
    rendered_feedback: Mapping[str, Any] | None = None,
    max_repair_attempts: int | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Select or page-audit one source-grounded content plan."""

    if page is not None and not isinstance(page, Mapping):
        raise planning.PlanningError("invalid_page", "page must be an object")
    capacity_contract = planning.content_capacity_hint(page, orientation=orientation)
    prompt_builder = (
        authoring.content_plan_audit_prompt
        if draft_budget is not None
        else authoring.content_plan_prompt
    )
    figure_refs = {
        str(item.get("token") or ""): str(item.get("content_sha256") or "")
        for item in assets
        if str(item.get("token") or "")
        and str(item.get("content_sha256") or "") in source_figure_sha256s
    }
    prompt_options: dict[str, Any] = {}
    if draft_budget is not None:
        prompt_options["draft_budget"] = _content_plan_for_model(
            draft_budget,
            figure_refs=figure_refs,
        )
        prompt_options["figure_observations"] = {
            ref: dict(figure_observations[digest])
            for ref, digest in figure_refs.items()
            if figure_observations and digest in figure_observations
        }
        prompt_options["rendered_feedback"] = rendered_feedback
    system, original_request = prompt_builder(
        source_text=source_text,
        assets=assets,
        authoring_request=authoring_request,
        source_equations=source_equations,
        page=capacity_contract,
        **prompt_options,
    )
    boundary_label = (
        "Evidence plan audit" if draft_budget is not None else "Evidence planning"
    )
    loop = asyncio.get_running_loop()
    local_deadline = loop.time() + DEFAULT_AUTHORING_TIMEOUT_SECONDS
    deadline = min(deadline, local_deadline) if deadline is not None else local_deadline
    previous_candidate = ""
    last_error = "Evidence budget was not returned."
    request = original_request
    budget: dict[str, Any] | None = None
    seen_errors: set[str] = set()
    attempt = 0
    while True:
        try:
            response = await _request_content_plan_candidate(
                llm,
                system=system,
                user=request,
                timeout_seconds=None,
                max_transport_retries=1 if attempt == 0 else 0,
                boundary_label=(
                    boundary_label if attempt == 0 else f"{boundary_label} repair"
                ),
                deadline=deadline,
            )
            if isinstance(response, dict):
                parsed = response
                previous_candidate = json.dumps(
                    parsed,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )[:30000]
            else:
                previous_candidate = response.strip()[:30000]
                parsed = parse_content_plan_response(response)
            parsed = _bind_content_plan_figure_refs(
                parsed,
                figure_refs=figure_refs,
            )
            parsed = planning.bind_prepared_figure_geometry(parsed, assets=assets)
            candidate_budget = planning.normalize_content_budget(
                parsed,
                source_figure_sha256s=source_figure_sha256s,
                source_equations=source_equations,
            )
            if "page_orientation" not in candidate_budget:
                raise planning.PlanningError(
                    "invalid_content_budget",
                    "page_orientation must be portrait or landscape",
                )
        except (ModelBoundaryError, planning.PlanningError) as exc:
            if isinstance(exc, ModelBoundaryError) and exc.code != "candidate_validation_failed":
                raise
            if isinstance(exc, planning.PlanningError) and exc.code == "invalid_page":
                raise
            last_error = str(exc)
        else:
            budget = candidate_budget
            break
        if (
            last_error in seen_errors
            or (max_repair_attempts is not None and attempt >= max_repair_attempts)
            or loop.time() >= deadline
        ):
            break
        seen_errors.add(last_error)
        request = original_request + "\n\n" + _content_plan_correction_request(
            validation_error=last_error,
            previous_candidate=previous_candidate,
            binding_context=(
                "Allowed figure_refs:\n"
                + json.dumps(sorted(figure_refs), ensure_ascii=False)
            ),
        )
        attempt += 1
    if budget is None:
        raise ModelBoundaryError(
            "candidate_validation_failed",
            "Evidence plan remained invalid after validation feedback stopped "
            f"making progress or the deadline was reached: {last_error}",
        )
    return budget


async def _request_content_plan_candidate(
    llm: Any,
    *,
    system: str,
    user: str,
    timeout_seconds: float | None,
    max_transport_retries: int,
    boundary_label: str,
    deadline: float | None,
) -> dict[str, Any] | str:
    """Use one native submission tool when available; retain portable text fallback."""

    if not callable(getattr(llm, "chat_with_tools_stream", None)):
        return await _request_model_text(
            llm,
            system=system,
            user=user,
            temperature=0.0,
            timeout_seconds=timeout_seconds,
            max_transport_retries=max_transport_retries,
            boundary_label=boundary_label,
            deadline=deadline,
        )

    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": user + "\n\nSubmit the final object with submit_content_plan.",
        },
    ]
    last_error: ModelBoundaryError | None = None
    for tool_choice in ("required", "auto"):
        try:
            result = await _request_model_tools(
                llm,
                messages=messages,
                tools=[_CONTENT_PLAN_TOOL],
                tool_choice=tool_choice,
                temperature=0.0,
                timeout_seconds=(
                    timeout_seconds
                    if timeout_seconds is not None
                    else DEFAULT_AUTHORING_TIMEOUT_SECONDS
                ),
                max_transport_retries=max_transport_retries,
                boundary_label=boundary_label,
                deadline=deadline,
            )
        except ModelBoundaryError as exc:
            last_error = exc
            continue
        calls = list(getattr(result, "tool_calls", None) or [])
        if not calls:
            content = result if isinstance(result, str) else str(
                getattr(result, "content", "") or ""
            )
            if content.strip():
                try:
                    parsed_text = parse_content_plan_response(content)
                except ModelBoundaryError:
                    return content
                else:
                    if (
                        parsed_text.get("name") == "submit_content_plan"
                        and isinstance(parsed_text.get("arguments"), dict)
                        and isinstance(parsed_text["arguments"].get("plan"), dict)
                    ):
                        return dict(parsed_text["arguments"]["plan"])
                    return content
        if len(calls) != 1 or str(getattr(calls[0], "name", "") or "") != (
            "submit_content_plan"
        ):
            raise ModelBoundaryError(
                "candidate_validation_failed",
                "Evidence planning must call submit_content_plan exactly once.",
            )
        call = calls[0]
        argument_error = str(getattr(call, "arguments_error", "") or "")
        arguments = getattr(call, "arguments", None)
        if argument_error or not isinstance(arguments, dict) or not isinstance(
            arguments.get("plan"), dict
        ):
            raise ModelBoundaryError(
                "candidate_validation_failed",
                "submit_content_plan requires one valid plan object"
                + (f": {argument_error}" if argument_error else "."),
            )
        return dict(arguments["plan"])
    if last_error is not None:
        return await _request_model_text(
            llm,
            system=system,
            user=user,
            temperature=0.0,
            timeout_seconds=timeout_seconds,
            max_transport_retries=0,
            boundary_label=f"{boundary_label} text transport",
            deadline=deadline,
            prefer_stream=False,
        )
    raise ModelBoundaryError(
        "candidate_validation_failed",
        "Evidence planning did not return a usable content plan.",
    )


def _content_plan_for_model(
    budget: Mapping[str, Any],
    *,
    figure_refs: Mapping[str, str],
) -> dict[str, Any]:
    """Expose short prepared refs while internal budgets retain content hashes."""

    model_budget = copy.deepcopy(dict(budget))
    model_budget.pop("organization_mode", None)
    model_budget.pop("focal_role", None)
    hash_to_ref = {digest: ref for ref, digest in figure_refs.items()}
    modules = model_budget.get("content_modules")
    for module in modules if isinstance(modules, list) else []:
        if not isinstance(module, dict):
            continue
        digests = module.pop("figure_sha256s", [])
        module["figure_refs"] = [
            hash_to_ref[str(digest)]
            for digest in digests if str(digest) in hash_to_ref
        ]
    return model_budget


def _bind_content_plan_figure_refs(
    budget: dict[str, Any],
    *,
    figure_refs: Mapping[str, str],
) -> dict[str, Any]:
    """Bind model-selected prepared refs to their host-verified content hashes."""

    bound = copy.deepcopy(budget)
    modules = bound.get("content_modules")
    if not isinstance(modules, list):
        return bound
    for module in modules:
        if not isinstance(module, dict):
            continue
        if "figure_sha256s" in module:
            raise planning.PlanningError(
                "invalid_content_budget",
                "content modules must select prepared figures with figure_refs",
            )
        raw_refs = module.pop("figure_refs", [])
        if not isinstance(raw_refs, list) or any(
            not isinstance(ref, str) or ref not in figure_refs for ref in raw_refs
        ):
            raise planning.PlanningError(
                "invalid_content_budget",
                "module selects a figure_ref unavailable in this planning stage",
            )
        module["figure_sha256s"] = [figure_refs[ref] for ref in raw_refs]
    return bound


def _content_plan_correction_request(
    *,
    validation_error: str,
    previous_candidate: str,
    binding_context: str,
) -> str:
    """Continue the existing evidence plan instead of silently re-authoring it."""

    if previous_candidate:
        correction = (
            "Correct the reported planning issue with the smallest grounded edit. "
            "Preserve valid claims, qualifiers, provenance, and bindings; "
        )
    else:
        correction = (
            "The previous response did not provide a usable budget. Recreate the "
            "complete grounded budget; "
        )
    return (
        "CORRECTION TO THE PREVIOUS RESPONSE\n"
        "Return a corrected evidence-budget JSON object only. The previous response "
        f"failed validation. {correction}"
        "never add details merely to fill the page or invent content to satisfy the "
        "schema.\n\n"
        f"Validation error:\n{validation_error}\n\n"
        f"Host-verified binding context:\n{binding_context}\n\n"
        "Previous invalid budget, if available:\n"
        f"{previous_candidate or '(unavailable)'}"
    )


def _blocking_issues(check: Mapping[str, Any] | None) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Return deterministic blockers keyed by their measured owner."""

    if not isinstance(check, Mapping):
        return {}
    issues: list[Any] = []
    diagnostics = check.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        issues.extend(diagnostics.get("issues") or [])
    report = check.get("report")
    if isinstance(report, Mapping):
        issues.extend(
            item
            for item in report.get("warnings") or []
            if isinstance(item, Mapping) and item.get("severity") == "error"
        )
    blockers: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in issues:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        observed = item.get("observed")
        observed = observed if isinstance(observed, Mapping) else {}
        owner = str(
            item.get("poster_id")
            or item.get("module_id")
            or observed.get("module_a")
            or item.get("sha256")
            or "poster"
        )
        peer = str(observed.get("module_b") or item.get("role") or "")
        blockers[(str(item.get("code") or "unknown"), owner, peer)] = item
    return blockers


def _blocker_magnitudes(issue: Mapping[str, Any]) -> tuple[float, ...]:
    """Extract comparable geometry excess from one browser blocker."""

    observed = issue.get("observed")
    if not isinstance(observed, Mapping):
        return ()
    values: list[float] = []
    for key, value in sorted(observed.items()):
        if not any(token in str(key) for token in ("overflow_", "overlap_")):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        values.append(max(0.0, float(value)))
    return tuple(values)


def _layout_relations(check: Mapping[str, Any] | None) -> str:
    """Return the measured parent-layout signature for structural progress."""

    report = check.get("report") if isinstance(check, Mapping) else None
    relations = report.get("layout_relations") if isinstance(report, Mapping) else None
    return json.dumps(relations or [], ensure_ascii=True, sort_keys=True, default=str)


def _render_digest(inspection: Mapping[str, Any] | None) -> bytes | None:
    """Hash rendered pixels so formatting-only HTML cannot advance the editor."""

    if not isinstance(inspection, Mapping):
        return None
    path = Path(str(inspection.get("screenshot_path") or ""))
    try:
        return hashlib.sha256(path.read_bytes()).digest() if path.is_file() else None
    except OSError:
        return None


def _unoccupied_page_fraction(check: Mapping[str, Any] | None) -> float | None:
    """Measure outer trailing space and the browser's inter-module gaps."""

    report = check.get("report") if isinstance(check, Mapping) else None
    if not isinstance(report, Mapping):
        return None
    poster = report.get("poster")
    modules = report.get("modules")
    if not isinstance(poster, Mapping) or not isinstance(modules, list) or not modules:
        return None
    width = float(poster["width"])
    height = float(poster["height"])
    if width <= 0 or height <= 0:
        return None
    bottoms = [
        float(rect["top"]) + float(rect["height"])
        for module in modules if isinstance(module, Mapping)
        for rect in [module.get("rect")]
        if isinstance(rect, Mapping)
    ]
    if not bottoms:
        return None
    area = width * max(0.0, height - max(bottoms))
    for gap in report.get("adjacent_vertical_gaps") or []:
        rect = gap.get("rect") if isinstance(gap, Mapping) else None
        if isinstance(rect, Mapping):
            area += float(rect["width"]) * float(rect["height"])
    return area / (width * height)


def _proposal_advances(
    incumbent: Mapping[str, Any] | None,
    proposal: Mapping[str, Any],
) -> bool:
    """Accept only deterministic progress while an incumbent still has blockers."""

    if proposal.get("status") == "ok":
        return True
    if proposal.get("static_valid") is True and not _blocking_issues(proposal):
        # The browser may be unavailable even though the HTML contract passed.
        # Keep the changed, statically valid candidate deliverable; there is no
        # measured geometry on which to prefer the neutral carrier or prior edit.
        return True
    if not isinstance(incumbent, Mapping) or incumbent.get("status") == "ok":
        return False
    incumbent_issues = _blocking_issues(incumbent)
    proposal_issues = _blocking_issues(proposal)
    if not incumbent_issues or not proposal_issues:
        return False
    incumbent_keys = set(incumbent_issues)
    proposal_keys = set(proposal_issues)
    if proposal_keys - incumbent_keys:
        return False

    improved = proposal_keys < incumbent_keys
    for key in proposal_keys:
        before = _blocker_magnitudes(incumbent_issues[key])
        after = _blocker_magnitudes(proposal_issues[key])
        if not before or len(before) != len(after):
            continue
        if any(new > old for old, new in zip(before, after, strict=True)):
            return False
        improved |= any(new < old for old, new in zip(before, after, strict=True))
    return improved or _layout_relations(incumbent) != _layout_relations(proposal)


def _inspection_unavailable(check: Mapping[str, Any] | None) -> bool:
    """Return whether static HTML passed but Chromium produced no fixable geometry."""

    if (
        not isinstance(check, Mapping)
        or check.get("static_valid") is not True
        or _blocking_issues(check)
    ):
        return False
    outcome = check.get("outcome")
    code = str(outcome.get("code") or "") if isinstance(outcome, Mapping) else ""
    return check.get("status") != "ok" and code in _UNAVAILABLE_INSPECTION_OUTCOMES


def _ready_for_handoff(
    check: Mapping[str, Any] | None,
    *,
    allow_unavailable: bool,
) -> bool:
    """Return whether the candidate can leave editing for review or partial delivery."""

    return bool(
        isinstance(check, Mapping)
        and (
            check.get("status") == "ok"
            or (allow_unavailable and _inspection_unavailable(check))
        )
    )


def _composition_needs_repair(check: Mapping[str, Any] | None) -> bool:
    """Return whether rendered authoring feedback still exposes unreadable type."""

    if not isinstance(check, Mapping):
        return False
    report = check.get("report")
    report = report if isinstance(report, Mapping) else {}
    return any(
        isinstance(warning, Mapping)
        and warning.get("code") == "type_below_reading_target"
        for warning in report.get("warnings") or []
    )


async def request_html_revision(
    llm: Any,
    *,
    source_html: str,
    system: str,
    user: str,
    validate: Callable[[str], dict[str, Any]],
    initial_feedback: str = "",
    inspect: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    initial_validation: dict[str, Any] | None = None,
    initial_inspection: dict[str, Any] | None = None,
    required_copy_targets: frozenset[str] | set[str] | None = None,
    required_visual_operation: str = "",
    initial_composition: bool = False,
    compact_layout: bool = False,
    include_rendered_image: bool = False,
    timeout_seconds: float = DEFAULT_AUTHORING_TIMEOUT_SECONDS,
    max_transport_retries: int = DEFAULT_AUTHORING_TRANSPORT_RETRIES,
    deadline: float | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Run one stateful model/tool editing session over the current poster."""

    if required_visual_operation not in {"", "restyle", "reflow"}:
        raise ValueError(
            "required_visual_operation must be empty, restyle, or reflow"
        )

    loop = asyncio.get_running_loop()
    local_deadline = loop.time() + timeout_seconds
    deadline = min(deadline, local_deadline) if deadline is not None else local_deadline

    candidate = source_html
    last_diagnostics: dict[str, Any] = {
        "status": "error",
        "issues": [{"code": "revision_not_checked"}],
    }
    last_failed_check: dict[str, Any] | None = None
    current_check: dict[str, Any] | None = None
    last_candidate_check: dict[str, Any] | None = None
    last_candidate: tuple[
        str, dict[str, Any], dict[str, Any] | None
    ] | None = None
    handoff_candidate: tuple[
        str, dict[str, Any], dict[str, Any] | None
    ] | None = None
    finishable: tuple[str, dict[str, Any], dict[str, Any] | None] | None = None
    check_instruction = (
        "Each edit receives validation feedback and, when its content contract passes, "
        "browser measurements."
        if inspect is not None
        else "Each edit receives HTML and scientific-content validation. This session "
        "does not provide browser measurements."
    )
    editor_system = (
        f"{system}\n\nPOSTER LAYOUT EDITOR\n"
        "The current module placement, complete CSS, and measured browser state "
        "appear below. Build a new wrapper shell on the first turn; later turns "
        "include the attempted shell for repair. Call replace_poster_layout and read "
        "its result. The host reinserts the exact scientific nodes. "
        "Use only the listed available_poster_refs, each exactly once. A reference "
        "design cannot create new scientific nodes; omit unsupported objects. "
        "If a measured_grouping_suggestion is present, use it as a first "
        "intrinsic-height composition and adjust it from browser feedback. "
        "Work from the measured modules, then the latest attempted shell."
        "Match CSS selectors to the actual shell after scientific nodes are reinserted. "
        "If Chromium reports unreadable type or unmatched role selectors, repair the "
        "governing wrapper or stylesheet before handing off."
        if compact_layout else
        f"{system}\n\nPOSTER EDITOR\n"
        f"{check_instruction} Use one editor tool per turn, read the result, then edit or finish. "
        "Use exact replacements for local changes, update authorized module copy through "
        "its structured copy tool, replace the whole stylesheet for CSS changes, or "
        "replace the poster root's inner layout shell when wrappers/content "
        "organization must change. Never put another poster root, html/head/body, or style "
        "element inside that shell; supply CSS through the separate css argument. "
        "The host preserves title/module nodes while applying your shell and CSS. You choose "
        "the composition; the host applies edits and measures it. Read current module, "
        "content and figure bounds, adjacent vertical gaps, and parent layout. "
        "Solve the governing relationship, not "
        "just its padding. If an edit has no visible effect, use a selector that matches "
        "the current DOM or change the wrapper layout; do not add rules for absent elements. "
        "Give source figures a deliberate painted size within the page; stretching a wrapper "
        "without enlarging its painted image does not reclaim empty space. "
        "No-op edits or repeating a failed action on unchanged HTML cannot "
        "advance the task. Restore a checkpoint only to try a different composition. The latest "
        "HTML/check is authoritative; older warnings explain prior failures, not current facts. "
        "When an edit passes deterministic delivery validation, the host sends that "
        "candidate and its advisory browser warnings to the independent visual reviewer. "
        "Otherwise, read the result and continue editing while model time remains."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": editor_system},
        {"role": "user", "content": (
            f"Current rendered feedback:\n{initial_feedback}\n\n{user}"
            if initial_feedback else user
        )},
    ]
    recent_turns: list[tuple[str, list[dict[str, Any]]]] = []

    async def check_html(
        html_text: str,
        *,
        supplied_validation: dict[str, Any] | None = None,
        supplied_inspection: dict[str, Any] | None = None,
    ) -> tuple[
        tuple[str, dict[str, Any], dict[str, Any] | None] | None,
        dict[str, Any],
    ]:
        static_report = supplied_validation or validate(html_text)
        if static_report.get("status") != "ok":
            return None, {"status": "error", "diagnostics": static_report}
        validation_feedback = (
            {"validation_warnings": static_report["warnings"]}
            if static_report.get("warnings") else {}
        )
        if inspect is None:
            return (html_text, static_report, None), {
                "status": "ok",
                **validation_feedback,
            }
        rendered = supplied_inspection or await inspect(html_text)
        public = {
            **{key: value for key, value in rendered.items() if not key.startswith("_")},
            **validation_feedback,
            "static_valid": True,
        }
        accepted = rendered.get("_accepted_inspection")
        return (
            html_text,
            static_report,
            accepted if isinstance(accepted, dict) else rendered,
        ), public

    _initial_candidate, current_check = await check_html(
        candidate,
        supplied_validation=initial_validation,
        supplied_inspection=initial_inspection,
    )
    # The source is the comparison incumbent and delivery fallback. An unchanged
    # source does not count as a completed edit, but model failure must not erase it.
    finishable = None
    current_render = _initial_candidate[2] if _initial_candidate is not None else None
    source_check = current_check
    source_render = current_render
    if include_rendered_image:
        messages[1]["content"] = _editor_user_content(
            str(messages[1]["content"]), current_render
        )
    has_working_edit = False
    last_layout_attempt: dict[str, Any] | None = None
    if compact_layout:
        messages = _layout_editor_messages(
            editor_system=editor_system, user=user, candidate=candidate,
            check=current_check, result=None, attempt=None,
            current_render=current_render,
            include_rendered_image=include_rendered_image,
        )

    def delivery_candidate() -> tuple[
        str, dict[str, Any], dict[str, Any] | None
    ] | None:
        """Return the best renderable state even when editing stops early."""

        return handoff_candidate or last_candidate or _initial_candidate

    async def stage_candidate(updated: str) -> dict[str, Any]:
        nonlocal candidate, finishable, last_failed_check
        nonlocal current_check, last_candidate, last_candidate_check
        nonlocal current_render, has_working_edit
        if updated == candidate:
            return {
                "status": "unchanged",
                "code": "revision_noop",
                **({"check": current_check} if current_check is not None else {}),
                "message": (
                    "This batch leaves the current HTML unchanged; no contract check "
                    "or rendering was repeated. Choose a "
                    "different edit, or finish if the current composition is ready."
                ),
            }
        proposal, checked = await check_html(updated)
        if not initial_composition and _inspection_unavailable(checked):
            raise ModelBoundaryError(
                "inspection_unavailable",
                "The revised candidate could not be measured, so the checked published "
                "version remains selected.",
            )
        proposal_render = proposal[2] if proposal is not None else None
        incumbent_digest = _render_digest(current_render)
        proposal_digest = _render_digest(proposal_render)
        if (
            incumbent_digest is not None
            and proposal_digest is not None
            and proposal_digest == incumbent_digest
        ):
            proposed_report = checked.get("report")
            current_report = current_check.get("report") if current_check else None
            proposed_unmatched = (
                proposed_report.get("unmatched_style_selectors") or []
                if isinstance(proposed_report, Mapping) else []
            )
            current_unmatched = (
                current_report.get("unmatched_style_selectors") or []
                if isinstance(current_report, Mapping) else []
            )
            new_unmatched = sorted(set(proposed_unmatched) - set(current_unmatched))
            return {
                "status": "diagnostic",
                "code": "revision_no_visible_change",
                "message": (
                    "The proposal rendered exactly the same pixels. The current best "
                    "candidate remains available. Change a matching layout rule or the "
                    "wrapper arrangement if model time remains."
                ),
                **({"new_unmatched_selectors": new_unmatched} if new_unmatched else {}),
                "check": current_check,
            }
        space_feedback: dict[str, Any] = {}
        if compact_layout and checked.get("status") == "ok":
            before_void = _unoccupied_page_fraction(current_check)
            after_void = _unoccupied_page_fraction(checked)
            if (
                before_void is not None and after_void is not None
                and after_void > before_void + 1e-6
            ):
                space_feedback = {
                    "space_use_warning": (
                        "Empty page area increased; consider a different grouping "
                        "or larger painted type and figures."
                    ),
                    "unoccupied_page_fraction_before": round(before_void, 4),
                    "unoccupied_page_fraction_after": round(after_void, 4),
                }
        can_continue = (
            proposal is not None
            and (
                not has_working_edit
                or _proposal_advances(current_check, checked)
            )
        )
        if not can_continue:
            last_failed_check = checked
            return {
                "status": "diagnostic",
                "code": "candidate_not_improved",
                "message": (
                    "The proposal neither reduced a measured issue nor changed the "
                    "governing layout relationship, so the working draft is unchanged. "
                    "Use the proposal diagnostics for a different edit if model time remains."
                ),
                "proposal_check": checked,
                "check": current_check,
            }
        if last_candidate is not None:
            delivery_improved = _proposal_advances(last_candidate_check, checked)
        elif initial_composition:
            delivery_improved = (
                source_check.get("status") != "ok"
                or (
                    checked.get("status") == "ok"
                    and not _composition_needs_repair(checked)
                )
            )
        else:
            delivery_improved = _proposal_advances(source_check, checked)
        candidate = updated
        has_working_edit = True
        finishable = proposal
        current_check = checked
        current_render = proposal_render
        observation = {
            "status": "applied",
            "check": current_check,
            "delivery_checkpoint_updated": delivery_improved,
            **space_feedback,
        }
        if delivery_improved and finishable is not None:
            last_candidate = finishable
            last_candidate_check = current_check
        return observation

    native_transport = callable(getattr(llm, "chat_with_tools_stream", None))
    # Text-only editing uses native commands; image-bearing turns can return HTML.
    native_tool_choice = "auto" if include_rendered_image else "required"
    pending_copy_targets = set(required_copy_targets or ())
    pending_visual_operation = required_visual_operation
    attempted_edits: set[tuple[str, str]] = set()
    failed_layout_shells: set[str] = set()

    def repeated_edit(action: Any) -> bool:
        """Repeating the same edit on the same bytes cannot change its outcome."""

        key = (
            hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
            json.dumps(action, ensure_ascii=False, sort_keys=True, default=str),
        )
        if key in attempted_edits:
            return True
        attempted_edits.add(key)
        return False

    def available_editor_tools() -> list[dict[str, Any]]:
        """Expose only commands authorized by the current editing state."""

        if compact_layout:
            return [
                tool for tool in _REVISION_TOOLS
                if tool["function"]["name"] == "replace_poster_layout"
            ]
        if initial_composition and not has_working_edit:
            return [
                tool
                for tool in _REVISION_TOOLS
                if tool["function"]["name"] == "replace_poster_layout"
            ]
        if pending_copy_targets:
            return [_copy_update_tool(candidate, min(pending_copy_targets))]
        if pending_visual_operation:
            operation_tools = (
                _LAYOUT_REFLOW_TOOLS
                if pending_visual_operation == "reflow"
                else frozenset({"replace_poster_stylesheet"})
            )
            return [
                tool
                for tool in _REVISION_TOOLS
                if tool["function"]["name"] in operation_tools
            ]
        return _REVISION_TOOLS

    def refreshed_messages() -> list[dict[str, Any]]:
        if compact_layout:
            return _layout_editor_messages(
                editor_system=editor_system, user=user, candidate=candidate,
                check=current_check, result=last_diagnostics,
                attempt=last_layout_attempt, current_render=current_render,
                include_rendered_image=include_rendered_image,
            )
        return _compacted_revision_messages(
            editor_system=editor_system, original_user=user,
            source_html=source_html, candidate=candidate,
            latest_result=last_diagnostics, current_check=current_check,
            recent_turns=(None if include_rendered_image else recent_turns),
            current_render=current_render,
            include_rendered_image=include_rendered_image,
        )

    async def request_editor_turn(
        iteration: int,
        available_tools: list[dict[str, Any]],
    ) -> Any:
        async def request_native() -> Any:
            result = await _request_model_tools(
                llm, messages=messages, tools=available_tools,
                tool_choice=native_tool_choice,
                temperature=0.0, timeout_seconds=timeout_seconds,
                max_transport_retries=max_transport_retries,
                boundary_label="HTML revision editor", deadline=deadline,
            )
            if result and getattr(result, "tool_calls", None):
                return result
            if native_tool_choice == "auto" and result and (
                isinstance(result, str) or getattr(result, "content", "")
            ):
                return result
            raise ModelBoundaryError(
                "llm_error",
                "Native editor returned no usable response",
            )

        if native_transport:
            return await request_native()
        return await _request_text_editor_turn(
            llm, messages=messages, tools=available_tools,
            timeout_seconds=timeout_seconds,
            max_transport_retries=max_transport_retries,
            deadline=deadline, call_id=f"poster-text-{iteration}",
        )

    _iteration = 0
    while loop.time() < deadline:
        turn_index = _iteration
        _iteration += 1
        turn_source = candidate
        available_tools = available_editor_tools()
        available_tool_names = {
            str(tool["function"]["name"]) for tool in available_tools
        }
        try:
            result = await request_editor_turn(turn_index, available_tools)
        except ModelBoundaryError:
            fallback = delivery_candidate()
            if fallback is not None and (has_working_edit or initial_composition):
                return fallback
            raise
        if isinstance(result, str):
            content = result
            tool_calls: list[Any] = []
            finish_reason = ""
        else:
            content = str(getattr(result, "content", "") or "")
            tool_calls = list(getattr(result, "tool_calls", None) or [])
            finish_reason = str(getattr(result, "finish_reason", "") or "")
        if not tool_calls and finish_reason == "length":
            last_diagnostics = {
                "status": "error",
                "issues": [
                    {
                        "code": "revision_output_truncated",
                        "message": (
                            "The ordinary-text response hit the output cap and was "
                            "discarded; the checked working HTML is unchanged."
                        ),
                    }
                ],
            }
            recent_turns.clear()
            messages = refreshed_messages()
            continue
        if tool_calls:
            wire_calls = [
                {
                    "id": str(getattr(call, "id", "") or f"poster-{turn_index}-{index}"),
                    "type": "function",
                    "function": {
                        "name": str(getattr(call, "name", "") or ""),
                        "arguments": (
                            str(getattr(call, "raw_arguments", "") or "")
                            if getattr(call, "arguments_error", None)
                            else json.dumps(
                                getattr(call, "arguments", {}) or {}, ensure_ascii=False
                            )
                        ),
                    },
                }
                for index, call in enumerate(tool_calls)
            ]
            assistant_message: dict[str, Any] = {
                "role": "assistant", "content": content[:2000], "tool_calls": wire_calls,
            }
            reasoning_content = str(getattr(result, "reasoning_content", "") or "")
            if reasoning_content:
                assistant_message["reasoning_content"] = reasoning_content
            turn_messages = [assistant_message]
            for call, wire_call in zip(tool_calls, wire_calls, strict=False):
                name = str(getattr(call, "name", "") or "")
                arguments = getattr(call, "arguments", None)
                arguments = arguments if isinstance(arguments, dict) else {}
                if compact_layout and name == "replace_poster_layout":
                    last_layout_attempt = arguments
                argument_error = str(getattr(call, "arguments_error", "") or "")
                duplicate = repeated_edit((name, arguments, argument_error))
                if duplicate:
                    if compact_layout:
                        raise ModelBoundaryError(
                            "candidate_validation_failed",
                            "The layout editor repeated an action on unchanged HTML; "
                            "the current poster remains selected.",
                        )
                    observation = {
                        "status": "unchanged", "code": "editor_no_progress",
                        "message": (
                            "This exact action already ran on the same HTML. It was not "
                            "repeated. Use the current check to choose a different edit, "
                            "including the governing CSS when layout is blocked."
                        ),
                        "check": current_check,
                    }
                elif argument_error:
                    observation = {
                        "status": "error",
                        "code": "invalid_tool_arguments",
                        "message": argument_error,
                    }
                elif name not in available_tool_names:
                    observation = {
                        "status": "error",
                        "code": "editor_tool_not_available",
                        "message": (
                            f"{name or 'unnamed command'} is not authorized in the "
                            "current editing state. Use one of: "
                            + ", ".join(sorted(available_tool_names))
                        ),
                    }
                elif name in {
                    "apply_poster_edits", "update_poster_module_copy",
                    "replace_poster_stylesheet", "move_poster_elements", "replace_poster_layout",
                }:
                    layout_shell_hash = (
                        hashlib.sha256(
                            str(arguments.get("layout_html") or "").encode("utf-8")
                        ).hexdigest()
                        if compact_layout and name == "replace_poster_layout" else ""
                    )
                    if layout_shell_hash and layout_shell_hash in failed_layout_shells:
                        raise ModelBoundaryError(
                            "candidate_validation_failed",
                            "The same invalid layout shell was submitted again; "
                            "the current poster remains selected.",
                        )
                    try:
                        if name == "replace_poster_layout":
                            updated = replace_poster_layout(
                                candidate,
                                layout_html=str(arguments.get("layout_html") or ""),
                                css=str(arguments.get("css") or ""),
                            )
                        elif name == "update_poster_module_copy":
                            module_id = str(arguments.get("module_id") or "")
                            if (
                                pending_copy_targets
                                and module_id not in pending_copy_targets
                            ):
                                raise ModelBoundaryError(
                                    "invalid_module_copy_update",
                                    "update one of the remaining content-replan targets: "
                                    + ", ".join(sorted(pending_copy_targets)),
                                )
                            updated = update_poster_module_copy(
                                candidate,
                                module_id=module_id,
                                text=(
                                    str(arguments["text"])
                                    if "text" in arguments
                                    else None
                                ),
                                detail_points=arguments.get("detail_points"),
                                flow_details=arguments.get("flow_details"),
                            )
                        elif name == "replace_poster_stylesheet":
                            updated = replace_html_stylesheet(candidate, str(arguments.get("css") or ""))
                        elif name == "move_poster_elements":
                            updated = move_html_elements(candidate, arguments.get("moves"))
                        else:
                            updated = apply_html_edits(
                                candidate,
                                json.dumps(
                                    {"edits": arguments.get("edits")},
                                    ensure_ascii=False,
                                ),
                            )
                    except ModelBoundaryError as exc:
                        if layout_shell_hash and exc.code == "invalid_layout_shell":
                            failed_layout_shells.add(layout_shell_hash)
                        observation = {
                            "status": "error",
                            "code": exc.code,
                            "message": str(exc),
                        }
                    else:
                        observation = await stage_candidate(updated)
                        if (
                            name == "update_poster_module_copy"
                            and observation["status"] == "applied"
                        ):
                            pending_copy_targets.discard(
                                str(arguments.get("module_id") or "")
                            )
                        if observation["status"] == "applied" and (
                            (
                                pending_visual_operation == "restyle"
                                and name == "replace_poster_stylesheet"
                            )
                            or (
                                pending_visual_operation == "reflow"
                                and name in _LAYOUT_REFLOW_TOOLS
                            )
                        ):
                            pending_visual_operation = ""
                        if (
                            finishable is not None
                            and not pending_copy_targets
                            and not pending_visual_operation
                            and _ready_for_handoff(
                                current_check,
                                allow_unavailable=initial_composition,
                            )
                            and not (
                                initial_composition
                                and _composition_needs_repair(current_check)
                            )
                        ):
                            handoff_candidate = finishable
                elif name == "restore_poster_checkpoint":
                    checkpoint = str(arguments.get("checkpoint") or "")
                    if checkpoint == "source":
                        candidate = source_html
                        finishable = None
                        current_check = source_check
                        current_render = source_render
                        observation = {
                            "status": "restored",
                            "checkpoint": checkpoint,
                        }
                    elif checkpoint == "last_candidate":
                        if last_candidate is None:
                            observation = {
                                "status": "error",
                                "code": "candidate_checkpoint_unavailable",
                                "message": (
                                    "No Chromium-valid edited candidate has been "
                                    "recorded in this session."
                                ),
                            }
                        else:
                            candidate = last_candidate[0]
                            current_check = last_candidate_check
                            current_render = last_candidate[2]
                            finishable = last_candidate
                            observation = {
                                "status": "restored",
                                "checkpoint": checkpoint,
                            }
                    else:
                        observation = {
                            "status": "error",
                            "code": "invalid_checkpoint",
                            "message": "checkpoint must be source or last_candidate",
                        }
                else:
                    observation = {
                        "status": "error",
                        "code": "unknown_revision_tool",
                        "message": f"Unknown revision tool: {name}",
                    }
                if name in {
                    "replace_poster_layout", "replace_poster_stylesheet",
                    "update_poster_module_copy",
                    "move_poster_elements", "restore_poster_checkpoint",
                } and (
                    observation.get("status") in {"applied", "restored"}
                ):
                    # Restore can reach outside retained history; parser-backed
                    # operations can change bytes beyond the selected elements/CSS.
                    observation["current_html"] = candidate
                last_diagnostics = observation
                turn_messages.append({
                    "role": "tool",
                    "tool_call_id": wire_call["id"],
                    "content": json.dumps(observation, ensure_ascii=False, default=str),
                })
            if (
                handoff_candidate is not None
                and not pending_visual_operation
                and _ready_for_handoff(
                    current_check,
                    allow_unavailable=initial_composition,
                )
                and not (
                    initial_composition
                    and _composition_needs_repair(current_check)
                )
            ):
                return handoff_candidate
            recent_turns.append((turn_source, turn_messages))
            recent_turns = recent_turns[-3:]
            messages = refreshed_messages()
            continue

        duplicate = repeated_edit(("text", content))
        requires_initial_layout = initial_composition and not has_working_edit
        requires_restricted_tool = bool(pending_copy_targets) or bool(
            pending_visual_operation
        )
        updated = (
            None
            if duplicate or requires_initial_layout or requires_restricted_tool
            else _direct_revision_candidate(candidate, content)
        )
        recent_turns.clear()
        if duplicate:
            last_diagnostics = {
                "status": "unchanged",
                "code": "editor_no_progress",
                "message": (
                    "This exact response already ran on the same HTML. It was not "
                    "repeated. Use the current check to choose a different edit."
                ),
                "check": current_check,
            }
        elif requires_initial_layout:
            last_diagnostics = {
                "status": "error",
                "code": "initial_layout_required",
                "message": (
                    "The first composition action must use replace_poster_layout so "
                    "the layout shell and stylesheet are authored together."
                ),
                "check": current_check,
            }
        elif requires_restricted_tool:
            last_diagnostics = {
                "status": "error",
                "code": "editor_tool_required",
                "message": (
                    "The current edit boundary requires one authorized editor command: "
                    + ", ".join(sorted(available_tool_names))
                ),
                "check": current_check,
            }
        elif updated is None:
            last_diagnostics = {
                "status": "error", "issues": [_revision_output_issue(content)],
            }
        else:
            last_diagnostics = await stage_candidate(updated)
            if (
                finishable is not None
                and not pending_copy_targets
                and not pending_visual_operation
                and _ready_for_handoff(
                    current_check,
                    allow_unavailable=initial_composition,
                )
                and not (
                    initial_composition
                    and _composition_needs_repair(current_check)
                )
            ):
                handoff_candidate = finishable
                return handoff_candidate
        messages = refreshed_messages()

    fallback = delivery_candidate()
    if fallback is not None and (has_working_edit or initial_composition):
        return fallback
    failure_diagnostics = last_failed_check or {
        key: value for key, value in last_diagnostics.items() if key != "current_html"
    }
    raise ModelBoundaryError(
        "candidate_validation_failed",
        "Revision editor reached its execution deadline without a reviewable candidate: "
        f"{json.dumps(failure_diagnostics, ensure_ascii=False, default=str)}",
    )


def _layout_editor_messages(
    *,
    editor_system: str,
    user: str,
    candidate: str,
    check: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
    attempt: Mapping[str, Any] | None,
    current_render: Mapping[str, Any] | None,
    include_rendered_image: bool,
) -> list[dict[str, Any]]:
    """Show the layout editor only the shell, CSS, and measured state."""

    document = lxml_html.document_fromstring(candidate)
    root = document.xpath("//*[@data-poster-root]")[0]
    shell = copy.deepcopy(root)
    modules = []
    for module in root.xpath(".//*[@data-poster-module]"):
        headings = module.xpath(".//h2 | .//h3")
        placement = []
        parent = module.getparent()
        while parent is not None and parent is not root:
            classes = str(parent.get("class") or "").strip()
            if classes:
                placement.append(classes)
            parent = parent.getparent()
        modules.append({
            "id": module.get("data-poster-module"),
            "title": " ".join(headings[0].itertext()).strip() if headings else "",
            "kind": module.get("data-visual-kind"),
            "priority": module.get("data-module-priority"),
            "current_placement": list(reversed(placement)),
        })
    for node in [
        *shell.xpath(".//*[@data-poster-module]"),
        *shell.xpath(".//*[@data-poster-title-band]"),
    ]:
        reference = (
            f"module:{node.get('data-poster-module')}"
            if node.get("data-poster-module") is not None else "title-band"
        )
        marker = lxml_html.Element("span")
        marker.set("data-poster-ref", reference)
        marker.tail = node.tail
        node.getparent().replace(node, marker)
    layout_html = "".join(
        lxml_html.tostring(child, encoding="unicode", method="html")
        for child in shell
    )
    css = "\n".join(str(style.text or "") for style in document.xpath("//style"))

    def brief_check(value: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        report = value.get("report")
        report = report if isinstance(report, Mapping) else {}
        figures = report.get("source_figures")
        figures = figures if isinstance(figures, Mapping) else {}
        return {
            "status": value.get("status"),
            "outcome": value.get("outcome"),
            "warnings": report.get("warnings") or [],
            "unmatched_style_selectors": report.get("unmatched_style_selectors") or [],
            "diagnostics": value.get("diagnostics"),
            "poster": report.get("poster"),
            "unoccupied_page_fraction": _unoccupied_page_fraction(value),
            "modules": [
                {
                    "id": module.get("module_id"),
                    "rect": module.get("rect"),
                    "height_mm": round(
                        float(module["rect"]["height"])
                        * float(report["poster"]["height_mm"])
                        / float(report["poster"]["height"]), 1
                    ) if module.get("rect") and report.get("poster") else None,
                    "content_bottom_inset_px": module.get("content_bottom_inset_px"),
                }
                for module in report.get("modules") or []
                if isinstance(module, Mapping)
            ],
            "source_figures": [
                {
                    key: figure.get(key)
                    for key in (
                        "module_id", "sha256", "painted_width_mm",
                        "module_width_mm", "visible",
                    )
                }
                for figure in figures.get("details") or []
                if isinstance(figure, Mapping)
            ],
            "adjacent_vertical_gaps": report.get("adjacent_vertical_gaps") or [],
        }

    state: dict[str, Any] = {
        "current_css": css,
        "available_poster_refs": [
            "title-band", *(f"module:{module['id']}" for module in modules)
        ],
        "modules": modules,
        "current_check": brief_check(check),
    }
    if result is None and isinstance(check, Mapping):
        report = check.get("report")
        report = report if isinstance(report, Mapping) else {}
        wide_ids = {
            str(warning.get("module_id"))
            for warning in report.get("warnings") or []
            if isinstance(warning, Mapping)
            and warning.get("code") == "dominant_figure_underallocated"
        }
        remaining = [module["id"] for module in modules if module["id"] not in wide_ids]
        heights = {
            str(module.get("module_id")): float(module["rect"]["height"])
            for module in report.get("modules") or []
            if isinstance(module, Mapping) and isinstance(module.get("rect"), Mapping)
        }
        if len(remaining) > 1 and all(mid in heights for mid in remaining):
            split = min(
                range(1, len(remaining)),
                key=lambda index: max(
                    sum(heights[mid] for mid in remaining[:index]),
                    sum(heights[mid] for mid in remaining[index:]),
                ),
            )
            state["measured_grouping_suggestion"] = {
                "column_1": remaining[:split],
                "column_2": remaining[split:],
                "full_width": [module["id"] for module in modules if module["id"] in wide_ids],
            }
    if result is not None:
        state["current_layout_html"] = layout_html
        state["last_edit_result"] = {
            key: result.get(key)
            for key in (
                "status", "code", "message", "new_unmatched_selectors",
                "space_use_warning",
                "unoccupied_page_fraction_before", "unoccupied_page_fraction_after",
            )
            if result.get(key) is not None
        }
        if isinstance(result.get("proposal_check"), Mapping):
            state["last_edit_result"]["proposal_check"] = brief_check(
                result["proposal_check"]
            )
        if attempt is not None and result.get("status") != "applied":
            state["last_attempt"] = dict(attempt)
    content = user + "\n\nCURRENT LAYOUT AND MEASURED STATE\n" + json.dumps(
        state, ensure_ascii=False,
    )
    return [
        {"role": "system", "content": editor_system},
        {
            "role": "user",
            "content": _editor_user_content(content, current_render)
            if include_rendered_image else content,
        },
    ]


def _compacted_revision_messages(
    *,
    editor_system: str,
    original_user: str,
    source_html: str,
    candidate: str,
    latest_result: Mapping[str, Any],
    current_check: Mapping[str, Any] | None = None,
    recent_turns: list[tuple[str, list[dict[str, Any]]]] | None = None,
    current_render: Mapping[str, Any] | None = None,
    include_rendered_image: bool = False,
) -> list[dict[str, Any]]:
    """Keep a coherent checkpoint followed by native call/result exchanges."""

    checkpoint = recent_turns[0][0] if recent_turns else candidate
    if source_html in original_user:
        current_user = original_user.replace(source_html, checkpoint, 1)
    else:
        current_user = (
            f"{original_user}\n\nCurrent complete HTML scientific-content carrier:\n"
            f"{checkpoint}"
        )
    if recent_turns:
        exchanges = [dict(message) for _source, turn in recent_turns for message in turn]
        for index, message in enumerate(exchanges):
            if message["role"] != "tool":
                continue
            result = json.loads(message["content"])
            if index == len(exchanges) - 1:
                # Only the latest state is authoritative for exact subsequent
                # edits, including parser serialization and restored checkpoints.
                result["current_html"] = candidate
                if current_check is not None:
                    result.setdefault("check", dict(current_check))
            else:
                result.pop("current_html", None)
                check = result.get("check")
                if isinstance(check, dict):
                    check.pop("observations", None)
                    check.pop("source_figure_changes", None)
                    report = check.get("report")
                    if isinstance(report, dict):
                        check["report"] = {
                            key: report[key]
                            for key in ("warnings", "unmatched_style_selectors")
                            if key in report
                        }
                    diagnostics = check.get("diagnostics")
                    if isinstance(diagnostics, dict):
                        check["diagnostics"] = {
                            key: diagnostics[key]
                            for key in ("status", "issues", "warnings")
                            if key in diagnostics
                        }
            message["content"] = json.dumps(result, ensure_ascii=False, default=str)
        return [
            {"role": "system", "content": editor_system},
            {
                "role": "user",
                "content": _editor_user_content(current_user, current_render)
                if include_rendered_image
                else current_user,
            },
            *exchanges,
        ]
    latest_feedback = dict(latest_result)
    if current_check is not None:
        latest_feedback.setdefault("check", dict(current_check))
    current_user += (
        "\n\nLATEST CHECKED EDIT RESULT\n"
        "The current HTML above already includes every accepted prior edit. Use this "
        "result as the only current diagnostic state; do not reconstruct older tool "
        "turns. Continue with one of the editor tools after reading it.\n"
        f"{json.dumps(latest_feedback, ensure_ascii=False, default=str)}"
    )
    return [
        {"role": "system", "content": editor_system},
        {
            "role": "user",
            "content": _editor_user_content(current_user, current_render)
            if include_rendered_image
            else current_user,
        },
    ]


def _editor_user_content(
    text: str,
    rendered: Mapping[str, Any] | None,
) -> str | list[dict[str, Any]]:
    """Attach the exact current Chromium render when the author supports vision."""

    if not isinstance(rendered, Mapping):
        return text
    path = Path(str(rendered.get("screenshot_path") or ""))
    try:
        image_bytes = path.read_bytes() if path.is_file() else b""
    except OSError:
        return text
    if not image_bytes:
        return text
    return [
        {"type": "text", "text": text},
        {
            "type": "image_url",
            "image_url": {
                "url": vlm_client.bounded_image_data_url(
                    vlm_client.VlmImage(
                        label="current rendered poster",
                        image_bytes=image_bytes,
                        mime_type="image/png",
                    ),
                    max_edge_px=2048,
                )
            },
        },
    ]




def _direct_revision_candidate(source_html: str, raw: str) -> str | None:
    try:
        return apply_html_edits(source_html, raw)
    except ModelBoundaryError:
        return None


def _revision_output_issue(raw: str) -> dict[str, str]:
    """Explain the observed output-contract failure without retaining model text."""

    lowered = raw.strip().lower()
    if "<!doctype html>" in lowered and "</html>" not in lowered:
        return {
            "code": "revision_output_truncated",
            "message": (
                "The complete HTML response was truncated before </html>; use an "
                "editor tool against the current checked HTML instead."
            ),
        }
    return {
        "code": "revision_output_invalid",
        "message": (
            "Use an editor tool, or return one exact-edit JSON object in text fallback mode."
        ),
    }


def update_poster_module_copy(
    source_html: str,
    *,
    module_id: str,
    text: str | None,
    detail_points: Any,
    flow_details: Any,
) -> str:
    """Update existing explanatory channels while the validator owns edit authority."""

    if not module_id.strip():
        raise ModelBoundaryError(
            "invalid_module_copy_update", "module_id must be non-empty"
        )
    requested = sum(
        value is not None for value in (text, detail_points, flow_details)
    )
    if requested == 0:
        raise ModelBoundaryError(
            "invalid_module_copy_update",
            "supply text, detail_points, or flow_details",
        )
    try:
        document = lxml_html.document_fromstring(source_html)
    except etree.ParserError as exc:
        raise ModelBoundaryError("invalid_html_revision", str(exc)) from exc
    modules = [
        node
        for node in document.xpath("//*[@data-poster-module]")
        if str(node.get("data-poster-module") or "").strip() == module_id
    ]
    if len(modules) != 1:
        raise ModelBoundaryError(
            "invalid_module_copy_update",
            f"module_id must identify one module: {module_id}",
        )
    module = modules[0]

    def role_nodes(role: str) -> list[Any]:
        return module.xpath(f'.//*[@data-content-role="{role}"]')

    def bound_fragment(node: Any, markup: str) -> Any:
        fragment = lxml_html.fragment_fromstring(markup, create_parent="div")
        frozen = node.xpath('.//*[@data-frozen-math]')
        generated = fragment.xpath('.//math[@data-latex]')
        frozen_latex = [str(item.get("data-latex") or "") for item in frozen]
        generated_latex = [str(item.get("data-latex") or "") for item in generated]
        if generated_latex != frozen_latex:
            raise ModelBoundaryError(
                "invalid_module_copy_update",
                "preserve every inline formula in this copy channel with its exact "
                "current LaTeX and order",
            )
        for rendered, placeholder in zip(generated, frozen, strict=True):
            rebound = copy.deepcopy(placeholder)
            rebound.tail = rendered.tail
            rendered.getparent().replace(rendered, rebound)
        return fragment

    def replace_inner(node: Any, markup: str) -> None:
        fragment = bound_fragment(node, markup)
        for child in list(node):
            node.remove(child)
        node.text = fragment.text
        for child in list(fragment):
            node.append(child)

    if text is not None:
        nodes = role_nodes("text")
        if len(nodes) != 1:
            raise ModelBoundaryError(
                "invalid_module_copy_update",
                f"module {module_id} has no single existing text channel",
            )
        replace_inner(nodes[0], authoring.render_inline_math(text))

    if detail_points is not None:
        if not isinstance(detail_points, list) or not detail_points or any(
            not isinstance(item, str) or not item.strip() for item in detail_points
        ):
            raise ModelBoundaryError(
                "invalid_module_copy_update",
                "detail_points must be a non-empty array of non-empty strings",
            )
        nodes = role_nodes("detail-points")
        if len(nodes) != 1:
            raise ModelBoundaryError(
                "invalid_module_copy_update",
                f"module {module_id} has no single existing detail_points channel",
            )
        visual_kind = str(module.get("data-visual-kind") or "text")
        replacement_parent = bound_fragment(
            nodes[0],
            authoring.render_detail_points(
                [" ".join(item.split()) for item in detail_points],
                visual_kind=visual_kind,
            ),
        )
        replacement = replacement_parent[0]
        replacement.tail = nodes[0].tail
        nodes[0].getparent().replace(nodes[0], replacement)

    if flow_details is not None:
        if not isinstance(flow_details, list) or not flow_details or any(
            not isinstance(item, str) or not item.strip() for item in flow_details
        ):
            raise ModelBoundaryError(
                "invalid_module_copy_update",
                "flow_details must be a non-empty array of non-empty strings",
            )
        nodes = role_nodes("flow-detail")
        if len(nodes) != len(flow_details):
            raise ModelBoundaryError(
                "invalid_module_copy_update",
                f"module {module_id} requires {len(nodes)} flow_details in existing order",
            )
        for node, value in zip(nodes, flow_details, strict=True):
            replace_inner(node, authoring.render_inline_math(" ".join(value.split())))

    return lxml_html.tostring(
        document, encoding="unicode", method="html", doctype="<!doctype html>"
    )


def replace_poster_layout(source_html: str, *, layout_html: str, css: str) -> str:
    """Replace wrapper topology while reinserting exact title and module nodes."""

    try:
        document = lxml_html.document_fromstring(source_html)
        shell = lxml_html.fragment_fromstring(layout_html, create_parent="div")
    except (etree.ParserError, TypeError, ValueError) as exc:
        raise ModelBoundaryError("invalid_layout_shell", str(exc)) from exc
    roots = document.xpath("//*[@data-poster-root]")
    if len(roots) != 1:
        raise ModelBoundaryError(
            "invalid_layout_shell", "current HTML must contain one poster root"
        )
    root = roots[0]
    title_bands = root.xpath(".//*[@data-poster-title-band]")
    if len(title_bands) != 1:
        raise ModelBoundaryError(
            "invalid_layout_shell", "current poster must contain one title band"
        )
    references: dict[str, Any] = {"title-band": title_bands[0]}
    for module in root.xpath(".//*[@data-poster-module]"):
        module_id = str(module.get("data-poster-module") or "").strip()
        reference = f"module:{module_id}"
        if not module_id or reference in references:
            raise ModelBoundaryError(
                "invalid_layout_shell", "current poster module ids must be unique"
            )
        references[reference] = module

    authored_scientific_nodes = shell.xpath(
        ".//*[@data-poster-root or @data-poster-title-band or @data-poster-module]"
    )
    if authored_scientific_nodes:
        raise ModelBoundaryError(
            "invalid_layout_shell",
            "layout_html may contain wrappers and data-poster-ref placeholders only; "
            "the host reinserts scientific nodes",
        )
    document_nodes = shell.xpath(".//html | .//head | .//body | .//style")
    if document_nodes:
        raise ModelBoundaryError(
            "invalid_layout_shell",
            "layout_html is the existing poster root's inner HTML; put CSS only in "
            "the css argument",
        )
    placeholders = shell.xpath(".//*[@data-poster-ref]")
    invalid_placeholders = [
        node
        for node in placeholders
        if set(node.attrib) != {"data-poster-ref"}
        or len(node)
        or bool((node.text or "").strip())
    ]
    if invalid_placeholders:
        raise ModelBoundaryError(
            "invalid_layout_shell",
            "each data-poster-ref placeholder must be empty and have no other "
            "attributes; put layout classes, styles, and content on its parent wrapper",
        )
    placeholder_refs = [
        str(node.get("data-poster-ref") or "").strip() for node in placeholders
    ]
    if placeholder_refs.count("title-band") != 1:
        raise ModelBoundaryError(
            "invalid_layout_shell",
            "layout_html must contain data-poster-ref=\"title-band\" exactly once",
        )
    if not any(reference.startswith("module:") for reference in placeholder_refs):
        raise ModelBoundaryError(
            "invalid_layout_shell", "layout_html must retain at least one module"
        )
    if len(placeholder_refs) != len(set(placeholder_refs)):
        raise ModelBoundaryError(
            "invalid_layout_shell", "each data-poster-ref may appear only once"
        )
    unknown = sorted(set(placeholder_refs) - set(references))
    if unknown:
        raise ModelBoundaryError(
            "invalid_layout_shell",
            "layout_html references unknown poster nodes: " + ", ".join(unknown),
        )
    for placeholder, reference in zip(placeholders, placeholder_refs, strict=True):
        parent = placeholder.getparent()
        if parent is None:
            raise ModelBoundaryError(
                "invalid_layout_shell", "data-poster-ref must be inside the layout shell"
            )
        restored = copy.deepcopy(references[reference])
        restored.tail = placeholder.tail
        parent.replace(placeholder, restored)

    for child in list(root):
        root.remove(child)
    root.text = shell.text
    for child in list(shell):
        root.append(child)
    structured = lxml_html.tostring(
        document, encoding="unicode", method="html", doctype="<!doctype html>"
    )
    return replace_html_stylesheet(structured, css)


def replace_html_stylesheet(source_html: str, css: str) -> str:
    """Apply model-authored CSS while the HTML parser preserves body content."""

    try:
        document = lxml_html.document_fromstring(source_html)
    except etree.ParserError as exc:
        raise ModelBoundaryError("invalid_html_revision", str(exc)) from exc
    for style in document.xpath("//style"):
        style.drop_tree()
    head = document.find("head")
    if head is None:
        head = lxml_html.Element("head")
        document.insert(0, head)
    style = lxml_html.Element("style")
    style.text = css
    head.append(style)
    return lxml_html.tostring(
        document, encoding="unicode", method="html", doctype="<!doctype html>"
    )


def move_html_elements(source_html: str, moves: list[Mapping[str, str]]) -> str:
    """Relocate model-selected nodes, leaving unrelated tail text at its origin."""

    if not isinstance(moves, list) or not moves:
        raise ModelBoundaryError("invalid_element_move", "moves must be a non-empty array.")
    try:
        document = lxml_html.document_fromstring(source_html)
    except etree.ParserError as exc:
        raise ModelBoundaryError("invalid_html_revision", str(exc)) from exc

    def select(expression: str) -> lxml_html.HtmlElement:
        try:
            nodes = document.xpath(expression)
        except (etree.XPathError, TypeError, ValueError) as exc:
            raise ModelBoundaryError("invalid_element_move", str(exc)) from exc
        if (
            not isinstance(nodes, list) or len(nodes) != 1
            or not isinstance(nodes[0], lxml_html.HtmlElement)
        ):
            raise ModelBoundaryError(
                "edit_target_not_unique", f"XPath must select one HTML element: {expression}",
            )
        return nodes[0]

    for move in moves:
        if not isinstance(move, Mapping):
            raise ModelBoundaryError("invalid_element_move", "Each move must be an object.")
        element = select(move.get("element_xpath"))
        parent = select(move.get("parent_xpath"))
        before = select(move["before_xpath"]) if move.get("before_xpath") else None
        if parent.tag in html_defs.empty_tags:
            raise ModelBoundaryError(
                "invalid_element_move", "The destination is an HTML void element and cannot contain children.",
            )
        if element.getparent() is None or parent in element.iter():
            raise ModelBoundaryError(
                "invalid_element_move", "Cannot move the document root or into a selected descendant.",
            )
        if before is not None and before.getparent() is not parent:
            raise ModelBoundaryError(
                "invalid_element_move", "before_xpath must select a direct child of parent_xpath.",
            )
        if before is element:
            continue
        element.drop_tree()
        element.tail = None
        if before is None:
            parent.append(element)
        else:
            parent.insert(parent.index(before), element)
    return lxml_html.tostring(
        document, encoding="unicode", method="html", doctype="<!doctype html>",
    )


def apply_html_edits(source_html: str, response: str) -> str:
    """Apply sequential exact replacements from one model JSON response."""

    text = response.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].strip().lower() in {"```", "```json"}:
            text = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelBoundaryError(
            "invalid_edit_patch",
            "HTML edit response must be one valid JSON object.",
        ) from exc
    if not isinstance(payload, dict) or "edits" not in payload:
        raise ModelBoundaryError(
            "invalid_edit_patch",
            'HTML edit response must contain an "edits" array.',
        )
    edits = payload["edits"]
    if not isinstance(edits, list) or not edits:
        raise ModelBoundaryError(
            "invalid_edit_patch",
            '"edits" must be a non-empty array.',
        )
    candidate = source_html
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict) or "old" not in edit or "new" not in edit:
            raise ModelBoundaryError(
                "invalid_edit_patch",
                f"Edit {index} must contain string old and new fields.",
            )
        old = edit["old"]
        new = edit["new"]
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise ModelBoundaryError(
                "invalid_edit_patch",
                f"Edit {index} must contain a non-empty string old and a string new.",
            )
        if old == candidate:
            raise ModelBoundaryError(
                "full_document_edit_forbidden",
                "An exact edit may not replace the complete HTML document.",
            )
        occurrences = candidate.count(old)
        if occurrences != 1:
            raise ModelBoundaryError(
                "edit_target_not_unique",
                f"Edit {index} old text occurs {occurrences} times in the current HTML.",
            )
        candidate = candidate.replace(old, new, 1)
    return candidate


def parse_content_plan_response(value: str) -> dict[str, Any]:
    """Parse one unambiguous JSON object from a host model response."""

    text = value.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        decoder = json.JSONDecoder()
        objects: list[dict[str, Any]] = []
        cursor = 0
        while True:
            start = text.find("{", cursor)
            if start < 0:
                break
            try:
                candidate, end = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                cursor = start + 1
                continue
            if isinstance(candidate, dict):
                objects.append(candidate)
                cursor = start + end
            else:
                cursor = start + 1
        if len(objects) == 1:
            return objects[0]
        raise ModelBoundaryError(
            "candidate_validation_failed",
            "Evidence plan must contain exactly one valid JSON object.",
        ) from exc
    if not isinstance(parsed, dict):
        raise ModelBoundaryError(
            "candidate_validation_failed",
            "Evidence plan must be one exact JSON object.",
        )
    return parsed


async def _request_model_text(
    llm: Any,
    *,
    system: str,
    user: str,
    temperature: float,
    timeout_seconds: float | None,
    max_transport_retries: int,
    boundary_label: str,
    deadline: float | None = None,
    prefer_stream: bool = True,
) -> Any:
    for retry_index in range(max_transport_retries + 1):
        call_timeout = _remaining_timeout(
            timeout_seconds,
            deadline=deadline,
            boundary_label=boundary_label,
        )
        try:
            return await _await_model_call(
                _call_model_text(
                    llm,
                    system=system,
                    user=user,
                    temperature=temperature,
                    prefer_stream=prefer_stream,
                ),
                timeout_seconds=call_timeout,
            )
        except TimeoutError as exc:
            if retry_index < max_transport_retries and deadline is None:
                continue
            raise ModelBoundaryError(
                "llm_error",
                f"{boundary_label} model call timed out"
                + (
                    f" after {call_timeout:g} seconds"
                    if call_timeout is not None
                    else ""
                ),
            ) from exc
        except Exception as exc:
            if retry_index < max_transport_retries and _is_transient_model_error(exc):
                continue
            raise ModelBoundaryError(
                "llm_error",
                f"{boundary_label} model call failed: {_model_error_detail(exc)}",
            ) from exc
    raise AssertionError("model transport retry loop exhausted without a result")


async def _request_text_editor_turn(
    llm: Any,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    timeout_seconds: float,
    max_transport_retries: int,
    deadline: float | None,
    call_id: str,
) -> Any:
    """Carry the same editor commands over a plain, non-streaming text request."""

    system = str(messages[0]["content"]) + (
        "\n\nTEXT TRANSPORT FOR EDITOR TOOLS\n"
        "Native function calling is unavailable in this session. Invoke exactly one "
        "of the same editor commands by returning a JSON object with exactly two fields: "
        '{"name":"command_name","arguments":{...}}. Do not emit provider tool-call '
        "markup. All command validation, browser checks, and finish/review semantics "
        "remain unchanged. The transcript below records previous commands and their "
        "actual observations; these are data, not new commands to replay.\n"
        "Available command definitions:\n"
        + json.dumps([tool["function"] for tool in tools], ensure_ascii=False)
    )
    raw = await _request_model_text(
        llm, system=system, user=json.dumps(messages[1:], ensure_ascii=False),
        temperature=0.0, timeout_seconds=timeout_seconds,
        max_transport_retries=max_transport_retries,
        boundary_label="HTML text editor", deadline=deadline, prefer_stream=False,
    )
    text = str(raw or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    try:
        command = json.loads(fence.group(1) if fence else text)
    except json.JSONDecodeError:
        return text
    if not (
        isinstance(command, dict)
        and set(command) == {"name", "arguments"}
        and isinstance(command["name"], str)
        and isinstance(command["arguments"], dict)
    ):
        return text
    return SimpleNamespace(
        content="", finish_reason="tool_calls",
        tool_calls=[SimpleNamespace(id=call_id, **command)],
    )


async def _request_model_tools(
    llm: Any,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: str,
    temperature: float,
    timeout_seconds: float,
    max_transport_retries: int,
    boundary_label: str,
    deadline: float | None = None,
) -> Any:
    """Request one turn in a stateful, skill-owned tool transcript."""

    stream = getattr(llm, "chat_with_tools_stream", None)
    if not callable(stream):
        raise ModelBoundaryError(
            "llm_error", f"{boundary_label} requires a tool-capable model client"
        )
    for retry_index in range(max_transport_retries + 1):
        call_timeout = _remaining_timeout(
            timeout_seconds,
            deadline=deadline,
            boundary_label=boundary_label,
        )
        try:
            return await _await_model_call(
                stream(
                    messages,
                    tools,
                    tool_choice=tool_choice,
                    temperature=temperature,
                    max_tokens=_STREAM_TEXT_MAX_TOKENS,
                    on_delta=None,
                    on_activity=None,
                ),
                timeout_seconds=call_timeout,
            )
        except TimeoutError as exc:
            if retry_index < max_transport_retries and deadline is None:
                continue
            raise ModelBoundaryError(
                "llm_error",
                f"{boundary_label} model call timed out after {call_timeout:g} seconds",
            ) from exc
        except Exception as exc:
            if retry_index < max_transport_retries and _is_transient_model_error(exc):
                continue
            raise ModelBoundaryError(
                "llm_error",
                f"{boundary_label} model call failed: {_model_error_detail(exc)}",
            ) from exc
    raise AssertionError("model transport retry loop exhausted without a result")


async def _call_model_text(
    llm: Any,
    *,
    system: str,
    user: str,
    temperature: float,
    prefer_stream: bool = True,
) -> str:
    """Prefer the host's activity-aware public stream for tool-free text."""

    stream = getattr(llm, "chat_with_tools_stream", None)
    if not prefer_stream or not callable(stream):
        chat_result = getattr(llm, "chat_result", None)
        if not callable(chat_result):
            return await llm.chat(system, user, temperature=temperature)
        try:
            result = await chat_result(system, user, temperature=temperature)
        except (AttributeError, NotImplementedError):
            # Some host wrappers expose optional methods even when their older
            # underlying client implements only the required chat interface.
            return await llm.chat(system, user, temperature=temperature)
    else:
        result = await stream(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            [],
            tool_choice="auto",
            temperature=temperature,
            max_tokens=_STREAM_TEXT_MAX_TOKENS,
            on_delta=None,
            on_activity=None,
        )
    if isinstance(result, str):
        return result
    if getattr(result, "tool_calls", None) or getattr(
        result, "malformed_tool_calls", 0
    ):
        raise ValueError("tool-free authoring returned an unexpected tool call")
    if str(getattr(result, "finish_reason", "") or "") == "length":
        raise ValueError("model answer stopped at the output cap")
    return str(getattr(result, "content", "") or "")


def _monotonic() -> float:
    """Read the active event loop's monotonic clock."""

    return asyncio.get_running_loop().time()


def _remaining_timeout(
    timeout_seconds: float | None,
    *,
    deadline: float | None,
    boundary_label: str,
) -> float | None:
    """Cap one transport attempt to the shared portable runtime deadline."""

    if deadline is None:
        return timeout_seconds
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise ModelBoundaryError(
            "llm_error",
            f"{boundary_label} runtime budget was exhausted before the model call.",
        )
    return remaining if timeout_seconds is None else min(timeout_seconds, remaining)


async def _await_model_call(
    call: Coroutine[Any, Any, Any], *, timeout_seconds: float | None,
) -> Any:
    """Cancel the owned request on either a transport or enclosing workflow timeout."""

    task = asyncio.create_task(call)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
        if task not in done:
            raise TimeoutError
        return task.result()
    except (asyncio.CancelledError, TimeoutError):
        task.cancel()
        task.add_done_callback(_consume_model_task)
        raise


def _consume_model_task(task: asyncio.Task[Any]) -> None:
    """Consume a late transport result after a hard skill-level timeout."""

    if task.cancelled():
        return
    task.exception()


def _is_transient_model_error(error: BaseException) -> bool:
    message = _model_error_detail(error).casefold()
    return any(marker in message for marker in _TRANSIENT_MODEL_ERROR_MARKERS)


def _model_error_detail(error: BaseException) -> str:
    """Keep transport failures diagnosable even when their message is empty."""

    error_type = type(error).__name__
    message = str(error).strip()
    return f"{error_type}: {message}" if message else error_type
