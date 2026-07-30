"""LLM-based content planning and slide content generation."""

from __future__ import annotations

import importlib.util as _ilu
import json
import logging
import re
import sys as _sys
from pathlib import Path
from pathlib import Path as _Path
from typing import Any

_spec = _ilu.spec_from_file_location(
    "research_pptx_models", _Path(__file__).resolve().parent / "models.py"
)
_models = _sys.modules.get("research_pptx_models")
if _models is None:
    _models = _ilu.module_from_spec(_spec)
    _sys.modules["research_pptx_models"] = _models
    _spec.loader.exec_module(_models)

ParsedContent = _models.ParsedContent
PresentationPlan = _models.PresentationPlan
PresentationRequest = _models.PresentationRequest
SlideData = _models.SlideData

logger = logging.getLogger(__name__)

# ── Load prompt templates ────────────────────────────────

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""

def extract_structured_payload(raw: str, task_type: str) -> dict | None:
    """Local replacement: pull a JSON envelope/payload out of an LLM reply."""
    import json
    if not raw:
        return None
    # try direct json
    for candidate in (raw, _strip_code_fence(raw)):
        try:
            obj = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            if "payload" in obj and isinstance(obj["payload"], dict):
                return obj["payload"]
            if "slides" in obj:
                return obj
    # try to locate the first {...} block
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj.get("payload", obj) if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _strip_code_fence(text: str) -> str:
    return re.sub(r"^```[a-zA-Z]*\n|\n```$", "", text.strip())


SCIENTIFIC_PRINCIPLES = _load_prompt("scientific_presentation.md")

# ── Slide count guidelines ───────────────────────────────

SLIDE_GUIDELINES: dict[str, dict[str, int]] = {
    "conference": {"5": 6, "10": 11, "15": 16, "20": 22},
    "seminar": {"30": 27, "45": 40, "60": 52},
    "group_meeting": {"15": 14, "30": 25, "45": 35, "60": 45},
    "defense": {"30": 30, "45": 40, "60": 52},
}

# ── Talk-type-specific style directives ──────────────────

TALK_TYPE_DIRECTIVES: dict[str, str] = {
    "conference": """
## TALK-TYPE: CONFERENCE

Audience: peers from the same field; limited attention window.
Style requirements:
- Tight narrative arc — ONE central message, ruthlessly focused
- Skip extensive background; assume field literacy
- Emphasize NOVELTY and KEY RESULT — 50-60% of slides on results
- Cut related work to 1 slide max
- No deep methodology; show architecture diagram only
- Conclusion: 3 bullets max, memorable takeaway
- Slide density: MEDIUM (3-4 bullets per content slide)
""",
    "seminar": """
## TALK-TYPE: SEMINAR

Audience: mixed — some outside the subfield. Goal: EDUCATE + INSPIRE.
Style requirements:
- Extended motivation and background (3-5 slides acceptable)
- Walk through methods carefully, include intuitions and diagrams
- Results section can have 8-15 slides with deep analysis
- Include 1-2 "aha moment" slides showing key insights
- Discussion section: prior work comparison + broader implications (2-3 slides)
- Slide density: MEDIUM-HIGH (4-6 bullets), include sub-points
- Add a "Future Directions" section before conclusion
""",
    "group_meeting": """
## TALK-TYPE: GROUP MEETING

Audience: labmates familiar with the general area. Goal: GET FEEDBACK.
Style requirements:
- Casual, working-draft tone OK
- Heavy emphasis on methods and intermediate results (40%+)
- Include FAILURE MODES and open questions explicitly
- Add a "Questions for discussion" or "Stuck on" slide
- Less polish on conclusion — more "what I tried" and "what's next"
- Results can be preliminary; label as such
- Slide density: HIGH (can be bullet-dense, raw data)
""",
    "defense": """
## TALK-TYPE: DEFENSE

Audience: committee — will probe deeply. Goal: DEMONSTRATE MASTERY.
Style requirements:
- FORMAL tone; full citations [Author, Year] on every claim
- Comprehensive background (3-5 slides) showing field mastery
- Detailed methodology (3-5 slides) — committee WILL ask how each step works
- EVERY result slide needs: claim, evidence (figure/table), quantitative delta, statistical significance
- Include EXPLICIT limitations slide with honest self-assessment
- "Contributions" slide listing 3-5 concrete contributions
- Comparison with SOTA is MANDATORY (dedicated table slide)
- Conclusion: revisit research questions posed at start, show each was answered
- Slide density: HIGH — committee reads slides, doesn't just listen
- Include one "broader impact" slide
""",
}

# ── Color themes ─────────────────────────────────────────

COLOR_THEMES: dict[str, dict[str, str]] = {
    "midnight_executive": {
        "primary": "1E2761",
        "secondary": "CADCFC",
        "accent": "FFFFFF",
        "dark": "0F1535",
        "bodyText": "2D2D2D",
        "muted": "6B7280",
        "tableFill": "F0F4FF",
        "tableHead": "1E2761",
    },
    "teal_trust": {
        "primary": "028090",
        "secondary": "00A896",
        "accent": "02C39A",
        "dark": "01293D",
        "bodyText": "2D2D2D",
        "muted": "6B7280",
        "tableFill": "E6F7F5",
        "tableHead": "028090",
    },
    "forest_moss": {
        "primary": "2C5F2D",
        "secondary": "97BC62",
        "accent": "F5F5F5",
        "dark": "1A3A1A",
        "bodyText": "2D2D2D",
        "muted": "6B7280",
        "tableFill": "F0F5E8",
        "tableHead": "2C5F2D",
    },
    "charcoal_minimal": {
        "primary": "36454F",
        "secondary": "F2F2F2",
        "accent": "212121",
        "dark": "1C2529",
        "bodyText": "2D2D2D",
        "muted": "6B7280",
        "tableFill": "F5F5F5",
        "tableHead": "36454F",
    },
}


