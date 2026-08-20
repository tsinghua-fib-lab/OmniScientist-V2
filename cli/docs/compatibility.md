# Compatibility with Claude Code, Codex, OpenClaw & MCP

OmniScientist is **bi-directionally** compatible with Claude Code, Codex and OpenClaw, across
several axes. The contract is the shared `SKILL.md` format, so no conversion is ever needed.

## 0. Our skills → their roots (export)

The built-in skills are authored as standalone packages under the repo's [`skills/`](../../skills/)
directory and bundled into the wheel. `omni init`, `omni skills export`, and `/skills export` copy them into the
on-disk roots each tool reads. You pick from **three tools** — `claude` / `codex` / `openclaw` —
and Codex/OpenClaw also get the shared `~/.agents/skills` root so discovery works on any version:

| tool       | writes to                                 | read by            |
|------------|-------------------------------------------|--------------------|
| `claude`   | `~/.claude/skills/<name>/`                | Claude Code        |
| `codex`    | `~/.codex/skills/` + `~/.agents/skills/`  | Codex              |
| `openclaw` | `~/.openclaw/skills/` + `~/.agents/skills/` | OpenClaw         |

| Action | Shell | Inside `omni` |
|---|---|---|
| Export to all tools | `omni skills export` | `/skills export` |
| Export only to Codex | `omni skills export codex` | `/skills export codex` |
| Export explicitly to all | `omni skills export --all` | `/skills export --all` |
| Remove Omni-exported copies | `omni skills unexport` | `/skills unexport` |

Exports are idempotent and tracked in `<OMNI_HOME>/skills_install.json`, so re-running updates our own
copies and `unexport` never deletes a same-named skill you authored yourself.

## 0.5. No-Omni adoption path

The built-in research skills are designed around this promotion principle:

> Skills work without Omni; Omni adds orchestration, provenance, and persistence.

For Claude Code, Codex, and OpenClaw users, use the lightest mode that fits:

1. **Copy-only mode** — copy one skill folder, such as `skills/arxiv-fetch/`, into
   the target tool's skill root. Prompt-only skills are usable this way because
   the host agent reads `SKILL.md` and follows the procedure with its own tools.
2. **Portable runner mode** — for `python_engine` skills, run the self-contained
   bridge from the copied skill directory:

   ```bash
   python3 scripts/run.py --json '{"identifier":"1706.03762"}'
   python3 scripts/run.py --json-file payload.json
   ```

   On Windows PowerShell, prefer `--json-file` or UTF-8 stdin. `--json '{"..."}'`
   loses quotes, and a non-UTF-8 console code page turns Chinese `output_dir`
   values into `????`.

   ```powershell
   python3 scripts/run.py --json-file payload.json
   Get-Content -Raw -Encoding utf8 payload.json | python3 scripts/run.py
   ```

   These runners do not import Omni. They print structured JSON, return
   `status: "error"` instead of crashing on bad input/network failures, and write
   local artifacts/provenance when the skill supports it.
3. **Omni enhanced mode** — install/register Omni only when you want durable
   workflow tasks, persistent corpus indexing, artifact storage, provenance
   records, task visualisation, or MCP tools through `omni mcp serve`.

External agents should call `scripts/run.py` or use MCP; they should not import
`engine.py` directly because `engine.py` is the Omni/HelixForge adapter that
expects `ExecContext`, task state, artifact storage, and provenance services.

### LiveFigure exception: Omni-first VLM execution

LiveFigure remains discoverable and copyable like the other built-ins, but
its reliable first integration is Omni-first. Keep the multimodal model and API
key in Omni, run `omni config vlm`, and expose the skill to Claude Code, Codex,
or OpenClaw through `omni mcp serve`. This avoids weakening a host agent's secret
filtering merely to make a shell-launched runner inherit `OMNI_VLM_API_KEY`.
The MCP contract never returns that credential. Within Omni, however,
LiveFigure is trusted in-process code: its adapter follows the narrow VLM port,
but `ExecContext` is not a hard security sandbox and other trusted Python Skill
code could inspect host settings. Treat imported executable Skills as trusted
code and use a separate process with a sanitized context if hard isolation is
required.

