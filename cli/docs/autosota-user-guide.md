# AutoSOTA user guide through OmniScientist

This guide covers a clean installation, validation, and production-oriented use
of AutoSOTA through `omni autosota`. Omni only downloads the external CLI,
stores launcher configuration, and starts it in the foreground. AutoSOTA
continues to own experiment environments, GPU allocation, repository changes,
evaluation, iteration, and result export.

The currently validated release is AutoSOTA `v0.3.1`. Use CPython 3.11 for its
protected Python optimizer runtime. The validation environment used Node.js 20
and four NVIDIA A100 GPUs.

## 1. Responsibility boundary

| Component | Responsibility |
| --- | --- |
| OmniScientist | Explicit installation, private credential storage, public launcher profile, safe paper configuration, and foreground process launch |
| AutoSOTA | Optimizer environment, model-agent loop, Git checkpoints, GPU selection, evaluation, resume, reports, and optimized-code export |
| Project owner | A reproducible repository, evaluation command, protected paths, metric direction, baseline, data, GPU capacity, and provider-side budget limits |

AutoSOTA uses a text code agent, not Omni's VLM. A working VLM configuration
does not configure the Anthropic-compatible endpoint required by the AutoSOTA
code agent.

## 2. Prerequisites

Verify the host tools:

```bash
node --version
npm --version
git --version
bash --version
python3.11 --version
```

Node.js must be at least version 18; Node.js 20 is recommended. If Python 3.11
is not installed, `uv` can provide a user-local interpreter:

```bash
uv python install 3.11
uv python find 3.11
```

Keep the AutoSOTA workspace separate from the repository it optimizes.

## 3. Install and initialize Omni

From an OmniScientist source checkout:

```bash
cd <OMNI_SOURCE_ROOT>
./cli/scripts/install.sh
omni --version
omni doctor
```

Contributors may instead use an editable environment:

```bash
cd <OMNI_SOURCE_ROOT>
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e "./cli[dev,mcp,vec]"
.venv/bin/omni --version
```

Run `omni init` once and configure the normal Omni text model. Prefer hidden
interactive input or environment expansion over a literal key in shell history.

## 4. Install the latest AutoSOTA release

AutoSOTA is not installed by `omni init`. Install it explicitly:

```bash
omni autosota get --version latest
omni autosota info
```

The runtime is installed in Omni's versioned private cache rather than with a
global `npm install -g`. `info` reports the resolved release, runtime directory,
executable, and ownership boundary. For the release validated with this guide,
the reported version is `v0.3.1`.

Release `v0.3.1` fixes repeated `[session] model=?` output. It prints one
deduplicated init event while preserving retry and error messages and the full
raw log.

## 5. Prepare the workspace and Python 3.11 toolchain

```bash
WS=<AUTOSOTA_WORKSPACE>
REPO=<TARGET_GIT_REPOSITORY>
PY311="$(command -v python3.11)"
mkdir -p "$WS/.toolchain/bin"
ln -sfn "$PY311" "$WS/.toolchain/bin/python3"
export PATH="$WS/.toolchain/bin:$PATH"
python3 --version
```

The final command must report Python 3.11. AutoSOTA creates its own environment
under `$WS/.autosota/venv` on first use.

Before giving the repository to an optimizer, verify all of the following:

1. `REPO` is a writable Git repository with a clean, recoverable baseline.
2. The evaluation command succeeds when run manually.
3. The primary metric, direction, and baseline value are known.
4. GPU indices and memory requirements are known.
5. Evaluation scripts, metric code, tests, and data splits are protected.

## 6. Configure the launcher

The simplest option is to reuse the configured Omni text model:

```bash
omni autosota config \
  --workspace "$WS" \
  --repo "$REPO" \
  --devices 0 \
  --eval-command 'bash run_eval.sh' \
  --primary-metric validation_accuracy \
  --metric-direction maximize \
  --baseline 0.50 \
  --max-iterations 1 \
  --max-total-hours 0.2 \
  --protected-path run_eval.sh \
  --protected-path tests \
  --use-omni-model \
  --force
```

For DeepSeek, Omni narrowly translates its normal OpenAI-compatible `/v1`
endpoint to the `/anthropic` code-agent endpoint. The research endpoint remains
the configured `/v1` endpoint.

To use separate providers, prompt for secrets in a trusted terminal:

```bash
omni autosota config \
  --workspace "$WS" \
  --repo "$REPO" \
  --devices 0 \
  --eval-command 'bash run_eval.sh' \
  --primary-metric validation_accuracy \
  --metric-direction maximize \
  --baseline 0.50 \
  --claude-base-url <ANTHROPIC_COMPATIBLE_ENDPOINT> \
  --claude-model <CODE_MODEL> \
  --research-base-url <RESEARCH_ENDPOINT> \
  --research-model <RESEARCH_MODEL> \
  --prompt-secrets \
  --force
```

