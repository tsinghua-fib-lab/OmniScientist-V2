"""Safe, ownership-aware OmniScientist uninstall planning and execution.

The uninstall path is deliberately split into a pure planner and an executor.
``omni uninstall --dry-run`` can therefore show every affected resource before
anything is stopped or deleted. Destructive data removal is opt-in, while MCP
entries and skill exports are removed surgically so unrelated agent settings
remain untouched.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from omni.config.paths import OmniPaths
from omni.config.workspaces import list_workspaces
from omni.data import BUILTIN_SKILLS_DIR
from omni.runtime.daemon import (
    daemon_info_from_pidfile,
    pid_alive,
    read_pidfile_path,
    scan_running_serve_pids,
)
from omni.skills_runtime.discovery import indexed_skill_dirs

DIST_NAME = "omniscientist"
INSTALL_MANIFEST = "install.json"
INSTALL_STATE_DIR_ENV = "OMNI_INSTALL_STATE_DIR"
UNINSTALL_PENDING = "uninstall.pending"
UNINSTALL_FAILED = "uninstall.failed"
_IGNORED_EXPORT_PARTS = {"__pycache__"}
_IGNORED_EXPORT_SUFFIXES = {".pyc", ".pyo"}
_STOP_TIMEOUT_SECONDS = 12.0
_SOURCE_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]+)@")
_SOURCE_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access[_-]?token|api[_-]?key|password|secret|token)=)[^&#\s]+"
)


@dataclass(frozen=True)
class InstallationRecord:
    """One package installation that can provide the ``omni`` command."""

    method: str
    executable: str
    python: str
    source: str = ""
    # Update "channel" recorded by the installer (master | pypi | local |
    # editable | pinned | <branch>). Advisory intent for `omni update`; the
    # updater still works from direct_url.json when this is absent.
    channel: str = ""
    editable: bool = False
    current: bool = False

    @property
    def identity(self) -> str:
        if self.method == "uv":
            return "uv-tool:omniscientist"
        if self.method == "pipx":
            return "pipx:omniscientist"
        return f"python:{Path(self.python).expanduser()}"


@dataclass(frozen=True)
class UninstallAction:
    """Human-readable operation in an uninstall plan."""

    category: str
    action: str
    target: str
    detail: str = ""
    irreversible: bool = False


@dataclass
class UninstallPlan:
    """Complete, serializable uninstall plan."""

    home: Path
    purge: bool
    all_project_data: bool
    all_installations: bool
    remove_program: bool
    remove_untracked_exports: bool
    actions: list[UninstallAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tracked_export_targets: list[Path] = field(default_factory=list)
    untracked_export_targets: list[Path] = field(default_factory=list)
    in_place_projects: list[Path] = field(default_factory=list)
    installations: list[InstallationRecord] = field(default_factory=list)
    preserved_installations: list[InstallationRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["home"] = str(self.home)
        for key in (
            "tracked_export_targets",
            "untracked_export_targets",
            "in_place_projects",
        ):
            payload[key] = [str(path) for path in getattr(self, key)]
        return payload


@dataclass
class UninstallReport:
    """Executor outcome suitable for CLI and tests."""

    completed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    program_removal_deferred: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def install_manifest_path(paths: OmniPaths) -> Path:
    return paths.home / INSTALL_MANIFEST


def install_operation_dir() -> Path:
    """Return installer lifecycle state kept outside purgeable Omni user data."""
    override = os.environ.get(INSTALL_STATE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "OmniScientist" / "state"
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "omni"


def _publish_pending_uninstall(operation_dir: Path) -> Path:
    operation_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending = operation_dir / UNINSTALL_PENDING
    temporary = operation_dir / f".{UNINSTALL_PENDING}.{os.getpid()}.tmp"
    payload = {
        "schema_version": 1,
        "operation": "uninstall",
        "status": "pending",
        "owner_pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(pending)
    try:
        (operation_dir / UNINSTALL_FAILED).unlink()
    except FileNotFoundError:
        pass
    return pending


def _sanitize_source(source: str) -> str:
    redacted = _SOURCE_USERINFO_RE.sub(r"\1***@", source.strip())
    return _SOURCE_QUERY_SECRET_RE.sub(r"\1***", redacted)


def _load_install_manifest(paths: OmniPaths) -> dict:
    path = install_manifest_path(paths)
    if not path.is_file():
        return {"schema_version": 1, "installations": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "installations": []}
    if not isinstance(data, dict):
        return {"schema_version": 1, "installations": []}
    data.setdefault("schema_version", 1)
    data.setdefault("installations", [])
    return data


def record_installation(
    paths: OmniPaths,
    *,
    method: str,
    source: str = "",
    editable: bool = False,
    channel: str = "",
) -> Path:
    """Record enough ownership metadata for a later exact uninstall."""
    executable = _current_entrypoint()
    record = InstallationRecord(
        method=(method or "env").strip().lower(),
        executable=str(Path(executable).expanduser()),
        python=str(Path(sys.executable).expanduser()),
        source=_sanitize_source(source),
        channel=(channel or "").strip().lower(),
        editable=editable,
        current=True,
    )
    data = _load_install_manifest(paths)
    rows = [
        row
        for row in data.get("installations", [])
        if isinstance(row, dict)
        and bool(str(row.get("executable") or ""))
        and Path(str(row.get("executable"))).expanduser().exists()
        and not (
            str(row.get("executable") or "") == record.executable
            or (
                str(row.get("method") or "") == record.method
                and str(row.get("python") or "") == record.python
            )
        )
    ]
    row = asdict(record)
    row.pop("current", None)
    row["recorded_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    rows.append(row)
    data["installations"] = rows
    path = install_manifest_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _editable_source() -> Path | None:
    try:
        dist = metadata.distribution(DIST_NAME)
        raw = dist.read_text("direct_url.json")
    except (metadata.PackageNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not (data.get("dir_info") or {}).get("editable"):
        return None
    url = str(data.get("url") or "")
    if not url.startswith("file://"):
        return None
    return Path(unquote(urlparse(url).path))


def installation_method_for_prefix(prefix: Path) -> str:
    """Classify the package owner from the active interpreter prefix."""
    # Receipt files keep custom UV_TOOL_DIR / PIPX_HOME layouts detectable even
    # when their path does not contain the conventional ``uv/tools`` or
    # ``pipx/venvs`` segments.
    if (prefix / "uv-receipt.toml").is_file():
        return "uv"
    if (prefix / "pipx_metadata.json").is_file():
        return "pipx"
    normalized = str(prefix).replace("\\", "/").lower()
    if "/uv/tools/omniscientist" in normalized:
        return "uv"
    if "/pipx/venvs/omniscientist" in normalized:
        return "pipx"
    return "env"


def _current_entrypoint() -> str:
    """Best-effort path to the command that launched this Python process."""
    invoked = Path(sys.argv[0]).expanduser()
    if invoked.name.lower() in {"omni", "omni.exe"} and invoked.exists():
        return str(invoked.resolve())
    scripts = Path(sysconfig.get_path("scripts"))
    owned = scripts / ("omni.exe" if sys.platform == "win32" else "omni")
    if owned.exists():
        return str(owned.resolve())
    return shutil.which("omni") or str(invoked)


def _entrypoint_python(path: Path) -> str:
    try:
        first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (OSError, IndexError):
        return ""
    if first.startswith("#!"):
        return first[2:].strip()
    return ""


def _looks_like_omni_entrypoint(path: Path) -> bool:
    if path.suffix.lower() == ".exe":
        return path.name.lower() == "omni.exe"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:8192]
    except OSError:
        return False
    return "omni.cli.main" in text


def omni_entrypoints_on_path() -> list[Path]:
    """Return verified Omni launchers in effective PATH order."""
    names = ("omni.exe", "omni") if sys.platform == "win32" else ("omni",)
    out: list[Path] = []
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir:
            continue
        for name in names:
            candidate = Path(raw_dir).expanduser() / name
            if (
                candidate.is_file()
                and candidate not in out
                and _looks_like_omni_entrypoint(candidate)
            ):
                out.append(candidate)
    return out


# Private aliases are retained inside this module so older tests/extensions
# that imported them do not diverge from the public ownership helpers.
_method_for_prefix = installation_method_for_prefix
_path_entrypoints = omni_entrypoints_on_path


def _record_from_entrypoint(path: Path, *, current: bool = False) -> InstallationRecord | None:
    if not _looks_like_omni_entrypoint(path):
        return None
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    method = _method_for_prefix(resolved.parent.parent)
    python = _entrypoint_python(resolved)
    if not python and current:
        python = sys.executable
    return InstallationRecord(
        method=method,
        executable=str(path),
        python=python,
        current=current,
    )


def detect_installations(paths: OmniPaths, *, all_installations: bool) -> list[InstallationRecord]:
    """Detect current and, optionally, every PATH/manifest Omni installation."""
    editable = _editable_source()
    current = InstallationRecord(
        method=_method_for_prefix(Path(sys.prefix)),
        executable=_current_entrypoint(),
        python=sys.executable,
        source=str(editable or ""),
        editable=editable is not None,
        current=True,
    )

    candidates = [current]
    if all_installations:
        for row in _load_install_manifest(paths).get("installations", []):
            if not isinstance(row, dict):
                continue
            candidates.append(
                InstallationRecord(
                    method=str(row.get("method") or "env"),
                    executable=str(row.get("executable") or ""),
                    python=str(row.get("python") or ""),
                    source=_sanitize_source(str(row.get("source") or "")),
                    channel=str(row.get("channel") or ""),
                    editable=bool(row.get("editable")),
                )
            )
        for entrypoint in _path_entrypoints():
            row = _record_from_entrypoint(entrypoint)
            if row is not None:
                candidates.append(row)

    unique: dict[str, InstallationRecord] = {}
    for row in candidates:
        prior = unique.get(row.identity)
        if prior is None or row.current:
            unique[row.identity] = row
    return sorted(unique.values(), key=lambda row: row.current)


def current_installation(paths: OmniPaths) -> InstallationRecord:
    """Return the installation that owns the currently running interpreter."""
    return next(
        row
        for row in detect_installations(paths, all_installations=False)
        if row.current
    )


def installation_command(record: InstallationRecord) -> list[str]:
    """Return the package-manager command that reverses one installation."""
    if record.method == "uv":
        return ["uv", "tool", "uninstall", DIST_NAME]
    if record.method == "pipx":
        return ["pipx", "uninstall", DIST_NAME]
    if record.python:
        uv = shutil.which("uv")
        if uv:
            return [uv, "pip", "uninstall", "--python", record.python, DIST_NAME]
        return [record.python, "-m", "pip", "uninstall", "-y", DIST_NAME]
    return []


def _skill_export_manifest(paths: OmniPaths) -> dict:
    path = paths.home / "skills_install.json"
    if not path.is_file():
        return {"owned": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"owned": []}
    return data if isinstance(data, dict) else {"owned": []}


def _tree_signature(root: Path) -> dict[str, str] | None:
    if not root.is_dir():
        return None
    try:
        files = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and not any(part in _IGNORED_EXPORT_PARTS for part in path.parts)
            and path.suffix not in _IGNORED_EXPORT_SUFFIXES
            and path.name != ".DS_Store"
        ]
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
        }
    except OSError:
        return None


def _export_inventory(paths: OmniPaths) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    manifest = _skill_export_manifest(paths)
    roots_by_target = {
        "claude": paths.claude_user_skills,
        "codex": paths.codex_user_skills,
        "agents": paths.agents_user_skills,
        "openclaw": paths.openclaw_user_skills,
    }
    tracked: list[Path] = []
    invalid: list[Path] = []
    for row in manifest.get("owned", []):
        if not isinstance(row, dict) or not str(row.get("dest") or ""):
            continue
        candidate = Path(str(row["dest"])).expanduser()
        root = roots_by_target.get(str(row.get("target") or ""))
        name = str(row.get("name") or "")
        try:
            safe = (
                root is not None
                and name
                and candidate.name == name
                and candidate.parent.resolve() == root.resolve()
            )
        except OSError:
            safe = False
        (tracked if safe else invalid).append(candidate)
    tracked_set = {str(path.resolve()) for path in tracked}
    roots = (
        paths.claude_user_skills,
        paths.codex_user_skills,
        paths.agents_user_skills,
        paths.openclaw_user_skills,
    )
    identical: list[Path] = []
    modified: list[Path] = []
    for source in indexed_skill_dirs(BUILTIN_SKILLS_DIR):
        source_signature = _tree_signature(source)
        if source_signature is None:
            continue
        for root in roots:
            target = root / source.name
            if not target.is_dir() or str(target.resolve()) in tracked_set:
                continue
            if _tree_signature(target) == source_signature:
                identical.append(target)
            else:
                modified.append(target)
    return tracked, identical, modified, invalid


def _completion_paths() -> list[Path]:
    home = Path.home()
    return [
        home / ".bash_completions" / "omni.sh",
        home / ".zfunc" / "_omni",
        home / ".config" / "fish" / "completions" / "omni.fish",
    ]


def _registered_in_place_projects(paths: OmniPaths) -> list[Path]:
    out: list[Path] = []
    for row in list_workspaces(paths.home):
        if str(row.get("kind") or "") != "in-place":
            continue
        candidate = Path(str(row.get("project_dir") or ""))
        if candidate.name == ".omni" and candidate not in out:
            out.append(candidate)
    if paths.project_dir.name == ".omni" and paths.project_dir not in out:
        out.append(paths.project_dir)
    return out


def _daemon_pidfiles(paths: OmniPaths, in_place_projects: list[Path]) -> list[Path]:
    out: list[Path] = []
    for root_name in ("projects", "workspaces"):
        root = paths.home / root_name
        if root.is_dir():
            out.extend(sorted(root.glob("*/serve.pid")))
    for candidate in (paths.home / "serve.pid", *(project / "serve.pid" for project in in_place_projects)):
        if candidate.is_file() and candidate not in out:
            out.append(candidate)
    return out


def build_uninstall_plan(
    paths: OmniPaths,
    *,
    purge: bool,
    all_project_data: bool,
    all_installations: bool,
    remove_program: bool,
    remove_untracked_exports: bool,
) -> UninstallPlan:
    """Build a deterministic plan without mutating disk, processes, or configs."""
    if all_project_data and not purge:
        raise ValueError("--all-project-data requires --purge")
    plan = UninstallPlan(
        home=paths.home,
        purge=purge,
        all_project_data=all_project_data,
        all_installations=all_installations,
        remove_program=remove_program,
        remove_untracked_exports=remove_untracked_exports,
    )
    in_place = _registered_in_place_projects(paths)
    pidfiles = _daemon_pidfiles(paths, in_place)
    plan.actions.append(
        UninstallAction(
            "service",
            "stop",
            "all Omni serve daemons",
            f"{len(pidfiles)} tracked/stale pidfiles found",
        )
    )

    tracked, identical, modified, invalid = _export_inventory(paths)
    plan.tracked_export_targets = tracked
    plan.untracked_export_targets = identical if remove_untracked_exports else []
    for target in tracked:
        plan.actions.append(UninstallAction("skill-export", "remove managed copy", str(target)))
    for target in plan.untracked_export_targets:
        plan.actions.append(
            UninstallAction(
                "skill-export",
                "remove identical untracked copy",
                str(target),
                "content exactly matches a current built-in skill",
            )
        )
    if identical and not remove_untracked_exports:
        plan.warnings.append(
            f"Found {len(identical)} untracked skill copies identical to built-ins; retained unless --everything is used."
        )
    if modified:
        plan.warnings.append(
            f"Found {len(modified)} same-named external skills with different content; they will not be deleted."
        )
    if invalid:
        plan.warnings.append(
            f"Ignored {len(invalid)} unsafe paths in skills_install.json; only direct children of known export roots can be removed."
        )

    from omni.compat.integrations import mcp_registration_status

    for target, registered in mcp_registration_status().items():
        if registered:
            plan.actions.append(
                UninstallAction("integration", "unregister MCP", target, "preserve unrelated MCP servers")
            )

    for completion in _completion_paths():
        if completion.is_file():
            plan.actions.append(UninstallAction("shell", "remove completion", str(completion)))

    if purge:
        if sys.platform == "darwin":
            plan.actions.append(
                UninstallAction(
                    "credentials",
                    "delete",
                    "macOS Keychain service=omniscientist",
                    "known Feishu, DingTalk, and WeChat accounts only",
                    irreversible=True,
                )
            )
        if all_project_data:
            plan.in_place_projects = in_place
            for project in in_place:
                plan.actions.append(
                    UninstallAction(
                        "project-data",
                        "delete",
                        str(project),
                        "registered in-place Omni workspace",
                        irreversible=True,
                    )
                )
        elif in_place:
            plan.warnings.append(
                f"Preserving {len(in_place)} registered in-place .omni directories; add --all-project-data to delete them."
            )
        if paths.home.exists():
            plan.actions.append(
                UninstallAction(
                    "user-data",
                    "delete",
                    str(paths.home),
                    "configuration, secrets, tasks, memory, logs, artifacts, and managed workspaces",
                    irreversible=True,
                )
            )
        plan.warnings.append("External WeChat gateways/containers are not owned by Omni and are not stopped or deleted.")
    else:
        plan.warnings.append(f"Preserving Omni configuration and research data under {paths.home}.")

    if remove_program:
        current_installations = detect_installations(paths, all_installations=False)
        if all_installations:
            plan.installations = detect_installations(paths, all_installations=True)
        else:
            plan.installations = current_installations
            selected = {installation.identity for installation in current_installations}
            plan.preserved_installations = [
                installation
                for installation in detect_installations(paths, all_installations=True)
                if installation.identity not in selected
            ]
        for installation in plan.installations:
            command = installation_command(installation)
            plan.actions.append(
                UninstallAction(
                    "program",
                    "uninstall",
                    installation.executable or installation.python,
                    " ".join(command) if command else "package manager could not be determined",
                )
            )
            if installation.editable and installation.source:
                plan.warnings.append(
                    f"Editable source checkout is preserved and must be removed separately: {installation.source}"
                )
        for installation in plan.preserved_installations:
            plan.actions.append(
                UninstallAction(
                    "program",
                    "preserve other install",
                    installation.executable or installation.python,
                    "use --all-installations to remove this installation too",
                )
            )
        if plan.preserved_installations:
            plan.warnings.append(
                f"Found {len(plan.preserved_installations)} additional Omni installation(s); "
                "they will remain available on PATH. Use --all-installations, or --everything "
                "for a full wipe."
            )
    else:
        plan.warnings.append("Program removal was disabled with --keep-program.")
    return plan


def _wait_pid_gone(pid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


def _stop_pid(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return not pid_alive(pid)
    if _wait_pid_gone(pid, _STOP_TIMEOUT_SECONDS):
        return True
    if sys.platform != "win32":
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return _wait_pid_gone(pid, 3.0)
    return not pid_alive(pid)


def _stop_all_daemons(paths: OmniPaths, in_place_projects: list[Path]) -> tuple[int, list[str]]:
    pidfiles = _daemon_pidfiles(paths, in_place_projects)
    live: dict[int, Path] = {}
    all_serve_pids = set(scan_running_serve_pids())
    for pidfile in pidfiles:
        info = daemon_info_from_pidfile(pidfile)
        if info is not None and int(info["pid"]) in all_serve_pids:
            live[int(info["pid"])] = pidfile
    from omni.runtime import service_state

    for pid in scan_running_serve_pids(
        service_id=service_state.service_instance_id(paths)
    ):
        live.setdefault(pid, Path())
    holder = service_state.singleton_holder_info(paths) or {}
    if holder.get("role") != "update":
        try:
            holder_pid = int(holder.get("pid", 0) or 0)
        except (TypeError, ValueError):
            holder_pid = 0
        if holder_pid > 0:
            live.setdefault(holder_pid, Path())

    stopped = 0
    errors: list[str] = []
    for pid, _pidfile in live.items():
        if _stop_pid(pid):
            stopped += 1
        else:
            errors.append(f"could not stop omni serve pid={pid}")

    for pidfile in pidfiles:
        data = read_pidfile_path(pidfile) or {}
        try:
            pid = int(data.get("pid", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0 or not pid_alive(pid):
            try:
                pidfile.unlink()
            except OSError:
                pass
    return stopped, errors


def _remove_completion_files() -> list[str]:
    removed: list[str] = []
    for path in _completion_paths():
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            if "_OMNI_COMPLETE" not in content:
                continue
            path.unlink()
            removed.append(str(path))
        except OSError:
            continue

    bashrc = Path.home() / ".bashrc"
    completion = Path.home() / ".bash_completions" / "omni.sh"
    if bashrc.is_file():
        try:
            original = bashrc.read_text(encoding="utf-8")
            lines = [line for line in original.splitlines() if str(completion) not in line]
            revised = "\n".join(lines).rstrip() + "\n"
            if revised != original:
                bashrc.write_text(revised, encoding="utf-8")
                removed.append(f"completion reference in {bashrc}")
        except OSError:
            pass
    return removed


def _safe_remove_home(home: Path) -> None:
    resolved = home.expanduser().resolve()
    user = Path.home().resolve()
    cwd = Path.cwd().resolve()
    if resolved in {Path(resolved.anchor), user, cwd} or resolved in cwd.parents:
        raise ValueError(f"refusing to purge unsafe OMNI_HOME: {resolved}")
    if (resolved / ".git").exists() or (resolved / "pyproject.toml").exists():
        raise ValueError(f"refusing to purge OMNI_HOME that looks like a source repository: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _safe_remove_in_place(path: Path) -> None:
    resolved = path.expanduser().resolve()
    if resolved.name != ".omni":
        raise ValueError(f"refusing to remove non-.omni project data: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _defer_windows_program_removal(
    commands: list[list[str]], operation_dir: Path
) -> bool:
    if not commands:
        return False
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return False
    script = Path(tempfile.gettempdir()) / f"omni-uninstall-{os.getpid()}.ps1"
    pending = operation_dir / UNINSTALL_PENDING
    failed = operation_dir / UNINSTALL_FAILED
    failed_tmp = operation_dir / f".{UNINSTALL_FAILED}.{os.getpid()}.tmp"
    lines = [
        f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue",
        "Start-Sleep -Milliseconds 250",
        "$status = 0",
    ]
    for command in commands:
        invocation = "& " + " ".join(_powershell_quote(part) for part in command)
        lines.extend(
            [
                "try {",
                f"  {invocation}",
                "  if ($LASTEXITCODE -ne 0) { $status = $LASTEXITCODE }",
                "} catch { $status = 1 }",
            ]
        )
    lines.extend(
        [
            "if ($status -eq 0) {",
            f"  Remove-Item -LiteralPath {_powershell_quote(str(failed))} -Force -ErrorAction SilentlyContinue",
            "} else {",
            "  @{ status = 'failed'; exit_code = $status } | ConvertTo-Json -Compress | "
            f"Set-Content -LiteralPath {_powershell_quote(str(failed_tmp))} -Encoding UTF8",
            f"  Move-Item -LiteralPath {_powershell_quote(str(failed_tmp))} -Destination {_powershell_quote(str(failed))} -Force",
            "}",
            f"Remove-Item -LiteralPath {_powershell_quote(str(pending))} -Force -ErrorAction SilentlyContinue",
            f"Remove-Item -LiteralPath {_powershell_quote(str(script))} -Force -ErrorAction SilentlyContinue",
            "exit $status",
        ]
    )
    try:
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _publish_pending_uninstall(operation_dir)
    except OSError:
        try:
            script.unlink()
        except OSError:
            pass
        return False
    detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    try:
        subprocess.Popen(  # noqa: S603 - generated arguments contain no shell interpolation.
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=detached | new_group,
        )
    except OSError:
        pending.unlink(missing_ok=True)
        script.unlink(missing_ok=True)
        return False
    return True


def _defer_posix_program_removal(
    commands: list[list[str]], operation_dir: Path
) -> bool:
    """Schedule package removal after the running CLI has finished rendering."""
    if not commands:
        return False
    shell = shutil.which("sh")
    if not shell:
        return False
    script = Path(tempfile.gettempdir()) / f"omni-uninstall-{os.getpid()}.sh"
    pending = operation_dir / UNINSTALL_PENDING
    failed = operation_dir / UNINSTALL_FAILED
    failed_tmp = operation_dir / f".{UNINSTALL_FAILED}.{os.getpid()}.tmp"
    lines = [
        "#!/bin/sh",
        f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.1; done",
        "sleep 0.1",
        "status=0",
    ]
    for command in commands:
        lines.append(" ".join(shlex.quote(part) for part in command) + " || status=$?")
    lines.extend(
        [
            'if [ "$status" -eq 0 ]; then',
            f"  rm -f {shlex.quote(str(failed))}",
            "else",
            f"  printf '{{\"status\":\"failed\",\"exit_code\":%s}}\\n' \"$status\" > {shlex.quote(str(failed_tmp))}",
            f"  mv -f {shlex.quote(str(failed_tmp))} {shlex.quote(str(failed))}",
            "fi",
            f"rm -f {shlex.quote(str(pending))}",
            f"rm -f {shlex.quote(str(script))}",
            'exit "$status"',
        ]
    )
    try:
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script.chmod(0o700)
        _publish_pending_uninstall(operation_dir)
        subprocess.Popen(  # noqa: S603 - generated arguments contain no shell interpolation.
            [shell, str(script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pending.unlink(missing_ok=True)
        try:
            script.unlink()
        except OSError:
            pass
        return False
    return True


def _defer_program_removal(commands: list[list[str]], operation_dir: Path) -> bool:
    if sys.platform == "win32":
        return _defer_windows_program_removal(commands, operation_dir)
    return _defer_posix_program_removal(commands, operation_dir)


def _remove_programs(
    installations: list[InstallationRecord],
    report: UninstallReport,
    operation_dir: Path | None = None,
) -> None:
    commands: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for installation in installations:
        command = installation_command(installation)
        if not command:
            report.errors.append(
                f"program manager could not be determined for {installation.executable or installation.python}"
            )
            continue
        key = tuple(command)
        if not command or key in seen:
            continue
        seen.add(key)
        commands.append(command)
    if not commands:
        return
    if _defer_program_removal(commands, operation_dir or install_operation_dir()):
        report.program_removal_deferred = True
        report.completed.append("scheduled program removal after this process exits")
    else:
        report.errors.append("program removal could not be scheduled after this process exits")


def _teardown_home_service(paths: OmniPaths, report: UninstallReport) -> None:
    """Stop and unregister the home background service and its OS supervisor unit.

    An uninstall must remove the launchd/systemd/schtasks unit too, or the OS
    would keep trying to relaunch a deleted CLI at every login. Best-effort: a
    failure is recorded but never aborts the rest of the uninstall.
    """
    try:
        from omni.runtime import service_state
        from omni.runtime.service_supervisors import SupervisorSpec, make_supervisor
    except Exception:  # noqa: BLE001 - service modules always import; guard defensively.
        return
    desired = service_state.read_desired(paths)
    if not (desired.configured or service_state.read_runtime(paths)):
        return
    desired.enabled = False
    desired.configured = True
    try:
        service_state.write_desired(paths, desired)
    except OSError:
        pass
    spec = SupervisorSpec(
        paths=paths,
        argv=desired.launcher or service_state.default_launcher(paths),
        workdir=paths.home,
        log_path=paths.logs_dir / "home-service.log",
    )
    try:
        supervisor = make_supervisor(spec, desired.manager)
        supervisor.stop()
        supervisor.uninstall()
        service_state.clear_runtime_if_owner(paths)
        report.completed.append("home service: stopped and OS supervisor unit removed")
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"could not tear down the home service: {exc}")


def execute_uninstall_plan(paths: OmniPaths, plan: UninstallPlan) -> UninstallReport:
    """Execute a confirmed plan in dependency-safe order."""
    report = UninstallReport()

    _teardown_home_service(paths, report)

    stopped, stop_errors = _stop_all_daemons(paths, _registered_in_place_projects(paths))
    report.completed.append(f"services: stopped {stopped} daemon(s) and removed stale pidfiles")
    report.errors.extend(stop_errors)

    from omni.skills_runtime.install import unexport_builtin_skills

    export_results = unexport_builtin_skills(paths) if plan.tracked_export_targets else []
    removed_exports = sum(result.status == "removed" for result in export_results)
    report.completed.append(f"skill exports: removed {removed_exports} managed copies")
    report.errors.extend(
        f"could not remove skill export {result.dest}: {result.status}"
        for result in export_results
        if result.status.startswith("error:")
    )
    for target in plan.untracked_export_targets:
        try:
            source = BUILTIN_SKILLS_DIR / target.name
            if target.is_dir() and _tree_signature(source) == _tree_signature(target):
                shutil.rmtree(target)
                report.completed.append(f"skill export: removed identical copy {target}")
            elif target.exists():
                report.skipped.append(f"skill export changed after planning; preserved {target}")
        except OSError as exc:
            report.errors.append(f"could not remove skill export {target}: {exc}")

    from omni.compat.integrations import unregister_with_claude, unregister_with_codex

    for name, operation in (
        ("codex", unregister_with_codex),
        ("claude", unregister_with_claude),
    ):
        try:
            _path, changed = operation()
        except OSError as exc:
            report.errors.append(f"could not unregister {name} MCP: {exc}")
        else:
            (report.completed if changed else report.skipped).append(
                f"MCP {name}: {'removed' if changed else 'not registered'}"
            )

    for item in _remove_completion_files():
        report.completed.append(f"shell completion: removed {item}")

    if plan.purge:
        from omni.channels.credentials import purge_known_channel_secrets

        try:
            removed_secrets = purge_known_channel_secrets()
            report.completed.append(f"credentials: removed {len(removed_secrets)} Keychain entries")
        except OSError as exc:
            report.errors.append(f"could not purge Keychain credentials: {exc}")

        for project in plan.in_place_projects:
            try:
                _safe_remove_in_place(project)
                report.completed.append(f"project data: removed {project}")
            except (OSError, ValueError) as exc:
                report.errors.append(str(exc))
        try:
            _safe_remove_home(plan.home)
            report.completed.append(f"user data: removed {plan.home}")
        except (OSError, ValueError) as exc:
            report.errors.append(str(exc))

    if plan.remove_program:
        _remove_programs(plan.installations, report)
    return report
