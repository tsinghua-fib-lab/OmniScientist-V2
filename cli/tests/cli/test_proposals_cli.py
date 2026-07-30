"""CLI smoke tests for the self-evolution proposal queue (offline)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from omni.cli.main import app
from omni.config import load_settings
from omni.storage.db import get_database
from omni.storage.models import SubtaskORM

runner = CliRunner()


async def _seed_failures(skill: str, goal: str, error: str, n: int) -> None:
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    base = datetime.now(UTC)
    async with db.session() as sess:
        for i in range(n):
            sess.add(SubtaskORM(
                skill_name=skill, status="failed",
                input_json={"goal": goal}, error=error,
                created_at=base + timedelta(seconds=i),
            ))
        await sess.commit()
    await db.dispose()


def _write_user_skill(name: str) -> None:
    s = load_settings()
    d = s.paths.user_skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: analyze\n---\n\n# {name}\n\n原始正文。\n",
        encoding="utf-8",
    )


def test_proposals_list_empty():
    res = runner.invoke(app, ["skills", "proposals", "list"])
    assert res.exit_code == 0
    assert "No proposals" in res.stdout


def test_proposals_scan_list_approve_flow():
    _write_user_skill("analyzer")
    asyncio.run(_seed_failures("analyzer", "分析数据", "KeyError: 'pvalue'", 3))

    scan = runner.invoke(app, ["skills", "proposals", "scan"])
    assert scan.exit_code == 0, scan.stdout
    assert "improvement candidates" in scan.stdout

    listed = runner.invoke(app, ["skills", "proposals", "list", "--json"])
    assert listed.exit_code == 0
    import json as _json

    proposals = _json.loads(listed.stdout)
    improve = next(p for p in proposals if p["kind"] == "improve_skill" and p["skill_name"] == "analyzer")

    approve = runner.invoke(app, ["skills", "proposals", "approve", improve["id"]])
    assert approve.exit_code == 0, approve.stdout
    s = load_settings()
    text = (s.paths.user_skills_dir / "analyzer" / "SKILL.md").read_text(encoding="utf-8")
    assert "Known pitfalls and learned safeguards" in text
