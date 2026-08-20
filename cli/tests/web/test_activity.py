"""Safe activity projection never includes raw secrets or full payloads."""

from __future__ import annotations

from omni.storage.models import TaskEventORM
from omni.web.activity import display_title, project_event


def test_display_title_prefers_owner_then_task_then_message() -> None:
    assert display_title(title="Mine", task_input="task", user_message="msg") == "Mine"
    assert display_title(title="", task_input="task input", user_message="msg") == "task input"
    assert display_title(title="", task_input="", user_message="hello there") == "hello there"
    assert display_title() == "\u65b0\u4f1a\u8bdd"


def test_project_event_redacts_and_truncates() -> None:
    event = TaskEventORM(
        task_id="t1",
        seq=3,
        event_type="react.tool.start",
        name="search_literature",
        tool_name="search_literature",
        status="running",
        summary="query papers",
        input_json={"api_key": "sk-secret", "q": "x" * 400},
        output_json={"authorization": "Bearer abc", "hits": 2},
    )
    item = project_event(event)
    assert item["seq"] == 3
    assert item["kind"] == "tool"
    assert "sk-secret" not in item["safe_args"]
    assert "***" in item["safe_args"]
    assert "Bearer abc" not in item["safe_result"]
    assert "input_json" not in item
    assert "output_json" not in item
    assert len(item["safe_args"]) <= 160


def test_project_event_whitelists_soulagent_payload_and_free_text() -> None:
    event = TaskEventORM(
        task_id="t1",
        seq=4,
        event_type="subtask.done",
        skill_name="soulagent",
        summary="wrote /Users/private/project/role.md",
        error="persona failed at /Users/private/project",
        input_json={
            "action": "activate",
            "scientist_id": "fengli-xu",
            "project_root": "/Users/private/project",
        },
        output_json={
            "outcome": {"code": "refreshed"},
            "persona_text": "private generated persona",
            "role_path": "/Users/private/project/role.md",
        },
    )

    item = project_event(event)

    serialized = str(item)
    assert "/Users/private" not in serialized
    assert "private generated persona" not in serialized
    assert item["summary"] == "SoulAgent operation update"
    assert item["error"] == "SoulAgent operation failed"
    assert '"scientist_id": "fengli-xu"' in item["safe_args"]
    assert '"redacted": true' in item["safe_result"]
