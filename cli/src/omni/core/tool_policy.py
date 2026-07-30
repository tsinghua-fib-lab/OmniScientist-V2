"""Runtime enforcement for IntentPlan tool policies."""

from __future__ import annotations

from typing import Any

from omni.core.tool_result import HostToolRejection, _mint_host_tool_rejection


def filter_tools_for_policy(tools: list[Any], policy: Any | None) -> list[Any]:
    """Return tools visible to the model under the plan policy."""
    if policy is None:
        return list(tools)
    out: list[Any] = []
    for tool in tools:
        name = tool.spec.name
        if name in policy.blocked_tools:
            continue
        if policy.allowed_tools is not None and name not in policy.allowed_tools:
            continue
        out.append(tool)
    return out


def policy_summary(policy: Any | None) -> dict[str, Any]:
    if policy is None:
        return {}
    return policy.to_dict()


def _configured_limit(value: int) -> int | None:
    """Normalize owner config: negative disables; zero remains exact zero."""
    parsed = int(value)
    return None if parsed < 0 else parsed


def _effective_limit(scoped: int | None, configured: int) -> int | None:
    global_limit = _configured_limit(configured)
    if scoped is None:
        return global_limit
    scoped_limit = max(0, int(scoped))
    if global_limit is None:
        return scoped_limit
    return min(global_limit, scoped_limit)


def policy_max_tool_calls(policy: Any | None, default: int) -> int | None:
    scoped = None if policy is None else policy.max_tool_calls
    return _effective_limit(scoped, default)


def policy_max_iterations(policy: Any | None, default: int) -> int | None:
    scoped = None if policy is None else policy.max_iterations
    return _effective_limit(scoped, default)


def _remedy(name: str, reason: str) -> str:
    """Say what the model may do instead of re-issuing a refused call.

    A refusal is deterministic: the same call will be refused again. Without a
    stated alternative the model's only move is to vary the arguments and try
    again, which reads as fresh work to a signature-keyed progress detector and
    quietly consumes the run.
    """
    code = reason.split(":", 1)[0]
    if code == "tool_limit_exceeded":
        return (
            f"the '{name}' budget for this task is spent; fold the remaining work "
            f"into what you already produced, or finish with the results you have"
        )
    if code == "max_tool_calls_exceeded":
        return "the tool budget is spent; answer now from the results you have"
    if code in {"not_in_allowed_tools", "blocked_by_plan"}:
        return (
            f"'{name}' is not part of this task's tool surface; use one of the "
            f"offered tools, or answer directly"
        )
    if code == "unknown_tool":
        return f"there is no tool named '{name}'; choose one of the offered tools"
    return "do not re-issue this call unchanged; take a different approach"


def policy_violation(name: str, reason: str) -> HostToolRejection:
    """Return a protocol-safe result for a tool rejected before execution."""
    remedy = _remedy(name, reason)
    return _mint_host_tool_rejection(
        {
            "status": "error",
            "error": f"tool '{name}' rejected by execution policy: {reason} — {remedy}",
            "policy_violation": True,
            "tool_name": name,
            "reason": reason,
            "remedy": remedy,
        }
    )


class ToolPolicyGuard:
    """Single stateful admission check shared by interactive and skill loops."""

    def __init__(
        self,
        *,
        allowed_tools: list[str] | None = None,
        blocked_tools: list[str] | None = None,
        max_tool_calls: int | None = None,
        per_tool_limits: dict[str, int] | None = None,
    ) -> None:
        self.allowed_tools = None if allowed_tools is None else set(allowed_tools)
        self.blocked_tools = set(blocked_tools or [])
        self.max_tool_calls = None if max_tool_calls is None else max(0, int(max_tool_calls))
        self.per_tool_limits = dict(per_tool_limits or {})
        self._total = 0
        self._counts: dict[str, int] = {}

    @classmethod
    def from_policy(cls, policy: Any | None) -> ToolPolicyGuard:
        if policy is None:
            return cls()
        return cls(
            allowed_tools=policy.allowed_tools,
            blocked_tools=policy.blocked_tools,
            max_tool_calls=policy.max_tool_calls,
            per_tool_limits=policy.per_tool_limits,
        )

    def authorization_rejection(self, name: str) -> dict[str, Any] | None:
        """Reject a forbidden target without consuming execution budget."""
        if name in self.blocked_tools:
            return policy_violation(name, "blocked_by_plan")
        if self.allowed_tools is not None and name not in self.allowed_tools:
            return policy_violation(name, "not_in_allowed_tools")
        return None

    def budget_rejection(self, name: str) -> dict[str, Any] | None:
        """Charge one logical operation, but only if it is admitted.

        A refused call executes nothing, so it must cost nothing. Charging for
        it let a model exhaust an entire run against a wall it had already hit:
        every retry was refused *and* billed to the shared budget. Convergence
        is the loop's job — it counts consecutive refusals and stops — not the
        meter's.
        """
        if self.max_tool_calls is not None and self._total >= self.max_tool_calls:
            return policy_violation(name, f"max_tool_calls_exceeded:{self.max_tool_calls}")
        limit = max(0, int(self.per_tool_limits.get(name, 0) or 0))
        if limit and self._counts.get(name, 0) >= limit:
            return policy_violation(name, f"tool_limit_exceeded:{limit}")
        self._total += 1
        if limit:
            self._counts[name] = self._counts.get(name, 0) + 1
        return None

    def rejection(self, name: str) -> dict[str, Any] | None:
        """Authorize and charge one ordinary, non-delegating operation."""
        return self.authorization_rejection(name) or self.budget_rejection(name)


__all__ = [
    "filter_tools_for_policy",
    "policy_max_iterations",
    "policy_max_tool_calls",
    "policy_summary",
    "policy_violation",
    "ToolPolicyGuard",
]
