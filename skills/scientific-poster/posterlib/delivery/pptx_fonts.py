"""Merge presentation-level embedded fonts through the existing OPC package."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from lxml import etree

if TYPE_CHECKING:
    from pptx.opc.package import Part
    from pptx.parts.presentation import PresentationPart

_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_VARIANTS = ("regular", "bold", "italic", "boldItalic")
_FontEntry = tuple[etree._Element, dict[str, "Part"]]


def import_embedded_fonts(source: PresentationPart, destination: PresentationPart) -> None:
    """Preserve each source font descriptor and its independently embedded variants."""

    _merge_entries(destination, _read_entries(source))


def has_embedded_regular_font(part: PresentationPart, typeface: str) -> bool:
    """Return whether the presentation already supplies the requested regular face."""

    return any(
        descriptor.get("typeface", "").casefold() == typeface.casefold() and "regular" in variants
        for descriptor, variants in _read_entries(part)
    )


def embed_regular_font(destination: PresentationPart, typeface: str, blob: bytes) -> None:
    """Register an EOT resource alongside fonts already used by native components."""

    from pptx.opc.package import Part

    package = destination.package
    resource = Part(package.next_partname("/ppt/fonts/font%d.fntdata"),
                    "application/x-fontdata", package, blob)
    _merge_entries(destination, [(etree.Element(_P + "font", typeface=typeface),
                                  {"regular": resource})])


def _read_entries(part: PresentationPart) -> list[_FontEntry]:
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    entries = []
    for entry in part._element.findall(f"{_P}embeddedFontLst/{_P}embeddedFont"):
        descriptor = entry.find(_P + "font")
        variants = {}
        for variant in _VARIANTS:
            reference = entry.find(_P + variant)
            if reference is not None:
                relation = part.rels[reference.attrib[_RID]]
                if relation.reltype != RT.FONT:
                    raise ValueError("Embedded font reference must target a font relationship")
                variants[variant] = relation.target_part
        entries.append((descriptor, variants))
    return entries


def _merge_entries(destination: PresentationPart, incoming: list[_FontEntry]) -> None:
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.opc.package import Part

    if not incoming:
        return

    # Preflight the entire batch before adding relationships or changing the font list.
    merged: dict[str, _FontEntry] = {}
    for descriptor, variants in [*_read_entries(destination), *incoming]:
        typeface = descriptor.attrib["typeface"]
        key = typeface.casefold()
        if key not in merged:
            merged[key] = (deepcopy(descriptor), {})
        font, registered = merged[key]
        for variant, resource in variants.items():
            previous = registered.get(variant)
            if previous is not None and previous.blob != resource.blob:
                raise ValueError(f"Conflicting embedded font data for {typeface!r} ({variant})")
            registered.setdefault(variant, resource)
        for attribute, value in descriptor.attrib.items():
            if attribute not in font.attrib:
                font.set(attribute, value)

    package = destination.package
    resources = {
        (rel.target_part.content_type, rel.target_part.blob): rel.target_part
        for rel in destination.rels.values() if rel.reltype == RT.FONT
    }
    fonts = etree.Element(_P + "embeddedFontLst")
    for descriptor, variants in merged.values():
        entry = etree.SubElement(fonts, _P + "embeddedFont")
        entry.append(descriptor)
        for variant in _VARIANTS:
            if variant not in variants:
                continue
            resource = variants[variant]
            resource_key = (resource.content_type, resource.blob)
            if resource_key not in resources:
                resources[resource_key] = Part(
                    package.next_partname(f"/ppt/fonts/font%d.{resource.partname.ext}"),
                    resource.content_type, package, resource.blob,
                )
            relation = destination.relate_to(resources[resource_key], RT.FONT)
            etree.SubElement(entry, _P + variant, {_RID: relation})

    root = destination._element
    current = root.find(_P + "embeddedFontLst")
    if current is not None:
        root.replace(current, fonts)
    else:
        root.insert_element_before(fonts, "p:custShowLst", "p:photoAlbum", "p:custDataLst", "p:kinsoku",
                                   "p:defaultTextStyle", "p:modifyVerifier", "p:extLst")
    root.set("embedTrueTypeFonts", "1")
