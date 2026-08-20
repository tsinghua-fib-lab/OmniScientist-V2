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

Reads are also *format aware*: a PDF is extracted as markdown and a binary file
is described rather than decoded. This is what makes an ``@paper.pdf`` mention
genuinely work instead of handing the model replacement-character noise.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from omni.agent.capabilities import contract_write_target
from omni.config.paths import OmniPaths
from omni.core.file_mentions import strip_mention_marker
from omni.core.path_lookup import (
    missing_path_message,
    path_exists,
    path_is_dir,
    path_is_file,
    resolve_existing_path,
)
from omni.core.react_agent import ToolSpec
from omni.core.sensitive_paths import is_sensitive_path, is_write_protected_path
from omni.core.sensitive_paths import is_sensitive_target as _shared_is_sensitive_target
from omni.skills_runtime.context import ExecContext, Tool

logger = logging.getLogger(__name__)

# Which paths are invisible lives in ``omni.core.sensitive_paths`` because the
# ``@`` mention picker applies the same policy: a pattern added in one place must
# not stay suggestible in the other.
_is_sensitive = is_sensitive_path
_is_sensitive_target = _shared_is_sensitive_target

# Noise dirs skipped during grep to keep it fast and relevant.
_SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".tox",
}


def _read_roots(ctx: ExecContext) -> list[Path]:
    from omni.core.observation import observation_spill_roots
    from omni.skills_runtime.exec_io import extra_exec_roots

    roots = [ctx.paths.project_dir.resolve(), Path.cwd().resolve()]
    if ctx.working_dir is not None:
        roots.insert(0, ctx.working_dir.resolve())
    skill_root = getattr(ctx, "skill_root", None)
    if skill_root is not None:
        roots.append(Path(skill_root).resolve())
    roots.extend(extra_exec_roots(ctx))
    roots.extend(observation_spill_roots(ctx.paths))
    return list(dict.fromkeys(roots))


def write_roots_for(
    project_dir: Path,
    working_dir: Path | None,
    extra_allow: Sequence[str] = (),
    managed_output_roots: Sequence[Path] = (),
) -> list[Path]:
    """The directories a turn may write into.

    Shared with the approval gate, which decides by destination whether a write
    needs the owner's confirmation. Both must read the same envelope: a boundary
    the tool enforces but the gate computes differently would either prompt for
    writes that are about to be refused, or wave through writes it never saw.
    """
    roots = [project_dir.resolve()]
    if working_dir is not None:
        roots.insert(0, working_dir.resolve())
    for extra in extra_allow:
        roots.append(Path(extra).expanduser().resolve())
    roots.extend(Path(root).resolve() for root in managed_output_roots)
    return list(dict.fromkeys(roots))


def output_roots_for(paths: OmniPaths, artifacts: object | None = None) -> list[Path]:
    """The directories omni generates into, carved out of the state guard.

    An installed omni keeps its workspace under ``~/.omni``, so the artifacts
    directory sits inside the tree ``is_write_protected_path`` refuses. Naming it
    here is what lets a generated document reach the place the project keeps
    generated documents, while the stores beside it stay unwritable.
    """
    roots = [paths.artifacts_dir.resolve()]
    for root in getattr(artifacts, "managed_output_roots", ()) or ():
        roots.append(Path(root).resolve())
    return list(dict.fromkeys(roots))


def _output_roots(ctx: ExecContext) -> list[Path]:
    return output_roots_for(ctx.paths, ctx.artifacts)


def _write_roots(ctx: ExecContext) -> list[Path]:
    from omni.skills_runtime.exec_io import extra_exec_roots

    return write_roots_for(
        ctx.paths.project_dir,
        ctx.working_dir,
        ctx.settings.security.fs_write_allow,
        (
            *tuple(getattr(ctx.artifacts, "managed_output_roots", ()) or ()),
            *extra_exec_roots(ctx),
        ),
    )


# Suffix → (artifact kind, mime). Anything else is registered as a plain file:
# the point is that the turn produced it, not that we classified it well.
_DOCUMENT_TYPES: dict[str, tuple[str, str]] = {
    ".md": ("document", "text/markdown"),
    ".markdown": ("document", "text/markdown"),
    ".txt": ("document", "text/plain"),
    ".tex": ("document", "application/x-tex"),
    ".html": ("document", "text/html"),
    ".csv": ("data", "text/csv"),
    ".json": ("data", "application/json"),
    ".svg": ("figure", "image/svg+xml"),
    ".png": ("figure", "image/png"),
}


