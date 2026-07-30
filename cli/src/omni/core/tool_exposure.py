"""Which tools a turn pays to advertise, as distinct from which it may run.

Denial lives in :mod:`omni.core.tool_policy` and answers "may this turn touch
that at all". This module answers a cheaper question — "is it worth sending this
schema on every iteration" — and the two must never be conflated. A denied tool
is absent from the tool list and unreachable; a deferred tool is present,
reachable, and merely unadvertised. Anything that blurs that line reintroduces
the failure this design exists to prevent, where withholding a schema to save
tokens silently withdrew the capability.

Deferral is keyed to stable facts about a capability family, never to an
inferred stage of the turn. Stage-keyed narrowing has to guess what the turn
will need before the turn has happened, and it is wrong exactly when it is most
expensive. A family is either usually-idle on a research turn or it is not, and
that judgement does not change halfway through.

The families below are all *reactive*: something in the turn — a schedule the
user asks about, a plan step that calls for provenance — sends the model looking
for the tool, and it finds the name in the catalog. A *discretionary* tool is
different. The model uses it only because seeing it suggested the strategy, so
withholding the schema suppresses the behaviour instead of merely deferring the
cost, which is the one thing deferral must not do. ``spawn_subagents`` is the
clear case and is deliberately not deferred despite being the second-largest
schema on the surface: nothing prompts a model to delegate except the tool.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omni.skills_runtime.context import Tool

# Usually idle on a research turn, and expensive to advertise: measured against a
# 50-tool coordinator surface these 18 schemas are 35% of the per-iteration tool
# cost. Local telemetry (720 recorded tool calls) shows them at 4.2% of calls
# combined, and the prompt already tells the model to reach for structured
# provenance "only when requested or required by the active plan" — so their
# rarity is by instruction, which is the case where deferral costs least.
#
# Each entry is a family judgement, and each is reversible one name at a time:
#
# * schedule follow-ups — ``schedule_task`` stays advertised because it is the
#   recognisable entry point; listing, cancelling, and answering a clarification
#   only matter once a schedule exists, and the model is told they exist.
# * compute — idle unless a run is dispatched, and ``bash`` already covers local
#   execution, so this family is the deliberate escalation to a remote backend.
# * structured provenance and artifact packaging — on-request by prompt.
DEFERRED_TOOLS: frozenset[str] = frozenset(
    {
        "list_schedules",
        "resolve_action_checkpoint",
        "cancel_schedule",
        "run_compute",
        "get_compute_job",
        "cancel_compute",
        "record_hypothesis",
        "record_claim",
        "add_evidence",
        "cite_source",
        "attach_provenance",
        "package_artifact",
        "build_research_artifact",
        "build_figure_bundle",
        "verify_figure_bundle",
        "review_statistics",
        "citation_neighbors",
        "log_run",
    }
)


def apply_default_exposure(tools: list[Tool]) -> list[Tool]:
    """Mark the usually-idle families deferred, leaving reachability untouched.

    Mutates each :class:`~omni.skills_runtime.context.Tool` in place rather than
    dropping anything, because dropping is what denial does. The returned list is
    the same list, with the same names, in the same order.
    """
    for tool in tools:
        if tool.spec.name in DEFERRED_TOOLS and tool.spec.exposure != "deferred":
            tool.spec = replace(tool.spec, exposure="deferred")
    return tools


__all__ = ["DEFERRED_TOOLS", "apply_default_exposure"]
