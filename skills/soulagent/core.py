from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from graph_pruner import prune_graph
from kg_decoder import DECODER_CONTRACT_VERSION, decode_subgraph
from kg_loader import KGValidationError, load_kg
from remote_registry import (
    DEFAULT_REGISTRY_URL,
    RemoteInstallError,
    RemoteRegistryError,
    RemoteScientistAmbiguous,
    RemoteScientistNotFound,
    resolve_and_install_remote_scientist,
)
from stoma_writer import HOST_STOMA, StomaError, unload_persona, write_persona
from task_sensor import inherit_task_context, objective_similarity, sense_task


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


def _loaded_persona(
    project_root: Path, state: dict[str, Any] | None
) -> tuple[bool, str | None]:
    """Return the committed persona state, never a bare or partial stoma."""
    if not state:
        return False, None
    scientist_id = str(state.get("scientist_id") or "").strip()
    host = str(state.get("host") or "").strip()
    stoma_names = HOST_STOMA.get(host)
    if not scientist_id or not stoma_names:
        return False, None

    lock_dir = project_root / ".soulagent" / "lock"
    if (lock_dir / "writing").exists() or not (lock_dir / "ready").is_file():
        return False, None
    try:
        if not all(
            (project_root / name).is_file()
            and (project_root / name).stat().st_size > 0
            for name in stoma_names
        ):
            return False, None
    except OSError:
        return False, None
    return True, scientist_id


def _pipeline_result(
    *,
    project_root: Path,
    state: dict[str, Any] | None,
    status: str,
    missing_kg: bool = False,
    invalid_kg: bool = False,
    needs_input: bool = False,
    **payload: Any,
) -> dict[str, Any]:
    """Build every ``run_pipeline`` return with the stable persona contract."""
    loaded, active_scientist_id = _loaded_persona(project_root, state)
    normalized_status = str(status).strip().casefold()
    if not loaded and normalized_status in {"ok", "succeeded", "refreshed"}:
        status = "error"
        payload.setdefault(
            "error",
            "SoulAgent 输出契约拒绝成功状态：科学家人格尚未完成加载。",
        )
    payload.update(
        {
            "status": status,
            "loaded": loaded,
            "missing_kg": bool(missing_kg),
            "invalid_kg": bool(invalid_kg),
            "needs_input": bool(needs_input),
            "active_scientist_id": active_scientist_id,
        }
    )
    return payload


def _distillation_offer(
    *,
    project_root: Path,
    state: dict[str, Any] | None,
    requested_scientist: str,
    reason: str,
    kg_root: Path,
    missing_kg: bool = True,
    invalid_kg: bool = False,
    registry_url: str = DEFAULT_REGISTRY_URL,
) -> dict[str, Any]:
    """Return a terminal user-confirmation boundary; never fabricate a KG."""
    question = (
        f"本地和远端人格仓库都没有可用的“{requested_scientist}”人格。"
        "是否调用 scientist-kg-distiller 现做一份？"
    )
    return _pipeline_result(
        project_root=project_root,
        state=state,
        status="needs_input",
        missing_kg=missing_kg,
        invalid_kg=invalid_kg,
        needs_input=True,
        outcome={"code": "distillation_confirmation_required"},
        action_required={
            "kind": "configure",
            "action": "confirm_scientist_distillation",
            "skill": "scientist-kg-distiller",
            "requested_scientist": requested_scientist,
            "distiller_input": {
                "scientist": requested_scientist,
                "project_root": str(project_root),
                "install_root": str(kg_root),
            },
        },
        recovery_choices=[
            {
                "id": "distill",
                "label": "是，调用蒸馏器",
                "skill": "scientist-kg-distiller",
            },
            {"id": "cancel", "label": "否，不创建人格"},
        ],
        offer_distillation=True,
        distiller_skill="scientist-kg-distiller",
        host_must_not_fabricate=True,
        remote_checked=True,
        registry_url=registry_url,
        requested_scientist=requested_scientist,
        error=reason,
        message=question,
        summary=question,
        text=question,
    )


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
    if state.get("decoder_contract_version") != DECODER_CONTRACT_VERSION:
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


def list_scientists(kg_root: str | Path) -> dict[str, list[dict[str, Any]]]:
    root = Path(kg_root).resolve()
    if not root.is_dir():
        return {"scientists": [], "invalid": []}
    scientists: list[dict[str, Any]] = []
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
                "aliases": [
                    str(value)
                    for value in kg["identity"].get("aliases") or []
                    if str(value).strip()
                ],
                "path": str(candidate),
            }
        )
    return {"scientists": scientists, "invalid": invalid}


