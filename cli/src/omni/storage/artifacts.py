"""File-based artifact store (replaces HelixForge's MinIO object storage).

Exposes a MinIO-compatible-ish surface (``put_bytes`` / ``put_file`` /
``resolve_path`` / ``url_for``) so ported skill engines that return
``*_uri`` fields keep working. URIs use the ``artifact://<id>`` scheme; the
bytes live under ``<project>/artifacts/<kind>/<slug>-<task8>-<art8>.<ext>``
(or one trusted ``<collection>/<task-title>_<task8>/`` bundle per task). Legacy
bare ``<id>.<ext>`` / ``<slug>-<art8>.<ext>`` names remain resolvable via the DB.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

# A task chooses one broad, user-facing collection on its first published
# artifact. Every later format stays beside it. This is deliberately smaller
# than the skill catalog: skills declare artifact kinds; the store owns layout.
_TASK_COLLECTIONS = {
    "report": "reports",
    "document": "reports",
    "figure": "figures",
    "poster": "figures",
    "slides": "presentations",
    "presentation": "presentations",
    "review": "reviews",
    "notebook": "notebooks",
    "data": "datasets",
    "dataset": "datasets",
    "evidence": "datasets",
    "table": "datasets",
}
_OUTPUT_SCOPE_META_KEY = "output_scope"
_OUTPUT_SCOPE_LAYOUT_VERSION = 2


def deliverable_subdirs() -> tuple[str, ...]:
    """The per-kind launch-directory subfolder names, de-duplicated.

    Exposed because the ``@`` mention picker has to keep offering these folders
    even when a repository gitignores them (this one gitignores ``figures/`` and
    ``reports/``). They hold omni's *own* deliverables, and for a research turn
    those are usually the very next thing the user wants to reference — the
    opposite of the build noise gitignore is normally protecting the picker from.
    """
    return tuple(
        dict.fromkeys(
            [*_DELIVERABLE_SUBDIRS.values(), *_TASK_COLLECTIONS.values(), "outputs"]
        )
    )


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


@dataclass(frozen=True)
class _TaskOutputScope:
    output_root: Path
    bundle_dir: Path
    collection: str
    bundle_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "layout_version": _OUTPUT_SCOPE_LAYOUT_VERSION,
            "output_root": str(self.output_root),
            "bundle_dir": str(self.bundle_dir),
            "collection": self.collection,
            "bundle_name": self.bundle_name,
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
        # ``mirror_dir`` is the trusted publication root. Taskless/untrusted
        # bytes retain the durable hidden-store behavior.
        self._mirror_dir = Path(mirror_dir) if mirror_dir else None
        self._mirror_formats = {
            str(f).lower().lstrip(".") for f in (mirror_formats or ())
        }
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._task_scopes: dict[str, _TaskOutputScope] = {}

    @property
    def mirror_dir(self) -> Path | None:
        """The trusted launch/output directory deliverables are written into.

        ``None`` for an untrusted run (durable store only). Revision guards
        consult this to treat a ``.dot`` Omni wrote into ``<output>/figures/``
        as a managed, re-renderable source — the same trust as the store.
        """
        return self._mirror_dir

    @property
    def managed_output_roots(self) -> tuple[Path, ...]:
        """Roots owned by artifact publication and safe for generated files."""
        roots = [self._paths.artifacts_dir.resolve()]
        if self._mirror_dir is not None:
            roots.append(self._mirror_dir.resolve())
        roots.extend(scope.output_root for scope in self._task_scopes.values())
        return tuple(dict.fromkeys(roots))

    def _task_lock(self, task_id: str) -> asyncio.Lock:
        return self._task_locks.setdefault(task_id, asyncio.Lock())

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

    @staticmethod
    def _scope_from_meta(meta: dict[str, Any] | None) -> _TaskOutputScope | None:
        omni_meta = (meta or {}).get("_omni")
        raw = omni_meta.get(_OUTPUT_SCOPE_META_KEY) if isinstance(omni_meta, dict) else None
        if not isinstance(raw, dict):
            return None
        try:
            root = Path(str(raw["output_root"])).expanduser().resolve()
            bundle = Path(str(raw["bundle_dir"])).expanduser().resolve()
            bundle.relative_to(root)
        except (KeyError, OSError, RuntimeError, ValueError):
            return None
        collection = str(raw.get("collection") or "outputs")
        bundle_name = str(raw.get("bundle_name") or bundle.name)
        return _TaskOutputScope(root, bundle, collection, bundle_name)

    @staticmethod
    def _meta_with_scope(
        meta: dict[str, Any] | None, scope: _TaskOutputScope | None
    ) -> dict[str, Any]:
        merged = dict(meta or {})
        raw_omni_meta = merged.get("_omni")
        omni_meta = dict(raw_omni_meta) if isinstance(raw_omni_meta, dict) else {}
        # This key is host authority. A portable skill may add metadata but may
        # not claim an arbitrary filesystem root as trusted.
        omni_meta.pop(_OUTPUT_SCOPE_META_KEY, None)
        if scope is not None:
            omni_meta[_OUTPUT_SCOPE_META_KEY] = scope.as_dict()
        if omni_meta:
            merged["_omni"] = omni_meta
        else:
            merged.pop("_omni", None)
        return merged

    async def _task_scope(
        self,
        task_id: str,
        kind: str,
        *,
        create: bool,
    ) -> _TaskOutputScope | None:
        if not task_id:
            return None
        cached = self._task_scopes.get(task_id)
        if cached is not None:
            return cached
        async with self._db.session() as session:
            task = await session.get(TaskORM, task_id)
            if task is None:
                return None
            rows = (
                await session.execute(
                    select(ArtifactORM)
                    .where(ArtifactORM.task_id == task_id)
                    .order_by(ArtifactORM.created_at.asc())
                )
            ).scalars().all()
            for row in rows:
                persisted = self._scope_from_meta(row.meta)
                if persisted is not None:
                    self._task_scopes[task_id] = persisted
                    return persisted
            if not create or self._mirror_dir is None:
                return None
            collection = _TASK_COLLECTIONS.get(kind.lower(), "outputs")
            title = task.title or task.user_input or "task-output"
        output_root = self._mirror_dir.expanduser().resolve()
        bundle_name = f"{slugify_filename(title) or 'task-output'}_{short_id(task_id)}"
        scope = _TaskOutputScope(
            output_root=output_root,
            bundle_dir=(output_root / collection / bundle_name).resolve(),
            collection=collection,
            bundle_name=bundle_name,
        )
        self._task_scopes[task_id] = scope
        return scope

    async def _task_dest_for(
        self,
        *,
        kind: str,
        title: str,
        art_id: str,
        ext: str,
        task_id: str,
    ) -> tuple[Path, _TaskOutputScope | None]:
        scope = await self._task_scope(task_id, kind, create=True)
        if scope is None:
            return (
                self._dest_for(
                    kind=kind,
                    title=title,
                    art_id=art_id,
                    ext=ext,
                    task_id=task_id,
                ),
                None,
            )
        scope.bundle_dir.mkdir(parents=True, exist_ok=True)
        name = artifact_filename(
            title=title,
            kind=kind,
            art_id=art_id,
            ext=ext,
            task_id=task_id,
        )
        dest = scope.bundle_dir / name
        if dest.exists():
            suffix = ext.lstrip(".")
            dest = scope.bundle_dir / (f"{art_id}.{suffix}" if suffix else art_id)
        return dest, scope

    async def task_output_path(
        self,
        filename: str,
        *,
        task_id: str,
        kind: str = "document",
    ) -> Path:
        """Reserve the stable destination for a new bare task output name."""
        name = Path(filename).name
        if not task_id:
            return self._paths.artifacts_dir / name
        async with self._task_lock(task_id):
            scope = await self._task_scope(task_id, kind, create=True)
            if scope is None:
                return self._paths.artifacts_dir / name
            scope.bundle_dir.mkdir(parents=True, exist_ok=True)
            return scope.bundle_dir / name

    async def existing_task_output_path(
        self,
        filename: str,
        *,
        task_id: str,
        kind: str = "document",
    ) -> Path | None:
        """Return a task bundle path only when its scope is already established."""
        if not task_id:
            return None
        async with self._task_lock(task_id):
            scope = await self._task_scope(task_id, kind, create=False)
            return scope.bundle_dir / Path(filename).name if scope is not None else None

    async def task_label(self, task_id: str = "") -> str:
        """Human title for a task, used to name rewritten contract writes."""
        tid = str(task_id or "").strip()
        if not tid:
            return ""
        async with self._db.session() as session:
            task = await session.get(TaskORM, tid)
        if task is None:
            return ""
        return str(task.title or task.user_input or "")

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
        async def store() -> StoredArtifact:
            art_id = _uuid()
            dest, scope = await self._task_dest_for(
                kind=kind,
                title=title,
                art_id=art_id,
                ext=ext,
                task_id=task_id,
            )
            dest.write_bytes(data)
            artifact = await self._record(
                art_id,
                dest,
                kind,
                title,
                mime,
                session_id,
                task_id,
                subtask_id,
                workflow_run_id,
                self._meta_with_scope(meta, scope),
            )
            if scope is not None:
                await self._write_manifest(task_id, scope)
            return artifact

        if not task_id:
            return await store()
        async with self._task_lock(task_id):
            return await store()

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

        async def store() -> StoredArtifact:
            art_id = _uuid()
            dest, scope = await self._task_dest_for(
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
            artifact = await self._record(
                art_id,
                dest,
                kind,
                title or src.name,
                mime,
                session_id,
                task_id,
                subtask_id,
                workflow_run_id,
                self._meta_with_scope(meta, scope),
            )
            if scope is not None:
                await self._write_manifest(task_id, scope)
            return artifact

        if not task_id:
            return await store()
        async with self._task_lock(task_id):
            return await store()

    async def register_existing(
        self,
        src: Path,
        *,
        kind: str = "document",
        title: str = "",
        mime: str = "text/markdown",
        session_id: str = "",
        task_id: str = "",
        meta: dict | None = None,
    ) -> StoredArtifact | None:
        """Record a file the turn wrote, leaving it exactly where it was written.

        ``put_file`` copies into the store's own layout, which is right for
        something being *collected* but wrong for something the model already
        placed and told the user about — the user would end up with two files and
        a reported path that is not the registered one. Registration is keyed on
        the path, so a document built through several appends stays one entry
        whose size follows the file rather than N entries for N calls.

        Returns ``None`` when there is nothing to record.
        """
        src = Path(src)

        async def register() -> StoredArtifact | None:
            if not src.is_file():
                return None
            rel_path_value = self._rel_path_value(src)
            existing = await self._find_by_path(rel_path_value, task_id)
            if existing is not None:
                async with self._db.session() as session:
                    row = await session.get(ArtifactORM, existing)
                    if row is not None:
                        row.size_bytes = src.stat().st_size
                        scope = self._scope_from_meta(row.meta)
                        await session.commit()
                        if scope is not None:
                            await self._write_manifest(task_id, scope)
                        return StoredArtifact(
                            row.id,
                            row.uri,
                            src,
                            row.kind,
                            row.title,
                            row.mime,
                            row.size_bytes,
                        )
            scope = await self._task_scope(task_id, kind, create=False)
            if scope is not None:
                try:
                    src.resolve().relative_to(scope.bundle_dir)
                except ValueError:
                    scope = None
            artifact = await self._record(
                _uuid(),
                src,
                kind,
                title or src.stem,
                mime,
                session_id,
                task_id,
                "",
                "",
                self._meta_with_scope(meta, scope),
            )
            if scope is not None:
                await self._write_manifest(task_id, scope)
            return artifact

        if not task_id:
            return await register()
        async with self._task_lock(task_id):
            return await register()

    def _rel_path_value(self, dest: Path) -> str:
        """How ``dest`` is stored: project-relative when inside, else absolute."""
        try:
            return str(dest.relative_to(self._paths.project_dir))
        except ValueError:
            return str(dest.resolve())

    async def _find_by_path(self, rel_path_value: str, task_id: str) -> str | None:
        """Id of this task's artifact already recorded at that path, if any."""
        if not task_id:
            return None
        async with self._db.session() as s:
            return (
                await s.execute(
                    select(ArtifactORM.id).where(
                        ArtifactORM.task_id == task_id,
                        ArtifactORM.rel_path == rel_path_value,
                    )
                )
            ).scalars().first()

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
        # A deliverable written straight into the trusted launch directory lives
        # outside the durable store; its absolute path is recorded so
        # ``resolve_path`` can still map the URI back to the single copy.
        rel_path_value = self._rel_path_value(dest)
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
        # Deliverables are written straight to their final location (a task
        # bundle when trusted, else the durable store), so there is exactly one
        # copy and no separate mirror step.
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
            candidate = Path(rel).resolve()
            scope = self._scope_from_meta(row.meta)
            trusted_root = scope.output_root if scope is not None else self._mirror_dir
            if trusted_root is None:
                return None
            try:
                candidate.relative_to(trusted_root.resolve())
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

    async def list_by_task(self, task_id: str) -> list[ArtifactORM]:
        """Canonical artifacts produced by one task, oldest first."""
        if not task_id:
            return []
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(ArtifactORM)
                    .where(ArtifactORM.task_id == task_id)
                    .order_by(ArtifactORM.created_at.asc())
                )
            ).scalars().all()
        return list(rows)

    async def _write_manifest(
        self, task_id: str, scope: _TaskOutputScope
    ) -> None:
        """Atomically refresh the portable inventory for one task bundle."""
        rows = await self.list_by_task(task_id)
        async with self._db.session() as session:
            task = await session.get(TaskORM, task_id)
            title = task.title if task is not None else ""
        artifacts: list[dict[str, Any]] = []
        for row in rows:
            row_scope = self._scope_from_meta(row.meta)
            if row_scope is None or row_scope.bundle_dir != scope.bundle_dir:
                continue
            path = Path(row.rel_path)
            if not path.is_absolute():
                path = self._paths.project_dir / path
            try:
                relative = path.resolve().relative_to(scope.bundle_dir)
            except ValueError:
                continue
            artifacts.append(
                {
                    "id": row.id,
                    "uri": row.uri,
                    "title": row.title,
                    "kind": row.kind,
                    "path": str(relative),
                    "mime": row.mime,
                    "size_bytes": row.size_bytes,
                }
            )
        payload = {
            "layout_version": _OUTPUT_SCOPE_LAYOUT_VERSION,
            "task_id": task_id,
            "title": title,
            "collection": scope.collection,
            "artifacts": artifacts,
        }
        scope.bundle_dir.mkdir(parents=True, exist_ok=True)
        target = scope.bundle_dir / "_omni-manifest.json"
        temporary = scope.bundle_dir / f".manifest-{_uuid()}.tmp"
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

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


