# Skills: install, discovery, selection, and execution

This is the user-facing source of truth for how OmniScientist loads and uses
built-in, project, and third-party skills. For authoring fields and runtime
contracts, see [the skill authoring guide](../../skills/docs/authoring.md). For
cross-agent export and MCP integration, see [compatibility.md](compatibility.md).

## The short version

- `$skill-name` is a **forced selection**, not the only way to use a skill.
- A normal natural-language request can use an installed, trusted skill without
  naming it.
- Contracted skills are selected deterministically through capabilities.
- A plain AgentSkills/Claude Code skill can be model-discovered and run as a
  prompt-only skill, but description-based selection is not a deterministic
  routing guarantee.
- `skills add` imports a third-party skill without changing Omni code. Adapt a
  skill with `metadata.helixforge` only when it must become a built-in,
  structured workflow provider with stronger input/output and provenance
  guarantees.
- Omni never downloads and executes an internet skill merely because a prompt
  appears to need it. Automatic selection is limited to already installed,
  trusted, enabled skills.

## Install and use a third-party skill

The same commands work as shell commands and, with a leading `/`, inside the
interactive `omni` REPL.

```bash
# 1. Import from another agent's local library, a directory, or Git.
omni skills add claude:my-skill
omni skills add codex:my-skill
omni skills add agents:my-skill
omni skills add openclaw:my-skill
omni skills add ~/work/my-skill
omni skills add ~/work/my-skill/SKILL.md
omni skills add https://github.com/org/repo.git#skills/my-skill

# 2. Inspect the imported package while it is quarantined.
omni skills info my-skill

# 3. After reviewing SKILL.md, license, scripts, and executable files, enable it.
omni skills trust my-skill --yes

# 4a. Ask normally. Omni may select the skill from the task intent.
omni exec "Draft a point-by-point response to the reviewer comments in reviews.md"

# 4b. Force this exact skill when selection must be deterministic.
omni exec '$my-skill Analyse these reviewer comments'
```

Inside the REPL:

```text
/skills add codex:my-skill
/skills info my-skill
/skills trust my-skill --yes
› Analyse these reviewer comments and prepare a point-by-point response.
```

### Ready-to-copy built-in examples

These natural-language requests exercise the recently added or updated built-in
skills without naming them. The full built-in set also includes `scientific-figure`,
`livefigure`, `arxiv-fetch`, `openalex-search`, `research-ideation`,
`scientist-kg-distiller`, and `soulagent`.

```bash
omni exec "Review paper.pdf for NeurIPS submission readiness and prioritise revisions"
omni exec "Draft a point-by-point response to the reviewer comments in reviews.md"
omni exec "Create a complete conference poster from paper.pdf with an HTML preview"
omni exec "Create a 12-slide group-meeting deck from paper.pdf"
```

The deterministic equivalents are:

```bash
omni exec '$paper-review Review paper.pdf for NeurIPS'
omni exec '$review-response Draft responses for reviews.md'
omni exec '$scientific-poster Create a conference poster from paper.pdf'
omni exec '$research-pptx Create a 12-slide group-meeting deck from paper.pdf'
```

The portable upstream names are normalized to Omni's hyphen-case names:
`paper_review` becomes `paper-review`, and the adapted poster skill is
`scientific-poster`.

Importing and trusting are deliberately separate. `skills add` copies the whole
package into `<OMNI_HOME>/skills/<name>/` and records its origin, but the skill
cannot execute or participate in planning until it is trusted.

If the source has no declared license, `skills trust` refuses it by default.
Use `--force` only when you have independently established the right to use the
content.

### Supported `skills add` sources

`omni skills add <source>` accepts the following source forms:

