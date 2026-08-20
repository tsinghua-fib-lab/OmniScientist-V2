"""`omni init` — guided one-time setup wizard."""

from __future__ import annotations

import tomllib
from pathlib import Path

import tomli_w
import typer

from omni.cli.command_surface import spell_commands
from omni.cli.render import (
    banner,
    confirm,
    data_table,
    error,
    info,
    kv_table,
    prompt_secret,
    prompt_text,
    success,
    warn,
)
from omni.cli.state import AppState, make_agent, run_async
from omni.config import resolve_settings
from omni.config.model_discovery import discover_init_seed
from omni.config.model_stack import MODEL_PROVIDER_CATALOG, ModelRole
from omni.config.paths import configure_user_home, user_home_resolution
from omni.config.secure_files import write_private_toml
from omni.config.workspaces import prior_user_data_summary
from omni.personas.installer import BuiltinPersonaInstallError, install_builtin_personas
from omni.skills_runtime.runtime_setup import (
    SkillRuntimeSetupError,
    setup_research_pptx_runtime,
)

app = typer.Typer(help="Setup wizard for models, retrieval, workspaces, skills, and MCP.")

_SEMANTIC_SCHOLAR_API_URL = "https://www.semanticscholar.org/product/api"


def _prepare_bundled_skill_runtimes(paths) -> None:  # noqa: ANN001
    """Prepare install-time Skill components before any task can select them."""
    info("Checking bundled Skill runtimes...")
    try:
        personas = install_builtin_personas(paths)
    except BuiltinPersonaInstallError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    if personas.installed:
        success(
            f"Installed {len(personas.installed)} bundled scientist personas into "
            f"{paths.scientist_kg_dir}."
        )
    else:
        info("Bundled scientist personas are ready; existing directories were preserved.")
    try:
        changed = setup_research_pptx_runtime(paths)
    except SkillRuntimeSetupError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    if changed:
        success("research-pptx renderer dependencies installed.")
    else:
        info("research-pptx renderer dependencies are ready.")


# What ``/init`` sets → the command to adjust each item later, so users never
# have to re-run the whole wizard to tweak one thing. Shared by ``/help`` (static
# map) and ``omni init`` on an already-configured machine (live overview).
_ADJUST_CMD: dict[str, str] = {
    "model": "/model <name> (advanced: /model main -p <provider> -u <BASE_URL> -m <MODEL> -k <API_KEY>)",
    "embeddings": "/model embedding --enable -u <EMBED_BASE_URL> -m <EMBED_MODEL> -k <API_KEY> (disable with /model embedding --disable)",
    "semantic_scholar": "/config set research.semantic_scholar_api_key <API_KEY> (remove with /config unset research.semantic_scholar_api_key)",
    "home": "/config home [PATH] (restore ~/.omni with /config home --reset)",
    "project": "/project new <name> / /project list",
    "skills": "/skills add <source> to import; /skills export to share built-ins with Claude, Codex, or OpenClaw",
    "mcp": "/mcp install both",
    "channels": "/channel login wechat|feishu|dingtalk",
}


def _adjust_cmd(key: str, *, surface: str | None = None) -> str:
    return spell_commands(_ADJUST_CMD[key], surface=surface)


def init_config_map_rows(*, surface: str | None = None) -> list[tuple[str, str, str]]:
    """Return setup areas, descriptions, and adjustment commands."""
    return [
        ("Model", "provider / base_url / model / api_key", _adjust_cmd("model", surface=surface)),
        ("Embedding recall", "semantic recall or offline keyword recall", _adjust_cmd("embeddings", surface=surface)),
        (
            "Semantic Scholar",
            "optional API key for higher literature-search rate limits",
            _adjust_cmd("semantic_scholar", surface=surface),
        ),
        ("Data directory", "sessions, memory, artifacts, and projects", _adjust_cmd("home", surface=surface)),
        ("Project workspace", "isolated data for each research project", _adjust_cmd("project", surface=surface)),
        ("Skill library", "built-in and imported external skills", _adjust_cmd("skills", surface=surface)),
        ("MCP registration", "expose Omni to Claude Code or Codex", _adjust_cmd("mcp", surface=surface)),
        ("Messaging channels", "connect messaging channels separately", _adjust_cmd("channels", surface=surface)),
    ]