class ContextArtifactStore:
    """ArtifactStore facade that supplies ownership from a live ExecContext.

    Skill engines should not have to remember every host attribution field.
    Reading the context lazily also matters because the durable runtime assigns
    subtask/workflow ids after constructing the context, and isolation uses
    ``dataclasses.replace`` to derive child contexts.
    """

    def __init__(self, store: ArtifactStore, context: Any) -> None:
        self._store = store
        self._context = context

    def for_context(self, context: Any) -> ContextArtifactStore:
        return ContextArtifactStore(self._store, context)

    def _owned(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        owned = dict(kwargs)
        for name in ("session_id", "task_id", "subtask_id", "workflow_run_id"):
            value = getattr(self._context, name, "") or ""
            if value:
                owned.setdefault(name, value)
        return owned

    @property
    def mirror_dir(self) -> Path | None:
        return self._store.mirror_dir

    @property
    def managed_output_roots(self) -> tuple[Path, ...]:
        return self._store.managed_output_roots

    async def put_bytes(self, data: bytes, **kwargs: Any) -> StoredArtifact:
        return await self._store.put_bytes(data, **self._owned(kwargs))

    async def put_file(self, src: Path, **kwargs: Any) -> StoredArtifact:
        return await self._store.put_file(src, **self._owned(kwargs))

    async def register_existing(
        self, src: Path, **kwargs: Any
    ) -> StoredArtifact | None:
        # register_existing intentionally has no subtask/workflow parameters.
        owned = self._owned(kwargs)
        owned.pop("subtask_id", None)
        owned.pop("workflow_run_id", None)
        return await self._store.register_existing(src, **owned)

    async def task_output_path(
        self, filename: str, *, kind: str = "document"
    ) -> Path:
        return await self._store.task_output_path(
            filename,
            task_id=str(getattr(self._context, "task_id", "") or ""),
            kind=kind,
        )

    async def existing_task_output_path(
        self, filename: str, *, kind: str = "document"
    ) -> Path | None:
        return await self._store.existing_task_output_path(
            filename,
            task_id=str(getattr(self._context, "task_id", "") or ""),
            kind=kind,
        )

    async def task_label(self, task_id: str = "") -> str:
        return await self._store.task_label(
            task_id or str(getattr(self._context, "task_id", "") or "")
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
