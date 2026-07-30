"""Acceptance scenarios: five real research runs that used to end badly.

Each test below is a scenario a researcher actually hits, written against the
seam where the old behaviour went wrong. They are deliberately outcome-shaped
(what the user is handed at the end) rather than implementation-shaped, so they
keep their meaning as the internals move.

Scenario map — defect → research situation:

1. ``blank tool name``      reviewing an arXiv paper with a fast model whose
                            tool calls carry no function name
2. ``iteration ceiling``    a systematic review that runs out of turns
                            mid-sweep
3. ``silent workflow``      a multi-step RAG-survey pipeline whose figure step
                            fails
4. ``internal as question`` asking for a capability no installed skill provides
5. ``invented tool names``  a data-analysis run where the model keeps calling
                            tools that do not exist
6. ``capped deliverable``   a long review whose section files outnumber the
                            manifest's write quota
"""

from __future__ import annotations

import httpx
import pytest

from omni.config import load_settings
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.termination import base_termination_reason
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.executor import execute_skill
from omni.skills_runtime.manifest import DeliveryMode, SkillEntry, SkillKind
from tests.conftest import ScriptedLLM


def _ctx(**kw):
    settings = load_settings()
    settings.paths.ensure_dirs()
    return ExecContext(settings=settings, paths=settings.paths, **kw)


def _event_tool_name(data: dict) -> str:
    """Read a tool name off a progress payload the way a renderer must.

    Spelled out here rather than imported so these scenarios can be replayed
    against any revision, including ones predating the shared accessor.
    """
    return str(data.get("name") or data.get("tool") or "").strip()


def _prompt_skill(name: str, body: str, *, max_iterations: int, max_tool_calls: int):
    return SkillEntry(
        name=name,
        description=f"{name} acceptance scenario",
        kind=SkillKind.PROMPT_ONLY,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        body=body,
        execution={"max_iterations": max_iterations, "max_tool_calls": max_tool_calls},
    )


# --------------------------------------------------------------------------
# Scenario 1 — reviewing an arXiv paper with a model that emits nameless calls
# --------------------------------------------------------------------------


class _NamelessCallModel:
    """A fast model whose tool calls arrive with an empty function name.

    Real deployments of small/fast chat models do this: the delta carries
    arguments but never a name. The transport used to admit the call anyway.
    """

    instance: _NamelessCallModel | None = None

    def __init__(self, **_kwargs) -> None:  # noqa: ANN003
        self.tool_turns = 0
        self.synthesis_turns = 0
        _NamelessCallModel.instance = self

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args) -> None:  # noqa: ANN002
        return None

    async def post(self, url: str, *, json=None, **_kwargs) -> httpx.Response:  # noqa: ANN001, ANN003
        payload = json or {}
        request = httpx.Request("POST", url)
        if not payload.get("tools"):
            # The no-tool finalisation pass: the model can still write prose.
            self.synthesis_turns += 1
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "Review of 'Attention Is All You Need': the paper "
                                    "replaces recurrence with self-attention, enabling "
                                    "parallel training. I could not open the PDF, so "
                                    "this is from prior knowledge."
                                )
                            }
                        }
                    ]
                },
            )
        self.tool_turns += 1
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": f"call-{self.tool_turns}",
                                    "function": {
                                        "name": "",
                                        "arguments": '{"path": "attention.pdf"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )


@pytest.mark.asyncio
async def test_paper_review_with_a_nameless_tool_call_still_answers(monkeypatch):
    """A researcher reviews a paper on a model with broken tool-call naming.

    Before: every nameless call was admitted, rendered as ``↳ ⚙ ?``, came back
    as ``unknown tool ''`` the model could not act on, and burned the whole
    iteration budget for a stub. After: the transport drops what has no
    identity, the model is told once to re-issue with a name, and the run ends
    with a real review plus a named reason for the degradation.
    """
    from omni.core.llm import providers
    from omni.core.llm.providers import OpenAICompatibleProvider

    monkeypatch.setattr(providers.httpx, "AsyncClient", _NamelessCallModel)
    llm = OpenAICompatibleProvider(
        base_url="https://models.invalid/v1", api_key="k", model="fast-model"
    )

    events: list[tuple[str, dict]] = []

    async def on_progress(stage: str, _pct, **data) -> None:  # noqa: ANN001
        events.append((stage, data))

    entry = _prompt_skill(
        "paper-review",
        "Review the attached paper.",
        max_iterations=4,
        max_tool_calls=8,
    )
    out = await execute_skill(
        entry,
        {"paper": "arXiv:1706.03762"},
        _ctx(llm=llm),
        progress_callback=on_progress,
    )

    # The user gets a real review, not a placeholder.
    assert "self-attention" in out["text"]
    assert not out["text"].startswith("Partial result:")

    # The reason names what actually went wrong, and is not silence.
    assert base_termination_reason(out["terminated_reason"]) == "malformed_tool_calls"

    # Nothing nameless ever reached the display layer.
    tool_events = [data for stage, data in events if stage.startswith("tool")]
    assert all(_event_tool_name(data) for data in tool_events)

    # The budget was not spent re-asking a question the model cannot answer.
    assert _NamelessCallModel.instance is not None
    assert _NamelessCallModel.instance.tool_turns <= 3


# --------------------------------------------------------------------------
# Scenario 2 — a systematic review that runs out of turns mid-sweep
# --------------------------------------------------------------------------


class _SurveyingModel(ScriptedLLM):
    """Keeps recording notes, one source per turn, and never volunteers a stop.

    This is the shape of a real literature sweep: each turn makes genuine
    progress, so no loop-detector fires — the run simply meets its ceiling.
    """

    def __init__(self, project_dir) -> None:  # noqa: ANN001
        super().__init__()
        self.model = "surveying"
        self._project_dir = project_dir
        self.turn = 0

    async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if not tools:
            return ChatWithToolsResult(
                content=(
                    "Interim synthesis across the 4 sources read so far: "
                    "retrieval-augmented pipelines converge on a query/retriever/"
                    "reranker/reader decomposition."
                )
            )
        self.turn += 1
        return ChatWithToolsResult(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"note-{self.turn}",
                    name="write_file",
                    arguments={
                        "path": str(self._project_dir / f"note_{self.turn}.md"),
                        "content": f"source {self.turn}",
                    },
                )
            ],
        )


