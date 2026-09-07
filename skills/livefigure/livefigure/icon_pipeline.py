"""Legacy LiveFigure complex-icon asset pipeline.

This module ports the pre-code stages from the original scientific_figure
workflow: identify meaningful visual icons in the reference, describe them,
generate one sprite sheet, then split, de-white, and tightly crop each asset.
It intentionally stops before the old actor/critic feedback loop.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image


class IconVlm(Protocol):
    """The small VLM surface used by the legacy icon preparation stages."""

    async def generate_text(
        self, prompt: str, *, reference_image_uri: str | None = None
    ) -> str: ...

    async def generate_image(
        self,
        prompt: str,
        *,
        size: str = "1536x1024",
        aspect_ratio: str = "16:9",
        image_size: str = "",
    ) -> bytes: ...


@dataclass(frozen=True)
class IconAssetResult:
    """The generated sprite sheet and individually processed assets."""

    asset_map: dict[str, Path]
    sheet_path: Path | None = None


# Prompt text below is retained from the old scientific_figure Coder and
# BatchIconFactory.  It is deliberately not condensed or rewritten.
_ICON_PLANNER_PROMPT = """
        Analyze the scientific diagram provided.
        Your task is to identify **Meaningful Visual Icons** that should be extracted as separate assets.

        ### What counts as a "Complex Icon"? (Broad Criteria)
        1. **Symbolic Objects**: Any graphic representing a concept (e.g., "Server", "Database", "Document", "User", "Brain", "Globe", "Lock").
        2. **Composite Shapes**: Even if it looks geometric, if it represents a specific entity (e.g., a cylinder representing a "Database", a page icon representing a "File"), include it.
        3. **Illustrations**: Anything that isn't a simple connecting line or a layout frame.

        ### What to EXCLUDE (Strict):
        - Pure layout containers (empty rectangles holding other content).
        - Simple arrows connecting boxes.
        - Text labels themselves (but include the icon *next* to the text).

        ### Output Format (CRITICAL):
        - Return ONLY a raw JSON list of strings.
        - Do not use Markdown code blocks.
        - Do not explain.
        - Example: ["Database Cylinder", "User Avatar", "AI Brain Icon"]

        If no icons are found, return [].
        """


def _description_prompt(icon_names: list[str]) -> str:
    request = json.dumps(icon_names, ensure_ascii=False)
    return f"""
        Look at the image. Extract visual descriptions for these specific icons:
        {request}

        Task:
        1. Locate each icon in the image.
        2. Describe its visual appearance (color, shape, style) in detail so a painter can recreate it.
        3. Return a JSON object: {{ "Icon Name": "visual_description", ... }}
        """


def _layout(count: int) -> tuple[int, int, str]:
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    if count == 8:
        rows, cols = 2, 4
    if count == 12:
        rows, cols = 3, 4
    target_ratio = cols / rows
    supported_ratios = {"1:1": 1.0, "4:3": 1.333, "3:4": 0.75, "16:9": 1.778, "9:16": 0.562}
    aspect_ratio = min(supported_ratios, key=lambda key: abs(target_ratio - supported_ratios[key]))
    return rows, cols, aspect_ratio


def _sprite_sheet_prompt(descriptions: dict[str, str]) -> str:
    count = len(descriptions)
    rows, cols, aspect_ratio = _layout(count)
    total_slots = rows * cols
    empty_slots = total_slots - count
    grid_descriptions = "".join(
        f"Slot {index} (Target: {name}): {description}\n"
        for index, (name, description) in enumerate(descriptions.items(), start=1)
    )
    return f"""
        Generate a high-resolution Sprite Sheet Image containing exactly {count} distinct icons.

        Layout Configuration:
        - CANVAS: Aspect Ratio {aspect_ratio}.
        - GRID: {rows} Rows x {cols} Columns.
        - TOTAL SLOTS: {total_slots}.
        - FILLED: Slots 1 to {count} (Row by row, Left to Right).
        - EMPTY: Leave the last {empty_slots} slots strictly EMPTY/WHITE.

        Background: Pure White (#FFFFFF).
        Spacing: Wide white gaps between every icon.

        ----------
        🛡️ SEGMENTATION REQUIREMENTS (CRITICAL):
        1. **CLEAR BOUNDARIES**: Every icon MUST have a distinct, continuous edge or outline (e.g., a thin dark grey or black stroke).
        2. **NO FADING**: Do not let icon colors fade or gradient into the white background. The edge must be sharp.
        3. **COMPOUND ICONS**: If a single icon is described as having multiple parts (e.g., "a palette AND a printer"), these parts MUST be visually touching, overlapping, or connected by a base. Do NOT leave a complete white gap that separates the parts of a single icon.
        ----------

        🚫 NEGATIVE CONSTRAINTS:
        1. NO TEXT, LABELS, or NUMBERS.
        2. NO BOXES or FRAMES around the grid cells.
        3. NO SHADOWS that extend far from the icon.

        Items to draw:
        {grid_descriptions}

        Style: Flat vector icon, clean lines, professional scientific style, consistent palette.
        """


async def prepare_icon_assets(
    vlm: IconVlm,
    *,
    reference_image_uri: str,
    output_dir: Path,
) -> IconAssetResult:
    """Run the old icon planning and sprite-sheet processing stages.

    Planning/description failures intentionally degrade to no icon assets, as
    the old workflow did, so code generation can still create a diagram.
    """
    icon_names = _parse_icon_names(
        await vlm.generate_text(_ICON_PLANNER_PROMPT, reference_image_uri=reference_image_uri)
    )
    if not icon_names:
        return IconAssetResult({})
    descriptions = _parse_descriptions(
        await vlm.generate_text(
            _description_prompt(icon_names), reference_image_uri=reference_image_uri
        ),
        icon_names,
    )
    if not descriptions:
        return IconAssetResult({})
    _, _, aspect_ratio = _layout(len(descriptions))
    sheet = await vlm.generate_image(
        _sprite_sheet_prompt(descriptions),
        aspect_ratio=aspect_ratio,
        image_size="4K",
    )
    sheet_path = output_dir / f"assets_grid_sheet_raw{_image_suffix(sheet)}"
    sheet_path.write_bytes(sheet)
    return IconAssetResult(
        _slice_and_process(sheet_path, list(descriptions), output_dir), sheet_path
    )


def _image_suffix(raw: bytes) -> str:
    """Choose a truthful suffix when an image provider changes encodings."""
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def _parse_icon_names(response: str) -> list[str]:
    match = re.search(r"\[.*\]", str(response or ""), re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


def _parse_descriptions(response: str, icon_names: list[str]) -> dict[str, str]:
    text = str(response or "").replace("```json", "").replace("```", "").strip()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        name: str(raw[name]).strip()
        for name in icon_names
        if isinstance(raw.get(name), str) and str(raw[name]).strip()
    }


def _slice_and_process(sheet_path: Path, icon_names: list[str], output_dir: Path) -> dict[str, Path]:
    """Port the old OpenCV split/de-white/tight-crop behavior using Pillow."""
    try:
        sheet = Image.open(sheet_path).convert("RGBA")
    except OSError:
        return {}
    rows, cols, _ = _layout(len(icon_names))
    cell_width, cell_height = sheet.width // cols, sheet.height // rows
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    assets: dict[str, Path] = {}
    for index, name in enumerate(icon_names):
        row, col = divmod(index, cols)
        left, top = col * cell_width, row * cell_height
        right, bottom = min(left + cell_width, sheet.width), min(top + cell_height, sheet.height)
        margin = 5
        if right - left <= margin * 2 or bottom - top <= margin * 2:
            continue
        cell = sheet.crop((left + margin, top + margin, right - margin, bottom - margin))
        processed = _dewhite_and_trim(cell)
        safe_name = "".join(character if character.isalnum() else "_" for character in name)[:20]
        path = assets_dir / f"icon_{index}_{safe_name}.png"
        processed.save(path, format="PNG")
        assets[name] = path
    return assets


def _dewhite_and_trim(image: Image.Image) -> Image.Image:
    """Match the old threshold-240 transparency and five-pixel crop padding."""
    rgba = image.convert("RGBA")
    pixels = [
        (red, green, blue, 0 if (red * 299 + green * 587 + blue * 114) // 1000 > 240 else alpha)
        for red, green, blue, alpha in rgba.getdata()
    ]
    rgba.putdata(pixels)
    bbox = rgba.getbbox()
    if bbox is None:
        return rgba
    left, top, right, bottom = bbox
    return rgba.crop((max(0, left - 5), max(0, top - 5), min(rgba.width, right + 5), min(rgba.height, bottom + 5)))
