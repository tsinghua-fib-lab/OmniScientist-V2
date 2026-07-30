"""What a clarification may say, and whether asking was allowed at all.

Incident cff3eeda. A fully specified research request was answered with "I need a
little more information before continuing: research goal and output scope." The
planner had written something else entirely -- "a previous research task on the
same topic already succeeded; would you like to view the existing report or
re-run the research?" -- and every word of it was dropped on the way to the
screen. The user could not answer a question they were never shown, so they
retyped the same request; the second planning call chose ``react_fallback``,
retrieved the finished report, and the turn succeeded. The repeat is what
unblocked it, not an answer.

Two independent faults, both pinned here.

The renderer and the model were given different contracts. ``missing_inputs`` is
requested from the model as ``{field, reason}``, and the renderer displays only
``ask`` plus four hard-coded field names. Across this workspace's history not one
model-declared field matched that list, so every gap the model declared itself
rendered as the generic sentence -- while its ``reason``, which is where the
model actually puts the question, was discarded as an internal diagnostic. It is
an internal diagnostic only for gate gaps, which carry ``ask`` as well.

Asking was not the planner's to do. Both the system prompt and the recent
activity block tell it never to ask the user to re-describe finished work, and
the finished task was listed in front of it with its id. Instruction alone did
not hold: two calls on the same evidence went opposite ways. Retrieval is
decidable from the record, so the boundary decides it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omni.agent.intent_plan import IntentType
from omni.agent.model_planner import ModelIntentPlanner, ModelPlanProposal
from omni.agent.plan_pipeline import PlanPipeline
from omni.agent.plan_runner_utils import assumption_block, needs_input_text
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.core.llm.client import LLMClient
from omni.skills_runtime.registry import SkillRegistry

# The clarification the planner wrote for run cff3eeda, verbatim from its plan.
_REAL_QUESTION = (
    "A previous research task on the same topic already succeeded. "
    "Would you like to view the existing report or re-run the research?"
)
_REAL_REQUEST = "帮我调研如何利用隐空间干预的方式提升LLM的Agentic能力"


class _ScriptedLLM(LLMClient):
    """Planner double: returns one scripted proposal, no binder call."""

    def __init__(self, plan: dict) -> None:
        self.model = "scripted"
        self._plan = plan

    async def chat(self, system: str, user: str, **kwargs: Any) -> str:
        return json.dumps(self._plan, ensure_ascii=False)

    async def chat_with_tools(self, messages, tools, **kwargs: Any):  # noqa: ANN001, ANN201
        raise AssertionError("scripted planning uses chat only")

    async def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        return [[0.0] for _ in texts]


def _registry() -> SkillRegistry:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return registry


class _FinishedTask:
    """The shape of a task row the duplicate check reads."""

    def __init__(
        self,
        task_id: str,
        user_input: str,
        status: str = "succeeded",
        *,
        age_minutes: float = 0.0,
    ) -> None:
        self.id = task_id
        self.user_input = user_input
        self.status = status
        self.title = user_input
        self.kind = "turn"
        self.finished_at = datetime.now(UTC) - timedelta(minutes=age_minutes)
        self.created_at = self.finished_at


class _Recorder:
    """Task store double holding whatever history a test needs."""

    def __init__(self, finished: list[_FinishedTask] | None = None) -> None:
        self._finished = finished or []
        self.events: list[dict[str, Any]] = []

    async def list_tasks(self, **kwargs: Any) -> list[_FinishedTask]:
        status = kwargs.get("status")
        return [row for row in self._finished if status is None or row.status == status]

    async def append_event(self, task_id: str, **event: Any) -> None:
        self.events.append({"task_id": task_id, **event})


# ── The question the planner wrote is the question the user reads ──


def test_a_declared_gap_reaches_the_user_in_the_planners_own_words():
    """The model writes the question in ``reason``; that is where it must come from.

    Nothing else on a model-declared gap carries a question. Dropping ``reason``
    leaves a sentence that describes no gap the user can close.
    """
    rendered = needs_input_text(
        [{"field": "user_intent_clarification", "reason": _REAL_QUESTION}]
    )

    assert "existing report or re-run" in rendered
    assert "research goal and output scope" not in rendered


def test_a_field_name_nobody_anticipated_still_asks_something_answerable():
    """The four known field names are a nicety, not the only way through.

    Every field the model declared in this workspace's history was one nobody
    had listed: ``instruction``, ``paper_spec``, ``reference_code_paths``,
    ``original_request``, ``user_intent_clarification``.
    """
    for field_name, reason in (
        ("paper_spec", "写综述需要明确主题范围、拟投期刊与篇幅"),
        ("reference_code_paths", "To compare against the codex sources I need their paths."),
        ("instruction", "The user typed '123', which is too vague to interpret."),
    ):
        rendered = needs_input_text([{"field": field_name, "reason": reason}])
        assert reason in rendered, field_name


@pytest.mark.asyncio
async def test_the_contract_offers_the_model_somewhere_to_put_the_question():
    """A renderer that reads ``ask`` while the contract omits it can never work.

    ``ask`` was reachable only from the grounding gate, so no gap the model
    declared itself could ever carry one.
    """
    planner = ModelIntentPlanner(_ScriptedLLM({"intent_type": "react_fallback"}), _registry())
    await planner.propose("anything")

    shape = planner.last_system
    assert '"missing_inputs"' in shape
    gap_shape = shape.split('"missing_inputs"', 1)[1].split("]", 1)[0]
    assert '"ask"' in gap_shape


def test_an_internal_diagnostic_still_never_reaches_the_user():
    """A gate gap carries both, and only ``ask`` is written for a person.

    ``reason`` there names the fabricated value for the event log. Preferring
    ``ask`` keeps that split intact while ``reason`` becomes the fallback for
    gaps that have nothing else.
    """
    rendered = needs_input_text(
        [
            {
                "field": "input",
                "ask": "the file to use",
                "reason": "planner supplied file:///tmp/invented.pdf for input, "
                "which appears nowhere in the request",
                "source": "grounding_gate",
            }
        ]
    )

    assert "the file to use" in rendered
    assert "planner supplied" not in rendered
    assert "invented.pdf" not in rendered


def test_a_gap_that_says_nothing_is_not_a_question():
    """An empty gap is a plan fault, and the user is not the one who can fix it.

    Rather than print a sentence naming a gap that was never described, the
    turn falls through to the capable path the plan already nominates.
    """
    proposal = ModelPlanProposal(
        intent_type="needs_input",
        missing_inputs=[{"field": "something_unstated"}],
        rationale="the model chose to ask but described nothing",
    )

    plan = IntentPlanner(_registry()).plan_from_proposal(
        "帮我看看这个", proposal, task_id="empty-gap"
    )

    assert plan.intent_type is not IntentType.NEEDS_INPUT


def test_a_described_gap_is_still_a_question():
    """The fallthrough above must not swallow gaps that do say something."""
    proposal = ModelPlanProposal(
        intent_type="needs_input",
        missing_inputs=[{"field": "topic", "reason": "No subject was given."}],
        rationale="genuinely vague",
    )

    plan = IntentPlanner(_registry()).plan_from_proposal(
        "帮我做个研究", proposal, task_id="real-gap"
    )

    assert plan.intent_type is IntentType.NEEDS_INPUT
    assert "No subject was given." in needs_input_text(plan.missing_inputs)


# ── A gap the model can fill itself is not a reason to stop ──


def test_a_gap_with_a_default_is_assumed_rather_than_asked():
    """Codex's Default mode, transplanted: assume, execute, and say what you assumed.

    "strongly prefer making reasonable assumptions and executing the user's
    request rather than stopping to ask questions" — codex-rs
    collaboration-mode-templates/templates/default.md. The cost of asking is not
    the question, it is the stop: a Codex turn that asks anyway keeps running
    and auto-resolves in two minutes, while an Omni ``needs_input`` turn ends
    and the task waits for a person who may not come back. Of thirty such turns
    in this workspace, none was ever answered.
    """
    proposal = ModelPlanProposal(
        intent_type="needs_input",
        missing_inputs=[
            {
                "field": "output_format",
                "ask": "Which format should the summary be in?",
                "default": "markdown",
            }
        ],
        rationale="the user did not say what format they wanted",
    )

    plan = IntentPlanner(_registry()).plan_from_proposal(
        "总结一下这几篇论文", proposal, task_id="assumed"
    )

    assert plan.intent_type is not IntentType.NEEDS_INPUT


def test_an_assumption_is_carried_so_the_answer_can_declare_it():
    """Assuming silently is guessing; the user has to be able to see and correct it."""
    proposal = ModelPlanProposal(
        intent_type="needs_input",
        missing_inputs=[
            {
                "field": "output_format",
                "ask": "Which format should the summary be in?",
                "default": "markdown",
            }
        ],
        rationale="the user did not say what format they wanted",
    )
    plan = IntentPlanner(_registry()).plan_from_proposal(
        "总结一下这几篇论文", proposal, task_id="assumed"
    )
    plan.missing_inputs = list(proposal.missing_inputs)

    block = assumption_block(plan.missing_inputs)

    assert "output_format" in block
    assert "markdown" in block
    assert "state" in block.casefold()


def test_a_gap_with_no_safe_default_still_stops_and_asks():
    """Assumption-first is not assumption-always.

    A value nobody can guess is the case the ask exists for, and Codex keeps it
    too: "If you absolutely must ask a question because the answer cannot be
    discovered from local context and a reasonable assumption would be risky,
    ask the user directly."
    """
    proposal = ModelPlanProposal(
        intent_type="needs_input",
        missing_inputs=[
            {"field": "paper_spec", "ask": "Which paper should I review?"},
            {"field": "output_format", "ask": "Which format?", "default": "markdown"},
        ],
        rationale="no paper was named",
    )

    plan = IntentPlanner(_registry()).plan_from_proposal(
        "帮我评审一下", proposal, task_id="no-default"
    )

    assert plan.intent_type is IntentType.NEEDS_INPUT
    assert "Which paper should I review?" in needs_input_text(plan.missing_inputs)


@pytest.mark.asyncio
async def test_the_contract_offers_the_model_somewhere_to_put_a_default():
    """A field the model is never told about is one it will never fill in."""
    planner = ModelIntentPlanner(_ScriptedLLM({"intent_type": "react_fallback"}), _registry())
    await planner.propose("anything")

    shape = planner.last_system.split("JSON shape:", 1)[1]
    gap_shape = shape.split('"missing_inputs"', 1)[1].split("]", 1)[0]
    assert '"default"' in gap_shape


# ── Asking about finished work is not the boundary's to do ──


@pytest.mark.asyncio
async def test_repeating_a_finished_request_runs_again_instead_of_asking():
    """A repeat is a new turn. Nobody is asked; the earlier id is only a hint.

    The prompt says never to ask, and the model still asked. The boundary
    decides from the record: run again (Codex / Claude Code), and if the twin
    is fresh, name it for the user — not for the model.
    """
    recorder = _Recorder([_FinishedTask("3f1fc56d", _REAL_REQUEST)])
    pipeline = PlanPipeline(
        settings=load_settings(),
        registry=_registry(),
        tasks=recorder,
        hooks=None,
    )
    scripted = _ScriptedLLM(
        {
            "intent_type": "needs_input",
            "missing_inputs": [
                {"field": "user_intent_clarification", "reason": _REAL_QUESTION}
            ],
            "rationale": "the user repeats a research task that has already been completed",
        }
    )

    plan, events, _ = await pipeline._produce_plan(  # noqa: SLF001
        planner=IntentPlanner(_registry()),
        llm=scripted,
        user_message=_REAL_REQUEST,
        task_id="cff3eeda",
        mode="auto",
        approved_plan=None,
        turn_context=None,
        context_summary="",
        recent_activity="",
    )

    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert "retriev" not in plan.rationale
    assert "3f1fc56d" in json.dumps(events, default=str)
    assert [event["event_type"] for event in events if "preferred" in event["event_type"]] == [
        "plan.rerun.preferred"
    ]
    assert any("/task show 3f1fc56d" in note for note in plan.user_notices)
    assert plan.twin_task_id == "3f1fc56d"
    assert plan.degraded_warnings == []


@pytest.mark.asyncio
async def test_a_first_time_request_is_still_allowed_to_ask():
    """No finished twin, no override: the model's question stands."""
    pipeline = PlanPipeline(
        settings=load_settings(),
        registry=_registry(),
        tasks=_Recorder([_FinishedTask("aaaaaaaa", "something else entirely")]),
        hooks=None,
    )
    scripted = _ScriptedLLM(
        {
            "intent_type": "needs_input",
            "missing_inputs": [{"field": "topic", "reason": "No subject was given."}],
            "rationale": "genuinely vague",
        }
    )

    plan, _, _ = await pipeline._produce_plan(  # noqa: SLF001
        planner=IntentPlanner(_registry()),
        llm=scripted,
        user_message="帮我做个研究",
        task_id="fresh",
        mode="auto",
        approved_plan=None,
        turn_context=None,
        context_summary="",
        recent_activity="",
    )

    assert plan.intent_type is IntentType.NEEDS_INPUT


