"""Discover and name model stacks without changing runtime layer precedence.

Runtime resolution stays Codex-shaped: override > project > profile >
user(``OMNI_HOME``) > environment > defaults. This module is only for
``/model <name>`` shortcuts and init/picker seeding. It never adds the host
``~/.omni`` as a live configuration layer when ``OMNI_HOME`` is isolated.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omni.config.model_stack import (
    MODEL_PROVIDER_CATALOG,
    ModelProviderPreset,
    ModelRole,
    ResolvedModelTarget,
    providers_for,
)
from omni.config.paths import default_user_home, home_selection_file
from omni.config.settings import SettingsResolution, _read_toml
from omni.core.model_catalog import source_for

_MOCK_PROVIDERS = frozenset({"", "mock", "offline"})
_CATALOG_SOURCE_TO_PRESET = {
    "openai/model-reference": "openai",
    "deepseek/models-and-pricing": "deepseek",
}
_LOCAL_NAME_HINTS = ("llama", "qwen", "mistral", "mixtral")


@dataclass(frozen=True)
class ModelSeed:
    """A complete main-model stack that can be persisted into the active Home."""

    provider: str
    base_url: str
    model: str
    api_key: str
    origin: str


@dataclass(frozen=True)
class NamedModelChoice:
    """How ``/model <name>`` should rewrite the persistent main role."""

    provider: str
    base_url: str
    model: str
    inferred_from: str
    keep_existing_endpoint: bool


def is_complete_main_model(provider: str, base_url: str, model: str) -> bool:
    """Return whether a main-model triple is usable without the offline mock."""
    normalized = (provider or "").strip().casefold()
    name = (model or "").strip()
    return (
        normalized not in _MOCK_PROVIDERS
        and bool((base_url or "").strip())
        and bool(name)
        and name != "omni-mock"
    )


def apply_main_preset_defaults(
    provider: str, base_url: str, model: str
) -> tuple[str, str, str]:
    """Fill a known provider's missing endpoint/model from the offline catalog."""
    key = (provider or "").strip().casefold()
    if key in _MOCK_PROVIDERS:
        return provider, base_url, model
    for preset in providers_for(ModelRole.MAIN):
        if preset.key == key:
            return (
                preset.key,
                (base_url or "").strip() or preset.default_endpoint,
                (model or "").strip() or preset.default_model,
            )
    return provider, base_url, model


def infer_preset_for_model_name(name: str) -> ModelProviderPreset | None:
    """Map a user-typed model or provider name onto one offline preset."""
    folded = (name or "").strip().casefold()
    if not folded:
        return None
    for preset in providers_for(ModelRole.MAIN):
        if folded == preset.key:
            return preset
        if preset.default_model and folded == preset.default_model.casefold():
            return preset
    mapped = _CATALOG_SOURCE_TO_PRESET.get(source_for(name))
    if mapped:
        return _preset_by_key(mapped)
    if any(hint in folded for hint in _LOCAL_NAME_HINTS):
        return _preset_by_key("ollama")
    return None


def resolve_named_main_model(
    name: str,
    current: ResolvedModelTarget,
) -> NamedModelChoice | None:
    """Resolve ``/model <name>`` the way Codex's picker resolves a catalog id.

    A known provider or vendor model switches the Home stack to that preset.
    An unknown name on an already-configured provider only changes the model
    id, keeping the current endpoint. An unknown name on mock is refused so
    we never persist a broken BYOK stack.
    """
    raw = (name or "").strip()
    if not raw:
        return None
    preset = infer_preset_for_model_name(raw)
    current_provider = (current.provider or "").strip().casefold()
    current_is_real = current_provider not in _MOCK_PROVIDERS
    if preset is not None and raw.casefold() == preset.key:
        return NamedModelChoice(
            provider=preset.key,
            base_url=preset.default_endpoint,
            model=preset.default_model,
            inferred_from="preset-key",
            keep_existing_endpoint=False,
        )
    if preset is not None and (
        not current_is_real or preset.key != current_provider
    ):
        model = raw if raw.casefold() != preset.key else preset.default_model
        return NamedModelChoice(
            provider=preset.key,
            base_url=preset.default_endpoint,
            model=model,
            inferred_from="catalog" if raw.casefold() != (preset.default_model or "").casefold() else "preset-model",
            keep_existing_endpoint=False,
        )
    if current_is_real:
        return NamedModelChoice(
            provider=current.provider,
            base_url=current.endpoint,
            model=raw,
            inferred_from="current-provider",
            keep_existing_endpoint=True,
        )
    return None


