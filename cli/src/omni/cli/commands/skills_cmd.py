"""`omni skills` — list / inspect / search / add / export skills.

Skill *content* is Claude Code / Codex / OpenClaw ``SKILL.md`` compatible and
lives in a separate, independent collection (``<repo>/skills``); this command is
the CLI surface for managing it:

- ``list``               skills omni manages (built-in + imported); ``--all`` adds
                         the Claude Code / Codex / OpenClaw libraries on disk.
- ``add <src>``          import an external/local skill INTO omni (~/.omni/skills).
- ``setup <name>``       prepare an owner-managed runtime for a bundled skill.
- ``remove <name>``      delete imported skills or disable builtin/external ones.
- ``restore <name>``     re-enable one skill from ``skills.disabled``.
- ``export [tools]``     export omni's built-ins OUT to Claude Code / Codex / OpenClaw.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from typing import Any

import typer
from rich.pager import Pager

from omni.cli.render import console, data_table, error, info, kv_table, success, warn
from omni.cli.state import AppState
from omni.skills_runtime.manifest import SkillEntry
from omni.skills_runtime.registry import ALL_SOURCES, SkillRegistry, capability_aliases

SKILL_WORKFLOW_EXAMPLES: list[tuple[str, str, str]] = [
    (
        "1 capability",
        "Fetch the abstract for arXiv 1706.03762 and record claims about the Transformer architecture.",
        "paper.fetch.arxiv",
    ),
    (
        "2 capabilities",
        "Search OpenAlex for RAG factuality papers and propose research directions.",
        "literature.search + research.ideation",
    ),
    (
        "3 capabilities",
        "Search RAG literature, propose research directions, and draw a lightweight architecture figure.",
        "literature.search + research.ideation + artifact.figure",
    ),
    (
        "4 capabilities",
        "Fetch arXiv 1706.03762, propose follow-up ideas, create a lightweight figure, and synthesize a section.",
        "paper.fetch.arxiv + research.ideation + artifact.figure; native: synthesis.final",
    ),
    (
        "5 capabilities",
        "Search RAG papers, propose a study, make one editable PPT figure, generate a full deck, and synthesize speaker notes.",
        "literature.search + research.ideation + figure.editable.pptx + slides.generate; native: synthesis.final",
    ),
    (
        "6 capabilities",
        "Search papers, fetch arXiv 1706.03762, ideate, create a lightweight figure, generate a full deck, and synthesize a draft.",
        "literature.search + paper.fetch.arxiv + research.ideation + artifact.figure + slides.generate; native: synthesis.final",
    ),
    (
        "7 capabilities",
        "Run an end-to-end RAG workflow: search, fetch, ideate, create lightweight and editable figures, generate a full deck, and synthesize notes.",
        (
            "literature.search + paper.fetch.arxiv + research.ideation + "
            "artifact.figure + figure.editable.pptx + slides.generate; native: synthesis.final"
        ),
    ),
]

SKILLS_HELP_TEXT = (
    "Inspect and manage skills compatible with Claude Code, Codex, and OpenClaw.\n\n"
    "Command help: `omni skills help` or `/skills help` in the REPL.\n"
    "Research workflow examples: `omni skills examples` or `/skills examples`."
)


class _LessPager(Pager):
    """Pipe rendered output to ``less`` (color + quit-if-one-screen), git-style.

    ``LESS=FRX`` → ``-F`` quit immediately if it fits one screen (so short lists
    print transparently), ``-R`` keep ANSI colors, ``-X`` leave output on exit.
    Falls back to a plain dump if ``less`` isn't installed.
    """

    def show(self, content: str) -> None:
        less = shutil.which("less")
        if not less:
            console.file.write(content)  # already-rendered text; write as-is
            return
        env = {**os.environ, "LESS": "FRX"}
        try:
            proc = subprocess.Popen([less], stdin=subprocess.PIPE, env=env)
            proc.communicate(content.encode("utf-8", "replace"))
        except (OSError, BrokenPipeError, KeyboardInterrupt):
            pass

app = typer.Typer(
    help=SKILLS_HELP_TEXT, no_args_is_help=True
)

# Human-friendly labels for each discovery source, used by the grouped view.
# Ordered high→low priority to match :data:`ALL_SOURCES` (builtin wins ties).
_SOURCE_LABEL: dict[str, str] = {
    "builtin": "omni built-in (packaged)",
    "project_omni": "Project · omni (.omni/skills)",
    "project_claude": "Project · Claude Code",
    "project_agents": "Project · Codex/OpenClaw",
    "user_omni": "omni user library (~/.omni/skills)",
    "user_claude": "Claude Code",
    "user_agents": "Codex/OpenClaw (.agents)",
    "user_codex": "Codex",
    "user_openclaw": "OpenClaw",
}


def _registry(ctx: typer.Context, *, all_sources: bool = False) -> SkillRegistry:
    state: AppState = ctx.obj
    reg = SkillRegistry(state.settings(), sources=ALL_SOURCES if all_sources else None)
    reg.build_index()
    return reg


def _sources_in_order(present: set[str]) -> list[str]:
    """Sources sorted by registry priority, with any unknown ones appended."""
    return [s for s in ALL_SOURCES if s in present] + sorted(present - set(ALL_SOURCES))


def _source_summary(entries: list[SkillEntry]) -> str:
    """One-line per-source breakdown, e.g. ``user_claude 165 · builtin 13``."""
    counts = Counter(e.source for e in entries)
    return " · ".join(f"{s} {counts[s]}" for s in _sources_in_order(set(counts)))


def _render_entries(
    entries: list[SkillEntry], *, title: str, group: bool, page: int, page_size: int,
    per_group: int, footer: bool,
) -> None:
    """Render the table(s) only (caller prints the per-source summary line)."""
    total = len(entries)
    if group:
        by_src: dict[str, list[SkillEntry]] = defaultdict(list)
        for e in entries:
            by_src[e.source].append(e)
        for src in _sources_in_order(set(by_src)):
            grp = by_src[src]
            label = _SOURCE_LABEL.get(src, src)
            rows = [[e.name, e.kind.value, e.delivery_mode.value, e.short_desc(58)] for e in grp[:per_group]]
            data_table(f"{label} · {src} ({len(grp)})", ["name", "kind", "delivery", "description"], rows)
            if len(grp) > per_group:
                info(
                    f"... {len(grp) - per_group} more. Use `/skills list --source {src}` in the REPL "
                    f"or `omni skills list --source {src}` in the shell."
                )
        return

    if page_size and page_size > 0:
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        window = entries[(page - 1) * page_size : (page - 1) * page_size + page_size]
    else:
        pages, page, window = 1, 1, entries
    rows = [[e.name, e.source, e.kind.value, e.delivery_mode.value, e.short_desc(60)] for e in window]
    data_table(f"{title} ({total})", ["name", "source", "kind", "delivery", "description"], rows)
    if footer and pages > 1:
        info(
            f"Page {page}/{pages} ({page_size} per page). "
            "Use `--page <n>`, `--page-size 0` for all entries, or `--group` by source."
        )


def render_skill_list(
    entries: list[SkillEntry],
    *,
    title: str = "Skills",
    page: int = 1,
    page_size: int = 30,
    group: bool = False,
    per_group: int = 12,
    pager: bool | None = None,
) -> None:
    """Render a skills table with a per-source breakdown + pagination/grouping.

    Shared by ``omni skills list`` and the REPL ``/skills list`` so both surfaces
    behave identically.

    Display mode:
    - In a terminal (``pager`` None/True): the *full* list is piped through
      ``less`` — scroll with ↑/↓ · PageUp/PageDown · ``/`` search · ``q`` quit.
      Short lists print transparently (``less -F``).
    - Non-interactive (piped/redirected) or ``pager=False``: print in place,
      honouring ``--page``/``--page-size``/``--group``. When piped without an
      explicit page request, the whole list is dumped (script-friendly).
    """
    total = len(entries)
    if not total:
        info("No matching skills.")
        return

    # ``sys.stdout.isatty()`` is the signal ``less`` itself uses; unlike
    # ``console.is_terminal`` it isn't fooled by FORCE_COLOR/CI env into paging
    # when output is actually piped/captured.
    interactive = sys.stdout.isatty()
    use_pager = interactive if pager is None else pager
    # Piped/redirected without an explicit page request → dump everything.
    if not interactive and pager is None and page == 1 and page_size == 30:
        page_size = 0

    if use_pager:
        with console.pager(pager=_LessPager(), styles=True):
            info(f"By source: {_source_summary(entries)} ({total} total) · ↑/↓ scroll · / search · q quit")
            _render_entries(
                entries, title=title, group=group, page=1, page_size=0, per_group=10**9, footer=False
            )
        return

    info(f"By source: {_source_summary(entries)} ({total} total)")
    _render_entries(
        entries, title=title, group=group, page=page, page_size=page_size,
        per_group=per_group, footer=True,
    )


def render_disabled_skill_list(records: list[dict[str, str]]) -> None:
    """Render skills disabled through ``skills.disabled``."""
    if not records:
        info("No disabled skills.")
        return
    data_table(
        "Disabled skills",
        ["name", "source", "action", "path"],
        [[r["name"], r["source"], r["action"], r["path"]] for r in records],
    )
    info(
        "Restore a skill with `omni skills restore <name>` in the shell or "
        "`/skills restore <name>` in the REPL; `enable` is an alias."
    )


def render_skill_usage_help() -> None:
    """Show research-oriented examples that map user requests to workflow capabilities."""
    info("Research workflow examples using one to seven capabilities.")
    info("A capability may be a skill, native workflow step, or deliverable; `draft.section` is not a skill.")
    info("Run examples with `omni -P skill-verify exec \"...\"` or enter the request directly in the REPL.")
    info("Use `/skills examples` to show these examples again.")
    info("Verification commands: `omni -P skill-verify task list`, `/task show <id>`, and `/task show <id> --json`.")
    data_table(
        "Research workflow examples",
        ["count", "example request", "expected capabilities"],
        [list(row) for row in SKILL_WORKFLOW_EXAMPLES],
    )
    data_table(
        "Shell and REPL execution",
        ["count", "shell request", "REPL request", "verification"],
        [
            [
                label,
                f'omni -P skill-verify exec "{prompt}"',
                f"Start `omni -P skill-verify` and enter: {prompt}",
                "omni -P skill-verify task list; /task show <id>; /task show <id> --json",
            ]
            for label, prompt, _skills in SKILL_WORKFLOW_EXAMPLES
        ],
    )


def render_skills_command_help() -> None:
    """Render the skills command group contract and examples."""
    info("Use `/skills ...` in the REPL or `omni skills ...` in the shell.")
    info("Bare `/skills` shows this help; `/skills <query>` searches, for example `/skills arxiv`.")
    info("Import and enable in the REPL: `/skills add codex:my-skill` -> `/skills trust my-skill --yes`.")
    info(
        "Disable and restore in the REPL: `/skills remove scientific-figure` -> "
        "`/skills list --disabled` -> `/skills restore scientific-figure`."
    )
    info("Export with `/skills export codex` in the REPL or `omni skills export codex` in the shell.")
    data_table(
        "skills subcommands",
        ["command", "purpose", "example"],
        [
            ["list", "List skills; defaults to skills managed by omni", "/skills list"],
            ["info <name>", "Show metadata, source, execution mode, and instructions", "/skills info arxiv-fetch"],
            ["why <capability>", "Explain which skill wins a capability slot and why others lost", "/skills why literature.search"],
            ["search <query>", "Search by keyword; `/skills <query>` is shorthand", "/skills search arxiv"],
            ["add <src>", "Import an external skill into quarantine", "/skills add codex:my-skill"],
            ["setup <name>", "Prepare a bundled Skill runtime", "/skills setup research-pptx"],
            ["trust <name>", "Trust and enable a reviewed imported skill", "/skills trust my-skill --yes"],
            ["untrust <name>", "Revoke execution and automatic planning access", "/skills untrust my-skill"],
            ["remove <name|#n>", "Delete managed skills or disable built-in/external skills", "/skills remove my-skill"],
            ["restore <name>", "Restore a disabled skill", "/skills restore scientific-figure"],
            ["enable <name>", "Alias for restore", "/skills enable scientific-figure"],
            ["sources", "Show skill discovery roots", "/skills sources"],
            ["evolve", "Generate candidates from successful traces; defaults to dry-run", "/skills evolve --install"],
            ["proposals", "Scan and review skill creation or improvement proposals", "/skills proposals list"],
            ["export [tools]", "Export built-in skills to Claude Code, Codex, or OpenClaw", "/skills export codex"],
            ["unexport [tools]", "Remove previously exported built-in skills", "/skills unexport codex"],
            ["examples", "Show research workflow examples", "/skills examples"],
            ["help", "Show this help", "/skills help"],
        ],
    )
    data_table(
        "Important skills options",
        ["option", "commands", "example"],
        [
            ["<query>", "/skills / search", "/skills arxiv"],
            ["--all", "list / info / search", "/skills list --all"],
            ["--pager / --no-pager", "list", "/skills list --all --pager"],
            ["--group", "list", "/skills list --all --group"],
            ["--page / --page-size", "list --no-pager", "/skills list --all --no-pager --page 2"],
            ["--source", "list", "/skills list --source builtin"],
            ["--disabled", "list", "/skills list --disabled"],
            ["--force", "add", "/skills add ~/work/my-skill --force"],
            ["--install / --limit / --min-support", "evolve", "/skills evolve --install --min-support 3"],
            ["--json / --all", "proposals", "/skills proposals list --all --json"],
            ["--all", "remove", "/skills remove ext-skill --all"],
            ["--physical --force", "remove", "/skills remove ext-skill --all --physical --force"],
            ["[tools] / --all", "export / unexport", "/skills export claude codex; /skills export --all"],
        ],
    )
    info("Subcommands select an operation; options control filtering, pagination, and output. Use subcommand --help for details.")
    info(
        "Import or export with `omni skills add|export` in the shell or "
        "`/skills add|export` in the REPL."
    )


@app.command("help")
def help_cmd() -> None:
    """Show skills subcommands, important options, and example entry points."""
    render_skills_command_help()


@app.command("examples")
def examples_cmd() -> None:
    """Show research workflow execution and verification examples."""
    render_skill_usage_help()


@app.command("setup")
def setup_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Bundled Skill runtime to prepare."),
    force: bool = typer.Option(False, "--force", help="Reinstall even when the pinned runtime is ready."),
) -> None:
    """Prepare a bundled Skill runtime outside the Python installation."""
    if name != "research-pptx":
        error(
            f"Skill '{name}' has no owner-managed setup. "
            "Available setup target: research-pptx."
        )
        raise typer.Exit(2)

    from omni.skills_runtime.runtime_setup import (
        SkillRuntimeSetupError,
        research_pptx_runtime_dir,
        setup_research_pptx_runtime,
    )

    paths = ctx.obj.settings().paths
    paths.ensure_dirs()
    info("Preparing the lockfile-pinned research-pptx renderer runtime...")
    try:
        changed = setup_research_pptx_runtime(paths, force=force)
    except SkillRuntimeSetupError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    runtime_dir = research_pptx_runtime_dir(paths)
    if changed:
        success(f"research-pptx renderer ready: {runtime_dir}")
    else:
        success(f"research-pptx renderer already ready: {runtime_dir}")


@app.command("list")
def list_cmd(ctx: typer.Context, source: str = typer.Option("", help="Filter by source, such as builtin or user_claude"),
             async_only: bool = typer.Option(False, "--async", help="Show only asynchronous skills"),
             disabled: bool = typer.Option(False, "--disabled", help="Show only skills disabled through skills.disabled"),
             group: bool = typer.Option(False, "--group", "-g", help="Group the overview by source"),
             page: int = typer.Option(1, "--page", help="Page number starting at 1; applies with --no-pager"),
             page_size: int = typer.Option(30, "--page-size", help="Entries per page; 0 means all; applies with --no-pager"),
             pager: bool | None = typer.Option(
                 None, "--pager/--no-pager",
                 help="Interactive pager: arrows or PgUp/PgDn, / to search, q to quit"),
             all_sources: bool = typer.Option(
                 False, "--all", "-a",
                 help="Include local Claude Code, Codex, and OpenClaw skill libraries")) -> None:
    """List skills managed by omni; use --all to include other local tools.

    Terminals use an interactive pager by default. ``--no-pager`` enables
    page/page-size output; ``--group`` groups by source and ``--source`` filters.
    """
    if disabled:
        from omni.skills_runtime.install import disabled_skill_records

        render_disabled_skill_list(disabled_skill_records(ctx.obj.settings().paths))
        return
    reg = _registry(ctx, all_sources=all_sources)
    entries = reg.list_async_skills() if async_only else reg.list_all()
    if source:
        entries = [e for e in entries if e.source == source]
    render_skill_list(entries, page=page, page_size=page_size, group=group, pager=pager)
    quarantined = [entry.name for entry in entries if not entry.trusted]
    if quarantined:
        warn(
            f"The results include {len(quarantined)} quarantined skills. "
            "Review one with `/skills info <name>` or `omni skills info <name>`, then trust it "
            "with `/skills trust <name> --yes` or `omni skills trust <name> --yes`."
        )
    if not source and not disabled:
        shadowed = reg.shadowed_entries()
        if shadowed:
            info(
                f"{len(shadowed)} same-named skill(s) are shadowed by a higher-priority source "
                "(built-ins win). Force a shadowed one with `$<source>:<name>`; "
                "see `omni skills why <capability>`."
            )
        hidden = [e for e in entries if e.trusted and not e.allow_implicit]
        if hidden:
            info(
                f"{len(hidden)} skill(s) are explicit-only (allow_implicit_invocation=false): "
                "hidden from automatic selection, runnable via `$<name>`."
            )
    if not all_sources:
        info(
            "Showing only skills managed by omni. Use `/skills list --all` in the REPL "
            "or `omni skills list --all` in the shell for all local skills."
        )


@app.command("info")
def info_cmd(ctx: typer.Context, name: str,
             all_sources: bool = typer.Option(False, "--all", "-a", help="Also search other local tool libraries")) -> None:
    """Show detailed skill metadata and instructions."""
    reg = _registry(ctx, all_sources=all_sources)
    e = reg.get(name)
    if not e and not all_sources:  # transparently fall back to the full library
        reg = _registry(ctx, all_sources=True)
        e = reg.get(name)
    if not e:
        error(f"Skill '{name}' was not found.")
        # Skill names are kebab-case; nudge users coming from underscore names.
        if "_" in name and reg.get(name.replace("_", "-")) is not None:
            suggested = name.replace("_", "-")
            info(
                "Skill names use kebab-case. "
                f"REPL: `/skills info {suggested}`; shell: `omni skills info {suggested}`."
            )
        raise typer.Exit(1)
    kv_table(f"Skill {e.name}", [
        ("source", e.source), ("kind", e.kind.value), ("delivery", e.delivery_mode.value),
        ("version", e.version or "-"), ("license", e.license or "-"),
        ("trust", "trusted" if e.trusted else "quarantine"), ("origin", e.origin or "-"),
        ("selection", "auto + explicit" if e.allow_implicit else "explicit only ($name)"),
        ("allowed-tools", ", ".join(e.allowed_tools) or "-"),
        ("requires bins", ", ".join(e.requires_bins) or "-"),
        ("requires env", ", ".join(e.requires_env) or "-"),
        ("path", e.path or "-"),
    ])
    console.print(f"\n[bold]description[/bold]\n{e.description}\n")
    if e.when_to_use:
        console.print(f"[bold]when to use[/bold]\n{e.when_to_use}\n")
    body = e.load_body()
    if body.strip():
        console.print("[bold]instructions (excerpt)[/bold]")
        console.print(body[:1200])


@app.command("why")
def why_cmd(
    ctx: typer.Context,
    capability: str = typer.Argument(
        ..., help="Capability slot to resolve, e.g. literature.search or artifact.figure"
    ),
    all_sources: bool = typer.Option(
        False, "--all", "-a", help="Include local Claude Code, Codex, and OpenClaw libraries"
    ),
) -> None:
    """Explain skill routing: which skill wins a capability slot, and why others lost.

    Mirrors the planner's capability resolution so routing is transparent — the
    selected provider plus each rejected candidate's reason. Built-ins rank highest,
    so a same-named user/external skill is shadowed unless forced with
    ``$<source>:<name>``.
    """
    reg = _registry(ctx, all_sources=all_sources)
    aliases = capability_aliases(capability)
    if len(aliases) > 1:
        info(f"Capability aliases considered: {', '.join(aliases)}")
    selected, rejected = reg.resolve_capability(
        capability, allow_contract_none=True, limit_rejections=20
    )
    if selected is None:
        warn(f"No installed, trusted, implicitly-selectable skill satisfies '{capability}'.")
    else:
        success(
            f"Selected: {selected.name}  "
            f"[{selected.source} · contract={selected.contract_level} · priority={selected.priority or 0}]"
        )
    if rejected:
        data_table(
            "Rejected candidates",
            ["skill", "source", "reason"],
            [[e.name, e.source, reason] for e, reason in rejected],
        )
    shadow = reg.shadowed_entries()
    if shadow:
        data_table(
            "Shadowed same-named skills (force with $<source>:<name>)",
            ["name", "source", "won_by"],
            [[e.name, e.source, (reg.get(e.name).source if reg.get(e.name) else "-")] for e in shadow],
        )


@app.command("search")
def search_cmd(ctx: typer.Context, query: str,
               all_sources: bool = typer.Option(False, "--all", "-a", help="Also search other local tool libraries")) -> None:
    """Search skills by keyword."""
    reg = _registry(ctx, all_sources=all_sources)
    q = query.lower()
    hits = [e for e in reg.list_all() if all(w in f"{e.name} {e.description} {e.when_to_use}".lower()
            for w in q.split())]
    rows = [[e.name, e.source, e.short_desc(80)] for e in hits[:40]]
    data_table(f"Matches for '{query}' ({len(hits)})", ["name", "source", "description"], rows)


def _resolve_remove_selector(reg: SkillRegistry, selector: str) -> SkillEntry | None:
    raw = selector.strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if raw.isdigit():
        idx = int(raw) - 1
        entries = reg.list_all()
        if 0 <= idx < len(entries):
            return entries[idx]
        return None
    return reg.get(selector)


def disabled_skill_restore_hint(name: str, paths, *, command: str = "omni skills") -> str | None:  # noqa: ANN001
    from omni.skills_runtime.install import deleted_skill_record, is_skill_disabled

    if is_skill_disabled(name, paths):
        return (
            f"Skill '{name}' is disabled. Use `{command} restore {name}` in the shell or "
            f"`/skills restore {name}` in the REPL; `enable` is an alias."
        )
    record = deleted_skill_record(name, paths)
    if record and record.get("action") == "config_disable":
        return (
            f"Skill '{name}' has a disable record. Use `{command} restore {name}` in the shell or "
            f"`/skills restore {name}` in the REPL; `enable` is an alias."
        )
    return None


@app.command("add")
def add_cmd(
    ctx: typer.Context,
    source: str = typer.Argument(..., help="Source: local path, tool:name, or git URL"),
    name: str = typer.Option("", "--name", help="Imported skill name; defaults to source name"),
    force: bool = typer.Option(False, "--force", help="Overwrite an imported skill with the same name"),
) -> None:
    """Import an external or local skill into quarantine.

    Claude Code, Codex, and OpenClaw SKILL.md files are supported. Importing
    never grants execution permission; review and trust the skill first.

    In the REPL, replace ``omni skills add`` with ``/skills add``.

    Examples:

      omni skills add ~/work/my-skill
      omni skills add codex:my-skill
      omni skills add openclaw:my-skill
      omni skills add https://github.com/org/repo.git
      omni skills add https://github.com/org/repo#skills/example
    """
    from omni.skills_runtime.install import import_skill, import_skill_from_git, looks_like_git_url

    paths = ctx.obj.settings().paths
    if looks_like_git_url(source):
        info(f"Cloning and importing from git: {source}")
        results = import_skill_from_git(source, paths, name=name or None, force=force)
    else:
        results = [import_skill(source, paths, name=name or None, force=force)]

    ok = [r for r in results if r.status in ("installed", "updated")]
    errs = [r for r in results if r.status.startswith("error")]
    skipped = [r for r in results if r.status == "skipped (exists)"]

    for r in ok:
        success(f"Imported skill '{r.name}' into quarantine: {r.dest}")
    for r in skipped:
        warn(f"Skill '{r.name}' already exists in omni; add --force to overwrite it.")
    for r in errs:
        error(f"Import failed: {r.status} (source: {source})")

    if ok:
        warn("Imported content cannot execute or auto-trigger until its source, license, and executables are reviewed.")
        info(
            f"After review, run `/skills trust {ok[0].name} --yes` in the REPL or "
            f"`omni skills trust {ok[0].name} --yes` in the shell."
        )
    if errs and not ok:
        info("Sources may be a local directory containing SKILL.md, tool:name, or a git URL with an optional #subdirectory.")
        raise typer.Exit(1)


@app.command("trust")
def trust_cmd(
    ctx: typer.Context,
    name: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm that source, license, and code were reviewed"),
    force: bool = typer.Option(False, "--force", help="Trust without a declared license only when you have usage rights"),
) -> None:
    """Trust a reviewed imported skill for execution and automatic planning."""
    from omni.skills_runtime.install import imported_skill_metadata, set_imported_skill_trust

    paths = ctx.obj.settings().paths
    metadata = imported_skill_metadata(name, paths)
    if metadata:
        kv_table("Import provenance", [
            ("source", metadata.get("source") or "-"),
            ("commit", metadata.get("commit") or "-"),
            ("imported", metadata.get("imported_at") or "-"),
        ])
    if not yes and not typer.confirm("Have you reviewed the skill source, license, and all executable files?", default=False):
        warn("Cancelled; the skill remains quarantined.")
        raise typer.Exit(1)
    result = set_imported_skill_trust(name, paths, trusted=True, allow_missing_license=force)
    if result.executable_files:
        warn("Executable files: " + ", ".join(result.executable_files))
    if result.status != "trusted":
        error(result.message or result.status)
        raise typer.Exit(1)
    success(f"Trusted skill '{name}'; it can now be invoked explicitly and selected by contract routing.")


@app.command("untrust")
def untrust_cmd(ctx: typer.Context, name: str) -> None:
    """Revoke trust and return an imported skill to quarantine."""
    from omni.skills_runtime.install import set_imported_skill_trust

    result = set_imported_skill_trust(name, ctx.obj.settings().paths, trusted=False)
    if result.status != "quarantined":
        error(result.message or result.status)
        raise typer.Exit(1)
    success(f"Skill '{name}' is quarantined and no longer executes or participates in automatic planning.")


def _restore_disabled_skill(ctx: typer.Context, name: str) -> None:
    from omni.skills_runtime.install import restore_disabled_skill

    res = restore_disabled_skill(name, ctx.obj.settings().paths)
    if res.status == "restored":
        success(f"Restored skill '{name}'.")
        if res.tombstone:
            info(f"Updated deletion record: {res.tombstone}")
        info("The skill is visible in `/skills list` and can participate in planning.")
        return
    if res.status == "not_disabled":
        warn(f"Skill '{name}' is not disabled.")
        return
    error(f"Restore failed: {res.message or res.status}")
    raise typer.Exit(1)


@app.command("restore")
def restore_cmd(ctx: typer.Context, name: str = typer.Argument(..., help="Skill name to restore from skills.disabled")) -> None:
    """Restore a skill disabled by ``skills remove``."""
    _restore_disabled_skill(ctx, name)


@app.command("enable")
def enable_cmd(ctx: typer.Context, name: str = typer.Argument(..., help="Skill name to enable or restore")) -> None:
    """Alias for ``restore``."""
    _restore_disabled_skill(ctx, name)


@app.command("remove")
def remove_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Skill name or #n from the current list"),
    all_sources: bool = typer.Option(False, "--all", "-a", help="Also match external Claude, Codex, and OpenClaw libraries"),
    physical: bool = typer.Option(False, "--physical", help="Physically delete an external skill; requires --force"),
    force: bool = typer.Option(False, "--force", help="Confirm a high-risk deletion"),
) -> None:
    """Delete or disable a loaded skill without changing historical tasks.

    user_omni and project_omni entries are deleted by default. Built-in and
    external entries are disabled; external physical deletion requires
    ``--physical --force``.
    """
    from omni.skills_runtime.install import remove_loaded_skill

    reg = _registry(ctx, all_sources=all_sources)
    entry = _resolve_remove_selector(reg, name)
    resolved_name = entry.name if entry is not None else name.lstrip("#")
    res = remove_loaded_skill(
        resolved_name,
        ctx.obj.settings().paths,
        entry=entry,
        physical=physical,
        force=force,
    )
    if res.status == "removed":
        success(f"Physically deleted skill '{res.name}' (source={res.source}).")
        info(f"path: {res.path}")
        if res.tombstone:
            info(f"Deletion record: {res.tombstone}")
    elif res.status == "disabled":
        success(f"Disabled skill '{res.name}' (source={res.source}); historical tasks remain available.")
        info(f"path: {res.path}")
        if res.tombstone:
            info(f"Deletion record: {res.tombstone}")
    elif res.status == "absent":
        hint = disabled_skill_restore_hint(resolved_name, ctx.obj.settings().paths)
        warn(hint or f"Skill '{name}' was not found.")
    elif res.status == "refused":
        error(res.message)
        raise typer.Exit(2)
    else:
        error(f"Remove failed: {res.message or res.status}")
        raise typer.Exit(1)


@app.command("sources")
def sources_cmd(ctx: typer.Context) -> None:
    """Show skill discovery roots and whether they exist."""
    from omni.compat.integrations import discovery_report

    rep = discovery_report(ctx.obj.settings())
    data_table("Skill discovery sources", ["source", "default", "path", "exists"],
               [[k, "✓" if v.get("default") else "—", v["path"], "✓" if v["exists"] else "—"]
                for k, v in rep.items()])
    info(
        "Sources marked default=✓ are indexed automatically. Use `/skills list --all`, "
        "`omni skills list --all`, or add another root to skills.sources."
    )
    info("Project-level discovery walks upward from the current directory to the repository root.")


def render_evolution_report(report: Any, *, installed: bool) -> None:  # noqa: ANN401
    """Render an ``EvolutionReport`` (shared by CLI and REPL)."""
    if not report.outcomes:
        info(f"Scanned {report.considered} successful traces; no reusable candidate reached the clustering threshold.")
        info("Run more similar tasks or lower --min-support.")
        return
    rows = [
        [o.name, str(o.support), o.action, "; ".join(o.reasons)[:70], o.path or "—"]
        for o in report.outcomes
    ]
    data_table(
        f"Self-evolution candidates ({report.considered} traces scanned)",
        ["name", "support", "action", "reasons", "path"], rows,
    )
    if installed:
        success(
            f"Installed {report.installed} gated skills into the active Omni skills directory; "
            "they are available in this session."
        )
    else:
        info("This was a dry run. Add --install to persist candidates that pass the gate.")


async def _open_evolve_ctx(settings: Any):  # noqa: ANN202
    """Shared setup for evolve/proposals: (db, registry, llm-or-None)."""
    from omni.core.llm.client import create_llm_client
    from omni.storage.db import get_database

    settings.paths.ensure_dirs()
    db = get_database(settings.paths.project_db)
    await db.init()
    reg = SkillRegistry(settings)
    reg.build_index()
    try:
        llm = create_llm_client(settings)
    except Exception:  # noqa: BLE001 — distillation degrades to heuristic without a model
        llm = None
    return db, reg, llm


async def _run_evolution_operation(
    settings: Any,
    db: Any,
    llm: Any,
    *,
    command: str,
    execute: Any,
):  # noqa: ANN202, ANN401
    """Give explicit self-evolution commands their own verified cost ledger."""
    from omni.agent.cost import record_text_cost_event
    from omni.runtime.task_recorder import TaskRecorder

    recorder = TaskRecorder(db, project=settings.paths.project_name)
    task = await recorder.create_task(
        session_id="",
        channel="cli",
        user_input=command,
        title=command,
        kind="maintenance",
    )
    await recorder.record_plan(
        task.id,
        {
            "intent_type": "skill_evolution",
            "verification_plan": {"required_events": ["evolution.completed"]},
        },
        status="validated",
    )

    async def meter(component: str, system: str, user: str, output: str) -> None:
        await record_text_cost_event(
            recorder,
            settings,
            llm,
            task.id,
            system=system,
            user_message=user,
            output=output,
            component=component,
        )

    try:
        result = await execute(meter)
    except Exception as exc:
        await recorder.append_event(
            task.id,
            event_type="evolution.completed",
            status="failed",
            name="evolution",
            error=str(exc),
            summary=f"{command} failed",
        )
        await recorder.finish_task(
            task.id,
            status="failed",
            summary=f"{command} failed",
            error=str(exc),
        )
        raise
    await recorder.append_event(
        task.id,
        event_type="evolution.completed",
        status="succeeded",
        name="evolution",
        summary=f"{command} completed",
    )
    await recorder.finish_task(
        task.id,
        status="succeeded",
        summary=f"{command} completed",
    )
    return result


async def _run_evolve(settings: Any, *, install: bool, limit: int, min_support: int):  # noqa: ANN202
    from omni.skills_runtime.evolution import evolve_skills

    db, reg, llm = await _open_evolve_ctx(settings)
    return await _run_evolution_operation(
        settings,
        db,
        llm,
        command="omni skills evolve",
        execute=lambda observer: evolve_skills(
            db,
            reg,
            settings.paths,
            llm,
            install=install,
            limit=limit,
            min_support=min_support,
            on_llm_call=observer,
        ),
    )


@app.command("evolve")
def evolve_cmd(
    ctx: typer.Context,
    install: bool = typer.Option(
        False,
        "--install",
        help="Persist gated candidates in the active Omni skills directory; defaults to dry-run",
    ),
    limit: int = typer.Option(200, "--limit", help="Maximum successful traces to scan"),
    min_support: int = typer.Option(2, "--min-support", help="Minimum similar successes required for a cluster"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON"),
) -> None:
    """Distill reusable skill candidates from successful historical traces.

    The pipeline collects, clusters, distills prompt-only SKILL.md candidates,
    validates names/manifests/contracts, and optionally installs them.
    """
    import asyncio
    import json as _json

    settings = ctx.obj.settings()
    report = asyncio.run(_run_evolve(settings, install=install, limit=limit, min_support=min_support))
    if as_json:
        console.print_json(_json.dumps(report.to_dict(), ensure_ascii=False))
        return
    render_evolution_report(report, installed=install)


# ── self-evolution proposals: the human-review queue ─────────────────────────
proposals_app = typer.Typer(no_args_is_help=False, help="Human review queue for self-evolution proposals")
app.add_typer(proposals_app, name="proposals")


def _proposal_metrics_blurb(prop: Any) -> str:  # noqa: ANN401
    m = prop.metrics or {}
    if prop.kind == "improve_skill":
        return f"failures {m.get('failures', '?')}/{m.get('total', '?')} ({int(float(m.get('failure_rate', 0)) * 100)}%)"
    return f"support={m.get('support', '?')}"


def render_proposals_table(proposals: list[Any], *, title: str) -> None:  # noqa: ANN401
    if not proposals:
        info(
            "No proposals. Run `/skills proposals scan` in the REPL or "
            "`omni skills proposals scan` in the shell."
        )
        return
    rows = [
        [p.id, p.kind, p.skill_name, p.status, _proposal_metrics_blurb(p), "; ".join(p.reasons)[:48]]
        for p in proposals
    ]
    data_table(title, ["id", "kind", "skill", "status", "metrics", "reasons"], rows)


async def _run_scan(settings: Any, *, limit: int, min_support: int, min_failures: int):  # noqa: ANN202
    from omni.skills_runtime.proposals import generate_and_enqueue

    db, reg, llm = await _open_evolve_ctx(settings)
    return await _run_evolution_operation(
        settings,
        db,
        llm,
        command="omni skills proposals scan",
        execute=lambda observer: generate_and_enqueue(
            db,
            reg,
            settings.paths,
            llm=llm,
            limit=limit,
            min_support=min_support,
            min_failures=min_failures,
            on_llm_call=observer,
        ),
    )


@proposals_app.command("scan")
def proposals_scan_cmd(
    ctx: typer.Context,
    limit: int = typer.Option(200, "--limit", help="Maximum historical tasks to scan"),
    min_support: int = typer.Option(2, "--min-support", help="Minimum similar successes for a new-skill cluster"),
    min_failures: int = typer.Option(2, "--min-failures", help="Minimum failures required to propose a skill improvement"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON"),
) -> None:
    """Scan recent outcomes and queue new-skill or improvement proposals."""
    import asyncio
    import json as _json

    settings = ctx.obj.settings()
    summary = asyncio.run(_run_scan(settings, limit=limit, min_support=min_support, min_failures=min_failures))
    added = summary.pop("added", [])
    if as_json:
        console.print_json(_json.dumps(summary, ensure_ascii=False))
        return
    info(
        f"Scanned {summary['considered']} traces: {summary['candidates']} new-skill candidates, "
        f"{summary['improvements']} improvement candidates, {summary['queued']} queued."
    )
    render_proposals_table(list(added), title="Newly queued proposals")
    if summary["queued"]:
        info(
            "Review with `/skills proposals` or `omni skills proposals`; use "
            "`approve <id>` to persist or `reject <id>` to reject."
        )


@proposals_app.command("list")
def proposals_list_cmd(
    ctx: typer.Context,
    show_all: bool = typer.Option(False, "--all", help="Include approved and rejected proposal history"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON"),
) -> None:
    """List self-evolution proposals awaiting human review."""
    import json as _json

    from omni.skills_runtime.proposals import PENDING, default_proposals_path, load_proposals

    settings = ctx.obj.settings()
    path = default_proposals_path(settings.paths)
    proposals = load_proposals(path, status=None if show_all else PENDING)
    if as_json:
        console.print_json(_json.dumps([p.to_dict() for p in proposals], ensure_ascii=False))
        return
    render_proposals_table(proposals, title="Self-evolution proposals" + (" (all)" if show_all else " (pending)"))


@proposals_app.command("show")
def proposals_show_cmd(
    ctx: typer.Context,
    pid: str = typer.Argument(..., help="Proposal ID or unique prefix"),
) -> None:
    """Show complete proposal content."""
    from omni.skills_runtime.proposals import default_proposals_path, get

    settings = ctx.obj.settings()
    prop = get(default_proposals_path(settings.paths), pid)
    if prop is None:
        error(f"Proposal '{pid}' was not found or its prefix is ambiguous.")
        raise typer.Exit(1)
    kv_table(f"Proposal {prop.id}", [
        ("kind", prop.kind), ("skill", prop.skill_name), ("status", prop.status),
        ("metrics", _proposal_metrics_blurb(prop)), ("reasons", "; ".join(prop.reasons)),
        ("created_at", prop.created_at),
    ])
    if prop.kind == "improve_skill":
        console.print("\n[bold]Lesson to append to SKILL.md:[/bold]\n")
        console.print(str(prop.payload.get("lesson") or ""))
    else:
        console.print("\n[bold]New skill SKILL.md:[/bold]\n")
        console.print(str(prop.payload.get("skill_md") or ""))


@proposals_app.command("approve")
def proposals_approve_cmd(
    ctx: typer.Context,
    pid: str = typer.Argument(..., help="Proposal ID or unique prefix to approve and persist"),
) -> None:
    """Approve a proposal, persist it in ~/.omni/skills, and rebuild the index."""
    from omni.skills_runtime.proposals import approve, default_proposals_path

    settings = ctx.obj.settings()
    path = default_proposals_path(settings.paths)
    reg = SkillRegistry(settings)
    reg.build_index()
    try:
        prop, applied_path = approve(path, pid, settings.paths, reg)
    except (OSError, ValueError) as exc:
        error(f"Could not persist proposal: {exc}")
        raise typer.Exit(1) from exc
    if prop is None:
        error(f"Proposal '{pid}' was not found or its prefix is ambiguous.")
        raise typer.Exit(1)
    reg.build_index()
    success(f"Approved and persisted {prop.skill_name} -> {applied_path}")


@proposals_app.command("reject")
def proposals_reject_cmd(
    ctx: typer.Context,
    pid: str = typer.Argument(..., help="Proposal ID or unique prefix to reject"),
) -> None:
    """Reject a proposal without persisting it."""
    from omni.skills_runtime.proposals import default_proposals_path, reject

    settings = ctx.obj.settings()
    prop = reject(default_proposals_path(settings.paths), pid)
    if prop is None:
        error(f"Proposal '{pid}' was not found or its prefix is ambiguous.")
        raise typer.Exit(1)
    info(f"Rejected proposal {prop.id} ({prop.skill_name}).")


def _parse_export_tools(tools: list[str] | None, targets: str) -> list[str]:
    """Merge positional tool names with the ``-t a,b`` form into one list."""
    picked = [t.strip() for t in (tools or []) if t.strip()]
    picked += [t.strip() for t in targets.split(",") if t.strip()]
    return picked


@app.command("export")
def export_cmd(
    ctx: typer.Context,
    tools: list[str] = typer.Argument(
        None, help="Target tools: claude, codex, or openclaw; may specify multiple"
    ),
    targets: str = typer.Option(
        "", "--target", "-t",
        help="Comma-separated alternative: claude,codex,openclaw",
    ),
    all_tools: bool = typer.Option(False, "--all", "-a", help="Export to all supported tools"),
    force: bool = typer.Option(False, "--force", help="Overwrite same-name skills in external tool directories"),
) -> None:
    """Export built-in omni skills to Claude Code, Codex, or OpenClaw.

    Use ``omni skills add`` or ``/skills add`` for the reverse import direction.
    """
    from omni.skills_runtime.install import (
        EXPORT_TOOLS,
        export_builtin_skills,
    )

    s = ctx.obj.settings()
    if all_tools:
        chosen: list[str] | None = list(EXPORT_TOOLS)
    else:
        chosen = _parse_export_tools(tools, targets) or list(s.skills.export_targets)
    valid = set(EXPORT_TOOLS) | {"agents"}
    unknown = [t for t in chosen if t not in valid]
    if unknown:
        warn(f"Unknown tools: {', '.join(unknown)}. Choose from {', '.join(EXPORT_TOOLS)}.")
        chosen = [t for t in chosen if t in valid]
    if not chosen:
        warn(
            "No valid tool was selected. Use `/skills export [claude|codex|openclaw]` in the REPL "
            "or `omni skills export [claude|codex|openclaw]` in the shell."
        )
        return
    results = export_builtin_skills(s.paths, chosen, force=force)
    if not results:
        warn("No built-in skills are available for export, or no valid target was selected.")
        return
    data_table(
        f"Exported built-in skills to {', '.join(chosen)}",
        ["skill", "target", "status", "dest"],
        [[r.name, r.target, r.status, str(r.dest)] for r in results],
    )
    success("Export complete. Claude Code, Codex, or OpenClaw can now discover these skills.")
    info(
        "Import external or local skills with `/skills add <source>` in the REPL "
        "or `omni skills add <source>` in the shell."
    )


@app.command("unexport")
def unexport_cmd(
    ctx: typer.Context,
    tools: list[str] = typer.Argument(
        None, help="Remove from claude, codex, or openclaw; defaults to all exported targets"
    ),
    targets: str = typer.Option("", "--target", "-t", help="Comma-separated alternative"),
    all_tools: bool = typer.Option(False, "--all", "-a", help="Remove from all tools"),
) -> None:
    """Remove built-in skills previously exported by omni without touching user skills."""
    from omni.skills_runtime.install import EXPORT_TOOLS, unexport_builtin_skills

    if all_tools:
        chosen: list[str] | None = list(EXPORT_TOOLS)
    else:
        chosen = _parse_export_tools(tools, targets) or None
    results = unexport_builtin_skills(ctx.obj.settings().paths, chosen)
    if not results:
        info("No exported omni skill records were found.")
        return
    data_table("Removed exported built-in skills", ["skill", "target", "status", "dest"],
               [[r.name, r.target, r.status, str(r.dest)] for r in results])
