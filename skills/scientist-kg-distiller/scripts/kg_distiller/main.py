from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kg_distiller.edge_completion import complete_edges
from kg_distiller.evidence_card import extract_evidence_cards
from kg_distiller.http_client import HttpClient
from kg_distiller.identity import resolve_identity
from kg_distiller.ids import scientist_slug
from kg_distiller.ingestion import MaterialIngestor
from kg_distiller.io_utils import read_json, read_jsonl, write_json
from kg_distiller.kg_encoder import encode_kg
from kg_distiller.kg_store import install_kg_store, validate_kg_store, write_kg_store
from kg_distiller.l2_induction import induce_l2
from kg_distiller.l3_abstraction import abstract_l3
from kg_distiller.llm import JsonLLM, OpenAIJsonLLM
from kg_distiller.prompts import L2_CATEGORIES, L3_QUESTIONS, PROMPT_VERSION
from kg_distiller.schemas import (
    SCHEMA_VERSION,
    validate_evidence_card,
    validate_source_object,
)
from kg_distiller.soul_capsule import build_soul_capsule

PIPELINE_VERSION = "2.3.0"
STEPS = (
    "identity",
    "collect",
    "ingest",
    "evidence",
    "l2",
    "l3",
    "edges",
    "kg",
    "capsule",
)
LLM_STEPS = {"evidence", "l2", "l3", "edges"}


def distill(
    project_root: Path,
    scientist_id: str | None = None,
    *,
    scientist_name: str | None = None,
    field: str | None = None,
    institution: str | None = None,
    identity_candidate: str | None = None,
    google_scholar_url: str | None = None,
    selected_step: str | None = None,
    resume: bool = False,
    model: str | None = None,
    max_sources: int = 200,
    result_root: Path | None = None,
    install_root: Path | None = None,
    portrait_url: str | None = None,
    portrait_source_url: str | None = None,
    llm: JsonLLM | None = None,
    http: HttpClient | None = None,
) -> list[Path]:
    if not scientist_id and not scientist_name:
        raise ValueError("Provide scientist_name or scientist_id")
    resolved_id = scientist_id or scientist_slug(scientist_name or "")
    corpus = project_root / "scientist-corpus" / resolved_id
    state_path = corpus / "distiller_state.json"
    state = _load_state(state_path, resolved_id)
    delivery_root = result_root or project_root / "result"
    outputs = _outputs(project_root, delivery_root, resolved_id)
    steps = [selected_step] if selected_step else list(STEPS)
    produced: list[Path] = []
    active_llm = llm

    for step in steps:
        output = outputs[step]
        input_hash = _input_digest(
            step,
            project_root,
            delivery_root,
            resolved_id,
            {
                "scientist_name": scientist_name,
                "field": field,
                "institution": institution,
                "identity_candidate": identity_candidate,
                "google_scholar_url": google_scholar_url,
                "max_sources": max_sources,
                "model": model,
                "portrait_url": portrait_url,
                "portrait_source_url": portrait_source_url,
            },
        )
        if resume and _can_resume(
            step, output, input_hash, state.get("steps", {}).get(step)
        ):
            state["steps"][step]["resumed_at"] = _now()
            write_json(state_path, state)
            continue
        state["current_step"] = step
        state["steps"][step] = {
            "status": "running",
            "input_hash": input_hash,
            "started_at": _now(),
        }
        write_json(state_path, state)
        try:
            if step == "identity":
                result = _run_identity(
                    project_root,
                    resolved_id,
                    scientist_name,
                    field,
                    institution,
                    identity_candidate,
                    google_scholar_url,
                    http,
                )
            elif step == "collect":
                result = MaterialIngestor(
                    project_root, http=http, max_sources=max_sources
                ).collect(resolved_id)
            elif step == "ingest":
                result = MaterialIngestor(
                    project_root, http=http, max_sources=max_sources
                ).ingest(resolved_id)
            elif step == "kg":
                bundle = encode_kg(project_root, resolved_id)
                result = write_kg_store(
                    read_json(bundle), delivery_root, resolved_id
                )
            elif step == "capsule":
                result = build_soul_capsule(
                    project_root,
                    resolved_id,
                    result_root=delivery_root,
                    portrait_url=portrait_url,
                    portrait_source_url=portrait_source_url,
                    http=http,
                )
            else:
                if active_llm is None:
                    active_llm = OpenAIJsonLLM(model=model)
                operation: Callable[..., Path] = {
                    "evidence": extract_evidence_cards,
                    "l2": induce_l2,
                    "l3": abstract_l3,
                    "edges": complete_edges,
                }[step]
                result = operation(project_root, resolved_id, active_llm)
            _validate_artifact(step, result)
            produced.append(result)
            state["steps"][step] = {
                "status": "completed",
                "input_hash": input_hash,
                "output": str(result),
                "output_hash": _path_digest(result),
                "pipeline_version": PIPELINE_VERSION,
                "schema_version": SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION if step in LLM_STEPS else None,
                "model": (
                    getattr(active_llm, "model", model) if step in LLM_STEPS else None
                ),
                "completed_at": _now(),
            }
            state["last_completed_step"] = step
            state["current_step"] = None
            write_json(state_path, state)
        except Exception as exc:
            state["steps"][step] = {
                "status": "failed",
                "input_hash": input_hash,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "failed_at": _now(),
            }
            state["current_step"] = None
            write_json(state_path, state)
            raise
    if install_root is not None:
        installed_manifest = install_kg_store(
            delivery_root / resolved_id / "kg",
            install_root,
            resolved_id,
        )
        produced.append(installed_manifest)
    return produced


