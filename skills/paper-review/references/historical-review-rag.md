# Historical-review RAG

This default-on layer lets the formal-review correction pass and the second-stage
revision planner see concerns that reviewers raised for semantically similar
papers. It is calibration and author-guidance memory, not current-paper evidence,
an official venue rubric, or a score prior. A historical concern may change a
formal finding or score only after the current manuscript or other current-paper
evidence independently verifies it.

## Build the FAISS index

For the production ICLR corpus, use the local SPECTER2 proximity adapter. Point
Omni at the dedicated Python environment and local model files, then enable the
embedding runtime:

```bash
omni config embeddings --enable -p specter2 \
  --python <SPECTER2_ENV>/bin/python \
  --base-model <LOCAL_SPECTER2_BASE> \
  --adapter <LOCAL_PROXIMITY_ADAPTER> \
  --device cuda:0

PAPER_REVIEW_SKILL_DIR=/absolute/path/to/skills/paper-review
python3 "$PAPER_REVIEW_SKILL_DIR/scripts/build_review_index.py" \
  --manifest <MANIFEST_BODY_JSONL> \
  --index <REVIEW_FAISS_INDEX_DIRECTORY> \
  --data-root <DIRECTORY_CONTAINING_REFERENCED_DATASETS>
```

When paper-review reports a missing index, its `setup_command` contains the
resolved builder path for the installed Skill. The command can be run from any
working directory. `--rebuild` is reserved for intentionally replacing an
incompatible index owned by this Skill.

A portable host may instead pass `--embedding-base-url` and
`--embedding-model` for a real OpenAI-compatible embedding service. Its key is
read from `OMNI_EMBEDDING_API_KEY` by default, never from a command-line value.
This remains a vector build: it does not enable a text or keyword fallback.
The endpoint must be an `http` or `https` service root without embedded
credentials, a query string, or a fragment.

If `--data-root` is omitted, referenced paper and review paths must stay under
the manifest directory's parent. This prevents a supplied manifest from
silently reading and embedding unrelated files.

## Directory format

The `--index` value is an owned directory, not a database file:

```text
<REVIEW_FAISS_INDEX_DIRECTORY>/
├── index.json
└── generations/
    └── <generation-id>/
        ├── vectors.faiss
        ├── papers.jsonl
        └── reviews.pack
```

- `index.json` records ownership, the active immutable generation, embedding
  space, vector dimension, policies, counts, and integrity hashes.
- `vectors.faiss` is the only retrieval index. Paper vectors are normalized and
  searched with FAISS exact inner product, which is exact cosine similarity for
  those normalized vectors.
- `papers.jsonl` maps FAISS integer ids to paper metadata and byte ranges. It is
  a plain metadata file, not a search index or database.
- `reviews.pack` stores the bounded, compressed qualitative review packets.
  It is opened only after FAISS has selected the closest papers.

The builder writes a new generation and publishes it through `index.json` only
after all required files and hashes are ready. A reader therefore sees a
complete immutable generation. The builder rejects symbolic links and
unrelated existing destinations rather than overwriting them.

The index records a one-way `embedding_space_id`. Build and query must use the
same embedding space, model, and vector dimension. For local SPECTER2 this
identity covers the selected base model, proximity adapter, and embedding
policy; a different adapter is not silently accepted just because its display
name or dimension matches.

The ICLR 2026 manifest contract uses:

- `source_paper_id` and `title` for paper identity;
- `reviews_json_path` for the submission abstract and `official_reviews`;
- `paper_path` and `paper_sha256` for paper provenance;
- `review_paths[0]` plus `reviews_sha256` for aggregate review-file provenance;
- the actual SHA-256 of `reviews_json_path`, tracked separately inside the
  generation metadata.

Each paper receives one SPECTER2 embedding from its title and abstract. Full
paper bodies are deliberately not collapsed into one vector: they exceed the
model's useful paper-level representation and make similarity less stable.
The index stores only the official reviews' qualitative author-guidance fields,
with bounded decompression and integrity checks for the vector, mapping record,
and review packet.

## Use it during review

The Skill bundles a ready index at `resources/indexes/iclr2026-reviews` and
resolves it relative to `engine.py`, so source checkouts, wheels, and copied
Skill folders do not depend on the original corpus location or the user's
working directory. Pass `review_rag_index` only to override that snapshot;
relative overrides are resolved against the user's working directory.
`review_rag_top_k` defaults to five.

