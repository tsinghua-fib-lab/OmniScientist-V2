# Authoring skills

A distributable skill is a folder containing `SKILL.md`, `LICENSE.txt`, and `NOTICE.md`.
`SKILL.md` uses YAML frontmatter and a Markdown body. The format is
**Claude Code / Codex / OpenClaw compatible**: only `name` and `description` are required, and
everything under `metadata:` is ignored by Claude Code / Codex — so a single file works in all the
tools. Use **hyphen-case** names (e.g. `arxiv-fetch`): that's the cross-tool convention and a valid
MCP / function-calling tool name.

Command notation in this guide uses both supported surfaces: `omni skills ...` in a normal shell,
and `/skills ...` after starting the interactive `omni` REPL.

## Where skills live (discovery order)

High → low priority (higher overrides same-named lower). Project roots are walked up from the CWD
to the repo root; user roots are the on-disk locations each external tool reads.

**Default sources** (what `omni skills list` or `/skills list` shows) — OmniScientist-managed only:

1. `<repo>/.omni/skills/` — project, OmniScientist
2. `~/.omni/skills/` — user, OmniScientist (also where `skills add` imports to)
3. bundled built-ins (this repo's `skills/`)

**Opt-in sources** (added by `omni skills list --all` or `/skills list --all`) — the other tools' libraries:

4. `<repo>/.claude/skills/`, `<repo>/.agents/skills/` — project, Claude Code / Codex / OpenClaw
5. `~/.claude/skills/` — user, Claude Code
6. `~/.agents/skills/` — user, Codex (current) / OpenClaw
7. `~/.codex/skills/` — user, Codex (`$CODEX_HOME`)
8. `~/.openclaw/skills/` — user, OpenClaw

Configure via `skills.sources` and disable individual skills via `skills.disabled`. Discovery is
recursive, so nested `SKILL.md` layouts work. The built-ins ship under the repo's `skills/` dir and
can be **exported** to roots 5–8 with `omni skills export` or `/skills export`, or an external skill
can be **imported** into omni (source 2) with `omni skills add <tool>:<name>` or
`/skills add <tool>:<name>` (see
[../../cli/docs/compatibility.md](../../cli/docs/compatibility.md)).

## Minimal (prompt-only) skill — works in CC / Codex / OpenClaw too

```markdown
---
name: lit-gap-finder
description: Find the research gap for a topic by surveying recent work and contrasting approaches.
allowed-tools: [web_fetch, arxiv-fetch]
---

# lit-gap-finder

1. Survey recent papers on the topic (use arxiv-fetch / web_fetch).
2. Cluster the approaches; identify what nobody has done.
3. Output the gap + 2 concrete directions, each with a citation.
```

Put this at `~/.claude/skills/lit-gap-finder/SKILL.md` and both OmniScientist and Claude Code can
use it. Import and trust it to make it part of Omni's managed catalogue. Omni's
ReAct agent can discover it by description through `find_skill` / `use_skill`;
`$lit-gap-finder ...` forces exact selection.

For a skill that will be published or copied outside its source repository, also include:

```text
LICENSE.txt     # complete license terms, including inherited third-party terms
NOTICE.md       # copyright, upstream URL/revision, and a concise modification statement
```

OmniScientist's bundled skills use Apache-2.0. Adapted skills must retain every upstream notice and
license condition; do not replace an inherited MIT or Apache notice with only the project's own
license. `omni skills export` treats these legal files as part of the skill identity and refreshes
an Omni-owned export when they change or are missing.

## Three portability modes

The target user experience for built-in and migrated skills is:

> Skills work without Omni; Omni adds orchestration, provenance, and persistence.

Design each skill so it degrades gracefully across three modes:

1. **Copy-only mode**: the skill folder can be copied into Claude Code, Codex,
   or OpenClaw. The host agent reads `SKILL.md` and follows the instructions
   with its normal tools. Prompt-only skills usually need nothing more.
2. **Portable runner mode**: `python_engine` skills include a
   self-contained `scripts/run.py` that accepts `--json` or JSON on stdin,
   prints structured JSON, writes local artifacts/provenance when requested,
   and does not import Omni. External agents should call this script instead of
   importing `engine.py`.
3. **Omni enhanced mode**: OmniScientist/HelixForge reads
   `metadata.helixforge`, executes `engine.py`, persists tasks/workflows,
   records artifacts/provenance, and exposes the skill through MCP when enabled.

Use this layering:

```text
SKILL.md        # cross-agent trigger and instructions
scripts/run.py  # optional no-Omni executable bridge for external agents
engine.py       # Omni native adapter with ExecContext/artifacts/provenance
```

## Python-engine skill (fast, structured, synchronous)

```markdown
---
name: arxiv-fetch
description: Fetch metadata for an arXiv paper by id or URL.
metadata:
  helixforge:
    delivery_mode: sync_tool          # available directly in the ReAct loop
    kind: python_engine
    engine: { module: engine, class: ArxivFetchEngine, method: execute }
    input_schema:
      type: object
      properties: { identifier: { type: string } }
      required: [identifier]
---
# arxiv-fetch
...
```

The skill `name` is hyphen-case. `engine.module: engine` is **loaded from the skill's own folder**
(`<skill>/engine.py`) by path — so the skill stays self-contained and portable; the executor falls
back to a normal dotted import only for engines shipped as installed packages. Engines may import
only OmniScientist's documented public runtime (for example `omni.research` and
`omni.research.*`) and must not import CLI-private internals.

