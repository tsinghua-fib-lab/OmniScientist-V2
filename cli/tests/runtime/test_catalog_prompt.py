"""ReAct skill catalog: Codex-style name+description, no contracts."""

from __future__ import annotations

from types import SimpleNamespace

from omni.config import load_settings
from omni.skills_runtime.catalog_prompt import (
    DEFAULT_CATALOG_CHAR_BUDGET,
    PER_LINE_DESCRIPTION_CHARS,
    catalog_char_budget,
    render_react_skill_catalog,
    skill_routing_description,
)
from omni.skills_runtime.registry import SkillRegistry


def _entry(name: str, description: str = "", when_to_use: str = "") -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description, when_to_use=when_to_use)


def test_catalog_uses_description_then_when_to_use() -> None:
    assert skill_routing_description(_entry("a", description="Deck from a paper")) == "Deck from a paper"
    assert skill_routing_description(_entry("a", when_to_use="only when slides")) == "only when slides"


def test_catalog_line_is_capped() -> None:
    text = skill_routing_description(_entry("a", description="x" * 4000))
    assert len(text) == PER_LINE_DESCRIPTION_CHARS
    assert text.endswith("...")


def test_catalog_budget_is_two_percent_or_default() -> None:
    assert catalog_char_budget() == DEFAULT_CATALOG_CHAR_BUDGET
    assert catalog_char_budget(context_window_tokens=200_000) == 16_000


def test_catalog_omits_schema_and_can_omit_skills() -> None:
    entries = [
        _entry(f"skill-{index}", description="does a thing", when_to_use="never dump schema")
        for index in range(40)
    ]
    text = render_react_skill_catalog(entries, context_window_tokens=200)
    assert '"properties"' not in text
    assert "Skill contract catalog" not in text
    assert "run_skill" in text
    assert "find_skill" in text
    assert "omitted from this bounded skills list" in text


def test_built_registry_lists_figure_and_pptx() -> None:
    registry = SkillRegistry(load_settings())
    registry.build_index()
    text = registry.react_skill_catalog()
    assert "- scientific-figure:" in text
    assert "- research-pptx:" in text
    assert '"properties"' not in text
    assert "capabilities:" not in text
