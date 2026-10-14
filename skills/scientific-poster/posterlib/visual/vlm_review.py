"""Poster-specific adapter from a bound visual request to Omni VLM feedback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from . import review_contract, vlm_client

_REVIEW_FIELDS = {
    "verdict",
    "comparison",
    "scores",
    "summary",
    "strengths",
    "critical_issues",
    "global_directives",
}
_MAX_TRANSIENT_ATTEMPTS = 2
_RETRY_BASE_DELAY_S = 0.25
_EMBEDDED_VISUAL_MARKER = "[embedded visual omitted; inspect attached image]"
_MAX_OVERVIEW_EDGE_PX = 2048

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
    """Review the exact rendered candidate bytes bound by a request."""

    try:
        bound = review_contract.validate_request(request)
    except (OSError, review_contract.ReviewContractError) as exc:
        raise vlm_client.VlmError(str(exc)) from exc

    result, _receipt = await review_bound_request(bound, client=client, sleep=sleep)
    return result


async def review_bound_request(
    bound: Mapping[str, Any],
    *,
    client: vlm_client.VlmClient,
    sleep: Sleep = asyncio.sleep,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Review an entry-validated request and return its result and checked receipt."""

    _require_current_file(
        bound["candidate_html_path"],
        bound["candidate_html_sha256"],
        label="candidate HTML",
    )
    overview = _require_current_file(
        bound["screenshot_path"], bound["screenshot_sha256"], label="screenshot"
    )
    review_images: list[vlm_client.VlmImage] = []
    comparison_baseline = bound["content_brief"].get("comparison_baseline")
    if comparison_baseline is not None:
        if not isinstance(comparison_baseline, Mapping):
            raise vlm_client.VlmError("comparison baseline must be an image binding")
        baseline = _require_current_file(
            comparison_baseline["path"],
            comparison_baseline["sha256"],
            label="comparison baseline",
        )
        overview_edge = _image_edge_px(overview)
        baseline_edge = _image_edge_px(baseline)
        pair_scale = min(1.0, _MAX_OVERVIEW_EDGE_PX / max(overview_edge, baseline_edge))
        overview_bytes, overview_mime = _bounded_overview(
            overview, max_edge_px=max(1, round(overview_edge * pair_scale))
        )
        baseline_bytes, baseline_mime = _bounded_overview(
            baseline, max_edge_px=max(1, round(baseline_edge * pair_scale))
        )
        review_images.append(
            vlm_client.VlmImage(
                label=(
                    "INCUMBENT CANDIDATE — current best poster; compare the proposal "
                    "against this complete image."
                ),
                image_bytes=baseline_bytes,
                mime_type=baseline_mime,
            )
        )
    else:
        overview_bytes, overview_mime = _bounded_overview(overview)
    review_images.append(
        vlm_client.VlmImage(
            label=(
                "PROPOSED CANDIDATE — review the complete poster and decide whether "
                "it improves on the incumbent when one is supplied."
            ),
            image_bytes=overview_bytes,
            mime_type=overview_mime,
        )
    )
    quality_reference = bound["content_brief"].get("quality_reference")
    if quality_reference is not None:
        if not isinstance(quality_reference, Mapping) or set(quality_reference) != {"path", "sha256"}:
            raise vlm_client.VlmError("quality reference must bind an image path and its hash")
        reference = _require_current_file(
            quality_reference["path"], quality_reference["sha256"], label="quality reference",
        )
        reference_bytes, reference_mime = _bounded_overview(reference)
        review_images.append(vlm_client.VlmImage(
            label=(
                "QUALITY REFERENCE — communication-quality floor; compare hierarchy, "
                "visual explanation and finish without copying scientific content or layout."
            ),
            image_bytes=reference_bytes, mime_type=reference_mime,
        ))
    atlas_binding = bound["visual_evidence"].get("atlas")
    attach_detail_atlas = bool(
        isinstance(atlas_binding, Mapping)
        and getattr(client, "accepts_detail_atlas", False)
    )
    if attach_detail_atlas:
        assert isinstance(atlas_binding, Mapping)
        atlas = _require_current_file(
            atlas_binding["path"],
            atlas_binding["sha256"],
            label="evidence detail atlas",
        )
        review_images.append(
            vlm_client.VlmImage(
                label=(
                    "EVIDENCE DETAIL ATLAS — crops of candidate figures, not extra "
                    "poster content; never infer whole-page scale from this image."
                ),
                image_bytes=atlas.content,
                mime_type=_image_mime(atlas.path, label="evidence detail atlas"),
            )
        )
    images = tuple(review_images)
    prompt = _review_prompt(
        bound,
        detail_atlas_attached=attach_detail_atlas,
    )
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
    """Decode review syntax at the shared VLM boundary before domain validation."""

    try:
        return vlm_client.parse_json_object(text)
    except vlm_client.VlmError as exc:
        raise VlmReviewResponseError(str(exc)) from exc


