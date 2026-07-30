"""What a reply looks like when the reader is in a chat thread, not a terminal.

Task 964f17aa was asked over WeChat for a survey and answered with one: fourteen
thousand characters of paper typed into the conversation, split by the transport
into ten messages, followed by a provider-selection report, four slash commands
written for a CLI reader, and two thirty-two character identifiers quoted
mid-sentence. The figures queued behind all that never sent — upstream refused
every message from the eleventh onward — and the run was recorded as failed
while the researcher sat looking at their answer.

The paper belongs in a file (see ``test_noninteractive_write_visibility``, which
is why it could not be written). These are the surfaces around it: what the chat
reply keeps, what it drops, and what still counts as a failed delivery.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omni.agent.turn_execution import TurnResult
from omni.channels.base import Channel
from omni.channels.outbound import (
    DeliveryPartResult,
    DeliveryReport,
    delivery_envelope_from_presentation,
)
from omni.runtime.notifications import TaskNotification
from omni.runtime.presentation import (
    ArtifactRef,
    TaskPresentation,
    task_presentation_from_notification,
    turn_presentation_from_result,
)

_TASK = "964f17aa42a94de5aacae5f738a7d74f"
_EXECUTION = "13a336de777043d69114a1d709072924"
# What the host says when it accepts background work, and what the submission
# card repeats back. A turn whose answer is this notice is the common case, not
# a contrived one: nothing else has happened yet for the answer to be about.
_SUBMISSION_NOTICE = (
    f"Submitted background skill scientific-figure as execution "
    f"{_EXECUTION} under task {_TASK}."
)


def _submitted_turn(text: str = "Done.") -> TurnResult:
    """The turn from the incident: an answer plus one background submission."""
    return TurnResult(
        text=text,
        session_id="sess-1",
        task_id=_TASK,
        submitted_subtask_ids=[_EXECUTION],
        tool_trace=[
            SimpleNamespace(
                result={
                    "status": "submitted",
                    "subtask_id": _EXECUTION,
                    "task_id": _TASK,
                    "skill_name": "scientific-figure",
                    "mode": "background",
                    "message": _SUBMISSION_NOTICE,
                }
            )
        ],
    )


def _chat(turn: TurnResult) -> str:
    """The message body as the transport builds it, local paths withheld."""
    return turn_presentation_from_result(turn, channel="wechat").to_markdown(
        include_local_paths=False
    )


def _terminal(turn: TurnResult) -> str:
    return turn_presentation_from_result(turn, channel="cli").to_markdown()


# ── what the host stops saying about itself ──


def test_a_degraded_card_does_not_look_like_it_is_still_running() -> None:
    card = TaskPresentation(
        subtask_id=_EXECUTION,
        skill="scientific-figure",
        status="degraded",
        summary="Partial result",
        task_id=_TASK,
        object_kind="skill_execution",
        object_id=_EXECUTION,
    )
    md = card.to_markdown(include_local_paths=False)
    assert "◷" not in md
    assert "(degraded)" in md
    assert md.startswith("!")


def test_a_chat_reply_does_not_report_how_the_host_chose_a_provider() -> None:
    """Routing is the host explaining itself, and the reader did not ask."""
    chat = _chat(_submitted_turn())

    assert "Plan decision" not in chat
    assert "Execution mode" not in chat


@pytest.mark.parametrize(
    "channel",
    ["wechat", "feishu", "dingtalk", "weixin", "lark", "dingding"],
)
def test_im_channels_share_chat_shaping(channel: str) -> None:
    turn = _submitted_turn()
    turn.plan_summary = "Plan decision: use scientific-figure as the background provider."
    chat = turn_presentation_from_result(turn, channel=channel).to_markdown(
        include_local_paths=False
    )
    terminal = turn_presentation_from_result(turn, channel="cli").to_markdown()

    assert "Plan decision" not in chat
    assert "Plan decision: use scientific-figure as the background provider." in terminal


def test_the_same_turn_still_explains_its_routing_in_the_terminal() -> None:
    """The reader there is at the machine that ran it; the next line is a prompt."""
    terminal = _terminal(_submitted_turn())

    assert "Plan decision: use scientific-figure as the background provider." in terminal
    assert "Execution mode: background" in terminal


def test_a_chat_reply_names_its_task_the_way_every_other_surface_does() -> None:
    """Identity survives — it is how the reader refers to this work later.

    What goes is the raw thirty-two character form, which is a record key that
    happened to be pasted into a sentence meant for a person.
    """
    chat = _chat(_submitted_turn())

    assert _TASK not in chat
    assert _EXECUTION not in chat
    assert _TASK[:8] in chat


def test_a_chat_reply_offers_two_ways_to_look_further_rather_than_four() -> None:
    """Look at the result, or carry it on — and nothing about the host itself.

    This was one command for a while, on the grounds that a thread can only
    afford so many. But the two are a pair: seeing a result and picking it back
    up are the whole interface to a task from a phone, and a reader who is only
    offered ``show`` has to know ``attach`` exists to ask for it.
    """
    chat = _chat(_submitted_turn())

    assert chat.count("/task ") == 2
    assert f"/task show {_TASK[:8]}" in chat
    assert f"/task attach {_TASK[:8]}" in chat
    assert "/inbox" not in chat
    assert "/verify" not in chat


def test_the_terminal_keeps_the_whole_menu() -> None:
    terminal = _terminal(_submitted_turn())

    assert "/task watch" in terminal
    assert "/inbox" in terminal


def test_a_chat_reply_does_not_restate_the_submission_it_just_announced() -> None:
    """The card says the work started, names it, and offers a menu.

    The reply it sits under has just said all three, so a request that has
    produced nothing yet arrived as a screenful of the host acknowledging
    itself — a heading, a status line, and four commands, ahead of any result.
    What the reader is waiting for is the completion card.
    """
    chat = _chat(_submitted_turn("Started the figure."))

    assert "Started the figure." in chat
    assert "(submitted)" not in chat
    assert "Result summary" not in chat
    assert "running in the background" not in chat
    assert "Submitted skill execution" not in chat


def test_dropping_that_card_does_not_drop_the_way_back_to_the_task() -> None:
    """The card was also the only thing carrying the follow-ups, so they move up.

    Without this the reply is an acknowledgement the reader cannot act on: the
    work is running under an id the message never says.
    """
    chat = _chat(_submitted_turn("Started the figure."))

    assert f"/task show {_TASK[:8]}" in chat
    assert f"/task attach {_TASK[:8]}" in chat


def test_a_reply_that_already_named_the_command_is_not_handed_a_menu() -> None:
    """Three statements of where to look, in a message about nothing else.

    The notice for this turn names the command and the id in its own sentence.
    The block that followed said them again, under a heading, having already
    been said once by the card the heading replaced.
    """
    chat = _chat(_submitted_turn(f"Planned. Use `/task show {_TASK[:8]}` to follow it."))

    assert chat.count("/task show") == 1
    assert "**Next actions**" not in chat


def test_an_answer_that_only_repeated_the_card_is_still_said() -> None:
    """The reply is suppressed when it duplicates the card; the card is gone.

    Both rules firing on the same turn would send an empty message, and the
    turn they both apply to is the common one: a submission whose answer is the
    submission notice.
    """
    chat = _chat(_submitted_turn(_SUBMISSION_NOTICE))

    assert _SUBMISSION_NOTICE.replace(_TASK, _TASK[:8]).replace(
        _EXECUTION, _EXECUTION[:8]
    ) in chat


def test_the_terminal_still_shows_the_submission_card() -> None:
    """There the card is the status line for a task about to be watched."""
    terminal = _terminal(_submitted_turn("Started the figure."))

    assert "(submitted)" in terminal
    assert "scientific-figure" in terminal


# ── how much of an answer a chat message may carry ──


def _long_answer() -> str:
    return "\n\n".join(f"## Section {n}\n" + "prose. " * 60 for n in range(1, 20))


def test_a_summary_that_already_fits_is_left_exactly_as_written() -> None:
    """The budget is a backstop; a model that summarized is not second-guessed."""
    summary = "Wrote the survey to rag_survey.md (8 sections, 15 references)."

    assert summary in _chat(_submitted_turn(summary))
    assert "Shortened for chat" not in _chat(_submitted_turn(summary))


def test_a_paper_typed_into_the_reply_is_cut_down_to_a_message() -> None:
    turn = _submitted_turn(_long_answer())

    chat = _chat(turn)

    assert len(chat) < len(_long_answer())
    assert "Shortened for chat" in chat


def test_what_was_cut_is_never_cut_without_saying_where_it_went() -> None:
    """Truncation destroys the only copy in the message, so it names another."""
    turn = _submitted_turn(_long_answer())
    turn.artifacts = [
        ArtifactRef(
            title="RAG survey",
            format="md",
            uri="artifact://a1b2c3d4",
            path="/w/artifacts/document/rag-survey-a1b2c3d4.md",
            mime="text/markdown",
            size_bytes=27_000,
        )
    ]

    assert "The full text is attached." in _chat(turn)


def test_with_nothing_attached_it_points_at_the_stored_task() -> None:
    """Asserted on the notice itself: the follow-up list mentions the same
    command whether or not anything was cut, so finding it there proves nothing.
    """
    chat = _chat(_submitted_turn(_long_answer()))
    notice = next(line for line in chat.splitlines() if "Shortened for chat" in line)

    assert f"/task show {_TASK[:8]}" in notice


def test_the_terminal_prints_the_answer_whole() -> None:
    """Truncating here would lose text the terminal can perfectly well show."""
    answer = _long_answer()

    assert answer in _terminal(_submitted_turn(answer))


# ── the files a reply talks about ──
#
# Task e5ce4d69 was a follow-up: everything had been produced on the turn before,
# so the model re-answered from context — "all three are ready, the figure is at
# /Users/antonio/.omni/projects/default/artifacts/figure/…" — and that reply left
# for WeChat with no attachment and an absolute path on somebody else's disk. A
# turn carries the artifacts it produced *itself*, and this one produced none.


@pytest.fixture
def figure(tmp_path: Path) -> Path:
    """A deliverable from an earlier turn, still on disk where it was written."""
    root = tmp_path / "artifacts" / "figure"
    root.mkdir(parents=True)
    path = root / "Scientific-Figure-e5ce4d69-d921117e.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 400)
    return path


def _chat_with_roots(turn: TurnResult, *roots: Path):  # noqa: ANN202
    return turn_presentation_from_result(turn, channel="wechat", output_roots=list(roots))


def _attached(presentation) -> list[str]:  # noqa: ANN001
    """Filenames the transport would upload alongside the message."""
    return [
        Path(part.path).name
        for part in delivery_envelope_from_presentation(presentation).parts
        if part.path
    ]


def test_a_reply_that_names_an_earlier_deliverable_sends_the_file(
    figure: Path, tmp_path: Path
) -> None:
    """The follow-up from the incident, which arrived with nothing to download."""
    turn = _submitted_turn(f"Already generated last round. The figure is at {figure}.")

    presentation = _chat_with_roots(turn, tmp_path / "artifacts")

    assert _attached(presentation) == [figure.name]


def test_the_file_is_found_when_the_reply_names_it_without_a_path(
    figure: Path, tmp_path: Path
) -> None:
    """A model that writes a filename means the file, not a string."""
    turn = _submitted_turn(f"Wrote the figure to `{figure.name}`.")

    assert _attached(_chat_with_roots(turn, tmp_path / "artifacts")) == [figure.name]


def test_a_chat_reader_is_not_given_a_directory_on_somebody_elses_machine(
    figure: Path, tmp_path: Path
) -> None:
    """Mid-sentence, the path is the part of that answer a phone cannot use.

    It is replaced by the filename, which is what the attachment beside the
    message is called. The location is not lost — the inventory below still
    carries it, for the owner of the machine that ran the work — but the prose
    stops carrying a line of somebody's home directory through a chat bubble.
    """
    turn = _submitted_turn(f"The figure is at {figure}.")

    chat = _chat_with_roots(turn, tmp_path / "artifacts").to_markdown(
        include_local_paths=False
    )
    prose = chat.partition("**Outputs**")[0]

    assert str(figure) not in prose
    assert figure.name in prose
    assert str(figure) in chat, "the inventory still says where the file is"


def test_the_terminal_still_prints_the_path_it_can_open(figure: Path) -> None:
    turn = _submitted_turn(f"The figure is at {figure}.")

    assert str(figure) in _terminal(turn)


def test_a_file_the_host_never_generated_is_never_uploaded(tmp_path: Path) -> None:
    """A filename is not a grant.

    Resolution only ever looks inside the directories the channel may send from,
    so naming something outside them finds nothing — which is what stops a reply
    from being a way to ask for any file on the host.
    """
    secret = tmp_path / "owner" / "id_rsa.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("PRIVATE KEY")
    (tmp_path / "artifacts").mkdir()
    turn = _submitted_turn(f"See {secret} and /etc/hosts.txt for details.")

    presentation = _chat_with_roots(turn, tmp_path / "artifacts")

    assert _attached(presentation) == []
    assert "PRIVATE KEY" not in presentation.to_markdown(include_local_paths=False)


def test_a_deliverable_the_turn_produced_is_not_attached_twice(
    figure: Path, tmp_path: Path
) -> None:
    """Naming what it just produced does not send it a second time."""
    turn = _submitted_turn(f"The figure is at {figure}.")
    turn.artifacts = [
        ArtifactRef(title="Scientific Figure", format="png", path=str(figure))
    ]

    assert _attached(_chat_with_roots(turn, tmp_path / "artifacts")) == [figure.name]


def test_a_name_that_matches_nothing_on_disk_adds_no_attachment(tmp_path: Path) -> None:
    """Saying a file exists does not make one; the reply is not evidence."""
    (tmp_path / "artifacts").mkdir()
    turn = _submitted_turn("Wrote it to rag_survey_paper.md.")

    assert _attached(_chat_with_roots(turn, tmp_path / "artifacts")) == []


def test_the_terminal_does_not_go_looking_for_files_at_all(figure: Path) -> None:
    """Its reader has a filesystem; an attachment list would be noise."""
    turn = _submitted_turn(f"The figure is at {figure}.")

    assert turn_presentation_from_result(turn, channel="cli").artifacts == []


def test_a_turn_that_produced_its_own_work_hands_over_only_that(
    figure: Path, tmp_path: Path
) -> None:
    """The last way an over-claim could still ship somebody else's work.

    The request drew a figure. A reply that also announces the survey is finished
    names a file that does exist — written by the task the user asked for it in —
    and looking names up would attach it to this task's reply.
    """
    drawn = tmp_path / "artifacts" / "figure" / "Scientific-Figure-ed444423-8021.png"
    drawn.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
    turn = _submitted_turn(f"All three materials are complete; the survey is {figure.name}.")
    turn.artifacts = [ArtifactRef(title="Scientific Figure", format="png", path=str(drawn))]

    presentation = _chat_with_roots(turn, tmp_path / "artifacts")

    assert _attached(presentation) == [drawn.name]


def test_a_turn_that_produced_nothing_may_point_at_what_it_means(
    figure: Path, tmp_path: Path
) -> None:
    """Fetching an earlier deliverable is what the mechanism is for.

    Answering from context about work already done produces nothing by
    definition, which is what distinguishes it from a turn that did the work.
    """
    turn = _submitted_turn(f"That was done earlier: {figure.name}.")

    assert _attached(_chat_with_roots(turn, tmp_path / "artifacts")) == [figure.name]


# ── the follow-up menu a finished task offers ──
#
# A completion arrives on its own, with none of the shaping a reply gets on the
# way out. So a chat reader was handed the skill's suggestions and the host's
# both: six lines of commands, the first three carrying a literal "<id>" they
# had no way to fill in.


def _completion(channel: str) -> str:
    note = TaskNotification(
        task_id=_TASK,
        subtask_id=_EXECUTION,
        skill_name="scientific-figure",
        status="succeeded",
        channel=channel,
        external_key="chat-1",
        summary="Figure generated.",
    )
    return task_presentation_from_notification(note, channel=channel).to_markdown()


def test_a_finished_task_offers_a_chat_reader_two_commands() -> None:
    """Look at the result, or carry it on. Nothing else is actionable here."""
    actions = [
        line for line in _completion("wechat").splitlines() if line.startswith("- /")
    ]

    assert actions == [
        f"- /task show {_TASK[:8]}: inspect details, trace, and the complete result",
        f"- /task attach {_TASK[:8]}: attach the result for follow-up or revision",
    ]


def test_the_terminal_keeps_the_whole_completion_menu() -> None:
    """Its reader is at the machine, one line above where they would run it."""
    assert "/verify --session" in _completion("cli")


def test_no_surface_is_offered_a_command_it_cannot_fill_in() -> None:
    """The skill used to declare navigation the host already owns, with a
    placeholder where the id belongs; it was printed above the real thing."""
    for channel in ("wechat", "cli"):
        assert "<id>" not in _completion(channel)


# ── the deliverables of the task, on every channel ──
#
# A chat reply once completed its inventory from the whole conversation, so that
# a reply reporting three finished materials would not ship only the one file its
# own turn had touched. Task ed444423 shows what that costs: a figure was
# generated, and the reply carried it along with a research report from an
# unrelated question two hours earlier and another from the morning before —
# the last twenty artifacts of a thread spanning two days and two topics, two of
# which were then uploaded to the user.
#
# The reply that motivated the widening was reporting materials that did not
# exist. Of "摘要、科研架构图、综述论文", the request produced figures and nothing
# else; the survey was written later, by the task the user finally asked for it
# in, and is owned by that task. Every deliverable in that conversation belongs
# to the task whose reply should hand it over, which is what these now assert.


def _paper(tmp_path: Path) -> Path:
    """A deliverable of a different task in the same conversation."""
    path = tmp_path / "artifacts" / "RAG-Survey-2e953101.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# A survey of retrieval-augmented generation\n", encoding="utf-8")
    return path


def _ref(path: Path, title: str) -> ArtifactRef:
    return ArtifactRef(title=title, format=path.suffix.lstrip("."), path=str(path))


def test_a_reply_hands_over_what_its_own_task_produced(
    figure: Path, tmp_path: Path
) -> None:
    """Incident ed444423: a figure was made, yesterday's reports were sent."""
    _paper(tmp_path)
    turn = _submitted_turn("架构图已生成。")
    turn.artifacts = [_ref(figure, "Scientific Figure")]

    presentation = turn_presentation_from_result(turn, channel="wechat")

    assert _attached(presentation) == [figure.name]