def _normal_scientist_name(value: str) -> str:
    return re.sub(r"[\s_.·•,'’\-]+", "", value.casefold())


_LOCAL_NAME_ALIASES = {
    "alan-turing": ("图灵", "艾伦·图灵", "艾伦图灵"),
    "claude-shannon": ("香农", "克劳德·香农", "克劳德香农"),
    "fengli-xu": ("徐丰力", "Xu Fengli"),
    "herbert-a-simon": ("赫伯特·西蒙", "赫伯特西蒙"),
    "john-von-neumann": ("冯·诺伊曼", "冯诺伊曼", "约翰·冯·诺伊曼"),
    "kaiming-he": ("何恺明",),
    "norbert-wiener": ("维纳", "诺伯特·维纳", "诺伯特维纳"),
    "richard-feynman": ("费曼", "理查德·费曼", "理查德费曼"),
}


def resolve_scientist_request(
    request: str,
    scientists: list[dict[str, Any]],
    *,
    contains: bool = False,
) -> tuple[str | None, str | None]:
    """Resolve a local scientist by ID, canonical name, or declared alias."""
    query = _normal_scientist_name(request)
    matches: list[str] = []
    for row in scientists:
        scientist_id = str(row.get("scientist_id") or "")
        forms = {
            _normal_scientist_name(scientist_id),
            _normal_scientist_name(str(row.get("scientist_name") or "")),
            *(
                _normal_scientist_name(str(alias))
                for alias in row.get("aliases") or []
            ),
            *(
                _normal_scientist_name(alias)
                for alias in _LOCAL_NAME_ALIASES.get(scientist_id, ())
            ),
        }
        forms.discard("")
        matched = any(form in query for form in forms) if contains else query in forms
        if matched:
            matches.append(scientist_id)
    matches = list(dict.fromkeys(matches))
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "科学家名称存在歧义：" + ", ".join(matches)
    return None, f"找不到科学家：{request}"


