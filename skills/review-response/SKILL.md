---
name: review-response
description: >-
  Draft, audit, or revise journal revision correspondence: point-by-point reviewer
  responses, rebuttals, revision cover letters, and LaTeX or marked-manuscript
  packages. Use for editor decisions, revision emails, reviewer comments,
  response-to-reviewers drafts, rebuttal letters, and revision correspondence.
  Do not use for pre-submission critique of a paper; use paper-review instead.
license: Apache-2.0
allowed-tools: [read_file, web_fetch, cite_source, write_file, package_artifact, attach_provenance]
metadata:
  helixforge:
    version: "1.0"
    dependencies: ["prompt-only"]
    tier: research
    role: task
    research_contract: portable_provenance_v1
    priority: 85
    kind: prompt_only
    delivery_mode: async_task
    capabilities: [review.response, writing.revision]
    deliverables: [response_letter, cover_letter, revision_package]
    execution:
      max_iterations: 12
      max_tool_calls: 24
      max_seconds: 600
      tool_limits:
        read_file: 8
        web_fetch: 3
    workflow:
      failure_policy: continue_with_partial
      allow_failed_dependencies: true
      failure_types: [missing_input, source_unavailable, tool_budget_exhausted, artifact_write_failed, provenance_write_failed]
    input_schema:
      type: object
      properties:
        input:
          type: string
          description: "editor letter, reviewer comments, author notes, or response draft"
          x-omni:
            semantic_role: instruction
        mode: {type: string, description: "draft, audit, revise, triage-only, cover-letter, revision-package, latex-template, or appeal-like"}
        journal: {type: string, description: "optional target journal or publisher"}
        output_format: {type: string, description: "optional Markdown, plain text, or LaTeX"}
      required: [input]
    output_schema:
      type: object
      properties:
        status: {type: string, enum: [ok, partial, error]}
        outcome: {type: object}
        text: {type: string}
        tracker: {type: array}
        readiness: {type: string}
        artifacts: {type: array}
        sources: {type: array}
        research: {type: object}
        summary: {type: string}
        warning: {type: string}
        recoverable: {type: boolean}
        blocking: {type: boolean}
        error: {type: string}
        error_info: {type: object}
      required: [status]
    trigger:
      phrases: ["\u5ba1\u7a3f\u610f\u89c1\u56de\u590d", "\u56de\u590d\u5ba1\u7a3f\u4eba", "\u8fd4\u4fee\u56de\u590d", "\u4fee\u6539\u7a3f\u56de\u590d", reviewer response, response to reviewers, rebuttal, revision cover letter, editor decision, major revision, minor revision]
      when_to_use: "Use for drafting, auditing, or revising a journal response to editors or reviewers, including a revision package or LaTeX output."
    notification:
      display_label: "Review response"
---

# review-response

Use this self-contained workflow to produce an auditable, professional journal revision
response. Do not look for `manifest.yaml`, `static/`, `references/`, or template files:
all required rules are included below.

## 1. Determine the task

Choose one mode:

- `draft`: create a new point-by-point response.
- `audit`: find omissions, unsupported claims, weak tone, or traceability gaps in an existing response.
- `revise`: rewrite a supplied response while preserving its facts and commitments.
- `triage-only`: create a strategy, tracker, risks, and missing-input list without drafting final prose.
- `cover-letter`: create only an editor-facing revision cover letter.
- `revision-package`: create the response plus cover letter and manuscript-change checklist.
- `latex-template`: create or fill a LaTeX response, cover letter, or redline structure.
- `appeal-like`: route an appeal separately; do not treat it as an ordinary revision response.

Identify the decision as minor revision, major revision, revise-and-resubmit, transfer after
review, or unclear. If a journal email is supplied, extract the manuscript title, ID, journal,
decision, editor instructions, reviewer boundaries, required files, deadline, and portal rules.

## 2. Parse and classify

Extract editor instructions first as `E.1`, `E.2`, etc. Preserve reviewer wording and assign
stable IDs such as `R1.1`, `R1.2`, `R2.1`. If boundaries or numbering are ambiguous, state the
ambiguity instead of inventing structure.

For every item record:

| Field | Required content |
|---|---|
| Type | presentation, method, evidence, analysis, statistics, data/code, citation, figure/table, compliance, or scope |
| Severity | minor, major, blocking, or unclear |
| Action | accept and revise, clarify existing work, add analysis, justify limitation, disagree with evidence, or `AUTHOR_INPUT_NEEDED` |
| Evidence | supplied result, manuscript location, citation, or missing fact |
| Readiness | ready, draft with placeholders, needs author input, or blocked |

Create a strategy summary before drafting prose. Map every claimed change to a section, page,
line, figure, table, supplement, citation, or explicit placeholder.

## 3. Draft the response

Use this sequence unless the user requests another format:

1. Thank the editor and reviewers briefly.
2. Address editor instructions before reviewer comments.
3. For each reviewer, show the full comment or a faithful excerpt, then answer immediately.
4. Acknowledge the concern before disagreeing; explain scientific or scope-based reasons.
5. State exactly what was changed, where it was changed, and what evidence supports it.
6. If a request cannot be answered, explain the limitation and propose the narrowest honest action.
7. Keep the cover letter shorter than the point-by-point response and do not duplicate every reply.

