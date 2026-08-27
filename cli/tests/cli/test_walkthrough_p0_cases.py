"""Walkthrough catalog cases for the three settlement/harvest/identifier P0s.

Each test is named after a Case ID in ``cli/docs/user-walkthrough-cases.md``.
These rows are executable offline (mock / Scripted skill results). Live VLM
happy-path bodies (A-LF-01, A-REV-01 visual) stay on a configured home.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omni.agent.intent_plan import IntentPlan, IntentType, VerificationPlan
from omni.agent.turn_execution import TurnCompletion
from omni.core.react_agent import AgentLoopResult, ToolInvocationRecord
from omni.runtime.remaining import failed_canonical_file_debts, remaining_deliverables
from omni.runtime.settlement import settlement_for
from omni.skills_runtime.exec_io import harvestable_output
from tests.runtime.test_settlement_empty_funnel import _child, _event, _Store, _task


class _Recorder:
    def __init__(self) -> None:
        self.timeline: list[str] = []
        self.events: list[dict[str, Any]] = []

    async def append_event(self, _task_id: str, **event: Any) -> None:
        self.timeline.append(f"event:{event['event_type']}")
        self.events.append(event)

    async def get_task(self, _task_id: str) -> None:
        return None


class _Hooks:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    async def emit(self, event: str, **_kwargs: Any) -> None:
        self._timeline.append(event)


class _TaskController:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    async def finish_turn(self, *_args: Any, **_kwargs: Any) -> None:
        self._timeline.append("finish")

    async def apply_settlement(self, _task_id: str, result: Any) -> Any:
        self._timeline.append("settle")
        result.settlement_status = "succeeded"
        return result


class _Runtime:
    async def drain(self, **_kwargs: Any) -> None:
        return None

    async def get_workflow_run(self, workflow_id: str) -> Any:
        return SimpleNamespace(status="succeeded", result_json={}, error="", trace_log=[])

    async def get_subtask(self, subtask_id: str) -> Any:
        return SimpleNamespace(
            skill_name="demo", status="succeeded", result_json={}, error="", trace_log=[]
        )


def _completion() -> TurnCompletion:
    recorder = _Recorder()
    return TurnCompletion(
        tasks=recorder,
        task_controller=_TaskController(recorder.timeline),
        hooks=_Hooks(recorder.timeline),
        runtime=_Runtime(),
    )


def _load_paper_review():
    import importlib.util
    import sys

    skill = Path(__file__).resolve().parents[3] / "skills" / "paper-review" / "engine.py"
    name = "walkthrough_paper_review_engine"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, skill)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_p01_succeeded_producers_clear_remaining_on_task_artifacts() -> None:
    """P-01: successful figure/writing/slides + Task files → remaining empty and succeeded."""
    from omni.cli.commands.tasks_cmd import _host_remaining_summary

    plan = {
        "outputs": ["artifact.figure", "draft.manuscript", "artifact.slides"],
        "verification_plan": {
            "required_outputs": ["artifact.figure", "draft.manuscript", "artifact.slides"],
        },
    }
    store = _Store(
        _task(subtask_ids=["fig-1"], outputs=plan["outputs"]),
        events=[
            _event(
                "react.finished",
                kind="text",
                terminated_reason="done",
                tool_names=["run_skill", "write_file"],
            )
        ],
        children=[
            _child(
                "fig-1",
                status="succeeded",
                result={"status": "ok", "skill_name": "scientific-figure"},
            )
        ],
        artifacts=[
            SimpleNamespace(
                kind="figure",
                format="png",
                path="outputs/RAG.png",
                mime="image/png",
                title="RAG architecture",
            ),
            SimpleNamespace(
                kind="document",
                format="md",
                path="outputs/RAG系统综述.md",
                mime="text/markdown",
                title="RAG survey",
            ),
            SimpleNamespace(
                kind="slides",
                format="pptx",
                path="outputs/deck.pptx",
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                title="RAG slides",
            ),
        ],
    )
    settled = await settlement_for(store, "parent")
    assert settled.status == "succeeded"
    assert not settled.detail.get("undelivered_outputs")
    remaining = _host_remaining_summary(
        plan,
        [
            ("RAG architecture", "outputs/RAG.png", "artifact://fig"),
            ("RAG survey", "outputs/RAG系统综述.md", "artifact://md"),
            ("RAG slides", "outputs/deck.pptx", "artifact://pptx"),
        ],
    )
    assert remaining == "all named deliverables present"


@pytest.mark.asyncio
async def test_a_lf_04_leftover_deck_does_not_pay_editable_figure() -> None:
    """A-LF-04: failed livefigure + harvested deck → artifact.pptx unpaid."""
    plan = IntentPlan(
        task_id="walk-lf-04",
        user_message="$livefigure Make one editable PPTX slide of a RAG architecture.",
        intent_type=IntentType.REACT_FALLBACK,
        verification_plan=VerificationPlan(required_outputs=["artifact.pptx"]),
    )
    loop = AgentLoopResult(
        kind="text",
        content="Wrote deck.pptx instead.",
        tool_trace=[
            ToolInvocationRecord(
                name="run_skill",
                arguments={"skill_name": "livefigure"},
                result={
                    "status": "error",
                    "skill_name": "livefigure",
                    "error": "Generated code contains a forbidden dunder attribute",
                },
            )
        ],
    )
    honesty = await _completion()._honest_unpaid_files(
        plan,
        loop,
        [
            {
                "skill": "livefigure",
                "status": "failed",
                "result": {
                    "artifacts": [
                        {"title": "deck.pptx", "path": "/tmp/deck.pptx", "format": "pptx"}
                    ]
                },
            }
        ],
        submitted=[],
        task_id=plan.task_id,
    )
    assert any("artifact.pptx" in note for note in honesty)
    assert "still owes" in (loop.content or "")


@pytest.mark.asyncio
async def test_e_05_failed_paper_review_leftover_markdown_is_not_success() -> None:
    """E-05: paper-review error + write_file leftover does not retire review."""
    plan = IntentPlan(
        task_id="walk-e-05",
        user_message="Review arXiv 1706.03762 as a NeurIPS reviewer.",
        intent_type=IntentType.REACT_FALLBACK,
        verification_plan=VerificationPlan(required_outputs=["review"]),
    )
    leftover = SimpleNamespace(
        kind="document",
        title="review",
        rel_path="outputs/review.md",
        mime="text/markdown",
        uri="artifact://md",
    )
    assert remaining_deliverables(["review"], [leftover]) == ["review"]
    record = ToolInvocationRecord(
        name="run_skill",
        arguments={"skill_name": "paper-review"},
        result={"status": "error", "skill_name": "paper-review", "error": "dunder"},
    )
    assert failed_canonical_file_debts([record], []) == ["review"]
    loop = AgentLoopResult(kind="text", content="Here is a Markdown review.", tool_trace=[record])
    honesty = await _completion()._honest_unpaid_files(
        plan, loop, [], submitted=[], task_id=plan.task_id
    )
    assert any("review" in note for note in honesty)


@pytest.mark.asyncio
async def test_e_05_settlement_failed_livefigure_with_harvested_deck() -> None:
    """E-05 durable record: failed livefigure + slides pptx is not succeeded."""
    store = _Store(
        _task(subtask_ids=["live-1"], outputs=["artifact.pptx"]),
        events=[
            _event(
                "react.finished",
                kind="text",
                terminated_reason="done",
                tool_names=["run_skill", "bash"],
            )
        ],
        children=[
            _child(
                "live-1",
                status="failed",
                result={"status": "error", "error": "forbidden dunder"},
            )
        ],
        artifacts=[
            SimpleNamespace(
                kind="slides",
                format="pptx",
                path="deck.pptx",
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                title="deck",
            )
        ],
    )
    settled = await settlement_for(store, "parent")
    assert settled.status == "failed"
    assert settled.detail.get("undelivered_outputs") == ["artifact.pptx"]
    assert settled.detail.get("lost") == ["live-1"]


@pytest.mark.asyncio
async def test_a_rev_04_unfetchable_arxiv_is_needs_input_not_missing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-REV-04: offline fetch miss → needs_input, not Paper input does not exist."""
    module = _load_paper_review()

    async def no_pdf(_arxiv_id: str, _ctx: Any) -> Path | None:
        return None

    monkeypatch.setattr(module, "_materialize_arxiv_pdf", no_pdf)
    engine = module.PaperReviewEngine()
    engine.ctx = SimpleNamespace(
        llm=SimpleNamespace(chat=lambda *_a, **_k: ""),
        settings=SimpleNamespace(),
    )
    result = await engine.execute(input="Review arXiv 1706.03762 as a NeurIPS reviewer.")
    assert result["status"] == "needs_input"
    assert result["outcome"] == "needs_input"
    error = str(result.get("error") or "")
    assert "does not exist" not in error.lower()
    assert "1706.03762" in error


