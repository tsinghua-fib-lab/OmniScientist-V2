# Getting started

## Requirements

- **OS:** macOS, Linux, or Windows 10/11 — `omni` is pure Python and runs the same on all three.
- **Python ≥ 3.11** (an isolated install does **not** touch your system Python).
- [`uv`](https://docs.astral.sh/uv/) or [`pipx`](https://pipx.pypa.io/) for an isolated install.
  The repository installer downloads uv from Astral automatically when it is missing.
- **Node.js ≥ 20.9** is required to *run* the lockfile-pinned research-pptx renderer.
  The first `omni skills setup research-pptx` (also run by `omni init` / `omni update`)
  additionally needs **npm** on PATH — pnpm is not a substitute for that `npm ci`.
  `ffmpeg` and LibreOffice remain optional for specific Skills.
- **Windows tip:** use **Windows Terminal** (or any UTF-8 console) so login QR codes render; or pass
  `--no-qr` to print the link instead.

## Install

Pick your OS below. The recommended path is one isolated `uv tool` per user; it does not depend on
whichever Conda/venv happens to be active. If `uv` is absent, the installer bootstraps it with the
official Astral installer and continues in the same command. From there the commands are identical
across operating systems.

### Stable install

PyPI is the stable package authority:

```bash
uv tool install OmniScientist-V2
# or
pip install OmniScientist-V2
# or
pipx install OmniScientist-V2

omni
```

For the loopback browser UI, include the optional Web runtime and then launch it:

```bash
uv tool install "OmniScientist-V2[web]"
# or: pip install "OmniScientist-V2[web]"
omni web
```

The browser remembers the selected workspace and conversation for the current tab, so a refresh
returns to the same page. While the tab is visible it automatically discovers CLI and message-
channel updates, follows durable Task activity, and refreshes open Task/artifact inspectors. Turns
started in Web stream assistant text token by token. Work started in another CLI/WeChat process
streams its durable progress into Web and loads the final reply when committed; provisional tokens
are not copied between processes.

`omni web` prints its loopback URL once. Ctrl+C then exits normally without an ASGI traceback or
Uvicorn request log stream. Diagnostics are retained under `<OMNI_HOME>/logs/web-<project>.log`
and rotate at 10 MiB, keeping 10 files total (`[observability] log_max_bytes` /
`log_files`, or `OMNI_LOG_MAX_BYTES` / `OMNI_LOG_FILES`). Routine access logging
is disabled. CLI and Home Service diagnostics live in the same directory and
use the same record format.

The first bare `omni` runs setup and converges the managed runtime and Home Service. Update later
with the same product-level command regardless of the original package manager:

```bash
omni update
```

Local snapshot and editable commands build the current loopback Web UI before replacing the Python
package. They require Node.js plus pnpm or npm and stop before installation if that build fails, so
an old `web/dist` is never relabeled as the current release.

### macOS / Linux

```bash
# From the repository root. Always uses an isolated uv tool by default and
# runs `uv tool update-shell`; active Conda/venv variables are ignored.
./cli/scripts/install.sh                          # isolated uv tool (recommended)
./cli/scripts/install.sh --channel master         # explicit moving development channel
./cli/scripts/install.sh --channel pypi           # explicit published package
./cli/scripts/install.sh --local                  # explicit checkout snapshot
./cli/scripts/install.sh --editable --local       # contributor/editable uv tool
./cli/scripts/install.sh --on-conflict migrate    # consolidate verified duplicate installs
./cli/scripts/install.sh --extras ""              # skip optional extras
./cli/scripts/install.sh --index-url aliyun        # optional per-run mirror

# Advanced: install into the explicitly active dedicated environment.
./cli/scripts/install.sh --method env
# Conda base is refused unless this unsafe override is stated explicitly:
./cli/scripts/install.sh --method env --force-conda-base
```

Or install the isolated tool yourself:

```bash
uv tool install "./cli[mcp,vec,channels,web]"   # then: uv tool update-shell
pipx install "./cli[mcp,vec,channels,web]"
```

### Windows (PowerShell)

```powershell
# From the repository root. Same isolated-uv ownership policy.
powershell -ExecutionPolicy Bypass -File cli\scripts\install.ps1
cli\scripts\install.ps1 -Channel master
cli\scripts\install.ps1 -Channel pypi
cli\scripts\install.ps1 -Local
cli\scripts\install.ps1 -Editable -Local
cli\scripts\install.ps1 -OnConflict migrate
cli\scripts\install.ps1 -IndexUrl pypi
cli\scripts\install.ps1 -Method env
cli\scripts\install.ps1 -Method env -ForceCondaBase
```

Or install the isolated tool yourself:

```powershell
uv tool install ".\cli[mcp,vec,channels,web]"   # then: uv tool update-shell
pipx install ".\cli[mcp,vec,channels,web]"
```

Before changing anything, the installer detects verified `omni` launchers. Interactive installs
offer **upgrade existing**, **migrate to uv**, or **cancel**. In CI/non-interactive shells choose
explicitly with `--on-conflict upgrade|migrate|cancel` (PowerShell:
`-OnConflict upgrade|migrate|cancel`). Migration installs and verifies the uv copy before removing
the old interpreter-owned package. If `omni` is not found afterwards, run `uv tool update-shell`
and reopen the terminal.

Both repository installers use official PyPI by default. The setting is process-local: it does not
change global pip or uv configuration. Select the Aliyun mirror with `--index-url aliyun` on
macOS/Linux or `-IndexUrl aliyun` on Windows. A custom package index can be supplied as a URL or
through `OMNI_PYPI_INDEX_URL`.

### Published releases

When run **from a checkout**, the no-flag installer deploys that local source. When run
**standalone** without a checkout, it installs the PyPI artifact. The moving `master` channel is
available only as an explicit development choice (`--channel master`; PowerShell `-Channel master`).

For a reproducible install, pin a real immutable semantic tag or full commit (the package lives
under `cli/`, hence `#subdirectory=cli`). `--remote --ref` rejects mutable branch refs — a moving
branch is only allowed through the explicit, non-reproducible `--channel master`:

```bash
uv tool install "OmniScientist-V2[mcp,vec,channels] @ git+<GITHUB_REPOSITORY_URL>@<RELEASE_TAG>#subdirectory=cli"
```

That direct Git URL is a CLI/agent development install. It cannot include the generated Web SPA,
because `web/` is outside the selected `cli/` subdirectory and `web/dist` is not committed. For
`omni web`, use the published package with `[web]`, or clone the full repository and run its
installer so the frontend is built before the Python package is installed.

For normal users, prefer `uv tool install OmniScientist-V2` or `pipx install OmniScientist-V2`; a shared
environment-level `pip install` is supported but does not provide the same isolation.

### Uninstall

Preview first, then choose whether research data should survive:

```bash
omni uninstall --dry-run
omni uninstall --yes                     # remove program/integrations; keep OMNI_HOME
omni uninstall --purge --yes             # also remove OMNI_HOME and known channel secrets
omni uninstall --everything --yes        # full wipe, including registered in-place .omni stores
```

The equivalent repository wrappers are:

```bash
./cli/scripts/uninstall.sh --everything --yes
```

```powershell
powershell -ExecutionPolicy Bypass -File cli\scripts\uninstall.ps1 -Everything -Yes
```

The full wipe still preserves the inspected source checkout, unrelated MCP registrations,
user-modified external Skills, and separately deployed channel gateways. Use `--json` for an
automation-readable plan/report and `--keep-program` to clean integrations or data while retaining
the current command. The complete ownership and safety contract is in [uninstall.md](uninstall.md).

### Verify (any OS)

```bash
omni --version
omni             # first bare launch starts setup, then opens the interactive REPL
omni doctor      # checks Python, PATH, config, and that `omni` is reachable
```

`omni doctor` reports the active executable, current installation method and interpreter, PATH
resolution order, package version, and conflicting copies. This is the authoritative first check
when two terminals appear to run different Omni versions.

### Developer setup (any OS)

```bash
uv venv --python 3.12 .venv
uv pip install -e "./cli[all,dev]" --python .venv
# Run tests / lint. POSIX: .venv/bin/<tool>   Windows: .venv\Scripts\<tool>
.venv/bin/pytest -q cli/tests
.venv/bin/ruff check cli/src cli/tests
```

`[all,dev]` matches CI (`uv sync --all-extras` from `cli/`). A narrower extra set
that omits `tokens` skips the tokenizer tests and measures transcripts in
different units than CI.

The base install includes active built-in Skill Python runtimes. Run `omni init`
once to configure Omni and prepare research-pptx's lockfile-pinned Node renderer.
`omni update` refreshes both Python dependencies and that renderer; use
`omni skills setup research-pptx` for an explicit repair. Skills never install
packages inside a task.

## First run

```bash
omni                           # fresh install: setup wizard, then interactive REPL
omni init                      # explicitly run the same setup wizard at any time
omni init --non-interactive    # safe defaults: offline mock + keyword recall, nothing exported
omni init --home /data/omni    # persist a custom data directory during setup
omni init --semantic-scholar-api-key "$SEMANTIC_SCHOLAR_API_KEY"  # optional research key
omni doctor                    # verify your environment
```

Bare `omni` checks for a user configuration before opening the REPL. On a fresh interactive
installation it runs the same wizard as `omni init`; after configuration has been written, later
launches go directly to the prompt. A complete model supplied by a profile or environment variables
is also respected and is not overwritten. Running `omni init` explicitly always remains available:
on an existing setup it shows the effective configuration first and asks before changing anything.

In a non-interactive terminal, a first bare `omni` exits with an actionable message rather than
blocking for input. Use interactive `omni init`, or choose deterministic CI defaults with
`omni init --non-interactive`.

After setup, a capable interactive TTY opens the inline dock REPL: committed history streams into
the terminal's own native scrollback while a compact dock — a live answer tail, the status line,
and the composer — stays pinned at the bottom. The dock never switches to the alternate screen and
never captures the mouse, so history survives after you exit and you select and copy transcript
text with the terminal's normal click-drag. Because the idle dock is quiescent — it never repaints
on a timer, only when something actually changes — a highlight you drag with the mouse stays put, so
your terminal's native copy (`Cmd+C`, or `Ctrl+Shift+C` on many Linux terminals) grabs exactly what
you selected. Committed rows are emitted at their natural width
(never padded out to the edge), so click-drag selects an arbitrary mid-line segment instead of
snapping to the start of the line — even inside tmux/Zellij copy-mode, where the selection gesture
is whatever your setup binds (for example Shift-drag). Omni releases mouse ownership but cannot
override that config. When you resize the window, the dock re-lays-out committed history at the new
width in a single clean pass, so nothing stays frozen at the old width and no stale composer boxes
are left behind. Scroll back through history with your terminal's usual wheel and
keys; Omni only ever redraws the bottom dock. For a copy that does not depend on the current mouse
selection, `Alt+Y` (or the `/copy` command) writes the most recent assistant answer to the system
clipboard over OSC 52 — which works across SSH and tmux — and writes a scrollback notice
(`copied last answer (N characters)`, or a failure with a next step). The command overview shown by bare `omni` and `/help` starts collapsed;
ordinary long raw output starts expanded. Press `Ctrl+T` to expand or fold these blocks in the
current normal-buffer transcript. Omni replays the semantic transcript at the current width, so
collapsed rows disappear and the same composer moves up without opening a pager, losing the draft,
or entering the alternate screen. Empty or whitespace-only Enter does nothing, and the composer
remains available while a turn is running. The composer uses the same multiline contract in the
inline dock and classic prompt:

| Input | Action |
|---|---|
| `Enter` | Submit the complete draft |
| `Ctrl+J` | Insert a newline on macOS, Linux, and Windows terminals |
| `Alt/Option+Enter` | Insert a newline when the terminal sends Escape+Enter |
| `Shift+Enter` | Insert a newline after CSI-u/xterm modified-key negotiation |
| trailing `\` then `Enter` | Remove the backslash and insert a portable continuation newline |
| `Ctrl+O` | Compatibility alias for newline |
| `Ctrl+X Ctrl+E` | Edit the draft in `$VISUAL` or `$EDITOR`, then return without submitting |

During active work, **Enter** steers an active semantic turn, **Tab** queues the draft, and
`/queue <message>` queues an explicit next turn. **Esc** or `/stop` first requests cooperative
cancellation; repeating it during the same turn force-cancels that turn, and completion resets the
sequence. A deterministic runner has no model steering boundary, so Enter is transferred once to the
next-turn queue; detached
`task steer` rejects the request with a follow-up hint. Ctrl+C with text in the composer clears only
that draft. `/exit`, `/quit`, or Ctrl+D
on an empty draft cancels active work, waits for checkpoint and child-process cleanup, and exits.
The first Ctrl+C in classic fallback mode also cancels the active turn instead of abandoning its
subprocesses. Completed results and artifacts remain inspectable through `/task`.

Bracketed multiline paste is inserted atomically and is sent only after Enter. The dock composer
grows from one to eight rows and then scrolls internally. Some terminal emulators send
exactly the same carriage return for Enter and Shift+Enter until extended-key reporting is enabled.
Omni requests the protocol while the composer owns the TTY and restores it on exit. Diagnose the
whole path with `omni terminal status` or `omni doctor`. In tmux, run `omni terminal setup` or
`/terminal-setup`: the command previews an idempotent managed block, asks for confirmation, backs up
an existing `~/.tmux.conf`, and reloads the current server. It configures `extended-keys`,
`terminal-features` extkeys, and `allow-passthrough`; it never edits the file silently. If the host
terminal still cannot expose Shift+Enter, use Ctrl+J or the trailing-backslash fallback.

On macOS and Linux, capable VT terminals use the negotiated xterm/CSI-u path. Windows Terminal and
modern VT-enabled consoles use the same parser contract; legacy Windows consoles safely skip raw
protocol writes and retain Ctrl+J plus `\` + Enter. The nested tmux path is covered by a real PTY
end-to-end test on POSIX systems, while platform detection and fallback behavior are tested for all
three operating-system families.

UI selection defaults to `auto`. Pipes, redirection, CI, `TERM=dumb`, and unsupported terminals use
the classic prompt. Choose explicitly with `omni --ui auto|tui|classic`, `OMNI_UI`, or
`omni config set display.ui_mode auto|tui|classic`; `tui` now selects the inline dock (the former
full-screen mode is retired). Commands that need passwords, QR codes, pagers, or direct terminal
control temporarily suspend the dock and restore it afterwards. Because the dock stays in the
normal buffer, committed transcript text remains in your shell's native scrollback after exit, and
Omni also continues to persist conversations, tasks, and artifacts. One consequence of the resize
re-layout: like Codex, a *width* change clears the terminal (scrollback included) before re-emitting
Omni's transcript at the new width, so lines you printed in the shell before launching Omni are
cleared on the first resize (Omni's own history is preserved and reflowed).

The wizard first asks where Omni should store config, sessions, memory, tasks, and artifacts. Press
Enter to accept `~/.omni`, or enter another directory. The selection is persisted outside the data
directory (`$XDG_CONFIG_HOME/omni/home`, or the platform fallback) so a later process can find it.
Then it asks which model provider to use, defaulting to
**openai**:

```text
Select a model provider (press Enter for 1 = openai):
  1) openai - official OpenAI or any OpenAI-compatible service
  2) deepseek - DeepSeek
  3) ollama - local Ollama, no API key required
  4) mock - offline placeholder for validating the local workflow
