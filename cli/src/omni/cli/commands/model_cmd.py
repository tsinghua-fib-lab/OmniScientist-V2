"""`omni model` — one explainable surface for the three existing model roles."""

from __future__ import annotations

import copy
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal

import click
import typer
from typer.core import TyperGroup

from omni.cli.command_surface import spell_commands
from omni.cli.commands import config_cmd
from omni.cli.render import data_table, error, info, prompt_secret, prompt_text, warn
from omni.config import SettingsResolution, resolve_settings
from omni.config.model_discovery import (
    ModelSeed,
    environment_model_is_unpersisted,
    process_environment_seed,
    resolve_named_main_model,
)
from omni.config.model_stack import (
    ModelProviderPreset,
    ModelRole,
    ResolvedModelStack,
    providers_for,
    resolve_model_stack,
)


class ModelGroup(TyperGroup):
    """Route ``omni model <name>`` to ``use`` the way Claude Code routes ``/model``."""

    def resolve_command(self, ctx: click.Context, args: list[str]):  # noqa: ANN201
        if args:
            first = args[0]
            if not first.startswith("-") and self.get_command(ctx, first) is None:
                args = ["use", *args]
        return super().resolve_command(ctx, args)


app = typer.Typer(
    help="Configure and explain the persistent main, vision, and embedding model stack.",
    invoke_without_command=True,
    no_args_is_help=False,
    cls=ModelGroup,
)


@dataclass(frozen=True)
class ModelEdit:
    """Validated interactive intent delegated to an existing config command."""

    role: ModelRole
    values: Mapping[str, Any]


def _resolution_for_state(state) -> SettingsResolution:  # noqa: ANN001
    if getattr(state, "trusted", None) is None and hasattr(state, "trusted"):
        # Source reporting should match the next Agent without opening a trust
        # prompt merely to inspect configuration.  REPL state is already
        # resolved; one-shot status/explain follows the existing fail-closed
        # non-interactive trust decision.
        from omni.cli.state import resolve_workspace_trust

        resolve_workspace_trust(state, interactive=False)
    overrides = copy.deepcopy(dict(getattr(state, "overrides", {}) or {}))
    if model := getattr(state, "model", None):
        overrides.setdefault("model", {})["model"] = model
    return resolve_settings(
        project=getattr(state, "project", None),
        profile=getattr(state, "profile", None),
        overrides=overrides or None,
        trusted=getattr(state, "trusted", None),
    )


def render_model_status(state) -> None:  # noqa: ANN001
    """Show the effective three-role stack without reading credential values."""
    resolution = _resolution_for_state(state)
    stack = resolve_model_stack(resolution)
    rows = []
    for target in stack.roles:
        provider = target.provider or target.protocol or "(unset)"
        enabled_state = "enabled" if target.enabled else "disabled"
        destination = f"{provider}/{target.model or '(unset)'}"
        if target.endpoint:
            destination += f" @ {target.endpoint}"
        rows.append(
            [
                target.role.value,
                enabled_state,
                destination,
                "configured" if target.credential_configured else "unset",
                target.source_summary,
            ]
        )
    data_table(
        "Effective model stack",
        ["role", "state", "provider/model", "credential", "source(s)"],
        rows,
    )
    info(
        f"Persistent model settings belong to this Omni Home: {resolution.settings.paths.home}. "
        "The existing --model option remains a non-persistent override for one launch."
    )
    _warn_unpersisted_environment(resolution)


def render_model_explain(state, role: str = "") -> None:  # noqa: ANN001
    """Explain the winning layer for each effective model configuration field."""
    resolution = _resolution_for_state(state)
    stack = resolve_model_stack(resolution)
    if role:
        try:
            targets = (stack.for_role(role),)
        except ValueError as exc:
            error(str(exc))
            raise typer.Exit(2) from exc
    else:
        targets = stack.roles
    rows = []
    for target in targets:
        for field in target.fields:
            rows.append(
                [
                    target.role.value,
                    field.path,
                    field.display_value,
                    field.source.kind,
                    field.source.detail,
                ]
            )
    data_table(
        "Model configuration sources",
        ["role", "field", "effective value", "source", "location/detail"],
        rows,
    )


