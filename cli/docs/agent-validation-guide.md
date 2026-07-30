# Agent and Skill Validation Guide

This guide validates OmniScientist as a research agent, not just as a generic CLI wrapper.
It focuses on intent recognition, skill planning, durable workflows, research provenance,
memory, sessions, storage, and external-agent compatibility.

Use this guide with a real model for planner validation:

```bash
omni -P skill-verify config list
omni -P skill-verify config test
```

The offline `mock` provider is intentionally limited; it is useful for deterministic unit tests and
tool smoke tests, but it does not validate real model-side workflow planning.

## What Must Be Validated

| Area | Why it matters | Visible checks |
|---|---|---|
| Intent recognition | The agent should select specialized research skills from natural user requests. | `task show <id> --json` lists the planned skill names. |
| Workflow planning | Multi-skill requests should become ordered workflow steps, not isolated tool calls. | `skills_used`, step status, dependencies, and artifacts are present in task JSON. |
| Workflow preflight | Under-specified workflows should ask for missing inputs before task creation. | `needs_input` or a follow-up question appears; `task list` has no half-failed task. |
| Durable tasks | Long work must survive outside a single ReAct turn. | `task list`, `task drain`, `task watch`, `task attach`. |
| Partial recovery | Failed downstream steps should not erase upstream results. | Failed task contains completed step results plus failed/skipped step details. |
| Research Object Model | Claims, evidence, hypotheses, sources, and runs must be explicit. | `source list`, `claim list`, `evidence list`, `hypo list`, `run list`, `verify`. |
| Grounded retrieval | Literature answers should cite corpus passages instead of relying on recall. | `lit "<question>" --verify` returns `[S#]` citations and audit results. |
| Memory/session continuity | Research context should persist across turns and sessions. | `memory search`, `session list`, `resume --last`, `replay <session>`. |
| External compatibility | Skills should work in Claude Code, Codex, OpenClaw without requiring Omni. | Copy skill folders and run `python3 scripts/run.py --self-test`. |
| Channel idempotency | IM retries should not trigger duplicate agent turns, while CLI repeats still work. | Replayed Feishu/WeChat/DingTalk message ids produce one response; CLI accepts repeated text. |
| Interaction control | Users must be able to plan, approve, review, steer, and cancel without losing the task. | `/plan`, `/review`, `task approve`, `task steer`, `task cancel`, and task events agree. |
| DAG recovery | Independent safe steps may run in parallel; failed subtasks/steps remain retryable and resumable. | `task subtask`, `task step`, `task retry`, `task resume`, and checkpoint data are visible. |
| Research quality | Citation, numeric, and reproducibility defects should fail deterministic CI checks. | `eval --research-quality` and project `--quality-input` fixtures report each dimension. |

## Intent Recognition Examples

Use natural prompts rather than skill names. The agent should infer the skill:

| Prompt | Expected skill behavior | Validation |
|---|---|---|
| `Fetch the abstract of arXiv 1706.03762.` | Plan `paper.fetch.arxiv` and resolve `arxiv-fetch`. | Output includes `Attention Is All You Need`; library/source is updated when available. |
| `Generate a RAG architecture figure with query, retriever, reranker, and LLM.` | Plan `artifact.figure` and prefer a full-contract built-in provider. | Task artifacts include DOT/SVG/PNG or a validated fallback format. |
| `Write a Transformer/RAG research section: search, fetch arXiv 1706.03762, generate a figure, and finish with paper prose.` | Plan search, fetch, figure, and native `draft.section` synthesis capabilities. | `task show <id> --json` contains fetch, figure, and final synthesis steps. |
| `Make this one editable scientific figure in PowerPoint.` | Plan `figure.editable.pptx`. | The provider is `livefigure`; output is a single-slide PPTX. |
| `Turn this study into a complete thesis-defense deck.` | Plan `slides.generate`. | The provider is `research-pptx`; output is a multi-slide PPTX. |

