"""UX-06: --continue / /resume echo a bounded transcript tail (Codex Initial)."""

from __future__ import annotations

import pytest

from omni.cli.session_resume import (
    CLASSIC_ASSISTANT_LINE_CAP,
    INITIAL_HISTORY_TURN_LIMIT,
    ResumeView,
    classic_assistant_preview,
    collect_resume_view,
    render_resume_view,
    take_last_user_turns,
)
from omni.cli.state import AppState, make_agent
from omni.storage.models import ConversationMessageORM
from tests.conftest import cli_text


def _row(role: str, content: str, **kwargs: object) -> ConversationMessageORM:
    return ConversationMessageORM(session_id="s", role=role, content=content, **kwargs)


def test_take_last_user_turns_keeps_five_and_counts_hidden() -> None:
    rows = []
    for index in range(7):
        rows.append(_row("user", f"USER-{index}"))
        rows.append(_row("assistant", f"ASSIST-{index}"))

    tail, hidden = take_last_user_turns(rows)

    assert hidden == 2
    assert INITIAL_HISTORY_TURN_LIMIT == 5
    assert [row.content for row in tail if row.role == "user"] == [
        "USER-2",
        "USER-3",
        "USER-4",
        "USER-5",
        "USER-6",
    ]
    assert "USER-0" not in [row.content for row in tail]
    assert "USER-1" not in [row.content for row in tail]


def test_take_last_user_turns_keeps_a_short_session_whole() -> None:
    rows = [_row("user", "only"), _row("assistant", "reply")]
    tail, hidden = take_last_user_turns(rows)
    assert hidden == 0
    assert [row.content for row in tail] == ["only", "reply"]


def test_classic_assistant_preview_leaves_short_answers_intact() -> None:
    text = "short answer"
    shown, hint = classic_assistant_preview(text)
    assert shown == text
    assert hint == ""


def test_classic_assistant_preview_caps_a_manuscript() -> None:
    text = "\n".join(f"line-{i}" for i in range(CLASSIC_ASSISTANT_LINE_CAP + 12))
    shown, hint = classic_assistant_preview(text)
    assert "line-0" in shown
    assert f"line-{CLASSIC_ASSISTANT_LINE_CAP}" not in shown
    assert hint == "12 more lines"


@pytest.mark.asyncio
async def test_collect_resume_view_skips_compacted_rows_and_notes_the_bridge() -> None:
    agent = await make_agent(AppState())
    try:
        sid = await agent.ensure_session(channel="cli", title="latent-space survey")
        await agent._persist_message(sid, "user", "COMPACTED-USER-HIDDEN")
        await agent._persist_message(sid, "assistant", "COMPACTED-ASSIST-HIDDEN")
        covered = [row.id for row in await agent.session_messages(sid)]
        await agent.conversations.write_compaction_bridge(
            sid, "BRIDGE-SUMMARY-EARLIER-SURVEY", covered
        )
        await agent._persist_message(sid, "user", "VISIBLE-USER")
        await agent._persist_message(sid, "assistant", "VISIBLE-ASSIST")
        task = await agent.tasks.create_task(
            session_id=sid, channel="cli", user_input="VISIBLE-USER", title="survey draft"
        )
        async with agent.db.session() as session:
            row = await session.get(type(task), task.id)
            assert row is not None
            row.status = "succeeded"
            await session.commit()

        view = await collect_resume_view(agent, sid)
    finally:
        await agent.aclose()

    assert view is not None
    assert view.session_id == sid
    assert view.title == "latent-space survey"
    assert view.hidden_user_turns == 0
    assert "BRIDGE-SUMMARY-EARLIER-SURVEY" in view.bridge_preview
    assert [m.content for m in view.messages] == ["VISIBLE-USER", "VISIBLE-ASSIST"]
    assert "COMPACTED-USER-HIDDEN" not in [m.content for m in view.messages]
    assert view.task_id == task.id
    assert view.task_status == "succeeded"
    assert view.task_active is False


@pytest.mark.asyncio
async def test_collect_resume_view_omits_turns_beyond_the_initial_limit() -> None:
    agent = await make_agent(AppState())
    try:
        sid = await agent.ensure_session(channel="cli", title="long")
        for index in range(INITIAL_HISTORY_TURN_LIMIT + 1):
            await agent._persist_message(sid, "user", f"USER-TURN-{index}")
            await agent._persist_message(sid, "assistant", f"ASSIST-TURN-{index}")
        view = await collect_resume_view(agent, sid)
    finally:
        await agent.aclose()

    assert view is not None
    assert view.hidden_user_turns == 1
    contents = [row.content for row in view.messages]
    assert "USER-TURN-0" not in contents
    assert "USER-TURN-1" in contents
    assert "USER-TURN-5" in contents


@pytest.mark.asyncio
async def test_latest_task_for_session_returns_finished_when_none_are_active() -> None:
    agent = await make_agent(AppState())
    try:
        sid = await agent.ensure_session(channel="cli")
        task = await agent.tasks.create_task(
            session_id=sid, channel="cli", user_input="done", title="finished turn"
        )
        async with agent.db.session() as session:
            row = await session.get(type(task), task.id)
            assert row is not None
            row.status = "succeeded"
            await session.commit()
        assert await agent.tasks.active_task_for_session(sid) is None
        latest = await agent.tasks.latest_task_for_session(sid)
        assert latest is not None
        assert latest.id == task.id
        assert latest.status == "succeeded"
    finally:
        await agent.aclose()


