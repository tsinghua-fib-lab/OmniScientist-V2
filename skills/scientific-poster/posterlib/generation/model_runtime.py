"""Model calls and stateful HTML editing for scientific-poster generation."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import poster_contract
from lxml import etree
from lxml import html as lxml_html
from lxml.html import defs as html_defs

from posterlib.content import equations, html_contract, planning
from posterlib.runtime import model_response
from posterlib.runtime.model_response import ModelBoundaryError
from posterlib.sources import paper_identity
from posterlib.visual import vlm_client

from . import authoring
from .editor_state import EditorCandidates, EditorResult, check_candidate

DEFAULT_AUTHORING_TRANSPORT_RETRIES = 0
MAX_AUTHORING_TRANSPORT_RETRIES = 2
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
                        "paper_identity": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "authors": {
                                    "type": "array", "items": {"type": "string"},
                                    "description": "Full author names in source order. Each item is a name; affiliation markers and author notes are composed later from the source.",
                                },
                                "affiliations": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["title", "authors", "affiliations"],
                            "additionalProperties": False,
                        },
                        "page_orientation": {
                            "type": "string",
                            "enum": ["portrait", "landscape"],
                        },
                        "sections": {"type": "array", "items": {"type": "object"}},
                        "content_modules": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "equations": {
                                        "type": "array",
                                        "items": {
                                            "oneOf": [
                                                {
                                                    "type": "object",
                                                    "properties": {"source_ref": {"type": "string"}},
                                                    "required": ["source_ref"],
                                                    "additionalProperties": False,
                                                },
                                                {
                                                    "type": "object",
                                                    "properties": {
                                                        "latex": {"type": "string"},
                                                        "source_label": {"type": "string"},
                                                    },
                                                    "required": ["latex", "source_label"],
                                                    "additionalProperties": False,
                                                },
                                            ],
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "required": ["paper_identity", "page_orientation", "sections", "content_modules"],
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
            "name": "render_poster_equations",
            "description": (
                "Convert source-grounded LaTeX into canonical MathML fragments for the "
                "poster. This prepares equations without changing the HTML. Obtain these "
                "fragments before composing new mathematics, then insert the returned "
                "MathML unchanged in a layout or exact edit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "equations": {
                        "type": "array", "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "latex": {"type": "string", "minLength": 1},
                                "display": {"type": "string", "enum": ["inline", "block"]},
                                "source_label": {"type": "string"},
                            },
                            "required": ["latex", "display"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["equations"],
                "additionalProperties": False,
            },
        },
    },
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
                "or another data-poster-root. Compose module interiors and surrounding "
                "groups using the current module HTML within the supplied editing scope. "
                "Keep source relationships and metadata consistent with the authored content. Alternatively "
                "retain a complete current node with data-poster-ref=\"title-band\" or "
                "data-poster-ref=\"module:<module-id>\". Image and formula refs from the "
                "current state insert their exact protected nodes into authored modules. "
                "Use a reference once, through its containing module or its own placeholder. "
                "Place layout classes on a retained "
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



def require_host_llm(ctx: Any) -> Any:
    """Return the callable host LLM or raise the stable boundary error."""

    llm = getattr(ctx, "llm", None) if ctx is not None else None
    if llm is None or not callable(getattr(llm, "chat", None)):
        raise ModelBoundaryError(
            "llm_unavailable",
            "This host action requires an LLM supplied by the host runtime.",
        )
    return llm


def vision_author_available(ctx: Any) -> bool:
    """Recognize the host's chat or credential-isolated vision generation port."""

    service = getattr(ctx, "vlm", None)
    return bool(getattr(service, "available", False)) and any(
        callable(getattr(service, method, None))
        for method in ("chat", "generate_text")
    )


def supports_image_input(llm: Any) -> bool:
    """Recognize image-capable chat clients and the host vision generation port."""

    return bool(getattr(llm, "supports_image_input", False)) or callable(
        getattr(llm, "generate_text", None)
    )


def require_author_llm(ctx: Any, role: str) -> Any:
    """Return the configured authoring model for one canonical runtime role."""

    if role == "main":
        return require_host_llm(ctx)
    if role != "vision":
        raise ModelBoundaryError(
            "invalid_payload", "author_model_role must be main or vision"
        )
    if not vision_author_available(ctx):
        raise ModelBoundaryError(
            "vision_author_unavailable",
            "The configured VLM is not available for authoring.",
        )
    return ctx.vlm


