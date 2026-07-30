# Renderer setup

`research-pptx` requires Python 3.11+, Node.js 20.9+, and npm. PyMuPDF handles PDF
inputs, matplotlib renders equations, python-pptx reuses templates, and
PptxGenJS plus `sharp` perform the final render.

## OmniScientist

The normal Omni package includes the active Skill's Python dependencies. The
repository installer, `omni init`, and `omni update` also run the lockfile-pinned
Node setup. Modules are stored outside the Python package under
`<OMNI_HOME>/cache/skill-runtimes/research-pptx/<lock-hash>/`; upgrades select a
new hash instead of reusing stale native modules.

To explicitly repair or verify this runtime, run:

```bash
omni skills setup research-pptx
```

Each phase checks only the dependency it actually uses. Outline review does not
require a renderer, and a template-reuse render can succeed without Node. The
PptxGenJS render phase returns a blocking, non-retryable `node_unavailable` or
`runtime_dependency_missing` failure when setup is incomplete. Dependency
failures remain failed CLI tasks with the repair command; they are not presented
as conversational `needs_input`. Task execution never downloads packages or
writes into site-packages.

## Portable runner

Claude Code, Codex, and OpenClaw copies remain self-contained. In the copied
Skill directory, install its local renderer once:

```bash
cd scripts
npm ci
cd ..
```

Portable execution additionally needs an OpenAI-compatible text model:

```bash
export OPENAI_BASE_URL="https://example.invalid/v1"
export OPENAI_API_KEY="..."
export OMNI_MODEL="model-name"  # optional
python3 scripts/run.py --json '{"topic":"My talk","reference_text":"..."}'
```
