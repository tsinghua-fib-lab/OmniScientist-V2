---
name: soulagent
description: Load, securely download, switch, refresh, and unload a scientist's task-specific runtime persona from an external scientist-kg knowledge graph. Use when a Coding Agent starts in a project containing scientist-kg, when the user explicitly asks to think like a named scientist, when a loaded scientist is working on a scientific task whose phase or constraints changed, or when the user asks to restore the Agent's original behavior.
license: Apache-2.0
metadata:
  helixforge:
    version: "1.2"
    dependencies: ["python>=3.11"]
    tier: research
    role: utility
    priority: 95
    capabilities:
      - persona.scientist
      - persona.scientist.list
      - persona.scientist.load
      - persona.scientist.refresh
      - persona.scientist.switch
      - persona.scientist.status
      - persona.scientist.unload
    kind: python_engine
    delivery_mode: async_task
    engine:
      module: engine
      class: SoulAgentEngine
      method: execute
    execution:
      max_iterations: 10
      max_tool_calls: 20
      max_seconds: 600
    workflow:
      failure_policy: continue_with_partial
      allow_failed_dependencies: true
    input_schema:
      type: object
      properties:
        input: {type: string, description: "activation, refresh, switch, status, list, or unload request"}
        action: {type: string, enum: [list, activate, refresh, switch, status, unload], description: "SoulAgent lifecycle action; inferred from input when omitted"}
        scientist_id: {type: string, description: "scientist KG id selected by the user"}
        project_root: {type: string, description: "Host project containing the external scientist-kg directory"}
        kg_root: {type: string, description: "optional exact local KG scanner and download directory"}
        force: {type: boolean, description: "Force persona regeneration even when the task frame is unchanged"}
      required: [input]
    output_schema:
      type: object
      properties:
        status: {type: string, description: "Lifecycle status such as ok, listed, inactive, needs_input, error, or unloaded"}
        loaded: {type: boolean, description: "true only when a committed scientist persona is ready for the active Host"}
        missing_kg: {type: boolean, description: "scientist KG root or selected scientist directory does not exist"}
        invalid_kg: {type: boolean, description: "selected scientist KG exists but fails validation"}
        needs_input: {type: boolean, description: "the caller must obtain user input or invoke the dedicated KG distiller before retrying"}
        active_scientist_id:
          type: [string, "null"]
          description: "currently committed scientist persona id; null when no persona is loaded"
        outcome: {type: object, description: "Lifecycle result code"}
        active: {type: boolean}
        scientist_id: {type: string}
        scientist_name: {type: string}
        scientists: {type: array}
        invalid: {type: array}
        project_root: {type: string}
        role_path: {type: string}
        persona_text: {type: string, description: "Decoded persona for same-turn host synthesis; also committed transactionally to role.md"}
        summary: {type: string}
        text: {type: string}
        error: {type: string}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        requested_scientist: {type: string}
        remote_checked: {type: boolean}
        registry_url: {type: string}
        remote_install: {type: object, description: "verified remote installation metadata when a KG was downloaded"}
        offer_distillation: {type: boolean}
        distiller_skill: {type: string}
        host_must_not_fabricate: {type: boolean}
        action_required: {type: object, description: "terminal user-confirmation boundary for distillation"}
        recovery_choices: {type: array}
      required: [status, loaded, missing_kg, invalid_kg, needs_input, active_scientist_id]
    trigger:
      phrases: [SoulAgent, load scientist persona, switch scientist, unload persona, 启用科学家人格, 切换科学家, 卸载人格, 用徐丰力的方式, 用何恺明的方式, 装载何恺明, 装载徐丰力, 换一个人格, 恢复你自己, 不用科学家了]
      when_to_use: "Use to select a scientist KG, sense the current scientific task, and manage a reversible task-specific Host persona."
    notification:
      display_label: "SoulAgent persona runtime"
---

# SoulAgent

Treat SoulAgent as the pluggable Skill. Treat `scientist-kg/` as external read-only data and the
decoded persona as temporary stoma content. Never turn a scientist KG or a decoded persona into
another Skill.

## Locate or download scientist KGs

Use one exact directory for both scanning and verified downloads:

1. Use an explicit `kg_root` or `--kg-root` when supplied.
2. Otherwise, preserve compatibility by using `<project-root>/scientist-kg/` when it exists.
3. Otherwise, use `~/.omni/scientist-kg/`.

The directory may contain project KGs, previously verified downloads, and KGs created by
`scientist-kg-distiller`. Never download to a different cache and then pretend it is scanned.
An OmniScientist installation also seeds missing product-bundled KGs into
`<OMNI_HOME>/scientist-kg/` during first-launch and upgrade convergence. Treat the copy under
`assets/builtin-scientist-kg/` as read-only package data; never activate it in place or overwrite
an existing writable scanner directory from it.

