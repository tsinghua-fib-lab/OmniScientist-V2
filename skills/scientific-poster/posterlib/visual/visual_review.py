"""Reference-and-screenshot-bound visual review contracts for scientific posters.

The contract is deliberately provider-neutral. A harness may send the request to
its own image-capable model, while the portable runner verifies that the returned
verdict describes the exact HTML and screenshot under review.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import reference_seeds

REQUEST_SCHEMA = "scientific-poster.visual-review-request.v5"
RESULT_SCHEMA = "scientific-poster.visual-review-result.v5"
RECEIPT_SCHEMA = "scientific-poster.visual-review-receipt.v5"
EVIDENCE_SCHEMA = "scientific-poster.visual-evidence.v1"
MAX_VISUAL_REVISIONS = 2
CRITERIA = (
    "hierarchy",
    "information_structure",
    "figure_readability",
    "space_use",
    "poster_character",
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ISSUE_PRIORITIES = frozenset({"critical", "major", "minor"})
_ISSUE_OPERATIONS = frozenset({"restyle", "reflow", "content-replan"})
_RUBRIC = {
    "hierarchy": (
        "Is the central contribution and decisive evidence easy to locate through area, "
        "figure scale, position, typography, or contrast? Supporting evidence, provenance, "
        "and any supplied limitation should have an intentional relationship to it. One "
        "dominant focal region is valid, but several coordinated entry points are also valid "
        "when their relative importance remains clear."
    ),
    "information_structure": (
        "Can a conference viewer identify the main contribution, supporting method or "
        "evidence, and takeaways through a scan-first hierarchy without reading every "
        "paragraph? Are figures and equations self-explaining through nearby labels, "
        "captions, or interpretation? Are section boundaries immediately clear through at "
        "least one section-level cue, and does that cue read as belonging to the group "
        "rather than every module? Does prose area support rather than overpower figures, "
        "tables, equations, metric callouts, and method flows that already carry the "
        "scientific detail? Multiple optional reading paths are valid; do not "
        "require a single linear narrative or a problem-to-method-to-result sequence."
    ),
    "figure_readability": (
        "Are the important paper figures and equations large enough to read, selectively "
        "used, and integrated with nearby explanations?"
    ),
    "space_use": (
        "Is the page purposefully dense without large accidental voids, stretched "
        "low-information blocks, undersized figures, or cramped text? Judge whether empty "
        "space interrupts the actual reading flow or provides useful separation. Independent "
        "content groups or columns do not need aligned bottom edges; modest trailing "
        "whitespace is acceptable when the group itself is compact. Inspect interior gaps "
        "between vertically consecutive modules separately from the page-bottom margin: a "
        "small page-bottom margin can coexist with a disruptive internal void."
    ),
    "poster_character": (
        "Does the candidate read immediately as a recognisable academic conference poster, "
        "with a masthead, varied section treatment, purposeful density, readable figure "
        "and body-text scale at whole-page viewing distance, clear section boundaries, and "
        "hierarchy inside or across columns, without "
        "module-by-module card chrome? A web report, dashboard, slide, paper page, three plain "
        "equal text columns, equal repeated cards, indistinguishable full-height columns, "
        "full-width band stack, or large accidental voids are warning signs when they erase "
        "poster hierarchy; they are not automatic failures without visible harm."
    ),
}
_INSTRUCTIONS = (
    "Compare the complete candidate screenshot at full resolution with the supplied "
    "reference image. The reference is visual grammar only: compare structure, "
    "density, hierarchy, section treatment, figure scale, and poster character. "
    "MUST NOT transfer any reference text, numbers, logos, figures, data, claims, "
    "authors, affiliations, citations, equations, or venue identity. Judge the "
    "rendered visual result, not DOM metrics or author intent. Deterministic inspection "
    "measurements in the content brief are evidence, not verdicts: act on them only when "
    "their visible effect harms reading, hierarchy, or evidence legibility. Judge typography "
    "from the complete overview at fit-to-page scale, not from imagined zoom or a "
    "high-resolution crop. Compare typography_distribution measurements with "
    "content_brief.readability_reference and the reference image's type-to-page "
    "relationship. Body copy that is materially underscaled, uses overly long thin lines, "
    "or requires zoom to read is actionable, while the physical targets remain advisory "
    "evidence rather than an automatic threshold. For typography_distribution, never claim "
    "that a role meets its supplied physical target when difference_from_minimum_mm is "
    "negative. Such a role may still be accepted only when the reason names concrete visible "
    "full-page and reference-comparison evidence that justifies the exception. "
    "For a multi-panel source figure, judge internal axes, legends, labels, and annotations "
    "at whole-page scale; a large outer image box alone does not prove readability. When "
    "important internal marks disappear at fit-to-page scale, request reflow or a grounded "
    "content replan that enlarges, crops, or splits only the already bound source figure. "
    "Never replace its evidence. Never shrink "
    "already hard-to-read figures or body text to hide avoidable layout imbalance. When one "
    "zone visibly overflows while another has substantial usable space, prefer relocating "
    "intact modules or changing their spans before reducing legibility. Treat "
    "web-report, dashboard, "
    "slide, paper-page, repeated-card, plain equal-column, and accidental-void patterns as "
    "visual evidence rather than automatic verdicts; revise them when they visibly weaken "
    "conference-poster hierarchy, density, or scan flow. "
    "Do not require a focal module to span columns when figure scale, module depth, "
    "position, or typography already makes it dominant, and do not penalize compact "
    "independent rails solely because their bottom edges differ. When one lane ends "
    "conspicuously earlier than its neighbours and leaves a large uninterrupted rectangle "
    "that the bound reference does not use compositionally, treat the visible void as "
    "actionable; prefer reassigning or reordering intact modules over equal-height stretch, "
    "padding, or filler. On a dense poster, generic phrases such as natural content length, "
    "breathing room, transition of attention, or a natural stopping point do not identify a "
    "visible compositional purpose for a large terminal rectangle. Return "
    f"one exact {RESULT_SCHEMA} JSON object. Scores are diagnostic; no numeric score "
    "threshold decides the verdict. Do not require a linear narrative: judge whether the "
    "scan-first hierarchy supports useful optional reading paths. Return revise whenever "
    "a safe, executable repair would improve the rubric; pass means no actionable "
    "residual remains, and requires empty critical_issues and global_directives. A revise "
    "requires at least one concrete critical issue; keep global_directives empty unless "
    "a genuine whole-page preservation constraint is needed. If a "
    "source-intrinsic limitation cannot be safely changed, describe it in summary instead "
    "of inventing a repair. A module or figure measured outside the physical page cannot "
    "be visible in the delivered poster and must remain actionable until it fits. Use "
    "content-replan when available visual evidence already carries the detail but parallel "
    "paragraphs, bullets, captions, or callouts visibly repeat it and make the candidate "
    "read like a paper page. Do not request less science merely to create whitespace: retain "
    "the decisive claim, qualification, and evidence in their strongest communication "
    "channel. Use "
    "content-replan only to compress or reorganize already "
    "grounded copy and module emphasis; it never authorizes new facts, replacement figures, "
    "or new evidence. Use it when repetition or grounded copy volume prevents complete "
    "readable fit or weakens the reference-like scan hierarchy; it may accompany reflow when "
    "placement alone cannot solve the visible density. For "
    "content-replan, every target must be an exact existing module "
    "id from content_brief.grounded_authority.content_modules; never use a visual region "
    "such as middle rail, page, or masthead. Those region descriptions remain valid for "
    "restyle or reflow. Treat targets as observation anchors, not as a movement whitelist. "
    "Separate the visible diagnosis from the repair: desired_outcome and acceptance_check "
    "describe the whole-page end state. Prescribe an exact relocation only when both its "
    "source and destination capacity are visibly supported and the move will not create a "
    "new void or crowded zone; otherwise request a global reflow and let the authoring model "
    "choose placement. When the bound visual design names a dominant macro topology, compare "
    "it from the body entrance below the masthead; later lanes do not excuse a detached "
    "preliminary stage with unused side regions unless the reference or grounded geometry "
    "visibly supports that stage. When broad genuine spare capacity remains, judge whether "
    "the smallest body, caption, equation, or evidence scale should improve for conference "
    "reading distance before accepting the void; never request filler or stretched "
    "low-information blocks. Compare section treatment at the group level: flag repeated card chrome "
    "or one-band-per-module styling when the reference instead uses open content and shared "
    "section cues. Before returning revise, mentally apply the proposal across the whole "
    "page and reject repairs that merely transfer imbalance elsewhere."
)


class VisualReviewError(ValueError):
    """A visual-review request, result, or receipt is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sha256_file(path: str | Path) -> str:
    """Hash exact regular-file bytes."""

    candidate = _regular_path(path, label="file")
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def document_sha256(value: Mapping[str, Any]) -> str:
    """Hash one canonical JSON object."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def build_request(
    *,
    html_path: str | Path,
    screenshot_path: str | Path,
    reference: reference_seeds.ReferenceBundle | Mapping[str, Any],
    content_brief: Mapping[str, Any],
    visual_evidence: Mapping[str, Any],
    iteration: int = 0,
    max_iterations: int = MAX_VISUAL_REVISIONS,
) -> dict[str, Any]:
    """Build a provider-neutral request bound to candidate and reference bytes."""

    html = _regular_path(html_path, label="candidate HTML")
    screenshot = _regular_path(screenshot_path, label="poster screenshot")
    normalized_reference = _reference_object(reference)
    if isinstance(iteration, bool) or not isinstance(iteration, int):
        raise VisualReviewError("visual_review_invalid", "iteration must be an integer")
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        raise VisualReviewError(
            "visual_review_invalid", "max_iterations must be an integer"
        )
    if max_iterations != MAX_VISUAL_REVISIONS or not 0 <= iteration <= max_iterations:
        raise VisualReviewError(
            "visual_review_invalid",
            f"visual review supports iterations 0 through {MAX_VISUAL_REVISIONS}",
        )
    normalized_content_brief = _json_object(content_brief, label="content brief")
    normalized_evidence = _visual_evidence_object(visual_evidence)
    request: dict[str, Any] = {
        "schema": REQUEST_SCHEMA,
        "candidate_html_path": str(html),
        "candidate_html_sha256": sha256_file(html),
        "screenshot_path": str(screenshot),
        "screenshot_sha256": sha256_file(screenshot),
        "reference": normalized_reference,
        "reference_image_sha256": normalized_reference["image_sha256"],
        "visual_evidence": normalized_evidence,
        "visual_evidence_sha256": normalized_evidence["bundle_sha256"],
        "iteration": iteration,
        "max_iterations": max_iterations,
        "content_brief": normalized_content_brief,
        "rubric": dict(_RUBRIC),
        "instructions": _INSTRUCTIONS,
    }
    request["request_sha256"] = document_sha256(request)
    return request


def validate_result(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a VLM result and return a content-bound receipt."""

    normalized_request = _validate_request(request)
    raw_result = _json_object(result, label="visual review result")
    required = {
        "schema",
        "request_sha256",
        "candidate_html_sha256",
        "screenshot_sha256",
        "reference_image_sha256",
        "visual_evidence_sha256",
        "reviewer",
        "verdict",
        "scores",
        "summary",
        "strengths",
        "observation_assessments",
        "critical_issues",
        "global_directives",
    }
    _require_exact_fields(raw_result, required, label="visual review result")
    if raw_result["schema"] != RESULT_SCHEMA:
        raise VisualReviewError("visual_review_invalid", "unsupported result schema")
    for field in (
        "request_sha256",
        "candidate_html_sha256",
        "screenshot_sha256",
        "reference_image_sha256",
        "visual_evidence_sha256",
    ):
        if raw_result[field] != normalized_request[field]:
            label = field.replace("_sha256", "").replace("_", " ")
            raise VisualReviewError(
                "visual_review_invalid", f"visual review {label} binding does not match"
            )

    review = _review_body(
        raw_result,
        content_replan_module_ids=_grounded_module_ids(
            normalized_request["content_brief"]
        ),
        observation_ids=_observation_ids(normalized_request["visual_evidence"]),
    )

    iteration = int(normalized_request["iteration"])
    max_iterations = int(normalized_request["max_iterations"])
    quality_state = _quality_state(review["verdict"], iteration, max_iterations)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "request_sha256": normalized_request["request_sha256"],
        "candidate_html_sha256": normalized_request["candidate_html_sha256"],
        "screenshot_sha256": normalized_request["screenshot_sha256"],
        "reference_image_sha256": normalized_request["reference_image_sha256"],
        "visual_evidence_sha256": normalized_request["visual_evidence_sha256"],
        "iteration": iteration,
        "max_iterations": max_iterations,
        **review,
        "quality_state": quality_state,
    }
    receipt["receipt_sha256"] = document_sha256(receipt)
    return receipt


