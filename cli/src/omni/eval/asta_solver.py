"""Inspect/AstaBench solver adapter for the OmniScientist agent.

Run this module from the official AstaBench checkout so Asta owns task loading,
sandboxing, scoring, and result aggregation. Omni supplies only the solver. The
adapter preserves task-provided tools, reports Omni's out-of-band model usage to
Inspect, and places every sample in an isolated Omni workspace.
"""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path
from typing import Any

from omni.agent import OmniAgent
from omni.config import load_settings
from omni.core.react_agent import ToolSpec
from omni.eval.blackbox import isolated_eval_settings
from omni.skills_runtime.context import Tool as OmniTool


def wrap_inspect_tools(tools: list[Any]) -> list[OmniTool]:
    """Adapt Inspect tools while retaining their official schema and callable."""
    try:
        from inspect_ai.tool import ToolDef
    except ImportError as exc:  # pragma: no cover - optional integration
        raise RuntimeError(
            "Inspect AI is required. Run this adapter inside the official AstaBench environment."
        ) from exc

    wrapped: list[OmniTool] = []
    for candidate in tools:
        definition = candidate if isinstance(candidate, ToolDef) else ToolDef(candidate)
        parameters = definition.parameters
        schema = (
            parameters.model_dump(exclude_none=True)
            if hasattr(parameters, "model_dump")
            else dict(parameters)
        )
        official_callable = definition.tool

        async def invoke(args: dict[str, Any], call=official_callable) -> Any:  # noqa: B008
            value = call(**args)
            return await value if inspect.isawaitable(value) else value

        wrapped.append(
            OmniTool(
                ToolSpec(
                    name=str(definition.name),
                    description=str(definition.description or "AstaBench task tool"),
                    parameters=schema,
                ),
                invoke,
                # Benchmark sandboxes are deliberately capable. Marking all host
                # tools sensitive keeps the same policy path as local execution;
                # the isolated solver explicitly opts into autonomous execution.
                sensitive=True,
            )
        )
    return wrapped


def _record_asta_usage(settings: Any, cost: dict[str, Any]) -> None:
    """Bridge custom-agent token usage into Asta/Inspect cost accounting."""
    try:
        from astabench.util.model import record_model_usage_with_inspect
        from inspect_ai.model import ModelUsage
    except ImportError:  # Inspect can use the solver outside AstaBench.
        return
    total = int(cost.get("total_tokens") or 0)
    if total <= 0:
        return
    usage = ModelUsage(
        input_tokens=int(cost.get("prompt_tokens") or 0),
        output_tokens=int(cost.get("completion_tokens") or 0),
        total_tokens=total,
    )
    model_name = str(settings.model.model or settings.model.provider or "omni")
    record_model_usage_with_inspect(model_name, usage)


def _missing_solver(*args: Any, **kwargs: Any) -> Any:
    del args, kwargs
    raise RuntimeError(
        "Inspect AI is not installed. Use this solver from the official AstaBench environment."
    )


try:  # Optional dependency: normal Omni installs must not import Inspect.
    from inspect_ai.model import ModelOutput
    from inspect_ai.solver import Generate, Solver, TaskState, solver
except ImportError:  # pragma: no cover - exercised only without the eval extra
    omni_agent = _missing_solver
else:

    @solver
    def omni_agent() -> Solver:
        """Run one AstaBench sample through Omni using task-owned tools only."""

        async def solve(state: TaskState, generate: Generate) -> TaskState:
            del generate  # Omni owns its model loop; usage is bridged explicitly.
            with tempfile.TemporaryDirectory(prefix="omni-astabench-") as raw_root:
                settings = isolated_eval_settings(
                    load_settings(),
                    Path(raw_root),
                    f"asta-{state.sample_id}-{state.epoch}",
                )
                # Registry workflows could substitute unrestricted local tools.
                # Official task tools are authoritative for comparable scoring.
                settings.skills.sources = []
                settings.security.require_approval = False
                agent = await OmniAgent.create(settings)
                agent.set_external_tools(wrap_inspect_tools(list(state.tools)), authoritative=True)
                try:
                    result = await agent.handle_turn(
                        state.input_text,
                        channel="astabench",
                        drain_tasks=True,
                    )
                    cost = await agent.tasks.cost_summary(result.task_id, include_child_tasks=True)
                finally:
                    await agent.aclose()
            _record_asta_usage(settings, cost)
            output = ModelOutput.from_content(
                model=f"omni/{settings.model.model or settings.model.provider}",
                content=result.text,
            )
            output.metadata = {
                "omni_run_id": result.task_id,
                "omni_status": result.kind,
                "omni_cost": cost,
            }
            state.output = output
            return state

        return solve


__all__ = ["omni_agent", "wrap_inspect_tools"]
