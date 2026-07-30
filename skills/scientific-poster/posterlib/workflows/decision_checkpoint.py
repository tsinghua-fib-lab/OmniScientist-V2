"""Portable user-decision requests for exhausted automatic visual repair."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

SCHEMA = "scientific-poster.decision-request.v2"
_REASONS = frozenset(
    {
        "automatic-revision-invalid",
        "automatic-revision-noop",
        "automatic-revision-regressed",
        "automatic-revision-exhausted",
        "reviewer-unavailable",
        "workflow-budget-exhausted",
    }
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def build_request(
    result: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Describe a non-blocking choice that a later normal invocation can apply."""

    normalized_reason = str(reason).strip()
    if normalized_reason not in _REASONS:
        raise ValueError(f"unsupported poster decision reason: {normalized_reason}")
    html_uri = str(result.get("html_uri") or "").strip()
    html_sha256 = str(result.get("html_sha256") or "").strip()
    if not html_uri or _HASH_RE.fullmatch(html_sha256) is None:
        raise ValueError("poster decision requires exact HTML identity")
    continuation = {
        "action": "revise",
        "source_html_uri": html_uri,
        "source_html_sha256": html_sha256,
    }
    raw_modules = (result.get("content_budget") or {}).get("content_modules")
    grounded_module_ids = (
        [
            str(module.get("id") or "").strip()
            for module in raw_modules
            if isinstance(module, Mapping) and str(module.get("id") or "").strip()
        ]
        if isinstance(raw_modules, list)
        else []
    )
    options = [
        {
            "id": "revise",
            "label": "Revise layout with feedback",
            "requires": ["feedback"],
            "continuation": {
                **continuation,
                "revision_mode": "full-layout",
            },
        }
    ]
    if grounded_module_ids:
        options.append(
            {
                "id": "replan-grounded-copy",
                "label": "Compress selected grounded modules",
                "requires": ["feedback", "content_replan_targets"],
                "continuation": {
                    **continuation,
                    "revision_mode": "content-replan",
                },
            }
        )
    options.append(
        {
            "id": "keep-draft",
            "label": "Keep current draft",
            "requires": [],
        }
    )
    unsigned = {
        "schema": SCHEMA,
        "blocking": False,
        "reason": normalized_reason,
        "prompt": (
            "Automatic visual repair could not safely choose between changing the "
            "current poster and preserving it."
        ),
        "target": {
            "html_uri": html_uri,
            "html_sha256": html_sha256,
            "preview_uri": str(result.get("preview_uri") or ""),
            "grounded_module_ids": grounded_module_ids,
        },
        "options": options,
    }
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {
        **unsigned,
        "request_id": hashlib.sha256(
            b"scientific-poster-decision-v2\0" + payload
        ).hexdigest(),
    }


__all__ = ["SCHEMA", "build_request"]
