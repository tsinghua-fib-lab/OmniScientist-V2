"""Offline contracts for portable non-VLM built-in skill adaptations."""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omni.skills_runtime.discovery import active_skill_names
from omni.skills_runtime.manifest import execution_budget_warnings

SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"
ACTIVE_BUILTIN_SKILLS = frozenset(active_skill_names(SKILLS_ROOT))
REMOVED_RESEARCH_SKILLS = frozenset(
    {
        "corpus-index",
        "lit-qa",
        "literature-search",
        "semanticscholar-search",
    }
)


def _frontmatter(skill_name: str) -> dict:
    text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])


def _load_module(skill_name: str, filename: str, module_name: str):
    path = SKILLS_ROOT / skill_name / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _all_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _all_strings(item)]
    if isinstance(value, (list, tuple, set)):
        return [text for item in value for text in _all_strings(item)]
    return []


def test_engine_backed_skills_do_not_publish_host_specific_tool_names() -> None:
    for skill_name in ACTIVE_BUILTIN_SKILLS:
        frontmatter = _frontmatter(skill_name)
        if frontmatter["metadata"]["helixforge"]["kind"] != "python_engine":
            continue
        assert "allowed-tools" not in frontmatter, skill_name


def test_prompt_only_review_skills_publish_bounded_tool_contracts() -> None:
    expected = {"review-response"}
    actual = {
        name
        for name in ACTIVE_BUILTIN_SKILLS
        if _frontmatter(name)["metadata"]["helixforge"]["kind"] == "prompt_only"
    }

    assert expected <= actual
    for skill_name in expected:
        frontmatter = _frontmatter(skill_name)
        assert frontmatter.get("allowed-tools"), skill_name
        execution = frontmatter["metadata"]["helixforge"]["execution"]
        assert execution["max_tool_calls"] <= 40, skill_name
        # The iteration ceiling is bounded by coherence with the tool budget,
        # not by an arbitrary number: a review that may make 40 tool calls but
        # is allowed only 8 turns always stops on iterations first, which is
        # what made complex reviews fail short of a verdict.
        assert execution_budget_warnings(execution, skill_name) == []


def test_paper_review_uses_a_complete_pipeline_engine() -> None:
    frontmatter = _frontmatter("paper-review")
    helix = frontmatter["metadata"]["helixforge"]

    assert helix["kind"] == "python_engine"
    assert helix["engine"] == {
        "module": "engine",
        "class": "PaperReviewEngine",
        "method": "execute",
    }
    assert "allowed-tools" not in frontmatter
    assert helix["execution"]["max_seconds"] <= 1200


def test_paper_review_declares_bounded_mineru_runtime_options() -> None:
    frontmatter = _frontmatter("paper-review")
    properties = frontmatter["metadata"]["helixforge"]["input_schema"]["properties"]

    assert properties["mineru_command"]["type"] == "string"
    assert properties["mineru_command"]["default"] == "mineru"
    assert properties["mineru_backend"] == {
        "type": "string",
        "enum": ["pipeline"],
        "default": "pipeline",
        "description": "bounded MinerU extraction backend",
    }
    assert properties["mineru_timeout_s"]["type"] == "number"
    assert properties["mineru_timeout_s"]["minimum"] == 1
    assert properties["mineru_timeout_s"]["maximum"] == 600
    assert properties["mineru_timeout_s"]["default"] == 600
    assert properties["mineru_device"] == {
        "type": "string",
        "default": "auto",
        "description": "auto-select the freest visible GPU, or pin cpu/cuda:N",
    }


def test_paper_review_enables_historical_review_rag_by_default() -> None:
    frontmatter = _frontmatter("paper-review")
    properties = frontmatter["metadata"]["helixforge"]["input_schema"]["properties"]

    assert properties["review_rag"]["default"] == "on"
    assert properties["review_rag"]["enum"] == ["auto", "on", "off"]