| Source | Example | Behaviour |
|---|---|---|
| Local skill directory | `omni skills add ./my-skill` | The directory must contain `SKILL.md` at its root; the complete package is copied |
| Local `SKILL.md` | `omni skills add ./my-skill/SKILL.md` | Imported using the parent directory name unless `--name` is supplied |
| Local Markdown file | `omni skills add ./review-workflow.md` | Stored as `<OMNI_HOME>/skills/review-workflow/SKILL.md` |
| Claude Code user skill | `omni skills add claude:my-skill` | Reads `~/.claude/skills/my-skill/` or the legacy flat `my-skill.md` |
| Codex user skill | `omni skills add codex:my-skill` | Reads `$CODEX_HOME/skills/my-skill/` (normally `~/.codex/skills`) |
| Shared Agent Skill | `omni skills add agents:my-skill` | Reads `~/.agents/skills/my-skill/` |
| OpenClaw user skill | `omni skills add openclaw:my-skill` | Reads `~/.openclaw/skills/my-skill/` |
| HTTPS/HTTP Git remote | `omni skills add https://gitee.com/org/repo.git` | Shallow-clones the remote's default branch; prefer HTTPS over unencrypted HTTP |
| SSH or `git+` remote | `omni skills add git@github.com:org/repo.git` | Uses the installed `git` client and its existing credentials |
| Well-known-host shorthand | `omni skills add github.com/org/repo` | Recognized for GitHub, GitLab, Gitee, Bitbucket, Codeberg, and SourceHut |
| Git subdirectory | `omni skills add 'https://github.com/org/repo.git#skills/foo'` | Restricts discovery to that repository-relative directory; `#path=...` and `#subdir=...` are aliases |

For every `tool:name` prefix, Omni first checks
`<tool-root>/<name>/SKILL.md`, then the compatible flat
`<tool-root>/<name>.md` form.

Use `--name <name>` to override the destination name when one skill is being
imported. If the destination already exists, the command skips it; use
`skills add --force` only after reviewing the replacement. Re-importing is a
copy operation, not an upstream subscription, so updates also require an
explicit `skills add --force`.

The two `--force` flags have different scopes:

| Command | Meaning |
|---|---|
| `omni skills add <source> --force` | Replace an existing imported skill with the new source content and return it to quarantine |
| `omni skills trust <name> --force --yes` | Trust a skill without a declared license only after independently verifying usage rights |

### Git repository layout and revisions

A Git source may have one of these layouts:

```text
repo/SKILL.md
repo/<skill-name>/SKILL.md
repo/skills/<skill-name>/SKILL.md
```

When the repository itself is a skill, only that root skill is imported.
Otherwise Omni imports immediate child skill directories at the repository
root and under `skills/`. Use `#path/to/collection` when skills live more
deeply. If several skills are found, each keeps the name declared in its own
`SKILL.md`, and each must be inspected and trusted separately:

```bash
omni skills list
omni skills info first-skill
omni skills trust first-skill --yes
omni skills info second-skill
omni skills trust second-skill --yes
```

The current command always shallow-clones the Git remote's **default branch**.
It does not yet accept a branch, tag, or commit selector. To install a pinned
or non-default revision, check it out locally first and import the selected
directory:

```bash
git clone --branch <tag-or-branch> --depth 1 <repo-url> skill-source
omni skills add ./skill-source/path/to/skill
```

For an exact commit, clone the repository, run `git checkout <commit>`, then
run the same local `skills add` command. The recorded import provenance includes
the resolved commit for a direct Git import, but recording a commit is not the
same as selecting or pinning it.

GitHub, Gitee, and GitLab `/tree/<ref>/<path>` pages are web UI addresses, not
Git remotes, and cannot be passed directly. Convert them to a clone URL plus an
Omni subdirectory fragment, provided the skill is already on the default
branch:

```text
https://github.com/org/repo/tree/main/skills/foo
    ↓
https://github.com/org/repo.git#skills/foo
```

### Unsupported sources and import limits

The current command does **not** download these source types:

- a raw HTTP(S) `SKILL.md` or other Markdown URL;
- a remote ZIP, TAR, or other archive;
- a Git hosting `/tree/...` or `/blob/...` web page;
- an npm package or remote marketplace manifest;
- a `file://` URL (use the corresponding filesystem path instead).

