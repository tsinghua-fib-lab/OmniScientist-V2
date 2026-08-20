"""Cross-channel presentation and inbound/outbound channel behavior."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omni.agent.orchestrator import TurnResult
from omni.channels.base import Channel
from omni.runtime.notifications import TaskNotification


def _task_payload() -> dict:
    return {
        "summary": "Transformer 架构图完成。",
        "artifacts": [
            {
                "title": "Transformer PNG",
                "format": "png",
                "uri": "artifact://png1",
                "path": "/tmp/transformer.png",
                "mime": "image/png",
            },
            {
                "title": "Transformer SVG",
                "format": "svg",
                "uri": "artifact://svg1",
                "path": "/tmp/transformer.svg",
                "mime": "image/svg+xml",
            },
        ],
        "research": {
            "source_ids": ["src-vaswani"],
            "claim_ids": ["claim-encoder"],
            "evidence_ids": ["ev-fig1"],
            "run_id": "run-dot",
        },
        "run_id": "run-dot",
    }


def test_task_presentation_markdown_contains_provenance_and_actions():
    from omni.runtime.presentation import task_presentation_from_result

    presentation = task_presentation_from_result(
        subtask_id="task-123456",
        skill="scientific-figure",
        status="succeeded",
        result=_task_payload(),
    )
    md = presentation.to_markdown()

    assert "Transformer 架构图完成" in md
    assert "/tmp/transformer.png" in md
    assert "src-vasw" in md
    assert "claim-e" in md
    assert "ev-fig1" in md
    assert "run-dot" in md
    assert "/task show task-123" in md
    assert "/verify --session" in md


def test_a_chat_reply_does_not_carry_the_provenance_ledger():
    """Ids naming rows in a store the reader has no way to open.

    The terminal keeps them, because there they are something to look up. In a
    thread they arrived as a heading of record keys sitting underneath the
    figure that had actually been asked for, and every one of them cost a line.
    """
    from omni.runtime.presentation import task_presentation_from_result

    presentation = task_presentation_from_result(
        subtask_id="task-123456",
        skill="scientific-figure",
        status="succeeded",
        result=_task_payload(),
    )

    chat = presentation.to_markdown(include_local_paths=False)

    assert "**Research record**" not in chat
    assert "run-dot" not in chat
    assert "src-vasw" not in chat
    assert "Transformer PNG (png): `/tmp/transformer.png`" in chat


def test_task_presentation_prefers_full_text_over_compact_summary():
    from omni.runtime.presentation import task_presentation_from_result

    presentation = task_presentation_from_result(
        subtask_id="task-idea123",
        skill="research-ideation",
        status="succeeded",
        result={
            "summary": "Generated 3 research ideas.",
            "text": "# Research Ideation Report\n\nFull user-facing report.",
        },
    )

    assert presentation.summary == "# Research Ideation Report\n\nFull user-facing report."


def test_action_required_task_result_presents_configuration_as_needs_input():
    from omni.runtime.presentation import task_presentation_from_result

    gateway_result = {
        "status": "error",
        "summary": "livefigure requires VLM configuration. Run `omni config vlm` and retry.",
        "error_info": {"code": "vlm_not_configured", "category": "configuration"},
        "action_required": {
            "kind": "configure",
            "command": "omni config vlm",
            "missing": ["model", "endpoint", "api_key"],
        },
        "next_actions": ["omni config vlm"],
    }
    presentation = task_presentation_from_result(
        subtask_id="task-vlm123",
        skill="livefigure",
        status="failed",
        result=gateway_result,
        error="livefigure requires VLM configuration.",
    )

    assert presentation.status == "needs_input"
    assert presentation.error == ""
    assert presentation.next_actions[0] == "omni config vlm"
    assert "omni config vlm" in presentation.to_markdown()

    optional_step = task_presentation_from_result(
        subtask_id="workflow-vlm123",
        skill="research-workflow",
        status="degraded",
        result={
            "summary": "The report completed without the optional editable figure.",
            "steps": [{"status": "failed", "required": False, "result": gateway_result}],
        },
    )
    assert optional_step.status == "degraded"
    assert optional_step.next_actions[0] == "omni config vlm"


def test_dependency_install_action_remains_failed_instead_of_needs_input():
    from omni.runtime.presentation import task_presentation_from_result

    result = {
        "status": "error",
        "summary": (
            "research-pptx renderer setup is incomplete. "
            "Run `omni skills setup research-pptx` in a terminal."
        ),
        "error": "research-pptx renderer dependencies are missing.",
        "error_info": {"code": "runtime_dependency_missing", "category": "dependency"},
        "action_required": {
            "kind": "install",
            "command": "omni skills setup research-pptx",
            "missing": ["pptxgenjs", "sharp"],
        },
        "next_actions": ["omni skills setup research-pptx"],
    }

    presentation = task_presentation_from_result(
        subtask_id="task-pptx123",
        skill="research-pptx",
        status="failed",
        result=result,
        error=result["error"],
    )

    assert presentation.status == "failed"
    assert presentation.error == result["error"]
    assert "omni skills setup research-pptx" in presentation.to_markdown()


def test_task_presentation_hides_dot_artifacts_from_cli_and_im_results():
    """Rendered results omit DOT source files while keeping user deliverables."""
    from omni.runtime.presentation import task_presentation_from_result

    presentation = task_presentation_from_result(
        subtask_id="task-123456",
        skill="scientific-figure",
        status="succeeded",
        result={
            "summary": "figure done",
            "artifacts": [
                {
                    "title": "RAG 系统架构图",
                    "format": "png",
                    "uri": "artifact://png1",
                    "path": "/srv/omni/artifacts/figure/RAG-系统架构图-10ddda65.png",
                    "mime": "image/png",
                    "size_bytes": 34_567,
                },
                {
                    "title": "RAG 系统架构图 DOT",
                    "uri": "artifact://dot1",
                    "path": "/srv/omni/artifacts/figure/RAG-系统架构图-DOT-77aa88bb.dot",
                    "mime": "text/vnd.graphviz",
                    "size_bytes": "512",
                },
            ],
        },
    )

    im = presentation.to_markdown(include_local_paths=False)
    # Chat is told about the same deliverables as the terminal, and no others:
    # the DOT source is how the figure was made, not the figure. It is reached
    # the same way on both surfaces, through ``/task show``.
    assert (
        "RAG 系统架构图 (png, 33.8 KB): "
        "`/srv/omni/artifacts/figure/RAG-系统架构图-10ddda65.png`"
    ) in im
    assert "RAG 系统架构图 DOT" not in im
    assert "artifact://dot1" not in im
    assert "77aa88bb.dot" not in im

    cli = presentation.to_markdown()
    assert "/srv/omni/artifacts/figure/RAG-系统架构图-10ddda65.png" in cli
    assert "artifact://png1" not in cli
    assert "RAG-系统架构图-DOT-77aa88bb.dot" not in cli
    assert "artifact://dot1" not in cli


def test_im_markdown_truncation_hint_never_falls_back_to_local_path():
    from dataclasses import replace

    from omni.runtime.presentation import task_presentation_from_result

    presentation = task_presentation_from_result(
        subtask_id="task-123456",
        skill="workflow",
        status="succeeded",
        result={
            "summary": "done",
            "artifacts": [
                {"title": "Draft", "format": "md", "path": "/srv/omni/artifacts/report/draft-1a2b3c4d.md"}
            ],
        },
    )
    art = replace(presentation.artifacts[0], preview="body…", preview_truncated=True)
    presentation = replace(presentation, artifacts=[art])

    im = presentation.to_markdown(include_local_paths=False)
    # The hint is an instruction the reader is meant to follow, so it has to name
    # something they can act on from where they are sitting. The inventory above
    # it is a different offer and does quote the path.
    hint = next(line for line in im.splitlines() if "Preview truncated" in line)
    assert "/srv/omni" not in hint
    assert "/task show task-123" in hint

    cli = presentation.to_markdown()
    assert "open_artifact /srv/omni/artifacts/report/draft-1a2b3c4d.md" in cli


def test_turn_presentation_includes_submitted_task_next_actions():
    from omni.runtime.presentation import turn_presentation_from_result

    turn = TurnResult(
        text="已提交后台任务。",
        session_id="sess-1",
        submitted_subtask_ids=["task-abc123"],
        drained_results=[],
    )

    presentation = turn_presentation_from_result(turn)

    assert presentation.assistant_text == "已提交后台任务。"
    assert presentation.submitted_subtask_ids == ["task-abc123"]
    assert presentation.tasks
    assert presentation.tasks[0].status == "submitted"
    assert any("/task watch" in action for action in presentation.tasks[0].next_actions)


def test_im_turn_presentation_hides_internal_plan_summary_and_needs_input_verification():
    from omni.runtime.presentation import turn_presentation_from_result

    turn = TurnResult(
        text="我需要先知道要修改哪一个产物。请提供 task id，或先使用 /task attach <id>。",
        session_id="sess-1",
        task_id="run-123456",
        kind="needs_input",
        plan_summary="计划：needs_input；原因：internal planner diagnostic",
        settlement_status="needs_input",
    )

    presentation = turn_presentation_from_result(turn, channel="wechat")
    md = presentation.to_markdown()

    assert "计划：" not in md
    assert "verification" not in md
    assert "我需要先知道" in md


def test_im_turn_presentation_hides_partial_raw_tool_trace():
    from omni.runtime.presentation import turn_presentation_from_result

    turn = TurnResult(
        text=(
            "部分结果：工具调用次数超出上限。\n\n"
            "已完成：\n"
            "- memory_search（成功）：{'matches': [{'summary': 'old artifact'}]}\n"
            "- read_file（成功）：{\"title\":\"Attention Is All You Need\"}"
        ),
        session_id="sess-1",
        task_id="run-abcdef123",
        kind="partial",
        terminated_reason="max_tool_calls",
        plan_summary="计划：react_fallback；原因：debug",
        settlement_status="salvaged",
    )

    cli = turn_presentation_from_result(turn)
    assert "memory_search" in cli.to_markdown()

    im = turn_presentation_from_result(turn, channel="wechat")
    md = im.to_markdown()
    assert "memory_search" not in md
    assert "Attention Is All You Need" not in md
    assert "react_fallback" not in md
    assert "tool budget reached" in md
    assert "run_ab" not in md
    assert "run-abcd" in md
    assert "task events" in md


def test_im_turn_presentation_preserves_synthesized_bounded_answer():
    from omni.runtime.presentation import turn_presentation_from_result

    turn = TurnResult(
        text="RAG 通过检索外部证据、约束生成上下文并绑定引用来降低事实性幻觉。",
        session_id="sess-1",
        task_id="run-abcdef123",
        kind="text",
        terminated_reason="synthesized_max_tool_calls",
    )

    presentation = turn_presentation_from_result(turn, channel="feishu")
    md = presentation.to_markdown()

    assert "RAG 通过检索外部证据" in md
    assert "tool budget reached" in md
    assert "some exploration stopped" in md


def test_feishu_http_errors_include_response_body():
    import httpx

    from omni.channels.outbound import OutboundError, _raise_for_status

    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://open.feishu.cn/open-apis/im/v1/messages"),
        json={"code": 230001, "msg": "invalid receive_id"},
    )

    with pytest.raises(OutboundError) as exc_info:
        _raise_for_status(response, "Feishu post send")

    message = str(exc_info.value)
    assert "HTTP 400" in message
    assert "230001" in message
    assert "invalid receive_id" in message


@pytest.mark.asyncio
async def test_feishu_markdown_uses_post_for_rich_content():
    from omni.channels.outbound import FeishuClient

    client = FeishuClient({"app_id": "app", "app_secret": "secret"})
    sent: list[tuple[str, str, dict]] = []

    async def fake_send_message(target, msg_type, content):  # noqa: ANN001
        sent.append((target, msg_type, content))

    client._send_message = fake_send_message  # type: ignore[method-assign]

    await client.send_markdown(
        "chat-1",
        "## 任务完成\n\n✅ **workflow** `id=c98e4330`\n\n可以继续查看任务详情。",
    )

    assert sent[0][0] == "chat-1"
    assert sent[0][1] == "post"
    # The im/v1/messages API expects the post body unwrapped ({"zh_cn": ...});
    # the extra {"post": ...} envelope is webhook-only and triggers 230001.
    assert "post" not in sent[0][2]
    assert "zh_cn" in sent[0][2]
    dumped = json.dumps(sent[0][2], ensure_ascii=False)
    assert "```" not in dumped
    assert "**" not in dumped
    assert "`" not in dumped
    assert "workflow" in dumped


def test_feishu_post_content_api_shape_has_no_post_wrapper():
    from omni.channels.outbound import _feishu_post_content, _feishu_webhook_post_content

    body = _feishu_post_content("## 标题\n\n正文一行")
    # Send-message API content: {"zh_cn": {...}} — NO "post" wrapper.
    assert set(body.keys()) == {"zh_cn"}
    zh = body["zh_cn"]
    assert zh["title"] == "OmniScientist"
    assert isinstance(zh["content"], list) and zh["content"]
    assert all(isinstance(para, list) and para for para in zh["content"])
    assert any(
        node.get("text") == "正文一行" for para in zh["content"] for node in para
    )

    # Custom-bot webhook keeps the extra {"post": ...} envelope around that body.
    webhook = _feishu_webhook_post_content("正文一行")
    assert set(webhook.keys()) == {"post"}
    assert webhook["post"] == _feishu_post_content("正文一行")


@pytest.mark.asyncio
async def test_feishu_webhook_post_send_wraps_content_under_post_key():
    import httpx

    from omni.channels.outbound import FeishuClient

    captured: list[dict] = []

    class _Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"code": 0})

    real_client = httpx.AsyncClient

    def _patched(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        kwargs["transport"] = _Transport()
        return real_client(*args, **kwargs)

    client = FeishuClient({"webhook_url": "https://open.feishu.cn/hook/x"})
    httpx.AsyncClient = _patched  # type: ignore[assignment]
    try:
        await client.send_markdown("chat-1", "## 任务完成\n\n可以继续查看任务详情。")
    finally:
        httpx.AsyncClient = real_client  # type: ignore[assignment]

    assert captured and captured[0]["msg_type"] == "post"
    # Webhook content IS wrapped: {"post": {"zh_cn": {...}}}.
    assert "post" in captured[0]["content"]
    assert "zh_cn" in captured[0]["content"]["post"]


@pytest.mark.asyncio
async def test_feishu_markdown_uses_text_for_code_blocks():
    from omni.channels.outbound import FeishuClient

    client = FeishuClient({"app_id": "app", "app_secret": "secret"})
    sent: list[tuple[str, str, dict]] = []

    async def fake_send_message(target, msg_type, content):  # noqa: ANN001
        sent.append((target, msg_type, content))

    client._send_message = fake_send_message  # type: ignore[method-assign]

    await client.send_markdown(
        "chat-1",
        "## Transformer\n\n```mermaid\nflowchart LR\nA-->B\n```",
    )

    assert sent[0][1] == "text"
    assert "flowchart LR" in sent[0][2]["text"]
    assert "```" not in sent[0][2]["text"]


@pytest.mark.asyncio
async def test_feishu_markdown_falls_back_to_text_when_post_is_rejected(caplog):
    from omni.channels.outbound import FeishuClient, OutboundError

    client = FeishuClient({"app_id": "app", "app_secret": "secret"})
    sent: list[tuple[str, str, dict]] = []

    async def fake_send_message(target, msg_type, content):  # noqa: ANN001
        sent.append((target, msg_type, content))
        if msg_type == "post":
            raise OutboundError("Feishu message API failed: 400 body={\"code\":230001}")

    client._send_message = fake_send_message  # type: ignore[method-assign]

    await client.send_markdown(
        "chat-1",
        "## Transformer\n\n✅ **scientific-figure** `id=ded7af96`",
    )

    assert [item[1] for item in sent] == ["post", "text"]
    assert sent[1][2]["text"].startswith("Transformer")
    assert "```" not in sent[1][2]["text"]
    assert "**" not in sent[1][2]["text"]
    assert "body={\"code\":230001}" in caplog.text


@pytest.mark.asyncio
async def test_send_presentation_continues_artifacts_when_markdown_send_fails(tmp_path):
    from omni.channels.outbound import send_presentation
    from omni.runtime.presentation import task_presentation_from_result

    document = tmp_path / "result.md"
    document.write_text("# result", encoding="utf-8")
    presentation = task_presentation_from_result(
        subtask_id="ded7af96abc",
        skill="scientific-figure",
        status="succeeded",
        result={
            "summary": "figure done",
            "artifacts": [{"title": "Result", "format": "md", "path": str(document), "mime": "text/markdown"}],
        },
    )
    client = _FailingMarkdownOutbound()

    report = await send_presentation(client, "chat-1", presentation)

    assert client.files == [("chat-1", str(document))]
    assert report.failed
    assert any(part.kind == "rich_text" and part.status == "failed" for part in report.parts)


@pytest.mark.asyncio
async def test_send_presentation_routes_text_images_and_files_by_part_type(tmp_path):
    from omni.channels.outbound import send_presentation
    from omni.runtime.presentation import task_presentation_from_result

    image = tmp_path / "transformer.png"
    document = tmp_path / "transformer.md"
    image.write_bytes(b"png")
    document.write_text("# Transformer notes", encoding="utf-8")
    presentation = task_presentation_from_result(
        subtask_id="ded7af96abc",
        skill="scientific-figure",
        status="succeeded",
        result={
            "summary": "```mermaid\nflowchart LR\nA-->B\n```",
            "artifacts": [
                {"title": "PNG", "format": "png", "path": str(image), "mime": "image/png"},
                {"title": "Notes", "format": "md", "path": str(document), "mime": "text/markdown"},
            ],
        },
    )
    client = _TypedOutbound()

    report = await send_presentation(client, "chat-1", presentation)

    assert not client.rich_texts
    assert client.texts and "flowchart LR" in client.texts[0][1]
    assert client.images == [("chat-1", str(image))]
    assert client.files == [("chat-1", str(document))]
    assert not report.failed
    assert not report.degraded


@pytest.mark.asyncio
async def test_send_presentation_uploads_nested_workflow_artifacts(tmp_path):
    from omni.channels.outbound import send_presentation
    from omni.runtime.presentation import task_presentation_from_result

    image = tmp_path / "figure.png"
    document = tmp_path / "chapter.md"
    image.write_bytes(b"png")
    document.write_text("# chapter", encoding="utf-8")
    presentation = task_presentation_from_result(
        subtask_id="c98e4330abc",
        skill="workflow",
        status="succeeded",
        result={
            "summary": "workflow done",
            "steps": [
                {
                    "id": "figure",
                    "skill_name": "scientific-figure",
                    "status": "succeeded",
                    "result": {
                        "summary": "figure done",
                        "artifacts": [
                            {
                                "title": "Transformer figure",
                                "format": "png",
                                "path": str(image),
                                "mime": "image/png",
                            }
                        ],
                    },
                },
                {
                    "id": "writing",
                    "skill_name": "synthesis.final",
                    "status": "succeeded",
                    "result": {
                        "summary": "chapter done",
                        "files": [{"title": "Chapter draft", "path": str(document), "format": "md"}],
                    },
                },
            ],
        },
    )
    client = _FakeFileOutbound()

    report = await send_presentation(client, "chat-1", presentation)

    assert client.markdown and "Result summary" in client.markdown[0][1]
    assert client.images == [("chat-1", str(image))]
    assert client.files == [("chat-1", str(document))]
    assert not report.failed


@pytest.mark.asyncio
async def test_send_presentation_sends_svg_as_file_and_skips_dot_sidecar(tmp_path):
    from omni.channels.outbound import send_presentation
    from omni.runtime.presentation import task_presentation_from_result

    dot = tmp_path / "transformer.dot"
    svg = tmp_path / "transformer.svg"
    png = tmp_path / "transformer.png"
    dot.write_text("digraph G {}", encoding="utf-8")
    svg.write_text("<svg></svg>", encoding="utf-8")
    png.write_bytes(b"png")
    presentation = task_presentation_from_result(
        subtask_id="0a4664e6abc",
        skill="scientific-figure",
        status="succeeded",
        result={
            "summary": "figure done",
            "artifacts": [
                {"title": "DOT", "format": "dot", "path": str(dot), "mime": "text/vnd.graphviz"},
                {"title": "SVG", "format": "svg", "path": str(svg), "mime": "image/svg+xml"},
                {"title": "PNG", "format": "png", "path": str(png), "mime": "image/png"},
            ],
        },
    )
    client = _FailingSvgImageOutbound()

    report = await send_presentation(client, "chat-1", presentation)

    # Rendered deliverables are uploaded; the .dot source is not user-visible.
    assert client.files == [("chat-1", str(svg))]
    assert client.images == [("chat-1", str(png))]
    assert all("DOT" not in message for _, message in client.markdown)
    assert all("transformer.dot" not in message for _, message in client.markdown)
    assert not report.failed


@pytest.mark.asyncio
async def test_send_presentation_does_not_show_or_upload_dot_only_result(tmp_path):
    from omni.channels.outbound import send_presentation
    from omni.runtime.presentation import task_presentation_from_result

    dot = tmp_path / "pipeline.dot"
    dot.write_text("digraph G { a -> b }", encoding="utf-8")
    presentation = task_presentation_from_result(
        subtask_id="0a4664e6abc",
        skill="scientific-figure",
        status="succeeded",
        result={
            "summary": "source only",
            "artifacts": [
                {"title": "Pipeline DOT", "format": "dot", "path": str(dot), "mime": "text/vnd.graphviz"}
            ],
        },
    )
    client = _FakeFileOutbound()

    await send_presentation(client, "chat-1", presentation)

    assert client.files == []
    assert all("Pipeline DOT" not in message for _, message in client.markdown)
    assert all("pipeline.dot" not in message for _, message in client.markdown)


def test_delivery_hides_support_records_and_caps_primary_attachments() -> None:
    from omni.channels.outbound import delivery_envelope_from_presentation
    from omni.runtime.presentation import task_presentation_from_result

    artifacts = [
        {
            "title": f"Report {index:02d}",
            "format": "md",
            "path": f"/workspace/reports/report-{index:02d}.md",
        }
        for index in range(14)
    ]
    artifacts.extend(
        [
            {
                "title": "Internal receipt",
                "format": "json",
                "path": "/workspace/data/receipt.json",
                "presentation_role": "support",
            },
            {
                "title": "DOT source",
                "format": "dot",
                "path": "/workspace/figures/source.dot",
            },
        ]
    )
    presentation = task_presentation_from_result(
        subtask_id="task-output-limit",
        skill="workflow",
        status="succeeded",
        result={"summary": "done", "artifacts": artifacts},
    )

    envelope = delivery_envelope_from_presentation(presentation)

    assert len(envelope.parts) == 13  # one message plus twelve attachments
    assert "Report 00" in envelope.parts[0].text
    assert "Report 11" in envelope.parts[0].text
    assert "Report 12" not in envelope.parts[0].text
    assert "2 additional artifact" in envelope.parts[0].text
    assert "Internal receipt" not in envelope.parts[0].text
    assert "DOT source" not in envelope.parts[0].text


@pytest.mark.asyncio
async def test_send_delivery_rejects_external_artifact_path_without_leaking_absolute_path(tmp_path):
    from omni.channels.outbound import DeliveryEnvelope, DeliveryPart, send_delivery

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    external = tmp_path / "external" / "secret.txt"
    external.parent.mkdir()
    external.write_text("secret", encoding="utf-8")
    client = _TypedOutbound()

    report = await send_delivery(
        client,
        "chat-1",
        DeliveryEnvelope(parts=[DeliveryPart(kind="file", title="Secret", path=str(external), format="txt")]),
        allowed_roots=[artifact_root],
    )

    assert not client.files
    assert report.degraded
    assert client.texts
    assert str(external) not in client.texts[0][1]
    assert "Secret" in client.texts[0][1]


@pytest.mark.asyncio
async def test_task_notification_records_degraded_delivery(settings):
    from omni.channels.outbound import DeliveryPartResult, DeliveryReport
    from omni.runtime.notifications import read_delivery_statuses

    class _DegradedChannel(Channel):
        name = "dummy"

        async def start(self) -> None:
            return None

        async def send_turn(self, external_key, presentation):  # noqa: ANN001
            return DeliveryReport(
                target=external_key,
                parts=[
                    DeliveryPartResult(
                        kind="file",
                        status="degraded",
                        title="Figure",
                        message="file upload failed; sent text fallback",
                    )
                ],
            )

    channel = _DegradedChannel(settings, _DummyAgent())
    note = TaskNotification(
        subtask_id="task-degraded",
        skill_name="scientific-figure",
        status="succeeded",
        channel="dummy",
        external_key="chat-1",
        session_id="sess-1",
    )

    await channel.send_task_notification(note)

    rows = read_delivery_statuses(settings.paths.project_dir, "task-degraded")
    assert rows[-1]["delivery_status"] == "degraded"
    assert "file upload failed" in rows[-1]["message"]
    assert not (settings.paths.project_dir / "delivery_retry.jsonl").exists()


@pytest.mark.asyncio
async def test_task_notification_records_presentation_run_event(settings):
    from types import SimpleNamespace

    from omni.channels.outbound import DeliveryPartResult, DeliveryReport

    class _Runs:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def append_event(self, task_id: str, **kwargs):  # noqa: ANN001
            self.events.append({"task_id": task_id, **kwargs})

    class _Runtime:
        async def get_subtask(self, subtask_id: str):  # noqa: ANN001
            return SimpleNamespace(id=subtask_id, task_id="run-delivery")

    class _Agent:
        def __init__(self) -> None:
            self.runtime = _Runtime()
            self.tasks = _Runs()

    class _DegradedChannel(Channel):
        name = "dummy"

        async def start(self) -> None:
            return None

        async def send_turn(self, external_key, presentation):  # noqa: ANN001
            return DeliveryReport(
                target=external_key,
                parts=[DeliveryPartResult(kind="file", status="degraded", message="file fallback")],
            )

    agent = _Agent()
    channel = _DegradedChannel(settings, agent)  # type: ignore[arg-type]
    note = TaskNotification(
        subtask_id="task-delivery",
        skill_name="scientific-figure",
        status="succeeded",
        channel="dummy",
        external_key="chat-1",
        session_id="sess-1",
    )

    await channel.send_task_notification(note)

    assert agent.tasks.events[-1]["task_id"] == "run-delivery"
    assert agent.tasks.events[-1]["event_type"] == "presentation.degraded"
    assert agent.tasks.events[-1]["subtask_id"] == "task-delivery"


@pytest.mark.asyncio
async def test_task_notification_records_failed_delivery_and_retry(settings):
    from omni.runtime.notifications import read_delivery_statuses

    class _FailingChannel(Channel):
        name = "dummy"

        async def start(self) -> None:
            return None

        async def send_turn(self, external_key, presentation):  # noqa: ANN001
            raise RuntimeError("send API unavailable")

    channel = _FailingChannel(settings, _DummyAgent())
    note = TaskNotification(
        subtask_id="task-failed",
        skill_name="scientific-figure",
        status="succeeded",
        channel="dummy",
        external_key="chat-1",
        session_id="sess-1",
    )

    with pytest.raises(RuntimeError):
        await channel.send_task_notification(note)

    rows = read_delivery_statuses(settings.paths.project_dir, "task-failed")
    assert rows[-1]["delivery_status"] == "failed"
    retry_text = (settings.paths.project_dir / "delivery_retry.jsonl").read_text(encoding="utf-8")
    assert "task-failed" in retry_text
    assert "send API unavailable" in retry_text


@pytest.mark.asyncio
async def test_file_uploads_use_title_derived_name_for_legacy_hash_files(tmp_path):
    """Legacy content-addressed files must not reach IM users as ``<uuid>.md``;
    the upload name comes from the artifact title (OpenClaw's leak fix)."""
    from omni.channels.outbound import DingTalkClient, send_presentation
    from omni.runtime.presentation import task_presentation_from_result

    legacy = tmp_path / "63fb795dc5504e2596fbf0e847a2d0d8.md"
    legacy.write_text("# 草稿", encoding="utf-8")
    presentation = task_presentation_from_result(
        subtask_id="aab13696abcd",
        skill="workflow",
        status="succeeded",
        result={
            "summary": "done",
            "artifacts": [
                {"title": "RAG 综述草稿", "format": "md", "path": str(legacy), "mime": "text/markdown"}
            ],
        },
    )
    client = DingTalkClient({"gateway_url": "http://gateway.local"})
    sent: list[dict] = []

    async def fake_post(payload):  # noqa: ANN001
        sent.append(payload)

    client._post_gateway = fake_post  # type: ignore[method-assign]

    await send_presentation(client, "user-1", presentation)

    file_payloads = [p for p in sent if p.get("type") == "file"]
    assert file_payloads
    assert file_payloads[0]["file_name"] == "RAG-综述草稿.md"
    assert file_payloads[0]["path"] == str(legacy)


def test_display_filename_keeps_semantic_on_disk_names():
    from omni.channels.outbound import DeliveryPart, _display_filename

    semantic = DeliveryPart(
        kind="file", title="RAG 系统架构图 SVG", path="/x/artifacts/figure/RAG-系统架构图-10ddda65.svg"
    )
    assert _display_filename(semantic) == "RAG-系统架构图-10ddda65.svg"

    legacy = DeliveryPart(
        kind="file", title="RAG 系统架构图 SVG", path="/x/artifacts/figure/10ddda6548f84f61b85256c99eb9dd8e.svg"
    )
    # Title-derived, with the duplicated format token folded into the extension.
    assert _display_filename(legacy) == "RAG-系统架构图.svg"

    untitled = DeliveryPart(kind="file", title="", path="/x/artifacts/figure/10ddda6548f84f61b85256c99eb9dd8e.svg")
    assert _display_filename(untitled) == "artifact.svg"


@pytest.mark.asyncio
async def test_dingtalk_gateway_client_sends_file_and_image_payloads():
    from omni.channels.outbound import DingTalkClient

    client = DingTalkClient({"gateway_url": "http://gateway.local"})
    sent: list[dict] = []

    async def fake_post(payload):  # noqa: ANN001
        sent.append(payload)

    client._post_gateway = fake_post  # type: ignore[method-assign]

    await client.send_file("conversation-1", "/tmp/model.dot")
    await client.send_image("conversation-1", "/tmp/model.png")

    assert sent[0]["type"] == "file"
    assert sent[0]["file_name"] == "model.dot"
    assert sent[1]["type"] == "image"
    assert sent[1]["file_name"] == "model.png"


@pytest.mark.asyncio
async def test_dingtalk_without_a_gateway_says_it_cannot_attach_a_file(tmp_path):  # noqa: ANN001
    """A robot webhook has no media call, and pretending otherwise was worse.

    Without a gateway the client used to post the artifact's absolute path as
    markdown: the recipient got a directory on the host's disk, and because the
    post succeeded the delivery was recorded as sent. Failing here lets the shared
    fallback name the file and mark the delivery degraded, which is what happened.
    """
    from omni.channels.outbound import (
        DeliveryPart,
        DingTalkClient,
        OutboundError,
        _send_delivery_part,
    )

    path = tmp_path / "Scientific-Figure-e5ce4d69-d921117e.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
    client = DingTalkClient({"webhook_url": "https://oapi.dingtalk.com/robot/send"})
    said: list[str] = []

    async def capture(target: str, text: str) -> None:
        said.append(text)

    client.send_text = capture  # type: ignore[method-assign]

    with pytest.raises(OutboundError, match="base_url"):
        await client.send_file("conversation-1", str(path))

    result = await _send_delivery_part(
        client,
        "conversation-1",
        DeliveryPart(kind="image", title="Scientific Figure", path=str(path)),
        allowed_roots=[tmp_path],
    )

    assert result.status == "degraded"
    assert path.name in said[0]
    assert str(tmp_path) not in said[0]


@pytest.mark.asyncio
async def test_dingtalk_gateway_client_sends_text_and_rich_payloads():
    from omni.channels.outbound import DingTalkClient

    client = DingTalkClient({"gateway_url": "http://gateway.local"})
    sent: list[dict] = []

    async def fake_post(payload):  # noqa: ANN001
        sent.append(payload)

    client._post_gateway = fake_post  # type: ignore[method-assign]

    await client.send_markdown("conversation-1", "## 任务完成\n\n继续查看详情")
    await client.send_markdown("conversation-1", "```bash\nomni task show ded7af96\n```")

    assert sent[0]["type"] == "markdown"
    assert sent[1]["type"] == "text"
    assert "```" not in sent[1]["text"]


class _DummyAgent:
    def __init__(self) -> None:
        self.kwargs = {}
        self.turns: list[dict] = []

    async def ensure_session(self, **kwargs):
        self.kwargs["ensure_session"] = kwargs
        return "sess-channel"

    async def handle_turn(self, text, **kwargs):
        self.kwargs["handle_turn"] = {"text": text, **kwargs}
        self.turns.append({"text": text, **kwargs})
        return TurnResult(
            text="通道回答",
            session_id=kwargs["session_id"],
            submitted_subtask_ids=["task-chan"],
        )


class _DelayedSubmitAgent(_DummyAgent):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def handle_turn(self, text, **kwargs):  # noqa: ANN001
        self.started.set()
        await self.finish.wait()
        return TurnResult(
            text="✅ 已提交 Transformer 架构图",
            session_id=kwargs["session_id"],
            submitted_subtask_ids=["task-abc123"],
        )


class _ExplodingAfterAckAgent(_DummyAgent):
    def __init__(self) -> None:
        super().__init__()
        self.tasks = _FailureAuditRuns()

    async def handle_turn(self, text, **kwargs):  # noqa: ANN001
        callback = kwargs.get("on_task_ack")
        assert callback is not None
        await callback({"task_id": "run-terminal-123456"})
        raise RuntimeError("workflow provider input contract failed")


class _FailureAuditRuns:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def claim_delivery(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return True

    async def finish_delivery(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None

    async def append_event(self, task_id, **kwargs):  # noqa: ANN001
        self.events.append({"task_id": task_id, **kwargs})

    async def get_task(self, task_id):  # noqa: ANN001, ARG002
        return None


class _RecordingChannel(Channel):
    name = "dummy"

    def __init__(self, settings, agent) -> None:  # noqa: ANN001
        super().__init__(settings, agent)
        self.sent: list[str] = []

    async def start(self) -> None:
        return None

    async def send_turn(self, external_key, presentation):  # noqa: ANN001
        self.sent.append(presentation.to_plain_text())


class _CommandRuntime:
    def __init__(self, workflows) -> None:  # noqa: ANN001
        from omni.storage.models import WorkflowStepORM

        self.workflows = list(workflows)
        self.steps = {
            workflow.id: [
                WorkflowStepORM(
                    id=f"step-{workflow.id[:8]}-{position}",
                    workflow_run_id=workflow.id,
                    task_id=workflow.task_id,
                    step_key=str(item.get("id") or f"step_{position + 1}"),
                    position=position,
                    skill_name=(
                        str(item.get("skill_name") or "")
                        if item.get("skill_name") != "synthesis.final"
                        else ""
                    ),
                    capability=str(item.get("skill_name") or ""),
                    provider_type=(
                        "native_executor"
                        if item.get("skill_name") == "synthesis.final"
                        else "skill"
                    ),
                    deliverable=(
                        "draft.section"
                        if item.get("skill_name") == "synthesis.final"
                        else ""
                    ),
                    status=str(item.get("status") or "succeeded"),
                    result_json=item.get("result") or {},
                )
                for position, item in enumerate((workflow.result_json or {}).get("steps") or [])
            ]
            for workflow in self.workflows
        }

    async def get_subtask(self, subtask_id: str):
        return None

    async def list_subtasks(self, *, limit: int = 30, status: str | None = None, include_archived: bool = False):
        _ = include_archived
        _ = (limit, status)
        return []

    async def get_workflow_run(self, workflow_id: str):
        matches = [
            workflow
            for workflow in self.workflows
            if workflow.id == workflow_id or workflow.id.startswith(workflow_id)
        ]
        return matches[0] if len(matches) == 1 else None

    async def list_workflow_runs(self, *, task_id: str = "", limit: int = 100):
        rows = [workflow for workflow in self.workflows if not task_id or workflow.task_id == task_id]
        return rows[:limit]

    async def list_workflow_steps(self, workflow_id: str):
        return list(self.steps.get(workflow_id, []))


class _CommandRuns:
    def __init__(self, runs) -> None:  # noqa: ANN001
        self.tasks = list(runs)

    async def get_task(self, task_id: str):
        matches = [run for run in self.tasks if run.id == task_id or run.id.startswith(task_id)]
        return matches[0] if len(matches) == 1 else None

    async def list_tasks(self, *, limit: int = 30, status: str | None = None, kind: str | None = None):
        rows = self.tasks
        if status:
            rows = [run for run in rows if run.status == status]
        return rows[:limit]

    async def list_events(self, task_id: str):  # noqa: ARG002
        return []

    async def list_child_tasks(self, task_id: str):  # noqa: ARG002
        return []


class _CommandAgent(_DummyAgent):
    def __init__(self, tasks, paths, db=None) -> None:  # noqa: ANN001
        super().__init__()
        self.runtime = _CommandRuntime(tasks)
        self.tasks = _CommandRuns([_workflow_run(task) for task in tasks])
        self.paths = paths
        self.db = db
        self.persisted: list[dict] = []

    async def handle_turn(self, text, **kwargs):  # noqa: ANN001
        raise AssertionError(f"channel command should not call agent.handle_turn: {text}")

    async def _persist_message(self, session_id, role, content, **kwargs):  # noqa: ANN001
        self.persisted.append({"session_id": session_id, "role": role, "content": content, **kwargs})


def _workflow_task():
    from omni.storage.models import WorkflowRunORM

    return WorkflowRunORM(
        id="c98e4330aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        session_id="sess-channel",
        task_id="runchannel0000000000000000000000000001",
        project="test",
        status="succeeded",
        goal="Transformer 架构 NeurIPS 论文章节",
        result_json={
            "summary": "工作流完成：已生成检索词、文献综述、评审意见、图和章节草稿。",
            "steps": [
                {
                    "id": "search",
                    "skill_name": "literature-search",
                    "status": "succeeded",
                    "result": {"summary": "构建并执行 Transformer 架构相关检索词。"},
                },
                {
                    "id": "writing",
                    "skill_name": "synthesis.final",
                    "status": "succeeded",
                    "result": {"summary": "完成 NeurIPS 风格章节草稿。"},
                },
            ],
        },
        error="",
        trace_log=[{"stage": "done", "pct": 100}],
        created_at=datetime(2026, 6, 26, 10, 0, tzinfo=UTC),
        started_at=datetime(2026, 6, 26, 10, 1, tzinfo=UTC),
        finished_at=datetime(2026, 6, 26, 10, 2, tzinfo=UTC),
    )


def _workflow_run(task):
    from omni.storage.models import TaskORM

    return TaskORM(
        id=task.task_id,
        session_id=task.session_id,
        project=task.project,
        channel="feishu",
        status="succeeded",
        title="Transformer 架构 NeurIPS 论文章节",
        user_input="Transformer 架构 NeurIPS 论文章节",
        summary="工作流完成。",
        current_workflow_id=task.id,
        submitted_workflow_ids=[task.id],
        created_at=datetime(2026, 6, 26, 9, 59, tzinfo=UTC),
        started_at=datetime(2026, 6, 26, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 6, 26, 10, 2, tzinfo=UTC),
    )


def test_channel_execution_presentation_actions_use_canonical_task_id():
    from omni.channels.commands import _presentation_for_task
    from omni.storage.models import SubtaskORM

    execution = SubtaskORM(
        id="f4902f1686924dd9a74efa920bbc6626",
        task_id="05571218b61b4f1aab86fd83a660c75e",
        skill_name="research-ideation",
        status="succeeded",
        result_json={"summary": "done"},
    )

    presentation = _presentation_for_task(execution)

    actions = "\n".join(presentation.next_actions)
    assert "/task show 05571218" in actions
    assert "/task attach 05571218" in actions
    assert "/task attach f4902f16" not in actions
    assert presentation.object_kind == "skill_execution"
    assert presentation.object_id == execution.id
    assert presentation.task_id == execution.task_id


class _DummyChannel(Channel):
    name = "dummy"

    async def start(self) -> None:
        return None


@pytest.mark.asyncio
async def test_channel_handle_inbound_returns_turn_presentation(settings):
    agent = _DummyAgent()
    channel = _DummyChannel(settings, agent)  # type: ignore[arg-type]

    presentation = await channel.handle_inbound("画 Transformer 图", "chat-1")

    assert presentation.assistant_text == "通道回答"
    assert presentation.submitted_subtask_ids == ["task-chan"]
    assert agent.kwargs["ensure_session"]["channel"] == "dummy"
    assert agent.kwargs["ensure_session"]["external_key"] == "chat-1"
    assert agent.kwargs["handle_turn"]["channel"] == "dummy"
    assert agent.kwargs["handle_turn"]["drain_tasks"] is False


class _ChatChannel(_DummyChannel):
    """Named as one of the IM channels, which is what shapes a reply."""

    name = "wechat"


class _RecallingAgent(_DummyAgent):
    """Answers a follow-up from context, producing nothing of its own.

    This is task e5ce4d69: everything had been generated on the previous turn, so
    the model re-reported it and quoted where the figure lives. A turn like this
    carries no artifacts, which is why its reply used to arrive with no
    attachment and a server path in the body.
    """

    def __init__(self, mentioned: str) -> None:
        super().__init__()
        self._mentioned = mentioned

    async def handle_turn(self, text, **kwargs):  # noqa: ANN001
        return TurnResult(
            text=f"三项材料已在上一轮生成完毕。架构图路径：{self._mentioned}",
            session_id=kwargs["session_id"],
        )


@pytest.mark.asyncio
async def test_a_chat_channel_attaches_the_deliverable_its_reply_quotes(settings):  # noqa: ANN001
    """End to end: the channel looks where it is allowed to upload from."""
    figure = settings.paths.artifacts_dir / "figure" / "Scientific-Figure-e5ce4d69-d921.png"
    figure.parent.mkdir(parents=True, exist_ok=True)
    figure.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 300)
    settings.paths.channels_dir.mkdir(parents=True, exist_ok=True)
    (settings.paths.channels_dir / "wechat.toml").write_text(
        "allowlist_enabled = false\n", encoding="utf-8"
    )
    channel = _ChatChannel(settings, _RecallingAgent(str(figure)))  # type: ignore[arg-type]

    presentation = await channel.handle_inbound("那些材料呢？", "chat-1")

    assert [Path(art.path).name for art in presentation.artifacts] == [figure.name]
    chat = presentation.to_markdown(include_local_paths=False)
    assert str(figure) not in chat.partition("**Outputs**")[0]


@dataclass
class _StoredArtifact:
    """One row of the artifact store, as the channel would read it."""

    uri: str
    title: str
    kind: str
    rel_path: str
    path: Path
    mime: str = ""
    size_bytes: int = 0


class _ThreadArtifacts:
    """A store holding two tasks' deliverables under one conversation."""

    def __init__(self, rows: list[_StoredArtifact]) -> None:
        self.rows = rows
        self.sessions_asked: list[str] = []

    async def list_by_session(self, session_id: str) -> list[_StoredArtifact]:
        self.sessions_asked.append(session_id)
        return list(self.rows)

    async def resolve_path(self, uri: str) -> Path | None:
        for row in self.rows:
            if row.uri == uri:
                return row.path
        return None


class _FigureProducingAgent(_DummyAgent):
    """Generates one figure, the way task ed444423 did."""

    def __init__(self, figure: Path, artifacts: _ThreadArtifacts) -> None:
        super().__init__()
        self._figure = figure
        self.artifacts = artifacts

    async def handle_turn(self, text, **kwargs):  # noqa: ANN001
        from omni.runtime.presentation import ArtifactRef

        return TurnResult(
            text="架构图已生成。",
            session_id=kwargs["session_id"],
            artifacts=[
                ArtifactRef(title="Scientific Figure PNG", format="png", path=str(self._figure))
            ],
        )


@pytest.mark.asyncio
async def test_a_chat_reply_does_not_carry_another_tasks_deliverables(settings):  # noqa: ANN001
    """Incident ed444423, end to end.

    The reply completed its inventory from the conversation, so a turn that
    generated one figure went out carrying a research report from an unrelated
    question hours earlier — and uploaded it. Both live in this store under the
    one session, as they did on the day; the reader is owed what ``/task show``
    lists for this task, which is the figure.
    """
    figure = settings.paths.artifacts_dir / "figure" / "Scientific-Figure-ed444423-8021.png"
    figure.parent.mkdir(parents=True, exist_ok=True)
    figure.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 300)
    stranger = settings.paths.artifacts_dir / "report" / "Research-ideation-809ebddb-8124.md"
    stranger.parent.mkdir(parents=True, exist_ok=True)
    stranger.write_text("# 隐空间干预\n", encoding="utf-8")
    settings.paths.channels_dir.mkdir(parents=True, exist_ok=True)
    (settings.paths.channels_dir / "wechat.toml").write_text(
        "allowlist_enabled = false\n", encoding="utf-8"
    )
    store = _ThreadArtifacts([
        _StoredArtifact(
            uri="artifact://figure/ed444423",
            title="Scientific Figure PNG",
            kind="figure",
            rel_path=str(figure.relative_to(settings.paths.artifacts_dir)),
            path=figure,
        ),
        _StoredArtifact(
            uri="artifact://report/809ebddb",
            title="Research ideation report",
            kind="report",
            rel_path=str(stranger.relative_to(settings.paths.artifacts_dir)),
            path=stranger,
        ),
    ])
    channel = _ChatChannel(settings, _FigureProducingAgent(figure, store))  # type: ignore[arg-type]

    presentation = await channel.handle_inbound("现在就执行吧", "chat-1")

    assert [Path(art.path).name for art in presentation.artifacts] == [figure.name]


@pytest.mark.asyncio
async def test_channel_plan_command_creates_approval_mode_turn(settings):
    agent = _DummyAgent()
    channel = _DummyChannel(settings, agent)  # type: ignore[arg-type]

    await channel.handle_inbound("/plan 分析这份实验数据", "chat-1")

    assert agent.kwargs["handle_turn"]["text"] == "分析这份实验数据"
    assert agent.kwargs["handle_turn"]["interaction_mode"] == "plan"


@pytest.mark.asyncio
async def test_channel_orders_task_completion_after_inbound_submission_ack(settings):
    agent = _DelayedSubmitAgent()
    channel = _RecordingChannel(settings, agent)  # type: ignore[arg-type]
    inbound = asyncio.create_task(channel.handle_inbound_and_send("帮我重新产出 Transformer 架构图", "chat-1"))

    await agent.started.wait()
    completion = asyncio.create_task(channel.send_task_notification(TaskNotification(
        subtask_id="task-abc123",
        skill_name="scientific-figure",
        status="succeeded",
        channel="dummy",
        external_key="chat-1",
        summary="Transformer 架构图完成。",
        payload=_task_payload(),
    )))
    await asyncio.sleep(0)

    assert channel.sent == []

    agent.finish.set()
    await asyncio.gather(inbound, completion)

    assert len(channel.sent) == 2
    assert "已提交 Transformer 架构图" in channel.sent[0]
    assert "Transformer 架构图完成" in channel.sent[1]


@pytest.mark.asyncio
async def test_channel_sends_one_terminal_failure_after_ack_when_agent_raises(settings):
    agent = _ExplodingAfterAckAgent()
    channel = _RecordingChannel(settings, agent)  # type: ignore[arg-type]

    presentation = await channel.handle_inbound_and_send("研究问题", "chat-1")

    assert presentation.task_id == "run-terminal-123456"
    assert len(channel.sent) == 2
    assert "Request received" in channel.sent[0]
    assert "could not complete" in channel.sent[1].lower()
    assert "workflow provider input contract failed" not in channel.sent[1]
    failure = next(event for event in agent.tasks.events if event["event_type"] == "assistant.message")
    assert failure["status"] == "failed"
    assert "workflow provider input contract failed" in failure["error"]


@pytest.mark.asyncio
async def test_channel_tasks_show_command_returns_task_without_agent_turn(settings):
    task = _workflow_task()
    agent = _CommandAgent([task], settings.paths)
    channel = _DummyChannel(settings, agent)  # type: ignore[arg-type]

    workflow_view = await channel.handle_inbound("/task show c98e4330", "chat-1")
    task_view = await channel.handle_inbound(
        f"/task show {task.task_id[:8]}", "chat-1"
    )

    assert f"Workflow `{task.id[:8]}`" in workflow_view.assistant_text
    assert "Object kind: `workflow_run`" in workflow_view.assistant_text
    assert f"Object ID: `{task.id}`" in workflow_view.assistant_text
    assert f"Task ID: `{task.task_id}`" in workflow_view.assistant_text
    assert (
        f"Full task: `/task show {task.task_id[:8]}`"
        in workflow_view.assistant_text
    )
    assert "literature-search" in workflow_view.assistant_text
    assert "synthesis.final" in workflow_view.assistant_text
    assert "Skill executions" not in workflow_view.assistant_text
    assert f"Task `{task.task_id[:8]}`" in task_view.assistant_text
    assert "Object kind: `task`" in task_view.assistant_text
    assert f"Object ID: `{task.task_id}`" in task_view.assistant_text
    assert f"Task ID: `{task.task_id}`" in task_view.assistant_text
    assert task_view.assistant_text != workflow_view.assistant_text
    assert "handle_turn" not in agent.kwargs


@pytest.mark.asyncio
async def test_channel_inbox_shows_canonical_task_and_concrete_object(settings):
    task = _workflow_task()
    agent = _CommandAgent([task], settings.paths)
    channel = _DummyChannel(settings, agent)  # type: ignore[arg-type]
    inbox = settings.paths.project_dir / "inbox.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        json.dumps(
            {
                "task_id": task.task_id,
                "object_kind": "workflow_run",
                "object_id": task.id,
                "session_id": "sess-channel",
                "skill_name": "workflow",
                "status": "succeeded",
                "summary": "done",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    presentation = await channel.handle_inbound("/inbox", "chat-1")

    assert "| time | task | object |" in presentation.assistant_text
    assert task.task_id[:8] in presentation.assistant_text
    assert f"workflow_run:{task.id[:8]}" in presentation.assistant_text


@pytest.mark.asyncio
async def test_channel_show_and_attach_fail_closed_on_typed_id_ambiguity(
    settings,
    monkeypatch,
):
    from omni.channels import commands
    from omni.runtime.task_object_resolver import TaskObjectResolution

    task = _workflow_task()
    agent = _CommandAgent([task], settings.paths)
    agent.settings = settings
    channel = _DummyChannel(settings, agent)  # type: ignore[arg-type]

    async def ambiguous(_settings, _object_id):  # noqa: ANN001
        return TaskObjectResolution(status="ambiguous")

    monkeypatch.setattr(commands, "resolve_task_object", ambiguous)

    shown = await channel.handle_inbound("/task show c98e4330", "chat-1")
    attached = await channel.handle_inbound("/task attach c98e4330", "chat-1")

    assert "ambiguous" in shown.assistant_text
    assert "ambiguous" in attached.assistant_text
    assert not agent.persisted


@pytest.mark.asyncio
async def test_channel_tasks_watch_returns_snapshot_without_blocking(settings):
    task = _workflow_task()
    agent = _CommandAgent([task], settings.paths)
    channel = _DummyChannel(settings, agent)  # type: ignore[arg-type]

    presentation = await channel.handle_inbound("/task watch", "chat-1")

    assert "Current task snapshot" in presentation.assistant_text
    assert "runchann" in presentation.assistant_text
    assert "pushed automatically" in presentation.assistant_text
    assert "handle_turn" not in agent.kwargs


@pytest.mark.asyncio
async def test_channel_natural_language_task_lookup_goes_to_semantic_agent(settings):
    agent = _DummyAgent()
    channel = _DummyChannel(settings, agent)  # type: ignore[arg-type]

    message = "Inspect the execution trace for task c98e4330."
    presentation = await channel.handle_inbound(message, "chat-1")

    assert presentation.assistant_text == "通道回答"
    assert agent.kwargs["handle_turn"]["text"] == message


@pytest.mark.asyncio
async def test_channel_natural_language_does_not_treat_plain_words_as_task_ids(settings):
    agent = _DummyAgent()
    channel = _DummyChannel(settings, agent)  # type: ignore[arg-type]

    presentation = await channel.handle_inbound("workflow 该怎么跑", "chat-1")

    assert presentation.assistant_text == "通道回答"
    assert agent.kwargs["handle_turn"]["text"] == "workflow 该怎么跑"


@pytest.mark.asyncio
async def test_channel_verify_session_command_reads_research_store(settings):
    from omni.storage.db import get_database

    db = get_database(settings.paths.project_db)
    await db.init()
    agent = _CommandAgent([], settings.paths, db=db)
    channel = _DummyChannel(settings, agent)  # type: ignore[arg-type]

    presentation = await channel.handle_inbound("/verify --session", "chat-1")

    assert "Evidence audit" in presentation.assistant_text
    assert "sess-cha" in presentation.assistant_text
    assert "No verifiable claims" in presentation.assistant_text
    assert "handle_turn" not in agent.kwargs


def _product_im_channels():
    from omni.channels.dingtalk import DingTalkChannel
    from omni.channels.feishu import FeishuChannel
    from omni.channels.wechat import WeChatChannel

    return (WeChatChannel, FeishuChannel, DingTalkChannel)


def _wechat_text_event(key: str, text: str, *, message_id: str = "") -> dict:
    event = {
        "from_user_id": key,
        "message_type": 1,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if message_id:
        event["message_id"] = message_id
    return event


async def _deliver_im_text(channel, text: str, key: str):
    name = channel.name
    if name == "wechat":
        return await channel.handle_ilink_message(_wechat_text_event(key, text))
    if name == "feishu":
        return await channel.handle_feishu_message({"chat_id": key, "text": text})
    if name == "dingtalk":
        return await channel.handle_dingtalk_message({"text": text, "conversationId": key})
    raise AssertionError(f"unexpected IM channel {name}")


def test_product_im_adapters_are_the_canonical_im_set() -> None:
    from omni.channels.security import IM_CHANNELS, canonical_im_channel, is_im_channel

    names = {cls.name for cls in _product_im_channels()}
    assert names == IM_CHANNELS
    for name in names:
        assert is_im_channel(name)
        assert canonical_im_channel(name) == name
    assert canonical_im_channel("weixin") == "wechat"
    assert canonical_im_channel("lark") == "feishu"
    assert canonical_im_channel("dingding") == "dingtalk"
    assert not is_im_channel("cli")


def test_empty_notify_is_not_refilled_from_an_im_parent() -> None:
    from omni.channels.security import completion_notify_channel

    assert completion_notify_channel("", "wechat") == ""
    assert completion_notify_channel("", "weixin") == ""
    assert completion_notify_channel("feishu", "wechat") == "feishu"
    assert completion_notify_channel("", "cli") == ""
    assert completion_notify_channel("", "") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", _product_im_channels(), ids=lambda cls: cls.name)
async def test_im_channel_requires_pairing_before_agent(settings, cls):
    agent = _DummyAgent()
    fake = _FakeOutbound()
    channel = cls(settings, agent, client=fake)  # type: ignore[arg-type]
    key = f"chat-{channel.name}"

    presentation = await _deliver_im_text(channel, "你好", key)

    assert presentation is not None
    assert "not paired" in presentation.assistant_text
    assert "handle_turn" not in agent.kwargs
    assert fake.sent and "not paired" in fake.sent[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", _product_im_channels(), ids=lambda cls: cls.name)
async def test_im_channel_pairing_allows_followup(settings, cls):
    from omni.channels.security import create_pairing_code

    name = cls.name
    cfg = settings.paths.channels_dir / f"{name}.toml"
    code = create_pairing_code(cfg)
    agent = _DummyAgent()
    fake = _FakeOutbound()
    channel = cls(settings, agent, client=fake)  # type: ignore[arg-type]
    key = f"chat-{name}"

    paired = await _deliver_im_text(channel, f"/pair {code}", key)
    assert paired is not None
    assert "Pairing complete" in paired.assistant_text
    assert "handle_turn" not in agent.kwargs

    presentation = await _deliver_im_text(channel, "画图", key)
    assert presentation is not None
    assert presentation.assistant_text == "通道回答"
    assert agent.kwargs["handle_turn"]["text"] == "画图"
    assert agent.kwargs["handle_turn"]["channel"] == name
    assert agent.kwargs["handle_turn"]["drain_tasks"] is False
    assert agent.kwargs["ensure_session"]["channel"] == name


@pytest.mark.asyncio
async def test_feishu_inbound_dedupes_duplicate_message_id(settings):
    from omni.channels.feishu import FeishuChannel
    from omni.channels.security import create_pairing_code

    cfg = settings.paths.channels_dir / "feishu.toml"
    code = create_pairing_code(cfg)
    agent = _DummyAgent()
    fake = _FakeOutbound()
    channel = FeishuChannel(settings, agent, client=fake)  # type: ignore[arg-type]

    await channel.handle_feishu_message({"chat_id": "chat-feishu", "text": f"/pair {code}", "message_id": "pair-1"})
    fake.sent.clear()

    event = {
        "chat_id": "chat-feishu",
        "text": "Prepare a submission section with search, fetch, index, grounded QA, review, figure, writing.",
        "message_id": "msg-duplicate-1",
    }
    first = await channel.handle_feishu_message(event)
    duplicate = await channel.handle_feishu_message(dict(event))

    assert first is not None
    assert duplicate is None
    assert len(agent.turns) == 1
    assert len(fake.sent) == 1


@pytest.mark.asyncio
async def test_wechat_answers_the_same_question_asked_twice(settings):
    """Without a provider id, a repeat is a request rather than a retransmission.

    This used to hash the message text and drop a match within five minutes,
    which cannot tell a person asking again from the network delivering once
    twice — and the person got silence. Deduplication needs an identity the
    sender assigned; the text is not one.
    """
    from omni.channels.security import add_allowed_external_key
    from omni.channels.wechat import WeChatChannel

    cfg = settings.paths.channels_dir / "wechat.toml"
    add_allowed_external_key(cfg, "wx-user-1")
    agent = _DummyAgent()
    fake = _FakeOutbound()
    channel = WeChatChannel(settings, agent, client=fake)  # type: ignore[arg-type]
    event = _wechat_text_event("wx-user-1", "重复触发检查")

    first = await channel.handle_ilink_message(event)
    again = await channel.handle_ilink_message(dict(event))

    assert first is not None
    assert again is not None
    assert len(agent.turns) == 2
    assert len(fake.sent) == 2


@pytest.mark.asyncio
async def test_wechat_still_ignores_the_same_event_delivered_twice(settings):
    """The retry the guard exists for: one message, two deliveries, one id."""
    from omni.channels.security import add_allowed_external_key
    from omni.channels.wechat import WeChatChannel

    cfg = settings.paths.channels_dir / "wechat.toml"
    add_allowed_external_key(cfg, "wx-user-1")
    agent = _DummyAgent()
    fake = _FakeOutbound()
    channel = WeChatChannel(settings, agent, client=fake)  # type: ignore[arg-type]
    event = _wechat_text_event("wx-user-1", "重复触发检查", message_id="wx-msg-1")

    first = await channel.handle_ilink_message(event)
    redelivered = await channel.handle_ilink_message(dict(event))

    assert first is not None
    assert redelivered is None
    assert len(agent.turns) == 1
    assert len(fake.sent) == 1


@pytest.mark.asyncio
async def test_feishu_ws_isolates_sdk_module_loop(monkeypatch, settings):
    """lark-oapi's WS client has a module-level loop; Omni must isolate it."""
    from omni.channels.feishu import FeishuChannel
    from omni.channels.security import create_pairing_code

    cfg = settings.paths.channels_dir / "feishu.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('mode = "ws"\napp_id = "app"\napp_secret = "secret"\n', encoding="utf-8")
    code = create_pairing_code(cfg)
    started = threading.Event()
    stop = threading.Event()

    import sys
    import types

    fake_ws = types.ModuleType("lark_oapi.ws.client")
    fake_ws.loop = asyncio.get_running_loop()

    class FakeLarkChannel:
        instance = None

        def __init__(self, *, app_id: str, app_secret: str) -> None:
            self.app_id = app_id
            self.app_secret = app_secret
            self.handlers = {}
            self._ready_flag = False
            self._ws_client = type("FakeWsClient", (), {"_conn": None})()
            FakeLarkChannel.instance = self

        def on(self, name, handler):  # noqa: ANN001
            self.handlers[name] = handler

        def start(self):  # noqa: ANN201
            if fake_ws.loop.is_running():
                raise RuntimeError("SDK module loop leaked from Omni's running loop")
            fake_ws.loop.run_until_complete(asyncio.sleep(0))
            self._ws_client._conn = object()
            self._ready_flag = True
            started.set()
            stop.wait(timeout=5)

        def stop(self) -> None:
            stop.set()

        async def emit_message(self, text: str = "/pair 123456") -> None:
            inbound = type(
                "Inbound",
                (),
                {"chat_id": "chat-feishu", "content_text": text, "raw": {}},
            )()
            result = self.handlers["message"](inbound)
            if hasattr(result, "__await__"):
                await result

    fake_pkg = types.ModuleType("lark_oapi")
    fake_channel = types.ModuleType("lark_oapi.channel")
    fake_ws_pkg = types.ModuleType("lark_oapi.ws")
    fake_channel.FeishuChannel = FakeLarkChannel
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_pkg)
    monkeypatch.setitem(sys.modules, "lark_oapi.channel", fake_channel)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws", fake_ws_pkg)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws.client", fake_ws)

    agent = _DummyAgent()
    fake_outbound = _FakeOutbound()
    channel = FeishuChannel(settings, agent, client=fake_outbound)  # type: ignore[arg-type]

    task = asyncio.create_task(channel.start())
    await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)

    assert FakeLarkChannel.instance is not None
    assert fake_ws.loop is not asyncio.get_running_loop()
    assert "message" in FakeLarkChannel.instance.handlers
    await FakeLarkChannel.instance.emit_message(f"/pair {code}")
    for _ in range(10):
        if fake_outbound.sent:
            break
        await asyncio.sleep(0)
    assert fake_outbound.sent
    assert fake_outbound.sent[0][0] == "chat-feishu"
    assert "Pairing complete" in fake_outbound.sent[0][1]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_drain_and_close_loop_cancels_pending_sdk_tasks():
    """Daemon stop/restart (e.g. via ``omni update``) must not leak lark tasks.

    Simulates lark-oapi's ``_receive_message_loop`` / ``_ping_loop`` / cron task
    left pending on the SDK loop; the teardown must cancel them and close the
    loop cleanly (no "Task was destroyed but it is pending!" / closed-loop noise).
    """
    from omni.channels.feishu import _drain_and_close_loop

    loop = asyncio.new_event_loop()
    holder: list[asyncio.Task] = []

    async def _seed() -> None:
        holder.append(asyncio.ensure_future(asyncio.sleep(1000)))
        holder.append(asyncio.ensure_future(asyncio.sleep(1000)))
        await asyncio.sleep(0)

    loop.run_until_complete(_seed())
    assert any(not t.done() for t in holder)  # pending before teardown

    _drain_and_close_loop(loop)

    assert loop.is_closed()
    assert all(t.done() for t in holder)
    assert all(t.cancelled() for t in holder)


def test_drain_and_close_loop_is_idempotent_on_closed_loop():
    from omni.channels.feishu import _drain_and_close_loop

    loop = asyncio.new_event_loop()
    loop.close()
    _drain_and_close_loop(loop)  # must not raise on an already-closed loop
    assert loop.is_closed()


class _FakeOutbound:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_markdown(self, target: str, markdown: str) -> None:
        self.sent.append((target, markdown))

    async def poll_messages(self):
        return []


class _FakeFileOutbound(_FakeOutbound):
    def __init__(self) -> None:
        super().__init__()
        self.markdown: list[tuple[str, str]] = self.sent
        self.files: list[tuple[str, str]] = []
        self.images: list[tuple[str, str]] = []

    async def send_file(self, target: str, path: str) -> None:
        self.files.append((target, path))

    async def send_image(self, target: str, path: str) -> None:
        self.images.append((target, path))


class _TypedOutbound(_FakeFileOutbound):
    def __init__(self) -> None:
        super().__init__()
        self.rich_texts: list[tuple[str, str]] = []
        self.texts: list[tuple[str, str]] = []

    async def send_rich_text(self, target: str, markdown: str) -> None:
        self.rich_texts.append((target, markdown))

    async def send_text(self, target: str, text: str) -> None:
        self.texts.append((target, text))


class _FailingSvgImageOutbound(_FakeFileOutbound):
    async def send_image(self, target: str, path: str) -> None:
        if path.endswith(".svg"):
            raise RuntimeError("svg is not a raster image")
        await super().send_image(target, path)


class _FailingMarkdownOutbound(_FakeFileOutbound):
    async def send_markdown(self, target: str, markdown: str) -> None:
        raise RuntimeError("markdown send failed")


class _ManagedFakeChannel:
    def __init__(self, name: str, *, fail: Exception | None = None) -> None:
        self.name = name
        self.fail = fail
        self.stopped = False

    async def start(self) -> None:
        if self.fail is not None:
            raise self.fail
        while True:
            await asyncio.sleep(3600)

    async def stop(self) -> None:
        self.stopped = True


def _write_enabled_channels(settings, names: list[str]) -> None:  # noqa: ANN001
    settings.paths.ensure_dirs()
    quoted = ", ".join(f'"{name}"' for name in names)
    settings.paths.config_file.write_text(f"[channels]\nenabled = [{quoted}]\n", encoding="utf-8")


def _write_wechat_ilink_config(settings) -> None:  # noqa: ANN001
    settings.paths.channels_dir.mkdir(parents=True, exist_ok=True)
    (settings.paths.channels_dir / "wechat.toml").write_text(
        'mode = "ilink"\naccount_id = "bot@im.bot"\nbase_url = "https://ilinkai.weixin.qq.com"\n',
        encoding="utf-8",
    )
    settings.paths.secrets_file.write_text(
        '[channels.wechat]\nbot_token = "tok-test"\n',
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_channel_manager_dynamically_reconciles_enabled_channels(monkeypatch, settings):
    import omni.channels.manager as manager_mod

    started: list[str] = []

    def fake_build_channels(names, _settings, _agent):  # noqa: ANN001
        started.extend(names)
        return [_ManagedFakeChannel(names[0])]

    monkeypatch.setattr(manager_mod, "build_channels", fake_build_channels)
    _write_enabled_channels(settings, ["cli"])
    _write_wechat_ilink_config(settings)
    manager = manager_mod.ChannelManager(settings, _DummyAgent())  # type: ignore[arg-type]

    await manager.reconcile_once()
    assert manager.snapshot()["cli"]["status"] == "running"

    _write_enabled_channels(settings, ["cli", "wechat"])
    await manager.reconcile_once()

    assert started == ["cli", "wechat"]
    assert manager.snapshot()["wechat"]["status"] == "running"
    await manager.stop()


@pytest.mark.asyncio
async def test_channel_manager_hot_reloads_only_changed_channel(monkeypatch, settings):
    """A re-login (config change) rebuilds *only* that channel; others stay up.

    This is the multi-channel safety guarantee: logging into wechat must not
    tear down a working feishu/cli adapter or interrupt its in-flight turns.
    """
    import omni.channels.manager as manager_mod

    built: dict[str, list[_ManagedFakeChannel]] = {}

    def fake_build_channels(names, _settings, _agent):  # noqa: ANN001
        ch = _ManagedFakeChannel(names[0])
        built.setdefault(names[0], []).append(ch)
        return [ch]

    monkeypatch.setattr(manager_mod, "build_channels", fake_build_channels)
    _write_enabled_channels(settings, ["cli", "wechat"])
    _write_wechat_ilink_config(settings)
    manager = manager_mod.ChannelManager(settings, _DummyAgent())  # type: ignore[arg-type]

    await manager.reconcile_once()
    assert len(built["cli"]) == 1
    assert len(built["wechat"]) == 1

    # Simulate `omni channel login wechat` rewriting wechat.toml (new account).
    (settings.paths.channels_dir / "wechat.toml").write_text(
        'mode = "ilink"\naccount_id = "bot-v2@im.bot"\nbase_url = "https://ilinkai.weixin.qq.com"\n',
        encoding="utf-8",
    )
    await manager.reconcile_once()

    # Only wechat rebuilt with the new config; cli left running untouched.
    assert len(built["wechat"]) == 2, "wechat must hot-reload after its config changed"
    assert built["wechat"][0].stopped is True, "old wechat adapter must be torn down"
    assert len(built["cli"]) == 1, "cli must NOT be rebuilt when only wechat changed"
    assert built["cli"][0].stopped is False, "cli adapter must keep running untouched"
    assert manager.snapshot()["wechat"]["status"] == "running"
    await manager.stop()


def test_request_reload_sentinel_triggers_single_wake(settings):
    """``request_reload`` makes the manager wake once, then consumes the signal."""
    import omni.channels.manager as manager_mod

    manager = manager_mod.ChannelManager(settings, _DummyAgent())  # type: ignore[arg-type]
    assert manager._reload_requested() is False  # no sentinel yet

    manager_mod.request_reload(settings.paths.channels_dir)
    assert manager._reload_requested() is True  # picked up the nudge
    assert manager._reload_requested() is False  # already consumed


@pytest.mark.asyncio
async def test_channel_manager_degrades_when_home_lock_held(monkeypatch, settings):
    """A second daemon must not bind an IM channel another daemon already owns."""
    import omni.channels.manager as manager_mod
    from omni.channels import locks

    # Simulate a foreign, *live* daemon already holding the wechat home-lock.
    settings.paths.channels_dir.mkdir(parents=True, exist_ok=True)
    (settings.paths.channels_dir / "wechat.lock").write_text(
        '{"pid": 999999, "ts": 0}', encoding="utf-8"
    )
    monkeypatch.setattr(locks, "pid_alive", lambda pid: pid == 999999)

    built: list[str] = []

    def fake_build_channels(names, _settings, _agent):  # noqa: ANN001
        built.extend(names)
        return [_ManagedFakeChannel(names[0])]

    monkeypatch.setattr(manager_mod, "build_channels", fake_build_channels)
    _write_wechat_ilink_config(settings)
    manager = manager_mod.ChannelManager(
        settings, _DummyAgent(), explicit_channels=["wechat"],  # type: ignore[arg-type]
    )

    await manager.reconcile_once()

    health = manager.snapshot()["wechat"]
    assert health["status"] == "degraded"
    assert "task-only" in health["reason"]
    assert built == []  # channel was never built/bound — stays task-only
    await manager.stop()


@pytest.mark.asyncio
async def test_channel_manager_reports_auth_expiry_as_degraded_not_running(
    monkeypatch, settings
) -> None:
    import omni.channels.manager as manager_mod

    class AuthExpired(RuntimeError):
        health_reason = "WeChat login expired; scan the QR code again."

    built: list[str] = []

    def fake_build_channels(names, _settings, _agent):  # noqa: ANN001
        built.extend(names)
        return [_ManagedFakeChannel(names[0], fail=AuthExpired("expired"))]

    monkeypatch.setattr(manager_mod, "build_channels", fake_build_channels)
    _write_enabled_channels(settings, ["wechat"])
    _write_wechat_ilink_config(settings)
    manager = manager_mod.ChannelManager(
        settings, _DummyAgent(), retry_interval=30.0  # type: ignore[arg-type]
    )

    await manager.reconcile_once()
    for _ in range(20):
        if manager.snapshot()["wechat"]["status"] == "degraded":
            break
        await asyncio.sleep(0.01)

    health = manager.snapshot()["wechat"]
    assert health["status"] == "degraded"
    assert health["reason"] == "WeChat login expired; scan the QR code again."
    assert built == ["wechat"]

    await manager.reconcile_once()
    assert built == ["wechat"]
    assert manager.snapshot()["wechat"]["status"] == "degraded"

    (settings.paths.channels_dir / "wechat.toml").write_text(
        'mode = "ilink"\naccount_id = "bot-v2@im.bot"\nbase_url = "https://ilinkai.weixin.qq.com"\n',
        encoding="utf-8",
    )
    await manager.reconcile_once()
    for _ in range(20):
        if len(built) >= 2:
            break
        await asyncio.sleep(0.01)
    assert built == ["wechat", "wechat"]
    await manager.stop()


def test_channel_login_start_reloads_running_service_without_restart(monkeypatch, settings):
    """``login --start`` with a live home service hot-reloads; it must NOT restart it."""
    from types import SimpleNamespace

    import omni.cli.commands.channel_cmd as channel_cmd
    from omni.runtime import service_control, service_state
    from omni.runtime.service_state import ServiceDesiredState

    monkeypatch.setattr(channel_cmd, "_warn_missing_runtime_dependency", lambda *a, **k: None)
    monkeypatch.setattr(channel_cmd, "_load_effective_channel_config", lambda *a, **k: {})

    # An enabled + live home service: a fresh login hot-reloads the channel and
    # only *reconciles* (ensure) — it must never re-enable/restart it.
    service_state.write_desired(settings.paths, ServiceDesiredState(enabled=True, configured=True))
    service_state.write_runtime(settings.paths, {"ready": True})

    calls = {"enable": 0, "ensure": 0}
    monkeypatch.setattr(
        service_control, "enable",
        lambda *a, **k: calls.__setitem__("enable", calls["enable"] + 1) or service_control.LifecycleResult(True, "enabled"),
    )
    monkeypatch.setattr(
        service_control, "ensure",
        lambda *a, **k: calls.__setitem__("ensure", calls["ensure"] + 1) or service_control.LifecycleResult(True, "ensured"),
    )

    ctx = SimpleNamespace(obj=SimpleNamespace(settings=lambda: settings))
    channel_cmd._start_channel_daemon(ctx, "wechat")

    assert (settings.paths.channels_dir / ".reload").is_file(), "must nudge a hot-reload"
    assert calls["enable"] == 0, "must not re-enable/restart a live service"
    assert calls["ensure"] == 1, "reconcile the enabled service only"


def test_channel_login_start_lazy_enables_home_service_when_unconfigured(monkeypatch, settings):
    """``login --start`` on a never-configured host lazily enables the home service."""
    from types import SimpleNamespace

    import omni.cli.commands.channel_cmd as channel_cmd
    from omni.runtime import service_control

    monkeypatch.setattr(channel_cmd, "_warn_missing_runtime_dependency", lambda *a, **k: None)
    monkeypatch.setattr(channel_cmd, "_load_effective_channel_config", lambda *a, **k: {})

    calls = {"enable": 0}
    monkeypatch.setattr(
        service_control, "enable",
        lambda *a, **k: calls.__setitem__("enable", calls["enable"] + 1) or service_control.LifecycleResult(True, "enabled via detached"),
    )

    ctx = SimpleNamespace(obj=SimpleNamespace(settings=lambda: settings))
    channel_cmd._start_channel_daemon(ctx, "feishu")

    assert (settings.paths.channels_dir / ".reload").is_file()
    assert calls["enable"] == 1, "first-time channel config lazily enables the service"


@pytest.mark.asyncio
async def test_channel_manager_degrades_failing_adapter_without_raising(monkeypatch, settings):
    import omni.channels.manager as manager_mod

    def fake_build_channels(names, _settings, _agent):  # noqa: ANN001
        return [_ManagedFakeChannel(names[0], fail=RuntimeError("gateway unreachable"))]

    monkeypatch.setattr(manager_mod, "build_channels", fake_build_channels)
    _write_wechat_ilink_config(settings)
    manager = manager_mod.ChannelManager(
        settings,
        _DummyAgent(),  # type: ignore[arg-type]
        explicit_channels=["wechat"],
        retry_interval=60,
    )

    await manager.reconcile_once()
    for _ in range(10):
        if manager.snapshot()["wechat"]["status"] == "degraded":
            break
        await asyncio.sleep(0)

    health = manager.snapshot()["wechat"]
    assert health["status"] == "degraded"
    assert "gateway unreachable" in health["reason"]
    await manager.stop()


@pytest.mark.asyncio
async def test_dingtalk_stream_registers_current_chatbot_topic():
    from types import SimpleNamespace

    from omni.channels.dingtalk import _build_dingtalk_stream, _normalize_dingtalk_event

    class FakeAckMessage:
        STATUS_OK = 200
        STATUS_SYSTEM_EXCEPTION = 500

    class FakeCredential:
        def __init__(self, client_id: str, client_secret: str) -> None:
            self.client_id = client_id
            self.client_secret = client_secret

    class FakeChatbotMessage:
        TOPIC = "/v1.0/im/bot/messages/get"

        def __init__(self, data: dict) -> None:
            self._data = data

        @classmethod
        def from_dict(cls, data: dict):
            return cls(data)

        def to_dict(self) -> dict:
            return self._data

    class FakeChatbotHandler:
        pass

    class FakeStreamClient:
        def __init__(self, credential: FakeCredential) -> None:
            self.credential = credential
            self.topic = ""
            self.handler = None

        def register_callback_handler(self, topic, handler) -> None:  # noqa: ANN001
            self.topic = topic
            self.handler = handler

    class FakeSDK:
        AckMessage = FakeAckMessage
        Credential = FakeCredential
        ChatbotMessage = FakeChatbotMessage
        ChatbotHandler = FakeChatbotHandler
        DingTalkStreamClient = FakeStreamClient

    received: list[dict[str, str]] = []

    async def inbound(event):
        received.append(_normalize_dingtalk_event(event))

    client = _build_dingtalk_stream(FakeSDK, "cid", "secret", inbound)
    assert client.topic == FakeChatbotMessage.TOPIC
    assert client.credential.client_id == "cid"

    code, message = await client.handler.process(SimpleNamespace(data={
        "msgtype": "text",
        "text": {"content": "你好"},
        "conversationId": "conversation-1",
    }))

    assert (code, message) == (200, "OK")
    assert received == [{"text": "你好", "target": "conversation-1"}]


@pytest.mark.asyncio
async def test_im_channels_send_turn_and_task_notifications(settings):
    from omni.channels.dingtalk import DingTalkChannel
    from omni.channels.feishu import FeishuChannel
    from omni.channels.wechat import WeChatChannel
    from omni.runtime.presentation import task_presentation_from_result

    note = TaskNotification(
        subtask_id="task-123456",
        skill_name="scientific-figure",
        status="succeeded",
        channel="feishu",
        session_id="sess-1",
        external_key="chat-feishu",
        summary="Transformer 架构图完成。",
        artifacts=["artifact://png1"],
        payload=_task_payload(),
    )
    presentation = task_presentation_from_result(
        subtask_id="task-123456",
        skill="scientific-figure",
        status="succeeded",
        result=_task_payload(),
    )

    for cls, name in ((FeishuChannel, "feishu"), (DingTalkChannel, "dingtalk"), (WeChatChannel, "wechat")):
        fake = _FakeOutbound()
        channel = cls(settings, _DummyAgent(), client=fake)  # type: ignore[arg-type]
        await channel.send_turn(f"chat-{name}", presentation)
        channel_note = note
        channel_note.channel = name
        channel_note.external_key = f"chat-{name}"
        await channel.notify(channel_note)

        assert len(fake.sent) >= 2
        assert fake.sent[0][0] == f"chat-{name}"
        assert "Transformer 架构图完成" in fake.sent[0][1]
        assert any("Transformer PNG" in message for _, message in fake.sent)
        # An artifact is named and located on the one line that announces it.
        # The path never runs through the prose above, where a phone cannot use
        # it and the upload beside the message is what the reader acts on.
        assert all(
            "/tmp/transformer.png" not in message.partition("**Artifacts**")[0]
            for _, message in fake.sent
        )
        assert any("`/tmp/transformer.png`" in message for _, message in fake.sent)
        # Ledger ids are for a reader who can look them up. In a thread they are
        # a heading of record keys sitting under the deliverable that was asked
        # for; the terminal still prints them.
        assert all("run-dot" not in message for _, message in fake.sent)
        assert fake.sent[1][0] == f"chat-{name}"


def test_uploadable_roots_combines_store_and_output_dir(tmp_path, settings):
    """The shared allow-list = durable artifacts_dir + the trusted output dir."""
    from types import SimpleNamespace

    from omni.channels.outbound import uploadable_roots

    art_dir = settings.paths.artifacts_dir
    # No store / no mirror → only the durable artifacts store is allowed.
    assert uploadable_roots(settings) == [art_dir]
    assert uploadable_roots(settings, artifacts=SimpleNamespace(mirror_dir=None)) == [art_dir]

    # A trusted launch/output dir (single-copy deliverables land here) is allowed
    # alongside the store — sourced from the agent's ArtifactStore.mirror_dir.
    out = tmp_path / "launch"
    assert uploadable_roots(settings, artifacts=SimpleNamespace(mirror_dir=out)) == [art_dir, out]

    # An explicit mirror equal to the store is de-duplicated by resolved path.
    assert uploadable_roots(settings, mirror_dir=art_dir) == [art_dir]


@pytest.mark.asyncio
async def test_send_turn_uploads_deliverable_written_to_trusted_output_dir(tmp_path, settings):
    """Regression: single-copy deliverables live under the trusted output dir, not
    ``~/.omni/.../artifacts``. ``send_turn`` must allow that dir so figures upload
    natively instead of degrading to an ``artifact://`` text link."""
    from types import SimpleNamespace

    from omni.channels.dingtalk import DingTalkChannel
    from omni.channels.outbound import send_presentation
    from omni.runtime.presentation import task_presentation_from_result

    output_dir = tmp_path / "launch"
    (output_dir / "figures").mkdir(parents=True)
    png = output_dir / "figures" / "rag.png"
    png.write_bytes(b"png-bytes")

    agent = _DummyAgent()
    agent.artifacts = SimpleNamespace(mirror_dir=output_dir)  # trusted launch dir
    fake = _FakeFileOutbound()
    channel = DingTalkChannel(settings, agent, client=fake)  # type: ignore[arg-type]

    presentation = task_presentation_from_result(
        subtask_id="task-abcdef12",
        skill="scientific-figure",
        status="succeeded",
        result={
            "summary": "figure done",
            "artifacts": [
                {"title": "RAG 架构图", "format": "png", "path": str(png),
                 "uri": "artifact://rag1", "mime": "image/png"}
            ],
        },
    )

    await channel.send_turn("chat-1", presentation)

    # Uploaded natively; no degraded artifact:// text link.
    assert fake.images == [("chat-1", str(png))]
    assert all("artifact://rag1" not in message for _, message in fake.markdown)

    # Contrast: the pre-fix allow-list (durable store only) rejects the launch-dir
    # file and degrades to a text link — the exact bug this change fixes.
    fake_old = _FakeFileOutbound()
    report = await send_presentation(
        fake_old, "chat-1", presentation, allowed_roots=[settings.paths.artifacts_dir]
    )
    assert fake_old.images == []
    assert report.degraded
    assert any("artifact://rag1" in message for _, message in fake_old.markdown)
