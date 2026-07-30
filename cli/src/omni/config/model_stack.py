"""Typed configuration view for Omni's main, vision, and embedding models.

The runtime keeps its established ``ModelCfg``/``VlmCfg``/``MemoryCfg`` shapes.
This module is a configuration-only adapter: it gives the CLI one catalog of
roles and providers, plus a uniform effective/source view, without changing how
an agent chooses or executes a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from omni.config.settings import ConfigSource, SettingsResolution


class ModelRole(StrEnum):
    """The three independently configured model roles exposed by Omni."""

    MAIN = "main"
    VISION = "vision"
    EMBEDDING = "embedding"


_ROLE_ALIASES = {
    "main": ModelRole.MAIN,
    "model": ModelRole.MAIN,
    "llm": ModelRole.MAIN,
    "vision": ModelRole.VISION,
    "vlm": ModelRole.VISION,
    "embedding": ModelRole.EMBEDDING,
    "embeddings": ModelRole.EMBEDDING,
}


def parse_model_role(value: str | ModelRole) -> ModelRole:
    """Resolve user-facing aliases to one canonical role."""
    if isinstance(value, ModelRole):
        return value
    role = _ROLE_ALIASES.get(value.strip().casefold())
    if role is None:
        expected = ", ".join(role.value for role in ModelRole)
        raise ValueError(f"Unknown model role '{value}'; choose {expected}.")
    return role


@dataclass(frozen=True)
class ModelRoleSpec:
    """Mapping from one role to the existing dotted configuration fields."""

    role: ModelRole
    title: str
    enabled_path: str | None
    provider_path: str | None
    endpoint_path: str | None
    model_path: str
    credential_path: str | None
    protocol_path: str | None = None
    extra_paths: tuple[str, ...] = ()

    @property
    def field_paths(self) -> tuple[str, ...]:
        return tuple(
            path
            for path in (
                self.enabled_path,
                self.provider_path,
                self.endpoint_path,
                self.model_path,
                self.protocol_path,
                self.credential_path,
                *self.extra_paths,
            )
            if path
        )


MODEL_ROLE_SPECS: tuple[ModelRoleSpec, ...] = (
    ModelRoleSpec(
        role=ModelRole.MAIN,
        title="Main model",
        enabled_path=None,
        provider_path="model.provider",
        endpoint_path="model.base_url",
        model_path="model.model",
        credential_path="model.api_key",
    ),
    ModelRoleSpec(
        role=ModelRole.VISION,
        title="Vision model (VLM)",
        enabled_path="vlm.enabled",
        provider_path=None,
        endpoint_path="vlm.endpoint",
        model_path="vlm.model",
        credential_path="vlm.api_key",
        protocol_path="vlm.protocol",
        extra_paths=("vlm.timeout_s",),
    ),
    ModelRoleSpec(
        role=ModelRole.EMBEDDING,
        title="Embedding model",
        enabled_path="memory.embeddings_enabled",
        provider_path="memory.embedding_provider",
        endpoint_path="memory.embedding_base_url",
        model_path="memory.embedding_model",
        credential_path="memory.embedding_api_key",
        extra_paths=(
            "memory.embedding_dim",
            "memory.embedding_specter2_python",
            "memory.embedding_specter2_base_model",
            "memory.embedding_specter2_adapter",
            "memory.embedding_specter2_device",
        ),
    ),
)


@dataclass(frozen=True)
class ModelProviderPreset:
    """Offline provider metadata used for suggestions, never runtime routing."""

    key: str
    label: str
    roles: tuple[ModelRole, ...]
    protocols: tuple[tuple[ModelRole, str], ...]
    default_endpoint: str = ""
    default_model: str = ""

    def protocol_for(self, role: str | ModelRole) -> str:
        """Return the executable protocol spelling for one supported role."""
        resolved = parse_model_role(role)
        for candidate, protocol in self.protocols:
            if candidate is resolved:
                return protocol
        raise KeyError(resolved)


MODEL_PROVIDER_CATALOG: tuple[ModelProviderPreset, ...] = (
    ModelProviderPreset(
        "openai",
        "OpenAI / OpenAI-compatible",
        (ModelRole.MAIN, ModelRole.VISION, ModelRole.EMBEDDING),
        (
            (ModelRole.MAIN, "openai_compatible"),
            (ModelRole.VISION, "openai_compatible_chat"),
            (ModelRole.EMBEDDING, "openai_compatible"),
        ),
        "https://api.openai.com/v1",
        "gpt-4o-mini",
    ),
    ModelProviderPreset(
        "deepseek",
        "DeepSeek",
        (ModelRole.MAIN,),
        ((ModelRole.MAIN, "openai_compatible"),),
        "https://api.deepseek.com/v1",
        "deepseek-chat",
    ),
    ModelProviderPreset(
        "ollama",
        "Ollama",
        (ModelRole.MAIN, ModelRole.EMBEDDING),
        (
            (ModelRole.MAIN, "openai_compatible"),
            (ModelRole.EMBEDDING, "openai_compatible"),
        ),
        "http://localhost:11434/v1",
        "llama3.1",
    ),
    ModelProviderPreset(
        "mock",
        "Offline mock",
        (ModelRole.MAIN,),
        ((ModelRole.MAIN, "mock"),),
        "",
        "omni-mock",
    ),
    ModelProviderPreset(
        "specter2",
        "Local SPECTER2",
        (ModelRole.EMBEDDING,),
        ((ModelRole.EMBEDDING, "local"),),
    ),
)


def providers_for(role: str | ModelRole) -> tuple[ModelProviderPreset, ...]:
    """Return offline provider suggestions compatible with ``role``."""
    resolved = parse_model_role(role)
    return tuple(item for item in MODEL_PROVIDER_CATALOG if resolved in item.roles)


def safe_endpoint_display(value: str) -> str:
    """Remove credentials and opaque URL suffixes from a displayed endpoint.

    Model endpoints normally do not need query strings or fragments.  Treat both
    as opaque because providers sometimes place access tokens there, and discard
    URL userinfo entirely.  The effective setting itself is left untouched.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        contains_opaque_part = any(mark in raw for mark in ("@", "?", "#"))
        return "(redacted endpoint)" if contains_opaque_part else raw

    # Without ``//``, urlsplit can treat userinfo as a path.  Do not echo an
    # ambiguous value containing ``@`` because it may still include a password.
    if "@" in parsed.path and not parsed.netloc:
        return "(redacted endpoint)"

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        hostname = f"{hostname}:{port}"
    netloc = hostname if parsed.netloc else ""
    query = "REDACTED" if parsed.query else ""
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


