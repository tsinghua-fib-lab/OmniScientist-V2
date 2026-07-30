# SoulAgent

`soulagent` is a portable runtime Skill that turns an external scientist KG
into a temporary, task-specific Host persona. It senses the current scientific
task, selects a bounded KG subgraph, decodes only that subgraph, and writes the
result through a reversible stoma protocol.

SoulAgent does not switch among several cooperating identity agents. One user-
selected scientist remains active until the user switches or unloads it; task
changes only alter which parts of that scientist KG are used.

## Package layout

```text
soulagent/
├── SKILL.md
├── README.md
├── LICENSE.txt
├── NOTICE.md
├── agents/
├── core.py
├── task_sensor.py
├── graph_pruner.py
├── kg_decoder.py
├── kg_loader.py
├── stoma_writer.py
├── references/
│   ├── runtime-design.md
│   └── codex测试指导文档.md
└── examples/
    └── scientist-kg/
        ├── fengli-xu/
        └── kaiming-he/
```

The example KGs are read-only test fixtures. Production KGs remain external to
the installed Skill and normally live under `<project-root>/scientist-kg/`.

## Installation

### OmniScientist built-in checkout

When this directory is present under the repository's top-level `skills/` and
listed in `skills/index.toml`, inspect it with:

```powershell
omni skills info soulagent
```

To import a copied checkout into an installed Omni environment:

```powershell
omni skills add ".\skills\soulagent"
omni skills trust soulagent --yes
```

### Codex

```powershell
Copy-Item ".\skills\soulagent" `
  "$HOME\.agents\skills\soulagent" -Recurse
```

`$CODEX_HOME/skills/soulagent` is also supported. Restart Codex after copying.
Use `$soulagent` in a prompt to force the runtime Skill instead of directly
invoking a scientist-specific research lens.

### Claude Code

```powershell
Copy-Item ".\skills\soulagent" `
  "$HOME\.claude\skills\soulagent" -Recurse
```

### OpenClaw

```powershell
Copy-Item ".\skills\soulagent" `
  "$HOME\.openclaw\skills\soulagent" -Recurse
```

### Python and model configuration

Use Python 3.11 or newer. Install the OpenAI client in the Host environment:

```powershell
python -m pip install "openai>=2,<3"
```

Configure the decoder in the same process that starts the Host:

```powershell
$env:SOULAGENT_API_KEY = "<API_KEY>"
$env:SOULAGENT_MODEL = "<MODEL_NAME>"
$env:SOULAGENT_BASE_URL = "<OPTIONAL_OPENAI_COMPATIBLE_URL>"
```

`SOULAGENT_BASE_URL` is optional for OpenAI and required for other compatible
providers. Never place the API key in command arguments, KG data, stoma files,
state files, screenshots, or test logs.

## Prepare a project

A production project supplies one or more validated KGs:

```text
<project-root>/
└── scientist-kg/
    ├── kaiming-he/
    └── fengli-xu/
```

For a local smoke test, copy the bundled examples into an empty test project:

```powershell
New-Item ".\soulagent-smoke" -ItemType Directory -Force
Copy-Item ".\skills\soulagent\examples\scientist-kg" `
  ".\soulagent-smoke\scientist-kg" -Recurse
```

## Quick start

The following commands assume the current directory is the installed
`soulagent` Skill directory.

List valid scientists without calling the decoder:

```powershell
python core.py --project-root "<PROJECT_ROOT>" list
```

Activate a scientist for Codex using a conversation JSON file:

```powershell
python core.py --project-root "<PROJECT_ROOT>" activate `
  --scientist-id "kaiming-he" `
  --host codex `
  --conversation-file "<CONVERSATION_JSON>"
```

Inspect state and unload:

```powershell
python core.py --project-root "<PROJECT_ROOT>" status
python core.py --project-root "<PROJECT_ROOT>" unload
```

The fixed runtime path is:

```text
task_sensor -> graph_pruner -> kg_decoder -> stoma_writer
```

## Testing

### Offline checks

Listing and source compilation do not require an API call:

```powershell
python -m compileall -q .
python core.py `
  --project-root ".\soulagent-smoke" `
  list
