"""Opening D in the web surface uses the same get_paths(cwd=D) key as the CLI."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omni.config import load_settings
from omni.config import trust as trustmod
from omni.config.paths import get_paths, user_home
from omni.personas.roots import persona_state_root

pytest.importorskip("starlette")

from omni.agent import OmniAgent  # noqa: E402
from omni.web.app import create_app  # noqa: E402
from omni.web.host import list_directory  # noqa: E402
from omni.web.protocol import RpcError  # noqa: E402
from omni.web.workspace import WorkspaceHub  # noqa: E402


async def _rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    res = await client.post(
        "/api",
        headers={"X-Omni-Web": "1"},
        json={"method": method, "params": params or {}},
    )
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.asyncio
async def test_open_path_matches_cli_get_paths(tmp_path: Path) -> None:
    work = tmp_path / "repo-a"
    work.mkdir()
    trustmod.set_trusted(work)
    expected = get_paths(cwd=work)
    hub = WorkspaceHub()
    try:
        rec = await hub.open_path(work)
        assert rec.paths.project_dir == expected.project_dir
        assert rec.paths.artifacts_dir == expected.artifacts_dir
        assert rec.paths.workspace_root == expected.workspace_root
        assert rec.paths.invocation_cwd == work.resolve()
        agent = await hub.agent_for(rec)
        assert agent.paths.project_dir == expected.project_dir
        assert agent.paths.artifacts_dir == expected.artifacts_dir
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_cli_session_is_visible_after_web_open_and_absent_in_other_dir(
    tmp_path: Path,
) -> None:
    dir_a = tmp_path / "proj-a"
    dir_b = tmp_path / "proj-b"
    dir_a.mkdir()
    dir_b.mkdir()
    trustmod.set_trusted(dir_a)
    trustmod.set_trusted(dir_b)

    settings_a = load_settings(cwd=dir_a, trusted=True)
    agent_a = await OmniAgent.create(settings_a)
    try:
        sid = await agent_a.ensure_session(channel="cli", title="from-cli")
        await agent_a.conversations.persist_message(sid, "user", "hello from cli")
        paths_a = agent_a.paths
    finally:
        await agent_a.aclose()

    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        opened_a = await _rpc(client, "workspace.open", {"path": str(dir_a)})
        assert opened_a["ok"] is True
        assert opened_a["workspace"]["project_dir"] == str(paths_a.project_dir)
        assert opened_a["workspace"]["artifacts_dir"] == str(paths_a.artifacts_dir)

        listed = await _rpc(client, "session.list", {"workspace": str(dir_a)})
        assert listed["ok"] is True
        ids = {row["id"] for row in listed["sessions"]}
        channels = {row["id"]: row["channel"] for row in listed["sessions"]}
        assert sid in ids
        assert channels[sid] == "cli"

        msgs = await _rpc(
            client, "session.messages", {"workspace": str(dir_a), "session_id": sid}
        )
        assert any(m["content"] == "hello from cli" for m in msgs["messages"])

        created = await _rpc(
            client, "session.create", {"workspace": str(dir_a), "title": "from-web"}
        )
        assert created["ok"] is True
        assert created["session"]["channel"] == "web"

        hub: WorkspaceHub = app.state.hub
        rec = hub.lookup(str(dir_a))
        assert rec is not None
        agent = await hub.agent_for(rec)
        art = await agent.artifacts.put_bytes(
            b"web-bytes", kind="file", title="web-probe", ext="bin"
        )
        assert get_paths(cwd=dir_a).artifacts_dir == agent.paths.artifacts_dir
        arts = await _rpc(client, "artifact.list", {"workspace": str(dir_a)})
        assert any(row["id"] == art.id for row in arts["artifacts"])

        opened_b = await _rpc(client, "workspace.open", {"path": str(dir_b)})
        assert opened_b["ok"] is True
        assert opened_b["workspace"]["project_dir"] != str(paths_a.project_dir)
        listed_b = await _rpc(client, "session.list", {"workspace": str(dir_b)})
        ids_b = {row["id"] for row in listed_b["sessions"]}
        assert sid not in ids_b


def test_list_directory_hides_omni_control_store(tmp_path: Path) -> None:
    listing = list_directory(str(tmp_path), show_hidden=True)
    assert all(Path(entry["path"]).resolve() != user_home() for entry in listing["entries"])
    with pytest.raises(RpcError, match="control store|Omni home"):
        list_directory(str(user_home()))


@pytest.mark.asyncio
async def test_open_control_store_is_rejected() -> None:
    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        body = await _rpc(client, "workspace.open", {"path": str(user_home())})
        assert body["ok"] is False
        assert body["error"]["code"] == "control_store"


@pytest.mark.asyncio
async def test_untrusted_directory_is_read_only(tmp_path: Path) -> None:
    work = tmp_path / "untrusted"
    work.mkdir()
    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        opened = await _rpc(client, "workspace.open", {"path": str(work)})
        assert opened["ok"] is True
        assert opened["workspace"]["writable"] is False
        listed = await _rpc(client, "session.list", {"workspace": str(work)})
        assert listed["ok"] is True
        created = await _rpc(client, "session.create", {"workspace": str(work)})
        assert created["ok"] is False
        assert created["error"]["code"] == "untrusted"


def _vcs_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


@pytest.mark.asyncio
async def test_open_subdir_does_not_reuse_parent_folder_agent(tmp_path: Path) -> None:
    repo = _vcs_repo(tmp_path / "repo")
    subdir = repo / "subdir"
    subdir.mkdir()
    trustmod.set_trusted(repo)
    trustmod.set_trusted(subdir)
    hub = WorkspaceHub()
    try:
        parent = await hub.open_path(repo)
        child = await hub.open_path(subdir)
        parent_agent = await hub.agent_for(parent)
        child_agent = await hub.agent_for(child)
        assert parent.store_key == child.store_key
        assert parent.key != child.key
        assert parent_agent is not child_agent
        assert parent_agent.paths.invocation_cwd == repo.resolve()
        assert child_agent.paths.invocation_cwd == subdir.resolve()
        assert persona_state_root(parent_agent.paths) == repo.resolve()
        assert persona_state_root(child_agent.paths) == subdir.resolve()
        assert hub.lookup(str(repo)).open_path == str(repo.resolve())
        assert hub.lookup(str(subdir)).open_path == str(subdir.resolve())
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_named_project_persona_root_is_the_store_folder() -> None:
    hub = WorkspaceHub()
    try:
        rec = await hub.open_named("lab")
        agent = await hub.agent_for(rec)
        assert rec.paths.workspace_root is None
        assert rec.paths.invocation_cwd == rec.paths.project_dir.resolve()
        assert persona_state_root(rec.paths) == rec.paths.project_dir.resolve()
        assert agent.paths.invocation_cwd == rec.paths.project_dir.resolve()
        assert persona_state_root(agent.paths) == rec.paths.project_dir.resolve()
    finally:
        await hub.aclose()
