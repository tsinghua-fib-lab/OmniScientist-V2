"""Task titles stay a short goal, not a clipped copy of the whole prompt."""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.runtime.task_recorder import TaskRecorder
from omni.runtime.task_title import SHORT_TITLE_MAX, manuscript_basename, short_task_title
from omni.storage.db import get_database

_EDA_PROMPT = (
    "为 智能体 系统综述准备材料：获取 Attention Is All You Need 摘要，"
    "并生成包含 query、key、value 的注意力机制示意图，以及一篇智能体系统综述论文。"
)


def test_colon_left_goal_becomes_the_title() -> None:
    title = short_task_title(_EDA_PROMPT)
    assert title == "智能体 系统综述"
    assert len(title) <= SHORT_TITLE_MAX
    assert "Attention" not in title
    assert "Draft" not in title


def test_rag_survey_prompt_stays_short() -> None:
    title = short_task_title(
        "为 RAG 系统综述准备材料：获取 Attention Is All You Need 摘要，并生成科研架构图。"
    )
    assert title == "RAG 系统综述"
    assert len(title) <= SHORT_TITLE_MAX


def test_english_prompt_takes_the_first_clause() -> None:
    assert short_task_title("Write a RAG survey and an architecture figure.") == "RAG survey"


def test_empty_input_is_untitled() -> None:
    assert short_task_title("") == "Untitled task"
    assert short_task_title("   ") == "Untitled task"


def test_manuscript_basename_is_a_bare_markdown_name() -> None:
    assert manuscript_basename("智能体 系统综述") == "智能体-系统综述.md"
    assert "Draft" not in manuscript_basename(_EDA_PROMPT)


@pytest.mark.asyncio
async def test_create_task_stores_the_short_title() -> None:
    settings = load_settings()
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    recorder = TaskRecorder(db, project=settings.paths.project_name)
    task = await recorder.create_task(
        session_id="sess-title",
        channel="cli",
        user_input=_EDA_PROMPT,
    )
    assert task.title == "智能体 系统综述"
    explicit = await recorder.create_task(
        session_id="sess-title",
        channel="cli",
        user_input=_EDA_PROMPT,
        title="自定义短标题",
    )
    assert explicit.title == "自定义短标题"
