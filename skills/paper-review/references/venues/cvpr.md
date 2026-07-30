# CVPR Profile

Status: official-site-anchored, evidence-labelled contract for author-facing pre-submission review.

Verification snapshot: 2026-08-06 (Asia/Shanghai).

## Official Sources

- CVPR 2026 Reviewer Guidelines: https://cvpr.thecvf.com/Conferences/2026/ReviewerGuidelines
- CVPR 2026 Reviewer Training Material: https://cvpr.thecvf.com/Conferences/2026/ReviewerTrainingMaterial
- CVPR 2026 Author Guidelines: https://cvpr.thecvf.com/Conferences/2026/AuthorGuidelines
- CVPR 2025 Reviewer Guidelines: https://cvpr.thecvf.com/Conferences/2025/ReviewerGuidelines
- CVPR 2026 OpenReview venue: https://openreview.net/group?id=thecvf.com/CVPR/2026/Conference

## Historical Public Form Evidence

The CVPR website publishes detailed criteria and review-writing guidance but does not expose the private main-conference review invitation or all of its field options. The newest publicly inspectable author export located during verification is a CVPR 2025 OpenReview record:

- Public CVPR 2025 OpenReview author export: https://cseweb.ucsd.edu/~jmcauley/reviews/cvpr25.pdf

That export confirms the seven 2025 field labels and order used below, including `3: borderline`, `2: weak reject`, and `3: Moderate Confidence`. It is historical form evidence, not an official normative guideline and not evidence that any individual review is exemplary.

## Evidence Boundary

- The official 2026 training page requires a concise summary, strengths, weaknesses, constructive suggestions, and a justified recommendation. Its final-justification examples use five labels: `Strong Accept`, `Weak Accept`, `Borderline`, `Weak Reject`, and `Reject`.
- The official public pages do not print a verbatim 2026 private form schema or numeric encoding. The 2026 operational contract therefore combines those official content requirements with the latest publicly observable CVPR field layout from 2025. It must not be described as a verbatim copy of the private 2026 form.
- The five qualitative recommendation labels are the authoritative 2026 representation. Numeric `5–1` values are a historical OpenReview encoding used only when a numeric simulation is required.
- The confidence range is `1–5`; exact 2026 option prose is not publicly printed. Do not invent official anchor wording.
- CVPR's author-facing data-asset and ethics checks should be preserved in the most relevant critique field when the exact private form label is unavailable.

## Scope And Version Selection

- This profile covers the CVPR main conference, not workshops, tutorials, challenges, doctoral consortia, or Findings-style workshop tracks.
- Use the 2025 verified public field contract for CVPR 2025.
- Use the 2026 official substantive guidance plus the clearly labelled historical-form bridge for CVPR 2026.
- If no year is supplied, use CVPR 2026 and state the evidence boundary in `Target Venue`.
- Reverify later years from that year's official pages and public form evidence. Do not infer that a numeric scale changed merely because wording changed.
- Never use the generic fallback form or generic scores for CVPR.

## CVPR 2025 Review Contract

The latest publicly inspectable CVPR 2025 OpenReview export exposes these substantive fields in this order:

1. `Paper Summary`
2. `Paper Strengths`
3. `Major Weaknesses`
4. `Minor Weaknesses`
5. `Overall Recommendation`
6. `Justification For Recommendation And Suggestions For Rebuttal`
7. `Confidence Level`

The `Notes` acknowledgement seen in the form is administrative and is not rendered.

## CVPR 2026 Operational Review Contract

Until a public 2026 form is verifiable, render the official 2026 content requirements through the latest known CVPR field layout:

1. `Paper Summary`
2. `Paper Strengths`
3. `Major Weaknesses`
4. `Minor Weaknesses`
5. `Overall Recommendation`
6. `Justification For Recommendation And Suggestions For Rebuttal`
7. `Confidence Level`

This is an operational compatibility contract, not a claim about unseen private-form metadata. Put constructive suggestions next to the weakness they address or in the justification field; do not add an invented official field.

## Field Guidance

### Paper Summary

- Use approximately 2–4 sentences, as recommended by the 2026 training material.
- Identify the problem, approach, principal contribution, and central claimed result in the evaluator's own words.
- Do not critique the paper or copy the abstract here. If the work cannot be summarized accurately, reread it before scoring.

### Paper Strengths

Name specific, evidence-backed merits: technical insight, conceptual contribution, sound methodology, useful empirical findings, rigorous analysis, practical value, strong presentation, or resource value. Point to paper sections, equations, tables, or figures when helpful.

### Major Weaknesses

Include only concerns that materially affect correctness, novelty, significance, evidence, reproducibility, ethics, or the overall recommendation. Each concern should contain:

