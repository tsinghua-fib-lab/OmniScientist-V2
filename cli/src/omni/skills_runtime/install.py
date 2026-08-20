"""Export the built-in skills into the system roots external tools read.

OmniScientist's built-in skills are authored in the shared, tri-tool-compatible
``SKILL.md`` format. ``omni skills export`` copies them from their project path
(``BUILTIN_SKILLS_DIR``) into the on-disk roots that Claude Code, Codex and
OpenClaw scan, so those tools can import and run them directly:

    claude   → ~/.claude/skills/<name>/
    codex    → ~/.codex/skills/<name>/      ($CODEX_HOME/skills)
    openclaw → ~/.openclaw/skills/<name>/
    agents   → ~/.agents/skills/<name>/     (shared root Codex/OpenClaw also read)

Users pick from three tools — ``claude`` / ``codex`` / ``openclaw`` — and
``codex`` / ``openclaw`` also write the shared ``~/.agents/skills`` root so the
skills are discovered regardless of tool version (see :data:`TOOL_ALIASES`).

Exports are idempotent and tracked in ``~/.omni/skills_install.json`` so
re-running updates OmniScientist-owned copies and ``unexport`` removes only
those (never a user's own same-named skill).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import tomli_w

from omni.config.paths import OmniPaths
from omni.config.settings import read_toml_file
from omni.data import BUILTIN_SKILLS_DIR
from omni.skills_runtime.discovery import indexed_skill_dirs, skill_dirs_in
from omni.skills_runtime.manifest import SkillEntry, parse_skill_path

# Logical target → resolver against OmniPaths.
TARGETS: dict[str, str] = {
    "claude": "claude_user_skills",
    "codex": "codex_user_skills",
    "agents": "agents_user_skills",
    "openclaw": "openclaw_user_skills",
}
ALL_TARGETS = tuple(TARGETS)

# The three tools users choose from when exporting (``agents`` is an internal
# shared root, not shown as a primary choice).
EXPORT_TOOLS: tuple[str, ...] = ("claude", "codex", "openclaw")

# A user-facing tool → the concrete roots it reads. Codex and OpenClaw also read
# the shared ``~/.agents/skills`` root, so exporting to either writes there too —
# this guarantees discovery no matter which location the installed tool version
# scans. ``claude`` maps 1:1 to its own root.
TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "codex": ("codex", "agents"),
    "openclaw": ("openclaw", "agents"),
}


def expand_targets(targets: list[str] | None) -> list[str]:
    """Expand user-facing tool names to concrete backend targets (roots).

    ``None`` → every export tool (one-click to all). ``codex`` / ``openclaw``
    additionally include the shared ``agents`` root. Order is preserved and
    duplicates removed; unknown names pass through (filtered by callers).
    """
    src = list(targets) if targets else list(EXPORT_TOOLS)
    out: list[str] = []
    for tool in src:
        for real in TOOL_ALIASES.get(tool, (tool,)):
            if real not in out:
                out.append(real)
    return out


@dataclass
class InstallResult:
    name: str
    target: str
    dest: Path
    status: str  # installed | updated | unchanged | skipped (exists) | error


@dataclass
class RemoveResult:
    name: str
    source: str
    path: Path
    status: str  # removed | disabled | absent | refused | error
    action: str  # physical_delete | config_disable | none
    message: str = ""
    tombstone: Path | None = None


@dataclass
class RestoreResult:
    name: str
    status: str  # restored | not_disabled | error
    message: str = ""
    tombstone: Path | None = None


@dataclass
class TrustResult:
    name: str
    status: str  # trusted | quarantined | absent | refused | error
    message: str = ""
    executable_files: tuple[str, ...] = ()


def target_root(target: str, paths: OmniPaths) -> Path | None:
    attr = TARGETS.get(target)
    return getattr(paths, attr) if attr else None


def _manifest_path(paths: OmniPaths) -> Path:
    return paths.home / "skills_install.json"


def _load_manifest(paths: OmniPaths) -> dict:
    p = _manifest_path(paths)
    if not p.is_file():
        return {"owned": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("owned", [])
        return data
    except (OSError, json.JSONDecodeError):
        return {"owned": []}


def _save_manifest(paths: OmniPaths, data: dict) -> None:
    paths.home.mkdir(parents=True, exist_ok=True)
    _manifest_path(paths).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def deleted_manifest_path(paths: OmniPaths) -> Path:
    return paths.home / "skills_deleted.json"


def _load_deleted_manifest(paths: OmniPaths) -> dict:
    path = deleted_manifest_path(paths)
    if not path.is_file():
        return {"deleted": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("deleted", [])
        return data
    except (OSError, json.JSONDecodeError):
        return {"deleted": []}


def _save_deleted_manifest(paths: OmniPaths, data: dict) -> None:
    paths.home.mkdir(parents=True, exist_ok=True)
    deleted_manifest_path(paths).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _record_deleted_skill(
    paths: OmniPaths,
    *,
    name: str,
    source: str,
    path: Path,
    action: str,
    sha256: str = "",
    reason: str = "user_removed",
) -> Path:
    manifest = _load_deleted_manifest(paths)
    rows = [row for row in manifest.get("deleted", []) if row.get("name") != name]
    rows.append(
        {
            "name": name,
            "source": source,
            "path": str(path),
            "sha256": sha256,
            "action": action,
            "reason": reason,
            "deleted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    manifest["deleted"] = rows
    _save_deleted_manifest(paths, manifest)
    return deleted_manifest_path(paths)


def deleted_skill_record(name: str, paths: OmniPaths) -> dict | None:
    for row in _load_deleted_manifest(paths).get("deleted", []):
        if row.get("name") == name:
            return row
    return None


def _disabled_names_from_value(raw_disabled: object) -> list[str]:
    if isinstance(raw_disabled, str):
        names = [item for item in raw_disabled.replace(",", " ").split() if item]
    elif isinstance(raw_disabled, list):
        names = [str(item) for item in raw_disabled if str(item)]
    else:
        names = []
    out: list[str] = []
    for item in names:
        if item not in out:
            out.append(item)
    return out


def disabled_skill_names(paths: OmniPaths) -> list[str]:
    data = read_toml_file(paths.config_file) if paths.config_file.is_file() else {}
    skills = data.get("skills", {})
    if not isinstance(skills, dict):
        return []
    return _disabled_names_from_value(skills.get("disabled", []))


def is_skill_disabled(name: str, paths: OmniPaths) -> bool:
    return name in disabled_skill_names(paths)


def disabled_skill_records(paths: OmniPaths) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for name in disabled_skill_names(paths):
        row = deleted_skill_record(name, paths) or {}
        records.append(
            {
                "name": name,
                "source": str(row.get("source") or "-"),
                "action": str(row.get("action") or "config_disable"),
                "path": str(row.get("path") or "-"),
            }
        )
    return records


def _write_disabled_names(paths: OmniPaths, disabled: list[str]) -> None:
    data = read_toml_file(paths.config_file) if paths.config_file.is_file() else {}
    skills = data.setdefault("skills", {})
    cleaned: list[str] = []
    for item in disabled:
        if item and item not in cleaned:
            cleaned.append(item)
    if cleaned:
        skills["disabled"] = cleaned
    else:
        skills.pop("disabled", None)
    paths.home.mkdir(parents=True, exist_ok=True)
    with paths.config_file.open("wb") as fh:
        tomli_w.dump(data, fh)


def _disable_skill_in_config(name: str, paths: OmniPaths) -> None:
    disabled = disabled_skill_names(paths)
    if name not in disabled:
        disabled.append(name)
    _write_disabled_names(paths, disabled)


def _clear_deleted_skill_record(name: str, paths: OmniPaths) -> Path | None:
    manifest = _load_deleted_manifest(paths)
    rows = [
        row for row in manifest.get("deleted", [])
        if not (row.get("name") == name and row.get("action") == "config_disable")
    ]
    if rows == manifest.get("deleted", []):
        return None
    manifest["deleted"] = rows
    _save_deleted_manifest(paths, manifest)
    return deleted_manifest_path(paths)


def restore_disabled_skill(name: str, paths: OmniPaths) -> RestoreResult:
    disabled = disabled_skill_names(paths)
    if name not in disabled:
        return RestoreResult(name, "not_disabled", "The skill is not disabled.")
    _write_disabled_names(paths, [item for item in disabled if item != name])
    tombstone = _clear_deleted_skill_record(name, paths)
    return RestoreResult(name, "restored", "Removed from skills.disabled.", tombstone)


def _delete_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _owns(manifest: dict, dest: Path) -> bool:
    return any(Path(e["dest"]) == dest for e in manifest.get("owned", []))


def _safe_export_destination(paths: OmniPaths, entry: dict, dest: Path) -> bool:
    """Validate that a manifest entry points at one direct, known export root.

    The ownership manifest is advisory user data, not authority to delete an
    arbitrary path. Both normal ``skills unexport`` and the global uninstaller
    use this guard before recursive removal.
    """
    target = str(entry.get("target") or "")
    name = str(entry.get("name") or "")
    root = target_root(target, paths)
    if root is None or not name or dest.name != name:
        return False
    try:
        return dest.parent.expanduser().resolve() == root.expanduser().resolve()
    except OSError:
        return False


def _record(manifest: dict, name: str, target: str, dest: Path) -> None:
    owned = [e for e in manifest.get("owned", []) if Path(e["dest"]) != dest]
    owned.append(
        {
            "name": name,
            "target": target,
            "dest": str(dest),
            "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    manifest["owned"] = owned


def _same_tree(source: Path, destination: Path) -> bool:
    """Return whether every source-owned skill file is identical at destination.

    Exported skills are standalone distributions, so identity includes portable
    runners, engines, licenses, and notices rather than only ``SKILL.md``. Extra
    destination files are ignored so harmless host caches do not force updates.
    """
    try:
        source_files = [
            path
            for path in source.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        ]
        return all(
            (destination / path.relative_to(source)).is_file()
            and path.read_bytes() == (destination / path.relative_to(source)).read_bytes()
            for path in source_files
        )
    except OSError:
        return False


def export_builtin_skills(
    paths: OmniPaths,
    targets: list[str] | None = None,
    *,
    force: bool = False,
    src_dir: Path | None = None,
) -> list[InstallResult]:
    """Copy each built-in skill into every requested tool's root(s)."""
    src = src_dir or BUILTIN_SKILLS_DIR
    chosen = [t for t in expand_targets(targets) if t in TARGETS]
    manifest = _load_manifest(paths)
    results: list[InstallResult] = []
    skill_dirs = indexed_skill_dirs(src) if src_dir is None else skill_dirs_in(src)

    for target in chosen:
        root = target_root(target, paths)
        if root is None:
            continue
        for skill_dir in skill_dirs:
            name = skill_dir.name
            dest = root / name
            try:
                if dest.exists():
                    if _same_tree(skill_dir, dest):
                        results.append(InstallResult(name, target, dest, "unchanged"))
                        _record(manifest, name, target, dest)
                        continue
                    if not (force or _owns(manifest, dest)):
                        results.append(InstallResult(name, target, dest, "skipped (exists)"))
                        continue
                    shutil.rmtree(dest)
                    status = "updated"
                else:
                    status = "installed"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(
                    skill_dir,
                    dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
                )
                _record(manifest, name, target, dest)
                results.append(InstallResult(name, target, dest, status))
            except OSError as exc:
                results.append(InstallResult(name, target, dest, f"error: {exc}"))

    _save_manifest(paths, manifest)
    return results


