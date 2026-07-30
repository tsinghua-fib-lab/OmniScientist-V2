"""Repository-level contracts that can only break on somebody else's machine.

Both invariants here were violated at the same time and neither showed up
locally. A Windows checkout rewrote the bytes of digest-pinned assets, failing
the build outright; and a run started from the repository root silently used a
different pytest configuration than the one the project declares, which turned
seven async tests into setup errors and pulled in test suites nobody meant to
run. A macOS or Linux developer sees neither.
"""

from __future__ import annotations

import subprocess
import tomllib
from configparser import ConfigParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PERSONAS = REPO / "skills" / "soulagent" / "assets" / "builtin-scientist-kg"


def _git(*args: str) -> str | None:
    """Run a read-only git query, or return ``None`` outside a checkout."""
    try:
        done = subprocess.run(
            ["git", "-C", str(REPO), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):  # git absent, e.g. an unpacked sdist
        return None
    return done.stdout if done.returncode == 0 else None


def test_digest_pinned_assets_are_exempt_from_line_ending_conversion() -> None:
    """No checkout may rewrite bytes that a manifest pins by SHA-256.

    The persona manifests carry a digest per file and ``cli/hatch_build.py``
    refuses to package a mismatch, so converting LF to CRLF is not a cosmetic
    difference — it is a failed release. Asking git the same question the
    checkout asks keeps the guarantee true for files added later.
    """
    if not PERSONAS.is_dir() or _git("rev-parse", "--is-inside-work-tree") is None:
        pytest.skip("not a git checkout of the repository")

    pinned = sorted(p for p in PERSONAS.rglob("*") if p.is_file())
    assert pinned, "expected bundled personas to be present"

    report = _git("check-attr", "text", "--", *(str(p) for p in pinned))
    assert report is not None

    converted = [
        line for line in report.splitlines() if not line.endswith(": text: unset")
    ]
    assert not converted, (
        "these digest-pinned files are still eligible for line-ending "
        "conversion; mark them '-text' in .gitattributes:\n" + "\n".join(converted)
    )


def test_the_repository_and_project_pytest_configuration_agree() -> None:
    """One test run, wherever it is started from.

    pytest only searches the invocation path's ancestors for a config file, so
    the project's settings in ``cli/pyproject.toml`` are invisible to a run
    started at the repository root — the way CI starts it. The root config
    exists to close that gap, which it only does while it says the same thing.
    """
    root = ConfigParser()
    root.read(REPO / "pytest.ini")
    rooted = root["pytest"]

    project_path = REPO / "cli" / "pyproject.toml"
    project = tomllib.loads(project_path.read_text(encoding="utf-8"))
    declared = project["tool"]["pytest"]["ini_options"]

    assert rooted["asyncio_mode"] == declared["asyncio_mode"]
    assert rooted["filterwarnings"].split() == declared["filterwarnings"]
    assert rooted["markers"].strip().splitlines() == declared["markers"]

    # Spelled relative to their own config file; they must name one directory.
    assert [(REPO / p).resolve() for p in rooted["testpaths"].split()] == [
        (project_path.parent / p).resolve() for p in declared["testpaths"]
    ]


def test_secret_scan_is_license_free_reproducible_and_fail_closed() -> None:
    """Public contributions must be scanned without privileged secrets.

    The commercial Gitleaks Action refuses organization repositories without a
    license and repository secrets are unavailable to fork and Dependabot PRs.
    Keep the open-source CLI download immutable and integrity-checked instead.
    """
    workflow = (REPO / ".github" / "workflows" / "security.yml").read_text(
        encoding="utf-8"
    )

    assert "gitleaks/gitleaks-action" not in workflow
    assert "GITLEAKS_LICENSE" not in workflow
    assert "pull_request_target" not in workflow
    assert "fetch-depth: 0" in workflow
    assert "actions/checkout@v" not in workflow
    assert workflow.count(
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2"
    ) == 2
    assert 'GITLEAKS_VERSION: "8.30.1"' in workflow
    assert (
        'GITLEAKS_LINUX_X64_SHA256: "551f6fc83ea457d62a0d98237cbad105'
        'af8d557003051f41f3e7ca7b3f2470eb"'
    ) in workflow
    assert "sha256sum --check -" in workflow
    assert "Verify Gitleaks rejects a synthetic credential" in workflow
    assert "--redact --exit-code 42" in workflow
    assert "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789" not in workflow
    assert "6bac3c67a4d928f6a371d7b621d019ab14e5" in workflow
    assert 'gitleaks" git --redact --verbose --exit-code 1' in workflow
    assert '--log-opts="--all" .' in workflow
    assert "continue-on-error" not in workflow

    ignored = {
        line
        for line in (REPO / ".gitleaksignore").read_text(encoding="utf-8").splitlines()
        if line
    }
    assert len(ignored) == 21
    paper_index = (
        "skills/paper-review/resources/indexes/iclr2026-reviews/generations/"
        "gen-1d1ad08cca6b4dfbb73f30350d3a050a/papers.jsonl"
    )
    for fingerprint in ignored:
        commit, path, rule, line = fingerprint.split(":")
        assert len(commit) == 40 and set(commit) <= set("0123456789abcdef")
        assert (path, rule) in {
            ("cli/uv.lock", "square-access-token"),
            (paper_index, "facebook-page-access-token"),
            (paper_index, "square-access-token"),
        } or (path.startswith("cli/tests/") and rule == "generic-api-key")
        assert line.isdigit()
