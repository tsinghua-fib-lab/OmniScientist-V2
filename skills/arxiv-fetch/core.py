"""Portable core for arxiv-fetch.

No Omni imports live here. Both the Omni engine adapter and the standalone
runner call this module so the lookup behavior stays aligned.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any


SKILL = "arxiv-fetch"
ATOM = "{http://www.w3.org/2005/Atom}"


def normalize_arxiv_id(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    match = re.search(r"(\d{4}\.\d{4,5})(?:v\d+)?", text)
    if match:
        return match.group(1)
    match = re.search(r"([a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", text, re.IGNORECASE)
    return match.group(1) if match else ""


def _text(node: ET.Element | None, name: str) -> str:
    if node is None:
        return ""
    child = node.find(f"{ATOM}{name}")
    return " ".join((child.text or "").split()) if child is not None else ""


def fetch(identifier: str, timeout: float = 20.0) -> dict[str, Any]:
    arxiv_id = normalize_arxiv_id(identifier)
    if not arxiv_id:
        return {
            "status": "error",
            "skill": SKILL,
            "outcome": {"code": "invalid_identifier"},
            "error": "identifier must contain an arXiv id or arXiv URL",
            "recoverable": False,
            "blocking": True,
        }

    params = urllib.parse.urlencode({"id_list": arxiv_id})
    url = f"https://export.arxiv.org/api/query?{params}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = response.read()
    except Exception as exc:  # noqa: BLE001 - portable boundary
        return {
            "status": "error",
            "skill": SKILL,
            "arxiv_id": arxiv_id,
            "outcome": {"code": "network_error"},
            "error": f"unable to reach arXiv: {exc}",
            "recoverable": True,
            "blocking": False,
        }

    root = ET.fromstring(data)
    entries = root.findall(f"{ATOM}entry")
    if not entries:
        warning = f"arXiv did not return a paper for {arxiv_id}"
        return {
            "status": "partial",
            "skill": SKILL,
            "arxiv_id": arxiv_id,
            "outcome": {"code": "not_found"},
            "summary": warning,
            "warning": warning,
            "recoverable": True,
            "blocking": False,
            "error_info": {
                "code": "not_found",
                "message": warning,
                "retryable": True,
                "workflow_recoverable": True,
            },
        }

    entry = entries[0]
    authors = [_text(author, "name") for author in entry.findall(f"{ATOM}author")]
    pdf_url = ""
    abs_url = _text(entry, "id")
    for link in entry.findall(f"{ATOM}link"):
        if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
            pdf_url = link.attrib.get("href", "")
            break
    categories = [cat.attrib.get("term", "") for cat in entry.findall(f"{ATOM}category")]
    title = _text(entry, "title")
    summary = _text(entry, "summary")
    return {
        "status": "ok",
        "skill": SKILL,
        "outcome": {"code": "found"},
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": [a for a in authors if a],
        "summary": summary,
        "published": _text(entry, "published"),
        "updated": _text(entry, "updated"),
        "abs_url": abs_url,
        "pdf_url": pdf_url,
        "categories": [c for c in categories if c],
        "sources": [{"kind": "arxiv", "id": arxiv_id, "url": abs_url, "title": title}],
        "provenance": {"sources": [{"arxiv_id": arxiv_id, "url": abs_url, "title": title}]},
    }
