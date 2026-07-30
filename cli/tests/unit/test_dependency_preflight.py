"""Declaration-driven capability acquisition (P1/A1 + A2).

These pin the *generic, non-enumerated* contract that replaced the earlier
"hard-code a PPTX reader in the host" idea:

* A skill declares its Python module deps and a one-shot setup command; a single
  host gate preflights them against omni's own interpreter and, when a module is
  genuinely missing, fails admission with an actionable ``action_required:
  install`` instead of a mid-engine ImportError or the model hand-rolling pip.
* ``open_artifact`` routes a binary artifact to whichever skill *declares* it can
  read that content type (``runtime_requirements.reads``); when none is declared
  it reports honestly and never invites shell/pip. Adding a new readable format
  is a skill declaration, never a host edit.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omni.config.settings import load_settings
from omni.skills_runtime.builtin_tools.recall import _binary_artifact_result
from omni.skills_runtime.builtin_tools.shell import (
    _dependency_install_interception,
    _install_targets,
    build_shell_tools,
)
from omni.skills_runtime.executor import (
    _missing_python_module_action,
    execute_skill,
)
from omni.skills_runtime.manifest import (
    EngineSpec,
    SkillEntry,
    SkillKind,
    missing_python_modules,
    parse_skill_text,
)
from omni.skills_runtime.registry import SkillRegistry

_MISSING = "omni_definitely_absent_module_xyz"
_SETUP = 'uv pip install -e "./cli" --python .venv'


def _ctx(**kw):
    s = load_settings()
    s.paths.ensure_dirs()
    from omni.skills_runtime.context import ExecContext

    return ExecContext(settings=s, paths=s.paths, **kw)


def _reader_entry(*, module: str, name: str = "reader-skill", priority: int = 10) -> SkillEntry:
    return SkillEntry(
        name=name,
        description="declares it reads .pptx",
        kind=SkillKind.PYTHON_ENGINE,
        engine=EngineSpec(module="engine", class_name="Engine"),
        priority=priority,
        requires_python_modules=[module],
        dependency_setup_command=_SETUP,
        reads_extensions=[".pptx"],
        reads_mime=["application/vnd.openxmlformats-officedocument.presentationml.presentation"],
    )


# --------------------------------------------------------------------------- A1

def test_missing_python_modules_uses_running_interpreter() -> None:
    present = SkillEntry(name="p", description="d", requires_python_modules=["json", "pathlib"])
    absent = SkillEntry(name="a", description="d", requires_python_modules=["json", _MISSING])
    assert missing_python_modules(present) == []
    assert missing_python_modules(absent) == [_MISSING]


def test_missing_python_modules_bridges_distribution_import_spelling() -> None:
    # A skill may declare a *distribution* name whose import module differs
    # (``PyYAML`` → ``yaml``). ``find_spec('pyyaml')`` is None, so a find_spec-only
    # gate would fail admission even though the package is installed. The shared
    # oracle resolves it via distribution metadata, keeping admission consistent
    # with the shell install guard (which already treats it as available).
    from omni.skills_runtime.builtin_tools.shell import _already_available

    entry = SkillEntry(name="dist", description="d", requires_python_modules=["pyyaml"])
    assert missing_python_modules(entry) == []
    assert _already_available("pyyaml") is True


def test_python_module_gate_returns_install_action_with_setup_command() -> None:
    entry = SkillEntry(
        name="needs-dep",
        description="d",
        requires_python_modules=[_MISSING],
        dependency_setup_command=_SETUP,
        dependency_error_code="runtime_dependency_missing",
    )
    action = _missing_python_module_action(entry)
    assert action is not None
    assert action["status"] == "error" and action["blocking"] is True
    assert action["action_required"] == {
        "kind": "install",
        "python_modules": [_MISSING],
        "command": _SETUP,
    }
    assert action["setup_command"] == _SETUP
    assert action["error_info"]["code"] == "runtime_dependency_missing"
    assert action["error_info"]["retryable"] is False
    assert _SETUP in action["error"]


def test_python_module_gate_passes_when_declared_modules_present() -> None:
    entry = SkillEntry(name="ok", description="d", requires_python_modules=["json"])
    assert _missing_python_module_action(entry) is None


@pytest.mark.asyncio
async def test_execute_skill_blocks_before_engine_when_module_missing(tmp_path: Path) -> None:
    # The engine must never be dispatched when a declared module is missing:
    # admission fails closed with the install prompt instead of an ImportError.
    marker = tmp_path / "engine-ran"
    mod = tmp_path / "guarded_engine.py"
    mod.write_text(
        "class Engine:\n"
        "    def execute(self, data):\n"
        f"        open({str(marker)!r}, 'w').write('x')\n"
        "        return {'status': 'ok'}\n",
        encoding="utf-8",
    )
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        entry = SkillEntry(
            name="guarded",
            description="d",
            kind=SkillKind.PYTHON_ENGINE,
            engine=EngineSpec(module="guarded_engine", class_name="Engine"),
            requires_python_modules=[_MISSING],
            dependency_setup_command=_SETUP,
        )
        result = await execute_skill(entry, {}, _ctx())
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("guarded_engine", None)
    assert result["action_required"]["kind"] == "install"
    assert result["setup_command"] == _SETUP
    assert not marker.exists(), "engine ran despite a missing declared dependency"


# --------------------------------------------------------------------------- A2

def test_manifest_parses_reads_and_python_modules() -> None:
    text = (
        "---\n"
        "name: deck-reader\n"
        "description: reads decks\n"
        "metadata:\n"
        "  helixforge:\n"
        "    runtime_requirements:\n"
        "      python_modules: [pptx]\n"
        "      dependency_setup_command: 'uv pip install foo'\n"
        "      reads:\n"
        "        extensions: [pptx, .PPTX]\n"
        "        mime: [application/vnd.ms-powerpoint]\n"
        "---\nbody\n"
    )
    entry = parse_skill_text(text, default_name="deck-reader")
    assert entry.requires_python_modules == ["pptx"]
    assert entry.dependency_setup_command == "uv pip install foo"
    # Extensions are normalized to a leading-dot lowercase form; mime lowercased.
    assert entry.reads_extensions == [".pptx", ".pptx"]
    assert entry.reads_mime == ["application/vnd.ms-powerpoint"]


def test_real_research_pptx_does_not_globally_require_pptx() -> None:
    # research-pptx renders via Node/PptxGenJS; python-pptx is only an *optional*
    # template-reuse backend with a PptxGenJS fallback. Declaring it as a global
    # ``requires_python_modules`` would gate outline-only planning on a phase-
    # specific dep — the invariant guarded by test_portable_skill_adaptation.
    # The A1 preflight mechanism is exercised by the synthetic ``deck-reader``
    # fixture above (a skill that *legitimately* hard-requires the module).
    from omni.skills_runtime.manifest import parse_skill_path

    skill_dir = Path(__file__).resolve().parents[3] / "skills" / "research-pptx"
    entry = parse_skill_path(skill_dir, source="builtin")
    assert entry.requires_python_modules == []


def test_registry_find_reader_matches_declared_type() -> None:
    reg = SkillRegistry(load_settings())
    reg.register(_reader_entry(module="json"))
    assert reg.find_reader(".pptx").name == "reader-skill"
    assert reg.find_reader("", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    assert reg.find_reader(".docx") is None
    assert reg.find_reader("") is None


def test_registry_find_reader_prefers_higher_priority() -> None:
    reg = SkillRegistry(load_settings())
    reg.register(_reader_entry(module="json", name="low", priority=1))
    reg.register(_reader_entry(module="json", name="high", priority=100))
    assert reg.find_reader(".pptx").name == "high"


def _binary_file(tmp_path: Path) -> Path:
    p = tmp_path / "deck.pptx"
    p.write_bytes(b"PK\x03\x04\x00\x00binary-ooxml-bytes")
    return p


def test_open_artifact_binary_surfaces_install_prompt_when_reader_dep_missing(tmp_path: Path) -> None:
    reg = SkillRegistry(load_settings())
    reg.register(_reader_entry(module=_MISSING))
    ctx = SimpleNamespace(registry=reg)
    path = _binary_file(tmp_path)
    out = _binary_artifact_result(ctx, str(path), path, path.stat().st_size)
    assert out["binary"] is True
    assert out["reader"] == "reader-skill"
    assert out["action_required"]["kind"] == "install"
    assert out["action_required"]["python_modules"] == [_MISSING]
    assert out["setup_command"] == _SETUP
    # It must steer *away* from ad-hoc installs, not toward bash/pip.
    assert "do not install packages ad hoc" in out["note"].lower()


def test_open_artifact_binary_routes_to_ready_reader(tmp_path: Path) -> None:
    reg = SkillRegistry(load_settings())
    reg.register(_reader_entry(module="json"))  # present in the interpreter
    ctx = SimpleNamespace(registry=reg)
    path = _binary_file(tmp_path)
    out = _binary_artifact_result(ctx, str(path), path, path.stat().st_size)
    assert out["reader"] == "reader-skill"
    assert "action_required" not in out
    assert "reader-skill" in out["note"]
    assert "shell" in out["note"].lower() or "pip" in out["note"].lower()


def test_open_artifact_binary_reports_honestly_when_no_reader(tmp_path: Path) -> None:
    reg = SkillRegistry(load_settings())  # no reader registered
    ctx = SimpleNamespace(registry=reg)
    path = _binary_file(tmp_path)
    out = _binary_artifact_result(ctx, str(path), path, path.stat().st_size)
    assert out["binary"] is True
    assert "reader" not in out
    assert "no reader capability is registered" in out["note"].lower()
    assert "do not" in out["note"].lower()  # never invites shell/pip


# --------------------------------------------------------------------------- A3

def test_registry_find_python_module_provider_bridges_spelling() -> None:
    reg = SkillRegistry(load_settings())
    reg.register(_reader_entry(module="pptx", name="deck-skill"))
    # Declared import name, plus a distribution-style spelling that normalizes alike.
    assert reg.find_python_module_provider(["pptx"]).name == "deck-skill"
    assert reg.find_python_module_provider(["PPTX", "unrelated"]).name == "deck-skill"
    assert reg.find_python_module_provider(["numpy"]) is None
    assert reg.find_python_module_provider([]) is None


@pytest.mark.parametrize(
    "command, expected",
    [
        ("pip install python-pptx==0.6.21", ["python-pptx"]),
        ("python -m pip install numpy scipy", ["numpy", "scipy"]),
        ("uv pip install --break-system-packages pptx", ["pptx"]),
        ("pip3 install 'pandas>=2.0'", ["pandas"]),
        ("echo setup && pip install pytest", ["pytest"]),
        ("pip install pkg[extra]", ["pkg"]),
        # Not resolvable to a named distribution → never intercepted:
        ("pip install -r requirements.txt", []),
        ("pip install -e .", []),
        ("pip install ./local/wheel.whl", []),
        ("pip install git+https://example.com/x.git", []),
        ("ls -la && echo done", []),
    ],
)
def test_install_targets_parsing(command: str, expected: list[str]) -> None:
    assert _install_targets(command) == expected


def test_interception_blocks_reinstall_of_already_available_package() -> None:
    # pydantic is a hard runtime dependency, so it is always importable here.
    ctx = SimpleNamespace(registry=None)
    env = _dependency_install_interception("pip install pydantic", ctx)
    assert env is not None
    assert env.event_output["command_status"] == "blocked"
    assert env.event_output["reason"] == "dependency_already_provided"
    assert "do not install packages ad hoc" in env.observation.lower()


def test_interception_names_declaring_capability() -> None:
    reg = SkillRegistry(load_settings())
    reg.register(_reader_entry(module="pptx", name="deck-skill"))
    ctx = SimpleNamespace(registry=reg)
    env = _dependency_install_interception("uv pip install pptx", ctx)
    assert env is not None
    assert env.event_output["reason"] == "dependency_already_provided"
    assert "deck-skill" in env.observation


def test_interception_leaves_genuinely_new_dependency_alone() -> None:
    ctx = SimpleNamespace(registry=SkillRegistry(load_settings()))
    assert _dependency_install_interception("pip install totally-absent-pkg-zzz9", ctx) is None
    # Requirement files / local installs are never intercepted.
    assert _dependency_install_interception("pip install -r requirements.txt", ctx) is None
    assert _dependency_install_interception("ls -la", ctx) is None


def test_interception_lets_a_mixed_install_proceed_intact() -> None:
    # A command that installs both a redundant package (pydantic, already present)
    # AND a genuinely new one must NOT be blocked wholesale — blocking it would
    # kill the legitimate new install. The guard only fires when *every* target
    # is already provided.
    ctx = SimpleNamespace(registry=SkillRegistry(load_settings()))
    assert (
        _dependency_install_interception(
            "pip install pydantic totally-absent-pkg-zzz9", ctx
        )
        is None
    )


@pytest.mark.asyncio
async def test_bash_tool_intercepts_before_running_a_redundant_install() -> None:
    reg = SkillRegistry(load_settings())
    ctx = _ctx(registry=reg)
    (bash_tool,) = build_shell_tools(ctx)
    env = await bash_tool.handler({"command": "pip install pydantic --break-system-packages"})
    assert env.event_output["command_status"] == "blocked"
    assert env.event_output["reason"] == "dependency_already_provided"
