# OmniScientist V2 skills

Portable `SKILL.md` packages — the research skill collection that ships with OmniScientist.

This directory is **independent of the CLI**. Each skill is a self-contained folder that follows
the common Claude Code / Codex / OpenClaw `SKILL.md` baseline and is discovered by `omni`.
Prompt-only instructions are portable; engine-backed capabilities require a portable runner or
Omni runtime. Skill content never imports CLI internals.

## Layout

```
skills/
├── <skill-name>/
│   ├── SKILL.md        # required: YAML frontmatter + Markdown instructions
│   ├── LICENSE.txt     # required: standalone redistribution terms
│   ├── NOTICE.md       # required: project/upstream attribution and modifications
│   ├── engine.py       # optional: Omni/HelixForge adapter for `python_engine` skills
│   └── scripts/run.py  # optional: portable runner for external agents
├── index.toml           # required: exact active built-in inventory
├── docs/
│   └── authoring.md    # how to write a skill
└── README.md
```

A skill is referenced everywhere by its **hyphen-case** folder name (e.g. `arxiv-fetch`) — the
cross-tool convention and a valid MCP / function-calling tool name.

## Catalogue

| skill | kind | description |
|-------|------|-------------|
| `arxiv-fetch` | python_engine | Fetch arXiv paper metadata by id/URL |
| `openalex-search` | python_engine | Search OpenAlex (Omni also records returned sources in its workspace) |
| `paper-review` | prompt_only | Produce a venue-aware author-facing pre-submission paper review |
| `review-response` | prompt_only | Draft or audit point-by-point journal revision correspondence |
| `scientific-figure` | python_engine | Unspecified architecture / flow / schematic (DOT/SVG/PNG) |
| `scientific-poster` | python_engine | Author, inspect, revise, preview, and approve evidence-grounded HTML scientific posters |
| `livefigure` | python_engine | Editable single-slide PPTX figure (requires VLM; not the default "draw a figure") |
| `research-ideation` | python_engine | Search literature and generate, critique, and refine structured research ideas |
| `research-pptx` | python_engine | Generate a complete multi-slide scientific deck from papers, text, outlines, or topics |

`skills/index.toml` is the single authoritative built-in activity inventory;
the table above mirrors it for readers. Build, discovery, export, and uninstall
all fail closed against that manifest instead of accepting every directory from
a dynamic scan.

## Artifact routing

| User intent | Capability | Provider | Output |
|---|---|---|---|
| Ordinary figure, architecture, or flowchart | `artifact.figure` | `scientific-figure` | DOT/SVG/PNG |
| Editable single-slide PPTX (needs VLM) | `figure.editable.pptx` | `livefigure` | Single-slide PPTX |
| Complete group-meeting, defense, seminar, or report deck | `slides.generate` | `research-pptx` | Multi-slide PPTX |
| Complete scientific or conference poster | `poster.scientific` | `scientific-poster` | HTML poster + preview/approval artifacts |
| Find and present papers on a topic | `literature.search` | `openalex-search` | Sources |
| Generate and pressure-test research directions | `research.ideation` | `research-ideation` | Report/answer/sources |
| Review a paper before submission | `review.paper` | `paper-review` | Venue-aligned Markdown review |
| Respond to received reviewer comments | `review.response` | `review-response` | Response letter/revision package |

`scientific-figure` is the default provider for an unspecified
`artifact.figure` (architecture, system, workflow). A configured VLM does
not change that default. `livefigure` runs only for `$livefigure` or
`figure.editable.pptx` and requires `omni config vlm`. A successful
livefigure PPTX can also pay `artifact.figure`. Host resolve does not parse
format words. `research-pptx` owns complete multi-slide decks.

### LiveFigure VLM configuration

LiveFigure is Omni-first because it needs a multimodal model and a protected API
key. Configure the owner-controlled values once:

```bash
omni config vlm \
  --endpoint https://provider.example/v1/chat/completions \
  --model vision-model \
  --api-key "$VLM_API_KEY" \
  --test
```

