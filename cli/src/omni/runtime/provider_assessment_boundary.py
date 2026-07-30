"""Execution-boundary enforcement for provider-owned assessments."""

from __future__ import annotations

from typing import Any

from omni.agent.provider_quality_binding import provider_assessment_binding
from omni.runtime.deliverable_assessment import (
    bind_deliverable_assessment_identity,
)
from omni.runtime.provider_authority import workflow_subtask_authority_error
from omni.skills_runtime.context import SKILL_SOURCE_PARAM


async def prepare_provider_assessment_execution(
    *,
    db: Any,
    registry: Any,
    skill_name: str,
    input_data: dict[str, Any],
    expected: dict[str, Any],
    workflow_run_id: str,
    workflow_step_id: str,
) -> tuple[Any, dict[str, Any] | None, str]:
    """Resolve a provider and validate its sealed execution identity."""

    forced = str(input_data.pop(SKILL_SOURCE_PARAM, "") or "")
    entry = registry.resolve_ref(skill_name, forced)
    authority_error = await workflow_subtask_authority_error(
        db=db,
        registry=registry,
        entry=entry,
        expected=expected,
        workflow_run_id=workflow_run_id,
        workflow_step_id=workflow_step_id,
    )
    if authority_error or entry is None:
        return entry, None, authority_error
    assessment_identity, authority_error = provider_assessment_binding(
        expected,
        entry,
    )
    return entry, assessment_identity, authority_error


def bind_execution_assessment_identity(
    result: Any,
    assessment_identity: dict[str, Any] | None,
) -> None:
    """Bind host-owned identity onto a provider assessment, when present."""

    if assessment_identity is not None and isinstance(result, dict):
        bind_deliverable_assessment_identity(result, assessment_identity)


__all__ = [
    "bind_execution_assessment_identity",
    "prepare_provider_assessment_execution",
]
