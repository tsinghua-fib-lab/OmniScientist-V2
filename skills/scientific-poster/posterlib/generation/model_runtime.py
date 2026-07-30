"""Bounded text-model calls for scientific-poster planning and authoring."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import poster_assets
import poster_core

from posterlib.content import planning

from . import authoring

EVIDENCE_PLANNING_TIMEOUT_SECONDS = 240
DEFAULT_AUTHORING_TIMEOUT_SECONDS = 240.0
MAX_AUTHORING_TIMEOUT_SECONDS = 900.0
DEFAULT_AUTHORING_TRANSPORT_RETRIES = 0
MAX_AUTHORING_TRANSPORT_RETRIES = 2
MAX_REPAIR_ATTEMPTS = 1

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
_INVALID_MODULE_FIELDS_RE = re.compile(
    r"module\s+(?P<module>[a-z0-9-]+)\s+has invalid or missing fields:\s*"
    r"(?P<fields>[^;\n]+)",
    re.IGNORECASE,
)


class ModelBoundaryError(ValueError):
    """A host model response could not satisfy its output contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ModelBudgetDeferred(ModelBoundaryError):
    """A checkpointed model repair was deferred to preserve delivery time."""


async def request_evidence_budget(
    llm: Any,
    *,
    source_text: str,
    assets: list[dict[str, Any]],
    source_figure_sha256s: set[str],
    authoring_request: str,
    page: Any = None,
    capacity_hint: Any = None,
    orientation: str = "auto",
    max_repair_attempts: int = MAX_REPAIR_ATTEMPTS,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Request and validate one evidence budget with bounded guided repairs."""

    system, original_request = authoring.evidence_budget_prompt(
        source_text=source_text,
        assets=assets,
        authoring_request=authoring_request,
        page=(
            capacity_hint
            if capacity_hint is not None
            else page or {"strategy": "auto", "orientation": orientation}
        ),
    )
    request = original_request
    last_error = "Evidence budget was not returned."
    last_error_code = "candidate_validation_failed"
    previous_candidate = ""
    for attempt in range(max_repair_attempts + 1):
        response = await _request_model_text(
            llm,
            system=system,
            user=request,
            temperature=0.0,
            timeout_seconds=EVIDENCE_PLANNING_TIMEOUT_SECONDS,
            max_transport_retries=1 if attempt == 0 else 0,
            boundary_label=(
                "Evidence planning" if attempt == 0 else "Evidence planning repair"
            ),
            deadline=deadline,
        )
        try:
            if not isinstance(response, str):
                raise ModelBoundaryError(
                    "candidate_validation_failed",
                    "Evidence plan must be returned as text containing one JSON object.",
                )
            previous_candidate = response.strip()[:30000]
            parsed = parse_evidence_budget_response(response)
            parsed = _sanitize_generated_copy(
                parsed,
                source_text=source_text,
            )
            parsed = _prune_generated_source_labels(
                parsed,
                source_text=source_text,
            )
            parsed = _bind_generated_figure_identities(parsed, assets=assets)
            parsed = _bind_generated_figure_geometry(parsed, assets=assets)
            parsed = _normalize_generated_identifiers(parsed)
            previous_candidate = json.dumps(
                parsed,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            if not parsed.get("sections"):
                raise planning.PlanningError(
                    "invalid_content_budget",
                    "evidence planning must return at least one ordered scan section",
                )
            budget = planning.normalize_content_budget(
                _downgrade_unbound_source_figure_focus(parsed),
                source_text=source_text,
                source_figure_sha256s=source_figure_sha256s,
                source_figure_numbers=planning.source_figure_number_bindings(assets),
            )
            page_plan = planning.estimate_page(
                budget,
                page=page,
                orientation=orientation,
            )
            if page_plan.strategy == "auto":
                planning.validate_content_capacity(
                    budget,
                    width_mm=page_plan.width_mm,
                    height_mm=page_plan.height_mm,
                )
        except (ModelBoundaryError, planning.PlanningError) as exc:
            last_error = str(exc)
            last_error_code = exc.code
        else:
            return budget
        if attempt < max_repair_attempts:
            invalid_module_fields = _INVALID_MODULE_FIELDS_RE.search(last_error)
            if last_error_code == "content_capacity_exceeded":
                correction = (
                    "Treat this as semantic evidence editing before layout. Preserve "
                    "the main scientific claim, the key method needed to understand "
                    "it, and the decisive supporting evidence. Remove repeated ideas "
                    "across text, detail_points, and takeaway instead of mechanically "
                    "shortening every field. Merge overlapping modules where useful, "
                    "and omit secondary figures or secondary modules that are not "
                    "needed for that argument. Remove an omitted figure as one grounded "
                    "unit: delete its hash, its dependent claims or caption/details, "
                    "and its matching Figure N locator together, while preserving "
                    "unrelated locators. Retain qualifiers and source locators for every "
                    "surviving claim, and retain figure bindings only for figures still "
                    "selected. Never invent replacement content or change the "
                    "scientific conclusion."
                )
            elif invalid_module_fields is not None:
                module_id = invalid_module_fields.group("module")
                fields = invalid_module_fields.group("fields").strip()
                correction = (
                    f"Repair only the named field(s) in module {module_id}: {fields}. "
                    "Schema reference: id must be unique kebab-case; section_id must name "
                    "an existing section; source_label must be a non-empty machine-verifiable "
                    "locator; and the module needs at least one visible text field, detail point, "
                    "prepared figure, or equation. priority and visual_kind are optional hints "
                    "whose defaults are derived by the runtime. Preserve every other valid "
                    "module field and all grounded content."
                )
            elif "prepared figure" in last_error and (
                "unbound" in last_error or "missing from source_label" in last_error
            ):
                correction = (
                    "Make each figure module internally consistent. If its explanation, "
                    "detail points, or takeaway discuss several prepared figures, bind every "
                    "corresponding SHA-256 and list those exact Figure N locators. Otherwise "
                    "remove claims and locators for every figure not bound to that module. "
                    "Never describe a figure that readers cannot see in the module."
                )
            elif "Rights or permission language is not supported" in last_error:
                correction = (
                    "Remove the unsupported rights or permission statement from the "
                    "affected module; do not replace it with another legal claim. Preserve "
                    "the grounded citation and scientific content."
                )
            elif (
                "has source_label" in last_error
                or "Source locator(s) are absent" in last_error
            ):
                correction = (
                    "Replace every invalid source_label with one explicit machine-verifiable "
                    'locator string such as "Figure 2", "Table 1", "Section 3.1", or '
                    '"p. 4". Every named locator must occur in the supplied source; use '
                    "the prepared figure number/page metadata when the module binds a figure. "
                    "A multi-source module may join explicit locators with semicolons. Never "
                    "use vague phrases such as cover page, respective figure pages, paper, or "
                    "results section, and do not change the grounded scientific content."
                )
            else:
                correction = (
                    "Correct the validation error with the smallest grounded edit. "
                    "Preserve valid claims, qualifiers, locators, and bindings; never "
                    "add details merely to fill the page or invent content to satisfy "
                    "the schema."
                )
            required_correction = (
                "Reduce whole-budget semantic scope as described above while "
                "preserving scientific grounding; this is not a single-field repair."
                if last_error_code == "content_capacity_exceeded"
                else (
                    "Correct the field named by the validation error while preserving "
                    "scientific grounding."
                )
            )
            request = (
                "Return a corrected evidence-budget JSON object only. The previous response "
                f"failed validation. {correction}\n\n"
                f"Required correction:\n{required_correction}\n\n"
                f"Validation error:\n{last_error}\n\n"
                "Previous invalid budget (edit this budget instead of starting over):\n"
                f"{previous_candidate or '(unavailable)'}\n\n"
                f"Original request:\n{original_request}"
            )
    raise ModelBoundaryError(
        "candidate_validation_failed",
        f"Evidence plan remained invalid after {max_repair_attempts} repair attempt(s): "
        f"{last_error}",
    )


def _normalize_generated_identifiers(
    value: dict[str, Any],
) -> dict[str, Any]:
    """Canonicalize model-owned section and module ids without editing evidence."""

    normalized = copy.deepcopy(value)
    raw_sections = normalized.get("sections")
    sections = raw_sections if isinstance(raw_sections, list) else []
    raw_section_ids = [
        str(section.get("id") or "").strip()
        for section in sections
        if isinstance(section, dict)
    ]
    unique_raw_ids = {
        section_id
        for section_id in raw_section_ids
        if section_id and raw_section_ids.count(section_id) == 1
    }
    section_aliases: dict[str, str] = {}
    section_ids: set[str] = set()
    normalized_sections: list[Any] = []
    for index, raw in enumerate(sections, start=1):
        if not isinstance(raw, dict):
            normalized_sections.append(raw)
            continue
        section = dict(raw)
        raw_id = str(section.get("id") or "").strip()
        label = " ".join(str(section.get("label") or "").split())
        section_id = _unique_generated_id(
            raw_id or label,
            fallback=f"section-{index}",
            seen=section_ids,
        )
        section["id"] = section_id
        if label:
            section["label"] = label
        normalized_sections.append(section)
        section_aliases[section_id] = section_id
        slug_alias = _generated_id_slug(raw_id, fallback="")
        if slug_alias:
            section_aliases.setdefault(slug_alias, section_id)
        if raw_id in unique_raw_ids:
            section_aliases[raw_id] = section_id
    normalized["sections"] = normalized_sections

    raw_modules = normalized.get("content_modules")
    modules = raw_modules if isinstance(raw_modules, list) else []
    module_ids: set[str] = set()
    normalized_modules: list[Any] = []
    valid_section_ids = [
        str(section.get("id") or "")
        for section in normalized_sections
        if isinstance(section, dict) and str(section.get("id") or "")
    ]
    for index, raw in enumerate(modules, start=1):
        if not isinstance(raw, dict):
            normalized_modules.append(raw)
            continue
        module = dict(raw)
        module["id"] = _unique_generated_id(
            str(module.get("id") or module.get("title") or ""),
            fallback=f"module-{index}",
            seen=module_ids,
        )
        raw_section_id = str(module.get("section_id") or "").strip()
        section_id = section_aliases.get(raw_section_id) or section_aliases.get(
            _generated_id_slug(raw_section_id, fallback="")
        )
        if section_id is None and len(valid_section_ids) == 1:
            section_id = valid_section_ids[0]
        if section_id is not None:
            module["section_id"] = section_id
        normalized_modules.append(module)
    normalized["content_modules"] = normalized_modules
    return normalized


def _unique_generated_id(value: str, *, fallback: str, seen: set[str]) -> str:
    """Return one deterministic, unique kebab-case machine identifier."""

    base = _generated_id_slug(value, fallback=fallback)
    candidate = base
    suffix = 2
    while candidate in seen:
        candidate = f"{base}-{suffix}"
        suffix += 1
    seen.add(candidate)
    return candidate


def _generated_id_slug(value: str, *, fallback: str) -> str:
    """Convert arbitrary model text to a stable ASCII machine identifier."""

    slug = re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")
    if not slug:
        return fallback
    if not slug[0].isalpha():
        prefix = fallback.split("-", 1)[0] if fallback else "item"
        slug = f"{prefix}-{slug}"
    return slug


def _sanitize_generated_copy(
    value: dict[str, Any],
    *,
    source_text: str,
) -> dict[str, Any]:
    """Normalize non-scientific boilerplate in model-generated copy fields."""

    cleaned = copy.deepcopy(value)
    modules = cleaned.get("content_modules")
    if not isinstance(modules, list):
        return cleaned
    for module in modules:
        if not isinstance(module, dict):
            continue
        for field in ("title", "text", "takeaway"):
            raw = module.get(field)
            if isinstance(raw, str):
                module[field] = poster_core.remove_unsupported_rights_claims(
                    raw, source_text
                )
        raw_points = module.get("detail_points")
        if isinstance(raw_points, list):
            cleaned_points: list[str] = []
            for point in raw_points:
                if not isinstance(point, str):
                    continue
                cleaned_point = poster_core.remove_unsupported_rights_claims(
                    point, source_text
                )
                if cleaned_point:
                    cleaned_points.append(cleaned_point)
            module["detail_points"] = cleaned_points
    return cleaned


def _prune_generated_source_labels(
    value: dict[str, Any],
    *,
    source_text: str,
) -> dict[str, Any]:
    """Remove only invalid locator parts when a valid audit locator remains."""

    modules = value.get("content_modules")
    if not isinstance(modules, list):
        return value
    for module in modules:
        if not isinstance(module, dict):
            continue
        module["source_label"] = poster_core.prune_invalid_source_locator_parts(
            str(module.get("source_label") or ""),
            source_text,
        )
        equations = module.get("equations")
        for equation in equations if isinstance(equations, list) else []:
            if isinstance(equation, dict):
                equation["source_label"] = (
                    poster_core.prune_invalid_source_locator_parts(
                        str(equation.get("source_label") or ""),
                        source_text,
                    )
                )
    return value


def _bind_generated_figure_geometry(
    value: dict[str, Any],
    *,
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replace model-guessed aspect ratios with prepared-asset geometry."""

    ratios = {
        str(asset.get("content_sha256") or ""): poster_assets.asset_aspect_ratio(asset)
        for asset in assets
        if asset.get("source_kind") == "pdf_figure"
        and str(asset.get("content_sha256") or "")
    }
    modules = value.get("content_modules")
    if not isinstance(modules, list):
        return value
    for module in modules:
        if not isinstance(module, dict):
            continue
        raw_hashes = module.get("figure_sha256s")
        if not isinstance(raw_hashes, list) or not raw_hashes:
            module.pop("figure_aspect_ratio", None)
            continue
        bound = [ratios.get(str(item)) for item in raw_hashes]
        valid = [ratio for ratio in bound if ratio is not None and ratio > 0]
        if len(valid) != len(raw_hashes):
            continue
        module["figure_aspect_ratio"] = round(
            len(valid) / sum(1.0 / ratio for ratio in valid),
            4,
        )
    return value


def _bind_generated_figure_identities(
    value: dict[str, Any],
    *,
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Retain only explicitly selected prepared figures with matching locators.

    SHA-256 values are machine-owned identities. A model may bind only hashes
    named by its explicit ``Figure N`` locator; extra and invented hashes are
    discarded. Provenance locators may cite additional source figures without
    implicitly displaying them.
    """

    hash_to_number = planning.source_figure_number_bindings(assets)
    modules = value.get("content_modules")
    if not isinstance(modules, list):
        return value
    for module in modules:
        if not isinstance(module, dict):
            continue
        source_label = str(module.get("source_label") or "")
        declared_numbers = planning.source_figure_numbers_in_label(source_label)
        raw_hashes = module.get("figure_sha256s")
        valid_hashes: list[str] = []
        if isinstance(raw_hashes, list):
            for item in raw_hashes:
                digest = str(item).strip()
                if (
                    hash_to_number.get(digest) in declared_numbers
                    and digest not in valid_hashes
                ):
                    valid_hashes.append(digest)
        module["figure_sha256s"] = valid_hashes
    return value


async def request_html(
    llm: Any,
    *,
    system: str,
    user: str,
    repair_system: str,
    repair_context: str,
    validate: Callable[[str], dict[str, Any]],
    canonicalize: Callable[[str], str] | None = None,
    initial_candidate: str | None = None,
    completed_repair_attempts: int = 0,
    max_repair_attempts: int = MAX_REPAIR_ATTEMPTS,
    initial_temperature: float = 0.2,
    timeout_seconds: float = DEFAULT_AUTHORING_TIMEOUT_SECONDS,
    max_transport_retries: int = DEFAULT_AUTHORING_TRANSPORT_RETRIES,
    on_repair_attempt: Callable[[int, int], Awaitable[None]] | None = None,
    on_invalid_candidate: Callable[[str, list[dict[str, Any]], int], Awaitable[None]]
    | None = None,
    deadline: float | None = None,
    minimum_repair_budget_seconds: float = 0.0,
) -> str:
    """Request exact complete HTML with bounded static-validator repairs."""

    request = user
    last_issues: list[dict[str, Any]] = []
    raw = ""
    start_attempt = 0
    if initial_candidate is not None:
        raw = canonicalize(initial_candidate) if canonicalize else initial_candidate
        report = validate(raw)
        if report.get("status") == "ok":
            return raw
        last_issues = _blocking_html_issues(report)
        start_attempt = completed_repair_attempts + 1
        request = _html_repair_request(
            raw=raw,
            issues=last_issues,
            repair_context=repair_context,
        )

    for attempt in range(start_attempt, max_repair_attempts + 1):
        if attempt and not _has_useful_model_budget(
            deadline,
            minimum_seconds=minimum_repair_budget_seconds,
        ):
            raise ModelBudgetDeferred(
                "llm_error",
                "HTML repair was temporarily deferred before model execution so the durable "
                "author-repair checkpoint can be resumed without exceeding the "
                "workflow delivery deadline.",
            )
        if attempt and on_repair_attempt is not None:
            await on_repair_attempt(attempt, max_repair_attempts)
        response = await _request_model_text(
            llm,
            system=system if attempt == 0 else repair_system,
            user=request,
            temperature=initial_temperature if attempt == 0 else 0.0,
            timeout_seconds=timeout_seconds,
            max_transport_retries=max_transport_retries,
            boundary_label="HTML",
            deadline=deadline,
        )
        if not isinstance(response, str):
            raw = str(response or "")
            last_issues = [
                _issue("non_text_response", "Model response must be text HTML.")
            ]
        else:
            normalized = _normalize_complete_html_response(response)
            raw = normalized if normalized is not None else response
            if normalized is not None and canonicalize is not None:
                raw = canonicalize(normalized)
            if normalized is None:
                last_issues = [
                    _issue(
                        "complete_document_required",
                        "A complete document must begin with <!doctype html>, end with "
                        "</html>, and contain no preamble or trailing commentary.",
                    )
                ]
            else:
                report = validate(raw)
                if report.get("status") == "ok":
                    return raw
                last_issues = _blocking_html_issues(report)
        if on_invalid_candidate is not None:
            await on_invalid_candidate(raw, last_issues, attempt)
        if attempt < max_repair_attempts:
            if any(
                item.get("code") in {"non_text_response", "complete_document_required"}
                for item in last_issues
            ):
                request = (
                    "Regenerate the complete HTML document from the immutable repair "
                    "manifest. "
                    "The previous response contained no usable complete document. Begin with "
                    "<!doctype html>, end with </html>, and return no Markdown or commentary.\n\n"
                    f"Immutable repair manifest:\n{repair_context}"
                )
                continue
            request = _html_repair_request(
                raw=raw,
                issues=last_issues,
                repair_context=repair_context,
            )
    message = "; ".join(str(item.get("message") or item) for item in last_issues)
    raise ModelBoundaryError(
        "candidate_validation_failed",
        f"HTML remained invalid after {max_repair_attempts} repair attempt(s): {message}",
    )


def _blocking_html_issues(report: dict[str, Any]) -> list[dict[str, Any]]:
    issues = [
        item
        for item in report.get("issues", [])
        if isinstance(item, dict) and item.get("severity") != "warning"
    ]
    return issues or [_issue("invalid_html", "HTML validation failed.")]


def _html_repair_request(
    *,
    raw: str,
    issues: list[dict[str, Any]],
    repair_context: str,
) -> str:
    grounding_guidance = authoring.repair_guidance(issues)
    return (
        "Repair the complete HTML document using every validator issue below. "
        "Return the full corrected document beginning with <!doctype html>. "
        "No Markdown fences or commentary. Preserve valid scientific content and "
        "make only the changes required by the issues.\n\n"
        f"Validator issues:\n{json.dumps(issues, ensure_ascii=False)}\n\n"
        f"{grounding_guidance}"
        f"Immutable repair manifest:\n{repair_context}\n\n"
        f"Invalid HTML:\n{raw}"
    )


async def request_stylesheet(
    llm: Any,
    *,
    system: str,
    user: str,
    apply_stylesheet: Callable[[str], str],
    validate: Callable[[str], dict[str, Any]],
    max_repair_attempts: int = MAX_REPAIR_ATTEMPTS,
    timeout_seconds: float = DEFAULT_AUTHORING_TIMEOUT_SECONDS,
    max_transport_retries: int = DEFAULT_AUTHORING_TRANSPORT_RETRIES,
    deadline: float | None = None,
) -> str:
    """Request one replacement stylesheet while deterministic code preserves content."""

    request = user
    last_issues: list[dict[str, Any]] = []
    for attempt in range(max_repair_attempts + 1):
        response = await _request_model_text(
            llm,
            system=system,
            user=request,
            temperature=0.0,
            timeout_seconds=timeout_seconds,
            max_transport_retries=max_transport_retries,
            boundary_label="Stylesheet",
            deadline=deadline,
        )
        stylesheet = _normalize_stylesheet_response(response)
        if stylesheet is None:
            last_issues = [
                _issue(
                    "single_stylesheet_required",
                    "Return exactly one <style>...</style> block with no other text.",
                )
            ]
        else:
            try:
                candidate = apply_stylesheet(stylesheet)
            except ValueError as exc:
                raise ModelBoundaryError("source_html_invalid", str(exc)) from exc
            report = validate(candidate)
            if report.get("status") == "ok":
                return candidate
            last_issues = [
                item
                for item in report.get("issues", [])
                if isinstance(item, dict) and item.get("severity") != "warning"
            ] or [_issue("invalid_html", "Revised stylesheet failed validation.")]
        if attempt < max_repair_attempts:
            request = (
                "Repair the stylesheet using every validator issue below. Return exactly "
                "one complete <style>...</style> block and no commentary. Preserve the "
                "document structure and content; solve only CSS geometry and typography.\n\n"
                f"Validator issues:\n{json.dumps(last_issues, ensure_ascii=False)}\n\n"
                f"Original request:\n{user}"
            )
    message = "; ".join(str(item.get("message") or item) for item in last_issues)
    raise ModelBoundaryError(
        "candidate_validation_failed",
        f"Stylesheet remained invalid after {max_repair_attempts} repair attempt(s): {message}",
    )


def _normalize_complete_html_response(value: str) -> str | None:
    """Extract one complete document from harmless model transport prose."""

    text = value.strip()
    lowered = text.lower()
    opening = "<!doctype html>"
    closing = "</html>"
    if lowered.count(opening) != 1 or lowered.count(closing) != 1:
        return None
    if lowered.startswith(opening) and lowered.endswith(closing):
        return text
    start = lowered.index(opening)
    end = lowered.rindex(closing) + len(closing)
    prefix = text[:start].strip()
    suffix = text[end:].strip()
    candidate = text[start:end].strip()
    fence_start = prefix.rfind("```")
    if fence_start >= 0:
        fence = prefix[fence_start:].strip().lower()
        introduction = prefix[:fence_start].strip()
        if fence not in {"```", "```html"} or not suffix.startswith("```"):
            return None
        if not _plain_transport_text(introduction):
            return None
        if not _plain_transport_text(suffix[3:].strip()):
            return None
    elif not _plain_transport_text(prefix) or not _plain_transport_text(suffix):
        return None
    return candidate if candidate.lower().startswith(opening) else None


def _plain_transport_text(value: str) -> bool:
    """Allow discardable prose but reject surrounding markup or nested fences."""

    return "<" not in value and ">" not in value and "```" not in value


def _normalize_stylesheet_response(value: Any) -> str | None:
    """Extract one style element; all surrounding model text is discarded."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    lowered = text.lower()
    if "<style" not in lowered and "</style>" not in lowered:
        css = _unwrap_bare_css(text)
        return f"<style>\n{css}\n</style>" if css is not None else None
    if lowered.count("<style") != 1 or lowered.count("</style>") != 1:
        return None
    start = lowered.index("<style")
    end = lowered.rindex("</style>") + len("</style>")
    candidate = text[start:end].strip()
    opening_end = candidate.find(">")
    return candidate if opening_end >= len("<style") else None


def _unwrap_bare_css(text: str) -> str | None:
    """Normalize a CSS-only response; the host still validates the applied document."""

    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        first_newline = candidate.find("\n")
        if first_newline < 0:
            return None
        fence = candidate[:first_newline].strip().lower()
        if fence not in {"```", "```css"}:
            return None
        candidate = candidate[first_newline + 1 : -3].strip()
    if not candidate or "```" in candidate:
        return None
    return candidate


def parse_evidence_budget_response(value: str) -> dict[str, Any]:
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
    timeout_seconds: float,
    max_transport_retries: int,
    boundary_label: str,
    deadline: float | None = None,
) -> Any:
    for retry_index in range(max_transport_retries + 1):
        call_timeout = _remaining_timeout(
            timeout_seconds,
            deadline=deadline,
            boundary_label=boundary_label,
        )
        try:
            call = asyncio.create_task(llm.chat(system, user, temperature=temperature))
            done, _pending = await asyncio.wait({call}, timeout=call_timeout)
            if call in done:
                return call.result()
            call.cancel()
            call.add_done_callback(_consume_model_task)
            raise TimeoutError
        except TimeoutError as exc:
            if retry_index < max_transport_retries:
                continue
            raise ModelBoundaryError(
                "llm_error",
                f"{boundary_label} model call timed out after {call_timeout:g} seconds",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - host LLM is an external boundary
            if retry_index < max_transport_retries and _is_transient_model_error(exc):
                continue
            raise ModelBoundaryError(
                "llm_error",
                f"{boundary_label} model call failed: {_model_error_detail(exc)}",
            ) from exc
    raise AssertionError("model transport retry loop exhausted without a result")


def _monotonic() -> float:
    """Read the active event loop's monotonic clock."""

    return asyncio.get_running_loop().time()


def _remaining_timeout(
    timeout_seconds: float,
    *,
    deadline: float | None,
    boundary_label: str,
) -> float:
    """Cap one transport attempt to the shared portable runtime deadline."""

    if deadline is None:
        return timeout_seconds
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise ModelBoundaryError(
            "llm_error",
            f"{boundary_label} runtime budget was exhausted before the model call.",
        )
    return min(timeout_seconds, remaining)


def _has_useful_model_budget(
    deadline: float | None,
    *,
    minimum_seconds: float,
) -> bool:
    """Return whether a new model call has enough bounded time to be useful."""

    if deadline is None or minimum_seconds <= 0:
        return True
    return deadline - _monotonic() >= minimum_seconds


def _consume_model_task(task: asyncio.Task[Any]) -> None:
    """Consume a late transport result after a hard skill-level timeout."""

    try:
        task.result()
    except BaseException:  # noqa: BLE001 - cancellation belongs to the transport boundary
        pass


def _downgrade_unbound_source_figure_focus(
    budget: dict[str, Any],
) -> dict[str, Any]:
    if str(budget.get("focal_role") or "") != "source-figure":
        return budget
    modules = budget.get("content_modules")
    if isinstance(modules, list) and any(
        isinstance(module, dict)
        and module.get("priority") == "focal"
        and isinstance(module.get("figure_sha256s"), list)
        and any(str(value).strip() for value in module["figure_sha256s"])
        for module in modules
    ):
        return budget
    downgraded = copy.deepcopy(budget)
    downgraded["focal_role"] = "result"
    downgraded_modules = downgraded.get("content_modules")
    if isinstance(downgraded_modules, list):
        for module in downgraded_modules:
            if isinstance(module, dict) and module.get("priority") == "focal":
                module["visual_kind"] = "comparison"
                break
    return downgraded


def _is_transient_model_error(error: BaseException) -> bool:
    message = _model_error_detail(error).casefold()
    return any(marker in message for marker in _TRANSIENT_MODEL_ERROR_MARKERS)


def _model_error_detail(error: BaseException) -> str:
    """Keep transport failures diagnosable even when their message is empty."""

    error_type = type(error).__name__
    message = str(error).strip()
    return f"{error_type}: {message}" if message else error_type


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message, "severity": "error"}
