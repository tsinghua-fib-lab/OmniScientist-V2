"""Natural-language, no-plan-injection evaluation for the public agent boundary.

The existing scenario harness is the fast deterministic contract suite. This
module is the product-quality layer: a scenario contains only user turns and
observable expectations, and every attempt enters through ``OmniAgent.handle_turn``.
Each repeat gets a fresh workspace so success-rate measurements cannot benefit
from state leaked by an earlier attempt.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.config.paths import OmniPaths
from omni.config.settings import OmniSettings
from omni.research.store import ResearchStore
from omni.research.verify import verify_session

AgentFactory = Callable[[OmniSettings], Awaitable[OmniAgent]]

_FORBIDDEN_TURN_KEYS = {
    "answer",
    "approve",
    "fixture",
    "fixtures",
    "model_output",
    "plan",
    "planner_output",
    "review",
    "seed_memories",
    "seed_tasks",
    "tool_calls",
    "tool_results",
}
_FORBIDDEN_SCENARIO_KEYS = _FORBIDDEN_TURN_KEYS | {
    "agent_factory",
    "model",
    "provider",
}
_ABSOLUTE_PATH = re.compile(
    r"(?<![:\w])/(?:Users|home|private|tmp|var/folders)/|(?:^|\s)[A-Za-z]:[\\/]",
    flags=re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class BlackBoxTurn:
    """One user-visible turn and checks over public/persisted outcomes."""

    user: str
    expect: dict[str, Any]
    channel: str | None = None
    drain_tasks: bool = True
    rework: bool = False


@dataclass(frozen=True, slots=True)
class BlackBoxScenario:
    """A real user journey with no model answer or planner plan embedded."""

    id: str
    title: str
    turns: tuple[BlackBoxTurn, ...]
    channel: str = "cli"
    persona: str = ""
    tags: tuple[str, ...] = ()
    requires_model: bool = False
    requires_network: bool = False


@dataclass(slots=True)
class BlackBoxAttempt:
    """One isolated execution of a scenario."""

    scenario_id: str
    repeat: int
    passed: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    provenance_accuracy: float = 1.0
    manual_rework: int = 0
    cost: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BlackBoxReport:
    """Reliability and operator-cost rollup across repeated attempts."""

    attempts: list[BlackBoxAttempt] = field(default_factory=list)
    skips: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def attempted(self) -> int:
        return len(self.attempts)

    @property
    def skipped(self) -> int:
        return len(self.skips)

    @property
    def success_rate(self) -> float:
        if not self.attempts:
            return 0.0
        return sum(attempt.passed for attempt in self.attempts) / len(self.attempts)

    @property
    def provenance_accuracy(self) -> float:
        if not self.attempts:
            return 0.0
        return sum(attempt.provenance_accuracy for attempt in self.attempts) / len(self.attempts)

    @property
    def mean_manual_rework(self) -> float:
        if not self.attempts:
            return 0.0
        return sum(attempt.manual_rework for attempt in self.attempts) / len(self.attempts)

    @property
    def mean_duration_ms(self) -> float:
        if not self.attempts:
            return 0.0
        return sum(attempt.duration_ms for attempt in self.attempts) / len(self.attempts)

    @property
    def total_tokens(self) -> int:
        return sum(int(attempt.cost.get("total_tokens") or 0) for attempt in self.attempts)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(float(attempt.cost.get("cost_usd") or 0.0) for attempt in self.attempts), 6)

    def scenario_metrics(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[BlackBoxAttempt]] = {}
        for attempt in self.attempts:
            grouped.setdefault(attempt.scenario_id, []).append(attempt)
        metrics: list[dict[str, Any]] = []
        for scenario_id, attempts in sorted(grouped.items()):
            count = len(attempts)
            metrics.append(
                {
                    "scenario_id": scenario_id,
                    "attempts": count,
                    "success_rate": sum(item.passed for item in attempts) / count,
                    "provenance_accuracy": sum(item.provenance_accuracy for item in attempts) / count,
                    "mean_manual_rework": sum(item.manual_rework for item in attempts) / count,
                    "mean_duration_ms": round(sum(item.duration_ms for item in attempts) / count, 3),
                    "total_tokens": sum(int(item.cost.get("total_tokens") or 0) for item in attempts),
                    "total_cost_usd": round(
                        sum(float(item.cost.get("cost_usd") or 0.0) for item in attempts),
                        6,
                    ),
                }
            )
        return metrics

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "skipped": self.skipped,
            "success_rate": self.success_rate,
            "provenance_accuracy": self.provenance_accuracy,
            "mean_manual_rework": self.mean_manual_rework,
            "mean_duration_ms": round(self.mean_duration_ms, 3),
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "metadata": dict(self.metadata),
            "scenarios": self.scenario_metrics(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "skips": list(self.skips),
        }


def bundled_blackbox_scenarios_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "blackbox_scenarios"


def load_blackbox_scenarios(source: Path | None = None) -> list[BlackBoxScenario]:
    """Load strict natural-language scenarios and reject harness-only controls."""
    root = source or bundled_blackbox_scenarios_dir()
    files: Iterable[Path]
    if root.is_dir():
        files = [*sorted(root.glob("*.yaml")), *sorted(root.glob("*.yml"))]
    else:
        files = [root]
    scenarios: list[BlackBoxScenario] = []
    seen: set[str] = set()
    for path in files:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if document is None:
            continue
        entries = document if isinstance(document, list) else [document]
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError(f"black-box scenario in {path} must be a mapping")
            scenario = _coerce_scenario(raw, path=path)
            if scenario.id in seen:
                raise ValueError(f"duplicate black-box scenario id: {scenario.id}")
            seen.add(scenario.id)
            scenarios.append(scenario)
    scenarios.sort(key=lambda item: item.id)
    return scenarios


async def run_blackbox_benchmark(
    scenarios: list[BlackBoxScenario] | None = None,
    *,
    settings: OmniSettings | None = None,
    repeats: int = 1,
    concurrency: int = 1,
    allow_model: bool = False,
    allow_network: bool = False,
    agent_factory: AgentFactory | None = None,
) -> BlackBoxReport:
    """Run scenarios through the real public boundary in isolated workspaces."""
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    source_settings = settings or load_settings()
    selected = scenarios if scenarios is not None else load_blackbox_scenarios()
    report = BlackBoxReport(
        metadata={
            "provider": str(source_settings.model.provider or "mock"),
            "model": str(source_settings.model.model or ""),
            "repeats": repeats,
            "concurrency": concurrency,
            "allow_model": allow_model,
            "allow_network": allow_network,
        }
    )
    provider = str(source_settings.model.provider or "mock").lower()
    jobs: list[tuple[BlackBoxScenario, int]] = []
    for scenario in selected:
        skip_reason = ""
        if scenario.requires_network and not allow_network:
            skip_reason = "requires network; pass allow_network=True"
        elif scenario.requires_model and agent_factory is None and not allow_model:
            skip_reason = "requires live model execution; pass allow_model=True"
        elif scenario.requires_model and provider == "mock" and agent_factory is None:
            skip_reason = "requires a configured non-mock model"
        if skip_reason:
            for repeat in range(1, repeats + 1):
                report.skips.append(
                    {"scenario_id": scenario.id, "repeat": repeat, "reason": skip_reason}
                )
            continue
        jobs.extend((scenario, repeat) for repeat in range(1, repeats + 1))
    semaphore = asyncio.Semaphore(concurrency)

    async def run_job(scenario: BlackBoxScenario, repeat: int) -> BlackBoxAttempt:
        async with semaphore:
            return await _run_attempt(
                scenario,
                repeat=repeat,
                source_settings=source_settings,
                agent_factory=agent_factory,
            )

    if jobs:
        report.attempts.extend(
            await asyncio.gather(*(run_job(scenario, repeat) for scenario, repeat in jobs))
        )
    return report


async def _run_attempt(
    scenario: BlackBoxScenario,
    *,
    repeat: int,
    source_settings: OmniSettings,
    agent_factory: AgentFactory | None,
) -> BlackBoxAttempt:
    attempt = BlackBoxAttempt(scenario_id=scenario.id, repeat=repeat)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix=f"omni-blackbox-{_slug(scenario.id)}-") as raw_root:
        root = Path(raw_root)
        attempt_settings = isolated_eval_settings(source_settings, root, scenario.id)
        factory = agent_factory or OmniAgent.create
        agent: OmniAgent | None = None
        try:
            agent = await factory(attempt_settings)
            external_key = f"blackbox:{scenario.id}:{repeat}"
            session_id = await agent.ensure_session(
                channel=scenario.channel,
                external_key=external_key,
            )
            channel_runners: dict[str, Any] = {}
            for index, turn in enumerate(scenario.turns):
                channel = turn.channel or scenario.channel
                if channel in {"wechat", "feishu", "dingtalk"}:
                    runner = channel_runners.get(channel)
                    if runner is None:
                        runner = _build_eval_channel(
                            channel,
                            settings=attempt_settings,
                            agent=agent,
                            external_key=external_key,
                        )
                        channel_runners[channel] = runner
                    presentation, result = await runner(turn.user, turn.drain_tasks)
                    result.text = presentation.to_plain_text()
                    session_id = result.session_id
                else:
                    result = await agent.handle_turn(
                        turn.user,
                        session_id=session_id,
                        channel=channel,
                        drain_tasks=turn.drain_tasks,
                    )
                attempt.task_ids.append(result.task_id)
                run = await agent.tasks.get_task(result.task_id)
                events = await agent.tasks.list_events(result.task_id)
                status = str(run.status if run is not None else "missing")
                attempt.statuses.append(status)
                attempt.checks.extend(
                    _evaluate_turn(
                        turn,
                        result=result,
                        run=run,
                        events=events,
                        turn_index=index,
                    )
                )
            provenance = await verify_session(
                ResearchStore(agent.db),
                session_id=session_id,
                audit_memory=False,
            )
            attempt.provenance_accuracy = provenance.grounding_rate
            attempt.cost = await _aggregate_cost(agent, attempt.task_ids)
        except Exception as exc:  # noqa: BLE001 - a crash is benchmark evidence
            attempt.error = f"{type(exc).__name__}: {exc}"
        finally:
            if agent is not None:
                await agent.aclose()
    attempt.duration_ms = (time.perf_counter() - started) * 1000
    attempt.passed = not attempt.error and bool(attempt.checks) and all(
        bool(check.get("passed")) for check in attempt.checks
    )
    declared_rework = sum(turn.rework for turn in scenario.turns)
    attempt.manual_rework = declared_rework + int(not attempt.passed)
    return attempt


def _evaluate_turn(  # noqa: PLR0912, PLR0915 - expectation vocabulary stays centralized
    turn: BlackBoxTurn,
    *,
    result: Any,
    run: Any,
    events: list[Any],
    turn_index: int,
) -> list[dict[str, Any]]:
    expect = turn.expect
    checks: list[dict[str, Any]] = []
    text = str(result.text or "")
    event_types = [str(event.event_type) for event in events]
    tool_starts = [
        event
        for event in events
        if event.tool_name and str(event.event_type).endswith(".start")
    ]
    tool_names = {str(event.tool_name) for event in events if event.tool_name}
    plan = run.plan_json if run is not None and isinstance(run.plan_json, dict) else {}
    capabilities = _plan_values(plan, "capability")
    skills = {
        str(item.get("skill"))
        for item in plan.get("selected_skills") or []
        if isinstance(item, dict) and item.get("skill")
    }
    skills |= _plan_values(plan, "skill") | _plan_values(plan, "selected_skill")

    def check(name: str, actual: Any, wanted: Any, passed: bool) -> None:
        checks.append(
            {
                "turn": turn_index,
                "name": name,
                "passed": bool(passed),
                "actual": actual,
                "expected": wanted,
            }
        )

    if "task_status" in expect:
        actual = str(run.status if run is not None else "missing")
        check("task_status", actual, expect["task_status"], actual == str(expect["task_status"]))
    if "kind" in expect:
        check("kind", result.kind, expect["kind"], result.kind == expect["kind"])
    if "intent_type" in expect:
        actual = str(plan.get("intent_type") or "")
        check("intent_type", actual, expect["intent_type"], actual == expect["intent_type"])
    if values := _strings(expect.get("output_contains")):
        check("output_contains", text, values, all(value in text for value in values))
    if values := _strings(expect.get("output_contains_any")):
        check("output_contains_any", text, values, any(value in text for value in values))
    if values := _strings(expect.get("output_excludes")):
        check("output_excludes", text, values, all(value not in text for value in values))
    if values := _strings(expect.get("tools_include")):
        check("tools_include", sorted(tool_names), values, set(values) <= tool_names)
    if values := _strings(expect.get("tools_exclude")):
        check("tools_exclude", sorted(tool_names), values, not (set(values) & tool_names))
    if values := _strings(expect.get("events_include")):
        check("events_include", event_types, values, set(values) <= set(event_types))
    if values := _strings(expect.get("capabilities_include")):
        check("capabilities_include", sorted(capabilities), values, set(values) <= capabilities)
    if values := _strings(expect.get("skills_include")):
        check("skills_include", sorted(skills), values, set(values) <= skills)
    if values := _strings(expect.get("terminated_reason_exclude")):
        check(
            "terminated_reason_exclude",
            result.terminated_reason,
            values,
            result.terminated_reason not in values,
        )
    if "verification_status" in expect:
        actual = next(
            (
                str(event.event_type).removeprefix("verification.")
                for event in reversed(events)
                if str(event.event_type).startswith("verification.")
            ),
            str(result.verification_status or ""),
        )
        check("verification_status", actual, expect["verification_status"], actual == expect["verification_status"])
    if "max_tool_calls" in expect:
        maximum = int(expect["max_tool_calls"])
        check("max_tool_calls", len(tool_starts), maximum, len(tool_starts) <= maximum)
    if "min_artifacts" in expect:
        actual = len(run.artifact_ids or []) if run is not None else 0
        minimum = int(expect["min_artifacts"])
        check("min_artifacts", actual, minimum, actual >= minimum)
    if "min_submitted_tasks" in expect:
        actual = len(run.submitted_subtask_ids or []) if run is not None else 0
        minimum = int(expect["min_submitted_tasks"])
        check("min_submitted_tasks", actual, minimum, actual >= minimum)
    if expect.get("no_absolute_paths"):
        check("no_absolute_paths", text, True, _ABSOLUTE_PATH.search(text) is None)
    return checks


async def _aggregate_cost(agent: OmniAgent, task_ids: list[str]) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "calls": 0,
        "estimated_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "components": {},
    }
    for task_id in task_ids:
        summary = await agent.tasks.cost_summary(task_id, include_child_tasks=True)
        for key in ("calls", "estimated_calls", "prompt_tokens", "completion_tokens", "total_tokens"):
            totals[key] += int(summary.get(key) or 0)
        totals["cost_usd"] = round(
            float(totals["cost_usd"]) + float(summary.get("cost_usd") or 0.0),
            6,
        )
        for name, values in (summary.get("components") or {}).items():
            bucket = totals["components"].setdefault(name, {})
            for key, value in values.items():
                bucket[key] = bucket.get(key, 0) + value
    return totals


def isolated_eval_settings(source: OmniSettings, root: Path, scenario_id: str) -> OmniSettings:
    """Clone settings onto a fresh workspace while retaining provider config."""
    workspace = root / "workspace"
    project = root / "omni" / "projects" / _slug(scenario_id)
    workspace.mkdir(parents=True, exist_ok=True)
    paths = OmniPaths(
        home=root / "omni",
        project_name=_slug(scenario_id),
        project_dir=project,
        workspace_root=workspace,
    )
    return source.model_copy(deep=True, update={"paths": paths})


def _build_eval_channel(
    name: str,
    *,
    settings: OmniSettings,
    agent: OmniAgent,
    external_key: str,
) -> Callable[[str, bool], Awaitable[tuple[Any, Any]]]:
    """Exercise the real IM command/presentation/delivery boundary in-memory."""
    from omni.channels.base import Channel
    from omni.channels.security import add_allowed_external_key

    settings.paths.ensure_dirs()
    add_allowed_external_key(settings.paths.channels_dir / f"{name}.toml", external_key)

    class CapturingAgent:
        def __init__(self, target: OmniAgent) -> None:
            self.target = target
            self.last_result: Any = None

        def __getattr__(self, attr: str) -> Any:
            return getattr(self.target, attr)

        async def handle_turn(self, *args: Any, **kwargs: Any) -> Any:
            self.last_result = await self.target.handle_turn(*args, **kwargs)
            return self.last_result

    class EvalChannel(Channel):
        async def start(self) -> None:
            return None

    capture = CapturingAgent(agent)
    channel = EvalChannel(settings, capture)  # type: ignore[arg-type]
    channel.name = name

    async def run(user: str, drain_tasks: bool) -> tuple[Any, Any]:
        capture.last_result = None
        presentation = await channel.handle_inbound_and_send(user, external_key)
        if capture.last_result is None:
            raise RuntimeError(f"channel command did not enter OmniAgent.handle_turn: {user[:80]}")
        workflow_ids = list(
            getattr(capture.last_result, "submitted_workflow_ids", []) or []
        )
        execution_ids = list(capture.last_result.submitted_subtask_ids or [])
        if drain_tasks and (workflow_ids or execution_ids):
            from omni.runtime.notifications import TaskNotification

            await agent.runtime.drain()
            for subtask_id in execution_ids:
                task = await agent.runtime.get_subtask(subtask_id)
                if task is None:
                    continue
                await channel.send_task_notification(
                    TaskNotification(
                        subtask_id=task.id,
                        task_id=str(task.task_id or ""),
                        skill_name=task.skill_name,
                        status=task.status,
                        channel=name,
                        session_id=task.session_id,
                        external_key=external_key,
                        summary=str(task.error or (task.result_json or {}).get("summary") or task.status),
                        payload=task.result_json or {},
                    )
                )
            for workflow_run_id in workflow_ids:
                workflow = await agent.runtime.get_workflow_run(workflow_run_id)
                if workflow is None:
                    continue
                await channel.send_task_notification(
                    TaskNotification(
                        subtask_id="",
                        task_id=str(workflow.task_id or ""),
                        skill_name="",
                        status=workflow.status,
                        object_kind="workflow_run",
                        object_id=workflow.id,
                        title="Research workflow",
                        channel=name,
                        session_id=workflow.session_id,
                        external_key=external_key,
                        summary=str(
                            workflow.error
                            or (workflow.result_json or {}).get("summary")
                            or workflow.status
                        ),
                        payload=workflow.result_json or {},
                    )
                )
        return presentation, capture.last_result

    return run


def _coerce_scenario(raw: dict[str, Any], *, path: Path) -> BlackBoxScenario:
    scenario_id = str(raw.get("id") or "").strip()
    if not scenario_id:
        raise ValueError(f"black-box scenario in {path} is missing id")
    forbidden_scenario = sorted(_FORBIDDEN_SCENARIO_KEYS & set(raw))
    if forbidden_scenario:
        raise ValueError(
            f"black-box scenario {scenario_id} cannot contain harness controls: "
            f"{', '.join(forbidden_scenario)}"
        )
    raw_turns = raw.get("turns") or []
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ValueError(f"black-box scenario {scenario_id} must contain turns")
    turns: list[BlackBoxTurn] = []
    for index, raw_turn in enumerate(raw_turns):
        if not isinstance(raw_turn, dict):
            raise ValueError(f"black-box scenario {scenario_id} turn {index} must be a mapping")
        forbidden = sorted(_FORBIDDEN_TURN_KEYS & set(raw_turn))
        if forbidden:
            raise ValueError(
                f"black-box scenario {scenario_id} turn {index} cannot contain "
                f"harness controls: {', '.join(forbidden)}"
            )
        user = str(raw_turn.get("user") or "").strip()
        expect = raw_turn.get("expect")
        if not user or not isinstance(expect, dict) or not expect:
            raise ValueError(f"black-box scenario {scenario_id} turn {index} needs user + expect")
        turns.append(
            BlackBoxTurn(
                user=user,
                expect=dict(expect),
                channel=str(raw_turn["channel"]) if raw_turn.get("channel") else None,
                drain_tasks=bool(raw_turn.get("drain_tasks", True)),
                rework=bool(raw_turn.get("rework", False)),
            )
        )
    return BlackBoxScenario(
        id=scenario_id,
        title=str(raw.get("title") or scenario_id),
        turns=tuple(turns),
        channel=str(raw.get("channel") or "cli"),
        persona=str(raw.get("persona") or ""),
        tags=tuple(str(value) for value in raw.get("tags") or []),
        requires_model=bool(raw.get("requires_model", False)),
        requires_network=bool(raw.get("requires_network", False)),
    )


def _plan_values(plan: dict[str, Any], key: str) -> set[str]:
    values: set[str] = set()
    for step in plan.get("workflow_steps") or []:
        if isinstance(step, dict) and step.get(key):
            values.add(str(step[key]))
    if plan.get(key):
        values.add(str(plan[key]))
    return values


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")[:80] or "scenario"


__all__ = [
    "BlackBoxAttempt",
    "BlackBoxReport",
    "BlackBoxScenario",
    "BlackBoxTurn",
    "bundled_blackbox_scenarios_dir",
    "isolated_eval_settings",
    "load_blackbox_scenarios",
    "run_blackbox_benchmark",
]
