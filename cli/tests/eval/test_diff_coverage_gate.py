from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from omni.eval.diff_coverage_gate import (
    DiffCoverageEvidenceError,
    collect_changed_lines,
    evaluate_diff_coverage,
    initialize_fail_closed_report,
    load_coverage_lines,
    parse_changed_lines,
    resolve_comparison,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_parse_changed_lines_tracks_only_new_side_hunks() -> None:
    patch = """\
diff --git a/cli/src/omni/old.py b/cli/src/omni/new.py
similarity index 90%
rename from cli/src/omni/old.py
rename to cli/src/omni/new.py
--- a/cli/src/omni/old.py
+++ b/cli/src/omni/new.py
@@ -2,2 +2,3 @@
+one
 unchanged
+three
@@ -20 +21,0 @@
-removed
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
"""

    assert parse_changed_lines(patch, source_prefix="cli/src/omni") == {
        "cli/src/omni/new.py": frozenset({2, 3, 4}),
    }


def test_diff_coverage_passes_at_exact_threshold() -> None:
    report = evaluate_diff_coverage(
        changed_lines={"cli/src/omni/example.py": frozenset(range(1, 7))},
        coverage_lines={
            "cli/src/omni/example.py": (
                frozenset({1, 2, 3, 4}),
                frozenset({5}),
            )
        },
        base_ref="base",
        candidate_ref="candidate",
        minimum_percent=80.0,
    )

    assert report.passed is True
    assert report.applicable is True
    assert report.baseline_kind == "explicit_ref"
    assert report.changed_executable_lines == 5
    assert report.covered_changed_lines == 4
    assert report.coverage_percent == 80.0
    assert report.files[0].missing_lines == (5,)


def test_diff_coverage_fails_below_threshold() -> None:
    report = evaluate_diff_coverage(
        changed_lines={"cli/src/omni/example.py": frozenset({1, 2, 3, 4, 5})},
        coverage_lines={
            "cli/src/omni/example.py": (
                frozenset({1, 2, 3}),
                frozenset({4, 5}),
            )
        },
        base_ref="base",
        candidate_ref="candidate",
        minimum_percent=80.0,
    )

    assert report.passed is False
    assert report.coverage_percent == 60.0
    assert report.missing_changed_lines == 2


def test_non_executable_change_is_explicitly_not_applicable() -> None:
    report = evaluate_diff_coverage(
        changed_lines={"cli/src/omni/example.py": frozenset({20})},
        coverage_lines={
            "cli/src/omni/example.py": (
                frozenset({1}),
                frozenset({2}),
            )
        },
        base_ref="base",
        candidate_ref="candidate",
    )

    assert report.passed is True
    assert report.applicable is False
    assert report.changed_executable_lines == 0
    assert report.coverage_percent is None


def test_changed_source_missing_from_coverage_fails_closed() -> None:
    with pytest.raises(
        DiffCoverageEvidenceError,
        match="missing coverage evidence",
    ):
        evaluate_diff_coverage(
            changed_lines={"cli/src/omni/unseen.py": frozenset({1})},
            coverage_lines={},
            base_ref="base",
            candidate_ref="candidate",
        )


def test_load_coverage_lines_rejects_incomplete_payload(tmp_path: Path) -> None:
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(
        json.dumps(
            {
                "files": {
                    "cli/src/omni/example.py": {
                        "executed_lines": [1, 2],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DiffCoverageEvidenceError, match="missing_lines"):
        load_coverage_lines(coverage_path, repository_root=tmp_path)


def test_fail_closed_report_is_valid_json(tmp_path: Path) -> None:
    report_path = tmp_path / "diff-coverage.json"

    initialize_fail_closed_report(
        report_path,
        base_ref="base",
        candidate_ref="candidate",
        failure_stage="coverage_not_started",
    )

    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "gate": "changed_code_coverage",
        "passed": False,
        "baseline_kind": "unresolved",
        "base_ref": "base",
        "base_sha": "",
        "candidate_ref": "candidate",
        "candidate_sha": "",
        "failure_stage": "coverage_not_started",
    }


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def test_explicit_push_or_pr_baseline_and_previous_release_tag(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "release-gate@example.invalid")
    _git(repository, "config", "user.name", "Release Gate")
    source = repository / "cli" / "src" / "omni"
    source.mkdir(parents=True)
    module = source / "example.py"
    module.write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "base")
    base_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "v1.0.0")
    module.write_text("value = 1\nchanged = 2\n", encoding="utf-8")
    _git(repository, "commit", "-qam", "candidate")
    candidate_sha = _git(repository, "rev-parse", "HEAD")

    explicit_ref, explicit_base, explicit_candidate = resolve_comparison(
        repository,
        base_ref=base_sha,
        candidate_ref=candidate_sha,
    )
    tag_ref, tag_base, tag_candidate = resolve_comparison(
        repository,
        base_ref="previous-tag",
        candidate_ref=candidate_sha,
    )
    first_push_ref, first_push_base, first_push_candidate = resolve_comparison(
        repository,
        base_ref="0" * 40,
        candidate_ref=candidate_sha,
        fallback_base_ref="v1.0.0",
    )

    assert (explicit_ref, explicit_base, explicit_candidate) == (
        base_sha,
        base_sha,
        candidate_sha,
    )
    assert (tag_ref, tag_base, tag_candidate) == (
        "v1.0.0",
        base_sha,
        candidate_sha,
    )
    assert (first_push_ref, first_push_base, first_push_candidate) == (
        "merge-base:v1.0.0",
        base_sha,
        candidate_sha,
    )
    assert collect_changed_lines(
        repository,
        base_sha=explicit_base,
        candidate_sha=explicit_candidate,
    ) == {"cli/src/omni/example.py": frozenset({2})}


def test_missing_comparison_ref_fails_closed(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "release-gate@example.invalid")
    _git(repository, "config", "user.name", "Release Gate")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "base")

    with pytest.raises(DiffCoverageEvidenceError, match="no fallback base ref"):
        resolve_comparison(
            repository,
            base_ref="0" * 40,
            candidate_ref="HEAD",
        )


def test_first_release_uses_explicit_immutable_bootstrap(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "release-gate@example.invalid")
    _git(repository, "config", "user.name", "Release Gate")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "bootstrap")
    bootstrap_sha = _git(repository, "rev-parse", "HEAD")
    (repository / "README.md").write_text("candidate\n", encoding="utf-8")
    _git(repository, "commit", "-qam", "candidate")
    candidate_sha = _git(repository, "rev-parse", "HEAD")

    resolved_ref, resolved_base, resolved_candidate = resolve_comparison(
        repository,
        base_ref="previous-tag",
        candidate_ref=candidate_sha,
        fallback_base_ref=bootstrap_sha,
    )

    assert resolved_ref == f"bootstrap:{bootstrap_sha}"
    assert resolved_base == bootstrap_sha
    assert resolved_candidate == candidate_sha


