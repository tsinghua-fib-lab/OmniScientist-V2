# Novelty Verification

Novelty is a dedicated verification stage, not a paragraph added at the end. The goal is to approximate how a strong reviewer checks whether the paper is genuinely new relative to related work.

## Architecture

```text
paper
 -> generate 3 key research questions
 -> systematic paper analysis
 -> active LLM converts questions into literature-search queries
 -> Semantic Scholar retrieval, up to 20 papers
 -> active LLM rerank to top 5
 -> novelty QA / analysis over top 10
 -> deterministic citation-status audit against reference list
 -> active LLM selects citation actions
 -> final novelty and related-work coverage section
```

Keep model choices pluggable. Use the active LLM unless a future implementation adds explicit model routing.

## Step 1: Generate 3 Key Research Questions

Generate exactly 3 questions from title, abstract, introduction, and contribution statements.

The questions must cover:

- research gap: what unresolved problem does the paper claim to address?
- innovation direction: what new idea, formulation, setting, or capability is claimed?
- method breakthrough: what technical mechanism is presented as the key advance?

Schema:

```yaml
research_questions:
  - question:
    rationale:
    query_intent:
```

## Step 2: Systematic Paper Analysis

Analyze:

```yaml
paper_analysis:
  motivation:
  core_idea:
  technical_approach:
  experimental_design:
  claimed_contributions:
  assumptions:
  novelty_claims:
```

Ground the analysis in the paper. Mark inferred claims as inferred.

## Step 3: Query Construction

Have the active LLM use the title, abstract, paper analysis, and research questions to build concise literature-search queries. Treat this as a semantic task; do not use script-level stopword lists, sentence-marker lists, or keyword scoring to construct queries.

Rules:

- Use 3-4 concise queries total across all questions.
- Each query should be 4-10 terms when possible.
- Queries should capture task, method family, dataset/setting, and claimed innovation category.
- Return those 3-4 unique queries and pass them verbatim to `scripts/semantic_scholar_search.py`.

Schema:

```yaml
queries:
  - query:
    purpose:
    derived_from: title | abstract | research_question_1 | research_question_2 | research_question_3
```

## Step 4: Retrieve Up to 20 Candidate Papers (One S2 Call)

Pass the active-LLM queries to `scripts/semantic_scholar_search.py` exactly once and collect up to 20 candidate papers from:

- title query
- abstract-derived contribution queries
- research-question-derived queries

Do not use citation-graph expansion in V1.

Semantic Scholar is the only literature source in this workflow. Do not call OpenAlex, Crossref, search-corpus tools, web search, or another literature helper. After this call returns, stop retrieval even when results are sparse or partial.

The retrieval helper may validate query shape, call the API, balance results across queries, and deduplicate identities. It must not generate queries or rank candidates by keyword overlap, citations, recency, or venue.

## Step 5: LLM Rerank to Top 5

Read `llm-rerank-candidates.md` and rerank the candidates into top 5 for novelty analysis. This is the primary reranking path.

Reranking must consider:

- overlap with the 3 research questions
- overlap with claimed contributions
- task, method, dataset, or setting overlap
- abstract-level conceptual similarity
- recency and canonicality
- venue and citation visibility as weak supporting signals

If an OpenScholar-compatible reranker exists, use it. Otherwise use the active LLM with the reranking prompt in `llm-rerank-candidates.md`. Do not substitute script-level keyword, citation-count, recency, or venue scoring for semantic reranking.

## Step 6: Novelty QA

Analyze the paper against the selected top 5 (or fewer when retrieval is limited):

```yaml
novelty_analysis:
  likely_new_elements:
  overlapping_prior_work:
  unclear_or_overclaimed_novelty:
  missing_differentiation:
  missing_citations:
  baseline_implications:
  confidence:
```

Rules:

- Retrieval is enrichment, not a completion gate. If the API fails, times out, or returns few candidates, continue with the paper evidence and lower novelty confidence.

- Do not claim the paper is not novel solely because related papers exist.
- Explain precise overlap and precise difference.
- Separate "needs citation/differentiation" from "needs new baseline."
- Keep low-confidence concerns out of the final review unless marked as speculative.
- Use `scripts/match_missing_citations.py` only to annotate cited, possibly cited, not cited, or target-paper identity and its match evidence. Let the active LLM resolve uncertain matches and decide whether an uncited top-10 paper warrants citation, differentiation, a baseline, a limitation discussion, or no action.

For deterministic citation auditing, save the reranked structure as JSON with candidate identifiers preserved, then run:

```bash
python scripts/match_missing_citations.py \
  --candidates top10.json \
  --references references.json \
  --target-title "Target paper title" \
  --output citation-audit.json
```