def _run_identity(
    project_root: Path,
    scientist_id: str,
    scientist_name: str | None,
    field: str | None,
    institution: str | None,
    identity_candidate: str | None,
    google_scholar_url: str | None,
    http: HttpClient | None,
) -> Path:
    profile = project_root / "scientist-corpus" / scientist_id / "profile.json"
    if not scientist_name:
        if not profile.exists():
            raise FileNotFoundError(
                f"Missing {profile}; provide --scientist to resolve identity"
            )
        return profile
    _, profile_path = resolve_identity(
        project_root,
        scientist_name,
        scientist_id=scientist_id,
        field=field,
        institution=institution,
        selected_candidate_id=identity_candidate,
        google_scholar_url=google_scholar_url,
        http=http,
    )
    return profile_path


def _outputs(
    root: Path, result_root: Path, scientist_id: str
) -> dict[str, Path]:
    corpus = root / "scientist-corpus" / scientist_id
    return {
        "identity": corpus / "profile.json",
        "collect": corpus / "source_candidates.jsonl",
        "ingest": corpus / "source_objects.jsonl",
        "evidence": root / "evidence_cards" / f"{scientist_id}.jsonl",
        "l2": root / "l2" / f"{scientist_id}_l2.json",
        "l3": root / "l3" / f"{scientist_id}_l3.json",
        "edges": root / "edges" / f"{scientist_id}_edges.json",
        "kg": result_root / scientist_id / "kg" / "manifest.json",
        "capsule": result_root / scientist_id / "capsule" / "index.html",
    }


def _step_input_paths(
    root: Path, result_root: Path, scientist_id: str, step: str
) -> list[Path]:
    corpus = root / "scientist-corpus" / scientist_id
    mapping = {
        "identity": [],
        "collect": [corpus / "profile.json", corpus / "sources.json", corpus / "raw"],
        "ingest": [
            corpus / "profile.json",
            corpus / "source_candidates.jsonl",
        ],
        "evidence": [corpus / "source_objects.jsonl"],
        "l2": [root / "evidence_cards" / f"{scientist_id}.jsonl"],
        "l3": [
            corpus / "profile.json",
            corpus / "source_objects.jsonl",
            root / "evidence_cards" / f"{scientist_id}.jsonl",
            root / "l2" / f"{scientist_id}_l2.json",
        ],
        "edges": [
            root / "l2" / f"{scientist_id}_l2.json",
            root / "l3" / f"{scientist_id}_l3.json",
        ],
        "kg": [
            corpus / "profile.json",
            root / "evidence_cards" / f"{scientist_id}.jsonl",
            root / "l2" / f"{scientist_id}_assignments.json",
            root / "l2" / f"{scientist_id}_l2.json",
            root / "l3" / f"{scientist_id}_l3.json",
            root / "edges" / f"{scientist_id}_edges.json",
        ],
        "capsule": [
            result_root / scientist_id / "kg",
        ],
    }
    paths = mapping[step]
    if step == "ingest":
        candidate_path = corpus / "source_candidates.jsonl"
        if candidate_path.exists():
            for candidate in read_jsonl(candidate_path):
                local_path = candidate.get("local_path")
                if local_path:
                    paths.append(Path(local_path))
    return paths