def render_init_config_map(*, surface: str | None = None) -> None:
    """Render the static /init → adjust-command map (used by ``/help``)."""
    data_table(
        "/init settings and later adjustment commands",
        ["setting", "description", "adjustment command"],
        [list(r) for r in init_config_map_rows(surface=surface)],
    )


def _model_status_text(settings) -> str:  # noqa: ANN001
    m = settings.model
    if _normalize_provider(m.provider) == "mock":
        return "mock (offline placeholder)"
    text = f"{m.provider} · {m.model}"
    if m.base_url:
        text += f" @ {m.base_url}"
    text += ", api_key " + ("set" if m.api_key else "not set")
    return text


def _channels_status_text(settings) -> str:  # noqa: ANN001
    from omni.channels.manager import channel_config_state

    bits = []
    for name in ("wechat", "feishu", "dingtalk"):
        ok, _reason = channel_config_state(settings, name)
        bits.append(f"{name} {'✓' if ok else '—'}")
    return " / ".join(bits)


def _embeddings_status_text(settings) -> str:  # noqa: ANN001
    memory = settings.memory
    if not memory.embeddings_enabled:
        return "disabled · keyword recall"
    endpoint = memory.embedding_base_url or "reuse model endpoint"
    return f"enabled · semantic recall · {memory.embedding_model} @ {endpoint}"


def _semantic_scholar_status_text(settings) -> str:  # noqa: ANN001
    return (
        "configured"
        if settings.research.semantic_scholar_api_key
        else "unset · public rate limits"
    )


def _mcp_status_text() -> str:
    from omni.compat.integrations import mcp_registration_status

    st = mcp_registration_status()
    done = [name for name in ("codex", "claude") if st.get(name)]
    return "registered: " + " / ".join(done) if done else "not registered"


def _skills_count_text(settings) -> str:  # noqa: ANN001
    try:
        from omni.skills_runtime.registry import SkillRegistry

        return f"{SkillRegistry(settings).build_index()} built-in and imported"
    except Exception:  # noqa: BLE001
        return "run `omni skills list`"


def render_config_overview(settings) -> None:  # noqa: ANN001
    """Show the live current configuration alongside how to adjust each item."""
    paths = settings.paths
    rows = [
        ["Model", _model_status_text(settings), _adjust_cmd("model")],
        ["Embedding recall", _embeddings_status_text(settings), _adjust_cmd("embeddings")],
        [
            "Semantic Scholar",
            _semantic_scholar_status_text(settings),
            _adjust_cmd("semantic_scholar"),
        ],
        ["Data directory", str(paths.home), _adjust_cmd("home")],
        ["Project workspace", paths.project_name, _adjust_cmd("project")],
        ["Skill library", _skills_count_text(settings), _adjust_cmd("skills")],
        ["MCP registration", _mcp_status_text(), _adjust_cmd("mcp")],
        ["Messaging channels", _channels_status_text(settings), _adjust_cmd("channels")],
    ]
    data_table("Current configuration", ["setting", "status", "adjustment command"], rows)


