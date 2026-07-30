"""Built-in-first precedence, shadowing, and scoped lookup.

Decision (route A): packaged built-ins hard-override a same-named user/external
skill in automatic selection; the only way to reach a shadowed skill is the
explicit ``$<source>:<name>`` escape (exercised end-to-end in the boundary
router / execution tests). Here we pin the registry-level guarantees.
"""

from __future__ import annotations

from omni.config import load_settings
from omni.skills_runtime.manifest import DeliveryMode, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry


def _cap_skill(name: str, capability: str, *, source: str, priority: int = 0) -> SkillEntry:
    return SkillEntry(
        name=name,
        description=f"{name} handles {capability}",
        source=source,
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role="task",
        capabilities=[capability],
        priority=priority,
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
    )


_USER_SKILL_MD = """---
name: {name}
description: A user-supplied override that must NOT beat the built-in.
metadata:
  helixforge:
    kind: cli_exec
    role: task
    delivery: async_task
    capabilities: [literature.search]
---

# {name}

User override body.
"""


def _write_user_skill(user_dir, name: str) -> None:  # noqa: ANN001
    skill_dir = user_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_USER_SKILL_MD.format(name=name), encoding="utf-8")


def test_builtin_hard_overrides_same_named_user_skill_on_disk(settings):
    """A ``~/.omni/skills`` skill with the same name as a built-in is shadowed;
    the built-in wins automatic discovery, and the user copy stays reachable."""
    # Discover the built-in inventory first to pick a real colliding name.
    builtin_only = SkillRegistry(settings, sources=("builtin",))
    builtin_only.build_index()
    builtin_names = [e.name for e in builtin_only.list_all()]
    assert builtin_names, "expected packaged built-in skills"
    victim = builtin_names[0]

    _write_user_skill(settings.paths.user_skills_dir, victim)

    reg = SkillRegistry(settings, sources=("builtin", "user_omni"))
    reg.build_index()

    winner = reg.get(victim)
    assert winner is not None
    assert winner.source == "builtin", "built-in must win the name collision"

    shadowed = reg.shadowed_entries()
    assert any(e.name == victim and e.source == "user_omni" for e in shadowed)

    # The shadowed user skill is still reachable by its scoped key (escape hatch).
    scoped = reg.get_scoped("user_omni", victim)
    assert scoped is not None and scoped.source == "user_omni"


def test_resolve_capability_prefers_builtin_over_higher_priority_user():
    """Source rank dominates priority: built-in wins even when the user skill
    declares a much higher ``priority``."""
    reg = SkillRegistry(load_settings())
    reg.register(_cap_skill("lit-builtin", "literature.search", source="builtin", priority=1))
    reg.register(_cap_skill("lit-user", "literature.search", source="user_omni", priority=999))

    selected, rejected = reg.resolve_capability("literature.search")
    assert selected is not None and selected.source == "builtin"
    assert any(item.name == "lit-user" for item, _ in rejected)


def test_default_sources_drop_project_omni_and_put_builtin_first():
    settings = load_settings()
    assert settings.skills.sources == ["builtin", "user_omni"]
    assert "project_omni" not in settings.skills.sources
