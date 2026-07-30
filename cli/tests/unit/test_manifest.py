"""SKILL.md parsing: Claude-Code compatibility + HelixForge extensions."""

from __future__ import annotations

from pathlib import Path

import yaml

from omni.core.tool_contracts import validate_json_schema
from omni.skills_runtime.discovery import active_skill_names
from omni.skills_runtime.manifest import DeliveryMode, SkillKind, parse_skill_text

CC_SKILL = """---
name: cc_demo
description: A plain Claude Code skill with only name + description.
allowed-tools: read_file, bash
---
# body
do stuff
"""

ENGINE_SKILL = """---
name: eng
description: engine skill
metadata:
  helixforge:
    version: "2.1"
    dependencies: ["python>=3.11", "example-lib"]
    delivery_mode: async_task
    kind: python_engine
    execution: {max_tool_calls: 8, tool_limits: {search_corpus: 3}}
    workflow: {failure_policy: continue_with_partial, allow_failed_dependencies: true}
    status: deprecated
    replaced_by: new-eng
    capabilities: [figure.architecture, artifact.svg]
    priority: 42
    default_for: [架构图, architecture diagram]
    engine: {module: pkg.mod, class: Eng, method: execute}
    input_schema: {type: object, properties: {q: {type: string}}}
    output_schema: {type: object, properties: {answer: {type: string}}}
---
body
"""

EXEC_SKILL = """---
name: ex
description: exec skill
metadata:
  helixforge:
    kind: cli_exec
    exec: {command: python, args: ["-c", "print(1)"], stdout_format: text, timeout_seconds: 5}
  openclaw:
    requires: {bins: [python], env: [FOO]}
---
body
"""


def test_parse_claude_code_skill():
    e = parse_skill_text(CC_SKILL, default_name="x")
    assert e.name == "cc_demo"
    assert e.kind == SkillKind.PROMPT_ONLY
    assert e.delivery_mode == DeliveryMode.SYNC_TOOL
    assert e.allowed_tools == ["read_file", "bash"]
    assert "do stuff" in e.body


def test_parse_engine_skill():
    e = parse_skill_text(ENGINE_SKILL, default_name="x")
    assert e.kind == SkillKind.PYTHON_ENGINE
    assert e.is_async
    assert e.engine.module == "pkg.mod" and e.engine.class_name == "Eng"
    assert e.input_schema["properties"]["q"]["type"] == "string"
    assert e.output_schema["properties"]["answer"]["type"] == "string"
    assert e.execution["max_tool_calls"] == 8
    assert e.execution["tool_limits"]["search_corpus"] == 3
    assert e.workflow["failure_policy"] == "continue_with_partial"
    assert e.workflow["allow_failed_dependencies"] is True
    assert e.is_deprecated
    assert e.replaced_by == "new-eng"
    assert e.capabilities == ["figure.architecture", "artifact.svg"]
    assert e.priority == 42
    assert e.default_for == ["架构图", "architecture diagram"]
    assert e.version == "2.1"
    assert e.dependencies == ["python>=3.11", "example-lib"]


def test_parse_preserves_explicit_null_schema_presence() -> None:
    entry = parse_skill_text(
        """---
name: null-contract
description: invalid explicit null contracts
metadata:
  helixforge:
    input_schema: null
    output_schema: null
---
body
""",
        default_name="null-contract",
        source="project_omni",
    )

    assert entry.input_schema is None
    assert entry.output_schema is None
    assert entry.input_schema_declared is True
    assert entry.output_schema_declared is True
    assert entry.contract_level == "full"


def test_parse_exec_skill():
    e = parse_skill_text(EXEC_SKILL, default_name="x")
    assert e.kind == SkillKind.CLI_EXEC
    assert e.exec_spec.command == "python"
    assert e.exec_spec.stdout_format == "text"
    assert e.requires_bins == ["python"]
    assert e.requires_env == ["FOO"]


def test_no_frontmatter_falls_back_to_name():
    e = parse_skill_text("just text", default_name="fallback")
    assert e.name == "fallback"
    assert e.kind == SkillKind.PROMPT_ONLY


# Vendored, prompt-only academic-persona packages (SoulAgent + its offline
# distiller). They manage a reversible scientist persona / build a scientist KG;
# neither emits portable research provenance, so the core-research contract below
# does not apply to them (same vendored-package carve-out as the English-only rule
# in ``test_contract_driven_boundaries``). The 9 core research skills still must.
_VENDORED_PERSONA_SKILLS = {"soulagent", "scientist-kg-distiller"}


def test_builtin_research_skills_declare_portable_provenance_contract():
    root = Path(__file__).resolve().parents[3] / "skills"
    research_skills = set(active_skill_names(root)) - _VENDORED_PERSONA_SKILLS

    for name in sorted(research_skills):
        text = (root / name / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(text.split("---", 2)[1])
        helix = frontmatter["metadata"]["helixforge"]

        assert helix["research_contract"] == "portable_provenance_v1"
        assert "input_schema" in helix
        assert "output_schema" in helix
        assert "workflow" in helix
        assert "## Portable research provenance" in text


def test_builtin_research_output_contract_is_minimal_and_open():
    root = Path(__file__).resolve().parents[3] / "skills"
    lifecycle_status = ["ok", "partial", "error"]
    common_fields = {
        "status",
        "outcome",
        "summary",
        "warning",
        "recoverable",
        "blocking",
        "error",
        "error_info",
        "research",
    }

    for skill_path in sorted(root.glob("*/SKILL.md")):
        text = skill_path.read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(text.split("---", 2)[1])
        helix = (frontmatter.get("metadata") or {}).get("helixforge") or {}
        if helix.get("research_contract") != "portable_provenance_v1":
            continue

        schema = helix["output_schema"]
        props = schema.get("properties") or {}

        assert schema.get("required") == ["status"], skill_path
        assert schema.get("additionalProperties") is not False, skill_path
        assert props["status"].get("enum") == lifecycle_status, skill_path
        assert common_fields <= set(props), skill_path


def test_paper_review_scientist_perspective_requires_loaded_persona() -> None:
    root = Path(__file__).resolve().parents[3] / "skills" / "paper-review"
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    helix = yaml.safe_load(text.split("---", 2)[1])["metadata"]["helixforge"]
    input_schema = helix["input_schema"]
    output_schema = helix["output_schema"]

    loaded_persona = {
        "loaded": True,
        "active_scientist_id": "kaiming-he",
    }
    inactive_persona = {
        "loaded": False,
        "active_scientist_id": None,
    }
    valid_input = {
        "input": "review.pdf",
        "claim_scientist_perspective": True,
        "requested_scientist_id": "kaiming-he",
        "persona": loaded_persona,
    }
    invalid_input = {**valid_input, "persona": inactive_persona}
    assert not list(validate_json_schema(valid_input, input_schema, declared=True))
    assert list(validate_json_schema(invalid_input, input_schema, declared=True))

    valid_claim = {
        "status": "ok",
        "scientist_perspective_applied": True,
        "persona": loaded_persona,
    }
    invalid_claim = {**valid_claim, "persona": inactive_persona}
    generic_review = {"status": "ok", "scientist_perspective_applied": False}
    assert not list(validate_json_schema(valid_claim, output_schema, declared=True))
    assert list(validate_json_schema(invalid_claim, output_schema, declared=True))
    assert not list(validate_json_schema(generic_review, output_schema, declared=True))
