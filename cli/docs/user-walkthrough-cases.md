# User Walkthrough Catalog

Copy-pasteable **user-facing** cases for the current OmniScientist command and
agent surface. Every case maps to a command, native ReAct tool, built-in skill,
or durable store that exists in this tree. The catalog does not invent
`omni project migrate`, `omni skills install` / `uninstall`, or a natural-language
allow/deny parser.

The specification table below is the operator index: one functional point,
at least two case IDs, and what the user is checking. Detailed inputs live
in the sections after it.

Use a dedicated named project so walkthrough artifacts stay off real work.
From a source checkout, put `cli/src` first on `PYTHONPATH` so the command
hits this tree rather than a previously installed wheel (do not run
`omni update --local` just to pick up local edits):

```bash
PYTHONPATH=cli/src .venv/bin/omni --project walkthrough-aug24 --trust --out .
```

The offline `mock` provider is enough for CLI help, list, status, and
`offline_mock_smoke`. Named literature retrieval, surveys, figures, decks, and
stacked prompts need a configured model and usually the network.

## How to read a case

| Column | Meaning |
|---|---|
| ID | Stable handle for comparison |
| Need | `offline` / `model` / `network` / `serve` |
| Input | Exact user text or shell command |
| Pass | Observable contract, not a vibe |

Pass/fail on three columns: **ID**, **tool or subcommand that actually ran**,
**whether a second assistant line appeared**.

`A-LIT-01` is the release-blocker regression for named `search_literature`.
`rows` is a per-connector cap; the deduped union may exceed that number.
If the host spills `source_ids` to disk, `read_file` on that path must succeed
and a jail denial must not be recorded as `succeeded` / `result_success=true`.
`X7` / `X8` must stay a live sequence against tool results, not one
`SINGLE_SKILL_TASK`. `X8-02` is an intentional contradiction: retrieve-only
host policy must still block `write_file` / `run_skill` / `spawn_subagents`.

## Specification table

