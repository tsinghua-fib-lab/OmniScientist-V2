"""Registry-driven domain packs stay additive and contract bounded."""

from omni.agent.model_planner import _planner_system_prompt
from omni.config import load_settings
from omni.research.domain_packs import DomainPackRegistry, load_domain_packs
from omni.research.registry import ConnectorRegistry
from omni.skills_runtime.registry import SkillRegistry


def test_bundled_domain_packs_have_specialists_connectors_and_artifacts():
    packs = load_domain_packs()

    assert {"core", "machine-learning", "life-sciences"} <= set(packs)
    assert "clinicaltrials" in packs["life-sciences"].connectors
    assert "PICO-table" in packs["life-sciences"].artifact_types
    assert {item.role for item in packs["machine-learning"].specialists} >= {
        "ml-method-reviewer", "ml-systems-engineer",
    }


def test_enabled_domain_packs_prioritise_only_available_connectors():
    settings = load_settings()
    settings.research.domain_packs = ["life-sciences", "missing"]
    settings.research.connectors = ["pubmed", "clinicaltrials", "openalex"]
    packs = DomainPackRegistry(settings)
    enabled = {item.name for item in ConnectorRegistry(settings).enabled()}

    assert [item.name for item in packs.enabled()] == ["life-sciences"]
    assert packs.recommended_connectors(available=enabled) == [
        "pubmed", "clinicaltrials", "openalex",
    ]


def test_domain_pack_guidance_is_visible_to_model_planner():
    settings = load_settings()
    settings.research.domain_packs = ["life-sciences"]
    skills = SkillRegistry(settings)
    skills.build_index()

    prompt = _planner_system_prompt(skills, settings=settings)

    assert "Life Sciences" in prompt
    assert "clinical-trial-analyst" in prompt
    assert "ClinicalTrials.gov" in prompt
