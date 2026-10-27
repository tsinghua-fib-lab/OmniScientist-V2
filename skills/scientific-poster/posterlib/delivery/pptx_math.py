"""Convert browser MathML into native PowerPoint Office Math markup."""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

from lxml import etree

A14_NAMESPACE = "http://schemas.microsoft.com/office/drawing/2010/main"
MATH_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/math"
DRAWING_NAMESPACE = "http://schemas.openxmlformats.org/drawingml/2006/main"


class NativeMathError(ValueError):
    """MathML cannot be represented as a native PowerPoint equation."""


def build_text_math_xml(mathml: str, *, align: str) -> str:
    """Return one ``a14:m`` element containing converted OMML."""

    source = mathml.strip()
    if not source or "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
        raise NativeMathError(
            "equation MathML must be a standalone safe <math> element"
        )
    try:
        math_root = ElementTree.fromstring(source)
    except ElementTree.ParseError as exc:
        raise NativeMathError(f"equation MathML is invalid: {exc}") from exc
    if _local_name(math_root.tag) != "math":
        raise NativeMathError("equation MathML root must be <math>")
    try:
        import mathml2omml
    except ImportError as exc:
        raise RuntimeError(
            "mathml2omml is required for native PowerPoint equations"
        ) from exc
    try:
        converter_source = _normalize_converter_mathml(math_root)
        omml = _repair_converter_omml(mathml2omml.convert(converter_source))
        converted = ElementTree.fromstring(
            f'<root xmlns:m="{MATH_NAMESPACE}">{omml}</root>'
        )
        _complete_omml_structure(converted[0])
    except Exception as exc:
        raise NativeMathError(f"MathML to OMML conversion failed: {exc}") from exc
    if len(converted) != 1 or _local_name(converted[0].tag) != "oMath":
        raise NativeMathError("MathML converter did not return one m:oMath element")
    justification = {"left": "left", "right": "right"}.get(align, "center")
    ElementTree.register_namespace("a14", A14_NAMESPACE)
    ElementTree.register_namespace("m", MATH_NAMESPACE)
    wrapper = ElementTree.Element(f"{{{A14_NAMESPACE}}}m")
    container = wrapper
    if math_root.get("display", "inline") == "block":
        container = ElementTree.SubElement(wrapper, f"{{{MATH_NAMESPACE}}}oMathPara")
        properties = ElementTree.SubElement(
            container, f"{{{MATH_NAMESPACE}}}oMathParaPr"
        )
        ElementTree.SubElement(
            properties,
            f"{{{MATH_NAMESPACE}}}jc",
            {f"{{{MATH_NAMESPACE}}}val": justification},
        )
    container.append(converted[0])
    return ElementTree.tostring(wrapper, encoding="unicode")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def complete_math_paragraphs(slide: etree._Element) -> None:
    """Keep source-inline math inline when it occupies a shape on its own.

    Office promotes paragraph-boundary math to display style. A zero-width word
    joiner outside the math zone supplies its inline text context. DrawingML then
    owns paragraph alignment; source display zones retain their math paragraph.
    """

    drawing, math = f"{{{DRAWING_NAMESPACE}}}", f"{{{MATH_NAMESPACE}}}"
    for body in slide.iter(f"{drawing}bodyPr"):
        for tag in ("spAutoFit", "normAutofit", "noAutofit"):
            for child in body.findall(f"{drawing}{tag}"):
                body.remove(child)
        # The captured layout owns the frame, including adjacent text positions.
        body.insert(0, etree.Element(f"{drawing}noAutofit"))
    for paragraph in slide.iter(f"{drawing}p"):
        content = [child for child in paragraph
                   if child.tag not in {f"{drawing}pPr", f"{drawing}endParaRPr"}]
        if len(content) != 1 or content[0].tag != f"{{{A14_NAMESPACE}}}m":
            continue
        zone = content[0]
        expression = zone.find(f"{math}oMath")
        if expression is None:
            continue
        context = etree.Element(f"{drawing}r")
        etree.SubElement(context, f"{drawing}t").text = "\u2060"
        paragraph.insert(paragraph.index(zone), context)