The engine class:

```python
class ArxivFetchEngine:
    @staticmethod
    def validate_params(*, arguments=None, input_data=None):
        if not (arguments or {}).get("identifier"):
            return {"error": "identifier required"}   # returned to the model
        return None

    async def execute(self, **input_data):            # may also accept progress_callback=...
        ...
        return {"status": "ok", "title": ..., "summary": ...}   # JSON-serialisable dict
```

The engine instance receives `self.ctx` (an `ExecContext`) before `execute`, giving access to
`ctx.artifacts` (file store), `ctx.paths`, `ctx.session_id`, etc.

For a `python_engine` skill intended for external adoption, also provide:

```text
scripts/run.py
```

The portable runner should:

- parse `--json` and stdin JSON;
- support `--self-test` without network access;
- print a JSON object with at least `status`, `skill`, `summary` or `error`,
  `artifacts`, and `provenance` when applicable;
- return a structured `status: "error"` result instead of crashing for bad
  input, network failure, or missing optional renderers;
- avoid `import omni` / `from omni` so copying a single skill folder still
  works in Claude Code, Codex, and OpenClaw.

## Portable research provenance

For research skills, keep the prompt portable and put OmniScientist-only behavior
under `metadata.helixforge`. Claude Code, Codex, and OpenClaw should still be
able to read the same `SKILL.md` and follow the instructions even if they cannot
load Omni's `python_engine`.

Use this marker for built-in or migrated research skills:

```yaml
metadata:
  helixforge:
    tier: research
    research_contract: portable_provenance_v1
```

The body should describe the fallback behavior:

- In OmniScientist, when tools exist, use `cite_source`, `record_claim`,
  `add_evidence`, `record_hypothesis`, and/or `log_run`, then return ids under
  `research.source_ids`, `research.claim_ids`, `research.evidence_ids`,
  `research.hypothesis_ids`, and `research.run_id`.
- In Claude Code, Codex, OpenClaw, or any runtime without those tools, do not
  fail. Include a Markdown **Provenance** section and, when file writing is
  available, write `provenance.json` with the same shape plus artifact paths.
- Never invent provenance ids. Use ids returned by tools; otherwise use
  human-readable source metadata such as arXiv id, DOI, URL, title, run command,
  and artifact path.

Python engines may import OmniScientist's small public runtime and write ROM
records or library entries directly. External agents should not import those engines directly; use
the portable `scripts/run.py` bridge for no-Omni execution, or install
OmniScientist as an MCP server with `omni mcp serve` for full task/artifact/
provenance integration.

## CLI-exec skill (any language)

```markdown
---
name: my-tool
description: ...
metadata:
  helixforge:
    kind: cli_exec
    exec: { command: python, args: ["scripts/run.py"], stdout_format: json, timeout_seconds: 120 }
  openclaw:
    requires: { bins: [python], env: [MY_API_KEY] }
---
```

OmniScientist runs the command, writes the input dict as JSON to **stdin**, and parses **stdout**
(`json` → dict, `text` → `{stdout: ...}`).

## Async (heavy) skills

Set `delivery_mode: async_task`. Such skills are not exposed as direct ReAct tool names, but they
are still synchronously usable when the caller chooses to wait:

- `run_skill` with `mode=auto` runs them through the durable task runtime and waits in foreground
  when the CLI is not detached; `mode=background` returns a task id immediately.
- `run_workflow` can include them as ordered workflow steps, so upstream results can feed downstream
  skills through `workflow_results` and `depends_on_results`.

Completion fires a notification (CLI/IM). Optional `notification.display_label` /
`notification.title_field` shape the message.

Workflow-friendly skills should always return a JSON-serialisable dict with:

- `status: "ok" | "partial" | "error"` as the lifecycle state only;
- `outcome.code` for domain-specific results such as `empty_results`,
  `not_found`, `answered`, `indexed`, or `renderer_missing`;
- `summary` for human task views;
- `warning`, `recoverable`, and `blocking` when the result is degraded or
  recoverable;