| Point | Case IDs | Need | What the user checks |
|---|---|---|---|
| Named `search_literature` | A-LIT-01, A-LIT-02 | network | Named native tool stays in this turn. Reply is `source_id` values. No `run_skill`. No second background-completion line. |
| Natural-language literature search | A-LIT-03, A-LIT-04 | network | Lone `literature.search` stays ReAct + `search_literature`. Settlement uses `source_ids`. |
| Retrieval plus written survey | A-SUR-01, A-SUR-02 | network | Survey pair: host retrieve then native synthesis. Manuscript debt is paid. |
| Local corpus QA | A-COR-01, A-COR-02 | model | `omni lit` / `search_corpus` with `[S#]`. Empty library does not invent a DOI. |
| arXiv fetch | A-ARX-01, A-ARX-02 | network | `$arxiv-fetch` / `paper.fetch.arxiv` produces a local paper artifact. |
| Paper review | A-REV-01, A-REV-02 | model/network | `paper-review` delivers the body, not a receipt. Missing file asks. |
| Review response | A-RR-01, A-RR-02 | model | `review-response` writes a response letter from the prior review. |
| Scientific figure | A-FIG-01, A-FIG-02 | model | `scientific-figure` then in-place `artifact.revise`. Not a deck. |
| Slides | A-SLD-01, A-SLD-02 | model | `research-pptx` / `artifact.slides`. Not a one-slide figure. |
| Poster | A-POS-01, A-POS-02 | model | `scientific-poster`. Distinct from slides. |
| Research ideation | A-IDE-01, A-IDE-02 | model/network | `research-ideation` proposes testable follow-ups. |
| Explicit `$skill` | A-SKL-01, A-SKL-02 | model/network | `$name` binds that skill. Unknown `$name` falls through to ReAct. |
| Plan mode | A-PLN-01, A-PLN-02 | offline/model | `omni chat --mode plan` or `/mode plan`. Bounded; approval reuses the task id. |
| Review mode | A-RD-01, A-RD-02 | model | Read-only tools. No `write_file` / `run_skill`. |
| Task inspect and retrospective | A-TSK-01, A-TSK-02 | model | One-task status vs multi-task review with an authoritative footer. |
| Schedules | A-SCH-01, A-SCH-02 | model/serve | `schedule_task` asks when time is ambiguous. Fire needs `omni serve`. |
| Subagents and workflows | A-SUB-01, A-SUB-02 | model/network | Multi-step work sequences live. Retrieve-only can block `spawn_subagents`. |
| Local files | A-FIL-01, A-FIL-02 | model | `read_file` / `@attachment`. Missing file asks. |
| Product self-knowledge | A-DOC-01, A-DOC-02 | model/offline | `docs_search` or honest mock. No absolute home-path leak. |
| Steer and stop | A-CTL-01, A-CTL-02 | model | `/steer` updates the turn. `/stop` cancels at the next boundary. |
| Explain and focus | A-WHY-01, A-WHY-02 | offline | `omni why` / `omni current`. |
| Hypothesis, claim, evidence | A-ROM-01, A-ROM-02 | model | ROM tools persist; CLI `hypo` / `evidence` can show them. |
| Verification | A-VFY-01, A-VFY-02 | model | `omni verify` and statistical audit. |
| Contradiction scan | A-CON-01, A-CON-02 | model | `evidence.contradiction_scan` or `review_statistics`. |
| Scientist persona | A-PER-01, A-PER-02 | offline/model | `omni soul` lists personas. Persona changes tone, not tool names. |
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
| Long-horizon campaign | W-01, W-02, W-03, W-04 | model/network | Cross-session continue, failed-route steer, plan-approve-verify, memory graph. |
| One-shot and unknown command | C-CHAT-01, C-CHAT-02 | model/offline | Bare `omni "..."` vs a mistyped command. |
| Init and doctor | C-INIT-01, C-INIT-02 | offline | `omni doctor` / `omni init --help`. |
| Config | C-CFG-01, C-CFG-02, C-CFG-03, C-CFG-04 | offline | `config list` / `path` / `test` / `get` / `home`. Do not `set` on a real home. |
| Project | C-PRJ-01, C-PRJ-02, C-PRJ-03, C-PRJ-04 | offline | `project list` / `info` / `help` / `new --help`. No `migrate`. |
| Profile | C-PRF-01, C-PRF-02 | offline | `profile list` / `show`. Missing default profile exits 1. |
| Session | C-SES-01, C-SES-02, C-SES-03, C-SES-04 | offline | `session list` / `fork` / `resume` / `show` / `export`. |
| Skills CLI | C-SKL-01, C-SKL-02, C-SKL-03, C-SKL-04 | offline | `skills list` / `why` / `info` / `search` / `sources` / `examples`. Local add uses SKILL.md `name`. |
| Model stack | C-MOD-01, C-MOD-02, C-MOD-03, C-MOD-04 | offline | `model status` / `explain` / `help` / `use --help`. |
| Memory CLI | C-MEM-01, C-MEM-02, C-MEM-03, C-MEM-04 | offline | `list` / `path` / `pin` / `clear` / `link` / `graph` / `detail` / `edit`. |
| Task CLI | C-TSK-01, C-TSK-02, C-TSK-03, C-TSK-04, C-TSK-05, C-TSK-06 | offline/model | Observe (`list` / `all` / `session` / `show` / `inbox`) then control (`steer` / `cancel` / `retry`). |
| Artifacts CLI | C-ART-01, C-ART-02, C-ART-03, C-ART-04 | offline | `preview` / `versions` / `review` / `diff` / `help`. |
| Cite and source | C-CIT-01, C-CIT-02 | offline | `cite list` / `export`, `source list` / `reindex`. |
| ROM CLI | C-HYP-01, C-HYP-02 | offline | `hypo` / `claim` / `evidence` / `run`. |
| Lit, bench, eval | C-LITC-01, C-LITC-02 | model/offline | `omni lit`. Black-box with zero successes exits 1. |
| Schedule CLI | C-SCH-01, C-SCH-02, C-SCH-03, C-SCH-04 | offline/serve | `list` / `all` / `show` / `proposals` / `add` / `run` / `enable` / `disable`. |
| Serve, status, web | C-SRV-01, C-SRV-02, C-SRV-03 | offline | Loopback only. Same store as `--project`. |
| Channels | C-CH-01, C-CH-02 | offline | `list` / `test`. QR login is interactive (blocked here). |
| MCP, trust, exec, replay | C-MCP-01, C-MCP-02, C-MCP-03 | offline | Help, list, and `mcp agents`. |
| Soul CLI | C-SOUL-01, C-SOUL-02 | offline | `status` / `list` / `help`. |
| AutoSOTA wrapper | C-AS-01, C-AS-02 | offline | `info` / `doctor` / `status`. No Omni background task. |
| Update, terminal, uninstall | C-OPS-01, C-OPS-02 | offline | Status plus `uninstall --dry-run`. |
| REPL verbs | C-REPL-01, C-REPL-02, C-REPL-03, C-REPL-04, C-REPL-05, C-REPL-06 | offline | `/help` plus `/context` `/compact` `/copy` `/verbose` `/debug` `/clear` `/new` `/inbox` `/task all`. |

