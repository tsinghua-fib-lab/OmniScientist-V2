"""Context-window compaction is independent from optional cumulative quotas.

The active request must leave room for the model's reply and stay in provider
token units. A long task may cross many such windows, so cumulative accounting
cannot also be the compaction ceiling: doing so turns an owner spend policy into
an accidental task-completion policy. These tests pin per-model tiers, request
fit, rollover wiring, and the separately opt-in cumulative quota path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from omni.agent.cost import react_usage_limits
from omni.config import load_settings
from omni.config.settings import (
    microcompact_token_budget,
    resolve_max_input_tokens,
    resolve_max_output_tokens,
    session_compact_token_budget,
    transcript_ceiling_tokens,
)
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ReActLoopAgent, ToolSpec
from omni.core.system_prompt import build_system_prompt
from omni.core.termination import base_termination_reason, execution_outcome_status
from omni.data import DEFAULT_ROLE
from omni.memory.compaction import (
    _MICROCOMPACT_PLACEHOLDER,
    estimate_messages_tokens,
    estimate_tokens,
)
from tests.conftest import ScriptedLLM


@pytest.fixture
def settings():  # noqa: ANN201
    return load_settings()


# Both tiers under the shipped model-window defaults.
_SHIPPED_THRESHOLDS = {
    "gpt-4o": (78_131, 100_454),
    "claude-3-5-sonnet": (134_265, 172_627),
    # 32,768 of window less 8,192 reserved for the reply.
    "omni-mock": (17_203, 22_118),
    "deepseek-v4-pro": (677_062, 870_508),
    # 8,192 of window, half of it reserved, because gpt-4's output cap is its
    # whole context and no other division leaves both sides usable.
    "gpt-4": (2_867, 3_686),
}

# The same models plus ``o3``, which is where the reply reservation was found
# missing: 180,000 of prompt at the expensive tier and 32,768 of reply requested,
# presented as 212,768 against a hard 200,000. They span the shapes that matter —
# a reply worth a twenty-fourth of the window, a tenth, an eighth, a sixth, and
# one worth the entire window.
_WINDOW_FIT_MODELS = (*sorted(_SHIPPED_THRESHOLDS), "o3")


@pytest.mark.parametrize(("model", "expected"), sorted(_SHIPPED_THRESHOLDS.items()))
def test_each_model_gets_the_thresholds_its_tighter_ceiling_allows(
    settings,  # noqa: ANN001
    model: str,
    expected: tuple[int, int],
) -> None:
    """Pinned so a future rescoping shows up as a readable diff rather than as a
    silent change in how often every deployment trims its context."""
    settings.model.model = model

    assert (
        microcompact_token_budget(settings),
        session_compact_token_budget(settings),
    ) == expected


def test_a_large_window_model_compacts_at_a_transcript_it_can_still_afford(
    settings,  # noqa: ANN001
) -> None:
    """A large model compacts against its active request window, not run spend."""
    settings.model.model = "deepseek-v4-pro"

    assert resolve_max_input_tokens(settings) == 1_000_000
    assert transcript_ceiling_tokens(settings) == 967_232
    assert microcompact_token_budget(settings) == 677_062
    assert session_compact_token_budget(settings) == 870_508


def test_explicit_cumulative_quota_does_not_shrink_the_active_context_window(
    settings,  # noqa: ANN001
) -> None:
    """One-request capacity and whole-run owner policy are independent axes."""
    settings.model.model = "deepseek-v4-pro"
    settings.cost.max_total_tokens = 20_000

    assert transcript_ceiling_tokens(settings) == 967_232
    assert microcompact_token_budget(settings) == 677_062


def test_a_small_window_still_wins_when_it_is_the_tighter_ceiling(
    settings,  # noqa: ANN001
) -> None:
    """A pinned window under twice the requested reply is split in half."""
    settings.model.model = "gpt-4o"
    settings.memory.context_window_tokens = 10_000

    assert microcompact_token_budget(settings) == 3_500
    assert session_compact_token_budget(settings) == 4_500


@pytest.mark.parametrize("quota", ["explicit_quota", "accounting_off"])
def test_run_budget_configuration_never_moves_context_tiers(
    settings,  # noqa: ANN001
    quota: str,
) -> None:
    """Accounting/spend policy does not alter one request's physical capacity."""
    settings.model.model = "deepseek-v4-pro"
    if quota == "explicit_quota":
        settings.cost.max_total_tokens = 250_000
    else:
        settings.cost.enabled = False

    assert microcompact_token_budget(settings) == 677_062
    assert session_compact_token_budget(settings) == 870_508
    assert session_compact_token_budget(settings) + resolve_max_output_tokens(
        settings.model
    ) <= resolve_max_input_tokens(settings)