### LiveFigure VLM admission

Before configuring a VLM, run the editable-figure prompt above and inspect the
result with `/task show <id> --json`. Admission occurs at actual execution:
the gateway returns one configuration action with error code
`vlm_not_configured`, a redacted list of missing fields, and
`omni config vlm`; the LiveFigure engine is not loaded. An inline Agent call
presents the action as `kind: needs_input`. A background task or workflow may
already have a durable execution record, but that record must contain the same
action instead of a generic provider failure. It must not silently substitute
`scientific-figure`, because SVG/PNG is a different deliverable from an editable
single-slide PPTX.

After configuring the owner profile, validate without exposing the key:

```bash
omni config vlm --endpoint <FULL_CHAT_COMPLETIONS_URL> --model <MODEL> --api-key <KEY> --test
omni doctor
```

Repeat the prompt and confirm that LiveFigure creates exactly one editable
PPTX slide. Task output, logs, generated code, and provenance must not contain
the API key.

## Workflow Capability Matrix

Run the scenarios through either:

```bash
omni -P skill-verify exec "<prompt>"
```

Or inside the REPL:

```text
<prompt>
/task show <id>
/task show <id> --json
```

| Count | Scenario | Expected workflow steps |
|---:|---|---|
| 1 | Fetch the abstract for arXiv `1706.03762`. | `paper.fetch.arxiv` |
| 2 | Search OpenAlex for RAG factuality papers and propose research directions. | `literature.search`, `research.ideation` |
| 3 | Search RAG literature, propose directions, and draw a lightweight architecture figure. | `literature.search`, `research.ideation`, `artifact.figure` |
| 4 | Fetch arXiv `1706.03762`, ideate, draw a lightweight figure, and synthesize a section. | three skill capabilities; native `synthesis.final` |
| 5 | Search, ideate, make one editable PPT figure, generate a full deck, and synthesize notes. | four skill capabilities; native `synthesis.final` |
| 6 | Search, fetch, ideate, draw a lightweight figure, generate a deck, and synthesize a draft. | five skill capabilities; native `synthesis.final` |
| 7 | End-to-end RAG workflow with both figure formats, a complete deck, and synthesized notes. | all six active skill capabilities; native `synthesis.final` |

Expected conclusion:

- The planner chooses specialized research skills from the loaded registry.
- Writing deliverables are native synthesis steps (`draft.section`), not separate skills.
- Runtime stores one WorkflowRun, stable WorkflowSteps, and one Skill Execution id per skill-backed step.
- The task remains inspectable even when one step fails.

## Workflow Boundary Scenarios

### Missing Information Before Workflow Creation

Prompt:

```text
Prepare a submission section with search, fetch, ideation, an editable figure, a full deck, and writing.
```

Expected result:

- The assistant asks for missing research context instead of launching a workflow.
- The user-request Task is visible immediately and settles as `needs_input`; no WorkflowRun or Skill
  Execution is created until the required context is supplied.
- After the user supplies a topic, paper/arXiv id, figure format, deck purpose, and writing target,
  the workflow should use the relevant providers from the six-skill activity list and native
  `draft.section` synthesis.

### Conservative Resolver Behavior

Prompt:

```text
Write a Transformer/RAG research section: search the literature, fetch arXiv 1706.03762, generate an architecture figure, and finish with paper prose.
```

Expected result:

- The model should plan `artifact.figure` for the figure deliverable.
- Plan arbitration must select a provider before validation. An invalid provider is rejected or
  resolved there; the executor must not silently replace it after validation.
- Valid capabilities such as corpus indexing, ideation, PPTX generation, and native synthesis must not be
  overwritten merely because the global goal mentions search or literature.

## Research Provenance Scenarios

### Source and Claim Binding

Prompt:

```text
Using arXiv 1706.03762, record the claim that the Transformer replaces recurrent and convolutional sequence modeling with self-attention, and bind supporting evidence.
```

