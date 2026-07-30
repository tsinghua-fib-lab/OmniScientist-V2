"""``@path`` file mentions for Omni's interactive surfaces.

Parsing is deliberately *stateless*: the ``@`` marker stays in the submitted
text instead of being replaced by a bare path. Codex can drop the marker
because a mentioned text file is, to it, nothing but characters the model may
later read; omni needs the marker at submit time because an explicit mention is
also an *authorization* (it widens the read roots for that turn) and the input
for the bounded attachment digest. A marker carried in the text works
identically for the REPL composer and for a one-shot ``omni chat "review
@README.md"``, where no composer state exists at all, and it survives history
and retry snapshots for free.

A mention is only recognised at a word boundary, so ordinary prose containing
``user@example.com`` or ``HEAD@{0}`` is never mistaken for an attachment.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from omni.core.path_lookup import QUOTE_WRAPPERS, closer_for_quote, resolve_existing_path

# Characters that may precede ``@`` for it to count as a mention. Anything else
# (a letter, digit, ``/`` …) means the ``@`` sits inside a larger word such as an
# email address or a git revision, which must not be treated as an attachment.
_BOUNDARY_BEFORE = "([{<\"'\u201c\u201d\u2018\u2019"
# Trailing characters trimmed when the literal token does not resolve. ``.`` is
# excluded on purpose: it is far more often part of a filename than sentence
# punctuation, and the resolve-aware retry below recovers the sentence case.
_TRAILING_PUNCTUATION = ",;:!?)]}>\"'`\u201c\u201d\u2018\u2019"
# A set, not a string: ``"" in '"\''`` is True for the empty slice of a bare
# ``@``, which would misread it as an unterminated quoted mention.
_QUOTES = QUOTE_WRAPPERS


@dataclass(frozen=True)
class Mention:
    """One ``@path`` mention resolved against a working directory.

    ``raw`` is the path text as the user typed it (without ``@`` and without
    surrounding quotes); ``path`` is the absolute resolution. ``exists`` lets
    callers warn about a typo instead of silently attaching nothing — a mention
    that resolves to nothing must never become a read grant.
    """

    raw: str
    path: Path
    exists: bool

    @property
    def is_dir(self) -> bool:
        return self.exists and self.path.is_dir()


@dataclass(frozen=True)
class ActiveMention:
    """The ``@`` token the cursor currently sits inside.

    ``quoted`` records that the user (or a previous completion) already opened a
    quote, so a completer can close it instead of emitting a second one.
    """

    token: str
    quoted: bool


def active_mention_token(text_before_cursor: str) -> ActiveMention | None:
    """The mention being typed at the cursor, or ``None`` when there is none.

    Returns an empty token for a bare ``@`` so a completer can answer it with an
    overview listing rather than an empty popup. A ``@`` that is not at a word
    boundary (``user@example.com``) never starts a mention, and whitespace ends
    an unquoted one.
    """
    index = text_before_cursor.rfind("@")
    if index == -1 or not _at_boundary(text_before_cursor, index):
        return None
    after = text_before_cursor[index + 1 :]
    if after[:1] in _QUOTES:
        quote = after[0]
        body = after[1:]
        if quote in body:  # the quote is closed: this mention is finished
            return None
        return ActiveMention(token=body, quoted=True)
    if any(char.isspace() for char in after):
        return None
    return ActiveMention(token=after, quoted=False)


def iter_mention_tokens(text: str) -> Iterator[str]:
    """Yield the raw path text of every ``@`` mention in ``text``, in order.

    Handles the quoted form (``@"a b/c.md"``) that a completer emits for paths
    containing whitespace, and stops an unquoted token at the first space.
    """
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "@" or not _at_boundary(text, index):
            index += 1
            continue
        token, index = _read_token(text, index + 1)
        if token:
            yield token
    return


def parse_mentions(text: str, *, cwd: Path | None = None) -> list[Mention]:
    """Resolve every ``@`` mention in ``text``, de-duplicated by absolute path.

    Order of first appearance is preserved so the digest and any user-facing
    listing read in the order the user wrote them.
    """
    base = (cwd or Path.cwd()).resolve()
    out: list[Mention] = []
    seen: set[Path] = set()
    for token in iter_mention_tokens(text):
        mention = resolve_mention(token, cwd=base)
        if mention.path in seen:
            continue
        seen.add(mention.path)
        out.append(mention)
    return out


def mention_file_uris(text: str, *, cwd: Path | None = None) -> list[str]:
    """Absolute paths of the mentions that actually exist, as ``file_uris``.

    Only existing paths are returned: ``file_uris`` doubles as the explicit
    read grant for the turn, and granting access to a path that is not there
    would widen authorization for nothing.
    """
    return [str(m.path) for m in parse_mentions(text, cwd=cwd) if m.exists]


@dataclass(frozen=True)
class TurnAttachments:
    """What a submitted line attaches, and what it failed to attach.

    ``file_uris`` is ``None`` when there is nothing to attach so callers keep
    passing the existing default through unchanged. ``missing`` exists so a typo
    is reported instead of silently attaching nothing.
    """

    file_uris: list[str] | None
    missing: list[str]


def resolve_turn_attachments(
    text: str,
    *,
    cwd: Path | None = None,
    extra: Sequence[str] | None = None,
) -> TurnAttachments:
    """Merge the line's ``@`` mentions with attachments the caller already has.

    ``extra`` carries uris from elsewhere — today a ``task retry`` snapshot —
    which must survive alongside anything newly mentioned. Order is caller-first,
    then mention order, de-duplicated.
    """
    mentions = parse_mentions(text, cwd=cwd)
    merged: list[str] = [str(uri) for uri in (extra or []) if str(uri or "").strip()]
    for mention in mentions:
        if not mention.exists:
            continue
        uri = str(mention.path)
        if uri not in merged:
            merged.append(uri)
    return TurnAttachments(
        file_uris=merged or None,
        missing=[mention.raw for mention in mentions if not mention.exists],
    )


def resolve_mention(raw: str, *, cwd: Path) -> Mention:
    """Resolve one mention token, retrying without trailing punctuation.

    ``review @notes.md, then plot`` yields the token ``notes.md,``. Rather than
    guessing which trailing characters are punctuation, each candidate is tested
    against the filesystem and the first hit wins; when nothing exists the
    untrimmed token is reported so the caller can echo exactly what was typed.
    """
    for candidate in _trim_candidates(raw):
        path = _absolute(candidate, cwd)
        if path.exists():
            return Mention(raw=candidate, path=path, exists=True)
        alt = resolve_existing_path(path)
        if alt is not None:
            try:
                return Mention(raw=candidate, path=alt.resolve(), exists=True)
            except (OSError, RuntimeError):
                return Mention(raw=candidate, path=alt, exists=True)
    return Mention(raw=raw, path=_absolute(raw, cwd), exists=False)


def strip_mention_marker(path: str) -> str:
    """Drop a leading ``@`` from a path argument.

    The marker stays in the prompt text, so a model may copy ``@notes.md``
    verbatim into a tool call. Accepting that spelling costs one line and turns
    an avoidable "path does not exist" dead end into a successful read.
    """
    text = str(path or "").strip()
    return text[1:] if text.startswith("@") else text


def _at_boundary(text: str, index: int) -> bool:
    if index == 0:
        return True
    previous = text[index - 1]
    return previous.isspace() or previous in _BOUNDARY_BEFORE


def _read_token(text: str, start: int) -> tuple[str, int]:
    """Read one token after ``@``; returns the token and the next scan index."""
    if start < len(text) and text[start] in _QUOTES:
        quote = text[start]
        closer = closer_for_quote(quote)
        end = text.find(closer, start + 1)
        if end == -1 and closer != quote:
            end = text.find(quote, start + 1)
        if end == -1:  # unterminated quote: treat the rest of the line as the path
            return text[start + 1 :].strip(), len(text)
        return text[start + 1 : end], end + 1
    end = start
    while end < len(text) and not text[end].isspace():
        end += 1
    return text[start:end], end


def _trim_candidates(raw: str) -> list[str]:
    """``raw`` first, then progressively shorter trailing-punctuation trims."""
    candidates = [raw]
    trimmed = raw
    while trimmed and trimmed[-1] in _TRAILING_PUNCTUATION:
        trimmed = trimmed[:-1]
        if trimmed:
            candidates.append(trimmed)
    # A single trailing dot is sentence punctuation only when the trimmed form
    # exists, which the caller's existence check decides.
    if trimmed.endswith(".") and len(trimmed) > 1:
        candidates.append(trimmed.rstrip("."))
    return candidates


def _absolute(raw: str, cwd: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = cwd / path
    # ``resolve`` also normalises ``..`` segments so the read-grant comparison
    # in the fs tools cannot be fooled by ``@sub/../../etc/passwd``.
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path
