"""Content-addressed approval bundles for exact poster HTML bytes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import poster_core

from .contracts import ContractError, validate_contract

MAX_HTML_BYTES = 32 * 1024 * 1024
MAX_APPROVAL_BYTES = 256 * 1024
MAX_GROUNDING_CHARS = 1_500_000
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_RE = re.compile(r"^[0-9a-f]{32}$")
_BUNDLE_DOMAIN = b"scientific-poster-html-approval-bundle-v1\0"


class ApprovalError(ValueError):
    """An HTML approval request or receipt is invalid."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class PosterApprovalBundle:
    """Verified exact HTML and approval receipt paths."""

    root: Path
    approval_path: Path
    html_path: Path
    receipt: dict[str, Any]
    bundle_sha256: str
    approval_sha256: str
    source_html_sha256: str


def create_poster_approval(
    *,
    source_html_path: str | Path,
    source_html_sha256: str,
    output_dir: str | Path,
    approved: object,
    operator_confirmation: object,
    session_id: object,
    source_text: str = "",
    source_figure_sha256s: object = (),
    source_html_uri: str | None = None,
    host_event_id: str | None = None,
    approved_at: str | None = None,
) -> PosterApprovalBundle:
    """Validate and atomically publish the exact approved HTML bytes."""

    _, html = _read_regular(
        source_html_path,
        limit=MAX_HTML_BYTES,
        code="approval_source_mismatch",
    )
    actual_html_sha256 = _sha256(html)
    if (
        _HASH_RE.fullmatch(str(source_html_sha256)) is None
        or source_html_sha256 != actual_html_sha256
    ):
        _fail("approval_source_mismatch", "source HTML bytes do not match source_html_sha256")
    try:
        html_text = html.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail("approval_source_mismatch", f"poster HTML must be UTF-8: {exc}")
    if not isinstance(source_text, str) or not source_text.strip():
        _fail("missing_input", "approval requires the original grounded source text")
    if len(source_text) > MAX_GROUNDING_CHARS:
        _fail("source_too_large", "approval grounding source exceeds the character limit")
    if poster_core.validate_poster_html(
        html_text,
        source_text=source_text,
    ).get("status") != "ok":
        _fail("approval_source_mismatch", "poster HTML does not satisfy the inert HTML contract")
    try:
        source_figure_manifest_sha256 = poster_core.source_figure_manifest_sha256(
            source_figure_sha256s
        )
    except ValueError as exc:
        _fail("approval_source_mismatch", str(exc))
    if poster_core.source_figure_usage_issues(
        html_text,
        {str(value) for value in source_figure_sha256s},
    ):
        _fail(
            "approval_source_mismatch",
            "poster no longer contains a visible figure from the prepared PDF",
        )
    _inspect_for_approval(html)
    session = str(session_id or "").strip()
    if not session or len(session) > 256:
        _fail("approval_required", "session_id is required and must be at most 256 characters")
    grounding_source_sha256 = _sha256(source_text.encode("utf-8"))
    phrase = poster_core.poster_approval_phrase(
        actual_html_sha256,
        grounding_source_sha256,
        source_figure_manifest_sha256,
    )
    if approved is not True or operator_confirmation != phrase:
        _fail("approval_required", f"operator_confirmation must exactly equal: {phrase}")
    event_id = host_event_id or secrets.token_hex(16)
    if _EVENT_RE.fullmatch(event_id) is None:
        _fail("approval_required", "host_event_id must be 32 lowercase hexadecimal characters")
    origin = source_html_uri or f"portable://sha256/{actual_html_sha256}"
    if not isinstance(origin, str) or not origin.strip() or len(origin) > 2048:
        _fail("approval_source_mismatch", "source_html_uri is invalid")
    decision = {
        "mode": "portable-operator",
        "session_id": session,
        "host_event_id": event_id,
        "target_kind": "poster",
        "target_sha256": actual_html_sha256,
        "user_message_sha256": _sha256(phrase.encode("utf-8")),
    }
    decision["event_sha256"] = _decision_sha256(decision)
    timestamp = approved_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    receipt = {
        "schema": "scientific-poster.poster-approval.v1",
        "source_html_uri": "bundle:poster.html",
        "source_html_origin_uri": origin,
        "source_html_sha256": actual_html_sha256,
        "grounding_source_sha256": grounding_source_sha256,
        "source_figure_manifest_sha256": source_figure_manifest_sha256,
        "approved": True,
        "approved_at": timestamp,
        "session_id": session,
        "decision": decision,
    }
    try:
        validated = validate_contract("scientific-poster.poster-approval.v1", receipt)
    except ContractError as exc:
        _fail("approval_required", f"invalid approval receipt: {exc}")
    approval = _canonical_json(validated)
    bundle_sha256 = _bundle_sha256(html, approval)
    output = Path(output_dir).expanduser().absolute()
    try:
        output.mkdir(parents=True, exist_ok=True)
        if output.is_symlink():
            _fail("approval_receipt_untrusted", "approval output may not be a symlink")
        approved_root = output / "approved"
        approved_root.mkdir(exist_ok=True)
        if approved_root.is_symlink():
            _fail("approval_receipt_untrusted", "approved directory may not be a symlink")
        final_root = approved_root / bundle_sha256
        _write_staged_bundle(
            approved_root=approved_root,
            final_root=final_root,
            html=html,
            approval=approval,
        )
    except ApprovalError:
        raise
    except OSError as exc:
        _fail("approval_receipt_untrusted", f"cannot publish approval bundle: {exc}")
    return load_poster_approval(final_root / "approval.json")


