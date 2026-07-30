"""Data models for the research_pptx skill (omni edition)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── enum alias normalization (LLM planners often pass natural-language values) ──
_LANGUAGE_ALIASES = {
    "chinese": "zh", "zh": "zh", "zh-cn": "zh", "zh_cn": "zh", "cn": "zh",
    "english": "en", "en": "en", "en-us": "en", "en_us": "en",
}
_TALK_TYPE_ALIASES = {
    "conference": "conference", "conf": "conference",
    "seminar": "seminar",
    "group_meeting": "group_meeting", "group meeting": "group_meeting",
    "groupmeeting": "group_meeting", "lab meeting": "group_meeting",
    "defense": "defense", "thesis defense": "defense",
}
_COLOR_THEME_ALIASES = {
    "midnight_executive": "midnight_executive", "midnight": "midnight_executive",
    "teal_trust": "teal_trust", "teal": "teal_trust",
    "forest_moss": "forest_moss", "forest": "forest_moss", "moss": "forest_moss",
    "charcoal_minimal": "charcoal_minimal", "charcoal": "charcoal_minimal", "minimal": "charcoal_minimal",
}

_COMMON_FIELD_ALIASES: dict[str, str] = {
    "paper_path": "pdf_uri",
    "paper_uri": "pdf_uri",
    "source_path": "pdf_uri",
    "source_uri": "pdf_uri",
    "file_path": "pdf_uri",
    "slide_count": "target_slides",
    "slide_number": "target_slides",
    "num_slides": "target_slides",
    "page_count": "target_slides",
    "outline_path": "markdown_uri",
    "outline_uri": "markdown_uri",
    "output_language": "language",
}


def remap_common_alias_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with common planner aliases mapped to contract fields."""

    normalized = dict(data)
    for alias, target in _COMMON_FIELD_ALIASES.items():
        if alias not in normalized:
            continue
        if not normalized.get(target):
            normalized[target] = normalized[alias]
        normalized.pop(alias, None)
    return normalized

def normalize_language(value: Any) -> str:
    key = str(value or "").strip().lower()
    return _LANGUAGE_ALIASES.get(key, "en" if not key else key)


def normalize_talk_type(value: Any) -> str:
    key = str(value or "").strip().lower()
    return _TALK_TYPE_ALIASES.get(key, key)

class PresentationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")  # tolerate ctx.base_input() fields

    @model_validator(mode="before")
    @classmethod
    def _remap_common_alias_fields(cls, data: Any) -> Any:
        """Auto-correct common parameter-name mistakes so models find the right fields.

        The Omni planner/routing often invents names like ``paper_path`` or
        ``slide_count`` that aren't in our input_schema. Remap them silently
        instead of rejecting the call or forcing a multi-turn docs_search.
        """
        if not isinstance(data, dict):
            return data
        return remap_common_alias_fields(data)

    topic: str = Field("", description="Main topic or instruction prompt")
    user_instruction: str | None = None
    pdf_uri: str | None = Field(None, description="artifact:// or local PDF path")
    paper_uris: list[str] = Field(
        default_factory=list,
        description="artifact:// or local paths of multiple PDFs to merge.",
    )
    file_uris: list[str] = Field(default_factory=list)
    reference_text: str | None = None
    corpus_query: str | None = Field(
        None,
        description="Search omni's local corpus and use hits as source text.",
    )
    source_ids: list[str] = Field(
        default_factory=list,
        description="ROM source ids to build the deck from.",
    )
    language: str = Field("en", description="en or zh")
    talk_type: str = Field("conference")
    duration_minutes: int = Field(15, ge=5, le=90)
    target_slides: int | None = Field(None, ge=3, le=80)
    color_theme: str = Field("midnight_executive")

    @field_validator("language", mode="before")
    @classmethod
    def _norm_language(cls, v: Any) -> str:
        if v is None:
            return "en"
        key = str(v).strip().lower()
        return _LANGUAGE_ALIASES.get(key, "en" if not key else key)

    @field_validator("talk_type", mode="before")
    @classmethod
    def _norm_talk_type(cls, v: Any) -> str:
        if v is None:
            return "conference"
        key = str(v).strip().lower()
        return _TALK_TYPE_ALIASES.get(key, "conference" if not key else key)

    @field_validator("color_theme", mode="before")
    @classmethod
    def _norm_color_theme(cls, v: Any) -> str:
        if v is None:
            return "midnight_executive"
        key = str(v).strip().lower()
        return _COLOR_THEME_ALIASES.get(key, "midnight_executive" if not key else key)

    # LLM-decision + human review
    mode: str = Field("auto", description="auto | agentic")
    review_mode: str = Field("none", description="none | plan | interactive")
    resume_token: str = ""
    approved_plan: dict[str, Any] | None = None
    plan_edits: list[dict[str, Any]] | None = Field(
        None,
        description="Lightweight edit ops applied to the cached plan on resume "
        "(set_title, set_bullets, set_bullet, remove_slide, add_slide, set_figure, "
        "set_type, swap_slides, move_slide). Use this instead of approved_plan "
        "for surgical edits — the engine applies them on top of the original plan.",
    )

    # ── Feature 1: adopt a user PPTX's theme (colours + fonts) ──
    template_uri: str | None = Field(None, description="artifact:// or path of a PPTX template")

    # ── Feature 2: more source formats ──
    outline: str | None = Field(None, description="user-provided outline text → slides")
    markdown_uri: str | None = Field(None, description="artifact:// or path of a .md source")

    session_id: str = ""
    channel: str = ""


