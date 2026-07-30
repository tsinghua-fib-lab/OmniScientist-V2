# ACL/ARR Profile

Status: official-site-anchored contract for author-facing pre-submission review.

## Official Sources

- ARR Reviewer Guidelines: https://aclrollingreview.org/reviewerguidelines
- ARR Review Form: https://aclrollingreview.org/reviewform
- ARR Reviewing Process: https://aclrollingreview.org/reviewing
- ARR Dates And Venues: https://aclrollingreview.org/dates
- ACL 2026 Main Conference CFP: https://2026.aclweb.org/calls/main_conference_papers/

## Scope And Version Selection

- Use this profile when the paper is reviewed through ACL Rolling Review for an ACL-family venue.
- Keep the ARR review cycle separate from the eventual conference and year. The conference year does not uniquely determine the ARR form version.
- When the ARR cycle is known, use the official form for that cycle. When it is unknown, use the current public ARR form and state the form date assumption in `Target Venue`.
- Direct conference tracks not reviewed through ARR require their own official guidance and must not inherit this form.
- Never use the generic fallback form or generic fallback scores for an ARR-routed submission.

## Current Public ARR Review Contract

Render the substantive, author-facing fields in this order:

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

Knowledge or conjecture about author identity is reviewer-private and is not rendered in an author-facing pre-submission review.

Use `N/A` for `Best Paper Justification` unless the overall assessment is at the award boundary. Use `N/A` for `Datasets` or `Software` when the paper does not claim that artifact as a contribution.

## Scoring

- `Reviewer Confidence`: integer 1-5, from educated guess or outside the reviewer's area to fully familiar and carefully checked.
- `Soundness`: 1 Major Issues, 2 Poor, 3 Acceptable, 4 Strong, 5 Excellent; half-points are allowed.
- `Excitement`: 1 Not Exciting, 2 Potentially Interesting, 3 Interesting, 4 Exciting, 5 Highly Exciting; half-points are allowed.
- `Overall Assessment`: 1 Do Not Resubmit, 1.5 Resubmit After Next Cycle, 2 Resubmit Next Cycle, 2.5 Borderline Findings, 3 Findings, 3.5 Borderline Conference, 4 Conference, 4.5 Borderline Award, 5 Consider For Award.
- `Reproducibility`: integer 1-5, from impossible to easily reproducible.
- `Datasets`: integer 1-5 when applicable: 1 no usable artifact, 2 documentary, 3 potentially useful, 4 useful, 5 enabling.
- `Software`: integer 1-5 when applicable: 1 no usable artifact, 2 documentary, 3 potentially useful, 4 useful, 5 enabling.

There is no separate novelty score. Novelty and impact contribute to `Excitement` and `Overall Assessment`.

## Criteria

- soundness;
- excitement and likely value to ACL readers;
- novelty and impact within the overall recommendation;
- reproducibility;
- limitations and societal impact;
- ethics;
- dataset and software value when applicable;
- NLP-specific methodological validity.

## Venue-Specific Checks

- Are the claims clearly stated and adequately supported?
- Are models, languages, datasets, benchmarks, and evaluation choices justified by the claim scope?
- Are human or LLM-based evaluations validated and appropriate for the conclusions?
- Are formal arguments complete and based on explicit assumptions?
- Is the work reproducible enough to verify, use as a baseline, or build upon?
- Are limitations, societal impact, and ethical concerns handled through the responsible-NLP lens?
- Is material needed to assess novelty, claims, implications, related work, and correctness present in the main paper?

## Calibration Rules

- Do not claim that work is not novel without naming relevant prior work and explaining the overlap.
- Do not use an acknowledged limitation as a rejection reason unless it undermines a central claim.
- Treat unsupported broad claims about languages, populations, or tasks as scope problems, not as reasons for a harsher tone.
- Distinguish Findings-level soundness from conference-level novelty, impact, and excitement using the published overall scale.
- Keep criticism specific and constructive; a low score still requires actionable reasoning.
