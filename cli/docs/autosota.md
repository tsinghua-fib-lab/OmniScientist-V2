# AutoSOTA integration

`omni autosota` is a deliberately thin owner-controlled launcher for the
external [AutoSOTA](https://github.com/tsinghua-fib-lab/AutoSOTA) CLI. AutoSOTA
continues to own its isolated environments, experiment scheduling, GPU use,
native onboarding, and long-running optimisation. OmniScientist does not turn
an AutoSOTA run into an Omni background task.

## Install explicitly

AutoSOTA is not installed by `omni init` and never downloaded by a research
task. Install the official GitHub Release package only when it is needed:

```bash
omni autosota get
omni autosota info
```

The package is installed into a versioned npm prefix under Omni's user cache;
it does not modify global npm packages. `get` checks Node.js >= 18, npm, git,
and bash, verifies the installed `autosota --version` command, and records its
source URL and SHA-256. If GitHub's Releases API is temporarily rate-limited,
`get` resolves the same official release through GitHub's release-page redirect
instead. An air-gapped administrator may explicitly pass a pre-downloaded
package with `--package /path/to/autosota-X.Y.Z.tgz`.

## Configure one workspace

Create a separate AutoSOTA workspace and point it at an existing target
repository:

```bash
omni autosota config \
  --workspace /data/autosota-demo \
  --repo /data/target-repository \
  --devices 0,1 \
  --claude-model <code-model> \
  --research-model <research-model> \
  --prompt-secrets
```

The command writes a non-secret `.omni-autosota.toml` launcher profile in the
workspace. Provider keys are written only to Omni's owner-only
`secrets.toml`, keyed by the workspace path; they are never printed and are
temporarily materialized into `config.yaml` only while the native AutoSOTA
process is alive. The original `config.yaml` bytes and permissions are restored
after every exit, including failures.

`--use-omni-model` is an explicit convenience for copying the configured Omni
text-model endpoint and key into AutoSOTA's private secret profile. It does not
run automatically. Existing native `config.yaml` files are left untouched
unless `--force` is supplied, so AutoSOTA-owned settings and comments are not
silently reformatted. For a DeepSeek Omni model, the code-agent endpoint is
automatically translated from the normal OpenAI-compatible `/v1` endpoint to
DeepSeek's Anthropic-compatible `/anthropic` endpoint; the research endpoint
keeps the original `/v1` URL. Other providers are copied unchanged, so a
non-Anthropic-compatible code endpoint requires the provider's documented
router or an explicit `--claude-base-url`.

## Native lifecycle

After configuring the workspace, create a non-secret native paper profile and
skip AutoSOTA's model-driven onboarding:

```bash
omni autosota init --workspace /data/autosota-demo
omni autosota prepare my-paper --workspace /data/autosota-demo
omni autosota run my-paper --workspace /data/autosota-demo --dry-run
omni autosota run my-paper --workspace /data/autosota-demo
omni autosota status --workspace /data/autosota-demo
omni autosota steer "prioritize memory efficiency" --workspace /data/autosota-demo
omni autosota resume --workspace /data/autosota-demo
```

`prepare` copies repository, evaluation, metric, budget, GPU, and protected-path
settings from `.omni-autosota.toml` into
`.autosota/papers/<paper>/config.yaml`. It leaves secret fields blank. This is
the required path when provider secrets or protected paths are configured:
native model onboarding cannot safely inherit those settings and is refused.

`run` remains foreground and passes the configured repository and GPU device
selection and configured iteration/time budgets to the native CLI. Use `exec`
for native commands that are not yet a named wrapper:

```bash
omni autosota exec --workspace /data/autosota-demo -- my-paper --skip-onboard
omni autosota exec --workspace /data/autosota-demo -- inspect
omni autosota exec --workspace /data/autosota-demo -- ask "summarize the latest verdict"
```

`login`, `doctor`, `inspect`, `steer`, and `resume` are likewise direct native
operations. OmniScientist never starts an optimisation from natural-language
planning or from a skill task; a user must invoke one of these commands.
The bundled Claude Code executable is made available only to the native child
process; it is not installed globally or added to the user's shell `PATH`.