@pytest.mark.asyncio
async def test_an_assumed_gap_survives_planning_so_the_turn_can_declare_it():
    """The whole point of assuming is lost if the assumption never travels.

    The planner drops a defaulted gap on the floor once it decides to execute:
    every builder past that branch constructs its plan from capabilities, not
    from ``missing_inputs``. Carrying it here is what lets the orchestrator put
    an assumptions block in front of the model, and the model put the same
    words in front of the user.
    """
    pipeline = PlanPipeline(
        settings=load_settings(), registry=_registry(), tasks=_Recorder(), hooks=None
    )
    scripted = _ScriptedLLM(
        {
            "intent_type": "needs_input",
            "missing_inputs": [
                {
                    "field": "output_format",
                    "ask": "Which format should the summary be in?",
                    "default": "markdown",
                }
            ],
            "rationale": "no format was given, markdown is the sensible choice",
        }
    )

    plan, _ = await pipeline._plan_with_model(  # noqa: SLF001
        IntentPlanner(_registry()),
        llm=scripted,
        user_message="总结一下这几篇论文",
        task_id="carried",
        turn_context=None,
        context_summary="",
    )

    assert plan.intent_type is not IntentType.NEEDS_INPUT
    assert [gap.get("field") for gap in plan.missing_inputs] == ["output_format"]
    assert "markdown" in assumption_block(plan.missing_inputs)