An HTTP(S) URL is accepted only when it is recognized as a Git remote. For an
unknown/self-hosted Git service, use a full Git URL ending in `.git` or a
`git+` form rather than a bare host path; SSH remotes should also use their
normal `.git` form. Git sources require `git` on `PATH`, and cloning times out
after 120 seconds.

Every skill package selected from a local or cloned source is validated before
copying:

- at most 500 regular files;
- at most 10 MiB in total;
- no symbolic links inside the imported package;
- a destination name of 1–128 characters using only letters, digits, `.`, `_`,
  and `-`, beginning with a letter or digit.

These limits apply separately to each imported skill package. For a large
monorepo, use the Git `#path/to/skill` form so Omni validates and copies only
the intended skill or collection.

## Add, discover, export, and invoke are different operations

| Operation | Meaning |
|---|---|
| `omni skills add <source>` | Copy an external skill into Omni quarantine |
| `omni skills trust <name> --yes` | Allow a reviewed imported skill to execute and participate in selection |
| `omni skills list` | List Omni-managed built-ins, project skills, and imported skills |
| `omni skills list --all` | Browse external Claude Code, Codex, and OpenClaw roots too; this does not import or trust them |
| `omni skills search <query>` | Search visible skill metadata interactively |
| `omni skills setup research-pptx` | Repair the lockfile-pinned Node renderer (currently the only setup target) |
| `omni skills why <name>` | Explain why a skill was or was not selected |
| `omni skills export [tool]` | Copy Omni built-ins out to another agent's skill roots |
| Natural-language request | Let the planner/agent choose an eligible skill |
| `$name ...` | Force a particular installed skill |

`list --all` is an inspection surface, not a permanent enable switch. To make a
skill from another agent's library part of Omni's normal managed catalogue,
import it with `skills add` and trust it. Advanced users may instead configure
additional `skills.sources`.

## Discovery and loading

Omni discovers managed skills from these sources, highest priority first:

1. project Omni skills: `<repo>/.omni/skills/`;
2. user/imported Omni skills: `<OMNI_HOME>/skills/`;
3. bundled built-ins.

The complete opt-in catalogue also includes project and user roots used by
Claude Code, Codex, and OpenClaw:

- `.claude/skills/` and `~/.claude/skills/`;
- `.agents/skills/` and `~/.agents/skills/`;
- `~/.codex/skills/`;
- `~/.openclaw/skills/`.

Use these commands to inspect what Omni sees:

```bash
omni skills sources
omni skills list --group
omni skills list --all --group
omni skills info my-skill
```

Discovery is recursive for configured roots. When the same name occurs in
multiple managed sources, the higher-priority source wins.

Omni uses progressive loading:

1. discovery parses `SKILL.md` metadata;
2. the planner and ReAct agent receive a compact catalogue containing names,
   descriptions, usage hints, execution modes, and available contracts;
3. full instructions and supporting files are loaded only after a skill is
   selected;
4. prompt-only skills run in a focused, bounded ReAct context; engine, CLI, and
   MCP skills execute through their declared adapters.

Good `description` and `when_to_use` text therefore matter. Put the primary use
case and boundaries first. Trigger phrases are search aliases and examples, not
language-specific substring rules.

## How automatic selection works

Omni has two complementary selection lanes.

### Contracted capability lane

Built-ins and adapted skills may declare:

- `capabilities` and `deliverables`;
- `role` and `contract_level`;
- input and output schemas;
- execution and workflow policy.

The semantic planner describes the task in capability terms, such as
`review.response` or `poster.scientific`. The registry then chooses an eligible
provider using trust, contract strength, source priority, configured defaults,
and role. This lane supports durable multi-step workflows, schema
validation, artifacts, provenance, and recovery.

### Universal prompt-skill lane

A third-party skill containing only `name`, `description`, and Markdown
instructions is imported as a prompt-only skill. After trust:

