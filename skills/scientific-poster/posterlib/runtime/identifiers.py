"""Value checks for poster provenance and DOM identifiers."""

from __future__ import annotations

import string


def is_hex(value: object, length: int) -> bool:
    """Recognize an exact lowercase hexadecimal identity."""

    return isinstance(value, str) and len(value) == length and all(
        character in "0123456789abcdef" for character in value
    )


def is_sha256(value: object) -> bool:
    """Recognize the SHA-256 representation used by source manifests."""

    return is_hex(value, 64)


def is_poster_id(value: object) -> bool:
    """Recognize one bounded selectable poster element id."""

    return is_identifier(value) and len(value) <= 200


def is_identifier(value: object) -> bool:
    """Recognize a selectable element or captured scene-object identifier."""

    return isinstance(value, str) and bool(value) and (
        value[0] in string.ascii_letters + string.digits
        and all(character in string.ascii_letters + string.digits + "_.:-" for character in value)
    )


def is_module_id(value: object) -> bool:
    """Recognize a lowercase kebab-case scientific module id."""

    return isinstance(value, str) and bool(value) and value[0] in string.ascii_lowercase and all(
        part and all(character in string.ascii_lowercase + string.digits for character in part)
        for part in value.split("-")
    )


def is_asset_token(value: object) -> bool:
    """Recognize a positive decimal transient asset reference."""

    if not isinstance(value, str) or not value.startswith("asset://"):
        return False
    number = value.removeprefix("asset://")
    return bool(number) and number[0] in "123456789" and all(c in string.digits for c in number)
