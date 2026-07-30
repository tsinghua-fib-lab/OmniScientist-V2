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
        converter_source = _normalize_converter_mathml(source)
        omml = _repair_converter_omml(mathml2omml.convert(converter_source))
        converted = ElementTree.fromstring(
            f'<root xmlns:m="{MATH_NAMESPACE}">{omml}</root>'
        )
    except (ElementTree.ParseError, NotImplementedError, ValueError) as exc:
        raise NativeMathError(f"MathML to OMML conversion failed: {exc}") from exc
    if len(converted) != 1 or _local_name(converted[0].tag) != "oMath":
        raise NativeMathError("MathML converter did not return one m:oMath element")
    justification = {"left": "left", "right": "right"}.get(align, "center")
    return (
        f'<a14:m xmlns:a14="{A14_NAMESPACE}" xmlns:m="{MATH_NAMESPACE}">'
        "<m:oMathPara>"
        f'<m:oMathParaPr><m:jc m:val="{justification}"/></m:oMathParaPr>'
        f"{omml}"
        "</m:oMathPara>"
        "</a14:m>"
    )


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


def _normalize_converter_mathml(value: str) -> str:
    """Keep conditional bars from being misread as unmatched converter fences."""

    return re.sub(
        r"(<(?:[A-Za-z_][\w.-]*:)?mo\b[^>]*>)\s*\|\s*(</(?:[A-Za-z_][\w.-]*:)?mo>)",
        r"\1∣\2",
        value,
    )


__all__ = [
    "A14_NAMESPACE",
    "MATH_NAMESPACE",
    "NativeMathError",
    "build_text_math_xml",
]
