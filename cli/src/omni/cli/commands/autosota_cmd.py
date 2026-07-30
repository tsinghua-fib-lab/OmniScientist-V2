"""`omni autosota` — install and foreground-launch the external AutoSOTA CLI."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from omni.autosota.integration import (
    AutosotaError,
    WorkspaceConfiguration,
    active_install,
    configure_workspace,
    install_release,
    launcher_defaults,
    prepare_native_paper_config,
    run_native,
)
from omni.cli.render import error, info, kv_table, success, warn
from omni.cli.state import AppState

app = typer.Typer(
    help="Install and launch the independent AutoSOTA experiment CLI.",
    no_args_is_help=True,
)


def _paths(ctx: typer.Context):  # noqa: ANN201
    state: AppState = ctx.obj
    paths = state.settings().paths
    paths.ensure_dirs()
    return paths


def _exit_for_error(exc: AutosotaError) -> None:
    error(str(exc))
    raise typer.Exit(1) from exc


def _run_or_exit(
    ctx: typer.Context,
    *,
    workspace: Path,
    args: list[str],
    materialize_secrets: bool = True,
) -> None:
    try:
        code = run_native(
            _paths(ctx),
            workspace=workspace,
            args=args,
            materialize_secrets=materialize_secrets,
        )
    except AutosotaError as exc:
        _exit_for_error(exc)
    if code:
        raise typer.Exit(code)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _code_model_base_url(provider: str, base_url: str) -> str:
    """Translate known Omni endpoints to AutoSOTA's code-model protocol.

    AutoSOTA invokes Claude Code for repository changes.  DeepSeek exposes a
    separate Anthropic-compatible route for that client, while Omni's normal
    text client uses its OpenAI-compatible ``/v1`` route.  Keep the conversion
    deliberately narrow and only use it after the user explicitly requested
    ``--use-omni-model``.
    """
    normalized_provider = provider.strip().lower()
    normalized_url = base_url.rstrip("/")
    if normalized_provider == "deepseek" and normalized_url.endswith("/v1"):
        return normalized_url[:-3] + "/anthropic"
    return base_url


@app.command("get")
def get_cmd(
    ctx: typer.Context,
    version: str = typer.Option("latest", "--version", help="Official release tag, or latest."),
    package: Path | None = typer.Option(None, "--package", help="Explicit local .tgz for air-gapped installation."),
    force: bool = typer.Option(False, "--force", help="Install a fresh isolated copy even if this release is active."),
) -> None:
    """Download an official AutoSOTA release into Omni's private npm cache."""
    try:
        result = install_release(_paths(ctx), version=version, package=package, force=force)
    except AutosotaError as exc:
        _exit_for_error(exc)
    if result.changed:
        success(f"AutoSOTA {result.version} installed: {result.runtime_dir}")
    else:
        success(f"AutoSOTA {result.version} is already ready: {result.runtime_dir}")
    info("AutoSOTA remains independent. Configure a workspace with `omni autosota config`.")


