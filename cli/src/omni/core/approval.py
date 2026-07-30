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
  and an owner-curated ``security.approval_allowlist``. A typed, in-memory
  session store remembers context-bound exact grants, validated prefix rules,
  and optional task-scoped Bash trust (Approve all) that never outlives the
  live CLI task or disables the sandbox.
* When no interactive approver is wired (daemon / IM / non-interactive run) a
  sensitive call **fails closed** with an actionable, structured tool result —
  never a raised exception — so the model can relay the block to the user and
  a batch/remote context can't silently run destructive work.
* ``omni exec`` is the Codex-``Never`` exception: ``workspace_auto`` auto-approves
  in-workspace writes and sandboxed CLI ``bash`` / ``run_compute``. Escapes,
  system-blocked commands, and IM channels still fail closed without prompting.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from omni.channels.security import is_im_channel
from omni.core.approval_rules import (
    ApprovalContext,
    SessionApprovalRule,
    SessionApprovalStore,
    TaskBashApprovalGrant,
    build_approval_context,
    exact_approval_grant,
    proposed_session_rule,
)
from omni.core.sensitive_paths import is_sensitive_path, is_write_protected_path
from omni.core.tool_result import HostToolRejection, _mint_host_tool_rejection
from omni.core.turn_clock import pause_clocks
from omni.skills_runtime.builtin_tools.shell import (
    command_is_destructive,
    command_is_known_safe,
)

ToolInvoker = Callable[[str, dict[str, Any]], Awaitable[Any]]

# Mutating / executing tools that leave the read-only "just look" envelope.
SENSITIVE_TOOLS = frozenset({"bash", "write_file", "edit_file", "run_compute"})

# The subset whose risk is fully described by an argument: these name the file
# they will change, so the gate can rule on them from the destination alone. A
# shell command names nothing, which is why bash and run_compute are excluded.
PATH_ASSESSED_TOOLS = frozenset({"write_file", "edit_file"})

@dataclass(frozen=True)
class ApprovalChoice:
    """One decision the approval boundary permits the UI to offer."""

    value: str
    label: str


@dataclass(frozen=True)
class ApprovalRequest:
    """One sensitive tool call awaiting the owner's decision."""

    tool_name: str
    arguments: dict[str, Any]
    risk: str  # write | exec | destructive | tool
    detail: str = ""  # command / path preview shown to the human
    choices: tuple[ApprovalChoice, ...] = ()
    session_rule: SessionApprovalRule | None = None
    task_id: str = ""


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    scope: str = "once"  # once | session (exact) | rule | task_bash
    reason: str = ""


# An approver turns a request into a decision (e.g. a terminal prompt). It is
# ``None`` in non-interactive contexts, which makes sensitive calls fail closed.
Approver = Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]

# Optional sink for audit events: ``on_event(kind, payload)`` may be async.
ApprovalEventSink = Callable[[str, dict[str, Any]], Any]
Preauthorizer = Callable[[str, dict[str, Any]], Awaitable[bool]]


def sandbox_is_write_capable(settings: Any) -> bool:
    """Whether the coarse sandbox may write inside the workspace.

    Codex ``workspace-write`` / ``danger-full-access`` are write-capable.
    ``read-only`` is not — OnRequest then asks (or fail-closes) before exec
    or a file write, instead of treating the destination as already settled.
    """
    mode = str(
        getattr(getattr(settings, "security", None), "bash_sandbox", "workspace-write")
        or "workspace-write"
    ).strip().lower()
    return mode in {"workspace-write", "full"}


def resolve_policy(settings: Any) -> str:
    """Resolve the effective approval policy string.

    ``require_approval=False`` collapses to ``"never"`` (full autonomy);
    otherwise the configured ``approval_policy`` is normalised. ``on-request``
    is Codex OnRequest (its own policy, not an alias of ``untrusted``).
    Unknown values are treated as the safe default.
    """
    sec = getattr(settings, "security", None)
    if sec is None or not bool(getattr(sec, "require_approval", True)):
        return "never"
    policy = str(getattr(sec, "approval_policy", "untrusted") or "untrusted").strip().lower()
    if policy in ("on-request", "on_request", "onrequest"):
        return "on-request"
    if policy in ("never", "untrusted", "always"):
        return policy
    return "untrusted"