## Conversation and research

### Named `search_literature`

| ID | Need | Input | Pass |
|---|---|---|---|
| A-LIT-01 | network | `omni --project walkthrough-aug24 --trust --out . "Call search_literature, query='large language model retrieval augmented generation survey 2024', rows=8. After retrieval do not write a survey; list hit source_id values only. Do not use write_file, run_skill, or spawn_subagents."` | Trace is `search_literature(query, rows=8)`. Reply lists `source_id` values. `rows` is per connector. No `run_skill`, `write_file`, or `spawn_subagents`. No second `[Background skill execution completed]` line. A spilled `source_ids` path must be readable; a jail denial is `rejected` / blocked, not `succeeded`. |
| A-LIT-02 | network | `Call search_literature with query='transformer attention survey 2017' and rows=5. After retrieval list source_ids only.` | Same native-tool path as A-LIT-01; English naming must not compile `openalex-search` as `run_skill`. |

### Natural-language literature retrieval

| ID | Need | Input | Pass |
|---|---|---|---|
| A-LIT-03 | network | `Search 2024 RAG factuality papers and give citeable source_id values only. Do not write a survey.` | ReAct plus `search_literature`. Empty `selected_skills`. Host blocks `run_skill`. |
| A-LIT-04 | network | `Find recent papers on privacy attacks against federated learning. List titles and source_id values.` | Settlement pays the `sources` debt with `source_ids`, not a title `summary`. |

### Retrieval plus written survey

| ID | Need | Input | Pass |
|---|---|---|---|
| A-SUR-01 | network | `Survey how latent-space intervention improves LLM agentic ability and write a related-work section.` | Survey pair: host retrieve plus native synthesis. Verification includes `draft.section`. |
| A-SUR-02 | network | `Write a short related-work section on retrieval-augmented generation evaluation benchmarks.` | Literature hits plus a manuscript artifact; not a title list alone. |

### Local corpus QA

| ID | Need | Input | Pass |
|---|---|---|---|
| A-COR-01 | model | `omni lit "How does RAG reduce hallucination?"` | `search_corpus`. Inline `[S#]`. No invented sources. |
| A-COR-02 | model | `From papers already in my library, which RAG evaluation benchmarks exist? Mark [S#].` | Only in-library chunks. Empty library says so; no fabricated DOI. |

### arXiv fetch

| ID | Need | Input | Pass |
|---|---|---|---|
| A-ARX-01 | network | `Fetch the full text of arXiv 1706.03762 and tell me the contributions.` | `paper.fetch.arxiv` / `$arxiv-fetch`. Local PDF or text artifact. |
| A-ARX-02 | network | `$arxiv-fetch Attention Is All You Need` | Explicit skill protocol binds `arxiv-fetch`. |