def exported_targets(paths: OmniPaths) -> list[str]:
    """Distinct targets that currently hold OmniScientist-owned exports."""
    manifest = _load_manifest(paths)
    seen: list[str] = []
    for entry in manifest.get("owned", []):
        target = str(entry.get("target") or "")
        if target and target not in seen:
            seen.append(target)
    return seen


def resync_exported_skills(paths: OmniPaths) -> list[InstallResult]:
    """Refresh previously-exported skills to the current bundled content.

    This is an explicit maintenance helper; ``omni update`` intentionally does
    not call it. Only OmniScientist-*owned* copies are touched (``force=False``
    keeps the manifest's ownership check), so a user's own same-named skill is
    never clobbered. No-op when nothing was exported.
    """
    targets = exported_targets(paths)
    if not targets:
        return []
    return export_builtin_skills(paths, targets, force=False)


# ── importing a skill INTO omni (the reverse of install/export) ────────────

# ``omni skills add <tool>:<name>`` source roots (other tools' user libraries).
IMPORT_SOURCES: dict[str, str] = {
    "claude": "claude_user_skills",
    "codex": "codex_user_skills",
    "agents": "agents_user_skills",
    "openclaw": "openclaw_user_skills",
}

_IMPORT_METADATA = ".omni-skill.json"
_SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EXECUTABLE_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".ps1", ".js", ".mjs", ".cjs", ".ts"}
_MAX_IMPORT_FILES = 500
_MAX_IMPORT_BYTES = 10 * 1024 * 1024


