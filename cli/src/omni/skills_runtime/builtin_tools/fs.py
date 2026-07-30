"""Filesystem tools: read_file, write_file, edit_file, grep, glob, list_dir.

Reads are confined to the project dir + current working dir subtree; writes
are confined to the project dir + ``security.fs_write_allow`` roots. This is
a pragmatic guard for a single-user local tool, not a hard sandbox.

Two invariants make observations *actionable* (so the ReAct loop can course-
correct instead of dead-ending):

* Sensitive files (``.env`` / ``secrets.toml`` / private keys / ``~/.ssh`` …)
  are invisible — never read, never listed, never grepped. They are hidden by
  construction, not by the model's discretion.
* Every error tells the model what to do next (which roots are searchable, to
  use ``list_dir`` to explore, that a path is a directory, …), and reading a
  directory returns its listing rather than a bare failure.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from omni.channels.security import channel_requires_sensitive_confirm
from omni.core.react_agent import ToolSpec
from omni.skills_runtime.context import ExecContext, Tool

# Filenames that must never be exposed via any read/list/search tool. Matched
# case-insensitively against the file name (fnmatch). This is the deterministic
# half of the invariant that environment files and secrets are never exposed.
_SENSITIVE_GLOBS = (
    "secrets.toml",
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.pfx",
    "*.p12",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "*.credentials",
    "credentials.json",
    "*.secret",
    "*_secret",
    ".netrc",
    ".pgpass",
)
# Directory names that hold credentials wholesale.
_SENSITIVE_DIRS = {".ssh", ".gnupg", ".aws", ".gpg"}
# Noise dirs skipped during grep to keep it fast and relevant.
_SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".tox",
}


def _read_roots(ctx: ExecContext) -> list[Path]:
    roots = [ctx.paths.project_dir.resolve(), Path.cwd().resolve()]
    if ctx.working_dir is not None:
        roots.insert(0, ctx.working_dir.resolve())
    return list(dict.fromkeys(roots))


def _write_roots(ctx: ExecContext) -> list[Path]:
    roots = [ctx.paths.project_dir.resolve()]
    if ctx.working_dir is not None:
        roots.insert(0, ctx.working_dir.resolve())
    for extra in ctx.settings.security.fs_write_allow:
        roots.append(Path(extra).expanduser().resolve())
    return roots


def _within(path: Path, roots: list[Path]) -> bool:
    rp = path.resolve()
    for root in roots:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    if any(fnmatch.fnmatch(name, pat) for pat in _SENSITIVE_GLOBS):
        return True
    return bool({p.lower() for p in path.parts} & _SENSITIVE_DIRS)


def _is_sensitive_target(path: Path) -> bool:
    """Sensitivity on both the *named* path and its *resolved* target.

    Checking only ``path.name`` lets a benign-looking symlink (``notes.txt`` →
    ``.env`` / ``~/.ssh/id_rsa``) smuggle a sensitive file past the name-glob
    guard once ``_within`` has cleared the *resolved* location — a TOCTOU on the
    symbolic name. Re-checking the resolved target closes that bypass while
    leaving ordinary (non-symlink) paths unchanged.
    """
    if _is_sensitive(path):
        return True
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):  # broken symlink / resolution loop → treat as unsafe
        return True
    return resolved != path and _is_sensitive(resolved)


# Public re-admission helpers so other tools (e.g. open_artifact) can apply the
# same read-root + resolved-sensitivity gate to a concrete path they resolved.
def read_roots(ctx: ExecContext) -> list[Path]:
    return _read_roots(ctx)


def within_roots(path: Path, roots: list[Path]) -> bool:
    return _within(path, roots)


def is_sensitive_target(path: Path) -> bool:
    return _is_sensitive_target(path)


def _roots_hint(roots: list[Path]) -> str:
    return ", ".join(str(r) for r in roots)


def _root_error(op: str, path: Path, roots: list[Path]) -> str:
    return (
        f"ERROR: {op} denied because the path is outside the accessible roots: {path}. "
        f"Accessible roots: {_roots_hint(roots)}. Use list_dir to inspect them."
    )


def _format_listing(base: Path) -> str:
    entries: list[str] = []
    for child in sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if _is_sensitive(child):
            continue
        entries.append(f"{child.name}/" if child.is_dir() else child.name)
    body = "\n".join(entries[:500]) or "(empty directory)"
    return f"{base} (directory, {len(entries)} entries):\n{body}"


def build_fs_tools(ctx: ExecContext) -> list[Tool]:
    read_roots = _read_roots(ctx)
    default_base = ctx.working_dir or ctx.paths.project_dir

    async def read_file(args: dict) -> str:
        path = Path(str(args.get("path", ""))).expanduser()
        if not _within(path, read_roots):
            return _root_error("read", path, read_roots)
        if _is_sensitive_target(path):
            return f"ERROR: sensitive file hidden by security policy: {path.name}"
        if path.is_dir():
            # Reading a directory is not an error — return its contents so the
            # model can pick a file instead of dead-ending.
            return "The path is a directory. Use read_file on a listed file:\n" + _format_listing(path)
        if not path.is_file():
            return f"ERROR: path does not exist: {path}. Use list_dir to inspect a directory or glob to find files."
        text = path.read_text(encoding="utf-8", errors="replace")
        offset = int(args.get("offset", 0) or 0)
        limit = int(args.get("limit", 0) or 0)
        if offset or limit:
            lines = text.splitlines()
            end = offset + limit if limit else len(lines)
            text = "\n".join(lines[offset:end])
        return text[:200_000]

    async def list_dir(args: dict) -> str:
        path = Path(str(args.get("path", default_base))).expanduser()
        if not _within(path, read_roots):
            return _root_error("list", path, read_roots)
        if not path.exists():
            return f"ERROR: path does not exist: {path}. Accessible roots: {_roots_hint(read_roots)}."
        if path.is_file():
            return f"{path} is a file, not a directory. Use read_file."
        return _format_listing(path)

    async def write_file(args: dict) -> str:
        if channel_requires_sensitive_confirm(ctx.settings, ctx.channel):
            return (
                "ERROR: file writes from IM channels require local confirmation. "
                "Run the request from the CLI, or explicitly disable "
                f"require_sensitive_confirm for channel '{ctx.channel}'."
            )
        path = Path(str(args.get("path", ""))).expanduser()
        if not _within(path, _write_roots(ctx)):
            return f"ERROR: write denied outside allowed roots: {path}"
        if _is_sensitive_target(path):
            return f"ERROR: write to sensitive file denied by security policy: {path.name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args.get("contents", "")), encoding="utf-8")
        return f"OK: wrote {len(str(args.get('contents', '')))} chars to {path}"

    async def edit_file(args: dict) -> str:
        if channel_requires_sensitive_confirm(ctx.settings, ctx.channel):
            return (
                "ERROR: file edits from IM channels require local confirmation. "
                "Run the request from the CLI, or explicitly disable "
                f"require_sensitive_confirm for channel '{ctx.channel}'."
            )
        path = Path(str(args.get("path", ""))).expanduser()
        if not _within(path, _write_roots(ctx)):
            return f"ERROR: edit denied outside allowed roots: {path}"
        if _is_sensitive_target(path):
            return f"ERROR: edit of sensitive file denied by security policy: {path.name}"
        if not path.is_file():
            return f"ERROR: not a file: {path}"
        old = str(args.get("old_string", ""))
        new = str(args.get("new_string", ""))
        text = path.read_text(encoding="utf-8")
        if old and text.count(old) != 1:
            return f"ERROR: old_string must occur exactly once (found {text.count(old)})"
        path.write_text(text.replace(old, new, 1) if old else new, encoding="utf-8")
        return f"OK: edited {path}"

    async def grep(args: dict) -> str:
        pattern = str(args.get("pattern", ""))
        base = Path(str(args.get("path", default_base))).expanduser()
        if not _within(base, read_roots):
            return _root_error("search", base, read_roots)
        hits: list[str] = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for f in files:
                fp = Path(root) / f
                if _is_sensitive(fp):
                    continue
                try:
                    for i, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if pattern in line:
                            hits.append(f"{fp}:{i}: {line.strip()[:200]}")
                            if len(hits) >= 200:
                                return "\n".join(hits)
                except OSError:
                    continue
        return "\n".join(hits) or f"(no matches for '{pattern}' under {base})"

    async def glob_tool(args: dict) -> str:
        pattern = str(args.get("pattern", "*"))
        base = ctx.working_dir or ctx.paths.project_dir
        out = [
            str(p)
            for p in base.rglob("*")
            if fnmatch.fnmatch(p.name, pattern) and not _is_sensitive(p)
        ][:500]
        return "\n".join(out) or f"(no matches for '{pattern}' under {base})"

    return [
        Tool(
            ToolSpec("read_file", f"Read a local file under the project/current-directory roots (default {default_base}); sensitive files are hidden. Directories return a listing.", {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            }),
            read_file,
        ),
        Tool(
            ToolSpec("list_dir", f"List a directory under the project/current-directory roots (default {default_base}).", {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            }),
            list_dir,
        ),
        Tool(
            ToolSpec("write_file", "Write a file under an allowed root.", {
                "type": "object",
                "properties": {"path": {"type": "string"}, "contents": {"type": "string"}},
                "required": ["path", "contents"],
            }),
            write_file,
        ),
        Tool(
            ToolSpec("edit_file", "Replace one exact text segment in a file.", {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            }),
            edit_file,
        ),
        Tool(
            ToolSpec("grep", f"Search file contents by substring under an accessible root (default {default_base}); skips sensitive and noisy directories.", {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                "required": ["pattern"],
            }),
            grep,
        ),
        Tool(
            ToolSpec("glob", "Find files in the project by wildcard pattern.", {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            }),
            glob_tool,
        ),
    ]
