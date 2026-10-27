"""Shared bibliographic fields for prepared sources and audited content plans."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FIELDS = ("title", "authors", "affiliations")


def from_request(input_data: Mapping[str, Any]) -> dict[str, Any]:
    """Read caller identity, giving explicit fields precedence over research metadata."""

    research = input_data.get("research")
    metadata = research if isinstance(research, Mapping) else {}
    selected = {
        field: input_data.get(field, metadata.get(field))
        for field in (*FIELDS, "author_notes")
        if field in input_data or field in metadata
    }
    if (
        "authors" in input_data and "author_notes" not in input_data
        and "author_notes" in metadata
    ):
        requested_authors = normalize({"authors": input_data["authors"]}).get("authors")
        previous_authors = normalize({"authors": metadata.get("authors", "")}).get("authors")
        if requested_authors != previous_authors:
            selected.pop("author_notes", None)
    return normalize(selected)


def normalize(value: object) -> dict[str, Any]:
    """Validate identity once at the caller or planning response boundary."""

    if not isinstance(value, Mapping) or set(value) - {*FIELDS, "author_notes"}:
        raise ValueError("paper_identity must contain only title, authors, affiliations and author_notes")
    identity: dict[str, Any] = {}
    title = value.get("title", "")
    if not isinstance(title, str):
        raise ValueError("paper_identity.title must be a string")
    if title.strip():
        identity["title"] = title.strip()
    authors = value.get("authors", "")
    if isinstance(authors, list) and all(isinstance(name, str) for name in authors):
        authors = ", ".join(name.strip() for name in authors if name.strip())
    if not isinstance(authors, str):
        raise ValueError("paper_identity.authors must be a name string or an array of names")
    if authors.strip():
        identity["authors"] = authors.strip()
    affiliations = value.get("affiliations", [])
    if not isinstance(affiliations, list) or any(
        not isinstance(item, str) for item in affiliations
    ):
        raise ValueError("paper_identity.affiliations must be a string array")
    if affiliations:
        identity["affiliations"] = [item.strip() for item in affiliations if item.strip()]
    if "author_notes" in value:
        notes = value["author_notes"]
        if not isinstance(notes, list):
            raise ValueError("paper_identity.author_notes must be an array of author-bound notes")
        normalized_notes = []
        for note in notes:
            if (
                not isinstance(note, Mapping) or set(note) != {"authors", "text"}
                or not isinstance(note["text"], str) or not note["text"].strip()
                or not isinstance(note["authors"], list) or not note["authors"]
                or any(not isinstance(name, str) or not name.strip() for name in note["authors"])
            ):
                raise ValueError("paper_identity.author_notes require author names and note text")
            author_line = ", " + ", ".join(part.strip() for part in authors.split(",")) + ", "
            if any(
                ", " + name.strip() + ", " not in author_line for name in note["authors"]
            ):
                raise ValueError("paper_identity.author_notes must refer to names in authors")
            normalized_notes.append({
                "authors": list(dict.fromkeys(name.strip() for name in note["authors"])),
                "text": note["text"].strip(),
            })
        identity["author_notes"] = normalized_notes
    return identity


def merge(
    prepared: Mapping[str, Any], inferred: Mapping[str, Any]
) -> dict[str, Any]:
    """Fill missing identity fields from the plan while retaining prepared metadata."""

    identity = {
        field: prepared.get(field) or inferred.get(field)
        for field in FIELDS
        if prepared.get(field) or inferred.get(field)
    }
    for source in (prepared, inferred):
        if "author_notes" in source and source.get("authors") == identity.get("authors"):
            identity["author_notes"] = source["author_notes"]
            break
    return identity


def warnings(identity: Mapping[str, Any]) -> list[str]:
    """Expose missing bibliographic fields alongside a usable grounded draft."""

    missing = [field for field in ("title", "authors") if not identity.get(field)]
    if not missing:
        return []
    return [
        "Paper identity is incomplete: " + ", ".join(missing)
        + ". Supply these fields with the grounding source to complete the title band."
    ]