def _resolve_import_source(spec: str, paths: OmniPaths) -> tuple[Path | None, str]:
    """Resolve an ``omni skills add`` spec to ``(skill_path, default_name)``.

    ``spec`` may be a filesystem path (a skill dir with ``SKILL.md``, a
    ``SKILL.md`` file, or a single ``<name>.md``), or ``tool:name`` to pull a
    named skill from another tool's user library (claude/codex/agents/openclaw).
    """
    if ":" in spec and not Path(spec).expanduser().exists():
        tool, _, name = spec.partition(":")
        attr = IMPORT_SOURCES.get(tool.strip().lower())
        name = name.strip()
        if attr is None or not name:
            return None, ""
        root = getattr(paths, attr)
        as_dir = root / name
        if (as_dir / "SKILL.md").is_file():
            return as_dir, name
        as_file = root / f"{name}.md"
        if as_file.is_file():
            return as_file, name
        return None, name

    p = Path(spec).expanduser()
    if p.is_dir() and (p / "SKILL.md").is_file():
        return p, p.name
    if p.is_file() and p.name == "SKILL.md":
        return p, p.parent.name
    if p.is_file() and p.suffix.lower() == ".md":
        return p, p.stem
    return None, p.stem if p.name else ""


def _validate_import_source(src: Path) -> str:
    files = [src] if src.is_file() else list(src.rglob("*"))
    regular = [path for path in files if path.is_file()]
    if any(path.is_symlink() for path in files):
        return "source contains symbolic links"
    if len(regular) > _MAX_IMPORT_FILES:
        return f"source contains more than {_MAX_IMPORT_FILES} files"
    try:
        total = sum(path.stat().st_size for path in regular)
    except OSError as exc:
        return f"cannot inspect source: {exc}"
    if total > _MAX_IMPORT_BYTES:
        return f"source is larger than {_MAX_IMPORT_BYTES // (1024 * 1024)} MiB"
    return ""


