"""Release-surface contracts that drifted in the 2.0.0rc5 walkthrough."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_docs_do_not_advertise_removed_commands() -> None:
    texts = [
        (ROOT / "README.md").read_text(encoding="utf-8"),
        (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
        (ROOT / "cli" / "docs" / "compatibility.md").read_text(encoding="utf-8"),
    ]
    joined = "\n".join(texts)
    assert "omni project migrate" not in joined
    assert "skills install" not in joined
    assert "skills uninstall" not in joined


def test_build_web_ui_sets_ci_when_not_a_tty() -> None:
    script = (ROOT / "cli" / "scripts" / "build_web_ui.sh").read_text(encoding="utf-8")
    assert "[[ ! -t 0 || ! -t 1 ]]" in script
    assert 'CI="${CI:-true}"' in script
