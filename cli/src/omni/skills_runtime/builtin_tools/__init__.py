"""Claude-Code-compatible builtin tool surface.

Provides the baseline tools that prompt-style skills (from Claude Code /
Codex) assume: read_file, write_file, edit_file, list_dir, bash, grep, glob,
web_fetch. It also adds omni's self-knowledge docs tools (docs_search /
docs_read) so questions about omni itself are answered from its own docs.
This is what lets the CLI *run* community skills, and what prompt-only skills
use to do their work.

On top of that baseline omni adds its *research* action tools (record
hypotheses/claims, cite sources, bind evidence, search the local literature
corpus, log experiment runs). They are appended here so both the main ReAct
loop and prompt-only skill sub-agents get them; they are gated on ``ctx.db`` so
minimal/DB-free callers (some unit tests) are unaffected.
"""

from __future__ import annotations

from omni.skills_runtime.builtin_tools.compute import build_compute_tools
from omni.skills_runtime.builtin_tools.docs import build_docs_tools
from omni.skills_runtime.builtin_tools.fs import build_fs_tools
from omni.skills_runtime.builtin_tools.shell import build_shell_tools
from omni.skills_runtime.builtin_tools.web import build_web_tools
from omni.skills_runtime.context import ExecContext, Tool


def build_builtin_tools(ctx: ExecContext) -> list[Tool]:
    tools = [
        *build_fs_tools(ctx),
        *build_docs_tools(ctx),
        *build_shell_tools(ctx),
        *build_compute_tools(ctx),
        *build_web_tools(ctx),
    ]
    if getattr(ctx, "db", None) is not None:
        # Imported lazily to avoid a hard import cycle (research → storage → …).
        from omni.research.tools import build_research_tools
        from omni.skills_runtime.builtin_tools.recall import build_recall_tools

        tools.extend(build_research_tools(ctx))
        tools.extend(build_recall_tools(ctx))
    # Multi-agent delegation (coordinating → specialist). Depth-gated inside the
    # builder so nested specialists stop offering it at ``max_depth``; imported
    # lazily to avoid a cycle (delegation → subagents → this module).
    from omni.skills_runtime.builtin_tools.delegate import build_delegation_tools

    tools.extend(build_delegation_tools(ctx))
    return tools


__all__ = ["build_builtin_tools", "Tool", "ExecContext"]
