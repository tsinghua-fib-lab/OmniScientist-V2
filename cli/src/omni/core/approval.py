"""Human-in-the-loop approval gate for sensitive tool calls (P0 security).

The sandbox/allowlist stack already confines *where* tools can act: fs writes
are pinned to workspace roots, ``bash`` is denylisted + OS-sandboxed, IM-origin
sensitive calls fail closed, and secret files are hidden. This module adds the
missing *consent* layer — before a mutating/executing tool runs, the owner may
approve or deny it.

Design
------
* ``classify_tool_call`` decides whether a call is sensitive (mutating /
  executing) and, for ``bash``, whether it is outright *destructive*.
* ``ApprovalGate.wrap`` decorates the ReAct tool invoker (composed *after* the
  plan tool-policy guard in the orchestrator). Safe tools pass straight through;
  sensitive tools are authorised first.
* Resolution honours ``security.require_approval`` + ``security.approval_policy``
  and an owner-curated ``security.approval_allowlist``. A per-session allow set
  remembers "approve for session" answers so the owner isn't re-asked.
* When no interactive approver is wired (daemon / IM / non-interactive run) a
  sensitive call **fails closed** with an actionable, structured tool result —
  never a raised exception — so the model can relay the block to the user and
  a batch/remote context can't silently run destructive work.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from omni.core.tool_result import HostToolRejection, _mint_host_tool_rejection
from omni.core.turn_clock import pause_clocks
from omni.skills_runtime.builtin_tools.shell import command_is_destructive

ToolInvoker = Callable[[str, dict[str, Any]], Awaitable[Any]]

# Mutating / executing tools that leave the read-only "just look" envelope.
SENSITIVE_TOOLS = frozenset({"bash", "write_file", "edit_file", "run_compute"})

_IM_CHANNELS = frozenset({"wechat", "feishu", "dingtalk"})


@dataclass(frozen=True)
class ApprovalRequest:
    """One sensitive tool call awaiting the owner's decision."""

    tool_name: str
    arguments: dict[str, Any]
    risk: str  # write | exec | destructive | tool
    detail: str = ""  # command / path preview shown to the human


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    scope: str = "once"  # once | session
    reason: str = ""


# An approver turns a request into a decision (e.g. a terminal prompt). It is
# ``None`` in non-interactive contexts, which makes sensitive calls fail closed.
Approver = Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]

# Optional sink for audit events: ``on_event(kind, payload)`` may be async.
ApprovalEventSink = Callable[[str, dict[str, Any]], Any]
Preauthorizer = Callable[[str, dict[str, Any]], Awaitable[bool]]


def resolve_policy(settings: Any) -> str:
    """Resolve the effective approval policy string.

    ``require_approval=False`` collapses to ``"never"`` (full autonomy);
    otherwise the configured ``approval_policy`` is normalised, with the
    ``on-request`` alias folded into ``untrusted`` and unknown values treated as
    the safe default.
    """
    sec = getattr(settings, "security", None)
    if sec is None or not bool(getattr(sec, "require_approval", True)):
        return "never"
    policy = str(getattr(sec, "approval_policy", "untrusted") or "untrusted").strip().lower()
    if policy in ("on-request", "on_request", "onrequest"):
        return "untrusted"
    if policy in ("never", "untrusted", "always"):
        return policy
    return "untrusted"


