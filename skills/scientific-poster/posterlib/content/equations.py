"""Convert source LaTeX into semantic MathML through one maintained backend."""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from html import escape
from typing import Any
from xml.etree import ElementTree

MATHML_NAMESPACE = "http://www.w3.org/1998/Math/MathML"
ElementTree.register_namespace("", MATHML_NAMESPACE)


class EquationRendererUnavailable(RuntimeError):
    """The configured LaTeX-to-MathML backend is unavailable."""


class EquationSyntaxError(ValueError):
    """Source LaTeX cannot be converted into one semantic MathML expression."""


def latex_to_mathml(
    latex: str,
    *,
    attributes: Mapping[str, str] | None = None,
) -> str:
    """Return one safe ``<math>`` tree for the exact source LaTeX."""

    try:
        from latex2mathml.converter import convert
    except ImportError as exc:
        raise EquationRendererUnavailable(
            "latex2mathml is required to render scientific-poster equations"
        ) from exc

    try:
        converted = convert(_without_resize_boxes(latex))
        root = ElementTree.fromstring(converted)
    except EquationRendererUnavailable:
        raise
    except Exception as exc:
        raise EquationSyntaxError(f"LaTeX-to-MathML conversion failed: {exc}") from exc
    if _local_name(root.tag) != "math":
        raise EquationSyntaxError("LaTeX converter did not return one <math> root")
    # The backend emits unknown control sequences as literal identifier text.
    # That is a failed conversion, not a faithfully rendered mathematical symbol.
    for node in root.iter():
        if _local_name(node.tag) == "mi" and re.fullmatch(r"\\[A-Za-z]+", node.text or ""):
            raise EquationSyntaxError(f"Unsupported LaTeX command: {node.text}")

    root.set("display", "block")
    for name, value in (attributes or {}).items():
        root.set(str(name), str(value))
    if root.get("display") == "block":
        _render_explicit_lines(root)
    return ElementTree.tostring(root, encoding="unicode", short_empty_elements=False)


@lru_cache(maxsize=1)
def _resize_box_parser() -> Any:
    """Read source groups before mathematical parsing attaches scripts or splits rows."""

    try:
        import pyparsing as pp
    except ImportError as exc:
        raise EquationRendererUnavailable(
            "pyparsing is required to render source-scaled equations"
        ) from exc

    comment = pp.Regex(r"%[^\n]*(?:\n|$)")
    escaped = pp.Regex(r"\\(?:[A-Za-z]+|[\s\S])")
    literal = pp.Regex(r"\\verb(?P<delimiter>.).*?(?P=delimiter)", flags=re.DOTALL)
    group = pp.original_text_for(pp.nested_expr("{", "}", ignore_expr=literal | escaped | comment))
    math_bodies = []
    for opening, closing in (("$", "$"), (r"\(", r"\)")):
        math_bodies.append(
            pp.Suppress(opening)
            + pp.SkipTo(closing, ignore=group | literal | comment | (~pp.Literal(closing) + escaped))
            + pp.Suppress(closing)
        )
    math_body = pp.MatchFirst(math_bodies).ignore(comment)
    gap = pp.ZeroOrMore(comment).suppress()
    box = (
        pp.Suppress(pp.Regex(r"\\resizebox(?![A-Za-z])")) + gap
        + pp.Opt(pp.Suppress("*") + gap)
        + pp.Suppress(group) + gap + pp.Suppress(group) + gap + group("body")
    )

    def unwrap(tokens: Any) -> str:
        body = transform.transform_string(tokens.body[1:-1])
        content = math_body.parse_string(body, parse_all=True)[0]
        # Retain grouping and the original mathematical bytes, not source-page size.
        return "{" + content + "}"

    box.set_parse_action(unwrap)
    # Unrecognized/malformed boxes remain source text for the backend's existing
    # unsupported-command handling. Comments and escaped commands are not rewrites.
    transform = box | literal | comment | escaped
    return transform


