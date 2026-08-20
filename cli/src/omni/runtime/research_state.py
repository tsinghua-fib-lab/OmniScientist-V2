"""Task-scoped research state the coordinator can see in the live loop.

The ledger already stores source/claim/evidence/artifact ids on the task.
This module projects a compact snapshot (and later a delta) so the model
acts on this task's facts instead of a workspace-wide brief.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from omni.agent.capabilities import contract_outputs
from omni.runtime.engine_observation import SCHEMA
from omni.runtime.remaining import remaining_deliverables

_REF_LIMITS = {"source": 8, "claim": 8, "evidence": 8, "artifact": 6}
_MAX_DEBT_REPLAYS = 2
_ACTIVE = frozenset({"scheduled", "pending", "running", "recovering"})
_ROUTINE_EVENTS = frozenset({"react.finished", "execution.finished", "plan.executed"})
# Finish-replays are ledger honesty only. Named missing files replay once
# (Codex stop-hook analog); the host still does not write them.
_REPLAYABLE_DEBT_KEYS = frozenset({"unsupported_claims", "full_provenance_empty"})
_REPLAYABLE_DEBT_PREFIXES = (
    "unknown_outcome:",
    "missing_event:",
    "missing_deliverable:",
)


@dataclass(slots=True)
class TaskRef:
    kind: str
    id: str
    title: str = ""

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.id}"

    def label(self) -> str:
        title = (self.title or "").replace("\n", " ").strip()
        if title:
            return f"{self.ref} {title[:80]}"
        return self.ref


@dataclass(slots=True)
class TaskResearchState:
    """Bounded facts about one task. Phases are not represented here."""

    task_id: str
    provenance_mode: str = "light"
    plan_hash: str = ""
    required_outputs: list[str] = field(default_factory=list)
    required_events: list[str] = field(default_factory=list)
    sources: list[TaskRef] = field(default_factory=list)
    claims: list[TaskRef] = field(default_factory=list)
    evidence: list[TaskRef] = field(default_factory=list)
    artifacts: list[TaskRef] = field(default_factory=list)
    missing_deliverables: list[str] = field(default_factory=list)
    unsupported_claims: int = 0
    open_children: int = 0
    unresolved_events: list[str] = field(default_factory=list)
    latest_observation: dict[str, Any] | None = None
    empty_funnel: bool = False
    resumed: bool = False
    unmatched_tools: list[str] = field(default_factory=list)

    @property
    def state_hash(self) -> str:
        observation = self.latest_observation or {}
        payload = {
            "task_id": self.task_id,
            "provenance_mode": self.provenance_mode,
            "plan_hash": self.plan_hash,
            "required_outputs": list(self.required_outputs),
            "required_events": list(self.required_events),
            "sources": [item.ref for item in self.sources],
            "claims": [item.ref for item in self.claims],
            "evidence": [item.ref for item in self.evidence],
            "artifacts": [item.ref for item in self.artifacts],
            "missing_deliverables": list(self.missing_deliverables),
            "unsupported_claims": self.unsupported_claims,
            "open_children": self.open_children,
            "unresolved_events": list(self.unresolved_events),
            "empty_funnel": self.empty_funnel,
            "unmatched_tools": list(self.unmatched_tools),
            "observation": {
                "status": observation.get("status") or "",
                "created_refs": list(observation.get("created_refs") or []),
                "limitations": list(observation.get("limitations") or []),
                "n_kept": (observation.get("metrics") or {}).get("n_kept"),
            },
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def is_empty(self) -> bool:
        return not (
            self.sources
            or self.claims
            or self.evidence
            or self.artifacts
            or self.missing_deliverables
            or self.unsupported_claims
            or self.required_events
            or self.unresolved_events
            or self.open_children
            or self.empty_funnel
            or self.unmatched_tools
            or self.provenance_mode == "full"
        )

    def snapshot_text(self) -> str:
        """Compact opening observation. Empty state yields empty string."""
        if self.is_empty():
            return ""
        lines = [
            "[Task research state]",
            f"task={self.task_id[:8]} provenance={self.provenance_mode or 'light'}",
        ]
        if self.required_outputs:
            lines.append("required outputs: " + ", ".join(self.required_outputs))
        if self.required_events:
            lines.append("required events: " + ", ".join(self.required_events))
        lines.append(_ref_line("sources", self.sources))
        lines.append(_ref_line("claims", self.claims))
        lines.append(_ref_line("evidence", self.evidence))
        lines.append(_ref_line("artifacts", self.artifacts))
        lines.append(
            "missing deliverables: "
            + (", ".join(self.missing_deliverables) if self.missing_deliverables else "none")
        )
        if self.provenance_mode == "full" or self.unsupported_claims:
            lines.append(f"unsupported claims: {self.unsupported_claims}")
        if self.open_children:
            lines.append(f"open children: {self.open_children}")
        if self.unresolved_events:
            lines.append("unresolved events: " + ", ".join(self.unresolved_events))
        if self.unmatched_tools:
            lines.append(
                "interrupted tools (outcome unknown): " + ", ".join(self.unmatched_tools)
            )
        lines.extend(_observation_lines(self.latest_observation))
        lines.append(
            "Only refs and files owned by this task_id satisfy the contract; "
            "another task's artifacts or ROM do not."
        )
        if self.resumed:
            lines.append(
                "Continue this task from the ledger above. Do not call find_skill "
                "and do not repeat a retrieval query already represented in sources "
                "or limitations."
            )
        return "\n".join(lines)

    def delta_against(self, previous: TaskResearchState | None) -> str:
        if previous is None:
            return self.snapshot_text()
        if previous.state_hash == self.state_hash:
            return ""
        added = []
        for _kind, now, was in (
            ("source", self.sources, previous.sources),
            ("claim", self.claims, previous.claims),
            ("evidence", self.evidence, previous.evidence),
            ("artifact", self.artifacts, previous.artifacts),
        ):
            before = {item.ref for item in was}
            for item in now:
                if item.ref not in before:
                    added.append(f"+ {item.label()}")
        lines = ["[Task research state Δ]"]
        lines.extend(added[:12])
        if self.missing_deliverables != previous.missing_deliverables:
            lines.append(
                "missing deliverables: "
                + (", ".join(self.missing_deliverables) if self.missing_deliverables else "none")
            )
        if self.unsupported_claims != previous.unsupported_claims:
            lines.append(f"unsupported claims: {self.unsupported_claims}")
        if self.unresolved_events != previous.unresolved_events:
            lines.append(
                "unresolved events: "
                + (", ".join(self.unresolved_events) if self.unresolved_events else "none")
            )
        if (self.latest_observation or {}) != (previous.latest_observation or {}):
            lines.extend(_observation_lines(self.latest_observation))
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def debt_findings(self) -> list[tuple[str, str]]:
        """Deterministic bookkeeping only — never quality judgement."""
        findings: list[tuple[str, str]] = []
        for name in self.missing_deliverables:
            findings.append(
                (
                    f"missing_deliverable:{name}",
                    f"This task still owes {name} on this task_id. "
                    "Sidecars and files from another task do not count.",
                )
            )
        if self.provenance_mode == "full" and self.unsupported_claims:
            findings.append(
                (
                    "unsupported_claims",
                    f"{self.unsupported_claims} claim(s) on this task have no evidence. "
                    "Full provenance requires add_evidence or an honest unverified mark.",
                )
            )
        if (
            self.provenance_mode == "full"
            and not self.claims
            and not self.missing_deliverables
        ):
            findings.append(
                (
                    "full_provenance_empty",
                    "This task's plan requires full provenance and has no claims yet.",
                )
            )
        writing = {
            name
            for name in self.missing_deliverables
            if "draft" in name or name in {"review", "response_letter"}
        }
        if writing and (not self.sources or self.empty_funnel):
            findings.append(
                (
                    "empty_sources_for_writing",
                    "A writing deliverable is still owed and this task has 0 sources.",
                )
            )
        if writing and self.provenance_mode == "full" and not self.claims:
            findings.append(
                (
                    "full_writing_no_claims",
                    "Full provenance is required and a writing deliverable is still owed, "
                    "but this task has no claims yet. Record claims and bind evidence.",
                )
            )
        for name in self.unmatched_tools:
            findings.append(
                (
                    f"unknown_outcome:{name}",
                    f"Tool {name} was interrupted after it started; its outcome is unknown. "
                    "Retry only if it is read-only or idempotent; otherwise verify state first.",
                )
            )
        for name in self.unresolved_events:
            findings.append(
                (
                    f"missing_event:{name}",
                    f"Required event {name} has not occurred on this task.",
                )
            )
        if self.open_children:
            findings.append(
                (
                    "open_children",
                    f"{self.open_children} submitted child or workflow still active on this task.",
                )
            )
        return findings


def _replayable_debt(key: str) -> bool:
    if key in _REPLAYABLE_DEBT_KEYS:
        return True
    return key.startswith(_REPLAYABLE_DEBT_PREFIXES)


async def opening_research_brief(
    tasks: Any,
    artifacts: Any,
    db: Any | None,
    task_id: str,
) -> str:
    """Opening desk for one task. Empty when the row has no ledger yet."""
    if not task_id:
        return ""
    feed = LiveTaskResearchFeed(
        tasks=tasks,
        artifacts=artifacts,
        db=db,
        task_id=task_id,
    )
    return await feed.opening_snapshot()


class LiveTaskResearchFeed:
    """Snapshot / delta / debt finding for one ReAct turn."""

    def __init__(
        self,
        *,
        tasks: Any,
        artifacts: Any,
        db: Any | None,
        task_id: str,
        plan: Any | None = None,
        resumed: bool = False,
    ) -> None:
        self._tasks = tasks
        self._artifacts = artifacts
        self._db = db
        self._task_id = task_id
        self._plan = plan
        self._resumed = resumed
        self._last: TaskResearchState | None = None
        self._debt_keys: set[str] = set()
        self._debt_replays = 0

    async def opening_snapshot(self) -> str:
        state = await self._load()
        self._last = state
        if state is None:
            return ""
        return state.snapshot_text()

    async def after_tool_batch(self) -> str:
        state = await self._load()
        if state is None:
            return ""
        text = state.delta_against(self._last)
        self._last = state
        return text

    async def after_rollover(self) -> str:
        state = await self._load()
        self._last = state
        if state is None:
            return ""
        return state.snapshot_text()

    async def after_steer(self) -> str:
        """Full desk after a mid-turn steer — not a hash-gated delta."""
        state = await self._load()
        self._last = state
        if state is None:
            return ""
        return state.snapshot_text()

    async def before_text_finish(self) -> str:
        if self._debt_replays >= _MAX_DEBT_REPLAYS:
            return ""
        state = await self._load()
        self._last = state
        if state is None:
            return ""
        pending = [
            (key, message)
            for key, message in state.debt_findings()
            if key not in self._debt_keys and _replayable_debt(key)
        ]
        if not pending:
            return ""
        key, message = pending[0]
        self._debt_keys.add(key)
        self._debt_replays += 1
        return "[Task research finding]\n" + message

    async def _load(self) -> TaskResearchState | None:
        if not self._task_id:
            return None
        try:
            return await load_task_research_state(
                self._tasks,
                self._artifacts,
                self._db,
                self._task_id,
                plan=self._plan,
                resumed=self._resumed,
            )
        except Exception:  # noqa: BLE001 — live loop must not die on projection
            return None


async def load_task_research_state(
    tasks: Any,
    artifacts: Any,
    db: Any | None,
    task_id: str,
    *,
    plan: Any | None = None,
    resumed: bool = False,
) -> TaskResearchState | None:
    """Read the current task row and build a bounded projection."""
    if not task_id or tasks is None:
        return None
    run = await tasks.get_task(task_id)
    if run is None:
        return None
    required, events, provenance, plan_hash = _contract_from(run, plan)
    rows = await artifacts_for_task(artifacts, run)
    missing = remaining_deliverables(required, rows)
    source_ids = [str(item) for item in (getattr(run, "source_ids", None) or []) if item]
    claim_ids = [str(item) for item in (getattr(run, "claim_ids", None) or []) if item]
    evidence_ids = [str(item) for item in (getattr(run, "evidence_ids", None) or []) if item]
    artifact_ids = [str(item) for item in (getattr(run, "artifact_ids", None) or []) if item]
    if not artifact_ids:
        artifact_ids = [str(getattr(row, "id", "") or "") for row in rows if getattr(row, "id", "")]
    unsupported = 0
    source_refs = [TaskRef("source", item) for item in source_ids[: _REF_LIMITS["source"]]]
    claim_refs = [TaskRef("claim", item) for item in claim_ids[: _REF_LIMITS["claim"]]]
    evidence_refs = [TaskRef("evidence", item) for item in evidence_ids[: _REF_LIMITS["evidence"]]]
    artifact_refs = [
        TaskRef(
            "artifact",
            str(getattr(row, "id", "") or ""),
            title=str(getattr(row, "title", "") or getattr(row, "kind", "") or ""),
        )
        for row in rows[: _REF_LIMITS["artifact"]]
        if getattr(row, "id", "")
    ]
    if not artifact_refs:
        artifact_refs = [
            TaskRef("artifact", item) for item in artifact_ids[: _REF_LIMITS["artifact"]]
        ]
    if db is not None:
        try:
            from omni.research.store import ResearchStore

            store = ResearchStore(db)
            source_refs = await _fill_source_titles(store, source_ids)
            claim_refs = await _fill_claim_titles(store, claim_ids)
            counts = await store.evidence_count_by_claim()
            unsupported = sum(1 for item in claim_ids if counts.get(item, 0) == 0)
        except Exception:  # noqa: BLE001
            unsupported = 0
    open_children = await _open_child_count(tasks, run)
    seen_events = await _event_types(tasks, task_id)
    required_events = [item for item in events if item not in _ROUTINE_EVENTS]
    unresolved = [item for item in required_events if item not in seen_events]
    observation = await _latest_observation(tasks, run)
    empty_funnel = _observation_empty_funnel(observation)
    unmatched = await _unmatched_tool_starts(tasks, task_id)
    return TaskResearchState(
        task_id=task_id,
        provenance_mode=provenance,
        plan_hash=plan_hash,
        required_outputs=contract_outputs(required),
        required_events=required_events,
        sources=source_refs,
        claims=claim_refs,
        evidence=evidence_refs,
        artifacts=artifact_refs,
        missing_deliverables=missing,
        unsupported_claims=unsupported,
        open_children=open_children,
        unresolved_events=unresolved,
        latest_observation=observation,
        empty_funnel=empty_funnel,
        resumed=resumed or bool(getattr(run, "retry_of_task_id", "")),
        unmatched_tools=unmatched,
    )


def _contract_from(run: Any, plan: Any | None) -> tuple[list[str], list[str], str, str]:
    payload = getattr(run, "plan_json", None)
    if not isinstance(payload, dict):
        payload = {}
    verification = payload.get("verification_plan") if isinstance(payload.get("verification_plan"), dict) else {}
    required = list(verification.get("required_outputs") or payload.get("outputs") or [])
    events = [str(item) for item in (verification.get("required_events") or []) if item]
    provenance = str(getattr(run, "provenance_mode", "") or payload.get("provenance_mode") or "light")
    if plan is not None:
        plan_required = list(getattr(getattr(plan, "verification_plan", None), "required_outputs", None) or [])
        if plan_required:
            required = plan_required
        plan_events = list(getattr(getattr(plan, "verification_plan", None), "required_events", None) or [])
        if plan_events:
            events = [str(item) for item in plan_events if item]
        plan_mode = str(getattr(plan, "provenance_mode", "") or "")
        if plan_mode:
            provenance = plan_mode
    if provenance not in {"light", "full"}:
        provenance = "light"
    fingerprint = str(getattr(run, "current_authority_fingerprint", "") or "")
    return required, events, provenance, fingerprint


async def _fill_source_titles(store: Any, ids: list[str]) -> list[TaskRef]:
    refs: list[TaskRef] = []
    for item in ids[: _REF_LIMITS["source"]]:
        row = await store.get_source(item)
        title = str(getattr(row, "title", "") or "") if row is not None else ""
        refs.append(TaskRef("source", item, title=title))
    return refs


async def _fill_claim_titles(store: Any, ids: list[str]) -> list[TaskRef]:
    refs: list[TaskRef] = []
    getter = getattr(store, "get_claim", None)
    for item in ids[: _REF_LIMITS["claim"]]:
        title = ""
        if callable(getter):
            row = await getter(item)
            title = str(getattr(row, "text", "") or "") if row is not None else ""
        refs.append(TaskRef("claim", item, title=title))
    return refs


async def _open_child_count(tasks: Any, run: Any) -> int:
    sub_ids = [str(item) for item in (getattr(run, "submitted_subtask_ids", None) or []) if item]
    wf_ids = [str(item) for item in (getattr(run, "submitted_workflow_ids", None) or []) if item]
    count = 0
    list_subs = getattr(tasks, "list_subtasks_by_ids", None)
    list_wfs = getattr(tasks, "list_workflows_by_ids", None)
    try:
        if callable(list_subs) and sub_ids:
            rows = await list_subs(sub_ids)
            count += sum(1 for row in rows if str(getattr(row, "status", "")) in _ACTIVE)
        if callable(list_wfs) and wf_ids:
            rows = await list_wfs(wf_ids)
            count += sum(1 for row in rows if str(getattr(row, "status", "")) in _ACTIVE)
    except Exception:  # noqa: BLE001
        return 0
    return count


async def artifacts_for_task(artifacts: Any, run: Any) -> list[Any]:
    """Files owned by this task row, including inherited ``artifact_ids``."""
    task_id = str(getattr(run, "id", "") or "")
    rows: list[Any] = []
    if artifacts is not None and task_id:
        try:
            rows = list(await artifacts.list_by_task(task_id))
        except Exception:  # noqa: BLE001
            rows = []
    seen = {str(getattr(row, "id", "") or "") for row in rows}
    getter = getattr(artifacts, "get", None)
    for item in getattr(run, "artifact_ids", None) or []:
        art_id = str(item or "")
        if not art_id or art_id in seen or not callable(getter):
            continue
        try:
            row = await getter(art_id)
        except Exception:  # noqa: BLE001
            row = None
        if row is None:
            continue
        rows.append(row)
        seen.add(str(getattr(row, "id", "") or art_id))
    return rows


async def _event_types(tasks: Any, task_id: str) -> set[str]:
    list_events = getattr(tasks, "list_events", None)
    if not callable(list_events) or not task_id:
        return set()
    try:
        rows = await list_events(task_id)
    except Exception:  # noqa: BLE001
        return set()
    return {str(getattr(row, "event_type", "") or "") for row in rows if getattr(row, "event_type", "")}


async def _latest_observation(tasks: Any, run: Any) -> dict[str, Any] | None:
    found = await _scan_observation(tasks, str(getattr(run, "id", "") or ""))
    if found is not None:
        return found
    parent = str(getattr(run, "retry_of_task_id", "") or "")
    if parent:
        return await _scan_observation(tasks, parent)
    return None


async def _scan_observation(tasks: Any, task_id: str) -> dict[str, Any] | None:
    list_events = getattr(tasks, "list_events", None)
    if not callable(list_events) or not task_id:
        return None
    try:
        rows = await list_events(task_id)
    except Exception:  # noqa: BLE001
        return None
    for row in reversed(list(rows)):
        payload = getattr(row, "output_json", None)
        if not isinstance(payload, dict):
            continue
        observation = payload.get("observation")
        if isinstance(observation, dict) and observation.get("schema") == SCHEMA:
            return observation
    return None


async def _unmatched_tool_starts(tasks: Any, task_id: str) -> list[str]:
    """Tools that recorded a start without a matching done/failed/cancelled."""
    list_events = getattr(tasks, "list_events", None)
    if not callable(list_events) or not task_id:
        return []
    try:
        rows = await list_events(task_id)
    except Exception:  # noqa: BLE001
        return []
    open_starts: dict[str, str] = {}
    for row in rows:
        event_type = str(getattr(row, "event_type", "") or "")
        name = str(getattr(row, "tool_name", "") or getattr(row, "name", "") or "")
        if not name:
            continue
        if event_type.endswith(".start"):
            open_starts[name] = event_type
        elif event_type.endswith((".done", ".failed", ".cancelled", ".aborted")):
            open_starts.pop(name, None)
    return list(open_starts)


def refresh_system_research_brief(system_prompt: str, snapshot: str) -> str:
    """Replace or append the task research block in a system prompt."""
    text = system_prompt or ""
    desk = (snapshot or "").strip()
    if not desk:
        return text
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.startswith("[Task research state]")),
        -1,
    )
    if start < 0:
        return text.rstrip() + "\n\n" + desk + "\n"
    end = start + 1
    while end < len(lines):
        if lines[end].startswith("[") and not lines[end].startswith("[Task research"):
            break
        end += 1
    rebuilt = [*lines[:start], *desk.splitlines(), *lines[end:]]
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(rebuilt) + suffix


def _observation_empty_funnel(observation: dict[str, Any] | None) -> bool:
    if not observation:
        return False
    limitations = [str(item).lower() for item in (observation.get("limitations") or [])]
    if any("0 sources" in item or "n_kept=0" in item for item in limitations):
        return True
    metrics = observation.get("metrics") if isinstance(observation.get("metrics"), dict) else {}
    return metrics.get("n_kept") == 0


def _observation_lines(observation: dict[str, Any] | None) -> list[str]:
    if not observation:
        return []
    lines = []
    summary = str(observation.get("summary") or "").strip()
    if summary:
        lines.append(f"latest observation: {summary[:160]}")
    limitations = [str(item) for item in (observation.get("limitations") or []) if item]
    if limitations:
        lines.append("limitations: " + "; ".join(limitations[:3]))
    metrics = observation.get("metrics") if isinstance(observation.get("metrics"), dict) else {}
    if metrics.get("n_kept") is not None:
        lines.append(f"n_kept: {metrics.get('n_kept')}")
    return lines


def _ref_line(label: str, refs: list[TaskRef]) -> str:
    if not refs:
        return f"{label} (0): none"
    shown = "; ".join(item.label() for item in refs[:6])
    extra = f" +{len(refs) - 6}" if len(refs) > 6 else ""
    return f"{label} ({len(refs)}): {shown}{extra}"


__all__ = [
    "LiveTaskResearchFeed",
    "TaskRef",
    "TaskResearchState",
    "artifacts_for_task",
    "load_task_research_state",
    "opening_research_brief",
    "refresh_system_research_brief",
]