@pytest.mark.asyncio
async def test_a_rev_02_missing_local_pdf_is_needs_input_not_failed() -> None:
    """A-REV-02: missing draft.pdf is ask-user, not Paper input does not exist."""
    from omni.core.funnel_facts import project_skill_observation

    module = _load_paper_review()
    engine = module.PaperReviewEngine()
    engine.ctx = SimpleNamespace(
        llm=SimpleNamespace(chat=lambda *_a, **_k: ""),
        settings=SimpleNamespace(),
    )
    for raw in ("draft.pdf", "the workspace file draft.pdf"):
        result = await engine.execute(input=raw)
        assert result["status"] == "needs_input"
        assert result["outcome"] == "needs_input"
        assert result["error_info"]["code"] == "missing_input"
        error = str(result.get("error") or "")
        assert "draft.pdf" in error
        assert "does not exist" not in error.lower()
        wrapped = project_skill_observation(
            result,
            extra={"status": "unknown", "skill_name": "paper-review"},
        )
        assert wrapped["status"] == "needs_input"
        assert wrapped["outcome"] == "needs_input"


def test_a_rev_05_doi_is_needs_input_not_a_path() -> None:
    """A-REV-05: DOI is an identifier, not a missing filesystem path."""
    module = _load_paper_review()
    with pytest.raises(module._RemotePaperRef) as caught:
        module._resolve_input("doi:10.5555/3295222.3295349")
    assert caught.value.kind == "doi"
    payload = module._source_needs_input(caught.value)
    assert payload["status"] == "needs_input"
    assert payload["outcome"] == "needs_input"
    assert "does not exist" not in str(payload.get("error") or "").lower()


