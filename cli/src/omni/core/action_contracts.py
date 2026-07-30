"""Generic Action semantic-admission contracts (V1).

One universal pipeline instead of a per-domain "intent compiler": an LLM may
*propose* an Action, but every field that materially changes the outcome (a
``critical`` field) must be verified against a trusted provenance, uniquely
resolved, and admitted **before** any canonical arguments reach approval or the
:class:`~omni.runtime.tool_gateway.ToolGateway`. Domain knowledge lives in
pluggable *resolvers* (temporal, identity, …); this module owns only the
domain-agnostic vocabulary they share.

Core invariant
--------------
    The model proposes; the contract binds. Strictness is decided by an
    action's declared *effects*, not by an ``IntentType``. Only ``canonical``
    arguments (never the raw model proposal) flow into approval and execution.

The pieces:

* :class:`EffectKind` — *what kind of impact* an action has. Composed as a set
  so "a deferred, persistent state change" reads as three orthogonal facts.
* :class:`ProvenanceKind` — *where a field's value came from*. A bare model
  assumption is intentionally absent from :data:`TRUSTED_CRITICAL_PROVENANCE`:
  a high-effect action can never be created from one.
* :class:`ResolverContext` — the frozen, trusted facts a resolver may read
  (the user message, a single reference time, the operator's zone, identity).
* :class:`ResolutionResult` — a resolver's verdict (resolved / ambiguous / …)
  with candidates + evidence.
* :class:`ActionDecision` — admission's verdict over a whole proposal
  (ready / needs_input / rejected) carrying the sealed canonical arguments.
* :class:`ActionContract` — the per-action declaration binding a proposal
  schema, an execution schema, its critical fields, its effects, and a
  ``prepare`` coroutine that turns a proposal into an :class:`ActionDecision`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class EffectKind(StrEnum):
    """Orthogonal impact facts. Composed as a set on an :class:`ActionContract`.

    Composition (rather than a single ordinal ``effect_level``) lets admission
    reason about each axis independently: a scheduled digest is a *deferred*,
    *persistent* *state change* but not *destructive*/*external*/*privileged*.
    """

    STATE_CHANGE = "state_change"  # mutates durable owner/app state
    DEFERRED = "deferred"  # runs later / unattended (time & target must be exact)
    EXTERNAL = "external"  # reaches a third party (recipient/channel must be exact)
    DESTRUCTIVE = "destructive"  # removes/overwrites; target & scope must be exact
    PERSISTENT = "persistent"  # survives the turn/process
    PRIVILEGED = "privileged"  # security-sensitive; also requires authorization


class ProvenanceKind(StrEnum):
    """Where a proposed field value came from — the trust axis for a field."""

    USER_EVIDENCE = "user_evidence"  # grounded in a span of the user's request
    CONVERSATION = "conversation"  # from earlier dialogue in this session
    HOST_CONTEXT = "host_context"  # trusted host fact (zone, workspace, actor)
    POLICY_DEFAULT = "policy_default"  # a versioned, owner-set default
    COMPUTED = "computed"  # deterministically derived from a trusted source
    USER_CONFIRMED = "user_confirmed"  # an explicit persisted user selection


# Provenances that may back a *critical* field. ``model_assumption`` is absent
# by design — a high-effect action can never be created from a bare guess.
TRUSTED_CRITICAL_PROVENANCE: frozenset[ProvenanceKind] = frozenset(
    {
        ProvenanceKind.USER_EVIDENCE,
        ProvenanceKind.HOST_CONTEXT,
        ProvenanceKind.POLICY_DEFAULT,
        ProvenanceKind.USER_CONFIRMED,
        ProvenanceKind.COMPUTED,
    }
)


def provenance_trusted_for_critical(kind: ProvenanceKind | str) -> bool:
    """Whether ``kind`` may back a critical field (fail-closed on unknowns)."""
    try:
        return ProvenanceKind(str(kind)) in TRUSTED_CRITICAL_PROVENANCE
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class ResolverContext:
    """The frozen, trusted facts a resolver may read.

    ``reference_time`` is captured **once** at the start of a turn and shared
    with the system prompt, so the model's notion of "now" and the resolver's
    never disagree. A resolver must not read a fresh wall clock.
    """

    user_message: str
    reference_time: datetime  # aware; the single "now" for the turn
    timezone: str  # IANA name (e.g. "Asia/Shanghai"); "" ⇒ process local
    timezone_source: str = ""  # "explicit" | "actor" | "host" | "process"
    locale: str = ""
    channel: str = "cli"
    session_id: str = ""
    principal: str = "local"
    project_dir: str = ""
    checkpoint_id: str = ""


class ResolutionStatus(StrEnum):
    """A resolver's verdict for one critical field."""

    RESOLVED = "resolved"  # a single canonical value was derived
    AMBIGUOUS = "ambiguous"  # >1 grounded candidate; the user must choose
    MISSING = "missing"  # a required sub-fact was not provided
    INVALID = "invalid"  # provided but malformed / impossible (e.g. DST gap)
    UNSUPPORTED = "unsupported"  # a well-formed request this resolver cannot model


