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

from . import runtime_budget, workflow_outcomes

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
    if action in {
        poster_core.ACTION_DRAFT, poster_core.ACTION_ESTIMATE, poster_core.ACTION_REVISE
    }:
        try:
            repair_attempts(data)
            authoring_transport_options(data)
            runtime_budget.workflow_timeout_seconds(data)
        except model_runtime.ModelBoundaryError as exc:
            return None, workflow_outcomes.error_result(exc.code, str(exc))
    return action, None


def normalize_poster_input(input_data: dict[str, Any]) -> dict[str, Any]:
    """Normalize only explicit structured controls at the Skill boundary."""

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
    page = normalized.get("page")
    if isinstance(page, Mapping) and str(page.get("format") or "").upper() == "A0":
        page_orientation = _ORIENTATION_VALUES.get(
            str(page.get("orientation") or normalized.get("orientation") or "portrait")
            .strip()
            .lower()
        )
        if page_orientation in {"landscape", "portrait"}:
            normalized["page"] = (
                dict(_A0_LANDSCAPE_PAGE)
                if page_orientation == "landscape"
                else dict(_A0_PORTRAIT_PAGE)
            )
            normalized["orientation"] = page_orientation
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


def repair_attempts(input_data: dict[str, Any]) -> int:
    """Return the validation repair allowance, separate from transport retries."""

    value = input_data.get("max_repair_attempts", model_runtime.MAX_REPAIR_ATTEMPTS)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
        raise model_runtime.ModelBoundaryError(
            "invalid_payload", "max_repair_attempts must be an integer between 0 and 10"
        )
    return value


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
