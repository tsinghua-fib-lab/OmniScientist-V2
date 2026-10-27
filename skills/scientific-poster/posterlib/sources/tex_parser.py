"""Source-preserving TeX structure built from pyparsing primitives."""

from __future__ import annotations

import string
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pyparsing as pp
    from pylatexenc.macrospec import LatexContextDb, MacroSpec


def control_word_parser(name: str) -> pp.ParserElement:
    """Match a named control word, including one adjacent to ordinary TeX text."""

    import pyparsing as pp

    return pp.Literal("\\" + name) + ~pp.Char(pp.alphas).leave_whitespace()


def source_tokens() -> tuple[pp.ParserElement, ...]:
    """Build independent grammars; verbatim's dynamic delimiter owns local state."""

    import pyparsing as pp

    comment = pp.Literal("%") + pp.rest_of_line.leave_whitespace()
    control = pp.Combine(pp.Literal("\\") + (pp.Word(pp.alphas) | pp.Char(pp.printables + " \t\r\n")))
    delimiter = pp.Char(pp.printables).leave_whitespace()
    closing = pp.match_previous_literal(delimiter)
    verb = pp.original_text_for(
        control_word_parser("verb") + pp.Opt("*")
        + delimiter + pp.SkipTo(closing) + closing
    )
    environment_name = pp.MatchFirst(
        [pp.Literal(name) for name in sorted(_literal_environments(), key=len, reverse=True)]
    )
    end_name = pp.match_previous_literal(environment_name)
    end_environment = pp.Literal(r"\end{") + end_name + pp.Literal("}")
    literal_environment = pp.original_text_for(
        control_word_parser("begin") + "{" + environment_name + "}"
        + pp.SkipTo(end_environment | pp.StringEnd()) + pp.Opt(end_environment)
    )
    verb = literal_environment | verb
    protected = verb | control | comment
    group = pp.original_text_for(pp.nested_expr("{", "}", ignore_expr=protected))
    # Braces protect a ']' in optional arguments, e.g. [width={f[x]}].
    option = pp.original_text_for(
        pp.nested_expr("[", "]", ignore_expr=group | protected)
    )
    return comment, control, verb, group, option


@lru_cache(maxsize=1)
def _literal_environments() -> tuple[str, ...]:
    """Use the semantic parser's literal environment definitions and starred forms."""

    from pylatexenc.latexwalker import get_default_latex_context_db
    from pylatexenc.macrospec import VerbatimArgsParser

    return tuple(
        spec.environmentname + suffix
        for spec in get_default_latex_context_db().iter_environment_specs()
        if isinstance(spec.args_parser, VerbatimArgsParser)
        for suffix in ("", "*")
    )


def without_comments(source: str) -> str:
    """Remove TeX comments while retaining escaped percent signs and verbatim."""

    comment, control, verb, _, _ = source_tokens()
    return (verb | control | comment.suppress()).parse_with_tabs().transform_string(source)


@dataclass(frozen=True)
class Command:
    name: str
    argument: str
    start: int
    end: int
    options: str = ""


def commands(source: str, names: tuple[str, ...], *, bare: bool = False) -> Iterator[Command]:
    """Locate commands and their balanced argument, skipping comments and literals."""

    import pyparsing as pp

    comment, control, verb, group, option = source_tokens()
    name = pp.MatchFirst([
        control_word_parser(value)("command") for value in names
    ])
    gap = pp.ZeroOrMore(comment).suppress()
    argument = group.copy().add_parse_action(lambda t: t[0][1:-1])
    if bare:
        argument = argument | pp.Word(pp.alphanums + "_./-")
    target = (
        name + pp.Opt("*").suppress() + gap
        + pp.Opt(option)("options") + gap + pp.Group(argument)("argument")
    )
    scanner = (verb.suppress() | target | (comment | control).suppress()).parse_with_tabs()
    for tokens, start, end in scanner.scan_string(source):
        if tokens:
            options = tokens.options[0][1:-1] if tokens.options else ""
            yield Command(tokens.command[0][1:], tokens.argument[0], start, end, options)


@dataclass(frozen=True)
class Environment:
    name: str
    body: str
    start: int
    end: int


def environments(source: str, names: tuple[str, ...]) -> Iterator[Environment]:
    """Pair explicit environment delimiters while preserving nested source bytes."""

    stack: list[Command] = []
    found: list[Environment] = []
    for command in commands(source, ("begin", "end")):
        if command.name == "begin":
            stack.append(command)
        elif stack and stack[-1].argument == command.argument:
            opening = stack.pop()
            if command.argument in names:
                found.append(Environment(
                    command.argument, source[opening.end:command.start], opening.start, command.end,
                ))
    yield from sorted(found, key=lambda item: item.start)


def remove_commands(source: str, names: tuple[str, ...]) -> str:
    """Remove only complete named commands and their balanced argument."""

    parts: list[str] = []
    previous = 0
    for command in commands(source, names):
        parts.extend((source[previous:command.start], ""))
        previous = command.end
    parts.append(source[previous:])
    return "".join(parts)


