# AGENTS.md — working in the OmniScientist repo

Guidance for Codex / Claude Code (and OmniScientist itself) when editing this repository.

## Repository layout

Two independent parts: the Python app in `cli/` and the portable skill collection in `skills/`.
The CLI runs skills; skill content never imports CLI internals.

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
- app docs: `cli/docs/`; skill docs: `skills/docs/`

## Research outputs

Workspaces are path-keyed by absolute working directory (like Claude Code): a `-P <name>` project
lives under `~/.omni/projects/<name>/`, otherwise the auto workspace is `~/.omni/workspaces/<slug>-<hash8>/`
(keyed to the VCS root, else CWD; `~/.omni` is never itself a project). Generated artifacts go under
`<workspace>/artifacts/`; the lab notebook is `NOTEBOOK.md`. `omni status` shows the active store;
`omni project migrate` moves legacy `default`/home-edge data into the current workspace. Prefer
citing sources (arXiv id / DOI / URL).
