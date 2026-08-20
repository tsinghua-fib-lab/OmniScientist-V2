"""Loopback RPC surface for ``omni config`` — same files, same semantics."""

from __future__ import annotations

from typing import Any

from omni.config.paths import get_paths
from omni.config.settings import load_settings
from omni.config.user_edits import (
    SEMANTIC_SCHOLAR_RELOAD_NOTICE,
    apply_config_value,
    apply_embeddings_config,
    apply_home_change,
    apply_model_config,
    apply_semantic_scholar_config,
    apply_vlm_config,
    describe_config,
    get_config_value,
    is_sensitive,
    mark_model_unverified,
    test_model_connectivity,
    test_semantic_scholar_connectivity,
    test_vlm_connectivity,
    unset_config_value,
)
from omni.web.protocol import RpcError

CONFIG_METHODS = frozenset(
    {
        "config.describe",
        "config.get",
        "config.set",
        "config.unset",
        "config.applyModel",
        "config.applyVlm",
        "config.applySemanticScholar",
        "config.applyEmbeddings",
        "config.home",
        "config.test",
    }
)

_WRITE_METHODS = CONFIG_METHODS - {"config.describe", "config.get"}


def _paths():
    settings = load_settings()
    return settings.paths or get_paths(), settings


def _notice_for_key(key: str) -> str:
    if key == "research.semantic_scholar_api_key":
        return SEMANTIC_SCHOLAR_RELOAD_NOTICE
    return (
        "This omni web process will use the new setting on the next turn. "
        "A new CLI command reads the files immediately. Restart an open REPL "
        "or `omni serve` to apply it there."
    )