def run_pipeline(
    *,
    project_root: str | Path,
    kg_root: str | Path,
    conversation: str | list[dict[str, Any]] | list[str],
    scientist_id: str | None = None,
    host: str = "claude",
    force: bool = False,
    completion_fn=None,
    task_completion_fn=None,
    registry_url: str = DEFAULT_REGISTRY_URL,
) -> dict[str, Any]:
    project = Path(project_root).resolve()
    if host not in HOST_STOMA:
        raise SoulAgentError(
            f"不支持的 Host：{host}；可选值为 {', '.join(HOST_STOMA)}"
        )
    state = _read_state(project)
    kg_base = Path(kg_root).expanduser().resolve()
    requested_scientist = scientist_id or (
        str(state["scientist_id"]) if state and state.get("scientist_id") else None
    )
    if not requested_scientist:
        return _pipeline_result(
            project_root=project,
            state=state,
            status="needs_input",
            needs_input=True,
            error="尚未选择科学家；请先指定 scientist_id",
        )

    explicitly_requested = scientist_id is not None
    selected_scientist = str(requested_scientist)
    remote_install: dict[str, Any] | None = None
    inventory = list_scientists(kg_base)
    if explicitly_requested:
        local_id, local_error = resolve_scientist_request(
            selected_scientist,
            list(inventory.get("scientists") or []),
        )
        if local_id is not None:
            selected_scientist = local_id
        elif local_error and "歧义" in local_error:
            return _pipeline_result(
                project_root=project,
                state=state,
                status="needs_input",
                needs_input=True,
                error=local_error,
                requested_scientist=selected_scientist,
            )
        else:
            try:
                remote_install = resolve_and_install_remote_scientist(
                    kg_base,
                    selected_scientist,
                    registry_url=registry_url,
                )
                selected_scientist = str(remote_install["scientist_id"])
            except RemoteScientistNotFound as exc:
                return _distillation_offer(
                    project_root=project,
                    state=state,
                    requested_scientist=selected_scientist,
                    reason=str(exc),
                    kg_root=kg_base,
                    registry_url=registry_url,
                )
            except RemoteScientistAmbiguous as exc:
                return _pipeline_result(
                    project_root=project,
                    state=state,
                    status="needs_input",
                    missing_kg=True,
                    needs_input=True,
                    error=str(exc),
                    requested_scientist=selected_scientist,
                    remote_checked=True,
                    registry_url=registry_url,
                )
            except RemoteInstallError as exc:
                return _distillation_offer(
                    project_root=project,
                    state=state,
                    requested_scientist=selected_scientist,
                    reason=str(exc),
                    kg_root=kg_base,
                    missing_kg=not (kg_base / selected_scientist).exists(),
                    invalid_kg=True,
                    registry_url=registry_url,
                )
            except RemoteRegistryError as exc:
                return _pipeline_result(
                    project_root=project,
                    state=state,
                    status="error",
                    missing_kg=True,
                    needs_input=False,
                    outcome={"code": "remote_lookup_failed"},
                    error=str(exc),
                    requested_scientist=selected_scientist,
                    remote_checked=False,
                    registry_url=registry_url,
                    recoverable=True,
                    blocking=True,
                )
    elif not kg_base.is_dir():
        return _pipeline_result(
            project_root=project,
            state=state,
            status="needs_input",
            missing_kg=True,
            needs_input=True,
            error=f"科学家 KG 根目录不存在：{kg_base}",
            kg_path=str(kg_base),
        )

    scientist_kg_path = kg_base / selected_scientist
    if not scientist_kg_path.is_dir():
        return _pipeline_result(
            project_root=project,
            state=state,
            status="needs_input",
            missing_kg=True,
            needs_input=True,
            error=f"科学家 KG 目录不存在：{scientist_kg_path}",
            requested_scientist_id=selected_scientist,
            kg_path=str(scientist_kg_path),
        )

    try:
        kg = load_kg(scientist_kg_path)
    except KGValidationError as exc:
        return _pipeline_result(
            project_root=project,
            state=state,
            status="needs_input",
            invalid_kg=True,
            needs_input=True,
            error=str(exc),
            requested_scientist_id=selected_scientist,
            kg_path=str(scientist_kg_path),
        )

    actual_scientist_id = str(kg["scientist_id"])
    if actual_scientist_id != selected_scientist:
        return _pipeline_result(
            project_root=project,
            state=state,
            status="needs_input",
            invalid_kg=True,
            needs_input=True,
            error=(
                "KG scientist_id 与目录选择不一致："
                f"{actual_scientist_id} != {selected_scientist}"
            ),
            requested_scientist_id=selected_scientist,
            kg_path=str(scientist_kg_path),
        )

    task_frame = sense_task(conversation, completion_fn=task_completion_fn)
    if task_frame is None:
        return _pipeline_result(
            project_root=project,
            state=state,
            status="no_scientific_task",
            refreshed=False,
        )
    task_frame = inherit_task_context(
        task_frame,
        state.get("task_frame") if state else None,
        conversation,
    )

    loaded, _ = _loaded_persona(project, state)
    if loaded and not force and not _needs_refresh(
        state, actual_scientist_id, kg["manifest_sha256"], task_frame, host
    ):
        return _pipeline_result(
            project_root=project,
            state=state,
            status="unchanged_task",
            refreshed=False,
            scientist_id=actual_scientist_id,
            task_frame=task_frame,
        )

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
        "decoder_contract_version": DECODER_CONTRACT_VERSION,
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
    return _pipeline_result(
        project_root=project,
        state=new_state,
        status="refreshed",
        refreshed=True,
        scientist_id=actual_scientist_id,
        scientist_name=kg["identity"]["scientist_name"],
        task_frame=task_frame,
        seed_categories=[
            node["category"]
            for node in subgraph["active_l2"]
            if node["activation_role"] == "seed"
        ],
        active_categories=[node["category"] for node in subgraph["active_l2"]],
        tension_resolved=subgraph["tension_resolved"],
        evidence_count=len(subgraph["l1_evidence"]),
        stoma_paths=stoma_paths,
        **({"remote_install": remote_install} if remote_install else {}),
    )


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
    scientist_name = str(state.get("scientist_name") or "").strip()
    return {
        "status": "unloaded",
        "project_root": str(project),
        "host": host,
        "scientist_name": scientist_name,
        "instruct_host": (
            f"科学家人格 {scientist_name} 已卸载。此时无任何科学家人格处于加载状态。"
        ),
    }


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
        "--kg-root",
        default=None,
        help="科学家 KG 根目录；兼容项目目录，否则默认 ~/.omni/scientist-kg",
    )
    parser.add_argument(
        "--registry-url",
        default=DEFAULT_REGISTRY_URL,
        help="可信远端科学家人格注册表 URL",
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
    project_kg = project / "scientist-kg"
    kg_root = (
        Path(args.kg_root).expanduser().resolve()
        if args.kg_root
        else project_kg if project_kg.is_dir() else Path.home() / ".omni" / "scientist-kg"
    )
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
                registry_url=args.registry_url,
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
