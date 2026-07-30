# Scientist KG Distiller

`scientist-kg-distiller` turns a named scientist or a supplied academic corpus
into a traceable research-judgment knowledge graph. It preserves identity
evidence, source text, extraction decisions, cognitive patterns, higher-level
stances, graph edges, checkpoints, and validation records.

The Skill is the production distillation stage. Its canonical output can be
used directly by `soulagent`, which selects a task-relevant subgraph and builds
a temporary Host persona.

## Package layout

```text
scientist-kg-distiller/
├── SKILL.md
├── README.md
├── LICENSE.txt
├── NOTICE.md
├── agents/
├── references/
│   ├── 设计稿02-学术人格KG设计.md
│   └── 设计稿03-蒸馏器与KG编码器.md
├── schemas/
│   ├── evidence-card.schema.json
│   ├── kg.schema.json
│   └── source-object.schema.json
└── scripts/
    ├── requirements.txt
    └── kg_distiller/
        └── main.py
```

## Installation

### OmniScientist built-in checkout

When this directory is present under the repository's top-level `skills/` and
listed in `skills/index.toml`, no extra Skill import is required:

```powershell
omni skills info scientist-kg-distiller
```

To import a copied checkout into an installed Omni environment:

```powershell
omni skills add ".\skills\scientist-kg-distiller"
omni skills trust scientist-kg-distiller --yes
```

### Codex

Copy the complete directory into the current shared Codex Skill root:

```powershell
Copy-Item ".\skills\scientist-kg-distiller" `
  "$HOME\.agents\skills\scientist-kg-distiller" -Recurse
```

`$CODEX_HOME/skills/scientist-kg-distiller` is also supported. Restart Codex
after installation, then invoke `$scientist-kg-distiller` explicitly.

### Claude Code

```powershell
Copy-Item ".\skills\scientist-kg-distiller" `
  "$HOME\.claude\skills\scientist-kg-distiller" -Recurse
```

### OpenClaw

```powershell
Copy-Item ".\skills\scientist-kg-distiller" `
  "$HOME\.openclaw\skills\scientist-kg-distiller" -Recurse
```

### Python dependencies

Use Python 3.11 or newer and install the portable pipeline dependencies:

```powershell
python -m pip install -r scripts/requirements.txt
```

Configure an OpenAI-compatible model in the same process that starts the Host:

```powershell
$env:OPENAI_API_KEY = "<API_KEY>"
$env:OPENAI_BASE_URL = "<OPTIONAL_OPENAI_COMPATIBLE_URL>"
$env:KG_DISTILLER_MODEL = "<MODEL_NAME>"
```

`OPENAI_BASE_URL` is optional for the OpenAI service. Never store API keys in
the Skill directory, KG, checkpoints, screenshots, or test logs.

## Quick start

From the Skill directory, start a complete run from a scientist name:

```powershell
python scripts/kg_distiller/main.py distill `
  --scientist "Kaiming He" `
  --project-root ".\workspace"
```

Use identity hints when a name is ambiguous:

```powershell
python scripts/kg_distiller/main.py distill `
  --scientist "<NAME>" `
  --field "computer vision" `
  --institution "<INSTITUTION>" `
  --project-root ".\workspace"
```

Resume validated checkpoints or execute one stage:

```powershell
python scripts/kg_distiller/main.py distill `
  --scientist-id "kaiming-he" `
  --project-root ".\workspace" `
  --resume

python scripts/kg_distiller/main.py distill `
  --scientist-id "kaiming-he" `
  --project-root ".\workspace" `
  --step kg