def _write_import_metadata(dest: Path, *, source: str, commit: str = "") -> None:
    data = {
        "source": source,
        "commit": commit,
        "trusted": False,
        "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (dest / _IMPORT_METADATA).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _copy_into_omni(
    src: Path,
    paths: OmniPaths,
    final_name: str,
    force: bool,
    *,
    source: str,
    commit: str = "",
) -> InstallResult:
    """Copy one resolved skill (dir or ``*.md`` file) into ``~/.omni/skills``."""
    final_name = (final_name or "").strip()
    if not final_name or not _SAFE_SKILL_NAME.fullmatch(final_name):
        return InstallResult(final_name or str(src), "omni", paths.user_skills_dir, "error: cannot determine skill name")
    validation_error = _validate_import_source(src)
    if validation_error:
        return InstallResult(final_name, "omni", paths.user_skills_dir, f"error: {validation_error}")
    dest = paths.user_skills_dir / final_name
    try:
        if dest.exists():
            if not force:
                return InstallResult(final_name, "omni", dest, "skipped (exists)")
            shutil.rmtree(dest)
            status = "updated"
        else:
            status = "installed"
        if src.is_dir():
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
        else:  # a SKILL.md or <name>.md file → store as <name>/SKILL.md
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / "SKILL.md")
        _write_import_metadata(dest, source=source, commit=commit)
        return InstallResult(final_name, "omni", dest, status)
    except OSError as exc:
        return InstallResult(final_name, "omni", dest, f"error: {exc}")