```

`openai` / `deepseek` / `ollama` all speak the OpenAI-compatible protocol — only the `base_url`
differs — so the wizard prefills a sensible endpoint + model for the one you pick; just press Enter to
accept. `omni init` then writes `config.toml` (and `secrets.toml` when a key is
given) and initializes the local runtime. Choose provider **mock** for an intentionally offline
setup; choosing a remote provider without its required key leaves a visible configuration warning
until the key is added. Use `omni config path` to show the exact files.

Change the persistent location later without deleting either directory:

```bash
omni config home                     # show the active path and resolution source
omni config home /data/omni          # use this path for future commands
omni config home --reset             # restore the default ~/.omni
```

Existing data is not moved automatically. Stop or restart an active REPL/daemon after switching.
`OMNI_HOME` remains the highest-priority environment override; unset it before selecting a different
persistent path.

The wizard then asks which retrieval mode to use:

- **Embeddings on — semantic recall:** finds paraphrases and related wording, but requires a real
  endpoint that serves `/embeddings`; it may add network cost and vector storage. The wizard collects
  the embedding endpoint/model/key separately. DeepSeek's chat endpoint is not reused automatically.
- **Embeddings off — keyword recall (default):** matches explicit terms, works without an embedding
  service, and never probes the chat endpoint.

The interactive wizard also offers an optional Semantic Scholar API-key prompt. You can request a
key from the [Semantic Scholar API page](https://www.semanticscholar.org/product/api). When set,
the key is written to `<OMNI_HOME>/secrets.toml` and the built-in `research-ideation` skill uses it
automatically. Leaving it blank keeps Semantic Scholar's public access, which may be more
rate-limited.

For an existing setup, configure or remove the key without re-running the wizard:

```bash
omni config set research.semantic_scholar_api_key "$SEMANTIC_SCHOLAR_API_KEY"
omni config get research.semantic_scholar_api_key  # masked; never prints the key
omni config list                                   # key status is masked here too
omni config unset research.semantic_scholar_api_key
```

The remaining optional steps all default to **No**, so pressing Enter completes a minimal local
setup:

- no external skills are imported automatically; use `omni skills add <source>` later
- built-in skills are not exported to Claude Code, Codex, or OpenClaw; use `omni skills export`
- Omni is not registered as an MCP integration, and no MCP service is started automatically

Channels are set up separately with Shell `omni channel login` or REPL `/channel login`.

> **Re-run any time.** `omni init` on an already-configured machine shows your **current
> configuration** (model, retrieval mode, data dir, project, skills, MCP, channels) alongside the exact command to
> adjust each item, and only re-runs the wizard if you confirm — so you never re-do the whole setup to
> tweak one thing.

### Configure a real model

Run `omni model` (or `/model` in the REPL) to pick a preset, or switch the main
model in one step:

```bash
omni model                    # picker: openai / deepseek / ollama / mock, plus vision and embedding
omni model deepseek-chat      # one-shot, like Claude Code's /model
omni model status             # effective roles and which layer won
omni model explain main       # field-level precedence
```

These commands keep the active-`OMNI_HOME` persistence scope. Isolated homes do
not inherit `~/.omni` at runtime; `omni init` will offer a discovered environment
or host stack instead of silently writing mock. Root `--model` remains a
temporary override for one launch.

The advanced one-line form sets main provider + endpoint + model + token and
(optionally) tests it immediately:

```bash
omni config model -p openai -u https://api.deepseek.com/v1 -m deepseek-chat -k sk-... --test
#                 └ -p provider  └ -u base_url                └ -m model       └ -k api_key
```

`-p` accepts `openai` / `deepseek` / `ollama` (all OpenAI-compatible — the `base_url` decides the real
service) or `mock` for offline. Set fields individually with full dotted keys instead (immediate
effect on the next command):

```bash
omni config set model.provider openai
omni config set model.base_url https://api.deepseek.com/v1  # OpenAI/DeepSeek/Ollama/… (+/v1 if required)
omni config set model.model    deepseek-chat
omni config set model.api_key  sk-xxx             # stored in the active secrets.toml automatically
omni config test                                  # main model + optional VLM / S2 / embeddings
```

Prefer config files? Run `omni config path`, then edit the reported `config.toml`
(`[model] provider/base_url/model`) and `secrets.toml` (`[model] api_key`) directly. The old
one-word shortcuts are intentionally rejected; use the complete `model.*` keys.
Inside the REPL you can run the same `config set …` / `config list` at the `›` prompt and the model
reloads live. Any OpenAI `/chat/completions`-compatible endpoint works (OpenAI, DeepSeek, Moonshot,
Ollama, vLLM, …). The API key is masked in all command output (even `omni config get model.api_key`).

### Configure the optional VLM for LiveFigure

LiveFigure needs a multimodal OpenAI-compatible chat endpoint. Configure and
probe it with `omni config vlm` (or `omni model vision`); `omni init` does not
accept VLM flags:

```bash
omni config vlm \
  --endpoint https://vision.example/v1/chat/completions \
  --model vision-model \
  --api-key sk-... \
  --test