def test_a_tiny_explicit_quota_does_not_move_context_tiers(
    settings,  # noqa: ANN001
) -> None:
    settings.model.model = "deepseek-v4-pro"
    settings.cost.max_total_tokens = 20_000

    assert microcompact_token_budget(settings) == 677_062
    assert session_compact_token_budget(settings) == 870_508


def test_raising_the_cumulative_quota_does_not_move_context_tiers(settings) -> None:  # noqa: ANN001
    settings.model.model = "deepseek-v4-pro"
    settings.cost.max_total_tokens = 250_000
    tight = (microcompact_token_budget(settings), session_compact_token_budget(settings))
    settings.cost.max_total_tokens = 1_000_000

    assert (microcompact_token_budget(settings), session_compact_token_budget(settings)) == tight


# ── metered runs: what the loop actually spends ──────────────────────────
#
# Everything below drives a real ReActLoopAgent, wired the way the orchestrator
# wires it, against an offline client that bills each call for the transcript it
# was handed. Scripted usage numbers would make a spend measurement circular.


# Enough repetitions for ``estimate_tokens``'s per-call constant to amortise
# away. Measured on one short unit it over-states the unit by about a tenth, and
# a payload sized a tenth under its target is the difference between seeding a
# transcript at a threshold and seeding it just below one.
_SIZING_PROBE_UNITS = 64


def _sized(unit: str, target_tokens: int) -> str:
    per_unit = estimate_tokens(unit * _SIZING_PROBE_UNITS) / _SIZING_PROBE_UNITS
    return unit * max(1, round(target_tokens / per_unit))


# A literature-search result set or a bundle of paper abstracts, which is what a
# research turn's observations actually weigh.
_RESEARCH_OBSERVATION = _sized(
    "Retrieval augmented generation improves factuality on open-domain QA. ", 2_500
)
# What the previous turn's summary reads like once the expensive tier has folded
# it: prose, arriving as one assistant message the cheap tier has no way to trim.
_SESSION_BRIDGE_UNIT = "Earlier in this session we established that "
# A loaded turn's system message: role, memory digest, skill catalog and project
# context all arrive on every iteration and none of it is compactable — it is the
# system message, not a tool observation, so it is the floor no amount of trimming
# can pass. Assembled through the real builder from the packaged role and measured
# here, rather than pinned at the 4,461 a single run once reported. The number was
# the whole problem: it was recorded in one estimator's units and the threshold it
# gets compared against is in another's, so the moment a machine loaded the real
# tokenizer this test simulated a system prompt 72% larger than the one that turn
# actually sent (4,461 against the 2,590 the same text really weighs). Fixing the
# *text* and measuring it leaves both sides in the units the run itself will use;
# the assembly below reproduces the pinned figure to within 1%.
_LOADED_TURN_CONTEXT = (
    "[Curated memory]\n"
    + "- The owner works on retrieval-augmented generation and prefers primary "
      "sources over survey summaries; cite arXiv ids inline.\n" * 24
    + "\n[Skill catalog]\n"
    + "- literature-search: find and rank papers across arXiv, OpenAlex and "
      "Semantic Scholar for a stated question.\n" * 24
    + "\n[Recent activity]\n"
    + "- Earlier today this project produced a review of sparse retrieval and "
      "filed it under artifacts/.\n" * 16
)
_ADVERTISED_TOOLS = [
    ToolSpec(name, "", {"type": "object", "properties": {}})
    for name in (
        "read_file", "write_file", "edit_file", "glob", "grep", "bash", "python",
        "web_search", "web_fetch", "find_skill", "run_skill", "remember", "recall",
        "schedule_task", "list_tasks", "todo_write", "notebook_append", "ask_owner",
    )
]
_PRODUCTION_SYSTEM_PROMPT = build_system_prompt(
    role=DEFAULT_ROLE,
    tools=_ADVERTISED_TOOLS,
    memory_block=_LOADED_TURN_CONTEXT,
    project_name="default",
    working_dir="/Users/researcher/omniscientist_v2",
)
_PRODUCTION_SYSTEM_PROMPT_TOKENS = estimate_tokens(_PRODUCTION_SYSTEM_PROMPT)
# gpt-4 holds 8,192 tokens in total, so the production system prompt does not fit
# in it at all — that model cannot run a research turn whatever compaction does.
# A lean prompt is what lets one test span windows three orders apart.
_LEAN_SYSTEM_PROMPT_TOKENS = 200
_COMPLETION_TOKENS = 300