### Paper review and response letter

| ID | Need | Input | Pass |
|---|---|---|---|
| A-REV-01 | network | `Review arXiv 1706.03762 as a NeurIPS reviewer.` | `review.paper` / `paper-review`. Foreground drain delivers the review body, not `Created execution`. |
| A-REV-02 | model | `$paper-review` the workspace file `draft.pdf` | Explicit skill. Missing file becomes `needs_input`; no invented URL. |
| A-RR-01 | model | `Write a response letter from the review you just produced.` | `review.response`. Does not restart retrieval. |
| A-RR-02 | model | `$review-response` reply to reviewer 2 on novelty, point by point. | Explicit skill. `response_letter` artifact. |

### Figure, slides, poster, ideation

| ID | Need | Input | Pass |
|---|---|---|---|
| A-FIG-01 | model | `Draw the most common RAG architecture diagram.` | `artifact.figure` / `scientific-figure`. PNG/SVG. Not a deck. |
| A-FIG-02 | model | `Change the previous figure's retriever to hybrid. Keep the color language.` | `artifact.revise` in place, or a full redraw if there is no active figure. |
| A-SLD-01 | model | `Make a group-meeting deck from the Transformer paper.` | `slides.generate` / `research-pptx`. `.pptx`. Not a one-slide figure. |
| A-SLD-02 | model | `$research-pptx` thesis-defense deck on RAG factuality. | Explicit skill. Debt is `artifact.slides`. |
| A-POS-01 | model | `Make a scientific poster summarizing RAG evaluation benchmarks.` | `poster.scientific` / `scientific-poster`. Not slides. |
| A-POS-02 | model | `$scientific-poster` from the survey we just wrote. | Explicit skill. `artifact.poster`. |
| A-IDE-01 | network | `From the RAG papers we just retrieved, propose 3 testable follow-ups.` | `research.ideation`. Does not stop at another literature search. |
| A-IDE-02 | model | `$research-ideation` stress-test "latent-space intervention improves agents". | Explicit ideation. Claims and risks, not a paper list. |

### Explicit `$skill`, plan mode, review mode

| ID | Need | Input | Pass |
|---|---|---|---|
| A-SKL-01 | network | `$openalex-search federated learning privacy` | `SINGLE_SKILL_TASK` plus `openalex-search`. Skill protocol wins over a later `search_literature` token. |
| A-SKL-02 | model | `$not-a-real-skill` search the literature | Missing skill falls through to bounded ReAct. |
| A-PLN-01 | offline | `omni chat --mode plan "Plan how to compare RAG retrieval strategies. Do not execute."` | `--mode` is a `chat` flag, not a root flag. No unbounded `propose`. `awaiting_approval`. `search_literature` and `run_skill` blocked. |
| A-PLN-02 | model | `/mode plan` then: fetch 1706.03762, write a survey, make slides. Execute only after approval. | Plan persists first. `omni task approve` reuses the same task id. |
| A-RD-01 | model | `/review` Check whether "RAG always reduces hallucination" is over-claimed. | Read-only tools. No `write_file` or `run_skill`. |
| A-RD-02 | model | `/mode review` Why was the last task degraded? | Reads `get_task` / events. Does not mutate artifacts. |

### Tasks, schedules, subagents, files, product docs

