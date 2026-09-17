"""Convert source LaTeX into semantic MathML through one maintained backend."""

from __future__ import annotations

from collections.abc import Mapping
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
        converted = convert(latex)
        root = ElementTree.fromstring(converted)
    except Exception as exc:
        raise EquationSyntaxError(f"LaTeX-to-MathML conversion failed: {exc}") from exc
    if _local_name(root.tag) != "math":
        raise EquationSyntaxError("LaTeX converter did not return one <math> root")

    root.set("display", "block")
    for name, value in (attributes or {}).items():
        root.set(str(name), str(value))
    return ElementTree.tostring(root, encoding="unicode", short_empty_elements=False)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


__all__ = [
    "MATHML_NAMESPACE",
    "EquationRendererUnavailable",
    "EquationSyntaxError",
    "latex_to_mathml",
]
