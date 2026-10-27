"""Explicit acceptance records for one self-contained poster HTML version."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import poster_contract

from posterlib.runtime import runtime_io
from posterlib.visual import review_contract


class ApprovalError(ValueError):
    """An acceptance request cannot identify or preserve its selected version."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PosterApproval:
    """Paths and decision for a saved HTML version and its optional review."""

    approval_path: Path
    html_path: Path
    visual_review_path: Path | None
    receipt: dict[str, Any]


def create_poster_approval(
    *,
    source_html_path: str | Path,
    source_html_sha256: str,
    output_dir: str | Path,
    approved: object,
    visual_review_path: str | Path | None = None,
) -> PosterApproval:
    """Record explicit acceptance, retaining the review's independent verdict."""

    if approved is not True:
        raise ApprovalError("approval_required", "Acceptance requires approved=true.")
    try:
        html = Path(source_html_path).expanduser().read_bytes()
    except OSError as exc:
        raise ApprovalError("approval_source_mismatch", str(exc)) from exc
    digest = hashlib.sha256(html).hexdigest()
    if source_html_sha256 != digest:
        raise ApprovalError(
            "approval_source_mismatch", "source HTML bytes do not match source_html_sha256",
        )
    try:
        validation = poster_contract.validate_poster_html(html.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ApprovalError("approval_source_mismatch", "Poster HTML must be UTF-8.") from exc
    if validation.get("status") != "ok":
        raise ApprovalError("approval_source_mismatch", "Poster HTML must satisfy its static contract.")

    approved_root = Path(output_dir).expanduser().resolve() / "approved"
    approved_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="acceptance-", dir=approved_root))
    html_path = root / "poster.html"
    approval_path = root / "approval.json"
    review_path = None
    quality = "not-reviewed"
    try:
        runtime_io.replace_file_atomic(html_path, html)
        if visual_review_path is not None:
            review_path = root / "visual-review.json"
            runtime_io.replace_file_atomic(
                review_path, Path(visual_review_path).expanduser().read_bytes(),
            )
            review = review_contract.load_receipt(review_path, expected_html_sha256=digest)
            quality = review["quality_state"]
        receipt = {
            "schema": "scientific-poster.poster-approval.v3",
            "source_html_sha256": digest,
            "approved": True,
            "approved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "visual_quality_state": quality,
        }
        # Publish the decision after both referenced copies are complete.
        runtime_io.write_json_atomic(approval_path, receipt)
    except (OSError, review_contract.ReviewContractError) as exc:
        shutil.rmtree(root)
        code = getattr(exc, "code", "approval_receipt_untrusted")
        raise ApprovalError(code, str(exc)) from exc
    return PosterApproval(approval_path, html_path, review_path, receipt)