def test_a_claim_of_more_than_the_task_made_adds_no_files(
    figure: Path, tmp_path: Path
) -> None:
    """An over-claim is not repaired by attaching whatever is nearby.

    "Three materials, all done" was said of a request that had produced figures
    and no paper. Completing the inventory from the thread made the reply look
    answered while handing over files from another question.
    """
    _paper(tmp_path)
    turn = _submitted_turn("三项材料均已完成：摘要、科研架构图、综述论文。")
    turn.artifacts = [_ref(figure, "Scientific Figure")]

    presentation = turn_presentation_from_result(turn, channel="wechat")

    assert _attached(presentation) == [figure.name]


def test_the_two_surfaces_report_the_same_deliverables(
    figure: Path, tmp_path: Path
) -> None:
    """What the terminal prints for a task is what the chat reader receives."""
    _paper(tmp_path)
    turn = _submitted_turn("Done.")
    turn.artifacts = [_ref(figure, "Scientific Figure")]

    terminal = turn_presentation_from_result(turn, channel="cli")
    chat = turn_presentation_from_result(turn, channel="wechat")

    assert [Path(art.path).name for art in terminal.artifacts] == [figure.name]
    assert [Path(art.path).name for art in chat.artifacts] == [figure.name]


# ── one idea of what was produced, on both surfaces ──