Only query the public registry when the user explicitly names a scientist and no local ID,
canonical name, or alias matches. Listing scientists, generic requests such as “换一个人格”,
and ordinary scientific tasks must remain local-only. The default trusted registry is:

```text
https://gitee.com/cvYaowenHu/scientist-kg-registry/raw/master/registry.json
```

Install a remote match into the exact scanner directory only after verifying the registry-level
`manifest.json` SHA-256, every manifest-declared file SHA-256, KG structure, scientist ID, path
safety, file-count limits, and size limits. Download into a temporary sibling directory, hold an
installation lock, and expose the complete KG with an atomic rename. Never overwrite an existing
local directory, including an invalid one.

Declare the current Host on every activation. Map the Host to exactly one stoma:

| Host | `--host` | Stoma |
|---|---|---|
| WorkBuddy | `workbuddy` | `soul.md` |
| Claude Code | `claude` | `claude.md` |
| Codex | `codex` | `agent.md` |
| OmniScientist | `omniscientist` | `role.md` |

Do not omit `--host`, even though Core defaults to `claude` for compatibility. Never write or
back up another Host's stoma.

When Core is run directly outside OmniScientist, require the following process environment for
real decoding:

```text
SOULAGENT_API_KEY
SOULAGENT_MODEL
SOULAGENT_BASE_URL  # optional for OpenAI; required for non-OpenAI-compatible providers
```

Never print, persist, copy, or place the API key in a command argument, KG, stoma, audit, or
project file.

In OmniScientist mode, `engine.py` uses the Host's injected model service. It does not read the
owner's config or secrets files, and it does not require the `openai` Python package.

## Start a session

At session start, or on the first scientific request, scan available scientists:

```powershell
python "<skill-directory>\core.py" --project-root "<project-root>" list
```

If valid scientists exist and none is loaded, ask which scientist to load. Do not silently choose
one. If none exists, ask the user to name the intended scientist; do not query the remote registry
until a concrete name is supplied.

SoulAgent exposes the queryable lifecycle capabilities `persona.scientist.list`,
`persona.scientist.load`, `persona.scientist.refresh`, `persona.scientist.switch`,
`persona.scientist.status`, and `persona.scientist.unload`. Keep the aggregate
`persona.scientist` capability only as a compatibility alias; planners should request the
specific lifecycle capability whenever the intended operation is known.

## Enforce the activation output contract

Every activation pipeline result has all five fields below, including early stops:

| Field | Contract |
|---|---|
| `loaded` | `true` only when the selected Host has a committed, ready persona stoma |
| `missing_kg` | the KG root or selected scientist directory does not exist |
| `invalid_kg` | the selected KG exists but fails structural or hash validation |
| `needs_input` | user input or a dedicated distillation step is required before retrying |
| `active_scientist_id` | the currently committed scientist ID, otherwise `null` |

Treat `loaded` as authoritative. If `loaded=false`, the status must never be `ok`,
`succeeded`, or `refreshed`, and the Host must not claim that a scientist persona is active.
Do not infer a successful load from a stoma file alone, an earlier conversation, or a generic
success message.

KG preflight and named-scientist resolution always run before task sensing. A local miss first
checks the trusted registry. A confirmed local-and-remote miss returns `status=needs_input` with
`action_required.kind=configure`; do not call the task sensor, decoder, or any downstream research
operation after that result.

## Require confirmation before distillation

Treat every file below `scientist-kg/` as read-only SoulAgent input. If a KG is missing or
invalid, return a terminal confirmation request before doing anything else:

- set `offer_distillation=true`, `distiller_skill=scientist-kg-distiller`, and
  `host_must_not_fabricate=true`;
- set `action_required.kind=configure` and
  `action_required.action=confirm_scientist_distillation`;
- preserve the exact scanner root in `action_required.distiller_input.install_root`
  so the distiller publishes to the directory SoulAgent actually scans;
- ask whether to invoke `scientist-kg-distiller` for the named scientist;
- stop the current Host turn until the user answers.

Only after explicit user approval may the Host invoke `scientist-kg-distiller`. A general Agent
must never use Write/Edit, shell redirection, or ad-hoc scripts to create or repair
`identity.json`, `meta.json`, `manifest.json`,
`l2-patterns.json`, `l3-stances.json`, `edges.json`, or `l1-evidence/*.jsonl`.

Stop after returning the missing/invalid contract result unless the user approves and the
dedicated distiller is actually selected. Never answer in the requested scientist's voice,
construct a substitute prompt, keep retrying SoulAgent, or fabricate a partial KG.

## Gate scientist-perspective paper review

Before entering `paper-review` for a request that claims an active scientist perspective, query
`persona.scientist.status` or consume the immediately preceding SoulAgent activation result.
Proceed with the scientist-perspective claim only when `loaded=true` and
`active_scientist_id` matches the requested scientist. If `loaded=false`, block that claim and
route first to `persona.scientist.load`; a generic paper review may proceed only when it is
clearly labelled as not using a loaded scientist persona. `status=ok` by itself is never proof
that a scientist persona is loaded.

