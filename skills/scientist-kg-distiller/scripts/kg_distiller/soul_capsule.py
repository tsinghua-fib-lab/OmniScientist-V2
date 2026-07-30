from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

from .http_client import HttpClient
from .io_utils import read_json, write_json
from .kg_store import read_kg_store, update_delivery_manifest_for_capsule

CAPSULE_VERSION = "1.2.0"
TEMPLATE_DIR = Path(__file__).parent / "capsule_template"


def build_soul_capsule(
    project_root: Path,
    scientist_id: str,
    *,
    result_root: Path | None = None,
    portrait_url: str | None = None,
    portrait_source_url: str | None = None,
    http: HttpClient | None = None,
) -> Path:
    if not TEMPLATE_DIR.exists():
        raise FileNotFoundError(f"Missing soul capsule template: {TEMPLATE_DIR}")

    delivery_dir = (result_root or project_root / "result") / scientist_id
    store_dir = delivery_dir / "kg"
    kg = read_kg_store(store_dir)
    profile = read_json(store_dir / "identity.json")
    profile = {
        **profile,
        "fields": profile.get("research_fields") or [],
        "biography_sources": profile.get("sources") or [],
    }
    output_dir = delivery_dir / "capsule"
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_DIR, output_dir, dirs_exist_ok=True)

    portrait = _collect_portrait(
        profile,
        assets_dir,
        portrait_url=portrait_url,
        portrait_source_url=portrait_source_url,
        http=http or HttpClient(),
    )
    capsule = _capsule_data(kg, profile, portrait)
    _write_capsule_data(output_dir, capsule)
    write_json(
        output_dir / "manifest.json",
        {
            "capsule_version": CAPSULE_VERSION,
            "scientist_id": scientist_id,
            "kg": "../kg.json",
            "entrypoint": "index.html",
            "data_layout": {
                "overview": "data/overview.js",
                "tone": "data/tone.js",
                "values": "data/values.js",
                "relations": "data/relations.js",
                "pattern_index": "data/patterns/index.js",
                "pattern_details": "data/patterns/c01.js ... c07.js",
            },
        },
    )
    (delivery_dir / "README.md").write_text(
        _delivery_readme(scientist_id), encoding="utf-8", newline="\n"
    )
    update_delivery_manifest_for_capsule(delivery_dir, scientist_id)
    index_path = output_dir / "index.html"
    if not index_path.exists():
        raise FileNotFoundError(f"Capsule template has no index.html: {output_dir}")
    return index_path


def _write_capsule_data(output_dir: Path, capsule: dict[str, Any]) -> None:
    data_dir = output_dir / "data"
    patterns_dir = data_dir / "patterns"
    patterns_dir.mkdir(parents=True, exist_ok=True)
    overview = {
        "capsule_version": capsule["capsule_version"],
        "generated_at": capsule["generated_at"],
        "kg_file": capsule["kg_file"],
        "meta": capsule["meta"],
        "identity": capsule["identity"],
        "personality_type": capsule["personality_type"],
        "portrait": capsule["portrait"],
        "soul_core": capsule["soul_core"],
    }
    index = [
        {key: pattern[key] for key in ("id", "category", "label", "evidence_count")}
        for pattern in capsule["patterns"]
    ]
    _write_js_data(data_dir / "overview.js", "overview", overview)
    _write_js_data(data_dir / "tone.js", "tone", capsule["tone"])
    _write_js_data(data_dir / "values.js", "values", capsule["values"])
    _write_js_data(
        data_dir / "relations.js", "relations", capsule["relations"]
    )
    _write_js_data(patterns_dir / "index.js", "patternIndex", index)
    for pattern in capsule["patterns"]:
        _write_js_data(
            patterns_dir / f"{str(pattern['category']).lower()}.js",
            f"patterns.{pattern['category']}",
            pattern,
        )


def _write_js_data(path: Path, key: str, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2).replace(
        "<", "\\u003c"
    )
    parts = key.split(".")
    lines = ["window.SOUL_CAPSULE = window.SOUL_CAPSULE || {};"]
    cursor = "window.SOUL_CAPSULE"
    for part in parts[:-1]:
        cursor += f"[{json.dumps(part)}]"
        lines.append(f"{cursor} = {cursor} || {{}};")
    final = f"{cursor}[{json.dumps(parts[-1])}]"
    lines.append(f"{final} = {encoded};")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _delivery_readme(scientist_id: str) -> str:
    return f"""# {scientist_id} personality delivery

- `kg/`: canonical progressively readable personality knowledge graph.
- `kg.json`: automatically generated portable bundle of the same graph.
- `capsule/index.html`: human-facing soul capsule; open directly in a browser.
- `capsule/data/`: progressively separated overview, tone exemplars, values,
  relations, and one detail file per cognitive pattern.
- `capsule/assets/portrait-source.json`: portrait provenance and crawl audit.

The pipeline workspace and extraction audits remain outside this delivery
folder.
"""


