"""Closed contracts for reusable poster components and layout policies."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from css_safety import offline_css_issues

RESOURCE_KINDS = ("component", "layout-policy")
COMPONENT_FILES = ("component.json", "fragment.html", "style.css")
LAYOUT_POLICY_FILES = ("policy.json", "guidance.md", "style.css")
MAX_MEMBER_BYTES = 1024 * 1024
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_RE = re.compile(r"^[0-9a-f]{32}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_UTC_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?Z$"
)
_ACTIVE_TAGS = {
    "animate",
    "animatemotion",
    "animatetransform",
    "audio",
    "base",
    "button",
    "details",
    "discard",
    "dialog",
    "embed",
    "foreignobject",
    "form",
    "iframe",
    "input",
    "link",
    "marquee",
    "meta",
    "object",
    "script",
    "select",
    "set",
    "style",
    "summary",
    "textarea",
    "video",
}
_INTERACTIVE_ATTRIBUTES = {
    "autofocus",
    "contenteditable",
    "popover",
    "popovertarget",
    "popovertargetaction",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_RESOURCE_ATTRIBUTES = {
    "action",
    "archive",
    "background",
    "cite",
    "data",
    "formaction",
    "href",
    "imagesrcset",
    "poster",
    "src",
    "srcset",
    "xlink:href",
}
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_DATA_IMAGE_REFERENCE_RE = re.compile(
    r"^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$",
    re.IGNORECASE,
)
_COMPONENT_PAGE_PLACEMENT_RE = re.compile(
    r"\b(?:grid-area|grid-column|grid-row|position\s*:\s*(?:absolute|fixed)|"
    r"(?:top|right|bottom|left)\s*:|@page)\b",
    re.IGNORECASE,
)
_FIXED_POLICY_RE = re.compile(
    r"grid-template-areas|\[\s*data-poster-region|\bgrid-area\s*:",
    re.IGNORECASE,
)
class ContractError(ValueError):
    """A reusable package or receipt violates its closed contract."""


@dataclass(frozen=True)
class ResourceIdentity:
    """Immutable public identity for one reusable package."""

    kind: str
    resource_id: str
    version: str
    content_sha256: str
    semantic_roles: tuple[str, ...]
    page_modes: tuple[str, ...]


@dataclass(frozen=True)
class ComponentPackage:
    """Validated content-free HTML/CSS component package."""

    root: Path
    manifest: dict[str, Any]
    fragment_html: str
    style_css: str
    content_sha256: str


@dataclass(frozen=True)
class LayoutPolicyPackage:
    """Validated adaptive conference layout guidance package."""

    root: Path
    manifest: dict[str, Any]
    guidance_markdown: str
    style_css: str
    content_sha256: str


class _ContentFreeFragmentParser(HTMLParser):
    """Check one structural fragment without accepting finished poster copy."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.roots = 0
        self.root_attributes: dict[str, str] = {}
        self.visible_text: list[str] = []
        self.invalid = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        names = [name.lower() for name, _ in attrs]
        if self.depth == 0:
            self.roots += 1
            self.root_attributes = {
                name.lower(): str(value or "") for name, value in attrs
            }
        attributes = {name.lower(): str(value or "") for name, value in attrs}
        if len(names) != len(set(names)):
            self.invalid = True
        if lowered in {"html", "head", "body"} or lowered in _ACTIVE_TAGS:
            self.invalid = True
        if any(name.startswith("on") for name in attributes):
            self.invalid = True
        if any(name in _INTERACTIVE_ATTRIBUTES for name in attributes):
            self.invalid = True
        if any(
            name in _RESOURCE_ATTRIBUTES and not _safe_component_reference(value)
            for name, value in attributes.items()
        ):
            self.invalid = True
        inline_style = attributes.get("style")
        if inline_style and (
            offline_css_issues(
                inline_style,
                safe_reference=_safe_component_reference,
            )
            or _COMPONENT_PAGE_PLACEMENT_RE.search(inline_style)
        ):
            self.invalid = True
        if lowered not in _VOID_TAGS:
            self.depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.depth -= 1

    def handle_endtag(self, tag: str) -> None:
        del tag
        self.depth -= 1
        if self.depth < 0:
            self.invalid = True

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.visible_text.append(data.strip())


