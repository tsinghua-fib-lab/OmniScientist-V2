"""Skill discovery & indexing across multiple roots.

Full source order (priority high→low):

    builtin → project_omni → project_claude → project_agents →
    user_omni → user_claude → user_agents → user_codex → user_openclaw

Packaged built-ins have the **highest** priority: they ship and update with the
program (via the wheel; ``omni update`` swaps them), so a same-named user or
external skill never silently overrides one. Use ``$user:<name>`` or
``$<source>:<name>`` to force a shadowed skill by name.

By **default** OmniScientist only indexes the skills it *manages* (``builtin``
and ``user_omni`` = ``~/.omni/skills`` where ``omni skills add`` imports — see
``settings.skills.sources``). The in-repo ``project_omni`` (``.omni/skills``)
and the Claude Code / Codex / OpenClaw on-disk libraries are opt-in
(``omni skills list --all`` or :data:`ALL_SOURCES`) and can be run directly when
selected — so the CLI can still import skills authored for any of those tools,
without drowning the catalog with a user's whole personal library.

Same-named skills are overridden by the higher-priority source. The project
sources walk up from the CWD to the repo root (like Claude Code / Codex).
Discovery is recursive so grouped/nested ``SKILL.md`` layouts (Codex/OpenClaw)
are picked up too.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omni.config.paths import OmniPaths, iter_project_skill_dirs
from omni.config.settings import OmniSettings
from omni.data import BUILTIN_SKILLS_DIR, SYSTEM_SKILLS_DIR
from omni.skills_runtime.discovery import (
    SkillIndexError,
    indexed_skill_dirs,
    iter_skill_paths,
    skill_dirs_in,
)
from omni.skills_runtime.manifest import DeliveryMode, SkillEntry, parse_skill_path

logger = logging.getLogger(__name__)

SKILL_SOURCE_PARAM = "_skill_source"

# Every discovery source, in priority order (high→low). ``omni skills list --all``
# indexes this full set; the default (``settings.skills.sources``) is a subset.
ALL_SOURCES: tuple[str, ...] = (
    "builtin",
    "project_omni",
    "project_claude",
    "project_agents",
    "user_omni",
    "user_claude",
    "user_agents",
    "user_codex",
    "user_openclaw",
)

# The external (other-tool) libraries that are opt-in, not indexed by default.
EXTERNAL_SOURCES: frozenset[str] = frozenset(
    {"project_claude", "project_agents", "user_claude", "user_agents", "user_codex", "user_openclaw"}
)

_CORE_CAPABILITY_SOURCES = frozenset({"project_omni", "user_omni", "builtin"})

# Built-ins rank highest so a same-named user/external skill can never win an
# automatic capability tie-break (the ``$user:`` / ``$<source>:`` escape is the
# only way to reach a shadowed skill). Kept in sync with :data:`ALL_SOURCES`
# order and ``settings.skills.sources`` (both put ``builtin`` first).
_SOURCE_RANK: dict[str, int] = {
    "builtin": 90,
    "project_omni": 80,
    "user_omni": 70,
    "project_claude": 40,
    "project_agents": 40,
    "user_claude": 30,
    "user_agents": 30,
    "user_codex": 30,
    "user_openclaw": 30,
    "mcp": 20,
}

_CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "artifact.figure": ("artifact.figure", "figure.scientific", "figure.architecture", "figure.workflow"),
    "figure.editable.pptx": (
        "figure.editable.pptx",
        "figure.livefigure",
        "figure.editable",
        "artifact.pptx",
    ),
    "slides.generate": ("slides.generate", "artifact.slides"),
    "artifact.slides": ("artifact.slides", "slides.generate"),
    "literature.search": ("literature.search", "literature.review", "research.literature_search"),
    "corpus.index": ("corpus.index", "literature.index", "research.corpus_index"),
    "qa.grounded": ("qa.grounded", "literature.qa", "research.grounded_qa"),
    "synthesis.final": ("synthesis.final", "draft.section", "draft.manuscript"),
    "draft.section": ("draft.section", "synthesis.final", "paper.write.section"),
    "draft.manuscript": ("draft.manuscript", "synthesis.final", "paper.write"),
    "analysis.paper": ("analysis.paper", "paper.analysis", "research.paper_analysis"),
    "review.paper": ("review.paper", "paper.review", "research.paper_review"),
    "review.response": ("review.response", "reviewer.response", "writing.revision"),
    "poster.scientific": ("poster.scientific", "poster.html_preview", "poster.element_feedback"),
    "research.ideation": ("research.ideation", "research.brainstorm", "idea.research"),
    "evidence.contradiction_scan": (
        "evidence.contradiction_scan",
        "contradiction.scan",
        "research.contradiction_scan",
    ),
    "paper.fetch.arxiv": ("paper.fetch.arxiv", "arxiv.fetch", "research.arxiv_fetch"),
}

def capability_aliases(capability: str) -> tuple[str, ...]:
    """Public view of a capability's alias set (for tooling / ``skills why``)."""
    key = (capability or "").strip().lower()
    return _CAPABILITY_ALIASES.get(key, (key,))


