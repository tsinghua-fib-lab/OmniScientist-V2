"""SKILL.md parsing.

Format (Claude Code compatible + OmniScientist/HelixForge extensions):

    ---
    name: example-skill
    description: ...                      # required by Claude Code
    allowed-tools: [web_fetch]            # Claude Code field
    dependencies: ["python>=3.10"]
    metadata:
      helixforge:                         # ignored by Claude Code
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
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# A declared tool budget is only reachable if the loop is given enough turns to
# spend it. Assuming a modest two tool calls per iteration, an iteration ceiling
# below ``max_tool_calls / 2`` means the skill can never use the budget it
# declares — it will always stop on iterations first. That mismatch is an
# authoring defect, not a runtime condition, so it is reported at parse time.
_ASSUMED_TOOL_CALLS_PER_ITERATION = 2

# Tools whose call *is* the deliverable. A quota on one of these bounds nothing:
# the cost was already paid by the work that produced the content, so the cap
# only truncates the output, and the model cannot comply except by delivering
# less than it was asked for. Acquisition and execution tools — search, fetch,
# shell — are what a quota is for. Neither Codex, OpenClaw nor OpenCode caps
# invocations of a named tool at all; they bound context and detect repetition.
DELIVERABLE_TOOLS = frozenset(
    {
        "add_evidence",
        "attach_provenance",
        "cite_source",
        "log_run",
        "package_artifact",
        "record_claim",
        "write_file",
    }
)


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


@dataclass(frozen=True)
class LocalToolSpec:
    """A Skill-private Python tool exposed only to its prompt sub-agent."""

    name: str
    description: str
    module: str
    class_name: str
    method: str = "execute"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


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
    # Optional provider-owned component vocabulary. The host preserves it in
    # authority snapshots but never uses it as a central semantic plan gate.
    template_signatures: dict[str, list[str]] = field(default_factory=dict)
    engine: EngineSpec | None = None
    exec_spec: ExecSpec | None = None
    local_tools: list[LocalToolSpec] = field(default_factory=list)
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
    # Importable Python modules the engine needs at run time (SKILL.md
    # ``runtime_requirements.python_modules``), e.g. ``["pptx"]``. Preflighted with
    # ``importlib.util.find_spec`` against omni's *own* interpreter so a declared
    # dependency that is genuinely missing yields one actionable install prompt
    # (``dependency_setup_command``) instead of a mid-engine ImportError or the
    # model hand-rolling ``pip install`` across the wrong interpreter/venv. Fully
    # generic: a skill declares its modules and the same host gate enforces them,
    # so no per-format host code is ever added.
    requires_python_modules: list[str] = field(default_factory=list)
    # One-shot command surfaced to the owner when a declared python module (or
    # bin) is missing. Kept declarative so the host never invents an installer.
    dependency_setup_command: str = ""
    # Domain error code reported when a declared dependency is missing.
    dependency_error_code: str = "runtime_dependency_missing"
    # Artifact content types this skill can *read/inspect* (SKILL.md
    # ``runtime_requirements.reads``: ``extensions`` and/or ``mime``). This is what
    # lets ``open_artifact`` route a binary artifact to the owning capability by
    # declaration instead of the host hard-coding a per-format reader — add a new
    # format by declaring it on a skill, never by editing host code.
    reads_extensions: list[str] = field(default_factory=list)
    reads_mime: list[str] = field(default_factory=list)
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


def execution_budget_warnings(execution: Any, name: str = "") -> list[str]:
    """Return authoring defects in a skill's declared execution budget."""
    if not isinstance(execution, dict):
        return []
    try:
        iterations = int(execution.get("max_iterations") or 0)
        tool_calls = int(execution.get("max_tool_calls") or 0)
        max_seconds = float(execution.get("max_seconds") or 0)
        stall_seconds = float(execution.get("stall_seconds") or 0)
    except (TypeError, ValueError):
        return [f"skill '{name}' declares a non-numeric execution budget"]
    warnings: list[str] = []
    reachable = iterations * _ASSUMED_TOOL_CALLS_PER_ITERATION
    if iterations > 0 and tool_calls > 0 and reachable < tool_calls:
        warnings.append(
            f"skill '{name}' declares max_tool_calls={tool_calls} but only "
            f"max_iterations={iterations}; the run will stop on iterations long "
            f"before that tool budget is reachable. Raise max_iterations to at "
            f"least {-(-tool_calls // _ASSUMED_TOOL_CALLS_PER_ITERATION)} or lower "
            f"max_tool_calls."
        )
    if stall_seconds > 0 and 0 < max_seconds <= stall_seconds:
        warnings.append(
            f"skill '{name}' declares stall_seconds={stall_seconds:g} but only "
            f"max_seconds={max_seconds:g}; the deadline arrives before the progress "
            f"watchdog can fire, so a stuck run is reported as a slow one. Put "
            f"stall_seconds well under max_seconds — it is the primary guard, and "
            f"max_seconds only the runaway backstop."
        )
    return warnings