def test_a_channel_attaches_exactly_what_the_terminal_calls_an_output(
    figure: Path, tmp_path: Path
) -> None:
    """The two readers are told about the same work, or neither can trust it.

    A chat-only notion of "deliverable" drifts from the terminal's, and the
    drift is invisible to whoever is at the machine: they see three outputs
    listed and cannot tell that a phone was sent one. So the set that arrives
    attached is the set the terminal prints under **Outputs**, and everything
    else a task wrote is reached the same way from either place, with
    ``/task show``.
    """
    dot = tmp_path / "artifacts" / "Scientific-Figure-e5ce4d69.dot"
    dot.parent.mkdir(parents=True, exist_ok=True)
    dot.write_text("digraph {}", encoding="utf-8")
    paper = _paper(tmp_path)
    turn = _submitted_turn("Done.")
    turn.artifacts = [
        _ref(figure, "Scientific Figure"),
        _ref(dot, "Figure source"),
        _ref(paper, "RAG Survey"),
    ]

    terminal = turn_presentation_from_result(turn, channel="cli").to_markdown()
    attached = _attached(turn_presentation_from_result(turn, channel="wechat"))

    assert attached == [figure.name, paper.name]
    assert all(name in terminal for name in attached)
    assert dot.name not in terminal, "a DOT source is not an output on either surface"