@pytest.mark.asyncio
async def test_systematic_review_that_hits_the_ceiling_hands_back_its_findings():
    """A survey too large for one budget must still deliver what it gathered.

    Before: hitting the iteration ceiling produced a canned ``Partial result:``
    stub, and the run was advertised as retryable — re-running it with the same
    budget would hit the same wall. After: the loop spends one final no-tool
    turn writing up what it found, reports ``partial``, and names the flag that
    actually widens the ceiling.
    """
    ctx = _ctx()
    ctx.llm = _SurveyingModel(ctx.paths.project_dir)

    entry = _prompt_skill(
        "literature-survey",
        "Survey the retrieval-augmented generation literature.",
        max_iterations=4,
        max_tool_calls=8,
    )
    out = await execute_skill(entry, {"topic": "RAG"}, ctx)

    # What was gathered comes back as prose, not as a stub.
    assert "retriever" in out["text"]
    assert not out["text"].startswith("Partial result:")

    # Truthfully labelled: cut short, so not a clean success.
    assert out["status"] == "partial"
    assert base_termination_reason(out["terminated_reason"]) == "max_iterations"

    # An exhausted budget is not retryable at the same budget, and the user is
    # told which knob actually changes the outcome.
    assert out["error_info"]["retryable"] is False
    assert "max_iterations" in (out.get("next_action") or "")


# --------------------------------------------------------------------------
# Scenario 3 — a multi-step RAG-survey pipeline whose figure step fails
# --------------------------------------------------------------------------


def test_failed_figure_step_tells_the_researcher_why():
    """A pipeline step failed; the reason must survive to the terminal.

    Before: the workflow line printed ``✗ workflow failed`` with an id and a
    duration — the ``error`` carried on the event was dropped, so the only way
    to learn the cause was to go digging in the database. After: the reason the
    step gave is what the researcher reads.
    """
    from omni.cli.live_display import TurnDisplay, console

    display = TurnDisplay(verbosity="normal", status_line=False)
    with console.capture() as capture:
        display.tool_event(
            "task_done",
            {
                "workflow_run_id": "rag-survey-0001",
                "status": "failed",
                "error": "figure step: graphviz executable not found on PATH",
            },
        )
        display.end()

    # Collapse the terminal's soft wrapping before matching.
    rendered = " ".join(capture.get().split())
    assert "graphviz executable not found on PATH" in rendered


# --------------------------------------------------------------------------
# Scenario 4 — asking for a capability no installed skill provides
# --------------------------------------------------------------------------