def _render_save_outcome(
    state,
    role: ModelRole,
    changed_paths: tuple[str, ...],
) -> None:  # noqa: ANN001
    """Name the unchanged persistence scope and any higher-layer shadowing."""
    resolution = _resolution_for_state(state)
    target = resolve_model_stack(resolution).for_role(role)
    info(
        "Persistence scope: Home (unchanged) — public fields are in "
        f"{resolution.settings.paths.config_file}; credentials are in "
        f"{resolution.settings.paths.secrets_file}."
    )
    shadowed = []
    for path in dict.fromkeys(changed_paths):
        field = target.field_for(path)
        if field.source.kind in {"profile", "project", "override"}:
            shadowed.append(field)
    if shadowed:
        details = "; ".join(
            f"{field.path} by {field.source.kind} ({field.source.detail})"
            for field in shadowed
        )
        warn(
            "Saved the Home default, but a higher layer still overrides these "
            "effective fields: "
            f"{details}."
        )
    else:
        sources = tuple(
            dict.fromkeys(target.field_for(path).source.label for path in changed_paths)
        )
        info(
            f"Saved {role.value} fields are effective from: "
            f"{', '.join(sources) or 'the existing Home layers'}."
        )


def _changed_paths_for_edit(edit: ModelEdit) -> tuple[str, ...]:
    values = edit.values
    if edit.role is ModelRole.MAIN:
        mapping = {
            "provider": "model.provider",
            "base_url": "model.base_url",
            "model": "model.model",
            "api_key": "model.api_key",
        }
        return tuple(path for key, path in mapping.items() if values.get(key))
    if edit.role is ModelRole.VISION:
        mapping = {
            "endpoint": "vlm.endpoint",
            "model": "vlm.model",
            "api_key": "vlm.api_key",
            "protocol": "vlm.protocol",
        }
        paths = [path for key, path in mapping.items() if values.get(key)]
        if values.get("timeout") is not None:
            paths.append("vlm.timeout_s")
        if paths or values.get("enabled") is not None:
            paths.append("vlm.enabled")
        return tuple(paths)

    if values.get("enabled") is False:
        return ("memory.embeddings_enabled",)
    paths = [
        "memory.embeddings_enabled",
        "memory.embedding_provider",
        "memory.embedding_model",
    ]
    local_requested = str(values.get("provider") or "").casefold() == "specter2" or any(
        values.get(key)
        for key in ("local_python", "local_base_model", "local_adapter", "device")
    )
    if local_requested:
        paths.extend(
            (
                "memory.embedding_dim",
                "memory.embedding_specter2_python",
                "memory.embedding_specter2_base_model",
                "memory.embedding_specter2_adapter",
                "memory.embedding_specter2_device",
            )
        )
    else:
        paths.append("memory.embedding_base_url")
        if values.get("api_key"):
            paths.append("memory.embedding_api_key")
    return tuple(paths)


def apply_model_edit(state, edit: ModelEdit) -> None:  # noqa: ANN001
    """Apply an interactive edit through the pre-existing config commands."""
    ctx = SimpleNamespace(obj=state)
    values = dict(edit.values)
    if edit.role is ModelRole.MAIN:
        config_cmd.model_cmd(ctx, **values)
    elif edit.role is ModelRole.VISION:
        config_cmd.vlm_cmd(ctx, **values)
    else:
        config_cmd.embeddings_cmd(ctx, **values)
    _render_save_outcome(state, edit.role, _changed_paths_for_edit(edit))


