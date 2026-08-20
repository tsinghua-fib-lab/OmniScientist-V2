"""Programmatic user-layer config edits shared by ``omni config`` and ``omni web``.

Writes go to the owner home only (``config.toml`` / ``secrets.toml``). This is
the same path the CLI uses — the web surface must not invent a second store.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from omni.config.model_stack import MODEL_PROVIDER_CATALOG, ModelRole, safe_endpoint_display
from omni.config.paths import (
    OmniPaths,
    configure_user_home,
    default_user_home,
    get_paths,
    home_selection_file,
    reset_user_home,
    user_home_resolution,
)
from omni.config.secure_files import write_private_toml
from omni.config.settings import OmniSettings, load_settings, read_toml_file
from omni.core.vlm import validate_vlm_endpoint, validate_vlm_protocol

SENSITIVE_TOKENS = ("api_key", "secret", "token", "password")

REMOVED_KEY_SHORTCUTS = {
    "provider": "model.provider",
    "api_key": "model.api_key",
    "apikey": "model.api_key",
    "key": "model.api_key",
    "model": "model.model",
    "model_name": "model.model",
    "base_url": "model.base_url",
    "baseurl": "model.base_url",
    "url": "model.base_url",
}

SEMANTIC_SCHOLAR_RELOAD_NOTICE = (
    "New CLI/REPL tasks use this setting immediately. If the home service is "
    "running, apply it there with `omni serve restart`."
)


def resolve_key(key: str) -> str:
    """Validate and return a canonical config key."""
    k = key.strip()
    canonical = REMOVED_KEY_SHORTCUTS.get(k.lower())
    if canonical:
        raise ValueError(f"Configuration alias `{k}` was removed; use `{canonical}`.")
    return k


def coerce_value(value: str) -> Any:
    """Coerce a CLI/RPC string the same way ``omni config set`` does."""
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if value.startswith(("[", "{")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


def set_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur: dict[str, Any] = data
    for part in parts[:-1]:
        nxt = cur.setdefault(part, {})
        if not isinstance(nxt, dict):
            raise ValueError(f"'{part}' is not a configuration table")
        cur = nxt
    cur[parts[-1]] = value


def get_dotted(data: dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def is_sensitive(key: str) -> bool:
    return any(tok in key for tok in SENSITIVE_TOKENS)


def mask_secret(value: Any) -> str:
    text = str(value)
    if len(text) <= 4:
        return "****"
    return f"{text[:2]}…{text[-2:]} (redacted)"


def redact_sensitive_values(value: Any) -> Any:
    """Recursively mask secrets when a parent config object is requested."""

    def _sensitive_field(name: object) -> bool:
        normalized = str(name).lower()
        return (
            "api_key" in normalized
            or "secret" in normalized
            or "password" in normalized
            or normalized == "token"
            or normalized.endswith("_token")
        )

    if isinstance(value, dict):
        return {
            key: (
                mask_secret(item)
                if _sensitive_field(key) and item not in (None, "")
                else redact_sensitive_values(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in value]
    return value


def read_editable_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return read_toml_file(path)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is invalid TOML and could not be repaired: {exc}") from exc


def write_config_value(paths: OmniPaths, key: str, value: Any) -> Path:
    """Persist ``key`` to user config (or secrets for sensitive keys)."""
    paths.home.mkdir(parents=True, exist_ok=True)
    target = paths.secrets_file if is_sensitive(key) else paths.config_file
    data = read_editable_toml(target)
    set_dotted(data, key, value)
    if target == paths.secrets_file:
        write_private_toml(target, data)
    else:
        with target.open("wb") as fh:
            tomli_w.dump(data, fh)
    return target


def apply_config_value(
    paths: OmniPaths, key: str, value: str | int | float | bool | list[Any] | dict[str, Any]
) -> tuple[str, Any, Path, str]:
    """Resolve a key, coerce string values, persist.

    Shared by ``omni config set`` and the web RPC. Returns
    ``(resolved_key, coerced_value, target_path, display)``.
    """
    resolved = resolve_key(key)
    if resolved == "data_dir":
        raise ValueError("data_dir is read-only; use `omni config home [PATH]`")
    if isinstance(value, str):
        coerced = coerce_value(value)
        raw_display = value
    else:
        coerced = value
        raw_display = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
    target = write_config_value(paths, resolved, coerced)
    display = mask_secret(coerced) if is_sensitive(resolved) else raw_display
    return resolved, coerced, target, display


def unset_config_value(paths: OmniPaths, key: str) -> Path:
    """Remove a user or secret setting. Raises ``LookupError`` if missing."""
    resolved = resolve_key(key)
    for target in (paths.config_file, paths.secrets_file):
        if not target.is_file():
            continue
        data = read_editable_toml(target)
        parts = resolved.split(".")
        cur: Any = data
        found = True
        for part in parts[:-1]:
            if not isinstance(cur, dict) or part not in cur:
                found = False
                break
            cur = cur[part]
        if found and isinstance(cur, dict) and parts[-1] in cur:
            del cur[parts[-1]]
            if target == paths.secrets_file:
                write_private_toml(target, data)
            else:
                with target.open("wb") as fh:
                    tomli_w.dump(data, fh)
            return target
    raise LookupError(f"Setting {resolved} was not found.")


def is_mock_provider(provider: str) -> bool:
    return (provider or "").strip().lower() in ("", "mock", "offline")


def normalize_provider(provider: str) -> str:
    value = (provider or "").strip().lower()
    return "mock" if value in ("", "mock", "offline") else value


def setup_required(settings: OmniSettings) -> bool:
    """Same gate as ``first_run_setup_required``: no user file and no complete model."""
    paths = settings.paths
    if paths is not None and paths.config_file.is_file():
        return False
    model = settings.model
    return not (
        normalize_provider(model.provider) != "mock"
        and bool(model.base_url)
        and bool(model.model)
        and model.model != "omni-mock"
    )


def mark_model_unverified(settings: OmniSettings) -> None:
    from omni.core.llm.health import record_model_health

    if settings.paths is None:
        return
    record_model_health(
        settings.paths,
        settings.model,
        status="unverified",
        message="Model configuration changed and has not been tested.",
    )


def apply_model_config(
    paths: OmniPaths,
    *,
    provider: str = "",
    base_url: str = "",
    model: str = "",
    api_key: str = "",
    current_provider: str = "",
) -> list[str]:
    """Apply ``omni config model`` field writes. Empty secret keeps the existing key."""
    changed: list[str] = []
    if provider:
        write_config_value(paths, "model.provider", provider)
        changed.append(f"provider={provider}")
    if base_url:
        write_config_value(paths, "model.base_url", base_url)
        changed.append(f"base_url={safe_endpoint_display(base_url)}")
    if model:
        write_config_value(paths, "model.model", model)
        changed.append(f"model={model}")
    if api_key:
        write_config_value(paths, "model.api_key", api_key)
        changed.append(f"api_key={mask_secret(api_key)}")
    if not changed:
        raise ValueError("No fields were provided. Use -p, -u, -m, or -k.")
    if not provider and (base_url or api_key) and is_mock_provider(current_provider):
        write_config_value(paths, "model.provider", "openai_compatible")
        changed.insert(0, "provider=openai_compatible (automatic)")
    mark_model_unverified(load_settings())
    return changed


def apply_vlm_config(
    paths: OmniPaths,
    *,
    endpoint: str = "",
    model: str = "",
    api_key: str = "",
    protocol: str = "",
    timeout_s: float | None = None,
    enabled: bool | None = None,
) -> list[str]:
    """Apply ``omni config vlm`` writes. All-empty is a no-op."""
    supplied = bool(endpoint or model or api_key or protocol or timeout_s is not None)
    if enabled is None and not supplied:
        return []
    if timeout_s is not None and timeout_s <= 0:
        raise ValueError("VLM timeout must be greater than zero seconds.")
    if endpoint:
        validate_vlm_endpoint(endpoint.strip())
    if protocol:
        validate_vlm_protocol(protocol.strip())

    changed: list[str] = []
    if endpoint:
        value = endpoint.strip()
        write_config_value(paths, "vlm.endpoint", value)
        changed.append(f"endpoint={safe_endpoint_display(value)}")
    if model:
        value = model.strip()
        write_config_value(paths, "vlm.model", value)
        changed.append(f"model={value}")
    if api_key:
        write_config_value(paths, "vlm.api_key", api_key)
        changed.append(f"api_key={mask_secret(api_key)}")
    if protocol:
        value = protocol.strip()
        write_config_value(paths, "vlm.protocol", value)
        changed.append(f"protocol={value}")
    if timeout_s is not None:
        write_config_value(paths, "vlm.timeout_s", timeout_s)
        changed.append(f"timeout_s={timeout_s:g}")
    if enabled is not None or supplied:
        resolved_enabled = enabled if enabled is not None else True
        write_config_value(paths, "vlm.enabled", resolved_enabled)
        changed.append(f"enabled={str(resolved_enabled).lower()}")
    return changed


def apply_semantic_scholar_config(paths: OmniPaths, *, api_key: str = "") -> list[str]:
    """Apply ``omni config semantic-scholar``. Empty key is a no-op."""
    token = api_key.strip()
    if not token:
        return []
    write_config_value(paths, "research.semantic_scholar_api_key", token)
    return [f"api_key={mask_secret(token)}"]


def apply_embeddings_config(
    paths: OmniPaths,
    memory: Any,
    *,
    enabled: bool | None = None,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    provider: str = "",
    local_python: str = "",
    local_base_model: str = "",
    local_adapter: str = "",
    device: str = "",
) -> str:
    """Apply ``omni config embeddings``. Returns the CLI success sentence."""
    local_values_supplied = bool(local_python or local_base_model or local_adapter or device)
    any_values_supplied = bool(base_url or api_key or model or provider or local_values_supplied)

    if enabled is None:
        raise ValueError("Choose either --enable or --disable.")

    if not enabled:
        if any_values_supplied:
            raise ValueError("Do not combine --disable with provider configuration options.")
        write_config_value(paths, "memory.embeddings_enabled", False)
        return "Embeddings disabled. Keyword recall will be used; endpoint settings are retained."

    requested_provider = provider.strip().casefold()
    if not requested_provider:
        if base_url or api_key:
            requested_provider = "openai_compatible"
        else:
            requested_provider = str(memory.embedding_provider or "openai_compatible").strip().casefold()
    if requested_provider == "openai":
        requested_provider = "openai_compatible"
    if requested_provider not in {"openai_compatible", "specter2"}:
        raise ValueError("Embedding provider must be openai_compatible or specter2.")

    if requested_provider == "specter2":
        if base_url or api_key:
            raise ValueError("Local SPECTER2 does not use --base-url or --api-key.")
        resolved_python = local_python or memory.embedding_specter2_python
        resolved_base = local_base_model or memory.embedding_specter2_base_model
        resolved_adapter = local_adapter or memory.embedding_specter2_adapter
        resolved_device = device or memory.embedding_specter2_device or "cpu"
        local_paths = (
            (resolved_python, "file"),
            (resolved_base, "directory"),
            (resolved_adapter, "directory"),
        )
        if not all(value for value, _kind in local_paths):
            raise ValueError(
                "SPECTER2 requires --python, --base-model, and --adapter "
                "the first time it is configured."
            )
        try:
            python_path = Path(os.path.abspath(os.path.expanduser(resolved_python)))
            base_path = Path(resolved_base).expanduser().resolve(strict=True)
            adapter_path = Path(resolved_adapter).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("A configured SPECTER2 local path does not exist.") from exc
        if not python_path.is_file() or not os.access(python_path, os.X_OK):
            raise ValueError("The SPECTER2 Python executable is not an executable file.")
        if not base_path.is_dir() or not adapter_path.is_dir():
            raise ValueError("The SPECTER2 base model and adapter must be directories.")
        if not re.fullmatch(r"(?:cpu|mps|cuda(?::\d+)?)", resolved_device):
            raise ValueError("SPECTER2 device must be cpu, mps, cuda, or cuda:N.")
        resolved_model = (
            model.strip()
            or (memory.embedding_model if memory.embedding_provider == "specter2" else "")
            or "allenai/specter2-proximity"
        )
        write_config_value(paths, "memory.embedding_provider", "specter2")
        write_config_value(paths, "memory.embedding_model", resolved_model)
        write_config_value(paths, "memory.embedding_dim", 768)
        write_config_value(paths, "memory.embedding_specter2_python", str(python_path))
        write_config_value(paths, "memory.embedding_specter2_base_model", str(base_path))
        write_config_value(paths, "memory.embedding_specter2_adapter", str(adapter_path))
        write_config_value(paths, "memory.embedding_specter2_device", resolved_device)
        write_config_value(paths, "memory.embeddings_enabled", True)
        return (
            f"Enabled local SPECTER2 embeddings: {resolved_model} "
            f"on {resolved_device} (768 dimensions)"
        )

    if local_values_supplied:
        raise ValueError("Local Python/model/adapter/device options require -p specter2.")
    resolved_base = (base_url or memory.embedding_base_url).rstrip("/")
    resolved_model = model or memory.embedding_model or "text-embedding-3-small"
    if not resolved_base:
        raise ValueError(
            "Enabling embeddings requires -u/--base-url and an endpoint that provides /embeddings."
        )
    write_config_value(paths, "memory.embedding_provider", requested_provider)
    write_config_value(paths, "memory.embedding_base_url", resolved_base)
    write_config_value(paths, "memory.embedding_model", resolved_model)
    if api_key:
        write_config_value(paths, "memory.embedding_api_key", api_key)
    write_config_value(paths, "memory.embeddings_enabled", True)
    return f"Enabled semantic recall: {resolved_model} @ {safe_endpoint_display(resolved_base)}"


def describe_home() -> dict[str, Any]:
    active, source = user_home_resolution()
    return {
        "active": str(active),
        "source": source,
        "default": str(default_user_home()),
        "selection_file": str(home_selection_file()),
        "environment_override": bool(os.environ.get("OMNI_HOME", "").strip()),
    }


def apply_home_change(*, path: str = "", reset: bool = False) -> dict[str, Any]:
    """Apply ``omni config home``. Changing home requires a process restart."""
    if path and reset:
        raise ValueError("Provide either PATH or --reset, not both.")
    if not path and not reset:
        return {**describe_home(), "changed": False, "restart_required": False}

    previous, source = user_home_resolution()
    notes: list[str] = []
    warning = ""
    if reset:
        reset_user_home()
        active, active_source = user_home_resolution()
        message = f"Restored the default Omni data directory selection: {default_user_home()}"
        if active_source == "environment (OMNI_HOME)":
            warning = (
                f"OMNI_HOME still overrides the saved selection; the active directory remains {active}."
            )
        else:
            notes.append(f"New commands will use {active}. Existing data at {previous} was not deleted.")
        return {
            **describe_home(),
            "changed": True,
            "restart_required": True,
            "previous": str(previous),
            "message": message,
            "warning": warning,
            "notes": notes,
        }

    target = Path(path).expanduser().resolve()
    if source == "environment (OMNI_HOME)" and target != previous:
        raise ValueError(
            f"OMNI_HOME currently selects {previous}. Unset OMNI_HOME before choosing {target}."
        )
    try:
        configure_user_home(target)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not configure the Omni data directory: {exc}") from exc
    active, active_source = user_home_resolution()
    message = f"Omni data directory set to {active} ({active_source})."
    if active != previous:
        notes.append(f"Existing data at {previous} was not moved or deleted.")
    if os.environ.get("OMNI_HOME", "").strip():
        notes.append("The persisted selection will also apply after OMNI_HOME is unset.")
    return {
        **describe_home(),
        "changed": True,
        "restart_required": True,
        "previous": str(previous),
        "message": message,
        "warning": warning,
        "notes": notes,
    }


def describe_paths(settings: OmniSettings) -> dict[str, str]:
    paths = settings.paths or get_paths()
    _, source = user_home_resolution()
    return {
        "home": str(paths.home),
        "home_source": source,
        "home_selection": str(home_selection_file()),
        "user_config": str(paths.config_file),
        "secrets": str(paths.secrets_file),
        "role": str(paths.role_file),
        "project_dir": str(paths.project_dir),
        "project_config": str(paths.project_config),
    }


def _secret_set(value: Any) -> bool:
    return bool(value)


def _fmt(value: Any) -> Any:
    if value is None or value == "":
        return None
    return value


def describe_effective(settings: OmniSettings) -> dict[str, Any]:
    """Structured equivalent of ``omni config list`` (secrets never in full)."""
    from omni.core.llm.health import load_model_health

    paths = settings.paths
    model_health = load_model_health(paths, settings.model) if paths is not None else None
    memory = settings.memory
    specter = memory.embedding_provider == "specter2"
    rows: list[dict[str, Any]] = [
        _row("project", settings.paths.project_name if settings.paths else ""),
        _row("data_dir", str(settings.paths.home if settings.paths else settings.data_dir)),
        _row("model.provider", settings.model.provider),
        _row("model.model", settings.model.model),
        _row("model.base_url", settings.model.base_url or None),
        _row("model.api_key", None, secret=True, present=_secret_set(settings.model.api_key)),
        _row("model.health", model_health.status if model_health else None),
        _row("model.health_detail", model_health.message if model_health else None),
        _row("vlm.enabled", settings.vlm.enabled),
        _row("vlm.model", settings.vlm.model or None),
        _row("vlm.endpoint", settings.vlm.endpoint or None),
        _row("vlm.protocol", settings.vlm.protocol),
        _row("vlm.api_key", None, secret=True, present=_secret_set(settings.vlm.api_key)),
        _row(
            "research.semantic_scholar_api_key",
            None,
            secret=True,
            present=_secret_set(settings.research.semantic_scholar_api_key),
        ),
        _row("research.semantic_scholar_enabled", "semanticscholar" in settings.research.connectors),
        _row("memory.enabled", memory.enabled),
        _row("memory.embeddings_enabled", memory.embeddings_enabled),
        _row("memory.embedding_provider", memory.embedding_provider or None),
        _row(
            "memory.embedding_base_url",
            "(not used by local SPECTER2)" if specter else (memory.embedding_base_url or None),
        ),
        _row("memory.embedding_model", memory.embedding_model or None),
        _row(
            "memory.embedding_api_key",
            None,
            secret=True,
            present=False if specter else _secret_set(memory.embedding_api_key),
        ),
        _row("memory.embedding_specter2_python", memory.embedding_specter2_python or None),
        _row("memory.embedding_specter2_base_model", memory.embedding_specter2_base_model or None),
        _row("memory.embedding_specter2_adapter", memory.embedding_specter2_adapter or None),
        _row("memory.embedding_specter2_device", memory.embedding_specter2_device),
        _row("memory.vector_backend", memory.vector_backend),
        _row("react.max_iterations", settings.react.max_iterations),
        _row("react.max_tool_calls", settings.react.max_tool_calls),
        _row("react.max_seconds", settings.react.max_seconds),
        _row("react.stall_timeout_s", settings.react.stall_timeout_s),
        _row("react.stream_max_retries", settings.react.stream_max_retries),
        _row("react.finalization_timeout_s", settings.react.finalization_timeout_s),
        _row("react.self_review", settings.react.self_review),
        _row("display.ui_mode", settings.display.ui_mode),
        _row("display.verbosity", settings.display.verbosity),
        _row("cost.enabled", settings.cost.enabled),
        _row("cost.max_total_tokens", settings.cost.max_total_tokens),
        _row("cost.max_cost_usd", settings.cost.max_cost_usd),
        _row("cost.warn_total_tokens", settings.cost.warn_total_tokens),
        _row("cost.warn_cost_usd", settings.cost.warn_cost_usd),
        _row("tasks.auto_retry", settings.tasks.auto_retry),
        _row("tasks.workflow_max_steps", settings.tasks.workflow_max_steps),
        _row("tasks.workflow_max_tool_calls", settings.tasks.workflow_max_tool_calls),
        _row("tasks.workflow_max_seconds", settings.tasks.workflow_max_seconds),
        _row("schedules.enabled", settings.schedules.enabled),
        _row("security.bash_sandbox", settings.security.bash_sandbox),
        _row("security.require_approval", settings.security.require_approval),
        _row("security.approval_policy", settings.security.approval_policy),
        _row("security.approval_allowlist", list(settings.security.approval_allowlist)),
        _row("channels.enabled", list(settings.channels.enabled)),
        _row("skills.sources", list(settings.skills.sources)),
        _row("skills.max_prompt_iterations", settings.skills.max_prompt_iterations),
        _row("skills.max_prompt_tool_calls", settings.skills.max_prompt_tool_calls),
        _row("skills.max_prompt_seconds", settings.skills.max_prompt_seconds),
        _row("skills.max_python_seconds", settings.skills.max_python_seconds),
        _row("skills.max_cli_seconds", settings.skills.max_cli_seconds),
        _row("skills.disabled", list(settings.skills.disabled)),
        _row("skills.default_for", dict(settings.skills.default_for)),
        _row("skills.export_targets", list(settings.skills.export_targets)),
    ]
    mcp = [
        {
            "name": name,
            "command": cfg.command or cfg.url,
            "url": cfg.url,
            "enabled": cfg.enabled,
            "args": list(cfg.args),
        }
        for name, cfg in settings.mcp_servers.items()
    ]
    return {
        "rows": rows,
        "mcp_servers": mcp,
        "blocks": {
            "model": {
                "provider": settings.model.provider,
                "base_url": settings.model.base_url,
                "model": settings.model.model,
                "api_key_set": _secret_set(settings.model.api_key),
                "health": model_health.status if model_health else "",
                "health_detail": model_health.message if model_health else "",
            },
            "vlm": {
                "enabled": settings.vlm.enabled,
                "model": settings.vlm.model,
                "endpoint": settings.vlm.endpoint,
                "protocol": settings.vlm.protocol,
                "timeout_s": settings.vlm.timeout_s,
                "api_key_set": _secret_set(settings.vlm.api_key),
            },
            "semantic_scholar": {
                "api_key_set": _secret_set(settings.research.semantic_scholar_api_key),
                "enabled": "semanticscholar" in settings.research.connectors,
            },
            "embeddings": {
                "enabled": memory.embeddings_enabled,
                "provider": memory.embedding_provider,
                "base_url": memory.embedding_base_url,
                "model": memory.embedding_model,
                "api_key_set": _secret_set(memory.embedding_api_key),
                "specter2_python": memory.embedding_specter2_python,
                "specter2_base_model": memory.embedding_specter2_base_model,
                "specter2_adapter": memory.embedding_specter2_adapter,
                "specter2_device": memory.embedding_specter2_device,
            },
            "memory": {"enabled": memory.enabled, "embeddings_enabled": memory.embeddings_enabled},
            "react": {
                "max_iterations": settings.react.max_iterations,
                "max_tool_calls": settings.react.max_tool_calls,
                "max_seconds": settings.react.max_seconds,
                "stall_timeout_s": settings.react.stall_timeout_s,
                "stream_max_retries": settings.react.stream_max_retries,
                "finalization_timeout_s": settings.react.finalization_timeout_s,
                "self_review": settings.react.self_review,
            },
            "display": {
                "ui_mode": settings.display.ui_mode,
                "verbosity": settings.display.verbosity,
            },
            "cost": {
                "enabled": settings.cost.enabled,
                "max_total_tokens": settings.cost.max_total_tokens,
                "max_cost_usd": settings.cost.max_cost_usd,
                "warn_total_tokens": settings.cost.warn_total_tokens,
                "warn_cost_usd": settings.cost.warn_cost_usd,
            },
            "tasks": {
                "auto_retry": settings.tasks.auto_retry,
                "workflow_max_steps": settings.tasks.workflow_max_steps,
                "workflow_max_tool_calls": settings.tasks.workflow_max_tool_calls,
                "workflow_max_seconds": settings.tasks.workflow_max_seconds,
            },
            "schedules": {"enabled": settings.schedules.enabled},
            "security": {
                "bash_sandbox": settings.security.bash_sandbox,
                "require_approval": settings.security.require_approval,
                "approval_policy": settings.security.approval_policy,
                "approval_allowlist": list(settings.security.approval_allowlist),
            },
            "channels": {"enabled": list(settings.channels.enabled)},
            "skills": {
                "sources": list(settings.skills.sources),
                "disabled": list(settings.skills.disabled),
                "default_for": dict(settings.skills.default_for),
                "export_targets": list(settings.skills.export_targets),
                "max_prompt_iterations": settings.skills.max_prompt_iterations,
                "max_prompt_tool_calls": settings.skills.max_prompt_tool_calls,
                "max_prompt_seconds": settings.skills.max_prompt_seconds,
                "max_python_seconds": settings.skills.max_python_seconds,
                "max_cli_seconds": settings.skills.max_cli_seconds,
            },
        },
    }


def _row(key: str, value: Any, *, secret: bool = False, present: bool | None = None) -> dict[str, Any]:
    if secret:
        set_flag = bool(present)
        return {
            "key": key,
            "value": "***set***" if set_flag else None,
            "secret": True,
            "set": set_flag,
        }
    return {"key": key, "value": _fmt(value), "secret": False, "set": value not in (None, "")}


def catalog_presets() -> list[dict[str, Any]]:
    return [
        {
            "key": item.key,
            "label": item.label,
            "roles": [role.value for role in item.roles],
            "default_endpoint": item.default_endpoint,
            "default_model": item.default_model,
        }
        for item in MODEL_PROVIDER_CATALOG
        if ModelRole.MAIN in item.roles or ModelRole.EMBEDDING in item.roles
    ]


def describe_config() -> dict[str, Any]:
    settings = load_settings()
    effective = describe_effective(settings)
    return {
        "setup_required": setup_required(settings),
        "paths": describe_paths(settings),
        "home": describe_home(),
        "catalog": catalog_presets(),
        **effective,
    }


def get_config_value(settings: OmniSettings, key: str) -> dict[str, Any]:
    resolved = resolve_key(key)
    paths = settings.paths or get_paths()
    if resolved == "data_dir":
        return {"key": resolved, "value": str(paths.home), "secret": False, "set": True}
    raw = read_editable_toml(paths.config_file)
    value = get_dotted(raw, resolved)
    if value is None:
        dumped = settings.model_dump(exclude={"paths"})
        value = get_dotted(dumped, resolved)
    secret = is_sensitive(resolved)
    present = value not in (None, "")
    if secret and present:
        return {"key": resolved, "value": mask_secret(value), "secret": True, "set": True}
    return {
        "key": resolved,
        "value": redact_sensitive_values(value),
        "secret": secret,
        "set": present,
    }


async def test_model_connectivity(settings: OmniSettings) -> tuple[bool, str]:
    from omni.core.llm.client import check_connectivity
    from omni.core.llm.health import record_model_health

    ok, detail = await check_connectivity(settings)
    if settings.paths is not None:
        record_model_health(
            settings.paths,
            settings.model,
            status="verified" if ok else "failed",
            message=detail,
        )
    return ok, detail


async def test_vlm_connectivity(settings: OmniSettings) -> tuple[bool, str]:
    from omni.core.vlm import check_vlm_connectivity

    return await check_vlm_connectivity(settings.vlm)


async def test_semantic_scholar_connectivity(settings: OmniSettings) -> tuple[bool, str]:
    from omni.research import connectors

    api_key = settings.research.semantic_scholar_api_key
    if not api_key:
        return False, (
            "Semantic Scholar API key is not configured. "
            "Run `config semantic-scholar -k <API_KEY>` first."
        )
    try:
        results = await connectors.semanticscholar_search(
            "automated peer review large language model",
            rows=1,
            api_key=api_key,
        )
    except Exception as exc:  # noqa: BLE001 — surface the connector's safe text
        return False, f"Semantic Scholar test failed: {exc}"
    if not results:
        return False, "Semantic Scholar responded but returned no result for the test query."
    return True, "Semantic Scholar credentials are working."
