# OmniScientist V2 CLI (`omni`)

A local-first, CLI-first personal **research agent**. `omni` plans work, calls
**skills** (Claude Code / Codex / OpenClaw `SKILL.md` compatible), runs a bounded
ReAct loop, remembers across sessions (SQLite + filesystem only), and ships with
WeChat / Feishu / DingTalk channels.

This CLI is the official next-generation implementation of the OmniScientist framework described
in [Shao et al., *OmniScientist: Toward a Co-evolving Ecosystem of Human and AI
Scientists*](https://doi.org/10.48550/arXiv.2511.16931). The current checkout reports package
version `2.0.0rc4` (planned Git tag `v2.0.0rc4`).

> This folder (`cli/`) is the Python application. The research **skills** are a
> separate, independent top-level collection in
> [the repository's `skills/` directory](https://github.com/tsinghua-fib-lab/OmniScientist-V2/tree/master/skills).
> The CLI
> *runs* skills; the skill packages do not depend on CLI internals.
>
> New users should start with the repository-level [installation and initialization
> guide](https://github.com/tsinghua-fib-lab/OmniScientist-V2#install-and-initialize).
> This file is the package/developer quick reference.

## Install

`omni` runs on macOS, Linux, and Windows. It requires Python ≥ 3.11 and Node.js ≥ 20.9
to run the bundled research-pptx renderer; npm is needed only for the first
`omni skills setup research-pptx` (pnpm is not a substitute). PyPI is the stable
package authority; install into one isolated tool environment:

```bash
uv tool install OmniScientist-V2
# or
pip install OmniScientist-V2
# or
pipx install OmniScientist-V2

omni
```

The first bare `omni` opens setup, prepares the lockfile-pinned Node runtime, and starts or repairs
the single Home Service. Normal updates use one product command:

```bash
omni update
```

`omni update` delegates package replacement to the manager that owns the running CLI (`uv tool`,
pipx, or an explicit dedicated Python environment), then performs runtime preparation, legacy
daemon cleanup, Home Service launcher refresh/restoration, and readiness verification as one
serialized lifecycle transaction. The public command is intentionally parameterless;
`omni update status` is read-only diagnostics.

Direct manager updates are supported too:

```bash
uv tool upgrade OmniScientist-V2
pipx upgrade OmniScientist-V2
```

Those commands replace Python packages only. The next bare `omni` detects the changed package
fingerprint and automatically completes the missing local runtime/service convergence before
entering the REPL; there is no second update command to remember.

From a downloaded source checkout, use the repository installer. It defaults to an isolated uv
tool and deploys the current local snapshot; it never silently selects an active Conda/venv:

```bash
./cli/scripts/install.sh
./cli/scripts/install.sh --editable --local  # contributor install
```

```powershell
powershell -ExecutionPolicy Bypass -File cli\scripts\install.ps1
```

Rerun the repository installer to redeploy the current tree. `omni update` on a linked clean
checkout safely fast-forwards its configured upstream and reinstalls. A standalone copy of the
installer uses PyPI; the moving `master` channel is available only as an explicit development
choice. Installing into an active environment requires the explicit advanced `--method env`
(`-Method env`) option.

Then, identically on every OS:

```bash
omni --version
omni init                           # model, retrieval mode, workspace, optional exports/MCP
omni doctor                         # environment and configuration checks
omni                                # interactive REPL
omni "Explain diffusion models in one sentence"  # one-shot
omni update                         # owner-aware package + lifecycle update
```

Like Codex, interactive startup reads a local update cache and offers a one-key update when a newer
PyPI release is known. Network refresh happens in the background (TTL 24h), stays silent offline,
and can be disabled with `omni config set update.check false` or `OMNI_UPDATE_CHECK=0`.

Full per-OS install/update/serve walkthrough:
[`getting-started.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/getting-started.md);
platform table:
[`commands.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/commands.md#platform-notes).

## First-run model setup

`omni init` walks you through it (provider menu, default **openai**). `omni` otherwise defaults to an
offline `mock` model so it runs with zero config. Run `omni model` (or `/model` in the REPL) for the
guided main/VLM/embedding picker; `omni model status` and `omni model explain` show the effective
values and their winning configuration layers. Point the main role at a real provider in one
advanced command:

```bash
omni config model -p deepseek -u https://api.deepseek.com/v1 \
  -m deepseek-chat -k "$DEEPSEEK_API_KEY" --test
omni config list                              # provider / model / endpoint / key status
```

These commands keep the existing persistent active-`OMNI_HOME` scope. Root `--model <name>` remains
a non-persistent override for one launch.

`-p` accepts `openai` / `deepseek` / `ollama` (all OpenAI-compatible; `base_url` picks the real
service) or `mock`. Prefer files? Edit `~/.omni/config.toml` (`[model]`) and `~/.omni/secrets.toml`
(`[model] api_key`). The key is stored in `secrets.toml` and masked everywhere.

## Shell and REPL commands

Use `omni <command> ...` in a terminal or start `omni` and use `/<command> ...` in the REPL. Both
forms dispatch to the same command implementation and configuration:

```text
Shell                         REPL
omni config list              /config list
omni task list               /task list
omni channel list             /channel list
omni serve status             /serve status
omni autosota status          /autosota status
```

In a capable TTY the REPL commits its transcript to the terminal's native scrollback above a bottom
dock containing the composer and permanent status line. It stays in the normal buffer with no mouse
capture, so use the terminal's own selection (click-drag, or Shift-drag/copy-mode in a multiplexer)
to copy history and its usual wheel/keys to scroll. Blank Enter is ignored. `auto` falls back to the
classic prompt for pipes, redirection, CI, `TERM=dumb`, and unsupported terminals. Enter sends;
Ctrl+O and Alt/Option+Enter insert a newline, as does Shift+Enter when the terminal reports the
modifier. Use trailing `\` + Enter as a portable continuation, or Ctrl+X Ctrl+E to edit the draft
with `$VISUAL`/`$EDITOR`. Multiline paste remains one request. Override the UI mode with
`omni --ui tui|classic`, `OMNI_UI=tui|classic`, or
`omni config set display.ui_mode auto|tui|classic` (`tui` selects the inline dock). Committed
transcript text stays in shell scrollback after exit, and Omni's conversations and tasks remain
persisted.

Natural language has no slash inside the REPL. Channel messages are processed by the persistent
`omni serve` owner, not by an open REPL window. See the complete [channel lifecycle and login
guide](https://github.com/tsinghua-fib-lab/OmniScientist-V2#message-channels).
Slash commands do not invoke a shell, so `$ENV`, pipes, and
redirections are not expanded; omit secret options to use a hidden prompt.

## Skills

Every skills operation has both a terminal form and a slash form after starting `omni`:

| Action | Shell | Inside `omni` |
|---|---|---|
| List managed skills | `omni skills list` | `/skills list` |
| Include external libraries | `omni skills list --all` | `/skills list --all` |
| Inspect one skill | `omni skills info <name>` | `/skills info <name>` |
| Import into quarantine | `omni skills add <local-path\|tool:name\|git-url>` | `/skills add <local-path\|tool:name\|git-url>` |
| Trust after review | `omni skills trust <name> --yes` | `/skills trust <name> --yes` |
| Show disabled skills | `omni skills list --disabled` | `/skills list --disabled` |
| Restore a disabled skill | `omni skills restore <name>` | `/skills restore <name>` |
| Export all built-ins | `omni skills export` | `/skills export` |
| Export only to Codex | `omni skills export codex` | `/skills export codex` |

Local paths may be a skill directory, `SKILL.md`, or another `.md` file;
`tool:name` supports `claude:`, `codex:`, `agents:`, and `openclaw:`. Direct
Git imports use the default branch. See
[`skills.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/skills.md)
for
the complete source matrix and limits, installation, trust, natural-language
and explicit invocation, loading, and cross-agent comparison. See
[`skills/README.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/skills/README.md)
for the built-in catalogue and
[`skills/docs/authoring.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/skills/docs/authoring.md)
for authoring.

## Research commands

omni is a *research* agent, not a coding one: it grounds answers in a local literature corpus
(`omni lit`, cited `[S#]`), records an auditable hypothesis→claim→evidence trail
(`omni hypo/claim/evidence`), keeps a reproducible run ledger (`omni run`), and audits its own
claims (`omni verify`) / retrieval (`omni bench`). All are also REPL slash commands. See
[`commands.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/commands.md)
and
[`research-agent-design.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/research-agent-design.md).

## Docs

- [`getting-started.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/getting-started.md) — install, configure, first run, research workflow
- [`commands.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/commands.md) — command reference (incl. research commands)
- [`skills.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/skills.md) — skill install, discovery, automatic/explicit invocation, and agent comparison
- [`testing-and-evaluation.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/testing-and-evaluation.md) — native test stack and Inspect AI selection
- [`research-agent-design.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/research-agent-design.md) — design, capabilities & how it compares
- [`architecture.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/architecture.md) — request flow & internals
- [`compatibility.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/cli/docs/compatibility.md) — Claude Code / Codex / OpenClaw / MCP

## Develop

```bash
cd cli
uv run --extra dev --extra mcp --extra vec --extra channels pytest -q
uv run --extra dev ruff check src
uv run --extra dev omni eval --coverage
uv run --extra dev omni eval --research-quality
```

Apache-2.0. See
[`LICENSE`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/LICENSE),
[`SECURITY.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/SECURITY.md), and
[`PRIVACY.md`](https://github.com/tsinghua-fib-lab/OmniScientist-V2/blob/master/PRIVACY.md).
