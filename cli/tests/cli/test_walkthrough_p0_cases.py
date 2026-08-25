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
from unittest.mock import AsyncMock

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


@pytest.mark.asyncio
async def test_a_out_05_harvest_publishes_into_task_bundle_not_promoted(
    tmp_path: Path,
) -> None:
    """A-OUT-05: outbox harvest location is <out>/<title>_<task8>/, not artifacts/promoted."""
    from omni.config import load_settings
    from omni.config.paths import OmniPaths
    from omni.skills_runtime.context import ExecContext
    from omni.skills_runtime.exec_io import durable_output_dir, register_output_dir
    from omni.storage.artifacts import ArtifactStore
    from omni.storage.db import get_database
    from omni.storage.models import TaskORM

    home = tmp_path / "omni-home"
    project_dir = home / "projects" / "demo"
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "outputs"
    settings = load_settings()
    paths = OmniPaths(
        home=home,
        project_name="demo",
        project_dir=project_dir,
        workspace_root=None,
        invocation_cwd=work,
    )
    paths.ensure_dirs()
    settings.paths = paths
    db = get_database(paths.project_db)
    await db.init()
    task_id = "aout0501harvest1"
    async with db.session() as session:
        session.add(
            TaskORM(
                id=task_id,
                status="running",
                kind="turn",
                title="RAG harvest publish",
            )
        )
        await session.commit()
    ctx = ExecContext(
        settings=settings,
        paths=paths,
        project="demo",
        session_id="sess-aout05",
        channel="cli",
        task_id=task_id,
        working_dir=work,
        db=db,
        artifacts=ArtifactStore(paths, db, mirror_dir=out),
    )
    outbox = durable_output_dir(ctx)
    (outbox / "results.csv").write_text("a,1\n", encoding="utf-8")
    (outbox / "deck.pptx").write_bytes(b"PK")
    assert await register_output_dir(ctx, outbox) == 2
    rows = await ctx.artifacts.list_by_task(task_id)
    assert {Path(row.rel_path).name for row in rows} == {"results.csv", "deck.pptx"}
    for row in rows:
        resolved = await ctx.artifacts.resolve_path(row.uri)
        assert resolved is not None
        assert resolved.is_relative_to(out)
        assert "promoted" not in resolved.parts
        assert outbox.resolve() not in resolved.parents
        assert resolved.parent.name.endswith("_aout0501")


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


_A_FIG_01 = (
    "Draw a RAG architecture diagram that includes query, retriever, reranker, and LLM."
)
_A_FIG_04 = (
    "Draw a RAG architecture diagram as SVG using Graphviz, including query, "
    "retriever, reranker, and LLM."
)
_A_LF_01 = (
    "$livefigure Make one editable PPTX slide of a RAG architecture with query, "
    "retriever, reranker, and LLM."
)
_A_LF_02 = "Make this RAG figure one editable scientific figure in PowerPoint."
_A_SLD_01 = "Make a group-meeting deck from the Transformer paper."
_A_SLD_02 = "$research-pptx thesis-defense deck on RAG factuality."
_A_FIG_02 = "Change the previous figure's retriever to hybrid. Keep the color language."
_P_01 = (
    "Prepare materials for a RAG system survey: fetch the abstract of Attention "
    "Is All You Need, generate a scientific architecture figure that includes "
    "query, retriever, reranker, and LLM, and write a paper plus a slide deck."
)
_P_02 = (
    "Prepare materials for an agentic loop-engineering system survey and produce "
    "a detailed introductory slide deck."
)


def _catalog_text() -> str:
    repo = Path(__file__).resolve().parents[3]
    return (repo / "cli" / "docs" / "user-walkthrough-cases.md").read_text(encoding="utf-8")


def _vlm(*, available: bool):
    return SimpleNamespace(
        available=available,
        setup_command="omni config vlm",
        error_code="" if available else "vlm_not_configured",
        missing=() if available else ("model", "endpoint", "api_key"),
    )


def _figure_registry(*, vlm: bool):
    from omni.config import load_settings
    from omni.skills_runtime.registry import SkillRegistry

    registry = SkillRegistry(load_settings())
    registry.build_index()
    registry.use_admission_services({"vlm": _vlm(available=vlm)})
    return registry


