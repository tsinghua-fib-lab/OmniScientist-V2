"""Shared pytest fixtures: isolated home + scripted offline LLM doubles."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from typing import Any

import pytest

from omni.core.llm.client import ChatWithToolsResult, LLMClient


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point OMNI_HOME and HOME at a temp dir so discovery is deterministic.

    Setting HOME isolates ``~/.claude/skills`` and ``~/.codex`` so tests don't
    pick up the real machine's skill library.
    """
    home = tmp_path / "home"
    omni_home = tmp_path / "omni"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNI_HOME", str(omni_home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    # Clear provider envs that could leak a real key into tests.
    for var in (
        "OMNI_MODEL_PROVIDER",
        "OMNI_MODEL_API_KEY",
        "OPENAI_API_KEY",
        "OMNI_MODEL_BASE_URL",
        "OMNI_VLM_MODEL",
        "OMNI_VLM_ENDPOINT",
        "OMNI_VLM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    # init/update own package-manager side effects. Keep the general offline
    # suite hermetic; dedicated runtime-setup tests exercise the real helper and
    # integration tests override these aliases to assert that each lifecycle
    # entry point calls it.
    from omni.cli.commands import init_cmd, update_cmd

    monkeypatch.setattr(init_cmd, "setup_research_pptx_runtime", lambda _paths: False)
    monkeypatch.setattr(update_cmd, "setup_research_pptx_runtime", lambda _paths: False)
    yield
    import asyncio

    from omni.storage.db import reset_databases
    asyncio.run(reset_databases())


@pytest.fixture(autouse=True)
def _inert_os_supervisors(monkeypatch):
    """Never let a test install a real OS supervisor or spawn a detached service.

    The home service registers with launchd / systemd-user / schtasks and, as a
    fallback, spawns a detached ``omni serve run``. A test that reaches those
    real host calls (e.g. an un-mocked ``service_control.enable``) would leak a
    persistent OS unit bound to the test's throwaway ``OMNI_HOME`` — exactly how
    stale launchd agents accumulated in the wild. Neutralise the two side-effect
    seams so the suite can exercise lifecycle logic without touching the host;
    tests that assert supervisor behaviour still monkeypatch ``make_supervisor``.
    """
    from omni.runtime import daemon as _daemon
    from omni.runtime import service_supervisors as _sup

    def _inert_run(cmd, **_kwargs):  # noqa: ANN001
        if cmd and cmd[0] == "launchctl" and (
            "print" in cmd or "bootout" in cmd
        ):
            return 1, "test-not-loaded"
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return 3, "inactive"
        return 0, "test-noop"

    monkeypatch.setattr(_sup, "_run", _inert_run, raising=True)
    monkeypatch.setattr(
        _sup.DetachedSupervisor, "start", lambda self: (True, "test-noop"), raising=True
    )
    # The stray/duplicate reaper scans the host process table (``ps``), which is
    # NOT scoped to the test's throwaway ``OMNI_HOME`` — so an un-mocked reap could
    # SIGTERM the developer's real ``omni serve``. Default the scan to empty so no
    # test kills a real process; the dedicated scan/reap unit tests exercise the
    # real functions via a captured reference / direct call.
    monkeypatch.setattr(
        _daemon, "scan_running_serve_pids", lambda **_kwargs: [], raising=True
    )


@pytest.fixture
def settings():
    from omni.config import load_settings
    return load_settings()


class ScriptedLLM(LLMClient):
    """Deterministic LLM: returns queued ChatWithToolsResult objects in order."""

    def __init__(self, script: list[ChatWithToolsResult] | None = None) -> None:
        self.model = "scripted"
        self._script = list(script or [])
        self.calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatWithToolsResult:
        self.calls += 1
        if self._script:
            return self._script.pop(0)
        return ChatWithToolsResult(content="done")

    async def chat(self, system: str, user: str, **kwargs: Any) -> str:
        return f"summary:{user[:20]}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # tiny deterministic embeddings
        return [[float(len(t) % 7), 1.0, 0.0, 0.5] for t in texts]


def python_shell_command(code: str) -> str:
    """Quote a Python snippet for the platform shell used by the Bash tool."""
    argv = [sys.executable, "-c", code]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


class FactExtractionLLM(ScriptedLLM):
    """Return a language-neutral structured fact contract for memory tests."""

    def __init__(self, facts: list[dict[str, str]]) -> None:
        super().__init__()
        self._facts = [dict(fact) for fact in facts]

    async def chat(self, system: str, user: str, **kwargs: Any) -> str:
        if "Extract only durable memory" in system:
            return json.dumps(self._facts, ensure_ascii=False)
        return await super().chat(system, user, **kwargs)


def _tool_name(tool: Any) -> str:
    """Extract a tool name from either OpenAI-dict or ToolSpec-object form."""
    if isinstance(tool, dict):
        return str((tool.get("function") or {}).get("name") or tool.get("name"))
    return str(getattr(tool, "name", ""))


class CapturingLLM(ScriptedLLM):
    """ScriptedLLM that records the tool catalog it was offered.

    ``tool_names`` holds the names from the most recent ``chat_with_tools`` call;
    ``tool_names_seen`` accumulates the list from every call. This unifies the
    two near-identical local doubles that used to live in
    ``test_orchestrator_intent_plan`` and ``test_executor``.
    """

    def __init__(self, script: list[ChatWithToolsResult] | None = None) -> None:
        super().__init__(script)
        self.tool_names: list[str] = []
        self.tool_names_seen: list[list[str]] = []

    async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatWithToolsResult:  # noqa: ANN001
        names = [_tool_name(tool) for tool in tools]
        self.tool_names = names
        self.tool_names_seen.append(names)
        return await super().chat_with_tools(messages, tools, **kwargs)


class PlanningLLM(CapturingLLM):
    """Deterministic semantic-planner double used by orchestrator/workflow tests.

    Construction:
      * ``PlanningLLM(plan_dict)``  → *sticky*: returns the same plan JSON on
        every ``chat()`` (the old orchestrator-test behaviour).
      * ``PlanningLLM([plan, …])``  → *queue*: pops one plan per ``chat()`` and
        returns ``"{}"`` once drained (the old workflow/routing behaviour).
      * ``planner_gated=True``       → only answers planner prompts (those whose
        system text contains "semantic intent planner"); other ``chat()`` calls
        fall back to the scripted summary (the old benchmark ``PlannerLLM``).

    ``plan_calls`` counts how many planning answers were produced.
    """

    def __init__(
        self,
        plans: dict | list[dict],
        *,
        planner_gated: bool = False,
        script: list[ChatWithToolsResult] | None = None,
    ) -> None:
        super().__init__(script)
        self._sticky = isinstance(plans, dict)
        self._plan = plans if self._sticky else None
        self._plans = [] if self._sticky else list(plans)
        self._planner_gated = planner_gated
        self.plan_calls = 0

    async def chat(self, system: str, user: str, **kwargs: Any) -> str:
        # Native synthesis writes through llm.chat with its own system prompt;
        # answer it with a plausible draft (as a live model would) instead of
        # consuming a queued plan. Key off the exported constant (not prompt
        # prose) so prompt wording changes never silently break these doubles.
        from omni.runtime.final_synthesis import SYNTHESIS_SYSTEM_PROMPT

        if system == SYNTHESIS_SYSTEM_PROMPT:
            return "# Draft\n\n" + "Grounded synthesis of the upstream workflow results. " * 6
        if self._planner_gated and "semantic intent planner" not in system.lower():
            return await super().chat(system, user, **kwargs)
        self.plan_calls += 1
        if self._sticky:
            return json.dumps(self._plan, ensure_ascii=False)
        if not self._plans:
            return "{}"
        return json.dumps(self._plans.pop(0), ensure_ascii=False)


def install_fake_dot(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Put a tiny fake Graphviz ``dot`` on PATH so figure tests render offline.

    Writes a POSIX shell stub that emits ``<svg></svg>`` for ``-Tsvg`` and a
    placeholder for other formats, then prepends ``tmp_path`` to PATH. Shared by
    the artifact-revision and harness-benchmark suites (was duplicated in both).
    """
    import os

    exe = tmp_path / "dot"
    exe.write_text(
        "#!/bin/sh\n"
        "fmt=''; out=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in -T*) fmt=${1#-T} ;; -o) shift; out=$1 ;; esac\n"
        "  shift\n"
        "done\n"
        "if [ \"$fmt\" = svg ]; then printf '<svg></svg>' > \"$out\"; else printf 'png' > \"$out\"; fi\n",
        encoding="utf-8",
    )
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.fixture
def scripted_llm():
    return ScriptedLLM


def pytest_configure(config: pytest.Config) -> None:
    """Register repository markers even when pytest is launched from repo root."""
    config.addinivalue_line(
        "markers",
        "release_gate: evidence-heavy release criteria run by the dedicated CI gate",
    )