@pytest.mark.asyncio
async def test_a_stale_answer_is_run_again_rather_than_handed_back():
    """A twin outside the window is still a repeat, not a question.

    The turn runs again either way; only a fresh twin is worth naming to the user.
    """
    stale = _Recorder([_FinishedTask("3f1fc56d", _REAL_REQUEST, age_minutes=180)])
    pipeline = PlanPipeline(
        settings=load_settings(), registry=_registry(), tasks=stale, hooks=None
    )
    scripted = _ScriptedLLM(
        {
            "intent_type": "needs_input",
            "missing_inputs": [
                {"field": "user_intent_clarification", "reason": _REAL_QUESTION}
            ],
            "rationale": "the user repeats a research task that has already been completed",
        }
    )

    plan, events, _ = await pipeline._produce_plan(  # noqa: SLF001
        planner=IntentPlanner(_registry()),
        llm=scripted,
        user_message=_REAL_REQUEST,
        task_id="stale-twin",
        mode="auto",
        approved_plan=None,
        turn_context=None,
        context_summary="",
        recent_activity="",
    )

    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert "retriev" not in plan.rationale
    assert [event["event_type"] for event in events if "preferred" in event["event_type"]] == [
        "plan.rerun.preferred"
    ]
    assert "3f1fc56d" in json.dumps(events, default=str)
    assert plan.user_notices == []