def _write(target, data: dict) -> None:
    existing = tomllib.loads(target.read_text()) if target.is_file() else {}
    existing.update(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.name == "secrets.toml":
        write_private_toml(target, existing)
    else:
        with target.open("wb") as fh:
            tomli_w.dump(existing, fh)


# Providers shown in the wizard. ``openai`` / ``deepseek`` / ``ollama`` all speak
# the OpenAI-compatible protocol — only the ``base_url`` (and a sensible default
# model) differ — so we prefill defaults and the user can just press Enter.
_PROVIDER_PRESETS: list[tuple[str, str, str, str]] = [
    (item.key, item.label, item.default_endpoint, item.default_model)
    for item in MODEL_PROVIDER_CATALOG
    if ModelRole.MAIN in item.roles
]


def _provider_preset(provider: str) -> tuple[str, str]:
    """Return ``(default_base_url, default_model)`` for a provider name."""
    for name, _label, base, model in _PROVIDER_PRESETS:
        if name == provider:
            return base, model
    return "", "gpt-4o-mini"


def _resolve_provider_choice(choice: str) -> str:
    """Map a menu answer (index ``1..4`` or a provider name) to a provider.

    Anything unrecognised falls back to the default, ``openai``.
    """
    names = [p[0] for p in _PROVIDER_PRESETS]  # openai, deepseek, ollama, mock
    c = (choice or "").strip().lower()
    if c.isdigit() and 1 <= int(c) <= len(names):
        return names[int(c) - 1]
    if c in names:
        return c
    if c in ("offline", "openai_compatible", "openai-compatible"):
        return "mock" if c == "offline" else "openai"
    return "openai"


def _normalize_provider(provider: str) -> str:
    """Lower/trim a provider label; empty/offline → ``mock``.

    Friendly names (``openai`` / ``deepseek`` / ``ollama`` / ``openai_compatible``)
    are kept as-is — the LLM client treats them all as OpenAI-compatible, so the
    stored value stays transparent to the user.
    """
    p = (provider or "").strip().lower()
    return "mock" if p in ("", "mock", "offline") else p


def _embedding_defaults(provider: str, chat_base_url: str) -> tuple[str, str]:
    """Return safe onboarding defaults for a dedicated embedding service.

    OpenAI and local Ollama commonly expose embeddings on their chat-compatible
    base URL. Known chat-only services such as DeepSeek deliberately receive no
    endpoint default, forcing an explicit dedicated service instead of a 404
    capability probe.
    """
    p = _normalize_provider(provider)
    base = (chat_base_url or "").rstrip("/")
    if p == "ollama":
        return base, "nomic-embed-text"
    if p == "openai" and "api.openai.com" in base:
        return base, "text-embedding-3-small"
    return "", "text-embedding-3-small"


def _render_embedding_choice() -> None:
    info("Choose a recall mode:")
    info("  Embeddings enable semantic recall and require a working /embeddings endpoint.")
    info("  Keyword recall is offline and does not probe the model endpoint.")


def first_run_setup_required(state: AppState) -> bool:
    """Return whether a bare interactive ``omni`` should launch setup.

    A user config file is the existing setup commit point. A complete effective
    model supplied by a profile or environment variables is equally valid and
    must not be overwritten by an unsolicited wizard.
    """
    from omni.config.user_edits import setup_required

    return setup_required(state.settings())


def first_run_setup_message(home: Path) -> str:
    """Explain why setup is running — never imply a wipe when data is present."""
    prior = prior_user_data_summary(home)
    if prior:
        return (
            "Model configuration is missing; completing setup without deleting "
            f"tasks or workspaces. {prior}"
        )
    return "No user configuration was found; starting first-time setup."


def _select_data_home(requested: str, *, explicit: bool) -> None:
    """Persist a setup-time data directory without overriding ``OMNI_HOME``."""
    current, source = user_home_resolution()
    target = Path(requested).expanduser().resolve()
    if source == "environment (OMNI_HOME)" and target != current:
        error(
            f"OMNI_HOME currently selects {current}. Unset OMNI_HOME before choosing {target}."
        )
        raise typer.Exit(2)
    # Accepting an environment-provided default should not turn a temporary
    # shell override into a persistent preference. An explicit --home does.
    if target == current and not explicit:
        return
    try:
        configure_user_home(target)
    except (OSError, ValueError) as exc:
        error(f"Could not configure the Omni data directory: {exc}")
        raise typer.Exit(2) from exc


def run_setup_wizard(
    state: AppState,
    *,
    non_interactive: bool = False,
    provider: str = "",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    embeddings: bool | None = None,
    embedding_base_url: str = "",
    embedding_api_key: str = "",
    embedding_model: str = "",
    semantic_scholar_api_key: str = "",
    home: str = "",
) -> None:
    """Run the setup flow shared by ``omni init`` and first bare launch."""
    paths = state.settings().paths

    # Second run: if already initialised (and the user didn't pass explicit model
    # flags or -y), don't blindly re-ask — show the current configuration plus the
    # command to tweak each item, and only re-run the wizard on confirmation. This
    # makes `/init` echo the state that `/config`, `/channel`, … maintain.
    explicit_flags = bool(
        provider or base_url or api_key or model or embeddings is not None
        or embedding_base_url or embedding_api_key or embedding_model
        or semantic_scholar_api_key
        or home
    )
    if paths.config_file.is_file() and not non_interactive and not explicit_flags:
        banner("OmniScientist current configuration")
        _, home_source = user_home_resolution()
        info(f"Data directory: {paths.home} ({home_source}); change with `config home [PATH]`")
        render_config_overview(state.settings())
        if not confirm("Rerun setup and overwrite these settings?", default=False):
            paths.ensure_dirs()
            _prepare_bundled_skill_runtimes(paths)
            info("Each setting can be changed independently with the commands shown above.")
            return

    prior = prior_user_data_summary(paths.home)
    if prior:
        banner("OmniScientist setup (existing data)")
        info(prior)
        info("This writes model settings only. Tasks, workspaces, and secrets are not deleted.")
    else:
        banner("OmniScientist setup")
    selected_home = home.strip()
    if not non_interactive and not selected_home:
        selected_home = prompt_text(
            "Omni data directory (config, sessions, memory, tasks, and artifacts)",
            default=str(paths.home),
        ).strip()
    if selected_home:
        _select_data_home(selected_home, explicit=bool(home.strip()))
        paths = state.settings().paths
    paths.ensure_dirs()
    _prepare_bundled_skill_runtimes(paths)
    _, home_source = user_home_resolution()
    info(f"Data directory: {paths.home} ({home_source})")

    cfg: dict = {}
    secrets: dict = {}

    # Isolated OMNI_HOME must not silently clobber a working environment stack
    # with mock. Discover env (and, interactively, the host ~/.omni) first.
    if not provider:
        seed = discover_init_seed(
            resolve_settings(
                project=getattr(state, "project", None),
                profile=getattr(state, "profile", None),
                trusted=getattr(state, "trusted", None),
            ),
            allow_host=not non_interactive,
        )
        if seed is not None:
            info(
                f"Discovered {seed.provider} · {seed.model}"
                + (f" @ {seed.base_url}" if seed.base_url else "")
                + f" from {seed.origin}."
            )
            accept = non_interactive or confirm(
                f"Use this model for this Omni Home ({paths.home})?",
                default=True,
            )
            if accept:
                provider = seed.provider
                base_url = base_url or seed.base_url
                model = model or seed.model
                api_key = api_key or seed.api_key

    # Model provider — present recognizable providers (openai / deepseek / ollama)
    # and default to openai. They are all OpenAI-compatible (base_url is what
    # actually differs), so the choice is transparent; mock stays the offline
    # fallback and is never a silent default.
    if non_interactive:
        provider = provider or "mock"
    elif not provider:
        info("Choose a model provider (Enter selects 1 = openai):")
        for i, (name, label, _base, _model) in enumerate(_PROVIDER_PRESETS, 1):
            info(f"  {i}) {name} — {label}")
        info("openai, deepseek, and ollama use the OpenAI-compatible protocol; base_url selects the service.")
        provider = _resolve_provider_choice(prompt_text("Select [1-4]", default="1"))

    provider = _normalize_provider(provider)
    if provider == "mock":
        cfg["model"] = {"provider": "mock", "model": "omni-mock"}
        warn(
            "Using the offline mock model. Configure a real model later with "
            f"`{spell_commands('/model')}`."
        )
    else:
        def_base, def_model = _provider_preset(provider)
        if not non_interactive:
            base_url = base_url or prompt_text(
                f"API base_url / endpoint ({provider} default: {def_base or 'required'})",
                default=def_base,
            ).strip()
            model = model or prompt_text("Model name", default=def_model)
            api_key = api_key or prompt_text(
                "API key / token (may be empty for local Ollama)",
                default="",
            )
        base_url = base_url or def_base
        if not base_url:
            # A real provider but no endpoint → don't persist a broken config;
            # fall back to mock and show the one-shot command to finish later.
            provider = "mock"
            cfg["model"] = {"provider": "mock", "model": "omni-mock"}
            warn(
                "No base_url was provided; using offline mock. Configure a model later with "
                f"{spell_commands('/model main -p openai -u <BASE_URL> -m <MODEL> -k <API_KEY>')}"
            )
        else:
            cfg["model"] = {"provider": provider, "base_url": base_url, "model": model or def_model}
            if api_key:
                secrets.setdefault("model", {})["api_key"] = api_key

    # Retrieval mode is an explicit onboarding decision. Safe/non-interactive
    # setup defaults to keyword recall, avoiding calls to chat-only endpoints.
    if embeddings is None:
        if non_interactive:
            embeddings = False
        else:
            _render_embedding_choice()
            embeddings = confirm("Enable embedding-based semantic recall?", default=False)

    if embeddings:
        default_embedding_base, default_embedding_model = _embedding_defaults(provider, base_url)
        if not non_interactive:
            if provider == "deepseek" and not embedding_base_url:
                warn("The selected chat endpoint does not provide embeddings; use a separate /embeddings service.")
            embedding_base_url = embedding_base_url or prompt_text(
                "Embedding base_url (must provide /embeddings)",
                default=default_embedding_base,
            ).strip()
            embedding_model = embedding_model or prompt_text(
                "Embedding model", default=default_embedding_model,
            ).strip()
            if not embedding_api_key:
                embedding_api_key = prompt_secret(
                    "Embedding API key (empty may reuse the model token)"
                )
        embedding_base_url = (embedding_base_url or default_embedding_base).rstrip("/")
        embedding_model = embedding_model or default_embedding_model
        if not embedding_base_url:
            error(
                "Embeddings require an endpoint providing /embeddings. Use --embedding-base-url "
                "or --no-embeddings for keyword recall."
            )
            raise typer.Exit(2)
        cfg["memory"] = {
            "embeddings_enabled": True,
            "embedding_provider": "openai_compatible",
            "embedding_base_url": embedding_base_url,
            "embedding_model": embedding_model,
        }
        if embedding_api_key:
            secrets.setdefault("memory", {})["embedding_api_key"] = embedding_api_key
    else:
        cfg["memory"] = {"embeddings_enabled": False}

    if not non_interactive and not semantic_scholar_api_key:
        info(
            "Semantic Scholar works without a key at public rate limits. "
            f"Register for an optional API key: {_SEMANTIC_SCHOLAR_API_URL}"
        )
        semantic_scholar_api_key = prompt_secret(
            "Semantic Scholar API key (optional; press Enter to skip)"
        )
    if semantic_scholar_api_key:
        secrets.setdefault("research", {})[
            "semantic_scholar_api_key"
        ] = semantic_scholar_api_key

    _write(paths.config_file, cfg)
    if secrets:
        _write(paths.secrets_file, secrets)

    from omni.core.llm.health import record_model_health

    configured = state.settings()
    record_model_health(
        configured.paths,
        configured.model,
        status="unverified",
        message="Model configuration changed and has not been tested.",
    )

    # Optional live connectivity check for real providers.
    if provider != "mock" and api_key and base_url:
        do_test = non_interactive or confirm("Test model connectivity now?", default=True)
        if do_test:
            from omni.core.llm.client import check_connectivity
            configured = state.settings()
            ok, detail = run_async(check_connectivity(configured))
            record_model_health(
                configured.paths,
                configured.model,
                status="verified" if ok else "failed",
                message=detail,
            )
            (success if ok else warn)(detail)

    # init db + default project
    async def _init_runtime():
        agent = await make_agent(state)
        n = len(agent.registry.list_all())
        await agent.aclose()
        return n

    n_skills = run_async(_init_runtime())

    # Optionally export built-in skills into the system roots external tools read
    # (Claude Code / Codex / OpenClaw). This writes into ~/.claude/skills etc., so
    # it's opt-in (default No) and fully reversible via `omni skills unexport`.
    export_targets = list(state.settings().skills.export_targets)
    do_export = False
    if not non_interactive:
        do_export = confirm(
            "Export Omni built-in skills for Claude Code, Codex, or OpenClaw? "
            "This is equivalent to `omni skills export`; use `skills add` to import external skills.",
            default=False,
        )
    n_exported = 0
    if do_export:
        from omni.skills_runtime.install import export_builtin_skills
        try:
            results = export_builtin_skills(paths, export_targets)
            n_exported = sum(1 for r in results if r.status in ("installed", "updated", "unchanged"))
            success(f"Exported {n_exported} built-in skills to {', '.join(export_targets)}.")
        except Exception as exc:  # noqa: BLE001
            warn(f"Built-in skill export failed: {exc}")

    # Optionally register OmniScientist as an MCP server in Claude Code / Codex so
    # they can call omni's research tools. Writes ~/.claude.json and
    # $CODEX_HOME/config.toml; opt-in (default No), undo via those configs.
    register_mcp = False
    mcp_status = "(skipped)"
    if not non_interactive:
        register_mcp = confirm(
            "Register OmniScientist as an MCP server for Claude Code and Codex?",
            default=False,
        )
    if register_mcp:
        from omni.compat.integrations import register_with_claude, register_with_codex
        try:
            register_with_codex()
            register_with_claude()
            mcp_status = "registered with Codex and Claude Code"
            success("Registered the `omniscientist` MCP server with Codex and Claude Code.")
        except Exception as exc:  # noqa: BLE001
            mcp_status = "failed; see warning above"
            warn(f"MCP registration failed: {exc}")

    # The always-on home background service is no longer configured here: it is
    # enabled lazily on first real need (configuring a channel or creating a
    # schedule) so setup stays friction-free, and an explicit `omni serve stop`
    # is always respected.

    m = cfg["model"]
    model_summary = f"{m['provider']} · {m.get('model', '')}" + (
        f" @ {m['base_url']}" if m.get("base_url") else ""
    )
    kv_table("Setup complete", [
        (
            "model",
            model_summary
            + (
                ""
                if secrets.get("model", {}).get("api_key") or provider == "mock"
                else f" (API key missing; add it with {spell_commands('/model main -k')})"
            ),
        ),
        (
            "retrieval",
            f"semantic recall · {embedding_model} @ {embedding_base_url}"
            if embeddings else "keyword recall · embeddings disabled",
        ),
        (
            "Semantic Scholar",
            _semantic_scholar_status_text(configured),
        ),
        ("config", paths.config_file),
        ("data directory", paths.home),
        ("secrets", paths.secrets_file if secrets else "(none)"),
        ("project", paths.project_name),
        ("skills", f"{n_skills} discovered"),
        ("external skills imported", "none (use omni skills add <source>)"),
        ("skills exported", f"{n_exported} -> {', '.join(export_targets)}" if do_export else "(skipped)"),
        ("MCP integration", mcp_status),
    ])
    # Channels aren't set up by init (they need interactive QR / app credentials);
    # point the user at the dedicated command so the wizard's scope is honest.
    info("Connect a messaging channel with `omni channel login <channel>`; see `omni channel help`.")
    info("Every setting can be changed independently; rerun `omni init` to inspect them.")
    success('Done. Try: omni "Introduce 3D Gaussian Splatting in three sentences" or run `omni`.')


@app.callback(invoke_without_command=True)
def init(
    ctx: typer.Context,
    non_interactive: bool = typer.Option(False, "--non-interactive", "-y",
                                         help="Use offline defaults without prompts, exports, or MCP registration."),
    provider: str = typer.Option("", help="Model provider: openai, deepseek, ollama, or mock."),
    base_url: str = typer.Option("", help="OpenAI-compatible base URL."),
    api_key: str = typer.Option("", help="API key stored in secrets.toml."),
    model: str = typer.Option("", help="Model name, for example deepseek-chat."),
    embeddings: bool | None = typer.Option(
        None,
        "--embeddings/--no-embeddings",
        help="Enable semantic recall or disable it for keyword recall.",
    ),
    embedding_base_url: str = typer.Option(
        "", "--embedding-base-url", help="Endpoint providing /embeddings.",
    ),
    embedding_api_key: str = typer.Option(
        "", "--embedding-api-key", help="Embedding token stored in secrets.toml.",
    ),
    embedding_model: str = typer.Option(
        "", "--embedding-model", help="Embedding model, such as text-embedding-3-small or bge-m3.",
    ),
    semantic_scholar_api_key: str = typer.Option(
        "",
        "--semantic-scholar-api-key",
        help="Optional Semantic Scholar API key stored in secrets.toml.",
    ),
    home: str = typer.Option(
        "", "--home", help="Persist a custom Omni data directory (default: ~/.omni).",
    ),
) -> None:
    run_setup_wizard(
        ctx.obj,
        non_interactive=non_interactive,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        embeddings=embeddings,
        embedding_base_url=embedding_base_url,
        embedding_api_key=embedding_api_key,
        embedding_model=embedding_model,
        semantic_scholar_api_key=semantic_scholar_api_key,
        home=home,
    )
