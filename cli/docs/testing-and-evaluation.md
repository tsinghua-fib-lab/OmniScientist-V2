# Testing and evaluation

OmniScientist needs two kinds of confidence:

1. deterministic confidence that storage, policy, lifecycle, and provenance contracts are correct;
2. empirical confidence that real models complete realistic research work reliably and efficiently.

No single framework supplies both. The recommended stack keeps Omni's native pytest and scenario
harnesses as the release source of truth, and uses **Inspect AI** as the external orchestration,
sandbox, trajectory, and benchmark interoperability layer.

## Decision

**Selected external orchestrator: [Inspect AI](https://inspect.aisi.org.uk/).**

This selection does not replace `pytest`, `omni eval`, or benchmark-owned scorers. Inspect should be
an optional development/evaluation dependency, not part of the normal `omni` runtime installation.
The integration boundary is an Omni solver/agent adapter that calls the same public
`OmniAgent.handle_turn` path as CLI and IM channels.

Why it is the best fit:

- Python-native tasks, datasets, custom solvers/agents, tools, and scorers fit the existing codebase.
- Agent evaluation includes tool use, custom/external agents, checkpointing, intervention, and
  token/message/time limits.
- Eval sets provide parallelism, retry/resume, error handling, and early stopping.
- Logs preserve sample-level transcripts, events, scores, model usage, and reproducibility metadata.
- Sandboxes and tool approval preserve benchmark-owned execution boundaries.
- AstaBench already builds on Inspect, and Omni already contains an AstaBench solver adapter.
- Hugging Face LightEval itself identifies Inspect as its preferred `eval` backend, reducing the
  value of introducing a second general orchestration layer.

## What exists today

### 1. Deterministic code and contract tests

```bash
uv sync --project cli --all-extras
uv run --project cli ruff check cli/src cli/tests
uv run --project cli pytest -q
```

The pytest suite covers storage migrations, sessions, memory, task/subtask lifecycle, ReAct transcript
normalization, bounded termination, policy and approval, skill contracts, workflow recovery,
scientific artifacts, channel presentation/idempotency, CLI/REPL parity, and distribution checks.
Tests use `mock`/`ScriptedLLM` and do not call a network model.

### 2. Native deterministic agent scenarios

```bash
uv run --project cli omni eval
uv run --project cli omni eval --coverage
uv run --project cli omni eval --record --gate --json
```

These persona and capability scenarios are fast CI regressions. They may script planner/model
proposals, so they prove that Omni handles a declared trajectory correctly; they do not prove that
a real model will discover that trajectory from natural language.

### 3. Research-quality invariants

```bash
uv run --project cli omni eval --research-quality
uv run --project cli omni eval --quality-input quality.json --json
```

These checks score citation fidelity, statistical invariants/tolerances, and reproducibility
manifests without an LLM judge. They should remain hard release gates.

### 4. Natural-language black-box journeys

```bash
# Offline subset; model/network scenarios are reported as skipped
uv run --project cli omni eval --black-box --repeats 5 --concurrency 4 --json

# Real configured model and network; explicit opt-in and may incur cost
uv run --project cli omni eval --black-box --repeats 5 --concurrency 2 --live --json
```

A black-box scenario contains user turns and observable expectations only. The loader rejects
planner answers, model outputs, tool scripts, seeded memories/tasks, and reviewer verdicts. Every
attempt receives a fresh workspace and enters through `OmniAgent.handle_turn`; IM scenarios also
exercise the common channel command/presentation/delivery boundary in memory.

The report includes repeated-run success rate, provenance accuracy, manual rework, duration, token
use, and estimated cost.

### 5. External scientific benchmarks

- `omni.eval.asta_solver` adapts official Inspect/AstaBench tools to Omni while leaving AstaBench in
  charge of task data, sandboxing, scoring, logs, and aggregation.
- `omni.eval.external_benchmarks` loads and runs BioMysteryBench cases in an attested sandbox, omits
  evaluator rubrics from prompts/exports, and leaves official scoring to the benchmark owner.

See [external-benchmarks.md](external-benchmarks.md) for governed setup and commands.

## What the current stack does not prove

The existing suite is substantial, but it is not a complete product certification:

- Most PR tests use a scripted or mock model. They cannot measure real model planning variance,
  provider-specific tool-call behavior, or repeated-run success probability.
- Offline IM tests exercise Omni's channel boundary, not a real WeChat/Feishu/DingTalk login,
  network reconnect, platform retry, media upload, or provider acknowledgement.
- Live black-box results are not yet emitted in a widely interoperable trajectory format with a
  standard viewer, resumable eval-set execution, and benchmark-level sandbox metadata.
- Semantic answer quality still needs calibrated domain rubrics and periodic expert review.
- Model/provider matrices, long-duration soak runs, rate-limit recovery, and real cost ceilings
  should run outside the fast PR gate.
- External benchmark scores cover selected scientific abilities; they do not validate Omni's own
  memory, task recovery, channel, storage, or approval semantics.

## Tool comparison

| Tool | Best at | Fit for Omni | Decision |
|---|---|---|---|
| **Inspect AI** | Stateful agent/tool evals, sandboxing, limits, eval sets, logs, external-agent bridges | Matches Python runtime and AstaBench; can preserve full trajectories and official tool boundaries | **Adopt as external orchestrator** |
| **Promptfoo** | Declarative prompt/provider matrices, text assertions, red teaming, CI dashboards | Excellent for prompt/API smoke matrices, but duplicates Omni's YAML scenarios and adds a separate Node-first execution model | Optional provider/red-team utility, not the core runner |
| **DeepEval** | Pytest-like LLM/RAG/agent metrics and LLM-as-judge workflows | Easy metric integration, but overlaps the current pytest harness and emphasizes judge-based scoring/instrumentation | Optional scorer experiment, not orchestration source of truth |
| **Ragas** | RAG faithfulness, relevance, context precision/recall, and tool/goal metrics | Useful for semantic research-answer scoring after deterministic citation checks | Optional scorer plugin; never a lifecycle/policy gate |
| **Hugging Face LightEval** | Comparing language models across many backends/tasks | Strong model benchmark suite, but Omni must be evaluated as a stateful agent application | Use for underlying model selection, not Omni runtime acceptance |
| **lm-evaluation-harness** | Reproducible few-shot/static LM benchmark tasks | Primarily model generations, likelihood, multiple choice, and static task YAML | Use for base-model regression only |
| **AstaBench** | Scientific-agent literature, code, analysis, and end-to-end research tasks | Direct domain benchmark and already Inspect-based | Keep official adapter and scorer |
| **BioMysteryBench** | Objective bioinformatics tasks over messy real data | Valuable domain benchmark with strict sandbox/data rules | Keep governed adapter; official scorer only |

## Target architecture

```text
pytest contracts ------------------------------> hard pass/fail
Omni deterministic scenarios -----------------> hard pass/fail + coverage
Omni natural-language scenarios --+
                                    +-> Inspect task/solver/log -> reliability metrics
official AstaBench tasks ------------+
official BioMysteryBench environment ----------> benchmark-owned score
retrieval/answer records -> optional Ragas ----> semantic diagnostics
```

### Sources of truth

- Python contracts stay in `cli/tests/`.
- Product journeys stay in `cli/src/omni/data/blackbox_scenarios/`.
- Omni behavior is observed from public results plus persisted runs/events/artifacts, never from a
  test-only planner injection.
- AstaBench and BioMysteryBench retain their own datasets, tools, sandboxes, licenses, and scorers.
- LLM-judge scores are diagnostics unless calibrated against a versioned human-labelled set.

### Inspect adapter contract

Each scenario attempt should:

1. create an isolated `OMNI_HOME` and workspace;
2. start from user-visible input only;
3. call `OmniAgent.handle_turn` or the real subprocess/channel boundary selected by the scenario;
4. preserve parent/child runs, events, tool calls/results, artifacts, provenance, tokens, cost, and
   termination reason in the Inspect sample log;
5. let deterministic scorers check status/events/policy/artifacts before any semantic judge runs;
6. support repeated epochs without sharing memory or artifacts between attempts.

## Automation matrix

| Gate | Frequency | Required checks | Failure policy |
|---|---|---|---|
| Pull request | Every change | Ruff, pytest, ≥80% changed executable-line coverage under `cli/src/omni/**/*.py` with a resolved base SHA, deterministic eval coverage, research-quality invariants, distribution build | Any regression or missing gate artifact blocks merge |
| Nightly | Daily | Live black-box provider matrix, 3-5 repeats, Inspect logs, latency/token/cost budgets | Alert and retain traces; repeated hard-contract failure blocks release |
| Reliability soak | Weekly | 20+ repeats, concurrency, cancellation/recovery, rate limits, daemon/channel simulators | Compare success-rate confidence interval and p95 latency/cost to baseline |
| Release candidate | Per release | Cross-platform install smoke, wheel validation, ≥80% changed executable-line coverage under `cli/src/omni/**/*.py` since the previous tag (or verified first-release bootstrap), reactive-binding/authority evidence, live core journeys, AstaBench validation subset | All hard gates and artifacts pass; no statistically material core-journey regression |
| Published benchmark | Milestone | Official AstaBench suite and governed BioMysteryBench runs | Publish official score, version, model, tools, cost, and run count together |

Do not put real platform credentials or unrestricted live-model tests in forked pull-request jobs.
Use protected scheduled environments and sanitize trajectories before uploading CI artifacts.

## Metrics to publish

For each model/provider/version combination, publish more than one aggregate score:

- per-scenario success rate over repeated isolated runs;
- hard-contract pass rate (status, policy, required/forbidden events and tools);
- provenance/citation accuracy and unsupported-claim rate;
- artifact validity and reproducibility checks;
- manual rework turns;
- p50/p95 wall time, total tokens, and estimated cost;
- failure taxonomy: model/provider, tool, policy, budget, network, storage, verifier, or presentation;
- benchmark-owned score for external suites.

## Implementation sequence

1. Add an optional Inspect development/eval dependency and a general Omni black-box solver adapter.
2. Convert the native black-box scenario schema into Inspect datasets without creating a second
   scenario source of truth.
3. Map Omni hard expectations to deterministic Inspect scorers and store the full run/event trace.
4. Publish `.eval` logs as protected CI artifacts and add a nightly provider matrix.
5. Add optional Ragas scorers for grounded-answer diagnostics, calibrated against expert labels.
6. Keep AstaBench and BioMysteryBench execution in their official environments and report official
   scores separately from Omni product-quality gates.

## Primary references

- [Inspect AI documentation](https://inspect.aisi.org.uk/)
- [Inspect AI agents](https://inspect.aisi.org.uk/agents.html)
- [Inspect AI running evals](https://inspect.aisi.org.uk/running.html)
- [Inspect AI repository](https://github.com/UKGovernmentBEIS/inspect_ai)
- [AstaBench repository](https://github.com/allenai/asta-bench)
- [BioMysteryBench research note](https://www.anthropic.com/research/Evaluating-Claude-For-Bioinformatics-With-BioMysteryBench)
- [Hugging Face LightEval](https://huggingface.co/docs/lighteval/main/index)
- [Promptfoo repository](https://github.com/promptfoo/promptfoo)
- [DeepEval repository](https://github.com/confident-ai/deepeval)
- [Ragas metrics](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/)
- [lm-evaluation-harness repository](https://github.com/EleutherAI/lm-evaluation-harness)
