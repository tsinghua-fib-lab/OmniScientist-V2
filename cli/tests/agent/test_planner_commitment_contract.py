"""What the planning boundary is allowed to commit on the user's behalf.

Incident dc787efa: a pasted review excerpt was routed to ``paper-review`` as a
``single_skill_task`` and ran for 407k tokens before settling ``degraded`` at the
iteration ceiling. Two mechanisms in the boundary made that possible.

``ask`` was advertised and discarded. The planner prompt offers the model
``execution_mode: "react|background|foreground|ask|direct"``, but the
single-capability route accepted only ``background``/``foreground`` and folded
everything else into ``background``. A model that hesitated had no way to say so:
the one channel built for "check with the user first" was wired to nothing.

Locators were invented. A planner-supplied ``https://`` or ``file://`` value that
appears nowhere in the request is not resolution — it is a claim that a specific
address holds the user's material, and following it fetches something nobody
asked for.

The gate stops at locators on purpose. Resolving a *named* work to its canonical
identifier (title -> ``1706.03762``) is legitimate and separately contracted in
``test_ask_last_planning.py``; the last test here pins that boundary so this one
cannot creep into it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omni.agent.intent_plan import IntentType
from omni.agent.model_planner import ModelIntentPlanner, ModelPlanProposal
from omni.agent.plan_runner_utils import needs_input_text
from omni.agent.planner import IntentPlanner
from omni.config import load_settings
from omni.core.llm.client import LLMClient
from omni.skills_runtime.registry import SkillRegistry


class _ScriptedLLM(LLMClient):
    """Planner double: returns one scripted proposal, no binder call."""

    def __init__(self, plan: dict) -> None:
        self.model = "scripted"
        self._plan = plan

    async def chat(self, system: str, user: str, **kwargs: Any) -> str:
        return json.dumps(self._plan, ensure_ascii=False)

    async def chat_with_tools(self, messages, tools, **kwargs: Any):  # noqa: ANN001, ANN201 # pragma: no cover
        raise AssertionError("scripted planning uses chat only")

    async def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        return [[0.0] for _ in texts]


def _registry() -> SkillRegistry:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    return registry


# ── The model may choose to ask ──


def test_choosing_to_ask_produces_a_question_not_a_background_run():
    """``execution_mode: "ask"`` is an intent the model chose, not a hint."""
    proposal = ModelPlanProposal(
        intent_type="single_skill_task",
        required_capabilities=["literature.search"],
        execution_mode="ask",
        rationale="the request reads like pasted notes rather than a task",
    )

    plan = IntentPlanner(_registry()).plan_from_proposal(
        "Review of 'Attention Is All You Need': the paper replaces recurrence "
        "with self-attention. I could not open the PDF, so this is from prior knowledge.",
        proposal,
        task_id="run-ask",
    )

    assert plan.intent_type == IntentType.NEEDS_INPUT
    assert plan.execution_mode == "ask"


def test_asking_without_a_named_gap_still_reaches_the_user_as_a_question():
    """A hesitating model owes the user a question, not an internal diagnostic."""
    proposal = ModelPlanProposal(
        intent_type="single_skill_task",
        required_capabilities=["literature.search"],
        execution_mode="ask",
        rationale="ambiguous whether this is a request or a paste",
    )

    plan = IntentPlanner(_registry()).plan_from_proposal(
        "Review of 'Attention Is All You Need'.", proposal, task_id="run-ask-bare"
    )
    question = needs_input_text(plan.missing_inputs)

    assert plan.intent_type == IntentType.NEEDS_INPUT
    # The engineering phrasing of the gap is for the event log, never the user.
    assert "planner" not in question.lower()
    assert "executable intent" not in question.lower()
    assert question.strip().endswith(".")


def test_choosing_to_ask_outranks_a_bindable_capability():
    """``ask`` is not overridden just because the plan happens to be runnable."""
    proposal = ModelPlanProposal(
        intent_type="single_skill_task",
        required_capabilities=["literature.search"],
        capability_inputs={"literature.search": {"topic": "retrieval augmented generation"}},
        execution_mode="ask",
        rationale="unsure the user wants this run now",
    )

    plan = IntentPlanner(_registry()).plan_from_proposal(
        "RAG 综述的材料", proposal, task_id="run-ask-bindable"
    )

    assert plan.intent_type == IntentType.NEEDS_INPUT


# ── The planner may not invent a locator ──


@pytest.mark.asyncio
async def test_an_invented_url_is_not_committed_as_the_users_source():
    payload = {
        "intent_type": "single_skill_task",
        "confidence": 0.95,
        "required_capabilities": ["paper.review"],
        "capability_inputs": {"paper.review": {"source_url": "https://arxiv.org/abs/1706.03762"}},
        "outputs": ["answer"],
        "rationale": "review the named paper",
    }
    goal = "Review of 'Attention Is All You Need'. I could not open the PDF."

    proposal = await ModelIntentPlanner(_ScriptedLLM(payload), _registry()).propose(goal)

    assert proposal is not None
    assert "source_url" not in proposal.capability_inputs.get("paper.review", {})
    gap = next(item for item in proposal.missing_inputs if item.get("field") == "source_url")
    # The user gets a question; the fabricated value stays in the diagnostic.
    assert gap.get("ask")
    assert "https://arxiv.org/abs/1706.03762" in str(gap.get("reason"))


@pytest.mark.asyncio
async def test_a_url_the_user_supplied_is_used_as_given():
    url = "https://arxiv.org/abs/1706.03762"
    payload = {
        "intent_type": "single_skill_task",
        "confidence": 0.9,
        "required_capabilities": ["paper.review"],
        "capability_inputs": {"paper.review": {"source_url": url}},
        "outputs": ["answer"],
        "rationale": "review the linked paper",
    }

    proposal = await ModelIntentPlanner(_ScriptedLLM(payload), _registry()).propose(
        f"Please review {url}"
    )

    assert proposal is not None
    assert proposal.capability_inputs["paper.review"]["source_url"] == url
    assert proposal.missing_inputs == []


@pytest.mark.asyncio
async def test_a_url_carried_by_the_turn_context_counts_as_supplied():
    """Context resolves "this paper"; a value grounded there is not invented."""
    url = "https://arxiv.org/abs/2005.11401"
    payload = {
        "intent_type": "single_skill_task",
        "confidence": 0.9,
        "required_capabilities": ["paper.review"],
        "capability_inputs": {"paper.review": {"source_url": url}},
        "outputs": ["answer"],
        "rationale": "review the paper in focus",
    }

    proposal = await ModelIntentPlanner(_ScriptedLLM(payload), _registry()).propose(
        "review this paper", context_summary=f"active paper: {url}"
    )

    assert proposal is not None
    assert proposal.capability_inputs["paper.review"]["source_url"] == url


@pytest.mark.asyncio
async def test_an_invented_file_path_is_not_committed_as_the_users_source():
    payload = {
        "intent_type": "single_skill_task",
        "confidence": 0.9,
        "required_capabilities": ["paper.review"],
        "capability_inputs": {"paper.review": {"source": "file:///Users/someone/attention.pdf"}},
        "outputs": ["answer"],
        "rationale": "review the local pdf",
    }

    proposal = await ModelIntentPlanner(_ScriptedLLM(payload), _registry()).propose(
        "Review of 'Attention Is All You Need'. I could not open the PDF."
    )

    assert proposal is not None
    assert "source" not in proposal.capability_inputs.get("paper.review", {})


@pytest.mark.asyncio
async def test_an_invented_locator_inside_a_workflow_step_is_also_refused():
    payload = {
        "intent_type": "workflow",
        "confidence": 0.9,
        "workflow_steps": [
            {
                "id": "fetch",
                "capability": "paper.fetch.arxiv",
                "input": {"identifier": "1706.03762", "pdf_url": "https://example.com/made-up.pdf"},
            },
        ],
        "outputs": ["workflow"],
        "rationale": "fetch then review",
    }

    proposal = await ModelIntentPlanner(_ScriptedLLM(payload), _registry()).propose(
        "获取 Attention Is All You Need 摘要"
    )

    assert proposal is not None
    step = proposal.workflow_steps[0]
    assert "pdf_url" not in step["input"]
    # The canonical identifier resolved from the title is untouched.
    assert step["input"]["identifier"] == "1706.03762"


# ── Refusing a value is a question, not a deletion ──


def _incident_138c7b6e() -> dict:
    """The proposal that produced the run this section exists for.

    The model read the user's *statement* ("I could not open the PDF, so this is
    from prior knowledge") as a *request* to fetch and review, then invented both
    an address to fetch from and a filename to review.
    """
    return {
        "intent_type": "workflow",
        "confidence": 0.95,
        "required_capabilities": ["review.paper", "react_fallback"],
        "workflow_steps": [
            {
                "id": "step1",
                "capability": "react_fallback",
                "depends_on": [],
                "input": {"prompt": "download https://arxiv.org/pdf/1706.03762.pdf"},
                "reason": "Retrieve the full paper PDF from arXiv.",
            },
            {
                "id": "step2",
                "capability": "review.paper",
                "depends_on": ["step1"],
                "input": {"input": "attention_is_all_you_need.pdf", "mode": "standard"},
                "reason": "Perform a comprehensive review of the downloaded paper.",
            },
        ],
        "capability_inputs": {
            "review.paper": {"input": "attention_is_all_you_need.pdf", "mode": "standard"}
        },
        "outputs": ["review_report"],
        "rationale": "They cannot provide the PDF, so the system must fetch it.",
    }


_INCIDENT_GOAL = (
    "Review of 'Attention Is All You Need': the paper replaces recurrence with "
    "self-attention, enabling parallel training. I could not open the PDF, so "
    "this is from prior knowledge."
)


@pytest.mark.asyncio
async def test_refusing_an_invented_value_asks_instead_of_leaving_a_hole():
    """Run 138c7b6e: the gate fired, and its firing is what broke the run.

    ``_ground_locators`` removed the fabricated ``https://arxiv.org/pdf/...`` and
    recorded a gap, but the plan continued because the ask-last rule only asks
    when there are no steps. That rule is right for a gap the *model* declared —
    the recovery ladder can often find such a value — and wrong for a gap the
    *gate* created, which is not a value anyone can discover. Deleting a field
    and proceeding is strictly worse than never checking: it converts "this
    address is wrong" into "there is no address", and nothing downstream can
    tell the difference.
    """
    planner = ModelIntentPlanner(_ScriptedLLM(_incident_138c7b6e()), _registry())
    proposal = await planner.propose(_INCIDENT_GOAL)
    assert proposal is not None

    plan = IntentPlanner(_registry()).plan_from_proposal(
        _INCIDENT_GOAL, proposal, task_id="run-138c7b6e"
    )

    assert plan.intent_type == IntentType.NEEDS_INPUT
    # The diagnostic explains the refusal to the log, not to the user.
    assert "planner supplied" not in needs_input_text(plan.missing_inputs)


@pytest.mark.asyncio
async def test_a_gate_gap_asks_even_when_the_rest_of_the_plan_would_bind():
    payload = {
        "intent_type": "workflow",
        "confidence": 0.9,
        "workflow_steps": [
            {
                "id": "fetch",
                "capability": "paper.fetch.arxiv",
                "input": {"identifier": "1706.03762", "pdf_url": "https://example.com/made-up.pdf"},
            },
        ],
        "outputs": ["workflow"],
        "rationale": "fetch the paper",
    }
    planner = ModelIntentPlanner(_ScriptedLLM(payload), _registry())
    proposal = await planner.propose("获取 Attention Is All You Need 摘要")
    assert proposal is not None

    plan = IntentPlanner(_registry()).plan_from_proposal(
        "获取 Attention Is All You Need 摘要", proposal, task_id="run-gate-binds"
    )

    assert plan.intent_type == IntentType.NEEDS_INPUT


def test_a_gap_the_model_declared_still_defers_to_the_recovery_ladder():
    """The ask-last rule is preserved for everything the gate did not touch.

    A model-declared gap names a value the agent may well be able to discover
    (rerouting to ``literature.search``, reading the working directory), so it
    must not short-circuit an executable plan into a question.
    """
    proposal = ModelPlanProposal(
        intent_type="workflow",
        workflow_steps=[
            {"id": "search", "capability": "literature.search", "input": {"topic": "RAG"}},
        ],
        missing_inputs=[{"field": "year_range", "reason": "not stated"}],
        outputs=["workflow"],
        confidence=0.8,
        rationale="survey the topic",
    )

    plan = IntentPlanner(_registry()).plan_from_proposal(
        "RAG 综述", proposal, task_id="run-model-gap"
    )

    assert plan.intent_type != IntentType.NEEDS_INPUT


# ── A filename is an address too ──


@pytest.mark.asyncio
async def test_an_invented_filename_is_refused_like_an_invented_url():
    """``attention_is_all_you_need.pdf`` has no scheme and is no less invented.

    The narrow first cut of this gate matched ``https://`` and ``file://`` only,
    so the second fabricated value in run 138c7b6e passed untouched and became
    the review's source. The skill then spent four tool calls proving the file
    did not exist before reviewing the paper from memory.
    """
    payload = {
        "intent_type": "single_skill_task",
        "confidence": 0.9,
        "required_capabilities": ["review.paper"],
        "capability_inputs": {"review.paper": {"input": "attention_is_all_you_need.pdf"}},
        "outputs": ["answer"],
        "rationale": "review the pdf",
    }

    proposal = await ModelIntentPlanner(_ScriptedLLM(payload), _registry()).propose(
        _INCIDENT_GOAL
    )

    assert proposal is not None
    assert "input" not in proposal.capability_inputs.get("review.paper", {})


@pytest.mark.asyncio
async def test_a_file_that_is_actually_there_is_used_without_asking(tmp_path, monkeypatch):
    """Existence settles it: a real file is a usable source however it was named."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "draft.pdf").write_bytes(b"%PDF-1.4\n")
    payload = {
        "intent_type": "single_skill_task",
        "confidence": 0.9,
        "required_capabilities": ["review.paper"],
        "capability_inputs": {"review.paper": {"input": "draft.pdf"}},
        "outputs": ["answer"],
        "rationale": "review the local draft",
    }

    proposal = await ModelIntentPlanner(_ScriptedLLM(payload), _registry()).propose(
        "review my draft"
    )

    assert proposal is not None
    assert proposal.capability_inputs["review.paper"]["input"] == "draft.pdf"


