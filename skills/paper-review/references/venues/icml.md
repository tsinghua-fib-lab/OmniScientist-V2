# ICML Profile

Status: official-site-derived, year-aware contract for author-facing pre-submission review.

Verification snapshot: 2026-08-06 (Asia/Shanghai).

## Official Sources

- ICML 2026 Reviewer Instructions: https://icml.cc/Conferences/2026/ReviewerInstructions
- ICML 2026 Policy For LLM Use In Reviewing: https://icml.cc/Conferences/2026/LLM-Policy
- ICML 2026 Research Ethics: https://icml.cc/Conferences/2026/ResearchEthics
- ICML 2026 Peer-Review Ethics: https://icml.cc/Conferences/2026/PeerReviewEthics
- ICML 2025 Reviewer Instructions: https://icml.cc/Conferences/2025/ReviewerInstructions

## Evidence Boundary

- The official reviewer-instruction pages publish the main-track form fields, field-level prompts, score anchors, process rules, and separate position-track instructions. No reconstructed third-party form is needed.
- ICML changed the main-track review form substantially between 2025 and 2026. The two contracts and score systems are not interchangeable.
- Administrative code-of-conduct and LLM-policy acknowledgements, reviewer-private expertise questions, and post-rebuttal final-justification fields are process metadata and are not part of the initial author-facing review.

## Scope And Version Selection

- This profile covers the ICML main research track.
- Use the 2025 contract for ICML 2025 and the 2026 contract for ICML 2026.
- If no year is supplied, use ICML 2026 and state the assumption in `Target Venue`.
- Position papers have a separate official form and evaluation purpose. Workshops, tutorials, and other tracks also require their own instructions.
- For an unverified year, consult that year's official reviewer instructions. Do not translate scores or add fields from another year.
- Never use the generic fallback form or generic fallback scores for an ICML main-track paper.

## ICML 2025 Review Contract

Render the substantive, author-facing main-track fields in this order:

1. `Summary`
2. `Claims And Evidence`
3. `Relation To Prior Works`
4. `Other Aspects`
5. `Questions For Authors`
6. `Ethical Issues`
7. `Overall Recommendation`

The 2025 main-track form does not include a numeric confidence field. Code-of-conduct acknowledgement and reviewer-private literature-familiarity information are not rendered.

## ICML 2026 Review Contract

Render the substantive, author-facing main-track fields in this order:

1. `Summary`
2. `Strengths And Weaknesses`
3. `Soundness`
4. `Presentation`
5. `Significance`
6. `Originality`
7. `Key Questions For Authors`
8. `Limitations`
9. `Overall Recommendation`
10. `Confidence`
11. `Ethical Concerns`

Compliance acknowledgements and the post-response final-justification field are not rendered in an initial pre-submission review.

## ICML 2025 Field Guidance

### Summary

Briefly state the paper's problem, contribution, method, and main findings in the evaluator's own words. Do not critique the work or paste the abstract.

### Claims And Evidence

- Identify the central claims and whether theoretical or empirical evidence supports each one.
- Check the suitability of the methods, evaluation criteria, proof assumptions, experimental design, controls, statistics, and conclusions.
- State which important proofs or derivations were checked and any point that could not be verified.
- State whether supplementary material needed to assess a central claim was reviewed.
- Separate a missing detail from evidence that directly contradicts a claim.

### Relation To Prior Works

Explain whether the paper is correctly situated in the literature and whether omitted work changes its novelty, significance, or conclusions. Name specific essential references and describe the overlap. Do not demand an exhaustive bibliography or expose reviewer-private familiarity answers.

### Other Aspects

Cover originality, significance, clarity, reproducibility, useful strengths, secondary concerns, and local corrections not already addressed. Do not use this field as an unstructured dump of minor issues.

### Questions For Authors

Ask concise questions whose answers could change the recommendation or resolve a central ambiguity. State how possible answers affect the assessment. Avoid requests that would require a new project during the response period.

### Ethical Issues

Describe potential issues involving discrimination, bias, fairness, inappropriate applications, human rights, human subjects, responsible research practice, privacy, security, legal compliance, or research integrity. Use neutral evidence language and distinguish a concern needing expert review from a proven violation.

### Overall Recommendation — ICML 2025

Use the official five-point scale:

- `5 — Strong Accept`;
- `4 — Accept`;
- `3 — Weak Accept`;
- `2 — Weak Reject`;
- `1 — Reject`.

Do not add the 2026 four dimension scores or a generic confidence score to a 2025 main-track review.

## ICML 2026 Field Guidance

### Summary

Briefly summarize the paper and its contributions in the evaluator's own understanding. The authors should normally agree with an accurate summary; critique belongs in later fields.

### Strengths And Weaknesses

Give a thorough, evidence-backed assessment across Soundness, Presentation, Significance, and Originality. Focus on major contribution-relevant issues before local corrections. Substantiate both praise and criticism with paper locations or concrete reasoning.

### Soundness

Assess whether claims are technically supported, methods are appropriate, proofs are correct and use reasonable assumptions, and experiments are well designed. Soundness is separate from impact: an incremental result can be sound, and an ambitious idea can be unsound.

### Presentation

Assess writing, structure, definitions, notation, figures, contextualization relative to prior and concurrent work, and whether an expert has enough information to reproduce the result.

### Significance

Assess the importance or relevance of the problem and whether the work advances understanding, capabilities, or practice. Impact may be broad or specialized; modest or domain-specific improvements can be significant when they unlock useful directions or practical value.

### Originality

Assess new insights, tasks, methods, theory, data, perspectives, relaxed assumptions, real-world applications, and creative combinations of existing ideas. A wholly new method is not required. Closely related literature and the basis for novelty must be identified accurately.

