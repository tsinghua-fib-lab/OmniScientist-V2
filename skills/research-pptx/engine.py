
"""Research PPTX Engine (omni edition).

Pipeline (auto mode):
  parse inputs → plan (LLM) → render equations → resolve figures →
  fix sparse slides → render PPTX (Node) → QA overflow loop →
  text-domain structured visual critique → repair & re-render → store artifact.

agentic mode delegates the slide-type mix to the LLM and may insert a review
checkpoint. review_mode='plan' returns the outline and a resume_token before
rendering, enabling human-in-the-loop approval.

The default model has no multimodal (image) capability, so the "visual" QA is
performed in the TEXT domain: the rendered deck is translated into a structured
layout report (figure aspect / layout mode / fill ratio / bullet lengths /
overflow) plus per-figure caption/related_text/section metadata, and the LLM
reviews that report to catch figure/section mismatches, sparse slides, and
figure↔bullet inconsistencies it cannot fix geometrically.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util as _ilu
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

_SKILL_DIR = Path(__file__).resolve().parent


def _load_sibling(mod_name: str):
    """Load a sibling module by file path (skill is self-contained, not a package)."""
    cached = sys.modules.get(f"research_pptx_{mod_name}")
    if cached is not None:
        return cached
    spec = _ilu.spec_from_file_location(
        f"research_pptx_{mod_name}", _SKILL_DIR / f"{mod_name}.py"
    )
    module = _ilu.module_from_spec(spec)
    sys.modules[f"research_pptx_{mod_name}"] = module
    spec.loader.exec_module(module)
    return module


_models = _load_sibling("models")
ParsedContent = _models.ParsedContent
PresentationPlan = _models.PresentationPlan
PresentationRequest = _models.PresentationRequest
PresentationResult = _models.PresentationResult

_slide_renderer = _load_sibling("slide_renderer")
check_overflow_structured = _slide_renderer.check_overflow_structured

_telemetry = _load_sibling("_telemetry")
build_telemetry = _telemetry.build_telemetry

logger = logging.getLogger(__name__)

# ── Localization map for Chinese (zh) output ──
# Keys are dot-separated paths. Values are the zh string; English is the
# default literal used inline.
_L10N_ZH = {
    # Progress messages
    "progress.parsing_pdf":                 "\u89e3\u6790PDF\u5185\u5bb9\u2026",
    "progress.processing_inputs":           "\u5904\u7406\u8f93\u5165\u2026",
    "progress.source_done":                 "\u6765\u6e90\u89e3\u6790\u5b8c\u6210",
    "progress.deciding_strategy":           "\u51b3\u5b9a\u6f14\u793a\u7b56\u7565\u2026",
    "progress.planning_structure":          "\u89c4\u5212\u5e7b\u706f\u7247\u7ed3\u6784\u2026",
    "progress.structure_ready":             "\u6f14\u793a\u7ed3\u6784\u5b8c\u6210",
    "progress.resuming_render":             "\u6062\u590d\u5e7b\u706f\u7247\u6e32\u67d3\u2026",
    "progress.rendering_eq":                "\u6e32\u67d3\u516c\u5f0f",
    "progress.resolving_figs":              "\u89e3\u6790\u56fe\u8868",
    "progress.rendering_slides":            "\u6e32\u67d3\u5e7b\u706f\u7247",
    "progress.render_done":                 "\u5e7b\u706f\u7247\u6e32\u67d3\u5b8c\u6210",
    "progress.quality_check":               "\u8d28\u91cf\u68c0\u67e5",
    "progress.quality_done":                "\u8d28\u91cf\u68c0\u67e5\u901a\u8fc7",
    "progress.visual_review":               "\u89c6\u89c9\u5ba1\u9605",
    "progress.visual_done":                 "\u89c6\u89c9\u5ba1\u9605\u5b8c\u6210",
    "progress.storing":                     "\u5b58\u50a8\u6587\u4ef6",
    # Milestone labels
    "milestone.source_parsing":             "\u6765\u6e90\u89e3\u6790",
    "milestone.slide_structure":            "\u6f14\u793a\u7ed3\u6784",
    "milestone.slide_render":               "\u5e7b\u706f\u7247\u6e32\u67d3",
    "milestone.quality_check":              "\u8d28\u91cf\u68c0\u67e5",
    "milestone.visual_review":              "\u89c6\u89c9\u5ba1\u9605",
    # Milestone / deliverable detail fragments
    "detail.pages":                         "\u9875",
    "detail.figures":                       "\u5f20\u56fe",
    "detail.tables":                        "\u4e2a\u8868\u683c",
    "detail.slides":                        "\u9875",
    "detail.no_overflow":                   "\u65e0\u6587\u5b57\u6ea2\u51fa",
    "detail.warnings_fixed":                "\u4fee\u590d {count} \u5904\u8b66\u544a",
    "detail.edits":                         "{count} \u5904\u4fee\u6539",
    "detail.figures_used":                  "\u4f7f\u7528\u56fe\u8868 {} \u5f20",
    "detail.file_size":                     "{} MB",
    # Deliverable block
    "deliverable.slides":                   "\u5e7b\u706f\u7247 {} \u9875",
    "deliverable.figures_used":             "\u4f7f\u7528\u56fe\u8868 {} \u5f20",
    "deliverable.file_size":                "{} MB",
    # Completion
    "completion.saved":                     "PPT \u751f\u6210\u5b8c\u6210 · {} MB · {}",
    "completion.saved_no_size":             "PPT \u751f\u6210\u5b8c\u6210 · {}",
    # Summary
    "summary.generated":                    "\u5df2\u751f\u6210 {n_slides} \u9875 '{title}' \u6f14\u793a\u6587\u7a3f\uff0c\u5305\u542b {figures} \u5f20\u56fe\u8868",
    # Source detail template
    "source_detail.with_pages":             "{pages} \u9875 · {figures} \u5f20\u56fe · {tables} \u4e2a\u8868\u683c",
    "source_detail.no_pages":               "{figures} \u5f20\u56fe · {tables} \u4e2a\u8868\u683c",
    "source_detail.title":                  "\u79d1\u7814\u6f14\u793a\u6587\u7a3f",
    "source_detail.source_label":           "\u6765\u6e90",
    "source_detail.format_label":           "\u7c7b\u578b",
}


def _l10n(language: str, key: str, **kwargs: Any) -> str:
    """Return the localized string for *key* when *language* is 'zh', otherwise empty.
    Currently all output is English-only; the L10N dict is reserved for future use.
    Callers use: ``_l10n(lang, "key", **fmt) or f"english {fallback}"``."""
    return ""

_VALID_THEMES = ("midnight_executive", "teal_trust", "forest_moss", "charcoal_minimal")
_VALID_TALKS = ("conference", "seminar", "group_meeting", "defense")
# In-process store of paused plans awaiting user review (token -> plan dict).
_REVIEW_CACHE: dict[str, dict[str, Any]] = {}


def _normalize_language(value: Any) -> str:
    return _models.normalize_language(value)


def _normalize_talk_type(value: Any) -> str:
    return _models.normalize_talk_type(value)


def _has_presentation_source(values: dict[str, Any]) -> bool:
    """Return whether an invocation contains any supported deck source."""

    scalar_fields = (
        "topic",
        "outline",
        "markdown_uri",
        "pdf_uri",
        "reference_text",
        "corpus_query",
    )
    collection_fields = ("paper_uris", "file_uris", "source_ids")
    return any(str(values.get(key, "")).strip() for key in scalar_fields) or any(
        bool(values.get(key)) for key in collection_fields
    )


def _slide_count_mismatch_result(
    *,
    target: int,
    actual: int,
    phase: str,
    resume_token: str = "",
) -> dict[str, Any]:
    """Build the recoverable error used by every exact-count gate."""

    remediation = (
        "Adjust plan_edits or pass a complete approved_plan with the exact count, "
        "then resume with the same resume_token."
        if resume_token
        else "Re-plan the deck with exactly the requested slide count."
    )
    result = {
        "status": "error",
        "outcome": {"code": "slide_count_mismatch"},
        "phase": phase,
        "error": (
            f"The presentation plan has {actual} slides, but target_slides requires "
            f"exactly {target}. {remediation}"
        ),
        "recoverable": True,
        "blocking": False,
        "target_slides": target,
        "actual_slides": actual,
        "error_info": {
            "code": "slide_count_mismatch",
            "retryable": True,
            "workflow_recoverable": True,
        },
    }
    if resume_token:
        result["resume_token"] = resume_token
    return result


def _pptx_assessment(
    ctx: Any,
    input_data: dict[str, Any],
    *,
    req: Any,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Assess the concrete rendered deck without asking the host to infer quality."""

    metadata = result.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    slide_count = max(0, int(result.get("slide_count") or 0))
    qa_warnings = max(0, int(metadata.get("qa_warnings") or 0))
    evidence_refs = [
        str(item.get("uri") or "")
        for item in result.get("artifacts") or []
        if isinstance(item, dict) and item.get("uri")
    ]
    pptx_uri = str(result.get("pptx_uri") or "")
    if pptx_uri and pptx_uri not in evidence_refs:
        evidence_refs.append(pptx_uri)

    # Grounding signal (C2): a delivered deck built from *no* input figures and
    # *no* cited sources has model-generated specifics (numbers, architecture,
    # roadmap). That is a quality note to surface for review — not a delivery
    # failure — so it rides the same advisory channel as layout warnings.
    figures_used = max(0, int(result.get("figures_used") or 0))
    research = result.get("research")
    source_ids = research.get("source_ids") if isinstance(research, dict) else None
    source_count = len(source_ids) if isinstance(source_ids, list) else 0

    # Delivery (was a real deck produced?) is separate from quality (residual
    # layout warnings / ungrounded content). A delivered deck with quality notes
    # is "succeeded with quality notes", not a failure-shaped degrade — so the
    # notes ride an *advisory* criterion the host surfaces without downgrading.
    complete = bool(slide_count and pptx_uri)
    advisory_note = False
    if not complete:
        status = "unknown"
        summary = (
            "A deck result was returned, but the provider could not confirm both "
            "a stored PPTX and a non-empty slide count."
        )
    else:
        status = "passed"
        notes: list[str] = []
        if qa_warnings:
            notes.append(f"{qa_warnings} residual layout warning(s)")
        if figures_used == 0 and source_count == 0:
            notes.append(
                "content is model-generated with no input figures or cited sources "
                "— verify specific facts, numbers, and claims before use"
            )
        if notes:
            advisory_note = True
            summary = (
                f"Rendered {slide_count} slides. Delivered with quality notes: "
                + "; ".join(notes)
                + "."
            )
        else:
            summary = f"Rendered {slide_count} slides with no remaining layout warnings."

    deliverable_id = str(
        input_data.get("deliverable_id")
        or input_data.get("deliverable")
        or "artifact.slides"
    )
    authority = getattr(ctx, "provider_authority", None)
    authority_fingerprint = (
        str(authority.get("fingerprint") or "")
        if isinstance(authority, dict)
        else ""
    )
    contract_hash = authority_fingerprint or hashlib.sha256(
        b"research-pptx:quality-contract:v1"
    ).hexdigest()
    step_id = str(
        getattr(ctx, "workflow_step_key", "")
        or getattr(ctx, "workflow_step_id", "")
        or input_data.get("workflow_step_id")
        or deliverable_id
    )
    return {
        "schema": "omni.deliverable-assessment/v1",
        "deliverable_id": deliverable_id,
        "provider_binding_id": str(
            input_data.get("provider_binding_id")
            or f"skill:research-pptx:{deliverable_id}"
        ),
        "provider": "research-pptx",
        "provider_authority_fingerprint": authority_fingerprint,
        "contract_hash": contract_hash,
        "step_id": step_id,
        "feedback": summary,
        "status": status,
        # This engine already performs bounded repair before storing the PPTX.
        # Replaying after artifact emission is not side-effect safe.
        "retryable": False,
        "effective_inputs": {
            "topic": str(getattr(req, "topic", "") or ""),
            "language": str(getattr(req, "language", "") or ""),
            "talk_type": str(getattr(req, "talk_type", "") or ""),
            "duration_minutes": int(getattr(req, "duration_minutes", 0) or 0),
            "target_slides": getattr(req, "target_slides", None),
            "color_theme": str(getattr(req, "color_theme", "") or ""),
            "mode": str(getattr(req, "mode", "") or ""),
            "review_mode": str(getattr(req, "review_mode", "") or ""),
        },
        "criteria": [
            {
                "criterion_id": "slides_rendered_and_quality_checked",
                # A delivered-but-warned deck rides an advisory criterion so the
                # host surfaces the layout note without degrading the task; the
                # envelope-level ``status`` stays the delivery verdict (passed).
                "status": "degraded" if advisory_note else status,
                "summary": summary,
                "evidence_refs": evidence_refs,
                "advisory": advisory_note,
            }
        ],
        "evidence_refs": evidence_refs,
        "summary": summary,
    }



# ── module-level LLM helpers (text-domain visual critique) ──
# Kept independent of content_planner's private helpers so the engine's QA loop
# has no cross-module coupling.

async def _chat_compat_engine(
    llm, *, system: str, user: str, temperature: float = 0.3, max_tokens: int = 2048
) -> str:
    """Call the LLM with a plain (system, user) completion, tolerant of signature."""
    try:
        return await llm.chat(system, user, temperature=temperature, max_tokens=max_tokens)
    except TypeError:
        return await llm.chat(system, user)