def _collect_portrait(
    profile: dict[str, Any],
    assets_dir: Path,
    *,
    portrait_url: str | None,
    portrait_source_url: str | None,
    http: HttpClient,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    candidates: list[tuple[str, str, str]] = []
    if portrait_url:
        candidates.append(
            (
                portrait_url,
                portrait_source_url or portrait_url,
                "explicit_official_source",
            )
        )
    stored_url = profile.get("portrait_url")
    if isinstance(stored_url, str) and stored_url:
        candidates.append(
            (
                stored_url,
                str(profile.get("portrait_source_url") or stored_url),
                "verified_profile",
            )
        )
    candidates.extend(_wikidata_portrait_candidates(profile, http, attempts))
    candidates.extend(_official_profile_portrait_candidates(profile, http, attempts))

    for image_url, source_url, method in candidates:
        try:
            data, headers = http.get_bytes(image_url)
            content_type = str(headers.get_content_type() or "")
            suffix = _image_suffix(content_type, image_url, data)
            if suffix is None:
                raise ValueError(
                    f"portrait response is not a supported image ({content_type})"
                )
            target = assets_dir / f"portrait{suffix}"
            target.write_bytes(data)
            record = {
                "status": "available",
                "method": method,
                "image_url": image_url,
                "source_page": source_url,
                "local_path": f"assets/{target.name}",
                "content_type": content_type,
                "sha256": hashlib.sha256(data).hexdigest(),
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "attempts": attempts,
            }
            write_json(assets_dir / "portrait-source.json", record)
            return record
        except Exception as exc:  # noqa: BLE001 - try the next portrait source
            attempts.append(
                {
                    "method": method,
                    "image_url": image_url,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    record = {
        "status": "unavailable",
        "local_path": None,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "attempts": attempts,
    }
    write_json(assets_dir / "portrait-source.json", record)
    return record


def _official_profile_portrait_candidates(
    profile: dict[str, Any], http: HttpClient, attempts: list[dict[str, Any]]
) -> list[tuple[str, str, str]]:
    name = _normalize_name(str(profile.get("scientist_name") or ""))
    candidates: list[tuple[str, str, str]] = []
    for source in profile.get("biography_sources") or []:
        source_url = str(source)
        if not source_url.startswith(("https://", "http://")):
            continue
        try:
            parser = _ImageParser()
            parser.feed(http.get_text(source_url))
            ranked = []
            for attrs in parser.images:
                src = attrs.get("src", "")
                if not src:
                    continue
                alt = _normalize_name(attrs.get("alt", ""))
                classes = f"{attrs.get('class', '')} {attrs.get('id', '')}".lower()
                score = 2 if name and name in alt else 1 if any(
                    value in classes for value in ("avatar", "portrait", "photo")
                ) else 0
                if score:
                    ranked.append((score, urljoin(source_url, src)))
            if ranked:
                _, image_url = max(ranked, key=lambda item: item[0])
                image_url = image_url.replace(" ", "%20")
                candidates.append((image_url, source_url, "official_profile_image"))
        except Exception as exc:  # noqa: BLE001 - optional profile image must degrade
            attempts.append(
                {"method": "official_profile_image", "source_page": source_url, "error_type": type(exc).__name__, "error": str(exc)}
            )
    return candidates


class _ImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            self.images.append({key.lower(): value or "" for key, value in attrs})


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", value.casefold())


def _wikidata_portrait_candidates(
    profile: dict[str, Any],
    http: HttpClient,
    attempts: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    for source in profile.get("biography_sources") or []:
        match = re.search(r"wikidata\.org/wiki/(Q\d+)", str(source))
        if not match:
            continue
        qid = match.group(1)
        entity_url = (
            f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
        )
        try:
            entity = http.get_json(entity_url)["entities"][qid]
            claims = entity.get("claims", {}).get("P18", [])
            if not claims:
                attempts.append(
                    {
                        "method": "wikidata_P18",
                        "source_page": str(source),
                        "result": "no_P18_claim",
                    }
                )
                continue
            filename = claims[0]["mainsnak"]["datavalue"]["value"]
            image_url = (
                "https://commons.wikimedia.org/wiki/Special:Redirect/file/"
                + quote(str(filename))
            )
            candidates.append((image_url, str(source), "wikidata_P18"))
        except Exception as exc:  # noqa: BLE001 - optional Wikidata image must degrade
            attempts.append(
                {
                    "method": "wikidata_P18",
                    "source_page": str(source),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
    return candidates


def _image_suffix(
    content_type: str, url: str, data: bytes
) -> str | None:
    known = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    if content_type in known:
        return known[content_type]
    guessed = mimetypes.guess_type(url)[0]
    if guessed in known:
        return known[guessed]
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _capsule_data(
    kg: dict[str, Any],
    profile: dict[str, Any],
    portrait: dict[str, Any],
) -> dict[str, Any]:
    l1_nodes = list(kg["L1_facts"])
    l2_nodes = list(kg["L2_patterns"])
    l3_nodes = [
        node
        for node in kg["L3_stances"]
        if node.get("question") in {"P01", "P02", "P03"}
    ]
    tone_node = next(
        (
            node
            for node in kg["L3_stances"]
            if node.get("question") == "P04"
        ),
        None,
    )
    if tone_node is None:
        raise ValueError("Soul capsule requires the P04 tone node")
    l1_by_id = {str(node["node_id"]): node for node in l1_nodes}
    l1_by_parent: dict[str, list[dict[str, Any]]] = {}
    for node in l1_nodes:
        l1_by_parent.setdefault(str(node["parent_L2"]), []).append(node)

    patterns = []
    for node in l2_nodes:
        evidence = _representative_evidence(
            l1_by_parent.get(str(node["node_id"]), [])
        )
        patterns.append(
            {
                "id": node["node_id"],
                "category": node["category"],
                "label": node["category_label"],
                "description": node["description"],
                "evidence_count": node["supporting_L1_count"],
                "trigger_contexts": node["trigger_contexts"],
                "contraindicated_contexts": node[
                    "contraindicated_contexts"
                ],
                "evidence": evidence,
            }
        )

    identity = next(
        (
            node.get("identity_context")
            for node in l3_nodes
            if node.get("question") == "P03"
        ),
        None,
    ) or {
        "scientist_name": profile.get("scientist_name"),
        "aliases": profile.get("aliases") or [],
        "occupations": profile.get("occupations") or [],
        "research_fields": profile.get("fields") or [],
        "education_history": profile.get("education_history") or [],
        "employment_history": profile.get("employment_history") or [],
        "institutions": profile.get("institutions") or [],
        "sources": profile.get("biography_sources") or [],
    }
    p01 = next(node for node in l3_nodes if node["question"] == "P01")
    p03 = next(node for node in l3_nodes if node["question"] == "P03")
    source_titles = {str(node["source_title"]) for node in l1_nodes}
    years = [
        int(node["year"])
        for node in l1_nodes
        if isinstance(node.get("year"), int)
    ]
    graph_relations = {
        key: list(kg["edges"].get(key) or [])
        for key in ("summarizes", "reinforces", "enables", "tension")
    }
    return {
        "capsule_version": CAPSULE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kg_file": "../kg.json",
        "meta": {
            **kg["meta"],
            "source_count": len(source_titles),
            "year_start": min(years) if years else None,
            "year_end": max(years) if years else None,
        },
        "identity": identity,
        "personality_type": _personality_type(str(p03["stance"])),
        "portrait": portrait,
        "soul_core": [
            {
                "id": node["node_id"],
                "question": node["question"],
                "label": node["question_label"],
                "stance": node["stance"],
                "explanation": node["explanation"],
                "summarized_patterns": list(
                    node.get("summarized_from_L2") or []
                ),
                "evidence": [
                    _compact_evidence(l1_by_id[evidence_id])
                    for evidence_id in node.get("exemplar_L1") or []
                    if evidence_id in l1_by_id
                ],
            }
            for node in l3_nodes
        ],
        "tone": {
            "id": tone_node["node_id"],
            "question": tone_node["question"],
            "label": tone_node["question_label"],
            "tone_exemplars": list(tone_node["tone_exemplars"]),
        },
        "values": list(p01.get("value_dimensions") or []),
        "patterns": patterns,
        "relations": graph_relations,
    }


def _representative_evidence(
    nodes: list[dict[str, Any]], limit: int = 5
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used_sources: set[str] = set()
    ordered = sorted(
        nodes,
        key=lambda node: (
            node.get("year") is None,
            node.get("year") or 0,
            str(node.get("source_title") or ""),
        ),
    )
    for node in ordered:
        source = str(node.get("source_id") or node.get("source_title") or "")
        if source in used_sources:
            continue
        selected.append(_compact_evidence(node))
        used_sources.add(source)
        if len(selected) == limit:
            return selected
    for node in ordered:
        if node["node_id"] in {item["id"] for item in selected}:
            continue
        selected.append(_compact_evidence(node))
        if len(selected) == limit:
            break
    return selected


def _compact_evidence(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node["node_id"],
        "paper": node["source_title"],
        "year": node.get("year"),
        "observation": node["observation"],
        "excerpt": node["quote_or_excerpt"],
        "section": node.get("location", {}).get("section"),
    }


def _personality_type(stance: str) -> str:
    match = re.search(r"自视为一位([^。；]+)", stance)
    if match:
        return match.group(1).strip()
    sentences = [
        value.strip()
        for value in re.split(r"[。；]", stance)
        if value.strip()
    ]
    return sentences[-1][:80] if sentences else "证据驱动的科学研究者"
