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
├── remote_registry.py
├── scripts/
│   └── run.py
├── task_sensor.py
├── graph_pruner.py
├── kg_decoder.py
├── kg_loader.py
├── stoma_writer.py
├── references/
│   ├── runtime-design.md
│   └── codex测试指导文档.md
└── assets/
    └── builtin-scientist-kg/
        ├── alan-turing/
        ├── claude-shannon/
        ├── fengli-xu/
        ├── herbert-a-simon/
        ├── john-von-neumann/
        ├── kaiming-he/
        ├── norbert-wiener/
        └── richard-feynman/
```

The packaged KG snapshot is read-only Skill data. On the first OmniScientist
launch after installation or upgrade, missing bundled KGs are validated and
atomically installed into `<OMNI_HOME>/scientist-kg/`; an existing scientist
directory is never overwritten, even when locally modified or invalid.
SoulAgent uses `<project-root>/scientist-kg/` when that compatibility directory
exists, otherwise `<OMNI_HOME>/scientist-kg/`; an explicit `kg_root` overrides
both. The selected location is both the scanner root and the destination for
verified remote downloads.

## Installation

### OmniScientist built-in checkout

When this directory is present under the repository's top-level `skills/` and
listed in `skills/index.toml`, inspect it with:

```powershell
omni skills info soulagent
```

An installed OmniScientist deploys missing built-in personas automatically on
the first launch. The setup can also be repaired explicitly:

```powershell
omni skills setup builtin-personas
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

Use Python 3.11 or newer. No third-party Python package is required. When
running `core.py` directly outside OmniScientist, configure the decoder in the
same process that starts the Host:

```powershell
$env:SOULAGENT_API_KEY = "<API_KEY>"
$env:SOULAGENT_MODEL = "<MODEL_NAME>"
$env:SOULAGENT_BASE_URL = "<OPTIONAL_OPENAI_COMPATIBLE_URL>"
```

`SOULAGENT_BASE_URL` is optional for OpenAI and required for other compatible
providers. Never place the API key in command arguments, KG data, stoma files,
state files, screenshots, or test logs.

OmniScientist mode needs none of these environment variables: `engine.py` uses
the Host's configured model service without exposing its credentials to the
Skill.

## Prepare a project

A production project supplies one or more validated KGs:

```text
<project-root>/
└── scientist-kg/
    ├── kaiming-he/
    └── fengli-xu/
```

For a local smoke test outside OmniScientist, copy the bundled snapshot into an
empty test project:

```powershell
New-Item ".\soulagent-smoke" -ItemType Directory -Force
Copy-Item ".\skills\soulagent\assets\builtin-scientist-kg" `
  ".\soulagent-smoke\scientist-kg" -Recurse
```

## Quick start

The following commands assume the current directory is the installed
`soulagent` Skill directory.

The copy-portable JSON runner is the recommended entry point for external
agents. It accepts JSON either through `--json` or standard input:

```powershell
python3 scripts/run.py --self-test
python3 scripts/run.py --json '{"action":"list","project_root":"<PROJECT_ROOT>"}'
python3 scripts/run.py --json '{"action":"activate","project_root":"<PROJECT_ROOT>","scientist_id":"kaiming-he","host":"codex","conversation":"Design a baseline and ablation experiment."}'
```

Actions `refresh` and `switch` use the activation path. `status` and `unload`
need only `project_root`. Activation always requires an explicit Host so the
runner cannot write the wrong stoma.

## Named-scientist fallback

When the user explicitly names a scientist, SoulAgent resolves the request in
this order:

```text
local scanner root -> public Gitee registry -> distillation confirmation
```

The public registry is
`https://gitee.com/cvYaowenHu/scientist-kg-registry/raw/master/registry.json`.
Generic requests and list operations never access it. A remote package is
downloaded into a temporary sibling directory, checked against the registry
manifest hash and every KG file hash, structurally loaded, then atomically
renamed into the scanner root. Existing local directories are never replaced.

