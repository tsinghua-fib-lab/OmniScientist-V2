"""OmniScientist adapter for the portable SoulAgent runtime."""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any


def _load_core():
    """Load sibling ``core.py`` without importing Omni internals."""
    skill_dir = Path(__file__).resolve().parent
    candidate = skill_dir / "core.py"
    module_name = f"soulagent_core_{abs(hash(str(candidate)))}"
    spec = importlib.util.spec_from_file_location(module_name, candidate)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {candidate}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    inserted = str(skill_dir) not in sys.path
    if inserted:
        sys.path.insert(0, str(skill_dir))
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        if inserted:
            try:
                sys.path.remove(str(skill_dir))
            except ValueError:
                pass
    return module


_core = _load_core()

TASK_SENSOR_MAX_TOKENS = 512
TASK_SENSOR_TIMEOUT_SECONDS = 30.0
# Skill-local decoder budget, not the agent ReAct reply cap. Keep in sync with
# ``kg_decoder.DECODER_MAX_TOKENS`` / ``DECODER_TIMEOUT_SECONDS``.
PERSONA_DECODER_MAX_TOKENS = 8192
PERSONA_DECODER_TIMEOUT_SECONDS = 120.0


class _OmniCompletion:
    """Expose Omni's async host model as Core's synchronous completion port."""

    def __init__(
        self,
        llm: Any,
        loop: asyncio.AbstractEventLoop,
        *,
        stage_label: str,
        max_tokens: int,
        timeout_seconds: float,
    ) -> None:
        self._llm = llm
        self._loop = loop
        self._stage_label = stage_label
        self._max_tokens = max_tokens
        self._timeout_seconds = timeout_seconds

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        future = asyncio.run_coroutine_threadsafe(
            self._llm.chat_with_tools(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                [],
                tool_choice="auto",
                temperature=0,
                max_tokens=self._max_tokens,
            ),
            self._loop,
        )
        try:
            result = future.result(timeout=self._timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RuntimeError(
                f"SoulAgent {self._stage_label}超时"
                f"（{self._timeout_seconds:g} 秒），已取消本次模型请求"
            ) from exc
        content = str(getattr(result, "content", "") or "").strip()
        if not content:
            if str(getattr(result, "finish_reason", "") or "") == "length":
                raise RuntimeError(
                    f"SoulAgent {self._stage_label}达到输出上限"
                    f"（{self._max_tokens} tokens），且未得到可用文本"
                )
            raise RuntimeError(f"SoulAgent {self._stage_label}收到宿主模型空文本")
        # A length stop with text is not discarded here. ``kg_decoder`` injects
        # canonical sections and retries once in compact form if validation fails.
        return content


_ACTION_ALIASES = {
    "activate": "activate",
    "load": "activate",
    "refresh": "activate",
    "switch": "activate",
    "list": "list",
    "status": "status",
    "unload": "unload",
    "restore": "unload",
}

_KNOWN_NAME_ALIASES = {
    "图灵": "alan-turing",
    "艾伦·图灵": "alan-turing",
    "香农": "claude-shannon",
    "徐丰力": "fengli-xu",
    "赫伯特·西蒙": "herbert-a-simon",
    "冯·诺伊曼": "john-von-neumann",
    "冯诺伊曼": "john-von-neumann",
    "何恺明": "kaiming-he",
    "维纳": "norbert-wiener",
    "费曼": "richard-feynman",
}

_GENERIC_SCIENTIST_REFERENCES = {
    "一个人格",
    "一位科学家",
    "科学家",
    "科学家人格",
    "人格",
    "另一个人格",
    "another scientist",
    "a scientist",
    "scientist",
}


def _normal_name(value: str) -> str:
    return re.sub(r"[\s_.-]+", "", value.casefold())


def _infer_action(text: str, explicit: Any) -> str:
    value = str(explicit or "").strip().casefold()
    if value:
        return _ACTION_ALIASES.get(value, value)
    lowered = text.casefold()
    if any(
        phrase in lowered
        for phrase in ("卸载", "不用科学家", "不用人格", "恢复你自己", "恢复原始", "unload", "restore")
    ):
        return "unload"
    if any(phrase in lowered for phrase in ("有哪些科学家", "列出科学家", "人格列表", "list scientists")):
        return "list"
    if any(
        phrase in lowered
        for phrase in (
            "人格状态",
            "当前人格",
            "谁的人格",
            "现在是谁的人格",
            "当前是谁的人格",
            "soulagent status",
            "whose persona",
        )
    ):
        return "status"
    return "activate"


def _resolve_project_root(ctx: Any, explicit: Any) -> Path:
    """Return the exact folder that owns persona state.

    An explicit ``project_root`` is used as-is. Otherwise the host working
    directory (the folder the user opened or launched in) wins. A parent
    ``scientist-kg`` must not move this root; KG lookup is a separate step.
    """
    if str(explicit or "").strip():
        return Path(str(explicit)).expanduser().resolve()
    if ctx is not None:
        working_dir = getattr(ctx, "working_dir", None)
        if working_dir:
            return Path(working_dir).expanduser().resolve()
    return Path.cwd().resolve()


def _resolve_kg_root(ctx: Any, project_root: Path, explicit: Any) -> Path:
    """Choose the exact scanner root used for local and downloaded KGs."""
    if str(explicit or "").strip():
        return Path(str(explicit)).expanduser().resolve()
    project_kg = project_root / "scientist-kg"
    if project_kg.is_dir():
        return project_kg
    paths = getattr(ctx, "paths", None) if ctx is not None else None
    omni_home = getattr(paths, "home", None) if paths is not None else None
    if omni_home:
        return Path(omni_home).expanduser().resolve() / "scientist-kg"
    return Path.home() / ".omni" / "scientist-kg"


def _explicit_scientist_name(text: str, explicit: Any) -> str | None:
    """Extract only an explicit named-persona request; generic requests return None."""
    requested = str(explicit or "").strip()
    if requested:
        return requested
    patterns = (
        (
            r"(?:用|使用)\s*"
            r"(?P<name>[A-Za-zÀ-ÖØ-öø-ÿ\u3400-\u9fff·. '\-]{2,80}?)"
            r"(?:的(?:方式|思维|视角|人格|风格)|$)"
        ),
        (
            r"(?:装载|加载|启用|切换到|切换为|换成|换为|换)\s*"
            r"(?P<name>[A-Za-zÀ-ÖØ-öø-ÿ\u3400-\u9fff·. '\-]{2,80}?)"
            r"(?:的(?:方式|思维|视角|人格|风格)|来|，|,|。|$)"
        ),
        (
            r"(?:think like|act as|load|activate|switch to)\s+"
            r"(?P<name>[A-Za-z][A-Za-zÀ-ÖØ-öø-ÿ. '\-]{1,80}?)"
            r"(?:\s+(?:persona|perspective|style)|[,.;]|$)"
        ),
        (
            r"use\s+"
            r"(?P<name>[A-Za-z][A-Za-zÀ-ÖØ-öø-ÿ. '\-]{1,80}?)"
            r"(?:\s+(?:persona|perspective|style)|$)"
        ),
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = match.group("name").strip(" ，,。.;")
        if candidate.casefold() not in _GENERIC_SCIENTIST_REFERENCES:
            return candidate
    return None


def _resolve_scientist(
    text: str,
    explicit: Any,
    scientists: list[dict[str, str]],
) -> tuple[str | None, str | None]:
    requested = str(explicit or "").strip()
    if requested:
        exact = [row for row in scientists if row.get("scientist_id") == requested]
        if exact:
            return requested, None
        requested_normal = _normal_name(requested)
    else:
        requested_normal = ""

    text_normal = _normal_name(text)
    matches: list[str] = []
    for row in scientists:
        scientist_id = str(row.get("scientist_id") or "")
        forms = {
            _normal_name(scientist_id),
            _normal_name(str(row.get("scientist_name") or "")),
            *(
                _normal_name(str(alias))
                for alias in row.get("aliases") or []
            ),
        }
        explicit_match = bool(requested_normal and requested_normal in forms)
        inferred_match = bool(
            not requested_normal and any(form and form in text_normal for form in forms)
        )
        if explicit_match or inferred_match:
            matches.append(scientist_id)

    if not requested_normal:
        for alias, scientist_id in _KNOWN_NAME_ALIASES.items():
            if alias in text and any(row.get("scientist_id") == scientist_id for row in scientists):
                matches.append(scientist_id)
    matches = list(dict.fromkeys(matches))
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "科学家名称存在歧义：" + ", ".join(matches)
    if requested:
        return None, f"找不到科学家：{requested}"
    return None, "请求中没有可唯一识别的科学家名称"


def _engine_result(
    *,
    project_root: Path,
    state: dict[str, Any] | None,
    status: str,
    missing_kg: bool = False,
    invalid_kg: bool = False,
    needs_input: bool = False,
    **payload: Any,
) -> dict[str, Any]:
    result = _core._pipeline_result(
        project_root=project_root,
        state=state,
        status=status,
        missing_kg=missing_kg,
        invalid_kg=invalid_kg,
        needs_input=needs_input,
        **payload,
    )
    result["active"] = bool(result["loaded"])
    result.setdefault("project_root", str(project_root))
    return result


def _error(
    message: str,
    code: str,
    *,
    project_root: Path,
    state: dict[str, Any] | None,
    status: str = "error",
    missing_kg: bool = False,
    invalid_kg: bool = False,
    needs_input: bool = False,
    recoverable: bool = True,
    blocking: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    return _engine_result(
        project_root=project_root,
        state=state,
        status=status,
        missing_kg=missing_kg,
        invalid_kg=invalid_kg,
        needs_input=needs_input,
        outcome={"code": code},
        error=message,
        summary=message,
        text=message,
        recoverable=recoverable,
        blocking=blocking,
        **extra,
    )


class SoulAgentEngine:
    @staticmethod
    def validate_params(
        *, arguments: dict | None = None, input_data: dict | None = None
    ) -> dict | None:
        data = arguments or input_data or {}
        if not str(data.get("input") or "").strip():
            return {"error": "input is required"}
        action = str(data.get("action") or "").strip().casefold()
        if action and action not in _ACTION_ALIASES:
            return {"error": f"unsupported action: {action}"}
        return None

    async def execute(
        self, progress_callback: Any = None, **input_data: Any
    ) -> dict[str, Any]:
        request_text = str(input_data.get("input") or "").strip()
        action = _infer_action(request_text, input_data.get("action"))
        ctx = getattr(self, "ctx", None)
        project_root = _resolve_project_root(ctx, input_data.get("project_root"))
        kg_root = _resolve_kg_root(ctx, project_root, input_data.get("kg_root"))
        state = _core._read_state(project_root)
        if action == "status":
            active, active_scientist_id = _core._loaded_persona(project_root, state)
            name = str((state or {}).get("scientist_name") or "")
            return _engine_result(
                project_root=project_root,
                state=state,
                status="ok" if active else "inactive",
                outcome={"code": "active" if active else "inactive"},
                scientist_id=active_scientist_id or "",
                scientist_name=name if active else "",
                summary=f"当前科学家人格：{name}" if active else "当前未装载科学家人格",
                text=f"当前科学家人格：{name}" if active else "当前未装载科学家人格。",
            )

        if action == "unload":
            if not state:
                return _engine_result(
                    project_root=project_root,
                    state=None,
                    status="inactive",
                    outcome={"code": "already_inactive"},
                    summary="当前未装载科学家人格",
                    text="当前未装载科学家人格，无需卸载。",
                )
            try:
                result = await asyncio.to_thread(_core.unload, project_root)
            except Exception as exc:  # noqa: BLE001 - portable Core boundary
                return _error(
                    str(exc),
                    "unload_failed",
                    project_root=project_root,
                    state=state,
                )
            message = str(result.get("instruct_host") or "科学家人格已卸载。")
            return _engine_result(
                project_root=project_root,
                state=None,
                status="unloaded",
                outcome={"code": "unloaded"},
                summary=message,
                text=message,
                **{
                    key: value
                    for key, value in result.items()
                    if key not in {"status", "project_root"}
                },
            )

        inventory = _core.list_scientists(kg_root)
        scientists = list(inventory.get("scientists") or [])
        if action == "list":
            return _engine_result(
                project_root=project_root,
                state=state,
                status="ok" if _core._loaded_persona(project_root, state)[0] else "listed",
                outcome={"code": "listed"},
                scientists=scientists,
                invalid=inventory.get("invalid") or [],
                summary=f"找到 {len(scientists)} 个可用科学家人格",
                text="可用科学家人格：" + ", ".join(
                    f"{row['scientist_name']} ({row['scientist_id']})"
                    for row in scientists
                ),
            )

        scientist_id, resolution_error = _resolve_scientist(
            request_text, input_data.get("scientist_id"), scientists
        )
        explicit_scientist = _explicit_scientist_name(
            request_text, input_data.get("scientist_id")
        )
        remote_install: dict[str, Any] | None = None
        if scientist_id is None and explicit_scientist is not None:
            if progress_callback is not None:
                value = progress_callback("SoulAgent 正在查询远端科学家人格仓库", 0.05)
                if hasattr(value, "__await__"):
                    await value
            try:
                remote_install = await asyncio.to_thread(
                    _core.resolve_and_install_remote_scientist,
                    kg_root,
                    explicit_scientist,
                    registry_url=_core.DEFAULT_REGISTRY_URL,
                )
                scientist_id = str(remote_install["scientist_id"])
                resolution_error = None
                inventory = _core.list_scientists(kg_root)
                scientists = list(inventory.get("scientists") or [])
            except _core.RemoteScientistNotFound as exc:
                result = _core._distillation_offer(
                    project_root=project_root,
                    state=state,
                    requested_scientist=explicit_scientist,
                    reason=str(exc),
                    kg_root=kg_root,
                )
                result.update(
                    {
                        "active": bool(result["loaded"]),
                        "project_root": str(project_root),
                    }
                )
                return result
            except _core.RemoteScientistAmbiguous as exc:
                return _error(
                    str(exc),
                    "remote_scientist_ambiguous",
                    project_root=project_root,
                    state=state,
                    status="needs_input",
                    missing_kg=True,
                    needs_input=True,
                    requested_scientist=explicit_scientist,
                    remote_checked=True,
                )
            except _core.RemoteInstallError as exc:
                result = _core._distillation_offer(
                    project_root=project_root,
                    state=state,
                    requested_scientist=explicit_scientist,
                    reason=str(exc),
                    kg_root=kg_root,
                    missing_kg=not kg_root.is_dir(),
                    invalid_kg=True,
                )
                result.update(
                    {
                        "active": bool(result["loaded"]),
                        "project_root": str(project_root),
                    }
                )
                return result
            except _core.RemoteRegistryError as exc:
                return _error(
                    str(exc),
                    "remote_lookup_failed",
                    project_root=project_root,
                    state=state,
                    status="error",
                    missing_kg=True,
                    recoverable=True,
                    blocking=True,
                    requested_scientist=explicit_scientist,
                    remote_checked=False,
                )

        if scientist_id is None and explicit_scientist is None and state and state.get("scientist_id"):
            scientist_id = str(state["scientist_id"])
            resolution_error = None
        if scientist_id is None:
            invalid = list(inventory.get("invalid") or [])
            if not scientists:
                return _error(
                    "本地没有通过校验的科学家人格；请明确指定要使用的科学家，"
                    "SoulAgent 才会查询远端仓库。",
                    "no_valid_scientist",
                    project_root=project_root,
                    state=state,
                    status="needs_input",
                    missing_kg=not invalid,
                    invalid_kg=bool(invalid),
                    needs_input=True,
                    invalid=invalid,
                )
            return _error(
                str(resolution_error),
                "scientist_required",
                project_root=project_root,
                state=state,
                status="needs_input",
                missing_kg=False,
                needs_input=True,
                scientists=scientists,
            )

        host_llm = getattr(ctx, "llm", None) if ctx is not None else None
        decoder_completion = None
        task_completion = None
        if host_llm is not None:
            loop = asyncio.get_running_loop()
            task_completion = _OmniCompletion(
                host_llm,
                loop,
                stage_label="科研任务识别",
                max_tokens=TASK_SENSOR_MAX_TOKENS,
                timeout_seconds=TASK_SENSOR_TIMEOUT_SECONDS,
            )
            decoder_completion = _OmniCompletion(
                host_llm,
                loop,
                stage_label="任务化人格生成",
                max_tokens=PERSONA_DECODER_MAX_TOKENS,
                timeout_seconds=PERSONA_DECODER_TIMEOUT_SECONDS,
            )
        if progress_callback is not None:
            value = progress_callback("SoulAgent 正在识别科研任务并生成任务化人格", 0.2)
            if hasattr(value, "__await__"):
                await value
        try:
            conversation = input_data.get("conversation") or request_text
            if not isinstance(conversation, str) or "模型" not in str(conversation) and "研究" not in str(conversation):
                conversation = "科学研究：" + str(conversation)
            result = await asyncio.to_thread(
                _core.run_pipeline,
                project_root=project_root,
                kg_root=kg_root,
                conversation=conversation,
                scientist_id=scientist_id,
                host="omniscientist",
                force=bool(input_data.get("force", False)),
                completion_fn=decoder_completion,
                task_completion_fn=task_completion,
            )
        except Exception as exc:  # noqa: BLE001 - portable Core boundary
            return _error(
                str(exc),
                "activation_failed",
                project_root=project_root,
                state=state,
                scientist_id=scientist_id,
            )

        operation_status = str(result.get("status") or "")
        if remote_install is not None:
            result["remote_install"] = remote_install
        if operation_status == "no_scientific_task":
            message = "当前请求未被识别为科学任务，未改写 role.md"
            return {
                **result,
                "outcome": {"code": "no_scientific_task"},
                "active": bool(result["loaded"]),
                "project_root": str(project_root),
                "summary": message,
                "text": message,
            }
        if operation_status in {"needs_input", "error"} or not result.get("loaded"):
            message = str(result.get("error") or "科学家人格未加载。")
            return {
                **result,
                "outcome": {"code": operation_status or "activation_failed"},
                "active": bool(result.get("loaded")),
                "project_root": str(project_root),
                "summary": message,
                "text": message,
                "recoverable": operation_status == "needs_input",
                "blocking": operation_status != "needs_input",
            }
        scientist_name = str(
            result.get("scientist_name")
            or (state or {}).get("scientist_name")
            or scientist_id
        )
        role_path = project_root / "role.md"
        persona_text = role_path.read_text(encoding="utf-8") if role_path.is_file() else ""
        unchanged = operation_status == "unchanged_task"
        message = (
            f"科学家人格 {scientist_name} 已保持装载，当前任务无需刷新。"
            if unchanged
            else f"科学家人格 {scientist_name} 已装载到 OmniScientist。"
        )
        return {
            **result,
            "status": "ok",
            "outcome": {"code": operation_status or "refreshed"},
            "active": True,
            "project_root": str(project_root),
            "role_path": str(role_path),
            "persona_text": persona_text,
            "summary": message,
            "text": message,
        }
