"""Trusted compute I/O: private scratch/outbox, host publish into the task bundle.

Codex ``WorkspaceWrite`` is cwd + configured roots + a host temp — never
``CODEX_HOME``. Omni keeps a separate ArtifactStore, so a compute process
writes deliverables to a host-owned outbox (``$OMNI_OUTPUT_DIR``); the host
then copies them into the user-facing task folder
(``outputs/<title>_<task8>/`` when a launch ``--out`` is set) and registers
that copy. Scratch is per-task, mode ``0700``, and refuses symlinks so a
planted link cannot reopen the control plane. ``~/.omni/.../artifacts/promoted``
is not a user-visible location.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omni.config.paths import (
    frozen_control_stores,
    is_within_home,
    opens_any_control_store,
    opens_control_store,
    sits_in_any_control_store,
    workspace_key,
)

OMNI_OUTPUT_ENV = "OMNI_OUTPUT_DIR"
OMNI_SKILL_ENV = "OMNI_SKILL_DIR"
_DEVICE_WRITE_ROOTS = ("/dev",)

# Windows rejects ``:`` (and a few others) in a path component. Subagent ids
# keep ``::sub-`` as an in-memory delimiter; the compute sink must not reuse
# that spelling as a directory name.
_UNSAFE_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
logger = logging.getLogger(__name__)


def compute_dir_key(raw: str) -> str:
    """Turn a task or session id into a directory name every host can mkdir."""
    key = _UNSAFE_FS.sub("-", (raw or "").strip()).strip(" .")
    return key or "ad-hoc"


def _paths_home(paths: Any) -> Path | None:
    home = getattr(paths, "home", None)
    return Path(home) if home is not None else None


def _is_world_writable_tmp(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved in {Path("/tmp"), Path("/private/tmp"), Path("/var/tmp"), Path("/private/var/tmp")}


def ensure_private_dir(path: Path) -> Path:
    """Create *path* at mode ``0700`` and refuse a leaf that reopens control state.

    A planted symlink leaf, or a parent whose resolve sits inside a frozen
    store, is refused before ``mkdir``. OS aliases such as ``/tmp`` →
    ``/private/tmp`` are not a hit: those ancestors are not inside the store.
    """
    dest = Path(path)
    if dest.is_symlink():
        raise RuntimeError(f"refusing to use a symlinked exec dir: {dest}")
    try:
        parent_r = dest.parent.resolve()
    except OSError as exc:
        raise RuntimeError(f"cannot resolve exec path parent: {dest.parent}") from exc
    if sits_in_any_control_store(parent_r):
        raise RuntimeError(
            f"refusing to create an exec dir inside Omni control state: {dest}"
        )
    dest.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        raise RuntimeError(f"refusing to use a symlinked exec dir: {dest}")
    if sits_in_any_control_store(dest):
        raise RuntimeError(
            f"refusing to use an exec dir inside Omni control state: {dest}"
        )
    if os.name != "nt":
        os.chmod(dest, 0o700)
        mode = dest.lstat().st_mode
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise RuntimeError(f"exec dir is not a private directory: {dest}")
    return dest.resolve()


def host_scratch_base(store: Path) -> Path:
    """User-private temp root, never the world-writable ``/tmp`` or the store.

    Codex uses the per-user ``TMPDIR`` (``/var/folders/...`` on macOS), not a
    predictable path under ``/tmp``. A parent Omni that pointed ``TMPDIR`` at
    the store must not force us to nest there.
    """
    store_r = store.resolve()
    account_home: Path | None = None
    try:
        from omni.config.paths import os_user_home

        process_home = Path.home().resolve()
        account = os_user_home()
        if process_home != account:
            # HOME was remapped (pytest). Do not write the real account cache.
            account_home = account
    except OSError:
        account_home = None
    candidates: list[Path] = []
    xdg_cache = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg_cache:
        candidates.append(Path(xdg_cache) / "omni-exec")
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg_runtime:
        candidates.append(Path(xdg_runtime) / "omni-exec")
    candidates.append(Path.home() / ".cache" / "omni-exec")
    inherited = Path(tempfile.gettempdir())
    if (
        "omni-exec" not in inherited.parts
        and not is_within_home(inherited, store_r)
        and not opens_control_store(inherited, store_r)
        and not _is_world_writable_tmp(inherited)
    ):
        candidates.append(inherited / "omni-exec")

    for base in candidates:
        try:
            if is_within_home(base, store_r) or opens_control_store(base, store_r):
                continue
            if _is_world_writable_tmp(base):
                continue
            if account_home is not None and is_within_home(base, account_home):
                continue
            return ensure_private_dir(base)
        except (OSError, RuntimeError):
            continue
    return ensure_private_dir(Path.home() / ".cache" / "omni-exec")


def exec_namespace(paths: Any, *, task_key: str = "ad-hoc") -> Path:
    """Per-store, per-workspace, per-task directory under the private temp root."""
    home = _paths_home(paths)
    project = getattr(paths, "project_dir", None)
    if home is None or project is None:
        fallback = getattr(paths, "artifacts_dir", None)
        root = Path(fallback) if fallback is not None else Path(tempfile.gettempdir())
        return ensure_private_dir(root / "omni-exec" / compute_dir_key(task_key))
    home_r = Path(home).resolve()
    project_r = Path(project).resolve()
    home_token = hashlib.sha256(str(home_r).encode("utf-8")).hexdigest()[:12]
    dest = (
        host_scratch_base(home_r)
        / home_token
        / workspace_key(project_r)
        / compute_dir_key(task_key)
    )
    return ensure_private_dir(dest)


def _task_key(ctx: Any) -> str:
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    session_id = str(getattr(ctx, "session_id", "") or "").strip()
    return compute_dir_key(task_id or session_id or "ad-hoc")


def durable_output_dir(ctx: Any) -> Path:
    """Task-scoped outbox for compute deliverables (outside the store)."""
    paths = getattr(ctx, "paths", None)
    key = _task_key(ctx)
    home = _paths_home(paths)
    if home is not None and getattr(paths, "project_dir", None) is not None:
        dest = exec_namespace(paths, task_key=key) / "outbox"
    else:
        dest = Path(paths.artifacts_dir) / "compute" / key
    return ensure_private_dir(dest)


def exec_tmp_dir(ctx: Any) -> Path:
    """Per-task scratch directory for this workspace's sandboxed processes."""
    paths = getattr(ctx, "paths", None)
    key = _task_key(ctx)
    home = _paths_home(paths)
    if home is not None and getattr(paths, "project_dir", None) is not None:
        dest = exec_namespace(paths, task_key=key) / "exec"
    else:
        dest = Path(paths.project_dir) / "tmp" / "exec" / key
    return ensure_private_dir(dest)