omni doctor
```

Remote VLM endpoints must use HTTPS; plain HTTP is allowed only for
`localhost`, `127.0.0.1`, or `::1`. The token is stored in
`<OMNI_HOME>/secrets.toml` with mode `0600` on POSIX and is never written to a
project configuration. VLM admission happens only when LiveFigure actually
executes. The gateway returns `vlm_not_configured` plus the action
`omni config vlm` without loading the skill engine. That is a route
observation: the turn continues so the model can use another catalog skill
or tell the owner to run the setup command. Conversational confirms still
suspend as `needs_input`. Background tasks and workflows retain their run
record and expose the same action in the task result. Claude Code, Codex, and OpenClaw should normally call LiveFigure through
`omni mcp serve`, keeping the VLM key in Omni rather than the external host
process.

### Configure embeddings later

Existing users can configure all embedding fields in one command:

```bash
# Semantic recall: endpoint must serve /embeddings
omni config embeddings --enable \
  -u https://api.openai.com/v1 \
  -m text-embedding-3-small \
  -k sk-...

# Keyword recall: the master switch prevents all embedding probes
omni config embeddings --disable
```

Individual fields work through both shell and REPL config commands:

```bash
omni config set memory.embeddings_enabled true
omni config set memory.embedding_provider openai_compatible
omni config set memory.embedding_base_url https://api.openai.com/v1
omni config set memory.embedding_model text-embedding-3-small
omni config set memory.embedding_api_key sk-...  # secrets.toml, masked in output
```

Direct-file equivalent:

```toml
# <OMNI_HOME>/config.toml
[memory]
embeddings_enabled = true
embedding_provider = "openai_compatible"
embedding_base_url = "https://api.openai.com/v1"
embedding_model = "text-embedding-3-small"