- the claim or decision criterion affected;
- the paper evidence supporting the concern;
- why the concern matters;
- a proportionate way to clarify, test, or repair it.

A `done before` claim must name relevant work and explain the relationship. Do not treat a preference for a different method as a technical flaw.

### Minor Weaknesses

Use for localized clarity issues, missing non-core details, figure legibility, limited typos, or small additions that do not determine acceptance. State `None identified` rather than promoting minor points into major concerns.

### Overall Recommendation

Choose one of the five qualitative labels. The recommendation must reflect the balance of the documented strengths and weaknesses, not a private acceptance-rate quota or state-of-the-art table alone.

### Justification For Recommendation And Suggestions For Rebuttal

- Explain why the evidence leads to the selected label.
- Distinguish concerns that an author response can clarify from work requiring a later revision.
- For each rebuttal question, state what answer would change the assessment.
- After rebuttal, say which concerns were or were not resolved and why any rating changed.
- Do not demand substantial new experiments during rebuttal; small, decision-relevant checks are acceptable.

### Confidence Level

Use `1–5`, where larger values mean greater subject-matter familiarity and more complete checking. The public 2025 form confirms `3` as moderate confidence. Do not manufacture exact 2026 prose for the other anchors. Lower confidence when central equations, experiments, supplementary evidence, or the relevant literature could not be assessed.

## Overall Recommendation — CVPR 2025 And 2026

Use these five labels:

- `5 — Strong Accept`;
- `4 — Weak Accept`;
- `3 — Borderline`;
- `2 — Weak Reject`;
- `1 — Reject`.

For 2026, prefer the label without a number because the public official pages show the labels but not the private numeric encoding. If a numeric output is required, label it as historical-form-compatible rather than official 2026 form metadata.

The old six-point split between `Borderline Accept` and `Borderline Reject` is **not** supported by the CVPR 2026 official training material and must not be used.

## Core Review Criteria

- technical correctness and completeness;
- meaningful contribution to computer vision;
- novelty, insight, and potential impact;
- experimental design and whether the evidence supports the claims;
- appropriate baselines, datasets, metrics, statistics, and ablations;
- clarity and reproducibility;
- responsible data use, attribution, privacy, consent, and human-subject treatment;
- limitations, foreseeable negative impact, and misuse risk.

CVPR does not require every paper to win a state-of-the-art benchmark. Methodological, empirical, analytical, systems, dataset, and conceptual contributions can all be valuable when their claims are sound and meaningful.

## Data, Evaluation, And Reproducibility Checks

- Quantitative and qualitative evidence must match the paper's stated claims; attractive examples cannot replace representative evaluation.
- Baselines and metrics should be appropriate to the task, data, and intended use. Requests to reimplement closed-source work need special justification and should be decision-relevant.
- Recent arXiv work need not be exhaustively compared, and failure to beat or cite it cannot be the sole reason for rejection. Suspected plagiarism is escalated separately.
- Private or restricted datasets are not an automatic rejection reason. They cannot be claimed as a publicly enabling dataset contribution, and the remaining technical contribution must be assessable.
- If a dataset is a contribution, there should be a reasonable expectation of public availability upon publication.
- Check data provenance, licenses, citations, withdrawn or deprecated assets, consent, personally identifiable information, demographic representation, and human-subject oversight where applicable.
- Reproducibility review should cover preprocessing, splits, leakage controls, hyperparameters, compute, random seeds, statistical variation, evaluation protocol, and artifact availability in proportion to the claims.

## Review-Process And Policy Rules

- Suspected formatting, anonymity, ethics, dual-submission, or prompt-injection violations should be reported to the area or program chairs; the scientific review should proceed as if the alleged violation were not yet established.
- Official CVPR 2026 reviewers may not use an LLM to generate review content, summarize or analyze confidential paper content, or translate the paper or review. They may use general background research and limited short-phrase clarity or grammar assistance without sharing substantial confidential content.
- This Omni profile is for authors evaluating their own paper before submission. It must never be presented as permission to use Omni for an official confidential CVPR review.

## Calibration Rules

- Evaluate each paper on its own merits; do not impose an assumed acceptance rate on a review stack.
- Do not reject solely for lack of state-of-the-art performance.
- Minor, readily correctable flaws must not determine rejection.
- Keep novelty and quality separate: a novel idea can be unsound, and a careful incremental result can be valuable.
- A recommendation must be evidence-based and consistent with the listed strengths and weaknesses.
- Rebuttal requests must be realistic within the response window and tied to the decision.
- Use a professional, depersonalized tone: refer to `the paper` or `the work`, not to `you` or personal characteristics of the authors.
