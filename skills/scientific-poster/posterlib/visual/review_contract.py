"""Candidate-and-screenshot-bound visual review contracts for scientific posters.

The contract is deliberately provider-neutral. A harness may send the request to
its own image-capable model, while the portable runner verifies that the returned
verdict describes the exact HTML and screenshot under review.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from posterlib.runtime.identifiers import is_sha256
from posterlib.visual.design_direction import GUIDANCE as DESIGN_GUIDANCE

REQUEST_SCHEMA = "scientific-poster.visual-review-request.v9"
RESULT_SCHEMA = "scientific-poster.visual-review-result.v9"
RECEIPT_SCHEMA = "scientific-poster.visual-review-receipt.v9"
EVIDENCE_SCHEMA = "scientific-poster.visual-evidence.v1"
CRITERIA = (
    "hierarchy",
    "information_structure",
    "figure_readability",
    "space_use",
    "poster_character",
)

_ISSUE_OPERATIONS = frozenset({"restyle", "reflow", "content-replan"})
_RUBRIC = {
    "hierarchy": (
        "Can a standing reader quickly locate the contribution and decisive evidence, with "
        "display emphasis distinct from comfortable continuous-reading body and table type?"
    ),
    "information_structure": (
        "Can a visitor grasp one central claim, its essential mechanism and decisive evidence "
        "by scanning the headings, explanations and visual carriers as connected groups, "
        "with qualifications available for closer reading? How much paragraph reading, "
        "equation decoding or table searching does that essential argument require? "
        "Does all visible copy, including the footer, serve the study or scholarly "
        "attribution? Identify any account of poster production occupying that space."
    ),
    "figure_readability": (
        "Are the important visual comparisons and equations interpretable at their actual "
        "physical size, with enough nearby explanation?"
    ),
    "space_use": (
        "Does spacing keep each heading, explanation and its figure or table perceptually "
        "connected, while separating distinct reader questions? Assess relationships within "
        "modules as well as between them and across the page."
    ),
    "poster_character": (
        "Does this read as a composed academic conference poster with a clear masthead, "
        "scanning hierarchy, selective visual synthesis, integrated source-figure surfaces "
        "and a clear distinction between the main argument and closer-reading detail? "
        "Does its canvas and framing follow the bound design requirements?"
    ),
}
_INSTRUCTIONS = (
    "Judge the current CANDIDATE OVERVIEW and measured physical geometry against "
    "the rubric. Make one concise assessment that covers every material blocker visible in "
    "this state; do not serialize obvious type, figure, and space defects across later rounds. "
    "Scores are rough diagnostics, not an optimization task; do not exhaustively discuss "
    "minor observations. "
    "The author already interpreted the reference and owns composition and source fidelity. "
    "Visual review does not certify scientific truth.\n\n"
    "Read the purpose of visible copy across the title band, body, captions and footer, "
    "including small print. Distinguish statements about the study from accounts of "
    "assembling, sourcing, checking or exporting this poster. The workflow owns the latter "
    "in separate production records. A visible account of poster production requires revise "
    "under information_structure with content-replan, even on a legible, well-composed page. "
    "Identify the affected passage and its purpose in the summary. Preserve author identity, "
    "bibliographic references, acknowledgments and required reproduction credits. Retain "
    "research provenance, experimental conditions and scientific qualifications when they "
    "share a paragraph with production notes. Judge meaning in context.\n\n"
    "Establish the candidate's absolute communication quality before comparing it with an "
    "incumbent. The verdict answers whether the candidate is ready as an academic poster; "
    "comparison answers whether it improved. An improved candidate can still require revision. "
    "In the summary, identify the visible focal relationship and how the essential mechanism "
    "and decisive comparison can be inspected from the image. Judge the actual carriers, "
    "including their connections, alignment, meaningful contrast and relative scale. "
    "A heading naming a mechanism or result establishes its topic; its visual organization "
    "must communicate the relationship. A sequence reduced to disconnected prose fragments "
    "or a decisive comparison buried in similarly weighted text warrants revision when it "
    "leaves the main argument dependent on sustained reading. Use reflow for a sound carrier "
    "whose visual organization fails, and content-replan when its selection or copy must change. "
    "Assess retained information, so text-only inputs can succeed through editable schematics "
    "and comparisons without a quota of source images.\n\n"
    "Use physical measurements for scale and the screenshot for reading impact. Measurements "
    "are evidence, not aesthetic thresholds. Judge type, contrast, title breaks, figure scale "
    "and space in their actual context. Independent groups may have different dimensions; "
    "a preferred alternative is not a defect. Empty or stretched panels do not compensate "
    "for unreadable scientific content. Evaluate a visitor standing about one metre from "
    "the printed page. The central claim, section headings, essential method and explanatory "
    "copy carrying the main argument must be readable in that setting; supporting detail "
    "may invite closer inspection. Use every rendered-inspection issue, including advisory "
    "measured type observations, together with the image. Read the module-scoped type "
    "distributions to locate small explanatory copy, table labels and equations. Judge "
    "essential expressions at their measured base size while allowing native mathematical "
    "script scaling. Necessary conditions and limitations retain their reading role in "
    "supporting and footer modules. Identify a material scale defect's module and reading "
    "impact in the summary and issue route.\n\n"
    "Assess typography by reading role. Display type establishes entry points and emphasis; "
    "body and table type support sustained reading and comparison. Compare their visual "
    "weight, width, spacing and rhythm in the screenshot. Heavy or compressed forms spread "
    "across extended prose can flatten emphasis and make lines harder to track even at a "
    "large measured size. Request restyle when that visible treatment materially burdens "
    "reading or obscures the distinction between headings and explanation. Judge the "
    "rendered relationship in context; the author chooses the font family and treatment.\n\n"
    "Trace the quick-scan argument from contribution through essential mechanism to decisive "
    "evidence. Headings that name those topics establish entry points; the connected "
    "explanations and visual carriers must also make their relationship easy to grasp. "
    "Assess that reading effort separately from type size and geometric fit. Sustained "
    "paragraph reading, decoding several equations, or searching exhaustive result tables "
    "can obscure the essential argument even on a fully legible page. Closer reading "
    "supplies necessary detail after that argument is apparent. Request "
    "content-replan for exact modules whose competing, repeated or secondary content obscures "
    "this hierarchy, including when the page has no overflow. This gives the scientific "
    "editor a reading-impact diagnosis for selecting or shortening content. Request reflow "
    "when the governing composition also needs to change.\n\n"
    "Judge whitespace by the information relationships it creates. Follow each heading to "
    "its explanation, figure or table, and reading key within the module. Compare the gaps "
    "inside that connected group with the separation between distinct groups. Spacing works "
    "when related material reads together and distinct questions remain distinguishable. "
    "Large internal gaps can detach a title, interpretation or caption from its evidence "
    "and force the reader to reconnect them across empty bands. A shared panel surface or "
    "content bounds reaching its edges establishes extent, not perceptual continuity. Locate "
    "the affected relationship in the complete screenshot and use bound extents to check its "
    "position; judge both internal separation and unoccupied page regions by their reading "
    "impact. Request reflow for a material disconnect, leaving the composition to the author. "
    "Purposeful breathing room supports grouping and emphasis; filler adds reading burden.\n\n"
    "When a QUALITY REFERENCE is supplied, it is the communication-quality floor on this "
    "rubric, not a layout template. Do not require its exact objects, styling or layout. "
    "Return revise when the candidate is clearly and materially weaker in scanning hierarchy, "
    "visual explanation or finish because of a correctable authoring choice, even if it remains "
    "legible and unclipped. A pass cannot simultaneously report a noticeable quality deficit "
    "against the reference. Mere stylistic difference with comparable communication quality is "
    "not a defect.\n\n"
    "Source figures are immutable images: they may be moved or resized intact, never cropped, "
    "redrawn or internally edited. Judge the important comparative pattern and its reading key; "
    "not every original tick label must match poster body type. Static validation owns module "
    "uniqueness and asset binding; report visible loss or clipping using the browser evidence.\n\n"
    "Assess source-figure background integration under poster_character. Inspect each "
    "image's outer field and edges against its containing panel, caption and neighboring "
    "page in the overview and detail crops. The measured surface_context runs from image "
    "element outward to poster; CSS backgrounds describe boxes, while embedded image "
    "backgrounds remain pixel evidence. A deliberate contrasting panel can provide clear "
    "grouping through coherent framing, spacing and alignment. An accidental seam or "
    "pasted-on island is actionable when it materially disrupts that grouping or finish. "
    "Judge the complete relationship: color difference alone is insufficient, and matching "
    "every surface is optional. Source immutability leaves the surrounding poster surfaces "
    "and framing under author control. Request restyle for a material integration defect, "
    "identify its module and visible impact, and leave the treatment to the author. Preserve "
    "the full source-image extent, bytes and colors, including intrinsic margins; image "
    "cropping, recoloring, background removal, filters, blend effects and masks fall outside "
    "this correction authority.\n\n"
    "Evaluate the bound design_requirements under poster_character. Use the same rules as "
    "composition:\n" + DESIGN_GUIDANCE + "\n\n"
    "A canvas tint, repeated decorative single-side borders or colored section cards "
    "outside those requirements warrants restyle, even when the poster remains readable. "
    "Identify the visible treatment and the applicable requirement. Honor explicit user "
    "or custom-reference overrides in their scope. Judge source-image colors separately "
    "from the surrounding page.\n\n"
    "Return revise for a specific material defect that impairs conference-scale reading, "
    "hides evidence, places production records in visible copy, breaks the composition "
    "or leaves the candidate materially below its "
    "quality reference; otherwise pass. A failed rendered inspection requires revise. "
    "Describe the defect and its reading impact, leaving layout choices to "
    "the author. Use restyle for appearance, reflow for placement or scale, content-replan for "
    "editorial selection or correction of visible copy. Target exact supplied module ids; "
    "use poster for copy outside module ownership or for whole-page restyle/reflow. "
    "The source-aware author resolves content ownership during drafting. "
    "Group symptoms with one shared correction into one issue. "
    "Return only the requested JSON."
)



class ReviewContractError(ValueError):
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
    reference_design_brief: str,
    content_brief: Mapping[str, Any],
    visual_evidence: Mapping[str, Any],
    iteration: int = 0,
) -> dict[str, Any]:
    """Build a provider-neutral request bound to the rendered candidate bytes."""

    html = _regular_path(html_path, label="candidate HTML")
    screenshot = _regular_path(screenshot_path, label="poster screenshot")
    design_brief = _optional_short_text(
        reference_design_brief,
        label="reference design brief",
        limit=12_000,
    )
    if isinstance(iteration, bool) or not isinstance(iteration, int):
        raise ReviewContractError("visual_review_invalid", "iteration must be an integer")
    if iteration < 0:
        raise ReviewContractError(
            "visual_review_invalid",
            "visual review iteration must be non-negative",
        )
    normalized_content_brief = _json_object(content_brief, label="content brief")
    _comparison_baseline(normalized_content_brief)
    normalized_evidence = _visual_evidence_object(visual_evidence)
    request: dict[str, Any] = {
        "schema": REQUEST_SCHEMA,
        "candidate_html_path": str(html),
        "candidate_html_sha256": sha256_file(html),
        "screenshot_path": str(screenshot),
        "screenshot_sha256": sha256_file(screenshot),
        "reference_design_brief": design_brief,
        "visual_evidence": normalized_evidence,
        "visual_evidence_sha256": normalized_evidence["bundle_sha256"],
        "iteration": iteration,
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

    return build_receipt(_validate_request(request), result)


def build_receipt(
    normalized_request: Mapping[str, Any], result: Mapping[str, Any],
) -> dict[str, Any]:
    """Check review semantics against a request already validated at entry."""

    raw_result = _json_object(result, label="visual review result")
    # Human-audit notes are optional; their omission cannot change the verdict.
    raw_result.setdefault("strengths", [])
    raw_result.setdefault("global_directives", [])
    raw_result.setdefault("scores", {})
    required = {
        "schema",
        "request_sha256",
        "candidate_html_sha256",
        "screenshot_sha256",
        "visual_evidence_sha256",
        "reviewer",
        "verdict",
        "comparison",
        "scores",
        "summary",
        "strengths",
        "critical_issues",
        "global_directives",
    }
    _require_exact_fields(raw_result, required, label="visual review result")
    if raw_result["schema"] != RESULT_SCHEMA:
        raise ReviewContractError("visual_review_invalid", "unsupported result schema")
    for field in (
        "request_sha256",
        "candidate_html_sha256",
        "screenshot_sha256",
        "visual_evidence_sha256",
    ):
        if raw_result[field] != normalized_request[field]:
            label = field.replace("_sha256", "").replace("_", " ")
            raise ReviewContractError(
                "visual_review_invalid", f"visual review {label} binding does not match"
            )

    baseline = _comparison_baseline(normalized_request["content_brief"])
    review = _review_body(
        raw_result,
        has_comparison_baseline=baseline is not None,
        # Review locations are diagnostic hints. Public scoped edits validate
        # their exact module authority when consuming the receipt.
        observations_by_id={
            str(observation["id"]): observation
            for observation in normalized_request["visual_evidence"]["observations"]
        },
    )
    rendered = normalized_request["content_brief"].get("rendered_inspection")
    if (
        isinstance(rendered, Mapping)
        and rendered.get("status") != "ok"
        and review["verdict"] == "pass"
    ):
        raise ReviewContractError(
            "visual_review_invalid",
            "The bound rendered inspection failed. Return revise with the visible/"
            "measured defects; visual approval cannot override delivery geometry.",
        )

    iteration = int(normalized_request["iteration"])
    quality_state = _quality_state(review["verdict"])
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "request_sha256": normalized_request["request_sha256"],
        "candidate_html_sha256": normalized_request["candidate_html_sha256"],
        "screenshot_sha256": normalized_request["screenshot_sha256"],
        "visual_evidence_sha256": normalized_request["visual_evidence_sha256"],
        "iteration": iteration,
        "comparison_baseline_sha256": (
            str(baseline["sha256"]) if baseline is not None else ""
        ),
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
) -> dict[str, Any]:
    """Load a receipt and verify self-hash and optional content bindings."""

    return _validate_receipt(
        _load_json(path, label="visual review receipt"),
        expected_html_sha256=expected_html_sha256,
    )


def _validate_receipt(
    value: Mapping[str, Any],
    *,
    expected_html_sha256: str | None = None,
) -> dict[str, Any]:
    receipt = _json_object(value, label="visual review receipt")
    required = {
        "schema",
        "request_sha256",
        "candidate_html_sha256",
        "screenshot_sha256",
        "visual_evidence_sha256",
        "iteration",
        "comparison_baseline_sha256",
        "reviewer",
        "verdict",
        "comparison",
        "scores",
        "summary",
        "strengths",
        "critical_issues",
        "global_directives",
        "quality_state",
        "receipt_sha256",
    }
    _require_exact_fields(receipt, required, label="visual review receipt")
    if receipt["schema"] != RECEIPT_SCHEMA:
        raise ReviewContractError("visual_review_invalid", "unsupported receipt schema")
    digest = str(receipt["receipt_sha256"] or "")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if not is_sha256(digest) or digest != document_sha256(unsigned):
        raise ReviewContractError(
            "visual_review_invalid", "visual review receipt hash is invalid"
        )
    html_digest = str(receipt["candidate_html_sha256"] or "")
    screenshot_digest = str(receipt["screenshot_sha256"] or "")
    evidence_digest = str(receipt["visual_evidence_sha256"] or "")
    if (
        not is_sha256(html_digest)
        or not is_sha256(screenshot_digest)
        or not is_sha256(evidence_digest)
    ):
        raise ReviewContractError(
            "visual_review_invalid", "visual review receipt bindings are invalid"
        )
    if not is_sha256(receipt["request_sha256"]):
        raise ReviewContractError(
            "visual_review_invalid", "visual review request binding is invalid"
        )
    iteration = receipt["iteration"]
    if (
        isinstance(iteration, bool)
        or not isinstance(iteration, int)
        or iteration < 0
    ):
        raise ReviewContractError(
            "visual_review_invalid", "visual review iteration is invalid"
        )
    baseline_sha256 = str(receipt["comparison_baseline_sha256"] or "")
    if baseline_sha256 and not is_sha256(baseline_sha256):
        raise ReviewContractError(
            "visual_review_invalid", "visual review comparison binding is invalid"
        )
    review = _review_body(
        receipt,
        has_comparison_baseline=bool(baseline_sha256),
    )
    expected_state = _quality_state(review["verdict"])
    if receipt["quality_state"] != expected_state:
        raise ReviewContractError(
            "visual_review_invalid", "visual review quality state is inconsistent"
        )
    if expected_html_sha256 is not None and html_digest != expected_html_sha256:
        raise ReviewContractError(
            "visual_review_invalid",
            "visual review receipt does not match the candidate HTML",
        )
    return receipt


def _review_body(
    value: Mapping[str, Any],
    *,
    has_comparison_baseline: bool = False,
    observations_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize the common reviewer verdict carried by results and receipts."""

    reviewer = _json_object(value.get("reviewer"), label="reviewer")
    _require_exact_fields(reviewer, {"provider", "model", "vision"}, label="reviewer")
    if reviewer["vision"] is not True:
        raise ReviewContractError(
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
        raise ReviewContractError(
            "visual_review_invalid", "visual review verdict must be pass or revise"
        )
    raw_comparison = value.get("comparison")
    comparison = str(raw_comparison or "").strip().lower()
    allowed_comparisons = (
        {"improved", "unchanged", "regressed"}
        if has_comparison_baseline
        else {"initial"}
    )
    if comparison not in allowed_comparisons or raw_comparison != comparison:
        raise ReviewContractError(
            "visual_review_invalid",
            "visual review comparison does not match the request baseline",
        )
    scores = _scores(value.get("scores"))
    summary = _short_text(value.get("summary"), label="review summary", limit=None)
    raw_strengths = value.get("strengths")
    strengths = [
        " ".join(item.split()) for item in raw_strengths
        if isinstance(item, str) and item.strip()
    ] if isinstance(raw_strengths, list) else []
    issues = _critical_issues(
        value.get("critical_issues"),
        observations_by_id=observations_by_id,
    )
    directives = _text_list(
        value.get("global_directives"), label="global_directives"
    )
    if verdict == "pass":
        if issues or directives:
            raise ReviewContractError(
                "visual_review_invalid",
                "a pass verdict cannot contain critical_issues or global_directives",
            )
        if has_comparison_baseline and comparison != "improved":
            raise ReviewContractError(
                "visual_review_invalid",
                "a revised candidate may pass only when it improves on the incumbent",
            )
    else:
        if not issues:
            raise ReviewContractError(
                "visual_review_invalid",
                "a revise verdict requires at least one critical_issue",
            )
    return {
        "reviewer": normalized_reviewer,
        "verdict": verdict,
        "comparison": comparison,
        "scores": scores,
        "summary": summary,
        "strengths": strengths,
        "critical_issues": issues,
        "global_directives": directives,
    }


def _quality_state(verdict: str) -> str:
    if verdict == "pass":
        return "passed"
    return "revision-required"


def format_revision_feedback(
    normalized: Mapping[str, Any],
) -> str:
    """Format a receipt already normalized by a review boundary."""

    if normalized.get("quality_state") != "revision-required":
        raise ReviewContractError(
            "visual_review_invalid", "receipt does not request a visual revision"
        )
    issues = normalized["critical_issues"]
    summary = str(normalized.get("summary") or "").strip()
    scores = _scores(normalized.get("scores"))
    lines = [
        "Visual-review routing for the current editor session:",
        (
            "The reviewer summary diagnoses the visible current-state defect; it does "
            "not own the repair. Choose the repair from the current HTML, user direction, "
            "issue route, and bound deterministic observations; do not adopt a "
            "reviewer-suggested destination or topology."
        ),
        "Visible-defect summary from the reviewed screenshot:",
        summary,
        *(["Diagnostic scores:", *(
            f"- {criterion}: {score}/5" for criterion, score in scores.items()
        )] if scores else []),
        (
            "Preserve all scientific claims, source bindings, figures, and the exact "
            "rendered physical page dimensions. Recompose inside that canvas."
        ),
        (
            "Treat issue targets as grounded diagnostic owners, not a whitelist of movable modules. "
            "The visible evidence and whole-page acceptance outcome are authoritative; any "
            "suggested exact placement is advisory. Repack other intact modules when needed, "
            "and do not transfer an empty or crowded zone from one part of the page to another."
        ),
        "Actionable issue routes:",
        *[
            f"- {item['criterion']} | targets: {', '.join(item['targets'])} "
            f"| operation: {item['operation']}\n"
            "  Bound observations: "
            f"{json.dumps(item['evidence'], ensure_ascii=False, sort_keys=True)}"
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
        "reference_design_brief",
        "visual_evidence",
        "visual_evidence_sha256",
        "iteration",
        "content_brief",
        "rubric",
        "instructions",
        "request_sha256",
    }
    _require_exact_fields(request, required, label="visual review request")
    if request["schema"] != REQUEST_SCHEMA:
        raise ReviewContractError("visual_review_invalid", "unsupported request schema")
    digest = str(request["request_sha256"] or "")
    unsigned = {key: value for key, value in request.items() if key != "request_sha256"}
    if not is_sha256(digest) or digest != document_sha256(unsigned):
        raise ReviewContractError(
            "visual_review_invalid", "visual review request hash is invalid"
        )
    _bound_path_text(request["candidate_html_path"], label="candidate HTML path")
    _bound_path_text(request["screenshot_path"], label="screenshot path")
    for field in (
        "candidate_html_sha256",
        "screenshot_sha256",
        "visual_evidence_sha256",
    ):
        if not is_sha256(request[field]):
            raise ReviewContractError("visual_review_invalid", f"{field} is invalid")
    normalized_evidence = _visual_evidence_object(request["visual_evidence"])
    if request["reference_design_brief"] != _optional_short_text(
        request["reference_design_brief"],
        label="reference design brief",
        limit=12_000,
    ):
        raise ReviewContractError(
            "visual_review_invalid", "reference design brief is not normalized"
        )
    if request["visual_evidence"] != normalized_evidence:
        raise ReviewContractError(
            "visual_review_invalid",
            "visual review evidence must be an exact visual-evidence bundle",
        )
    if request["visual_evidence_sha256"] != normalized_evidence["bundle_sha256"]:
        raise ReviewContractError(
            "visual_review_invalid",
            "visual review evidence binding does not match its bundle",
        )
    overview = normalized_evidence["overview"]
    if (
        overview.get("path") != request["screenshot_path"]
        or overview.get("sha256") != request["screenshot_sha256"]
    ):
        raise ReviewContractError(
            "visual_review_invalid",
            "visual evidence overview must bind the exact candidate screenshot",
        )
    if request["rubric"] != _RUBRIC:
        raise ReviewContractError(
            "visual_review_invalid", "visual review rubric was changed"
        )
    iteration = request["iteration"]
    if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 0:
        raise ReviewContractError(
            "visual_review_invalid", "visual review iteration is invalid"
        )
    content_brief = _json_object(request["content_brief"], label="content brief")
    _comparison_baseline(content_brief)
    if request["instructions"] != _INSTRUCTIONS:
        raise ReviewContractError(
            "visual_review_invalid", "visual review instructions were changed"
        )
    return request


def _scores(value: Any) -> dict[str, int | float]:
    """Retain available numeric observations without making them a verdict gate."""

    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, int | float] = {}
    for criterion in CRITERIA:
        score = value.get(criterion)
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        number = float(score)
        if math.isfinite(number) and 1 <= number <= 5:
            normalized[criterion] = int(number) if number.is_integer() else number
    return normalized


def _critical_issues(
    value: Any,
    *,
    observations_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ReviewContractError(
            "visual_review_invalid",
            "critical_issues must be an array",
        )
    issues: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ReviewContractError(
                "visual_review_invalid", "critical issue must be an object"
            )
        item = dict(raw)
        model_supplied_observation_ids = (
            observations_by_id is not None and "observation_ids" in item
        )
        required_fields = {"criterion", "targets", "operation"}
        if observations_by_id is None or model_supplied_observation_ids:
            required_fields.add("observation_ids")
        if observations_by_id is None:
            required_fields.add("evidence")
        _require_exact_fields(item, required_fields, label="critical issue")
        criterion = str(item["criterion"] or "").strip()
        if criterion not in CRITERIA:
            raise ReviewContractError(
                "visual_review_invalid", "critical issue criterion is invalid"
            )
        operation = str(item["operation"] or "").strip().lower()
        if operation not in _ISSUE_OPERATIONS or item["operation"] != operation:
            raise ReviewContractError(
                "visual_review_invalid", "critical issue operation is invalid"
            )
        raw_targets = item["targets"]
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ReviewContractError(
                "visual_review_invalid",
                "critical issue targets must be a non-empty array",
            )
        targets = list(
            dict.fromkeys(
                _short_text(target, label="critical issue target", limit=120)
                for target in raw_targets
            )
        )
        if observations_by_id is not None:
            if model_supplied_observation_ids:
                raw_observation_ids = item["observation_ids"]
                if not isinstance(raw_observation_ids, list):
                    raise ReviewContractError(
                        "visual_review_invalid",
                        "critical issue observation_ids must be an array",
                    )
                observation_ids = list(
                    dict.fromkeys(
                        _short_text(
                            observation_id,
                            label="critical issue observation id",
                            limit=200,
                        )
                        for observation_id in raw_observation_ids
                    )
                )
                unknown = [
                    observation_id
                    for observation_id in observation_ids
                    if observation_id not in observations_by_id
                ]
                if unknown:
                    raise ReviewContractError(
                        "visual_review_invalid",
                        "critical issue references unknown visual observations: "
                        + ", ".join(unknown),
                    )
                evidence = [
                    dict(observations_by_id[observation_id])
                    for observation_id in observation_ids
                ]
            else:
                evidence = _issue_observations(
                    criterion=criterion,
                    targets=targets,
                    observations=observations_by_id.values(),
                )
                observation_ids = [
                    str(observation.get("id") or "") for observation in evidence
                ]
        else:
            raw_observation_ids = item["observation_ids"]
            if not isinstance(raw_observation_ids, list):
                raise ReviewContractError(
                    "visual_review_invalid",
                    "critical issue observation_ids must be an array",
                )
            observation_ids = list(
                dict.fromkeys(
                    _short_text(
                        observation_id,
                        label="critical issue observation id",
                        limit=200,
                    )
                    for observation_id in raw_observation_ids
                )
            )
            raw_evidence = item["evidence"]
            if not isinstance(raw_evidence, list) or not all(
                isinstance(observation, Mapping) for observation in raw_evidence
            ):
                raise ReviewContractError(
                    "visual_review_invalid",
                    "critical issue evidence must be bound observation objects",
                )
            evidence = [dict(observation) for observation in raw_evidence]
            if [str(observation.get("id") or "") for observation in evidence] != observation_ids:
                raise ReviewContractError(
                    "visual_review_invalid",
                    "critical issue evidence does not match observation_ids",
                )
        issues.append(
            {
                "criterion": criterion,
                "targets": targets,
                "observation_ids": observation_ids,
                "evidence": evidence,
                "operation": operation,
            }
        )
    return issues


def _issue_observations(
    *,
    criterion: str,
    targets: list[str],
    observations: Any,
) -> list[dict[str, Any]]:
    """Bind rendered facts by issue meaning and measured ownership."""

    relevant_kinds = {
        "information_structure": {"rendered_copy"},
        "figure_readability": {
            "source_figure_extent", "native_carrier_extent", "typography_distribution",
        },
        "poster_character": {"source_figure_extent", "native_carrier_extent"},
        "space_use": {
            "module_content_extent",
            "container_trailing_space",
            "flex_row_peer_trailing_space",
            "inter_module_gap",
            "module_internal_trailing_space",
            "page_trailing_space",
        },
        "hierarchy": {"typography_distribution"},
    }.get(criterion, set())
    if not relevant_kinds:
        return []
    poster_target = "poster" in targets
    target_set = set(targets)
    matched: list[dict[str, Any]] = []
    for raw in observations:
        if not isinstance(raw, Mapping) or raw.get("kind") not in relevant_kinds:
            continue
        module_id = str(raw.get("module_id") or "")
        if raw.get("kind") == "rendered_copy":
            if (poster_target and not module_id) or (
                module_id != "poster" and module_id in target_set
            ):
                matched.append(dict(raw))
            continue
        module_ids = {
            str(value)
            for value in raw.get("module_ids") or []
            if str(value or "")
        }
        if (
            poster_target
            or (
                raw.get("kind") == "typography_distribution"
                and not module_id and not module_ids
            )
            or (criterion == "space_use" and raw.get("kind") == "page_trailing_space")
            or module_id in target_set
            or module_ids & target_set
        ):
            matched.append(dict(raw))
    return matched


def module_binding_brief(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expose selection bindings, never obsolete copy from an initial content plan."""

    modules = value.get("content_modules")
    return {
        "content_modules": [
            {
                key: module.get(key)
                for key in (
                    "id", "semantic_roles", "priority", "visual_kind", "source_label", "figure_sha256s"
                )
            }
            for module in modules if isinstance(module, Mapping)
        ] if isinstance(modules, list) else [],
    }


def _visual_evidence_object(value: Any) -> dict[str, Any]:
    bundle = _json_object(value, label="visual evidence")
    _require_exact_fields(
        bundle,
        {"schema", "overview", "observations", "crops", "atlas", "bundle_sha256"},
        label="visual evidence",
    )
    if bundle["schema"] != EVIDENCE_SCHEMA:
        raise ReviewContractError("visual_review_invalid", "unsupported evidence schema")
    _image_binding(bundle["overview"], label="evidence overview")
    observations = bundle["observations"]
    if not isinstance(observations, list):
        raise ReviewContractError(
            "visual_review_invalid", "evidence observations must be an array"
        )
    observation_ids = set(_observation_ids(bundle))
    crops = bundle["crops"]
    if not isinstance(crops, list):
        raise ReviewContractError(
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
            raise ReviewContractError(
                "visual_review_invalid",
                f"evidence crop {index} refers to an unknown observation",
            )
        crop_ids.append(crop_id)
    if len(crop_ids) != len(set(crop_ids)):
        raise ReviewContractError(
            "visual_review_invalid", "evidence crops must refer to unique observations"
        )
    if bundle["atlas"] is not None:
        _image_binding(bundle["atlas"], label="evidence atlas")
    digest = str(bundle["bundle_sha256"] or "")
    unsigned = {key: item for key, item in bundle.items() if key != "bundle_sha256"}
    if not is_sha256(digest) or digest != document_sha256(unsigned):
        raise ReviewContractError(
            "visual_review_invalid", "visual evidence bundle hash is invalid"
        )
    return bundle


def _image_binding(value: Any, *, label: str) -> dict[str, Any]:
    binding = _json_object(value, label=label)
    if "path" not in binding or "sha256" not in binding:
        raise ReviewContractError(
            "visual_review_invalid", f"{label} requires path and sha256"
        )
    _bound_path_text(binding["path"], label=f"{label} path")
    if not is_sha256(binding["sha256"]):
        raise ReviewContractError("visual_review_invalid", f"{label} hash is invalid")
    return binding


def _observation_ids(bundle: Mapping[str, Any]) -> tuple[str, ...]:
    raw = bundle.get("observations")
    if not isinstance(raw, list):
        raise ReviewContractError(
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
        raise ReviewContractError(
            "visual_review_invalid", "evidence observation ids must be unique"
        )
    return tuple(ids)


def _text_list(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ReviewContractError(
            "visual_review_invalid",
            f"{label} must be an array",
        )
    return [_short_text(item, label=label, limit=None) for item in value]


def _short_text(value: Any, *, label: str, limit: int | None = 200) -> str:
    if not isinstance(value, str):
        raise ReviewContractError("visual_review_invalid", f"{label} must be a string")
    text = " ".join(value.split())
    if not text or (limit is not None and len(text) > limit):
        raise ReviewContractError(
            "visual_review_invalid",
            f"{label} must contain 1 to {limit} characters" if limit is not None
            else f"{label} must contain text",
        )
    return text


def _optional_short_text(value: Any, *, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ReviewContractError("visual_review_invalid", f"{label} must be a string")
    text = " ".join(value.split())
    if len(text) > limit:
        raise ReviewContractError(
            "visual_review_invalid", f"{label} must contain at most {limit} characters"
        )
    return text


def _json_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewContractError("visual_review_invalid", f"{label} must be an object")
    normalized = dict(value)
    try:
        _canonical_json(normalized)
    except (TypeError, ValueError) as exc:
        raise ReviewContractError(
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
        raise ReviewContractError(
            "visual_review_invalid", f"{label} fields are invalid: {'; '.join(details)}"
        )


def _load_json(path: str | Path, *, label: str) -> dict[str, Any]:
    candidate = _regular_path(path, label=label)
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewContractError(
            "visual_review_invalid", f"cannot read {label}: {exc}"
        ) from exc
    return _json_object(value, label=label)


def _regular_path(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ReviewContractError(
            "visual_review_invalid", f"{label} may not be a symbolic link"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ReviewContractError(
            "visual_review_invalid", f"cannot resolve {label}: {exc}"
        ) from exc
    if not resolved.is_file():
        raise ReviewContractError(
            "visual_review_invalid", f"{label} must be a regular file"
        )
    return resolved


def _bound_path_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewContractError(
            "visual_review_invalid", f"{label} must be an absolute canonical path"
        )
    candidate = Path(value)
    if not candidate.is_absolute() or str(candidate.resolve(strict=False)) != value:
        raise ReviewContractError(
            "visual_review_invalid", f"{label} must be an absolute canonical path"
        )
    return value


def _comparison_baseline(content_brief: Mapping[str, Any]) -> dict[str, str] | None:
    """Validate the optional incumbent screenshot used for pairwise review."""

    value = content_brief.get("comparison_baseline")
    if value is None:
        return None
    baseline = _json_object(value, label="comparison baseline")
    _require_exact_fields(baseline, {"path", "sha256"}, label="comparison baseline")
    path = _bound_path_text(baseline["path"], label="comparison baseline path")
    digest = str(baseline["sha256"] or "")
    if not is_sha256(digest):
        raise ReviewContractError(
            "visual_review_invalid", "comparison baseline hash is invalid"
        )
    return {"path": path, "sha256": digest}


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
    "RECEIPT_SCHEMA",
    "REQUEST_SCHEMA",
    "RESULT_SCHEMA",
    "ReviewContractError",
    "build_request",
    "document_sha256",
    "load_receipt",
    "load_request",
    "sha256_file",
    "validate_request",
    "validate_result",
]