When the user writes in Chinese, draft the submission response in English and add a concise
Chinese author-notes block when useful. Preserve supplied Chinese notes as author intent, not as
verified manuscript facts.

## 4. Scientific and editorial guardrails

- Never invent experiments, data, statistical results, citations, line numbers, figure panels,
  supplementary items, editor instructions, reviewer identities, or manuscript changes.
- Never claim that a revision was completed unless the user supplied the result and location.
- Mark missing facts exactly as `AUTHOR_INPUT_NEEDED`; do not silently fill them.
- Do not ignore, merge away, or materially rephrase a reviewer concern.
- Treat reviewer misunderstandings as a possible presentation problem before attributing fault.
- Do not use hostile, accusatory, defensive, or convenience-based refusals.
- Do not hide limitations or label a package `ready_to_submit` while placeholders remain.
- For current venue requirements, use the target journal or publisher's official page and cite it;
  generic editorial advice is not binding journal policy.
- Route ethics, compliance, data-integrity, central-evidence, transfer, and appeal-like issues
  as risks or blockers rather than forcing a normal response.

## 5. Manuscript and LaTeX handling

Only use manuscript text and change details supplied by the author. If editing manuscript text,
work on a copy and mark changed passages in red. For LaTeX, use visible placeholders such as
`[AUTHOR_INPUT_NEEDED: exact line or section]`, `\textcolor{red}{...}` for changed text, and
`\newpage` between reviewer sections. Put quoted revised manuscript text in italics in a
plain-text/Markdown response and make placeholders visible in LaTeX.

## 6. Default output

Return the following sections unless the user requests a narrower format:

```text
Response strategy summary
- Decision type:
- Task mode:
- Overall posture:
- Major risks:
- Parsed email metadata:
- Suggested ordering:

Comment-response tracker
| ID | Reviewer concern | Type | Severity | Proposed action | Missing author input |
|---|---|---|---|---|---|

Draft point-by-point response letter
[English response with each comment preserved and answered]

Draft revision cover letter
[Only when requested or when returning a revision package]

Manuscript change checklist
- [specific change, location, or AUTHOR_INPUT_NEEDED]

Missing information / risk flags
- [unresolved facts, limitations, or None]

Readiness
- ready_to_submit / draft_with_placeholders / needs_author_input / blocked
```

## 7. File writing and provenance

Do not write files unless the user requests it. If writing is requested, call `write_file` only
with a concrete filename under an allowed workspace root; never use `.`, a directory, or an empty
path. If the path is unavailable, return the result in the conversation instead of retrying a
directory path.

When Omni tools are available, use real source and artifact identifiers. In other runtimes,
include a Markdown **Provenance** section with supplied document paths, journal URLs, citations,
and artifact paths. Never invent provenance IDs.

## OmniScientist completion contract

This is a `prompt_only` Skill and stays one: drafting a revision response is a
single reasoning pass, not a measurable multi-stage pipeline, so there are no
typed stage events to emit and a `python_engine` promotion would add a wrapper
with nothing to report. Its live progress under OmniScientist is simply the tool
calls it makes (`read_file`, `web_fetch`, `write_file`); its completion is read
from the returned result. Keep that result legible to the CLI's closing block:

- Deliverable. Always return a one-line `summary` (decision type, task mode, and
  what was produced) and any `artifacts` (response letter, cover letter, revision
  package, or LaTeX). The closing deliverable block reads these fields.
- Readiness is the completion state. Set `readiness` to exactly one of
  `ready_to_submit`, `draft_with_placeholders`, `needs_author_input`, or
  `blocked`. This is the Skill's completion vocabulary — do not report
  `ready_to_submit` while any `AUTHOR_INPUT_NEEDED` marker remains.
- Human input. `needs_author_input` / `blocked`, together with the
  `AUTHOR_INPUT_NEEDED` markers, are the signal that the operator must supply a
  missing fact before the response can be finished; surface them rather than
  inventing the fact.

## External agent portability

The skill works without Omni; Omni adds persistence, provenance, and task lifecycle support.

- Copy-only mode: copy this folder into a Claude Code, Codex, or OpenClaw skill
  directory. The host agent can follow this self-contained prompt with its
  normal reading, web, and writing tools.
- Portable runner mode: this prompt-only skill intentionally has no executable
  runner. Follow `SKILL.md` directly and save requested artifacts with the
  host's file tools.
- Omni enhanced mode: OmniScientist/HelixForge enforces the declared tool and
  execution budgets, schedules the response workflow, stores artifacts, and
  records provenance when those tools are available.

## Portable research provenance

This skill must remain portable across OmniScientist, Claude Code, Codex, and
OpenClaw.

- In OmniScientist, use available provenance tools and include their returned
  ids under `research.source_ids`, `research.claim_ids`,
  `research.evidence_ids`, `research.hypothesis_ids`, and/or `research.run_id`.
- In other runtimes, do not fail when those tools are absent. Include a
  Markdown **Provenance** section and, when writing files, record supplied
  document paths, official journal URLs, citations, and artifact paths.
- Never invent provenance ids, manuscript changes, or source metadata.