@pytest.mark.asyncio
async def test_a_filename_the_user_typed_is_used_as_given(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payload = {
        "intent_type": "single_skill_task",
        "confidence": 0.9,
        "required_capabilities": ["review.paper"],
        "capability_inputs": {"review.paper": {"input": "my_submission.pdf"}},
        "outputs": ["answer"],
        "rationale": "review the named file",
    }

    proposal = await ModelIntentPlanner(_ScriptedLLM(payload), _registry()).propose(
        "review my_submission.pdf please"
    )

    assert proposal is not None
    assert proposal.capability_inputs["review.paper"]["input"] == "my_submission.pdf"


@pytest.mark.asyncio
async def test_a_file_an_upstream_step_promises_is_left_to_the_resolver(tmp_path, monkeypatch):
    """A dependent step's filename is a claim about the plan, not about the disk.

    It cannot exist yet — the step that writes it has not run. Whether the
    promise holds is decided when the upstream step survives resolution, which
    is the workflow builder's contract, not this gate's.
    """
    monkeypatch.chdir(tmp_path)
    payload = {
        "intent_type": "workflow",
        "confidence": 0.9,
        "workflow_steps": [
            {"id": "fetch", "capability": "paper.fetch.arxiv", "input": {"identifier": "1706.03762"}},
            {
                "id": "review",
                "capability": "review.paper",
                "depends_on": ["fetch"],
                "input": {"input": "fetched_paper.pdf"},
            },
        ],
        "outputs": ["workflow"],
        "rationale": "fetch then review",
    }

    proposal = await ModelIntentPlanner(_ScriptedLLM(payload), _registry()).propose(
        "获取并评审 Attention Is All You Need"
    )

    assert proposal is not None
    assert proposal.workflow_steps[1]["input"]["input"] == "fetched_paper.pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("identifier", "1706.03762"),  # an arXiv id is a dotted token, not a file
        ("doi", "10.1145/3292500.3330701"),  # so is a DOI
        ("version", "v1.2"),
        ("mode", "standard"),
    ],
)
async def test_a_dotted_identifier_is_not_mistaken_for_a_filename(
    tmp_path, monkeypatch, field: str, value: str
):
    monkeypatch.chdir(tmp_path)
    payload = {
        "intent_type": "single_skill_task",
        "confidence": 0.9,
        "required_capabilities": ["paper.fetch.arxiv"],
        "capability_inputs": {"paper.fetch.arxiv": {field: value}},
        "outputs": ["answer"],
        "rationale": "fetch the named work",
    }

    proposal = await ModelIntentPlanner(_ScriptedLLM(payload), _registry()).propose(
        "获取 Attention Is All You Need"
    )

    assert proposal is not None
    assert proposal.capability_inputs["paper.fetch.arxiv"][field] == value