def extract_structured_payload_engine(raw: str) -> Any | None:
    """Pull a JSON object/array out of an LLM reply (fenced or inline)."""
    if not raw:
        return None
    for cand in (raw, re.sub(r"^```[a-zA-Z]*\n|\n```$", "", raw.strip())):
        try:
            return json.loads(cand)
        except (TypeError, json.JSONDecodeError):
            continue
    # first [...] or {...} block
    for pattern in (r"\[.*\]", r"{.*}"):
        m = re.search(pattern, raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    return None


def _omni_renderer_runtime_dir(ctx: Any) -> Path | None:
    """Resolve Omni's fixed renderer cache; portable hosts keep local modules."""
    paths = getattr(ctx, "paths", None)
    cache_dir = getattr(paths, "cache_dir", None)
    if cache_dir is None:
        return None
    return Path(cache_dir) / "skill-runtimes" / "research-pptx"


def _renderer_preflight_error(
    exc: BaseException,
    *,
    setup_command: str = "cd scripts && npm ci",
) -> dict[str, Any]:
    """Return an actionable, non-retryable setup error for the harness."""
    code = str(getattr(exc, "code", "renderer_unavailable"))
    message = str(exc)
    missing = list(getattr(exc, "missing", ()))
    remediation = f"Run `{setup_command}` in a terminal, then retry."
    display_error = f"{message} {remediation}"
    return {
        "status": "error",
        "outcome": {"code": code},
        "error": display_error,
        "summary": f"research-pptx renderer setup is incomplete. {remediation}",
        "recoverable": False,
        "blocking": True,
        "setup_command": setup_command,
        "next_actions": [setup_command],
        "action_required": {
            "kind": "install",
            "command": setup_command,
            "missing": missing,
        },
        "error_info": {
            "code": code,
            "message": display_error,
            "missing": missing,
            "retryable": False,
            "workflow_recoverable": False,
            "setup_command": setup_command,
        },
    }


def _python_dependency_error(exc: ModuleNotFoundError) -> dict[str, Any]:
    """Return the repository-extra setup action for an optional Python phase."""
    missing = [str(exc.name or "research-pptx runtime")]
    setup_command = "omni update --force"
    message = f"research-pptx is missing a required Python dependency: {missing[0]}."
    return {
        "status": "error",
        "outcome": {"code": "runtime_dependency_missing"},
        "error": message,
        "summary": message,
        "recoverable": False,
        "blocking": True,
        "setup_command": setup_command,
        "next_actions": [setup_command],
        "action_required": {
            "kind": "install",
            "command": setup_command,
            "missing": missing,
        },
        "error_info": {
            "code": "runtime_dependency_missing",
            "message": message,
            "missing": missing,
            "retryable": False,
            "workflow_recoverable": False,
            "setup_command": setup_command,
        },
    }


_FILE_REFERENCE_RE = re.compile(
    r"\"(?P<double_quoted>[^\"\n]+\.(?:pdf|md|markdown|txt|pptx))\""
    r"|'(?P<single_quoted>[^'\n]+\.(?:pdf|md|markdown|txt|pptx))'"
    r"|(?P<artifact>artifact://[^\s\"'，。；;]+?\.(?:pdf|md|markdown|txt|pptx))"
    r"|(?P<windows>[A-Za-z]:[/\\][^\n\"'，。；;]+?\.(?:pdf|md|markdown|txt|pptx))"
    r"|(?P<posix>(?:~|\.{1,2})?[/\\][^\n\"'，。；;]+?\.(?:pdf|md|markdown|txt|pptx))"
    r"|(?P<bare>[^\s/\\\"'，。；;,]+\.(?:pdf|md|markdown|txt|pptx))",
    re.IGNORECASE,
)
_PPTX_EXPORT_VERB_RE = re.compile(
    r"\b(?:convert|export|turn|save|output)\b|\u5bfc\u51fa|\u8f6c\u6362|"
    r"\u8f6c\u6210|\u8f93\u51fa|\u53d8\u6210|\u6539\u6210|\u5b58\u4e3a",
    re.IGNORECASE,
)
_PPTX_EXPORT_TARGET_RE = re.compile(
    r"\b(?:pdf|png|jpe?g|svg|image)\b|\u56fe\u7247|\u56fe\u50cf",
    re.IGNORECASE,
)
_PPTX_TEMPLATE_CUE_RE = re.compile(
    r"\b(?:template|theme|layout)\b|\u6a21\u677f|\u4e3b\u9898|\u7248\u5f0f",
    re.IGNORECASE,
)


def _topic_file_references(topic: str) -> list[str]:
    """Return ordered file references embedded in a natural-language topic."""

    references: list[str] = []
    seen: set[str] = set()
    for match in _FILE_REFERENCE_RE.finditer(topic):
        value = next((part for part in match.groups() if part is not None), "").strip()
        value = value.rstrip(")]】}")
        if value and value not in seen:
            seen.add(value)
            references.append(value)
    return references


def _extract_file_paths_from_topic(args: dict[str, Any]) -> None:
    """Populate source/template fields from paths written inside ``topic``.

    Explicit structured fields always win. A PPTX mentioned only as the source
    of a conversion/export request is deliberately not treated as a template.
    """

    topic = str(args.get("topic") or "").strip()
    if not topic:
        return

    candidates: dict[str, list[str]] = {
        "pdf": [],
        "markdown": [],
        "text": [],
        "pptx": [],
    }
    for reference in _topic_file_references(topic):
        suffix = Path(reference).suffix.lower()
        if suffix == ".pdf":
            candidates["pdf"].append(reference)
        elif suffix in {".md", ".markdown"}:
            candidates["markdown"].append(reference)
        elif suffix == ".txt":
            candidates["text"].append(reference)
        elif suffix == ".pptx":
            candidates["pptx"].append(reference)

    if candidates["pdf"] and not args.get("pdf_uri"):
        args["pdf_uri"] = candidates["pdf"][0]
    if candidates["markdown"] and not args.get("markdown_uri"):
        args["markdown_uri"] = candidates["markdown"][0]

    is_export = bool(
        _PPTX_EXPORT_VERB_RE.search(topic) and _PPTX_EXPORT_TARGET_RE.search(topic)
    )
    has_template_cue = bool(_PPTX_TEMPLATE_CUE_RE.search(topic))
    if candidates["pptx"] and (not is_export or has_template_cue) and not args.get("template_uri"):
        args["template_uri"] = candidates["pptx"][0]

    if (
        candidates["text"]
        and not args.get("outline")
        and not args.get("pdf_uri")
        and not args.get("markdown_uri")
    ):
        text_uri = candidates["text"][0]
        if not text_uri.startswith("artifact://"):
            path = Path(text_uri.removeprefix("file://")).expanduser()
            try:
                if path.is_file():
                    args["outline"] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass


class ResearchPptxEngine:
    """LLM-driven scientific presentation generation engine."""

    def __init__(self) -> None:
        self.ctx: Any = None  # injected by the executor before execute()

    @staticmethod
    def _extract_upstream_source(kwargs: Any) -> str:
        """Pull text from upstream workflow step results, if any."""
        if not isinstance(kwargs, dict):
            return ""
        dep = kwargs.get("depends_on_results") or kwargs.get("workflow_results") or {}
        if not isinstance(dep, dict):
            return ""
        parts: list[str] = []
        for res in dep.values():
            if not isinstance(res, dict):
                continue
            for key in ("summary", "text", "report", "abstract"):
                if res.get(key):
                    parts.append(str(res[key]))
                    break
            # Upstream literature providers often return a list of papers.
            papers = res.get("papers") or res.get("results")
            if isinstance(papers, list):
                for p in papers[:12]:
                    if isinstance(p, dict):
                        title = p.get("title", "")
                        summ = p.get("summary") or p.get("abstract") or ""
                        if title:
                            parts.append(f"### {title}\n{summ}")
        return "\n\n".join(parts).strip()[:20000]


    # ── synchronous validation (returns dict -> shown to the model) ──
    @staticmethod
    def validate_params(
            *, arguments: dict[str, Any] | None = None, input_data: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        args = arguments or input_data or {}
        normalized = _models.remap_common_alias_fields(args)
        args.clear()
        args.update(normalized)
        _extract_file_paths_from_topic(args)
        # Single-skill routing / a workflow step may hand us the raw user text as
        # ``input``/``query`` (or the overall ``workflow_goal``) instead of
        # ``topic``. Absorb it so a topic-only deck still has a source instead of
        # failing with "provide a source". Mutating ``args`` here propagates to
        # execute() because the executor reuses the same merged dict.
        if not str(args.get("topic", "")).strip():
            _fb = str(args.get("input") or args.get("query")
                      or args.get("workflow_goal") or "").strip()
            if _fb:
                args["topic"] = _fb
                _extract_file_paths_from_topic(args)
        action = str(args.get("action", "generate")).lower()
        if action != "generate":
            return {
                "error": "research-pptx only generates decks; PPTX export is not supported",
                "recoverable": False,
                "blocking": True,
                "error_info": {
                    "code": "unsupported_action",
                    "message": f"unsupported research-pptx action: {action}",
                    "retryable": False,
                    "workflow_recoverable": False,
                },
            }
        # A new deck needs at least one source: topic, an outline, a markdown
        # source, a PDF, or inline reference text. resume_token continues a
        # paused review. In a workflow step, upstream results (depends_on_results
        # / workflow_results) are a valid source too — they are absorbed into
        # reference_text inside execute(), but validate_params runs BEFORE that,
        # so we must recognise them here or the step fails as missing_input.
        _has_source = _has_presentation_source(args)
        _has_upstream = bool(
            (isinstance(args.get("depends_on_results"), dict) and args["depends_on_results"])
            or (isinstance(args.get("workflow_results"), dict) and args["workflow_results"])
        )
        if not _has_source and not _has_upstream and not args.get("resume_token"):
            return {
                "error": (
                    "Provide a source: topic, outline, markdown_uri, pdf_uri, "
                    "paper_uris, file_uris, reference_text, corpus_query, or "
                    "source_ids (or resume_token to continue a review)."
                ),
                "recoverable": False, "blocking": True,
                "error_info": {"code": "missing_input",
                               "message": "no source or resume_token provided",
                               "retryable": False, "workflow_recoverable": False},
            }

        # Early, actionable feedback for structured paths, including paths
        # extracted from a natural-language request.
        for field, example, detail in (
            (
                "pdf_uri",
                "S:/paper.pdf",
                "Do NOT read the PDF manually; this skill parses it internally.",
            ),
            ("markdown_uri", "S:/outline.md", ""),
            ("template_uri", "S:/template.pptx", ""),
        ):
            uri = str(args.get(field, "")).strip()
            if not uri or uri.startswith("artifact://"):
                continue
            path = Path(uri.removeprefix("file://")).expanduser()
            if not path.is_file():
                suffix = f" {detail}" if detail else ""
                return {
                    "error": (
                        f"{field} '{uri}' was not found. Pass the correct absolute "
                        f"path (e.g. {example}) or an artifact:// uri.{suffix}"
                    ),
                    "recoverable": True, "blocking": False,
                    "error_info": {"code": f"{field.removesuffix('_uri')}_not_found",
                                   "message": f"{field} not found: {uri}",
                                   "retryable": True, "workflow_recoverable": True},
                }
        # Normalize common LLM-planner aliases BEFORE validating enums, so a planner
        # that passes 'Chinese'/'conference talk' is accepted instead of failing
        # the whole workflow. Mirrors PresentationRequest.
        lang = args.get("language")
        if lang is not None:
            norm_lang = _normalize_language(lang)
            if norm_lang not in ("en", "zh"):
                return {
                    "error": (
                        f"language must be 'en' or 'zh' (got {lang!r}); "
                        "use 'zh' for Chinese, 'en' for English."
                    ),
                    "recoverable": True, "blocking": False,
                    "error_info": {"code": "invalid_enum", "field": "language",
                                   "retryable": True, "workflow_recoverable": True},
                }
            args["language"] = norm_lang  # normalize in place for downstream
        tt = args.get("talk_type")
        if tt is not None:
            norm_tt = _normalize_talk_type(tt)
            if norm_tt not in _VALID_TALKS:
                return {
                    "error": (
                        f"talk_type must be one of {_VALID_TALKS} (got {tt!r})."
                    ),
                    "recoverable": True, "blocking": False,
                    "error_info": {"code": "invalid_enum", "field": "talk_type",
                                   "retryable": True, "workflow_recoverable": True},
                }
            args["talk_type"] = norm_tt

        dur = args.get("duration_minutes")
        if dur is not None:
            try:
                if not (5 <= int(dur) <= 90):
                    return {"error": "duration_minutes must be 5-90"}
            except (TypeError, ValueError):
                return {"error": "duration_minutes must be an integer"}
        ts = args.get("target_slides")
        if ts is not None:
            try:
                if not (3 <= int(ts) <= 80):
                    return {"error": "target_slides must be 3-80"}
            except (TypeError, ValueError):
                return {"error": "target_slides must be an integer"}
        ct = args.get("color_theme")
        if ct is not None and ct not in _VALID_THEMES:
            return {"error": f"color_theme must be one of {_VALID_THEMES}"}
        return None

    # ── entry point ──
    async def execute(self, progress_callback: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        kwargs = _models.remap_common_alias_fields(kwargs)
        _extract_file_paths_from_topic(kwargs)
        # Belt-and-suspenders topic fallback (see validate_params): if the
        # framework passed the user text as input/query/workflow_goal, use it as
        # the deck topic so single-skill routing doesn't dead-end.
        if not str(kwargs.get("topic", "")).strip():
            _fb = str(kwargs.get("input") or kwargs.get("query")
                      or kwargs.get("workflow_goal") or "").strip()
            if _fb:
                kwargs["topic"] = _fb
                _extract_file_paths_from_topic(kwargs)
        # topic can be empty on the resume path; require topic XOR resume_token
        # here so PresentationRequest(topic="") no longer crashes.
        _has_source = _has_presentation_source(kwargs)
        _has_upstream = bool(
            (isinstance(kwargs.get("depends_on_results"), dict) and kwargs["depends_on_results"])
            or (isinstance(kwargs.get("workflow_results"), dict) and kwargs["workflow_results"])
        )
        if not _has_source and not _has_upstream and not kwargs.get("resume_token"):
            return {
                "status": "error",
                "error": (
                    "Provide a source: topic, outline, markdown_uri, pdf_uri, "
                    "paper_uris, file_uris, reference_text, corpus_query, or "
                    "source_ids (or resume_token)."
                ),
                "recoverable": False, "blocking": True,
                "error_info": {
                    "code": "missing_input",
                    "message": "no source or resume_token provided",
                    "retryable": False, "workflow_recoverable": False,
                },
            }
        # Defensive re-routing: if the model pasted an export/review intent into
        # topic instead of setting the real fields, correct or ask instead of
        # generating the wrong deck.
        _routing = self._detect_misrouted_intent(kwargs)
        if _routing is not None:
            return _routing

        # Absorb upstream workflow results (dropped by extra='ignore') as a
        # source. A "search then make a deck" chain must feed the deck even when
        # the user also gave a topic: merge upstream into reference_text so the
        # planner sees the retrieved papers, not just the raw user sentence.
        _upstream = self._extract_upstream_source(kwargs)
        if _upstream:
            _has_rich_source = any(
                str(kwargs.get(k, "")).strip()
                for k in ("pdf_uri", "markdown_uri", "outline")
            )
            if not _has_rich_source:
                existing = str(kwargs.get("reference_text", "") or "").strip()
                kwargs["reference_text"] = (
                    f"{existing}\n\n{_upstream}".strip() if existing else _upstream
                )

        _in_workflow = bool(
            getattr(self.ctx, "workflow_step_id", "")
            or kwargs.get("workflow_step_id")
            or kwargs.get("workflow_task_id")
        )
        if _in_workflow and str(kwargs.get("review_mode", "none")) != "none":
            logger.info("research-pptx: forcing review_mode=none inside workflow step")
            kwargs["review_mode"] = "none"
        req = PresentationRequest(**kwargs)
        result = await self._run(req, progress_callback)

        output = dict(result) if isinstance(result, dict) else result.model_dump()
        if output.get("pptx_uri") or output.get("artifacts"):
            output["deliverable_assessment"] = _pptx_assessment(
                self.ctx,
                kwargs,
                req=req,
                result=output,
            )
        return output

    async def _record_provenance(self, plan, pptx_uri, content, req) -> dict:
        """Best-effort ROM provenance: a render run + source for the PDF."""
        ctx = self.ctx
        if ctx is None or getattr(ctx, "db", None) is None:
            return {"source_ids": [], "run_id": ""}
        try:
            from omni.research import ResearchStore, capture_env_lock

            store = ResearchStore(ctx.db)
            source_ids: list[str] = []
            if content.source_type == "pdf" and plan.title:
                src = await store.add_source(
                    {"title": plan.title, "authors": plan.authors,
                     "venue": plan.venue, "kind": "paper"},
                    origin="research-pptx",
                )
                source_ids.append(src.id)
            run = await store.add_run(
                title=f"Generate slides: {plan.title}",
                session_id=getattr(ctx, "session_id", ""),
                subtask_id=getattr(ctx, "subtask_id", "") or getattr(ctx, "task_id", ""),
                cmd="research-pptx engine (PptxGenJS)",
                env_lock=capture_env_lock(),
                output_uris=[pptx_uri],
                metrics={
                    "slides": len(plan.slides),
                    "figures_used": sum(
                        1 for s in plan.slides
                        if s.figure_path and os.path.exists(s.figure_path)
                    ),
                    "talk_type": req.talk_type,
                    "language": req.language,
                    "duration_minutes": req.duration_minutes,
                },
                status="succeeded",
            )
            return {"source_ids": source_ids, "run_id": run.id}
        except Exception as exc:  # noqa: BLE001
            return {"source_ids": [], "run_id": "", "error": str(exc)}

    # ── clients from ctx (local-first) ──
    def _llm(self) -> Any:
        llm = getattr(self.ctx, "llm", None)
        if llm is None:
            raise RuntimeError("no LLM client available in ExecContext")
        return llm

    async def _store_pptx(self, pptx_path: str, title: str) -> dict[str, Any]:
        local_path = Path(pptx_path).resolve()
        mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        artifacts = getattr(self.ctx, "artifacts", None)
        if artifacts is None:
            return {
                "title": title,
                "format": "pptx",
                "uri": f"file://{local_path}",
                "path": str(local_path),
                "mime": mime,
                "size_bytes": local_path.stat().st_size,
            }
        data = local_path.read_bytes()
        stored = await artifacts.put_bytes(
            data, kind="presentation", title=title, ext="pptx",
            mime=mime,
            session_id=getattr(self.ctx, "session_id", ""),
            task_id=getattr(self.ctx, "task_id", ""),
            subtask_id=getattr(self.ctx, "subtask_id", ""),
            workflow_run_id=getattr(self.ctx, "workflow_run_id", ""),
        )
        return {
            "title": title,
            "format": "pptx",
            "uri": stored.uri,
            "path": str(stored.path),
            "mime": stored.mime,
            "size_bytes": stored.size_bytes,
        }

    async def _persist_review_state(
        self,
        token: str,
        plan: PresentationPlan,
        content: ParsedContent,
        work_dir: str,
        *,
        request: PresentationRequest,
    ) -> dict[str, Any]:
        """Copy resolved figures to a durable dir + write a review-state file so a
        cross-process resume can rebuild the deck with all visuals intact."""
        request_dict = request.model_dump(
            exclude={"approved_plan", "plan_edits", "resume_token"}
        )
        paths = getattr(self.ctx, "paths", None)
        if paths is None or getattr(paths, "artifacts_dir", None) is None:
            # No durable store: best-effort, same-process resume only.
            return {
                "content": content.model_dump(),
                "request": request_dict,
                "state_uri": "",
            }

        review_dir = Path(paths.artifacts_dir) / "pptx_review" / token
        fig_dir = review_dir / "figures"
        fig_dir.mkdir(parents=True, exist_ok=True)

        # Copy each figure out of the soon-to-be-deleted work_dir.
        new_figs: list[dict[str, str]] = []
        for i, f in enumerate(content.figures):
            src = f.get("path", "")
            if src and os.path.exists(src):
                dst = fig_dir / f"fig_{i}{Path(src).suffix or '.png'}"
                try:
                    shutil.copy2(src, dst)
                    nf = dict(f)
                    nf["path"] = str(dst)
                    new_figs.append(nf)
                    continue
                except Exception:  # noqa: BLE001
                    pass
            new_figs.append(dict(f))

        content_dict = content.model_dump()
        content_dict["figures"] = new_figs

        plan_dict = plan.model_dump()
        # Preserve template master assets (background/logo images) across the
        # work_dir cleanup so a cross-process resume still renders the branding.
        tpl = plan_dict.get("template_master") or {}
        tpl_assets = review_dir / "template_assets"
        for key in ("background_image",):
            src = tpl.get(key, "")
            if src and os.path.exists(src):
                tpl_assets.mkdir(parents=True, exist_ok=True)
                dst = tpl_assets / Path(src).name
                try:
                    shutil.copy2(src, dst)
                    tpl[key] = str(dst)
                except Exception:  # noqa: BLE001
                    pass
        logo = tpl.get("logo")
        if isinstance(logo, dict) and logo.get("path") and os.path.exists(logo["path"]):
            tpl_assets.mkdir(parents=True, exist_ok=True)
            dst = tpl_assets / Path(logo["path"]).name
            try:
                shutil.copy2(logo["path"], dst)
                logo["path"] = str(dst)
            except Exception:  # noqa: BLE001
                pass
        plan_dict["template_master"] = tpl

        # persist the template PPTX itself so a cross-process resume can still
        # reuse it as a render container.
        tpl_src = plan_dict.get("template_local_path", "")
        if tpl_src and os.path.exists(tpl_src):
            tpl_assets.mkdir(parents=True, exist_ok=True)
            dst = tpl_assets / Path(tpl_src).name
            try:
                shutil.copy2(tpl_src, dst)
                plan_dict["template_local_path"] = str(dst)
            except Exception:  # noqa: BLE001
                pass

        owner = {
            key: str(getattr(self.ctx, key, "") or "")
            for key in (
                "session_id",
                "task_id",
                "subtask_id",
                "workflow_run_id",
            )
        }
        state = {
            "plan": plan_dict,
            "content": content_dict,
            "request": request_dict,
            "owner": owner,
            "created_at": time.time(),
            "consumed_at": None,
        }
        state_path = review_dir / "state.json"
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return {
            "content": content_dict,
            "request": request_dict,
            "state_uri": f"file://{state_path}",
            "plan": plan_dict,
        }

    async def _resolve_to_local(self, uri: str, work_dir: str, suffix: str) -> str | None:
        """Resolve an artifact:// or local uri to a real file (generic)."""
        if not uri:
            return None
        artifacts = getattr(self.ctx, "artifacts", None)
        if uri.startswith("artifact://") and artifacts is not None:
            src = await artifacts.resolve_path(uri)
            if src is None:
                return None
            dst = os.path.join(work_dir, f"source{suffix}")
            shutil.copy2(str(src), dst)
            return dst
        p = Path(uri.replace("file://", "")).expanduser()
        return str(p) if p.is_file() else None

    async def _store_file(self, path: str, *, kind: str, title: str, ext: str, mime: str) -> str:
        artifacts = getattr(self.ctx, "artifacts", None)
        if artifacts is None:
            return f"file://{path}"
        with open(path, "rb") as f:
            data = f.read()
        stored = await artifacts.put_bytes(
            data, kind=kind, title=title, ext=ext, mime=mime,
            session_id=getattr(self.ctx, "session_id", ""),
            task_id=getattr(self.ctx, "task_id", ""),
        )
        return stored.uri

    # ── Feature 1: template — theme + master (logo/background/geometry) ──
    async def _apply_template_theme(self, req, plan, work_dir: str) -> bool:
        local = await self._resolve_to_local(req.template_uri or "", work_dir, ".pptx")
        if not local:
            return False
        # Remember the resolved template path so the render step can REUSE the
        # template as a container (true reuse), not just adopt its theme.
        plan.template_local_path = local

        tpl_dir = Path(work_dir) / "template_assets"
        theme = await asyncio.to_thread(_extract_pptx_template, local, str(tpl_dir))
        if not theme:
            return False

        # Optional LLM refinement of layout role assignments. The engine's
        # XML extractor + template_backend's heuristics both classify by
        # placeholder shape, which cannot distinguish "TOC" from "content"
        # (both are TITLE + BODY). The LLM reads the layout NAMES (in any
        # language, including WPS's Chinese names) and refines the mapping.
        try:
            _tb = _load_sibling("template_backend")
            llm = self._llm()
        except Exception:
            _tb, llm = None, None
        if _tb is not None and llm is not None and theme.get("layouts"):
            try:
                refined = await _tb.classify_layouts_with_llm(
                    llm, theme["layouts"],
                )
            except Exception:  # noqa: BLE001 — refinement is best-effort
                refined = {}
            if refined:
                theme["layout_roles"] = {**theme["layout_roles"], **refined}
                logger.info(
                    "[template] LLM refined layout roles: %s", refined,
                )

        # Theme colours & fonts
        if theme.get("colors"):
            plan.color_theme = {**plan.color_theme, **theme["colors"]}
        if theme.get("header_font"):
            plan.header_font = theme["header_font"]
        if theme.get("body_font"):
            plan.body_font = theme["body_font"]
        if theme.get("master"):
            plan.template_master = theme["master"]

        # Store layout metadata so downstream renderers (PptxGenJS fallback or
        # future cached paths) don't need to re-parse the PPTX.
        plan.template_master["layouts"] = theme.get("layouts", [])
        plan.template_master["layout_roles"] = theme.get("layout_roles", {})

        return True


    async def _resolve_pdf(self, pdf_uri: str, work_dir: str) -> str | None:
        """Resolve a PDF uri (artifact:// or local path) to a local file."""
        if not pdf_uri:
            return None
        artifacts = getattr(self.ctx, "artifacts", None)
        if pdf_uri.startswith("artifact://") and artifacts is not None:
            # ArtifactStore exposes resolve_path(), not get_bytes().
            src = await artifacts.resolve_path(pdf_uri)
            if src is None:
                return None
            dst = os.path.join(work_dir, "source.pdf")
            shutil.copy2(str(src), dst)
            return dst
        p = Path(pdf_uri.replace("file://", "")).expanduser()
        return str(p) if p.is_file() else None

    def _load_review_state(self, token: str) -> dict | None:
        paths = getattr(self.ctx, "paths", None)
        if paths is None or getattr(paths, "artifacts_dir", None) is None:
            return None
        state_path = Path(paths.artifacts_dir) / "pptx_review" / token / "state.json"
        if not state_path.is_file():
            return None
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            return None if state.get("consumed_at") else state
        except Exception:  # noqa: BLE001
            return None

    def _find_recent_review_token(self) -> str | None:
        """Return the sole active review token owned by the current session.

        Automatic recovery is deliberately fail-closed: legacy unowned state,
        cross-session state, consumed checkpoints, stale checkpoints, and more
        than one active candidate all require an explicit ``resume_token``.
        """
        paths = getattr(self.ctx, "paths", None)
        if paths is None or getattr(paths, "artifacts_dir", None) is None:
            return None
        session_id = str(getattr(self.ctx, "session_id", "") or "")
        if not session_id:
            return None
        review_root = Path(paths.artifacts_dir) / "pptx_review"
        if not review_root.is_dir():
            return None
        cutoff = time.time() - 86400  # 24-hour expiry
        candidates: list[str] = []
        for child in review_root.iterdir():
            if not child.is_dir():
                continue
            state_file = child / "state.json"
            if not state_file.is_file():
                continue
            mtime = state_file.stat().st_mtime
            if mtime < cutoff:
                continue
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            owner = state.get("owner")
            if not isinstance(owner, dict):
                continue
            if str(owner.get("session_id") or "") != session_id:
                continue
            if state.get("consumed_at"):
                continue
            candidates.append(child.name)
        return candidates[0] if len(candidates) == 1 else None

    def _mark_review_consumed(self, token: str) -> None:
        """Mark a durable review checkpoint consumed after a successful render."""

        _REVIEW_CACHE.pop(token, None)
        paths = getattr(self.ctx, "paths", None)
        if paths is None or getattr(paths, "artifacts_dir", None) is None:
            return
        state_path = Path(paths.artifacts_dir) / "pptx_review" / token / "state.json"
        if not state_path.is_file():
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["consumed_at"] = time.time()
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except (OSError, json.JSONDecodeError):
            logger.debug("Unable to mark review token consumed", exc_info=True)

    def _detect_misrouted_intent(
        self, kwargs: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Catch intents the planner pasted into topic instead of input fields.

        Returns a recoverable guidance result (so the model re-calls correctly),
        or None when the request looks well-formed.

        Also *mutates* kwargs in-place to set review_mode, target_slides, and
        resume_token when they are clearly implied by the topic text but not
        explicitly set. This catches common patterns the Omni planner often
        fails to route correctly:

        - "let me check outline first" → review_mode="plan"
        - "15-page deck" → target_slides=15
        - approval/generate-after-review phrases -> auto-resume
        """
        topic = str(kwargs.get("topic", "") or "")
        low = topic.lower()
        has_resume = bool(str(kwargs.get("resume_token", "") or "").strip())

        # Extract the page constraint before any review-routing early return so a
        # combined request such as "15 slides; show the outline first" keeps both
        # pieces of intent.
        slide_match = re.search(
            r"(\d{1,2})\s*(?:-?\s*(?:page|slide|\u9875|\u30da\u30fc\u30b8|p)\w*)",
            topic,
            flags=re.IGNORECASE,
        )
        if slide_match and not kwargs.get("target_slides"):
            count = int(slide_match.group(1))
            if 3 <= count <= 80:
                kwargs["target_slides"] = count

        # ── Auto-resume: detect approve / generate-from-outline intents ──
        # When the user says "approve" or "generate the PPT based on the outline"
        # but the Omni planner didn't pass resume_token, find the most recent
        # review state and inject the token so the skill resumes correctly.
        _resume_patterns = (
            "approve this outline",
            "approve the outline",
            "outline is approved",
            "render this outline",
            "render the approved outline",
            "generate the ppt based on this outline",
            "generate the slides based on this outline",
            "\u6279\u51c6\u5927\u7eb2",
            "\u786e\u8ba4\u5927\u7eb2\u5e76\u751f\u6210",
            "\u6839\u636e\u5927\u7eb2",
            "\u6839\u636e\u4fee\u6539",
            "\u6309\u5927\u7eb2",
            "\u6309\u4fee\u6539",
            "\u57fa\u4e8e\u5927\u7eb2",
        )
        exact_approval = low.strip(" ，,。.；;：:") in {
            "approve",
            "approved",
            "\u6279\u51c6",
            "\u540c\u610f",
        }
        matched_resume = (
            "approve"
            if exact_approval
            else next((pattern for pattern in _resume_patterns if pattern in low), "")
        )
        if not has_resume and matched_resume:
            token = self._find_recent_review_token()
            if token:
                kwargs["resume_token"] = token
                logger.info(
                    "[intent] Auto-resume detected from topic "
                    "(pattern=%r, token=%s)", matched_resume, token[:8]
                )
                return None
            return {
                "status": "error",
                "outcome": {"code": "review_pending"},
                "error": (
                    "No unique active outline checkpoint was found. Pass the "
                    "resume_token returned by the outline review before rendering."
                ),
                "recoverable": True,
                "blocking": False,
                "error_info": {
                    "code": "review_pending",
                    "retryable": True,
                    "workflow_recoverable": True,
                },
            }

        # Phrases that the user might type to request outline review.
        # Extended with more patterns the Omni planner misses in practice.
        review_directives = (
            "review_mode=plan",
            "review the outline",
            "show the outline first",
            "approve before",
            "let me review",
            "let me check",
            "outline first",
            "check outline",
            "check the outline",
            "review outline",
            "approve outline",
            "approve the outline",
            "outline review",
            "outline for review",
            "check first",
            "review first",
            "review before",
            # Chinese patterns (same intent, different language)
            "\u5148\u770b\u5927\u7eb2",
            "\u5148\u770b\u4e0b\u5927\u7eb2",
            "\u5148\u770b\u4e00\u4e0b\u5927\u7eb2",
            "\u5ba1\u6838\u5927\u7eb2",
            "\u6279\u51c6\u5927\u7eb2",
            "\u5148\u5ba1\u9605",
            "\u68c0\u67e5\u5927\u7eb2",
            "\u786e\u8ba4\u5927\u7eb2",
        )

        review_mode = str(kwargs.get("review_mode", "none")).lower()
        if review_mode != "none":
            return None  # already explicitly set, don't override

        # If any directive appears in the topic, flip review_mode and clean it out
        for directive in review_directives:
            if directive in low:
                kwargs["review_mode"] = "plan"
                # Remove the directive itself and any adjacent punctuation/spaces
                cleaned = re.sub(
                    r"[，,。.；;：:\s]*?" + re.escape(directive) + r"[，,。.；;：:\s]*",
                    "",
                    topic,
                    flags=re.IGNORECASE,
                ).strip()
                # Fall back to the original if cleaning emptied it entirely
                kwargs["topic"] = cleaned or topic
                return None

        return None

    # ── main pipeline ──
    async def _run(
        self, req: PresentationRequest, progress_callback: Any | None
    ) -> PresentationResult | dict[str, Any]:
        t0 = time.time()
        tel = build_telemetry(self.ctx)
        work_dir = tempfile.mkdtemp(prefix="research_pptx_")
        last_ts = t0

        def _ms() -> int:
            nonlocal last_ts
            now = time.time()
            d = int((now - last_ts) * 1000)
            last_ts = now
            return d

        async def _progress(message: str, fraction: float) -> None:
            """Emit stable stage ids to Omni and localized text to simple hosts."""
            if progress_callback:
                if fraction <= 0.18:
                    stage = "parsing"
                elif fraction <= 0.22:
                    stage = "deciding"
                elif fraction <= 0.32:
                    stage = "planning"
                elif fraction <= 0.60:
                    stage = "rendering"
                elif fraction <= 0.75:
                    stage = "qa"
                elif fraction <= 0.85:
                    stage = "critique"
                else:
                    stage = "upload"
                try:
                    await progress_callback(stage, fraction)
                except TypeError:
                    await progress_callback(message)

        tel.audit("started", topic=req.topic, mode=req.mode, review_mode=req.review_mode)

        try:
            # ── Resume path: render a previously-reviewed plan ──
            if req.resume_token:
                cached = _REVIEW_CACHE.get(req.resume_token)
                if cached is None:
                    cached = self._load_review_state(req.resume_token)
                cached_plan = (cached or {}).get("plan")
                cached_request = (cached or {}).get("request")
                if isinstance(cached_request, dict):
                    continuation = req
                    restored_request = dict(cached_request)
                    restored_request.update(
                        {
                            "resume_token": continuation.resume_token,
                            "approved_plan": continuation.approved_plan,
                            "plan_edits": continuation.plan_edits,
                        }
                    )
                    if continuation.target_slides is not None:
                        restored_request["target_slides"] = continuation.target_slides
                    req = PresentationRequest(**restored_request)
                approved_plan = (
                    req.approved_plan
                    if isinstance(req.approved_plan, dict)
                    and req.approved_plan.get("slides")
                    else None
                )
                if cached_plan is None and approved_plan is None:
                    return {
                        "status": "error",
                        "outcome": {"code": "review_pending"},
                        "error": (
                            "unknown or expired resume_token; pass a complete "
                            "approved_plan to recover"
                        ),
                        "recoverable": True,
                        "error_info": {"code": "review_pending", "retryable": True,
                                       "workflow_recoverable": True},
                    }

                # ── Build the plan to render ──
                # Priority (highest first):
                #  1. approved_plan (full replacement, user- or model-edited)
                #  2. plan_edits applied on top of the cached original (surgical edits)
                #  3. cached plan as-is (plain approve)
                plan_edits = req.plan_edits or []
                plan_dict: dict[str, Any] | None = None
                edit_source = "unchanged"

                if approved_plan is not None:
                    # Validate: an approved_plan MUST have a non-empty slides array.
                    plan_dict = approved_plan
                    edit_source = "approved_plan"
                elif req.approved_plan and isinstance(req.approved_plan, dict):
                    if cached_plan is not None:
                        # Truncated / incomplete approved_plan — merge with cached
                        # and warn so the model can learn the right shape.
                        logger.warning(
                            "approved_plan missing 'slides' — merging with cached plan"
                        )
                        merged = dict(cached_plan)
                        merged.update(req.approved_plan)
                        plan_dict = merged
                        edit_source = "approved_plan_merged"

                if plan_edits:
                    # Apply surgical edits on top of whatever plan_dict we have
                    # (or the cached original if no approved_plan was provided).
                    if plan_dict is None and cached_plan is None:
                        return {
                            "status": "error",
                            "outcome": {"code": "review_pending"},
                            "error": "plan_edits require the original review checkpoint",
                            "recoverable": True,
                            "error_info": {
                                "code": "review_pending",
                                "retryable": True,
                                "workflow_recoverable": True,
                            },
                        }
                    base = plan_dict if plan_dict is not None else dict(cached_plan)
                    try:
                        plan_dict = self._apply_plan_edits(base, plan_edits)
                    except ValueError as exc:
                        return {
                            "status": "error",
                            "outcome": {"code": "invalid_plan_edits"},
                            "error": str(exc),
                            "recoverable": True,
                            "blocking": False,
                            "resume_token": req.resume_token,
                            "error_info": {
                                "code": "invalid_plan_edits",
                                "retryable": True,
                                "workflow_recoverable": True,
                            },
                        }
                    edit_source = (
                        "plan_edits"
                        if edit_source == "unchanged"
                        else f"approved_plan+{len(plan_edits)}_edits"
                    )

                if plan_dict is None:
                    plan_dict = dict(cached_plan)

                plan = PresentationPlan(**plan_dict)
                if (
                    req.target_slides is not None
                    and len(plan.slides) != req.target_slides
                ):
                    return _slide_count_mismatch_result(
                        target=req.target_slides,
                        actual=len(plan.slides),
                        phase="review_resume",
                        resume_token=req.resume_token,
                    )
                original = cached_plan or {}
                edited = edit_source != "unchanged"
                n_edits = len(plan_edits) if plan_edits else 0

                # structured decision event for "user reviewed the outline".
                tel.decision(
                    "outline_review",
                    options=["approve_unchanged", "approve_edited"],
                    chosen="approve_edited" if edited else "approve_unchanged",
                    decided_by="user",
                    requires_review=True,
                    rationale="resumed from review checkpoint",
                    extra={"token": req.resume_token,
                           "edit_source": edit_source,
                           "n_edits": n_edits,
                           "original_slides": len(original.get("slides", [])),
                           "final_slides": len(plan.slides)},
                )
                tel.audit(
                    "review_resumed", token=req.resume_token,
                    user_edited_plan=edited,
                    edit_source=edit_source,
                    n_edits=n_edits,
                    original_slides=len(original.get("slides", [])),
                    final_slides=len(plan.slides),
                )
                cached_content = (cached or {}).get("content")
                if cached_content is not None:
                    _cache_content_for_render(work_dir, ParsedContent(**cached_content))
                tel.stage("pptx_resumed", input_summary={"token": req.resume_token})
                await _progress(
                    _l10n(req.language, "progress.resuming_render") or "Resuming slide render...",
                    0.25,
                )
                result = await self._render_and_finish(plan, work_dir, req, tel, _progress, t0, _ms)
                if not isinstance(result, dict) and result.status == "ok":
                    self._mark_review_consumed(req.resume_token)
                return result

            # ── Step 1: parse ──
            await _progress(
                _l10n(req.language, "progress.processing_inputs") or "Processing inputs...",
                0.05,
            )
            content = await self._process_inputs(req, work_dir, _progress)
            tel.stage("pptx_input_parsed",
                      output_summary={"source_type": content.source_type,
                                      "n_figures": len(content.figures),
                                      "n_tables": len(content.tables),
                                      "chars": len(content.markdown_text)},
                      latency_ms=_ms())
            await _progress(
                _l10n(req.language, "progress.source_done") or "Source parsing done",
                0.18,
            )

            # ── Step 1.5: agentic strategy decision ──
            # In agentic mode the LLM decides the deck strategy (slide mix,
            # whether a human checkpoint is warranted) instead of a fixed
            # pipeline. auto mode keeps the deterministic path.
            effective_review = req.review_mode
            strategy: dict[str, Any] | None = None
            if req.mode == "agentic":
                _cp = _load_sibling("content_planner")
                await _progress(
                    _l10n(req.language, "progress.deciding_strategy") or "Deciding presentation strategy...",
                    0.20,
                )
                strategy = await _cp.decide_presentation_strategy(
                    self._llm(), content, req
                )
                recommend_review = bool(strategy.get("recommend_review"))
                tel.decision(
                    "deck_strategy",
                    options=["auto_render", "human_review"],
                    chosen="human_review" if recommend_review else "auto_render",
                    decided_by="llm",
                    requires_review=recommend_review,
                    rationale=str(strategy.get("review_reason") or strategy.get("emphasis") or ""),
                    extra={"slide_mix": strategy.get("slide_mix", {}),
                           "risks": strategy.get("risks", [])},
                )
                tel.stage("pptx_strategy_decided",
                          output_summary={"target_slides": strategy.get("target_slides"),
                                          "recommend_review": recommend_review},
                          latency_ms=_ms())

            # ── Step 2: plan (auto | agentic) ──
            plan_presentation = _load_sibling("content_planner").plan_presentation
            await _progress(
                _l10n(req.language, "progress.planning_structure") or "Planning slide structure...",
                0.25,
            )
            plan = await plan_presentation(self._llm(), content, req)
            tel.stage("pptx_plan_built",
                      output_summary={"title": plan.title[:120], "n_slides": len(plan.slides)},
                      latency_ms=_ms())
            await _progress(
                _l10n(req.language, "progress.structure_ready") or "Slide structure ready",
                0.32,
            )

            # ── Feature 1: adopt colours + fonts from a user PPTX template ──
            if req.template_uri:
                applied = await self._apply_template_theme(req, plan, work_dir)
                tel.decision(
                    "template_theme",
                    options=["default_theme", "template_theme"],
                    chosen="template_theme" if applied else "default_theme",
                    decided_by="user" if applied else "default",
                    requires_review=False,
                    rationale="user provided template_uri" if applied
                    else "template unreadable; kept default theme",
                    extra={"header_font": plan.header_font, "body_font": plan.body_font},
                )
                tel.stage("pptx_template_applied",
                          output_summary={"applied": applied}, latency_ms=_ms())

            # ── Review checkpoint (human-in-the-loop) ──
            if effective_review in ("plan", "interactive"):
                token = uuid.uuid4().hex[:16]
                persisted = await self._persist_review_state(
                    token,
                    plan,
                    content,
                    work_dir,
                    request=req,
                )
                _REVIEW_CACHE[token] = {
                    # Use the persisted plan so template asset paths point at the
                    # durable review dir (survive work_dir cleanup + cross-process
                    # resume), staying consistent with state.json on disk.
                    "plan": persisted.get("plan") or plan.model_dump(),
                    "content": persisted["content"],
                    "request": persisted["request"],
                    "state_uri": persisted["state_uri"],
                }
                # emit the review checkpoint as a decision event too.
                tel.decision(
                    "review_checkpoint_emitted",
                    options=["awaiting_user"],
                    chosen="awaiting_user",
                    decided_by="llm" if req.mode == "agentic" else "default",
                    requires_review=True,
                    rationale=str((strategy or {}).get("review_reason") or ""),
                    extra={"token": token, "n_slides": len(plan.slides),
                           "n_figs": len(content.figures)},
                )
                tel.audit("review_checkpoint", token=token,
                          n_slides=len(plan.slides), n_figs=len(content.figures))

                # Human-friendly outline as a Markdown artifact.
                outline_lines = [f"# Presentation outline for review: {plan.title}\n"]
                if strategy:
                    outline_lines.append(
                        f"> Strategy — emphasis: {strategy.get('emphasis', '')}; "
                        f"target slides: {strategy.get('target_slides', len(plan.slides))}."
                    )
                    if strategy.get("risks"):
                        outline_lines.append(
                            "> Risks: " + "; ".join(strategy.get("risks", []))
                        )
                    outline_lines.append("")
                for i, s in enumerate(plan.slides):
                    outline_lines.append(f"## {i + 1}. [{s.slide_type}] {s.title}")
                    if s.bullets:
                        outline_lines.append("\n".join(f"  - {b}" for b in s.bullets))
                    if s.figure_path:
                        outline_lines.append("  - 📸 figure assigned")
                    outline_lines.append("")
                outline_md = "\n".join(outline_lines)

                artifacts = getattr(self.ctx, "artifacts", None)
                doc_uri = ""
                if artifacts:
                    try:
                        stored = await artifacts.put_bytes(
                            outline_md.encode("utf-8"), kind="report",
                            title=f"PPT_outline_{self._sanitize_filename(plan.title, 20)}",
                            ext="md", mime="text/markdown",
                            session_id=getattr(self.ctx, "session_id", ""),
                            task_id=getattr(self.ctx, "task_id", ""),
                        )
                        doc_uri = stored.uri
                    except Exception:
                        pass

                tel.stage("pptx_awaiting_review", output_summary={"token": token})

                # ── Build the review checkpoint response ──
                _await_prompt = "Outline approval required"
                _await_state = "Awaiting outline approval"
                _opt_enter = "Approve & render"
                _opt_edit = "Edit outline"
                _opt_cancel = "Cancel"

                # Detect slide-count mismatch so the REPL model can flag it.
                _target = req.target_slides
                _actual = len(plan.slides)
                _count_mismatch = _target is not None and _actual != _target
                _count_note = ""
                if _count_mismatch:
                    _diff = _actual - _target
                    _word = "more" if _diff > 0 else "fewer"
                    _count_note = (
                        f" NOTE: the plan has {_actual} slides ({abs(_diff)} {_word} "
                        f"than the requested {_target}). You may trim via plan_edits "
                        f"(remove_slide) or merge slides before approving."
                    )

                # Include the exact typed continuation in the user-visible
                # checkpoint; the host confirmation flow can submit it verbatim.
                _resume_call = (
                    f'run_skill(skill_name="research-pptx", mode="foreground", '
                    f'input={{"resume_token":"{token}"}})'
                )
                _summary = (
                    f"Drafted a {_actual}-slide outline for review"
                    + (_count_note if _count_mismatch else "")
                    + ". Review the outline below.\n\n"
                    + outline_md
                    + "\n\n"
                    + f"To approve and render: call {_resume_call}. "
                    + "To edit: pass plan_edits with the same resume_token."
                )
                _omni_msg = (
                    f"Drafted {_actual}-slide outline"
                    + (f" (requested {_target})" if _count_mismatch else "")
                    + f" — on approval call {_resume_call}."
                )

                # Plan-edits operations the model can use for surgical edits,
                # avoiding the need to reconstruct the full plan JSON.
                _plan_edits_guide = {
                    "indexing": (
                        "slide_index is 0‑based — outline slide N → "
                        "slide_index N-1. Always subtract 1 from the number "
                        "the user sees in the outline."
                    ),
                    "description": (
                        "For surgical edits, use plan_edits instead of "
                        "approved_plan. Each edit is one op: set_title, "
                        "set_bullets, set_bullet, remove_slide, add_slide, "
                        "set_figure, set_type, swap_slides, move_slide."
                    ),
                    "examples": [
                        {"action": "set_title", "slide_index": 2,
                         "title": "New title text"},
                        {"action": "set_bullet", "slide_index": 3,
                         "bullet_index": 0, "text": "Updated bullet"},
                        {"action": "remove_slide", "slide_index": 4},
                        {"action": "add_slide", "after_index": 2,
                         "slide": {"slide_type": "content",
                                   "title": "New Slide",
                                   "bullets": ["point 1", "point 2"]}},
                        {"action": "set_figure", "slide_index": 5,
                         "figure_path": "figure_2"},
                        {"action": "swap_slides", "index_a": 2, "index_b": 5},
                    ],
                    "merge_two_slides": (
                        "To merge slide A into slide B: use set_bullets on "
                        "slide B to combine bullets from both, then "
                        "remove_slide on slide A. Example — merge outline "
                        "slide 4 into slide 3: "
                        '[{"action":"set_bullets","slide_index":2,'
                        '"bullets":["merged bullet 1","merged bullet 2","..."]},'
                        '{"action":"remove_slide","slide_index":3}]'
                    ),
                    "delete_slide_and_renumber": (
                        "To delete a slide: remove_slide on its 0‑based index. "
                        "To delete outline slide N, use slide_index N-1."
                    ),
                }

                return {
                    "status": "partial",
                    "outcome": {"code": "awaiting_review"},
                    "phase": "awaiting_review",
                    "recoverable": True,
                    "blocking": False,
                    "resume_token": token,
                    "target_slides": _target,
                    "actual_slides": _actual,
                    "slide_count_mismatch": _count_mismatch,
                    "awaiting_action": {
                        "prompt": _await_prompt,
                        "options": [
                            {
                                "key": "enter",
                                "label": _opt_enter,
                                "tool": "run_skill",
                                "skill_name": "research-pptx",
                                "input": {"resume_token": token},
                            },
                            {
                                "key": "e",
                                "label": _opt_edit,
                                "tool": "run_skill",
                                "skill_name": "research-pptx",
                                "input": {"resume_token": token},
                            },
                            {"key": "c", "label": _opt_cancel},
                        ],
                        "state": _await_state,
                    },
                    "summary": _summary,
                    "report_uri": doc_uri,
                    # Full markdown outline — the REPL model MUST display this
                    # to the user verbatim. It is the primary review artifact.
                    "outline_text": outline_md,
                    "strategy": strategy or {},
                    # Surgical-edit guide so the model doesn't reconstruct the
                    # full plan JSON (error-prone for multi-turn editing).
                    "plan_edits_guide": _plan_edits_guide,
                    "next_action": {
                        "when": "after the user approves or edits the outline",
                        "tool": "run_skill",
                        "skill_name": "research-pptx",
                        "mode": "foreground",
                        "required_input": {
                            "resume_token": token,
                        },
                        "optional_input": {
                            "plan_edits": (
                                "[list of edit ops — see plan_edits_guide above. "
                                "Use this for surgical edits INSTEAD of approved_plan. "
                                "Pass approved_plan ONLY for full-plan replacement.]"
                            ),
                            "approved_plan": (
                                "<full plan JSON — only if the user provided "
                                "a complete replacement plan>"
                            ),
                        },
                        "note": (
                            "Prefer plan_edits for incremental changes. "
                            "Pass approved_plan only when replacing the entire plan."
                        ),
                    },
                    "_omni_control": {
                        "terminal": True,
                        "message": _omni_msg,
                        "display": "outline",
                        "approve_tool": "run_skill",
                        "approve_skill": "research-pptx",
                        "approve_input": {"resume_token": token},
                    },
                    "resume_instructions": {
                        "skill_name": "research-pptx",
                        "resume_token": token,
                        "approved_plan": persisted.get("plan") or plan.model_dump(),
                        "plan_edits_guide": _plan_edits_guide,
                    },
                    "plan": persisted.get("plan") or plan.model_dump(),
                    "outline": [
                        {"i": i, "type": s.slide_type, "title": s.title}
                        for i, s in enumerate(plan.slides)
                    ],
                }

            return await self._render_and_finish(
                plan, work_dir, req, tel, _progress, t0, _ms
            )

        except Exception as exc:  # noqa: BLE001
            missing_module = (
                str(exc.name or "").partition(".")[0]
                if isinstance(exc, ModuleNotFoundError)
                else ""
            )
            if missing_module in {"PIL", "fitz", "matplotlib", "pptx", "pymupdf4llm"}:
                return _python_dependency_error(exc)
            tel.audit("failed", error=str(exc)[:500])
            logger.exception("research_pptx failed")
            return PresentationResult(
                status="error",
                summary=f"Slide generation failed: {str(exc)[:200]}",
                title=req.topic[:120], pptx_uri="",
                error=str(exc)[:500],
                metadata={
                    "status": "error",
                    "error": str(exc),
                    "recoverable": True,
                    "error_info": {
                        "code": "render_pipeline_error",
                        "message": str(exc)[:300],
                        "retryable": True,
                        "workflow_recoverable": True,
                    },
                },
            )
        finally:
            if work_dir.startswith(tempfile.gettempdir()):
                shutil.rmtree(work_dir, ignore_errors=True)

    async def _render_and_finish(
            self, plan, work_dir, req, tel, _progress, t0, _ms
    ) -> PresentationResult | dict[str, Any]:
        if req.target_slides is not None and len(plan.slides) != req.target_slides:
            return _slide_count_mismatch_result(
                target=req.target_slides,
                actual=len(plan.slides),
                phase="pre_render",
                resume_token=req.resume_token,
            )
        _sr = _load_sibling("slide_renderer")
        node_runtime_dir = _omni_renderer_runtime_dir(self.ctx)
        setup_command = (
            "omni skills setup research-pptx"
            if node_runtime_dir is not None
            else "cd scripts && npm ci"
        )

        async def render_pptx(render_plan, render_work_dir):  # noqa: ANN001, ANN202
            return await _sr.render_pptx(
                render_plan,
                render_work_dir,
                node_runtime_dir=node_runtime_dir,
            )

        resolve_figure_paths = _sr.resolve_figure_paths

        content = _resolve_content_for_render_impl(work_dir)

        if content.equations:
            await _progress(
                _l10n(req.language, "progress.rendering_eq") or "Rendering equations",
                0.34,
            )
            await self._render_equations(content.equations, work_dir)
            tel.stage("pptx_equations_rendered",
                      input_summary={"n": len(content.equations)}, latency_ms=_ms())

        await _progress(
            _l10n(req.language, "progress.resolving_figs") or "Resolving figures",
            0.35,
        )
        plan = resolve_figure_paths(plan, content.figures, work_dir)
        # Auto-number figure / table captions so the audience can point at them
        # during Q&A. Idempotent — runs skip captions that already carry a
        # "Figure N" / "Table N" prefix (or their zh equivalents).
        _number_captions(plan, language=req.language)
        tel.stage("pptx_figures_resolved", latency_ms=_ms())

        sparse = self._check_sparse_slides(plan)
        if sparse:
            self._fix_sparse_slides(
                plan,
                sparse,
                target_slides=req.target_slides,
            )
            tel.stage("pptx_sparse_fixed", input_summary={"n": len(sparse)}, latency_ms=_ms())

        await _progress(
            _l10n(req.language, "progress.rendering_slides") or "Rendering slides",
            0.45,
        )

        # ── template-faithful path — reuse the user PPTX as a container ──
        # When a resolved template exists AND python-pptx can reuse its layouts,
        # inject slides into a copy of the template so master/theme/logo/layout are
        # preserved verbatim. Fall back to the PptxGenJS rebuild on any failure.
        pptx_path = None
        tpl_local = getattr(plan, "template_local_path", "")
        render_backend = "pptxgenjs"
        if tpl_local and os.path.exists(tpl_local):
            try:
                _tb = _load_sibling("template_backend")
                pptx_path = await _tb.render_pptx_from_template(plan, tpl_local, work_dir)
                if pptx_path:
                    render_backend = "template_reuse"
            except Exception as exc:
                logger.warning(
                    "template backend unavailable, falling back to pptxgenjs: %s",
                    exc, exc_info=True,
                )
                pptx_path = None
        if pptx_path is None:
            try:
                pptx_path = await render_pptx(plan, work_dir)
            except _sr.RendererDependencyError as exc:
                # Renderer availability is a render-stage concern. In
                # particular, outline review and a successful template-reuse
                # backend do not need Node or its pre-installed packages.
                return _renderer_preflight_error(exc, setup_command=setup_command)

        tel.stage("pptx_rendered",
                  output_summary={"bytes": os.path.getsize(pptx_path),
                                  "backend": render_backend}, latency_ms=_ms())
        tel.decision(
            "render_backend",
            options=["pptxgenjs", "template_reuse"],
            chosen=render_backend, decided_by="default", requires_review=False,
            rationale="reused template container" if render_backend == "template_reuse"
            else "no reusable template; rebuilt master",
        )

        await _progress(
            f"{_l10n(req.language, 'progress.render_done') or 'Slide render done'} · {len(plan.slides)}/{len(plan.slides)} "
            f"{_l10n(req.language, 'detail.pages') or 'pages'}",
            0.60,
        )

        # ── QA overflow loop (geometric, deterministic) ──
        await _progress(
            _l10n(req.language, "progress.quality_check") or "Quality check",
            0.65,
        )
        for qa_iter in range(2):
            overflow = await asyncio.to_thread(check_overflow_structured, pptx_path)
            critical = [o for o in overflow if o["severity"] == "critical"]
            if not critical and qa_iter > 0:
                break
            if critical or (qa_iter == 0 and overflow):
                fixes = self._apply_overflow_fixes(plan, overflow, iteration=qa_iter)
                if fixes:
                    # CHANGED: Re-render using the SAME backend that was used initially
                    if render_backend == "template_reuse":
                        _tb = _load_sibling("template_backend")
                        pptx_path = await _tb.render_pptx_from_template(plan, tpl_local, work_dir)
                    else:
                        pptx_path = await render_pptx(plan, work_dir)
                else:
                    break
            else:
                break
        warnings = await asyncio.to_thread(check_overflow_structured, pptx_path)
        _qa_detail = (
            _l10n(req.language, "detail.no_overflow") or "No overflow"
            if not warnings
            else (_l10n(req.language, "detail.warnings_fixed", count=len(warnings))
                  or f"{len(warnings)} warning(s) fixed")
        )
        await _progress(
            f"{_l10n(req.language, 'progress.quality_done') or 'Quality check done'} · {_qa_detail}",
            0.75,
        )

        # ── Text-domain structured visual critique (no multimodal model) ──
        # Translate the rendered deck into a *textual* layout report and let the
        # LLM catch figure/section mismatches, sparse pages, and figure↔bullet
        # inconsistencies it cannot fix geometrically. One bounded repair pass.
        await _progress(
            _l10n(req.language, "progress.visual_review") or "Visual review",
            0.78,
        )
        warnings_before = list(warnings)
        edits: list[dict[str, Any]] = []
        proposed = 0
        applied = 0
        try:
            report = _sr.build_layout_report(plan, warnings)
            edits = await self._critique_layout(report, content, req)
            proposed = len(edits or [])
            applied = self._apply_layout_edits(plan, edits) if edits else 0
            if applied:
                plan = resolve_figure_paths(plan, content.figures, work_dir)
                if render_backend == "template_reuse":
                    _tb = _load_sibling("template_backend")
                    pptx_path = await _tb.render_pptx_from_template(
                        plan, tpl_local, work_dir,
                    )
                else:
                    pptx_path = await render_pptx(plan, work_dir)
                # Re-check for overflow introduced by the critique edits
                # and run one bounded fix pass so the stored artifact is clean.
                warnings = await asyncio.to_thread(check_overflow_structured, pptx_path)
                if warnings:
                    crit = [w for w in warnings if w.get("severity") == "critical"]
                    if crit:
                        self._apply_overflow_fixes(plan, crit, iteration=1)
                        try:
                            if render_backend == "template_reuse":
                                pptx_path = await _tb.render_pptx_from_template(
                                    plan, tpl_local, work_dir,
                                )
                            else:
                                pptx_path = await render_pptx(plan, work_dir)
                            warnings = await asyncio.to_thread(check_overflow_structured, pptx_path)
                        except Exception:
                            pass  # best-effort; don't fail the whole render
                tel.decision(
                    "visual_critique",
                    options=["render_as_is", "apply_edits"],
                    chosen="apply_edits", decided_by="llm", requires_review=False,
                    rationale="structured layout report (text-domain visual feedback)",
                    extra={"n_edits": applied},
                )
            else:
                tel.decision(
                    "visual_critique",
                    options=["render_as_is", "apply_edits"],
                    chosen="render_as_is", decided_by="llm", requires_review=False,
                    rationale="no actionable layout edits",
                )
        except Exception as exc:  # noqa: BLE001 — critique is best-effort
            logger.debug("visual critique skipped: %s", exc)
            tel.decision(
                "visual_critique",
                options=["render_as_is", "apply_edits"],
                chosen="render_as_is", decided_by="default", requires_review=False,
                rationale=f"critique unavailable: {str(exc)[:120]}",
            )

        tel.stage(
            "pptx_visual_critique",
            input_summary={"warnings_before": len(warnings_before)},
            output_summary={"warnings_after": len(warnings),
                            "edits_proposed": proposed,
                            "edits_applied": applied},
            latency_ms=_ms(),
        )
        tel.stage("pptx_qa_done", output_summary={"remaining": len(warnings)}, latency_ms=_ms())
        _vr_detail = (
            _l10n(req.language, "detail.edits", count=applied) or f"{applied} edit(s)"
        )
        await _progress(
            f"{_l10n(req.language, 'progress.visual_done') or 'Visual review done'} · {_vr_detail}",
            0.85,
        )

        if req.target_slides is not None and len(plan.slides) != req.target_slides:
            return _slide_count_mismatch_result(
                target=req.target_slides,
                actual=len(plan.slides),
                phase="post_render",
                resume_token=req.resume_token,
            )

        # ── store artifact ──
        await _progress(
            _l10n(req.language, "progress.storing") or "Storing artifact",
            0.88,
        )
        pptx_artifact = await self._store_pptx(pptx_path, plan.title)
        pptx_uri = str(pptx_artifact["uri"])
        figures_used = sum(
            1 for s in plan.slides if s.figure_path and os.path.exists(s.figure_path)
        )
        tel.stage("pptx_stored", output_summary={"uri": pptx_uri}, latency_ms=_ms())

        research = await self._record_provenance(plan, pptx_uri, content, req)

        elapsed = round(time.time() - t0, 1)
        tel.audit(
            "completed",
            title=plan.title,
            slide_count=len(plan.slides),
            figures_used=figures_used,
            qa_warnings=len(warnings),
            duration_s=elapsed,
            pptx_uri=pptx_uri,
        )
        size_mb = round(os.path.getsize(pptx_path) / (1024 * 1024), 1)
        elapsed_str = f"{int(elapsed // 60)}:{int(elapsed % 60):02d}" if elapsed >= 60 else f"{elapsed:.0f}s"
        summary = (f"Generated {len(plan.slides)}-slide deck '{plan.title}'"
                   f" with {figures_used} figures")
        await _progress(f"PPTX saved · {size_mb} MB · {elapsed_str}", 1.0)
        return PresentationResult(
            status="ok",
            summary=summary,
            title=plan.title,
            pptx_uri=pptx_uri,
            slide_count=len(plan.slides),
            figures_used=figures_used,
            research=research,
            run_id=research.get("run_id", ""),
            report_uri=pptx_uri,
            artifacts=[pptx_artifact],
            metadata={
                "status": "ok",
                "summary": summary,
                "generation_time_s": elapsed,
                "language": req.language,
                "talk_type": req.talk_type,
                "duration_minutes": req.duration_minutes,
                "target_slides": req.target_slides,
                "qa_warnings": len(warnings),
                "visual_critique_edits": applied,
                "render_backend": render_backend,
                "template_uri": req.template_uri or "",
                "report_uri": pptx_uri,
                "research": research,
                "run_id": research.get("run_id", ""),
            },
        )

    # ── text-domain visual critique (no multimodal capability required) ──
    async def _critique_layout(
        self, report: list[dict[str, Any]], content: ParsedContent, req
    ) -> list[dict[str, Any]]:
        """Ask the LLM to review the *textual* layout report and propose edits.

        This is the text-domain replacement for a multimodal "look at the
        rendered slide" step: the model reasons over layout metrics + figure
        caption/section metadata instead of pixels. Returns a (possibly empty)
        list of minimal edits.
        """
        if not report:
            return []
        # Build a section-by-page lookup so each figure carries a section tag
        # the model can match against a slide's topic.
        sections_by_page: dict[int, str] = {}
        prescreen = (content.metadata or {}).get("section_pages") or {}
        if prescreen:
            total = (content.metadata or {}).get("page_count", 0) or (
                content.metadata or {}
            ).get("total_pages", 0)
            order = [
                "abstract", "introduction", "background", "methods",
                "results", "discussion", "conclusion", "references",
            ]
            for p in range(int(total)):
                current, best = "", -1
                for name in order:
                    sp = prescreen.get(name)
                    if sp is not None and best < sp <= p:
                        best, current = sp, name
                if current:
                    sections_by_page[p] = current

        fig_catalog: list[dict[str, Any]] = []
        for i, f in enumerate(content.figures):
            try:
                page = int(f.get("page_num", "") or -1)
            except (TypeError, ValueError):
                page = -1
            fig_catalog.append({
                "ref": f"figure_{i}",
                "section": sections_by_page.get(page, ""),
                "caption": (f.get("caption") or "")[:120],
                "related": (f.get("related_text") or "")[:160],
            })

        system = (
            "You are a slide layout reviewer. You CANNOT see images; you reason "
            "ONLY over a structured layout report and figure metadata. Output "
            "ONLY a JSON list of minimal edits. Never invent figures or numbers."
        )
        user = (
            "Layout report (per slide, from the rendered deck):\n"
            f"{json.dumps(report, ensure_ascii=False)[:6000]}\n\n"
            "Available figures (with section tag + caption + in-text context):\n"
            f"{json.dumps(fig_catalog, ensure_ascii=False)[:3000]}\n\n"
            "Find slides where: (a) a figure's caption/section does not match "
            "the slide's topic; (b) a content slide is too sparse; (c) a bullet "
            "is too long; (d) a wide/tall figure would fit a different slide "
            "better; (e) a figure is missing/unresolved. Return minimal edits:\n"
            '[{"slide_index":5,"action":"reassign_figure","figure":"figure_3"},'
            '{"slide_index":7,"action":"trim_bullet","bullet_index":2},'
            '{"slide_index":9,"action":"drop_figure"}]\n'
            "Only use figure refs that appear in the catalog above. Return [] if "
            "the deck is already coherent."
        )
        try:
            raw = await _chat_compat_engine(self._llm(), system=system, user=user)
        except Exception:  # noqa: BLE001
            return []
        payload = extract_structured_payload_engine(raw)
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _apply_layout_edits(plan, edits: list[dict[str, Any]]) -> int:
        """Apply the LLM's minimal layout edits in-place. Returns count applied."""
        n_figs_ref = re.compile(r"^figure_\d+$")
        applied = 0
        for e in edits:
            if not isinstance(e, dict):
                continue
            try:
                idx = int(e.get("slide_index"))
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(plan.slides)):
                continue
            slide = plan.slides[idx]
            action = str(e.get("action", ""))

            if action == "reassign_figure":
                fig = str(e.get("figure", ""))
                if n_figs_ref.match(fig):
                    slide.figure_path = fig  # re-resolved on next render pass
                    # Ensure the layout can actually show a figure.
                    if slide.slide_type not in ("content_figure", "full_figure"):
                        slide.slide_type = "content_figure"
                    applied += 1
            elif action == "drop_figure":
                if slide.figure_path:
                    slide.figure_path = None
                    slide.figure_caption = None
                    if slide.slide_type in ("content_figure", "full_figure"):
                        slide.slide_type = "content"
                        if not slide.bullets:
                            slide.bullets = [slide.title or "See paper for details"]
                    applied += 1
            elif action == "trim_bullet":
                bi = e.get("bullet_index")
                if slide.bullets and isinstance(bi, int) and 0 <= bi < len(slide.bullets):
                    b = slide.bullets[bi]
                    if len(b) > 60:
                        cut = b[:60].rfind(" ")
                        slide.bullets[bi] = (
                            b[: cut if cut > 30 else 60].rstrip(",.;:") + "..."
                        )
                        applied += 1
        return applied

    @staticmethod
    def _apply_plan_edits(plan_dict: dict, edits: list[dict[str, Any]]) -> dict:
        """Apply a list of edit operations to a plan dict.

        This lets the REPL model make surgical edits without reconstructing
        the entire plan JSON — it passes ``plan_edits`` instead of (or in
        addition to) ``approved_plan`` when resuming from a review checkpoint.

        Supported actions:

        - ``set_title``: change a slide's title
        - ``set_bullets``: replace a slide's entire bullet list
        - ``set_bullet``: change a single bullet by index
        - ``remove_slide``: delete a slide by index
        - ``add_slide``: insert a new slide (provide ``slide`` dict + ``after_index``)
        - ``set_figure``: change figure_path on a slide
        - ``set_type``: change slide_type (content→content_figure etc.)
        - ``swap_slides``: swap two slides by index
        - ``move_slide``: move a slide from one position to another
        """
        output = copy.deepcopy(plan_dict)
        slides = output.get("slides", [])
        if not slides:
            raise ValueError("plan_edits require a plan with at least one slide")

        supported_actions = {
            "set_title",
            "set_bullets",
            "set_bullet",
            "remove_slide",
            "add_slide",
            "set_figure",
            "set_type",
            "swap_slides",
            "move_slide",
        }

        def index_for(edit: dict[str, Any], key: str) -> int:
            try:
                index = int(edit[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"plan edit {key} must be an integer") from exc
            if not 0 <= index < len(slides):
                raise ValueError(
                    f"plan edit {key}={index} is outside 0..{len(slides) - 1}"
                )
            return index

        # Operations are applied in declaration order. Every index therefore
        # refers to the plan produced by the preceding operation, matching the
        # examples shown to users (edit a slide, then remove another one).
        for edit in edits:
            if not isinstance(edit, dict):
                raise ValueError("every plan edit must be an object")
            action = str(edit.get("action", "")).strip()
            if action not in supported_actions:
                raise ValueError(f"unsupported plan edit action: {action or '<empty>'}")

            if action == "remove_slide":
                del slides[index_for(edit, "slide_index")]

            elif action == "add_slide":
                new_slide = edit.get("slide")
                if not isinstance(new_slide, dict):
                    raise ValueError("add_slide requires a slide object")
                try:
                    after = int(edit.get("after_index", len(slides) - 1))
                except (TypeError, ValueError) as exc:
                    raise ValueError("add_slide after_index must be an integer") from exc
                if not -1 <= after < len(slides):
                    raise ValueError(
                        f"plan edit after_index={after} is outside -1..{len(slides) - 1}"
                    )
                slides.insert(after + 1, copy.deepcopy(new_slide))

            elif action == "swap_slides":
                a = index_for(edit, "index_a")
                b = index_for(edit, "index_b")
                slides[a], slides[b] = slides[b], slides[a]

            elif action == "move_slide":
                src = index_for(edit, "from_index")
                dst = index_for(edit, "to_index")
                if src != dst:
                    slides.insert(dst, slides.pop(src))

            elif action == "set_title":
                idx = index_for(edit, "slide_index")
                title = str(edit.get("title", "")).strip()
                if not title:
                    raise ValueError("set_title requires non-empty title text")
                slides[idx]["title"] = title

            elif action == "set_bullets":
                idx = index_for(edit, "slide_index")
                bullets = edit.get("bullets")
                if not isinstance(bullets, list):
                    raise ValueError("set_bullets requires a bullets array")
                slides[idx]["bullets"] = [str(bullet) for bullet in bullets]

            elif action == "set_bullet":
                idx = index_for(edit, "slide_index")
                bullets = slides[idx].get("bullets")
                if not isinstance(bullets, list):
                    raise ValueError("set_bullet requires an existing bullets array")
                try:
                    bullet_idx = int(edit["bullet_index"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("set_bullet bullet_index must be an integer") from exc
                if not 0 <= bullet_idx < len(bullets):
                    raise ValueError(
                        f"plan edit bullet_index={bullet_idx} is outside "
                        f"0..{len(bullets) - 1}"
                    )
                text = str(edit.get("text", "")).strip()
                if not text:
                    raise ValueError("set_bullet requires non-empty text")
                bullets[bullet_idx] = text

            elif action == "set_figure":
                idx = index_for(edit, "slide_index")
                figure_path = edit.get("figure_path")
                slides[idx]["figure_path"] = (
                    str(figure_path) if figure_path else None
                )

            elif action == "set_type":
                idx = index_for(edit, "slide_index")
                slide_type = str(edit.get("slide_type", "")).strip()
                if not slide_type:
                    raise ValueError("set_type requires a non-empty slide_type")
                slides[idx]["slide_type"] = slide_type

        output["slides"] = slides
        return output

    async def _process_paper_corpus(
            self, req: PresentationRequest, work_dir: str, _progress: Any,
    ) -> ParsedContent:
        """Parse multiple PDFs and merge them into one ParsedContent.

        Each paper contributes its own figures/tables (paths remain unique
        per work_dir) and is prefixed with a heading so the planner can tell
        them apart. Section metadata is dropped — the corpus is treated as
        one long document by the LLM.
        """
        _pp = _load_sibling("pdf_parser")
        parts: list[str] = []
        all_figs: list[dict[str, str]] = []
        all_tabs: list[dict[str, Any]] = []
        all_eqs: list[str] = []
        citation_seed: list[dict[str, str]] = []

        for i, uri in enumerate(req.paper_uris):
            local = await self._resolve_pdf(uri, work_dir)
            if not local:
                continue
            img_dir = os.path.join(work_dir, f"paper_{i}_imgs")
            md_text, meta, _ = await _pp.parse_pdf(local, output_dir=img_dir, prescreen=False)
            fig_dir = os.path.join(work_dir, f"paper_{i}_figs")
            enriched = await _pp.extract_figures_enriched(local, md_text, fig_dir, None)
            # ``ParsedContent.figures`` is typed as list[dict[str, str]] — the PDF
            # extractor returns width/height/element_count as ints. Normalize
            # every value to str here (same shape as the single-PDF path in
            # ``_process_inputs``), otherwise pydantic v2 rejects the whole
            # ParsedContent with dozens of ``string_type`` errors.
            for f in enriched:
                all_figs.append({
                    "path": f.get("path", ""),
                    "caption": f.get("caption", ""),
                    "source": f.get("source", "unknown"),
                    "page_num": str(f.get("page_num", "")),
                    "width": str(f.get("width", "")),
                    "height": str(f.get("height", "")),
                    "related_text": f.get("related_text", ""),
                    "element_count": str(f.get("element_count", 1)),
                    # Provenance tag: which paper this figure came from.
                    "source_paper": f"paper_{i}",
                })
            all_tabs.extend(_pp.extract_tables_enriched(local, md_text, None))
            all_eqs.extend(_pp.extract_equations_from_markdown(md_text))
            title = meta.get("title") or Path(local).stem
            parts.append(f"# Paper {i + 1}: {title}\n\n{md_text}")
            citation_seed.append({
                "key": f"[{i + 1}]",
                "text": f"{title}. {meta.get('author', '')}",
                "path": local,
            })

        return ParsedContent(
            source_type="corpus",
            markdown_text="\n\n---\n\n".join(parts),
            sections={},  # corpus is intentionally flat
            figures=all_figs,
            tables=all_tabs,
            equations=all_eqs,
            metadata={"citation_seed": citation_seed, "n_papers": len(citation_seed)},
        )

    async def _process_rom_corpus(
            self, req: PresentationRequest, work_dir: str,
    ) -> ParsedContent | None:
        """Pull source chunks + citation metadata from omni's ROM.

        Falls back to returning None (so the caller can use another source)
        when omni is not available (portable-runner / no db in ctx).
        """
        ctx = self.ctx
        db = getattr(ctx, "db", None)
        if db is None:
            return None
        try:
            from omni.research import ResearchStore, search_corpus

            store = ResearchStore(db)
            source_ids: list[str] = list(req.source_ids or [])
            chunks: list[Any] = []
            if req.corpus_query:
                settings = getattr(ctx, "settings", None)
                research_settings = getattr(settings, "research", None)
                hits = await search_corpus(
                    store,
                    getattr(ctx, "llm", None),
                    req.corpus_query,
                    k=24,
                    as_of=str(getattr(research_settings, "as_of", "") or ""),
                    vector_backend=str(getattr(research_settings, "vector_backend", "auto") or "auto"),
                )
                chunks.extend(hits)
                source_ids.extend({h.source_id for h in hits if getattr(h, "source_id", "")})
            # Load explicit sources + their chunks so we always have context
            # even when corpus_query returned nothing (search index empty).
            source_ids = list(dict.fromkeys(source_ids))
            parts: list[str] = []
            citation_seed: list[dict[str, str]] = []
            for i, sid in enumerate(source_ids, start=1):
                src = await store.get_source(sid)
                if src is None:
                    continue
                citation_seed.append({
                    "key": f"[{i}]",
                    "text": f"{src.title}. {getattr(src, 'authors', '')} "
                            f"({getattr(src, 'year', '')})",
                    "path": getattr(src, "url", "") or getattr(src, "doi", ""),
                })
                src_chunks = [c for c in chunks if getattr(c, "source_id", "") == sid]
                body = "\n\n".join(getattr(c, "text", "") for c in src_chunks) \
                       or getattr(src, "abstract", "") or getattr(src, "summary", "")
                parts.append(f"# [{i}] {src.title}\n\n{body}")
            if not parts:
                return None
            return ParsedContent(
                source_type="corpus",
                markdown_text="\n\n---\n\n".join(parts),
                sections={},
                metadata={"citation_seed": citation_seed, "n_papers": len(citation_seed)},
            )
        except Exception:  # noqa: BLE001 — corpus is optional
            return None

    # ── input processing ──
    async def _process_inputs(
        self, req: PresentationRequest, work_dir: str, _progress: Any
    ) -> ParsedContent:
        _pp = _load_sibling("pdf_parser")
        extract_equations_from_markdown = _pp.extract_equations_from_markdown
        extract_figures_enriched = _pp.extract_figures_enriched
        extract_tables_enriched = _pp.extract_tables_enriched
        prescreen_sections = _pp.prescreen_sections
        parse_pdf = _pp.parse_pdf
        split_markdown_sections = _pp.split_markdown_sections
        _page_range_to_list = _pp._page_range_to_list

        # Case 0a: explicit outline → slides.
        # An outline may still embed ![](fig.png) references and pipe tables.
        # We parse them the same way we do for a Markdown source so the
        # planner gets the full visual context.
        if req.outline and req.outline.strip():
            outline_text = req.outline
            # Outline figures: resolve any ![alt](path) references relative
            # to the current working dir (users typically drop a folder with
            # both the outline and the assets).
            outline_figures = _extract_markdown_figures(
                outline_text, Path.cwd(), work_dir,
            )
            outline_tables = extract_tables_enriched(
                None, outline_text, None,
            )
            # Best-effort citation seed: any "[N] Author, Year, Title" line.
            citation_seed = _extract_outline_citations(outline_text)
            content = ParsedContent(
                source_type="outline",
                markdown_text=outline_text,
                sections=split_markdown_sections(outline_text),
                figures=outline_figures,
                tables=outline_tables,
                equations=extract_equations_from_markdown(outline_text),
                metadata={"citation_seed": citation_seed} if citation_seed else {},
            )
            _cache_content_for_render(work_dir, content)
            return content

        # Case 0b: markdown source → sectioned text.
        # Extract pipe tables and citation seed from the markdown too, so a
        # markdown-driven deck has the same feature parity as a PDF-driven one.
        md_local = await self._resolve_to_local(req.markdown_uri or "", work_dir, ".md")
        if md_local:
            md_text = Path(md_local).read_text(encoding="utf-8", errors="replace")
            figures = _extract_markdown_figures(
                md_text, Path(md_local).parent, work_dir,
            )
            tables = extract_tables_enriched(None, md_text, None)
            citation_seed = _extract_outline_citations(md_text)
            content = ParsedContent(
                source_type="markdown", markdown_text=md_text,
                sections=split_markdown_sections(md_text),
                figures=figures,
                tables=tables,
                equations=extract_equations_from_markdown(md_text),
                metadata=(
                    {"citation_seed": citation_seed} if citation_seed else {}
                ),
            )
            _cache_content_for_render(work_dir, content)
            return content

        # resolve a PDF source (artifact:// or local path)
        pdf_path = await self._resolve_pdf(req.pdf_uri or "", work_dir)

        # A .md/.txt mistakenly passed as pdf_uri → treat as markdown, not PDF.
        if pdf_path and pdf_path.lower().endswith((".md", ".markdown", ".txt")):
            md_text = Path(pdf_path).read_text(encoding="utf-8", errors="replace")
            content = ParsedContent(
                source_type="markdown", markdown_text=md_text,
                sections=split_markdown_sections(md_text),
                equations=extract_equations_from_markdown(md_text),
            )
            _cache_content_for_render(work_dir, content)
            return content

        # Case 1: PDF
        if pdf_path and pdf_path.lower().endswith(".pdf"):
            await _progress(
                _l10n(req.language, "progress.parsing_pdf") or "Parsing PDF content...",
                0.08,
            )
            sections_map, total_pages = await prescreen_sections(pdf_path)
            ref_start = sections_map.get("references", total_pages)
            page_range = None
            if total_pages > 6 and ref_start > 0:
                end_page = min(ref_start - 1, 28) if total_pages > 30 else ref_start - 1
                if end_page >= 0:
                    page_range = f"0-{end_page}"

            img_dir = os.path.join(work_dir, "marker_images")
            md_text, metadata, _ = await parse_pdf(
                pdf_path, page_range, img_dir, prescreen=False
            )
            metadata["section_pages"] = sections_map
            metadata["total_pages"] = total_pages

            fig_dir = os.path.join(work_dir, "figures")
            page_list = _page_range_to_list(page_range)
            enriched = await extract_figures_enriched(pdf_path, md_text, fig_dir, page_list)

            figures: list[dict[str, str]] = [
                {
                    "path": f["path"],
                    "caption": f.get("caption", ""),
                    "source": f.get("source", "unknown"),
                    "page_num": str(f.get("page_num", "")),
                    "width": str(f.get("width", "")),
                    "height": str(f.get("height", "")),
                    "related_text": f.get("related_text", ""),
                    "element_count": str(f.get("element_count", 1)),
                }
                for f in enriched
            ]

            sections = split_markdown_sections(md_text)
            tables = extract_tables_enriched(pdf_path, md_text, page_list)
            equations = extract_equations_from_markdown(md_text)

            await _progress(
                _l10n(req.language, "progress.extraction_done",
                      figures=len(figures), tables=len(tables))
                or f"Extracted {len(figures)} figures · {len(tables)} tables",
                0.12,
            )
            content = ParsedContent(
                source_type="pdf",
                markdown_text=md_text,
                sections=sections,
                figures=figures,
                tables=tables,
                equations=equations,
                metadata={**(metadata or {}), "section_pages": sections_map,
                          "page_count": total_pages},
            )
            _cache_content_for_render(work_dir, content)
            return content

        # Case 2: extra file uris (resolved like the PDF path)
        if req.file_uris:
            all_text = ""
            for uri in req.file_uris:
                local = await self._resolve_pdf(uri, work_dir) if uri.lower().endswith(".pdf") else None
                if local:
                    md_text, _, _ = await parse_pdf(local, output_dir=os.path.join(work_dir, "imgs"))
                    all_text += f"---\nContent from {uri}:\n{md_text}\n\n"
                else:
                    # treat as a readable local text file
                    p = Path(uri.replace("file://", "")).expanduser()
                    if p.is_file():
                        all_text += f"---\nContent from {p.name}:\n{p.read_text(encoding='utf-8', errors='replace')}\n\n"
            content = ParsedContent(source_type="text", markdown_text=all_text)
            _cache_content_for_render(work_dir, content)
            return content

        # Case 2b: multiple PDFs merged as one corpus. Each PDF is parsed
        # with the same enriched extractor as the single-PDF path, so figures /
        # tables from every paper are available to the planner.
        if req.paper_uris:
            return await self._process_paper_corpus(req, work_dir, _progress)

        # Case 2c: resolve ROM sources — from an upstream literature step
        # (source_ids) or via a semantic query (corpus_query). Produces one
        # text-only ParsedContent with structured sections per source and a
        # citations block the planner can attach to slides.
        if req.corpus_query or req.source_ids:
            corpus_content = await self._process_rom_corpus(req, work_dir)
            if corpus_content is not None:
                _cache_content_for_render(work_dir, corpus_content)
                return corpus_content

        # Case 3: inline text
        if req.reference_text:
            content = ParsedContent(source_type="text", markdown_text=req.reference_text)
            _cache_content_for_render(work_dir, content)
            return content

        # Case 4: bare topic
        content = ParsedContent(source_type="prompt", markdown_text=req.topic)
        _cache_content_for_render(work_dir, content)
        return content

    # ── equation rendering (parallel, best-effort) ──
    async def _render_equations(self, equations: list[str], work_dir: str) -> list[str]:
        eq_dir = os.path.join(work_dir, "equations")
        os.makedirs(eq_dir, exist_ok=True)
        script = str(_SKILL_DIR / "scripts" / "render_latex.py")

        async def _one(i: int, eq: str) -> str | None:
            out_path = os.path.join(eq_dir, f"eq_{i}.png")
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "python", script, eq, "-o", out_path, "--dpi", "300",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=15)
                return out_path if os.path.exists(out_path) else None
            except (TimeoutError, Exception):  # noqa: BLE001
                try:
                    if proc is not None:
                        proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                return None

        results = await asyncio.gather(*[_one(i, e) for i, e in enumerate(equations[:10])])
        return [r for r in results if r]

    # ── overflow / sparse fixers ──
    @staticmethod
    def _apply_overflow_fixes(plan, overflow_results, iteration: int = 0) -> int:
        fixed = 0
        for item in overflow_results:
            idx = item["slide_index"]
            if idx >= len(plan.slides):
                continue
            slide = plan.slides[idx]
            severity = item.get("severity", "warning")
            overflow_inches = item.get("overflow_inches", 0)
            modified = False
            if slide.slide_type in ("title", "section"):
                continue
            if slide.slide_type == "metrics" and slide.metrics:
                modified = False
                # Truncate long labels that cause horizontal overflow in
                # narrow cards. Values are handled by adaptive font sizing
                # in the JS renderer on re-render.
                max_label_len = 18 if severity == "critical" else 24
                for i, m in enumerate(slide.metrics):
                    label = str(m.get("label", ""))
                    if len(label) > max_label_len:
                        cut = label[:max_label_len].rfind(" ")
                        if cut < max_label_len // 2:
                            cut = max_label_len
                        m["label"] = label[:cut].rstrip(",.;:") + "…"
                        modified = True
                # If still critical, reduce metric count — fewer cards
                # means wider cards and more room for text.
                if severity == "critical" and len(slide.metrics) > 2:
                    slide.metrics = slide.metrics[:2]
                    modified = True
                elif overflow_inches >= 0.3 and len(slide.metrics) > 3:
                    slide.metrics = slide.metrics[:3]
                    modified = True
                if modified:
                    fixed += 1
                continue
            if slide.bullets:
                max_len = 70 if severity == "critical" else 90
                for i, b in enumerate(slide.bullets):
                    if len(b) > max_len:
                        cut = b[:max_len].rfind(" ")
                        if cut < max_len // 2:
                            cut = max_len
                        slide.bullets[i] = b[:cut].rstrip(",.;:") + "..."
                        modified = True
            if slide.bullets and overflow_inches >= 0.3:
                max_bullets = 3 if severity == "critical" else 4
                if len(slide.bullets) > max_bullets:
                    slide.bullets = slide.bullets[:max_bullets]
                    modified = True
            if slide.table_rows:
                max_rows = 5 if severity == "critical" else 6
                if len(slide.table_rows) > max_rows:
                    slide.table_rows = slide.table_rows[:max_rows]
                    modified = True
                # Also truncate overly long cell text that causes
                # horizontal overflow within narrow columns.
                max_cell_len = 45 if severity == "critical" else 60
                for ri, row in enumerate(slide.table_rows):
                    for ci, cell in enumerate(row):
                        cell_str = str(cell)
                        if len(cell_str) > max_cell_len:
                            cut = cell_str[:max_cell_len].rfind(" ")
                            if cut < max_cell_len // 2:
                                cut = max_cell_len
                            row[ci] = cell_str[:cut].rstrip(",.;:") + "…"
                            modified = True
                # If there are too many columns, text becomes illegible;
                # drop the rightmost columns to keep content readable.
                max_cols = 6 if severity == "critical" else 8
                if slide.table_headers and len(slide.table_headers) > max_cols:
                    slide.table_headers = slide.table_headers[:max_cols]
                    for ri in range(len(slide.table_rows)):
                        slide.table_rows[ri] = slide.table_rows[ri][:max_cols]
                    modified = True
            if slide.figure_caption and len(slide.figure_caption) > 120:
                slide.figure_caption = slide.figure_caption[:117] + "..."
                modified = True
            if modified:
                fixed += 1
        return fixed

    @staticmethod
    def _check_sparse_slides(plan) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        for idx, slide in enumerate(plan.slides):
            if slide.slide_type in ("title", "section", "conclusion",
                                    "two_column", "icon_rows", "steps",
                                    "emphasis_box", "full_figure", "metrics", "table"):
                continue
            issue = None
            if slide.slide_type in ("content", "content_figure"):
                n = len(slide.bullets) if slide.bullets else 0
                if n == 0:
                    issue = "no_bullets"
                elif n == 1:
                    issue = "single_bullet"
            elif slide.slide_type == "metrics":
                if len(slide.metrics) < 2:
                    issue = "insufficient_metrics"
            elif slide.slide_type == "table":
                if not slide.table_rows or not slide.table_headers:
                    issue = "empty_table"
            elif slide.slide_type == "full_figure":
                if not slide.title and not slide.figure_caption:
                    issue = "unlabeled_figure"
            if issue:
                warnings.append({"slide_index": idx, "slide_type": slide.slide_type, "issue": issue})
        return warnings

    @staticmethod
    def _fix_sparse_slides(
        plan: PresentationPlan,
        sparse_warnings: list[dict[str, Any]],
        *,
        target_slides: int | None = None,
    ) -> int:
        """Remove mergeable sparse slides unless an exact count forbids it."""

        if target_slides is not None:
            return 0
        indices_to_remove: set[int] = set()
        fixed = 0
        for w in sparse_warnings:
            idx, issue = w["slide_index"], w["issue"]
            slide = plan.slides[idx]
            if issue == "no_bullets" and slide.slide_type == "content":
                indices_to_remove.add(idx)
                fixed += 1
            elif issue == "single_bullet" and slide.slide_type == "content" and idx > 0:
                prev = plan.slides[idx - 1]
                if prev.slide_type in ("content", "content_figure") and prev.bullets and len(prev.bullets) < 6:
                    prev.bullets.append(slide.bullets[0])
                    indices_to_remove.add(idx)
                    fixed += 1
        for idx in sorted(indices_to_remove, reverse=True):
            plan.slides.pop(idx)
        return fixed

    @staticmethod
    def _sanitize_filename(title: str, max_len: int = 60) -> str:
        name = re.sub(r'[\\/:*?"<>|\n\r\t]+', "", title).strip().replace(" ", "_")
        return (name[:max_len] or "presentation")


# ── module-level content cache (so resume can re-resolve figures) ──
_CONTENT_CACHE: dict[str, ParsedContent] = {}


def _cache_content_for_render(work_dir: str, content: ParsedContent) -> None:
    _CONTENT_CACHE[work_dir] = content


def _resolve_content_for_render_impl(work_dir: str) -> ParsedContent:
    return _CONTENT_CACHE.get(work_dir, ParsedContent(source_type="prompt"))

def _extract_pptx_theme(pptx_path: str) -> dict[str, Any] | None:
    """Pull colour scheme + fonts from a PPTX theme (zip → theme1.xml)."""
    import zipfile
    from xml.etree import ElementTree as ET

    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    try:
        with zipfile.ZipFile(pptx_path) as z:
            names = sorted(n for n in z.namelist()
                           if n.startswith("ppt/theme/theme") and n.endswith(".xml"))
            if not names:
                return None
            root = ET.fromstring(z.read(names[0]))
    except Exception:  # noqa: BLE001
        return None

    def _hex(scheme, tag: str) -> str:
        if scheme is None:
            return ""
        el = scheme.find(f"a:{tag}", ns)
        if el is None:
            return ""
        srgb = el.find("a:srgbClr", ns)
        if srgb is not None:
            return (srgb.get("val") or "").upper()
        sysc = el.find("a:sysClr", ns)
        if sysc is not None:
            return (sysc.get("lastClr") or "").upper()
        return ""

    clr = root.find(".//a:clrScheme", ns)
    dk1 = _hex(clr, "dk1") or _hex(clr, "dk2") or "1E2761"
    dk2 = _hex(clr, "dk2") or dk1
    lt1 = _hex(clr, "lt1") or "FFFFFF"
    accent1 = _hex(clr, "accent1") or dk2
    accent2 = _hex(clr, "accent2") or "CADCFC"
    colors = {
        "primary": accent1,
        "secondary": accent2,
        "accent": lt1 or "FFFFFF",
        "dark": dk2,
        "bodyText": dk1 if dk1 != "000000" else "2D2D2D",
        "muted": "6B7280",
        "tableFill": "F0F4FF",
        "tableHead": accent1,
    }

    fonts = root.find(".//a:fontScheme", ns)
    def _font(kind: str) -> str:
        if fonts is None:
            return ""
        node = fonts.find(f"a:{kind}/a:latin", ns)
        return (node.get("typeface") if node is not None else "") or ""

    return {
        "colors": colors,
        "header_font": _font("majorFont"),
        "body_font": _font("minorFont"),
    }

def _extract_pptx_template(pptx_path: str, asset_dir: str) -> dict[str, Any] | None:
    """Extract reusable template spec from a user PPTX.

    Returns dict with:
      - colors: theme palette
      - header_font, body_font
      - master: background/logo/title_band info (for defineSlideMaster fallback)
      - layouts: list of layout specs, each with:
          name, index, placeholders (list of {type, idx, x, y, w, h})
      - layout_roles: mapping from semantic role -> layout index
    """
    import zipfile
    from xml.etree import ElementTree as ET

    theme = _extract_pptx_theme(pptx_path) or {"colors": {}, "header_font": "", "body_font": ""}

    master: dict[str, Any] = {
        "background": "", "background_image": "", "logo": None, "title_band": None,
    }
    layouts_spec: list[dict[str, Any]] = []
    layout_roles: dict[str, int] = {}

    ns = {
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    EMU = 914400.0
    SLIDE_W_EMU = 12192000.0
    SLIDE_H_EMU = 6858000.0

    try:
        os.makedirs(asset_dir, exist_ok=True)
        with zipfile.ZipFile(pptx_path) as z:
            # ── 1. Extract master background/logo ──
            masters = sorted(n for n in z.namelist()
                             if n.startswith("ppt/slideMasters/slideMaster")
                             and n.endswith(".xml"))
            if masters:
                master_xml = masters[0]
                root = ET.fromstring(z.read(master_xml))

                # Background solid fill
                bg = root.find(".//p:cSld/p:bg//a:solidFill/a:srgbClr", ns)
                if bg is not None:
                    master["background"] = (bg.get("val") or "").upper()

                # Rels for master images
                rels_name = master_xml.replace(
                    "slideMasters/", "slideMasters/_rels/") + ".rels"
                rel_map: dict[str, str] = {}
                if rels_name in z.namelist():
                    rroot = ET.fromstring(z.read(rels_name))
                    for rel in rroot:
                        rid = rel.get("Id") or ""
                        tgt = rel.get("Target") or ""
                        if "image" in (rel.get("Type") or "").lower():
                            rel_map[rid] = tgt

                # Picture shapes on master
                pics: list[dict[str, Any]] = []
                for pic in root.findall(".//p:pic", ns):
                    blip = pic.find(".//a:blip", ns)
                    off = pic.find(".//a:off", ns)
                    ext = pic.find(".//a:ext", ns)
                    if blip is None or off is None or ext is None:
                        continue
                    rid = blip.get(f"{{{ns['r']}}}embed") or ""
                    tgt = rel_map.get(rid, "")
                    if not tgt:
                        continue
                    src = ("ppt/" + tgt.replace("../", "")).replace("//", "/")
                    if src not in z.namelist():
                        continue
                    out = Path(asset_dir) / Path(src).name
                    out.write_bytes(z.read(src))
                    pics.append({
                        "path": str(out),
                        "x": int(off.get("x", 0)) / EMU,
                        "y": int(off.get("y", 0)) / EMU,
                        "w": int(ext.get("cx", 0)) / EMU,
                        "h": int(ext.get("cy", 0)) / EMU,
                    })

                if pics:
                    pics.sort(key=lambda p: p["w"] * p["h"], reverse=True)
                    biggest = pics[0]
                    if biggest["w"] >= (SLIDE_W_EMU / EMU) * 0.8 and \
                       biggest["h"] >= (SLIDE_H_EMU / EMU) * 0.8:
                        master["background_image"] = biggest["path"]
                        corner = pics[1] if len(pics) > 1 else None
                    else:
                        corner = biggest
                    if corner and corner["w"] < (SLIDE_W_EMU / EMU) * 0.25:
                        master["logo"] = corner

            # ── 2. Extract each slide layout ──
            layout_files = sorted(
                n for n in z.namelist()
                if n.startswith("ppt/slideLayouts/slideLayout")
                and n.endswith(".xml")
            )
            for li, layout_path in enumerate(layout_files):
                try:
                    layout_root = ET.fromstring(z.read(layout_path))
                except Exception:
                    continue

                # Layout name from CSld name attribute
                cSld = layout_root.find(".//p:cSld", ns)
                layout_name = ""
                if cSld is not None:
                    layout_name = cSld.get("name", "") or f"Layout {li + 1}"

                placeholders: list[dict[str, Any]] = []
                for ph in layout_root.findall(".//p:ph", ns):
                    ph_type = ph.get("type", "")
                    ph_idx = ph.get("idx")
                    if ph_idx is None:
                        continue
                    # Find the parent sp (shape) for position
                    sp = ph
                    for _ in range(5):  # walk up to find sp
                        sp = sp.getparent() if sp is not None else None
                        if sp is not None and sp.tag.endswith("}sp"):
                            break
                    xfrm = sp.find(".//a:xfrm", ns) if sp is not None else None
                    if xfrm is not None:
                        off = xfrm.find("a:off", ns)
                        ext = xfrm.find("a:ext", ns)
                        x = int(off.get("x", 0)) / EMU if off is not None else 0
                        y = int(off.get("y", 0)) / EMU if off is not None else 0
                        w = int(ext.get("cx", 0)) / EMU if ext is not None else 0
                        h = int(ext.get("cy", 0)) / EMU if ext is not None else 0
                    else:
                        x, y, w, h = 0, 0, 0, 0

                    placeholders.append({
                        "type": ph_type,
                        "idx": int(ph_idx),
                        "x": round(x, 3),
                        "y": round(y, 3),
                        "w": round(w, 3),
                        "h": round(h, 3),
                    })

                layouts_spec.append({
                    "index": li,
                    "name": layout_name,
                    "placeholders": placeholders,
                })

                # ── 3. Classify layout roles ──
                # Heuristic: classify by placeholder types present
                ph_types = {p["type"] for p in placeholders}
                has_ctr_title = "ctrTitle" in ph_types
                has_title = "title" in ph_types or has_ctr_title
                has_body = "body" in ph_types
                has_subtitle = "subTitle" in ph_types
                has_pic = "pic" in ph_types

                # Title slide layout
                if (has_ctr_title or (has_title and has_subtitle)) and not has_body:
                    layout_roles.setdefault("title", li)
                # Section header layout (often has title + subtitle, no body)
                elif has_title and not has_body and not has_subtitle:
                    layout_roles.setdefault("section", li)
                # Content with picture (two_content)
                elif has_title and has_body and has_pic:
                    layout_roles.setdefault("two_content", li)
                    layout_roles.setdefault("picture", li)
                # Two body placeholders
                elif has_title and len([p for p in placeholders if p["type"] == "body"]) >= 2:
                    layout_roles.setdefault("two_content", li)
                # Standard content
                elif has_title and has_body:
                    layout_roles.setdefault("content", li)
                # Blank
                elif not has_title and not has_body:
                    layout_roles.setdefault("blank", li)

            # Fallback assignments
            n_l = len(layouts_spec)
            if n_l > 0:
                layout_roles.setdefault("title", 0)
                layout_roles.setdefault("content", 1 if n_l > 1 else 0)
                layout_roles.setdefault("section", layout_roles.get("title", 0))
                layout_roles.setdefault("two_content", layout_roles.get("content", 0))
                layout_roles.setdefault("blank", n_l - 1)

    except Exception:
        logger.debug("template extraction failed", exc_info=True)
        pass

    theme["master"] = master
    theme["layouts"] = layouts_spec
    theme["layout_roles"] = layout_roles
    return theme

def _extract_markdown_figures(
    md_text: str, base_dir: Path, work_dir: str,
) -> list[dict[str, str]]:
    """Resolve ![alt](path) image references from a markdown source.

    Returns the same dict shape as PDF-extracted figures so downstream
    slide planning / rendering treats them uniformly. Remote URLs
    (http/https) are downloaded to work_dir; missing files are skipped.
    """
    import re as _re
    import urllib.request as _url

    figs: list[dict[str, str]] = []
    fig_dir = Path(work_dir) / "md_figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    pattern = _re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    for i, m in enumerate(pattern.finditer(md_text)):
        alt = m.group(1).strip()
        raw = m.group(2).strip()
        path: Path | None = None
        if raw.startswith(("http://", "https://")):
            dst = fig_dir / f"remote_{i}{Path(raw).suffix or '.png'}"
            try:
                _url.urlretrieve(raw, dst)
                path = dst
            except Exception:  # noqa: BLE001
                continue
        else:
            candidate = (base_dir / raw).expanduser()
            if candidate.is_file():
                path = candidate
        if path is None or not path.is_file():
            continue
        # Copy into work_dir so cleanup on failure never touches the user file.
        local = fig_dir / f"md_{i}_{path.name}"
        try:
            shutil.copy2(path, local)
        except Exception:  # noqa: BLE001
            continue
        figs.append({
            "path": str(local),
            "caption": alt or f"Figure from markdown ({i + 1})",
            "source": "markdown_inline",
            "page_num": "",
            "width": "", "height": "",
            "related_text": "",
            "element_count": "1",
        })
    return figs

def _number_captions(plan, *, language: str = "en") -> None:
    """Prefix figure / table captions with a running number.

    We number Figures and Tables separately in first-encounter order.
    Skips slides whose caption already looks numbered (heuristic: starts
    with English or Chinese variants of 'Figure N' / 'Fig N' / 'Table N').
    """
    import re as _re

    fig_i = tab_i = 0
    fig_prefix = "\u56fe" if language == "zh" else "Figure"
    tab_prefix = "\u8868" if language == "zh" else "Table"
    numbered_re = _re.compile(
        r"^\s*(?:figure|fig\.?|\u56fe|table|tab\.?|\u8868)\s*\d+", _re.IGNORECASE,
    )

    for s in plan.slides:
        if s.slide_type in ("content_figure", "full_figure") and s.figure_path:
            fig_i += 1
            cap = (s.figure_caption or "").strip()
            if cap and not numbered_re.match(cap):
                s.figure_caption = f"{fig_prefix} {fig_i}: {cap}"
            elif not cap:
                s.figure_caption = f"{fig_prefix} {fig_i}"
        elif s.slide_type == "table" and s.table_headers:
            tab_i += 1
            # Tables carry their number in the title (there's no separate
            # caption field); prefix only when the title is a real claim.
            title = (s.title or "").strip()
            if title and not numbered_re.match(title):
                s.title = f"{tab_prefix} {tab_i}: {title}"

def _extract_outline_citations(text: str) -> list[dict[str, str]]:
    """Pull "[N] free-text" citation lines out of a user outline.

    Users writing outlines often list references at the end as
    "[1] Vaswani et al., 2017, Attention is all you need." We surface those
    to the planner so bullets can cite them and a References slide is added.
    """
    import re as _re

    seen: dict[str, str] = {}
    for line in text.splitlines():
        m = _re.match(r"\s*(\[\d+\])\s+(.{5,300})\s*$", line)
        if m and m.group(1) not in seen:
            seen[m.group(1)] = m.group(2).strip()
    return [{"key": k, "text": v} for k, v in seen.items()]
