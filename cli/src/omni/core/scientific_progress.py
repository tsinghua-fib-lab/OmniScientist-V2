"""This-turn scientific work vs lookup that only informs it.

Codex's loop treats progress as movement toward the user's request. Omni adds
durable memory and task tools; those are context, not a substitute for the
artifacts this ``task_id`` still owes. Distinct lookup queries look like
progress to a signature-keyed stall detector, so the ReAct loop counts them
separately — the same idea as contract-hunting after ``find_skill``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from omni.core.funnel_facts import is_empty_literature_funnel

# Tools that reopen earlier work. They never register this turn's figure,
# manuscript, slides, or review. Workspace read/git/shell is not this set —
# Codex treats those as progress toward the request.
LOOKUP_TOOLS = frozenset(
    {
        "memory_search",
        "memory_get",
        "search_tasks",
        "list_recent_tasks",
        "get_task",
        "get_subtask",
        "open_artifact",
        "list_session_artifacts",
    }
)

# Tools that retrieve, run, or record scientific work for *this* turn.
# ``run_skill`` / ``run_workflow`` cover skill-as-tool names such as arxiv-fetch.
RESEARCH_PRODUCE_TOOLS = frozenset(
    {
        "run_skill",
        "run_workflow",
        "search_literature",
        "search_corpus",
        "citation_neighbors",
        "add_evidence",
        "cite_source",
        "build_research_artifact",
        "web_search",
        "web_fetch",
        "write_file",
        "edit_file",
        "arxiv-fetch",
        "arxiv_search",
        "openalex-search",
        "openalex_search",
        "crossref_search",
        "pubmed_search",
    }
)

# One successful lookup may still be followed by a produce call. Two trailing
# lookups while a contract output is owed is the same empty loop as BUG-11.
MIN_LOOKUP_STREAK = 2

# Injected after the first lookup-only batch while a manuscript is still owed.
# Codex would not stop after `ls`; it would keep going. Omni cannot treat a
# sibling task's file as delivery, so the host names the produce path instead.
LOOKUP_STEER = (
    "This turn still owes a manuscript on this task_id. Files from another task "
    "do not count. Retrieve sources if this task has none, then produce the file "
    "with write_file or the relevant skill. Do not stop after lookup and wait "
    "for the host to write. git / read_file / list_dir of this workspace are "
    "not lookup — they are how the file gets written."
)


def lookup_pressure(trace: Iterable[Any], *, owed: bool) -> int:
    """Ledger lookups after the last this-turn scientific produce.

    ``owed`` is false for answer-only / inspect / review turns: lookup *is*
    the work, so the fuse stays off. ``bash`` / ``read_file`` / ``grep`` do
    not increment — those are workspace progress (Codex). A successful
    produce anywhere in the turn clears the fuse; sibling-task archaeology
    after that is leftover, not a missing file.
    """
    if not owed:
        return 0
    records = list(trace)
    if any(_produce_clears_pressure(record) for record in records):
        return 0
    trailing = 0
    for record in records:
        name = getattr(record, "name", "") or ""
        if name in LOOKUP_TOOLS:
            trailing += 1
    return trailing


def this_turn_research_evidence(
    trace: Iterable[Any],
    drained: Iterable[Any] | None = None,
) -> bool:
    """Whether this turn already retrieved or ran scientific work.

    Host manuscript fill is salvage for "the model did the research and did
    not write the file". Memory or task archaeology is not that evidence.
    A drained skill/workflow result counts even when the coordinator loop
    itself only called ``run_skill``. An empty literature funnel does not:
    Codex would not treat an empty search as progress toward the file.
    """
    for record in trace:
        if _produce_clears_pressure(record):
            return True
    for item in drained or ():
        if not isinstance(item, dict):
            continue
        payload = item.get("result") if item.get("result") is not None else item
        if is_empty_literature_funnel(payload) or is_empty_literature_funnel(item):
            continue
        if item.get("result") or item.get("subtask_id") or item.get("workflow_run_id"):
            return True
    return False


def _produce_clears_pressure(record: Any) -> bool:
    if getattr(record, "name", "") not in RESEARCH_PRODUCE_TOOLS:
        return False
    if getattr(record, "status", "") != "succeeded":
        return False
    return not is_empty_literature_funnel(getattr(record, "result", None))


__all__ = [
    "LOOKUP_STEER",
    "LOOKUP_TOOLS",
    "MIN_LOOKUP_STREAK",
    "RESEARCH_PRODUCE_TOOLS",
    "lookup_pressure",
    "this_turn_research_evidence",
]
