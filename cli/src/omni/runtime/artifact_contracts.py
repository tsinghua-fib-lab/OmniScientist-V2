"""Artifact contract registry.

Contracts describe how a source artifact is revised, rendered, validated, and
recorded.  They are the runtime equivalent of a build/test harness: the model can
ask for changes naturally, but the runtime closes the loop deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from omni.runtime.artifact_intents import ArtifactElement
from omni.runtime.artifact_renderers import GraphvizRenderer


@dataclass(frozen=True, slots=True)
class DerivedOutput:
    format: str
    suffix: str
    mime: str


class ArtifactContract(Protocol):
    name: str
    source_suffixes: tuple[str, ...]
    derived_outputs: tuple[DerivedOutput, ...]

    def extract_elements(self, source_text: str) -> list[ArtifactElement]: ...

    def patch(self, source_text: str, intent: Any) -> tuple[str, list[str]]: ...

    async def render(self, source: Path, *, output_stem: Path | None = None) -> Any: ...


@dataclass(slots=True)
class ArtifactContractRegistry:
    contracts: list[ArtifactContract] = field(default_factory=list)

    def register(self, contract: ArtifactContract) -> None:
        self.contracts.append(contract)

    def for_path(self, path: Path | str) -> ArtifactContract | None:
        suffix = Path(path).suffix.lower()
        for contract in self.contracts:
            if suffix in contract.source_suffixes:
                return contract
        return None


class GraphvizDotContract:
    name = "graphviz-dot"
    source_suffixes = (".dot",)
    derived_outputs = (
        DerivedOutput("svg", ".svg", "image/svg+xml"),
        DerivedOutput("png", ".png", "image/png"),
    )

    def __init__(self, renderer: GraphvizRenderer | None = None) -> None:
        self.renderer = renderer or GraphvizRenderer()

    def extract_elements(self, source_text: str) -> list[ArtifactElement]:
        from omni.runtime.graphviz_revision import extract_graphviz_elements

        return extract_graphviz_elements(source_text)

    def patch(self, source_text: str, intent: Any) -> tuple[str, list[str]]:
        from omni.runtime.graphviz_revision import patch_graphviz_style

        return patch_graphviz_style(source_text, intent)

    async def render(self, source: Path, *, output_stem: Path | None = None) -> Any:
        return await self.renderer.render(source, output_stem=output_stem)


DEFAULT_CONTRACT_REGISTRY = ArtifactContractRegistry()
DEFAULT_CONTRACT_REGISTRY.register(GraphvizDotContract())


def contract_for_path(path: Path | str) -> ArtifactContract | None:
    return DEFAULT_CONTRACT_REGISTRY.for_path(path)

__all__ = [
    "ArtifactContractRegistry",
    "DerivedOutput",
    "GraphvizDotContract",
    "DEFAULT_CONTRACT_REGISTRY",
    "contract_for_path",
]
