"""SKILL.md parsing.

Format (Claude Code compatible + OmniScientist/HelixForge extensions):

    ---
    name: example-skill
    description: ...                      # required by Claude Code
    allowed-tools: [web_fetch]            # Claude Code field
    dependencies: ["python>=3.10"]
    metadata:
      helixforge:                         # ignored by Claude Code
        tier: research
        delivery_mode: async_task         # sync_tool | async_task
        kind: python_engine               # prompt_only|python_engine|cli_exec|remote_mcp
        engine: {module: ..., class: ..., method: execute}
        exec:  {command: python, args: [...], stdout_format: json, timeout_seconds: 120}
        input_schema: {...}               # JSON schema for sync tools
        trigger: {phrases: [...], when_to_use: ...}
        notification: {display_label: ..., title_field: ...}
      openclaw:
        requires: {bins: [...], env: [...]}
    ---
    # markdown body (instructions)

The same file is a valid Claude Code skill (only ``name`` + ``description``
are required; everything under ``metadata:`` is ignored by Claude Code).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class SkillKind(StrEnum):
    PROMPT_ONLY = "prompt_only"
    PYTHON_ENGINE = "python_engine"
    CLI_EXEC = "cli_exec"
    REMOTE_MCP = "remote_mcp"


class DeliveryMode(StrEnum):
    SYNC_TOOL = "sync_tool"
    ASYNC_TASK = "async_task"


@dataclass(frozen=True)
class EngineSpec:
    module: str
    class_name: str
    method: str = "execute"


@dataclass(frozen=True)
class ExecSpec:
    command: str
    args: list[str] = field(default_factory=list)
    stdout_format: str = "json"  # json | text
    timeout_seconds: int = 120
    cwd: str = ""
    env_allowlist: list[str] = field(default_factory=list)


@dataclass
class SkillEntry:
    name: str
    description: str
    source: str = "builtin"  # builtin|user_omni|user_claude|project_omni|project_claude|mcp
    path: Path | None = None
    body: str = ""  # empty for on-disk skills; loaded lazily via load_body()
    sha256: str = ""
    kind: SkillKind = SkillKind.PROMPT_ONLY
    delivery_mode: DeliveryMode = DeliveryMode.SYNC_TOOL
    tier: str = "agent"
    version: str = ""
    license: str = ""
    status: str = "stable"  # stable | deprecated | disabled
    replaced_by: str = ""
    role: str = ""  # task | support | utility (empty means infer)
    capabilities: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    priority: int = 0
    default_for: list[str] = field(default_factory=list)
    # Codex-style governance (``policy.allow_implicit_invocation``): when False the
    # skill is installed + trusted but hidden from automatic selection (planner
    # catalog / find_skill / capability resolution). It stays runnable via an
    # explicit ``$name`` / ``$<scope>:name`` escape.
    allow_implicit: bool = True
    allowed_tools: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    execution: dict[str, Any] = field(default_factory=dict)
    workflow: dict[str, Any] = field(default_factory=dict)
    artifact_revision: dict[str, Any] = field(default_factory=dict)
    # Provider-owned deliverable acceptance contract. The host only aggregates
    # declared check ids and assessment envelopes; domain judgement stays in
    # the provider.
    quality_contract: Any = field(default_factory=dict)
    quality_contract_declared: bool | None = None
    # Optional provider-owned component vocabulary. The host preserves it in
    # authority snapshots but never uses it as a central semantic plan gate.
    template_signatures: dict[str, list[str]] = field(default_factory=dict)
    engine: EngineSpec | None = None
    exec_spec: ExecSpec | None = None
    input_schema: Any = field(default_factory=lambda: {"type": "object", "properties": {}})
    output_schema: Any = field(default_factory=lambda: {"type": "object", "properties": {}})
    # ``None`` preserves compatibility for synthetic entries: infer whether the
    # non-placeholder schema was explicitly supplied. Parsed manifests set the
    # flags exactly, which distinguishes an absent schema from valid ``{}``,
    # boolean, composed, and empty-object schemas.
    input_schema_declared: bool | None = None
    output_schema_declared: bool | None = None
    trigger: dict[str, Any] = field(default_factory=dict)
    notification: dict[str, Any] = field(default_factory=dict)
    requires_bins: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    # Host services the engine needs at run time (SKILL.md
    # ``runtime_requirements.services``), e.g. ``["vlm"]``. Checked by a
    # pre-execution availability gate so a structurally-unsatisfiable run asks
    # the owner to configure the service instead of failing mid-engine.
    requires_services: list[str] = field(default_factory=list)
    when_to_use: str = ""
    trusted: bool = True
    origin: str = ""
    _body_cache: str | None = field(default=None, compare=False, repr=False)

    def load_body(self) -> str:
        """Return the skill's markdown instruction body.

        The body is *not* held in memory for on-disk skills after indexing (only
        the lightweight frontmatter metadata is). It is read from ``SKILL.md`` on
        execution. Synthetic skills that were parsed from raw text (no ``path``)
        keep their inline :attr:`body`.
        """
        if self.body:
            return self.body
        body = ""
        if self.path is not None:
            body_file = self.path / "SKILL.md" if self.path.is_dir() else self.path
            try:
                _, body = _split_frontmatter(body_file.read_text(encoding="utf-8"))
            except OSError:
                body = ""
        # On-disk skills are mutable across local updates. Re-read them at each
        # execution so a long-lived daemon never pairs a freshly verified file
        # authority with stale instructions cached before the update.
        self._body_cache = body
        return body

    @property
    def is_async(self) -> bool:
        return self.delivery_mode == DeliveryMode.ASYNC_TASK

    @property
    def is_deprecated(self) -> bool:
        return self.status.lower() == "deprecated"

    @property
    def is_disabled(self) -> bool:
        return self.status.lower() == "disabled"

    @property
    def replay_safe(self) -> bool:
        """Whether the owner-authored execution contract permits automatic replay."""
        return (self.execution or {}).get("replay_safe") is True

    @property
    def contract_level(self) -> str:
        if not self.trusted:
            return "none"
        has_input = _has_declared_schema(
            self.input_schema,
            declared=self.input_schema_declared,
        )
        has_output = _has_declared_schema(
            self.output_schema,
            declared=self.output_schema_declared,
        )
        if self.source in {"builtin", "project_omni", "user_omni"} and has_input and has_output:
            return "full"
        if has_input or has_output:
            return "partial"
        return "none"

    @property
    def skill_role(self) -> str:
        role = (self.role or "").strip().lower()
        if role in {"task", "support", "utility", "unknown"}:
            return role
        return "unknown"

    def short_desc(self, limit: int = 200) -> str:
        d = (self.description or "").strip().replace("\n", " ")
        return d[: limit - 1] + "…" if len(d) > limit else d


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off"}
    return bool(value)


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p for p in value.replace(",", " ").split() if p]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _coerce_signatures(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): [token for token in (str(v).strip().lower() for v in raw) if token]
        for key, raw in value.items()
        if isinstance(raw, list)
    }


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    body = parts[2].lstrip("\n")
    return (meta if isinstance(meta, dict) else {}), body


def parse_skill_text(
    text: str, *, default_name: str, source: str = "builtin", path: Path | None = None
) -> SkillEntry:
    meta, body = _split_frontmatter(text)
    name = str(meta.get("name") or default_name)
    description = str(meta.get("description") or "").strip()
    hf = ((meta.get("metadata") or {}).get("helixforge")) or {}
    oc = ((meta.get("metadata") or {}).get("openclaw")) or {}

    engine = None
    if isinstance(hf.get("engine"), dict):
        e = hf["engine"]
        engine = EngineSpec(
            module=str(e.get("module", "")),
            class_name=str(e.get("class", e.get("class_name", ""))),
            method=str(e.get("method", "execute")),
        )

    exec_spec = None
    if isinstance(hf.get("exec"), dict):
        x = hf["exec"]
        exec_spec = ExecSpec(
            command=str(x.get("command", "")),
            args=_coerce_list(x.get("args")),
            stdout_format=str(x.get("stdout_format", "json")),
            timeout_seconds=int(x.get("timeout_seconds", 120)),
            cwd=str(x.get("cwd", "")),
            env_allowlist=_coerce_list(x.get("env_allowlist")),
        )

    kind_raw = str(hf.get("kind", "")).lower()
    if kind_raw in {k.value for k in SkillKind}:
        kind = SkillKind(kind_raw)
    elif engine is not None:
        kind = SkillKind.PYTHON_ENGINE
    elif exec_spec is not None:
        kind = SkillKind.CLI_EXEC
    else:
        kind = SkillKind.PROMPT_ONLY

    delivery_raw = str(hf.get("delivery_mode", "")).lower()
    delivery = (
        DeliveryMode(delivery_raw)
        if delivery_raw in {d.value for d in DeliveryMode}
        else DeliveryMode.SYNC_TOOL
    )

    requires = (oc.get("requires") or {}) if isinstance(oc.get("requires"), dict) else {}
    runtime_reqs = (
        hf.get("runtime_requirements") if isinstance(hf.get("runtime_requirements"), dict) else {}
    )
    trigger = hf.get("trigger") or {}
    policy = hf.get("policy") if isinstance(hf.get("policy"), dict) else {}
    allow_implicit = _coerce_bool(
        hf.get(
            "allow_implicit_invocation",
            hf.get("allow_implicit", policy.get("allow_implicit_invocation")),
        ),
        default=True,
    )

    trust = _trust_metadata(path, source)
    return SkillEntry(
        name=name,
        description=description,
        source=source,
        path=path,
        # On-disk skills load their body lazily via ``load_body()``; only inline
        # (pathless) parses keep the body so nothing is lost when there is no file.
        body="" if path is not None else body,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        kind=kind,
        delivery_mode=delivery,
        tier=str(hf.get("tier", "agent")),
        version=str(hf.get("version") or meta.get("version", "")),
        license=str(meta.get("license", "")),
        status=str(hf.get("status", "stable")).lower(),
        replaced_by=str(hf.get("replaced_by", "")),
        role=str(hf.get("role", "")).lower(),
        capabilities=_coerce_list(hf.get("capabilities")),
        deliverables=_coerce_list(hf.get("deliverables")),
        priority=int(hf.get("priority", 0) or 0),
        default_for=_coerce_list(hf.get("default_for")),
        allow_implicit=allow_implicit,
        allowed_tools=_coerce_list(meta.get("allowed-tools") or hf.get("allowed_tools")),
        dependencies=_coerce_list(hf.get("dependencies") or meta.get("dependencies")),
        execution=hf.get("execution") if isinstance(hf.get("execution"), dict) else {},
        workflow=hf.get("workflow") if isinstance(hf.get("workflow"), dict) else {},
        artifact_revision=hf.get("artifact_revision")
        if isinstance(hf.get("artifact_revision"), dict)
        else {},
        quality_contract=(hf.get("quality_contract") if "quality_contract" in hf else {}),
        quality_contract_declared="quality_contract" in hf,
        template_signatures=_coerce_signatures(hf.get("template_signatures")),
        engine=engine,
        exec_spec=exec_spec,
        input_schema=(
            hf.get("input_schema") if "input_schema" in hf else {"type": "object", "properties": {}}
        ),
        output_schema=(
            hf.get("output_schema")
            if "output_schema" in hf
            else {"type": "object", "properties": {}}
        ),
        input_schema_declared="input_schema" in hf,
        output_schema_declared="output_schema" in hf,
        trigger=trigger if isinstance(trigger, dict) else {},
        notification=hf.get("notification") if isinstance(hf.get("notification"), dict) else {},
        requires_bins=_coerce_list(requires.get("bins")),
        requires_env=_coerce_list(requires.get("env")),
        requires_services=_coerce_list(runtime_reqs.get("services")),
        when_to_use=str(trigger.get("when_to_use", "")) if isinstance(trigger, dict) else "",
        trusted=trust[0],
        origin=trust[1],
    )


def parse_skill_path(path: Path, *, source: str = "builtin") -> SkillEntry:
    """Parse a directory skill (``<dir>/SKILL.md``) or a single ``<name>.md``."""
    if path.is_dir():
        body_path = path / "SKILL.md"
        default_name = path.name
    else:
        body_path = path
        default_name = path.stem
    text = body_path.read_text(encoding="utf-8")
    return parse_skill_text(text, default_name=default_name, source=source, path=path)


def _has_declared_schema(
    schema: Any,
    *,
    declared: bool | None = None,
) -> bool:
    if declared is not None:
        return declared
    if isinstance(schema, bool):
        return True
    if not isinstance(schema, dict):
        return False
    # These values are historical placeholders used by synthetic/legacy
    # entries for a missing manifest field. Parsed entries carry an explicit
    # presence bit, so a provider that intentionally declares either valid
    # schema is preserved.
    return schema not in ({}, {"type": "object", "properties": {}})


def validate_quality_contract_definition(
    value: Any,
    *,
    declared: bool | None = None,
) -> tuple[dict[str, str], ...]:
    """Return provider-owned quality contract definition errors.

    The check is deliberately structural and domain-neutral. Both the typed
    planner gate and direct workflow ingress call it before execution.
    """
    if declared is False or (declared is None and value == {}):
        return ()
    errors: list[dict[str, str]] = []
    if not isinstance(value, dict):
        return (
            {
                "path": "",
                "message": "quality_contract must be an object",
            },
        )
    checks = value.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(not isinstance(item, str) or not item.strip() for item in checks)
    ):
        errors.append(
            {
                "path": "checks",
                "message": (
                    "quality_contract.checks must be a non-empty array of non-empty strings"
                ),
            }
        )
    if "assessment_required" in value and not isinstance(value["assessment_required"], bool):
        errors.append(
            {
                "path": "assessment_required",
                "message": "quality_contract.assessment_required must be a boolean",
            }
        )
    if "assessment_schema" in value and (
        not isinstance(value["assessment_schema"], str) or not value["assessment_schema"].strip()
    ):
        errors.append(
            {
                "path": "assessment_schema",
                "message": "quality_contract.assessment_schema must be a non-empty string",
            }
        )
    if "retry" in value and not isinstance(value["retry"], dict):
        errors.append(
            {
                "path": "retry",
                "message": "quality_contract.retry must be an object",
            }
        )
    retry = value.get("retry") if isinstance(value.get("retry"), dict) else {}
    if "max_attempts" in retry:
        attempts = retry["max_attempts"]
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            errors.append(
                {
                    "path": "retry/max_attempts",
                    "message": (
                        "quality_contract.retry.max_attempts must be a non-negative integer"
                    ),
                }
            )
    if "provider_replay_safe_required" in retry and not isinstance(
        retry["provider_replay_safe_required"], bool
    ):
        errors.append(
            {
                "path": "retry/provider_replay_safe_required",
                "message": (
                    "quality_contract.retry.provider_replay_safe_required must be a boolean"
                ),
            }
        )
    for field_name in (
        "side_effect_policy",
        "idempotency_key_field",
        "feedback_field",
    ):
        if field_name in retry and (
            not isinstance(retry[field_name], str) or not retry[field_name].strip()
        ):
            errors.append(
                {
                    "path": f"retry/{field_name}",
                    "message": (f"quality_contract.retry.{field_name} must be a non-empty string"),
                }
            )
    return tuple(errors)


def _trust_metadata(path: Path | None, source: str) -> tuple[bool, str]:
    """Return owner trust separately from a skill's self-declared contract."""
    if source in {"builtin", "project_omni"}:
        return True, source
    if path is None:
        # In-memory parsing validates a proposal only; no code can execute from it.
        return source == "user_omni", source
    if source != "user_omni":
        return False, source
    root = path if path.is_dir() else path.parent
    marker = root / ".omni-skill.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "untracked user skill"
    return bool(data.get("trusted", False)), str(data.get("source") or "imported skill")
