# NeurIPS Profile

Status: official-site-derived, year-aware contract for author-facing pre-submission review.

Verification snapshot: 2026-08-06 (Asia/Shanghai).

## Official Sources

- NeurIPS 2026 Main Track Handbook, version V2026.3: https://neurips.cc/Conferences/2026/MainTrackHandbook
- NeurIPS 2026 Contribution-Type Reviewer Guidelines: https://neurips.cc/Conferences/2026/ReviewerGuidelines
- NeurIPS 2026 Call For Papers: https://neurips.cc/Conferences/2026/CallForPapers
- NeurIPS Paper Checklist: https://neurips.cc/public/guides/PaperChecklist
- NeurIPS 2025 Reviewer Guidelines: https://neurips.cc/Conferences/2025/ReviewerGuidelines
- NeurIPS Code Of Ethics: https://neurips.cc/public/EthicsGuidelines

## Evidence Boundary

- The 2026 handbook publishes the complete main-track review form, field descriptions, numeric anchors, ethical-concern categories, and administrative acknowledgements. No historical or third-party form reconstruction is needed for 2026.
- The 2025 official reviewer page publishes the prior form and is retained because the 2026 form added contribution-type confirmation and formatting concerns.
- Code-of-conduct and responsible-reviewing acknowledgements are administrative. They are recorded as process requirements but not rendered as scientific feedback in this author-facing simulation.
- Other NeurIPS tracks have separate calls and rubrics. Their forms must not be inferred from the main track.

## Scope And Version Selection

- This profile covers only the NeurIPS main research track.
- Use the 2025 contract for NeurIPS 2025 and the 2026 contract for NeurIPS 2026.
- If no year is supplied, use NeurIPS 2026 and state the assumption in `Target Venue`.
- Evaluations and Datasets, Position Papers, Competitions, Creative AI, reproducibility, workshops, and other tracks require their own official instructions.
- For a later year, consult that year's handbook. Do not translate or average scores from a previous form.
- Never use the generic fallback form or generic scores for a NeurIPS main-track paper.

## NeurIPS 2025 Review Contract

Render the substantive, author-facing fields in this order:

1. `Summary`
2. `Strengths And Weaknesses`
3. `Quality`
4. `Clarity`
5. `Significance`
6. `Originality`
7. `Questions`
8. `Limitations`
9. `Overall`
10. `Confidence`
11. `Ethical Concerns`

Code-of-conduct and responsible-reviewing acknowledgements are not rendered.

## NeurIPS 2026 Review Contract

Render the substantive, author-facing fields in this order:

1. `Summary`
2. `Contribution Type Confirmation`
3. `Strengths And Weaknesses`
4. `Quality`
5. `Clarity`
6. `Significance`
7. `Originality`
8. `Questions`
9. `Limitations`
10. `Overall`
11. `Confidence`
12. `Ethical Concerns`
13. `Paper Formatting Concerns`

Code-of-conduct and responsible-reviewing acknowledgements follow the official form but are administrative and are not rendered.

## Field Guidance

### Summary

Briefly state the paper's problem, approach, main claims, and contributions in the evaluator's own words. Do not critique the paper or copy its abstract. The authors should generally agree with an accurate summary.

### Contribution Type Confirmation — 2026 Only

Select exactly one type declared by the submission:

- `General`;
- `Theory`;
- `Use-Inspired`;
- `Concept & Feasibility`;
- `Negative Results`.

The official form is the same for all types, but the meanings of Quality, Clarity, Significance, and Originality differ. The contribution type cannot be changed after submission. For an author-facing draft without a declared type, infer the most plausible type, mark it as an assumption, and explain any ambiguity rather than silently evaluating against the General rubric.

### Strengths And Weaknesses

Provide a reasoned narrative, not an unconnected issue list. Cover Quality, Clarity, Significance, and Originality with evidence from the paper. Identify concrete strengths, distinguish central flaws from local improvements, and explain how each major concern affects the recommendation.

### Quality

Assess technical soundness, support for claims, suitability of methods, completeness, reproducibility, and honest treatment of strengths and weaknesses. The correct evidence depends on contribution type; for example, a theory contribution does not require empirical state-of-the-art results.

### Clarity

Assess organization, precision, accessibility to the intended NeurIPS audience, contextualization, and whether an expert can understand and reproduce the result. Domain-specific use-inspired work must explain necessary domain concepts without requiring every reader to be a domain expert.

### Significance

Assess whether the result changes understanding, capabilities, research practice, or a meaningful real-world use case. Impact may be broad or concentrated in an important subcommunity. Match the bar to the selected contribution type.

### Originality

Assess new insights, framings, tasks, metrics, methods, analyses, data, theory, or well-motivated combinations of existing ideas. Originality does not require a wholly new method. A prior-art objection must cite the relevant work and explain the overlap.

### Questions

- Ask ideally `3–5` decision-relevant questions.
- Make each question actionable and state what answer would raise, lower, or leave the score unchanged.
- If requesting an experiment, explain which uncertainty it resolves and account for limited rebuttal time and compute.
- Do not demand a substantially new paper during the response period.

### Limitations

If limitations and potential negative implications are addressed adequately, `Yes` is sufficient. Otherwise provide constructive missing points. Reward candid disclosure rather than using acknowledged limitations as automatic weaknesses.

### Overall

Use the official six-point recommendation scale. The score synthesizes the review and is not a mechanical average of the four dimension scores. Explicitly explain borderline choices.

### Confidence

Use the official five-point scale to report evaluator expertise and checking depth. Confidence is not paper quality and should not be inflated to make a recommendation appear stronger.

### Ethical Concerns