- `error` plus optional `error_info` on failures;
- `artifacts` / `*_uri` for generated files;
- `research` ids (`source_ids`, `claim_ids`, `evidence_ids`, `run_id`) when provenance was recorded.

Do not crash for predictable failures such as bad input, renderer absence,
network timeouts, empty evidence, or tool limits. Return `status: "partial"`
when the step produced a useful but incomplete result, and `status: "error"`
when it could not produce a usable result. In both cases, include
`recoverable: true` and `blocking: false` when downstream workflow steps may
continue honestly. Prompt-only skills that hit runtime boundaries should let
Omni produce a `status: "partial"` result from the gathered observations, so
the parent workflow can persist a degraded but recoverable state.

## Execution and workflow contracts

OmniScientist treats `SKILL.md` as a runnable contract, not just a prompt. Keep
the cross-agent instructions portable, then put Omni-only runtime policy under
`metadata.helixforge`.

This contract is a **progressive enhancement**, not a gate for ecosystem
compatibility. A Claude Code, Codex, or OpenClaw skill with only `name`,
`description`, and Markdown instructions can still be imported and used as a
prompt-only skill. Add the fields below when the skill is part of Omni's built-in
research surface or will be composed into durable workflows.

For Omni-maintained research skills, the expected contract is:

- `input_schema`: required fields and aliases the workflow planner can validate.
- `output_schema`: the minimal stable output interface. It should declare
  lifecycle `status`, `outcome`, text/summary, artifacts, sources,
  `research` ids, warning/recoverability fields, and `error_info`.
- `allowed-tools`: the actual tool surface for prompt-only skills.
- `execution`: per-skill budgets (`max_iterations`, `max_tool_calls`,
  `max_seconds`, `tool_limits`).
- `workflow`: failure policy, failed-dependency behavior, and named
  `failure_types`.

The intended behavior is not "skills never fail"; it is "skills fail visibly,
persist partial state, and let the workflow recover when recovery is honest."

`output_schema` is not a closed whitelist. Do not set
`additionalProperties: false` for Omni research skills. The stable fields are
what downstream workflow steps can rely on; each skill can still return richer
domain fields such as `matches`, `claims`, `figure_path`, `bibtex`,
`confidence`, or renderer logs.

Recommended minimum shape:

```yaml
output_schema:
  type: object
  properties:
    status: {type: string, enum: ["ok", "partial", "error"]}
    outcome: {type: object}
    summary: {type: string}
    warning: {type: string}
    recoverable: {type: boolean}
    blocking: {type: boolean}
    artifacts: {type: array}
    sources: {type: array}
    research: {type: object}
    error: {type: string}
    error_info: {type: object}
  required: ["status"]
```

Keep domain outcomes out of `status`. For example, use
`{"status": "partial", "outcome": {"code": "empty_results"}}` rather than
`{"status": "empty"}`. This keeps workflow scheduling simple while preserving
the exact domain reason.

For prompt-only skills, use `allowed-tools` to define the actual tool surface.
Omni enforces this list before the sub-agent sees tools. Use `execution` for
bounded runs:

```yaml
allowed-tools: [search_corpus, cite_source, record_claim, add_evidence]
metadata:
  helixforge:
    kind: prompt_only
    execution:
      max_iterations: 4
      max_tool_calls: 16
      tool_limits:
        search_corpus: 4
```

This mirrors Claude Code / Codex style tool scoping while adding deterministic
runtime limits. If a model tries to over-search, the tool call returns a budget
message and the skill should synthesize from already retrieved evidence. If it
still hits timeout/iteration/tool-call limits, Omni makes one no-tool salvage
pass and records a partial result rather than treating the entire workflow as
lost.

For workflow composition, distinguish hard dependencies from soft/degraded
dependencies:

```yaml
metadata:
  helixforge:
    workflow:
      failure_policy: continue_with_partial
      allow_failed_dependencies: true
```

- `failure_policy: continue_with_partial` means this skill can fail
  recoverably. The step is recorded as `failed` when it returns a recoverable
  error, or `degraded` when it returns a partial result. The parent workflow may
  finish as `completed_with_warnings` instead of failing the whole task.
- `allow_failed_dependencies: true` lets a downstream skill run even when an
  upstream dependency failed. The failed upstream result is still available in
  `workflow_results`, `depends_on_results`, and `dependency_failures` so the
  downstream output can mark evidence gaps.
- Omit these fields for hard requirements. A failed hard dependency still skips
  downstream steps and fails the workflow.

Use this for exploratory research chains: literature search may succeed, grounded
QA may hit a tool limit, and writing/figures can still produce a cautious draft
with clear warnings. Do not use it for safety-critical gates, destructive
operations, or steps whose absence makes downstream output misleading.