Validate:

```bash
omni -P skill-verify source list
omni -P skill-verify claim list
omni -P skill-verify evidence list <claim_id>
omni -P skill-verify verify
```

Expected result:

- A source exists for `1706.03762`.
- A claim exists with calibrated confidence.
- Evidence links the claim to the source with stance `supports`.
- `verify` no longer reports that claim as unsupported.

### Hypothesis and Experiment Run

Prompt:

```text
Propose a falsifiable hypothesis that reranking reduces hallucination in RAG answers; design and run a toy experiment with a fixed seed and record the run.
```

Validate:

```bash
omni -P skill-verify hypo list
omni -P skill-verify run list
omni -P skill-verify run show <run_id>
```

Expected result:

- A hypothesis is recorded as proposed/testing/supported/refuted/inconclusive.
- The run ledger includes command, seed or environment lock when available, metrics, and output URIs.
- Any reported number in the agent answer can be traced to a run.

### Figure Reproducibility

Prompt:

```text
Generate a RAG system architecture figure and record DOT, SVG/PNG, the render command, and provenance.
```

Validate:

```bash
omni -P skill-verify task show <task_id>
omni -P skill-verify run list
```

Expected result:

- Figure artifacts are listed.
- Graphviz DOT or fallback SVG is present.
- The rendering run is auditable through `run list` when runtime provenance tools are available.

## Grounded Retrieval and Verification

Build a small corpus:

```bash
omni -P skill-verify exec '$openalex-search Transformer attention architecture'
```

Ask a grounded question:

```bash
omni -P skill-verify lit "What is the Transformer's encoder-decoder attention structure?" --verify
```

Expected result:

- The answer uses `[S#]` citations.
- The Sources section maps each `[S#]` to a real source.
- `--verify` reports claim grounding status.

## Memory, Session, and Storage

Memory:

```bash
omni -P skill-verify memory add "This project evaluates RAG rerankers for factual consistency." --pin
omni -P skill-verify memory search reranker
```

Session:

```bash
omni -P skill-verify session list
omni -P skill-verify resume --last
omni -P skill-verify replay <session_id>
```

Storage:

```bash
omni -P skill-verify status
omni -P skill-verify project info
```

Expected result:

- Pinned memory is retrievable and can inform later turns.
- Sessions are durable and resumable.
- Workspace status shows SQLite DB, artifact directory, task inbox, and daemon state.

## Runtime Harness Validation

Plan and approval:

```bash
omni -P skill-verify chat "Compare three RAG reranking methods and propose an experiment plan" --mode plan
omni -P skill-verify task show <run_id>
omni -P skill-verify task approve <run_id>
```

Expected result:

- The acknowledgement returns a `run_id` before planning finishes.
- The run is `awaiting_approval`, contains a structured plan and selection reasons, and has no
  execution events before approval.
- Approval reuses the same task id and appends `plan.approved`. The approval CAS already performs
  the sole `awaiting_approval → running` transition, so execution does not append a fictitious
  `task.resumed` event whose previous status is also `running`.

Review, steer, and cancellation:

```bash
omni -P skill-verify chat "Review these experimental conclusions" --mode review
omni -P skill-verify task steer <running_run_id> "Prioritize checking statistical significance"
omni -P skill-verify task cancel <running_run_id>
```

Expected result:

- Review mode excludes mutating/executing tools and records the review pass.
- Steer is consumed once at the next ReAct iteration or workflow wave.
- Cancellation preserves completed results and ends cooperatively rather than deleting the task.

Workflow DAG and step recovery:

```bash
omni -P skill-verify task subtask <task_id>
omni -P skill-verify task step <workflow_run_id> <step_id>
omni -P skill-verify task retry <workflow_run_id> --step <step_id>
omni -P skill-verify task resume <workflow_run_id> --step <step_id>
```

Expected result:

