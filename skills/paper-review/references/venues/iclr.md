# ICLR Profile

Status: official-site-anchored, year-aware contract for author-facing pre-submission review.

Verification snapshot: 2026-08-06 (Asia/Shanghai).

## Official Sources

- ICLR 2027 Reviewer Guidelines: https://iclr.cc/Conferences/2027/ReviewerGuidelines
- ICLR 2027 AI Policy For Reviewers: https://iclr.cc/Conferences/2027/AIPolicyForReviewers
- ICLR 2027 Call For Papers: https://iclr.cc/Conferences/2027/CallForPapers
- ICLR 2026 Reviewer Guide: https://iclr.cc/Conferences/2026/ReviewerGuide
- ICLR 2025 Reviewer Guide: https://iclr.cc/Conferences/2025/ReviewerGuide
- ICLR Code Of Ethics: https://iclr.cc/public/CodeOfEthics

## Public Form Evidence

The conference guides describe how to reason about and organize a review, but they do not list every OpenReview field or every score option. The field contracts and numeric choices below are additionally checked against public ICLR reviews on the conference's OpenReview venue:

- ICLR 2026 conference: https://openreview.net/group?id=ICLR.cc/2026/Conference
- ICLR 2026 public review example: https://openreview.net/forum?id=fNC3aaGeIa
- ICLR 2025 public review example: https://openreview.net/forum?id=dq3keisMjT

OpenReview review records are evidence for field labels and options, not normative sources for review quality. Do not copy or imitate an individual review.

## Evidence Boundary

- The guide's six-part writing sequence (`summary`, strong/weak points, initial recommendation, supporting arguments, questions, and additional feedback) is conceptual guidance. It is **not** the literal 2025/2026 OpenReview field schema.
- The verified 2025 and 2026 public forms instead expose the ten substantive fields listed in the contracts below.
- ICLR 2027 has published reviewer guidance and an AI policy, but as of the verification date its live review form and rating legend are not yet publicly observable. For an ICLR 2027 simulation, apply the 2027 substantive guidance and use the verified 2026 field layout only as an explicitly labelled historical fallback. Do not call the inherited numeric scale an official ICLR 2027 scale.
- Administrative acknowledgements, reviewer identity information, and AI-use disclosures are not rendered as scientific feedback in this author-facing tool.

## Scope And Version Selection

- This profile covers the ICLR main conference.
- Use the exact 2025 contract for ICLR 2025 and the exact 2026 contract for ICLR 2026.
- If no year is supplied, use ICLR 2026 as the newest completely verified form and state that assumption in `Target Venue`.
- For ICLR 2027, state `2027 guidance + provisional 2026 form fallback` in `Target Venue` until the 2027 form is publicly verifiable.
- For another year, consult that year's official reviewer guide and public form. Never silently reuse a previous year's rating scale.
- Workshops and other tracks need their own instructions and must not inherit this main-conference profile automatically.

## ICLR 2025 Review Contract

Render the substantive, author-facing OpenReview fields in this order:

1. `Summary`
2. `Soundness`
3. `Presentation`
4. `Contribution`
5. `Strengths`
6. `Weaknesses`
7. `Questions`
8. `Flag For Ethics Review`
9. `Rating`
10. `Confidence`

The code-of-conduct acknowledgement is administrative and is not rendered.

## ICLR 2026 Review Contract

Render the substantive, author-facing OpenReview fields in this order:

1. `Summary`
2. `Soundness`
3. `Presentation`
4. `Contribution`
5. `Strengths`
6. `Weaknesses`
7. `Questions`
8. `Flag For Ethics Review`
9. `Rating`
10. `Confidence`

The code-of-conduct acknowledgement and mandatory reviewer LLM-use disclosure are administrative and are not rendered. The final disclaimer must still make Omni's author-facing, LLM-assisted status explicit.

## Field Guidance

### Summary

- State the problem, approach, principal claims, and claimed contribution in the reviewer's own words.
- Do not paste or lightly paraphrase the abstract.
- Keep critique out of this field; a correct summary should normally be recognizable to the authors.

### Soundness

Score whether the central claims are technically correct and supported by appropriate theory, experiments, or analysis. Check assumptions, proof-critical steps, experimental design, statistics, controls, and whether conclusions follow from the reported results. Soundness is distinct from impact.

### Presentation

Score clarity, organization, precision, and whether an expert has enough information to understand and reproduce the work. Include material problems in notation, definitions, figures, or experimental detail, but do not turn a list of minor typos into the main review.

### Contribution

Score whether the work creates new, relevant, and potentially useful knowledge for the ICLR community. Contributions may be empirical, theoretical, methodological, systems-oriented, or practitioner-facing. State-of-the-art performance is not required by itself.

### Strengths And Weaknesses