```

The only mandatory human gate is identity confirmation when multiple candidates
remain plausible. Evidence extraction, pattern induction, stance abstraction,
tone extraction, edge completion, KG assembly, and capsule generation are
automatic after identity is fixed.

## Output and SoulAgent handoff

The complete result is written under:

```text
<project-root>/result/<scientist_id>/
├── manifest.json
├── kg.json
├── kg/
├── README.md
└── capsule/
```

`kg/` is the canonical progressively readable store. To expose it to
SoulAgent, pass the exact active scanner root to the distiller:

```powershell
python scripts/kg_distiller/main.py distill `
  --scientist-id "kaiming-he" `
  --project-root ".\workspace" `
  --step kg `
  --install-root "$HOME\.omni\scientist-kg"
```

The command validates the delivery KG, copies it into an invisible same-volume
staging directory, validates the staged copy, and atomically exposes it as
`<install-root>/kaiming-he/`. It holds an installation lock and refuses to
overwrite any existing local directory. Use a project-local
`<project-root>/scientist-kg` as `--install-root` only when that compatibility
directory is the scanner selected by SoulAgent.

Do not use the human-facing capsule as SoulAgent input. Do not copy temporary
checkpoints into `scientist-kg/`.

## Testing

### Offline package checks

These checks do not call a model or the network:

```powershell
python scripts/kg_distiller/main.py --help
python -m compileall -q scripts/kg_distiller
```

Inside an OmniScientist source checkout, run the built-in distribution tests:

```powershell
pytest -q cli/tests/unit/test_builtin_skill_index.py `
  cli/tests/unit/test_academic_persona_skills.py `
  cli/tests/unit/test_portable_skills.py
```

### Real distillation check

Run a bounded source set first:

```powershell
python scripts/kg_distiller/main.py distill `
  --scientist "Kaiming He" `
  --project-root ".\smoke-workspace" `
  --max-sources 5
```

Acceptance conditions:

- identity is verified or an explicit candidate-selection file is produced;
- every accepted evidence excerpt remains traceable to a SourceObject;
- KG validation reports no dangling IDs, duplicate IDs, or invalid partitions;
- canonical `kg/` and compatibility `kg.json` represent the same graph;
- no API key is written to the workspace.

## Host adaptation

| Host | Installation root | Invocation | Runtime behavior |
|---|---|---|---|
| OmniScientist | repository `skills/` or `~/.omni/skills/` | `$scientist-kg-distiller` or Skill discovery | Prompt-only task with bounded Omni tools and persisted artifacts |
| Codex | `~/.agents/skills/` or `$CODEX_HOME/skills/` | `$scientist-kg-distiller` | Codex follows `SKILL.md` and runs the local pipeline |
| Claude Code | `~/.claude/skills/` | `/scientist-kg-distiller` or natural-language trigger | Claude Code follows the same Skill and scripts |
| OpenClaw | `~/.openclaw/skills/` | Skill name or natural-language trigger | OpenClaw follows the same portable workflow |

All Hosts use the same schemas and Python implementation. Omni-specific policy
lives only under `metadata.helixforge`; external Hosts safely ignore it.

## Operational notes

- Keep `result/`, checkpoints, downloaded papers, and generated capsules in a
  user workspace, never inside the installed Skill directory.
- Use `--resume` instead of deleting checkpoints after a partial failure.
- Treat identity ambiguity, missing full text, malformed evidence, and hash
  mismatches as visible states rather than filling gaps with model guesses.
- Review `references/设计稿02-学术人格KG设计.md` and
  `references/设计稿03-蒸馏器与KG编码器.md` before changing schemas or stage order.

## Troubleshooting

**Skill is not discovered:** confirm the directory contains `SKILL.md`, restart
the Host, and run `omni skills sources` or the Host's Skill listing command.

**No LLM connection:** set `OPENAI_API_KEY`, optionally `OPENAI_BASE_URL`, and
`KG_DISTILLER_MODEL` in the Host process environment.

**Identity remains ambiguous:** inspect `identity_candidates.json` and rerun
with `--identity-candidate`; do not choose from name similarity alone.

**A resumed run rebuilds a stage:** inspect the checkpoint input hash. Changed
sources, prompts, schemas, model settings, or stage parameters intentionally
invalidate stale checkpoints.