def step_skill_source(step: dict[str, Any]) -> str:
    """Return the explicit discovery source carried by one workflow step."""
    input_data = step.get("input")
    return str(
        step.get("skill_source")
        or (
            input_data.get(SKILL_SOURCE_PARAM)
            if isinstance(input_data, dict)
            else ""
        )
        or ""
    )


def resolve_step_entry(
    registry: SkillRegistry,
    step: dict[str, Any],
) -> SkillEntry | None:
    """Resolve the exact provider source sealed on one workflow step.

    A source-qualified step is an authority-bearing reference. If that source
    disappeared, fail closed instead of silently validating against the
    same-named catalog winner that execution will not dispatch.
    """
    name = str(step.get("skill_name") or step.get("skill") or "")
    source = step_skill_source(step)
    if source:
        getter = getattr(registry, "get_scoped", None)
        return getter(source, name) if callable(getter) else None
    getter = getattr(registry, "get", None)
    return getter(name) if callable(getter) else None


# Friendly ``$<scope>:<name>`` prefixes → the concrete source(s) they force.
# A bare exact source name (e.g. ``$user_omni:foo``) is also accepted.
_SCOPE_ALIASES: dict[str, tuple[str, ...]] = {
    "builtin": ("builtin",),
    "user": ("user_omni",),
    "project": ("project_omni",),
    "claude": ("user_claude", "project_claude"),
    "codex": ("user_codex", "user_agents", "project_agents"),
    "openclaw": ("user_openclaw",),
    "agents": ("user_agents", "project_agents"),
}


def _name_variants(name: str) -> set[str]:
    """kebab/underscore-insensitive candidates for a skill name."""
    base = (name or "").strip().lower()
    return {base, base.replace("-", "_"), base.replace("_", "-")}


def _normalize_module_token(name: str) -> str:
    """Canonical form for comparing a package/module token across the
    distribution/import spelling gap (``Python-PPTX`` → ``python_pptx``)."""
    return (name or "").strip().lower().replace("-", "_")


def scope_sources(scope: str) -> tuple[str, ...] | None:
    """Resolve a ``$<scope>:`` prefix to candidate sources, or ``None`` if the
    prefix is not a recognised scope (so it is treated as part of the name)."""
    key = (scope or "").strip().lower()
    if key in _SCOPE_ALIASES:
        return _SCOPE_ALIASES[key]
    if key in ALL_SOURCES:
        return (key,)
    return None


@dataclass(frozen=True)
class _CapabilityCandidate:
    entry: SkillEntry
    score: float
    reason: str
    tier: int

    @property
    def sort_key(self) -> tuple[int, float, str]:
        return (_SOURCE_RANK.get(self.entry.source, 0), self.score, self.entry.name)


