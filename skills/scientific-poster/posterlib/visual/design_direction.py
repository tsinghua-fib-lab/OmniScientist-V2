"""Design requirements shared by composition and independent visual review."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .reference_analysis import normalize_preferences

GUIDANCE = """Design requirements govern the poster's visual treatment. Apply explicit user
direction and visual preferences first; later explicit changes take precedence over earlier
direction. A custom reference supplies the requested visual identity at the fidelity the
user specifies. Where those inputs leave a choice open, apply the defaults below.

Default to a white (#FFFFFF) canvas with transparent or white content regions.
Choose boundary strength locally from the information relationships. Open regions work
when proximity, alignment and type hierarchy make group membership clear. A light divider
can distinguish adjacent reader questions along a continuous reading path. A restrained
complete outline can gather several elements into one shared comparison, experiment or
process when surrounding content makes their membership ambiguous. Enclose the whole
related group, keeping its heading, evidence and interpretation together. Combine these
treatments within one poster and use the lightest treatment that makes the grouping clear.
The scientific module ids remain editing anchors; visible groups follow the argument.
Both open and enclosed groups can succeed. Judge whether unrelated material merges,
related material is fragmented, or boundary weight competes with the evidence. Describe
the affected reading relationship and let the author choose its treatment.

Keep color in scientific figures, meaningful data encoding and restrained heading emphasis.
Use borders and rules for information-bearing structures such as method nodes, connections,
tables and chart axes. Scientific node outlines, grouping enclosures and restrained semantic
fills can explain stages or categories within a diagram. White surfaces and local outlines
can establish a group independently of colored backgrounds.
Decorative single-side borders, accent rails, heading stripes, tinted section cards and
whole-page color washes fall outside the default treatment. Retain all source-image bytes,
colors, intrinsic margins and full extent; integrate each intact figure through placement
and surrounding white space.

Compose each heading, evidence carrier and interpretation as one content-sized group.
Let rendered copy, intact figure proportions and readable label scale determine its depth.
Keep tables and comparison rows at their natural cell height with consistent local padding;
wrapped labels expand the rows that contain them. Connect related items through explicit
local gaps, and place independent groups according to their actual depths. Give additional
continuous area to evidence that benefits from a larger readable scale. Keep clear separation
between different reader questions, with related content gathered together inside each group.
Apply these relationships within modules and across the fixed physical sheet. Assess the
distances between actual text, figures and row contents in the rendered image, alongside
their measured extents.

An automatically selected built-in reference (source_kind=seed) supplies reading hierarchy,
proximity and evidence scale. Its palette, canvas tint and decorative framing are observations,
not target styling requirements. A custom reference (source_kind=custom) and explicit brand
or color direction can override the defaults in their stated scope. A requested accent color
alone colors emphasis, not the entire page or every section."""


def context(
    *,
    reference_source_kind: str = "seed",
    user_direction: str = "",
    visual_preferences: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep explicit direction intact rather than infer preferences from keywords."""

    return {
        "reference_source_kind": reference_source_kind,
        "user_direction": user_direction,
        "visual_preferences": normalize_preferences(visual_preferences),
    }


def update_user_direction(current: str, *updates: str) -> str:
    """Retain explicit changes in order for authoring, checkpoints and review."""

    additions = list(dict.fromkeys(
        value.strip() for value in updates if value.strip() and value.strip() != current.strip()
    ))
    if not additions:
        return current
    return "\n\n".join(filter(None, (
        current, "Updated user direction:\n" + "\n\n".join(additions),
    )))
