# Review-preference RAG

This default-available layer uses anonymous Arena battles to correct the formal-review
draft and improve how Stage 2 turns verified concerns into useful revision
advice. It is separate from historical-review memory:

- historical-review memory suggests what reviewers commonly check in similar
  work, subject to verification against the current manuscript;
- review-preference memory demonstrates which of two complete reviews people
  found more helpful for the same paper and can expose dimensions that the
  current formal-review draft should recheck.

Neither layer is evidence about the current paper. Preference memory may suggest
that a concern, severity judgment, or score rationale should be rechecked, but
the formal review or score may change only when current-paper evidence verifies
the correction. It cannot support a citation, decision, or factual claim by
itself.

## Source records

Join the Arena files through `query_id`:

1. Read each battle from `reviewer_results.json`.
2. Resolve each A/B side independently. If `agent_key` is
   `reviewer_human`, or the agent name is `Reviewer human`, collect
   `human_response` values for that `query_id` from
   `queries_human_responses.json`. Otherwise collect `answer` values by
   `(query_id, agent_key)` from `paper_review_answers.jsonl`.
3. Locate the paper Markdown through an answer record's `file_name`: remove a
   leading `pdf\` or `pdf_md\`, replace a final `.pdf` with `.md`, and keep an
   existing `.md` suffix.

Treat every lookup as one-to-many. Use a battle only when both sides can be
matched unambiguously to the exact complete answers used in that battle. If
the source data does not provide enough information to choose among multiple
answers, skip the battle and report it as ambiguous; never select the first
record or guess by text similarity.

## Normalize battles

Hash each complete resolved answer and aggregate records by:

```text
(query_id, min(answer_hash_a, answer_hash_b), max(answer_hash_a, answer_hash_b))
```

Map every A/B vote back to this canonical hash order before aggregation, so a
duplicate battle with reversed sides does not reverse its meaning. Keep only a
strictly preferred complete answer paired with its complete less-preferred
answer. `Tie` and `BothBad` may remain in build statistics, but they are not
positive preference examples. Do not publish a pair when decisive votes do not
identify a unique preferred answer.

Remove structured agent, paper, query, battle, and vote-count metadata from
model-facing packets. Keep internal hashes only for deduplication, provenance
checks, and retrieval mapping. Because each answer is intentionally kept
complete, its prose may itself mention the source paper or method; treat those
mentions as untrusted examples and never copy or expose them in the generated
review.

## Build and retrieve

Create one normalized SPECTER2 vector per paper and store those vectors in a
dedicated FAISS index. Do not create review-level vectors or a database search
fallback. Map each paper vector to its bounded packet of complete
`preferred`/`less_preferred` review pairs; load those texts only after FAISS
selects similar papers.

At review time, query this index once with the current paper representation.
Pass a small context-budgeted set of complete pairs to the evidence-focused
formal-review refinements and to Stage 2. Keep this index and its packets
separate from the historical-review FAISS index: the two sources have different
roles and prompt instructions even when they share the same SPECTER2 runtime.

The Skill bundles the active snapshot at
`resources/indexes/review-arena-preferences` and resolves it relative to its
installed `engine.py`. `preference_rag=auto` uses this snapshot by default;
`preference_rag_index` is an optional override whose relative path is resolved
against the user's working directory, and `preference_rag=off` is the explicit
opt-out. The snapshot contains no source-machine absolute paths and shares the
content-addressed SPECTER2 space used by historical-review memory. Another
machine still needs `faiss-cpu` and matching local SPECTER2 assets to create the
query embedding.

## Formal-review correction boundary

The initial formal-review generation and Paper Summary remain grounded only in
the current paper, visual evidence, Semantic Scholar, and the venue contract.
During the later Stage 1 refinements, preference pairs can audit whether the
draft omitted, overstated, or understated a material issue and whether its score
rationale is consistent. They must not:

- change a finding or score without independent current-manuscript, visual, or
  Semantic Scholar support and a current-paper locator;
- act as current-paper facts, prior art, citations, transferable scores,
  acceptance signals, or venue decisions;
- copy historical numbers, model names, compute resources, experimental
  settings, deadlines, or paper-specific wording;
- expose agent identities, source-paper identifiers, or battle metadata in the
  generated review.

The preferred side is not automatically scientifically correct, and a source
rating or verdict is never copied to the current paper. The pairwise contrast
only prompts the model to recheck completeness, severity, specificity, and
score rationale; the current evidence decides whether any correction is made.

## Stage 2 use

Generate one `Detailed Revision Plan` from the completed formal review and the
available evidence. Preference RAG does not generate two candidate plans and
does not add a judge or reranking model. Within `Prioritized Revision Actions`,
render each action compactly with only `Review concern`, `Paper location`, and a
detailed `Required change`. Generate that last field directly as cohesive prose
covering the rationale, execution, validation, completion, and dependencies;
do not create separate hidden fields for later merging or show more labels.

If the preference index is absent, incompatible, damaged, ambiguous, or cannot
be queried, report the layer as unavailable and continue the normal review and
revision-plan workflow without preference examples. Do not fall back to
keywords or treat an unresolved Arena record as usable training context.