@dataclass(frozen=True, slots=True)
class ResolutionCandidate:
    """One grounded interpretation the user may pick between."""

    id: str
    value: Any
    label: str
    validity: str = "valid"  # "valid" | "past" | "future" | "dst_gap" | "dst_fold"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """A resolver's structured verdict (kept intentionally minimal for V1)."""

    status: ResolutionStatus
    value: Any = None
    candidates: tuple[ResolutionCandidate, ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    unresolved_fields: tuple[str, ...] = ()
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED

    @property
    def ambiguous(self) -> bool:
        return self.status is ResolutionStatus.AMBIGUOUS


class ActionDecisionStatus(StrEnum):
    """Admission's verdict over a whole action proposal."""

    READY = "ready"  # canonical arguments sealed; proceed to approval/execution
    NEEDS_INPUT = "needs_input"  # a critical field needs a user choice/clarification
    REJECTED = "rejected"  # cannot proceed (untrusted provenance / invalid)


@dataclass(frozen=True, slots=True)
class ActionDecision:
    """The outcome of admitting a proposal. Only ``READY`` carries canonical args."""

    status: ActionDecisionStatus
    canonical_arguments: dict[str, Any] | None = None
    resolution: ResolutionResult | None = None
    checkpoint_id: str = ""
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.status is ActionDecisionStatus.READY

    @property
    def needs_input(self) -> bool:
        return self.status is ActionDecisionStatus.NEEDS_INPUT

    @classmethod
    def ready_with(cls, canonical_arguments: dict[str, Any]) -> ActionDecision:
        return cls(status=ActionDecisionStatus.READY, canonical_arguments=dict(canonical_arguments))

    @classmethod
    def needs_input_with(
        cls, resolution: ResolutionResult, *, reason: str = "", checkpoint_id: str = ""
    ) -> ActionDecision:
        return cls(
            status=ActionDecisionStatus.NEEDS_INPUT,
            resolution=resolution,
            reason=reason or resolution.reason,
            checkpoint_id=checkpoint_id,
        )

    @classmethod
    def rejected_with(cls, reason: str, *, resolution: ResolutionResult | None = None) -> ActionDecision:
        return cls(status=ActionDecisionStatus.REJECTED, resolution=resolution, reason=reason)


PrepareFn = Callable[[dict[str, Any], ResolverContext], Awaitable["ActionDecision"]]


@dataclass(frozen=True, slots=True)
class ActionContract:
    """Per-action declaration that routes a proposal through semantic admission.

    ``proposal_schema`` is what the *model* may submit (semantic, may be
    ambiguous). ``execution_schema`` is what the *handler* accepts (canonical,
    unambiguous). A gateway that honours a contract must only ever execute
    arguments that pass ``execution_schema`` — never the raw proposal.
    """

    name: str
    version: str
    proposal_schema: dict[str, Any]
    execution_schema: dict[str, Any]
    critical_fields: frozenset[str]
    effects: frozenset[EffectKind]
    prepare: PrepareFn


__all__ = [
    "EffectKind",
    "ProvenanceKind",
    "TRUSTED_CRITICAL_PROVENANCE",
    "provenance_trusted_for_critical",
    "ResolverContext",
    "ResolutionStatus",
    "ResolutionCandidate",
    "ResolutionResult",
    "ActionDecisionStatus",
    "ActionDecision",
    "ActionContract",
    "PrepareFn",
]
