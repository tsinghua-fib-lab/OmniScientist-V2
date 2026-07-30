"""CLI Outputs and IM attachments are the same files, in the same order.

The researcher who compared CLI task 00c4fe62 with WeChat b0cd360c asked for
a 1:1 contract: if the terminal Outputs table lists N files, WeChat / Feishu /
DingTalk send N attachments — not a paper paste plus a leftover figure from
another task, and not a child-skill PNG without the survey sitting on the parent.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omni.agent.turn_execution import TurnResult
from omni.channels.base import Channel
from omni.channels.outbound import delivery_envelope_from_presentation
from omni.runtime.notifications import TaskNotification
from omni.runtime.presentation import (
    ArtifactRef,
    TaskPresentation,
    TurnPresentation,
    drop_delivered_attachments,
    inventory_attachment_keys,
    output_inventory,
    promises_later_deliverables,
    task_presentation_from_notification,
    turn_covers_deliverables,
    turn_presentation_from_result,
)

_TASK = "00c4fe62" + "a" * 24
_IM_CHANNELS = ("wechat", "feishu", "dingtalk")


def _ref(path: Path, *, title: str, fmt: str, mime: str = "") -> ArtifactRef:
    return ArtifactRef(
        title=title,
        format=fmt,
        path=str(path),
        uri=f"artifact://{path.stem}",
        mime=mime,
        size_bytes=path.stat().st_size,
    )


def _materials(tmp_path: Path) -> tuple[ArtifactRef, ArtifactRef, ArtifactRef, ArtifactRef]:
    root = tmp_path / "artifacts"
    (root / "report").mkdir(parents=True)
    (root / "figure").mkdir(parents=True)
    paper = root / "report" / "RAG-System-Survey.md"
    paper.write_text("# RAG survey\n", encoding="utf-8")
    png = root / "figure" / "Scientific-Figure-00c4fe62-aaaa.png"
    png.write_bytes(b"\x89PNG\r\n" + b"0" * 80)
    svg = root / "figure" / "Scientific-Figure-00c4fe62-bbbb.svg"
    svg.write_text("<svg/>", encoding="utf-8")
    dot = root / "figure" / "Scientific-Figure-00c4fe62-cccc.dot"
    dot.write_text("digraph G { a -> b }", encoding="utf-8")
    return (
        _ref(paper, title="RAG-System-Survey", fmt="md", mime="text/markdown"),
        _ref(png, title="Scientific Figure PNG", fmt="png", mime="image/png"),
        _ref(svg, title="Scientific Figure SVG", fmt="svg", mime="image/svg+xml"),
        _ref(dot, title="Scientific Figure DOT", fmt="dot", mime="text/vnd.graphviz"),
    )


def _turn(*artifacts: ArtifactRef, **kwargs: object) -> TurnResult:
    payload = dict(
        text="Three materials are ready.",
        session_id="sess-1",
        task_id=_TASK,
        artifacts=list(artifacts),
    )
    payload.update(kwargs)
    return TurnResult(**payload)  # type: ignore[arg-type]


def _cli_output_paths(turn: TurnResult) -> list[str]:
    return [item.path for item in output_inventory(turn_presentation_from_result(turn, channel="cli"))]


def _im_attachment_paths(turn: TurnResult, channel: str) -> list[str]:
    presentation = turn_presentation_from_result(turn, channel=channel)
    return [
        part.path
        for part in delivery_envelope_from_presentation(presentation).parts
        if part.path
    ]


@pytest.mark.parametrize("channel", _IM_CHANNELS)
def test_one_cli_output_is_one_im_attachment(tmp_path: Path, channel: str) -> None:
    paper, *_ = _materials(tmp_path)
    turn = _turn(paper)

    assert _cli_output_paths(turn) == [paper.path]
    assert _im_attachment_paths(turn, channel) == [paper.path]


@pytest.mark.parametrize("channel", _IM_CHANNELS)
def test_three_cli_outputs_are_three_im_attachments_without_the_dot(
    tmp_path: Path, channel: str
) -> None:
    """The CLI 00c4fe62 table: md + PNG + SVG. DOT is a process sidecar."""
    paper, png, svg, dot = _materials(tmp_path)
    turn = _turn(paper, png, svg, dot)

    assert _cli_output_paths(turn) == [paper.path, png.path, svg.path]
    assert _im_attachment_paths(turn, channel) == _cli_output_paths(turn)


def test_files_on_a_nested_task_card_are_not_added_when_the_turn_already_listed_outputs(
    tmp_path: Path,
) -> None:
    """Walking task.artifacts as a second group is how IM used to send extras."""
    paper, png, svg, _dot = _materials(tmp_path)
    extra = tmp_path / "artifacts" / "report" / "other-task.md"
    extra.write_text("from another card\n", encoding="utf-8")
    presentation = TurnPresentation(
        assistant_text="Three materials are ready.",
        task_id=_TASK,
        artifacts=[paper, png, svg],
        tasks=[
            TaskPresentation(
                subtask_id="child",
                skill="scientific-figure",
                status="succeeded",
                artifacts=[_ref(extra, title="other-task", fmt="md")],
            )
        ],
    )
    attached = [
        part.path
        for part in delivery_envelope_from_presentation(presentation).parts
        if part.path
    ]
    assert attached == [paper.path, png.path, svg.path]
    assert str(extra) not in attached


@pytest.mark.parametrize("channel", _IM_CHANNELS)
def test_a_pending_figure_turn_does_not_send_the_paper_ahead_of_the_inventory(
    tmp_path: Path, channel: str
) -> None:
    """IM drain_tasks=False; the completion notice is where all N files go."""
    paper, png, svg, _dot = _materials(tmp_path)
    turn = _turn(
        paper,
        png,
        svg,
        settlement_status="pending_child_task",
        text="# long paper\n\n" + ("section. " * 200),
        degraded_warnings=["Host filled remaining draft.manuscript via native synthesis."],
    )

    cli = _cli_output_paths(turn)
    im = turn_presentation_from_result(turn, channel=channel)
    chat = im.to_markdown(include_local_paths=True)

    assert cli == [paper.path, png.path, svg.path]
    assert _im_attachment_paths(turn, channel) == []
    assert im.artifacts == []
    assert "Degraded execution" not in chat
    assert "verification:" not in chat
    assert "Host filled remaining" not in chat
    assert "long paper" not in chat
    assert "Files will be sent" in chat
    assert turn_covers_deliverables(im) is False


def test_a_finished_im_turn_that_attached_files_covers_a_later_skill_notice(
    tmp_path: Path,
) -> None:
    paper, png, svg, _dot = _materials(tmp_path)
    turn = _turn(paper, png, svg)

    assert turn_covers_deliverables(turn_presentation_from_result(turn, channel="wechat")) is True


def test_cover_is_the_files_sent_not_the_fact_that_something_was_sent(
    tmp_path: Path,
) -> None:
    paper, png, svg, _dot = _materials(tmp_path)
    sent = turn_presentation_from_result(_turn(paper), channel="wechat")
    later = TurnPresentation(assistant_text="Figure ready.", artifacts=[paper, png, svg])

    delivered = inventory_attachment_keys(sent)
    leftover = drop_delivered_attachments(later, delivered)
    assert paper.path in delivered
    assert inventory_attachment_keys(leftover) == {png.uri, png.path, svg.uri, svg.path}


@pytest.mark.parametrize("channel", _IM_CHANNELS)
def test_prose_that_promises_a_later_pptx_withholds_the_markdown(
    tmp_path: Path, channel: str
) -> None:
    paper, *_ = _materials(tmp_path)
    turn = _turn(
        paper,
        text="PPT 生成完成后，你可以通过子任务记录取回 .pptx 文件。",
        submitted_subtask_ids=["child-pptx"],
    )
    im = turn_presentation_from_result(turn, channel=channel)

    assert promises_later_deliverables(turn.text) is True
    assert _im_attachment_paths(turn, channel) == []
    assert im.artifacts == []
    assert "Files will be sent" in im.to_markdown()


@pytest.mark.parametrize("channel", _IM_CHANNELS)
def test_an_im_approval_essay_is_not_the_user_visible_reply(
    tmp_path: Path, channel: str
) -> None:
    paper, *_ = _materials(tmp_path)
    essay = (
        "Writing files was intercepted again. The root cause is:\n\n"
        "sensitive tools triggered from an IM channel require local confirmation; "
        "run the request from the CLI on the owner's machine\n\n"
        "So I am pasting the paper below."
    )
    turn = _turn(paper, text=essay)
    chat = turn_presentation_from_result(turn, channel=channel).to_markdown()

    assert "require local confirmation" not in chat
    assert "run the request from the CLI" not in chat
    assert "Writing files was intercepted" not in chat
    assert "Deliverables are attached." in chat


class _Chat(Channel):
    name = "wechat"

    async def start(self) -> None:  # pragma: no cover
        return None


class _ParentStore:
    def __init__(self, rows: list[SimpleNamespace], paths: dict[str, Path]) -> None:
        self._rows = rows
        self._paths = paths

    async def list_by_task(self, task_id: str) -> list[SimpleNamespace]:
        assert task_id == _TASK
        return list(self._rows)

    async def resolve_path(self, uri: str) -> Path | None:
        return self._paths.get(uri)


def _row(path: Path, *, kind: str, title: str, uri: str) -> SimpleNamespace:
    return SimpleNamespace(
        kind=kind,
        title=title,
        uri=uri,
        mime="",
        size_bytes=path.stat().st_size,
        rel_path=str(path),
    )


@pytest.mark.asyncio
async def test_a_figure_completion_notice_carries_the_parent_cli_inventory(
    tmp_path: Path,
) -> None:
    """scientific-figure finishing must not ship PNG alone while the survey sits on the parent."""
    paper, png, svg, dot = _materials(tmp_path)
    paths = {ref.uri: Path(ref.path) for ref in (paper, png, svg, dot)}
    rows = [
        _row(Path(paper.path), kind="report", title=paper.title, uri=paper.uri),
        _row(Path(png.path), kind="figure", title=png.title, uri=png.uri),
        _row(Path(svg.path), kind="figure", title=svg.title, uri=svg.uri),
        _row(Path(dot.path), kind="figure", title=dot.title, uri=dot.uri),
    ]
    store = _ParentStore(rows, paths)
    channel = _Chat.__new__(_Chat)
    channel.settings = SimpleNamespace(paths=SimpleNamespace(project_dir=tmp_path))
    channel.agent = SimpleNamespace(artifacts=store)
    channel.name = "wechat"

    note = TaskNotification(
        subtask_id="f723c28f" + "0" * 24,
        skill_name="scientific-figure",
        status="succeeded",
        channel="wechat",
        task_id=_TASK,
        summary="Figure rendered.",
        artifacts=[png.uri],
        payload={
            "summary": "Figure rendered.",
            "artifacts": [
                {"title": png.title, "uri": png.uri, "format": "png", "path": png.path}
            ],
        },
    )
    located = await channel._located_artifacts(note)
    presentation = task_presentation_from_notification(located, channel="wechat")
    attached = [
        part.path
        for part in delivery_envelope_from_presentation(presentation).parts
        if part.path
    ]

    assert attached == [paper.path, png.path, svg.path]
    assert dot.path not in attached
    chat = presentation.to_markdown()
    assert "verification:" not in chat
    assert "Execution contract" not in chat


class _KeyStore:
    def __init__(self, keys: set[str]) -> None:
        self._keys = keys

    async def delivered_attachment_keys(self, *_args: object, **_kwargs: object) -> set[str]:
        return set(self._keys)

    async def claim_delivery(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def finish_delivery(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def append_event(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def get_task(self, *_args: object, **_kwargs: object) -> None:
        return None


class _SendingChat(_Chat):
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.name = "wechat"
        self._outbound_locks = {}

    async def send_turn(self, _external_key: str, presentation: object) -> None:
        self.sent.append(presentation)


def _pptx(tmp_path: Path) -> ArtifactRef:
    path = tmp_path / "artifacts" / "presentation" / "deck.pptx"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK" + b"0" * 80)
    return _ref(path, title="Loop Engineering deck", fmt="pptx", mime="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@pytest.mark.asyncio
async def test_a_later_pptx_notice_still_sends_after_the_parent_attached_only_the_markdown(
    tmp_path: Path,
) -> None:
    paper, *_ = _materials(tmp_path)
    deck = _pptx(tmp_path)
    store = _ParentStore(
        [
            _row(Path(paper.path), kind="report", title=paper.title, uri=paper.uri),
            _row(Path(deck.path), kind="slides", title=deck.title, uri=deck.uri),
        ],
        {paper.uri: Path(paper.path), deck.uri: Path(deck.path)},
    )
    channel = _SendingChat()
    channel.settings = SimpleNamespace(paths=SimpleNamespace(project_dir=tmp_path))
    channel.agent = SimpleNamespace(artifacts=store, tasks=_KeyStore({paper.uri, paper.path}))

    status = await channel.send_task_notification(
        TaskNotification(
            subtask_id="41ef68eb" + "0" * 24,
            skill_name="research-pptx",
            status="succeeded",
            object_kind="skill_execution",
            channel="wechat",
            external_key="user-a",
            task_id=_TASK,
            summary="Deck rendered.",
            artifacts=[deck.uri],
            payload={
                "summary": "Deck rendered.",
                "artifacts": [
                    {"title": deck.title, "uri": deck.uri, "format": "pptx", "path": deck.path}
                ],
            },
        )
    )

    assert status == "sent"
    assert len(channel.sent) == 1
    attached = [
        part.path
        for part in delivery_envelope_from_presentation(channel.sent[0]).parts
        if part.path
    ]
    assert attached == [deck.path]


@pytest.mark.asyncio
async def test_a_notice_that_only_repeats_already_sent_files_is_suppressed(
    tmp_path: Path,
) -> None:
    paper, *_ = _materials(tmp_path)
    store = _ParentStore(
        [_row(Path(paper.path), kind="report", title=paper.title, uri=paper.uri)],
        {paper.uri: Path(paper.path)},
    )
    channel = _SendingChat()
    channel.settings = SimpleNamespace(paths=SimpleNamespace(project_dir=tmp_path))
    channel.agent = SimpleNamespace(artifacts=store, tasks=_KeyStore({paper.uri, paper.path}))

    status = await channel.send_task_notification(
        TaskNotification(
            subtask_id="41ef68eb" + "0" * 24,
            skill_name="research-pptx",
            status="succeeded",
            object_kind="skill_execution",
            channel="wechat",
            external_key="user-a",
            task_id=_TASK,
            summary="Materials ready.",
            artifacts=[paper.uri],
            payload={
                "summary": "Materials ready.",
                "artifacts": [
                    {"title": paper.title, "uri": paper.uri, "format": "md", "path": paper.path}
                ],
            },
        )
    )

    assert status == "sent"
    assert channel.sent == []
