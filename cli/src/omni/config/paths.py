"""Path resolution for OmniScientist.

Mirrors the ``~/.claude`` / ``~/.codex`` (``CODEX_HOME``) conventions, but keys
a *workspace* by its absolute root path (like Claude Code) so every terminal
launched in the same project shares one durable store — no surprising
``default`` bucket and no treating ``~/.omni`` (the home) as a project.

- User home:    ``$OMNI_HOME``, a persisted user selection, or ``~/.omni``
- Workspace dir, resolved in order:
  1. explicit ``-P <name>`` → ``<home>/projects/<name>`` (named project)
  2. in-place ``.omni/`` walking up from CWD (excluding the home dir)
  3. the enclosing VCS root (``.git``/``.hg``), keyed by absolute path →
     ``<home>/workspaces/<slug>-<hash8>``
  4. otherwise the resolved CWD, keyed the same way

All filesystem layout decisions live here so the rest of the code never
hard-codes a path.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

_HOME_ENV = "OMNI_HOME"
_HOME_POINTER_NAME = "home"
_PROJECT_MARKER = ".omni"
_VCS_MARKERS = (".git", ".hg")
# Named project used when resolution would otherwise key a workspace off the
# Omni home itself (see ``get_paths`` / ``is_within_home``).
_HOME_FALLBACK_PROJECT = "default"

# Project-level skill roots discovered by walking up from the CWD, mirroring how
# Claude Code (``.claude/skills``), Codex / OpenClaw (``.agents/skills``) and
# OmniScientist (``.omni/skills``) locate in-repo skills.
_PROJECT_SKILL_SUBDIRS = (
    (".omni/skills", "project_omni"),
    (".claude/skills", "project_claude"),
    (".agents/skills", "project_agents"),
)


def default_user_home() -> Path:
    """Return the default OmniScientist data directory."""
    return (Path.home() / ".omni").resolve()


def home_selection_file() -> Path:
    """Return the stable bootstrap file that stores a custom data directory.

    The selection cannot live inside the selected data directory: doing so
    would make the directory undiscoverable on the next process start. Follow
    the XDG convention on POSIX and APPDATA on Windows without adding a
    platform-specific dependency.
    """
    if raw := os.environ.get("XDG_CONFIG_HOME", "").strip():
        root = Path(raw).expanduser()
    elif os.name == "nt" and (raw := os.environ.get("APPDATA", "").strip()):
        root = Path(raw).expanduser()
    else:
        root = Path.home() / ".config"
    return (root / "omni" / _HOME_POINTER_NAME).resolve()


def _saved_user_home() -> Path | None:
    pointer = home_selection_file()
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    target = Path(raw).expanduser().resolve()
    try:
        _validate_user_home_target(target)
    except ValueError:
        return None
    return target


def _validate_user_home_target(target: Path) -> None:
    """Reject roots that could turn a purge or workspace scan destructive."""
    if target == Path.home().resolve():
        raise ValueError("the Omni data directory cannot be the user home itself")
    if target == target.parent:
        raise ValueError("the Omni data directory cannot be a filesystem root")
    if target.exists() and not target.is_dir():
        raise ValueError(f"the Omni data directory is not a directory: {target}")


def user_home_resolution() -> tuple[Path, str]:
    """Return ``(path, source)`` for the active Omni data directory."""
    raw = os.environ.get(_HOME_ENV, "").strip()
    if raw:
        return Path(raw).expanduser().resolve(), "environment (OMNI_HOME)"
    if saved := _saved_user_home():
        return saved, f"saved selection ({home_selection_file()})"
    return default_user_home(), "default"


def user_home() -> Path:
    """Return the active OmniScientist data directory."""
    return user_home_resolution()[0]


def configure_user_home(path: str | Path) -> Path:
    """Persist ``path`` as the data directory used by future processes.

    Existing data is deliberately not moved or deleted. Selecting the default
    removes the bootstrap pointer so a clean install retains no extra state.
    """
    target = Path(path).expanduser().resolve()
    _validate_user_home_target(target)
    target.mkdir(parents=True, exist_ok=True)

    pointer = home_selection_file()
    if target == default_user_home():
        try:
            pointer.unlink()
        except FileNotFoundError:
            pass
        return target

    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_name(f".{pointer.name}.tmp")
    temporary.write_text(f"{target}\n", encoding="utf-8")
    temporary.replace(pointer)
    return target


def reset_user_home() -> Path:
    """Forget the persisted selection and return the default data directory."""
    pointer = home_selection_file()
    try:
        pointer.unlink()
    except FileNotFoundError:
        pass
    return default_user_home()


def codex_home() -> Path:
    """Return Codex's home (``$CODEX_HOME`` or ``~/.codex``)."""
    raw = os.environ.get("CODEX_HOME", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def iter_project_skill_dirs(start: Path | None = None, subpath: str = ".claude/skills") -> list[Path]:
    """Walk up from ``start`` collecting existing ``<dir>/<subpath>`` roots.

    Mirrors how Claude Code / Codex discover *project* skills from the CWD up to
    the repository root. The walk stops after the first directory containing a
    VCS marker (inclusive), at the user's home directory (exclusive — anything at
    or above ``~`` is a *user* root, handled by the ``user_*`` sources, not a
    project root), or at the filesystem root.
    """
    cur = (start or Path.cwd()).resolve()
    home = Path.home().resolve()
    out: list[Path] = []
    for directory in (cur, *cur.parents):
        # Don't treat the home dir (or above) as a project root.
        if directory == home:
            break
        candidate = directory / subpath
        if candidate.is_dir():
            out.append(candidate)
        if any((directory / m).exists() for m in _VCS_MARKERS):
            break
    return out


def _slugify(name: str) -> str:
    """A short, filesystem-safe label for a workspace directory name."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")
    return (slug or "workspace")[:48]


def workspace_key(root: Path) -> str:
    """Stable, collision-free dir name for a workspace keyed by absolute path.

    ``<slug>-<hash8>`` keeps it human-readable while the path hash prevents two
    same-named directories (e.g. two ``app`` repos) from colliding.
    """
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{_slugify(root.name)}-{digest}"


def is_within_home(path: Path, home: Path | None = None) -> bool:
    """True when ``path`` is the Omni home (``user_home()``) or lives inside it.

    Used to refuse keying a *workspace* off the Omni home itself: launching a
    ``omni serve`` / REPL from inside ``~/.omni/workspaces/<x>`` must never spawn
    a nested ``<x>-<hash>`` ghost workspace (which would then poll the same IM
    bots as the real daemon).
    """
    base = home or user_home()
    try:
        base = base.resolve()
    except OSError:
        pass
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    # ``Path.__eq__`` / ``in parents`` are case-insensitive on Windows and
    # case-sensitive on POSIX, matching each platform's filesystem semantics.
    return resolved == base or base in resolved.parents


def find_vcs_root(start: Path | None = None) -> Path | None:
    """Nearest enclosing VCS root (``.git``/``.hg``), never the home dir or above."""
    cur = (start or Path.cwd()).resolve()
    home = Path.home().resolve()
    for directory in (cur, *cur.parents):
        if directory == home:  # don't treat the home dir (or above) as a workspace
            break
        if any((directory / m).exists() for m in _VCS_MARKERS):
            return directory
    return None


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for an in-place ``.omni`` project dir.

    Stops at a VCS root, the home directory, or the filesystem root. Returns the
    directory that *contains* the ``.omni`` folder, or ``None`` when there is no
    in-place project (callers then fall back to path-keyed workspace resolution).
    The home guard prevents ``~/.omni`` (the user home) from ever being mistaken
    for an in-place project root.
    """
    cur = (start or Path.cwd()).resolve()
    home = Path.home().resolve()
    for directory in (cur, *cur.parents):
        if directory == home:
            break
        if (directory / _PROJECT_MARKER).is_dir():
            return directory
        if any((directory / m).exists() for m in _VCS_MARKERS):
            # Reached a repo boundary without an .omni dir → no in-place project.
            return None
    return None


@dataclass(frozen=True)
class OmniPaths:
    """Resolved filesystem locations for a given home + active project."""

    home: Path
    project_name: str
    project_dir: Path
    # Absolute workspace root this project was keyed from (``None`` for named
    # ``-P`` projects). Used by the workspace registry and ``--all`` views.
    workspace_root: Path | None = None
    # Absolute directory the CLI was launched from (resolved ``cwd``). Unlike
    # ``workspace_root`` (which is ``None`` for ``-P`` and home/non-git launches
    # and points at the VCS root otherwise) this always records exactly where the
    # user is, so local file/shell tools can operate on that folder like Claude
    # Code does. ``None`` only for directly-constructed paths (tests/eval).
    invocation_cwd: Path | None = None

    # ── user-level files ──
    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def secrets_file(self) -> Path:
        return self.home / "secrets.toml"

    @property
    def role_file(self) -> Path:
        return self.home / "role.md"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def user_skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def channels_dir(self) -> Path:
        return self.home / "channels"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def projects_dir(self) -> Path:
        return self.home / "projects"

    @property
    def workspaces_dir(self) -> Path:
        return self.home / "workspaces"

    @property
    def service_dir(self) -> Path:
        """Home-level background service state (``~/.omni/service``).

        Holds the machine-level control service's persisted *desired* state
        (``settings.json``) and observed *runtime* state (``service.pid``),
        kept out of any single workspace so one supervised service can own
        channels and dispatch schedules for every registered workspace.
        """
        return self.home / "service"

    # ── machine-global memory (cross-workspace / cross-CLI / cross-channel) ──
    @property
    def global_memory_db(self) -> Path:
        """Machine-global long-term memory store (``~/.omni/memory.sqlite3``).

        Lives in the data *home* (not a workspace) so every CLI, workspace and
        the ``omni serve`` daemon on this machine share one durable store for the
        owner's identity/preference memory. Workspace-bound rows (sessions,
        tasks, artifacts, project findings) stay in the per-workspace
        ``project_db``; only cross-workspace identity memory routes here.
        """
        return self.home / "memory.sqlite3"

    @property
    def control_db(self) -> Path:
        """Machine-global control-plane store (``~/.omni/control.sqlite3``).

        Holds cross-workspace *control* rows that belong to the machine owner
        rather than any single project — currently the durable schedule-approval
        proposals. An IM turn is served on one anchor workspace while the owner
        approves from whatever repo they are in; keying these rows to the home
        (not a ``sessions.sqlite3``) is what lets ``omni schedule approve`` find
        a proposal regardless of the workspace the CLI resolves to.
        """
        return self.home / "control.sqlite3"

    @property
    def memories_dir(self) -> Path:
        """Human-readable + injected global memory files (``~/.omni/memories``)."""
        return self.home / "memories"

    @property
    def memory_summary_file(self) -> Path:
        """The small, always-injected global memory digest (``memory_summary.md``)."""
        return self.memories_dir / "memory_summary.md"

    # ── Claude Code / Codex / OpenClaw compatible discovery roots ──
    @property
    def claude_user_skills(self) -> Path:
        """Claude Code personal skills (``~/.claude/skills``)."""
        return Path.home() / ".claude" / "skills"

    @property
    def agents_user_skills(self) -> Path:
        """Shared agent skills root (``~/.agents/skills``) — Codex (current) + OpenClaw."""
        return Path.home() / ".agents" / "skills"

    @property
    def codex_user_skills(self) -> Path:
        """Codex personal skills (``$CODEX_HOME/skills`` or ``~/.codex/skills``)."""
        return codex_home() / "skills"

    @property
    def openclaw_user_skills(self) -> Path:
        """OpenClaw managed skills (``~/.openclaw/skills``)."""
        return Path.home() / ".openclaw" / "skills"

    # ── project-level files ──
    @property
    def project_config(self) -> Path:
        return self.project_dir / "project.toml"

    @property
    def project_db(self) -> Path:
        return self.project_dir / "sessions.sqlite3"

    @property
    def notebook(self) -> Path:
        return self.project_dir / "NOTEBOOK.md"

    @property
    def library(self) -> Path:
        return self.project_dir / "library.jsonl"

    @property
    def artifacts_dir(self) -> Path:
        return self.project_dir / "artifacts"

    @property
    def local_ops_dir(self) -> Path:
        """Directory local file/shell tools should operate in for a local turn.

        Prefers the launch directory (``invocation_cwd``) so tools act on the
        folder the user is actually in — Claude-Code-style — then the workspace
        root, then the data store. The filesystem root is never returned (it is
        too broad a write/exec root). Callers restrict this to local CLI turns;
        IM/daemon turns keep using ``workspace_root``.
        """
        for candidate in (self.invocation_cwd, self.workspace_root, self.project_dir):
            if candidate is None:
                continue
            resolved = candidate.resolve()
            if resolved.parent == resolved:  # filesystem root ('/') guard
                continue
            return resolved
        return self.project_dir

    @property
    def project_skills_dir(self) -> Path:
        return self.project_dir / "skills"

    @property
    def project_claude_skills(self) -> Path:
        """In-place ``.claude/skills`` next to an in-place ``.omni`` project."""
        # project_dir is <repo>/.omni for in-place projects; the sibling
        # .claude/skills lives at <repo>/.claude/skills.
        return self.project_dir.parent / ".claude" / "skills"

    def ensure_dirs(self) -> None:
        """Create all directories needed for normal operation (idempotent)."""
        for d in (
            self.home,
            self.logs_dir,
            self.user_skills_dir,
            self.channels_dir,
            self.cache_dir,
            self.projects_dir,
            self.memories_dir,
            self.project_dir,
            self.artifacts_dir,
            self.project_skills_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def get_paths(project: str | None = None, cwd: Path | None = None) -> OmniPaths:
    """Resolve :class:`OmniPaths` for the active workspace.

    Resolution order for the project directory:

    1. explicit ``project`` name → ``<home>/projects/<project>``
    2. in-place ``.omni`` dir found walking up from ``cwd`` (excluding the home)
    3. the enclosing VCS root, keyed by absolute path →
       ``<home>/workspaces/<slug>-<hash8>``
    4. otherwise the resolved ``cwd``, keyed the same way

    Steps 3–4 mean every terminal launched in the same repo (or directory)
    shares one durable store, and ``~/.omni`` is never treated as a project.
    """
    home = user_home()
    # The directory the user actually launched from. Recorded on every branch so
    # local file/shell tools can operate on this folder regardless of how the
    # data store is keyed (named project, in-place, VCS root, or path-keyed).
    invocation_cwd = (cwd or Path.cwd()).resolve()
    if project:
        return OmniPaths(
            home=home, project_name=project, project_dir=home / "projects" / project,
            workspace_root=None, invocation_cwd=invocation_cwd,
        )

    root = find_project_root(cwd)
    if root is not None:
        return OmniPaths(
            home=home, project_name=root.name, project_dir=root / _PROJECT_MARKER,
            workspace_root=root, invocation_cwd=invocation_cwd,
        )

    base = find_vcs_root(cwd) or invocation_cwd
    if is_within_home(base, home):
        # Never key a workspace off the Omni home itself. This triggers when a
        # serve/REPL is launched from inside ``~/.omni/workspaces/<x>``: keying
        # off that *data* dir would spawn an endless chain of nested ghost
        # workspaces (``<x>-<hash>-<hash>…``), each polling the same IM bots.
        # Fall back to the shared named ``default`` project instead.
        return OmniPaths(
            home=home, project_name=_HOME_FALLBACK_PROJECT,
            project_dir=home / "projects" / _HOME_FALLBACK_PROJECT, workspace_root=None,
            invocation_cwd=invocation_cwd,
        )
    return OmniPaths(
        home=home, project_name=base.name or "workspace",
        project_dir=home / "workspaces" / workspace_key(base), workspace_root=base,
        invocation_cwd=invocation_cwd,
    )