@app.command("config")
def config_cmd(
    ctx: typer.Context,
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="AutoSOTA workspace directory."),
    repo: Path | None = typer.Option(None, "--repo", help="Existing target repository to optimise."),
    devices: str = typer.Option("", "--devices", help="Comma-separated GPU devices, for example 0,1."),
    eval_command: str = typer.Option("", "--eval-command", help="Evaluation command retained in the launcher profile."),
    primary_metric: str = typer.Option("", "--primary-metric", help="Metric name retained in the launcher profile."),
    metric_direction: str = typer.Option("", "--metric-direction", help="maximize or minimize."),
    baseline: str = typer.Option("", "--baseline", help="Baseline metric value retained in the launcher profile."),
    max_iterations: int | None = typer.Option(None, "--max-iterations", min=1, help="Iteration budget retained in the launcher profile."),
    max_total_hours: float | None = typer.Option(None, "--max-total-hours", min=0.1, help="Wall-clock budget retained in the launcher profile."),
    protected_path: list[str] = typer.Option([], "--protected-path", help="Path AutoSOTA must not modify; repeatable."),
    claude_base_url: str = typer.Option("", "--claude-base-url", help="AutoSOTA code-model endpoint."),
    claude_model: str = typer.Option("", "--claude-model", help="AutoSOTA code-model name."),
    research_base_url: str = typer.Option("", "--research-base-url", help="AutoSOTA research-model endpoint."),
    research_model: str = typer.Option("", "--research-model", help="AutoSOTA research-model name."),
    claude_api_key: str = typer.Option("", "--claude-api-key", help="Prefer an interactive prompt so this key is not kept in shell history."),
    research_api_key: str = typer.Option("", "--research-api-key", help="Prefer an interactive prompt so this key is not kept in shell history."),
    openrouter_api_key: str = typer.Option("", "--openrouter-api-key", help="Prefer an interactive prompt so this key is not kept in shell history."),
    use_omni_model: bool = typer.Option(False, "--use-omni-model", help="Explicitly copy Omni's configured text-model endpoint and key."),
    prompt_secrets: bool = typer.Option(False, "--prompt-secrets", help="Prompt for missing provider keys without echoing them."),
    force: bool = typer.Option(False, "--force", help="Update an existing AutoSOTA config.yaml (it may reformat YAML comments)."),
) -> None:
    """Create a public launcher profile and store keys only in Omni secrets."""
    if repo is None and _interactive():
        entered = typer.prompt("Existing target repository path", default="").strip()
        repo = Path(entered) if entered else None
    if repo is None:
        error("`--repo` is required in non-interactive mode; AutoSOTA optimises an existing repository.")
        raise typer.Exit(2)

    state: AppState = ctx.obj
    if use_omni_model:
        model = state.settings().model
        if model.provider == "mock" or not model.api_key:
            error("The active Omni text model has no usable API key; configure it first or enter AutoSOTA keys directly.")
            raise typer.Exit(2)
        claude_base_url = claude_base_url or _code_model_base_url(model.provider, model.base_url)
        claude_model = claude_model or model.model
        claude_api_key = claude_api_key or model.api_key
        research_base_url = research_base_url or model.base_url
        research_model = research_model or model.model
        research_api_key = research_api_key or model.api_key
    if prompt_secrets and _interactive():
        if not claude_api_key:
            claude_api_key = typer.prompt("AutoSOTA code-model API key (blank to skip)", default="", hide_input=True)
        if not research_api_key:
            research_api_key = typer.prompt("AutoSOTA research-model API key (blank to skip)", default="", hide_input=True)

    configuration = WorkspaceConfiguration(
        workspace=workspace,
        repo_path=repo,
        devices=devices,
        eval_command=eval_command,
        primary_metric=primary_metric,
        metric_direction=metric_direction,
        baseline=baseline,
        max_iterations=max_iterations,
        max_total_hours=max_total_hours,
        protected_paths=tuple(protected_path),
        claude_base_url=claude_base_url,
        claude_model=claude_model,
        research_base_url=research_base_url,
        research_model=research_model,
    )
    try:
        result = configure_workspace(
            _paths(ctx),
            configuration,
            secrets={
                "claude_api_key": claude_api_key,
                "research_api_key": research_api_key,
                "openrouter_api_key": openrouter_api_key,
            },
            force=force,
        )
    except AutosotaError as exc:
        _exit_for_error(exc)
    success(f"AutoSOTA launcher profile saved: {result.profile_path}")
    if result.config_updated:
        success(f"AutoSOTA public model configuration saved: {result.config_path}")
    else:
        warn(f"Kept existing native AutoSOTA config unchanged: {result.config_path} (use --force to update it).")
    if result.secrets_saved:
        info("Provider keys were saved in Omni's owner-only secrets.toml and are materialized only while AutoSOTA runs.")
    else:
        warn("No provider key was saved. Use --prompt-secrets, a key option, or --use-omni-model before a model-backed run.")
    info(
        "Run `omni autosota prepare PAPER --workspace ...` to create the non-secret "
        "native paper config, then launch with `--skip-onboard`."
    )


@app.command("init")
def init_cmd(
    ctx: typer.Context,
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="AutoSOTA workspace directory."),
) -> None:
    """Run AutoSOTA's own workspace initializer in the foreground."""
    _run_or_exit(ctx, workspace=workspace, args=["init"])


@app.command("prepare")
def prepare_cmd(
    ctx: typer.Context,
    paper: str = typer.Argument(..., help="Stable AutoSOTA paper/run name."),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="AutoSOTA workspace directory."),
) -> None:
    """Create a non-secret native paper config from Omni's launcher profile."""
    _paths(ctx)
    try:
        config_path = prepare_native_paper_config(workspace, paper)
    except AutosotaError as exc:
        _exit_for_error(exc)
    success(f"Safe AutoSOTA paper configuration saved: {config_path}")
    info("Launch this paper with `--skip-onboard`; native model onboarding is not needed.")


