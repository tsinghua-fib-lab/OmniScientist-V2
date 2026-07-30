"""Curated memory files — the human-readable half of long-term memory.

OmniScientist injects three deterministic, git-friendly Markdown files into
every session (mirroring Claude Code's ``CLAUDE.md`` and Codex's ``AGENTS.md``
+ personal memories):

- project ``AGENTS.md`` / ``CLAUDE.md`` (repo rules, conventions) — authoritative
- user ``~/.omni/MEMORY.md`` (personal preferences, durable facts)

These are *files as interface*: a researcher can open and edit them, version
them, and the agent treats them as high-priority context. They complement the
SQLite ``memory_entries`` store (the fuzzy, learned half).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

from omni.config.paths import OmniPaths

if TYPE_CHECKING:
    from omni.memory.service import MemoryService

_PROJECT_MEMORY_NAMES = ("AGENTS.md", "CLAUDE.md")

# A line a human explicitly flagged as durable: a "[pin]" marker, or a bullet in
# the personal MEMORY.md. These flow back into the SQLite store (pinned) so the
# file and the learned memory stay in sync — "files as interface", both ways.
_PIN_MARKER_RE = re.compile(r"\[\[?pin\]?\]|#pin\b|^\s*!", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+)$")


def user_memory_file(paths: OmniPaths) -> Path:
    """User-level personal memory file (``~/.omni/MEMORY.md``)."""
    return paths.home / "MEMORY.md"


def user_profile_file(paths: OmniPaths) -> Path:
    """Self-maintained global user profile (``~/.omni/profile.md``).

    Unlike ``MEMORY.md`` (which the human curates), this file is *written by the
    agent*: at session end it distils the owner's stable preferences/decisions
    into a compact persona note, injected into every workspace so the agent
    "gets to know you". Human edits survive — they're fed back into the next
    LLM merge as the prior profile.
    """
    return paths.home / "profile.md"


def project_memory_files(paths: OmniPaths) -> list[Path]:
    """Project-level curated files at the workspace root (``AGENTS.md``/``CLAUDE.md``)."""
    root = paths.workspace_root
    if root is None:
        return []
    return [root / name for name in _PROJECT_MEMORY_NAMES]


def curated_memory_paths(paths: OmniPaths) -> list[Path]:
    """All curated memory file candidates, project rules first."""
    return [*project_memory_files(paths), user_memory_file(paths)]


def load_curated_memory(paths: OmniPaths, *, per_file: int = 1200, budget: int = 3000) -> str:
    """Read curated memory files into a single, budgeted prompt block.

    Project rules come first (and take precedence on conflict). Returns ``""``
    when no curated file exists, so the prompt stays lean for fresh projects.
    """
    blocks: list[str] = []
    used = 0

    def add_text(label: str, name: str, text: str) -> None:
        nonlocal used
        text = (text or "").strip()
        if used >= budget or not text:
            return
        text = text[:per_file]
        remaining = budget - used
        if len(text) > remaining:
            text = text[:remaining].rstrip() + " ...(truncated)"
        blocks.append(f"### {label} ({name})\n{text}")
        used += len(text)

    def add(label: str, path: Path) -> None:
        if not path.is_file():
            return
        try:
            add_text(label, path.name, path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return

    for p in project_memory_files(paths):
        add("Project memory and rules", p)
    # Machine-global memory digest first among the personal blocks: the small,
    # always-fresh "who you are" that follows the owner across projects/channels.
    # Use the cleaned body (marker/header stripped) rather than the raw file.
    add_text("Global memory digest (automatic)", "memory_summary.md",
             load_memory_summary(paths, budget=per_file))
    # Self-maintained persona note next: the distilled persona prose.
    add("User profile (automatic)", user_profile_file(paths))
    add("User memory and preferences", user_memory_file(paths))
    if not blocks:
        return ""
    return "Curated memory (authoritative; project rules win on conflict):\n" + "\n\n".join(blocks)


# ── machine-global memory digest (memory_summary.md) ───────────────────────

_SUMMARY_FILE_HEADER = (
    "# Global Memory Digest (memory_summary.md, maintained automatically)\n\n"
    "> A compact, always-injected snapshot of your durable preferences, shared across every\n"
    "> workspace, terminal and channel. Rewritten only when the underlying memory changes.\n\n"
)
_SUMMARY_HASH_RE = re.compile(r"omni:memhash=([0-9a-f]+)")


def memory_summary_file(paths: OmniPaths) -> Path:
    """The small, always-injected global memory digest (``~/.omni/memories/memory_summary.md``)."""
    return paths.memory_summary_file


def _summary_hash(bullets: list[str]) -> str:
    return hashlib.sha1("\n".join(bullets).encode("utf-8")).hexdigest()[:16]


def write_memory_summary(paths: OmniPaths, bullets: list[str], *, budget_chars: int = 2800) -> bool:
    """Rewrite ``memory_summary.md`` only when its content changed. Returns changed?.

    The digest is bounded to ``budget_chars`` and stamped with a content hash so a
    consolidation pass that produced no new durable memory is a pure no-op (the
    "only refresh on change" guarantee). An empty digest removes any stale file.
    Best-effort — never raises.
    """
    clean = [" ".join(str(b).split())[:200] for b in bullets if b and str(b).strip()]
    path = memory_summary_file(paths)
    new_hash = _summary_hash(clean)
    existing = ""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
    match = _SUMMARY_HASH_RE.search(existing)
    if match and match.group(1) == new_hash:
        return False  # unchanged → skip the rewrite (and its downstream churn)
    if not clean:
        # Nothing durable to inject: drop a stale digest so we never inject old data.
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return bool(existing)
    body_lines: list[str] = []
    used = 0
    for b in clean:
        line = f"- {b}\n"
        if used + len(line) > max(200, budget_chars):
            break
        body_lines.append(line)
        used += len(line)
    content = f"<!-- omni:memhash={new_hash} -->\n{_SUMMARY_FILE_HEADER}{''.join(body_lines)}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError:
        return False
    return True


def load_memory_summary(paths: OmniPaths, *, budget: int = 1200) -> str:
    """Read the global memory digest body for prompt injection (or ``""``).

    Strips the hash marker comment and header guidance, returning just the digest
    bullets, truncated to ``budget`` chars.
    """
    path = memory_summary_file(paths)
    if not path.is_file():
        return ""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [
        ln for ln in raw.splitlines()
        if not ln.startswith("<!--") and not ln.startswith("#") and not ln.startswith(">")
    ]
    text = "\n".join(lines).strip()
    return text[:budget]


_PROFILE_FILE_HEADER = (
    "# User Profile (profile.md, maintained automatically)\n\n"
    "> OmniScientist summarizes stable preferences and research decisions here and injects them into each workspace.\n"
    "> Manual edits are preserved and merged during the next profile update; newer information wins on conflict.\n\n"
)


def load_user_profile(paths: OmniPaths, *, budget: int = 1200) -> str:
    """Read the self-maintained profile body for prompt injection (or ``""``)."""
    path = user_profile_file(paths)
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[:budget]


def write_user_profile(paths: OmniPaths, body: str) -> bool:
    """Overwrite ``~/.omni/profile.md`` with a freshly distilled profile body.

    ``body`` is the bullet list only; the human-facing header is prepended here
    so the file stays self-describing. Best-effort (never raises).
    """
    body = (body or "").strip()
    if not body:
        return False
    path = user_profile_file(paths)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_PROFILE_FILE_HEADER + body + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _curated_pin_lines(paths: OmniPaths) -> list[tuple[str, str]]:
    """Extract human-flagged durable lines → ``(text, memory_type)`` pairs.

    - every bullet in the personal ``MEMORY.md`` → a ``preference``;
    - any line marked ``[pin]`` / ``#pin`` / leading ``!`` in MEMORY/NOTEBOOK →
      a ``finding`` (project knowledge the user wants kept verbatim).
    """
    out: list[tuple[str, str]] = []
    mem = user_memory_file(paths)
    if mem.is_file():
        for raw in mem.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _BULLET_RE.match(raw)
            if m and len(m.group(1).strip()) >= 4:
                out.append((m.group(1).strip()[:200], "preference"))
    for path in (mem, getattr(paths, "notebook", None)):
        if path and Path(path).is_file():
            for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if len(line) >= 5 and _PIN_MARKER_RE.search(line):
                    cleaned = _PIN_MARKER_RE.sub("", line).strip(" -*!").strip()
                    if len(cleaned) >= 4:
                        out.append((cleaned[:200], "finding"))
    # de-dupe within the harvested set (preserve first occurrence/type)
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for text, mtype in out:
        key = text[:60]
        if key not in seen:
            seen.add(key)
            uniq.append((text, mtype))
    return uniq


_MEMORY_FILE_HEADER = (
    "# Long-Term User Memory (MEMORY.md)\n\n"
    "> Each `- item` is injected into all workspaces and imported as a pinned preference. "
    "Lines marked `[pin]` or starting with `!` are imported as pinned facts. Edit with `omni memory edit`.\n\n"
)


def append_user_preference(paths: OmniPaths, text: str) -> bool:
    """Mirror a distilled user preference into the global ``~/.omni/MEMORY.md``.

    Learned "user"-scope preferences live in the per-workspace DB, so on their
    own they never follow the researcher into another project. Appending them
    as bullets here — the file every workspace injects and re-imports as pinned
    memory — is what makes user-scope preferences truly user-global.
    Deduplicates by substring so repeated sessions don't accumulate copies.
    """
    text = " ".join((text or "").split())[:200]
    if len(text) < 4:
        return False
    path = user_memory_file(paths)
    existing = ""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
    if text in existing:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            if not existing:
                fh.write(_MEMORY_FILE_HEADER)
            fh.write(f"- {text}\n")
    except OSError:
        return False
    return True


def read_user_memory_bullets(paths: OmniPaths) -> tuple[list[str], bool]:
    """Return ``(bullets, safe_to_rewrite)`` for ``~/.omni/MEMORY.md``.

    ``safe_to_rewrite`` is only True when the file is the standard header +
    bullet list with no other hand-written prose — so an automated rewrite can
    never clobber sections a human added. Headings (``#``) and blockquote
    guidance (``>``) are ignored, everything else counts as prose.
    """
    path = user_memory_file(paths)
    if not path.is_file():
        return [], False
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], False
    bullets: list[str] = []
    has_prose = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(">"):
            continue
        m = _BULLET_RE.match(line)
        if m and len(m.group(1).strip()) >= 4:
            bullets.append(m.group(1).strip())
        else:
            has_prose = True
    return bullets, (bool(bullets) and not has_prose)


def rewrite_user_memory(paths: OmniPaths, bullets: list[str]) -> bool:
    """Overwrite ``~/.omni/MEMORY.md`` with the standard header + ``bullets``.

    Caller must have verified the file is safe to rewrite (see
    :func:`read_user_memory_bullets`). Best-effort; never raises.
    """
    clean = [" ".join(b.split())[:200] for b in bullets if b and b.strip()]
    if not clean:
        return False
    path = user_memory_file(paths)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _MEMORY_FILE_HEADER + "".join(f"- {b}\n" for b in clean),
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


async def import_curated_memory(paths: OmniPaths, memory: MemoryService) -> int:
    """Flow human-flagged lines from MEMORY.md/NOTEBOOK.md back into the store.

    Idempotent: skips lines already present (near-duplicate) in semantic memory.
    Imported entries are pinned so they always surface. Returns the count added.
    """
    added = 0
    for text, mtype in _curated_pin_lines(paths):
        try:
            if await memory._is_duplicate_semantic(text):
                continue
            scope = "user" if mtype == "preference" else "project"
            await memory.record(
                layer="M4", scope=scope, scope_id="local" if scope == "user" else "",
                summary=text, memory_type=mtype, importance=0.85, pinned=True,
            )
            added += 1
        except Exception:  # noqa: BLE001
            continue
    return added