If the trusted registry has no matching name or alias, SoulAgent returns a
terminal `needs_input` result asking whether to invoke
`scientist-kg-distiller`. `host_must_not_fabricate=true` means the Host must
stop: it may neither synthesize a substitute persona nor speak as that
scientist. The response includes `action_required.distiller_input.install_root`,
which is the exact scanner root the approved distiller must use for its atomic
installation. A network or malformed-registry failure is reported separately as
`remote_lookup_failed`; it is not treated as proof that the scientist is absent.

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
python3 scripts/run.py --self-test
python core.py `
  --project-root ".\soulagent-smoke" `
  list
```

Expected scientist IDs are `alan-turing`, `claude-shannon`, `fengli-xu`,
`herbert-a-simon`, `john-von-neumann`, `kaiming-he`, `norbert-wiener`, and
`richard-feynman`; `invalid` should be empty.

Inside an OmniScientist source checkout, run:

```powershell
python skills/soulagent/tests/test_runtime_full.py -v
python skills/soulagent/tests/test_multiturn_qa.py -v
python skills/soulagent/tests/test_engine.py -v
python skills/soulagent/tests/test_remote_registry.py -v
pytest -q cli/tests/agent/test_react_agent.py `
  -k soulagent_distillation_confirmation
pytest -q cli/tests/unit/test_builtin_skill_index.py `
  cli/tests/unit/test_academic_persona_skills.py `
  cli/tests/unit/test_portable_skills.py `
  cli/tests/agent/test_persona_stoma_overlay.py `
  cli/tests/agent/test_react_agent.py
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
For the stateful end-to-end conversation used by automated regression tests,
use [`references/multiturn-qa.md`](references/multiturn-qa.md).

## Host adaptation

| Host | `--host` | Stoma written by Core | Host adapter responsibility |
|---|---|---|---|
| OmniScientist | `omniscientist` | `role.md` | Bind `persona.scientist` to `engine.py`; use the injected Host model and return the decoded persona for same-turn synthesis |
| Codex | `codex` | `agent.md` | The `$soulagent` Skill invokes Core, waits for `ready`, reads the stoma, and applies it to the task |
| Claude Code | `claude` | `claude.md` | The Skill or wrapper invokes Core and reads the ready stoma before continuing |
| WorkBuddy | `workbuddy` | `soul.md` | The Host wrapper reads the ready stoma at its persona boundary |
| OpenClaw | Host wrapper chooses a supported value | supported stoma | Adapter maps its persona boundary to one supported Host contract |

Core is Host-neutral except for the explicit stoma map. It does not monitor
process memory, patch a running model request, or assume that writing a file
automatically changes an already assembled system prompt. The Host integration
must invoke SoulAgent before prompt assembly or explicitly read the ready stoma
after activation.

For OmniScientist, the executable Skill contract makes activation discoverable
and schedulable. The engine writes `role.md` for later turns and also returns
`persona_text` so the current turn's final synthesis can apply the decoded
persona even when its system prompt was assembled before Skill execution.

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

**No scientists are listed:** listing is deliberately local-only. Confirm the
selected scanner root, run `omni skills setup builtin-personas`, explicitly
name the scientist to permit a registry lookup, or copy the bundled asset
directory into the smoke project.

**Named scientist is absent remotely:** accept or decline the returned
distillation question. Do not let the Host invent a persona while waiting.

**Remote lookup failed:** check HTTPS connectivity and the registry contract.
Do not claim that the scientist is absent and do not start distillation based
only on a network failure.

**Missing decoder configuration:** this applies only to direct `core.py` use.
Set `SOULAGENT_API_KEY` and `SOULAGENT_MODEL`; set `SOULAGENT_BASE_URL` for a
compatible third-party API. In OmniScientist, verify that the Host model itself
is configured and available.

**`no_scientific_task`:** provide a research context involving experiments,
hypotheses, evidence, data, methods, validation, failure analysis, or scientific
decisions.

**Persona file changed but the answer did not:** verify the Host consumed the
engine's returned `persona_text` for the current turn or read the ready stoma on
the next turn. File polling
alone is not part of this Skill.