def test_render_resume_view_classic_truncates_long_assistant(capsys) -> None:
    long_body = "\n".join(f"paper-line-{i}" for i in range(CLASSIC_ASSISTANT_LINE_CAP + 5))
    render_resume_view(
        ResumeView(
            session_id="abcdef12deadbeef",
            title="paper",
            updated_at="today",
            stored_messages=2,
            hidden_user_turns=0,
            messages=[
                _row("user", "write the paper"),
                _row("assistant", long_body),
            ],
        )
    )
    text = cli_text(capsys.readouterr().out)
    assert "Resumed session abcdef12" in text
    assert "write the paper" in text
    assert "paper-line-0" in text
    assert f"paper-line-{CLASSIC_ASSISTANT_LINE_CAP}" not in text
    assert "5 more lines" in text
    assert "/replay abcdef12" in text


def test_tui_sink_marks_long_assistant_foldable() -> None:
    from omni.cli.repl_output import use_output_sink
    from omni.cli.repl_transcript import TranscriptKind

    class _Sink:
        def __init__(self) -> None:
            self.events: list[object] = []

        def write(self, text: str) -> None:
            del text

        def publish_event(self, event: object) -> None:
            self.events.append(event)

        def set_status(self, text: str) -> None:
            del text

        def clear(self) -> None:
            return

        def redraw(self) -> None:
            return

    sink = _Sink()
    long_body = "\n".join(f"fold-line-{i}" for i in range(20))
    with use_output_sink(sink):
        render_resume_view(
            ResumeView(
                session_id="abc12345deadbeef",
                title="fold",
                updated_at="now",
                stored_messages=2,
                hidden_user_turns=0,
                messages=[
                    _row("user", "ask"),
                    _row("assistant", long_body),
                ],
            )
        )
    markdown = [
        event
        for event in sink.events
        if getattr(event, "kind", None) == TranscriptKind.MARKDOWN
    ]
    assert markdown
    assert markdown[-1].foldable is True
    assert markdown[-1].payload == long_body


async def _seed_session(*pairs: tuple[str, str], title: str = "seed") -> str:
    agent = await make_agent(AppState())
    try:
        sid = await agent.ensure_session(channel="cli", title=title)
        for user, assistant in pairs:
            await agent._persist_message(sid, "user", user)
            await agent._persist_message(sid, "assistant", assistant)
        return sid
    finally:
        await agent.aclose()


async def _run_repl_until_eof(monkeypatch, *, resume_session_id: str | None) -> str:
    from omni.cli import main as main_module

    async def _eof(*_args, **_kwargs):
        raise EOFError

    monkeypatch.setattr(main_module, "_maybe_prompt_update", lambda _settings: False)
    monkeypatch.setattr(main_module, "_read_repl_line_async", _eof)
    monkeypatch.setattr(main_module.update_check, "maybe_refresh_in_background", lambda *_a, **_k: None)
    state = AppState(overrides={"display": {"ui_mode": "classic"}})
    await main_module._repl_async(state, resume_session_id=resume_session_id)


@pytest.mark.asyncio
async def test_repl_continue_echoes_recent_history(monkeypatch, capsys) -> None:
    user = "UX06-HISTORY-MARKER-ALPHA: latent-space intervention survey"
    assist = "UX06-ASSISTANT-MARKER-BETA: first-pass notes on steering vectors"
    sid = await _seed_session((user, assist), title="latent-space survey")
    await _run_repl_until_eof(monkeypatch, resume_session_id=sid)
    text = cli_text(capsys.readouterr().out)
    assert f"Resumed session {sid[:8]}" in text
    assert "latent-space survey" in text
    assert user in text
    assert assist in text


@pytest.mark.asyncio
async def test_repl_new_session_does_not_echo_prior_history(monkeypatch, capsys) -> None:
    user = "UX06-SHOULD-NOT-APPEAR-ON-NEW-SESSION"
    await _seed_session((user, "old assistant"), title="old")
    await _run_repl_until_eof(monkeypatch, resume_session_id=None)
    text = cli_text(capsys.readouterr().out)
    assert user not in text
    assert "Resumed session" not in text


@pytest.mark.asyncio
async def test_repl_resume_switch_echoes_target_history(capsys) -> None:
    from omni.cli.main import _repl_resume

    sid = await _seed_session(
        ("SWITCH-USER-MARKER", "SWITCH-ASSIST-MARKER"), title="switch-me"
    )
    agent = await make_agent(AppState())
    try:
        other = await agent.ensure_session(channel="cli", reuse_latest=False, title="other")
        bound = await _repl_resume(agent, AppState(), sid, other)
    finally:
        await agent.aclose()
    text = cli_text(capsys.readouterr().out)
    assert bound == sid
    assert f"Resumed session {sid[:8]}" in text
    assert "SWITCH-USER-MARKER" in text
    assert "SWITCH-ASSIST-MARKER" in text
