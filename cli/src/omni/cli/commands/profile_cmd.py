"""`omni profile` — manage configuration profiles.

A profile is a ``~/.omni/<name>.config.toml`` overlay selected with
``--profile <name>`` (or the ``default_profile`` key). Profiles hold non-secret
model/runtime overrides; API keys always live in the shared ``secrets.toml``.
"""

from __future__ import annotations

import tomllib

import tomli_w
import typer

from omni.cli.render import console, data_table, error, info, success

app = typer.Typer(help="Manage configuration profiles.", no_args_is_help=True)

_SUFFIX = ".config.toml"


@app.command("help")
def help_cmd() -> None:
    """Show profile commands and examples."""
    info("Use `/profile ...` in the REPL or `omni profile ...` in the shell.")
    data_table(
        "profile subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "List all profiles", "omni profile list"],
            ["add <name>", "Create a profile with optional model settings", "omni profile add deepseek --provider openai --base-url https://api.deepseek.com --model deepseek-v4-pro"],
            ["use <name>", "Set the default profile", "omni profile use deepseek"],
            ["show [name]", "Show profile contents", "omni profile show deepseek"],
        ],
    )


def _profile_names(home) -> list[str]:  # noqa: ANN001
    return sorted(p.name[: -len(_SUFFIX)] for p in home.glob(f"*{_SUFFIX}"))


@app.command("list")
def list_cmd(ctx: typer.Context) -> None:
    """List all profiles."""
    s = ctx.obj.settings()
    home = s.paths.home
    default = s.default_profile or ""
    names = _profile_names(home)
    rows = [
        [("→ " if n == default else "  ") + n, str(home / f"{n}{_SUFFIX}")]
        for n in names
    ]
    data_table(
        f"Profiles (default: {default or '(none)'})",
        ["profile", "file"],
        rows or [["(none)", "Create one with `omni profile add <name>`"]],
    )


@app.command("add")
def add_cmd(
    ctx: typer.Context,
    name: str,
    provider: str = typer.Option("", "--provider", "-p", help="mock | openai_compatible"),
    base_url: str = typer.Option("", "--base-url", "-u", help="endpoint"),
    model: str = typer.Option("", "--model", "-m", help="Model name."),
    use: bool = typer.Option(False, "--use", help="Set as default after creation."),
) -> None:
    """Create a profile with optional model settings."""
    paths = ctx.obj.settings().paths
    target = paths.home / f"{name}{_SUFFIX}"
    if target.is_file():
        error(f"Profile '{name}' already exists: {target}")
        raise typer.Exit(1)
    model_block = {
        k: v for k, v in (("provider", provider), ("base_url", base_url), ("model", model)) if v
    }
    data = {"model": model_block} if model_block else {}
    paths.home.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as fh:
        tomli_w.dump(data, fh)
    success(f"Created profile '{name}' at {target}")
    info(f"Use once: `omni --profile {name} \"...\"`; set default: `omni profile use {name}`")
    if use:
        _set_default(paths, name)
        success(f"Default profile set to {name}")


@app.command("use")
def use_cmd(ctx: typer.Context, name: str) -> None:
    """Set the default profile in ~/.omni/config.toml."""
    paths = ctx.obj.settings().paths
    if not (paths.home / f"{name}{_SUFFIX}").is_file():
        error(f"Profile '{name}' does not exist. Create it with `omni profile add {name}`.")
        raise typer.Exit(1)
    _set_default(paths, name)
    success(f"Default profile changed to {name}; the next command will use it.")


@app.command("show")
def show_cmd(ctx: typer.Context, name: str = typer.Argument("")) -> None:
    """Show a profile, defaulting to the active profile."""
    s = ctx.obj.settings()
    name = name or s.default_profile
    if not name:
        error("No profile was specified and no default profile is configured.")
        raise typer.Exit(1)
    target = s.paths.home / f"{name}{_SUFFIX}"
    if not target.is_file():
        error(f"Profile '{name}' does not exist: {target}")
        raise typer.Exit(1)
    console.print(f"[bold]{target}[/bold]\n")
    console.print(target.read_text(encoding="utf-8") or "[dim](empty)[/dim]")


def _set_default(paths, name: str) -> None:  # noqa: ANN001
    data: dict = (
        tomllib.loads(paths.config_file.read_text()) if paths.config_file.is_file() else {}
    )
    data["default_profile"] = name
    paths.home.mkdir(parents=True, exist_ok=True)
    with paths.config_file.open("wb") as fh:
        tomli_w.dump(data, fh)
