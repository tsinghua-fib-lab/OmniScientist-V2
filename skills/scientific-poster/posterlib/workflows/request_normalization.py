"""Normalize scientific-poster host requests before workflow orchestration."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

import poster_core

from posterlib.generation import model_runtime
from posterlib.sources import source_runtime

from . import workflow_outcomes

_PAGE_DIMENSIONS_PATTERN = re.compile(
    r"(?P<width>\d+(?:\.\d+)?)\s*(?:mm)?\s*[x×*]\s*"
    r"(?P<height>\d+(?:\.\d+)?)\s*mm\b",
    re.IGNORECASE,
)
_OUTPUT_DIR_PATTERN = re.compile(
    r"output[_ -]?(?:dir|directory)\s*[=:]\s*(?P<path>[^\s,;，；]+)",
    re.IGNORECASE,
)
_QUOTED_PDF_PATH_PATTERN = re.compile(
    r"""(?P<quote>[`"'])(?P<path>(?:file://|/|\./|\.\./|~/|[A-Za-z]:[\\/]).+?\.pdf)(?P=quote)""",
    re.IGNORECASE,
)
_BARE_PDF_PATH_PATTERN = re.compile(
    r"""(?<!\S)(?P<path>(?:file://|/|\./|\.\./|~/|[A-Za-z]:[\\/])[^\s`"',;，；。)\]}]+\.pdf)(?=$|[\s,;，；。)\]}])""",
    re.IGNORECASE,
)
_A0_LANDSCAPE_PAGE = {"width_mm": 1189.0, "height_mm": 841.0}
_A0_PORTRAIT_PAGE = {"width_mm": 841.0, "height_mm": 1189.0}
_ORIENTATION_VALUES = {
    "auto": "auto",
    "horizontal": "landscape",
    "landscape": "landscape",
    "portrait": "portrait",
    "vertical": "portrait",
}


def validate_action_boundary(
    data: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Normalize an action and enforce required inputs at every host entry point."""

    try:
        action = poster_core.normalize_action(data.get("action"))
    except ValueError as exc:
        return None, workflow_outcomes.error_result("invalid_action", str(exc))
    if action in {
        poster_core.ACTION_DRAFT,
        poster_core.ACTION_ESTIMATE,
    } and not source_runtime.has_draft_source(data):
        return None, workflow_outcomes.error_result(
            "missing_input",
            "A local PDF, complete paper text, or grounded poster brief is required.",
        )
    if action == poster_core.ACTION_REVISE and not (
        str(data.get("source_html_uri") or "").strip()
        and (
            str(data.get("feedback") or "").strip()
            or str(data.get("visual_review_path") or "").strip()
        )
    ):
        return None, workflow_outcomes.error_result(
            "missing_input",
            "source_html_uri plus feedback or visual_review_path are required.",
        )
    return action, None


def normalize_poster_input(input_data: dict[str, Any]) -> dict[str, Any]:
    """Fill missing poster controls from a natural-language Omni request."""

    normalized = dict(input_data)
    embedded = _embedded_control_payload(normalized.get("input"))
    if embedded is not None:
        normalized.pop("input", None)
        normalized = {**embedded, **normalized}
    raw_action = str(normalized.get("action") or "").strip().lower()
    if raw_action in {"", poster_core.ACTION_DRAFT}:
        inferred_action = _infer_continuation_action(normalized)
        if inferred_action:
            normalized["action"] = inferred_action
    raw_orientation = normalized.get("orientation")
    if isinstance(raw_orientation, str):
        canonical_orientation = _ORIENTATION_VALUES.get(raw_orientation.strip().lower())
        if canonical_orientation is not None:
            normalized["orientation"] = canonical_orientation
    request = source_runtime.authoring_request(normalized)
    if not request:
        return normalized

    if _may_infer_draft_pdf(normalized):
        inferred_pdf = _single_pdf_path(request)
        if inferred_pdf:
            normalized["paper_path"] = inferred_pdf

    if not str(normalized.get("action") or "").strip() and _requests_estimate_only(
        request
    ):
        normalized["action"] = poster_core.ACTION_ESTIMATE

    if not normalized.get("page"):
        dimensions = _PAGE_DIMENSIONS_PATTERN.search(request)
        if dimensions is not None:
            normalized["page"] = {
                "width_mm": float(dimensions.group("width")),
                "height_mm": float(dimensions.group("height")),
            }
        elif re.search(r"\bA0\b", request, re.IGNORECASE):
            normalized["page"] = (
                dict(_A0_LANDSCAPE_PAGE)
                if _requests_landscape(request)
                else dict(_A0_PORTRAIT_PAGE)
            )
        elif _requests_landscape(request):
            normalized.setdefault("orientation", "landscape")
        elif _requests_portrait(request):
            normalized.setdefault("orientation", "portrait")
        else:
            normalized.setdefault("orientation", "auto")

    raw_preferences = normalized.get("visual_preferences")
    preferences = dict(raw_preferences) if isinstance(raw_preferences, Mapping) else {}
    if "visual_preferences" not in normalized:
        typography = re.search(
            r"\b(neutral-sans|scholarly-serif|hybrid)\b",
            request,
            re.IGNORECASE,
        )
        if typography is not None:
            preferences["typography"] = typography.group(1).lower()
        if re.search(r"\b(?:no borders?|unframed)\b", request, re.IGNORECASE):
            preferences["framing"] = "unframed"
        elif re.search(r"\b(?:border|frame|outline)\b", request, re.IGNORECASE):
            preferences["framing"] = "section-outline"
        accent = re.search(
            r"accent(?:\s+color)?\s*[:=]?\s*(#[0-9a-f]{6})\b",
            request,
            re.IGNORECASE,
        )
        if accent is not None:
            preferences["accent_color"] = accent.group(1).lower()
    if preferences:
        normalized["visual_preferences"] = preferences

    if not str(normalized.get("output_dir") or "").strip():
        output_dir = _OUTPUT_DIR_PATTERN.search(request)
        if output_dir is not None:
            normalized["output_dir"] = (
                output_dir.group("path").strip("`'\"").rstrip(".,;:，；。)]}")
            )
    return normalized


def _embedded_control_payload(value: Any) -> dict[str, Any] | None:
    """Unpack one closed JSON object carried by Omni's explicit skill boundary."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    explicit = re.match(
        r"^(?:run_skill|use_skill)\s+scientific-poster\b",
        candidate,
        re.IGNORECASE,
    )
    if explicit is not None:
        candidate = candidate[explicit.end() :].strip()
    if not candidate.startswith("{"):
        return None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or any(
        not isinstance(key, str) or key.startswith("_") for key in payload
    ):
        return None
    try:
        poster_core.normalize_action(payload.get("action"))
    except ValueError:
        return None
    return dict(payload)


def _infer_continuation_action(data: Mapping[str, Any]) -> str:
    """Recover an unambiguous revision verb omitted by a host planner."""

    source_html_uri = str(data.get("source_html_uri") or "").strip()
    has_revision_direction = bool(
        str(data.get("feedback") or "").strip()
        or str(data.get("visual_review_path") or "").strip()
    )
    has_approval_direction = bool(
        data.get("approved") is True
        or str(data.get("operator_confirmation") or "").strip()
        or str(data.get("session_id") or "").strip()
    )
    if source_html_uri and has_revision_direction and not has_approval_direction:
        return poster_core.ACTION_REVISE
    return ""


def _may_infer_draft_pdf(data: Mapping[str, Any]) -> bool:
    """Allow explicit invocation text to carry one unambiguous local PDF path."""

    action = str(data.get("action") or "").strip().lower()
    return action in {
        "",
        poster_core.ACTION_DRAFT,
        poster_core.ACTION_ESTIMATE,
    } and not any(
        data.get(field) not in (None, "", {}, [])
        for field in (
            "paper_path",
            "file_uri",
            "source",
            "source_text",
            "research",
        )
    )


def _single_pdf_path(request: str) -> str:
    """Extract one explicit path-like PDF token without treating prose as evidence."""

    candidates = {
        match.group("path").strip()
        for match in _BARE_PDF_PATH_PATTERN.finditer(request)
    }
    candidates.update(
        path
        for match in _QUOTED_PDF_PATH_PATTERN.finditer(request)
        if (path := match.group("path").strip())
        and len(re.findall(r"\.pdf\b", path, re.IGNORECASE)) == 1
    )
    return next(iter(candidates)) if len(candidates) == 1 else ""


def authoring_transport_options(input_data: dict[str, Any]) -> tuple[float, int]:
    """Return bounded HTML authoring timeout and transient retry settings."""

    raw_timeout = input_data.get(
        "authoring_timeout_seconds",
        model_runtime.DEFAULT_AUTHORING_TIMEOUT_SECONDS,
    )
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        raise model_runtime.ModelBoundaryError(
            "invalid_payload", "authoring_timeout_seconds must be a number"
        )
    timeout = float(raw_timeout)
    if (
        not math.isfinite(timeout)
        or not 0 < timeout <= model_runtime.MAX_AUTHORING_TIMEOUT_SECONDS
    ):
        raise model_runtime.ModelBoundaryError(
            "invalid_payload",
            f"authoring_timeout_seconds must be greater than 0 and at most "
            f"{model_runtime.MAX_AUTHORING_TIMEOUT_SECONDS:g}",
        )

    retries = input_data.get(
        "authoring_transport_retries",
        model_runtime.DEFAULT_AUTHORING_TRANSPORT_RETRIES,
    )
    if isinstance(retries, bool) or not isinstance(retries, int):
        raise model_runtime.ModelBoundaryError(
            "invalid_payload", "authoring_transport_retries must be an integer"
        )
    if not 0 <= retries <= model_runtime.MAX_AUTHORING_TRANSPORT_RETRIES:
        raise model_runtime.ModelBoundaryError(
            "invalid_payload",
            f"authoring_transport_retries must be between 0 and "
            f"{model_runtime.MAX_AUTHORING_TRANSPORT_RETRIES}",
        )
    return timeout, retries


def _requests_landscape(request: str) -> bool:
    return bool(re.search(r"\b(?:landscape|horizontal)\b", request, re.IGNORECASE))


def _requests_estimate_only(request: str) -> bool:
    """Recognize an explicit planning-only request without guessing from paper prose."""

    return bool(
        re.search(r"\baction\s*[:=]?\s*estimate\b", request, re.IGNORECASE)
        or re.search(
            r"\b(?:estimate(?:\s*/\s*planning)?|planning)\s+stage\s+only\b",
            request,
            re.IGNORECASE,
        )
    )


def _requests_portrait(request: str) -> bool:
    return bool(re.search(r"\b(?:portrait|vertical)\b", request, re.IGNORECASE))