- Dependency-ready steps only overlap when their contracts declare `concurrent_safe=true`.
- A checkpoint contains completed step ids and pending work.
- Retry creates a linked child attempt; resume keeps the child id and reuses successful upstream
  results.

Research-quality and domain extension smoke tests:

```bash
omni -P skill-verify eval --research-quality
omni -P skill-verify eval --quality-input quality.json --json
omni -P skill-verify config get research.domain_packs
```

Expected result:

- Citation fidelity, statistical correctness, and reproducibility are individually scored.
- The bundled baseline passes offline.
- Enabled domain packs add guidance and connector/artifact recommendations but do not bypass
  connector enablement, permissions, or tool budgets.

## Black-box Reliability and External Benchmarks

Run repeated natural-language scenarios without planner or answer injection:

```bash
.venv/bin/omni eval --black-box --repeats 5 --concurrency 4 --json
```

The bundled suite covers CLI, WeChat, Feishu, and DingTalk journeys, including memory,
self-knowledge, needs-input, multi-skill research, specialized PPTX routing, and artifact revision.
Model/network scenarios are skipped offline and run only with `--live`.

For AstaBench and BioMysteryBench setup, isolation rules, and official-scoring boundaries, see
[Black-box and External Benchmark Validation](external-benchmarks.md).

## Compatibility and Portable Skill Validation

Validate portable runner packaging and offline self-test entry points:

```bash
for d in \
  skills/arxiv-fetch \
  skills/openalex-search \
  skills/scientific-figure \
  skills/livefigure \
  skills/research-ideation \
  skills/research-pptx
do
  (cd "$d" && python3 scripts/run.py --self-test)
done
```

Expected result:

- Every runner returns `{"status": "ok", "portable_runner": true}`.
- The runner scripts do not import Omni.
- Copying a skill folder to Claude Code, Codex, or OpenClaw leaves `SKILL.md` readable and
  `scripts/run.py` executable where present.

Validate Omni enhanced mode:

```bash
omni -P skill-verify skills info arxiv-fetch
omni -P skill-verify skills info scientific-figure
omni -P skill-verify mcp agents
```

Expected result:

- `skills info` shows `kind`, `delivery`, `allowed-tools`, and path.
- `python_engine` skills still use `engine.py` under Omni.
- External agents can choose copy-only, portable runner, or MCP enhanced mode.

## Regression Commands

Run the local regression suite:

```bash
.venv/bin/pytest -q cli/tests
.venv/bin/ruff check cli/src cli/tests
.venv/bin/omni eval --coverage
.venv/bin/omni eval --research-quality
```

Targeted validations:

```bash
.venv/bin/pytest -q cli/tests/unit/test_portable_skills.py
.venv/bin/pytest -q cli/tests/agent/test_interaction_lifecycle.py
.venv/bin/pytest -q cli/tests/agent/test_workflow_runtime.py
.venv/bin/pytest -q cli/tests/agent/test_subagents.py
.venv/bin/pytest -q cli/tests/eval/test_research_quality.py
.venv/bin/pytest -q cli/tests/research/test_domain_packs.py
.venv/bin/pytest -q cli/tests/research/test_rich_research_artifacts.py
.venv/bin/pytest -q cli/tests/cli/test_cli.py
```

Expected result:

- Portable host simulation passes for Claude Code, Codex, and OpenClaw.
- Exact workflow prompt still plans the expected skills plus native writing deliverable.
- CLI and REPL examples stay in sync.

## Final Acceptance Conclusion

OmniScientist passes agent validation when:

1. Natural prompts trigger the expected research skills without naming them.
2. Multi-step workflow tasks are durable, inspectable, and recoverable.
3. Research outputs can be audited through sources, claims, evidence, hypotheses, runs, and artifacts.
4. Memory and session state persist across turns and terminals.
5. Copy-only and portable-runner modes work without Omni; Omni enhanced mode adds tasks, provenance,
   corpus, artifacts, and MCP.
