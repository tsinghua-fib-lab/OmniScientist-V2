"""Shared CLI state: global options → settings → agent."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omni.agent import OmniAgent
from omni.config import OmniSettings, load_settings

if TYPE_CHECKING:
    from omni.runtime.task_object_resolver import TaskObjectResolution


@dataclass
class AppState:
    project: str | None = None
    profile: str | None = None
    model: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    # ``--trust`` / ``--no-trust`` (tri-state); resolved decision cached in
    # ``trusted`` after the first workspace-trust check.
    trust_flag: bool | None = None
    trusted: bool | None = None

    def settings(self) -> OmniSettings:
        overrides = dict(self.overrides)
        if self.model:
            overrides.setdefault("model", {})["model"] = self.model
        return load_settings(
            project=self.project,
            profile=self.profile,
            overrides=overrides,
            trusted=self.trusted,
        )


def run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def _terminal_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def resolve_workspace_trust(state: AppState, *, interactive: bool | None = None) -> bool:
    """Resolve (and cache) whether the launch directory is trusted.

    Mirrors Claude Code's folder-trust gate. The first interactive run in an
    untrusted directory prompts; the decision persists in ``~/.omni/trust.json``
    and inherits downward. Non-interactive/CI never prompts and stays restricted
    unless ``--trust`` is passed. Named ``-P`` projects and already-adopted
    in-place ``.omni`` workspaces are trusted implicitly (explicit user opt-ins
    under the user's own control), as is launching inside the omni home.
    """
    if state.trusted is not None:
        return state.trusted
    if interactive is None:
        interactive = _terminal_is_interactive()
    from omni.config import trust as trustmod
    from omni.config.paths import find_project_root, is_within_home

    # Read trust config from user/env/overrides only. ``trust.*`` is a forbidden
    # project key, so a probe load (trusted=True) cannot let a repo self-trust.
    probe = load_settings(project=state.project, profile=state.profile, trusted=True)
    tcfg = probe.trust
    home = probe.paths.home if probe.paths else None

    if not tcfg.enabled:
        state.trusted = True
    elif state.project:  # named -P project: user-created, lives under ~/.omni
        state.trusted = True
    elif home is not None and is_within_home(Path.cwd(), home):
        state.trusted = True  # inside ~/.omni: session-only, nothing to vouch for
    elif find_project_root(Path.cwd()) is not None:
        state.trusted = True  # in-place .omni already adopted → prior consent
    elif trustmod.is_trusted(Path.cwd(), home=home, allow=tcfg.allow):
        state.trusted = True
    elif state.trust_flag is True:
        trustmod.set_trusted(Path.cwd(), home=home)
        state.trusted = True
    elif state.trust_flag is False:
        state.trusted = False
    elif not interactive or tcfg.prompt == "never":
        state.trusted = False  # fail closed → restricted read-only
    else:
        state.trusted = _prompt_trust(Path.cwd(), home)
    return state.trusted


def _prompt_trust(cwd: Path, home: Path | None) -> bool:
    """Blocking first-run trust prompt (interactive terminals only).

    Display == enforcement: the prompt names the exact path trust is keyed on.
    ``set_trusted`` records ``trust_key(cwd)`` (the enclosing VCS root, else cwd),
    so when launched from a repo subdirectory we say so explicitly instead of
    implying trust is scoped to the current folder. Mirrors Codex's
    ``trust_directory.rs`` which surfaces the repo ``<root>`` before vouching.
    """
    from omni.cli.render import confirm, console
    from omni.config import trust as trustmod

    target = trustmod.trust_key(cwd)  # the path trust is actually recorded against
    console.print()
    console.print("[bold]Do you trust the files in this folder?[/bold]")
    console.print(f"  [cyan]{cwd}[/cyan]")
    if target != cwd.resolve():
        console.print(
            "  [yellow]Inside a repository — trust applies to the repo root:[/yellow] "
            f"[cyan]{target}[/cyan]"
        )
    console.print(
        "[dim]Trusted: omni writes generated files (figures/reports) here and applies "
        "this folder's .omni config.\nUntrusted: read-only — outputs stay in ~/.omni and "
        "repo-local config is ignored.[/dim]"
    )
    trusted = confirm("Trust this folder and write generated files here?", default=True)
    if trusted:
        trustmod.set_trusted(cwd, home=home)
    return trusted


async def make_agent_from_settings(settings: OmniSettings, **kwargs: Any) -> OmniAgent:
    """Build an :class:`OmniAgent` for already-resolved settings (no trust gate).

    Shared by :func:`make_agent` (CWD workspace, after the trust gate) and
    :func:`make_agent_for_task` (a *different* workspace resolved from the global
    task index), so both wire the registry entry and the interactive approver the
    same way.
    """
    agent = await OmniAgent.create(settings, **kwargs)
    try:
        from omni.config.workspaces import register_workspace

        register_workspace(agent.paths)
    except Exception:  # noqa: BLE001 — registry is advisory, never fatal
        pass
    # Human-in-the-loop approval (P0): only wire the terminal prompt when we can
    # actually ask (a TTY on both ends). Non-interactive runs leave the approver
    # None so sensitive tools fail closed rather than blocking on dead stdin.
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            from omni.cli.approval_prompt import build_cli_approver

            agent.approver = build_cli_approver()
        except Exception:  # noqa: BLE001 — approval prompt is best-effort.
            pass
    return agent


async def make_agent(state: AppState, **kwargs: Any) -> OmniAgent:
    # Workspace-trust gate (Claude Code style): resolve before building the
    # agent so an untrusted directory neither applies repo-local config nor
    # mirrors generated files into the user's folder.
    resolve_workspace_trust(state)
    return await make_agent_from_settings(state.settings(), **kwargs)


async def make_agent_for_task(
    state: AppState, ident: str, **kwargs: Any
) -> tuple[OmniAgent, bool]:
    """Build an agent bound to the workspace that owns task ``ident``.

    Cross-workspace routing for the global task index: ``omni task show <id>``
    (and friends) must open the workspace that owns the id, not merely the
    CWD-resolved one — otherwise a task listed by ``--all`` can't be shown. Falls
    back to the local workspace (unchanged behaviour) when the id isn't a known
    cross-workspace task, e.g. a subtask/workflow id or a genuinely missing id.
    Returns ``(agent, remote)`` where ``remote`` is True iff a different
    workspace was resolved.
    """
    resolve_workspace_trust(state)
    local_settings = state.settings()
    target: OmniSettings | None = None
    try:
        from omni.runtime.task_index import resolve_task_workspace

        target = await resolve_task_workspace(local_settings, ident)
    except Exception:  # noqa: BLE001 — routing is best-effort; fall back to local.
        target = None
    if target is not None and str(target.paths.project_dir) != str(
        local_settings.paths.project_dir
    ):
        return await make_agent_from_settings(target, **kwargs), True
    return await make_agent_from_settings(local_settings, **kwargs), False


async def make_agent_for_object(
    state: AppState, ident: str, **kwargs: Any
) -> tuple[OmniAgent, TaskObjectResolution, bool]:
    """Build an agent for a typed task object without hiding lookup ambiguity.

    Unlike the compatibility-oriented :func:`make_agent_for_task`, this entry
    point returns the resolver outcome alongside the agent.  Ambiguous and
    missing ids receive a local agent only so callers can close it uniformly;
    their non-``ok`` status remains authoritative and must not be reinterpreted
    as a successful local lookup.
    """
    from omni.runtime.task_object_resolver import resolve_task_object

    resolve_workspace_trust(state)
    local_settings = state.settings()
    resolution = await resolve_task_object(local_settings, ident)
    target = resolution.settings if resolution.status == "ok" else None
    selected = target or local_settings
    local_paths = local_settings.paths
    selected_paths = selected.paths
    remote = bool(
        resolution.status == "ok"
        and local_paths is not None
        and selected_paths is not None
        and selected_paths.project_dir != local_paths.project_dir
    )
    return (
        await make_agent_from_settings(selected, **kwargs),
        resolution,
        remote,
    )


async def make_agent_for_schedule(
    state: AppState, ident: str, **kwargs: Any
) -> tuple[OmniAgent, bool]:
    """Build an agent bound to the workspace that owns schedule ``ident``.

    The schedule analogue of :func:`make_agent_for_task`: ``schedule all`` lists
    schedules from every workspace, so ``schedule show/enable/disable/remove <id>``
    must open the workspace that owns the id rather than only the CWD-resolved one
    (otherwise an id seen in ``schedule all`` reports "not found"). Falls back to
    the local workspace when the id isn't a unique cross-workspace schedule.
    Returns ``(agent, remote)`` where ``remote`` is True iff a different workspace
    was resolved.
    """
    resolve_workspace_trust(state)
    local_settings = state.settings()
    target: OmniSettings | None = None
    try:
        from omni.runtime.aggregate import resolve_schedule_workspace

        target = await resolve_schedule_workspace(local_settings, ident)
    except Exception:  # noqa: BLE001 — routing is best-effort; fall back to local.
        target = None
    if target is not None and str(target.paths.project_dir) != str(
        local_settings.paths.project_dir
    ):
        return await make_agent_from_settings(target, **kwargs), True
    return await make_agent_from_settings(local_settings, **kwargs), False
