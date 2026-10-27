"""Normalize scientific-poster host requests before workflow orchestration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import poster_contract

from posterlib.generation import model_runtime
from posterlib.sources import source_preparation
from posterlib.visual import design_direction, reference_analysis

from . import deadlines, outcomes

_A0_LANDSCAPE_PAGE = {"width_mm": 1189.0, "height_mm": 841.0}
_A0_PORTRAIT_PAGE = {"width_mm": 841.0, "height_mm": 1189.0}
_ORIENTATION_VALUES = {
    "auto": "auto",
    "landscape": "landscape",
    "portrait": "portrait",
}


def validate_action_boundary(
    data: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Normalize an action and enforce required inputs at every host entry point."""

    try:
        action = poster_contract.normalize_action(data.get("action"))
    except ValueError as exc:
        return None, outcomes.error_result("invalid_action", str(exc))
    if action in {
        poster_contract.ACTION_DRAFT,
        poster_contract.ACTION_ESTIMATE,
    } and not source_preparation.has_draft_source(data):
        return None, outcomes.error_result(
            "missing_input",
            "A paper PDF, LaTeX project, arXiv id, complete paper text, or grounded poster brief is required.",
        )
    if action == poster_contract.ACTION_REVISE and not (
        str(data.get("source_html_uri") or "").strip()
        and (
            source_preparation.authoring_request(data)
            or str(data.get("feedback") or "").strip()
            or data.get("visual_preferences")
            or str(data.get("visual_review_path") or "").strip()
        )
    ):
        return None, outcomes.error_result(
            "missing_input",
            "source_html_uri plus current direction, visual_preferences, or visual_review_path are required.",
        )
    if action in {
        poster_contract.ACTION_DRAFT, poster_contract.ACTION_ESTIMATE, poster_contract.ACTION_REVISE
    }:
        try:
            repair_attempts(data)
            authoring_transport_options(data)
            author_model_role(data)
            deadlines.workflow_timeout_seconds(data)
        except model_runtime.ModelBoundaryError as exc:
            return None, outcomes.error_result(exc.code, str(exc))
    return action, None


def normalize_poster_input(input_data: dict[str, Any]) -> dict[str, Any]:
    """Normalize only explicit structured controls at the Skill boundary."""

    normalized = dict(input_data)
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


def repair_attempts(input_data: dict[str, Any]) -> int | None:
    """Return an explicit validation-repair cap, when the caller supplied one."""

    value = input_data.get("max_repair_attempts")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
        raise model_runtime.ModelBoundaryError(
            "invalid_payload", "max_repair_attempts must be an integer between 0 and 10"
        )
    return value


def author_model_role(input_data: Mapping[str, Any]) -> str:
    """Return the explicitly selected canonical model role for authoring."""

    role = str(input_data.get("author_model_role") or "main").strip().lower()
    if role not in {"main", "vision"}:
        raise model_runtime.ModelBoundaryError(
            "invalid_payload", "author_model_role must be main or vision"
        )
    return role


def effective_authoring_request(
    input_data: dict[str, Any],
    stored: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Resolve current author settings and the explicit direction still to execute."""

    original = str(stored.get("authoring_request") or "")
    direction = source_preparation.authoring_request(input_data)
    feedback = "\n\n".join(dict.fromkeys(
        value for value in (
            direction if direction != original else "",
            str(input_data.get("feedback") or "").strip(),
        ) if value
    ))
    preferences = reference_analysis.normalize_preferences(stored.get("visual_preferences"))
    updates = input_data.get("visual_preferences")
    if updates is not None:
        reference_analysis.normalize_preferences(updates)
        preferences = reference_analysis.normalize_preferences({**preferences, **updates})
    effective = {
        "authoring_request": design_direction.update_user_direction(original, feedback),
        "visual_preferences": preferences,
        "author_model_role": author_model_role({
            "author_model_role": input_data.get(
                "author_model_role", stored.get("author_model_role", "main"),
            ),
        }),
    }
    delta = feedback
    if preferences != stored.get("visual_preferences", {}):
        delta = "\n\n".join(filter(None, (
            delta, "Current visual preferences:\n" + json.dumps(preferences, ensure_ascii=False),
        )))
    return effective, delta


def authoring_transport_options(input_data: dict[str, Any]) -> tuple[float | None, int]:
    """Return the explicit authoring timeout and transient retry settings."""

    timeout = deadlines.optional_timeout_seconds(input_data, "authoring_timeout_seconds")

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