- it is included in the selectable catalogue;
- the normal ReAct tool surface can search it with `find_skill`;
- the model can load and execute it with `run_skill`;
- the user may force it with `$name`.

This makes zero-adaptation, natural-language use possible. It is still a
model-side description match: overlapping or vague descriptions can cause a
skill to be missed or the wrong skill to be chosen.

A skill installed later does not live in an “explicit-only” silo. After trust,
its source is a policy and ranking attribute, while its description and any
contracts join the same selectable catalogue. Full-contract built-ins normally
win when they already satisfy the requested capability; external skills provide
new capabilities or a ReAct fallback without a per-skill Omni code change.

The current guarantee boundary is:

| Situation | Behaviour |
|---|---|
| User writes `$name` | Exact eligible skill has priority |
| Skill declares a matching full/partial contract | Capability routing can select it automatically |
| Plain trusted prompt skill, no Omni contract | ReAct may discover and invoke it from its description |
| Plain skill used as a required automatic workflow step | Rejected; add a contract or invoke it explicitly |
| Skill is quarantined, disabled, or deprecated | Excluded from normal automatic selection |
| Relevant skill exists only on the internet | Not auto-installed or executed |

In other words, explicit invocation is the reliability escape hatch, not a
requirement for every third-party skill.

The current runtime does **not** promise a mandatory
“capability gap → retrieve every contract-less external skill → rank Top-K →
invoke the winner” stage. Contract-less discovery is currently performed by the
ReAct model and tools. Adding such a deterministic fallback would be a one-time
Omni runtime enhancement, not an adaptation requirement for every external
skill.

## Explicit invocation forms

The recommended public form is `$name`. The agent boundary also recognizes
these text aliases before semantic planning:

```text
$my-skill task description
skill:my-skill task description
run_skill my-skill task description
```

The normal shell forms are:

```bash
omni '$my-skill task description'
omni exec '$my-skill task description'
```

Use explicit selection when:

- two skills have overlapping descriptions;
- a reproducible script or evaluation must use a known provider;
- the skill has no Omni contract and must not depend on model discovery;
- the user intentionally overrides the normal built-in/provider priority.

## Execution types

| Kind | How Omni executes it | Typical use |
|---|---|---|
| `prompt_only` | Focused ReAct execution using the skill instructions and allowed tools | Portable third-party procedure or writing workflow |
| `python_engine` | Calls a class from the skill's own `engine.py` through the public runtime | Structured research or artifact generation. Omni-enhanced figure skills generate and render DOT/SVG/PNG from natural-language `input`; pass `source_artifact_dot` only for an exact graph or a revision. Do not re-render with `bash dot` after the engine returns |
| `cli_exec` | Runs a declared external executable and parses JSON/text output | Existing command-line tool |
| `remote_mcp` | Calls a declared tool on an MCP server | Remote capability or service |

Delivery is separate from implementation:

- synchronous tools can run inline;
- foreground tasks persist and wait for completion;
- background tasks persist and return a task id;
- workflows compose several contracted steps.

The bundled `research-pptx` renderer has an owner-managed, lockfile-pinned Node
runtime. Prepare or repair it outside task execution:

```bash
omni skills setup research-pptx
```

This is currently the only `skills setup` target. Third-party skills retain
their own reviewed dependency instructions; tasks do not invoke package
managers.

## When a third-party skill needs Omni adaptation

No adaptation is required merely to install, inspect, explicitly invoke, or let
the model discover a prompt-only skill.

Add `metadata.helixforge` when the skill needs one or more of these guarantees:

- preinstallation in Omni's built-in inventory;
- deterministic capability selection;
- structured and validated inputs or outputs;
- use as a step in a durable multi-skill workflow;
- durable foreground/background task semantics;
- artifact registration, research provenance, or recovery;
- an engine, CLI, MCP, or owner-controlled dependency setup binding.

Keep Omni fields under `metadata.helixforge`; keep the main `SKILL.md`
instructions portable. This is progressive enhancement rather than a fork of
the upstream skill.

