# OmniScientist V2

> **A local-first, CLI-first open-source personal research agent.**
> Chat in your terminal (like Codex / Claude Code) to do **grounded, cited** literature Q&A,
> research ideation, scientific figures, paper writing & review — with an auditable
> hypothesis→claim→evidence trail, a reproducible run ledger, and an honesty `--verify` pass.
> Durable state is stored locally (no MySQL/Redis); configured model, connector, and IM providers
> still receive the data required for each request. Skills use a portable SKILL.md baseline for
> Claude Code, Codex & OpenClaw (and MCP), and the same agent is reachable from
> **WeChat / Feishu / DingTalk**.

This repository is the **official next-generation (V2) implementation** of the OmniScientist
project introduced in [*OmniScientist: Toward a Co-evolving Ecosystem of Human and AI
Scientists*](https://doi.org/10.48550/arXiv.2511.16931) (Shao et al., arXiv:2511.16931). V2
distills the earlier HelixForge research-agent OS into a local-first, single-machine agent while
preserving auditable research objects, workflows, and provenance. See [`CITATION.cff`](CITATION.cff)
for software and paper citation metadata.

For the full design and how it compares to Claude Code/Codex and other open-source research
agents, see [`cli/docs/research-agent-design.md`](cli/docs/research-agent-design.md).

The current source build reports package version `2.0.0rc1` (planned Git tag `v2.0.0rc1`). The
build is published to a public index only when the release workflow
(`cli/scripts/release.sh`) runs; until then, install from the source checkout.

## Install and initialize

### Requirements

- macOS, Linux, or Windows 10/11
- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/) or [`pipx`](https://pipx.pypa.io/) for an isolated installation
- Node.js 20.9 or newer, including npm (required by the bundled research-pptx renderer)
- A model endpoint is optional for installation: the bundled `mock` provider supports offline
  setup and deterministic evaluation

### Quick install

PyPI is the stable package authority. Install one isolated copy, then launch Omni:

```bash
uv tool install omniscientist
# or
pipx install omniscientist

omni
```

The first `omni` launch runs setup, prepares the lockfile-pinned Node runtime, and starts or repairs
the single Home Service. Later launches are fast local checks. If a package manager replaces the
package directly (`uv tool upgrade omniscientist` or `pipx upgrade omniscientist`), the next
`omni` detects the new installation fingerprint and completes runtime/config/service convergence
without downloading the Python package again.

Use one product command for normal updates:

```bash
omni update
```

It delegates the Python package step to the owning package manager, then converges managed runtime
state, retires legacy daemons, refreshes the supervisor launcher, restores the Home Service, and
verifies readiness. SQLite schema changes remain data-preserving and run with backups when each
store is first opened by the new version.

### Install from a checkout

From a downloaded/cloned source tree, the repository installer deploys the local checkout snapshot,
including uncommitted work. It creates an isolated `uv tool` and bootstraps `uv` when needed. A copy
of the script run without its checkout installs PyPI; the moving `master` channel is development-only
and must be selected explicitly. The default extras include MCP, vector retrieval, and channel SDKs.

```bash
git clone https://github.com/tsinghua-fib-lab/OmniScientist-V2.git
cd OmniScientist-V2

# macOS / Linux
./cli/scripts/install.sh

# Editable contributor install
./cli/scripts/install.sh --editable --local

# If another omni exists, consolidate verified copies into the uv installation
./cli/scripts/install.sh --on-conflict migrate
```

```powershell
# Windows PowerShell, from the repository root
powershell -ExecutionPolicy Bypass -File cli\scripts\install.ps1
```

The repository installers use official PyPI by default and never rewrite global `uv` or pip
configuration. Mainland-China users may opt into Aliyun for the current process with
`--index-url aliyun` (`-IndexUrl aliyun` on PowerShell), a custom URL, or
`OMNI_PYPI_INDEX_URL`.

An existing installation triggers an explicit **upgrade existing / migrate to uv / cancel**
choice. Non-interactive use must pass `--on-conflict` (`-OnConflict` in PowerShell). Installing into
an active environment is intentionally advanced and explicit:

```bash
./cli/scripts/install.sh --method env              # dedicated active venv/conda env only
./cli/scripts/install.sh --method env --force-conda-base  # unsafe override, never the default
```

The installer records ownership by invoking the newly installed launcher's absolute path. Run
`omni doctor` to see the active executable, owning interpreter/method, effective PATH order, and
any conflicting copies.

Manual alternatives:

```bash
# Editable contributor install
uv venv --python 3.12 .venv
uv pip install -e "./cli[dev,mcp,vec,channels]" --python .venv
```

The base package includes the Python dependencies required by active built-in Skills. The first
`omni` launch, repository installer, `omni init`, and `omni update` prepare the lockfile-pinned
research-pptx Node renderer. The runtime layout and explicit repair command are described in
[`skills/research-pptx/references/runtime-setup.md`](skills/research-pptx/references/runtime-setup.md).
Task execution never installs packages. An unexpected dependency failure stays
failed and prints `omni skills setup research-pptx`; configuration such as a
missing LiveFigure VLM remains a separate `needs_input` condition.

Git URL installs require an immutable semantic release tag or full commit hash; mutable branches
are rejected except through the explicit development channel. Until the first PyPI artifact is
published, use the checkout installer.

### Maintainer release flow

GitHub is the release authority and PyPI publication is tag-driven. Before the first release,
the canonical repository is
[`tsinghua-fib-lab/OmniScientist-V2`](https://github.com/tsinghua-fib-lab/OmniScientist-V2).
Create a GitHub environment named `pypi`, then configure the PyPI Trusted Publisher with these
exact values. If the environment restricts deployment branches and tags, allow the tag pattern
`v*`; otherwise the tag-triggered publish job cannot enter the environment.

| PyPI field | Value |
|---|---|
| PyPI project name (pending publisher only) | `omniscientist` |
| Owner | `tsinghua-fib-lab` |
| Repository name | `OmniScientist-V2` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

`Workflow name` is the filename only—not `.github/workflows/release.yml`. The workflow file must
be committed at that path before a release tag is pushed. The workflow grants `id-token: write`
only to the publish job and uses no persistent PyPI token. A pending publisher does not reserve
the project name; the first successful `2.0.0rc1` publication creates the project and claims it.

For the first migration, push the source branch before creating any release tag, set it as the
GitHub default branch, and enable private vulnerability reporting:

```bash
git push -u origin master
```

Set `cli/src/omni/__init__.py` to the intended immutable version, commit a clean tree, then run
`cli/scripts/release.sh`. Before tagging, require the exact candidate commit to pass the ordinary CI
matrix (Python 3.11–3.13 on Linux, macOS, and Windows), the reactive-binding evidence gate, the
distribution check, and at least 80% coverage of changed executable lines under
`cli/src/omni/**/*.py`. Pull requests compare with their base commit and ordinary pushes compare with
the previous push commit. Tag builds compare with the nearest prior `v*` tag. The first `v*` release
uses immutable bootstrap commit `2b7dfe46a0028fe643126f12ba83a5e8c4f9bb94`, the last `master`
baseline before this control-plane change; the release candidate must descend from it. Once a prior
release tag exists, the tag takes precedence and the bootstrap remains provenance only.

The release script requires remote `master` to equal local `HEAD`, validates and pushes
`v<version>`. The tag workflow reruns all nine OS/Python combinations (Linux/Python 3.12 in the
coverage build), enforces the release gates, tests the built wheel on Linux/macOS/Windows, and
publishes through OIDC. It never uploads to PyPI before the tag exists and never accepts a local
PyPI token.
For the first public release, validate `2.0.0rc1` on all three platforms before bumping and tagging
`2.0.0`.

After the release workflow publishes the candidate, test the exact prerelease explicitly:

```bash
uv tool install 'omniscientist==2.0.0rc1'
pipx install 'omniscientist==2.0.0rc1'
```

The unpinned `uv tool install omniscientist` and `pipx install omniscientist` commands remain the
stable `2.0.0` user path after the candidate is promoted.

### Uninstall

The uninstaller is ownership-aware and shows the complete plan before it changes anything:

```bash
omni uninstall --dry-run                 # inspect services, integrations, files, and packages
omni uninstall --yes                     # uninstall the program; preserve the active research data directory
omni uninstall --purge --yes             # also delete OMNI_HOME data and known channel secrets
omni uninstall --everything --yes        # full wipe: all detected installs + registered project data
```

Inside the REPL, use the same command as `/uninstall --dry-run`. Repository wrappers are also
available as `./cli/scripts/uninstall.sh` on macOS/Linux and
`cli\scripts\uninstall.ps1` on Windows.

`--everything` stops all detected Omni `serve` processes, removes Omni-managed Skill exports and
MCP registrations, deletes configuration/tasks/memory/artifacts, removes registered in-place
`.omni` project stores, and uninstalls every detected Omni package installation. It preserves the
source checkout, unrelated MCP servers, user-modified or differently sourced Skills, and external
WeChat gateways/containers. See [the uninstall safety guide](cli/docs/uninstall.md) for the exact
resource matrix and recovery implications.

Plain `omni uninstall` removes only the installation currently running the command. If another
verified installation would remain on `PATH`, the plan shows it explicitly; use
`--all-installations` or `--everything` when all copies must be removed. Program removal is deferred
until the CLI exits so its final report can render safely.

### Verify and initialize

```bash
omni --version
omni                      # first bare launch starts the setup wizard, then opens the REPL
omni init                 # run the same setup wizard explicitly at any time
omni doctor               # environment, config, storage, and dependency checks
omni config list          # inspect the effective configuration (secrets are masked)
omni status               # confirm the active workspace and local store
```

`omni init` configures the model, chooses semantic or keyword recall, initializes local storage,
and optionally exports built-in skills or registers the Omni MCP integration. On a fresh
interactive installation, bare `omni` runs this same wizard before opening the REPL. Once a user
configuration exists (or a complete model is supplied by a profile/environment), later bare
launches open the REPL directly. Explicit `omni init` remains available for inspection or
reconfiguration.

The safe Enter defaults keep optional integrations off:

- embedding recall: **No** (offline keyword recall)
- automatic skill import/export: **none** (use `omni skills add` or `omni skills export` later)
- MCP registration: **No** (no MCP integration or service is enabled automatically)

Initialization does **not** log in to WeChat, Feishu, or DingTalk; channels have their own
credential and pairing flow.

For an offline or CI bootstrap with no questions and no external writes:

```bash
omni init --non-interactive    # mock model + keyword recall; no skill export or MCP registration
omni init --home /data/omni    # choose a persistent data directory during setup
```

A first bare `omni` in a non-interactive terminal exits with a setup instruction instead of
waiting for input. Run `omni init` in a terminal, or use `omni init --non-interactive` in CI.

Re-running `omni init` is safe: it first shows the current setup and the command that owns each
setting. Use `omni config path` to locate `config.toml`, `secrets.toml`, and project config files.
The data directory defaults to `~/.omni`. Change it later with `omni config home <PATH>` and restore
the default with `omni config home --reset`. Existing data is left in place; Omni does not silently
move or delete it. `OMNI_HOME` remains the highest-priority per-process override.

### Configure a real model

The wizard is the easiest route. The equivalent one-line commands for OpenAI-compatible services
are:

```bash
# DeepSeek
omni config model -p deepseek -u https://api.deepseek.com/v1 \
  -m deepseek-chat -k "$DEEPSEEK_API_KEY" --test

# OpenAI (replace <MODEL> with a model available to the account)
omni config model -p openai -u https://api.openai.com/v1 \
  -m <MODEL> -k "$OPENAI_API_KEY" --test

# Local Ollama (replace <MODEL> with an installed model; no key required)
omni config model -p ollama -u http://localhost:11434/v1 -m <MODEL> --test
```

`provider` selects an OpenAI-compatible protocol preset; `base_url` decides which service is
actually called. API keys are written to `secrets.toml` in the active Omni data directory and masked
in command output. To
change one field, use its full key, for example `omni config set model.model <MODEL>`.

Keyword recall is the safe default and does not call an embedding endpoint. Enable semantic recall
only when a real `/embeddings` service is available:

```bash
omni config embeddings --enable \
  -u https://api.openai.com/v1 -m text-embedding-3-small -k "$OPENAI_API_KEY"
omni config embeddings --disable       # return to offline keyword recall
```

DeepSeek's chat endpoint is not an embedding service; configure a separate embedding endpoint when
using DeepSeek for chat.

## First use

### Ask from the shell

```bash
omni "Summarise recent advances in 3D Gaussian Splatting for SLAM"
omni chat "Review arXiv:1706.03762 and list the strongest limitations"
omni exec -f task.md -o answer.md       # non-interactive scripts/CI
```

Longer work is persisted as runs, child tasks, artifacts, sources, claims, and evidence in the
active workspace. It does not depend on the terminal remaining open when `omni serve` owns the task
worker.

### Use the interactive REPL

```bash
omni
```

On the first bare launch this opens the setup wizard automatically. After setup, it goes straight
to the interactive prompt; it does not repeat onboarding unless you explicitly run `omni init`.

On a capable interactive terminal, Omni uses an inline dock: committed results stream into the
terminal's own native scrollback while the composer stays docked above a permanent status line. The
dock stays in the normal buffer and never captures the mouse, so history survives exit and you
select and copy transcript text with the terminal's normal click-drag (Shift-drag or copy-mode
inside tmux/Zellij, per your config). Scroll back with your terminal's usual wheel and keys; Omni
only redraws the bottom dock. Empty or whitespace-only Enter is ignored. Enter submits the draft. Ctrl+J is the portable newline action on macOS, Linux, and
Windows; Shift+Enter and Alt/Option+Enter map to the same action when the host exposes modifiers.
A trailing `\` followed by Enter is the universal continuation fallback, Ctrl+O remains a
compatibility alias, and Ctrl+X Ctrl+E opens the draft in `$VISUAL`/`$EDITOR`. Multiline paste is
kept as one turn and the composer grows to eight rows before scrolling.

While a turn is running, the composer stays active. **Enter** steers the active semantic turn,
**Tab** queues the draft and `/queue <message>` queues an explicit next turn. **Esc** (or `/stop`)
first requests cooperative cancellation; repeating it during the same turn force-cancels that turn,
and the next turn starts a fresh cancellation sequence. If the current deterministic runner has no
model steering boundary,
Enter is transferred once to the next-turn queue. Ctrl+C with a draft only clears that draft. `/exit`, `/quit`,
or Ctrl+D on an empty draft cancels active work, preserves completed artifacts/checkpoints, closes
the session cleanly, and then leaves. In classic fallback mode, Ctrl+C cancels the active turn.
Leaving the REPL never stops a separately running `omni serve` or its IM channels.

Omni negotiates xterm modified-key reporting while it owns the TTY and restores the prior mode on
exit or before an interactive child command. Check the complete path with `omni terminal status`
or `omni doctor`. Inside tmux, run `omni terminal setup` (or `/terminal-setup` in the REPL): Omni
previews an idempotent managed block for `~/.tmux.conf`, asks before writing, creates a timestamped
backup, and reloads the current server. It never rewrites terminal configuration silently.

The default `auto` UI safely falls back to the classic prompt for pipes, redirection, CI,
`TERM=dumb`, or terminals without the required capabilities. Force a mode for diagnosis with
`omni --ui tui`, `omni --ui classic`, `OMNI_UI=tui|classic`, or persisted
`omni config set display.ui_mode tui|classic|auto` (`tui` selects the inline dock). Interactive
child commands temporarily own the real terminal and the dock is restored afterwards. Because the
dock stays in the normal buffer, committed transcript text remains in shell scrollback after exit;
conversations and tasks also remain persisted by Omni.

At the `›` prompt, text without a leading slash is a conversation turn. Commands start with `/`:

```text
› How does RAG reduce factual hallucination?
› /steer prioritize primary sources
› /stop
› /why
› /task list
› /exit
```

### Shell commands and REPL commands

Most management commands have two equivalent surfaces. The slash form is only used **after** a
bare `omni` has opened the REPL.

| Operation | Shell | Inside the REPL |
|---|---|---|
| Inspect configuration | `omni config list` | `/config list` |
| List skills | `omni skills list` | `/skills list` |
| Inspect tasks | `omni task list` | `/task list` |
| Configure a channel | `omni channel list` | `/channel list` |
| Check the home service | `omni serve status` | `/serve status` |
| Audit the last route | `omni why` | `/why` |

Slash commands dispatch to the same Typer command implementation and retain the REPL's active
project, profile, and model. Put global options before the shell subcommand:

```bash
omni -P robotics task list
omni -P robotics                 # then use /task list in this project REPL
```

The REPL parses arguments directly; it does not invoke a shell. Environment variables, pipes, and
redirections such as `$TOKEN`, `|`, and `>` are therefore Shell-only. For secrets in a REPL command,
omit the secret option and answer the hidden prompt instead of putting a literal secret in history.

### Workspaces and sessions

By default, storage is keyed to the current Git repository (or current directory). Named projects
are useful when research should be independent of a checkout:

```bash
omni project new robotics
omni -P robotics "draft a related-work section on visual SLAM"
omni -P robotics session list
omni -P robotics resume --last
```

The persistent data root is selected with `omni config home` (default `~/.omni`); `OMNI_HOME`
overrides it for the current environment. `omni status` always prints the active workspace, SQLite
store, artifact directory, daemon, and task counts.

## Message channels (WeChat / Feishu / DingTalk)

Reach the **same** agent from your phone. Besides the terminal, Omni can answer from WeChat, Feishu,
and DingTalk. CLI turns are handled by the current `omni` process; IM messages are handled by the
always-on **home service** — one `omni serve` process per `OMNI_HOME` that owns the channels and
routes inbound messages to the anchor workspace:

```text
terminal prompt -> current omni process -> workspace agent
IM message      -> platform connection -> omni serve (home service) -> anchor workspace agent -> platform reply
```

Multiple CLI windows are safe. The home service is home-scoped: one process per `OMNI_HOME` owns each
IM account (from the anchor workspace) and dispatches every workspace's schedules; home-level channel
locks still prevent a stray legacy daemon from claiming an account. Use `omni serve status` to see the
anchor and channels, or `--all` to find lingering legacy daemons.

The recommended transports need **no public callback URL** (Feishu WebSocket long connection, DingTalk
Stream mode), so they work from a laptop behind NAT. The Feishu/DingTalk SDKs ship with the default
installer's `channels` extra; for a plain source/venv install add them with
`pip install "omniscientist[channels]"`.

### What to prepare on each platform

| Platform | Channel id | Recommended transport | Prepare on the platform side | Required config |
|---|---|---|---|---|
| Feishu / Lark | `feishu` | WebSocket long connection (no public URL) | A custom app; subscribe the bot to the `im.message.receive_v1` event over a **long connection**; copy its **App ID** and **App Secret** | `app_id`, `app_secret` |
| DingTalk | `dingtalk` | Stream mode (no public webhook) | An enterprise robot with **Stream mode** enabled; copy its **Client ID** (AppKey) and **Client Secret** (AppSecret) | `client_id`, `client_secret` |
| WeChat / WeCom | `wechat` | Operator-managed local gateway (default), or experimental iLink | Run a WeChat gateway reachable at e.g. `http://127.0.0.1:8088` (**Omni does not launch it**); *or* use `--method ilink` and scan the printed QR with WeChat | gateway: `gateway_url` · iLink: `bot_token` (obtained by scanning) |

### 1. Configure and start

One command per platform stores the credentials, prints any QR/setup link, creates a short-lived
`/pair <code>` for manual binding, enables the channel, and (`--start`) applies it to the always-on
home service. Closing the REPL does not stop that service.

```bash
# Feishu — App ID + App Secret from your custom app
omni channel login feishu \
  --app-id <FEISHU_APP_ID> --app-secret "$FEISHU_APP_SECRET" \
  --credential-store file --start

# DingTalk — Client ID + Client Secret from a Stream-mode robot
omni channel login dingtalk \
  --client-id <DINGTALK_CLIENT_ID> --client-secret "$DINGTALK_CLIENT_SECRET" \
  --credential-store file --start

# WeChat — through an operator-managed local gateway (you run the gateway)
omni channel login wechat --method gateway \
  --gateway-url http://127.0.0.1:8088 --credential-store file --start

# WeChat — experimental iLink / "WeChat ClawBot" flow (scan the QR)
omni channel login wechat --method ilink --credential-store file --start
```

Inside a running `omni` REPL, use the slash forms. Feishu and DingTalk securely prompt for the
omitted secret (the REPL does not expand shell variables such as `$FEISHU_APP_SECRET`):

```text
/channel login feishu --app-id <FEISHU_APP_ID> --credential-store file --start
/channel login dingtalk --client-id <DINGTALK_CLIENT_ID> --credential-store file --start
/channel login wechat --method gateway --gateway-url http://127.0.0.1:8088 --credential-store file --start
/channel login wechat --method ilink --credential-store file --start
```

On macOS, omit `--credential-store file` to store secrets in the Keychain. Linux and Windows must
pass it because Omni provides no built-in encrypted OS credential backend there; secrets then go to
`<OMNI_HOME>/secrets.toml`, which should remain user-only. The iLink connector is an experimental,
explicit opt-in and may be restricted by the platform; prefer a managed gateway/WeCom path for a
controlled deployment.

### 2. Pair and verify

External IM users are gated by an allowlist with pairing **on by default**: the account that scanned
a WeChat QR is auto-bound, and anyone else self-binds by sending the short-lived `/pair <code>` (from
`channel login`) in the bot conversation.

```bash
omni channel list           # enabled, configured, and runtime state
omni channel test feishu    # local config/dependency check; not a live platform round trip
omni serve status           # home service: desired state, runtime, anchor, channels
omni serve status --all     # list any lingering legacy per-workspace daemons under OMNI_HOME
omni serve restart          # reload the home service after config/code changes
omni serve stop             # transient pause; the next `omni` launch brings it back
```

The equivalent REPL checks are `/channel list`, `/channel test feishu`, `/serve status`,
`/serve status --all`, `/serve restart`, and `/serve stop`.

### Where the configuration lives

- **Enablement** — `<OMNI_HOME>/config.toml` under `[channels] enabled = ["cli", "feishu", …]`.
- **Per-channel settings** — `<OMNI_HOME>/channels/<name>.toml` (mode, endpoints, allowlist/pairing).
- **Secrets** — macOS Keychain, or `<OMNI_HOME>/secrets.toml` when using `--credential-store file`.
- Prefer `omni channel login` / `omni channel add` over hand-editing: they write secure
  allowlist/pairing defaults for you. `omni config path` prints the exact file locations.

Detailed platform setup, media/typing behavior, and cross-platform credential/QR notes are in
[`cli/docs/getting-started.md#always-on-home-service-im-channels`](cli/docs/getting-started.md#always-on-home-service-im-channels).

## Important commands

| Area | Commands | Purpose |
|---|---|---|
| Conversation | `omni "..."`, `omni`, `omni exec` | One-shot, REPL, and non-interactive execution |
| Setup | `init`, `doctor`, `config`, `status` | Initialize and inspect the effective local setup |
| Projects | `project`, `profile`, `session`, `resume`, `replay` | Isolate work and continue prior context |
| Skills | `skills list/info/add/trust/remove/restore/export` | Manage built-in and third-party skill lifecycles |
| Planning | `why`, `current`, `omni chat --mode plan`, `omni chat --mode review`, `/mode`, `/plan`, `/review` | Explain routing, resolve focus, and control execution mode |
| Durable work | `task list/show/watch/approve/steer/cancel/retry/resume` | Observe and control parent runs and child tasks |
| Artifacts | `artifacts preview/versions/diff/review` | Inspect generated outputs and provenance |
| Literature | `lit`, `cite`, `source`, `bench` | Grounded Q&A, library export, corpus inspection, retrieval quality |
| Research ledger | `hypo`, `claim`, `evidence`, `run`, `verify` | Maintain and audit hypotheses, claims, evidence, and experiments |
| Memory | `memory`, `/compact`, `/context` | Manage long-term memory and REPL context budgets |
| Channels | `channel`, `serve` | Configure IM accounts and the persistent worker/channel service |
| Interop | `mcp` | Expose Omni capabilities to Claude Code and Codex |
| Quality | `eval` | Deterministic, black-box, and research-quality evaluation |
| Maintenance | `update`, `serve prune` | Upgrade the CLI and clean stale daemon workspaces |

Every command supports `--help`; in the REPL use `/<group> help`, such as `/channel help` or
`/task help`. The complete reference is [`cli/docs/commands.md`](cli/docs/commands.md).

## Repository layout

This repo is a two-part monorepo: the **CLI application** and the **skill collection** are
independent so they can be versioned, audited, and reused separately.

```
omniscientist_v2/
├── cli/        # the `omni` Python application (source, tests, docs, packaging)
├── skills/     # portable SKILL.md packages (the research skill collection)
├── README.md   # you are here
├── LICENSE
└── AGENTS.md   # shared project conventions (Codex/Claude-Code convention)
```

- **`cli/`** — everything Python: `src/omni`, `tests/`, `pyproject.toml`, `scripts/`, and the
  app docs in `cli/docs/`. See [`cli/README.md`](cli/README.md).
- **`skills/`** — one folder per skill, each a self-contained `SKILL.md` (+ optional `engine.py`).
  Skill *content* never imports CLI internals. Portable prompt skills work across Claude Code,
  Codex and OpenClaw; engine-backed features require their portable runner or Omni runtime. See
  [`skills/README.md`](skills/README.md).

The CLI **runs** skills; skills don't depend on CLI internals. A Python-engine skill (for example,
`arxiv-fetch`) calls only the public runtime surface (`omni.research`). The executor loads its engine from the skill directory, so the package can
be copied without importing private CLI modules.

## How skills work

A skill is a standalone folder containing `SKILL.md`, `LICENSE.txt`, and `NOTICE.md`.
`SKILL.md` has YAML frontmatter and a Markdown body. Only `name` + `description` are required by
the portable execution contract; OmniScientist reads optional extensions under
`metadata.helixforge` and OpenClaw hints under `metadata.openclaw`. One skill can run **four ways**:

| kind | runs as | example |
|------|---------|---------|
| `prompt_only` | a focused ReAct sub-agent with the builtin tools | an imported methodology skill |
| `python_engine` | a Python class in the skill's `engine.py` | `arxiv-fetch`, `research-pptx` |
| `cli_exec` | an external command (stdout parsed as JSON/text) | wrap any CLI |
| `remote_mcp` | a tool on an MCP server | any MCP tool |

### Install mechanism (where skills come from)

OmniScientist treats **its own** skills and **other tools'** skills differently:

In the table below, the Shell form runs in a normal terminal; the REPL form runs after starting
`omni` and reaching the `›` prompt.

1. **Bundled built-ins** live in [`skills/`](skills/) and ship inside the wheel. They are the
   default research toolkit and are always discoverable.
2. **`omni init`, `omni skills export`, or `/skills export`** *export* the built-ins into the on-disk roots the other
   tools read (`~/.claude/skills`, `~/.codex/skills`, `~/.openclaw/skills`, plus the shared
   `~/.agents/skills`), so Claude Code / Codex / OpenClaw can use them too. The complete folder is
   copied, including its license, notices, portable runner, and engine when present. Pick one tool
   (`omni skills export codex` or `/skills export codex`) or all three (`omni skills export --all`
   or `/skills export --all`). Tracked in `<OMNI_HOME>/skills_install.json`; `omni skills unexport` or
   `/skills unexport` removes only the copies OmniScientist created.
3. **`omni skills add <src>` or `/skills add <src>`** imports a skill into a **quarantine** under `<OMNI_HOME>/skills`.
   Imported skills are visible but cannot run or participate in automatic planning until the user
   reviews the source and runs `omni skills trust <name>` or `/skills trust <name>`.

| Action | Shell | Inside `omni` |
|---|---|---|
| Import a Claude Code skill | `omni skills add claude:my-skill` | `/skills add claude:my-skill` |
| Import a Codex skill | `omni skills add codex:my-skill` | `/skills add codex:my-skill` |
| Import a shared Agent Skill | `omni skills add agents:my-skill` | `/skills add agents:my-skill` |
| Import an OpenClaw skill | `omni skills add openclaw:my-skill` | `/skills add openclaw:my-skill` |
| Import a local directory | `omni skills add ~/work/my-skill` | `/skills add ~/work/my-skill` |
| Import a local Markdown file | `omni skills add ~/work/my-skill/SKILL.md` | `/skills add ~/work/my-skill/SKILL.md` |
| Import a Git repository | `omni skills add https://github.com/org/repo.git` | `/skills add https://github.com/org/repo.git` |
| Import one Git sub-path | `omni skills add https://github.com/org/repo#skills/example` | `/skills add https://github.com/org/repo#skills/example` |
| Trust after review | `omni skills trust my-skill --yes` | `/skills trust my-skill --yes` |
| Remove an imported skill | `omni skills remove my-skill` | `/skills remove my-skill` |
| Disable a built-in skill | `omni skills remove livefigure` | `/skills remove livefigure` |
| List disabled skills | `omni skills list --disabled` | `/skills list --disabled` |
| Restore a disabled skill | `omni skills restore livefigure` | `/skills restore livefigure` |

A Git source may be a single skill or a repository containing many skills. Direct imports shallow-clone
the default branch; for a non-default or pinned revision, check it out locally and import its directory.
Raw HTTP(S) `SKILL.md`/archive URLs and Git hosting `/tree/...` pages are not accepted. Importing
untrusted code is not an endorsement: inspect executable files and verify its license before trusting
it. See the complete source matrix, limits, and update behaviour in
[`cli/docs/skills.md`](cli/docs/skills.md#supported-skills-add-sources).

### How the agent selects & runs skills

Skills are wired into the whole agent loop, not just a flat tool list:

- **Intent recognition** — the semantic planner interprets the current turn in context and proposes
  capabilities and deliverables. The runtime then resolves those slots against trusted skill
  contracts. `trigger.phrases` remain optional catalog/search aliases; they do not form an automatic
  language-specific intent router. In the ReAct lane, `find_skill` searches the trusted catalogue
  and `use_skill` loads a selected provider. A normal request may therefore discover a portable
  prompt-only skill by description; `$skill-name` forces exact selection when required.
- **Task planning** — sync skills (Python-engine / CLI-exec) are direct tools in the bounded ReAct
  loop; the model composes them and the builtin tools (`read_file`/`write_file`/`bash`/`web_fetch`/…).
- **Long-horizon tasks** — skills marked `delivery_mode: async_task` (or an `escalate_run`) become
  durable background tasks (`submit_task`) in a SQLite-backed runtime with crash recovery, progress
  traces, and completion notifications — drained inline for one-shot CLI, or owned by the `omni serve`
  daemon (DB poller + heartbeat + atomic claim) so tasks run once even with several terminals open.
- **Local memory** — a 7-layer store (session → task → episodic/semantic/artifact) recalls relevant
  context (keyword + recency + importance by default; opt into vector recall with
  `memory.embeddings_enabled`) into the prompt; task results are recorded back as memories. All
  local: SQLite + files under the active data directory (`~/.omni` by default).
- **Workspaces & sessions** — the store is path-keyed by absolute working directory (like Claude
  Code), so every terminal in a repo shares one durable store; `omni status` shows which one. Tasks
  are visible across windows (`/task`, `omni task all`), and you resume past sessions with
  `omni resume` / `omni -c` (`omni project migrate` brings forward pre-upgrade data).
- **Grounded research** — what makes omni a *research* agent: a local literature corpus with
  cited (`[S#]`) retrieval, a **Research Object Model** (hypotheses → claims → evidence → sources)
  and a reproducible run ledger, all in the same per-workspace store. Reachable as `omni lit`
  (grounded Q&A), `omni verify` (audit claims for unsupported/contradicted/over-confident),
  `omni bench` (offline recall@k/MRR), and the `hypo/claim/evidence/run/source` groups — each also a
  REPL slash command. See [`commands.md`](cli/docs/commands.md).

### Discovery: omni-managed by default

`omni skills list` (or `/skills list` in the REPL) shows **only OmniScientist-managed skills**
(built-ins + what you imported with `skills add`). Your full Claude Code / Codex / OpenClaw
libraries are **not** mixed in by default — that is why deployment-specific entries like
`csi-claw-fusion-*` no longer appear. To browse everything on the machine:

| Action | Shell | Inside `omni` |
|---|---|---|
| Include external libraries | `omni skills list --all` | `/skills list --all` |
| Show discovery roots | `omni skills sources` | `/skills sources` |

### Bundled skills

The explicit active inventory is `arxiv-fetch`, `openalex-search`,
`paper-review`, `review-response`, `scientific-figure`, `scientific-poster`,
`livefigure`, `research-ideation`, and `research-pptx`.

- `artifact.figure` → `scientific-figure` → lightweight DOT/SVG/PNG
- `figure.editable.pptx` → `livefigure` → one editable single-slide PPTX
- `slides.generate` → `research-pptx` → a complete multi-slide deck
- `poster.scientific` → `scientific-poster` → a complete HTML scientific poster
- `review.paper` → `paper-review` → a venue-aware pre-submission review
- `review.response` → `review-response` → reviewer/editor revision correspondence
- `research.ideation` → `research-ideation` → pressure-tested research directions

Paper sections are delivered by the native evidence-aware `synthesis.final`
capability rather than a separate writing skill.

User guide: [`cli/docs/skills.md`](cli/docs/skills.md). Authoring guide:
[`skills/docs/authoring.md`](skills/docs/authoring.md).

## Testing and evaluation

Omni already has an automated test stack; `omni eval` is the agent-level entry point, not a synonym
for unit tests. Run the release-safe offline gates from the repository root:

```bash
uv sync --project cli --all-extras
uv run --project cli ruff check cli/src cli/tests
uv run --project cli pytest -q
uv run --project cli omni eval --coverage
uv run --project cli omni eval --research-quality
uv run --project cli omni eval --black-box --repeats 5 --concurrency 4 --json
```

The layers have different jobs:

| Layer | What it proves | Network/model |
|---|---|---|
| `pytest` | Storage, routing, policy, transcripts, skills, workflows, channels, CLI contracts | Offline |
| `omni eval` | Deterministic persona/capability scenarios and coverage gaps | Offline, scripted model plans |
| `omni eval --research-quality` | Citation, statistical, and reproducibility invariants | Offline |
| `omni eval --black-box` | Natural-language turns through the real public agent boundary in fresh workspaces | Offline subset by default |
| `omni eval --black-box --live` | Repeated real-model/network reliability, provenance, latency, tokens, cost, and rework | Explicit opt-in; may cost money |
| AstaBench / BioMysteryBench adapters | External scientific-agent performance under benchmark-owned tools and scorers | Separate governed environments |

**Tool selection:** keep pytest and Omni's native scenario corpus as the release source of truth;
adopt [Inspect AI](https://inspect.aisi.org.uk/) as the external evaluation orchestrator and
portable trajectory/log format. This is a complement, not a runtime dependency or replacement for
deterministic gates. It fits the existing AstaBench solver adapter, supports custom agents, tools,
sandboxes, limits, retries/resume, parallel eval sets, and inspectable logs. Ragas can be an optional
semantic scorer for RAG faithfulness/precision, but an LLM judge must never decide storage, policy,
or run-status correctness by itself.

The comparison with Inspect AI, Promptfoo, DeepEval, Ragas, Hugging Face LightEval, and
lm-evaluation-harness, plus the proposed PR/nightly/release matrix, is documented in
[`cli/docs/testing-and-evaluation.md`](cli/docs/testing-and-evaluation.md). External benchmark setup
is in [`cli/docs/external-benchmarks.md`](cli/docs/external-benchmarks.md).

## Documentation

- **Design & positioning**: [`cli/docs/research-agent-design.md`](cli/docs/research-agent-design.md)
  — capabilities, comparison with Claude Code/Codex, why it fits research, and a survey of other
  open-source research agents (GPT Researcher, STORM, PaperQA2, AI2 Asta, AI Scientist, …)
- App docs: [`cli/docs/getting-started.md`](cli/docs/getting-started.md) ·
  [`commands.md`](cli/docs/commands.md) · [`skills.md`](cli/docs/skills.md) ·
  [`architecture.md`](cli/docs/architecture.md) ·
  [`compatibility.md`](cli/docs/compatibility.md) (Claude Code / Codex / OpenClaw / MCP interop)
- Quality: [`cli/docs/testing-and-evaluation.md`](cli/docs/testing-and-evaluation.md) ·
  [`agent-validation-guide.md`](cli/docs/agent-validation-guide.md) ·
  [`external-benchmarks.md`](cli/docs/external-benchmarks.md)
- Skills: [`skills/README.md`](skills/README.md) · [`skills/docs/authoring.md`](skills/docs/authoring.md)
- Contributing: [`cli/CONTRIBUTING.md`](cli/CONTRIBUTING.md)

## License

Apache-2.0. Third-party attributions and modification notices are listed in [`NOTICE`](NOTICE).
See [`PRIVACY.md`](PRIVACY.md) for data flows and [`THIRD_PARTY_SERVICES.md`](THIRD_PARTY_SERVICES.md)
for external services and dataset responsibilities. Code and skill lineage is documented in
[`PROVENANCE.md`](PROVENANCE.md).
