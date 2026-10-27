"""Bibliographic extraction from explicit TeX declarations and author-block structure."""

from __future__ import annotations

from email import policy
from email.errors import HeaderParseError
from typing import Any

from . import tex_parser


def extract_identity(source: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Read certain fields independently and retain diagnostics for unresolved declarations."""
    source = "\n".join(
        line for line in source.splitlines()
        if not (line.startswith("[LaTeX file: ") and line.endswith("]"))
    )
    identity: dict[str, Any] = {"title": "", "authors": "", "affiliations": []}
    diagnostics: dict[str, str] = {}
    fields = {"title": "title", "author": "authors", "affiliation": "affiliations"}
    declarations_by_name: dict[str, list[tex_parser.Command]] = {
        name: [] for name in fields
    }
    for declaration in tex_parser.commands(source, tuple(declarations_by_name)):
        declarations_by_name[declaration.name].append(declaration)
    for name, declarations in declarations_by_name.items():
        try:
            if name != "affiliation" and len(declarations) > 1:
                raise ValueError(f"Repeated \\{name} declarations require document-class semantics")
            if name == "author" and declarations:
                authors, affiliations, notes, note_error = _author_block(declarations[0].argument)
                identity["authors"] = authors
                identity["affiliations"].extend(affiliations)
                if notes and any(
                    next(tex_parser.commands(title.argument, ("thanks", "footnote")), None)
                    for title in declarations_by_name["title"]
                ):
                    note_error = "Title and author notes share document-class footnote numbering"
                if note_error:
                    diagnostics["author_notes"] = (
                        "LaTeX paper identity (author_notes) requires author-block interpretation: "
                        + note_error
                    )
                elif notes:
                    identity["author_notes"] = notes
            else:
                values = [tex_parser.plain_text(command.argument) for command in declarations]
                if name == "title":
                    identity["title"] = next(iter(values), "")
                elif name == "affiliation":
                    identity["affiliations"].extend(value for value in values if value)
        except ValueError as exc:
            if name == "affiliation":
                identity["affiliations"] = []
            diagnostics[fields[name]] = (
                f"LaTeX paper identity ({name}) requires full-source interpretation: {exc}"
            )
    identity["affiliations"] = list(dict.fromkeys(identity["affiliations"]))
    return identity, diagnostics


def _author_block(source: str) -> tuple[str, list[str], list[dict[str, Any]], str]:
    """Keep note references attached to names while separating affiliations and contacts."""
    from pylatexenc import latexwalker, macrospec

    context, _ = tex_parser.text_contexts()
    context.add_context_category("poster-author-block", macros=[
        macrospec.MacroSpec(name, arguments)
        for name, arguments in (
            ("textsuperscript", "{"), ("thanks", "{"), ("footnote", "[{"),
            ("footnotemark", "["), ("footnotetext", "[{"),
            ("space", ""), ("detokenize", "{"),
        )
    ], prepend=True)
    try:
        nodes, _, _ = latexwalker.LatexWalker(
            source, latex_context=context, tolerant_parsing=False,
        ).get_latex_nodes()
    except latexwalker.LatexWalkerParseError as exc:
        raise ValueError("TeX author block requires source interpretation") from exc

    note_counter = 0
    note_text: dict[int, str] = {}
    note_error = ""

    def collect(nodes: list[Any]) -> list[tuple[str, str]]:
        nonlocal note_counter, note_error
        parts: list[tuple[str, str]] = []
        for node in nodes:
            if node.isNodeType(latexwalker.LatexGroupNode):
                group = collect(node.nodelist)
                text = "".join(text for _, text in group)
                if (
                    "@" in text and all(kind in {"text", "contact"} for kind, _ in group)
                    and _contact_only(tex_parser.plain_text(text))
                ):
                    parts.append(("contact", text))
                else:
                    parts.extend(group)
            elif node.isNodeType(latexwalker.LatexMacroNode):
                name = node.macroname
                if name in {"\\", "and"}:
                    parts.append(("boundary", name))
                elif name in {"thanks", "footnote", "footnotemark", "footnotetext"}:
                    args = node.nodeargd.argnlist
                    option = None if name == "thanks" else args[0]
                    try:
                        if option is not None:
                            number = int(tex_parser.plain_text(option.latex_verbatim()[1:-1]))
                        else:
                            if name != "footnotetext":
                                note_counter += 1
                            number = note_counter
                        if name != "footnotemark":
                            text = tex_parser.plain_text(args[-1].latex_verbatim()[1:-1])
                            if not text or (number in note_text and note_text[number] != text):
                                raise ValueError("Author note definition is empty or conflicting")
                            note_text[number] = text
                        if name != "footnotetext":
                            parts.append(("note_ref", str(number)))
                    except ValueError as exc:
                        note_error = str(exc)
                elif name == "textsuperscript":
                    parts.append(("mark", ""))
                elif name == "space":
                    parts.append(("text", " "))
                elif name == "detokenize":
                    parts.append(("text", node.nodeargd.argnlist[0].latex_verbatim()[1:-1]))
                elif tex_parser.is_text_wrapper(context.get_macro_spec(name)):
                    parts.extend(collect(node.nodeargd.argnlist))
                else:
                    parts.append(("text", node.latex_verbatim()))
            else:
                parts.append(("text", node.latex_verbatim()))
        return parts

    rows: list[tuple[str, list[tuple[str, str]]]] = [("", [])]
    for kind, text in collect(nodes):
        if kind == "boundary":
            rows.append((text, []))
        else:
            rows[-1][1].append((kind, text))
    authors: list[str] = []
    affiliations: list[str] = []
    note_owners: dict[int, list[str]] = {}
    for boundary, row in rows:
        for index, (kind, _) in enumerate(row):
            if kind != "contact":
                continue
            for neighbor, edge in ((index - 1, -1), (index + 1, 0)):
                if 0 <= neighbor < len(row) and row[neighbor][0] == "text":
                    adjacent = row[neighbor][1]
                    if adjacent and not adjacent[edge].isspace():
                        raise ValueError("Contact fragment boundaries require source interpretation")
        row = [(kind, " " if kind == "contact" else text) for kind, text in row]
        meaningful = [(kind, text) for kind, text in row if kind == "mark" or text.strip()]
        if not meaningful:
            continue
        rendered = tex_parser.plain_text("".join(text for kind, text in row if kind != "note_ref"))
        if "@" in rendered:
            if _contact_only(rendered):
                continue
            raise ValueError("Mixed author and contact text requires source interpretation")
        if boundary == "\\" and not any(kind == "mark" for kind, _ in row):
            raise ValueError("Unmarked author-block lines require source interpretation")
        destination = affiliations if meaningful[0][0] == "mark" else authors
        parts: list[str] = []
        current_author = ""
        for kind, text in [*row, ("mark", "")]:
            if kind in {"mark", "note_ref"}:
                value = tex_parser.plain_text("".join(parts))
                if value:
                    destination.append(value)
                    current_author = value if destination is authors else ""
                parts = []
                if kind == "note_ref":
                    if not current_author or "," in current_author:
                        note_error = "Author note needs an unambiguous preceding name"
                    else:
                        note_owners.setdefault(int(text), []).append(current_author)
            else:
                parts.append(text)
    if note_owners.keys() - note_text.keys():
        note_error = "Author note reference has no definition in the author block"
    notes = [
        {"authors": list(dict.fromkeys(note_owners[number])), "text": text}
        for number, text in note_text.items() if number in note_owners
    ] if not note_error else []
    return ", ".join(authors), affiliations, notes, note_error


def _contact_only(value: str) -> bool:
    """Accept complete address lists whose parse preserves every non-space character."""
    try:
        header = policy.default.header_factory("To", value)
    except (ValueError, HeaderParseError):
        return False
    addresses = header.addresses
    return bool(addresses) and not header.defects and all(
        not address.display_name and address.username and address.domain
        for address in addresses
    ) and "".join(value.split()) == "".join(
        ",".join(address.addr_spec for address in addresses).split()
    )