def _validated_result(
    request: Mapping[str, Any],
    raw: str,
    *,
    model: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # The model owns semantic review fields; the host owns artifact provenance.
    # Ignore unused commentary fields instead of spending another vision call
    # repairing data no downstream consumer reads. Required values remain strict.
    review = {key: value for key, value in parse_review_json(raw).items() if key in _REVIEW_FIELDS}
    if isinstance(review.get("critical_issues"), list):
        review["critical_issues"] = [
            {key: value for key, value in issue.items() if key in {"criterion", "targets", "operation"}}
            if isinstance(issue, Mapping) else issue
            for issue in review["critical_issues"]
        ]
    if "verdict" not in review and review.get("critical_issues"):
        # Valid critical issues already preclude a pass. Keep that feedback
        # without repeating vision inference for a redundant decision field.
        # The shared result validator below still validates every issue; a pass
        # is never inferred, and explicit contradictory verdicts remain invalid.
        review["verdict"] = "revise"
    result = {
        "schema": review_contract.RESULT_SCHEMA,
        "request_sha256": request["request_sha256"],
        "candidate_html_sha256": request["candidate_html_sha256"],
        "screenshot_sha256": request["screenshot_sha256"],
        "visual_evidence_sha256": request["visual_evidence_sha256"],
        "reviewer": {
            "provider": "omni-vlm",
            "model": model,
            "vision": True,
        },
        **review,
    }
    try:
        receipt = review_contract.build_receipt(request, result)
    except review_contract.ReviewContractError as exc:
        raise VlmReviewResponseError(f"VLM returned an invalid review: {exc}") from exc
    return result, receipt


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


def _image_mime(path: Path, *, label: str) -> str:
    image_mime = mimetypes.guess_type(path.name)[0] or ""
    if not image_mime.startswith("image/"):
        raise vlm_client.VlmError(f"{label} must have a recognized image type")
    return image_mime


def _image_edge_px(bound: _BoundFile) -> int:
    try:
        with Image.open(BytesIO(bound.content)) as source:
            return max(source.size)
    except (OSError, UnidentifiedImageError) as exc:
        raise vlm_client.VlmError("poster overview is not a readable image") from exc


def _bounded_overview(
    bound: _BoundFile, *, max_edge_px: int = _MAX_OVERVIEW_EDGE_PX,
) -> tuple[bytes, str]:
    """Bound VLM pixel cost while the request still hashes the original screenshot."""

    mime_type = _image_mime(bound.path, label="candidate overview")
    try:
        with Image.open(BytesIO(bound.content)) as source:
            if max(source.size) <= max_edge_px:
                return bound.content, mime_type
            overview = source.convert("RGB")
            overview.thumbnail(
                (max_edge_px, max_edge_px),
                Image.Resampling.LANCZOS,
            )
            output = BytesIO()
            overview.save(output, format="PNG")
    except (OSError, UnidentifiedImageError) as exc:
        raise vlm_client.VlmError("candidate overview is not a readable image") from exc
    return output.getvalue(), "image/png"


def _review_semantic_shape(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact model-owned fields expected by the review validator."""

    return {
        "verdict": "pass or revise",
        "comparison": (
            "improved, unchanged, or regressed"
            if request["content_brief"].get("comparison_baseline") is not None
            else "initial"
        ),
        "scores": {criterion: 3 for criterion in review_contract.CRITERIA},
        "summary": "concise visual assessment",
        "strengths": ["observed visual strength"],
        "critical_issues": [],
        "global_directives": [],
    }


def _prompt_json(value: Any) -> str:
    """Serialize model context without repeating embedded visual bytes.

    The exact request remains content-addressed and unchanged for validation. The VLM
    already receives the candidate and evidence images, so inline
    ``data:`` payloads inside the scientific snapshot add transport and context cost but
    no review evidence.
    """

    return json.dumps(
        _without_embedded_visual_bytes(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _without_embedded_visual_bytes(value: Any) -> Any:
    """Return a JSON-shaped value with inline visual payloads replaced by a marker."""

    if isinstance(value, Mapping):
        return {
            str(key): _without_embedded_visual_bytes(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_without_embedded_visual_bytes(item) for item in value]
    if isinstance(value, str) and value.lstrip().lower().startswith("data:image/"):
        return _EMBEDDED_VISUAL_MARKER
    return value


def _review_revise_issue_shape() -> dict[str, Any]:
    """Describe one issue element without making it part of the pass example."""

    return {
        "criterion": "one rubric key",
        "targets": [
            (
                "exact grounded module ids; poster is also allowed only for a "
                "whole-page restyle or reflow"
            )
        ],
        "operation": "restyle, reflow, or content-replan",
    }


def _review_contract_guidance(request: Mapping[str, Any]) -> tuple[str, ...]:
    """Explain verdict-dependent arrays without showing a contradictory pass shape."""

    return (
        "Scores are JSON integers from 1 to 5 and describe the five rubric dimensions.",
        (
            "comparison must be improved, unchanged, or regressed. Judge the whole "
            "proposal against the whole incumbent; a local gain does not count as "
            "improved when another material dimension becomes worse."
            if request["content_brief"].get("comparison_baseline") is not None
            else "comparison must be initial because no incumbent is attached."
        ),
        "A pass has empty critical_issues and global_directives. A revise has at least one issue.",
        "Each issue has exactly this shape:",
        _prompt_json(_review_revise_issue_shape()),
        "The concise summary states the visible defect and reading impact. The author chooses "
        "the repair; do not prescribe coordinates, columns, rows or other layout topology.",
        "The host binds observations from the criterion and exact targets. Do not repeat "
        "observation ids or measurements in the response. Keep global_directives empty unless "
        "a necessary preservation constraint applies.",
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
            _prompt_json(_review_semantic_shape(request)),
            *_review_contract_guidance(request),
            "Preserve valid visual judgments; repair only the contract-invalid fields.",
            "Previous response, encoded as one JSON string (decode before correcting):",
            json.dumps(previous_response, ensure_ascii=False),
        ]
    )


def _review_prompt(
    request: Mapping[str, Any],
    *,
    detail_atlas_attached: bool = False,
) -> str:
    """Ask for one current-artifact judgment without replaying authoring history."""

    content_brief = request["content_brief"]
    review_content_brief = {
        key: content_brief[key]
        for key in ("page", "comparison_baseline_page", "rendered_inspection")
        if key in content_brief
    }
    rendered = review_content_brief.get("rendered_inspection")
    if isinstance(rendered, Mapping):
        review_content_brief["rendered_inspection"] = {
            "status": rendered.get("status"),
            "issues": [item for item in rendered.get("issues", [])
                       if isinstance(item, Mapping)],
        }
    authority = content_brief.get("grounded_authority")
    if isinstance(authority, Mapping):
        review_content_brief["grounded_authority"] = review_contract.module_binding_brief(
            authority
        )
    attached_image_guidance = "PROPOSED CANDIDATE is the poster being evaluated."
    if content_brief.get("comparison_baseline") is not None:
        attached_image_guidance += (
            " INCUMBENT CANDIDATE is the current best version. Compare both complete "
            "posters in this same judgment. Their overviews use one pixel scale; the "
            "declared incumbent and proposal page dimensions give their physical sizes. "
            "Mark improved only when the proposal makes "
            "material progress without a material regression elsewhere; unchanged or "
            "regressed proposals must not replace the incumbent."
        )
    if content_brief.get("quality_reference") is not None:
        attached_image_guidance += (
            " QUALITY REFERENCE follows: the user's reference poster sets the comparison "
            "standard for hierarchy, explanatory clarity, relative type scale and purposeful "
            "space use. A pass requires comparable visual communication quality, not merely "
            "an unclipped layout. Do not copy its scientific content, require identical "
            "columns or penalize a different successful design. Compare the ease of reading "
            "the contribution, method and evidence in each complete image."
        )
    if detail_atlas_attached:
        attached_image_guidance += " The final EVIDENCE DETAIL ATLAS only magnifies candidate regions."
    return "\n".join(
        [
            "Act as an independent visual reviewer for a top-conference academic poster.",
            str(request["instructions"]),
            attached_image_guidance,
            (
                "Judge legibility from the image at the declared physical page size. "
                "Compare title, body, table and figure-label scale at the declared "
                "physical page sizes. Equal displayed image height does not mean equal "
                "physical scale when the sheets have different dimensions; use measured "
                "font and painted-figure sizes in millimetres before judging legibility. A font "
                "that clears an advisory size in millimetres can still read too small "
                "relative to a large canvas; use the measured type-to-page ratios as "
                "context, not as automatic pass thresholds. "
                "No transcript is supplied: knowing the underlying words or numbers is not "
                "evidence that a visitor can read them. The source author owns scientific "
                "fidelity; this review owns visual communication. A failed rendered_inspection "
                "requires revise even if the visible portion looks good. Inspect the whole page "
                "for large unoccupied cells between content bands or beside spanning modules; "
                "page-wide content does not make an interior void intentional."
            ),
            "Return only one JSON object with exactly this semantic shape:",
            _prompt_json(_review_semantic_shape(request)),
            *_review_contract_guidance(request),
            "Grounded poster content brief:",
            _prompt_json(review_content_brief),
            "Deterministic visual evidence observations:",
            _prompt_json(request["visual_evidence"]["observations"]),
            "Rubric:",
            _prompt_json(request["rubric"]),
        ]
    )


__all__ = [
    "VlmReviewResponseError",
    "parse_review_json",
    "review_request",
]
