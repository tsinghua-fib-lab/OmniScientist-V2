"""Parse language-neutral, machine-readable boundaries before planning.

Natural-language intent belongs to the semantic planner. This module recognizes
only explicit provider syntax whose meaning does not depend on the user's
language: ``$name``, ``skill:name``, ``run_skill name``, ``use_skill name``, and
``/skills run name``. Commands, identifiers, paths, and permissions are handled
by their dedicated parsers.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

from omni.core.field_contract import instruction_field
from omni.skills_runtime.registry import SkillRegistry, scope_sources


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    kind: str
    reason: str = ""
    skill: str = ""
    missing_field: str = ""
    # Forced discovery source when the user wrote ``$<scope>:<name>``; empty for
    # a bare ``$name`` (which resolves to the winning skill for that name).
    skill_source: str = ""
    # Registered native tool identifier the user named as a token, or "".
    tool: str = ""

# Language-neutral identifiers already registered on the ReAct catalog.
# Matching these is the same class of host boundary as ``run_skill name``,
# not a natural-language allow/deny grammar.
_NATIVE_TOOL_NAMES = (
    "search_literature",
    "search_corpus",
    "cite_source",
    "record_claim",
    "add_evidence",
    "record_hypothesis",
    "citation_neighbors",
)
_NATIVE_TOOL_RE = re.compile(
    r"(?:^|[^\w])(?P<name>" + "|".join(re.escape(n) for n in _NATIVE_TOOL_NAMES) + r")(?:[^\w]|$)"
)


_EXPLICIT_SKILL_PATTERNS = (
    re.compile(r"(?:^|\s)\$(?P<name>[A-Za-z0-9][\w.+:/-]*)\b"),
    re.compile(r"(?:^|\s)skill:(?P<name>[A-Za-z0-9][\w.+:/-]*)\b", re.IGNORECASE),
    re.compile(
        r"(?:^|\s)(?:run_skill|use_skill)\s+(?P<name>[A-Za-z0-9][\w.+:/-]*)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|\s)/skills\s+run\s+(?P<name>[A-Za-z0-9][\w.+:/-]*)\b",
        re.IGNORECASE,
    ),
)


class BoundaryRouter:
    """Evaluate explicit protocol boundaries before semantic planning."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def route(self, text: str) -> BoundaryDecision | None:
        name, source = explicit_skill_ref(text or "", self._registry)
        if name:
            return BoundaryDecision(
                "explicit_skill",
                "user explicitly selected a skill/provider",
                skill=name,
                skill_source=source,
            )
        tool = explicit_native_tool(text or "")
        if not tool:
            return None
        return BoundaryDecision(
            "explicit_tool",
            "user named a registered native tool",
            tool=tool,
        )


def explicit_native_tool(text: str) -> str:
    """Return a registered native tool token from ``text``, or ``""``.

    Skill protocol (``$name`` / ``run_skill name``) is checked first by
    :meth:`BoundaryRouter.route`. This only matches catalog identifiers.
    """
    match = _NATIVE_TOOL_RE.search(text or "")
    return match.group("name") if match is not None else ""


def _explicit_skill_match(text: str) -> re.Match[str] | None:
    for pattern in _EXPLICIT_SKILL_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match
    return None


def _explicit_token(text: str) -> str:
    """The raw ``$name`` / ``$<scope>:<name>`` token the user typed, or ``""``."""
    match = _explicit_skill_match(text)
    return match.group("name") if match is not None else ""


def explicit_skill_ref(text: str, registry: SkillRegistry) -> tuple[str, str]:
    """Resolve an explicit selection to ``(canonical_name, source)``.

    A bare ``$name`` returns ``(name, "")`` (the winning skill for that name). A
    ``$<scope>:<name>`` escape returns the concrete source (e.g. ``user_omni``)
    so a skill shadowed by a same-named built-in is still reachable end to end.
    Returns ``("", "")`` when nothing matches an installed skill.
    """
    token = _explicit_token(text)
    if not token:
        return "", ""
    entry = registry.resolve_explicit(token)
    if entry is None:
        return "", ""
    # A recognised scope prefix means the user forced this specific source.
    scope, sep, rest = token.partition(":")
    forced = bool(sep and rest and scope_sources(scope) is not None)
    return entry.name, (entry.source if forced else "")


def explicit_skill_name(text: str, registry: SkillRegistry) -> str:
    """Backwards-compatible helper returning only the canonical skill name."""
    return explicit_skill_ref(text, registry)[0]


def explicit_skill_arguments(text: str, registry: SkillRegistry) -> dict[str, Any]:
    """Parse optional JSON or shell-style ``key=value`` after an explicit skill.

    This keeps protocol invocations deterministic while letting paths containing
    spaces reach typed skill contracts, for example::

        $paper-review input="paper draft.pdf" venue="ACL 2025" mode=strict

    A plain remainder binds to the provider's contract-declared instruction
    field, preserving the long-standing ``$skill natural-language instruction``
    form without inventing an undeclared ``input`` field. Scoped invocations
    such as ``$user:paper-review`` use the same argument parsing.
    """

    match = _explicit_skill_match(text)
    entry = registry.resolve_explicit(match.group("name")) if match is not None else None
    if match is None or entry is None:
        return {}
    instruction_slot = instruction_field(entry.input_schema)
    remainder = str(text or "")[match.end() :].strip()
    if not remainder:
        return {}
    if remainder.startswith("{"):
        try:
            payload = json.loads(remainder)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return {str(key): value for key, value in payload.items()}

    try:
        tokens = shlex.split(remainder)
    except ValueError:
        return {instruction_slot: remainder} if instruction_slot else {}
    arguments: dict[str, Any] = {}
    bare: list[str] = []
    for token in tokens:
        key, separator, value = token.partition("=")
        if separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key):
            arguments[key.replace("-", "_")] = _coerce_protocol_value(value)
        else:
            bare.append(token)
    if not arguments:
        return {instruction_slot: remainder} if instruction_slot else {}
    if bare and instruction_slot and instruction_slot not in arguments:
        arguments[instruction_slot] = " ".join(bare)
    return arguments


def _coerce_protocol_value(value: str) -> Any:
    lowered = value.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
