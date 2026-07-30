"""Shared pytest fixtures: isolated home + scripted offline LLM doubles."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from omni.core.llm.client import ChatWithToolsResult, LLMClient

# Chosen here, at conftest import, because pytest loads this before it imports a
# single test module — and a module that sizes a payload at import time would
# otherwise calibrate it with one estimator and have the run measure it with the
# other. The two agree only to within thirty percent, so that split is enough to
# push a metered run over a ceiling it never approaches in production. The
# fixture below re-declares it per test and resets the memoised encoder; see it
# for why the offline census is the estimator the suite measures with.
os.environ["OMNI_DISABLE_TIKTOKEN"] = "1"


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def cli_text(*parts: str) -> str:
    """Return what a command *said*, independent of how it was painted.

    Rich emits a styled run as its own escape sequence, so a colourised
    ``--input`` reaches the stream as ``-`` and ``-input`` under two spans with
    no literal ``--input`` between them. Typer turns colour on whenever
    ``GITHUB_ACTIONS`` is set, so an assertion written against the plain local
    rendering passes on a developer's machine and fails only on CI. Collapsing
    whitespace additionally absorbs soft wrapping, which moves with the width of
    whatever terminal happened to render the text.
    """
    return " ".join(_ANSI.sub("", "".join(parts)).split())


def store_shaped_home(root: Path, label: str = "") -> Path:
    """Create a store directory under *root* shaped like the one that ships.

    An installed omni keeps its store at ``~/.omni`` and several rules key on
    that name: ``STATE_PROTECTED_DIRS`` is the marker itself, so a store called
    anything else is not write-protected and a test working inside one is
    exercising a configuration nobody runs. The name comes from the production
    constant rather than a literal here, so the two cannot drift apart — that
    drift is what let a filesystem guard pass the suite and refuse the workspace
    artifacts directory in the field.

    Pass *label* when a test needs a store distinct from the fixture's; the
    marker stays the leaf and the label distinguishes the home above it, exactly
    as two real machines' homes differ.
    """
    from omni.config.paths import _PROJECT_MARKER

    store = (root / label / _PROJECT_MARKER) if label else (root / _PROJECT_MARKER)
    store.mkdir(parents=True, exist_ok=True)
    return store


@pytest.fixture
def omni_home() -> Path:
    """The store ``isolated_home`` already put on ``$OMNI_HOME``.

    Most tests that used to build their own home only needed to *know* where it
    was. Reading it back keeps them on the shipping shape for free, and keeps
    one store per test rather than two with the code resolving to whichever the
    environment happened to name.
    """
    return Path(os.environ["OMNI_HOME"])


@pytest.fixture(scope="session", autouse=True)
def _warm_prompt_toolkit_bindings() -> None:
    """Import prompt_toolkit search bindings once before the suite collects work.

    On CPython 3.13 a full-suite import order can occasionally raise
    ``ImportError: cannot import name 'search' from
    'prompt_toolkit.key_binding.bindings'`` while the package is still
    initializing. Warming the submodule up-front makes the REPL TUI tests
    deterministic; the local release gate additionally pins CPython 3.12.
    """
    import prompt_toolkit.key_binding.bindings.search  # noqa: F401
    from prompt_toolkit.key_binding.defaults import load_key_bindings

    load_key_bindings()


@pytest.fixture(autouse=True)
def _headless_prompt_toolkit_session():
    """Give headless Windows tests a pipe input and dummy screen.

    GitHub's Windows runner has no console screen buffer, so prompt_toolkit's
    Win32 output constructor correctly raises ``NoConsoleScreenBufferError``.
    Its documented test setup is an AppSession backed by a pipe and
    ``DummyOutput``. Explicit inputs/outputs used by PTY tests still win.
    """
    if os.name != "nt":
        yield
        return

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            yield


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point OMNI_HOME and HOME at a temp dir so discovery is deterministic.

    Setting HOME isolates ``~/.claude/skills`` and ``~/.codex`` so tests don't
    pick up the real machine's skill library.

    ``OMNI_HOME`` is store-shaped on purpose, and takes its name from the rule
    rather than restating it. An installed omni lives at ``~/.omni``, and rules
    keyed on that name — ``is_write_protected_path`` most sharply — answered one
    way here and the other way in production. That let a guard refuse the
    workspace artifacts directory, the default destination for every generated
    document, while the whole suite reported the feature working. A temp
    directory of the shipping shape costs nothing and keeps the only
    configuration users have under test.
    """
    home = tmp_path / "home"
    omni_home = store_shaped_home(tmp_path)
    # Both must exist before any code opens ``$OMNI_HOME/memory.sqlite3`` /
    # ``control.sqlite3``. Creating only ``HOME`` left global-store init racing
    # to ``unable to open database file`` under the full release suite.
    home.mkdir(parents=True, exist_ok=True)
    omni_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNI_HOME", str(omni_home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    if os.name == "nt":
        # pathlib.Path.home() follows USERPROFILE/HOMEDRIVE on Windows rather
        # than HOME. Keep skill exports and any "~" expansion inside tmp_path.
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("HOMEDRIVE", home.drive)
        monkeypatch.setenv("HOMEPATH", str(home)[len(home.drive) :])
    # Clear provider envs that could leak a real key into tests.
    for var in (
        "OMNI_MODEL_PROVIDER",
        "OMNI_MODEL_API_KEY",
        "OPENAI_API_KEY",
        "OMNI_MODEL_BASE_URL",
        "OMNI_VLM_MODEL",
        "OMNI_VLM_ENDPOINT",
        "OMNI_VLM_API_KEY",
        "OMNI_SEMANTIC_SCHOLAR_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    # init/update own package-manager side effects. Keep the general offline
    # suite hermetic; dedicated runtime-setup tests exercise the real helper and
    # integration tests override these aliases to assert that each lifecycle
    # entry point calls it.
    from omni.cli.commands import init_cmd, update_cmd

    inert_persona_result = type(
        "InertPersonaInstallResult",
        (),
        {"installed": (), "skipped_existing": (), "changed": False},
    )()

    monkeypatch.setattr(init_cmd, "setup_research_pptx_runtime", lambda _paths: False)
    monkeypatch.setattr(update_cmd, "setup_research_pptx_runtime", lambda _paths: False)
    monkeypatch.setattr(init_cmd, "install_builtin_personas", lambda _paths: inert_persona_result)
    monkeypatch.setattr(update_cmd, "install_builtin_personas", lambda _paths: inert_persona_result)
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
        if cmd and cmd[0] == "schtasks" and "/Query" in cmd:
            return 1, "test-not-loaded"
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


@pytest.fixture(autouse=True)
def _shipped_token_estimator(monkeypatch):
    """Measure transcripts with the estimator users actually get.

    ``tiktoken`` is an optional extra, and even installed it loads its BPE
    vocabulary over the network on first use — so whether a machine counts real
    tokens or the offline byte census depends on which extras it installed and
    whether it had a route out the day the cache was cold. CI installed the
    extra and every documented developer setup did not, which meant the two
    measured the same transcript in different units and neither could see it.

    The estimator that ships is the byte census, so that is the one the suite
    measures with, everywhere, by declaration rather than by discovery. Tests
    that are *about* the real tokenizer opt back in (see
    ``tests/unit/test_token_estimate.py``); the memoised encoder is reset here
    so the choice takes effect whatever imported the module first.
    """
    import omni.memory.compaction as compaction

    monkeypatch.setenv("OMNI_DISABLE_TIKTOKEN", "1")
    monkeypatch.setattr(compaction, "_TIKTOKEN_TRIED", False, raising=False)
    monkeypatch.setattr(compaction, "_TIKTOKEN_ENC", None, raising=False)


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
    from omni.skills_runtime.builtin_tools.shell import posix_shell_executable

    argv = [sys.executable, "-c", code]
    if posix_shell_executable() is not None:
        return shlex.join(argv)
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


@lru_cache(maxsize=1)
def has_usable_bash() -> bool:
    """True only for a native POSIX bash suitable for shell-script contracts."""
    if os.name == "nt":
        # Git Bash/WSL may answer a trivial probe, but these tests also require
        # POSIX PATH, chmod, shebang, signal, and process-tree semantics. The
        # Windows installer is covered independently by PowerShell contracts.
        return False
    executable = shutil.which("bash")
    if not executable:
        return False
    try:
        probe = subprocess.run(
            [executable, "-c", "printf omni-bash-ok"],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0 and probe.stdout == b"omni-bash-ok"


def install_fake_dot(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Put a tiny fake Graphviz ``dot`` on PATH so figure tests render offline.

    Emits ``<svg></svg>`` for ``-Tsvg`` and a placeholder for other formats,
    using a POSIX script or Windows batch file as appropriate, then prepends
    ``tmp_path`` to PATH.
    """
    import os

    if os.name == "nt":
        exe = tmp_path / "dot.cmd"
        exe.write_text(
            "@echo off\r\n"
            "set \"fmt=\"\r\n"
            "set \"out=\"\r\n"
            ":args\r\n"
            "if \"%~1\"==\"\" goto render\r\n"
            "set \"arg=%~1\"\r\n"
            "if /I \"%arg:~0,2%\"==\"-T\" set \"fmt=%arg:~2%\"\r\n"
            "if /I not \"%~1\"==\"-o\" goto next\r\n"
            "shift\r\n"
            "set \"out=%~1\"\r\n"
            ":next\r\n"
            "shift\r\n"
            "goto args\r\n"
            ":render\r\n"
            "if /I \"%fmt%\"==\"svg\" "
            "(>\"%out%\" echo ^<svg^>^</svg^>) else (>\"%out%\" echo png)\r\n",
            encoding="utf-8",
        )
    else:
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
