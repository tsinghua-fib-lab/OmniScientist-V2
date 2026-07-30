"""Shared fail-closed checks for inert, offline CSS."""

from __future__ import annotations

import re
from collections.abc import Callable

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)
_URL_START_RE = re.compile(r"(?<![\w-])url\s*\(", re.IGNORECASE)
_ACTIVE_RE = re.compile(
    r"@import\b|@(?:-\w+-)?keyframes\b|"
    r"(?:^|[;{\s])(?:-\w+-)?(?:animation|transition)(?:-[\w-]+)?\s*:",
    re.IGNORECASE,
)
_UNSAFE_REFERENCE_RE = re.compile(
    r"(?:https?|file|javascript|vbscript):|(?<!:)//",
    re.IGNORECASE,
)
_UNSUPPORTED_REFERENCE_RE = re.compile(
    r"(?<![\w-])(?:-webkit-)?(?:image-set|image|cross-fade|src)\s*\(",
    re.IGNORECASE,
)


def offline_css_issues(
    css: str,
    *,
    safe_reference: Callable[[str], bool],
) -> frozenset[str]:
    """Classify CSS that cannot be proven inert and offline without a CSS parser."""

    issues: set[str] = set()
    if "\\" in css:
        issues.add("unsafe_syntax")
    normalized = _COMMENT_RE.sub("", css)
    if _ACTIVE_RE.search(normalized):
        issues.add("active_construct")
    url_matches = list(_URL_RE.finditer(normalized))
    if (
        _UNSAFE_REFERENCE_RE.search(normalized)
        or _UNSUPPORTED_REFERENCE_RE.search(normalized)
        or len(url_matches) != len(_URL_START_RE.findall(normalized))
        or any(
            not safe_reference(match.group(2).strip(" '\"")) for match in url_matches
        )
    ):
        issues.add("unsafe_reference")
    return frozenset(issues)
