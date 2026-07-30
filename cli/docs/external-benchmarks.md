# Black-box and External Benchmark Validation

Omni has three separate evaluation layers. Do not merge their scores:

1. The deterministic contract suite is fast CI coverage and may script planner proposals.
2. The black-box suite accepts natural-language turns only and enters through
   `OmniAgent.handle_turn` in a fresh workspace for every attempt.
3. External benchmarks retain their own datasets, tools, sandboxes, scorers, and licenses.

## Natural-language Black-box Suite

Run the offline, deterministic subset:

```bash
.venv/bin/omni eval --black-box --repeats 5 --concurrency 4 --json
```

Run scenarios that require the configured model and network:

```bash
.venv/bin/omni eval --black-box --repeats 5 --concurrency 2 --live --json
```

For a longer reliability soak, use at least 20 repeats and preserve the JSON report:

```bash
.venv/bin/omni eval --black-box --repeats 20 --concurrency 4 --live --json
```

The report publishes per-scenario and aggregate success rate, provenance accuracy,
manual rework, duration, token use, and estimated cost. A scenario file may contain only
user turns and observable expectations. Planner output, model answers, tool scripts,
fixtures, and reviewer verdicts are rejected by the loader.

## AstaBench

Run Omni's solver from the official AstaBench checkout and environment. The adapter in
`omni.eval.asta_solver` wraps the task-owned `state.tools`, disables registry skills for the
sample, preserves Asta's sandbox and scorer, and reports Omni's model usage back to Inspect.

Example validation run from the AstaBench checkout:

```bash
uv run astabench eval \
  --solver /absolute/path/to/omniscientist_v2/cli/src/omni/eval/asta_solver.py@omni_agent \
  --split validation --limit 1 --log-dir logs/omni-validation/
uv run astabench score logs/omni-validation/
```

Configure Omni's provider/model in the evaluation environment before running. The solver owns
its model loop, so it does not silently replace the configured Omni model with Inspect's
`generate` callback. Use the official AstaBench score command for all reported benchmark scores.

References:

- <https://github.com/allenai/asta-bench>
- <https://inspect.aisi.org.uk/solvers.html>

## BioMysteryBench

BioMysteryBench data is gated and its execution policy prohibits identifying a source study by
looking up dataset accession metadata. Accept the dataset terms and run only inside the required
container/network sandbox. Omni deliberately fails closed unless the caller passes
`sandbox_attested=True`.

```python
import asyncio
from pathlib import Path
from omni.config import load_settings
from omni.eval import load_biomystery_cases, run_biomystery_cases, write_benchmark_answers

async def main() -> None:
    cases = load_biomystery_cases(Path("problems.csv"), data_dir=Path("data"))
    answers = await run_biomystery_cases(
        cases,
        settings=load_settings(),
        repeats=5,
        sandbox_attested=True,
    )
    write_benchmark_answers(Path("omni-answers.jsonl"), answers)

asyncio.run(main())
```

The answer export never contains `answer_rubric` and sets `official_score` to `null`. Grade it
with the benchmark owner's evaluator; do not publish an Omni-generated surrogate score.

References:

- <https://huggingface.co/datasets/Anthropic/BioMysteryBench-full>
- <https://www.anthropic.com/research/Evaluating-Claude-For-Bioinformatics-With-BioMysteryBench>

## Operational Boundaries

- Outbound IM delivery is application-level effectively-once. Concurrent workers cannot claim
  the same logical delivery, and failed/expired claims are recoverable. A crash after a provider
  accepted a message but before the local acknowledgement can still create ambiguity unless that
  provider offers an idempotency key or reconciliation API.
- `run_compute`, status lookup, and cancellation are durable. Local cancellation terminates the
  process group; submitted schedulers remain `cancel_requested` until a backend acknowledges it.
- Planner, coordinator, review, prompt-skill, subagent, memory-compaction, and
  profile-maintenance LLM calls emit component-level `cost.usage` events. Session-end memory work
  uses a separate `maintenance` run rather than changing the cost or status of the preceding
  user turn.
- Figure bundles bind figure, code, data, and run ids by SHA-256. Mutation after generation fails
  `verify_figure_bundle` instead of silently presenting a stale figure.
