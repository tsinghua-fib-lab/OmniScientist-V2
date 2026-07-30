"""Static guardrails for the contract-driven harness boundary."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from omni.core.tool_contracts import ProviderInputCompiler, skill_input_contract_error
from omni.skills_runtime.manifest import DeliveryMode, SkillEntry, SkillKind

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_capability_runner_has_no_domain_specific_figure_templates() -> None:
    src = _source("src/omni/agent/capability_runners.py")

    assert "class ArtifactFigureRunner" not in src
    assert "_figure_input" not in src
    assert "RAG Architecture" not in src
    assert "Transformer Architecture" not in src
    assert "RAG 降低幻觉" not in src
    assert "retrieval augmented generation" not in src.lower()


def test_planner_stays_small_and_does_not_build_workflow_recipes() -> None:
    """The planner classifies intent; it never ships a canned research recipe.

    Multi-step sequencing now belongs to the model, so there is no plan-time
    workflow compiler left to hide a recipe in either.
    """
    src = _source("src/omni/agent/planner.py")

    assert len(src.splitlines()) <= 780
    assert "heuristic_research_specs" not in src
    assert "heuristic_plan" not in src
    assert "research_recipe_specs" not in src
    assert "recipe_plan" not in src
    assert "_RECIPE_PATH" not in src
    assert not (ROOT / "src/omni/data/workflow_recipes/research.toml").exists()


def test_plan_executor_schema_builder_does_not_extract_provider_specific_identifiers() -> None:
    src = _source("src/omni/agent/plan_executor.py")

    assert "_extract_arxiv" not in src
    assert "_extract_doi" not in src
    assert "arxiv_id" not in src
    assert "paper_id" not in src


def test_boundary_router_is_not_embedded_in_planner() -> None:
    planner = _source("src/omni/agent/planner.py")
    boundary = _source("src/omni/agent/boundary_router.py")

    assert "class BoundaryRouter" not in planner
    assert "class BoundaryRouter" in boundary
    for forbidden in (
        "_PRODUCT_RE",
        "_WORKSPACE_RE",
        "_SKILL_MGMT_RE",
        "_PREVIOUS_ARTIFACT_RE",
        "_REVISION_RE",
    ):
        assert forbidden not in planner


def test_boundary_router_only_parses_machine_readable_protocol_syntax() -> None:
    src = _source("src/omni/agent/boundary_router.py")

    assert "_EXPLICIT_SKILL_PATTERNS" in src
    assert "run_skill" in src
    assert "use_skill" in src
    assert "class BoundaryDecision" in src
    assert "Natural-language intent belongs to the semantic planner" in src
    for forbidden in (
        "_PRODUCT_RE",
        "_WORKSPACE_RE",
        "_SKILL_MGMT_RE",
        "_PREVIOUS_ARTIFACT_RE",
        "_REVISION_RE",
    ):
        assert forbidden not in src


def test_plan_recovery_does_not_depend_on_the_planner() -> None:
    """The repair layer must not import the produce layer (no import cycle).

    Both the planner (produces plans) and the recovery ladder (repairs them)
    build the same capable ReAct floor via ``plan_factory`` — the shared leaf —
    so recovery never needs a lazy ``from omni.agent.planner import`` back-edge.
    """
    recovery = _source("src/omni/agent/plan_recovery.py")
    factory = _source("src/omni/agent/plan_factory.py")

    assert "from omni.agent.planner import" not in recovery
    assert "import omni.agent.planner" not in recovery
    assert "build_assistant_plan" in recovery  # uses the shared factory instead

    # The factory is the leaf: it depends on neither the producer nor the repairer.
    for edge in ("planner", "plan_recovery"):
        assert f"from omni.agent.{edge} import" not in factory
        assert f"import omni.agent.{edge}" not in factory


def test_orchestrator_delegates_run_and_tool_lifecycle() -> None:
    orchestrator = _source("src/omni/agent/orchestrator.py")
    interaction_lifecycle = _source("src/omni/agent/interaction_lifecycle.py")
    task_controller = _source("src/omni/agent/task_controller.py")
    turn_execution = _source("src/omni/agent/turn_execution.py")
    tool_gateway = _source("src/omni/runtime/tool_gateway.py")

    # Ratchet: the orchestrator is a thin coordinator over extracted collaborators
    # (ConversationStore / SessionCompactor / TurnMemory / ArtifactRevisionRouter /
    # InteractionLifecycle / TaskController / TurnCompletion / ToolGateway).
    # Prefer moving this ceiling down via extraction; 1600 covers the current
    # channel-anchor / task-index / workspace-auto coordination surface without
    # regrowing the pre-extraction monolith.
    assert len(orchestrator.splitlines()) <= 1600
    tree = ast.parse(orchestrator)
    handle_turn_impl = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_handle_turn_impl"
    )
    assert handle_turn_impl.end_lineno is not None
    assert handle_turn_impl.end_lineno - handle_turn_impl.lineno + 1 <= 480
    assert "def _finish_run_for_turn" not in orchestrator
    assert "def _emit_run_tool_event" not in orchestrator
    assert "TaskController(" in orchestrator
    assert "ToolGateway(" in orchestrator
    assert "self.interaction.gate_plan_execution(" in orchestrator
    assert "self.turn_completion.complete_plan(" in orchestrator
    assert "self.turn_completion.complete_react(" in orchestrator
    assert "async def gate_plan_execution" in interaction_lifecycle
    assert "class TaskController" in task_controller
    assert "def finish_turn" in task_controller
    assert "class TurnCompletion" in turn_execution
    assert "async def complete_plan" in turn_execution
    assert "async def complete_react" in turn_execution
    assert "class ToolGateway" in tool_gateway
    assert "def invoker" in tool_gateway
    assert "def emit" in tool_gateway


def test_orchestrator_delegates_attached_artifact_revision() -> None:
    orchestrator = _source("src/omni/agent/orchestrator.py")
    router = _source("src/omni/agent/artifact_revision_router.py")

    # The multi-step attached-figure revision cluster (minor patch → escalate to
    # redraw, plus graphviz-derivative enforcement) lives in its own coordinator;
    # the orchestrator keeps only thin delegators on the turn path.
    assert "ArtifactRevisionRouter(" in orchestrator
    assert "self.artifact_revision.apply(" in orchestrator
    assert "self.artifact_revision.enforce_contracts(" in orchestrator
    assert "def _maybe_route_attached_artifact_revision" not in orchestrator
    assert "def _maybe_route_attached_major_revision" not in orchestrator
    assert "class ArtifactRevisionRouter" in router
    assert "async def apply" in router
    assert "async def enforce_contracts" in router


def test_task_runtime_delegates_workflow_execution() -> None:
    task_runtime = _source("src/omni/runtime/subtask_runtime.py")
    workflow_manager = _source("src/omni/runtime/workflow_run_manager.py")
    workflow_runtime = _source("src/omni/runtime/workflow_runtime.py")
    workflow_state_store = _source("src/omni/runtime/workflow_state_store.py")
    workflow_plan = _source("src/omni/runtime/workflow_plan.py")
    task_results = _source("src/omni/runtime/task_results.py")

    assert len(task_runtime.splitlines()) <= 1100
    assert len(workflow_manager.splitlines()) <= 750
    assert len(workflow_runtime.splitlines()) <= 700
    assert len(workflow_state_store.splitlines()) <= 350
    assert "def _execute_workflow" not in task_runtime
    assert "def _load_workflow_resume_state" not in task_runtime
    assert "def _persist_workflow_checkpoint" not in task_runtime
    assert "WorkflowRunManager(" in task_runtime
    assert "WorkflowRuntime(" in workflow_manager
    assert "async def _execute_skill_step" in workflow_manager
    assert "class WorkflowRuntime" in workflow_runtime
    assert "async def execute" in workflow_runtime
    assert "WorkflowStateStore(" in workflow_runtime
    assert "class WorkflowStateStore" in workflow_state_store
    assert "WorkflowCheckpointORM" in workflow_state_store
    assert "def _prepare_workflow_plan" not in workflow_runtime
    assert "def _prepare_workflow_plan" in workflow_plan
    assert "class WorkflowNeedsInput" in workflow_plan
    assert "def _collect_artifacts" not in task_runtime
    assert "def _collect_artifacts" in task_results


def test_unwired_future_registries_are_not_present() -> None:
    assert not (ROOT / "src/omni/research/connector_registry.py").exists()
    assert not (ROOT / "src/omni/runtime/compute_providers.py").exists()


def test_manifest_does_not_infer_skill_role_from_name_or_capability() -> None:
    src = _source("src/omni/skills_runtime/manifest.py")

    assert "infer_skill_role" not in src
    assert 'return "task" if delivery_mode == DeliveryMode.ASYNC_TASK else "utility"' not in src


def test_provider_input_compiler_uses_schema_shape_and_field_formats() -> None:
    compiler = ProviderInputCompiler()
    schema = {
        "type": "object",
        "required": ["identifier"],
        "properties": {
            "identifier": {
                "type": "string",
                "format": "arxiv_id",
            }
        },
    }

    for request in (
        "Fetch the abstract for arXiv 1706.03762.",
        "请获取 arXiv 1706.03762 摘要。",
        "Obtén el resumen de arXiv 1706.03762.",
    ):
        compiled = compiler.compile_schema(schema, semantic_input={}, raw_message=request)
        assert compiled.arguments == {"identifier": "1706.03762"}
        assert compiled.errors == ()
    for request in (
        "Fetch the abstract for Attention Is All You Need.",
        "请获取 Attention Is All You Need 摘要。",
        "Obtén el resumen de Attention Is All You Need.",
    ):
        compiled = compiler.compile_schema(schema, semantic_input={}, raw_message=request)
        assert compiled.arguments == {}
        assert compiled.errors


def test_provider_input_compiler_binds_one_text_slot_without_aliases() -> None:
    compiler = ProviderInputCompiler()
    schema = {
        "type": "object",
        "required": ["provider_defined_field"],
        "properties": {"provider_defined_field": {"type": "string"}},
    }

    compiled = compiler.compile_schema(
        schema,
        semantic_input={"search_query": "autonomous navigation optimisation"},
        raw_message="Which areas should be improved for fully autonomous navigation?",
    )

    assert compiled.arguments == {"provider_defined_field": "autonomous navigation optimisation"}
    assert compiled.errors == ()


def test_provider_input_compiler_does_not_guess_multiple_required_fields() -> None:
    compiler = ProviderInputCompiler()
    schema = {
        "type": "object",
        "required": ["left", "right"],
        "properties": {
            "left": {"type": "string"},
            "right": {"type": "string"},
        },
    }

    compiled = compiler.compile_schema(
        schema,
        semantic_input={"query": "ambiguous"},
        raw_message="ambiguous",
    )

    assert compiled.arguments == {}
    assert {error["field"] for error in compiled.errors} == {"left", "right"}


def test_provider_input_is_compiled_only_at_the_plan_boundary() -> None:
    capability_runner = _source("src/omni/agent/capability_runners.py")
    workflow_plan = _source("src/omni/runtime/workflow_plan.py")
    workflow_runtime = _source("src/omni/runtime/workflow_runtime.py")

    assert "build_input_from_contract" not in capability_runner
    assert "_adapt_workflow_input" not in workflow_plan
    assert "_alias_value_for_required" not in workflow_plan
    assert "_normalise_contract_aliases" not in workflow_plan
    assert "_adapt_workflow_input" not in workflow_runtime

    allowed = {
        ROOT / "src/omni/core/tool_contracts.py",
        ROOT / "src/omni/agent/plan_validator.py",
        ROOT / "src/omni/runtime/workflow_plan.py",
    }
    offenders = []
    for path in (ROOT / "src/omni").rglob("*.py"):
        if path in allowed:
            continue
        if "ProviderInputCompiler" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_portable_skill_contracts_do_not_require_omni_field_aliases() -> None:
    offenders: list[str] = []
    for path in (ROOT.parent / "skills").glob("*/SKILL.md"):
        source = path.read_text(encoding="utf-8")
        if "x-kind:" in source or "aliases:" in source:
            offenders.append(path.parent.name)

    assert offenders == []


def test_tool_gateway_is_the_only_production_hook_invocation_boundary() -> None:
    allowed = {
        ROOT / "src/omni/runtime/tool_gateway.py",
        ROOT / "src/omni/runtime/hooks.py",
    }
    offenders = []
    for path in (ROOT / "src/omni").rglob("*.py"):
        if path in allowed:
            continue
        if "invoke_tool_with_hooks" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_undeclared_skill_metadata_has_no_contract_or_role_privilege() -> None:
    entry = SkillEntry(
        name="external-thing",
        description="third party skill without Omni contract",
        source="user_codex",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        input_schema={},
        output_schema={},
    )

    assert entry.contract_level == "none"
    assert entry.skill_role == "unknown"
    assert skill_input_contract_error(entry, {"input": "anything"}) == {}


# Vendored, portable academic-persona packages whose authored language is
# intentional: their Chinese is design provenance (``references/`` design docs)
# and model-facing prompt scaffolding for Chinese-language scientists — i.e. it
# is effectively model input/output, not OmniScientist's own control plane. They
# ship as host-neutral third-party skills (own LICENSE/NOTICE) and integrate with
# Omni only through a language-agnostic surface (SKILL.md frontmatter, the
# ``.soulagent`` state protocol, and the ``role.md`` persona stoma read by
# ``omni.agent.persona_stoma``), so the English-only rule does not extend into
# their internals. English stays enforced everywhere else under ``skills/``.
_VENDORED_PERSONA_SKILLS = (
    ROOT.parent / "skills" / "soulagent",
    ROOT.parent / "skills" / "scientist-kg-distiller",
)


def test_production_control_plane_and_public_docs_are_english_only() -> None:
    """User language belongs to model input/output, not runtime source assets."""
    han = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
    roots = [
        ROOT / "src/omni",
        ROOT / "docs",
        ROOT.parent / "skills",
    ]
    files = [ROOT.parent / "README.md", ROOT.parent / "NOTICE"]
    for root in roots:
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".py", ".md", ".toml"}
        )
    exempt = _VENDORED_PERSONA_SKILLS
    violations = [
        str(path.relative_to(ROOT.parent))
        for path in files
        if not any(root in path.parents for root in exempt)
        and han.search(path.read_text(encoding="utf-8"))
    ]

    assert violations == []
