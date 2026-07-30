"""Task contracts and workflow DAG materialization for complex turns.

These structures are intentionally plain dictionaries at the IntentPlan
boundary. They are durable, renderable, and easy to store in SQLite JSON
columns, while keeping the planner/runtime split clear:

- planner/model proposes capabilities and steps
- registry resolves concrete skills
- runtime validates and executes the resulting DAG
"""

from __future__ import annotations

import copy
from typing import Any

TASK_CONTRACT_SCHEMA_VERSION = 2


def build_task_contract(
    *,
    objective: str,
    deliverables: list[str],
    capabilities: list[str],
    workflow_steps: list[dict[str, Any]],
    provenance_mode: str = "light",
    confidence: float = 0.0,
) -> dict[str, Any]:
    """Return the durable contract for a multi-step task.

    A contract is only created for workflow/long-running turns. It captures the
    user objective, expected outputs, capability requirements, and lightweight
    execution constraints without forcing simple product QA into a heavy
    research workflow.
    """
    unique_deliverables = _unique(deliverables or _deliverables_from_steps(workflow_steps))
    unique_caps = _unique([cap for cap in capabilities if cap])
    return {
        "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
        "objective": objective,
        "deliverables": [
            _deliverable_contract(deliverable)
            for deliverable in unique_deliverables
        ],
        "capabilities": unique_caps,
        "provenance_mode": provenance_mode if provenance_mode in {"light", "full"} else "light",
        "autonomy": "background_workflow",
        "risk_profile": "research_artifact" if "artifact" in unique_deliverables else "research_workflow",
        "confidence": round(float(confidence or 0.0), 3),
        "step_count": len(workflow_steps),
        "acceptance": _acceptance_for_deliverables(unique_deliverables),
    }


def build_schedule_task_contract(
    *,
    objective: str,
    deferred_goal: str,
    schedule_proposal: dict[str, Any] | None = None,
    provenance_mode: str = "light",
    confidence: float = 0.0,
) -> dict[str, Any]:
    """Return a creation-time contract without binding the future goal.

    A scheduling turn owns only durable schedule registration. The work that will
    run later remains natural-language intent and is planned against the then-live
    provider catalog when the schedule fires.
    """
    normalized_provenance = (
        provenance_mode if provenance_mode in {"light", "full"} else "light"
    )
    contract = {
        "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
        "objective": objective,
        "deliverables": [_deliverable_contract("schedule")],
        "capabilities": [],
        "provenance_mode": normalized_provenance,
        "autonomy": "schedule_registration",
        "risk_profile": "schedule",
        "confidence": round(float(confidence or 0.0), 3),
        "step_count": 0,
        "acceptance": ["schedule_resolved"],
        "deferred_goal": {
            "objective": deferred_goal,
            "binding_state": "deferred",
            "provenance_mode": normalized_provenance,
        },
    }
    if schedule_proposal:
        contract["schedule_proposal"] = copy.deepcopy(schedule_proposal)
    return contract


