"""Portable, deterministic contracts for the paper-review workflow.

This module deliberately contains no Omni imports.  The Omni engine and the
portable runner share it for venue selection, bounded Semantic Scholar result
merging, structured model-output parsing, and final Markdown rendering.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from json_repair import repair_json

DESK_FIELDS: tuple[str, ...] = (
    "Paper Length",
    "Topic Compatibility",
    "Minimum Quality",
    "Prompt Injection and Hidden Manipulation Detection",
)

DETAILED_REVISION_HEADING = "Detailed Revision Plan"

FALLBACK_FIELDS: tuple[str, ...] = (
    "Paper Summary",
    "Summary Of Strengths",
    "Summary Of Weaknesses",
    "Potentially Missing Related Work",
    "Comments Suggestions And Typos",
    "Confidence",
    "Soundness",
    "Excitement / Significance",
    "Overall Assessment",
    "Limitations And Societal Impact",
    "Ethical Concerns",
    "Needs Ethics Review",
    "Reproducibility",
    "Datasets",
    "Software",
)


@dataclass(frozen=True)
class VenueSelection:
    """Resolved venue profile and its exact author-facing field order."""

    key: str
    profile_filename: str
    requested: str
    fields: tuple[str, ...]
    supported: bool


_VENUE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("neurips", ("neurips", "nips")),
    ("iclr", ("iclr",)),
    ("icml", ("icml",)),
    ("cvpr", ("cvpr",)),
    ("acl-arr", ("acl", "arr", "emnlp", "naacl", "eacl", "aacl")),
    ("aaai", ("aaai",)),
)

_PROFILE_FILES = {
    "neurips": "neurips.md",
    "iclr": "iclr.md",
    "icml": "icml.md",
    "cvpr": "cvpr.md",
    "acl-arr": "acl-arr.md",
    "aaai": "aaai.md",
}


def resolve_venue(
    venue: str,
    venue_dir: str | Path,
) -> VenueSelection:
    """Select one bundled profile and its year-appropriate field contract."""

    requested = " ".join(str(venue or "").split())
    lower = requested.casefold()
    key = ""
    for candidate, aliases in _VENUE_ALIASES:
        if any(re.search(rf"\b{re.escape(alias)}\b", lower) for alias in aliases):
            key = candidate
            break
    if not key:
        return VenueSelection(
            key="fallback",
            profile_filename="",
            requested=requested or "Unspecified venue",
            fields=FALLBACK_FIELDS,
            supported=False,
        )

    filename = _PROFILE_FILES[key]
    profile_path = Path(venue_dir) / filename
    profile_text = profile_path.read_text(encoding="utf-8")
    fields = extract_profile_fields(profile_text, requested)
    if not fields:
        raise ValueError(f"Venue profile {filename} has no numbered review contract.")
    return VenueSelection(
        key=key,
        profile_filename=filename,
        requested=requested or key,
        fields=tuple(fields),
        supported=True,
    )


def extract_profile_fields(profile_text: str, venue: str = "") -> list[str]:
    """Read the exact numbered backtick fields from the applicable contract."""

    blocks: list[tuple[str, str]] = []
    headings = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", profile_text))
    for index, heading in enumerate(headings):
        title = heading.group(1).strip()
        if "contract" not in title.casefold():
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(profile_text)
        blocks.append((title, profile_text[heading.end() : end]))

    if not blocks:
        return []
    year_match = re.search(r"\b(20\d{2})\b", venue)
    selected: tuple[str, str] | None = None
    if year_match:
        selected = next(
            (block for block in blocks if year_match.group(1) in block[0]),
            None,
        )
    if selected is None:
        selected = blocks[-1]
    return re.findall(r"(?m)^\s*\d+\.\s+`([^`]+)`", selected[1])


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract one JSON object from a plain or fenced model response."""

    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model response did not contain a JSON object") from None
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"model response contained invalid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        # Keep every malformed model response on the same repairable boundary.
        raise ValueError(  # noqa: TRY004 - JSON shape is invalid response data
            "model response JSON must be an object"
        )
    return parsed


