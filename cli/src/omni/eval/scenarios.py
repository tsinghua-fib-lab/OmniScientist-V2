"""Scenario corpus model + YAML loader for the capability benchmark.

A *scenario* is a short, realistic research/dialogue interaction. Two kinds:

* ``type: turns`` (default) — one or more user turns driven through the agent.
  Each turn may carry the semantic-planner JSON the model *should* emit (so the
  run is deterministic and offline), a ``drain`` flag, and an ``expect`` block
  of checks.
* ``type: retrieval`` — a thin wrapper over ``run_retrieval_bench`` so the
  retrieval quality slice lives on the same scoreboard.

The corpus is plain YAML so non-Python contributors can add scenarios; the
bundled seed lives in ``omni/data/eval_scenarios``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class ScenarioTurn:
    """One user turn plus the expectations the agent's response must satisfy."""

    user: str
    plan: dict[str, Any] | None = None
    drain: bool = False
    channel: str | None = None  # overrides the scenario channel for this turn
    expect: dict[str, Any] = field(default_factory=dict)
    # L4 journey extension: scripted tool calls the model "emits" in the ReAct
    # loop this turn (each ``{name, args}``). Drives real tool execution through
    # the approval gate + sandbox so the ``safety`` dimension is exercised the
    # way a human's request would. ``approve`` is the simulated owner's answer
    # ("once"/"session"/True → approve, "deny"/False → deny, None → no approver
    # wired, i.e. non-interactive/remote → fail closed).
    tool_calls: tuple[dict[str, Any], ...] = ()
    approve: Any = None
    # L4 journey extension (self-review): scripted reviewer verdicts the in-loop
    # judge returns in order (each ``{verdict, score, notes}``). Paired with
    # ``expect.self_review`` ("revised" | "passed" | "bounded" | "none").
    review: tuple[dict[str, Any], ...] = ()
    # L4 journey extension (recall bounds): number of synthetic project findings to
    # bulk-insert into memory before the turn, so recall runs against a realistic
    # (or oversized) store. Paired with ``expect.recall`` ("bounded" | "functional").
    seed_memories: int = 0
    # L4 journey extension (task auto-retry): submit a background fixture task after
    # the turn that fails deterministically to exercise the runtime's retry path
    # (each ``{capability, mode, input}``; ``mode`` = "transient" | "permanent").
    # Paired with ``expect.retry`` ("auto" | "none" | "exhausted").
    submit_task: dict[str, Any] | None = None
    # L4 journey extension (session forking): after the turn, fork the current
    # session and check the branch. Paired with ``expect.fork`` ("copied" |
    # "isolated").
    fork: bool = False
    # L4 journey extension (cron/scheduled jobs): after the turn, create a schedule
    # (``{capability, kind, interval_s|cron, input}``) and tick the scheduler.
    # Paired with ``expect.schedule`` ("fired" | "recurring" | "not_due").
    schedule: dict[str, Any] | None = None
    # L4 journey extension (streaming output): capture the answer's streamed
    # deltas during the turn. Paired with ``expect.streaming`` ("progressive" |
    # "complete"). ``answer`` optionally scripts the (longer) final answer so the
    # progressive deltas are observable.
    stream: bool = False
    answer: str = ""


@dataclass(frozen=True, slots=True)
class Scenario:
    """A benchmarkable interaction with the agent."""

    id: str
    title: str
    type: str = "turns"  # "turns" | "retrieval"
    channel: str = "cli"
    # Who this scenario role-plays: ``scientist`` (research workflows) or
    # ``general`` (everyday assistant use). Empty = unattributed (legacy seed).
    persona: str = ""
    tags: tuple[str, ...] = ()
    fixtures: tuple[str, ...] = ()  # capabilities to back with offline echo skills
    turns: tuple[ScenarioTurn, ...] = ()
    # retrieval-only:
    k: int = 3
    min_recall: float = 0.8

    @property
    def dimensions(self) -> tuple[str, ...]:
        """Capability dimensions this scenario contributes to (its tags)."""
        return self.tags or ("uncategorized",)

    def capabilities(self) -> set[str]:
        """Capabilities referenced by this scenario's planner steps / expects."""
        caps: set[str] = set()
        for turn in self.turns:
            for step in (turn.plan or {}).get("workflow_steps") or []:
                if isinstance(step, dict) and step.get("capability"):
                    caps.add(str(step["capability"]))
            single = (turn.plan or {}).get("capability")
            if single:
                caps.add(str(single))
            for cap in turn.expect.get("capabilities_include") or []:
                caps.add(str(cap))
        return caps


def bundled_scenarios_dir() -> Path:
    """Directory of the seed scenario corpus shipped with the package."""
    return Path(__file__).resolve().parent.parent / "data" / "eval_scenarios"


def _coerce_turn(raw: dict[str, Any]) -> ScenarioTurn:
    return ScenarioTurn(
        user=str(raw.get("user", "")),
        plan=raw.get("plan"),
        drain=bool(raw.get("drain", False)),
        channel=raw.get("channel"),
        expect=dict(raw.get("expect") or {}),
        tool_calls=tuple(dict(c) for c in (raw.get("tool_calls") or ())),
        approve=raw.get("approve"),
        review=tuple(dict(c) for c in (raw.get("review") or ())),
        seed_memories=int(raw.get("seed_memories") or 0),
        submit_task=dict(raw["submit_task"]) if isinstance(raw.get("submit_task"), dict) else None,
        fork=bool(raw.get("fork", False)),
        schedule=dict(raw["schedule"]) if isinstance(raw.get("schedule"), dict) else None,
        stream=bool(raw.get("stream", False)),
        answer=str(raw.get("answer") or ""),
    )


def _coerce_scenario(raw: dict[str, Any]) -> Scenario:
    return Scenario(
        id=str(raw["id"]),
        title=str(raw.get("title", raw["id"])),
        type=str(raw.get("type", "turns")),
        channel=str(raw.get("channel", "cli")),
        persona=str(raw.get("persona", "")),
        tags=tuple(raw.get("tags") or ()),
        fixtures=tuple(raw.get("fixtures") or ()),
        turns=tuple(_coerce_turn(t) for t in (raw.get("turns") or ())),
        k=int(raw.get("k", 3)),
        min_recall=float(raw.get("min_recall", 0.8)),
    )


def load_scenarios(
    source: Path | None = None, *, tag: str | None = None, persona: str | None = None
) -> list[Scenario]:
    """Load scenarios from a YAML file or a directory of ``*.yaml`` files.

    ``source`` defaults to the bundled corpus. Each YAML file may hold a single
    scenario mapping or a list of them. Pass ``tag`` to keep only scenarios that
    declare it, or ``persona`` (``scientist``/``general``/``red_team``) to keep only that
    persona's suite. Scenarios are returned sorted by id for stable reporting.
    """
    root = source or bundled_scenarios_dir()
    files: Iterable[Path]
    if root.is_dir():
        files = sorted(root.glob("*.yaml")) + sorted(root.glob("*.yml"))
    else:
        files = [root]

    scenarios: list[Scenario] = []
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if doc is None:
            continue
        entries = doc if isinstance(doc, list) else [doc]
        for entry in entries:
            scenarios.append(_coerce_scenario(entry))

    if tag:
        scenarios = [s for s in scenarios if tag in s.tags]
    if persona:
        scenarios = [s for s in scenarios if s.persona == persona]
    scenarios.sort(key=lambda s: s.id)
    return scenarios


__all__ = ["ScenarioTurn", "Scenario", "load_scenarios", "bundled_scenarios_dir"]
