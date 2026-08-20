# AGENTS.md — working in the OmniScientist repo

Guidance for Codex / Claude Code (and OmniScientist itself) when editing this repository.

## Repository layout

Three independent parts: the Python app in `cli/`, the portable skill collection in `skills/`,
and the loopback SPA in `web/`. The CLI runs skills; skill content never imports CLI internals.
`omni web` is another surface over the same agent, not a second intelligence.

## Build / test / lint

```bash
uv venv --python 3.12 .venv
uv pip install -e "./cli[all,dev]" --python .venv   # the package lives in cli/
.venv/bin/pytest -q                                  # run from cli/ (or: pytest cli/tests)
.venv/bin/ruff check cli/src
```

Install every extra, the same set CI installs with `uv sync --all-extras`, so a
local run and a CI run execute the same tests. The narrower `[dev,mcp,vec]` this
once documented left `tokens` out, which meant the tokenizer tests skipped for
every developer and ran only in CI — and the estimator those tests guard was
measuring transcripts in different units in the two places. Every extra costs
41 MB and seven seconds more than the narrow set. Note that having `tiktoken`
installed does not change what the suite measures: `conftest` pins compaction to
the estimator that ships, and tests about the real tokenizer opt back in.

## Conventions

- Python ≥ 3.11, `src/` layout, package root `cli/src/omni`.
- Local-first: persistence is SQLite + filesystem only (no MySQL/Redis/MinIO/Chroma).
- Tests must run offline (use the `mock` LLM provider / `ScriptedLLM`). Never hit a network model.
- `SKILL.md` stays Claude-Code compatible; OmniScientist-only fields go under `metadata.helixforge`.
- Keep CLI and skill code independent: a `python_engine` skill keeps its `engine.py` inside its own
  `skills/<name>/` folder and may import only omni's public runtime (e.g. `omni.research`).
- Keep `ruff` clean; type-annotate public functions; docstrings explain intent.

## Where things are

- request flow: `cli/src/omni/agent/orchestrator.py`
- ReAct loop: `cli/src/omni/core/react_agent.py`
- skill runtime (discovery/exec/install): `cli/src/omni/skills_runtime/`
- public research helpers reused by engines: `cli/src/omni/research/`
- built-in skill *content*: top-level `skills/` (each `<name>/SKILL.md` + optional `engine.py`)
- compatibility (MCP / Codex / Claude): `cli/src/omni/compat/`
- loopback web surface: `cli/src/omni/web/` (ASGI) + top-level `web/` (Vite + React SPA)
- app docs: `cli/docs/`; skill docs: `skills/docs/`

`omni web` (extra `[web]`) serves the SPA on `127.0.0.1:1088` and refuses `0.0.0.0`.
Opening a directory in the UI calls `get_paths(cwd=that path)` — the same store key as
`omni` launched in that folder. Refresh restores the open workspace from the URL hash;
named projects use `workspace.select`, not `open` of `~/.omni/projects/<name>`. The Settings panel and first-run modal write the same
user `config.toml` / `secrets.toml` as `omni config` (`omni.config.user_edits`); theme
and language stay in the browser. Settings → Skills lists packaged built-ins plus
`~/.omni/skills` through Home-level `skill.*` RPCs (local add / trust / remove never
touch built-ins or the project tree). After a web save this process drops cached agents so
the next turn reloads settings. Other Omni processes follow the CLI rule: a new command
reads disk immediately; an open REPL or `omni serve` restarts. The UI is a wheel payload (`omni/data/web`), not a
live Vite app: release CI runs `cli/scripts/build_web_ui.sh` before `uv build`, and
`omni update` replaces that tree with the new package. `omni web` never rebuilds an
already-packaged SPA (users have no Node). After update, restart `omni web` so the
new process serves the new files; `index.html` is no-cache and `/assets/*` are
content-hashed. Explicit local/editable deployment rebuilds `web/dist` and fails
before package replacement if it cannot; ordinary commands never run the frontend
toolchain. Editable checkouts resolve that `web/dist`, and `omni doctor` reports
its path and stamped version.

## Research outputs

Workspaces are path-keyed by absolute working directory (like Claude Code): a `-P <name>` project
lives under `~/.omni/projects/<name>/`, otherwise the auto workspace is `~/.omni/workspaces/<slug>-<hash8>/`
(keyed to the VCS root, else CWD; `~/.omni` is never itself a project). Generated artifacts go under
`<workspace>/artifacts/`; the lab notebook is `NOTEBOOK.md`. `omni status` shows the active store;
`omni project migrate` moves legacy `default`/home-edge data into the current workspace. Prefer
citing sources (arXiv id / DOI / URL).
