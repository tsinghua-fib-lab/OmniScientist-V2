---
name: soulagent
description: Load, switch, refresh, and unload a scientist's task-specific runtime persona from an external scientist-kg knowledge graph. Use when a Coding Agent starts in a project containing scientist-kg, when the user asks to think like a named scientist, when a loaded scientist is working on a scientific task whose phase or constraints changed, or when the user asks to restore the Agent's original behavior.
license: Apache-2.0
allowed-tools: [read_file, bash, write_file]
metadata:
  helixforge:
    version: "1.0"
    dependencies: ["python>=3.11"]
    tier: research
    role: utility
    kind: prompt_only
    delivery_mode: async_task
    execution:
      max_iterations: 6
      max_tool_calls: 20
      max_seconds: 600
    workflow:
      failure_policy: continue_with_partial
      allow_failed_dependencies: true
    input_schema:
      type: object
      properties:
        input: {type: string, description: "activation, refresh, switch, status, list, or unload request"}
        action: {type: string, description: "list, activate, status, or unload"}
        scientist_id: {type: string, description: "scientist KG id selected by the user"}
        host: {type: string, description: "workbuddy, claude, codex, or omniscientist"}
        project_root: {type: string, description: "Host project containing the external scientist-kg directory"}
      required: [input]
    trigger:
      phrases: [SoulAgent, load scientist persona, switch scientist, unload persona, 启用科学家人格, 切换科学家, 卸载人格]
      when_to_use: "Use to select a scientist KG, sense the current scientific task, and manage a reversible task-specific Host persona."
    notification:
      display_label: "SoulAgent persona runtime"
---

# SoulAgent

Treat SoulAgent as the pluggable Skill. Treat `scientist-kg/` as external read-only data and the
decoded persona as temporary stoma content. Never turn a scientist KG or a decoded persona into
another Skill.

## Locate the runtime

Resolve `core.py` relative to this `SKILL.md`. Treat the user's current project as
`<project-root>`. By default, read KGs from `<project-root>/scientist-kg/<scientist_id>/`.

Declare the current Host on every activation. Map the Host to exactly one stoma:

| Host | `--host` | Stoma |
|---|---|---|
| WorkBuddy | `workbuddy` | `soul.md` |
| Claude Code | `claude` | `claude.md` |
| Codex | `codex` | `agent.md` |
| OmniScientist | `omniscientist` | `role.md` |

Do not omit `--host`, even though Core defaults to `claude` for compatibility. Never write or
back up another Host's stoma.

Require the following process environment for real decoding:

```text
SOULAGENT_API_KEY
SOULAGENT_MODEL
SOULAGENT_BASE_URL  # optional for OpenAI; required for non-OpenAI-compatible providers
```

Never print, persist, copy, or place the API key in a command argument, KG, stoma, audit, or
project file.

## Start a session

At session start, or on the first scientific request, scan available scientists:

```powershell
python "<skill-directory>\core.py" --project-root "<project-root>" list
```

If valid scientists exist and none is loaded, ask which scientist to load. Do not silently choose
one. If no valid KG exists, report that SoulAgent cannot load a persona.

## Interpret user intent

- On “用何恺明的方式想”, “装载 Kaiming He”, or equivalent, select that scientist.
- On “换 LeCun” or equivalent, pass the new `scientist_id`; Core performs a full switch.
- On “不用科学家了”, “恢复你自己”, or equivalent, unload and restore the original stomas.
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
task_sensor → graph_pruner → kg_decoder → stoma_writer
```

The decoder may verbalize only the selected subgraph. It must not select new nodes, invent
evidence, expose internal IDs, write back to the KG, or modify this Skill.
The pruned SoulPack keeps P01-P03 under `philosophy_kernel.stances` and copies P04
under `philosophy_kernel.tone_exemplars`. P04 does not participate in graph traversal,
L2 selection, or tension resolution; the decoder uses its verbatim sentences only as
rhythm and diction references.

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

## Handle failures

- Stop on missing or hash-invalid KG data.
- Leave stomas untouched when sensing or LLM decoding fails.
- Roll back to the previous complete stoma if a write fails.
- Surface the exact error; never substitute a generic or fabricated persona.

Read [runtime-design.md](references/runtime-design.md) only when changing the runtime
architecture, graph semantics, or stoma protocol.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Copy-only mode: copy this folder into a Claude Code, Codex, or OpenClaw Skill
  directory. The Host follows `SKILL.md` and calls `core.py` locally.
- Portable runner mode: this prompt-only Skill uses `core.py` as the portable
  command entry point. It reads an external `scientist-kg/` directory and writes
  only the declared Host stoma plus `.soulagent/` transactional state.
- Omni enhanced mode: OmniScientist discovers the Skill from its built-in
  inventory, schedules explicit invocations, and records the returned status;
  `--host omniscientist` targets `role.md`.

See `README.md` for installation, example KG setup, complete test commands, Host
adaptation rules, and rollback behavior.