def repair_json_object(text: str) -> dict[str, Any]:
    """Repair one malformed model-authored JSON object without another model call."""

    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    candidate = raw[start : end + 1] if start >= 0 and end > start else raw
    try:
        repaired = repair_json(
            candidate,
            return_objects=True,
            skip_json_loads=True,
        )
    except Exception as exc:  # noqa: BLE001 - normalize third-party parser failures
        raise ValueError(f"json_repair could not repair the model response: {exc}") from None
    if not isinstance(repaired, dict) or not repaired:
        raise ValueError("json_repair did not produce a non-empty JSON object")
    return repaired


def parse_queries(text: str, *, minimum: int = 3, maximum: int = 4) -> list[str]:
    """Validate and deduplicate active-model-authored literature queries."""

    payload = parse_json_object(text)
    raw_queries = payload.get("queries")
    if not isinstance(raw_queries, list):
        raise ValueError(  # noqa: TRY004 - triggers the bounded model-response repair
            "query response must contain a queries array"
        )
    queries: list[str] = []
    seen: set[str] = set()
    for value in raw_queries:
        query = " ".join(str(value or "").split())
        key = query.casefold()
        if not query or key in seen:
            continue
        if len(query) > 500:
            raise ValueError("Semantic Scholar query exceeds 500 characters")
        seen.add(key)
        queries.append(query)
    if not minimum <= len(queries) <= maximum:
        raise ValueError(f"expected {minimum}-{maximum} unique literature queries")
    return queries


def missing_payload_fields(
    payload: dict[str, Any],
    review_fields: Sequence[str],
) -> list[str]:
    """List empty required fields before deterministic rendering."""

    missing: list[str] = []
    if not _text(payload.get("target_venue")):
        missing.append("target_venue")
    if not _text(payload.get("reviewed_as")):
        missing.append("reviewed_as")
    desk = payload.get("desk_rejection")
    if not isinstance(desk, dict):
        missing.extend(f"desk_rejection.{field}" for field in DESK_FIELDS)
    else:
        missing.extend(
            f"desk_rejection.{field}"
            for field in DESK_FIELDS
            if not _text(desk.get(field))
        )
    review = payload.get("review_fields")
    if not isinstance(review, dict):
        missing.extend(f"review_fields.{field}" for field in review_fields)
    else:
        missing.extend(
            f"review_fields.{field}"
            for field in review_fields
            if not _text(review.get(field))
        )
    if not _text(payload.get("disclaimer")):
        missing.append("disclaimer")
    return missing


def review_field_isolation_failures(
    payload: dict[str, Any],
    review_fields: Sequence[str],
) -> list[str]:
    """Find venue-form headings nested inside another review field.

    Each venue field is rendered by the outer form, so recreating a sibling
    field as a Markdown heading inside a value duplicates and corrupts that
    form. Ordinary paper-specific subheadings remain allowed.
    """

    review = payload.get("review_fields")
    if not isinstance(review, dict):
        return []
    canonical = {_heading_key(field): str(field) for field in review_fields}
    failures: list[str] = []
    for owner in review_fields:
        value = _text(review.get(owner))
        for heading in _markdown_headings(value):
            nested = canonical.get(_heading_key(heading))
            if nested is None:
                continue
            failures.append(
                f"review_fields.{owner} contains nested venue field heading: {nested}"
            )
    return failures