async def request_content_plan(
    llm: Any,
    *,
    source_text: str,
    assets: list[dict[str, Any]],
    source_figure_sha256s: set[str],
    authoring_request: str,
    prepared_identity: Mapping[str, Any] | None = None,
    source_equations: list[dict[str, str]] | None = None,
    page: Any = None,
    orientation: str = "auto",
    max_repair_attempts: int | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Propose grounded evidence and a starting page budget."""

    if page is not None and not isinstance(page, Mapping):
        raise planning.PlanningError("invalid_page", "page must be an object")
    capacity_contract = planning.content_capacity_hint(page, orientation=orientation)
    figure_refs = {
        str(item.get("token") or ""): str(item.get("content_sha256") or "")
        for item in assets
        if str(item.get("token") or "")
        and (str(item.get("content_sha256") or "") in source_figure_sha256s
             or item.get("source_kind") == "native_component")
    }
    system, original_request = authoring.content_plan_prompt(
        source_text=source_text,
        assets=assets,
        authoring_request=authoring_request,
        source_equations=source_equations,
        page=capacity_contract,
    )
    known_identity = paper_identity.merge(prepared_identity or {}, {})
    original_request += (
        "\n\nPrepared paper identity (preserve the supplied title, author names and "
        "affiliation facts; express author names as the schema's array and extract "
        "missing fields from the grounding source):\n"
        + json.dumps(known_identity, ensure_ascii=False, sort_keys=True)
    )
    boundary_label = "Evidence planning"
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
                source_text=source_text,
                source_assets=assets,
            )
            candidate_budget["paper_identity"] = paper_identity.merge(
                known_identity, candidate_budget.get("paper_identity", {})
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
            or (deadline is not None and _monotonic() >= deadline)
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
    try:
        result = await _request_model_tools(
            llm,
            messages=messages,
            tools=[_CONTENT_PLAN_TOOL],
            tool_choice="auto",
            temperature=0.0,
            timeout_seconds=timeout_seconds,
            max_transport_retries=max_transport_retries,
            boundary_label=boundary_label,
            deadline=deadline,
        )
    except ModelBoundaryError as exc:
        if exc.reason != "tool_transport_unavailable":
            raise
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
    if len(calls) != 1 or str(getattr(calls[0], "name", "") or "") != "submit_content_plan":
        raise ModelBoundaryError(
            "candidate_validation_failed",
            "Evidence planning must call submit_content_plan exactly once.",
        )
    call = calls[0]
    argument_error = str(getattr(call, "arguments_error", "") or "")
    arguments = getattr(call, "arguments", None)
    if argument_error or not isinstance(arguments, dict) or not isinstance(arguments.get("plan"), dict):
        raise ModelBoundaryError(
            "candidate_validation_failed",
            "submit_content_plan requires one valid plan object"
            + (f": {argument_error}" if argument_error else "."),
        )
    return dict(arguments["plan"])


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


def _editor_system_prompt(system: str, *, compact_layout: bool, inspected: bool) -> str:
    """Describe the supplied context independently of the editing authority."""

    representation = (
        "The current shell, complete CSS, editable module HTML and measured state appear below. "
        "Module HTML retains current text, method order, table cells, content roles and CSS targets. "
        "Compose the internal diagram or comparison as well as the surrounding page. Use "
        "replace_poster_layout to submit authored modules alongside references to retained nodes. "
        "A whole-module reference retains its descendants too; use its image and formula refs "
        "only when authoring that module's interior. The host restores the protected nodes. "
        "protected_content describes their notation and image identity. References belong to "
        "this current state. Exact local edits match actual source substrings; protected "
        "reference markers are resolved by replace_poster_layout."
        if compact_layout else
        "The current HTML and measured state appear below."
    )
    checks = (
        "Each statically valid edit receives browser measurements."
        if inspected else "Each edit receives HTML and scientific-content validation."
    )
    return (
        f"{system}\n\nPOSTER EDITOR\n{representation} {checks} "
        "Use one available editor tool per turn and read its result. "
        "Prepare new equations with render_poster_equations from source LaTeX before editing; "
        "insert its returned MathML unchanged. This read-only operation preserves the current "
        "editing authority and leaves the poster untouched. Choose local replacements, "
        "a stylesheet update or a move of existing nodes for a local defect. Replace the layout "
        "shell when the reading structure needs new grouping. The first composition of a neutral "
        "carrier requires a complete shell and stylesheet; subsequent repairs can be local. "
        "A layout shell belongs inside the existing poster root; supply CSS separately. "
        "Use the current module, painted content and figure bounds, spacing observations and "
        "parent relationships to locate the governing layout rule. Match selectors to the "
        "actual DOM. Size scientific carriers for reading, with content-sized rows and local "
        "gaps; surrounding container growth alone leaves their content unchanged. "
        "Preserve each source image's bytes, full extent, colors and intrinsic margins. "
        "Use its surface observations and measured surface_context to integrate it through "
        "surrounding spacing, alignment and white surfaces. Filters, blends, masks and background "
        "removal change source evidence and are outside layout editing. "
        "The latest state is authoritative. A repeated operation on unchanged HTML is skipped; "
        "use the returned diagnostics to choose another edit. The host hands a checked edit "
        "to the independent visual reviewer as soon as its authorized operation is complete."
    )


def _apply_editor_command(
    name: str, arguments: Mapping[str, Any], source: str,
) -> str:
    """Apply a parsed editing command to one exact source document."""

    if name == "replace_poster_layout":
        return replace_poster_layout(
            source, layout_html=str(arguments.get("layout_html") or ""),
            css=str(arguments.get("css") or ""),
        )
    if name == "replace_poster_stylesheet":
        return replace_html_stylesheet(source, str(arguments.get("css") or ""))
    if name == "move_poster_elements":
        return move_html_elements(source, arguments.get("moves"))
    if name == "update_poster_module_copy":
        module_id = str(arguments.get("module_id") or "")
        return update_poster_module_copy(
            source, module_id=module_id,
            text=str(arguments["text"]) if "text" in arguments else None,
            detail_points=arguments.get("detail_points"),
            flow_details=arguments.get("flow_details"),
        )
    if name == "apply_poster_edits":
        return apply_html_edits(source, json.dumps(
            {"edits": arguments.get("edits")}, ensure_ascii=False,
        ))
    raise ModelBoundaryError("unknown_revision_tool", f"Unknown revision tool: {name}")


def _render_poster_equations(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Expose the shared source-LaTeX renderer to the authoring tool loop."""

    values = arguments.get("equations")
    if not isinstance(values, list) or not values:
        raise ModelBoundaryError("invalid_tool_arguments", "equations must be a non-empty array")
    fragments = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping) or not isinstance(value.get("latex"), str) or not value["latex"].strip():
            raise ModelBoundaryError("invalid_tool_arguments", f"equations[{index}].latex must contain source LaTeX")
        display = value.get("display")
        if display not in ("inline", "block"):
            raise ModelBoundaryError("invalid_tool_arguments", f"equations[{index}].display must be inline or block")
        label = value.get("source_label", "")
        if not isinstance(label, str):
            raise ModelBoundaryError("invalid_tool_arguments", f"equations[{index}].source_label must be text")
        latex = value["latex"]
        attributes = {"display": display, "data-latex": latex}
        if label:
            attributes["data-source-label"] = label
        try:
            mathml = equations.latex_to_mathml(latex, attributes=attributes)
        except (equations.EquationSyntaxError, equations.EquationRendererUnavailable) as exc:
            raise ModelBoundaryError("equation_rendering_failed", f"equations[{index}]: {exc}") from exc
        fragments.append({"latex": latex, "display": display, "mathml": mathml})
    return {"status": "rendered", "data": {"equations": fragments}}