Omni stores the key in `<OMNI_HOME>/secrets.toml` (`0600` on POSIX) and injects
the VLM as a host service while it executes LiveFigure. The LiveFigure adapter
uses the injected generation port and does not copy credentials into the
process environment or MCP result. This is not hard isolation from trusted
in-process Python Skills: their engine receives Omni's `ExecContext`, including
host settings and services. Only install and trust executable Skill code you
have reviewed; a sanitized out-of-process context is required for a hard secret
boundary.

The standalone portable runner is a separate, explicit path. It reads:

- `OMNI_VLM_MODEL`
- `OMNI_VLM_ENDPOINT`
- `OMNI_VLM_API_KEY`

The initial portable contract is an OpenAI-compatible multimodal chat endpoint:
Bearer authentication, image/text input, and text output. Remote endpoints must
use HTTPS; plain HTTP is accepted only for loopback development. Do not put these
values in project-local configuration. At actual LiveFigure execution, Omni
returns one actionable configuration result without loading the skill engine;
inline calls become `needs_input`, while durable tasks retain the same action in
their result. DOT/SVG/PNG figures, Graphviz revisions, and runs without a VLM
continue to use `scientific-figure`.

The normal Omni install includes the active built-in Skills' Python runtimes.
Installation, `omni init`, and `omni update` prepare research-pptx's
lockfile-pinned Node modules in the Omni user cache. A copied LiveFigure runner
still needs `httpx` and `python-pptx`; a copied research-pptx runner installs its
local Node modules as described in `research-pptx/references/runtime-setup.md`.
Tasks never invoke package managers.

## Portability modes

The promotion principle for built-in and migrated skills is:

> Skills work without Omni; Omni adds orchestration, provenance, and persistence.

Each research skill should support the strongest mode the host agent can use:

1. **Copy-only mode** — copy a skill folder into Claude Code, Codex, or
   OpenClaw. The host agent reads `SKILL.md` and follows the procedure with its
   own tools. This is enough for prompt-only skills. Keep `LICENSE.txt` and
   `NOTICE.md` in the copied folder; they are part of the skill distribution.
2. **Portable runner mode** — for `python_engine` skills, run
   `python3 scripts/run.py --json '{...}'` from the skill directory. These
   runners are self-contained, use Python stdlib where possible, print
   structured JSON, write local artifacts/provenance when requested, and do not
   require Omni to be installed.
3. **Omni enhanced mode** — when OmniScientist or HelixForge is installed, the
   runtime reads `metadata.helixforge`, calls `engine.py`, persists tasks and
   workflows, stores artifacts, records provenance, and can expose every skill
   over MCP.

This split keeps `SKILL.md` as the cross-agent trigger/instruction surface,
`scripts/run.py` as the no-Omni executable bridge, and `engine.py` as the Omni
native adapter.

## Engines stay portable

`python_engine` skills keep their implementation in a local `engine.py` and reference it as
`metadata.helixforge.engine.module: engine`. The CLI executor loads that file *from the skill
folder* by path, so a skill can be copied out and still run. Engines reuse only OmniScientist's
small public runtime (for example `omni.research` for research helpers) — never CLI-private modules.

For external agents, do not ask Claude Code, Codex, or OpenClaw to import
`engine.py` directly. Use `scripts/run.py` for copy-only portability and Omni's
MCP server for full task/artifact/provenance integration.

## Research provenance contract

Research skills use a portable contract: the `SKILL.md` remains readable by
OmniScientist, Claude Code, Codex, and OpenClaw, while Omni-specific runtime
bindings live under `metadata.helixforge`.

Built-in research skills declare:

```yaml
metadata:
  helixforge:
    research_contract: portable_provenance_v1
```

When OmniScientist tools are available, skills should record sources, claims,
evidence, hypotheses, runs, and artifacts through the runtime (`cite_source`,
`record_claim`, `add_evidence`, `record_hypothesis`, `log_run`). In Claude Code,
Codex, OpenClaw, or any runtime without those tools, the same skill should not
fail; it should include a Markdown **Provenance** section and, when file writing
is available, write a `provenance.json` artifact with the same shape.

This keeps one skill portable while allowing OmniScientist to make the output
auditable, reproducible, and queryable.

## Runtime contracts in Omni enhanced mode