def document_kind_for(path: Path) -> tuple[str, str]:
    """Return ``(kind, mime)`` for a harvested deliverable path."""
    return _DOCUMENT_TYPES.get(path.suffix.lower(), ("file", "application/octet-stream"))


async def register_written_file(ctx: ExecContext, path: Path) -> None:
    """Record a file this turn wrote so the turn can show what it produced.

    A skill's output is registered and therefore listed; a file the model wrote
    itself was not, so the paper that answered the request was absent from the
    result list while the figure beside it appeared. Codex closes the same gap
    from the tool side — ``apply_patch`` feeds the turn's diff tracker, which is
    what the UI later reports. ``bash`` / ``run_compute`` harvest
    ``$OMNI_OUTPUT_DIR`` through this same function so verification sees one
    inventory. Registration is best-effort: it describes work that already
    succeeded and must never turn a completed write into an error.
    """
    store = getattr(ctx, "artifacts", None)
    if store is None or not getattr(ctx, "task_id", ""):
        return
    kind, mime = document_kind_for(path)
    try:
        await store.register_existing(
            path,
            kind=kind,
            title=path.stem,
            mime=mime,
            session_id=getattr(ctx, "session_id", "") or "",
            task_id=ctx.task_id,
        )
    except Exception:  # noqa: BLE001 - inventory is not worth failing a good write over
        logger.debug("artifact.register_failed path=%s", path, exc_info=True)


async def _register_written(ctx: ExecContext, path: Path) -> None:
    await register_written_file(ctx, path)


async def resolve_write_target(ctx: ExecContext, raw: str) -> Path:
    """Where a requested write actually lands.

    A bare filename is the model naming a deliverable, not pointing at a file, so
    it belongs in managed output rather than wherever the process happens to be
    running. Dropping a paper into the source root leaves it untracked, since the
    ignore rules cover the deliverable directories but not stray files beside
    them, and a path recorded outside them is stored absolute, so the artifact
    record does not survive the tree being moved.

    A ledger token (``draft.section``, ``artifact.figure``, …) is never a
    filename. Those names stay on the settlement contract; the write is rewritten
    to a human stem in the matching task collection. That rewrite wins over a
    same-named stray in the working directory so one pre-fix ``draft.section``
    cannot keep capturing later turns.

    Anything carrying a directory component is the model being explicit and is
    honoured as given. For a bare name, an existing file wins over a new location
    so an append or a rewrite continues the document it is extending:

    * already in this task's output bundle — the deliverable we are still writing;
    * already in the workspace ``artifacts/`` dir — a document we generated before
      the task-scoped layout, or one written without a task;
    * only in the working directory — a real repository file (``README.md``,
      ``AGENTS.md``), and naming it still means it;
    * nowhere yet — new, so it opens the task's output bundle, falling back to
      ``artifacts/`` when the turn carries no task.

    Checking the working directory before the managed locations is what let one
    stray file entrench itself: a pre-fix run left a paper in the source root,
    every later write of that name found it there, and the deliverable could never
    reach managed output. Worse, a chunked write could tear in half — the first
    chunk creating the file under artifacts and a same-named repo file capturing
    the appends.

    Every branch lands inside the write roots, which is what lets the approval
    gate treat a bare filename as in-workspace without repeating this lookup.
    """
    candidate = Path(raw).expanduser()
    if candidate.parent != Path("."):
        return candidate
    rewritten = await _rewrite_contract_write_target(ctx, candidate.name)
    if rewritten is not None:
        return rewritten
    kind, _mime = _DOCUMENT_TYPES.get(
        candidate.suffix.lower(), ("file", "application/octet-stream")
    )
    store = getattr(ctx, "artifacts", None)
    if store is not None and getattr(ctx, "task_id", ""):
        existing_output = await store.existing_task_output_path(
            candidate.name, kind=kind
        )
        if existing_output is not None and existing_output.exists():
            return existing_output
    generated = ctx.paths.artifacts_dir / candidate.name
    if generated.exists():
        return generated
    if ctx.working_dir is not None and (ctx.working_dir / candidate.name).exists():
        return ctx.working_dir / candidate.name
    if store is not None and getattr(ctx, "task_id", ""):
        return await store.task_output_path(candidate.name, kind=kind)
    return generated