# ── where the files that were attached are ──


def test_a_chat_reply_says_where_its_deliverables_are(figure: Path, tmp_path: Path) -> None:
    """The reader owns the machine; the path is something they can paste.

    Scoped to the deliverables the terminal would list, so the two surfaces
    describe the same work. A process file is reached through ``/task show`` on
    either of them.
    """
    dot = tmp_path / "artifacts" / "Scientific-Figure-e5ce4d69.dot"
    dot.write_text("digraph {}", encoding="utf-8")
    turn = _submitted_turn("Figure regenerated.")
    turn.artifacts = [_ref(figure, "Scientific Figure"), _ref(dot, "Figure source")]

    chat = turn_presentation_from_result(turn, channel="wechat").to_markdown(
        include_local_paths=False
    )

    assert str(figure) in chat
    assert str(dot) not in chat


def test_a_deliverable_is_named_and_located_on_the_same_line(figure: Path) -> None:
    """Naming it under one heading and locating it under another read as two.

    A reply carrying three outputs said each of their names once as an
    inventory and again against a path, and the reader had to match the halves
    up by title to learn where the file they had just been handed lives.
    """
    turn = _submitted_turn("Figure regenerated.")
    turn.artifacts = [_ref(figure, "Scientific Figure")]

    chat = turn_presentation_from_result(turn, channel="wechat").to_markdown(
        include_local_paths=False
    )

    assert f"- Scientific Figure (png): `{figure}`" in chat
    assert chat.count("Scientific Figure") == 1