Provider keys are stored in Omni's owner-only `secrets.toml`. The public
`.omni-autosota.toml` and normal workspace `config.yaml` do not retain them.

## 7. Prepare one safe paper configuration

After `config`, create the native per-paper configuration:

```bash
PAPER=<STABLE_PAPER_NAME>
omni autosota prepare "$PAPER" --workspace "$WS"
```

`prepare` transfers the repository, evaluation command, metric, baseline,
budget, GPU list, and every protected path into:

```text
$WS/.autosota/papers/<paper>/config.yaml
```

The file contains no provider key. Real runs must use `--skip-onboard`.
Model-driven native onboarding is not a substitute: it cannot reliably inherit
Omni's protected paths and may write supplied credentials into native files or
logs. Omni refuses that unsafe path when stored secrets or protected paths are
present.

## 8. Validate before spending model budget

Inspect the installation and native dependencies:

```bash
omni autosota info
omni autosota doctor --workspace "$WS"
```

Then run a no-model dry run:

```bash
omni autosota exec --workspace "$WS" -- "$PAPER" --repo "$REPO" --devices 0 --skip-onboard --skip-research --skip-eval --max-iter 1 --max-total-minutes 6 --dry-run
```

The dry run should create a run directory, effective configuration, and master
prompt without sending a model request.

## 9. Start a bounded real run

```bash
omni autosota exec --workspace "$WS" -- "$PAPER" --repo "$REPO" --devices 0 --skip-onboard --skip-research --skip-eval --max-iter 1 --max-total-minutes 6
```

Begin with one iteration, a short wall-clock limit, and `--skip-research`.
Remove `--skip-research` and `--skip-eval` only after the repository and cost
envelope are validated. AutoSOTA has iteration and time limits but no hard
dollar limit; configure provider-side budget caps and alerts.

For a single-node multi-GPU recording where InfiniBand is not used:

```bash
OMP_NUM_THREADS=1 NCCL_IB_DISABLE=1 CUDA_VISIBLE_DEVICES=0,1,2,3 omni autosota exec --workspace "$WS" -- "$PAPER" --skip-onboard --skip-research --skip-eval --devices 0,1,2,3 --max-iter 1 --max-total-minutes 8
```

Do not set `NCCL_IB_DISABLE=1` for a multi-node InfiniBand workload.

## 10. Inspect, steer, and resume

```bash
omni autosota status --workspace "$WS"
omni autosota inspect latest --workspace "$WS"
omni autosota exec --workspace "$WS" -- inspect latest --report
omni autosota steer "Prioritize lower latency without sacrificing the primary metric" --workspace "$WS"
omni autosota resume --workspace "$WS"
```

AutoSOTA stays in the foreground; Omni does not convert it into an Omni
background task. Typical outputs are under:

```text
$WS/
├── .autosota/papers/<paper>/runs/<run-id>/
│   ├── logs/
│   ├── memory/
│   └── results/
├── logs/
└── optimized_code/<paper>/
```

Inspect the repository checkpoints and best diff:

```bash
git -C "$REPO" log --oneline -5
git -C "$REPO" diff _baseline _best --stat
```

## 11. Security and operational notes

1. Do not pass provider keys as literal command-line values. Prefer
   `--prompt-secrets` or `--use-omni-model`.
2. Native AutoSOTA components may pass a temporarily materialized key to a
   child process. Avoid process-list commands that print complete arguments on
   shared hosts, use a dedicated account, and rotate any exposed key.
3. Omni restores the non-secret workspace configuration after the native
   process exits and scrubs per-paper secret fields. This does not replace host
   isolation or provider-side key controls.
4. Use a clean Git baseline and make evaluation code, tests, metrics, and data
   splits protected paths.
5. A successful mock or dry run validates plumbing, not scientific improvement.

## 12. Troubleshooting

| Symptom | Action |
| --- | --- |
| Protected optimizer reports an ABI symbol error or crashes | Confirm that workspace `python3` is CPython 3.11 and recreate only that workspace's `.autosota/venv` |
| `model CLI: NOT FOUND` | Re-run `omni autosota get --version latest --force`, then run `doctor` |
| VLM works but AutoSOTA cannot call the model | Configure an Anthropic-compatible code-agent endpoint; VLM settings are unrelated |
| `--skip-onboard` reports a missing paper config | Run `omni autosota prepare <paper> --workspace "$WS"` |
| No GPU is detected | A CPU mock may continue; a real workload must validate CUDA, `--devices`, dependencies, and memory |
| Torch reports `OMP_NUM_THREADS=1` | This is torchrun's safe per-process default; set it explicitly after profiling if required |
| `libibverbs.d` warnings appear on one host | NCCL probed RDMA; use `NCCL_IB_DISABLE=1` only for a single-node run that does not need InfiniBand |
| Cost is uncertain | Start with one short iteration, skip research, limit output tokens, and enforce provider-side budget controls |
