"""Shared execution context + Tool wrapper used by builtin tools and skills."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from omni.config.paths import OmniPaths
from omni.config.settings import OmniSettings
from omni.core.react_agent import ToolSpec
from omni.skills_runtime.registry import SKILL_SOURCE_PARAM as SKILL_SOURCE_PARAM

# Control key carried in a task's ``input_json`` to force a specific discovery
# source at execution time (from a ``$<scope>:<name>`` escape). It is popped
# before the skill engine runs so it never leaks into skill input.


@dataclass
class ExecContext:
    settings: OmniSettings
    paths: OmniPaths
    project: str = "default"
    session_id: str = ""
    channel: str = "cli"
    # Conversational identity for memory isolation: "local" (owner) or
    # "<channel>:<external_key>" for an IM peer. Recall/writes are scoped to it.
    principal: str = "local"
    # Owning user-request task, and the executing subtask (set by the runtime).
    task_id: str = ""
    subtask_id: str = ""
    file_uris: list[str] = field(default_factory=list)
    # Optional wiring injected by the orchestrator (avoid hard import cycles).
    db: Any = None
    artifacts: Any = None
    llm: Any = None
    # Host-provided model services. Skills consume these ports; they do not read
    # owner settings or mutate the process environment.
    vlm: Any = None
    registry: Any = None
    hooks: Any = None
    approval_gate_factory: Any = None
    resource_locks: Any = None
    workflow_run_id: str = ""
    # Stable persisted WorkflowStepORM id and planner-facing logical key.
    workflow_step_id: str = ""
    workflow_step_key: str = ""
    # Optional aggregate envelope supplied by a workflow. Per-skill limits are
    # still applied locally; this parent budget prevents cross-step overspend.
    tool_budget: Any = None
    execution_deadline: float = 0.0
    # Live, pause-aware workflow envelope (``omni.core.turn_clock.TurnClock``).
    # When present it is preferred over the static ``execution_deadline`` float
    # so approval waits (which pause the clock) extend the envelope instead of
    # timing out subsequent steps. Typed ``Any`` to keep this module import-light.
    execution_clock: Any = None
    # Agent/subagent isolation may override the repository working tree and
    # compute profile without mutating global settings.
    working_dir: Path | None = None
    compute_override: Any = None
    runtime_steer: list[str] = field(default_factory=list)
    execution_control: Any = None
    tool_gateway: Any = None
    task_recorder: Any = None
    # The content-addressed plan/provider authority verified immediately before
    # deterministic dispatch. Async enqueue paths persist its provider slice.
    execution_authority: Any = None
    # Exact provider slice for the currently executing skill/native step.  Child
    # agents carry an aggregate slice containing every registry skill they may
    # expose, allowing each eventual tool call to re-check the sealed provider.
    provider_authority: Any = None
    # Turn-scoped async multi-agent control plane (``SubagentControl``). Present
    # only on a coordinating turn when ``settings.subagents.async_enabled`` is on;
    # the async delegation tools (spawn/wait/list/interrupt) read it from here and
    # the orchestrator joins/cancels it at turn end. ``None`` keeps today's
    # fork-join ``spawn_subagents`` behavior untouched.
    subagent_control: Any = None
    # Where this turn originated. ``"schedule"`` marks an unattended headless run
    # fired by the scheduler (no human in the loop); anything else is an ordinary
    # interactive/channel turn. Used to withhold self-referential controls (a
    # scheduled run must not create more schedules) and to keep the run headless.
    origin: str = "interactive"
    # A scheduled/unattended run has no interactive approver: sensitive tools are
    # cleared only by the owning task's pre-authorised grant (never a live prompt).
    autonomous: bool = False
    # Offer the scheduling tools on this turn's coordinator surface. False for a
    # scheduled headless run (recursion guard) and any non-coordinator surface.
    allow_scheduling: bool = True
    # Frozen semantic-admission facts for this turn (``core.action_contracts.
    # ResolverContext``): the user's raw message, a single reference time, and
    # the operator's IANA zone. Resolvers (temporal, …) read time only from here
    # so the model's "now" and theirs never disagree. Typed ``Any`` to keep this
    # module import-light; ``None`` on non-coordinator/headless surfaces.
    resolver_context: Any = None
    # Host-owned deferred goal for a SCHEDULE turn: the objective the planner
    # extracted for future execution (``IntentPlan.task_contract.deferred_goal``).
    # When set, ``schedule_task`` seals *this* goal into the schedule instead of a
    # goal the ReAct model re-typed, so the model cannot silently rewrite it. Empty
    # when the planner extracted no distinct goal (then the model's goal is used).
    deferred_goal: str = ""

    def os_sandbox_prefix(self) -> tuple[str, ...]:
        """OS-level write-confinement argv prefix for subprocesses a skill spawns.

        Derived from owner security settings and workspace paths so a portable
        skill engine gets real kernel confinement (seatbelt / bwrap / firejail)
        without importing CLI internals or reading ``ctx.settings`` itself.
        Empty when confinement is disabled or no backend is available.
        """
        try:
            from omni.skills_runtime.sandbox import sandbox_prefix

            return tuple(sandbox_prefix(self.settings.security, self.paths))
        except Exception:  # noqa: BLE001 - confinement is best-effort, never fatal
            return ()

    def approval_gate(self, *, sensitive_tools: set[str] | None = None) -> Any:
        """Build the run-scoped approval gate at invocation time."""
        if self.approval_gate_factory is None:
            return None
        return self.approval_gate_factory(
            self.task_id,
            self.channel,
            self.session_id,
            set(sensitive_tools or ()),
        )

    @property
    def resource_scope(self) -> str:
        return str(self.working_dir or self.paths.workspace_root or self.project)

    def base_input(self) -> dict[str, Any]:
        """Context fields injected into every skill input (HelixForge parity)."""
        payload = {
            "tenant_id": "local",
            "user_id": self.principal,
            "project": self.project,
            "session_id": self.session_id,
            "channel": self.channel,
            "task_id": self.task_id,
            "file_uri": self.file_uris[0] if self.file_uris else "",
        }
        if self.runtime_steer:
            payload["runtime_steer"] = list(self.runtime_steer)
        return payload


@dataclass
class Tool:
    spec: ToolSpec
    handler: Callable[[dict[str, Any]], Awaitable[Any]]
    sensitive: bool = False
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    replay_safe: bool | None = None
    # Generic routers such as ``run_skill`` expose one model-facing transport
    # call but delegate runtime admission to a concrete target. The resolver is
    # host-only metadata: it never appears in the provider-facing tool schema.
    admission_target: Callable[[dict[str, Any]], str] | None = None
    # Optional semantic-admission contract (``core.action_contracts.
    # ActionContract``). When present, the tool's *proposal* arguments must be
    # run through ``contract.prepare(...)`` to seal canonical arguments before
    # the handler executes; tools without one keep today's direct behaviour.
    # Typed ``Any`` to avoid importing the contracts module here.
    action_contract: Any = None

    def __post_init__(self) -> None:
        """Inherit host metadata from the spec while allowing explicit overrides."""
        if self.input_schema is None:
            self.input_schema = self.spec.parameters
        elif self.input_schema != self.spec.parameters:
            self.spec = replace(self.spec, parameters=self.input_schema)
        if self.output_schema is None:
            self.output_schema = {}
        if self.replay_safe is None:
            self.replay_safe = self.spec.replay_safe
        elif self.replay_safe != self.spec.replay_safe:
            self.spec = replace(self.spec, replay_safe=self.replay_safe)