def test_omni_tool_policies_are_namespaced_without_losing_existing_permissions() -> None:
    expected = {
        "arxiv-fetch": ["bash", "write_file"],
        "openalex-search": ["bash", "write_file"],
        "scientific-figure": [
            "write_file",
            "bash",
            "read_file",
            "cite_source",
            "record_claim",
            "add_evidence",
            "log_run",
        ],
        "scientific-poster": [
            "write_file",
            "bash",
            "read_file",
            "cite_source",
            "record_claim",
            "add_evidence",
            "log_run",
        ],
        "livefigure": ["write_file", "bash", "read_file", "log_run"],
        "research-ideation": [],
        "research-pptx": [
            "arxiv-fetch",
            "log_run",
            "cite_source",
            "record_claim",
            "add_evidence",
        ],
    }

    for skill_name, allowed_tools in expected.items():
        helixforge = _frontmatter(skill_name)["metadata"]["helixforge"]
        assert helixforge.get("allowed_tools", []) == allowed_tools, skill_name


def test_builtin_frontmatter_uses_codex_claude_portable_top_level_fields() -> None:
    allowed = {"name", "description", "license", "allowed-tools", "metadata"}

    for skill_name in ACTIVE_BUILTIN_SKILLS:
        frontmatter = _frontmatter(skill_name)
        assert set(frontmatter) <= allowed, skill_name
        helix = frontmatter["metadata"]["helixforge"]
        assert helix.get("version"), skill_name
        assert helix.get("dependencies"), skill_name


@pytest.mark.parametrize(
    ("skill_name", "field_name"),
    [
        ("scientific-poster", "input"),
        ("paper-review", "input"),
        ("review-response", "input"),
        ("research-pptx", "topic"),
    ],
)
def test_cli_triggerable_skills_declare_an_omni_instruction_field(
    skill_name: str,
    field_name: str,
) -> None:
    helix = _frontmatter(skill_name)["metadata"]["helixforge"]
    field = helix["input_schema"]["properties"][field_name]

    assert field["x-omni"]["semantic_role"] == "instruction"


def test_arxiv_fetch_user_results_never_recommend_removed_skills() -> None:
    module = _load_module(
        "arxiv-fetch",
        "engine.py",
        "portable_adaptation_arxiv_fetch_engine",
    )

    result = module.ArxivFetchEngine.validate_params(arguments={"identifier": "not-an-arxiv-id"})
    assert result is not None
    user_visible_text = "\n".join(_all_strings(result))

    for removed_name in REMOVED_RESEARCH_SKILLS:
        assert removed_name not in user_visible_text
    assert "next_tools" not in result
    assert result.get("next_capabilities") == ["literature.search"]


def test_openalex_advertises_search_without_claiming_corpus_ownership() -> None:
    frontmatter = _frontmatter("openalex-search")
    helix = frontmatter["metadata"]["helixforge"]

    assert helix["capabilities"] == ["literature.search"]
    assert "index" not in frontmatter["description"].lower()


@pytest.mark.parametrize("skill_name", ["arxiv-fetch", "openalex-search"])
def test_search_skill_documentation_does_not_reference_removed_skills(skill_name: str) -> None:
    text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")

    for removed_name in REMOVED_RESEARCH_SKILLS:
        assert removed_name not in text


