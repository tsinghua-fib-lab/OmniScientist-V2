---
name: livefigure
description: One editable single-slide PPTX figure. Use when the user names livefigure or asks for an editable PowerPoint figure. Requires owner VLM (`omni config vlm`). Ordinary architecture / system diagrams are scientific-figure (SVG/PNG). Do not use for complete multi-slide decks (research-pptx).
license: Apache-2.0
metadata:
  helixforge:
    version: "1.1"
    dependencies: ["python>=3.11", "httpx>=0.27", "python-pptx>=1.0.2"]
    allowed_tools: [write_file, bash, read_file, log_run]
    tier: research
    role: task
    research_contract: portable_provenance_v1
    status: stable
    priority: 60
    delivery_mode: async_task
    kind: python_engine
    execution:
      max_seconds: 600
    runtime_requirements:
      python_modules: [pptx]
      services: [vlm]
      dependency_setup_command: 'uv pip install "./cli[livefigure]" --python .venv'
      dependency_error_code: runtime_dependency_missing
    capabilities:
      - artifact.figure
      - figure.architecture
      - figure.editable.pptx
      - artifact.pptx
      - figure.livefigure
      - figure.editable
    default_for:
      - editable PPTX figure
      - one-slide editable figure
      - LiveFigure
    deliverables:
      - artifact.figure
      - artifact.pptx
    engine:
      module: engine
      class: LiveFigureEngine
      method: execute
    input_schema:
      type: object
      properties:
        input:
          type: string
          description: figure requirement in natural language
          x-omni:
            semantic_role: instruction
        title:
          type: string
          description: optional title for the delivered figure
        reference_image_uri:
          type: string
          description: optional base64 image data URL; Omni also accepts an artifact-workspace image or a file explicitly attached to the request
      required: [input]
    workflow:
      failure_policy: continue_with_partial
      allow_failed_dependencies: true
      failure_types: ["missing_input", "configuration_error", "network_error", "generation_failed", "artifact_write_failed"]
    output_schema:
      type: object
      properties:
        status: {type: string, enum: [ok, partial, error]}
        outcome: {type: object}
        title: {type: string}
        summary: {type: string}
        warning: {type: string}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        pptx_uri: {type: string}
        source_code_uri: {type: string}
        reference_image_uri: {type: string}
        artifacts: {type: array}
        run_id: {type: string}
        research: {type: object}
        error: {type: string}
        error_info: {type: object}
      required: [status]
    trigger:
      phrases:
        - LiveFigure
        - single editable PPTX
        - editable PPTX scientific figure
        - one-slide editable scientific figure
      when_to_use: "Use for a user-named livefigure or an editable single-slide PPTX. Requires configured VLM (`omni config vlm`). Ordinary figures and architecture diagrams are scientific-figure."
      when_not_to_use: "Ordinary figure, architecture, flowchart, or schematic (scientific-figure). Multi-slide decks (research-pptx). If this skill is named and VLM is missing, stop — do not switch to Graphviz."
    notification:
      display_label: "LiveFigure PPTX"
      title_field: "title"
  openclaw:
    emoji: "📊"
    requires:
      bins: ["python3"]
---

# LiveFigure

Editable single-slide PPTX figure. Use this skill when the user names
`livefigure` or asks for an editable PowerPoint / one-slide PPTX figure.

The deliverable is one editable single-slide PPTX. A configured owner VLM is
required (`omni config vlm`). A successful PPTX can pay `artifact.figure`, but
this is not the default drawing skill — ordinary architecture and system
diagrams are `scientific-figure` (SVG/PNG). Pass natural-language input only.

When not to use: a complete multi-slide deck (`research-pptx`), or an ordinary
figure / Graphviz / SVG-only / PNG-only request (`scientific-figure`). If the
user named this skill and VLM is missing, stop and configure VLM; do not
silently switch to Graphviz.