@pytest.mark.asyncio
async def test_an_unfinished_twin_does_not_count_as_an_answer():
    """Only a *succeeded* run has something to retrieve.

    A failed or still-waiting twin would send the turn off to retrieve a result
    that does not exist.
    """
    pipeline = PlanPipeline(
        settings=load_settings(),
        registry=_registry(),
        tasks=_Recorder([_FinishedTask("bbbbbbbb", _REAL_REQUEST, status="failed")]),
        hooks=None,
    )
    scripted = _ScriptedLLM(
        {
            "intent_type": "needs_input",
            "missing_inputs": [
                {"field": "user_intent_clarification", "reason": _REAL_QUESTION}
            ],
            "rationale": "already attempted",
        }
    )

    plan, _, _ = await pipeline._produce_plan(  # noqa: SLF001
        planner=IntentPlanner(_registry()),
        llm=scripted,
        user_message=_REAL_REQUEST,
        task_id="failed-twin",
        mode="auto",
        approved_plan=None,
        turn_context=None,
        context_summary="",
        recent_activity="",
    )

    assert plan.intent_type is IntentType.NEEDS_INPUT
    assert plan.user_notices == []


@pytest.mark.asyncio
async def test_a_direct_execute_plan_still_hints_at_a_fresh_twin():
    """The hint is not gated on the planner asking first."""
    recorder = _Recorder([_FinishedTask("3f1fc56d", _REAL_REQUEST)])
    pipeline = PlanPipeline(
        settings=load_settings(),
        registry=_registry(),
        tasks=recorder,
        hooks=None,
    )
    scripted = _ScriptedLLM(
        {"intent_type": "react_fallback", "rationale": "do the research"}
    )

    plan, events, _ = await pipeline._produce_plan(  # noqa: SLF001
        planner=IntentPlanner(_registry()),
        llm=scripted,
        user_message=_REAL_REQUEST,
        task_id="direct-twin",
        mode="auto",
        approved_plan=None,
        turn_context=None,
        context_summary="",
        recent_activity="",
    )

    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert not any("preferred" in event["event_type"] for event in events)
    assert any("/task show 3f1fc56d" in note for note in plan.user_notices)
    assert plan.twin_task_id == "3f1fc56d"
    assert "retriev" not in plan.rationale


