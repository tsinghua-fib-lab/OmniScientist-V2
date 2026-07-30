"""Wire OmniScientist into Claude Code / Codex (and shared AGENTS.md).

``omni mcp install codex|claude|both`` registers ``omni mcp serve`` as an
MCP server in the target tool's config (idempotent, preserves existing
content). This makes every OmniScientist research skill available *inside*
Claude Code / Codex.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import tomli_w

from omni.config.paths import codex_home
from omni.data import BUILTIN_SKILLS_DIR

__all__ = [
    "codex_home",
    "register_with_codex",
    "register_with_claude",
    "unregister_with_codex",
    "unregister_with_claude",
    "mcp_registration_status",
    "emit_agents_md",
    "discovery_report",
]

_OMNI_SERVER = {"command": "omni", "args": ["mcp", "serve"]}


def mcp_registration_status(server_name: str = "omniscientist") -> dict[str, bool]:
    """Best-effort: is ``omni`` already registered as an MCP server?

    Mirrors what :func:`register_with_codex` / :func:`register_with_claude`
    write, so the ``omni init`` overview can report the real state. Never
    raises — a missing/corrupt config just reads as "not registered".
    """
    codex = False
    codex_cfg = codex_home() / "config.toml"
    if codex_cfg.is_file():
        try:
            with codex_cfg.open("rb") as fh:
                codex = server_name in (tomllib.load(fh).get("mcp_servers") or {})
        except (OSError, tomllib.TOMLDecodeError):
            codex = False
    claude = False
    claude_cfg = Path.home() / ".claude.json"
    if claude_cfg.is_file():
        try:
            data = json.loads(claude_cfg.read_text(encoding="utf-8"))
            claude = server_name in (data.get("mcpServers") or {})
        except (OSError, json.JSONDecodeError):
            claude = False
    return {"codex": codex, "claude": claude}


def register_with_codex(server_name: str = "omniscientist") -> Path:
    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / "config.toml"
    data: dict = {}
    if cfg_path.is_file():
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    servers = data.setdefault("mcp_servers", {})
    servers[server_name] = dict(_OMNI_SERVER)
    with cfg_path.open("wb") as fh:
        tomli_w.dump(data, fh)
    return cfg_path


def register_with_claude(server_name: str = "omniscientist") -> Path:
    cfg_path = Path.home() / ".claude.json"
    data: dict = {}
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    servers = data.setdefault("mcpServers", {})
    servers[server_name] = dict(_OMNI_SERVER)
    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg_path


def unregister_with_codex(server_name: str = "omniscientist") -> tuple[Path, bool]:
    """Remove only Omni's MCP entry from Codex, preserving unrelated config."""
    cfg_path = codex_home() / "config.toml"
    if not cfg_path.is_file():
        return cfg_path, False
    try:
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return cfg_path, False
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict) or server_name not in servers:
        return cfg_path, False
    del servers[server_name]
    if not servers:
        data.pop("mcp_servers", None)
    with cfg_path.open("wb") as fh:
        tomli_w.dump(data, fh)
    return cfg_path, True


def unregister_with_claude(server_name: str = "omniscientist") -> tuple[Path, bool]:
    """Remove only Omni's MCP entry from Claude Code's user configuration."""
    cfg_path = Path.home() / ".claude.json"
    if not cfg_path.is_file():
        return cfg_path, False
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cfg_path, False
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or server_name not in servers:
        return cfg_path, False
    del servers[server_name]
    if not servers:
        data.pop("mcpServers", None)
    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg_path, True


_AGENTS_TEMPLATE = """# AGENTS.md

This repository is set up to work with **OmniScientist** alongside Codex / Claude Code.

## Research capabilities (via OmniScientist MCP)

If the `omniscientist` MCP server is configured (`omni mcp install`), the following
research tools are available:

- `omni_ask` — delegate a full research question (planning + skills + memory).
- `omni_list_skills` — list available research skills.
- per-skill tools — `openalex-search`, `arxiv-fetch`, `research-ideation`, …

## Conventions

- Long-running research outputs (figures, reports, papers) are written under
  `.omni/projects/<name>/artifacts/` and logged in `NOTEBOOK.md`.
- Skills follow the Claude Code `SKILL.md` format and live in `.claude/skills/`
  and `.omni/.../skills/` — both tools can use them.
"""


def emit_agents_md(target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "AGENTS.md"
    if not path.exists():
        path.write_text(_AGENTS_TEMPLATE, encoding="utf-8")
    return path


def discovery_report(settings) -> dict:
    """Where skills are discovered from (for ``omni skills sources`` / doctor).

    Each entry also reports ``default`` — whether that source is indexed with the
    current ``settings.skills.sources`` (vs. opt-in via ``omni skills list --all``).
    """
    from omni.config.paths import iter_project_skill_dirs

    p = settings.paths
    active = set(settings.skills.sources)

    def _project(subpath: str) -> str:
        dirs = iter_project_skill_dirs(subpath=subpath)
        return str(dirs[0]) if dirs else f"(none up from CWD) {subpath}"

    # (display label, source id, path, exists)
    roots = [
        ("project_omni", "project_omni", _project(".omni/skills"), bool(iter_project_skill_dirs(subpath=".omni/skills"))),
        ("project_claude", "project_claude", _project(".claude/skills"), bool(iter_project_skill_dirs(subpath=".claude/skills"))),
        ("project_agents", "project_agents", _project(".agents/skills"), bool(iter_project_skill_dirs(subpath=".agents/skills"))),
        ("user_omni", "user_omni", str(p.user_skills_dir), p.user_skills_dir.exists()),
        ("user_claude (Claude Code)", "user_claude", str(p.claude_user_skills), p.claude_user_skills.exists()),
        ("user_agents (Codex/OpenClaw)", "user_agents", str(p.agents_user_skills), p.agents_user_skills.exists()),
        ("user_codex (Codex)", "user_codex", str(p.codex_user_skills), p.codex_user_skills.exists()),
        ("user_openclaw (OpenClaw)", "user_openclaw", str(p.openclaw_user_skills), p.openclaw_user_skills.exists()),
        ("builtin", "builtin", str(BUILTIN_SKILLS_DIR), BUILTIN_SKILLS_DIR.exists()),
    ]
    return {
        label: {"path": path, "exists": exists, "default": source_id in active}
        for label, source_id, path, exists in roots
    }
