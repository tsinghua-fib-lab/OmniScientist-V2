"""Graphviz DOT revision adapter."""

from __future__ import annotations

import re

from omni.runtime.artifact_intents import (
    ArtifactElement,
    ArtifactIntent,
    normalize_text,
    resolve_named_element,
)

_PALETTES = {
    "blue": {"cluster": "#2563eb", "cluster_bg": "#eff6ff", "node_fill": "#dbeafe", "node_border": "#1d4ed8", "edge": "#2563eb"},
    "cyan": {"cluster": "#0891b2", "cluster_bg": "#ecfeff", "node_fill": "#cffafe", "node_border": "#0e7490", "edge": "#0891b2"},
    "teal": {"cluster": "#0f766e", "cluster_bg": "#ecfdf5", "node_fill": "#ccfbf1", "node_border": "#0d9488", "edge": "#0f766e"},
    "green": {"cluster": "#16a34a", "cluster_bg": "#f0fdf4", "node_fill": "#dcfce7", "node_border": "#15803d", "edge": "#16a34a"},
    "purple": {"cluster": "#7c3aed", "cluster_bg": "#f5f3ff", "node_fill": "#ede9fe", "node_border": "#6d28d9", "edge": "#7c3aed"},
    "pink": {"cluster": "#db2777", "cluster_bg": "#fdf2f8", "node_fill": "#fce7f3", "node_border": "#be185d", "edge": "#db2777"},
    "orange": {"cluster": "#ea580c", "cluster_bg": "#fff7ed", "node_fill": "#ffedd5", "node_border": "#c2410c", "edge": "#ea580c"},
    "neutral": {"cluster": "#475569", "cluster_bg": "#f8fafc", "node_fill": "#e2e8f0", "node_border": "#334155", "edge": "#64748b"},
    "calm": {"cluster": "#0f766e", "cluster_bg": "#ecfeff", "node_fill": "#ccfbf1", "node_border": "#0d9488", "edge": "#0891b2"},
}


def extract_graphviz_elements(dot: str) -> list[ArtifactElement]:
    lines = dot.splitlines()
    elements: list[ArtifactElement] = []
    for start, end in _subgraph_ranges(lines):
        body = "\n".join(lines[start:end + 1])
        identifier = _subgraph_id(lines[start])
        label = _label(body)
        elements.append(ArtifactElement(id=identifier, label=label, kind="subgraph", start=start, end=end))
    for idx, line in enumerate(lines):
        # A node declaration has an attribute list. Bare ``label=`` lines are
        # graph/subgraph attributes and would duplicate the enclosing element.
        if "label=" not in line or "subgraph" in line or "[" not in line:
            continue
        identifier = line.split("[", 1)[0].strip().strip(";")
        if not identifier:
            continue
        elements.append(ArtifactElement(id=identifier, label=_label(line), kind="node", start=idx, end=idx))
    return elements


def patch_graphviz_style(dot: str, intent: ArtifactIntent) -> tuple[str, list[str]]:
    lines = dot.splitlines()
    elements = extract_graphviz_elements(dot)
    target = intent.matched_element
    if target is None and intent.target:
        target = resolve_named_element(normalize_text(intent.target), elements)
    if target is None:
        return dot, []

    palette = _palette(intent.style)
    changed: list[str] = []
    if target.kind == "subgraph":
        _patch_range(lines, target.start, target.end, palette)
        changed.append(f"updated colors for {target.label or target.id}")
    elif target.kind == "node":
        lines[target.start] = _patch_node_line(lines[target.start], palette)
        changed.append(f"updated node colors for {target.label or target.id}")

    if _patch_matching_legend(lines, target, palette):
        changed.append("synchronized legend colors")
    if not changed:
        return dot, []
    return "\n".join(lines) + ("\n" if dot.endswith("\n") else ""), changed


def patch_graphviz_color(dot: str, edit_spec: dict[str, str]) -> tuple[str, list[str]]:
    """Ground a structured edit contract and apply it to Graphviz source."""
    from omni.runtime.artifact_intents import artifact_intent_from_spec

    elements = extract_graphviz_elements(dot)
    intent = artifact_intent_from_spec(edit_spec, elements=elements)
    if intent is None:
        return dot, []
    return patch_graphviz_style(dot, intent)


def _palette(style: str) -> dict[str, str]:
    if style in _PALETTES:
        return _PALETTES[style]
    if re.fullmatch(r"#[0-9a-f]{6}", style or "", re.IGNORECASE):
        return {
            "cluster": style,
            "cluster_bg": "#ffffff",
            "node_fill": "#ffffff",
            "node_border": style,
            "edge": style,
        }
    return _PALETTES["neutral"]


def _subgraph_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for idx, line in enumerate(lines):
        if "subgraph" not in line:
            continue
        depth = line.count("{") - line.count("}")
        if depth <= 0:
            continue
        end = idx
        for end in range(idx + 1, len(lines)):
            depth += lines[end].count("{") - lines[end].count("}")
            if depth <= 0:
                break
        ranges.append((idx, end))
    return ranges


def _subgraph_id(line: str) -> str:
    m = re.search(r"\bsubgraph\s+([A-Za-z0-9_:-]+)", line)
    return m.group(1) if m else "subgraph"


def _label(text: str) -> str:
    m = re.search(r'label\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def _patch_range(lines: list[str], start: int, end: int, palette: dict[str, str]) -> None:
    for i in range(start, end + 1):
        line = lines[i]
        stripped = line.strip()
        if re.match(r"color\s*=", stripped):
            lines[i] = _replace_attr(line, "color", palette["cluster"])
        elif re.match(r"bgcolor\s*=", stripped):
            lines[i] = _replace_attr(line, "bgcolor", palette["cluster_bg"])
        elif "node [" in line:
            lines[i] = _patch_node_line(line, palette)
        elif "edge [" in line:
            lines[i] = _replace_or_add_attr(line, "color", palette["edge"])


def _patch_node_line(line: str, palette: dict[str, str]) -> str:
    line = _replace_or_add_attr(line, "fillcolor", palette["node_fill"])
    return _replace_or_add_attr(line, "color", palette["node_border"])


def _patch_matching_legend(lines: list[str], target: ArtifactElement, palette: dict[str, str]) -> bool:
    target_tokens = set(normalize_text(target.search_text).split())
    changed = False
    for i, line in enumerate(lines):
        if "label=" not in line:
            continue
        line_tokens = set(normalize_text(line).split())
        if not target_tokens & line_tokens:
            continue
        new_line = _patch_node_line(line, palette)
        if new_line != line:
            lines[i] = new_line
            changed = True
    return changed


def _replace_attr(line: str, attr: str, value: str) -> str:
    return re.sub(rf'(?<![A-Za-z]){re.escape(attr)}\s*=\s*"[^"]*"', f'{attr}="{value}"', line)


def _replace_or_add_attr(line: str, attr: str, value: str) -> str:
    replaced = _replace_attr(line, attr, value)
    if replaced != line:
        return replaced
    if "]" in line:
        return line.replace("]", f', {attr}="{value}"]', 1)
    return line
