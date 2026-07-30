"""Adapters for external scientific benchmarks without replacing their scorers."""

from __future__ import annotations

import ast
import csv
import json
import tempfile
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omni.config.settings import OmniSettings

BioAgentFactory = Callable[[OmniSettings], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class BioMysteryCase:
    """One official BioMysteryBench row; rubric remains evaluator-only."""

    id: str
    question: str
    answer_rubric: str
    allowed_domains: tuple[str, ...]
    human_solvable: bool
    archive_path: Path | None = None


def load_biomystery_cases(
    problems_csv: Path,
    *,
    data_dir: Path | None = None,
    limit: int | None = None,
) -> list[BioMysteryCase]:
    """Load the official CSV schema without exposing answer rubrics to prompts."""
    cases: list[BioMysteryCase] = []
    with problems_csv.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            case_id = str(row.get("id") or "").strip()
            question = str(row.get("question") or "").strip()
            if not case_id or not question:
                raise ValueError("BioMysteryBench rows require id and question")
            archive = (data_dir / f"{case_id}.zip") if data_dir is not None else None
            cases.append(
                BioMysteryCase(
                    id=case_id,
                    question=question,
                    answer_rubric=str(row.get("answer_rubric") or ""),
                    allowed_domains=_parse_domains(row.get("allowed_domains")),
                    human_solvable=str(row.get("human_solvable") or "").strip().lower()
                    in {"1", "true", "yes", "y"},
                    archive_path=archive,
                )
            )
            if limit is not None and len(cases) >= limit:
                break
    return cases


def build_biomystery_prompt(case: BioMysteryCase, *, data_files: list[str]) -> str:
    """Build the solve prompt from public fields only; never interpolate rubric."""
    files = "\n".join(f"- {path}" for path in sorted(data_files)) or "- (no files found)"
    domains = ", ".join(case.allowed_domains) or "none"
    return (
        f"BioMysteryBench problem {case.id}\n\n"
        f"{case.question}\n\n"
        "Available files in the managed workspace:\n"
        f"{files}\n\n"
        f"Allowed network domains: {domains}.\n"
        "Do not look up GEO/SRA/ENA/BioProject accession identifiers to identify the "
        "source dataset, publication, or study metadata. Standard analysis and permitted "
        "bioinformatics database use are allowed. Analyze the files and return a concise "
        "final answer with the key evidence supporting it."
    )


def extract_biomystery_data(case: BioMysteryCase, destination: Path) -> list[str]:
    """Extract a case archive with traversal protection and return relative files."""
    archive = case.archive_path
    if archive is None or not archive.is_file():
        raise FileNotFoundError(f"BioMysteryBench archive missing for {case.id}")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"unsafe archive member in {case.id}: {member.filename}")
        bundle.extractall(destination)
    return sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    )


def write_benchmark_answers(path: Path, answers: list[dict[str, Any]]) -> None:
    """Write solver outputs for an official scorer or evaluator-owned grading pass."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for answer in answers:
            public = {key: value for key, value in answer.items() if key != "answer_rubric"}
            handle.write(json.dumps(public, ensure_ascii=False, default=str) + "\n")


async def run_biomystery_cases(
    cases: list[BioMysteryCase],
    *,
    settings: OmniSettings | None = None,
    repeats: int = 5,
    sandbox_attested: bool = False,
    agent_factory: BioAgentFactory | None = None,
) -> list[dict[str, Any]]:
    """Solve cases in isolated workspaces and return unscored official inputs.

    BioMysteryBench requires a container with canonical bioinformatics tools and
    enforced network rules. Omni cannot prove those host controls from inside
    the process, so the caller must attest that this function is running in that
    environment. Rubrics are never passed to the agent or written to outputs;
    returned answers are intended for evaluator-owned grading.
    """
    if not sandbox_attested:
        raise RuntimeError(
            "BioMysteryBench execution requires an attested container/network sandbox; "
            "dataset loading and answer export remain available without it"
        )
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    from omni.agent import OmniAgent
    from omni.config import load_settings
    from omni.eval.blackbox import isolated_eval_settings

    source_settings = settings or load_settings()
    factory = agent_factory or OmniAgent.create
    answers: list[dict[str, Any]] = []
    for case in cases:
        for repeat in range(1, repeats + 1):
            started = time.perf_counter()
            with tempfile.TemporaryDirectory(prefix=f"omni-biomystery-{case.id}-") as raw_root:
                root = Path(raw_root)
                attempt_settings = isolated_eval_settings(
                    source_settings,
                    root,
                    f"biomystery-{case.id}-{repeat}",
                )
                attempt_settings.security.require_approval = False
                attempt_settings.web_fetch.allow_hosts = list(case.allowed_domains)
                workspace = attempt_settings.paths.workspace_root
                data_files = extract_biomystery_data(case, workspace)
                prompt = build_biomystery_prompt(case, data_files=data_files)
                agent = await factory(attempt_settings)
                try:
                    result = await agent.handle_turn(
                        prompt,
                        channel="biomystery",
                        drain_tasks=True,
                    )
                    run = await agent.tasks.get_task(result.task_id)
                    events = await agent.tasks.list_events(result.task_id)
                    cost = await agent.tasks.cost_summary(result.task_id, include_child_tasks=True)
                finally:
                    await agent.aclose()
            answers.append(
                {
                    "id": case.id,
                    "repeat": repeat,
                    "answer": result.text,
                    "status": str(run.status if run is not None else result.kind),
                    "human_solvable": case.human_solvable,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "cost": cost,
                    "trace": [
                        {
                            "seq": event.seq,
                            "event_type": event.event_type,
                            "status": event.status,
                            "tool": event.tool_name,
                            "skill": event.skill_name,
                            "duration_ms": event.duration_ms,
                            "summary": event.summary,
                        }
                        for event in events
                    ],
                    "official_score": None,
                }
            )
    return answers


def _parse_domains(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value).strip()
    if not text:
        return ()
    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            parsed = [part.strip() for part in text.split(",")]
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, (list, tuple)):
        raise ValueError(f"invalid allowed_domains value: {text}")
    return tuple(str(item).strip() for item in parsed if str(item).strip())


__all__ = [
    "BioMysteryCase",
    "build_biomystery_prompt",
    "extract_biomystery_data",
    "load_biomystery_cases",
    "run_biomystery_cases",
    "write_benchmark_answers",
]
