"""Poster-specific adapter from a bound visual request to Omni VLM feedback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import visual_review, vlm_client

_REVIEW_FIELDS = {
    "verdict",
    "scores",
    "summary",
    "strengths",
    "observation_assessments",
    "critical_issues",
    "global_directives",
}
_MAX_TRANSIENT_ATTEMPTS = 2
_RETRY_BASE_DELAY_S = 0.25

Sleep = Callable[[float], Awaitable[None]]


class VlmReviewResponseError(vlm_client.VlmError):
    """A malformed or contract-invalid review that may be regenerated."""


@dataclass(frozen=True)
class _BoundFile:
    path: Path
    content: bytes


async def review_request(
    request: Mapping[str, Any],
    *,
    client: vlm_client.VlmClient,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, Any]:
    """Review the exact reference and candidate bytes bound by a request."""

    try:
        bound = visual_review.validate_request(request)
    except (OSError, visual_review.VisualReviewError) as exc:
        raise vlm_client.VlmError(str(exc)) from exc

    _require_current_file(
        bound["candidate_html_path"],
        bound["candidate_html_sha256"],
        label="candidate HTML",
    )
    reference = _require_current_file(
        bound["reference"]["image_path"],
        bound["reference_image_sha256"],
        label="reference image",
    )
    overview = _require_current_file(
        bound["screenshot_path"], bound["screenshot_sha256"], label="screenshot"
    )
    evidence = bound["visual_evidence"]
    images: tuple[vlm_client.VlmImage, ...] = (
        vlm_client.VlmImage(
            label="REFERENCE IMAGE — use visual grammar only; do not copy its content.",
            image_bytes=reference.content,
            mime_type=_image_mime(reference.path, label="reference image"),
        ),
        vlm_client.VlmImage(
            label="CANDIDATE OVERVIEW — review the complete poster.",
            image_bytes=overview.content,
            mime_type=_image_mime(overview.path, label="candidate overview"),
        ),
    )
    if evidence["atlas"] is not None:
        atlas = _evidence_image(evidence["atlas"], label="evidence atlas")
        images += (
            vlm_client.VlmImage(
                label=(
                    "EVIDENCE ATLAS — labeled high-resolution crops for the "
                    "observation ids in the request."
                ),
                image_bytes=atlas.content,
                mime_type=_image_mime(atlas.path, label="evidence atlas"),
            ),
        )
    prompt = _review_prompt(bound)
    raw = await _generate_with_transient_retry(
        client,
        prompt,
        images=images,
        sleep=sleep,
    )
    try:
        return _validated_result(bound, raw, model=client.model)
    except VlmReviewResponseError as exc:
        repair_prompt = _review_repair_prompt(
            prompt,
            request=bound,
            validation_error=str(exc),
            previous_response=raw,
        )
    repaired_raw = await _generate_with_transient_retry(
        client,
        repair_prompt,
        images=images,
        sleep=sleep,
    )
    return _validated_result(bound, repaired_raw, model=client.model)


async def _generate_with_transient_retry(
    client: vlm_client.VlmClient,
    prompt: str,
    *,
    images: tuple[vlm_client.VlmImage, ...],
    sleep: Sleep,
) -> str:
    """Preserve bounded transport retries independently of schema repair."""

    for attempt in range(_MAX_TRANSIENT_ATTEMPTS):
        try:
            return await client.generate_json_text(prompt, images=images)
        except vlm_client.RetryableVlmError:
            if attempt == _MAX_TRANSIENT_ATTEMPTS - 1:
                raise
            await sleep(_RETRY_BASE_DELAY_S * (2**attempt))
    raise AssertionError("unreachable VLM transient retry state")


def parse_review_json(text: str) -> dict[str, Any]:
    """Parse a raw or fenced JSON object returned by the configured VLM."""

    candidate = text.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        candidate,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise VlmReviewResponseError(
            f"VLM review is not valid JSON: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise VlmReviewResponseError("VLM review must be a JSON object")
    return value


def _validated_result(
    request: Mapping[str, Any],
    raw: str,
    *,
    model: str,
) -> dict[str, Any]:
    review = parse_review_json(raw)
    missing = sorted(_REVIEW_FIELDS - set(review))
    extra = sorted(set(review) - _REVIEW_FIELDS)
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise VlmReviewResponseError(
            "VLM review fields are invalid: " + "; ".join(details)
        )
    result = {
        "schema": visual_review.RESULT_SCHEMA,
        "request_sha256": request["request_sha256"],
        "candidate_html_sha256": request["candidate_html_sha256"],
        "screenshot_sha256": request["screenshot_sha256"],
        "reference_image_sha256": request["reference_image_sha256"],
        "visual_evidence_sha256": request["visual_evidence_sha256"],
        "reviewer": {
            "provider": "omni-vlm",
            "model": model,
            "vision": True,
        },
        **review,
    }
    try:
        visual_review.validate_result(request, result)
    except visual_review.VisualReviewError as exc:
        raise VlmReviewResponseError(f"VLM returned an invalid review: {exc}") from exc
    return result


def _require_current_file(path: Any, expected_sha256: Any, *, label: str) -> _BoundFile:
    raw_path = Path(str(path)).expanduser()
    try:
        if raw_path.is_symlink():
            raise OSError("symbolic links are not allowed")
        candidate = raw_path.resolve(strict=True)
        if not candidate.is_file():
            raise OSError("not a regular file")
        content = candidate.read_bytes()
    except OSError as exc:
        raise vlm_client.VlmError(f"{label} is not a readable regular file") from exc
    actual = hashlib.sha256(content).hexdigest()
    if actual != str(expected_sha256):
        raise vlm_client.VlmError(f"{label} changed after the visual-review request")
    return _BoundFile(path=candidate, content=content)


def _evidence_image(value: Any, *, label: str) -> _BoundFile:
    if not isinstance(value, Mapping):
        raise vlm_client.VlmError(f"{label} binding is invalid")
    return _require_current_file(value.get("path"), value.get("sha256"), label=label)


def _image_mime(path: Path, *, label: str) -> str:
    image_mime = mimetypes.guess_type(path.name)[0] or ""
    if not image_mime.startswith("image/"):
        raise vlm_client.VlmError(f"{label} must have a recognized image type")
    return image_mime


def _review_semantic_shape(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact model-owned fields expected by the review validator."""

    return {
        "verdict": "pass or revise",
        "scores": {criterion: "integer 1-5" for criterion in visual_review.CRITERIA},
        "summary": "concise visual assessment",
        "strengths": ["observed visual strength"],
        "observation_assessments": [
            {
                "observation_id": str(item["id"]),
                "judgment": "acceptable, actionable, or uncertain",
                "reason": "visible explanation grounded in overview or evidence atlas",
            }
            for item in request["visual_evidence"]["observations"]
        ],
        "critical_issues": [],
        "global_directives": [],
    }


