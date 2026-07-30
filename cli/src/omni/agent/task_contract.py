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
    return {
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
        if isinstance(item.get("required_checks"), list):
            record["required_checks"] = _declared_check_ids(
                item["required_checks"]
            )
        normalized.append(record)
    return normalized


def bind_task_contract_providers(
    contract: dict[str, Any],
    workflow_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attach provider-owned quality obligations to exact workflow consumers.

    The host never invents domain checks here. It copies only check identifiers
    declared by the selected provider's ``quality_contract`` and binds them to
    the sealed provider identity. The provider judges those checks after
    execution; the verifier merely matches and aggregates the envelopes.
    """
    bound = copy.deepcopy(contract)
    deliverables = task_contract_deliverables(bound)
    used_record_indexes: set[int] = set()
    for index, step in enumerate(workflow_steps):
        quality = (
            step.get("quality_contract")
            if isinstance(step.get("quality_contract"), dict)
            else {}
        )
        checks = _declared_check_ids(quality.get("checks"))
        if not checks or quality.get("assessment_required") is not True:
            continue
        step_id = str(step.get("id") or "")
        binding_id = str(step.get("provider_binding_id") or "")
        contract_hash = str(step.get("provider_contract_hash") or "")
        if not step_id or not binding_id or not contract_hash:
            # An unsealed provider cannot authoritatively own an assessment.
            # Plan validation/materialization will either seal it later or fail.
            continue
        deliverable_id = str(step.get("deliverable_id") or step_id)
        step["deliverable_id"] = deliverable_id
        exact_index = _exact_obligation_index(
            deliverables,
            deliverable_id=deliverable_id,
            step_id=step_id,
            provider_binding_id=binding_id,
            provider_contract_hash=contract_hash,
        )
        record_index = exact_index
        if record_index is None:
            record_index = _unbound_deliverable_index(
                deliverables,
                deliverable_id=deliverable_id,
                excluded=used_record_indexes,
            )
        if record_index is None:
            record = {
                "id": deliverable_id,
                "kind": str(
                    step.get("deliverable")
                    or step.get("capability")
                    or deliverable_id
                ),
                "required": bool(step.get("required", True))
                and not bool(step.get("optional")),
                "acceptance": [],
            }
            deliverables.append(record)
            record_index = len(deliverables) - 1
        else:
            record = deliverables[record_index]
        used_record_indexes.add(record_index)
        record.update(
            {
                "consumer_step_id": step_id,
                "provider_binding_id": binding_id,
                "provider_contract_hash": contract_hash,
                "provider_name": str(
                    step.get("provider_name")
                    or step.get("skill_name")
                    or step.get("skill")
                    or ""
                ),
                "provider_source": str(step.get("provider_source") or ""),
                "input_pointer": str(
                    step.get("input_pointer")
                    or f"/workflow_steps/{index}/input"
                ),
                "required_checks": checks,
            }
        )
    bound["deliverables"] = deliverables
    return bound


def _exact_obligation_index(
    deliverables: list[dict[str, Any]],
    *,
    deliverable_id: str,
    step_id: str,
    provider_binding_id: str,
    provider_contract_hash: str,
) -> int | None:
    """Return an existing obligation only when its sealed identity is exact."""

    for index, record in enumerate(deliverables):
        if (
            str(record.get("id") or "") == deliverable_id
            and str(record.get("consumer_step_id") or "") == step_id
            and str(record.get("provider_binding_id") or "")
            == provider_binding_id
            and str(record.get("provider_contract_hash") or "")
            == provider_contract_hash
        ):
            return index
    return None


def _unbound_deliverable_index(
    deliverables: list[dict[str, Any]],
    *,
    deliverable_id: str,
    excluded: set[int],
) -> int | None:
    """Return one unclaimed generic record that can become an exact obligation."""

    for index, record in enumerate(deliverables):
        if index in excluded or str(record.get("id") or "") != deliverable_id:
            continue
        if any(
            str(record.get(key) or "")
            for key in (
                "consumer_step_id",
                "provider_binding_id",
                "provider_contract_hash",
            )
        ):
            continue
        return index
    return None


def provider_quality_checks(
    workflow_steps: list[dict[str, Any]],
) -> list[str]:
    """Return required check ids declared by exact selected providers."""
    checks: list[str] = []
    for step in workflow_steps:
        if bool(step.get("optional")) or not bool(step.get("required", True)):
            continue
        quality = (
            step.get("quality_contract")
            if isinstance(step.get("quality_contract"), dict)
            else {}
        )
        if quality.get("assessment_required") is not True:
            continue
        checks.extend(_declared_check_ids(quality.get("checks")))
    return _unique(checks)


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


def _declared_check_ids(value: Any) -> list[str]:
    """Return check ids only from the contract's declared string array."""

    if not isinstance(value, list):
        return []
    return _unique(
        [
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ]
    )


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
