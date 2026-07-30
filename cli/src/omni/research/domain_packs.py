"""Registry-driven research domain packs.

Packs add compact planning guidance, specialist templates, connector priority,
and artifact expectations.  They never bypass connector enablement, tool policy,
or skill contracts; those remain runtime-owned security boundaries.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SpecialistTemplate:
    role: str
    description: str
    tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DomainPack:
    name: str
    title: str
    description: str
    guidance: str
    connectors: tuple[str, ...] = ()
    artifact_types: tuple[str, ...] = ()
    specialists: tuple[SpecialistTemplate, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


def bundled_domain_packs_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "domain_packs"


def load_domain_packs(root: Path | None = None) -> dict[str, DomainPack]:
    """Load the bundled or user-supplied TOML pack catalogue."""
    directory = root or bundled_domain_packs_dir()
    packs: dict[str, DomainPack] = {}
    for path in sorted(directory.glob("*.toml")):
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        name = str(raw.get("name") or path.stem).strip()
        if not name:
            continue
        specialists = tuple(
            SpecialistTemplate(
                role=str(item.get("role") or "specialist").strip(),
                description=str(item.get("description") or "").strip(),
                tools=tuple(str(tool) for tool in (item.get("tools") or []) if str(tool).strip()),
            )
            for item in (raw.get("specialists") or [])
            if isinstance(item, dict)
        )
        known = {
            "name", "title", "description", "guidance", "connectors",
            "artifact_types", "specialists",
        }
        packs[name] = DomainPack(
            name=name,
            title=str(raw.get("title") or name),
            description=str(raw.get("description") or ""),
            guidance=str(raw.get("guidance") or ""),
            connectors=tuple(str(item) for item in (raw.get("connectors") or [])),
            artifact_types=tuple(str(item) for item in (raw.get("artifact_types") or [])),
            specialists=specialists,
            metadata={key: value for key, value in raw.items() if key not in known},
        )
    return packs


class DomainPackRegistry:
    """Resolve configured domain packs and their additive recommendations."""

    def __init__(self, settings: Any, *, root: Path | None = None) -> None:
        self._settings = settings
        self._packs = load_domain_packs(root)

    def all(self) -> list[DomainPack]:
        return list(self._packs.values())

    def get(self, name: str) -> DomainPack | None:
        return self._packs.get(str(name or "").strip())

    def enabled(self) -> list[DomainPack]:
        names = list(getattr(getattr(self._settings, "research", None), "domain_packs", []) or [])
        return [self._packs[name] for name in names if name in self._packs]

    def recommended_connectors(self, *, available: set[str] | None = None) -> list[str]:
        out: list[str] = []
        for pack in self.enabled():
            for name in pack.connectors:
                if (available is None or name in available) and name not in out:
                    out.append(name)
        return out

    def prompt(self, *, char_budget: int = 3000) -> str:
        """Compact planner/ReAct guidance for only the enabled packs."""
        lines = ["Enabled research domain packs provide methods and artifact contracts; runtime policy still controls safety and tools:"]
        for pack in self.enabled():
            lines.append(f"- {pack.name}（{pack.title}）：{pack.description}")
            if pack.guidance:
                lines.append(f"  Guidance: {pack.guidance}")
            if pack.connectors:
                lines.append(f"  Recommended sources: {', '.join(pack.connectors)}")
            if pack.artifact_types:
                lines.append(f"  Recommended artifacts: {', '.join(pack.artifact_types)}")
            for specialist in pack.specialists:
                lines.append(f"  specialist {specialist.role}：{specialist.description}")
        text = "\n".join(lines)
        if len(text) <= char_budget:
            return text
        return text[:char_budget].rstrip() + "\n... (truncated)"


__all__ = [
    "DomainPack",
    "DomainPackRegistry",
    "SpecialistTemplate",
    "bundled_domain_packs_dir",
    "load_domain_packs",
]