def load_component_package(path: str | Path) -> ComponentPackage:
    """Load one exact three-file component package."""

    root, contents = _read_package(path, COMPONENT_FILES)
    manifest = _load_json(contents["component.json"], source=root / "component.json")
    normalized = _validate_component_manifest(manifest)
    fragment = _decode(contents["fragment.html"], root / "fragment.html")
    style = _decode(contents["style.css"], root / "style.css")
    parser = _ContentFreeFragmentParser()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - malformed fragment becomes a contract error
        raise ContractError(f"component fragment is malformed: {exc}") from exc
    if parser.invalid or parser.depth != 0 or parser.roots != 1:
        raise ContractError("component fragment must contain exactly one inert root element")
    if parser.visible_text:
        raise ContractError("component fragment must be content-free; fill it only in poster HTML")
    expected_root = {
        "data-component-root": "1",
        "data-component-id": normalized["id"],
        "data-component-version": normalized["version"],
    }
    for name, expected in expected_root.items():
        if parser.root_attributes.get(name) != expected:
            raise ContractError(f"component fragment root must set {name}={expected!r}")
    root_class = f"sp-{normalized['id']}"
    if root_class not in parser.root_attributes.get("class", "").split():
        raise ContractError(f"component fragment root must use local class {root_class!r}")
    _validate_component_css(style, root_class=root_class)
    return ComponentPackage(
        root=root,
        manifest=normalized,
        fragment_html=fragment,
        style_css=style,
        content_sha256=_package_sha256(contents),
    )


def load_layout_policy_package(path: str | Path) -> LayoutPolicyPackage:
    """Load one exact three-file adaptive layout-policy package."""

    root, contents = _read_package(path, LAYOUT_POLICY_FILES)
    manifest = _load_json(contents["policy.json"], source=root / "policy.json")
    normalized = _validate_layout_policy_manifest(manifest)
    guidance = _decode(contents["guidance.md"], root / "guidance.md")
    style = _decode(contents["style.css"], root / "style.css")
    if len(guidance.strip()) < 40:
        raise ContractError("layout-policy guidance must explain adaptive composition")
    if _FIXED_POLICY_RE.search(guidance) or _FIXED_POLICY_RE.search(style):
        raise ContractError("layout-policy may not define fixed semantic slots")
    if offline_css_issues(style, safe_reference=_safe_component_reference):
        raise ContractError("layout-policy CSS must be inert and offline")
    if not style.strip():
        raise ContractError("layout-policy style.css may not be empty")
    return LayoutPolicyPackage(
        root=root,
        manifest=normalized,
        guidance_markdown=guidance,
        style_css=style,
        content_sha256=_package_sha256(contents),
    )


def load_resource_identity(kind: str, path: str | Path) -> ResourceIdentity:
    """Return the immutable identity of one supported package."""

    if kind == "component":
        package = load_component_package(path)
        manifest = package.manifest
        return ResourceIdentity(
            kind=kind,
            resource_id=manifest["id"],
            version=manifest["version"],
            content_sha256=package.content_sha256,
            semantic_roles=tuple(manifest["semantic_roles"]),
            page_modes=(),
        )
    if kind == "layout-policy":
        package = load_layout_policy_package(path)
        manifest = package.manifest
        return ResourceIdentity(
            kind=kind,
            resource_id=manifest["id"],
            version=manifest["version"],
            content_sha256=package.content_sha256,
            semantic_roles=tuple(manifest["strategies"]),
            page_modes=tuple(manifest["page_modes"]),
        )
    raise ContractError(f"unsupported resource kind: {kind}")


def package_sha256(path: str | Path) -> str:
    """Hash one exact supported package after validating it."""

    root = Path(path)
    component = root / "component.json"
    policy = root / "policy.json"
    if component.is_file() and not policy.exists():
        return load_component_package(root).content_sha256
    if policy.is_file() and not component.exists():
        return load_layout_policy_package(root).content_sha256
    raise ContractError("resource package kind is ambiguous or unsupported")


def validate_contract(schema_id: str, value: object) -> dict[str, Any]:
    """Validate the three public JSON contracts without a schema dependency."""

    if not isinstance(value, dict):
        raise ContractError("contract root must be an object")
    if schema_id == "scientific-poster.component.v1":
        return _validate_component_manifest(value)
    if schema_id == "scientific-poster.layout-policy.v1":
        return _validate_layout_policy_manifest(value)
    if schema_id == "scientific-poster.poster-approval.v1":
        return _validate_approval_receipt(value)
    raise ContractError(f"unsupported contract: {schema_id}")


def _validate_component_manifest(value: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "id", "version", "level", "purpose", "semantic_roles"}
    _exact_fields(value, required, label="component manifest")
    if value["schema"] != "scientific-poster.component.v1":
        raise ContractError("unsupported component schema")
    _identity_fields(value)
    if value["level"] not in {"atom", "composite"}:
        raise ContractError("component level must be atom or composite")
    purpose = value["purpose"]
    if not isinstance(purpose, str) or not 20 <= len(purpose.strip()) <= 500:
        raise ContractError("component purpose must contain 20 to 500 characters")
    roles = _string_list(value["semantic_roles"], label="semantic_roles", pattern=_ROLE_RE)
    if not roles:
        raise ContractError("component needs at least one semantic role")
    return {
        "schema": value["schema"],
        "id": value["id"],
        "version": value["version"],
        "level": value["level"],
        "purpose": purpose.strip(),
        "semantic_roles": roles,
    }


