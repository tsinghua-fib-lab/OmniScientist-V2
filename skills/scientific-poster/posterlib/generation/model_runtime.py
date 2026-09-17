"""Bounded text-model calls for scientific-poster planning and authoring."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Callable
from typing import Any

from posterlib.content import planning

from . import authoring

EVIDENCE_PLANNING_TIMEOUT_SECONDS = 240
# The workflow deadline is the authoritative bound.  Keep the transport guard
# longer than the local workflow budget so a complete grounded HTML revision is
# not cancelled by a second, shorter timer; _remaining_timeout still caps every
# call to the actual remaining workflow time.
DEFAULT_AUTHORING_TIMEOUT_SECONDS = 900.0
MAX_AUTHORING_TIMEOUT_SECONDS = 900.0
DEFAULT_AUTHORING_TRANSPORT_RETRIES = 0
MAX_AUTHORING_TRANSPORT_RETRIES = 2
MAX_REPAIR_ATTEMPTS = 3

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
class ModelBoundaryError(ValueError):
    """A host model response could not satisfy its output contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
    previous_candidate = ""
    best_overfull_budget: dict[str, Any] | None = None
    best_overfull_occupancy = float("inf")
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
            parsed = _bind_generated_figure_identities(parsed, assets=assets)
            parsed = planning.bind_prepared_figure_geometry(parsed, assets=assets)
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
                source_figure_sha256s=source_figure_sha256s,
            )
            page_plan = planning.estimate_page(
                budget,
                page=page,
                orientation=orientation,
            )
        except (ModelBoundaryError, planning.PlanningError) as exc:
            if isinstance(exc, planning.PlanningError) and exc.code == "invalid_page":
                raise
            last_error = str(exc)
        else:
            fixed_page_overfull = (
                page is not None and page_plan.predicted_occupancy > 1.0
            )
            if not fixed_page_overfull:
                return budget
            if page_plan.predicted_occupancy < best_overfull_occupancy:
                best_overfull_budget = budget
                best_overfull_occupancy = page_plan.predicted_occupancy
            last_error = (
                "content_budget_overfull: estimated one-page occupancy is "
                f"{page_plan.predicted_occupancy:.0%} on the fixed page. Reduce it to "
                "100% or less with semantic edits: merge repeated interpretations, "
                "replace secondary visual channels with lighter grounded summaries, and "
                "omit redundant figures. Preserve the central claim, key method, decisive "
                "evidence, qualifiers, and source bindings; do not mechanically truncate "
                "copy or shrink figure intent."
            )
            if attempt >= max_repair_attempts:
                assert best_overfull_budget is not None
                return best_overfull_budget
        if attempt < max_repair_attempts:
            correction = (
                "Correct the reported planning issue with the smallest grounded edit. "
                "Preserve valid claims, qualifiers, provenance, and bindings; never "
                "add details merely to fill the page or invent content to satisfy "
                "the schema."
            )
            request = (
                "Return a corrected evidence-budget JSON object only. The previous response "
                f"failed validation. {correction}\n\n"
                f"Validation error:\n{last_error}\n\n"
                "Previous invalid budget (edit this budget instead of starting over):\n"
                f"{previous_candidate or '(unavailable)'}"
                f"\n\nOriginal request:\n{original_request}"
            )
    if best_overfull_budget is not None:
        return best_overfull_budget
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


def _bind_generated_figure_identities(
    value: dict[str, Any],
    *,
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Retain only explicitly selected hashes from the prepared figure manifest."""

    prepared_hashes = {
        str(asset.get("content_sha256") or "")
        for asset in assets
        if asset.get("source_kind") == "pdf_figure"
    }
    modules = value.get("content_modules")
    if not isinstance(modules, list):
        return value
    for module in modules:
        if not isinstance(module, dict):
            continue
        raw_hashes = module.get("figure_sha256s")
        valid_hashes: list[str] = []
        if isinstance(raw_hashes, list):
            for item in raw_hashes:
                digest = str(item).strip()
                if digest in prepared_hashes and digest not in valid_hashes:
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
    max_repair_attempts: int = MAX_REPAIR_ATTEMPTS,
    initial_temperature: float = 0.2,
    timeout_seconds: float = DEFAULT_AUTHORING_TIMEOUT_SECONDS,
    max_transport_retries: int = DEFAULT_AUTHORING_TRANSPORT_RETRIES,
    deadline: float | None = None,
) -> str:
    """Request exact complete HTML with bounded static-validator repairs."""

    request = user
    last_issues: list[dict[str, Any]] = []
    raw = ""
    for attempt in range(max_repair_attempts + 1):
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
    return (
        "Repair the complete HTML document using every validator issue below. "
        "Return the full corrected document beginning with <!doctype html>. "
        "No Markdown fences or commentary. Preserve valid scientific content and "
        "make only the changes required by the issues.\n\n"
        f"Validator issues:\n{json.dumps(issues, ensure_ascii=False)}\n\n"
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
    """Request one compact stylesheet override while deterministic code preserves content."""

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
                "one complete <style>...</style> block containing only corrective override "
                "rules and no commentary. Do not reproduce the base stylesheet or HTML. "
                "Preserve the document structure and content; solve only CSS geometry and "
                "typography.\n\n"
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
        except Exception as exc:
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


def _consume_model_task(task: asyncio.Task[Any]) -> None:
    """Consume a late transport result after a hard skill-level timeout."""

    if task.cancelled():
        return
    task.exception()


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
