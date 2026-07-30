"""File-based artifact store (replaces HelixForge's MinIO object storage).

Exposes a MinIO-compatible-ish surface (``put_bytes`` / ``put_file`` /
``resolve_path`` / ``url_for``) so ported skill engines that return
``*_uri`` fields keep working. URIs use the ``artifact://<id>`` scheme; the
bytes live under ``<project>/artifacts/<kind>/<slug>-<task8>-<art8>.<ext>``
(or a trusted launch-dir ``<kind>s/`` subfolder with the same filename). Legacy
bare ``<id>.<ext>`` / ``<slug>-<art8>.<ext>`` names remain resolvable via the DB.
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from omni.config.paths import OmniPaths
from omni.core.identifiers import short_id
from omni.storage.db import Database
from omni.storage.models import ArtifactORM, TaskORM, _uuid

_ARTIFACT_SCHEME = "artifact://"

# On-disk filenames are ``<slug>-<task8>-<art8>.<ext>`` (or ``<slug>-<art8>.<ext>``
# when no owning task is known): the slug makes the file human-readable in
# listings and IM attachments, the task id ties every deliverable of one turn
# together, and the artifact id keeps names collision-safe and greppable. The
# DB ``rel_path`` remains the source of truth for resolution, so the slug never
# has to round-trip.
_MAX_SLUG_LEN = 60
# Characters unsafe in a filename across macOS/Linux/Windows and IM upload APIs.
# CJK and other Unicode letters are intentionally preserved (titles are often
# Chinese; those filesystems and the WeChat/Feishu APIs handle UTF-8 names).
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")


def slugify_filename(text: str, *, max_len: int = _MAX_SLUG_LEN) -> str:
    """Return a human-readable, filesystem-safe slug (Unicode/CJK preserved)."""
    normalized = unicodedata.normalize("NFC", text or "").strip()
    cleaned = _UNSAFE_FILENAME_CHARS.sub("-", normalized)
    cleaned = _WHITESPACE.sub("-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-._")
    return cleaned[:max_len].strip("-._")


def artifact_filename(
    *,
    title: str,
    kind: str,
    art_id: str,
    ext: str,
    task_id: str = "",
) -> str:
    """Compose an artifact filename from its title/kind/task/artifact ids.

    Shape: ``<slug>-<task8>-<art8>.<ext>`` when an owning task is known, else
    ``<slug>-<art8>.<ext>``. Both the launch-directory deliverable and the
    durable store use the same rule so ``ls`` / ``/task show`` can attribute a
    file to its task at a glance.
    """
    slug = slugify_filename(title) or slugify_filename(kind) or "artifact"
    suffix = ext.lstrip(".")
    # Titles often embed the format ("<figure title> SVG"); the extension
    # already says it, so drop the duplicated trailing token.
    if suffix and slug.lower().endswith(f"-{suffix.lower()}"):
        slug = slug[: -(len(suffix) + 1)] or "artifact"
    art8 = short_id(art_id)
    task8 = short_id((task_id or "").strip())
    if task8 and art8:
        stem = f"{slug}-{task8}-{art8}"
    elif art8:
        stem = f"{slug}-{art8}"
    else:
        stem = slug
    return f"{stem}.{suffix}" if suffix else stem


# Launch-directory deliverables are grouped into human-friendly subfolders by
# kind (``figures/`` / ``reports/`` …), mirroring how Codex — or a person — lays
# out a task's outputs. Unknown kinds fall back to the kind name itself.
_DELIVERABLE_SUBDIRS = {
    "figure": "figures",
    "report": "reports",
    "poster": "posters",
    "slides": "slides",
    "notebook": "notebooks",
    "evidence": "evidence",
    "data": "data",
    "bundle": "bundles",
    "table": "tables",
    "document": "documents",
}

# "Bundle" kinds keep their whole self-contained set together in one subfolder —
# the rendered deliverable *and* its source/provenance sidecars — the way Codex
# lays a task's outputs side by side. A ``figure`` therefore surfaces its
# ``.svg``/``.png`` next to the ``.dot`` source and ``*.provenance.json`` so the
# folder is a portable, auditable unit. Non-bundle kinds still gate on the
# user-facing deliverable formats, so their loose intermediates stay hidden in
# the durable store.
_BUNDLE_KINDS = {"figure"}


@dataclass
class StoredArtifact:
    id: str
    uri: str
    path: Path
    kind: str
    title: str
    mime: str
    size_bytes: int
    # Deprecated: retained for backward compatibility. Deliverables in a trusted
    # launch directory are now written there directly as the single canonical
    # copy (see ``path``), so no separate mirror copy is produced and this stays
    # ``None``. ``display_path`` still prefers it if some caller ever sets it.
    mirror_path: Path | None = None

    @property
    def display_path(self) -> Path:
        """The path a user should be pointed at for this deliverable.

        When the file was surfaced into the trusted launch directory (next to
        the user's own work) that copy is what we show; otherwise the canonical
        store path. The durable ``artifact://`` URI is always kept alongside for
        reproducible resolution regardless of which path is displayed.
        """
        return self.mirror_path or self.path

    def result_record(
        self,
        *,
        title: str | None = None,
        format: str | None = None,
    ) -> dict[str, str]:
        """Canonical ``StoredArtifact`` → skill-result artifact record.

        Every engine should surface produced files through this single helper so
        result payloads share one shape *and* one path convention: the
        user-facing ``display_path`` (launch-directory copy when mirrored, else
        the durable store path) plus the ``artifact://`` URI for resolution.
        Centralizing it here means new engines can't drift or forget a field.
        """
        ext = self.path.suffix.lower().lstrip(".")
        return {
            "title": title or self.title or self.kind or "artifact",
            "format": (format or ext or "").lower(),
            "uri": self.uri,
            "path": str(self.display_path),
            "mime": self.mime,
            "size_bytes": str(self.size_bytes),
        }


class ArtifactStore:
    def __init__(
        self,
        paths: OmniPaths,
        db: Database,
        *,
        mirror_dir: Path | str | None = None,
        mirror_formats: object = None,
    ) -> None:
        self._paths = paths
        self._db = db
        # Canonical bytes always live under the workspace; ``mirror_dir`` (set
        # only for a trusted launch directory) receives an extra copy of the
        # user-facing deliverables so results land next to the user's work.
        self._mirror_dir = Path(mirror_dir) if mirror_dir else None
        self._mirror_formats = {
            str(f).lower().lstrip(".") for f in (mirror_formats or ())
        }

    @property
    def mirror_dir(self) -> Path | None:
        """The trusted launch/output directory deliverables are written into.

        ``None`` for an untrusted run (durable store only). Revision guards
        consult this to treat a ``.dot`` Omni wrote into ``<output>/figures/``
        as a managed, re-renderable source — the same trust as the store.
        """
        return self._mirror_dir

    def _dir_for(self, kind: str) -> Path:
        d = self._paths.artifacts_dir / kind
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _deliverable_dir(self, kind: str, ext: str) -> Path | None:
        """The launch/output subdirectory for a user-facing deliverable, or ``None``.

        When the launch directory is trusted (``mirror_dir`` set) the canonical
        bytes are written *directly* into ``<output_dir>/<kind>s/`` — a single
        file, in a clean per-kind subfolder next to the user's work (Codex /
        Claude-Code parity), not a durable-store copy plus a mirror. A *bundle*
        kind (``figure``) co-locates its whole set there — the rendered image,
        its ``.dot`` source, and ``*.provenance.json`` — forming a portable,
        self-contained bundle; other kinds only surface user-facing deliverable
        formats, so their loose intermediate sidecars stay in the durable store.
        Untrusted runs always keep the durable store location.
        """
        if self._mirror_dir is None:
            return None
        normalized = ext.lower().lstrip(".")
        is_bundle_kind = kind.lower() in _BUNDLE_KINDS
        if not is_bundle_kind and self._mirror_formats and normalized not in self._mirror_formats:
            return None
        subdir = _DELIVERABLE_SUBDIRS.get(kind.lower(), kind.lower() or "files")
        return self._mirror_dir / subdir

    def _dest_for(
        self, *, kind: str, title: str, art_id: str, ext: str, task_id: str = ""
    ) -> Path:
        """Resolve a semantic destination path.

        Launch-directory deliverables and durable-store files share one naming
        rule (``<slug>-<task8>-<art8>.<ext>``). A rare residual collision falls
        back to the bare artifact id so ``put_*`` never overwrites an existing
        file.
        """
        name = artifact_filename(
            title=title, kind=kind, art_id=art_id, ext=ext, task_id=task_id
        )
        deliverable_dir = self._deliverable_dir(kind, ext)
        directory = deliverable_dir if deliverable_dir is not None else self._dir_for(kind)
        if deliverable_dir is not None:
            directory.mkdir(parents=True, exist_ok=True)
        dest = directory / name
        if dest.exists():
            suffix = ext.lstrip(".")
            dest = directory / (f"{art_id}.{suffix}" if suffix else art_id)
        return dest

    async def put_bytes(
        self,
        data: bytes,
        *,
        kind: str = "file",
        title: str = "",
        ext: str = "bin",
        mime: str = "application/octet-stream",
        session_id: str = "",
        task_id: str = "",
        subtask_id: str = "",
        workflow_run_id: str = "",
        meta: dict | None = None,
    ) -> StoredArtifact:
        art_id = _uuid()
        dest = self._dest_for(
            kind=kind, title=title, art_id=art_id, ext=ext, task_id=task_id
        )
        dest.write_bytes(data)
        return await self._record(
            art_id,
            dest,
            kind,
            title,
            mime,
            session_id,
            task_id,
            subtask_id,
            workflow_run_id,
            meta,
        )

    async def put_file(
        self,
        src: Path,
        *,
        kind: str = "file",
        title: str = "",
        mime: str = "application/octet-stream",
        session_id: str = "",
        task_id: str = "",
        subtask_id: str = "",
        workflow_run_id: str = "",
        meta: dict | None = None,
        copy: bool = True,
    ) -> StoredArtifact:
        src = Path(src)
        art_id = _uuid()
        dest = self._dest_for(
            kind=kind,
            title=title or src.stem,
            art_id=art_id,
            ext=src.suffix,
            task_id=task_id,
        )
        if copy:
            shutil.copy2(src, dest)
        else:
            shutil.move(str(src), dest)
        return await self._record(
            art_id,
            dest,
            kind,
            title or src.name,
            mime,
            session_id,
            task_id,
            subtask_id,
            workflow_run_id,
            meta,
        )

    async def _record(
        self,
        art_id: str,
        dest: Path,
        kind: str,
        title: str,
        mime: str,
        session_id: str,
        task_id: str,
        subtask_id: str,
        workflow_run_id: str,
        meta: dict | None,
    ) -> StoredArtifact:
        try:
            rel_path_value = str(dest.relative_to(self._paths.project_dir))
        except ValueError:
            # A deliverable written straight into the trusted launch directory
            # lives outside the durable store; record its absolute path so
            # ``resolve_path`` can still map the URI back to the single copy.
            rel_path_value = str(dest.resolve())
        size = dest.stat().st_size
        uri = f"{_ARTIFACT_SCHEME}{art_id}"
        async with self._db.session() as s:
            task = await s.get(TaskORM, task_id) if task_id else None
            s.add(
                ArtifactORM(
                    id=art_id,
                    session_id=session_id,
                    task_id=task.id if task is not None else None,
                    subtask_id=subtask_id or None,
                    workflow_run_id=workflow_run_id or None,
                    kind=kind,
                    title=title,
                    uri=uri,
                    rel_path=rel_path_value,
                    mime=mime,
                    size_bytes=size,
                    meta=meta or {},
                )
            )
            if task is not None:
                artifact_ids = list(task.artifact_ids or [])
                if art_id not in artifact_ids:
                    artifact_ids.append(art_id)
                    task.artifact_ids = artifact_ids
            await s.commit()
        # Deliverables are written straight to their final location (a launch-dir
        # ``<kind>s/`` subfolder when trusted, else the durable store), so there
        # is exactly one copy and no separate mirror step.
        return StoredArtifact(art_id, uri, dest, kind, title, mime, size)

    async def resolve_path(self, uri: str) -> Path | None:
        """Map an ``artifact://<id>`` URI (or raw path) back to a local file."""
        if not uri:
            return None
        if not uri.startswith(_ARTIFACT_SCHEME):
            p = Path(uri.replace("file://", ""))
            return p if p.exists() else None
        art_id = uri[len(_ARTIFACT_SCHEME) :]
        async with self._db.session() as s:
            row = (
                await s.execute(select(ArtifactORM).where(ArtifactORM.id == art_id))
            ).scalar_one_or_none()
        if not row:
            return None
        rel = row.rel_path or ""
        if Path(rel).is_absolute():
            # An absolute ``rel_path`` only exists for a deliverable written into
            # the trusted launch/output directory (single copy). It is resolvable
            # ONLY when it still sits inside that configured output dir: a
            # crafted/foreign absolute path — or one recorded by a session whose
            # output dir has since changed — is refused, preserving the
            # path-traversal guard. Surface only if the file still exists (the
            # user may have moved or removed their own copy).
            if self._mirror_dir is None:
                return None
            candidate = Path(rel).resolve()
            try:
                candidate.relative_to(self._mirror_dir.resolve())
            except ValueError:
                return None
            return candidate if candidate.is_file() else None
        project_root = self._paths.project_dir.resolve()
        full = (project_root / rel).resolve()
        try:
            full.relative_to(project_root)
        except ValueError:
            return None
        return full if full.exists() else None

    async def list_recent(self, limit: int = 20) -> list[ArtifactORM]:
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(ArtifactORM).order_by(ArtifactORM.created_at.desc()).limit(limit)
                )
            ).scalars().all()
        return list(rows)

    async def list_by_session(self, session_id: str, *, limit: int = 20) -> list[ArtifactORM]:
        """Artifacts produced in (or attributed to) a session, newest first."""
        if not session_id:
            return []
        async with self._db.session() as s:
            rows = (
                await s.execute(
                    select(ArtifactORM)
                    .where(ArtifactORM.session_id == session_id)
                    .order_by(ArtifactORM.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
        return list(rows)

    async def get(self, uri: str) -> ArtifactORM | None:
        """Resolve an artifact URI, exact id, or unambiguous leading prefix."""
        if not uri:
            return None
        art_id = (
            uri[len(_ARTIFACT_SCHEME):]
            if uri.startswith(_ARTIFACT_SCHEME)
            else uri
        ).strip()
        if not art_id:
            return None
        async with self._db.session() as s:
            exact = await s.get(ArtifactORM, art_id)
            if exact is not None:
                return exact
            rows = (
                await s.execute(
                    select(ArtifactORM)
                    .where(ArtifactORM.id.startswith(art_id, autoescape=True))
                    .limit(2)
                )
            ).scalars().all()
        return rows[0] if len(rows) == 1 else None

    async def set_meta(self, uri: str, patch: dict) -> bool:
        """Merge ``patch`` into an artifact's ``meta`` (used to attach provenance).

        Returns ``True`` when the artifact exists and was updated. A shallow merge
        is intentional: callers own the nested keys (e.g. ``meta["provenance"]``).
        """
        if not uri or not patch:
            return False
        art_id = uri[len(_ARTIFACT_SCHEME):] if uri.startswith(_ARTIFACT_SCHEME) else uri
        async with self._db.session() as s:
            row = (
                await s.execute(select(ArtifactORM).where(ArtifactORM.id == art_id))
            ).scalar_one_or_none()
            if row is None:
                return False
            merged = dict(row.meta or {})
            merged.update(patch)
            row.meta = merged
            await s.commit()
        return True


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