def test_the_terminal_states_a_location_once(figure: Path) -> None:
    """It prints locations inline already; a second copy is noise."""
    turn = _submitted_turn("Done.")
    turn.artifacts = [_ref(figure, "Scientific Figure")]

    assert _terminal(turn).count(str(figure)) == 1


# ── what a finished task says when it arrives on its own ──
#
# Task cbffcbb6 completed successfully: research-ideation wrote a thirty-two
# thousand character report, registered it, and returned both the report and an
# eighty-character summary of it. The completion notice carried the report — the
# transport cut it into eighteen messages and upstream accepted ten — and
# described the file only by ``artifact://`` URI, so there was nothing to upload.
# The delivery failed, and the task was settled failed on the strength of it.

_REPORT_URI = "artifact://738774cfe9ee48de94e3a274b2a69b9a"


def _ideation_note(**over: object) -> TaskNotification:
    """The notification from that task, with its report and its summary."""
    payload = {
        "status": "ok",
        "summary": "Generated 2 candidate ideas, best score: 7.7/10",
        "text": "# Research Ideation Report\n\n" + "prose. " * 5000,
        "artifacts": [{"uri": _REPORT_URI, "kind": "report", "ext": "md"}],
    }
    payload.update(over)
    return TaskNotification(
        subtask_id="e80a6b0d",
        skill_name="research-ideation",
        status="succeeded",
        channel="wechat",
        task_id=_TASK,
        session_id="sess-1",
        external_key="o9cq@im.wechat",
        summary=str(payload["summary"]),
        payload=payload,
    )