def _validate_layout_policy_manifest(value: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "id", "version", "purpose", "page_modes", "strategies"}
    _exact_fields(value, required, label="layout-policy manifest")
    if value["schema"] != "scientific-poster.layout-policy.v1":
        raise ContractError("unsupported layout-policy schema")
    _identity_fields(value)
    purpose = value["purpose"]
    if not isinstance(purpose, str) or not 20 <= len(purpose.strip()) <= 500:
        raise ContractError("layout-policy purpose must contain 20 to 500 characters")
    page_modes = _string_list(value["page_modes"], label="page_modes")
    if not page_modes or set(page_modes) - {"portrait", "landscape"}:
        raise ContractError("layout-policy page_modes must use portrait or landscape")
    strategies = _string_list(value["strategies"], label="strategies", pattern=_ROLE_RE)
    if not strategies:
        raise ContractError("layout-policy needs at least one strategy")
    return {
        "schema": value["schema"],
        "id": value["id"],
        "version": value["version"],
        "purpose": purpose.strip(),
        "page_modes": page_modes,
        "strategies": strategies,
    }


def _validate_approval_receipt(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "source_html_uri",
        "source_html_origin_uri",
        "source_html_sha256",
        "grounding_source_sha256",
        "source_figure_manifest_sha256",
        "approved",
        "approved_at",
        "session_id",
        "decision",
    }
    _exact_fields(value, required, label="approval receipt")
    if value["schema"] != "scientific-poster.poster-approval.v1":
        raise ContractError("unsupported approval schema")
    if value["source_html_uri"] != "bundle:poster.html":
        raise ContractError("approval source_html_uri must identify bundle:poster.html")
    if (
        not isinstance(value["source_html_origin_uri"], str)
        or not value["source_html_origin_uri"].strip()
        or len(value["source_html_origin_uri"]) > 2048
    ):
        raise ContractError("approval source_html_origin_uri is invalid")
    if not isinstance(value["source_html_sha256"], str) or not _HASH_RE.fullmatch(
        value["source_html_sha256"]
    ):
        raise ContractError("invalid approval HTML hash")
    if not isinstance(value["grounding_source_sha256"], str) or not _HASH_RE.fullmatch(
        value["grounding_source_sha256"]
    ):
        raise ContractError("invalid approval grounding hash")
    if not isinstance(value["source_figure_manifest_sha256"], str) or not _HASH_RE.fullmatch(
        value["source_figure_manifest_sha256"]
    ):
        raise ContractError("invalid approval source-figure manifest hash")
    if value["approved"] is not True:
        raise ContractError("approval receipt must record literal approval")
    if not _is_utc_rfc3339(value["approved_at"]):
        raise ContractError("approval timestamp must be UTC")
    if not _bounded_string(value["session_id"], limit=256):
        raise ContractError("approval session_id is required")
    if not isinstance(value["decision"], dict):
        raise ContractError("approval decision must be an object")
    decision = value["decision"]
    decision_fields = {
        "mode",
        "session_id",
        "host_event_id",
        "target_kind",
        "target_sha256",
        "user_message_sha256",
        "event_sha256",
    }
    _exact_fields(decision, decision_fields, label="approval decision")
    if decision["mode"] != "portable-operator":
        raise ContractError("approval decision mode must be portable-operator")
    if not _bounded_string(decision["session_id"], limit=256):
        raise ContractError("approval decision session_id is required")
    if not isinstance(decision["host_event_id"], str) or not _EVENT_RE.fullmatch(
        decision["host_event_id"]
    ):
        raise ContractError("invalid approval host_event_id")
    if decision["target_kind"] != "poster":
        raise ContractError("approval decision target_kind must be poster")
    for name in ("target_sha256", "user_message_sha256", "event_sha256"):
        if not isinstance(decision[name], str) or not _HASH_RE.fullmatch(decision[name]):
            raise ContractError(f"invalid approval decision hash: {name}")
    return dict(value)


