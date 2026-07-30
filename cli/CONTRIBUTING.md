# Contributing to OmniScientist V2

Thanks for your interest! OmniScientist V2 is an open-source, local-first research agent.

## Dev setup

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev,mcp,vec,channels]" --python .venv
.venv/bin/pytest -q          # run tests
.venv/bin/ruff check src     # lint
.venv/bin/ruff format src    # format
```

## Project layout

See [`docs/architecture.md`](docs/architecture.md). Key entry points:

- `src/omni/agent/orchestrator.py` — request flow
- `src/omni/core/react_agent.py` — the ReAct loop
- `src/omni/skills_runtime/` — skills (parsing, registry, executor)
- `src/omni/research/` — research subsystem (ROM store, corpus, connectors, tools, verify, bench)
- `src/omni/compat/` — Claude Code / Codex / MCP interop

## Guidelines

- **Tests**: add/extend tests under `tests/`. Tests must run **offline** — use the `mock`
  provider and the `ScriptedLLM` fixture; never call a network API or a real model in tests.
- **Style**: ruff-clean, type hints, Google-style docstrings on public surfaces. Keep comments
  about intent, not narration.
- **Local-first**: no new heavy infra dependencies (no MySQL/Redis/etc.). Prefer SQLite + files.
- **Compatibility**: keep `SKILL.md` Claude-Code-compatible (only `name`/`description` required;
  OmniScientist extras under `metadata.helixforge`).
- **Conventional commits** are appreciated (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`).

## Adding a skill

See [`../skills/docs/authoring.md`](../skills/docs/authoring.md). Built-in skill *content* lives in
the top-level [`../skills/<name>/`](../skills/) directory (independent of the CLI): each is a
`SKILL.md` plus an optional `engine.py` in the **same** folder. Engines reuse only omni's small
public runtime (e.g. `omni.research`), never CLI-private modules. There is no `skills_builtin/`
package.

## Adding a channel

Implement `omni/channels/<name>.py` subclassing `Channel` (inbound loop + `notify`), register it in
`channels/base.py:build_channels`, and document its `~/.omni/channels/<name>.toml`.

## Reporting issues / PRs

- Open an issue with repro steps (`omni doctor` output helps).
- Keep PRs focused; include tests and a short rationale (the "why").

By contributing you agree your contributions are licensed under Apache-2.0.
Every commit must include a DCO sign-off (`git commit -s`). Repository-wide contribution,
security, and conduct policies live in [`../CONTRIBUTING.md`](../CONTRIBUTING.md),
[`../SECURITY.md`](../SECURITY.md), and [`../CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md).
