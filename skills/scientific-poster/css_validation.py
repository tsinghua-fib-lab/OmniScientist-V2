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
    if _css_can_generate_content(normalized):
        issues.add("generated_text")
    return frozenset(issues)


def _css_can_generate_content(css: str) -> bool:
    """Inspect declarations once with the parser already required by the Skill."""

    return bool(generated_content_declarations(css))


def generated_content_declarations(css: str) -> list[dict[str, str]]:
    """Return the exact CSS declarations rejected by the generated-content rule."""

    from tinycss2 import parse_blocks_contents, serialize

    pending = list(parse_blocks_contents(css, skip_comments=True, skip_whitespace=True))
    declarations: list[dict[str, str]] = []
    while pending:
        item = pending.pop()
        if item.type == "declaration":
            value = " ".join(serialize(item.value).split())
            if _declaration_can_generate_content(item.lower_name, value):
                declarations.append(
                    {"property": str(item.lower_name), "value": value}
                )
        elif item.type == "at-rule" and item.lower_at_keyword == "counter-style":
            # Custom systems can turn a presentation marker into arbitrary copy.
            declarations.append({"property": "@counter-style", "value": serialize(item.prelude).strip()})
        content = getattr(item, "content", None)
        if content is not None:
            pending.extend(
                parse_blocks_contents(
                    content,
                    skip_comments=True,
                    skip_whitespace=True,
                )
            )
    return list(reversed(declarations))


def _declaration_can_generate_content(property_name: str, value: str) -> bool:
    """Allow native marker styles, not authored text outside the scientific DOM."""

    name = property_name.casefold()
    normalized = " ".join(value.split()).casefold()
    if name == "content":
        return normalized not in {"normal", "none", '""', "''"}
    if name == "list-style-image":
        return normalized != "none"
    if name not in {"list-style", "list-style-type"}:
        return False
    from tinycss2 import parse_component_value_list

    # Native style identifiers are interpreted by Chromium, not enumerated here.
    # Strings/functions could inject copy through custom symbols or variables.
    return any(token.type not in {"ident", "whitespace", "comment"}
               for token in parse_component_value_list(value))