def text_contexts() -> tuple[LatexContextDb, LatexContextDb]:
    """Share the library's text-argument semantics with extraction consumers."""
    from pylatexenc import latex2text, latexwalker, macrospec

    parser_context = latexwalker.get_default_latex_context_db()
    text_context = latex2text.get_default_latex_context_db()
    text_context.add_context_category(
        "poster-text-arguments",
        macros=[
            latex2text.MacroTextSpec(spec.macroname, discard=False)
            for spec in parser_context.iter_macro_specs()
            if is_text_wrapper(spec) and text_context.get_macro_spec(spec.macroname) is None
        ],
    )
    # Preserve the existing identity separators while the library owns text
    # commands, accent composition, grouping and literal argument semantics.
    parser_context.add_context_category(
        "poster-identity", macros=[macrospec.MacroSpec("and", "")], prepend=True,
    )
    text_context.add_context_category(
        "poster-identity",
        macros=[
            latex2text.MacroTextSpec("and", ", "),
            latex2text.MacroTextSpec("\\", ", "),
            *(
                latex2text.MacroTextSpec(
                    spec.macroname, lambda node: node.nodeargd.verbatim_text,
                )
                for spec in parser_context.iter_macro_specs()
                if isinstance(spec.args_parser, macrospec.VerbatimArgsParser)
            ),
        ],
        prepend=True,
    )
    return parser_context, text_context


def is_text_wrapper(spec: MacroSpec | None) -> bool:
    """Recognize library-declared commands whose sole argument stays in text mode."""
    return (
        spec is not None
        and getattr(spec.args_parser, "argspec", None) == "{"
        and getattr(spec.args_parser, "args_math_mode", None) == [False]
    )


def plain_text(source: str) -> str:
    """Convert known TeX text semantics, raising when source interpretation is required."""
    from pylatexenc import latex2text, latexwalker, macrospec

    parser_context, text_context = text_contexts()
    try:
        nodes, _, _ = latexwalker.LatexWalker(
            source, latex_context=parser_context, tolerant_parsing=False,
        ).get_latex_nodes()
    except latexwalker.LatexWalkerParseError as exc:
        raise ValueError("TeX text requires source interpretation") from exc

    pending = list(nodes)
    while pending:
        node = pending.pop()
        if node.isNodeType(latexwalker.LatexMacroNode):
            spec = text_context.get_macro_spec(node.macroname)
            if spec is None or (spec.discard and spec.simplify_repl is None):
                raise ValueError(f"TeX command requires source interpretation: \\{node.macroname}")
            if isinstance(node.nodeargd, macrospec.ParsedVerbatimArgs):
                continue
            pending.extend(item for item in node.nodeargd.argnlist if item is not None)
        elif node.isNodeType(latexwalker.LatexGroupNode):
            pending.extend(node.nodelist)
        elif node.isNodeType(latexwalker.LatexMathNode) or node.isNodeType(latexwalker.LatexEnvironmentNode):
            raise ValueError("TeX math or environments require source interpretation")
    text = latex2text.LatexNodes2Text(
        latex_context=text_context, strict_latex_spaces=True,
    ).nodelist_to_text(nodes)
    return ",".join(part.rstrip() for part in " ".join(text.split()).split(",")).strip(" ,")


def is_control_word(value: str) -> bool:
    """Recognize one ASCII TeX control word emitted literally by a math backend."""

    return value.startswith("\\") and bool(value[1:]) and all(
        char in string.ascii_letters for char in value[1:]
    )


def math_body_parser(*, display: bool = True) -> pp.ParserElement:
    """Parse delimited math and return its exact body, protecting grouped delimiters."""

    import pyparsing as pp

    comment, control, verb, group, _ = source_tokens()
    pairs = [(r"\(", r"\)"), ("$", "$")]
    if display:
        pairs = [("$$", "$$"), (r"\[", r"\]")] + pairs
    bodies = []
    for opening, closing in pairs:
        bodies.append(
            pp.Suppress(opening)
            + pp.SkipTo(closing, ignore=group | verb | comment | (~pp.Literal(closing) + control))
            + pp.Suppress(closing)
        )
    return pp.MatchFirst(bodies).parse_with_tabs()


def without_math(source: str) -> str:
    """Replace complete inline and display math spans with a word-separating space."""

    comment, control, verb, _, _ = source_tokens()
    math = math_body_parser().set_parse_action(lambda: " ")
    return (verb | comment | math | control).parse_with_tabs().transform_string(source)


def strip_layout_skips(source: str) -> str:
    """Remove explicit point-valued vskip commands from source math."""

    import pyparsing as pp

    comment, control, verb, _, _ = source_tokens()
    number = pp.Combine(
        pp.Opt(pp.one_of("+ -"))
        + ((pp.Word(pp.nums) + pp.Opt("." + pp.Opt(pp.Word(pp.nums))))
           | (pp.Literal(".") + pp.Word(pp.nums)))
    )
    skip = (
        control_word_parser("vskip") + number
        + pp.CaselessKeyword("pt", ident_chars=pp.alphas)
    ).suppress()
    return (verb | skip | comment | control).parse_with_tabs().transform_string(source)