def complete_math_text_sizes(shape: etree._Element, item: dict[str, Any]) -> None:
    """Preserve literal text while supplying the script size omitted by m:nor.

    Office applies its automatic argument sizing to mathematical runs. Normal
    text keeps its declared size. Capture supplies the browser-computed size
    for each literal token, including inherited MathML and CSS styles.
    """

    math, drawing = f"{{{MATH_NAMESPACE}}}", f"{{{DRAWING_NAMESPACE}}}"
    parts = [part for part in item.get("runs", [item]) if "mathml" in part]
    expressions = list(shape.iter(f"{math}oMath"))
    for expression, part in zip(expressions, parts, strict=True):
        runs = [run for run in expression.iter(f"{math}r")
                if run.find(f"{math}rPr/{math}nor") is not None]
        if not runs:
            continue
        sizes = part.get("normal_text_sizes_pt")
        if sizes is None:
            continue
        if len(runs) != len(sizes):
            raise NativeMathError("MathML normal-text arguments changed during OMML conversion")
        for run, size in zip(runs, sizes, strict=True):
            run.find(f"{drawing}rPr").set("sz", str(round(size * 100)))


def _repair_converter_omml(value: str) -> str:
    """Recover the backend's group-character property close through an XML parser."""

    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(f'<root xmlns:m="{MATH_NAMESPACE}">{value}</root>', parser)
    for error in parser.error_log:
        if not (
            error.type_name == "ERR_TAG_NAME_MISMATCH"
            and error.message.startswith("Opening and ending tag mismatch: groupChrPr line ")
            and error.message.endswith(" and groupChr")
        ):
            raise NativeMathError(f"MathML converter returned invalid OMML: {error.message}")
    return "".join(etree.tostring(child, encoding="unicode") for child in root)


def _complete_omml_structure(root: ElementTree.Element) -> None:
    """Complete converter slots and retain mathematical base alignment."""

    namespace = f"{{{MATH_NAMESPACE}}}"
    # Empty MathML grouping has no operand. Retain the enclosing argument slots.
    for parent in reversed(list(root.iter())):
        for child in list(parent):
            if child.tag == f"{namespace}box" and _empty_group_argument(child):
                parent.remove(child)
    for matrix in root.iter(f"{namespace}m"):
        cells = matrix.findall(f"{namespace}mr/{namespace}e")
        if not any(_empty_group_argument(cell) for cell in cells):
            continue
        properties = matrix.find(f"{namespace}mPr")
        if properties is None:
            properties = ElementTree.Element(f"{namespace}mPr")
            matrix.insert(0, properties)
        hidden = properties.find(f"{namespace}plcHide")
        if hidden is None:
            hidden = ElementTree.Element(f"{namespace}plcHide")
            # Matrix properties place plcHide after baseJc and before spacing.
            position = int(properties.find(f"{namespace}baseJc") is not None)
            properties.insert(position, hidden)
        hidden.set(f"{namespace}val", "1")
    for properties in root.iter(f"{namespace}groupChrPr"):
        position = properties.find(f"{namespace}pos")
        if position.get(f"{namespace}val") == "top":
            # Office's implicit top justification puts the base below the baseline.
            ElementTree.SubElement(
                properties, f"{namespace}vertJc", {f"{namespace}val": "bot"},
            )
    for nary in root.iter(f"{namespace}nary"):
        properties = nary.find(f"{namespace}naryPr")
        for name in ("sub", "sup"):
            argument = nary.find(f"{namespace}{name}")
            # Empty MathML rows become nested box/e groups in the required slots.
            # Fractions, scripts, and other mathematical objects retain their slots.
            if not _empty_group_argument(argument):
                continue
            hide_tag = f"{namespace}{name}Hide"
            if properties.find(hide_tag) is None:
                ElementTree.SubElement(properties, hide_tag, {f"{namespace}val": "1"})
    for radical in root.iter(f"{namespace}rad"):
        if radical.find(f"{namespace}deg") is not None:
            continue
        expression = radical.find(f"{namespace}e")
        position = list(radical).index(expression)
        properties = ElementTree.Element(f"{namespace}radPr")
        ElementTree.SubElement(
            properties,
            f"{namespace}degHide",
            {f"{namespace}val": "1"},
        )
        radical.insert(position, properties)
        radical.insert(position + 1, ElementTree.Element(f"{namespace}deg"))


