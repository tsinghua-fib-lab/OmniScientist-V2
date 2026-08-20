"""Align web uploads with the CLI ``@path`` + absolute ``file_uris`` contract.

The planner, field resolvers, and grounding only see ``user_message`` (plus a
context summary). ``file_uris`` grant a later fs read; they are not a second
input channel. The SPA paperclip therefore has to become the same submit the
REPL would have typed: an existing absolute path in ``file_uris``, and an
``@`` mention of that path in the text.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from omni.core.file_mentions import format_mention, parse_mentions


def normalize_web_file_uri(uri: str, *, cwd: Path | None = None) -> str | None:
    """Turn a web upload URI into an existing absolute filesystem path.

    Accepts a bare path or a ``file://`` URI (including percent-encoded
    spaces). Remote ``file://`` hosts and missing paths are dropped so a
    stale chip cannot widen the read grant.
    """
    raw = str(uri or "").strip()
    if not raw or raw.startswith("artifact://"):
        return None
    if raw.lower().startswith("file:"):
        parsed = urlparse(raw)
        if parsed.scheme != "file":
            return None
        if parsed.netloc and parsed.netloc not in {"", "localhost"}:
            return None
        # ``file:///C:/Users/...`` parses as ``/C:/Users/...``. ``url2pathname``
        # turns that into a real Windows path; POSIX paths stay unchanged.
        raw = url2pathname(unquote(parsed.path))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    if not resolved.exists():
        return None
    return str(resolved)


def bind_web_attachments(
    text: str,
    file_uris: Sequence[str] | None,
    *,
    cwd: Path | None = None,
) -> tuple[str, list[str] | None]:
    """Merge uploaded files into the text the way the CLI composer does.

    Already-mentioned paths are left alone so a retry or a frontend that
    already appended ``@`` does not duplicate the line.
    """
    extras: list[str] = []
    seen: set[str] = set()
    for uri in file_uris or []:
        path = normalize_web_file_uri(str(uri), cwd=cwd)
        if path is None or path in seen:
            continue
        seen.add(path)
        extras.append(path)

    mentioned = {str(m.path) for m in parse_mentions(text, cwd=cwd) if m.exists}
    to_inject = [path for path in extras if path not in mentioned]
    if not to_inject:
        return text, extras or None

    mentions = "\n".join(format_mention(path) for path in to_inject)
    body = text.rstrip()
    merged = f"{body}\n\n{mentions}" if body else mentions
    return merged, extras or None