@dataclass(frozen=True)
class ResolvedModelField:
    """One effective field with redaction-aware display and provenance."""

    path: str
    value: Any
    source: ConfigSource
    sensitive: bool = False

    @property
    def display_value(self) -> str:
        if self.sensitive:
            return "configured" if self.value else "unset"
        if self.value in (None, ""):
            return "(unset)"
        if isinstance(self.value, bool):
            return str(self.value).lower()
        return str(self.value)


@dataclass(frozen=True)
class ResolvedModelTarget:
    """Uniform effective view of one configured model role."""

    role: ModelRole
    title: str
    enabled: bool
    provider: str
    endpoint: str
    endpoint_redacted: bool
    model: str
    protocol: str
    credential_configured: bool
    fields: tuple[ResolvedModelField, ...]

    @property
    def source_summary(self) -> str:
        """Describe all winning layers instead of inventing one role-wide owner."""
        kinds = tuple(dict.fromkeys(field.source.kind for field in self.fields))
        if len(kinds) == 1:
            return kinds[0]
        return "mixed"

    def field_for(self, path: str) -> ResolvedModelField:
        """Return one typed field from this role."""
        for field in self.fields:
            if field.path == path:
                return field
        raise KeyError(path)


@dataclass(frozen=True)
class ResolvedModelStack:
    """The three-role effective model configuration."""

    roles: tuple[ResolvedModelTarget, ...]

    def for_role(self, role: str | ModelRole) -> ResolvedModelTarget:
        resolved = parse_model_role(role)
        for target in self.roles:
            if target.role is resolved:
                return target
        raise KeyError(resolved)  # pragma: no cover - catalog invariant


def _value_at(settings: Any, dotted_path: str | None, default: Any = "") -> Any:
    if not dotted_path:
        return default
    value: Any = settings
    for part in dotted_path.split("."):
        value = getattr(value, part)
    return value


def resolve_model_stack(resolution: SettingsResolution) -> ResolvedModelStack:
    """Map effective settings and their sources into the typed three-role stack."""
    settings = resolution.settings
    targets: list[ResolvedModelTarget] = []
    for spec in MODEL_ROLE_SPECS:
        raw_endpoint = str(_value_at(settings, spec.endpoint_path, "") or "").strip()
        display_endpoint = safe_endpoint_display(raw_endpoint)
        fields = []
        for path in spec.field_paths:
            sensitive = path == spec.credential_path
            value = _value_at(settings, path)
            if path == spec.endpoint_path:
                value = display_endpoint
            fields.append(
                ResolvedModelField(
                    path=path,
                    value=bool(value) if sensitive else value,
                    source=resolution.source_for(path),
                    sensitive=sensitive,
                )
            )
        targets.append(
            ResolvedModelTarget(
                role=spec.role,
                title=spec.title,
                enabled=bool(_value_at(settings, spec.enabled_path, True)),
                provider=str(_value_at(settings, spec.provider_path, "") or ""),
                endpoint=display_endpoint,
                endpoint_redacted=display_endpoint != raw_endpoint,
                model=str(_value_at(settings, spec.model_path, "") or ""),
                protocol=str(_value_at(settings, spec.protocol_path, "") or ""),
                credential_configured=bool(
                    _value_at(settings, spec.credential_path, False)
                ),
                fields=tuple(fields),
            )
        )
    return ResolvedModelStack(tuple(targets))