def load_request(path: str | Path) -> dict[str, Any]:
    """Load and validate one request JSON file."""

    return _validate_request(_load_json(path, label="visual review request"))


def validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an in-memory visual-review request object."""

    return _validate_request(value)


def load_receipt(
    path: str | Path,
    *,
    expected_html_sha256: str | None = None,
    expected_reference_image_sha256: str | None = None,
) -> dict[str, Any]:
    """Load a receipt and verify self-hash and optional content bindings."""

    return _validate_receipt(
        _load_json(path, label="visual review receipt"),
        expected_html_sha256=expected_html_sha256,
        expected_reference_image_sha256=expected_reference_image_sha256,
    )


def _validate_receipt(
    value: Mapping[str, Any],
    *,
    expected_html_sha256: str | None = None,
    expected_reference_image_sha256: str | None = None,
) -> dict[str, Any]:
    receipt = _json_object(value, label="visual review receipt")
    required = {
        "schema",
        "request_sha256",
        "candidate_html_sha256",
        "screenshot_sha256",
        "reference_image_sha256",
        "visual_evidence_sha256",
        "iteration",
        "max_iterations",
        "reviewer",
        "verdict",
        "scores",
        "summary",
        "strengths",
        "observation_assessments",
        "critical_issues",
        "global_directives",
        "quality_state",
        "receipt_sha256",
    }
    _require_exact_fields(receipt, required, label="visual review receipt")
    if receipt["schema"] != RECEIPT_SCHEMA:
        raise VisualReviewError("visual_review_invalid", "unsupported receipt schema")
    digest = str(receipt["receipt_sha256"] or "")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if _HASH_RE.fullmatch(digest) is None or digest != document_sha256(unsigned):
        raise VisualReviewError(
            "visual_review_invalid", "visual review receipt hash is invalid"
        )
    html_digest = str(receipt["candidate_html_sha256"] or "")
    screenshot_digest = str(receipt["screenshot_sha256"] or "")
    reference_digest = str(receipt["reference_image_sha256"] or "")
    evidence_digest = str(receipt["visual_evidence_sha256"] or "")
    if (
        _HASH_RE.fullmatch(html_digest) is None
        or _HASH_RE.fullmatch(screenshot_digest) is None
        or _HASH_RE.fullmatch(reference_digest) is None
        or _HASH_RE.fullmatch(evidence_digest) is None
    ):
        raise VisualReviewError(
            "visual_review_invalid", "visual review receipt bindings are invalid"
        )
    if _HASH_RE.fullmatch(str(receipt["request_sha256"] or "")) is None:
        raise VisualReviewError(
            "visual_review_invalid", "visual review request binding is invalid"
        )
    iteration = receipt["iteration"]
    max_iterations = receipt["max_iterations"]
    if (
        isinstance(iteration, bool)
        or not isinstance(iteration, int)
        or isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or max_iterations != MAX_VISUAL_REVISIONS
        or not 0 <= iteration <= max_iterations
    ):
        raise VisualReviewError(
            "visual_review_invalid", "visual review iteration is invalid"
        )
    review = _review_body(receipt)
    expected_state = _quality_state(review["verdict"], iteration, max_iterations)
    if receipt["quality_state"] != expected_state:
        raise VisualReviewError(
            "visual_review_invalid", "visual review quality state is inconsistent"
        )
    if expected_html_sha256 is not None and html_digest != expected_html_sha256:
        raise VisualReviewError(
            "visual_review_invalid",
            "visual review receipt does not match the candidate HTML",
        )
    if (
        expected_reference_image_sha256 is not None
        and reference_digest != expected_reference_image_sha256
    ):
        raise VisualReviewError(
            "visual_review_invalid",
            "visual review receipt does not match the reference image",
        )
    return receipt


def _review_body(
    value: Mapping[str, Any],
    *,
    content_replan_module_ids: frozenset[str] | None = None,
    observation_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Normalize the common reviewer verdict carried by results and receipts."""

    reviewer = _json_object(value.get("reviewer"), label="reviewer")
    _require_exact_fields(reviewer, {"provider", "model", "vision"}, label="reviewer")
    if reviewer["vision"] is not True:
        raise VisualReviewError(
            "visual_review_invalid", "visual review requires an image-capable reviewer"
        )
    normalized_reviewer = {
        "provider": _short_text(reviewer["provider"], label="reviewer provider"),
        "model": _short_text(reviewer["model"], label="reviewer model"),
        "vision": True,
    }
    raw_verdict = value.get("verdict")
    verdict = str(raw_verdict or "").strip().lower()
    if verdict not in {"pass", "revise"} or raw_verdict != verdict:
        raise VisualReviewError(
            "visual_review_invalid", "visual review verdict must be pass or revise"
        )
    scores = _scores(value.get("scores"))
    summary = _short_text(value.get("summary"), label="review summary", limit=2000)
    strengths = _text_list(value.get("strengths"), label="strengths", limit=8)
    assessments = _observation_assessments(
        value.get("observation_assessments"),
        expected_ids=observation_ids,
    )
    issues = _critical_issues(
        value.get("critical_issues"),
        content_replan_module_ids=content_replan_module_ids,
    )
    directives = _text_list(
        value.get("global_directives"), label="global_directives", limit=8
    )
    if verdict == "pass":
        if issues or directives:
            raise VisualReviewError(
                "visual_review_invalid",
                "a pass verdict cannot contain critical_issues or global_directives",
            )
        if any(item["judgment"] != "acceptable" for item in assessments):
            raise VisualReviewError(
                "visual_review_invalid",
                "a pass verdict requires every evidence observation to be acceptable",
            )
    else:
        if not issues:
            raise VisualReviewError(
                "visual_review_invalid",
                "a revise verdict requires at least one critical_issue",
            )
    return {
        "reviewer": normalized_reviewer,
        "verdict": verdict,
        "scores": scores,
        "summary": summary,
        "strengths": strengths,
        "observation_assessments": assessments,
        "critical_issues": issues,
        "global_directives": directives,
    }