Direct portable execution is an advanced option. It requires all three
`OMNI_VLM_MODEL`, `OMNI_VLM_ENDPOINT`, and `OMNI_VLM_API_KEY` variables and an
OpenAI-compatible multimodal chat endpoint. Never place the key in `SKILL.md`, a
project file, a prompt, or generated PowerPoint code.

For OpenClaw, add an stdio MCP server with command `omni` and arguments
`["mcp", "serve"]` using the host's MCP configuration surface. The copied skill
remains discoverable without injecting the VLM secret into OpenClaw; Omni owns
the credential and executes LiveFigure behind that MCP transport boundary.

## 1. Their skills → our agent (discovery & import)

OmniScientist can discover and run Claude Code / Codex / OpenClaw skills directly, but by default it
shows **only OmniScientist-managed skills** so your full external libraries (and deployment-specific
entries like `csi-claw-fusion-*`) don't flood `omni skills list` or `/skills list`.

**Default sources** (`skills.sources`, high→low priority): `project_omni` (`<repo>/.omni/skills`,
walked up to the repo root), `user_omni` (`<OMNI_HOME>/skills`), and `builtin` (bundled). Same-named
skills are resolved by priority (your project/user skills override the built-ins).

**Opt-in: see/use everything on the machine.** `--all` adds the external roots
`~/.claude/skills`, `~/.codex/skills` (`$CODEX_HOME`), `~/.agents/skills`, `~/.openclaw/skills`, and
the project `.claude`/`.agents` roots:

| Action | Shell | Inside `omni` |
|---|---|---|
| Show discovery roots | `omni skills sources` | `/skills sources` |
| List managed skills | `omni skills list` | `/skills list` |
| Include external libraries | `omni skills list --all` | `/skills list --all` |
| Inspect one skill | `omni skills info <name>` | `/skills info <name>` |

**Import an external skill into omni.** `omni skills add` or `/skills add` copies a skill into a quarantine under
`<OMNI_HOME>/skills`. It remains non-executable and excluded from planning until the owner reviews it
and runs `omni skills trust <name> --yes` or `/skills trust <name> --yes`:

| Action | Shell | Inside `omni` |
|---|---|---|
| Import from Claude Code | `omni skills add claude:my-skill` | `/skills add claude:my-skill` |
| Import from Codex | `omni skills add codex:my-skill` | `/skills add codex:my-skill` |
| Import from shared Agent Skills | `omni skills add agents:my-skill` | `/skills add agents:my-skill` |
| Import from OpenClaw | `omni skills add openclaw:my-skill` | `/skills add openclaw:my-skill` |
| Import a local directory | `omni skills add ~/work/my-skill` | `/skills add ~/work/my-skill` |
| Import a local Markdown file | `omni skills add ~/work/my-skill/SKILL.md` | `/skills add ~/work/my-skill/SKILL.md` |
| Import a Git repository | `omni skills add https://github.com/org/repo.git` | `/skills add https://github.com/org/repo.git` |
| Import one Git sub-path | `omni skills add https://github.com/org/repo#skills/example` | `/skills add https://github.com/org/repo#skills/example` |
| Trust after review | `omni skills trust my-skill --yes` | `/skills trust my-skill --yes` |
| Remove an imported skill | `omni skills remove my-skill` | `/skills remove my-skill` |

