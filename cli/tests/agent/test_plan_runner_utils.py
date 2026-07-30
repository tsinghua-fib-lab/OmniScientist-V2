from omni.agent.plan_runner_utils import workflow_terminal_message


def test_workflow_terminal_message_uses_owning_task_id() -> None:
    message = workflow_terminal_message(
        {"summary": "Partial result retained.", "error": "step failed"},
        task_id="task-12345678",
    )

    assert "/task show task-123" in message
    assert "step failed" in message
