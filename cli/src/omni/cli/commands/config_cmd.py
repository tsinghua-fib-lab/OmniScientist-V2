"""`omni config` — inspect & edit layered configuration."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
import typer

from omni.cli.command_surface import spell_commands
from omni.cli.render import data_table, error, info, kv_table, success, warn
from omni.cli.state import AppState
from omni.config.model_stack import safe_endpoint_display
from omni.config.paths import (
    configure_user_home,
    default_user_home,
    get_paths,
    home_selection_file,
    reset_user_home,
    user_home_resolution,
)
from omni.config.secure_files import write_private_toml
from omni.config.settings import read_toml_file
from omni.core.vlm import (
    check_vlm_connectivity,
    validate_vlm_endpoint,
    validate_vlm_protocol,
)

app = typer.Typer(help="Inspect and modify layered TOML configuration.", no_args_is_help=True)
_CONFIG_SUBCOMMANDS = (
    "list", "get", "set", "model", "vlm", "semantic-scholar", "embeddings", "home", "test", "path", "unset", "help",
)

_SENSITIVE = ("api_key", "secret", "token", "password")

_REMOVED_KEY_SHORTCUTS = {
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


def render_config_usage_help() -> None:
    """Render detailed config help for shell and REPL users."""
    info("Use `config ...` or `/config ...` in the REPL and `omni config ...` in the shell.")
    info(
        f"For model setup, prefer `{spell_commands('/model')}`: it unifies main, vision, and embedding "
        "roles and explains the effective source. Advanced dotted config remains available here."
    )
    info(f"Available subcommands: {', '.join(_CONFIG_SUBCOMMANDS)}.")
    data_table(
        "config subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "Show effective model, memory, channel, and skill settings", spell_commands("/config list")],
            ["get <key>", "Read a setting; sensitive values are redacted", spell_commands("/config get model.provider")],
            ["set <key> <value>", "Write a setting; secrets go to secrets.toml", spell_commands("/config set model.base_url https://api.deepseek.com/v1")],
            [
                "set research.semantic_scholar_api_key <KEY>",
                "Set the optional Semantic Scholar key in secrets.toml",
                spell_commands("/config set research.semantic_scholar_api_key <API_KEY>"),
            ],
            ["model", "Set model endpoint, key, and name", spell_commands("/config model -p openai -u <BASE_URL> -m <MODEL> -k <API_KEY>")],
            ["vlm", "Configure the optional vision model used by visual skills", spell_commands("/config vlm -u <ENDPOINT> -m <MODEL> -k <API_KEY>")],
            ["semantic-scholar", "Configure literature-search credentials", spell_commands("/config semantic-scholar -k <API_KEY> --test")],
            ["embeddings", "Configure remote or local semantic embeddings", spell_commands("/config embeddings --enable -p specter2 --python <PYTHON> --base-model <DIR> --adapter <DIR>")],
            ["home [PATH]", "Show or change the Omni data directory; --reset restores ~/.omni", spell_commands("/config home /data/omni")],
            ["test", "Test the active model configuration", spell_commands("/config test")],
            ["path", "Show user, secret, and project config paths", spell_commands("/config path")],
            ["unset <key>", "Remove a user or secret setting", spell_commands("/config unset model.api_key")],
        ],
    )
    info("config model options: -p provider · -u base_url · -m model · -k api_key.")
    _render_model_config_guide()
    _render_vlm_config_guide()
    _render_semantic_scholar_config_guide()
    _render_embeddings_config_guide()
    info(
        f"Semantic Scholar: `{spell_commands('/config set research.semantic_scholar_api_key <API_KEY>')}`. "
        "Register at https://www.semanticscholar.org/product/api"
    )


def _render_model_config_guide() -> None:
    """Explain the model config: the one-shot command, provider, and config files."""
    from rich.text import Text

    from omni.cli.render import console

    text = Text()
    text.append("Configure a model; changes apply to the next command:\n", "bold")
    text.append("  1. Guided three-role setup: ", "dim")
    text.append(f"{spell_commands('/model')}\n", "cyan")
    text.append("  2. One advanced command: ", "dim")
    text.append(f"{spell_commands('/config model -p openai -u https://api.deepseek.com/v1 -m deepseek-chat -k sk-xxx')}\n", "cyan")
    text.append(f"  3. Or run `{spell_commands('/config path')}` and edit the reported files:\n", "dim")
    text.append("       config.toml   → [model] provider / base_url / model\n", "cyan")
    text.append("       secrets.toml  -> [model] api_key (stored separately from projects)\n", "cyan")
    text.append("Provider selection: ", "bold")
    text.append(
        "openai, openai_compatible, deepseek, and ollama use the OpenAI-compatible protocol.\n"
        "  base_url selects the actual service. For DeepSeek, provider=openai and\n"
        "  base_url=https://api.deepseek.com/v1 are sufficient. mock is offline.\n",
        "dim",
    )
    text.append(f"  If only -u is supplied, mock changes to openai_compatible. Verify with {spell_commands('/config test')}.", "dim")
    console.print(text)


def _render_vlm_config_guide() -> None:
    """Explain the owner-controlled, reusable VLM configuration."""
    from rich.text import Text

    from omni.cli.render import console

    text = Text()
    text.append("Configure an optional vision model for visual skills:\n", "bold")
    text.append("  ", "dim")
    text.append(
        f"{spell_commands('/config vlm -u https://vision.example/v1/chat/completions -m <VISION_MODEL> -k <API_KEY>')}\n",
        "cyan",
    )
    text.append(
        "  Use HTTPS for remote services (HTTP is allowed only on loopback). "
        "The API key is kept in secrets.toml. Add --test to verify multimodal support.",
        "dim",
    )
    console.print(text)


def _render_semantic_scholar_config_guide() -> None:
    """Explain the owner-scoped Semantic Scholar credential."""
    from rich.text import Text

    from omni.cli.render import console

    text = Text()
    text.append("Configure Semantic Scholar for literature evidence:\n", "bold")
    text.append("  ", "dim")
    text.append(f"{spell_commands('/config semantic-scholar -k <API_KEY> --test')}\n", "cyan")
    text.append(
        "  The token is stored in secrets.toml and cannot be overridden by a "
        "project config. Paper review requires it for its complete literature-check stage.",
        "dim",
    )
    console.print(text)


def _render_embeddings_config_guide() -> None:
    """Explain remote and owner-scoped local embedding configuration."""
    from rich.text import Text

    from omni.cli.render import console

    text = Text()
    text.append("Configure embedding recall:\n", "bold")
    text.append("  Enable: ", "dim")
    text.append(f"{spell_commands('/config embeddings --enable -u https://api.openai.com/v1 -m text-embedding-3-small -k sk-xxx')}\n", "cyan")
    text.append("       Semantic recall matches paraphrases; the endpoint must provide /embeddings and may incur cost.\n", "dim")
    text.append("  Local SPECTER2: ", "dim")
    text.append(
        f"{spell_commands('/config embeddings --enable -p specter2 --python <PYTHON> --base-model <BASE_DIR> --adapter <ADAPTER_DIR> --device cuda:0')}\n",
        "cyan",
    )
    text.append(
        "       Runs the local model offline in its dedicated Python environment; "
        "no API endpoint or token is used.\n",
        "dim",
    )
    text.append("  Disable: ", "dim")
    text.append(spell_commands("/config embeddings --disable"), "cyan")
    text.append(" -> keyword recall without an embedding request.\n", "dim")
    text.append(f"  Settings can also be changed individually with {spell_commands('/config set')}.\n", "dim")
    text.append(f"  Or run `{spell_commands('/config path')}` and edit [memory] embedding_* in the reported files.", "dim")
    console.print(text)


@app.command("help")
def help_cmd() -> None:
    """Show config commands and examples."""
    render_config_usage_help()


def _resolve_key(key: str) -> str:
    """Validate and return a canonical config key."""
    k = key.strip()
    canonical = _REMOVED_KEY_SHORTCUTS.get(k.lower())
    if canonical:
        raise ValueError(f"Configuration alias `{k}` was removed; use `{canonical}`.")
    return k


def _resolve_key_or_exit(key: str) -> str:
    try:
        return _resolve_key(key)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(2) from exc


def _coerce(value: str) -> Any:
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


def _set_dotted(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = data
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
        if not isinstance(cur, dict):
            raise typer.BadParameter(f"'{p}' is not a configuration table")
    cur[parts[-1]] = value


def _get_dotted(data: dict, dotted: str) -> Any:
    cur: Any = data
    for p in dotted.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


@app.command("list")
def list_cmd(ctx: typer.Context) -> None:
    """Show effective configuration."""
    from omni.core.llm.health import load_model_health

    state: AppState = ctx.obj
    s = state.settings()
    model_health = load_model_health(s.paths, s.model)
    kv_table("Effective configuration", [
        ("project", s.paths.project_name),
        ("data_dir", s.data_dir),
        ("model.provider", s.model.provider),
        ("model.model", s.model.model),
        ("model.base_url", s.model.base_url or "(unset)"),
        ("model.api_key", "***set***" if s.model.api_key else "(unset)"),
        ("model.health", model_health.status),
        ("model.health_detail", model_health.message),
        ("vlm.enabled", s.vlm.enabled),
        ("vlm.model", s.vlm.model or "(unset)"),
        ("vlm.endpoint", s.vlm.endpoint or "(unset)"),
        ("vlm.protocol", s.vlm.protocol),
        ("vlm.api_key", "***set***" if s.vlm.api_key else "(unset)"),
        ("research.semantic_scholar_api_key", "***set***" if s.research.semantic_scholar_api_key else "(unset)"),
        ("research.semantic_scholar_enabled", "semanticscholar" in s.research.connectors),
        ("memory.enabled", s.memory.enabled),
        ("memory.embeddings_enabled", s.memory.embeddings_enabled),
        ("memory.embedding_provider", s.memory.embedding_provider or "(unset)"),
        ("memory.embedding_base_url", s.memory.embedding_base_url or "(unset)"),
        ("memory.embedding_model", s.memory.embedding_model),
        ("memory.embedding_api_key", "***set***" if s.memory.embedding_api_key else "(unset)"),
        ("memory.embedding_specter2_python", s.memory.embedding_specter2_python or "(unset)"),
        ("memory.embedding_specter2_base_model", s.memory.embedding_specter2_base_model or "(unset)"),
        ("memory.embedding_specter2_adapter", s.memory.embedding_specter2_adapter or "(unset)"),
        ("memory.embedding_specter2_device", s.memory.embedding_specter2_device),
        ("memory.vector_backend", s.memory.vector_backend),
        (
            "research.semantic_scholar_api_key",
            "***set***" if s.research.semantic_scholar_api_key else "(unset)",
        ),
        ("react.max_iterations", s.react.max_iterations),
        ("react.max_tool_calls", s.react.max_tool_calls),
        ("react.max_seconds", s.react.max_seconds),
        ("react.stall_timeout_s", s.react.stall_timeout_s),
        ("react.stream_max_retries", s.react.stream_max_retries),
        ("react.finalization_timeout_s", s.react.finalization_timeout_s),
        ("react.self_review", s.react.self_review),
        ("display.ui_mode", s.display.ui_mode),
        ("display.verbosity", s.display.verbosity),
        ("cost.enabled", s.cost.enabled),
        ("cost.max_total_tokens", s.cost.max_total_tokens),
        ("cost.max_cost_usd", s.cost.max_cost_usd),
        ("cost.warn_total_tokens", s.cost.warn_total_tokens),
        ("cost.warn_cost_usd", s.cost.warn_cost_usd),
        ("tasks.auto_retry", s.tasks.auto_retry),
        ("tasks.workflow_max_steps", s.tasks.workflow_max_steps),
        ("tasks.workflow_max_tool_calls", s.tasks.workflow_max_tool_calls),
        ("tasks.workflow_max_seconds", s.tasks.workflow_max_seconds),
        ("schedules.enabled", s.schedules.enabled),
        ("security.bash_sandbox", s.security.bash_sandbox),
        ("security.require_approval", s.security.require_approval),
        ("security.approval_policy", s.security.approval_policy),
        ("security.approval_allowlist", ", ".join(s.security.approval_allowlist) or "(none)"),
        ("channels.enabled", ", ".join(s.channels.enabled)),
        ("skills.sources", ", ".join(s.skills.sources)),
        ("skills.max_prompt_iterations", s.skills.max_prompt_iterations),
        ("skills.max_prompt_tool_calls", s.skills.max_prompt_tool_calls),
        ("skills.max_prompt_seconds", s.skills.max_prompt_seconds),
        ("skills.max_python_seconds", s.skills.max_python_seconds),
        ("skills.max_cli_seconds", s.skills.max_cli_seconds),
        ("skills.disabled", ", ".join(s.skills.disabled)),
        ("skills.default_for", json.dumps(s.skills.default_for, ensure_ascii=False)),
        ("skills.export_targets", ", ".join(s.skills.export_targets)),
    ])
    if s.mcp_servers:
        data_table("MCP servers", ["name", "command/url", "enabled"],
                   [[n, c.command or c.url, c.enabled] for n, c in s.mcp_servers.items()])


@app.command("get")
def get_cmd(ctx: typer.Context, key: str) -> None:
    """Read a setting using its full dotted path."""
    state: AppState = ctx.obj
    key = _resolve_key_or_exit(key)
    if key == "data_dir":
        info(f"{key} = {json.dumps(str(state.settings().paths.home))}")
        return
    paths = get_paths(project=state.project)
    raw = _read_editable_toml(paths.config_file)
    val = _get_dotted(raw, key)
    if val is None:
        # fall back to effective settings dump (also surfaces secrets-derived values)
        eff = state.settings().model_dump(exclude={"paths"})
        val = _get_dotted(eff, key)
    # Never echo a secret in full, even on an explicit get.
    if _is_sensitive(key) and val not in (None, ""):
        info(f"{key} = {_mask(val)}")
        return
    info(
        f"{key} = "
        f"{json.dumps(_redact_sensitive_values(val), ensure_ascii=False)}"
    )


def _is_sensitive(key: str) -> bool:
    return any(tok in key for tok in _SENSITIVE)


def _mask(value: Any) -> str:
    s = str(value)
    if len(s) <= 4:
        return "****"
    return f"{s[:2]}…{s[-2:]} (redacted)"


def _redact_sensitive_values(value: Any) -> Any:
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
                _mask(item)
                if _sensitive_field(key) and item not in (None, "")
                else _redact_sensitive_values(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_values(item) for item in value]
    return value


def _write_config_value(paths, key: str, value: Any) -> Any:
    """Persist ``key`` to user config (or secrets for sensitive keys)."""
    paths.home.mkdir(parents=True, exist_ok=True)
    target = paths.secrets_file if _is_sensitive(key) else paths.config_file
    data = _read_editable_toml(target)
    _set_dotted(data, key, value)
    if target == paths.secrets_file:
        write_private_toml(target, data)
    else:
        with target.open("wb") as fh:
            tomli_w.dump(data, fh)
    return target


def _read_editable_toml(path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return read_toml_file(path)
    except tomllib.TOMLDecodeError as exc:
        error(f"{path} is invalid TOML and could not be repaired: {exc}")
        raise typer.Exit(2) from exc


def apply_config_value(paths, key: str, value: str) -> tuple[str, Any, Any, str]:
    """Resolve a key shortcut, coerce the value, persist it.

    Shared by ``omni config set`` and the in-REPL ``config set`` so both behave
    identically. Returns ``(resolved_key, coerced_value, target_path, display)``
    where ``display`` is already masked for sensitive keys.
    """
    resolved = _resolve_key(key)
    if resolved == "data_dir":
        raise ValueError("data_dir is read-only; use `omni config home [PATH]`")
    coerced = _coerce(value)
    target = _write_config_value(paths, resolved, coerced)
    display = _mask(coerced) if _is_sensitive(resolved) else str(value)
    return resolved, coerced, target, display


def _semantic_scholar_reload_notice() -> None:
    info(
        "New CLI/REPL tasks use this setting immediately. If the home service is "
        "running, apply it there with `omni serve restart`."
    )


@app.command("set")
def set_cmd(ctx: typer.Context, key: str, value: str) -> None:
    """Write a setting; changes apply to the next command.

    Sensitive fields are stored in secrets.toml. Use full model keys such as:

      omni config set model.provider openai
      omni config set model.base_url https://api.deepseek.com
      omni config set model.model deepseek-v4-pro
      omni config set model.api_key sk-xxx
    """
    state: AppState = ctx.obj
    paths = get_paths(project=state.project)
    resolved = _resolve_key_or_exit(key)
    if _is_sensitive(resolved):
        info("Sensitive fields are written to secrets.toml and are not shared with projects.")
    try:
        resolved, _, target, display = apply_config_value(paths, key, value)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(2) from exc
    if resolved.startswith("model."):
        _mark_model_unverified(state.settings())
    success(f"Set {resolved} -> {display} ({target})")
    if resolved == "research.semantic_scholar_api_key":
        _semantic_scholar_reload_notice()


def _render_home_configuration() -> None:
    active, source = user_home_resolution()
    kv_table("Omni data directory", [
        ("active", active),
        ("source", source),
        ("default", default_user_home()),
        ("saved selection", home_selection_file()),
    ])
    info("Change with `config home <PATH>`; restore the default with `config home --reset`.")


@app.command("home")
def home_cmd(
    path: str = typer.Argument("", help="New data directory. Existing data is not moved."),
    reset: bool = typer.Option(False, "--reset", help="Restore the default ~/.omni directory."),
) -> None:
    """Show, change, or reset the persistent Omni data directory."""
    if path and reset:
        error("Provide either PATH or --reset, not both.")
        raise typer.Exit(2)
    if not path and not reset:
        _render_home_configuration()
        return

    previous, source = user_home_resolution()
    if reset:
        reset_user_home()
        active, active_source = user_home_resolution()
        success(f"Restored the default Omni data directory selection: {default_user_home()}")
        if active_source == "environment (OMNI_HOME)":
            warn(f"OMNI_HOME still overrides the saved selection; the active directory remains {active}.")
        else:
            info(f"New commands will use {active}. Existing data at {previous} was not deleted.")
        info("Restart an active Omni REPL or daemon so every component uses the same directory.")
        return

    target = Path(path).expanduser().resolve()
    if source == "environment (OMNI_HOME)" and target != previous:
        error(f"OMNI_HOME currently selects {previous}. Unset OMNI_HOME before choosing {target}.")
        raise typer.Exit(2)
    try:
        configure_user_home(target)
    except (OSError, ValueError) as exc:
        error(f"Could not configure the Omni data directory: {exc}")
        raise typer.Exit(2) from exc
    active, active_source = user_home_resolution()
    success(f"Omni data directory set to {active} ({active_source}).")
    if active != previous:
        info(f"Existing data at {previous} was not moved or deleted.")
    if os.environ.get("OMNI_HOME", "").strip():
        info("The persisted selection will also apply after OMNI_HOME is unset.")
    info("Restart an active Omni REPL or daemon so every component uses the same directory.")


@app.command("model")
def model_cmd(
    ctx: typer.Context,
    provider: str = typer.Option(
        "", "--provider", "-p",
        help="Provider: openai, openai_compatible, deepseek, ollama, or mock.",
    ),
    base_url: str = typer.Option("", "--base-url", "-u", help="Endpoint, for example https://api.deepseek.com/v1."),
    api_key: str = typer.Option("", "--api-key", "-k", help="Token stored in secrets.toml."),
    model: str = typer.Option("", "--model", "-m", help="Model name, for example deepseek-chat."),
    test: bool = typer.Option(False, "--test", help="Test the configuration after saving."),
) -> None:
    """Set endpoint, token, and model in one command.

    Options: -p provider, -u base_url, -m model, and -k api_key.

    Example: omni config model -p openai -u https://api.deepseek.com/v1 -m deepseek-chat -k sk-xxx --test

    OpenAI-compatible providers are selected by base_url. Supplying -u, -m,
    or -k without -p automatically changes mock to openai_compatible.
    """
    state_settings = ctx.obj.settings()
    paths = state_settings.paths
    changed: list[str] = []
    if provider:
        _write_config_value(paths, "model.provider", provider)
        changed.append(f"provider={provider}")
    if base_url:
        _write_config_value(paths, "model.base_url", base_url)
        changed.append(f"base_url={safe_endpoint_display(base_url)}")
    if model:
        _write_config_value(paths, "model.model", model)
        changed.append(f"model={model}")
    if api_key:
        _write_config_value(paths, "model.api_key", api_key)
        changed.append(f"api_key={_mask(api_key)}")
    if not changed:
        error("No fields were provided. Use -p, -u, -m, or -k.")
        raise typer.Exit(2)
    # If the user pointed at a real endpoint but never chose a provider, don't
    # leave them stranded on the offline mock — default to the OpenAI-compatible
    # protocol so the endpoint is actually used.
    if not provider and (base_url or api_key) and _is_mock_provider(state_settings.model.provider):
        _write_config_value(paths, "model.provider", "openai_compatible")
        changed.insert(0, "provider=openai_compatible (automatic)")
    success("Updated model configuration: " + ", ".join(changed))
    _mark_model_unverified(ctx.obj.settings())
    if test:
        _run_connectivity_test(ctx)
    else:
        info("Model configuration is saved but unverified; run `config test` before research work.")


@app.command("vlm")
def vlm_cmd(
    ctx: typer.Context,
    endpoint: str = typer.Option(
        "",
        "--endpoint",
        "--base-url",
        "-u",
        help="Complete multimodal endpoint URL; stored without path rewriting.",
    ),
    model: str = typer.Option("", "--model", "-m", help="Vision-capable model name."),
    api_key: str = typer.Option(
        "", "--api-key", "-k", help="Token stored separately in secrets.toml."
    ),
    protocol: str = typer.Option(
        "", "--protocol", help="Wire protocol (default: openai_compatible_chat)."
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", "--timeout-s", help="Request timeout in seconds."
    ),
    enabled: bool | None = typer.Option(
        None, "--enable/--disable", help="Enable or disable VLM-backed skills."
    ),
    test: bool = typer.Option(
        False, "--test", help="Verify the saved endpoint with a tiny multimodal request."
    ),
) -> None:
    """Configure the optional vision model shared by VLM-backed skills."""
    settings = ctx.obj.settings()
    vlm = settings.vlm
    paths = settings.paths
    supplied = bool(endpoint or model or api_key or protocol or timeout is not None)

    if enabled is None and not supplied and not test:
        kv_table("Vision model (VLM)", [
            ("enabled", vlm.enabled),
            ("model", vlm.model or "(unset)"),
            ("endpoint", vlm.endpoint or "(unset)"),
            ("protocol", vlm.protocol),
            ("timeout_s", vlm.timeout_s),
            ("api_key", "***set***" if vlm.api_key else "(unset)"),
        ])
        info("Configure with `config vlm -u <ENDPOINT> -m <MODEL> -k <API_KEY>`.")
        return

    if timeout is not None and timeout <= 0:
        error("VLM timeout must be greater than zero seconds.")
        raise typer.Exit(2)

    # Validate every supplied value before writing either the public config or
    # secrets file. A rejected endpoint/protocol must not leave a mixed profile.
    try:
        if endpoint:
            validate_vlm_endpoint(endpoint.strip())
        if protocol:
            validate_vlm_protocol(protocol.strip())
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(2) from exc

    changed: list[str] = []
    if endpoint:
        value = endpoint.strip()
        _write_config_value(paths, "vlm.endpoint", value)
        changed.append(f"endpoint={safe_endpoint_display(value)}")
    if model:
        value = model.strip()
        _write_config_value(paths, "vlm.model", value)
        changed.append(f"model={value}")
    if api_key:
        _write_config_value(paths, "vlm.api_key", api_key)
        changed.append(f"api_key={_mask(api_key)}")
    if protocol:
        value = protocol.strip()
        _write_config_value(paths, "vlm.protocol", value)
        changed.append(f"protocol={value}")
    if timeout is not None:
        _write_config_value(paths, "vlm.timeout_s", timeout)
        changed.append(f"timeout_s={timeout:g}")

    if enabled is not None or supplied:
        resolved_enabled = enabled if enabled is not None else True
        _write_config_value(paths, "vlm.enabled", resolved_enabled)
        changed.append(f"enabled={str(resolved_enabled).lower()}")
    if changed:
        success("Updated VLM configuration: " + ", ".join(changed))
    if test:
        _run_vlm_connectivity_test(ctx)


def _run_vlm_connectivity_test(ctx: typer.Context) -> bool:
    """Test fresh effective VLM settings without logging endpoint response bodies."""
    from omni.cli.state import run_async

    config = ctx.obj.settings().vlm
    info(f"Testing VLM {config.protocol} / {config.model or '(unset)'}...")
    ok, detail = run_async(check_vlm_connectivity(config))
    (success if ok else error)(detail)
    if not ok:
        info("Update the VLM with `config vlm ...`, then run `config vlm --test` again.")
    return ok


@app.command("semantic-scholar")
def semantic_scholar_cmd(
    ctx: typer.Context,
    api_key: str = typer.Option(
        "", "--api-key", "-k", help="Semantic Scholar token stored in secrets.toml."
    ),
    test: bool = typer.Option(
        False, "--test", help="Verify the configured token with one small metadata request."
    ),
) -> None:
    """Configure the owner-scoped Semantic Scholar credential."""
    settings = ctx.obj.settings()
    current = settings.research.semantic_scholar_api_key
    if not api_key and not test:
        kv_table(
            "Semantic Scholar",
            [
                ("connector_enabled", "semanticscholar" in settings.research.connectors),
                ("api_key", "***set***" if current else "(unset)"),
            ],
        )
        info("Configure with `config semantic-scholar -k <API_KEY> --test`.")
        return

    if api_key:
        _write_config_value(
            settings.paths,
            "research.semantic_scholar_api_key",
            api_key.strip(),
        )
        success(
            "Updated Semantic Scholar configuration: "
            f"api_key={_mask(api_key.strip())}"
        )
    if test:
        _run_semantic_scholar_connectivity_test(ctx)


def _run_semantic_scholar_connectivity_test(ctx: typer.Context) -> bool:
    """Test Semantic Scholar without exposing its token or response body."""
    from omni.cli.state import run_async
    from omni.research import connectors

    api_key = ctx.obj.settings().research.semantic_scholar_api_key
    if not api_key:
        error(
            "Semantic Scholar API key is not configured. "
            "Run `config semantic-scholar -k <API_KEY>` first."
        )
        return False
    info("Testing Semantic Scholar credentials...")
    try:
        results = run_async(
            connectors.semanticscholar_search(
                "automated peer review large language model",
                rows=1,
                api_key=api_key,
            )
        )
    except Exception as exc:  # noqa: BLE001 - CLI converts connector errors to safe text
        error(f"Semantic Scholar test failed: {exc}")
        return False
    if not results:
        error("Semantic Scholar responded but returned no result for the test query.")
        return False
    success("Semantic Scholar credentials are working.")
    return True


@app.command("embeddings")
def embeddings_cmd(
    ctx: typer.Context,
    enabled: bool | None = typer.Option(
        None, "--enable/--disable",
        help="Enable semantic recall or disable embeddings for keyword recall.",
    ),
    base_url: str = typer.Option(
        "", "--base-url", "-u", help="Endpoint providing /embeddings.",
    ),
    api_key: str = typer.Option(
        "", "--api-key", "-k", help="Embedding token stored in secrets.toml.",
    ),
    model: str = typer.Option(
        "", "--model", "-m", help="Embedding model, such as text-embedding-3-small or bge-m3.",
    ),
    provider: str = typer.Option(
        "",
        "--provider",
        "-p",
        help="Embedding provider: openai_compatible or local specter2.",
    ),
    local_python: str = typer.Option(
        "",
        "--python",
        help="Dedicated Python executable containing torch, transformers, and adapters.",
    ),
    local_base_model: str = typer.Option(
        "",
        "--base-model",
        help="Local SPECTER2 base-model directory.",
    ),
    local_adapter: str = typer.Option(
        "",
        "--adapter",
        help="Local SPECTER2 proximity-adapter directory.",
    ),
    device: str = typer.Option(
        "",
        "--device",
        help="Local SPECTER2 device: cpu, mps, cuda, or cuda:N.",
    ),
) -> None:
    """Configure remote or local semantic embeddings."""
    settings = ctx.obj.settings()
    memory = settings.memory
    paths = settings.paths
    local_values_supplied = bool(
        local_python or local_base_model or local_adapter or device
    )
    any_values_supplied = bool(
        base_url or api_key or model or provider or local_values_supplied
    )

    if enabled is None:
        if any_values_supplied:
            error("Choose either --enable or --disable.")
            raise typer.Exit(2)
        rows: list[tuple[str, Any]] = [
            ("enabled", memory.embeddings_enabled),
            ("provider", memory.embedding_provider or "(unset)"),
            (
                "base_url",
                (
                    "(not used by local SPECTER2)"
                    if memory.embedding_provider == "specter2"
                    else memory.embedding_base_url or "(unset)"
                ),
            ),
            ("model", memory.embedding_model or "(unset)"),
            (
                "api_key",
                (
                    "(not used by local SPECTER2)"
                    if memory.embedding_provider == "specter2"
                    else (
                        "***set***"
                        if memory.embedding_api_key
                        else "(unset; model token is reused only for the same origin)"
                    )
                ),
            ),
        ]
        if memory.embedding_provider == "specter2":
            rows.extend(
                [
                    (
                        "python",
                        memory.embedding_specter2_python or "(unset)",
                    ),
                    (
                        "base_model",
                        memory.embedding_specter2_base_model or "(unset)",
                    ),
                    (
                        "adapter",
                        memory.embedding_specter2_adapter or "(unset)",
                    ),
                    ("device", memory.embedding_specter2_device),
                ]
            )
        kv_table("Embedding recall", rows)
        info(
            "Use `config embeddings --enable -u <URL> -m <MODEL> -k <KEY>` "
            "or choose local `-p specter2 --python ... --base-model ... "
            "--adapter ...`."
        )
        return

    if not enabled:
        if any_values_supplied:
            error("Do not combine --disable with provider configuration options.")
            raise typer.Exit(2)
        _write_config_value(paths, "memory.embeddings_enabled", False)
        success("Embeddings disabled. Keyword recall will be used; endpoint settings are retained.")
        return

    requested_provider = provider.strip().casefold()
    if not requested_provider:
        if base_url or api_key:
            requested_provider = "openai_compatible"
        else:
            requested_provider = (
                str(memory.embedding_provider or "openai_compatible")
                .strip()
                .casefold()
            )
    if requested_provider == "openai":
        requested_provider = "openai_compatible"
    if requested_provider not in {"openai_compatible", "specter2"}:
        error("Embedding provider must be openai_compatible or specter2.")
        raise typer.Exit(2)

    if requested_provider == "specter2":
        if base_url or api_key:
            error("Local SPECTER2 does not use --base-url or --api-key.")
            raise typer.Exit(2)
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
            error(
                "SPECTER2 requires --python, --base-model, and --adapter "
                "the first time it is configured."
            )
            raise typer.Exit(2)
        try:
            # Preserve a virtual-environment launcher symlink. Resolving it can
            # bypass that environment's ``pyvenv.cfg`` and start the base
            # interpreter without the packages installed in the selected env.
            python_path = Path(
                os.path.abspath(os.path.expanduser(resolved_python))
            )
            base_path = Path(resolved_base).expanduser().resolve(strict=True)
            adapter_path = Path(resolved_adapter).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            error("A configured SPECTER2 local path does not exist.")
            raise typer.Exit(2) from None
        if not python_path.is_file() or not os.access(python_path, os.X_OK):
            error("The SPECTER2 Python executable is not an executable file.")
            raise typer.Exit(2)
        if not base_path.is_dir() or not adapter_path.is_dir():
            error("The SPECTER2 base model and adapter must be directories.")
            raise typer.Exit(2)
        if not re.fullmatch(r"(?:cpu|mps|cuda(?::\d+)?)", resolved_device):
            error("SPECTER2 device must be cpu, mps, cuda, or cuda:N.")
            raise typer.Exit(2)
        resolved_model = (
            model.strip()
            or (
                memory.embedding_model
                if memory.embedding_provider == "specter2"
                else ""
            )
            or "allenai/specter2-proximity"
        )
        _write_config_value(paths, "memory.embedding_provider", "specter2")
        _write_config_value(paths, "memory.embedding_model", resolved_model)
        _write_config_value(paths, "memory.embedding_dim", 768)
        _write_config_value(
            paths,
            "memory.embedding_specter2_python",
            str(python_path),
        )
        _write_config_value(
            paths,
            "memory.embedding_specter2_base_model",
            str(base_path),
        )
        _write_config_value(
            paths,
            "memory.embedding_specter2_adapter",
            str(adapter_path),
        )
        _write_config_value(
            paths,
            "memory.embedding_specter2_device",
            resolved_device,
        )
        _write_config_value(paths, "memory.embeddings_enabled", True)
        success(
            f"Enabled local SPECTER2 embeddings: {resolved_model} "
            f"on {resolved_device} (768 dimensions)"
        )
        return

    if local_values_supplied:
        error("Local Python/model/adapter/device options require -p specter2.")
        raise typer.Exit(2)
    resolved_base = (base_url or memory.embedding_base_url).rstrip("/")
    resolved_model = model or memory.embedding_model or "text-embedding-3-small"
    if not resolved_base:
        error(
            "Enabling embeddings requires -u/--base-url and an endpoint that provides /embeddings."
        )
        raise typer.Exit(2)

    _write_config_value(paths, "memory.embedding_provider", requested_provider)
    _write_config_value(paths, "memory.embedding_base_url", resolved_base)
    _write_config_value(paths, "memory.embedding_model", resolved_model)
    if api_key:
        _write_config_value(paths, "memory.embedding_api_key", api_key)
    _write_config_value(paths, "memory.embeddings_enabled", True)
    success(
        f"Enabled semantic recall: {resolved_model} @ "
        f"{safe_endpoint_display(resolved_base)}"
    )
    if not api_key and not memory.embedding_api_key:
        warn(
            "No dedicated embedding API key is configured. The model token is reused "
            "only when the model and embedding endpoints have the same origin; "
            "otherwise configure -k."
        )


def _is_mock_provider(provider: str) -> bool:
    return (provider or "").strip().lower() in ("", "mock", "offline")


@app.command("test")
def test_cmd(ctx: typer.Context) -> None:
    """Test the active model endpoint, token, and model."""
    _run_connectivity_test(ctx)


def _run_connectivity_test(ctx: typer.Context) -> bool:
    from omni.cli.state import run_async
    from omni.core.llm.client import check_connectivity
    from omni.core.llm.health import record_model_health

    # ``settings()`` reads config files fresh each call, so the values written
    # above are already in effect — no daemon/restart needed.
    s = ctx.obj.settings()
    info(f"Testing {s.model.provider} / {s.model.model}...")
    ok, detail = run_async(check_connectivity(s))
    record_model_health(
        s.paths,
        s.model,
        status="verified" if ok else "failed",
        message=detail,
    )
    (success if ok else error)(detail)
    if not ok:
        info(
            "Update the credential or endpoint with "
            f"`{spell_commands('/model main ...')}`, then run "
            f"`{spell_commands('/config test')}` again."
        )
    return ok


def _mark_model_unverified(settings) -> None:  # noqa: ANN001
    from omni.core.llm.health import record_model_health

    record_model_health(
        settings.paths,
        settings.model,
        status="unverified",
        message="Model configuration changed and has not been tested.",
    )


@app.command("path")
def path_cmd(ctx: typer.Context) -> None:
    """Show configuration file paths."""
    p = ctx.obj.settings().paths
    _, source = user_home_resolution()
    kv_table("Configuration paths", [
        ("data directory", p.home),
        ("data directory source", source),
        ("saved home selection", home_selection_file()),
        ("user config", p.config_file),
        ("secrets", p.secrets_file),
        ("role", p.role_file),
        ("project dir", p.project_dir),
        ("project config", p.project_config),
    ])


@app.command("unset")
def unset_cmd(ctx: typer.Context, key: str) -> None:
    """Remove a user setting by full dotted path."""
    p = get_paths(project=ctx.obj.project)
    key = _resolve_key_or_exit(key)
    for target in (p.config_file, p.secrets_file):
        if not target.is_file():
            continue
        data = _read_editable_toml(target)
        parts = key.split(".")
        cur = data
        ok = True
        for part in parts[:-1]:
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur[part]
        if ok and isinstance(cur, dict) and parts[-1] in cur:
            del cur[parts[-1]]
            if target == p.secrets_file:
                write_private_toml(target, data)
            else:
                with target.open("wb") as fh:
                    tomli_w.dump(data, fh)
            if key.startswith("model."):
                _mark_model_unverified(ctx.obj.settings())
            success(f"Removed {key} ({target})")
            if key == "research.semantic_scholar_api_key":
                _semantic_scholar_reload_notice()
            return
    error(f"Setting {key} was not found.")
