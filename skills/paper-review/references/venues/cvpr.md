# CVPR Profile

Status: official-site-anchored contract for author-facing pre-submission review.

## Official Sources

- CVPR 2026 Reviewer Guidelines: https://cvpr.thecvf.com/Conferences/2026/ReviewerGuidelines
- CVPR 2026 Reviewer Training Material: https://cvpr.thecvf.com/Conferences/2026/ReviewerTrainingMaterial
- CVPR 2025 Reviewer Guidelines: https://cvpr.thecvf.com/Conferences/2025/ReviewerGuidelines

## Evidence Boundary

The official pages define review criteria and provide qualitative recommendation examples, but they do not print the private form's numeric encoding. The year-specific numeric scales below record conference-form metadata corroborated across public conference data. They are operational scales for review simulation, not quotations from the public guidelines. No individual review text is a rubric source.

## Scope And Version Selection

- This profile covers the CVPR main conference.
- Use the target year's official reviewer guidance. Do not silently import a form or score from another year.
- When no year is supplied, use the newest verified guidance and state the assumed year in `Target Venue`.
- Reverify later years because CVPR changed its recommendation scale between 2025 and 2026.
- Never use the generic fallback form or scores for CVPR.

## CVPR 2026 Public Review Contract

The official reviewer training material says a review should include these substantive components:

1. `Concise Summary`
2. `Strengths`
3. `Weaknesses`
4. `Constructive Suggestions`
5. `Overall Recommendation`
6. `Confidence`

Cover data-asset attribution, human-subject or personal-data issues, negative societal impact, limitations, and reproducibility inside the most relevant component. The reviewer guidelines mention a corresponding review-form field for data-asset citations but do not publish its exact label or options, so do not invent them.

Use the CVPR 2026 six-point overall-recommendation scale:

- `6` — Accept, the highest recommendation tier. The public training material describes the strongest positive case as `Strong Accept`.
- `5` — Weak Accept.
- `4` — Borderline Accept.
- `3` — Borderline Reject.
- `2` — Weak Reject.
- `1` — Reject.

The maximum overall recommendation is `6`. Confidence uses `1–5`, with `5` as the maximum. The numeric encoding is not printed on the public CVPR page, so do not attribute this table verbatim to the reviewer guidelines.

## CVPR 2025 Public Review Contract

The official reviewer guidelines require the main critique to explain strengths and weaknesses and to give specific feedback for improvement. Render:

1. `Summary`
2. `Strengths And Weaknesses`
3. `Constructive Feedback`
4. `Overall Recommendation`
5. `Confidence`

Use the CVPR 2025 five-point overall-recommendation scale:

- `5` — Accept.
- `4` — Weak Accept.
- `3` — Borderline.
- `2` — Weak Reject.
- `1` — Reject.

The maximum overall recommendation is `5`. Confidence uses `1–5`, with `5` as the maximum. Do not reuse this five-point scale for CVPR 2026.

## Criteria

- technical soundness;
- contribution to computer vision;
- novelty and potential impact;
- experimental rigor and evidence quality;
- appropriateness of datasets, metrics, and baselines;
- reproducibility;
- data attribution, privacy, and human-subject ethics;
- limitations and negative societal impact.

## Venue-Specific Checks

- Does the paper make a clear contribution to computer vision beyond isolated benchmark tuning?
- Do quantitative and qualitative results support the stated claims?
- Are ablations, baselines, datasets, and metrics appropriate for the contribution?
- Are data assets properly cited, and are restricted or withdrawn datasets handled responsibly?
- Are comparisons requested only when the work or implementation has been available sufficiently early?
- Are reproducibility details adequate for the central claims?
- Are limitations discussed honestly without being over-penalized?

## Calibration Rules

- Do not reject solely for lack of state-of-the-art accuracy.
- Minor flaws that can be corrected easily must not determine rejection.
- A novelty criticism must name relevant work and explain the relationship.
- Replace generic requests for more experiments with specific, decision-relevant requests that are reasonable in scope.
- Do not penalize a paper solely for lacking a separate limitations or societal-impact section.
- Keep the tone constructive and refer to `the paper` or `the work`, not the authors.
- CVPR 2026 prohibits LLM use in the formal reviewing process. This profile is for authors reviewing their own work before submission, not for producing an official confidential review.
