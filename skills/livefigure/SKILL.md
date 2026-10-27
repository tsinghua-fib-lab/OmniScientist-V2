---
name: livefigure
description: Use when a scientific, architecture or workflow figure benefits from visual composition or native editing in one PPTX slide. Requires VLM image input and output. Choose by scientific fidelity, readability and editing needs. Original paper figures may be reused, poor drawings rejected. Complete posters use scientific-poster.
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
      effects: [user_files.write]
      max_seconds: 600
    runtime_requirements:
      python_modules: [pptx]
      services: [vlm]
      dependency_setup_command: 'uv pip install "./cli[livefigure]" --python .venv'
      dependency_error_code: runtime_dependency_missing
    capabilities:
      - artifact.figure
      - figure.scientific
      - figure.architecture
      - figure.workflow
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
        - architecture diagram
        - workflow diagram
        - scientific schematic
      when_to_use: "Scientific, architecture or workflow figure components; choose by fidelity, readability and native editing needs. Requires VLM image input and output. Reuse suitable paper originals; reject poor drawings."
      when_not_to_use: "Exact DOT/SVG/PNG rendering (scientific-figure); complete posters (scientific-poster); multi-slide decks (research-pptx). A missing VLM blocks livefigure; explicit native PPTX requirements remain unmet."
    notification:
      display_label: "LiveFigure PPTX"
      title_field: "title"
  openclaw:
    emoji: "📊"
    requires:
      bins: ["python3"]
---

# LiveFigure

One editable single-slide PPTX figure component for scientific schematics,
architectures and workflows that benefit from visual composition or native
PowerPoint shapes, text and connectors. The orchestrator can select it from
these needs; `scientific-figure` specializes in graph topology and DOT/SVG/PNG.

Choose by scientific fidelity, readability at the intended size and editing
needs. The orchestrator may reject an unsuitable drawing or reuse an original
paper figure, including a permitted bitmap, with its citation and editing limits.

An owner-configured MLLM with image input and image output is required
(`omni config vlm`). Pass intent and editing requirements in natural-language
`input`. A successful component can pay `artifact.figure`.

Deliver only the figure component. `scientific-poster` owns complete poster
composition, review and export; `research-pptx` owns complete slide decks.

Generate one editable PPTX figure from a research requirement. The default
workflow creates a whole-figure composition reference. The VLM selects complex
visual assets using the original figure requirement, including its editing scope.
Requests for fully native diagrams use editable shapes, text and connectors.
When raster artwork is permitted and useful, the workflow generates an icon sprite
sheet and splits it into transparent PNG assets. Their paths are available to the
`python-pptx` author as optional scientific illustrations.
It retains syntax/execution repair, but does not render the PPTX to PNG or run
the old post-generation visual critic/actor revision loop.

An owner-controlled MLLM with image input and image output is required. If it
is missing, Omni stops before the engine runs and asks the owner to run
`omni config vlm` — do not silently switch providers. Configure it once:

```toml
# ~/.omni/config.toml
[vlm]
enabled = true
model = "your-vision-model"
# optional: pin Images (gpt-image-2) or a Gemini image model
# image_model = "gpt-image-2"
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

Each build attempt retains its source in the run directory. Executed attempts
retain process output and exit code. Failed syntax, execution and package checks
retain the failing stage and error beside that attempt's source.

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
runner's environment. `OMNI_VLM_ENDPOINT` may be a site origin or the
complete chat-completions URL. Image *output* routes by model: a Gemini
image chat or pin (`gemini` + `image` in the name) uses
`/v1beta/models/{model}:generateContent`; GPT Image / DALL·E use the OpenAI
Images sibling `/v1/images/generations`. A Gemini miss falls through to
Images. Optional `OMNI_VLM_IMAGE_MODEL` pins `gpt-image-2` (Images only) or
another Gemini image model. A chat-only `omni config vlm --test` / image-input
probe does not prove this generation path. The runner has a network-free
`--self-test` mode.
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