def reconcile_sensitive_visibility(
    blocked_tools: Sequence[str],
    *,
    gate_can_clear: bool,
    approved: frozenset[str] | set[str] = frozenset(),
    path_assessed_can_clear: bool = False,
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
    * ``path_assessed_can_clear`` — no human is available, but the gate can
      still settle a *file write* by itself, because a write states its
      destination and one inside the workspace is auto-approved. Only
      ``PATH_ASSESSED_TOOLS`` are freed. ``bash`` and ``run_compute`` are not:
      a command does not say what it will touch, so there is nothing to assess
      and no way to clear it without asking. Codex draws the line in the same
      place — ``assess_patch_safety`` decides a patch by its paths, while exec
      approval stays coarse in ``default_exec_approval_requirement``.
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
    clearable = set(approved or ())
    if path_assessed_can_clear:
        clearable |= PATH_ASSESSED_TOOLS
    if not clearable:
        return blocked
    return [t for t in blocked if t not in clearable]


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


def write_target(name: str, args: dict[str, Any]) -> str:
    """The path a write-risk call would land on, or "" if the call has none."""
    if name not in ("write_file", "edit_file"):
        return ""
    return str((args or {}).get("path", "") or "")


def within_roots(path: Path, roots: Sequence[Path]) -> bool:
    """Whether ``path`` resolves inside one of ``roots``."""
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def session_scope_key(name: str, args: dict[str, Any]) -> str:
    """Legacy identity retained for callers that still own a plain string set.

    A command states what it will do, so the consent the owner gave is about
    that command — not about the shell. Remembering the *tool* meant one "yes"
    to ``git log`` silently said yes to every later command in the session,
    ``rm -rf`` included: the grant outgrew the sentence the owner had read.
    New stores use :mod:`omni.core.approval_rules`, whose key also binds the
    working directory, workspace, channel, sandbox, and risk. Tools whose risk
    is not carried in a command keep their historical tool-wide scope.
    """
    if name in ("bash", "run_compute"):
        blob = str(args.get("command", "") or args.get("code", "") or "")
        return f"{name}:{' '.join(blob.split())}"
    return name


def _allowlisted(name: str, args: dict[str, Any], allowlist: list[str]) -> bool:
    """Whether this call is pre-approved by the persistent owner allowlist."""
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


def _no_approver_reason(channel: str, *, risk: str = "") -> str:
    """Why a call that needed a human was refused instead.

    Codex ``assess_patch_safety`` rejects an out-of-project patch with
    ``writing outside of the project`` when nobody can be asked. A write that
    escaped the workspace is that case — telling the model to rerun from the
    CLI made it paste a forensics essay into WeChat instead of writing a
    deliverable under a bare filename. Shell/exec still needs a local
    confirmation on IM, because a command names nothing to assess.
    """
    if risk == "write":
        return "writing outside of the project; rejected by user approval settings"
    if is_im_channel(channel):
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
        session_allow: SessionApprovalStore | set[str] | None = None,
        additional_sensitive_tools: set[str] | None = None,
        preauthorizer: Preauthorizer | None = None,
        writable_roots: Sequence[Path] | None = None,
        output_roots: Sequence[Path] | None = None,
        working_dir: Path | str | None = None,
        workspace: Path | str | None = None,
        task_id: str = "",
        workspace_auto: bool = False,
    ) -> None:
        self.settings = settings
        self.channel = channel or "cli"
        self.approver = approver
        self.on_event = on_event
        self.policy = resolve_policy(settings)
        self.workspace_auto = bool(workspace_auto)
        self._additional_sensitive_tools = set(additional_sensitive_tools or ())
        self._preauthorizer = preauthorizer
        # The write envelope this turn already operates in. Empty ⇒ unknown, and
        # an unknown envelope prompts for everything, as before.
        self._writable_roots = [Path(root).expanduser().resolve() for root in (writable_roots or ())]
        # The gate must read the protected set exactly as the write tools do, or
        # it grants a write the tool then refuses (or asks about one the tool
        # would take without a word).
        self._output_roots = [Path(root).expanduser().resolve() for root in (output_roots or ())]
        # Caller-owned so session grants and the approval queue span every gate
        # created for the same conversational session.
        if isinstance(session_allow, SessionApprovalStore):
            self._session_approvals = session_allow
        else:
            self._session_approvals = SessionApprovalStore(legacy_grants=session_allow)
        self._working_dir = working_dir
        self._workspace = workspace
        self._task_id = str(task_id or "").strip()
        self._approval_context_cache: ApprovalContext | None = None

    @property
    def _approval_context(self) -> ApprovalContext:
        """Resolve the context-bound grant key only when approval needs it."""
        if self._approval_context_cache is None:
            self._approval_context_cache = build_approval_context(
                self.settings,
                working_dir=self._working_dir,
                workspace=self._workspace,
                channel=self.channel,
            )
        return self._approval_context_cache

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
        session_match = self._session_approvals.match(
            name, args, self._approval_context, req.risk, task_id=self._task_id
        )
        if session_match is not None:
            grant_kind, grant_fingerprint = session_match
            note = "task-bash-grant" if grant_kind == "task_bash" else "session-grant"
            scope = "task-bash-grant" if grant_kind == "task_bash" else f"{grant_kind}-session"
            await self._emit(
                "approval.auto",
                req,
                note,
                approval_scope=scope,
                grant_kind=grant_kind,
                grant_fingerprint=grant_fingerprint,
            )
            return ApprovalDecision(
                True,
                scope="task_bash" if grant_kind == "task_bash" else "session",
                reason=note,
            )
        if _allowlisted(name, args, allowlist):
            await self._emit(
                "approval.auto", req, "allowlisted", approval_scope="persistent"
            )
            return ApprovalDecision(True, reason="allowlisted")

        if self._write_stays_inside(req, args):
            await self._emit("approval.auto", req, "within-workspace")
            return ApprovalDecision(True, reason="within-workspace")

        if self._reports_without_changing(req, args):
            await self._emit("approval.auto", req, "known-safe-read")
            return ApprovalDecision(True, reason="known-safe-read")

        if self._on_request_allows_exec(req):
            await self._emit("approval.auto", req, "on-request")
            return ApprovalDecision(True, reason="on-request")

        if self._workspace_auto_exec(req):
            await self._emit("approval.auto", req, "workspace-auto")
            return ApprovalDecision(True, reason="workspace-auto")

        write_escapes = self._write_leaves_envelope(req, args)
        if self._workspace_auto_enabled() and write_escapes:
            reason = _no_approver_reason(self.channel, risk="write")
            await self._emit("approval.denied", req, reason)
            return ApprovalDecision(False, reason=reason)

        # A task grant names tools, not destinations. It must not widen the
        # write envelope past the workspace the turn already operates in.
        if self._preauthorizer is not None and not write_escapes:
            try:
                preapproved = await self._preauthorizer(name, args)
            except Exception:  # noqa: BLE001 - broken consent lookup must fail closed.
                preapproved = False
            if preapproved:
                await self._emit("approval.auto", req, "run-approved")
                return ApprovalDecision(True, reason="run-approved")

        if self.approver is None:
            reason = _no_approver_reason(self.channel, risk=req.risk)
            await self._emit("approval.denied", req, reason)
            return ApprovalDecision(False, reason=reason)

        req = self._with_choices(req, args)
        # Re-check after waiting so the first decision can satisfy a queued
        # duplicate instead of the TUI denying it merely because another modal
        # is visible. Only the human decision itself pauses execution clocks.
        async with self._session_approvals.prompt_lock:
            session_match = self._session_approvals.match(
                name, args, self._approval_context, req.risk, task_id=self._task_id
            )
            if session_match is not None:
                grant_kind, grant_fingerprint = session_match
                note = (
                    "task-bash-grant"
                    if grant_kind == "task_bash"
                    else "session-grant-after-wait"
                )
                scope = "task-bash-grant" if grant_kind == "task_bash" else f"{grant_kind}-session"
                await self._emit(
                    "approval.auto",
                    req,
                    note,
                    approval_scope=scope,
                    grant_kind=grant_kind,
                    grant_fingerprint=grant_fingerprint,
                )
                return ApprovalDecision(
                    True,
                    scope="task_bash" if grant_kind == "task_bash" else "session",
                    reason=note,
                )
            await self._emit("approval.requested", req, "")
            # Preserve the existing budget contract: only the owner's actual
            # decision latency is outside turn/workflow clocks. Queue and audit
            # coordination remain ordinary host overhead.
            if bool(getattr(self.approver, "_omni_manages_turn_clock", False)):
                decision = await self.approver(req)
            else:
                with pause_clocks():
                    decision = await self.approver(req)
            audit_scope = "once" if decision.approved else "denied"
            grant_kind = ""
            grant_fingerprint = ""
            publish_exact = False
            publish_rule: SessionApprovalRule | None = None
            publish_task_bash = False
            if decision.approved and decision.scope == "session":
                grant = exact_approval_grant(
                    name, args, self._approval_context, req.risk
                )
                audit_scope = "exact-session"
                grant_kind = "exact"
                grant_fingerprint = grant.fingerprint()
                publish_exact = True
            elif decision.approved and decision.scope == "rule" and req.session_rule:
                audit_scope = "rule-session"
                grant_kind = "rule"
                grant_fingerprint = req.session_rule.fingerprint()
                publish_rule = req.session_rule
            elif (
                decision.approved
                and decision.scope == "task_bash"
                and req.tool_name in {"bash", "run_compute"}
                and self._task_bash_eligible()
            ):
                grant = TaskBashApprovalGrant(
                    task_id=self._task_id, context=self._approval_context
                )
                audit_scope = "task-bash-grant"
                grant_kind = "task_bash"
                grant_fingerprint = grant.fingerprint()
                publish_task_bash = True
            # Record the owner's decision before publishing reusable authority.
            # A sibling cannot observe the grant until this event completes,
            # and the prompt lock keeps it from opening a duplicate modal in
            # that interval. A wide Bash grant is published only if that
            # record succeeded: an un-audited "approve all" is worse than
            # asking again.
            recorded = await self._emit(
                "approval.granted" if decision.approved else "approval.denied",
                req,
                decision.reason,
                approval_scope=audit_scope,
                grant_kind=grant_kind,
                grant_fingerprint=grant_fingerprint,
            )
            if publish_exact:
                self._session_approvals.grant_exact(
                    name, args, self._approval_context, req.risk
                )
            elif publish_rule is not None:
                self._session_approvals.grant_rule(publish_rule)
            elif publish_task_bash and recorded:
                self._session_approvals.grant_task_bash(
                    self._task_id, self._approval_context
                )
            elif publish_task_bash:
                decision = replace(
                    decision,
                    scope="once",
                    reason="task-bash-audit-failed",
                )
        return decision

    def _with_choices(self, req: ApprovalRequest, args: dict[str, Any]) -> ApprovalRequest:
        rule = proposed_session_rule(
            req.tool_name,
            args,
            self._approval_context,
            req.risk,
        )
        choices = [ApprovalChoice("approve", "Approve once")]
        if rule is not None:
            choices.append(
                ApprovalChoice(
                    "approve_rule",
                    f"Approve `{rule.display_pattern()}` for this session",
                )
            )
        elif req.tool_name != "bash":
            if req.tool_name == "run_compute":
                label = "Approve this exact command for this session"
            else:
                label = f"Approve {req.tool_name} for this session"
            choices.append(ApprovalChoice("approve_session", label))
        if req.tool_name in {"bash", "run_compute"} and self._task_bash_eligible():
            choices.append(
                ApprovalChoice("approve_all_bash", "Approve this turn's workspace")
            )
        choices.append(ApprovalChoice("deny", "Deny"))
        return replace(
            req,
            choices=tuple(choices),
            session_rule=rule,
            task_id=self._task_id,
        )

    def _task_bash_eligible(self) -> bool:
        """Whether this gate may offer or publish a task-scoped workspace grant."""
        return bool(self._task_id) and not is_im_channel(self.channel)

    def _workspace_auto_enabled(self) -> bool:
        """Whether this CLI turn may settle sandboxed mutations without a prompt."""
        return self.workspace_auto and not is_im_channel(self.channel)

    def _workspace_auto_exec(self, req: ApprovalRequest) -> bool:
        """Whether workspace-auto may run this command inside the sandbox.

        Codex ``exec`` / Never still honours the sandbox: a write-capable
        workspace auto-approves sandboxed ``bash`` / ``run_compute``. Read-only
        (untrusted folder) does not widen — only known-safe reporting runs
        without a prompt, via ``_reports_without_changing``.
        """
        return (
            self._workspace_auto_enabled()
            and req.tool_name in {"bash", "run_compute"}
            and sandbox_is_write_capable(self.settings)
        )

    def _on_request_allows_exec(self, req: ApprovalRequest) -> bool:
        """Codex OnRequest: non-destructive exec in a write-capable sandbox.

        ``untrusted`` stays UnlessTrusted and still asks. ``dot`` is not
        known-safe — this is the unmatched-command path that lets a
        workspace-write turn render a figure without a modal.
        """
        if self.policy != "on-request":
            return False
        if req.risk in {"destructive", "write", "tool"}:
            return False
        if req.tool_name not in {"bash", "run_compute"}:
            return False
        return sandbox_is_write_capable(self.settings)

    def _reports_without_changing(self, req: ApprovalRequest, args: dict[str, Any]) -> bool:
        """Whether this shell call only reports on the tree it runs in.

        The gate knew a shell call from a file write but not one command from
        another, so reading the log was interrogated as hard as deleting the
        tree. Codex settles the same question with ``is_known_safe_command``,
        and only under its ``untrusted`` policy — ``always`` means the owner
        asked to see everything, so it still asks.
        """
        if req.tool_name != "bash" or self.policy == "always":
            return False
        return command_is_known_safe(str(args.get("command", "") or ""))

    def _write_leaves_envelope(self, req: ApprovalRequest, args: dict[str, Any]) -> bool:
        """True only when this write can be shown to leave the workspace.

        An unknown envelope (no writable roots) is not an escape — the caller
        may still have a task grant, as scheduled runs do. A bare filename
        lands in the workspace by construction.
        """
        if req.risk != "write" or not self._writable_roots:
            return False
        target = write_target(req.tool_name, args)
        if not target:
            return False
        path = self._resolve_write_destination(target)
        if path is None:
            return False
        if path.parent == Path("."):
            return False
        if is_write_protected_path(path, self._output_roots) or is_sensitive_path(path):
            return True
        if within_roots(path, self._writable_roots):
            return False
        if self._output_roots and within_roots(path, self._output_roots):
            return False
        return True

    def _write_stays_inside(self, req: ApprovalRequest, args: dict[str, Any]) -> bool:
        """Whether this write lands in the workspace the turn already works in.

        Asking the owner to confirm a file the agent writes into their own
        working directory taxes the ordinary case — writing a draft — at the same
        rate as the dangerous one, and a prompt that fires every time is a prompt
        that stops being read. Codex draws this line by destination rather than by
        tool name (``assess_patch_safety`` auto-approves a patch whose every path
        is under a writable root, and asks only when one escapes); this is the
        same line. ``always`` still asks for everything, escaping the workspace
        still asks, and a protected subdirectory is refused by the tool outright.

        What makes a destination boring is that it is a document. An SSH key or a
        ``.env`` sitting inside the workspace is neither, and the fast path used
        to wave those through on the strength of their location alone — the write
        tools then refused them, so nothing was lost, but the gate had already
        recorded a grant for a call it should have stopped.
        """
        if req.risk != "write" or not self._writable_roots:
            return False
        if not sandbox_is_write_capable(self.settings):
            return False
        if self.policy == "always" and not self._workspace_auto_enabled():
            return False
        target = write_target(req.tool_name, args)
        if not target:
            return False
        path = self._resolve_write_destination(target)
        if path is None:
            return False
        if is_write_protected_path(path, self._output_roots) or is_sensitive_path(path):
            return False
        # A bare filename is resolved by the write tools into the workspace
        # (``resolve_write_target``), so it is in-workspace by construction.
        # Resolving it here against the process directory would answer a question
        # nobody asked and could disagree with where the file actually lands.
        if path.parent == Path("."):
            return True
        if within_roots(path, self._writable_roots):
            return True
        return bool(self._output_roots) and within_roots(path, self._output_roots)

    def _resolve_write_destination(self, raw: str) -> Path | None:
        """Where the gate believes this write lands, using the turn directory.

        Codex ``assess_patch_safety`` prefixes relative patch paths with the
        turn cwd, not the process directory. ``omni serve`` is often launched
        from a git checkout while an IM turn's workspace is
        ``~/.omni/projects/<name>``; judging ``artifacts/paper.md`` against the
        serve cwd made an ordinary deliverable look like an escape.
        """
        text = str(raw or "").strip()
        if not text:
            return None
        path = Path(text).expanduser()
        if path.parent == Path("."):
            return path
        if not path.is_absolute() and self._working_dir:
            return Path(self._working_dir).expanduser() / path
        return path

    async def _emit(
        self,
        kind: str,
        req: ApprovalRequest,
        note: str,
        *,
        approval_scope: str = "",
        grant_kind: str = "",
        grant_fingerprint: str = "",
    ) -> bool:
        if self.on_event is None:
            return True
        payload = {
            "tool": req.tool_name,
            "risk": req.risk,
            "detail": req.detail,
            "note": note,
            "summary": f"{kind}: {req.tool_name} ({req.risk})",
            "context_fingerprint": self._approval_context.fingerprint(),
        }
        if approval_scope:
            payload["approval_scope"] = approval_scope
        if grant_kind:
            payload["grant_kind"] = grant_kind
        if grant_fingerprint:
            payload["grant_fingerprint"] = grant_fingerprint
        if self._task_id:
            payload["task_id"] = self._task_id
        try:
            emitted = self.on_event(kind, payload)
            if hasattr(emitted, "__await__"):
                await emitted
        except Exception:  # noqa: BLE001 — audit is best-effort, never fatal.
            return False
        return True


__all__ = [
    "SENSITIVE_TOOLS",
    "ApprovalChoice",
    "ApprovalRequest",
    "ApprovalDecision",
    "Approver",
    "Preauthorizer",
    "ApprovalGate",
    "classify_tool_call",
    "resolve_policy",
    "sandbox_is_write_capable",
    "denied_result",
    "session_scope_key",
]