## Comparison with other agents

All four systems support explicit and implicit use of installed skills, but
they place the reliability boundary in different places.

| Agent | Default discovery and invocation | Scaling and eligibility | Stronger platform-specific layer |
|---|---|---|---|
| [Claude Code](https://code.claude.com/docs/en/slash-commands) | Names/descriptions are available to Claude; matching tasks can load full skill content; `/name` forces invocation | Description quality controls matching; `disable-model-invocation` can make a skill user-only | Tool permissions, subagent context, dynamic context, plugins |
| [Codex](https://developers.openai.com/codex/skills) | Starts with name/description/path, loads full `SKILL.md` when selected; `$name` forces invocation | Initial catalogue has a bounded context budget; `allow_implicit_invocation` defaults to true | `agents/openai.yaml` policy/dependencies and plugin distribution |
| [OpenClaw](https://github.com/openclaw/openclaw/blob/main/docs/tools/skills.md) | Eligible skill identities/descriptions/locations are compiled into a compact system-prompt catalogue; slash commands can force a skill | Source precedence, agent allowlists, binary/env/config gates, prompt budget, session snapshots | `metadata.openclaw`, ClawHub, dependency/install hints |
| OmniScientist | Trusted catalogues are available to capability planning and ReAct; `$name` forces a provider | Managed sources by default; quarantine/trust; contract strength and provider priority | Typed capabilities, durable workflow/step/attempt records, artifacts, provenance, and retryable tasks |

Claude Code, Codex, and OpenClaw primarily rely on description-driven model
selection for portable skills. Omni supports that portable lane and adds a
contracted lane for research workflows. A host-specific flag is not assumed to
have identical semantics in every other host; review imported side-effecting
skills and use Omni trust/tool policy to control them.

## Security boundary

Treat third-party skills as untrusted code until reviewed. A `SKILL.md` can
instruct an agent to run commands, and its package may contain executable
Python, JavaScript, shell, or PowerShell files.

Before trusting:

1. inspect `SKILL.md`, `LICENSE.txt`, and `NOTICE.md`;
2. inspect every executable or generated-code path;
3. check network, credential, file-write, and package-install behaviour;
4. when reproducibility matters, check out a pinned Git revision locally before
   importing it because direct Git imports currently use the default branch;
5. keep destructive or external side-effect workflows explicitly invoked.

Trust makes a skill eligible; it does not prove the skill is safe. Omni's tool
policy, task isolation, and approval boundaries still apply.

## Troubleshooting

**A newly added skill does not run**

```bash
omni skills info my-skill       # check source, status, kind, and instructions
omni skills trust my-skill --yes
omni skills list --group
```

**Natural-language selection misses the skill**

- make `description` specific and front-load the use case;
- remove overlap with another skill's description;
- add `metadata.helixforge.trigger.when_to_use`;
- use `$my-skill` when exact selection is required;
- add a capability contract when it must participate deterministically in
  automatic workflows.

**The skill appears only with `--all`**

It is in another agent's root but is not Omni-managed. Import it:

```bash
omni skills add claude:my-skill   # or codex:/agents:/openclaw:
omni skills trust my-skill --yes
```

**A Git `/tree/...` URL fails, or the skill is on another branch**

`skills add` accepts Git clone URLs, not repository browser pages, and direct
Git imports use the default branch. Clone the required revision locally, then
import the skill directory:

```bash
git clone --branch <ref> --depth 1 <repo-url> skill-source
omni skills add ./skill-source/path/to/skill
```

**An existing imported skill does not update**

Imports are copied snapshots. Inspect the new source, then replace the
quarantined copy explicitly:

```bash
omni skills add <source> --force
omni skills info my-skill
omni skills trust my-skill --yes
```

**The skill needs a missing binary or runtime**

Review its dependency instructions. For the bundled `research-pptx` renderer,
run `omni skills setup research-pptx`. Task execution will not install
dependencies.
