"""Shared execution result for deterministic plan executors and runners."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omni.core.react_agent import ToolInvocationRecord


@dataclass(slots=True)
class PlanExecutionResult:
    handled: bool = False
    text: str = ""
    kind: str = "text"
    submitted_workflow_ids: list[str] = field(default_factory=list)
    submitted_subtask_ids: list[str] = field(default_factory=list)
    drained_results: list[dict[str, Any]] = field(default_factory=list)
    tool_trace: list[ToolInvocationRecord] = field(default_factory=list)
    terminated_reason: str = "plan_executed"
    error: str = ""
    plan_summary: str = ""
    degraded_warnings: list[str] = field(default_factory=list)
    settlement_status: str = "pending"