```

Expected scientist IDs are `kaiming-he` and `fengli-xu`; `invalid` should be
empty.

Inside an OmniScientist source checkout, run:

```powershell
pytest -q cli/tests/unit/test_builtin_skill_index.py `
  cli/tests/unit/test_academic_persona_skills.py `
  cli/tests/unit/test_portable_skills.py
```

### Live activation sequence

With the decoder environment configured, test these state transitions:

1. scientific request with a selected scientist -> `refreshed`;
2. materially unchanged task -> `unchanged_task`;
3. changed phase, objective, constraints, scientist, or KG manifest -> `refreshed`;
4. ordinary non-scientific request -> `no_scientific_task`;
5. unload -> `unloaded` and original stoma restored.

For copy-ready Codex prompts and expected files, use
[`references/codex测试指导文档.md`](references/codex测试指导文档.md).

## Host adaptation

| Host | `--host` | Stoma written by Core | Host adapter responsibility |
|---|---|---|---|
| OmniScientist | `omniscientist` | `role.md` | Invoke SoulAgent at a stable turn boundary and include the ready stoma in prompt assembly |
| Codex | `codex` | `agent.md` | The `$soulagent` Skill invokes Core, waits for `ready`, reads the stoma, and applies it to the task |
| Claude Code | `claude` | `claude.md` | The Skill or wrapper invokes Core and reads the ready stoma before continuing |
| WorkBuddy | `workbuddy` | `soul.md` | The Host wrapper reads the ready stoma at its persona boundary |
| OpenClaw | Host wrapper chooses a supported value | supported stoma | Adapter maps its persona boundary to one supported Host contract |

Core is Host-neutral except for the explicit stoma map. It does not monitor
process memory, patch a running model request, or assume that writing a file
automatically changes an already assembled system prompt. The Host integration
must invoke SoulAgent before prompt assembly or explicitly read the ready stoma
after activation.

For OmniScientist, installing the Skill makes it discoverable and runnable. A
live dynamic system-prompt effect still depends on an Omni turn-boundary adapter
that invokes the Skill before `build_system_prompt`; a process that cached its
Role earlier will not be changed merely by a later filesystem write. This
boundary is deliberate and documented rather than hidden behind file polling.

## State and rollback protocol

SoulAgent writes only the stoma selected by `--host`. During a successful write:

1. `.soulagent/lock/writing` prevents readers from consuming a partial file;
2. the original stoma is backed up once;
3. the new persona is atomically installed;
4. `.soulagent/lock/ready` marks the readable version;
5. `.soulagent/state.json` records scientist, Host, KG hash, task frame, persona
   hash, and paths.

On decoder or KG validation failure, the existing stoma remains unchanged. On
write or state-commit failure, Core restores the previous complete stoma. On
unload, Core restores the original file and removes SoulAgent-owned state,
backup, and lock files.

## KG contract

SoulAgent validates the selected KG before pruning. Each scientist directory
contains `manifest.json`, `meta.json`, `identity.json`, `l2-patterns.json`,
`l3-stances.json`, `edges.json`, and partitioned `l1-evidence/`. Hash-invalid,
missing, dangling, or identity-mismatched data stops activation.

The runtime never writes back to the KG. It does not invent new graph nodes or
use the decoder to select evidence. Deterministic pruning selects the bounded
subgraph first; the model only verbalizes that selected material.

## Troubleshooting

**Codex calls `kaiming-he` directly:** write `使用 $soulagent` explicitly. A
direct scientist Skill does not exercise SoulAgent's task sensor or KG pruning.

**No scientists are listed:** confirm `<project-root>/scientist-kg/<id>/` exists,
or copy the bundled example directory into the smoke project.

**Missing decoder configuration:** set `SOULAGENT_API_KEY` and
`SOULAGENT_MODEL`; set `SOULAGENT_BASE_URL` for a compatible third-party API.

**`no_scientific_task`:** provide a research context involving experiments,
hypotheses, evidence, data, methods, validation, failure analysis, or scientific
decisions.

**Persona file changed but the answer did not:** verify the Host adapter invoked
SoulAgent before prompt assembly or read the ready stoma afterward. File polling
alone is not part of this Skill.