The import keeps the whole package (engine, scripts, references) for review, but does not endorse or
execute it. After trust, compatible prompt/engine contracts become eligible for routing. A git source may be a single
skill (root `SKILL.md`), hold skill folders at the top level, or under a `skills/` directory; all
discovered skills are imported, each named from its own `SKILL.md` and requiring separate trust.
Direct Git imports use the default branch. Raw HTTP(S) files, archives, and repository `/tree/...`
browser pages are not accepted; clone a non-default or pinned revision locally before importing it.
See [skills.md](skills.md#supported-skills-add-sources) for every accepted source form, overwrite
semantics, and package limits.

How discovered/imported skills run:

- Engine/exec skills run via the executor (engines load from the skill's own folder).
- Prompt skills (the common external case — only `name` + `description` + body) run through
  `run_skill`, which spins up a focused ReAct sub-agent equipped with the Claude-Code-compatible
  builtin tools (`read_file`, `write_file`, `edit_file`, `bash`, `grep`, `glob`, `web_fetch`),
  approximating the Claude Code execution environment.

`$skill-name` is the explicit, deterministic selection form, not a requirement
for every imported skill. Once trusted, a plain prompt skill is visible to the
normal ReAct catalogue and can be discovered from its description; a skill with
an Omni capability contract can additionally be selected deterministically by
the semantic planner. A contract-less skill is not accepted as a required step
in an automatically planned multi-skill workflow. This preserves zero-adapter
use without claiming the same reliability as a typed workflow provider.

See [skills.md](skills.md) for the complete load/selection lifecycle, natural
language and explicit examples, security boundary, and a direct comparison with
Claude Code, Codex, and OpenClaw.

## 2. Our skills → their agents (MCP server)

`omni mcp serve` runs an MCP **server** (stdio) exposing:

- one tool per OmniScientist skill (executed through the skill executor and returned synchronously),
- `omni_ask` — delegate a full research turn (planning + skills + memory),
- `omni_list_skills` — introspection.

Register it into the host tools (idempotent, preserves your existing config):

```bash
omni mcp install codex      # writes [mcp_servers.omniscientist] to ~/.codex/config.toml
omni mcp install claude     # writes mcpServers.omniscientist to ~/.claude.json
omni mcp install both
omni mcp uninstall both  # removes only mcpServers.omniscientist
```

Manual equivalents:

```toml
# ~/.codex/config.toml
[mcp_servers.omniscientist]
command = "omni"
args = ["mcp", "serve"]
```

```jsonc
// ~/.claude.json
{ "mcpServers": { "omniscientist": { "command": "omni", "args": ["mcp", "serve"] } } }
```

Claude Code users can also run: `claude mcp add omniscientist -- omni mcp serve`.

After registering, ask Claude Code / Codex things like *"use omni_ask to review arXiv 2310.06825"*
or call `openalex-search` directly. Use the direct skill tools when the host already knows which
skill to call. Use `omni_ask` when you want Omni's full agent loop: registry-driven planning,
`run_workflow`, durable tasks, memory, corpus/provenance tools, partial recovery, and task results.

## 3. External MCP servers → our agent (MCP client)

Any MCP server you configure becomes callable inside OmniScientist's ReAct loop:

```toml
# <OMNI_HOME>/config.toml
[mcp_servers.fetch]
command = "uvx"
args = ["mcp-server-fetch"]

[mcp_servers.some_http]
url = "https://example.com/mcp"     # SSE transport
```

```bash
omni mcp list                # show configured external servers
```

Tools are namespaced `server__tool` and introspected on demand.

## 4. Shared project guidance (AGENTS.md)

```bash
omni mcp agents              # writes AGENTS.md at the project root
```

This mirrors Codex's `AGENTS.md` convention so Codex / Claude Code and OmniScientist share the same
project conventions (where artifacts go, the notebook, available research tools).

## Format contract

- Required by Claude Code: `name`, `description`. Use **hyphen-case** names (e.g. `arxiv-fetch`) —
  this is what Codex and OpenClaw expect and is valid as an MCP / function-calling tool name.
- OmniScientist extensions live under `metadata.helixforge` (engine/exec/delivery/schema/trigger/
  notification) and OpenClaw hints under `metadata.openclaw` (emoji/homepage, `requires.bins`,
  `requires.env`). Both are ignored by Claude Code and Codex, so the same file is portable
  everywhere.