### Key Questions For Authors

- Ask ideally `3–5` numbered questions.
- Reserve questions for answers likely to change the evaluation, resolve confusion, or address a critical limitation.
- Explain how plausible answers would affect the evaluation.
- Avoid substantial new-work requests during discussion.

### Limitations

Enter `Yes` if limitations and potential negative societal impact are addressed adequately. Otherwise give constructive missing points. Candid limitation disclosure should be rewarded rather than punished.

### Overall Recommendation

Synthesize the assessment using the official six-point scale. Scores `3` and `4` are boundary choices and should be used sparingly with explicit reasoning about which side the paper falls on.

### Confidence

Report expertise and checking depth, not enthusiasm or paper quality. Lower the score when important proofs, experiments, relevant literature, supplementary evidence, or domain assumptions could not be checked.

### Ethical Concerns

State whether specialist ethics review is warranted, select the relevant expertise categories, and explain the evidence. Official categories include:

- discrimination, bias, and fairness;
- inappropriate potential applications and human-rights impact;
- responsible research practice such as IRB or documentation;
- privacy and security;
- legal compliance, including copyright, terms of use, and data protection;
- research integrity such as plagiarism;
- other.

Do not allege misconduct as fact without evidence, and do not use an ethics flag as a substitute for a scientific assessment.

## ICML 2026 Scoring

### Dimension Scores

- `Soundness`: 1 poor, 2 fair, 3 good, 4 excellent;
- `Presentation`: 1 poor, 2 fair, 3 good, 4 excellent;
- `Significance`: 1 poor, 2 fair, 3 good, 4 excellent;
- `Originality`: 1 poor, 2 fair, 3 good, 4 excellent.

Every `1` or `2` needs a concrete explanation in `Strengths And Weaknesses`. Reconcile any tension between the dimensions and the overall score.

### Overall Recommendation

- `6 — Strong Accept`: technically exceptional with exceptional impact, strong evaluation and reproducibility, and no unaddressed ethical concerns;
- `5 — Accept`: technically solid with high impact and good-to-excellent evaluation and reproducibility;
- `4 — Weak Accept`: technically solid and likely to be built upon, with weaknesses that limit impact; use sparingly;
- `3 — Weak Reject`: clear merits, but weaknesses outweigh them and meaningful revision is needed; use sparingly;
- `2 — Reject`: substantial technical, evaluation, reproducibility, ethical, or intelligibility problems;
- `1 — Strong Reject`: fundamental failure such as well-known results, decisive unresolved ethics issues, or writing that prevents identification of the contribution.

### Confidence

- `5` — absolutely certain; very familiar with related work and checked details carefully;
- `4` — confident but not absolutely certain;
- `3` — fairly confident; some parts, details, or literature may have been missed;
- `2` — willing to defend, but likely to have missed central parts or related work;
- `1` — educated guess; outside the area or unable to understand and check important details.

## Year-Specific Process And Literature Rules

### ICML 2026

- Reviewers must follow the actual LLM policy assigned in their Reviewer Console. Under Policy A, LLM use during reviewing is prohibited apart from incidental use in conventional search, spelling, or grammar tools. Under Policy B, a privacy-compliant LLM may help clarify the paper, explore related work, or polish reviewer-written text, but it may not summarize the paper, judge quality or significance, identify strengths or weaknesses, outline or write the review, or propose author questions. The human reviewer must form and write the assessment. The position track uses Policy A only.
- Suspected prompt injection should be reported, but the remainder of the paper must still be reviewed normally. Prompts that only try to detect reviewer LLM use are not penalized as prompt injection.
- The discussion has three rounds—rebuttal, reviewer follow-up, and author follow-up—each limited to 5000 characters. Questions should therefore be prioritized and answerable.
- Work is contemporaneous if published within two months before the submission deadline. Missing comparison to contemporaneous work should not determine rejection.

### ICML 2025

- Generative-AI use for official reviewing was prohibited.
- The contemporaneous-work window was four months before the submission deadline.
- Apply the 2025 form and policies even if a later profile appears more familiar.

This Omni profile is for authors reviewing their own manuscripts. It does not authorize processing a confidential ICML submission with an external model.

## Non-Main-Track Boundary

ICML 2026 Position Papers have a separate purpose and official form, including position-specific questions and their own dimension and recommendation scales. A position paper should be reviewed under that form, not forced into the main-track Soundness/Presentation/Significance/Originality contract. Similar care is required for any future track-specific format.

## Venue-Specific Checks

- Are the claimed contributions explicit, and does each important claim have suitable evidence?
- Do theoretical statements follow from complete arguments under reasonable, visible assumptions?
- Do experiments test the stated hypotheses, and do reported conclusions follow from the results?
- Are baselines, datasets, metrics, statistics, ablations, uncertainty, and compute appropriate to the claim?
- Would omitted prior work materially change novelty, significance, or conclusions?
- Is the work reproducible enough to scrutinize and build upon?
- Are limitations and foreseeable impacts discussed constructively?

## Calibration Rules

- Evaluate against the target year's published form, not a remembered form from another year.
- Prioritize central contribution-relevant issues over minor corrections.
- Do not treat every missing citation as severe; explain whether it changes the conclusions.
- Separate soundness, originality, significance, presentation, and confidence rather than letting one impression determine all scores.
- Do not inflate significance for sound but narrow work, and do not dismiss specialized work without assessing its actual value.
- Reward candid limitations and proportionate claims.
- Use an empathetic, professional, evidence-grounded tone and update the review when discussion resolves a material concern.
