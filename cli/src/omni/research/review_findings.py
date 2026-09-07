"""Informational review findings cards for ``/task show``.

These cards are events, not a second settlement contract. A ``reject`` on the
review event must not paint Partial success by itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ReviewFinding:
    kind: str
    title: str
    body: str
    priority: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ReviewFinding | None:
        if not isinstance(data, dict):
            return None
        title = str(data.get("title") or "").strip()
        body = str(data.get("body") or "").strip()
        if not title and not body:
            return None
        return cls(
            kind=str(data.get("kind") or "note"),
            title=title or "Review finding",
            body=body,
            priority=str(data.get("priority") or "medium"),
        )


def findings_from_structural(
    *,
    dangling_anchors: Sequence[str] = (),
    unanchored_absolutes: Sequence[str] = (),
    figure_gaps: Sequence[str] = (),
) -> list[ReviewFinding]:
    cards: list[ReviewFinding] = []
    dangling = [str(item) for item in dangling_anchors if str(item).strip()]
    if dangling:
        cards.append(
            ReviewFinding(
                kind="dangling_anchor",
                title="Dangling [S#] anchors",
                body=", ".join(dangling[:8]) + " is not mapped"
                if len(dangling) == 1
                else ", ".join(dangling[:8]),
                priority="high",
            )
        )
    unanchored = [str(item) for item in unanchored_absolutes if str(item).strip()]
    if unanchored:
        cards.append(
            ReviewFinding(
                kind="unanchored_absolute",
                title="Unanchored absolute claims",
                body=", ".join(unanchored[:8]),
                priority="high",
            )
        )
    gaps = [str(item) for item in figure_gaps if str(item).strip()]
    if gaps:
        cards.append(
            ReviewFinding(
                kind="figure_gap",
                title="Figures without source script",
                body=", ".join(Path(item).name or item for item in gaps[:6]),
                priority="medium",
            )
        )
    return cards


def finding_from_judge_notes(notes: str) -> ReviewFinding | None:
    body = str(notes or "").strip()
    if not body:
        return None
    return ReviewFinding(
        kind="judge",
        title="Reviewer notes",
        body=body[:400],
        priority="low",
    )


def format_review_findings(payload: dict[str, Any] | None) -> list[str]:
    data = payload if isinstance(payload, dict) else {}
    if not data:
        return []
    lines: list[str] = []
    verdict = str(data.get("verdict") or "").strip()
    if verdict:
        lines.append(
            f"Review verdict: {verdict} (informational — does not change task status)"
        )
    raw = data.get("findings") if isinstance(data.get("findings"), list) else []
    for item in raw[:8]:
        card = ReviewFinding.from_dict(item) if isinstance(item, dict) else None
        if card is None:
            continue
        lines.append(f"[{card.priority}] {card.title}: {card.body}" if card.body else f"[{card.priority}] {card.title}")
    slides = data.get("slide_paths") if isinstance(data.get("slide_paths"), list) else []
    for path in slides[:4]:
        name = Path(str(path)).name or str(path)
        if name:
            lines.append(f"[info] Slide deck on this task: {name}")
    return lines


__all__ = [
    "ReviewFinding",
    "finding_from_judge_notes",
    "findings_from_structural",
    "format_review_findings",
]
