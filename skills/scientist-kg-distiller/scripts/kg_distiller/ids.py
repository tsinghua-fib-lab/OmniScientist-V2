from __future__ import annotations

import re
import unicodedata


def scientist_slug(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).casefold()
    slug = re.sub(r"[^a-z0-9\u3400-\u9fff]+", "-", normalized).strip("-")
    if not slug:
        raise ValueError("scientist name does not contain usable identifier characters")
    return slug


def scientist_key(scientist_id: str) -> str:
    key = re.sub(
        r"[^A-Za-z0-9\u3400-\u9fff]+",
        "_",
        unicodedata.normalize("NFKC", scientist_id),
    ).strip("_")
    if not key:
        raise ValueError("scientist_id does not contain usable node-id characters")
    return key


def l1_id(scientist_id: str, number: int) -> str:
    return f"l1_{scientist_key(scientist_id)}_{number:04d}"


def l2_id(scientist_id: str, category: str) -> str:
    return f"l2_{scientist_key(scientist_id)}_{category}"


def l3_id(scientist_id: str, question: str) -> str:
    return f"l3_{scientist_key(scientist_id)}_{question}"