# <OMNI_HOME>/secrets.toml
[memory]
embedding_api_key = "sk-..."
```

## Update

The public update surface is intentionally small:

```bash
omni update
omni update status   # diagnostics only
```

### 1) Startup prompt (auto-detect)

When you launch `omni` interactively and a newer release exists, it shows a Codex-style menu:

```text
New version available: 2.0.0 -> 2.0.1
  Update with `omni update` or reinstall through your package manager
> 1. Update now (run omni update)
  2. Skip
  3. Skip this version
```

The network check runs in the background and caches its result in
`<OMNI_HOME>/update-check.json` (default TTL 24h). Startup reads only that local cache, so it never
blocks on the network and remains silent offline. The prompt appears only on an interactive TTY.
Choosing **Update now** runs the parameterless `omni update` transaction and automatically
relaunches the foreground CLI on success. Choosing **Ignore this version** suppresses only that
version.

### 2) `omni update`

`omni update` detects the owner of the running installation:

- a PyPI package owned by uv runs `uv tool upgrade OmniScientist-V2`;
- a PyPI package owned by pipx runs `pipx upgrade OmniScientist-V2`;
- a dedicated Python environment upgrades through that exact interpreter;
- a linked source checkout fast-forwards with `git pull --ff-only`, then reinstalls it;
- an explicit moving development channel re-resolves its branch tip.

A dirty or diverged checkout aborts without stashing, resetting, or discarding work. To deploy the
current local tree, including uncommitted changes, rerun `./cli/scripts/install.sh`; use
`--editable --local` for a contributor install.

The package operation and local lifecycle are one serialized transaction. Omni quiesces the
supervisor, reserves the Home Service singleton, updates the package, prepares bundled runtimes,
retires old per-workspace daemons, refreshes the launcher, restores the prior
service state, waits until the new gateway has claimed the singleton (control-plane READY is
preferred; IM channels may still be connecting), and records the converged package fingerprint. If a
step fails, the
fingerprint remains pending so the next `omni`/`omni update` retries instead of silently accepting
mixed versions. SQLite migrations stay lazy and data-preserving: each store is backed up and
reconciled when first opened by the new CLI or Home Service.

#### Behavior matrix

| Audience | Install | Update |
|---|---|---|
| **User, uv** | `uv tool install OmniScientist-V2` | `omni update` or `uv tool upgrade OmniScientist-V2`, then `omni` |
| **User, pipx** | `pipx install OmniScientist-V2` | `omni update` or `pipx upgrade OmniScientist-V2`, then `omni` |
| **Developer, snapshot** | `./cli/scripts/install.sh` | rerun the installer for the current tree; `omni update` follows git upstream |
| **Developer, editable** | `./cli/scripts/install.sh --editable --local` | edits load on next launch; rerun the installer to re-sync dependencies |

### 3) Direct package-manager update

Direct package-manager updates are supported:

```bash
uv tool upgrade OmniScientist-V2          # isolated uv tool install
pipx upgrade OmniScientist-V2             # pipx install
```

Package managers replace only Python files. They cannot know `OMNI_HOME`, lock the Home Service,
coordinate local runtime/state compatibility, prepare Node assets, or restart the supervisor. The next bare
`omni` detects the changed installation fingerprint and performs those local steps before entering
the REPL. There is no need to remember a second `omni update` command.

### Turn the check off

The startup check is on by default. Disable it with `omni config set update.check false` or the
`OMNI_UPDATE_CHECK=0` environment variable (for CI / air-gapped machines). Tune it via
`update.interval_hours` (TTL, default 24). PyPI is the default authority; `update.source=auto|raw`
and `update.raw_url` remain explicit development-channel compatibility settings.

To avoid conflicts, keep one installation owner per shell. Start with the structured diagnostic,
then use native shell inspection if needed:

```bash
omni doctor
```

```bash
# macOS / Linux
command -v omni        # the path that will run
command -v -a omni     # if this prints multiple paths, the first one wins
```

```powershell
# Windows (PowerShell)
Get-Command omni       # the path that will run
where.exe omni         # if this prints multiple paths, the first one wins
```

If multiple `omni` executables appear, rerun the installer and choose migration rather than relying
on PATH reordering:

```bash
./cli/scripts/install.sh --on-conflict migrate
```

For `uv tool` installs, run `uv tool update-shell` once and reopen the terminal if the upgraded CLI
is not immediately visible. `omni update` is preferred over a bare global package-manager command
because it is bound to the interpreter that owns the running CLI.

## Two ways to run every command

Once installed, the same verbs work in two interchangeable forms — pick whichever fits the moment:

| Form | How | Example |
|---|---|---|
| **Shell command** | `omni <command> …` from your terminal (good for scripts/CI) | `omni channel list` · `omni task list` |
| **REPL slash command** | start `omni`, then type `/<command> …` at the `›` prompt | `/channel list` · `/task list` |

Inside the REPL, anything that is **not** a slash command is treated as a chat prompt to the agent.
Slash commands re-run the very same code as their `omni …` counterparts (with your current
`--project`/`--profile`/`--model`), so behavior is identical on every OS. A bare natural-language
shell prompt (`omni "Explain diffusion models"`) is chat; a mistyped command (`omni chanel list`) returns an
"unknown command" suggestion instead of being sent to chat.

## Use it

```bash
# one-shot
omni "Summarise recent advances in 3D Gaussian Splatting for SLAM"
omni "Analyze arXiv 2310.06825"

