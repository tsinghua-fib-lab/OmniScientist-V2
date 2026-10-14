"""Closed contract for scientific-poster approval receipts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_RE = re.compile(r"^[0-9a-f]{32}$")
_UTC_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?Z$"
)


class ApprovalContractError(ValueError):
    """An approval receipt violates its closed contract."""


def validate_approval_receipt(value: object) -> dict[str, Any]:
    """Validate one approval receipt against the supported schema."""

    if not isinstance(value, dict):
        raise ApprovalContractError("approval receipt must be an object")
    required = {
        "schema",
        "source_html_uri",
        "source_html_origin_uri",
        "source_html_sha256",
        "grounding_source_sha256",
        "source_figure_manifest_sha256",
        "visual_review_sha256",
        "approved",
        "approved_at",
        "session_id",
        "decision",
    }
    _exact_fields(value, required, label="approval receipt")
    if value["schema"] != "scientific-poster.poster-approval.v2":
        raise ApprovalContractError("unsupported approval schema")
    if value["source_html_uri"] != "bundle:poster.html":
        raise ApprovalContractError("approval source_html_uri must identify bundle:poster.html")
    if (
        not isinstance(value["source_html_origin_uri"], str)
        or not value["source_html_origin_uri"].strip()
        or len(value["source_html_origin_uri"]) > 2048
    ):
        raise ApprovalContractError("approval source_html_origin_uri is invalid")
    if not isinstance(value["source_html_sha256"], str) or not _HASH_RE.fullmatch(
        value["source_html_sha256"]
    ):
        raise ApprovalContractError("invalid approval HTML hash")
    if not isinstance(value["grounding_source_sha256"], str) or not _HASH_RE.fullmatch(
        value["grounding_source_sha256"]
    ):
        raise ApprovalContractError("invalid approval grounding hash")
    if not isinstance(
        value["source_figure_manifest_sha256"], str
    ) or not _HASH_RE.fullmatch(value["source_figure_manifest_sha256"]):
        raise ApprovalContractError("invalid approval source-figure manifest hash")
    if not isinstance(value["visual_review_sha256"], str) or not _HASH_RE.fullmatch(
        value["visual_review_sha256"]
    ):
        raise ApprovalContractError("invalid approval visual-review hash")
    if value["approved"] is not True:
        raise ApprovalContractError("approval receipt must record literal approval")
    if not _is_utc_rfc3339(value["approved_at"]):
        raise ApprovalContractError("approval timestamp must be UTC")
    if not _bounded_string(value["session_id"], limit=256):
        raise ApprovalContractError("approval session_id is required")
    if not isinstance(value["decision"], dict):
        raise ApprovalContractError("approval decision must be an object")
    decision = value["decision"]
    decision_fields = {
        "mode",
        "session_id",
        "host_event_id",
        "target_kind",
        "target_sha256",
        "user_message_sha256",
        "event_sha256",
    }
    _exact_fields(decision, decision_fields, label="approval decision")
    if decision["mode"] != "portable-operator":
        raise ApprovalContractError("approval decision mode must be portable-operator")
    if not _bounded_string(decision["session_id"], limit=256):
        raise ApprovalContractError("approval decision session_id is required")
    if not isinstance(decision["host_event_id"], str) or not _EVENT_RE.fullmatch(
        decision["host_event_id"]
    ):
        raise ApprovalContractError("invalid approval host_event_id")
    if decision["target_kind"] != "poster":
        raise ApprovalContractError("approval decision target_kind must be poster")
    for name in ("target_sha256", "user_message_sha256", "event_sha256"):
        if not isinstance(decision[name], str) or not _HASH_RE.fullmatch(
            decision[name]
        ):
            raise ApprovalContractError(f"invalid approval decision hash: {name}")
    return dict(value)


def _is_utc_rfc3339(value: object) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or _UTC_RFC3339_RE.fullmatch(value) is None
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _bounded_string(value: object, *, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _exact_fields(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ApprovalContractError(f"{label} fields differ; missing={missing}, extra={extra}")


__all__ = ["ApprovalContractError", "validate_approval_receipt"]
