"""Pure text/DOT helpers for artifact follow-up turns.

These functions have no dependency on ``OmniAgent`` state; they turn attached
figure sources (DOT) and session focus into small display/answer/payload
fragments. They live here so the orchestrator stays focused on the request
flow rather than artifact string wrangling.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from omni.runtime.artifact_contracts import contract_for_path
from omni.runtime.session_focus import ActiveTarget


def artifact_title_from_focus(target: ActiveTarget, source_text: str) -> str:
    focus_title = str(getattr(target.focus, "artifact_title", "") or "").strip()
    if focus_title:
        return focus_title
    result_owner = target.workflow_step or target.skill_execution
    if result_owner is not None and isinstance(result_owner.result_json, dict):
        summary = str(
            result_owner.result_json.get("title")
            or result_owner.result_json.get("summary")
            or ""
        ).strip()
        if summary:
            return summary[:80]
    match = re.search(r'label\s*=\s*"([^"]+)"', source_text)
    return match.group(1) if match else "Current figure"


def dot_labels(source_text: str) -> list[str]:
    return [match.group(1).replace("\\n", " ") for match in re.finditer(r'label\s*=\s*"([^"]+)"', source_text)]


def revision_constraints(
    nodes: list[Any], edges: list[Any], *, allow_simplification: bool = False
) -> dict[str, Any]:
    """The anti-hallucination contract handed to a figure redraw.

    Deterministic guardrail: the redraw must preserve/expand the source and must
    not collapse into a generic template. ``allow_simplification`` (default
    ``False`` — the safest posture) is a *semantic* parameter that belongs to the
    model, never to a runtime keyword scan; when set it relaxes the size floor.
    """
    return {
        "preserve_source_structure": True,
        "expand_from_source": True,
        "reject_generic_template": True,
        "require_rendered_derivatives": True,
        "allow_simplification": allow_simplification,
        "min_nodes": 0 if allow_simplification else len(nodes),
        "min_edges": 0 if allow_simplification else len(edges),
    }


def artifact_revision_source_payload(
    source_path: Path, source_text: str, *, allow_simplification: bool = False
) -> dict[str, Any]:
    contract = contract_for_path(source_path)
    elements = contract.extract_elements(source_text) if contract is not None else []
    nodes = [
        {
            "id": str(getattr(element, "id", "") or ""),
            "label": str(getattr(element, "label", "") or ""),
            "kind": str(getattr(element, "kind", "") or ""),
        }
        for element in elements
        if str(getattr(element, "id", "") or getattr(element, "label", "") or "")
    ]
    edges = [
        {"from": left, "to": right}
        for left, right in re.findall(r"([A-Za-z_][\w]*)\s*->\s*([A-Za-z_][\w]*)", source_text or "")
    ]
    return {
        "revision_mode": "major",
        "source_artifact_dot": source_text,
        "source_artifact_title": artifact_title_from_dot(source_text),
        "source_nodes": nodes[:80],
        "source_edges": edges[:120],
        "revision_constraints": revision_constraints(nodes, edges, allow_simplification=allow_simplification),
    }


def artifact_title_from_dot(source_text: str) -> str:
    match = re.search(r"graph\s*\[[^\]]*label\s*=\s*\"([^\"]+)\"", source_text or "", flags=re.DOTALL)
    if match:
        return match.group(1)
    labels = dot_labels(source_text)
    return labels[0] if labels else "Current figure"