@pytest.mark.asyncio
async def test_a_zero_retrieval_window_disables_the_hint_but_still_reruns():
    settings = load_settings()
    settings.planner.retrieval_window_minutes = 0
    recorder = _Recorder([_FinishedTask("3f1fc56d", _REAL_REQUEST)])
    pipeline = PlanPipeline(
        settings=settings, registry=_registry(), tasks=recorder, hooks=None
    )
    scripted = _ScriptedLLM(
        {
            "intent_type": "needs_input",
            "missing_inputs": [
                {"field": "user_intent_clarification", "reason": _REAL_QUESTION}
            ],
            "rationale": "already done",
        }
    )

    plan, events, _ = await pipeline._produce_plan(  # noqa: SLF001
        planner=IntentPlanner(_registry()),
        llm=scripted,
        user_message=_REAL_REQUEST,
        task_id="window-off",
        mode="auto",
        approved_plan=None,
        turn_context=None,
        context_summary="",
        recent_activity="",
    )

    assert plan.intent_type is IntentType.REACT_FALLBACK
    assert [event["event_type"] for event in events if "preferred" in event["event_type"]] == [
        "plan.rerun.preferred"
    ]
    assert plan.user_notices == []


def test_identical_twin_notice_is_english_for_any_request_language():
    from omni.agent.plan_pipeline import identical_twin_notice

    chinese = identical_twin_notice("3f1fc56d" + "0" * 24, _REAL_REQUEST)
    english = identical_twin_notice("3f1fc56d" + "0" * 24, "Write a RAG survey")
    expected = (
        "An identical request succeeded recently as `3f1fc56d`. "
        "This turn is producing a new result; `/task show 3f1fc56d` opens the earlier one."
    )
    assert chinese == expected
    assert english == expected


def test_twin_notice_is_a_footnote_not_a_degraded_warning():
    from omni.runtime.presentation import TurnPresentation

    md = TurnPresentation(
        assistant_text="Here is the new survey.",
        user_notices=[
            "An identical request succeeded recently as `3f1fc56d`. "
            "This turn is producing a new result; `/task show 3f1fc56d` opens the earlier one."
        ],
        degraded_warnings=["Still missing deliverables: artifact.figure"],
    ).to_markdown()
    assert "Here is the new survey." in md
    assert "/task show 3f1fc56d" in md
    assert md.index("/task show 3f1fc56d") < md.index("**Degraded execution**")


def test_im_keeps_the_twin_notice_and_does_not_call_it_degraded():
    from omni.agent.turn_execution import TurnResult
    from omni.runtime.presentation import turn_presentation_from_result

    turn = TurnResult(
        text="New survey.",
        session_id="s",
        task_id="n" * 32,
        user_notices=[
            "An identical request succeeded recently as `3f1fc56d`. "
            "This turn is producing a new result; `/task show 3f1fc56d` opens the earlier one."
        ],
    )
    md = turn_presentation_from_result(turn, channel="wechat").to_markdown()
    assert "/task show 3f1fc56d" in md
    assert "**Degraded execution**" not in md
