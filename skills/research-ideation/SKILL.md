---
name: research-ideation
description: "Literature-grounded research ideation: search papers, identify gaps, generate novel ideas through structured concept reasoning, and refine them through critique. Use when the user asks for research gaps, novelty analysis, pressure-tested research directions, or structured research ideas. Do not use for a literature search or related-work survey that does not ask for new ideas or gaps."
license: Apache-2.0
metadata:
  helixforge:
    version: "2.0"
    dependencies: ["python>=3.11", "httpx>=0.27"]
    tier: research
    role: task
    research_contract: portable_provenance_v1
    priority: 80
    capabilities:
      - research.ideation
    deliverables:
      - answer
      - report
      - sources
    delivery_mode: async_task
    kind: python_engine
    engine:
      module: engine
      class: ResearchIdeationEngine
      method: execute
    input_schema:
      type: object
      properties:
        input:
          type: string
          description: "Research question or direction"
          x-omni:
            semantic_role: instruction
        n_ideas:
          type: integer
          description: "Number of candidate ideas to generate (1-5; default 2)"
        use_tools:
          type: boolean
          description: "Whether ideation may query Semantic Scholar for validation (default true)"
      required: ["input"]
    output_schema:
      type: object
      properties:
        status: {type: string, enum: ["ok", "partial", "error"]}
        outcome: {type: object, description: "domain-specific result metadata such as code, reason, counts, or classification"}
        research_question: {type: string}
        steps: {type: object}
        final_idea: {type: object}
        report_uri: {type: string}
        artifacts: {type: array}
        sources: {type: array}
        summary: {type: string}
        text: {type: string, description: "Full ideation report with retrieved paper titles, concept-level reasoning, gaps, and the final idea; prefer displaying this field to the user"}
        research: {type: object}
        run_id: {type: string}
        warning: {type: string}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        error: {type: string}
        error_info: {type: object}
      required: ["status"]
    execution:
      # The pipeline reports progress at six coarse stages, so silence — not
      # elapsed time — is what distinguishes a stuck run from a slow one. The
      # wall clock is only the runaway backstop.
      max_seconds: 1800
      stall_seconds: 600
    workflow:
      failure_policy: continue_with_partial
      failure_types: ["missing_input", "literature_search_failed", "ideation_failed", "pipeline_error"]
    trigger:
      phrases: ["research ideation", "generate research idea", "brainstorm research", "identify research gaps", "novel research direction"]
      when_to_use: "Use when the user wants structured research ideas, gap analysis as a means to propose directions, novelty analysis, or pressure-tested research proposals. Do not use for a literature-only search, related-work survey, or source list that does not ask for new ideas."
    notification:
      display_label: "Research ideation"
      title_field: "input"
  openclaw:
    emoji: "💡"
    homepage: "https://github.com/tsinghua-fib-lab/OmniScientist-V2"
    requires:
      bins: ["python3"]
      env: ["LLM_GATEWAY_BASE_URL", "LLM_GATEWAY_API_KEY"]
---

# research-ideation

A literature-grounded research ideation engine. Given a research question, it runs a four-stage pipeline:

1. **Literature search and concept extraction** — query the available scholarly sources, extract core concepts, and merge synonyms.
2. **Research-gap identification** — use the literature and concepts to identify four to five valuable gaps.
3. **Idea generation** — create candidate ideas through first-principles reasoning, cross-domain analogy, and hypothesis deduction.
4. **Critique and refinement** — assess novelty, disruption, and impact, then refine the strongest candidate.

## Structured concept reasoning

The ideation engine uses a structured concept-evolution process:

- `<concept>` anchors each reasoning step to one concept.
- `<first_principles>` decomposes the concept into fundamentals.
- `<free_association>` explores cross-domain conceptual links.
- `<cross_analogy>` searches for structurally analogous systems.
- `<hypothesis_deduction>` proposes and tests hypotheses.
- `<concept_evolve>` changes granularity or expands scope.
- `<new_concept>` crystallizes a new scientific concept.

## Where the literature comes from

Inside Omni the search runs through the host's retrieval funnel, which fans out
across every enabled connector — arXiv, OpenAlex, Crossref, PubMed, Semantic
Scholar — with health checks, backoff, and a local-corpus floor. No connector is
required: a missing credential costs that one source and the run continues on the
rest. When literature is thin the pipeline says so in its warnings and continues
with LLM-only reasoning rather than stopping.

A Semantic Scholar key is therefore an upgrade, not a prerequisite. Without one
that connector still works on the public tier, just rate-limited enough that the
funnel usually ranks it behind the keyless sources. Request a free key at
<https://www.semanticscholar.org/product/api>; Omni prompts for it during
`omni init`, and it can be set later from the shell or `/config`:

```bash
omni config set research.semantic_scholar_api_key <your-key>
```

Omni stores the value in its owner-level secrets file and exposes it to this
skill only through the Semantic Scholar connector's scoped secrets — never from
the ambient environment. Run standalone, outside Omni, there is no funnel to
borrow: the pipeline queries Semantic Scholar directly and reads `S2_API_KEY`
from the environment to avoid the public tier's strict limits.

## Output

For successful and partial runs, `text` is the complete Markdown report. It
includes retrieved paper titles, available concept-level reasoning, research
gaps, and the final proposal. `summary` remains a compact lifecycle summary.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Copy-only mode: copy this skill folder into a Claude Code, Codex, or OpenClaw
  skill directory. The agent can read this `SKILL.md` and follow the 4-step
  procedure with its normal search/file tools.
- Portable runner mode: from this skill directory, install `httpx` once with
  `python3 -m pip install 'httpx>=0.27'`, then run
  `python3 scripts/run.py --json '{"input":"your research question"}'`.
  The runner is independent of Omni, queries Semantic Scholar with an HTTP client,
  calls an OpenAI-compatible LLM, and does not require Omni.
- Omni enhanced mode: OmniScientist/HelixForge reads `metadata.helixforge`,
  calls `engine.py`, injects its host-managed LLM service without exposing model
  credentials to the skill, persists workflow state, stores report artifacts,
  records provenance, and can compose with other skills.

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