# ── Confidence observes; it does not decide ──


@pytest.mark.parametrize("confidence", [0.05, 0.5, 0.95])
def test_confidence_does_not_change_where_a_request_is_routed(confidence: float):
    """dc787efa was planned at 0.95, and the number changed nothing.

    A self-reported score is not evidence: the model was certain about a reading
    it had got wrong. Routing is decided by what the request contains and what
    the model explicitly chose, so the score stays an observation carried on the
    plan for the event log. Anything that needs to stop a run has to be a gate.
    """
    proposal = ModelPlanProposal(
        intent_type="single_skill_task",
        required_capabilities=["literature.search"],
        capability_inputs={"literature.search": {"topic": "retrieval augmented generation"}},
        confidence=confidence,
        execution_mode="background",
        rationale="survey the topic",
    )

    plan = IntentPlanner(_registry()).plan_from_proposal(
        "RAG 综述", proposal, task_id="run-confidence"
    )

    assert plan.intent_type == IntentType.REACT_FALLBACK
    assert plan.tool_policy.allows("search_literature")
    assert not plan.tool_policy.allows("run_skill")


# ── The gate stops at locators ──


@pytest.mark.asyncio
async def test_resolving_a_named_work_to_its_identifier_is_still_allowed():
    """Title -> canonical id is resolution, not fabrication.

    Guards the boundary from the other side: broadening this gate to "every
    value must appear verbatim in the request" would re-break the dfcb92bb
    contract, where a bound ``1706.03762`` is admitted as a locally-provable
    fact and verified by fetch at execution instead.
    """
    payload = {
        "intent_type": "workflow",
        "confidence": 0.9,
        "workflow_steps": [
            {"id": "paper", "capability": "paper.fetch.arxiv", "input": {"identifier": "1706.03762"}},
        ],
        "outputs": ["workflow"],
        "rationale": "fetch the named paper",
    }

    proposal = await ModelIntentPlanner(_ScriptedLLM(payload), _registry()).propose(
        "获取 Attention Is All You Need 摘要"
    )

    assert proposal is not None
    assert proposal.workflow_steps[0]["input"]["identifier"] == "1706.03762"
    assert proposal.missing_inputs == []