def _review_revise_issue_shape() -> dict[str, Any]:
    """Describe one issue element without making it part of the pass example."""

    return {
        "criterion": "one rubric key",
        "priority": "critical, major, or minor",
        "targets": [
            "1-4 items: exact grounded module ids for content-replan; existing "
            "module ids or visual regions for restyle/reflow"
        ],
        "evidence": "visible candidate-vs-reference evidence",
        "desired_outcome": "specific whole-page visible end state, not an unverified move",
        "operation": "restyle, reflow, or content-replan",
        "acceptance_check": "visible check for the repaired result",
    }


def _review_contract_guidance() -> tuple[str, ...]:
    """Explain verdict-dependent arrays without showing a contradictory pass shape."""

    return (
        "For pass, critical_issues and global_directives must both be empty arrays.",
        "For revise, critical_issues must contain at least one object with exactly "
        "this element structure:",
        json.dumps(_review_revise_issue_shape(), ensure_ascii=False, indent=2),
        "For revise, keep global_directives empty unless a genuine cross-task "
        "constraint or preservation directive is needed.",
    )


def _review_repair_prompt(
    original_prompt: str,
    *,
    request: Mapping[str, Any],
    validation_error: str,
    previous_response: str,
) -> str:
    """Request one contract repair while retaining the original evidence."""

    return "\n".join(
        [
            original_prompt,
            "",
            "CORRECTIVE RETRY: the previous JSON failed contract validation.",
            f"Exact validation error: {validation_error}",
            "Return one corrected JSON object only, with exactly this semantic shape:",
            json.dumps(_review_semantic_shape(request), ensure_ascii=False, indent=2),
            *_review_contract_guidance(),
            "Preserve valid visual judgments; repair only the contract-invalid fields.",
            "Previous invalid JSON:",
            vlm_client.bounded_response_excerpt(previous_response),
        ]
    )


