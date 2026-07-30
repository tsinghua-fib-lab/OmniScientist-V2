# ACL / ARR Profile

Status: current official ARR-form contract for author-facing pre-submission review.

Verification snapshot: 2026-08-06 (Asia/Shanghai).

## Official Sources

- ARR Review Form: https://aclrollingreview.org/reviewform
- ARR Reviewer Guidelines: https://aclrollingreview.org/reviewerguidelines
- ARR Reviewing Process: https://aclrollingreview.org/reviewing
- ARR Dates And Venues: https://aclrollingreview.org/dates
- ARR Responsible NLP Checklist: https://aclrollingreview.org/responsibleNLPresearch
- ARR Changelog: https://aclrollingreview.org/changelog
- ACL 2026 Main Conference Call For Papers: https://2026.aclweb.org/calls/main_conference_papers/
- ACL Code Of Ethics: https://www.aclweb.org/portal/content/acl-code-ethics

## Evidence Boundary

- The public ARR review-form page exposes the current substantive fields, field prompts, score options, and private author-identity questions. No reconstructed third-party form is needed.
- ARR cycles and conference commitment are separate. A conference year does not by itself identify which version of the ARR form governed the review.
- The public page can change between cycles. This profile records the form visible on the verification date; a known cycle should be checked against the corresponding ARR changelog and official instructions.
- Author-identity conjecture is reviewer-private and is never rendered in this pre-submission review.
- Conference tracks not routed through ARR need their own official review instructions.

## Scope And Version Selection

- Use this profile for papers reviewed through ACL Rolling Review and later committed to an ACL-family venue.
- Keep `ARR cycle`, `conference`, `conference year`, `track`, `paper type`, and `long/short format` distinct in `Target Venue`.
- If the ARR cycle is unknown, use the current public form and state `current form verified 2026-08-06`.
- The same form can evaluate empirical, theoretical, resource, survey, position, long, and short papers, but Soundness and contribution expectations must be interpreted for the declared paper type.
- Direct conference tracks, workshops, demos, journals, and tracks with separate calls must not inherit this form automatically.
- Never use the generic fallback form or generic scores for an ARR-routed submission.

## Current Public ARR Review Contract

The current public ARR form defines these substantive fields in this order:

1. `Paper Summary`
2. `Summary Of Strengths`
3. `Summary Of Weaknesses`
4. `Comments Suggestions And Typos`
5. `Reviewer Confidence`
6. `Soundness`
7. `Excitement`
8. `Overall Assessment`
9. `Best Paper Justification`
10. `Limitations And Societal Impact`
11. `Ethical Concerns`
12. `Needs Ethics Review`
13. `Reproducibility`
14. `Datasets`
15. `Software`

Knowledge or conjecture about author identity and the optional identity guess are reviewer-private fields and are not rendered.

Use `N/A` for `Best Paper Justification` unless `Overall Assessment` is `4.5` or `5`. Use `N/A` for `Datasets` or `Software` when the paper does not claim that artifact as a contribution.

## Omni Output Mapping

The official form includes `Comments Suggestions And Typos`. Omni's author-facing document does not render that field as a standalone Stage 1 heading. It preserves the remaining formal-review fields in official relative order and appends `Detailed Revision Plan` after the formal review.

The revision plan absorbs the content that would otherwise appear in `Comments Suggestions And Typos`, including:

- actionable scientific and experimental improvements;
- related-work positioning;
- statistical, evaluation, and reproducibility checks;
- figure, table, equation, and notation corrections;
- prose, organization, and typographical fixes;
- final verification steps.

Each plan item should state priority and title, the review concern, paper location, and required change. The required change should explain the rationale, concrete edit or execution step, new evidence or analysis, observable completion criterion, and material dependencies. This mapping is an author-aid design choice, not an alternative ARR form.

## Field Guidance

### Paper Summary

Describe the topic, problem, approach, main claims, and contributions accurately enough that action editors and area chairs can identify misunderstandings. Use the evaluator's own words and keep criticism out of this field.

