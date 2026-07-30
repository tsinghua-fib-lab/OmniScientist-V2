# CLI reference

For a complete copy-paste validation matrix covering command-line and REPL forms, see
[`cli-validation-guide.md`](cli-validation-guide.md). For agent, skill, and workflow validation,
see [`agent-validation-guide.md`](agent-validation-guide.md).

Global options (before any subcommand): `--project/-P`, `--profile`, `--model/-m`, `--ui auto|tui|classic`, `--continue/-c` (bare `omni` continues this workspace's latest session), `--trust/--no-trust`, `--out`, `--debug`, `--version/-V`.

**Two ways to run every command** (identical on macOS / Linux / Windows): as a **shell command**
`omni <command> …`, or as a **REPL slash command** `/<command> …` after starting `omni`. The slash
form re-runs the same code with your active `--project`/`--profile`/`--model`; e.g. `omni channel
list` ≡ `/channel list` and `omni task list` ≡ `/task list`. See
[Platform notes](#platform-notes) for the few OS-specific details (credential storage, QR, daemon).

## Command design conventions

Omni follows the same command-shape conventions as modern agent CLIs such as Claude Code and
Codex:

- **Commands / subcommands say what to do.** Examples: `omni task all`,
  `omni skills list`, `/channel login`.
- **Arguments and options say how to do it or what to filter.** Examples:
  `--session <id>`, `--json`, `--page 2`, `<query>`.
- **Command groups are discoverable.** If a command has subcommands, the group entry points to
  `<command> help` in the REPL, or `<command> --help` in shell contexts. A group may have a
  documented default behavior, but its help must clearly label that behavior.
- **Detailed options live in command help.** `/help` and the REPL startup screen show the
  important subcommands and only the most important options. Full options belong in
  `<command> help` or `<command> <subcommand> --help`.
- **Examples are commands, not overloaded help.** For example, `skills examples` shows the 1~7
  workflow capability prompts; `skills help` shows the skills command group contract.

| Command | Description |
|---|---|
| `omni` | Interactive REPL. On a fresh installation with no model configuration, the first bare launch runs the `omni init` setup wizard before opening the REPL. Later launches open the REPL directly (`-c`/`--continue` resumes the latest session). A capable TTY gets an inline dock — committed history in the terminal's native scrollback (natural-width lines you can mid-line select/copy, survives exit, re-laid-out at the new width on resize) plus a bottom composer/status dock with no mouse capture and an idle-quiescent dock so a mouse selection stays highlighted for native copy (`Alt+Y`/`/copy` copies the last answer via OSC 52; the startup command overview and `/help` start collapsed, while `Ctrl+T` expands or folds long help/raw-output blocks in place without entering the alternate screen); `auto` falls back to classic mode for pipes, CI, `TERM=dumb`, and unsupported terminals. Select with `--ui`, `OMNI_UI`, or `display.ui_mode` |
| `omni "<prompt>"` / `omni chat "<prompt>"` | One-shot question (`-c` continue, `-q` quiet, `-v` verbose live progress, `--detach`, `--mode auto\|plan\|review`) |
| `omni status` | Show the active workspace, its store path, and daemon/task state |
| `omni resume [id]` | Resume a session in this workspace (picker if no id; `--last`) |
| `omni exec` | Non-interactive run from file/stdin/args (`-f file`, `-o out`, `-c`, `-q`, `-v`, `--detach`). In a trusted directory this is Codex Never + workspace-write (in-workspace writes + sandboxed `bash`/`run_compute`, no prompt). An untrusted directory stays read-only. `--ask` keeps the TTY approval loop; without a TTY it warns and still fail-closes |
| `omni init` | Run the setup wizard explicitly: choose the data directory (default `~/.omni`), provider (menu, default **openai**), key, retrieval mode, and optional Semantic Scholar key, then initialize local storage. `--home <path>` persists a custom data directory. Optional skill export and MCP registration default to **No**. Cold-start flags include `--embeddings/--no-embeddings`, embedding fields, and `--semantic-scholar-api-key`. Configure the optional VLM afterwards with `omni config vlm` or `omni model vision`. Re-running shows current config and per-item adjustment commands |
| `omni model` | Show or configure the persistent main / vision / embedding model stack |
| `omni trust` | Trust or revoke a directory for writes and repo-local `.omni` config (`--list`, `--revoke`) |
| `omni current` | Show the session focus (active artifact, paper, task, or source) |
| `omni why [task]` | Explain route, plan, provider selection, and settlement for a task |
| `omni doctor` | Environment/config diagnostics, including active executable, owning interpreter/install method, version, effective PATH order, conflicting Omni copies, host terminal, tmux extended-key state, and the terminal repair command |
| `omni terminal status\|setup\|help` | Inspect host/tmux keyboard capabilities or safely enable modified-key reporting. `setup` previews the managed tmux block, confirms before writing, backs up the old file, applies atomically, and reloads the current server. `/terminal-setup` is the REPL alias; `--check` never writes |
| `omni update [status]` | Parameterless owner-aware update. PyPI installs use their actual owner (`uv tool upgrade` or `pipx upgrade`); a dedicated environment uses its running interpreter; source checkouts fast-forward safely and reinstall. The same serialized transaction prepares bundled runtimes, retires legacy daemons, refreshes/restores the Home Service, waits until the new gateway has claimed the singleton (IM channels may still be connecting), and records a package fingerprint. SQLite migrations remain lazy, backed up, and data-preserving when a store is first opened by the new code. A direct `uv tool upgrade`/`pipx upgrade` is also supported: the next bare `omni` detects the fingerprint change and performs only the missing local convergence. `status` is read-only diagnostics. Advanced legacy switches remain accepted but hidden from help |
| `omni uninstall` | Ownership-aware removal. `--dry-run` previews; default removes the current program and Omni-managed integrations while preserving research data; `--purge` removes `OMNI_HOME`; `--everything` also removes registered in-place project data, identical untracked built-in exports, and all detected installations. Add `--yes` for non-interactive execution and `--json` for structured output |
| `omni config list\|get\|set\|unset\|path\|home\|model\|vlm\|embeddings\|semantic-scholar\|test` | Inspect/edit layered config; `home [PATH]` shows or changes the persistent data directory and `home --reset` restores `~/.omni`; `model ...` configures chat, `vlm ...` configures the optional owner-controlled visual model, `semantic-scholar` stores the literature token, while `embeddings --enable -u <base_url> -m <model> -k <api_key>` configures semantic recall and `embeddings --disable` selects keyword recall; `test` checks chat connectivity |
| `omni autosota get\|config\|init\|prepare\|login\|run\|status\|resume\|steer\|doctor\|inspect\|info\|exec` | Explicit bridge to the independent AutoSOTA CLI. `get` installs one official Release into Omni's private npm cache; `config` keeps keys in owner-only `secrets.toml`; `init`/`prepare`/`run`/lifecycle commands execute in the foreground. AutoSOTA, not Omni, owns environments, GPU scheduling, and long-running optimisation. See [autosota.md](autosota.md). |
| `omni skills help\|list\|info\|search\|sources\|examples\|setup\|why` | Browse omni-managed skills; add `--all` to include Claude Code/Codex/OpenClaw libraries. `help` shows subcommands and important parameters; `examples` shows 1~7 workflow capability prompts with both `omni -P ... exec` and REPL trigger forms; `setup research-pptx` repairs the pinned Node renderer; `why` explains a skill routing decision. In a terminal `list` opens an interactive pager (↑/↓ · PgUp/PgDn · `/` search · `q` quit); `--no-pager` prints in place with `--page`/`--page-size`, `--group` groups by source, `--source` filters one source |
| `omni skills add\|trust\|untrust\|remove\|restore\|enable` | Import an external/local skill into quarantine, grant/revoke trust after review, remove or disable it, and restore a disabled skill. `add` accepts a local skill directory, local `SKILL.md`/`.md`, `claude:\|codex:\|agents:\|openclaw:<name>`, or a Git URL with an optional `#path/to/skill` fragment; use `add --name` to rename one import and `add --force` to replace an existing copy. Raw HTTP(S) files, archives, and Git `/tree/...` pages are not accepted. Inside the REPL use the corresponding `/skills ...` form |
| `omni skills export\|unexport` | Export the built-ins to Claude Code / Codex / OpenClaw; pick tools (`export codex`) or all (`export --all`). Former names `install\|uninstall` remain as aliases |
| `omni soul [status]\|list\|create\|help` | Manage scientist personas. Bare `soul` and `status` show the active persona plus SoulAgent's effective scanner root; `list` enumerates locally discoverable persona KGs (`--json` for scripts); `create <scientist>` submits a focused `$scientist-kg-distiller` run that builds, validates, and atomically installs a new KG without activating it. Identity hints include `--field` and `--institution`; `--workspace`, `--resume/--no-resume`, `--max-sources`, `--detach`, and `--dry-run` control creation. In the REPL use `/soul`, `/soul list`, or `/soul create ...` |
| `omni project list\|new\|info` | Manage workspaces; `list` shows named + path-keyed workspaces, `new` creates a named project, and `info` shows the active workspace |
| `omni profile list\|add\|use\|show` | Configuration profiles (`<OMNI_HOME>/<name>.config.toml`, `--profile`) |
| `omni session list\|show\|resume\|export\|fork` | Inspect / continue / export sessions; `fork` copies history into an independent branch |
| `omni memory help\|list\|search\|add\|pin\|detail\|rm\|clear\|edit\|sync\|profile\|notebook\|graph\|link\|path` | Inspect/curate long-term memory + lab notebook. `list/search` show truncated summaries (`detail <id>` for full text), `rm <id>` deletes one entry (pinned needs `--force`), `clear` bulk-deletes by `--type/--layer/--scope` (needs `--yes`, keeps pinned), `edit` opens `<OMNI_HOME>/MEMORY.md` in `$EDITOR` and re-imports it, `sync` re-imports curated files by hand; `graph`/`link`/`path` inspect memory links |
| `omni cite list\|export` | Browse the project library; export BibTeX/JSON/CSV |
| `omni task help\|list\|session\|all\|show\|subtask\|step\|watch\|attach\|approve\|steer\|cancel\|retry\|resume\|requeue\|drain\|inbox\|archive\|unarchive\|rm\|delete\|clear\|prune` | Tasks (user requests), WorkflowRuns, stable WorkflowSteps, Skill Execution attempts, Child Tasks, and completion inbox; `list` defaults to this workspace's tasks of kind `turn` (`--kind subagent\|maintenance\|all` shows system records), `show <id>` resolves any execution object, `subtask <task>` lists only Skill Executions, and `step <workflow-run> <step>` focuses one logical step. `approve` releases a plan-mode task; `steer` supplies an instruction at the next safe execution boundary; `cancel` requests cooperative cancellation; `retry` creates a new Skill Execution attempt while preserving the step id; `resume` continues from persisted state; `requeue` returns one standalone skill execution to the queue in place. `watch` follows status, `attach` restores a selected result boundary into a session, and `drain` executes pending work without the daemon. `archive` hides a task without losing provenance; `rm`/`delete`, `clear`, and `prune` delete task history under their documented safeguards. |
| `omni artifacts preview\|diff\|versions\|review` | Review generated artifacts (figures, drafts, tables). `preview <id>` shows metadata + a text head, `diff <old> <new>` unified-diffs two text artifacts (DOT/Markdown/SVG/JSON…), `versions <id>` lists the version/revision family, `review <id>` audits health (file/contract/render derivatives, revision link, task status, and `source/claim/evidence` provenance) |
| `omni channel help\|list\|add\|login\|pair\|remove\|test` | Configure and bind messaging channels (WeChat / Feishu / DingTalk); `add` writes a template, `login` starts platform auth / QR pairing, `pair` completes a `/pair <code>` bind. The always-on home service already owns channels (it comes up when you launch `omni`), so a new login is applied dynamically within seconds — `--start` just guarantees the service is up first. Inbound messages are routed to the anchor workspace (the `default` project) |
| `omni mcp serve\|install\|uninstall\|list\|agents` | MCP bridge, register/unregister Omni in Codex/Claude without changing unrelated MCP servers, list external servers, emit AGENTS.md |
| `omni replay <session>` | Replay a session's Q&A + tool trace |
| `omni serve` / `serve run\|daemon\|poller\|start\|stop\|restart\|status\|doctor\|prune\|help` | The always-on **home service**: one OS-supervised process per `OMNI_HOME` that owns messaging channels and dispatches **every** registered workspace's schedules (there is no separate `omni service` command — it was folded into `omni serve`). It comes up automatically when you launch `omni`; omni is not usable without it. `start` enables it and installs an OS supervisor (macOS launchd / Linux systemd-user / Windows Scheduled Task) so it survives logout and restarts on crash; `restart` reloads the latest installed code/config; `stop` is a **transient pause** — it stops the service now, but the next `omni` launch brings it back (to keep it off, set `service.ensure_on_launch = false`) — and also stops legacy per-workspace daemons for this workspace (`--all`/`--ghosts` sweep them everywhere). Foreground `omni serve` / `serve run` (the supervisor entrypoint) / `daemon` run it in this terminal; `poller` / `--no-channels` dispatch schedules without owning channels. `status` presents the single service (desired state, live runtime, channel **anchor**, channels); `--verbose` expands the per-workspace schedule-dispatch breakdown and `--all` lists any lingering legacy daemons; `doctor` diagnoses supervisor availability and drift. **The service is what fires due schedules**, so it must be running (or use `omni schedule run`) for schedules to execute unattended |
| `omni schedule add\|list\|all\|show\|remove\|enable\|disable\|run` | Recurring / one-time scheduled jobs. Trigger with exactly one of `--every N`, `--cron "…"`, or a one-time `--at ISO` (`--once` is the historical alias); a **naive `--at` is your local wall-clock** (add `--timezone` for another zone), the same semantics the `schedule_task` agent tool uses, and a time already in the past is refused with guidance instead of being silently created. The job is a registered skill (positional `<skill>` + `--input`) or a free-form goal (`--goal "…"` / trailing text, which schedules the `agent-goal` sub-agent). `list` shows this workspace's schedules — trigger, next fire, last-run time, and status; `all` aggregates schedules across **every** registered workspace, tagged with their workspace (the parity to `task all`, since the home service dispatches them all); `show <id>` adds the full definition, the last run's status/result/**artifact paths**, and history; `run` fires due jobs now (`--drain` executes in-process). Each fire is a first-class task (in `omni task`) carrying the schedule's unattended-autonomy grant (see `schedules.autonomy`); `add --allow-tool <name>` overrides that grant per schedule. Schedules fire from `omni serve` |
| `omni schedule proposals\|approve\|deny\|clarifications` | Durable approval for schedule requests that arrive without a local approver (e.g. an IM `schedule_task`). Such a request is not silently created or dead-ended — it is persisted as a proposal that `proposals` lists and `approve <id>` / `deny <id>` resolve. Approval executes the **exact stored, digest-checked** request (never a command re-composed from prose) and is idempotent. `clarifications` lists open schedule-time time-of-day drafts the original requester must answer |

> Unknown leading tokens: a natural-language prompt (`omni "Explain diffusion models"`) is sent to chat,
> but a mistyped/unimplemented command (e.g. `omni profil list`, or an unknown word carrying
> option flags) returns an "unknown command" error with a suggestion — it is **not** silently
> turned into a chat prompt.

> Flag placement: only the **global** options (`-P/--project`, `--profile`, `-m/--model`, `--ui`,
> `-c/--continue`, `--trust/--no-trust`, `--out`, `--debug`, `-V/--version`) may precede the subcommand. Per-command flags must come
> **after** it — e.g. `omni chat "…" -q` works, but `omni -q chat "…"` errors with
> `No such option: -q` (`-q` belongs to `chat`, not the top-level app).

## Research commands

These turn omni into a grounded, auditable research agent: a local literature corpus
(cited `[S#]`), a Research Object Model (hypotheses → claims → evidence → sources), a
reproducible run ledger, an honesty `--verify` pass, and an offline retrieval benchmark.

| Command | Description |
|---|---|
| `omni lit "<question>"` | Native grounded Q&A over the local corpus (shows citable passages, then answers with in-text `[S#]`). `--k N` controls retrieval depth; `--verify` audits claims already recorded in the active session; `-q` is quiet mode |
| `omni verify` | Honesty pass over recorded claims: flags unsupported / contradicted / over-confident. `--session/-s` scopes to one conversation (default: whole workspace). Does **not** call the model — it audits what was recorded |
| `omni bench` | Offline retrieval benchmark (recall@k / MRR) on a bundled gold set in a throwaway store — no network, no effect on your workspace. `--k N`, `--embed` scores with the configured embedding model |
| `omni eval --research-quality` | Deterministic CI-ready research-quality checks for citation fidelity, statistical invariants/tolerances, and reproducibility manifests. Use `--quality-input <json>` for a project fixture and `--json` for machine-readable output |
| `omni hypo list\|new\|show\|status` | Falsifiable hypotheses; `status` adjudicates `proposed/testing/supported/refuted/inconclusive` |
| `omni claim list\|new\|show` | Claims with calibrated confidence + their evidence count (0 = unsupported) |
| `omni evidence add <claim>\|list <claim>` | Bind a claim to a source (`--source`, `--stance supports\|contradicts\|mentions`, `--quote`) |
| `omni run list\|show <id>` | Experiment-run ledger (cmd/seed/env-lock/metrics/outputs) so every reported number is traceable |
| `omni source list\|show <id>\|reindex` | Local source corpus; `reindex` imports `library.jsonl` into the structured store (idempotent) |

Built-in research **skills** are the explicit active set: `arxiv-fetch`, `openalex-search`,
`paper-review`, `review-response`, `scientific-figure`, `scientific-poster`,
`livefigure`, `research-ideation`, `research-pptx`, `scientist-kg-distiller`,
and `soulagent`. `paper-review` is a
complete pipeline engine: it starts MinerU as soon as a local PDF is accepted,
extracts text concurrently, starts full-manuscript structured understanding as
soon as that text is ready, and overlaps both it and the bounded Semantic Scholar
stage with the still-running MinerU/VLM path before rendering and validating the
full venue form.

The primary review model may be text-only (for example, DeepSeek) because visual
inspection uses the separately configured VLM. With no VLM, `paper-review`
immediately reports the missing visual capability, continues the text review and
MinerU crop extraction as a partial run, and returns `omni config vlm` plus the
`skip_visual=true` option. Run `omni config vlm --test` to catch a text-only model
that was accidentally configured as the VLM before reviewing a full paper.

```bash
omni exec '$paper-review input="/absolute/path/paper.pdf" venue="ACL 2025 Main Conference — Long Papers" mode=standard'
```

Writing deliverables use native synthesis, e.g. `draft.section`, rather
than a separate skill; the synthesis draft labels every conclusion **grounded / inferred / insufficient evidence**
(sourced / inferred / insufficient) so a reader can tell claims apart from evidence.

**Connectors** are the external data sources (arXiv, OpenAlex, Crossref, Unpaywall, PubMed,
Semantic Scholar, bioRxiv, and ClinicalTrials.gov) — a layer distinct from skills (workflows).
They are curated in one `ConnectorRegistry`
that owns enablement (`research.connectors` allow-list) and **secret-scope**: each connector only
receives the secrets it declares (e.g. Unpaywall/Crossref read `research.contact_email` for the
polite pool; a connector never sees another's credentials).

Relevant config (`omni config set research.<key> <value>`): `as_of` (date-pin retrieval for
reproducibility), `corpus_top_k`, `chunk_target_words`, `contact_email`, `connectors`, and
`semantic_scholar_api_key`. Request a key from the
[Semantic Scholar API page](https://www.semanticscholar.org/product/api), then configure and test
the owner-scoped token with `omni config semantic-scholar -k <API_KEY> --test`. You can also use
`omni config set research.semantic_scholar_api_key <KEY>` or pass
`--semantic-scholar-api-key <KEY>` to `omni init`; the interactive wizard prompts for it too.
Omni stores the token in `<OMNI_HOME>/secrets.toml`; `config get` and `config list` mask it, and
`omni config unset research.semantic_scholar_api_key` removes it. A cloned project's config cannot
replace the token. Complete `paper-review` requires it; `research-ideation` uses it automatically
when configured and otherwise continues through Semantic Scholar's more rate-limited public access.

## REPL slash commands

| Form | Type | Description |
|---|---|---|
| `/help` | command | Show REPL help with command/subcommand/important-option labels |
| `/terminal status` | subcommand | Show host terminal, tmux, Shift+Enter readiness, and the Ctrl+J fallback |
| `/terminal-setup` | command | Preview and confirm the reversible tmux setup flow; use `--check` for diagnostics only |
| `/stop` | command | Cancel the active turn in this session; completed results, artifacts, and workflow checkpoints are retained |
| `/steer <instruction>` | command + argument | Redirect an active semantic ReAct turn; deterministic work is transferred to the foreground next-turn queue |
| `/copy` | command | Copy the latest assistant answer to the system clipboard via OSC 52 (works over SSH/tmux); `Alt+Y` is the dock keybinding. Writes a scrollback notice (`copied last answer (N characters)`, or a failure with a next step) |
| `/new` | command | Start a new session |
| `/mode auto\|plan\|review` | command + argument | Switch the current REPL interaction mode; `plan` waits for approval, `review` uses a read-only tool surface |
| `/model` | command | Show or switch the persistent model stack (same surface as `omni model`) |
| `/current` | command | Show the active artifact, paper, task, or source focus |
| `/why [task]` | command + argument | Explain route, plan, provider selection, and settlement |
| `/trust` | command | Trust or revoke the current directory |
| `/compact` | command | Compact older turns and report estimated token savings |
| `/context` | command | Show the session context budget and injected sections |
| `/verbose quiet\|normal\|verbose` | command + argument | Set live progress detail (plan decisions, tool calls with args/results, workflow step hierarchy); no argument shows the current level |
| `/plan <request>` | command + argument | Create and persist a plan for one request without executing it |
| `/review <request>` | command + argument | Review one request with a read-only tool surface and a forced bounded self-review of the output |
| `/resume [id]` | command + argument | Resume/switch a session in this workspace; `/resume help` has details |
| `/autosota` | command group | Show the external AutoSOTA launcher commands; `/autosota run ...` remains a foreground native process, not an Omni task |
| `/skills` | command group | Show the skills command group help; `/skills <query>` searches |
| `/skills help` | subcommand | Show skills subcommands and important parameters |
| `/skills examples` | subcommand | Show 1~7 workflow capability prompts with command-line and REPL validation steps |
| `/skills list` | subcommand | List omni-managed skills; `--all`, `--group`, `--no-pager`, `--page`, `--source` are options |
| `/skills add codex:my-skill` | subcommand | Import an external skill into quarantine |
| `/skills trust <name> --yes` | subcommand | Enable an imported skill after reviewing its source, license, and executable files |
| `/skills remove <name>` | subcommand | Remove an imported skill or disable a built-in/external skill according to its source |
| `/skills list --disabled` | subcommand | List skills disabled through `skills.disabled` |
| `/skills restore <name>` | subcommand | Restore a disabled skill; `/skills enable <name>` is an alias |
| `/skills export [tools]` | subcommand | Export built-ins to Claude Code, Codex, or OpenClaw; `/skills unexport` removes Omni-owned copies |
| `/channel list` | subcommand | Show enabled, configured, and runtime state for messaging channels |
| `/channel login <name> ...` | subcommand | Configure and bind WeChat, Feishu, or DingTalk; omit secret options to use the hidden prompt |
| `/channel test <name>` | subcommand | Run the local channel config/dependency check |
| `/serve status [--all]` | subcommand | Show the home service (desired state, runtime, anchor, channels); `--all` lists any lingering legacy per-workspace daemons under `OMNI_HOME` |
| `/schedule` | command group | List this workspace's schedules with trigger, next fire, last-run time and status (bare `/schedule` ≡ `/schedule list`) |
| `/schedule all` | subcommand | The same schedule list aggregated across all registered workspaces (the home service fires them all) |
| `/schedule show <id>` | subcommand + argument | Full schedule definition + last-run status/result/**artifact paths** + recent run history + next-step hints |
| `/schedule add\|remove\|enable\|disable\|run` | subcommand | Create (`--every\|--cron\|--at`, `--goal`, `--allow-tool`; naive `--at` is local time), delete, toggle, or fire-now (`run --drain`) a schedule |
| `/task` | command group | List this workspace's tasks (user requests); `--kind subagent\|maintenance\|all` shows system records |
| `/task session` | subcommand | List tasks from the current REPL session |
| `/task all` | subcommand | The same task list across every store on disk (path-keyed workspaces + named projects), not only `workspaces.json` |
| `/task show <id>` | subcommand + argument | Show a Task, WorkflowRun, WorkflowStep, Skill Execution, or Child Task; `--json` is an output option |
| `/task subtask <task>` | subcommand + argument | List the Skill Execution attempts owned by a Task |
| `/task step <workflow-run> <step>` | subcommand + argument | Inspect one stable WorkflowStep, including input, result, attempts, child task, error, and recovery commands |
| `/task approve <task>` | subcommand + argument | Release a plan-mode task for execution |
| `/task steer <task> <instruction>` | subcommand + arguments | Queue an instruction for the next ReAct iteration; deterministic runners reject detached steering with a follow-up hint |
| `/task cancel <task>` | subcommand + argument | Request cooperative cancellation at the next safe execution boundary |
| `/task retry <execution> [--step <id>]` | subcommand + argument | Retry a direct Skill Execution, or pass a WorkflowRun plus `--step` to create a new attempt while preserving the WorkflowStep id |
| `/task resume <execution> [--step <id>]` | subcommand + argument | Continue a Skill Execution or WorkflowStep from persisted state |
| `/task rm\|delete\|clear\|prune` | subcommand | Delete current-workspace task history: `rm/delete <id...>` deletes one tree immediately or previews multiple until `--yes`; active descendants always block and protected descendants require `--force`; `clear`/`prune` apply the same full-tree protection |
| `/artifacts preview\|diff\|versions\|review <id>` | subcommand + argument | Review generated artifacts: preview text/metadata, diff two versions, list a revision family, or `review` health + `source/claim/evidence` provenance |
| `/inbox` | command | Task-completion notifications |
| `/memory <q>` | command + argument | Bare text → semantic recall; a leading subcommand (`list`, `search`, `add`, `rm`, `detail`, `edit`, `help`, …) dispatches to `omni memory <sub>` |
| `/lit "<q>"` | command + argument | Grounded literature Q&A (`[S#]` cites); `--verify`, `--k N` are options |
| `/verify` | command | Audit recorded claims; `--session` scopes to the active session |
| `/bench` | command | Offline retrieval benchmark; `--k N`, `--embed` are options |
| `/eval` | command | Offline behavior/coverage benchmark; `--research-quality` enables citation/statistics/reproducibility checks |
| `/hypo new "<s>"` | subcommand + argument | Record a hypothesis; also `/hypo list|show|status` |
| `/claim list` | subcommand | List claims + evidence counts; also `/claim new|show` |
| `/evidence add <claim> --source <id>` | subcommand + options | Bind a claim to a source |
| `/run list|show <id>` | subcommand | Experiment-run ledger |
| `/source list|reindex` | subcommand | Source corpus; `reindex` imports `library.jsonl` |
| `/exit` / `/quit` / empty `Ctrl+D` | command / key | Cancel active work if needed, close the session and terminal UI, then leave; `omni serve` keeps running |

> The REPL exposes every research verb as a slash command. `/lit`, `/verify`, `/bench` run
> **in-process** so they share the active session (the answer + recorded claims belong to the
> current conversation); the ROM groups (`/hypo`, `/claim`, `/evidence`, `/run`, `/source`)
> reuse the same per-workspace store, so a claim the agent recorded mid-chat is visible there.

## Examples

```bash
omni "Explain diffusion models in three sentences"
omni -P thesis "draft an outline for a survey on retrieval-augmented generation"
omni exec -f task.md -o answer.md          # non-interactive, write the answer to a file
omni skills search slam
omni skills examples                       # 1~7 workflow capability prompts + validation commands
omni skills info research-pptx              # skill names are hyphenated (kebab-case)
omni skills setup research-pptx             # repair the lock-pinned owner runtime
omni autosota get                           # explicitly install the external AutoSOTA CLI
omni autosota config --workspace /data/autosota --repo /data/target --prompt-secrets
omni autosota prepare my-paper --workspace /data/autosota
omni autosota run my-paper --workspace /data/autosota --dry-run
omni skills list --all                      # interactive pager in a terminal: ↑/↓ · / search · q quit
omni skills list --all --group              # group by source: per-source counts + sections
omni skills list --all --no-pager --source user_claude  # in-place output, one source
omni skills list --all --no-pager --page 2  # in-place pagination (page 2)
omni skills list --all | grep adaptyv       # piped/redirected → full list (no pager), script-friendly
omni skills add codex:my-skill              # import into quarantine (<OMNI_HOME>/skills)
omni skills add ./workflow.md --name my-skill  # local Markdown becomes <name>/SKILL.md
omni skills add 'https://github.com/org/repo.git#skills/my-skill'  # default Git branch only
omni skills trust my-skill --yes            # enable after source/license review
omni exec "Use the best installed skill to analyse this task"  # allow implicit selection
omni exec '$my-skill Analyse this task'      # force one exact installed skill
omni skills export codex                     # export omni's built-ins to Codex (or: --all for all three tools)
omni model                                   # Codex-style picker of main presets, plus vision / embedding
omni model deepseek-chat                     # one-shot main-model switch (also: /model deepseek-chat)
omni model status                            # effective three-role stack and primary sources
omni model explain main                      # field-level layered-config provenance
omni config model -p openai -u https://api.deepseek.com/v1 -m deepseek-chat -k sk-xxx --test  # -p/-u/-m/-k = provider/base_url/model/api_key
omni config vlm --endpoint https://vision.example/v1/chat/completions --model vision-model --api-key sk-xxx --test
omni config semantic-scholar --api-key s2-xxx --test
omni config set react.max_iterations 8       # opt into a hard turn ceiling; -1 = unbounded, 0 = no iterations
omni profile add local -p ollama -u http://localhost:11434/v1 -m qwen2.5 --use
omni memory add "I work on neuromorphic vision" --pin
omni memory list --type preference          # truncated summaries; `memory detail <id>` for full text
omni memory rm 1a2b3c                        # delete one entry (pinned needs --force)
omni memory edit                             # open <OMNI_HOME>/MEMORY.md in $EDITOR (global, re-imported on save)
omni status                                 # which workspace/DB am I in? is the home service running?
omni serve start                            # enable the always-on home service (channels + all workspaces' schedules)
omni serve start --no-channels              # dispatch schedules only; don't own messaging channels
omni serve status                           # the single home service: desired state, runtime, anchor, channels
omni serve status --verbose                 # + per-workspace schedule-dispatch breakdown
omni serve status --all                     # list any lingering legacy per-workspace daemons under OMNI_HOME
omni serve restart                          # reload updated code/config into the home service
omni serve stop                             # transient pause; the next `omni` launch brings it back
omni serve doctor                           # diagnose supervisor drift / lingering legacy daemons
omni serve poller                           # foreground: dispatch schedules only, no channels (debug)
omni resume                                 # pick a past session in this workspace
omni resume --last                          # or: omni -c   → jump back into the latest session
omni session list                          # then: omni session resume <id>
omni task list                             # tasks (user requests) in this workspace; same list in every terminal here
omni task list --kind maintenance          # system/maintenance tasks (default view hides them)
omni task session                          # tasks from the active session
omni task all                              # the same task list aggregated across all workspaces
omni task show 9a1b2c3d                    # readable Task/Workflow/Step/Execution/Child Task detail
omni task show 9a1b2c3d --json             # raw structured payload
omni task subtask 9a1b2c3d                # Skill Execution attempts owned by a Task
omni task step flow1234 diagram            # inspect one stable WorkflowStep + attempts/recovery
omni task approve 9a1b2c3d                 # execute a persisted plan-mode task
omni task steer 9a1b2c3d "focus on ablations" # steer at the next safe boundary
omni task cancel 9a1b2c3d                  # cooperative cancellation
omni task retry flow1234 --step diagram    # fresh attempt from the step input snapshot
omni task resume flow1234 --step diagram   # continue from the step checkpoint
omni task watch                            # follow task status; press q to return
omni task attach 5aba1eea                  # attach a Task/Workflow/Step/Execution result to a session
omni task drain                            # execute pending workflows and Skill Executions now
omni task prune --yes                       # sweep failed + stale pending; a protected/active descendant keeps its tree
omni task clear --status failed --yes       # filter-based cleanup (or --before 30d), with full-tree protection
omni task rm 5aba1eea --force               # one Task tree deletes immediately; active work always blocks
omni task rm 5aba1eea 7bc91d2f              # multiple Task ids/prefixes preview without deleting
omni task rm 5aba1eea 7bc91d2f --yes        # confirm the atomic multi-Task deletion
omni config set tasks.retention_days 30      # auto-delete failed/cancelled/interrupted tasks older than 30d
# ── schedules (fire from `omni serve`) ──
omni schedule add agent-goal --cron "0 18 * * *" --input '{"input":"summarise today\u2019s research"}' --title "daily digest"
omni schedule list                          # id, trigger, next fire, last-run time + status
omni schedule show f6c600e9                 # definition + last-run status/result/artifact paths + history
omni schedule run                           # fire all due jobs now (add --no-drain to only enqueue)
omni schedule add --goal "one-off summary" --at 2099-01-01T09:00 --allow-tool write_file --allow-tool run_compute
omni schedule add "summarise today's research" --at 2099-01-01T09:00   # trailing text ⇒ agent-goal; naive --at is local time
omni schedule proposals                     # schedule requests awaiting owner approval (e.g. from IM)
omni schedule approve 4a40d5d9              # execute the exact stored request; `deny <id>` rejects it
omni serve start                            # creating a schedule lazily enables this; run it explicitly to be sure it fires unattended
omni project info                           # show the active workspace and storage paths
omni channel login wechat --start           # official ClawBot QR; the scanning account binds itself
omni channel login feishu --app-id cli_xxx --app-secret "$FEISHU_APP_SECRET" --start
omni channel login dingtalk --client-id ding_xxx --client-secret "$DINGTALK_CLIENT_SECRET" --start
omni channel pair feishu                    # new /pair code; the previous one is single-use and lasts 10 min
omni channel test feishu                    # config + SDK presence; not a live platform round-trip
omni cite export -f bibtex -o refs.bib      # export the project library
omni mcp install both
omni task show 5aba1eea
omni replay 5aba1eea
# ── research ──
omni '$openalex-search retrieval-augmented generation'  # search; Omni records returned sources locally
omni lit "How does RAG reduce hallucination?" --verify        # grounded answer ([S#] cites) + honesty audit
omni hypo new "scaling laws hold for protein LMs" -c 0.6
omni claim list                             # claims + evidence counts (0 = unsupported)
omni evidence add 1eaa097d --source 9f8e2a1b --stance supports
omni run show 1a2b3c                         # cmd/seed/env-lock/metrics for a logged run
omni source reindex                          # import library.jsonl → structured sources
omni verify                                  # audit all recorded claims in this workspace
omni bench --k 3                             # offline retrieval recall@k / MRR
omni eval --research-quality                 # bundled citation/statistics/reproducibility baseline
omni eval --quality-input quality.json --json # evaluate a structured research-quality fixture
omni config set research.as_of 2024-12-31    # date-pin retrieval for reproducibility
```

Inside a running `omni` REPL, the same skill-management flow is:

```text
/skills search slam
/skills info livefigure
/skills list --all
/skills add codex:my-skill
/skills trust my-skill --yes
/skills remove livefigure
/skills list --disabled
/skills restore livefigure
/skills export codex
/channel login feishu --app-id cli_xxx --app-secret "$FEISHU_APP_SECRET" --start
/channel pair feishu
/channel list
/serve status
```

> Note: `arxiv-fetch` queries the public arXiv API and
> therefore **need network access**. The offline `mock` model can still *drive* the command, but
> with no route to `export.arxiv.org` the tool returns a clean `{"status":"error", …}` (it retries
> a few times and never crashes the CLI) — it does not fabricate paper data.

## Platform notes

`omni` and every command/slash-command behave identically on macOS, Linux, and Windows. The only
OS-specific details:

| Topic | macOS | Linux | Windows |
|---|---|---|---|
| Install / update | `cli/scripts/install.sh`, `omni update` | `cli/scripts/install.sh`, `omni update` | `cli\scripts\install.ps1`, `omni update` |
| Uninstall | `omni uninstall`, `cli/scripts/uninstall.sh` | same | `omni uninstall`, `cli\scripts\uninstall.ps1` |
| Data home | `~/.omni` (`config home`, `$OMNI_HOME` override) | `~/.omni` (same) | `%USERPROFILE%\.omni` (`config home`, `$env:OMNI_HOME` override) |
| Channel credentials | system **Keychain** by default | `secrets.toml` (0600) automatically | `secrets.toml` automatically |
| Background service | `omni serve start/stop/restart/status` (launchd) | same (systemd user unit) | same (Scheduled Task) |
| Login QR | Unicode QR, or `--no-qr` for the link | same | Windows Terminal renders it; legacy consoles print the link |

- **Credentials.** The same command works on every OS with no storage flag. Windows/Linux have no
  built-in encrypted store, so `omni channel login …` writes secrets to `<OMNI_HOME>/secrets.toml`
  (mode 0600 where the OS supports it) and says so; a locked Keychain or SSH session on macOS falls
  back there too, with a warning, rather than discarding a credential you just earned by scanning.
  `--credential-store file|keychain` overrides the choice when you need to pin it.
- **Service liveness is safe to poll on every OS.** `omni serve status` never terminates the service
  (on Windows it queries the process handle instead of signalling it).
- **Path separators.** Use `/` in the shell examples on macOS/Linux and `\` on Windows
  (`cli\scripts\install.ps1`); options and subcommands are spelled the same everywhere.