_DIG = ToolSpec(
    "dig", "search the literature",
    {"type": "object", "properties": {"q": {"type": "string"}}},
)


@dataclass
class _MeteredRun:
    cumulative: int = 0
    iterations: int = 0
    reason: str = ""
    kind: str = ""
    transcripts: list[int] = field(default_factory=list)


class _MeteredLLM(ScriptedLLM):
    """Offline client that bills every call for the transcript it was handed."""

    def __init__(self, *, stop_after: int | None = None) -> None:
        super().__init__([])
        self._stop_after = stop_after
        self.transcripts: list[int] = []
        self.stubbed: list[int] = []
        self.requests: list[int] = []

    async def chat_with_tools(self, messages, tools, **kwargs):  # noqa: ANN001, ANN201
        size = estimate_messages_tokens(messages)
        self.transcripts.append(size)
        # What the provider weighs against its window is the whole request: the
        # transcript, the schemas of every tool offered, and the room the reply is
        # told it may take.
        self.requests.append(
            size
            + estimate_tokens(json.dumps(tools, default=str))
            + int(kwargs.get("max_tokens") or 0)
        )
        self.stubbed.append(
            sum(
                1
                for m in messages
                if _MICROCOMPACT_PLACEHOLDER in str(m.get("content") or "")
            )
        )
        usage = {
            "prompt_tokens": size,
            "completion_tokens": _COMPLETION_TOKENS,
            "total_tokens": size + _COMPLETION_TOKENS,
        }
        n = len(self.transcripts)
        if self._stop_after is not None and n >= self._stop_after:
            return ChatWithToolsResult(content="final answer", usage=usage)
        return ChatWithToolsResult(
            tool_calls=[ToolCall(f"c{n}", "dig", {"q": f"topic-{n}"})], usage=usage
        )


async def _meter_research_turn(
    settings,  # noqa: ANN001
    *,
    system_prompt_tokens: int,
    observations: list[str] | None = None,
    history: list[dict[str, str]] | None = None,
    stop_after: int | None = None,
) -> tuple[_MeteredRun, _MeteredLLM]:
    """Run one research-shaped turn under the settings' real compaction wiring."""
    llm = _MeteredLLM(stop_after=stop_after)
    queue = list(observations or [])

    async def invoker(_name, args):  # noqa: ANN001
        body = queue.pop(0) if queue else _RESEARCH_OBSERVATION
        return f"{args['q']}: {body}"

    agent = ReActLoopAgent(
        llm,
        invoker,
        # This fixture intentionally measures a finite explicit policy. Shipped
        # coordinator defaults are unbounded and therefore cannot be the stop
        # condition of a never-ending synthetic model.
        max_iterations=(
            settings.react.max_iterations
            if settings.react.max_iterations > 0
            else 20
        ),
        max_tool_calls=(
            settings.react.max_tool_calls
            if settings.react.max_tool_calls > 0
            else 40
        ),
        max_tokens=resolve_max_output_tokens(settings.model),
        soft_token_limit=microcompact_token_budget(settings),
        context_rollover_token_limit=session_compact_token_budget(settings),
        microcompact_keep_tool_results=settings.memory.microcompact_keep_tool_results,
        no_progress_threshold=settings.react.no_progress_threshold,
        **react_usage_limits(settings, llm),
    )
    result = await agent.run(
        system_prompt=_sized("policy clause about tool usage. ", system_prompt_tokens),
        user_message="review the retrieval-augmented generation literature",
        tools=[_DIG],
        history=history,
    )
    return (
        _MeteredRun(
            cumulative=int(result.total_usage.get("total_tokens") or 0),
            iterations=result.total_iterations,
            reason=base_termination_reason(result.terminated_reason),
            kind=result.kind,
            transcripts=list(llm.transcripts),
        ),
        llm,
    )


@pytest.mark.asyncio
async def test_a_research_turn_reaches_its_iteration_limit_inside_its_budget(
    settings,  # noqa: ANN001
) -> None:
    """A fixture's explicit semantic limit still works with default quota off."""
    run, _ = await _meter_research_turn(settings, system_prompt_tokens=400)

    assert run.iterations == 20
    assert run.reason == "max_iterations"
    assert run.cumulative > 0


