"""Private MinerU/VLM tool used only inside the paper-review prompt agent."""

from __future__ import annotations

import mimetypes
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")

_SKILL_DIR = Path(__file__).resolve().parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from paper_review_visual.core import (
    MineruError,
    MineruMissingError,
    ReviewRun,
    run_review,
)

_VLM_SETUP_COMMAND = (
    "omni config vlm -u <ENDPOINT> -m <VISION_MODEL> -k <API_KEY> --test"
)


class PaperReviewVisualTool:
    """Run visual extraction locally through paper-review's private tool."""

    @staticmethod
    def validate_params(
        *, arguments: dict | None = None, input_data: dict | None = None
    ) -> dict | None:
        data = arguments or input_data or {}
        if not str(data.get("input") or data.get("file_uri") or "").strip():
            return {"error": "input is required"}
        return None

    async def execute(
        self,
        progress_callback: Any = None,
        **input_data: Any,
    ) -> dict[str, Any]:
        ctx = getattr(self, "ctx", None)
        raw_input = str(
            input_data.get("input") or input_data.get("file_uri") or ""
        ).strip()
        pdf_path = _local_path(raw_input)
        if pdf_path is None:
            return _input_error(
                "paper-review visual analysis requires a local PDF path; remote URLs are not fetched."
            )

        output_dir = _output_dir(ctx)
        extract_only = bool(input_data.get("extract_only", False))
        vlm = None if extract_only else _host_vlm(ctx)
        try:
            result = await run_review(
                pdf_path,
                output_dir,
                vlm=vlm,
                max_visuals=_bounded_int(input_data.get("max_visuals"), 12, 1, 30),
                visual_types=_visual_types(input_data.get("visual_types")),
                analysis_language=str(
                    input_data.get("analysis_language") or ""
                ).strip(),
                focus=str(input_data.get("focus") or "").strip(),
                extract_only=extract_only,
                mineru_backend=str(
                    input_data.get("mineru_backend") or "pipeline"
                ).strip(),
                mineru_command=(
                    str(input_data.get("mineru_command") or "mineru").strip()
                    or "mineru"
                ),
                mineru_timeout_s=_bounded_float(
                    input_data.get("mineru_timeout_s"),
                    600.0,
                    1.0,
                    600.0,
                ),
                mineru_device=str(
                    input_data.get("mineru_device") or "auto"
                ).strip(),
                progress=lambda stage, pct: _progress(
                    progress_callback,
                    stage,
                    pct,
                ),
            )
        except MineruMissingError as exc:
            return {
                "status": "error",
                "summary": (
                    "MinerU is not installed, so no figure or table crops were extracted."
                ),
                "error": str(exc),
                "recoverable": True,
                "blocking": False,
                "setup_command": 'uv pip install -U "mineru[core]"',
                "next_actions": [
                    'install MinerU with `uv pip install -U "mineru[core]"`',
                    "then retry the paper visual review",
                ],
                "error_info": {
                    "code": exc.code,
                    "category": "dependency",
                    "retryable": False,
                    "workflow_recoverable": True,
                },
            }
        except MineruError as exc:
            process_run = exc.runs[-1] if exc.runs else None
            artifacts = await _run_artifacts(ctx, process_run)
            return {
                "status": "error",
                "summary": f"Paper visual extraction failed: {exc}",
                "error": str(exc),
                "recoverable": bool(exc.retryable),
                "blocking": False,
                "mineru_run": process_run.as_dict() if process_run else {},
                "mineru_runtime": (
                    process_run.runtime.as_dict() if process_run else {}
                ),
                "artifacts": artifacts,
                "diagnostic_notice": (
                    "MinerU stdout, stderr, run metadata, and the resolved device "
                    "decision were saved. Inspect the stderr artifact first."
                ),
                "next_actions": [
                    "inspect the MinerU error-output and run-metadata artifacts",
                    "free or select another GPU with mineru_device=cuda:N, or use cpu",
                ],
                "error_info": {
                    "code": exc.code,
                    "category": "extraction",
                    "retryable": bool(exc.retryable),
                    "workflow_recoverable": True,
                    "run_started": process_run is not None,
                },
            }
        except Exception as exc:  # noqa: BLE001 - adapter returns a safe boundary
            return {
                "status": "error",
                "summary": "Paper visual review failed unexpectedly.",
                "error": _safe_message(exc),
                "recoverable": True,
                "blocking": False,
                "error_info": {
                    "code": "visual_review_failed",
                    "category": "generation",
                    "retryable": True,
                    "workflow_recoverable": True,
                },
            }

        payload = await _result_payload(ctx, result)
        if not extract_only and vlm is None:
            return _with_vlm_guidance(payload, configured=False)
        if result.selected_visuals and not result.reviews and vlm is not None:
            return _with_vlm_guidance(payload, configured=True)
        return payload


