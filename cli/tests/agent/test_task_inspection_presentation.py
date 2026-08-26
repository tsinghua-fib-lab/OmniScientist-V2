"""Task-inspection presentation (Layers 3 & 4).

Layer 4 — a failed task's error is a *diagnosis*, never a summary: it must not be
folded into ``summary`` (where it was re-consumed as a valid result, the
recursive-failure loop in incident 78071dd2) and it must render under a distinct
"Why it failed" label.

Layer 3 — a durable ``summary`` may contain a provider's native tool markup
(DSML) or be very long; the host projection must strip the markup and truncate
it instead of dumping raw sentinels as the answer (incident c60c4c85).
"""

from __future__ import annotations

from datetime import UTC, datetime

from omni.agent.capabilities import CAPABILITY_TASK_INSPECT, CAPABILITY_TASK_REVIEW
from omni.agent.plan_factory import build_task_review_plan
from omni.agent.turn_execution import (
    _format_task_inspection,
    append_task_review_status,
)
from omni.core.react_agent import AgentLoopResult, ToolInvocationRecord
from omni.skills_runtime.builtin_tools.recall import _task_payload
from omni.storage.models import TaskORM


def _task(**overrides) -> TaskORM:
    base = {
        "id": "abcd1234abcd1234",
        "status": "succeeded",
        "title": "t",
        "user_input": "x",
        "summary": "",
        "error": "",
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return TaskORM(**base)


# --- Layer 4: error is not a summary ---------------------------------------


def test_failed_task_payload_keeps_error_out_of_summary() -> None:
    payload = _task_payload(
        _task(status="failed", summary="", error="No authoritative task record")
    )
    assert payload["summary"] == ""
    assert payload["failure_reason"] == "No authoritative task record"


def test_task_payload_names_historical_state_without_removing_status_alias() -> None:
    payload = _task_payload(_task(status="cancelled", summary="partial", error=""))

    assert payload["task_status"] == "cancelled"
    assert payload["status"] == "cancelled"


def test_format_task_inspection_labels_failure_reason_not_summary() -> None:
    payload = _task_payload(
        _task(status="failed", summary="", error="No authoritative task record")
    )
    text = _format_task_inspection(payload)
    assert "**failed**" in text
    assert "Why it failed: No authoritative task record" in text
    assert "System summary" not in text


def test_format_task_inspection_succeeded_uses_system_summary() -> None:
    payload = _task_payload(_task(status="succeeded", summary="all done", error=""))
    text = _format_task_inspection(payload)
    assert "System summary: all done" in text
    assert "Why it failed" not in text


# --- Layer 3: DSML-stripped, truncated summary ------------------------------


def test_format_task_inspection_strips_dsml_from_summary() -> None:
    dsml = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls> <\uff5c\uff5cDSML\uff5c\uff5cinvoke "
        'name="update_plan"> <\uff5c\uff5cDSML\uff5c\uff5cparameter name="plan" '
        'string="false">[{"status":"completed","step":"review"}]'
        "</\uff5c\uff5cDSML\uff5c\uff5cparameter> </\uff5c\uff5cDSML\uff5c\uff5cinvoke> "
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    payload = _task_payload(_task(status="degraded", summary=dsml, error=""))
    text = _format_task_inspection(payload)
    assert "DSML" not in text
    assert "update_plan" not in text


def test_format_task_inspection_truncates_a_very_long_summary() -> None:
    payload = _task_payload(_task(status="degraded", summary="x" * 5000, error=""))
    text = _format_task_inspection(payload)
    # The whole 5000-char blob must not be dumped verbatim into the answer.
    assert len(text) < 2000
    assert "\u2026" in text  # ellipsis marks the elision


def test_format_task_inspection_shows_filesystem_path_not_uri() -> None:
    text = _format_task_inspection(
        {
            "task_id": "abcd1234abcd1234",
            "status": "succeeded",
            "summary": "wrote the survey",
            "artifacts": [
                {
                    "title": "latent-steering-related-work",
                    "path": "/Users/me/repo/reports/Survey/latent-steering-related-work.md",
                    "uri": "artifact://35c6de7a6a7c4f8d93dd2dbeeffbc1e5",
                }
            ],
        }
    )
    assert "Result artifacts:" in text
    assert "/Users/me/repo/reports/Survey/latent-steering-related-work.md" in text
    assert "artifact://" not in text
    assert "35c6de7a6a7c4f8d93dd2dbeeffbc1e5" not in text


def test_format_task_inspection_omits_uri_when_path_is_missing() -> None:
    text = _format_task_inspection(
        {
            "task_id": "abcd1234abcd1234",
            "status": "succeeded",
            "artifacts": [
                {
                    "title": "latent-steering-related-work",
                    "path": "",
                    "uri": "artifact://35c6de7a6a7c4f8d93dd2dbeeffbc1e5",
                }
            ],
        }
    )
    assert "latent-steering-related-work" in text
    assert "saved (path unavailable)" in text
    assert "artifact://" not in text


def test_format_task_inspection_does_not_print_artifact_refs() -> None:
    text = _format_task_inspection(
        {
            "task_id": "abcd1234abcd1234",
            "status": "succeeded",
            "artifact_refs": ["artifact:35c6de7a6a7c4f8d93dd2dbeeffbc1e5"],
        }
    )
    assert "artifact://" not in text
    assert "35c6de7a6a7c4f8d93dd2dbeeffbc1e5" not in text


# --- Layer 3: task.review keeps prose, host appends authoritative status ----


def _review_plan():
    plan = build_task_review_plan(
        "review", task_id="run", rationale="", confidence=0.8
    )
    plan.capability_inputs = {CAPABILITY_TASK_REVIEW: {}}
    return plan


def _result_with_trace(records: list[ToolInvocationRecord]) -> AgentLoopResult:
    return AgentLoopResult(
        kind="text",
        content="Here is what we worked on recently.",
        tool_trace=records,
    )


def test_task_review_keeps_model_prose_and_appends_status_footer() -> None:
    records = [
        ToolInvocationRecord(
            name="list_recent_tasks",
            arguments={"scope": "all"},
            status="succeeded",
            result={
                "scope": "all",
                "tasks": [
                    {"task_id": "aaaa1111", "status": "succeeded", "workspace": "alpha", "title": "Figure"},
                    {"task_id": "bbbb2222", "status": "failed", "workspace": "beta", "title": "RAG review"},
                ],
            },
        ),
    ]
    result = append_task_review_status(_review_plan(), _result_with_trace(records))

    # The model's narrative is preserved verbatim (never overwritten)...
    assert result.content.startswith("Here is what we worked on recently.")
    # ...and the host appends an authoritative status line per task.
    assert "Reviewed tasks (authoritative status)" in result.content
    assert "`aaaa1111` **succeeded**" in result.content
    assert "`bbbb2222` **failed**" in result.content
    assert "beta" in result.content
    assert "Recovery candidates" in result.content
    assert "`/task retry bbbb2222`" in result.content
    assert "`/task retry aaaa1111`" not in result.content


def test_task_review_footer_is_noop_without_review_capability() -> None:
    records = [
        ToolInvocationRecord(
            name="get_task",
            arguments={},
            status="succeeded",
            result={"task_id": "aaaa1111", "status": "failed", "workspace": "alpha"},
        ),
    ]
    plan = _review_plan()
    plan.capability_inputs = {CAPABILITY_TASK_INSPECT: {}}  # not a review turn
    result = append_task_review_status(plan, _result_with_trace(records))

    assert "Reviewed tasks" not in result.content
