# Memory and storage

This document describes the as-built memory model. Implementation details live
under `cli/src/omni/memory`, `cli/src/omni/runtime`, and
`cli/src/omni/storage`. See also [architecture.md](architecture.md) and
[commands.md](commands.md).

## Design goals

OmniScientist keeps all durable state in SQLite and the filesystem. It does not
require Redis, a vector database, or a separate memory service.

The design has three surfaces:

1. **Working continuity** stores sessions, messages, runs, tasks, focus, and
   artifact references. It is exact, local, and deterministic.
2. **Long-term memory** stores distilled semantic and episodic entries. Entries
   can decay, be pinned, and carry provenance references.
3. **Curated files** expose human-editable project rules, preferences, and lab
   notes through `AGENTS.md`, `CLAUDE.md`, `MEMORY.md`, `profile.md`, and
   `NOTEBOOK.md`.

SQLite is the structured source of truth. Markdown is the reviewable human
interface. Generated binary artifacts remain files and are referenced from the
database.

## Storage responsibilities

| Store | Content | Lifecycle |
|---|---|---|
| `conversation_messages` | User and assistant messages, task-result write-back, compaction bridges | Session-scoped and compactable |
| `tasks` and `task_events` | User requests, Child Tasks, intent plans, audit events, settled status, presentation | Durable |
| `workflow_runs`, `workflow_steps`, `workflow_checkpoints` | Model-submitted multi-step runs, stable logical nodes, dependency and recovery state | Durable; cascades with the owning task |
| `subtasks` | Retryable Skill Execution attempts, structured results, trace references | Durable; cascades with its task/workflow step |
| `artifacts` | Artifact URI, path, kind, owning session/subtask, metadata | Durable |
| `session_focus` | Active task, source, and artifact target for follow-up turns | Short-horizon, append-only |
| `memory_entries` (workspace) | Session (M1), task (M2), artifact (M5), and project-scope findings (M4) | Durable with decay and deduplication |
| `memory_entries` (`<OMNI_HOME>/memory.sqlite3`) | Machine-global owner identity: user-scope preferences, `user_profile`, episodic (M3) | Durable; shared across workspaces/CLIs/channels |
| Curated Markdown | Project instructions, user preferences, profile, notebook | Human-maintained and optionally versioned |
| `library.jsonl` | Literature records used by citation commands | Durable |

## Memory layers

The layer field partitions one `memory_entries` table. It does not create five
independent storage engines.

| Layer | Purpose | Typical scope |
|---|---|---|
| M1 | Session summary and continuity | session |
| M2 | Task result summary linked to a Task, WorkflowRun, WorkflowStep, or Skill Execution | session/task |
| M3 | Replayable research episode, including negative results and idea evolution | run/task |
| M4 | Findings, decisions, preferences, and project knowledge | project/user |
| M5 | Searchable artifact reference | artifact/run |

## Identity and channel isolation

Every long-term entry has a `principal`:

- local CLI activity (and MCP) uses `local` — the machine owner;
- IM identities map according to `memory.channel_identity`.

`memory.channel_identity` selects how an *authorized* IM identity is mapped
(only allow-listed / paired messages ever reach the agent):

| Mode | Mapping | Use |
|---|---|---|
| `owner` (default) | every authorized IM identity → `local` | personal assistant: what you tell the bot on Feishu is recalled in the CLI, and vice-versa. Pairing binds the identity to the owner. |
| `per_peer` | each IM identity → `<channel>:<external-key>` | shared / multi-user bot: strict isolation, zero cross-talk. |

Recall is always restricted to the active principal plus the `local` owner
baseline: in `per_peer` mode one IM user can never recall another user's memory,
nor can the owner see a peer's memory. The mapping is the single source of truth
shared by the foreground path (`orchestrator`) and the background task runtime
(`subtask_runtime`), so async task results never land under the wrong principal.

This is an identity boundary for a local agent, not a multi-tenant governance
system.

## Machine-global memory (cross-workspace / cross-CLI / cross-channel)

Workspaces are path-keyed, so a per-workspace store alone means the owner's
identity is forgotten the moment they `cd` elsewhere. To make the owner's "who
you are" follow them, durable *identity* memory routes to one machine-global
store at `<OMNI_HOME>/memory.sqlite3`, shared by every CLI invocation, workspace,
and the `omni serve` daemon on the machine.

- **Routing.** Only cross-workspace identity rows go global: `user`-scope
  entries, the synthesized `user_profile`, and episodic (M3) summaries.
  Everything else — session dialogue (M1), task results (M2), artifact refs
  (M5), and project-scope findings (M4) — stays workspace-bound.
- **Dual-read.** Recall unions a bounded candidate set from *both* stores
  (dedup by id) before scoring, so a preference set in another project is still
  a candidate here. The memory graph is likewise traversed across both stores
  and the boosts merged (`_graph_spread`).
- **Gray switch.** `memory.global_store` (default on) gates the whole feature;
  `off` reverts to exactly the legacy per-workspace behaviour with no global
  handle.
- **Backfill.** On first use with the store enabled, a one-time,
  marker-guarded pass (`migrate_identity_to_global`) copies existing
  workspace-scoped owner identity rows into the global store; it is idempotent.