def _prompt_main(stack: ResolvedModelStack) -> ModelEdit:
    current = stack.for_role(ModelRole.MAIN)
    provider = prompt_text("Provider", current.provider or "openai")
    endpoint = prompt_text(
        "Base URL (blank keeps the configured value)"
        if current.endpoint_redacted
        else "Base URL",
        "" if current.endpoint_redacted else current.endpoint,
    )
    model = prompt_text(
        "Model name",
        "" if current.model == "omni-mock" else current.model,
    )
    api_key = prompt_secret("API key (blank keeps the configured key)")
    return ModelEdit(
        ModelRole.MAIN,
        {
            "provider": provider.strip(),
            "base_url": endpoint.strip(),
            "api_key": api_key.strip(),
            "model": model.strip(),
            "test": False,
        },
    )


def _prompt_vision(stack: ResolvedModelStack) -> ModelEdit:
    current = stack.for_role(ModelRole.VISION)
    endpoint = prompt_text(
        "Vision endpoint (blank keeps the configured value)"
        if current.endpoint_redacted
        else "Vision endpoint",
        "" if current.endpoint_redacted else current.endpoint,
    )
    model = prompt_text("Vision model", current.model)
    protocol = prompt_text(
        "Protocol",
        current.protocol or "openai_compatible_chat",
    )
    api_key = prompt_secret("API key (blank keeps the configured key)")
    return ModelEdit(
        ModelRole.VISION,
        {
            "endpoint": endpoint.strip(),
            "model": model.strip(),
            "api_key": api_key.strip(),
            "protocol": protocol.strip(),
            "timeout": None,
            "enabled": True,
            "test": False,
        },
    )


def _prompt_embedding(stack: ResolvedModelStack) -> ModelEdit | None:
    current = stack.for_role(ModelRole.EMBEDDING)
    default_mode = "local" if current.provider == "specter2" else "remote"
    mode = prompt_text("Embedding mode (remote/local/disable)", default_mode).casefold()
    if mode == "disable":
        return ModelEdit(
            ModelRole.EMBEDDING,
            _embedding_values(enabled=False),
        )
    if mode == "local":
        python = prompt_text("SPECTER2 Python executable")
        base_model = prompt_text("SPECTER2 base-model directory")
        adapter = prompt_text("SPECTER2 adapter directory")
        device = prompt_text(
            "Device",
            str(current.field_for("memory.embedding_specter2_device").value or "cpu"),
        )
        model = prompt_text("Embedding model", current.model)
        return ModelEdit(
            ModelRole.EMBEDDING,
            _embedding_values(
                enabled=True,
                provider="specter2",
                model=model.strip(),
                local_python=python.strip(),
                local_base_model=base_model.strip(),
                local_adapter=adapter.strip(),
                device=device.strip(),
            ),
        )
    if mode != "remote":
        warn("Embedding mode must be remote, local, or disable; no changes were made.")
        return None
    provider = prompt_text(
        "Embedding provider",
        current.provider if current.provider != "specter2" else "openai_compatible",
    )
    endpoint = prompt_text(
        "Embedding base URL (blank keeps the configured value)"
        if current.endpoint_redacted
        else "Embedding base URL",
        "" if current.endpoint_redacted else current.endpoint,
    )
    model = prompt_text("Embedding model", current.model)
    api_key = prompt_secret("API key (blank keeps the configured key)")
    return ModelEdit(
        ModelRole.EMBEDDING,
        _embedding_values(
            enabled=True,
            provider=provider.strip(),
            base_url=endpoint.strip(),
            model=model.strip(),
            api_key=api_key.strip(),
        ),
    )


@dataclass(frozen=True)
class _PickerItem:
    """One numbered choice in the Codex-style model picker."""

    kind: Literal["preset", "save_env", "custom", "vision", "embedding"]
    title: str
    detail: str
    preset: ModelProviderPreset | None = None
    seed: ModelSeed | None = None


def _warn_unpersisted_environment(resolution: SettingsResolution) -> None:
    """Tell the user when an isolated Home is running on env-only model config."""
    if not environment_model_is_unpersisted(resolution):
        return
    warn(
        "This Omni Home has no config.toml; the effective model comes from the "
        "process environment and is not persisted. Choose a preset or run "
        f"{spell_commands('/model <name>')} to save it here."
    )


