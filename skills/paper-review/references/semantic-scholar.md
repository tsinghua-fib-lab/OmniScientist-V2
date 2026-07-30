# Semantic Scholar Usage

Use Semantic Scholar automatically by default for novelty verification and missing-citation checks.

## API Key

- Read `SEMANTIC_SCHOLAR_API_KEY` from the environment only.
- Send it as the `x-api-key` header.
- If missing, continue unauthenticated when possible.
- Never write the key into code, config, logs, generated docs, or review files.
- If a key appears in conversation or logs, treat it as exposed and recommend rotation.

## Query Inputs

Have the active LLM build search queries from:

- title
- abstract
- systematic paper analysis
- the 3 research questions

Pass the finished queries verbatim to the helper. The helper validates only structural limits; it does not remove stopwords, identify method sentences, generate queries, or rank scholarly relevance.

## Retrieval Fields

Request fields when available:

```text
paperId,title,abstract,authors,year,venue,url,externalIds,citationCount,influentialCitationCount,fieldsOfStudy
```

## Rate Limits

Respect roughly 1 request per second unless the user's key has higher approved limits. Prefer fewer broader searches plus LLM reranking over many narrow searches.

For this skill, Semantic Scholar is the sole external literature source. Make one helper invocation containing the active LLM's multiple queries, then stop retrieval. Do not substitute OpenAlex, Crossref, web search, search-corpus tools, or citation-graph expansion.

If rate-limited:

- back off;
- keep already collected candidates;
- mark retrieval as limited if fewer than 5 candidates remain after deduplication.

## Helper Interface

Call the helper with each active-LLM query:

```bash
python scripts/semantic_scholar_search.py \
  --query "first literature query" \
  --query "second literature query" \
  --output candidates.json
```

The helper retrieves, balances candidates across queries, and deduplicates by stable identifiers and normalized titles. It deliberately does not produce a top 10; perform that semantic reranking with `llm-rerank-candidates.md`.

## Failure Behavior

If retrieval fails, continue the review with lower confidence in novelty and citation coverage. Record the limitation internally. In the final review, state only review-scope limitations that are useful to the author, without naming helper failures or implementation details.
