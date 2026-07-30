"""BUG-04: skills must not succeed empty, and bundled files must resolve.

Classmates saw paper-review / review-response / scientist-kg-distiller
``succeeded`` with an empty ``text`` after a few bash/read calls, research-ideation
rejected for undeclared ``domain``/``goal`` and falling back to ReAct, and
livefigure failing to import its sibling ``tools.py`` under the host's isolated
interpreter.

Codex's turn loop is progress-driven and still forces a final answer; skill
scripts resolve against the skill root, not process cwd. Omni keeps its own
ArtifactStore and provider contracts: empty bash traces are not a deliverable,
undeclared planner aliases are dropped rather than added to the schema, and
``$OMNI_SKILL_DIR`` is the host-owned analogue of ``$OMNI_OUTPUT_DIR``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType, SkillSelection, VerificationPlan
from omni.agent.plan_recovery import ACTION_EXECUTE, recover
from omni.agent.plan_validator import PlanValidator
from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.tool_contracts import admit_provider_arguments, skill_input_contract_error
from omni.runtime.task_results import _result_has_visible_output
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.executor import execute_skill
from omni.skills_runtime.manifest import (
    DeliveryMode,
    SkillEntry,
    SkillKind,
    parse_skill_path,
)
from omni.skills_runtime.registry import SkillRegistry
from tests.conftest import ScriptedLLM

SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"


def _ctx(**kw) -> ExecContext:
    settings = load_settings()
    settings.paths.ensure_dirs()
    return ExecContext(settings=settings, paths=settings.paths, **kw)


class _EmptyFinalLLM(ScriptedLLM):
    """Reproduce a model that stops after tools with no final prose."""

    async def chat(self, system: str, user: str, **kwargs: object) -> str:
        return ""


def test_bash_traces_are_not_a_visible_skill_deliverable() -> None:
    """Zhao Keyu: paper-review succeeded because bash traces filled partial_outputs."""
    assert not _result_has_visible_output(
        {
            "status": "ok",
            "text": "",
            "partial_outputs": [
                {"tool": "bash", "command": "python scripts/extract_pdf_text.py paper.pdf"},
                {"tool": "bash", "command": "cat references/venues/neurips.md"},
            ],
        }
    )
    assert _result_has_visible_output(
        {
            "status": "ok",
            "text": "",
            "partial_outputs": [{"tool": "write_file", "path": "review.md"}],
        }
    )


def test_paper_review_is_owned_by_the_python_engine() -> None:
    """A small model must not be able to stop after three SKILL.md tool calls."""
    entry = parse_skill_path(SKILLS_ROOT / "paper-review")
    assert entry.kind == SkillKind.PYTHON_ENGINE
    assert (SKILLS_ROOT / "paper-review" / "scripts" / "extract_pdf_text.py").is_file()


def test_research_ideation_domain_goal_aliases_are_admitted() -> None:
    """Fang Yi: planner emitted domain/goal; run_skill rejected and fell back to ReAct."""
    entry = parse_skill_path(SKILLS_ROOT / "research-ideation")
    raw = {"domain": "NLP", "goal": "factuality of RAG"}
    assert skill_input_contract_error(entry, raw)
    admitted, error = admit_provider_arguments(entry, raw)
    assert error == {}
    assert admitted["input"] == "factuality of RAG"
    assert "domain" not in admitted
    assert "goal" not in admitted


def test_research_ideation_plan_with_domain_goal_still_executes() -> None:
    entry = parse_skill_path(SKILLS_ROOT / "research-ideation")
    registry = SkillRegistry(load_settings(), sources=())
    registry.register(entry)
    plan = IntentPlan(
        task_id="run-ideation",
        user_message="survey RAG factuality and propose a research direction",
        intent_type=IntentType.SINGLE_SKILL_TASK,
        selected_skills=[
            SkillSelection(
                skill="research-ideation",
                reason="task",
                contract_level="full",
                matched_capabilities=["research.ideation"],
            )
        ],
        capability_inputs={
            "research.ideation": {"domain": "NLP", "goal": "RAG factuality"}
        },
        verification_plan=VerificationPlan(required_outputs=["report"]),
    )
    validation = PlanValidator(registry).validate(plan)
    outcome = recover(plan, validation, registry)
    assert outcome.action == ACTION_EXECUTE
    assert plan.provider_inputs["research-ideation"]["input"]


@pytest.mark.asyncio
async def test_prompt_skill_empty_done_after_bash_is_not_ok(tmp_path: Path) -> None:
    """review-response / kg-distiller: three tools, then done, empty text."""
    entry = SkillEntry(
        name="review-response",
        description="d",
        kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        body="Draft a rebuttal.",
        path=tmp_path / "review-response",
        allowed_tools=["bash"],
    )
    (tmp_path / "review-response").mkdir()
    ctx = _ctx()
    ctx.llm = _EmptyFinalLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("c1", "bash", {"command": "echo extracted"})]
            ),
            ChatWithToolsResult(content=""),
        ]
    )

    out = await execute_skill(entry, {"input": "reply to reviewer 1"}, ctx)

    assert out["status"] == "error"
    assert "empty result" in str(out.get("error") or "")
    assert not str(out.get("text") or "").strip()


@pytest.mark.asyncio
async def test_prompt_skill_empty_done_is_salvaged(tmp_path: Path) -> None:
    """Codex always forces a final answer; Omni salvages instead of succeeding empty."""
    entry = SkillEntry(
        name="review-response",
        description="d",
        kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        body="Draft a rebuttal.",
        path=tmp_path / "review-response",
        allowed_tools=["bash"],
    )
    (tmp_path / "review-response").mkdir()
    ctx = _ctx()
    ctx.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("c1", "bash", {"command": "echo extracted"})]
            ),
            ChatWithToolsResult(content=""),
        ]
    )

    out = await execute_skill(entry, {"input": "reply to reviewer 1"}, ctx)

    assert out["status"] == "partial"
    assert str(out.get("text") or "").strip()
    assert "stopped without a final answer" in str(out.get("warning") or "")


@pytest.mark.asyncio
async def test_prompt_skill_bash_sees_omni_skill_dir(tmp_path: Path) -> None:
    """Zhao Mengsheng: scripts/extract_pdf_text.py was resolved against cwd."""
    skill_root = tmp_path / "paper-review"
    (skill_root / "scripts").mkdir(parents=True)
    (skill_root / "scripts" / "extract_pdf_text.py").write_text(
        "print('extracted')\n", encoding="utf-8"
    )
    entry = SkillEntry(
        name="paper-review",
        description="d",
        kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        body="Extract the PDF.",
        path=skill_root,
        allowed_tools=["bash"],
    )
    ctx = _ctx(working_dir=tmp_path)
    from omni.skills_runtime.builtin_tools.shell import posix_shell_executable

    if posix_shell_executable() is None:
        pytest.skip("bash tool needs a POSIX shell for $OMNI_SKILL_DIR expansion")
    ctx.settings.security.bash_sandbox = "workspace-write"
    ctx.settings.security.os_sandbox = "off"
    ctx.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        "c1",
                        "bash",
                        {
                            "command": (
                                'test -f "$OMNI_SKILL_DIR/scripts/extract_pdf_text.py" '
                                "&& echo FOUND"
                            )
                        },
                    )
                ]
            ),
            ChatWithToolsResult(content="extracted via skill root"),
        ]
    )

    out = await execute_skill(entry, {"input": "review paper.pdf"}, ctx)

    assert out["status"] == "ok"
    observation = str(out["partial_outputs"][0].get("observation") or "")
    assert "FOUND" in observation


@pytest.mark.asyncio
async def test_livefigure_isolated_interpreter_imports_sibling_tools(tmp_path: Path) -> None:
    """Chen Zhiyu: host ``python -I`` dropped cwd, so ``from tools import *`` failed."""
    import sys

    skill_dir = SKILLS_ROOT / "livefigure"
    sys.path.insert(0, str(skill_dir))
    try:
        from livefigure.pipeline import _execute_code
    finally:
        sys.path.remove(str(skill_dir))

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "tools.py").write_text(
        "marker = 'bundled'\n",
        encoding="utf-8",
    )
    code_path = output_dir / "generate.py"
    code_path.write_text(
        "from tools import marker\n"
        "from pathlib import Path\n"
        "Path('livefigure.pptx').write_bytes(b'PK')\n"
        "assert marker == 'bundled'\n",
        encoding="utf-8",
    )
    pptx_path = output_dir / "livefigure.pptx"

    await _execute_code(code_path, output_dir, pptx_path)

    assert pptx_path.is_file()