def _picker_items(resolution: SettingsResolution) -> list[_PickerItem]:
    """Build the main-model list Codex shows, plus Omni's extra roles."""
    items = [
        _PickerItem(
            "preset",
            f"{preset.key} / {preset.default_model or '(offline)'}",
            preset.default_endpoint or preset.label,
            preset=preset,
        )
        for preset in providers_for(ModelRole.MAIN)
    ]
    seed = process_environment_seed(resolution)
    if seed is not None and not resolution.settings.paths.config_file.is_file():
        items.append(
            _PickerItem(
                "save_env",
                f"Save {seed.provider} / {seed.model} to this Home",
                seed.origin,
                seed=seed,
            )
        )
    items.extend(
        (
            _PickerItem("custom", "Custom main model", "provider, URL, model, and key"),
            _PickerItem("vision", "Vision / VLM", "optional multimodal endpoint"),
            _PickerItem("embedding", "Embedding recall", "semantic or keyword"),
        )
    )
    return items


def _edit_from_preset(preset: ModelProviderPreset) -> ModelEdit:
    return ModelEdit(
        ModelRole.MAIN,
        {
            "provider": preset.key,
            "base_url": preset.default_endpoint,
            "model": preset.default_model,
            "api_key": "",
            "test": False,
        },
    )


def _edit_from_seed(seed: ModelSeed) -> ModelEdit:
    return ModelEdit(
        ModelRole.MAIN,
        {
            "provider": seed.provider,
            "base_url": seed.base_url,
            "model": seed.model,
            "api_key": seed.api_key,
            "test": False,
        },
    )


def _edit_from_named_model(
    name: str,
    resolution: SettingsResolution,
) -> ModelEdit | None:
    current = resolve_model_stack(resolution).for_role(ModelRole.MAIN)
    choice = resolve_named_main_model(name, current)
    if choice is None:
        warn(
            f"Unknown model '{name}'. Choose a listed preset or configure "
            f"{spell_commands('/model main -p <provider> -u <URL> -m <MODEL>')}."
        )
        return None
    values: dict[str, Any] = {
        "model": choice.model,
        "api_key": "",
        "test": False,
        "provider": "",
        "base_url": "",
    }
    if not choice.keep_existing_endpoint:
        values["provider"] = choice.provider
        values["base_url"] = choice.base_url
    return ModelEdit(ModelRole.MAIN, values)


def prompt_model_edit(state) -> ModelEdit | None:  # noqa: ANN001
    """Prompt for one persistent Home edit; an empty choice cancels cleanly."""
    resolution = _resolution_for_state(state)
    render_model_status(state)
    items = _picker_items(resolution)
    data_table(
        "Model picker",
        ["#", "choice", "detail"],
        [[str(index), item.title, item.detail] for index, item in enumerate(items, start=1)],
    )
    info(
        "Type a number, a model name (for example deepseek-chat), or Enter to cancel. "
        "Vision and embedding stay on their own roles."
    )
    choice = prompt_text("Choose a model").strip()
    if not choice:
        return None
    if choice.isdigit():
        index = int(choice)
        if 1 <= index <= len(items):
            return _edit_from_picker_item(items[index - 1], resolution)
        warn("That number is not in the list; no changes were made.")
        return None
    roles = {
        "main": ModelRole.MAIN,
        "2": ModelRole.VISION,
        "vision": ModelRole.VISION,
        "vlm": ModelRole.VISION,
        "3": ModelRole.EMBEDDING,
        "embedding": ModelRole.EMBEDDING,
        "embeddings": ModelRole.EMBEDDING,
    }
    role = roles.get(choice.casefold())
    if role is ModelRole.MAIN:
        return _prompt_main(resolve_model_stack(resolution))
    if role is ModelRole.VISION:
        return _prompt_vision(resolve_model_stack(resolution))
    if role is ModelRole.EMBEDDING:
        return _prompt_embedding(resolve_model_stack(resolution))
    return _edit_from_named_model(choice, resolution)