def _inspect_for_approval(html: bytes) -> None:
    """Rerun Chromium against the exact bytes entering an approval bundle."""

    skill_dir = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="scientific-poster-approval-check-") as directory:
        source_html_path = Path(directory) / "poster.html"
        try:
            with source_html_path.open("xb") as handle:
                handle.write(html)
        except OSError as exc:
            _fail("inspection_unavailable", f"approval inspection input failed: {exc}")
        command = [
            sys.executable,
            str(skill_dir / "scripts" / "inspect_poster.py"),
            "--html",
            str(source_html_path),
            "--out",
            directory,
        ]
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                timeout=180,
            )
            result = json.loads(completed.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            _fail("inspection_unavailable", f"approval inspection could not run: {exc}")
    if not isinstance(result, dict):
        _fail("inspection_unavailable", "approval inspection returned a non-object result")
    if result.get("status") != "ok":
        outcome = result.get("outcome")
        code = str(outcome.get("code") or "inspection_blocked") if isinstance(outcome, dict) else "inspection_blocked"
        summary = str(result.get("summary") or "Chromium inspection must pass before approval")
        details = {
            key: value
            for key, value in result.items()
            if key not in {"status", "outcome", "summary", "blocking", "recoverable"}
        }
        _fail(code, summary, details=details)


def load_poster_approval(approval_path: str | Path) -> PosterApprovalBundle:
    """Load and verify one exact two-file content-addressed approval bundle."""

    approval_file, approval = _read_regular(
        approval_path,
        limit=MAX_APPROVAL_BYTES,
        code="approval_receipt_untrusted",
    )
    root = approval_file.parent
    if root.is_symlink() or not root.is_dir():
        _fail("approval_receipt_untrusted", "approval bundle root must be a regular directory")
    try:
        names = {item.name for item in root.iterdir()}
    except OSError as exc:
        _fail("approval_receipt_untrusted", f"cannot inspect approval bundle: {exc}")
    if names != {"poster.html", "approval.json"}:
        _fail("approval_receipt_untrusted", "approval bundle must contain only poster.html and approval.json")
    html_path, html = _read_regular(
        root / "poster.html",
        limit=MAX_HTML_BYTES,
        code="approval_source_mismatch",
    )
    raw_receipt = _json_object(
        approval,
        source=approval_file,
        code="approval_receipt_untrusted",
    )
    try:
        receipt = validate_contract("scientific-poster.poster-approval.v1", raw_receipt)
    except ContractError as exc:
        _fail("approval_receipt_untrusted", f"invalid approval receipt: {exc}")
    html_sha256 = _sha256(html)
    if receipt["source_html_sha256"] != html_sha256:
        _fail("approval_source_mismatch", "receipt does not match poster.html bytes")
    decision = receipt["decision"]
    phrase = poster_core.poster_approval_phrase(
        html_sha256,
        receipt["grounding_source_sha256"],
        receipt["source_figure_manifest_sha256"],
    )
    if (
        decision.get("target_kind") != "poster"
        or decision.get("target_sha256") != html_sha256
        or decision.get("session_id") != receipt["session_id"]
        or decision.get("user_message_sha256") != _sha256(phrase.encode("utf-8"))
        or decision.get("event_sha256") != _decision_sha256(decision)
    ):
        _fail("approval_receipt_untrusted", "approval decision hash chain is invalid")
    bundle_sha256 = _bundle_sha256(html, approval)
    if root.name != bundle_sha256:
        _fail("approval_receipt_untrusted", "approval bundle directory is not content-addressed")
    try:
        if poster_core.validate_poster_html(html.decode("utf-8")).get("status") != "ok":
            _fail("approval_source_mismatch", "approved poster no longer satisfies the HTML contract")
    except UnicodeDecodeError as exc:
        _fail("approval_source_mismatch", f"approved poster is not UTF-8: {exc}")
    return PosterApprovalBundle(
        root=root,
        approval_path=approval_file,
        html_path=html_path,
        receipt=receipt,
        bundle_sha256=bundle_sha256,
        approval_sha256=_sha256(approval),
        source_html_sha256=html_sha256,
    )


def _decision_sha256(decision: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in decision.items() if key != "event_sha256"}
    return _sha256(
        b"scientific-poster-html-approval-event-v1\0"
        + json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _bundle_sha256(html: bytes, approval: bytes) -> str:
    digest = hashlib.sha256(_BUNDLE_DOMAIN)
    for name, raw in ((b"poster.html", html), (b"approval.json", approval)):
        digest.update(len(name).to_bytes(2, "big"))
        digest.update(name)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _write_staged_bundle(
    *,
    approved_root: Path,
    final_root: Path,
    html: bytes,
    approval: bytes,
) -> None:
    if final_root.exists():
        return
    temporary = Path(tempfile.mkdtemp(prefix=".poster-bundle-", dir=approved_root))
    try:
        for name, raw in (("poster.html", html), ("approval.json", approval)):
            path = temporary / name
            with path.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        try:
            temporary.replace(final_root)
        except FileExistsError:
            pass
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _read_regular(path: str | Path, *, limit: int, code: str) -> tuple[Path, bytes]:
    candidate = Path(path).expanduser()
    try:
        if candidate.is_symlink():
            _fail(code, f"symbolic links are not accepted: {candidate}")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file() or resolved.is_symlink():
            _fail(code, f"a regular file is required: {candidate}")
        if resolved.stat().st_size > limit:
            _fail(code, f"file exceeds the {limit}-byte limit: {candidate}")
        raw = resolved.read_bytes()
    except ApprovalError:
        raise
    except OSError as exc:
        _fail(code, f"cannot read {candidate}: {exc}")
    return resolved, raw


def _json_object(raw: bytes, *, source: Path, code: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(code, f"duplicate JSON key in {source}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(code, f"invalid UTF-8 JSON in {source}: {exc}")
    if not isinstance(value, dict):
        _fail(code, f"JSON root must be an object: {source}")
    return value


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fail(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    raise ApprovalError(code, message, details=details)


__all__ = [
    "ApprovalError",
    "PosterApprovalBundle",
    "create_poster_approval",
    "load_poster_approval",
]
