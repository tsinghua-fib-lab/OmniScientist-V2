---
name: arxiv-fetch
description: Fetch metadata (title, authors, abstract, PDF link, categories) for an arXiv paper by id or URL. Use for quick lookups of a specific paper the user mentions.
license: Apache-2.0
metadata:
  helixforge:
    version: "1.0"
    dependencies: ["python>=3.11", "httpx"]
    allowed_tools: [bash, write_file]
    tier: research
    role: support
    research_contract: portable_provenance_v1
    priority: 90
    capabilities:
      - paper.fetch.arxiv
    delivery_mode: sync_tool
    kind: python_engine
    engine:
      module: engine
      class: ArxivFetchEngine
      method: execute
    input_schema:
      type: object
      properties:
        identifier:
          type: string
          format: arxiv_id
          description: "arXiv id (e.g. 2401.01234) or abs/pdf URL"
          x-omni:
            semantic_key: paper_id
            binding_owner: resolver
            missing_message: "A concrete arXiv id or URL is required. If only a title is available, resolve it through literature search first."
            repair_capability: literature.search
      required: ["identifier"]
    output_schema:
      type: object
      properties:
        status: {type: string, enum: ["ok", "partial", "error"]}
        outcome: {type: object, description: "domain-specific result metadata such as code, reason, counts, or classification"}
        arxiv_id: {type: string}
        title: {type: string}
        authors: {type: array, items: {type: string}}
        summary: {type: string}
        published: {type: string}
        pdf_url: {type: string}
        categories: {type: array, items: {type: string}}
        source_id: {type: string}
        research: {type: object}
        warning: {type: string}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        error: {type: string}
        error_info: {type: object}
        next_capabilities: {type: array, items: {type: string}}
      required: ["status"]
    workflow:
      failure_policy: continue_with_partial
      failure_types: ["not_found", "network_error", "invalid_identifier", "rate_limited"]
    trigger:
      phrases: ["arxiv-fetch", "arXiv paper", "arxiv id", "arxiv abstract"]
      when_to_use: "Use when the user provides a concrete arXiv id or URL and requests paper metadata or an abstract."
  openclaw:
    emoji: "📄"
    homepage: "https://github.com/tsinghua-fib-lab/OmniScientist-V2"
    requires:
      bins: ["python3"]
---

# arxiv-fetch

Look up a single arXiv paper and return structured metadata. Accepts a bare id
(`2401.01234`), a versioned id (`2401.01234v2`), an `arXiv:` prefix, or an
abs/pdf URL.

Returns: `arxiv_id`, `title`, `authors`, `summary`, `published`, `pdf_url`,
`categories`, plus lifecycle `status` (`ok` / `partial` / `error`). Put
domain-specific conditions such as a missing paper in `outcome.code`
(`not_found`).

## Online requirement

This tool queries the public arXiv API (`https://export.arxiv.org/api/query`)
and therefore **needs network access**. The offline `mock` model can still
*drive* the call, but it cannot fabricate paper data: with no route to arXiv the
tool returns a structured network error (it retries a few
times and never crashes the CLI). Configure a real model and ensure outbound
HTTPS to `export.arxiv.org` for live results.

## Cross-tool compatibility

This `SKILL.md` is shared, unmodified, across OmniScientist, Claude Code, Codex,
and OpenClaw. Other agents read only the top-level `name`/`description`; the
`metadata` block is OmniScientist/HelixForge-specific and ignored elsewhere. If
you prefer an MCP-based arXiv tool, register one under `[mcp_servers.*]` and it
will be discovered alongside this skill.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Copy-only mode: copy this skill folder into a Claude Code, Codex, or OpenClaw
  skill directory. The agent can read this `SKILL.md`, identify arXiv lookup
  requests, and answer from its normal tools.
- Portable runner mode: from this skill directory, run
  `python3 scripts/run.py --json '{"identifier":"1706.03762"}'`. The runner is
  self-contained, uses only Python stdlib plus network access to arXiv, prints a
  structured JSON result, and never requires Omni to be installed.
- Omni enhanced mode: OmniScientist/HelixForge reads `metadata.helixforge`,
  calls `engine.py`, persists the task/workflow state, records artifacts and
  provenance, and can expose the same skill over MCP.

## Portable research provenance

This skill must remain portable across OmniScientist, Claude Code, Codex, and
OpenClaw.

- In OmniScientist, if tools such as `cite_source`, `record_claim`,
  `add_evidence`, `record_hypothesis`, or `log_run` are available, use them and
  include returned ids in `research.source_ids`, `research.claim_ids`,
  `research.evidence_ids`, `research.hypothesis_ids`, and/or `research.run_id`.
- In other runtimes, do not fail because those tools are absent. Instead include
  a **Provenance** section in the Markdown answer and, when file writing is
  available, save `provenance.json` with the same shape plus artifact paths.
- Never invent provenance ids. Use real tool-returned ids; otherwise cite
  human-readable source metadata such as arXiv id, DOI, URL, title, run
  command, and artifact path.