Choose `NO or VERY MINOR ethics concerns only`, `CLEAR MAJOR CONCERN`, or the relevant expert-consultation category. The 2026 form names human-subject research; privacy, copyright, and consent; data quality and representativeness; safety and security; discrimination, bias, and unfairness; deception and harassment; environmental impact; and human rights including surveillance.

If asking for expert consultation, do not preemptively lower the overall score for the same uncertain concern. If the concern is already clear, describe it in `Strengths And Weaknesses` and alert the responsible chair rather than treating ethics review as a substitute for reasoning.

### Paper Formatting Concerns — 2026 Only

Report major formatting, anonymity, or related submission-rule concerns. The official handbook explicitly says these perceived violations must **not** lower the scientific `Overall` score; chairs handle them separately.

## Scoring — NeurIPS 2025 And 2026

Use these official dimension anchors:

- `Quality`: 1 poor, 2 fair, 3 good, 4 excellent;
- `Clarity`: 1 poor, 2 fair, 3 good, 4 excellent;
- `Significance`: 1 poor, 2 fair, 3 good, 4 excellent;
- `Originality`: 1 poor, 2 fair, 3 good, 4 excellent.

Any `1` or `2` must be justified in `Strengths And Weaknesses`. A high overall score paired with a low dimension score, or the reverse, requires an explicit reconciliation.

### Overall

- `6 — Strong Accept`: technically exceptional, groundbreaking impact, exceptionally strong evaluation and reproducibility, with no unaddressed ethical concerns;
- `5 — Accept`: technically solid, high potential value, strong evaluation and reproducibility;
- `4 — Borderline Accept`: reasons to accept narrowly outweigh reasons to reject; use sparingly;
- `3 — Borderline Reject`: reasons to reject narrowly outweigh reasons to accept; use sparingly;
- `2 — Reject`: substantial technical, evaluation, reproducibility, or unresolved ethical weaknesses;
- `1 — Strong Reject`: fundamental failure such as well-known results or decisive unaddressed concerns.

### Confidence

- `5` — absolutely certain; very familiar with related work and checked details carefully;
- `4` — confident but not absolutely certain;
- `3` — fairly confident; some parts, literature, or details may have been missed;
- `2` — willing to defend, but likely to have missed central parts or relevant work;
- `1` — educated guess; outside the area or unable to check important details.

## NeurIPS 2026 Contribution-Type Interpretation

### General

Apply the broad definitions above. Quality asks whether methods and evidence support the claims; Clarity asks whether the paper informs and enables expert reproduction; Significance asks whether people are likely to use or build on the result; Originality includes new insights and useful combinations, not only new algorithms.

### Theory

- Quality prioritizes mathematical correctness, logical flow, core proof checking, and the appropriateness of assumptions.
- Empirical validation is not inherently required, and a theory paper must not be penalized for lacking state-of-the-art experiments.
- Clarity requires intuition, a high-level proof strategy, and a clear statement of what is new.
- Significance may come from a new abstraction, progress on an established problem, or a result that changes theoretical understanding.
- Originality may be a proof technique, formulation, definition, synthesis of tools, or new angle on a known problem.

### Use-Inspired

- The use case must be real, meaningful, and grounded in needs outside a contrived benchmark.
- Task framing, methods, constraints, data, metrics, and baselines should fit that use case, including relevant non-ML approaches.
- Non-standard real-world datasets are appropriate when justified.
- Significance may be practical impact, value to the NeurIPS community, or both.
- Applying existing methods can be original when the framing or combination is use-driven and produces transferable insight.

### Concept & Feasibility

- The work may have a scope larger than one paper can fully validate, but its present claims still require rigorous empirical, analytical, or conceptual support.
- The significance and originality bars are intentionally high: the idea should plausibly change approaches, understanding, or practice beyond the included evaluation.
- Treat it as a high-risk, high-reward contribution, not unfinished workshop work.

### Negative Results

- A failed experiment alone is insufficient. The negative result needs grounded analysis, careful experimentation, proof, or a convincing combination.
- It should change understanding of an important question or redirect how the community approaches it.
- The result must be surprising relative to prevailing understanding, with a high significance and originality bar.
- A mitigation or positive replacement is not mandatory when the negative finding itself is rigorous and informative.

## Reproducibility, Ethics, And Evidence Checks

- Check whether claims, code, data, model details, compute, hyperparameters, and evaluation protocols provide enough information for scrutiny and reproduction.
- Treat the submission checklist as an aid, not an automatic rejection checklist; a `No` answer is not usually sufficient grounds for rejection.
- Examine human-subject approval and compensation, privacy and consent, data licenses and deprecated datasets, representativeness, security, dual use, discrimination, surveillance, environmental impact, and human rights when relevant.
- Assess whether artifacts include the information required to understand them: execution environment, weights, data access, licenses, intended use, and limitations.
- Distinguish a scientific weakness from a policy allegation. Report suspected policy or integrity violations through the designated process while continuing a merit review unless instructed otherwise.

## LLM And Confidentiality Boundary

NeurIPS 2026 requires all submission and review material to remain confidential and forbids disclosure to unsanctioned LLMs. Official reviewers must follow the conference's assigned or sanctioned LLM policy and acknowledgements. This profile is for authors reviewing their own work; it is not authorization to process a confidential NeurIPS submission with Omni or another external model.

## Calibration Rules

- Do not impose a rejection prior or stack-level acceptance quota.
- Do not require state-of-the-art empirical results when the contribution and claims do not call for them.
- Judge the paper under its declared contribution type rather than a preferred style of research.
- Focus on major, claim-relevant issues before minor presentation corrections.
- Reward transparent limitations and avoid double-penalizing one concern across multiple scores.
- Make rebuttal questions specific, feasible, and linked to a possible score change.
- Keep formatting and unresolved ethics consultation separate from the scientific score as required by the form.
- Use a professional, constructive, evidence-grounded tone.