def test_a_out_01_harvest_skips_venv_and_license(tmp_path: Path) -> None:
    """A-OUT-01: .venv / LICENSE are not harvestable deliverables."""
    root = tmp_path / "outbox"
    (root / ".venv" / "lib" / "site-packages" / "pkg").mkdir(parents=True)
    license_file = root / "LICENSE"
    license_file.write_text("MIT\n")
    wheel = root / ".venv" / "lib" / "site-packages" / "pkg" / "mod.py"
    wheel.write_text("x = 1\n")
    csv = root / "results.csv"
    csv.write_text("a,1\n")
    assert harvestable_output(license_file, root) is False
    assert harvestable_output(wheel, root) is False
    assert harvestable_output(csv, root) is True


def test_a_out_02_harvested_deck_does_not_pay_editable_figure() -> None:
    """A-OUT-02: kind=slides pptx does not retire artifact.pptx."""
    deck = SimpleNamespace(
        kind="slides",
        title="deck",
        rel_path="outputs/deck.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        uri="artifact://deck",
    )
    assert remaining_deliverables(["artifact.pptx"], [deck]) == ["artifact.pptx"]
    assert remaining_deliverables(["artifact.slides"], [deck]) == []


@pytest.mark.asyncio
async def test_a_out_03_walkthrough_out_writes_a_bundle_not_checkout_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-OUT-03: --out outputs_walkthrough publishes <title>_<task8>/ under that folder."""
    from omni.agent.orchestrator import _artifact_mirror_dir
    from omni.config import load_settings
    from omni.storage.artifacts import ArtifactStore
    from omni.storage.db import get_database
    from omni.storage.models import TaskORM

    monkeypatch.chdir(tmp_path)
    settings = load_settings(
        cwd=tmp_path,
        trusted=True,
        overrides={"artifacts": {"output_dir": "outputs_walkthrough"}},
    )
    out = (tmp_path / "outputs_walkthrough").resolve()
    assert _artifact_mirror_dir(settings) == out

    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    store = ArtifactStore(settings.paths, db, mirror_dir=out, mirror_formats=["md"])
    task_id = "aout0301walkthru"
    async with store._db.session() as session:
        session.add(
            TaskORM(
                id=task_id,
                session_id=f"session-{task_id}",
                project=store._paths.project_name,
                title="walkthrough output isolation",
            )
        )
        await session.commit()
    art = await store.put_bytes(
        b"# ok\n",
        kind="report",
        title="walkthrough probe",
        ext="md",
        mime="text/markdown",
        task_id=task_id,
    )
    assert art.path.is_file()
    assert art.path.parent.parent == out
    assert art.path.parent.name.endswith("_aout0301")
    assert (art.path.parent / "_omni-manifest.json").is_file()
    root_bundles = [
        path
        for path in tmp_path.iterdir()
        if path.is_dir() and path.name.endswith("_aout0301")
    ]
    assert root_bundles == []


def test_a_out_04_help_gitignore_and_catalog_do_not_use_out_dot() -> None:
    """A-OUT-04: default is outputs/; walkthrough folder is ignored; catalog avoids --out ."""
    from typer.testing import CliRunner

    from omni.cli.main import app

    repo = Path(__file__).resolve().parents[3]
    catalog = (repo / "cli" / "docs" / "user-walkthrough-cases.md").read_text(
        encoding="utf-8"
    )
    gitignore = (repo / ".gitignore").read_text(encoding="utf-8")
    assert "--out outputs_walkthrough" in catalog
    assert ' --out ."' not in catalog
    assert "outputs_walkthrough/" in gitignore
    help_text = CliRunner().invoke(app, ["--help"], env={"COLUMNS": "200"}).stdout
    # Narrow CI terminals wrap Rich cells, so "--out" can split across lines.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", help_text)
    collapsed = "".join(plain.split())
    assert "--out" in collapsed
    assert "outputs/" in help_text
    assert "default: the launch directory" not in help_text


def test_a_chg_01_changelog_prompt_is_git_first_not_repo_grep() -> None:
    """A-CHG-01: changelog turns inject bounded git log instead of repo-wide grep."""
    from omni.core.react_agent import ToolSpec
    from omni.core.system_prompt import build_system_prompt
    from omni.runtime.git_info import repository_history_block, utterance_asks_repo_changelog

    message = (
        "Review the last four days of git commits in this repository. "
        "Analyze new features, optimizations, problems solved, and anything "
        "that looks unreasonable. What were the optimization points?"
    )
    assert utterance_asks_repo_changelog(message)
    repo = Path(__file__).resolve().parents[3]
    block = repository_history_block(repo, message)
    prompt = build_system_prompt(
        role="R",
        tools=[
            ToolSpec("bash", "shell", {"type": "object"}),
            ToolSpec("grep", "search", {"type": "object"}),
        ],
        project_name="walkthrough",
        working_dir=repo,
        repo_history=block,
    )
    assert "git log" in prompt
    assert "repository-wide grep" in prompt
    assert "search_literature" not in prompt
    if "Git is unavailable" not in block:
        assert "[Repository history]" in prompt
        assert "Host-injected" in prompt


def test_a_rev_01_identifier_is_not_resolved_as_a_path() -> None:
    """A-REV-01 slice: the catalog input is an arXiv id, not a missing file."""
    module = _load_paper_review()
    with pytest.raises(module._RemotePaperRef) as caught:
        module._resolve_input("Review arXiv 1706.03762 as a NeurIPS reviewer.")
    assert caught.value.kind == "arxiv"
    assert caught.value.identifier == "1706.03762"