- `review_rag=on` is the default. It uses the bundled index and applies the
  concern memory to any target venue.
- `review_rag=auto` uses the bundled or overridden index for ICLR targets and
  leaves other venues unchanged; it is a compatibility mode, not the default.
- `review_rag=off` is an explicit user opt-out. Do not select it merely to make
  a run faster or because the target venue is not ICLR.

The bundled metadata and paper map contain no source-machine absolute paths.
Local SPECTER2 identity is content-addressed: identical base/tokenizer and
proximity-adapter assets retain the same identity when installed at another
path, while different weights are rejected. The other machine still needs
`faiss-cpu` and those matching SPECTER2 assets configured in Omni. If the
bundled index is missing, an override is absent, or either runtime dependency
is unavailable, the engine reports the exact issue and completes the review as
a marked partial result.

FAISS embedding retrieval is the only retrieval path. If FAISS is unavailable,
the local SPECTER2 runtime cannot start, the query embedding fails, the index is
damaged, or its embedding space is incompatible, the engine reports the exact
problem and does not inject historical reviews. It never substitutes FTS,
keyword overlap, or another lower-precision ranking method. The main paper
review can still finish as a clearly marked partial result, with a setup or
repair action when possible.

If the prompt budget cannot include every retrieved packet, the public RAG
result reports how many packets were included and omitted and marks the
historical-memory layer partial; the formal review and the revision plan still
complete with the evidence that is available.

## Evidence boundary

The pipeline keeps a current-evidence anchor while allowing memory correction:

1. Generate the initial formal-review draft from the current manuscript,
   manuscript understanding, visual evidence, and Semantic Scholar evidence.
   Historical text is absent from this initial generation, Paper Summary, and
   structural-only repair.
2. Supply redacted historical packets to the existing evidence-focused
   refinements for weaknesses, author feedback, responsible-review fields, and
   venue scores. The model may correct an omitted, overstated, or understated
   issue only when it identifies independent support in the current manuscript,
   visual evidence, or Semantic Scholar records. Similarity alone cannot change
   a field or score.
3. Generate `Detailed Revision Plan` from the original manuscript, the
   memory-corrected formal review, structured manuscript understanding, selected
   visual evidence, Semantic Scholar evidence, and the redacted historical
   packets that fit the bounded context.

The closest matched paper packets may contribute whole qualitative fields,
including summary, strengths, weaknesses, questions, and concrete ethics
comments, to formal-review correction and revision planning. Historical numeric ratings, confidence, decisions,
ethics-review flags, structured reviewer identifiers, explicit score or
acceptance statements, and self-disclosed reviewer identity are removed. Raw
historical review text is withheld from Paper Summary and structural-only
repairs. It may prompt a verified correction to critique and score fields, but
it cannot itself become current-paper evidence or a score prior.

Historical paper titles, abstracts, URLs, and citation identifiers are also
withheld from model-facing concern packets. Similar-paper titles may remain in
public retrieval metadata for operator transparency, but only independently
retrieved Semantic Scholar records may support related-work or novelty claims
in the generated review.

Historical reviews answer: “For a similar kind of work, what checks and evidence
did reviewers commonly expect, and did the current draft underweight or
overweight any verified issue?” They do not prove that the current paper has the
same problem. A historical concern may enter the formal review or revision plan
only after the model verifies it in the original manuscript. For
each actionable item, put priority and a short title in the numbered lead and
display only `Review concern`, `Paper location`, and `Required change`. The last
field must cohesively explain the scientific or acceptance impact, concrete
edits or execution steps, new experiment, analysis, comparison, citation
discussion, artifact, or explanation, an observable completion criterion, and
any material dependency or trade-off. Generate the complete `Required change`
directly rather than producing separate hidden detail fields for later merging.

The plan also consolidates detailed experimental and statistical work,
related-work positioning, figures/tables/equations, prose and typographical
corrections, and final consistency and claim-validation checks. Do not impose a
word-count, character-count, item-count, or per-category quota. Historical
reviews may improve the checklist, but their study settings are not facts about
the current paper; present any borrowed design idea as a proposal that the
authors still need to verify or choose.

Semantic Scholar remains the source for novelty and related-work evidence.
Historical-review RAG is never presented as prior art or an official venue
rubric; it is used for evidence-checked formal-review correction and for the
later revision plan.
