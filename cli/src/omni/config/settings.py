"""Layered TOML configuration (Codex-style precedence).

Precedence (high → low):

1. explicit overrides (CLI ``--config k=v``)
2. project ``<repo>/.omni/config.toml`` (trusted project)
3. profile ``~/.omni/<profile>.config.toml`` (``--profile``)
4. user ``~/.omni/config.toml``
5. environment (``OMNI_*`` and standard provider variables)
6. built-in defaults

Sensitive keys (provider/embedding endpoints, API keys, channel secrets) and
the embedding opt-in switch may NOT be overridden from a project-level file —
this prevents a cloned repo from silently redirecting credentials or enabling
billable vector calls, matching Codex's behaviour.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from omni.config.paths import OmniPaths, get_paths
from omni.core.model_catalog import max_input_tokens_for, max_output_tokens_for

# Keys that a project-local config.toml is not allowed to set.
_PROJECT_FORBIDDEN_PREFIXES = (
    "model.base_url",
    "model.api_key",
    "memory.embeddings_enabled",
    "memory.embedding_provider",
    "memory.embedding_base_url",
    "memory.embedding_api_key",
    "memory.embedding_model",
    "memory.embedding_dim",
    "memory.embedding_specter2_python",
    "memory.embedding_specter2_base_model",
    "memory.embedding_specter2_adapter",
    "memory.embedding_specter2_device",
    "channels.",
    "hooks.",
    # Semantic validation and model-repair policy are owner decisions. A cloned
    # project may declare skill contracts, but it cannot disable their gate or
    # opt the owner into an additional model call.
    "planner.",
    "compute_profiles.",
    "subagents.default_model",
    "subagents.default_compute_profile",
    "subagents.default_isolation",
    # Literature credentials are owner-controlled.  A cloned project may
    # choose which connectors to use, but it must never be able to replace the
    # owner's Semantic Scholar token.
    "research.semantic_scholar_api_key",
    # Vision endpoints and credentials are owner-controlled.  Strip the whole
    # namespace so a cloned project cannot redirect image data or pair an owner
    # token with a project-selected provider/model.
    "vlm.",
    # Workspace trust is a *global* user decision; a cloned repo must never be
    # able to declare itself trusted or redirect where generated files land.
    "trust.",
    # The home-level background service is a machine-global, owner-controlled
    # decision; a cloned project must never enable/disable or reconfigure it.
    "service.",
    # Web-process admission is an owner preference; a cloned repo must not
    # cap or uncap the owner's loopback UI.
    "web.",
    "artifacts.output_dir",
    # Global-memory routing and IM identity are owner/machine decisions: a
    # cloned repo must not be able to divert the owner's cross-workspace memory
    # store or collapse per-peer isolation into the owner's identity.
    "memory.global_store",
    "memory.channel_identity",
)


class ModelCfg(BaseModel):
    provider: str = "mock"  # mock | openai_compatible | anthropic | ollama
    base_url: str = ""
    api_key: str = ""
    model: str = "omni-mock"
    # 0 → size the response cap from the model catalog. A pinned value wins, so
    # an existing config that says 4096 still gets 4096.
    max_tokens: int = 0
    temperature: float = 0.3
    request_timeout_s: float = 120.0
    # optional fallback
    fallback_provider: str = ""
    fallback_base_url: str = ""
    fallback_api_key: str = ""
    fallback_model: str = ""


class VlmCfg(BaseModel):
    """Owner-controlled vision-model configuration shared by VLM skills."""

    enabled: bool = False
    model: str = ""
    endpoint: str = ""
    api_key: str = ""
    protocol: str = "openai_compatible_chat"
    timeout_s: float = 180.0


class ReactCfg(BaseModel):
    # Coordinator counters are opt-in. -1 means unbounded, matching Codex's
    # progress-driven turn loop; 0 remains an exact zero-work policy for
    # backward compatibility and fail-closed owner configuration. Positive
    # values are hard ceilings. Plan/Skill limits remain independently scoped.
    max_iterations: int = -1
    max_tool_calls: int = -1
    # ── Three-layer wall-clock model (Codex / Claude Code / OpenClaw parity) ──
    # None of the reference agents kill a whole turn at a low fixed wall clock;
    # they bound *stalls* and *resources* and let a productive turn run on, then
    # always deliver a best-effort answer. Omni mirrors that with three layers,
    # all of which funnel into the *same* forced final synthesis (never a bare
    # "execution timed out" failure that discards completed tool results):
    #
    #   1. ``stall_timeout_s``  — idle watchdog. A single model call that
    #      goes *quiet* (no SSE / delta / tool-call fragment) trips this; it
    #      resets on every observable event, so a long streaming draft is never
    #      clipped. This is the *primary* "something is stuck" guard. Default
    #      matches Codex's stream idle (5 minutes).
    #   2. ``max_seconds``      — overall hard ceiling (runaway backstop, not the
    #      primary bound). Interactive turns use this; scheduled/research turns
    #      use the larger ``scheduled_max_seconds`` (threaded per run).
    #   3. ``foreground_soft_seconds`` — soft threshold. Past it the turn is a
    #      long-running task: the loop emits a one-time "still working" notice
    #      (so the UI keeps showing the task id) but does NOT stop or fail.
    #
    # Sized so a legitimately long research turn completes; clipping a productive
    # turn here would reintroduce the very "couldn't finish in N steps" failure
    # this control model removes. Finalization owns a *separate* reserve below.
    max_seconds: float = 1800.0
    # Overall ceiling for headless scheduled / long-running research turns
    # (``origin="schedule"``). Threaded into the loop per run so an autonomous
    # multi-stage job is not clipped at the interactive ceiling.
    scheduled_max_seconds: float = 7200.0
    # Idle watchdog: the longest a *single* model call may go quiet (no SSE
    # activity) before the loop forces a graceful final synthesis (reason
    # ``stalled``). 0 disables it. Aligns with Codex ``DEFAULT_STREAM_IDLE_TIMEOUT_MS``.
    stall_timeout_s: float = 300.0
    # Extra attempts after the first on idle/transport errors (Codex stream
    # reconnects). The CLI shows ``Reconnecting n/N``; IM stays silent until
    # these are exhausted.
    stream_max_retries: int = 5
    # Soft foreground threshold. When a turn passes this the loop emits one
    # ``notice`` event (kind ``soft_timeout``) so the surface can reaffirm the
    # task id and long-running status. It never stops or fails the turn. 0
    # disables the notice.
    foreground_soft_seconds: float = 240.0
    # Independent reserve for the final tool-free answer after exploration ends.
    # A bounded stop (iteration/tool/token/cost) must still deliver a real
    # best-effort answer, never a "reached the iteration limit" stub — so this
    # reserve is generous and retried once when the provider is transiently slow.
    finalization_timeout_s: float = 45.0
    # How many times to attempt the final tool-free synthesis before falling back
    # to a salvage stub. >1 means a timed-out/transient synthesis is retried so a
    # slow provider does not turn a bounded stop into a non-answer.
    finalization_attempts: int = 2
    # Consecutive unproductive tool observations (identical repeats, or calls
    # that returned nothing usable) after which the loop stops spending and
    # writes its final answer from what it already has.
    no_progress_threshold: int = 2
    # In-execution self-review (P1 research depth): after the main ReAct loop
    # produces a final answer, an LLM-as-judge scores it against the user's goal
    # and, below the threshold, feeds the critique back for one bounded revision
    # *before* the answer is presented (the coordinator-level analogue of the
    # subagent reviewer gate). Off by default — an extra judge call per turn — but
    # worth enabling for research/artifact work where correctness beats latency.
    self_review: bool = False
    self_review_min_score: float = 0.6
    self_review_max_revises: int = 1
    # Streaming output (P2): when a TTY is present, render the assistant's answer
    # progressively as it arrives (Claude Code / Codex feel) instead of appearing
    # all at once. Providers with native SSE stream token-by-token; others (and
    # the offline mock) fall back to progressive chunking of the final answer.
    stream: bool = True


class DisplayCfg(BaseModel):
    """Terminal live-progress display (Claude Code / Codex-style transcript).

    Only the CLI surfaces (REPL and one-shot runs) construct a live display;
    IM channels never receive live events, so these knobs are CLI-only.
    """

    # quiet: no live events; normal: plan/tool/step lines with result previews;
    # verbose: expanded arguments/results, skill stages, context and budgets.
    verbosity: str = "normal"
    # Reveal the L4 diagnostic layer (raw args/results, protocol labels, budget
    # and transcript internals) without widening the whole verbosity band. The
    # global --debug flag sets this for a run; verbose implies it as well.
    debug: bool = False
    # Transient bottom status line (spinner · stage · elapsed · tool count).
    status_line: bool = True
    # auto: inline dock (normal buffer, native scrollback, no mouse capture) on a
    # capable TTY, classic elsewhere; either mode can be forced by owner config,
    # OMNI_UI, or the global --ui option. "tui" now selects the inline dock.
    ui_mode: str = "auto"  # auto | tui | classic


class PlannerCfg(BaseModel):
    """Owner-controlled bounded repair of objective provider-schema errors."""

    # off: deterministic floor only; allowlist: repair named capabilities;
    # auto: repair any trusted full-contract model-owned field.
    model_repair: Literal["off", "allowlist", "auto"] = "auto"
    model_repair_capabilities: list[str] = Field(
        default_factory=lambda: ["artifact.figure"]
    )
    # How long a finished identical request is worth mentioning. The turn always
    # runs again (Codex / Claude Code). Within the window the user is told the
    # earlier task id so they can open it; beyond it there is no hint. Zero
    # disables the hint. Repeating a request is never a question.
    retrieval_window_minutes: int = 30


class InteractionCfg(BaseModel):
    """Top-level agent interaction modes.

    ``auto`` executes a validated plan, ``plan`` persists it and waits for an
    explicit approval, and ``review`` gives the model a read-only tool surface
    and forces an output review before presentation.
    """

    default_mode: str = "auto"  # auto | plan | review


class HooksCfg(BaseModel):
    """Trusted lifecycle hook commands keyed by event name.

    Hooks are owner configuration, never project configuration. Commands are
    parsed with ``shlex`` and executed without a shell. A hook receives a JSON
    event on stdin and may return ``{"action":"deny","reason":"..."}``.
    """

    enabled: bool = False
    timeout_s: float = 10.0
    failure_policy: str = "warn"  # warn | fail
    max_output_bytes: int = 64 * 1024
    commands: dict[str, list[str]] = Field(default_factory=dict)


class SubagentsCfg(BaseModel):
    """Multi-agent delegation (coordinating → specialist → reviewer).

    The coordinating ReAct loop may delegate focused subtasks to *specialist*
    sub-agents that each run an isolated ReAct loop (own context, own tool
    budget), optionally in parallel, and hand back a compact summary — not their
    full transcript. A *reviewer* (LLM-as-judge) can gate each specialist's
    output. All bounded so a long-running research task fans out safely instead
    of serially stuffing one context (Claude-Science's three-layer pattern).
    """

    enabled: bool = True
    # Per delegate call: how many specialists, how deep nesting may go, and how
    # many run concurrently. Depth stops a specialist from endlessly re-spawning.
    max_subagents: int = 4
    max_depth: int = 2
    concurrency: int = 3
    # ── Async multi-agent (Codex V2 AgentControl parity) ──
    # Off by default (gray-rollout, mirrors ``multi_agent_v2 = false``). When on,
    # a coordinating turn also gets async delegation tools (``spawn_subagent`` /
    # ``wait_subagent`` / ``list_subagents`` / ``interrupt_subagent``): fire a
    # specialist, keep working, and collect its result later — instead of the
    # blocking fork-join ``spawn_subagents``. ``max_active`` is the session-tree
    # cap on *concurrently executing* async subagents (AgentExecutionLimiter
    # analog); ``wait_default_s`` is the default ``wait_subagent`` timeout.
    async_enabled: bool = False
    max_active: int = 3
    wait_default_s: float = 30.0
    # Per-specialist budgets (kept below the coordinator's so fan-out stays cheap).
    max_iterations: int = 4
    max_tool_calls: int = 8
    max_seconds: float = 90.0
    # Reviewer gate: LLM-as-judge scores each specialist output; below the
    # threshold it asks for one revision (bounded by ``reviewer_max_revises``).
    reviewer_enabled: bool = True
    reviewer_min_score: float = 0.5
    reviewer_max_revises: int = 1
    # Optional per-specialist execution defaults. Individual delegation specs
    # may override these while remaining bounded by the coordinator.
    default_model: str = ""
    default_compute_profile: str = ""
    default_isolation: str = "none"  # none | worktree | container


class MemoryCfg(BaseModel):
    enabled: bool = True
    # ── Machine-global memory (cross-workspace / cross-CLI / cross-channel) ──
    # When on, durable *identity* memory — user-scope preferences, the persona
    # profile, and episodic summaries — is stored in a single machine-global
    # SQLite (``~/.omni/memory.sqlite3``) shared by every workspace, CLI and the
    # serve daemon, instead of the per-workspace ``sessions.sqlite3``. So the
    # agent "remembers you" across projects, terminals and channels. Off ⇒ exact
    # legacy behaviour (everything per-workspace). Workspace-bound knowledge
    # (session/task/artifact rows and project-scope findings) always stays local.
    global_store: bool = True
    # How an IM peer's memory identity is resolved when a serve daemon hosts
    # channels (WeChat / Feishu / DingTalk):
    #   owner    → every *authorized* IM message shares the machine owner's
    #              memory (personal-assistant default: you paired your own
    #              accounts, so what you tell the bot on Feishu is recalled in
    #              the CLI and vice-versa).
    #   per_peer → each IM identity keeps a private partition, seeing only its
    #              own memory + the owner baseline (multi-user shared-bot safe).
    channel_identity: str = "owner"  # owner | per_peer
    # Char budget for the always-injected global memory digest (memory_summary.md);
    # kept small so "who you are" travels with every turn without bloating context.
    summary_token_budget: int = 700
    # Master switch for semantic (vector) recall. Off by default: onboarding asks
    # the user to opt in and configure an endpoint that actually serves
    # ``/embeddings``. When false it wins over any stale embedding endpoint/model
    # values and recall stays keyword-based without a capability probe.
    embeddings_enabled: bool = False
    vector_backend: str = "auto"  # auto | sqlite_vec | none
    embedding_provider: str = ""  # openai_compatible | specter2
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    # Owner-scoped local SPECTER2 runtime. The dedicated Python environment
    # carries torch/transformers/adapters, keeping those heavyweight packages
    # out of Omni's base installation. Project config cannot replace any path.
    embedding_specter2_python: str = ""
    embedding_specter2_base_model: str = ""
    embedding_specter2_adapter: str = ""
    embedding_specter2_device: str = "cpu"
    recall_limit: int = 6
    # Hard bound on how many candidate rows recall pulls into Python to score on
    # any single call. Recall reads the top pinned/important/recent rows up to this
    # cap (never the whole table), and clamps a caller's ``limit`` to it too — so a
    # large/adversarial ``limit`` can never turn recall into a full-store scan or
    # exfiltrate the entire memory. Realistic recall limits are ≤20, so 200 leaves
    # ample headroom while keeping recall O(cap), not O(store).
    recall_candidate_limit: int = 200
    cross_session: bool = True
    # P2: long-term hygiene. Importance decays geometrically for non-pinned,
    # decaying-type entries during session-end maintenance; near-duplicates merge.
    decay_factor: float = 0.9
    staleness_days: int = 45
    profile_enabled: bool = True
    # ``recall`` scans the whole table in-process (fine for a single-user store
    # kept lean by decay/dedup). Warn once when the store grows past this many
    # rows so the scan never silently becomes a latency sink — the hint points
    # at `omni memory clear` / decay. 0 disables the check.
    max_entries_warn: int = 5000
    # ── Multi-session memory graph (P3) ───────────────────────────────────
    # Memory nodes get auto-linked to their nearest existing neighbours on write,
    # and recall spreads one hop over those edges so retrieving one hit surfaces
    # its cross-session neighbours, so relevance can improve across sessions.
    # All bounded: link scanning reuses ``recall_candidate_limit`` and the
    # out-degree cap keeps the graph sparse. Set ``graph_enabled=false`` to skip
    # both linking and spreading entirely (pure flat recall).
    graph_enabled: bool = True
    graph_max_edges: int = 5  # max auto edges added per new memory (out-degree cap)
    graph_min_weight: float = 0.6  # similarity/overlap threshold to create an edge
    graph_spread_hops: int = 1  # neighbour hops recall spreads over the top hits
    graph_spread_decay: float = 0.5  # per-hop boost decay; boost = weight * decay^hop
    # ── Model-aware context compaction (P2) ──────────────────────────────
    # Two tiers, deliberately staggered. Trimming old tool observations is cheap
    # and loses little; folding the transcript into a bridge summary costs a
    # model call and discards the detail a long research run is built on. Both
    # are fractions of the same ceiling — the lower of the model's actual
    # context window and the transcript the run's token budget can sustain
    # re-sending (``transcript_ceiling_tokens``) — Claude/Codex-style rather
    # than a fixed char budget. Driving both from one number made the expensive
    # tier fire at the same moment as the cheap one, summarising while a third
    # of the window still sat unused; Codex compacts at ~90%.
    autocompact_pct: float = 0.90  # expensive tier: fold the transcript
    microcompact_pct: float = 0.70  # cheap tier: shrink old tool observations
    # Pin the largest prompt to build; 0 → infer from ``model.model`` (see
    # ``infer_max_input_tokens``). Set this for models we don't recognise. The key
    # keeps its original name: it is written in configuration files that exist.
    context_window_tokens: int = 0
    # Microcompact (the cheap first tier): before folding the conversation,
    # shrink the content of older tool observations inside a long single ReAct
    # turn, keeping the most recent N intact. 0 disables it.
    microcompact_keep_tool_results: int = 4
    # Bound the *latest* tool observation before it re-enters the transcript.
    # Microcompact only trims older results; a single huge skill dump still
    # burns tokens on every later iteration. 0 keeps the historic unbounded
    # dump. The full result stays on the task event.
    tool_observation_max_chars: int = 8000


class SkillsCfg(BaseModel):
    # Priority high→low. By default OmniScientist indexes only the skills it
    # *manages*: the packaged built-ins (highest priority — they ship and update
    # with the program) and the user skills root (``~/.omni/skills`` — where
    # ``omni skills add`` imports to). Built-ins hard-override a same-named user
    # skill; use ``$user:<name>`` (or ``$<source>:<name>``) to force a shadowed
    # skill. The in-repo ``project_omni`` (``.omni/skills``) and the Claude Code
    # / Codex / OpenClaw on-disk libraries are NOT indexed by default (so a huge
    # personal library doesn't drown the catalog, and per-repo skills don't leak
    # across projects); opt in per command with ``omni skills list --all`` or by
    # adding their source names here (e.g. ``["builtin", "user_omni", "project_omni"]``).
    sources: list[str] = Field(
        default_factory=lambda: [
            "builtin",
            "user_omni",
        ]
    )
    disabled: list[str] = Field(default_factory=list)
    # Project/user routing defaults. Keys are free-form capability phrases
    # matched against the current request (for example, "architecture diagram"),
    # values are skill names.
    default_for: dict[str, str] = Field(default_factory=dict)
    # Which tools ``omni skills export`` copies the built-in skills to (the
    # system roots those tools read). Subset of: claude, codex, openclaw.
    export_targets: list[str] = Field(
        default_factory=lambda: ["claude", "codex", "openclaw"],
    )
    # Delegated-skill ceilings: the most a manifest's ``execution`` block may
    # request. They bound the independent prompt defaults below; an unbounded
    # coordinator sentinel must never be reused as a delegated skill budget.
    max_prompt_iterations: int = 32
    max_prompt_tool_calls: int = 80
    # Prompt-skill defaults are independent from both the unbounded coordinator
    # sentinel and the trusted ceilings. A manifest may request more than these
    # defaults, but never more than the corresponding max_prompt_* value.
    default_prompt_iterations: int = 20
    default_prompt_tool_calls: int = 40
    # A ceiling is not a default. Keeping the two the same silently clamps every
    # manifest back to the fallback, so a declared budget could only ever shrink
    # the run — never lengthen it. These bound a declaration at the outer turn /
    # workflow envelope (``react.max_seconds``, ``tasks.workflow_max_seconds``),
    # which the live envelope clock then enforces for real.
    max_prompt_seconds: float = 1800.0
    max_python_seconds: float = 1800.0
    max_cli_seconds: float = 1800.0
    # Wall clock for a skill whose manifest declares no ``execution.max_seconds``.
    # Deliberately well under the ceilings: an undeclared skill that hangs should
    # free the workflow envelope for its siblings rather than consume all of it.
    # A skill that genuinely needs longer says so in its own SKILL.md.
    default_seconds: float = 600.0


class WebFetchCfg(BaseModel):
    allow_hosts: list[str] = Field(
        default_factory=lambda: [
            "arxiv.org",
            "export.arxiv.org",
            "*.arxiv.org",
            "api.semanticscholar.org",
            "*.semanticscholar.org",
            "api.openalex.org",
            "*.openalex.org",
            "api.crossref.org",
            "api.unpaywall.org",
            "eutils.ncbi.nlm.nih.gov",
            "*.ncbi.nlm.nih.gov",
            "api.biorxiv.org",
            "*.biorxiv.org",
            "clinicaltrials.gov",
            "*.clinicaltrials.gov",
        ]
    )
    max_body_bytes: int = 2 * 1024 * 1024
    timeout_s: float = 10.0
    max_redirects: int = 5
    allow_private_hosts: bool = False


class WebSearchCfg(BaseModel):
    """Pluggable web-search backends for the ``web_search`` tool and funnel rung.

    Keyless by default: the Exa and Parallel public MCP endpoints require no
    credential to start, so the capability works out of the box. Keys are an
    optional upgrade (higher limits / keyed providers), never a requirement, and
    the tool degrades to a clear ``needs a key or retry`` message rather than
    raising when nothing can serve a query.
    """

    enabled: bool = True
    # Preference order. Keyless public MCP first (zero setup); keyed REST
    # providers are only attempted when their key is configured. Reorder or add
    # providers by config, never by code.
    backend_order: list[str] = Field(
        default_factory=lambda: ["exa", "parallel", "tavily", "brave", "serper"]
    )
    max_results: int = 5
    timeout_s: float = 20.0
    # Public MCP endpoints for the keyless default (a key only raises limits).
    exa_mcp_url: str = "https://mcp.exa.ai/mcp"
    parallel_mcp_url: str = "https://search.parallel.ai/mcp"
    # Keyed REST endpoint used when an Exa key is configured.
    exa_search_url: str = "https://api.exa.ai/search"
    # Optional API keys — never required; when set they authenticate/select a backend.
    exa_api_key: str = ""
    parallel_api_key: str = ""
    tavily_api_key: str = ""
    brave_api_key: str = ""
    serper_api_key: str = ""


class ResearchCfg(BaseModel):
    """Research-subsystem knobs (literature corpus + reproducible retrieval).

    All additive: the literature corpus, the hypothesis/claim/evidence graph and
    the run ledger reuse the existing per-workspace store and embedding surface.
    """

    # Date pin (``YYYY-MM-DD``) for reproducible, date-restricted retrieval.
    # Empty → no restriction. Set per-project for an Asta-style fixed "as-of".
    as_of: str = ""
    # Grounded-retrieval defaults.
    corpus_top_k: int = 6
    chunk_target_words: int = 180
    # Hybrid retrieval: fuse semantic (cosine) + lexical (keyword) rankings with
    # Reciprocal Rank Fusion. Pure-offline; falls back to keyword-only when no
    # embeddings are available. ``rrf_k`` is the standard RRF constant.
    hybrid_rerank: bool = True
    rrf_k: int = 60
    # Contact email some connectors (Unpaywall, Crossref/PubMed polite pool) want.
    contact_email: str = ""
    # Owner-controlled Semantic Scholar API key.  The generic connector can
    # still make anonymous requests, while evidence-critical workflows such as
    # paper-review may require the configured key for a complete run.
    semantic_scholar_api_key: str = ""
    # Curated, trusted literature connectors enabled for routing/use.
    connectors: list[str] = Field(
        default_factory=lambda: [
            "arxiv", "openalex", "crossref", "unpaywall", "pubmed", "semanticscholar",
            "biorxiv", "clinicaltrials",
        ]
    )
    # Registry-driven domain packs add specialist guidance, connectors and
    # artifact expectations without hard-coding domain names in the planner.
    domain_packs: list[str] = Field(default_factory=lambda: ["core", "machine-learning"])
    # Research-fact acceptance over a finished turn's honesty audit:
    #   off    → do not evaluate acceptance
    #   warn   → annotate when grounding / citation support is thin [default]
    #   strict → mark the result not-accepted so the caller can gate/downgrade
    # Kept ``warn`` by default so the differentiating rigor is visible without
    # changing terminal status (the structural verification gate is unchanged).
    acceptance_mode: str = "warn"
    # Minimum semantic citation-support rate and structural grounding rate below
    # which acceptance flags (warn) or fails (strict).
    acceptance_min_citation_support: float = 0.6
    acceptance_min_grounding: float = 0.5


class MCPServerCfg(BaseModel):
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    enabled: bool = True


class SecurityCfg(BaseModel):
    # readonly | workspace-write | full. Default ``workspace-write`` lets the
    # interactive CLI create/edit/delete files and run commands inside the
    # launch directory (Claude-Code parity) — destructive ops still pass through
    # the approval gate, and system-level patterns stay hard-blocked. Sensitive
    # tools remain out of the catalog for IM/no-approver contexts regardless.
    bash_sandbox: str = "workspace-write"
    fs_write_allow: list[str] = Field(default_factory=list)
    # Master switch for the human-in-the-loop approval gate (P0). When True the
    # owner confirms mutating/executing tool calls (bash / write_file / edit_file
    # / run_compute) before they run; False disables the gate (full autonomy).
    require_approval: bool = True
    # How the approval gate behaves when armed (require_approval=True):
    #   never       → auto-approve everything (equivalent to require_approval off)
    #   untrusted   → Codex UnlessTrusted: ask for every non-known-safe exec
    #   on-request  → Codex OnRequest: workspace-write auto-allows non-destructive
    #                 bash; destructive and sandbox-escape still ask
    #   always      → ask before every tool call
    # Factory default is UnlessTrusted so library/test loads stay conservative.
    # A trusted CLI workspace upgrades the unset policy to on-request (Codex Auto).
    approval_policy: str = "untrusted"
    # Skip the prompt for these entries: a bare tool name ("read_file"),
    # "*" (all), or "<tool>:<prefix>" matched against the call's command/path
    # (e.g. "bash:git status", "bash:pytest"). Owner-curated fast path.
    approval_allowlist: list[str] = Field(default_factory=list)
    # OS-level sandbox for local exec (P2-F): auto | off | sandbox-exec | bwrap |
    # firejail. "auto" uses a backend after a functional probe. With no backend
    # it falls back to the coarse guard and warns once (stock Linux has none).
    # Explicit backends fail closed. Set "off" (or bash_sandbox=full) to opt out.
    os_sandbox: str = "auto"
    # Network policy *inside* the OS sandbox: allow | deny. Default ``allow``
    # keeps historical behaviour (research tools often need the network); ``deny``
    # injects a no-network clause (seatbelt ``(deny network*)``, bwrap
    # ``--unshare-net``, firejail ``--net=none``) so untrusted exec can be run
    # air-gapped without disabling the sandbox entirely.
    sandbox_network: str = "allow"
    # Prompt-injection scan over tool observations (web/file/artifact content)
    # before they enter the model context: flag (annotate) | strip | off.
    injection_defense: str = "flag"


class ComputeCfg(BaseModel):
    """Where compute jobs run. Local subprocess by default; ``docker`` / ``ssh`` /
    ``slurm`` / ``modal`` are opt-in and degrade to local (when ``fallback_local``)
    if the backend binary/SDK or its config is unavailable — keeping the tool
    usable offline and without extra deps.
    """

    backend: str = "local"           # local | docker | ssh | slurm | modal
    fallback_local: bool = True
    timeout_s: float = 600.0
    workdir: str = ""                # remote/in-container working dir (docker/ssh/slurm)
    # docker
    docker_image: str = ""           # image ref, e.g. "python:3.12-slim"
    docker_gpus: str = ""            # passed to --gpus (e.g. "all" | "device=0"); empty = none
    docker_mounts: list[str] = Field(default_factory=list)  # -v specs, e.g. "/data:/data:ro"
    # ssh
    ssh_host: str = ""               # user@host
    ssh_port: int = 22
    ssh_key: str = ""
    # slurm
    slurm_partition: str = ""
    slurm_cpus: int = 0
    slurm_mem: str = ""              # e.g. "4G"
    slurm_time: str = ""            # e.g. "01:00:00"
    slurm_via_ssh: bool = True       # submit through ssh_host; else local sbatch
    # modal
    modal_app: str = ""              # runnable ref for `modal run`


class CostCfg(BaseModel):
    """Per-run token & cost accounting (P2).

    On by default: every turn that reaches the ReAct loop records a ``cost.usage``
    run event (tokens + an estimated USD cost) so spend is visible after the fact.
    Python-engine skills are metered the same way: the host wraps ``ctx.llm`` and
    writes ``component=engine:<skill>``. Pricing comes from a small built-in table
    keyed on the model name; override it for your deployment with
    ``input_per_mtok`` / ``output_per_mtok`` (USD per 1M tokens, 0 → use the
    table). Recording is best-effort and never blocks a turn.
    """

    enabled: bool = True
    currency: str = "USD"
    input_per_mtok: float = 0.0   # 0 → use built-in price table
    output_per_mtok: float = 0.0  # 0 → use built-in price table
    # Hard-stop boundaries evaluated after each provider response, per run —
    # the coordinator turn, a delegated prompt skill, and a subagent each carry
    # their own. Zero disables enforcement while keeping accounting.
    #
    # Cumulative token ceilings are owner policy, not a default task-completion
    # mechanism. 0/-1 keeps accounting but does not stop a productive task.
    # Context-window pressure is handled separately by compaction/continuation.
    max_total_tokens: int = 0
    # Cost is left opt-in: it depends on a price table that reads 0 for local
    # and self-hosted models, where it would silently never fire.
    max_cost_usd: float = 0.0
    # Soft notices (Codex-style status, not a stop). 0 disables the warning.
    # Long-horizon research stays owner-capped via the hard knobs above.
    warn_total_tokens: int = 200_000
    warn_cost_usd: float = 0.50


class TasksCfg(BaseModel):
    """Background task lifecycle knobs (P2).

    ``auto_retry`` replays an explicitly ``execution.replay_safe`` skill after a
    *transient* error (timeout / connection / 5xx / rate-limit), up to
    ``max_auto_retries`` times. Skills without that owner-authored contract and
    deterministic failures (bad input, unknown skill, empty result) fail fast.
    Set ``auto_retry=false`` to require a manual ``omni task retry``.
    """

    auto_retry: bool = True
    max_auto_retries: int = 2
    # Backoff between inline retries: sleep ``retry_backoff_s × attempt`` seconds.
    # 0 (default) keeps retries immediate (and the test suite fast); raise it to be
    # polite to a rate-limited upstream.
    retry_backoff_s: float = 0.0
    # A task (user request) whose process died mid-turn would show "running"
    # forever. When a task in running/recovering has produced no event for this
    # many seconds it is settled as ``interrupted`` by the startup/tick
    # reconcile (omni serve, task drain). Long enough that a slow model or
    # compute step never trips it; 0 disables reconciliation entirely.
    interrupt_stale_after_s: float = 1800.0
    # Age-based history cleanup: failed / cancelled / interrupted tasks older
    # than this many days are deleted automatically (cascading to their
    # subtasks and events; artifact files are never touched). Succeeded and
    # degraded tasks are provenance and are never auto-deleted. 0 keeps
    # everything forever.
    retention_days: int = 0
    # Reclassify a terminal-succeeded turn that answered without reaching for
    # anything -- no tool call, no delegated child task, and no
    # skill/subtask/workflow/artifact/schedule -- to ``kind="chat"``, so pure
    # conversational answers drop out of the default ``/task`` view (they stay
    # in the transcript and under ``/task list --kind chat``). Set to False to
    # keep the legacy "one task per request" behaviour.
    classify_conversational: bool = True
    # Maximum number of dependency-ready, concurrency-safe workflow steps run
    # in one DAG wave. Unsafe or unspecified steps remain serial.
    workflow_concurrency: int = 4
    # Aggregate workflow envelope across all skill steps. Individual skill
    # budgets still apply inside this owner-controlled outer boundary. The
    # default tool envelope is 12 steps times the 8-call specialist allowance.
    workflow_max_steps: int = 12
    workflow_max_tool_calls: int = 96
    workflow_max_seconds: float = 1800.0


class SchedulesCfg(BaseModel):
    """Cron / scheduled-jobs knobs (P2).

    A schedule is a *recurring source of tasks*: when it comes due the scheduler
    materialises it into a normal background task (reusing the runtime's
    durability / retry / notifications). ``enabled`` gates the whole feature;
    when a long-running process (``omni serve``) is up, its poller ticks the
    scheduler every ``tick_interval_s`` seconds so due jobs fire without a
    separate daemon. One-shot ``drain`` / CLI paths fire due jobs on demand.
    """

    enabled: bool = True
    tick_interval_s: float = 30.0
    # Safety cap: never enqueue more than this many jobs in a single tick (guards
    # against a misconfigured schedule or a long outage backlog stampede).
    max_per_tick: int = 50
    # How a due *goal* schedule (``agent-goal`` marker) executes:
    #   headless_turn → run the same planner→workflow→verification pipeline an
    #                   interactive turn uses, so a multi-deliverable goal is
    #                   decomposed into separately-budgeted, separately-verified
    #                   steps instead of one flat, bounded ReAct loop that hits an
    #                   iteration cliff and returns a single deliverable (the
    #                   deployed regression: "fetch abstract + draw diagram + write
    #                   paper" produced only the diagram). This is the default.
    #   skill         → legacy: enqueue the ``agent-goal`` prompt-skill sub-agent
    #                   as a single background subtask (kept as an escape hatch and
    #                   for schedules that target a specific skill by name).
    # Explicit-skill schedules (``omni schedule add <skill>``) always run that
    # skill directly regardless of this setting.
    execution_mode: str = "headless_turn"  # headless_turn | skill
    # Verification-driven bounded auto-continuation. When a headless scheduled turn
    # finishes ``degraded``/``failed`` because required deliverables are missing,
    # enqueue up to ``max_continuations`` follow-up turns that re-plan and re-run
    # only the missing work, then re-verify — an unattended "keep going until done"
    # with a hard ceiling so a stuck goal can never loop forever.
    auto_continue: bool = True
    max_continuations: int = 1
    # Unattended autonomy for scheduled runs. A schedule fires in ``omni serve``
    # where there is no interactive approver, so without a grant every sensitive
    # tool (write_file/edit_file/run_compute/bash) fails closed and the run
    # produces nothing. Creating a schedule is itself an owner-consented
    # (``sensitive``) action, so scheduled jobs may pre-authorise a bounded tool
    # set — seeded onto each schedule and granted to the run's owning task:
    #   off      → grant nothing (fail-closed; sensitive tools stay blocked)
    #   standard → write_file, edit_file, run_compute [default: lets research
    #              jobs write artefacts / run compute, but NOT arbitrary shell]
    #   full     → standard + bash (arbitrary shell; use only when trusted)
    # The OS sandbox / fs roots still confine *where* those tools may act.
    autonomy: str = "standard"


class ChannelsCfg(BaseModel):
    enabled: list[str] = Field(default_factory=lambda: ["cli"])


class WebCfg(BaseModel):
    """Loopback web surface knobs. CLI, IM, and the agent runtime ignore these.

    ``max_inflight_turns`` caps concurrent *running* turns started by one
    ``omni web`` process in a single workspace. ``0`` means unlimited — the
    same policy as opening many CLI windows on that workspace. Session and
    window counts are never capped.
    """

    max_inflight_turns: int = 10


class ServiceCfg(BaseModel):
    """Home-level background service preferences (machine-global control plane).

    This is *not* the same as ``[serve]`` (there is none): it is the persisted
    preference for whether OmniScientist keeps ONE supervised background service
    per ``OMNI_HOME`` that owns messaging channels and dispatches schedules for
    every registered workspace — instead of one detached daemon per workspace.

    Only a few knobs live here; the authoritative *desired state* (enabled /
    disabled, chosen supervisor, channel anchor) is persisted separately in
    ``<OMNI_HOME>/service/settings.json`` so a stale config file can never
    silently re-enable a service the user explicitly disabled. These fields are
    onboarding/reconciliation defaults, read only when that state file is
    absent (first run).

    ``manager`` selects the OS supervisor: ``auto`` picks the best available
    (launchd / systemd-user / Windows Scheduled Task) and otherwise falls back
    to a detached best-effort process; ``detached`` forces the fallback.
    ``ensure_on_launch`` makes a bare ``omni`` guarantee the single home service
    is running (always-on model): it enables + installs it on first need and
    repairs it if it drifted down, on a background thread. Set it to ``false`` to
    opt out entirely (CI / power users who manage the unit themselves) — that is
    the escape hatch, since a plain ``omni serve stop`` is only a transient pause
    that the next launch undoes. ``reconcile_interval_s`` is how often the running
    service re-scans the workspace registry for newly opened workspaces.
    """

    enabled: bool = False
    manager: str = "auto"  # auto | launchd | systemd | schtasks | detached
    ensure_on_launch: bool = True
    reconcile_interval_s: float = 30.0
    # Bounded drain window (seconds) the service waits for in-flight tasks to
    # settle before it stops, e.g. during ``omni update`` handoff.
    drain_grace_s: float = 10.0


class TrustCfg(BaseModel):
    """Per-directory workspace trust (Claude Code / VS Code style).

    ``enabled`` turns the whole gate off. ``allow`` pre-trusts directory
    subtrees (power users / automation); trusting a parent trusts everything
    under it. ``prompt`` = ``auto`` prompts interactively on the first run in an
    untrusted directory; ``never`` runs restricted until ``--trust`` / ``omni
    trust`` grants it explicitly.
    """

    enabled: bool = True
    allow: list[str] = Field(default_factory=list)
    prompt: str = "auto"  # auto | never


class ArtifactsCfg(BaseModel):
    """Where generated deliverables are surfaced.

    When the launch directory is trusted, omni writes user-facing deliverables
    (figures / reports / slides) DIRECTLY into per-kind subfolders of
    ``output_dir`` — e.g. ``<output_dir>/figures/<name>.svg`` — by default the
    directory omni was started in, as the single canonical copy, so results land
    next to the user's work like Codex / Claude Code (a clean ``figures/`` bundle,
    no duplicate under ``~/.omni``). Intermediate sidecars (``.dot`` / ``.json``)
    and untrusted or library/direct runs keep the durable workspace store
    (``~/.omni/...``).
    ``mirror_outputs`` is the *effective* switch: it is forced off unless the
    launch directory is trusted (resolved in ``load_settings``). The name is
    retained for compatibility; it now means "write deliverables to output_dir",
    not "copy them there".
    """

    mirror_outputs: bool = False
    output_dir: str = "."
    mirror_formats: list[str] = Field(
        default_factory=lambda: [
            "svg", "png", "pdf", "docx", "pptx", "html", "md", "csv", "xlsx",
        ]
    )


class ObservabilityCfg(BaseModel):
    log_level: str = "INFO"
    telemetry: bool = False


class UpdateCfg(BaseModel):
    """Startup update-notifier knobs (npm / Codex-style).

    A background refresh caches the latest published version; the next launch
    prompts if a newer one exists. All best-effort: offline is silent. Disable
    entirely with ``check = false`` or ``OMNI_UPDATE_CHECK=0``.
    """

    check: bool = True
    interval_hours: float = 24.0
    # Stable installs trust PyPI. ``auto``/``raw`` remain explicit compatibility
    # modes for development channels, never the default release authority.
    source: str = "pypi"
    # Optional development-channel raw ``__init__.py``. Stable installs leave
    # this empty and trust PyPI; configure it explicitly for a repository mirror.
    raw_url: str = ""


class OmniSettings(BaseModel):
    data_dir: str = ""
    default_profile: str = ""
    role: str = ""  # path or inline override of the system role
    model: ModelCfg = Field(default_factory=ModelCfg)
    vlm: VlmCfg = Field(default_factory=VlmCfg)
    react: ReactCfg = Field(default_factory=ReactCfg)
    display: DisplayCfg = Field(default_factory=DisplayCfg)
    planner: PlannerCfg = Field(default_factory=PlannerCfg)
    interaction: InteractionCfg = Field(default_factory=InteractionCfg)
    hooks: HooksCfg = Field(default_factory=HooksCfg)
    subagents: SubagentsCfg = Field(default_factory=SubagentsCfg)
    memory: MemoryCfg = Field(default_factory=MemoryCfg)
    skills: SkillsCfg = Field(default_factory=SkillsCfg)
    web_fetch: WebFetchCfg = Field(default_factory=WebFetchCfg)
    web_search: WebSearchCfg = Field(default_factory=WebSearchCfg)
    research: ResearchCfg = Field(default_factory=ResearchCfg)
    security: SecurityCfg = Field(default_factory=SecurityCfg)
    compute: ComputeCfg = Field(default_factory=ComputeCfg)
    compute_profiles: dict[str, ComputeCfg] = Field(default_factory=dict)
    cost: CostCfg = Field(default_factory=CostCfg)
    tasks: TasksCfg = Field(default_factory=TasksCfg)
    schedules: SchedulesCfg = Field(default_factory=SchedulesCfg)
    channels: ChannelsCfg = Field(default_factory=ChannelsCfg)
    service: ServiceCfg = Field(default_factory=ServiceCfg)
    web: WebCfg = Field(default_factory=WebCfg)
    observability: ObservabilityCfg = Field(default_factory=ObservabilityCfg)
    update: UpdateCfg = Field(default_factory=UpdateCfg)
    trust: TrustCfg = Field(default_factory=TrustCfg)
    artifacts: ArtifactsCfg = Field(default_factory=ArtifactsCfg)
    mcp_servers: dict[str, MCPServerCfg] = Field(default_factory=dict)

    # Resolved at load time (not serialised back).
    paths: OmniPaths | None = Field(default=None, exclude=True)

    model_config = {"arbitrary_types_allowed": True}


# ── helpers ──────────────────────────────────────────────────────────────


def read_toml_file(path: Path) -> dict[str, Any]:
    """Read strict TOML from ``path``."""
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return read_toml_file(path)
    except OSError:
        return {}


def _strip_forbidden(layer: dict[str, Any]) -> dict[str, Any]:
    """Remove keys a project-level config may not set (dotted paths)."""

    def drop(d: dict[str, Any], prefix: str) -> None:
        head, _, tail = prefix.partition(".")
        if not tail:
            d.pop(head, None)
            return
        child = d.get(head)
        if isinstance(child, dict):
            drop(child, tail)

    cleaned = {k: (dict(v) if isinstance(v, dict) else v) for k, v in layer.items()}
    for prefix in _PROJECT_FORBIDDEN_PREFIXES:
        if prefix.endswith("."):
            drop(cleaned, prefix.rstrip("."))
        else:
            drop(cleaned, prefix)
    return cleaned


def _env_layer() -> dict[str, Any]:
    """Map a small, explicit set of env vars into the config tree."""
    env = os.environ
    layer: dict[str, Any] = {}
    model: dict[str, Any] = {}

    def pick(*names: str) -> str | None:
        for n in names:
            if env.get(n):
                return env[n]
        return None

    if (v := pick("OMNI_MODEL_PROVIDER")):
        model["provider"] = v
    if (v := pick("OMNI_MODEL_BASE_URL")):
        model["base_url"] = v
    if (v := pick("OMNI_MODEL_API_KEY", "OPENAI_API_KEY")):
        model["api_key"] = v
    if (v := pick("OMNI_MODEL", "OMNI_MODEL_NAME")):
        model["model"] = v
    if model:
        layer["model"] = model
    vlm: dict[str, Any] = {}
    if (v := pick("OMNI_VLM_MODEL")):
        vlm["model"] = v
    if (v := pick("OMNI_VLM_ENDPOINT")):
        vlm["endpoint"] = v
    if (v := pick("OMNI_VLM_API_KEY")):
        vlm["api_key"] = v
    if vlm:
        vlm["enabled"] = True
        layer["vlm"] = vlm
    research: dict[str, Any] = {}
    if (v := pick("OMNI_SEMANTIC_SCHOLAR_API_KEY", "SEMANTIC_SCHOLAR_API_KEY")):
        research["semantic_scholar_api_key"] = v
    if research:
        layer["research"] = research
    if (v := pick("OMNI_UI", "OMNI_UI_MODE")):
        layer["display"] = {"ui_mode": v}
    return layer


ConfigSourceKind = Literal[
    "default",
    "environment",
    "user",
    "profile",
    "project",
    "secrets",
    "override",
]


@dataclass(frozen=True)
class ConfigSource:
    """The authoritative layer for one effective dotted configuration field."""

    kind: ConfigSourceKind
    detail: str

    @property
    def label(self) -> str:
        """Compact human-readable source used by status/explain commands."""
        return f"{self.kind} ({self.detail})" if self.detail else self.kind


@dataclass(frozen=True)
class SettingsResolution:
    """Effective settings plus field-level provenance for their existing layers."""

    settings: OmniSettings
    sources: Mapping[str, ConfigSource]

    def source_for(self, dotted_path: str) -> ConfigSource:
        """Return the winning source, treating absent fields as built-in defaults."""
        return self.sources.get(
            dotted_path,
            ConfigSource("default", "built-in defaults"),
        )


def _merge_layer_with_sources(
    base: dict[str, Any],
    overlay: dict[str, Any],
    sources: dict[str, ConfigSource],
    source: ConfigSource,
    *,
    prefix: str = "",
) -> dict[str, Any]:
    """Deep-merge one layer while recording the winner for every leaf field."""
    out = dict(base)

    def clear(path: str) -> None:
        for existing in tuple(sources):
            if existing == path or existing.startswith(f"{path}."):
                sources.pop(existing, None)

    for key, value in overlay.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        current = out.get(key)
        if isinstance(value, dict):
            if not isinstance(current, dict):
                clear(path)
                current = {}
            out[key] = _merge_layer_with_sources(
                current,
                value,
                sources,
                source,
                prefix=path,
            )
        else:
            clear(path)
            out[key] = value
            sources[path] = source
    return out


def resolve_settings(
    *,
    project: str | None = None,
    profile: str | None = None,
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
    trusted: bool | None = None,
) -> SettingsResolution:
    """Resolve settings and field sources without changing layer precedence.

    ``trusted`` carries the workspace-trust decision (resolved by the CLI):
    ``False`` skips the repo-local project config layer and never mirrors
    outputs; ``True`` applies the project layer and enables output mirroring
    (subject to the user's ``artifacts.mirror_outputs`` preference); ``None``
    (direct/library callers, tests) keeps the historical behaviour — project
    layer applied, mirroring off.
    """
    paths = get_paths(project=project, cwd=cwd)
    sources: dict[str, ConfigSource] = {}

    # 6. defaults handled by pydantic; start empty and layer up.
    merged: dict[str, Any] = {}

    def apply(layer: dict[str, Any], kind: ConfigSourceKind, detail: str) -> None:
        nonlocal merged
        merged = _merge_layer_with_sources(
            merged,
            layer,
            sources,
            ConfigSource(kind, detail),
        )

    # 5. env
    apply(_env_layer(), "environment", "process environment")
    # 4. user config
    apply(_read_toml(paths.config_file), "user", str(paths.config_file))

    # 3. profile
    chosen_profile = profile or merged.get("default_profile") or ""
    if chosen_profile:
        profile_path = paths.home / f"{chosen_profile}.config.toml"
        apply(
            _read_toml(profile_path),
            "profile",
            f"{chosen_profile}: {profile_path}",
        )

    # 2. project (with forbidden-key protection). An untrusted launch directory
    # (workspace trust declined or not yet granted) must NOT apply repo-supplied
    # config — a cloned repo could otherwise change behaviour before the user
    # vouches for it. Skip the whole project layer only when explicitly untrusted.
    if trusted is not False:
        raw_project_layer = _read_toml(paths.project_config)
        # Unwrap first, then apply the owner-control filter. Filtering the wrapper
        # itself would let `[omni.vlm]` bypass the same rule enforced for `[vlm]`.
        wrapped = raw_project_layer.get("omni")
        project_payload = wrapped if isinstance(wrapped, dict) else raw_project_layer
        project_layer = _strip_forbidden(project_payload)
        apply(project_layer, "project", str(paths.project_config))

    # secrets (merged into model + channels; not project-overridable)
    secrets = _read_toml(paths.secrets_file)
    if secrets:
        apply(_project_safe_secrets(secrets), "secrets", str(paths.secrets_file))

    # 1. explicit overrides
    if overrides:
        apply(overrides, "override", "explicit CLI or caller override")

    settings = OmniSettings(**merged)
    settings.paths = paths
    # ``data_dir`` is a read-only compatibility view of the bootstrap-resolved
    # home. The selection itself cannot live in config.toml inside that home.
    settings.data_dir = str(paths.home)
    # Output mirroring only activates for a directory the user has trusted; the
    # config field expresses the *preference* (default on), the trust decision
    # is the gate. Direct/library (None) and restricted (False) loads never mirror.
    artifacts_layer = merged.get("artifacts")
    pref = True
    if isinstance(artifacts_layer, dict) and "mirror_outputs" in artifacts_layer:
        pref = bool(artifacts_layer["mirror_outputs"])
    settings.artifacts.mirror_outputs = pref and (trusted is True)
    from omni.config.security_preset import apply_codex_security_preset

    apply_codex_security_preset(settings, sources, trusted)
    return SettingsResolution(settings=settings, sources=dict(sources))


def load_settings(
    *,
    project: str | None = None,
    profile: str | None = None,
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
    trusted: bool | None = None,
) -> OmniSettings:
    """Return effective settings using the established layered resolution."""
    return resolve_settings(
        project=project,
        profile=profile,
        cwd=cwd,
        overrides=overrides,
        trusted=trusted,
    ).settings


def _project_safe_secrets(secrets: dict[str, Any]) -> dict[str, Any]:
    """Secrets are always allowed (user-level only file)."""
    return secrets


def infer_max_input_tokens(model: ModelCfg | None) -> int:
    """Best-effort largest acceptable prompt (tokens), inferred from the model name."""
    return max_input_tokens_for(model.model if model else "")


# Neither the prompt nor the reply may claim more than this share of a shared
# context window. Only models whose output cap approaches their whole window are
# affected — gpt-4 and the moonshot line may spend all of theirs on either side —
# and for those no single-sided rule works: reserving the full reply leaves no
# room for a prompt, reserving none leaves no room for the reply, and the
# provider refuses the request either way. Splitting the window is the only
# division that keeps both sides usable, and an even split is the one that needs
# no justification per model.
_MAX_WINDOW_SHARE = 0.5


def _reply_reservation_tokens(requested_output: int, window: int) -> int:
    """Prompt tokens to leave unspent so the reply we ask for still fits beside it."""
    if window <= 0 or requested_output <= 0:
        return 0
    return min(requested_output, int(window * _MAX_WINDOW_SHARE))


def resolve_max_output_tokens(model: ModelCfg | None) -> int:
    """Output tokens to request per response: explicit config, else the catalog.

    ``ModelCfg.max_tokens`` used to default to a flat 4096 for every model, which
    silently truncated any response longer than that — including a tool call
    carrying a document, whose arguments then arrived unparseable. Zero means
    "ask the catalog", so an owner who never set the value follows their model
    while one who pinned a number keeps exactly the number they pinned.

    A pinned number is still bounded by what the model can give back beside a
    prompt. On a model whose output cap is its entire window, asking for that cap
    is asking for a request with no prompt in it; the provider does not clamp
    such a call, it refuses it. Truncating a long reply costs a second call,
    where a refused one costs the turn, so the request yields.
    """
    explicit = int(getattr(model, "max_tokens", 0) or 0) if model else 0
    name = model.model if model else ""
    requested = explicit if explicit > 0 else max_output_tokens_for(name)
    capped = _reply_reservation_tokens(requested, max_input_tokens_for(name))
    return capped if capped > 0 else requested


def resolve_max_input_tokens(settings: OmniSettings) -> int:
    """Largest prompt to build: the owner's pinned figure, else the catalog's.

    The setting it reads keeps the name ``memory.context_window_tokens``, because
    that one is a written configuration key and renaming it would break the files
    people already have. An owner pinning it is telling us what to accept as
    input, which is what this returns.
    """
    pinned = int(getattr(settings.memory, "context_window_tokens", 0) or 0)
    if pinned > 0:
        return pinned
    return infer_max_input_tokens(settings.model)


def _tier_fraction(settings: OmniSettings, attribute: str, default: float) -> float:
    """Where a tier sits inside the transcript ceiling, clamped to a usable band.

    Neither extreme is usable: near 0 a tier fires continuously and frees
    nothing, and at 1 it only fires once an *estimated* token count has already
    overrun the ceiling it was supposed to protect.
    """
    pct = float(getattr(settings.memory, attribute, default) or default)
    return min(max(pct, 0.1), 0.95)


def transcript_ceiling_tokens(settings: OmniSettings) -> int:
    """Prompt capacity available before context compaction/continuation.

    This is intentionally independent from a run's cumulative token quota. A
    context window bounds one provider request; ``max_total_tokens`` is an
    optional owner policy across the whole task. Coupling them made a large,
    productive task compact early and then fail solely because it needed more
    turns. Both compaction tiers now follow the real per-request constraint.

    The window has the reply taken out of it first. Where a window is shared, it
    bounds the request as a whole, so a transcript sized to fill it leaves the
    response nowhere to go: ``o3`` summarised at 180k of prompt and asked for
    32,768 tokens back, presenting 212,768 against a hard 200,000 limit, and the
    provider rejects that before compaction can help. The subtraction is the
    window's alone. The budget-derived ceiling is our own cumulative accounting
    across a whole run, not a limit anyone enforces on one request, and the
    replies it pays for are already counted against it.
    """
    window = resolve_max_input_tokens(settings)
    window -= _reply_reservation_tokens(
        resolve_max_output_tokens(settings.model), window
    )
    return max(0, window)


def _tier_threshold(settings: OmniSettings, attribute: str, default: float) -> int:
    """A compaction threshold in tokens: one fraction of the shared ceiling."""
    ceiling = transcript_ceiling_tokens(settings)
    if ceiling <= 0:
        return 0
    return int(ceiling * _tier_fraction(settings, attribute, default))


def session_compact_token_budget(settings: OmniSettings) -> int:
    """Transcript size that triggers the expensive tier (fold into a summary)."""
    return _tier_threshold(settings, "autocompact_pct", 0.90)


def microcompact_token_budget(settings: OmniSettings) -> int:
    """Transcript size that triggers the cheap tier (shrink old observations).

    The cheap tier is defined by standing below the expensive one — trim old
    observations first, pay for a summary only when trimming was not enough — so
    a configuration that would order them the other way round, or a ceiling
    small enough for rounding to collapse the gap, is corrected here rather than
    allowed to summarise on first contact.
    """
    session = session_compact_token_budget(settings)
    micro = _tier_threshold(settings, "microcompact_pct", 0.70)
    if 0 < session <= micro:
        return session - 1
    return micro
