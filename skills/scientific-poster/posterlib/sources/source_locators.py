"""Resolve literal source excerpts and numbered caption descriptions together."""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from typing import Any

from . import tex_parser
from .paper_extraction import _CARRIER_LABELS, _parse_carrier_label


def text_key(value: str) -> str:
    """Fold extracted whitespace and Unicode presentation forms for source lookup."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def excerpt_exists(source: str, excerpt: str) -> bool:
    """Retain glued PDF caption starts while requiring a complete final token."""
    if not excerpt:
        return False
    start = source.find(excerpt)
    while start >= 0:
        end = start + len(excerpt)
        if end == len(source) or not (excerpt[-1].isalnum() and source[end].isalnum()):
            return True
        start = source.find(excerpt, start + 1)
    return False


class SourceLocators:
    """Index source structure once for all modules in a proposed evidence plan."""

    def __init__(self, source: str) -> None:
        self.raw_source = source
        self.source = text_key(source)
        self.excerpts: list[str] = []
        self.captions: dict[tuple[str, int], list[str]] = defaultdict(list)
        self._indexed = False

    def _index(self) -> None:
        source = self.raw_source
        if next(tex_parser.commands(source, ("documentclass",)), None) is not None:
            self._latex(source)
        else:
            for kind in ("Figure", "Table", "Algorithm"):
                for parsed, _, end in _CARRIER_LABELS[kind, True].scan_string(self.source):
                    self.captions[kind, int(parsed.value.number)].append(self.source[end:].lstrip())
        self._indexed = True

    def _latex(self, source: str) -> None:
        for command in tex_parser.commands(source, ("section", "subsection", "subsubsection", "label")):
            try:
                self.excerpts.append(text_key(tex_parser.plain_text(command.argument)))
            except ValueError:
                continue
        captions, uncertain = _caption_structure(source)
        for kind in ("Figure", "Table", "Algorithm"):
            environment = kind.lower()
            # Custom counters require the printed identifier or an explicit TeX label.
            customized = any(
                command.argument == environment
                for command in tex_parser.commands(source, (
                    "setcounter", "addtocounter", "stepcounter", "refstepcounter",
                    "counterwithin", "counterwithout", "numberwithin", "captionof",
                ))
            ) or "\\the" + environment in source or environment in uncertain
            number = 0
            for owner, caption in captions:
                if owner != environment:
                    continue
                number += 1
                prefix = _caption_prefix(caption)
                if prefix:
                    self.excerpts.append(prefix)
                if not customized:
                    self.captions[kind, number].append(prefix)

    def contains(self, label: str) -> bool:
        """Require a description to match the caption belonging to its identifier."""
        key = text_key(label)
        if excerpt_exists(self.source, key):
            return True
        if not self._indexed:
            self._index()
        for kind in ("Figure", "Table", "Algorithm"):
            parsed = _parse_carrier_label(key, kind)
            if parsed is None:
                continue
            number, description = parsed
            description = description.lstrip(".:：-–— ").rstrip(".")
            return any(
                not description or (
                    caption.startswith(description) and excerpt_exists(caption, description)
                )
                for caption in self.captions.get((kind, number), [])
            )
        return any(excerpt_exists(excerpt, key) for excerpt in self.excerpts)


def _caption_structure(source: str) -> tuple[list[tuple[str, str]], set[str]]:
    """Associate captions with supported counter scopes without expanding TeX programs."""
    from pylatexenc import latexwalker, macrospec

    counters = {"figure", "table", "algorithm"}
    subcounters = {"subfigure", "subtable"}
    layout_scopes = {"document", "center", "flushleft", "flushright", "minipage"}
    context, _ = tex_parser.text_contexts()
    context.add_context_category("poster-captions", macros=[
        macrospec.MacroSpec("caption", "*[{"),
        macrospec.MacroSpec("captionof", "*{[{"),
    ], environments=[
        macrospec.EnvironmentSpec(name, "[[[{")
        for name in ("minipage", "subfigure", "subtable")
    ], prepend=True)
    try:
        nodes, _, _ = latexwalker.LatexWalker(
            source, latex_context=context, tolerant_parsing=False,
        ).get_latex_nodes()
    except latexwalker.LatexWalkerParseError:
        return [], counters

    captions: list[tuple[str, str]] = []
    uncertain: set[str] = set()

    def visit(nodes: list[Any], scopes: tuple[str, ...] = ()) -> None:
        for node in nodes:
            if node.isNodeType(latexwalker.LatexEnvironmentNode):
                environment = node.environmentname.removesuffix("*")
                start = len(captions)
                visit(node.nodelist, (*scopes, environment))
                owners = {owner for owner, _ in captions[start:]}
                if environment in counters and "sub" + environment in owners and environment not in owners:
                    uncertain.add(environment)
            elif node.isNodeType(latexwalker.LatexGroupNode):
                visit(node.nodelist, scopes)
            elif node.isNodeType(latexwalker.LatexMacroNode):
                owner = next((scope for scope in reversed(scopes) if scope not in layout_scopes), "")
                enclosing = next((scope for scope in reversed(scopes) if scope in counters), "")
                if node.macroname == "caption":
                    if node.nodeargd.argnlist[0] is not None:
                        continue
                    if owner in counters | subcounters and all(
                        scope in counters | subcounters | layout_scopes for scope in scopes
                    ):
                        argument = node.nodeargd.argnlist[-1]
                        text = argument.latex_verbatim()
                        if argument.isNodeType(latexwalker.LatexGroupNode):
                            text = text[1:-1]
                        captions.append((owner, text))
                    else:
                        uncertain.update({enclosing} if enclosing else counters)
                else:
                    if owner in counters | subcounters and context.get_macro_spec(node.macroname) is None:
                        uncertain.update({enclosing} if enclosing else counters)
                    # Macro arguments have semantics owned by that macro; a caption
                    # in a definition or an unsupported wrapper cannot advance our index.
                    visit(
                        [item for item in node.nodeargd.argnlist if item is not None],
                        (*scopes, "\\" + node.macroname),
                    )

    visit(nodes)
    return captions, uncertain


def _caption_prefix(source: str) -> str:
    """Keep the readable caption prefix when later notation needs source interpretation."""
    from pylatexenc import latexwalker

    try:
        return text_key(tex_parser.plain_text(source))
    except ValueError:
        context, _ = tex_parser.text_contexts()
        try:
            nodes, _, _ = latexwalker.LatexWalker(
                source, latex_context=context, tolerant_parsing=False,
            ).get_latex_nodes()
        except latexwalker.LatexWalkerParseError:
            return ""
        end = 0
        for node in nodes:
            try:
                tex_parser.plain_text(node.latex_verbatim())
            except ValueError:
                break
            end = node.pos + node.len
        return text_key(tex_parser.plain_text(source[:end]))