def _get_target_slide_count(
    talk_type: str,
    duration: int,
    override: int | None = None,
) -> int:
    if override is not None:
        return max(4, min(60, override))
    guidelines = SLIDE_GUIDELINES.get(talk_type, SLIDE_GUIDELINES["conference"])
    durations = sorted(guidelines.keys(), key=int)
    nearest = min(durations, key=lambda d: abs(int(d) - duration))
    return guidelines[nearest]


# ── Section-aware content summarization ──────────────────

_SECTION_BUDGET_WEIGHTS: dict[str, float] = {
    "abstract": 1.0,
    "conclusion": 0.95,
    "results": 0.90,
    "discussion": 0.60,
    "introduction": 0.50,
    "background": 0.40,
    "methods": 0.35,
    "other": 0.20,
    "preamble": 0.10,
}


def _classify_section(heading: str) -> str:
    h = heading.lower().strip()
    if "abstract" in h:
        return "abstract"
    if "conclu" in h or h.startswith("summary"):
        return "conclusion"
    if any(kw in h for kw in ("result", "experiment", "evaluation", "finding")):
        return "results"
    if "discussion" in h:
        return "discussion"
    if any(kw in h for kw in ("introduc", "motivation", "overview")):
        return "introduction"
    if any(kw in h for kw in ("related", "background", "preliminary", "prior")):
        return "background"
    if any(kw in h for kw in ("method", "approach", "framework", "model", "architecture", "system")):
        return "methods"
    if any(kw in h for kw in ("reference", "bibliograph", "appendix", "acknowledge")):
        return "preamble"
    return "other"


def _build_content_summary(
    content: ParsedContent,
    max_chars: int = 28000,
) -> str:
    if not content.sections:
        text = content.markdown_text
        if len(text) <= max_chars:
            return text
        # Give 60% to head (intro/methods), 35% to tail (results/conclusion), 5% to separator
        head = int(max_chars * 0.60)
        tail = int(max_chars * 0.35)
        return text[:head] + "\n\n[... content truncated ...]\n\n" + text[-tail:]

    classified: list[tuple[str, str, str, float]] = []
    for heading, text in content.sections.items():
        category = _classify_section(heading)
        weight = _SECTION_BUDGET_WEIGHTS.get(category, 0.2)
        classified.append((heading, text.strip(), category, weight))

    total_weight = sum(w for _, _, _, w in classified) or 1.0
    classified.sort(key=lambda x: x[3], reverse=True)
    parts: list[str] = []
    remaining = max_chars

    for heading, text, _category, weight in classified:
        if remaining <= 100:
            break
        # Allocate budget proportionally, with a minimum of 400 chars
        # High-priority sections (results, abstract, conclusion) get at least 600
        min_budget = 600 if weight >= 0.9 else 400
        budget = max(min_budget, int(max_chars * weight / total_weight))
        budget = min(budget, remaining, len(text) + len(heading) + 10)

        header_line = f"### {heading}\n"
        text_budget = budget - len(header_line)
        if text_budget <= 0:
            continue

        if len(text) <= text_budget:
            truncated = text
        else:
            truncated = text[:text_budget - 15] + " [truncated]"

        parts.append(header_line + truncated)
        remaining -= len(header_line) + len(truncated) + 2

    section_order = list(content.sections.keys())
    order_map = {heading: i for i, heading in enumerate(section_order)}
    parts.sort(key=lambda p: order_map.get(p.split("\n")[0].lstrip("# ").strip(), 999))

    return "\n\n".join(parts)


# ── Figure description block for planning prompt ─────────