def _quality_state(verdict: str, iteration: int, max_iterations: int) -> str:
    if verdict == "pass":
        return "passed"
    return "revision-required" if iteration < max_iterations else "failed"


def require_passing_receipt(
    path: str | Path,
    *,
    html_path: str | Path,
    screenshot_path: str | Path | None = None,
    reference: reference_seeds.ReferenceBundle | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Require a passing receipt for current candidate and optional bound assets."""

    reference_digest = None
    if reference is not None:
        reference_digest = str(_reference_object(reference)["image_sha256"])
    receipt = load_receipt(
        path,
        expected_html_sha256=sha256_file(html_path),
        expected_reference_image_sha256=reference_digest,
    )
    if screenshot_path is not None and receipt["screenshot_sha256"] != sha256_file(
        screenshot_path
    ):
        raise VisualReviewError(
            "visual_review_invalid",
            "visual review receipt does not match the screenshot",
        )
    if receipt["quality_state"] != "passed" or receipt["verdict"] != "pass":
        raise VisualReviewError(
            "visual_review_failed", "poster has no passing visual review"
        )
    return receipt


def revision_feedback(receipt: Mapping[str, Any]) -> str:
    """Convert one valid revise receipt into an executable authoring brief."""

    normalized = _validate_receipt(receipt)
    if normalized.get("quality_state") == "failed":
        raise VisualReviewError(
            "visual_review_failed", "visual revision limit has been reached"
        )
    if normalized.get("quality_state") != "revision-required":
        raise VisualReviewError(
            "visual_review_invalid", "receipt does not request a visual revision"
        )
    return _revision_feedback_text(normalized)


def continuation_feedback(receipt: Mapping[str, Any]) -> str:
    """Convert one exhausted revise receipt into a fresh-cycle repair brief."""

    normalized = _validate_receipt(receipt)
    if normalized.get("quality_state") != "failed":
        raise VisualReviewError(
            "visual_review_invalid",
            "continuation feedback requires an exhausted revise receipt",
        )
    return _revision_feedback_text(normalized)


def _revision_feedback_text(normalized: Mapping[str, Any]) -> str:
    """Format one already-validated revise receipt without changing its authority."""

    directives = _text_list(
        normalized.get("global_directives"), label="global_directives", limit=8
    )
    issues = _critical_issues(normalized.get("critical_issues"))
    lines = [
        "Overall visual assessment:",
        normalized["summary"],
        "Diagnostic scores:",
        *[
            f"- {criterion}: {normalized['scores'][criterion]}/5"
            for criterion in CRITERIA
        ],
        "Protect these accepted strengths:",
        *[f"- {strength}" for strength in normalized["strengths"]],
        "Evidence-bound observations:",
        *[
            f"- [{item['judgment']}] {item['observation_id']}: {item['reason']}"
            for item in normalized["observation_assessments"]
        ],
        "Preserve all scientific claims, source bindings, figures, page width and orientation. "
        "A host may adjust height only within an existing adaptive page plan; an explicit "
        "fixed page remains fixed.",
        "Use the reference as visual grammar only; never transfer its text, numbers, logos, "
        "figures, data, or identity.",
        "Treat issue targets as observation anchors, not a whitelist of movable modules. "
        "The visible evidence and whole-page acceptance outcome are authoritative; any "
        "suggested exact placement is advisory. Repack other intact modules when needed, "
        "and do not transfer an empty or crowded zone from one part of the page to another.",
        "Global directives:",
        *[f"- {directive}" for directive in directives],
        "Actionable repair tasks:",
        *[
            f"- [{item['priority']}] {item['criterion']} | targets: {', '.join(item['targets'])} "
            f"| operation: {item['operation']}\n"
            f"  Evidence: {item['evidence']}\n"
            f"  Desired outcome: {item['desired_outcome']}\n"
            f"  Acceptance check: {item['acceptance_check']}"
            for item in issues
        ],
    ]
    return "\n".join(lines)


def _validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = _json_object(value, label="visual review request")
    required = {
        "schema",
        "candidate_html_path",
        "candidate_html_sha256",
        "screenshot_path",
        "screenshot_sha256",
        "reference",
        "reference_image_sha256",
        "visual_evidence",
        "visual_evidence_sha256",
        "iteration",
        "max_iterations",
        "content_brief",
        "rubric",
        "instructions",
        "request_sha256",
    }
    _require_exact_fields(request, required, label="visual review request")
    if request["schema"] != REQUEST_SCHEMA:
        raise VisualReviewError("visual_review_invalid", "unsupported request schema")
    digest = str(request["request_sha256"] or "")
    unsigned = {key: value for key, value in request.items() if key != "request_sha256"}
    if _HASH_RE.fullmatch(digest) is None or digest != document_sha256(unsigned):
        raise VisualReviewError(
            "visual_review_invalid", "visual review request hash is invalid"
        )
    _bound_path_text(request["candidate_html_path"], label="candidate HTML path")
    _bound_path_text(request["screenshot_path"], label="screenshot path")
    for field in (
        "candidate_html_sha256",
        "screenshot_sha256",
        "reference_image_sha256",
        "visual_evidence_sha256",
    ):
        if _HASH_RE.fullmatch(str(request[field] or "")) is None:
            raise VisualReviewError("visual_review_invalid", f"{field} is invalid")
    normalized_reference = _reference_object(request["reference"])
    if request["reference"] != normalized_reference:
        raise VisualReviewError(
            "visual_review_invalid",
            "visual review reference must be an exact ReferenceBundle.to_dict object",
        )
    if request["reference_image_sha256"] != normalized_reference["image_sha256"]:
        raise VisualReviewError(
            "visual_review_invalid",
            "visual review reference image binding does not match its bundle",
        )
    normalized_evidence = _visual_evidence_object(request["visual_evidence"])
    if request["visual_evidence"] != normalized_evidence:
        raise VisualReviewError(
            "visual_review_invalid",
            "visual review evidence must be an exact visual-evidence bundle",
        )
    if request["visual_evidence_sha256"] != normalized_evidence["bundle_sha256"]:
        raise VisualReviewError(
            "visual_review_invalid",
            "visual review evidence binding does not match its bundle",
        )
    overview = normalized_evidence["overview"]
    if (
        overview.get("path") != request["screenshot_path"]
        or overview.get("sha256") != request["screenshot_sha256"]
    ):
        raise VisualReviewError(
            "visual_review_invalid",
            "visual evidence overview must bind the exact candidate screenshot",
        )
    if request["rubric"] != _RUBRIC:
        raise VisualReviewError(
            "visual_review_invalid", "visual review rubric was changed"
        )
    iteration = request["iteration"]
    max_iterations = request["max_iterations"]
    if (
        isinstance(iteration, bool)
        or not isinstance(iteration, int)
        or isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or max_iterations != MAX_VISUAL_REVISIONS
        or not 0 <= iteration <= max_iterations
    ):
        raise VisualReviewError(
            "visual_review_invalid", "visual review iteration is invalid"
        )
    _json_object(request["content_brief"], label="content brief")
    if request["instructions"] != _INSTRUCTIONS:
        raise VisualReviewError(
            "visual_review_invalid", "visual review instructions were changed"
        )
    return request


def _scores(value: Any) -> dict[str, int]:
    scores = _json_object(value, label="scores")
    _require_exact_fields(scores, set(CRITERIA), label="scores")
    normalized: dict[str, int] = {}
    for criterion in CRITERIA:
        score = scores[criterion]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise VisualReviewError(
                "visual_review_invalid", f"{criterion} score must be a number"
            )
        number = float(score)
        if not math.isfinite(number) or not number.is_integer() or not 1 <= number <= 5:
            raise VisualReviewError(
                "visual_review_invalid",
                f"{criterion} score must be an integer from 1 to 5",
            )
        normalized[criterion] = int(number)
    return normalized


def _critical_issues(
    value: Any,
    *,
    content_replan_module_ids: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 8:
        raise VisualReviewError(
            "visual_review_invalid",
            "critical_issues must be an array of at most 8 items",
        )
    issues: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise VisualReviewError(
                "visual_review_invalid", "critical issue must be an object"
            )
        item = dict(raw)
        _require_exact_fields(
            item,
            {
                "criterion",
                "priority",
                "targets",
                "evidence",
                "desired_outcome",
                "operation",
                "acceptance_check",
            },
            label="critical issue",
        )
        criterion = str(item["criterion"] or "").strip()
        if criterion not in CRITERIA:
            raise VisualReviewError(
                "visual_review_invalid", "critical issue criterion is invalid"
            )
        priority = str(item["priority"] or "").strip().lower()
        if priority not in _ISSUE_PRIORITIES or item["priority"] != priority:
            raise VisualReviewError(
                "visual_review_invalid", "critical issue priority is invalid"
            )
        operation = str(item["operation"] or "").strip().lower()
        if operation not in _ISSUE_OPERATIONS or item["operation"] != operation:
            raise VisualReviewError(
                "visual_review_invalid", "critical issue operation is invalid"
            )
        raw_targets = item["targets"]
        if not isinstance(raw_targets, list) or not 1 <= len(raw_targets) <= 4:
            raise VisualReviewError(
                "visual_review_invalid",
                "critical issue targets must contain 1 to 4 items",
            )
        targets = [
            _short_text(target, label="critical issue target", limit=120)
            for target in raw_targets
        ]
        if not targets:
            raise VisualReviewError(
                "visual_review_invalid",
                "critical issue targets must contain 1 to 4 items",
            )
        if operation == "content-replan" and (
            content_replan_module_ids is not None
            and any(target not in content_replan_module_ids for target in targets)
        ):
            raise VisualReviewError(
                "visual_review_invalid",
                "content-replan targets must be grounded module ids",
            )
        issues.append(
            {
                "criterion": criterion,
                "priority": priority,
                "targets": targets,
                "evidence": _short_text(
                    item["evidence"], label="critical issue evidence", limit=1000
                ),
                "desired_outcome": _short_text(
                    item["desired_outcome"],
                    label="critical issue desired_outcome",
                    limit=1000,
                ),
                "operation": operation,
                "acceptance_check": _short_text(
                    item["acceptance_check"],
                    label="critical issue acceptance_check",
                    limit=1000,
                ),
            }
        )
    return issues


def _grounded_module_ids(content_brief: Mapping[str, Any]) -> frozenset[str]:
    """Read the immutable module ids exposed to the visual reviewer."""

    authority = content_brief.get("grounded_authority")
    modules = (
        authority.get("content_modules") if isinstance(authority, Mapping) else None
    )
    if not isinstance(modules, list):
        return frozenset()
    return frozenset(
        str(module.get("id") or "").strip()
        for module in modules
        if isinstance(module, Mapping) and str(module.get("id") or "").strip()
    )


def _visual_evidence_object(value: Any) -> dict[str, Any]:
    bundle = _json_object(value, label="visual evidence")
    _require_exact_fields(
        bundle,
        {"schema", "overview", "observations", "crops", "atlas", "bundle_sha256"},
        label="visual evidence",
    )
    if bundle["schema"] != EVIDENCE_SCHEMA:
        raise VisualReviewError("visual_review_invalid", "unsupported evidence schema")
    _image_binding(bundle["overview"], label="evidence overview")
    observations = bundle["observations"]
    if not isinstance(observations, list):
        raise VisualReviewError(
            "visual_review_invalid", "evidence observations must be an array"
        )
    observation_ids = set(_observation_ids(bundle))
    crops = bundle["crops"]
    if not isinstance(crops, list):
        raise VisualReviewError(
            "visual_review_invalid", "evidence crops must be an array"
        )
    crop_ids: list[str] = []
    for index, crop in enumerate(crops):
        _image_binding(crop, label=f"evidence crop {index}")
        crop_id = _short_text(
            crop.get("observation_id") if isinstance(crop, Mapping) else None,
            label=f"evidence crop {index} observation id",
            limit=160,
        )
        if crop_id not in observation_ids:
            raise VisualReviewError(
                "visual_review_invalid",
                f"evidence crop {index} refers to an unknown observation",
            )
        crop_ids.append(crop_id)
    if len(crop_ids) != len(set(crop_ids)):
        raise VisualReviewError(
            "visual_review_invalid", "evidence crops must refer to unique observations"
        )
    if bundle["atlas"] is not None:
        _image_binding(bundle["atlas"], label="evidence atlas")
    digest = str(bundle["bundle_sha256"] or "")
    unsigned = {key: item for key, item in bundle.items() if key != "bundle_sha256"}
    if _HASH_RE.fullmatch(digest) is None or digest != document_sha256(unsigned):
        raise VisualReviewError(
            "visual_review_invalid", "visual evidence bundle hash is invalid"
        )
    return bundle


def _image_binding(value: Any, *, label: str) -> dict[str, Any]:
    binding = _json_object(value, label=label)
    if "path" not in binding or "sha256" not in binding:
        raise VisualReviewError(
            "visual_review_invalid", f"{label} requires path and sha256"
        )
    _bound_path_text(binding["path"], label=f"{label} path")
    if _HASH_RE.fullmatch(str(binding["sha256"] or "")) is None:
        raise VisualReviewError("visual_review_invalid", f"{label} hash is invalid")
    return binding


def _observation_ids(bundle: Mapping[str, Any]) -> tuple[str, ...]:
    raw = bundle.get("observations")
    if not isinstance(raw, list):
        raise VisualReviewError(
            "visual_review_invalid", "evidence observations must be an array"
        )
    ids: list[str] = []
    for item in raw:
        observation = _json_object(item, label="evidence observation")
        identifier = _short_text(
            observation.get("id"), label="evidence observation id", limit=160
        )
        ids.append(identifier)
    if len(ids) != len(set(ids)):
        raise VisualReviewError(
            "visual_review_invalid", "evidence observation ids must be unique"
        )
    return tuple(ids)


def _observation_assessments(
    value: Any,
    *,
    expected_ids: tuple[str, ...] | None,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise VisualReviewError(
            "visual_review_invalid", "observation_assessments must be an array"
        )
    normalized: list[dict[str, str]] = []
    for raw in value:
        item = _json_object(raw, label="observation assessment")
        _require_exact_fields(
            item,
            {"observation_id", "judgment", "reason"},
            label="observation assessment",
        )
        judgment = str(item["judgment"] or "").strip().lower()
        if judgment not in {"acceptable", "actionable", "uncertain"}:
            raise VisualReviewError(
                "visual_review_invalid", "observation assessment judgment is invalid"
            )
        normalized.append(
            {
                "observation_id": _short_text(
                    item["observation_id"],
                    label="observation assessment id",
                    limit=160,
                ),
                "judgment": judgment,
                "reason": _short_text(
                    item["reason"], label="observation assessment reason", limit=1000
                ),
            }
        )
    ids = [item["observation_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise VisualReviewError(
            "visual_review_invalid", "observation assessments must be unique"
        )
    if expected_ids is not None and set(ids) != set(expected_ids):
        raise VisualReviewError(
            "visual_review_invalid",
            "observation assessments must cover every evidence observation exactly once",
        )
    return normalized


def _reference_object(value: object) -> dict[str, Any]:
    """Return the exact JSON-safe representation of a current reference bundle."""

    try:
        if isinstance(value, reference_seeds.ReferenceBundle):
            raw = value.to_dict()
        elif isinstance(value, Mapping):
            raw = dict(value)
        else:
            raise reference_seeds.ReferenceSeedError(
                "reference must be a ReferenceBundle or its exact to_dict object"
            )
        bundle = reference_seeds.ReferenceBundle.from_dict(raw)
        normalized = bundle.to_dict()
        if raw != normalized:
            raise reference_seeds.ReferenceSeedError(
                "reference must be an exact ReferenceBundle.to_dict object"
            )
        return normalized
    except reference_seeds.ReferenceSeedError as exc:
        raise VisualReviewError(
            "visual_review_invalid", f"visual review reference is invalid: {exc}"
        ) from exc


def _text_list(value: Any, *, label: str, limit: int) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise VisualReviewError(
            "visual_review_invalid",
            f"{label} must be an array of at most {limit} items",
        )
    return [_short_text(item, label=label, limit=1000) for item in value]


def _short_text(value: Any, *, label: str, limit: int = 200) -> str:
    if not isinstance(value, str):
        raise VisualReviewError("visual_review_invalid", f"{label} must be a string")
    text = " ".join(value.split())
    if not text or len(text) > limit:
        raise VisualReviewError(
            "visual_review_invalid", f"{label} must contain 1 to {limit} characters"
        )
    return text


def _json_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise VisualReviewError("visual_review_invalid", f"{label} must be an object")
    normalized = dict(value)
    try:
        _canonical_json(normalized)
    except (TypeError, ValueError) as exc:
        raise VisualReviewError(
            "visual_review_invalid", f"{label} is not canonical JSON: {exc}"
        ) from exc
    return normalized


def _require_exact_fields(
    value: Mapping[str, Any], required: set[str], *, label: str
) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise VisualReviewError(
            "visual_review_invalid", f"{label} fields are invalid: {'; '.join(details)}"
        )


def _load_json(path: str | Path, *, label: str) -> dict[str, Any]:
    candidate = _regular_path(path, label=label)
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VisualReviewError(
            "visual_review_invalid", f"cannot read {label}: {exc}"
        ) from exc
    return _json_object(value, label=label)


def _regular_path(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise VisualReviewError(
            "visual_review_invalid", f"{label} may not be a symbolic link"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise VisualReviewError(
            "visual_review_invalid", f"cannot resolve {label}: {exc}"
        ) from exc
    if not resolved.is_file():
        raise VisualReviewError(
            "visual_review_invalid", f"{label} must be a regular file"
        )
    return resolved


def _bound_path_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VisualReviewError(
            "visual_review_invalid", f"{label} must be an absolute canonical path"
        )
    candidate = Path(value)
    if not candidate.is_absolute() or str(candidate.resolve(strict=False)) != value:
        raise VisualReviewError(
            "visual_review_invalid", f"{label} must be an absolute canonical path"
        )
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


__all__ = [
    "CRITERIA",
    "EVIDENCE_SCHEMA",
    "MAX_VISUAL_REVISIONS",
    "RECEIPT_SCHEMA",
    "REQUEST_SCHEMA",
    "RESULT_SCHEMA",
    "VisualReviewError",
    "build_request",
    "continuation_feedback",
    "document_sha256",
    "load_receipt",
    "load_request",
    "require_passing_receipt",
    "revision_feedback",
    "sha256_file",
    "validate_request",
    "validate_result",
]