def test_research_ideation_has_no_process_global_llm_configuration() -> None:
    core_source = (SKILLS_ROOT / "research-ideation" / "core.py").read_text(encoding="utf-8")
    engine_source = (SKILLS_ROOT / "research-ideation" / "engine.py").read_text(encoding="utf-8")
    tree = ast.parse(core_source)
    module_assignments = {
        node.target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    } | {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert "_llm_config" not in module_assignments
    assert "_core.configure_llm(" not in engine_source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("LLM endpoint/API key is not configured"),
        type(
            "AuthenticationError",
            (RuntimeError,),
            {"status_code": 401},
        )("invalid API key"),
    ],
)
async def test_research_ideation_configuration_failures_are_blocking_and_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    engine_module = _load_module(
        "research-ideation",
        "engine.py",
        "portable_adaptation_research_ideation_engine",
    )

    def fail_pipeline(**_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(engine_module._core, "run_pipeline", fail_pipeline)
    monkeypatch.setattr(
        engine_module,
        "_resolve_s2_key",
        lambda _ctx: "scoped-s2-secret",
    )
    engine = engine_module.ResearchIdeationEngine()
    engine.ctx = SimpleNamespace(
        llm=object(),
        artifacts=None,
        paths=None,
    )

    result = await engine.execute(input="Generate a research idea")

    assert result["status"] == "error"
    assert result["error_info"]["retryable"] is False
    assert result["error_info"]["workflow_recoverable"] is False
    assert result["blocking"] is True

@pytest.mark.asyncio
async def test_research_pptx_review_plan_does_not_require_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_module = _load_module(
        "research-pptx",
        "engine.py",
        "portable_adaptation_research_pptx_engine",
    )
    pipeline_started = False

    async def fake_pipeline(
        _engine: object,
        req: object,
        *_args: object,
        **_kwargs: object,
    ) -> dict:
        nonlocal pipeline_started
        pipeline_started = True
        assert vars(req)["review_mode"] == "plan"
        return {"status": "partial", "outcome": {"code": "awaiting_review"}}

    monkeypatch.setattr(engine_module.ResearchPptxEngine, "_run", fake_pipeline)
    monkeypatch.setattr(
        engine_module._slide_renderer,
        "preflight_renderer",
        lambda: (_ for _ in ()).throw(
            AssertionError("review planning must not inspect renderer dependencies")
        ),
    )

    result = await engine_module.ResearchPptxEngine().execute(
        topic="RAG systems",
        review_mode="plan",
    )

    assert pipeline_started is True
    assert result == {"status": "partial", "outcome": {"code": "awaiting_review"}}


@pytest.mark.asyncio
async def test_research_pptx_uses_omni_workflow_context_to_skip_review_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_module = _load_module(
        "research-pptx",
        "engine.py",
        "portable_adaptation_research_pptx_workflow_context_engine",
    )

    async def fake_pipeline(
        _engine: object,
        req: object,
        *_args: object,
        **_kwargs: object,
    ) -> dict:
        assert vars(req)["review_mode"] == "none"
        return {"status": "ok"}

    monkeypatch.setattr(engine_module.ResearchPptxEngine, "_run", fake_pipeline)
    engine = engine_module.ResearchPptxEngine()
    engine.ctx = SimpleNamespace(workflow_step_id="step-1")

    result = await engine.execute(topic="RAG systems", review_mode="plan")

    assert result == {"status": "ok"}


def test_research_pptx_extracts_embedded_source_and_template_paths(tmp_path: Path) -> None:
    engine_module = _load_module(
        "research-pptx",
        "engine.py",
        "portable_adaptation_research_pptx_path_extraction",
    )
    paper = tmp_path / "source paper.pdf"
    template = tmp_path / "conference template.pptx"
    paper.write_bytes(b"%PDF-test")
    template.write_bytes(b"pptx-test")
    arguments = {
        "topic": f'Create slides from "{paper}" using "{template}" as the template.'
    }

    error = engine_module.ResearchPptxEngine.validate_params(arguments=arguments)

    assert error is None
    assert arguments["pdf_uri"] == str(paper)
    assert arguments["template_uri"] == str(template)


def test_research_pptx_extracts_path_after_input_to_topic_fallback(tmp_path: Path) -> None:
    engine_module = _load_module(
        "research-pptx",
        "engine.py",
        "portable_adaptation_research_pptx_fallback_path_extraction",
    )
    markdown = tmp_path / "研究提纲.md"
    markdown.write_text("# Outline\n", encoding="utf-8")
    arguments = {"input": f'请根据“{markdown}”生成汇报幻灯片'}

    error = engine_module.ResearchPptxEngine.validate_params(arguments=arguments)

    assert error is None
    assert arguments["topic"] == arguments["input"]
    assert arguments["markdown_uri"] == str(markdown)


def test_research_pptx_does_not_treat_export_source_as_template(tmp_path: Path) -> None:
    engine_module = _load_module(
        "research-pptx",
        "engine.py",
        "portable_adaptation_research_pptx_export_path_extraction",
    )
    deck = tmp_path / "existing deck.pptx"
    deck.write_bytes(b"pptx-test")
    arguments = {"topic": f'Convert "{deck}" to PDF.'}

    error = engine_module.ResearchPptxEngine.validate_params(arguments=arguments)

    assert error is None
    assert "template_uri" not in arguments


def test_research_pptx_reports_missing_extracted_markdown_path(tmp_path: Path) -> None:
    engine_module = _load_module(
        "research-pptx",
        "engine.py",
        "portable_adaptation_research_pptx_missing_markdown",
    )
    missing = tmp_path / "missing outline.md"
    arguments = {"topic": f'Generate slides from "{missing}".'}

    error = engine_module.ResearchPptxEngine.validate_params(arguments=arguments)

    # A mention is not a required handle. topic itself is a valid source.
    assert error is None
    assert "markdown_uri" not in arguments


@pytest.mark.asyncio
async def test_research_pptx_never_runs_npm_ci_during_a_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    renderer = _load_module(
        "research-pptx",
        "slide_renderer.py",
        "portable_adaptation_research_pptx_renderer",
    )
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(renderer, "_SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(
        renderer.shutil,
        "which",
        lambda name: "/usr/bin/node" if name == "node" else None,
    )
    spawned: list[tuple[object, ...]] = []

    async def record_spawn(*args: object, **_kwargs: object) -> None:
        spawned.append(args)
        raise AssertionError("task execution must not launch a package installer")

    monkeypatch.setattr(renderer.asyncio, "create_subprocess_exec", record_spawn)

    with pytest.raises(renderer.RendererDependencyError) as caught:
        await renderer.render_pptx(object(), str(tmp_path))

    assert set(caught.value.missing) == {"pptxgenjs", "sharp"}
    assert spawned == []


def test_research_pptx_runtime_does_not_require_npm_after_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    renderer = _load_module(
        "research-pptx",
        "slide_renderer.py",
        "portable_adaptation_research_pptx_runtime_renderer",
    )
    scripts_dir = tmp_path / "scripts"
    for package in ("pptxgenjs", "sharp"):
        (scripts_dir / "node_modules" / package).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(renderer, "_SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(
        renderer.shutil,
        "which",
        lambda name: "/usr/bin/node" if name == "node" else None,
    )

    assert renderer.preflight_renderer() == "/usr/bin/node"


@pytest.mark.asyncio
async def test_research_pptx_omni_runtime_resolves_modules_outside_installed_skill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    renderer = _load_module(
        "research-pptx",
        "slide_renderer.py",
        "portable_adaptation_research_pptx_external_runtime_renderer",
    )
    scripts_dir = tmp_path / "site-packages" / "omni" / "data" / "skills" / "research-pptx" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "generate_slides.js").write_text("// renderer", encoding="utf-8")
    runtime_dir = tmp_path / "omni-cache" / "skill-runtimes" / "research-pptx"
    for package in ("pptxgenjs", "sharp"):
        (runtime_dir / "node_modules" / package).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(renderer, "_SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(
        renderer.shutil,
        "which",
        lambda name: "/usr/bin/node" if name == "node" else None,
    )
    spawned: list[dict[str, object]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        Path(str(args[3])).write_bytes(b"pptx")
        spawned.append(kwargs)
        return FakeProcess()

    monkeypatch.setattr(renderer.asyncio, "create_subprocess_exec", fake_spawn)
    plan = SimpleNamespace(
        title="Runtime test",
        authors=[],
        affiliation="",
        venue="",
        color_theme={},
        slides=[],
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    output = await renderer.render_pptx(
        plan,
        str(work_dir),
        node_runtime_dir=runtime_dir,
    )

    assert Path(output).read_bytes() == b"pptx"
    assert spawned[0]["env"]["NODE_PATH"].split(os.pathsep)[0] == str(
        runtime_dir / "node_modules"
    )


@pytest.mark.asyncio
async def test_research_pptx_reports_renderer_dependency_at_render_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine_module = _load_module(
        "research-pptx",
        "engine.py",
        "portable_adaptation_research_pptx_render_stage_engine",
    )
    engine_module._cache_content_for_render(
        str(tmp_path),
        engine_module.ParsedContent(source_type="prompt", markdown_text="RAG systems"),
    )
    plan = engine_module.PresentationPlan(
        title="RAG systems",
        slides=[
            engine_module._models.SlideData(
                slide_type="title",
                title="RAG systems",
            )
        ],
    )
    request = engine_module.PresentationRequest(topic="RAG systems")
    monkeypatch.setattr(engine_module._slide_renderer, "_SCRIPTS_DIR", tmp_path)
    monkeypatch.setattr(engine_module._slide_renderer.shutil, "which", lambda _name: None)

    class Telemetry:
        @staticmethod
        def stage(*_args: object, **_kwargs: object) -> None:
            return None

    async def progress(*_args: object, **_kwargs: object) -> None:
        return None

    engine = engine_module.ResearchPptxEngine()
    engine.ctx = SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path / "omni-cache"))
    result = await engine._render_and_finish(
        plan,
        str(tmp_path),
        request,
        Telemetry(),
        progress,
        0.0,
        lambda: 0,
    )

    assert result["status"] == "error"
    assert result["error_info"]["code"] == "node_unavailable"
    assert result["error_info"]["missing"] == ["node"]
    assert result["action_required"] == {
        "kind": "install",
        "command": result["setup_command"],
        "missing": ["node"],
    }
    assert result["setup_command"] == "omni skills setup research-pptx"
    assert "site-packages" not in result["error"]


