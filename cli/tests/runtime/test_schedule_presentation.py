"""Host-rendered schedule cards stay structured; the one-line summary stays English."""

from __future__ import annotations

from omni.agent.turn_execution import TurnResult
from omni.core.react_agent import ToolInvocationRecord
from omni.runtime.presentation import turn_presentation_from_result
from omni.scheduling.contracts import (
    STATUS_AWAITING_APPROVAL,
    STATUS_CREATED,
    ScheduleCreateResult,
)
from omni.scheduling.presentation import (
    build_card,
    build_summary,
    is_summary_echo,
    result_from_tool_payload,
)

_GOAL = (
    "为 RAG 系统综述准备材料：获取 Attention Is All You Need 摘要，"
    "并生成包含 query、retriever、reranker、LLM 的科研架构图，输出一篇论文"
)


def _created(**kwargs: object) -> ScheduleCreateResult:
    defaults: dict[str, object] = {
        "status": STATUS_CREATED,
        "schedule_id": "c60b961cabcdef12",
        "spec": "once",
        "title": "RAG 系统综述材料准备",
        "goal": _GOAL,
        "next_run_local": "2026-08-14 17:49:00 +0800",
        "channel": "wechat",
        "approved_tools": ["edit_file", "run_compute", "write_file"],
        "scheduling_enabled": True,
        "runner_ready": True,
        "registered": True,
    }
    defaults.update(kwargs)
    result = ScheduleCreateResult(**defaults)  # type: ignore[arg-type]
    result.summary = build_summary(result)
    return result


def test_summary_stays_one_english_sentence_and_truncates_the_goal() -> None:
    result = _created()
    summary = build_summary(result)
    assert "\n" not in summary
    assert summary.startswith("Scheduled '")
    assert "omni schedule show c60b961c" in summary
    assert "…" in summary
    assert _GOAL not in summary


def test_chat_card_keeps_the_full_goal_and_hides_the_cli_manage_command() -> None:
    result = _created()
    card = build_card(result, chat=True)
    assert _GOAL in card
    assert "The scheduled task is confirmed" in card
    assert "- **When**: 2026-08-14 17:49:00 +0800" in card
    assert "- **Tools**: edit_file, run_compute, write_file" in card
    assert "Results will be sent to this conversation." in card
    assert "omni schedule show" not in card
    assert card.count("\n") >= 4


def test_cli_card_keeps_the_manage_command() -> None:
    result = _created()
    card = build_card(result, chat=False)
    assert "omni schedule show c60b961c" in card
    assert _GOAL in card


def test_payload_round_trip_rebuilds_the_card() -> None:
    result = _created()
    payload = result.tool_result()
    hydrated = result_from_tool_payload(payload)
    assert hydrated is not None
    assert hydrated.schedule_id.startswith("c60b961c")
    assert _GOAL in build_card(hydrated, chat=True)


def test_wechat_turn_replaces_the_echoed_summary_with_the_card() -> None:
    result = _created()
    turn = TurnResult(
        text=result.summary,
        session_id="sess-1",
        task_id="f7d06b1cabcdef12",
        tool_trace=[
            ToolInvocationRecord(
                name="schedule_task",
                arguments={"goal": _GOAL},
                result=result.tool_result(),
            )
        ],
    )
    presentation = turn_presentation_from_result(turn, channel="wechat")
    md = presentation.to_markdown(include_local_paths=False)
    assert _GOAL in md
    assert "omni schedule show" not in md
    assert "Scheduled '" not in md
    assert "When it runs it may use these tools unattended" not in md


def test_cli_turn_keeps_a_model_written_reply() -> None:
    result = _created()
    chinese = "已为您更新定时任务：\n\n● 任务：为 RAG 系统综述准备材料"
    turn = TurnResult(
        text=chinese,
        session_id="sess-1",
        task_id="8645824babcdef12",
        tool_trace=[
            ToolInvocationRecord(
                name="resolve_action_checkpoint",
                arguments={"checkpoint_id": "5a47367d"},
                result=result.tool_result(),
            )
        ],
    )
    presentation = turn_presentation_from_result(turn, channel="cli")
    assert presentation.assistant_text == chinese


def test_wechat_keeps_a_short_lead_in_above_the_card() -> None:
    result = _created()
    turn = TurnResult(
        text="已成功改期并确认。",
        session_id="sess-1",
        task_id="1c1caac2abcdef12",
        tool_trace=[
            ToolInvocationRecord(
                name="schedule_task",
                arguments={"goal": _GOAL},
                result=result.tool_result(),
            )
        ],
    )
    presentation = turn_presentation_from_result(turn, channel="wechat")
    assert presentation.assistant_text.startswith("已成功改期并确认。")
    assert _GOAL in presentation.assistant_text
    assert "omni schedule show" not in presentation.assistant_text


def test_awaiting_approval_card_keeps_the_local_approve_command() -> None:
    result = ScheduleCreateResult(
        status=STATUS_AWAITING_APPROVAL,
        proposal_id="prop1234abcd",
        approve_command="omni schedule approve prop1234",
        goal=_GOAL,
        channel="wechat",
    )
    card = build_card(result, chat=True)
    assert "Nothing runs yet." in card
    assert "omni schedule approve prop1234" in card
    assert "omni schedule deny" not in card
    assert _GOAL in card


def test_is_summary_echo_detects_the_one_line_wall() -> None:
    result = _created()
    assert is_summary_echo(result.summary, result)
    assert not is_summary_echo("已为您更新定时任务。", result)


def test_wechat_strips_a_cli_manage_hint_from_any_reply() -> None:
    turn = TurnResult(
        text=(
            "已成功改期并确认。View or manage it with `omni schedule show c60b961c`. "
            "Approve locally with `omni schedule approve prop1234`."
        ),
        session_id="sess-1",
        task_id="f7d06b1cabcdef12",
    )
    presentation = turn_presentation_from_result(turn, channel="wechat")
    assert "omni schedule show" not in presentation.assistant_text
    assert "omni schedule approve prop1234" in presentation.assistant_text
    assert "已成功改期并确认" in presentation.assistant_text


def test_wechat_echo_without_a_host_card_keeps_the_observation() -> None:
    summary = "memory_search returned 3 matches in the workspace index."
    turn = TurnResult(
        text=summary,
        session_id="sess-1",
        task_id="aaaaaaaaaaaaaaaa",
        tool_trace=[
            ToolInvocationRecord(
                name="memory_search",
                arguments={"query": "RAG"},
                result={"status": "ok", "summary": summary},
            )
        ],
    )
    presentation = turn_presentation_from_result(turn, channel="wechat")
    assert presentation.assistant_text == summary