| ID | Need | Input | Pass |
|---|---|---|---|
| A-TSK-01 | model | `What is the status of the last literature task, and why did it fail if it failed?` | `task.inspect` / `get_task`. Does not narrate success. |
| A-TSK-02 | model | `Which research tasks did I handle poorly in the last two days?` | `task.review` plus an authoritative status footer. |
| A-SCH-01 | model | `Every day at 6pm search for new RAG papers.` | `schedule_task`. Ambiguous time asks; no invented cron. |
| A-SCH-02 | serve | `List my schedules and run anything that is due.` | `list_schedules` plus `omni schedule run`. Fires only with `omni serve`. |
| A-SUB-01 | network | `In parallel: search RAG papers, draw an architecture figure, then write a short synthesis.` | Live ReAct (`run_skill` / `run_workflow` / `spawn_subagents`). No plan-time sealed DAG. |
| A-SUB-02 | model | `Do not spawn_subagents. Use only search_literature in this turn.` | Named native tool. Retrieve-only policy blocks spawn even without a Chinese deny parser. |
| A-FIL-01 | model | `Read cli/src/omni/agent/planner.py and say what the planning boundary owns.` | `read_file` inside the directory jail. |
| A-FIL-02 | model | `Open @draft.pdf and summarize the methods section.` | Attachment path works. Missing file asks; no invented `https://`. |
| A-DOC-01 | model | `How is storage implemented? Where do sessions, long-term memory, and artifacts live?` | `docs_search`. SQLite and filesystem. No `/Users` or `/home` leak. |
| A-DOC-02 | offline | `Reply OK only.` | Mock: `Offline mock model`. No tools. Matches `offline_mock_smoke`. |

### Steer, explain, ROM, verify, persona

| ID | Need | Input | Pass |
|---|---|---|---|
| A-CTL-01 | model | During retrieval: `/steer` use only OpenAlex and do not write a survey. | Same turn updates. Completed steps stay. |
| A-CTL-02 | model | During a long task: `/stop` | Cancel at the next boundary. Existing artifacts remain on `task show`. |
| A-WHY-01 | offline | `omni why` | Latest task route, plan, provider, settlement. |
| A-WHY-02 | offline | `omni current` | Active paper / artifact / task / source focus. |
| A-ROM-01 | model | `Record the hypothesis: a dense retriever beats BM25 on long documents. Confidence 0.6.` | `record_hypothesis`. Visible on `omni hypo show`. |
| A-ROM-02 | model | `Bind that claim to source_id X with stance=supports and a one-sentence quote.` | `record_claim` plus `add_evidence`. Visible on `omni evidence list`. |
| A-VFY-01 | model | `omni verify --session <session>` | `--session` requires an id. Flags unsupported, contradicted, or overconfident claims. |
| A-VFY-02 | model | `Audit this statistic: p=0.03, n=12, claimed as a large-scale significant effect.` | `review_statistics` or `verify` flags the mismatch. |
| A-CON-01 | model | `Scan recorded claims for contradictions against the RAG sources we just cited.` | `evidence.contradiction_scan` or `omni verify`. Does not restart retrieval as the only action. |
| A-CON-02 | model | Same statistic audit as A-VFY-02 | Shared boundary: numeric overclaim is visible on `verify` / `review_statistics`. |
| A-PER-01 | offline | `omni soul list` | Discoverable personas. Offline. |
| A-PER-02 | model | `Rewrite the last related-work section in the active scientist persona, more cautious.` | Tone changes. Tool names do not. |

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
| X4-01 | network | `Search RAG factuality papers, write related work, draw an architecture figure, and record a claim with one evidence edge.` | Four live steps. A failed search must not settle the figure as `succeeded`. |
| X4-02 | model | `$research-pptx` for a group meeting, remember the 12-slide cap, `/mode plan` for an outline, then `task approve`. | Plan pauses. Same task id produces the pptx after approval. Memory stores the cap. |
| X5-01 | network | `Fetch 1706.03762, review it for NeurIPS, make a group-meeting deck, remember that future reviews use the NeurIPS bar, then omni why.` | Five-step live sequence. Not `SINGLE_SKILL_TASK`. |
| X5-02 | network | `search_literature RAG 2024 rows=8; search_corpus on hallucination; record_hypothesis one item; cite_source one item; do not spawn_subagents.` | Four ROM/retrieve tools stay callable. Spawn stays off. |
| X6-01 | network | `Search, write a survey, draw a figure, make a poster, record a hypothesis, and bind it to one source.` | Six debts. Poster and slides stay distinct capabilities. |
| X6-02 | model | `Review yesterday's tasks, recall Vancouver, run a follow-up search, revise the last figure, append the notebook, and verify claims.` | `task.review` + memory + search + revise + notebook + verify. |
| X7-01 | network | `Search RAG, write related work, architecture figure, group-meeting deck, a review, a weekly search schedule, and remember the 12-slide cap.` | Seven observable debts. Schedule asks or waits for approval. Plan mode must not hang unbounded. |
| X7-02 | network | `search_literature + fetch 1706.03762 + review + response letter + two follow-ups + bind evidence + /verify.` | ROM chain closes: source to claim/evidence to verify. |
| X8-01 | network | `Search, fetch full text, review, response letter, poster, ideation, weekly schedule, then ask what citation style I use.` | Eighth step must hit the stored preference. Schedule does not silently run a sensitive skill. |
| X8-02 | network | Same utterance: list `source_id` only and forbid `write_file` / `run_skill` / `spawn_subagents`, **and** also demand a survey, figure, deck, hypothesis, memory write, and `/why`. | Retrieve-only policy wins this turn. Honest behavior: deliver ids and say writing needs a new turn, or ask. Must not sneak `run_skill`. |