def test_a_fig_catalog_covers_format_neutral_routing() -> None:
    """A-FIG-01/03/04/05: catalog names the slot and the default producer."""
    catalog = _catalog_text()
    for token in (
        "A-FIG-01",
        "A-FIG-03",
        "A-FIG-04",
        "A-FIG-05",
        "format-neutral",
        "scientific-figure",
        "figure.editable.pptx",
        "Do not upgrade an ordinary",
        "start condition",
        _A_FIG_01,
        _A_FIG_04,
    ):
        assert token in catalog
    stale = "`artifact.figure` / `scientific-figure`. PNG/SVG."
    assert stale not in catalog


def test_a_fig_01_unspecified_resolves_to_scientific_figure() -> None:
    """A-FIG-01: unspecified architecture → scientific-figure, VLM or not."""
    from omni.skills_runtime.slot_routing import user_named_skill

    assert not user_named_skill(_A_FIG_01, "livefigure")
    assert not user_named_skill(_A_FIG_01, "scientific-figure")
    for vlm in (True, False):
        selected, _ = _figure_registry(vlm=vlm).resolve_capability(
            "artifact.figure", request=_A_FIG_01
        )
        assert selected is not None and selected.name == "scientific-figure"


def test_a_fig_03_unspecified_stays_on_scientific_figure_when_vlm_down() -> None:
    """A-FIG-03: same provider as A-FIG-01. Not needs_input."""
    selected, rejected = _figure_registry(vlm=False).resolve_capability(
        "artifact.figure", request=_A_FIG_01
    )
    assert selected is not None and selected.name == "scientific-figure"
    assert any(
        "livefigure" in item.name and "vlm_not_configured" in why for item, why in rejected
    )


def test_a_fig_04_named_graphviz_is_model_choice_not_host_rerank() -> None:
    """A-FIG-04: host resolve is admission+priority; Graphviz words are not a name."""
    from omni.skills_runtime.slot_routing import user_named_skill

    assert not user_named_skill(_A_FIG_04, "scientific-figure")
    assert not user_named_skill(_A_FIG_04, "livefigure")
    selected, _ = _figure_registry(vlm=True).resolve_capability(
        "artifact.figure", request=_A_FIG_04
    )
    assert selected is not None and selected.name == "scientific-figure"


def test_a_lf_03_named_livefigure_does_not_fall_back() -> None:
    """A-LF-03: named livefigure without VLM does not swap to scientific-figure."""
    from omni.skills_runtime.slot_routing import allow_slot_fallback

    selected, rejected = _figure_registry(vlm=False).resolve_capability(
        "figure.editable.pptx", request=_A_LF_01
    )
    assert selected is None
    assert any(item.name == "livefigure" for item, _ in rejected)
    assert not allow_slot_fallback(
        preferred="livefigure",
        slot="artifact.figure",
        user_message=_A_LF_01,
    )


@pytest.mark.asyncio
async def test_a_fig_05_invented_dot_is_ignored_on_unspecified_figure() -> None:
    """A-FIG-05: host fill drops a model-invented .dot on the A-FIG-01 input."""
    from omni.agent.figure_runner import host_fill_figure

    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-1"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(status="succeeded", error="", result_json={})
        ),
    )
    await host_fill_figure(
        runtime=runtime,
        registry=_figure_registry(vlm=True),
        task_id="t1",
        session_id="s1",
        user_message=_A_FIG_01,
        source_artifact_path="figures/rag.dot",
    )
    params = runtime.enqueue.await_args.args[1]
    assert runtime.enqueue.await_args.args[0] == "scientific-figure"
    assert "source_artifact_path" not in params


