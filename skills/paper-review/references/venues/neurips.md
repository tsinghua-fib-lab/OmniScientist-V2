# NeurIPS Profile

Status: official-site-anchored contract for author-facing pre-submission review.

## Official Sources

- NeurIPS 2026 Main Track Handbook: https://neurips.cc/Conferences/2026/MainTrackHandbook
- NeurIPS 2026 Reviewer Guidelines: https://neurips.cc/Conferences/2026/ReviewerGuidelines
- NeurIPS 2025 Reviewer Guidelines: https://neurips.cc/Conferences/2025/ReviewerGuidelines

## Scope And Version Selection

- This profile covers the NeurIPS main track only.
- Use the 2025 contract for NeurIPS 2025 and the 2026 contract for NeurIPS 2026.
- When no year is supplied, use the newest verified contract and state the assumed year in `Target Venue`.
- Other NeurIPS tracks require their own official guidance and must not inherit the main-track form automatically.
- For an unverified year, consult that year's official handbook or reviewer guidelines. Do not translate scores from another year and do not use the generic fallback form.

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

Code-of-conduct and responsible-reviewing acknowledgements are administrative fields and are not rendered in an author-facing pre-submission review.

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

Use one 2026 contribution type: `General`, `Theory`, `Use-Inspired`, `Concept & Feasibility`, or `Negative Results`. If the paper does not declare one, infer the best fit and state that it is an assumption. Apply the official type-specific interpretation of the four criteria.

Code-of-conduct and responsible-reviewing acknowledgements are administrative fields and are not rendered in an author-facing pre-submission review.

## Scoring

For both verified years:

- `Quality`: 1 poor, 2 fair, 3 good, 4 excellent.
- `Clarity`: 1 poor, 2 fair, 3 good, 4 excellent.
- `Significance`: 1 poor, 2 fair, 3 good, 4 excellent.
- `Originality`: 1 poor, 2 fair, 3 good, 4 excellent.
- `Overall`: 1 Strong Reject, 2 Reject, 3 Borderline Reject, 4 Borderline Accept, 5 Accept, 6 Strong Accept.
- `Confidence`: 1 educated guess, 2 willing to defend with substantial uncertainty, 3 fairly confident, 4 confident but not certain, 5 absolutely certain.

Use scores 3 and 4 sparingly and explain which considerations place the paper on that side of the acceptance boundary. Do not use a generic 1-5 or 1-10 overall scale.

## Criteria

- quality;
- clarity;
- significance;
- originality;
- reproducibility;
- limitations and implications;
- ethics;
- contribution-type fit for 2026.

## Venue-Specific Checks

- Are the claims technically sound and supported by evidence appropriate to the contribution type?
- Is the work complete enough to assess, reproduce, and build upon?
- Is the contribution likely to matter to the NeurIPS community?
- Is the relationship to prior work specific and accurate?
- For theory, are the claims, assumptions, lemmas, and proofs rigorous and properly scoped?
- For use-inspired work, do the task, metrics, data, and comparisons match the real use case?
- For concept-and-feasibility or negative-results work, is the finding sufficiently significant and well grounded for that contribution type?

## Calibration Rules

- Do not apply a rejection prior; weigh strengths and weaknesses against the published score anchors.
- Do not require state-of-the-art empirical results when the selected contribution type does not call for them.
- Reward clear limitations rather than treating acknowledged limitations as automatic rejection reasons.
- Questions should be actionable and should say what kind of answer would change the evaluation.
- Formatting concerns must not lower the 2026 overall score; report them only in the dedicated field.