- `Strengths` should identify concrete merits and point to supporting paper evidence.
- `Weaknesses` should separate decision-relevant concerns from optional improvements.
- A novelty objection must identify the relevant prior work and explain the actual overlap.
- Do not count the same concern repeatedly across Soundness, Presentation, Contribution, and Rating.

### Questions

Ask only questions that resolve a material ambiguity, test a central concern, or could change the assessment. State what answer or evidence would increase or decrease the rating. Requested experiments must be limited enough to validate the submitted claims rather than create a substantially different paper.

### Flag For Ethics Review

State whether a potential ICLR Code of Ethics issue warrants escalation. If yes, describe the issue and the evidence without declaring misconduct as a settled fact. The official CoE report has two questions: whether a potential violation exists and, if so, why.

### Rating And Confidence

The overall rating must synthesize the written evidence rather than average the three dimension scores mechanically. Confidence describes the reviewer's ability to assess the work; it is not a proxy for paper quality.

## Dimension Scores — ICLR 2025 And 2026

Use the following choices for `Soundness`, `Presentation`, and `Contribution`:

- `4` — excellent;
- `3` — good;
- `2` — fair;
- `1` — poor.

Every score below `3` needs a concrete explanation in `Weaknesses`. A high overall rating with a low dimension score, or the reverse, needs an explicit reconciliation.

## Overall Rating — ICLR 2026

Use only the listed choices; do not interpolate unlisted numbers:

- `10` — Strong Accept: outstanding work that should be highlighted as a spotlight or oral;
- `8` — Accept: a good paper suitable for presentation as a poster;
- `6` — Marginally Above The Acceptance Threshold;
- `4` — Marginally Below The Acceptance Threshold;
- `2` — Reject: not good enough for acceptance;
- `0` — Strong Reject.

Scores `6`, `8`, and `10` are on the accept side; scores `0`, `2`, and `4` are on the reject side.

## Overall Rating — ICLR 2025

Use only the listed choices:

- `10` — Strong Accept: outstanding work that should be highlighted;
- `8` — Accept: a good paper;
- `6` — Marginally Above The Acceptance Threshold;
- `5` — Marginally Below The Acceptance Threshold;
- `3` — Reject: not good enough for acceptance;
- `1` — Strong Reject.

Scores `6`, `8`, and `10` are on the accept side; scores `1`, `3`, and `5` are on the reject side.

## Confidence

The verified 2026 form uses `1–5`:

- `5` — absolutely certain; expert in the area and checked the technical details carefully;
- `4` — confident but not absolutely certain;
- `3` — fairly confident, with a realistic possibility of missed details or related work;
- `2` — willing to defend the assessment, but likely to have missed central details or literature;
- `1` — unable to make a reliable assessment; the official reviewer should alert the area chair.

ICLR 2025 public reviews use the same `1–5` orientation. Do not fabricate a high confidence score when the paper is outside the evaluator's expertise or when equations, appendices, code, or figures could not be checked.

## ICLR 2027 Current Guidance — Form Pending

The latest official guide changes emphasis even though the full form is not yet public:

- Be rigorous and open-minded, but concise. Include points likely to affect the final accept/reject decision rather than an exhaustive AI-generated issue inventory.
- Evaluate whether the work brings sufficient value and new knowledge; leaderboard dominance and state-of-the-art performance are not prerequisites.
- Keep new experiment requests small and directly tied to validating an existing claim.
- Work published in a peer-reviewed venue on or after 2026-07-17 is contemporaneous for ICLR 2027. Missing comparison to such work, or to work available only on arXiv, cannot be the basis for rejection.
- Limited, responsible AI assistance is allowed for official reviewers only with mandatory disclosure. A reviewer using AI must preserve and report the original self-written assessment and the relevant interactions; AI may not create that initial assessment.

These 2027 rules supersede conflicting historical process guidance when the target is ICLR 2027, but they do not establish an unverified 2027 numeric scale.

## Venue-Specific Checks

- Is the precise research question or problem identifiable?
- Is the approach well motivated and correctly situated in peer-reviewed literature?
- Are theoretical and empirical claims correct, scientifically rigorous, and scoped to the evidence?
- Does the work add relevant knowledge or utility for the ICLR community?
- Are experiments, baselines, uncertainty estimates, and ablations appropriate to the claim rather than to a generic checklist?
- Is enough information supplied for a knowledgeable reader to reproduce or scrutinize the result?
- Are ethical risks, sensitive data, human-subject issues, potential harms, and disclosure obligations handled appropriately?

## Calibration Rules

- Do not reject solely because the paper lacks state-of-the-art performance.
- Separate correctness from significance and significance from personal topic preference.
- Distinguish a fatal central flaw, a rebuttal-resolvable uncertainty, and a camera-ready improvement.
- Treat contemporaneous and non-peer-reviewed work according to the target year's cutoff policy.
- Update the recommendation when an author response or revision resolves a central uncertainty, and explain what changed.
- Use a constructive, depersonalized tone and assess the work rather than the authors.
