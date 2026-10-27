"""Shared fail-closed checks for inert, offline CSS."""

from __future__ import annotations

from collections.abc import Callable


def inline_style_is_hidden(style: str) -> bool:
    """Read unambiguous inline hiding hints for static content collection.

    An unknown later value keeps content in the static snapshot. Chromium owns
    value support, substitution and the full cascade during rendered inspection.
    """

    from tinycss2 import parse_declaration_list

    declarations: dict[str, tuple[bool, str | None]] = {}
    for item in parse_declaration_list(style, skip_comments=True, skip_whitespace=True):
        if item.type != "declaration" or item.lower_name not in {"display", "visibility"}:
            continue
        values = [token for token in item.value if token.type not in {"whitespace", "comment"}]
        if not values or any(token.type == "error" for token in values):
            continue
        value = values[0].lower_value if len(values) == 1 and values[0].type == "ident" else None
        previous = declarations.get(item.lower_name)
        if previous is None or item.important or not previous[0]:
            declarations[item.lower_name] = (item.important, value)
    return declarations.get("display", (False, None))[1] == "none" or (
        declarations.get("visibility", (False, None))[1] in {"hidden", "collapse"}
    )


def _css_nodes(css: str):
    """Visit declarations and rules, including CSS nesting and grouping rules."""

    from tinycss2 import parse_blocks_contents

    pending = list(reversed(parse_blocks_contents(css, skip_comments=True, skip_whitespace=True)))
    while pending:
        item = pending.pop()
        yield item
        content = getattr(item, "content", None)
        if content is not None:
            pending.extend(reversed(parse_blocks_contents(content, skip_comments=True, skip_whitespace=True)))


def _unprefixed(name: str) -> str:
    return name.split("-", 2)[-1] if name.startswith("-") and not name.startswith("--") else name


def offline_css_issues(
    css: str,
    *,
    safe_reference: Callable[[str], bool],
) -> frozenset[str]:
    """Classify decoded CSS syntax and references through tinycss2's AST."""

    from tinycss2 import parse_component_value_list

    issues: set[str] = set()
    pending = list(parse_component_value_list(css, skip_comments=True))
    while pending:
        token = pending.pop()
        if token.type == "error":
            issues.add("unsafe_syntax")
            if token.kind == "bad-url":
                issues.add("unsafe_reference")
        elif token.type == "url":
            if not safe_reference(token.value.strip()):
                issues.add("unsafe_reference")
        elif token.type == "function":
            name = _unprefixed(token.lower_name)
            if name == "url":
                values = [item for item in token.arguments if item.type not in {"whitespace", "comment"}]
                if len(values) != 1 or values[0].type != "string" or not safe_reference(values[0].value.strip()):
                    issues.add("unsafe_reference")
            elif name in {"image-set", "image", "cross-fade", "src"}:
                issues.add("unsafe_reference")
            elif name == "expression":
                issues.add("active_construct")
        pending.extend(getattr(token, "arguments", getattr(token, "content", ())))
    for item in _css_nodes(css):
        if item.type == "at-rule" and _unprefixed(item.lower_at_keyword) in {"import", "keyframes"}:
            issues.add("active_construct")
        if item.type == "declaration" and _unprefixed(item.lower_name).split("-", 1)[0] in {"animation", "transition"}:
            issues.add("active_construct")
    if _css_can_generate_content(css):
        issues.add("generated_text")
    return frozenset(issues)


def _css_can_generate_content(css: str) -> bool:
    """Inspect declarations once with the parser already required by the Skill."""

    return bool(generated_content_declarations(css))


def generated_content_declarations(css: str) -> list[dict[str, str]]:
    """Return the exact CSS declarations rejected by the generated-content rule."""

    from tinycss2 import serialize

    declarations: list[dict[str, str]] = []
    for item in _css_nodes(css):
        if item.type == "declaration":
            value = " ".join(serialize(item.value).split())
            if _declaration_can_generate_content(item.lower_name, value):
                declarations.append(
                    {"property": str(item.lower_name), "value": value}
                )
        elif item.type == "at-rule" and item.lower_at_keyword == "counter-style":
            # Custom systems can turn a presentation marker into arbitrary copy.
            declarations.append({"property": "@counter-style", "value": serialize(item.prelude).strip()})
    return declarations


def _declaration_can_generate_content(property_name: str, value: str) -> bool:
    """Allow native marker styles, not authored text outside the scientific DOM."""

    from tinycss2 import parse_component_value_list

    name = property_name.casefold()
    tokens = [token for token in parse_component_value_list(value) if token.type not in {"whitespace", "comment"}]
    normalized = tokens[0].lower_value if len(tokens) == 1 and tokens[0].type == "ident" else ""
    if name == "content":
        return normalized not in {"normal", "none"} and not (
            len(tokens) == 1 and tokens[0].type == "string" and tokens[0].value == ""
        )
    if name == "list-style-image":
        return normalized != "none"
    if name not in {"list-style", "list-style-type"}:
        return False
    # Native style identifiers are interpreted by Chromium, not enumerated here.
    # Strings/functions could inject copy through custom symbols or variables.
    return any(token.type not in {"ident", "whitespace", "comment"}
               for token in tokens)
