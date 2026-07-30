"""Reviewer agent — an LLM-as-judge gate over a specialist's output.

The third layer of the multi-agent pattern: after a specialist produces a
result, a reviewer scores it against the subtask goal and returns a structured
verdict (``pass`` / ``revise`` / ``reject`` + a 0–1 score + notes). The runtime
uses that to accept, ask for one bounded revision, or reject.

It is deliberately *fail-open*: any LLM/parse failure yields a ``pass`` so a
flaky judge never blocks real work — the reviewer improves quality when it can
speak, and never becomes a single point of failure when it can't.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from omni.agent.cost import usage_budget_exhausted

_REVIEW_SYSTEM = (
    "You are a rigorous research reviewer. Evaluate an output against its subtask goal. "
    "Return one JSON object and no additional prose:\n"
    '{"verdict": "pass|revise|reject", "score": 0.0-1.0, "notes": "brief reason and improvement"}\n'
    "pass means acceptable; revise means directionally correct but incomplete; reject means off-target or wrong."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_VALID_VERDICTS = ("pass", "revise", "reject")


@dataclass
class ReviewVerdict:
    verdict: str            # pass | revise | reject
    score: float            # 0.0 – 1.0
    notes: str = ""
    parsed: bool = True     # False when the judge's reply couldn't be parsed
    metering_input: str = ""
    metering_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "score": round(self.score, 3),
                "notes": self.notes, "parsed": self.parsed}


def _clamp(x: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.5


def parse_verdict(raw: str) -> ReviewVerdict:
    """Parse a judge reply into a verdict; fail-open to ``pass`` if unparseable."""
    m = _JSON_RE.search(raw or "")
    if not m:
        return ReviewVerdict("pass", 1.0, "reviewer reply not JSON → fail-open", parsed=False)
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return ReviewVerdict("pass", 1.0, "reviewer JSON invalid → fail-open", parsed=False)
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in _VALID_VERDICTS:
        verdict = "pass"
    return ReviewVerdict(
        verdict=verdict,
        score=_clamp(data.get("score", 0.5)),
        notes=str(data.get("notes", "")).strip(),
    )


async def review_output(
    llm: Any, *, goal: str, output: str, criteria: str = ""
) -> ReviewVerdict:
    """Score a specialist ``output`` against ``goal`` via an LLM judge."""
    if llm is None or not (output or "").strip():
        return ReviewVerdict("pass", 1.0, "no reviewer / empty output", parsed=False)
    user = (
        f"Subtask goal:\n{goal}\n\n"
        f"Review criteria: {criteria or 'completeness, correctness, directness, and evidentiary support'}\n\n"
        f"Output to review:\n{output}"
    )
    try:
        raw = await llm.chat(_REVIEW_SYSTEM, user)
    except Exception:  # noqa: BLE001 — a broken judge must never block work
        return ReviewVerdict("pass", 1.0, "reviewer call failed → fail-open", parsed=False)
    verdict = parse_verdict(raw)
    verdict.metering_input = f"{_REVIEW_SYSTEM}\n{user}"
    verdict.metering_output = raw
    return verdict


def gate(verdict: ReviewVerdict, *, min_score: float) -> str:
    """Map a verdict + score threshold to an action: accept | revise | reject."""
    if verdict.verdict == "reject":
        return "reject"
    if verdict.verdict == "revise" or verdict.score < min_score:
        return "revise"
    return "accept"


async def review_and_correct(
    *,
    llm: Any,
    cfg: Any,
    tasks: Any,
    react: Any,
    result: Any,
    system: str,
    user_message: str,
    tool_specs: Any,
    history: Any,
    task_id: str,
    force: bool = False,
    settings: Any = None,
    execution_control: Any = None,
) -> Any:
    """Judge and revise one main-loop answer within a bounded review budget."""
    if not (force or getattr(cfg, "self_review", False)) or llm is None:
        return result
    if result.kind not in ("text", "partial") or not (result.content or "").strip():
        return result
    if usage_budget_exhausted(result):
        return result

    verdict = await review_output(llm, goal=user_message, output=result.content)
    await _record_review_cost(
        tasks, settings, llm, task_id, verdict, component="reviewer.main.1"
    )
    action = gate(verdict, min_score=float(cfg.self_review_min_score))
    revises = 0
    while action == "revise" and revises < int(cfg.self_review_max_revises):
        revises += 1
        revised = await react.run(
            system_prompt=system,
            user_message=(
                f"{user_message}\n\n[Self-review feedback] {verdict.notes}\n"
                "Revise the answer to be more complete, accurate, and evidence-grounded."
            ),
            tools=tool_specs,
            history=history,
            allow_escalation=False,
            # Share the parent turn's cancel/steer control so an interrupt during
            # a self-review revision is honoured instead of ignored.
            execution_control=execution_control,
        )
        if (revised.content or "").strip():
            result = revised
        await _record_loop_cost(
            tasks,
            settings,
            llm,
            task_id,
            revised,
            system=system,
            user_message=(
                f"{user_message}\n\n[Self-review feedback] {verdict.notes}\n"
                "Revise the answer to be more complete, accurate, and evidence-grounded."
            ),
            component=f"self_review.revision.{revises}",
        )
        verdict = await review_output(llm, goal=user_message, output=result.content)
        await _record_review_cost(
            tasks,
            settings,
            llm,
            task_id,
            verdict,
            component=f"reviewer.main.{revises + 1}",
        )
        action = gate(verdict, min_score=float(cfg.self_review_min_score))

    try:
        await tasks.append_event(
            task_id,
            event_type="self_review",
            status="succeeded" if action == "accept" else "degraded",
            name="self_review",
            output_json={
                "action": action,
                "revises": revises,
                "verdict": verdict.verdict,
                "score": round(float(verdict.score), 3),
                "notes": (verdict.notes or "")[:300],
                "source": "main_loop",
            },
            summary=(
                f"self-review {action} revises={revises} "
                f"score={round(float(verdict.score), 3)}"
            ),
        )
    except Exception:  # noqa: BLE001 - review auditing is best-effort.
        pass
    return result


async def _record_loop_cost(
    tasks: Any,
    settings: Any,
    llm: Any,
    task_id: str,
    result: Any,
    *,
    system: str,
    user_message: str,
    component: str,
) -> None:
    if settings is None:
        return
    from omni.agent.cost import record_cost_event

    await record_cost_event(
        tasks,
        settings,
        llm,
        task_id,
        result,
        system=system,
        user_message=user_message,
        component=component,
    )


async def _record_review_cost(
    tasks: Any,
    settings: Any,
    llm: Any,
    task_id: str,
    verdict: ReviewVerdict,
    *,
    component: str,
) -> None:
    if settings is None or not verdict.metering_output:
        return
    from omni.agent.cost import record_text_cost_event

    await record_text_cost_event(
        tasks,
        settings,
        llm,
        task_id,
        system=_REVIEW_SYSTEM,
        user_message=verdict.metering_input.removeprefix(f"{_REVIEW_SYSTEM}\n"),
        output=verdict.metering_output,
        component=component,
    )


__all__ = ["ReviewVerdict", "review_and_correct", "review_output", "parse_verdict", "gate"]