def _review_prompt(request: Mapping[str, Any]) -> str:
    reference = request["reference"]
    content_brief = request["content_brief"]
    visual_design = content_brief.get("visual_design")
    visual_design = visual_design if isinstance(visual_design, Mapping) else {}
    reference_grammar = {
        "orientation": reference["orientation"],
        "density": reference["density"],
        "design_brief": reference["design_brief"],
        "non_authoritative_policy": reference["non_authoritative_policy"],
    }
    topology_grammar = {
        key: visual_design.get(key)
        for key in (
            "topology",
            "focal_strategy",
            "reading_path",
            "reference_observations",
            "directives",
        )
        if visual_design.get(key) is not None
    }
    return "\n".join(
        [
            "Act as a strict visual reviewer for a top-conference academic poster.",
            str(request["instructions"]),
            "The visual input is pixel-labeled. REFERENCE is style grammar only; CANDIDATE "
            "OVERVIEW is the poster being judged; EVIDENCE ATLAS contains candidate crops. "
            "These embedded labels are authoritative even when the reference contains "
            "unrelated paper text or figures.",
            "Treat the grounded candidate content brief as the only authority for scientific content.",
            "Judge only what is visibly present in the attached full-resolution images.",
            "Score every rubric criterion from 1 to 5.",
            "Assess every supplied evidence observation exactly once by its exact id. "
            "The measurements identify what to inspect, not whether it is aesthetically "
            "wrong. Mark each acceptable, actionable, or uncertain and explain the visible "
            "effect. A pass requires every observation to be acceptable.",
            "For inter_module_gap, lane_entry_offset, lane_depth_profile, and "
            "lane_trailing_space observations, "
            "inspect the complete overview and evidence crop to decide whether the separation "
            "supports hierarchy or interrupts a visual lane. A lane_entry_offset is a factual "
            "difference between the first module positions, not an automatic defect. When the "
            "grounded visual design names a dominant lane topology, several intended lanes "
            "starting far below an isolated module can instead reveal a detached pre-grid "
            "stage. Do not describe the later columns as topology-aligned unless the reference "
            "or visible content geometry supports that stage. "
            "A small page-trailing margin does not excuse a large accidental interior void, "
            "including one created when a full-width section cue waits below uneven modules. "
            "A lane_depth_profile crop spans the interval where a shallower lane has ended "
            "while peer lanes continue. If accepting it, identify the visible compositional "
            "purpose rather than citing unequal lane bottoms alone. This remains a visual "
            "judgment, not a numeric threshold or an equal-height rule.",
            "Apply the bound topology grammar below as explicit reference-specific context "
            "while following the request instructions; never infer a universal lane count.",
            "Return only one JSON object with exactly this semantic shape:",
            json.dumps(_review_semantic_shape(request), ensure_ascii=False, indent=2),
            *_review_contract_guidance(),
            "Reference visual-grammar metadata:",
            json.dumps(reference_grammar, ensure_ascii=False, indent=2, sort_keys=True),
            "Bound reference-derived topology grammar:",
            json.dumps(topology_grammar, ensure_ascii=False, indent=2, sort_keys=True),
            "Grounded poster content brief:",
            json.dumps(content_brief, ensure_ascii=False, indent=2, sort_keys=True),
            "Deterministic visual evidence observations:",
            json.dumps(
                request["visual_evidence"]["observations"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            "Rubric:",
            json.dumps(request["rubric"], ensure_ascii=False, indent=2, sort_keys=True),
        ]
    )


__all__ = [
    "VlmReviewResponseError",
    "parse_review_json",
    "review_request",
]
