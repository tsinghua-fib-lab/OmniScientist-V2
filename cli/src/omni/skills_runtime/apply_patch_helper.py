"""Self-contained unified-diff / Codex-patch applier.

Copied into the sandbox as ``apply_patch.py`` so a skill process can patch
files without a bash heredoc. The host ``apply_patch`` tool imports the same
functions.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

BEGIN_MARK = "*** Begin Patch"
END_MARK = "*** End Patch"
UPDATE_MARK = "*** Update File:"
ADD_MARK = "*** Add File:"
DELETE_MARK = "*** Delete File:"


class PatchError(ValueError):
    """A hunk or file header did not match the current file."""

    def __init__(self, message: str, *, hunk_index: int | None = None, path: str = "") -> None:
        super().__init__(message)
        self.hunk_index = hunk_index
        self.path = path


@dataclass
class Hunk:
    old_start: int = 0
    old_count: int = 0
    new_start: int = 0
    new_count: int = 0
    lines: list[tuple[str, str]] = field(default_factory=list)

    @property
    def expected_old(self) -> list[str]:
        return [text for op, text in self.lines if op in {" ", "-"}]


@dataclass
class FilePatch:
    path: str
    kind: str  # update | add | delete
    hunks: list[Hunk] = field(default_factory=list)


def parse_patch(text: str) -> list[FilePatch]:
    """Parse a unified diff or a Codex-style begin/end patch."""
    body = str(text or "")
    if not body.strip():
        raise PatchError("patch is empty")
    if BEGIN_MARK in body or UPDATE_MARK in body or ADD_MARK in body:
        return _parse_codex(body)
    return _parse_unified(body)


def apply_to_text(original: str, hunks: list[Hunk], *, path: str = "") -> str:
    """Apply ``hunks`` to ``original``. Raises :class:`PatchError` on mismatch."""
    if not hunks:
        return original
    lines = original.splitlines(keepends=True)
    # Normalize to newline-terminated working lines, remember whether the
    # original ended without a newline.
    ended_nl = original.endswith("\n") if original else True
    work = [line.rstrip("\n") for line in original.splitlines()]
    cursor = 0
    for index, hunk in enumerate(hunks, start=1):
        start = _locate_hunk(work, hunk, cursor)
        if start is None:
            preview = "\n".join(hunk.expected_old[:4])
            raise PatchError(
                f"hunk {index} did not match in {path or 'file'}: expected\n{preview}",
                hunk_index=index,
                path=path,
            )
        replacement: list[str] = []
        consumed = 0
        for op, text in hunk.lines:
            if op == " ":
                if start + consumed >= len(work) or work[start + consumed] != text:
                    raise PatchError(
                        f"hunk {index} context mismatch in {path or 'file'} "
                        f"at line {start + consumed + 1}",
                        hunk_index=index,
                        path=path,
                    )
                replacement.append(text)
                consumed += 1
            elif op == "-":
                if start + consumed >= len(work) or work[start + consumed] != text:
                    found = work[start + consumed] if start + consumed < len(work) else "<eof>"
                    raise PatchError(
                        f"hunk {index} did not match in {path or 'file'}: "
                        f"expected {text!r}, found {found!r}",
                        hunk_index=index,
                        path=path,
                    )
                consumed += 1
            elif op == "+":
                replacement.append(text)
        work[start : start + consumed] = replacement
        cursor = start + len(replacement)
    text = "\n".join(work)
    if ended_nl or lines:
        if original.endswith("\n") or hunks:
            if work:
                text += "\n"
            elif original:
                text = ""
    return text


def apply_file_patches(
    patches: list[FilePatch],
    *,
    read_text,
    write_text,
    exists,
    delete,
) -> list[str]:
    """Apply each file patch via the supplied I/O callbacks. Returns notes."""
    notes: list[str] = []
    for patch in patches:
        if not patch.path:
            raise PatchError("patch is missing a target path")
        if patch.kind == "add":
            if exists(patch.path):
                raise PatchError(f"cannot add {patch.path}: file already exists", path=patch.path)
            write_text(patch.path, _added_text(patch.hunks))
            notes.append(f"added {patch.path}")
            continue
        if patch.kind == "delete":
            if not exists(patch.path):
                raise PatchError(f"cannot delete {patch.path}: file not found", path=patch.path)
            delete(patch.path)
            notes.append(f"deleted {patch.path}")
            continue
        if not exists(patch.path):
            raise PatchError(f"cannot update {patch.path}: file not found", path=patch.path)
        original = read_text(patch.path)
        updated = apply_to_text(original, patch.hunks, path=patch.path)
        write_text(patch.path, updated)
        notes.append(f"updated {patch.path} ({len(patch.hunks)} hunk(s))")
    return notes


def _added_text(hunks: list[Hunk]) -> str:
    lines: list[str] = []
    for hunk in hunks:
        for op, text in hunk.lines:
            if op in {"+", " "}:
                lines.append(text)
    return ("\n".join(lines) + "\n") if lines else ""


def _locate_hunk(work: list[str], hunk: Hunk, cursor: int) -> int | None:
    expected = hunk.expected_old
    if not expected:
        # Pure-addition hunk: insert at old_start (1-based) or at cursor.
        if hunk.old_start > 0:
            pos = min(max(hunk.old_start - 1, 0), len(work))
            return pos
        return cursor
    start_hint = hunk.old_start - 1 if hunk.old_start > 0 else cursor
    for origin in (max(start_hint, 0), 0):
        pos = _find_sequence(work, expected, origin)
        if pos is not None:
            return pos
    return None


def _find_sequence(work: list[str], expected: list[str], origin: int) -> int | None:
    if origin > len(work):
        return None
    last = len(work) - len(expected)
    if last < origin:
        return None
    for index in range(origin, last + 1):
        if work[index : index + len(expected)] == expected:
            return index
    return None


def _parse_unified(text: str) -> list[FilePatch]:
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    for raw in text.splitlines():
        if raw.startswith("--- "):
            if current is not None:
                _close_hunk(current, hunk)
                patches.append(current)
            path = _strip_diff_prefix(raw[4:])
            current = FilePatch(path=path, kind="update")
            hunk = None
            continue
        if raw.startswith("+++ "):
            if current is None:
                current = FilePatch(path=_strip_diff_prefix(raw[4:]), kind="update")
            else:
                plus = _strip_diff_prefix(raw[4:])
                if plus and plus != "/dev/null":
                    current.path = plus
            if current.path == "/dev/null":
                current.kind = "delete"
            hunk = None
            continue
        if raw.startswith("@@"):
            if current is None:
                raise PatchError("hunk header appeared before a file header")
            _close_hunk(current, hunk)
            hunk = _parse_hunk_header(raw)
            current.hunks.append(hunk)
            continue
        if hunk is None:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            hunk.lines.append(("+", raw[1:]))
        elif raw.startswith("-"):
            hunk.lines.append(("-", raw[1:]))
        elif raw.startswith(" "):
            hunk.lines.append((" ", raw[1:]))
        elif raw == "":
            hunk.lines.append((" ", ""))
    if current is not None:
        _close_hunk(current, hunk)
        patches.append(current)
    if not patches:
        raise PatchError("no file hunks found in unified diff")
    for patch in patches:
        if patch.path in {"/dev/null", ""}:
            raise PatchError("unified diff is missing a target path")
        if not patch.hunks and patch.kind == "update":
            raise PatchError(f"no hunks for {patch.path}", path=patch.path)
    return patches


def _parse_codex(text: str) -> list[FilePatch]:
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    for raw in text.splitlines():
        stripped = raw.rstrip("\n")
        if stripped in {BEGIN_MARK, END_MARK}:
            continue
        if stripped.startswith(UPDATE_MARK):
            if current is not None:
                _close_hunk(current, hunk)
                patches.append(current)
            current = FilePatch(path=stripped[len(UPDATE_MARK) :].strip(), kind="update")
            hunk = None
            continue
        if stripped.startswith(ADD_MARK):
            if current is not None:
                _close_hunk(current, hunk)
                patches.append(current)
            current = FilePatch(path=stripped[len(ADD_MARK) :].strip(), kind="add")
            hunk = Hunk()
            current.hunks.append(hunk)
            continue
        if stripped.startswith(DELETE_MARK):
            if current is not None:
                _close_hunk(current, hunk)
                patches.append(current)
            current = FilePatch(path=stripped[len(DELETE_MARK) :].strip(), kind="delete")
            hunk = None
            continue
        if stripped.startswith("@@"):
            if current is None:
                raise PatchError("hunk header appeared before a file header")
            _close_hunk(current, hunk)
            hunk = _parse_hunk_header(stripped)
            current.hunks.append(hunk)
            continue
        if current is None:
            continue
        if hunk is None:
            hunk = Hunk()
            current.hunks.append(hunk)
        if stripped.startswith("\\"):
            continue
        if stripped.startswith("+"):
            hunk.lines.append(("+", stripped[1:]))
        elif stripped.startswith("-"):
            hunk.lines.append(("-", stripped[1:]))
        elif stripped.startswith(" "):
            hunk.lines.append((" ", stripped[1:]))
        elif current.kind == "add":
            hunk.lines.append(("+", stripped[1:] if stripped.startswith("+") else stripped))
        elif stripped == "":
            hunk.lines.append((" ", ""))
    if current is not None:
        _close_hunk(current, hunk)
        patches.append(current)
    if not patches:
        raise PatchError("no file operations found in patch")
    return patches


def _close_hunk(current: FilePatch, hunk: Hunk | None) -> None:
    if hunk is None:
        return
    if not hunk.lines and hunk in current.hunks:
        current.hunks.remove(hunk)


def _parse_hunk_header(header: str) -> Hunk:
    # @@ -l,s +l,s @@  or a bare @@
    hunk = Hunk()
    body = header.strip()
    if not body.startswith("@@"):
        return hunk
    parts = body.split("@@")
    spec = parts[1].strip() if len(parts) > 1 else ""
    old_part, _, new_part = spec.partition(" ")
    hunk.old_start, hunk.old_count = _parse_span(old_part, prefix="-")
    hunk.new_start, hunk.new_count = _parse_span(new_part, prefix="+")
    return hunk


def _parse_span(part: str, *, prefix: str) -> tuple[int, int]:
    token = part.strip()
    if token.startswith(prefix):
        token = token[1:]
    if not token:
        return 0, 0
    start, _, count = token.partition(",")
    try:
        return int(start or 0), int(count or 0)
    except ValueError:
        return 0, 0


def _strip_diff_prefix(raw: str) -> str:
    path = raw.strip()
    if "\t" in path:
        path = path.split("\t", 1)[0]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply a unified diff or Codex patch.")
    parser.add_argument("patch", nargs="?", help="Patch file; stdin when omitted")
    parser.add_argument("--root", default=".", help="Directory paths in the patch are relative to")
    args = parser.parse_args(argv)
    raw = Path(args.patch).read_text(encoding="utf-8") if args.patch else sys.stdin.read()
    root = Path(args.root)
    try:
        patches = parse_patch(raw)
    except PatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    def exists(path: str) -> bool:
        return (root / path).is_file()

    def read_text(path: str) -> str:
        return (root / path).read_text(encoding="utf-8")

    def write_text(path: str, text: str) -> None:
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")

    def delete(path: str) -> None:
        (root / path).unlink()

    try:
        notes = apply_file_patches(
            patches,
            read_text=read_text,
            write_text=write_text,
            exists=exists,
            delete=delete,
        )
    except PatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(f"OK: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