class SlideData(BaseModel):
    slide_type: str
    title: str = ""
    subtitle: str = ""
    bullets: list[str] = Field(default_factory=list)
    figure_path: str | None = None
    figure_caption: str | None = None
    metrics: list[dict[str, str]] = Field(default_factory=list)
    table_headers: list[str] = Field(default_factory=list)
    table_rows: list[list[str]] = Field(default_factory=list)
    highlight_row: int | None = None
    notes: str = ""
    dark_background: bool = False
    citations: list[dict[str, str]] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("table_rows", mode="before")
    @classmethod
    def _coerce_cells_to_str(cls, v: Any) -> Any:
        """LLMs happily return numeric cells (94.2 instead of "94.2"). Coerce
        every non-string cell into str so a well-formed plan doesn't waste
        a retry on `Input should be a valid string`. Rows / cells that are
        None become empty strings; everything else uses Python's str()."""
        if not isinstance(v, list):
            return v
        out: list[list[str]] = []
        for row in v:
            if not isinstance(row, (list, tuple)):
                continue
            out.append(["" if c is None else str(c) for c in row])
        return out


class PresentationPlan(BaseModel):
    model_config = ConfigDict(extra="allow")  # allow private _template_local_path

    title: str
    authors: str = ""
    affiliation: str = ""
    venue: str = ""
    color_theme: dict[str, str] = Field(default_factory=dict)
    header_font: str = "Arial Black"
    body_font: str = "Arial"
    template_master: dict[str, Any] = Field(default_factory=dict)
    # Absolute local path of the resolved template PPTX, used by the
    # template-reuse render backend (empty = rebuild path).
    template_local_path: str = ""
    references: list[dict[str, str]] = Field(default_factory=list)
    slides: list[SlideData] = Field(default_factory=list)


class ParsedContent(BaseModel):
    source_type: str
    markdown_text: str = ""
    sections: dict[str, str] = Field(default_factory=dict)
    figures: list[dict[str, str]] = Field(default_factory=list)
    tables: list[dict[str, Any]] = Field(default_factory=list)
    equations: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PresentationResult(BaseModel):
    status: str = "ok"
    summary: str = ""
    title: str
    pptx_uri: str = ""
    slide_count: int = 0
    figures_used: int = 0
    research: dict[str, Any] = Field(default_factory=dict)
    run_id: str = ""
    report_uri: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