OmniScientist enforces the portable `allowed-tools` list for prompt-only skills,
and python-engine skills can declare the same workflow behavior in metadata.
Skill authors can add Omni-only execution and workflow policy under
`metadata.helixforge`:

```yaml
execution:
  max_iterations: 8
  max_tool_calls: 16
  tool_limits:
    search_corpus: 4
workflow:
  failure_policy: continue_with_partial
  allow_failed_dependencies: true
```

Cap acquisition and execution tools only. A quota on a tool that emits the
deliverable — `write_file`, `cite_source`, `package_artifact` and friends — caps
the work itself and is ignored at load time; see `skills/docs/authoring.md`.

This is the harness layer that keeps skills powerful but bounded. Prompt-only
skills that hit a runtime boundary return a partial/degraded result when
possible; recoverable failures remain visible in workflow steps, but downstream
skills can continue with `workflow_results` and `dependency_failures`. The
parent task may finish as `completed_with_warnings` instead of losing the whole
research run.

## Built-in research skill policy

Omni's own research skills are stricter than generic imported skills because
they are composed into long-running scientific workflows. The design goal is:

> Keep baseline compatibility lightweight and make research workflows rigorous.

For built-in skills that declare
`metadata.helixforge.research_contract: portable_provenance_v1`:

- keep the cross-agent `SKILL.md` body readable by Claude Code, Codex, and
  OpenClaw;
- declare `input_schema` and `output_schema` so the planner and MCP surface have
  a concrete but open contract;
- declare `workflow.failure_policy` and `workflow.failure_types` so failures are
  visible, recoverable, and explainable;
- for prompt-only skills, declare `execution` budgets and `tool_limits`, and use
  `allowed-tools` as the true tool boundary;
- return partial outputs, artifacts, sources, and `research` ids whenever
  possible, even when the step cannot complete fully.

For built-in research skills, `output_schema` declares the **minimum stable
interface**, not every field a skill may return. Keep the object open and use
lifecycle `status: ok | partial | error`. Put domain-specific outcomes in
`outcome.code` (`empty_results`, `not_found`, `answered`, `indexed`,
`renderer_missing`, and so on) so workflow scheduling can stay generic while
downstream skills still see the precise reason. Common optional fields are
`summary`, `warning`, `recoverable`, `blocking`, `artifacts`, `sources`,
`research`, `error`, and `error_info`.

Imported ecosystem skills are not required to carry this full contract. They can
remain plain `SKILL.md` instructions and still work in Omni as prompt-only
skills; teams can add Omni metadata later when a third-party skill becomes part
of a durable research workflow.

## Using these skills

Use the Shell form in a terminal, or the slash form after starting the interactive `omni` REPL:

| Action | Shell | Inside `omni` |
|---|---|---|
| List managed skills | `omni skills list` | `/skills list` |
| Inspect a skill | `omni skills info arxiv-fetch` | `/skills info arxiv-fetch` |
| Import a skill | `omni skills add <local-path\|tool:name\|git-url>` | `/skills add <local-path\|tool:name\|git-url>` |
| Trust after review | `omni skills trust my-skill --yes` | `/skills trust my-skill --yes` |
| Export built-ins | `omni skills export` | `/skills export` |

`$skill-name` is an explicit reliability override, not the only invocation
path. Trusted imported skills enter Omni's model-facing catalogue: contracted
skills can be selected through capabilities, while plain prompt-only skills can
be discovered by description in the ReAct loop. Use `$skill-name` when exact
provider selection is required. Plain skills need Omni metadata only when they
must become deterministic required workflow providers or use Omni's structured
artifact/provenance/runtime contracts.

Local paths may be a skill directory or Markdown file; named external sources
include `claude:`, `codex:`, `agents:`, and `openclaw:`. Git URLs may select a
subdirectory but currently use the default branch. See
[`docs/authoring.md`](docs/authoring.md) to write your own, and
[`../cli/docs/skills.md`](../cli/docs/skills.md) for the complete install,
selection, and invocation lifecycle. The full interop story is in
[`../cli/docs/compatibility.md`](../cli/docs/compatibility.md).