class _OmniVlmAdapter:
    """Map host-service failures to safe per-crop errors."""

    def __init__(self, host: Any) -> None:
        self._host = host

    async def generate_text(
        self,
        prompt: str,
        *,
        reference_image_uri: str | None = None,
    ) -> str:
        try:
            return await self._host.generate_text(
                prompt,
                reference_image_uri=reference_image_uri,
            )
        except Exception as exc:  # noqa: BLE001 - narrow host boundary
            raise RuntimeError(
                str(getattr(exc, "safe_message", "VLM request failed"))
            ) from None


def _host_vlm(ctx: Any) -> _OmniVlmAdapter | None:
    host = getattr(ctx, "vlm", None) if ctx is not None else None
    if host is None or not callable(getattr(host, "generate_text", None)):
        return None
    available = getattr(host, "available", True)
    if callable(available):
        available = available()
    return _OmniVlmAdapter(host) if available else None


def has_configured_vlm(ctx: Any) -> bool:
    """Return whether Omni exposed a configured, callable vision service."""

    return _host_vlm(ctx) is not None


def _with_vlm_guidance(
    payload: dict[str, Any],
    *,
    configured: bool,
) -> dict[str, Any]:
    """Attach actionable guidance without blocking the text-only review."""

    result = dict(payload)
    if configured:
        code = "vlm_visual_review_failed"
        notice = (
            "The configured VLM could not review any extracted image. Verify that "
            "the selected model accepts image input; text-only models such as "
            "DeepSeek cannot perform this visual stage."
        )
        setup_command = "omni config vlm --test"
        actions = [
            "run `omni config vlm --test` with the saved configuration",
            "select a vision-capable model that supports image input",
            "or set `skip_visual=true` for an intentional text-only paper review",
        ]
    else:
        code = "vlm_not_configured"
        notice = (
            "No separate VLM is configured. The primary text model can still review "
            "the manuscript, and MinerU crops remain available, but figures and tables "
            "were not visually interpreted."
        )
        setup_command = _VLM_SETUP_COMMAND
        actions = [
            f"configure and verify a vision-capable model with `{_VLM_SETUP_COMMAND}`",
            "or set `skip_visual=true` for an intentional text-only paper review",
        ]
    result.update(
        {
            "outcome": {"code": code},
            "configuration_notice": notice,
            "setup_command": setup_command,
            "next_actions": actions,
            "recoverable": True,
            "blocking": False,
            "error_info": {
                "code": code,
                "category": "configuration",
                "retryable": False,
                "workflow_recoverable": True,
            },
        }
    )
    return result


def _local_path(value: str) -> Path | None:
    # A drive letter is not a URL scheme. ``urlparse`` reads "C:\\work\\p.pdf" as
    # scheme "c", so the rejection below discarded every absolute Windows path
    # and the review failed as invalid input before MinerU was ever reached.
    # The separator is required, so a genuine one-letter scheme stays a URL.
    if _WINDOWS_DRIVE.match(str(value or "").strip()):
        return Path(value).expanduser()
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} or parsed.netloc:
        return None
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        # "file:///C:/work/p.pdf" leaves the drive behind a leading slash.
        if _WINDOWS_DRIVE.match(path[1:]):
            path = path[1:]
        return Path(path).expanduser()
    if parsed.scheme:
        return None
    return Path(value).expanduser()


def _output_dir(ctx: Any) -> Path:
    if ctx is not None and getattr(ctx, "paths", None) is not None:
        root = Path(ctx.paths.artifacts_dir)
    else:
        root = Path.cwd() / "paper-review-output"
    target = root / "paper-review-visual-runs" / uuid.uuid4().hex
    target.mkdir(parents=True, exist_ok=True)
    return target