def reconcile_sensitive_visibility(
    blocked_tools: Sequence[str],
    *,
    gate_can_clear: bool,
    approved: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Reconcile a capability deny-list against what the *security* gate can clear.

    This is a SECURITY decision and therefore lives in the security module, not
    in the planner/orchestrator. Capability planning declares sensitive
    mutations (``SENSITIVE_TOOLS`` — bash/write_file/edit_file/run_compute) as
    ``blocked_tools`` (deny-by-default in the plan record). Whether those tools
    are actually *offered* to the model depends only on security state:

    * ``gate_can_clear`` — autonomy mode (``require_approval`` off) or an
      interactive approver is wired: drop every sensitive tool from the
      deny-list; the approval gate governs them at invoke time.
    * otherwise (daemon / IM / non-interactive): keep them blocked, except any
      tool the owner already pre-approved for this task (``approved``).

    Non-sensitive entries are capability decisions and are left untouched. The
    hard floor is unaffected — the gate still denies by name anything that slips
    through, so this only controls *catalog visibility*, never authorisation.
    """
    blocked = list(blocked_tools or [])
    if not any(t in blocked for t in SENSITIVE_TOOLS):
        return blocked
    if gate_can_clear:
        return [t for t in blocked if t not in SENSITIVE_TOOLS]
    if not approved:
        return blocked
    return [t for t in blocked if t not in approved]


def _call_detail(name: str, args: dict[str, Any]) -> str:
    if name == "bash" or name == "run_compute":
        blob = str(args.get("command", "") or args.get("code", "") or "")
        return blob.strip()[:200]
    if name in ("write_file", "edit_file"):
        return str(args.get("path", "") or "")
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3])


def classify_tool_call(name: str, args: dict[str, Any]) -> ApprovalRequest | None:
    """Return an :class:`ApprovalRequest` for a sensitive call, else ``None``.

    Read-only tools (read_file, grep, glob, list_dir, web_fetch, memory_search,
    docs_*, research read tools) are not sensitive and return ``None``.
    """
    if name not in SENSITIVE_TOOLS:
        return None
    args = args or {}
    if name == "bash":
        cmd = str(args.get("command", "") or "")
        risk = "destructive" if command_is_destructive(cmd) else "exec"
    elif name == "run_compute":
        risk = "exec"
    else:  # write_file / edit_file
        risk = "write"
    return ApprovalRequest(tool_name=name, arguments=args, risk=risk, detail=_call_detail(name, args))


def _allowlisted(name: str, args: dict[str, Any], allowlist: list[str], session_allow: set[str]) -> bool:
    """Whether this call is pre-approved by the owner allowlist or session set."""
    if name in session_allow:
        return True
    blob = _call_detail(name, args)
    for raw in allowlist or []:
        entry = str(raw or "").strip()
        if not entry:
            continue
        if entry in ("*", name):
            return True
        if ":" in entry:
            tool, _, prefix = entry.partition(":")
            if tool.strip() == name and blob.strip().startswith(prefix.strip()):
                return True
    return False


def _no_approver_reason(channel: str) -> str:
    if (channel or "").strip().lower() in _IM_CHANNELS:
        return (
            "sensitive tools triggered from an IM channel require local confirmation; "
            "run the request from the CLI on the owner's machine"
        )
    return (
        "approval required but no interactive approver is available "
        "(non-interactive session); re-run from a terminal, add the command to "
        "security.approval_allowlist, or set security.require_approval=false"
    )


def denied_result(name: str, reason: str) -> HostToolRejection:
    """Structured (non-raising) tool result for a blocked call.

    Mirrors the plan-policy violation shape so the ReAct loop treats it as an
    ordinary failed observation and can course-correct or relay to the user.
    """
    return _mint_host_tool_rejection(
        {
            "status": "error",
            "error": f"tool '{name}' was not run: {reason}",
            "approval_required": True,
            "tool_name": name,
            "reason": reason,
        }
    )


class ApprovalGate:
    """Authorise sensitive tool calls before they execute."""

    def __init__(
        self,
        settings: Any,
        *,
        channel: str = "cli",
        approver: Approver | None = None,
        on_event: ApprovalEventSink | None = None,
        session_allow: set[str] | None = None,
        additional_sensitive_tools: set[str] | None = None,
        preauthorizer: Preauthorizer | None = None,
    ) -> None:
        self.settings = settings
        self.channel = channel or "cli"
        self.approver = approver
        self.on_event = on_event
        self.policy = resolve_policy(settings)
        self._additional_sensitive_tools = set(additional_sensitive_tools or ())
        self._preauthorizer = preauthorizer
        # Caller-owned so "approve for session" persists across turns.
        self._session_allow: set[str] = session_allow if session_allow is not None else set()

    def wrap(self, invoker: ToolInvoker) -> ToolInvoker:
        async def guarded(name: str, args: dict[str, Any]) -> Any:
            return await self.invoke(name, args or {}, lambda: invoker(name, args))

        return guarded

    async def invoke(
        self,
        name: str,
        args: dict[str, Any],
        invoker: Callable[[], Awaitable[Any]],
        *,
        sensitive: bool = False,
    ) -> Any:
        """Authorize and invoke one call, including manifest-declared risks."""
        decision = await self._authorize(name, args or {}, force_sensitive=sensitive)
        if decision is None or decision.approved:
            return await invoker()
        return denied_result(name, decision.reason)

    async def _authorize(
        self, name: str, args: dict[str, Any], *, force_sensitive: bool = False
    ) -> ApprovalDecision | None:
        if self.policy == "never":
            return None
        req = classify_tool_call(name, args)
        if req is None and (force_sensitive or name in self._additional_sensitive_tools):
            req = ApprovalRequest(
                tool_name=name,
                arguments=args,
                risk="exec",
                detail=_call_detail(name, args),
            )
        if req is None:
            if self.policy != "always":
                return None
            req = ApprovalRequest(tool_name=name, arguments=args, risk="tool", detail=_call_detail(name, args))

        allowlist = list(getattr(getattr(self.settings, "security", None), "approval_allowlist", []) or [])
        if _allowlisted(name, args, allowlist, self._session_allow):
            await self._emit("approval.auto", req, "allowlisted")
            return ApprovalDecision(True, reason="allowlisted")

        if self._preauthorizer is not None:
            try:
                preapproved = await self._preauthorizer(name, args)
            except Exception:  # noqa: BLE001 - broken consent lookup must fail closed.
                preapproved = False
            if preapproved:
                await self._emit("approval.auto", req, "run-approved")
                return ApprovalDecision(True, reason="run-approved")

        if self.approver is None:
            reason = _no_approver_reason(self.channel)
            await self._emit("approval.denied", req, reason)
            return ApprovalDecision(False, reason=reason)

        await self._emit("approval.requested", req, "")
        # Human thinking time must not burn the turn/workflow wall-clock budget
        # (Codex treats approval as a gate *before* the timed section): pause
        # every in-scope turn clock for exactly the owner's decision latency, so
        # a slow approval can no longer time out a call that then succeeds.
        with pause_clocks():
            decision = await self.approver(req)
        if decision.approved and decision.scope == "session":
            self._session_allow.add(name)
        await self._emit(
            "approval.granted" if decision.approved else "approval.denied",
            req,
            decision.reason,
        )
        return decision

    async def _emit(self, kind: str, req: ApprovalRequest, note: str) -> None:
        if self.on_event is None:
            return
        payload = {
            "tool": req.tool_name,
            "risk": req.risk,
            "detail": req.detail,
            "note": note,
            "summary": f"{kind}: {req.tool_name} ({req.risk})",
        }
        try:
            emitted = self.on_event(kind, payload)
            if hasattr(emitted, "__await__"):
                await emitted
        except Exception:  # noqa: BLE001 — audit is best-effort, never fatal.
            pass


__all__ = [
    "SENSITIVE_TOOLS",
    "ApprovalRequest",
    "ApprovalDecision",
    "Approver",
    "Preauthorizer",
    "ApprovalGate",
    "classify_tool_call",
    "resolve_policy",
    "denied_result",
]
