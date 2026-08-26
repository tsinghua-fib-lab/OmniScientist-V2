# User Walkthrough Catalog

This file is the **source of truth** for later user-perspective walkthroughs
and functional validation. Do not add a second catalog. Score every run against
the cases, the output-format families, and the coverage inventory below.

The inventory maps **command groups**, **feature areas**, **built-in skills**,
and **one third-party skill add → trust → invoke → remove** loop. It is an
operator index, not an execution proof of every leaf action. Aliases are not
second cases.
Hidden installer hooks (`upgrade`, `terminal-setup`, `_record-install`,
`_converge-install`) stay out of the user catalog.

Copy-pasteable **user-facing** cases for the current OmniScientist command and
agent surface. Every case maps to a command, native ReAct tool, built-in skill,
or durable store that exists in this tree. The catalog does not invent
`omni project migrate`, `omni skills install` / `uninstall`, or a natural-language
allow/deny parser.

The specification table below is the operator index: one functional point,
at least two case IDs, and what the user is checking. Detailed inputs live
in the sections after it.

Use a dedicated named project so walkthrough store rows stay off real work.
From a source checkout, put `cli/src` first on `PYTHONPATH` so the command
hits this tree rather than a previously installed wheel (do not run
`omni update --local` just to pick up local edits). Publish walkthrough
files with `--out outputs_walkthrough` (gitignored). Do **not** use
`--out .`: that writes `<title>_<task8>/` next to `cli/` and dirties
`git status`. Real dogfood stays in `outputs/`.

```bash
PYTHONPATH=cli/src .venv/bin/omni --project walkthrough-aug24 --trust --out outputs_walkthrough
```

The offline `mock` provider is enough for CLI help, list, status, and
`offline_mock_smoke`. Named literature retrieval, surveys, figures, decks, and
stacked prompts need a configured model and usually the network.

