# Omni execution guide

Read this reference when OmniScientist is executing `research-pptx`. The shared
`SKILL.md` intentionally keeps host-specific calls out of the primary context.

## Background generation

Full generation can take several minutes. Submit it once in the background,
tell the user it is running, and do not poll in a loop:

```text
run_skill(skill_name="research-pptx", mode="background",
          input={"topic": "<ask>", "pdf_uri": "/path/paper.pdf",
                 "language": "en", "talk_type": "conference"})
```

The skill parses a PDF itself. Pass `pdf_uri` verbatim; do not read, copy, or
convert the binary with agent shell tools. A missing path returns a structured
error.

## Review and resume

Use `review_mode="plan"` only when the user explicitly asks to approve an
outline. The first call returns `status="partial"`,
`outcome.code="awaiting_review"`, a plan, and a `resume_token`. Show the plan and
wait. After approval, resume once:

```text
run_skill(skill_name="research-pptx", mode="foreground",
          input={"resume_token": "<token>"})
```

Do not pass a new `topic`, source, or `review_mode` with the resume token; that
can start another review cycle. Use `approved_plan` only for a complete plan
replacement. For surgical changes, pass ordered `plan_edits`; every index refers
to the plan produced by the preceding operation:

```json
[
  {"action": "set_title", "slide_index": 2, "title": "New title"},
  {"action": "set_bullets", "slide_index": 3, "bullets": ["A", "B"]},
  {"action": "set_bullet", "slide_index": 3, "bullet_index": 0, "text": "A2"},
  {"action": "add_slide", "after_index": 3,
   "slide": {"slide_type": "content", "title": "New", "bullets": ["Point"]}},
  {"action": "remove_slide", "slide_index": 4},
  {"action": "set_figure", "slide_index": 5, "figure_path": "figure_2"},
  {"action": "set_type", "slide_index": 5, "slide_type": "content_figure"},
  {"action": "swap_slides", "index_a": 2, "index_b": 5},
  {"action": "move_slide", "from_index": 5, "to_index": 2}
]
```

Outline numbering is one-based while edit indices are zero-based. To merge two
slides, first update the destination bullets and then remove the source slide.
Automated workflows must use `review_mode="none"`, because they cannot collect
approval between steps.

Automatic resume without an explicit token is only attempted when the current
session has exactly one active checkpoint. Cross-session, stale, consumed, and
ambiguous checkpoints require the explicit token returned above.

## Multi-paper and corpus sources

```text
run_skill(skill_name="research-pptx", mode="background",
          input={"topic": "...", "paper_uris": ["/p1.pdf", "/p2.pdf"]})

run_skill(skill_name="research-pptx", mode="background",
          input={"topic": "...", "source_ids": ["<sid1>", "<sid2>"]})

run_skill(skill_name="research-pptx", mode="background",
          input={"topic": "...", "corpus_query": "attention long context"})
```

`source_ids` and `corpus_query` are Omni-only because they use the local
research store. Portable hosts should use `paper_uris`, `pdf_uri`,
`reference_text`, `outline`, or `markdown_uri`.

When the source is pasted text or prior conversation, synthesize the complete
relevant material into `reference_text`; do not pass only “based on our
discussion” as `topic` with no evidence source.

Template assets persisted in a review plan must remain in `approved_plan` so a
cross-process resume keeps the selected branding.

## Parameters and telemetry

New decks accept `topic`, source fields (`pdf_uri`, `paper_uris`,
`reference_text`, `outline`, `markdown_uri`, `source_ids`, `corpus_query`),
`language`, `talk_type`, `duration_minutes`, `target_slides`, `color_theme`,
`mode`, `review_mode`, and `template_uri`. Review continuation accepts
`resume_token` plus optional `plan_edits` or a complete `approved_plan`.

Runs append audit, stage, and decision JSONL rows under
`<workspace>/artifacts/telemetry/research_pptx.jsonl`. Rows carry session/task
identity so review, rendering, QA, and delivery remain joinable offline.