# non-interactive (scripts/CI): read a task from a file or stdin.
# Defaults to workspace-auto (in-workspace writes + sandboxed bash/compute).
# Pass --ask on a TTY to keep the approval prompt.
omni exec -f task.md -o answer.md
echo "Summarize 2310.06825" | omni exec -q

# interactive REPL (commands: /skills, /task, /stop, /steer, /memory, /new, /exit)
omni

# in a specific project workspace
omni project new robotics
omni --project robotics "draft a related-work section on visual SLAM"

# sessions: list, continue, replay, export
omni session list
omni session resume <id>           # continue in the REPL (or append a prompt for one-shot)
omni replay <id>

# reference library → BibTeX (arxiv-fetch / openalex-search populate it)
omni cite list
omni cite export -f bibtex -o refs.bib

# background research task results
omni task list
omni task inbox
```

> arXiv tools need network access. Offline (the `mock` model), `omni "Analyze arXiv 2310.06825"`
> still runs, but `arxiv-fetch` returns a clean `{"status":"error", …}` instead of paper data —
> it cannot reach `export.arxiv.org`.

### Grounded research workflow

What turns omni into a *research* agent: a local literature corpus, an auditable
hypothesis→claim→evidence trail, a reproducible run ledger, and an honesty pass over what was
recorded. Every research verb is also a REPL slash command (`/lit`, `/verify`, `/bench`, `/hypo`, …).

```bash
# 1) search OpenAlex; Omni records returned sources in the local workspace
omni '$openalex-search retrieval-augmented generation'

# 2) grounded, cited Q&A over that corpus ([S#] in-text citations) + optional honesty audit
omni lit "How does RAG reduce hallucination?" --verify

