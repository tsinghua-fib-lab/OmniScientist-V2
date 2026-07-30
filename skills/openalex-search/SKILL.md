---
name: openalex-search
description: Search OpenAlex (250M+ scholarly works across all fields) for papers on a topic. Use for broad, cross-disciplinary literature beyond arXiv (biology, medicine, social science, etc.).
license: Apache-2.0
metadata:
  helixforge:
    version: "1.0"
    dependencies: ["python>=3.11", "httpx"]
    allowed_tools: [bash, write_file]
    tier: research
    role: support
    research_contract: portable_provenance_v1
    priority: 30
    capabilities:
      - literature.search
    deliverables:
      - sources
    delivery_mode: sync_tool
    kind: python_engine
    engine:
      module: engine
      class: OpenAlexSearchEngine
      method: execute
    input_schema:
      type: object
      properties:
        query: {type: string, description: "topic / keywords"}
        max_results: {type: integer, description: "1-25, default 8"}
      required: ["query"]
    output_schema:
      type: object
      properties:
        status: {type: string, enum: ["ok", "partial", "error"]}
        outcome: {type: object, description: "domain-specific result metadata such as code, reason, counts, or classification"}
        query: {type: string}
        count: {type: integer}
        indexed: {type: integer}
        results: {type: array}
        sources: {type: array}
        summary: {type: string}
        research: {type: object}
        warning: {type: string}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        error: {type: string}
        error_info: {type: object}
      required: ["status"]
    workflow:
      failure_policy: continue_with_partial
      failure_types: ["missing_query", "network_error", "empty_results", "rate_limited"]
    trigger:
      phrases: ["openalex", "cross-disciplinary literature", "search openalex", "scholarly works"]
      when_to_use: "Use for cross-disciplinary scholarly literature beyond arXiv."
  openclaw:
    emoji: "🌐"
    requires:
      bins: ["python3"]
---

# openalex-search

Search OpenAlex and return normalized scholarly records. When Omni runs this
skill, it also stores the results in the current workspace so native corpus
search can cite them later; portable hosts receive the search results directly.

Returns a `results` list (`title`, `authors`, `year`, `doi`, `url`, `venue`,
`summary`). Omni results also report how many new workspace sources were
indexed. Set a contact email for the polite pool:
`omni config set research.contact_email you@example.com`. Needs network access;
offline it returns a clean error.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Copy-only mode: copy this skill folder into a Claude Code, Codex, or OpenClaw
  skill directory. The agent can read this `SKILL.md` and search broad scholarly
  literature with its normal tools.
- Portable runner mode: from this skill directory, run
  `python3 scripts/run.py --json '{"query":"single-cell foundation models","max_results":5}'`.
  The runner is self-contained, queries OpenAlex with Python stdlib, can write
  local JSON artifacts with `output_dir`, and does not require Omni to be
  installed.
- Omni enhanced mode: OmniScientist/HelixForge reads `metadata.helixforge`,
  calls `engine.py`, respects connector settings, persists task state, indexes
  records into the local corpus, and records provenance.

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