### Summary Of Strengths

Explain the major reasons the work may be worth publishing at a selective ACL-family venue. Relevant strengths include useful methodology, insightful empirical findings, sound theory, valuable resources, clear synthesis of literature, improved access, negative results, or benefits to a particular language or community.

### Summary Of Weaknesses

- Explain concerns that would prioritize other strong papers over this one.
- Number distinct concerns so authors can respond individually.
- Separate correctness and evidential problems from limited excitement or presentation issues.
- State how each major concern affects claims, Soundness, Excitement, or Overall Assessment.
- A non-novelty claim must cite specific related work and explain the overlap.

### Comments Suggestions And Typos

Use for improvements beyond the decision-relevant weaknesses. In Omni output, route these points to the final revision plan instead of an extra formal heading.

### Reviewer Confidence

Use the official integer scale:

- `5` — positive the evaluation is correct; read very carefully and familiar with related work;
- `4` — quite sure; important points checked and unlikely to have missed a rating-changing issue;
- `3` — pretty sure, but details such as mathematics or experimental design were not checked completely;
- `2` — willing to defend, but likely to have missed details, central points, or novelty context;
- `1` — outside the area or the paper is very hard to understand; an educated guess.

Confidence is about evaluator expertise and checking depth, not paper quality.

### Soundness

Judge whether the work is sufficiently sound and thorough for its declared length and type, whether claims are explicit, and whether evidence supports them.

- Experimental papers: research-question depth or breadth, experimental design, methodological validity, statistics, controls, and claim support.
- Theory papers: assumptions, definitions, proof correctness, completeness, and claim scope.
- Position papers and surveys: faithful representation of the field and serious engagement with counterarguments.
- Resource papers: collection methodology, data quality, documentation, and distinction from existing resources.

### Excitement

Judge the subjective but reasoned value of the work: conceptual breakthroughs, surprising or assumption-challenging evidence, usefulness to a community, cost or access reduction, enabling applications, or relevance to future research. Excitement does not simply mean popularity and is orthogonal to Soundness.

### Overall Assessment

Judge likely disposition if committed to an ACL-family conference. Findings primarily requires Soundness and Reproducibility; main-conference recommendations may additionally weigh novelty, impact, and Excitement. Explain how the paper crosses or misses the selected boundary.

### Best Paper Justification

Complete only for `4.5 Borderline Award` or `5 Consider For Award`. Explain why the work could be fascinating, controversial, surprising, impressive, or field-changing rather than merely very good.

### Limitations And Societal Impact

Assess whether the paper covers limitations and both positive and negative impact. Reward candid disclosure. Consider excluded user groups, overgeneralization, unequal impact on marginalized populations, language or approach underexposure, dual use, beneficiaries, failure modes, and who may be harmed.

### Ethical Concerns And Needs Ethics Review

Describe issues under the ACL Code of Ethics and Responsible NLP checklist, including human participants, consent, privacy, annotation labor, compensation, sensitive attributes, bias, data rights, dual use, environmental cost, and overclaiming. Request specialist ethics review only for difficult issues needing that expertise, and explain why. Do not present an allegation as a settled finding.

### Reproducibility

Judge whether a reader can reproduce the main results, use them as a baseline, or build on the work. Check models, data, preprocessing, prompts, annotators or evaluators, code, hyperparameters, compute, randomness, splits, metrics, statistics, and access restrictions as applicable.

### Datasets And Software

Score only when release is claimed. Judge utility beyond reproducing the current paper, documentation, licensing, access, maintenance, usability, and whether the artifact enables new work. `1` means no usable artifact; `2` is still a positive documentary contribution when it supports replication.

## Official Scoring

### Soundness

Half-points are allowed:

