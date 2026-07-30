from __future__ import annotations

import json

import pytest

from omni.skills_runtime.executor import SkillExecutionError, execute_skill
from omni.skills_runtime.install import import_skill, set_imported_skill_trust
from omni.skills_runtime.registry import SkillRegistry


@pytest.mark.asyncio
async def test_imported_skill_is_quarantined_until_owner_trusts_it(tmp_path, settings):
    source = tmp_path / "demo"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\n"
        "name: demo\n"
        "description: demo skill\n"
        "license: MIT\n"
        "metadata:\n"
        "  helixforge:\n"
        "    role: task\n"
        "    capabilities: [demo.run]\n"
        "    input_schema: {type: object, properties: {input: {type: string}}}\n"
        "    output_schema: {type: object, properties: {result: {type: string}}}\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    (source / "engine.py").write_text("print('demo')\n", encoding="utf-8")

    installed = import_skill(str(source), settings.paths)
    assert installed.status == "installed"
    marker = json.loads((installed.dest / ".omni-skill.json").read_text(encoding="utf-8"))
    assert marker["trusted"] is False

    registry = SkillRegistry(settings)
    registry.build_index()
    entry = registry.get("demo")
    assert entry is not None and entry.trusted is False
    assert entry.contract_level == "none"
    assert "demo" not in {item.name for item in registry.list_selectable()}
    with pytest.raises(SkillExecutionError, match="quarantined"):
        await execute_skill(entry, {}, None)  # type: ignore[arg-type]

    result = set_imported_skill_trust("demo", settings.paths, trusted=True)
    assert result.status == "trusted"
    assert result.executable_files == ("engine.py",)
    registry.build_index()
    entry = registry.get("demo")
    assert entry is not None and entry.trusted is True
    assert entry.contract_level == "full"


def test_trust_refuses_skill_without_declared_license(tmp_path, settings):
    source = tmp_path / "unlicensed"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: unlicensed\ndescription: demo\n---\nbody\n", encoding="utf-8"
    )
    assert import_skill(str(source), settings.paths).status == "installed"

    refused = set_imported_skill_trust("unlicensed", settings.paths, trusted=True)
    assert refused.status == "refused"
    forced = set_imported_skill_trust(
        "unlicensed", settings.paths, trusted=True, allow_missing_license=True
    )
    assert forced.status == "trusted"


def test_import_rejects_symlinks(tmp_path, settings):
    source = tmp_path / "linked"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: linked\ndescription: demo\nlicense: MIT\n---\nbody\n", encoding="utf-8"
    )
    (source / "outside.py").symlink_to(tmp_path / "missing.py")

    result = import_skill(str(source), settings.paths)
    assert result.status == "error: source contains symbolic links"