@app.command("login")
def login_cmd(ctx: typer.Context, workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w")) -> None:
    """Open AutoSOTA's native login flow; Omni does not intercept credentials."""
    _run_or_exit(ctx, workspace=workspace, args=["login"], materialize_secrets=False)


@app.command("run")
def run_cmd(
    ctx: typer.Context,
    paper: str | None = typer.Argument(
        None,
        help="Prepared AutoSOTA paper name. Required for a real run; omit only with --dry-run.",
    ),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="AutoSOTA workspace directory."),
    repo: Path | None = typer.Option(None, "--repo", help="Override the configured target repository."),
    devices: str = typer.Option("", "--devices", help="Override configured GPU devices."),
    max_iterations: int | None = typer.Option(None, "--max-iter", min=1, help="Override the configured iteration budget."),
    max_total_hours: float | None = typer.Option(None, "--max-total-hours", min=0.1, help="Override the configured wall-clock budget."),
    resume: bool = typer.Option(False, "--resume", help="Ask AutoSOTA to resume its most recent run."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the native command without starting an optimisation."),
) -> None:
    """Launch AutoSOTA in the foreground; it owns GPUs and long-running work."""
    if paper is None and not dry_run:
        error(
            "A prepared paper name is required for a real run. "
            "Run `omni autosota prepare PAPER --workspace ...`, then "
            "`omni autosota run PAPER --workspace ...`."
        )
        raise typer.Exit(1)
    defaults = launcher_defaults(workspace)
    selected_repo = repo or (Path(str(defaults["repo_path"])) if defaults.get("repo_path") else None)
    selected_devices = devices or str(defaults.get("devices") or "")
    selected_iterations = max_iterations if max_iterations is not None else defaults.get("max_iterations")
    selected_hours = max_total_hours if max_total_hours is not None else defaults.get("max_total_hours")
    args: list[str] = [paper, "--skip-onboard"] if paper is not None else []
    if selected_repo is not None:
        args.extend(["--repo", str(selected_repo.expanduser().resolve())])
    if selected_devices:
        args.extend(["--devices", selected_devices])
    if selected_iterations is not None:
        args.extend(["--max-iter", str(int(selected_iterations))])
    if selected_hours is not None:
        args.extend(["--max-total-minutes", str(max(1, round(float(selected_hours) * 60)))])
    if resume:
        args.append("--resume")
    if dry_run:
        info("Native command: autosota " + " ".join(args))
        info("No AutoSOTA process, GPU job, or model request was started.")
        return
    _run_or_exit(ctx, workspace=workspace, args=args)


@app.command("resume")
def resume_cmd(
    ctx: typer.Context,
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="AutoSOTA workspace directory."),
) -> None:
    """Resume using AutoSOTA's native continuation supervisor."""
    _run_or_exit(ctx, workspace=workspace, args=["--resume"])


@app.command("status")
def status_cmd(
    ctx: typer.Context,
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="AutoSOTA workspace directory."),
) -> None:
    """Show AutoSOTA sessions; no Omni background task is created."""
    _run_or_exit(ctx, workspace=workspace, args=["sessions"])


@app.command("inspect")
def inspect_cmd(
    ctx: typer.Context,
    session: str = typer.Argument("latest", help="AutoSOTA run, paper, or latest."),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="AutoSOTA workspace directory."),
) -> None:
    """Inspect one native AutoSOTA session."""
    _run_or_exit(ctx, workspace=workspace, args=["inspect", session])


@app.command("steer")
def steer_cmd(
    ctx: typer.Context,
    instruction: str = typer.Argument(..., help="Instruction delivered to AutoSOTA's native controller."),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="AutoSOTA workspace directory."),
) -> None:
    """Send a native steering instruction to an active AutoSOTA run."""
    _run_or_exit(ctx, workspace=workspace, args=["steer", instruction])


@app.command("doctor")
def doctor_cmd(
    ctx: typer.Context,
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="AutoSOTA workspace directory."),
) -> None:
    """Run AutoSOTA's native environment checks."""
    _run_or_exit(ctx, workspace=workspace, args=["doctor"])


@app.command("info")
def info_cmd(ctx: typer.Context) -> None:
    """Show the selected isolated AutoSOTA runtime without exposing secrets."""
    try:
        installation = active_install(_paths(ctx))
    except AutosotaError as exc:
        _exit_for_error(exc)
    if installation is None:
        warn("AutoSOTA is not installed. Run `omni autosota get`.")
        return
    kv_table(
        "AutoSOTA runtime",
        [
            ("version", installation.version),
            ("runtime", installation.runtime_dir),
            ("executable", installation.executable),
            ("ownership", "external CLI; AutoSOTA controls environments, GPUs, and optimisation"),
        ],
    )


@app.command("exec")
def exec_cmd(
    ctx: typer.Context,
    args: list[str] = typer.Argument(..., help="Arguments passed verbatim to AutoSOTA; use `--` before option-like arguments."),
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="AutoSOTA workspace directory."),
) -> None:
    """Pass through a native AutoSOTA command omitted by this thin wrapper."""
    _run_or_exit(ctx, workspace=workspace, args=args)