def process_environment_seed(resolution: SettingsResolution) -> ModelSeed | None:
    """Return a persistable stack when the process environment already has one.

    Isolation keeps host ``~/.omni`` out of the live layer stack. Environment
    variables still apply (Codex does the same with ``OPENAI_API_KEY``). This
    helper only *names* that stack so init/picker can write it into the
    isolated Home instead of clobbering it with mock.
    """
    env_fields = [
        path
        for path in (
            "model.provider",
            "model.base_url",
            "model.model",
            "model.api_key",
        )
        if resolution.source_for(path).kind == "environment"
    ]
    if not env_fields:
        return None
    model_cfg = resolution.settings.model
    provider = model_cfg.provider
    base_url = model_cfg.base_url
    model = model_cfg.model
    if (provider or "").strip().casefold() in _MOCK_PROVIDERS and model not in {
        "",
        "omni-mock",
    }:
        inferred = infer_preset_for_model_name(model)
        if inferred is not None:
            provider = inferred.key
            base_url = base_url or inferred.default_endpoint
    provider, base_url, model = apply_main_preset_defaults(provider, base_url, model)
    if not is_complete_main_model(provider, base_url, model):
        return None
    return ModelSeed(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=model_cfg.api_key,
        origin="process environment",
    )


def discover_host_seed(current_home: Path) -> ModelSeed | None:
    """Read a complete stack from the machine's default/saved Home, if different.

    Used only as an interactive init offer. Never consulted by ``resolve_settings``.
    """
    for home in iter_host_homes(current_home):
        cfg = _read_toml(home / "config.toml")
        secrets = _read_toml(home / "secrets.toml")
        public = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
        private = secrets.get("model") if isinstance(secrets.get("model"), dict) else {}
        provider, base_url, model = apply_main_preset_defaults(
            str(public.get("provider") or ""),
            str(public.get("base_url") or ""),
            str(public.get("model") or ""),
        )
        if not is_complete_main_model(provider, base_url, model):
            continue
        return ModelSeed(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=str(private.get("api_key") or ""),
            origin=f"host home ({home})",
        )
    return None


def discover_init_seed(
    resolution: SettingsResolution,
    *,
    allow_host: bool,
) -> ModelSeed | None:
    """Prefer a complete environment stack; optionally fall back to host Home."""
    if seed := process_environment_seed(resolution):
        return seed
    if allow_host:
        return discover_host_seed(resolution.settings.paths.home)
    return None


def iter_host_homes(current_home: Path) -> tuple[Path, ...]:
    """Return discoverable host Homes that are not the active ``OMNI_HOME``."""
    current = current_home.expanduser().resolve()
    found: list[Path] = []
    pointer = home_selection_file()
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    if raw:
        saved = Path(raw).expanduser().resolve()
        if saved != current and saved.is_dir():
            found.append(saved)
    default = default_user_home()
    if default != current and default not in found:
        found.append(default)
    return tuple(found)


def environment_model_is_unpersisted(resolution: SettingsResolution) -> bool:
    """True when the effective main model comes from env and Home has no file."""
    if resolution.settings.paths.config_file.is_file():
        return False
    return process_environment_seed(resolution) is not None


def _preset_by_key(key: str) -> ModelProviderPreset | None:
    for preset in MODEL_PROVIDER_CATALOG:
        if preset.key == key:
            return preset
    return None