def _read_package(path: str | Path, expected: tuple[str, ...]) -> tuple[Path, dict[str, bytes]]:
    root = Path(path).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise ContractError("resource package must be a regular directory")
    try:
        names = {item.name for item in root.iterdir()}
    except OSError as exc:
        raise ContractError(f"cannot inspect resource package: {exc}") from exc
    if names != set(expected):
        raise ContractError("resource package must contain exactly: " + ", ".join(expected))
    contents: dict[str, bytes] = {}
    for name in expected:
        member = root / name
        if member.is_symlink() or not member.is_file():
            raise ContractError(f"resource member must be a regular file: {name}")
        try:
            raw = member.read_bytes()
        except OSError as exc:
            raise ContractError(f"cannot read resource member {name}: {exc}") from exc
        if len(raw) > MAX_MEMBER_BYTES:
            raise ContractError(f"resource member exceeds 1 MiB: {name}")
        contents[name] = raw
    return root.resolve(), contents


def _load_json(raw: bytes, *, source: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ContractError(f"duplicate JSON key in {source}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid UTF-8 JSON in {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"JSON root must be an object: {source}")
    return value


def _decode(raw: bytes, source: Path) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"resource member must be UTF-8: {source}") from exc


def _package_sha256(contents: dict[str, bytes]) -> str:
    digest = hashlib.sha256(b"scientific-poster-resource-package-v1\0")
    for name in sorted(contents):
        encoded = name.encode("utf-8")
        raw = contents[name]
        digest.update(len(encoded).to_bytes(2, "big"))
        digest.update(encoded)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _validate_component_css(css: str, *, root_class: str) -> None:
    if not css.strip():
        raise ContractError("component style.css may not be empty")
    if offline_css_issues(css, safe_reference=_safe_component_reference):
        raise ContractError("component CSS must be inert and offline")
    if _COMPONENT_PAGE_PLACEMENT_RE.search(css):
        raise ContractError("component CSS may not control page placement")
    selector_prefix = re.compile(rf"^\.{re.escape(root_class)}(?![a-zA-Z0-9-])")
    preludes = _top_level_rule_preludes(css)
    if not preludes:
        raise ContractError(f"component CSS must be local to .{root_class}")
    for prelude in preludes:
        if prelude.startswith("@"):
            raise ContractError("component CSS may not contain unscoped at-rules")
        selectors = [selector.strip() for selector in prelude.split(",")]
        if not selectors or any(selector_prefix.match(selector) is None for selector in selectors):
            raise ContractError("component CSS may not escape its local class boundary")


def _top_level_rule_preludes(css: str) -> list[str]:
    """Return top-level CSS rule preludes, rejecting malformed brace structure."""

    source = _CSS_COMMENT_RE.sub("", css)
    depth = 0
    start = 0
    quote = ""
    escaped = False
    preludes: list[str] = []
    for index, character in enumerate(source):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character == "{":
            if depth == 0:
                prelude = source[start:index].strip()
                if not prelude:
                    raise ContractError("component CSS contains an empty selector")
                preludes.append(prelude)
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                raise ContractError("component CSS has unbalanced braces")
            if depth == 0:
                start = index + 1
    if depth != 0 or quote:
        raise ContractError("component CSS is malformed")
    if source[start:].strip():
        raise ContractError("component CSS has trailing content outside a rule")
    return preludes


def _safe_component_reference(value: str) -> bool:
    candidate = value.strip()
    return (
        not candidate
        or candidate.startswith("#")
        or _DATA_IMAGE_REFERENCE_RE.fullmatch(candidate) is not None
    )


def _is_utc_rfc3339(value: object) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or _UTC_RFC3339_RE.fullmatch(value) is None
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _bounded_string(value: object, *, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _identity_fields(value: dict[str, Any]) -> None:
    if not isinstance(value["id"], str) or not _ID_RE.fullmatch(value["id"]):
        raise ContractError("invalid resource id")
    if not isinstance(value["version"], str) or not _VERSION_RE.fullmatch(value["version"]):
        raise ContractError("invalid semantic version")


def _string_list(
    value: object,
    *,
    label: str,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractError(f"{label} must be a string array")
    normalized = [item.strip() for item in value]
    if any(not item or len(item) > 128 for item in normalized):
        raise ContractError(f"{label} contains an invalid value")
    if len(normalized) != len(set(normalized)):
        raise ContractError(f"{label} must not contain duplicates")
    if pattern is not None and any(pattern.fullmatch(item) is None for item in normalized):
        raise ContractError(f"{label} contains an invalid identifier")
    return normalized


def _exact_fields(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ContractError(f"{label} fields differ; missing={missing}, extra={extra}")


__all__ = [
    "COMPONENT_FILES",
    "LAYOUT_POLICY_FILES",
    "RESOURCE_KINDS",
    "ComponentPackage",
    "ContractError",
    "LayoutPolicyPackage",
    "ResourceIdentity",
    "load_component_package",
    "load_layout_policy_package",
    "load_resource_identity",
    "package_sha256",
    "validate_contract",
]
