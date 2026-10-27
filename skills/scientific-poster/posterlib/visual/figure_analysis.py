"""Visual observations for source figures selected by the content planner."""

from __future__ import annotations

import base64
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any

from . import vlm_client

_TEXT_FIELDS = ("structure", "embedded_text", "legibility_pressure")
_WIDTH_NEEDS = frozenset({"column", "wide", "dominant"})
# Two-by-two panes fit the existing bounded image transport while retaining labels.
_FIGURES_PER_BATCH = 4


async def analyze_figures(
    selected: list[str],
    assets: list[dict[str, Any]],
    *,
    client: Any | None,
    deadline: float | None = None,
) -> tuple[dict[str, dict[str, str]], str | None]:
    """Describe selected figures' readable labels, complexity and surfaces."""

    if not selected or client is None:
        return {}, None

    assets_by_hash = {
        str(asset.get("content_sha256") or ""): asset
        for asset in assets
        if str(asset.get("content_sha256") or "") in selected
    }
    ordered_hashes = [digest for digest in selected if digest in assets_by_hash]
    if not ordered_hashes:
        return {}, None

    observations: dict[str, dict[str, str]] = {}
    for start in range(0, len(ordered_hashes), _FIGURES_PER_BATCH):
        batch = ordered_hashes[start:start + _FIGURES_PER_BATCH]
        try:
            images = tuple(
                _vlm_image(index, assets_by_hash[digest])
                for index, digest in enumerate(batch, start=1)
            )
            request_images = (
                (vlm_client.labeled_montage(
                    images, label=f"SOURCE FIGURES — panes F1 through F{len(images)}",
                    columns=2,
                ),)
                if len(images) > 1 else images
            )
            raw = await _generate(
                client, _prompt(len(batch)), images=request_images, deadline=deadline,
            )
            observations.update(_parse_response(raw, batch))
        except (TimeoutError, TypeError, ValueError, vlm_client.VlmError) as exc:
            return observations, (
                "Source-figure visual analysis stopped; available observations were retained "
                f"({type(exc).__name__}: {exc})."
            )
    return observations, None


def normalize_observations(
    value: Any,
    *,
    selected_sha256s: set[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Normalize observed reading keys and optional advice for the selected figures."""

    if not isinstance(value, Mapping):
        return {}
    allowed = selected_sha256s
    normalized: dict[str, dict[str, str]] = {}
    for raw_digest, raw_observation in value.items():
        digest = str(raw_digest or "").strip().lower()
        if allowed is not None and digest not in allowed:
            continue
        if not isinstance(raw_observation, Mapping):
            continue
        fields = {
            field: " ".join(text.split())
            for field in (*_TEXT_FIELDS, "surface")
            if isinstance(text := raw_observation.get(field), str) and text.strip()
        }
        width_need = str(raw_observation.get("width_need") or "").strip()
        if width_need in _WIDTH_NEEDS:
            fields["width_need"] = width_need
        if fields:
            normalized[digest] = fields
    return normalized


async def _generate(
    client: Any,
    prompt: str,
    *,
    images: tuple[vlm_client.VlmImage, ...],
    deadline: float | None,
) -> str:
    """Use the shared planning deadline and visual transport retry policy."""

    return await vlm_client.generate_json_text_bounded(
        client,
        prompt,
        images=images,
        deadline=deadline,
        timeout_message="source-figure analysis budget was exhausted",
    )


def _prompt(figure_count: int) -> str:
    figure_ids = [f"F{index}" for index in range(1, figure_count + 1)]
    return (
        "Inspect the attached source figures' reading keys, visual complexity and outer surfaces. "
        "These are the editor's proposed carriers. Describe visible defects even when a "
        "figure has already been selected: the source-aware author can replace or drop it. "
        "Assess visible communication; scientific verification belongs to that author. "
        "Describe what remains visually demanding when each intact image is reduced "
        "for a printed academic poster. Multiple figures appear as panes labeled F1, F2, "
        "and so on in one attached montage.\n\n"
        "Return exactly one JSON object with this shape: "
        '{"figures":[{"id":"F1","structure":"...",'
        '"embedded_text":"...","legibility_pressure":"...","surface":"...",'
        '"width_need":"column|wide|dominant"}]}. '
        "Use the supplied ids and include observations you can make from the image. "
        "Leave unavailable observations absent. In structure, state the "
        "visible panel count and arrangement or that it is a single view. In embedded_text, "
        "record clearly legible panel titles, axis quantities and units, and legend identities "
        "needed to interpret the image, together with their visual density. Associate reading "
        "keys with their panels so the author can distinguish separate measurements from an "
        "explicitly plotted aggregate. Mark unreadable labels as unknown; keep point values "
        "and scientific conclusions for the source-aware author to verify. "
        "In legibility_pressure, state which reading keys or comparisons would disappear "
        "first when reduced and whether the intact figure needs a large continuous region. "
        "Include visibly confusing connections, missing reading keys or density that "
        "limits its usefulness. Distinguish observed defects from details too small to judge. "
        "In surface, describe the visible outer field, margins and edge treatment that the "
        "author must integrate with the surrounding poster. Distinguish visible appearance "
        "from unknown image transparency when the viewing background obscures it. Treat "
        "these as intact source-image properties; the author controls the surrounding "
        "surfaces and framing while preserving the image bytes. "
        "Keep each description to one or two sentences. "
        "When the image supports a width recommendation, set the optional width_need "
        "to column for a simple figure that remains interpretable in a "
        "normal poster column, wide when it needs one generous continuous column, or "
        "dominant only when essential panels or reading keys require most of the "
        "printable page width. Omit width_need when the recommendation is uncertain. "
        "Multiple panels alone do not make a figure dominant. "
        "Use observations rather than numeric scores or scientific conclusions.\n\n"
        "Figure ids in the attached image:\n"
        + json.dumps(figure_ids, ensure_ascii=False)
    )


def _parse_response(
    raw: Any,
    ordered_hashes: list[str],
) -> dict[str, dict[str, str]]:
    if not isinstance(raw, str):
        raise TypeError("source-figure analysis must be returned as JSON text")
    parsed = vlm_client.parse_json_object(raw)
    if not isinstance(parsed.get("figures"), list):
        raise TypeError("source-figure analysis requires a figures array")
    figures = parsed["figures"]
    hashes_by_id = {f"F{index}": digest for index, digest in enumerate(ordered_hashes, start=1)}
    identified = [
        item for item in figures
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    ]
    counts = Counter(item["id"] for item in identified)
    return normalize_observations({
        hashes_by_id[item["id"]]: item
        for item in identified
        if item["id"] in hashes_by_id and counts[item["id"]] == 1
    })


def _vlm_image(index: int, asset: Mapping[str, Any]) -> vlm_client.VlmImage:
    data_uri = str(asset.get("data_uri") or "")
    header, separator, payload = data_uri.partition(",")
    if not separator or not header.startswith("data:image/") or ";base64" not in header:
        raise ValueError("selected source figure has no prepared image bytes")
    try:
        image_bytes = base64.b64decode(payload, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("selected source figure has invalid prepared image bytes") from exc
    label = " ".join(
        str(asset.get("carrier_label") or "").split()
    )
    return vlm_client.VlmImage(
        label=f"F{index} — {label or 'selected source figure'}",
        image_bytes=image_bytes,
        mime_type=str(asset.get("mime") or ""),
    )


__all__ = [
    "analyze_figures",
    "normalize_observations",
]
