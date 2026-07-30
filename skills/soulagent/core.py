from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from graph_pruner import prune_graph
from kg_decoder import decode_subgraph
from kg_loader import KGValidationError, load_kg
from stoma_writer import HOST_STOMA, StomaError, unload_persona, write_persona
from task_sensor import objective_similarity, sense_task


class SoulAgentError(RuntimeError):
    pass


def _state_path(project_root: Path) -> Path:
    return project_root / ".soulagent" / "state.json"


def _read_state(project_root: Path) -> dict[str, Any] | None:
    path = _state_path(project_root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SoulAgentError(f"SoulAgent 状态文件损坏：{path}: {exc}") from exc


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _needs_refresh(
    state: dict[str, Any] | None,
    scientist_id: str,
    manifest_sha256: str,
    task_frame: dict[str, Any],
    host: str,
) -> bool:
    if state is None:
        return True
    if state.get("scientist_id") != scientist_id:
        return True
    if state.get("manifest_sha256") != manifest_sha256:
        return True
    if state.get("host") != host:
        return True
    previous = state.get("task_frame") or {}
    if previous.get("phase") != task_frame.get("phase"):
        return True
    if previous.get("constraints") != task_frame.get("constraints"):
        return True
    return (
        objective_similarity(
            str(previous.get("objective", "")), str(task_frame.get("objective", ""))
        )
        < 0.6
    )


def list_scientists(kg_root: str | Path) -> dict[str, list[dict[str, str]]]:
    root = Path(kg_root).resolve()
    if not root.is_dir():
        return {"scientists": [], "invalid": []}
    scientists: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    for candidate in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            kg = load_kg(candidate)
        except KGValidationError as exc:
            invalid.append({"directory": candidate.name, "error": str(exc)})
            continue
        scientists.append(
            {
                "scientist_id": str(kg["scientist_id"]),
                "scientist_name": str(kg["identity"]["scientist_name"]),
                "path": str(candidate),
            }
        )
    return {"scientists": scientists, "invalid": invalid}


def run_pipeline(
    *,
    project_root: str | Path,
    kg_root: str | Path,
    conversation: str | list[dict[str, Any]] | list[str],
    scientist_id: str | None = None,
    host: str = "claude",
    force: bool = False,
    completion_fn=None,
) -> dict[str, Any]:
    project = Path(project_root).resolve()
    if host not in HOST_STOMA:
        raise SoulAgentError(
            f"不支持的 Host：{host}；可选值为 {', '.join(HOST_STOMA)}"
        )
    state = _read_state(project)
    selected_scientist = scientist_id or (
        str(state["scientist_id"]) if state and state.get("scientist_id") else None
    )
    if not selected_scientist:
        raise SoulAgentError("尚未选择科学家；请先指定 scientist_id")

    task_frame = sense_task(conversation)
    if task_frame is None:
        return {"status": "no_scientific_task", "refreshed": False}

    kg = load_kg(Path(kg_root) / selected_scientist)
    actual_scientist_id = str(kg["scientist_id"])
    if actual_scientist_id != selected_scientist:
        raise SoulAgentError(
            f"KG scientist_id 与目录选择不一致：{actual_scientist_id} != {selected_scientist}"
        )

    if not force and not _needs_refresh(
        state, actual_scientist_id, kg["manifest_sha256"], task_frame, host
    ):
        return {
            "status": "unchanged_task",
            "refreshed": False,
            "scientist_id": actual_scientist_id,
            "task_frame": task_frame,
        }

    subgraph = prune_graph(task_frame, kg)
    persona_text = decode_subgraph(
        subgraph, task_frame, completion_fn=completion_fn
    )
    switching = bool(
        state
        and state.get("scientist_id")
        and state.get("scientist_id") != actual_scientist_id
    )
    stoma_paths = write_persona(
        project, persona_text, host, switching_scientist=switching
    )

    new_state = {
        "version": 1,
        "scientist_id": actual_scientist_id,
        "scientist_name": kg["identity"]["scientist_name"],
        "host": host,
        "manifest_sha256": kg["manifest_sha256"],
        "task_frame": task_frame,
        "persona_sha256": hashlib.sha256(persona_text.encode("utf-8")).hexdigest(),
        "stoma_paths": stoma_paths,
    }
    try:
        _atomic_json(_state_path(project), new_state)
    except Exception as exc:
        try:
            unload_persona(project, host)
        finally:
            state_path = _state_path(project)
            if state_path.exists():
                state_path.unlink()
        raise SoulAgentError(
            f"状态提交失败，已恢复原始造口：{exc}"
        ) from exc
    return {
        "status": "refreshed",
        "refreshed": True,
        "scientist_id": actual_scientist_id,
        "scientist_name": kg["identity"]["scientist_name"],
        "task_frame": task_frame,
        "seed_categories": [
            node["category"]
            for node in subgraph["active_l2"]
            if node["activation_role"] == "seed"
        ],
        "active_categories": [node["category"] for node in subgraph["active_l2"]],
        "tension_resolved": subgraph["tension_resolved"],
        "evidence_count": len(subgraph["l1_evidence"]),
        "stoma_paths": stoma_paths,
    }


def unload(project_root: str | Path) -> dict[str, Any]:
    project = Path(project_root).resolve()
    state = _read_state(project)
    if state is None or not state.get("host"):
        raise SoulAgentError("SoulAgent 状态缺少 Host，无法确定应恢复哪个造口")
    host = str(state["host"])
    unload_persona(project, host)
    state_path = _state_path(project)
    if state_path.exists():
        state_path.unlink()
    return {"status": "unloaded", "project_root": str(project), "host": host}


def _load_conversation(args: argparse.Namespace) -> Any:
    if args.conversation_file:
        path = Path(args.conversation_file)
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SoulAgentError(f"无法读取 conversation_file：{path}: {exc}") from exc
    return args.conversation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SoulAgent scientist persona runtime")
    parser.add_argument("--project-root", default=".", help="造口所在项目根目录")
    parser.add_argument(
        "--kg-root", default=None, help="科学家 KG 根目录；默认 <project-root>/scientist-kg"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="扫描并列出可用科学家")

    activate = subparsers.add_parser("activate", help="感知任务并刷新人格造口")
    activate.add_argument("--scientist-id")
    activate.add_argument(
        "--host",
        choices=["workbuddy", "claude", "codex", "omniscientist"],
        default="claude",
        help="当前运行的 Host 类型",
    )
    group = activate.add_mutually_exclusive_group(required=True)
    group.add_argument("--conversation")
    group.add_argument("--conversation-file")
    activate.add_argument("--force", action="store_true")

    subparsers.add_parser("status", help="查看当前 SoulAgent 状态")
    subparsers.add_parser("unload", help="卸载人格并恢复原始造口")
    return parser


def main() -> int:
    args = _parser().parse_args()
    project = Path(args.project_root).resolve()
    kg_root = Path(args.kg_root).resolve() if args.kg_root else project / "scientist-kg"
    try:
        if args.command == "list":
            result: Any = list_scientists(kg_root)
        elif args.command == "activate":
            result = run_pipeline(
                project_root=project,
                kg_root=kg_root,
                conversation=_load_conversation(args),
                scientist_id=args.scientist_id,
                host=args.host,
                force=args.force,
            )
        elif args.command == "status":
            result = _read_state(project) or {"status": "inactive"}
        else:
            result = unload(project)
    except (SoulAgentError, KGValidationError, StomaError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