def test_research_pptx_manifest_only_admits_invariant_runtime_requirements() -> None:
    requirements = _frontmatter("research-pptx")["metadata"]["helixforge"].get(
        "runtime_requirements", {}
    )

    # PDF parsing, equation rendering, template reuse, and Node rendering are
    # phase-specific. A global admission check must not block outline planning.
    assert not requirements.get("bins")
    assert not requirements.get("python_modules")
    assert not requirements.get("paths")
    assert not requirements.get("setup")


def test_openclaw_metadata_declares_actual_portable_runner_requirements() -> None:
    expected = {
        "arxiv-fetch": ({"python3"}, set()),
        "openalex-search": ({"python3"}, set()),
        "scientific-figure": ({"python3"}, set()),
        "scientific-poster": ({"python3"}, set()),
        # Omni/MCP is the reliable first integration. OpenClaw can discover the
        # skill without inheriting the owner VLM credential.
        "livefigure": ({"python3"}, set()),
        "research-ideation": (
            {"python3"},
            {"LLM_GATEWAY_BASE_URL", "LLM_GATEWAY_API_KEY"},
        ),
        "research-pptx": (
            {"python3", "node"},
            {"OPENAI_BASE_URL", "OPENAI_API_KEY"},
        ),
    }

    for skill_name, (expected_bins, expected_env) in expected.items():
        requirements = _frontmatter(skill_name)["metadata"]["openclaw"]["requires"]
        assert set(requirements.get("bins", ())) == expected_bins, skill_name
        assert set(requirements.get("env", ())) == expected_env, skill_name

    for skill_name in ("arxiv-fetch", "openalex-search", "scientific-figure"):
        requirements = _frontmatter(skill_name)["metadata"]["openclaw"]["requires"]
        assert "env" not in requirements, skill_name


def test_research_pptx_uses_references_for_progressive_disclosure() -> None:
    skill_dir = SKILLS_ROOT / "research-pptx"
    skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    reference_files = sorted((skill_dir / "references").glob("*.md"))

    assert len(skill_text.splitlines()) < 500
    assert reference_files
    assert any(f"references/{path.name}" in skill_text for path in reference_files)
