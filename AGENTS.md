# AGENTS.md — working in the OmniScientist repo

Guidance for Codex / Claude Code (and OmniScientist itself) when editing this repository.

## Repository layout

Two independent parts: the Python app in `cli/` and the portable skill collection in `skills/`.
The CLI runs skills; skill content never imports CLI internals.

## Build / test / lint

```bash
uv venv --python 3.12 .venv
uv pip install -e "./cli[dev,mcp,vec]" --python .venv   # the package lives in cli/
.venv/bin/pytest -q                                      # run from cli/ (or: pytest cli/tests)
.venv/bin/ruff check cli/src
```

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
