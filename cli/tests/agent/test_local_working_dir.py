"""Local working-directory + file/shell tooling (Claude-Code-style CLI ops).

Covers the four surfaces that turn the already-implemented builtin file/shell
tools into a usable local agent:

* ``OmniPaths.invocation_cwd`` / ``local_ops_dir`` — bind tools to the launch dir.
* ``OmniAgent._tool_working_dir`` — launch dir for local turns, workspace root for IM.
* the two-tier ``bash`` sandbox guard (system vs workspace-destructive).
* the ``[Local environment]`` system-prompt block and planner routing hint.

All tests are offline and deterministic (no network, no real LLM).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from omni.config import load_settings
from omni.config.paths import OmniPaths, get_paths
from omni.core.approval import classify_tool_call
from omni.core.react_agent import ToolSpec
from omni.core.system_prompt import build_system_prompt, render_local_environment
from omni.core.tool_result import tool_observation
from omni.skills_runtime.builtin_tools.shell import (
    build_shell_tools,
    command_is_destructive,
    command_is_system_blocked,
)
from omni.skills_runtime.context import ExecContext

# ── paths: invocation_cwd + local_ops_dir ────────────────────────────────────


def test_get_paths_records_invocation_cwd_for_named_project(tmp_path):
    # A ``-P`` project keys its store by name (workspace_root is None) but local
    # ops must still land where the user launched, not in the data store.
    paths = get_paths(project="demo", cwd=tmp_path)
    assert paths.workspace_root is None
    assert paths.invocation_cwd == tmp_path.resolve()
    assert paths.local_ops_dir == tmp_path.resolve()


def test_get_paths_records_invocation_cwd_for_pathkeyed_workspace(tmp_path):
    # A non-git, non-project directory is path-keyed; invocation_cwd == cwd.
    paths = get_paths(cwd=tmp_path)
    assert paths.invocation_cwd == tmp_path.resolve()
    assert paths.local_ops_dir == tmp_path.resolve()


def test_local_ops_dir_prefers_invocation_cwd_over_workspace_root(tmp_path):
    launch = tmp_path / "launch"
    repo = tmp_path / "repo"
    launch.mkdir()
    repo.mkdir()
    paths = OmniPaths(
        home=tmp_path / "home",
        project_name="repo",
        project_dir=tmp_path / "home" / "ws",
        workspace_root=repo,
        invocation_cwd=launch,
    )
    assert paths.local_ops_dir == launch


def test_local_ops_dir_guards_filesystem_root(tmp_path):
    # If the launch dir resolves to '/', fall back to the next safe candidate
    # rather than exposing the whole filesystem as a write/exec root.
    paths = OmniPaths(
        home=tmp_path / "home",
        project_name="repo",
        project_dir=tmp_path / "home" / "ws",
        workspace_root=tmp_path / "repo",
        invocation_cwd=Path("/"),
    )
    assert paths.local_ops_dir == (tmp_path / "repo")


def test_local_ops_dir_falls_back_to_project_dir_when_unset(tmp_path):
    project_dir = tmp_path / "home" / "ws"
    paths = OmniPaths(
        home=tmp_path / "home",
        project_name="ws",
        project_dir=project_dir,
        workspace_root=None,
        invocation_cwd=None,
    )
    assert paths.local_ops_dir == project_dir


# ── orchestrator: local turn uses launch dir, IM keeps workspace root ─────────


def _paths_stub(tmp_path) -> OmniPaths:
    return OmniPaths(
        home=tmp_path / "home",
        project_name="repo",
        project_dir=tmp_path / "home" / "ws",
        workspace_root=tmp_path / "repo",
        invocation_cwd=tmp_path / "launch",
    )


def test_tool_working_dir_local_channel_uses_launch_dir(tmp_path):
    from omni.agent.orchestrator import OmniAgent

    paths = _paths_stub(tmp_path)
    agent = types.SimpleNamespace(paths=paths)
    # Local CLI turn → the folder the user launched from.
    assert OmniAgent._tool_working_dir(agent, "cli") == paths.local_ops_dir


def test_tool_working_dir_im_channel_keeps_workspace_root(tmp_path):
    from omni.agent.orchestrator import OmniAgent

    paths = _paths_stub(tmp_path)
    agent = types.SimpleNamespace(paths=paths)
    # IM turns never widen scope to an arbitrary launch directory.
    assert OmniAgent._tool_working_dir(agent, "feishu") == paths.workspace_root


# ── shell: two-tier sandbox guard ────────────────────────────────────────────


def _bash_handler(tmp_path, *, mode: str):
    s = load_settings(cwd=tmp_path)
    s.security.bash_sandbox = mode
    s.security.os_sandbox = "off"  # deterministic offline; no OS backend probe
    ctx = ExecContext(settings=s, paths=s.paths, channel="cli", working_dir=tmp_path)
    return build_shell_tools(ctx)[0].handler


def test_predicate_system_vs_workspace_destructive():
    assert command_is_system_blocked("sudo rm x") is True
    assert command_is_system_blocked("rm -rf build") is False
    # Union classification for the approval gate: either tier is "destructive".
    assert command_is_destructive("rm -rf build") is True
    assert command_is_destructive("git push origin main") is True
    assert command_is_destructive("sudo apt install") is True
    assert command_is_destructive("ls -la") is False


@pytest.mark.asyncio
async def test_workspace_write_allows_rm_rf_past_guard(tmp_path):
    bash = _bash_handler(tmp_path, mode="workspace-write")
    # No-op target (nothing to delete) proves the guard *allows* the command
    # rather than deleting anything real; the approval gate is a separate layer.
    out = await bash({"command": "rm -rf ./__does_not_exist__ && echo RAN"})
    observation = tool_observation(out)
    assert "blocked by sandbox" not in observation
    assert "RAN" in observation


@pytest.mark.asyncio
async def test_readonly_blocks_rm_rf(tmp_path):
    bash = _bash_handler(tmp_path, mode="readonly")
    out = await bash({"command": "rm -rf ./__does_not_exist__"})
    assert "blocked by sandbox (destructive" in tool_observation(out)


@pytest.mark.asyncio
async def test_system_ops_blocked_even_in_workspace_write(tmp_path):
    bash = _bash_handler(tmp_path, mode="workspace-write")
    # ``sudo`` escapes the working directory; blocked before it can execute.
    out = await bash({"command": "sudo echo hi"})
    assert "blocked by sandbox (system" in tool_observation(out)


@pytest.mark.asyncio
async def test_full_mode_bypasses_guard_for_workspace_destructive(tmp_path):
    bash = _bash_handler(tmp_path, mode="full")
    out = await bash({"command": "rm -rf ./__does_not_exist__ && echo RAN"})
    observation = tool_observation(out)
    assert "blocked by sandbox" not in observation
    assert "RAN" in observation


@pytest.mark.asyncio
async def test_bash_runs_in_the_bound_working_directory(tmp_path):
    bash = _bash_handler(tmp_path, mode="workspace-write")
    out = await bash(
        {"command": f'"{sys.executable}" -c "import os; print(os.getcwd())"'}
    )
    # The bound working dir is the process cwd for the command.
    reported = tool_observation(out).strip().splitlines()[-1]
    assert Path(reported).resolve() == tmp_path.resolve()


# ── approval: destructive shell keeps its explicit high-risk classification ───


def test_classify_bash_destructive_and_exec():
    assert classify_tool_call("bash", {"command": "rm -rf x"}).risk == "destructive"
    assert classify_tool_call("bash", {"command": "git push"}).risk == "destructive"
    assert classify_tool_call("bash", {"command": "ls -la"}).risk == "exec"
    assert classify_tool_call("write_file", {"path": "a.txt"}).risk == "write"


# ── system prompt: [Local environment] block + working dir / OS ───────────────


def _local_tools() -> list[ToolSpec]:
    schema = {"type": "object"}
    return [ToolSpec("bash", "run", schema), ToolSpec("read_file", "read", schema)]


def test_local_environment_block_present_with_local_tools_and_workdir():
    prompt = build_system_prompt(
        role="R", tools=_local_tools(), project_name="proj", working_dir="/tmp/work"
    )
    assert "[Local environment]" in prompt
    assert "Working directory: /tmp/work" in prompt
    assert "Operating system:" in prompt
    assert "required_outputs" in prompt
    assert "another task" in prompt.lower()
    assert "$OMNI_OUTPUT_DIR" in prompt
    assert "do not rewrite quotation marks" in prompt.lower()


def test_local_environment_block_absent_without_local_tools():
    prompt = build_system_prompt(
        role="R",
        tools=[ToolSpec("search_corpus", "s", {"type": "object"})],
        project_name="proj",
        working_dir="/tmp/work",
    )
    assert "[Local environment]" not in prompt
    # The working directory is still surfaced in session context.
    assert "Working directory: /tmp/work" in prompt
    assert "required_outputs" in prompt
    assert "this task_id" in prompt


def test_write_file_guidance_is_omitted_when_write_file_is_unavailable():
    tools = [ToolSpec("list_dir", "list", {"type": "object"})]
    prompt = build_system_prompt(role="R", tools=tools, project_name="proj")
    local = render_local_environment(tools, "/tmp/work")
    assert "write_file" not in local
    assert "the host will not write the file" in prompt.lower()


def test_local_environment_says_ledger_tokens_are_not_paths():
    tools = [*_local_tools(), ToolSpec("write_file", "write", {"type": "object"})]
    local = render_local_environment(tools, "/tmp/work")
    assert "draft.section" in local
    assert "ledger" in local


def test_render_local_environment_empty_without_local_tools():
    assert render_local_environment([ToolSpec("web_fetch", "w", {"type": "object"})], "/tmp") == ""


def test_working_dir_absent_when_not_provided():
    prompt = build_system_prompt(role="R", tools=_local_tools(), project_name="proj")
    assert "Working directory:" not in prompt


# ── planner + react policy: local ops reach the tool-capable turn ─────────────


def test_planner_prompt_routes_local_ops_to_react_fallback():
    from omni.agent.model_planner import _planner_system_prompt
    from omni.skills_runtime.registry import SkillRegistry

    prompt = _planner_system_prompt(SkillRegistry(load_settings()))
    assert "react_fallback" in prompt
    # The guidance names local filesystem/shell work as a real job for that turn.
    assert "working directory" in prompt.lower()
    assert "deleting files" in prompt or "shell command" in prompt
    assert "ledger deliverable" in prompt
    assert "write_file" in prompt
    assert "git log" in prompt
    assert "literature.search" in prompt


def test_local_environment_changelog_is_git_first():
    local = render_local_environment(_local_tools(), "/tmp/work")
    assert "git log" in local
    assert "repository-wide grep" in local


def test_react_tool_policy_unblocks_sensitive_tools_when_approver_present():
    from omni.agent.intent_plan import ToolPolicy
    from omni.agent.orchestrator import OmniAgent

    s = load_settings()
    s.security.require_approval = True
    s.security.approval_policy = "untrusted"
    agent = types.SimpleNamespace(
        settings=s,
        approver=lambda _req: None,
        _approved_task_tools={},
        _workspace_auto_tasks=set(),
    )
    policy = ToolPolicy(
        allowed_tools=None,
        blocked_tools=["bash", "write_file", "edit_file", "run_compute"],
    )
    effective = OmniAgent._react_tool_policy(agent, policy, task_id="t1")
    for tool in ("bash", "write_file", "edit_file", "run_compute"):
        assert tool not in (effective.blocked_tools or [])


def test_react_tool_policy_keeps_tools_blocked_without_approver():
    from omni.agent.intent_plan import ToolPolicy
    from omni.agent.orchestrator import OmniAgent

    s = load_settings()
    s.security.require_approval = True
    s.security.approval_policy = "untrusted"
    agent = types.SimpleNamespace(
        settings=s,
        approver=None,
        _approved_task_tools={},
        _workspace_auto_tasks=set(),
    )
    policy = ToolPolicy(
        allowed_tools=None,
        blocked_tools=["bash", "write_file", "edit_file", "run_compute"],
    )
    effective = OmniAgent._react_tool_policy(agent, policy, task_id="t1")
    # No local approver → sensitive tools stay out of the catalog (fail closed).
    assert "bash" in (effective.blocked_tools or [])