def test_initial_push_can_compare_against_empty_tree(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "release-gate@example.invalid")
    _git(repository, "config", "user.name", "Release Gate")
    source = repository / "cli" / "src" / "omni"
    source.mkdir(parents=True)
    (source / "example.py").write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "initial")

    resolved_ref, base_sha, candidate_sha = resolve_comparison(
        repository,
        base_ref="0" * 40,
        candidate_ref="HEAD",
        fallback_base_ref="HEAD",
    )

    assert resolved_ref == "empty-tree"
    assert collect_changed_lines(
        repository,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
    ) == {"cli/src/omni/example.py": frozenset({1})}


def test_non_ancestor_bootstrap_fails_closed(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "release-gate@example.invalid")
    _git(repository, "config", "user.name", "Release Gate")
    (repository / "README.md").write_text("common\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "common")
    _git(repository, "branch", "feature")
    (repository / "README.md").write_text("main\n", encoding="utf-8")
    _git(repository, "commit", "-qam", "main-only")
    unrelated_bootstrap = _git(repository, "rev-parse", "HEAD")
    _git(repository, "switch", "-q", "feature")
    (repository / "README.md").write_text("feature\n", encoding="utf-8")
    _git(repository, "commit", "-qam", "feature-only")

    with pytest.raises(DiffCoverageEvidenceError, match="is-ancestor"):
        resolve_comparison(
            repository,
            base_ref="previous-tag",
            candidate_ref="HEAD",
            fallback_base_ref=unrelated_bootstrap,
        )


def test_ci_and_release_workflows_use_complete_comparison_history() -> None:
    ci = (_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release = (_REPO_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )

    assert "github.event.pull_request.base.sha || github.event.before" in ci
    assert "origin/${{ github.event.repository.default_branch }}" in ci
    assert "fetch-depth: 0" in ci
    assert "--fallback-base-ref \"$OMNI_DIFF_COVERAGE_FALLBACK_BASE\"" in ci

    assert "--base-ref previous-tag" in release
    assert "fetch-depth: 0" in release
    bootstrap = re.search(
        r"OMNI_DIFF_COVERAGE_BOOTSTRAP_BASE: ([0-9a-f]{40})",
        release,
    )
    assert bootstrap is not None
    # The bootstrap SHA is pinned to the GitHub canonical publish history.
    # File-content mirrors (e.g. Gitee) may diverge in git objects; skip there
    # so local pytest stays green, while the GitHub release clone still hard-
    # fails if the pin is missing or wrong.
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{bootstrap.group(1)}^{{commit}}"],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip(
            f"bootstrap commit {bootstrap.group(1)} not in local history; "
            "required on the GitHub canonical clone used for PyPI release"
        )
    assert "--fallback-base-ref \"$OMNI_DIFF_COVERAGE_BOOTSTRAP_BASE\"" in release


def test_release_compatibility_matrix_is_in_the_publish_dependency_chain() -> None:
    release = (_REPO_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )

    assert "compatibility:" in release
    assert 'python: ["3.11", "3.12", "3.13"]' in release
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in release
    assert "build:\n    needs: compatibility" in release
    assert "smoke:\n    needs: build" in release
    assert "publish:\n    needs: smoke" in release
    assert 'pytest -q -m "not release_gate"' in release
