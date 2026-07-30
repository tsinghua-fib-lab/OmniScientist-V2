"""Base contract for Python skill engines (identical shape to HelixForge).

A skill engine implements an optional ``validate_params`` static method and
an async ``execute(**input_data)`` returning a JSON-serialisable dict. The
``progress_callback`` is optional and used by long-running async skills.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

ProgressCallback = Any  # Callable[[str, float], Awaitable[None]] | None


@runtime_checkable
class SkillEngine(Protocol):
    async def execute(self, progress_callback: ProgressCallback = None, **input_data: Any) -> dict[str, Any]: ...


class BaseSkillEngine:
    """Convenience base; engines may subclass or just duck-type ``execute``."""

    @staticmethod
    def validate_params(*, arguments: dict | None = None, input_data: dict | None = None) -> dict | None:
        """Return ``None`` if valid, else an error dict the LLM will see."""
        return None

    async def execute(self, progress_callback: ProgressCallback = None, **input_data: Any) -> dict[str, Any]:
        raise NotImplementedError
