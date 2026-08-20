"""find_skill ranking: capability overlap, not all-words AND."""

from __future__ import annotations

from types import SimpleNamespace

from omni.agent.skill_lookup import rank_skill_matches
from omni.config import load_settings
from omni.skills_runtime.registry import SkillRegistry


def _entry(
    name: str,
    *,
    description: str = "",
    when_to_use: str = "",
    capabilities: list[str] | None = None,
    deliverables: list[str] | None = None,
    default_for: list[str] | None = None,
    priority: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=description,
        when_to_use=when_to_use,
        capabilities=capabilities or [],
        deliverables=deliverables or [],
        default_for=default_for or [],
        priority=priority,
        trigger={},
    )


def test_long_figure_query_hits_figure_skills() -> None:
    hits = rank_skill_matches(
        [
            _entry(
                "scientific-figure",
                capabilities=["artifact.figure", "figure.architecture"],
                default_for=["scientific figure"],
                priority=80,
            ),
            _entry(
                "livefigure",
                capabilities=["figure.livefigure"],
                default_for=["architecture diagram"],
                priority=110,
            ),
            _entry("research-pptx", capabilities=["slides.generate"], priority=100),
        ],
        "scientific figure generation architecture diagram",
    )
    names = [entry.name for entry in hits]
    assert "scientific-figure" in names or "livefigure" in names
    assert names[0] in {"scientific-figure", "livefigure"}


def test_long_pptx_query_hits_research_pptx() -> None:
    hits = rank_skill_matches(
        [
            _entry("livefigure", capabilities=["figure.livefigure"], priority=110),
            _entry(
                "research-pptx",
                capabilities=["slides.generate", "artifact.slides"],
                default_for=["generate slides"],
                priority=100,
            ),
        ],
        "research pptx slides presentation generation",
    )
    assert hits[0].name == "research-pptx"


def test_exact_name_still_wins() -> None:
    hits = rank_skill_matches(
        [
            _entry("livefigure", when_to_use="Do not use for decks (research-pptx).", priority=110),
            _entry("research-pptx", description="Generate a complete scientific presentation", priority=100),
        ],
        "research-pptx",
    )
    assert hits[0].name == "research-pptx"


def test_built_registry_ranks_figure_and_pptx_queries() -> None:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    selectable = registry.list_selectable()
    figure = rank_skill_matches(selectable, "scientific figure generation architecture diagram")
    assert figure
    assert figure[0].name in {"scientific-figure", "livefigure"}
    slides = rank_skill_matches(selectable, "research pptx slides presentation generation")
    assert slides
    assert slides[0].name == "research-pptx"
