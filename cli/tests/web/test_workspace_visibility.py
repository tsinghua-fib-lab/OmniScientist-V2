from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omni.config import trust as trustmod
from omni.web.app import create_app
from omni.web.workspace_visibility import (
    canonical_project_dir,
    hidden_project_dirs,
    set_hidden_project_dirs,
    visibility_path,
)


async def _rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    response = await client.post(
        "/api",
        headers={"X-Omni-Web": "1"},
        json={"method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_visibility_is_persistent_and_reversible(tmp_path: Path) -> None:
    first = tmp_path / "stores" / "first"
    second = tmp_path / "stores" / "second"

    hidden = set_hidden_project_dirs([first, second], hidden=True, home=tmp_path)

    assert hidden == {canonical_project_dir(first), canonical_project_dir(second)}
    assert hidden_project_dirs(tmp_path) == hidden
    payload = json.loads(visibility_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["hidden_project_dirs"] == sorted(hidden)

    remaining = set_hidden_project_dirs([first], hidden=False, home=tmp_path)
    assert remaining == {canonical_project_dir(second)}
    assert hidden_project_dirs(tmp_path) == remaining


def test_visibility_tolerates_invalid_state_and_normalizes_paths(tmp_path: Path) -> None:
    path = visibility_path(tmp_path)
    path.write_text("not-json", encoding="utf-8")

    assert hidden_project_dirs(tmp_path) == set()
    updated = set_hidden_project_dirs(
        [tmp_path / "folder" / ".." / "workspace"],
        hidden=True,
        home=tmp_path,
    )

    assert updated == {canonical_project_dir(tmp_path / "workspace")}


@pytest.mark.asyncio
async def test_workspace_hide_is_sidebar_only_and_open_unhides(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    trustmod.set_trusted(first)
    trustmod.set_trusted(second)
    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        first_open = await _rpc(client, "workspace.open", {"path": str(first)})
        second_open = await _rpc(client, "workspace.open", {"path": str(second)})
        first_store = first_open["workspace"]["project_dir"]
        second_store = second_open["workspace"]["project_dir"]
        await _rpc(client, "workspace.select", {"project_dir": first_store})

        hidden = await _rpc(
            client,
            "workspace.hideMany",
            {"project_dirs": [second_store]},
        )
        assert hidden["ok"] is True
        assert hidden["project_dirs"] == [canonical_project_dir(second_store)]
        assert second.exists()
        assert Path(second_store, "sessions.sqlite3").is_file()
        assert second_store not in {
            row["project_dir"] for row in hidden["workspaces"]
        }

        listed = await _rpc(client, "workspace.list")
        assert listed["hidden_count"] == 1
        assert second_store not in {
            row["project_dir"] for row in listed["workspaces"]
        }

        # Selecting an already-open hidden workspace is an explicit restore,
        # just like opening its source directory again.
        selected = await _rpc(
            client,
            "workspace.select",
            {"project_dir": second_store},
        )
        assert selected["ok"] is True
        listed = await _rpc(client, "workspace.list")
        assert listed["hidden_count"] == 0

        await _rpc(client, "workspace.select", {"project_dir": first_store})
        await _rpc(
            client,
            "workspace.hideMany",
            {"project_dirs": [second_store]},
        )

        reopened = await _rpc(client, "workspace.open", {"path": str(second)})
        assert reopened["ok"] is True
        listed = await _rpc(client, "workspace.list")
        assert listed["hidden_count"] == 0
        assert second_store in {row["project_dir"] for row in listed["workspaces"]}


@pytest.mark.asyncio
async def test_workspace_hide_rejects_selected_batch_without_partial_change(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    trustmod.set_trusted(first)
    trustmod.set_trusted(second)
    app = create_app(cors_origins=[], trusted_hosts={"omni.test"})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://omni.test") as client:
        first_open = await _rpc(client, "workspace.open", {"path": str(first)})
        second_open = await _rpc(client, "workspace.open", {"path": str(second)})
        first_store = first_open["workspace"]["project_dir"]
        second_store = second_open["workspace"]["project_dir"]

        blocked = await _rpc(
            client,
            "workspace.hideMany",
            {"project_dirs": [first_store, second_store]},
        )
        assert blocked["ok"] is False
        assert blocked["error"]["code"] == "busy"
        listed = await _rpc(client, "workspace.list")
        assert {first_store, second_store}.issubset(
            {row["project_dir"] for row in listed["workspaces"]}
        )