VLM-driven built-in skills (`livefigure`, `paper-review`, `scientific-poster`)
branch on whether a vision model is configured. Probe first; do not guess.
See [VLM-driven skills](#vlm-driven-skills).

## How to read a case

| Column | Meaning |
|---|---|
| ID | Stable handle for comparison |
| Need | `offline` / `model` / `network` / `serve` |
| Input | Exact user text or shell command |
| Pass | Observable contract, not a vibe |

Pass/fail on four columns: **ID**, **tool or subcommand that actually ran**,
**whether a second assistant line appeared**, **whether the user-visible
output matches the family in [User-visible output format](#user-visible-output-format)**.

`A-LIT-01` is the release-blocker regression for named `search_literature`.
`rows` is a per-connector cap; the deduped union may exceed that number.
If the host spills `source_ids` to disk, `read_file` on that path must succeed
and a jail denial must not be recorded as `succeeded` / `result_success=true`.
`X7` / `X8` must stay a live sequence against tool results, not one
`SINGLE_SKILL_TASK`. `X8-02` is an intentional contradiction: a source-id-only
scope must still block `write_file` / `run_skill` / `spawn_subagents`. Naming
the retrieve tool plus produce, without that scope, keeps those debts unpaid
and still does not open write on the retrieve window.

Long-horizon work has two shapes. **Campaigns** (`W-01`–`W-04`) cross sessions
and durable stores. **One task, many executions** (`E-01`–`E-04`) is one user
request that becomes a Task with more than one Skill Execution (often a
WorkflowRun plus WorkflowSteps). If one execution has a problem, that row can
be `degraded` / `failed` / `needs_input` while siblings that delivered stay;
the parent Task must not be narrated as `succeeded`. Omni has no product type
named "Great Wall"; that label is only the user-facing name for the campaign
shape.

## User-visible output format

Every Pass column below is scored against this family table. Do not invent a
second answer shape. Host settlement does not grade prose quality; it reads
durable rows (`sources`, artifacts, children) and decides `succeeded` /
`degraded` / `failed` / `needs_input` / `pending`.

The durable object graph the user can inspect:

```text
Task (one user request)
├── WorkflowRun (one run_workflow call)
│   ├── WorkflowStep (stable logical node)
│   │   ├── Skill Execution attempt 1
│   │   └── Skill Execution attempt 2 (retry_of attempt 1)
│   └── WorkflowStep (delegation) ──> Child Task
├── direct Skill Execution
└── direct Child Task
```

`omni task show <id>` renders that graph: plan / settlement / remaining,
workflow runs, workflow steps, skill executions, child tasks, activity.
`omni task subtask <task>` lists only Skill Executions. `omni task step`
needs `{workflow_run_id} {step_id}`; a lone task id exits 2.

| Family | Cases | User-visible answer | Inspect / settlement |
|---|---|---|---|
| Retrieve-only | A-LIT-01–04, X8-02 | Host-projected `source_id` list, one id per line. Not a title `summary`. `rows` is per connector; the deduped union may exceed `rows`. No second `[Background skill execution completed]` line. A spilled `source_ids` path must be readable; a jail denial is `rejected` / blocked, not `succeeded`. | `outputs` / `required_outputs` include `sources`. Any persisted `source_id` pays that debt (existence-only). `omni why` shows route + settlement. |
| Survey / related work | A-SUR-01–02 | A manuscript / `draft.section`, not a title list. Synthesis labels conclusions **grounded / inferred / insufficient evidence** when it writes them. | Verification includes `draft.section`. |
| Full paper | P-01 | `draft.manuscript` is a full paper, not a section and not a title list. Do not use the ledger token as a `write_file` path. | Distinct from `draft.section`. |
| User-facing files | P-01, A-FIG, A-SLD, A-POS, A-LF, A-OUT | Deliverables land under `<out>/<title>_<task8>/` (for example `outputs/RAG-system-survey_67f26c86/`). Filenames stay `<slug>-<task8>-<art8>.ext`. Product default `--out` is `outputs/`. This catalog launches with `--out outputs_walkthrough` so validation files stay out of `git status` and out of real `outputs/`. Do not use `--out .` from a checkout. Do not present `artifact://` as the path the user should open. `research-pptx` binds sources by a durable handle, not a missing raw file. A bash dump of `.venv` / LICENSE into `$OMNI_OUTPUT_DIR` is not a deliverable inventory. | Recent contract: hide `artifact://` from the reader. Harvest is suffix-allowlisted. Walkthrough isolation is A-OUT-03 / A-OUT-04. |
| Corpus QA | A-COR-01–02, C-LITC-01 | `omni lit` / `search_corpus` with inline `[S#]`. Empty library says so; no invented DOI. `omni lit` is the corpus, not `source list`. | `[S#]` maps to a real in-library chunk. |
| arXiv fetch | A-ARX-01–02 | Metadata, abstract, `pdf_url`, and `source_id`. Foreground body, not `Created execution`. A local PDF is not required by the contract. | `source_id` is on this Task. |
| Paper review | A-REV-01–05 | Review body in the turn. Missing file → `needs_input`. An arXiv id is fetched (or `needs_input` if the PDF cannot be materialized). A DOI is `needs_input`, not `Paper input does not exist`. With VLM: visual crops are interpreted. Without VLM: text review continues, visual is partial, `omni config vlm` and `skip_visual=true` are offered. | Not a receipt. Do not call visual review `succeeded` when the code is `vlm_not_configured`. A `write_file` Markdown leftover does not pay `review` after `paper-review` failed. |
| Response letter | A-RR-01–02 | `response_letter` artifact. Does not restart retrieval. | |
| Figure | A-FIG-01–02, P-01 | PNG/SVG (`scientific-figure`). Not a deck. A sidecar `.dot` / `.json` does not satisfy `artifact.figure`. The RAG architecture figure names **query, retriever, reranker, and LLM**. `scientific-figure` does not need a VLM. | |
| LiveFigure (VLM on) | A-LF-01–02, A-LF-04 | One editable PPTX slide. Provider is `livefigure`. File under `outputs/<title>_<task8>/`. No `vlm_not_configured`. No API key in output. | Debt is `figure.editable.pptx` / `artifact.pptx`. Not `research-pptx`. A harvested deck or `write_file` leftover does not pay that debt after `livefigure` failed. |
| LiveFigure (VLM off) | A-LF-03 | `vlm_not_configured`. `needs_input` or execution "needs configuration". Next action is `omni config vlm`. Engine is not loaded. No silent `scientific-figure` swap. | Do not call this `succeeded`. |
| Slides | A-SLD-01–02 | Multi-slide `.pptx` (`research-pptx`). Not a one-slide figure. Text-domain critique; no VLM required. | Debt is `artifact.slides`. |
| Poster (VLM on) | A-POS-01–02 | Distinct from slides. `visual_review_mode=vlm` (or the visual loop actually ran). | Debt is `artifact.poster`. |
| Poster (VLM off) | A-POS-03 | Poster may still land (`deterministic-only`). Visual quality is pending, not VLM-approved. Missing VLM is not `needs_input`. | Do not claim a VLM reviewed it. |
| Ideation | A-IDE-01–02 | Testable follow-ups, claims, and risks. Not another paper list. | |
| Plan mode | A-PLN-01–02, W-03 | Bounded plan. `awaiting_approval`. No execution events before `task approve`. Approval reuses the same Task id. | |
| Review mode | A-RD-01–02 | Read-only. No `write_file` / `run_skill`. | |
| Task inspect | A-TSK-01 | `Task {id} status: **{status}** ({description}).` Degraded means "completed with warnings or missing pieces; this is not a full success". Failed uses **Why it failed**, not System summary. | Does not narrate success. |
| Task review | A-TSK-02 | Model narrative plus footer `Reviewed tasks (authoritative status):` with `` `{id}` **{status}** ``. Recovery candidates include `/task retry {id}`. | Footer is host-owned. |
| Why | A-WHY-01 | Table: seq, event, summary, decision/detail. Events include `plan.*`, `route.arbitration`, `plan.target.artifact`, `subtask.submitted`, `assistant.message`. | |
| Current focus | A-WHY-02 | Table: session, origin, skill, target_kind, task, workflow, workflow_step, skill_execution, child_task, title, artifact, source, confidence. | |
| Skill card (CLI) | A-REV, A-FIG, A-SLD, A-POS | `✅ **skill** (succeeded) execution=xxxxxxxx task=xxxxxxxx` / `!` degraded / `❌` failed. Artifacts, Research record (`sources` / claims / evidence), Next actions. Degraded with no file: "No saved artifact was produced." | |
| Live execution line | E-01–04, A-SUB | `◷ execution {skill} started execution=xxxxxxxx` → `✓ … succeeded` / `⚠ … degraded` / `⚠ … needs configuration` / `✗` failed. Workflow: `≈ workflow degraded workflow=xxxxxxxx`. | |
| Task show | C-TSK-04, E-01–04 | `object_kind=task`, user input, plan (intent / skills / contract / settlement / remaining), workflow runs, workflow steps, skill executions (`execution_id`, step, skill, attempt, status), child tasks, activity. Failed tools stay visible. Degraded/failed recommended action is `omni task retry {short}`. | |
| Task all | C-TSK-03 | Footer `Showing N of M`. Default kind is `all`. | |
| Memory list / search | M-WR, M-RC, C-MEM-01 | Truncated summaries. `memory detail` for full text. | |
| Memory graph | M-GR-01, C-MEM-03, W-04 | Stored edges only. No invented edges. | |
| Verify | A-VFY-01–02 | `--session` requires an id. Flags unsupported / contradicted / overconfident. | |
| One task, many executions | E-01–02 | One Task id. Each skill-backed step has its own `execution=` id. `task show` / `task subtask` list ≥2 rows. | Ledger of what ran, not a contract the model must follow. |
| Execution degrade | E-03–05, W-02 | The bad execution is `degraded` / `failed` / `needs_input`. Do not call it `succeeded`. Siblings that delivered stay. Parent is `degraded` (or `needs_input` / `failed`) unless a later retry superseded that child and every required output is already on this Task. Leftover `write_file` / harvested files do not count as that retry. `task retry <execution>` creates a new execution id and keeps the WorkflowStep id. `task resume` continues from the checkpoint. `task requeue` accepts a standalone skill-execution id only. | |

## Specification table

| Point | Case IDs | Need | What the user checks |
|---|---|---|---|
| Named `search_literature` | A-LIT-01, A-LIT-02 | network | Named native tool stays in this turn. Reply is `source_id` values. No `run_skill`. No second background-completion line. |
| Natural-language literature search | A-LIT-03, A-LIT-04 | network | Lone `literature.search` stays ReAct + `search_literature`. Settlement uses `source_ids`. |
| Retrieval plus written survey | A-SUR-01, A-SUR-02 | network | Survey pair: host retrieve then native synthesis. A-SUR-01 is the latent-space-intervention research brief. Manuscript debt is paid. |
| Recent-commit / changelog review | A-CHG-01, A-CHG-02 | model | Review the last few days of local git commits. Features, optimizations, problems solved, remaining risks. Not a literature search. |
| Survey pack (fetch + figure + paper + slides) | P-01, P-02 | network | P-01 is task `72590550`: Attention abstract + query/retriever/reranker/LLM figure + `draft.manuscript` + PPT. P-02 is loop-engineering survey + a detailed intro deck. Do not stop at `find_skill`. |
| Local corpus QA | A-COR-01, A-COR-02 | model | `omni lit` / `search_corpus` with `[S#]`. Empty library does not invent a DOI. |
| arXiv fetch | A-ARX-01, A-ARX-02 | network | `$arxiv-fetch` / `paper.fetch.arxiv` returns metadata, abstract, `pdf_url`, and `source_id`. |
| Paper review | A-REV-01, A-REV-02, A-REV-03, A-REV-04, A-REV-05 | model/network | Body, not a receipt. A-REV-01 is VLM-on visual. A-REV-03 is VLM-off partial. Missing file asks. An arXiv id is not a missing local path. A DOI asks; it is not fetched. |
| VLM-driven skills | A-LF-01, A-LF-02, A-LF-03, A-LF-04, A-REV-01, A-REV-03, A-REV-04, A-REV-05, A-POS-01, A-POS-03 | model/network | Probe `omni config vlm`. If VLM is on, run both the configured and the unconfigured rows. If VLM is off, run only the unconfigured degrade rows. Identifier and leftover-file honesty rows are VLM-independent. |
| Review response | A-RR-01, A-RR-02 | model | `review-response` writes a response letter from the prior review. |
| Scientific figure | A-FIG-01, A-FIG-02 | model | `scientific-figure` then in-place `artifact.revise`. RAG figure names query / retriever / reranker / LLM. Not a deck. |
| Editable LiveFigure | A-LF-01, A-LF-02, A-LF-03, A-LF-04 | model | VLM-on: one editable PPTX. VLM-off (A-LF-03): `vlm_not_configured`, no silent `scientific-figure` swap. A-LF-04: failed `livefigure` plus a leftover deck does not settle the parent `succeeded`. |
| Slides | A-SLD-01, A-SLD-02 | model | `research-pptx` / `artifact.slides`. Not a one-slide figure. No VLM required. |
| Poster | A-POS-01, A-POS-02, A-POS-03 | model | VLM-on: visual loop. VLM-off (A-POS-03): `deterministic-only`, poster may still land. |
| Research ideation | A-IDE-01, A-IDE-02 | model/network | `research-ideation` proposes testable follow-ups. |
| Explicit `$skill` | A-SKL-01, A-SKL-02 | model/network | `$name` binds that skill. Unknown `$name` falls through to ReAct. |
| Third-party skill load and use | C-SKL-05, A-SKL-03 | offline/model | `skills add` a local SKILL.md, `trust`, then `$walkthrough-echo` actually runs. `untrust` / `remove` cleans up. Not `skills install`. |
| Plan mode | A-PLN-01, A-PLN-02 | offline/model | `omni chat --mode plan` or `/mode plan`. Bounded; approval reuses the task id. |
| Review mode | A-RD-01, A-RD-02 | model | Read-only tools. No `write_file` / `run_skill`. |
| Task inspect and retrospective | A-TSK-01, A-TSK-02 | model | One-task status vs multi-task review with an authoritative footer. |
| Schedules | A-SCH-01, A-SCH-02 | model/serve | Recurring `schedule_task`. Ambiguous time asks. `omni schedule run` fires due jobs in this process; `omni serve` is for unattended fire. |
| One-shot wall-clock schedule | A-SCH-03, C-SCH-05 | model/offline | Today 7:10 / `--at` local wall-clock for the P-01 RAG pack. A time already in the past is refused. |
| Subagents and workflows | A-SUB-01, A-SUB-02 | model/network | Multi-step work sequences live. Retrieve-only can block `spawn_subagents`. |
| Local files | A-FIL-01, A-FIL-02 | model | `read_file` / `@attachment`. Missing file asks. |
| Product self-knowledge | A-DOC-01, A-DOC-02 | model/offline | `docs_search` or honest mock. No absolute home-path leak. |
| Steer and stop | A-CTL-01, A-CTL-02 | model | `/steer` updates the turn. `/stop` cancels at the next boundary. |
| Explain and focus | A-WHY-01, A-WHY-02 | offline | `omni why` / `omni current`. |
| Hypothesis, claim, evidence | A-ROM-01, A-ROM-02 | model | ROM tools persist; CLI `hypo` / `evidence` can show them. |
| Verification | A-VFY-01, A-VFY-02 | model | `omni verify` and statistical audit. |
| Contradiction scan | A-CON-01, A-CON-02 | model | `evidence.contradiction_scan` or `review_statistics`. |
| Scientist persona | A-PER-01, A-PER-02 | offline/model | `omni soul` lists personas. Persona changes tone, not tool names. |
| Scientist KG distiller | A-KG-01, C-SOUL-03 | model/offline | `$scientist-kg-distiller` or `soul create --dry-run`. Dry-run does not install a KG. |
| Write memory | M-WR-01, M-WR-02 | model/offline | Conversational remember vs `omni memory add`. |
| Recall memory | M-RC-01, M-RC-02 | model | New session recalls style and continues prior research. |
| Memory graph and profile | M-GR-01, M-GR-02 | offline | `memory graph` / `memory profile`. |
| Research notebook | M-NB-01, M-NB-02 | offline | `memory notebook` / `memory sync`. |
| Stacked 3 capabilities | X3-01, X3-02 | network | One utterance, three debts. |
| Stacked 4 capabilities | X4-01, X4-02 | model/network | Four live steps, or plan-then-approve. |
| Stacked 5 capabilities | X5-01, X5-02 | network | Five-step live sequence. Not `SINGLE_SKILL_TASK`. |
| Stacked 6 capabilities | X6-01, X6-02 | model/network | Six debts; poster and slides stay distinct. |
| Stacked 7 capabilities | X7-01, X7-02 | network | Seven debts. Schedule asks. ROM chain can close. |
| Stacked 8 capabilities | X8-01, X8-02 | network | Eighth step recalls memory. X8-02: retrieve-only wins a contradictory utterance. |
| Long-horizon campaign | W-01, W-02, W-03, W-04 | model/network | Cross-session continue, failed-route steer, plan-approve-verify, memory graph. Not the same as one Task with many executions. |
| One task, many executions | E-01, E-02 | model/network | One user request → one Task → ≥2 Skill Executions (or a WorkflowRun with ≥2 steps). `task show` / `task subtask` list them separately. |
| Execution-level degrade | E-03, E-04, E-05 | model/network | One execution fails or is incomplete. That row is `degraded` / `failed` / `needs_input`. Siblings that delivered stay. Parent is not `succeeded`. Leftover Markdown / harvested PPTX do not retire `review` / `artifact.pptx`. `task retry` / `resume` target the execution and keep the step id. |
| One-shot and unknown command | C-CHAT-01, C-CHAT-02 | model/offline | Bare `omni "..."` vs a mistyped command. |
| Init and doctor | C-INIT-01, C-INIT-02 | offline | `omni doctor` / `omni init --help`. |
| Config | C-CFG-01, C-CFG-02, C-CFG-03, C-CFG-04, C-CFG-05, C-CFG-06 | offline | `list` / `path` / `test` / `get` / `home` plus `vlm` / `embeddings` / `semantic-scholar` / `model` help. Do not `set` on a real home. |
| Project | C-PRJ-01, C-PRJ-02, C-PRJ-03, C-PRJ-04 | offline | `project list` / `info` / `help` / `new --help`. No `migrate`. |
| Profile | C-PRF-01, C-PRF-02 | offline | `profile list` / `show`. Missing default profile exits 1. |
| Session | C-SES-01, C-SES-02, C-SES-03, C-SES-04 | offline | `session list` / `fork` / `resume` / `show` / `export`. |
| Skills CLI | C-SKL-01, C-SKL-02, C-SKL-03, C-SKL-04, C-SKL-05, C-SKL-06 | offline | Browse, add+trust a local third-party SKILL.md, setup/export help, then untrust/remove. No `skills install`. |
| Model stack | C-MOD-01, C-MOD-02, C-MOD-03, C-MOD-04 | offline | `model status` / `explain` / `help` / `use --help`. |
| Memory CLI | C-MEM-01, C-MEM-02, C-MEM-03, C-MEM-04 | offline | `list` / `path` / `pin` / `clear` / `link` / `graph` / `detail` / `edit`. |
| Task CLI | C-TSK-01, C-TSK-02, C-TSK-03, C-TSK-04, C-TSK-05, C-TSK-06, C-TSK-07, C-TSK-08 | offline/model | Observe, control, then `attach` / `archive` / `drain` help. Do not archive or drain another operator's running task. |
| Artifacts CLI | C-ART-01, C-ART-02, C-ART-03, C-ART-04 | offline | `preview` / `versions` / `review` / `diff` / `help`. |
| Outbox harvest | A-OUT-01, A-OUT-02 | offline | `$OMNI_OUTPUT_DIR` harvest skips `.venv` / LICENSE. A harvested deck does not pay `artifact.pptx`. |
| Walkthrough output isolation | A-OUT-03, A-OUT-04 | offline | Catalog `--out outputs_walkthrough`. Bundles are `outputs_walkthrough/<title>_<task8>/`, not checkout-root `*_xxxxxxxx/`. `git check-ignore` covers that folder. `--out` help names `outputs/` as the product default. |
| Cite and source | C-CIT-01, C-CIT-02 | offline | `cite list` / `export`, `source list` / `reindex`. |
| ROM CLI | C-HYP-01, C-HYP-02 | offline | `hypo` / `claim` / `evidence` / `run`. |
| Lit, bench, eval | C-LITC-01, C-LITC-02 | model/offline | `omni lit`. Black-box with zero successes exits 1. |
| Schedule CLI | C-SCH-01, C-SCH-02, C-SCH-03, C-SCH-04, C-SCH-05, C-SCH-06 | offline/serve | Recurring cron plus one-shot `--at`. Past `--at` is refused. Then `remove` the walkthrough job. |
| Serve, status, web | C-SRV-01, C-SRV-02, C-SRV-03 | offline | Loopback only. Same store as `--project`. |
| Channels | C-CH-01, C-CH-02 | offline | `list` / `test`. QR login is interactive (blocked here). |
| MCP, trust, exec, replay | C-MCP-01, C-MCP-02, C-MCP-03, C-EXE-01, C-EXE-02, C-TRU-01 | offline/model | `mcp` help/list/agents; `exec` a one-shot prompt; `replay` a session; `trust --list`. |
| Soul CLI | C-SOUL-01, C-SOUL-02, C-SOUL-03 | offline | `status` / `list` / `help` / `create --dry-run`. |
| AutoSOTA wrapper | C-AS-01, C-AS-02 | offline | `info` / `doctor` / `status`. No Omni background task. |
| Update, terminal, uninstall | C-OPS-01, C-OPS-02 | offline | Status plus `uninstall --dry-run`. |
| REPL verbs | C-REPL-01, C-REPL-02, C-REPL-03, C-REPL-04, C-REPL-05, C-REPL-06, C-REPL-07 | offline | `/help` plus context/compact/copy/verbose/debug/clear/new/inbox/task all, and `/web` in the background. |

## Named user prompts this catalog must hit

These four prompts (or the same shape) are required. Live Chinese wording may
be used at the keyboard; public rows stay English.

| User prompt | Case IDs | Notes |
|---|---|---|
| Review the last four days of commits: features, optimizations, problems solved, anything unreasonable. What were the optimization points? | A-CHG-01, A-CHG-02 | Local git / docs. Not `search_literature` as the only action. |
| Research how latent-space intervention improves LLM agentic ability. | A-SUR-01, A-IDE-02 | A-SUR-01 writes related work. A-IDE-02 stress-tests the claim. |
| Prepare materials for an agentic loop-engineering survey and output a detailed introductory PPT. | P-02, A-SLD-01 | Survey + detailed `.pptx`, not a one-slide figure. |
| Set a one-time job today at 7:10 to prepare RAG survey materials: fetch the Attention Is All You Need abstract, draw a query/retriever/reranker/LLM figure, and write a paper. | A-SCH-03, C-SCH-05, P-01 | P-01 is the live pack (also task `72590550`). The 7:10 job is one-shot `--at`. |

Task `72590550` user input was: prepare RAG survey materials, fetch the
Attention Is All You Need abstract, generate a scientific architecture figure
with query / retriever / reranker / LLM, and deliver a paper plus PPT. That
shape is **P-01**. A run that only calls `find_skill` / `search_corpus` and
settles `degraded` with `remaining artifact.figure, draft.manuscript,
artifact.slides` **fails** P-01.

## Coverage inventory

Every user-facing surface maps to at least one case. Aliases
(`task delete` = `task rm`, `skills install` = `skills export`) are not
second cases. Hidden hooks are out.

### Built-in skills

| Skill | Cases |
|---|---|
| `arxiv-fetch` | A-ARX-01, A-ARX-02, P-01, E-02 |
| `openalex-search` | A-SKL-01, W-02 |
| `paper-review` | A-REV-01, A-REV-02, A-REV-03, A-REV-04, A-REV-05, E-02, E-05 |
| `review-response` | A-RR-01, A-RR-02 |
| `scientific-figure` | A-FIG-01, A-FIG-02, P-01, E-01 |
| `livefigure` | A-LF-01, A-LF-02, A-LF-03, A-LF-04, E-03, E-05 |
| `research-pptx` | A-SLD-01, A-SLD-02, P-01, P-02, E-02 |
| `scientific-poster` | A-POS-01, A-POS-02, A-POS-03 |
| `research-ideation` | A-IDE-01, A-IDE-02 |
| `scientist-kg-distiller` | A-KG-01, C-SOUL-03 |
| `soulagent` | A-PER-01, A-PER-02 |
| `agent-goal` (schedule `--goal`) | A-SCH-01, A-SCH-03, C-SCH-02, C-SCH-05 |
| Native `search_literature` | A-LIT-01–04 |
| Native `draft.section` / `draft.manuscript` | A-SUR-01–02, P-01, P-02 |
| Third-party local SKILL.md | C-SKL-05, A-SKL-03, C-SKL-06 |

### CLI command groups

| Group | Cases that exercise it |
|---|---|
| `omni` / `chat` / one-shot prompt | C-CHAT-01, A-LIT-01, P-01 |
| `lit` / `verify` / `bench` / `eval` | A-COR-01, A-VFY-01, C-LITC-01, C-LITC-02 |
| `exec` / `replay` | C-EXE-01, C-EXE-02 |
| `trust` | C-TRU-01, C-MCP-01 |
| `current` / `why` | A-WHY-01, A-WHY-02 |
| `doctor` / `init` | C-INIT-01, C-INIT-02 |
| `status` / `serve` / `web` | C-SRV-01, C-SRV-02, C-SRV-03, C-REPL-07 |
| `resume` | C-SES-02 |
| `update` / `terminal` / `uninstall` | C-OPS-01, C-OPS-02, C-TERM-01 |
| `config` | C-CFG-01–06 |
| `autosota` | C-AS-01, C-AS-02 |
| `skills` | C-SKL-01–06, A-SKL-01–03 |
| `soul` | C-SOUL-01–03, A-PER, A-KG-01 |
| `project` / `profile` | C-PRJ-01–04, C-PRF-01–03 |
| `session` | C-SES-01–04 |
| `memory` | M-*, C-MEM-01–04 |
| `cite` / `source` | C-CIT-01–03 |
| `task` | C-TSK-01–08, A-TSK, E-01–05, A-PLN-02 |
| `artifacts` | C-ART-01–04, A-OUT-01, A-OUT-02, A-OUT-03, A-OUT-04 |
| `channel` | C-CH-01, C-CH-02 |
| `mcp` | C-MCP-01–04 |
| `schedule` | A-SCH-01–03, C-SCH-01–06 |
| `hypo` / `claim` / `evidence` / `run` | A-ROM-01–02, C-HYP-01–03 |
| `model` | C-MOD-01–04 |
| REPL in-process | C-REPL-01–07, A-CTL, A-PLN-02, A-RD |

## VLM-driven skills

Three built-in skills use a vision model as a driver. `scientific-figure` and
`research-pptx` do **not**; they stay in the no-VLM path.

| Skill | VLM role | No-VLM behavior |
|---|---|---|
| `livefigure` | Required (`services: [vlm]`). Draws and revises one editable PPTX. | Hard stop before the engine loads. `vlm_not_configured`. `needs_input` / "needs configuration". Action is `omni config vlm`. Must not swap to `scientific-figure`. |
| `paper-review` | Drives MinerU crop interpretation. Text model stays separate. | Notify immediately. Continue text review and crop extraction. Visual outcome `vlm_not_configured`. Offer `omni config vlm` and `skip_visual=true`. Review body is still delivered. Do not call visual review `succeeded`. |
| `scientific-poster` | Drives the pixel visual-review loop. | `visual_review_mode=deterministic-only`. Poster may still land if Chromium inspection passes. Missing VLM is not a human checkpoint and not `needs_input`. Do not claim a VLM reviewed it. |

### Probe (read-only)

```bash
omni config vlm
omni doctor
```

`omni config vlm` with no flags prints enabled / model / endpoint / key-set.
It does not write. `omni doctor` VLM row is `not configured (optional)` or
the model @ endpoint. Do not `config vlm --disable` on a real home.

**Branch**

- VLM **not** configured → run only the `novlm` rows (A-LF-03, A-REV-03, A-POS-03).
- VLM **is** configured → run the `vlm` rows **and** the `novlm` rows. For
  `novlm` while the owner VLM stays on, use an isolated home (set `OMNI_HOME`
  and remap `XDG_CONFIG_HOME`; do **not** `omni init --home`):

```bash
export HOME=/tmp/omni-walkthrough-novlm
export XDG_CONFIG_HOME="$HOME/.config"
export OMNI_HOME="$HOME/.omni"
PYTHONPATH=cli/src .venv/bin/omni --project walkthrough-novlm --trust --out outputs_walkthrough
omni config vlm    # must show not configured
```

Do not put VLM keys in the case log. `omni config vlm --test` is allowed on a
home that already has a VLM; it must not print the key.

### Cases

| ID | Branch | Need | Input | Expected output |
|---|---|---|---|---|
| A-LF-01 | vlm | model | `$livefigure` Make one editable PPTX slide of a RAG architecture with query, retriever, reranker, and LLM. | Provider `livefigure`. One editable PPTX under `outputs/<title>_<task8>/`. Four boxes present. No `vlm_not_configured`. No `scientific-figure` swap. No API key in the answer or `task show`. Live line `✓ execution livefigure succeeded`. If `livefigure` fails, parent is not `succeeded` (see A-LF-04 / E-05). |
| A-LF-02 | vlm | model | `Make this RAG figure one editable scientific figure in PowerPoint.` | Same as A-LF-01. Distinct from `research-pptx` (full deck) and from `scientific-figure` (PNG/SVG). |
| A-LF-03 | novlm | model | Same as A-LF-01 (`$livefigure` … four boxes). | Admission: `vlm_not_configured` plus `omni config vlm`. Engine not loaded. Inline turn is `needs_input` or the execution card is `⚠ … needs configuration` / `!` degraded. No editable PPTX. No silent `scientific-figure`. Do not record `succeeded` / `result_success=true`. |
| A-LF-04 | either | model | After a failed `livefigure` (dunder / engine error), a leftover `deck.pptx` from `bash` / `write_file` is registered. | Parent is `degraded` or `failed`. `artifact.pptx` remains unpaid. The leftover deck is `artifact.slides` at most. `task show` does not narrate success. |
| A-REV-01 | vlm | network | `Review arXiv 1706.03762 as a NeurIPS reviewer.` | `paper-review` resolves the arXiv id to a local PDF (not `Paper input does not exist`). Review body in the turn, not `Created execution`. Visual stage is not `vlm_not_configured`. Figures/tables are interpreted, or the visual status is honest. Venue form + Detailed Revision Plan. A `write_file` Markdown leftover after `paper-review` failed does not settle `succeeded`. |
| A-REV-02 | either | model | `$paper-review` the workspace file `draft.pdf` | Missing file → `needs_input`. No invented URL. Independent of VLM. |
| A-REV-03 | novlm | network | `Review arXiv 1706.03762 as a NeurIPS reviewer.` | Identifier is fetched first. Review body still arrives when the PDF is in hand (text + MinerU crops). Immediate note: no VLM. Visual outcome `vlm_not_configured`. Next actions include `omni config vlm` and `skip_visual=true`. Do not call visual review `succeeded`. Task may be `degraded` / `partial`. |
| A-REV-04 | either | model | `Review arXiv 1706.03762 as a NeurIPS reviewer.` when the PDF cannot be materialized (offline / fetch miss). | `needs_input`. Error names the identifier. Not `Paper input does not exist: arXiv 1706.03762`. Parent is not `succeeded`. |
| A-REV-05 | either | model | `Review doi:10.5555/3295222.3295349 as a NeurIPS reviewer.` | `needs_input`. DOI is not fetched. Not a missing local path. |
| A-POS-01 | vlm | model | `Make a scientific poster summarizing RAG evaluation benchmarks.` | `scientific-poster`. `artifact.poster` under `outputs/<title>_<task8>/`. `visual_review_mode=vlm` or the visual loop ran. Not slides. |
| A-POS-02 | vlm | model | `$scientific-poster` from the survey we just wrote. | Explicit skill. Same poster debt. Run on the VLM-on home when VLM is configured. |
| A-POS-03 | novlm | model | Same as A-POS-01. | Poster may still land. `visual_review_mode=deterministic-only`. Visual quality pending, not VLM-approved. Missing VLM is not `needs_input`. Do not claim a VLM reviewed it. |

If the configured VLM rejects every crop or image, that is not the `novlm`
row. Tell the operator to run `omni config vlm --test` and pick a model that
accepts image input. A text-only chat model used as the VLM fails that test.

## Conversation and research

### Named `search_literature`

| ID | Need | Input | Pass |
|---|---|---|---|
| A-LIT-01 | network | `omni --project walkthrough-aug24 --trust --out outputs_walkthrough "Call search_literature, query='large language model retrieval augmented generation survey 2024', rows=8. After retrieval do not write a survey; list hit source_id values only. Do not use write_file, run_skill, or spawn_subagents."` | Trace is `search_literature(query, rows=8)`. Reply lists `source_id` values. `rows` is per connector. No `run_skill`, `write_file`, or `spawn_subagents`. No second `[Background skill execution completed]` line. A spilled `source_ids` path must be readable; a jail denial is `rejected` / blocked, not `succeeded`. |
| A-LIT-02 | network | `Call search_literature with query='transformer attention survey 2017' and rows=5. After retrieval list source_ids only.` | Same native-tool path as A-LIT-01; English naming must not compile `openalex-search` as `run_skill`. |

### Natural-language literature retrieval

| ID | Need | Input | Pass |
|---|---|---|---|
| A-LIT-03 | network | `Search 2024 RAG factuality papers and give citeable source_id values only. Do not write a survey.` | ReAct plus `search_literature`. Empty `selected_skills`. Host blocks `run_skill`. |
| A-LIT-04 | network | `Find recent papers on privacy attacks against federated learning. List titles and source_id values.` | Settlement pays the `sources` debt with `source_ids`, not a title `summary`. |

### Retrieval plus written survey

| ID | Need | Input | Pass |
|---|---|---|---|
| A-SUR-01 | network | `Survey how latent-space intervention improves LLM agentic ability and write a related-work section.` | Required research-brief shape. Survey pair: host retrieve plus native synthesis. Verification includes `draft.section`. Conclusions labeled grounded / inferred / insufficient evidence when written. Not a title list. |
| A-SUR-02 | network | `Write a short related-work section on retrieval-augmented generation evaluation benchmarks.` | Literature hits plus a manuscript artifact; not a title list alone. Same synthesis labels as A-SUR-01. |

### Local corpus QA

| ID | Need | Input | Pass |
|---|---|---|---|
| A-COR-01 | model | `omni lit "How does RAG reduce hallucination?"` | `search_corpus`. Inline `[S#]`. No invented sources. |
| A-COR-02 | model | `From papers already in my library, which RAG evaluation benchmarks exist? Mark [S#].` | Only in-library chunks. Empty library says so; no fabricated DOI. |

### arXiv fetch

| ID | Need | Input | Pass |
|---|---|---|---|
| A-ARX-01 | network | `Fetch the full text of arXiv 1706.03762 and tell me the contributions.` | `paper.fetch.arxiv` / `$arxiv-fetch`. Metadata, abstract, `pdf_url`, `source_id`. |
| A-ARX-02 | network | `$arxiv-fetch Attention Is All You Need` | Explicit skill protocol binds `arxiv-fetch`. |

### Paper review and response letter

| ID | Need | Input | Pass |
|---|---|---|---|
| A-REV-01 | network | `Review arXiv 1706.03762 as a NeurIPS reviewer.` | VLM-on. arXiv id is fetched, not treated as a missing path. `paper-review` body in the turn, not `Created execution`. Visual stage is not `vlm_not_configured`. See [VLM-driven skills](#vlm-driven-skills). |
| A-REV-02 | model | `$paper-review` the workspace file `draft.pdf` | Explicit skill. Missing file becomes `needs_input`; no invented URL. Independent of VLM. |
| A-REV-03 | network | `Review arXiv 1706.03762 as a NeurIPS reviewer.` | VLM-off. Identifier is fetched first. Review body still arrives when the PDF is in hand. Visual outcome `vlm_not_configured`. Offers `omni config vlm` and `skip_visual=true`. Do not call visual review `succeeded`. |
| A-REV-04 | model | Same input as A-REV-01 when the PDF cannot be materialized. | `needs_input`. Not `Paper input does not exist`. Parent is not `succeeded`. |
| A-REV-05 | model | `Review doi:10.5555/3295222.3295349 as a NeurIPS reviewer.` | `needs_input`. DOI is not a local path and is not fetched. |
| A-RR-01 | model | `Write a response letter from the review you just produced.` | `review.response`. Does not restart retrieval. |
| A-RR-02 | model | `$review-response` reply to reviewer 2 on novelty, point by point. | Explicit skill. `response_letter` artifact. |

### Figure, slides, poster, ideation

| ID | Need | Input | Pass |
|---|---|---|---|
| A-FIG-01 | model | `Draw a RAG architecture diagram that includes query, retriever, reranker, and LLM.` | `artifact.figure` / `scientific-figure`. PNG/SVG. The four boxes are present. Not a deck. File lands under `outputs/<title>_<task8>/`. |
| A-FIG-02 | model | `Change the previous figure's retriever to hybrid. Keep the color language.` | `artifact.revise` in place, or a full redraw if there is no active figure. |
| A-SLD-01 | model | `Make a group-meeting deck from the Transformer paper.` | `slides.generate` / `research-pptx`. `.pptx`. Not a one-slide figure. File lands under `outputs/<title>_<task8>/`. |
| A-SLD-02 | model | `$research-pptx` thesis-defense deck on RAG factuality. | Explicit skill. Debt is `artifact.slides`. |
| A-POS-01 | model | `Make a scientific poster summarizing RAG evaluation benchmarks.` | VLM-on. `scientific-poster`. `artifact.poster`. `visual_review_mode=vlm` or the visual loop ran. Not slides. |
| A-POS-02 | model | `$scientific-poster` from the survey we just wrote. | Explicit skill. Same poster debt. Run on the VLM-on home when VLM is configured. |
| A-POS-03 | model | `Make a scientific poster summarizing RAG evaluation benchmarks.` | VLM-off. Poster may still land (`deterministic-only`). Visual quality pending. Missing VLM is not `needs_input`. Do not claim a VLM reviewed it. |
| A-LF-01 | model | `$livefigure` Make one editable PPTX slide of a RAG architecture with query, retriever, reranker, and LLM. | VLM-on. One editable PPTX under `outputs/<title>_<task8>/`. No `vlm_not_configured`. No `scientific-figure` swap. No API key in output. If the skill fails, see A-LF-04. |
| A-LF-02 | model | `Make this RAG figure one editable scientific figure in PowerPoint.` | VLM-on. Same LiveFigure contract as A-LF-01. Not a full `research-pptx` deck. |
| A-LF-03 | model | Same input as A-LF-01. | VLM-off. `vlm_not_configured` + `omni config vlm`. Engine not loaded. `needs_input` / needs configuration. No silent `scientific-figure`. Not `succeeded`. |
| A-LF-04 | model | Failed `livefigure` plus a leftover `deck.pptx` from bash / `write_file`. | Parent is `degraded` / `failed`. `artifact.pptx` unpaid. Leftover deck is not the editable figure. |
| A-IDE-01 | network | `From the RAG papers we just retrieved, propose 3 testable follow-ups.` | `research.ideation`. Does not stop at another literature search. |
| A-IDE-02 | model | `$research-ideation` stress-test "latent-space intervention improves agents". | Explicit ideation. Claims and risks, not a paper list. |

### Outbox harvest

| ID | Need | Input | Pass |
|---|---|---|---|
| A-OUT-01 | offline | A bash fallback writes `.venv/…/mod.py`, `LICENSE`, and `results.csv` under `$OMNI_OUTPUT_DIR`. | Only `results.csv` is registered. LICENSE and `site-packages` are not task artifacts. |
| A-OUT-02 | offline | A harvested `deck.pptx` (`kind=slides`) sits on a Task that owes `artifact.pptx`. | `artifact.pptx` remains unpaid. The deck may pay `artifact.slides`. |
| A-OUT-03 | offline | From the checkout: `omni --project walkthrough-aug24 --trust --out outputs_walkthrough` then publish one task deliverable. | File is `outputs_walkthrough/<title>_<task8>/…` with `_omni-manifest.json`. Checkout root has no new `<title>_<task8>/` sibling of `cli/`. |
| A-OUT-04 | offline | `omni --help` for `--out`; `git check-ignore -v outputs_walkthrough/probe.md`; `git status --porcelain` after A-OUT-03. | `--out` help says default is `outputs/`. `outputs_walkthrough/` is ignored. No new untracked root-level `*_xxxxxxxx/` directory. |

### Explicit `$skill`, plan mode, review mode

| ID | Need | Input | Pass |
|---|---|---|---|
| A-SKL-01 | network | `$openalex-search federated learning privacy` | `SINGLE_SKILL_TASK` plus `openalex-search`. Skill protocol wins over a later `search_literature` token. |
| A-SKL-02 | model | `$not-a-real-skill` search the literature | Missing skill falls through to bounded ReAct. |
| A-SKL-03 | model | After C-SKL-05: `$walkthrough-echo ping` | Trusted third-party skill binds and runs. Reply includes `WALKTHROUGH_OK`. `skills info walkthrough-echo` still finds it. Unknown `$name` still falls through (A-SKL-02). |
| A-PLN-01 | offline | `omni chat --mode plan "Plan how to compare RAG retrieval strategies. Do not execute."` | `--mode` is a `chat` flag, not a root flag. No unbounded `propose`. `awaiting_approval`. `search_literature` and `run_skill` blocked. |
| A-PLN-02 | model | `/mode plan` then: fetch 1706.03762, write a survey, make slides. Execute only after approval. | Plan persists first. `omni task approve` reuses the same task id. |
| A-RD-01 | model | `/review` Check whether "RAG always reduces hallucination" is over-claimed. | Read-only tools. No `write_file` or `run_skill`. |
| A-RD-02 | model | `/mode review` Why was the last task degraded? | Reads `get_task` / events. Does not mutate artifacts. |

### Tasks, schedules, subagents, files, product docs

| ID | Need | Input | Pass |
|---|---|---|---|
| A-TSK-01 | model | `What is the status of the last literature task, and why did it fail if it failed?` | Host inspect card: `Task {id} status: **{status}** (…)`. Degraded is not a full success. Failed uses **Why it failed**, not System summary. Does not narrate success. |
| A-TSK-02 | model | `Which research tasks did I handle poorly in the last two days?` | `task.review` plus host footer `Reviewed tasks (authoritative status):`. Recovery candidates include `/task retry {id}`. |
| A-SCH-01 | model | `Every day at 6pm search for new RAG papers.` | `schedule_task`. Ambiguous time asks; no invented cron. |
| A-SCH-02 | offline | `List my schedules and run anything that is due.` | `list_schedules` plus `omni schedule run`. `run` fires due jobs in this process. `omni serve` is only for unattended fire. |
| A-SCH-03 | model | `Set a one-time schedule for today at 7:10 to prepare RAG survey materials: fetch the Attention Is All You Need abstract, generate a scientific architecture figure with query, retriever, reranker, and LLM, and write a paper.` | One-shot `--at`, not cron. Naive 7:10 is local wall-clock. If 7:10 already passed, the host refuses or asks for a future time — it does not silently create a past job. Goal is `agent-goal`. Does not fire without `omni serve` / `schedule run`. |
| A-SUB-01 | network | `In parallel: search RAG papers, draw an architecture figure, then write a short synthesis.` | Live ReAct (`run_skill` / `run_workflow` / `spawn_subagents`). No plan-time sealed DAG. If the model used `run_workflow`, `task show` lists ≥2 steps or executions (see E-01). |
| A-SUB-02 | model | `Do not spawn_subagents. Use only search_literature in this turn.` | Named native tool. Retrieve-only policy blocks spawn even without a Chinese deny parser. |
| A-FIL-01 | model | `Read cli/src/omni/agent/planner.py and say what the planning boundary owns.` | `read_file` inside the directory jail. |
| A-FIL-02 | model | `Open @draft.pdf and summarize the methods section.` | Attachment path works. Missing file asks; no invented `https://`. |
| A-DOC-01 | model | `How is storage implemented? Where do sessions, long-term memory, and artifacts live?` | `docs_search`. SQLite and filesystem. No `/Users` or `/home` leak. |
| A-DOC-02 | offline | `Reply OK only.` | Mock: `Offline mock model`. No tools. Matches `offline_mock_smoke`. |
| A-CHG-01 | model | `Review the last four days of git commits in this repository. Analyze new features, optimizations, problems solved, and anything that looks unreasonable. What were the optimization points?` | Uses local `git log` / files in the jail, or says git is unavailable. Structured: features / fixes / remaining risks. Does not invent SHAs. Not `search_literature` as the only action. |
| A-CHG-02 | model | `What changed in Omni for researchers in the latest commits: artifact output paths, literature debts, and the REPL /web command?` | Product-visible: files under `outputs/<title>_<task8>/` (not `artifact://` as the user path), honest remaining debts, `/web` in the background. `docs_search` or local docs. No fabricated changelog. |

### Steer, explain, ROM, verify, persona

| ID | Need | Input | Pass |
|---|---|---|---|
| A-CTL-01 | model | During retrieval: `/steer` use only OpenAlex and do not write a survey. | Same turn updates. Completed steps stay. |
| A-CTL-02 | model | During a long task: `/stop` | Cancel at the next boundary. Existing artifacts remain on `task show`. |
| A-WHY-01 | offline | `omni why` | Table: seq, event, summary, decision/detail. Covers route, plan, provider, settlement. |
| A-WHY-02 | offline | `omni current` | Focus table: session, skill, task, workflow, workflow_step, skill_execution, artifact, source. |
| A-ROM-01 | model | `Record the hypothesis: a dense retriever beats BM25 on long documents. Confidence 0.6.` | `record_hypothesis`. Visible on `omni hypo show`. |
| A-ROM-02 | model | `Bind that claim to source_id X with stance=supports and a one-sentence quote.` | `record_claim` plus `add_evidence`. Visible on `omni evidence list`. |
| A-VFY-01 | model | `omni verify --session <session>` | `--session` requires an id. Flags unsupported, contradicted, or overconfident claims. |
| A-VFY-02 | model | `Audit this statistic: p=0.03, n=12, claimed as a large-scale significant effect.` | `review_statistics` or `verify` flags the mismatch. |
| A-CON-01 | model | `Scan recorded claims for contradictions against the RAG sources we just cited.` | `evidence.contradiction_scan` or `omni verify`. Does not restart retrieval as the only action. |
| A-CON-02 | model | Same statistic audit as A-VFY-02 | Shared boundary: numeric overclaim is visible on `verify` / `review_statistics`. |
| A-PER-01 | offline | `omni soul list` | Discoverable personas. Offline. |
| A-PER-02 | model | `Rewrite the last related-work section in the active scientist persona, more cautious.` | Tone changes. Tool names do not. |
| A-KG-01 | model | `$scientist-kg-distiller` for Ada Lovelace, field computing, dry-run only. | Binds `scientist-kg-distiller` or `soul create --dry-run`. Dry-run shows the resolved request and does not install a KG. |

## Memory

| ID | Need | Input | Pass |
|---|---|---|---|
| M-WR-01 | model | `Remember: this project uses Vancouver citation style by default.` | `memory_update` or `remember`. `omni memory search Vancouver` hits. |
| M-WR-02 | offline | `omni memory add "Group-meeting decks stay at 12 slides or fewer."` | CLI write. No model. Visible on `memory list`. |
| M-RC-01 | model | New session: `What citation style do we use?` | Recalls Vancouver without the user restating it. |
| M-RC-02 | model | `Continue last week's latent-space intervention survey. Do not start from an empty search.` | Recovers notebook / task / source_ids. Extra search is allowed; old ids stay. |
| M-GR-01 | offline | `omni memory graph <id>` | Cross-session neighbors, or an empty graph. No invented edges. |
| M-GR-02 | offline | `omni memory profile` | Distilled user profile. Includes written preferences when present. |
| M-NB-01 | offline | `omni memory notebook` | Reads `NOTEBOOK.md`. |
| M-NB-02 | offline | `omni memory sync` | Imports marked bullets from `MEMORY.md` and `NOTEBOOK.md`. |
| C-MEM-03 | offline | `omni memory link <id1> <id2>` then `omni memory graph <id1>` | An edge is stored. Graph shows it. No invented edges. |
| C-MEM-04 | offline | `omni memory detail <id>` then `omni memory edit <id>` | Detail has full text and provenance. Edit is visible on list/search. |

## Stacked prompts (3 to 8 capabilities in one utterance)

| ID | Need | Input | Pass |
|---|---|---|---|
| X3-01 | network | `1) search_literature query='RAG evaluation benchmark 2024' rows=8, keep source_id only. 2) Remember I only want Vancouver. 3) Do not write a survey.` | Retrieve plus memory write. Tool remains `search_literature`. |
| X3-02 | network | `Fetch 1706.03762, draw a self-attention architecture figure, then run omni why.` | Fetch + figure + explainable route. Both debts remain. |
| X4-01 | network | `Search RAG factuality papers, write related work, draw an architecture figure, and record a claim with one evidence edge.` | Four live steps. Related work is `draft.section`; figure is not a deck. A failed search must not settle the figure as `succeeded` (same honesty rule as E-03). |
| X4-02 | model | `$research-pptx` for a group meeting, remember the 12-slide cap, `/mode plan` for an outline, then `task approve`. | Plan pauses. Same task id produces the pptx after approval. Memory stores the cap. |
| X5-01 | network | `Fetch 1706.03762, review it for NeurIPS, make a group-meeting deck, remember that future reviews use the NeurIPS bar, then omni why.` | Five-step live sequence. Not `SINGLE_SKILL_TASK`. |
| X5-02 | network | `search_literature RAG 2024 rows=8; search_corpus on hallucination; record_hypothesis one item; cite_source one item; do not spawn_subagents.` | Four ROM/retrieve tools stay callable. Spawn stays off. |
| X6-01 | network | `Search, write a survey, draw a figure, make a poster, record a hypothesis, and bind it to one source.` | Six debts. Poster and slides stay distinct capabilities. |
| X6-02 | model | `Review yesterday's tasks, recall Vancouver, run a follow-up search, revise the last figure, append the notebook, and verify claims.` | `task.review` + memory + search + revise + notebook + verify. |
| X7-01 | network | `Search RAG, write related work, architecture figure, group-meeting deck, a review, a weekly search schedule, and remember the 12-slide cap.` | Seven observable debts. Schedule asks or waits for approval. Plan mode must not hang unbounded. |
| X7-02 | network | `search_literature + fetch 1706.03762 + review + response letter + two follow-ups + bind evidence + /verify.` | ROM chain closes: source to claim/evidence to verify. |
| X8-01 | network | `Search, fetch full text, review, response letter, poster, ideation, weekly schedule, then ask what citation style I use.` | Eighth step must hit the stored preference. Schedule does not silently run a sensitive skill. |
| X8-02 | network | Same utterance: list `source_id` only and forbid `write_file` / `run_skill` / `spawn_subagents`, **and** also demand a survey, figure, deck, hypothesis, memory write, and `/why`. | Source-id-only scope wins this turn. Honest behavior: deliver ids and say writing needs a new turn, or ask. Must not sneak `run_skill`. |

## Survey packs (fetch + figure + paper + slides)

These are the live multi-deliverable packs. P-01 is the English form of
task `72590550`. A pack that stops at `find_skill` and leaves
`artifact.figure`, `draft.manuscript`, and `artifact.slides` unpaid **fails**.

| ID | Need | Input | Pass |
|---|---|---|---|
| P-01 | network | `Prepare materials for a RAG system survey: fetch the abstract of Attention Is All You Need, generate a scientific architecture figure that includes query, retriever, reranker, and LLM, and write a paper plus a slide deck.` | `arxiv-fetch` / `paper.fetch.arxiv` for the abstract. Figure is PNG/SVG with the four boxes, not a deck. Paper is `draft.manuscript`, not only `draft.section`. Slides are `.pptx` via `research-pptx`. Files land under `<out>/<title>_<task8>/` (this catalog: `outputs_walkthrough/…`), not a bare tree and not checkout-root `*_xxxxxxxx/`. Settlement remaining is empty for those three debts. Do not stop at `find_skill`. No second background-completion line. |
| P-02 | network | `Prepare materials for an agentic loop-engineering system survey and produce a detailed introductory slide deck.` | Literature plus a manuscript or long survey, plus a detailed `.pptx` (not a one-slide figure). Topic is loop engineering, not the P-01 RAG pack. |

## Long-horizon campaigns

These are multi-session campaigns over durable tasks, memory, and schedules.
They are **not** the same as one Task split into many Skill Executions
(that is E-01–E-04). Omni has no product type named "Great Wall"; the label
is only the user-facing name for this campaign shape.

Each turn still uses the [output-format family](#user-visible-output-format)
for that step: T0 retrieve-only lists `source_id` values; T1 survey pays
`draft.section`; T2 is a multi-slide `.pptx`, not a figure.

| ID | Need | Campaign | Pass |
|---|---|---|---|
| W-01 | network | T0: retrieve RAG 2024, keep `source_id` only. T1 new session: continue yesterday and write related work. T2: make a 12-slide deck from the survey. | Cross-session `source_id` / notebook / memory survive. T0 answer is the host `source_id` list. T1 is a manuscript, not an empty search as the only action. T2 is `.pptx`. T0/T1/T2 may be separate Task ids (campaign). Score E-01 if one of those turns itself grew ≥2 executions. |
| W-02 | network | Search Semantic Scholar without a key (expect `degraded`), `/steer` to OpenAlex, `task retry` the same goal. | The failed route is `degraded` / blocked, not `succeeded`. Steer updates the same Task. Retry keeps the original input and, if it targets an execution, creates a new execution id. Live line / skill card use `⚠` / `!`, not `✓` / `✅`. |
| W-03 | network | `/plan` compare three RAG retrievers, `task approve`, then `verify --session` and `omni why`. | Plan is bounded. No execution events before approve. Approval reuses the Task id. `verify --session` needs that session id. `omni why` is the seq/event/summary table for the same run. |
| W-04 | model | Write two memories (Vancouver, 12-slide cap), `memory link`, new session "what are my meeting rules?", then `memory graph`. | One recall returns both preferences. Graph shows only the stored edge. List/search stay truncated; `memory detail` has full text. |

## One task, many executions

One user request is one Task. Multi-step work is sequenced by the model
(`update_plan` / `run_skill` / `run_workflow` / `spawn_subagents`), not sealed
as a pre-execution DAG. When the model uses `run_workflow`, the user-visible
ledger is WorkflowRun → WorkflowStep → Skill Execution. A later `task retry`
keeps the step id and creates a new execution id.

| ID | Need | Input | Pass |
|---|---|---|---|
| E-01 | network | `Search RAG evaluation papers, write a short related-work paragraph, and draw one architecture figure. Keep all three on this same request.` | One Task id. `task show` lists a WorkflowRun with ≥2 steps, or ≥2 Skill Executions. Live lines use `execution=xxxxxxxx`. Related work is `draft.section`, not a title list. Figure is PNG/SVG, not a deck. Retrieve-only projection does not apply (writing and a figure are owed). |
| E-02 | network | `Fetch arXiv 1706.03762, review it as a NeurIPS reviewer, then make a 12-slide group-meeting deck from that review.` | One Task id. ≥2 durable executions (fetch / review / slides). `task subtask <task>` lists Skill Executions. `task step` needs `{workflow_run_id} {step_id}`; a lone Task id exits 2. Review body is in the turn, not `Created execution`. Deck is `.pptx`, not a figure. |

## Execution-level degrade and recover

A child that is `degraded` / `failed` / `needs_input` is not rewritten as
`succeeded`. Siblings that already delivered stay on the Task. The parent
settles `degraded` (or `needs_input` / `failed`) unless a later retry
superseded that child **and** every required output is already on this Task.

| ID | Need | Input | Pass |
|---|---|---|---|
| E-03 | network | `In one request: search RAG 2024 papers, write a two-paragraph related-work section, and make one editable LiveFigure PPTX slide. If LiveFigure cannot run, continue the rest.` | ≥2 executions. On a `novlm` home the LiveFigure row matches A-LF-03 (`vlm_not_configured`, not a silent `scientific-figure` swap). Search or `draft.section` may still succeed. Parent is not `succeeded` while that required slide is missing. `task show` is mixed. Live line is `⚠ … needs configuration` or `degraded`; card is `!`, not `✅`. |
| E-04 | network | After E-03 (or any mixed-status Task): `omni task subtask <task>` then `omni task retry <degraded-execution>` (or `omni task retry <workflow> --step <step>`). Then `omni task show <task>`. Optional: `omni task resume <execution>` if the row has a checkpoint; `omni task requeue` only with a standalone skill-execution id. | `subtask` lists each execution and status. Retry creates a **new** execution id, keeps the WorkflowStep id, and increments attempt. The original row remains (`retry_of`). Parent recommended action while still degraded is `omni task retry {short}`. Do not call the old degraded row `succeeded`. Resume continues from the checkpoint; requeue refuses a Task id. |
| E-05 | model | `livefigure` or `paper-review` fails; the model then `write_file`s a Markdown review or harvests a deck PPTX. | The failed execution stays `failed` / `error` / `needs_input`. Parent is `degraded` or `failed`, not `succeeded`. `review` / `artifact.pptx` remain on remaining. Leftover files are not a retry. |

## CLI and REPL

Each group has two user-facing scenarios. Aliases (`task delete` = `task rm`)
are not duplicated. Removed commands are out of scope.

| ID | Need | Input | Pass |
|---|---|---|---|
| C-CHAT-01 | model | `omni "Summarize the contributions of arXiv 2310.06825"` | Same path as `omni chat`. |
| C-CHAT-02 | offline | `omni profil list` | Unknown-command error. Not swallowed as a prompt. |
| C-INIT-01 | offline | `omni doctor` | Environment and config diagnostics. |
| C-INIT-02 | offline | `omni init --help` | Setup wizard help. Do not run the interactive wizard in CI. |
| C-MOD-01 | offline | `omni model status` | Main / vision / embedding and source layers. |
| C-MOD-02 | offline | `omni model explain` | Every effective field names its config layer. |
| C-MOD-03 | offline | `omni model help` | Lists status/explain/main/vision/embedding/use. |
| C-MOD-04 | offline | `omni model use --help` | Says whether the override is one-shot or persistent. Do not switch the owner's main model. |
| C-CFG-01 | offline | `omni config list` | Effective config. Secrets stay redacted. |
| C-CFG-02 | offline | `omni config path` then `omni config test` | Paths are printable. `test` hits the main model and names optional VLM / S2 / embeddings (live-probes VLM and S2 when they are set). A VLM site origin is expanded to chat/completions. A failure must name the reason. |
| C-CFG-03 | offline | `omni config get model.model` then `omni config home` | Prints the effective value and home. Does not write `~/.config/omni/home`. |
| C-CFG-04 | offline | `omni config --help` | Discover get/set/unset/test/path. Do not `set`/`unset` on a real home. |
| C-CFG-05 | offline | `omni config vlm` (no flags) then `omni config vlm --help` and `omni config embeddings --help` | Probe is read-only: enabled / model / endpoint / key-set. This is the VLM branch decision. `--help` is discoverable. Do not set or disable keys on a real home. |
| C-CFG-06 | offline | `omni config semantic-scholar --help` and `omni model vision --help` | Discoverable. Do not write keys. |
| C-PRJ-01 | offline | `omni project list` | Named projects and path-keyed workspaces. No `migrate` subcommand. |
| C-PRJ-02 | offline | `omni --project walkthrough-aug24 project info` | Store path agrees with `-P`. |
| C-PRJ-03 | offline | `omni project help` | Only list/new/info/help. No `migrate`. |
| C-PRJ-04 | offline | `omni project new --help` | Parameters are discoverable. Do not create another project when `walkthrough-aug24` already exists. |
| C-PRF-01 | offline | `omni profile list` | Profiles. |
| C-PRF-02 | offline | `omni profile show` | Active profile. |
| C-PRF-03 | offline | `omni profile add --help` and `omni profile use --help` | Parameters are discoverable. Do not switch the owner's profile. |
| C-SES-01 | offline | `omni session list` | Newest first. |
| C-SES-02 | offline | `omni session fork <id>` then `omni resume --last` | Fork prints a new session id. Bare fork opens the REPL; non-interactive runs stop after the success line. `resume --last` without a TTY tells the operator to pass an id. |
| C-SES-03 | offline | `omni session show <id>` | Matches the A-LIT-01 session. |
| C-SES-04 | offline | `omni session export <id>` | Export is printable. Do not delete other sessions. |
| C-SKL-01 | offline | `omni skills list` and `omni skills why literature.search` | Built-ins listed. `why` explains the winner. No `skills install`. |
| C-SKL-02 | offline | `omni skills add` a folder whose stem differs from SKILL.md `name`, then `skills trust <frontmatter-name>` | Destination uses the frontmatter name. |
| C-SKL-03 | offline | `omni skills info search_literature` or `omni skills info literature.search`, then `omni skills search rag` | Capability ids are not skill names. `literature.search` is expected to miss; search still lists built-ins. |
| C-SKL-04 | offline | `omni skills sources` and `omni skills examples` | Sources and examples are discoverable. Still no install/uninstall. |
| C-SKL-05 | offline | In the walkthrough project, write `walkthrough-echo/SKILL.md` with frontmatter `name: walkthrough-echo`, then `omni skills add walkthrough-echo` and `omni skills trust walkthrough-echo`, then `skills info walkthrough-echo` and `skills list --no-pager` | Destination uses the frontmatter name. Trusted skill is listed and info-able. This is add+trust, not `skills install`. |
| C-SKL-06 | offline | After C-SKL-05: `omni skills setup --help` and `omni skills export --help`, then `omni skills untrust walkthrough-echo` and `omni skills remove walkthrough-echo` | Setup lists `research-pptx`, `builtin-personas`, `all`. Export is not add. Untrust/remove clean up the walkthrough skill. Do not export onto the owner's Claude/Codex. |
| C-MEM-01 | offline | `omni memory list` and `omni memory path` | Offline. |
| C-MEM-02 | offline | `omni memory pin <id>` then `omni memory clear` without `--yes` | Pin succeeds. Clear without `--yes` exits 1 and keeps rows (there is no `--dry-run` flag). |
| C-TSK-01 | offline | `omni task list` and `omni task inbox` | Status and notifications. |
| C-TSK-02 | model | After a live task exists: `omni task steer <id> "Use only OpenAlex"` then `cancel` / `retry` | Steer persists. Cancel keeps finished artifacts. Retry is a new attempt with the same input. If the id is a Skill Execution, the new row has a new execution id (see E-04). |
| C-TSK-03 | offline | `omni task all --limit 8` | Cross-workspace index. Footer is `Showing N of M`. `--limit 0` prints all. Default kind is `all`. |
| C-TSK-04 | offline | `omni task session <sid>` and `omni task show <id>` | Session filter. Show has `object_kind=task`, plan / settlement / remaining, workflow runs, workflow steps, skill executions, activity. Failed tools stay visible. |
| C-TSK-05 | offline | `omni task step --help` then `omni task subtask <task>` | `task step` needs `{workflow_run_id} {step_id}`. A lone task id exits 2. `subtask` lists only Skill Executions; a turn with none is a clear empty. After E-01 this list has ≥2 rows. |
| C-TSK-06 | offline | `omni task watch <id>` on a finished task, then `omni task inbox` | Finished watch returns the terminal state. Do not `cancel`/`drain` another operator's running task. |
| C-TSK-07 | offline | `omni task attach --help` | Attach restores a result boundary into a session. The shell form needs `--session`. Do not attach over someone else's live session. |
| C-TSK-08 | offline | `omni task archive --help` and `omni task drain --help` | Help only on a real home. Do not archive or drain another operator's running task. |
| C-ART-01 | offline | `omni artifacts preview <id>` or a missing id | Metadata plus a text prefix, or a clear miss. No crash. |
| C-ART-02 | offline | `omni artifacts versions <id>` and `omni artifacts review <id>` | Lineage and reproducibility evidence when an artifact exists; a missing id is a clear miss. |
| C-ART-03 | offline | `omni artifacts diff <id1> <id2>` or a missing id | A delta when versions exist; a clear miss otherwise. |
| C-ART-04 | offline | `omni artifacts help` | Subcommands include preview/diff/versions/review. |
| C-CIT-01 | offline | `omni cite list` and `omni source list` | Library and structured sources. |
| C-CIT-02 | offline | `omni cite export --format bibtex` and `omni source reindex` | Empty library is a warning, not a crash. Reindex is idempotent. |
| C-CIT-03 | offline | After a retrieve: `omni source show <id>` | Shows the structured source. A missing id is a clear miss. |
| C-HYP-01 | offline | `omni hypo new "Dense retriever beats BM25 on long docs" -c 0.6` then `omni hypo list` | Hypothesis is stored. |
| C-HYP-02 | offline | `omni claim new "..."` then `omni evidence help` and `omni run list` | Claim lands. Evidence bind needs a real `source_id` after retrieval. Run ledger may be empty. |
| C-HYP-03 | offline | `omni hypo show <id>` and `omni hypo status` | Show and status work on a stored hypothesis. A missing id is a clear miss. |
| C-LITC-01 | model | `omni lit "How does RAG reduce hallucination?"` | Same entry as A-COR-01. |
| C-LITC-02 | offline | `omni bench --k 3` and `omni eval --research-quality` | Offline metrics. `omni eval --black-box` with zero successes must exit 1. |
| C-SCH-01 | offline | `omni schedule list` and `omni schedule proposals` | Next fire and pending approvals. |
| C-SCH-02 | offline | `omni schedule add --cron "0 18 * * *" --goal "Search for new RAG papers"` then `omni schedule run` | Exactly one trigger mode. `run` fires due jobs now; `omni serve` is for unattended repeats. |
| C-SCH-03 | offline | `omni schedule all` and `omni schedule show <id>` | All-workspace list. A missing id is a clear miss. |
| C-SCH-04 | offline | On a schedule created for this walkthrough: `schedule disable <id>` then `enable <id>` | State flips. `approve`/`deny`/`clarifications` only hit a real proposal. |
| C-SCH-05 | offline | `omni schedule add --at 2026-08-26T07:10 --goal "Prepare RAG survey materials: fetch the Attention Is All You Need abstract, draw a query/retriever/reranker/LLM figure, write a paper."` | Exactly one trigger. Naive `--at` is local wall-clock. If 07:10 already passed, the command exits non-zero with guidance — it does not silently create a past job. A future `--at` appears on `schedule list` / `show`. |
| C-SCH-06 | offline | `omni schedule add --at 1999-01-01T07:10 --goal "past job"` then, on a walkthrough job that exists, `omni schedule remove <id>` | Past `--at` is refused. `remove` deletes only the walkthrough job. |
| C-SRV-01 | offline | `omni status` and `omni serve status` and `omni serve doctor` | Workspace, db, daemon. Bind address is not `0.0.0.0`. |
| C-SRV-02 | offline | `omni web start` then fetch `127.0.0.1:1088` then `omni web stop` | Loopback only. Same store as `-P`. Do not leave the process running. |
| C-SRV-03 | offline | `omni web status` and `omni web help` | Status is honest when the UI is down. Bare `web port` requires `{port}` and is a setter, not a getter. |
| C-CH-01 | offline | `omni channel list` and `omni channel test wechat` | List works offline. Test fails closed when the channel is unconfigured. |
| C-CH-02 | offline | Do not run `omni channel login wechat --start` in this catalog | QR login is interactive. Record as blocked. |
| C-MCP-01 | offline | `omni mcp list` and `omni trust --help` | External MCP and trusted directories. |
| C-MCP-02 | offline | `omni exec --help` and `omni replay --help` | Non-interactive run and chronological replay. |
| C-MCP-03 | offline | `omni mcp help` and `omni mcp agents` | Help and agent targets. |
| C-MCP-04 | offline | `omni mcp serve --help` and `omni mcp install --help` | Help only. Do not install into the owner's Claude/Codex. |
| C-EXE-01 | model | `omni exec --help` then `omni exec "Reply OK only."` | Non-interactive run. Mock: `Offline mock model`. Trusted walkthrough writes under `--out outputs_walkthrough`, not the checkout root. |
| C-EXE-02 | offline | `omni replay --help` then `omni replay <session>` | Chronological Q&A plus tool trace. Do not delete the session. |
| C-TRU-01 | offline | `omni trust --help` and `omni trust --list` | Trusted directories listed. Do not revoke the walkthrough project. |
| C-SOUL-01 | offline | `omni soul status` and `omni soul list` | Active persona and scanner root. |
| C-SOUL-02 | offline | `omni soul help` | Lifecycle. |
| C-SOUL-03 | offline | `omni soul create "Ada Lovelace" --field computing --dry-run` | Prints the resolved request. Does not install a KG. |
| C-AS-01 | offline | `omni autosota info` and `omni autosota doctor` | Wrapper. No secrets in output. |
| C-AS-02 | offline | `omni autosota status` | Does not create an Omni background task. |
| C-OPS-01 | offline | `omni update status` and `omni terminal status` | Update state. Shift+Enter readiness. |
| C-OPS-02 | offline | `omni uninstall --dry-run` | Preview only. |
| C-TERM-01 | offline | `omni terminal setup --help` and `omni terminal setup --check` | Preview only. Does not write tmux config. |
| C-REPL-01 | offline | `omni --help` | Slash verbs exist as Typer groups or in-process REPL commands. |
| C-REPL-02 | offline | `/help` text in `omni --help` and `cli/src/omni/cli/repl_commands.py` | `/inbox` is in the catalog. `/exit` and `/quit` leave cleanly. |
| C-REPL-03 | offline | REPL: `/context` then `/compact` | Context reports budget and injected sections. Compact reports savings or "nothing to compact". |
| C-REPL-04 | offline | REPL: `/copy` then `/verbose quiet` then `/debug on` / `/debug off` | Copy is honest outside the dock. Verbose/debug take effect immediately. |
| C-REPL-05 | offline | REPL: `/clear` then `/new` | Context clears; tasks and memory stay. `/new` changes session without wiping scrollback. |
| C-REPL-06 | offline | REPL: `/inbox` then `/task all --limit 5` | In-process inspect. Must not fork-and-hang. |
| C-REPL-07 | offline | REPL: `/web status` then `/web start` then `/web stop` | `/web` runs in the background. Loopback only. Do not leave the process running. |

`A-LIT-01` is the release-blocker named-tool contract. Public docs keep the
English wording above. The original rc5 walkthrough used a Chinese phrasing of
the same contract (named `search_literature`, `rows=8`, list `source_id` only,
no survey, no `write_file` / `run_skill` / `spawn_subagents`). Live execution
may use that Chinese phrasing; do not paste it into `cli/docs`.

Later user-perspective walkthroughs use **this file only**: the specification
table, the named-prompt map, the coverage inventory, the output-format
families, and the case rows. Do not open a parallel catalog for campaigns,
multi-execution tasks, degrade/recover, survey packs, or third-party skills.

## Cases this catalog refuses

- `omni project migrate`
- `omni skills install` / `omni skills uninstall` as the way to **add** a
  third-party skill (those names are export/unexport aliases). Third-party
  load is `skills add` + `skills trust` (C-SKL-05, A-SKL-03).
- A host grammar that parses "do not use X" from Chinese or English prose.
  Deny lists are `ToolPolicy` and interaction mode, not an NL parser.
- Hidden installer hooks: `upgrade`, `terminal-setup`, `_record-install`,
  `_converge-install`.

## Related docs

- [cli-validation-guide.md](cli-validation-guide.md) — CLI/REPL loops and plan-mode approval
- [agent-validation-guide.md](agent-validation-guide.md) — skills, workflows, ROM, memory
- [testing-and-evaluation.md](testing-and-evaluation.md) — pytest and `omni eval`
- [commands.md](commands.md) — full command reference
