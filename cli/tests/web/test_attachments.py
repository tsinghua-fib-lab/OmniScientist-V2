"""Web uploads become CLI ``@`` mentions + absolute ``file_uris``."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omni.config import trust as trustmod
from omni.core.file_mentions import format_mention
from omni.web.attachments import bind_web_attachments, normalize_web_file_uri

pytest.importorskip("starlette")

from omni.web.app import create_app  # noqa: E402


async def _rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    res = await client.post(
        "/api",
        headers={"X-Omni-Web": "1"},
        json={"method": method, "params": params or {}},
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_normalize_unquotes_file_uri_with_spaces(tmp_path: Path) -> None:
    paper = tmp_path / "OmniScientist Cli.pdf"
    paper.write_bytes(b"%PDF-1.4")
    assert normalize_web_file_uri(paper.resolve().as_uri()) == str(paper.resolve())
    assert "%" not in (normalize_web_file_uri(paper.resolve().as_uri()) or "")


def test_normalize_drops_missing_and_remote_uris(tmp_path: Path) -> None:
    assert normalize_web_file_uri(str(tmp_path / "gone.pdf")) is None
    assert normalize_web_file_uri("file://example.com/secret.pdf") is None
    assert normalize_web_file_uri("artifact://paper") is None


def test_bind_injects_at_mention_without_duplicating(tmp_path: Path) -> None:
    paper = tmp_path / "OmniScientist Cli.pdf"
    paper.write_bytes(b"%PDF-1.4")
    path = str(paper.resolve())
    text, uris = bind_web_attachments(
        "完整分析总结这篇论文，总结生成 PPT",
        [paper.resolve().as_uri()],
    )
    assert uris == [path]
    assert format_mention(path) in text
    assert text.startswith("完整分析总结这篇论文")

    again, again_uris = bind_web_attachments(text, [path])
    assert again == text
    assert again_uris == [path]


@pytest.mark.asyncio
async def test_upload_returns_absolute_path(tmp_path: Path) -> None:
    work = tmp_path / "upload-repo"
    work.mkdir()
    trustmod.set_trusted(work)
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        opened = await _rpc(client, "workspace.open", {"path": str(work)})
        assert opened["ok"] is True
        res = await client.post(
            "/api/attachment.upload",
            headers={"X-Omni-Web": "1"},
            data={"workspace": str(work)},
            files={"file": ("OmniScientist Cli.pdf", b"%PDF-1.4", "application/pdf")},
        )
        body = res.json()
        assert body["ok"] is True
        dest = Path(body["uri"])
        assert dest.is_file()
        assert not str(body["uri"]).startswith("file:")
        assert dest.name.startswith("OmniScientist")
        assert " " in dest.name


@pytest.mark.asyncio
async def test_turn_start_persists_at_mention(tmp_path: Path) -> None:
    work = tmp_path / "attach-repo"
    work.mkdir()
    trustmod.set_trusted(work)
    paper = work / "OmniScientist Cli.pdf"
    paper.write_bytes(b"%PDF-1.4")
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(work)})
        created = await _rpc(client, "session.create", {"workspace": str(work)})
        sid = created["session"]["id"]
        started = await _rpc(
            client,
            "turn.start",
            {
                "workspace": str(work),
                "session_id": sid,
                "text": "完整分析总结这篇论文，总结生成 PPT",
                "file_uris": [paper.resolve().as_uri()],
            },
        )
        assert started["ok"] is True
        mention = format_mention(str(paper.resolve()))
        for _ in range(50):
            messages = await _rpc(
                client, "session.messages", {"workspace": str(work), "session_id": sid}
            )
            user = next(
                (
                    row
                    for row in messages.get("messages") or []
                    if row.get("role") == "user"
                ),
                None,
            )
            if user and mention in str(user.get("content") or ""):
                assert "file://" not in str(user.get("content") or "")
                return
            await asyncio.sleep(0.05)
        raise AssertionError("persisted user message did not carry the @ attachment")
