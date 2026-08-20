"""Web skill RPC is Home-scoped: builtin + ~/.omni/skills only."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omni.config.paths import get_paths
from omni.skills_runtime.install import deleted_skill_record

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


@pytest.fixture
def app_client():
    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)
    return app, transport


def _write_skill(root: Path, name: str, *, license_text: str = "MIT") -> Path:
    dest = root / name
    dest.mkdir(parents=True)
    frontmatter = (
        f"---\nname: {name}\ndescription: {name} skill\n"
        + (f"license: {license_text}\n" if license_text else "")
        + "---\nbody for {name}\n"
    )
    (dest / "SKILL.md").write_text(frontmatter.format(name=name), encoding="utf-8")
    return dest


@pytest.mark.asyncio
async def test_list_is_home_level_and_includes_builtins(app_client) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        data = await _rpc(client, "skill.list")
    assert data["ok"] is True
    names = {item["skill_id"]: item for item in data["skills"]}
    assert "builtin:arxiv-fetch" in names
    builtin = names["builtin:arxiv-fetch"]
    assert builtin["can_remove"] is False
    assert builtin["can_trust"] is False
    assert builtin["can_untrust"] is False
    assert builtin["active"] is True
    assert builtin["source"] == "builtin"
    assert all(item["source"] in {"builtin", "user_omni"} for item in data["skills"])


@pytest.mark.asyncio
async def test_list_ignores_project_skills(app_client, tmp_path, monkeypatch) -> None:
    project = tmp_path / "repo"
    sneak = project / ".omni" / "skills" / "sneaky-project"
    sneak.mkdir(parents=True)
    (sneak / "SKILL.md").write_text(
        "---\nname: sneaky-project\ndescription: must not appear\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        data = await _rpc(client, "skill.list")
    ids = {item["skill_id"] for item in data["skills"]}
    names = {item["name"] for item in data["skills"]}
    assert "sneaky-project" not in names
    assert "project_omni:sneaky-project" not in ids


@pytest.mark.asyncio
async def test_info_returns_body_kind_and_delivery(app_client) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        data = await _rpc(client, "skill.info", {"skill_id": "builtin:arxiv-fetch"})
    assert data["ok"] is True
    skill = data["skill"]
    assert skill["name"] == "arxiv-fetch"
    assert skill["kind"]
    assert skill["delivery_mode"]
    assert "body" in skill and skill["body"].strip()
    assert skill["can_remove"] is False


@pytest.mark.asyncio
async def test_add_trust_untrust_remove_user_skill(app_client, tmp_path) -> None:
    source = _write_skill(tmp_path, "demo-web")
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        added = await _rpc(client, "skill.add", {"path": str(source)})
        listed = await _rpc(client, "skill.list")
        trusted = await _rpc(client, "skill.trust", {"skill_id": "user_omni:demo-web"})
        untrusted = await _rpc(client, "skill.untrust", {"skill_id": "user_omni:demo-web"})
        removed = await _rpc(client, "skill.remove", {"skill_id": "user_omni:demo-web"})
        after = await _rpc(client, "skill.list")
    assert added["ok"] is True
    assert added["status"] == "installed"
    row = next(item for item in listed["skills"] if item["skill_id"] == "user_omni:demo-web")
    assert row["can_remove"] is True
    assert row["can_trust"] is True
    assert row["trusted"] is False
    assert trusted["ok"] is True
    assert trusted["status"] == "trusted"
    assert untrusted["status"] == "quarantined"
    assert removed["status"] == "removed"
    assert removed["action"] == "physical_delete"
    assert "historical tasks" in removed["notice"].lower()
    dest = get_paths().user_skills_dir / "demo-web"
    assert not dest.exists()
    tombstone = deleted_skill_record("demo-web", get_paths())
    assert tombstone is not None
    assert tombstone["source"] == "user_omni"
    assert tombstone["action"] == "physical_delete"
    assert tombstone["path"] == str(dest)
    assert all(item["skill_id"] != "user_omni:demo-web" for item in after["skills"])


@pytest.mark.asyncio
async def test_builtin_delete_is_refused(app_client) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        data = await _rpc(client, "skill.remove", {"skill_id": "builtin:arxiv-fetch"})
    assert data["ok"] is False
    assert data["error"]["code"] == "forbidden"
    from omni.data import BUILTIN_SKILLS_DIR

    assert (BUILTIN_SKILLS_DIR / "arxiv-fetch" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_bad_names_and_git_specs_are_refused(app_client) -> None:
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        bad_name = await _rpc(client, "skill.remove", {"skill_id": "user_omni:../escape"})
        git = await _rpc(client, "skill.add", {"path": "https://github.com/example/skill.git"})
        tool = await _rpc(client, "skill.add", {"path": "claude:demo"})
    assert bad_name["ok"] is False
    assert bad_name["error"]["code"] == "invalid_params"
    assert git["ok"] is False
    assert "git" in git["error"]["message"].lower()
    assert tool["ok"] is False
    assert "local" in tool["error"]["message"].lower()


@pytest.mark.asyncio
async def test_shadowed_user_skill_is_listed_and_removed_by_id(app_client, tmp_path) -> None:
    source = _write_skill(tmp_path, "arxiv-fetch")
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        added = await _rpc(client, "skill.add", {"path": str(source)})
        listed = await _rpc(client, "skill.list")
        removed = await _rpc(client, "skill.remove", {"skill_id": "user_omni:arxiv-fetch"})
    assert added["ok"] is True
    by_id = {item["skill_id"]: item for item in listed["skills"]}
    assert by_id["builtin:arxiv-fetch"]["active"] is True
    shadowed = by_id["user_omni:arxiv-fetch"]
    assert shadowed["shadowed"] is True
    assert shadowed["shadowed_by"] == "builtin:arxiv-fetch"
    assert shadowed["can_remove"] is True
    assert removed["ok"] is True
    dest = get_paths().user_skills_dir / "arxiv-fetch"
    assert not dest.exists()
    tombstone = deleted_skill_record("arxiv-fetch", get_paths())
    assert tombstone is not None
    assert tombstone["source"] == "user_omni"
    assert tombstone["action"] == "physical_delete"


@pytest.mark.asyncio
async def test_symlink_escape_is_refused(app_client, tmp_path) -> None:
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: escaped\ndescription: no\nlicense: MIT\n---\nbody\n",
        encoding="utf-8",
    )
    skills_dir = get_paths().user_skills_dir
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "escaped").symlink_to(outside)
    _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        data = await _rpc(client, "skill.remove", {"skill_id": "user_omni:escaped"})
    assert data["ok"] is False
    assert data["error"]["code"] == "forbidden"
    assert outside.is_dir()