@pytest.mark.asyncio
async def test_the_cheap_tier_sits_above_the_floor_it_can_never_trim_past(
    settings,  # noqa: ANN001
) -> None:
    """The cheap tier must leave room above the content it preserves verbatim."""
    settings.memory.context_window_tokens = 50_000
    run, _ = await _meter_research_turn(
        settings, system_prompt_tokens=_PRODUCTION_SYSTEM_PROMPT_TOKENS
    )
    threshold = microcompact_token_budget(settings)
    settled = run.transcripts[settings.memory.microcompact_keep_tool_results :]
    floor = min(settled)

    assert floor <= threshold
    assert threshold - floor >= estimate_tokens(_RESEARCH_OBSERVATION)


@pytest.mark.asyncio
async def test_compaction_does_not_stop_the_budget_from_ending_a_run(
    settings,  # noqa: ANN001
) -> None:
    """The spend ceiling counts cumulative tokens, so no amount of trimming can
    lower it. Compaction changes how much a run gets done inside its budget,
    never whether exhausting that budget still ends the run as ``degraded``.
    """
    settings.cost.max_total_tokens = 60_000
    settings.memory.context_window_tokens = 50_000

    run, llm = await _meter_research_turn(
        settings, system_prompt_tokens=_PRODUCTION_SYSTEM_PROMPT_TOKENS
    )

    assert run.reason == "max_total_tokens"
    assert execution_outcome_status(run.kind, run.reason) == "degraded"
    assert run.cumulative >= settings.cost.max_total_tokens


@pytest.mark.asyncio
async def test_a_front_loaded_transcript_still_stops_on_its_budget(
    settings,  # noqa: ANN001
) -> None:
    """The named failure mode of a growth-aware ceiling. Sizing assumes the
    transcript grows into it; a run that arrives at the ceiling on its first
    observation — a pasted document — pays the full ceiling every iteration and
    runs out of budget early. The cumulative counter is what catches it: the run
    stops short, degraded, rather than spending past the cap its owner set.
    """
    document = _sized("Section body text of a pasted manuscript. ", 90_000)
    settings.cost.max_total_tokens = 200_000

    run, _ = await _meter_research_turn(
        settings,
        system_prompt_tokens=_PRODUCTION_SYSTEM_PROMPT_TOKENS,
        observations=[document],
    )

    assert run.iterations < 20
    assert run.reason == "max_total_tokens"
    assert execution_outcome_status(run.kind, run.reason) == "degraded"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", _WINDOW_FIT_MODELS)
@pytest.mark.parametrize("budget", [True, False], ids=["budget_on", "budget_off"])
async def test_the_largest_request_a_tier_permits_still_fits_the_window(
    settings,  # noqa: ANN001
    model: str,
    budget: bool,
) -> None:
    """A threshold is only correct if the request built at it is one the provider
    will accept, and on a shared window that request is prompt *plus* reply.

    Seeded with the largest history the expensive tier allows, because that is
    the worst case the loop can be handed: a folded session bridge arrives as an
    assistant message, which the cheap tier cannot trim — it only shrinks tool
    observations. So the first request of the next turn carries the whole of it.

    Measured on the request rather than on the threshold. Arithmetic on a
    threshold was never wrong; what was wrong was its relationship to what the
    provider accepts, and only a built request shows that. Unreserved, ``o3``
    builds a 212,790-token request against its 200,000 limit; ``gpt-4o`` 131,691
    against 128,000; ``gpt-4`` 15,831 against 8,192. ``claude-3-5-sonnet`` and
    ``deepseek-v4-pro`` fit either way, their replies being a small enough share
    of their windows to hide the defect — which is how it survived.
    """
    settings.model.model = model
    settings.cost.enabled = budget
    window = resolve_max_input_tokens(settings)
    folded = session_compact_token_budget(settings)
    bridge = [
        {"role": "user", "content": "continue the review"},
        {"role": "assistant", "content": _sized(_SESSION_BRIDGE_UNIT, folded)},
    ]

    _, llm = await _meter_research_turn(
        settings,
        system_prompt_tokens=_LEAN_SYSTEM_PROMPT_TOKENS,
        history=bridge,
        stop_after=1,
    )

    assert llm.requests
    assert max(llm.requests) <= window


@pytest.mark.asyncio
async def test_trimming_really_happens_over_a_long_research_turn(
    settings,  # noqa: ANN001
) -> None:
    """Guards the metered tests above from passing vacuously: with compaction
    unwired they would be measuring an uncompacted loop and reporting its spend
    as if the tiers had produced it."""
    settings.memory.context_window_tokens = 50_000
    _, llm = await _meter_research_turn(
        settings, system_prompt_tokens=_PRODUCTION_SYSTEM_PROMPT_TOKENS
    )

    assert max(llm.stubbed) > 0