def test_a_completed_task_does_not_send_its_report_as_the_message() -> None:
    """The report is a file. What the message owes the reader is its summary."""
    note = _ideation_note()

    chat = task_presentation_from_notification(note, channel="wechat")

    assert chat.summary == "Generated 2 candidate ideas, best score: 7.7/10"
    assert "prose." not in chat.summary


def test_the_terminal_still_shows_the_whole_report() -> None:
    """`/task show` is where the result is meant to be legible in full."""
    terminal = task_presentation_from_notification(_ideation_note(), channel="cli")

    assert len(terminal.summary) > 30_000


def test_a_completion_with_a_body_that_already_fits_is_left_alone() -> None:
    """Nothing changes for the tasks that were never the problem."""
    note = _ideation_note(text="Found 3 relevant papers.")

    chat = task_presentation_from_notification(note, channel="wechat")

    assert chat.summary == "Found 3 relevant papers."


def test_a_long_body_with_no_summary_to_fall_back_on_is_bounded() -> None:
    """Bounding is the backstop, not the plan; it still names where the rest is."""
    note = _ideation_note(summary="")

    chat = task_presentation_from_notification(note, channel="wechat")

    assert len(chat.summary) < 3000
    assert "Shortened for chat" in chat.summary


def test_a_completion_fits_in_the_sends_upstream_will_accept() -> None:
    """Why the bound is a delivery property and not a matter of taste.

    Upstream accepted the first ten sends of a reply on task 964f17aa and
    answered ``ret=-2 prepare failed`` to every one after; on ed444423 it refused
    the fifteenth. The files are queued behind the prose, so a message that
    spends the reply on bubbles does not merely read badly — the attachments
    never go out. Keeping it to a few is what leaves room for them.
    """
    from omni.channels import weixin_ilink as wi

    chat = task_presentation_from_notification(_ideation_note(), channel="wechat")
    assert len(wi._chunk_text(chat.to_markdown(include_local_paths=False))) <= 4

    # The same result unbounded, which is the shape upstream refused.
    whole = task_presentation_from_notification(_ideation_note(), channel="cli")
    assert len(wi._chunk_text(whole.to_markdown())) > 10


