"""Prompt assembly adapted from the legacy scientific_figure skill.

The legacy prompt constants are retained verbatim in ``legacy_prompts.py``.
This module supplies the current runtime's output contract and only omits
legacy services that do not exist in the portable LiveFigure runtime.
"""

from __future__ import annotations

from .legacy_prompts import PPTX_BEST_PRACTICES, TOOLS_SPECIFICATION


def build_generation_prompt(requirement: str, title: str) -> str:
    """Build the legacy-equivalent code-generation prompt for one editable slide."""
    return f"""
You are an expert Python developer specialized in `python-pptx`.

Task: Write a COMPLETE, STANDALONE Python script to create an editable scientific diagram.

Context Requirements:
1. **Objective**: Create a scientific diagram based on the user's request: "{requirement}".
2. **Figure Title**: Use "{title}" when a visible title is appropriate.
3. **Layout Reference**: When an image is attached, mimic its structure, shapes, arrows, and text.
4. **Text Guidelines**:
   - Always use black or dark-gray text.
   - Center-align text inside shapes or text boxes unless a label needs another alignment.
   - Keep text legible and proportionate to the containing shape.
5. **Coordinates**: Carefully arrange every component and label; alignment determines the figure quality.

Technical Specifications:
1. **Output**: You MUST save the presentation exactly as "livefigure.pptx".
2. **Imports**: Include every necessary import.
3. **Available helper**: A local ``tools.py`` file is present beside your generated script. Use
   ``from tools import *`` and its documented helpers whenever they fit the diagram.

{PPTX_BEST_PRACTICES}

{TOOLS_SPECIFICATION}

!!! IMPORTANT OUTPUT FORMAT !!!
1. Return RAW Python code only.
2. Do NOT use Markdown code blocks.
3. Do NOT write an introduction, explanation, or summary.
4. Start directly with imports and end with the save command.
""".strip()


def build_repair_prompt(requirement: str, title: str, code: str, error: str) -> str:
    """Build the legacy-equivalent error-driven repair prompt."""
    return f"""
You are an expert Python developer and debugger for the `python-pptx` library.

The following script for "{requirement}" (title: "{title}") failed to execute.

--------------------------------------------------
[Error Log]
{error[:3000]}
--------------------------------------------------

[Broken Code]
{code[:30000]}
--------------------------------------------------

Task:
1. Analyze the Error Log to identify the syntax or logical issue.
2. Fix the code to resolve the error.
3. Preserve working layout and helper usage; make surgical changes where possible.
4. Ensure the code saves the output exactly as "livefigure.pptx".
5. Return the COMPLETE, FIXED Python script.
6. A local ``tools.py`` file is available beside the script; use its documented helpers as needed.

{PPTX_BEST_PRACTICES}

{TOOLS_SPECIFICATION}

!!! IMPORTANT OUTPUT FORMAT !!!
1. Return RAW Python code only.
2. Do NOT use Markdown code blocks.
3. Do NOT explain the fix.
4. Start directly with imports and end with the save command.
""".strip()