async def request_html_revision(
    llm: Any,
    *,
    source_html: str,
    system: str,
    user: str,
    validate: Callable[[str], dict[str, Any]],
    inspect: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    initial_validation: dict[str, Any] | None = None,
    initial_inspection: dict[str, Any] | None = None,
    initial_composition: bool = False,
    compact_layout: bool = False,
    include_rendered_image: bool = False,
    timeout_seconds: float | None = None,
    max_transport_retries: int = DEFAULT_AUTHORING_TRANSPORT_RETRIES,
    deadline: float | None = None,
) -> EditorResult:
    """Continue one editor transcript and return its selected checkpoint and stop cause."""

    template = html_contract.strip_math_font_styles(source_html)
    if template != source_html:
        user = user.replace(source_html, template)
        source_html = template
    loop = asyncio.get_running_loop()
    if timeout_seconds is not None:
        local_deadline = loop.time() + timeout_seconds
        deadline = min(deadline, local_deadline) if deadline is not None else local_deadline
    inspection = inspect
    if inspect is not None:
        async def bounded_inspection(html: str) -> dict[str, Any]:
            try:
                async with asyncio.timeout_at(deadline):
                    return await inspect(html)
            except TimeoutError:
                return poster_contract.outcome_result(
                    "inspection_unavailable",
                    summary="Browser inspection reached the editor deadline.",
                )
        inspection = bounded_inspection
    source = await check_candidate(
        source_html, validate=validate, inspect=inspection,
        supplied_validation=initial_validation, supplied_inspection=initial_inspection,
    )
    candidates = EditorCandidates(
        source, validate=validate, inspect=inspection, initial_composition=initial_composition,
    )
    editor_system = _editor_system_prompt(
        system, compact_layout=compact_layout, inspected=inspect is not None,
    )
    last_diagnostics: dict[str, Any] = {}
    attempted_edits: set[tuple[str, str]] = set()
    native_transport = callable(getattr(llm, "chat_with_tools_stream", None))

    def available_editor_tools() -> list[dict[str, Any]]:
        if initial_composition and not candidates.has_working_edit:
            names = {"render_poster_equations", "replace_poster_layout"}
        else:
            return _REVISION_TOOLS
        return [tool for tool in _REVISION_TOOLS if tool["function"]["name"] in names]

    def editor_feedback() -> dict[str, Any]:
        current = candidates.current
        if compact_layout:
            return _layout_editor_state(
                candidate=current.html,
                check=current.feedback, result=last_diagnostics or None,
            )
        return {
            **last_diagnostics,
            "current_html": current.html,
            "check": current.feedback,
        }

    def append_editor_turn(result: Any, observations: list[dict[str, Any]]) -> None:
        nonlocal messages
        content = result if isinstance(result, str) else str(getattr(result, "content", "") or "")
        calls = [] if isinstance(result, str) else list(getattr(result, "tool_calls", None) or [])
        assistant: dict[str, Any] = {"role": "assistant", "content": content}
        reasoning = str(getattr(result, "reasoning_content", "") or "")
        if reasoning:
            assistant["reasoning_content"] = reasoning
        wire_calls = [
            {
                "id": str(getattr(call, "id", "") or f"poster-{iteration}-{index}"),
                "type": "function",
                "function": {
                    "name": str(getattr(call, "name", "") or ""),
                    "arguments": str(getattr(call, "raw_arguments", "") or "")
                    or json.dumps(getattr(call, "arguments", {}) or {}, ensure_ascii=False),
                },
            }
            for index, call in enumerate(calls)
        ]
        if wire_calls:
            assistant["tool_calls"] = wire_calls
        exchange = [assistant]
        feedback = editor_feedback()
        if wire_calls:
            observations[-1] = feedback
            exchange.extend({
                "role": "tool", "tool_call_id": call["id"],
                "content": json.dumps(observation, ensure_ascii=False, default=str),
            } for call, observation in zip(wire_calls, observations, strict=True))
        else:
            exchange.append({
                "role": "user",
                "content": json.dumps(feedback, ensure_ascii=False, default=str),
            })
        if include_rendered_image:
            rendered_content = _editor_user_content(
                "Current rendered poster after the preceding edit result.",
                candidates.current.inspection,
            )
            if isinstance(rendered_content, list):
                exchange.append({"role": "user", "content": rendered_content})
        # Keep the request prefix and native call/result pairs intact so the
        # next turn can continue the same reasoning and provider prompt cache.
        messages = [*messages, *exchange]

    def repeated_edit(action: Any) -> bool:
        key = (
            hashlib.sha256(candidates.current.html.encode("utf-8")).hexdigest(),
            json.dumps(action, ensure_ascii=False, sort_keys=True, default=str),
        )
        if key in attempted_edits:
            return True
        attempted_edits.add(key)
        return False

    async def request_editor_turn(iteration: int, tools: list[dict[str, Any]]) -> Any:
        nonlocal native_transport
        if native_transport:
            try:
                result = await _request_model_tools(
                    llm, messages=messages, tools=tools, tool_choice="auto",
                    temperature=0.0, timeout_seconds=timeout_seconds,
                    max_transport_retries=max_transport_retries,
                    boundary_label="HTML revision editor", deadline=deadline,
                )
                if result and getattr(result, "tool_calls", None):
                    return result
                if getattr(result, "finish_reason", "") == "length":
                    return result
                content = result if isinstance(result, str) else getattr(result, "content", "")
                if content:
                    parsed = _text_editor_response(content, call_id=f"poster-text-{iteration}")
                    if not isinstance(parsed, str):
                        parsed.reasoning_content = getattr(result, "reasoning_content", "")
                        return parsed
                    return result
                raise ModelBoundaryError("llm_error", "Native editor returned no usable response",
                                         reason="invalid_model_response")
            except ModelBoundaryError as exc:
                if candidates.has_working_edit or exc.reason != "tool_transport_unavailable":
                    raise
                native_transport = False
        return await _request_text_editor_turn(
            llm, messages=messages, tools=tools, timeout_seconds=timeout_seconds,
            max_transport_retries=max_transport_retries, deadline=deadline,
            call_id=f"poster-text-{iteration}",
        )

    initial_feedback = editor_feedback()
    if not compact_layout and source_html in user:
        initial_feedback.pop("current_html")
    initial_content = user + "\n\nCURRENT EDITOR STATE\n" + json.dumps(
        initial_feedback, ensure_ascii=False, default=str,
    )
    messages = [
        {"role": "system", "content": editor_system},
        {
            "role": "user",
            "content": _editor_user_content(initial_content, candidates.current.inspection)
            if include_rendered_image else initial_content,
        },
    ]
    iteration = 0
    while deadline is None or loop.time() < deadline:
        available_tools = available_editor_tools()
        available_names = {tool["function"]["name"] for tool in available_tools}
        try:
            result = await request_editor_turn(iteration, available_tools)
        except ModelBoundaryError as exc:
            return candidates.stopped(exc)
        iteration += 1
        content = result if isinstance(result, str) else str(getattr(result, "content", "") or "")
        tool_calls = [] if isinstance(result, str) else list(getattr(result, "tool_calls", None) or [])
        if not tool_calls and getattr(result, "finish_reason", "") == "length":
            return candidates.stopped(ModelBoundaryError(
                "revision_output_truncated",
                "The model reached its output cap before returning an editor command. "
                "The checked checkpoint remains selected; continuing requires another "
                "model request with sufficient output capacity.",
            ))
        if tool_calls:
            observations = []
            for call in tool_calls:
                name = str(getattr(call, "name", "") or "")
                arguments = getattr(call, "arguments", None)
                arguments = arguments if isinstance(arguments, dict) else {}
                argument_error = str(getattr(call, "arguments_error", "") or "")
                if getattr(call, "arguments_truncated", False):
                    observation = {
                        "status": "error", "code": "revision_output_truncated",
                        "message": (
                            "The output cap interrupted this command's arguments. "
                            "The incomplete command was not applied. Continue from the "
                            "preceding reasoning and current state with a complete command "
                            "using: " + ", ".join(sorted(available_names))
                        ),
                    }
                elif argument_error:
                    observation = {"status": "error", "code": "invalid_tool_arguments",
                                   "message": argument_error}
                elif name not in available_names:
                    observation = {
                        "status": "error", "code": "editor_tool_not_available",
                        "message": f"{name} is outside the current authority. Use: "
                                   + ", ".join(sorted(available_names)),
                    }
                elif name == "render_poster_equations":
                    try:
                        observation = _render_poster_equations(arguments)
                    except ModelBoundaryError as exc:
                        observation = {"status": "error", "code": exc.code, "message": str(exc)}
                elif repeated_edit((name, arguments)):
                    observation = {
                        "status": "unchanged", "code": "editor_no_progress",
                        "message": "This exact action already ran on these bytes. Choose a different edit.",
                        "check": candidates.current.feedback,
                    }
                elif name == "restore_poster_checkpoint":
                    observation = candidates.restore(str(arguments.get("checkpoint") or ""))
                else:
                    try:
                        updated = _apply_editor_command(
                            name, arguments, candidates.current.html,
                        )
                    except ModelBoundaryError as exc:
                        observation = {"status": "error", "code": exc.code, "message": str(exc)}
                    else:
                        try:
                            observation = await candidates.stage(updated)
                        except ModelBoundaryError as exc:
                            return candidates.stopped(exc)
                last_diagnostics = observation
                observations.append(observation)
            if getattr(result, "malformed_tool_calls", 0):
                return candidates.stopped(ModelBoundaryError(
                    "revision_output_invalid",
                    "The model response included an unreadable editor command. "
                    "Completed edits remain in the checked checkpoint.",
                ))
            turn_failed = any(
                observation.get("status") in {"error", "diagnostic"}
                for observation in observations
            )
            completed_edit = any(
                observation.get("status") in {"applied", "restored"}
                for observation in observations
            )
            prepared_content = any(
                observation.get("status") == "rendered" for observation in observations
            )
            # Read-only preparation must reach the author before a later edit
            # can finish the turn, even when an earlier candidate is valid.
            if completed_edit and not prepared_content and not turn_failed and candidates.ready():
                return EditorResult(candidates.current)
            append_editor_turn(result, observations)
            continue

        duplicate = repeated_edit(("text", content))
        requires_initial_layout = initial_composition and not candidates.has_working_edit
        updated = (
            None if duplicate or requires_initial_layout
            else _direct_revision_candidate(candidates.current.html, content)
        )
        if duplicate:
            last_diagnostics = {
                "status": "unchanged", "code": "editor_no_progress",
                "message": "This response already ran on these bytes. Choose a different edit.",
                "check": candidates.current.feedback,
            }
        elif requires_initial_layout:
            last_diagnostics = {
                "status": "error",
                "code": "initial_layout_required",
                "message": "Use an authorized editor command: " + ", ".join(sorted(available_names)),
                "check": candidates.current.feedback,
            }
        elif updated is None:
            last_diagnostics = {"status": "error", "issues": [_revision_output_issue(content)]}
        else:
            try:
                last_diagnostics = await candidates.stage(updated)
            except ModelBoundaryError as exc:
                return candidates.stopped(exc)
            if candidates.ready():
                return EditorResult(candidates.current)
        append_editor_turn(result, [])

    stopped = candidates.stopped(ModelBoundaryError(
        "candidate_validation_failed",
        "The editor reached its execution deadline; the checked checkpoint remains selected.",
        reason="deadline_exhausted",
    ))
    stopped.issue["last_edit"] = {
        key: value for key, value in last_diagnostics.items() if key != "current_html"
    }
    return stopped