Generate one editable PPTX figure from a research requirement. The default
one-pass workflow asks an OpenAI-compatible multimodal model for constrained
`python-pptx` source. It never generates a reference image implicitly, renders
the PPTX to PNG, or performs a visual critic/actor revision loop.

A configured owner-controlled VLM is required. If it is missing, Omni stops
before the engine runs and asks the owner to run `omni config vlm` — do not
silently switch providers. Configure it once:

```toml
# ~/.omni/config.toml
[vlm]
enabled = true
model = "your-vision-model"
endpoint = "https://provider.example/v1/chat/completions"
protocol = "openai_compatible_chat"

# ~/.omni/secrets.toml
[vlm]
api_key = "..."
```

Run `omni config vlm` for guided setup. The API key is written to
`<OMNI_HOME>/secrets.toml` with mode `0600` on POSIX. `endpoint` is a complete
OpenAI-compatible chat-completions URL and uses bearer authentication. Remote
endpoints must use HTTPS; plain HTTP is allowed only for loopback. Supply
`reference_image_uri` as a base64 image data URL when a visual reference is
useful. Under Omni, a local path is accepted only when it is inside the active
artifact workspace or names a file explicitly attached to the request. The
portable runner accepts local files only under its working directory or output
directory. LiveFigure never downloads a remote reference.

The result contains the editable PPTX, the generated Python source, the input
record, and (when requested) the reference image. Do not claim PNG preview or
post-generation visual evaluation.

## Recovery

- There is no `livefigure/sandbox_runner.py`. Do not invent skill-source paths
  after a failure.
- Do not bash-write a leftover PPTX under `$OMNI_OUTPUT_DIR` as a substitute
  for this skill. Ask for `omni config vlm` when this skill was named.
- Under Omni the generated-code cwd is host scratch (`$TMPDIR`), not
  `~/.omni/.../artifacts`. The user-visible file is the path `put_file`
  returns under `outputs/<title>_<task8>/`.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Copy-only mode: copy this skill folder into a Claude Code, Codex, or OpenClaw
  skill directory and follow this `SKILL.md` with the host's normal tools.
- Portable runner mode: run the command below with owner-controlled VLM
  credentials. The runner writes artifacts locally and does not import Omni.
- Omni enhanced mode: Omni reads `metadata.helixforge`, stores artifacts, and
  supplies the owner-controlled VLM through an injected host service.

The injected port is the LiveFigure adapter's API contract, not a hard sandbox:
trusted in-process Python Skills receive Omni's `ExecContext` and could inspect
host settings. Omni MCP does not serialize VLM credentials to an external
agent; only trust executable Skill code you have reviewed.

From this skill folder, run:

```bash
python3 -m pip install 'httpx>=0.27' 'python-pptx>=1.0.2'
python3 scripts/run.py --json '{"input":"Draw an editable RAG architecture PPTX","output_dir":"livefigure-out"}'
```

Set `OMNI_VLM_MODEL`, `OMNI_VLM_ENDPOINT`, and `OMNI_VLM_API_KEY` in the
runner's environment. `OMNI_VLM_ENDPOINT` must be the complete
chat-completions endpoint. The runner has a network-free `--self-test` mode.
Prefer invoking LiveFigure through Omni MCP from Claude Code, Codex, or
OpenClaw so the external agent does not need direct access to the VLM key.
Configure Omni with `omni config vlm`, then register the stdio server with
`omni mcp install codex`, `omni mcp install claude`, or the OpenClaw host's MCP
configuration using command `omni` and arguments `["mcp", "serve"]`.

## Portable research provenance

This skill must remain portable across OmniScientist, Claude Code, Codex, and
OpenClaw.

- In OmniScientist, artifacts are stored through the managed artifact service;
  include the real run id and artifact URIs in `research` when available.
- In other runtimes, never invent provenance ids. Keep the input record,
  generated Python source, editable PPTX, and optional reference image together
  and return their local paths. The portable runner does not claim durable Omni
  provenance records or run ids.
