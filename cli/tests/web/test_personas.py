"""Workspace-scoped scientist persona Web controls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omni.config import load_settings
from omni.config import trust as trustmod

pytest.importorskip("starlette")

from omni.agent import OmniAgent  # noqa: E402
from omni.storage.models import SubtaskORM, TaskEventORM  # noqa: E402
from omni.web import rpc as rpcmod  # noqa: E402
from omni.web.activity import persona_turn_display_title, project_events_after  # noqa: E402
from omni.web.app import create_app  # noqa: E402
from omni.web.personas import folder_persona_input  # noqa: E402


async def _rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    response = await client.post(
        "/api",
        headers={"X-Omni-Web": "1"},
        json={"method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _write_persona(root: Path, scientist_id: str, name: str) -> None:
    target = root / scientist_id
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(
        json.dumps({"scientist_id": scientist_id}), encoding="utf-8"
    )
    (target / "identity.json").write_text(
        json.dumps(
            {
                "scientist_id": scientist_id,
                "scientist_name": name,
                "aliases": [name, f"{name} alias"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_invalid_aliases(root: Path, scientist_id: str, name: str) -> None:
    target = root / scientist_id
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(
        json.dumps({"scientist_id": scientist_id}), encoding="utf-8"
    )
    (target / "identity.json").write_text(
        json.dumps(
            {
                "scientist_id": scientist_id,
                "scientist_name": name,
                "aliases": 123,
            }
        ),
        encoding="utf-8",
    )


def _activate_fixture(project: Path, scientist_id: str, name: str) -> None:
    lock = project / ".soulagent" / "lock"
    lock.mkdir(parents=True)
    (lock / "ready").touch()
    (project / ".soulagent" / "state.json").write_text(
        json.dumps(
            {
                "host": "omniscientist",
                "scientist_id": scientist_id,
                "scientist_name": name,
            }
        ),
        encoding="utf-8",
    )
    (project / "role.md").write_text("Research taste.", encoding="utf-8")


def test_folder_persona_input_matches_cli_phrases() -> None:
    assert folder_persona_input(action="activate", scientist_name="Fengli Xu") == (
        "think like Fengli Xu"
    )
    assert folder_persona_input(action="switch", scientist_name="Herbert A. Simon") == (
        "think like Herbert A. Simon"
    )
    assert folder_persona_input(action="unload") == "restore yourself"


def test_persona_protocol_titles_are_browser_friendly() -> None:
    assert persona_turn_display_title(
        '$soulagent {"action":"activate","input":"Research task: Study memory"}'
    ) == "Study memory"
    assert persona_turn_display_title(
        '$soulagent {"action":"activate","input":"think like Claude Shannon"}'
    ) == "think like Claude Shannon"
    assert persona_turn_display_title(
        '$soulagent {"action":"unload","input":"restore yourself"}'
    ) == "Scientist persona"
    assert persona_turn_display_title("ordinary research task") == "ordinary research task"


@pytest.mark.asyncio
async def test_describe_uses_the_cli_persona_projection_without_exposing_stoma(
    tmp_path: Path,
) -> None:
    project = tmp_path / "研究项目"
    project.mkdir()
    trustmod.set_trusted(project)
    _write_persona(project / "scientist-kg", "kaiming-he", "Kaiming He")
    _activate_fixture(project, "kaiming-he", "Kaiming He")

    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(project)})
        data = await _rpc(client, "persona.describe", {"workspace": str(project)})

    assert data["ok"] is True
    persona = data["persona"]
    assert persona["active"] is True
    assert persona["scientist_id"] == "kaiming-he"
    assert persona["scientist_name"] == "Kaiming He"
    assert persona["scanner"] == "project"
    assert persona["available"] == [
        {
            "scientist_id": "kaiming-he",
            "scientist_name": "Kaiming He",
            "aliases": ["Kaiming He", "Kaiming He alias"],
        }
    ]
    serialized = json.dumps(data)
    assert "persona_text" not in serialized
    assert "Research taste" not in serialized
    assert "role.md" not in serialized


@pytest.mark.asyncio
async def test_describe_does_not_open_the_persona_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "metadata-only"
    project.mkdir()
    trustmod.set_trusted(project)
    _write_persona(project / "scientist-kg", "kaiming-he", "Kaiming He")
    _activate_fixture(project, "kaiming-he", "Kaiming He")
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):  # noqa: ANN002, ANN003
        if path == project / "role.md":
            raise AssertionError("persona.describe must not read role.md prose")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(project)})
        data = await _rpc(client, "persona.describe", {"workspace": str(project)})

    assert data["ok"] is True
    assert data["persona"]["active"] is True
    assert data["persona"]["scientist_id"] == "kaiming-he"



@pytest.mark.asyncio
async def test_describe_is_read_only_and_isolates_a_malformed_catalog_entry(
    tmp_path: Path,
) -> None:
    project = tmp_path / "read-only-persona"
    project.mkdir()
    trustmod.set_trusted(project)
    _write_persona(project / "scientist-kg", "alan-turing", "Alan Turing")
    _write_invalid_aliases(project / "scientist-kg", "broken", "Broken")
    role = project / "role.md"
    backup = project / "role.md.soulagent.bak"
    role.write_text("temporary persona", encoding="utf-8")
    backup.write_text("original role", encoding="utf-8")

    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(project)})
        data = await _rpc(client, "persona.describe", {"workspace": str(project)})

    assert data["ok"] is True
    assert [item["scientist_id"] for item in data["persona"]["available"]] == [
        "alan-turing"
    ]
    assert data["persona"]["invalid"] == [
        {"directory": "broken", "error": "aliases must be a list of strings"}
    ]
    assert role.read_text(encoding="utf-8") == "temporary persona"
    assert backup.read_text(encoding="utf-8") == "original role"


@pytest.mark.asyncio
async def test_describe_isolates_a_non_utf8_catalog_entry(tmp_path: Path) -> None:
    project = tmp_path / "non-utf8-persona"
    project.mkdir()
    trustmod.set_trusted(project)
    _write_persona(project / "scientist-kg", "alan-turing", "Alan Turing")
    broken = project / "scientist-kg" / "broken"
    broken.mkdir()
    (broken / "identity.json").write_bytes(b"\xff\xfe")
    (broken / "manifest.json").write_text(
        json.dumps({"scientist_id": "broken"}),
        encoding="utf-8",
    )

    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(project)})
        data = await _rpc(client, "persona.describe", {"workspace": str(project)})

    assert data["ok"] is True
    assert [item["scientist_id"] for item in data["persona"]["available"]] == [
        "alan-turing"
    ]
    assert data["persona"]["invalid"] == [
        {
            "directory": "broken",
            "error": "missing identity or scientist_id does not match the directory",
        }
    ]


@pytest.mark.asyncio
async def test_start_wraps_a_local_selection_in_the_normal_soulagent_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "persona-turn"
    project.mkdir()
    trustmod.set_trusted(project)
    _write_persona(project / "scientist-kg", "fengli-xu", "Fengli Xu")
    captured: dict[str, Any] = {}

    async def _fake_start_turn(hub, rec, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return {
            "session_id": "session-1",
            "task_id": "task-1",
            "client_run_id": "run-1",
            "channel": "web",
            "kind": "turn",
        }

    monkeypatch.setattr(rpcmod.turns, "start_turn", _fake_start_turn)

    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(project)})
        data = await _rpc(
            client,
            "persona.start",
            {
                "workspace": str(project),
                "action": "activate",
                "scientist_id": "fengli-xu",
                "task_context": "评估 agent memory 的检索架构",
                "session_id": "session-1",
                "project_root": "/tmp/escape",
            },
        )

    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_params"
    assert captured == {}

    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(project)})
        data = await _rpc(
            client,
            "persona.start",
            {
                "workspace": str(project),
                "action": "activate",
                "scientist_id": "fengli-xu",
                "session_id": "session-1",
            },
        )
        listed = await _rpc(client, "session.list", {"workspace": str(project)})

    assert data["ok"] is True
    assert data["task_id"] == "task-1"
    assert captured["session_id"]
    assert captured["session_id"] != "session-1"
    assert captured["interaction_mode"] == "auto"
    assert captured["file_uris"] == []
    text = str(captured["text"])
    assert text.startswith("$soulagent ")
    payload = json.loads(text.removeprefix("$soulagent "))
    assert payload == {
        "input": "think like Fengli Xu",
        "action": "activate",
        "scientist_id": "fengli-xu",
        "force": False,
        "project_root": str(project.resolve()),
    }
    assert "kg_root" not in payload
    assert "Research task:" not in payload["input"]
    assert all(
        item.get("external_key") != "persona-control" for item in listed["sessions"]
    )


@pytest.mark.asyncio
async def test_persona_mutations_fail_closed_for_untrusted_or_unknown_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "untrusted-persona"
    project.mkdir()
    _write_persona(project / "scientist-kg", "alan-turing", "Alan Turing")

    async def _must_not_start(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("untrusted or invalid requests must not start a turn")

    monkeypatch.setattr(rpcmod.turns, "start_turn", _must_not_start)
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(project)})
        described = await _rpc(
            client,
            "persona.describe",
            {"workspace": str(project)},
        )
        refused = await _rpc(
            client,
            "persona.start",
            {
                "workspace": str(project),
                "action": "activate",
                "scientist_id": "alan-turing",
                "task_context": "研究可计算性",
            },
        )
    assert described["ok"] is True
    assert described["persona"]["writable"] is False
    assert [item["scientist_id"] for item in described["persona"]["available"]] == [
        "alan-turing"
    ]
    assert refused["ok"] is False
    assert refused["error"]["code"] == "untrusted"

    trustmod.set_trusted(project)
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(project)})
        unknown = await _rpc(
            client,
            "persona.start",
            {
                "workspace": str(project),
                "action": "activate",
                "scientist_id": "not-installed",
            },
        )
    assert unknown["ok"] is False
    assert unknown["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_start_rejects_an_active_soulagent_task_in_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "persona-exclusive"
    project.mkdir()
    trustmod.set_trusted(project)
    _write_persona(project / "scientist-kg", "alan-turing", "Alan Turing")
    settings = load_settings(cwd=project, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="web")
        task = await agent.tasks.create_task(
            session_id=session_id,
            channel="web",
            user_input=(
                '$soulagent {"action":"activate","scientist_id":"alan-turing",'
                '"input":"private task context"}'
            ),
        )
    finally:
        await agent.aclose()

    async def _must_not_start(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("a second workspace persona operation must not start")

    monkeypatch.setattr(rpcmod.turns, "start_turn", _must_not_start)
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(project)})
        described = await _rpc(
            client,
            "persona.describe",
            {"workspace": str(project)},
        )
        refused = await _rpc(
            client,
            "persona.start",
            {
                "workspace": str(project),
                "action": "activate",
                "scientist_id": "alan-turing",
                "task_context": "Study computability",
            },
        )

    assert described["persona"]["operation"] == {
        "task_id": task.id,
        "status": "running",
        "action": "activate",
        "scientist_id": "alan-turing",
    }
    assert "private task context" not in json.dumps(described)
    assert refused["ok"] is False
    assert refused["error"]["code"] == "busy"
    assert refused["error"]["task_id"] == task.id


@pytest.mark.asyncio
async def test_describe_clears_operation_after_soulagent_skill_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "persona-skill-settled"
    project.mkdir()
    trustmod.set_trusted(project)
    _write_persona(project / "scientist-kg", "alan-turing", "Alan Turing")
    settings = load_settings(cwd=project, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="web")
        task = await agent.tasks.create_task(
            session_id=session_id,
            channel="web",
            user_input=(
                '$soulagent {"action":"activate","scientist_id":"alan-turing",'
                f'"project_root":"{project.resolve()}",'
                '"input":"Activate Alan Turing as the scientist persona"}'
            ),
        )
        async with agent.db.session() as session:
            session.add(
                SubtaskORM(
                    id="soulagent-folder-settled",
                    session_id=session_id,
                    task_id=task.id,
                    skill_name="soulagent",
                    status="succeeded",
                    input_json={"action": "activate", "scientist_id": "alan-turing"},
                    result_json={"status": "ok", "outcome": {"code": "refreshed"}},
                )
            )
            await session.commit()
    finally:
        await agent.aclose()

    captured: dict[str, Any] = {}

    async def _fake_start_turn(hub, rec, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return {
            "session_id": "session-next",
            "task_id": "task-next",
            "client_run_id": "run-next",
            "channel": "web",
            "kind": "turn",
        }

    monkeypatch.setattr(rpcmod.turns, "start_turn", _fake_start_turn)
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(project)})
        described = await _rpc(client, "persona.describe", {"workspace": str(project)})
        started = await _rpc(
            client,
            "persona.start",
            {
                "workspace": str(project),
                "action": "activate",
                "scientist_id": "alan-turing",
            },
        )
        status = await _rpc(
            client,
            "persona.status",
            {"workspace": str(project), "task_id": task.id},
        )

    assert described["persona"]["operation"] is None
    assert started["ok"] is True
    assert captured["text"].startswith("$soulagent ")
    assert status["task_status"] == "running"
    assert status["skill_status"] == "succeeded"
    assert status["outcome_code"] == "refreshed"


@pytest.mark.asyncio
async def test_status_and_task_projection_keep_soulagent_prose_server_side(
    tmp_path: Path,
) -> None:
    project = tmp_path / "persona-settlement"
    project.mkdir()
    trustmod.set_trusted(project)
    settings = load_settings(cwd=project, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="web")
        task = await agent.tasks.create_task(
            session_id=session_id,
            channel="web",
            user_input=(
                '$soulagent {"action":"activate",'
                '"input":"Research task: private research topic"}'
            ),
        )
        async with agent.db.session() as session:
            session.add(
                SubtaskORM(
                    id="soulagent-web-settlement",
                    session_id=session_id,
                    task_id=task.id,
                    skill_name="soulagent",
                    status="succeeded",
                    input_json={
                        "action": "activate",
                        "scientist_id": "fengli-xu",
                        "project_root": str(project),
                    },
                    result_json={
                        "status": "ok",
                        "outcome": {"code": "refreshed"},
                        "active": True,
                        "project_root": str(project),
                        "role_path": str(project / "role.md"),
                        "persona_text": "private scientist persona prose",
                    },
                )
            )
            session.add(
                TaskEventORM(
                    task_id=task.id,
                    seq=99,
                    event_type="subtask.done",
                    status="succeeded",
                    skill_name="soulagent",
                    summary=f"persona stored at {project / 'role.md'}",
                    input_json={
                        "action": "activate",
                        "scientist_id": "fengli-xu",
                        "project_root": str(project),
                    },
                    output_json={
                        "outcome": {"code": "refreshed"},
                        "project_root": str(project),
                        "persona_text": "private event persona prose",
                    },
                    error=f"failed to read {project / 'role.md'}",
                )
            )
            session.add(
                TaskEventORM(
                    task_id=task.id,
                    seq=100,
                    event_type="task.failed",
                    status="failed",
                    name=f"root persona at {project / 'role.md'}",
                    summary=f"root persona prose stored at {project / 'role.md'}",
                    output_json={
                        "project_root": str(project),
                        "persona_text": "private root-event persona prose",
                    },
                    error=f"root failure at {project / 'role.md'}",
                )
            )
            await session.commit()
        await agent.tasks.finish_task(
            task.id,
            status="succeeded",
            summary=f"root persona prose stored at {project / 'role.md'}",
            error=f"root failure at {project / 'role.md'}",
        )
        live_projection = await project_events_after(agent, task.id, after_seq=99)
    finally:
        await agent.aclose()

    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(project)})
        status = await _rpc(
            client,
            "persona.status",
            {"workspace": str(project), "task_id": task.id},
        )
        detail = await _rpc(
            client,
            "task.get",
            {"workspace": str(project), "task_id": task.id},
        )
        events = await _rpc(
            client,
            "task.events",
            {"workspace": str(project), "task_id": task.id},
        )
        sessions = await _rpc(
            client,
            "session.list",
            {"workspace": str(project)},
        )

    assert status == {
        "ok": True,
        "task_id": task.id,
        "task_status": "succeeded",
        "skill_status": "succeeded",
        "outcome_code": "refreshed",
    }
    serialized = json.dumps(detail)
    assert "private scientist persona prose" not in serialized
    assert "private event persona prose" not in serialized
    assert "private root-event persona prose" not in serialized
    assert str(project) not in serialized
    assert detail["task"]["summary"] == "SoulAgent operation update"
    assert detail["task"]["error"] == "SoulAgent operation failed"
    assert detail["task"]["title"] == "private research topic"
    session_row = next(item for item in sessions["sessions"] if item["id"] == session_id)
    assert session_row["display_title"] == "private research topic"
    detail_root_event = next(item for item in detail["events"] if item["seq"] == 100)
    assert detail_root_event["name"] == "SoulAgent operation update"
    execution = detail["executions"][0]
    assert execution["input_json"] == {
        "action": "activate",
        "scientist_id": "fengli-xu",
    }
    assert execution["result_json"]["redacted"] is True
    assert execution["result_json"]["outcome"] == {"code": "refreshed"}
    event_serialized = json.dumps(events)
    assert "private event persona prose" not in event_serialized
    assert "private root-event persona prose" not in event_serialized
    assert str(project) not in event_serialized
    soul_event = next(item for item in events["events"] if item["skill"] == "soulagent")
    assert soul_event["summary"] == "SoulAgent operation update"
    assert soul_event["error"] == "SoulAgent operation failed"
    root_event = next(item for item in events["events"] if item["seq"] == 100)
    assert root_event["skill"] == ""
    assert root_event["title"] == "SoulAgent operation update"
    assert root_event["summary"] == "SoulAgent operation update"
    assert root_event["error"] == "SoulAgent operation failed"
    assert root_event["safe_result"] == '{"redacted": true, "outcome": {"code": ""}}'
    live_root_event = next(item for item in live_projection if item["seq"] == 100)
    assert live_root_event == root_event
    assert str(project) not in json.dumps(live_projection)


def _vcs_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


@pytest.mark.asyncio
async def test_describe_stays_on_the_opened_folder_when_parent_has_persona(
    tmp_path: Path,
) -> None:
    repo = _vcs_repo(tmp_path / "repo")
    subdir = repo / "subdir"
    subdir.mkdir()
    trustmod.set_trusted(repo)
    trustmod.set_trusted(subdir)
    _write_persona(repo / "scientist-kg", "fengli-xu", "Fengli Xu")
    _activate_fixture(repo, "fengli-xu", "Fengli Xu")

    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(repo)})
        parent = await _rpc(client, "persona.describe", {"workspace": str(repo)})
        await _rpc(client, "workspace.open", {"path": str(subdir)})
        child = await _rpc(client, "persona.describe", {"workspace": str(subdir)})
        parent_again = await _rpc(client, "persona.describe", {"workspace": str(repo)})

    assert parent["persona"]["active"] is True
    assert parent["persona"]["scientist_id"] == "fengli-xu"
    assert child["persona"]["active"] is False
    assert child["persona"]["scientist_id"] == ""
    assert parent_again["persona"]["active"] is True
    assert parent_again["persona"]["scientist_id"] == "fengli-xu"


@pytest.mark.asyncio
async def test_start_pins_server_project_root_to_the_opened_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _vcs_repo(tmp_path / "repo")
    subdir = repo / "subdir"
    subdir.mkdir()
    trustmod.set_trusted(subdir)
    from omni.config.paths import get_paths

    _write_persona(get_paths(cwd=subdir).scientist_kg_dir, "fengli-xu", "Fengli Xu")
    captured: dict[str, Any] = {}

    async def _fake_start_turn(hub, rec, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        captured["open_path"] = rec.open_path
        captured["invocation_cwd"] = str(rec.paths.invocation_cwd)
        return {
            "session_id": "session-1",
            "task_id": "task-1",
            "client_run_id": "run-1",
            "channel": "web",
            "kind": "turn",
        }

    monkeypatch.setattr(rpcmod.turns, "start_turn", _fake_start_turn)
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(repo)})
        await _rpc(client, "workspace.open", {"path": str(subdir)})
        data = await _rpc(
            client,
            "persona.start",
            {
                "workspace": str(subdir),
                "action": "activate",
                "scientist_id": "fengli-xu",
                "task_context": "评估检索架构",
            },
        )

    assert data["ok"] is True
    payload = json.loads(str(captured["text"]).removeprefix("$soulagent "))
    assert payload["project_root"] == str(subdir.resolve())
    assert captured["open_path"] == str(subdir.resolve())
    assert captured["invocation_cwd"] == str(subdir.resolve())


@pytest.mark.asyncio
async def test_parent_persona_task_does_not_lock_a_child_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _vcs_repo(tmp_path / "repo")
    subdir = repo / "subdir"
    subdir.mkdir()
    trustmod.set_trusted(repo)
    trustmod.set_trusted(subdir)
    from omni.config.paths import get_paths

    _write_persona(get_paths(cwd=subdir).scientist_kg_dir, "alan-turing", "Alan Turing")
    settings = load_settings(cwd=repo, trusted=True)
    agent = await OmniAgent.create(settings)
    try:
        session_id = await agent.ensure_session(channel="web")
        await agent.tasks.create_task(
            session_id=session_id,
            channel="web",
            user_input="$soulagent "
            + json.dumps(
                {
                    "action": "activate",
                    "scientist_id": "fengli-xu",
                    "project_root": str(repo.resolve()),
                    "input": "parent folder task",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    finally:
        await agent.aclose()

    started: dict[str, Any] = {}

    async def _fake_start_turn(hub, rec, **kwargs):  # noqa: ANN001
        started.update(kwargs)
        return {
            "session_id": "session-child",
            "task_id": "task-child",
            "client_run_id": "run-child",
            "channel": "web",
            "kind": "turn",
        }

    monkeypatch.setattr(rpcmod.turns, "start_turn", _fake_start_turn)
    app = create_app(cors_origins=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:1088"
    ) as client:
        await _rpc(client, "workspace.open", {"path": str(subdir)})
        described = await _rpc(client, "persona.describe", {"workspace": str(subdir)})
        started_rpc = await _rpc(
            client,
            "persona.start",
            {
                "workspace": str(subdir),
                "action": "activate",
                "scientist_id": "alan-turing",
                "task_context": "研究可计算性",
            },
        )

    assert described["persona"]["operation"] is None
    assert started_rpc["ok"] is True
    assert started["text"].startswith("$soulagent ")
    payload = json.loads(str(started["text"]).removeprefix("$soulagent "))
    assert payload["project_root"] == str(subdir.resolve())