# 3) the Research Object Model (also visible via /hypo /claim /evidence in the REPL)
omni hypo new "scaling laws hold for protein LMs" -c 0.6
omni claim list                         # claims + evidence counts (0 = unsupported)
omni evidence add <claim_id> --source <source_id> --stance supports
omni run show <run_id>                   # cmd/seed/env-lock/metrics for a logged experiment

# 4) trust checks
omni verify                              # audit claims: unsupported / contradicted / over-confident
omni bench --k 3                         # offline retrieval recall@k / MRR (no network)

# 5) reproducible retrieval: pin an "as-of" date so a corpus query is repeatable
omni config set research.as_of 2024-12-31
```

> The full design — capabilities, comparison with Claude Code/Codex and other open-source research
> agents — is in [`research-agent-design.md`](research-agent-design.md); the command reference is in
> [`commands.md`](commands.md).

### Parallel & async delegation (subagents)

For long-horizon research the coordinating agent can hand focused subtasks to isolated **specialist
subagents**. Each specialist runs its own bounded ReAct loop with a fresh context — it never sees the
coordinator's transcript or its siblings' — and returns only a compact summary, so the coordinator's
context stays small. An optional reviewer scores each summary and can ask for one revision.

- **Blocking fan-out (default, on):** the model calls `spawn_subagents` to run a batch in parallel —
  e.g. read several papers at once, or test competing hypothesis branches — and receives all
  summaries together.
- **Async fire-and-collect (opt-in):** turn it on to let the coordinator spawn a specialist, keep
  working, and collect the result later — the reliable pattern when one subtask is slow (a deep
  search or a long experiment) and you want to overlap it with drafting.

```bash
omni config set subagents.async_enabled true
```

With async on, the coordinator also gets `spawn_subagent` (fire one, return a handle),
`wait_subagent` (collect a specific one, or whichever finishes first), `message_subagent` (steer a
running one), `followup_subagent` (continue a finished one with its result as context), plus
`list_subagents` and `interrupt_subagent`. Collecting is explicit: a finished subagent posts its
answer to the coordinator, but it never starts a new turn on its own.

Specialists are bounded and privilege-reduced by default (no write/shell/compute unless explicitly
granted) and may run in a separate git worktree or container. Inspect them with
`omni task list --kind subagent`. Configuration keys and per-tool semantics are in
[`agent-runtime-harness.md`](agent-runtime-harness.md); the design and Codex-parity notes are in
[`architecture.md`](architecture.md).

### Profiles

A profile is a named overlay (`<OMNI_HOME>/<name>.config.toml`) for switching model stacks:

```bash
omni profile add local -p ollama -u http://localhost:11434/v1 -m qwen2.5 --use
omni profile list
omni --profile local "Run this question with the local model"   # one-off; or `omni profile use local` to default
```

## Export skills to Claude Code / Codex / OpenClaw

`skills export` copies omni's built-in skills **out** into the system roots those tools read, so
Claude Code / Codex / OpenClaw can use them too. This is the opposite of `skills add`, which imports
an external skill **into** omni. The wizard can export during `omni init`; later, use either surface:

| Action | Shell | Inside `omni` |
|---|---|---|
| Export to all three tools | `omni skills export` | `/skills export` |
| Export explicitly to all | `omni skills export --all` | `/skills export --all` |
| Export only to Codex | `omni skills export codex` | `/skills export codex` |
| Show discovery roots | `omni skills sources` | `/skills sources` |
| Remove Omni-exported copies | `omni skills unexport` | `/skills unexport` |

You pick from three tools — `claude`, `codex`, `openclaw`. Because current Codex/OpenClaw also read
the shared `~/.agents/skills` root, exporting to either writes there too, so the skills are found
regardless of tool version. The same `SKILL.md` files are valid Claude Code / Codex / OpenClaw
skills, so they appear natively in those tools. OmniScientist also *discovers* skills authored for
those tools (see below).

To go in the other direction—install an external skill for normal use inside
Omni—import it, inspect it while quarantined, and trust it:

```bash
omni skills add claude:my-skill       # also codex:, agents:, openclaw:, a local path, or a Git URL
omni skills info my-skill
omni skills trust my-skill --yes

omni exec "Use the most appropriate installed workflow for this task"  # natural-language selection
omni exec '$my-skill Run this exact workflow for the task'             # forced selection
```

The `$name` form is optional. `skills add` also accepts a local `SKILL.md` or
other `.md` file and a Git `#path/to/skill` fragment; direct Git imports use the default
branch. Raw HTTP(S) files, archives, and repository `/tree/...` pages are not
accepted. Contracted skills participate in deterministic
capability routing; trusted plain skills can be discovered by description in
the ReAct loop. Read [skills.md](skills.md) for the precise guarantee boundary,
complete source matrix and limits, loading order, execution types, security
checks, and Claude Code/Codex/OpenClaw
comparison.

## Make OmniScientist usable from Claude Code / Codex (MCP)

```bash
omni mcp install both     # registers `omni mcp serve` into ~/.codex and ~/.claude.json
```

Then in Claude Code / Codex you can call `omni_ask`, `openalex-search`, `arxiv-fetch`, etc.
See [`compatibility.md`](compatibility.md).

## Scheduled tasks

Ask in natural language ("summarize today's research every day at 6pm", "run this once tomorrow at 9am") and Omni
registers a durable schedule, or drive it directly:

```bash
omni schedule add agent-goal --cron "0 18 * * *" --input '{"input":"summarise today’s research"}' --title "daily digest"
omni schedule list                 # trigger, next fire, last-run time + status
omni schedule show <id>            # + last-run status/result, artifact paths, and recent run history
omni schedule run                  # fire all due jobs now (handy without a daemon)
```

Two things matter for a schedule to actually produce output:

1. **It fires from the always-on home service.** One service per `OMNI_HOME` dispatches every
   workspace's due schedules, and it is already running: launching `omni` brings the service up, and
   creating a schedule confirms it — no separate step. You can still run `omni schedule run` to fire due
   jobs on demand. `omni status` shows scheduler health (enabled count, next/last fire) and warns when
   schedules exist but the service is not running.