def tool_limit_warnings(limits: Any, name: str = "") -> list[str]:
    """Return authoring defects in a skill's per-tool quotas."""
    if not isinstance(limits, dict):
        return []
    return [
        f"skill '{name}' caps its own deliverable with tool_limits.{tool}="
        f"{limits[tool]}; the number of outputs is decided by the content, so "
        f"this truncates the result instead of bounding the work. Cap the "
        f"acquisition tools it uses instead."
        for tool in sorted(set(limits) & DELIVERABLE_TOOLS)
    ]


def _execution_contract(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    for warning in execution_budget_warnings(value, name):
        logger.warning("[skills] %s", warning)
    limits = value.get("tool_limits")
    warnings = tool_limit_warnings(limits, name)
    if not warnings:
        return value
    for warning in warnings:
        logger.warning("[skills] %s", warning)
    # Drop the offending caps rather than honouring them: a third-party skill
    # must not be able to truncate its own output through a manifest typo.
    return {
        **value,
        "tool_limits": {
            tool: cap for tool, cap in limits.items() if tool not in DELIVERABLE_TOOLS
        },
    }


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


def _coerce_local_tools(value: Any) -> list[LocalToolSpec]:
    if not isinstance(value, list):
        return []
    tools: list[LocalToolSpec] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        module = str(raw.get("module") or "").strip()
        class_name = str(raw.get("class") or raw.get("class_name") or "").strip()
        if not name or not module or not class_name:
            continue
        schema = raw.get("input_schema")
        tools.append(
            LocalToolSpec(
                name=name,
                description=str(raw.get("description") or name).strip(),
                module=module,
                class_name=class_name,
                method=str(raw.get("method") or "execute").strip(),
                input_schema=(
                    schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
                ),
            )
        )
    return tools


def _reads_block(runtime_reqs: dict[str, Any]) -> dict[str, Any]:
    block = runtime_reqs.get("reads")
    return block if isinstance(block, dict) else {}


def _normalize_extension(value: Any) -> str:
    """Normalize a declared reader extension to a lowercase ``.ext`` form."""
    ext = str(value or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    return ext


def python_module_available(name: str) -> bool:
    """True if ``name`` is importable / installed in omni's own interpreter.

    Bridges the distribution/import spelling gap (``python-pptx`` ↔ ``pptx``) by
    consulting distribution metadata *and* ``find_spec`` across hyphen/underscore
    variants. This is the single availability oracle shared by the admission gate
    (``missing_python_modules``), ``open_artifact``, and the shell install guard,
    so a skill that declares a *distribution* name is never fail-closed at
    admission while the same install is intercepted elsewhere as "already there".
    """
    import importlib.metadata
    import importlib.util

    token = str(name).strip()
    if not token:
        return False
    for variant in {token, token.replace("-", "_"), token.replace("_", "-")}:
        try:
            importlib.metadata.version(variant)
            return True
        except importlib.metadata.PackageNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — metadata backends vary; treat as "unknown"
            pass
    for variant in {token, token.replace("-", "_")}:
        try:
            if importlib.util.find_spec(variant) is not None:
                return True
        except (ImportError, ValueError):
            pass
    return False


def missing_python_modules(entry: SkillEntry) -> list[str]:
    """Declared Python modules that are NOT importable in omni's own interpreter.

    Shared by the executor's admission gate and ``open_artifact`` so dependency
    detection is one implementation (:func:`python_module_available`) against the
    running interpreter — the same environment engines import from — and tolerant
    of the distribution/import spelling gap so a declared ``python-pptx`` resolves
    to the installed ``pptx``.
    """
    missing: list[str] = []
    for module in entry.requires_python_modules:
        name = str(module).strip()
        if not name:
            continue
        if not python_module_available(name):
            missing.append(name)
    return missing


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
        execution=_execution_contract(hf.get("execution"), name),
        workflow=hf.get("workflow") if isinstance(hf.get("workflow"), dict) else {},
        artifact_revision=hf.get("artifact_revision")
        if isinstance(hf.get("artifact_revision"), dict)
        else {},
        template_signatures=_coerce_signatures(hf.get("template_signatures")),
        engine=engine,
        exec_spec=exec_spec,
        local_tools=_coerce_local_tools(hf.get("local_tools")),
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
        requires_python_modules=_coerce_list(runtime_reqs.get("python_modules")),
        dependency_setup_command=str(runtime_reqs.get("dependency_setup_command") or ""),
        dependency_error_code=str(
            runtime_reqs.get("dependency_error_code") or "runtime_dependency_missing"
        ),
        reads_extensions=[
            _normalize_extension(item) for item in _coerce_list(_reads_block(runtime_reqs).get("extensions"))
        ],
        reads_mime=[str(item).strip().lower() for item in _coerce_list(_reads_block(runtime_reqs).get("mime"))],
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
