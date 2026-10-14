"""Visual observations for source figures selected by the content planner."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from . import vlm_client

_TEXT_FIELDS = ("structure", "embedded_text", "legibility_pressure")
_WIDTH_NEEDS = frozenset({"column", "wide", "dominant"})


async def analyze_figures(
    selected: list[str],
    assets: list[dict[str, Any]],
    *,
    client: Any | None,
    deadline: float | None = None,
) -> tuple[dict[str, dict[str, str]], str | None]:
    """Describe selected figures' visual complexity without choosing evidence."""

    if not selected or client is None:
        return {}, None

    assets_by_hash = {
        str(asset.get("content_sha256") or ""): asset
        for asset in assets
        if str(asset.get("content_sha256") or "") in selected
    }
    ordered_hashes = [digest for digest in selected if digest in assets_by_hash]
    images = tuple(
        _vlm_image(index, assets_by_hash[digest])
        for index, digest in enumerate(ordered_hashes, start=1)
    )
    if not images:
        return {}, None

    try:
        request_images = (
            (vlm_client.labeled_montage(
                images,
                label="SELECTED SOURCE FIGURES — panes F1 through "
                f"F{len(images)}",
            ),)
            if len(images) > 1
            else images
        )
        raw = await _generate(
            client,
            _prompt(ordered_hashes),
            images=request_images,
            deadline=deadline,
        )
        return _parse_response(raw, ordered_hashes), None
    except (TimeoutError, TypeError, ValueError, vlm_client.VlmError) as exc:
        return {}, (
            "Selected source-figure visual analysis was unavailable "
            f"({type(exc).__name__}: {exc})."
        )


def selected_figure_sha256s(budget: Mapping[str, Any]) -> list[str]:
    """Return selected figure hashes once, in content scan order."""

    selected: list[str] = []
    modules = budget.get("content_modules")
    for module in modules if isinstance(modules, list) else []:
        if not isinstance(module, Mapping):
            continue
        hashes = module.get("figure_sha256s")
        for value in hashes if isinstance(hashes, list) else []:
            digest = str(value or "").strip().lower()
            if digest and digest not in selected:
                selected.append(digest)
    return selected


def normalize_observations(
    value: Any,
    *,
    selected_sha256s: set[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Normalize checkpointed observations and discard entries no longer selected."""

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
            field: _observation_text(raw_observation.get(field), label=field)
            for field in _TEXT_FIELDS
        }
        width_need = str(raw_observation.get("width_need") or "column").strip()
        fields["width_need"] = (
            width_need if width_need in _WIDTH_NEEDS else "column"
        )
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


def _prompt(ordered_hashes: list[str]) -> str:
    figures = [
        {
            "id": f"F{index}",
            "sha256": digest,
        }
        for index, digest in enumerate(ordered_hashes, start=1)
    ]
    return (
        "Inspect only the attached source figures' internal visual complexity. "
        "The paper editor has already selected them for scientific reasons. Do not rank "
        "importance, choose content, interpret results, verify claims, or recommend another "
        "figure. Describe what remains visually demanding when each intact image is reduced "
        "for a printed academic poster. Multiple figures appear as panes labeled F1, F2, "
        "and so on in one attached montage.\n\n"
        "Return exactly one JSON object with this shape: "
        '{"figures":[{"id":"F1","structure":"...",'
        '"embedded_text":"...","legibility_pressure":"...",'
        '"width_need":"column|wide|dominant"}]}. '
        "Return one entry for every supplied id in the same order. In structure, state the "
        "visible panel count and arrangement or that it is a single view. In embedded_text, "
        "describe the visible density of axes, ticks, legends, labels, annotations, or prose. "
        "Do not transcribe their wording, values, or scientific conclusions. "
        "In legibility_pressure, state which reading keys or comparisons would disappear "
        "first when reduced and whether the intact figure needs a large continuous region. "
        "Keep each description to one or two sentences. "
        "Set width_need to column for a simple figure that remains interpretable in a "
        "normal poster column, wide when it needs one generous continuous column, or "
        "dominant only when essential panels or reading keys require most of the "
        "printable page width. Multiple panels alone do not make a figure dominant. "
        "Use observations rather than numeric scores or scientific conclusions.\n\n"
        "Figure bindings:\n"
        + json.dumps(figures, ensure_ascii=False, sort_keys=True)
    )


def _parse_response(
    raw: Any,
    ordered_hashes: list[str],
) -> dict[str, dict[str, str]]:
    if not isinstance(raw, str):
        raise TypeError("source-figure analysis must be returned as JSON text")
    parsed = vlm_client.parse_json_object(raw)
    if set(parsed) != {"figures"} or not isinstance(parsed["figures"], list):
        raise ValueError("source-figure analysis must contain only a figures array")
    figures = parsed["figures"]
    if len(figures) != len(ordered_hashes):
        raise ValueError("source-figure analysis must describe every selected figure")
    observations: dict[str, dict[str, str]] = {}
    for index, (digest, raw_figure) in enumerate(
        zip(ordered_hashes, figures, strict=True),
        start=1,
    ):
        if not isinstance(raw_figure, Mapping):
            raise TypeError("each source-figure observation must be an object")
        expected_fields = {"id", "width_need", *_TEXT_FIELDS}
        if set(raw_figure) != expected_fields or raw_figure.get("id") != f"F{index}":
            raise ValueError("source-figure observation ids or fields are invalid")
        width_need = str(raw_figure.get("width_need") or "").strip()
        if width_need not in _WIDTH_NEEDS:
            raise ValueError("source-figure width_need is invalid")
        observations[digest] = {
            field: _observation_text(raw_figure.get(field), label=field)
            for field in _TEXT_FIELDS
        }
        observations[digest]["width_need"] = width_need
    return observations


def _observation_text(value: Any, *, label: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError(f"source-figure {label} must not be empty")
    return text


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
    "selected_figure_sha256s",
]