def import_skill(
    spec: str, paths: OmniPaths, *, name: str | None = None, force: bool = False
) -> InstallResult:
    """Copy a skill INTO omni's managed user dir (``~/.omni/skills/<name>``).

    ``spec`` is a local path or ``tool:name`` (for git URLs use
    :func:`import_skill_from_git`). The imported skill then shows by default in
    ``omni skills list`` and is available to the agent (intent recognition /
    planning) — no ``--all`` needed.
    """
    src, default_name = _resolve_import_source(spec, paths)
    if src is None:
        return InstallResult(
            (name or default_name or spec), "omni", paths.user_skills_dir, "error: source not found"
        )
    canonical = _skill_name_of(src if src.is_dir() else src.parent, default_name)
    return _copy_into_omni(src, paths, name or canonical, force, source=spec)


def imported_skill_metadata(name: str, paths: OmniPaths) -> dict:
    marker = paths.user_skills_dir / name / _IMPORT_METADATA
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def set_imported_skill_trust(
    name: str,
    paths: OmniPaths,
    *,
    trusted: bool,
    allow_missing_license: bool = False,
) -> TrustResult:
    """Set owner trust after inspecting an imported skill and its provenance."""
    dest = paths.user_skills_dir / name
    if not dest.is_dir() or not (dest / "SKILL.md").is_file():
        return TrustResult(name, "absent", "imported skill not found")
    metadata = imported_skill_metadata(name, paths)
    if not metadata:
        metadata = {"source": "legacy user skill", "commit": "", "imported_at": ""}
    try:
        entry = parse_skill_path(dest, source="user_omni")
    except Exception as exc:  # noqa: BLE001
        return TrustResult(name, "error", f"invalid SKILL.md: {exc}")
    executable_files = tuple(
        str(path.relative_to(dest))
        for path in sorted(dest.rglob("*"))
        if path.is_file() and path.suffix.lower() in _EXECUTABLE_SUFFIXES
    )
    if trusted and not entry.license and not allow_missing_license:
        return TrustResult(
            name,
            "refused",
            "SKILL.md does not declare a license; add one or pass --force after verifying rights",
            executable_files,
        )
    metadata["trusted"] = trusted
    metadata["reviewed_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    (dest / _IMPORT_METADATA).write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return TrustResult(
        name,
        "trusted" if trusted else "quarantined",
        str(metadata.get("source") or ""),
        executable_files,
    )


# ── git URLs ───────────────────────────────────────────────────────────────

_GIT_HOSTS = ("github.com", "gitlab.com", "gitee.com", "bitbucket.org", "codeberg.org", "git.sr.ht")
_GIT_SCHEME_RE = re.compile(r"^(git\+|git@|ssh://|https?://)", re.IGNORECASE)


def looks_like_git_url(spec: str) -> bool:
    """Heuristic: is ``spec`` a git repo URL (vs. a local path or ``tool:name``)?"""
    s = spec.strip()
    if not s or Path(s).expanduser().exists():
        return False
    if s.startswith(("git+", "git@")) or s.endswith(".git"):
        return True
    if _GIT_SCHEME_RE.match(s) and any(h in s for h in _GIT_HOSTS):
        return True
    # bare ``host/org/repo`` for well-known hosts (we prepend https:// later)
    return any(s.startswith(h + "/") for h in _GIT_HOSTS)