def extra_exec_roots(ctx: Any) -> list[Path]:
    """Roots compute tools add on top of ``write_roots_for``."""
    return [durable_output_dir(ctx), exec_tmp_dir(ctx)]


def compute_io_vars(ctx: Any) -> dict[str, str]:
    """Host-owned variables that point a process at trusted I/O and the skill root."""
    output = durable_output_dir(ctx)
    scratch = exec_tmp_dir(ctx)
    env = {
        OMNI_OUTPUT_ENV: str(output),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
    }
    skill_root = getattr(ctx, "skill_root", None)
    if skill_root:
        env[OMNI_SKILL_ENV] = str(Path(skill_root).resolve())
    return env


def compute_env(ctx: Any, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a sandboxed process: durable output + persistent TMPDIR."""
    env = dict(os.environ if base is None else base)
    env.update(compute_io_vars(ctx))
    return env


def kernel_write_roots(ctx: Any, extra: Sequence[Path | str] = ()) -> list[str]:
    """OS-sandbox write roots: user source + outbox + scratch, never the store.

    Codex ``WorkspaceWrite`` is cwd + additional roots + a host temp. Omni does
    **not** add ``$OMNI_HOME``, the project store, or the whole of ``/tmp``.
    Approval may pass extra roots (for example ``cwd/.git``) but those are
    still dropped if they would open control state.
    """
    paths = ctx.paths
    stores = frozen_control_stores()
    extra_allow = getattr(getattr(ctx, "settings", None), "security", None)
    allow = tuple(getattr(extra_allow, "fs_write_allow", ()) or ())
    managed = getattr(getattr(ctx, "artifacts", None), "managed_output_roots", ()) or ()

    candidates: list[Path] = []
    working = getattr(ctx, "working_dir", None) or getattr(paths, "invocation_cwd", None)
    if working is not None:
        candidates.append(Path(working))
    workspace = getattr(paths, "workspace_root", None)
    if workspace is not None:
        candidates.append(Path(workspace))
    for raw in allow:
        candidates.append(Path(raw).expanduser())
    for root in managed:
        candidates.append(Path(root))
    candidates.extend(extra_exec_roots(ctx))
    candidates.extend(Path(raw) for raw in extra)
    candidates.extend(Path(raw) for raw in _DEVICE_WRITE_ROOTS)

    roots: list[str] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if opens_any_control_store(resolved, stores):
            continue
        roots.append(str(resolved))
    return _dedupe_roots(roots)


def input_write_roots(payload: Any) -> list[Path]:
    """Directories implied by absolute paths the host already put in skill input.

    Codex ``WorkspaceWrite`` is cwd + additional roots. A ``cli_exec`` skill
    often writes a counter or log at a path the host chose. Those parents are
    granted for this spawn only; ``kernel_write_roots`` still drops any root
    that would open a frozen control store (so ``tmp_path`` beside isolated
    ``.omni`` is not writable — put I/O in a subdirectory).
    """
    found: list[Path] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
            return
        if not isinstance(value, str) or not value.strip():
            return
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            return
        target = raw if raw.is_dir() else raw.parent
        if target == target.parent:
            return
        found.append(target)

    walk(payload)
    return found


def confined_exec_prefix(
    ctx: Any, extra_writable: Sequence[Path | str] = ()
) -> list[str]:
    """Argv prefix shared by bash, local ``run_compute``, and ``cli_exec``."""
    from omni.skills_runtime.sandbox import sandbox_prefix

    return sandbox_prefix(
        ctx.settings.security,
        ctx.paths,
        writable_roots=kernel_write_roots(ctx, extra_writable),
        persist_tmp=exec_tmp_dir(ctx),
        warn_on_fallback=True,
    )


def _dedupe_roots(roots: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in roots:
        if not raw or raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


def _artifact_inner(store: Any) -> Any:
    return getattr(store, "_store", store)


def user_facing_output_roots(ctx: Any) -> list[Path]:
    """Launch-directory deliverable roots. Never ``$OMNI_HOME`` or ``artifacts/``."""
    roots: list[Path] = []
    store = getattr(ctx, "artifacts", None)
    inner = _artifact_inner(store)
    mirror = getattr(store, "mirror_dir", None) or getattr(inner, "mirror_dir", None)
    if mirror is not None:
        roots.append(Path(mirror))
    for scope in getattr(inner, "_task_scopes", {}) or {}.values():
        for attr in ("bundle_dir", "output_root"):
            raw = getattr(scope, attr, None)
            if raw is not None:
                roots.append(Path(raw))
    resolved: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            value = root.expanduser().resolve()
        except OSError:
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(value)
    return resolved


def _inside_control_store(path: Path, ctx: Any) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    artifacts = getattr(getattr(ctx, "paths", None), "artifacts_dir", None)
    if artifacts is not None:
        try:
            resolved.relative_to(Path(artifacts).resolve())
            return True
        except (ValueError, OSError):
            pass
    return sits_in_any_control_store(resolved)


def _inside_user_facing_root(ctx: Any, path: Path) -> bool:
    """True when *path* already lives in the user-facing task folder.

    ``project_dir`` is the control-plane store (``~/.omni/workspaces/...``),
    the Codex ``$CODEX_HOME`` analogue — not the user's checkout. A file under
    ``artifacts/promoted/`` is therefore *not* a published deliverable.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if _inside_control_store(resolved, ctx):
        return False
    for root in user_facing_output_roots(ctx):
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# Bulk harvest of $OMNI_OUTPUT_DIR. A model that dumps a venv into the outbox
# used to register thousands of site-packages files as task artifacts.
_HARVEST_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "virtualenv",
        "site-packages",
        "dist-packages",
        "node_modules",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
    }
)
_HARVEST_SKIP_DIR_SUFFIXES = (".dist-info", ".egg-info")
_HARVEST_SKIP_FILENAMES = frozenset(
    {
        "license",
        "license.txt",
        "license.md",
        "licence",
        "licence.txt",
        "notice",
        "notice.txt",
        "notice.md",
        "copying",
        "authors",
        "authors.txt",
        "pyvenv.cfg",
        "pip-selfcheck.json",
    }
)
_HARVEST_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".tex",
        ".html",
        ".csv",
        ".json",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".pptx",
        ".ppt",
        ".pdf",
        ".docx",
        ".doc",
        ".py",
        ".dot",
        ".gv",
        ".ipynb",
    }
)