def render_review(
    payload: dict[str, Any],
    review_fields: Sequence[str],
    *,
    requested_venue: str,
) -> str:
    """Render the complete review and an optional second-stage revision plan."""

    desk = payload.get("desk_rejection")
    desk = desk if isinstance(desk, dict) else {}
    review = payload.get("review_fields")
    review = review if isinstance(review, dict) else {}
    target = _text(payload.get("target_venue")) or requested_venue
    reviewed_as = _text(payload.get("reviewed_as")) or requested_venue
    lines = [
        "# Target Venue",
        "",
        target,
        "",
        "# Reviewed as if submitted to",
        "",
        reviewed_as,
        "",
        "# Desk Rejection Assessment",
        "",
    ]
    for field in DESK_FIELDS:
        value = _text(desk.get(field)) or "Unable to assess from the supplied evidence."
        lines.extend([f"**{field}:** {value}", ""])
    lines.extend(["# Expected Review Outcome", ""])
    for field in review_fields:
        value = _text(review.get(field)) or "Unable to assess from the supplied evidence."
        lines.extend([f"## {field}", "", value, ""])
    disclaimer = _text(payload.get("disclaimer")) or (
        "This is an author-facing simulated pre-submission review, not an official venue review."
    )
    lines.extend(["# Disclaimer", "", disclaimer, ""])
    revision_plan = render_revision_plan(payload.get("revision_plan"))
    if revision_plan:
        lines.extend([f"# {DETAILED_REVISION_HEADING}", "", revision_plan, ""])
    return "\n".join(lines).strip() + "\n"


def render_revision_plan(plan: Any) -> str:
    """Render the structured author revision plan without changing the review."""

    if not isinstance(plan, dict):
        return ""
    status = _text(plan.get("status")).casefold()
    if status and status != "ok":
        return _text(plan.get("message")) or (
            "The formal review completed, but the detailed revision-planning stage "
            "did not finish. Use the evidence-backed weaknesses above as the current "
            "revision boundary."
        )

    lines: list[str] = []
    strategy = _text(plan.get("revision_strategy"))
    if strategy:
        lines.extend(["## Revision Strategy", "", strategy, ""])

    actions = plan.get("prioritized_actions")
    if isinstance(actions, list) and actions:
        lines.extend(["## Prioritized Revision Actions", ""])
        for index, raw_action in enumerate(actions, start=1):
            if not isinstance(raw_action, dict):
                continue
            priority = _text(raw_action.get("priority")) or "Priority"
            title = _text(raw_action.get("title")) or f"Revision action {index}"
            lines.extend([f"{index}. **{priority} — {title}**", ""])
            review_concern = _text(raw_action.get("review_concern"))
            paper_location = _text(raw_action.get("paper_location"))
            required_change = _text(raw_action.get("required_change"))
            labels = (
                ("Review concern", review_concern),
                ("Paper location", paper_location),
                ("Required change", required_change),
            )
            for label, value in labels:
                if value:
                    indented = "\n   ".join(value.splitlines())
                    lines.extend([f"   **{label}:** {indented}", ""])

    sections = (
        ("Experiments and Analysis", "experiments_and_analysis"),
        (
            "Manuscript and Related-Work Edits",
            "manuscript_and_related_work_edits",
        ),
        (
            "Figures, Tables, Formulas, Writing, and Typos",
            "figures_tables_formulas_writing_and_typos",
        ),
        ("Final Verification", "final_verification"),
    )
    for heading, key in sections:
        value = _text(plan.get(key))
        if value:
            lines.extend([f"## {heading}", "", value, ""])
    return "\n".join(lines).strip()


def validate_rendered_review(
    markdown: str,
    review_fields: Sequence[str],
    *,
    require_revision_plan: bool = False,
    forbidden_review_fields: Sequence[str] = (),
) -> list[str]:
    """Return missing, duplicated, or improperly nested contract headings."""

    required = [
        "# Target Venue",
        "# Reviewed as if submitted to",
        "# Desk Rejection Assessment",
        "# Expected Review Outcome",
        *(f"## {field}" for field in review_fields),
        "# Disclaimer",
    ]
    if require_revision_plan:
        required.append(f"# {DETAILED_REVISION_HEADING}")
    failures: list[str] = []
    for heading in required:
        count = len(re.findall(rf"(?m)^{re.escape(heading)}\s*$", markdown))
        if count != 1:
            failures.append(f"{heading} (count={count})")

    for field in forbidden_review_fields:
        heading = f"## {field}"
        count = len(re.findall(rf"(?m)^{re.escape(heading)}\s*$", markdown))
        if count:
            failures.append(f"{heading} must be absorbed into the revision plan (count={count})")

    if require_revision_plan:
        revision_offset = markdown.rfind(f"# {DETAILED_REVISION_HEADING}")
        disclaimer_offset = markdown.rfind("# Disclaimer")
        if revision_offset <= disclaimer_offset:
            failures.append(
                f"# {DETAILED_REVISION_HEADING} must be the final top-level section"
            )

    canonical = {_heading_key(field): str(field) for field in review_fields}
    current_field = ""
    in_fence = False
    for line in str(markdown or "").splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
        if match is None:
            continue
        level = len(match.group(1))
        label = re.sub(r"\s+#+\s*$", "", match.group(2)).strip()
        contract_field = canonical.get(_heading_key(label))
        if level == 2:
            current_field = contract_field or ""
        elif level == 1:
            current_field = ""
        elif level >= 3 and contract_field is not None:
            owner = current_field or "another section"
            failures.append(
                f"nested venue field heading: {contract_field} inside {owner}"
            )
    return failures


