"""SoulAgent persona-overlay turn-boundary adapter (``omni.agent.persona_stoma``).

The adapter reads the ready ``omniscientist`` persona stoma written by the
``soulagent`` skill and overlays it on the sticky base role, per the SoulAgent
contract (read the ready stoma at a stable turn boundary; never hot-swap the base
identity or touch ``~/.omni/role.md``). These tests pin the on-disk protocol
(state.json + ``lock/writing``/``lock/ready`` + ``role.md``), the fail-open
behaviour, and that the overlay reaches the prompt only when a persona is ready —
plus one end-to-end run of the real skill pipeline into the adapter.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from omni.agent.persona_stoma import EMPTY_OVERLAY, load_persona_overlay
from omni.core.system_prompt import build_system_prompt

SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills"


def _write_stoma(
    root: Path,
    *,
    text: str = "Prefer the simplest baseline that could work.",
    host: str = "omniscientist",
    scientist_id: str = "kaiming-he",
    scientist_name: str = "Kaiming He",
    ready: bool = True,
    writing: bool = False,
    stoma_name: str = "role.md",
    state: dict | None = None,
) -> None:
    """Materialise a SoulAgent stoma exactly as ``stoma_writer`` would on disk."""
    lock = root / ".soulagent" / "lock"
    lock.mkdir(parents=True, exist_ok=True)
    if text is not None:
        (root / stoma_name).write_text(text, encoding="utf-8")
    if state is None:
        state = {
            "version": 1,
            "host": host,
            "scientist_id": scientist_id,
            "scientist_name": scientist_name,
        }
    (root / ".soulagent" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    if ready:
        (lock / "ready").touch()
    if writing:
        (lock / "writing").touch()


def test_no_soulagent_state_yields_no_overlay(tmp_path: Path) -> None:
    # Bare working dir, and a bare project ``role.md`` with no ``.soulagent`` state
    # (a user's own file) must both be ignored — presence of state is the gate.
    assert load_persona_overlay(tmp_path) is EMPTY_OVERLAY
    (tmp_path / "role.md").write_text("my own notes", encoding="utf-8")
    assert load_persona_overlay(tmp_path).active is False
    assert load_persona_overlay(None) is EMPTY_OVERLAY


def test_ready_persona_is_read_rendered_and_placed_after_base_role(tmp_path: Path) -> None:
    _write_stoma(tmp_path, text="Residual thinking: make the mapping easy to learn.")
    overlay = load_persona_overlay(tmp_path)

    assert overlay.active and overlay.scientist_id == "kaiming-he"
    block = overlay.render()
    assert block.startswith("[Active scientist persona]")
    assert "Kaiming He" in block and "Residual thinking" in block

    prompt = build_system_prompt(
        role="You are OmniScientist.", tools=[], persona_overlay=block, project_name="p"
    )
    # Overlay is spliced between the sticky base role and the tool catalog, and it
    # reaches the prompt only when a persona is active.
    assert prompt.index("You are OmniScientist.") < prompt.index("[Active scientist persona]")
    assert prompt.index("[Active scientist persona]") < prompt.index("[Available tools]")


def test_prompt_has_no_overlay_when_persona_is_absent(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        role="You are OmniScientist.",
        tools=[],
        persona_overlay=load_persona_overlay(tmp_path).render(),
        project_name="p",
    )
    assert "[Active scientist persona]" not in prompt


def test_writing_lock_suppresses_overlay_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A write in progress (``writing`` held, ``ready`` cleared) must fail open: no
    # overlay this turn rather than blocking the conversation. Keep the wait tiny.
    monkeypatch.setattr("omni.agent.persona_stoma._READY_WAIT_S", 0.05)
    monkeypatch.setattr("omni.agent.persona_stoma._POLL_S", 0.01)
    _write_stoma(tmp_path, ready=False, writing=True)
    assert load_persona_overlay(tmp_path).active is False

    # Once the writer commits (writing cleared, ready set) the overlay appears.
    (tmp_path / ".soulagent" / "lock" / "writing").unlink()
    (tmp_path / ".soulagent" / "lock" / "ready").touch()
    assert load_persona_overlay(tmp_path).active is True


def test_ready_missing_yields_no_overlay(tmp_path: Path) -> None:
    _write_stoma(tmp_path, ready=False)
    assert load_persona_overlay(tmp_path).active is False


def test_other_host_missing_scientist_or_empty_stoma_yield_nothing(tmp_path: Path) -> None:
    # A stoma committed for another host must never be adopted here.
    _write_stoma(tmp_path, host="codex", stoma_name="agent.md")
    assert load_persona_overlay(tmp_path).active is False

    # State without a scientist id is inactive.
    two = tmp_path / "two"
    _write_stoma(two, state={"version": 1, "host": "omniscientist", "scientist_id": ""})
    assert load_persona_overlay(two).active is False

    # An empty stoma body yields nothing even when ready.
    three = tmp_path / "three"
    _write_stoma(three, text="   \n")
    assert load_persona_overlay(three).active is False


def test_corrupt_state_json_is_failopen(tmp_path: Path) -> None:
    (tmp_path / ".soulagent" / "lock").mkdir(parents=True)
    (tmp_path / ".soulagent" / "state.json").write_text("{not json", encoding="utf-8")
    (tmp_path / ".soulagent" / "lock" / "ready").touch()
    (tmp_path / "role.md").write_text("persona", encoding="utf-8")
    assert load_persona_overlay(tmp_path).active is False


def test_adapter_reads_project_stoma_never_home_role(tmp_path: Path) -> None:
    # The persona comes from the project stoma; a same-named file elsewhere (e.g. a
    # home ``role.md`` base) is neither read nor mutated by the adapter.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    home_role = home / "role.md"
    home_role.write_text("BASE OMNISCIENTIST ROLE", encoding="utf-8")

    project = tmp_path / "project"
    _write_stoma(project, text="PROJECT PERSONA STOMA")

    overlay = load_persona_overlay(project)
    assert "PROJECT PERSONA STOMA" in overlay.text
    assert "BASE OMNISCIENTIST ROLE" not in overlay.render()
    # The home base file is untouched (never read, never written).
    assert home_role.read_text(encoding="utf-8") == "BASE OMNISCIENTIST ROLE"


@contextmanager
def _soulagent_core() -> Iterator[object]:
    """Import the ``soulagent`` skill's ``core`` in a fully isolated import scope.

    The skill uses flat module names (``core``, ``graph_pruner`` …) that collide
    with other skills' flat modules (e.g. research-ideation also ships ``core``),
    so we snapshot ``sys.path``/``sys.modules`` and restore them on exit — importing
    the skill here must never shadow another skill's modules for later tests.
    """
    soulagent = str(SKILLS_ROOT / "soulagent")
    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    sys.path.insert(0, soulagent)
    try:
        import core  # type: ignore

        yield core
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - saved_modules:
            sys.modules.pop(name, None)


def _offline_decoder(system_prompt: str, user_prompt: str) -> str:
    """Deterministic stand-in for the LLM decoder (no network).

    The decoder's user prompt embeds the core-principle stances verbatim, so we
    echo them inside the required headings to satisfy ``validate_persona``.
    """
    match = re.search(r"【核心原则】\n(.*?)\n\n【", user_prompt, re.S)
    principles = match.group(1) if match else ""
    return (
        "## persona: Kaiming He\n"
        "### 核心原则\n" + principles + "\n"
        "### 当前任务中的思考方式\n- Prefer the simplest baseline that could work.\n"
        "### 当前取舍\n当前没有触发需要消解的取舍。\n"
        "### 证据来源\n- ResNet eases optimization via residual learning.\n"
    )


def test_end_to_end_soulagent_activate_omniscientist_feeds_prompt(tmp_path: Path) -> None:
    """The real skill pipeline and the adapter agree on the stoma protocol.

    Runs ``soulagent`` activation for ``--host omniscientist`` against the shipped
    example KG (offline decoder), then confirms the adapter surfaces that persona
    on the next turn's prompt and that ``unload`` removes it.
    """
    kg_root = SKILLS_ROOT / "soulagent" / "examples" / "scientist-kg"
    with _soulagent_core() as core:
        result = core.run_pipeline(
            project_root=tmp_path,
            kg_root=kg_root,
            conversation="How should I design an ablation and baseline for my network's loss?",
            scientist_id="kaiming-he",
            host="omniscientist",
            completion_fn=_offline_decoder,
        )
        assert result["status"] == "refreshed"
        assert (tmp_path / "role.md").is_file()

        overlay = load_persona_overlay(tmp_path)
        assert overlay.active and overlay.scientist_id == "kaiming-he"
        prompt = build_system_prompt(
            role="You are OmniScientist.", tools=[], persona_overlay=overlay.render()
        )
        assert "[Active scientist persona]" in prompt and "Kaiming He" in prompt

        core.unload(tmp_path)
        assert load_persona_overlay(tmp_path).active is False
