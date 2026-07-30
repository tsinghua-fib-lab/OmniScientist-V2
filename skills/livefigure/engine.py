"""OmniScientist adapter for the one-pass LiveFigure PPTX generator."""

from __future__ import annotations

import mimetypes
import re
import sys
import uuid
from pathlib import Path
from typing import Any

_SKILL_DIR = Path(__file__).resolve().parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from livefigure.pipeline import LiveFigureError, PipelineConfig, generate_pptx  # noqa: E402
from livefigure.vlm import VlmError  # noqa: E402


class LiveFigureEngine:
    @staticmethod
    def validate_params(
        *, arguments: dict | None = None, input_data: dict | None = None
    ) -> dict | None:
        data = arguments or input_data or {}
        if not str(data.get("input") or data.get("query") or data.get("prompt") or "").strip():
            return {"error": "input is required"}
        return None

    async def execute(self, progress_callback: Any = None, **input_data: Any) -> dict[str, Any]:
        requirement = str(
            input_data.get("input") or input_data.get("query") or input_data.get("prompt") or ""
        ).strip()
        title = str(input_data.get("title") or "LiveFigure scientific diagram").strip()
        ctx = getattr(self, "ctx", None)
        config = _pipeline_config(ctx)
        if config is None:
            return _host_invariant_error()

        output_dir = _resolve_output_dir(ctx, input_data)
        try:
            result = await generate_pptx(
                requirement,
                title=title,
                output_dir=output_dir,
                config=config,
                reference_image_uri=_optional_text(input_data.get("reference_image_uri")),
                progress=lambda stage, pct, **data: _progress(
                    progress_callback, stage, pct, **data
                ),
            )
        except LiveFigureError as exc:
            message = _redact_authorization(str(exc))
            retryable = bool(getattr(exc, "retryable", True))
            return {
                "status": "error",
                "title": title,
                "summary": f"LiveFigure failed: {message}",
                "error": message,
                "recoverable": retryable,
                "blocking": not retryable,
                "error_info": {
                    "code": str(getattr(exc, "code", "livefigure_failed")),
                    "category": str(getattr(exc, "category", "generation")),
                    "message": message,
                    "retryable": retryable,
                    "workflow_recoverable": retryable,
                },
            }

        artifact_specs = [
            (
                result.pptx_path,
                f"{title} PPTX",
                "pptx",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "figure",
            ),
            (result.code_path, f"{title} Source", "py", "text/x-python", "code"),
            (result.input_path, f"{title} Input", "txt", "text/plain", "data"),
        ]
        if result.reference_path is not None:
            reference_format, reference_mime = _image_metadata(result.reference_path)
            artifact_specs.append(
                (
                    result.reference_path,
                    f"{title} Reference",
                    reference_format,
                    reference_mime,
                    "figure",
                )
            )

        artifacts = []
        for path, artifact_title, fmt, mime, kind in artifact_specs:
            if path is not None and path.is_file():
                artifacts.append(await _store_artifact(ctx, path, artifact_title, fmt, mime, kind))
        pptx_uri = _first_uri(artifacts, "pptx")
        research = await _record_provenance(ctx, title, artifacts)
        return {
            "status": "ok",
            "title": title,
            "summary": f"Generated an editable PPTX: {title}.",
            "caption": "A one-slide editable scientific diagram generated from the requirement and optional visual reference.",
            "pptx_uri": pptx_uri,
            "source_code_uri": _first_uri(artifacts, "py"),
            "reference_image_uri": _first_image_uri(artifacts)
            or _optional_text(input_data.get("reference_image_uri"))
            or "",
            "artifacts": artifacts,
            "run_id": research.get("run_id", ""),
            "research": research,
            "metadata": {
                "attempts": result.attempts,
                "renderer": "python-pptx",
                "visual_critique": False,
            },
        }


class _OmniVlmAdapter:
    """Translate the owner service's safe error contract for the portable core."""

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
        except Exception as exc:  # noqa: BLE001 - host errors cross a narrow port
            raise VlmError(
                str(getattr(exc, "safe_message", "VLM request failed")),
                code=str(getattr(exc, "code", "vlm_request_failed")),
                category=str(getattr(exc, "category", "network")),
                retryable=bool(getattr(exc, "retryable", True)),
            ) from None


def _pipeline_config(ctx: Any) -> PipelineConfig | None:
    """Build one run from Omni's injected VLM host service only."""
    host = getattr(ctx, "vlm", None)
    if not callable(getattr(host, "generate_text", None)):
        return None
    reference_roots, reference_files = _reference_policy(ctx)
    return PipelineConfig(
        vlm=_OmniVlmAdapter(host),
        reference_roots=reference_roots,
        reference_files=reference_files,
        sandbox_prefix=_sandbox_prefix(ctx),
    )