def _skill_name_of(skill_dir: Path, fallback: str) -> str:
    """Canonical skill name from ``SKILL.md`` (so the dest folder matches it)."""
    try:
        name = parse_skill_path(skill_dir).name.strip()
        return name or fallback
    except Exception:  # noqa: BLE001
        return fallback


def _find_skill_dirs(base: Path) -> list[Path]:
    """Locate skill packages inside a cloned repo (repo-is-skill, flat, or skills/)."""
    if (base / "SKILL.md").is_file():
        return [base]
    found: list[Path] = []
    seen: set[Path] = set()
    for root in (base, base / "skills"):
        for d in skill_dirs_in(root):
            if d.resolve() not in seen:
                found.append(d)
                seen.add(d.resolve())
    return found


def import_skill_from_git(
    spec: str, paths: OmniPaths, *, name: str | None = None, force: bool = False, timeout: int = 120
) -> list[InstallResult]:
    """Clone a git repo and import the skill(s) it contains into ``~/.omni/skills``.

    Accepts ``https://…``, ``git@…``, ``git+…`` and bare ``host/org/repo`` forms,
    with an optional ``#subdir`` (or ``#path=<subdir>``) fragment to pick a
    sub-path. A repo may *be* a skill (root ``SKILL.md``), hold skill folders at
    the top level, or under a ``skills/`` directory — all are imported.
    """
    if shutil.which("git") is None:
        return [InstallResult(name or spec, "omni", paths.user_skills_dir,
                              "error: git not found (install git to add from a URL)")]
    url, _, frag = spec.partition("#")
    url = url.strip()
    if url.startswith("git+"):
        url = url[4:]
    if "://" not in url and not url.startswith("git@"):
        url = "https://" + url  # bare host/org/repo
    subdir = ""
    if frag:
        subdir = frag.split("=", 1)[1] if frag.startswith(("subdir=", "path=")) else frag
        subdir = subdir.strip().strip("/")

    tmp = Path(tempfile.mkdtemp(prefix="omni-skill-git-"))
    try:
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", url, str(tmp / "repo")],
                check=True, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return [InstallResult(name or url, "omni", paths.user_skills_dir, "error: git clone timed out")]
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip().splitlines()
            return [InstallResult(name or url, "omni", paths.user_skills_dir,
                                  f"error: git clone failed: {detail[-1] if detail else exc}")]
        repo = (tmp / "repo").resolve()
        base = (repo / subdir).resolve() if subdir else repo
        if not base.is_relative_to(repo):
            return [InstallResult(name or url, "omni", paths.user_skills_dir,
                                  "error: subdir escapes the cloned repository")]
        if not base.is_dir():
            return [InstallResult(name or url, "omni", paths.user_skills_dir,
                                  f"error: subdir '{subdir}' not found in repo")]
        skill_dirs = _find_skill_dirs(base)
        if not skill_dirs:
            return [InstallResult(name or url, "omni", paths.user_skills_dir,
                                  "error: no SKILL.md found in the repository")]
        single = len(skill_dirs) == 1
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return [
            _copy_into_omni(
                sd,
                paths,
                (name if single and name else _skill_name_of(sd, sd.name)),
                force,
                source=url,
                commit=commit,
            )
            for sd in skill_dirs
        ]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def remove_imported_skill(name: str, paths: OmniPaths) -> InstallResult:
    """Remove a skill previously imported into ``~/.omni/skills``."""
    dest = paths.user_skills_dir / name
    try:
        if dest.is_dir():
            shutil.rmtree(dest)
            return InstallResult(name, "omni", dest, "removed")
        return InstallResult(name, "omni", dest, "absent")
    except OSError as exc:
        return InstallResult(name, "omni", dest, f"error: {exc}")


