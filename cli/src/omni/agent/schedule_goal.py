"""Seal the work a schedule will actually run.

The coordinating model, the planner, and an open time-clarification draft can
each name a different goal on the same turn. The 2026-08-13 WeChat incident
stored a federated-learning objective (planner, via Active target) while the
user-visible title and tool-call still said RAG (open draft + ReAct args).
This module is the single host rule that picks one goal *before* anything is
persisted or shown.

Order of trust
--------------
1. This-turn user text, when the host/planner goal is grounded in it
   (Decision #3 anti-drift: the model cannot silently rewrite stated work).
2. The open clarification draft for this requester — a time-only follow-up
   updates the trigger and keeps that goal. Active target is not consulted.
3. The model's ``schedule_task`` goal, when no draft exists.
4. Otherwise fail closed and ask. An ungrounded host goal (typically the
   latest research-ideation report sitting in Active target) is never the
   default scheduled work. Active target remains valid for "revise this
   figure" / "inspect this report" on other intents.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SealedScheduleWork:
    """The goal/title that will be stored, shown, and fired — one object."""

    goal: str
    title: str
    source: str  # host | model | draft | conflict


def goal_grounded_in_message(goal: str, user_message: str) -> bool:
    """True when ``goal`` is attested by the current user message.

    Whitespace-folded substring match. A host/planner objective that names
    different work (an Active-target report the user did not mention this
    turn) is not grounded and must not replace a draft or model goal.
    """
    needle = " ".join((goal or "").split())
    haystack = " ".join((user_message or "").split())
    if not needle or not haystack:
        return False
    if needle in haystack:
        return True
    # A cleaned objective is often a long stem of the user text.
    stem = needle[:24]
    return len(stem) >= 12 and stem in haystack


def _names_other_work(title: str, goal: str, rejected: tuple[str, ...]) -> bool:
    """True when ``title`` names a losing candidate rather than the sealed goal."""
    if title in goal or goal_grounded_in_message(title, goal):
        return False
    for other in rejected:
        other = str(other or "").strip()
        if other and other != goal and goal_grounded_in_message(title, other):
            return True
    return False


def _title_for(goal: str, *candidates: str, rejected: tuple[str, ...] = ()) -> str:
    """Keep a supplied title unless it names work we did not seal.

    Short labels ("daily digest") are kept even when they are not a substring
    of the goal. A title that is grounded in a *losing* candidate (federated
    learning while we sealed RAG) is dropped so display matches storage.
    """
    for raw in candidates:
        title = str(raw or "").strip()
        if not title:
            continue
        if _names_other_work(title, goal, rejected):
            continue
        return title
    return (goal[:57] + "…") if len(goal) > 60 else goal


def seal_schedule_work(
    *,
    model_goal: str = "",
    model_title: str = "",
    host_goal: str = "",
    user_message: str = "",
    draft_goal: str = "",
    draft_title: str = "",
) -> SealedScheduleWork:
    """Pick the one goal that will be stored, displayed, and fired.

    ``source="conflict"`` means the host must ask rather than guess — there is
    no this-turn text, no open draft, and no model goal to trust, or the only
    candidate is an ungrounded host inference.
    """
    model_goal = str(model_goal or "").strip()
    model_title = str(model_title or "").strip()
    host_goal = str(host_goal or "").strip()
    user_message = str(user_message or "").strip()
    draft_goal = str(draft_goal or "").strip()
    draft_title = str(draft_title or "").strip()

    if host_goal and goal_grounded_in_message(host_goal, user_message):
        return SealedScheduleWork(
            goal=host_goal,
            title=_title_for(
                host_goal, model_title, draft_title, rejected=(model_goal, draft_goal)
            ),
            source="host",
        )
    if draft_goal:
        # A new work description this turn (user changed the assignment) beats
        # the draft. A model that merely copied Active target is not grounded
        # in a time-only follow-up, so the draft stands.
        if (
            model_goal
            and model_goal != draft_goal
            and goal_grounded_in_message(model_goal, user_message)
        ):
            return SealedScheduleWork(
                goal=model_goal,
                title=_title_for(
                    model_goal, model_title, rejected=(draft_goal, host_goal)
                ),
                source="model",
            )
        return SealedScheduleWork(
            goal=draft_goal,
            title=_title_for(
                draft_goal, draft_title, model_title, rejected=(model_goal, host_goal)
            ),
            source="draft",
        )
    if model_goal:
        return SealedScheduleWork(
            goal=model_goal,
            title=_title_for(model_goal, model_title, rejected=(host_goal,)),
            source="model",
        )
    return SealedScheduleWork(goal="", title="", source="conflict")


__all__ = [
    "SealedScheduleWork",
    "goal_grounded_in_message",
    "seal_schedule_work",
]
