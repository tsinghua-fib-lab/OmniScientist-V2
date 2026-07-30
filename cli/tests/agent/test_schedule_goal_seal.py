"""Host sealing of scheduled work: conversation + draft beat Active target.

Reproduces the 2026-08-13 WeChat incident at the pure-function boundary:

    Open draft goal     = RAG 系统综述
    Planner/host goal   = 联邦学习综述  (Active target: latest research-ideation)
    User this turn      = 今天上午10点13分开始执行吧
    Model tool args     = RAG (or, in a drift variant, 联邦学习)

    Expected            = stored/shown/fired goal is RAG

Active target remains valid for "revise this figure" / "inspect this report"
on other intents; it is not the default scheduled work item.
"""

from __future__ import annotations

from types import SimpleNamespace

from omni.agent.intent_plan import IntentType
from omni.agent.orchestrator import OmniAgent
from omni.agent.schedule_goal import goal_grounded_in_message, seal_schedule_work

RAG = "为 RAG 系统综述准备材料，并生成 Attention Is All You Need 摘要与架构图"
FL = "撰写联邦学习在非IID数据下的优化方法综述"
TIME_ONLY = "今天上午10点13分开始执行吧"
FIRST_TURN = f"{RAG}，今天7点10分执行"


def test_this_turn_user_text_still_beats_a_drifted_model_goal():
    sealed = seal_schedule_work(
        model_goal="a totally different drifted goal",
        host_goal=RAG,
        user_message=FIRST_TURN,
    )
    assert sealed.source == "host" and sealed.goal == RAG


def test_time_only_followup_reuses_open_rag_draft_not_federated_learning_active_target():
    """Incident: title/tool said RAG, planner sealed federated learning."""
    sealed = seal_schedule_work(
        model_goal=RAG,
        model_title="RAG系统综述材料准备",
        host_goal=FL,
        user_message=TIME_ONLY,
        draft_goal=RAG,
        draft_title="RAG系统综述材料准备",
    )
    assert sealed.source == "draft"
    assert sealed.goal == RAG
    assert "RAG" in sealed.title
    assert FL not in sealed.goal
    assert FL not in sealed.title


def test_draft_wins_even_when_the_model_also_copied_the_active_target():
    sealed = seal_schedule_work(
        model_goal=FL,
        model_title="联邦学习综述",
        host_goal=FL,
        user_message=TIME_ONLY,
        draft_goal=RAG,
        draft_title="RAG系统综述材料准备",
    )
    assert sealed.source == "draft" and sealed.goal == RAG
    assert "RAG" in sealed.title


def test_user_can_change_the_work_this_turn_while_a_draft_is_open():
    sealed = seal_schedule_work(
        model_goal=FL,
        host_goal=FL,
        user_message=f"改成{FL}，今天上午10点13分开始",
        draft_goal=RAG,
        draft_title="RAG系统综述材料准备",
    )
    assert sealed.source in {"host", "model"}
    assert sealed.goal == FL
    assert RAG not in sealed.goal


def test_ungrounded_active_target_is_not_the_default_schedule_when_there_is_no_draft():
    sealed = seal_schedule_work(
        model_goal="",
        host_goal=FL,
        user_message=TIME_ONLY,
    )
    assert sealed.source == "conflict" and sealed.goal == ""


def test_model_goal_is_used_when_there_is_no_host_and_no_draft():
    sealed = seal_schedule_work(
        model_goal=RAG,
        model_title="RAG系统综述材料准备",
        user_message=FIRST_TURN,
    )
    assert sealed.source == "model" and sealed.goal == RAG


def test_title_cannot_name_different_work_than_the_sealed_goal():
    sealed = seal_schedule_work(
        model_goal=FL,
        model_title="联邦学习综述",
        host_goal=FL,
        user_message=TIME_ONLY,
        draft_goal=RAG,
        draft_title="RAG系统综述材料准备",
    )
    assert sealed.goal == RAG
    assert "RAG" in sealed.title
    assert FL not in sealed.title


def test_short_english_title_is_kept_when_it_does_not_name_losing_work():
    sealed = seal_schedule_work(
        model_goal="总结今天的科研",
        model_title="daily digest",
        user_message="每天六点总结今天的科研",
    )
    assert sealed.goal == "总结今天的科研"
    assert sealed.title == "daily digest"


def test_anaphora_this_figure_is_grounded_and_may_use_the_model_goal():
    """Research follow-up: 'revise this figure at 6pm' is allowed to name the work."""
    goal = "revise this figure: RAG architecture"
    sealed = seal_schedule_work(
        model_goal=goal,
        user_message="revise this figure tomorrow at 6pm",
    )
    assert sealed.source == "model"
    assert "this figure" in sealed.goal


def test_goal_grounded_in_message_rejects_unrelated_active_target_text():
    assert goal_grounded_in_message(RAG, FIRST_TURN)
    assert not goal_grounded_in_message(FL, TIME_ONLY)
    assert not goal_grounded_in_message(FL, FIRST_TURN)


def test_plan_deferred_goal_rejects_active_target_inference():
    plan = SimpleNamespace(
        intent_type=IntentType.SCHEDULE,
        user_message=TIME_ONLY,
        task_contract={"deferred_goal": {"objective": FL}},
    )
    assert OmniAgent._plan_deferred_goal(plan) == ""


def test_plan_deferred_goal_keeps_a_clean_subset_of_this_turn():
    plan = SimpleNamespace(
        intent_type=IntentType.SCHEDULE,
        user_message=FIRST_TURN,
        task_contract={"deferred_goal": {"objective": RAG}},
    )
    assert OmniAgent._plan_deferred_goal(plan) == RAG
