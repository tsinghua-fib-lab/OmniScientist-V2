"""A fallback continues usable work instead of starting the task over."""

from types import SimpleNamespace

from omni.agent.plan_fallthrough import history_with_failed_attempt, loop_result_with_failed_attempt


def test_failed_route_carries_partial_artifact_evidence_into_react() -> None:
    attempt = SimpleNamespace(
        handled=False,
        drained_results=[
            {
                "skill": "research-ideation",
                "status": "degraded",
                "error": "",
                "result": {
                    "summary": "Draft completed; one optional score was unavailable.",
                    "artifacts": [
                        {
                            "uri": "artifact://draft-1",
                            "path": "/workspace/reports/task/draft.md",
                        }
                    ],
                },
            }
        ],
    )

    history = history_with_failed_attempt([], attempt)

    content = history[-1]["content"]
    assert "Draft completed" in content
    assert "/workspace/reports/task/draft.md" in content
    assert "inspect and continue" in content
    assert "failed without a message" not in content


def test_failed_route_trace_is_kept_on_the_react_result() -> None:
    from omni.core.react_agent import ToolInvocationRecord

    prior = ToolInvocationRecord(name="run_skill", arguments={"skill_name": "livefigure"})
    react = ToolInvocationRecord(name="find_skill", arguments={"query": "figure"})
    attempt = SimpleNamespace(handled=False, tool_trace=[prior])
    result = SimpleNamespace(tool_trace=[react])

    merged = loop_result_with_failed_attempt(result, attempt)
    assert [record.name for record in merged.tool_trace] == ["run_skill", "find_skill"]