def is_selectable_skill(entry: SkillEntry) -> bool:
    """Whether a skill participates in *automatic* matching/planning.

    ``allow_implicit`` is the Codex ``allow_implicit_invocation`` primitive: a
    skill can be installed, trusted, and runnable while staying out of the model
    catalog / find_skill / capability resolution. Such a skill is only reachable
    through an explicit ``$name`` / ``$<scope>:name`` escape (which goes through
    :meth:`SkillRegistry.resolve_explicit`, not this gate)."""
    return (
        entry.trusted
        and entry.allow_implicit
        and not entry.is_disabled
        and not entry.is_deprecated
    )


def _compact_output_contract(entry: SkillEntry) -> str:
    """Small output hint for model-side workflow planning.

    The full JSON schema is kept in ``SkillEntry.output_schema`` for tools and
    docs. The prompt only needs the stable fields a downstream step can rely on.
    """
    schema = entry.output_schema if isinstance(entry.output_schema, dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if not props:
        return ""
    preferred = [
        "status",
        "outcome",
        "summary",
        "warning",
        "artifacts",
        "sources",
        "research",
        "error",
    ]
    fields = [name for name in preferred if name in props]
    extras = [name for name in props if name not in preferred][:3]
    names = fields + extras
    if not names:
        return ""
    status = props.get("status")
    status_hint = ""
    if isinstance(status, dict) and isinstance(status.get("enum"), list):
        status_hint = "=" + "|".join(str(item) for item in status["enum"][:4])
    return "; outputs: " + ", ".join([f"status{status_hint}" if n == "status" else n for n in names])


def _source_dirs(source: str, paths: OmniPaths, cwd: Path | None = None) -> list[Path]:
    """Resolve the on-disk root(s) for a discovery source (may be several)."""
    if source == "project_omni":
        return [paths.project_skills_dir, *iter_project_skill_dirs(cwd, ".omni/skills")]
    if source == "project_claude":
        return iter_project_skill_dirs(cwd, ".claude/skills")
    if source == "project_agents":
        return iter_project_skill_dirs(cwd, ".agents/skills")
    return {
        "user_omni": [paths.user_skills_dir],
        "user_claude": [paths.claude_user_skills],
        "user_agents": [paths.agents_user_skills],
        "user_codex": [paths.codex_user_skills],
        "user_openclaw": [paths.openclaw_user_skills],
        "builtin": [BUILTIN_SKILLS_DIR],
    }.get(source, [])


class SkillRegistry:
    def __init__(
        self,
        settings: OmniSettings,
        *,
        cwd: Path | None = None,
        sources: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self._settings = settings
        self._paths = settings.paths
        self._cwd = cwd or Path.cwd()
        # ``sources`` overrides the configured default (used by ``--all``).
        self._uses_config_sources = sources is None
        self._sources = list(sources) if sources is not None else list(settings.skills.sources)
        self._entries: dict[str, SkillEntry] = {}
        self._generation = 0
        # Every parsed (source, name) → entry, including entries shadowed by a
        # higher-priority same-named skill. Powers ``$<source>:<name>`` escapes
        # and the "shadowed by" observability surface.
        self._by_source_name: dict[tuple[str, str], SkillEntry] = {}
        self._shadowed: list[SkillEntry] = []
        # Internal system skills (e.g. ``agent-goal``): resolvable by name /
        # ``$name`` but intentionally *outside* ``_entries`` so they never appear
        # in the product catalog (``list_all``/``list_selectable``) or automatic
        # selection. Populated from the packaged system-skills dir on every build.
        self._system_entries: dict[str, SkillEntry] = {}

    def refresh_settings(self, settings: OmniSettings) -> int:
        """Reload settings-backed filters while preserving this registry object."""
        self._settings = settings
        self._paths = settings.paths
        if self._uses_config_sources:
            self._sources = list(settings.skills.sources)
        return self.build_index()

    def build_index(self) -> int:
        self._entries = {}
        self._by_source_name = {}
        self._shadowed = []
        self._index_system_skills()
        disabled = set(self._settings.skills.disabled)
        seen_paths: set[Path] = set()
        for source in self._sources:
            for root in _source_dirs(source, self._paths, self._cwd):
                source_paths = indexed_skill_dirs(root) if source == "builtin" else iter_skill_paths(root)
                for path in source_paths:
                    resolved = path.resolve()
                    if resolved in seen_paths:
                        continue
                    seen_paths.add(resolved)
                    try:
                        entry = parse_skill_path(path, source=source)
                    except Exception as exc:  # noqa: BLE001
                        if source == "builtin":
                            raise SkillIndexError(f"invalid active built-in skill {path.name}: {exc}") from exc
                        logger.warning("skipping invalid skill %s: %s", path, exc)
                        continue
                    if source == "builtin" and entry.name != path.name:
                        raise SkillIndexError(
                            f"active built-in directory '{path.name}' declares name '{entry.name}'"
                        )
                    if entry.name in disabled:
                        continue
                    # Retain every parsed entry keyed by (source, name) so an
                    # explicit ``$<source>:<name>`` can reach a shadowed skill.
                    self._by_source_name[(source, entry.name)] = entry
                    # First source wins (higher priority); a same-named skill
                    # from a lower-priority source is recorded as shadowed.
                    if entry.name in self._entries:
                        self._shadowed.append(entry)
                    else:
                        self._entries[entry.name] = entry
        logger.info("indexed %d skills", len(self._entries))
        self._generation += 1
        return len(self._entries)

    def _index_system_skills(self) -> None:
        """Load packaged internal system skills (kept out of ``_entries``).

        These are omni's own machinery (currently ``agent-goal``), not product
        skills, so they are indexed unconditionally from the packaged dir and are
        reachable only by exact name / ``$name`` — never via the catalog or
        capability matching (which read ``_entries``)."""
        self._system_entries = {}
        for path in skill_dirs_in(SYSTEM_SKILLS_DIR):
            try:
                entry = parse_skill_path(path, source="builtin")
            except Exception as exc:  # noqa: BLE001 - a broken system skill must not brick discovery
                logger.warning("skipping invalid system skill %s: %s", path, exc)
                continue
            self._system_entries[entry.name] = entry

    def register(self, entry: SkillEntry) -> None:
        self._entries[entry.name] = entry
        self._by_source_name[(entry.source, entry.name)] = entry
        self._generation += 1

    @property
    def generation(self) -> int:
        """Monotonic catalog generation for safe derived-view caching."""
        return self._generation

    def get(self, name: str) -> SkillEntry | None:
        return self._entries.get(name) or self._system_entries.get(name)

    def get_scoped(self, source: str, name: str) -> SkillEntry | None:
        """Return a specific ``(source, name)`` skill, even if it is shadowed by
        a higher-priority same-named skill. Backs ``$<source>:<name>`` escapes."""
        return self._by_source_name.get((source, name))

    def entries_named(self, name: str) -> list[SkillEntry]:
        """All parsed entries sharing ``name`` across sources (winner + shadowed),
        ordered by source priority (highest first)."""
        found = [e for (src, nm), e in self._by_source_name.items() if nm == name]
        return sorted(found, key=lambda e: _SOURCE_RANK.get(e.source, 0), reverse=True)

    def find_reader(self, suffix: str = "", mime: str = "") -> SkillEntry | None:
        """Return the highest-priority skill that *declares* it can read this
        content type (SKILL.md ``runtime_requirements.reads``), matched by file
        extension and/or MIME.

        Declaration-driven on purpose: ``open_artifact`` routes a binary artifact
        to the owning capability by looking it up here, so a new readable format
        is added by declaring ``reads`` on a skill — never by editing host code.
        Returns ``None`` when no skill claims the type (the caller then reports an
        honest "no reader registered" instead of pushing the model to shell).
        """
        suffix = (suffix or "").strip().lower()
        mime = (mime or "").strip().lower()
        if not suffix and not mime:
            return None
        best: SkillEntry | None = None
        for entry in self._entries.values():
            exts = {str(e).strip().lower() for e in entry.reads_extensions}
            mimes = {str(m).strip().lower() for m in entry.reads_mime}
            if (suffix and suffix in exts) or (mime and mime in mimes):
                if best is None or entry.priority > best.priority:
                    best = entry
        return best

    def find_python_module_provider(self, names: Iterable[str]) -> SkillEntry | None:
        """Highest-priority skill that *declares* it owns one of ``names`` as a
        runtime Python module (SKILL.md ``runtime_requirements.python_modules``).

        Lets the shell guard answer "a capability already provides this package —
        route to it / use its setup command" instead of running an ad-hoc
        cross-interpreter ``pip install``. Matching is hyphen/underscore- and
        case-insensitive so a distribution token (``python-pptx``) still resolves
        to a declared import module (``pptx``) when they normalize alike.
        """
        wanted = {_normalize_module_token(n) for n in names if str(n).strip()}
        if not wanted:
            return None
        best: SkillEntry | None = None
        for entry in self._entries.values():
            declared = {_normalize_module_token(m) for m in entry.requires_python_modules}
            if declared & wanted:
                if best is None or entry.priority > best.priority:
                    best = entry
        return best

    def shadowed_entries(self) -> list[SkillEntry]:
        """Entries excluded from automatic selection because a higher-priority
        same-named skill won. Still reachable via ``$<source>:<name>``."""
        return list(self._shadowed)

    def _lookup_bare(self, name: str) -> SkillEntry | None:
        for variant in _name_variants(name):
            entry = self._entries.get(variant) or self._system_entries.get(variant)
            if entry is not None:
                return entry
        return None

    def _lookup_scoped(self, sources: tuple[str, ...], name: str) -> SkillEntry | None:
        variants = _name_variants(name)
        for source in sources:
            for variant in variants:
                entry = self._by_source_name.get((source, variant))
                if entry is not None:
                    return entry
        return None

    def resolve_explicit(self, token: str) -> SkillEntry | None:
        """Resolve a ``$name`` or ``$<scope>:<name>`` explicit selection.

        A bare ``name`` resolves to the winning (highest-priority) skill. A
        ``<scope>:<name>`` prefix forces a specific source, so a user/external
        skill shadowed by a same-named built-in stays reachable — e.g.
        ``$user:openalex-search`` runs the imported one, ``$builtin:openalex-search``
        the packaged one. Unknown scopes fall back to a bare-name lookup so an
        actual skill name containing ``:`` still resolves.
        """
        raw = (token or "").strip()
        if not raw:
            return None
        scope, sep, rest = raw.partition(":")
        if sep and rest:
            sources = scope_sources(scope)
            if sources is not None:
                return self._lookup_scoped(sources, rest)
        return self._lookup_bare(raw)

    def resolve_ref(self, name: str, source: str = "") -> SkillEntry | None:
        """Resolve an already-selected skill reference. When ``source`` is set
        only the exact ``(source, name)`` entry is valid; otherwise the winner
        for ``name`` is returned."""
        if source:
            return self.get_scoped(source, name)
        return self._lookup_bare(name)

    def list_all(self) -> list[SkillEntry]:
        return sorted(self._entries.values(), key=lambda e: e.name)

    def list_selectable(self) -> list[SkillEntry]:
        return [e for e in self.list_all() if is_selectable_skill(e)]

    def list_sync_tools(self) -> list[SkillEntry]:
        return [e for e in self.list_selectable() if e.delivery_mode == DeliveryMode.SYNC_TOOL]

    def list_async_skills(self) -> list[SkillEntry]:
        return [e for e in self.list_selectable() if e.delivery_mode == DeliveryMode.ASYNC_TASK]

    def configured_default_for(self) -> dict[str, str]:
        return dict(self._settings.skills.default_for)

    def async_skill_names(self) -> set[str]:
        return {e.name for e in self.list_async_skills()}

    def resolve_capability(
        self,
        capability: str,
        *,
        allow_contract_none: bool = False,
        limit_rejections: int = 5,
    ) -> tuple[SkillEntry | None, list[tuple[SkillEntry, str]]]:
        """Select the best installed skill for a semantic capability slot.

        This is the registry-driven planning hinge: the agent can ask for
        ``qa.grounded`` or ``artifact.figure`` without knowing which concrete
        built-in, project, or user skill will satisfy it. Contract-less third
        party skills stay visible as rejected candidates unless explicitly
        allowed by the caller.
        """
        candidates: list[_CapabilityCandidate] = []
        rejected: list[tuple[SkillEntry, str]] = []
        for entry in self.list_selectable():
            score, reason = _capability_score(entry, capability)
            if score <= 0:
                continue
            tier = _capability_tier(entry, allow_contract_none=allow_contract_none)
            if tier is None:
                if len(rejected) < limit_rejections:
                    rejected.append((entry, "contract is none; automatic required workflow step requires a contract"))
                continue
            candidates.append(_CapabilityCandidate(entry=entry, score=score, reason=reason, tier=tier))

        if not candidates:
            return None, rejected

        best_tier = min(candidate.tier for candidate in candidates)
        eligible = [candidate for candidate in candidates if candidate.tier == best_tier]
        eligible.sort(key=lambda candidate: candidate.sort_key, reverse=True)
        preferred = str(self.configured_default_for().get(capability) or "").strip()
        selected = eligible[0]
        if preferred:
            for candidate in eligible:
                if candidate.entry.name == preferred:
                    selected = candidate
                    break

        for candidate in sorted(candidates, key=lambda item: (item.tier, item.sort_key), reverse=True):
            if candidate.entry.name == selected.entry.name:
                continue
            if len(rejected) >= limit_rejections:
                break
            rejected.append((candidate.entry, _capability_rejection_reason(selected, candidate)))
        return selected.entry, rejected

    def index_prompt(self) -> str:
        lines = ["Available research skills (load full instructions only when selected):"]
        for e in self.list_async_skills():
            line = f"- {e.name}: {e.short_desc(140)}"
            if e.when_to_use:
                line += f" (use when: {e.when_to_use[:80]})"
            lines.append(line)
        return "\n".join(lines)

    def selection_prompt(self) -> str:
        """Compact all-skill catalog for model-side workflow planning."""
        lines = ["Skill contract catalog for workflow planning:"]
        for e in self.list_selectable():
            phrases = e.trigger.get("phrases") if isinstance(e.trigger, dict) else []
            phrase_text = "; examples: " + ", ".join(str(p) for p in phrases[:5]) if phrases else ""
            when = f"; use when: {e.when_to_use[:90]}" if e.when_to_use else ""
            outputs = _compact_output_contract(e)
            capability_text = "; capabilities: " + ", ".join(e.capabilities[:5]) if e.capabilities else ""
            priority_text = f"; priority: {e.priority}" if e.priority else ""
            role_text = f"; role: {e.skill_role}"
            lines.append(
                f"- {e.name} [{e.delivery_mode.value}/{e.kind.value}]: "
                f"{e.short_desc(130)}{when}{phrase_text}{capability_text}{role_text}{priority_text}{outputs}"
            )
        lines.append(
            "Selection policy: explicit providers win; task skills own primary deliverables; "
            "support skills may only satisfy dependencies. Prefer trusted full-contract core providers, "
            "then project defaults, then contracted extensions."
        )
        return "\n".join(lines)

    def react_skill_catalog(self, *, context_window_tokens: int = 0) -> str:
        """ReAct name+description index. Never includes input_schema."""
        from omni.skills_runtime.catalog_prompt import render_react_skill_catalog

        return render_react_skill_catalog(
            self.list_selectable(),
            context_window_tokens=context_window_tokens,
        )

    def react_discovery_hint(self, *, context_window_tokens: int = 0) -> str:
        """ReAct skill index — names and descriptions, never the planner contracts."""
        return self.react_skill_catalog(context_window_tokens=context_window_tokens)

    def suggest(self, message: str, *, limit: int = 5) -> list[SkillEntry]:
        """Search skill metadata for an interactive catalog query.

        This method is not an intent router. Automatic execution resolves the
        semantic planner's capability slots through :meth:`resolve_capability`.
        """
        msg = (message or "").casefold().strip()
        if not msg:
            return []
        msg_tokens = {w for w in re.findall(r"\w+", msg, flags=re.UNICODE) if len(w) > 1}
        scored: list[tuple[float, str, SkillEntry]] = []
        for e in self._entries.values():
            if not is_selectable_skill(e):
                continue
            score = 0.0
            phrases = e.trigger.get("phrases") if isinstance(e.trigger, dict) else None
            for p in phrases or []:
                p_norm = str(p).lower().strip()
                if p_norm and p_norm in msg:
                    score += 2.0
            if len(e.name) > 2 and e.name.lower() in msg:
                score += 1.5
            hay = f"{e.name} {e.description} {e.when_to_use}".casefold()
            hay_tokens = {w for w in re.findall(r"\w+", hay, flags=re.UNICODE) if len(w) > 1}
            overlap = len(msg_tokens & hay_tokens)
            if overlap:
                score += min(1.0, overlap / max(3, len(msg_tokens)))
            if score > 0:
                scored.append((score, e.name, e))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [e for _, _, e in scored[:limit]]


def _capability_score(entry: SkillEntry, capability: str) -> tuple[float, str]:
    requested = capability.strip().lower()
    aliases = {item.lower() for item in _CAPABILITY_ALIASES.get(requested, (requested,))}
    caps = {str(item).lower() for item in entry.capabilities or []}
    reason_parts: list[str] = []
    score = 0.0

    if requested in caps:
        score += 100.0
        reason_parts.append(f"declares {requested}")
    else:
        matched_aliases = sorted(caps & aliases)
        if matched_aliases:
            score += 92.0
            reason_parts.append("declares alias " + ",".join(matched_aliases[:3]))

    if score <= 0:
        return 0.0, ""

    priority_weight = min(float(entry.priority or 0), 500.0) / 10.0
    score += priority_weight
    reason_parts.append(f"contract={entry.contract_level}")
    reason_parts.append(f"source={entry.source}")
    if entry.priority:
        reason_parts.append(f"priority={entry.priority}")
    return score, "; ".join(reason_parts)


def _capability_tier(entry: SkillEntry, *, allow_contract_none: bool) -> int | None:
    """Return the automatic-selection tier for a capability candidate.

    Lower is better. Priority and trigger matching are deliberately unable to
    cross these boundaries: core full-contract skills are the trustworthy
    default, core partial skills are degraded but still local, and external
    skills are fallback candidates unless the user explicitly picks them by
    name elsewhere.
    """
    contract = entry.contract_level
    is_core = entry.source in _CORE_CAPABILITY_SOURCES
    if contract == "none" and not allow_contract_none:
        return None
    if is_core and contract == "full":
        return 0
    if is_core and contract == "partial":
        return 1
    if not is_core and contract in {"full", "partial"}:
        return 2
    return 3 if allow_contract_none else None


def _capability_rejection_reason(selected: _CapabilityCandidate, candidate: _CapabilityCandidate) -> str:
    prefix = "lower capability score"
    if selected.tier == 0 and candidate.tier > 0:
        prefix = "core full-contract skill is available"
    elif selected.tier <= 1 and candidate.tier > selected.tier:
        prefix = "core capability skill is available"
    elif candidate.tier > selected.tier:
        prefix = "higher-trust capability tier selected"
    return f"{prefix}: {candidate.reason}"