def _visual_types(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ("image", "chart", "table")
    return tuple(str(item) for item in value)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _bounded_float(
    value: Any,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Coerce one numeric option into a bounded runtime-safe range."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


async def _progress(callback: Any, stage: str, pct: float) -> None:
    if callback is None:
        return
    value = callback(stage, pct)
    if hasattr(value, "__await__"):
        await value


async def _result_payload(ctx: Any, result: ReviewRun) -> dict[str, Any]:
    local = result.as_dict()
    artifacts: list[dict[str, str]] = []
    specs = [
        (result.manifest_path, "MinerU visual manifest", "json", "data"),
        (
            result.review_json_path,
            "Structured paper-review visual evidence",
            "json",
            "data",
        ),
        (result.review_markdown_path, "Paper-review visual evidence", "md", "document"),
        (result.archive_path, "Paper-review visual evidence package", "zip", "archive"),
    ]
    specs.extend(
        (
            visual.image_path,
            (
                f"Extracted {visual.visual_type} · page "
                f"{visual.page_number if visual.page_number is not None else 'unknown'} "
                f"· {visual.visual_id}"
            ),
            visual.image_path.suffix.lower().lstrip(".") or "image",
            "figure",
        )
        for visual in result.selected_visuals
    )
    specs.extend(_run_specs(result.extraction.run))
    for path, title, fmt, kind in specs:
        artifacts.append(await _store_artifact(ctx, path, title, fmt, kind))
    local["artifacts"] = artifacts
    local["research"] = {
        "source_ids": [],
        "claim_ids": [],
        "evidence_ids": [],
        "run_id": "",
    }
    return local


async def _run_artifacts(
    ctx: Any,
    process_run: Any | None,
) -> list[dict[str, str]]:
    """Store failure diagnostics without hiding the original MinerU error."""

    artifacts: list[dict[str, str]] = []
    for path, title, fmt, kind in _run_specs(process_run):
        if not path.is_file():
            continue
        try:
            artifact = await _store_artifact(ctx, path, title, fmt, kind)
        except Exception:  # noqa: BLE001 - local diagnostics remain usable
            artifact = _local_artifact(path, title, fmt)
        artifacts.append(artifact)
    return artifacts


def _run_specs(
    process_run: Any | None,
) -> tuple[tuple[Path, str, str, str], ...]:
    """Return stable artifact titles for the single MinerU process run."""

    if process_run is None:
        return ()
    return (
        (process_run.stdout_path, "MinerU standard output", "log", "data"),
        (process_run.stderr_path, "MinerU error output", "log", "data"),
        (process_run.metadata_path, "MinerU run metadata", "json", "data"),
    )


def _local_artifact(path: Path, title: str, fmt: str) -> dict[str, str]:
    """Describe a diagnostic that could not be copied into the host store."""

    return {
        "title": title,
        "format": fmt,
        "uri": "",
        "path": str(path),
        "mime": _artifact_mime(path),
    }


async def _store_artifact(
    ctx: Any,
    path: Path,
    title: str,
    fmt: str,
    kind: str,
) -> dict[str, str]:
    mime = _artifact_mime(path)
    if ctx is not None and getattr(ctx, "artifacts", None) is not None:
        stored = await ctx.artifacts.put_file(
            path,
            kind=kind,
            title=title,
            mime=mime,
            session_id=getattr(ctx, "session_id", ""),
            subtask_id=getattr(ctx, "subtask_id", ""),
            workflow_run_id=getattr(ctx, "workflow_run_id", ""),
            copy=True,
        )
        return {
            "title": title,
            "format": fmt,
            "uri": stored.uri,
            "path": str(stored.path),
            "mime": stored.mime,
        }
    return {
        "title": title,
        "format": fmt,
        "uri": "",
        "path": str(path),
        "mime": mime,
    }


def _artifact_mime(path: Path) -> str:
    """Use a readable MIME type for MinerU diagnostic logs."""

    if path.suffix.casefold() == ".log":
        return "text/plain"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _input_error(message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "summary": message,
        "error": message,
        "recoverable": False,
        "blocking": True,
        "error_info": {
            "code": "invalid_input",
            "category": "input",
            "retryable": False,
        },
    }


def _safe_message(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    return message[:600] or exc.__class__.__name__