def _build_figure_block(
    figures: list[dict[str, str]],
    sections_by_page: dict[int, str] | None = None,
) -> str:
    if not figures:
        return ""

    # Budget: as the figure count grows, shrink per-figure context
    n = len(figures)
    if n <= 6:
        related_chars = 300
    elif n <= 12:
        related_chars = 200
    else:
        related_chars = 120

    strong_candidates = sum(
        1 for f in figures
        if f.get("caption") and f.get("related_text")
    )
    min_required = min(
        n, max(3, min(strong_candidates, n * 2 // 3)),
    )

    lines: list[str] = [
        f"\n[{n} FIGURES AVAILABLE — "
        f"you MUST use at least {min_required} of them]\n"
    ]

    # Iterate ALL figures (preserves figure_N → index mapping)
    for i, fig in enumerate(figures):
        caption = fig.get("caption", "")
        page_s = fig.get("page_num", "?")
        try:
            page = int(page_s)
        except (ValueError, TypeError):
            page = -1
        source = fig.get("source", "unknown")
        related = fig.get("related_text", "")
        elem_count = fig.get("element_count", "")

        section_tag = ""
        if sections_by_page and page >= 0:
            sec = sections_by_page.get(page, "")
            if sec:
                section_tag = f" [section: {sec}]"

        if source == "region_render":
            source_tag = "📊 region"
        elif source == "page_render":
            source_tag = "📐 page-render"
        else:
            source_tag = "📊 raster"

        composite_note = ""
        if elem_count and str(elem_count).isdigit() and int(elem_count) > 1:
            composite_note = f", {elem_count} sub-panels"

        desc = caption if caption else f"Figure from page {page_s}"
        # Bound caption length in listing
        if len(desc) > 250:
            desc = desc[:247] + "..."

        lines.append(
            f"  figure_{i} (page {page_s}{section_tag}, {source_tag}"
            f"{composite_note}): {desc}"
        )

        if related:
            if len(related) > related_chars:
                related = related[:related_chars] + "..."
            lines.append(f"    📝 Context: \"{related}\"")

    lines.append(
        "\n⚠️ FIGURE ASSIGNMENT RULES:\n"
        "  - Match each figure to the slide whose topic matches its section tag\n"
        "  - [section: methods] → methods/architecture slide; "
        "[section: results] → results slide\n"
        "  - Use 'full_figure' for composite architecture/framework diagrams\n"
        "  - Use 'content_figure' when bullets cite specific data from the figure\n"
        "  - NEVER use 'full_figure' for 📐 page-render (those are full pages)\n"
        "  - Do NOT duplicate a figure across slides\n"
        "  - Use the Context passage to write bullets with SPECIFIC numbers\n"
    )

    return "\n".join(lines)

def _build_table_block(tables: list[dict[str, Any]]) -> str:
    """Render structured tables for the LLM prompt."""
    if not tables:
        return ""

    n = len(tables)
    min_required = min(n, max(1, n // 2))

    lines: list[str] = [
        f"\n[{n} TABLES AVAILABLE — "
        f"you MUST use at least {min_required} as 'table' type slides]\n"
    ]

    for i, t in enumerate(tables):
        caption = t.get("caption", "") or "(no caption)"
        if len(caption) > 200:
            caption = caption[:197] + "..."
        page = t.get("page_num", "?")
        source = t.get("source", "unknown")
        truncated = " [truncated]" if t.get("truncated") else ""

        lines.append(
            f"  table_{i} (page {page}, {source}{truncated}): {caption}"
        )
        # Show actual table content (compact)
        headers = t.get("headers", [])
        rows = t.get("rows", [])
        if headers:
            lines.append(f"    Headers: {' | '.join(str(h)[:25] for h in headers)}")
        # Show up to 3 rows as preview
        for r_idx, row in enumerate(rows[:3]):
            row_str = " | ".join(str(c)[:25] for c in row)
            lines.append(f"    Row {r_idx + 1}: {row_str}")
        if len(rows) > 3:
            lines.append(f"    ... ({len(rows) - 3} more rows)")

    lines.append(
        "\n⚠️ TABLE USAGE RULES:\n"
        "  - When creating a 'table' slide, use table_N reference in extra.table_ref\n"
        "  - OR copy table_headers and table_rows directly from the data above\n"
        "  - Tables show comparison/quantitative data — use them in Results section\n"
        "  - Highlight the row representing YOUR method using highlight_row\n"
    )
    return "\n".join(lines)

# ── Planning prompt ──────────────────────────────────────


async def plan_presentation(
        llm_client: Any,
        content: ParsedContent,
        req: PresentationRequest,
        max_retries: int = 2,
) -> PresentationPlan:
    target_slides = _get_target_slide_count(
        req.talk_type, req.duration_minutes, override=req.target_slides,
    )
    colors = COLOR_THEMES.get(req.color_theme, COLOR_THEMES["midnight_executive"])

    user_instruction = getattr(req, "user_instruction", None) or ""
    if user_instruction.strip() == req.topic.strip():
        instruction_block = ""
    elif user_instruction.strip():
        instruction_block = f"""
    ## USER'S SPECIFIC INSTRUCTIONS (HIGH PRIORITY — follow these closely)
    {user_instruction}
    ---
    """
    else:
        instruction_block = ""

    caption_lang_rule = (
        "Write figure_caption in Chinese even if the source paper is English."
        if req.language == "zh"
        else "Write figure_caption in English."
    )

    raw_len = len(content.markdown_text)
    slide_budget = target_slides * 900
    content_budget = int(raw_len * 0.7)
    summary_budget = max(12000, min(40000, max(slide_budget, content_budget)))
    content_summary = _build_content_summary(content, max_chars=summary_budget)

    sections_by_page: dict[int, str] = {}
    _section_order = [
        "abstract", "introduction", "background", "methods",
        "results", "discussion", "conclusion", "references",
    ]
    _prescreen = content.metadata.get("section_pages", {}) if content.metadata else {}
    if _prescreen:
        total_pages = content.metadata.get("page_count", 0)
        for p in range(total_pages):
            current = ""
            best_p = -1
            for sec_name in _section_order:
                sp = _prescreen.get(sec_name)
                if sp is not None and sp <= p and sp > best_p:
                    best_p = sp
                    current = sec_name
            if current:
                sections_by_page[p] = current

    if content.figures:
        figure_block = _build_figure_block(content.figures, sections_by_page)
        content_summary += figure_block
    else:
        # No source figures (topic-only / outline-only / markdown-without-images):
        # allow the planner to *declare* that a slide SHOULD have a figure by
        # setting ``figure_path`` to the sentinel ``"__placeholder__"``. The
        # renderer will draw a neutral grey placeholder card so the audience
        # sees "a figure belongs here — swap in the real one later" instead
        # of a silently dropped layout.
        content_summary += (
            "\n[NO SOURCE FIGURES AVAILABLE]\n"
            "You MAY still emit up to 2 'content_figure' or 'full_figure' "
            "slides IF the topic genuinely calls for a diagram (architecture "
            "overview, pipeline, comparison chart). When you do, set "
            "figure_path=\"__placeholder__\" and write a concrete "
            "figure_caption describing what the figure SHOULD show, e.g. "
            "\"Architecture diagram: retriever → generator → answer\". Do NOT "
            "emit figure_N references — there are no real figures to bind.\n"
        )

    # structured table block
    table_block = _build_table_block(content.tables) if content.tables else ""
    content_summary += table_block

    if content.equations:
        content_summary += f"\n[{len(content.equations)} equations found]\n"

    # seed citations into the prompt so the planner can (a) attach
    # [N] references to bullets and (b) emit the plan-level bibliography.
    citation_seed = content.metadata.get("citation_seed") if content.metadata else None
    if citation_seed:
        lines = ["\n[REFERENCES AVAILABLE — cite them inline as [1], [2], ...]\n"]
        for c in citation_seed[:20]:
            lines.append(f"  {c.get('key', '')}: {c.get('text', '')[:180]}")
        lines.append(
            "\nWhen a slide draws a specific claim from one of these sources, "
            "add its key to that slide's `citations` array, e.g. "
            '{"key":"[1]","text":"<same as above>"}. Also emit a plan-level '
            '"references" array with EVERY key you cited, in numeric order.\n'
        )
        content_summary += "\n".join(lines)

    slide_types_desc = """
    Available slide types (use exactly these names):
    - "title": Title slide. dark_background: true.
    - "section": Section divider. dark_background: true.
    - "content": 3-6 bullet points. For background, motivation, methods narrative.
      Use ONLY when the content is a simple list of facts/points without sub-structure.
    - "content_figure": Bullets left + figure right. Use when a figure illustrates
      specific bullet points. When no source figures exist but a diagram would
      genuinely help the audience, set figure_path="__placeholder__" and write a
      concrete figure_caption describing what the diagram SHOULD show.
    - "full_figure": Full-width figure. For architecture diagrams or hero result
      figures. Same __placeholder__ convention applies when no source figures exist.
    - "metrics": 2-4 large number callouts. For headlining quantitative wins.
    - "table": Comparison table. REQUIRED whenever a table_N is available with comparable data.
    - "two_column": Two parallel content blocks side-by-side. Each column has its own
      sub_title, bullet list, and optional small icon/figure. Use for:
      - Comparing two approaches/methods/theories
      - "Problem vs Solution" or "Before vs After" contrasts
      - Two parallel research streams or findings
      - Two complementary perspectives on the same topic
    - "icon_rows": 3-4 horizontal rows, each with an icon+name label, bold header,
      and short description below. Use for:
      - Displaying principles, features, or component overviews
      - Listing capabilities of a system
      - Breaking down a complex concept into parallel facets
    - "steps": Horizontal flow of 3-5 numbered steps, each with a short title +
      1-line description. Use for:
      - Process flows (pipeline stages, methodology steps)
      - Roadmaps or phased plans
      - Sequential cause-effect chains
    - "emphasis_box": Title area (3-5 bullets or 2-3 paragraphs) + a prominent
      colored bottom box containing 1-2 key takeaway sentences. Use for:
      - Slides that build toward a single critical insight
      - "So what?" pages that translate evidence into meaning
      - Key findings that need to be visually anchored
    - "conclusion": Key takeaways. dark_background: true.
    """

    lang_hint = "Output all content in Chinese." if req.language == "zh" else "Output all content in English."
    talk_directive = TALK_TYPE_DIRECTIVES.get(req.talk_type, "")

    outline_directive = ""
    _src_type = getattr(content, "source_type", "")
    if _src_type == "outline":
        outline_directive = (
            "\n## SOURCE IS A USER OUTLINE — FOLLOW IT\n"
            "The source material is the user's own outline. Preserve its section "
            "order and coverage: walk it top-to-bottom, expanding each outline item "
            "into one (or a few) slides. Do NOT drop outline items and do NOT invent "
            "a different structure. The target slide count is a SOFT guide — if the "
            "outline has more top-level items than the target, add slides to cover "
            "them all rather than dropping content. You may still pick slide types "
            "and rewrite lines into presentation fragments.\n"
        )
    elif _src_type == "markdown":
        outline_directive = (
            "\n## SOURCE IS A MARKDOWN DOCUMENT — FOLLOW ITS STRUCTURE\n"
            "The source is a user Markdown document. Use its heading hierarchy "
            "(#, ##, ###) as the section skeleton: each top-level heading becomes a "
            "section, its content becomes one or a few slides. Preserve heading "
            "order. Rewrite prose into presentation fragments; do NOT copy "
            "paragraphs verbatim.\n"
        )

    # ── presentation-style rewriting rules ──
    rewriting_rules = """
## ⚠️ CRITICAL: PRESENTATION-STYLE REWRITING (NOT TRANSCRIPTION)

You are creating SLIDES for ORAL PRESENTATION, not summarizing the paper.
DO NOT copy sentences from the source. REWRITE every line for spoken delivery.

### Slide titles MUST be CLAIMS, not LABELS
❌ LABEL (boring, like a section heading): "Experimental Results"
✅ CLAIM (memorable, an argument): "Our model beats SOTA by 3.1% on ImageNet"

❌ "Methodology"
✅ "Two-stage training with contrastive pre-training"

❌ "Background"
✅ "Existing methods fail on long sequences (>1k tokens)"

### Bullets MUST be PRESENTATION FRAGMENTS, not paper sentences
❌ FROM PAPER: "We propose a novel attention mechanism that combines local and global context through a hierarchical aggregation strategy, leading to significant improvements in downstream tasks."
✅ FOR SLIDE:
   - "Hierarchical attention: local + global context"
   - "Combines via learned gating (eq. 3)"
   - "Wins on 4/5 downstream tasks"

### Bullet style rules
1. ≤ 12 words per bullet (Chinese: ≤ 20 characters)
2. Start with strong nouns or action verbs — NOT articles ("The", "A")
3. Use → for cause/effect, : for definitions, ↑↓ for trends
4. Drop filler: "We show that", "It is observed that", "Our results indicate"
5. Use math notation: "10⁻⁵" not "ten to the minus five"
6. Every bullet MUST contain ≥1 of: a number, a name, a comparison, a mechanism

### Examples of REWRITING (copy these patterns)

Source: "The proposed method achieves 94.2% accuracy on the ImageNet
benchmark, compared to 91.1% for the previous state of the art ResNet-152,
representing an improvement of 3.1 percentage points."

❌ Slide bullet (copied): "The proposed method achieves 94.2% accuracy on ImageNet, compared to 91.1% for ResNet-152"
✅ Slide bullet (rewritten): "94.2% on ImageNet — beats ResNet-152 by 3.1pp"

Source: "We employed the Adam optimizer with a learning rate of 1e-4 and
trained for 100 epochs with a batch size of 256 on 8 V100 GPUs."

❌ "We trained with Adam at 1e-4 for 100 epochs with batch 256 on 8 V100s"
✅ Three bullets:
   - "Adam, lr = 1×10⁻⁴, 100 epochs"
   - "Batch 256, 8× V100 GPUs"
   - "Total compute: ~340 GPU-hours"
"""

    user_prompt = f"""Plan a scientific presentation with the following specifications:

    Topic/Description: {req.topic}
    Talk type: {req.talk_type}
    Duration: {req.duration_minutes} minutes
    Target slide count: {target_slides} slides (±3)
    Language: {req.language}

    {lang_hint}

    {instruction_block}

    Source material:
    ---
    {content_summary}
    ---

    {slide_types_desc}

    {rewriting_rules}

    {SCIENTIFIC_PRINCIPLES}

    {talk_directive}

    {outline_directive}

    ## NARRATIVE STRUCTURE (follow this arc)

    1. **Title slide** (1): Paper title, authors, affiliation, venue. dark_background: true.
    2. **Hook & Context** (1-2): What problem? Why does it matter NOW?
       - Prefer "two_column" (problem left, significance right) or "emphasis_box" (setup → key insight).
    3. **Background & Related Work** (1-2): Key prior work + the GAP this paper fills.
       - Prefer "icon_rows" for listing prior approaches, or "two_column" (prior work vs gap).
    4. **Methods** (2-3): Use "content_figure" with architecture/pipeline figures,
       or "steps" for pipeline stages.
    5. **Results** (40-50% of slides): The CORE.
       - Title each result slide with a CLAIM
       - Mix slide types: content_figure, metrics, table, emphasis_box — DO NOT make all "content"
       - EVERY available table_N MUST become a "table" slide
       - EVERY captioned figure MUST be assigned to a slide
    6. **Discussion** (1-2): Limitations, comparisons, broader impact.
       - Prefer "two_column" (strengths vs limitations) or "emphasis_box".
    7. **Conclusion** (1): 3-5 takeaways, dark_background: true.

    ## FIGURE & TABLE COVERAGE (CRITICAL — ENFORCED)

    Total available visuals: {len(content.figures)} figures + {len(content.tables)} tables.

    MANDATORY USAGE:
    - At least {max(1, len(content.figures) * 2 // 3)} of {len(content.figures)} figures must appear
    - At least {max(1, len(content.tables) * 2 // 3)} of {len(content.tables)} tables must appear
    - Aim for VISUAL VARIETY: at least 30% of non-title slides should contain a figure or table
    - If you have ≥3 figures, use ≥1 "full_figure" slide for the most important one
    - If you have ≥2 tables, use ≥2 "table" slides
    - "metrics" slides count toward visual variety — use them for headline numbers

    ## SLIDE-TYPE QUOTAS (for {target_slides} slides)

    Required mix (rough proportions):
    - title: 1
    - section: 0-{2 if req.talk_type in ('seminar', 'defense') else 1}
    - content: ≤ {target_slides // 3} (don't over-rely on plain bullets!)
    - content_figure + full_figure: ≥ {max(2, len(content.figures) // 2)}
    - table: ≥ {max(1, len(content.tables) // 2) if content.tables else 0}
    - metrics: 1-2 (if quantitative results exist)
    - two_column: 1-3 (good for comparisons, problem/solution, dual perspectives)
    - icon_rows: 1-2 (good for principles, features, component overviews)
    - steps: 1-2 (good for pipeline stages, methodological flows, roadmaps)
    - emphasis_box: 1-2 (good for key findings, "so what" slides)
    - conclusion: 1

    ## TABLE SLIDES — DATA REQUIREMENTS

    For a "table" slide, you MUST provide:
    - table_headers: list of column names (use the headers from table_N data above)
    - table_rows: list of row arrays (use the rows from table_N data above)
    - highlight_row: 0-indexed row to bold (the row showing YOUR/THE BEST method)
    - title: A CLAIM about what the table shows, e.g. "Ours wins on 4/5 benchmarks"

    Return ONLY a JSON envelope:
    {{"schema_version":"v1","task_type":"presentation_plan","payload":{{
      "title": "Paper/Presentation Title",
      "authors": "Authors (if found)",
      "affiliation": "Institutions (if found)",
      "venue": "Venue (if found)",
      "slides": [
        {{"slide_type":"title","title":"...","subtitle":"...","dark_background":true,
          "extra":{{"authors":"...","affiliation":"..."}}}},

        {{"slide_type":"content","title":"<CLAIM>","bullets":["...","...","..."]}},

        {{"slide_type":"content_figure","title":"<CLAIM>","bullets":["...","..."],
          "figure_path":"figure_0","figure_caption":"<short label>"}},

        {{"slide_type":"metrics","title":"...",
          "metrics":[{{"value":"94.2%","label":"Accuracy"}},{{"value":"2.3×","label":"Speedup"}}]}},

        {{"slide_type":"table","title":"<CLAIM>",
          "table_headers":["Method","Acc","Speed"],
          "table_rows":[["Baseline","81.8%","1.0×"],["Ours","94.2%","2.3×"]],
          "highlight_row":1}},

        {{"slide_type":"two_column","title":"<CLAIM tying both columns together>",
          "columns":[
            {{
              "sub_title":"Left Column Heading",
              "bullets":["Point 1","Point 2"],
              "figure_path":null,
              "figure_caption":null
            }},
            {{
              "sub_title":"Right Column Heading",
              "bullets":["Counterpoint 1","Counterpoint 2"],
              "figure_path":"figure_3",
              "figure_caption":"Comparison chart"
            }}
          ],
          "emphasis_note":"(optional) One-line synthesis below both columns"
        }},

        {{"slide_type":"icon_rows","title":"<CLAIM about the set of items>",
          "rows":[
            {{"label":"01","header":"Principle Name","description":"One-line explanation of this principle"}},
            {{"label":"02","header":"Second Principle","description":"One-line explanation"}},
            {{"label":"03","header":"Third Principle","description":"One-line explanation"}}
          ]
        }},

        {{"slide_type":"steps","title":"<CLAIM about the overall process>",
          "steps":[
            {{"step_number":"1","step_title":"Stage Name","step_desc":"What happens in this stage"}},
            {{"step_number":"2","step_title":"Next Stage","step_desc":"What happens next"}},
            {{"step_number":"3","step_title":"Final Stage","step_desc":"Outcome"}}
          ]
        }},

        {{"slide_type":"emphasis_box","title":"<CLAIM or setup question>",
          "bullets":["Supporting point 1","Supporting point 2","Supporting point 3"],
          "box_text":"KEY INSIGHT: One or two sentences that the audience must remember — the 'so what' of this slide."
        }},

        {{"slide_type":"conclusion","title":"Key Takeaways",
          "bullets":["...","...","..."],"dark_background":true}}
      ]
    }},"meta":{{"confidence":0.0}}}}

    HARD RULES:
    1. NEVER copy a sentence from the source verbatim — REWRITE for slides.
    2. Every slide title (except title/section/conclusion) is a CLAIM, not a label.
    3. Bullets are short fragments (≤12 words / ≤20 Chinese chars).
    4. Use figure_N references for figures and copy table data from table_N data above.
    5. The "content" slide-type ratio must NOT exceed {target_slides // 3 + 1}/{target_slides}.
    6. Spend 40-50% of slides on results.
    7. ALL numbers, p-values, percentages must be preserved.
    8. {caption_lang_rule}
    9. For "two_column" slides: each column MUST have a sub_title and ≥2 bullets.
       If you include a figure in a column, use figure_N reference.
    10. For "icon_rows" slides: exactly 3-5 rows, each with label+header+description.
        Labels can be numbers ("01") or icons (emoji like "🔬" or "📊").
    11. For "steps" slides: exactly 3-5 steps, each with step_number+step_title+step_desc.
    12. For "emphasis_box" slides: box_text MUST be 1-2 short sentences, the SINGLE
        most important insight from this slide's evidence.
    """

    system = (
        f"You are a scientific presentation designer for a {req.talk_type} talk. "
        "You produce structured JSON only. "
        "You REWRITE the paper's content into PRESENTATION FRAGMENTS — never copy "
        "full sentences. Every slide title is a CLAIM. Every bullet is short and "
        "data-rich. You actively choose the RIGHT slide type for each content pattern: "
        "use 'two_column' for comparisons, 'icon_rows' for parallel features, "
        "'steps' for process flows, 'emphasis_box' for key insights, "
        "and 'content_figure'/'full_figure' whenever a relevant figure exists. "
        "Never default to plain 'content' when a more specific layout fits."
    )

    last_error: Exception | None = None
    raw: str = ""

    for attempt in range(1 + max_retries):
        raw = ""
        try:
            raw = await _chat_compat(
                llm_client, system=system, user=user_prompt,
                temperature=0.4, max_tokens=32768,
            )

            payload = extract_structured_payload(raw, "presentation_plan")
            if not payload:
                try:
                    parsed = json.loads(raw)
                    if "slides" in parsed:
                        payload = parsed
                    elif "payload" in parsed:
                        payload = parsed["payload"]
                except Exception:
                    pass

            if not payload or "slides" not in payload:
                raise ValueError(
                    f"LLM returned no valid slides (attempt {attempt + 1}). "
                    f"Raw[:300]: {(raw or '')[:300]}"
                )

            _title = (payload.get("title") or req.topic or "").strip()
            if not _title:
                # Fallback title from the first document heading, else generic.
                if content.sections:
                    _title = next(iter(content.sections.keys()), "").strip()[:80]
                _title = _title or "Untitled Presentation"
            # Normalize slide payloads: move layout-specific fields into extra
            _KNOWN_SLIDE_FIELDS = {
                "slide_type", "title", "subtitle", "bullets", "figure_path",
                "figure_caption", "metrics", "table_headers", "table_rows",
                "highlight_row", "notes", "dark_background",
                "citations",  # per-slide footnote citations
                "extra",
            }
            _slides = []
            for s in payload["slides"]:
                extra = s.get("extra", {}) or {}
                for k in list(s.keys()):
                    if k not in _KNOWN_SLIDE_FIELDS:
                        extra[k] = s.pop(k)
                s["extra"] = extra
                _slides.append(SlideData(**s))

            refs = payload.get("references")
            if not isinstance(refs, list):
                refs = []
            # Keep only entries with a key+text; drop any dangling references so a
            # malformed LLM reply cannot break the bibliography slide.
            refs = [
                {"key": str(r.get("key", "")).strip(),
                 "text": str(r.get("text", "")).strip()}
                for r in refs
                if isinstance(r, dict) and r.get("key") and r.get("text")
            ]
            plan = PresentationPlan(
                title=_title,
                authors=payload.get("authors", ""),
                affiliation=payload.get("affiliation", ""),
                venue=payload.get("venue", ""),
                color_theme=colors,
                references=refs,
                slides=_slides,
            )
            plan = _autoattach_slide_citations(plan)

            plan = _append_references_slide(plan)

            # post-validation — enforce visual variety
            plan = _enforce_visual_variety(plan, content)

            logger.info(
                "[content-planner] Plan: '%s', %d slides "
                "(content=%d, figure=%d, table=%d, metrics=%d) attempt=%d",
                plan.title[:60], len(plan.slides),
                sum(1 for s in plan.slides if s.slide_type == "content"),
                sum(1 for s in plan.slides if s.slide_type in ("content_figure", "full_figure")),
                sum(1 for s in plan.slides if s.slide_type == "table"),
                sum(1 for s in plan.slides if s.slide_type == "metrics"),
                attempt + 1,
            )
            return plan


        except Exception as exc:
            last_error = exc
            logger.warning(
                "[content-planner] Attempt %d/%d failed: %s\nRaw[:2000]: %s",
                attempt + 1, 1 + max_retries, exc, (raw or "")[:2000],
            )

    raise ValueError(
        f"Failed to generate presentation plan after {1 + max_retries} attempts: "
        f"{last_error}"
    )

def _enforce_visual_variety(
    plan: PresentationPlan,
    content: ParsedContent,
) -> PresentationPlan:
    """Post-validation: warn (and best-effort fix) if visuals are under-used.

    Strategies:
      - If unused captioned figures exist AND there are >2 consecutive
        plain 'content' slides in results section, convert one to content_figure.
      - If unused tables exist AND there are no 'table' slides, append one.
    """
    used_figure_refs: set[str] = set()
    for s in plan.slides:
        if s.figure_path and s.figure_path.startswith("figure_"):
            used_figure_refs.add(s.figure_path)

    n_figs = len(content.figures)
    unused_figs = [
        i for i in range(n_figs)
        if f"figure_{i}" not in used_figure_refs
        and content.figures[i].get("caption")  # only push captioned ones
    ]

    # Strategy 1: convert run of plain content slides into content_figure
    if unused_figs:
        for i, slide in enumerate(plan.slides):
            if not unused_figs:
                break
            if (
                slide.slide_type == "content"
                and slide.bullets
                and len(slide.bullets) >= 2
                and not slide.dark_background
                # Skip first 2 slides (title + intro) and last 2 (discussion + conclusion)
                and 2 <= i <= len(plan.slides) - 3
            ):
                fig_idx = unused_figs.pop(0)
                slide.slide_type = "content_figure"
                slide.figure_path = f"figure_{fig_idx}"
                cap = content.figures[fig_idx].get("caption", "")[:80]
                slide.figure_caption = cap
                logger.info(
                    "[variety] Upgraded slide %d to content_figure with figure_%d",
                    i, fig_idx,
                )

    # Strategy 2: ensure at least one table slide exists if tables available
    has_table_slide = any(s.slide_type == "table" for s in plan.slides)
    if content.tables and not has_table_slide:
        # Pick the table with most data
        best_tbl = max(
            content.tables,
            key=lambda t: len(t.get("rows", [])) * len(t.get("headers", [])),
        )
        # Insert before conclusion
        insert_pos = len(plan.slides) - 1
        for i in range(len(plan.slides) - 1, -1, -1):
            if plan.slides[i].slide_type == "conclusion":
                insert_pos = i
                break

        title_text = best_tbl.get("caption", "")[:60] or "Quantitative Comparison"
        new_slide = SlideData(
            slide_type="table",
            title=title_text,
            table_headers=[str(h) for h in best_tbl.get("headers", [])],
            table_rows=[
                [str(c) for c in r] for r in best_tbl.get("rows", [])[:6]
            ],
            highlight_row=len(best_tbl.get("rows", [])) - 1,  # often last row = ours
        )
        plan.slides.insert(insert_pos, new_slide)
        logger.info("[variety] Inserted missing table slide at position %d", insert_pos)

    return plan

async def _chat_compat(llm, *, system: str, user: str, temperature: float, max_tokens: int) -> str:
    """Adapt to omni's LLMClient.chat(system_prompt, user_message) signature.

    Falls back to chat_with_tools-less plain completion. JSON mode is requested
    via the system prompt instead of a kwarg (omni's mock/openai clients honour
    plain-text JSON instructions)."""
    try:
        return await llm.chat(system, user, temperature=temperature, max_tokens=max_tokens)
    except TypeError:
        # minimal signature
        return await llm.chat(system, user)


# ── LLM strategy decision (agentic mode) ────────────────

async def decide_presentation_strategy(
    llm_client: Any,
    content: ParsedContent,
    req: PresentationRequest,
) -> dict[str, Any]:
    """Let the LLM decide the deck strategy instead of a fixed pipeline.

    Returns a JSON-able decision record describing the recommended slide-type
    mix, whether a human review checkpoint is warranted, and why. The engine
    logs this as a telemetry ``decision`` event and (optionally) surfaces the
    ``recommend_review`` flag to gate a human checkpoint.
    """
    target_slides = _get_target_slide_count(
        req.talk_type, req.duration_minutes, override=req.target_slides,
    )
    n_fig = len(content.figures)
    n_tab = len(content.tables)

    system = (
        "You are a scientific presentation strategist. You output ONLY compact "
        "JSON. You decide the high-level plan for a talk, not the slide text."
    )
    user = f"""Decide the strategy for this talk.

Talk type: {req.talk_type}
Duration: {req.duration_minutes} min → about {target_slides} slides
Source type: {content.source_type}
Available visuals: {n_fig} figures, {n_tab} tables
Language: {req.language}
User instruction: {getattr(req, "user_instruction", "") or req.topic}

Return ONLY this JSON:
{{
  "target_slides": <int>,
  "slide_mix": {{"content": <int>, "content_figure": <int>,
                 "full_figure": <int>, "metrics": <int>, "table": <int>}},
  "emphasis": "<one line: what to foreground>",
  "recommend_review": <true|false>,
  "review_reason": "<why a human should approve the outline, or empty>",
  "risks": ["<short risk>", "..."]
}}

Guidance: recommend_review=true when the source is ambiguous, visuals are
scarce for the requested length, or the talk is high-stakes (defense)."""

    raw = await _chat_compat(
        llm_client, system=system, user=user, temperature=0.3, max_tokens=1024,
    )
    payload = extract_structured_payload(raw, "presentation_strategy") or {}
    if not isinstance(payload, dict):
        payload = {}

    # Defensive defaults so the engine never crashes on a bad LLM reply.
    payload.setdefault("target_slides", target_slides)
    payload.setdefault("slide_mix", {})
    payload.setdefault("emphasis", "")
    payload.setdefault("recommend_review", req.talk_type == "defense")
    payload.setdefault("review_reason", "")
    payload.setdefault("risks", [])
    return payload

def _append_references_slide(plan: PresentationPlan) -> PresentationPlan:
    """Auto-append References slide(s) when the plan has citations but none.

    Idempotent: if a `references` slide already exists we do nothing. When
    there are more than 12 references we split them across multiple slides
    (12 per page) rather than truncating, so no citation is silently dropped.
    Slides are inserted just before the conclusion so the talk still ends on
    a takeaway.
    """
    if not plan.references:
        return plan
    if any(s.slide_type == "references" for s in plan.slides):
        return plan

    per_page = 12
    total = len(plan.references)
    pages = (total + per_page - 1) // per_page

    insert_at = len(plan.slides)
    for i in range(len(plan.slides) - 1, -1, -1):
        if plan.slides[i].slide_type == "conclusion":
            insert_at = i
            break

    new_slides: list[SlideData] = []
    for pi in range(pages):
        chunk = plan.references[pi * per_page : (pi + 1) * per_page]
        title = "References" if pages == 1 else f"References ({pi + 1}/{pages})"
        new_slides.append(SlideData(
            slide_type="references",
            title=title,
            bullets=[f"{r['key']} {r['text']}" for r in chunk],
        ))

    for offset, sd in enumerate(new_slides):
        plan.slides.insert(insert_at + offset, sd)
    return plan

def _autoattach_slide_citations(plan: PresentationPlan) -> PresentationPlan:
    """Ensure every slide that mentions ``[N]`` also carries [N] as a footnote.

    Scans each slide's title + bullets + figure_caption for tokens like
    ``[1]``, ``[2]``. For every match, if ``plan.references`` has an entry
    with that key AND the slide doesn't already list it in ``citations``,
    append it. Fully idempotent: calling twice yields the same plan.
    """
    if not plan.references:
        return plan
    import re as _re

    # Fast lookup: key -> reference dict.
    ref_by_key = {str(r.get("key", "")).strip(): r for r in plan.references}
    citation_key_re = _re.compile(r"\[\d+\]")

    for slide in plan.slides:
        # References slide itself already lists everything.
        if slide.slide_type == "references":
            continue
        # Collect all textual signals on the slide.
        haystack_parts: list[str] = [slide.title or "", slide.figure_caption or ""]
        haystack_parts.extend(slide.bullets or [])
        haystack = " ".join(haystack_parts)

        cited_keys = citation_key_re.findall(haystack)
        if not cited_keys:
            continue

        already = {str(c.get("key", "")).strip() for c in (slide.citations or [])}
        new_citations = list(slide.citations or [])
        for key in cited_keys:
            if key in already:
                continue
            ref = ref_by_key.get(key)
            if ref is None:
                continue
            new_citations.append({"key": key, "text": ref.get("text", "")})
            already.add(key)
        if new_citations != (slide.citations or []):
            slide.citations = new_citations
    return plan