def _edit_from_picker_item(
    item: _PickerItem,
    resolution: SettingsResolution,
) -> ModelEdit | None:
    if item.kind == "preset" and item.preset is not None:
        return _edit_from_preset(item.preset)
    if item.kind == "save_env" and item.seed is not None:
        return _edit_from_seed(item.seed)
    stack = resolve_model_stack(resolution)
    if item.kind == "custom":
        return _prompt_main(stack)
    if item.kind == "vision":
        return _prompt_vision(stack)
    return _prompt_embedding(stack)


def _embedding_values(
    *,
    enabled: bool,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    provider: str = "",
    local_python: str = "",
    local_base_model: str = "",
    local_adapter: str = "",
    device: str = "",
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "provider": provider,
        "local_python": local_python,
        "local_base_model": local_base_model,
        "local_adapter": local_adapter,
        "device": device,
    }


def _terminal_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@app.callback(invoke_without_command=True)
def model_root(ctx: typer.Context) -> None:
    """Open the model picker, or show status when stdin is non-interactive."""
    _resolution_for_state(ctx.obj)
    if ctx.invoked_subcommand is not None:
        return
    if not _terminal_is_interactive():
        render_model_status(ctx.obj)
        info("Run `omni model` in an interactive terminal to open the persistent model picker.")
        return
    edit = prompt_model_edit(ctx.obj)
    if edit is not None:
        apply_model_edit(ctx.obj, edit)


@app.command("status")
def status_cmd(ctx: typer.Context) -> None:
    """Show effective main, vision, and embedding models and their source layers."""
    render_model_status(ctx.obj)


@app.command("explain")
def explain_cmd(
    ctx: typer.Context,
    role: str = typer.Argument("", help="Optional role: main, vision/vlm, or embedding."),
) -> None:
    """Explain which configuration layer supplies every effective field."""
    render_model_explain(ctx.obj, role)


@app.command("main")
def main_cmd(
    ctx: typer.Context,
    provider: str = typer.Option("", "--provider", "-p"),
    base_url: str = typer.Option("", "--base-url", "-u"),
    api_key: str = typer.Option("", "--api-key", "-k"),
    model: str = typer.Option("", "--model", "-m"),
    test: bool = typer.Option(False, "--test"),
) -> None:
    """Configure the persistent main model using the existing Home scope."""
    if not any((provider, base_url, api_key, model)):
        if test:
            config_cmd.test_cmd(ctx)
        else:
            render_model_explain(ctx.obj, ModelRole.MAIN.value)
        return
    before = _resolution_for_state(ctx.obj).settings.model.provider
    config_cmd.model_cmd(ctx, provider, base_url, api_key, model, test)
    changed_paths = tuple(
        path
        for value, path in (
            (provider, "model.provider"),
            (base_url, "model.base_url"),
            (model, "model.model"),
            (api_key, "model.api_key"),
        )
        if value
    )
    if not provider and (base_url or api_key) and config_cmd._is_mock_provider(before):
        changed_paths = (*changed_paths, "model.provider")
    _render_save_outcome(ctx.obj, ModelRole.MAIN, changed_paths)


@app.command("vision")
def vision_cmd(
    ctx: typer.Context,
    endpoint: str = typer.Option("", "--endpoint", "--base-url", "-u"),
    model: str = typer.Option("", "--model", "-m"),
    api_key: str = typer.Option("", "--api-key", "-k"),
    protocol: str = typer.Option("", "--protocol"),
    timeout: float | None = typer.Option(None, "--timeout", "--timeout-s"),
    enabled: bool | None = typer.Option(None, "--enable/--disable"),
    test: bool = typer.Option(False, "--test"),
) -> None:
    """Configure the persistent vision/VLM role using the existing Home scope."""
    supplied = bool(endpoint or model or api_key or protocol or timeout is not None)
    if not supplied and enabled is None and not test:
        render_model_explain(ctx.obj, ModelRole.VISION.value)
        return
    config_cmd.vlm_cmd(ctx, endpoint, model, api_key, protocol, timeout, enabled, test)
    if supplied or enabled is not None:
        changed_paths = [
            path
            for value, path in (
                (endpoint, "vlm.endpoint"),
                (model, "vlm.model"),
                (api_key, "vlm.api_key"),
                (protocol, "vlm.protocol"),
            )
            if value
        ]
        if timeout is not None:
            changed_paths.append("vlm.timeout_s")
        changed_paths.append("vlm.enabled")
        _render_save_outcome(ctx.obj, ModelRole.VISION, tuple(changed_paths))