- **Always-injected digest.** `<OMNI_HOME>/memories/memory_summary.md` is a
  small, token-bounded (`memory.summary_token_budget`) digest of the owner's
  durable global preferences, rewritten only when its content changes and
  injected first among the personal blocks at session start.
- **Concurrency.** The global store runs in SQLite WAL mode with a busy
  timeout; heavy read-modify-write consolidation is additionally serialized
  across processes by an advisory file lock (`memory/locks.py`), so two
  concurrent `omni` processes cannot corrupt it or duplicate work.
- **Settings protection.** `memory.global_store` and `memory.channel_identity`
  are owner-controlled: a repo-local project config cannot override them.

## Context assembly

Memory is compiled before semantic planning. The compiler uses the current
intent, target, active focus, principal, and token budget to select context from
four scopes:

1. active session focus and explicit attachments;
2. current session history and task results;
3. project memory and curated files;
4. principal-scoped long-term memory.

Current focus has higher priority than older semantic memory. A phrase such as
"this figure" binds to the active artifact before broad recall is considered.
Ambiguous targets are not guessed; the planner requests clarification.

Recall follows a bounded search-then-get pattern. The model sees compact
summaries first and requests full records only when needed. Candidate limits,
scope filters, and token budgets prevent a large store from flooding a prompt.

## Long conversations

Long conversations remain usable through four mechanisms:

1. Completed task summaries and artifact references are written back to the
   session transcript.
2. Older transcript regions are folded into a compaction bridge when the
   configured fraction of the model context window is reached.
3. Older tool observations inside a long ReAct turn are micro-compacted before
   transcript folding.
4. Durable facts are flushed before compaction, so prompt reduction does not
   discard project knowledge.

`/compact` triggers compaction manually. `/context` reports visible, folded,
and injected context sizes.

## Long-term maintenance

At session end, eligible turns can be distilled into M3 and M4 entries. Failed,
partial, tool-budget-exhausted, and pure connector turns are excluded from
finding extraction so operational failures do not become research facts.

Maintenance performs:

- importance decay for non-pinned empirical entries;
- near-duplicate consolidation;
- bounded graph links between related entries (per store; recall spreads across both);
- usage-aware ranking through recall counts;
- citation-aware ranking: an entry anchored to a real source/claim/run/artifact
  (`payload_ref`) gets a small, fixed lift over an equally-similar but ungrounded
  recollection — the provenance moat, never overriding similarity;
- local-owner profile synthesis into `<OMNI_HOME>/profile.md`;
- a rewrite of the always-injected global digest (`memory_summary.md`) when its
  content changed.

Global-store maintenance (decay, profile rebuild, digest refresh) runs inside
the cross-process advisory lock so concurrent processes don't race. Pinned
entries do not decay. Human edits to curated files remain authoritative inputs
to later profile synthesis.

## Research provenance

Remembered content is not automatically treated as true. Research memories may
reference a source, claim, evidence edge, run, or artifact. The `omni verify`
honesty pass can therefore distinguish:

- grounded findings with recorded evidence;
- contextual summaries without a complete evidence chain;
- unsupported or stale findings that need review.

The final synthesis layer receives these evidence levels and must not present a
degraded memory as a verified conclusion.

## Commands

Common operations:

```text
omni memory help
omni memory list
omni memory search <query>
omni memory detail <id>
omni memory add <text>
omni memory pin <id>
omni memory pin <id> --off
omni memory rm <id> [--force]
omni memory clear [filters] --yes
omni memory sync
omni memory profile
omni memory notebook
omni memory graph
omni memory link
omni memory path
omni resume --last
omni replay <session>
```

The same commands are available in the REPL with a leading slash. Use
`/task attach <id>` to attach a completed task result to the active session.

## Testing

The persistent-memory contract is guarded by an offline, deterministic
benchmark scored on three metrics — **injection hit + citation hit + zero
leakage** — across the cross-session, cross-workspace, cross-channel,
isolation, concurrency, and offline dimensions. Run it with `omni eval
--memory` (add `--json` for CI, `--gate` to exit non-zero on any regression);
it lives in CI as `tests/eval/test_memory_benchmark.py` and runs against a
throwaway data home so it never touches the owner's real store.

## Invariants

- Local-first SQLite and filesystem persistence.
- Principal isolation applies to every recall path; `per_peer` mode has zero
  cross-talk between peers or with the owner.
- Durable owner identity is machine-global and follows the owner across
  workspaces, CLIs, and (in `owner` mode) channels; `global_store=off` reverts
  to per-workspace behaviour.
- Compaction flushes durable facts before folding history.
- Automatic file maintenance never overwrites arbitrary handwritten prose.
- Context injection is bounded (including the global digest) and reports provenance.
- Grounded memory outranks equally-similar ungrounded memory.
- Concurrent processes cannot corrupt the shared global store (WAL + advisory lock).
- Session focus outranks broad historical recall for referential follow-ups.
- A cross-session, principal-scoped recent-activity digest (recent deliverables +
  their artifacts) is injected into the planner so references to prior work
  ("regenerate the last figure") resolve without re-asking; a clarifying question
  that points at the agent's own output is downgraded to a tool-enabled lookup turn.
- Offline operation degrades to deterministic keyword recall rather than
  disabling memory; keyword recall is CJK-aware (character-bigram tokenisation)
  so Chinese/Japanese/Korean queries do not silently lose lexical overlap.