def merge_candidate_groups(
    groups: Sequence[Sequence[dict[str, Any]]],
    *,
    target_count: int = 20,
) -> list[dict[str, Any]]:
    """Round-robin and deduplicate S2 results without semantic proxy ranking."""

    interleaved: list[dict[str, Any]] = []
    width = max((len(group) for group in groups), default=0)
    for index in range(width):
        for group in groups:
            if index < len(group):
                interleaved.append(dict(group[index]))

    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for candidate in interleaved:
        key = _candidate_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(candidate)
        if len(merged) >= max(1, int(target_count)):
            break
    return merged


def slugify(value: str, *, fallback: str = "paper") -> str:
    """Create a stable, portable filename component."""

    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
    return slug[:100] or fallback


def review_filename(title: str, venue: str, timestamp: str) -> str:
    """Build the standard Omni review filename from venue, title, and time."""

    return (
        f"omni-review-{slugify(venue, fallback='venue')}-"
        f"{slugify(title, fallback='paper')}-"
        f"{slugify(timestamp, fallback='timestamp')}.md"
    )


def unique_path(path: str | Path) -> Path:
    """Avoid overwriting an existing review artifact."""

    candidate = Path(path)
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        alternative = candidate.with_name(f"{candidate.stem}-{index}{candidate.suffix}")
        if not alternative.exists():
            return alternative
    raise RuntimeError(f"Could not allocate a unique output path near {candidate}")


def _candidate_key(candidate: dict[str, Any]) -> str:
    for field in ("paper_id", "doi", "arxiv_id"):
        value = " ".join(str(candidate.get(field) or "").split()).casefold()
        if value:
            return f"{field}:{value}"
    title = " ".join(str(candidate.get("title") or "").split()).casefold()
    return f"title:{title}" if title else ""


def _text(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        text = "\n".join(f"- {part}" for part in parts)
    elif value is None:
        text = ""
    else:
        text = str(value).strip()
    text = re.sub(r"^```(?:markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _markdown_headings(value: str) -> list[str]:
    """Return ATX Markdown headings outside fenced code blocks."""

    headings: list[str] = []
    in_fence = False
    for line in str(value or "").splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match is None:
            continue
        headings.append(re.sub(r"\s+#+\s*$", "", match.group(1)).strip())
    return headings


def _heading_key(value: Any) -> str:
    """Normalize a venue field or Markdown heading for exact label matching."""

    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def compact_candidates(
    candidates: Iterable[dict[str, Any]],
    *,
    abstract_chars: int = 2400,
) -> list[dict[str, Any]]:
    """Bound retrieved metadata before it enters the synthesis prompt."""

    keys = ("title", "authors", "year", "venue", "doi", "arxiv_id", "url")
    compact: list[dict[str, Any]] = []
    for candidate in candidates:
        item = {key: candidate.get(key) for key in keys if candidate.get(key)}
        summary = " ".join(str(candidate.get("summary") or "").split())
        if summary:
            item["abstract"] = summary[:abstract_chars]
        compact.append(item)
    return compact
