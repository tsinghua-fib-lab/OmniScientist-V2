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
          input={"resume_token": "<token>", "approved_plan": <plan>})
```

Do not pass a new `topic`, source, or `review_mode` with the resume token; that
can start another review cycle. Apply requested edits to `approved_plan`.
Automated workflows must use `review_mode="none"`, because they cannot collect
approval between steps.

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

Template assets persisted in a review plan must remain in `approved_plan` so a
cross-process resume keeps the selected branding.