def remove_loaded_skill(
    name: str,
    paths: OmniPaths,
    *,
    entry: SkillEntry | None = None,
    physical: bool = False,
    force: bool = False,
) -> RemoveResult:
    """Remove a loaded skill according to its ownership/source.

    User/project Omni skills are physically deleted by default. Built-in and
    external-library skills are disabled in Omni config by default so other
    tools and upgrade-managed files are not mutated accidentally. External
    physical deletion is allowed only with ``--physical --force``.
    """
    source = entry.source if entry is not None else "user_omni"
    path = entry.path if entry is not None and entry.path is not None else paths.user_skills_dir / name
    sha256 = entry.sha256 if entry is not None else ""

    try:
        if entry is None and not path.exists():
            return RemoveResult(name, source, path, "absent", "none", "No loaded or imported skill was found.")

        if source in {"user_omni", "project_omni"}:
            if not path.exists():
                return RemoveResult(name, source, path, "absent", "none", "The skill path does not exist; historical tasks are unchanged.")
            _delete_path(path)
            tombstone = _record_deleted_skill(
                paths, name=name, source=source, path=path, action="physical_delete", sha256=sha256
            )
            return RemoveResult(name, source, path, "removed", "physical_delete", "The skill was physically deleted; historical tasks are unchanged.", tombstone)

        if source == "builtin":
            _disable_skill_in_config(name, paths)
            tombstone = _record_deleted_skill(
                paths, name=name, source=source, path=path, action="config_disable", sha256=sha256
            )
            return RemoveResult(name, source, path, "disabled", "config_disable", "The built-in skill was disabled; bundled files were preserved.", tombstone)

        if physical:
            if not force:
                return RemoveResult(
                    name,
                    source,
                    path,
                    "refused",
                    "none",
                    "Deleting an external tool directory requires both --physical and --force.",
                )
            if not path.exists():
                return RemoveResult(name, source, path, "absent", "none", "The external skill path does not exist.")
            _delete_path(path)
            tombstone = _record_deleted_skill(
                paths, name=name, source=source, path=path, action="physical_delete", sha256=sha256
            )
            return RemoveResult(name, source, path, "removed", "physical_delete", "The external skill was physically deleted.", tombstone)

        _disable_skill_in_config(name, paths)
        tombstone = _record_deleted_skill(
            paths, name=name, source=source, path=path, action="config_disable", sha256=sha256
        )
        return RemoveResult(name, source, path, "disabled", "config_disable", "The external skill was disabled in Omni; external files were preserved.", tombstone)
    except OSError as exc:
        return RemoveResult(name, source, path, "error", "none", str(exc))


def unexport_builtin_skills(
    paths: OmniPaths, targets: list[str] | None = None
) -> list[InstallResult]:
    """Remove only OmniScientist-owned exported copies (per the manifest)."""
    chosen = set(expand_targets(targets))
    manifest = _load_manifest(paths)
    results: list[InstallResult] = []
    remaining: list[dict] = []

    for entry in manifest.get("owned", []):
        if entry.get("target") not in chosen:
            remaining.append(entry)
            continue
        dest = Path(entry["dest"])
        if not _safe_export_destination(paths, entry, dest):
            results.append(
                InstallResult(
                    str(entry.get("name") or "unknown"),
                    str(entry.get("target") or "unknown"),
                    dest,
                    "error: unsafe ownership-manifest destination",
                )
            )
            remaining.append(entry)
            continue
        try:
            if dest.is_dir():
                shutil.rmtree(dest)
                results.append(InstallResult(entry["name"], entry["target"], dest, "removed"))
            else:
                results.append(InstallResult(entry["name"], entry["target"], dest, "absent"))
        except OSError as exc:
            results.append(InstallResult(entry["name"], entry["target"], dest, f"error: {exc}"))
            remaining.append(entry)

    manifest["owned"] = remaining
    _save_manifest(paths, manifest)
    return results