## Interpret user intent

- On "用何恺明的方式想", "装载 Kaiming He", or equivalent, select that scientist.
- On "换 LeCun" or equivalent, pass the new `scientist_id`; Core performs a full switch.
- On "不用科学家了", "恢复你自己", or equivalent, unload and restore the original stomas.
- Resolve names against the `list` output. Ask when aliases are ambiguous.

## Refresh a loaded persona

Call Core when a scientist is loaded and:

- a scientific task begins;
- the task phase, objective, or resource constraints changed materially;
- the scientist changed;
- the selected KG manifest changed.

Do not refresh for a continuous turn in the same task, ordinary code editing, or non-scientific
conversation. Core also enforces these rules and returns `unchanged_task` or
`no_scientific_task`.

Pass recent user messages plus concise Agent reply summaries:

```powershell
python "<skill-directory>\core.py" `
  --project-root "<project-root>" `
  activate `
  --scientist-id "<scientist-id>" `
  --host "<workbuddy|claude|codex|omniscientist>" `
  --conversation-file "<temporary-json-file>"
```

The pipeline is fixed:

```text
kg_preflight → task_sensor → graph_pruner → kg_decoder → stoma_writer
```

Under the Omni host adapter, keep the small JSON task sensor and the persona
decoder on separate bounded model calls: 512 output tokens / 30 seconds for
task sensing, and 3072 output tokens / 60 seconds for persona decoding. Cancel
the in-flight host future on timeout and never accept an output-cap-truncated
persona.

The decoder may verbalize only the selected subgraph. It must not select new nodes, invent
evidence, expose internal IDs, write back to the KG, or modify this Skill.
The pruned SoulPack keeps P01-P03 under `philosophy_kernel.stances` and copies P04
under `philosophy_kernel.tone_exemplars`. P04 does not participate in graph traversal,
L2 selection, or tension resolution. Like P01-P03, P04 is canonical text: Core injects
all 3-5 exemplars verbatim into `### 表达语气` after the LLM returns. The LLM must not
receive, summarize, rewrite, or regenerate the P04 sentences.

## Respect the stoma protocol

Core writes the decoded persona only to the stoma mapped from the declared Host. It backs up and
restores only that same file. The active Host is recorded in `.soulagent/state.json`, so unload
can restore the correct stoma.

Before reading a stoma:

1. Check `<project-root>/.soulagent/lock/writing`.
2. If it exists, pause and retry; never read a partial update.
3. Read only after `<project-root>/.soulagent/lock/ready` exists.

Do not treat an arbitrary pre-existing stoma as a loaded persona. A valid load also has
`.soulagent/state.json` and SoulAgent backup metadata.

## Unload

Run:

```powershell
python "<skill-directory>\core.py" --project-root "<project-root>" unload
```

Confirm that Core restored the original stomas and removed SoulAgent backups and locks. Never
delete or overwrite unrelated project instructions.

**After unload succeeds:** The unload response contains an `instruct_host` key, e.g. "科学家人格 Fengli Xu 已卸载。此时无任何科学家人格处于加载状态。" Write this sentence as a memory entry — it serves as a timestamped record of the unload event, not a permanent ban on re-loading.

## Handle failures

- Distinguish an authoritative registry miss from a network or registry failure. Offer
  distillation only after the registry was checked successfully; return
  `outcome.code=remote_lookup_failed` for connectivity or registry-contract errors.
- Stop on missing or hash-invalid KG data before task sensing. Return the five-field activation
  contract and invoke only `scientist-kg-distiller` after explicit user authorization.
- Leave stomas untouched when sensing or LLM decoding fails.
- Roll back to the previous complete stoma if a write fails.
- Surface the exact error; never substitute a generic or fabricated persona.

Read [runtime-design.md](references/runtime-design.md) only when changing the runtime
architecture, graph semantics, or stoma protocol.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Copy-only mode: copy this folder into a Claude Code, Codex, or OpenClaw Skill
  directory. The Host follows `SKILL.md` and calls `core.py` locally.
- Portable runner mode: from this Skill directory, run
  `python3 scripts/run.py --json '{...}'`. The JSON action can be `list`,
  `activate`, `refresh`, `switch`, `status`, or `unload`. The runner delegates
  to `core.py`, reads an external `scientist-kg/` directory, and writes only
  the declared Host stoma plus `.soulagent/` transactional state. Activation
  requires an explicit `host` and `conversation` (or `input`).
- Omni enhanced mode: OmniScientist discovers the Skill from its built-in
  inventory, binds the `persona.scientist` capability to `engine.py`, injects
  its configured model service, and records the returned status; the engine
  targets `role.md` without exposing model credentials to SoulAgent.

See `README.md` for installation, bundled KG setup, complete test commands, Host
adaptation rules, and rollback behavior.