def test_unavailable_capability_is_not_dressed_up_as_a_question():
    """Ask for protein structure prediction with no such skill installed.

    Before: the planner failed to bind a provider and returned a ``needs_input``
    plan whose question text was the internal diagnostic — the researcher was
    asked to clarify a sentence about "contracted providers" that no answer
    could satisfy. After: an unbindable capability falls through to the general
    assistant, which can at least respond.
    """
    from omni.agent.intent_plan import IntentType
    from omni.agent.model_planner import ModelPlanProposal
    from omni.agent.plan_runner_utils import needs_input_text
    from omni.agent.planner import IntentPlanner
    from omni.skills_runtime.registry import SkillRegistry

    plan = IntentPlanner(SkillRegistry(load_settings())).plan_from_proposal(
        "predict the folded structure of this sequence",
        ModelPlanProposal(
            intent_type="single_skill_task",
            confidence=0.7,
            required_capabilities=["protein.structure.prediction"],
            rationale="model proposed a capability nothing provides",
        ),
        task_id="fold-1",
    )

    # Not a question — the system's own gap is not the user's to fill.
    assert plan.intent_type is not IntentType.NEEDS_INPUT
    assert not plan.missing_inputs

    # And the diagnostic vocabulary never reaches a user-facing prompt.
    question = needs_input_text(
        [{"field": "capability", "reason": "no executable contracted provider matched"}]
    )
    assert "contracted provider" not in question


# --------------------------------------------------------------------------
# Scenario 5 — a data-analysis run where the model invents tool names
# --------------------------------------------------------------------------


class _ToolInventingModel(ScriptedLLM):
    """Reaches for a plausible-sounding analysis tool that does not exist.

    A different name every turn, so each call looks like fresh progress to any
    repetition detector — the failure is the *kind* of error, not its shape.
    """

    def __init__(self) -> None:
        super().__init__()
        self.model = "inventing"
        self.turn = 0

    async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if not tools:
            return ChatWithToolsResult(
                content=(
                    "I could not run the statistics tools I expected to have; "
                    "here is the analysis plan instead."
                )
            )
        self.turn += 1
        return ChatWithToolsResult(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"inv-{self.turn}",
                    name=f"run_anova_v{self.turn}",
                    arguments={"data": "measurements.csv"},
                )
            ],
        )


@pytest.mark.asyncio
async def test_invented_analysis_tools_stop_the_run_early():
    """The model keeps calling analysis tools that were never offered.

    Before: each invented name was a fresh signature, so the no-progress
    detector never fired and the run burned its entire budget rediscovering
    that the tool does not exist. After: repeated model-fault rejections are
    counted as their own failure mode and stop the run well short of the
    ceiling, with an answer rather than nothing.
    """
    ctx = _ctx(llm=_ToolInventingModel())

    entry = _prompt_skill(
        "stats-analysis",
        "Analyse the measurement table.",
        max_iterations=20,
        max_tool_calls=40,
    )
    out = await execute_skill(entry, {"table": "measurements.csv"}, ctx)

    # Stopped early instead of spending twenty turns on the same discovery.
    assert out["total_iterations"] < 10

    # And still returned something usable, under a reason the user can read.
    assert "analysis plan" in out["text"]
    assert out["terminated_reason"]


# --------------------------------------------------------------------------
# Scenario 6 — a review with more sections than the manifest allowed writes
# --------------------------------------------------------------------------


def test_a_long_review_can_write_every_section_it_produced():
    """A thorough review of a dense paper runs to fifteen section files.

    Before: ``paper-review`` shipped a manifest that capped ``write_file`` at
    ten, so sections eleven onward were refused — and each refusal was still
    billed to the shared tool budget, so the run also died sooner. The reviewer
    was handed a review missing its last third.

    After: the number of sections is decided by the paper, while the caps that
    represent real cost still bite. That is now a property of every shipped
    budget rather than of one skill's manifest, which is what keeps it true —
    ``paper-review`` has since become a python engine and consults no tool
    budget at all, so pinning the guarantee to its manifest would have retired
    the guarantee along with the cap.
    """
    from omni.config import load_settings
    from omni.core.tool_policy import ToolPolicyGuard
    from omni.skills_runtime.registry import SkillRegistry

    registry = SkillRegistry(load_settings())
    registry.build_index()
    budgeted = [
        entry for entry in registry.list_all() if (entry.execution or {}).get("tool_limits")
    ]
    assert budgeted, "expected at least one shipped skill to declare a tool budget"

    def _guard(execution: dict) -> ToolPolicyGuard:
        return ToolPolicyGuard(
            max_tool_calls=int(execution.get("max_tool_calls") or 0) or None,
            per_tool_limits=execution["tool_limits"],
        )

    for entry in budgeted:
        execution = entry.execution or {}

        writing = _guard(execution)
        for section in range(1, 16):
            assert writing.rejection("write_file") is None, (
                f"{entry.name} refused section {section}"
            )

        # And every cap the manifest does declare is a real bound, not decoration.
        for tool, cap in sorted(execution["tool_limits"].items()):
            acquiring = _guard(execution)
            for _ in range(int(cap)):
                assert acquiring.rejection(tool) is None
            refused = acquiring.rejection(tool)
            assert refused is not None, f"{entry.name} does not enforce {tool}<={cap}"
            assert refused["remedy"], "an enforced cap must still offer a way to conclude"
