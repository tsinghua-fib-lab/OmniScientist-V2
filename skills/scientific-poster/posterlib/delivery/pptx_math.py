"""Convert browser MathML into native PowerPoint Office Math markup."""

from __future__ import annotations

import re
from xml.etree import ElementTree

A14_NAMESPACE = "http://schemas.microsoft.com/office/drawing/2010/main"
MATH_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/math"


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
    paragraph = ElementTree.SubElement(wrapper, f"{{{MATH_NAMESPACE}}}oMathPara")
    properties = ElementTree.SubElement(
        paragraph, f"{{{MATH_NAMESPACE}}}oMathParaPr"
    )
    ElementTree.SubElement(
        properties,
        f"{{{MATH_NAMESPACE}}}jc",
        {f"{{{MATH_NAMESPACE}}}val": justification},
    )
    paragraph.append(converted[0])
    return ElementTree.tostring(wrapper, encoding="unicode")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _repair_converter_omml(value: str) -> str:
    """Repair the malformed group-character property close emitted for ``mover``."""

    return re.sub(
        r"(<m:groupChrPr>.*?)</m:groupChr>(?=<m:e>)",
        r"\1</m:groupChrPr>",
        value,
        flags=re.DOTALL,
    )


def _complete_omml_structure(root: ElementTree.Element) -> None:
    """Add required empty slots omitted by the MathML converter."""

    namespace = f"{{{MATH_NAMESPACE}}}"
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
    return ElementTree.tostring(root, encoding="unicode")


__all__ = [
    "A14_NAMESPACE",
    "MATH_NAMESPACE",
    "NativeMathError",
    "build_text_math_xml",
]