def test_a_fig_04_catalog_and_cards_keep_graphviz_on_scientific_figure() -> None:
    """A-FIG-04: descriptions still send a Graphviz-named request to scientific-figure."""
    from omni.agent.skill_lookup import (
        FIND_SKILL_NEXT_ACTION,
        rank_skill_matches,
        skill_contract_card,
    )

    catalog = _catalog_text()
    assert _A_FIG_04 in catalog
    assert "Host resolve is admission+priority" in catalog or "model case" in catalog.casefold()

    registry = _figure_registry(vlm=True)
    services = registry.admission_services()
    live = next(item for item in registry.list_selectable() if item.name == "livefigure")
    sci = next(item for item in registry.list_selectable() if item.name == "scientific-figure")
    live_card = skill_contract_card(live, services=services)
    sci_card = skill_contract_card(sci, services=services)
    live_route = f"{live.description} {live.when_to_use} {live_card.get('when_not_to_use', '')}"
    sci_route = f"{sci.description} {sci.when_to_use} {sci_card.get('when_to_use', '')}"
    assert "graphviz" in sci_route.casefold()
    assert "scientific-figure" in live_route.casefold()
    assert "first-choice" not in live.description.casefold()
    assert live_card["next_action"] == FIND_SKILL_NEXT_ACTION
    assert "call run_skill now" in FIND_SKILL_NEXT_ACTION.casefold()
    named = rank_skill_matches(registry.list_selectable(), "scientific-figure", services=services)
    assert named[0].name == "scientific-figure"


@pytest.mark.asyncio
async def test_a_fig_05_user_dot_is_passed_through() -> None:
    """A-FIG-05: only an explicit scientific-figure path may pass a task .dot."""
    from omni.agent.figure_runner import host_fill_figure

    runtime = SimpleNamespace(
        enqueue=AsyncMock(return_value="sub-1"),
        process=AsyncMock(),
        get_subtask=AsyncMock(
            return_value=SimpleNamespace(status="succeeded", error="", result_json={})
        ),
    )
    await host_fill_figure(
        runtime=runtime,
        registry=_figure_registry(vlm=True),
        task_id="t1",
        session_id="s1",
        user_message="Draw a RAG architecture diagram using Graphviz from rag.dot",
        source_artifact_path="figures/rag.dot",
    )
    assert runtime.enqueue.await_args.args[0] == "scientific-figure"
    assert "source_artifact_path" not in runtime.enqueue.await_args.args[1]

    await host_fill_figure(
        runtime=runtime,
        registry=_figure_registry(vlm=True),
        task_id="t1",
        session_id="s1",
        user_message="$scientific-figure render rag.dot",
        source_artifact_path="figures/rag.dot",
        explicit_skill="scientific-figure",
        pass_source=True,
    )
    assert runtime.enqueue.await_args.args[0] == "scientific-figure"
    assert runtime.enqueue.await_args.args[1]["source_artifact_path"] == "figures/rag.dot"


def test_a_fig_02_revise_stays_on_artifact_revise() -> None:
    """A-FIG-02: change-the-previous-figure is revise, not a new Graphviz contract."""
    from omni.agent.model_planner import _planner_system_prompt
    from omni.config import load_settings
    from omni.runtime.remaining import infer_figure_and_paper_outputs, infer_slide_outputs
    from omni.skills_runtime.registry import SkillRegistry
    from omni.skills_runtime.slot_routing import user_named_skill

    assert not user_named_skill(_A_FIG_02, "scientific-figure")
    assert not user_named_skill(_A_FIG_02, "livefigure")
    assert infer_figure_and_paper_outputs(_A_FIG_02) == []
    assert infer_slide_outputs(_A_FIG_02) == []
    registry = SkillRegistry(load_settings())
    registry.build_index()
    prompt = _planner_system_prompt(registry)
    assert "artifact.revise" in prompt
    assert "Prefer livefigure" not in prompt
    assert "Do not name a concrete provider" in prompt


def test_a_lf_01_and_02_bind_livefigure_not_a_deck() -> None:
    """A-LF-01 / A-LF-02: named editable PPTX stays on livefigure when VLM is up."""
    from omni.runtime.remaining import infer_slide_outputs
    from omni.skills_runtime.slot_routing import (
        allow_slot_fallback,
        user_named_skill,
    )

    assert user_named_skill(_A_LF_01, "livefigure")
    assert not user_named_skill(_A_LF_02, "livefigure")
    assert not allow_slot_fallback(
        preferred="livefigure",
        slot="artifact.figure",
        user_message=_A_LF_01,
    )
    for message in (_A_LF_01, _A_LF_02):
        assert infer_slide_outputs(message) == []
        selected, _ = _figure_registry(vlm=True).resolve_capability(
            "figure.editable.pptx", request=message
        )
        assert selected is not None and selected.name == "livefigure"


