"""Release identity and provenance invariants for the public V2 distribution."""

from pathlib import Path

import yaml

from omni import __version__

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_v2_release_metadata_is_aligned() -> None:
    citation = yaml.safe_load((REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8"))
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    cli_readme = (REPO_ROOT / "cli" / "README.md").read_text(encoding="utf-8")

    assert __version__ == "2.0.0rc3"
    assert citation["title"] == "OmniScientist V2"
    assert citation["version"] == __version__
    assert __version__ in changelog
    assert f"package version `{__version__}`" in readme
    assert f"version `{__version__}`" in cli_readme
    assert f"v{__version__}" in readme
    assert f"v{__version__}" in cli_readme
    assert "official next-generation (V2) implementation" in readme


def test_public_notices_are_synchronized_and_precise() -> None:
    root_notice = (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    cli_notice = (REPO_ROOT / "cli" / "NOTICE").read_text(encoding="utf-8")

    assert root_notice == cli_notice
    assert "arXiv:2511.16931" in root_notice
    assert "clean-room" not in root_notice
    assert "independently maintained Python" in root_notice
