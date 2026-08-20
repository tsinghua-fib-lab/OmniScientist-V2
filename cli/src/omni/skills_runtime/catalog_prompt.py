"""ReAct-visible skill index: names and routing descriptions only.

Codex lists ``- name: description`` under a catalog-level token budget
(2% of the context window, else 8_000 characters) and a 1_024-character
per-line cap. DeepSeek-Harness publishes the same name+description pair
(default 500 chars) and tells the model to load every applicable skill.
Omni's engines are invoked with ``run_skill``, so the on-demand body is
``find_skill`` / a contract error — not SKILL.md in the coordinator loop.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

PER_LINE_DESCRIPTION_CHARS = 1024
DEFAULT_CATALOG_CHAR_BUDGET = 8_000
CATALOG_CONTEXT_WINDOW_PERCENT = 2
_APPROX_CHARS_PER_TOKEN = 4

_HEADER = (
    "Available skills (name + description). If the user names a skill or the "
    "task matches a description, call run_skill with that exact name. "
    "find_skill only if you need that skill's input_schema."
)
_FOOTER = (
    "Built-in research tools (cite_source, record_claim, add_evidence, "
    "search_literature, search_corpus) are already in this turn's tool list — "
    "do not find_skill for them."
)


def catalog_char_budget(*, context_window_tokens: int = 0) -> int:
    """Codex-style catalog budget: 2% of the context window, else 8_000 chars."""
    window = max(0, int(context_window_tokens or 0))
    if window > 0:
        tokens = (window * CATALOG_CONTEXT_WINDOW_PERCENT) // 100
        return max(1, tokens * _APPROX_CHARS_PER_TOKEN)
    return DEFAULT_CATALOG_CHAR_BUDGET


def skill_routing_description(entry: Any, *, limit: int = PER_LINE_DESCRIPTION_CHARS) -> str:
    """Prefer description; fall back to the full when_to_use field."""
    text = " ".join(str(getattr(entry, "description", "") or "").split())
    if not text:
        text = " ".join(str(getattr(entry, "when_to_use", "") or "").split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    suffix = "..."
    keep = max(1, limit - len(suffix))
    return text[:keep] + suffix


def render_react_skill_catalog(
    entries: Iterable[Any],
    *,
    context_window_tokens: int = 0,
) -> str:
    """Render a bounded name+description list with no input_schema."""
    rows: list[tuple[str, str]] = []
    for entry in entries:
        name = str(getattr(entry, "name", "") or "").strip()
        if not name:
            continue
        rows.append((name, skill_routing_description(entry)))
    if not rows:
        return "\n".join([_HEADER, _FOOTER])

    budget = catalog_char_budget(context_window_tokens=context_window_tokens)
    overhead = len(_HEADER) + len(_FOOTER) + 2
    lines, omitted = _fit_lines(rows, budget=max(0, budget - overhead))
    blocks = [_HEADER, *lines]
    if omitted:
        word = "skill" if omitted == 1 else "skills"
        blocks.append(f"- {omitted} additional {word} omitted from this bounded skills list.")
    blocks.append(_FOOTER)
    return "\n".join(blocks)


def _line_cost(lines: list[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def _fit_lines(rows: list[tuple[str, str]], *, budget: int) -> tuple[list[str], int]:
    descriptions = [desc for _name, desc in rows]

    def render(descs: list[str]) -> list[str]:
        return [_render_line(name, desc) for (name, _full), desc in zip(rows, descs, strict=True)]

    lines = render(descriptions)
    if _line_cost(lines) <= budget:
        return lines, 0

    while any(descriptions):
        for index, desc in enumerate(descriptions):
            if desc:
                descriptions[index] = desc[:-1]
        lines = render(descriptions)
        if _line_cost(lines) <= budget:
            return lines, 0

    names_only = [_render_line(name, "") for name, _desc in rows]
    if _line_cost(names_only) <= budget:
        return names_only, 0

    kept: list[str] = []
    for line in names_only:
        if _line_cost([*kept, line]) > budget:
            return kept, len(rows) - len(kept)
        kept.append(line)
    return kept, 0


def _render_line(name: str, description: str) -> str:
    if description:
        return f"- {name}: {description}"
    return f"- {name}"


__all__ = [
    "DEFAULT_CATALOG_CHAR_BUDGET",
    "PER_LINE_DESCRIPTION_CHARS",
    "catalog_char_budget",
    "render_react_skill_catalog",
    "skill_routing_description",
]