async def handle_config(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a ``config.*`` RPC. Callers drop the web agent cache after writes."""
    if method == "config.describe":
        return describe_config()
    if method == "config.get":
        key = str(params.get("key") or "").strip()
        if not key:
            raise RpcError("invalid_params", "config.get requires key")
        try:
            return get_config_value(load_settings(), key)
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from exc
    if method == "config.set":
        return _set(params)
    if method == "config.unset":
        return _unset(params)
    if method == "config.applyModel":
        return _apply_model(params)
    if method == "config.applyVlm":
        return _apply_vlm(params)
    if method == "config.applySemanticScholar":
        return _apply_semantic_scholar(params)
    if method == "config.applyEmbeddings":
        return _apply_embeddings(params)
    if method == "config.home":
        return _home(params)
    if method == "config.test":
        return await _test(params)
    raise RpcError("unknown_method", f"unknown method: {method}")


def writes_config(method: str) -> bool:
    return method in _WRITE_METHODS and method != "config.test"


def _set(params: dict[str, Any]) -> dict[str, Any]:
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("invalid_params", "config.set requires key")
    if "value" not in params:
        raise RpcError("invalid_params", "config.set requires value")
    paths, _settings = _paths()
    try:
        resolved, _coerced, target, display = apply_config_value(paths, key, params["value"])
    except ValueError as exc:
        raise RpcError("invalid_params", str(exc)) from exc
    if resolved.startswith("model."):
        mark_model_unverified(load_settings())
    return {
        "key": resolved,
        "display": display,
        "target": str(target),
        "secret": is_sensitive(resolved),
        "notice": _notice_for_key(resolved),
    }


def _unset(params: dict[str, Any]) -> dict[str, Any]:
    key = str(params.get("key") or "").strip()
    if not key:
        raise RpcError("invalid_params", "config.unset requires key")
    paths, _settings = _paths()
    try:
        target = unset_config_value(paths, key)
    except LookupError as exc:
        raise RpcError("not_found", str(exc)) from exc
    except ValueError as exc:
        raise RpcError("invalid_params", str(exc)) from exc
    if key.startswith("model."):
        mark_model_unverified(load_settings())
    return {
        "key": key,
        "target": str(target),
        "notice": _notice_for_key(key),
    }


def _apply_model(params: dict[str, Any]) -> dict[str, Any]:
    paths, settings = _paths()
    try:
        changed = apply_model_config(
            paths,
            provider=str(params.get("provider") or ""),
            base_url=str(params.get("base_url") or params.get("baseUrl") or ""),
            model=str(params.get("model") or ""),
            api_key=str(params.get("api_key") or params.get("apiKey") or ""),
            current_provider=settings.model.provider,
        )
    except ValueError as exc:
        raise RpcError("invalid_params", str(exc)) from exc
    return {
        "changed": changed,
        "notice": _notice_for_key("model.provider"),
    }


def _apply_vlm(params: dict[str, Any]) -> dict[str, Any]:
    paths, _settings = _paths()
    timeout = params.get("timeout_s")
    if timeout is None:
        timeout = params.get("timeout")
    timeout_s: float | None
    if timeout is None or timeout == "":
        timeout_s = None
    else:
        try:
            timeout_s = float(timeout)
        except (TypeError, ValueError) as exc:
            raise RpcError("invalid_params", "vlm timeout must be a number") from exc
    enabled = params.get("enabled")
    if enabled is not None:
        enabled = bool(enabled)
    try:
        changed = apply_vlm_config(
            paths,
            endpoint=str(params.get("endpoint") or params.get("base_url") or ""),
            model=str(params.get("model") or ""),
            api_key=str(params.get("api_key") or params.get("apiKey") or ""),
            protocol=str(params.get("protocol") or ""),
            timeout_s=timeout_s,
            enabled=enabled,
        )
    except ValueError as exc:
        raise RpcError("invalid_params", str(exc)) from exc
    return {"changed": changed, "notice": _notice_for_key("vlm.endpoint")}


def _apply_semantic_scholar(params: dict[str, Any]) -> dict[str, Any]:
    paths, _settings = _paths()
    try:
        changed = apply_semantic_scholar_config(
            paths,
            api_key=str(params.get("api_key") or params.get("apiKey") or ""),
        )
    except ValueError as exc:
        raise RpcError("invalid_params", str(exc)) from exc
    return {
        "changed": changed,
        "notice": SEMANTIC_SCHOLAR_RELOAD_NOTICE if changed else "",
    }


def _apply_embeddings(params: dict[str, Any]) -> dict[str, Any]:
    paths, settings = _paths()
    if "enabled" not in params:
        raise RpcError("invalid_params", "config.applyEmbeddings requires enabled")
    try:
        message = apply_embeddings_config(
            paths,
            settings.memory,
            enabled=bool(params.get("enabled")),
            base_url=str(params.get("base_url") or params.get("baseUrl") or ""),
            api_key=str(params.get("api_key") or params.get("apiKey") or ""),
            model=str(params.get("model") or ""),
            provider=str(params.get("provider") or ""),
            local_python=str(params.get("python") or params.get("local_python") or ""),
            local_base_model=str(params.get("base_model") or params.get("local_base_model") or ""),
            local_adapter=str(params.get("adapter") or params.get("local_adapter") or ""),
            device=str(params.get("device") or ""),
        )
    except ValueError as exc:
        raise RpcError("invalid_params", str(exc)) from exc
    return {"message": message, "notice": _notice_for_key("memory.embeddings_enabled")}


def _home(params: dict[str, Any]) -> dict[str, Any]:
    try:
        result = apply_home_change(
            path=str(params.get("path") or ""),
            reset=bool(params.get("reset")),
        )
    except ValueError as exc:
        raise RpcError("invalid_params", str(exc)) from exc
    if result.get("restart_required"):
        result["notice"] = (
            "Changing the data directory does not move existing data. "
            "Restart this omni web process, any open REPL, and `omni serve` "
            "so every component uses the same directory."
        )
    return result


async def _test(params: dict[str, Any]) -> dict[str, Any]:
    target = str(params.get("target") or "model").strip().lower().replace("-", "_")
    settings = load_settings()
    if target in {"model", "main"}:
        ok, detail = await test_model_connectivity(settings)
    elif target in {"vlm", "vision"}:
        ok, detail = await test_vlm_connectivity(settings)
    elif target in {"semantic_scholar", "s2"}:
        ok, detail = await test_semantic_scholar_connectivity(settings)
    else:
        raise RpcError("invalid_params", "config.test target must be model, vlm, or semantic_scholar")
    return {"passed": ok, "target": target, "detail": detail}