def _sandbox_prefix(ctx: Any) -> tuple[str, ...]:
    """OS-level write-confinement prefix for the generated-code subprocess.

    The host injects confinement via ``ctx.os_sandbox_prefix()`` (seatbelt /
    bwrap / firejail); the adapter never reads owner settings or imports CLI
    internals. Best-effort: when unavailable the core runs the child unwrapped,
    still guarded by the static code denylist.
    """
    getter = getattr(ctx, "os_sandbox_prefix", None)
    if not callable(getter):
        return ()
    try:
        return tuple(str(part) for part in (getter() or ()))
    except Exception:  # noqa: BLE001 - confinement is defence-in-depth, never fatal
        return ()


def _reference_policy(ctx: Any) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Limit local images to Omni artifacts and explicitly attached files."""
    if ctx is None:
        return (), ()
    roots: list[Path] = []
    paths = getattr(ctx, "paths", None)
    artifacts_dir = getattr(paths, "artifacts_dir", None)
    if artifacts_dir:
        roots.append(Path(artifacts_dir))
    files: list[Path] = []
    for raw in getattr(ctx, "file_uris", ()) or ():
        value = str(raw or "").strip()
        if not value or value.startswith("artifact://"):
            continue
        if value.startswith("file://"):
            from urllib.parse import urlparse
            from urllib.request import url2pathname

            value = url2pathname(urlparse(value).path)
        if "://" not in value:
            files.append(Path(value))
    return tuple(roots), tuple(files)


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _redact_authorization(message: str) -> str:
    return re.sub(
        r"(?i)(authorization\s*[:=]?\s*(?:bearer\s+)?)[^\s,;]+",
        r"\1[REDACTED]",
        str(message),
    )


def _resolve_output_dir(ctx: Any, input_data: dict[str, Any]) -> Path:
    del input_data  # Omni outputs are always constrained to its artifact workspace.
    if ctx is not None and getattr(ctx, "paths", None) is not None:
        path = Path(ctx.paths.artifacts_dir) / "livefigure-runs" / uuid.uuid4().hex
    else:
        path = Path.cwd() / "livefigure-output" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _progress(callback: Any, stage: str, pct: float, **data: Any) -> None:
    if callback is None:
        return
    try:
        value = callback(stage, pct, **data)
    except TypeError:
        value = callback(stage, pct)
    if hasattr(value, "__await__"):
        await value


async def _store_artifact(
    ctx: Any, path: Path, title: str, fmt: str, mime: str, kind: str
) -> dict[str, str]:
    if ctx is not None and getattr(ctx, "artifacts", None) is not None:
        stored = await ctx.artifacts.put_file(
            path,
            kind=kind,
            title=title,
            mime=mime,
            session_id=getattr(ctx, "session_id", ""),
            task_id=getattr(ctx, "task_id", ""),
            subtask_id=getattr(ctx, "subtask_id", "") or getattr(ctx, "task_id", ""),
            copy=True,
        )
        return {
            "title": title,
            "format": fmt,
            "uri": stored.uri,
            "path": str(stored.path),
            "mime": stored.mime,
        }
    return {"title": title, "format": fmt, "uri": "", "path": str(path), "mime": mime}


def _first_uri(artifacts: list[dict[str, str]], fmt: str) -> str:
    return next((item["uri"] for item in artifacts if item["format"] == fmt), "")


def _first_image_uri(artifacts: list[dict[str, str]]) -> str:
    return next(
        (item["uri"] for item in artifacts if item.get("mime", "").startswith("image/")),
        "",
    )


def _image_metadata(path: Path) -> tuple[str, str]:
    """Describe the copied reference without pretending every image is PNG."""
    image_format = path.suffix.lower().lstrip(".") or "image"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return image_format, mime


async def _record_provenance(
    ctx: Any, title: str, artifacts: list[dict[str, str]]
) -> dict[str, Any]:
    if ctx is None or getattr(ctx, "db", None) is None:
        return {"source_ids": [], "claim_ids": [], "evidence_ids": [], "run_id": ""}
    try:
        from omni.research import ResearchStore, capture_env_lock

        output_uris = [item["uri"] for item in artifacts if item.get("uri")]
        run = await ResearchStore(ctx.db).add_run(
            title=f"Generate LiveFigure: {title}",
            session_id=getattr(ctx, "session_id", ""),
            subtask_id=getattr(ctx, "subtask_id", "") or getattr(ctx, "task_id", ""),
            cmd="livefigure engine (VLM + python-pptx)",
            code_uri=_first_uri(artifacts, "py"),
            env_lock=capture_env_lock(),
            output_uris=output_uris,
            metrics={"artifact_count": len(output_uris), "editable": True, "slide_count": 1},
            status="succeeded",
        )
        return {"source_ids": [], "claim_ids": [], "evidence_ids": [], "run_id": run.id}
    except Exception as exc:  # noqa: BLE001 - provenance failure must not discard the PPTX
        return {
            "source_ids": [],
            "claim_ids": [],
            "evidence_ids": [],
            "run_id": "",
            "warning": str(exc),
        }


def _host_invariant_error() -> dict[str, Any]:
    return {
        "status": "error",
        "summary": "LiveFigure could not start because its VLM host service was not injected.",
        "error": "Missing callable VLM host service.",
        "recoverable": False,
        "blocking": True,
        "error_info": {
            "code": "vlm_host_service_missing",
            "category": "internal",
            "retryable": False,
        },
    }