@app.command("embedding")
def embedding_cmd(
    ctx: typer.Context,
    enabled: bool | None = typer.Option(None, "--enable/--disable"),
    base_url: str = typer.Option("", "--base-url", "-u"),
    api_key: str = typer.Option("", "--api-key", "-k"),
    model: str = typer.Option("", "--model", "-m"),
    provider: str = typer.Option("", "--provider", "-p"),
    local_python: str = typer.Option("", "--python"),
    local_base_model: str = typer.Option("", "--base-model"),
    local_adapter: str = typer.Option("", "--adapter"),
    device: str = typer.Option("", "--device"),
) -> None:
    """Configure persistent remote or local embeddings using the existing scope."""
    supplied = bool(
        base_url
        or api_key
        or model
        or provider
        or local_python
        or local_base_model
        or local_adapter
        or device
    )
    if not supplied and enabled is None:
        render_model_explain(ctx.obj, ModelRole.EMBEDDING.value)
        return
    config_cmd.embeddings_cmd(
        ctx,
        enabled,
        base_url,
        api_key,
        model,
        provider,
        local_python,
        local_base_model,
        local_adapter,
        device,
    )
    if supplied or enabled is not None:
        values = _embedding_values(
            enabled=bool(enabled),
            base_url=base_url,
            api_key=api_key,
            model=model,
            provider=provider,
            local_python=local_python,
            local_base_model=local_base_model,
            local_adapter=local_adapter,
            device=device,
        )
        _render_save_outcome(
            ctx.obj,
            ModelRole.EMBEDDING,
            _changed_paths_for_edit(ModelEdit(ModelRole.EMBEDDING, values)),
        )


@app.command("use")
def use_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Provider key or model name, for example deepseek-chat."),
) -> None:
    """Switch the persistent main model with one name, like Claude Code's /model."""
    resolution = _resolution_for_state(ctx.obj)
    edit = _edit_from_named_model(name, resolution)
    if edit is None:
        raise typer.Exit(2)
    apply_model_edit(ctx.obj, edit)


# Compatibility aliases keep users from having to remember vision vs VLM or
# singular vs plural; hidden aliases do not clutter the picker/completer.
app.command("vlm", hidden=True)(vision_cmd)
app.command("embeddings", hidden=True)(embedding_cmd)


@app.command("help")
def help_cmd() -> None:
    """Show model-stack commands and persistence semantics."""
    data_table(
        "model subcommands",
        ["command", "purpose", "example"],
        [
            ["<name>", "Switch the main model in one step", spell_commands("/model deepseek-chat")],
            ["status", "Show effective roles and source layers", spell_commands("/model status")],
            ["explain [ROLE]", "Explain every effective config field", spell_commands("/model explain main")],
            ["main", "Configure the main model", spell_commands("/model main -p openai -u <URL> -m <MODEL> -k <KEY>")],
            ["vision", "Configure the VLM role", spell_commands("/model vision -u <ENDPOINT> -m <MODEL> -k <KEY>")],
            ["embedding", "Configure embedding recall", spell_commands("/model embedding --enable -u <URL> -m <MODEL> -k <KEY>")],
        ],
    )
    info(
        "Changes keep the existing persistent Home scope. Use --model on the root "
        "command for the existing one-launch, non-persistent override."
    )