def test_an_artifact_known_only_by_uri_cannot_be_attached() -> None:
    """The state that shipped: the entry names a store, not a file.

    Guards the premise of the fix below — without it there is nothing to prove,
    because a resolved path would look the same as never having needed one.
    """
    chat = task_presentation_from_notification(_ideation_note(), channel="wechat")

    assert [art.path for art in chat.artifacts] == [""]


class _Store:
    """The artifact store, which is the only thing that knows where files are."""

    def __init__(self, files: dict[str, Path]) -> None:
        self._files = files

    async def resolve_path(self, uri: str) -> Path | None:
        return self._files.get(uri)


class _Chat(Channel):
    name = "wechat"

    async def start(self) -> None:  # pragma: no cover - never started here
        return None


async def _located(note: TaskNotification, store: object) -> TaskPresentation:
    """The notification as the transport receives it, files and all."""
    settings = SimpleNamespace(paths=SimpleNamespace(project_dir=Path(".")))
    channel = _Chat.__new__(_Chat)
    channel.settings = settings  # type: ignore[attr-defined]
    channel.agent = SimpleNamespace(artifacts=store)  # type: ignore[attr-defined]
    return task_presentation_from_notification(
        await channel._located_artifacts(note), channel="wechat"
    )


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "artifacts" / "report" / "Research-ideation-cbffcbb6.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Research Ideation Report\n", encoding="utf-8")
    return path


async def test_the_report_a_completion_names_is_sent_as_a_file(report: Path) -> None:
    """A recipient of the summary can obtain what it summarizes."""
    presentation = await _located(_ideation_note(), _Store({_REPORT_URI: report}))

    parts = delivery_envelope_from_presentation(presentation).parts
    assert [(p.kind, p.path) for p in parts if p.kind == "file"] == [
        ("file", str(report))
    ]


async def test_the_attachment_is_labelled_by_what_it_is(report: Path) -> None:
    """The producer said "report"; that reads better than "md" or "artifact"."""
    presentation = await _located(_ideation_note(), _Store({_REPORT_URI: report}))

    assert [art.title for art in presentation.artifacts] == ["report"]
    assert [art.size_bytes for art in presentation.artifacts] == [report.stat().st_size]


async def test_a_uri_the_store_cannot_place_is_not_claimed_as_a_file() -> None:
    """An entry with no file behind it stays as it was, and the news still goes."""
    presentation = await _located(_ideation_note(), _Store({}))

    kinds = {p.kind for p in delivery_envelope_from_presentation(presentation).parts}
    assert "file" not in kinds
    assert presentation.summary == "Generated 2 candidate ideas, best score: 7.7/10"


# ── what counts as a failed delivery ──


def _report(*parts: tuple[str, str]) -> DeliveryReport:
    return DeliveryReport(
        target="o9cq@im.wechat",
        parts=[DeliveryPartResult(kind=kind, status=status) for kind, status in parts],
    )


def test_an_attachment_that_would_not_upload_does_not_fail_the_reply() -> None:
    """The researcher had the answer on screen; the record said the run failed.

    WeChat refused both figure uploads after accepting the answer. A file that
    did not send leaves the fallback link and the file on disk — the work is done
    and the task must not be settled as if it were not.
    """
    report = _report(("rich_text", "sent"), ("image", "failed"), ("file", "failed"))

    assert not report.failed
    assert report.degraded


def test_an_answer_that_never_arrived_is_a_failed_delivery() -> None:
    """Nothing else in the envelope substitutes for the reply itself."""
    report = _report(("rich_text", "failed"), ("file", "sent"))

    assert report.failed


def test_a_reply_that_had_to_drop_to_plain_text_is_only_degraded() -> None:
    report = _report(("rich_text", "degraded"), ("file", "sent"))

    assert not report.failed
    assert report.degraded


def test_a_delivery_with_nothing_wrong_is_neither() -> None:
    report = _report(("rich_text", "sent"), ("image", "sent"))

    assert not report.failed
    assert not report.degraded
