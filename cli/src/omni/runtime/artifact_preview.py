"""Inline small text/report artifact bodies into task-completion presentations.

Workflow completions historically rendered produced artifacts as *references*
(``report_uri: artifact://…``) and relied on ``open_artifact`` / ``/task show``
to read the body. For small text deliverables (Markdown / plain-text reports)
that extra hop is pure friction: the user asked a question and the answer *is*
the report. This module loads such bodies and attaches them to the
``ArtifactRef`` so the shared presentation renderer can inline them alongside
the link, while large or binary artifacts (figures, datasets) stay link-only.

Resolution is filesystem-only (no async store / DB dependency) so it works
identically from async channel handlers and from the REPL's completion-watcher
thread.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from omni.runtime.presentation import ArtifactRef, TaskPresentation

# Deliberately conservative: only inline *report-shaped* text so figure sidecars
# (``.dot`` / ``.svg`` / ``.mmd``) and structured data (``.json`` / ``.csv``)
# keep a link + open_artifact hint instead of dumping their source into chat.
MAX_PREVIEW_BYTES = 16_000
_REPORT_MIMES = {"text/markdown", "text/plain"}
_REPORT_EXT = {".md", ".markdown", ".txt", ".rst"}
_REPORT_FORMATS = {"md", "markdown", "txt", "text", "report", "rst"}
_ARTIFACT_SCHEME = "artifact://"


def inline_text_artifacts(
    presentation: TaskPresentation,
    artifacts_dir: Path | str | None,
    *,
    max_bytes: int = MAX_PREVIEW_BYTES,
    injection_mode: str = "flag",
) -> TaskPresentation:
    """Return a presentation whose small text artifacts carry an inline body.

    The input is returned unchanged when nothing could be inlined, so callers
    without filesystem access (``artifacts_dir=None``) keep the link-only
    behaviour. Best-effort: unreadable/oversized/binary artifacts are skipped.
    """
    if not presentation.artifacts:
        return presentation
    root = Path(artifacts_dir) if artifacts_dir else None
    changed = False
    refs: list[ArtifactRef] = []
    for ref in presentation.artifacts:
        loaded = _preview_for(ref, root, max_bytes=max_bytes, injection_mode=injection_mode)
        if loaded is None:
            refs.append(ref)
            continue
        text, truncated = loaded
        refs.append(replace(ref, preview=text, preview_truncated=truncated))
        changed = True
    return replace(presentation, artifacts=refs) if changed else presentation


def _preview_for(
    ref: ArtifactRef,
    artifacts_dir: Path | None,
    *,
    max_bytes: int,
    injection_mode: str,
) -> tuple[str, bool] | None:
    if ref.preview or ref.is_image:
        return None
    path = _resolve_path(ref, artifacts_dir)
    if path is None:
        return None
    if not _is_report_text(mime=ref.mime, ext=path.suffix.lower(), fmt=ref.format):
        return None
    try:
        size = path.stat().st_size
        data = path.read_bytes()[: max_bytes + 1]
    except OSError:
        return None
    if b"\x00" in data:  # binary despite a texty name → link only
        return None
    text = data[:max_bytes].decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        from omni.core.injection import defend_observation

        text, _hits = defend_observation(text, mode=injection_mode)
    except Exception:  # noqa: BLE001 - never fail a completion over defence
        pass
    return text, size > max_bytes


def _resolve_path(ref: ArtifactRef, artifacts_dir: Path | None) -> Path | None:
    """Map an ``ArtifactRef`` to a local file (raw path or ``artifact://<id>``)."""
    if ref.path:
        p = Path(ref.path).expanduser()
        if p.is_file():
            return p
    uri = ref.uri
    if not uri:
        return None
    if uri.startswith(_ARTIFACT_SCHEME):
        art_id = uri[len(_ARTIFACT_SCHEME) :]
        if not art_id or artifacts_dir is None:
            return None
        # New scheme ``<slug>-<id8>.<ext>`` first, then legacy ``<id>.<ext>``.
        for pattern in (f"*-{art_id[:8]}.*", f"{art_id}.*"):
            for cand in sorted(artifacts_dir.rglob(pattern)):
                if cand.is_file():
                    return cand
        return None
    p = Path(uri.replace("file://", "")).expanduser()
    return p if p.is_file() else None


def _is_report_text(*, mime: str, ext: str, fmt: str) -> bool:
    if mime.startswith("image/"):
        return False
    if mime in _REPORT_MIMES:
        return True
    if ext in _REPORT_EXT:
        return True
    return fmt.lower() in _REPORT_FORMATS