async def _rewrite_contract_write_target(ctx: ExecContext, basename: str) -> Path | None:
    """Map a ledger token onto a human file in the task collection."""
    spec = contract_write_target(basename)
    if spec is None:
        return None
    kind, suffix = spec
    filename = f"{await _task_write_stem(ctx)}{suffix}"
    store = getattr(ctx, "artifacts", None)
    if store is not None and getattr(ctx, "task_id", ""):
        existing = await store.existing_task_output_path(filename, kind=kind)
        if existing is not None and existing.exists():
            return existing
        return await store.task_output_path(filename, kind=kind)
    return ctx.paths.artifacts_dir / filename


async def _task_write_stem(ctx: ExecContext) -> str:
    from omni.storage.artifacts import slugify_filename

    store = getattr(ctx, "artifacts", None)
    getter = getattr(store, "task_label", None)
    title = ""
    if callable(getter):
        try:
            title = str(await getter() or "")
        except Exception:  # noqa: BLE001 - a missing title must not block the write
            title = ""
    return slugify_filename(title) or "draft"


def _granted_paths(ctx: ExecContext) -> set[Path]:
    """Files the user explicitly attached this turn (``@`` mentions).

    The read roots exist to stop the *model* from wandering the filesystem; they
    are not a veto over the owner. When the user writes ``@/abs/path/paper.md``
    that is consent to read that exact file, so it is admitted even from outside
    the roots — otherwise the feature silently fails whenever omni is launched
    from a different directory than the document.

    Consent widens *which paths*, never *which kinds*: sensitivity is still
    enforced by the caller, so ``@~/.ssh/id_rsa`` stays refused.
    """
    granted: set[Path] = set()
    for uri in getattr(ctx, "file_uris", None) or []:
        raw = str(uri or "").strip()
        if not raw or raw.startswith("artifact://"):
            continue
        raw = raw.removeprefix("file://")
        try:
            granted.add(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    return granted


def _within(path: Path, roots: list[Path]) -> bool:
    try:
        rp = path.resolve()
    except (OSError, RuntimeError):
        # Windows rejects ASCII ``"`` in a filename. Admit by the resolvable
        # parent so a quote-rewritten miss still gets a missing-path error
        # instead of "outside roots".
        try:
            rp = path.parent.resolve()
        except (OSError, RuntimeError):
            return False
    for root in roots:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


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


def _looks_like_host_tmp(path: Path) -> bool:
    text = str(path).replace("\\", "/").lower()
    return (
        text.startswith("/tmp/")
        or text.startswith("/private/tmp/")
        or text.startswith("/var/tmp/")
        or text.startswith("/private/var/tmp/")
    )


def _root_error(op: str, path: Path, roots: list[Path], *, ctx: ExecContext | None = None) -> str:
    message = (
        f"ERROR: {op} denied because the path is outside the accessible roots: {path}. "
        f"Accessible roots: {_roots_hint(roots)}. Use list_dir to inspect them."
    )
    if ctx is not None and _looks_like_host_tmp(path):
        from omni.skills_runtime.exec_io import OMNI_OUTPUT_ENV, durable_output_dir

        message += (
            f" Host /tmp is not a deliverable path. Write CSV/JSON/PNG/SVG to "
            f"${OMNI_OUTPUT_ENV} ({durable_output_dir(ctx)}) so read_file and the "
            "artifact store can see them."
        )
    return message


# Cap on one read's payload. Reached only by very large documents, and never
# silently: the observation says how to continue so the model can page through.
_MAX_READ_CHARS = 200_000
# Pages extracted from a PDF in one read. A whole thesis would otherwise turn one
# tool call into a context-flooding dump.
_MAX_PDF_PAGES = 50


def _looks_binary(probe: bytes) -> bool:
    """NUL byte or a high share of control bytes in the first chunk."""
    if b"\x00" in probe:
        return True
    if not probe:
        return False
    control = sum(1 for byte in probe if byte < 9 or 13 < byte < 32)
    return control / len(probe) > 0.3


def _pdf_markdown(path: Path) -> str:
    """Extract a PDF as markdown, bounded by :data:`_MAX_PDF_PAGES`.

    ``read_text`` on a PDF yields replacement-character noise: it costs tokens
    and teaches the model nothing, so a research agent that cannot read a paper
    only *appears* to support ``@paper.pdf``. PyMuPDF ships as a product
    dependency, so this normalisation is always available.
    """
    try:
        import pymupdf
        import pymupdf4llm
    except ImportError:  # pragma: no cover - both ship as product dependencies
        return "(PDF text extraction unavailable: pymupdf/pymupdf4llm not installed.)"
    try:
        with pymupdf.open(path) as document:
            total = document.page_count
        pages = list(range(min(total, _MAX_PDF_PAGES)))
        text = str(pymupdf4llm.to_markdown(str(path), pages=pages))
    except Exception as exc:  # noqa: BLE001 - malformed/encrypted PDFs vary widely
        return f"(could not extract text from this PDF: {exc})"
    if total > _MAX_PDF_PAGES:
        text += (
            f"\n\n[extracted the first {_MAX_PDF_PAGES} of {total} pages; "
            "ask for a specific page range to continue]"
        )
    return text


def _document_text(path: Path) -> str:
    """File contents as text, normalising formats ``read_text`` cannot handle."""
    if path.suffix.lower() == ".pdf":
        return _pdf_markdown(path)
    try:
        with path.open("rb") as handle:
            probe = handle.read(4096)
    except OSError as exc:
        return f"ERROR: could not open {path}: {exc}"
    if _looks_binary(probe):
        size = path.stat().st_size
        return (
            f"({path.name} is a binary file, {size} bytes, so it has no text to "
            "read. Describe it by name/size, or use a skill that handles this format.)"
        )
    return path.read_text(encoding="utf-8", errors="replace")


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
    # Explicit ``@`` attachments for this turn. A mentioned *file* is admitted on
    # its own; a mentioned *directory* becomes a root, because "review @corpus"
    # is meaningless if the files inside it stay unreadable.
    granted = _granted_paths(ctx)
    admitted_roots = read_roots + [path for path in granted if path.is_dir()]

    def admitted(path: Path) -> bool:
        if _within(path, admitted_roots):
            return True
        try:
            return path.resolve() in granted
        except (OSError, RuntimeError):
            return False

    async def read_file(args: dict) -> str:
        raw = strip_mention_marker(str(args.get("path", "")))
        if raw.startswith("artifact://"):
            store = getattr(ctx, "artifacts", None)
            path = await store.resolve_path(raw) if store is not None else None
            if path is None:
                return (
                    f"ERROR: artifact not found: {raw}. "
                    "Use open_artifact or the exact local path from the previous tool result."
                )
            from_store = True
        else:
            path = resolve_existing_path(raw) or Path(raw).expanduser()
            from_store = False
        if not from_store and not admitted(path):
            return _root_error("read", path, read_roots, ctx=ctx)
        if _is_sensitive_target(path):
            return f"ERROR: sensitive file hidden by security policy: {path.name}"
        if path_is_dir(path):
            # Reading a directory is not an error — return its contents so the
            # model can pick a file instead of dead-ending.
            return "The path is a directory. Use read_file on a listed file:\n" + _format_listing(path)
        if not path_is_file(path):
            return missing_path_message(
                path,
                next_step="Use list_dir to inspect a directory or glob to find files.",
            )
        text = _document_text(path)
        offset = int(args.get("offset", 0) or 0)
        limit = int(args.get("limit", 0) or 0)
        if offset or limit:
            lines = text.splitlines()
            end = offset + limit if limit else len(lines)
            text = "\n".join(lines[offset:end])
        if len(text) <= _MAX_READ_CHARS:
            return text
        # Say that the payload was cut and how to continue: a silent truncation
        # looks to the model like the file simply ends there.
        return (
            text[:_MAX_READ_CHARS]
            + f"\n\n[truncated at {_MAX_READ_CHARS} characters; call read_file again "
            "with offset/limit to continue]"
        )

    async def list_dir(args: dict) -> str:
        raw = strip_mention_marker(str(args.get("path", default_base)))
        path = resolve_existing_path(raw) or Path(raw).expanduser()
        if not admitted(path):
            return _root_error("list", path, read_roots, ctx=ctx)
        if not path_exists(path):
            return missing_path_message(
                path, next_step=f"Accessible roots: {_roots_hint(read_roots)}."
            )
        if path_is_file(path):
            return f"{path} is a file, not a directory. Use read_file."
        return _format_listing(path)

    async def write_file(args: dict) -> str:
        raw = str(args.get("path", "")).strip()
        if not raw:
            return "ERROR: write needs a 'path'."
        path = await resolve_write_target(ctx, raw)
        if not path_exists(path):
            existing = resolve_existing_path(path)
            if existing is not None:
                path = existing
        if not _within(path, _write_roots(ctx)):
            return f"ERROR: write denied outside allowed roots: {path}"
        if is_write_protected_path(path, _output_roots(ctx)):
            return f"ERROR: write denied in a protected directory: {path}"
        if _is_sensitive_target(path):
            return f"ERROR: write to sensitive file denied by security policy: {path.name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        contents = str(args.get("contents", ""))
        # Appending is the only way to write a document larger than one response
        # can carry. Without it a long file must arrive as a single call, and a
        # call bigger than the output cap is cut off mid-argument and lost.
        appending = bool(args.get("append", False)) and path_exists(path)
        # ``newline=""`` writes the model's bytes through untranslated. Left to
        # the platform default, Windows expands every "\n" to "\r\n", so the same
        # deliverable has a different size and content hash there than on the
        # machine that recorded it — and a document assembled from appended
        # chunks no longer matches the length its own tool reported.
        with path.open(
            "a" if appending else "w", encoding="utf-8", newline=""
        ) as handle:
            handle.write(contents)
        await _register_written(ctx, path)
        verb = "appended" if appending else "wrote"
        total = path.stat().st_size if appending else len(contents)
        suffix = f" (file now {total} bytes)" if appending else ""
        return f"OK: {verb} {len(contents)} chars to {path}{suffix}"

    async def edit_file(args: dict) -> str:
        raw = str(args.get("path", "")).strip()
        if not raw:
            return "ERROR: edit needs a 'path'."
        path = await resolve_write_target(ctx, raw)
        if not path_exists(path):
            existing = resolve_existing_path(path)
            if existing is not None:
                path = existing
        if not _within(path, _write_roots(ctx)):
            return f"ERROR: edit denied outside allowed roots: {path}"
        if is_write_protected_path(path, _output_roots(ctx)):
            return f"ERROR: edit denied in a protected directory: {path}"
        if _is_sensitive_target(path):
            return f"ERROR: edit of sensitive file denied by security policy: {path.name}"
        if not path_is_file(path):
            return f"ERROR: not a file: {path}"
        old = str(args.get("old_string", ""))
        new = str(args.get("new_string", ""))
        text = path.read_text(encoding="utf-8")
        if old and text.count(old) != 1:
            return f"ERROR: old_string must occur exactly once (found {text.count(old)})"
        path.write_text(
            text.replace(old, new, 1) if old else new, encoding="utf-8", newline=""
        )
        await _register_written(ctx, path)
        return f"OK: edited {path}"

    async def grep(args: dict) -> str:
        pattern = str(args.get("pattern", ""))
        base = Path(str(args.get("path", default_base))).expanduser()
        if not _within(base, read_roots):
            return _root_error("search", base, read_roots, ctx=ctx)
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

    from omni.core.tool_result import fs_result_outcome

    tools = [
        Tool(
            ToolSpec("read_file", f"Read a local file under the project/current-directory roots (default {default_base}) or any file the user attached with @; artifact:// URIs resolve through the artifact store. Use the exact path or URI from a previous tool result — do not rewrite quotation marks. Sensitive files are hidden. PDFs are extracted as markdown. Directories return a listing. Use offset/limit to page through a long file.", {
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
            ToolSpec("write_file", (
                "Write a file under an allowed root. To write a document longer "
                "than a few thousand words, send the first part with append "
                "omitted, then the rest in further calls with append=true — one "
                "call carrying the whole document can exceed the response limit "
                "and be cut off."
            ), {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "A human filename (Survey.md). A bare name lands in "
                            "this task's reports (or the matching collection). "
                            "Do not use plan output tokens such as draft.section "
                            "or draft.manuscript as the path — those are ledger "
                            "names. Give a directory to write somewhere specific."
                        ),
                    },
                    "contents": {"type": "string"},
                    "append": {
                        "type": "boolean",
                        "description": (
                            "Add to the end of the file instead of replacing it. "
                            "Defaults to false (replace)."
                        ),
                    },
                },
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
    for tool in tools:
        tool.outcome_resolver = fs_result_outcome
    return tools
