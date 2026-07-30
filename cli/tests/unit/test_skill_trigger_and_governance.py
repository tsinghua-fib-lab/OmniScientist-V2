"""P1/P2/P3 skill parity with Codex.

- P1 (description-triggering): a capability-less skill is still discoverable and
  model-visible purely by its description + when-to-use. Codex matches on
  name+description with no capability taxonomy; omni must not *require* a declared
  capability for a user skill to be reachable.
- P2 (declarative runtime, generic enforcement): a skill's declared executable
  requirements are enforced by ONE generic preflight (no per-skill host code).
  Env vars are intentionally not gated — Codex dropped env-var prompting, and a
  hard env gate would break offline runs whose secrets live elsewhere.
- P3 (governance primitive): ``allow_implicit_invocation: false`` (Codex's flag)
  hides a skill from automatic selection while keeping it runnable via the
  explicit ``$name`` / ``$<scope>:name`` escape.
"""

from __future__ import annotations

import pytest

from omni.agent.model_planner import _planner_skill_catalog
from omni.config import load_settings
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.executor import _missing_binary_action, execute_skill
from omni.skills_runtime.manifest import (
    DeliveryMode,
    ExecSpec,
    SkillEntry,
    SkillKind,
    parse_skill_text,
)
from omni.skills_runtime.registry import SkillRegistry


def _desc_skill(
    name: str,
    description: str,
    *,
    when_to_use: str = "",
    source: str = "user_omni",
    capabilities: list[str] | None = None,
) -> SkillEntry:
    return SkillEntry(
        name=name,
        description=description,
        source=source,
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        role="task",
        capabilities=list(capabilities or []),
        when_to_use=when_to_use,
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
    )


def _bin_skill(name: str, bins: list[str]) -> SkillEntry:
    return SkillEntry(
        name=name,
        description=f"{name} needs {bins}",
        source="user_omni",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command="true", args=[], stdout_format="json"),
        requires_bins=list(bins),
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
    )


# ── P1: description-based triggering (no capability declaration) ───────────────
def test_capability_less_skill_is_discoverable_by_description():
    reg = SkillRegistry(load_settings(), sources=())
    reg.register(
        _desc_skill(
            "kelp-forest-mapper",
            "Map and quantify kelp forest canopy from satellite imagery.",
            when_to_use="Use when the user asks about kelp canopy coverage or seaweed biomass.",
            capabilities=[],  # deliberately no capability taxonomy
        )
    )
    hits = reg.suggest("estimate kelp canopy coverage from imagery")
    assert any(e.name == "kelp-forest-mapper" for e in hits)


def test_capability_less_skill_appears_in_planner_catalog_with_when_to_use():
    reg = SkillRegistry(load_settings(), sources=())
    reg.register(
        _desc_skill(
            "kelp-forest-mapper",
            "Map kelp forest canopy from imagery.",
            when_to_use="Use for kelp canopy coverage questions.",
            capabilities=[],
        )
    )
    catalog = _planner_skill_catalog(reg)
    assert "kelp-forest-mapper" in catalog
    # Trigger guidance is surfaced so the model can match a capability-less skill.
    assert "when_to_use=" in catalog


# ── P2: generic executable preflight; env NOT gated ───────────────────────────
def test_missing_binary_preflight_blocks_generically():
    action = _missing_binary_action(_bin_skill("needs-foo", ["totally-missing-bin-xyzzy"]))
    assert action is not None
    assert action["action_required"] == {"kind": "install", "bins": ["totally-missing-bin-xyzzy"]}
    assert action["error_info"]["code"] == "missing_binary"
    assert action["blocking"] is True


def test_interpreter_and_present_bins_do_not_block():
    # ``python``/``python3`` are satisfied by the running interpreter; empty is a no-op.
    assert _missing_binary_action(_bin_skill("needs-py", ["python3", "python"])) is None
    assert _missing_binary_action(_bin_skill("needs-none", [])) is None


@pytest.mark.asyncio
async def test_execute_skill_rejects_admission_on_missing_binary():
    settings = load_settings()
    settings.paths.ensure_dirs()
    ctx = ExecContext(settings=settings, paths=settings.paths, session_id="s1", channel="")
    result = await execute_skill(
        _bin_skill("needs-foo", ["totally-missing-bin-xyzzy"]), {"input": "x"}, ctx
    )
    assert result["status"] == "error" and result["blocking"] is True
    assert result["action_required"]["kind"] == "install"


# ── P3: allow_implicit_invocation governance ──────────────────────────────────
_HIDDEN_SKILL_MD = """---
name: {name}
description: {desc}
metadata:
  helixforge:
    kind: cli_exec
    role: task
    delivery: async_task
    capabilities: [literature.search]
    allow_implicit_invocation: false
    trigger:
      when_to_use: Use for stealth literature lookups.
---

# {name}

Hidden skill body.
"""


def test_allow_implicit_defaults_true_and_parses_false():
    plain = parse_skill_text(
        "---\nname: plain\ndescription: plain skill.\n---\n# plain\nbody",
        default_name="plain",
        source="user_omni",
    )
    assert plain.allow_implicit is True

    hidden = parse_skill_text(
        _HIDDEN_SKILL_MD.format(name="stealth-lit", desc="A hidden literature engine."),
        default_name="stealth-lit",
        source="user_omni",
    )
    assert hidden.allow_implicit is False


def test_hidden_skill_excluded_from_auto_but_reachable_by_name():
    reg = SkillRegistry(load_settings(), sources=())
    reg.register(
        parse_skill_text(
            _HIDDEN_SKILL_MD.format(
                name="stealth-lit", desc="Stealth literature canopy engine mapper."
            ),
            default_name="stealth-lit",
            source="user_omni",
        )
    )

    # Hidden from every automatic surface.
    assert all(e.name != "stealth-lit" for e in reg.list_selectable())
    assert all(e.name != "stealth-lit" for e in reg.suggest("stealth literature canopy engine mapper"))
    selected, _ = reg.resolve_capability("literature.search", allow_contract_none=True)
    assert selected is None or selected.name != "stealth-lit"

    # Still discovered and explicitly runnable via the escape.
    assert reg.get("stealth-lit") is not None
    assert reg.resolve_explicit("stealth-lit").name == "stealth-lit"
    assert reg.resolve_explicit("user:stealth-lit").name == "stealth-lit"