def _without_resize_boxes(latex: str) -> str:
    if r"\resizebox" not in latex:
        return latex
    return _resize_box_parser().transform_string(latex)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _render_explicit_lines(root: ElementTree.Element) -> None:
    """Honor only converter-marked source newlines, shared by all block authors."""

    children = list(root)
    if len(children) != 1 or _local_name(children[0].tag) != "mrow":
        return
    source_row = children[0]
    lines: list[list[ElementTree.Element]] = [[]]
    for child in source_row:
        if _local_name(child.tag) == "mspace" and child.get("linebreak") == "newline":
            lines.append([])
        else:
            lines[-1].append(child)
    if len(lines) < 2 or any(not line for line in lines):
        return

    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""

    def qualified(name: str) -> str:
        return f"{{{namespace}}}{name}" if namespace else name

    table = ElementTree.Element(qualified("mtable"), {"columnalign": "left"})
    for line in lines:
        row = ElementTree.SubElement(table, qualified("mtr"))
        cell = ElementTree.SubElement(row, qualified("mtd"))
        content = ElementTree.SubElement(cell, qualified("mrow"))
        content.extend(line)
    root.remove(source_row)
    root.append(table)


@lru_cache(maxsize=1)
def _inline_parser() -> Any:
    """Use maintained delimiter rules, without enabling Markdown layout or HTML."""

    try:
        from markdown_it import MarkdownIt
        from mdit_py_plugins.dollarmath import dollarmath_plugin
        from mdit_py_plugins.texmath import texmath_plugin
    except ImportError as exc:
        raise EquationRendererUnavailable(
            "markdown-it-py and mdit-py-plugins are required for inline equations"
        ) from exc
    return (
        MarkdownIt("zero").enable(["escape", "backticks"]).disable("text_join")
        .use(texmath_plugin, delimiters="brackets")
        .use(dollarmath_plugin, allow_space=False, allow_digits=False)
    )


def _inline_tokens(value: str) -> list[Any]:
    if r"\(" not in value and "$" not in value:
        return []
    tokens = _inline_parser().parseInline(value)[0].children or []
    return tokens if any(token.type == "math_inline" for token in tokens) else []


def _token_source(token: Any) -> str:
    """Keep plain copy literal when using the tokenizer solely for math spans."""

    if token.type == "math_inline":
        closing = r"\)" if token.markup == r"\(" else token.markup
        return token.markup + token.content + closing
    if token.type == "text_special":
        return token.markup
    if token.type == "code_inline":
        return token.markup + token.content + token.markup
    return token.content


def contains_inline_math(value: str) -> bool:
    """Return whether maintained delimiter rules recognize inline mathematics."""

    return bool(_inline_tokens(value))


def validate_inline_math(value: str) -> None:
    """Require every explicitly delimited inline expression to render as MathML."""

    for token in _inline_tokens(value):
        if token.type == "math_inline":
            latex_to_mathml(token.content, attributes={"display": "inline"})


def visible_inline_math_text(value: str) -> str:
    """Return prose plus rendered math glyph text for physical-size estimates."""

    tokens = _inline_tokens(value)
    if not tokens:
        return value
    visible: list[str] = []
    for token in tokens:
        if token.type == "math_inline":
            rendered = latex_to_mathml(token.content, attributes={"display": "inline"})
            visible.append("".join(ElementTree.fromstring(rendered).itertext()))
        else:
            visible.append(_token_source(token))
    return "".join(visible)


def render_inline_math(value: str) -> str:
    """Escape prose and render only explicitly delimited source mathematics."""

    tokens = _inline_tokens(value)
    if not tokens:
        return escape(value)
    rendered: list[str] = []
    for token in tokens:
        if token.type == "math_inline":
            try:
                rendered.append(latex_to_mathml(token.content, attributes={
                    "display": "inline", "data-latex": token.content,
                }))
                continue
            except EquationSyntaxError:
                # Unconvertible copy remains visible; it is not a new draft gate.
                pass
        rendered.append(escape(_token_source(token)))
    return "".join(rendered)


__all__ = [
    "MATHML_NAMESPACE",
    "EquationRendererUnavailable",
    "EquationSyntaxError",
    "contains_inline_math",
    "latex_to_mathml",
    "render_inline_math",
    "validate_inline_math",
    "visible_inline_math_text",
]


def _main() -> int:
    """Expose the same converter to native file-editing hosts, without a model call."""

    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latex-file", type=Path, required=True,
                        help="UTF-8 file containing one exact source LaTeX expression")
    parser.add_argument("--display", choices=("block", "inline"), default="block")
    args = parser.parse_args()
    try:
        latex = args.latex_file.read_text(encoding="utf-8-sig").strip()
        attributes = {"display": args.display, "data-latex": latex}
        if args.display == "block":
            attributes["data-content-role"] = "equation"
        rendered = latex_to_mathml(latex, attributes=attributes)
    except (OSError, UnicodeError, EquationRendererUnavailable, EquationSyntaxError) as exc:
        parser.exit(2, f"{exc}\n")
    sys.stdout.buffer.write((rendered + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
