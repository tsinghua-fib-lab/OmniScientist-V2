"""Per-artifact provenance capsules (P1 research depth).

A *provenance capsule* binds one produced artifact (figure/report/dataset) to the
evidence that justifies it: the sources cited, the claims it rests on, the
evidence edges, the experiment runs, and the tool calls that made it. Where the
repro bundle (:mod:`omni.research.repro`) answers *"can I re-derive these bytes?"*,
the capsule answers *"what is this artifact's claim to truth, and is it grounded?"*

The capsule is a small JSON object stored on the artifact
(``ArtifactORM.meta["provenance"]``) and mirrored as a ``provenance.capsule`` run
event so the verification runner (``artifact_provenance_capsule`` check) and the
eval harness can assert an artifact was shipped *with* its grounding — not naked.

Pure data + stdlib; no LLM, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

CAPSULE_SCHEMA = "omni.provenance_capsule/v1"


@dataclass(slots=True)
class ProvenanceCapsule:
    """What grounds one artifact: sources, claims, evidence, runs, tool trace."""

    artifact_uri: str
    title: str = ""
    source_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    artifact_sha256: str = ""
    notes: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat(timespec="seconds")

    @property
    def is_grounded(self) -> bool:
        """A capsule is grounded when it binds the artifact to ≥1 piece of evidence.

        Sources, claims, or evidence edges all count — an artifact tied to a cited
        source or an evidenced claim is defensible; a capsule with none of these is
        hollow (a citation-less "trust me").
        """
        return bool(self.source_ids or self.claim_ids or self.evidence_ids)

    def completeness(self) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if not self.source_ids:
            reasons.append("no source cited")
        if not self.claim_ids:
            reasons.append("no claim recorded")
        if not self.evidence_ids:
            reasons.append("no evidence edge")
        # grounded (the gate) needs any one of the three; reasons are advisory.
        return self.is_grounded, reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CAPSULE_SCHEMA,
            "artifact_uri": self.artifact_uri,
            "title": self.title,
            "source_ids": list(self.source_ids),
            "claim_ids": list(self.claim_ids),
            "evidence_ids": list(self.evidence_ids),
            "run_ids": list(self.run_ids),
            "tool_calls": list(self.tool_calls),
            "artifact_sha256": self.artifact_sha256,
            "notes": self.notes,
            "created_at": self.created_at,
            "grounded": self.is_grounded,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProvenanceCapsule:
        return cls(
            artifact_uri=str(data.get("artifact_uri") or ""),
            title=str(data.get("title") or ""),
            source_ids=[str(v) for v in (data.get("source_ids") or []) if v],
            claim_ids=[str(v) for v in (data.get("claim_ids") or []) if v],
            evidence_ids=[str(v) for v in (data.get("evidence_ids") or []) if v],
            run_ids=[str(v) for v in (data.get("run_ids") or []) if v],
            tool_calls=[str(v) for v in (data.get("tool_calls") or []) if v],
            artifact_sha256=str(data.get("artifact_sha256") or ""),
            notes=str(data.get("notes") or ""),
            created_at=str(data.get("created_at") or ""),
        )


def read_capsule(artifact_row_or_meta: Any) -> ProvenanceCapsule | None:
    """Extract a capsule from an ``ArtifactORM`` row or its ``meta`` dict."""
    meta = getattr(artifact_row_or_meta, "meta", artifact_row_or_meta)
    if not isinstance(meta, dict):
        return None
    cap = meta.get("provenance")
    if not isinstance(cap, dict):
        return None
    return ProvenanceCapsule.from_dict(cap)


__all__ = ["CAPSULE_SCHEMA", "ProvenanceCapsule", "read_capsule"]