def _input_digest(
    step: str,
    root: Path,
    result_root: Path,
    scientist_id: str,
    parameters: dict[str, Any],
) -> str:
    digest = hashlib.sha256()
    digest.update(PIPELINE_VERSION.encode())
    digest.update(SCHEMA_VERSION.encode())
    if step in LLM_STEPS:
        digest.update(PROMPT_VERSION.encode())
    relevant_parameters = {
        "identity": {
            key: parameters[key]
            for key in (
                "scientist_name",
                "field",
                "institution",
                "identity_candidate",
            )
        },
        "collect": {"max_sources": parameters["max_sources"]},
        "ingest": {"max_sources": parameters["max_sources"]},
        "evidence": {"model": parameters["model"]},
        "l2": {"model": parameters["model"]},
        "l3": {"model": parameters["model"]},
        "edges": {"model": parameters["model"]},
        "kg": {},
        "capsule": {
            "portrait_url": parameters["portrait_url"],
            "portrait_source_url": parameters["portrait_source_url"],
        },
    }[step]
    digest.update(
        json.dumps(relevant_parameters, sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
    )
    for path in sorted(
        _step_input_paths(root, result_root, scientist_id, step),
        key=lambda value: str(value),
    ):
        digest.update(str(path).encode("utf-8"))
        digest.update(_path_digest(path).encode("ascii"))
    return digest.hexdigest()


def _path_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        digest.update(b"<missing>")
        return digest.hexdigest()
    if path.is_file():
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    for child in sorted(
        (item for item in path.rglob("*") if item.is_file()),
        key=lambda value: str(value.relative_to(path)),
    ):
        digest.update(str(child.relative_to(path)).encode("utf-8"))
        with child.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _can_resume(
    step: str,
    output: Path,
    input_hash: str,
    record: dict[str, Any] | None,
) -> bool:
    if not record or record.get("status") != "completed":
        return False
    if record.get("input_hash") != input_hash:
        return False
    if record.get("output_hash") != _path_digest(output):
        return False
    try:
        _validate_artifact(step, output)
    except (ValueError, OSError, KeyError, TypeError):
        return False
    return True


def _validate_artifact(step: str, path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError(f"{step} output is missing or empty: {path}")
    if step == "identity":
        value = read_json(path)
        if not isinstance(value, dict) or not value.get("scientist_name"):
            raise ValueError("identity profile requires scientist_name")
        return
    if step in {"collect", "ingest", "evidence"}:
        rows = read_jsonl(path)
        if not rows and step != "evidence":
            raise ValueError(f"{step} output contains no rows")
        if step == "ingest":
            for row in rows:
                validate_source_object(row)
        if step == "evidence":
            for row in rows:
                validate_evidence_card(row)
        return
    if step == "capsule":
        manifest = path.parent / "manifest.json"
        if not manifest.exists():
            raise ValueError("capsule delivery requires manifest.json")
        validate_kg_store(path.parent.parent / "kg")
        return
    if step == "kg":
        validate_kg_store(path.parent)
        return
    value = read_json(path)
    if step == "l2":
        if (
            not isinstance(value, list)
            or len(value) != 7
            or {node.get("category") for node in value} != set(L2_CATEGORIES)
        ):
            raise ValueError("L2 output must contain C01-C07 exactly once")
    elif step == "l3":
        if (
            not isinstance(value, list)
            or len(value) != 4
            or {node.get("question") for node in value}
            != {*L3_QUESTIONS, "P04"}
        ):
            raise ValueError("L3 output must contain P01-P04 exactly once")
    elif step == "edges" and (
        not isinstance(value, dict)
        or set(value)
        != {
            "reinforces",
            "enables",
            "tension",
            "summarizes",
        }
    ):
        raise ValueError("edge output has invalid edge groups")


def _load_state(path: Path, scientist_id: str) -> dict[str, Any]:
    if path.exists():
        value = read_json(path)
        if isinstance(value, dict):
            value.setdefault("steps", {})
            return value
    return {
        "scientist_id": scientist_id,
        "pipeline_version": PIPELINE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "steps": {},
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect academic materials and build a scientist personality KG."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("distill", help="run all or one pipeline step")
    command.add_argument(
        "--scientist",
        help="scientist name; resolves identity and derives scientist_id",
    )
    command.add_argument(
        "--scientist-id",
        help="stable corpus ID; required only for an existing/manual profile",
    )
    command.add_argument("--field", help="optional field hint for identity resolution")
    command.add_argument(
        "--institution", help="optional institution hint for identity resolution"
    )
    command.add_argument(
        "--identity-candidate",
        help="explicit candidate ID after an ambiguity report",
    )
    command.add_argument(
        "--google-scholar-url",
        help="optional public Google Scholar author URL; verifies and enriches the selected identity",
    )
    command.add_argument("--step", choices=STEPS)
    command.add_argument("--resume", action="store_true")
    command.add_argument("--model")
    command.add_argument("--max-sources", type=int, default=200)
    command.add_argument(
        "--result-root",
        type=Path,
        default=Path.cwd() / "result",
        help="clean final deliverables, grouped by scientist ID",
    )
    command.add_argument(
        "--install-root",
        type=Path,
        help=(
            "optional SoulAgent scanner root; atomically installs the validated KG "
            "as <install-root>/<scientist-id> without overwriting"
        ),
    )
    command.add_argument(
        "--portrait-url",
        help="optional verified portrait image URL for the soul capsule",
    )
    command.add_argument(
        "--portrait-source-url",
        help="source page documenting the portrait URL",
    )
    command.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
    )
    args = parser.parse_args(argv)
    if args.command == "distill" and not (args.scientist or args.scientist_id):
        parser.error("distill requires --scientist or --scientist-id")
    if args.command == "distill" and args.max_sources < 1:
        parser.error("--max-sources must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        outputs = distill(
            args.project_root.resolve(),
            args.scientist_id,
            scientist_name=args.scientist,
            field=args.field,
            institution=args.institution,
            identity_candidate=args.identity_candidate,
            google_scholar_url=args.google_scholar_url,
            selected_step=args.step,
            resume=args.resume,
            model=args.model,
            max_sources=args.max_sources,
            result_root=args.result_root.resolve(),
            install_root=(
                args.install_root.expanduser().resolve() if args.install_root else None
            ),
            portrait_url=args.portrait_url,
            portrait_source_url=args.portrait_source_url,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports provider failures cleanly
        print(f"distill failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if outputs:
        for output in outputs:
            print(output)
    else:
        print("All requested artifacts passed strict resume validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