## How a skill becomes selectable

OmniScientist's semantic planner proposes capabilities and deliverables. The runtime resolves those
slots to providers using the contracts you write:

- `capabilities`, `deliverables`, `role`, and `contract_level` define what the provider may satisfy
  and whether automatic planning may select it.
- `description` and `trigger.when_to_use` explain the provider to the semantic planner and to
  `find_skill` users.
- `trigger.phrases` are optional catalog-search aliases and model-facing examples. They are not
  substring rules for automatic intent routing, so authors do not need language-specific phrase
  lists.

```yaml
metadata:
  helixforge:
    trigger:
      phrases: ["write a paper", "full paper", "NeurIPS paper"]
      when_to_use: "Use when the user wants a complete academic paper from a topic."
```

So a skill is reachable several ways:

- as a **direct tool** for sync engine/exec skills;
- through **semantic capability selection** from the contracted catalog;
- through **model-side catalogue discovery** (`find_skill` → `use_skill`);
- through **explicit selection** (`$skill-name ...`);
- through **single-skill scheduling** (`run_skill`, foreground or background);
- as a step in a **model-planned workflow** (`run_workflow`).

For workflow planning, OmniScientist gives the semantic planner a compact catalog of currently
loaded contracts. The runtime resolves requested capabilities and deliverables through registry
metadata, validates the selected provider, and locks the plan before execution. Trigger phrases are
optional explicit aliases; they are not a substitute for semantic planning.

Plain AgentSkills/Claude Code skills without an Omni contract remain eligible
for description-driven ReAct discovery and explicit invocation. That is a
portable, best-effort model-selection lane rather than a deterministic
capability guarantee. A contract-less skill cannot satisfy a required automatic
workflow step; add `metadata.helixforge` only when that stronger role is needed.

Workflow inputs are validated from each skill's `metadata.helixforge.input_schema`. Before creating
a parent workflow task, OmniScientist applies a schema-driven adapter for common field names (for
example `question`, `description`, `query`, `topic`, or `identifier` into a required `input` field
when that is what the skill schema declares). If required fields are still missing, `run_workflow`
returns `status: "needs_input"` so the agent asks the user for the missing information instead of
creating a half-failed task. Skill authors should therefore keep `required` precise: use it for
truly mandatory inputs, not optional hints that can be inferred from upstream workflow results.

## Importing skills (incl. from git)

| Action | Shell | Inside `omni` |
|---|---|---|
| Import a local directory | `omni skills add ~/work/my-skill` | `/skills add ~/work/my-skill` |
| Import a local Markdown file | `omni skills add ~/work/my-skill/SKILL.md` | `/skills add ~/work/my-skill/SKILL.md` |
| Import from Claude Code | `omni skills add claude:my-skill` | `/skills add claude:my-skill` |
| Import from Codex | `omni skills add codex:playwright` | `/skills add codex:playwright` |
| Import from shared Agent Skills | `omni skills add agents:my-skill` | `/skills add agents:my-skill` |
| Import from OpenClaw | `omni skills add openclaw:my-skill` | `/skills add openclaw:my-skill` |
| Trust after review | `omni skills trust playwright --yes` | `/skills trust playwright --yes` |
| Import a Git repository | `omni skills add https://github.com/org/repo.git` | `/skills add https://github.com/org/repo.git` |
| Import one Git sub-path | `omni skills add https://github.com/org/repo#skills/example` | `/skills add https://github.com/org/repo#skills/example` |

Imports land in `~/.omni/skills/<name>` (named from each `SKILL.md`) and remain quarantined until
the owner reviews the source, license, and executable files and runs `omni skills trust <name> --yes`
or `/skills trust <name> --yes`. Direct Git imports shallow-clone the default
branch; use a local checkout for a non-default or pinned revision. Raw HTTP
or HTTPS files, archives, and repository `/tree/...` pages are not accepted. The
complete source and limit matrix is in
[`../../cli/docs/skills.md`](../../cli/docs/skills.md#supported-skills-add-sources).

## Tips

- Write a crisp `description`, `metadata.helixforge.trigger.when_to_use`, capability list, and input/
  output contract. Add `trigger.phrases` only as optional catalog aliases or examples; do not depend
  on them for automatic routing.
- Cite sources (arXiv id / DOI / URL) in research outputs.
- Keep engine outputs JSON-serialisable; put large content in an artifact and return its `*_uri`.
- Validate inputs in `validate_params` to give the model actionable feedback.

Inspect anything with `omni skills info <name>` or `/skills info <name>`; inspect discovery roots
with `omni skills sources` or `/skills sources`. The user-facing lifecycle and
cross-agent comparison are in
[../../cli/docs/skills.md](../../cli/docs/skills.md).
