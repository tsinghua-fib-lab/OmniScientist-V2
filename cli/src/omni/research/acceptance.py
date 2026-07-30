"""AcceptanceEngine — a configurable research-fact acceptance layer.

Structural verification (:class:`omni.runtime.verification.VerificationRunner`)
already gates a turn's *terminal status* on contract checks (required events,
artifacts, provenance). This engine is a **complementary, research-fact** layer
that judges whether the honesty audit (grounding + semantic citation support +
contradictions) is strong enough to *accept* the result.

It is deliberately configurable and additive:

* ``off``    — do not evaluate acceptance.
* ``warn``   — (default) annotate findings; ``accepted`` stays ``True`` so the
  terminal status and existing behaviour are unchanged. This surfaces the moat
  (verifiable, cited research) without destabilising flows.
* ``strict`` — ``accepted`` becomes ``False`` when any finding fires, so a
  caller may downgrade/gate on it.

The engine reads a :class:`omni.research.verify.VerifyReport` (already offline,
no model call), so acceptance is deterministic and testable with the mock stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from omni.research.verify import VerifyReport

AcceptanceMode = Literal["off", "warn", "strict"]
_VALID_MODES: frozenset[str] = frozenset({"off", "warn", "strict"})


@dataclass
class AcceptanceReport:
    """The acceptance verdict over a :class:`VerifyReport`."""

    mode: str
    accepted: bool
    notes: list[str] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.notes)

    def annotation(self) -> str:
        """A short, user-facing block summarising acceptance findings (or "")."""
        if not self.notes:
            return ""
        head = "⚠ Acceptance" if self.accepted else "✗ Not accepted"
        bullets = "\n".join(f"  - {n}" for n in self.notes)
        return f"[{head}] research-fact check:\n{bullets}"


class AcceptanceEngine:
    """Evaluate a :class:`VerifyReport` into an :class:`AcceptanceReport`."""

    def __init__(
        self,
        mode: str = "warn",
        *,
        min_citation_support: float = 0.6,
        min_grounding: float = 0.5,
    ) -> None:
        self.mode: str = mode if mode in _VALID_MODES else "warn"
        self.min_citation_support = min_citation_support
        self.min_grounding = min_grounding

    @classmethod
    def from_settings(cls, settings: Any) -> AcceptanceEngine:
        research = getattr(settings, "research", None)
        return cls(
            mode=str(getattr(research, "acceptance_mode", "warn") or "warn"),
            min_citation_support=float(
                getattr(research, "acceptance_min_citation_support", 0.6) or 0.6
            ),
            min_grounding=float(getattr(research, "acceptance_min_grounding", 0.5) or 0.5),
        )

    def evaluate(self, report: VerifyReport) -> AcceptanceReport:
        if self.mode == "off":
            return AcceptanceReport("off", True, [])
        notes: list[str] = []
        if report.total_claims and report.grounding_rate < self.min_grounding:
            notes.append(
                f"grounding {report.grounding_rate:.0%} < {self.min_grounding:.0%} "
                f"({len(report.unsupported)} claim(s) without supporting evidence)"
            )
        if report.contradicted:
            notes.append(
                f"{len(report.contradicted)} claim(s) have contradicting evidence"
            )
        cs = report.citation_support
        if cs is not None and cs.checked and cs.support_rate < self.min_citation_support:
            notes.append(
                f"citation support {cs.support_rate:.0%} < {self.min_citation_support:.0%} "
                f"({len(cs.unsupported)} cited claim(s) not entailed by their evidence)"
            )
        # warn annotates but always accepts; strict fails closed on any finding.
        accepted = True if self.mode == "warn" else not notes
        return AcceptanceReport(self.mode, accepted, notes)


__all__ = ["AcceptanceEngine", "AcceptanceMode", "AcceptanceReport"]
