"""Adapters preserve external benchmark rules and official scoring ownership."""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import pytest

from omni.agent import OmniAgent
from omni.core.llm.client import ChatWithToolsResult, ToolCall
from omni.core.react_agent import ToolSpec
from omni.eval.external_benchmarks import (
    BioMysteryCase,
    build_biomystery_prompt,
    load_biomystery_cases,
    run_biomystery_cases,
)
from omni.skills_runtime.context import Tool
from tests.conftest import PlanningLLM, ScriptedLLM


def test_biomystery_loader_keeps_rubric_private_from_agent_prompt(tmp_path: Path) -> None:
    source = tmp_path / "problems.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "question", "answer_rubric", "allowed_domains", "human_solvable"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "id": "mystery-1",
                "question": "Which organism generated these reads?",
                "answer_rubric": "SECRET_EXPECTED_ORGANISM",
                "allowed_domains": '["ncbi.nlm.nih.gov", "ensembl.org"]',
                "human_solvable": "yes",
            }
        )

    case = load_biomystery_cases(source)[0]
    prompt = build_biomystery_prompt(case, data_files=["reads.fastq.gz"])

    assert case.answer_rubric == "SECRET_EXPECTED_ORGANISM"
    assert "SECRET_EXPECTED_ORGANISM" not in prompt
    assert "reads.fastq.gz" in prompt
    assert "accession" in prompt.lower()
    assert case.allowed_domains == ("ncbi.nlm.nih.gov", "ensembl.org")


def test_biomystery_case_agent_payload_never_contains_rubric() -> None:
    case = BioMysteryCase(
        id="case",
        question="Identify the knocked-out gene.",
        answer_rubric="TP53",
        allowed_domains=("ncbi.nlm.nih.gov",),
        human_solvable=False,
    )

    assert "TP53" not in build_biomystery_prompt(case, data_files=["counts.tsv"])


@pytest.mark.asyncio
async def test_biomystery_execution_fails_closed_without_sandbox_attestation() -> None:
    with pytest.raises(RuntimeError, match="attested"):
        await run_biomystery_cases([], sandbox_attested=False)


@pytest.mark.asyncio
async def test_biomystery_attested_run_uses_real_agent_and_exports_unscored_answer(
    settings,
    tmp_path: Path,
) -> None:  # noqa: ANN001
    archive = tmp_path / "case-1.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("counts.tsv", "gene\tsample\nTP53\t12\n")
    case = BioMysteryCase(
        id="case-1",
        question="Which gene is most strongly implicated?",
        answer_rubric="PRIVATE_RUBRIC",
        allowed_domains=(),
        human_solvable=True,
        archive_path=archive,
    )

    async def factory(attempt_settings):  # noqa: ANN202, ANN001
        agent = await OmniAgent.create(attempt_settings)
        agent.llm = PlanningLLM(
            {
                "intent_type": "react_fallback",
                "execution_mode": "react",
                "provenance_mode": "light",
                "confidence": 0.8,
                "rationale": "analyze benchmark files",
            },
            script=[ChatWithToolsResult(content="TP53 is most strongly implicated.")],
        )
        return agent

    answers = await run_biomystery_cases(
        [case],
        settings=settings,
        repeats=1,
        sandbox_attested=True,
        agent_factory=factory,
    )

    assert len(answers) == 1
    assert answers[0]["answer"] == "TP53 is most strongly implicated."
    assert answers[0]["official_score"] is None
    assert "PRIVATE_RUBRIC" not in str(answers[0])
    assert answers[0]["cost"]["components"]["planner"]["total_tokens"] > 0


@pytest.mark.asyncio
async def test_authoritative_external_tools_use_public_agent_gateway(settings) -> None:  # noqa: ANN001
    calls: list[dict] = []

    async def official_search(args: dict):  # noqa: ANN202
        calls.append(args)
        return {"results": ["official result"]}

    tool = Tool(
        ToolSpec(
            name="official_search",
            description="Benchmark-owned restricted search",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        official_search,
        sensitive=True,
    )
    settings.security.require_approval = False
    settings.skills.sources = []
    agent = await OmniAgent.create(settings)
    agent.set_external_tools([tool], authoritative=True)
    agent.llm = ScriptedLLM(
        [
            ChatWithToolsResult(
                tool_calls=[ToolCall("official", "official_search", {"query": "RAG"})]
            ),
            ChatWithToolsResult(content="answer from the official result"),
        ]
    )
    try:
        turn = await agent.handle_turn("Find the benchmark answer", drain_tasks=False)
        events = await agent.tasks.list_events(turn.task_id)
    finally:
        await agent.aclose()

    assert calls == [{"query": "RAG"}]
    assert any(event.tool_name == "official_search" for event in events)
    assert not any(event.tool_name == "web_fetch" for event in events)