def harvestable_output(path: Path, root: Path) -> bool:
    """Whether a file under the outbox is a scientific deliverable, not junk.

    ``register_output_dir`` used to ``rglob("*")`` and promote every regular
    file. A bash fallback that created ``.venv`` then registered LICENSE,
    ``site-packages``, and thousands of wheel files as the turn's artifacts.
    """
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    if not resolved.is_file() or resolved.name.startswith("."):
        return False
    if resolved.name.lower() in _HARVEST_SKIP_FILENAMES:
        return False
    for part in resolved.relative_to(root.resolve()).parts[:-1]:
        lowered = part.lower()
        if lowered in _HARVEST_SKIP_DIR_NAMES or lowered.endswith(
            _HARVEST_SKIP_DIR_SUFFIXES
        ):
            return False
    return resolved.suffix.lower() in _HARVEST_SUFFIXES


async def _task_bundle_dest(ctx: Any, src: Path, kind: str) -> Path | None:
    """Stable destination in the current task folder, preserving the filename."""
    store = getattr(ctx, "artifacts", None)
    locator = getattr(store, "task_output_path", None)
    if not callable(locator):
        return None
    try:
        dest = await locator(src.name, kind=kind)
    except TypeError:
        dest = await locator(src.name, task_id=str(ctx.task_id), kind=kind)
    return Path(dest) if dest is not None else None