def _empty_group_argument(argument: ElementTree.Element) -> bool:
    """Recognize property-free empty grouping, preserving mathematical objects."""

    return not argument.attrib and not (argument.text or "").strip() and all(
        child.tag in {f"{{{MATH_NAMESPACE}}}box", f"{{{MATH_NAMESPACE}}}e"}
        and _empty_group_argument(child)
        and not (child.tail or "").strip()
        for child in argument
    )


def _normalize_converter_mathml(root: ElementTree.Element) -> str:
    """Adapt the parsed copy to the backend's leaf-token and namespace contracts."""

    def empty_group(node: ElementTree.Element) -> bool:
        return (
            _local_name(node.tag) == "mrow" and not node.attrib
            and not (node.text or "").strip()
            and all(empty_group(child) and not (child.tail or "").strip() for child in node)
        )

    for node in root.iter():
        # SAX conversion is namespace-unaware; the original HTML is not changed.
        if node.tag.startswith("{http://www.w3.org/1998/Math/MathML}"):
            node.tag = _local_name(node.tag)
        if (node.tag in {"mi", "mn", "mo", "mtext", "ms"} and len(node)
                and all(empty_group(child) for child in node)):
            # Preserve even an empty atom: it may own subscripts or superscripts.
            # Nonempty mathematical subtrees must never be flattened to text.
            node.text = (node.text or "") + "".join(
                "".join(child.itertext()) + (child.tail or "") for child in node
            )
            for child in list(node):
                node.remove(child)
        if node.tag == "mo" and not len(node) and (node.text or "").strip() == "|":
            # Keep conditional bars from being misread as unmatched converter fences.
            node.text = "∣"
        if node.tag in {"msub", "msup", "msubsup"} and _local_name(node[0].tag) == "mo":
            # Fixed side scripts retain their placement even for movable-limit operators.
            node[0].set("movablelimits", "false")
    _resolve_movable_limits(root, display=root.get("display") == "block")
    return ElementTree.tostring(root, encoding="unicode")


def _resolve_movable_limits(node: ElementTree.Element, *, display: bool) -> None:
    """Resolve MathML's movable limits before the converter drops displaystyle."""

    from mathml2omml import MO, op_attrs

    if node.get("displaystyle") in {"true", "false"}:
        display = node.get("displaystyle") == "true"
    if not display and node.tag in {"munder", "mover", "munderover"} and node[0].tag == "mo":
        operator = MO(dict(node[0].attrib))
        operator.set_text(node[0].text or "")
        if op_attrs(operator)["movablelimits"]:
            node.tag = {"munder": "msub", "mover": "msup", "munderover": "msubsup"}[node.tag]
            node[0].set("movablelimits", "false")
    for index, child in enumerate(node):
        # Fractions, scripts and table cells use compact style in MathML Core.
        compact = node.tag in {"mfrac", "mtd"} or (
            index > 0 and node.tag in {"msub", "msup", "msubsup", "munder", "mover", "munderover"}
        )
        _resolve_movable_limits(child, display=display and not compact)


__all__ = [
    "A14_NAMESPACE",
    "MATH_NAMESPACE",
    "NativeMathError",
    "build_text_math_xml",
    "complete_math_paragraphs",
    "complete_math_text_sizes",
]
