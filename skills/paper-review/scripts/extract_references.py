#!/usr/bin/env python3
"""Extract rough reference entries from paper text."""

from __future__ import annotations

import argparse
import json
import re

REFERENCE_HEADING_RE = re.compile(r"(?im)^\s*(references|bibliography)\s*$")
POST_REFERENCE_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(appendix|appendices|supplementary\s+material|supplemental\s+material|"
    r"acknowledg(?:e)?ments?|author\s+contributions?|ethics\s+statement|broader\s+impact|"
    r"reproducibility\s+checklist|checklist)\b.*$"
)
NUMBERED_REFERENCE_START_RE = re.compile(r"^(\[\d+\]|\d+\.|\(\w+.*?\d{4}\))\s+")
AUTHOR_YEAR_REFERENCE_START_RE = re.compile(
    r"^[A-Z][^.\n]{1,220}\.\s+.+\b(19|20)\d{2}\b"
)


def _looks_like_reference_start(line: str) -> bool:
    return bool(NUMBERED_REFERENCE_START_RE.match(line) or AUTHOR_YEAR_REFERENCE_START_RE.match(line))


def reference_section(text: str) -> str:
    matches = list(REFERENCE_HEADING_RE.finditer(text or ""))
    if not matches:
        return ""
    boundary_positions = [match.start() for match in POST_REFERENCE_BOUNDARY_RE.finditer(text or "")]
    first_boundary = boundary_positions[0] if boundary_positions else None
    candidates: list[tuple[int, int, int, str]] = []
    for index, match in enumerate(matches):
        section = text[match.end() :]
        relative_boundary = POST_REFERENCE_BOUNDARY_RE.search(section)
        if relative_boundary:
            section = section[: relative_boundary.start()]
        section = section.strip()
        if not section:
            continue
        entries = split_reference_entries(section)
        entry_count = len(entries)
        if entry_count == 0:
            continue
        before_appendix = 1 if first_boundary is None or match.start() < first_boundary else 0
        candidates.append((before_appendix, entry_count, -index, section))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[:3])[3]


def split_reference_entries(ref_text: str) -> list[str]:
    if not ref_text.strip():
        return []
    lines = [line.strip() for line in ref_text.splitlines() if line.strip()]
    entries: list[str] = []
    current: list[str] = []
    for line in lines:
        if _looks_like_reference_start(line) and current:
            entries.append(" ".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        entries.append(" ".join(current))
    if len(entries) <= 1:
        entries = re.split(r"\n(?=\s*(?:\[\d+\]|\d+\.))", ref_text.strip())
    return [entry.strip() for entry in entries if entry.strip()]


def parse_reference_entry(entry: str) -> dict:
    year_match = re.search(r"\b(19|20)\d{2}\b", entry)
    title = entry
    quoted = re.search(r"[\"“](.*?)[\"”]", entry)
    if quoted:
        title = quoted.group(1)
    else:
        parts = re.split(r"\.\s+", entry, maxsplit=3)
        if len(parts) >= 2:
            title = parts[1]
    authors_part = entry.split(".", 1)[0]
    authors = [author.strip() for author in re.split(r",| and ", authors_part) if author.strip()]
    return {
        "raw": entry,
        "title": title.strip(),
        "authors": authors,
        "year": int(year_match.group(0)) if year_match else None,
    }


def extract_references(text: str) -> list[dict]:
    return [parse_reference_entry(entry) for entry in split_reference_entries(reference_section(text))]


def load_input_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"]
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract reference entries from paper text.")
    parser.add_argument("input")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = extract_references(load_input_text(args.input))
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
