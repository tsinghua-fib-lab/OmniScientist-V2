"""Model-visible citation anchors vs grounded sources.

B0 settlement contract: if a task already has grounded sources and a writing
deliverable exists, the draft must contain at least one model-visible anchor
(``[S#]``, a recorded ``source_id``, or a citation key). Settlement reads the
recorded ``citation.anchors`` fact and does not re-grade prose.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CITATION_ANCHORS_EVENT = "citation.anchors"

_S_ANCHOR_RE = re.compile(r"\[S(\d+)\]")
_ARXIV_RE = re.compile(r"\b(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?\b", re.IGNORECASE)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
_SOURCE_ID_RE = re.compile(r"\b(?:src|source)[_-]?[A-Za-z0-9]{6,40}\b", re.IGNORECASE)

_WRITING_SUFFIXES = frozenset({".md", ".markdown", ".tex", ".txt", ".html"})
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})


@dataclass(frozen=True, slots=True)
class CitationAnchorReport:
    grounded_source_ids: tuple[str, ...] = ()
    visible_anchors: tuple[str, ...] = ()
    uncovered: bool = False
    notice: str = ""

    @property
    def grounded_count(self) -> int:
        return len(self.grounded_source_ids)

    @property
    def anchor_count(self) -> int:
        return len(self.visible_anchors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "grounded_source_ids": list(self.grounded_source_ids),
            "visible_anchors": list(self.visible_anchors),
            "uncovered": self.uncovered,
            "grounded_count": self.grounded_count,
            "anchor_count": self.anchor_count,
            "notice": self.notice,
        }


def is_scholarly_citation_key(token: str) -> bool:
    """Whether ``token`` is an arXiv id or DOI, not a ROM ``source_id``.

    B0 treats these as model-visible anchors. B1 must not compare them to
    hash ``source_id``s and call the mismatch dangling.
    """
    item = str(token or "").strip()
    if not item:
        return False
    if _DOI_RE.search(item):
        return True
    return bool(_ARXIV_RE.fullmatch(item) or _ARXIV_RE.fullmatch(item.removeprefix("arxiv:")))


def extract_visible_anchors(
    text: str,
    *,
    source_ids: Sequence[str] = (),
    citation_keys: Sequence[str] = (),
) -> list[str]:
    """Return model-visible anchors found in ``text``.

    An anchor is ``[S#]``, a grounded ``source_id`` substring, or a citation
    key (DOI / arXiv id / caller-supplied key). Any one of these is enough for
    the structural B0 gate; semantic support is a later warning tier.
    """
    body = str(text or "")
    if not body:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        item = str(token or "").strip()
        if not item or item in seen:
            return
        seen.add(item)
        found.append(item)

    for match in _S_ANCHOR_RE.finditer(body):
        add(f"[S{match.group(1)}]")
    for source_id in source_ids:
        token = str(source_id or "").strip()
        if token and token in body:
            add(token)
    for key in citation_keys:
        token = str(key or "").strip()
        if token and token in body:
            add(token)
    for match in _ARXIV_RE.finditer(body):
        add(match.group(1))
    for match in _DOI_RE.finditer(body):
        add(match.group(0))
    return found


def citation_anchor_gate(
    draft_text: str,
    grounded_source_ids: Sequence[str],
    *,
    citation_keys: Sequence[str] | None = None,
) -> CitationAnchorReport:
    """Structural gate: sources exist and the draft shows none of them.

    No grounded sources → not uncovered (do not punish a contextual draft).
    Empty draft → not this gate (missing-file honesty handles that).
    """
    sources = tuple(
        dict.fromkeys(str(item).strip() for item in grounded_source_ids if str(item).strip())
    )
    draft = str(draft_text or "")
    if not sources or not draft.strip():
        return CitationAnchorReport(grounded_source_ids=sources)
    anchors = tuple(
        extract_visible_anchors(draft, source_ids=sources, citation_keys=citation_keys or ())
    )
    if anchors:
        return CitationAnchorReport(
            grounded_source_ids=sources,
            visible_anchors=anchors,
        )
    notice = (
        f"Grounded upstream sources were available ({len(sources)}), "
        "but the draft contains none of their model-visible citation anchors."
    )
    return CitationAnchorReport(
        grounded_source_ids=sources,
        visible_anchors=(),
        uncovered=True,
        notice=notice,
    )


def apply_synthesis_citation_gate(result: dict[str, Any]) -> dict[str, Any]:
    """Stamp ``citation_anchors`` onto a native-synthesis payload and degrade if needed."""
    provenance = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
    source_ids = [
        str(item).strip()
        for item in (provenance.get("source_ids") or [])
        if str(item).strip()
    ]
    draft = _body_without_host_provenance(
        str(result.get("draft_markdown") or result.get("text") or "")
    )
    report = citation_anchor_gate(draft, source_ids)
    previous = result.get("citation_anchors") if isinstance(result.get("citation_anchors"), dict) else {}
    result["citation_anchors"] = report.to_dict()
    if report.uncovered:
        result["status"] = "partial"
        warning = str(result.get("warning") or "").strip()
        if report.notice and report.notice not in warning:
            result["warning"] = f"{warning} {report.notice}".strip() if warning else report.notice
        return result
    if previous.get("uncovered") and result.get("status") == "partial":
        if result.get("synthesis_mode") == "llm" or result.get("upstream_summaries"):
            result["status"] = "ok"
        warning = str(result.get("warning") or "")
        notice = str(previous.get("notice") or "")
        if notice and notice in warning:
            result["warning"] = warning.replace(notice, "").strip()
            if not result["warning"]:
                result.pop("warning", None)
    return result


def _body_without_host_provenance(draft: str) -> str:
    """Drop the template provenance footer so B0 judges the manuscript body."""
    lines = [
        line
        for line in str(draft or "").splitlines()
        if not line.startswith("Evidence status:")
    ]
    return "\n".join(lines)


def collect_source_ids(*payloads: Any) -> list[str]:
    """Walk tool results / events for grounded ``source_ids``."""
    found: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        token = str(value or "").strip()
        if not token or token in seen:
            return
        seen.add(token)
        found.append(token)

    def walk(value: Any, *, key: str = "") -> None:
        if isinstance(value, dict):
            raw = value.get("source_ids")
            if isinstance(raw, list):
                for item in raw:
                    add(item)
            single = value.get("source_id")
            if single:
                add(single)
            for item_key, item in value.items():
                if item_key in {"source_ids", "source_id"}:
                    continue
                walk(item, key=str(item_key))
            return
        if isinstance(value, list):
            for item in value:
                walk(item, key=key)
            return
        if key == "source_ids":
            add(value)

    for payload in payloads:
        walk(payload)
    return found


def collect_citation_keys(*payloads: Any) -> list[str]:
    """DOI / arXiv / title keys the model can cite without repeating a source_id."""
    keys: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        token = str(value or "").strip()
        if not token or token in seen:
            return
        seen.add(token)
        keys.append(token)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for field in ("doi", "arxiv_id", "dedup_key", "title"):
                add(value.get(field))
            for item in value.values():
                if isinstance(item, (dict, list)):
                    walk(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)

    for payload in payloads:
        walk(payload)
    return keys


def collect_s_index_map(*payloads: Any) -> dict[str, str]:
    """Map ``S#`` labels from corpus/literature hits onto source ids when present."""
    mapping: dict[str, str] = {}

    def consider(index: int, item: Any) -> None:
        if not isinstance(item, dict):
            return
        source_id = str(item.get("source_id") or item.get("id") or "").strip()
        label = str(item.get("cite") or item.get("label") or "").strip()
        if label.startswith("[") and label.endswith("]"):
            key = label
        elif label.upper().startswith("S") and label[1:].isdigit():
            key = f"[{label.upper()}]"
        else:
            key = f"[S{index}]"
        if source_id:
            mapping.setdefault(key, source_id)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            matches = value.get("matches") or value.get("results") or value.get("passages")
            if isinstance(matches, list):
                for index, item in enumerate(matches, start=1):
                    consider(index, item)
            for item in value.values():
                if isinstance(item, (dict, list)):
                    walk(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)

    for payload in payloads:
        walk(payload)
    return mapping


def is_writing_path(path: str) -> bool:
    suffix = Path(str(path or "")).suffix.lower()
    return suffix in _WRITING_SUFFIXES


def draft_text_from_writes(tool_trace: Sequence[Any]) -> str:
    """Concatenate writing-tool payloads the model already produced."""
    chunks: list[str] = []
    for record in tool_trace or []:
        name = str(getattr(record, "name", "") or "")
        if name not in _WRITE_TOOLS:
            continue
        arguments = getattr(record, "arguments", None)
        if not isinstance(arguments, dict):
            continue
        path = str(arguments.get("path") or "")
        if path and not is_writing_path(path):
            continue
        if name == "write_file":
            text = str(arguments.get("contents") or "")
            if text.strip():
                chunks.append(text)
            continue
        if name == "apply_patch":
            text = str(arguments.get("patch") or arguments.get("diff") or "")
            if text.strip():
                chunks.append(text)
    return "\n\n".join(chunks)


def draft_text_from_synthesis(drained: Sequence[dict[str, Any]] | None) -> str:
    chunks: list[str] = []
    for item in drained or []:
        if not isinstance(item, dict):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else item
        if not isinstance(result, dict):
            continue
        text = str(result.get("draft_markdown") or result.get("text") or "")
        if text.strip() and (
            result.get("synthesis_mode")
            or result.get("deliverable")
            or result.get("evidence_level")
        ):
            chunks.append(text)
    return "\n\n".join(chunks)


def read_writing_artifact_text(artifacts: Iterable[Any]) -> str:
    """Best-effort read of manuscript-like artifact files already on disk."""
    chunks: list[str] = []
    for artifact in artifacts:
        path = str(
            getattr(artifact, "path", "")
            or getattr(artifact, "rel_path", "")
            or ""
        )
        if not path:
            continue
        suffix = Path(path).suffix.lower()
        mime = str(getattr(artifact, "mime", "") or "").lower()
        kind = str(getattr(artifact, "kind", "") or "").lower()
        writing = (
            suffix in _WRITING_SUFFIXES
            or mime.startswith("text/")
            or kind in {"report", "paper", "manuscript", "document"}
        )
        if not writing:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        if text.strip():
            chunks.append(text)
    return "\n\n".join(chunks)


def collect_turn_draft_text(
    *,
    tool_trace: Sequence[Any] = (),
    drained: Sequence[dict[str, Any]] | None = None,
    artifacts: Iterable[Any] = (),
    extra_text: str = "",
) -> str:
    parts = [
        read_writing_artifact_text(artifacts),
        draft_text_from_synthesis(drained),
        draft_text_from_writes(tool_trace),
        str(extra_text or "").strip(),
    ]
    return "\n\n".join(part for part in parts if part.strip())


def uncovered_citation_event(events: Sequence[Any]) -> bool:
    """Whether the last ``citation.anchors`` fact says the draft is uncovered."""
    last: Any = None
    for event in events:
        if str(getattr(event, "event_type", "") or "") == CITATION_ANCHORS_EVENT:
            last = event
    if last is None:
        return False
    payload = getattr(last, "output_json", None) or {}
    if isinstance(payload, dict) and payload.get("uncovered") is True:
        return True
    return str(getattr(last, "status", "") or "") == "degraded"


__all__ = [
    "CITATION_ANCHORS_EVENT",
    "CitationAnchorReport",
    "apply_synthesis_citation_gate",
    "citation_anchor_gate",
    "collect_citation_keys",
    "collect_s_index_map",
    "collect_source_ids",
    "collect_turn_draft_text",
    "draft_text_from_synthesis",
    "draft_text_from_writes",
    "extract_visible_anchors",
    "is_scholarly_citation_key",
    "is_writing_path",
    "read_writing_artifact_text",
    "uncovered_citation_event",
]