def _layout_editor_state(
    *,
    candidate: str,
    check: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Expose current scientific meaning with the shell, CSS, and measured state."""

    document = lxml_html.document_fromstring(candidate)
    etree.strip_tags(document, etree.Comment)
    root = document.xpath("//*[@data-poster-root]")[0]
    references = _layout_references(root)
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
            "html": _layout_node_html(module, references),
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
            "issues": value.get("issues") or [],
            "warnings": report.get("warnings") or [],
            "unmatched_style_selectors": report.get("unmatched_style_selectors") or [],
            "diagnostics": value.get("diagnostics"),
            "poster": report.get("poster"),
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
                    "content_bounds_px": module.get("content_bounds_px"),
                    "internal_space": module.get("internal_space"),
                    "layout": module.get("layout"),
                }
                for module in report.get("modules") or []
                if isinstance(module, Mapping)
            ],
            "source_figures": [
                {
                    key: figure.get(key)
                    for key in (
                        "module_id", "sha256", "painted_width_mm",
                        "module_width_mm", "visible", "surface_context", "figure_wrapper_rect",
                    )
                }
                for figure in figures.get("details") or []
                if isinstance(figure, Mapping)
            ],
            "adjacent_vertical_gaps": report.get("adjacent_vertical_gaps") or [],
            "layout_containers": report.get("layout_containers") or [],
        }

    state: dict[str, Any] = {
        "title_band": next((
            _layout_node_html(node, references) for node in root.xpath(".//*[@data-poster-title-band]")
        ), None),
        "current_css": css,
        "current_layout_html": layout_html,
        "available_poster_refs": list(references),
        "protected_content": {
            reference: {
                "tag": node.tag,
                **{name: node.get(name) for name in (
                    "data-latex", "display", "alt",
                    "data-source-figure-aspect-ratio", "style",
                ) if node.get(name)},
            }
            for reference, node in references.items()
            if node.tag in {"math", "img"}
        },
        "modules": modules,
        "current_check": brief_check(check),
    }
    if result is not None:
        state["last_edit_result"] = {
            key: result.get(key)
            for key in (
                "status", "code", "message", "issues", "new_unmatched_selectors",
                "data",
            )
            if result.get(key) is not None
        }
        if isinstance(result.get("proposal_check"), Mapping):
            state["last_edit_result"]["proposal_check"] = brief_check(
                result["proposal_check"]
            )
    return state


def _layout_references(root: Any) -> dict[str, Any]:
    """Index current reusable nodes for both the editor context and its operation."""

    references: dict[str, Any] = {}
    for node in root.xpath(".//*[@data-poster-title-band or @data-poster-module]"):
        reference = (
            f"module:{node.get('data-poster-module')}"
            if node.get("data-poster-module") is not None else "title-band"
        )
        if reference in references:
            raise ModelBoundaryError("invalid_layout_shell", "current poster references must be unique")
        references[reference] = node
    for tag, prefix in (("math", "math"), ("img", "image")):
        for index, node in enumerate(root.xpath(f".//{tag}"), start=1):
            references[f"{prefix}:{index}"] = node
    return references


def _layout_node_html(node: Any, references: Mapping[str, Any]) -> str:
    """Expose editable markup with atomic references for image bytes and MathML."""

    clone = copy.deepcopy(node)
    container = lxml_html.Element("div")
    container.append(clone)
    protected = {element: reference for reference, element in references.items()
                 if element.tag in {"math", "img"}}
    for original, copied in list(zip(node.iter(), clone.iter(), strict=True)):
        if original in protected:
            marker = lxml_html.Element("span", {"data-poster-ref": protected[original]})
            marker.tail = copied.tail
            copied.getparent().replace(copied, marker)
    return lxml_html.tostring(container[0], encoding="unicode", method="html", with_tail=False)


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
    """Compose scientific regions and wrappers, resolving current protected references."""

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
    references = _layout_references(root)
    if shell.xpath(".//*[@data-poster-root]"):
        raise ModelBoundaryError(
            "invalid_layout_shell",
            "layout_html is inside the existing poster root; preserve that outer root",
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
    root.attrib.pop("data-content-carrier", None)
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

    try:
        return model_response.parse_json_object(value)
    except ValueError as exc:
        raise ModelBoundaryError(
            "candidate_validation_failed",
            f"Evidence plan must contain exactly one valid JSON object: {exc}",
        ) from exc


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
    reference_image_uri: str | None = None,
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
                        reference_image_uri=reference_image_uri,
                ),
                timeout_seconds=call_timeout,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
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
                reason="model_timeout",
            ) from exc
        except Exception as exc:
            if retry_index < max_transport_retries and _is_transient_model_error(exc):
                continue
            raise ModelBoundaryError(
                "llm_error",
                f"{boundary_label} model call failed: {_model_error_detail(exc)}",
                reason=_model_error_reason(exc),
            ) from exc
    raise AssertionError("model transport retry loop exhausted without a result")


async def _request_text_editor_turn(
    llm: Any,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    timeout_seconds: float | None,
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
    transcript = messages[1:]
    reference_image_uri = None
    if callable(getattr(llm, "generate_text", None)):
        transcript = copy.deepcopy(transcript)
        for message in transcript:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for index, block in enumerate(content):
                if block.get("type") == "image_url":
                    reference_image_uri = block["image_url"]["url"]
                    content[index] = {
                        "type": "text",
                        "text": "A poster render was attached with this message.",
                    }
    raw = await _request_model_text(
        llm, system=system, user=json.dumps(transcript, ensure_ascii=False),
        temperature=0.0, timeout_seconds=timeout_seconds,
        max_transport_retries=max_transport_retries,
        boundary_label="HTML text editor", deadline=deadline, prefer_stream=False,
        reference_image_uri=reference_image_uri,
    )
    return _text_editor_response(raw, call_id=call_id)


def _text_editor_response(raw: str, *, call_id: str) -> Any:
    """Reuse returned text commands through the same editor authority and checks."""

    text = str(raw or "").strip()
    try:
        command = model_response.parse_json_object(text)
    except ValueError:
        return text
    if set(command) == {"edits"}:
        command = {"name": "apply_poster_edits", "arguments": command}
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
    timeout_seconds: float | None,
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
                    **_model_output_options(llm),
                    on_delta=None,
                    on_activity=None,
                ),
                timeout_seconds=call_timeout,
            )
        except NotImplementedError as exc:
            raise ModelBoundaryError(
                "llm_error", f"{boundary_label} tool transport is unavailable",
                reason="tool_transport_unavailable",
            ) from exc
        except (TimeoutError, httpx.TimeoutException) as exc:
            if retry_index < max_transport_retries and deadline is None:
                continue
            raise ModelBoundaryError(
                "llm_error",
                f"{boundary_label} model call timed out"
                + (f" after {call_timeout:g} seconds" if call_timeout is not None else ""),
                reason="model_timeout",
            ) from exc
        except Exception as exc:
            if retry_index < max_transport_retries and _is_transient_model_error(exc):
                continue
            raise ModelBoundaryError(
                "llm_error",
                f"{boundary_label} model call failed: {_model_error_detail(exc)}",
                reason=_model_error_reason(exc),
            ) from exc
    raise AssertionError("model transport retry loop exhausted without a result")


async def _call_model_text(
    llm: Any,
    *,
    system: str,
    user: str,
    temperature: float,
    prefer_stream: bool = True,
    reference_image_uri: str | None = None,
) -> str:
    """Use the host's output allowance across streaming and plain transports."""

    generate = getattr(llm, "generate_text", None)
    if callable(generate):
        return await generate(
            f"Instructions:\n{system}\n\nRequest:\n{user}",
            reference_image_uri=reference_image_uri,
        )
    stream = getattr(llm, "chat_with_tools_stream", None)
    complete = stream if prefer_stream and callable(stream) else getattr(llm, "chat_with_tools", None)
    result: Any = None
    if callable(complete):
        options = {"on_delta": None, "on_activity": None} if complete is stream else {}
        try:
            result = await complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                [],
                tool_choice="auto",
                temperature=temperature,
                **_model_output_options(llm),
                **options,
            )
        except NotImplementedError:
            # Older portable hosts can expose optional methods through wrappers.
            complete = None
    if not callable(complete):
        chat_result = getattr(llm, "chat_result", None)
        if not callable(chat_result):
            return await llm.chat(system, user, temperature=temperature)
        try:
            result = await chat_result(system, user, temperature=temperature)
        except NotImplementedError:
            # Some host wrappers expose optional methods even when their older
            # underlying client implements only the required chat interface.
            return await llm.chat(system, user, temperature=temperature)
    if isinstance(result, str):
        return result
    if getattr(result, "tool_calls", None) or getattr(
        result, "malformed_tool_calls", 0
    ):
        raise ValueError("tool-free authoring returned an unexpected tool call")
    if str(getattr(result, "finish_reason", "") or "") == "length":
        raise ValueError("model answer stopped at the output cap")
    return str(getattr(result, "content", "") or "")


def _model_output_options(llm: Any) -> dict[str, int]:
    """Leave output policy with the supplied model service."""

    allowance = getattr(llm, "max_output_tokens", None)
    return {"max_tokens": allowance} if allowance is not None else {}


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
            reason="deadline_exhausted",
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
    """Use host classification, HTTP status or transport types to bound retries."""

    retryable = getattr(error, "retryable", None)
    if isinstance(retryable, bool):
        return retryable
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status == 429 or 500 <= status < 600
    return isinstance(error, (TimeoutError, ConnectionError, httpx.TransportError))


def _model_error_reason(error: BaseException) -> str:
    """Preserve an existing host classification without importing its internals."""

    info = getattr(error, "info", None)
    category = info.get("category") if isinstance(info, Mapping) else getattr(info, "category", None)
    return "model_timeout" if category == "timeout" else "provider_error"


def _model_error_detail(error: BaseException) -> str:
    """Keep transport failures diagnosable even when their message is empty."""

    error_type = type(error).__name__
    message = str(error).strip()
    return f"{error_type}: {message}" if message else error_type
