"""A fallback continues usable work instead of starting the task over."""

from types import SimpleNamespace

from omni.agent.plan_fallthrough import history_with_failed_attempt


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
