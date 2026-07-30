# ICLR Profile

Status: official-site-anchored contract for author-facing pre-submission review.

## Official Sources

- ICLR 2026 Reviewer Guide: https://iclr.cc/Conferences/2026/ReviewerGuide
- ICLR 2025 Reviewer Guide: https://iclr.cc/Conferences/2025/ReviewerGuide

## Evidence Boundary

The official guides define the review criteria and requested review content, but they do not enumerate the numeric choices in the conference form. The year-specific rating scales below record conference-form metadata corroborated across public conference data. Use them for review simulation, but do not claim that their wording was transcribed from the public guides. No individual review text is a rubric source.

## Scope And Version Selection

- This profile covers the ICLR main conference.
- When the target year is 2025 or 2026, use that year's guide.
- When no year is supplied, use the newest verified guide and state the assumed year in `Target Venue`.
- When the target year is not covered here, consult that year's official ICLR reviewer guide and reverify the form scale. Do not silently reuse a previous year's scale.
- Never use the generic fallback form or generic fallback scores for ICLR.

## Public Review Contract

The 2025 and 2026 reviewer guides ask reviewers to organize the substantive review as follows:

1. `Summary`
2. `Strengths And Weaknesses`
3. `Initial Recommendation`
4. `Supporting Arguments`
5. `Questions For Authors`
6. `Additional Feedback`
7. `Code Of Ethics Assessment`
8. `Overall Rating`
9. `Confidence`

Render related-work findings inside `Strengths And Weaknesses`, `Supporting Arguments`, or `Additional Feedback`; do not add a generic missing-citations section.

The 2026 guide also requires reviewers to disclose LLM use in the official form, but it does not publish the exact field label or options. Do not invent them. The skill's final `Disclaimer` should make its author-facing, LLM-assisted status clear.

## Rating And Recommendation

Use the target year's exact discrete rating choices. Do not interpolate unlisted scores.

### ICLR 2026

- `10` — Strong Accept; should be highlighted as a spotlight or oral.
- `8` — Accept; a good paper suitable for a poster.
- `6` — Marginally above the acceptance threshold.
- `4` — Marginally below the acceptance threshold.
- `2` — Reject; not good enough.
- `0` — Strong Reject.

The maximum overall rating is `10`. Confidence uses `1–5`, with `5` as the maximum.

### ICLR 2025

- `10` — Strong Accept; should be highlighted.
- `8` — Accept; a good paper.
- `6` — Marginally above the acceptance threshold.
- `5` — Marginally below the acceptance threshold.
- `3` — Reject; not good enough.
- `1` — Strong Reject.

The maximum overall rating is `10`. Confidence uses `1–5`, with `5` as the maximum.

Use the guide's explicit decision language for the accompanying recommendation:

- `Initial Recommendation: Accept`
- `Initial Recommendation: Reject`

For 2026, ratings `6`, `8`, and `10` map to `Accept`; ratings `0`, `2`, and `4` map to `Reject`. For 2025, ratings `6`, `8`, and `10` map to `Accept`; ratings `1`, `3`, and `5` map to `Reject`. Give one or two principal reasons, followed by evidence in `Supporting Arguments`.

## Criteria

- value to the ICLR community;
- new, relevant, and impactful knowledge;
- technical correctness and scientific rigor;
- support for theoretical and empirical claims;
- motivation and placement in the literature;
- clarity, experimental rigor, and reproducibility;
- compliance with the ICLR Code of Ethics.

## Venue-Specific Checks

- Does the paper clearly identify the problem, objective, and claimed contribution?
- Is the approach well motivated and appropriately situated in the literature?
- Are the central claims supported by correct theory, rigorous experiments, or both?
- Does the work contribute sufficient new knowledge or value to the ICLR community?
- Are any requested experiments limited enough to validate the submitted claims rather than create a substantially different paper?
- Are contemporaneous and non-peer-reviewed works treated according to the target year's guide?

## Calibration Rules

- Do not reject solely because the paper lacks state-of-the-art performance.
- Separate decision-relevant concerns from additional feedback that would improve the paper but does not determine the recommendation.
- Do not turn a limited, answerable question into a fundamental flaw.
- Keep the recommendation open to revision when an author response could resolve a central uncertainty.
- Use a constructive tone and assess the work, not the authors.