2. **Unattended autonomy.** A scheduled run has no interactive approver, so by default a sensitive
   tool (`write_file` / `edit_file` / `run_compute`) would fail closed and the run would produce
   nothing. Because creating a schedule is itself an owner-consented action, each fire becomes a
   first-class task (visible in `omni task`) that is granted a **bounded** tool set controlled by
   `schedules.autonomy`:

   | `schedules.autonomy` | scheduled runs may use |
   |---|---|
   | `off` | nothing (fail-closed; sensitive tools stay blocked) |
   | `standard` *(default)* | `write_file`, `edit_file`, `run_compute` (write artefacts / run compute; **no** arbitrary shell) |
   | `full` | `standard` + `bash` (arbitrary shell — only when trusted) |

   Override per schedule with `omni schedule add … --allow-tool write_file --allow-tool run_compute`
   (pass no `--allow-tool` to inherit the default; the OS sandbox / filesystem roots still confine
   *where* those tools may act). As a last-resort manual escape hatch you can instead widen the global
   gate in `~/.omni/config.toml` under `[security] approval_allowlist = ["bash"]`, but that applies to
   **every** context, not just this schedule — prefer the per-schedule grant above.

Results land in the inbox (`omni task inbox` or REPL `/inbox`) and the run's task
(`omni task show <id>`) exactly like any other background task.

## Always-on home service (IM channels)

Channels (WeChat / Feishu / DingTalk) and the background service work the **same on macOS, Linux, and
Windows**. The only OS difference is credential storage (see the cross-platform note below). Every
command here also exists as a REPL slash command — e.g. `/channel login feishu …`, `/serve status`.

There is **one** always-on service per `OMNI_HOME` (`omni serve`): it owns the messaging channels and
dispatches every workspace's schedules, supervised by the OS (macOS launchd / Linux systemd-user /
Windows Scheduled Task) so it survives logout and restarts on crash. It comes up automatically when you
launch `omni` — omni is not usable without it — so configuring a channel just applies dynamically to the
running service (`--start` guarantees it is up first); inbound messages are routed to the **anchor
workspace** (the `default` project). `omni serve stop` is a **transient pause**: it stops the service
now, but the next `omni` launch brings it back (to keep it off, set `service.ensure_on_launch = false`).

### Managing the service (`omni serve`)

`omni serve` (aka `/serve` in the REPL) is the single command that runs and manages the home service —
there is no separate `omni service` command. Lifecycle subcommands act on the OS-supervised background
service; the foreground ones run it in your current terminal (mostly for debugging or when a supervisor
is unavailable). Every subcommand also exists as a slash command (`/serve start`, `/serve status`, …).

| Subcommand | What it does |
| --- | --- |
| `omni serve start` | Enable and start the always-on, OS-supervised service (macOS launchd / Linux systemd-user / Windows Scheduled Task). It survives logout and restarts on crash. Usually redundant — launching `omni` already brings the service up — but handy to force it up right now. |
| `omni serve status [--verbose] [--all]` | Show the single home service: desired state, live runtime, the channel **anchor**, and channels. `--verbose` expands the per-workspace schedule-dispatch breakdown; `--all` lists any lingering legacy per-workspace daemons under `OMNI_HOME`. Always safe to poll. |
| `omni serve restart` | Manually reload the service onto current config. `omni update` already refreshes/restores it transactionally, so no extra restart is needed after an update. Restart succeeds once the new process has claimed the singleton; WeChat/Feishu/DingTalk may still be reconnecting — check `omni serve status`. |
| `omni serve stop [--all] [--ghosts]` | **Transient pause**: stop the service now (the next `omni` launch brings it back; to keep it off, set `service.ensure_on_launch = false`) and clean up legacy per-workspace daemons for this workspace; `--all`/`--ghosts` sweep them everywhere. |
| `omni serve doctor` | Diagnose supervisor availability, desired-vs-actual drift, and any lingering legacy daemons, with hints to fix them. |
| `omni serve prune` | Stop ghost legacy daemons and optionally remove their stale data directories. |
| `omni serve` / `omni serve run` / `omni serve daemon` | Run the service in the **foreground** (this terminal): channels + every workspace's schedules. `run` is the supervisor's entrypoint; `daemon` is an alias of a bare `omni serve`. |
| `omni serve poller` | Foreground run **without** owning channels (equivalent to `omni serve --no-channels`): dispatch schedules only. |

Flags on the foreground forms: `--channels "wechat,feishu"` limits which channels are owned (defaults to
your configured set), `--workers N` sets per-workspace background-task concurrency, and `--no-channels`
dispatches schedules only. Prefer `omni serve start` for day-to-day use so the OS keeps it alive.

```bash
omni serve start            # force the always-on home service up now (usually redundant)
omni serve status           # the single service: desired state, runtime, anchor, channels
omni serve status --verbose # + per-workspace schedule-dispatch breakdown
omni serve doctor           # diagnose supervisor drift / lingering legacy daemons
omni serve restart          # manual config reload/recovery; update already handles this
omni serve stop             # transient pause; the next `omni` launch brings it back
omni serve poller           # foreground: dispatch schedules only, no channels (debug)
```

```bash
omni channel add feishu   # optional: write <OMNI_HOME>/channels/feishu.toml template
omni channel login wechat --start
omni channel login feishu --app-id cli_xxx --app-secret "$FEISHU_APP_SECRET" --start
omni channel login dingtalk --client-id ding_xxx --client-secret "$DINGTALK_CLIENT_SECRET" --start
# stores credentials, prints a QR/setup link, creates a short-lived /pair code, and applies the channel to the running home service
omni channel pair feishu    # the /pair code is single-use and expires in 10 minutes; this issues a new one
omni channel list         # see enabled / configured channels
omni channel test feishu  # check the config is complete
omni serve status         # the single home service: desired state, runtime, anchor, channels
omni serve status --all   # list any lingering legacy per-workspace daemons under OMNI_HOME
omni serve restart        # reload the home service after config/code changes
omni serve stop           # transient pause (the next `omni` launch brings it back)
```

