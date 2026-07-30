"""Offline, deterministic execution harness for capability scenarios.

Builds a throwaway :class:`OmniAgent` (mock provider, isolated home), drives the
scenario's turns while feeding the model the planner JSON each turn declares,
and scores the resulting :class:`TurnResult` against the ``expect`` block.

Nothing here touches the user's real workspace or the network.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from omni.core.approval import ApprovalDecision, ApprovalRequest
from omni.core.llm.client import ChatWithToolsResult, LLMClient, ToolCall
from omni.eval.report import CHECK_DIMENSION, CheckOutcome, ScenarioResult
from omni.eval.scenarios import Scenario, ScenarioTurn
from omni.runtime.presentation import turn_presentation_from_result
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind

# Capabilities the runtime satisfies natively (no skill to register for them).
_NATIVE_CAPABILITIES = {"synthesis.final", "qa.grounded", "draft.section", "draft.manuscript"}

_VERIFICATION_ALIASES = {
    "passed": {"passed"},
    "salvaged": {"salvaged"},
    "failed": {"failed"},
    "ok": {"passed", "salvaged"},  # "did not hard-fail"
    "any": {"passed", "salvaged", "failed", ""},
}


class ScenarioLLM(LLMClient):
    """Deterministic planner/ReAct double.

    ``set_plan`` installs the semantic-planner JSON for the *current* turn: the
    planner prompt gets that JSON, any other ``chat`` gets a scripted summary,
    and ``chat_with_tools`` returns a fixed completion so ReAct fallbacks close
    cleanly. ``plan_calls`` counts planner invocations for the current turn.
    """

    def __init__(self) -> None:
        self.model = "scenario"
        self._plan: dict[str, Any] | None = None
        self.plan_calls = 0
        # L4 journey: scripted tool calls emitted on the *first* ReAct step of a
        # turn (so real tools run through the approval gate), then the loop closes.
        self._tool_script: list[ToolCall] = []
        self._tool_script_emitted = False
        # L4 journey (self-review): scripted reviewer verdicts returned in order
        # when the judge is consulted; once exhausted the judge fails open (pass).
        self._review_script: list[dict[str, Any]] = []
        # L4 journey (streaming): the final ReAct answer text for this turn. A
        # longer answer lets the streaming journeys observe progressive deltas.
        self._answer = "Completed."

    def set_plan(self, plan: dict[str, Any] | None) -> None:
        self._plan = plan
        self.plan_calls = 0

    def set_answer(self, text: str) -> None:
        """Install the final ReAct answer for this turn ("" → default)."""
        self._answer = text or "Completed."

    def set_review_script(self, verdicts: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> None:
        """Install the reviewer verdicts the self-review judge should return."""
        self._review_script = [dict(v) for v in (verdicts or [])]

    def set_tool_script(self, calls: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> None:
        """Install the tool calls the ReAct loop should emit this turn."""
        self._tool_script = [
            ToolCall(id=f"call_{i}", name=str(c.get("name", "")), arguments=dict(c.get("args") or {}))
            for i, c in enumerate(calls or [])
        ]
        self._tool_script_emitted = False

    async def chat(self, system: str, user: str, *, temperature: float = 0.3) -> str:
        if "semantic intent planner" in system.lower():
            self.plan_calls += 1
            return json.dumps(self._plan, ensure_ascii=False) if self._plan else "{}"
        # The self-review / reviewer judge asks for a strict {"verdict":…} JSON.
        if '"verdict"' in system and self._review_script:
            return json.dumps(self._review_script.pop(0), ensure_ascii=False)
        # Native synthesis writes through llm.chat; answer with a plausible
        # draft (as a live model would) so scenarios exercise the LLM rung.
        # Key off the exported constant, not prompt prose.
        from omni.runtime.final_synthesis import SYNTHESIS_SYSTEM_PROMPT

        if system == SYNTHESIS_SYSTEM_PROMPT:
            return "# Draft\n\n" + "Grounded synthesis of the upstream workflow results. " * 6
        return f"summary:{user[:40]}"

    async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatWithToolsResult:  # noqa: ANN001
        if self._tool_script and not self._tool_script_emitted:
            self._tool_script_emitted = True
            return ChatWithToolsResult(content="", tool_calls=list(self._tool_script))
        return ChatWithToolsResult(content=self._answer)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t) % 7), 1.0, 0.0, 0.5] for t in texts]


def _scripted_approver(approve: Any):  # noqa: ANN202
    """Build a simulated-owner approver from a scenario turn's ``approve`` value.

    Returns ``None`` when ``approve is None`` so the agent leaves ``approver``
    unset — modelling a non-interactive/remote caller where sensitive tools must
    fail closed.
    """
    if approve is None:
        return None
    token = approve if isinstance(approve, str) else ("session" if approve is True else "deny")
    token = str(token).strip().lower()

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        if token in ("deny", "no", "n", "false"):
            return ApprovalDecision(False, reason="scenario-deny")
        scope = "session" if token in ("session", "s", "true", "yes", "y") else "once"
        return ApprovalDecision(True, scope=scope, reason="scenario-approve")

    return approver


def _fixture_skill(capability: str, *, fail_mode: str = "") -> SkillEntry:
    """An offline echo skill that satisfies ``capability`` so drains complete.

    ``fail_mode`` makes the fixture fail deterministically for the auto-retry
    journeys: ``"transient"`` returns a retryable error (timeout / 503), so the
    task runtime auto-retries it; ``"permanent"`` returns a deterministic error
    (bad input), which must *not* be retried.
    """
    name = "eval-" + capability.replace(".", "-")
    if fail_mode == "transient":
        payload = {"status": "error", "error": "connection timeout: upstream 503 (transient)"}
    elif fail_mode == "permanent":
        payload = {"status": "error", "error": "invalid input: missing required field"}
    else:
        payload = {"status": "ok", "summary": f"ran {name}", "artifacts": []}
    script = f"import sys;sys.stdin.read();print({json.dumps(payload)!r})"
    return SkillEntry(
        name=name,
        description=f"eval fixture satisfying {capability}",
        kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        capabilities=[capability],
        # Explicit fixtures isolate offline execution cases from product
        # providers; provider-routing scenarios intentionally omit them.
        priority=500,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        # This fixture exists specifically to exercise automatic transient
        # recovery. Production now fails closed unless replay safety is an
        # explicit owner-authored contract, so the eval must declare it too.
        execution={"replay_safe": fail_mode == "transient"},
        input_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "query": {"type": "string"},
                "identifier": {"type": "string"},
            },
        },
        output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
    )


@contextlib.contextmanager
def isolated_eval_home() -> Iterator[Path]:
    """Point OMNI_HOME/HOME/CWD at a throwaway dir for one benchmark run.

    Each call uses a fresh ``mkdtemp`` root, so the per-workspace SQLite path is
    unique and the ``get_database`` cache never returns a stale handle across
    scenarios — no explicit reset needed.
    """
    saved_env = {k: os.environ.get(k) for k in ("OMNI_HOME", "HOME", "CODEX_HOME")}
    saved_cwd = os.getcwd()
    tmp = Path(tempfile.mkdtemp(prefix="omni-eval-"))
    (tmp / "home").mkdir(parents=True, exist_ok=True)
    try:
        os.environ["OMNI_HOME"] = str(tmp / "omni")
        os.environ["HOME"] = str(tmp / "home")
        os.environ["CODEX_HOME"] = str(tmp / "home" / ".codex")
        os.chdir(tmp)
        yield tmp
    finally:
        os.chdir(saved_cwd)
        for key, val in saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


async def _build_agent():  # noqa: ANN202 - OmniAgent imported lazily to avoid import cost
    from omni.agent.orchestrator import OmniAgent
    from omni.config import load_settings

    settings = load_settings(overrides={"model": {"provider": "mock"}})
    settings.paths.ensure_dirs()
    return await OmniAgent.create(settings)


def _register_fixtures(agent, capabilities: tuple[str, ...]) -> None:  # noqa: ANN001
    for cap in capabilities:
        if cap in _NATIVE_CAPABILITIES:
            continue
        agent.registry.register(_fixture_skill(cap))


def _unique_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return stable, de-duplicated step evidence without changing its source."""
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        key = (
            str(step.get("id") or step.get("step_id") or ""),
            str(step.get("skill_name") or ""),
            str(step.get("capability") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(step)
    return unique


async def _collect_routing_steps(agent, result) -> list[dict[str, Any]]:  # noqa: ANN001
    """Read provider choices from the durable, validated intent plan only."""
    steps: list[dict[str, Any]] = []
    task = await agent.tasks.get_task(getattr(result, "task_id", ""))
    plan = task.plan_json if task is not None and isinstance(task.plan_json, dict) else {}
    for selection in plan.get("selected_skills") or []:
        if not isinstance(selection, dict):
            continue
        matched = selection.get("matched_capabilities") or [""]
        for capability in matched:
            steps.append(
                {
                    "skill_name": str(selection.get("skill") or ""),
                    "capability": str(capability or ""),
                }
            )
    for raw_step in plan.get("workflow_steps") or []:
        if isinstance(raw_step, dict):
            steps.append(dict(raw_step))
    return _unique_steps(steps)


async def _collect_execution_steps(agent, result) -> list[dict[str, Any]]:  # noqa: ANN001
    """Read work that actually started; submission and plans are not execution."""
    steps: list[dict[str, Any]] = []
    for workflow_run_id in getattr(result, "submitted_workflow_ids", []) or []:
        for step in await agent.runtime.list_workflow_steps(workflow_run_id):
            if not _execution_started(step):
                continue
            steps.append(
                {
                    "id": str(getattr(step, "step_key", "") or ""),
                    "skill_name": str(getattr(step, "skill_name", "") or ""),
                    "capability": str(getattr(step, "capability", "") or ""),
                    "status": str(getattr(step, "status", "") or ""),
                }
            )
    for subtask_id in result.submitted_subtask_ids:
        task = await agent.runtime.get_subtask(subtask_id)
        if task is not None and _execution_started(task):
            entry = agent.registry.get(task.skill_name)
            steps.append({
                "skill_name": task.skill_name,
                "capability": (entry.capabilities[0] if entry and entry.capabilities else ""),
                "status": str(getattr(task, "status", "") or ""),
            })
    for drained in result.drained_results:
        if not isinstance(drained, dict) or not _execution_started(drained):
            continue
        skill_name = str(drained.get("skill") or "")
        if skill_name:
            entry = agent.registry.get(skill_name)
            steps.append(
                {
                    "skill_name": skill_name,
                    "capability": (
                        entry.capabilities[0]
                        if entry is not None and entry.capabilities
                        else ""
                    ),
                    "status": str(drained.get("status") or ""),
                }
            )
    return _unique_steps(steps)


def _execution_started(value: Any) -> bool:
    """Distinguish real attempts from pending/skipped durable declarations."""
    if _admission_rejected(value):
        return False
    status = str(
        value.get("status", "")
        if isinstance(value, dict)
        else getattr(value, "status", "")
    ).lower()
    if status in {"running", "succeeded", "degraded", "failed"}:
        return True
    started_at = (
        value.get("started_at")
        if isinstance(value, dict)
        else getattr(value, "started_at", None)
    )
    return started_at is not None


def _admission_rejected(value: Any) -> bool:
    """Gateway admission is an attempted route, not provider execution."""
    if isinstance(value, dict):
        payload = value.get("result_json") or value.get("result") or value
    else:
        payload = getattr(value, "result_json", None)
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("action_required"), dict):
        return True
    nested = payload.get("result")
    return isinstance(nested, dict) and isinstance(nested.get("action_required"), dict)


def _safety_outcome(want: str, approval_events: list[str]) -> tuple[bool, str]:
    """Judge a ``safety`` expectation from the run's approval-gate events.

    A sensitive call is *permitted* only when the gate emits ``approval.granted``
    or ``approval.auto``. Anything else means it did not run: the owner denied it
    (``approval.denied``) or — for a remote/non-interactive caller — the tool was
    never offered (no event, so the ReAct loop rejected it), which is still
    "blocked". So:

    * ``allowed`` / ``granted`` ⇒ a grant event fired.
    * ``auto``                  ⇒ the allowlist/session fast-path fired.
    * ``blocked`` / ``denied``  ⇒ no grant fired (denied or unavailable).
    """
    events = set(approval_events)
    granted = bool(events & {"approval.granted", "approval.auto"})
    want = want.strip().lower()
    detail = f"approval_events={sorted(events)} want={want}"
    if want in ("allowed", "granted"):
        return granted, detail
    if want == "auto":
        return "approval.auto" in events, detail
    return (not granted), detail  # blocked / denied


async def _seed_skill_tasks(agent, seeds: tuple[dict[str, Any], ...]) -> None:  # noqa: ANN001
    """Seed historical skill-task outcomes so the evolution loop has signal.

    Each seed is ``{skill, status, goal, error, count}``. A referenced skill is
    registered (minimally) if absent so ``propose_improvements`` can resolve it —
    modelling "this installed skill has been failing".
    """
    from datetime import UTC, datetime, timedelta

    from omni.storage.models import SubtaskORM

    base = datetime.now(UTC)
    idx = 0
    async with agent.db.session() as s:
        for seed in seeds:
            name = str(seed.get("skill") or "").strip()
            status = str(seed.get("status") or "succeeded")
            goal = str(seed.get("goal") or name or "task")
            err = str(seed.get("error") or "")
            count = max(1, int(seed.get("count") or 1))
            if name and agent.registry.get(name) is None:
                agent.registry.register(SkillEntry(name=name, description=f"seeded {name}"))
            for _ in range(count):
                s.add(SubtaskORM(
                    skill_name=name, status=status,
                    input_json={"goal": goal},
                    result_json={"summary": f"done: {goal[:20]}"},
                    error=err,
                    created_at=base + timedelta(seconds=idx),
                ))
                idx += 1
        await s.commit()


async def _evolution_outcome(agent, want: str) -> tuple[bool, str]:  # noqa: ANN001
    """Run the self-evolution loop and judge an ``expect.evolution`` value.

    * ``improves:<skill>`` ⇒ an improvement proposal was queued for ``<skill>``.
    * ``proposes_new``     ⇒ at least one new-skill candidate was queued.
    * ``none``             ⇒ nothing queued (insufficient signal — no over-fit).
    """
    from omni.skills_runtime.proposals import generate_and_enqueue

    summary = await generate_and_enqueue(agent.db, agent.registry, agent.paths, llm=agent.llm)
    added = summary.get("added", [])
    queued = {(p.kind, p.skill_name) for p in added}
    want = want.strip().lower()
    detail = f"queued={sorted(f'{k}:{n}' for k, n in queued)} want={want}"
    if want.startswith("improves:"):
        target = want.split(":", 1)[1].strip()
        return (("improve_skill", target) in queued), detail
    if want in ("proposes_new", "new"):
        return any(kind == "new_skill" for kind, _ in queued), detail
    if want in ("none", "empty"):
        return len(added) == 0, detail
    return False, detail + " (unknown want)"


async def _provenance_capsule_outcome(agent, task_id: str, want: str) -> tuple[bool, str]:  # noqa: ANN001
    """Judge an ``expect.provenance_capsule`` value from the run's capsule events.

    * ``complete``/``grounded`` ⇒ a ``provenance.capsule`` event with
      ``complete=true`` fired (artifact bound to ≥1 source/claim/evidence).
    * ``none``/``hollow``       ⇒ no grounded capsule (a hollow, citation-less
      capsule must not count as grounding).
    """
    events = []
    if task_id:
        try:
            events = await agent.tasks.list_events(task_id)
        except Exception:  # noqa: BLE001
            events = []
    capsules = [e for e in events if str(e.event_type) == "provenance.capsule"]
    grounded = any((e.output_json or {}).get("complete") for e in capsules)
    want = want.strip().lower()
    detail = f"capsules={len(capsules)} grounded={grounded} want={want}"
    if want in ("complete", "grounded", "any"):
        return grounded, detail
    return (not grounded), detail  # none / hollow


async def _self_review_outcome(agent, task_id: str, want: str) -> tuple[bool, str]:  # noqa: ANN001
    """Judge an ``expect.self_review`` value from the run's ``self_review`` event.

    * ``revised``  ⇒ the judge asked for a revision (revises ≥ 1) — the loop
      self-corrected before presenting.
    * ``passed``   ⇒ the answer was accepted first try (revises == 0, accept).
    * ``bounded``  ⇒ revision was needed but the loop stopped at the cap without
      silently accepting a bad answer (no infinite revise loop).
    * ``none``     ⇒ no self-review ran (disabled / non-substantive turn).
    """
    events = []
    if task_id:
        try:
            events = await agent.tasks.list_events(task_id)
        except Exception:  # noqa: BLE001
            events = []
    reviews = [e for e in events if str(e.event_type) == "self_review"]
    want = want.strip().lower()
    if not reviews:
        return (want == "none"), f"self_review events=0 want={want}"
    out = reviews[-1].output_json or {}
    revises = int(out.get("revises") or 0)
    action = str(out.get("action") or "")
    detail = f"revises={revises} action={action} want={want}"
    if want == "revised":
        return revises >= 1, detail
    if want == "passed":
        return revises == 0 and action == "accept", detail
    if want == "bounded":
        return revises >= 1 and action != "accept", detail
    return (want == "none") and not reviews, detail


async def _fake_connector_get_json(url: str, params: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """Deterministic offline stand-in for connector HTTP so ``search_literature``
    L4 journeys never touch the network (mirrors the connector unit-test fakes)."""
    q = str(params.get("search") or params.get("query") or params.get("term") or "")[:30]
    if "openalex" in url:
        return {"results": [{
            "title": f"OpenAlex: {q}", "publication_year": 2020,
            "doi": "https://doi.org/10.1/oa", "id": "https://openalex.org/W1",
            "authorships": [{"author": {"display_name": "A Researcher"}}],
        }]}
    if "crossref" in url:
        return {"message": {"items": [{
            "title": ["Crossref paper"], "DOI": "10.2/cr",
            "issued": {"date-parts": [[2021]]}, "URL": "https://doi.org/10.2/cr",
        }]}}
    if "esearch" in url:
        return {"esearchresult": {"idlist": ["111"]}}
    if "esummary" in url:
        return {"result": {"111": {"title": "PubMed paper", "authors": [{"name": "B Author"}],
                                   "pubdate": "2019", "articleids": [], "fulljournalname": "J"}}}
    if "semanticscholar" in url:
        return {"data": [{"title": "S2 paper", "year": 2022, "authors": [{"name": "C"}],
                          "externalIds": {"DOI": "10.3/s2"}}]}
    return {}


async def _literature_outcome(agent, want: str) -> tuple[bool, str]:  # noqa: ANN001
    """Judge an ``expect.literature`` value by how many sources were ingested.

    * ``indexed`` ⇒ ≥1 source landed in the research store (a live connector hit
      was normalised into the corpus).
    * ``none``    ⇒ nothing indexed (disabled/unknown source → graceful no-op).
    """
    from sqlalchemy import func, select

    from omni.storage.models import SourceORM

    try:
        async with agent.db.session() as s:
            count = (await s.execute(select(func.count()).select_from(SourceORM))).scalar_one()
    except Exception as exc:  # noqa: BLE001
        return False, f"source count failed: {exc}"
    want = want.strip().lower()
    detail = f"sources={count} want={want}"
    if want in ("indexed", "any"):
        return count >= 1, detail
    return count == 0, detail  # none


async def _compute_outcome(agent, task_id: str, want: str) -> tuple[bool, str]:  # noqa: ANN001
    """Judge an ``expect.compute`` value from the run's ``compute.run`` events.

    Offloading is sensitive and easy to lose track of, so we score the durable
    ledger the tool writes (which backend ran, and whether it was a *transparent*
    fallback rather than a silent remote escape):

    * ``local``/``docker``/``ssh``/… ⇒ a run executed on that backend.
    * ``fell_back``                  ⇒ a requested remote backend degraded to
      local (recorded as such — auditable, not silent).
    * ``ran``/``any``                ⇒ at least one compute run succeeded.
    """
    events = []
    if task_id:
        try:
            events = await agent.tasks.list_events(task_id)
        except Exception:  # noqa: BLE001
            events = []
    runs = [e for e in events if str(e.event_type) == "compute.run"]
    payloads = [e.output_json or {} for e in runs]
    want = want.strip().lower()
    detail = f"compute_runs={[p.get('backend') for p in payloads]} want={want}"
    if not payloads:
        return False, detail
    if want in ("ran", "any"):
        return any(p.get("status") in ("ok", "submitted") for p in payloads), detail
    if want in ("fell_back", "fallback"):
        return any(p.get("fell_back") for p in payloads), detail
    return any(str(p.get("backend")) == want for p in payloads), detail


async def _seed_memories(agent, count: int) -> None:  # noqa: ANN001
    """Bulk-insert ``count`` synthetic project findings into memory.

    Direct ORM insert (no embed/dedup) so a scenario can cheaply stand up a
    realistic — or deliberately oversized — store to exercise recall's bound.
    """
    if count <= 0:
        return
    from datetime import UTC, datetime, timedelta

    from omni.memory.service import MemoryLayer
    from omni.storage.models import MemoryEntryORM

    base = datetime.now(UTC)
    async with agent.db.session() as s:
        for i in range(count):
            s.add(MemoryEntryORM(
                principal="local", layer=MemoryLayer.SEMANTIC.value, scope="project",
                memory_type="finding", summary=f"finding number {i}",
                importance=0.5, created_at=base + timedelta(seconds=i),
            ))
        await s.commit()


async def _recall_outcome(agent, want: str) -> tuple[bool, str]:  # noqa: ANN001
    """Judge an ``expect.recall`` value against a deliberately huge ``limit``.

    * ``bounded`` ⇒ recall returns ≤ ``recall_candidate_limit`` even when asked
      for far more — a large/adversarial limit can't scan or exfiltrate the whole
      store.
    * ``functional``/``any`` ⇒ recall still surfaces stored memories, so the
      bound does not prevent relevance from improving through use.
    """
    cap = int(getattr(agent.settings.memory, "recall_candidate_limit", 200) or 200)
    try:
        results = await agent.memory.recall("finding", cross_session=True, limit=100_000)
    except Exception as exc:  # noqa: BLE001
        return False, f"recall failed: {exc}"
    n = len(results)
    want = want.strip().lower()
    detail = f"recalled={n} cap={cap} want={want}"
    if want in ("bounded",):
        return n <= cap, detail
    if want in ("functional", "any"):
        return n >= 1, detail
    return False, detail


async def _submit_failing_task(agent, spec: dict, *, session_id: str, task_id: str) -> None:  # noqa: ANN001
    """Enqueue a deterministically-failing fixture task and drain it once.

    Exercises the *real* ``SubtaskRuntime`` retry path (inline auto-retry inside
    ``process``): a ``transient`` fixture is auto-retried, a ``permanent`` one is
    not. Events attach to ``task_id`` so :func:`_retry_outcome` can inspect them.
    """
    capability = str(spec.get("capability") or spec.get("skill") or "review.paper")
    mode = str(spec.get("mode") or "transient")
    skill_name = "eval-" + capability.replace(".", "-")
    if agent.registry.get(skill_name) is None:
        agent.registry.register(_fixture_skill(capability, fail_mode=mode))
    await agent.runtime.enqueue(
        skill_name,
        dict(spec.get("input") or {"input": "go"}),
        "",  # foreground (no notify channel) — drained inline below
        session_id=session_id,
        task_id=task_id,
    )
    await agent.runtime.drain()


async def _retry_outcome(agent, task_id: str, want: str) -> tuple[bool, str]:  # noqa: ANN001
    """Judge an ``expect.retry`` value from the run's ``task.retry`` events.

    Auto-retry must fire *only* for genuinely transient failures and self-heal a
    flaky task — never mask a deterministic one:

    * ``auto``/``retried`` ⇒ ≥1 auto ``task.retry`` event (transient self-heal).
    * ``none``/``!auto``   ⇒ no auto retry (deterministic failure fails fast).
    * ``exhausted``        ⇒ retried then still failed (bounded, not infinite).
    """
    events = []
    if task_id:
        try:
            events = await agent.tasks.list_events(task_id)
        except Exception:  # noqa: BLE001
            events = []
    retries = [
        e.output_json or {}
        for e in events
        if str(e.event_type) == "subtask.retry" and (e.output_json or {}).get("auto")
    ]
    failed = any(str(e.event_type) == "subtask.failed" for e in events)
    want = want.strip().lower()
    detail = f"auto_retries={len(retries)} failed={failed} want={want}"
    if want in ("auto", "retried"):
        return bool(retries), detail
    if want in ("none", "!auto", "no_retry"):
        return not retries, detail
    if want in ("exhausted",):
        return bool(retries) and failed, detail
    return False, detail


def _streaming_outcome(chunks: list[str], final_text: str, want: str) -> tuple[bool, str]:
    """Judge ``expect.streaming`` from the deltas captured during the turn.

    Streaming must render progressively *and* deliver the whole answer intact:

    * ``progressive``/``any`` ⇒ ≥2 deltas arrived (the answer forms incrementally,
      not in one lump).
    * ``complete``            ⇒ concatenating the deltas reproduces the final
      answer verbatim — a stream never silently drops or mangles content.
    """
    joined = "".join(chunks)
    want = want.strip().lower()
    detail = f"chunks={len(chunks)} streamed_len={len(joined)} final_len={len(final_text or '')} want={want}"
    if want in ("progressive", "any", "incremental"):
        return len(chunks) >= 2 and bool(joined.strip()), detail
    if want in ("complete", "lossless"):
        return bool(joined.strip()) and joined.strip() == (final_text or "").strip(), detail
    return False, detail


async def _schedule_outcome(agent, spec: dict, *, session_id: str, want: str) -> tuple[bool, str]:  # noqa: ANN001
    """Create a schedule, tick it, and judge ``expect.schedule``.

    Drives the *real* :class:`~omni.runtime.scheduler.Scheduler`: a schedule set
    due in the past must fire (materialise a task); one set due in the future
    must not; an interval schedule must re-arm for the future after firing.

    * ``fired``     ⇒ the due schedule enqueued a task (``run_count`` bumped).
    * ``recurring`` ⇒ fired *and* re-armed (still enabled, next fire in future).
    * ``not_due``   ⇒ a not-yet-due schedule does **not** fire prematurely.
    """
    from datetime import UTC, datetime, timedelta

    capability = str(spec.get("capability") or spec.get("skill") or "review.paper")
    kind = str(spec.get("kind") or "interval")
    interval_s = int(spec.get("interval_s") or 60)
    cron_expr = str(spec.get("cron") or "")
    skill_name = "eval-" + capability.replace(".", "-")
    if agent.registry.get(skill_name) is None:
        agent.registry.register(_fixture_skill(capability))

    now = datetime.now(UTC)
    want = want.strip().lower()
    # not_due → arm in the future; otherwise arm in the past to force a fire.
    first_due = now + timedelta(hours=1) if want == "not_due" else now - timedelta(seconds=1)
    sid = await agent.scheduler.add(
        skill_name, dict(spec.get("input") or {"input": "go"}),
        kind=kind, interval_s=interval_s, cron_expr=cron_expr,
        session_id=session_id, first_due=first_due,
    )
    fired = await agent.scheduler.run_due(now=now)
    sched = await agent.scheduler.get(sid)
    ran = int(getattr(sched, "run_count", 0) or 0)
    detail = f"fired={len(fired)} run_count={ran} enabled={getattr(sched, 'enabled', None)} want={want}"
    if want == "not_due":
        return (not fired) and ran == 0, detail
    if want == "recurring":
        rearmed = bool(getattr(sched, "enabled", False)) and getattr(sched, "next_due_at", None) is not None
        return bool(fired) and ran >= 1 and rearmed, detail
    if want in ("fired", "any"):
        return bool(fired) and ran >= 1, detail
    return False, detail


async def _fork_outcome(agent, session_id: str, want: str) -> tuple[bool, str]:  # noqa: ANN001
    """Fork ``session_id`` and judge the branch against ``expect.fork``.

    Forking must branch a conversation faithfully *and* independently:

    * ``copied``   ⇒ the fork is a distinct session whose transcript is copied
      from the source and whose ``forked_from`` points back at it.
    * ``isolated`` ⇒ additionally, writing into the fork never mutates the
      source (branches evolve independently — no cross-branch contamination).
    """
    if not session_id:
        return False, "no session to fork"
    src_msgs = await agent.session_messages(session_id)
    new_id = await agent.fork_session(session_id)
    if not new_id or new_id == session_id:
        return False, f"fork returned {new_id!r}"
    fork_msgs = await agent.session_messages(new_id)
    fork_row = await agent.get_session(new_id)
    copied = (
        len(fork_msgs) == len(src_msgs)
        and getattr(fork_row, "forked_from", "") == session_id
    )
    want = want.strip().lower()
    detail = f"src={len(src_msgs)} fork={len(fork_msgs)} forked_from={getattr(fork_row, 'forked_from', '')!r}"
    if want == "copied":
        return copied, detail
    if want == "isolated":
        if not copied:
            return False, detail
        # A write into the branch must not leak back into the source.
        await agent._persist_message(new_id, "user", "branch-only probe")  # noqa: SLF001
        after_src = await agent.session_messages(session_id)
        after_fork = await agent.session_messages(new_id)
        isolated = len(after_src) == len(src_msgs) and len(after_fork) == len(fork_msgs) + 1
        return isolated, f"{detail} after_src={len(after_src)} after_fork={len(after_fork)}"
    return False, detail


async def _cost_outcome(agent, task_id: str, want: str) -> tuple[bool, str]:  # noqa: ANN001
    """Judge an ``expect.cost`` value from the run's ``cost.usage`` events.

    Spend must be *accountable* — every turn that burns the model records a
    ``cost.usage`` event with a positive token count and a non-negative cost — so
    runaway usage is visible rather than silent.

    * ``recorded``/``any`` ⇒ at least one cost event with tokens > 0 and cost ≥ 0.
    """
    events = []
    if task_id:
        try:
            events = await agent.tasks.list_events(task_id)
        except Exception:  # noqa: BLE001
            events = []
    costs = [e.output_json or {} for e in events if str(e.event_type) == "cost.usage"]
    want = want.strip().lower()
    detail = f"cost_events={len(costs)} want={want}"
    if not costs:
        return False, detail
    if want in ("recorded", "any"):
        ok = any(
            int(c.get("total_tokens") or 0) > 0 and float(c.get("cost_usd") or 0.0) >= 0.0
            for c in costs
        )
        return ok, detail
    return False, detail


async def _memory_graph_outcome(agent, want: str) -> tuple[bool, str]:  # noqa: ANN001
    """Seed related memories and judge ``expect.memory_graph`` on the graph.

    Drives the *real* :class:`~omni.memory.graph.MemoryGraph` through
    ``MemoryService.record`` (auto-linking) and ``recall`` (spreading activation):

    * ``linked``      ⇒ a fresh memory auto-links to its near-duplicate neighbour
      (an edge is built between related memories from different writes).
    * ``recall_boost``⇒ graph-aware recall surfaces a *tag-linked* memory that
      plain similarity ranks outside the window — cross-session recall in action.
    * ``isolated``    ⇒ traversal is principal-isolated: an IM peer's identical
      memory is never linked to or reached from the owner's (no cross-peer leak),
      while same-principal linking still works.
    """
    from omni.memory.service import MemoryLayer

    want = want.strip().lower()

    async def _rec(summary: str, **kw: Any) -> str:
        return await agent.memory.record(
            layer=MemoryLayer.SEMANTIC, scope="project", memory_type="finding",
            summary=summary, **kw,
        )

    if want in ("linked", "any"):
        await _rec("ribosome translation elongation rate measurement")
        b = await _rec("ribosome translation elongation rate replication")
        neigh = await agent.memory.graph.neighbors(b)
        return bool(neigh), f"edges_from_b={len(neigh)} want={want}"

    if want in ("recall_boost", "boost"):
        # Mock embeddings are hash-based (non-semantic), so score recall by keyword
        # overlap here for a deterministic offline check — real deployments add the
        # semantic signal on top. The graph edge (shared tag) is what carries the
        # cross-session link regardless of embedding quality.
        cfg = agent.settings.memory
        saved_backend = cfg.vector_backend
        cfg.vector_backend = "none"
        try:
            x = await _rec("CRISPR base editing off-target profile", tags=["crispr-project"], importance=0.6)
            y = await _rec("weekly lab logistics and freezer inventory", tags=["crispr-project"], importance=0.5)
            for phrase in (
                "photosynthesis chlorophyll absorption spectrum",
                "volcanic basalt geochemistry sampling",
                "neutrino oscillation baseline experiment",
                "polymer viscosity temperature dependence",
                "glacier mass balance annual survey",
            ):
                await _rec(phrase, importance=0.6)  # distractors outrank y on the query
            query = "CRISPR base editing off-target"
            cfg.graph_enabled = False  # flat recall: the tag-linked note stays out of window
            off = {sm.entry.id for sm in await agent.memory.recall(query, limit=3)}
            cfg.graph_enabled = True  # graph recall: spreading activation surfaces it
            on = {sm.entry.id for sm in await agent.memory.recall(query, limit=3)}
        finally:
            cfg.vector_backend = saved_backend
        surfaced = (x in on) and (y in on) and (y not in off)
        return surfaced, f"x={x[:6]} y_on={y in on} y_off={y in off} want={want}"

    if want in ("isolated", "bounded"):
        owner = await _rec("dark matter halo concentration mass relation")
        owner2 = await _rec("dark matter halo concentration mass relation refit")
        peer = await _rec(
            "dark matter halo concentration mass relation refit", principal="feishu:peer"
        )
        neigh_ids = {n.id for n in await agent.memory.graph.neighbors(owner, principal="local")}
        boosts = await agent.memory.graph.spread([owner], principal="local")
        leaked = peer in neigh_ids or peer in boosts
        own_linked = owner2 in neigh_ids
        return (own_linked and not leaked), (
            f"own_linked={own_linked} peer_leaked={leaked} neigh={len(neigh_ids)} want={want}"
        )

    return False, f"unknown want={want}"


def _skill_routing_outcome(steps: list[dict[str, Any]], want: str) -> tuple[bool, str]:
    """Judge provider selection from the durable validated routing plan.

    Catalog search is intentionally not an intent router. The benchmark must
    exercise the same semantic-plan and contract arbitration path as production.
    """
    names = {str(step.get("skill_name")) for step in steps if step.get("skill_name")}
    want = want.strip()
    if want.startswith("!"):
        target = want[1:].strip()
        return target not in names, f"selected={sorted(names)} must-not={target}"
    return want in names, f"selected={sorted(names)} want={want}"


def _find_action_required(value: Any) -> dict[str, Any] | None:
    """Find the gateway action in executed result data, including workflow steps."""
    if isinstance(value, dict):
        action = value.get("action_required")
        if isinstance(action, dict):
            return action
        for nested in value.values():
            found = _find_action_required(nested)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _find_action_required(nested)
            if found is not None:
                return found
    return None


def _action_required_outcome(result: Any, want: Any) -> tuple[bool, str]:
    """Judge the transport-neutral action returned at actual execution time."""
    payloads = list(getattr(result, "drained_results", ()) or ())
    payloads.extend(
        record.result
        for record in (getattr(result, "tool_trace", ()) or ())
        if getattr(record, "result", None) is not None
    )
    action = _find_action_required(payloads)
    if not isinstance(want, dict):
        return action is not None, f"action_required={action!r}"
    mismatches = {
        str(key): {"actual": None if action is None else action.get(key), "want": expected}
        for key, expected in want.items()
        if action is None or action.get(key) != expected
    }
    return not mismatches, f"action_required={action!r} mismatches={mismatches!r}"


async def _approval_event_kinds(agent, task_id: str) -> list[str]:  # noqa: ANN001
    """Approval-gate event types recorded for a run (``approval.*``)."""
    if not task_id:
        return []
    try:
        events = await agent.tasks.list_events(task_id)
    except Exception:  # noqa: BLE001 — absence of events is just "no approval".
        return []
    return [e.event_type for e in events if str(e.event_type).startswith("approval.")]


def _evaluate_turn(
    scenario_id: str,
    turn: ScenarioTurn,
    result,  # noqa: ANN001
    *,
    routing_steps: list[dict[str, Any]],
    execution_steps: list[dict[str, Any]],
    llm: ScenarioLLM,
    channel: str,
    approval_events: list[str],
) -> list[CheckOutcome]:
    checks: list[CheckOutcome] = []
    routed_caps = {
        str(step.get("capability"))
        for step in routing_steps
        if step.get("capability")
    }
    executed_caps = {
        str(step.get("capability"))
        for step in execution_steps
        if step.get("capability")
    }
    executed_skills = {
        str(step.get("skill_name"))
        for step in execution_steps
        if step.get("skill_name")
    }
    routed_skills = {
        str(step.get("skill_name"))
        for step in routing_steps
        if step.get("skill_name")
    }

    def add(key: str, passed: bool, detail: str) -> None:
        checks.append(CheckOutcome(scenario_id, CHECK_DIMENSION.get(key, key), key, passed, detail))

    exp = turn.expect
    if "kind" in exp:
        add("kind", result.kind == exp["kind"], f"kind={result.kind} want={exp['kind']}")
    if "terminated_reason" in exp:
        add(
            "terminated_reason",
            result.terminated_reason == exp["terminated_reason"],
            f"reason={result.terminated_reason} want={exp['terminated_reason']}",
        )
    if "planner_calls" in exp:
        add("planner_calls", llm.plan_calls == int(exp["planner_calls"]),
            f"planner_calls={llm.plan_calls} want={exp['planner_calls']}")
    if "capabilities_include" in exp:
        for cap in exp["capabilities_include"]:
            add(
                "capabilities_include",
                cap in routed_caps,
                f"{cap} in routed={sorted(routed_caps)}",
            )
    if "capabilities_executed" in exp:
        for cap in exp["capabilities_executed"]:
            add(
                "capabilities_executed",
                cap in executed_caps,
                f"{cap} in executed={sorted(executed_caps)}",
            )
    if "skills_include" in exp:
        for name in exp["skills_include"]:
            add(
                "skills_include",
                name in routed_skills,
                f"{name} in routed={sorted(routed_skills)}",
            )
    if "skills_exclude" in exp:
        for name in exp["skills_exclude"]:
            add(
                "skills_exclude",
                name not in routed_skills,
                f"{name} not in routed={sorted(routed_skills)}",
            )
    if "skills_executed_include" in exp:
        for name in exp["skills_executed_include"]:
            add(
                "skills_executed_include",
                name in executed_skills,
                f"{name} in executed={sorted(executed_skills)}",
            )
    if "skills_executed_exclude" in exp:
        for name in exp["skills_executed_exclude"]:
            add(
                "skills_executed_exclude",
                name not in executed_skills,
                f"{name} not in executed={sorted(executed_skills)}",
            )
    if "skill_selected" in exp:
        ok, detail = _skill_routing_outcome(routing_steps, str(exp["skill_selected"]))
        add("skill_selected", ok, detail)
    if "action_required" in exp:
        ok, detail = _action_required_outcome(result, exp["action_required"])
        add("action_required", ok, detail)
    if "verification" in exp:
        want = str(exp["verification"])
        if want == "any":
            # "verification ran" — exercises the dimension without pinning a
            # verdict (covers passed/salvaged/failed/pending_child_task/"").
            add("verification", True, f"verification={result.verification_status!r} (any)")
        else:
            allowed = _VERIFICATION_ALIASES.get(want, {want})
            add("verification", result.verification_status in allowed,
                f"verification={result.verification_status!r} want∈{sorted(allowed)}")
    if "text_contains" in exp:
        for needle in exp["text_contains"]:
            add("text_contains", needle in result.text, f"{needle!r} in text")
    if "im_hides" in exp:
        markdown = turn_presentation_from_result(result, channel=channel).to_markdown()
        for needle in exp["im_hides"]:
            add("im_hides", needle not in markdown, f"{needle!r} hidden on {channel}")
    if "safety" in exp:
        ok, detail = _safety_outcome(str(exp["safety"]), approval_events)
        add("safety", ok, detail)
    return checks


async def _run_turns_scenario(scenario: Scenario) -> ScenarioResult:
    result = ScenarioResult(scenario.id, scenario.title, scenario.tags)
    with isolated_eval_home():
        # Keep connector HTTP offline+deterministic for search_literature journeys.
        from omni.research import connectors as _connectors
        from omni.runtime import compute as _compute

        _saved_get_json = _connectors._get_json
        _connectors._get_json = _fake_connector_get_json
        # Keep run_compute hermetic: no real subprocess/docker/ssh in the harness.
        _saved_exec = _compute._exec

        async def _fake_exec(  # noqa: ANN001, ANN202
            argv, *, shell, cwd, timeout, cancel_check=None
        ):
            return 0, "STUB-OK"

        _compute._exec = _fake_exec
        agent = await _build_agent()
        _register_fixtures(agent, scenario.fixtures)
        # Exercise the in-execution self-review path (P1): scenarios that script
        # reviewer verdicts drive real revisions; the rest fail open to pass.
        agent.settings.react.self_review = True
        llm = ScenarioLLM()
        agent.llm = llm
        agent.memory._llm = llm  # keep extraction offline+deterministic too
        session_id: str | None = None
        try:
            for turn in scenario.turns:
                llm.set_plan(turn.plan)
                llm.set_tool_script(turn.tool_calls)
                llm.set_review_script(turn.review)
                llm.set_answer(turn.answer)
                agent.approver = _scripted_approver(turn.approve) if turn.tool_calls else None
                if turn.seed_tasks:
                    await _seed_skill_tasks(agent, turn.seed_tasks)
                if turn.seed_memories:
                    await _seed_memories(agent, turn.seed_memories)
                channel = turn.channel or scenario.channel
                stream_chunks: list[str] = []
                want_stream = turn.stream or "streaming" in turn.expect
                turn_result = await agent.handle_turn(
                    turn.user, session_id=session_id, channel=channel, drain_tasks=turn.drain,
                    on_token=(stream_chunks.append if want_stream else None),
                )
                session_id = turn_result.session_id
                routing_steps = await _collect_routing_steps(agent, turn_result)
                execution_steps = await _collect_execution_steps(agent, turn_result)
                approval_events = await _approval_event_kinds(agent, turn_result.task_id)
                result.checks.extend(
                    _evaluate_turn(
                        scenario.id,
                        turn,
                        turn_result,
                        routing_steps=routing_steps,
                        execution_steps=execution_steps,
                        llm=llm,
                        channel=channel,
                        approval_events=approval_events,
                    )
                )
                if "evolution" in turn.expect:
                    ok, detail = await _evolution_outcome(agent, str(turn.expect["evolution"]))
                    result.checks.append(
                        CheckOutcome(scenario.id, "self_evolution", "evolution", ok, detail)
                    )
                if "provenance_capsule" in turn.expect:
                    ok, detail = await _provenance_capsule_outcome(
                        agent, turn_result.task_id, str(turn.expect["provenance_capsule"])
                    )
                    result.checks.append(
                        CheckOutcome(scenario.id, "provenance", "provenance_capsule", ok, detail)
                    )
                if "self_review" in turn.expect:
                    ok, detail = await _self_review_outcome(
                        agent, turn_result.task_id, str(turn.expect["self_review"])
                    )
                    result.checks.append(
                        CheckOutcome(scenario.id, "self_review", "self_review", ok, detail)
                    )
                if "literature" in turn.expect:
                    ok, detail = await _literature_outcome(agent, str(turn.expect["literature"]))
                    result.checks.append(
                        CheckOutcome(scenario.id, "literature", "literature", ok, detail)
                    )
                if "compute" in turn.expect:
                    ok, detail = await _compute_outcome(
                        agent, turn_result.task_id, str(turn.expect["compute"])
                    )
                    result.checks.append(
                        CheckOutcome(scenario.id, "compute", "compute", ok, detail)
                    )
                if "recall" in turn.expect:
                    ok, detail = await _recall_outcome(agent, str(turn.expect["recall"]))
                    result.checks.append(
                        CheckOutcome(scenario.id, "recall", "recall", ok, detail)
                    )
                if "memory_graph" in turn.expect:
                    ok, detail = await _memory_graph_outcome(
                        agent, str(turn.expect["memory_graph"])
                    )
                    result.checks.append(
                        CheckOutcome(scenario.id, "memory_graph", "memory_graph", ok, detail)
                    )
                if "cost" in turn.expect:
                    ok, detail = await _cost_outcome(
                        agent, turn_result.task_id, str(turn.expect["cost"])
                    )
                    result.checks.append(
                        CheckOutcome(scenario.id, "cost", "cost", ok, detail)
                    )
                if turn.submit_task:
                    await _submit_failing_task(
                        agent, turn.submit_task,
                        session_id=session_id or "", task_id=turn_result.task_id,
                    )
                if "retry" in turn.expect:
                    ok, detail = await _retry_outcome(
                        agent, turn_result.task_id, str(turn.expect["retry"])
                    )
                    result.checks.append(
                        CheckOutcome(scenario.id, "retry", "retry", ok, detail)
                    )
                if turn.fork or "fork" in turn.expect:
                    ok, detail = await _fork_outcome(
                        agent, session_id or "", str(turn.expect.get("fork", "copied"))
                    )
                    result.checks.append(
                        CheckOutcome(scenario.id, "fork", "fork", ok, detail)
                    )
                if turn.schedule is not None or "schedule" in turn.expect:
                    ok, detail = await _schedule_outcome(
                        agent, turn.schedule or {}, session_id=session_id or "",
                        want=str(turn.expect.get("schedule", "fired")),
                    )
                    result.checks.append(
                        CheckOutcome(scenario.id, "schedule", "schedule", ok, detail)
                    )
                if turn.stream or "streaming" in turn.expect:
                    ok, detail = _streaming_outcome(
                        stream_chunks, turn_result.text, str(turn.expect.get("streaming", "progressive"))
                    )
                    result.checks.append(
                        CheckOutcome(scenario.id, "streaming", "streaming", ok, detail)
                    )
        except Exception as exc:  # noqa: BLE001 - a crash is a scenario failure, not a harness crash
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            await agent.aclose()
            _connectors._get_json = _saved_get_json
            _compute._exec = _saved_exec
    return result


async def _run_retrieval_scenario(scenario: Scenario) -> ScenarioResult:
    from omni.research.bench import run_retrieval_bench
    from omni.research.store import ResearchStore
    from omni.storage.db import Database

    result = ScenarioResult(scenario.id, scenario.title, scenario.tags)
    tmp = Path(tempfile.mkdtemp(prefix="omni-eval-ret-")) / "bench.sqlite3"
    db = Database(tmp)
    await db.init()
    try:
        bench = await run_retrieval_bench(ResearchStore(db), None, k=scenario.k)
        passed = bench.recall_at_k >= scenario.min_recall
        result.checks.append(
            CheckOutcome(
                scenario.id, "retrieval", "retrieval",
                passed, f"recall@{bench.k}={bench.recall_at_k:.2f} MRR={bench.mrr:.2f} want≥{scenario.min_recall}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        await db.dispose()
    return result


async def run_scenario(scenario: Scenario) -> ScenarioResult:
    """Execute one scenario offline and return its scored result."""
    if scenario.type == "retrieval":
        return await _run_retrieval_scenario(scenario)
    return await _run_turns_scenario(scenario)


__all__ = ["ScenarioLLM", "run_scenario", "isolated_eval_home"]
