"""Regression contract for immutable, content-addressed plan revisions."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType
from omni.agent.plan_revision import (
    canonical_plan_hash,
    create_provider_authority_renewal,
    create_revision,
    deep_clone_plan,
    execution_authority_hash,
    provider_authority_error,
    provider_authority_renewal_chain_is_valid,
    provider_authority_renewal_is_valid,
    provider_authority_snapshot,
    provider_snapshot_authority_error,
    queued_workflow_authority,
    registry_snapshot_hashes,
    runtime_provider_authority_snapshot,
)
from omni.skills_runtime.manifest import (
    DeliveryMode,
    EngineSpec,
    ExecSpec,
    SkillEntry,
    SkillKind,
)


def _plan() -> IntentPlan:
    return IntentPlan(
        task_id="task-revision",
        user_message="为 RAG 系统生成一张架构图。",
        intent_type=IntentType.WORKFLOW,
        outputs=["figure"],
        capability_inputs={"artifact.figure": {"template": "generic"}},
        provider_inputs={
            "scientific-figure": {
                "input": "query, retriever, reranker, LLM",
                "figure_kind": "generic",
            }
        },
        inputs_compiled=True,
        workflow_steps=[
            {
                "id": "figure",
                "capability": "artifact.figure",
                "skill": "scientific-figure",
                "input": {
                    "input": "query, retriever, reranker, LLM",
                    "figure_kind": "generic",
                },
            }
        ],
    )


def _provider_snapshot(name: str) -> dict:
    snapshot = provider_authority_snapshot(
        SkillEntry(
            name=name,
            description="provider authority fixture",
            source="project_omni",
            kind=SkillKind.CLI_EXEC,
        )
    )
    snapshot.update(
        consumer_kind="workflow_step",
        consumer_id=name,
        provider_name=name,
        provider_source="project_omni",
    )
    return snapshot


def test_deep_clone_keeps_the_prior_plan_revision_unchanged() -> None:
    original = _plan()
    cloned = deep_clone_plan(original)

    cloned.workflow_steps[0]["input"]["figure_kind"] = "rag"
    cloned.provider_inputs["scientific-figure"]["figure_kind"] = "rag"
    cloned.capability_inputs["artifact.figure"]["template"] = "rag"

    assert original.workflow_steps[0]["input"]["figure_kind"] == "generic"
    assert original.provider_inputs["scientific-figure"]["figure_kind"] == "generic"
    assert original.capability_inputs["artifact.figure"]["template"] == "generic"
    assert cloned.to_dict() != original.to_dict()


def test_revision_captures_an_immutable_snapshot_and_canonical_hash() -> None:
    plan = _plan()
    revision = create_revision(
        plan,
        revision=2,
        parent_hash="parent-content-hash",
        source="deterministic_repair",
    )
    expected_snapshot = revision.plan.to_dict()

    assert revision.revision == 2
    assert revision.parent_hash == "parent-content-hash"
    assert revision.source == "deterministic_repair"
    assert revision.content_hash == canonical_plan_hash(plan)
    assert revision.plan is not plan

    plan.workflow_steps[0]["input"]["figure_kind"] = "rag"

    assert revision.plan.to_dict() == expected_snapshot
    assert revision.content_hash != canonical_plan_hash(plan)


def test_revision_envelope_cannot_disagree_with_its_materialized_snapshot() -> None:
    revision = create_revision(
        _plan(),
        revision=2,
        parent_hash="parent-content-hash",
        source="deterministic_repair",
    )
    payload = revision.to_dict()
    corruptions = (
        ("revision", 3),
        ("revision_id", "forged:r3:000000000000"),
        ("parent_hash", "different-parent"),
        ("source", "model_repair"),
    )

    for field_name, value in corruptions:
        corrupted = copy.deepcopy(payload)
        corrupted[field_name] = value
        with pytest.raises(ValueError, match="envelope|revision"):
            type(revision).from_dict(corrupted)


def test_canonical_hash_is_order_independent_but_semantically_sensitive() -> None:
    left = _plan()
    right = deep_clone_plan(left)
    right.capability_inputs = {
        key: dict(reversed(list(value.items())))
        for key, value in reversed(list(right.capability_inputs.items()))
    }

    assert canonical_plan_hash(left) == canonical_plan_hash(right)

    right.workflow_steps[0]["input"]["figure_kind"] = "rag"
    assert canonical_plan_hash(left) != canonical_plan_hash(right)


def test_registry_snapshot_hashes_separate_catalog_identity_from_contract_content() -> None:
    entry = SimpleNamespace(
        name="figure",
        source="builtin",
        version="1",
        contract_level="full",
        capabilities=["artifact.figure"],
        deliverables=["artifact.png"],
        input_schema={"type": "object", "properties": {"kind": {"type": "string"}}},
        output_schema={"type": "object"},
        template_signatures={"rag": ["query", "retriever"]},
    )

    class Registry:
        def list_selectable(self):  # noqa: ANN201
            return [entry]

    catalog_before, contract_before = registry_snapshot_hashes(Registry())
    entry.input_schema["properties"]["kind"]["enum"] = ["rag", "generic"]
    catalog_after, contract_after = registry_snapshot_hashes(Registry())

    assert catalog_before == catalog_after
    assert contract_before != contract_after


def test_registry_snapshot_detects_live_engine_dependency_change_without_refresh(
    tmp_path,
) -> None:
    skill_dir = tmp_path / "mutable-engine"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: mutable-engine\ndescription: fixture\n---\nRun it.\n",
        encoding="utf-8",
    )
    engine_path = skill_dir / "engine.py"
    engine_path.write_text(
        "from helper import ENGINE_VERSION\n",
        encoding="utf-8",
    )
    helper_path = skill_dir / "helper.py"
    helper_path.write_text("ENGINE_VERSION = 1\n", encoding="utf-8")
    entry = SkillEntry(
        name="mutable-engine",
        description="fixture",
        source="project_omni",
        path=skill_dir,
        kind=SkillKind.PYTHON_ENGINE,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        engine=EngineSpec(module="engine", class_name="Engine"),
        capabilities=["fixture.mutable"],
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}},
    )

    class Registry:
        def list_selectable(self):  # noqa: ANN201
            return [entry]

    catalog_before, contract_before = registry_snapshot_hashes(Registry())
    helper_path.write_text("ENGINE_VERSION = 2\n", encoding="utf-8")
    catalog_after, contract_after = registry_snapshot_hashes(Registry())

    assert catalog_before != catalog_after
    assert contract_before == contract_after


def test_registry_snapshot_detects_javascript_engine_dependency_change(
    tmp_path,
) -> None:
    skill_dir = tmp_path / "javascript-engine"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: javascript-engine\ndescription: fixture\n---\nRun it.\n",
        encoding="utf-8",
    )
    (skill_dir / "engine.py").write_text(
        "class Engine:\n    async def execute(self, **kwargs):\n        return kwargs\n",
        encoding="utf-8",
    )
    script_path = scripts_dir / "render.mjs"
    script_path.write_text("export const version = 1;\n", encoding="utf-8")
    entry = SkillEntry(
        name="javascript-engine",
        description="fixture",
        source="project_omni",
        path=skill_dir,
        kind=SkillKind.PYTHON_ENGINE,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        engine=EngineSpec(module="engine", class_name="Engine"),
    )

    class Registry:
        def list_selectable(self):  # noqa: ANN201
            return [entry]

    catalog_before, _contract_before = registry_snapshot_hashes(Registry())
    script_path.write_text("export const version = 2;\n", encoding="utf-8")
    catalog_after, _contract_after = registry_snapshot_hashes(Registry())

    assert catalog_before != catalog_after


def test_registry_snapshot_detects_cli_exec_argument_script_change(
    tmp_path,
) -> None:
    skill_dir = tmp_path / "cli-engine"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: cli-engine\ndescription: fixture\n---\nRun it.\n",
        encoding="utf-8",
    )
    script_path = scripts_dir / "run.js"
    script_path.write_text("console.log('v1');\n", encoding="utf-8")
    entry = SkillEntry(
        name="cli-engine",
        description="fixture",
        source="project_omni",
        path=skill_dir,
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command="node", args=["scripts/run.js"]),
    )

    class Registry:
        def list_selectable(self):  # noqa: ANN201
            return [entry]

    catalog_before, _contract_before = registry_snapshot_hashes(Registry())
    script_path.write_text("console.log('v2');\n", encoding="utf-8")
    catalog_after, _contract_after = registry_snapshot_hashes(Registry())

    assert catalog_before != catalog_after


def test_registry_snapshot_does_not_import_dotted_engine_parent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "malicious_parent"
    package.mkdir()
    marker = tmp_path / "parent-imported.txt"
    (package / "__init__.py").write_text(
        (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('imported')\n"
        ),
        encoding="utf-8",
    )
    (package / "engine.py").write_text(
        "class Engine:\n    async def execute(self, **kwargs):\n        return kwargs\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    entry = SkillEntry(
        name="dotted-engine",
        description="fixture",
        source="project_omni",
        kind=SkillKind.PYTHON_ENGINE,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        engine=EngineSpec(
            module="malicious_parent.engine",
            class_name="Engine",
        ),
    )

    class Registry:
        def list_selectable(self):  # noqa: ANN201
            return [entry]

    registry_snapshot_hashes(Registry())

    assert not marker.exists()


def test_registry_snapshot_includes_explicit_provider_outside_planner_catalog() -> None:
    entry = SimpleNamespace(
        name="explicit-only",
        source="user_omni",
        version="1",
        contract_level="full",
        capabilities=["artifact.custom"],
        deliverables=["artifact"],
        input_schema={"type": "object", "properties": {"mode": {"type": "string"}}},
        output_schema={"type": "object"},
        template_signatures={},
    )
    plan = _plan()
    plan.workflow_steps[0]["skill"] = "explicit-only"

    class Registry:
        def list_selectable(self):  # noqa: ANN201
            return []

        def get(self, name: str):  # noqa: ANN201
            return entry if name == "explicit-only" else None

    _catalog_before, contract_before = registry_snapshot_hashes(
        Registry(),
        plan,
    )
    entry.input_schema["properties"]["mode"]["enum"] = ["safe"]
    _catalog_after, contract_after = registry_snapshot_hashes(Registry(), plan)

    assert contract_before != contract_after


def test_provider_authority_renewal_is_content_addressed_and_tamper_evident() -> None:
    provider = _provider_snapshot("render")
    renewal = create_provider_authority_renewal(
        previous_fingerprint="original-authority",
        action="retry_workflow_step:render",
        renewed_at="2026-07-29T10:00:00+00:00",
        provider_authorities=[provider],
    )

    assert provider_authority_renewal_is_valid(renewal)
    tampered = copy.deepcopy(renewal)
    tampered["provider_authorities"][0]["contract"]["input_schema"] = {
        "type": "string"
    }
    assert not provider_authority_renewal_is_valid(tampered)


def test_provider_authority_renewal_chain_is_anchored_and_contiguous() -> None:
    provider_v1 = _provider_snapshot("render-v1")
    provider_v2 = _provider_snapshot("render-v2")
    provider_v3 = _provider_snapshot("render-v3")
    envelope = queued_workflow_authority([provider_v1])
    first = create_provider_authority_renewal(
        previous_fingerprint=envelope["fingerprint"],
        action="retry_workflow_step:render",
        renewed_at="2026-07-29T10:00:00+00:00",
        provider_authorities=[provider_v2],
    )
    second = create_provider_authority_renewal(
        previous_fingerprint=first["fingerprint"],
        action="resume_workflow_step:render",
        renewed_at="2026-07-29T10:01:00+00:00",
        provider_authorities=[provider_v3],
    )
    envelope["provider_authority_renewals"] = [first, second]

    assert provider_authority_renewal_chain_is_valid(envelope)
    broken = copy.deepcopy(envelope)
    broken["provider_authority_renewals"][1]["previous_fingerprint"] = envelope[
        "fingerprint"
    ]
    assert not provider_authority_renewal_chain_is_valid(broken)


def _standalone_authority_with_renewal(
    active: dict,
    renewed: dict,
) -> dict:
    root = copy.deepcopy(active)
    renewal = create_provider_authority_renewal(
        previous_fingerprint=root["fingerprint"],
        action="resume_subtask:fixture",
        renewed_at="2026-07-29T10:03:00+00:00",
        provider_authorities=[renewed],
    )
    return {
        **copy.deepcopy(active),
        "provider_authority_root": root,
        "provider_authority_renewals": [renewal],
        "authority_renewal": renewal,
    }


def test_provider_execution_rejects_tampered_renewal_link() -> None:
    active = _provider_snapshot("standalone")
    expected = _standalone_authority_with_renewal(active, active)
    expected["provider_authority_renewals"][0][
        "previous_fingerprint"
    ] = "forged-root"

    assert "renewal chain is invalid" in provider_snapshot_authority_error(
        active,
        expected,
    )


def test_provider_execution_rejects_tampered_authority_root() -> None:
    active = _provider_snapshot("standalone")
    expected = _standalone_authority_with_renewal(active, active)
    expected["provider_authority_root"]["contract"]["input_schema"] = {
        "type": "string"
    }

    assert "renewal chain is invalid" in provider_snapshot_authority_error(
        active,
        expected,
    )


def test_provider_execution_rejects_active_snapshot_behind_latest_renewal() -> None:
    active = _provider_snapshot("provider-v1")
    renewed = _provider_snapshot("provider-v2")
    renewed.update(
        consumer_kind=active["consumer_kind"],
        consumer_id=active["consumer_id"],
        provider_name=active["provider_name"],
        provider_source=active["provider_source"],
    )
    expected = _standalone_authority_with_renewal(active, renewed)

    assert "does not match the latest renewal" in (
        provider_snapshot_authority_error(active, expected)
    )


def test_provider_snapshot_rejects_a_body_that_disagrees_with_its_fingerprint() -> None:
    live = _provider_snapshot("canonical-provider")
    tampered = copy.deepcopy(live)
    tampered["contract"]["input_schema"] = {"type": "string"}

    assert "invalid" in provider_snapshot_authority_error(live, tampered)


def test_provider_authority_chain_recomputes_root_and_nested_fingerprints() -> None:
    provider = _provider_snapshot("rooted-provider")
    envelope = queued_workflow_authority([provider])

    tampered_root = copy.deepcopy(envelope)
    tampered_root["provider_authorities"][0]["contract"]["input_schema"] = {
        "type": "string"
    }
    assert not provider_authority_renewal_chain_is_valid(tampered_root)

    tampered_provider = copy.deepcopy(provider)
    tampered_provider["contract"]["input_schema"] = {"type": "integer"}
    rehashed_envelope = queued_workflow_authority([tampered_provider])
    assert not provider_authority_renewal_chain_is_valid(rehashed_envelope)

    renewal = create_provider_authority_renewal(
        previous_fingerprint=envelope["fingerprint"],
        action="resume",
        renewed_at="2026-07-29T10:02:00+00:00",
        provider_authorities=[tampered_provider],
    )
    assert not provider_authority_renewal_is_valid(renewal)


@pytest.mark.parametrize(
    ("asset_name", "before", "after"),
    [
        ("template.yaml", "theme: light\n", "theme: dark\n"),
        ("fragment.html", "<section>before</section>", "<section>after</section>"),
        ("style.css", ".card { color: black; }", ".card { color: white; }"),
        ("evidence.jsonl", '{"version": 1}\n', '{"version": 2}\n'),
        ("weights.bin", "binary-v1", "binary-v2"),
    ],
)
def test_runtime_asset_changes_invalidate_provider_authority(
    tmp_path,
    asset_name: str,
    before: str,
    after: str,
) -> None:
    skill_dir = tmp_path / "asset-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# asset skill")
    template = skill_dir / asset_name
    template.write_text(before)
    entry = SkillEntry(
        name="asset-skill",
        source="project_omni",
        path=skill_dir,
        description="asset-backed",
        kind=SkillKind.PYTHON_ENGINE,
        engine=EngineSpec(module="engine", class_name="Engine"),
    )
    (skill_dir / "engine.py").write_text(
        "class Engine:\n"
        "    def execute(self, **kw):\n"
        "        return {'status': 'ok'}\n"
    )

    before = provider_authority_snapshot(entry)
    template.write_text(after)
    after = provider_authority_snapshot(entry)

    assert before["fingerprint"] != after["fingerprint"]


def test_symlinked_runtime_tree_fails_closed(tmp_path) -> None:
    skill_dir = tmp_path / "linked-skill"
    target_dir = tmp_path / "shared-assets"
    skill_dir.mkdir()
    target_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# linked skill")
    (skill_dir / "engine.py").write_text(
        "class Engine:\n"
        "    def execute(self, **kw):\n"
        "        return {'status': 'ok'}\n"
    )
    (target_dir / "config.json").write_text('{"version": 1}')
    (skill_dir / "assets").symlink_to(target_dir, target_is_directory=True)
    entry = SkillEntry(
        name="linked-skill",
        source="project_omni",
        path=skill_dir,
        description="linked assets",
        kind=SkillKind.PYTHON_ENGINE,
        engine=EngineSpec(module="engine", class_name="Engine"),
    )
    expected = provider_authority_snapshot(entry)

    assert "symbolic link" in provider_authority_error(entry, expected)


def test_prompt_provider_authority_seals_dynamic_sync_tools() -> None:
    prompt = SkillEntry(
        name="prompt-agent",
        description="prompt",
        kind=SkillKind.PROMPT_ONLY,
    )
    tool = SkillEntry(
        name="inline-tool",
        source="project_omni",
        description="tool",
        kind=SkillKind.CLI_EXEC,
        exec_spec=ExecSpec(
            command="tool",
            args=["--mode", "before"],
        ),
    )

    class Registry:
        def list_sync_tools(self):  # noqa: ANN201
            return [tool]

    registry = Registry()
    expected = runtime_provider_authority_snapshot(registry, prompt)
    tool.exec_spec = ExecSpec(command="tool", args=["--mode", "after"])

    assert "provider execution authority changed" in provider_authority_error(
        prompt,
        expected,
        registry=registry,
    )


def test_uninstalled_unsealed_provider_defers_to_unknown_skill_error() -> None:
    assert provider_authority_error(None, {}) == ""
    assert "no longer installed" in provider_authority_error(
        None,
        {"fingerprint": "sealed-provider"},
    )


def test_execution_authority_fingerprint_binds_plan_contract_catalog_and_grants() -> None:
    plan = _plan()
    baseline = execution_authority_hash(
        plan,
        catalog_hash="catalog-a",
        contract_hash="contract-a",
        approval_tools={"bash", "write_file"},
    )

    assert baseline == execution_authority_hash(
        plan,
        catalog_hash="catalog-a",
        contract_hash="contract-a",
        approval_tools={"write_file", "bash"},
    )
    assert baseline != execution_authority_hash(
        plan,
        catalog_hash="catalog-a",
        contract_hash="contract-b",
        approval_tools={"bash", "write_file"},
    )
    assert baseline != execution_authority_hash(
        plan,
        catalog_hash="catalog-a",
        contract_hash="contract-a",
        approval_tools={"bash"},
    )