## Long-horizon campaigns

These are multi-turn campaigns over durable tasks, memory, and schedules.
Omni has no product type named "Great Wall"; the label is the user-facing
name for that long-horizon shape.

| ID | Need | Campaign | Pass |
|---|---|---|---|
| W-01 | network | T0: retrieve RAG 2024, keep `source_id` only. T1 new session: continue yesterday and write related work. T2: make a 12-slide deck from the survey. | Cross-session source_ids / notebook / memory survive. T1 is not an empty search as the only action. |
| W-02 | network | Search Semantic Scholar without a key (expect `degraded`), `/steer` to OpenAlex, `task retry` the same goal. | One failed route is not the whole turn. Retry keeps the original input. Do not call `degraded` `succeeded`. |
| W-03 | network | `/plan` compare three RAG retrievers, `task approve`, then `verify --session` and `omni why`. | Plan is bounded. Approval reuses the task id. Verify and why point at the same run. |
| W-04 | model | Write two memories (Vancouver, 12-slide cap), `memory link`, new session "what are my meeting rules?", then `memory graph`. | One recall returns both preferences. Graph has an edge. |

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
| C-CFG-02 | offline | `omni config path` then `omni config test` | Paths are printable. `test` hits the configured endpoint; a failure must name the reason. |
| C-CFG-03 | offline | `omni config get model.model` then `omni config home` | Prints the effective value and home. Does not write `~/.config/omni/home`. |
| C-CFG-04 | offline | `omni config --help` | Discover get/set/unset/test/path. Do not `set`/`unset` on a real home. |
| C-PRJ-01 | offline | `omni project list` | Named projects and path-keyed workspaces. No `migrate` subcommand. |
| C-PRJ-02 | offline | `omni --project walkthrough-aug24 project info` | Store path agrees with `-P`. |
| C-PRJ-03 | offline | `omni project help` | Only list/new/info/help. No `migrate`. |
| C-PRJ-04 | offline | `omni project new --help` | Parameters are discoverable. Do not create another project when `walkthrough-aug24` already exists. |
| C-PRF-01 | offline | `omni profile list` | Profiles. |
| C-PRF-02 | offline | `omni profile show` | Active profile. |
| C-SES-01 | offline | `omni session list` | Newest first. |
| C-SES-02 | offline | `omni session fork <id>` then `omni resume --last` | Fork prints a new session id. Bare fork opens the REPL; non-interactive runs stop after the success line. `resume --last` without a TTY tells the operator to pass an id. |
| C-SES-03 | offline | `omni session show <id>` | Matches the A-LIT-01 session. |
| C-SES-04 | offline | `omni session export <id>` | Export is printable. Do not delete other sessions. |
| C-SKL-01 | offline | `omni skills list` and `omni skills why literature.search` | Built-ins listed. `why` explains the winner. No `skills install`. |
| C-SKL-02 | offline | `omni skills add` a folder whose stem differs from SKILL.md `name`, then `skills trust <frontmatter-name>` | Destination uses the frontmatter name. |
| C-SKL-03 | offline | `omni skills info search_literature` or `omni skills info literature.search`, then `omni skills search rag` | Capability ids are not skill names. `literature.search` is expected to miss; search still lists built-ins. |
| C-SKL-04 | offline | `omni skills sources` and `omni skills examples` | Sources and examples are discoverable. Still no install/uninstall. |
| C-MEM-01 | offline | `omni memory list` and `omni memory path` | Offline. |
| C-MEM-02 | offline | `omni memory pin <id>` then `omni memory clear` without `--yes` | Pin succeeds. Clear without `--yes` exits 1 and keeps rows (there is no `--dry-run` flag). |
| C-TSK-01 | offline | `omni task list` and `omni task inbox` | Status and notifications. |
| C-TSK-02 | model | After a live task exists: `omni task steer <id> "Use only OpenAlex"` then `cancel` / `retry` | Steer persists. Cancel keeps finished artifacts. Retry is a new attempt with the same input. |
| C-TSK-03 | offline | `omni task all --limit 8` | Cross-workspace index. Footer is `Showing N of M`. `--limit 0` prints all. Default kind is `all`. |
| C-TSK-04 | offline | `omni task session <sid>` and `omni task show <id>` | Session filter. Show has plan/cost/activity. Failed tools stay visible. |
| C-TSK-05 | offline | `omni task step --help` then `omni task subtask <task>` | `task step` needs `{workflow_run_id} {step_id}`. A lone task id exits 2. `subtask` on a turn with no skill executions is a clear empty. |
| C-TSK-06 | offline | `omni task watch <id>` on a finished task, then `omni task inbox` | Finished watch returns the terminal state. Do not `cancel`/`drain` another operator's running task. |
| C-ART-01 | offline | `omni artifacts preview <id>` or a missing id | Metadata plus a text prefix, or a clear miss. No crash. |
| C-ART-02 | offline | `omni artifacts versions <id>` and `omni artifacts review <id>` | Lineage and reproducibility evidence when an artifact exists; a missing id is a clear miss. |
| C-ART-03 | offline | `omni artifacts diff <id1> <id2>` or a missing id | A delta when versions exist; a clear miss otherwise. |
| C-ART-04 | offline | `omni artifacts help` | Subcommands include preview/diff/versions/review. |
| C-CIT-01 | offline | `omni cite list` and `omni source list` | Library and structured sources. |
| C-CIT-02 | offline | `omni cite export --format bibtex` and `omni source reindex` | Empty library is a warning, not a crash. Reindex is idempotent. |
| C-HYP-01 | offline | `omni hypo new "Dense retriever beats BM25 on long docs" -c 0.6` then `omni hypo list` | Hypothesis is stored. |
| C-HYP-02 | offline | `omni claim new "..."` then `omni evidence help` and `omni run list` | Claim lands. Evidence bind needs a real `source_id` after retrieval. Run ledger may be empty. |
| C-LITC-01 | model | `omni lit "How does RAG reduce hallucination?"` | Same entry as A-COR-01. |
| C-LITC-02 | offline | `omni bench --k 3` and `omni eval --research-quality` | Offline metrics. `omni eval --black-box` with zero successes must exit 1. |
| C-SCH-01 | offline | `omni schedule list` and `omni schedule proposals` | Next fire and pending approvals. |
| C-SCH-02 | serve | `omni schedule add --cron "0 18 * * *" --goal "Search for new RAG papers"` then `omni schedule run` | Exactly one trigger mode. `run` needs `omni serve`; without it, record blocked. |
| C-SCH-03 | offline | `omni schedule all` and `omni schedule show <id>` | All-workspace list. A missing id is a clear miss. |
| C-SCH-04 | offline | On a schedule created for this walkthrough: `schedule disable <id>` then `enable <id>` | State flips. `approve`/`deny`/`clarifications` only hit a real proposal. |
| C-SRV-01 | offline | `omni status` and `omni serve status` and `omni serve doctor` | Workspace, db, daemon. Bind address is not `0.0.0.0`. |
| C-SRV-02 | offline | `omni web start` then fetch `127.0.0.1:1088` then `omni web stop` | Loopback only. Same store as `-P`. Do not leave the process running. |
| C-SRV-03 | offline | `omni web status` and `omni web help` | Status is honest when the UI is down. Bare `web port` requires `{port}` and is a setter, not a getter. |
| C-CH-01 | offline | `omni channel list` and `omni channel test wechat` | List works offline. Test fails closed when the channel is unconfigured. |
| C-CH-02 | offline | Do not run `omni channel login wechat --start` in this catalog | QR login is interactive. Record as blocked. |
| C-MCP-01 | offline | `omni mcp list` and `omni trust --help` | External MCP and trusted directories. |
| C-MCP-02 | offline | `omni exec --help` and `omni replay --help` | Non-interactive run and chronological replay. |
| C-MCP-03 | offline | `omni mcp help` and `omni mcp agents` | Help and agent targets. |
| C-SOUL-01 | offline | `omni soul status` and `omni soul list` | Active persona and scanner root. |
| C-SOUL-02 | offline | `omni soul help` | Lifecycle. |
| C-AS-01 | offline | `omni autosota info` and `omni autosota doctor` | Wrapper. No secrets in output. |
| C-AS-02 | offline | `omni autosota status` | Does not create an Omni background task. |
| C-OPS-01 | offline | `omni update status` and `omni terminal status` | Update state. Shift+Enter readiness. |
| C-OPS-02 | offline | `omni uninstall --dry-run` | Preview only. |
| C-REPL-01 | offline | `omni --help` | Slash verbs exist as Typer groups or in-process REPL commands. |
| C-REPL-02 | offline | `/help` text in `omni --help` and `cli/src/omni/cli/repl_commands.py` | `/inbox` is in the catalog. `/exit` and `/quit` leave cleanly. |
| C-REPL-03 | offline | REPL: `/context` then `/compact` | Context reports budget and injected sections. Compact reports savings or "nothing to compact". |
| C-REPL-04 | offline | REPL: `/copy` then `/verbose quiet` then `/debug on` / `/debug off` | Copy is honest outside the dock. Verbose/debug take effect immediately. |
| C-REPL-05 | offline | REPL: `/clear` then `/new` | Context clears; tasks and memory stay. `/new` changes session without wiping scrollback. |
| C-REPL-06 | offline | REPL: `/inbox` then `/task all --limit 5` | In-process inspect. Must not fork-and-hang. |

`A-LIT-01` is the release-blocker named-tool contract. Public docs keep the
English wording above. The original rc5 walkthrough used a Chinese phrasing of
the same contract (named `search_literature`, `rows=8`, list `source_id` only,
no survey, no `write_file` / `run_skill` / `spawn_subagents`). Live execution
may use that Chinese phrasing; do not paste it into `cli/docs`.

## Cases this catalog refuses

- `omni project migrate`
- `omni skills install` / `omni skills uninstall`
- A host grammar that parses "do not use X" from Chinese or English prose.
  Deny lists are `ToolPolicy` and interaction mode, not an NL parser.

## Related docs

- [cli-validation-guide.md](cli-validation-guide.md) — CLI/REPL loops and plan-mode approval
- [agent-validation-guide.md](agent-validation-guide.md) — skills, workflows, ROM, memory
- [testing-and-evaluation.md](testing-and-evaluation.md) — pytest and `omni eval`
- [commands.md](commands.md) — full command reference
