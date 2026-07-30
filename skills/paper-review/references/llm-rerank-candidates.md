# LLM Rerank Candidates

Use this reference after Semantic Scholar retrieval and before novelty QA. Reranking is a semantic task for the active LLM or an OpenScholar-compatible reranker; do not replace it with script-level keyword or metadata scoring.

## Inputs

Provide the reranker with:

- paper title
- paper abstract
- 3 key research questions from `novelty-verification.md`
- systematic paper analysis:
  - motivation
  - core idea
  - technical approach
  - experimental design
  - claimed contributions
  - assumptions
  - novelty claims
- up to 20 Semantic Scholar candidates:
  - title
  - abstract
  - authors
  - year
  - venue
  - url
  - citation count

Do not include full paper body text in the rerank prompt unless the user explicitly permits it.

## Task

Rank candidates by how important they are for evaluating the target paper's novelty, positioning, and missing-citation risk.

Prefer papers that:

- address the same research gap;
- propose a similar method, mechanism, evaluation setup, task, dataset, or benchmark;
- directly challenge the paper's novelty claim;
- imply a missing baseline or missing differentiation;
- are canonical or recent enough to affect positioning.

Do not rank highly solely because of:

- broad topical similarity;
- a shared generic keyword;
- citation count;
- venue prestige;
- recency without conceptual overlap.

## Required Output

Return exactly this structure:

```yaml
rerank_method: llm
top5:
  - rank:
    paper_id:
    external_ids:
    title:
    authors:
    year:
    venue:
    url:
    relevance: high | medium | low
    overlap_type:
      - research_gap
      - method
      - task_or_dataset
      - evaluation
      - baseline
      - theory
      - application
    evidence:
      paper_claim:
      candidate_evidence:
      why_it_matters_for_novelty:
    citation_action: cite | cite_and_differentiate | consider_baseline | discuss_limitation | no_action
    confidence: high | medium | low
excluded_not_relevant:
  - title:
    reason:
rerank_limitations:
  - ...
```

## Decision Rules

- Include at most 5 papers in `top5`.
- Copy `paper_id`, `external_ids`, title, authors, year, venue, and URL verbatim from the retrieved candidate so deterministic citation matching remains possible.
- Use `high` relevance only when the candidate overlaps with the target paper's claim, method, task, or evaluation in a way that could affect novelty or evaluation.
- Use `medium` relevance for papers that are useful context but not directly threatening to novelty.
- Do not include low-relevance papers in `top5` unless fewer than 5 medium/high candidates exist.
- Use `excluded_not_relevant` for tempting but broad matches that share generic terms without novelty impact.
- Missing-citation suggestions in the final review should come only from top5 entries with `citation_action` other than `no_action`, after checking whether they are already cited.

## Novelty QA Handoff

The novelty QA step should use the top5 entries and answer:

- What appears genuinely new in the target paper?
- Which prior works overlap most directly?
- Which novelty claims need narrowing?
- Which papers should be cited, differentiated, or used as baselines?
- How confident is this conclusion, given available abstracts and metadata?