def task_contract_deliverables(contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read v2 deliverable objects and legacy string lists uniformly.

    Persisted v1 contracts are intentionally not rewritten in place: changing an
    accepted plan snapshot during deserialization would invalidate its content
    hash. Consumers get a normalized copy instead.
    """
    raw = contract.get("deliverables") if isinstance(contract, dict) else []
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                normalized.append(_deliverable_contract(item))
            continue
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("id") or "").strip()
        if not kind:
            continue
        record = _deliverable_contract(kind)
        record["id"] = str(item.get("id") or kind)
        record["required"] = bool(item.get("required", True))
        if isinstance(item.get("acceptance"), list):
            record["acceptance"] = [
                str(value)
                for value in item["acceptance"]
                if str(value).strip()
            ]
        for key in (
            "consumer_step_id",
            "provider_binding_id",
            "provider_contract_hash",
            "provider_name",
            "provider_source",
            "input_pointer",
        ):
            if item.get(key):
                record[key] = str(item[key])
        normalized.append(record)
    return normalized


def build_workflow_dag(workflow_steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Materialize workflow steps as a validated DAG snapshot."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[str] = set()

    for index, step in enumerate(workflow_steps, start=1):
        step_id = str(step.get("id") or f"step_{index}")
        if step_id in seen:
            errors.append(f"duplicate step id: {step_id}")
        seen.add(step_id)
        nodes.append(
            {
                "id": step_id,
                "skill_name": str(step.get("skill_name") or step.get("skill") or ""),
                "capability": str(step.get("capability") or ""),
                "required": _failure_policy(step) != "continue_with_partial",
                "reason": str(step.get("reason") or ""),
            }
        )
        for dep in step.get("depends_on") or []:
            dep_id = str(dep)
            edges.append({"from": dep_id, "to": step_id})

    node_ids = {node["id"] for node in nodes}
    for edge in edges:
        if edge["from"] not in node_ids:
            errors.append(f"unknown dependency {edge['from']} -> {edge['to']}")
        if edge["to"] not in node_ids:
            errors.append(f"unknown target {edge['from']} -> {edge['to']}")

    order, cycle_errors = _topological_order([node["id"] for node in nodes], edges)
    errors.extend(cycle_errors)
    return {
        "nodes": nodes,
        "edges": edges,
        "topological_order": order,
        "is_dag": not errors,
        "errors": errors,
    }


def _deliverables_from_steps(workflow_steps: list[dict[str, Any]]) -> list[str]:
    deliverables: list[str] = []
    for step in workflow_steps:
        capability = str(step.get("capability") or "")
        if capability.startswith("artifact."):
            deliverables.append("artifact")
        elif capability.startswith("qa."):
            deliverables.append("answer")
        elif capability.startswith("literature."):
            deliverables.append("sources")
        elif capability.startswith(("draft.", "synthesis.")):
            deliverables.append(capability if capability.startswith("draft.") else "draft.section")
        elif capability.startswith("review."):
            deliverables.append("review")
    return deliverables or ["workflow"]


def _acceptance_for_deliverables(deliverables: list[str]) -> list[str]:
    checks = ["workflow_steps_recorded", "run_events_recorded"]
    for deliverable in deliverables:
        checks.extend(_acceptance_for_deliverable(deliverable))
    return _unique(checks)


def _deliverable_contract(kind: str) -> dict[str, Any]:
    normalized = str(kind or "").strip()
    return {
        "id": normalized,
        "kind": normalized,
        "required": True,
        "acceptance": _acceptance_for_deliverable(normalized),
    }


def _acceptance_for_deliverable(deliverable: str) -> list[str]:
    if deliverable == "answer":
        return ["answer_or_partial_answer_present"]
    if deliverable == "artifact" or "figure" in deliverable:
        return ["artifact_uri_present", "artifact_generation_trace_present"]
    if deliverable == "sources":
        return ["source_summary_present"]
    if deliverable.startswith(("draft.", "synthesis.")):
        return ["draft_content_present"]
    if deliverable == "schedule":
        return ["schedule_resolved"]
    return []


def _failure_policy(step: dict[str, Any]) -> str:
    return str(step.get("failure_policy") or "").strip().lower().replace("-", "_")


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _topological_order(node_ids: list[str], edges: list[dict[str, str]]) -> tuple[list[str], list[str]]:
    incoming = {node: 0 for node in node_ids}
    outgoing: dict[str, list[str]] = {node: [] for node in node_ids}
    for edge in edges:
        src = edge["from"]
        dst = edge["to"]
        if src not in incoming or dst not in incoming:
            continue
        incoming[dst] += 1
        outgoing[src].append(dst)

    ready = [node for node in node_ids if incoming[node] == 0]
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for dst in outgoing[node]:
            incoming[dst] -= 1
            if incoming[dst] == 0:
                ready.append(dst)
    if len(order) != len(node_ids):
        return order, ["workflow graph contains a dependency cycle"]
    return order, []
