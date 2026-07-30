"""Offline contracts for paper-review's historical-review memory."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "paper-review"


def _load_review_memory() -> Any:
    name = "paper_review_historical_review_memory"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / "review_memory.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _TopicEmbedding:
    """Small deterministic embedding provider with meaningful topic geometry."""

    space_id = "emb-v1:offline-topic-v1"

    def __init__(self, *, dimension: int = 3) -> None:
        self.dimension = dimension
        self.calls: list[list[str]] = []

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        lowered = text.casefold()
        if self.dimension != 3:
            return [1.0] + [0.25] * (self.dimension - 1)
        if "graph" in lowered and "retrieval" in lowered:
            return [1.0, 0.1, 0.0]
        if "retrieval" in lowered:
            return [0.75, 0.5, 0.0]
        if "vision" in lowered or "image" in lowered:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


def test_vector_normalization_is_stable_for_large_finite_values() -> None:
    review_memory = _load_review_memory()

    normalized = review_memory._normalize_vector([1e308, 1e308])

    assert normalized == pytest.approx([2**-0.5, 2**-0.5])
    assert review_memory._pack_vector([1e308, 1e308])


def test_historical_review_text_redacts_identity_and_score_prior() -> None:
    review_memory = _load_review_memory()
    text = (
        "The ablation omits the retrieval component. My name is Joshua Vogelstein. "
        "I will raise my score if the authors add this experiment. "
        "The authors should report the component-wise result."
    )

    cleaned = review_memory._redact_historical_review_text(text)

    assert "Joshua Vogelstein" not in cleaned
    assert "raise my score" not in cleaned
    assert "ablation omits the retrieval component" in cleaned
    assert "component-wise result" in cleaned
    assert "identity removed" in cleaned
    assert "score or decision statement removed" in cleaned


def test_historical_review_text_redacts_real_decisions_but_keeps_metrics() -> None:
    review_memory = _load_review_memory()
    text = (
        "My current rating is 4 (borderline reject). "
        "I lean toward a Reject (Score: 2). "
        "I think this paper should be accepted. "
        "The paper should be rejected due to the missing control. "
        "I would reject this paper without the ablation. "
        "Given the limited novelty, I recommend rejection at this stage. "
        "I strongly recommend \nrejection of this paper in its current form. "
        "I am inclined to recommend its acceptance. "
        "The evidence remains below the acceptance threshold. "
        "The work is not ready for acceptance. "
        "The presentation is not good enough for publication. "
        "The authors need a stronger baseline before it can be accepted. "
        "My main reason for rejection is the missing evaluation. "
        "The result is marginally below the ICLR acceptance bar. "
        "The paper is polished enough to be publishable. "
        "The draft is in a publishable state. "
        "I would give it a 9/10 if that option existed. "
        "I would rate this paper as 7. "
        "I give 2 points in Presentation. "
        "Overall Rating: 6/10. "
        "I currently rate this paper a 6. "
        "I'd rate it a 6 or 8. "
        "I gave a 2 instead of a 4. "
        "The main reason for giving a score of 2 is the missing baseline. "
        "Reason for Final Score: the evaluation is incomplete. "
        "The reviewer is willing to reconsider the rating after rebuttal. "
        "A stronger evaluation would lead to a higher score. "
        "The weaknesses prevent the paper from meeting ICLR's acceptance criteria. "
        "For the purpose of acceptance this is not a major issue. "
        "Given the limitations, I do not think it is ready for this venue. "
        "The work lacks sufficient novelty for a publication at this venue. "
        "This submission was justifiably rejected. "
        "It lacks a publishable contribution. "
        "The revision would render the manuscript publishable. "
        "This contribution is more appropriate for a workshop than publication at ICLR. "
        "The current draft is not ready for submission to a venue like ICLR. "
        "This type of work is not good enough for a machine learning conference. "
        "This contribution is more suitable for a focused venue. "
        "The reason for me giving a Score of 2 is the weak evaluation. "
        "I assign a rate of 9, but not the highest score. "
        "The primary reason for a score of 2 is the missing baseline. "
        "This is the reason for the reject score. "
        "Lean reject. "
        "**Initial Recommendation:** Reject. "
        "Therefore, my initial recommendation is rejection. "
        "My initial recommendation is acceptance. "
        "Overall recommendation (tentative). "
        "The missing baseline prevents me from giving a higher score. "
        "The paper is above the threshold for acceptance. "
        "The submission does not meet ICLR standards. "
        "Overall, I will keep the current score. "
        "This contribution is more suitable for a journal. "
        "The current draft is not ready for ICLR. "
        "This type of work is not a good fit for ICLR. "
        "The current version falls far short of a publishable draft. "
        "- Reject. "
        "My publication recommendation mainly relies on the weak evidence. "
        "I will keep my positive recommendation contingent on a new baseline. "
        "The reviewer assigns an initial score of 6. "
        "I am giving a low initial score. "
        "I put a lower rating because the control is missing. "
        "I will adjust the final score after rebuttal. "
        "My low initial score reflects the incomplete evaluation. "
        "The current score is 4 and may be raised. "
        "I lean toward a negative recommendation. "
        "I recommend submission to a different venue. "
        "Recommend for journal submission instead of an ML conference. "
        "ICLR may not be the right venue for this contribution. "
        "It is concerning if this meets ICLR standard. "
        "The result is not substantial enough to meet the standards of ICLR. "
        "The contribution does not meet the bar for ICLR. "
        "The presentation falls short of ICLR standards. "
        "My low score is currently due to the missing baseline. "
        "A complete ablation would result in a higher rating. "
        "The requested experiment would contribute to a higher score. "
        "I am happy to raise my score after the rebuttal. "
        "I may change my scores after clarification. "
        "Justification for final score: key evidence is missing. "
        "I am raising to a score of 6. "
        "I would consider a higher score with stronger evidence. "
        "I start with a lower score because the control is absent. "
        "The F1 score drops on out-of-distribution data. "
        "The strategic link score is 0 for two examples. "
        "I recommend using rejection sampling for the baseline."
    )

    cleaned = review_memory._redact_historical_review_text(text)

    assert "current rating" not in cleaned
    assert "lean toward" not in cleaned
    assert "should be accepted" not in cleaned
    assert "should be rejected" not in cleaned
    assert "would reject" not in cleaned
    assert "recommend rejection" not in cleaned
    assert "recommend \nrejection" not in cleaned
    assert "rejection of this paper" not in cleaned
    assert "recommend its acceptance" not in cleaned
    assert "acceptance threshold" not in cleaned
    assert "ready for acceptance" not in cleaned
    assert "good enough for publication" not in cleaned
    assert "before it can be accepted" not in cleaned
    assert "reason for rejection" not in cleaned
    assert "acceptance bar" not in cleaned
    assert "publishable" not in cleaned
    assert "9/10" not in cleaned
    assert "rate this paper as 7" not in cleaned
    assert "2 points in Presentation" not in cleaned
    assert "Overall Rating" not in cleaned
    assert "currently rate this paper" not in cleaned
    assert "rate it a 6" not in cleaned
    assert "gave a 2" not in cleaned
    assert "reason for giving a score" not in cleaned
    assert "Final Score" not in cleaned
    assert "reconsider the rating" not in cleaned
    assert "higher score" not in cleaned
    assert "acceptance criteria" not in cleaned
    assert "purpose of acceptance" not in cleaned
    assert "ready for this venue" not in cleaned
    assert "novelty for a publication" not in cleaned
    assert "justifiably rejected" not in cleaned
    assert "publishable contribution" not in cleaned
    assert "render the manuscript publishable" not in cleaned
    assert "more appropriate" not in cleaned
    assert "ready for submission" not in cleaned
    assert "good enough for a machine learning conference" not in cleaned
    assert "more suitable for a focused venue" not in cleaned
    assert "reason for me giving" not in cleaned
    assert "assign a rate of 9" not in cleaned
    assert "primary reason for a score" not in cleaned
    assert "reject score" not in cleaned
    assert "Lean reject" not in cleaned
    assert "Initial Recommendation" not in cleaned
    assert "initial recommendation" not in cleaned
    assert "Overall recommendation" not in cleaned
    assert "higher score" not in cleaned
    assert "threshold for acceptance" not in cleaned
    assert "meet ICLR standards" not in cleaned
    assert "keep the current score" not in cleaned
    assert "suitable for a journal" not in cleaned
    assert "ready for ICLR" not in cleaned
    assert "good fit for ICLR" not in cleaned
    assert "publishable draft" not in cleaned
    assert "- Reject" not in cleaned
    assert "publication recommendation" not in cleaned
    assert "positive recommendation" not in cleaned
    assert "assigns an initial score" not in cleaned
    assert "low initial score" not in cleaned
    assert "lower rating" not in cleaned
    assert "adjust the final score" not in cleaned
    assert "current score is 4" not in cleaned
    assert "negative recommendation" not in cleaned
    assert "submission to a different venue" not in cleaned
    assert "journal submission" not in cleaned
    assert "right venue" not in cleaned
    assert "meets ICLR standard" not in cleaned
    assert "standards of ICLR" not in cleaned
    assert "bar for ICLR" not in cleaned
    assert "falls short of ICLR standards" not in cleaned
    assert "My low score" not in cleaned
    assert "higher rating" not in cleaned
    assert "contribute to a higher score" not in cleaned
    assert "happy to raise" not in cleaned
    assert "change my scores" not in cleaned
    assert "Justification for final score" not in cleaned
    assert "raising to a score of 6" not in cleaned
    assert "consider a higher score" not in cleaned
    assert "start with a lower score" not in cleaned
    assert "F1 score drops" in cleaned
    assert "strategic link score is 0" in cleaned
    assert "rejection sampling" in cleaned
    assert (
        "Directly turns papers into publishable presentation videos"
        in review_memory._redact_historical_review_text(
            "Directly turns papers into publishable presentation videos."
        )
    )
    assert (
        "We assign a score of 0 to each outlier"
        in review_memory._redact_historical_review_text(
            "We assign a score of 0 to each outlier."
        )
    )
    assert (
        "The paper uses rejection sampling"
        in review_memory._redact_historical_review_text(
            "The paper uses rejection sampling for candidate generation."
        )
    )
    assert (
        "It accepts an input tensor"
        in review_memory._redact_historical_review_text(
            "It accepts an input tensor and returns a normalized score."
        )
    )
    assert (
        "This paper analyzes publication bias"
        in review_memory._redact_historical_review_text(
            "This paper analyzes publication bias in benchmark reporting."
        )
    )


@pytest.mark.parametrize(
    "sentence",
    [
        (
            "It provides guarantees for both acceptance probability and expected "
            "acceptance length objectives."
        ),
        (
            "The paper introduces a risk-bounded acceptance criterion while reducing "
            "over-rejection of low-risk drafts under sampling."
        ),
        (
            "The method uses acceptance–rejection sampling with a derived upper bound."
        ),
        (
            "The paper analyzes Sequential Rejection Sampling and Batched Rejection "
            "Sampling."
        ),
        (
            "A detector distinguishes an accepted draft token from a rejected token."
        ),
        (
            "SNF's similarity score is not interpretable as an overall score."
        ),
        (
            "For this reason, along with the asymmetric F1 score, the result needs "
            "more analysis."
        ),
        (
            "The acceptance–rejection sampler still relies on an approximate ratio."
        ),
        (
            "The manuscript aligns trajectories to raise acceptance and uses "
            "acceptance–rejection sampling."
        ),
        (
            "The paper studies a deferred acceptance mechanism over accepted tokens."
        ),
        (
            "For (i) an accepted draft token and (ii) a rejected token, the detector "
            "records whether all tokens were accepted."
        ),
    ],
)
def test_historical_review_text_preserves_technical_decision_terms(
    sentence: str,
) -> None:
    review_memory = _load_review_memory()

    assert review_memory._redact_historical_review_text(sentence) == sentence


@pytest.mark.parametrize(
    "sentence",
    [
        "If the author's response is satisfactory to me, I would be willing to maintain a positive score.",
        "I would maintain the high score if authors can address my concerns.",
        "Due to open questions on my side I would for now maintain a slightly skeptical rating at rather low confidence.",
        "The authors should clearly address my concern in order to keep my original score.",
        "My questions should be addressed to maintain this rating.",
        "The reviewer is glad to keep the rating if the questions are well-discussed.",
        "I would like to raise the scores if the authors can address my questions properly.",
        "If the author can solve my questions, I will improve the rating.",
        "The reviewer would consider raising the score if the first two weaknesses are addressed.",
        "My confidence score is low.",
        "I will assign scores after the discussion.",
        "I set my confidence score as 2.",
        "I believe it would be a much better fit for a dataset/benchmark track.",
        "The manuscript may be better submitted to a specialized journal.",
        "The submitted paper is not strong enough for ICLR.",
        "This might be appropriate for very specialized venues but not a general ML conference.",
        "The contribution is borderline strong enough for an ICLR paper.",
        "The paper is recommended for submission to mathematics-focused conferences/journals.",
        "Wrong venue: This paper does not include learning aspects.",
        "ICLR is the wrong venue and the work is better suited for a numerical analysis journal.",
        "I recommend the paper be submitted at a neuroscience focused venue instead.",
        "I served as the reviewer for this paper at previous conferences.",
        "After comparing the changes, I retain my previous review.",
        "I previously reviewed this paper for NeurIPS.",
        "The authors addressed questions I raised in my previous review.",
        "In my previous review, I noted that evaluation was insufficient.",
        "I raised this as a reviewer for a previous submission.",
        "Below is my previous review of the paper with minor updates.",
        "My main ask to improve the rating of this paper is to include related work.",
        "This ultimately informed my choice of rating.",
        "I would like to hear from the authors before up-leveling the paper's rating.",
        "My relatively low confidence score reflects limited expertise.",
        "This is reflected in my low confidence rating.",
        "I set my confidence level to 1.",
        "Justification for Rating.",
        "I'm willing to adjust the scores if the authors provide more analysis.",
        "I may revise the score higher or lower after reading the rebuttal.",
        "The score I gave is too harsh.",
        "I would consider revising the rating if the authors clarify these issues.",
        "I am open to reconsidering the rating if my concerns are addressed.",
        "My actual score for this paper would be 5.",
        "My true score is closer to 5.",
        "Why is this the right venue for the paper?",
        "I suggest a thorough revision and submission to another venue.",
        "The paper should be submitted to a journal where it can be longer.",
        "I encourage the authors to submit an improved version to a robotics conference.",
        "The work should be presented as workshop contributions.",
        "I would suggest that the authors submit to a less prestigious venue.",
        "I am curious about the decision to submit this work to ICLR instead of ACL.",
        "Overall, my assessment is toward acceptance.",
        "This is a clear accept-level paper.",
        "These qualities constitute a clear accept.",
        "This omission is a valid reason for strong rejection.",
        "The additional experiments would move this toward acceptance.",
        "I assign a positive score at this time.",
        "Hence the positive score.",
        "I give a negative rating.",
        "I recommend a positive rating.",
        "This deserves a positive rating.",
        "I lean toward a negative score.",
        "I support an acceptance of this paper.",
        "I have given this paper a reject.",
        "My current rating is a weak reject.",
        "This paper is a clear rejection.",
        "I may raise my rating to acceptance during rebuttal.",
        "I would revise my final score after the response.",
        "I would rate this paper 3/10.",
        "The final desk-rejection decision is appropriate.",
        "I recommend accepting the work.",
        "I am unable to assign a positive score.",
        "Soundness=1 and overall score is low.",
        "I set my confidence to 2.",
        "My review has low confidence.",
        "The non-expert reviewer (see confidence) should be discounted.",
        "Review confidence will be low.",
        "I reviewed this paper in NeurIPS 2025 and gave a positive score.",
        "I reviewed the same paper for NeurIPS.",
        "I reviewed this same paper for ACL.",
        "I have previously reviewed this paper.",
        "I reviewed a previous version.",
        "I reviewed this manuscript in a previous conference.",
        "I have reviewed an earlier workshop version.",
        "Last time I reviewed this paper, the experiments were missing.",
        "This aligns more closely with the dataset track rather than ICLR.",
        "Why submit to this track?",
        "Unsuitable submission track: this belongs in a benchmark track.",
        "A theoretical CS conference may be a better fit.",
        "I do not believe ICLR is the right audience.",
        "This conference might not be the right avenue.",
        "A robotics venue might be more fitting.",
        "Why the main conference rather than the dataset track?",
        "This is insufficient to justify acceptance at this venue.",
        "I will adjust my future rating after rebuttal.",
        "I acknowledge the contribution and would recommend a positive rating.",
        "This paper is well-written, rich in content, and deserves a positive rating.",
        "I cannot assign it the highest rating.",
        "I have currently put scores based on uncertainty on these points.",
        "My concerns have caused me to give a more borderline score.",
        "The results and missing comparisons lead to a negative rating.",
        "To improve the score I give on this paper, I would like more information.",
        "I would have given the paper a better score with stronger experiments.",
        "I cannot give a good score, but after clarification the score would go up.",
        "I would be inclined to give it a higher score after clarification.",
        "I have gone with a lower overall score at the moment.",
        "It is difficult for me to give it a highly positive rating at this stage.",
        "Those answers could entail an improvement of the score.",
        "I selected a confidence score of 3.",
        "I will reduce my confidence from 4 to 3.",
        "I can only have lukewarm support for this paper with a low-confidence score.",
        "My review and score is reserved with weak confidence.",
        "This is my first time reviewing an AI paper, so I will lower my confidence.",
        "It is not clear that ICLR is the best venue for this research.",
        "This draft should be submitted to AISTATS or ALT instead.",
        "The pros outweigh the cons.",
        "The paper strengths outweigh its weaknesses.",
        "As a reviewer, I reviewed this work on an almost per-word-by-word basis.",
        "I compared this paper to other papers in my batch.",
        "The presentation is completely off, thereby a score of 1 from me.",
        "I find it hard to find the value of this paper on this conference venue.",
        "These weaknesses inhibit the potential of this work to be published in a top venue.",
        "This should be a major flaw for a submission to a top-tier conference.",
        "Disclosure: I accidentally learned the author names from another manuscript.",
        "Glad to raise the scores if these concerns are addressed.",
        "We cannot provide a positive score this round.",
        "I hold a negative rating towards this paper.",
        "I start with a tentative score that I will update after rebuttal.",
        "Because of this, this review has lower confidence.",
        "I could consider changing my decision, though low confidence is preserved.",
        "These weaknesses reduce my confidence in accepting it.",
        "I would be happy to see the paper accepted, but would not fight for it.",
        "The reviewer gives a weak acceptance.",
        "I intend to assign a borderline rejection after the rebuttal.",
        "This submission is not ready to be published, thus I gave a weak rejection.",
        "I would strongly push for rejection without the missing baselines.",
        "I oscillated between weak acceptance and acceptance.",
        "For now, I will start out with a reject.",
        "The reviewer recommends a weak rejection.",
        "Overall, I am open to accepting this paper.",
        "I will desk reject the paper for the format violation.",
        "I reviewed the same exact paper for NeurIPS 2025.",
        "I have reviewed 3-4 contemporaneous papers following this paradigm.",
        "I reviewed this paper in the past.",
        "I have previously reviewed these works.",
        "I have reviewed a prior version of the paper.",
        "One could consider raising the score if the concerns are addressed.",
        "I would not oppose acceptance if the questions are addressed.",
        "I would not reject the paper if it did not have this optional experiment.",
        "I would be open to moving into the accept range after rebuttal.",
        "I cannot give an acceptance recommendation at this time.",
        "I will be supportive of acceptance if the issue is fixed.",
        "I support the acceptance of this work.",
        "I would push this paper towards acceptance.",
        "In this form I cannot accept the work.",
        "I strongly encourage acceptance after the minor comments are answered.",
        "I cannot yet advocate for acceptance.",
        "It needs more work before I can recommend it for publication.",
        "I voted for desk reject.",
        "This is not an obstacle to acceptance.",
        "The score of this paper tends toward rejection.",
        "These limitations do not represent grounds for rejection.",
        "I would want to see the paper accepted into the conference.",
        "This paper cannot be accepted for ICLR.",
        "This paper should be given a weak acceptance.",
        "In general I would give a weakly accept.",
        "I am willing to change my opinion to accept.",
        "This paper is acceptable for publication after revision.",
        "Before I could give this paper a clear accept, the comparison must be added.",
        "I am supportive of accepting this paper.",
        "I cannot accept this paper without a complete overhaul.",
        "I cannot support the paper to be accepted.",
        "I will defer to other reviewers on acceptance decisions.",
        "This reviewer considers the missing code a blocker to acceptance.",
        "This reviewer recommends rejection.",
        (
            "This technical contribution is useful. I would have given it a clear accept."
        ),
        "My acceptance grade will depend on the missing comparison.",
        "This contribution cannot justify acceptance to a conference like ICLR.",
        "For this work to be accepted at a venue, the figures need comparisons.",
        "These issues should be addressed before I can commit to an acceptance.",
        "I do not think it should be accepted by a top venue like ICLR.",
        "This work is publishable given its practical novelty.",
        "TMLR would be a better venue for this empirical analysis.",
        "Perhaps AAAI would be more fitting.",
        "The paper feels more aligned with NLP venues rather than ICLR.",
        "I do not think ICLR is a reasonable choice for this work.",
        "The best thing for the work would be to be published in an IEEE journal.",
        "This work seems better suited as a report or blog post.",
        "The tool might be more appropriate to position it as a demo paper.",
        "This paper is probably better suited to something like EC.",
        "The paper fits better a dataset track rather than a research main track.",
        "The paper should be published where it reaches the right audience.",
    ],
)
def test_real_corpus_score_venue_and_identity_priors_are_redacted(
    sentence: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert cleaned != sentence
    assert "removed" in cleaned


@pytest.mark.parametrize(
    "sentence",
    [
        "I find the work straightforward if the reader is familiar with score-based generative modeling.",
        "This paper analyzes criteria that lead to a paper being accepted at ICLR conferences.",
        "The citation points to a following archival paper accepted at CVPR.",
        "The paper should compare with a widely used approach accepted in CVPR25.",
        "I can accept the current dependence as long as the proof is fixed.",
        'This work echoes a paper titled "Latent Diffusion for MRI" accepted to MICCAI 2025.',
        "Without publication, it is hard for other researchers to follow the work.",
        "While I accept Theorem 1, its implication seems misleading.",
        "The model converts verify accept/reject signals into online supervision.",
        "The paper converts scientific text into publication-ready illustrations.",
        "The claim concerns the growing acceptance of AI-generated papers at workshops.",
        "The result on accepted length is positively correlated with draft quality.",
        "Their papers may get desk rejected before reviewer matching.",
        "The method minimizes the total number of desk-rejected papers.",
        "This desk rejection ILP is not the bottleneck in reviewer assignment.",
        "The paper studies desk-rejection mechanisms and policies at top conferences.",
        "The paper formalizes which papers can be desk-accepted for review.",
        "The paper tracks token rejection and acceptance during generation.",
        "The rejection probability remains stable for benign prompts.",
        "QA pairs that passed verification were rejected by annotators.",
        "The method rejects detrimental effects caused by forget-tokens.",
        "The paper asks whether users reject valid actions.",
        "I accept that the bound is loose.",
        "Building on this, the method mixes (i) high-resolution grounding and (ii) lower-resolution attention with a fusion score.",
        "As far as I know, a lower Block Influence score indicates higher layer similarity.",
        "I am curious whether error tokens are assigned lower quality scores.",
        "I think a small set of benchmark scores should be reported.",
        "I suggest the authors set the anomaly score threshold on held-out validation data.",
        "I think adding calibration would improve the F1 score on rare classes.",
        "I recommend that the authors keep the score normalization step and justify it.",
        "The paper proposes a framework named ACCEPT for few-shot LLM jailbreaking.",
        "The paper does not detail strong kinematic rejection tests, so label noise remains.",
        "Is the metric 'average acceptance tokens' still effective for comparison?",
        "This paper combines a cost-derived acceptance gate with slow reasoning.",
        "This work integrates graph sampling with rejection-based fine-tuning.",
        "The rewards are discounted less in non-accepting states.",
        "The model accepts everything in a single accept state.",
        "The model increases the recommendation score when given a positive explanation.",
        "The algorithm moves toward low acceptance and erodes the gains.",
        "The paper lets the agent explicitly reject goal updates.",
        "The method studies acceptance-throughput collapse and acceptance-rate calibration.",
        "The paper proposes an interpretable reject option.",
        "The paper studies sensitivity to the rejection threshold.",
        "This paper studies harmless instructions incorrectly rejected by safety models.",
        "Experiments use classification-with-rejection tasks.",
        "The paper introduces a heuristic for rejecting priors.",
        "Wall-clock speed depends on acceptance rates.",
        "The model predicts driver order acceptance behavior.",
        "The benchmark covers hallucination rejection.",
        "The framework uses a state's acceptance in the DFA.",
        "The method uses side-talk rejection as an evaluation task.",
        "The paper uses rejection criteria for sampling termination.",
        (
            "The paper concedes that quantization cost grows rapidly, recommending "
            "“accepting the one-time cost.” Stronger engineering details are needed."
        ),
    ],
)
def test_real_corpus_technical_language_is_not_redacted(sentence: str) -> None:
    review_memory = _load_review_memory()

    assert review_memory._redact_historical_review_text(sentence) == sentence


@pytest.mark.parametrize(
    "sentence",
    [
        (
            "About the definition of AP given at line 150: If I am not mistaken, "
            "the PR curve is formed by applying a systematic thresholding to the "
            "confidence score of the object detector, not by changing IoU thresholds."
        ),
        (
            "I imagine that a user would need to read the LLM's answer, interpret the "
            "confidence score, and decide whether or not to trust the answer based on "
            "the confidence score."
        ),
        (
            "Their method doesn't seem to be too far off and I think that would "
            "_greatly_ improve confidence in the method."
        ),
        (
            "2.Could the authors provide a detailed explanation through which refining "
            "the bounding boxes via reward in the RFT phase leads to more accurate "
            "artifact heatmaps and higher score prediction accuracy?"
        ),
        (
            "- While the forward confidence score is fairly straightforward, I found "
            "the presentation of the backward confidence score a bit confusing."
        ),
        (
            "- Even though the confidence score calculation and usage already exist "
            "in the literature and do not provide much to the community, I liked the "
            "incorporation of the backward confidence score."
        ),
        (
            "At the very least, a clear and convincing theoretical framework would be "
            "needed before such strong claims can be accepted."
        ),
        (
            "The paper introduces models such as Review Tendency Signal, but it is "
            "unclear how good this probabilistic model is, compared to other "
            "alternatives, e.g., the review score might follow a Bradley–Terry model."
        ),
        (
            "- The decision to have the LLM generate the review score via a prompt, "
            "alongside its explanations, raises concerns about the reliability of that score."
        ),
        (
            'Line 64: You state that the review score "reflects how favorably the text '
            'evaluates the target item."'
        ),
        "Line 233: Why did you choose the scalar range [1, 10] for the review score?",
        (
            "- The paper is submitted to the Datasets and Benchmarks track, but the "
            "benchmark collection and processing protocol are insufficiently described."
        ),
        (
            "Yet in my view, this is a relatively high score when considering the "
            "benchmark’s complexity."
        ),
        (
            "The experiment & Appendix D shows the various traces of problem (with "
            "correct vs incorrect) and the change of the score."
        ),
        (
            "If the paper is accepted, I would recommend to spend some of the extra "
            "page allowance on expanding to include more work on indirect prompt "
            "injections, instruction hierarchy, and other relevant topics, eg [1,2,3]."
        ),
        (
            "I know if the paper is accepted the page limit goes up to 10 pages, but "
            "**this feels rather unfair to the other submissions** in my opinion."
        ),
    ],
)
def test_real_corpus_confidence_and_score_terms_are_preserved_when_technical(
    sentence: str,
) -> None:
    review_memory = _load_review_memory()

    assert review_memory._redact_historical_review_text(sentence) == sentence


@pytest.mark.parametrize(
    ("sentence", "removed_fragment", "retained_fragment"),
    [
        (
            "It seems better to me that the paper is submitted to some benchmark tracks.",
            "submitted to some benchmark tracks",
            "",
        ),
        (
            "ICLR papers typically have a strong machine learning component, and my "
            "sense is that this paper might be slightly out of scope for ICLR — I have "
            "some questions about the Hidden Markov Model (HMM) which constitutes the "
            "main predictive component of the paper (see below).",
            "out of scope for ICLR",
            "questions about the Hidden Markov Model",
        ),
        (
            "I propose to resubmit the manuscript to VLDB or KDD.",
            "resubmit the manuscript to VLDB or KDD",
            "",
        ),
        (
            "Overall, I don't think the paper can be published at ICLR in its current form.",
            "paper can be published at ICLR",
            "",
        ),
        (
            "This mismatch, plus how different this paper is from the rest of the "
            "papers I'm currently reviewing for ICLR, drives my evaluation of "
            "contribution/venue fit.",
            "contribution/venue fit",
            "",
        ),
        (
            "Secondly, the venue fit is very good.",
            "venue fit is very good",
            "",
        ),
        (
            "Since there are some major weaknesses in the paper (see the Weaknesses "
            "Section), I suggest that the authors resubmit the paper to another "
            "conference or journal.",
            "resubmit the paper to another conference or journal",
            "major weaknesses",
        ),
        (
            "Publishing the paper in a journal could be an alternative to a conference "
            "because of the share amount of work that is reported.",
            "journal could be an alternative",
            "amount of work",
        ),
        (
            "To my judgment this paper would likely get rejected at flagship systems "
            "conferences that are all interested in work on ML, so why should it be "
            "published at ICLR?",
            "rejected at flagship systems conferences",
            "",
        ),
        (
            "My primary concern is that I think this paper belongs in a statistical "
            "journal, not ICRL.",
            "belongs in a statistical journal",
            "",
        ),
        (
            "That said, I may not be fully aware of the current editorial stance on "
            "such submissions, and I would defer to the area and meta chairs regarding "
            "venue fit.",
            "defer to the area and meta chairs",
            "",
        ),
        (
            "But if they resubmit the paper to another venue, they should consider "
            "adding some comparison studies to show that a general method such as EAD "
            "can even outperform methods specifically designed for inference-time scaling.",
            "resubmit the paper to another venue",
            "adding some comparison studies",
        ),
        (
            "I believe a specialised quantum computing conference or journals are a "
            "better venue for publishing this work.",
            "specialised quantum computing conference",
            "",
        ),
        (
            "While I think the approach is interesting and worth pursuing, given the "
            "magnitude of the changes needed in the evaluation I would recommend taking "
            "some time to improve the paper and resubmitting rather than trying to make "
            "all these changes in the rebuttal, but I could change my mind if new "
            "convincing evaluations are provided.",
            "resubmitting rather than",
            "magnitude of the changes needed in the evaluation",
        ),
        (
            "I suggest that this dataset paper be resubmitted to a dataset or benchmark track.",
            "resubmitted to a dataset or benchmark track",
            "",
        ),
        (
            "If I were the authors, I would submit this work to CV conferences including "
            "biometrics (e.g., FG2025).",
            "submit this work to CV conferences",
            "",
        ),
        (
            "The paper would be more appropriately published as a blog post or opinion "
            "piece rather than subjected to peer review at ICLR.",
            "blog post or opinion piece",
            "",
        ),
        (
            "I encourage the authors to improve this work, as I’d like to see it "
            "published at a top venue.",
            "published at a top venue",
            "",
        ),
        (
            "I’ll keep this reviews short because, overall, I would be very happy to "
            "see this published in the conference.",
            "published in the conference",
            "",
        ),
        (
            "Probably, the findings would be of more interest for audiences of medical "
            "(informatics) journals, who could also more rigorously judge the significance "
            "and validity of the research, rather than an AI conference.",
            "audiences of medical",
            "",
        ),
        (
            "I would recommend that the authors substantially revise their work or "
            "resubmit to other venues.",
            "resubmit to other venues",
            "",
        ),
        (
            "If this is the core contribution, considering the above point, the reviewer "
            "thinks it is not enough for a publication at a top-tier conference like ICLR.",
            "not enough for a publication",
            "",
        ),
        (
            "Given its specialized nature concerning batteries, I believe more specialized "
            "journals (such as those heavily cited in the references) would be a better fit.",
            "specialized journals",
            "specialized nature concerning batteries",
        ),
    ],
)
def test_real_corpus_venue_conclusions_are_removed_without_losing_revisions(
    sentence: str,
    removed_fragment: str,
    retained_fragment: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "venue-fit conclusion removed" in cleaned
    assert removed_fragment.casefold() not in cleaned.casefold()
    if retained_fragment:
        assert retained_fragment.casefold() in cleaned.casefold()


@pytest.mark.parametrize(
    ("sentence", "removed_fragment", "retained_fragment"),
    [
        (
            "The encoding scheme, model architectures are all well-defined, making it "
            "a good application paper but less ideal for a ICLR paper.",
            "less ideal for a ICLR paper",
            "model architectures are all well-defined",
        ),
        (
            "I strongly urge the authors to invest significant effort in improving the "
            "clarity, organization, and overall presentation of the paper before "
            "resubmitting to a future venue.",
            "resubmitting to a future venue",
            "improving the clarity",
        ),
        (
            "May be a more specialized QML venue is more ideal.",
            "specialized QML venue",
            "",
        ),
        (
            "I leave the decision for the AC and other reviewers to evaluate the "
            "significance and appropriateness of this paper in a conference like ICLR.",
            "leave the decision for the AC",
            "",
        ),
        (
            "Even though I could easily see this paper published in a top-tier control "
            "venue (IEEE CDC, TAC or SIOPT SICON), the fit with ICLR is very slim.",
            "top-tier control venue",
            "",
        ),
        (
            'My "reject" recommendation reflects precisely this: it should not be taken '
            "as a criticism of the paper's technical content (which I cannot assess at "
            "the level of a dedicated expert), but as an assessment of the suitability "
            "of this paper for ICLR as a whole.",
            'My "reject" recommendation',
            "",
        ),
        (
            "It seems to me the paper would get a more meaningful review, and a move "
            "understanding audience, in a venue like Siggraph or CVPR",
            "venue like Siggraph or CVPR",
            "",
        ),
        (
            "As such, it maybe worth questioning the adequacy of ICLR as a venue to "
            "publish this paper, although I do commend the authors for the quality of "
            "their work.",
            "adequacy of ICLR as a venue",
            "commend the authors",
        ),
        (
            "Its contribution is primarily infrastructural (data collection, filtering, "
            "and standardization), which, while useful, fits better within the scope of "
            "bioinformatics resources than an ML research venue.",
            "fits better within the scope",
            "contribution is primarily infrastructural",
        ),
        (
            "It is my view that such a submission would be better suited to a "
            '"mathematics of machine learning" journal rather than a conference such as '
            "ICLR.",
            "mathematics of machine learning",
            "",
        ),
        (
            "I think the result is interesting, mathematically, but not for the ICLR audience.",
            "not for the ICLR audience",
            "result is interesting, mathematically",
        ),
        (
            "However I don't know if Neurips is appropriate for this kind of paper,",
            "Neurips is appropriate",
            "",
        ),
        ("Why did you choose ICRL as a venue?", "choose ICRL as a venue", ""),
        (
            "Why did you choose ICLR as a venue for this work?",
            "choose ICLR as a venue",
            "",
        ),
        (
            "Overall, I am not sure whether it is the best venue.",
            "best venue",
            "",
        ),
        (
            "Perhaps the machine learning community in ICLR could be one of the best "
            "audiences for this work, even if it is not a typical paper.",
            "best audiences for this work",
            "",
        ),
        (
            "It might be better positioned for a venue focused more on applied or "
            "empirical studies, such as EMNLP, where the practical insights and "
            "orchestration design could be particularly appreciated.",
            "better positioned for a venue",
            "",
        ),
        (
            "I encourage the authors to substantially revise the paper for clarity, "
            "provide formal definitions and scalable implementations, and ensure "
            "consistency between the motivation and methodology before resubmission "
            "to a major venue.",
            "resubmission to a major venue",
            "provide formal definitions and scalable implementations",
        ),
        (
            "The contribution would be better appreciated in a \"datasets and "
            "benchmarks\" track in a major conference but as far as I know, ICLR does "
            "not have such a track.",
            "better appreciated in a \"datasets and benchmarks\" track",
            "",
        ),
        (
            "I am not sure whether it fits better under a benchmark or dataset track, "
            "if such a category exists.",
            "fits better under a benchmark or dataset track",
            "",
        ),
        (
            "Any reinforcement and validation of these findings from other existing "
            "literature will be very helpful here to make this work more relevant for "
            "ICLR venue (imo, this work seems more relevant for ACL/EMNLP venues or "
            "more focused tracks/workshops at ML conferences)",
            "more relevant for ICLR venue",
            "reinforcement and validation of these findings",
        ),
        (
            "However, its relevance to a ML venue is questionable.",
            "relevance to a ML venue is questionable",
            "",
        ),
        (
            "This paper is primarily a resource and system paper for one language, "
            "which aligns better with NLP venues that emphasize language resources "
            "and regional applications [1, 2].",
            "aligns better with NLP venues",
            "resource and system paper for one language",
        ),
        (
            'If there is no "learning" involved, then I don\'t think that ICLR is the '
            "ideal venue for this paper.",
            "ICLR is the ideal venue",
            'no "learning" involved',
        ),
        (
            "Due to the lack of experiments, I find this to be more of a workshop "
            "contribution.",
            "workshop contribution",
            "lack of experiments",
        ),
        (
            "But I don't think ICLR is the ideal place to publish this kind of result.",
            "ICLR is the ideal place",
            "",
        ),
        (
            "Consequently, this work seems better suited as a report or blog post, "
            "rather than a distinct contribution to academic research.",
            "report or blog post",
            "",
        ),
    ],
)
def test_additional_real_corpus_venue_language_is_redacted_precisely(
    sentence: str,
    removed_fragment: str,
    retained_fragment: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "removed" in cleaned
    assert removed_fragment.casefold() not in cleaned.casefold()
    if retained_fragment:
        assert retained_fragment.casefold() in cleaned.casefold()


@pytest.mark.parametrize(
    "sentence",
    [
        (
            "All in all, I found that the contributions are correct, and that there is "
            "enough novelty and insight to be presented at this conference."
        ),
        (
            "This paper appears to be more closely aligned with AI applications in "
            "education venues, such as AIED, EDM, and EAAI, among others, once it has "
            "been improved."
        ),
        (
            "While this is indeed a very serious issue with LRM, I believe the current "
            "paper is more like a homework assignment than a paper accepted by a conference."
        ),
        (
            "While its immediate practical relevance is limited, the conceptual "
            "advancement justifies acceptance at ICLR, provided the venue continues "
            "to welcome high-theory work."
        ),
        "I think that this project is a promising start but does not yet rise to the level of being accepted to a conference.",
        "Despite all these questions, I do love this paper and I think ICLR should accept such papers even if the results are flawed!",
        "This paper was previously submitted to ICLR 2025 and rejected for well-founded reasons (see OpenReview).",
        "In its current form, I am afraid that the work is not sufficient to justify acceptance at a venue like ICLR.",
        "According to ICLR policy, this paper should be desk-rejected.",
        (
            "Although I believe the proposed CTC are valid contributions to this "
            "community, the overall contribution of this paper does not reach the bar "
            "of acceptance of this top-tier conference."
        ),
        "I just wonder if a better audience for this set of results might be found at COLT or CCC rather than ICLR.",
        "- Could you better explain how this fits into ICLR?",
        "The idea attracts me a lot and I'd like to see the core idea published in the conference.",
        "I like this work and I believe it is worth being accepted to ICLR.",
        (
            "The paper requires significant revision to address these limitations before "
            "it can be considered for acceptance at the ICLR 2026 conference."
        ),
        "I think the current version of the paper has reached the acceptance bar of ICLR.",
        "Mainly for this reason my opinion regarding acceptance at ICLR is on the negative side.",
        "This seems mostly a dealbreaker for acceptance to ICLR",
        "Overall, I believe that this paper is borderline regarding acceptance to ICLR.",
        (
            "The proposed techniques are mostly a combination of existing methods, "
            "making it insufficient to be accepted as an ICLR paper."
        ),
    ],
)
def test_more_real_corpus_decision_and_venue_variants_are_redacted(
    sentence: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert cleaned != sentence
    assert "removed" in cleaned


@pytest.mark.parametrize(
    "sentence",
    [
        (
            "This paper is also submitted in ICML 2025 and NeurIPS 2025 but without "
            "replying to any reviewer comments."
        ),
        (
            "It is very likely that this is the first time the authors are submitting "
            "to a major ML conference and the authors are not fluent in English."
        ),
        "Was this paper submitted elsewhere first?",
        "The paper is a resubmission from an earlier venue.",
        "In fact, I was one of the reviewers of this paper submitting to NeurIPS.",
        (
            "I have personally tried before an idea like this, although I am currently "
            "not planning to publish this idea as a conference paper."
        ),
    ],
)
def test_more_real_corpus_reviewer_history_and_identity_are_redacted(
    sentence: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert cleaned != sentence
    assert "reviewer identity removed" in cleaned


@pytest.mark.parametrize(
    ("sentence", "retained_fragment"),
    [
        (
            "The contributions are correct, and there is enough novelty and insight to "
            "be presented at this conference.",
            "contributions are correct",
        ),
        (
            "The conceptual advancement is clear and justifies acceptance at ICLR.",
            "conceptual advancement is clear",
        ),
        (
            "This project is a promising start but does not yet rise to the level of "
            "being accepted to a conference.",
            "promising start",
        ),
        (
            "Nearly repeated work is not proper for ICLR.",
            "nearly repeated work",
        ),
        (
            "This is a complex and fragile approach with no tangible improvement, "
            "making me hard to accept it to ICLR.",
            "complex and fragile approach",
        ),
        (
            "The paper has several limitations that make it unsuitable for acceptance "
            "at this conference:",
            "paper has several limitations",
        ),
        (
            "The paper requires significant revision to address these limitations before "
            "it can be considered for acceptance at the ICLR 2026 conference.",
            "requires significant revision",
        ),
        (
            "The presentation improved since the NeurIPS version, with better discussion "
            "of parameter dependencies.",
            "better discussion of parameter dependencies",
        ),
        (
            "The paper lacks sufficient contribution and novelty for it to be accepted "
            "at the conference at this time.",
            "lacks sufficient contribution and novelty",
        ),
        (
            "The README states that the code is submitted to AAAI.",
            "README states",
        ),
        (
            "This paper was previously submitted to AAAI, but it appears only the "
            "formatting was revised.",
            "only the formatting was revised",
        ),
        (
            "I already commented to the AC before submitting the review regarding the "
            "comparison with VCR-GauS and the missing references in Section 3.2.",
            "comparison with VCR-GauS",
        ),
    ],
)
def test_latest_redactions_retain_actionable_current_paper_content(
    sentence: str,
    retained_fragment: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "removed" in cleaned
    assert retained_fragment.casefold() in cleaned.casefold()


@pytest.mark.parametrize(
    ("sentence", "retained_fragment"),
    [
        (
            "I currently rate this paper as reject due to the missing control.",
            "due to the missing control",
        ),
        (
            "I am happy to increase the score if the authors address the missing ablation.",
            "authors address the missing ablation",
        ),
        (
            "I lean toward rejection rating since the evidence is mostly case studies.",
            "evidence is mostly case studies",
        ),
        (
            "The current manuscript has room for improvement, which makes me lean to "
            "rejection.",
            "current manuscript has room for improvement",
        ),
        (
            "Acceptance should be contingent on addressing the issues above; most "
            "notably, statistical significance.",
            "statistical significance",
        ),
        (
            "The findings and experiments are too limited for a full publication at ICLR.",
            "findings and experiments are too limited",
        ),
        (
            "The paper requires substantial revision before it can be considered for "
            "acceptance at the ICLR 2026 conference.",
            "requires substantial revision",
        ),
        (
            "The proposed techniques combine existing methods, making it insufficient "
            "to be accepted as an ICLR paper.",
            "techniques combine existing methods",
        ),
        (
            "To justify publication, the paper needs deeper educational analysis.",
            "needs deeper educational analysis",
        ),
        (
            "I already commented to the AC before submitting the review regarding the "
            "missing references in Section 3.2.",
            "missing references in Section 3.2",
        ),
    ],
)
def test_decision_fragment_redaction_preserves_the_reason_or_revision(
    sentence: str,
    retained_fragment: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "removed" in cleaned
    assert retained_fragment.casefold() in cleaned.casefold()


@pytest.mark.parametrize(
    ("sentence", "retained_fragment", "removed_fragment"),
    [
        (
            "While there are aspects to improve, I think the paper should at least "
            "be accepted.",
            "aspects to improve",
            "accepted",
        ),
        (
            "I am not certain this work contains enough distinction from previous "
            "work to be accepted.",
            "enough distinction from previous work",
            "accepted",
        ),
        (
            "The empirical evidence and clarity are insufficient for acceptance at "
            "this stage.",
            "empirical evidence and clarity are insufficient",
            "acceptance",
        ),
        (
            "The paper has to be rejected because it presents existing technologies "
            "as new contributions.",
            "existing technologies as new contributions",
            "rejected",
        ),
        (
            "While I am not against accepting the paper, I have concerns about the "
            "empirical evaluation and practical overheads.",
            "concerns about the empirical evaluation",
            "accepting",
        ),
        (
            "The paper gets rejected because its proofs contain explicit bugs.",
            "proofs contain explicit bugs",
            "rejected",
        ),
        (
            "I would love to see this paper published, with a fairer treatment of "
            "existing work and experiments at smaller scales.",
            "fairer treatment of existing work",
            "published",
        ),
        (
            "The poor evaluation should be improved and made more general to render "
            "this paper publishable.",
            "poor evaluation should be improved",
            "publishable",
        ),
        (
            "The paper is below the bar for publication due to a lack of technical "
            "novelty and empirical results.",
            "lack of technical novelty",
            "bar for publication",
        ),
        (
            "To be suitable for publication, the paper should include stronger "
            "experimental validation and quantitative comparisons.",
            "paper should include stronger experimental validation",
            "suitable for publication",
        ),
    ],
)
def test_audited_verdict_variants_keep_author_guidance(
    sentence: str,
    retained_fragment: str,
    removed_fragment: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "removed" in cleaned
    assert retained_fragment.casefold() in cleaned.casefold()
    assert removed_fragment.casefold() not in cleaned.casefold()


@pytest.mark.parametrize(
    ("sentence", "retained_fragment", "removed_fragment"),
    [
        (
            "We would consider raising the score if the authors clarify this issue.",
            "authors clarify this issue",
            "raising the score",
        ),
        (
            "The mismatch between claims and experiments leads me to a score of 2-3.",
            "mismatch between claims and experiments",
            "2-3",
        ),
        (
            "I would be happy to raise my score to 8 if the weaknesses are addressed.",
            "weaknesses are addressed",
            "to 8",
        ),
        (
            "I am open to a high score provided the theoretical and empirical analysis "
            "is meticulous.",
            "theoretical and empirical analysis is meticulous",
            "high score",
        ),
        (
            "To achieve a score of 4, all major uncertainties must be resolved.",
            "all major uncertainties must be resolved",
            "score of 4",
        ),
    ],
)
def test_audited_score_variants_keep_the_revision_condition(
    sentence: str,
    retained_fragment: str,
    removed_fragment: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "removed" in cleaned
    assert retained_fragment.casefold() in cleaned.casefold()
    assert removed_fragment.casefold() not in cleaned.casefold()


@pytest.mark.parametrize(
    "sentence",
    [
        "I never have much confidence that I understand everything.",
        "I am not deeply familiar with this domain and defer to other reviewers "
        "when determining the final score.",
        "I express a low level of confidence in my review.",
    ],
)
def test_audited_reviewer_self_confidence_is_removed(sentence: str) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert cleaned == "[Historical score or decision statement removed.]"


@pytest.mark.parametrize(
    "sentence",
    [
        "Please cite the published articles rather than their preprints.",
        "The method uses a recently published benchmark.",
        "The authors should publish the code and data.",
        "The claim should be removed until it is explained in the planned publication.",
    ],
)
def test_publication_terms_are_preserved_when_they_are_not_review_verdicts(
    sentence: str,
) -> None:
    review_memory = _load_review_memory()

    assert review_memory._redact_historical_review_text(sentence) == sentence


def test_parenthesized_prior_submission_marker_has_balanced_output() -> None:
    review_memory = _load_review_memory()
    sentence = (
        "(This paper was previously submitted to AAAI, but it appears only the "
        "formatting was revised.)"
    )

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert cleaned == (
        "[Historical prior-submission detail removed.] "
        "it appears only the formatting was revised."
    )


def test_historical_review_text_redacts_reviewer_ids_and_self_disclosed_roles() -> None:
    review_memory = _load_review_memory()
    text = (
        "Reviewer ID: u6gT. "
        "I also reviewed this paper for another conference. "
        "I am a researcher in this area. "
        "I work at Example University. "
        "The missing ablation remains the central methodological concern."
    )

    cleaned = review_memory._redact_historical_review_text(text)

    assert "u6gT" not in cleaned
    assert "another conference" not in cleaned
    assert "researcher in this area" not in cleaned
    assert "Example University" not in cleaned
    assert "missing ablation" in cleaned


def test_venue_fit_redaction_keeps_the_actionable_reason() -> None:
    review_memory = _load_review_memory()
    text = (
        "The contribution is application-oriented rather than methodological, making "
        "it align more naturally with systems venues like HPCA rather than ICLR."
    )

    cleaned = review_memory._redact_historical_review_text(text)

    assert "application-oriented rather than methodological" in cleaned
    assert "venue-fit conclusion removed" in cleaned
    assert "HPCA rather than ICLR" not in cleaned


def test_quoted_sentence_split_keeps_critique_around_removed_verdict() -> None:
    review_memory = _load_review_memory()
    text = (
        'The paper claims "LoRA can still improve reasoning." '
        "I would have given it a clear accept."
    )

    cleaned = review_memory._redact_historical_review_text(text)

    assert 'The paper claims "LoRA can still improve reasoning."' in cleaned
    assert "clear accept" not in cleaned
    assert "score or decision statement removed" in cleaned


def _official_review(label: str) -> dict[str, Any]:
    return {
        "id": f"review-{label}",
        "content": {
            "summary": {"value": f"{label} complete summary text"},
            "strengths": {"value": f"{label} complete strengths text"},
            "weaknesses": {"value": f"{label} actionable weaknesses text"},
            "questions": {"value": f"{label} author questions text"},
            "flag_for_ethics_review": {"value": ["No ethics review needed."]},
            "rating": {"value": "7: Accept"},
            "confidence": {"value": "4: Confident"},
        },
    }


def _write_review_record(
    path: Path,
    *,
    title: str,
    abstract: str,
    label: str,
) -> None:
    path.write_text(
        json.dumps(
            {
                "submission": {
                    "content": {
                        "title": {"value": title},
                        "abstract": {"value": abstract},
                    }
                },
                "official_reviews": [_official_review(label)],
                "conference": "ICLR 2026",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_manifest(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    papers = [
        (
            "p-self",
            "Graph Retrieval for Scientific Papers",
            "Graph neural retrieval for scientific papers.",
            "self",
        ),
        (
            "p-graph",
            "Graph Memory for Scholarly Search",
            "Graph representations improve retrieval over scholarly documents.",
            "graph",
        ),
        (
            "p-retrieval",
            "Dense Retrieval with Language Models",
            "Retrieval augmented generation uses dense language representations.",
            "retrieval",
        ),
        (
            "p-vision",
            "Vision Models for Medical Images",
            "A vision transformer analyzes medical image benchmarks.",
            "vision",
        ),
    ]
    rows: list[dict[str, Any]] = []
    review_paths: dict[str, Path] = {}
    for paper_id, title, abstract, label in papers:
        review_path = tmp_path / f"{paper_id}.reviews.json"
        paper_path = tmp_path / f"{paper_id}.pdf"
        paper_path.write_bytes(f"offline fixture for {paper_id}".encode())
        _write_review_record(
            review_path,
            title=title,
            abstract=abstract,
            label=label,
        )
        review_paths[paper_id] = review_path
        rows.append(
            {
                "source_paper_id": paper_id,
                "openreview_forum_id": paper_id,
                "title": title,
                "paper_path": paper_path.name,
                "reviews_json_path": review_path.name,
                "conference": "ICLR 2026",
            }
        )
    manifest = tmp_path / "manifest_body.jsonl"
    manifest.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return manifest, review_paths


async def _build_fixture_index(
    tmp_path: Path,
) -> tuple[Any, Path, Path, dict[str, Path], _TopicEmbedding]:
    review_memory = _load_review_memory()
    manifest, review_paths = _write_manifest(tmp_path)
    index = tmp_path / "review-memory-faiss"
    embedder = _TopicEmbedding()
    result = await review_memory.build_review_index(
        manifest,
        index,
        embedder=embedder,
        embedding_model="offline-topic-v1",
        batch_size=2,
        rebuild=False,
        limit=None,
    )
    assert result["status"] == "ok"
    assert result["paper_count"] == 4
    assert result["review_count"] == 4
    return review_memory, manifest, index, review_paths, embedder


def _active_generation(index: Path) -> Path:
    header = json.loads((index / "index.json").read_text(encoding="utf-8"))
    return index / "generations" / str(header["active_generation"])


def _corrupt_one_byte(path: Path) -> None:
    payload = path.read_bytes()
    assert payload
    middle = len(payload) // 2
    path.write_bytes(payload[:middle] + bytes([payload[middle] ^ 1]) + payload[middle + 1 :])


@pytest.mark.asyncio
async def test_retrieval_ranks_topics_loads_text_and_hides_scores(tmp_path: Path) -> None:
    review_memory, _manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )

    result = await review_memory.retrieve_review_memory(
        index,
        embedder=_TopicEmbedding(),
        structure={
            "paper_id": "new-paper",
            "title": "Graph Retrieval for Scientific Papers",
            "abstract": "Graph neural retrieval for scientific papers.",
        },
        top_k=2,
        embedding_model="offline-topic-v1",
    )

    assert result["status"] == "ok"
    assert result["retrieval_mode"] == "faiss"
    assert [match["paper_id"] for match in result["matches"]] == [
        "p-graph",
        "p-retrieval",
    ]
    assert "p-self" not in {match["paper_id"] for match in result["matches"]}

    first_review = result["_review_packets"][0]["official_reviews"][0]
    assert first_review["textual_review_fields"] == {
        "summary": "graph complete summary text",
        "strengths": "graph complete strengths text",
        "weaknesses": "graph actionable weaknesses text",
        "questions": "graph author questions text",
    }
    packet_text = json.dumps(result["_review_packets"], ensure_ascii=False).casefold()
    assert "rating" not in packet_text
    assert "confidence" not in packet_text
    assert "7: accept" not in packet_text

    public = review_memory.public_review_memory(result)
    assert "_review_packets" not in public
    assert "graph actionable weaknesses text" not in json.dumps(
        public, ensure_ascii=False
    )

    metadata = review_memory.inspect_review_index(index)
    assert metadata["status"] == "ready"
    assert metadata["paper_count"] == 4
    assert metadata["review_count"] == 4
    assert "_review_packets" not in metadata


@pytest.mark.asyncio
async def test_each_build_publishes_one_complete_immutable_generation(
    tmp_path: Path,
) -> None:
    review_memory, manifest, index, review_paths, first_embedder = (
        await _build_fixture_index(tmp_path)
    )
    initial_metadata = review_memory.inspect_review_index(index)
    initial_fingerprint = initial_metadata["index_fingerprint"]
    initial_generation = initial_metadata["active_generation"]
    assert sum(len(batch) for batch in first_embedder.calls) == 4

    unchanged_embedder = _TopicEmbedding()
    unchanged = await review_memory.build_review_index(
        manifest,
        index,
        embedder=unchanged_embedder,
        embedding_model="offline-topic-v1",
        batch_size=3,
        rebuild=False,
        limit=None,
    )
    assert unchanged["papers_embedded"] == 4
    assert unchanged["papers_unchanged"] == 0
    assert unchanged["incremental_reuse"] is False
    assert sum(len(batch) for batch in unchanged_embedder.calls) == 4
    unchanged_metadata = review_memory.inspect_review_index(index)
    assert unchanged_metadata["active_generation"] != initial_generation
    assert unchanged_metadata["index_fingerprint"] == initial_fingerprint
    assert (
        index / "generations" / initial_generation / "vectors.faiss"
    ).is_file()

    graph_record = json.loads(review_paths["p-graph"].read_text(encoding="utf-8"))
    graph_record["official_reviews"][0]["content"]["weaknesses"]["value"] = (
        "graph newly revised actionable weaknesses text"
    )
    review_paths["p-graph"].write_text(
        json.dumps(graph_record, ensure_ascii=False), encoding="utf-8"
    )
    changed_embedder = _TopicEmbedding()
    changed = await review_memory.build_review_index(
        manifest,
        index,
        embedder=changed_embedder,
        embedding_model="offline-topic-v1",
        batch_size=4,
        rebuild=False,
        limit=None,
    )
    assert changed["papers_embedded"] == 4
    assert changed["papers_unchanged"] == 0
    assert sum(len(batch) for batch in changed_embedder.calls) == 4
    assert review_memory.inspect_review_index(index)["paper_count"] == 4
    assert (
        review_memory.inspect_review_index(index)["index_fingerprint"]
        != initial_fingerprint
    )


@pytest.mark.asyncio
async def test_corrupt_owned_index_is_refused_without_overwriting_it(
    tmp_path: Path,
) -> None:
    review_memory, manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )
    vectors = _active_generation(index) / "vectors.faiss"
    _corrupt_one_byte(vectors)
    corrupted = vectors.read_bytes()

    assert review_memory.inspect_review_index(index)["status"] == "invalid"
    with pytest.raises(ValueError, match="invalid or corrupted"):
        await review_memory.build_review_index(
            manifest,
            index,
            embedder=_TopicEmbedding(),
            embedding_model="offline-topic-v1",
            rebuild=True,
        )
    assert vectors.read_bytes() == corrupted


@pytest.mark.asyncio
async def test_rebuild_reloads_review_packets_from_the_source_files(
    tmp_path: Path,
) -> None:
    review_memory, manifest, index, review_paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )
    source = json.loads(review_paths["p-graph"].read_text(encoding="utf-8"))
    source["official_reviews"][0]["content"]["weaknesses"]["value"] = (
        "graph source-controlled replacement weakness"
    )
    review_paths["p-graph"].write_text(
        json.dumps(source, ensure_ascii=False), encoding="utf-8"
    )

    rebuilt = await review_memory.build_review_index(
        manifest,
        index,
        embedder=_TopicEmbedding(),
        embedding_model="offline-topic-v1",
        batch_size=4,
        rebuild=True,
    )

    assert rebuilt["papers_embedded"] == 4
    result = await review_memory.retrieve_review_memory(
        index,
        embedder=_TopicEmbedding(),
        structure={"title": "New graph paper", "abstract": "Graph retrieval."},
        top_k=1,
        embedding_model="offline-topic-v1",
        embedding_space_id=_TopicEmbedding.space_id,
    )
    assert "source-controlled replacement weakness" in json.dumps(
        result, ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_empty_manifest_cannot_be_marked_ready(tmp_path: Path) -> None:
    review_memory = _load_review_memory()
    manifest = tmp_path / "empty.jsonl"
    manifest.write_text("\n  \n", encoding="utf-8")
    index = tmp_path / "empty-faiss"

    with pytest.raises(ValueError, match="contains no paper records"):
        await review_memory.build_review_index(
            manifest,
            index,
            embedder=_TopicEmbedding(),
            embedding_model="offline-topic-v1",
        )

    assert not index.exists()


@pytest.mark.asyncio
async def test_model_mismatch_makes_review_memory_unavailable(tmp_path: Path) -> None:
    review_memory, _manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )

    result = await review_memory.retrieve_review_memory(
        index,
        embedder=_TopicEmbedding(),
        structure={
            "title": "Graph Retrieval for New Science",
            "abstract": "Graph retrieval augmented generation for scholarly documents.",
        },
        top_k=2,
        embedding_model="different-model-v2",
    )

    assert result["status"] == "unavailable"
    assert result["retrieval_mode"] == "none"
    assert result["matched_paper_count"] == 0
    assert any(
        "different-model-v2" in warning
        and "offline-topic-v1" in warning
        for warning in result["warnings"]
    )


@pytest.mark.asyncio
async def test_dimension_mismatch_without_fallback_is_unavailable(tmp_path: Path) -> None:
    review_memory, _manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )

    result = await review_memory.retrieve_review_memory(
        index,
        embedder=_TopicEmbedding(dimension=2),
        structure={
            "title": "Graph Retrieval for New Science",
            "abstract": "Graph retrieval for scholarly documents.",
        },
        top_k=2,
        embedding_model="offline-topic-v1",
    )

    assert result["status"] == "unavailable"
    assert result["outcome"]["code"] == "review_memory_embedding_unavailable"
    assert result["retrieval_mode"] == "none"
    assert "dimension 2" in result["warnings"][0]
    assert "dimension 3" in result["warnings"][0]


def test_untrusted_embedding_error_message_is_not_exposed() -> None:
    review_memory = _load_review_memory()
    error = NotImplementedError(
        "https://embedding.example/v1?api_key=super-secret"
    )

    rendered = review_memory._safe_embedding_error(error)

    assert rendered == "embedding runtime is unavailable"
    assert "embedding.example" not in rendered
    assert "super-secret" not in rendered


@pytest.mark.asyncio
async def test_embedding_space_mismatch_never_runs_semantic_search(
    tmp_path: Path,
) -> None:
    review_memory, _manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )
    query_embedder = _TopicEmbedding()

    result = await review_memory.retrieve_review_memory(
        index,
        embedder=query_embedder,
        structure={
            "title": "Graph Retrieval for New Science",
            "abstract": "Graph retrieval for scholarly documents.",
        },
        top_k=2,
        embedding_model="offline-topic-v1",
        embedding_space_id="emb-v1:different-service",
    )

    assert result["status"] == "unavailable"
    assert result["outcome"]["code"] == "review_memory_embedding_unavailable"
    assert "service/model space does not match" in result["warnings"][0]
    assert query_embedder.calls == []


@pytest.mark.asyncio
async def test_outdated_redaction_policy_requires_rebuild(tmp_path: Path) -> None:
    review_memory, manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )
    header_path = index / "index.json"
    header = json.loads(header_path.read_text(encoding="utf-8"))
    header["review_text_policy"] = "author_guidance_redacted_v1"
    header_path.write_text(json.dumps(header), encoding="utf-8")

    result = await review_memory.retrieve_review_memory(
        index,
        embedder=_TopicEmbedding(),
        structure={"title": "Graph retrieval", "abstract": "Scholarly retrieval."},
        embedding_model="offline-topic-v1",
    )

    assert result["status"] == "unavailable"
    assert result["outcome"]["code"] == "review_memory_index_incompatible"
    assert "policies are outdated" in result["warnings"][0]
    assert result["setup_command"].endswith("--rebuild")
    rebuilt = await review_memory.build_review_index(
        manifest,
        index,
        embedder=_TopicEmbedding(),
        embedding_model="offline-topic-v1",
        rebuild=True,
    )
    assert rebuilt["status"] == "ok"
    assert review_memory.inspect_review_index(index)["status"] == "ready"


@pytest.mark.asyncio
async def test_missing_index_returns_actionable_unavailable_result(tmp_path: Path) -> None:
    review_memory = _load_review_memory()
    missing = tmp_path / "does-not-exist-faiss"

    result = await review_memory.retrieve_review_memory(
        missing,
        embedder=_TopicEmbedding(),
        structure={"title": "New Paper", "abstract": "A new method."},
        top_k=5,
        embedding_model="offline-topic-v1",
    )

    assert result["status"] == "unavailable"
    assert result["outcome"]["code"] == "review_memory_index_missing"
    assert result["matches"] == []
    assert result["_review_packets"] == []
    assert "build_review_index.py" in result["setup_command"]
    assert review_memory.inspect_review_index(missing) == {
        "status": "missing",
        "index_path": str(missing.resolve()),
    }


@pytest.mark.asyncio
async def test_rebuild_refuses_unowned_file_without_changing_it(tmp_path: Path) -> None:
    review_memory = _load_review_memory()
    manifest, _paths = _write_manifest(tmp_path)
    destination = tmp_path / "unrelated-faiss"
    original = b"not an Omni review index"
    destination.write_bytes(original)

    with pytest.raises(ValueError, match="refusing to modify"):
        await review_memory.build_review_index(
            manifest,
            destination,
            embedder=_TopicEmbedding(),
            embedding_model="offline-topic-v1",
            rebuild=True,
        )

    assert destination.read_bytes() == original


@pytest.mark.asyncio
async def test_failed_atomic_rebuild_preserves_ready_index(tmp_path: Path) -> None:
    review_memory, manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )
    original_fingerprint = review_memory.inspect_review_index(index)[
        "index_fingerprint"
    ]

    async def failing_embedder(_texts: list[str]) -> list[list[float]]:
        raise RuntimeError("provider URL and secret must not escape")

    with pytest.raises(
        RuntimeError,
        match=r"embedding batch failed: embedding request failed \(RuntimeError\)",
    ):
        await review_memory.build_review_index(
            manifest,
            index,
            embedder=failing_embedder,
            embedding_model="offline-topic-v1",
            embedding_space_id=_TopicEmbedding.space_id,
            rebuild=True,
        )

    metadata = review_memory.inspect_review_index(index)
    assert metadata["status"] == "ready"
    assert metadata["index_fingerprint"] == original_fingerprint


@pytest.mark.asyncio
async def test_duplicate_manifest_ids_are_rejected(tmp_path: Path) -> None:
    review_memory = _load_review_memory()
    manifest, _paths = _write_manifest(tmp_path)
    first_row = manifest.read_text(encoding="utf-8").splitlines()[0]
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(first_row + "\n")

    with pytest.raises(ValueError, match="duplicate paper id p-self"):
        await review_memory.build_review_index(
            manifest,
            tmp_path / "duplicate-faiss",
            embedder=_TopicEmbedding(),
            embedding_model="offline-topic-v1",
            batch_size=10,
        )


@pytest.mark.asyncio
async def test_manifest_paths_cannot_escape_explicit_data_root(tmp_path: Path) -> None:
    review_memory = _load_review_memory()
    data_root = tmp_path / "allowed"
    data_root.mkdir()
    outside_review = tmp_path / "outside.reviews.json"
    _write_review_record(
        outside_review,
        title="Outside Paper",
        abstract="Outside abstract.",
        label="outside",
    )
    paper = data_root / "paper.pdf"
    paper.write_bytes(b"paper")
    manifest = data_root / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "source_paper_id": "outside",
                "title": "Outside Paper",
                "paper_path": str(paper),
                "reviews_json_path": str(outside_review),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="outside the allowed data root"):
        await review_memory.build_review_index(
            manifest,
            tmp_path / "escape-faiss",
            embedder=_TopicEmbedding(),
            embedding_model="offline-topic-v1",
            allowed_data_root=data_root,
        )


@pytest.mark.asyncio
async def test_manifest_paper_body_hash_is_verified(tmp_path: Path) -> None:
    review_memory = _load_review_memory()
    manifest, _paths = _write_manifest(tmp_path)
    rows = [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
    ]
    rows[0]["paper_sha256"] = "0" * 64
    manifest.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="paper body hash mismatch for p-self"):
        await review_memory.build_review_index(
            manifest,
            tmp_path / "bad-paper-hash-faiss",
            embedder=_TopicEmbedding(),
            embedding_model="offline-topic-v1",
        )


@pytest.mark.asyncio
async def test_manifest_and_review_json_paper_ids_must_agree(tmp_path: Path) -> None:
    review_memory = _load_review_memory()
    manifest, paths = _write_manifest(tmp_path)
    review = json.loads(paths["p-self"].read_text(encoding="utf-8"))
    review["paper_id"] = "different-paper"
    paths["p-self"].write_text(json.dumps(review), encoding="utf-8")

    with pytest.raises(ValueError, match="paper identity mismatch"):
        await review_memory.build_review_index(
            manifest,
            tmp_path / "mismatched-paper-id-faiss",
            embedder=_TopicEmbedding(),
            embedding_model="offline-topic-v1",
        )


@pytest.mark.asyncio
async def test_corrupt_review_pack_invalidates_the_index(tmp_path: Path) -> None:
    review_memory, _manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )
    _corrupt_one_byte(_active_generation(index) / "reviews.pack")

    result = await review_memory.retrieve_review_memory(
        index,
        embedder=_TopicEmbedding(),
        structure={"title": "Graph Retrieval", "abstract": "Graph retrieval."},
        top_k=1,
        embedding_model="offline-topic-v1",
        embedding_space_id=_TopicEmbedding.space_id,
    )

    assert result["status"] == "unavailable"
    assert result["outcome"]["code"] == "review_memory_index_invalid"
    assert result["matched_paper_count"] == 0
    assert "could not be validated" in result["warnings"][0]


@pytest.mark.asyncio
async def test_corrupt_same_length_faiss_file_is_rejected_during_retrieval(
    tmp_path: Path,
) -> None:
    review_memory, _manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )
    _corrupt_one_byte(_active_generation(index) / "vectors.faiss")

    result = await review_memory.retrieve_review_memory(
        index,
        embedder=_TopicEmbedding(),
        structure={"title": "Graph Retrieval", "abstract": "Graph retrieval."},
        top_k=1,
        embedding_model="offline-topic-v1",
        embedding_space_id=_TopicEmbedding.space_id,
    )

    assert result["status"] == "unavailable"
    assert result["outcome"]["code"] == "review_memory_index_invalid"
    assert "could not be validated" in result["warnings"][0]


@pytest.mark.asyncio
async def test_empty_query_is_reported_without_calling_embedder(tmp_path: Path) -> None:
    review_memory, _manifest, index, _paths, embedder = (
        await _build_fixture_index(tmp_path)
    )
    embedder.calls.clear()

    result = await review_memory.retrieve_review_memory(
        index,
        embedder=embedder,
        structure={"title": "", "abstract": ""},
        embedding_model="offline-topic-v1",
    )

    assert result["status"] == "unavailable"
    assert result["outcome"]["code"] == "review_memory_query_insufficient"
    assert embedder.calls == []


@pytest.mark.asyncio
async def test_index_uses_faiss_and_plain_files_without_a_database(
    tmp_path: Path,
) -> None:
    review_memory, manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )
    generation = _active_generation(index)

    assert json.loads((index / "index.json").read_text(encoding="utf-8"))[
        "index_owner"
    ] == review_memory.INDEX_OWNER
    header = json.loads((index / "index.json").read_text(encoding="utf-8"))
    assert "manifest_path" not in header
    assert header["manifest_name"] == manifest.name
    first_record = json.loads(
        (generation / "papers.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert "paper_path" not in first_record
    assert "reviews_json_path" not in first_record
    assert {path.name for path in generation.iterdir()} == {
        "vectors.faiss",
        "papers.jsonl",
        "reviews.pack",
    }
    assert not any(
        path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}
        for path in index.rglob("*")
    )
    assert (generation / "vectors.faiss").read_bytes()[:16] != b"SQLite format 3\x00"


@pytest.mark.asyncio
async def test_embedding_provider_exception_details_are_redacted(tmp_path: Path) -> None:
    review_memory, _manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )

    async def unsafe_embedder(_texts: list[str]) -> list[list[float]]:
        raise ValueError("https://provider.invalid/?api_key=super-secret")

    result = await review_memory.retrieve_review_memory(
        index,
        embedder=unsafe_embedder,
        structure={"title": "Graph Retrieval", "abstract": "Graph retrieval."},
        embedding_model="offline-topic-v1",
        embedding_space_id=_TopicEmbedding.space_id,
    )

    rendered = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "unavailable"
    assert "super-secret" not in rendered
    assert "provider.invalid" not in rendered
    assert "embedding request failed (ValueError)" in rendered


@pytest.mark.asyncio
async def test_safe_embedding_http_status_survives_query_boundary(tmp_path: Path) -> None:
    review_memory, _manifest, index, _paths, _embedder = (
        await _build_fixture_index(tmp_path)
    )

    class _SafeHTTPError(RuntimeError):
        code = "embedding_http_error"
        http_status = 429

    async def limited_embedder(_texts: list[str]) -> list[list[float]]:
        raise _SafeHTTPError("unsafe URL and provider body")

    result = await review_memory.retrieve_review_memory(
        index,
        embedder=limited_embedder,
        structure={"title": "Graph Retrieval", "abstract": "Graph retrieval."},
        embedding_model="offline-topic-v1",
        embedding_space_id=_TopicEmbedding.space_id,
    )

    rendered = json.dumps(result, ensure_ascii=False)
    assert "HTTP 429" in rendered
    assert "unsafe URL" not in rendered


@pytest.mark.parametrize(
    "sentence",
    [
        (
            "The optimization selects which papers to reject while preserving "
            "diversity in the post-rejection pool."
        ),
        "The safety model rejects safe queries and audits its rejection responses.",
        (
            "The paper predicts acceptance decisions as an outcome in a regression "
            "study."
        ),
        "The sampler estimates rejection constants and applies an acceptance rule.",
    ],
)
def test_peer_review_and_algorithmic_decision_terms_remain_technical(
    sentence: str,
) -> None:
    review_memory = _load_review_memory()

    assert review_memory._redact_historical_review_text(sentence) == sentence


@pytest.mark.parametrize(
    ("sentence", "retained_fragments", "removed_fragments"),
    [
        (
            "My main ask to improve the rating of this paper is to include thorough "
            "related work and a warm-start conditioning experiment.",
            ("thorough related work", "warm-start conditioning experiment"),
            ("improve the rating",),
        ),
        (
            "I would further raise it to 6 (accept) if authors perform cost analysis "
            "and address interpretability claims.",
            ("cost analysis", "interpretability claims"),
            ("raise it to 6", "accept"),
        ),
        (
            "The paper has significant weaknesses, which make me recommend a score "
            "of 4 (Borderline Accept).",
            ("significant weaknesses",),
            ("score of 4", "Borderline Accept"),
        ),
    ],
)
def test_numeric_stance_is_removed_but_author_actions_survive(
    sentence: str,
    retained_fragments: tuple[str, ...],
    removed_fragments: tuple[str, ...],
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "Historical score or decision statement removed" in cleaned
    for fragment in retained_fragments:
        assert fragment.casefold() in cleaned.casefold()
    for fragment in removed_fragments:
        assert fragment.casefold() not in cleaned.casefold()


@pytest.mark.parametrize(
    ("sentence", "retained_fragment"),
    [
        (
            "The technical approach lacks sufficient novelty for a top-tier venue "
            "like NeurIPS.",
            "lacks sufficient novelty",
        ),
        (
            "There appeared to be no learning in the paper which would make it out "
            "of ICLR's scope.",
            "no learning in the paper",
        ),
        (
            "Since the submission is intended for ICLR, quantitative and qualitative "
            "experiments are essential.",
            "quantitative and qualitative experiments are essential",
        ),
    ],
)
def test_venue_prior_is_removed_while_technical_reason_survives(
    sentence: str,
    retained_fragment: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "Historical venue-fit conclusion removed" in cleaned
    assert retained_fragment.casefold() in cleaned.casefold()


@pytest.mark.parametrize(
    ("sentence", "retained_fragment"),
    [
        (
            "As I have said in a previous review of the same paper: The method needs "
            "a GNN baseline.",
            "method needs a GNN baseline",
        ),
        (
            "Given this is a resubmission, the paper should compare against TabR.",
            "paper should compare against TabR",
        ),
    ],
)
def test_prior_submission_prefix_is_removed_but_revision_survives(
    sentence: str,
    retained_fragment: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "Historical prior-submission detail removed" in cleaned
    assert retained_fragment.casefold() in cleaned.casefold()


@pytest.mark.parametrize(
    "sentence",
    [
        "I was invited as a supplementary reviewer for this paper.",
        "I do not have the bandwidth to check the proofs due to the review workload.",
        "I would like to disclose that I served as one of the reviewers.",
    ],
)
def test_reviewer_process_metadata_is_removed(sentence: str) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert cleaned == "[Historical reviewer identity removed.]"


def test_unpublishable_stance_does_not_hide_writing_reason() -> None:
    review_memory = _load_review_memory()
    sentence = (
        "The method is promising, but in an unpublishable state due to writing and "
        "presentation."
    )

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "unpublishable" not in cleaned
    assert "writing and presentation" in cleaned


@pytest.mark.parametrize(
    ("sentence", "retained", "removed"),
    [
        (
            "If these points can be convincingly addressed, I would consider "
            "raising my overall rating.",
            ("If these points can be convincingly addressed",),
            ("raising my overall rating",),
        ),
        (
            "Am open to potentially increasing my score if the authors narrow "
            "the claims, remove the robotics framing, and add fluid context.",
            ("authors narrow the claims", "add fluid context"),
            ("increasing my score",),
        ),
        (
            "I will raise my rating to 8 or 10 if my major questions are well "
            "addressed.",
            ("major questions are well addressed",),
            ("raise my rating", "8 or 10"),
        ),
        (
            "I will only consider a higher score if the authors extend the "
            "evaluation and compare against small modern models.",
            ("extend the evaluation", "small modern models"),
            ("consider a higher score",),
        ),
        (
            "If the authors address the above concerns effectively, particularly by "
            "clarifying the implementation details and expanding the evaluation to "
            "include text-to-image generation tasks, I would be willing to reconsider "
            "my assessment and potentially give a more positive score.",
            ("clarifying the implementation details", "text-to-image generation tasks"),
            ("reconsider my assessment", "positive score"),
        ),
        (
            "My current rating tends toward rejection, yet I am willing reconsider my "
            "assessment if the authors address the relation of Prefix-RFT to [2] and "
            "provide evidence that the gaps in performance in Table 1 are significant "
            "(e.g., what is the standard deviation of these results across random seeds "
            "for training?).",
            (
                "authors address the relation of Prefix-RFT to [2]",
                "standard deviation of these results across random seeds",
            ),
            ("current rating", "rejection", "reconsider my assessment"),
        ),
        (
            "I am ready to give a *borderline accept* thanks to the attractive idea but "
            "would gladly increase my score if the authors can engage with the rest of "
            "the art and **really** demonstrate both theoretically and empirically the "
            "superiority of Waterfall compared to other schemes.",
            (
                "authors can engage with the rest of the art",
                "superiority of Waterfall compared to other schemes",
            ),
            ("borderline accept", "increase my score"),
        ),
        (
            "1, It would improve my score considereably if the author can provide "
            "further comparison between ablating / pruning neuron groups from [1] and "
            "[2] in the experiment of \"causal pruning evaluation\".",
            (
                "author can provide further comparison",
                "ablating / pruning neuron groups from [1] and [2]",
                "causal pruning evaluation",
            ),
            ("improve my score",),
        ),
        (
            "To improve my score, I would like to see evaluations with other common "
            "self-attention techniques such as SageAttention and a more comprehensive "
            "model quality evaluation.",
            (
                "evaluations with other common self-attention techniques such as SageAttention",
                "more comprehensive model quality evaluation",
            ),
            ("improve my score",),
        ),
        (
            "To improve the score I give on this paper, I would like more information "
            "on reward calibration and specification, an acknowledgement in the ethics "
            "statement about potential misuse and an answer to question 2.",
            (
                "reward calibration and specification",
                "ethics statement about potential misuse",
                "answer to question 2",
            ),
            ("score I give",),
        ),
    ],
)
def test_real_score_stances_keep_author_revision_conditions(
    sentence: str,
    retained: tuple[str, ...],
    removed: tuple[str, ...],
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "Historical score or decision statement removed" in cleaned
    for fragment in retained:
        assert fragment.casefold() in cleaned.casefold()
    for fragment in removed:
        assert fragment.casefold() not in cleaned.casefold()


@pytest.mark.parametrize(
    ("sentence", "retained"),
    [
        (
            "I am not an expert in L2D, but I feel that the model seems to be "
            "impractical.",
            "model seems to be impractical",
        ),
        (
            "I am afraid I am by no means an expert in this field so my review "
            "will have pretty low confidence, but I would love it if the authors "
            "could clarify the following.",
            "authors could clarify",
        ),
    ],
)
def test_reviewer_self_context_is_removed_without_hiding_critique(
    sentence: str,
    retained: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "Historical reviewer identity removed" in cleaned
    assert retained.casefold() in cleaned.casefold()
    assert "not an expert" not in cleaned.casefold()
    assert "low confidence" not in cleaned.casefold()


@pytest.mark.parametrize(
    ("sentence", "retained"),
    [
        (
            "The O(1/sqrt(n)) bound has a C^4 dependence, which was explicit "
            "in the previous version of the paper, but this issue is now hidden "
            "in big-O notation.",
            "issue is now hidden in big-O notation",
        ),
        (
            "Most reviewers at that time asked how omega is set and if there is "
            "a way to make it learnable.",
            "how omega is set",
        ),
    ],
)
def test_prior_review_context_is_removed_without_hiding_technical_issue(
    sentence: str,
    retained: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "Historical prior-submission detail removed" in cleaned
    assert retained.casefold() in cleaned.casefold()


def test_prior_version_reading_and_borderline_stance_are_removed() -> None:
    review_memory = _load_review_memory()
    text = (
        "I carefully read this new version. "
        "The seed variance is not reported. "
        "I therefore believe that it falls on the borderline."
    )

    cleaned = review_memory._redact_historical_review_text(text)

    assert "carefully read this new version" not in cleaned.casefold()
    assert "falls on the borderline" not in cleaned.casefold()
    assert "seed variance is not reported" in cleaned.casefold()
    assert "Historical prior-submission detail removed" in cleaned
    assert "Historical score or decision statement removed" in cleaned


def test_recommendation_to_authors_is_kept_as_revision_guidance() -> None:
    review_memory = _load_review_memory()
    sentence = (
        "My recommendation to the authors would be to include a much more "
        "substantive discussion of and comparison with prior work."
    )

    assert review_memory._redact_historical_review_text(sentence) == sentence

    that_form = (
        "My recommendation is that the authors revise the simulation framework "
        "and improve the empirical evaluation."
    )
    assert review_memory._redact_historical_review_text(that_form) == that_form


def test_author_guidance_survives_when_future_venue_phrase_is_removed() -> None:
    review_memory = _load_review_memory()
    sentence = (
        "As such, although the topic of the paper is interesting, my recommendation "
        "is that the authors revise their simulation framework to feature first "
        "principles simulations with LLMs as operators, focus on improving the quality "
        "of the empirical evaluation and the clarity of the paper, and submit to a "
        "future venue."
    )

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert (
        "revise their simulation framework to feature first principles simulations "
        "with LLMs as operators"
    ) in cleaned
    assert "improving the quality of the empirical evaluation and the clarity" in cleaned
    assert "submit to a future venue" not in cleaned
    assert "Historical venue-fit conclusion removed" in cleaned
    assert "paper [Historical venue-fit conclusion removed.]" in cleaned


@pytest.mark.parametrize(
    ("sentence", "retained", "removed"),
    [
        (
            "After comparing the current version with the earlier submission, I find "
            "that the presentation has significantly improved.",
            ("presentation has significantly improved",),
            ("current version", "earlier submission"),
        ),
        (
            "Could the authors provide details on what changes or improvements have "
            "been made in this version compared to the previous one?",
            ("authors provide details on what changes or improvements have been made",),
            ("previous one",),
        ),
        (
            "Unfortunately, it seems that the valuable comments raised by the reviewers "
            "at that time have not been addressed or incorporated into this version.",
            ("comments", "have not been addressed or incorporated"),
            ("reviewers at that time",),
        ),
        (
            "Unfortunately, some important weaknesses from the last reviews remain "
            "unresolved:",
            ("important weaknesses", "remain unresolved"),
            ("last reviews",),
        ),
        (
            "I reviewed this work from the previous conference cycle; the current draft "
            "shows clear progress with better exposition and richer experiments, so I "
            "inclined to support acceptance.",
            ("current draft shows clear progress", "better exposition and richer experiments"),
            ("reviewed this work", "previous conference cycle", "support acceptance"),
        ),
        (
            "Nevertheless, this is a minor comment and I admit that investigating this "
            "phenomenon is fully justified (I remember the answer from the authors "
            "regarding this point).",
            ("investigating this phenomenon is fully justified",),
            ("remember the answer from the authors",),
        ),
        (
            "There was however a major issue raised during the previous round of reviews, "
            "which convinced me that the paper was not ready for publication, even though "
            "I was rather positive in the first place.",
            ("major issue raised",),
            ("previous round of reviews", "not ready for publication", "rather positive"),
        ),
        (
            "- compared to a previous draft in Neurips 2025, a new SSD baseline is "
            "included in the experiments, which strengthens the experimental evaluation .",
            ("new SSD baseline is included", "strengthens the experimental evaluation"),
            ("previous draft", "Neurips 2025"),
        ),
        (
            "My primary and most significant concern with this work, which I also raised "
            "as a reviewer for a previous submission of this paper to NeurIPS 2025, is a "
            "fundamental ambiguity in the threat model.",
            ("primary and most significant concern", "fundamental ambiguity in the threat model"),
            ("as a reviewer", "previous submission", "NeurIPS 2025"),
        ),
        (
            "In my previous review, I noted that the experimental evaluation was "
            "insufficient, as several commonly used datasets were missing from the "
            "comparison.",
            (
                "experimental evaluation was insufficient",
                "commonly used datasets were missing from the comparison",
            ),
            ("my previous review",),
        ),
    ],
)
def test_prior_review_context_is_removed_without_losing_current_guidance(
    sentence: str,
    retained: tuple[str, ...],
    removed: tuple[str, ...],
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert (
        "Historical prior-submission detail removed" in cleaned
        or "Historical reviewer identity removed" in cleaned
    )
    for fragment in retained:
        assert fragment.casefold() in cleaned.casefold()
    for fragment in removed:
        assert fragment.casefold() not in cleaned.casefold()


def test_accepting_an_authors_technical_goal_is_not_a_review_verdict() -> None:
    review_memory = _load_review_memory()
    sentence = (
        'Even if we accept the authors\' new "process privacy" goal, they provide '
        "no evidence that their own method achieves it."
    )

    assert review_memory._redact_historical_review_text(sentence) == sentence


def test_model_version_number_is_not_mistaken_for_numeric_review_score() -> None:
    review_memory = _load_review_memory()
    sentence = (
        "I think the author could provide further experiments: if we only give "
        "the Gemini-2.5-pro clean inputs, what is the BiasNet output?"
    )

    assert review_memory._redact_historical_review_text(sentence) == sentence


@pytest.mark.parametrize(
    "sentence",
    [
        "The model can accept additional conditional inputs.",
        "The preference data contains chosen-rejected pairs.",
        (
            "Beyond pairwise (1 chosen, 1 rejected) multimodal DPO, the method "
            "also supports listwise preferences."
        ),
        (
            'The dataset contains "chosen" (safe) and "rejected" (unsafe) '
            "responses for preference learning."
        ),
        "The table reports chosen and rejected ratings for each preference pair.",
        (
            "The authors should report what proportion were accepted, rejected, "
            "or manually revised."
        ),
        "The method returns solutions that are accepted by the DFA.",
        "The method improves accepted lengths on the in-domain datasets.",
        "The method improves acceptance lengths on the in-domain datasets.",
        "The ablation reports Mean Accept Length and expected accept length.",
        "Feature regression improves step-wise acceptance.",
        "Feature regression improves stepwise acceptance.",
        "Efficiency depends on drafted-token acceptance.",
        "The DFA guides re-ranking through the automaton's acceptance structure.",
        "The procedure estimates the probability of this acceptance event.",
        "The estimator aggregates multiple acceptance events.",
        (
            "The action is accepted with a binary outcome (accept or reject) "
            "during sampling."
        ),
        "The sampler pseudorandomizes the accept/reject coin.",
        "The sampler uses pseudorandom acceptance during speculative decoding.",
        "Rejection sampling accepts trajectories drawn from the proposal.",
        "The MH acceptance step rejects low-probability proposals.",
        "The safety model rejects malicious queries.",
        "The model is aligned to reject the original malicious query.",
        "The privacy guard makes it reject the inference request.",
        "The adversarial optimizer appends a rejected incentive suffix.",
        "The defense is evaluated by rejecting privacy-inference attempts.",
        "Please clarify whether the verifier can reject all actions in a batch.",
        "Please clarify whether the verifier may reject every action in a batch.",
        "The guardrail rejects privacy-inference requests.",
        "The authors should publish a detailed breakdown of the dataset.",
        "The authors promised to publish their code.",
        "These works were published in 2025.",
        "Several works published on multimodal DPO are missing from the comparison.",
        "This obvious idea has been published with an ACL Anthology reference.",
        "The appendix compares published experiments with the new protocol.",
        "The comparison omits recently published or preprinted RL tool-use frameworks.",
        "The analysis studies how sentiment influences acceptance and rejection.",
        "The bound implies higher acceptance through Pinsker.",
        "The criteria are used for culling or acceptance at each stage.",
        "Draft tokens rejected by the verifier trigger recomputation.",
        "The trust-region method can accept or reject using a model-agreement ratio.",
        "The verifier risks accepting incorrect solutions.",
        "The safety layer learns a rejection policy and reports rejection rates.",
        "The DPO objective uses preferred minus rejected rewards.",
        "Acceptance criteria vary across conferences in this meta-science study.",
        (
            "The pipeline uses GPT-4o-mini for both initial rating and "
            "synthesis/evaluation tasks."
        ),
        "The work aligns with ICLR’s ethical standards.",
        (
            "The random track from the 2018 SAT competition may be better suited "
            "for this experiment."
        ),
    ],
)
def test_technical_decision_vocabulary_is_not_redacted(sentence: str) -> None:
    review_memory = _load_review_memory()

    assert review_memory._redact_historical_review_text(sentence) == sentence


@pytest.mark.parametrize(
    ("sentence", "retained"),
    [
        (
            "To strengthen its fit for ICLR, the authors could better articulate "
            "how the estimator generalizes beyond queues.",
            "authors could better articulate",
        ),
        (
            "The work may be more suitable for a more focused venue (e.g., a "
            "workshop) unless the authors articulate a broader connection.",
            "authors articulate a broader connection",
        ),
        (
            "While the method is sound, it may be more naturally aligned with "
            "the *computer vision* community (e.g., CVPR).",
            "method is sound",
        ),
        (
            "The writing needs substantial improvement to reach the typical bar "
            "for ICLR.",
            "writing needs substantial improvement",
        ),
        (
            "The paper is technically correct but lacks significance for "
            "publication at ICLR.",
            "lacks significance",
        ),
        (
            "In its current state, the work does not meet the standards of a "
            "theoretical contribution suitable for ICLR, primarily due to "
            "incomplete and potentially wrong proofs.",
            "incomplete and potentially wrong proofs",
        ),
        (
            "Given the focus on o-minimal structures, the paper is arguably a "
            "better fit for an optimization conference than a major DL conference "
            "like ICLR, where scalability is a crucial metric.",
            "scalability is a crucial metric",
        ),
        (
            "- **Overall fit (why this is a poor contribution for ICLR):** While "
            "the paper is useful for policy analysis, it reads as an LLM pipeline "
            "rather than a core methodological contribution.",
            "reads as an LLM pipeline",
        ),
    ],
)
def test_venue_conclusion_is_removed_but_author_guidance_survives(
    sentence: str,
    retained: str,
) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "Historical venue-fit conclusion removed" in cleaned
    assert retained.casefold() in cleaned.casefold()


@pytest.mark.parametrize(
    "sentence",
    [
        "Overall, I give a borderline reject recommendation.",
        "I cannot give an acceptance recommendation till now.",
        "I reject this paper because the control is missing.",
        "This paper should be published.",
        "My initial rating is 4.",
    ],
)
def test_true_review_verdicts_remain_redacted(sentence: str) -> None:
    review_memory = _load_review_memory()

    cleaned = review_memory._redact_historical_review_text(sentence)

    assert "Historical score or decision statement removed" in cleaned