def test_a_sld_01_and_02_bind_slides_not_a_figure() -> None:
    """A-SLD-01 / A-SLD-02: a deck is research-pptx, not livefigure."""
    from omni.runtime.remaining import infer_figure_and_paper_outputs, infer_slide_outputs
    from omni.skills_runtime.slot_routing import user_named_skill

    assert infer_slide_outputs(_A_SLD_01) == ["artifact.slides"]
    assert infer_figure_and_paper_outputs(_A_SLD_01) == []
    assert not user_named_skill(_A_SLD_01, "livefigure")
    assert infer_slide_outputs(_A_SLD_02) == ["artifact.slides"]
    registry = _figure_registry(vlm=True)
    assert registry.suggest(_A_SLD_01)[0].name == "research-pptx"
    assert registry.suggest("research-pptx thesis-defense deck")[0].name == "research-pptx"


def test_p01_and_p02_keep_figure_and_deck_distinct() -> None:
    """P-01 / P-02: figure, manuscript, and slides stay separate debts."""
    from omni.runtime.remaining import infer_figure_and_paper_outputs, infer_slide_outputs

    assert infer_figure_and_paper_outputs(_P_01) == ["artifact.figure", "draft.manuscript"]
    assert infer_slide_outputs(_P_01) == ["artifact.slides"]
    assert infer_figure_and_paper_outputs(_P_02) == []
    assert infer_slide_outputs(_P_02) == ["artifact.slides"]
    on = _figure_registry(vlm=True)
    off = _figure_registry(vlm=False)
    selected_on, _ = on.resolve_capability("artifact.figure", request=_P_01)
    selected_off, rejected = off.resolve_capability("artifact.figure", request=_P_01)
    assert selected_on is not None and selected_on.name == "scientific-figure"
    assert selected_off is not None and selected_off.name == "scientific-figure"
    assert any("vlm_not_configured" in why for _, why in rejected)
    slides, _ = on.resolve_capability("slides.generate", request=_P_01)
    assert slides is not None and slides.name == "research-pptx"


def test_e_03_named_livefigure_does_not_silent_swap() -> None:
    """E-03 / A-LF-03: a required editable slide does not degrade to Graphviz."""
    from omni.skills_runtime.slot_routing import allow_slot_fallback, skip_observation

    message = (
        "In one request: search RAG 2024 papers, write a two-paragraph related-work "
        "section, and make one editable LiveFigure PPTX slide."
    )
    selected, rejected = _figure_registry(vlm=False).resolve_capability(
        "figure.editable.pptx", request=message
    )
    assert selected is None
    assert any(item.name == "livefigure" for item, _ in rejected)
    assert not allow_slot_fallback(
        preferred="livefigure",
        slot="artifact.figure",
        user_message=message,
    )
    notice = skip_observation(
        skipped="livefigure",
        fallback="scientific-figure",
        reason="vlm_not_configured",
    )
    assert "Using scientific-figure instead" in notice


def test_react_catalog_distinguishes_figure_skills() -> None:
    """Catalog the coordinator sees still distinguishes the two figure skills."""
    from omni.config import load_settings
    from omni.skills_runtime.registry import SkillRegistry

    registry = SkillRegistry(load_settings())
    registry.build_index()
    text = registry.react_skill_catalog().casefold()
    assert "livefigure" in text and "scientific-figure" in text
    assert "first-choice" not in text
    assert "graphviz" in text
    assert "find_skill that exact name" in text or "follow the" in text
    assert "prefer livefigure" not in _planner_prompt().casefold()


def _planner_prompt() -> str:
    from omni.agent.model_planner import _planner_system_prompt
    from omni.config import load_settings
    from omni.skills_runtime.registry import SkillRegistry

    registry = SkillRegistry(load_settings())
    registry.build_index()
    return _planner_system_prompt(registry)
