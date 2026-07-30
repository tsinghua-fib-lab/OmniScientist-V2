"""Typed, in-memory grants for one interactive approval session.

The persistent owner allowlist remains a separate compatibility surface.  These
objects model only decisions made by a live owner: exact invocations are bound
to their execution context, and optional command-family rules are accepted only
after the host verifies a literal argv prefix.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PLAIN_COMMAND_SOURCE = re.compile(
    r"[A-Za-z0-9_./:@%+=,-]+(?:[ \t]+[A-Za-z0-9_./:@%+=,-]+)*"
)
_TASK_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,39}")
_SAFE_OMNI_TASK_RULES = frozenset({"list", "ls", "show", "status", "watch"})
_PACKAGE_RUNNERS = frozenset({"npm", "pnpm", "yarn"})
_SAFE_CARGO_RULES = frozenset(
    {"bench", "build", "check", "clippy", "doc", "fmt", "metadata", "run", "test"}
)
_SAFE_GIT_RULES = frozenset({"clone", "commit", "fetch", "pull"})
_SAFE_RUFF_RULES = frozenset({"check", "format"})
_GIT_REDIRECTING_OPTIONS = frozenset(
    {"-c", "-C", "--config-env", "--exec-path", "--git-dir", "--namespace", "--work-tree"}
)


def _resolved(value: str | Path | None) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        return str(Path(value).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(value)


@dataclass(frozen=True)
class ApprovalContext:
    """Execution facts that prevent a grant from leaking into another scope."""

    working_dir: str = ""
    workspace: str = ""
    channel: str = "cli"
    bash_sandbox: str = ""
    os_sandbox: str = ""
    sandbox_network: str = ""

    def fingerprint(self) -> str:
        return _fingerprint(repr(self))


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()


def build_approval_context(
    settings: Any,
    *,
    working_dir: str | Path | None,
    workspace: str | Path | None,
    channel: str,
) -> ApprovalContext:
    security = getattr(settings, "security", None)
    return ApprovalContext(
        working_dir=_resolved(working_dir),
        workspace=_resolved(workspace),
        channel=str(channel or "cli").strip().lower(),
        bash_sandbox=str(getattr(security, "bash_sandbox", "") or ""),
        os_sandbox=str(getattr(security, "os_sandbox", "") or ""),
        sandbox_network=str(getattr(security, "sandbox_network", "") or ""),
    )


def simple_command_argv(command: str) -> tuple[str, ...] | None:
    """Return argv only when shell parsing cannot add expansion semantics."""
    text = str(command or "").strip()
    if not text or _PLAIN_COMMAND_SOURCE.fullmatch(text) is None:
        return None
    return tuple(text.split())


def _invocation_identity(name: str, args: dict[str, Any]) -> tuple[str, ...]:
    if name == "bash":
        # `/bin/sh -c` executes source, not argv. Quotes, escapes and expansion
        # are therefore part of the exact identity and must never be erased.
        return ("script", str(args.get("command", "") or "").strip())
    if name == "run_compute":
        command = str(args.get("command", "") or args.get("code", "") or "")
        return ("command", " ".join(command.split()))
    # Preserve the historical tool-wide meaning for non-command tools.
    return ("tool",)


def _legacy_scope_key(name: str, args: dict[str, Any]) -> str:
    """Identity used by the pre-store ``set[str]`` compatibility surface."""
    if name in {"bash", "run_compute"}:
        blob = str(args.get("command", "") or args.get("code", "") or "")
        return f"{name}:{' '.join(blob.split())}"
    return name


@dataclass(frozen=True)
class ExactApprovalGrant:
    tool_name: str
    invocation: tuple[str, ...]
    context: ApprovalContext | None
    risk: str = ""

    def token(self) -> str:
        """Stable token used only to keep legacy caller-owned sets working."""
        return repr((self.tool_name, self.invocation, self.context, self.risk))

    def fingerprint(self) -> str:
        return _fingerprint(self.token())


def exact_approval_grant(
    name: str,
    args: dict[str, Any],
    context: ApprovalContext,
    risk: str,
) -> ExactApprovalGrant:
    command_scoped = name == "bash"
    return ExactApprovalGrant(
        tool_name=name,
        invocation=_invocation_identity(name, args),
        context=context if command_scoped else None,
        risk=risk if command_scoped else "",
    )


@dataclass(frozen=True)
class SessionApprovalRule:
    """A validated argv prefix, bound to the context in which it was granted."""

    tool_name: str
    prefix: tuple[str, ...]
    context: ApprovalContext
    risk: str
    family: str = "argv_prefix"
    modifiers: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        return _fingerprint(
            repr(
                (
                    self.tool_name,
                    self.prefix,
                    self.context,
                    self.risk,
                    self.family,
                    self.modifiers,
                )
            )
        )

    def display_pattern(self) -> str:
        """Return the exact operation family shown to the approving owner."""
        if self.family == "omni_task_delete":
            parts = [*self.prefix, "<task-id...>", *self.modifiers]
            return " ".join(parts)
        return f"{' '.join(self.prefix)} ..."

    def matches(
        self,
        name: str,
        args: dict[str, Any],
        context: ApprovalContext,
        risk: str,
    ) -> bool:
        if name != self.tool_name or context != self.context or risk != self.risk:
            return False
        if self.family == "omni_task_delete":
            invocation = _parse_omni_task_delete(
                str(args.get("command", "") or "")
            )
            return (
                invocation is not None
                and invocation.prefix == self.prefix
                and invocation.modifiers == self.modifiers
            )
        argv = simple_command_argv(str(args.get("command", "") or ""))
        return (
            argv is not None
            and argv[: len(self.prefix)] == self.prefix
            and _safe_rule_invocation(argv)
        )


def proposed_session_rule(
    name: str,
    args: dict[str, Any],
    context: ApprovalContext,
    risk: str,
) -> SessionApprovalRule | None:
    """Validate tool-proposed prefix metadata; invalid proposals disappear.

    The metadata can only narrow the literal command currently awaiting
    approval.  It is not interpreted as shell text and is never executed.
    """
    if name != "bash":
        return None
    if risk == "destructive":
        invocation = _parse_omni_task_delete(str(args.get("command", "") or ""))
        if invocation is None:
            return None
        return SessionApprovalRule(
            name,
            invocation.prefix,
            context,
            risk,
            family="omni_task_delete",
            modifiers=invocation.modifiers,
        )
    if risk != "exec":
        return None
    raw = args.get("prefix_rule")
    if (
        not isinstance(raw, list)
        or not 2 <= len(raw) <= 16
        or not all(isinstance(item, str) and 0 < len(item) <= 128 for item in raw)
    ):
        return None
    prefix = tuple(raw)
    argv = simple_command_argv(str(args.get("command", "") or ""))
    if argv is None or len(prefix) < 2 or argv[: len(prefix)] != prefix:
        return None
    if not _supported_rule_prefix(prefix):
        return None
    return SessionApprovalRule(name, prefix, context, risk)


@dataclass(frozen=True)
class _OmniTaskDeleteInvocation:
    prefix: tuple[str, ...]
    modifiers: tuple[str, ...]


def _parse_omni_task_delete(command: str) -> _OmniTaskDeleteInvocation | None:
    """Parse one closed Omni task-delete operation for a reusable grant.

    Task ids are the only variable part. Project selection, delete verb, force
    and batch-confirmation authority stay fixed. A final ``2>&1`` is accepted
    as a semantic no-op because the bash tool already merges stderr into
    stdout; every other shell operator remains ineligible.
    """
    text = str(command or "").strip()
    redirected = re.search(r"[ \t]+2>&1$", text)
    if redirected is not None:
        text = text[: redirected.start()].rstrip()
    argv = simple_command_argv(text)
    if argv is None or not argv or argv[0] != "omni":
        return None

    index = 1
    project = ""
    if index < len(argv) and argv[index] in {"-P", "--project"}:
        if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
            return None
        project = argv[index + 1]
        index += 2
    elif index < len(argv) and argv[index].startswith("-P"):
        project = argv[index][2:]
        if not project:
            return None
        index += 1
    elif index < len(argv) and argv[index].startswith("--project="):
        project = argv[index].partition("=")[2]
        if not project:
            return None
        index += 1

    if index + 2 >= len(argv) or argv[index] != "task":
        return None
    verb = argv[index + 1]
    if verb not in {"rm", "delete"}:
        return None
    index += 2

    task_ids: list[str] = []
    force = False
    yes = False
    for token in argv[index:]:
        if token in {"--force", "-f"}:
            force = True
        elif token in {"--yes", "-y"}:
            yes = True
        elif _TASK_REFERENCE.fullmatch(token):
            task_ids.append(token)
        else:
            return None
    if not task_ids:
        return None
    # ``omni task rm id1 id2`` is a dry-run unless the owner also supplies
    # ``--yes``; a single-id invocation without ``--yes`` performs the delete.
    # Do not let approval of the weaker preview authorize that stronger shape.
    if len(task_ids) > 1 and not yes:
        return None

    prefix = ["omni"]
    if project:
        prefix.extend(("--project", project))
    prefix.extend(("task", verb))
    modifiers: list[str] = []
    if force:
        modifiers.append("--force")
    if yes:
        modifiers.append("--yes")
    return _OmniTaskDeleteInvocation(tuple(prefix), tuple(modifiers))


def _supported_rule_prefix(prefix: tuple[str, ...], *, allow_uv: bool = True) -> bool:
    """Accept only families whose suffix cannot replace the executable.

    This deliberately is not a shell lowerer. Assignments, wrappers, arbitrary
    executables, and commands such as ``find`` stay exact-only. New reusable
    families are added here one reviewed grammar at a time.
    """
    if len(prefix) < 2:
        return False
    executable = prefix[0]
    if executable in _PACKAGE_RUNNERS:
        return _specific_package_script(prefix)
    if executable in {"pytest", "py.test"}:
        return _pytest_args_are_reusable(prefix[1:])
    if executable == "ruff":
        return prefix[1] in _SAFE_RUFF_RULES
    if executable == "cargo":
        return prefix[1] in _SAFE_CARGO_RULES
    if executable == "git":
        return _specific_git_operation(prefix)
    if executable == "omni":
        return _safe_omni_task_prefix(prefix)
    if executable == "uv" and allow_uv:
        return _specific_uv_program(prefix)
    return False


def _safe_rule_invocation(argv: tuple[str, ...]) -> bool:
    """Validate suffix-sensitive hazards each time a stored rule is matched."""
    nested = argv[2:] if argv[:2] == ("uv", "run") else argv
    if nested and nested[0] in {"pytest", "py.test"}:
        return _pytest_args_are_reusable(nested[1:])
    return True


def _pytest_args_are_reusable(args: tuple[str, ...]) -> bool:
    return not any(
        token == "--basetemp" or token.startswith("--basetemp=") for token in args
    )


def _specific_package_script(prefix: tuple[str, ...]) -> bool:
    return len(prefix) >= 3 and prefix[1] == "run" and not prefix[2].startswith("-")


def _specific_uv_program(prefix: tuple[str, ...]) -> bool:
    return (
        len(prefix) >= 4
        and prefix[1] == "run"
        and _supported_rule_prefix(prefix[2:], allow_uv=False)
    )


def _specific_git_operation(prefix: tuple[str, ...]) -> bool:
    if len(prefix) < 2:
        return False
    for token in prefix[1:]:
        option = token.split("=", 1)[0]
        if option in _GIT_REDIRECTING_OPTIONS:
            return False
        if not token.startswith("-"):
            return token in _SAFE_GIT_RULES
    return False


def _safe_omni_task_prefix(prefix: tuple[str, ...]) -> bool:
    """Allow only a complete read-only `omni task` operation as a rule."""
    index = 1
    while index < len(prefix):
        token = prefix[index]
        if token in {"-P", "--project"}:
            index += 2
            continue
        if token.startswith("--project="):
            index += 1
            continue
        break
    return (
        index + 1 < len(prefix)
        and prefix[index] == "task"
        and prefix[index + 1] in _SAFE_OMNI_TASK_RULES
    )


@dataclass(frozen=True)
class TaskBashApprovalGrant:
    """Consent to skip later workspace-command prompts for one live task.

    This is not Codex Full Access: the workspace sandbox, system hard-blocks,
    tool policy, and every other host guard still run. It answers "must the
    owner see another ``bash`` / ``run_compute`` prompt on this task?" Writes
    stay on destination assessment and cannot use this grant to leave the
    workspace. The grant is bound to the task and the execution context it was
    given in, so a later turn, a changed sandbox, or an IM channel cannot
    inherit it.
    """

    task_id: str
    context: ApprovalContext

    def fingerprint(self) -> str:
        return _fingerprint(repr((self.task_id, self.context)))

    def matches(self, name: str, *, task_id: str, context: ApprovalContext) -> bool:
        # IM never inherits workspace trust, even if a grant was planted with an
        # IM context. The gate also refuses to publish on those channels.
        if context.channel in {"wechat", "feishu", "dingtalk"}:
            return False
        return (
            name in {"bash", "run_compute"}
            and bool(self.task_id)
            and self.task_id == task_id
            and self.context == context
        )


@dataclass
class SessionApprovalStore:
    """Owner grants and a FIFO-shaped prompt mutex for one live session."""

    exact_grants: set[ExactApprovalGrant] = field(default_factory=set)
    rules: list[SessionApprovalRule] = field(default_factory=list)
    task_bash_grants: list[TaskBashApprovalGrant] = field(default_factory=list)
    legacy_grants: set[str] | None = None
    prompt_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def match(
        self,
        name: str,
        args: dict[str, Any],
        context: ApprovalContext,
        risk: str,
        *,
        task_id: str = "",
    ) -> tuple[str, str] | None:
        """Return grant kind + non-secret fingerprint for audit, if allowed."""
        exact = exact_approval_grant(name, args, context, risk)
        if exact in self.exact_grants:
            return "exact", exact.fingerprint()
        if self.legacy_grants is not None:
            if (
                exact.token() in self.legacy_grants
                or _legacy_scope_key(name, args) in self.legacy_grants
            ):
                return "exact", exact.fingerprint()
        for rule in self.rules:
            if rule.matches(name, args, context, risk):
                return "rule", rule.fingerprint()
        for grant in self.task_bash_grants:
            if grant.matches(name, task_id=task_id, context=context):
                return "task_bash", grant.fingerprint()
        return None

    def grant_exact(
        self,
        name: str,
        args: dict[str, Any],
        context: ApprovalContext,
        risk: str,
    ) -> ExactApprovalGrant:
        grant = exact_approval_grant(name, args, context, risk)
        self.exact_grants.add(grant)
        if self.legacy_grants is not None:
            self.legacy_grants.add(_legacy_scope_key(name, args))
        return grant

    def grant_rule(self, rule: SessionApprovalRule) -> None:
        if rule not in self.rules:
            self.rules.append(rule)

    def grant_task_bash(
        self, task_id: str, context: ApprovalContext
    ) -> TaskBashApprovalGrant:
        grant = TaskBashApprovalGrant(
            task_id=str(task_id or "").strip(), context=context
        )
        if grant.task_id and grant not in self.task_bash_grants:
            self.task_bash_grants.append(grant)
        return grant

    def revoke_task_bash(self, task_id: str) -> None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        self.task_bash_grants = [
            grant for grant in self.task_bash_grants if grant.task_id != task_id
        ]


__all__ = [
    "ApprovalContext",
    "SessionApprovalRule",
    "SessionApprovalStore",
    "TaskBashApprovalGrant",
    "build_approval_context",
    "exact_approval_grant",
    "proposed_session_rule",
    "simple_command_argv",
]
