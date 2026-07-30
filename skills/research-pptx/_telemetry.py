"""Local-first telemetry for research_pptx (JSONL, no external services).

Events are appended to <project>/artifacts/telemetry/research_pptx.jsonl so
they can be analysed offline. All functions are no-ops if no path is wired,
keeping the engine runnable in isolated/CLI mode.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
SKILL_NAME = "research-pptx"


class Telemetry:
    """Append-only JSONL recorder. One instance per skill run."""

    def __init__(self, path: Path | None, *, session_id: str = "", task_id: str = "") -> None:
        self._path = path
        self._session_id = session_id
        self._task_id = task_id
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, record: dict[str, Any]) -> None:
        if self._path is None:
            return
        record["ts"] = datetime.now(UTC).isoformat()
        record["skill"] = SKILL_NAME
        # Make every row joinable back to the durable task / session.
        if self._session_id:
            record.setdefault("session_id", self._session_id)
        if self._task_id:
            record.setdefault("task_id", self._task_id)
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001
            logger.debug("telemetry write failed", exc_info=True)

    # ── audit (start / complete / fail) ──
    def audit(self, action: str, **details: Any) -> None:
        self._write({"type": "audit", "action": action, "details": details})

    # ── one row per internal stage ──
    def stage(
        self,
        event_kind: str,
        *,
        input_summary: dict[str, Any] | None = None,
        output_summary: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        self._write({
            "type": "stage",
            "event_kind": event_kind,
            "input_summary": input_summary or {},
            "output_summary": output_summary or {},
            "latency_ms": latency_ms,
            "status": status,
            "error": error,
        })

    # ── one row per human/LLM decision point (for later analytics) ──
    def decision(
            self,
            point: str,
            *,
            options: list[str] | None = None,
            chosen: str = "",
            decided_by: str = "llm",  # llm | user | default
            requires_review: bool = False,
            rationale: str = "",
            extra: dict[str, Any] | None = None,
    ) -> None:
        """Record a decision point so LLM/human choices can be analysed offline.

        This is the instrumentation hook behind the skill's "LLM-decides +
        human-reviews" design: every branch that the model or the user resolves
        is written as one JSONL row, joinable back to the session/task.
        """
        self._write({
            "type": "decision",
            "point": point,
            "options": options or [],
            "chosen": chosen,
            "decided_by": decided_by,
            "requires_review": requires_review,
            "rationale": rationale[:500],
            "extra": extra or {},
        })


def build_telemetry(ctx: Any) -> Telemetry:
    path: Path | None = None
    paths = getattr(ctx, "paths", None)
    if paths is not None and getattr(paths, "artifacts_dir", None) is not None:
        path = Path(paths.artifacts_dir) / "telemetry" / f"{SKILL_NAME}.jsonl"
    return Telemetry(
        path,
        session_id=getattr(ctx, "session_id", ""),
        task_id=getattr(ctx, "task_id", ""),
    )