- `5 — Excellent`: exceptionally thorough for its type;
- `4 — Strong`: all claims sufficiently supported; extra work would be useful but not essential;
- `3 — Acceptable`: main claims supported, with minor support or detail gaps;
- `2 — Poor`: some main claims lack support or major methodological problems exist;
- `1 — Major Issues`: not sufficiently thorough for publication or not relevant to ACL.

### Excitement

Half-points are allowed:

- `5 — Highly Exciting`;
- `4 — Exciting`;
- `3 — Interesting`;
- `2 — Potentially Interesting`;
- `1 — Not Exciting`.

### Overall Assessment

Half-points encode meaningful boundaries:

- `5 — Consider For Award`;
- `4.5 — Borderline Award`;
- `4 — Conference`;
- `3.5 — Borderline Conference`;
- `3 — Findings`;
- `2.5 — Borderline Findings`;
- `2 — Resubmit Next Cycle`;
- `1.5 — Resubmit After Next Cycle`;
- `1 — Do Not Resubmit`.

### Reproducibility

- `5` easily reproducible;
- `4` mostly reproducible with minor variation;
- `3` reproducible with some difficulty or underspecified choices;
- `2` very difficult because of inaccessible data or missing details;
- `1` impossible from the provided information.

### Datasets And Software

For each applicable artifact:

- `5 — Enabling`;
- `4 — Useful`;
- `3 — Potentially Useful`;
- `2 — Documentary`;
- `1 — No Usable Artifact`.

There is no separate novelty score. Novelty and impact inform `Excitement` and `Overall Assessment`.

## NLP-Specific Method Checks

- Are claims about languages, dialects, populations, domains, tasks, or modalities supported by the sampled data?
- Are dataset construction, filtering, annotation, adjudication, quality control, demographic coverage, licenses, and contamination addressed?
- Are automatic metrics valid for the claimed construct, and are metric limitations discussed?
- For human evaluation, are recruitment, instructions, expertise, blinding, sample size, agreement, uncertainty, and statistical tests adequate?
- For LLM-as-judge evaluation, are judge model/version, prompts, ordering, calibration, bias, leakage, variance, human validation, and reproducibility addressed?
- Are baselines comparable in data, compute, tuning, prompting, and access to external resources?
- Do significance tests, confidence intervals, multiple-run variation, and effect sizes support the conclusions?
- Are formal arguments complete and their assumptions visible?
- Are qualitative examples sampled systematically rather than selected only to support the claim?

## Related Work, Appendices, And Resubmissions

- Do an initial assessment before external related-work search. Search may refine novelty assessment, but must not be used to identify anonymous authors.
- A `not novel` claim requires specific references and an explanation of equivalence or overlap.
- Negative, simple, or non-state-of-the-art results may still be sound and valuable.
- The main paper must stand on its own for novelty, claims, implications, related work, and correctness. Reviewers are not required to inspect appendices.
- For resubmissions, reviewers may be assigned `Repeat`, `Substitute`, or `Fresh` roles. Evaluate the revision notes and earlier concerns according to the assigned role.
- Do not create an endless sequence of new concerns after authors address prior reviews, except for genuinely critical Soundness issues.

## Generative-AI And Confidentiality Boundary

ARR does not permit generative AI to create an official review's first draft. Any allowed assistance must preserve confidentiality and follow current ARR policy; confidential paper content must not be uploaded to an external model. Suspected prompt injection should be reported through the review process.

This Omni profile is for authors evaluating their own paper. It is not authorization to process a confidential ARR assignment with Omni or another external model.

## Calibration Rules

- Distinguish Findings-level Soundness and Reproducibility from main-conference novelty, impact, and Excitement.
- Do not lower a score merely because authors disclosed a limitation honestly.
- Treat broad unsupported generalization as a claim-scope issue and specify the evidence needed.
- Do not equate reviewer taste or topic popularity with Excitement for the whole ACL community.
- Keep Confidence independent of recommendation direction.
- Tie every score to concrete review text and make major concerns individually answerable.
- Use a specific, constructive, professional tone even when recommending resubmission or rejection.
