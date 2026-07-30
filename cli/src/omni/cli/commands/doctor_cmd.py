"""`omni doctor` — environment & configuration diagnostics."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import typer

from omni.cli.render import console, data_table
from omni.cli.state import AppState, make_agent, run_async
from omni.cli.terminal_harness import inspect_terminal
from omni.config.paths import OmniPaths
from omni.core.vlm import VlmGateway
from omni.runtime.uninstall import (
    InstallationRecord,
    current_installation,
    detect_installations,
    omni_entrypoints_on_path,
)

app = typer.Typer(help="Environment and configuration diagnostics.")

_OK, _WARN, _FAIL = "[green]OK[/green]", "[yellow]WARN[/yellow]", "[red]FAIL[/red]"


def _launcher_version(path: Path) -> str:
    """Read one launcher's version without allowing a diagnostic to hang."""
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown version"
    first = (result.stdout or result.stderr).strip().splitlines()
    return first[0] if result.returncode == 0 and first else "unknown version"


def _resolved(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    try:
        return candidate.resolve()
    except OSError:
        return candidate


def _installation_rows(
    paths: OmniPaths,
) -> tuple[InstallationRecord, list[Path], list[InstallationRecord]]:
    """Collect active owner, PATH launchers, and existing non-current copies."""
    active = current_installation(paths)
    launchers = omni_entrypoints_on_path()
    others = [
        row
        for row in detect_installations(paths, all_installations=True)
        if not row.current
        and row.executable
        and Path(row.executable).expanduser().exists()
    ]
    return active, launchers, others


@app.callback(invoke_without_command=True)
def doctor(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    state: AppState = ctx.obj
    s = state.settings()
    checks: list[list[str]] = []

    py = sys.version_info
    checks.append(["Python", _OK if py >= (3, 11) else _FAIL, f"{py.major}.{py.minor}.{py.micro}"])

    from omni import __version__
    checks.append(["OmniScientist", _OK, __version__])

    active_install, path_launchers, other_installs = _installation_rows(s.paths)
    checks.append(["Active executable", _OK, active_install.executable])
    checks.append(
        [
            "Install owner",
            _OK,
            f"method={active_install.method}; python={active_install.python}; prefix={sys.prefix}",
        ]
    )

    active_resolved = _resolved(active_install.executable)
    if path_launchers:
        order = "; ".join(
            f"{index}. {path} ({_launcher_version(path)})"
            for index, path in enumerate(path_launchers, start=1)
        )
        active_on_path = any(_resolved(path) == active_resolved for path in path_launchers)
        checks.append(["Omni PATH order", _OK if active_on_path else _WARN, order])
    else:
        checks.append(
            [
                "Omni PATH order",
                _WARN,
                "no omni launcher on PATH; run `uv tool update-shell` and restart the shell",
            ]
        )

    conflicting_paths = {
        str(path)
        for path in path_launchers
        if _resolved(path) != active_resolved
    }
    conflicting_paths.update(
        row.executable
        for row in other_installs
        if row.executable and _resolved(row.executable) != active_resolved
    )
    if conflicting_paths:
        checks.append(
            [
                "Conflicting installs",
                _WARN,
                "; ".join(sorted(conflicting_paths))
                + " (re-run the installer and choose migrate, or use `omni uninstall --all-installations`)",
            ]
        )
    else:
        checks.append(["Conflicting installs", _OK, "none detected"])

    terminal = inspect_terminal()
    checks.append(
        [
            "Host terminal",
            _OK if terminal.interactive else _WARN,
            f"{terminal.host_terminal}; platform={terminal.platform}; TERM={terminal.term}",
        ]
    )
    if terminal.tmux.active:
        tmux_detail = (
            f"{terminal.tmux.version or 'tmux'}; extended-keys="
            f"{terminal.tmux.extended_keys or 'unknown'}; allow-passthrough="
            f"{terminal.tmux.allow_passthrough or 'unknown'}"
        )
        checks.append(["tmux keyboard", _OK if terminal.shift_enter_ready else _WARN, tmux_detail])
    else:
        checks.append(["tmux keyboard", _OK, "not active"])
    keyboard_detail = (
        "Shift+Enter available; Ctrl+J is the portable fallback; "
        "repair with `omni terminal setup`"
    )
    if not terminal.shift_enter_ready:
        keyboard_detail = "; ".join(terminal.issues) + f"; run `{terminal.repair_command}`"
    checks.append(
        ["Extended keyboard", _OK if terminal.shift_enter_ready else _WARN, keyboard_detail]
    )
    checks.append(["Terminal repair", _OK, "omni terminal setup"])

    # data dir writable + db
    try:
        s.paths.ensure_dirs()
        writable = _OK
    except Exception as exc:  # noqa: BLE001
        writable = _FAIL
        console.print(f"[red]{exc}[/red]")
    checks.append(["Data dir", writable, str(s.paths.home)])
    if s.paths.secrets_file.exists() and os.name != "nt":
        mode = stat.S_IMODE(s.paths.secrets_file.stat().st_mode)
        checks.append(
            [
                "Secrets permissions",
                _OK if mode == 0o600 else _WARN,
                f"{oct(mode)}" if mode == 0o600 else f"{oct(mode)}; rewrite a secret with `omni config` to repair to 0o600",
            ]
        )

    async def _runtime_checks():
        from collections import Counter

        agent = await make_agent(state)
        db_ok = await agent.db.healthcheck()
        entries = agent.registry.list_all()
        n_skills = len(entries)
        n_async = len(agent.registry.list_async_skills())
        by_source = Counter(e.source for e in entries)
        n_shadowed = len(agent.registry.shadowed_entries())
        try:
            stale = await agent.tasks.list_stale_active_tasks(
                stale_after_s=s.tasks.interrupt_stale_after_s
            )
            n_stale = len(stale)
        except Exception:  # noqa: BLE001 — hygiene check must never break doctor
            n_stale = 0
        await agent.aclose()
        return db_ok, n_skills, n_async, n_stale, dict(by_source), n_shadowed

    db_ok, n_skills, n_async, n_stale, by_source, n_shadowed = run_async(_runtime_checks())
    checks.append(["Database (SQLite)", _OK if db_ok else _FAIL, str(s.paths.project_db)])
    source_detail = ", ".join(f"{src} {cnt}" for src, cnt in sorted(by_source.items())) or "none"
    checks.append(["Skills discovered", _OK if n_skills else _WARN, f"{n_skills} ({n_async} async) · {source_detail}"])
    if n_shadowed:
        checks.append(
            ["Skills shadowed", _OK, f"{n_shadowed} same-named skill(s) overridden by higher priority; `omni skills why <capability>`"]
        )

    # Task hygiene: running/recovering tasks whose process looks dead. They are
    # settled to `interrupted` by `omni serve` / `omni task drain` housekeeping.
    if s.tasks.interrupt_stale_after_s <= 0:
        checks.append(["Task hygiene", _WARN, "stale-task reconcile disabled (tasks.interrupt_stale_after_s = 0)"])
    elif n_stale:
        checks.append(
            [
                "Task hygiene",
                _WARN,
                f"{n_stale} task(s) look stuck in running; start `omni serve` or run "
                "`omni task drain` to settle them as interrupted",
            ]
        )
    else:
        retention = f"retention {s.tasks.retention_days}d" if s.tasks.retention_days > 0 else "retention off"
        checks.append(["Task hygiene", _OK, f"no stale running tasks ({retention})"])

    # model
    if s.model.provider == "mock":
        checks.append(
            [
                "Model",
                _WARN,
                "mock (offline placeholder); configure a model with "
                "`omni config set model.provider openai`",
            ]
        )
    else:
        has_key = bool(s.model.api_key)
        checks.append(["Model", _OK if has_key else _WARN,
                       f"{s.model.provider} / {s.model.model} {'(key set)' if has_key else '(no key!)'}"])

    # VLM is optional globally. Its owning configuration gets one diagnostic;
    # individual skill requirements are enforced only at execution time.
    vlm = s.vlm
    if not vlm.enabled:
        checks.append(
            [
                "VLM",
                _OK,
                "not configured (optional); visual skills can be enabled with `omni config vlm`",
            ]
        )
    else:
        service = VlmGateway(vlm)
        missing = list(service.missing)
        invalid = service.configuration_error
        if not missing and not invalid:
            detail = (
                f"{vlm.model} @ {vlm.endpoint} ({vlm.protocol}; key set; "
                "verify with `omni config vlm --test`)"
            )
        else:
            detail = invalid or "missing " + ", ".join(missing) + "; run `omni config vlm`"
        checks.append(["VLM", _OK if not missing and not invalid else _WARN, detail])

    # MCP extra
    try:
        import mcp  # noqa: F401
        checks.append(["MCP support", _OK, "mcp installed"])
    except ImportError:
        checks.append(
            [
                "MCP support",
                _WARN,
                "install `omniscientist[mcp]` to enable `omni mcp serve`",
            ]
        )

    # optional bins
    for tool, why in [
        ("node", "external MCP/npx servers"),
        ("ffmpeg", "media-processing skills"),
        ("git", "Git-related skills"),
        ("soffice", "document-conversion skills"),
    ]:
        found = shutil.which(tool) or shutil.which("libreoffice" if tool == "soffice" else tool)
        checks.append(
            [f"bin: {tool}", _OK if found else _WARN, found or f"not installed ({why})"]
        )

    # CC/Codex discovery
    from omni.compat.integrations import codex_home
    cc = s.paths.claude_user_skills
    checks.append(["Claude Code skills", _OK if cc.exists() else _WARN, str(cc)])
    cx = codex_home() / "config.toml"
    checks.append(["Codex config", _OK if cx.exists() else _WARN, str(cx)])

    data_table("OmniScientist Doctor", ["check", "status", "detail"], checks)