On **Windows (PowerShell)** the commands are identical; use PowerShell's `$env:` for secrets:

```powershell
omni channel login wechat --start
omni channel login feishu --app-id cli_xxx --app-secret $env:FEISHU_APP_SECRET --start
omni serve status
```

CLI works out of the box. WeChat / Feishu / DingTalk require `omni channel login <name>` or
`/channel login <name>`: it writes
secure defaults, stores platform secrets, and enables allowlist/pairing before IM messages can reach
the local agent. The default installer includes the Feishu/DingTalk channel SDKs via the `channels`
extra. IM channels need the always-on home service, which launching `omni` already brings up (and
`channel login --start` guarantees is up), so it keeps the iLink long-poll / Feishu WebSocket /
DingTalk Stream connection alive. The service is supervised by the OS, so it survives closing the
terminal *and* logging out, and restarts on crash. Exiting the interactive CLI does not stop it;
`omni serve stop` only pauses it until your next `omni` launch (to keep it off, set
`service.ensure_on_launch = false`).

After starting the interactive `omni` REPL, use the corresponding slash commands:

```text
/channel login wechat --start
/channel login feishu --app-id cli_xxx --app-secret '<FEISHU_APP_SECRET>' --start
/channel login dingtalk --client-id ding_xxx --client-secret '<DINGTALK_CLIENT_SECRET>' --start
/channel pair feishu
/channel list
/channel test feishu
/serve status
```

The REPL does not expand shell variables such as `$FEISHU_APP_SECRET`, so quote the literal value
(it is redacted in the transcript and in history). You may instead omit the option and type it at
the hidden prompt, but that hands the terminal to the child command, and the `/pair` code it prints
disappears when the REPL repaints — run `/channel pair <name>` afterwards to get a fresh one.

The home service is **home-scoped**: one process per `OMNI_HOME` owns each WeChat/Feishu/DingTalk
account (from the anchor workspace) and dispatches every workspace's schedules, so multiple CLI
windows can chat and inspect any workspace safely without fighting over a channel. Home-level channel
locks still guard against a stray legacy per-workspace daemon claiming an account; `omni serve status`
shows the anchor and channels, and `omni serve status --all` lists any lingering legacy daemons to
retire.

Readiness is three layers, written to `<OMNI_HOME>/service/service.pid`:

- **starting** — the process has claimed the singleton (new code is in memory).
- **ready** — agents and task runtimes can accept work (schedules, inbound tasks). This is
  control-plane READY; it does **not** wait for WeChat / Feishu / DingTalk HTTP.
- **channel_health** — each IM adapter reports `starting` / `running` / `degraded` on its own.

`omni update` always restarts this gateway onto the new code. It succeeds once the new process has
claimed the singleton; a still-starting service is a warning, not an update failure. Use
`omni serve status` to watch channels reconnect.

### Cross-platform notes (credentials & QR)

- **Credential storage.** No flag required anywhere: macOS uses the system **Keychain**, and
  **Windows and Linux have no built-in encrypted store**, so `channel login` writes secrets to
  `<OMNI_HOME>/secrets.toml` (`0600` on POSIX; keep it user-only on Windows) and says so. A locked
  Keychain or a headless/SSH session falls back to the same file with a warning, so a credential you
  just earned by scanning a QR is never thrown away. `--credential-store file` forces the file on any
  OS and `--credential-store keychain` refuses to start without one, but neither is needed normally.
- **QR codes.** Login draws a compact QR from Unicode half blocks (about 39x20 cells, so it fits an
  80x24 window) and keeps that size without colour by varying the glyph. Terminals that cannot draw
  block characters print the plain link instead; `--no-qr` always prints just the link.
- **Stopping/restarting.** `omni serve stop` / `omni serve restart` work cross-platform (Windows uses
  the OS terminate path rather than POSIX signals). `omni serve status [--all]` is always safe to run.

### WeChat

Shell `omni channel login wechat --start` and REPL `/channel login wechat --start` go through
Tencent's **official WeChat ClawBot bot API** (the iLink protocol on `ilinkai.weixin.qq.com`, the same
backend as the `@tencent-weixin/openclaw-weixin` plugin). It prints the
`liteapp.weixin.qq.com/...&bot_type=3` QR, you scan it with WeChat, and you then chat with the
**WeChat ClawBot** — no gateway, no local port, no Node, no OpenClaw, and no public webhook. After
the scan the connector stores the returned `bot_token` (macOS Keychain, otherwise `secrets.toml`),
records the per-account messaging host, and auto-allows the WeChat account that scanned, so WeChat
never needs a `/pair` code. `omni serve` then keeps a `getupdates` long-poll open and replies through
`sendmessage`.

```bash
omni channel login wechat --start   # the one command; identical on Linux, macOS, and Windows
```

- Tencent's *WeChat ClawBot* terms govern the paired account.
- **Session lifetime**: an iLink connection is valid for about 24 hours. When it lapses the service
  logs that the token expired and stops replying; rerun `omni channel login wechat --start` to
  rebind. There is no automatic re-scan yet.
- **Images, files & typing**: replies that carry artifacts (plots, PDFs) are AES-128-ECB encrypted and
  uploaded to WeChat's CDN, then sent as native image/file messages; inbound images/files/videos are
  downloaded, decrypted, and handed to the agent as a local path. A "typing…" indicator shows while
  the agent works. These need the optional `cryptography` backend (`pip install "OmniScientist-V2[channels]"`);
  without it, media gracefully falls back to a text link. Disable typing with `typing_indicator = false`
  in `<OMNI_HOME>/channels/wechat.toml`.
- Use the official ClawBot iLink QR. Self-hosted `:8088` bridges and WeCom are not supported.
- The scanning account is bound automatically. Additional WeChat users can self-bind by sending
  `/pair <code>` (from `omni channel login` or `/channel login`) in the bot conversation.