async def _write_task_manifest(ctx: Any, kind: str) -> None:
    inner = _artifact_inner(getattr(ctx, "artifacts", None))
    writer = getattr(inner, "_write_manifest", None)
    scope_fn = getattr(inner, "_task_scope", None)
    if not callable(writer) or not callable(scope_fn):
        return
    try:
        scope = await scope_fn(str(ctx.task_id), kind, create=False)
    except TypeError:
        return
    if scope is not None:
        await writer(str(ctx.task_id), scope)


async def _publish_outbox_file(ctx: Any, src: Path, kind: str, mime: str) -> None:
    """Copy an outbox file into the user-facing task bundle and register it.

    Codex imagegen: a project-referenced asset must not remain only under
    ``$CODEX_HOME``. Omni's analogue is ``outputs/<title>_<task8>/`` (or the
    store-local kind folder when no launch ``--out`` is set). ``artifacts/promoted``
    is never the registered location.
    """
    from omni.skills_runtime.builtin_tools.fs import register_written_file

    store = getattr(ctx, "artifacts", None)
    if _inside_user_facing_root(ctx, src):
        await register_written_file(ctx, src)
        return
    dest = await _task_bundle_dest(ctx, src, kind)
    if dest is None:
        await store.put_file(
            src,
            kind=kind,
            title=src.stem,
            mime=mime,
            session_id=str(getattr(ctx, "session_id", "") or ""),
            task_id=ctx.task_id,
            copy=True,
        )
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() != src.resolve():
        shutil.copy2(src, dest)
    await register_written_file(ctx, dest)
    await _write_task_manifest(ctx, kind)


async def register_output_dir(ctx: Any, directory: Path | None = None) -> int:
    """Publish outbox files into the task bundle, then register that copy.

    Sandboxed processes write outside the store. The host copies each
    harvestable file into ``outputs/<title>_<task8>/`` (when a launch output
    root is set) under a stable filename so ``artifact://`` survives outbox
    cleanup and a second harvest does not create a new UUID object. Control
    store paths (``~/.omni/.../artifacts/promoted``) are never the published
    location.
    """
    from omni.skills_runtime.builtin_tools.fs import document_kind_for

    store = getattr(ctx, "artifacts", None)
    if store is None or not str(getattr(ctx, "task_id", "") or ""):
        return 0
    root = Path(directory or durable_output_dir(ctx)).resolve()
    if not root.is_dir():
        return 0
    count = 0
    for path in sorted(root.rglob("*")):
        if not harvestable_output(path, root):
            continue
        kind, mime = document_kind_for(path)
        try:
            await _publish_outbox_file(ctx, path, kind, mime)
            count += 1
        except Exception:  # noqa: BLE001 - inventory is not worth failing a good write over
            logger.debug("artifact.promote_failed path=%s", path, exc_info=True)
    return count


__all__ = [
    "OMNI_OUTPUT_ENV",
    "OMNI_SKILL_ENV",
    "compute_dir_key",
    "compute_env",
    "compute_io_vars",
    "confined_exec_prefix",
    "durable_output_dir",
    "ensure_private_dir",
    "exec_namespace",
    "exec_tmp_dir",
    "extra_exec_roots",
    "harvestable_output",
    "host_scratch_base",
    "input_write_roots",
    "kernel_write_roots",
    "register_output_dir",
    "user_facing_output_roots",
]
