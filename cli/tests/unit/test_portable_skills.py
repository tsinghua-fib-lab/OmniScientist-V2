"""Portable skill runners for Claude Code / Codex / OpenClaw users."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.discovery import active_skill_names
from omni.skills_runtime.executor import execute_skill
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.artifacts import ArtifactStore
from omni.storage.db import get_database

SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"

BUILTIN_SKILLS = frozenset(active_skill_names(SKILLS_ROOT))

EXTERNAL_HOSTS = {
    "claude-code": ".claude/skills",
    "codex": ".codex/skills",
    "openclaw": ".openclaw/skills",
}

ENGINE_SMOKE_INPUTS = {
    "arxiv-fetch": {},
    "livefigure": {"input": "Create an editable RAG architecture PPTX"},
    "openalex-search": {},
    "research-ideation": {},
    "research-pptx": {},
    "scientific-figure": {"input": "RAG system architecture with query, retriever, reranker, LLM"},
    "scientific-poster": {},
}


def _frontmatter(skill_name: str) -> dict:
    text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])


def _python_engine_skills() -> list[str]:
    out: list[str] = []
    for skill_name in sorted(BUILTIN_SKILLS):
        helix = _frontmatter(skill_name)["metadata"]["helixforge"]
        if helix.get("kind") == "python_engine":
            out.append(skill_name)
    return out


def test_builtin_skills_document_three_portability_modes():
    for skill_name in sorted(BUILTIN_SKILLS):
        text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")

        assert "## External agent portability" in text
        assert "Copy-only mode" in text
        assert "Portable runner mode" in text
        assert "Omni enhanced mode" in text
        assert "The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support." in text


def test_python_engine_skills_publish_portable_runner_entrypoints():
    for skill_name in _python_engine_skills():
        run_py = SKILLS_ROOT / skill_name / "scripts" / "run.py"
        assert run_py.is_file(), f"{skill_name} lacks scripts/run.py"

        text = run_py.read_text(encoding="utf-8")
        assert "import omni" not in text
        assert "from omni" not in text
        assert "--self-test" in text

        skill_text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert "python3 scripts/run.py" in skill_text


def test_arxiv_fetch_runner_and_engine_share_portable_core():
    skill_dir = SKILLS_ROOT / "arxiv-fetch"

    assert (skill_dir / "core.py").is_file()
    assert "from core import fetch" in (skill_dir / "scripts" / "run.py").read_text(encoding="utf-8")
    assert "core.py" in (skill_dir / "engine.py").read_text(encoding="utf-8")


def test_python_engine_portable_runner_self_tests_pass_offline():
    for skill_name in _python_engine_skills():
        proc = subprocess.run(
            [sys.executable, "scripts/run.py", "--self-test"],
            cwd=SKILLS_ROOT / skill_name,
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        payload = json.loads(proc.stdout)
        assert payload["status"] == "ok"
        assert payload["skill"] == skill_name
        assert payload["portable_runner"] is True


def test_portable_runners_return_structured_input_errors():
    cases = {
        "arxiv-fetch": {},
        "openalex-search": {},
        "livefigure": {},
        "research-ideation": {},
        "research-pptx": {},
        "scientific-figure": {},
    }
    for skill_name, payload in cases.items():
        proc = subprocess.run(
            [sys.executable, "scripts/run.py", "--json", json.dumps(payload)],
            cwd=SKILLS_ROOT / skill_name,
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        result = json.loads(proc.stdout)
        assert result["status"] == "error"
        assert result["skill"] == skill_name
        assert result.get("error")


def test_scientific_poster_portable_runner_hands_authoring_to_host_model():
    proc = subprocess.run(
        [sys.executable, "scripts/run.py", "--json", "{}"],
        cwd=SKILLS_ROOT / "scientific-poster",
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    result = json.loads(proc.stdout)

    assert result["status"] == "partial"
    assert result["outcome"]["code"] == "host_agent_required"
    assert result["recoverable"] is True
    assert result["skill"] == "scientific-poster"


@pytest.mark.parametrize("source", ["argument", "stdin"])
def test_research_ideation_runner_returns_structured_invalid_json(source: str):
    command = [sys.executable, "scripts/run.py"]
    stdin = None
    if source == "argument":
        command.extend(["--json", "{bad-json"])
    else:
        stdin = "{bad-json"

    proc = subprocess.run(
        command,
        cwd=SKILLS_ROOT / "research-ideation",
        input=stdin,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    result = json.loads(proc.stdout)

    assert result["status"] == "error"
    assert result["skill"] == "research-ideation"
    assert result["error_info"]["code"] == "invalid_json"
    assert "Traceback" not in proc.stderr


def test_scientific_figure_portable_runner_creates_local_artifacts(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run.py",
            "--json",
            json.dumps({"input": "生成 Transformer/RAG 架构科研图", "output_dir": str(tmp_path)}),
        ],
        cwd=SKILLS_ROOT / "scientific-figure",
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    result = json.loads(proc.stdout)

    assert result["status"] == "ok"
    assert result["skill"] == "scientific-figure"
    assert any(a["format"] == "dot" for a in result["artifacts"])
    assert any(a["format"] == "svg" for a in result["artifacts"])
    for artifact in result["artifacts"]:
        assert Path(artifact["path"]).is_file()


def test_scientific_figure_generic_runner_does_not_guess_business_vocabulary(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run.py",
            "--json",
            json.dumps(
                {
                    "input": "Microservices architecture: gateway, auth, orders, database",
                    "output_dir": str(tmp_path),
                }
            ),
        ],
        cwd=SKILLS_ROOT / "scientific-figure",
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    result = json.loads(proc.stdout)
    dot_path = next(Path(item["path"]) for item in result["artifacts"] if item["format"] == "dot")
    dot = dot_path.read_text(encoding="utf-8")

    assert "Transformer" not in dot
    assert "Microservices Architecture" not in dot
    for guessed_component in ("API Gateway", "Auth Service", "Orders Service", "Database"):
        assert guessed_component not in dot
    for generic_component in ("Input", "Core Process", "Output"):
        assert generic_component in dot


def test_claude_codex_openclaw_copy_mode_can_discover_all_builtin_skills(tmp_path):
    for host_name, relative_root in EXTERNAL_HOSTS.items():
        host_root = tmp_path / host_name / relative_root
        host_root.mkdir(parents=True)

        for skill_name in sorted(BUILTIN_SKILLS):
            copied_skill = host_root / skill_name
            shutil.copytree(SKILLS_ROOT / skill_name, copied_skill)

            text = (copied_skill / "SKILL.md").read_text(encoding="utf-8")
            frontmatter = yaml.safe_load(text.split("---", 2)[1])
            assert frontmatter["name"] == skill_name
            assert frontmatter["description"]
            assert "metadata" in frontmatter
            assert (copied_skill / "LICENSE.txt").is_file()
            assert (copied_skill / "NOTICE.md").is_file()


def test_every_builtin_skill_is_licensed_for_standalone_distribution():
    for skill_name in sorted(BUILTIN_SKILLS):
        skill_dir = SKILLS_ROOT / skill_name
        license_text = (skill_dir / "LICENSE.txt").read_text(encoding="utf-8")
        notice_text = (skill_dir / "NOTICE.md").read_text(encoding="utf-8")

        assert "Apache License" in license_text
        assert "OmniScientist V2" in notice_text


def test_contributed_skill_notices_record_exact_upstream_revision():
    revisions = {
        "livefigure": "bd6d406de2e1c09652e763f6259cc6167a0f61e7",
        "paper-review": "2f75b9f5a7d20dc744eead1f1100a9673596f88a",
        "research-ideation": "88dbfe6699fc8fddd04278c00599f252894a1b3d",
        "research-pptx": "a7def530f1b15434d5eb8344cb5e141d78658f0f",
        "review-response": "442872e9af4045570ca5d95422cf970a29e5639b",
        "scientific-poster": "b2104fe288aa4869fe5929cac2ecabb609defcb1",
    }
    for skill_name, revision in revisions.items():
        notice = (SKILLS_ROOT / skill_name / "NOTICE.md").read_text(encoding="utf-8")
        assert "https://gitee.com/zgc-omni/omniscientistv2.git" in notice
        assert revision in notice
        assert "adapted" in notice.lower()


def test_copied_runner_self_tests_work_in_external_host_layouts(tmp_path):
    for host_name, relative_root in EXTERNAL_HOSTS.items():
        host_root = tmp_path / host_name / relative_root
        host_root.mkdir(parents=True)

        for skill_name in _python_engine_skills():
            copied_skill = host_root / skill_name
            shutil.copytree(SKILLS_ROOT / skill_name, copied_skill)
            proc = subprocess.run(
                [sys.executable, "scripts/run.py", "--self-test"],
                cwd=copied_skill,
                text=True,
                capture_output=True,
                check=True,
                timeout=20,
            )
            payload = json.loads(proc.stdout)
            assert payload == {"status": "ok", "skill": skill_name, "portable_runner": True}


def test_manifest_driven_registry_exposes_active_capability_table(settings):
    registry = SkillRegistry(settings)
    registry.build_index()

    discovered = {entry.name for entry in registry.list_all()}
    assert discovered == BUILTIN_SKILLS

    expected = {
        "literature.search": "openalex-search",
        "paper.fetch.arxiv": "arxiv-fetch",
        "artifact.figure": "scientific-figure",
        "figure.editable.pptx": "livefigure",
        "poster.scientific": "scientific-poster",
        "review.paper": "paper-review",
        "review.response": "review-response",
        "research.ideation": "research-ideation",
        "slides.generate": "research-pptx",
        "artifact.slides": "research-pptx",
    }
    for capability, provider_name in expected.items():
        provider, _ = registry.resolve_capability(capability)
        assert provider is not None and provider.name == provider_name

    livefigure = registry.get("livefigure")
    assert livefigure is not None
    assert "artifact.figure" not in livefigure.capabilities

    catalog = registry.selection_prompt()
    assert "Skill contract catalog for workflow planning" in catalog
    assert "capabilities: paper.fetch.arxiv" in catalog
    assert "outcome" in catalog


def test_catalog_search_disambiguates_single_editable_figure_from_full_deck(settings):
    registry = SkillRegistry(settings)
    registry.build_index()

    assert registry.suggest("Create a single editable PPTX scientific figure")[0].name == "livefigure"
    assert registry.suggest("Generate a complete 12-slide thesis defense deck")[0].name == "research-pptx"
    assert registry.suggest("Draw an ordinary RAG architecture diagram as SVG")[0].name == "scientific-figure"
    assert registry.suggest("Create an ordinary workflow flowchart as SVG")[0].name == "scientific-figure"
    assert registry.suggest("Create a complete group meeting presentation")[0].name == "research-pptx"


@pytest.mark.asyncio
async def test_omni_and_helixforge_python_engine_entrypoints_are_callable(settings):
    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    registry = SkillRegistry(settings)
    registry.build_index()
    ctx = ExecContext(
        settings=settings,
        paths=settings.paths,
        project=settings.paths.project_name,
        session_id="host-sim",
        channel="cli",
        db=db,
        artifacts=ArtifactStore(settings.paths, db),
        registry=registry,
    )

    for skill_name, payload in ENGINE_SMOKE_INPUTS.items():
        entry = registry.get(skill_name)
        assert entry is not None
        assert entry.engine is not None
        assert entry.engine.module == "engine"
        result = await execute_skill(entry, payload, ctx)

        assert isinstance(result, dict)
        assert result.get("status") in {"ok", "partial", "error"}
        if skill_name == "scientific-figure":
            assert result["status"] == "ok"
            assert result["artifacts"]
        else:
            assert result.get("summary") or result.get("error") or result.get("indexed") == 0
