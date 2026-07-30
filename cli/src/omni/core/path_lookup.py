"""Host-side path lookup that tolerates typographic quote equivalents.

Models often rewrite Chinese curly quotes (``“”`` / ``‘’``) to ASCII when
repeating a tool result. Codex-style hosts accept the equivalent spelling
instead of sending the model around a retry loop. If no unique match exists,
lookup fails closed — the caller must not invent a rewritten path.
"""

from __future__ import annotations

from pathlib import Path

# Typographic / ASCII quotation marks that models treat as interchangeable.
# Folded to one character so ``report“draft”.md`` and ``report"draft".md`` compare equal.
_QUOTE_CHARS = frozenset("\"'\u201c\u201d\u2018\u2019")

# Opening mark → expected closer. Used when a whole path is wrapped in quotes,
# not when quotes sit *inside* a filename.
_WRAPPER_CLOSE = {
    '"': '"',
    "'": "'",
    "\u201c": "\u201d",
    "\u201d": "\u201d",
    "\u2018": "\u2019",
    "\u2019": "\u2019",
}
QUOTE_WRAPPERS = frozenset(_WRAPPER_CLOSE)

MISSING_PATH_HINT = (
    "Use the exact path or artifact:// URI from the previous tool result; "
    "do not rewrite quotation marks. Do not retry with a normalized spelling."
)


def fold_quote_marks(text: str) -> str:
    """Replace typographic and ASCII quotes with one comparable character."""
    return "".join('"' if char in _QUOTE_CHARS else char for char in text)


def unwrap_matching_quotes(text: str) -> str:
    """Strip one matching pair of wrapping quotes, including curly pairs."""
    value = str(text or "").strip()
    if len(value) < 2:
        return value
    closer = _WRAPPER_CLOSE.get(value[0])
    if closer is not None and value[-1] == closer:
        return value[1:-1]
    return value


def closer_for_quote(quote: str) -> str:
    """Return the closer that pairs with ``quote`` (same char if unpaired)."""
    return _WRAPPER_CLOSE.get(quote, quote)


def resolve_existing_path(raw: str | Path) -> Path | None:
    """Return the unique existing path that matches ``raw`` after quote-folding.

    Each path component is matched against the real directory listing so a
    Windows-illegal ASCII ``"`` in the *requested* name can still find a file
    whose name uses curly quotes. Zero or several matches fail closed.
    """
    try:
        candidate = Path(raw).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        if candidate.exists():
            return candidate
    except OSError:
        pass

    parts = list(candidate.parts)
    if not parts:
        return None
    if candidate.is_absolute():
        current = Path(parts[0])
        rest = parts[1:]
    else:
        current = Path()
        rest = parts
    try:
        if not current.exists():
            return None
    except OSError:
        return None

    for part in rest:
        try:
            if not current.is_dir():
                return None
            folded = fold_quote_marks(part)
            matches = [
                child
                for child in current.iterdir()
                if fold_quote_marks(child.name) == folded
            ]
        except OSError:
            return None
        if len(matches) != 1:
            return None
        current = matches[0]
    return current


def path_exists(path: Path) -> bool:
    """``exists()`` that treats illegal Windows names as missing, not as a crash."""
    try:
        return path.exists()
    except OSError:
        return False


def path_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def path_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def missing_path_message(path: Path | str, *, next_step: str = "") -> str:
    """Fail-closed observation: name the miss and forbid quote-normalization retries."""
    message = f"ERROR: path does not exist: {path}. {MISSING_PATH_HINT}"
    if next_step:
        return f"{message} {next_step}"
    return message
